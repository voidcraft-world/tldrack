"""session_records.py — what survives a reboot, and the drop rule."""

import json

import tldrack.session_records as records_module
from tldrack.session_records import RECORD_MAX_AGE_DAYS, SessionRecordStore

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"
DAY = 86400.0


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def row(session, cwd, profile="claude-a", live=True):
    return {"session": session, "cwd": cwd, "profile": profile, "live": live}


def kind_of(profile):
    return {"claude-a": "claude", "shell": "shell"}.get(profile)


def test_a_live_session_is_recorded_with_its_kind(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("wt-nav", str(tmp_path))], kind_of)
    on_disk = json.loads((tmp_path / "r.json").read_text())
    assert on_disk["sessions"]["wt-nav"] == {
        "cwd": str(tmp_path),
        "profile": "claude-a",
        "kind": "claude",
        "agent_session_id": None,
        "last_seen": 1_000_000.0,
    }


def test_a_dead_server_never_deletes_records(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("a", "/a"), row("b", "/b")], kind_of)
    store.observe_live([], kind_of)  # tmux server gone (a reboot), or `tmux ls` failed
    assert set(store.not_live(set())) == {"a", "b"}


def test_a_session_killed_by_hand_is_dropped_while_others_live(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("a", "/a"), row("b", "/b")], kind_of)
    store.observe_live([row("b", "/b")], kind_of)
    assert store.get("a") is None and store.get("b") is not None


def test_the_first_pass_after_a_start_has_no_baseline(tmp_path):
    first = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    first.observe_live([row("a", "/a"), row("b", "/b")], kind_of)
    # Bridge restart after a reboot: the user opens a new session before RESTORE.
    second = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    second.observe_live([row("vc-1", "/x")], kind_of)
    second.observe_live([row("vc-1", "/x")], kind_of)
    assert set(second.not_live({"vc-1"})) == {"a", "b"}


def test_old_records_expire_on_load_and_on_read(tmp_path):
    clock = Clock()
    store = SessionRecordStore(tmp_path / "r.json", clock=clock)
    store.observe_live([row("a", "/a")], kind_of)
    clock.now += (RECORD_MAX_AGE_DAYS + 1) * DAY
    assert store.not_live(set()) == {}
    assert SessionRecordStore(tmp_path / "r.json", clock=clock).get("a") is None


def test_an_unchanged_pass_does_not_write_until_last_seen_is_stale(tmp_path, monkeypatch):
    writes = []
    real = records_module.SessionStateFile._persist_locked
    monkeypatch.setattr(records_module.SessionStateFile, "_persist_locked", lambda self: writes.append(1) or real(self))
    clock = Clock()
    store = SessionRecordStore(tmp_path / "r.json", clock=clock)
    store.observe_live([row("a", "/a")], kind_of)
    for _ in range(5):
        clock.now += 3
        store.observe_live([row("a", "/a")], kind_of)
    assert len(writes) == 1
    clock.now += records_module.LAST_SEEN_WRITE_SECONDS
    store.observe_live([row("a", "/a")], kind_of)
    assert len(writes) == 2


def test_conversation_ids_are_uuid_only_and_cleared_on_request(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("a", "/a")], kind_of)
    store.note_agent_session("a", "--dangerously-skip-permissions")
    assert store.get("a")["agent_session_id"] is None
    store.note_agent_session("a", SID)
    assert store.get("a")["agent_session_id"] == SID
    store.clear_agent_session("a")
    assert store.get("a")["agent_session_id"] is None


def test_an_id_for_an_unrecorded_session_waits_for_its_first_pass(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.note_agent_session("never-seen", SID)
    assert store.get("never-seen") is None


def test_rename_moves_the_record_and_its_liveness(tmp_path):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("old", "/a"), row("other", "/b")], kind_of)
    store.rename("old", "new")
    store.observe_live([row("new", "/a"), row("other", "/b")], kind_of)
    assert store.get("new")["cwd"] == "/a"


def test_tolerant_load_drops_bad_fields_and_bad_rows(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "good": {
                        "cwd": "relative/dir",
                        "profile": "Bad Id",
                        "kind": "zsh",
                        "agent_session_id": "nope",
                        "last_seen": 1_000_000.0,
                    },
                    "bad name": {"cwd": "/a", "last_seen": 1_000_000.0},
                    "no-stamp": {"cwd": "/a"},
                },
            }
        )
    )
    store = SessionRecordStore(path, clock=Clock())
    assert store.get("good") == {"cwd": None, "profile": None, "kind": None, "agent_session_id": None, "last_seen": 1_000_000.0}
    assert store.get("bad name") is None and store.get("no-stamp") is None


def test_a_corrupt_file_is_ignored_not_rewritten(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{corrupt")
    SessionRecordStore(path, clock=Clock())
    assert path.read_text() == "{corrupt"


def test_the_launch_mode_follows_the_live_stamp_and_never_outlives_it(tmp_path):
    # A live row always sees the stamp (unset reads ""), so no stamp means "launched without one":
    # a name reused after a reboot must not inherit its predecessor's `auto`. Sparse on disk.
    path = tmp_path / "r.json"
    store = SessionRecordStore(path, clock=Clock())
    worker = {**row("wt-lead-a", "/a"), "launch_permission_mode": "auto"}
    store.observe_live([worker, row("vc-1", "/b")], kind_of)
    on_disk = json.loads(path.read_text())["sessions"]
    assert on_disk["wt-lead-a"]["launch_permission_mode"] == "auto"
    assert "launch_permission_mode" not in on_disk["vc-1"]
    store.observe_live([row("wt-lead-a", "/a"), row("vc-1", "/b")], kind_of)
    assert store.get("wt-lead-a").get("launch_permission_mode") is None, "re-entered in the default mode"
    store.observe_live([worker, {**row("vc-1", "/b"), "launch_permission_mode": "bypassPermissions"}], kind_of)
    assert store.get("vc-1").get("launch_permission_mode") is None


def test_the_live_mode_is_noted_from_hooks_kept_and_revalidated(tmp_path):
    path = tmp_path / "r.json"
    store = SessionRecordStore(path, clock=Clock())
    store.note_live_mode("wt-lead-a", "manual")  # no record yet: waits for its first pass
    store.observe_live([{**row("wt-lead-a", "/a"), "launch_permission_mode": "auto"}], kind_of)
    assert store.get("wt-lead-a").get("live_permission_mode") is None
    store.note_live_mode("wt-lead-a", "auto")
    store.note_live_mode("wt-lead-a", "plan")
    for junk in (None, "", "rm -rf /", 7):
        store.note_live_mode("wt-lead-a", junk)
    reloaded = SessionRecordStore(path, clock=Clock())
    assert reloaded.get("wt-lead-a")["live_permission_mode"] == "plan"
    assert reloaded.get("wt-lead-a")["launch_permission_mode"] == "auto"
    on_disk = json.loads(path.read_text())
    on_disk["sessions"]["wt-lead-a"].update(launch_permission_mode="--dangerously-skip-permissions", live_permission_mode="a b")
    path.write_text(json.dumps(on_disk))
    hand_edited = SessionRecordStore(path, clock=Clock()).get("wt-lead-a")
    assert hand_edited.get("launch_permission_mode") is None and hand_edited.get("live_permission_mode") is None


def test_an_unchanged_live_mode_never_rewrites_the_file(tmp_path, monkeypatch):
    store = SessionRecordStore(tmp_path / "r.json", clock=Clock())
    store.observe_live([row("wt-lead-a", "/a")], kind_of)
    store.note_live_mode("wt-lead-a", "auto")
    writes = []
    monkeypatch.setattr(store, "_persist_locked", lambda: writes.append(1))
    for _ in range(50):
        store.note_live_mode("wt-lead-a", "auto")
    assert writes == []
