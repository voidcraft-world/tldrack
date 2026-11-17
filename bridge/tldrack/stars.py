"""The operator's stars: the handful of conversations to come back to.

A rack holds many sessions at once, and the marks on a row are all the RACK's
reading of it (busy, ✅, ⚠️, the generated name). A star is the one mark the
OPERATOR sets: "I paused this thought, and I want it again". Nothing here judges
or expires it; only the operator sets it and only the operator clears it.

    {"version": 1, "sessions": {"vc-7": 1791500000.0}}

`~/.local/state/tldrack/session-stars.json` (env `TLDRACK_STARS_PATH`). The value
is when the star was set, so a client can order by it. The bridge is the one
store, so a star set on any surface shows on every one of them.

── A STAR FOLLOWS THE CONVERSATION, NOT THE NAME ───────────────────────────────
This is where it differs from an icon pick (`icons.py`), which a recreated name
keeps. `vc-3` starred, closed, and minted again by `+ NEW` is a new conversation
and opens with no star. So:

- a rename moves the star (`SessionStateFile.rename`);
- a close TAKES it (`take`): it rides the closed entry (`closed_sessions.py`,
  `starred_at`), where it also exempts that entry from the list's count cap;
- a reopen hands it back (`adopt`), with its original time;
- a spawn under a free name forgets whatever an earlier occupant left.

A reboot is not a close: the store is keyed by name and `POST /restore` brings a
session back under its name, so its star is still there.

The row field is `starred`, a boolean on EVERY row of a bridge that has this
store, so a client draws the control only where the bridge can keep its answer.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import Callable

from tldrack import paths
from tldrack.session_state_file import SessionStateFile, parse_session_map, serialize_session_map

STARS_FILE_VERSION = 1
STARS_FILE = "session-stars.json"


def resolve_stars_path() -> Path:
    env_path = os.environ.get("TLDRACK_STARS_PATH")
    return Path(env_path) if env_path else paths.state_file(STARS_FILE)


def clean_starred_at(value: object) -> float | None:
    """A stored star's time: a positive number of epoch seconds, or None. Pure."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return None
    return float(value)


class SessionStarStore(SessionStateFile[float]):
    """`session -> starred_at`, persisted. Every method is thread-safe."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(
            path,
            parse=lambda text: parse_session_map(text, clean_starred_at),
            serialize=lambda entries: serialize_session_map(entries, STARS_FILE_VERSION),
            noun="star",
            plural="stars",
            file_stem="session-stars",
        )
        self._clock = clock

    def get_all(self) -> dict[str, float]:
        with self._lock:
            return dict(self._entries)

    def set(self, session: str, starred: bool) -> bool:
        """Star or unstar a session. True when that changed something: starring a
        starred session keeps its first time, so a second press never reorders it."""
        with self._lock:
            if starred == (session in self._entries):
                return False
            if starred:
                self._entries[session] = self._clock()
            else:
                del self._entries[session]
            self._persist_locked()
            return True

    def take(self, session: str) -> float | None:
        """Remove and return a session's star: its conversation closed, and the star goes with it."""
        with self._lock:
            starred_at = self._entries.pop(session, None)
            if starred_at is not None:
                self._persist_locked()
            return starred_at

    def adopt(self, session: str, starred_at: object) -> None:
        """A reopened conversation gets back the star it closed with, under the name it reopened as."""
        cleaned = clean_starred_at(starred_at)
        if cleaned is None:
            return
        with self._lock:
            self._entries[session] = cleaned
            self._persist_locked()
