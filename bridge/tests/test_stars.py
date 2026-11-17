"""stars.py — the operator's stars, and the star a closed entry carries."""

import json

from tldrack.closed_sessions import CLOSED_MAX, ClosedSessionStore, clean_entry
from tldrack.stars import SessionStarStore, clean_starred_at

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _record(sid=SID):
    return {"cwd": "/tmp/wt", "profile": "claude-a", "kind": "claude", "agent_session_id": sid, "last_seen": 1.0}


def test_a_star_is_kept_with_its_time_and_survives_a_reload(tmp_path):
    path = tmp_path / "stars.json"
    clock = Clock()
    store = SessionStarStore(path, clock=clock)
    assert store.set("vc-3", True) is True
    clock.now += 60
    # Starring a starred session changes nothing: the first time stands.
    assert store.set("vc-3", True) is False
    assert SessionStarStore(path).get_all() == {"vc-3": 1_800_000_000.0}
    assert store.set("vc-3", False) is True
    assert store.set("vc-3", False) is False
    assert SessionStarStore(path).get_all() == {}


def test_a_close_takes_the_star_and_a_reopen_adopts_it_under_the_new_name(tmp_path):
    store = SessionStarStore(tmp_path / "stars.json", clock=Clock())
    store.set("vc-3", True)
    assert store.take("vc-3") == 1_800_000_000.0
    assert store.take("vc-3") is None and store.get_all() == {}
    store.adopt("vc-1", 1_800_000_000.0)
    assert store.get_all() == {"vc-1": 1_800_000_000.0}
    # An entry that closed without a star hands nothing back.
    store.adopt("vc-2", None)
    assert list(store.get_all()) == ["vc-1"]


def test_a_rename_moves_the_star(tmp_path):
    store = SessionStarStore(tmp_path / "stars.json", clock=Clock())
    store.set("vc-3", True)
    assert store.rename("vc-3", "nav-fix") is True
    assert list(store.get_all()) == ["nav-fix"]


def test_a_bad_entry_is_dropped_alone(tmp_path):
    path = tmp_path / "stars.json"
    path.write_text(
        json.dumps({"version": 1, "sessions": {"vc-1": 5.0, "vc-2": True, "vc-3": -1, "vc-4": "soon", "bad name": 5.0}})
    )
    assert SessionStarStore(path).get_all() == {"vc-1": 5.0}


def test_a_starred_time_is_a_positive_number():
    assert clean_starred_at(5) == 5.0
    assert [clean_starred_at(value) for value in (True, 0, -1, "5", None, float("nan"))] == [None] * 6


def test_a_closed_entry_carries_its_star_through_a_reload(tmp_path):
    path = tmp_path / "closed.json"
    store = ClosedSessionStore(path, clock=Clock())
    store.add("vc-3", _record(), "killed", "fix the nav sidebar", 1_799_999_000.0)
    ((_, entry),) = ClosedSessionStore(path, clock=Clock()).entries()
    assert entry["starred_at"] == 1_799_999_000.0
    # Sparse: an unstarred close writes no field, and a bad one is dropped alone.
    assert "starred_at" not in clean_entry({**_record(), "session": "vc-3", "closed_at": 5.0})
    assert "starred_at" not in clean_entry({**_record(), "session": "vc-3", "closed_at": 5.0, "starred_at": "x"})


def test_a_starred_close_is_never_dropped_for_room_but_still_ages_out(tmp_path):
    clock = Clock()
    store = ClosedSessionStore(tmp_path / "closed.json", clock=clock)
    store.add("keep-me", _record(sid=None), "killed", None, clock.now)
    for index in range(CLOSED_MAX + 3):
        clock.now += 1
        store.add(f"vc-{index}", _record(sid=None), "killed")
    names = [entry["session"] for _, entry in store.entries()]
    # The newest CLOSED_MAX unstarred closes, and the starred one beside them.
    assert len(names) == CLOSED_MAX + 1
    assert "keep-me" in names and "vc-0" not in names and f"vc-{CLOSED_MAX + 2}" in names
    clock.now += 400 * 86400
    assert store.entries() == []
