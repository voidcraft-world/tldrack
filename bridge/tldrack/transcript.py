"""Read the running Claude model + cumulative token usage from a session's
Claude Code transcript.

Claude Code hooks carry a `transcript_path` in their stdin JSON (already forwarded
to POST /hook — see main.py `hook_command`). That transcript is a JSONL file with
one entry per message; every assistant entry holds `message.model` (e.g.
`claude-opus-4-8`) and `message.usage` (`input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`). We surface those to the
frontend so the rack / agent character can label which model is running
and roughly what the session has cost.

Kept OUT of activity.py on purpose: that module is pure/no-I/O and pytest-locked.
File reading lives here. The tracker tails each transcript incrementally (a byte
offset per session) so a multi-megabyte JSONL is not re-parsed on every hook; it
re-sums from scratch only when the file shrinks/rotates or the path changes (a new
Claude session started in the same tmux session).

The same pass folds the session's open BACKGROUND AGENTS and WORKFLOW RUNS
(launched, not yet notified) — the count behind a `background` row's label,
and the in-process children attention.py § 1b holds a parent's ⚠️ for. Why
agents come from here and shells from the process tree: owned by
agent_index.py — see BACKGROUND WORK there.

And the TURN'S TRACE (`fold_turn_trace`): whether the turn that just ended ran
tests (and whether the last run passed), edited files, made a commit or opened a
PR. Counts and booleans, logged beside each verdict by the landing record; the
served question never reads them, a question-v4 challenger does (open_work.py
`turn_facts`).

And it keeps the main thread's CLOSING TEXT — the text of the newest assistant
message — which open_work.py judges at each turn end. In memory only: it is
never stored, logged or put on the wire.

And the STOP FAILURE — whether that newest assistant message is Claude Code's
own synthetic API-error entry rather than a reply (`fold_stop_failure` owns
the shape). It is what tells a turn the agent FINISHED from one the session
limit CUT OFF: both end with the index idle and the row free.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from collections.abc import Iterable

from tldrack.permission_ledger import FEEDBACK_MARKER, REJECT_MARKER, tool_key

# Keys summed across the transcript's assistant messages. Order is display-only.
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


# Hard ceiling on a single incremental read. A transcript is tailed from a byte
# offset, so a normal read is one hook's worth of new lines; this only bites on
# the first read of a huge file, which re-sums on the next hook anyway.
#
# `handle.read()` with no argument was unbounded, and `path` arrives in an HTTP
# body — so pointing it at a multi-gigabyte file read the whole thing into RAM.
MAX_READ_BYTES = 8 * 1024 * 1024

#: The hand-off is the END of the closing message; a long report's head is the
#: work, not the verdict. Also bounds what is held per session.
CLOSING_TEXT_MAX_CHARS = 2_000


#: Every OTHER Claude account's config dir, set once at startup from the launch
#: profiles (`profiles.ProfileRegistry.claude_roots`). Bridge CONFIG, never the
#: request: a root that came off the wire would make the allowlist a formality.
_configured_roots: tuple[str, ...] = ()


def configure_transcript_roots(roots: Iterable[str]) -> None:
    """Widen the allowlist to these config dirs (realpathed). Called by main
    before the status server starts; replaces, never accumulates."""
    global _configured_roots
    _configured_roots = tuple(os.path.realpath(os.path.expanduser(root)) for root in roots)


def _transcript_roots() -> list[str]:
    """The directories transcripts may be read from.

    The env root honours `CLAUDE_CONFIG_DIR` because Claude Code does:
    hardcoding `~/.claude` would silently stop reporting model/usage for anyone
    who relocates it, and a guard that breaks the feature gets deleted rather
    than fixed. Read per-call, not cached at import, so the env var is still
    respected if it is set after this module loads.

    The configured roots are the rest of the machine's Claude accounts: a
    second account pinned by `CLAUDE_CONFIG_DIR=~/.claude-<name>` writes its
    transcripts there, and a single-root allowlist left every one of its
    sessions "model unknown".
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    roots = [os.path.realpath(os.path.expanduser(configured))]
    roots.extend(root for root in _configured_roots if root not in roots)
    return roots


def normalize_transcript_path(path: object) -> str | None:
    """The resolved path the bridge may open, or None when it may not.

    `transcript_path` comes off the wire (POST /hook, POST /usage), so it is
    untrusted input, not a local constant. The Origin guard in main.py is the
    primary defence; this is the second one, because a route that opens an
    arbitrary caller-named file should not be one config mistake away from
    doing it.

    realpath FIRST, then the prefix test: that collapses `..` and resolves
    symlinks, so a link planted inside ~/.claude is judged on its real target
    rather than its location. Same order as the claude-skills plugin's
    `reveal.py`. The test is separator-aware per root, so `~/.claude` never
    admits a sibling like `~/.claude-evil`.

    Returning the resolved path rather than a bool is what lets it double as
    the JOIN KEY between a hook and a statusline report of the same Claude
    session (statusline_models.py). Both sides normalize here, so one owner
    decides what "the same transcript" means and the two can never drift.
    """
    if not isinstance(path, str) or not path:
        return None
    if not path.endswith(".jsonl"):
        return None
    resolved = os.path.realpath(os.path.expanduser(path))
    for root in _transcript_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def is_allowed_transcript(path: str) -> bool:
    """Whether the bridge may open `path` as a Claude Code transcript.
    Guard rationale: owned by normalize_transcript_path — see the note there."""
    return normalize_transcript_path(path) is not None


def _zero_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def _add_usage(totals: dict[str, int], usage: object) -> None:
    """Add one message's `usage` block into the running totals (ignore non-ints)."""
    if not isinstance(usage, dict):
        return
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int):
            totals[key] += value


def parse_entries(lines: list[bytes]) -> list[dict]:
    """JSONL lines → the entries that parse as objects. Pure; garbage is skipped."""
    entries: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def scan_lines(lines: list[bytes]) -> tuple[str | None, dict[str, int]]:
    """Reduce transcript JSONL lines to (last assistant model, summed usage).

    Pure helper (no I/O) so it is unit-testable. Non-JSON lines and non-assistant
    entries are skipped; `model` is the last assistant entry's, `None` if none seen.
    """
    return scan_entries(parse_entries(lines))


def scan_entries(entries: Iterable[dict]) -> tuple[str | None, dict[str, int]]:
    """`scan_lines` over already-parsed entries — one parse feeds every fold."""
    model: str | None = None
    totals = _zero_usage()
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        entry_model = message.get("model")
        if isinstance(entry_model, str) and entry_model:
            model = entry_model
        _add_usage(totals, message.get("usage"))
    return model, totals


#: One `<task-notification>` block, as Claude Code writes it into the
#: transcript (measured 2.1.299): the task id and a terminal status.
TASK_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
TASK_DONE_RE = re.compile(r"<status>\s*(completed|failed|killed)\s*</status>")


def _entry_text(entry: dict) -> str:
    """The text a notification can ride in: a user message's content (a string,
    or a list of text blocks) or a `queue-operation` entry's own `content`."""
    content = entry.get("content") if entry.get("type") == "queue-operation" else None
    if content is None and entry.get("type") == "user":
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _agent_launch(result: dict) -> str | None:
    """An Agent tool's background launch `{isAsync: true, status: "async_launched", agentId}` (2.1.299)."""
    agent_id = result.get("agentId")
    return agent_id if isinstance(agent_id, str) and agent_id else None


#: The `taskType` of a Workflow tool's launch (measured 2.1.319).
WORKFLOW_TASK_TYPE = "local_workflow"


def _workflow_launch(result: dict) -> str | None:
    """A Workflow tool's launch `{status: "async_launched", taskType: "local_workflow", taskId, runId}`.

    It carries no `agentId`, so `fold_background_agents` never saw one, and a parent that ended its
    turn on a running wave stored `free` (what that cost: owned by attention.py § 1b — see the note
    there). Its notification names the `taskId`, exactly as an agent's names its id."""
    task_id = result.get("taskId")
    if result.get("taskType") != WORKFLOW_TASK_TYPE or not isinstance(task_id, str) or not task_id:
        return None
    return task_id


def _fold_launches(open_ids: frozenset[str], entries: Iterable[dict], launched) -> frozenset[str]:
    """`open_ids` plus every launch `launched` reads off a result, minus every id a terminal
    `<task-notification>` names. Pure; one rule for agents and workflows."""
    opened = set(open_ids)
    for entry in entries:
        result = entry.get("toolUseResult")
        if isinstance(result, dict) and result.get("status") == "async_launched":
            launch = launched(result)
            if launch is not None:
                opened.add(launch)
        text = _entry_text(entry)
        if "<task-notification>" not in text:
            continue
        for block in TASK_NOTIFICATION_RE.findall(text):
            task_id = TASK_ID_RE.search(block)
            if task_id and TASK_DONE_RE.search(block):
                opened.discard(task_id.group(1))
    return frozenset(opened)


def fold_background_agents(open_ids: frozenset[str], entries: Iterable[dict]) -> frozenset[str]:
    """The background agents still open after these entries. Pure.

    Opened by an Agent tool result `{isAsync: true, status: "async_launched",
    agentId}`; closed by a `<task-notification>` naming that id with a terminal
    status (both measured 2.1.299). A Bash or Monitor job (`backgroundTaskId`,
    `taskId`) is not an agent — the process tree counts those. A LABEL only:
    a resumed agent notifies again under its id without a new launch, so this
    never decides a state (agent_index.py § BACKGROUND WORK).
    """
    return _fold_launches(open_ids, entries, _agent_launch)


def fold_background_workflows(open_ids: frozenset[str], entries: Iterable[dict]) -> frozenset[str]:
    """The Workflow runs (waves of in-process agents) still open after these entries. Pure; the
    launch shape is `_workflow_launch`'s, the close is `fold_background_agents`'."""
    return _fold_launches(open_ids, entries, _workflow_launch)


# ── the turn's trace ──────────────────────────────────────────────────────────
# What the turn that just ended DID, read off its main-thread tool calls: the Rubric-critic
# features the landing record logs beside each verdict. Logged only, never fed to Laya; why, and
# where their lift is measured: owned by plugins/laya/laya_bridge/landing.py — see the note there.

#: A test runner's command. Bounded as a whole token, so `pytest-cov`, `jest.config.js` or a file
#: named `test.py` do not count.
TEST_COMMAND_RE = re.compile(
    r"(?<![\w.-])(?:pytest|vitest|jest|affected_tests\.py)(?![\w.-])"
    r"|\bnode\b[^\n|;&]*\s--test\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"
    r"|\b(?:go|cargo|swift|deno|mix)\s+test\b"
    r"|\bplaywright\s+test\b|\bgradlew\b[^\n|;&]*\btest"
)
PR_COMMAND_RE = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create)\b")
#: A commit: `git commit`, with any `-C <dir>` / `-c <k=v>` between (`git -C repo commit -m …`). Bounded
#: like the two above, so `git commit-tree`, `git log --grep commit` or a message naming a commit do not count.
COMMIT_COMMAND_RE = re.compile(r"\bgit(?:\s+-[Cc]\s+\S+)*\s+commit(?![\w-])")
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
#: Distinct paths a turn's trace remembers; past it the count stops growing (a bound, not a reading).
TRACE_EDITS_MAX = 256

# What a test run's result said. The LAST test run decides, read off its own tool result. A runner's
# summary sits at the end of its output, so only the tail is read. A failure word with a non-zero count
# beats a pass word (`5 passed, 1 failed`); an errored call that RAN (non-zero exit, not a rejection) is a
# failure too. Output with neither, or a call that never ran (rejected; still running in the background),
# stays UNKNOWN: a guess here would teach a challenger a fact the turn never showed.

#: How much of a test call's output is read: its end, where every runner prints its summary.
TEST_RESULT_TAIL_CHARS = 4000
TEST_FAILED_RE = re.compile(
    r"\b[1-9]\d*\s+(?:failed|failing|failures?|errors?)\b"  # pytest, vitest, jest, mocha
    r"|^#\s*fail\s+[1-9]"  # node --test (TAP)
    r"|\bFAILED\b|^FAIL\b"  # pytest, jest, go, cargo (`test result: FAILED`)
    r"|^BUILD FAILED\b",  # gradle
    re.MULTILINE,
)
TEST_PASSED_RE = re.compile(
    r"\b[1-9]\d*\s+(?:passed|passing)\b"  # pytest, vitest, jest, mocha
    r"|^#\s*pass\s+[1-9]"  # node --test (TAP)
    r"|\btest result: ok\b|^ok\s+\S|^PASS\b"  # cargo, go, jest
    r"|^BUILD SUCCESSFUL\b",  # gradle
    re.MULTILINE,
)
#: Tool calls a trace waits on for a result (a test run, a commit); past it the oldest is forgotten.
TRACE_PENDING_MAX = 16


def _fresh_trace() -> dict:
    return {"tests": False, "edits": frozenset(), "pr": False, "tests_passed": None, "commit": False, "pending": {}}


def _result_text(block: dict) -> str:
    """A tool_result block's text: a string, or the text of its text blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def read_test_result(text: str, *, errored: bool) -> bool | None:
    """Did a test run pass (True), fail (False), or not say (None)? Pure; the note above owns the rule."""
    if REJECT_MARKER in text:
        return None  # rejected: the run never happened
    tail = text[-TEST_RESULT_TAIL_CHARS:]
    if errored or TEST_FAILED_RE.search(tail):
        return False
    return True if TEST_PASSED_RE.search(tail) else None


def _is_turn_start(entry: dict) -> bool:
    """A main-thread prompt: a `user` entry with text and no tool result, typed (not `isMeta`) or
    named by its own `turnOrigin` (a peer message, a scheduled wake). Skill bodies, reminders and
    local-command caveats are `isMeta` with no origin: context inside a turn, never a new one."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    if entry.get("isMeta") and not (isinstance(entry.get("turnOrigin"), str) and entry["turnOrigin"]):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return bool(_entry_text(entry).strip())


def fold_turn_opening(prior: dict | None, entries: Iterable[dict]) -> dict | None:
    """The entry that opened the CURRENT turn after these entries (`_is_turn_start`), or `prior`
    when none of them opens one. Pure. Who wrote it and what it asked is the reader's call
    (open_work.py `turn_request`), so this module stays free of the ledger's vocabulary."""
    for entry in entries:
        if _is_turn_start(entry):
            prior = entry
    return prior


def fold_turn_trace(prior: dict | None, entries: Iterable[dict]) -> dict:
    """`{tests, edits, pr, tests_passed, commit, pending}` of the CURRENT turn after these entries. Pure;
    returns a new dict.

    A main-thread prompt (`_is_turn_start`) starts a fresh trace. `tests`: a Bash call ran a test
    runner (`TEST_COMMAND_RE`); `edits`: the distinct paths an Edit/MultiEdit/Write/NotebookEdit
    named (in memory only — the record keeps the count); `pr`: a Bash call opened a pull request.
    `tests_passed`: what the LAST test call's result said (`read_test_result`; None until it lands, and
    when it never says); `commit`: a `git commit` call whose result came back without an error.
    `pending` pairs those calls with their results across reads (tool_use id → `test` | `commit`).
    Sidechain (subagent) entries never speak for the main thread."""
    trace = {**_fresh_trace(), **prior} if prior else _fresh_trace()
    trace["pending"] = dict(trace["pending"])
    for entry in entries:
        if _is_turn_start(entry):
            trace = _fresh_trace()
            continue
        message = entry.get("message")
        if entry.get("isSidechain") or not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if entry.get("type") == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    _fold_result(trace, block)
            continue
        if entry.get("type") != "assistant":
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, tool_input = block.get("name"), block.get("input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            if name == "Bash" and isinstance(tool_input.get("command"), str):
                command = tool_input["command"]
                if TEST_COMMAND_RE.search(command) is not None:
                    trace["tests"], trace["tests_passed"] = True, None
                    _await(trace, block.get("id"), "test")
                if COMMIT_COMMAND_RE.search(command) is not None:
                    _await(trace, block.get("id"), "commit")
                trace["pr"] = trace["pr"] or PR_COMMAND_RE.search(command) is not None
            elif name in EDIT_TOOLS:
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if isinstance(path, str) and path and len(trace["edits"]) < TRACE_EDITS_MAX:
                    trace["edits"] = trace["edits"] | {path}
    return trace


def _await(trace: dict, call_id: object, kind: str) -> None:
    """Wait on one call's result. A newer test call replaces an older one still waiting: the LAST run decides.
    A call that is both (`pytest && git commit`) waits as a test: its one result cannot tell the two apart."""
    if not isinstance(call_id, str):
        return
    pending = trace["pending"]
    if kind == "test":
        for old in [key for key, value in pending.items() if value == "test"]:
            del pending[old]
    pending.setdefault(call_id, kind)
    while len(pending) > TRACE_PENDING_MAX:
        del pending[next(iter(pending))]


def _fold_result(trace: dict, block: dict) -> None:
    """One tool result against the call it answers, when the trace waits on that call."""
    kind = trace["pending"].pop(block.get("tool_use_id"), None)
    if kind is None:
        return
    errored = bool(block.get("is_error"))
    if kind == "test":
        trace["tests_passed"] = read_test_result(_result_text(block), errored=errored)
    elif not errored:
        trace["commit"] = True


#: A closing line's trailing decoration, peeled before the `?` test (`**Ship it?**`, `(y/n)?`).
_TRAILING_MARKUP = " \t*_`)\"'»”’"


def trace_reading(trace: dict | None, closing: str | None) -> dict:
    """A folded trace as its readers take it (`TranscriptTracker.turn_trace`): counts and booleans, no path. Pure;
    laya-lab's export reads a turn the same way."""
    trace = {**_fresh_trace(), **(trace or {})}
    return {
        "ran_tests": trace["tests"],
        "edited_files": len(trace["edits"]),
        "opened_pr": trace["pr"],
        "ended_on_question": ends_on_question(closing),
        "tests_passed": trace["tests_passed"],
        "made_commit": trace["commit"],
    }


def ends_on_question(text: str | None) -> bool:
    """Does the hand-off's LAST non-empty line ask something? Pure. A question mid-message is
    narration; the last line is where "want me to go on?" lives."""
    if not text:
        return False
    for line in reversed(text.splitlines()):
        stripped = line.rstrip(_TRAILING_MARKUP)
        if stripped.strip():
            return stripped.endswith("?")
    return False


#: The error and limit words are Claude Code's own tokens (`rate_limit`,
#: `five_hour`); anything else is read as `unknown` / None.
STOP_FAILURE_TOKEN_RE = re.compile(r"[a-z0-9_]{1,40}")

#: Claude Code's one-line notice ("You've hit your session limit · resets
#: 3:00pm (Europe/Lisbon)"); a cap, not a format.
STOP_FAILURE_MESSAGE_MAX_CHARS = 300


def fold_stop_failure(prior: dict | None, entries: Iterable[dict]) -> dict | None:
    """Why the main thread's last turn was CUT OFF, after these entries — or None. Pure.

    A turn an API error ends (the session or weekly limit, an overload, a lapsed
    login) writes ONE synthetic assistant entry — `isApiErrorMessage: true`,
    `error`, model `<synthetic>` — and fires `StopFailure` instead of `Stop`
    (measured 2.1.306). A rate limit also carries
    `apiErrorParams.rate_limit_info` with the window (`rateLimitType`) and the
    exact epoch it resets (`resetsAt`), which the notice text only rounds.

    The NEWEST main-thread assistant entry decides, so the next real reply
    clears it with no event of its own. User entries are skipped: a "continue"
    typed while still limited earns a fresh error entry, not a reply.
    Sidechain (subagent) entries never speak for the main thread — a workflow
    agent can hit the limit while the main thread carries on.
    """
    reading = prior
    for entry in entries:
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            reading = _stop_failure_of(entry)
    return reading


def _stop_failure_of(entry: dict) -> dict | None:
    """One main-thread assistant entry → its stop failure, or None for a real reply."""
    if entry.get("isApiErrorMessage") is not True:
        return None
    error = entry.get("error")
    params = entry.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    info = info if isinstance(info, dict) else {}
    limit = info.get("rateLimitType")
    resets_at = info.get("resetsAt")
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = " ".join(
        block["text"].strip()
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
    )
    return {
        "error": error if isinstance(error, str) and STOP_FAILURE_TOKEN_RE.fullmatch(error) else "unknown",
        "limit": limit if isinstance(limit, str) and STOP_FAILURE_TOKEN_RE.fullmatch(limit) else None,
        "resets_at": int(resets_at)
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at > 0
        else None,
        "message": text[:STOP_FAILURE_MESSAGE_MAX_CHARS] or None,
        # When the turn was cut off: what orders it against the account's later turns
        # (account_standing.py). None for an entry with no readable timestamp.
        "at": _entry_epoch(entry.get("timestamp")),
    }


def _entry_epoch(value: object) -> float | None:
    """A transcript entry's ISO-8601 `timestamp` as epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def fold_closing_text(prior: tuple[str | None, str], entries: Iterable[dict]) -> tuple[str | None, str]:
    """`(message id, text)` of the newest MAIN-THREAD assistant message, after these entries. Pure.

    Claude Code writes one entry per content block, all carrying the message's `message.id`, so
    a message's text blocks are joined across entries. A newer message replaces the text even
    when it holds only tool calls: text from before a tool call is mid-turn narration, not the
    hand-off. Sidechain (subagent) entries never speak for the main thread.
    """
    message_id, text = prior
    for entry in entries:
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        entry_id = message.get("id") if isinstance(message.get("id"), str) else None
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        chunk = "\n".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        )
        if entry_id is None or entry_id != message_id:
            message_id, text = entry_id, chunk
        elif chunk:
            text = f"{text}\n{chunk}" if text else chunk
        text = text[-CLOSING_TEXT_MAX_CHARS:]
    return message_id, text


#: How many of a transcript's newest tool calls `fold_tool_events` keeps — the
#: permission ledger only ever binds the last few, minutes old at most.
TOOL_EVENTS_MAX = 64


def _iso_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fold_tool_events(prior: dict[str, dict], entries: Iterable[dict]) -> dict[str, dict]:
    """tool_use id → `{name, key, at, outcome, result_at, feedback, failed}` for the newest
    TOOL_EVENTS_MAX main-thread tool calls. Pure; returns a new dict.

    `outcome` is None until the call's tool_result lands, then `approved` (the tool
    ran, whatever it returned) or `denied` (Claude Code's rejection text — owned
    by permission_ledger.py, the one reader). `failed` is a tool that RAN and
    errored (`is_error`, not a rejection): stuck.py's repeat-failure count.
    Sidechain (subagent) entries are skipped: a subagent's calls live in its own
    transcript."""
    events = dict(prior)
    for entry in entries:
        message = entry.get("message")
        if entry.get("isSidechain") or not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        at = _iso_epoch(entry.get("timestamp"))
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                events[block["id"]] = {
                    "name": block.get("name"),
                    "key": tool_key(block.get("name"), block.get("input")),
                    "at": at,
                    "outcome": None,
                    "result_at": None,
                    "feedback": False,
                    "failed": False,
                }
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in events:
                text = block.get("content")
                text = text if isinstance(text, str) else json.dumps(text, default=str)
                rejected = bool(block.get("is_error")) and REJECT_MARKER in text
                events[block["tool_use_id"]] = {
                    **events[block["tool_use_id"]],
                    "outcome": "denied" if rejected else "approved",
                    "result_at": at,
                    "feedback": rejected and FEEDBACK_MARKER in text,
                    "failed": bool(block.get("is_error")) and not rejected,
                }
    if len(events) > TOOL_EVENTS_MAX:
        events = dict(list(events.items())[-TOOL_EVENTS_MAX:])
    return events


class TranscriptTracker:
    """Per-session incremental reader of Claude Code transcripts.

    `update(session, path)` tails the file since the last read, folds any new
    assistant usage into the session's running totals, and returns
    `(model, usage)` for storage in the StatusStore. Thread-safe: the status
    server handles hooks on multiple threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session -> {path, real_path, offset (bytes), model, usage, agents, workflows, closing,
        #             stop_failure, tools, trace, opening}
        self._state: dict[str, dict] = {}

    def update(self, session: str, path: str | None) -> tuple[str | None, dict[str, int] | None]:
        """Refresh a session's model/usage from its transcript.

        Returns the last-known `(model, usage)` when `path` is missing or the
        file can't be read yet — never wipes a prior reading on a transient miss.
        """
        with self._lock:
            prior = self._state.get(session)
            # A refused path is treated exactly like a missing one — keep the
            # prior reading rather than raising. A bad `transcript_path` must
            # not turn into a 500 on a hook the user's shell is waiting on.
            real_path = normalize_transcript_path(path)
            if real_path is None:
                if prior is None:
                    return None, None
                return prior["model"], dict(prior["usage"])

            # A different transcript = a new Claude session in this tmux session:
            # start its totals fresh. `real_path` is resolved ONCE, here on the
            # binding, so `paths()` costs no syscall on the snapshot path.
            if prior is None or prior.get("path") != path:
                prior = _fresh_state(path, real_path)

            try:
                size = _file_size(path)
            except OSError:
                # File named but not readable yet — keep whatever we had.
                self._state[session] = prior
                return prior["model"], dict(prior["usage"])

            # Truncated / rotated / compacted below our cursor → re-sum from the top.
            if size < prior["offset"]:
                prior = _fresh_state(path, real_path)

            self._read_since(path, prior)
            self._state[session] = prior
            return prior["model"], dict(prior["usage"])

    def tool_events(self, session: str) -> dict[str, dict]:
        """The session's newest tool calls and their outcomes (`fold_tool_events`),
        as of its last read; empty for an unbound session. A copy."""
        with self._lock:
            entry = self._state.get(session)
            return {k: dict(v) for k, v in entry.get("tools", {}).items()} if entry else {}

    def open_agents(self, session: str) -> int:
        """How many background agents AND Workflow runs the session's transcript
        has launched and not yet heard back from, as of its last hook: the
        in-process half of its background work. 0 for an unbound session."""
        return sum(self.open_children(session))

    def open_children(self, session: str) -> tuple[int, int]:
        """`(background agents, Workflow runs)` still open, as `open_agents` counts
        them, kept apart for the landing record (attention.py § 1b)."""
        with self._lock:
            entry = self._state.get(session)
            if not entry:
                return 0, 0
            return len(entry.get("agents", ())), len(entry.get("workflows", ()))

    def turn_trace(self, session: str) -> dict | None:
        """The ended turn's trace as the landing record logs it — `ran_tests`,
        `edited_files` (a count), `opened_pr`, `ended_on_question` — and the two
        facts only a question-v4 state reads, `tests_passed` (bool | None) and
        `made_commit`; or None for an unbound session. Counts and booleans only:
        no path or text leaves."""
        with self._lock:
            entry = self._state.get(session)
            if not entry:
                return None
            return trace_reading(entry.get("trace"), entry["closing"][1])

    def turn_opening(self, session: str) -> dict | None:
        """The transcript entry that opened the session's current turn (`fold_turn_opening`), as of
        its last read; None for an unbound session or a transcript with no prompt yet."""
        with self._lock:
            entry = self._state.get(session)
            return entry.get("opening") if entry else None

    def closing_text(self, session: str) -> str | None:
        """The newest main-thread assistant message's text (its tail), as of the last hook;
        None when there is none — a tool-only last message, or an unbound session."""
        with self._lock:
            entry = self._state.get(session)
            text = entry["closing"][1] if entry else ""
            return text or None

    def stop_failure(self, session: str) -> dict | None:
        """Why the session's last turn was cut off (`fold_stop_failure`), as of its
        last read; None for a finished turn or an unbound session. A copy."""
        with self._lock:
            entry = self._state.get(session)
            reading = entry.get("stop_failure") if entry else None
            return dict(reading) if reading else None

    def refresh(self, session: str) -> bool:
        """Re-tail a bound session's transcript with no hook to prompt it; True when
        its stop failure changed.

        The state arbiter's tick calls it for every turn-over row (main.py), because
        a cut-off turn may arrive with NO hook: an account whose relay predates
        `StopFailure` fires nothing, and even with it nothing says Claude Code
        flushed the error entry before the hook ran. Incremental like `update`, so
        an idle transcript costs one stat.
        """
        with self._lock:
            entry = self._state.get(session)
            if entry is None:
                return False
            before = entry.get("stop_failure")
        self.update(session, entry["path"])
        return self.stop_failure(session) != before

    def clear_stop_failure(self, session: str) -> bool:
        """Forget the reading until a NEW entry sets it; True if there was one.

        For a conversation that ended or restarted in the pane (SessionEnd,
        SessionStart): a `--resume` of a cut-off conversation re-reads the same
        tail, and the user who just resumed it is already acting on it.
        """
        with self._lock:
            entry = self._state.get(session)
            if entry is None or entry.get("stop_failure") is None:
                return False
            entry["stop_failure"] = None
            return True

    def paths(self) -> dict[str, str]:
        """`{session: resolved transcript path}` for every session bound so far.

        THE JOIN TABLE for statusline_models.py, and the reason it needs no
        store of its own: this binding is written on the FIRST hook of a
        session, whether or not that hook found a model — which is exactly the
        session-start case where the transcript has no assistant entry yet.

        Sessions whose path was refused never get an entry, so a caller can
        treat presence here as "this name is bound to a transcript we may read".
        """
        with self._lock:
            return {session: entry["real_path"] for session, entry in self._state.items() if entry.get("real_path")}

    def rename(self, old: str, new: str) -> bool:
        """Move a session's transcript binding onto a new name; True if there was one.

        MANDATORY after `tmux rename-session`, for the same reason
        `SessionModelStore.rename` is: leave the binding under the old name and
        `paths()` stops answering for the renamed session, which drops it back
        to "claude disconnected" — the very bug this join exists to fix,
        re-entered through rename.

        The new name wins if something is already stored under it: the rename
        already happened in tmux, so that entry describes a session that no
        longer exists under that name.
        """
        with self._lock:
            entry = self._state.pop(old, None)
            if entry is None:
                return False
            self._state[new] = entry
            return True

    def forget(self, session: str) -> bool:
        """Drop a session's binding; True if there was one.

        For a NAME being reused by a new spawn: the binding still points at
        the previous occupant's transcript, and the statusline tier joins on
        it — so without this a fresh Codex in `vc-3` would read as whatever
        Claude model `vc-3` last ran.
        """
        with self._lock:
            return self._state.pop(session, None) is not None

    @staticmethod
    def _read_since(path: str, state: dict) -> None:
        """Read complete lines from `state["offset"]` to EOF and fold them into
        `state` (usage, model, background agents and workflows, closing text,
        stop failure, tool calls, the turn's trace and opening prompt).

        Only consumes up to the last newline so a half-written trailing line
        (the file is being appended to live) is left for the next call. An
        unreadable file, or nothing committed since the last read, leaves
        `state` exactly as it was.
        """
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                # Bounded: an unbounded read() here loads the whole file into
                # memory, and the file is named by the request.
                chunk = handle.read(MAX_READ_BYTES)
        except OSError:
            return
        newline = chunk.rfind(b"\n")
        if newline == -1:
            return  # nothing committed since last read
        entries = parse_entries(chunk[: newline + 1].splitlines())
        model, delta = scan_entries(entries)
        for key in USAGE_KEYS:
            state["usage"][key] += delta[key]
        state["model"] = model or state["model"]
        state["agents"] = fold_background_agents(state["agents"], entries)
        state["workflows"] = fold_background_workflows(state.get("workflows", frozenset()), entries)
        state["trace"] = fold_turn_trace(state.get("trace"), entries)
        state["opening"] = fold_turn_opening(state.get("opening"), entries)
        state["closing"] = fold_closing_text(state["closing"], entries)
        state["stop_failure"] = fold_stop_failure(state["stop_failure"], entries)
        state["tools"] = fold_tool_events(state.get("tools", {}), entries)
        state["offset"] += newline + 1


def _fresh_state(path: str, real_path: str) -> dict:
    """A session's reading before any line of `path` has been folded."""
    return {
        "path": path,
        "real_path": real_path,
        "offset": 0,
        "model": None,
        "usage": _zero_usage(),
        "agents": frozenset(),
        "workflows": frozenset(),
        "closing": (None, ""),
        "stop_failure": None,
        "tools": {},
        "trace": _fresh_trace(),
        "opening": None,
    }


def _file_size(path: str) -> int:
    return os.path.getsize(path)
