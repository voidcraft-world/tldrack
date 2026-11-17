"""The rack's session search past the titles: ledger threads, the live tail, and the route's guards."""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from tldrack.ledger import reader
from tldrack.ledger.ingest import Root
from tldrack.ledger.rack_search import (
    SESSIONS_MAX,
    LiveTails,
    RowBinding,
    parse_query,
    rack_search,
)
from tldrack.ledger.store import connect
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.icons import SessionIconStore
from tldrack.models import SessionModelStore
from tldrack.session_names import SessionNameStore
from tldrack.session_records import SessionRecordStore, serialize_records
from test_laya_links import add_file, add_message
from test_ledger_bridge import handler

CONV = "5e551011-0000-4000-8000-00000000c001"
RESUMED = "5e551011-0000-4000-8000-00000000c002"
OTHER = "5e551011-0000-4000-8000-00000000c003"
CODEX = "019a0000-0000-7000-8000-00000000c004"


def claude_line(role: str, text: str, ts: str = "2026-11-11T10:00:00Z") -> bytes:
    content = text if role == "user" else [{"type": "text", "text": text}]
    entry = {"type": role, "timestamp": ts, "sessionId": CONV, "message": {"role": role, "content": content}}
    return (json.dumps(entry) + "\n").encode()


@pytest.fixture
def ledger(tmp_path):
    """vc-1 ran CONV, then was resumed into RESUMED (one thread); vc-2 ran OTHER."""
    root = tmp_path / "ledger"
    conn = connect(root)
    add_file(conn, 1, CONV, 100, 200, first_prompt="fix the harbor")
    add_file(conn, 2, RESUMED, 100, 300, first_prompt="fix the harbor")  # the resume copied the history
    add_file(conn, 3, OTHER, 500, 600, first_prompt="street lamps")
    add_message(conn, 1, 0, 110, "human", "fix the harbor islands, the atoll reads wrong")
    add_message(conn, 1, 1, 120, "agent", "The atoll shape comes from disposition_of.")
    add_message(conn, 2, 0, 250, "human", "now the z-fighting on the water plate")
    add_message(conn, 3, 0, 510, "human", "the street lamps float over the curb")
    conn.execute("INSERT INTO ingest_runs (started_at, finished_at) VALUES (10, 20)")
    conn.commit()
    conn.close()
    return root


def binder(**rows: RowBinding):
    return lambda sessions: [rows[name] for name in sessions if name in rows]


def run(root, q, sessions, rows, tails=None, roots=()):
    query = {"q": [q], "s": list(sessions)}
    return rack_search(root, query, binder(**rows), tails or LiveTails(), lambda: list(roots))


ROWS = {
    "vc-1": RowBinding("vc-1", "vc-1 Harbor atoll fix", conversation=RESUMED),
    "vc-2": RowBinding("vc-2", "vc-2 Street lamps", conversation=OTHER),
}


def test_chat_decides_only_what_the_title_does_not(ledger):
    status, body = run(ledger, "z-fighting", ["vc-1", "vc-2"], ROWS)
    assert status == 200 and set(body["matches"]) == {"vc-1"}
    assert "z-fighting on the water plate" in body["matches"]["vc-1"]["snippet"]
    assert body["ledger"] is True and body["indexed_at"] == 20
    # A title-only match is the rack's own to show; the route adds nothing for it.
    assert run(ledger, "harbor", ["vc-1"], ROWS)[1]["matches"] == {}


def test_terms_meet_across_title_messages_and_the_resumed_thread(ledger):
    # "atoll" from the title, "disposition_of" from the first file, "water" from the resume.
    _, body = run(ledger, "ATOLL disposition_of water", ["vc-1", "vc-2"], ROWS)
    assert set(body["matches"]) == {"vc-1"}
    # The snippet is the newest message that matched a term the title did not.
    assert "water plate" in body["matches"]["vc-1"]["snippet"]
    assert run(ledger, "disposition_of curb", ["vc-1", "vc-2"], ROWS)[1]["matches"] == {}


def test_a_row_without_a_record_joins_by_its_transcript_stem(ledger, tmp_path):
    path = tmp_path / f"{OTHER}.jsonl"
    path.write_bytes(b"")
    rows = {"vc-9": RowBinding("vc-9", "vc-9 ", transcript=str(path))}
    _, body = run(ledger, "curb", ["vc-9"], rows)
    assert "street lamps" in body["matches"]["vc-9"]["snippet"]


def test_the_live_tail_past_the_fold_is_searched_and_read_incrementally(ledger, tmp_path):
    path = tmp_path / f"{CONV}.jsonl"
    folded = claude_line("user", "fix the harbor islands")
    path.write_bytes(folded)
    conn = connect(ledger)
    conn.execute("UPDATE files SET path = ?, size = ? WHERE id = 1", (str(path), len(folded)))
    conn.commit()
    conn.close()
    with path.open("ab") as handle:
        handle.write(claude_line("assistant", "Renamed the SCUTTLE verb in branch-actions.ts", "2026-11-11T10:05:00Z"))
    rows = {"vc-1": RowBinding("vc-1", "vc-1 ", conversation=CONV, transcript=os.path.realpath(path))}
    tails = LiveTails()
    _, body = run(ledger, "scuttle", ["vc-1"], rows, tails)
    assert "SCUTTLE verb" in body["matches"]["vc-1"]["snippet"] and body["matches"]["vc-1"]["role"] == "agent"
    # A half-written line is not read until it is whole, and then only the new bytes are folded.
    with path.open("ab") as handle:
        handle.write(claude_line("user", "now the moat")[:-12])
    assert run(ledger, "moat", ["vc-1"], rows, tails)[1]["matches"] == {}
    with path.open("ab") as handle:
        handle.write(claude_line("user", "now the moat")[-12:])
    _, body = run(ledger, "moat", ["vc-1"], rows, tails)
    assert body["matches"]["vc-1"]["role"] == "human"
    # The folded part is the ledger's, never read twice: its text is found through the ledger only.
    assert len(tails.messages(os.path.realpath(path), "claude", len(folded))) == 2


def test_the_tail_window_is_bounded_and_cuts_at_a_whole_line(tmp_path):
    path = tmp_path / f"{CONV}.jsonl"
    path.write_bytes(
        b"".join(claude_line("user", f"line {i} " + "x" * 80) for i in range(50)) + claude_line("user", "the last thing said")
    )
    tails = LiveTails(tail_bytes=400)
    texts = [m.text for m in tails.messages(str(path), "claude", 0)]
    assert texts[-1] == "the last thing said" and 0 < len(texts) < 5
    assert all(text.startswith(("line", "the last")) for text in texts)


def test_without_a_ledger_the_tails_are_still_searched(tmp_path):
    path = tmp_path / f"{CONV}.jsonl"
    path.write_bytes(claude_line("user", "wire the OPS row"))
    rows = {"vc-1": RowBinding("vc-1", "vc-1 ", conversation=CONV, transcript=str(path))}
    for root in (None, tmp_path / "absent"):
        status, body = run(root, "ops row", ["vc-1"], rows)
        assert status == 200 and body["ledger"] is False and set(body["matches"]) == {"vc-1"}
    assert not (tmp_path / "absent").exists()


def test_an_unindexed_codex_conversation_is_found_under_its_account_root(tmp_path):
    base = tmp_path / ".codex"
    day = base / "sessions" / "2026" / "11" / "11"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-11-11T10-00-00-{CODEX}.jsonl"
    entry = {
        "type": "event_msg",
        "timestamp": "2026-11-11T10:00:00Z",
        "payload": {"type": "user_message", "message": "ship the harbor buoys"},
    }
    rollout.write_text(json.dumps(entry) + "\n")
    rows = {"codex-1": RowBinding("codex-1", "codex-1 ", conversation=CODEX)}
    roots = [Root("codex", "codex", base, ("sessions",))]
    _, body = run(None, "buoys", ["codex-1"], rows, roots=roots)
    assert "harbor buoys" in body["matches"]["codex-1"]["snippet"]


def test_queries_are_bounded():
    assert parse_query({"q": ["  Harbor  harbor ATOLL "], "s": ["vc-1"]}) == (["harbor", "atoll"], ["vc-1"])
    for query in (
        {"q": ["x" * 301]},
        {"q": ["a"], "s": ["bad name"]},
        {"q": ["a"], "s": [f"vc-{i}" for i in range(SESSIONS_MAX + 1)]},
        {"q": [" ".join(f"t{i}" for i in range(9))]},
    ):
        with pytest.raises(ValueError):
            parse_query(query)
    assert rack_search(None, {"q": ["a"], "s": ["bad name"]}, binder(), LiveTails(), list)[0] == 400
    assert rack_search(None, {"q": [" "], "s": ["vc-1"]}, binder(), LiveTails(), list)[1]["matches"] == {}


def test_a_busy_ledger_is_a_503(ledger):
    reader._READ_SLOTS.acquire()
    reader._READ_SLOTS.acquire()
    try:
        assert run(ledger, "water", ["vc-1"], ROWS)[0] == 503
    finally:
        reader._READ_SLOTS.release()
        reader._READ_SLOTS.release()


def test_the_route_joins_records_and_auto_titles(tmp_path, ledger, monkeypatch):
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(ledger))
    records_path = tmp_path / "records.json"
    records_path.write_text(
        serialize_records(
            {
                "vc-1": {
                    "cwd": str(tmp_path),
                    "profile": "claude",
                    "kind": "claude",
                    "agent_session_id": RESUMED,
                    "last_seen": time.time(),
                }
            }
        )
    )
    names = SessionNameStore(tmp_path / "names.json")
    assert names.title("vc-1", "Harbor atoll fix")
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="voidcraft",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        records=SessionRecordStore(records_path),
        auto_names=names,
    )
    h = object.__new__(cls)
    sent: dict = {}
    h._send_json = lambda status, body: sent.update(status=status, body=body)
    h.headers = {}
    # "atoll" is the auto title's, "water" the resumed conversation's.
    h.path = "/ledger/rack-search?q=atoll+water&s=vc-1&s=vc-2"
    h._handle_rack_search()
    assert sent["status"] == 200 and set(sent["body"]["matches"]) == {"vc-1"}
    h.headers = {"Sec-Fetch-Site": "cross-site"}
    h._handle_rack_search()
    assert sent["status"] == 403


def test_the_route_is_no_store(tmp_path, ledger, monkeypatch):
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(ledger))
    h, _ = handler(tmp_path, None)
    h.path = "/ledger/rack-search?q=water&s=vc-1"
    h.headers = {"Host": "127.0.0.1:7682"}
    h.requestline = "GET /ledger/rack-search HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.command = "GET"
    h.wfile = io.BytesIO()
    h.log_request = lambda *args: None
    del h._send_json
    h.do_GET()
    raw = h.wfile.getvalue().decode()
    assert " 200 " in raw and "Cache-Control: no-store" in raw


def test_only_the_chat_is_searched_never_machine_messages(ledger, tmp_path):
    conn = connect(ledger)
    add_message(conn, 3, 1, 520, "machine", "TS2322 in GitHarborPane.vue, from the typecheck hook")
    conn.commit()
    conn.close()
    assert run(ledger, "ts2322", ["vc-2"], ROWS)[1]["matches"] == {}
    path = tmp_path / f"{CONV}.jsonl"
    away = {
        "type": "system",
        "subtype": "away_summary",
        "timestamp": "2026-11-11T10:00:00Z",
        "content": "while you were away: plumbing",
    }
    path.write_bytes((json.dumps(away) + "\n").encode() + claude_line("user", "wire the OPS row"))
    rows = {"vc-1": RowBinding("vc-1", "vc-1 ", conversation=CONV, transcript=str(path))}
    assert run(None, "plumbing", ["vc-1"], rows)[1]["matches"] == {}
    assert set(run(None, "ops", ["vc-1"], rows)[1]["matches"]) == {"vc-1"}


def test_a_line_holding_every_term_is_the_snippet_over_a_newer_single_word(ledger, tmp_path):
    conn = connect(ledger)
    add_message(conn, 3, 1, 520, "agent", "the moat and the drawbridge both need a Z rung")
    add_message(conn, 3, 2, 530, "human", "moat only, newer")
    add_message(conn, 3, 3, 540, "agent", "drawbridge only, newest")
    conn.commit()
    conn.close()
    _, body = run(ledger, "moat drawbridge", ["vc-2"], ROWS)
    assert "moat and the drawbridge" in body["matches"]["vc-2"]["snippet"]
    # In the live tail too: the newest line with both beats a newer line with one.
    path = tmp_path / f"{CONV}.jsonl"
    path.write_bytes(
        claude_line("user", "fix the moat and the drawbridge")
        + claude_line("assistant", "drawbridge done", "2026-11-11T10:09:00Z")
    )
    rows = {"vc-1": RowBinding("vc-1", "vc-1 ", conversation=CONV, transcript=str(path))}
    _, body = run(None, "moat drawbridge", ["vc-1"], rows)
    assert body["matches"]["vc-1"]["snippet"] == "fix the moat and the drawbridge"
