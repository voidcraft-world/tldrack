"""The freshness watcher (tldrack/freshness.py): is the running code
still the code on disk, is the new code safe to restart into, and what would a
restart kill. Every test runs against a temp tree through an injected
`WatchSpec`, never against this checkout — except the one smoke test that
preflights the real package."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

import tldrack.main as main_module
from tldrack.freshness import (
    CHANGED_CAP,
    BusyProbe,
    FreshnessMonitor,
    WatchSpec,
    bridge_watch_spec,
    describe_freshness,
    display_path,
    mounted_plugin_dirs,
    plugin_module_name,
    run_preflight,
)
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore

CONTRACT_KEYS = {"stale", "changed", "changed_count", "changed_at", "checked_at", "preflight", "busy"}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path):
    """repo/{bridge/fb_pkg, plugins/demo}, a home with the config file, and a
    spec over all of it. `fb_pkg` is a unique name so the preflight child can
    import it without colliding with anything installed."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    pkg = repo / "bridge" / "fb_pkg"
    write(pkg / "__init__.py", "")
    write(pkg / "main.py", "VALUE = 1\n")
    write(pkg / "sub" / "helpers.py", "def helper():\n    return 1\n")
    plugin = repo / "plugins" / "demo"
    write(
        plugin / "bridge_plugin.py",
        "from demo_pkg.core import NAME\n"
        "PLUGIN_NAME = NAME\n"
        "def create_plugin(context):\n"
        "    raise AssertionError('preflight must never create the plugin')\n",
    )
    write(plugin / "demo_pkg" / "__init__.py", "")
    write(plugin / "demo_pkg" / "core.py", "NAME = 'demo'\n")
    write(plugin / "tests" / "test_demo.py", "x = 1\n")
    write(plugin / ".venv" / "lib" / "site.py", "x = 1\n")
    write(plugin / "demo_pkg" / "__pycache__" / "core.py", "x = 1\n")
    write(repo / "bridge" / "pyproject.toml", "[project]\n")
    profiles = home / ".config" / "tb" / "profiles.json"
    spec = WatchSpec(
        code_dirs=(pkg,),
        plugin_dirs=(plugin,),
        files=(repo / "bridge" / "pyproject.toml", profiles),
        display_root=repo,
        home=home,
        import_modules=("fb_pkg.main",),
        sys_path=(str(repo / "bridge"),),
    )
    return {"repo": repo, "home": home, "pkg": pkg, "plugin": plugin, "profiles": profiles, "spec": spec}


def monitor_for(tree, clock=None, **kwargs) -> FreshnessMonitor:
    kwargs.setdefault("quiet_seconds", 1e9)  # preflight off unless a test wants it
    return FreshnessMonitor(tree["spec"], clock=clock or Clock(), **kwargs)


# --- staleness ---------------------------------------------------------------


def test_a_fresh_monitor_is_not_stale_and_matches_the_contract(tree):
    block = monitor_for(tree).snapshot()
    assert set(block) == CONTRACT_KEYS
    assert block["stale"] is False
    assert block["changed"] == [] and block["changed_count"] == 0
    assert block["changed_at"] is None
    assert block["preflight"] == {"status": None, "error": None, "checked_at": None}
    assert block["busy"] == []


def test_an_edit_is_stale_with_a_repo_relative_path(tree):
    monitor = monitor_for(tree)
    write(tree["pkg"] / "main.py", "VALUE = 22\n")
    monitor.check()
    block = monitor.snapshot()
    assert block["stale"] is True
    assert block["changed"] == ["bridge/fb_pkg/main.py"]


def test_an_added_and_a_removed_file_are_both_changes(tree):
    monitor = monitor_for(tree)
    write(tree["plugin"] / "demo_pkg" / "extra.py", "y = 2\n")
    (tree["pkg"] / "sub" / "helpers.py").unlink()
    monitor.check()
    assert monitor.snapshot()["changed"] == ["bridge/fb_pkg/sub/helpers.py", "plugins/demo/demo_pkg/extra.py"]


def test_an_edit_reverted_to_identical_bytes_is_not_stale(tree):
    clock = Clock()
    monitor = monitor_for(tree, clock)
    target = tree["pkg"] / "main.py"
    original = target.read_text()
    write(target, "VALUE = 'something else'\n")
    monitor.check()
    assert monitor.snapshot()["stale"] is True
    write(target, original)
    clock.now += 5
    monitor.check()
    block = monitor.snapshot()
    assert block["stale"] is False
    assert block["changed_at"] is None
    assert block["preflight"]["status"] is None


def test_an_mtime_only_touch_is_not_stale(tree):
    monitor = monitor_for(tree)
    future = time.time() + 3600
    os.utime(tree["pkg"] / "main.py", (future, future))
    monitor.check()
    assert monitor.snapshot()["stale"] is False


def test_pruned_dirs_are_never_watched(tree):
    monitor = monitor_for(tree)
    write(tree["plugin"] / "tests" / "test_demo.py", "x = 'changed'\n")
    write(tree["plugin"] / ".venv" / "lib" / "site.py", "x = 'changed'\n")
    write(tree["plugin"] / "demo_pkg" / "__pycache__" / "core.py", "x = 'changed'\n")
    write(tree["plugin"] / "README.md", "not python\n")
    monitor.check()
    assert monitor.snapshot()["stale"] is False


def test_an_explicit_file_appearing_is_a_change_and_renders_home_relative(tree):
    monitor = monitor_for(tree)
    write(tree["profiles"], '{"profiles": []}\n')
    monitor.check()
    assert monitor.snapshot()["changed"] == ["~/.config/tb/profiles.json"]


def test_changed_is_capped_but_changed_count_is_the_truth(tree):
    monitor = monitor_for(tree)
    for i in range(CHANGED_CAP + 5):
        write(tree["pkg"] / f"new_{i:02d}.py", f"N = {i}\n")
    monitor.check()
    block = monitor.snapshot()
    assert len(block["changed"]) == CHANGED_CAP
    assert block["changed_count"] == CHANGED_CAP + 5


def test_changed_at_moves_on_every_new_edit_and_holds_when_quiet(tree):
    clock = Clock(100.0)
    monitor = monitor_for(tree, clock)
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    assert monitor.snapshot()["changed_at"] == 100.0
    clock.now = 110.0
    write(tree["pkg"] / "main.py", "VALUE = 333\n")
    monitor.check()
    assert monitor.snapshot()["changed_at"] == 110.0
    clock.now = 120.0
    monitor.check()
    block = monitor.snapshot()
    assert block["changed_at"] == 110.0
    assert block["checked_at"] == 120.0


def test_a_file_edited_between_import_and_baseline_starts_stale(tree):
    os.utime(tree["pkg"] / "main.py", (time.time() + 60, time.time() + 60))
    monitor = monitor_for(tree, loaded_at=time.time())
    block = monitor.snapshot()
    assert block["stale"] is True
    assert block["changed"] == ["bridge/fb_pkg/main.py"]
    assert block["changed_at"] is not None


def test_display_path_falls_back_to_absolute():
    assert display_path("/opt/x/y.py", Path("/repo"), Path("/home/me")) == "/opt/x/y.py"
    assert display_path("/repo/a/b.py", Path("/repo"), Path("/home/me")) == "a/b.py"
    assert display_path("/home/me/.config/p.json", Path("/repo"), Path("/home/me")) == "~/.config/p.json"


# --- preflight ---------------------------------------------------------------


def test_preflight_waits_for_the_quiet_period(tree):
    clock = Clock()
    calls = []
    monitor = monitor_for(tree, clock, quiet_seconds=20, preflight=lambda spec, files: calls.append(files) or ("ok", None))
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    assert monitor.snapshot()["preflight"]["status"] == "pending"
    clock.now += 19
    monitor.check()
    assert calls == []
    clock.now += 1
    monitor.check()
    monitor.join_preflight(5)
    assert len(calls) == 1
    assert monitor.snapshot()["preflight"] == {"status": "ok", "error": None, "checked_at": clock.now}


def test_preflight_ok_on_a_clean_tree_and_never_creates_the_plugin(tree):
    monitor = monitor_for(tree, quiet_seconds=0)
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    monitor.join_preflight(60)
    preflight = monitor.snapshot()["preflight"]
    assert preflight["status"] == "ok", preflight["error"]


def test_preflight_fails_on_a_syntax_error_and_names_the_file(tree):
    monitor = monitor_for(tree, quiet_seconds=0)
    write(tree["pkg"] / "sub" / "helpers.py", "def helper(:\n")
    monitor.check()
    monitor.join_preflight(60)
    preflight = monitor.snapshot()["preflight"]
    assert preflight["status"] == "failed"
    assert "SyntaxError" in preflight["error"]
    assert "bridge/fb_pkg/sub/helpers.py" in preflight["error"]
    assert str(tree["repo"]) not in preflight["error"]


def test_preflight_fails_when_a_plugin_entry_no_longer_imports(tree):
    monitor = monitor_for(tree, quiet_seconds=0)
    write(tree["plugin"] / "demo_pkg" / "core.py", "from demo_pkg.gone import NAME\n")
    monitor.check()
    monitor.join_preflight(60)
    preflight = monitor.snapshot()["preflight"]
    assert preflight["status"] == "failed"
    assert preflight["error"].startswith("plugin demo: ModuleNotFoundError")


def test_preflight_fails_when_the_main_module_raises_at_import(tree):
    write(tree["pkg"] / "main.py", "VALUE = 1\n")
    monitor = monitor_for(tree, quiet_seconds=0)
    write(tree["pkg"] / "main.py", "raise RuntimeError('boom at import')\n")
    monitor.check()
    monitor.join_preflight(60)
    preflight = monitor.snapshot()["preflight"]
    assert preflight["status"] == "failed"
    assert "import fb_pkg.main: RuntimeError: boom at import" in preflight["error"]


def test_a_tree_that_moves_during_preflight_is_checked_again(tree):
    target = tree["pkg"] / "main.py"
    calls = []

    def preflight(spec, files):
        calls.append(target.read_text())
        if len(calls) == 1:
            write(target, "VALUE = 'edited mid-preflight'\n")
        return "ok", None

    clock = Clock()
    monitor = monitor_for(tree, clock, quiet_seconds=0, preflight=preflight)
    write(target, "VALUE = 2\n")
    monitor.check()
    monitor.join_preflight(5)
    assert monitor.snapshot()["preflight"]["status"] == "pending"
    clock.now += 5
    monitor.check()
    monitor.join_preflight(5)
    assert calls == ["VALUE = 2\n", "VALUE = 'edited mid-preflight'\n"]
    assert monitor.snapshot()["preflight"]["status"] == "ok"


def test_preflight_never_runs_twice_at_once(tree):
    release = threading.Event()
    in_flight, peak = [0], [0]

    def preflight(spec, files):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        release.wait(5)
        in_flight[0] -= 1
        return "ok", None

    clock = Clock()
    monitor = monitor_for(tree, clock, quiet_seconds=0, preflight=preflight)
    for value in range(3):
        write(tree["pkg"] / "main.py", f"VALUE = {value * 1000}\n")
        clock.now += 5
        monitor.check()
    release.set()
    monitor.join_preflight(5)
    assert peak[0] == 1


def test_a_crashing_preflight_function_reads_as_failed(tree):
    def preflight(spec, files):
        raise OSError("fork failed")

    monitor = monitor_for(tree, quiet_seconds=0, preflight=preflight)
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    monitor.join_preflight(5)
    preflight_block = monitor.snapshot()["preflight"]
    assert preflight_block["status"] == "failed"
    assert "OSError: fork failed" in preflight_block["error"]


def test_the_real_bridge_package_preflights_ok():
    spec = bridge_watch_spec([], None, Path("/nonexistent/profiles.json"))
    files = [p for p in map(str, Path(spec.code_dirs[0]).rglob("*.py")) if "__pycache__" not in p]
    assert run_preflight(spec, files) == ("ok", None)


# --- the watch set -----------------------------------------------------------


def test_bridge_watch_spec_covers_the_package_lock_and_configs(tmp_path):
    plugins_json = tmp_path / "plugins.json"
    profiles = tmp_path / "profiles.json"
    spec = bridge_watch_spec([tmp_path / "plug"], plugins_json, profiles, home=tmp_path)
    names = {f.name for f in spec.files}
    assert {"pyproject.toml", "uv.lock", "profiles.json", "plugins.json"} <= names
    assert spec.code_dirs[0].name == "tldrack"
    assert spec.plugin_module_names[str((tmp_path / "plug").resolve())] == plugin_module_name(tmp_path / "plug")
    shadowed = bridge_watch_spec([], None, profiles, home=tmp_path)
    assert "plugins.json" not in {f.name for f in shadowed.files}


def test_mounted_plugin_dirs_mirrors_the_core_first_wins(monkeypatch, tmp_path):
    def entry(directory: Path, name: str) -> None:
        module = type(sys)(plugin_module_name(directory))
        module.PLUGIN_NAME = name
        monkeypatch.setitem(sys.modules, plugin_module_name(directory), module)

    good, dup, builtin_clash, broken = (tmp_path / n for n in ("good", "dup-of-good", "llm", "broken"))
    entry(good, "good")
    entry(dup, "good")
    entry(builtin_clash, "local-llm")
    registry = {"local-llm": object(), "good": object()}
    assert mounted_plugin_dirs([good, dup, builtin_clash, broken], registry, {"local-llm"}) == [good]


# --- busy --------------------------------------------------------------------


class Plugin:
    def __init__(self, answer=None, error=None, gate=None):
        self.answer, self.error, self.gate, self.calls = answer, error, gate, 0

    def busy_reasons(self):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.answer


def test_busy_aggregates_and_a_raising_plugin_contributes_nothing():
    lines = []
    probe = BusyProbe(
        {
            "worktree": Plugin(["creating worktree feat-x", "  ", 7]),
            "broken": Plugin(error=RuntimeError("bug")),
            "garbage": Plugin("not a list"),
            "silent": object(),
            "sync": Plugin(["sync running"]),
        },
        log=lines.append,
    )
    assert probe.reasons() == ["creating worktree feat-x", "sync running"]
    assert probe.reasons() == ["creating worktree feat-x", "sync running"]
    assert len(lines) == 1 and "broken" in lines[0]


def test_a_hung_busy_probe_reads_as_busy_and_is_not_asked_again_while_stuck():
    gate = threading.Event()
    slow = Plugin(["late"], gate=gate)
    probe = BusyProbe({"slow": slow, "fast": Plugin(["fast job"])}, budget=0.05)
    assert probe.reasons() == ["slow: busy check not answering", "fast: busy check not answering"]
    assert probe.reasons() == ["slow: busy check not answering", "fast job"]
    assert slow.calls == 1
    gate.set()
    time.sleep(0.05)
    assert probe.reasons() == ["late", "fast job"]


def test_the_monitor_asks_busy_live_on_every_snapshot(tree):
    plugin = Plugin([])
    monitor = monitor_for(tree, plugins={"worktree": plugin})
    assert monitor.snapshot()["busy"] == []
    plugin.answer = ["opening PR feat/x"]
    assert monitor.snapshot()["busy"] == ["opening PR feat/x"]


# --- the wire ----------------------------------------------------------------


def _snapshot_bridge(tmp_path, monkeypatch, freshness):
    monkeypatch.setattr(main_module, "list_tmux_sessions", dict)
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "started_at": 0.0},
        control=ShutdownControl("status-only"),
        freshness=freshness,
    )
    return object.__new__(cls).snapshot_payload()["bridge"]


def test_the_snapshot_bridge_block_carries_freshness(tree, tmp_path, monkeypatch):
    monitor = monitor_for(tree)
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    block = _snapshot_bridge(tmp_path, monkeypatch, monitor)["freshness"]
    assert set(block) == CONTRACT_KEYS
    assert block["stale"] is True and block["changed"] == ["bridge/fb_pkg/main.py"]


def test_a_bridge_without_a_watcher_says_null_not_stale(tmp_path, monkeypatch):
    bridge = _snapshot_bridge(tmp_path, monkeypatch, None)
    assert "freshness" in bridge and bridge["freshness"] is None


def test_describe_freshness_for_the_status_cli():
    assert describe_freshness(None).startswith("unknown")
    assert describe_freshness({"stale": False}) == "current"
    line = describe_freshness({"stale": True, "changed_count": 3, "preflight": {"status": "ok"}, "busy": ["opening PR feat/x"]})
    assert line == "RESTART NEEDED — 3 files changed, preflight ok, busy: opening PR feat/x"
