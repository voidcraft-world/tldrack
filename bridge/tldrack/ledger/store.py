"""Where the session ledger lives, and its SQLite schema. Why the ledger exists: `__init__.py`.

The folder is the `ledger` store (paths.py; env `TLDRACK_LEDGER_DIR` wins, for tests). An operator
who wants it on another disk maps that store in `paths.json`.

── SCHEMA ──────────────────────────────────────────────────────────────────────
`PRAGMA user_version` is the schema version; `migrate` walks it forward and never back, one block per
version. Every table that holds a conversation's rows is keyed by `file_id`, the conversation FILE, not
its session id: a subagent shares its parent's session id, and a file is the unit the fold re-derives
whole. (A Claude RESUME is a new file under a NEW session id — each main file's id is its file stem —
so one conversation can span files; `laya_links.thread_files` owns how they are joined back up.) Times
are epoch seconds (REAL), so `latency_s` is plain subtraction in SQL.

v1: `files`, `sessions`, `turns`, `messages`, `events`, `tool_calls`, `bridge_rows`, `ingest_runs`,
    and `messages_fts` where this sqlite has FTS5.
v2: `laya_links` — each Laya verdict's conversation, file and closing message. Derived and rebuildable:
    `laya_links.py` owns how a verdict is matched and why.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from tldrack import paths

LEDGER_DIR_ENV = "TLDRACK_LEDGER_DIR"
DB_NAME = "ledger.sqlite"
RAW_DIRNAME = "raw"
LOCK_NAME = ".ingest.lock"
#: The child's progress, read by `/ledger/status` while an ingest runs.
PROGRESS_NAME = "ingest-progress.json"

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,          -- claude | codex | bridge
    profile TEXT,                  -- the launch profile whose root holds it
    role TEXT NOT NULL,            -- transcript | sidecar | side_ledger
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    head_hash TEXT,
    archive_path TEXT,
    archived_bytes INTEGER NOT NULL DEFAULT 0,
    archive_size INTEGER NOT NULL DEFAULT 0,
    archive_generation INTEGER NOT NULL DEFAULT 0,
    read_bytes INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_ingest REAL,
    gone_at REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    profile TEXT,
    session_id TEXT,
    kind TEXT NOT NULL,            -- main | subagent | workflow | headless | auto_review
    parent_session_id TEXT,
    agent_id TEXT,
    agent_name TEXT,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    cli_version TEXT,
    title TEXT,
    first_prompt TEXT,
    pr_url TEXT,
    started_at REAL,
    ended_at REAL,
    turns INTEGER NOT NULL DEFAULT 0,
    human_turns INTEGER NOT NULL DEFAULT 0,
    human_events INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    compactions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS turns (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    origin TEXT NOT NULL,          -- human | command | task | peer | sdk | system
    prompt_ts REAL,
    end_ts REAL,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    interrupted INTEGER NOT NULL DEFAULT 0,
    closing_text TEXT,
    PRIMARY KEY (file_id, idx)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts REAL,
    role TEXT NOT NULL,            -- human | agent | machine | peer | parent (a subagent's spawner)
    turn_idx INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_file ON messages(file_id, seq);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ts REAL,
    actor TEXT NOT NULL,           -- human | machine
    kind TEXT NOT NULL,            -- prompt | command | interrupt | permission | question | plan | auto_review | auto_block
    decision TEXT,                 -- accept | reject | answer | allow | deny | NULL
    turn_idx INTEGER,
    tool TEXT,
    summary TEXT,
    text TEXT,
    scope TEXT,
    latency_s REAL,                -- how long the agent had been waiting on this human
    context TEXT,                  -- what the human was answering: the agent's last words
    data TEXT                      -- kind-specific JSON (questions + answers, review verdict)
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_file ON events(file_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts REAL,
    turn_idx INTEGER,
    tool TEXT NOT NULL,
    pattern TEXT,
    summary TEXT,
    decision TEXT,                 -- accept | reject (Claude's permissionDecision)
    decision_source TEXT,          -- config | user_temporary | user_permanent | user_reject | …
    reason_type TEXT,              -- classifier | mode | rule | …
    denial_kind TEXT,              -- user-rejected | automode-blocked | permission-rule
    is_error INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, seq)
);
CREATE INDEX IF NOT EXISTS tool_calls_source ON tool_calls(decision_source);

CREATE TABLE IF NOT EXISTS bridge_rows (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- permissions | decisions | decision-judge | laya:<surface> | laya-operator | rack-activity | claude-history | codex-history
    row_key TEXT NOT NULL,
    ts REAL,
    session TEXT,
    kind TEXT,
    data TEXT NOT NULL,
    UNIQUE (source, row_key)
);
CREATE INDEX IF NOT EXISTS bridge_rows_ts ON bridge_rows(source, ts);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    bytes_read INTEGER NOT NULL DEFAULT 0,
    bytes_archived INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
"""

#: Full-text search over messages, where this sqlite has FTS5 (an external-content table, so the
#: text is stored once). Without FTS5 `search` falls back to LIKE.
_FTS_V1 = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

#: One row per Laya verdict (`decision_id`), written by `laya_links.link_laya_decisions` at ingest.
#: `generation` is the linked file's `archive_generation` when the link was made: a rewritten file
#: bumps it, and its links are redone.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS laya_links (
    decision_id TEXT PRIMARY KEY,
    state_key TEXT,                -- state_hash[:16]: the REVIEW key a deck card carries
    file_id INTEGER,
    seq INTEGER,                   -- the closing message's messages.seq
    conversation TEXT,             -- the linked file's session id
    thread TEXT,                   -- the session id the conversation's first file ran under
    method TEXT NOT NULL CHECK (method IN ('id', 'state', 'tail', 'none')),
    at REAL,                       -- the verdict's own time
    linked_at REAL,                -- the last attempt
    generation INTEGER
);
CREATE INDEX IF NOT EXISTS laya_links_state_key ON laya_links(state_key);
CREATE INDEX IF NOT EXISTS laya_links_file ON laya_links(file_id);
"""


def ledger_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(LEDGER_DIR_ENV)
    return Path(override).expanduser() if override else paths.store_dir("ledger")


def ensure_private_dir(path: Path) -> None:
    """Create `path` 0700 (and tighten it if it exists looser). Operator text lives under it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o700)
    except OSError:
        pass


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS temp._fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(_SCHEMA_V1)
        if has_fts5(conn):
            conn.executescript(_FTS_V1)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    if version < 2:
        conn.executescript(_SCHEMA_V2)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


def fts_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
    return row is not None


def connect(root: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """The ledger database, migrated. `readonly` opens without creating anything (status reads)."""
    db = root / DB_NAME
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    ensure_private_dir(root)
    fresh = not db.exists()
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        try:
            db.chmod(0o600)
        except OSError:
            pass
    migrate(conn)
    return conn
