"""Where every tmux session was, so a reboot is not the end of it.

tmux sessions die with the machine; the conversations inside them do not —
`claude --resume=<id>` reopens one exactly. What is lost is the MAPPING: which
session name ran which conversation, on which account, in which directory. This
store keeps it, and `POST /restore` (main.py) replays it.

    {"version": 1, "sessions": {"wt-fix-nav": {
        "cwd": "/Users/u/worktrees/project/fix-nav",
        "profile": "claude-a", "kind": "claude",
        "agent_session_id": "6f1c…-uuid", "last_seen": 1791500000.0}}}

`~/.local/state/tldrack/session-records.json` (env
`TLDRACK_RECORDS_PATH`), with the tolerant load and atomic write every
store here shares (`session_state_file.py`).

── FED BY THREE THINGS ────────────────────────────────────────────────────────
- the snapshot pass (`observe_live`): cwd — AFTER `apply_hook_cwds`, so a
  worktree agent records where Claude is, not where its shell is — profile,
  kind, `last_seen`;
- `POST /hook`: the hook body's `session_id`;
- the state arbiter's index read: Claude's own `sessionId` per session, which
  covers an account with no hooks installed.

── THE DROP RULE IS THE CAREFUL PART ──────────────────────────────────────────
A record exists to outlive its session, so "not live right now" can never be a
reason to delete it — that is precisely the state after a reboot. A record is
forgotten only when

- a route ended it on purpose (`/kill-session`, and `/spawn` reusing the name),
- it was live on the PREVIOUS pass and is absent now while some OTHER session
  is still live (it was killed by hand — the tmux server is plainly fine), or
- it is older than RECORD_MAX_AGE_DAYS.

`/kill-session` and the hand kill are CLOSES: the record moves to
`closed_sessions.py`, where the rack's ↺ CLOSED list can reopen it.

A dead tmux server, or a `tmux ls` that failed, lists nothing — and nothing
being live is never evidence. Neither is the first pass after a bridge start,
which has no "previous" to compare against.

Disk is written only when a field changes, or when `last_seen` has moved
LAST_SEEN_WRITE_SECONDS: a poll every 3 s must not become a write every 3 s.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping

from tldrack import paths
from tldrack.profiles import AGENT_SESSION_ID_RE, PROFILE_ID_RE, PROFILE_KINDS, clean_launch_model, clean_permission_mode
from tldrack.session_lineage import clean_lineage
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE

RECORDS_FILE_VERSION = 1
RECORD_MAX_AGE_DAYS = 7
LAST_SEEN_WRITE_SECONDS = 600.0
RECORDS_FILE = "session-records.json"

#: The fields one record holds, in serialization order.
RECORD_FIELDS = (
    "cwd",
    "profile",
    "kind",
    "agent_session_id",
    "last_seen",
    "lineage",
    "launch_permission_mode",
    "live_permission_mode",
    "launch_model",
    "subagent_model",
)
#: Optional fields written only when set (`optional_fields` cleans them), shared
#: with the closed list.
#: - `launch_permission_mode`: what the spawn stamped (`profiles.PERMISSION_MODE_OPTION`).
#: - `live_permission_mode`: the mode Claude last REPORTED on a main-thread hook
#:   (the payload's `permission_mode`). An operator's Shift+Tab out of `auto` lands
#:   here, and `restore.restore_mode` then refuses to replay `auto` over it.
#: - `launch_model`, `subagent_model`: the models the spawn stamped (`profiles.py`
#:   § LAUNCH MODELS), replayed by a restore.
SPARSE_FIELDS = ("lineage", "launch_permission_mode", "live_permission_mode", "launch_model", "subagent_model")
#: A hook-reported mode is Claude's own token (`default`, `plan`, `auto`, …): kept
#: by shape, since only its equality with the launch mode is ever read.
LIVE_MODE_RE = re.compile(r"[A-Za-z]{1,32}")


def resolve_records_path() -> Path:
    env_path = os.environ.get("TLDRACK_RECORDS_PATH")
    return Path(env_path) if env_path else paths.state_file(RECORDS_FILE)


def clean_record(raw: object) -> dict | None:
    """One stored record, re-validated field by field, or None. Pure.

    Every field ends up somewhere that matters — `cwd` is a spawn directory,
    `profile` picks a CLI and an account, the id lands on argv — and this file
    is writable by anything running as the user. A bad FIELD is dropped to
    None; a record without a usable `last_seen` is dropped whole, since it can
    never be aged out.
    """
    if not isinstance(raw, dict):
        return None
    last_seen = raw.get("last_seen")
    if isinstance(last_seen, bool) or not isinstance(last_seen, (int, float)) or last_seen <= 0:
        return None
    cwd = raw.get("cwd")
    profile = raw.get("profile")
    kind = raw.get("kind")
    agent_id = raw.get("agent_session_id")
    return {
        "cwd": cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None,
        "profile": profile if isinstance(profile, str) and PROFILE_ID_RE.fullmatch(profile) else None,
        "kind": kind if kind in PROFILE_KINDS else None,
        "agent_session_id": agent_id if isinstance(agent_id, str) and AGENT_SESSION_ID_RE.fullmatch(agent_id) else None,
        "last_seen": float(last_seen),
        **optional_fields(raw),
    }


def clean_live_mode(value: object) -> str | None:
    return value if isinstance(value, str) and LIVE_MODE_RE.fullmatch(value) else None


def optional_fields(raw: Mapping) -> dict:
    """The SPARSE_FIELDS a stored row carries, each re-validated, absent when unset.
    Pure — the one cleaner for a record and a closed entry alike."""
    cleaned = {
        "lineage": clean_lineage(raw.get("lineage")),
        "launch_permission_mode": clean_permission_mode(raw.get("launch_permission_mode")),
        "live_permission_mode": clean_live_mode(raw.get("live_permission_mode")),
        "launch_model": clean_launch_model(raw.get("launch_model")),
        "subagent_model": clean_launch_model(raw.get("subagent_model")),
    }
    return {field: value for field, value in cleaned.items() if value}


def parse_records(text: str) -> dict[str, dict]:
    """The file → `session -> record`. Raises ValueError on a bad document."""
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("version") != RECORDS_FILE_VERSION:
        raise ValueError("not a version-1 session-records document")
    sessions = document.get("sessions")
    if not isinstance(sessions, dict):
        raise ValueError("`sessions` must be an object")
    records: dict[str, dict] = {}
    for name, raw in sessions.items():
        if isinstance(name, str) and SESSION_NAME_RE.fullmatch(name):
            record = clean_record(raw)
            if record is not None:
                records[name] = record
    return records


def serialize_records(records: Mapping[str, dict]) -> str:
    return (
        json.dumps(
            {
                "version": RECORDS_FILE_VERSION,
                "sessions": {
                    name: {
                        field: records[name].get(field)
                        for field in RECORD_FIELDS
                        if field not in SPARSE_FIELDS or records[name].get(field)
                    }
                    for name in sorted(records)
                },
            },
            indent=2,
        )
        + "\n"
    )


def is_expired(record: Mapping, now: float) -> bool:
    return now - record.get("last_seen", 0.0) > RECORD_MAX_AGE_DAYS * 86400


class SessionRecordStore(SessionStateFile[dict]):
    """`session -> record`, persisted. Every method is thread-safe."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(
            path,
            parse=parse_records,
            serialize=serialize_records,
            noun="session records",
            plural="session records",
            file_stem="session-records",
        )
        self._clock = clock
        # Sessions live on the last pass; None until the first pass, which has
        # nothing to compare against (see the drop rule in the module docstring).
        self._was_live: set[str] | None = None
        self._persisted_at = clock()
        with self._lock:
            now = clock()
            expired = [name for name, record in self._entries.items() if is_expired(record, now)]
            for name in expired:
                del self._entries[name]
            if expired:
                self._persist_locked()

    # -- reads ----------------------------------------------------------------
    def get(self, session: str) -> dict | None:
        with self._lock:
            record = self._entries.get(session)
            return dict(record) if record is not None else None

    def not_live(self, live: Iterable[str]) -> dict[str, dict]:
        """Unexpired records with no live session behind them — the restore
        candidates — as copies. Prunes the expired ones on the way."""
        live_set = set(live)
        now = self._clock()
        with self._lock:
            expired = [name for name, record in self._entries.items() if is_expired(record, now)]
            for name in expired:
                del self._entries[name]
            if expired:
                self._persist_locked()
            return {name: dict(record) for name, record in self._entries.items() if name not in live_set}

    # -- writes ---------------------------------------------------------------
    def observe_live(self, rows: Iterable[Mapping], kind_of: Callable[[object], str | None]) -> dict[str, dict]:
        """One snapshot pass. `rows` are merged snapshot rows; dead ones are
        ignored. `kind_of` maps a profile id to its kind (the registry's).

        Returns the records this pass DROPPED under the drop rule — sessions
        closed outside the bridge — so the caller can file them as closes
        (`closed_sessions.py`)."""
        now = self._clock()
        live = {
            row["session"]: row
            for row in rows
            if row.get("live") and isinstance(row.get("session"), str) and SESSION_NAME_RE.fullmatch(row["session"])
        }
        dropped: dict[str, dict] = {}
        with self._lock:
            changed = False
            for name, row in live.items():
                record = self._entries.get(name) or dict.fromkeys(RECORD_FIELDS)
                cwd = row.get("cwd") if isinstance(row.get("cwd"), str) and os.path.isabs(row["cwd"]) else None
                profile = row.get("profile") if isinstance(row.get("profile"), str) else None
                updated = {
                    **record,
                    # A pass that cannot see a cwd/profile keeps the last one.
                    "cwd": cwd or record.get("cwd"),
                    "profile": profile or record.get("profile"),
                    "last_seen": now,
                    # NOT kept forward: a live row always sees the stamp (an unset tmux option
                    # reads ""), so its absence means this session was launched without one —
                    # a name reused after a reboot must not inherit its predecessor's `auto`.
                    "launch_permission_mode": clean_permission_mode(row.get("launch_permission_mode")),
                    "launch_model": clean_launch_model(row.get("launch_model")),
                    "subagent_model": clean_launch_model(row.get("subagent_model")),
                    **({"lineage": clean_lineage(row["lineage"])} if clean_lineage(row.get("lineage")) else {}),
                }
                updated["kind"] = kind_of(updated["profile"]) if updated["profile"] else record.get("kind")
                if any(updated.get(f) != record.get(f) for f in RECORD_FIELDS if f != "last_seen"):
                    changed = True
                self._entries[name] = updated
            if self._was_live is not None and live:
                for name in self._was_live - live.keys():
                    record = self._entries.pop(name, None)
                    if record is not None:
                        dropped[name] = record
                        changed = True
            self._was_live = set(live)
            if changed or (live and now - self._persisted_at >= LAST_SEEN_WRITE_SECONDS):
                self._persist_locked()
        return dropped

    def note_agent_session(self, session: str, agent_session_id: object) -> None:
        """Record which conversation a session runs. A uuid or nothing; a
        session with no record yet waits for its first snapshot pass."""
        if not isinstance(agent_session_id, str) or not AGENT_SESSION_ID_RE.fullmatch(agent_session_id):
            return
        with self._lock:
            record = self._entries.get(session)
            if record is None or record.get("agent_session_id") == agent_session_id:
                return
            record["agent_session_id"] = agent_session_id
            self._persist_locked()

    def note_live_mode(self, session: str, mode: object) -> None:
        """Record the permission mode a main-thread hook reported (see
        SPARSE_FIELDS). No-ops on an unchanged reading, so a tool-call storm never
        rewrites the file, and on a session with no record yet."""
        live = clean_live_mode(mode)
        if live is None:
            return
        with self._lock:
            record = self._entries.get(session)
            if record is None or record.get("live_permission_mode") == live:
                return
            record["live_permission_mode"] = live
            self._persist_locked()

    def clear_agent_session(self, session: str) -> None:
        """The user ended that conversation (`/exit`); restore a shell, not it."""
        with self._lock:
            record = self._entries.get(session)
            if record is None or record.get("agent_session_id") is None:
                return
            record["agent_session_id"] = None
            self._persist_locked()

    def rename(self, old: str, new: str) -> bool:
        moved = super().rename(old, new)
        with self._lock:
            if self._was_live is not None and old in self._was_live:
                self._was_live.discard(old)
                self._was_live.add(new)
        return moved

    def forget(self, session: str) -> bool:
        dropped = super().forget(session)
        with self._lock:
            if self._was_live is not None:
                self._was_live.discard(session)
        return dropped

    def _persist_locked(self) -> None:
        super()._persist_locked()
        self._persisted_at = self._clock()
