"""rack.sqlite — the decision inbox and the LEAD runs, in one file.

ONE FILE ON PURPOSE. Answering a decision and reopening the steps built on its assumption must
commit together (inbox.py `answer`): two files would leave a window where the operator's answer is
stored and the work it contradicts still reads as verified. So `decisions` (rung 2's inbox) and the
`lead_*` tables (lead/runs.py) share this database and its migration ladder.

WHERE. `rack.sqlite` in the `rack` store (paths.py), because these rows are the operator's decisions
and the operator's plans: kept data, never state. `TLDRACK_RACK_DB` points tests at a scratch file.

HOW. A fresh connection per operation (rack_metrics.py's shape: WAL, short timeout, nothing held
across requests), the schema versioned by `PRAGMA user_version` (ledger/store.py's ladder), the file
0600 inside a 0700 directory. Writes go through `write()`, which takes a process lock and
`BEGIN IMMEDIATE`, so a read-modify-write never interleaves with another thread's.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tldrack import paths

PATH_ENV = "TLDRACK_RACK_DB"
DB_NAME = "rack.sqlite"

_SCHEMA_V1 = """
CREATE TABLE decisions (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    session TEXT,
    session_lineage TEXT,
    run_id TEXT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    options TEXT,
    opened_at REAL NOT NULL,
    defer_until REAL,
    blocking INTEGER NOT NULL,
    assumption TEXT,
    state TEXT NOT NULL,
    answer TEXT,
    answer_kind TEXT,
    contradicts INTEGER,
    answered_at REAL,
    answered_via TEXT,
    resurfaced_at REAL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX decisions_state ON decisions(state, run_id);
CREATE INDEX decisions_fingerprint ON decisions(run_id, fingerprint);
CREATE TABLE decision_steps (
    decision_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    PRIMARY KEY (decision_key, step_id)
);
CREATE TABLE lead_runs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    plan_path TEXT,
    plan_sha256 TEXT,
    lead_identity TEXT NOT NULL,
    lead_session TEXT,
    profile TEXT,
    group_id TEXT,
    start_sha TEXT,
    parallel_max INTEGER NOT NULL,
    spawn_cap INTEGER NOT NULL,
    spawns INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    ended_at REAL,
    report TEXT
);
CREATE TABLE lead_steps (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    kind TEXT NOT NULL,
    lane TEXT NOT NULL,
    after TEXT NOT NULL,
    state TEXT NOT NULL,
    branch TEXT,
    -- crew_session, crew_lineage: renamed by V3 (step 6.8)
    crew_session TEXT,
    crew_lineage TEXT,
    profile TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    report_path TEXT,
    report_sha256 TEXT,
    observed TEXT NOT NULL DEFAULT '{}',
    closed_at REAL,
    close_facts TEXT,
    PRIMARY KEY (run_id, step_id)
);
-- renamed by V3 (step 6.8)
CREATE TABLE crew_questions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    session TEXT,
    text TEXT NOT NULL,
    blocking INTEGER NOT NULL,
    assumption TEXT,
    opened_at REAL NOT NULL,
    state TEXT NOT NULL,
    resolution TEXT,
    source TEXT,
    decision_key TEXT
);
CREATE TABLE lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    step_id TEXT,
    detail TEXT NOT NULL,
    wakes INTEGER NOT NULL,
    woken_at REAL,
    delivered_at REAL
);
CREATE TABLE lead_sessions (
    identity TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    profile TEXT,
    launched_at REAL NOT NULL
);
CREATE INDEX lead_events_pending ON lead_events(run_id, delivered_at);
CREATE TABLE typed_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    lineage TEXT,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    queued_at REAL NOT NULL,
    delivered_at REAL,
    dropped TEXT
);
"""

#: The pair a run started with: owned by lead/models.py — see the note there. Renamed by V3 (step 6.8).
_SCHEMA_V2 = """
ALTER TABLE lead_runs ADD COLUMN lead_model TEXT;
ALTER TABLE lead_runs ADD COLUMN crew_model TEXT;
"""

#: The rack's words (step 6.8): a worker's columns, its questions' table and the step kinds take the names people
#: read. V1 and V2 above keep the names they shipped with, because a ladder replays history. The `observed` facts
#: (lead/watcher.py) are a JSON object whose keys carry the same prefix; `close_facts` is a record and stays as written.
_SCHEMA_V3 = """
ALTER TABLE lead_steps RENAME COLUMN crew_session TO worker_session;
ALTER TABLE lead_steps RENAME COLUMN crew_lineage TO worker_lineage;
ALTER TABLE lead_runs RENAME COLUMN crew_model TO worker_model;
ALTER TABLE crew_questions RENAME TO worker_questions;
-- step 6.8: the stored kinds and the observed facts' keys
UPDATE lead_steps SET kind = 'change' WHERE kind = 'ship';
UPDATE lead_steps SET kind = 'report' WHERE kind = 'scout';
UPDATE lead_steps SET observed = replace(observed, '"crew_', '"worker_');
"""

#: The repository a run leads: owned by lead/service.py `_resolve_repo` — see the note there. NULL (a run started
#: before V4) reads as the worktree plugin's first repository, which is what every such run used.
_SCHEMA_V4 = """
ALTER TABLE lead_runs ADD COLUMN repo TEXT;
"""

#: Index i holds the script that takes the schema from version i to i + 1.
_SCHEMA_V5 = """
ALTER TABLE lead_runs ADD COLUMN base TEXT NOT NULL DEFAULT 'main';
"""

MIGRATIONS: tuple[str, ...] = (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4, _SCHEMA_V5)
SCHEMA_VERSION = len(MIGRATIONS)


def resolve_rack_db_path() -> Path:
    env = os.environ.get(PATH_ENV)
    return Path(env) if env else paths.store_dir("rack") / DB_NAME


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for step in range(version, SCHEMA_VERSION):
        # One transaction per rung, the version bump inside it: a rung that fails leaves the file as it was.
        conn.executescript(f"BEGIN;\n{MIGRATIONS[step]}\nPRAGMA user_version = {step + 1};\nCOMMIT;")


class RackDb:
    """The file, opened per operation. `read()` for a query, `write()` for a transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._ready = False

    def _open(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._ready:
            with self._lock:
                migrate(conn)
                for suffix in ("", "-wal", "-shm"):
                    side = Path(str(self.path) + suffix)
                    if side.exists():
                        side.chmod(0o600)
                self._ready = True
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One transaction: committed when the block returns, rolled back when it raises."""
        with self._lock:
            conn = self._open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
