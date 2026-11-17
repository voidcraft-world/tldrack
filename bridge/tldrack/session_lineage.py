"""Spawn ancestry lives on tmux sessions, not their mutable names.

The UUID stamp survives bridge restarts and renames. Restore saves/replays the
same stamp; a fresh spawn gets a fresh UUID even when it reuses a name. A child
keeps its parent's last label so a closed parent remains intelligible.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import uuid

LINEAGE_OPTION = "@vc_lineage"
_lock = threading.Lock()
_name = re.compile(r"[A-Za-z0-9_-]{1,200}")


def clean_lineage(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None

    def valid_id(value):
        return isinstance(value, str) and bool(
            re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        )

    if not valid_id(raw.get("id")):
        return None
    out = {"id": raw["id"]}
    parent = raw.get("parent_id")
    if valid_id(parent) and parent != raw["id"]:
        out["parent_id"] = parent
        label = raw.get("parent_name")
        if isinstance(label, str) and _name.fullmatch(label):
            out["parent_name"] = label
    return out


def parse_lineage(text: str) -> dict | None:
    try:
        return clean_lineage(json.loads(text))
    except (ValueError, TypeError):
        return None


def lineage_args(session: str, lineage: dict) -> list[str]:
    return [";", "set-option", "-t", f"={session}:", LINEAGE_OPTION, json.dumps(lineage, separators=(",", ":"))]


def prepare_lineage(parent_session: object = None) -> dict:
    """Resolve an explicit caller while it is alive; never use the bridge's env.

    Legacy parents get stamped once under a lock, before any children launch.
    A missing/invalid parent refuses the spawn instead of silently losing it.
    """
    child = {"id": str(uuid.uuid4())}
    if parent_session is None:
        return child
    if not isinstance(parent_session, str) or not _name.fullmatch(parent_session):
        raise ValueError("parent_session must be a valid session name")
    with _lock:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", f"={parent_session}:", "#{" + LINEAGE_OPTION + "}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                raise ValueError("parent session is no longer running")
            parent = parse_lineage(result.stdout.strip())
            if parent is None:
                parent = {"id": str(uuid.uuid4())}
                stamped = subprocess.run(
                    ["tmux", *lineage_args(parent_session, parent)[1:]], capture_output=True, text=True, timeout=2
                )
                if stamped.returncode != 0:
                    raise ValueError("could not record parent session identity")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("could not resolve parent session") from exc
    return {**child, "parent_id": parent["id"], "parent_name": parent_session}
