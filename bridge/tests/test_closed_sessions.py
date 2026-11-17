"""closed_sessions.py — the undo list behind the rack's ↺ CLOSED."""

import json

from tldrack.closed_sessions import (
    CLOSED_MAX,
    ClosedSessionStore,
    clean_entry,
    reopen_name,
)
from tldrack.session_records import RECORD_MAX_AGE_DAYS

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"
OTHER_SID = "6f1c2a3b-0000-4000-8000-1234567890ac"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _record(cwd="/tmp/wt", sid=SID, profile="claude-a"):
    return {"cwd": cwd, "profile": profile, "kind": "claude", "agent_session_id": sid, "last_seen": 1.0}


def test_a_close_is_kept_with_how_and_label_and_survives_a_reload(tmp_path):
    path = tmp_path / "closed.json"
    store = ClosedSessionStore(path, clock=Clock())
    closed_id = store.add("vc-3", _record(), "killed", "fix the nav sidebar")
    assert closed_id is not None

    reloaded = ClosedSessionStore(path, clock=Clock())
    ((key, entry),) = reloaded.entries()
    assert key == closed_id
    assert entry["session"] == "vc-3" and entry["how"] == "killed"
    assert entry["label"] == "fix the nav sidebar"
    assert entry["agent_session_id"] == SID
    assert entry["closed_at"] == 1_800_000_000.0


def test_no_record_or_no_cwd_files_nothing(tmp_path):
    store = ClosedSessionStore(tmp_path / "closed.json", clock=Clock())
    assert store.add("vc-3", None, "killed") is None
    assert store.add("vc-3", {**_record(), "cwd": None}, "gone") is None
    assert store.entries() == []


def test_a_shell_that_ended_on_its_own_is_not_a_close(tmp_path):
    store = ClosedSessionStore(tmp_path / "closed.json", clock=Clock())
    assert store.add("deploy-dev", _record(sid=None), "gone") is None
    assert store.add("deploy-dev", _record(sid=None), "killed") is not None


def test_one_conversation_is_one_entry_and_the_newest_close_wins(tmp_path):
    clock = Clock()
    store = ClosedSessionStore(tmp_path / "closed.json", clock=clock)
    store.add("vc-3", _record(), "killed")
    clock.now += 60
    newest = store.add("vc-3-2", _record(), "gone")
    ((key, entry),) = store.entries()
    assert key == newest and entry["session"] == "vc-3-2"


def test_shells_without_a_conversation_are_never_merged(tmp_path):
    store = ClosedSessionStore(tmp_path / "closed.json", clock=Clock())
    store.add("sh-1", _record(sid=None), "killed")
    store.add("sh-1", _record(sid=None), "killed")
    assert len(store.entries()) == 2


def test_newest_first_capped_at_closed_max(tmp_path):
    clock = Clock()
    store = ClosedSessionStore(tmp_path / "closed.json", clock=clock)
    for n in range(CLOSED_MAX + 5):
        clock.now += 1
        store.add(f"s-{n}", _record(sid=None), "killed")
    names = [entry["session"] for _key, entry in store.entries()]
    assert len(names) == CLOSED_MAX
    assert names[0] == f"s-{CLOSED_MAX + 4}" and names[-1] == "s-5"


def test_entries_age_out_with_the_reboot_records(tmp_path):
    clock = Clock()
    store = ClosedSessionStore(tmp_path / "closed.json", clock=clock)
    store.add("vc-3", _record(), "killed")
    clock.now += RECORD_MAX_AGE_DAYS * 86400 + 1
    assert store.entries() == []


def test_take_consumes_the_entry(tmp_path):
    store = ClosedSessionStore(tmp_path / "closed.json", clock=Clock())
    closed_id = store.add("vc-3", _record(), "killed")
    assert store.take(closed_id)["session"] == "vc-3"
    assert store.take(closed_id) is None
    assert store.entries() == []


def test_a_hand_edited_file_is_revalidated(tmp_path):
    path = tmp_path / "closed.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "closed": {
                    "a1b2c3d4e5f6": {
                        "session": "ok",
                        "cwd": "/tmp",
                        "closed_at": 1_800_000_000.0,
                        "agent_session_id": "--dangerously-skip-permissions",
                        "how": "nuked",
                        "label": "bad\x07label",
                    },
                    "not-an-id": {"session": "ok", "cwd": "/tmp", "closed_at": 1_800_000_000.0},
                    "b1b2c3d4e5f6": {"session": "bad name", "cwd": "/tmp", "closed_at": 1_800_000_000.0},
                    "c1b2c3d4e5f6": {"session": "rel", "cwd": "relative/dir", "closed_at": 1_800_000_000.0},
                },
            }
        )
    )
    ((key, entry),) = ClosedSessionStore(path, clock=Clock()).entries()
    assert key == "a1b2c3d4e5f6"
    assert entry["agent_session_id"] is None
    assert entry["how"] == "gone"
    assert "label" not in entry


def test_clean_entry_needs_a_name_a_cwd_and_a_time():
    assert clean_entry({"session": "x", "cwd": "/tmp"}) is None
    assert clean_entry({"session": "x", "closed_at": 5.0}) is None
    assert clean_entry({"cwd": "/tmp", "closed_at": 5.0}) is None
    assert clean_entry({"session": "x", "cwd": "/tmp", "closed_at": True}) is None


def test_reopen_name_keeps_a_free_original():
    assert reopen_name("wt-fix-nav", {"vc-1"}) == "wt-fix-nav"


def test_a_reused_generated_name_takes_the_lowest_free_generated_one():
    assert reopen_name("vc-3", {"vc-1", "vc-3"}) == "vc-2"


def test_any_other_taken_name_is_suffixed():
    assert reopen_name("wt-fix-nav", {"wt-fix-nav", "wt-fix-nav-2"}) == "wt-fix-nav-3"


def test_reopen_name_gives_up_rather_than_loop_forever():
    taken = {"x"} | {f"x-{n}" for n in range(2, 100)}
    assert reopen_name("x", taken) is None


def test_a_closed_crew_keeps_both_modes_and_a_bad_one_is_dropped(tmp_path):
    path = tmp_path / "closed.json"
    store = ClosedSessionStore(path, clock=Clock())
    crew = store.add("wt-lead-a", {**_record(), "launch_permission_mode": "auto", "live_permission_mode": "manual"}, "killed")
    plain = store.add("vc-3", _record(sid=OTHER_SID), "killed")
    entries = dict(ClosedSessionStore(path, clock=Clock()).entries())
    assert entries[crew]["launch_permission_mode"] == "auto"
    assert entries[crew]["live_permission_mode"] == "manual"
    assert not {"launch_permission_mode", "live_permission_mode"} & set(json.loads(path.read_text())["closed"][plain])
    forged = clean_entry({**_record(), "session": "vc-3", "closed_at": 1.0, "launch_permission_mode": "bypassPermissions"})
    assert "launch_permission_mode" not in forged
