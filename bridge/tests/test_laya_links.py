"""Laya verdicts linked to the conversation and message they judged (tldrack/ledger/laya_links.py)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from tldrack.ledger import store
from tldrack.ledger.ingest import Root, ingest
from tldrack.ledger.laya_links import RETRY_SECONDS, link_laya_decisions, thread_of
from tldrack.ledger.side_ledgers import SideLedger, parse_row
from tldrack.ledger.store import connect
from tldrack.open_work import decide_state

T0 = 1_794_000_000.0
SOURCE = "laya:agent_open_work"
A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
#: A long hand-off with a code block: its Laya state is FITTED (`fit_hand_off`), so it differs from the text.
LONG = (
    "Shipped phases A and B.\n\n```\n"
    + "log line\n" * 120
    + "```\n\n"
    + "\n".join(f"- step {i}: the band header now carries the title" for i in range(60))
    + "\n\nPhase C is not started yet; say go."
)


def iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def decision_line(at: float, state: str, *, conversation: str | None = None, version: int = 2, n: int = 1) -> bytes:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(at))
    row = {
        "kind": "decision",
        "v": 1,
        "decision_id": f"{stamp}-{n:012x}",
        "at": iso(at),
        "surface": "agent_open_work",
        "subject": "vc-8",
        "question_version": version,
        "state_hash": hashlib.sha256(state.encode()).hexdigest(),
        "state": state,
        "decisions": [{"q": "q", "type": "noul", "choice": "yes", "p": 0.9}],
        "ms": 40,
    }
    if conversation:
        row["conversation"] = conversation
    return (json.dumps(row) + "\n").encode()


def add_decision(conn: sqlite3.Connection, at: float, state: str, **kw) -> str:
    raw = decision_line(at, state, **kw)
    conn.execute(
        "INSERT INTO bridge_rows (source, row_key, ts, session, kind, data) VALUES (?, ?, ?, ?, ?, ?)",
        (SOURCE, *parse_row(SOURCE, raw)),
    )
    return json.loads(raw)["decision_id"]


def add_file(
    conn: sqlite3.Connection,
    file_id: int,
    session_id: str,
    started: float,
    ended: float | None,
    *,
    first_prompt: str = "Build the review bands",
    cwd: str = "/repo",
    kind: str = "main",
) -> None:
    conn.execute(
        "INSERT INTO files (id, path, source, role, first_seen) VALUES (?, ?, 'claude', 'transcript', 0)",
        (file_id, f"/p/{file_id}.jsonl"),
    )
    conn.execute(
        "INSERT INTO sessions (file_id, source, profile, session_id, kind, cwd, first_prompt, started_at, "
        "ended_at) VALUES (?, 'claude', 'claude', ?, ?, ?, ?, ?, ?)",
        (file_id, session_id, kind, cwd, first_prompt, started, ended),
    )


def add_message(conn: sqlite3.Connection, file_id: int, seq: int, ts: float, role: str, text: str, turn: int | None = 0) -> None:
    conn.execute(
        "INSERT INTO messages (file_id, seq, ts, role, turn_idx, text) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, seq, ts, role, turn, text),
    )


def links(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["decision_id"]: row for row in conn.execute("SELECT * FROM laya_links")}


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "ledger")
    yield connection
    connection.close()


def test_tagged_verdict_links_its_own_conversation_and_message(conn):
    for file_id, session_id in ((1, A), (2, B)):
        add_file(conn, file_id, session_id, T0, T0 + 30)
        add_message(conn, file_id, 0, T0, "human", "Build the review bands")
        add_message(conn, file_id, 1, T0 + 10, "agent", "Working on it.")
        add_message(conn, file_id, 2, T0 + 20, "agent", LONG)  # the same text in B, at the same moment
    decision = add_decision(conn, T0 + 21, decide_state(LONG[-2000:]), conversation=A)
    assert link_laya_decisions(conn, T0 + 100)["linked"] == 1
    link = links(conn)[decision]
    assert (link["method"], link["file_id"], link["seq"], link["conversation"], link["thread"]) == ("id", 1, 2, A, A)
    assert link["state_key"] == hashlib.sha256(decide_state(LONG[-2000:]).encode()).hexdigest()[:16]
    assert decide_state(LONG[-2000:]) != LONG[-2000:]  # the fitted state, not the raw tail


def test_tagged_verdict_skips_a_reply_after_the_verdict_and_never_crosses_conversations(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    add_message(conn, 1, 0, T0 + 20, "agent", "Done: the ledger is built.")
    add_message(conn, 1, 1, T0 + 24, "human", "next")
    add_message(conn, 1, 2, T0 + 25, "agent", "Starting the next thing.", turn=1)  # within at + 5, next turn
    add_file(conn, 2, B, T0, T0 + 30)
    add_message(conn, 2, 0, T0 + 20, "agent", "Elsewhere: all green.")
    found = add_decision(conn, T0 + 21, "Done: the ledger is built.", conversation=A, n=1)
    wrong = add_decision(conn, T0 + 21, "Elsewhere: all green.", conversation=A, n=2)
    link_laya_decisions(conn, T0 + 100)
    got = links(conn)
    assert (got[found]["method"], got[found]["seq"]) == ("id", 0)
    assert got[wrong]["method"] == "none" and got[wrong]["file_id"] is None


def test_legacy_verdict_links_by_state(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    add_message(conn, 1, 0, T0 + 20, "agent", LONG)
    decision = add_decision(conn, T0 + 21, decide_state(LONG[-2000:]))
    link_laya_decisions(conn, T0 + 100)
    link = links(conn)[decision]
    assert (link["method"], link["file_id"], link["seq"], link["conversation"]) == ("state", 1, 0, A)


def test_legacy_tail_is_for_pre_fit_verdicts_only(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    add_message(conn, 1, 0, T0 + 20, "agent", LONG)
    raw_tail = LONG[-2000:]  # question version 1 judged the raw tail; decide_state would refit it now
    v1 = add_decision(conn, T0 + 21, raw_tail, version=1, n=1)
    v2 = add_decision(conn, T0 + 21, raw_tail, version=2, n=2)
    link_laya_decisions(conn, T0 + 100)
    got = links(conn)
    assert (got[v1]["method"], got[v1]["seq"]) == ("tail", 0)
    assert got[v2]["method"] == "none"


def test_same_text_in_a_session_not_running_then_is_refused(conn):
    add_file(conn, 1, A, T0 - 7200, T0 - 3600)  # over an hour before the verdict
    add_message(conn, 1, 0, T0 - 3600, "agent", "Done: the ledger is built.")
    add_file(conn, 2, B, T0 - 600, T0 + 30)  # running at the verdict, but said it ten minutes earlier
    add_message(conn, 2, 0, T0 - 600, "agent", "Done: the ledger is built.")
    decision = add_decision(conn, T0 + 21, "Done: the ledger is built.")
    link_laya_decisions(conn, T0 + 100)
    assert links(conn)[decision]["method"] == "none"
    add_file(conn, 3, C, T0, T0 + 30)
    add_message(conn, 3, 0, T0 + 20, "agent", "Done: the ledger is built.")
    link_laya_decisions(conn, T0 + 200)
    assert (links(conn)[decision]["method"], links(conn)[decision]["file_id"]) == ("state", 3)


def test_a_resume_links_the_newer_file_under_one_thread(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    add_file(conn, 2, B, T0, T0 + 900)  # the resume: same start, first prompt and cwd, a new session id
    for file_id in (1, 2):  # the resume copied the history, timestamps and all
        add_message(conn, file_id, 0, T0, "human", "Build the review bands")
        add_message(conn, file_id, 1, T0 + 20, "agent", "Phase A shipped.")
    add_message(conn, 2, 2, T0 + 800, "human", "go on", turn=1)
    add_message(conn, 2, 3, T0 + 850, "agent", "Phase B shipped.", turn=1)
    early = add_decision(conn, T0 + 21, "Phase A shipped.", n=1)
    late = add_decision(conn, T0 + 851, "Phase B shipped.", n=2)
    link_laya_decisions(conn, T0 + 1000)
    got = links(conn)
    assert (got[early]["file_id"], got[early]["conversation"], got[early]["thread"]) == (2, B, A)
    assert (got[late]["file_id"], got[late]["thread"]) == (2, A)
    assert thread_of(conn, 1) == thread_of(conn, 2) == A
    add_file(conn, 3, C, T0, T0 + 900, cwd="/elsewhere")  # the same instant in another folder: its own thread
    assert thread_of(conn, 3) == C


def test_unmatched_is_retried_for_seven_days_then_left(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    soon = add_decision(conn, T0 + 21, "Phase A shipped.", n=1)
    late = add_decision(conn, T0 + 21, "Phase B shipped.", n=2)
    assert link_laya_decisions(conn, T0 + 100)["none"] == 2
    add_message(conn, 1, 0, T0 + 20, "agent", "Phase A shipped.")
    assert link_laya_decisions(conn, T0 + 3600)["linked"] == 1
    assert links(conn)[soon]["method"] == "state"
    add_message(conn, 1, 1, T0 + 20, "agent", "Phase B shipped.")
    counts = link_laya_decisions(conn, T0 + 21 + RETRY_SECONDS + 1)  # past the window: given up, not retried
    assert counts == {"voided": 0, "linked": 0, "none": 0, "deferred": 0}
    assert links(conn)[late]["method"] == "none"


def test_a_verdict_newer_than_the_walk_waits_for_the_next_pass(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    decision = add_decision(conn, T0 + 21, "Phase A shipped.")
    link_laya_decisions(conn, T0 + 100, settled_before=T0 + 22)  # the walk began just after the verdict
    assert decision not in links(conn)
    add_message(conn, 1, 0, T0 + 20, "agent", "Phase A shipped.")
    link_laya_decisions(conn, T0 + 1000, settled_before=T0 + 900)
    assert links(conn)[decision]["method"] == "state"


def test_a_rewritten_file_is_relinked(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    add_message(conn, 1, 0, T0, "human", "Build the review bands")
    add_message(conn, 1, 1, T0 + 20, "agent", "Phase A shipped.")
    decision = add_decision(conn, T0 + 21, "Phase A shipped.")
    link_laya_decisions(conn, T0 + 100)
    assert links(conn)[decision]["seq"] == 1
    # The file was rewritten (a new archive generation) and re-folded with different sequence numbers.
    conn.execute("UPDATE files SET archive_generation = 1 WHERE id = 1")
    conn.execute("DELETE FROM messages WHERE file_id = 1")
    add_message(conn, 1, 0, T0, "human", "Build the review bands")
    add_message(conn, 1, 1, T0 + 5, "agent", "Starting.")
    add_message(conn, 1, 2, T0 + 20, "agent", "Phase A shipped.")
    counts = link_laya_decisions(conn, T0 + 200)
    assert counts["voided"] == 1 and counts["linked"] == 1
    assert (links(conn)[decision]["seq"], links(conn)[decision]["generation"]) == (2, 1)
    # A link whose message vanished without a new generation is redone too.
    conn.execute("DELETE FROM messages WHERE file_id = 1 AND seq = 2")
    add_message(conn, 1, 3, T0 + 20, "agent", "Phase A shipped.")
    assert link_laya_decisions(conn, T0 + 300)["voided"] == 1
    assert links(conn)[decision]["seq"] == 3
    assert link_laya_decisions(conn, T0 + 400) == {"voided": 0, "linked": 0, "none": 0, "deferred": 0}


def test_the_pass_is_bounded_newest_first(conn):
    add_file(conn, 1, A, T0, T0 + 30)
    older = add_decision(conn, T0 + 10, "one", n=1)
    newer = add_decision(conn, T0 + 21, "two", n=2)
    ticks = iter([0.0, 0.0, 10.0])
    counts = link_laya_decisions(conn, T0 + 100, budget=5.0, clock=lambda: next(ticks))
    assert counts["deferred"] == 1 and newer in links(conn) and older not in links(conn)


def test_migrate_v1_to_v2_keeps_rows(tmp_path: Path):
    root = tmp_path / "ledger"
    root.mkdir()
    raw = sqlite3.connect(root / store.DB_NAME)
    raw.executescript(store._SCHEMA_V1)
    raw.execute("PRAGMA user_version = 1")
    raw.execute("INSERT INTO ingest_runs (started_at, finished_at) VALUES (1, 2)")
    raw.commit()
    raw.close()
    ro = connect(root, readonly=True)  # a reader never migrates
    assert ro.execute("SELECT name FROM sqlite_master WHERE name = 'laya_links'").fetchone() is None
    ro.close()
    conn = connect(root)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 2
    assert conn.execute("SELECT count(*) FROM ingest_runs").fetchone()[0] == 1
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'laya_links'")}
    assert {"laya_links_state_key", "laya_links_file"} <= indexes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO laya_links (decision_id, method) VALUES ('x', 'guess')")
    conn.close()


def test_ingest_links_on_every_pass_not_only_a_changed_one(tmp_path: Path):
    from test_ledger import claude_transcript

    claude = tmp_path / ".claude-a"
    session_dir = claude / "projects" / "-repo"
    session_dir.mkdir(parents=True)
    (session_dir / "s-1.jsonl").write_bytes(b"".join(claude_transcript()))
    base = 1793527200  # test_ledger's clock: "The task finished." lands at +405
    laya = tmp_path / "agent_open_work.jsonl"
    laya.write_bytes(
        decision_line(base + 406, "The task finished.", n=1) + decision_line(base + 406, "Not in any transcript.", n=2)
    )
    roots = [Root("claude", "claude-a", claude, ("projects",))]
    side = [SideLedger(SOURCE, laya)]
    ledger = tmp_path / "agent-ledger"
    first = ingest(ledger, roots=roots, side=side, now=lambda: base + 1000)
    assert first["laya_links"]["linked"] == 1 and first["laya_links"]["none"] == 1
    second = ingest(ledger, roots=roots, side=side, now=lambda: base + 2000)
    assert second["files_changed"] == 0 and second["laya_links"]["none"] == 1  # retried anyway
    assert "laya_links" not in ingest(ledger, roots=roots, side=side, only="claude", now=lambda: base + 3000)
    # A bridge-only pass reads no transcript, so it settles at the last FULL run's start (base + 2000), never at its
    # own start nor at the claude-only run's: a verdict from before that walk is attempted, a newer one waits.
    with laya.open("ab") as handle:
        handle.write(
            decision_line(base + 1500, "Walked past already.", n=3) + decision_line(base + 2100, "Written after the walk.", n=4)
        )
    older, newer = (json.loads(decision_line(at, "x", n=n))["decision_id"] for at, n in ((base + 1500, 3), (base + 2100, 4)))
    bridge_only = ingest(ledger, roots=roots, side=side, only="bridge", now=lambda: base + 4000)
    assert "laya_links" in bridge_only
    conn = connect(ledger)
    assert conn.execute("SELECT note FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "only:bridge"
    assert older in links(conn) and newer not in links(conn)
    conn.close()
    ingest(ledger, roots=roots, side=side, now=lambda: base + 5000)
    conn = connect(ledger)
    assert newer in links(conn)  # the next full walk attempts it
    linked = conn.execute(
        "SELECT l.method, m.text, t.origin FROM laya_links l JOIN messages m "
        "ON m.file_id = l.file_id AND m.seq = l.seq JOIN turns t "
        "ON t.file_id = m.file_id AND t.idx = m.turn_idx WHERE l.method != 'none'"
    ).fetchone()
    conn.close()
    assert tuple(linked) == ("state", "The task finished.", "task")
