import json

import pytest

from tldrack.launch_directories import LaunchDirectories
from test_session_routes import _record_tmux, _with_body, spawner  # noqa: F401


def configuration(*paths):
    return {"directories": [{"label": path.name, "path": str(path)} for path in paths], "default_path": str(paths[-1])}


def test_initial_directory_then_saved_default_survives_restart(tmp_path):
    second = tmp_path / "another project"
    second.mkdir()
    path = tmp_path / "config" / "directories.json"
    store = LaunchDirectories(str(tmp_path), path)
    assert store.resolve() == str(tmp_path)
    store.save(configuration(tmp_path, second))
    assert LaunchDirectories(str(tmp_path), path).resolve() == str(second)
    assert store.resolve(str(tmp_path)) == str(tmp_path)
    assert store.view()["bridge_path"] == str(tmp_path)


def test_bad_update_leaves_file_untouched(tmp_path):
    store = LaunchDirectories(str(tmp_path), tmp_path / "directories.json")
    store.save(configuration(tmp_path))
    original = store.path.read_bytes()
    for body in ({}, {"directories": []}, configuration(tmp_path, tmp_path), configuration(tmp_path / "missing")):
        with pytest.raises(ValueError):
            store.save(body)
        assert store.path.read_bytes() == original


def test_removed_or_missing_directory_never_falls_back(tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    store = LaunchDirectories(str(tmp_path), tmp_path / "directories.json")
    with pytest.raises(ValueError, match="no longer saved"):
        store.resolve(str(second))
    store.save(configuration(tmp_path, second))
    second.rmdir()
    with pytest.raises(ValueError, match="missing"):
        store.resolve()
    assert store.resolve(str(tmp_path)) == str(tmp_path)


def test_corrupt_file_never_resets_the_default(tmp_path):
    path = tmp_path / "directories.json"
    path.write_text("{")
    with pytest.raises(ValueError, match="Could not read"):
        LaunchDirectories(str(tmp_path), path).resolve()


@pytest.mark.parametrize("path", ["relative", "", 123, "/tmp/line\nfeed", "/tmp/null\0"])
def test_bad_path_is_refused(tmp_path, path):
    body = {"directories": [{"label": "project", "path": path}], "default_path": path}
    with pytest.raises(ValueError):
        LaunchDirectories(str(tmp_path), tmp_path / "directories.json").save(body)


def test_save_failure_is_reported(tmp_path, monkeypatch):
    store = LaunchDirectories(str(tmp_path), tmp_path / "directories.json")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.launch_directories.os.replace", fail)
    with pytest.raises(OSError):
        store.save(configuration(tmp_path))
    assert not store.path.exists()
    assert not list(tmp_path.glob("launch-directories-*.tmp"))


def test_save_and_launch_selected_path_as_single_argv(spawner, monkeypatch):
    second = spawner.tmp / "project with spaces"
    second.mkdir()
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, configuration(spawner.tmp, second))._handle_save_launch_directories()
    assert spawner.recorded.status == 200
    for extra, expected in (({}, second), ({"cwd": str(spawner.tmp)}, spawner.tmp)):
        _with_body(spawner, {"name": "vc-2", "profile": "claude-a", **extra})._handle_spawn()
        assert spawner.recorded.status == 201, spawner.recorded.body
        assert calls[-1][5:7] == ["-c", str(expected)]


def test_bad_directory_does_not_run_tmux_or_erase_session_state(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a", "cwd": str(spawner.tmp / "missing")})._handle_spawn()
    assert spawner.recorded.status == 400
    assert calls == []


def test_settings_write_requires_allowed_origin(spawner):
    spawner.headers = {"Origin": "https://hostile.example"}
    _with_body(spawner, configuration(spawner.tmp))._handle_save_launch_directories()
    assert spawner.recorded.status == 403


def test_directory_routes_are_registered_behind_core_guards(spawner):
    assert spawner.GET_ROUTES["/launch-directories"] == "_handle_get_launch_directories"
    assert spawner.POST_ROUTES["/launch-directories"] == "_handle_save_launch_directories"
    spawner._handle_get_launch_directories()
    assert spawner.recorded.status == 200
    assert json.dumps(spawner.recorded.body)
