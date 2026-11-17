"""Every Laya verdict, linked to the conversation, the file and the closing message it judged.

The REVIEW page shows each verdict inside its conversation (`GET /ledger/threads`, `reader.py`
`threads`). A verdict row (`bridge_rows`, source `laya:agent_open_work`, the whole JSONL line in
`data`, `ts` from its `at` at 1 s precision) carries the state Laya judged, its `state_hash`, the tmux
`subject` (a reused, renamable name that identifies nothing) and, since 2026-11-07, `conversation`:
the Stop hook's `session_id`. This pass turns each into one `laya_links` row after every ingest.
On a ledger of untagged verdicts (2026-11-07) the rules below link nearly all of them, in about
1 s, and a pass with nothing new takes 10 ms.

── MATCHING ────────────────────────────────────────────────────────────────────
The verdict lands AFTER its message: `at − message.ts` ran −2.9 s … +27 s (median 1.2; the negative
end is `at`'s 1 s truncation). So "the nearest message" would often be the NEXT turn's; the rule is
the last agent message no later than `at + AFTER_SECONDS`, and it must reproduce the state:

- **state** — `decide_state(text[-CLOSING_TEXT_MAX_CHARS:])` equals the state, whitespace-trimmed.
  That is exactly how the judge built it: the tracker keeps the closing message's 2,000-char tail
  (`transcript.fold_closing_text`) and `open_work.decide_state` fits it to Laya's window. The
  common case.
- **tail** — the message ends with the state: verdicts from before `fit_hand_off` judged the raw
  tail, which `decide_state` would now refit. Every such row is question version 1, so a
  version ≥ 2 verdict never takes it (a short state like "Done." ends many messages).

- **Tagged** (`conversation` in the row): only that conversation's main file(s), back to
  `TAGGED_BEFORE_SECONDS`, newest first; the method is `id`. The id is certain; the state check
  picks WHICH message.
- **Legacy** (no tag): the main files active at `at` (`started_at ≤ at + 5`, `ended_at ≥ at − 60`;
  a handful at a time), their agent messages in `[at − 60, at + 5]`; any file's `state` match
  first, then `tail` only when none. Same text in a session that was not running at `at` never
  links. Two matches: the gap closest to the typical one wins, then the NEWER file — a resume copies
  its parent's history, timestamps and all, so both files hold the message (and are one thread).
- **Unmatched**: `none`, retried every pass until `RETRY_SECONDS` past `at`, then left.

A verdict is only attempted once its message is surely indexed: `settled_before` is when the pass
began walking the transcripts, and a verdict newer than that (written DURING the walk) waits for the
next pass, so it can never link to an earlier message while its own is still unread.

── THE THREAD ──────────────────────────────────────────────────────────────────
A resumed Claude conversation is a NEW file under a NEW session id (each main file's id is its file
stem), but the resume copies the history, so
both files share `started_at`, `first_prompt` and `cwd`. `thread_files` joins them; the thread key is
the session id of the EARLIEST (first to end), so a resume chain is one band on the page instead of
"a new conversation in vc-8".

── KEEPING IT TRUE ─────────────────────────────────────────────────────────────
Derived and rebuildable: `DELETE FROM laya_links` and the next pass relinks everything. `seq`
(`messages.seq`) is stable while a file only grows; a REWRITTEN file bumps `archive_generation`
(`ingest.ingest_conversation_file`), and its links are deleted and redone, as is any link whose
message is gone. Newest verdicts first, inside `PASS_SECONDS`, so a large backlog never holds the
niced ingest child; what is left waits for the next pass. Idempotent: a linked verdict is not touched.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from tldrack.open_work import DECISION_ID, decide_state
from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.transcript import CLOSING_TEXT_MAX_CHARS

#: The side ledgers whose verdicts judged an agent's closing message (`side_ledgers.py` names them).
LINKED_SOURCES = ("laya:agent_open_work",)
AFTER_SECONDS = 5.0
BEFORE_SECONDS = 60.0
#: Far past a cold start's `open_work.WARM_WAIT_SECONDS`; safe only because the conversation is known.
TAGGED_BEFORE_SECONDS = 600.0
#: The measured median gap between a closing message and its verdict: what "closest" is measured from.
TYPICAL_GAP_SECONDS = 1.2
#: The first question version whose state is `fit_hand_off`'s (open_work.py `QUESTION_VERSION`).
FITTED_VERSION = 2
RETRY_SECONDS = 7 * 86_400
PASS_SECONDS = 30.0
STATE_KEY_CHARS = 16  # the REVIEW key: owned by plugins/laya/laya_bridge/review_data.py `_ledger_decisions`

THREAD_COLUMNS = (
    "file_id, session_id, source, profile, kind, title, cwd, git_branch, first_prompt, started_at, ended_at, human_turns"
)


@dataclass(frozen=True)
class Match:
    file_id: int
    seq: int
    method: str


def judged(text: str, state: str, *, tail: bool = True) -> str | None:
    """`state` or `tail` when this message is the one `state` was built from (module docstring)."""
    want = state.strip()
    if not want:
        return None
    if decide_state(text[-CLOSING_TEXT_MAX_CHARS:]).strip() == want:
        return "state"
    if tail and text.strip().endswith(want):
        return "tail"
    return None


def thread_files(conn: sqlite3.Connection, file_id: int) -> list[sqlite3.Row]:
    """The main files of `file_id`'s conversation, the thread's own first: the file and every file a
    resume copied it into (module docstring § THE THREAD). Empty for a file with no session row."""
    row = conn.execute(f"SELECT {THREAD_COLUMNS} FROM sessions WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        return []
    if row["kind"] != "main" or row["started_at"] is None:
        return [row]
    return conn.execute(
        f"SELECT {THREAD_COLUMNS} FROM sessions WHERE started_at = ? AND kind = 'main' "
        "AND first_prompt IS ? AND cwd IS ? ORDER BY ended_at, file_id",
        (row["started_at"], row["first_prompt"], row["cwd"]),
    ).fetchall() or [row]


def thread_of(conn: sqlite3.Connection, file_id: int) -> str | None:
    files = thread_files(conn, file_id)
    return files[0]["session_id"] if files else None


def _agent_messages(conn: sqlite3.Connection, file_id: int, low: float, high: float) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seq, ts, text FROM messages WHERE file_id = ? AND role = 'agent' "
        "AND ts BETWEEN ? AND ? ORDER BY ts DESC, seq DESC",
        (file_id, low, high),
    ).fetchall()


def match_tagged(conn: sqlite3.Connection, conversation: str, at: float, state: str, *, tail: bool = True) -> Match | None:
    files = conn.execute(
        "SELECT file_id FROM sessions WHERE session_id = ? AND kind = 'main' ORDER BY ended_at DESC, file_id DESC",
        (conversation,),
    ).fetchall()
    for (file_id,) in files:
        for message in _agent_messages(conn, file_id, at - TAGGED_BEFORE_SECONDS, at + AFTER_SECONDS):
            if judged(message["text"], state, tail=tail):
                return Match(file_id, message["seq"], "id")
    return None


def match_legacy(conn: sqlite3.Connection, at: float, state: str, *, tail: bool = True) -> Match | None:
    files = conn.execute(
        "SELECT file_id, ended_at FROM sessions WHERE kind = 'main' AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)",
        (at + AFTER_SECONDS, at - BEFORE_SECONDS),
    ).fetchall()
    found: dict[str, list[tuple[float, float, int, int]]] = {"state": [], "tail": []}
    for file_id, ended_at in files:
        for message in _agent_messages(conn, file_id, at - BEFORE_SECONDS, at + AFTER_SECONDS):
            method = judged(message["text"], state, tail=tail)
            if method:
                gap = abs((at - message["ts"]) - TYPICAL_GAP_SECONDS)
                found[method].append((gap, -(ended_at or 0.0), -file_id, message["seq"]))
    for method in ("state", "tail"):
        if found[method]:
            _gap, _ended, neg_file, seq = min(found[method])
            return Match(-neg_file, seq, method)
    return None


def _void_stale_links(conn: sqlite3.Connection) -> int:
    """A rewritten file's links (its `archive_generation` moved), and any link whose message is gone."""
    before = conn.total_changes
    conn.execute(
        "DELETE FROM laya_links WHERE file_id IN (SELECT l.file_id FROM laya_links l "
        "JOIN files f ON f.id = l.file_id WHERE l.generation IS NOT f.archive_generation)"
    )
    conn.execute(
        "DELETE FROM laya_links WHERE method != 'none' AND NOT EXISTS (SELECT 1 FROM messages m "
        "WHERE m.file_id = laya_links.file_id AND m.seq = laya_links.seq AND m.role = 'agent')"
    )
    return conn.total_changes - before


def _due_rows(conn: sqlite3.Connection, now: float, settled_before: float) -> list[sqlite3.Row]:
    """Verdicts never attempted, and unmatched ones still inside the retry window: newest first."""
    marks = ", ".join("?" for _ in LINKED_SOURCES)
    return conn.execute(
        "SELECT b.ts, b.data FROM bridge_rows b "
        "LEFT JOIN laya_links l ON l.decision_id = json_extract(b.data, '$.decision_id') "
        f"WHERE b.source IN ({marks}) AND b.kind = 'decision' AND json_valid(b.data) "
        "AND json_extract(b.data, '$.decision_id') IS NOT NULL AND (b.ts IS NULL OR b.ts <= ?) "
        "AND (l.decision_id IS NULL OR (l.method = 'none' AND l.at >= ?)) "
        "ORDER BY b.ts DESC",
        (*LINKED_SOURCES, settled_before - AFTER_SECONDS, now - RETRY_SECONDS),
    ).fetchall()


def link_laya_decisions(
    conn: sqlite3.Connection,
    now: float,
    *,
    settled_before: float | None = None,
    budget: float = PASS_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """One pass (module docstring). `settled_before`: when the transcripts were last walked (default
    `now`). The caller commits. Returns what it did, counts only."""
    settled = now if settled_before is None else settled_before
    counts = {"voided": _void_stale_links(conn), "linked": 0, "none": 0, "deferred": 0}
    deadline = clock() + budget
    threads: dict[int, str | None] = {}
    generations: dict[int, int | None] = {}
    rows = _due_rows(conn, now, settled)
    for i, row in enumerate(rows):
        if clock() > deadline:
            counts["deferred"] = len(rows) - i
            break
        try:
            data = json.loads(row["data"])
        except ValueError:
            continue
        decision_id = data.get("decision_id") if isinstance(data, dict) else None
        if not isinstance(decision_id, str) or not DECISION_ID.fullmatch(decision_id):
            continue
        state = data.get("state") if isinstance(data.get("state"), str) else ""
        digest = data.get("state_hash")
        state_key = digest[:STATE_KEY_CHARS] if isinstance(digest, str) and len(digest) == 64 else None
        tag = data.get("conversation")
        conversation = tag if isinstance(tag, str) and AGENT_SESSION_ID_RE.fullmatch(tag) else None
        version = data.get("question_version")
        tail = not (isinstance(version, int) and version >= FITTED_VERSION)
        at = row["ts"]
        match = None
        if at is not None and state.strip():
            match = (
                match_tagged(conn, conversation, at, state, tail=tail)
                if conversation
                else match_legacy(conn, at, state, tail=tail)
            )
        if match is None:
            conn.execute(
                "INSERT OR REPLACE INTO laya_links (decision_id, state_key, file_id, seq, conversation, "
                "thread, method, at, linked_at, generation) VALUES (?, ?, NULL, NULL, ?, NULL, 'none', ?, ?, NULL)",
                (decision_id, state_key, conversation, at, now),
            )
            counts["none"] += 1
            continue
        if match.file_id not in threads:
            threads[match.file_id] = thread_of(conn, match.file_id)
            held = conn.execute("SELECT archive_generation FROM files WHERE id = ?", (match.file_id,)).fetchone()
            generations[match.file_id] = held[0] if held else None
        own = conn.execute("SELECT session_id FROM sessions WHERE file_id = ?", (match.file_id,)).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO laya_links (decision_id, state_key, file_id, seq, conversation, thread, "
            "method, at, linked_at, generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                state_key,
                match.file_id,
                match.seq,
                own,
                threads[match.file_id],
                match.method,
                at,
                now,
                generations[match.file_id],
            ),
        )
        counts["linked"] += 1
    return counts
