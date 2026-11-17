"""Account-level Claude usage-limit store (the /usage screen data).

Claude Code's statusLine command receives JSON on stdin whose `rate_limits`
carries the 5-hour and weekly windows shown by /usage (Max subscribers only).
statusline.py forwards that raw JSON to POST /usage; this module validates it
and keeps the latest report so /snapshot can expose it to every client.

Account-level, not per-session: the windows are the same for every session on
one ACCOUNT, so within an account the latest report wins. Across accounts it
must not — two Claude accounts (a second one pinned by `CLAUDE_CONFIG_DIR`)
reporting into one slot made every reader flip between them on each statusline
refresh. Reports are therefore keyed by launch profile (profiles.py), resolved
from the statusline payload's `transcript_path`; an unresolvable report keeps
the `None` slot, which is exactly the single slot this store used to be.

── WHY THE PER-ACCOUNT SLOTS PERSIST ───────────────────────────────────────
A report arrives only while a session ON THAT ACCOUNT renders its status line.
The account you most need a reading for is the one you have stopped using
because it is spent — so in memory, its 100% vanished on every bridge restart
and never came back, and the rack's LAUNCH chip showed nothing for exactly the
account it should have warned about. So attributed slots are written to disk
(`usage-limits.json`, beside `session-models.json`) and reloaded at start.

Two rules keep a persisted reading honest:
- **A window past its `resets_at` is dropped on read.** After the reset the
  old percentage describes a window that no longer exists; "unknown" is true,
  "100%" would be a lie. Applied to live slots too — a long-running bridge
  outlives resets just as a restarted one does.
- **`age_seconds` is wall-clock**, from when the WINDOWS last changed. The file
  is written only on a change (the status line refreshes every few seconds),
  so a reloaded age can only overstate how old the reading is, never understate.
The unattributed `None` slot stays in memory: it names no account to keep.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from tldrack import paths
from tldrack.profiles import PROFILE_ID_RE
from tldrack.session_state_file import SessionStateFile

USAGE_WINDOWS = ("five_hour", "seven_day")

USAGE_FILE_VERSION = 1

USAGE_FILE = "usage-limits.json"


def resolve_usage_path() -> Path:
    env_path = os.environ.get("TLDRACK_USAGE_PATH")
    return Path(env_path) if env_path else paths.state_file(USAGE_FILE)


def _parse_resets_at(value: object, window: str) -> float:
    """`resets_at` as epoch seconds. Claude Code has shipped it as a number;
    accept an ISO-8601 string too so a format change doesn't silently drop
    every report. Anything else fails loud."""
    if isinstance(value, bool):
        raise ValueError(f"{window}.resets_at must be a timestamp, not a bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError as err:
            raise ValueError(f"{window}.resets_at is not a timestamp") from err
    raise ValueError(f"{window}.resets_at must be a timestamp")


def parse_usage_limits(payload: dict) -> dict:
    """Validated subset of a statusline payload's `rate_limits`.

    Absent `rate_limits` / absent windows are fine (non-subscriber, partial
    data) → {} / partial dict. A window that IS present must be well-formed —
    malformed shapes raise ValueError so garbage is never stored (fail loud,
    like hook_state's narrow matching). Kept per window: used_percentage,
    resets_at (when present).
    """
    rate_limits = payload.get("rate_limits")
    if rate_limits is None:
        return {}
    if not isinstance(rate_limits, dict):
        raise ValueError("rate_limits must be an object")
    windows: dict[str, dict] = {}
    for name in USAGE_WINDOWS:
        window = rate_limits.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError(f"{name} must be an object")
        used = window.get("used_percentage")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            raise ValueError(f"{name}.used_percentage must be a number")
        entry: dict[str, float] = {"used_percentage": float(used)}
        resets_at = window.get("resets_at")
        if resets_at is not None:
            entry["resets_at"] = _parse_resets_at(resets_at, name)
        windows[name] = entry
    return windows


def parse_usage_file(text: str) -> dict[str, dict]:
    """usage-limits.json → `{profile id: {"windows": …, "at": epoch}}`.

    The envelope failing raises (the caller warns and ignores the file); one
    malformed ACCOUNT is dropped alone, through the same `parse_usage_limits`
    that judges a live report, so the file can never hold a shape POST /usage
    would refuse.
    """
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise ValueError('expected {"version": ..., "profiles": {...}}')
    parsed: dict[str, dict] = {}
    for profile, entry in payload["profiles"].items():
        if not isinstance(profile, str) or not PROFILE_ID_RE.fullmatch(profile) or not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        try:
            windows = parse_usage_limits({"rate_limits": entry.get("windows")})
        except ValueError:
            continue
        if windows:
            parsed[profile] = {"windows": windows, "at": float(at)}
    return parsed


def serialize_usage_file(profiles: dict[str, dict]) -> str:
    body = {"version": USAGE_FILE_VERSION, "profiles": {name: profiles[name] for name in sorted(profiles)}}
    return json.dumps(body, indent=2) + "\n"


class _UsageFile(SessionStateFile[dict]):
    """The persisted per-account slots. The file, lock and tolerant load are
    owned by `session_state_file.py` — see the note there."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_usage_file,
            serialize=serialize_usage_file,
            noun="usage reading",
            plural="usage readings",
            file_stem="usage-limits",
        )

    def entries(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._entries)

    def put(self, profile: str, entry: dict) -> None:
        with self._lock:
            self._entries[profile] = entry
            self._persist_locked()


class UsageLimitsStore:
    """Latest usage-limit report per account (launch profile id, or None).

    With a `path`, attributed slots survive a restart (the header owns why);
    without one — tests, a handler built without a store — it is memory only.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._file = _UsageFile(path) if path is not None else None
        self._lock = threading.Lock()
        self._slots: dict[str | None, tuple[dict[str, dict], float]] = {}
        if self._file is not None:
            for profile, entry in self._file.entries().items():
                self._slots[profile] = (entry["windows"], entry["at"])

    def set(self, windows: dict, profile: str | None = None) -> None:
        copy = {name: entry.copy() for name, entry in windows.items()}
        now = self._clock()
        with self._lock:
            previous = self._slots.get(profile)
            changed = previous is None or previous[0] != copy
            # An unchanged reading keeps its timestamp: age = since it last MOVED.
            self._slots[profile] = (copy, now if changed else previous[1])
        if changed and profile is not None and self._file is not None:
            self._file.put(profile, {"windows": copy, "at": now})

    def _view(self, slot: tuple[dict[str, dict], float]) -> dict | None:
        windows, at = slot
        now = self._clock()
        live = {
            name: entry.copy()
            for name, entry in windows.items()
            if not (isinstance(entry.get("resets_at"), (int, float)) and entry["resets_at"] <= now)
        }
        if not live:
            return None
        live["age_seconds"] = round(max(0.0, now - at), 1)
        return live

    def get(self, profile: str | None = None) -> dict | None:
        """{five_hour?, seven_day?, age_seconds} copy for ONE account, or None
        before its first report (or once every window it had has reset).
        age_seconds lets clients grey out a stale bar."""
        with self._lock:
            slot = self._slots.get(profile)
        return self._view(slot) if slot is not None else None

    def get_preferred(self, profile: str | None) -> dict | None:
        """The legacy single-slot answer (`usage_limits` on /snapshot): this
        account's report when it has one, else the most recent of any.

        Preferring ONE account is what stops the old readers — the 3D usage
        bar, a native client — flipping between accounts; falling back to the
        latest keeps them showing something on a machine whose default
        account is simply idle.
        """
        with self._lock:
            slot = self._slots.get(profile)
            if slot is None and self._slots:
                slot = max(self._slots.values(), key=lambda pair: pair[1])
        return self._view(slot) if slot is not None else None

    def by_profile(self) -> dict[str, dict]:
        """`{profile id: view}` for every attributed account with a live window."""
        with self._lock:
            slots = [(key, slot) for key, slot in self._slots.items() if key is not None]
        views = ((key, self._view(slot)) for key, slot in slots)
        return {key: view for key, view in views if view is not None}
