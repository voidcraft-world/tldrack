"""The rack's state changes and phone pushes, kept on disk for the session ledger.

`history.TransitionLog` is a 100-entry ring buffer for the rack UI's ticker, and `notify.py`'s
ledger is in memory on purpose (a cooldown restored from disk would mute a new alert). Both are
right for what they serve, and both forget the two things the session ledger most wants about the
operator's attention: how long each agent sat in `attention` waiting on a human, and every time the
operator's phone was pinged about it. Pinging the human less cannot be measured without them.

So each ticker entry and each push is also one JSON line here, at
`~/.local/state/tldrack/rack-activity.jsonl` (env `TLDRACK_ACTIVITY_LOG_PATH`,
spelled once in `ledger/side_ledgers.py` `activity_log_path`), and `tldrack ledger ingest`
folds it into `bridge_rows` (source `rack-activity`). Rows are POINTERS, as push bodies are: a
session name, states, an event, the activity's zone and tool, a push's title — never an agent's
message and never a command line.

Rotation keeps `KEEP_SEGMENTS` timestamped files, ~8 months of a busy rack; the ledger has read
every row long before a segment ages out. An append that fails is dropped silently: this is a
record, never a reason for a hook to fail.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tldrack.ledger.side_ledgers import activity_log_path

ROTATE_BYTES = 32 * 1024 * 1024
KEEP_SEGMENTS = 8
#: The activity fields worth keeping: where the agent was, never what it ran.
ACTIVITY_FIELDS = ("zone", "tool")


def transition_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A ticker entry (`history.transition_entry`) as a log row — pointers only. Pure."""
    activity = entry.get("activity") if isinstance(entry.get("activity"), Mapping) else None
    return {
        "kind": "transition",
        "ts": entry.get("ts"),
        "session": entry.get("session"),
        "from": entry.get("from_state"),
        "to": entry.get("to_state"),
        "event": entry.get("event"),
        "activity": {k: activity.get(k) for k in ACTIVITY_FIELDS if k in activity} if activity else None,
    }


def push_row(
    kind: str, sessions: tuple[str, ...] | list[str], title: str, sent: bool, now: float | None = None
) -> dict[str, Any]:
    """One phone push. `kind` is `agents` (blocked sessions), `test` or `event`. Pure."""
    return {
        "kind": "push",
        "ts": round(time.time() if now is None else now, 3),
        "push": kind,
        "sessions": list(sessions),
        "title": title[:120],
        "sent": bool(sent),
    }


class ActivityLog:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or activity_log_path()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, row: Mapping[str, Any]) -> None:
        try:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_full()
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except (OSError, TypeError, ValueError):
            pass

    def _rotate_if_full(self) -> None:
        try:
            if self._path.stat().st_size < ROTATE_BYTES:
                return
        except FileNotFoundError:
            return
        stamp = time.strftime("%Y%m%dT%H%M%S")
        self._path.replace(self._path.with_name(f"{self._path.stem}-{stamp}{self._path.suffix}"))
        segments = sorted(self._path.parent.glob(f"{self._path.stem}-*{self._path.suffix}"))
        for old in segments[:-KEEP_SEGMENTS]:
            try:
                old.unlink()
            except OSError:
                pass
