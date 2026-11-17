"""Antigravity CLI (`agy`): its hooks file, and its hook payloads in the bridge's terms.

`agy` is Google's agent CLI: the successor to Gemini CLI for personal Google
accounts, which Gemini CLI stopped serving on 2026-06-18. It runs as the
`antigravity` profile kind (profiles.py). Everything that differs from
Claude/Codex lives here, so main.py and profiles.py only ask "is this agy?".

THE HOOKS FILE IS A DIFFERENT SHAPE. Claude and Codex nest `{"hooks": {event:
[group]}}`. agy reads `~/.gemini/config/hooks.json`, whose TOP-LEVEL keys are
hook NAMES, each mapping events to handlers. Tool events (`PreToolUse`,
`PostToolUse`) take `[{"matcher", "hooks": [handler]}]`; every other event
takes a flat `[handler]`. The bridge owns ONE name (`HOOK_NAME`) and replaces
it whole on install, so a user's own hooks are never touched. The file is
global: the Antigravity IDE and desktop app read it too. The curl relay is a
no-op outside tmux (`main.hook_command`), so their sessions post nothing.

**`PreToolUse` is never installed.** agy reads a PreToolUse hook's stdout as
the decision, and empty stdout or `{}` DENIES the tool call (exit codes are
ignored). The relay prints nothing, so hooking it would refuse every tool.
"allow" and "ask" are both real decisions too, so no answer is neutral. The
cost: no hook says "waiting on a permission prompt", so such a row reads busy.

Payloads are camelCase and name different fields. `normalize_payload` maps
them onto the snake_case fields /hook already reads (`session_id`,
`transcript_path`, `cwd`, `model`, `tool_name`, `tool_input`), so the rest of
the route is shared.

Sources: antigravity.google/docs/hooks and /docs/settings (2026-11), plus the
agy 1.2.x measurements at symposium.dev/design/agent-details/antigravity-cli.

Stdlib only, pure: tests/test_antigravity_hooks.py covers all of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tldrack.activity import KEEP_ACTIVITY, classify_activity

KIND = "antigravity"
#: The binary `resolve_cli` looks for. agy's installer drops it in ~/.local/bin.
CLI_NAME = "agy"
#: The `?agent=` value the relay adds, so /hook knows to normalize the body.
AGENT_PARAM = KIND
#: The one top-level hook name the bridge owns in agy's hooks file.
HOOK_NAME = "voidcraft-world-bridge"
#: Relative to the profile's config dir (`~/.gemini`).
HOOKS_FILE = "config/hooks.json"
#: agy's per-hook timeout is in SECONDS (default 30). The relay's curl gives up after 2.
HOOK_TIMEOUT_SECONDS = 5

#: agy hook event → the state it implies. `PreInvocation` fires before every
#: model call, so it is the "working" signal; `PostToolUse` names the tool
#: that just ran (the activity zone). `Stop` refines in `hook_state`.
#: `PostInvocation` adds nothing PreInvocation and Stop do not already say.
HOOK_STATES = {
    "SessionStart": "free",
    "PreInvocation": "busy",
    "PostToolUse": "busy",
    "Stop": "free",
}
#: Events that take a matcher group rather than a flat handler list.
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
TOOL_MATCHER = ".*"

#: agy tool name → the Claude tool name `classify_activity` already zones,
#: plus the arg keys that carry its detail. Args are matched case-blind:
#: agy's own args are PascalCase (`CommandLine`, `AbsolutePath`).
TOOL_ALIASES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "run_command": ("Bash", "command", ("commandline", "command", "cmd")),
    "view_file": ("Read", "file_path", ("absolutepath", "file_path", "path")),
    "view_file_outline": ("Read", "file_path", ("absolutepath", "file_path", "path")),
    "write_to_file": ("Write", "file_path", ("targetfile", "absolutepath", "file_path", "path")),
    "replace_file_content": ("Edit", "file_path", ("targetfile", "absolutepath", "file_path", "path")),
    "multi_replace_file_content": ("Edit", "file_path", ("targetfile", "absolutepath", "file_path", "path")),
    "grep_search": ("Grep", "pattern", ("query", "pattern", "searchpath")),
    "find_by_name": ("Glob", "pattern", ("pattern", "searchdirectory")),
    "list_dir": ("Glob", "path", ("directorypath", "path")),
    "search_web": ("WebSearch", "query", ("query",)),
    "read_url_content": ("WebFetch", "url", ("url",)),
}
#: Browser tools have open-ended names (`browser_click`, `browser_navigate`…).
BROWSER_TOOL_RE = re.compile(r"browser_[a-z_]+")

_MODEL_JUNK_RE = re.compile(r"[^a-z0-9._:-]+")


def hooks_block(entry_for: Callable[[str], dict]) -> dict:
    """`{HOOK_NAME: {event: handlers}}` for every `HOOK_STATES` event, in agy's
    shape. `entry_for(event)` builds one relay handler (main.py owns the command)."""
    events: dict[str, list] = {}
    for event in HOOK_STATES:
        handler = entry_for(event)
        events[event] = [{"matcher": TOOL_MATCHER, "hooks": [handler]}] if event in TOOL_EVENTS else [handler]
    return {HOOK_NAME: events}


def _handlers(groups: object) -> list:
    """Every handler under one event, whichever of agy's two shapes it uses."""
    found: list = []
    for item in groups if isinstance(groups, list) else ():
        nested = item.get("hooks") if isinstance(item, dict) else None
        if isinstance(nested, list):
            found.extend(nested)
        else:
            found.append(item)
    return found


def relay_events(document: object, is_relay: Callable[[object], bool]) -> set[str]:
    """Events whose handlers include a relay, under ANY hook name that is not
    switched off (`"enabled": false`). Pure."""
    found: set[str] = set()
    if not isinstance(document, dict):
        return found
    for hook in document.values():
        if not isinstance(hook, dict) or hook.get("enabled") is False:
            continue
        for event, groups in hook.items():
            if event != "enabled" and any(is_relay(h) for h in _handlers(groups)):
                found.add(event)
    return found


def merge(document: dict, block: dict) -> dict:
    """`document` with the bridge's hook name replaced by `block`'s. Pure: a deep
    copy, every other name kept in order."""
    merged = {key: value for key, value in document.items() if key not in block}
    merged.update(block)
    return merged


def model_token(name: object) -> str | None:
    """agy's `modelName` → the token the rack shows (`models.MODEL_TOKEN_RE`).
    It may be a display label ("Gemini 3.8 Flash (High)"), so it is slugged:
    `gemini-3.8-flash-high`. None for anything empty. Pure."""
    if not isinstance(name, str):
        return None
    slug = _MODEL_JUNK_RE.sub("-", name.strip().lower()).strip("-")
    return slug[:128] or None


def _pick(args: dict, keys: tuple[str, ...]) -> str | None:
    lowered = {str(k).lower(): v for k, v in args.items()}
    for key in keys:
        value = lowered.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def tool_in_claude_terms(tool_call: object) -> tuple[str | None, dict]:
    """agy's `toolCall` → `(tool_name, tool_input)` as `classify_activity` reads
    them. An unknown tool keeps its own name (it zones as "computer"). Pure."""
    if not isinstance(tool_call, dict):
        return None, {}
    name = tool_call.get("name")
    args = tool_call.get("args")
    args = args if isinstance(args, dict) else {}
    if not isinstance(name, str) or not name:
        return None, {}
    alias = TOOL_ALIASES.get(name)
    if alias is not None:
        claude_name, field, keys = alias
        value = _pick(args, keys)
        return claude_name, ({field: value} if value else {})
    if BROWSER_TOOL_RE.fullmatch(name):
        url = _pick(args, ("url",))
        return "WebFetch", ({"url": url} if url else {})
    return name, {}


def normalize_payload(payload: dict) -> dict:
    """An agy hook body with the bridge's snake_case fields added beside its own
    camelCase ones. Pure; never mutates `payload`."""
    out = dict(payload)
    conversation = payload.get("conversationId")
    if isinstance(conversation, str) and conversation:
        out["session_id"] = conversation
    transcript = payload.get("transcriptPath")
    if isinstance(transcript, str) and transcript:
        out["transcript_path"] = transcript
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str) and workspaces[0]:
        out["cwd"] = workspaces[0]
    model = model_token(payload.get("modelName"))
    if model is not None:
        out["model"] = model
    tool_name, tool_input = tool_in_claude_terms(payload.get("toolCall"))
    if tool_name is not None:
        out["tool_name"] = tool_name
        out["tool_input"] = tool_input
    return out


def hook_state(event: str, payload: dict) -> str:
    """The state an agy event implies. A `Stop` with `fullyIdle: false` means
    the loop ended while background agents still run: `background`, not free.
    Raises KeyError for an event outside HOOK_STATES (the route checks first)."""
    if event == "Stop" and payload.get("fullyIdle") is False:
        return "background"
    return HOOK_STATES[event]


def activity_update(event: str, payload: dict) -> object:
    """What this event does to the stored activity (activity.py's contract):
    PostToolUse names the tool that just ran, SessionStart and Stop clear,
    PreInvocation keeps. Reads the NORMALIZED payload."""
    if event == "PostToolUse":
        tool_input = payload.get("tool_input")
        return classify_activity(payload.get("tool_name"), tool_input if isinstance(tool_input, dict) else {})
    if event in ("SessionStart", "Stop"):
        return None
    return KEEP_ACTIVITY
