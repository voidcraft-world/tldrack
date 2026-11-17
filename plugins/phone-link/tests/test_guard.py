"""`GET /plugin/phone-link/state`'s caller policy: this Mac only, and the origins are the host's.

The plugin's own policy admits any Origin on purpose (guard.py); the last test proves, on a real core handler, that
the host's origin guard turns a stranger's page away before the plugin runs. The link's own listener has its own,
stricter gate (test_gate_and_allowlist.py).
"""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass

import pytest
from voidcraft_world_bridge.server import build_handler, make_server

from phone_link import guard
from phone_link.config import LinkConfig
from phone_link.plugin import PhoneLinkPlugin


@pytest.mark.parametrize("host", [None, "127.0.0.1:7682", "localhost:7682", "[::1]:7682", "localhost"])
def test_a_loopback_host_may_read(host):
    assert guard.request_refusal({} if host is None else {"host": host}) is None


@pytest.mark.parametrize("host", ["evil.example", "mymac.tail1234.ts.net", "", "127.0.0.1.evil.example"])
def test_any_other_host_is_refused(host):
    assert guard.request_refusal({"host": host}).code == "host_refused"


@pytest.mark.parametrize(
    "header",
    [
        {"x-forwarded-for": "10.0.0.2"},
        {"Via": "1.1 proxy"},
        {"Forwarded": "for=x"},
        {"x-real-ip": "10.0.0.2"},
        {"tailscale-user-login": "x"},
    ],
)
def test_a_proxied_request_is_refused(header):
    assert guard.request_refusal({"host": "127.0.0.1:7682", **header}).code == "host_refused"


@pytest.mark.parametrize("site", ["cross-site", "same-site", "same-origin"])
def test_a_browser_request_that_hid_its_origin_is_refused(site):
    assert guard.request_refusal({"host": "127.0.0.1:7682", "sec-fetch-site": site}).code == "no_cors_refused"


def test_a_typed_url_may_read():
    assert guard.request_refusal({"host": "127.0.0.1:7682", "sec-fetch-site": "none"}) is None


def test_which_page_may_read_is_left_to_the_host():
    assert guard.POLICY.allowed_origins is None
    assert guard.request_refusal({"host": "127.0.0.1:7682", "origin": "https://anything.example"}) is None


@dataclass
class Context:
    status_port: int = 7682
    port: int = 7681

    def log(self, line: str) -> None:
        pass


@pytest.fixture
def host(tmp_path):
    """A real core handler carrying this plugin, with one configured origin: what the rack's bridge serves."""
    plugin = PhoneLinkPlugin(Context(), start=False, state_dir=tmp_path / "state", find=lambda: None, config=LinkConfig())
    server = make_server(0, build_handler({plugin.name: plugin}, allowed_origins=["https://rack.example"]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        plugin.close()


def get_state(port: int, origin: str | None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/plugin/phone-link/state", headers={"Origin": origin} if origin else {})
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read() or b"{}")
    finally:
        conn.close()


def test_the_host_turns_a_strange_page_away_before_the_plugin_runs(host):
    status, body = get_state(host, "https://evil.example")
    assert status == 403 and "devices" not in body
    for origin in (None, "https://rack.example", "http://localhost:5173", "http://127.0.0.1:7682"):
        status, body = get_state(host, origin)
        assert status == 200 and body["devices"] == [], origin
