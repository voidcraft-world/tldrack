"""features.py: the operator's feature switches, their file, the loops that read them and the routes."""

import json
import subprocess
from pathlib import Path

import pytest

from tldrack import doctor, features, main
from tldrack.agents_cli import PublicEntry, PublicStatus
from tldrack.features import (
    BRIDGE_FEATURES,
    CODE_WATCH,
    LEDGER_INGEST,
    PUBLIC_STATUS,
    FeatureSwitches,
    mountable,
    plugin_key,
)
from tldrack.ledger.scheduler import LedgerScheduler
from test_agents_cli import ROOT, SID, Clock, _private
from test_freshness import monitor_for, tree, write  # noqa: F401
from test_session_routes import _with_body, spawner  # noqa: F401


def store(tmp_path: Path, *plugins: str) -> FeatureSwitches:
    return FeatureSwitches(tmp_path / "features.json", plugins)


# --- the store ----------------------------------------------------------------


def test_no_file_runs_everything(tmp_path):
    switches = store(tmp_path, "laya")
    assert all(row["on"] and not row["pending"] for row in switches.view()["features"])
    assert switches.view()["error"] is None and switches.off_keys() == []
    assert not switches.path.exists()  # reading never writes


def test_a_switch_is_saved_and_survives_a_restart(tmp_path):
    switches = store(tmp_path)
    view = switches.save({"set": {CODE_WATCH: False}})
    assert [row["key"] for row in view["features"] if not row["on"]] == [CODE_WATCH]
    assert json.loads(switches.path.read_text()) == {"version": 1, "off": [CODE_WATCH]}
    assert store(tmp_path).on(CODE_WATCH) is False and store(tmp_path).on(PUBLIC_STATUS) is True
    switches.save({"set": {CODE_WATCH: True}})
    assert store(tmp_path).off_keys() == []


def test_the_view_carries_what_the_page_draws(tmp_path):
    rows = {row["key"]: row for row in store(tmp_path, "laya", "my-plugin").view()["features"]}
    assert set(rows) == {feature.key for feature in BRIDGE_FEATURES} | {"plugin:laya", "plugin:my-plugin"}
    assert rows[CODE_WATCH]["group"] == "bridge" and rows[CODE_WATCH]["applies"] == "now" and rows[CODE_WATCH]["lite"] is True
    laya = rows["plugin:laya"]
    assert laya["group"] == "plugins" and laya["applies"] == "restart" and laya["lite"] is False and laya["title"] == "laya"
    for row in rows.values():
        assert all(isinstance(row[key], str) and row[key] for key in ("title", "does", "cost", "without"))
    # A plugin directory this build has no words for still gets a switch.
    assert rows["plugin:my-plugin"]["does"] == features.UNKNOWN_PLUGIN[0]


def test_a_plugin_switch_is_pending_until_the_next_start(tmp_path):
    switches = store(tmp_path, "laya")
    view = switches.save({"set": {"plugin:laya": False, CODE_WATCH: False}})
    rows = {row["key"]: row for row in view["features"]}
    assert rows["plugin:laya"]["pending"] is True and switches.started_on("plugin:laya") is True
    assert rows[CODE_WATCH]["pending"] is False  # a `now` switch is never pending
    restarted = store(tmp_path, "laya")
    assert {row["key"]: row["pending"] for row in restarted.view()["features"]}["plugin:laya"] is False
    assert restarted.started_on("plugin:laya") is False


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"set": {}},
        {"set": [CODE_WATCH]},
        {"set": {"no_such_feature": False}},
        {"set": {"plugin:not-offered": False}},
        {"set": {CODE_WATCH: "off"}},
        {"set": {CODE_WATCH: 0}},
        {"set": {CODE_WATCH: False, "no_such_feature": True}},
    ],
)
def test_a_bad_request_changes_nothing(tmp_path, body):
    switches = store(tmp_path)
    switches.save({"set": {PUBLIC_STATUS: False}})
    before = switches.path.read_bytes()
    with pytest.raises(ValueError):
        switches.save(body)
    assert switches.path.read_bytes() == before and switches.off_keys() == [PUBLIC_STATUS]


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[]",
        "{}",
        '{"version": 2, "off": []}',
        '{"version": 1}',
        '{"version": 1, "off": "code_watch"}',
        '{"version": 1, "off": [1]}',
    ],
)
def test_an_unreadable_file_switches_nothing_off_and_says_so(tmp_path, text):
    path = tmp_path / "features.json"
    path.write_text(text)
    switches = FeatureSwitches(path)
    assert switches.off_keys() == [] and switches.on(CODE_WATCH)
    assert "could not be read" in switches.view()["error"] and "features.json" in switches.view()["error"]
    # Saving replaces the file, so the error is gone.
    assert switches.save({"set": {CODE_WATCH: False}})["error"] is None
    assert FeatureSwitches(path).error is None


def test_a_stored_key_this_build_does_not_know_is_kept_and_not_shown(tmp_path):
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"version": 1, "off": ["from_a_newer_build", "plugin:gone"]}))
    switches = FeatureSwitches(path)
    assert switches.error is None and switches.off_keys() == []
    switches.save({"set": {CODE_WATCH: False}})
    assert json.loads(path.read_text())["off"] == [CODE_WATCH, "from_a_newer_build", "plugin:gone"]


def test_a_failed_write_is_raised_and_leaves_memory_and_disk_alone(tmp_path, monkeypatch):
    switches = store(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.features.os.replace", fail)
    with pytest.raises(OSError):
        switches.save({"set": {CODE_WATCH: False}})
    assert switches.on(CODE_WATCH) and not switches.path.exists()
    assert not list(tmp_path.glob("features-*.tmp"))


# --- plugins: the switches only subtract ----------------------------------------


def test_a_switched_off_plugin_is_not_mounted(tmp_path):
    dirs = [tmp_path / "worktree", tmp_path / "laya", tmp_path / "phone-link"]
    switches = store(tmp_path)
    assert mountable(dirs, switches) == (dirs, [])
    switches.save({"set": {plugin_key("phone-link"): False}})
    assert mountable(dirs, store(tmp_path)) == ([dirs[0], dirs[1]], ["phone-link"])


def test_a_directory_no_plugin_could_be_named_after_is_never_skipped(tmp_path):
    odd = tmp_path / "Not A Plugin Name"
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"version": 1, "off": [plugin_key(odd.name)]}))
    switches = FeatureSwitches(path)
    assert mountable([odd], switches) == ([odd], [])
    assert all(row["group"] == "bridge" for row in switches.view()["features"])


def test_the_loader_skips_what_is_off_and_says_so(tmp_path, monkeypatch, capsys):
    plugins = tmp_path / "plugins"
    for name in ("alpha", "beta"):
        write(
            plugins / name / "bridge_plugin.py",
            f"PLUGIN_NAME = {name!r}\n"
            "class P:\n"
            "    name = PLUGIN_NAME\n"
            "    def routes(self): return []\n"
            "    def handle_get(self, subpath, query): return 404, {}\n"
            "    def handle_post(self, subpath, query, body): return 404, {}\n"
            "def create_plugin(context):\n"
            "    return P()\n",
        )
    monkeypatch.setenv("TLDRACK_PLUGINS", f"{plugins / 'alpha'}:{plugins / 'beta'}")
    switches = store(tmp_path)
    registry, mounted = main.load_rack_plugins(7781, 7791, "tldrack", str(tmp_path), features=switches)
    assert {"alpha", "beta"} <= set(registry) and [path.name for path in mounted] == ["alpha", "beta"]
    switches.save({"set": {plugin_key("beta"): False}})
    registry, mounted = main.load_rack_plugins(7781, 7791, "tldrack", str(tmp_path), features=store(tmp_path))
    assert "alpha" in registry and "beta" not in registry and [path.name for path in mounted] == ["alpha"]
    assert "plugins off:       beta" in capsys.readouterr().out


# --- the loops ------------------------------------------------------------------


def test_the_code_watch_stops_hashing_and_reads_unknown_while_off(tree):  # noqa: F811
    on = {"value": False}
    monitor = monitor_for(tree, enabled=lambda: on["value"])
    write(tree["pkg"] / "main.py", "VALUE = 22\n")
    monitor.tick()
    assert monitor.snapshot() is None
    on["value"] = True
    assert monitor.snapshot()["stale"] is False  # nothing was hashed while it was off
    monitor.tick()
    assert monitor.snapshot()["stale"] is True


def test_the_cross_check_forgets_instead_of_freezing(tmp_path):
    on = {"value": True}
    clock = Clock()
    status = PublicStatus(
        lambda: [ROOT],
        lambda: "/bin/claude",
        "/Users/x/.claude",
        read=lambda _cli, _root, _default: [PublicEntry(500, "busy", None, SID)],
        pane_pids=lambda: {300: "%7"},
        process_table=lambda: "  300     1 zsh\n  500   300 claude\n",
        started=lambda pid: "Mon Jan 1 12:00:00 2024",
        clock=clock,
        enabled=lambda: on["value"],
    )
    status.observe({ROOT: []})  # the index no longer parses
    status.tick()
    clock.now += 5
    status.tick()
    assert status.drifted() == {ROOT} and status.health()
    on["value"] = False
    status.tick()
    assert status.drifted() == set() and status.health() == {} and status.entries_for(ROOT) == []
    status.observe({ROOT: [_private(500, "busy")]})
    on["value"] = True
    status.tick()
    assert status.health()[ROOT]["mode"] == "index"


def test_the_ledger_timer_stops_but_an_asked_ingest_still_runs(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    on = {"value": False}
    sched = LedgerScheduler(tmp_path, run=run, enabled=lambda: on["value"])
    assert sched.tick(asked=False) is None and calls == []
    assert sched.tick(asked=True)["exit"] == 0 and len(calls) == 1
    on["value"] = True
    assert sched.tick(asked=False)["exit"] == 0 and len(calls) == 2


# --- the routes -----------------------------------------------------------------


def test_feature_routes_are_registered_and_round_trip(spawner):  # noqa: F811
    assert spawner.GET_ROUTES["/features"] == "_handle_get_features"
    assert spawner.POST_ROUTES["/features"] == "_handle_save_features"
    spawner._handle_get_features()
    assert spawner.recorded.status == 200 and all(row["on"] for row in spawner.recorded.body["features"])
    _with_body(spawner, {"set": {LEDGER_INGEST: False}})._handle_save_features()
    assert spawner.recorded.status == 200
    assert [row["key"] for row in spawner.recorded.body["features"] if not row["on"]] == [LEDGER_INGEST]
    assert json.dumps(spawner.recorded.body)
    _with_body(spawner, {"set": {"nope": False}})._handle_save_features()
    assert spawner.recorded.status == 400 and "Unknown feature" in spawner.recorded.body["error"]


def test_a_feature_write_requires_an_allowed_origin(spawner):  # noqa: F811
    spawner.headers = {"Origin": "https://hostile.example"}
    _with_body(spawner, {"set": {CODE_WATCH: False}})._handle_save_features()
    assert spawner.recorded.status == 403
    assert FeatureSwitches().on(CODE_WATCH)


def test_a_failed_disk_write_is_a_500_not_a_saved(spawner, monkeypatch):  # noqa: F811
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("tldrack.features.os.replace", fail)
    _with_body(spawner, {"set": {CODE_WATCH: False}})._handle_save_features()
    assert spawner.recorded.status == 500


def test_the_doctor_route_answers_the_commands_checks_as_json(spawner, monkeypatch):  # noqa: F811
    assert spawner.GET_ROUTES["/doctor"] == "_route_doctor"
    monkeypatch.setattr(main, "port_holder", lambda port: (1234, "/x/bin/tldrack"))
    monkeypatch.setattr(doctor, "find_tools", lambda: {"tmux": "/bin/tmux", "ttyd": "/bin/ttyd", "claude": "/bin/claude"})
    monkeypatch.setattr(spawner, "snapshot_payload", lambda: {"plugins": {"laya": {}}}, raising=False)
    _with_body(spawner, {"set": {CODE_WATCH: False}})._handle_save_features()
    spawner._handle_doctor()
    assert spawner.recorded.status == 200, spawner.recorded.body
    body = spawner.recorded.body
    assert json.dumps(body) and set(body) == {"sections", "exit", "verdict"}
    checks = {check["name"]: check for section in body["sections"] for check in section["checks"]}
    assert checks["bridge"]["level"] == "ok" and checks["port 7682"]["detail"].startswith("this rack")
    assert checks["switched off"]["level"] == "off" and CODE_WATCH in checks["switched off"]["detail"]
    assert all(set(check) == {"name", "level", "detail", "fix", "action"} for check in checks.values())


def test_a_handler_without_ports_has_no_doctor(spawner, monkeypatch):  # noqa: F811
    from test_session_routes import Recorder
    from tldrack.icons import SessionIconStore
    from tldrack.main import ShutdownControl, StatusStore, build_status_handler
    from tldrack.models import SessionModelStore

    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(spawner.tmp / "icons2.json"),
        model_store=SessionModelStore(spawner.tmp / "models2.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
    )
    bare = object.__new__(cls)
    rec = Recorder()
    bare._send_json = lambda status, body: (setattr(rec, "status", status), setattr(rec, "body", body))
    bare._handle_doctor()
    assert rec.status == 503
