"""`tldrack ledger ingest`: find every conversation file and side ledger, archive what is new,
re-fold what changed, and say what the ledger holds. Why the ledger exists: `ledger/__init__.py`.

── WHAT COUNTS AS A SOURCE ─────────────────────────────────────────────────────
Every launch profile's root (`profiles.load_profiles`): a Claude account's `projects/`, Codex's
`sessions/` and `archived_sessions/`. A `~/.claude*` folder with a `projects/` that no profile names
is ingested too, under its folder name — an account used once by hand still held conversations.
Two profiles on one real folder are walked once. Symlinks are never followed: one Claude scratchpad
links back into the repo, and following it walked the same files under several names (a naive
walk reported many times the real size of the transcripts).

── PER FILE ────────────────────────────────────────────────────────────────────
Unchanged size + mtime → nothing. A transcript that grew → its new lines go to the archive
(`archive.append_lines`) and the WHOLE file is re-folded into its rows (one transaction per file;
re-deriving whole is what keeps a turn that spans two runs correct). A file that is gone keeps its
rows and archive and gets `gone_at` — that is the reaper, and outliving it is the point.

── ONE AT A TIME ───────────────────────────────────────────────────────────────
An `flock` on `.ingest.lock` makes a second ingest (the bridge's timer and a hand-run CLI) answer
"already running" instead of racing it. The running one writes `ingest-progress.json`.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tldrack.ledger import archive
from tldrack.ledger.claude_fold import fold_claude
from tldrack.ledger.codex_fold import fold_codex
from tldrack.ledger.fold import Fold
from tldrack.ledger.laya_links import link_laya_decisions
from tldrack.ledger.relay import mark_relayed_prompts
from tldrack.ledger.side_ledgers import SideLedger, parse_row, side_ledgers
from tldrack.ledger.store import LOCK_NAME, PROGRESS_NAME, RAW_DIRNAME, connect, ensure_private_dir

#: Claude Code's default `cleanupPeriodDays`.
DEFAULT_REAPER_DAYS = 30
#: A transcript this close to its account's reaper and not fully archived is AT RISK.
AT_RISK_MARGIN_DAYS = 5
SKIP_NAMES = frozenset({".DS_Store"})
PROGRESS_EVERY = 25

Log = Callable[[str], None]


@dataclass(frozen=True)
class Root:
    source: str  # claude | codex
    profile: str
    base: Path  # the account's config dir
    subdirs: tuple[str, ...]


class AlreadyRunning(RuntimeError):
    pass


# ── discovery ──────────────────────────────────────────────────────────────────


def discover_roots(profiles: list[tuple[str, str, str | None]], home: Path) -> list[Root]:
    """`profiles` is `[(id, kind, config_dir)]`. Pure apart from `is_dir`/`realpath`."""
    roots: list[Root] = []
    seen: set[str] = set()

    def add(source: str, profile: str, base: Path, subdirs: tuple[str, ...]) -> None:
        if not any((base / d).is_dir() for d in subdirs):
            return
        real = os.path.realpath(base)
        if real in seen:
            return
        seen.add(real)
        roots.append(Root(source, profile, base, subdirs))

    for profile_id, kind, config_dir in profiles:
        if kind == "claude" and config_dir:
            add("claude", profile_id, Path(config_dir), ("projects",))
        elif kind == "codex" and config_dir:
            add("codex", profile_id, Path(config_dir), ("sessions", "archived_sessions"))
    try:
        extra = sorted(p for p in home.glob(".claude*") if p.is_dir())
    except OSError:
        extra = []
    for directory in extra:
        add("claude", directory.name.lstrip("."), directory, ("projects",))
    codex_default = home / ".codex"
    if codex_default.is_dir():
        add("codex", "codex", codex_default, ("sessions", "archived_sessions"))
    return roots


def load_roots() -> list[Root]:
    from tldrack.profiles import load_profiles

    registry = load_profiles()
    profiles = []
    for profile_id in registry.ids():
        profile = registry.get(profile_id)
        if profile is not None:
            profiles.append((profile.id, profile.kind, profile.config_dir))
    return discover_roots(profiles, Path.home())


def walk_root(root: Root) -> Iterator[tuple[Path, str, str]]:
    """(path, rel to the account dir, role) for every regular file under the root's subdirs."""
    for sub in root.subdirs:
        top = root.base / sub
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                if name in SKIP_NAMES or name.endswith(".tmp"):
                    continue
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                rel = str(path.relative_to(root.base))
                yield path, rel, role_of(root.source, rel)


def role_of(source: str, rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    if not parts[-1].endswith(".jsonl") or "tool-results" in parts:
        return "sidecar"
    if source == "codex" and not parts[-1].startswith("rollout-"):
        return "sidecar"
    return "transcript"


def reaper_days(config_dir: Path) -> int:
    """The account's `cleanupPeriodDays`, else Claude's default."""
    try:
        settings = json.loads((config_dir / "settings.json").read_text())
        value = settings.get("cleanupPeriodDays")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_REAPER_DAYS


# ── one file ───────────────────────────────────────────────────────────────────


def _file_row(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()


def _upsert_file(conn: sqlite3.Connection, path: Path, source: str, profile: str | None, role: str, now: float) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO files (path, source, profile, role, first_seen) VALUES (?, ?, ?, ?, ?)",
        (str(path), source, profile, role, now),
    )
    conn.execute("UPDATE files SET gone_at = NULL WHERE path = ? AND gone_at IS NOT NULL", (str(path),))
    row = _file_row(conn, path)
    assert row is not None
    return row


def _meta_for(path: Path) -> dict | None:
    sidecar = path.with_name(path.name[: -len(".jsonl")] + ".meta.json")
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        yield from handle


def fold_file(source: str, path: Path) -> Fold:
    if source == "codex":
        return fold_codex(_read_lines(path))
    return fold_claude(_read_lines(path), path=str(path), meta=_meta_for(path))


SESSION_COLUMNS = (
    "source",
    "profile",
    "session_id",
    "kind",
    "parent_session_id",
    "agent_id",
    "agent_name",
    "cwd",
    "git_branch",
    "model",
    "cli_version",
    "title",
    "first_prompt",
    "pr_url",
    "started_at",
    "ended_at",
    "turns",
    "human_turns",
    "human_events",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "compactions",
)
EVENT_COLUMNS = (
    "ts",
    "actor",
    "kind",
    "decision",
    "turn_idx",
    "tool",
    "summary",
    "text",
    "scope",
    "latency_s",
    "context",
    "data",
)
TOOL_COLUMNS = (
    "seq",
    "ts",
    "turn_idx",
    "tool",
    "pattern",
    "summary",
    "decision",
    "decision_source",
    "reason_type",
    "denial_kind",
    "is_error",
)


def write_fold(conn: sqlite3.Connection, file_id: int, profile: str | None, fold: Fold) -> None:
    for table in ("messages", "events", "tool_calls", "turns"):
        conn.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
    session = dict(fold.session)
    session["profile"] = profile
    values = [session.get(c) for c in SESSION_COLUMNS]
    for i, column in enumerate(SESSION_COLUMNS):
        if column in (
            "turns",
            "human_turns",
            "human_events",
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "compactions",
        ):
            values[i] = int(values[i] or 0)
    conn.execute(
        f"INSERT OR REPLACE INTO sessions (file_id, {', '.join(SESSION_COLUMNS)}) "
        f"VALUES (?, {', '.join('?' for _ in SESSION_COLUMNS)})",
        (file_id, *values),
    )
    conn.executemany(
        "INSERT INTO turns (file_id, idx, origin, prompt_ts, end_ts, tool_calls, interrupted, closing_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (file_id, t.idx, t.origin, t.prompt_ts, t.end_ts, t.tool_calls, int(t.interrupted), t.closing_text or None)
            for t in fold.turns
        ],
    )
    conn.executemany(
        "INSERT INTO messages (file_id, seq, ts, role, turn_idx, text) VALUES (?, ?, ?, ?, ?, ?)",
        [(file_id, m["seq"], m["ts"], m["role"], m["turn_idx"], m["text"]) for m in fold.messages],
    )
    conn.executemany(
        f"INSERT INTO events (file_id, {', '.join(EVENT_COLUMNS)}) VALUES (?, {', '.join('?' for _ in EVENT_COLUMNS)})",
        [(file_id, *[e.get(c) for c in EVENT_COLUMNS]) for e in fold.events],
    )
    conn.executemany(
        f"INSERT INTO tool_calls (file_id, {', '.join(TOOL_COLUMNS)}) VALUES (?, {', '.join('?' for _ in TOOL_COLUMNS)})",
        [(file_id, *[c.get(k) for k in TOOL_COLUMNS]) for c in fold.tool_calls],
    )


def ingest_conversation_file(
    conn: sqlite3.Connection, raw_root: Path, root: Root, path: Path, rel: str, role: str, now: float
) -> tuple[bool, int, int]:
    """Archive + re-fold one file if it changed. Returns (changed, bytes read, bytes archived)."""
    st = path.stat()
    row = _upsert_file(conn, path, root.source, root.profile, role, now)
    if row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns and row["last_ingest"] is not None:
        return False, 0, 0
    dest = archive.archive_path_for(raw_root, root.source, root.profile, rel)
    head = archive.head_hash(path, st.st_size) if st.st_size else ""
    generation = row["archive_generation"]
    archived = row["archived_bytes"]
    if role == "transcript":
        rewritten = (row["head_hash"] is not None and head != row["head_hash"]) or st.st_size < archived
        if rewritten:
            generation += 1
            archive.rotate_generation(dest, generation)
            archived = 0
        appended = archive.append_lines(path, dest, archived, st.st_size)
        archived += appended
    else:
        appended = archive.replace_whole(path, dest)
        archived = appended
    archive_size = dest.stat().st_size if dest.exists() else 0
    error = None
    if role == "transcript":
        try:
            write_fold(conn, row["id"], root.profile, fold_file(root.source, path))
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
    conn.execute(
        "UPDATE files SET size = ?, mtime_ns = ?, head_hash = ?, archive_path = ?, archived_bytes = ?, "
        "archive_size = ?, archive_generation = ?, read_bytes = ?, last_ingest = ?, error = ? WHERE id = ?",
        (
            st.st_size,
            st.st_mtime_ns,
            head,
            str(dest),
            archived,
            archive_size,
            generation,
            st.st_size if role == "transcript" else 0,
            now,
            error,
            row["id"],
        ),
    )
    return True, st.st_size, appended


def ingest_side_ledger(conn: sqlite3.Connection, ledger: SideLedger, now: float) -> tuple[bool, int, int]:
    """Read the rows appended since the last run (from 0 after a rotation). Returns (changed, bytes, rows)."""
    path = ledger.path
    st = path.stat()
    row = _upsert_file(conn, path, "bridge", ledger.source, "side_ledger", now)
    if row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns and row["last_ingest"] is not None:
        return False, 0, 0
    head = archive.head_hash(path, st.st_size) if st.st_size else ""
    start = row["read_bytes"]
    if head != row["head_hash"] or st.st_size < start:
        start = 0  # rotated or rewritten: re-read, the UNIQUE key drops what is already in
    inserted = 0
    consumed = start
    with path.open("rb") as handle:
        handle.seek(start)
        batch = []
        for line in handle:
            if not line.endswith(b"\n"):
                break  # the writer is mid-line; the next run starts here
            consumed += len(line)
            parsed = parse_row(ledger.source, line)
            if parsed is not None:
                batch.append((ledger.source, *parsed))
        if batch:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO bridge_rows (source, row_key, ts, session, kind, data) VALUES (?, ?, ?, ?, ?, ?)", batch
            )
            inserted = conn.total_changes - before
    conn.execute(
        "UPDATE files SET size = ?, mtime_ns = ?, head_hash = ?, read_bytes = ?, last_ingest = ?, error = NULL WHERE id = ?",
        (st.st_size, st.st_mtime_ns, head, consumed, now, row["id"]),
    )
    return True, consumed - start, inserted


# ── the run ────────────────────────────────────────────────────────────────────


class IngestLock:
    def __init__(self, root_dir: Path) -> None:
        self.path = root_dir / LOCK_NAME
        self._fd: int | None = None

    def __enter__(self) -> IngestLock:
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise AlreadyRunning(str(self.path)) from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def is_running(root_dir: Path) -> bool:
    try:
        with IngestLock(root_dir):
            return False
    except AlreadyRunning:
        return True
    except OSError:
        return False


def _write_progress(root_dir: Path, progress: dict) -> None:
    tmp = root_dir / (PROGRESS_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(progress))
        tmp.chmod(0o600)
        tmp.replace(root_dir / PROGRESS_NAME)
    except OSError:
        pass


def ingest(
    root_dir: Path,
    *,
    roots: list[Root] | None = None,
    side: list[SideLedger] | None = None,
    only: str | None = None,
    now: Callable[[], float] = time.time,
    log: Log | None = None,
) -> dict[str, Any]:
    """One full pass. `only` limits it to `claude`, `codex` or `bridge`. Raises AlreadyRunning."""
    say = log or (lambda _msg: None)
    ensure_private_dir(root_dir)
    raw_root = root_dir / RAW_DIRNAME
    ensure_private_dir(raw_root)
    with IngestLock(root_dir):
        roots = load_roots() if roots is None else roots
        if side is None:
            side = side_ledgers(
                [(r.profile, r.base) for r in roots if r.source == "claude"],
                [(r.profile, r.base) for r in roots if r.source == "codex"],
            )
        conn = connect(root_dir)
        started = now()
        # `note` names a partial run's scope: a full run is what last walked EVERY transcript.
        run_id = conn.execute(
            "INSERT INTO ingest_runs (started_at, note) VALUES (?, ?)", (started, f"only:{only}" if only else None)
        ).lastrowid
        conn.commit()
        totals = {"files_seen": 0, "files_changed": 0, "bytes_read": 0, "bytes_archived": 0, "errors": 0, "rows_inserted": 0}
        seen: set[str] = set()
        progress = {
            "run_id": run_id,
            "started_at": started,
            "files_seen": 0,
            "files_changed": 0,
            "current": None,
            "pid": os.getpid(),
        }
        try:
            for root in roots:
                if only and only != root.source:
                    continue
                say(f"ledger · {root.source}/{root.profile} ← {root.base}")
                for path, rel, role in walk_root(root):
                    seen.add(str(path))
                    totals["files_seen"] += 1
                    try:
                        changed, read, archived_bytes = ingest_conversation_file(conn, raw_root, root, path, rel, role, now())
                        conn.commit()
                    except (OSError, sqlite3.DatabaseError) as exc:
                        conn.rollback()
                        totals["errors"] += 1
                        say(f"ledger · ! {path}: {type(exc).__name__}: {exc}")
                        continue
                    if changed:
                        totals["files_changed"] += 1
                        totals["bytes_read"] += read
                        totals["bytes_archived"] += archived_bytes
                    if totals["files_seen"] % PROGRESS_EVERY == 0:
                        progress.update(
                            files_seen=totals["files_seen"],
                            files_changed=totals["files_changed"],
                            current=f"{root.source}/{root.profile}",
                        )
                        _write_progress(root_dir, progress)
            if not only or only == "bridge":
                for ledger in side:
                    seen.add(str(ledger.path))
                    try:
                        changed, read, inserted = ingest_side_ledger(conn, ledger, now())
                        conn.commit()
                    except (OSError, sqlite3.DatabaseError) as exc:
                        conn.rollback()
                        totals["errors"] += 1
                        say(f"ledger · ! {ledger.path}: {type(exc).__name__}: {exc}")
                        continue
                    if changed:
                        totals["files_changed"] += 1
                        totals["bytes_read"] += read
                        totals["rows_inserted"] += inserted
            if totals["files_changed"]:
                totals["relayed"] = mark_relayed_prompts(conn)
                conn.commit()
            if not only or only == "bridge":
                # Every pass, not only a changed one: an unmatched verdict is retried (laya_links.py owns why).
                try:
                    walked = (
                        started
                        if not only
                        else conn.execute(
                            "SELECT max(started_at) FROM ingest_runs WHERE finished_at IS NOT NULL AND note IS NULL"
                        ).fetchone()[0]
                    )
                    totals["laya_links"] = link_laya_decisions(conn, now(), settled_before=walked or 0.0)
                    conn.commit()
                except sqlite3.DatabaseError as exc:
                    conn.rollback()
                    totals["errors"] += 1
                    say(f"ledger · ! laya links: {type(exc).__name__}: {exc}")
            if only is None:
                # Files no source holds any more: the reaper (or a rotation) took them. Rows stay.
                stamp = now()
                gone = [(stamp, p) for (p,) in conn.execute("SELECT path FROM files WHERE gone_at IS NULL") if p not in seen]
                conn.executemany("UPDATE files SET gone_at = ? WHERE path = ?", gone)
                totals["files_gone"] = len(gone)
            conn.execute(
                "UPDATE ingest_runs SET finished_at = ?, files_seen = ?, files_changed = ?, bytes_read = ?, "
                "bytes_archived = ?, errors = ? WHERE id = ?",
                (
                    now(),
                    totals["files_seen"],
                    totals["files_changed"],
                    totals["bytes_read"],
                    totals["bytes_archived"],
                    totals["errors"],
                    run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
            try:
                (root_dir / PROGRESS_NAME).unlink()
            except OSError:
                pass
        totals["seconds"] = round(now() - started, 1)
        return totals


# ── status ─────────────────────────────────────────────────────────────────────


def _counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {str(k): int(v) for k, v in conn.execute(sql).fetchall()}


def ledger_status(
    root_dir: Path | None, *, roots: list[Root] | None = None, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    """Counts only — never a message, a prompt or a path inside a conversation. What the bridge serves."""
    if root_dir is None:
        return {"enabled": False, "reason": "no ledger directory (TLDRACK_LEDGER_DIR or the data directory)"}
    db = root_dir / "ledger.sqlite"
    status: dict[str, Any] = {
        "enabled": True,
        "dir": str(root_dir).replace(str(Path.home()), "~", 1),
        "running": is_running(root_dir) if root_dir.is_dir() else False,
    }
    try:
        status["progress"] = json.loads((root_dir / PROGRESS_NAME).read_text())
    except (OSError, ValueError):
        status["progress"] = None
    if not db.exists():
        status.update(ingested=False)
        return status
    conn = connect(root_dir, readonly=True)
    try:
        status["ingested"] = True
        status["db_bytes"] = db.stat().st_size + sum(p.stat().st_size for p in (root_dir / "ledger.sqlite-wal",) if p.exists())
        status["sessions"] = _counts(conn, "SELECT source || ':' || kind, COUNT(*) FROM sessions GROUP BY 1")
        status["events"] = _counts(
            conn, "SELECT actor || ':' || kind || COALESCE(':' || decision, ''), COUNT(*) FROM events GROUP BY 1"
        )
        status["turns"] = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        status["messages"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        status["tool_calls"] = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        status["tool_decisions"] = _counts(conn, "SELECT COALESCE(decision_source, 'none'), COUNT(*) FROM tool_calls GROUP BY 1")
        status["bridge_rows"] = _counts(conn, "SELECT source, COUNT(*) FROM bridge_rows GROUP BY 1")
        files = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(archive_size), 0), "
            "COALESCE(SUM(CASE WHEN gone_at IS NOT NULL THEN 1 ELSE 0 END), 0) "
            "FROM files WHERE role != 'side_ledger'"
        ).fetchone()
        status["files"] = {"count": files[0], "source_bytes": files[1], "archive_bytes": files[2], "outlived_source": files[3]}
        span = conn.execute("SELECT MIN(started_at), MAX(ended_at) FROM sessions").fetchone()
        status["span"] = {"first": span[0], "last": span[1]}
        last = conn.execute(
            "SELECT started_at, finished_at, files_changed, errors FROM ingest_runs "
            "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        status["last_run"] = dict(last) if last else None
        status["at_risk"] = _at_risk(conn, roots, now())
    finally:
        conn.close()
    return status


def _at_risk(conn: sqlite3.Connection, roots: list[Root] | None, now: float) -> dict[str, Any]:
    """Claude transcripts within `AT_RISK_MARGIN_DAYS` of their account's reaper and not fully archived."""
    reapers: dict[str, int] = {}
    for root in roots or ():
        if root.source == "claude":
            reapers[root.profile] = reaper_days(root.base)
    count = 0
    oldest_age_days = None
    for profile, _size, _archived, mtime_ns in conn.execute(
        "SELECT profile, size, archived_bytes, mtime_ns FROM files "
        "WHERE source = 'claude' AND role = 'transcript' AND gone_at IS NULL AND archived_bytes < size"
    ):
        age = (now - mtime_ns / 1e9) / 86400
        limit = reapers.get(profile, DEFAULT_REAPER_DAYS)
        if age >= limit - AT_RISK_MARGIN_DAYS:
            count += 1
            oldest_age_days = age if oldest_age_days is None else max(oldest_age_days, age)
    return {
        "count": count,
        "oldest_age_days": round(oldest_age_days, 1) if oldest_age_days else None,
        "reaper_days": reapers or {"default": DEFAULT_REAPER_DAYS},
    }
