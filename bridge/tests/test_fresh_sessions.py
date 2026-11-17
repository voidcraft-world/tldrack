"""A new, unprompted conversation is `fresh`; its first turn ends that for good.

The rack drew a just-opened agent and one whose reply you had read with the
same ○ (`main.py` § Fresh sessions).
"""

from tldrack.main import KEEP, StatusStore, fresh_for_event

TOOL = {"zone": "web", "tool": "WebFetch", "detail": "webfetch"}


def test_which_hooks_speak_to_freshness():
    assert fresh_for_event("SessionStart", {"source": "startup"}) is True
    assert fresh_for_event("SessionStart", {"source": "clear"}) is True
    assert fresh_for_event("SessionStart", {}) is True  # Codex sends no source
    assert fresh_for_event("SessionStart", {"source": "resume"}) is False
    assert fresh_for_event("SessionStart", {"source": "compact"}) is False
    assert fresh_for_event("SessionEnd", {}) is False
    assert fresh_for_event("Stop", {}) is KEEP
    assert fresh_for_event("Notification", {}) is KEEP


def test_fresh_until_the_first_prompt_then_never_again():
    store = StatusStore()
    store.set("s", "free", None, fresh=True)
    assert store.get("s")["fresh"] is True
    # The idle nag maps to free and says nothing about turns.
    store.set("s", "free", None)
    assert store.get("s")["fresh"] is True
    store.set("s", "busy", None)
    assert store.get("s")["fresh"] is False
    store.set("s", "free", None)  # Stop: a read-or-unread reply, not a new session
    assert store.get("s")["fresh"] is False


def test_a_legacy_or_non_free_write_cannot_leave_it_standing():
    store = StatusStore()
    store.set("s", "attention", None, fresh=True)
    assert store.get("s")["fresh"] is False


def test_an_arbiter_correction_to_busy_clears_it():
    store = StatusStore()
    store.set("s", "free", None, fresh=True)
    seq = store._sessions["s"]["seq"]
    assert store.correct("s", seq, "busy", None) is not None
    assert store.get("s")["fresh"] is False


def test_an_arbiter_free_keeps_it_and_a_new_row_is_never_fresh():
    store = StatusStore()
    store.set("s", "free", None, fresh=True)
    assert store.correct("s", store._sessions["s"]["seq"], "free", None) is not None
    assert store.get("s")["fresh"] is True
    assert store.correct("t", None, "free", None) is not None
    assert store.get("t")["fresh"] is False


def test_subagent_work_ends_it():
    store = StatusStore()
    store.set("s", "free", None, fresh=True)
    store.note_subagent_work("s", TOOL)
    assert store._sessions["s"]["fresh"] is False


def test_rename_carries_it_and_an_unknown_session_is_not_fresh():
    store = StatusStore()
    store.set("old", "free", None, fresh=True)
    store.rename("old", "new")
    assert store.get("new")["fresh"] is True
    assert store.get("nobody")["fresh"] is False
