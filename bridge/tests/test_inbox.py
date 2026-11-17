"""The decision inbox (inbox.py): ask once, blocking parks, KEEP holds, a contradiction reopens in the
answer's own transaction, dismiss, and the defer wake-up."""

import pytest

from tldrack.inbox import Inbox, InboxError, contradicts, fingerprint
from tldrack.lead.runs import LeadRuns
from tldrack.rack_db import RackDb


class Clock:
    def __init__(self, now: float = 1_900_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    db = RackDb(tmp_path / "rack.sqlite")
    inbox, runs = Inbox(db, clock=clock), LeadRuns(db, clock=clock)
    inbox.add_listener(runs.on_decision)
    run = runs.start(
        lead_identity="11111111-1111-4111-8111-111111111111",
        lead_session="lead-one",
        profile="a",
        title="T",
        plan_path=None,
        plan_text="# plan",
        run_dir=lambda rid: tmp_path / rid,
    )
    for sid in ("S1", "S2", "S3"):
        runs.add_step(run["id"], step_id=sid, title=f"step {sid}", brief="do it")
    return clock, inbox, runs, run["id"]


def ask(inbox, run_id, **kw):
    base = {
        "source": "lead",
        "run_id": run_id,
        "title": "Which table?",
        "detail": "for client errors",
        "blocking": False,
        "assumption": "page_cache",
        "steps": ["S1"],
    }
    return inbox.ask(**{**base, **kw})


def test_a_non_blocking_question_must_state_its_assumption(world):
    _, inbox, _, run_id = world
    with pytest.raises(InboxError):
        ask(inbox, run_id, assumption=None)
    assert ask(inbox, run_id, blocking=True, assumption=None)["outcome"] == "opened"


def test_ask_once_returns_the_open_row_then_the_answer(world):
    _, inbox, _, run_id = world
    first = ask(inbox, run_id)
    again = ask(inbox, run_id, title="  which   TABLE? ")
    assert again == {**first, "outcome": "existing"}
    inbox.answer(first["key"], "text", "a new table", via="http://localhost:5173")
    third = ask(inbox, run_id)
    assert third["outcome"] == "answered" and third["answer"] == "a new table" and third["key"] == first["key"]
    assert len(inbox.open_rows(run_id)) == 0, "asking again never opened a second row"


def test_blocking_parks_exactly_its_steps_until_answered(world):
    _, inbox, _, run_id = world
    key = ask(inbox, run_id, blocking=True, assumption=None, steps=["S2", "S3"])["key"]
    assert inbox.parked_steps(run_id) == {"S2", "S3"}
    assert inbox.open_for_step(run_id, "S2") == [key] and inbox.open_for_step(run_id, "S1") == []
    inbox.answer(key, "text", "go")
    assert inbox.parked_steps(run_id) == set()


def test_keep_holds_the_assumption_and_reopens_nothing(world):
    _, inbox, runs, run_id = world
    runs.update_step(run_id, "S1", state="published")
    result = inbox.answer(ask(inbox, run_id)["key"], "keep")
    assert result["contradicts"] is False and result["reopened"] == []
    assert runs.step(run_id, "S1")["state"] == "published"
    assert all(e["kind"] != "answered" for e in runs.unwoken(run_id)), "a held assumption wakes nobody"


def test_a_contradicting_answer_reopens_the_linked_work_in_one_transaction(world):
    _, inbox, runs, run_id = world
    runs.update_step(run_id, "S1", state="closed")
    runs.update_step(run_id, "S2", state="pending")
    key = ask(inbox, run_id, steps=["S1", "S2"])["key"]
    result = inbox.answer(key, "text", "a new table")
    assert result["contradicts"] is True and result["reopened"] == ["S1"], "pending work has nothing to reopen"
    assert runs.step(run_id, "S1")["state"] == "reopened"
    (event,) = [e for e in runs.unwoken(run_id) if e["kind"] == "answered"]
    assert event["detail"]["reopened"] == ["S1"]


def test_a_failing_listener_rolls_the_answer_back(world):
    _, inbox, _, run_id = world
    key = ask(inbox, run_id)["key"]

    def boom(conn, decision, verb):
        raise RuntimeError("listener failed")

    inbox.add_listener(boom)
    with pytest.raises(RuntimeError):
        inbox.answer(key, "keep")
    assert inbox.get(key)["state"] == "open", "the answer and its reaction commit together or not at all"


def test_options_keep_and_dismiss_rules(world):
    _, inbox, _, run_id = world
    key = ask(inbox, run_id, options=["page_cache", "new table"], assumption="page_cache")["key"]
    with pytest.raises(InboxError):
        inbox.answer(key, "option", "something else")
    assert inbox.answer(key, "option", "PAGE_CACHE")["contradicts"] is False
    blocking = ask(inbox, run_id, title="other", blocking=True, assumption=None)["key"]
    with pytest.raises(InboxError) as err:
        inbox.answer(blocking, "keep")
    assert err.value.status == 409
    with pytest.raises(InboxError):
        inbox.dismiss(blocking, "  ")
    assert inbox.dismiss(blocking, "not mine")["decision"]["state"] == "dismissed"
    with pytest.raises(InboxError) as err:
        inbox.answer(blocking, "text", "late")
    assert err.value.status == 409


def test_contradicts_is_the_press_not_a_reading():
    row = {"blocking": 0, "assumption": "page_cache"}
    assert contradicts(row, "keep", None) is False
    assert contradicts(row, "option", "page_cache") is False
    assert contradicts(row, "text", "use page_cache please") is True
    assert contradicts({"blocking": 1, "assumption": None}, "text", "x") is None
    assert fingerprint("A  b?", "c") == fingerprint("a b", "C!")


def test_a_withdrawn_question_can_be_asked_fresh(world):
    _, inbox, _, run_id = world
    first = ask(inbox, run_id)
    assert inbox.withdraw(first["key"], session="someone-else") is False
    assert inbox.withdraw(first["key"]) is True
    assert ask(inbox, run_id)["outcome"] == "opened"
