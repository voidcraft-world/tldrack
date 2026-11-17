"""Classify Claude Code hook payloads into architecture-zone activities.

`POST /hook` (main.py) receives each hook's raw stdin JSON — `tool_name` and
`tool_input` for PreToolUse — and this module reduces it to an *activity*:

    {"zone": "aws", "tool": "Bash", "detail": "bash: aws s3 ls"}

The zone drives the agent characters in the rack UI
(ui/src/engine/terminal/agent-zone-logic.ts): the bot for a session glides to the
zone's platform while the tool runs. Zones:

    computer  — local bash execution (anything not classified below)
    codebase  — file reads/edits/searches and git, via tools or bash
    aws       — aws / cdk CLI calls
    web       — WebFetch / WebSearch
    tests     — pytest / playwright / node --test / npm run test:*

("thinking" — busy with no tool yet — is a frontend routing state, not a
bridge zone: it's exactly the absence of an activity while busy.)

Pure functions only (no I/O) — everything here is pytest-covered
(tests/test_activity.py).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

ZONES = ("computer", "codebase", "aws", "web", "tests")

MAX_DETAIL_LEN = 80

# Sentinel for "this event says nothing about activity — keep what's stored".
# Distinct from None, which means "clear the stored activity".
KEEP_ACTIVITY = object()

# Non-Bash tools with a fixed zone. Anything absent falls back to "computer"
# (the agent is doing *something* locally we can't name better).
TOOL_ZONES = {
    "Read": "codebase",
    "Edit": "codebase",
    "MultiEdit": "codebase",
    "Write": "codebase",
    "NotebookEdit": "codebase",
    "Grep": "codebase",
    "Glob": "codebase",
    "WebFetch": "web",
    "WebSearch": "web",
}

# Bash classification: the first meaningful word of any command segment.
# Priority: aws > tests > codebase — `git pull && cdk deploy` is an AWS story,
# `pytest | grep FAILED` is a test run.
# "aws-cdk" covers `npx -y aws-cdk@latest deploy`.
AWS_COMMANDS = {"aws", "cdk", "aws-cdk"}
TEST_COMMANDS = {"pytest", "playwright", "vitest", "jest"}
CODEBASE_COMMANDS = {"git", "grep", "rg", "ag", "find", "fd", "ls", "cat", "sed", "awk", "head", "tail", "diff", "wc"}
# npm/yarn/pnpm/bun aren't wrappers (`npm run build` is just local compute),
# but a test-ish subcommand makes the segment a test run.
PACKAGE_RUNNERS = {"npm", "yarn", "pnpm", "bun"}
TEST_MARKER_RE = re.compile(r"\btest")

# Tokens that wrap the real command rather than being it. "run" covers
# `uv run pytest` / `uv run aws` (uv is skipped, then run, then the command).
COMMAND_WRAPPERS = {"sudo", "command", "exec", "time", "nohup", "env", "npx", "uv", "uvx", "xargs", "run"}

ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Segment separators: pipes, chains, subshell boundaries, newlines. Splitting
# naively is fine — a separator inside a quoted string just yields a segment
# whose first word isn't a known command, which classifies as "computer".
SEGMENT_SPLIT_RE = re.compile(r"[|;&\n]+")


def first_command_word(segment: str) -> str | None:
    """The command a shell segment actually runs: skips env-var assignments,
    flags, and wrappers (sudo/npx/…); strips any path prefix (`/usr/bin/git` →
    `git`) and any npm version suffix (`aws-cdk@latest` → `aws-cdk`)."""
    for token in segment.split():
        if ENV_ASSIGNMENT_RE.match(token) or token.startswith("-"):
            continue
        word = token.rsplit("/", 1)[-1].split("@", 1)[0]
        if word in COMMAND_WRAPPERS:
            continue
        return word
    return None


def _is_test_segment(word: str | None, segment: str) -> bool:
    if word in TEST_COMMANDS:
        return True
    if word in PACKAGE_RUNNERS and TEST_MARKER_RE.search(segment):
        return True  # npm run test:e2e, yarn test, …
    return word == "node" and "--test" in segment


def classify_bash_zone(command: str) -> str:
    """Zone for a Bash tool call, from the command words of every segment."""
    words: set[str] = set()
    has_tests = False
    for segment in SEGMENT_SPLIT_RE.split(command):
        word = first_command_word(segment)
        has_tests = has_tests or _is_test_segment(word, segment)
        if word is not None:
            words.add(word)
    if words & AWS_COMMANDS:
        return "aws"
    if has_tests:
        return "tests"
    if words & CODEBASE_COMMANDS:
        return "codebase"
    return "computer"


def squash(text: str) -> str:
    return " ".join(text.split())


def clip(text: str) -> str:
    return text if len(text) <= MAX_DETAIL_LEN else text[: MAX_DETAIL_LEN - 1] + "…"


def _detail(tool_name: str, tool_input: dict) -> str:
    """Short human string for tooltips/logs — never load-bearing, best-effort."""
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        return clip(f"bash: {squash(command)}".strip().rstrip(":"))
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if isinstance(file_path, str) and file_path:
        return clip(f"{tool_name.lower()}: {file_path.rsplit('/', 1)[-1]}")
    pattern = tool_input.get("pattern") or tool_input.get("query")
    if isinstance(pattern, str) and pattern:
        return clip(f"{tool_name.lower()}: {squash(pattern)}")
    url = tool_input.get("url")
    if isinstance(url, str) and url:
        try:
            host = urlsplit(url).hostname or url
        except ValueError:
            host = url
        return clip(f"{tool_name.lower()}: {host}")
    return tool_name.lower()


def classify_activity(tool_name: str | None, tool_input: dict) -> dict | None:
    """PreToolUse payload → activity dict, or None when there's no tool to name."""
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if tool_name == "Bash":
        zone = classify_bash_zone(str(tool_input.get("command") or ""))
    else:
        zone = TOOL_ZONES.get(tool_name, "computer")
    return {"zone": zone, "tool": tool_name, "detail": _detail(tool_name, tool_input)}


def activity_update_for_event(event: str, payload: dict) -> dict | None | object:
    """What POST /hook should do to the stored activity for this event:
    a dict (set it), None (clear it), or KEEP_ACTIVITY (leave it unchanged).

    PreToolUse sets (and the activity persists while the tool runs — there is
    no PostToolUse hook, the next event overwrites). UserPromptSubmit clears
    (busy with no tool yet = "thinking at the desk"). Stop/SessionStart/
    SessionEnd clear. Notification keeps — attention outranks zone anyway.
    Codex's `Interrupt` (Esc mid-turn) and Claude's `StopFailure` (an API error
    ended the turn) clear like Stop: the turn is over.
    """
    if event == "PreToolUse":
        tool_input = payload.get("tool_input")
        return classify_activity(payload.get("tool_name"), tool_input if isinstance(tool_input, dict) else {})
    if event in ("UserPromptSubmit", "Stop", "StopFailure", "SessionStart", "SessionEnd", "Interrupt"):
        return None
    return KEEP_ACTIVITY
