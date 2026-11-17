"""BRIEF an island: one sentence on what a branch is, and the next steps to
take with it, from a cheap model — on demand, cached by the branch tip.

── The call is the host's ──────────────────────────────────────────────────
The `claude -p` recipe (every cheap flag, the env, no `--bare`, thinking off)
is owned by `bridge/tldrack/headless.py` — see the note
there. The plugin asks `context.profiles.headless_call(...)` for it, so a brief
runs on the DEFAULT Claude profile's own login, and `brief_one` takes the
`{argv, env}` it returns. Measured with it: $0.0023 a brief.

── The prompt rides on STDIN ───────────────────────────────────────────────
Why: owned by `bridge/tldrack/headless.py` — see the note there.

── Facts bind the model ────────────────────────────────────────────────────
The harbor already KNOWS ahead/behind, merged, dirty, gone, the PR state and
the delete verdict. They go into the prompt as a facts block and the system
prompt requires `next` to agree with them, so the model describes and
suggests and never contradicts the geometry. `what` and `next` are the only
things it is asked for; everything else on the entry (`sha`, `base_sha`,
`model`, `cost_usd`, `at`) is ours.

── Staleness is a SHA pair ─────────────────────────────────────────────────
An entry is stamped with the branch tip and the base tip it was written
against. Either moving makes it `stale` — shown dimmed with a tag, re-made on
the next BRIEF, never silently. The base is a row (`is_base`), so its sha
costs nothing extra.

The pure halves — target selection, the argv, the env, the prompt, the
response parser, the cap table — are unit-tested without spawning; `brief_one`
is the one I/O function, with `runner=` injectable like `branches.py`'s.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from collections.abc import Callable

from worktree import create as create_mod
from worktree.branches import GIT_LOCAL_TIMEOUT_SECONDS
from worktree.names import scrub_home
from worktree.repo import repo_dir_name

Runner = Callable[..., tuple[int, str, str]]

#: The model alias, not a pinned name: `haiku` tracks the cheapest current
#: model, and the entry records the name the CLI actually resolved.
DEFAULT_MODEL = "haiku"

#: One call — a 1.7 s answer under a 60 s ceiling; the cap is for a wedged
#: login prompt or a network stall, not a budget for the model.
BRIEF_TIMEOUT_SECONDS = 60

#: Per CALL, enforced by the CLI itself (`--max-budget-usd`). Twenty times the
#: measured cost, so a runaway prompt is cut and an ordinary one never is.
PER_CALL_BUDGET_USD = 0.05

#: Per RUN — the sum of the CLI's own `total_cost_usd` — and a call count.
#: Both end the run with a NOTE, not a failure: what was made is kept.
DEFAULT_MAX_CALLS = 64
DEFAULT_MAX_COST_USD = 0.75

#: What the prompt may carry of the branch: the log and the diffstat are cut
#: here, so a 300-commit branch costs the same as a 3-commit one.
CONTEXT_MAX_CHARS = 6000
LOG_MAX_COMMITS = 20
MERGED_LOG_COMMITS = 12
DIFFSTAT_MAX_LINES = 40

WHAT_MAX_CHARS = 140
NEXT_MAX_STEPS = 3
NEXT_MAX_CHARS = 60

SYSTEM_PROMPT = (
    "You write briefs for the git branches of a software repository. You are "
    "given the repository's name, then the FACTS the tooling already "
    "knows about one branch, then its commit log and diffstat.\n"
    'Answer with JSON: {"what": string, "next": [string, ...]}.\n'
    "- what: ONE sentence, at most 140 characters, sentence case, plain words, no "
    "leading 'This branch', and never repeat the branch name. Say what the work IS "
    "or DOES, not that it is a branch.\n"
    "- next: one to three imperative steps, at most 60 characters each, in the "
    "order to take them. They MUST agree with the facts: a merged branch gets "
    "cleanup steps only; a dirty worktree must be committed or discarded before "
    "anything else; a gone upstream is re-pushed or dropped; an open PR is landed "
    "or closed; a branch far behind its base is rebased or merged from base "
    "before it is landed. Name concrete files or commits from the log when that "
    "makes a step actionable.\n"
    "No preamble, no markdown, JSON only."
)

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string"},
        "next": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["what", "next"],
}


# ── pure ─────────────────────────────────────────────────────────────────────


def base_sha_of(rows: list[dict]) -> str | None:
    """The base branch's tip — it is a row, so no extra git call."""
    for row in rows:
        if row.get("is_base") and isinstance(row.get("sha"), str):
            return row["sha"]
    return None


def is_stale(entry: dict | None, row: dict, base_sha: str | None) -> bool:
    """A brief written against another tip, on either side, is stale.

    Total: a missing entry is not "stale", it is absent (`select_targets`
    handles that); an entry missing its stamps is stale, because it cannot be
    shown to match anything.
    """
    if not entry:
        return False
    return entry.get("sha") != row.get("sha") or entry.get("base_sha") != base_sha


def select_targets(rows: list[dict], entries: dict[str, dict], *, only: str | None = None) -> list[str]:
    """Branches to brief, in the document's order (work sorts first there).

    `only`: that branch, always — a RE-BRIEF is a request, not a staleness
    check. Otherwise every non-base row with no entry or a stale one. The
    base is never a target: its next steps are the plugin's own readout.
    """
    if only is not None:
        return [only] if any(row.get("branch") == only and not row.get("is_base") for row in rows) else []
    base_sha = base_sha_of(rows)
    out: list[str] = []
    for row in rows:
        branch = row.get("branch")
        if not isinstance(branch, str) or row.get("is_base"):
            continue
        entry = entries.get(branch)
        if entry is None or is_stale(entry, row, base_sha):
            out.append(branch)
    return out


def build_context_commands(repo_root: Path | str, base: str, row: dict) -> list[list[str]]:
    """The git argv that describes the branch to the model — pure.

    Unlanded work (`ahead > 0`): its commits past the base and the diffstat
    against the merge base. A merged branch has nothing past the base, so its
    own recent history stands in — what it WAS is still the question.
    """
    branch = row["branch"]
    log_format = "--format=%h %cs %s%n%b"
    if (row.get("ahead") or 0) > 0:
        return [
            ["git", "-C", str(repo_root), "log", "--no-merges", f"-n{LOG_MAX_COMMITS}", log_format, f"{base}..{branch}"],
            ["git", "-C", str(repo_root), "diff", "--stat", f"{base}...{branch}"],
        ]
    return [
        ["git", "-C", str(repo_root), "log", "--no-merges", f"-n{MERGED_LOG_COMMITS}", log_format, branch],
    ]


def trim_context(log_text: str, diffstat_text: str) -> tuple[str, str]:
    """Cut the git text to the budget: the diffstat keeps its first lines
    (git puts the total last — dropped with the tail, deliberately, since a
    total for a truncated list would be a lie), the log its first chars."""
    lines = diffstat_text.splitlines()
    if len(lines) > DIFFSTAT_MAX_LINES:
        lines = lines[:DIFFSTAT_MAX_LINES] + [f"… ({len(lines) - DIFFSTAT_MAX_LINES} more files)"]
    diffstat = "\n".join(lines)
    remaining = max(0, CONTEXT_MAX_CHARS - len(diffstat))
    log = log_text if len(log_text) <= remaining else log_text[:remaining].rstrip() + "\n…"
    return log, diffstat


def facts_block(row: dict, base: str, now: float | None = None) -> str:
    """The facts the model must not contradict, one per line — pure."""
    now = time.time() if now is None else now
    ahead, behind = row.get("ahead"), row.get("behind")
    lines = [f"branch: {row.get('branch')}", f"base: {base}"]
    if ahead is None or behind is None:
        lines.append(f"ahead/behind {base}: unknown")
    else:
        lines.append(f"ahead of {base}: {ahead} commits · behind {base}: {behind} commits")
        pr = row.get("pr") if isinstance(row.get("pr"), dict) else {}
        if ahead > 0 and row.get("landed") is True:
            # Squash-landed (branches.py § landed): "no" here would have the
            # model propose landing work that already shipped.
            lines.append(
                f"merged into {base}: yes — landed via PR #{pr.get('number')}; the {ahead} commits ahead are its pre-merge copies"
            )
        else:
            lines.append(f"merged into {base}: {'yes' if ahead == 0 else 'no'}")
    upstream = row.get("upstream")
    if isinstance(upstream, dict):
        if upstream.get("gone"):
            lines.append(f"upstream: {upstream.get('ref')} — GONE (deleted on the remote)")
        else:
            lines.append(
                f"upstream: {upstream.get('ref')} · unpushed {upstream.get('ahead', 0)} · to pull {upstream.get('behind', 0)}"
            )
    else:
        lines.append("upstream: none (never pushed)")
    worktree = row.get("worktree")
    if isinstance(worktree, dict):
        dirty = worktree.get("dirty")
        state = "dirty (uncommitted changes)" if dirty is True else "clean" if dirty is False else "unknown"
        lines.append(f"worktree: {scrub_home(str(worktree.get('path') or ''))} · {state}")
    else:
        lines.append("worktree: none")
    pr = row.get("pr")
    if isinstance(pr, dict):
        lines.append(f"pull request: #{pr.get('number')} {pr.get('state')}{' (draft)' if pr.get('draft') else ''}")
    else:
        lines.append("pull request: none")
    verdict = row.get("delete_verdict")
    if verdict:
        reason = row.get("delete_reason")
        lines.append(f"delete verdict: {verdict}{f' — {reason}' if reason else ''}")
    committed_at = row.get("committed_at")
    if isinstance(committed_at, (int, float)) and committed_at > 0:
        days = max(0, int((now - committed_at) // 86400))
        lines.append(
            f"last commit: {days} days ago by {row.get('author') or 'unknown'} — {row.get('subject') or ''}".rstrip(" —")
        )
    return "\n".join(lines)


def build_prompt(row: dict, base: str, log_text: str, diffstat_text: str, now: float | None = None, repo_name: str = "") -> str:
    log, diffstat = trim_context(log_text, diffstat_text)
    # The repository's directory name: the model's only hint at what the code is
    # for, and nothing a GET body does not already show.
    parts = [
        "REPOSITORY",
        repo_name[:80] or "(unnamed)",
        "",
        "FACTS",
        facts_block(row, base, now),
        "",
        "COMMITS (newest first)",
        log.strip() or "(none past the base)",
    ]
    if diffstat.strip():
        parts += ["", "DIFFSTAT", diffstat.strip()]
    return "\n".join(parts) + "\n"


# jscpd:ignore-start
# A declared cross-package port: plugins do not import each other.
def _extract_json(text: object):
    """First parseable JSON value in `text`, tolerating markdown fences and
    surrounding prose; None when there is none. A copy of the coordinator
    plugin's `coordinator/rank.py::_extract_json`; the reasoning is there."""
    if not isinstance(text, str) or not text.strip():
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    try:
        return json.loads(body)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = body.find(opener)
        end = body.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(body[start : end + 1])
            except ValueError:
                continue
    return None


# jscpd:ignore-end


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:limit].rstrip()


def parse_brief_response(stdout: object) -> tuple[dict | None, str | None]:
    """The CLI's JSON envelope → `({what, next, model, cost_usd}, None)` or
    `(None, why)`. Pure.

    `structured_output` (from `--json-schema`) is the answer when present;
    `result` is parsed as a fallback for a CLI that did not honour the schema.
    `is_error` carries the CLI's own reason in `result` ("Not logged in",
    "budget exceeded") — that is the message to show. Text is whitespace-
    collapsed and cut to the limits the system prompt asked for, so a model
    that ignores them still fits the panel.
    """
    envelope = _extract_json(stdout)
    if not isinstance(envelope, dict):
        return None, "the brief returned no JSON"
    if envelope.get("is_error"):
        reason = envelope.get("result")
        return None, _clean_text(reason, 200) or "the brief failed"
    answer = envelope.get("structured_output")
    if not isinstance(answer, dict):
        answer = _extract_json(envelope.get("result"))
    if not isinstance(answer, dict):
        return None, "the brief returned nothing usable"
    what = _clean_text(answer.get("what"), WHAT_MAX_CHARS)
    raw_next = answer.get("next")
    steps = [_clean_text(step, NEXT_MAX_CHARS) for step in raw_next] if isinstance(raw_next, list) else []
    steps = [step for step in steps if step][:NEXT_MAX_STEPS]
    if not what:
        return None, "the brief had no description"
    what = what[0].upper() + what[1:]  # sentence case was asked for; haiku sometimes forgets
    usage = envelope.get("modelUsage")
    model = next(iter(usage), None) if isinstance(usage, dict) and usage else None
    cost = envelope.get("total_cost_usd")
    return {
        "what": scrub_home(what),
        "next": [scrub_home(step) for step in steps],
        "model": model if isinstance(model, str) else None,
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else 0.0,
    }, None


def decide_run(
    *, done: int, cost_usd: float, max_calls: int = DEFAULT_MAX_CALLS, max_cost_usd: float = DEFAULT_MAX_COST_USD
) -> str | None:
    """None = make the next brief; else the NOTE that ends the run. Pure.

    A cap is a stop, not a failure: everything made before it is kept and
    shown, and the note says what to raise. Checked BEFORE each call, so the
    last brief that pushed the sum over the line still lands.
    """
    if done >= max_calls:
        return f"stopped at {max_calls} briefs this run (WORKTREE_BRIEF_MAX_CALLS)"
    if cost_usd >= max_cost_usd:
        return f"stopped at ${cost_usd:.2f} this run (WORKTREE_BRIEF_MAX_COST_USD {max_cost_usd:g})"
    return None


def resolve_max_calls() -> int:
    raw = os.environ.get("WORKTREE_BRIEF_MAX_CALLS")
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_CALLS
    except ValueError:
        return DEFAULT_MAX_CALLS


def resolve_max_cost_usd() -> float:
    raw = os.environ.get("WORKTREE_BRIEF_MAX_COST_USD")
    try:
        return max(0.01, float(raw)) if raw else DEFAULT_MAX_COST_USD
    except ValueError:
        return DEFAULT_MAX_COST_USD


def resolve_model() -> str:
    return os.environ.get("WORKTREE_BRIEF_MODEL") or DEFAULT_MODEL


# ── I/O ─────────────────────────────────────────────────────────────────────


def brief_one(
    repo_root: Path | str,
    base: str,
    base_sha: str | None,
    row: dict,
    *,
    call: dict,
    model: str = DEFAULT_MODEL,
    runner: Runner | None = None,
    timeout: int = BRIEF_TIMEOUT_SECONDS,
    now: float | None = None,
) -> tuple[dict | None, str | None]:
    """Context → prompt → one `claude -p` → `(entry, None)` or `(None, why)`.

    Runs on the job's background thread, never a request thread. The git
    half is local and bounded; the model half is one blocking call in a
    scratch cwd (no CLAUDE.md, no .mcp.json), run as `call` says: the
    host's `headless_call` `{argv, env}` (module note).
    """
    run = runner or create_mod.run_bounded
    texts: list[str] = []
    for command in build_context_commands(repo_root, base, row):
        code, out, err = run(command, GIT_LOCAL_TIMEOUT_SECONDS)
        if code != 0:
            tail = (err or "").strip().splitlines()
            return None, (tail[-1][:200] if tail else f"git {command[3]} failed")
        texts.append(out)
    log_text = texts[0] if texts else ""
    diffstat_text = texts[1] if len(texts) > 1 else ""
    prompt = build_prompt(row, base, log_text, diffstat_text, now, repo_dir_name(repo_root))

    with tempfile.TemporaryDirectory(prefix="worktree-brief-") as workdir:
        code, out, err = run(call["argv"], timeout, env=call["env"], cwd=workdir, input=prompt)
    if code != 0:
        # The CLI writes its envelope on stdout even when it exits non-zero
        # (a budget stop, a login failure) — read that before the stderr tail.
        parsed, why = parse_brief_response(out)
        if why and why != "the brief returned no JSON":
            return None, why
        tail = (err or "").strip().splitlines()
        return None, (tail[-1][:200] if tail else f"claude exited {code}")
    parsed, why = parse_brief_response(out)
    if parsed is None:
        return None, why
    return {
        "branch": row["branch"],
        "sha": row.get("sha"),
        "base_sha": base_sha,
        "what": parsed["what"],
        "next": parsed["next"],
        "model": parsed["model"] or model,
        "cost_usd": parsed["cost_usd"],
        "at": time.time() if now is None else now,
    }, None
