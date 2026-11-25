import json

import pytest

from tldrack.profile_availability import ProfileAvailability
from tldrack.launch_pick import pick_launch_profile
from tldrack.profiles import Profile, ProfileRegistry
from test_session_routes import _record_tmux, _with_body, spawner  # noqa: F401


def test_pause_survives_restart_and_resume_keeps_other_profiles(tmp_path):
    path = tmp_path / "availability.json"
    store = ProfileAvailability(["a", "b"], path)
    assert store.view() == {"paused": [], "error": None}
    store.save({"profile": "a", "paused": True})
    store.save({"profile": "b", "paused": True})
    restarted = ProfileAvailability(["a", "b"], path)
    assert restarted.paused("a")
    assert restarted.save({"profile": "a", "paused": False})["paused"] == ["b"]


@pytest.mark.parametrize("body", [{}, [], {"profile": "missing", "paused": True}, {"profile": "a", "paused": "false"}])
def test_invalid_change_never_writes(tmp_path, body):
    path = tmp_path / "availability.json"
    store = ProfileAvailability(["a"], path)
    with pytest.raises(ValueError):
        store.save(body)
    assert not path.exists()
    assert not store.paused("a")


def test_failed_save_does_not_claim_success_or_change_availability(tmp_path, monkeypatch):
    store = ProfileAvailability(["a"], tmp_path / "availability.json")

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.profile_availability.os.replace", fail)
    with pytest.raises(OSError):
        store.save({"profile": "a", "paused": True})
    assert not store.paused("a")
    assert not list(tmp_path.iterdir())


def test_unreadable_configuration_fails_closed_and_can_be_repaired(tmp_path):
    path = tmp_path / "availability.json"
    path.write_text("{")
    store = ProfileAvailability(["a", "b"], path)
    assert store.view()["error"]
    assert store.view()["paused"] == ["a", "b"]
    store.save({"profile": "a", "paused": False})
    assert json.loads(path.read_text()) == {"paused": ["b"]}
    assert store.view()["error"] is None


def test_auto_never_falls_back_to_paused_accounts():
    rows = [
        {"id": "a", "label": "A", "kind": "claude", "cli_found": True, "paused": True},
        {"id": "b", "label": "B", "kind": "claude", "cli_found": True},
    ]
    assert pick_launch_profile(rows, [], 0).id == "b"
    rows[1]["paused"] = True
    result = pick_launch_profile(rows, [], 0)
    assert result.id is None
    assert result.ranking == ()


def test_registry_keeps_identity_but_refuses_new_launch(tmp_path):
    registry = ProfileRegistry([Profile("a", "A", "claude", str(tmp_path), True)], "a", home=tmp_path, source="file")
    registry.availability.save({"profile": "a", "paused": True})
    assert registry.ids() == ["a"]
    assert registry.profile_for_path(str(tmp_path / "chat.jsonl")) == "a"
    assert registry.claude_roots() == [str(tmp_path)]
    with pytest.raises(ValueError, match="paused"):
        registry.spawn_argv("a", "session", tmp_path)
    registry.set_launch_picker(lambda: {"id": "a", "ranking": []})
    assert registry.pick_launch() is None


def test_routes_pause_refuse_spawn_then_resume(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    assert spawner.GET_ROUTES["/profile-availability"] == "_handle_get_profile_availability"
    assert spawner.POST_ROUTES["/profile-availability"] == "_handle_save_profile_availability"
    _with_body(spawner, {"profile": "claude-a", "paused": True})._handle_save_profile_availability()
    assert spawner.recorded.status == 200
    assert spawner.recorded.body["paused"] == ["claude-a"]
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a"})._handle_spawn()
    assert spawner.recorded.status == 400
    assert calls == []
    _with_body(spawner, {"profile": "claude-a", "paused": False})._handle_save_profile_availability()
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a"})._handle_spawn()
    assert spawner.recorded.status == 201
    assert len(calls) == 1


def test_update_obeys_origin_guard_and_validates_body(spawner):
    _with_body(spawner, {"profile": "claude-a", "paused": "true"})._handle_save_profile_availability()
    assert spawner.recorded.status == 400
    spawner.headers = {"Origin": "https://hostile.example"}
    _with_body(spawner, {"profile": "claude-a", "paused": True})._handle_save_profile_availability()
    assert spawner.recorded.status == 403
    spawner._handle_get_profile_availability()
    assert spawner.recorded.body["paused"] == []
