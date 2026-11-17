"""The desk: answers delivered or queued, queues flushed, questions raised once."""

from __future__ import annotations

from conftest import IDLE_PANE, RULE, Clock, write_canvas, write_doc

from agent_canvas.delivery import QUESTION_MESSAGE_PREFIX
from agent_canvas.desk import QUEUE_TTL_SECONDS, RAISE_WINDOW_SECONDS, CanvasDesk

QUESTION = '<div class="choice" data-q="Ship it?"><button data-reply="yes">Y</button></div>'
KEY = "7-1760000000"


def make_desk(host, tmux, clock=None):
    clock = clock or Clock()
    return CanvasDesk(host, tmux=tmux, clock=clock), clock


def wire(tmux, host, *, state="free", pane=IDLE_PANE, session="vc-1"):
    tmux.keys[session] = KEY
    tmux.panes[session] = pane
    host.rows[session] = {"session": session, "state": state}


def test_fragment_view_reports_no_canvas_and_gone_sessions(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    assert desk.fragment_view("nobody") == {"rev": None, "gone": True, "view": "canvas"}
    tmux.keys["vc-1"] = KEY
    assert desk.fragment_view("vc-1") == {"rev": None, "key": KEY, "view": "canvas"}


def test_fragment_view_carries_the_question(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    view = desk.fragment_view("vc-1")
    assert view["question"] == "Ship it?"
    assert view["html"] == QUESTION
    assert view["answered"] is None and view["queued"] is None


def test_an_idle_agent_gets_the_answer_typed(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    status, body = desk.reply("vc-1", "yes", None, None)
    assert (status, body["delivered"]) == (200, True)
    assert tmux.typed == [("vc-1", "canvas › Ship it? → yes")]
    assert desk.fragment_view("vc-1")["answered"] == {"answers": {"q1": "yes"}}


def test_a_stale_rev_is_refused(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    status, _body = desk.reply("vc-1", "yes", "123", None)
    assert status == 409 and tmux.typed == []


def test_a_busy_agent_queues_then_the_tick_delivers(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host, state="busy")
    write_canvas(canvas_root, KEY, QUESTION)
    status, body = desk.reply("vc-1", "no", None, None)
    assert status == 202 and body["reason"] == "agent is working"
    assert desk.fragment_view("vc-1")["queued"] == {"answers": {"q1": "no"}, "reason": "agent is working"}
    assert desk.queued_sessions() == ["vc-1"]
    desk.tick()
    assert tmux.typed == []
    host.rows["vc-1"]["state"] = "free"
    desk.tick()
    assert tmux.typed == [("vc-1", "canvas › Ship it? → no")]
    assert desk.queued_sessions() == []


def test_a_second_click_replaces_the_queued_answer(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host, state="busy")
    write_canvas(canvas_root, KEY, QUESTION)
    desk.reply("vc-1", "no", None, None)
    desk.reply("vc-1", "yes", None, None)
    host.rows["vc-1"]["state"] = "free"
    desk.tick()
    assert tmux.typed == [("vc-1", "canvas › Ship it? → yes")]


def test_a_queued_answer_expires(canvas_root, tmux, host):
    desk, clock = make_desk(host, tmux)
    wire(tmux, host, state="busy")
    write_canvas(canvas_root, KEY, QUESTION)
    desk.reply("vc-1", "no", None, None)
    clock.now += QUEUE_TTL_SECONDS + 1
    host.rows["vc-1"]["state"] = "free"
    desk.tick()
    assert tmux.typed == [] and desk.queued_sessions() == []


def test_a_draft_in_the_box_queues(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host, pane=f"{RULE}\n❯ my own thought\n{RULE}\n")
    write_canvas(canvas_root, KEY, QUESTION)
    status, body = desk.reply("vc-1", "yes", None, None)
    assert status == 202 and "draft" in body["reason"]


def test_the_client_question_wins_for_a_second_choice(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    desk.reply("vc-1", "b", None, "Which branch?")
    assert tmux.typed == [("vc-1", "canvas › Which branch? → b")]


def test_a_waiting_agent_with_a_fresh_question_is_raised_once(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host, state="busy")
    write_canvas(canvas_root, KEY, QUESTION, mtime=clock.now + 5)
    clock.now += 10
    desk.tick()
    assert host.raised == []  # mid-turn: Stop would overwrite it
    host.rows["vc-1"]["state"] = "free"
    desk.tick()
    desk.tick()
    assert host.raised == [("vc-1", f"{QUESTION_MESSAGE_PREFIX}Ship it?")]


def test_a_canvas_from_before_startup_never_raises(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION, mtime=clock.now - 60)
    desk.tick()
    assert host.raised == []


def test_an_old_question_stops_raising(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION, mtime=clock.now + 1)
    clock.now += RAISE_WINDOW_SECONDS + 5
    desk.tick()
    assert host.raised == []


def test_an_answered_question_is_not_raised(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION, mtime=clock.now + 1)
    clock.now += 5
    desk.reply("vc-1", "yes", None, None)
    desk.tick()
    assert host.raised == []


def test_our_raised_attention_does_not_block_the_answer(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION, mtime=clock.now + 1)
    clock.now += 5
    desk.tick()
    assert host.rows["vc-1"]["state"] == "attention"
    status, _body = desk.reply("vc-1", "yes", None, None)
    assert status == 200


def test_a_gone_session_answers_410(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    del tmux.panes["vc-1"]
    assert desk.reply("vc-1", "yes", None, None)[0] == 410


# -- typed (JSON) canvases --------------------------------------------------

SHIP = {"type": "choice", "id": "ship", "q": "Ship it?", "options": ["yes", "no"]}
LAYOUT = {
    "type": "form",
    "id": "layout",
    "q": "Layout",
    "fields": [{"type": "select", "name": "pick", "label": "Pick", "options": ["A", "B"]}],
}


def test_a_doc_canvas_is_served_parsed_with_its_questions(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_doc(canvas_root, KEY, {"type": "stat", "label": "tests", "value": 41}, SHIP, LAYOUT, {"type": "chart"})
    view = desk.fragment_view("vc-1")
    assert view["kind"] == "doc" and "html" not in view
    assert [block["type"] for block in view["doc"]["blocks"]] == ["stat", "choice", "form"]
    assert view["errors"][0]["code"] == "unknown-block"
    assert view["questions"] == [{"id": "ship", "q": "Ship it?"}, {"id": "layout", "q": "Layout"}]
    assert view["question"] == "Ship it? (+1)"


def test_several_answers_are_typed_as_one_line(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_doc(canvas_root, KEY, SHIP, LAYOUT)
    status, _body = desk.reply_answers("vc-1", {"ship": "yes", "layout": {"pick": "B"}}, None)
    assert status == 200
    assert tmux.typed == [("vc-1", 'canvas › Ship it? → yes · Layout → {"pick":"B"}')]
    assert desk.fragment_view("vc-1")["answered"] == {"answers": {"ship": "yes", "layout": {"pick": "B"}}}


def test_an_answer_that_does_not_fit_is_refused_untyped(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_doc(canvas_root, KEY, SHIP, LAYOUT)
    status, body = desk.reply_answers("vc-1", {"layout": {"pick": "C"}}, None)
    assert status == 400 and "not one of its options" in body["error"]
    assert tmux.typed == []


def test_a_single_answer_on_a_doc_answers_its_first_question(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_doc(canvas_root, KEY, SHIP)
    assert desk.reply("vc-1", "no", None, None)[0] == 200
    assert tmux.typed == [("vc-1", "canvas › Ship it? → no")]


def test_answers_to_a_markup_canvas_use_its_one_id(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_canvas(canvas_root, KEY, QUESTION)
    assert desk.reply_answers("vc-1", {"q1": "yes"}, None)[0] == 200
    assert tmux.typed == [("vc-1", "canvas › Ship it? → yes")]


def test_the_newest_form_wins(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    write_doc(canvas_root, KEY, SHIP, mtime=1_000_000.0)
    write_canvas(canvas_root, KEY, "<p>newer markup</p>", mtime=1_000_010.0)
    assert desk.fragment_view("vc-1")["kind"] == "html"
    write_doc(canvas_root, KEY, SHIP, mtime=1_000_020.0)
    assert desk.fragment_view("vc-1")["kind"] == "doc"


def test_a_doc_question_raises_attention_with_its_summary(canvas_root, tmux, host):
    clock = Clock(start=1_000_000.0)
    desk, _ = make_desk(host, tmux, clock)
    wire(tmux, host)
    write_doc(canvas_root, KEY, SHIP, LAYOUT, mtime=clock.now + 1)
    clock.now += 5
    desk.tick()
    assert host.raised == [("vc-1", f"{QUESTION_MESSAGE_PREFIX}Ship it? (+1)")]


def test_check_view_reports_what_the_hook_prints(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    write_doc(canvas_root, KEY, {"type": "table", "colums": []})
    view = desk.check_view(KEY)
    assert view["ok"] is False and view["kind"] == "doc"
    assert {error["path"] for error in view["errors"]} == {"blocks[0].colums", "blocks[0].columns", "blocks[0].rows"}
    write_doc(canvas_root, KEY, SHIP)
    assert desk.check_view(KEY) == {"ok": True, "kind": "doc", "drawn": True, "errors": [], "questions": ["ship"]}


# ── the pane's two faces ─────────────────────────────────────────────────────


def test_the_view_starts_on_the_canvas_and_toggles_per_session(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    tmux.keys["vc-2"] = "8-1760000000"
    assert desk.fragment_view("vc-1")["view"] == "canvas"
    assert desk.set_view("vc-1", "toggle") == (200, {"session": "vc-1", "view": "skills"})
    assert desk.fragment_view("vc-1")["view"] == "skills"
    assert desk.fragment_view("vc-2")["view"] == "canvas"  # another agent's pane is untouched
    write_canvas(canvas_root, tmux.keys["vc-1"], "<p>drawn</p>")
    assert desk.fragment_view("vc-1")["view"] == "skills"  # a new canvas never yanks the audit away
    assert desk.set_view("vc-1", "toggle")[1]["view"] == "canvas"
    assert desk.set_view("vc-1", "canvas")[1]["view"] == "canvas"


def test_the_view_follows_the_key_through_a_rename(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    desk.set_view("vc-1", "skills")
    tmux.keys["renamed"] = tmux.keys.pop("vc-1")
    assert desk.fragment_view("renamed")["view"] == "skills"


def test_a_bad_view_or_a_gone_session_is_refused(canvas_root, tmux, host):
    desk, _ = make_desk(host, tmux)
    wire(tmux, host)
    assert desk.set_view("vc-1", "everything")[0] == 400
    assert desk.set_view("vc-1", None)[0] == 400
    assert desk.set_view("nobody", "skills")[0] == 404
    assert desk.fragment_view("nobody") == {"rev": None, "gone": True, "view": "canvas"}
