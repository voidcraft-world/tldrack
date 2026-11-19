"""The routes through the host's own entry (`handle_request`), headers and all."""

from __future__ import annotations

import json

import pytest

from laya_bridge import lab_jobs, paths, review_log, rows
from laya_bridge.plugin import LayaPlugin

LOOPBACK = {"host": "127.0.0.1:7682"}
DECK = {**LOOPBACK, "origin": "http://localhost:5173", "content-type": "application/json"}
SPA = {**LOOPBACK, "origin": "http://localhost:5173", "sec-fetch-site": "cross-site", "content-type": "application/json"}
SURFACE = "agent_open_work"


@pytest.fixture(autouse=True)
def public_golden(tmp_path, monkeypatch):
    """A synthetic public suite with nothing provisional: the repo's own would deal its sign-off cards here."""
    folder = tmp_path / "public-golden"
    folder.mkdir()
    case = {"id": "case-done", "expected": False, "why": "all done", "tags": ["done"], "state": {"hand_off": "All shipped."}}
    (folder / "agent-open-work.jsonl").write_text("# public\n" + json.dumps(case) + "\n")
    monkeypatch.setattr(paths, "PUBLIC_GOLDEN_DIR", folder)


@pytest.fixture
def plugin(tree, fake_runtime):
    tree.path("data/x")
    return LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)


@pytest.fixture
def labelled(tree):
    """Two Sonnet-labelled texts in the sample stratum."""
    texts, i = [], 0
    while len(texts) < 2:
        text = f"route text {i}: phase two is not started"
        if rows.bucket(rows.state_key(text), "review-sample") < 50:
            texts.append(text)
        i += 1
    keys = tree.unlabelled(texts)
    tree.labels({keys[0]: True, keys[1]: False})
    return keys


def get(plugin, subpath, headers=DECK, **query):
    return plugin.handle_request("GET", subpath, {k: [v] for k, v in query.items()}, None, headers)


def post(plugin, subpath, body, headers=DECK):
    return plugin.handle_request("POST", subpath, {}, body, headers)


def assert_refusal(response, status, code):
    got_status, body = response
    assert got_status == status, body
    assert body["code"] == code and body["ok"] is False and body["v"] == 1
    assert body["error"] == body["message"], "the shared readers show `error` unchanged"


# -- the guard -------------------------------------------------------------------

POLICY_CALLS = [
    ("GET", "/ops", None),
    ("GET", "/review/queue", None),
    ("GET", "/review/item", None),
    ("GET", "/review/recent", None),
    ("POST", "/review/answer", {}),
    ("POST", "/review/retest", {}),
    ("POST", "/review/undo", {}),
    ("POST", "/lab", {}),
    ("POST", "/restart", {}),
    ("POST", "/install", {}),
    ("POST", "/start", {}),
    ("POST", "/stop", {}),
]


@pytest.mark.parametrize("method, subpath, body", POLICY_CALLS)
@pytest.mark.parametrize(
    "headers, code",
    [
        ({"host": "evil.example", "origin": "http://localhost:5173"}, "host_refused"),
        ({**DECK, "x-forwarded-for": "10.0.0.2"}, "host_refused"),
        ({**LOOPBACK, "origin": "https://evil.example"}, "origin_refused"),
        ({"host": "mymac.tail1234.ts.net", "tailscale-user-login": "someone"}, "host_refused"),
        ({**LOOPBACK, "sec-fetch-site": "cross-site"}, "no_cors_refused"),
    ],
)
def test_every_policy_route_refuses_a_foreign_caller(plugin, fake_runtime, method, subpath, body, headers, code):
    assert_refusal(plugin.handle_request(method, subpath, {}, body, headers), 403, code)
    assert fake_runtime.calls == [], "nothing ran"


def test_a_foreign_origin_learns_the_allowed_list(plugin):
    status, body = get(plugin, "/ops", {**LOOPBACK, "origin": "https://evil.example"})
    assert status == 403 and "http://localhost:5173" in body["allowed_origins"]


@pytest.mark.parametrize("subpath", ["/lab", "/review/answer", "/review/retest", "/review/undo"])
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded", "application/jsonx"])
def test_json_posts_need_a_json_media_type(plugin, subpath, content_type):
    headers = {k: v for k, v in DECK.items() if k != "content-type"}
    if content_type:
        headers["content-type"] = content_type
    assert_refusal(post(plugin, subpath, {}, headers), 415, "unsupported_media_type")
    assert post(plugin, subpath, {}, {**DECK, "content-type": "application/json; charset=utf-8"})[0] == 400


def test_the_in_process_pair_fails_closed_on_policy_routes(plugin):
    for subpath in ("/ops", "/review/queue", "/review/item", "/review/recent"):
        assert_refusal(plugin.handle_get(subpath, {}), 403, "host_too_old")
    for subpath in ("/review/answer", "/review/retest", "/review/undo", "/lab", "/restart"):
        assert_refusal(plugin.handle_post(subpath, {}, {}), 403, "host_too_old")


def test_unknown_routes_keep_the_404_string(plugin):
    assert get(plugin, "/nope") == (404, {"error": "not found"})
    assert post(plugin, "/nope", {}) == (404, {"error": "not found"})
    assert get(plugin, "/lab") == (404, {"error": "not found"})
    assert plugin.handle_get("/decide", {}) == (404, {"error": "not found"})


# -- host routes behave as before -------------------------------------------------


@pytest.mark.parametrize("headers", [None, {}, SPA])
def test_status_and_decide_are_unchanged_through_handle_request(plugin, fake_runtime, headers, decide):
    status, body = plugin.handle_request("GET", "/status", {}, None, headers)
    assert status == 200 and body["state"] == "ready" and body == fake_runtime.status()
    status, body = plugin.handle_request("POST", "/decide", {}, decide(), headers)
    assert status == 200 and body["answers"]["q"]["noul"] == 0.8
    assert set(body) >= {"decision_id", "model_id", "checkpoint"}
    status, body = plugin.handle_request("POST", "/decide", {}, {"state": "x"}, headers)
    assert status == 400 and body["error"] == "bad_request" and "questions" in body["detail"]


def test_the_spa_cannot_tag_a_rack_surface_but_may_tag_paste(plugin, decide):
    status, body = post(plugin, "/decide", decide(ledger={"surface": "agent_open_work"}), SPA)
    assert status == 400 and "ledger.surface" in body["detail"]
    status, body = post(plugin, "/decide", decide(ledger={"surface": "coordinator"}), SPA)
    assert status == 400 and "ledger.surface" in body["detail"]
    status, body = post(plugin, "/decide", decide(ledger={"surface": "paste"}), SPA)
    assert status == 200 and body["decision_id"]


def test_the_coordinator_and_the_rack_may_tag_their_surfaces(plugin, decide):
    coordinator = {**LOOPBACK, "content-type": "application/json", "user-agent": "Python-urllib/3.12"}
    status, body = post(plugin, "/decide", decide(ledger={"surface": "coordinator"}), coordinator)
    assert status == 200 and body["decision_id"]
    status, body = plugin.handle_post("/decide", {}, decide(ledger={"surface": "agent_open_work", "subject": "wt-x"}))
    assert status == 200 and body["decision_id"]


def test_react_goes_through_handle_request(plugin, decide):
    decision_id = post(plugin, "/decide", decide(ledger={"surface": "paste"}), SPA)[1]["decision_id"]
    assert post(plugin, "/react", {"decision_id": decision_id, "action": "shown"}, SPA)[0] == 200


def test_max_body_is_raised_for_long_hand_offs(plugin):
    assert plugin.max_body_bytes >= 256 * 1024


# -- the deck --------------------------------------------------------------------


def test_queue_lists_cards_and_stats(plugin, labelled):
    status, body = get(plugin, "/review/queue")
    assert status == 200 and body["ok"] is True and body["surface"] == SURFACE
    assert sorted(card["key"] for card in body["cards"]) == sorted(labelled)
    assert body["surfaces"][0]["to_review"] == 2 and body["sources"]["labels"] == "ok"
    assert body["generated_at"].endswith("Z")
    status, body = get(plugin, "/review/queue", limit="0")
    assert status == 200 and body["cards"] == [] and body["surfaces"][0]["to_review"] == 2


@pytest.mark.parametrize(
    "query",
    [{"limit": "51"}, {"limit": "-1"}, {"limit": "1.5"}, {"surface": "mail"}, {"stratum": "everything"}, {"cachebust": "1"}],
)
def test_queue_refuses_a_bad_query(plugin, query):
    assert_refusal(get(plugin, "/review/queue", **query), 400, "bad_query")


def test_queue_without_params_is_503(fake_runtime):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: None)
    status, body = get(plugin, "/review/queue")
    assert_refusal((status, body), 503, "params_unavailable")
    assert body["hint"]


def test_item_finds_a_card_or_says_unknown(plugin, labelled):
    status, body = get(plugin, "/review/item", surface=SURFACE, key=labelled[0])
    assert status == 200 and body["card"]["key"] == labelled[0] and body["card"]["reference"]["open"] is True
    assert_refusal(get(plugin, "/review/item", surface=SURFACE, key="0" * 16), 409, "unknown_item")
    assert_refusal(get(plugin, "/review/item", surface=SURFACE, key="ZZ"), 400, "bad_query")
    assert_refusal(get(plugin, "/review/item", surface=SURFACE), 400, "bad_query")


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"surface": SURFACE, "key": "0123456789abcdef", "answer": "agree"},
        {"surface": SURFACE, "key": "0123456789abcdef", "answer": "agree", "reference_open": True, "extra": 1},
        {"surface": SURFACE, "key": "not-a-key", "answer": "agree", "reference_open": True},
        {"surface": SURFACE, "key": "0123456789abcdef", "answer": "maybe", "reference_open": True},
        {"surface": SURFACE, "key": "0123456789abcdef", "answer": "agree", "reference_open": 1},
        {"surface": SURFACE, "key": "0123456789abcdef", "answer": "agree", "reference_open": "true"},
        {"surface": "mail", "key": "0123456789abcdef", "answer": "agree", "reference_open": True},
    ],
)
def test_answer_refuses_a_bad_body_without_echoing_it(plugin, body):
    status, out = post(plugin, "/review/answer", body)
    assert_refusal((status, out), 400, "bad_body")
    assert "not-a-key" not in out["message"] and "maybe" not in out["message"]


def test_answer_then_undo_round_trip(plugin, tree, labelled):
    key = labelled[0]
    status, body = post(plugin, "/review/answer", {"surface": SURFACE, "key": key, "answer": "flip", "reference_open": True})
    assert status == 200 and body["ok"] is True and body["card"] is None
    assert body["answer"]["key"] == key and body["answer"]["open"] is False and body["stats"]["reviewed"] == 1
    [row] = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]
    assert row["open"] is False and row["against"] == {"by": "claude-sonnet-5-5", "open": True} and row["stratum"] == "sample"
    assert row["held_out"] is True and row["laya"] is None

    assert_refusal(
        post(plugin, "/review/answer", {"surface": SURFACE, "key": key, "answer": "agree", "reference_open": True}),
        409,
        "already_answered",
    )
    status, again = post(plugin, "/review/answer", {"surface": SURFACE, "key": key, "answer": "agree", "reference_open": True})
    assert again["answer"]["answer_id"] == body["answer"]["answer_id"]

    status, undone = post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]})
    assert status == 200 and undone["answer"]["answer_id"] == body["answer"]["answer_id"]
    assert undone["card"]["key"] == key and undone["stats"]["reviewed"] == 0
    assert_refusal(
        post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]}), 409, "already_undone"
    )
    assert_refusal(post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": "a_0000000000000000"}), 409, "unknown_answer")
    assert_refusal(post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": "nope"}), 400, "bad_body")


def test_a_skip_records_no_open(plugin, tree, labelled):
    status, body = post(
        plugin, "/review/answer", {"surface": SURFACE, "key": labelled[1], "answer": "skip", "reference_open": False}
    )
    assert status == 200 and body["answer"]["open"] is None
    [row] = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]
    assert "open" not in row


def test_an_open_answer_records_whether_it_needed_the_operator(plugin, tree, labelled):
    """labelled[0]'s reference is OPEN, so AGREE records open — and may say who it was waiting on."""
    status, body = post(
        plugin,
        "/review/answer",
        {"surface": SURFACE, "key": labelled[0], "answer": "agree", "reference_open": True, "needs": "running"},
    )
    assert status == 200 and body["answer"]["open"] is True and body["answer"]["needs"] == "running"
    [row] = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]
    assert row["open"] is True and row["needs"] == "running"
    assert review_log.fold([row]).labels[labelled[0]]["open"] is True  # every count built on `open` is unchanged


def test_a_done_answer_records_that_its_answer_owes_a_read(plugin, tree, labelled):
    """📖 READ: FLIP against an OPEN reference records DONE, and `read` says the answer still owes the operator a
    read. To the open/done label it is a DONE like any other."""
    status, body = post(
        plugin,
        "/review/answer",
        {"surface": SURFACE, "key": labelled[0], "answer": "flip", "reference_open": True, "needs": "read"},
    )
    assert status == 200 and body["answer"]["open"] is False and body["answer"]["needs"] == "read"
    [row] = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]
    assert row["open"] is False and row["needs"] == "read"
    assert review_log.fold([row]).labels[labelled[0]]["open"] is False
    assert body["stats"]["three_way"]["read"] == 1


@pytest.mark.parametrize(
    "answer, reference_open, needs",
    [
        ("flip", True, "you"),  # records DONE
        ("agree", False, "you"),  # records DONE
        ("agree", True, "read"),  # records OPEN
        ("flip", False, "read"),  # records OPEN
        ("skip", True, "running"),  # records nothing
        ("skip", False, "read"),  # records nothing
        ("agree", True, "maybe"),  # not a value
    ],
)
def test_needs_is_refused_off_its_own_side(plugin, tree, labelled, answer, reference_open, needs):
    assert_refusal(
        post(
            plugin,
            "/review/answer",
            {"surface": SURFACE, "key": labelled[0], "answer": answer, "reference_open": reference_open, "needs": needs},
        ),
        400,
        "bad_body",
    )
    assert review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0] == []


@pytest.mark.parametrize("needs, answer", [("you", "flip"), ("read", "agree")])
def test_a_row_carrying_needs_off_its_side_is_not_folded(needs, answer):
    row = review_log.answer_row(
        answer_id="a_0123456789abcdef",
        at="2026-11-04T00:00:00Z",
        surface=SURFACE,
        key="0123456789abcdef",
        stratum="sample",
        answer=answer,
        against={"by": "claude-sonnet-5-5", "open": True},
        laya=None,
        decision_id=None,
        held_out=None,
        needs=needs,
    )
    assert review_log.fold([row]).bad_lines == 1


def test_answer_409s(plugin, labelled):
    assert_refusal(
        post(plugin, "/review/answer", {"surface": SURFACE, "key": "0" * 16, "answer": "agree", "reference_open": True}),
        409,
        "unknown_item",
    )
    status, body = post(
        plugin, "/review/answer", {"surface": SURFACE, "key": labelled[0], "answer": "agree", "reference_open": False}
    )
    assert_refusal((status, body), 409, "reference_changed")
    assert body["card"]["key"] == labelled[0] and body["card"]["reference"]["open"] is True


def test_answer_413_when_the_log_is_full(plugin, labelled, monkeypatch):
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 0)
    assert_refusal(
        post(plugin, "/review/answer", {"surface": SURFACE, "key": labelled[0], "answer": "agree", "reference_open": True}),
        413,
        "review_full",
    )


def test_answer_without_a_data_folder_is_503(tree, fake_runtime):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    status, body = post(
        plugin, "/review/answer", {"surface": SURFACE, "key": "0" * 16, "answer": "agree", "reference_open": True}
    )
    assert_refusal((status, body), 503, "params_unavailable")


# -- restart ---------------------------------------------------------------------


def test_restart(plugin, fake_runtime):
    status, body = post(plugin, "/restart", {})
    assert status == 200 and body["ok"] is True and body["status"]["state"] == "ready"
    assert fake_runtime.calls == ["stop:restart", "ensure_started"]
    assert_refusal(post(plugin, "/restart", {"now": True}), 400, "bad_body")
    fake_runtime.installed = False
    assert_refusal(post(plugin, "/restart", None), 503, "not_installed")


def test_lifecycle_routes_answer_as_before_once_the_policy_passes(plugin, fake_runtime):
    assert post(plugin, "/install", {})[0] == 202 and post(plugin, "/stop", {})[0] == 200
    assert fake_runtime.calls == ["install", "stop:requested"]
    assert plugin.handle_post("/start", {}, None)[0] == 200


def test_through_the_real_host_dispatch(plugin, labelled, monkeypatch):
    """The SDK's own dispatcher: header names arrive lowercased, and every body is JSON it can send."""
    from voidcraft_world_bridge.plugins import dispatch_plugin, validate_plugin

    monkeypatch.setattr(lab_jobs, "list_panes", lambda: "")

    validate_plugin(plugin, "laya")
    registry = {"laya": plugin}
    headers = {"Host": "127.0.0.1:7682", "Origin": "http://localhost:5173", "Content-Type": "application/json"}
    status, body = dispatch_plugin(registry, "GET", "laya", "/review/queue", {}, None, headers)
    assert status == 200 and len(body["cards"]) == 2
    status, body = dispatch_plugin(
        registry,
        "POST",
        "laya",
        "/review/answer",
        {},
        {"surface": SURFACE, "key": labelled[0], "answer": "agree", "reference_open": True},
        headers,
    )
    assert status == 200 and body["answer"]["open"] is True
    status, body = dispatch_plugin(registry, "GET", "laya", "/ops", {}, None, headers)
    assert status == 200 and body["review"][0]["reviewed"] == 1
    status, body = dispatch_plugin(registry, "GET", "laya", "/ops", {}, None, {"Host": "evil.example"})
    assert status == 403 and body["code"] == "host_refused"


# -- the RECENT list --------------------------------------------------------------


def decide_text(plugin, text, **tag):
    """A rack turn end, through the in-process pair as tldrack sends it (FakeRuntime answers p = 0.8)."""
    return plugin.handle_post(
        "/decide",
        {},
        {
            "state": text,
            "questions": {"q": {"type": "noul", "instructions": "open?"}},
            "ledger": {"surface": SURFACE, "threshold": 0.7, "done_threshold": 0.4, "subject": "wt-x", **tag},
        },
    )


def test_recent_lists_live_verdicts_and_an_answer_from_it_lands(plugin):
    status, body = decide_text(plugin, "all done here, shipped")
    assert status == 200 and body["decision_id"]
    status, body = get(plugin, "/review/recent", surface=SURFACE)
    assert status == 200 and body["ok"] is True and body["surface"] == SURFACE
    [row] = body["rows"]
    assert row["subject"] == "wt-x" and row["done_threshold"] == 0.4 and row["operator"] is None
    assert body["summary"]["n"] == 1 and body["stats"]["surface"] == SURFACE
    status, answered = post(
        plugin,
        "/review/answer",
        {"surface": SURFACE, "key": row["key"], "answer": "flip", "reference_open": row["reference_open"]},
    )
    assert status == 200, answered
    [after] = get(plugin, "/review/recent")[1]["rows"]
    assert after["operator"]["answer"] == "flip" and after["operator"]["open"] is (not row["reference_open"])
    assert after["operator"] == answered["answer"], "one ref shape on every route, key included"


def test_recent_serves_the_conversation_the_rack_tagged(plugin):
    conversation = "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab"
    assert decide_text(plugin, "an older turn, no id")[0] == 200
    assert decide_text(plugin, "a tagged turn, shipped", conversation=conversation)[0] == 200
    rows_out = get(plugin, "/review/recent", surface=SURFACE)[1]["rows"]
    assert {row["text"]: row["conversation"] for row in rows_out} == {
        "a tagged turn, shipped": conversation,
        "an older turn, no id": None,
    }


def test_recent_takes_an_order_and_todo(plugin):
    decide_text(plugin, "all done here, shipped")
    status, body = get(plugin, "/review/recent", surface=SURFACE, order="edge", todo="1")
    assert status == 200 and body["summary"]["order"] == "edge" and body["summary"]["todo"] is True
    assert body["rows"][0]["edge"]["cut"] in ("open", "done")


def test_recent_narrows_to_one_session(plugin):
    decide_text(plugin, "all done here, shipped")
    status, body = get(plugin, "/review/recent", surface=SURFACE, subject="wt-x", since="1000000000")
    assert status == 200 and len(body["rows"]) == 1 and body["summary"]["subject"] == "wt-x"
    assert get(plugin, "/review/recent", surface=SURFACE, subject="wt-y")[1]["rows"] == []
    assert len(get(plugin, "/review/recent", surface=SURFACE, subject="wt-y,wt-x")[1]["rows"]) == 1
    assert get(plugin, "/review/recent", surface=SURFACE, subject="wt-x", since="4000000000")[1]["rows"] == []


def test_recent_pages_through_the_same_order_with_an_offset(plugin):
    for text in ("first turn, shipped", "second turn, shipped", "third turn, shipped"):
        assert decide_text(plugin, text)[0] == 200
    status, every = get(plugin, "/review/recent", surface=SURFACE)
    assert status == 200 and every["summary"]["offset"] == 0, "the offset is echoed even when it was not asked"
    status, body = get(plugin, "/review/recent", surface=SURFACE, limit="1", offset="1")
    assert status == 200 and body["summary"]["offset"] == 1 and body["summary"]["pool"] == 3
    assert [row["decision_id"] for row in body["rows"]] == [every["rows"][1]["decision_id"]]
    status, past = get(plugin, "/review/recent", surface=SURFACE, offset="100000")
    assert status == 200 and past["rows"] == [] and past["summary"]["pool"] == 3


def test_recent_pages_on_after_the_cursor_a_page_sent(plugin):
    for text in ("first turn, shipped", "second turn, shipped", "third turn, shipped"):
        assert decide_text(plugin, text)[0] == 200
    status, every = get(plugin, "/review/recent", surface=SURFACE)
    assert status == 200 and every["summary"]["next"] is None, "one page holds them all"
    status, first = get(plugin, "/review/recent", surface=SURFACE, limit="2")
    assert status == 200 and first["summary"]["next"].startswith("n~")
    status, rest = get(plugin, "/review/recent", surface=SURFACE, limit="2", after=first["summary"]["next"])
    assert status == 200 and rest["summary"]["next"] is None
    assert [row["decision_id"] for row in first["rows"] + rest["rows"]] == [row["decision_id"] for row in every["rows"]]


@pytest.mark.parametrize(
    "after",
    [
        "",
        "x",
        "n~",
        "n~2026-10-27T12:00:00Z",
        "n~2026-10-27T12:00:00Z~0123456789ABCDEF",
        "e~0.1~2026-10-27T12:00:00Z~0123456789abcdef",
        "n~2026-10-27T12:00:00Z~0123456789abcdef\n",
        "e~0.100000~2026-10-27T12:00:00Z~0123456789abcdef",
    ],
)
def test_recent_refuses_a_bad_cursor_without_echoing_it(plugin, after):
    """The last one is a good EDGE cursor, asked of the newest order: a cursor names a place in one ordering."""
    response = get(plugin, "/review/recent", after=after)
    assert_refusal(response, 400, "bad_query")
    assert response[1]["message"] == "after must be the summary.next of a page in the same order"
    assert get(plugin, "/review/recent", order="edge", after="e~0.100000~2026-10-27T12:00:00Z~0123456789abcdef")[0] == 200


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "x"},
        {"surface": "nope"},
        {"stratum": "sample"},
        {"order": "oldest"},
        {"todo": "yes"},
        {"subject": "a b"},
        {"subject": ",".join("abcdefghij")},
        {"subject": "a,"},
        {"since": "-1"},
        {"since": "17940219.5"},
    ],
)
def test_recent_refuses_a_bad_query(plugin, query):
    assert_refusal(get(plugin, "/review/recent", **query), 400, "bad_query")


@pytest.mark.parametrize("offset", ["-1", "100001", "999999", "1234567", "1.5", "x", "", " 1", "1\n", "١"])
def test_recent_refuses_a_bad_offset_without_echoing_it(plugin, offset):
    response = get(plugin, "/review/recent", offset=offset)
    assert_refusal(response, 400, "bad_query")
    assert response[1]["message"] == "offset must be a whole number from 0 to 100000"


def test_recent_without_params_is_unavailable(tree, fake_runtime):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: None)
    assert get(plugin, "/review/recent")[0] == 503


# -- waves -----------------------------------------------------


def wave_texts(n):
    """Turn ends outside `live_sample` (which never rides a wave), so all `n` form one card."""
    found, i = [], 0
    while len(found) < n:
        text = f"wave turn {i}: still waiting on three child agents"
        if rows.bucket(rows.state_key(text), "review-live") >= 10:
            found.append(text)
        i += 1
    return found


@pytest.fixture
def wave_card(plugin):
    """Three ⚠️ turn ends of one session seconds apart (p 0.8: `uncertain`), dealt as one card."""
    for text in wave_texts(3):
        assert decide_text(plugin, text)[0] == 200
    status, queue = get(plugin, "/review/queue", surface=SURFACE)
    assert status == 200 and len(queue["cards"]) == 1, "the riders leave their lanes"
    return queue["cards"][0]


def wave_answer(card, riders, answer="flip"):
    return {"surface": SURFACE, "key": card["key"], "answer": answer, "reference_open": card["reference"]["open"], "wave": riders}


def test_one_press_grades_a_whole_wave_and_one_undo_takes_it_back(plugin, tree, wave_card):
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    assert wave_card["wave"]["n"] == 3 and wave_card["stratum"] == "uncertain" and wave_card["edge"]["cut"] == "open"
    status, body = post(plugin, "/review/answer", wave_answer(wave_card, riders))
    assert status == 200 and body["message"] == "recorded ×3" and body["stats"]["reviewed"] == 3
    assert body["stats"]["last_answer_wave"] is True, "the deck's UNDO after a reload takes the whole press back"
    assert body["answer"]["key"] == wave_card["key"] and body["answer"]["wave_id"].startswith("w_")
    logged = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]
    assert {row["key"] for row in logged} == {wave_card["key"], *riders}
    assert {row["wave_id"] for row in logged} == {body["answer"]["wave_id"]} and {row["open"] for row in logged} == {False}

    status, undone = post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": body["answer"]["answer_id"], "wave": True})
    assert status == 200 and undone["message"] == "undone ×3" and undone["stats"]["reviewed"] == 0
    assert undone["card"]["key"] == wave_card["key"] and undone["card"]["wave"]["n"] == 3, "the card comes back whole"


def test_an_undo_after_a_reload_names_the_last_rider_and_still_deals_the_wave_back(plugin, wave_card):
    """A reloaded deck has no undo stack: it names `stats.last_answer_id`, the press's last row, which is a rider."""
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    status, body = post(plugin, "/review/answer", wave_answer(wave_card, riders))
    assert status == 200
    [stats] = [s for s in get(plugin, "/review/queue", surface=SURFACE)[1]["surfaces"] if s["surface"] == SURFACE]
    last = stats["last_answer_id"]
    assert stats["last_answer_wave"] is True and last != body["answer"]["answer_id"], "the last row written is a rider"
    status, undone = post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": last, "wave": True})
    assert status == 200 and undone["message"] == "undone ×3" and undone["answer"]["answer_id"] == last
    assert undone["card"]["key"] == wave_card["key"] and undone["card"]["wave"]["n"] == 3, "the lead's card, whole"


@pytest.mark.parametrize("cap, value", [("LOG_CAP_BYTES", 1), ("LOG_MAX_ROWS", 2)])
def test_a_wave_press_past_a_log_cap_records_nothing(plugin, tree, wave_card, monkeypatch, cap, value):
    """Checked row by row, the byte cap crossed after the lead refused the press with the lead already on disk, and the
    row cap, read off a fold that never moved, let all three rows past it. The whole press is checked first."""
    monkeypatch.setattr(review_log, cap, value)
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    assert_refusal(post(plugin, "/review/answer", wave_answer(wave_card, riders)), 413, "review_full")
    assert review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0] == [], "nothing was recorded"
    [card] = get(plugin, "/review/queue", surface=SURFACE)[1]["cards"]
    assert card["key"] == wave_card["key"] and card["wave"]["n"] == 3, "the wave is still dealt whole"


def test_a_wave_undo_past_a_log_cap_takes_nothing_back(plugin, tree, wave_card, monkeypatch):
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    status, body = post(plugin, "/review/answer", wave_answer(wave_card, riders))
    assert status == 200
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 5)  # three answer rows on disk, three undo rows to write
    assert_refusal(
        post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": body["answer"]["answer_id"], "wave": True}),
        413,
        "review_full",
    )
    assert len(review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]) == 3


def test_a_plain_undo_takes_back_one_member_and_leaves_its_siblings(plugin, tree, wave_card):
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    post(plugin, "/review/answer", wave_answer(wave_card, riders))
    rider_row = next(
        row for row in review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0] if row["key"] == riders[0]
    )
    status, undone = post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": rider_row["answer_id"]})
    assert status == 200 and undone["message"] == "undone" and undone["stats"]["reviewed"] == 2


def test_a_wave_that_changed_is_refused_with_the_fresh_card_and_nothing_is_recorded(plugin, tree, wave_card):
    riders = [m["key"] for m in wave_card["wave"]["members"]]
    status, body = post(plugin, "/review/answer", wave_answer(wave_card, riders[:1]))
    assert_refusal((status, body), 409, "wave_changed")
    assert body["card"]["wave"]["n"] == 3
    post(
        plugin, "/review/answer", {"surface": SURFACE, "key": riders[0], "answer": "agree", "reference_open": True}
    )  # graded on RECENT meanwhile
    assert_refusal(post(plugin, "/review/answer", wave_answer(wave_card, riders)), 409, "wave_changed")
    assert len(review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0]) == 1


def test_a_wave_card_answered_without_its_riders_grades_the_lead_alone(plugin, tree, wave_card):
    """An older page sends no `wave`: the lead is graded, and the riders form a smaller wave."""
    body = wave_answer(wave_card, [])
    del body["wave"]
    assert post(plugin, "/review/answer", body)[0] == 200
    [card] = get(plugin, "/review/queue", surface=SURFACE)[1]["cards"]
    assert card["wave"]["n"] == 2


@pytest.mark.parametrize(
    "wave", ["0123456789abcdef", ["golden:case-a"], ["0123456789abcdef"] * 2, [7], [f"{i:016x}" for i in range(12)]]
)
def test_a_bad_wave_is_refused_without_echoing_it(plugin, wave_card, wave):
    status, body = post(plugin, "/review/answer", wave_answer(wave_card, wave))
    assert_refusal((status, body), 400, "bad_body")
    assert "golden:case-a" not in body["message"]


def test_a_bad_wave_flag_on_undo_is_refused(plugin):
    assert_refusal(
        post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": "a_0123456789abcdef", "wave": "yes"}), 400, "bad_body"
    )


def test_a_wave_id_that_is_not_one_is_not_folded():
    row = review_log.answer_row(
        answer_id="a_0123456789abcdef",
        at="2026-11-09T00:00:00Z",
        surface=SURFACE,
        key="0123456789abcdef",
        stratum="uncertain",
        answer="agree",
        against={"by": "english", "open": True},
        laya=None,
        decision_id=None,
        held_out=None,
        wave_id="w_nope",
    )
    assert review_log.fold([row]).bad_lines == 1


# -- the blind retest ---------------------------------------------


@pytest.fixture
def graded(tree):
    """Three Sonnet-labelled texts the retest hash draws, each with an operator label of a month ago; a fourth labelled
    today. Written straight to the log: the routes stamp the real clock, and a retest waits a week."""
    texts, i = [], 0
    while len(texts) < 4:
        text = f"retest route text {i}: phase two is not started"
        if rows.bucket(rows.state_key(text), "review-retest") < 10:
            texts.append(text)
        i += 1
    keys = tree.unlabelled(texts)
    tree.labels(dict.fromkeys(keys, True))
    tree.path("data/x")
    labels = []
    for n, key in enumerate(keys):
        at = rows.utc_stamp() if n == 3 else f"2026-10-0{n + 1}T01:02:03Z"
        labels.append(
            review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at=at,
                surface=SURFACE,
                key=key,
                stratum="rest",
                answer="agree",
                against={"by": "claude-sonnet-5-5", "open": True},
                laya=None,
                decision_id=None,
                held_out=True,
                needs="you",
            )
        )
        tree.append_jsonl("data/agent-open-work-operator.jsonl", labels[-1])
    return labels


def unmoved(stats):
    """Everything a retest must never move: the counts, the strata, the labels' classes and both κ."""
    return {
        k: stats[k]
        for k in (
            "reviewed",
            "agree",
            "flip",
            "skip",
            "by_stratum",
            "operator_vs_sonnet",
            "operator_vs_laya",
            "sweep_ready",
            "three_way",
            "last_answer_id",
        )
    }


def test_the_queue_deals_blind_retests_before_its_cards_and_only_in_the_mix(plugin, graded):
    status, body = get(plugin, "/review/queue", surface=SURFACE, limit="20")
    assert status == 200 and [card["key"] for card in body["retests"]] == [graded[0]["key"], graded[1]["key"]]
    assert set(body["retests"][0]) == {"item_id", "key", "surface", "of", "text", "text_chars", "truncated", "at", "needs"}
    [stats] = body["surfaces"]
    assert stats["retest"]["due"] == 3 and (stats["retest"]["percent"], stats["retest"]["after_days"]) == (10, 7)
    assert get(plugin, "/review/queue", surface=SURFACE, limit="0")[1]["retests"] == []
    assert get(plugin, "/review/queue", surface=SURFACE, limit="20", stratum="rest")[1]["retests"] == []


def test_a_retest_is_recorded_without_moving_the_label_it_re_tests(plugin, tree, graded):
    first = graded[0]
    before = get(plugin, "/review/queue", surface=SURFACE, limit="0")[1]["surfaces"][0]
    body = {"surface": SURFACE, "key": first["key"], "of": first["answer_id"], "open": False}
    status, out = post(plugin, "/review/retest", body)
    assert status == 200 and out["ok"] is True and out["message"] == "recorded"
    assert out["retest"] == {
        "answer_id": out["retest"]["answer_id"],
        "key": first["key"],
        "of": first["answer_id"],
        "open": False,
        "needs": None,
    }
    assert unmoved(out["stats"]) == unmoved(before), "the second look said DONE; the label is still the first look's OPEN"
    assert out["stats"]["retest"]["due"] == 2 and out["stats"]["retest"]["pairs"] == 1
    fold = review_log.read_fold(tree.base, SURFACE)[1]
    assert fold.labels[first["key"]]["open"] is True and fold.labels[first["key"]]["needs"] == "you"
    written = review_log.read_operator_log(review_log.operator_path(tree.base, SURFACE))[0][-1]
    assert written == {
        "v": 1,
        "op": "retest",
        "answer_id": out["retest"]["answer_id"],
        "at": written["at"],
        "by": "operator",
        "surface": SURFACE,
        "key": first["key"],
        "of": first["answer_id"],
        "open": False,
    }

    assert_refusal(post(plugin, "/review/retest", body), 409, "already_retested")
    # UNDO names the retest's own id: the text is due again, and nothing else moved.
    status, undone = post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": out["retest"]["answer_id"]})
    assert status == 200 and undone["message"] == "retest undone" and undone["answer"] is None and undone["card"] is None
    assert undone["stats"]["retest"]["due"] == 3 and undone["stats"]["retest"]["pairs"] == 0
    assert unmoved(undone["stats"]) == unmoved(before)
    assert_refusal(
        post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": out["retest"]["answer_id"]}), 409, "already_undone"
    )
    assert post(plugin, "/review/retest", {**body, "open": True, "needs": "running"})[0] == 200, "retested again after the undo"


def test_a_retest_is_refused_unless_it_is_the_live_label_and_due(plugin, tree, graded):
    old, _second, _third, fresh = graded
    size = lambda: review_log.operator_path(tree.base, SURFACE).stat().st_size  # noqa: E731
    before = size()
    retest = lambda **change: post(
        plugin,
        "/review/retest",
        {  # noqa: E731
            "surface": SURFACE,
            "key": old["key"],
            "of": old["answer_id"],
            "open": True,
            **change,
        },
    )
    assert_refusal(retest(key=fresh["key"], of=fresh["answer_id"]), 409, "not_due")  # labelled today
    assert_refusal(retest(of="a_0000000000000000"), 409, "retest_stale")
    assert_refusal(retest(key="f" * 16), 409, "retest_stale")  # nobody labelled it
    for bad in (
        {"open": "yes"},
        {"of": "nope"},
        {"key": "not-a-key"},
        {"surface": "nope"},
        {"open": False, "needs": "you"},
        {"open": True, "needs": "read"},
        {"open": None, "needs": "you"},
        {"open": None, "needs": "read"},
        {"needs": "later"},
        {"extra": 1},
    ):
        status, out = retest(**bad)
        assert_refusal((status, out), 400, "bad_body")
        assert out["retest"] is None and out["stats"] is None
    assert_refusal(
        post(plugin, "/review/retest", {"surface": SURFACE, "key": old["key"], "of": old["answer_id"]}), 400, "bad_body"
    )
    assert size() == before, "a refused retest writes nothing"
    # The label is undone after its card was dealt: the card's `of` no longer names a live label.
    assert post(plugin, "/review/undo", {"surface": SURFACE, "answer_id": old["answer_id"]})[0] == 200
    assert_refusal(retest(), 409, "retest_stale")
    assert retest(open=None, key=graded[1]["key"], of=graded[1]["answer_id"])[1]["retest"]["open"] is None, "cannot say"
