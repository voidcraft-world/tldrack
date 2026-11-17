"""What each agent session has cost, per day — and the daily cap a FIXER spawn must honour.

The statusline already posts `cost.total_cost_usd` every refresh (`POST /usage`, `statusline.py`):
Claude Code's own running total for that conversation. Nothing kept it. This store does, because
the agent-ops loop (a fixer per prod error, a fixer per failed deploy) spawns agents nobody asked
for by hand, and an automatic spawn with no ceiling is how a budget dies quietly.

── ONE ROW PER CONVERSATION, A DELTA PER DAY ───────────────────────────────────
The figure is cumulative per conversation (`session_id`), so the store keeps the last total seen
and books only the INCREASE into today's bucket. A total that goes DOWN is a new count under the
same id (it should not happen; if it does, the new total is booked whole rather than lost). Days
are the bridge's local calendar date, and buckets older than `KEEP_DAYS` are pruned on write.

── WHICH RACK SESSION, RESOLVED ONCE ───────────────────────────────────────────
A statusline post names a transcript, not a tmux session. The caller resolves it through
`TranscriptTracker.paths()` (the same join `statusline_models.py` uses) and the store keeps the
LAST name seen, because a later snapshot may no longer hold the binding. Mission and fixer
rollups key on that name.

── FIXERS ──────────────────────────────────────────────────────────────────────
A fixer is a session the agent-ops loop started: `deploy-fix`, `deploy-fix-N`
(a deploy fix agent) and `wt-fix-err-*` (an error fixer's worktree, `error_fix.py`). `fixer_spend_today` is the number every fixer spawn checks against
`fix_cap_usd()` before it starts one. The cap is a ceiling on STARTING fixers, never a kill switch
for one already running.

Persisted to `~/.local/state/tldrack/session-costs.json` (env
`TLDRACK_COSTS_PATH`); the rack UI's DATA STORES field lists it.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tldrack import paths
from tldrack.atomic_store import atomic_write

COSTS_FILE_VERSION = 1
COSTS_FILE = "session-costs.json"
KEEP_DAYS = 14
#: A conversation that has not reported for this long is dropped from `sessions` (its day buckets stay until they age out).
FORGET_SESSION_SECONDS = KEEP_DAYS * 86_400
#: A single conversation reporting more than this is a malformed payload, not a real bill.
MAX_SESSION_USD = 10_000.0
MAX_SESSIONS = 2_000
AGENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TMUX_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FIXER_SESSION_RE = re.compile(r"^(deploy-fix(-\d+)?|wt-fix-err-[a-z0-9-]{1,48})$")
DEFAULT_FIX_CAP_USD = 20.0
FIX_CAP_ENV = "TLDRACK_FIX_CAP_USD"


def resolve_costs_path() -> Path:
    env_path = os.environ.get("TLDRACK_COSTS_PATH")
    return Path(env_path) if env_path else paths.state_file(COSTS_FILE)


def fix_cap_usd() -> float:
    """The daily ceiling on STARTING fixers. An unparseable env value falls back to the default."""
    raw = os.environ.get(FIX_CAP_ENV)
    try:
        value = float(raw) if raw else DEFAULT_FIX_CAP_USD
    except ValueError:
        return DEFAULT_FIX_CAP_USD
    return value if math.isfinite(value) and value >= 0 else DEFAULT_FIX_CAP_USD


def is_fixer(tmux_session: str | None) -> bool:
    return bool(tmux_session) and FIXER_SESSION_RE.match(tmux_session) is not None


def parse_statusline_cost(payload: Mapping[str, Any]) -> tuple[str, float] | None:
    """`(session_id, total_usd)` from a statusline payload, or None when either is absent or odd.
    Never raises: a cost reading is optional, and `/usage` must not fail over it."""
    session_id = payload.get("session_id")
    cost = payload.get("cost")
    total = cost.get("total_cost_usd") if isinstance(cost, Mapping) else None
    if not isinstance(session_id, str) or not AGENT_SESSION_ID_RE.match(session_id):
        return None
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    total = float(total)
    if not math.isfinite(total) or total < 0 or total > MAX_SESSION_USD:
        return None
    return session_id, total


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class SessionCostStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        #: agent session_id → {usd, tmux, cwd, seen}
        self._sessions: dict[str, dict[str, Any]] = {}
        #: YYYY-MM-DD → {agent session_id: usd booked that day}
        self._days: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            print(f"tldrack: ignoring unreadable {self._path} ({err})", file=sys.stderr)
            return
        if not isinstance(raw, dict) or raw.get("version") != COSTS_FILE_VERSION:
            return
        sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {}
        for sid, row in sessions.items():
            if isinstance(sid, str) and AGENT_SESSION_ID_RE.match(sid) and isinstance(row, dict):
                usd = row.get("usd")
                if isinstance(usd, (int, float)) and math.isfinite(usd) and usd >= 0:
                    self._sessions[sid] = {
                        "usd": float(usd),
                        "tmux": row.get("tmux") if isinstance(row.get("tmux"), str) else None,
                        "seen": float(row.get("seen") or 0),
                    }
        days = raw.get("days") if isinstance(raw.get("days"), dict) else {}
        for day, bucket in days.items():
            if isinstance(day, str) and isinstance(bucket, dict):
                self._days[day] = {
                    k: float(v)
                    for k, v in bucket.items()
                    if isinstance(k, str) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
                }

    def _persist_locked(self) -> None:
        text = json.dumps(
            {"version": COSTS_FILE_VERSION, "sessions": self._sessions, "days": self._days}, sort_keys=True, separators=(",", ":")
        )
        atomic_write(self._path, text, "session costs")

    def _prune_locked(self, now: float) -> None:
        cutoff = _day(now - KEEP_DAYS * 86_400)
        for day in [d for d in self._days if d < cutoff]:
            del self._days[day]
        stale = [sid for sid, row in self._sessions.items() if now - row["seen"] > FORGET_SESSION_SECONDS]
        for sid in stale:
            del self._sessions[sid]
        if len(self._sessions) > MAX_SESSIONS:
            for sid, _ in sorted(self._sessions.items(), key=lambda kv: kv[1]["seen"])[: len(self._sessions) - MAX_SESSIONS]:
                del self._sessions[sid]

    def record(self, session_id: str, total_usd: float, tmux_session: str | None) -> bool:
        """Book the increase since the last total seen. True when anything was written."""
        if tmux_session is not None and not TMUX_NAME_RE.match(tmux_session):
            tmux_session = None
        now = self._clock()
        with self._lock:
            row = self._sessions.get(session_id)
            prior = row["usd"] if row else 0.0
            delta = total_usd - prior if total_usd >= prior else total_usd
            renamed = tmux_session is not None and (row is None or row.get("tmux") != tmux_session)
            if delta <= 0 and not renamed and row is not None:
                row["seen"] = now  # in memory only: a refresh with no new spend never writes the file
                return False
            self._sessions[session_id] = {
                "usd": total_usd,
                "tmux": tmux_session if tmux_session is not None else (row or {}).get("tmux"),
                "seen": now,
            }
            if delta > 0:
                bucket = self._days.setdefault(_day(now), {})
                bucket[session_id] = round(bucket.get(session_id, 0.0) + delta, 6)
            self._prune_locked(now)
            self._persist_locked()
            return True

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            changed = False
            for row in self._sessions.values():
                if row.get("tmux") == old:
                    row["tmux"] = new
                    changed = True
            if changed:
                self._persist_locked()

    def _tmux_of_locked(self, sid: str) -> str | None:
        row = self._sessions.get(sid)
        return row.get("tmux") if row else None

    def fixer_spend_today(self) -> float:
        today = _day(self._clock())
        with self._lock:
            bucket = self._days.get(today, {})
            return round(sum(usd for sid, usd in bucket.items() if is_fixer(self._tmux_of_locked(sid))), 6)

    def summary(self, mission_of: Callable[[str], str | None] | None = None, *, week_days: int = 7) -> dict:
        """Today and the last `week_days` days: totals, per rack session, per mission, and fixers vs the cap."""
        now = self._clock()
        today = _day(now)
        week_floor = _day(now - (week_days - 1) * 86_400)
        with self._lock:
            per_session: dict[str, dict[str, float]] = {}
            for day, bucket in self._days.items():
                if day < week_floor:
                    continue
                for sid, usd in bucket.items():
                    name = self._tmux_of_locked(sid) or f"agent:{sid[:8]}"
                    row = per_session.setdefault(name, {"today": 0.0, "week": 0.0})
                    row["week"] += usd
                    if day == today:
                        row["today"] += usd
        missions: dict[str, dict[str, float]] = {}
        if mission_of is not None:
            for name, row in per_session.items():
                mission = mission_of(name)
                if mission:
                    bucket = missions.setdefault(mission, {"today": 0.0, "week": 0.0})
                    bucket["today"] += row["today"]
                    bucket["week"] += row["week"]
        rounded = lambda m: {k: round(v, 4) for k, v in m.items()}  # noqa: E731
        sessions = sorted(
            ({"session": name, **rounded(row)} for name, row in per_session.items()),
            key=lambda r: (-r["today"], -r["week"], r["session"]),
        )
        fixer_today = round(sum(r["today"] for r in sessions if is_fixer(r["session"])), 4)
        return {
            "day": today,
            "today_usd": round(sum(r["today"] for r in sessions), 4),
            "week_usd": round(sum(r["week"] for r in sessions), 4),
            "sessions": sessions,
            "missions": [{"mission": mid, **rounded(row)} for mid, row in sorted(missions.items())],
            "fixers": {"today_usd": fixer_today, "cap_usd": fix_cap_usd(), "over_cap": fixer_today >= fix_cap_usd()},
        }


def resolve_tmux_session(
    transcript_path: str | None, bound: Mapping[str, str], normalize: Callable[[object], str | None]
) -> str | None:
    """The rack session bound to this transcript (`TranscriptTracker.paths()`), else None."""
    real = normalize(transcript_path)
    if real is None:
        return None
    matches: Iterable[str] = (session for session, path in bound.items() if path == real)
    return next(iter(sorted(matches)), None)
