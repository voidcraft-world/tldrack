"""One Claude Code transcript → a `Fold` (sessions, turns, messages, events, tool calls). Pure.

── WHERE THE HUMAN DECISIONS ARE ───────────────────────────────────────────────
Measured on Claude Code 2.1.31x transcripts; every field is read defensively, an older or newer
shape degrades to fewer rows, never to an exception:

- A typed prompt is a `user` entry with text content. `turnOrigin` (newer builds) says who wrote
  it: `human`, `task_notification`, `peer`, `sdk`; without it the text's own prefix decides
  (`origin_of`). `isMeta` entries are system-injected and skipped.
- `[Request interrupted by user]` (… `for tool use`) is an interrupt.
- `<command-name>/x</command-name>` is a slash command the operator typed; its
  `<local-command-stdout>` echo is not a prompt.
- Every `tool_result` entry carries Claude's own `permissionDecision`
  `{decision, source, reasonType, grantScope}`. `source: user_*` is a HUMAN pick
  (`user_temporary` once/session, `user_permanent` a new rule, `user_reject`); `config` is the
  machine — a rule, the mode, or the auto-mode classifier. `toolDenialKind` says why a call was
  refused (`user-rejected`, `automode-blocked`, `permission-rule`) and `userFeedback`, or the text
  after "the user said:", is what the operator typed with a rejection.
- `AskUserQuestion`'s result carries `toolUseResult.{questions, answers, annotations}`;
  `ExitPlanMode` is approved unless its result is the rejection text.

── WHAT IS SKIPPED UNPARSED ────────────────────────────────────────────────────
`api-request` / `api-request-blob` lines are the full request bodies Claude now logs — most of a
transcript's bytes and none of its decisions. `SKIP_HEADS` drops them by their first bytes, before
JSON decoding, which is what makes re-folding a whole 200 MB transcript on every change affordable.
The raw archive still keeps them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from tldrack.ledger.fold import Fold, clip, dumps
from tldrack.permission_ledger import FEEDBACK_MARKER, REJECT_MARKER, redact, summarize, tool_pattern

SKIP_HEADS = (b'"type":"api-request', b'"type":"file-history', b'"type":"queue-operation"', b'"type":"attachment"')
HEAD_BYTES = 96

_COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_COMMAND_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
_LOCAL_OUTPUT = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>")
INTERRUPT_PREFIX = "[Request interrupted by user"
#: The rack's own typed lines (pane_typist.py `RACK_TAGS`) count as an agent's message too.
PEER_TAGS = ("<teammate-message", "<peer-message", "<agent-message", "<rack-wake", "<lead-message")

#: `turnOrigin` → the ledger's origin. Unknown values pass through, so a new kind is visible.
ORIGINS = {"human": "human", "task_notification": "task", "peer": "peer", "sdk": "sdk"}
#: Message role for a turn origin.
ROLE_FOR_ORIGIN = {
    "human": "human",
    "command": "human",
    "task": "machine",
    "system": "machine",
    "sdk": "machine",
    "peer": "peer",
    "parent": "parent",
}

TMUX_TYPING = ("send-keys", "paste-buffer")
TYPING_SUMMARY_MAX = 1_000

QUESTION_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"
PLAN_MAX = 8_000


def iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def block_text(content: object) -> str:
    """A message's or tool result's text: a plain string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def session_shape(path: str, meta: dict | None) -> dict[str, Any]:
    """What the file's PLACE says: main thread, subagent or workflow agent, and whose child."""
    parts = path.replace("\\", "/").split("/")
    shape: dict[str, Any] = {"source": "claude", "kind": "main", "agent_id": None, "parent_session_id": None, "agent_name": None}
    if "subagents" in parts:
        i = parts.index("subagents")
        shape["parent_session_id"] = parts[i - 1] if i > 0 else None
        stem = parts[-1].rsplit(".jsonl", 1)[0]
        shape["agent_id"] = stem.removeprefix("agent-")
        shape["kind"] = "workflow" if "workflows" in parts[i + 1 : -1] else "subagent"
    if isinstance(meta, dict):
        name = meta.get("name") or meta.get("description")
        shape["agent_name"] = clip(name, 200) if isinstance(name, str) else None
        if meta.get("agentType") == "workflow-subagent":
            shape["kind"] = "workflow"
    return shape


def origin_of(entry: dict, text: str, kind: str) -> str:
    if kind in ("subagent", "workflow"):
        # A subagent's prompts come from the agent that spawned it, never from a keyboard.
        return "parent"
    turn_origin = entry.get("turnOrigin")
    if isinstance(turn_origin, str) and turn_origin:
        return ORIGINS.get(turn_origin, turn_origin)
    stripped = text.lstrip()
    if stripped.startswith("<task-notification>"):
        return "task"
    # SendMessage between sessions arrives as `Another Claude session sent a message: <teammate-message …>`.
    if any(tag in stripped[:300] for tag in PEER_TAGS):
        return "peer"
    if entry.get("promptSource") == "system":
        return "system"
    if str(entry.get("entrypoint") or "").startswith("sdk"):
        return "sdk"
    return "human"


def feedback_of(entry: dict, text: str) -> str | None:
    feedback = entry.get("userFeedback")
    if isinstance(feedback, str) and feedback.strip():
        return clip(feedback.strip())
    at = text.find(FEEDBACK_MARKER)
    if at >= 0:
        rest = text[at + len(FEEDBACK_MARKER) :].strip()
        return clip(rest) if rest else None
    return None


def _question_rows(questions: object) -> list[dict]:
    rows = []
    for q in questions if isinstance(questions, list) else ():
        if not isinstance(q, dict):
            continue
        options = [o.get("label") for o in q.get("options") or () if isinstance(o, dict)]
        rows.append(
            {
                "question": clip(q.get("question"), 2_000),
                "header": q.get("header"),
                "options": [o for o in options if isinstance(o, str)],
                "multi": bool(q.get("multiSelect")),
            }
        )
    return rows


def _answer_line(answers: dict) -> str:
    return "\n".join(f"{clip(q, 300)} → {clip(a, 1_000)}" for q, a in answers.items())


def fold_claude(lines: Iterable[bytes], *, path: str, meta: dict | None = None) -> Fold:
    fold = Fold()
    s = fold.session
    s.update(session_shape(path, meta))
    kind = s["kind"]
    #: tool_use_id → (call row, tool name, tool input)
    pending: dict[str, tuple[dict, str, Any]] = {}
    seen_usage: set[str] = set()
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    compactions = 0

    for raw in lines:
        if not raw.strip():
            continue
        head = raw[:HEAD_BYTES]
        if any(marker in head for marker in SKIP_HEADS):
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        ts = iso_epoch(entry.get("timestamp"))
        if ts is not None and s.get("started_at") is None:
            s["started_at"] = ts

        if etype == "ai-title" and isinstance(entry.get("aiTitle"), str):
            s["title"] = clip(entry["aiTitle"], 300)
            continue
        if etype == "summary" and isinstance(entry.get("summary"), str):
            s.setdefault("title", clip(entry["summary"], 300))
            continue
        if etype == "agent-name" and isinstance(entry.get("agentName"), str):
            s["agent_name"] = clip(entry["agentName"], 200)
            continue
        if etype == "pr-link" and isinstance(entry.get("prUrl"), str):
            s["pr_url"] = entry["prUrl"]
            continue
        if etype == "cost-state" and isinstance(entry.get("totalCostUSD"), (int, float)):
            s["cost_usd"] = float(entry["totalCostUSD"])
            continue
        if etype == "system":
            subtype = entry.get("subtype")
            if subtype == "compact_boundary":
                compactions += 1
            elif subtype == "away_summary" and isinstance(entry.get("content"), str):
                fold.add_message(ts, "machine", entry["content"])
            elif subtype == "turn_duration":
                fold.agent_activity(ts)
            continue
        if etype not in ("user", "assistant"):
            continue
        if kind == "main" and entry.get("isSidechain"):
            continue  # an old build's inline subagent entry: its own file is the subagent's record
        for key, field in (("sessionId", "session_id"), ("cwd", "cwd"), ("gitBranch", "git_branch")):
            value = entry.get(key)
            if isinstance(value, str) and value and not s.get(field):
                s[field] = value
        if isinstance(entry.get("version"), str):
            s["cli_version"] = entry["version"]
        entrypoint = entry.get("entrypoint")
        if kind == "main" and isinstance(entrypoint, str) and entrypoint.startswith("sdk"):
            s["kind"] = kind = "headless"
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content")

        if etype == "assistant":
            model = message.get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                s["model"] = model
            usage = message.get("usage")
            mid = message.get("id")
            if isinstance(usage, dict) and (not isinstance(mid, str) or mid not in seen_usage):
                if isinstance(mid, str):
                    seen_usage.add(mid)
                totals["input_tokens"] += int(usage.get("input_tokens") or 0)
                totals["output_tokens"] += int(usage.get("output_tokens") or 0)
                totals["cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
                totals["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    fold.agent_text(ts, block.get("text") or "")
                elif block.get("type") == "tool_use":
                    name = block.get("name") if isinstance(block.get("name"), str) else "?"
                    tool_input = block.get("input")
                    call = {
                        "seq": len(fold.tool_calls),
                        "ts": ts,
                        "turn_idx": fold.turn.idx if fold.turn else None,
                        "tool": name,
                        "pattern": tool_pattern(name, tool_input),
                        "summary": _call_summary(name, tool_input),
                        "decision": None,
                        "decision_source": None,
                        "reason_type": None,
                        "denial_kind": None,
                        "is_error": 0,
                    }
                    fold.tool_calls.append(call)
                    if fold.turn is not None:
                        fold.turn.tool_calls += 1
                    if isinstance(block.get("id"), str):
                        pending[block["id"]] = (call, name, tool_input)
            if isinstance(content, str):
                fold.agent_text(ts, content)
            fold.agent_activity(ts)
            continue

        # ── user entries ────────────────────────────────────────────────────
        if entry.get("isMeta") or entry.get("isVisibleInTranscriptOnly"):
            continue
        if entry.get("isCompactSummary"):
            fold.add_message(ts, "machine", block_text(content))
            continue
        results = (
            [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
        )
        if results:
            for block in results:
                _tool_result(fold, entry, block, ts, pending)
            fold.agent_activity(ts)
            continue
        text = block_text(content).strip()
        if not text:
            continue
        if text.startswith(INTERRUPT_PREFIX):
            fold.human_event("interrupt", ts, text=text, scope="tool" if "for tool use" in text else "turn")
            if fold.turn is not None:
                fold.turn.interrupted = True
            continue
        if text.startswith(_LOCAL_OUTPUT):
            continue
        command = _COMMAND_NAME.search(text[:500])
        if command and kind == "main":
            args = _COMMAND_ARGS.search(text)
            line = f"{command.group(1)} {args.group(1) if args else ''}".strip()
            event = fold.human_event("command", ts, text=clip(line), tool=command.group(1))
            fold.prompt_turn("command", ts, "human", line, event)
            continue
        origin = origin_of(entry, text, kind)
        event = fold.human_event("prompt", ts, text=clip(text)) if origin == "human" else None
        fold.prompt_turn(origin, ts, ROLE_FOR_ORIGIN.get(origin, "machine"), text, event)

    s.update(totals)
    s["compactions"] = compactions
    fold.finish()
    return fold


def _call_summary(name: str, tool_input: Any) -> str:
    if name == QUESTION_TOOL and isinstance(tool_input, dict):
        rows = _question_rows(tool_input.get("questions"))
        if rows:
            return clip(rows[0]["question"], 160)
    if name == PLAN_TOOL:
        return "plan"
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if name == "Bash" and isinstance(command, str) and any(word in command for word in TMUX_TYPING):
        # Long enough to hold the typed text, so `relay.py` can match it to the prompt it became.
        return clip(redact(" ".join(command.split())), TYPING_SUMMARY_MAX)
    return summarize(name, tool_input)


def _tool_result(fold: Fold, entry: dict, block: dict, ts: float | None, pending: dict[str, tuple[dict, str, Any]]) -> None:
    tool_use_id = block.get("tool_use_id")
    call, name, tool_input = pending.pop(tool_use_id, (None, None, None)) if isinstance(tool_use_id, str) else (None, None, None)
    text = block_text(block.get("content"))
    is_error = bool(block.get("is_error"))
    decision = entry.get("permissionDecision") if isinstance(entry.get("permissionDecision"), dict) else {}
    denial = entry.get("toolDenialKind") if isinstance(entry.get("toolDenialKind"), str) else None
    source = decision.get("source") if isinstance(decision.get("source"), str) else None
    rejected = is_error and REJECT_MARKER in text
    if call is not None:
        call.update(
            decision=decision.get("decision"),
            decision_source=source,
            reason_type=decision.get("reasonType"),
            denial_kind=denial,
            is_error=int(is_error),
        )
    if name is None:
        return
    waited = round(max(0.0, ts - call["ts"]), 3) if ts is not None and call and call["ts"] is not None else None
    feedback = feedback_of(entry, text) if rejected else None
    summary = call["summary"] if call else None
    by_machine = denial in ("automode-blocked", "permission-rule") or (
        decision.get("decision") == "reject" and source == "config"
    )
    if by_machine:
        fold.machine_event(
            "auto_block",
            ts,
            decision="reject",
            tool=name,
            summary=summary,
            data=dumps({"denial": denial, "reason_type": decision.get("reasonType"), "reason_code": decision.get("reasonCode")}),
        )
        return
    if name == QUESTION_TOOL:
        result = entry.get("toolUseResult") if isinstance(entry.get("toolUseResult"), dict) else {}
        asked = tool_input.get("questions") if isinstance(tool_input, dict) else None
        questions = _question_rows(result.get("questions") or asked)
        answers = result.get("answers") if isinstance(result.get("answers"), dict) else {}
        if rejected or not answers:
            fold.human_event(
                "question",
                ts,
                decision="reject",
                tool=name,
                summary=summary,
                text=feedback,
                latency_s=waited,
                data=dumps({"questions": questions}),
            )
        else:
            fold.human_event(
                "question",
                ts,
                decision="answer",
                tool=name,
                summary=summary,
                text=clip(_answer_line(answers)),
                latency_s=waited,
                data=dumps({"questions": questions, "answers": answers, "annotations": result.get("annotations")}),
            )
        return
    if name == PLAN_TOOL:
        plan = (tool_input or {}).get("plan") if isinstance(tool_input, dict) else None
        fold.human_event(
            "plan",
            ts,
            decision="reject" if rejected else "accept",
            tool=name,
            summary=summary,
            text=feedback,
            latency_s=waited,
            data=dumps({"plan": clip(plan, PLAN_MAX)}) if plan else None,
        )
        return
    human_pick = (source or "").startswith("user") or (rejected and denial in (None, "user-rejected"))
    if human_pick:
        fold.human_event(
            "permission",
            ts,
            decision="reject" if rejected else "accept",
            tool=name,
            summary=summary,
            text=feedback,
            latency_s=waited,
            scope=decision.get("grantScope") or source,
        )
