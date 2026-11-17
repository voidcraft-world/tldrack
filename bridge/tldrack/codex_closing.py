"""Read a Codex turn's final reply for the local open-work judge.

Claude's transcript tracker must not parse Codex rollouts. This reader accepts only
the named conversation inside a configured Codex profile's sessions directory,
and never treats commentary, analysis or a previous turn as the closing reply.
"""

from __future__ import annotations

from tldrack.codex_rollout import read_codex_records
from tldrack.transcript import CLOSING_TEXT_MAX_CHARS

TAIL_BYTES = 512 * 1024


def read_codex_closing(path: object, config_dir: str, session_id: object) -> str | None:
    """Bounded read; config_dir comes from bridge configuration, never the hook."""
    closing = None
    for entry in read_codex_records(path, config_dir, session_id, TAIL_BYTES):
        closing = fold_codex_closing(closing, entry)
    return closing


def fold_codex_closing(closing: str | None, entry: dict) -> str | None:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return closing
    kind = payload.get("type")
    if entry.get("type") == "event_msg":
        if kind in ("task_started", "user_message", "turn_aborted"):
            return None
        if kind == "task_complete":
            text = payload.get("last_agent_message")
            return text[-CLOSING_TEXT_MAX_CHARS:] if isinstance(text, str) and text.strip() else closing
    if entry.get("type") == "response_item":
        if kind in ("function_call", "custom_tool_call") or (kind == "message" and payload.get("role") == "user"):
            return None
        if kind == "message" and payload.get("role") == "assistant":
            if payload.get("phase") != "final_answer":
                return None
            content = payload.get("content")
            text = "\n".join(
                block["text"]
                for block in (content if isinstance(content, list) else [])
                if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str)
            )
            return text[-CLOSING_TEXT_MAX_CHARS:] if text.strip() else None
    return closing
