"""One Codex rollout → a `Fold`. Pure. Why a ledger at all: `ledger/__init__.py`.

── THREE GENERATIONS OF THE SAME FACTS ─────────────────────────────────────────
Codex has written the same conversation three ways since 2025, and a rollout often holds two of
them at once (seen on 0.16x: most files carry both `item_completed` items and
`response_item` messages for the same prompt). Counting both would double every prompt, so each
fact is read from the NEWEST representation the file has, and only from that one (`_prefer`):

| fact            | newest                         | then                    | oldest                         |
|-----------------|--------------------------------|-------------------------|--------------------------------|
| human prompt    | `item_completed` UserMessage   | `event_msg` user_message| `response_item` user message   |
| agent text      | `item_completed` AgentMessage  | `event_msg` agent_message| `response_item` assistant msg |
| tool call       | `item_completed` Command/File/Mcp… |                     | `response_item` function_call  |

A `response_item` user message is also where Codex injects AGENTS.md and the environment, so the
oldest generation drops texts with an injected prefix (`INJECTED_PREFIXES`).

── THE MACHINE APPROVER ────────────────────────────────────────────────────────
With `approvals_reviewer = auto_review`, Codex spawns a GUARDIAN thread per risky action
(`session_meta.thread_source == "guardian_review"`, its own rollout, `parent_thread_id` set). Its
answers are JSON verdicts `{risk_level, user_authorization, outcome, rationale}`: a machine making
exactly the call a human would otherwise be pinged for. Each becomes an `auto_review` event, so the
ledger can say how often the machine said allow, and on what.

Codex logs no human approval decision of its own; the bridge's permission ledger (a `bridge_rows`
source) is where a Codex approval prompt and its outcome are recorded.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from tldrack.ledger.claude_fold import TMUX_TYPING, TYPING_SUMMARY_MAX, iso_epoch
from tldrack.ledger.fold import Fold, clip, dumps
from tldrack.permission_ledger import redact

INJECTED_PREFIXES = (
    "# AGENTS.md",
    "<environment_context>",
    "<user_instructions>",
    "<permissions",
    "<INSTRUCTIONS>",
    "<turn_aborted>",
    "<subagent_notification>",
)
TEXT_ITEMS = {"UserMessage", "AgentMessage", "Reasoning", "ContextCompaction", "Plan"}
RESPONSE_TOOL_TYPES = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
GUARDIAN_SOURCES = {"guardian_review"}
SUMMARY_MAX = 160


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _normalize(entry: dict) -> tuple[str, dict]:
    """(`type`, payload) for every generation; the oldest wrote items bare, with no wrapper."""
    payload = entry.get("payload")
    if isinstance(payload, dict) and isinstance(entry.get("type"), str):
        return entry["type"], payload
    if entry.get("type") in ("message", *RESPONSE_TOOL_TYPES, "reasoning", "function_call_output"):
        return "response_item", entry
    if "instructions" in entry and "id" in entry:
        return "legacy_meta", entry
    return str(entry.get("type")), {}


def _session_kind(meta: dict) -> tuple[str, str | None]:
    source = meta.get("source")
    parent = meta.get("parent_thread_id") if isinstance(meta.get("parent_thread_id"), str) else None
    if meta.get("thread_source") in GUARDIAN_SOURCES:
        return "auto_review", parent
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        sub = source["subagent"]
        if sub.get("other") == "guardian":
            return "auto_review", parent
        spawn = sub.get("thread_spawn")
        if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
            parent = spawn["parent_thread_id"]
        return "subagent", parent
    return "main", parent


def _tool_row(item: dict) -> tuple[str, str]:
    """(tool, redacted summary) for one `item_completed` tool item."""
    kind = item.get("type")
    if kind == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = command[-1] if command else ""
        command = " ".join(str(command).split())
        limit = TYPING_SUMMARY_MAX if any(word in command for word in TMUX_TYPING) else SUMMARY_MAX
        return "shell", clip(redact(command), limit)
    if kind == "FileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        return "apply_patch", clip(", ".join(sorted(changes)), SUMMARY_MAX)
    if kind == "McpToolCall":
        return f"mcp:{item.get('server')}.{item.get('tool')}", str(item.get("tool") or "")
    if kind == "Extension":
        return f"ext:{item.get('kind')}", clip(redact(str(item.get("query") or "")), SUMMARY_MAX)
    if kind == "ImageView":
        return "view_image", clip(str(item.get("path") or ""), SUMMARY_MAX)
    return str(kind or "?"), ""


def _response_tool_row(payload: dict) -> tuple[str, str]:
    name = payload.get("name") or payload.get("type") or "?"
    args = payload.get("arguments") if isinstance(payload.get("arguments"), str) else payload.get("input")
    summary = ""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = None
        command = parsed.get("command") or parsed.get("cmd") if isinstance(parsed, dict) else None
        summary = " ".join(command) if isinstance(command, list) else str(command or args)
    elif isinstance(payload.get("action"), dict):
        summary = " ".join(str(c) for c in payload["action"].get("command") or ())
    return str(name), clip(redact(" ".join(summary.split())), SUMMARY_MAX)


def _prefer(*candidates: list) -> list:
    return next((c for c in candidates if c), [])


def fold_codex(lines: Iterable[bytes]) -> Fold:
    entries: list[tuple[str, dict, float | None]] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        etype, payload = _normalize(entry)
        entries.append((etype, payload, iso_epoch(entry.get("timestamp"))))

    fold = Fold()
    s = fold.session
    s.update({"source": "codex", "kind": "main", "agent_id": None, "parent_session_id": None})

    # Pass 1: what the file holds, so each fact is read from one generation only.
    ic_users, em_users, ri_users = [], [], []
    ic_agents, em_agents, ri_agents = [], [], []
    ic_tools, ri_tools = [], []
    for i, (etype, p, _ts) in enumerate(entries):
        ptype = p.get("type")
        if etype == "event_msg" and ptype == "item_completed" and isinstance(p.get("item"), dict):
            item = p["item"]
            if item.get("type") == "UserMessage":
                ic_users.append(i)
            elif item.get("type") == "AgentMessage":
                ic_agents.append(i)
            elif item.get("type") not in TEXT_ITEMS:
                ic_tools.append(i)
        elif etype == "event_msg" and ptype == "user_message":
            em_users.append(i)
        elif etype == "event_msg" and ptype == "agent_message":
            em_agents.append(i)
        elif etype == "response_item" and ptype == "message":
            text = _text_of(p.get("content")).lstrip()
            if p.get("role") == "user" and text and not text.startswith(INJECTED_PREFIXES):
                ri_users.append(i)
            elif p.get("role") == "assistant":
                ri_agents.append(i)
        elif etype == "response_item" and ptype in RESPONSE_TOOL_TYPES:
            ri_tools.append(i)
    users = set(_prefer(ic_users, em_users, ri_users))
    agents = set(_prefer(ic_agents, em_agents, ri_agents))
    tools = set(_prefer(ic_tools, ri_tools))

    totals: dict[str, int] = {}
    compactions = 0
    for i, (etype, p, ts) in enumerate(entries):
        if ts is not None and s.get("started_at") is None:
            s["started_at"] = ts
        ptype = p.get("type")
        if etype in ("session_meta", "legacy_meta"):
            if not s.get("session_id"):
                s["session_id"] = p.get("id")
                s["cwd"] = p.get("cwd") or s.get("cwd")
                s["cli_version"] = p.get("cli_version")
                kind, parent = _session_kind(p)
                s["kind"], s["parent_session_id"] = kind, parent
                if ts is None and iso_epoch(p.get("timestamp")) is not None:
                    s["started_at"] = s.get("started_at") or iso_epoch(p.get("timestamp"))
                git = p.get("git") if isinstance(p.get("git"), dict) else {}
                if isinstance(git.get("branch"), str):
                    s["git_branch"] = git["branch"]
            continue
        if etype == "turn_context":
            if isinstance(p.get("model"), str):
                s["model"] = p["model"]
            if isinstance(p.get("cwd"), str) and not s.get("cwd"):
                s["cwd"] = p["cwd"]
            continue
        if etype == "compacted":
            compactions += 1
            continue
        if etype == "event_msg" and ptype == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
            if usage:
                totals = {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cached_input_tokens") or 0),
                }
            continue
        if etype == "event_msg" and ptype == "turn_aborted":
            if p.get("reason") == "interrupted" and s["kind"] == "main":
                fold.human_event("interrupt", ts, text="turn aborted: interrupted", scope="turn")
                if fold.turn is not None:
                    fold.turn.interrupted = True
            continue
        if i in users:
            text = _text_of(
                (p.get("item") or {}).get("content")
                if ptype == "item_completed"
                else p.get("message")
                if ptype == "user_message"
                else p.get("content")
            ).strip()
            if not text:
                continue
            origin = "human" if s["kind"] == "main" else "parent" if s["kind"] == "subagent" else "system"
            event = fold.human_event("prompt", ts, text=clip(text)) if origin == "human" else None
            fold.prompt_turn(origin, ts, {"human": "human", "parent": "parent"}.get(origin, "machine"), text, event)
            continue
        if i in agents:
            text = _text_of(
                (p.get("item") or {}).get("content")
                if ptype == "item_completed"
                else p.get("message")
                if ptype == "agent_message"
                else p.get("content")
            )
            fold.agent_text(ts, text)
            if s["kind"] == "auto_review":
                _guardian_verdict(fold, ts, text)
            continue
        if i in tools:
            tool, summary = _tool_row(p["item"]) if ptype == "item_completed" else _response_tool_row(p)
            status = (p.get("item") or {}).get("status") if ptype == "item_completed" else p.get("status")
            fold.tool_calls.append(
                {
                    "seq": len(fold.tool_calls),
                    "ts": ts,
                    "turn_idx": fold.turn.idx if fold.turn else None,
                    "tool": tool,
                    "pattern": tool,
                    "summary": summary,
                    "decision": None,
                    "decision_source": None,
                    "reason_type": None,
                    "denial_kind": None,
                    "is_error": int(status in ("failed", "declined")),
                }
            )
            if fold.turn is not None:
                fold.turn.tool_calls += 1
            fold.agent_activity(ts)
            continue
        if etype == "event_msg" and ptype == "task_complete":
            fold.agent_activity(ts)

    s.update(totals)
    s["compactions"] = compactions
    fold.finish()
    return fold


def _guardian_verdict(fold: Fold, ts: float | None, text: str) -> None:
    try:
        verdict = json.loads(text)
    except ValueError:
        return
    if not isinstance(verdict, dict) or "outcome" not in verdict:
        return
    fold.machine_event(
        "auto_review",
        ts,
        decision=str(verdict.get("outcome")),
        text=clip(verdict.get("rationale"), 2_000),
        scope=str(verdict.get("risk_level")) if verdict.get("risk_level") else None,
        data=dumps({k: verdict.get(k) for k in ("risk_level", "user_authorization", "outcome")}),
    )
