"""Off until the operator turns it on; then the routes and the device list: this machine first, hosts.json
order after, a removed host stopped; and the opt-in lease and jobs readouts."""

import platform
import threading
import time

import pytest
from voidcraft_world_bridge.local_llm.hosts import RemoteHost

import bridge_plugin
from devices_plugin import mac
from devices_plugin import plugin as plugin_module
from devices_plugin.plugin import DevicesPlugin
from devices_plugin.probe import Reading
from devices_plugin.settings import DevicesSettings

ON = DevicesSettings(enabled=True)
OFF_LINE = "  devices: off (set plugins.devices.enabled in settings.json)"


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Host:
    """The bridge's context: `log`, and the settings seam when `settings` is given."""

    def __init__(self, settings=None):
        self.lines = []
        if settings is not None:
            self.plugin_settings = lambda name: settings if name == "devices" else {}

    def log(self, line):
        self.lines.append(line)


class FakeSampler:
    def __init__(self, name, alias):
        self.name, self.alias = name, alias
        self.touched = 0
        self.closed = False

    def touch(self):
        self.touched += 1

    def close(self):
        self.closed = True

    def snapshot(self):
        return {"name": self.name, "local": False}


@pytest.fixture
def world():
    hosts = [RemoteHost(name="compute-0", ssh="box2")]
    made: list[FakeSampler] = []

    def make(host):
        made.append(FakeSampler(host.name, host.ssh))
        return made[-1]

    plugin = DevicesPlugin(
        None, settings=ON, load=lambda: list(hosts), read=lambda: Reading(cores=1), read_sessions=dict, make_remote=make
    )
    yield plugin, hosts, made
    plugin.close()


def test_state_lists_this_machine_then_each_host(world):
    plugin, _, made = world
    status, body = plugin.handle_request("GET", "/state", {}, None, {})
    assert status == 200
    assert body["version"] == 1 and body["sample_s"] == 2.0
    assert [d["name"] for d in body["devices"]] == ["local", "compute-0"]
    assert body["devices"][0]["local"] is True
    assert made[0].touched == 1


def test_samplers_are_reused_across_reads(world):
    plugin, _, made = world
    plugin.state()
    plugin.state()
    assert len(made) == 1 and made[0].touched == 2


def test_a_removed_or_repointed_host_is_stopped(world):
    plugin, hosts, made = world
    plugin.state()
    hosts[0] = RemoteHost(name="compute-0", ssh="box3")
    plugin.state()
    assert made[0].closed and not made[1].closed and made[1].alias == "box3"
    hosts.clear()
    body = plugin.state()
    assert made[1].closed
    assert [d["name"] for d in body["devices"]] == ["local"]


def test_routes_and_refusals(world):
    plugin, _, _ = world
    assert plugin.routes() == ["/plugin/devices/state"]
    assert plugin.handle_request("POST", "/state", {}, {}, {})[0] == 405
    assert plugin.handle_post("/state", {}, {})[0] == 405
    assert plugin.handle_get("/nope", {})[0] == 404
    assert plugin.handle_get("", {})[0] == 200


def test_close_stops_every_sampler(world):
    plugin, _, made = world
    plugin.state()
    plugin.close()
    assert made[0].closed


@pytest.mark.skipif(platform.system() not in ("Darwin", "Linux"), reason="no local source")
def test_this_machine_reads_sane_numbers():
    first = mac.read_local()
    second = mac.read_local()
    assert first.cores and first.cores >= 1
    assert 0 < second.mem_used < second.mem_total
    assert second.ticks.total >= first.ticks.total and second.ticks.busy <= second.ticks.total


# ── off until the operator turns it on ─────────────────────────────────────────────────────────


@pytest.fixture()
def nothing_may_run(monkeypatch):
    """Every way the plugin could touch the machine, wired to fail the test."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an off plugin touched the machine")

    for name in ("LocalSampler", "RemoteSampler", "load_hosts", "load_lease", "read_local"):
        monkeypatch.setattr(plugin_module, name, forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)


@pytest.mark.parametrize(
    "settings",
    [None, {}, {"enabled": False}, {"enabled": False, "lease": "/nope.py"}],
    ids=["no seam", "no settings", "false", "false with other keys"],
)
def test_off_by_default_builds_nothing_and_answers_like_an_absent_plugin(settings, nothing_may_run):
    host = Host(settings)
    plugin = bridge_plugin.create_plugin(host)
    assert plugin.enabled is False
    # One line, and nothing about the other keys: an off plugin does not read them.
    assert host.lines == [OFF_LINE]
    assert plugin.routes() == []
    for method in ("GET", "POST"):
        status, body = plugin.handle_request(method, "/state", {}, None, {})
        assert (status, body["error"], body["enabled"]) == (404, "off", False)
    assert plugin.handle_get("/state", {})[0] == 404
    assert plugin.handle_post("/state", {}, {})[0] == 404
    assert plugin.state() == {"version": 1, "sample_s": 2.0, "devices": []}
    plugin.close()


def test_a_non_boolean_enabled_is_reported_and_stays_off(nothing_may_run):
    host = Host({"enabled": "yes"})
    assert bridge_plugin.create_plugin(host).enabled is False
    assert host.lines == ["  devices: plugins.devices.enabled must be true or false; staying off", OFF_LINE]


def test_enabled_mounts_the_route_and_logs_nothing(monkeypatch):
    monkeypatch.setattr(plugin_module, "load_hosts", list)
    host = Host({"enabled": True})
    plugin = bridge_plugin.create_plugin(host)
    assert plugin.enabled is True and plugin.routes() == ["/plugin/devices/state"]
    assert host.lines == []
    plugin.close()


# ── the lease readout: only with a configured lease ────────────────────────────────────────────

LEASE = {"v": 1, "slots": [], "window": {"start": "03:00", "end": "07:00", "open": False}, "live": [], "unreadable": 0}


def lease_file(tmp_path, body):
    path = tmp_path / "my_lease.py"
    path.write_text(body)
    return path


def test_no_lease_setting_means_no_lease_key_and_no_import(monkeypatch):
    monkeypatch.setattr(plugin_module, "load_lease", lambda _path: pytest.fail("loaded a lease nobody configured"))
    plugin = DevicesPlugin(None, settings=ON, load=list, read=lambda: Reading(cores=1), read_sessions=dict)
    assert "lease" not in plugin.state()["devices"][0]
    plugin.close()


def test_a_configured_lease_is_read_on_the_samplers_thread(tmp_path):
    from dataclasses import replace

    path = lease_file(
        tmp_path,
        f"from dataclasses import dataclass\n\n@dataclass\nclass Slot:\n    name: str\n\ndef status():\n    return {LEASE!r}\n",
    )
    plugin = DevicesPlugin(None, settings=replace(ON, lease=path), load=list, read=lambda: Reading(cores=1), read_sessions=dict)
    assert "lease" in plugin.state()["devices"][0]
    assert wait_for(lambda: plugin.state()["devices"][0]["lease"] == LEASE)
    plugin.close()


@pytest.mark.parametrize(
    "body, why",
    [
        ("raise RuntimeError('broken on purpose')\n", "failed to load: RuntimeError: broken on purpose"),
        ("status = 3\n", "defines no callable status()"),
    ],
)
def test_a_lease_file_that_cannot_answer_costs_the_readout_not_the_plugin(tmp_path, body, why):
    from dataclasses import replace

    host = Host()
    plugin = DevicesPlugin(
        host, settings=replace(ON, lease=lease_file(tmp_path, body)), load=list, read=lambda: Reading(cores=1), read_sessions=dict
    )
    assert plugin.enabled and len(host.lines) == 1 and why in host.lines[0]
    assert "lease" not in plugin.state()["devices"][0]
    plugin.close()


# ── the jobs stream: only with a configured command ────────────────────────────────────────────


def test_a_configured_jobs_command_runs_per_host_by_name():
    from dataclasses import replace

    plugin = DevicesPlugin(
        None,
        settings=replace(ON, jobs=("/bin/jobs-tool", "--host", "{host}", "--every", "{seconds}")),
        load=list,
        read=lambda: Reading(cores=1),
    )
    sampler = plugin._make_remote(RemoteHost(name="compute-0", ssh="box2"))
    assert sampler._jobs_argv == ["/bin/jobs-tool", "--host", "compute-0", "--every", "10"]
    plugin.close()


def test_no_jobs_setting_means_no_jobs_stream():
    plugin = DevicesPlugin(None, settings=ON, load=list, read=lambda: Reading(cores=1))
    sampler = plugin._make_remote(RemoteHost(name="compute-0", ssh="box2"))
    assert sampler._jobs_argv is None and "jobs" not in sampler.snapshot()
    plugin.close()
