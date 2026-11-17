"""The sessions the user closed recently, so a ✕ can be taken back.

`session_records.py` remembers where every LIVE session is so a reboot can
bring it back, and it forgets a session the moment it is closed on purpose —
which is right for a reboot (a killed agent must not respawn at the next
RESTORE) and wrong for a slip of the hand. This is the other half: the record a
close throws away lands HERE, with when and how it ended, and the rack's ↺
CLOSED list reopens one with `claude --resume=<id>` (`POST /reopen`, main.py).

    {"version": 1, "closed": {"a1b2c3d4e5f6": {
        "session": "wt-fix-nav", "cwd": "/Users/u/worktrees/project/fix-nav",
        "profile": "claude-a", "kind": "claude",
        "agent_session_id": "6f1c…-uuid", "label": "fix the nav sidebar",
        "closed_at": 1791500000.0, "how": "killed"}}}

`~/.local/state/tldrack/closed-sessions.json` (env
`TLDRACK_CLOSED_PATH`). Keyed by an opaque id, never by session name:
`vc-3` closed twice is two entries, and a reused name must not overwrite the
first one's conversation.

── WHAT COUNTS AS A CLOSE ──────────────────────────────────────────────────────
- `killed` — `POST /kill-session` succeeded (the rack ✕, the Branch Harbor's kill).
- `gone` — the drop rule in `session_records.py`: live on the previous pass,
  absent now while other sessions are still live (a hand-run `tmux kill-session`,
  a pane closed in iTerm, a worktree CLEAN). Only when it ran a CONVERSATION:
  deploy shells, job panes and services end themselves all day, and a list full
  of them would bury the one agent you meant to get back. A reboot is never a
  close: a dead tmux server lists nothing, and that is RESTORE's job.

── WHAT IS KEPT, AND FOR HOW LONG ─────────────────────────────────────────────
At most CLOSED_MAX entries, each for RECORD_MAX_AGE_DAYS (the same horizon the
reboot records have — a conversation older than that is `claude --resume`'s
picker's business). One conversation is one entry: closing a reopened session
again replaces its earlier entry rather than listing it twice. A reopen TAKES
the entry; it comes back only if that session is closed again.

A STARRED close (`starred_at`, the star `stars.py` took when the session closed)
does not count toward CLOSED_MAX and is never dropped for room: the operator
marked it to come back to, and twenty later closes must not bury it. It still
ages out with the rest, because past that horizon nothing here can resume it.
A reopen hands the star back.

`label` is the rack's first-prompt name for a `vc-N` session
(`session_names.py`), taken at close time: it is the only thing that tells two
closed `vc-*` rows apart, and a reopen hands it back.

The conversation id never leaves the bridge: `GET /closed` rows say
`resumable`, exactly as `GET /restorable` does.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping

from tldrack import paths
from tldrack.profiles import AGENT_SESSION_ID_RE, PROFILE_ID_RE, PROFILE_KINDS
from tldrack.session_names import VC_NAME, valid_title
from tldrack.session_records import RECORD_MAX_AGE_DAYS, optional_fields
from tldrack.session_records import SPARSE_FIELDS as RECORD_SPARSE_FIELDS
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE
from tldrack.stars import clean_starred_at

CLOSED_FILE_VERSION = 1
#: The list is an undo, not an archive: the last screenful of closes.
CLOSED_MAX = 20
CLOSED_FILE = "closed-sessions.json"

CLOSE_KINDS = ("killed", "gone")
#: Serialization order of one entry.
ENTRY_FIELDS = (
    "session",
    "cwd",
    "profile",
    "kind",
    "agent_session_id",
    *RECORD_SPARSE_FIELDS,
    "label",
    "starred_at",
    "closed_at",
    "how",
)
#: Optional fields written only when set: the record's own (`session_records.optional_fields`), the
#: label, and the star.
SPARSE_FIELDS = (*RECORD_SPARSE_FIELDS, "label", "starred_at")
#: How far `reopen_name` counts before giving up on a free name.
MAX_NAME_SUFFIX = 99


def resolve_closed_path() -> Path:
    env_path = os.environ.get("TLDRACK_CLOSED_PATH")
    return Path(env_path) if env_path else paths.state_file(CLOSED_FILE)


def new_closed_id() -> str:
    return secrets.token_hex(6)


def is_closed_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 12 and all(c in "0123456789abcdef" for c in value)


def clean_label(value: object) -> str | None:
    """A non-empty `session_names.py` label, or None."""
    return value if value and valid_title(value) else None


def reopen_name(original: str, taken: Iterable[str]) -> str | None:
    """The tmux name a reopen spawns under. Pure.

    The original name when it is free. A generated `vc-N` that a `+ NEW` has
    since reused takes the lowest free `vc-N`, so it stays a generated name and
    keeps its label; any other name gets `-2`, `-3`, … None when nothing within
    MAX_NAME_SUFFIX is free.
    """
    taken_set = set(taken)
    if original not in taken_set:
        return original
    if VC_NAME.fullmatch(original):
        candidates = (f"vc-{n}" for n in range(1, MAX_NAME_SUFFIX + 1))
    else:
        candidates = (f"{original}-{n}" for n in range(2, MAX_NAME_SUFFIX + 1))
    return next((name for name in candidates if name not in taken_set and SESSION_NAME_RE.fullmatch(name)), None)


def clean_entry(raw: object) -> dict | None:
    """One stored entry, re-validated field by field, or None. Pure.

    The same posture as `session_records.clean_record`: `cwd` becomes a spawn
    directory and the id lands on argv, and this file is writable by anything
    running as the user. A bad optional field drops to None; an entry without a
    session name, a cwd or a usable `closed_at` can never be reopened or aged
    out, so it is dropped whole.
    """
    if not isinstance(raw, dict):
        return None
    session = raw.get("session")
    cwd = raw.get("cwd")
    closed_at = raw.get("closed_at")
    if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
        return None
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    if isinstance(closed_at, bool) or not isinstance(closed_at, (int, float)) or closed_at <= 0:
        return None
    profile = raw.get("profile")
    kind = raw.get("kind")
    agent_id = raw.get("agent_session_id")
    label = clean_label(raw.get("label"))
    starred_at = clean_starred_at(raw.get("starred_at"))
    return {
        "session": session,
        "cwd": cwd,
        "profile": profile if isinstance(profile, str) and PROFILE_ID_RE.fullmatch(profile) else None,
        "kind": kind if kind in PROFILE_KINDS else None,
        "agent_session_id": agent_id if isinstance(agent_id, str) and AGENT_SESSION_ID_RE.fullmatch(agent_id) else None,
        **optional_fields(raw),
        **({"label": label} if label else {}),
        **({"starred_at": starred_at} if starred_at else {}),
        "closed_at": float(closed_at),
        "how": raw.get("how") if raw.get("how") in CLOSE_KINDS else "gone",
    }


def parse_closed(text: str) -> dict[str, dict]:
    """The file → `id -> entry`. Raises ValueError on a bad document."""
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("version") != CLOSED_FILE_VERSION:
        raise ValueError("not a version-1 closed-sessions document")
    closed = document.get("closed")
    if not isinstance(closed, dict):
        raise ValueError("`closed` must be an object")
    entries: dict[str, dict] = {}
    for key, raw in closed.items():
        if is_closed_id(key):
            entry = clean_entry(raw)
            if entry is not None:
                entries[key] = entry
    return entries


def serialize_closed(entries: Mapping[str, dict]) -> str:
    return (
        json.dumps(
            {
                "version": CLOSED_FILE_VERSION,
                "closed": {
                    key: {
                        field: entries[key].get(field)
                        for field in ENTRY_FIELDS
                        if field not in SPARSE_FIELDS or entries[key].get(field)
                    }
                    for key in sorted(entries)
                },
            },
            indent=2,
        )
        + "\n"
    )


def is_expired(entry: Mapping, now: float) -> bool:
    return now - entry.get("closed_at", 0.0) > RECORD_MAX_AGE_DAYS * 86400


class ClosedSessionStore(SessionStateFile[dict]):
    """`id -> entry`, persisted: the newest CLOSED_MAX, and every starred one. Thread-safe.

    Rides `SessionStateFile` for its load and atomic write; its keys are close
    ids, so the base's `rename` (a session-name move) has no meaning here and
    nothing calls it.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(
            path,
            parse=parse_closed,
            serialize=serialize_closed,
            noun="closed sessions",
            plural="closed sessions",
            file_stem="closed-sessions",
        )
        self._clock = clock
        with self._lock:
            if self._prune_locked():
                self._persist_locked()

    # -- reads ----------------------------------------------------------------
    def entries(self) -> list[tuple[str, dict]]:
        """`(id, entry)` newest first, as copies. Prunes the expired ones."""
        with self._lock:
            if self._prune_locked():
                self._persist_locked()
            return [
                (key, dict(entry))
                for key, entry in sorted(self._entries.items(), key=lambda item: item[1]["closed_at"], reverse=True)
            ]

    def get(self, closed_id: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(closed_id)
            return dict(entry) if entry is not None else None

    # -- writes ---------------------------------------------------------------
    def add(self, session: str, record: Mapping | None, how: str, label: object = None, starred_at: object = None) -> str | None:
        """File one close from the record `session_records` is about to drop.
        `starred_at` is the star the session closed with (`stars.py` `take`).
        Returns the new id, or None when the record cannot be reopened (no
        record yet, no cwd)."""
        if record is None:
            return None
        now = self._clock()
        entry = clean_entry(
            {**record, "session": session, "label": label, "starred_at": starred_at, "closed_at": now, "how": how}
        )
        if entry is None or (how == "gone" and entry["agent_session_id"] is None):
            return None
        closed_id = new_closed_id()
        with self._lock:
            agent_id = entry["agent_session_id"]
            if agent_id is not None:
                # One conversation, one entry: the newest close wins.
                for key in [key for key, old in self._entries.items() if old.get("agent_session_id") == agent_id]:
                    del self._entries[key]
            self._entries[closed_id] = entry
            self._prune_locked()
            self._persist_locked()
        return closed_id

    def take(self, closed_id: str) -> dict | None:
        """Remove and return one entry — a reopen consumed it."""
        with self._lock:
            entry = self._entries.pop(closed_id, None)
            if entry is not None:
                self._persist_locked()
            return entry

    def _prune_locked(self) -> bool:
        """Drop the expired, then every unstarred entry past CLOSED_MAX (oldest first)."""
        now = self._clock()
        before = len(self._entries)
        for key in [key for key, entry in self._entries.items() if is_expired(entry, now)]:
            del self._entries[key]
        unstarred = [key for key, entry in self._entries.items() if not entry.get("starred_at")]
        if len(unstarred) > CLOSED_MAX:
            newest = sorted(unstarred, key=lambda key: self._entries[key]["closed_at"], reverse=True)
            for key in newest[CLOSED_MAX:]:
                del self._entries[key]
        return len(self._entries) != before
