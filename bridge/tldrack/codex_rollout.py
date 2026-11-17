"""Bounded, profile-fenced reads of Codex's best-effort rollout metadata.

The rollout format can change. Missing or unfamiliar records yield no evidence;
they must never silently suppress a human approval prompt.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

APPROVAL_TAIL_BYTES = 8 * 1024 * 1024


def read_codex_records(
    path: object, config_dir: str, session_id: object, max_bytes: int, *, reverse: bool = False
) -> Iterator[dict]:
    """config_dir and the byte cap come from the bridge, never from the hook."""
    if not isinstance(path, str) or not isinstance(session_id, str):
        return
    try:
        UUID(session_id)
        source = Path(path).expanduser().resolve()
        root = Path(config_dir).expanduser().resolve() / "sessions"
        source.relative_to(root)
        if not source.name.startswith("rollout-") or not source.name.endswith(f"-{session_id}.jsonl"):
            return
        if not source.is_file():
            return
        with source.open("rb") as handle:
            start = max(0, handle.seek(0, 2) - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
    except (OSError, ValueError, RuntimeError):
        return
    lines = data.splitlines(keepends=True)
    if start:
        lines = lines[1:]
    for line in reversed(lines) if reverse else lines:
        if not line.endswith(b"\n"):
            continue  # The writer has not committed this record yet.
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            yield entry


def codex_uses_auto_review(payload: dict, config_dir: str) -> bool:
    """Only a matching current turn can identify non-human local-tool review.

    App/MCP tools can override the turn's reviewer; leave those as attention.
    Config files are insufficient: a running session can override their values.
    """
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id or payload.get("tool_name") not in ("Bash", "apply_patch"):
        return False
    for entry in read_codex_records(
        payload.get("transcript_path"), config_dir, payload.get("session_id"), APPROVAL_TAIL_BYTES, reverse=True
    ):
        if entry.get("type") == "turn_context":
            context = entry.get("payload")
            return (
                isinstance(context, dict)
                and context.get("turn_id") == turn_id
                and context.get("approvals_reviewer") == "auto_review"
            )
    return False
