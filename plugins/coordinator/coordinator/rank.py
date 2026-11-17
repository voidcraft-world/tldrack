"""The optional deep pass: one batched `claude -p` call that CLASSIFIES.

── What this is for, and what it is NOT for ───────────────────────────────
Detection is done by the time this runs. The heuristic layer stands alone and
must keep standing alone: if the CLI is missing, times out, or answers rubbish,
the findings are served UNRANKED rather than not served.

What the model adds is the judgement a phrase list cannot make:

1. **open-work vs routine.** Most `Next:` clauses are reflexes the user
   performs without being reminded — "verify it visually", "restart tldrack",
   "run /code-review", "deploy". Those are severity 4 noise. The minority that
   name real unfinished engineering are the entire point of the feature, and
   separating them is a judgement call, not a regex.
2. **cross-session dedup.** One reflex such as "restart tldrack" can close
   several sessions in a row. One row, not five.

── Cost discipline ────────────────────────────────────────────────────────
ONE batched call for the whole audit, never one per finding, and the payload is
a compact `{key, headline, label, age_hours}` list — never a transcript, never a
`detail`. The call itself is the host's `headless_call` (no tools, no MCP, no
settings, the instructions as the system prompt, the items on stdin, thinking
off, the default Claude profile's own login): owned by
`bridge/tldrack/headless.py` — see the note there. It runs
from a scratch dir, so no project CLAUDE.md is dragged in.

The headlines are text from the user's own transcripts, so they are untrusted
input in the prompt-injection sense. The model has no tools and every field of
its answer is validated against a closed vocabulary and the candidate key set,
so the worst it can do is mis-sort — with ONE exception that needed its own
guard: `duplicate_of` DROPS a row. A key check alone would let steered text
collapse an unrelated finding out of the panel, so a collapse also has to
survive `_looks_like_the_same_task` (see below). Suppressing a row the user
never saw is the one irreversible thing this pass can do.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile

from coordinator.signals import (
    KIND_BLOCKED,
    WORK_CLASS_OPEN,
    WORK_CLASS_ROUTINE,
    severity_for,
    sort_findings,
)

#: Cheap and fast enough to run on every deep audit; the task is classification,
#: not reasoning about code.
RANK_MODEL = "claude-haiku-4-5-20251001"

#: Hard ceiling on the background thread. The route already returned
#: `status: "scanning"`, so the only cost of a timeout is an unranked list.
RANK_TIMEOUT_SECONDS = 90

#: One batch, bounded. Beyond this the prompt stops being compact and the
#: marginal item is deep in a list nobody scrolls to anyway.
RANK_MAX_CANDIDATES = 80

#: The CLI's own stop for one call (`--max-budget-usd`). A full batch of compact
#: one-line items with thinking off costs a few cents; this cuts a runaway,
#: never a real batch.
RANK_BUDGET_USD = 0.25

RANK_INSTRUCTIONS = """\
You are triaging a developer's own Claude Code sessions to find what they left unfinished.

Each ITEM below is one hand-off line pulled from the end of a session. For each one decide:

work_class:
  "open-work" - it names real unfinished engineering work: code still to write, a
                decision still to make, a migration/script still to run, a question
                still open.
  "routine"   - it is a reflex the developer performs without being reminded:
                verify/check it visually, reload the page, restart tldrack,
                run /code-review, deploy, re-run the build, eyeball the UI,
                "your call whether I run X".

duplicate_of:
  the "key" of an EARLIER item in the list that describes the same underlying task,
  or null. Only collapse genuine repeats of one task across sessions.

Answer with ONLY a JSON array, one object per ITEM, in the same order, no prose and
no markdown fence:
[{"key": "...", "work_class": "open-work" | "routine", "duplicate_of": null | "<key>"}]
"""


def rank_candidates(findings: list[dict]) -> list[dict]:
    """The compact payload the model sees.

    `blocked` is excluded on purpose: it is a live process sitting at a prompt
    right now, which is neither routine nor a duplicate of anything, and it must
    never be collapsed away by a classifier.
    """
    candidates = []
    for finding in findings:
        if finding.get("kind") == KIND_BLOCKED:
            continue
        candidates.append(
            {
                "key": finding.get("key"),
                "headline": finding.get("headline"),
                "label": finding.get("label"),
                "age_hours": finding.get("age_hours"),
            }
        )
        if len(candidates) >= RANK_MAX_CANDIDATES:
            break
    return candidates


def build_rank_prompt(candidates: list[dict]) -> str:
    """The user message, on stdin: the ITEMS. The instructions are the system prompt."""
    return "ITEMS:\n" + json.dumps(candidates, indent=1)


def parse_rank_response(text: str) -> dict[str, dict]:
    """Model output → `{key: {work_class, duplicate_of}}`, tolerantly.

    The model is asked for a bare array and usually obliges, but a ```json fence
    or an `{"items": [...]}` wrapper are both common enough that refusing them
    would throw away a good answer over formatting.
    """
    payload = _extract_json(text)
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("findings") or payload.get("results")
    if not isinstance(payload, list):
        return {}
    ranking: dict[str, dict] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        work_class = row.get("work_class")
        duplicate_of = row.get("duplicate_of")
        ranking[key] = {
            "work_class": work_class if work_class in (WORK_CLASS_OPEN, WORK_CLASS_ROUTINE) else None,
            "duplicate_of": duplicate_of if isinstance(duplicate_of, str) and duplicate_of else None,
        }
    return ranking


#: A collapse must be justified by the rows themselves, not only by the model
#: saying so. Two shared significant words AND that being a decent fraction of
#: the shorter headline is enough to let "restart tldrack so docs-reader
#: mounts" collapse onto "restart the bridge to pick up the plugin", and enough
#: to refuse a collapse of two findings that merely both exist.
DUPLICATE_MIN_SHARED_WORDS = 2
DUPLICATE_MIN_OVERLAP = 0.4

#: Words too common to be evidence of anything. Short words are dropped by
#: length, so this only needs the frequent long ones.
_STOPWORDS = frozenset(
    {
        "then",
        "that",
        "this",
        "with",
        "from",
        "into",
        "your",
        "there",
        "here",
        "them",
        "they",
        "have",
        "when",
        "what",
        "which",
        "while",
        "before",
        "after",
        "still",
        "also",
        "just",
        "only",
        "over",
        "back",
        "next",
        "step",
        "steps",
        "action",
        "actions",
        "would",
        "could",
        "should",
        "about",
        "does",
    }
)


def _significant_words(text: object) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {word for word in re.split(r"[^a-z0-9]+", text.lower()) if len(word) >= 4 and word not in _STOPWORDS}


def _looks_like_the_same_task(row: dict, target: dict) -> bool:
    """Do these two findings plausibly describe one task?

    The model's `duplicate_of` is the only field that can DELETE a row, and the
    headlines it judges from are untrusted transcript text — so the collapse is
    re-checked against the rows here, where no model output is involved. A
    genuine cross-session repeat says nearly the same words; text that talked
    the classifier into pointing at an unrelated finding does not.
    """
    left = _significant_words(row.get("headline"))
    right = _significant_words(target.get("headline"))
    if not left or not right:
        return False
    shared = len(left & right)
    if shared < DUPLICATE_MIN_SHARED_WORDS:
        return False
    return shared / min(len(left), len(right)) >= DUPLICATE_MIN_OVERLAP


def apply_ranking(findings: list[dict], ranking: dict[str, dict]) -> list[dict]:
    """Fold a ranking into the findings: set `work_class`, re-derive `severity`,
    drop collapsed duplicates, re-sort. Pure — no I/O, so it is testable and so
    a partial/garbage ranking simply changes less."""
    by_key = {finding.get("key"): finding for finding in findings}
    duplicates: dict[str, str] = {}
    for key, row in ranking.items():
        target = row.get("duplicate_of")
        if key not in by_key or not isinstance(target, str) or target == key or target not in by_key:
            continue
        if _looks_like_the_same_task(by_key[key], by_key[target]):
            duplicates[key] = target

    ranked: list[dict] = []
    for finding in findings:
        key = finding.get("key")
        # ONE hop only. If the survivor is itself marked as a duplicate the
        # chain is incoherent (or a cycle), so nothing is dropped — losing a row
        # to a confused classifier is worse than showing a near-duplicate.
        if key in duplicates and duplicates[key] not in duplicates:
            continue
        updated = dict(finding)
        row = ranking.get(key) or {}
        updated["work_class"] = row.get("work_class")
        updated["severity"] = severity_for(updated.get("kind", ""), updated["work_class"])
        # Additive: Laya's open-work probability (laya_rank.py). The Claude pass has none.
        if isinstance(row.get("p"), float):
            updated["laya_p"] = round(row["p"], 3)
        ranked.append(updated)
    return sort_findings(ranked)


def run_rank(
    candidates: list[dict], *, profiles: object | None, timeout: int = RANK_TIMEOUT_SECONDS, claude_bin: str | None = None
) -> tuple[dict[str, dict], str | None]:
    """Shell out for one batched classification. Returns `(ranking, error)`.

    `profiles` is the host's launch-profile registry (`PluginContext.profiles`),
    which builds the call. Never raises: every failure path answers
    `({}, "why")` so the caller serves the heuristic findings unranked. That is
    the whole contract of this module.
    """
    if not candidates:
        return {}, None
    if profiles is None or not hasattr(profiles, "headless_call"):
        return {}, "this bridge cannot make headless calls (restart it) — findings are unranked"
    call = profiles.headless_call(
        None, system_prompt=RANK_INSTRUCTIONS, model=RANK_MODEL, budget_usd=RANK_BUDGET_USD, cli_bin=claude_bin
    )
    if call is None:
        return {}, "the `claude` CLI was not found — findings are unranked"
    try:
        # A scratch cwd so no project CLAUDE.md / .mcp.json is loaded into a call
        # that only ever needs to read the prompt.
        with tempfile.TemporaryDirectory(prefix="coordinator-rank-") as workdir:
            completed = subprocess.run(
                call["argv"],
                input=build_rank_prompt(candidates),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                env=call["env"],
            )
    except subprocess.TimeoutExpired:
        return {}, f"the classification pass timed out after {timeout}s — findings are unranked"
    except OSError as exc:
        return {}, f"could not run the classification pass ({type(exc).__name__}) — findings are unranked"

    envelope = _extract_json(completed.stdout)
    if completed.returncode != 0:
        # The CLI writes its envelope on stdout even when it exits non-zero (a
        # budget stop, a login failure): its own words beat the stderr tail.
        if isinstance(envelope, dict) and envelope.get("is_error") and isinstance(envelope.get("result"), str):
            return {}, f"the classification pass failed: {envelope['result'][:160]}"
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1][:160] if detail else f"exit {completed.returncode}"
        return {}, f"the classification pass failed: {tail}"

    result = envelope.get("result") if isinstance(envelope, dict) else None
    ranking = parse_rank_response(result if isinstance(result, str) else completed.stdout)
    if not ranking:
        return {}, "the classification pass returned nothing usable — findings are unranked"
    return ranking, None


def _extract_json(text: object):
    """First parseable JSON value in `text`, tolerating markdown fences and
    surrounding prose. Returns None when there is none."""
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
