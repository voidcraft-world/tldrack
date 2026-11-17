"""Conversation retrieval boundaries: literal text, stable pointers, pagination and HTTP guards."""

from __future__ import annotations

import io
import json
import time

import pytest

from tldrack.ledger.laya_links import link_laya_decisions
from tldrack.ledger.reader import MESSAGE_PAGE, SEARCH_PAGE, read_history
from tldrack.ledger.store import connect
from tldrack.ledger import reader
from tldrack.open_work import decide_state
from test_laya_links import add_decision, add_file, add_message
from test_ledger_bridge import handler


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "ledger"
    conn = connect(root)
    for file_id, source in [(1, "claude"), (2, "codex")]:
        conn.execute(
            "INSERT INTO files (id,path,source,role,first_seen) VALUES (?,?,?,'transcript',0)",
            (file_id, f"/private/{file_id}.jsonl", source),
        )
        conn.execute(
            "INSERT INTO sessions (file_id,source,profile,session_id,kind,title) VALUES (?,?,?,?,?,?)",
            (file_id, source, source, "shared-session-id", "main" if file_id == 1 else "subagent", f"Thread {file_id}"),
        )
    for i in range(80):
        conn.execute(
            "INSERT INTO messages (file_id,seq,ts,role,text) VALUES (1,?,?,?,?)",
            (i * 10, i, "human" if i % 2 else "agent", f"message {i} about RACK history"),
        )
    conn.execute(
        "INSERT INTO messages (file_id,seq,ts,role,text) VALUES (2,7,100,'agent',?)",
        ("z" * 500 + ' a_b% /path/[file].ts "quoted" <script>bad()</script>',),
    )
    conn.execute("INSERT INTO ingest_runs (started_at,finished_at) VALUES (10,20)")
    conn.commit()
    conn.close()
    return root


def test_literal_search_and_context_snippet(history):
    for text in ("a_b%", "/path/[file].ts", '"quoted"', "<script>"):
        status, body = read_history(history, "search", {"q": [text]})
        assert status == 200 and len(body["hits"]) == 1
        assert text in body["hits"][0]["snippet"]
        assert body["hits"][0]["file_id"] == 2
    assert read_history(history, "search", {"q": ["NOT syntax*"]})[1]["hits"] == []
    status, body = read_history(history, "search", {"q": ["rack"], "file_id": ["2"]})
    assert status == 200 and body["hits"] == []


def test_recent_conversations_and_search_pagination(history):
    status, body = read_history(history, "search", {})
    assert status == 200 and [r["file_id"] for r in body["hits"]] == [2, 1]
    assert body["indexed_at"] == 20
    seen = []
    offset = 0
    while offset is not None:
        _, body = read_history(history, "search", {"q": ["rack"], "offset": [str(offset)]})
        assert len(body["hits"]) <= SEARCH_PAGE
        seen.extend(r["seq"] for r in body["hits"])
        offset = body["next_offset"]
    assert seen == list(range(790, -1, -10))


def test_context_window_and_bidirectional_pagination(history):
    status, page = read_history(history, "conversation", {"file_id": ["1"], "around": ["400"]})
    assert status == 200 and page["messages"][10]["seq"] == 400
    assert page["before"] == 300 and page["after"] == 590
    _, earlier = read_history(history, "conversation", {"file_id": ["1"], "before": [str(page["before"])]})
    _, later = read_history(history, "conversation", {"file_id": ["1"], "after": [str(page["after"])]})
    seqs = [m["seq"] for part in (earlier, page, later) for m in part["messages"]]
    assert seqs == list(range(0, 800, 10))
    assert earlier["before"] is None and later["after"] is None
    assert all(len(part["messages"]) <= MESSAGE_PAGE for part in (earlier, page, later))
    _, subagent = read_history(history, "conversation", {"file_id": ["2"]})
    assert len(subagent["messages"]) == 1  # shared session UUID must never mix conversation files


def test_message_pointer_survives_reingest(history):
    _, hits = read_history(history, "search", {"q": ["message 40 about"]})
    hit = hits["hits"][0]
    conn = connect(history)
    conn.execute("UPDATE messages SET id = id + 10000 WHERE file_id = 1")
    conn.commit()
    conn.close()
    _, page = read_history(history, "conversation", {"file_id": [str(hit["file_id"])], "around": [str(hit["seq"])]})
    assert page["messages"][10]["text"].startswith("message 40 ")


def test_first_user_is_independent_of_page_and_scoped_to_file(history):
    _, page = read_history(history, "conversation", {"file_id": ["1"], "before": ["790"]})
    assert page["first_user"] == {"seq": 10, "ts": 1.0, "role": "human", "text": "message 1 about RACK history"}
    assert page["first_user"]["seq"] < page["messages"][0]["seq"]
    _, other = read_history(history, "conversation", {"file_id": ["2"]})
    assert other["first_user"] is None  # Same session UUID, different file; no request to borrow.
    conn = connect(history)
    conn.execute("INSERT INTO messages (file_id,seq,ts,role,text) VALUES (2,8,101,'user','Original request')")
    conn.commit()
    conn.close()
    _, other = read_history(history, "conversation", {"file_id": ["2"]})
    assert other["first_user"]["text"] == "Original request"


@pytest.mark.parametrize(
    "route,query",
    [
        ("search", {"offset": ["-1"]}),
        ("search", {"file_id": ["../secret"]}),
        ("search", {"q": ["x" * 301]}),
        ("search", {"q": ["x", "y"]}),
        ("search", {"offset": ["9999999999999999999"]}),
        ("conversation", {}),
        ("conversation", {"file_id": ["1"], "before": ["5"], "after": ["6"]}),
    ],
)
def test_invalid_queries_are_rejected(history, route, query):
    assert read_history(history, route, query)[0] == 400


def test_missing_index_and_conversation_do_not_create_files(tmp_path, history):
    assert read_history(None, "search", {})[0] == 503
    absent = tmp_path / "absent"
    assert read_history(absent, "search", {})[0] == 503 and not absent.exists()
    assert read_history(history, "conversation", {"file_id": ["999"]})[0] == 404


def test_scans_are_bounded_and_release_their_slot(history, monkeypatch):
    reader._READ_SLOTS.acquire()
    reader._READ_SLOTS.acquire()
    try:
        assert read_history(history, "search", {})[0] == 503
    finally:
        reader._READ_SLOTS.release()
        reader._READ_SLOTS.release()
    monkeypatch.setattr(reader, "READ_SECONDS", -1)
    assert read_history(history, "search", {"q": ["rack"]})[0] == 503
    monkeypatch.setattr(reader, "READ_SECONDS", 2)
    assert read_history(history, "search", {"q": ["rack"]})[0] == 200


def test_route_and_no_cors_guard(tmp_path, history, monkeypatch):
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(history))
    h, sent = handler(tmp_path, None)
    h.path = "/ledger/search?q=a_b%25"
    h._handle_ledger_history()
    assert sent["status"] == 200 and len(sent["body"]["hits"]) == 1
    h.headers = {"Sec-Fetch-Site": "cross-site"}
    h._handle_ledger_history()
    assert sent["status"] == 403


def test_http_origin_host_and_cache_guards(tmp_path, history, monkeypatch):
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(history))
    for headers, expected in [
        ({"Host": "127.0.0.1:7682"}, 200),
        ({"Host": "evil.example"}, 403),
        ({"Host": "127.0.0.1:7682", "Origin": "https://evil.example"}, 403),
    ]:
        h, _ = handler(tmp_path, None)
        h.path = "/ledger/search?q=message"
        h.headers = headers
        h.requestline = "GET /ledger/search HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.command = "GET"
        h.wfile = io.BytesIO()
        h.log_request = lambda *args: None
        # Use the real responder so this also exercises no-store on success and errors.
        del h._send_json
        h.do_GET()
        raw = h.wfile.getvalue().decode()
        assert f" {expected} " in raw
        assert "Cache-Control: no-store" in raw
        if expected == 200:
            body = json.loads(raw.split("\r\n\r\n", 1)[1])
            assert body["hits"]
        else:
            assert "Thread 1" not in raw


# ── /ledger/threads: the Laya REVIEW page's batch join ─────────────────────────

S1 = "5e551011-0000-4000-8000-00000000a001"
ELSEWHERE = "5e551011-0000-4000-8000-00000000ffff"
NOW = float(int(time.time()) - 86_400)  # a day ago: inside the linker's retry window
PROMPT = "Plan the rack bands\n" + " ".join(f"word{i}" for i in range(600))
CLOSING = "\n".join(f"Line {i}: the thread header now shows the title and your first message." for i in range(60))
TURN_FIELDS = {
    "status",
    "thread",
    "conversation",
    "file_id",
    "closing_seq",
    "turn_idx",
    "human_ordinal",
    "woke_by",
    "request",
    "ask",
    "closing_tail",
    "closing_chars",
}
THREAD_FIELDS = {
    "thread",
    "title",
    "first_user",
    "started_at",
    "last_ts",
    "cwd",
    "branch",
    "profile",
    "source",
    "kind",
    "human_turns",
    "resumed",
}


@pytest.fixture
def linked(tmp_path):
    """One conversation: the operator's prompt, a plan, then a turn a task notification opened, judged by Laya."""
    root = tmp_path / "ledger"
    conn = connect(root)
    add_file(conn, 10, S1, NOW, NOW + 310, first_prompt=PROMPT[:500])
    conn.execute("UPDATE sessions SET title = 'Review bands', git_branch = 'main', human_turns = 1 WHERE file_id = 10")
    conn.executemany(
        "INSERT INTO turns (file_id, idx, origin, prompt_ts) VALUES (10, ?, ?, ?)", [(0, "human", NOW), (1, "task", NOW + 300)]
    )
    add_message(conn, 10, 0, NOW, "human", PROMPT, turn=0)
    add_message(conn, 10, 1, NOW + 10, "agent", "Plan: A, B, C.", turn=0)
    add_message(conn, 10, 2, NOW + 300, "machine", "<task-notification>\nthe build finished\n</task-notification>", turn=1)
    add_message(conn, 10, 3, NOW + 305, "agent", "Reading the build log.", turn=1)
    add_message(conn, 10, 4, NOW + 310, "agent", CLOSING, turn=1)
    decision = add_decision(conn, NOW + 311, decide_state(CLOSING[-2000:]), conversation=S1)
    link_laya_decisions(conn, NOW + 400)
    conn.execute("INSERT INTO ingest_runs (started_at, finished_at) VALUES (?, ?)", (NOW + 390, NOW + 400))
    conn.commit()
    key = conn.execute("SELECT state_key FROM laya_links WHERE decision_id = ?", (decision,)).fetchone()[0]
    conn.close()
    return root, decision, key


def test_threads_join_a_task_woken_turn(linked):
    root, decision, key = linked
    status, body = read_history(root, "threads", {"d": [decision], "k": [key], "c": [f"{S1},{ELSEWHERE}"]})
    assert status == 200 and set(body) == {"decisions", "keys", "conversations", "threads", "indexed_at"}
    turn = body["decisions"][decision]
    assert set(turn) == TURN_FIELDS
    assert (turn["status"], turn["thread"], turn["conversation"], turn["file_id"]) == ("linked", S1, S1, 10)
    assert (turn["closing_seq"], turn["turn_idx"], turn["human_ordinal"], turn["woke_by"]) == (4, 1, 1, "task")
    # The turn's request is what opened it (the task notification); the ask is the operator's last prompt.
    assert turn["request"]["seq"] == 2 and turn["request"]["role"] == "machine"
    assert turn["request"]["text"].startswith("<task-notification>") and "chars" not in turn["request"]
    assert turn["ask"]["seq"] == 0 and turn["ask"]["role"] == "human" and turn["ask"]["chars"] == len(PROMPT)
    assert len(turn["ask"]["text"]) <= 1_200 and PROMPT.startswith(turn["ask"]["text"])
    tail = turn["closing_tail"]
    assert turn["closing_chars"] == len(CLOSING) and len(tail) <= 1_200 and CLOSING.endswith(tail)
    assert CLOSING[-len(tail) - 1] == "\n" and tail.startswith("Line ")  # cut at a line, never mid-word
    assert body["keys"][key] == turn
    assert body["conversations"] == {S1: S1, ELSEWHERE: None}
    header = body["threads"][S1]
    assert set(header) == THREAD_FIELDS and set(body["threads"]) == {S1}
    assert (header["title"], header["started_at"], header["last_ts"], header["branch"]) == (
        "Review bands",
        NOW,
        NOW + 310,
        "main",
    )
    assert (header["source"], header["kind"], header["human_turns"], header["resumed"]) == ("claude", "main", 1, 0)
    assert header["first_user"]["seq"] == 0 and header["first_user"]["chars"] == len(PROMPT)
    assert len(header["first_user"]["text"]) <= 600
    assert body["indexed_at"] == NOW + 400


def test_first_user_is_the_operators_never_a_rack_typed_prompt(linked):
    """A rack-typed opener (a lead's launch line, a `<rack-wake`) keeps the human ROLE: relay.py
    marks only its turn's origin. Neither the thread header nor the conversation may call it yours."""
    root, decision, _ = linked
    conn = connect(root)
    conn.execute("UPDATE turns SET origin = 'relay' WHERE file_id = 10 AND idx = 0")
    conn.execute("INSERT INTO turns (file_id, idx, origin, prompt_ts) VALUES (10, 2, 'human', ?)", (NOW + 320,))
    add_message(conn, 10, 5, NOW + 320, "human", "now check the bands on a phone", turn=2)
    conn.commit()
    conn.close()
    _, body = read_history(root, "threads", {"d": [decision]})
    assert body["threads"][S1]["first_user"]["seq"] == 5
    assert body["threads"][S1]["first_user"]["text"] == "now check the bands on a phone"
    _, page = read_history(root, "conversation", {"file_id": ["10"]})
    assert page["first_user"]["seq"] == 5
    conn = connect(root)
    conn.execute("UPDATE turns SET origin = 'relay' WHERE file_id = 10 AND idx = 2")
    conn.commit()
    conn.close()
    _, body = read_history(root, "threads", {"d": [decision]})
    assert body["threads"][S1]["first_user"] is None  # every prompt was the rack's: no "You, first" at all


def test_threads_title_falls_back_to_the_first_prompt_line(linked):
    root, decision, _ = linked
    conn = connect(root)
    conn.execute("UPDATE sessions SET title = NULL")
    conn.commit()
    conn.close()
    _, body = read_history(root, "threads", {"d": [decision]})
    assert body["threads"][S1]["title"] == "Plan the rack bands"


def test_threads_statuses_say_what_to_do_next(linked):
    root, decision, _ = linked
    conn = connect(root)
    unread = add_decision(conn, NOW + 100, "Not linked yet.", n=7)  # read by the ingest, not yet attempted
    conn.execute(
        "INSERT INTO laya_links (decision_id, method, at, linked_at) VALUES "
        "('20200101T000000-00000000000a', 'none', ?, ?), ('20200101T000000-00000000000b', 'none', ?, ?)",
        (NOW, NOW, NOW - 8 * 86_400, NOW),
    )
    conn.commit()
    conn.close()
    old = time.strftime("%Y%m%dT%H%M%S", time.gmtime(NOW - 10 * 86_400)) + "-0000000000ff"
    new = time.strftime("%Y%m%dT%H%M%S", time.gmtime(NOW + 3_600)) + "-0000000000ff"
    ids = [unread, "20200101T000000-00000000000a", "20200101T000000-00000000000b", old, new]
    status, body = read_history(root, "threads", {"d": [",".join(ids)], "k": ["0123456789abcdef,golden:case-1"]})
    assert status == 200
    assert [body["decisions"][i]["status"] for i in ids] == ["pending", "pending", "none", "missing", "pending"]
    assert all(set(turn) == TURN_FIELDS and turn["thread"] is None for turn in body["decisions"].values())
    assert {k: v["status"] for k, v in body["keys"].items()} == {"0123456789abcdef": "missing", "golden:case-1": "missing"}
    assert body["threads"] == {}


@pytest.mark.parametrize(
    "query",
    [
        {"d": [",".join(f"20261101T000000-{i:012x}" for i in range(101))]},
        {"d": [",".join(f"20261101T000000-{i:012x}" for i in range(60))], "k": [",".join(f"{i:016x}" for i in range(41))]},
        {"d": ["nope"]},
        {"d": ["20261101T000000-abc"]},
        {"c": ["not-a-uuid"]},
        {"c": [f"{S1};drop"]},
        {"k": ["ABCDEF0123456789"]},
        {"k": ["abc"]},
        {"d": ["20261101T000000-000000000001", "20261101T000000-000000000002"]},
    ],
)
def test_threads_caps_and_shapes_are_400(linked, query):
    assert read_history(linked[0], "threads", query)[0] == 400


def test_threads_at_the_cap_and_empty(linked):
    root, decision, _ = linked
    ids = [decision] + [f"20261101T000000-{i:012x}" for i in range(99)]
    status, body = read_history(root, "threads", {"d": [",".join(ids)]})
    assert status == 200 and len(body["decisions"]) == 100
    status, body = read_history(root, "threads", {"d": [""]})
    assert status == 200 and body["decisions"] == body["keys"] == body["conversations"] == body["threads"] == {}
    assert read_history(root, "nope", {})[0] == 404


def test_threads_on_a_ledger_without_the_links_table(linked):
    root, decision, key = linked
    conn = connect(root)
    conn.execute("DROP TABLE laya_links")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    status, body = read_history(root, "threads", {"d": [decision], "k": [key], "c": [S1]})
    assert status == 200  # never 503: a read-only connection does not migrate
    assert body["decisions"][decision]["status"] == "missing" and body["keys"][key]["status"] == "missing"
    assert body["conversations"] == {S1: S1} and set(body["threads"]) == {S1}


def test_threads_route_guard_and_no_store(tmp_path, linked, monkeypatch):
    root, decision, _ = linked
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(root))
    h, sent = handler(tmp_path, None)
    h.path = f"/ledger/threads?d={decision}&c={S1}"
    h._handle_ledger_history()
    assert sent["status"] == 200 and sent["body"]["decisions"][decision]["status"] == "linked"
    h.headers = {"Sec-Fetch-Site": "cross-site"}  # a no-cors probe: refused before any SQL
    h._handle_ledger_history()
    assert sent["status"] == 403
    h, _ = handler(tmp_path, None)
    h.path = f"/ledger/threads?d={decision}"
    h.headers = {"Host": "127.0.0.1:7682"}
    h.requestline = "GET /ledger/threads HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.command = "GET"
    h.wfile = io.BytesIO()
    h.log_request = lambda *args: None
    del h._send_json
    h.do_GET()
    raw = h.wfile.getvalue().decode()
    assert " 200 " in raw and "Cache-Control: no-store" in raw
