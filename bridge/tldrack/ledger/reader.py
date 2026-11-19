"""Bounded, read-only conversation browsing for the rack.

Search is a literal substring, including punctuation in code and paths, like browser Find.
SQL parameters keep `%`, `_`, quotes and FTS operators literal. Results use (file_id, seq),
not message rowids: ingest replaces a file's rows and may reuse rowids for other messages.
The index's existing text limits still apply; the raw archive is not served by these routes.

`threads` is the Laya REVIEW page's batch join: one call resolves a page of verdicts (and deck cards,
by state key) to the turn each judged and one header per conversation thread. It reads the
`laya_links` table the ingest writes (`laya_links.py` owns the matching and the thread rule), and a
ledger that predates the table answers every verdict `missing` rather than failing: these
connections are read-only and never migrate.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tldrack.ledger.laya_links import LINKED_SOURCES, RETRY_SECONDS, thread_files
from tldrack.ledger.store import DB_NAME, connect
from tldrack.open_work import DECISION_ID, asks_question, command_request
from tldrack.profiles import AGENT_SESSION_ID_RE

SEARCH_PAGE = 30
MESSAGE_PAGE = 30
QUERY_MAX = 300
#: A hit's snippet: up to `SNIPPET_LEAD` characters before the match, `SNIPPET_CHARS` in all. The rack's
#: session search (`rack_search.py`) cuts its live-tail snippets by the same two numbers.
SNIPPET_LEAD = 80
SNIPPET_CHARS = 300
READ_SECONDS = 2.0
# Keystrokes cancel browser fetches, not work already running in a request thread.
# Bound concurrent scans so rapid typing cannot crowd out agent hooks.
_READ_SLOTS = threading.BoundedSemaphore(2)

SESSION_COLUMNS = "s.file_id, s.session_id, s.source, s.profile, s.kind, s.title, s.cwd"

#: Ids per `threads` call, all kinds together: `HISTORY_THREADS_MAX`, owned by schemas/src/agent-history.ts.
THREADS_MAX = 100
#: A copy of `REVIEW_KEY`: owned by schemas/src/laya-review.ts. A `golden:` key is never a verdict's.
REVIEW_KEY_RE = re.compile(r"[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63}")
CLIP_CHARS = 1_200
FIRST_USER_CHARS = 600
TITLE_CHARS = 120
WOKE_BY_CHARS = 32
HUMAN_ORIGINS = ("human", "command")


def integer(query: dict, key: str, default: int | None = None) -> int | None:
    values = query.get(key)
    if values is None:
        return default
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise ValueError(f"{key} must be a non-negative integer")
    value = int(values[0])
    if value > 2_000_000_000:
        raise ValueError(f"{key} is too large")
    return value


def search_messages(conn: sqlite3.Connection, query: dict) -> dict:
    terms = query.get("q", [""])
    if len(terms) != 1 or len(terms[0]) > QUERY_MAX:
        raise ValueError(f"q must be at most {QUERY_MAX} characters")
    text = terms[0].strip()
    offset = integer(query, "offset", 0)
    file_id = integer(query, "file_id")
    scope = " AND s.file_id = ?" if file_id is not None else ""
    scope_args = [file_id] if file_id is not None else []
    if text:
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS}, m.seq, m.ts, m.role, "
            "substr(m.text, max(1, instr(lower(m.text), lower(?)) - ?), ?) AS snippet "
            "FROM messages m JOIN sessions s ON s.file_id = m.file_id "
            f"WHERE instr(lower(m.text), lower(?)) > 0{scope} "
            "ORDER BY m.ts DESC, m.file_id DESC, m.seq DESC LIMIT ? OFFSET ?",
            [text, SNIPPET_LEAD, SNIPPET_CHARS, text, *scope_args, SEARCH_PAGE + 1, offset],
        ).fetchall()
    else:
        # A blank search is one latest message per conversation, not thirty turns of one agent.
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS}, m.seq, m.ts, m.role, substr(m.text, 1, ?) AS snippet "
            "FROM sessions s JOIN messages m ON m.id = "
            "(SELECT id FROM messages WHERE file_id = s.file_id ORDER BY seq DESC LIMIT 1) "
            f"WHERE 1=1{scope} ORDER BY m.ts DESC, s.file_id DESC LIMIT ? OFFSET ?",
            [SNIPPET_CHARS, *scope_args, SEARCH_PAGE + 1, offset],
        ).fetchall()
    return {
        "hits": [dict(row) for row in rows[:SEARCH_PAGE]],
        "next_offset": offset + SEARCH_PAGE if len(rows) > SEARCH_PAGE else None,
    }


def conversation(conn: sqlite3.Connection, query: dict) -> dict:
    file_id = integer(query, "file_id")
    if file_id is None:
        raise ValueError("file_id is required")
    anchors = [key for key in ("around", "before", "after") if key in query]
    if len(anchors) > 1:
        raise ValueError("use only one of around, before or after")
    session = conn.execute(f"SELECT {SESSION_COLUMNS} FROM sessions s WHERE file_id = ?", (file_id,)).fetchone()
    if session is None:
        raise LookupError("Conversation is no longer in the index")
    mode = anchors[0] if anchors else "before"
    anchor = integer(query, mode)
    if mode == "around":
        # Find by sequence, not arithmetic: tool frames can leave large gaps between messages.
        preceding = conn.execute(
            "SELECT seq FROM messages WHERE file_id = ? AND seq < ? ORDER BY seq DESC LIMIT 10", (file_id, anchor)
        ).fetchall()
        anchor = preceding[-1][0] if preceding else anchor
    op = {"around": ">=", "after": ">", "before": "<"}[mode]
    order = "DESC" if mode == "before" else "ASC"
    where = f" AND seq {op} ?" if anchor is not None else ""
    rows = conn.execute(
        f"SELECT seq, ts, role, text FROM messages WHERE file_id = ?{where} ORDER BY seq {order} LIMIT ?",
        [file_id, *([anchor] if anchor is not None else []), MESSAGE_PAGE],
    ).fetchall()
    messages = sorted((dict(row) for row in rows), key=lambda row: row["seq"])
    # Independent of the page: a review of the end still needs the session's original request.
    first_user = first_user_message(conn, file_id)
    bounds = conn.execute("SELECT min(seq), max(seq) FROM messages WHERE file_id = ?", (file_id,)).fetchone()
    return {
        "session": dict(session),
        "messages": messages,
        "first_user": dict(first_user) if first_user is not None else None,
        "before": messages[0]["seq"] if messages and bounds[0] < messages[0]["seq"] else None,
        "after": messages[-1]["seq"] if messages and bounds[1] > messages[-1]["seq"] else None,
    }


def first_user_message(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    """The operator's first message in a file. A prompt the rack or another agent typed keeps the
    human ROLE (relay.py marks only its turn's origin), so the turn decides whose it was; a message
    with no turn row is taken at its role."""
    marks = ", ".join("?" for _ in HUMAN_ORIGINS)
    return conn.execute(
        "SELECT m.seq, m.ts, m.role, m.text FROM messages m LEFT JOIN turns t "
        "ON t.file_id = m.file_id AND t.idx = m.turn_idx WHERE m.file_id = ? "
        f"AND m.role IN ('human', 'user') AND (t.origin IS NULL OR t.origin IN ({marks})) "
        "ORDER BY m.seq LIMIT 1",
        (file_id, *HUMAN_ORIGINS),
    ).fetchone()


# ── /ledger/threads ────────────────────────────────────────────────────────────


def clip_head(text: str, limit: int) -> tuple[str, int | None]:
    """`text`'s start, at most `limit` chars and ended at a word where one is near; and its full
    length, or None when nothing was cut."""
    if len(text) <= limit:
        return text, None
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= limit * 0.8:
        cut = cut[:space]
    return cut.rstrip(), len(text)


def tail_at_line(text: str, limit: int) -> str:
    """`text`'s end, at most `limit` chars, starting at a line boundary (else a word), never mid-word."""
    if len(text) <= limit:
        return text
    window = text[-limit:]
    if text[-limit - 1] == "\n":
        return window
    line = window.find("\n")
    if 0 <= line < limit // 2:
        return window[line + 1 :]
    if text[-limit - 1].isspace():
        return window.lstrip()
    space = next((i for i, ch in enumerate(window) if ch.isspace()), None)
    return window[space:].lstrip() if space is not None else window


def _clip(row: sqlite3.Row | None, limit: int = CLIP_CHARS) -> dict | None:
    if row is None:
        return None
    text, chars = clip_head(row["text"], limit)
    clipped = {"seq": row["seq"], "ts": row["ts"], "role": row["role"], "text": text}
    if chars is not None:
        clipped["chars"] = chars
    return clipped


def turn_asked(text: str | None, woke_by: str | None) -> bool:
    """Did the operator open this turn with a question? Pure. The rack's rule for an answer to read
    (`open_work.py` § An answer to read: the operator's own prompt, its last line ending in `?`), read
    from the ledger's whole prompt, never the page's clip, whose head-cut drops the `?`."""
    if not text or woke_by not in HUMAN_ORIGINS:
        return False
    return asks_question(command_request(text) or text)


def _first_line(text: str | None) -> str | None:
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), None)
    return clip_head(line, TITLE_CHARS)[0] if line else None


_UNLINKED = dict.fromkeys(
    (
        "thread",
        "conversation",
        "file_id",
        "closing_seq",
        "turn_idx",
        "human_ordinal",
        "woke_by",
        "request",
        "asked",
        "ask",
        "closing_tail",
        "closing_chars",
    )
)


def _unlinked(status: str) -> dict:
    return {"status": status, **_UNLINKED}


def _id_list(query: dict, key: str, shape: re.Pattern, what: str) -> list[str]:
    values = query.get(key)
    if values is None:
        return []
    if len(values) != 1:
        raise ValueError(f"{key} must be one comma-joined parameter")
    if len(values[0]) > THREADS_MAX * 72:
        raise ValueError(f"{key} is longer than {THREADS_MAX} ids")
    parts = [part for part in values[0].split(",") if part]
    if not all(shape.fullmatch(part) for part in parts):
        raise ValueError(f"{key} must hold {what}, comma-joined")
    return parts


class _ThreadJoin:
    """One `threads` call's lookups, memoised per file so a page of turns from one conversation builds
    its header once."""

    def __init__(self, conn: sqlite3.Connection, now: float) -> None:
        self.conn = conn
        self.now = now
        self.has_links = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'laya_links'").fetchone() is not None
        )
        self.headers: dict[str, dict] = {}
        self._threads: dict[int, str | None] = {}

    def decision(self, decision_id: str) -> dict:
        if not self.has_links:
            return _unlinked("missing")
        link = self.conn.execute("SELECT * FROM laya_links WHERE decision_id = ?", (decision_id,)).fetchone()
        return self.turn(link) if link is not None else _unlinked(self._unattempted(decision_id))

    def key(self, key: str) -> dict:
        if not self.has_links:
            return _unlinked("missing")
        # The deck shows a key's NEWEST verdict (review_data.py `_ledger_decisions`); a linked one wins a tie
        # of text, since the same text is the same turn to read.
        link = self.conn.execute(
            "SELECT * FROM laya_links WHERE state_key = ? ORDER BY method = 'none', at DESC LIMIT 1", (key,)
        ).fetchone()
        return self.turn(link) if link is not None else _unlinked("missing")

    def conversation(self, conversation: str) -> str | None:
        row = self.conn.execute(
            "SELECT file_id FROM sessions WHERE session_id = ? AND kind = 'main' ORDER BY ended_at DESC, file_id DESC LIMIT 1",
            (conversation,),
        ).fetchone()
        return self.thread(row[0]) if row is not None else None

    def _unattempted(self, decision_id: str) -> str:
        """`pending` while the ledger may simply not have read this verdict (or its turn) yet, else `missing`."""
        try:
            stamp = calendar.timegm(time.strptime(decision_id[:15], "%Y%m%dT%H%M%S"))
        except ValueError:
            return "missing"
        marks = ", ".join("?" for _ in LINKED_SOURCES)
        held = self.conn.execute(
            f"SELECT 1 FROM bridge_rows WHERE source IN ({marks}) AND kind = 'decision' "
            "AND ts BETWEEN ? AND ? AND instr(data, ?) > 0 LIMIT 1",
            (*LINKED_SOURCES, stamp - 5, stamp + 5, decision_id),
        ).fetchone()
        if held is not None:
            return "pending"  # read, not linked yet: the linker runs after the walk that will see its turn
        read = self.conn.execute(
            "SELECT max(started_at) FROM ingest_runs WHERE finished_at IS NOT NULL AND (note IS NULL OR note = 'only:bridge')"
        ).fetchone()[0]
        return "pending" if read is None or stamp >= read - 60 else "missing"

    def turn(self, link: sqlite3.Row) -> dict:
        if link["method"] == "none":
            retrying = link["at"] is not None and link["at"] >= self.now - RETRY_SECONDS
            return _unlinked("pending" if retrying else "none")
        conn, file_id = self.conn, link["file_id"]
        closing = conn.execute(
            "SELECT seq, ts, role, turn_idx, text FROM messages WHERE file_id = ? AND seq = ?", (file_id, link["seq"])
        ).fetchone()
        if closing is None or closing["role"] != "agent":
            return _unlinked("pending")  # the file was re-folded since; the next ingest relinks it
        own = conn.execute("SELECT session_id FROM sessions WHERE file_id = ?", (file_id,)).fetchone()
        idx = closing["turn_idx"]
        woke_by = request = ask = None
        ordinal = 0
        if idx is not None:
            origin = conn.execute("SELECT origin FROM turns WHERE file_id = ? AND idx = ?", (file_id, idx)).fetchone()
            woke_by = origin[0][:WOKE_BY_CHARS] if origin is not None else None
            request = self._opener(file_id, idx)
            asked, ordinal = conn.execute(
                "SELECT max(idx), count(*) FROM turns WHERE file_id = ? AND idx <= ? AND origin IN (?, ?)",
                (file_id, idx, *HUMAN_ORIGINS),
            ).fetchone()
            ask = request if asked == idx else self._opener(file_id, asked) if asked is not None else None
        return {
            "status": "linked",
            "thread": self.thread(file_id),
            "conversation": own[0] if own else None,
            "file_id": file_id,
            "closing_seq": closing["seq"],
            "turn_idx": idx,
            "human_ordinal": ordinal,
            "woke_by": woke_by,
            "request": _clip(request),
            "asked": turn_asked(request["text"] if request is not None else None, woke_by),
            "ask": _clip(ask),
            "closing_tail": tail_at_line(closing["text"], CLIP_CHARS),
            "closing_chars": len(closing["text"]),
        }

    def _opener(self, file_id: int, idx: int) -> sqlite3.Row | None:
        """A turn's first message: the prompt that opened it (`fold.Fold.prompt_turn`)."""
        return self.conn.execute(
            "SELECT seq, ts, role, text FROM messages WHERE file_id = ? AND turn_idx = ? ORDER BY seq LIMIT 1", (file_id, idx)
        ).fetchone()

    def thread(self, file_id: int) -> str | None:
        if file_id not in self._threads:
            files = thread_files(self.conn, file_id)
            key = files[0]["session_id"] if files else None
            if key is not None and key not in self.headers:
                self.headers[key] = self._header(key, files)
            for row in files or ():
                self._threads[row["file_id"]] = key
            self._threads[file_id] = key
        return self._threads[file_id]

    def _header(self, key: str, files: list[sqlite3.Row]) -> dict:
        first, newest = files[0], files[-1]
        title = next((row["title"] for row in reversed(files) if row["title"]), None)
        last = [
            self.conn.execute("SELECT ts FROM messages WHERE file_id = ? ORDER BY seq DESC LIMIT 1", (row["file_id"],)).fetchone()
            for row in files
        ]
        stamps = [row[0] for row in last if row is not None and row[0] is not None]
        return {
            "thread": key,
            "title": clip_head(title, TITLE_CHARS)[0] if title else _first_line(first["first_prompt"]),
            "first_user": _clip(first_user_message(self.conn, first["file_id"]), FIRST_USER_CHARS),
            "started_at": first["started_at"],
            "last_ts": max(stamps) if stamps else None,
            "cwd": newest["cwd"],
            "branch": newest["git_branch"],
            "profile": newest["profile"],
            "source": newest["source"],
            "kind": newest["kind"],
            "human_turns": max((row["human_turns"] or 0) for row in files),
            "resumed": len(files) - 1,
        }


def threads(conn: sqlite3.Connection, query: dict, *, now: float | None = None) -> dict:
    """`GET /ledger/threads?d=&c=&k=` — the shape is `history_threads_schema` (schemas/src/agent-history.ts)."""
    decisions = _id_list(query, "d", DECISION_ID, "Laya decision ids")
    conversations = _id_list(query, "c", AGENT_SESSION_ID_RE, "conversation ids")
    keys = _id_list(query, "k", REVIEW_KEY_RE, "review keys")
    if len(decisions) + len(conversations) + len(keys) > THREADS_MAX:
        raise ValueError(f"at most {THREADS_MAX} ids per call")
    join = _ThreadJoin(conn, time.time() if now is None else now)
    return {
        "decisions": {d: join.decision(d) for d in dict.fromkeys(decisions)},
        "keys": {k: join.key(k) for k in dict.fromkeys(keys)},
        "conversations": {c: join.conversation(c) for c in dict.fromkeys(conversations)},
        "threads": join.headers,
    }


ROUTES = {"search": search_messages, "conversation": conversation, "threads": threads}


class LedgerBusy(Exception):
    """Both read slots are taken: a 503 the caller retries."""


#: The 503 bodies every ledger read answers with, so the rack's two readers say the same thing.
LEDGER_OFF = "Session ledger is off. Configure it in AGENT RACK › SESSION LEDGER."
LEDGER_EMPTY = "No conversations indexed yet. Use Refresh index."
LEDGER_BUSY = "History search is busy. Please retry."
LEDGER_UNAVAILABLE = "History index is busy or unavailable. Narrow your search and retry."


def ledger_ready(root: Path | None) -> str | None:
    """Why this ledger cannot be read right now, or None. Never creates the directory."""
    if root is None:
        return LEDGER_OFF
    if not (root / DB_NAME).is_file():
        return LEDGER_EMPTY
    return None


@contextmanager
def ledger_read(root: Path) -> Iterator[sqlite3.Connection]:
    """THE door every read route takes: one of `_READ_SLOTS`, a read-only connection, one
    transaction, and a `READ_SECONDS` deadline that aborts a long scan as a `sqlite3.Error`.
    Raises `LedgerBusy` when both slots are taken."""
    if not _READ_SLOTS.acquire(blocking=False):
        raise LedgerBusy(LEDGER_BUSY)
    conn = None
    try:
        conn = connect(root, readonly=True)
        deadline = time.monotonic() + READ_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute("BEGIN")
        yield conn
    finally:
        if conn is not None:
            conn.close()
        _READ_SLOTS.release()


def indexed_at(conn: sqlite3.Connection) -> float | None:
    return conn.execute("SELECT max(finished_at) FROM ingest_runs WHERE finished_at IS NOT NULL").fetchone()[0]


def read_history(root: Path | None, route: str, query: dict) -> tuple[int, dict]:
    handler = ROUTES.get(route)
    if handler is None:
        return 404, {"error": "No such history route"}
    unready = ledger_ready(root)
    if unready is not None:
        return 503, {"error": unready}
    try:
        with ledger_read(root) as conn:
            body = handler(conn, query)
            body["indexed_at"] = indexed_at(conn)
            return 200, body
    except LedgerBusy as exc:
        return 503, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except LookupError as exc:
        return 404, {"error": str(exc)}
    except (OSError, sqlite3.Error):
        return 503, {"error": LEDGER_UNAVAILABLE}
