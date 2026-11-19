"""Is a launch profile's ACCOUNT able to run a turn at all? (`profiles[i].standing` on /snapshot.)

── WHY THIS IS NOT THE USAGE READING ────────────────────────────────────────
`usage.py` keeps the last rate-limit windows an account reported, and holds them until each
window's own reset. That is right for a spent account and wrong for a DEAD one: a subscription
that lapsed, or a login that was revoked, stops producing readings, so the last one (say 41% of
the week) stays on the LAUNCH chip as if the account were merely idle, and AUTO keeps scoring it
as the roomiest account there is. The panes make it worse: an open session keeps printing the
windows of its last good response on every status-line refresh, so the reading even looks fresh.

── WHAT IS EVIDENCE ─────────────────────────────────────────────────────────
A turn Claude Code could not run for a reason that belongs to the ACCOUNT, read from the
transcript's own error entry (`transcript.py` `fold_stop_failure`): `error` is one of
`ACCOUNT_ERRORS`. A rate limit is not one (it resets, and `launch_pick.py` already skips it
until then); an overload or a malformed request says nothing about the account.

Nothing here asks the CLI who it is signed in as. `claude auth status` answers from the login
it cached: on 2026-11-19 it still said `max` for an account whose operator had let the plan
lapse. So it cannot be the detector.

── WHAT CLEARS IT ───────────────────────────────────────────────────────────
A turn on that account that ENDED NORMALLY after the failure: the `Stop` hook, which Claude
Code fires only for a turn the API answered (a cut-off turn fires `StopFailure`). So the
operator who renews the plan launches an agent on the account, and its first finished turn
puts the chip back. A usage report cannot clear it, and a CHANGED one cannot either: two idle
panes on one account each re-post the windows of their own last response, so the stored
reading flips between them with no request made.

Order is by TIME, never by arrival: a cut-off session stays on the rack with its notice for
hours, and every snapshot re-reads it. A failure older than the account's last good turn is
history and marks nothing, which is why a failure carries its entry's own timestamp (`at`).
The last good turn is kept in memory for every account and written only when it clears a
mark, so a finished turn costs no write. The one gap that leaves: after a restart, a session
still showing a failure from BEFORE the account recovered marks it out again, until that
account's next finished turn.

── WHY IT PERSISTS ──────────────────────────────────────────────────────────
For `usage.py`'s reason: the account that needs the mark is the one the operator has stopped
using, so in memory it would vanish on the next restart and never come back.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from tldrack import paths
from tldrack.profiles import PROFILE_ID_RE
from tldrack.session_state_file import SessionStateFile

#: Claude Code's error tokens for a turn the ACCOUNT could not pay for or sign.
ACCOUNT_ERRORS = frozenset({"billing_error", "authentication_failed"})

STANDING_FILE = "account-standing.json"
STANDING_FILE_VERSION = 1

#: The CLI's notice rides along for the tooltip; a cap, not a format.
MESSAGE_MAX_CHARS = 300


def resolve_standing_path() -> Path:
    env_path = os.environ.get("TLDRACK_STANDING_PATH")
    return Path(env_path) if env_path else paths.state_file(STANDING_FILE)


def account_failure(row: Mapping) -> dict | None:
    """A session row's cut-off, when it is the account's: `{"error", "message", "at"}`, else None.
    `at` is the error entry's own time, or None where the transcript gave none."""
    failure = row.get("stop_failure")
    if not isinstance(failure, Mapping) or failure.get("error") not in ACCOUNT_ERRORS:
        return None
    message = failure.get("message")
    at = failure.get("at")
    return {
        "error": failure["error"],
        "message": message[:MESSAGE_MAX_CHARS] if isinstance(message, str) and message else None,
        "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else None,
    }


def _clean_time(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def parse_standing_file(text: str) -> dict[str, dict]:
    """account-standing.json → `{profile id: {"out": {...} | None, "ok_at": epoch | None}}`.
    A wrong envelope raises; one malformed account is dropped alone."""
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise ValueError('expected {"version": ..., "profiles": {...}}')
    parsed: dict[str, dict] = {}
    for profile, entry in payload["profiles"].items():
        if not isinstance(profile, str) or not PROFILE_ID_RE.fullmatch(profile) or not isinstance(entry, dict):
            continue
        out = entry.get("out")
        mark = None
        if isinstance(out, dict) and out.get("error") in ACCOUNT_ERRORS and _clean_time(out.get("at")) is not None:
            message = out.get("message")
            mark = {
                "error": out["error"],
                "message": message[:MESSAGE_MAX_CHARS] if isinstance(message, str) and message else None,
                "at": _clean_time(out["at"]),
            }
        ok_at = _clean_time(entry.get("ok_at"))
        if mark is not None or ok_at is not None:
            parsed[profile] = {"out": mark, "ok_at": ok_at}
    return parsed


def serialize_standing_file(profiles: dict[str, dict]) -> str:
    body = {"version": STANDING_FILE_VERSION, "profiles": {name: profiles[name] for name in sorted(profiles)}}
    return json.dumps(body, indent=2) + "\n"


class _StandingFile(SessionStateFile[dict]):
    """The persisted marks. The file, lock and tolerant load are owned by
    `session_state_file.py` — see the note there."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_standing_file,
            serialize=serialize_standing_file,
            noun="account standing",
            plural="account standings",
            file_stem="account-standing",
        )

    def entries(self) -> dict[str, dict]:
        with self._lock:
            return {name: dict(entry) for name, entry in self._entries.items()}

    def put(self, profile: str, entry: dict) -> None:
        with self._lock:
            self._entries[profile] = entry
            self._persist_locked()


class AccountStandingStore:
    """Which accounts are OUT, per launch profile id. The module docstring owns the rules.

    With a `path` the marks survive a restart; without one (tests, a handler built without a
    store) it is memory only.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._file = _StandingFile(path) if path is not None else None
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._file.entries() if self._file is not None else {}
        #: `(session, error, message)` readings that carried no time of their own, and when each
        #: was first seen: a re-read of the same notice must not count as a new failure.
        self._first_seen: dict[tuple[str, str, str | None], float] = {}

    def _write(self, profile: str, entry: dict) -> None:
        self._entries[profile] = entry
        if self._file is not None:
            self._file.put(profile, dict(entry))

    def observe(self, rows: Iterable[Mapping]) -> bool:
        """Read the live rows' cut-offs; True when a mark was made or replaced. Cheap when
        nothing is cut off, which is every snapshot but a handful."""
        changed = False
        for row in rows:
            profile = row.get("profile")
            if not row.get("live") or not isinstance(profile, str) or not PROFILE_ID_RE.fullmatch(profile):
                continue
            failure = account_failure(row)
            if failure is None:
                continue
            with self._lock:
                at = failure["at"]
                if at is None:
                    key = (str(row.get("session")), failure["error"], failure["message"])
                    at = self._first_seen.setdefault(key, self._clock())
                entry = self._entries.get(profile) or {"out": None, "ok_at": None}
                ok_at = entry.get("ok_at")
                mark = entry.get("out")
                if (ok_at is not None and at <= ok_at) or (mark is not None and at <= mark["at"]):
                    continue
                self._write(profile, {"out": {**failure, "at": at}, "ok_at": ok_at})
                changed = True
        return changed

    def mark_ok(self, profile: str | None) -> bool:
        """A turn on this account just ended normally. True when that cleared a mark."""
        if not isinstance(profile, str) or not PROFILE_ID_RE.fullmatch(profile):
            return False
        with self._lock:
            entry = self._entries.get(profile)
            now = self._clock()
            if entry is None or entry.get("out") is None:
                # Memory only: the module docstring owns why a finished turn writes nothing.
                self._entries[profile] = {"out": None, "ok_at": now}
                return False
            self._write(profile, {"out": None, "ok_at": now})
            return True

    def by_profile(self) -> dict[str, dict]:
        """`{profile id: {"state": "out", "error", "message", "since"}}` for every account that is out."""
        with self._lock:
            marks = {profile: entry["out"] for profile, entry in self._entries.items() if entry.get("out")}
        return {
            profile: {"state": "out", "error": mark["error"], "message": mark["message"], "since": mark["at"]}
            for profile, mark in marks.items()
        }
