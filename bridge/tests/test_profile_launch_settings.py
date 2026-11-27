import json
import shlex
from concurrent.futures import ThreadPoolExecutor

import pytest

from tldrack.profile_launch_settings import ProfileLaunchSettings, override_args
from tldrack.profiles import Profile, ProfileRegistry
from test_session_routes import _record_tmux, _with_body, handler, spawner  # noqa: F401


def profiles(tmp_path):
    return [
        Profile("codex", "CODEX", "codex", str(tmp_path), False, ("-m", "old-model", "-c", 'model_reasoning_effort="high"')),
        Profile("claude", "CLAUDE", "claude", str(tmp_path), False),
        Profile("shell", "SHELL", "shell", None, False),
    ]


def request(profile="codex", model="example-model-v2", effort="medium"):
    return {"profile": profile, "model": model, "reasoning_effort": effort}


def test_persist_live_defaults_and_reset_without_changing_accounts(tmp_path):
    base = profiles(tmp_path)
    store = ProfileLaunchSettings(base, tmp_path / "defaults.json")
    before = store.apply(base[0])
    store.save(request())
    effective = store.apply(base[0])
    assert effective.args == ("--model", "example-model-v2", "-c", 'model_reasoning_effort="medium"')
    assert effective.config_dir == before.config_dir
    assert before.args == base[0].args
    restarted = ProfileLaunchSettings(base, store.path)
    assert restarted.apply(base[0]) == effective
    restarted.save(request(model=None, effort=None))
    assert restarted.apply(base[0]) == before
    assert json.loads(store.path.read_text())["profiles"] == {}


@pytest.mark.parametrize(
    "args",
    [
        ("-m", "old", "-c", 'model="old"', "--config", 'model_reasoning_effort="high"'),
        ("--model=old", '--config=model="old"', '-cmodel_reasoning_effort="high"'),
        ("-mold", "--model", "older", "-c", '"model_reasoning_effort" = "high"'),
    ],
)
def test_override_removes_duplicates_preserves_other_config_and_prompt(args):
    args += ("-c", 'sandbox_mode="workspace-write"', "--", "--model=prompt-text")
    assert override_args(args, "codex", "new", "medium") == (
        "-c",
        'sandbox_mode="workspace-write"',
        "--model",
        "new",
        "-c",
        'model_reasoning_effort="medium"',
        "--",
        "--model=prompt-text",
    )


def test_each_blank_field_inherits_existing_arguments(tmp_path):
    base = profiles(tmp_path)
    store = ProfileLaunchSettings(base, tmp_path / "defaults.json")
    store.save(request(model=None))
    assert store.apply(base[0]).args == ("-m", "old-model", "-c", 'model_reasoning_effort="medium"')
    store.save(request(effort=None))
    assert store.apply(base[0]).args == ("-c", 'model_reasoning_effort="high"', "--model", "example-model-v2")


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        request(profile="missing"),
        request(profile="shell"),
        request(profile=[]),
        request(model="$(touch nope)"),
        request(model="--help"),
        request(model="x" * 129),
        request(model=7),
        request(effort="turbo"),
        request(effort=[]),
        request(profile="claude"),
        {**request(), "args": ["--help"]},
    ],
)
def test_invalid_input_never_writes(tmp_path, body):
    store = ProfileLaunchSettings(profiles(tmp_path), tmp_path / "defaults.json")
    with pytest.raises(ValueError):
        store.save(body)
    assert not store.path.exists()


def test_failed_save_keeps_last_committed_defaults(tmp_path, monkeypatch):
    base = profiles(tmp_path)
    store = ProfileLaunchSettings(base, tmp_path / "defaults.json")
    store.save(request())
    previous = store.path.read_bytes(), store.apply(base[0])

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.profile_launch_settings.os.replace", fail)
    with pytest.raises(OSError):
        store.save(request(model="another-model"))
    assert (store.path.read_bytes(), store.apply(base[0])) == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ["defaults.json"]


def test_parallel_profile_saves_keep_both_changes(tmp_path):
    store = ProfileLaunchSettings(profiles(tmp_path), tmp_path / "defaults.json")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.save, [request(), request("claude", "sonnet", None)]))
    assert len(results) == 2
    saved = json.loads(store.path.read_text())["profiles"]
    assert saved["codex"]["reasoning_effort"] == "medium"
    assert saved["claude"]["model"] == "sonnet"


def test_registry_uses_live_defaults_and_explicit_launch_wins(tmp_path):
    registry = ProfileRegistry(profiles(tmp_path), "codex", home=tmp_path, source="file", which=lambda _: None)
    before = registry.spawn_argv(None, "before", tmp_path)
    registry.launch_settings.save(request())
    after = registry.spawn_argv(None, "after", tmp_path)
    assert "old-model" in shlex.join(before)
    assert "example-model-v2" in shlex.join(after)
    assert "old-model" not in shlex.join(after)
    assert registry.describe("codex")["args"][1] == "old-model"
    registry.launch_settings.save(request("claude", "sonnet", None))
    explicit = shlex.join(registry.spawn_argv("claude", "explicit", tmp_path, model="opus", cli_bin="claude"))
    assert "--model opus" in explicit and "--model sonnet" not in explicit
    assert "not weaker" in registry.model_refusal("claude", subagent_model="opus")


def test_bad_file_blocks_agent_launch_and_saves_but_not_shell(tmp_path):
    registry = ProfileRegistry(profiles(tmp_path), "codex", home=tmp_path, source="file")
    path = registry.launch_settings.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{")
    registry._launch_settings = None
    assert registry.launch_settings.view()["error"]
    with pytest.raises(ValueError, match="Could not read"):
        registry.spawn_argv("codex", "blocked", tmp_path)
    with pytest.raises(ValueError, match="Could not read"):
        registry.launch_settings.save(request())
    assert registry.launch_refusal("shell") is None
    assert path.read_text() == "{"


def test_unknown_profiles_are_preserved(tmp_path):
    path = tmp_path / "defaults.json"
    path.write_text(json.dumps({"version": 1, "profiles": {"other": {"model": "custom", "reasoning_effort": None}}}))
    store = ProfileLaunchSettings(profiles(tmp_path), path)
    store.save(request())
    assert json.loads(path.read_text())["profiles"]["other"]["model"] == "custom"


def test_routes_save_then_launch_without_restarting(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    assert spawner.GET_ROUTES["/profile-launch-settings"] == "_handle_get_profile_launch_settings"
    assert spawner.POST_ROUTES["/profile-launch-settings"] == "_handle_save_profile_launch_settings"
    _with_body(spawner, request("claude-a", "sonnet", None))._handle_save_profile_launch_settings()
    assert spawner.recorded.status == 200
    _with_body(spawner, {"name": "new-agent", "profile": "claude-a"})._handle_spawn()
    assert spawner.recorded.status == 201
    assert "--model sonnet" in shlex.join(calls[0])
    spawner._handle_get_profile_launch_settings()
    assert spawner.recorded.body["profiles"][0]["model"] == "sonnet"


def test_route_guards_and_invalid_input(spawner):
    _with_body(spawner, request(effort="invalid"))._handle_save_profile_launch_settings()
    assert spawner.recorded.status == 400
    spawner.headers = {"Origin": "https://hostile.example"}
    _with_body(spawner, request())._handle_save_profile_launch_settings()
    assert spawner.recorded.status == 403
    spawner._handle_get_profile_launch_settings()
    assert all(row["model"] is None for row in spawner.recorded.body["profiles"])


def test_missing_registry_is_unavailable(handler):
    handler._handle_get_profile_launch_settings()
    assert handler.recorded.status == 503
    _with_body(handler, request())._handle_save_profile_launch_settings()
    assert handler.recorded.status == 503


def test_route_reports_write_failure(spawner, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.profile_launch_settings.os.replace", fail)
    _with_body(spawner, request())._handle_save_profile_launch_settings()
    assert spawner.recorded.status == 500
    spawner._handle_get_profile_launch_settings()
    assert all(row["model"] is None for row in spawner.recorded.body["profiles"])
