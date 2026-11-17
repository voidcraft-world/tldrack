"""Transition ring buffer — recent per-session state/activity changes.

Feeds the rack UI's activity ticker (GET /activity + the `activity` SSE event).
Entries are appended only when something USER-VISIBLE changed: an actual state
transition, or a PreToolUse whose classified activity differs from the stored
one. Repeated busy hooks and Notification keep-alives don't spam the window.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from tldrack.activity import KEEP_ACTIVITY

DEFAULT_MAXLEN = 100


def transition_entry(
    session: str,
    event: str,
    previous: dict,
    state: str,
    activity: object,
    now=time.time,
) -> dict | None:
    """Ticker entry for a report, or None when nothing user-visible changed.

    `previous` is StatusStore.set's return ({} on first report); `activity` is
    the update that went with it: a zone dict, None (cleared), or the
    KEEP_ACTIVITY sentinel (carry stored forward — Notification, legacy
    /status). The entry's activity is what the session is doing AFTER the
    report, so an attention entry keeps the tool it was on when it asked.
    Pure — `now` injectable for tests.
    """
    from_state = previous.get("state") or "unknown"
    if activity is KEEP_ACTIVITY:
        entry_activity = previous.get("activity")
    else:
        entry_activity = activity if isinstance(activity, dict) else None
    activity_changed = event == "PreToolUse" and isinstance(activity, dict) and activity != previous.get("activity")
    if state == from_state and not activity_changed:
        return None
    return {
        "ts": round(now(), 3),
        "session": session,
        "from_state": from_state,
        "to_state": state,
        "event": event,
        "activity": entry_activity,
    }


class TransitionLog:
    """Bounded, thread-safe, in-memory — lost on restart like StatusStore."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._lock = threading.Lock()
        self._entries: deque[dict] = deque(maxlen=maxlen)

    def append(self, entry: dict) -> None:
        with self._lock:
            self._entries.append(entry)

    def latest(self, limit: int | None = None) -> list[dict]:
        """Newest-first copies; `limit` caps the count (None = everything)."""
        with self._lock:
            entries = [entry.copy() for entry in reversed(self._entries)]
        return entries if limit is None else entries[:limit]
