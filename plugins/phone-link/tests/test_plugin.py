"""The plugin on the bridge: its one route, its caller policy, and the whole link end to end."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest
from voidcraft_world_bridge.testing import PluginClient

import link_client
from fakes import FakeBridge, FakePhone, FakeRunner
from phone_link.paths import LOCK_FILE
from phone_link.plugin import PhoneLinkPlugin, bridge_ports

SERIAL = "0123456789A"


@dataclass
class Context:
    status_port: int = 7682
    port: int = 7681
    logs: list[str] = field(default_factory=list)

    def log(self, line: str) -> None:
        self.logs.append(line)


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def quiet(tmp_path):
    plugin = PhoneLinkPlugin(Context(), start=False, state_dir=tmp_path / "state", find=lambda: None)
    yield plugin
    plugin.close()


def test_one_route_and_a_valid_plugin(quiet):
    assert quiet.routes() == ["/plugin/phone-link/state"]
    client = PluginClient(quiet)
    status, body = client.request("GET", "/state", headers={"Host": "127.0.0.1:7682"})
    assert status == 200
    assert set(body) >= {"v", "adb", "port", "app", "devices", "requests"}
    assert body["v"] == 1 and body["port"] == 7683 and body["devices"] == []
    assert body["app"] == {"package": "world.voidcraft.tldrack", "source": "default", "error": None}
    assert set(body["requests"]) >= {"n", "last_at"}


def test_the_caller_policy(quiet):
    client = PluginClient(quiet)
    # Which page may read is the host's call, made before dispatch: test_guard.py proves it on a real handler.
    assert client.request("GET", "/state", headers={"Origin": "https://rack.example", "Host": "127.0.0.1:7682"})[0] == 200
    assert client.request("GET", "/state", headers={"Host": "mymac.tail1234.ts.net"})[0] == 403
    assert client.request("GET", "/state", headers={"X-Forwarded-For": "10.0.0.2"})[0] == 403
    assert client.request("POST", "/state", body={})[0] == 405
    assert client.request("GET", "/token")[0] == 404


def test_a_refused_app_setting_is_logged_and_shown_and_the_default_stands(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_PHONE_LINK_APP", "x.y; reboot")
    context = Context()
    plugin = PhoneLinkPlugin(context, start=False, state_dir=tmp_path / "state", find=lambda: None)
    try:
        app = plugin.state()["app"]
        assert (app["package"], app["source"]) == ("world.voidcraft.tldrack", "default")
        assert "TLDRACK_PHONE_LINK_APP" in app["error"]
        assert any(app["error"] in line for line in context.logs)
    finally:
        plugin.close()


def test_a_configured_app_is_logged_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_PHONE_LINK_APP", "org.example.review")
    context = Context()
    plugin = PhoneLinkPlugin(context, start=False, state_dir=tmp_path / "state", find=lambda: None)
    try:
        assert plugin.state()["app"] == {"package": "org.example.review", "source": "env", "error": None}
        assert [line for line in context.logs if "org.example.review" in line] == [
            "phone-link: the token goes to org.example.review (set by env)"
        ]
    finally:
        plugin.close()


def test_an_older_host_without_headers_is_refused_not_served(quiet):
    """The pair a host from before `handle_request` calls: it cannot show the caller, so /state fails closed."""
    for status, body in (
        quiet.handle_get("/state", {}),
        quiet.handle_post("/state", {}, {}),
        quiet.handle_request("GET", "/state", {}, None, None),
    ):
        assert (status, body["error"]) == (403, "host_too_old")
        assert "devices" not in body
    assert quiet.handle_get("/token", {})[0] == 404


def test_bridge_ports_include_what_this_bridge_was_given():
    assert bridge_ports(Context(status_port=7792, port=7791)) == {7681, 7682, 7791, 7792}
    assert bridge_ports(object()) == {7681, 7682}


def test_started_without_adb_the_listener_still_answers(tmp_path):
    context = Context()
    plugin = PhoneLinkPlugin(context, state_dir=tmp_path / "state", link_port=0, find=lambda: None)
    try:
        assert wait_for(lambda: plugin.state()["listener"]["up"])
        assert plugin.state()["listener"] == {"up": True, "error": None, "owner": True}
        assert (tmp_path / "state" / LOCK_FILE).is_file()  # the link's lock lives beside the token
        assert wait_for(lambda: plugin.state()["adb"] == "missing")
        assert any("minted the link token" in line for line in context.logs)
    finally:
        plugin.close()
    assert plugin.state()["listener"] == {"up": False, "error": None, "owner": False}


def test_a_second_bridge_on_this_mac_waits_for_the_link(tmp_path):
    first = PhoneLinkPlugin(Context(), state_dir=tmp_path / "state", link_port=0, find=lambda: None)
    second = PhoneLinkPlugin(Context(), state_dir=tmp_path / "state", link_port=0, find=lambda: None)
    try:
        assert wait_for(lambda: first.state()["listener"]["up"])
        assert wait_for(lambda: second.state()["listener"]["error"] == "another bridge holds the link")
        assert second.link_port is None
    finally:
        first.close()
        second.close()


def test_the_link_end_to_end(tmp_path):
    """A phone attaches; the keeper arms it and hands it the token; with that token the phone reaches the
    bridge's RECENT list through the link, and with nothing else it reaches nothing."""
    bridge = FakeBridge()
    bridge.reply_body = b'{"ok":true,"rows":[]}'
    runner = FakeRunner(FakePhone(SERIAL))
    context = Context(status_port=bridge.port)
    plugin = PhoneLinkPlugin(
        context,
        state_dir=tmp_path / "state",
        link_port=0,
        runner=runner,
        find=lambda: "/fake/adb",
        known=lambda: None,
        keeper_options={"backoff": (5.0,)},
    )
    try:
        phone = runner.phones[SERIAL]

        def linked():
            rows = plugin.state()["devices"]
            return bool(rows) and rows[0]["token"] == "pushed" and rows[0]["reverse"] is True

        # The first check can run before the listener has bound; the listener coming up wakes the keeper,
        # so the tunnel is armed now rather than CHECK_SECONDS later.
        assert wait_for(linked)
        assert phone.reverses == {"tcp:7683": "tcp:7683"}

        token = phone.token_file.strip()  # what the app reads from its private storage
        port = plugin.link_port
        assert link_client.call(port, token, "GET", "/health").json() == {"ok": True, "v": 2}
        recent = link_client.call(port, token, "GET", "/plugin/laya/review/recent?limit=5")
        assert (recent.status, recent.json()) == (200, {"ok": True, "rows": []})
        assert bridge.requests[0]["path"] == "/plugin/laya/review/recent?limit=5"
        assert link_client.exchange(port, "GET", "/plugin/laya/review/recent")[0] == 401
        assert link_client.call(port, token, "GET", "/spawn").status == 404
        with pytest.raises(link_client.NotTheLink):  # a stale token on the phone cannot even read a nonce
            link_client.call(port, "S" * 43, "GET", "/health")

        state = plugin.state()
        assert state["requests"]["n"] == 3 and state["requests"]["refused"] == 1
        assert token not in json.dumps(state)
        assert not any(token in line for line in context.logs)
    finally:
        plugin.close()
        bridge.close()
