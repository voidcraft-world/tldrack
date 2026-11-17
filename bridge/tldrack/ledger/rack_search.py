"""Find a rack session by what was SAID in it — the rack's search box, past the titles.

`GET /ledger/rack-search?q=<terms>&s=<session>&s=…` asks, for each named LIVE session, whether every
term appears somewhere in its name, its auto title or its conversation, and answers only the
sessions the CHAT decided, each with the newest message that matched. A session its title alone
matches is left out: the rack shows those by itself, instantly (`ui/src/agent-rack/
rack-search.ts`), and this route only ever adds rows to that.

── SEMANTICS ─────────────────────────────────────────────────────────────────────
The rack filter's own (`rack-search.ts`): whitespace-separated terms, case-folded substrings, every
term somewhere. One term may be met by the title and another by the chat, and two terms by two
different messages: this finds a SESSION, not a line. The snippet is still the best LINE: the newest
message holding every term the title did not, else the newest holding any of them.

The CHAT is what the operator and the agent said (`CHAT_ROLES`), never a `machine` message. Seen
the first day: a hook's typecheck failure, injected into every session that edited while it stood,
matched one search term in several unrelated rows with the same snippet. Task notifications,
compaction summaries and rack-typed wakes are the session's plumbing, not what anyone said in it.

── WHERE A ROW'S CONVERSATION IS ─────────────────────────────────────────────────
A rack row is a tmux name; the ledger keys a conversation by the agent's own id. The join is the
bridge's session record (`session_records.py` `agent_session_id`, written by every hook), else the
stem of the transcript the tracker bound (`TranscriptTracker.paths()`; a main Claude file is named
for its id). It is widened to the THREAD (`laya_links.thread_files`): a RESTORE or a ↺ CLOSED reopen
resumes into a new file under a new id, and what was said before the resume is still this row's.

── THE FRESH TAIL ────────────────────────────────────────────────────────────────
The ledger ingests every 15 min (`scheduler.py`), so the minutes you are most likely to remember are
the ones it has not read. Each row's LIVE transcript is folded here past the size the ledger last
folded, with the ledger's own folds, so a tail message reads exactly as its indexed twin will. A
`LiveTails` entry is kept per path and only the bytes appended since are read on the next keystroke.
At most `TAIL_BYTES` from the end: with the ledger off, that window is all a row is searched by.

The live path is the tracker's binding, else the conversation's newest ledger file, else a glob for
its id under the configured account roots (`ingest.discover_roots`). Every one of those is a path
the bridge chose; nothing in the request names a file.
"""

from __future__ import annotations

import glob
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tldrack.ledger.claude_fold import fold_claude
from tldrack.ledger.codex_fold import fold_codex
from tldrack.ledger.ingest import Root
from tldrack.ledger.laya_links import thread_files
from tldrack.ledger.reader import (
    LEDGER_UNAVAILABLE,
    QUERY_MAX,
    SNIPPET_CHARS,
    SNIPPET_LEAD,
    LedgerBusy,
    indexed_at,
    ledger_read,
    ledger_ready,
)
from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.snapshot import SESSION_NAME_RE

#: Sessions per call: `RACK_SEARCH_SESSIONS_MAX`, owned by schemas/src/agent-history.ts.
SESSIONS_MAX = 64
#: Terms per call. Every term past the title is one scan of each row's thread.
TERMS_MAX = 8
#: The most of a live transcript read past the ledger's fold, from its end.
TAIL_BYTES = 8 * 1024 * 1024
#: A tail held this far past its window is rebuilt from the window, so an entry stays bounded
#: when the ledger is off and never moves the window itself.
TAIL_SLACK = 2
#: How long a glob that found no transcript for a conversation is believed.
MISS_SECONDS = 60.0
#: The ledger roles a search reads: the operator's prompts and the agent's replies.
CHAT_ROLES = ("human", "agent")
_ROLE_MARKS = ", ".join(f"'{role}'" for role in CHAT_ROLES)


@dataclass(frozen=True)
class RowBinding:
    """What the bridge knows of one rack session, for the search."""

    session: str
    #: The text the rack's own filter reads: the session name and its auto title.
    title: str
    #: The agent's conversation id (`agent_session_id`), when one is on record.
    conversation: str | None = None
    #: The live transcript the tracker bound (a resolved, allowlisted Claude path).
    transcript: str | None = None


@dataclass(frozen=True)
class TailMessage:
    ts: float | None
    role: str
    text: str
    lowered: str


@dataclass
class _Tail:
    ident: tuple[int, int]
    indexed: int
    start: int
    end: int
    messages: list[TailMessage] = field(default_factory=list)


def _source_of(path: str, roots: Sequence[Root]) -> str | None:
    """`claude` or `codex` for a path under one of the account roots, by the longest root."""
    best: tuple[int, str] | None = None
    for root in roots:
        base = os.path.realpath(root.base) + os.sep
        if path.startswith(base) and (best is None or len(base) > best[0]):
            best = (len(base), root.source)
    return best[1] if best else None


class LiveTails:
    """The unindexed end of each live transcript, folded, per path. Thread-safe."""

    def __init__(self, tail_bytes: int = TAIL_BYTES) -> None:
        self._lock = threading.Lock()
        self._tail_bytes = tail_bytes
        self._tails: dict[str, _Tail] = {}
        self._found: dict[str, tuple[str, str] | None] = {}
        self._missed_at: dict[str, float] = {}

    def messages(self, path: str, source: str, indexed: int) -> list[TailMessage]:
        """The messages in `path` past byte `indexed` (what the ledger has folded), newest last.
        An unreadable file is no messages, never an error: the row keeps its ledger hits."""
        with self._lock:
            try:
                return list(self._refresh(path, source, indexed).messages)
            except OSError:
                self._tails.pop(path, None)
                return []

    def retain(self, paths: Iterable[str]) -> None:
        """Forget every tail but these: only the rows on screen are worth holding."""
        keep = set(paths)
        with self._lock:
            for path in [p for p in self._tails if p not in keep]:
                del self._tails[path]

    def find(self, conversation: str, roots: Sequence[Root], now: float) -> tuple[str, str] | None:
        """`(path, source)` of a conversation's live transcript under the account roots, by its id.
        A hit is kept for good (a file never changes id); a miss for `MISS_SECONDS`."""
        with self._lock:
            if conversation in self._found:
                return self._found[conversation]
            if now - self._missed_at.get(conversation, -MISS_SECONDS) < MISS_SECONDS:
                return None
        stem = glob.escape(conversation)
        found: tuple[str, str] | None = None
        for root in roots:
            base = glob.escape(str(root.base))
            pattern = (
                os.path.join(base, "projects", "*", f"{stem}.jsonl")
                if root.source == "claude"
                else os.path.join(base, "sessions", "**", f"rollout-*{stem}.jsonl")
            )
            hits = sorted(glob.glob(pattern, recursive=True))
            if hits:
                found = (os.path.realpath(hits[-1]), root.source)
                break
        with self._lock:
            if found is None:
                self._missed_at[conversation] = now
            else:
                self._found[conversation] = found
                self._missed_at.pop(conversation, None)
        return found

    def _refresh(self, path: str, source: str, indexed: int) -> _Tail:
        st = os.stat(path)
        ident = (st.st_dev, st.st_ino)
        start = max(indexed, st.st_size - self._tail_bytes, 0)
        tail = self._tails.get(path)
        if (
            tail is None
            or tail.ident != ident
            or tail.indexed != indexed
            or st.st_size < tail.end
            or start - tail.start > self._tail_bytes * TAIL_SLACK
        ):
            tail = _Tail(ident=ident, indexed=indexed, start=start, end=start)
            self._tails[path] = tail
        if st.st_size <= tail.end:
            return tail
        with open(path, "rb") as handle:
            handle.seek(tail.end)
            chunk = handle.read(st.st_size - tail.end)
        if tail.end == tail.start and tail.start != indexed and tail.start > 0:
            # Cut into a line by the window: the first whole line starts after the next newline.
            cut = chunk.find(b"\n")
            if cut < 0:
                return tail
            tail.end += cut + 1
            chunk = chunk[cut + 1 :]
        last = chunk.rfind(b"\n")
        if last < 0:
            return tail  # a line still being written: read it whole next time
        whole = chunk[: last + 1]
        tail.end += len(whole)
        lines = whole.splitlines(keepends=True)
        fold = fold_codex(lines) if source == "codex" else fold_claude(lines, path=path)
        tail.messages.extend(
            TailMessage(m["ts"], m["role"], m["text"], m["text"].lower())
            for m in fold.messages
            if m.get("text") and m.get("role") in CHAT_ROLES
        )
        return tail


def snippet(text: str, at: int) -> str:
    """The same cut as the ledger's SQL snippet (`reader.SNIPPET_LEAD` / `SNIPPET_CHARS`)."""
    start = max(0, at - SNIPPET_LEAD)
    return text[start : start + SNIPPET_CHARS]


def parse_query(query: dict) -> tuple[list[str], list[str]]:
    """`(terms, sessions)` from the query string. Raises ValueError for a 400."""
    raw = query.get("q", [""])
    if len(raw) != 1 or len(raw[0]) > QUERY_MAX:
        raise ValueError(f"q must be at most {QUERY_MAX} characters")
    terms = list(dict.fromkeys(term for term in raw[0].lower().split() if term))
    if len(terms) > TERMS_MAX:
        raise ValueError(f"at most {TERMS_MAX} search terms")
    sessions = list(dict.fromkeys(query.get("s", [])))
    if len(sessions) > SESSIONS_MAX:
        raise ValueError(f"at most {SESSIONS_MAX} sessions per search")
    if any(not SESSION_NAME_RE.fullmatch(name) for name in sessions):
        raise ValueError("s must be session names")
    return terms, sessions


@dataclass
class _Row:
    binding: RowBinding
    #: The terms the title does not already meet: the chat must meet every one.
    wanted: list[str]
    conversation: str | None
    #: The thread's main ledger files (`_thread_rows`), read in the slot and used after it.
    threads: list[sqlite3.Row] = field(default_factory=list)
    #: The newest ledger message holding EVERY wanted term, when there are two or more: the snippet
    #: a phrase deserves over a line holding one of its words.
    together: dict | None = None
    #: The live transcript: `(path, source, bytes the ledger has folded)`.
    live: tuple[str, str, int] | None = None


def _conversation_of(binding: RowBinding) -> str | None:
    if binding.conversation and AGENT_SESSION_ID_RE.fullmatch(binding.conversation):
        return binding.conversation
    stem = Path(binding.transcript).stem if binding.transcript else ""
    return stem if AGENT_SESSION_ID_RE.fullmatch(stem) else None


def _thread_rows(conn: sqlite3.Connection, row: _Row) -> list[sqlite3.Row]:
    """Every main ledger file of the row's thread, with its path and folded size."""
    seeds: list[sqlite3.Row] = []
    if row.conversation:
        seeds = conn.execute(
            "SELECT file_id FROM sessions WHERE session_id = ? AND kind = 'main'", (row.conversation,)
        ).fetchall()
    if not seeds and row.binding.transcript:
        seeds = conn.execute(
            "SELECT s.file_id FROM sessions s JOIN files f ON f.id = s.file_id WHERE f.path = ? AND s.kind = 'main'",
            (row.binding.transcript,),
        ).fetchall()
    ids = {thread["file_id"] for seed in seeds for thread in thread_files(conn, seed["file_id"])}
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    return conn.execute(
        "SELECT s.file_id, s.session_id, s.source, s.ended_at, f.path, f.size, f.error "
        f"FROM sessions s JOIN files f ON f.id = s.file_id WHERE s.file_id IN ({marks})",
        sorted(ids),
    ).fetchall()


def _ledger_hits(conn: sqlite3.Connection, rows: list[_Row]) -> dict[tuple[int, str], dict]:
    """`(file_id, term)` → the newest message in that file holding the term. One scan per term,
    over the searched rows' threads only (the `messages_file` index), never the whole ledger."""
    hits: dict[tuple[int, str], dict] = {}
    for term in dict.fromkeys(t for row in rows for t in row.wanted):
        ids = sorted({t["file_id"] for row in rows if term in row.wanted for t in row.threads})
        if not ids:
            continue
        marks = ", ".join("?" for _ in ids)
        # SQLite's bare columns beside max(): ts, role and the snippet come from the newest match.
        for found in conn.execute(
            "SELECT file_id, max(seq) AS seq, ts, role, "
            "substr(text, max(1, instr(lower(text), lower(?)) - ?), ?) AS snippet "
            f"FROM messages WHERE file_id IN ({marks}) AND role IN ({_ROLE_MARKS}) "
            "AND instr(lower(text), lower(?)) > 0 GROUP BY file_id",
            [term, SNIPPET_LEAD, SNIPPET_CHARS, *ids, term],
        ):
            hits[(found["file_id"], term)] = {"snippet": found["snippet"], "role": found["role"], "ts": found["ts"]}
    return hits


def _together(conn: sqlite3.Connection, row: _Row) -> dict | None:
    """The newest chat message in the row's thread holding every wanted term, or None."""
    ids = [t["file_id"] for t in row.threads]
    if len(row.wanted) < 2 or not ids:
        return None
    marks = ", ".join("?" for _ in ids)
    every = " AND ".join("instr(lower(text), lower(?)) > 0" for _ in row.wanted)
    found = conn.execute(
        "SELECT ts, role, substr(text, max(1, instr(lower(text), lower(?)) - ?), ?) AS snippet "
        f"FROM messages WHERE file_id IN ({marks}) AND role IN ({_ROLE_MARKS}) AND {every} "
        "ORDER BY ts DESC, seq DESC LIMIT 1",
        [row.wanted[0], SNIPPET_LEAD, SNIPPET_CHARS, *ids, *row.wanted],
    ).fetchone()
    return {"snippet": found["snippet"], "role": found["role"], "ts": found["ts"]} if found else None


def _live_path(row: _Row, tails: LiveTails, roots: Sequence[Root], now: float) -> tuple[str, str] | None:
    if row.binding.transcript:
        return row.binding.transcript, "claude"  # the tracker binds Claude transcripts only
    if row.threads:
        newest = max(row.threads, key=lambda t: (t["ended_at"] or 0, t["file_id"]))
        path = os.path.realpath(newest["path"])
        if os.path.isfile(path):
            return path, newest["source"]
    if row.conversation:
        return tails.find(row.conversation, roots, now)
    return None


def _folded_bytes(path: str, threads: list[sqlite3.Row]) -> int:
    """How much of `path` the ledger's messages already cover: its file's size at the last clean
    fold, else nothing (a fold that errored indexed no messages)."""
    for thread in threads:
        if thread["error"] is None and os.path.realpath(thread["path"]) == path:
            return int(thread["size"] or 0)
    return 0


def _newest(hits: Iterable[dict]) -> dict | None:
    best: dict | None = None
    for hit in hits:
        if best is None or (hit["ts"] or 0) > (best["ts"] or 0):
            best = hit
    return best


def plan_rows(
    conn: sqlite3.Connection | None, bindings: Sequence[RowBinding], terms: list[str]
) -> tuple[list[_Row], dict[tuple[int, str], dict]]:
    """The SQL half, run inside the read slot: each row the title alone does not decide, with its
    thread's ledger files, and the ledger's hits for the terms it still wants."""
    rows = []
    for binding in bindings:
        title = binding.title.lower()
        wanted = [term for term in terms if term not in title]
        if wanted:
            rows.append(_Row(binding, wanted, _conversation_of(binding)))
    if conn is None:
        return rows, {}
    for row in rows:
        row.threads = _thread_rows(conn, row)
    hits = _ledger_hits(conn, rows)
    for row in rows:
        if all(any((t["file_id"], term) in hits for t in row.threads) for term in row.wanted):
            row.together = _together(conn, row)
    return rows, hits


def match_rows(
    rows: list[_Row], ledger: dict[tuple[int, str], dict], tails: LiveTails, roots: Sequence[Root], now: float | None = None
) -> dict[str, dict]:
    """The file half, run after the slot is released: each row's live tail, then the verdict.
    session → `{snippet, role, ts}` for every row the chat decided."""
    now = time.time() if now is None else now
    for row in rows:
        found = _live_path(row, tails, roots, now)
        if found is not None:
            path, source = found
            row.live = (path, _source_of(path, roots) or source, _folded_bytes(path, row.threads))
    tails.retain(row.live[0] for row in rows if row.live)
    matches: dict[str, dict] = {}
    for row in rows:
        tail = tails.messages(*row.live) if row.live else []
        together = next(
            (
                {"snippet": snippet(m.text, m.lowered.find(row.wanted[0])), "role": m.role, "ts": m.ts}
                for m in reversed(tail)
                if all(term in m.lowered for term in row.wanted)
            ),
            None,
        )
        newest_by_term: list[dict] = []
        for term in row.wanted:
            hit = next(
                (
                    {"snippet": snippet(m.text, m.lowered.find(term)), "role": m.role, "ts": m.ts}
                    for m in reversed(tail)
                    if term in m.lowered
                ),
                None,
            )
            hit = hit or _newest(ledger[(t["file_id"], term)] for t in row.threads if (t["file_id"], term) in ledger)
            if hit is None:
                break
            newest_by_term.append(hit)
        else:
            matches[row.binding.session] = together or row.together or _newest(newest_by_term)
    return matches


def rack_search(
    root: Path | None,
    query: dict,
    bind: Callable[[list[str]], list[RowBinding]],
    tails: LiveTails,
    roots: Callable[[], Sequence[Root]],
) -> tuple[int, dict]:
    """`GET /ledger/rack-search` — the shape is `rack_search_schema` (schemas/src/agent-history.ts)."""
    try:
        terms, sessions = parse_query(query)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    readable = ledger_ready(root) is None
    if not terms or not sessions:
        return 200, {"matches": {}, "ledger": readable, "indexed_at": None}
    bindings = bind(sessions)
    stamp = None
    if not readable:
        # No ledger to read: the live tails are still a search, of the recent window only.
        rows, ledger = plan_rows(None, bindings, terms)
    else:
        try:
            with ledger_read(root) as conn:
                rows, ledger = plan_rows(conn, bindings, terms)
                stamp = indexed_at(conn)
        except LedgerBusy as exc:
            return 503, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            return 503, {"error": LEDGER_UNAVAILABLE}
    return 200, {"matches": match_rows(rows, ledger, tails, roots()), "ledger": readable, "indexed_at": stamp}
