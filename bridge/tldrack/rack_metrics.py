"""Rack interaction history. Local SQLite, separate from transcripts and model-review labels.

Clicks are explicit select intents, including re-clicking the selected row; a payload refresh or
automatic focus never counts. Revisits count selections after the first recorded selection of
that conversation (not just this report's date range), excluding reselects. Conversation ids from
SessionRecordStore survive renames; unbound shells fall back to their tmux name and are reported
as unattributed. Reusing an unbound name cannot be distinguished from revisiting it.

Exposure is foreground *pane visibility*, not reading, engagement, or productivity. The browser
reports <=20 s intervals; overlapping windows are unioned, and midnight intervals are split.
There is no historical backfill or inferred task-success metric. Schema version and dated
experiment markers make comparisons explicit. Keep raw events indefinitely for later questions.
Only bounded metadata crosses this boundary: never prompts, paths, terminal contents or keys.

── Attention signals: what interrupted the operator, and what was held back ─────
Clicks measure what the operator DID; signals measure what the bridge ASKED of them, so a change to
the attention policy (attention.py, stuck.py, notify.py) can be proven to save attention rather
than asserted to. The bridge records them itself (`RackMetrics.signal`, never from a browser):
`INTERRUPTIONS` are the signals that cost the operator a look or a buzz, `AVOIDED` the ones the
policy held back that the old rules would have raised. A day's `interruptions` and `avoided` are
those sums. Same database, same PAUSE switch, same "no text" rule: a signal is a name, a reason
from a closed set, a session name and a count.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from collections.abc import Callable

from tldrack import paths

METRICS_FILE = "rack-metrics.sqlite"
EVENT_FIELDS = {
    "id",
    "client",
    "at",
    "kind",
    "session",
    "state",
    "mark",
    "action",
    "source",
    "reselect",
    "duration_ms",
    "rack_size",
    "context_left",
    "model",
    "profile",
}
ACTIONS = {
    "select",
    "kill",
    "rename",
    "new",
    "worktree",
    "pr",
    "cleanup",
    "dismiss_open_work",
    "snooze_open_work",
    "confirm_open_work",
    "reopen",
    "group_control",
    "exposure",
    "lead",
}
STATES = {"busy", "attention", "background", "done", "free", "unknown"}
MARKS = {"open", "done", "background", "reply", "none"}
#: Bridge-recorded attention signals (module docstring § Attention signals). Every name is in
#: exactly one of the two sets that sum into a day, or in neither (a denominator).
INTERRUPTIONS = frozenset({"warning_new", "ping_sent", "stuck_flagged", "digest_sent"})
AVOIDED = frozenset({"warning_repeat", "warning_retired", "warning_held", "ping_held"})
DENOMINATORS = frozenset({"wait_started", "turn_end", "warning_snoozed", "warning_dismissed", "warning_confirmed"})
SIGNALS = INTERRUPTIONS | AVOIDED | DENOMINATORS
#: Why a signal was raised or held: a ping's kind or its suppression, a stuck flag's shape.
SIGNAL_REASONS = frozenset(
    {
        "wait",
        "stuck",
        "digest",
        "event",
        "test",
        "presence",
        "cooldown",
        "budget",
        "episode",
        "repeat_failure",
        "no_progress",
        "background",
        "children",
    }
)


def resolve_metrics_path() -> Path:
    return Path(os.environ.get("TLDRACK_METRICS_PATH") or paths.state_file(METRICS_FILE))


def _number(value: object, lo: float, hi: float, *, integer: bool = False) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi and (not integer or int(value) == value)


#: Legacy (step 6.8): `rack_size` was `fleet_size` before 0.1.0, in an event a page one release behind posts and in
#: the column of an older file (`RackMetrics._db` renames it in place).
LEGACY_EVENT_FIELDS = {"fleet_size": "rack_size"}


def validate_event(raw: object, now_ms: int) -> dict:
    if isinstance(raw, dict) and not set(raw) & set(LEGACY_EVENT_FIELDS.values()):
        raw = {LEGACY_EVENT_FIELDS.get(key, key): value for key, value in raw.items()}
    if not isinstance(raw, dict) or set(raw) != EVENT_FIELDS:
        raise ValueError("Invalid metric fields")
    try:
        for key in ("id", "client"):
            if not isinstance(raw[key], str) or str(uuid.UUID(raw[key])) != raw[key]:
                raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Invalid metric id") from None
    if not _number(raw["at"], now_ms - 86_400_000, now_ms + 60_000, integer=True):
        raise ValueError("Metric timestamp outside the one-day delivery window")
    enums = {
        "kind": {"select", "exposure", "action"},
        "state": STATES,
        "mark": MARKS,
        "action": ACTIONS,
        "source": {"window", "embedded"},
    }
    if any(not isinstance(raw[k], str) or raw[k] not in allowed for k, allowed in enums.items()):
        raise ValueError("Invalid metric category")
    if raw["session"] is not None and (
        not isinstance(raw["session"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw["session"]) is None
    ):
        raise ValueError("Invalid metric session")
    if (
        type(raw["reselect"]) is not bool
        or not _number(raw["duration_ms"], 0, 20_000, integer=True)
        or not _number(raw["rack_size"], 0, 10_000, integer=True)
        or (raw["context_left"] is not None and not _number(raw["context_left"], 0, 100))
    ):
        raise ValueError("Invalid metric measurement")
    for key, cap in (("model", 160), ("profile", 80)):
        if raw[key] is not None and (not isinstance(raw[key], str) or len(raw[key]) > cap):
            raise ValueError("Invalid metric label")
    if (
        (raw["kind"] in ("select", "exposure") and raw["session"] is None)
        or (raw["kind"] == "select" and raw["action"] != "select")
        or (raw["kind"] == "exposure" and (raw["action"] != "exposure" or raw["duration_ms"] == 0))
        or (raw["kind"] != "exposure" and raw["duration_ms"] != 0)
        or (raw["kind"] != "select" and raw["reselect"])
    ):
        raise ValueError("Inconsistent metric event")
    return raw


def _totals() -> dict:
    return dict.fromkeys(
        (
            "clicks",
            "sessions",
            "revisits",
            "reselects",
            "working_clicks",
            "attention_clicks",
            "warning_clicks",
            "done_clicks",
            "identified_clicks",
            "visible_ms",
            "interruptions",
            "avoided",
        ),
        0,
    )


class RackMetrics:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._renamed = False

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens it; do not chmod the shared parent directory.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, at INTEGER NOT NULL, received_at INTEGER NOT NULL,
                    client TEXT NOT NULL, kind TEXT NOT NULL, session TEXT, session_key TEXT,
                    identified INTEGER NOT NULL, state TEXT NOT NULL, mark TEXT NOT NULL,
                    action TEXT NOT NULL, source TEXT NOT NULL, reselect INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, rack_size INTEGER NOT NULL,
                    context_left REAL, model TEXT, profile TEXT, version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS events_at ON events(at);
                CREATE INDEX IF NOT EXISTS events_session ON events(session_key, kind, at);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS markers (id TEXT PRIMARY KEY, at INTEGER NOT NULL, label TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, at INTEGER NOT NULL, session TEXT, session_key TEXT,
                    signal TEXT NOT NULL, reason TEXT, count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS signals_at ON signals(at);
                INSERT OR IGNORE INTO settings VALUES ('enabled', '1');
            """)
            if not self._renamed:
                columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
                for old, new in LEGACY_EVENT_FIELDS.items():
                    if old in columns and new not in columns:
                        db.execute(f"ALTER TABLE events RENAME COLUMN {old} TO {new}")
                self._renamed = True
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.path) + suffix)
                if side.exists():
                    side.chmod(0o600)
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, raw: object, record_of: Callable[[str], dict | None] = lambda _: None) -> dict:
        now = round(self.clock() * 1000)
        event = validate_event(raw, now)
        record = record_of(event["session"]) if event["session"] else None
        agent_id = (record or {}).get("agent_session_id")
        key = f"agent:{(record or {}).get('kind', '')}:{agent_id}" if agent_id else f"tmux:{event['session']}"
        with self._db() as db:
            enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] == "1"
            if not enabled:
                return {"ok": True, "recorded": False, "enabled": False}
            values = (
                event["id"],
                event["at"],
                now,
                event["client"],
                event["kind"],
                event["session"],
                key,
                int(bool(agent_id)),
                event["state"],
                event["mark"],
                event["action"],
                event["source"],
                int(event["reselect"]),
                event["duration_ms"],
                event["rack_size"],
                event["context_left"],
                event["model"],
                event["profile"],
            )
            written = db.execute("INSERT OR IGNORE INTO events VALUES (" + ",".join("?" for _ in values) + ",1)", values)
            return {"ok": True, "recorded": written.rowcount == 1, "enabled": True}

    def signal(
        self,
        signal: str,
        session: str | None = None,
        *,
        reason: str | None = None,
        count: int = 1,
        record_of: Callable[[str], dict | None] = lambda _: None,
    ) -> bool:
        """Record one attention signal the bridge raised or held back; True when written. Never
        raises: a hook, a ping or a verdict must not fail because the telemetry did."""
        try:
            if (
                signal not in SIGNALS
                or (reason is not None and reason not in SIGNAL_REASONS)
                or type(count) is not int
                or not 1 <= count <= 10_000
                or (
                    session is not None
                    and (not isinstance(session, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session) is None)
                )
            ):
                return False
            record = record_of(session) if session else None
            agent_id = (record or {}).get("agent_session_id")
            key = f"agent:{(record or {}).get('kind', '')}:{agent_id}" if agent_id else f"tmux:{session}" if session else None
            with self._db() as db:
                if db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] != "1":
                    return False
                db.execute(
                    "INSERT INTO signals VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), round(self.clock() * 1000), session, key, signal, reason, count),
                )
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def configure(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("Invalid metrics setting")
        now = round(self.clock() * 1000)
        with self._db() as db:
            if set(raw) == {"enabled"} and type(raw["enabled"]) is bool:
                db.execute("UPDATE settings SET value=? WHERE key='enabled'", ("1" if raw["enabled"] else "0",))
                label = "Recording resumed" if raw["enabled"] else "Recording paused"
            elif set(raw) == {"label"} and isinstance(raw["label"], str) and 1 <= len(raw["label"].strip()) <= 120:
                label = raw["label"].strip()
            else:
                raise ValueError("Use enabled:boolean or a marker label of 1–120 characters")
            db.execute("INSERT INTO markers VALUES (?,?,?)", (str(uuid.uuid4()), now, label))
        return {"ok": True, "message": label}

    def summary(self, start: str | None = None, end: str | None = None) -> dict:
        today = datetime.fromtimestamp(self.clock()).date()
        try:
            first = date.fromisoformat(start) if start else today - timedelta(days=29)
            last = date.fromisoformat(end) if end else today
        except (ValueError, TypeError):
            raise ValueError("Use YYYY-MM-DD dates") from None
        if not 0 <= (last - first).days < 90 or last > today:
            raise ValueError("Choose 1–90 days ending no later than today")
        lo = round(datetime.combine(first, day_time()).timestamp() * 1000)
        hi = round(datetime.combine(last + timedelta(days=1), day_time()).timestamp() * 1000)
        days = {str(first + timedelta(days=i)): _totals() for i in range((last - first).days + 1)}
        observed: set[str] = set()
        total = _totals()
        sessions: dict[str, dict] = {}
        seen: dict[str, set] = {"total": set()}
        ends: dict[str, int] = {}
        groups: dict[str, dict[str, int]] = {name: {} for name in ("action", "state", "mark", "source")}

        def expose(bucket: dict, scope: str, begin: int, finish: int):
            bucket["visible_ms"] += max(0, finish - max(begin, ends.get(scope, begin)))
            ends[scope] = max(finish, ends.get(scope, finish))

        with self._db() as db:
            enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] == "1"
            bounds = db.execute("SELECT MIN(at), MAX(at) FROM events").fetchone()
            # First-ever selection for each conversation, using the indexed session/kind/time key.
            first_click = {
                r[0]: r[1] for r in db.execute("SELECT session_key, MIN(at) FROM events WHERE kind='select' GROUP BY session_key")
            }
            for row in db.execute(
                """SELECT * FROM events WHERE at>=? AND at<?
                    ORDER BY (at-duration_ms),id""",
                (lo, hi + 20_000),
            ):
                at, key = row["at"], row["session_key"]
                if row["kind"] != "exposure" and at >= hi:
                    continue
                if row["kind"] == "action":
                    observed.add(str(datetime.fromtimestamp(at / 1000).date()))
                    action = row["action"]
                    groups["action"][action] = groups["action"].get(action, 0) + 1
                    continue
                session = sessions.setdefault(
                    key, {**_totals(), "key": key, "session": row["session"], "model": row["model"], "profile": row["profile"]}
                )
                session.update(session=row["session"], model=row["model"], profile=row["profile"])
                if row["kind"] == "exposure":
                    begin, finish = max(lo, at - row["duration_ms"]), min(hi, at)
                    if finish <= begin:
                        continue
                    expose(total, "total", begin, finish)
                    expose(session, key, begin, finish)
                    while begin < finish:
                        day = datetime.fromtimestamp(begin / 1000).date()
                        observed.add(str(day))
                        midnight = round(datetime.combine(day + timedelta(days=1), day_time()).timestamp() * 1000)
                        stop = min(finish, midnight)
                        expose(days[str(day)], "day:" + str(day), begin, stop)
                        begin = stop
                    continue
                day = str(datetime.fromtimestamp(at / 1000).date())
                observed.add(day)
                seen.setdefault(day, set()).add(key)
                seen["total"].add(key)
                for bucket in (total, days[day], session):
                    bucket["clicks"] += 1
                    bucket["reselects"] += row["reselect"]
                    bucket["revisits"] += int(at > first_click[key] and not row["reselect"])
                    bucket["working_clicks"] += int(row["state"] in ("busy", "background"))
                    bucket["attention_clicks"] += int(row["state"] == "attention")
                    bucket["warning_clicks"] += int(row["mark"] == "open")
                    bucket["done_clicks"] += int(row["mark"] == "done")
                    bucket["identified_clicks"] += row["identified"]
                for field in ("state", "mark", "source"):
                    groups[field][row[field]] = groups[field].get(row[field], 0) + 1
            markers = [dict(r) for r in db.execute("SELECT at,label FROM markers WHERE at>=? AND at<? ORDER BY at", (lo, hi))]
            signal_counts: dict[tuple[str, str | None], int] = {}
            for row in db.execute(
                "SELECT at, session, session_key, signal, reason, count FROM signals WHERE at>=? AND at<?", (lo, hi)
            ):
                day = str(datetime.fromtimestamp(row["at"] / 1000).date())
                observed.add(day)
                field = "interruptions" if row["signal"] in INTERRUPTIONS else "avoided" if row["signal"] in AVOIDED else None
                if field is not None:
                    buckets = [total, days[day]]
                    if row["session_key"]:
                        # A session that only ever interrupted (never clicked) is the one worth seeing.
                        buckets.append(
                            sessions.setdefault(
                                row["session_key"],
                                {
                                    **_totals(),
                                    "key": row["session_key"],
                                    "session": row["session"],
                                    "model": None,
                                    "profile": None,
                                },
                            )
                        )
                    for bucket in buckets:
                        bucket[field] += row["count"]
                pair = (row["signal"], row["reason"])
                signal_counts[pair] = signal_counts.get(pair, 0) + row["count"]
        total["sessions"] = len(seen["total"])
        for day, bucket in days.items():
            bucket["sessions"] = len(seen.get(day, set()))
        for bucket in sessions.values():
            bucket["sessions"] = int(bucket["clicks"] > 0)
        for bucket in (total, *days.values(), *sessions.values()):
            bucket["clicks_per_session"] = round(bucket["clicks"] / bucket["sessions"], 2) if bucket["sessions"] else None
            bucket["clicks_per_hour"] = (
                round(bucket["clicks"] * 3_600_000 / bucket["visible_ms"], 2) if bucket["visible_ms"] else None
            )
        return {
            "version": 1,
            "enabled": enabled,
            "started_at": bounds[0],
            "last_event_at": bounds[1],
            "path": str(self.path),
            "timezone": time.tzname[0],
            "from": str(first),
            "to": str(last),
            "total": total,
            "days": [{"day": d, "observed": d in observed, **b} for d, b in days.items()],
            "sessions": sorted(sessions.values(), key=lambda s: (-s["clicks"], s["session"])),
            **{
                f"{field}s": [{field: key, "count": count} for key, count in sorted(values.items())]
                for field, values in groups.items()
            },
            "markers": markers,
            "signals": [
                {"signal": name, "reason": reason, "count": count}
                for (name, reason), count in sorted(signal_counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
            ],
        }


#: Signals waiting for the writer thread; past this the OLDEST drop. A stuck disk must not grow the
#: bridge's memory, and a lost signal is only telemetry.
MAX_PENDING_SIGNALS = 1024


class SignalQueue:
    """`RackMetrics.signal` off the hook path: callers `put` and return at once, one daemon thread
    writes in order. Hooks run on request threads a Claude shell is blocked on, and a SQLite open
    per signal is milliseconds that belong to nobody's turn. `worker=False` + `drain()` for tests."""

    def __init__(
        self, metrics: RackMetrics | None, record_of: Callable[[str], dict | None] = lambda _: None, *, worker: bool = True
    ) -> None:
        self._metrics = metrics
        self._record_of = record_of
        self._worker = worker
        self._lock = threading.Condition()
        self._pending: deque[tuple[str, str | None, str | None, int]] = deque(maxlen=MAX_PENDING_SIGNALS)
        self._thread: threading.Thread | None = None

    def put(self, signal: str, session: str | None = None, reason: str | None = None, count: int = 1) -> None:
        if self._metrics is None:
            return
        with self._lock:
            self._pending.append((signal, session, reason, count))
            if self._worker and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rack-signals", daemon=True)
                self._thread.start()
            self._lock.notify()

    def drain(self) -> int:
        written = 0
        while True:
            with self._lock:
                if not self._pending:
                    return written
                item = self._pending.popleft()
            written += self._write(item)

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._pending:
                    self._lock.wait()
                item = self._pending.popleft()
            self._write(item)

    def _write(self, item: tuple[str, str | None, str | None, int]) -> int:
        signal, session, reason, count = item
        assert self._metrics is not None
        return int(self._metrics.signal(signal, session, reason=reason, count=count, record_of=self._record_of))


def metrics_response(
    metrics: RackMetrics | None, route: str, *, body=None, query=None, record_of: Callable[[str], dict | None] = lambda _: None
) -> tuple[int, dict]:
    """Transport adapter: storage failure never affects an agent action or hook."""
    if metrics is None:
        return 503, {"error": "Rack metrics unavailable; restart the bridge"}
    try:
        if route == "event":
            return 200, metrics.record(body, record_of)
        if route == "settings":
            return 200, metrics.configure(body)
        query = query or {}
        return 200, metrics.summary((query.get("from") or [None])[0], (query.get("to") or [None])[0])
    except ValueError as err:
        return 400, {"error": str(err)}
    except (OSError, sqlite3.Error):
        return 503, {"error": "Rack metrics database unavailable; check local disk access"}
