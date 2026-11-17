"""A subagent's hooks report on the subagent, never on the main thread's row.

Measured 2026-10-15 (`main.py` § Subagent hooks): a question waited behind a
blue dot because every subagent PreToolUse repainted the asking row `busy`.
"""

from tldrack.agent_index import IndexEntry
from tldrack.main import StatusStore, is_subagent_hook, subagent_work_state
from tldrack.state_arbiter import HOLD_SECONDS, decide_correction

TOOL = {"zone": "web", "tool": "WebFetch", "detail": "webfetch"}


def test_agent_id_is_the_signal_and_agent_type_alone_is_not():
    assert is_subagent_hook({"agent_id": "a1b2", "agent_type": "Explore"})
    # `--agent` sessions carry agent_type on the MAIN thread.
    assert not is_subagent_hook({"agent_type": "code-reviewer"})
    assert not is_subagent_hook({})
    assert not is_subagent_hook({"agent_id": ""})
    assert not is_subagent_hook({"agent_id": 7})


def test_the_state_table():
    assert subagent_work_state("attention") is None
    assert subagent_work_state("free") == "background"
    assert subagent_work_state("background") == "background"
    assert subagent_work_state("busy") == "busy"
    assert subagent_work_state(None) == "busy"


def test_a_pending_question_is_held_and_its_blocked_clock_keeps_running():
    store = StatusStore()
    store.set("s", "attention", None)
    before = store._sessions["s"].copy()
    assert store.note_subagent_work("s", TOOL, model="claude-opus-5-5") is None
    after = store._sessions["s"]
    assert after["state"] == "attention"
    # `at` is the blocked duration notify.py's grace window reads; `seq` and
    # `wall_at` are what the arbiter compares. None of them may move.
    assert (after["at"], after["seq"], after["wall_at"]) == (before["at"], before["seq"], before["wall_at"])
    assert after["activity"] == before["activity"]
    assert after["model"] == "claude-opus-5-5"  # session-level, so it does move


def test_a_finished_turn_becomes_background_and_keeps_the_stops_clock():
    store = StatusStore()
    store.set("s", "free", None, activity=None)
    stop = store._sessions["s"]["wall_at"]
    previous, state = store.note_subagent_work("s", TOOL)
    assert (previous["state"], state) == ("free", "background")
    row = store._sessions["s"]
    assert row["state"] == "background"
    assert row["wall_at"] == stop
    assert row["activity"] is None  # no zone painted onto a finished turn


def test_a_busy_row_takes_the_activity_but_not_the_cwd_or_tasks():
    store = StatusStore()
    store.set("s", "busy", None, cwd="/repo", fold_tasks=lambda _: {"t": 1})
    _previous, state = store.note_subagent_work("s", TOOL)
    row = store._sessions["s"]
    assert state == "busy"
    assert row["activity"] == TOOL
    assert row["cwd"] == "/repo"
    assert row["tasks"] == {"t": 1}


def test_a_row_a_subagent_creates_is_outranked_by_the_index():
    store = StatusStore()
    _previous, state = store.note_subagent_work("s", TOOL)
    assert state == "busy"
    assert store.get("s")["state"] == "busy"
    assert store._sessions["s"]["wall_at"] == 0.0


def test_claudes_waiting_gets_through_while_subagents_keep_working():
    # Main thread: PreToolUse AskUserQuestion at t, which the index records as
    # `waiting` a moment later. Subagent hooks then keep landing — they must not
    # out-date the index, or rule 2 would let them pin the row on busy.
    store = StatusStore()
    store.set("s", "busy", None)
    asked = store._sessions["s"]["wall_at"]
    store.note_subagent_work("s", TOOL)
    store.note_subagent_work("s", TOOL)
    current = store.versions()["s"]
    index = IndexEntry("s", "attention", asked + 0.5, "input needed", None, 1, "x")
    assert decide_correction(current, index, asked + 0.5 + HOLD_SECONDS) == "attention"
