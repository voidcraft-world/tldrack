"""The bridge stands alone: an empty home is a whole rack, its own page is served from loopback,
and no hosted site is allowed until the operator names one."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import tldrack.main as main
from tldrack import framing
from tldrack.icons import SessionIconStore
from tldrack.models import SessionModelStore
from tldrack.settings import Settings

HOSTED = "https://rack.example"
#: The core's shipped default set names this site. A rack must refuse it unless asked.
CORE_DEFAULT_ORIGIN = "https://voidcraft.world"

RUNNER = """
import sys
from pathlib import Path
from tldrack import main, paths, plugin_sources
from tldrack.identity import capture_identity
from tldrack.settings import load_settings

port, status_port, start_dir = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
# Nothing installed beside it: no checkout plugins either (phone-link would bind its fixed port).
plugin_sources.checkout_plugins_dir = lambda: Path(start_dir) / "no-checkout"
paths.export_env()
control = main.ShutdownControl("status-only")
info = capture_identity("status-only", port, status_port, None, ttyd_owned=False)
main.serve_status(port, status_port, "main", start_dir, info, control, load_settings())
control.event.wait()
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(
            method, path, body=payload, headers={**({"Content-Type": "application/json"} if payload else {}), **(headers or {})}
        )
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_an_empty_home_is_a_whole_bridge(tmp_path):
    """Nothing configured, nothing installed beside it: `/snapshot` answers, the kept stores are on,
    and every directory it made is under that home."""
    home = tmp_path / "home"
    home.mkdir()
    status_port = free_port()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "TMUX_TMPDIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-c", RUNNER, str(free_port()), str(status_port), str(tmp_path)],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline, snapshot = time.monotonic() + 30, None
        while time.monotonic() < deadline and snapshot is None:
            if proc.poll() is not None:
                pytest.fail(f"the bridge exited {proc.returncode}: {proc.stdout.read().decode()[-2000:]}")
            try:
                status, _, raw = request(status_port, "GET", "/snapshot")
                snapshot = json.loads(raw) if status == 200 else None
            except OSError:
                time.sleep(0.2)
        assert snapshot is not None, "no /snapshot 200 within 30 s"
        assert snapshot["sessions"] == [] and snapshot["bridge"]["status_port"] == status_port
        # The routes that used to answer 503 until a parameter directory was named.
        assert request(status_port, "GET", "/inbox")[0] == 200
        assert request(status_port, "GET", "/lead/state")[0] == 200
        assert request(status_port, "GET", "/decisions")[0] == 200
        assert json.loads(request(status_port, "GET", "/ledger/status")[2])["enabled"] is True
        # Loopback only: the site the core serves by default is refused here.
        assert request(status_port, "GET", "/snapshot", {"Origin": CORE_DEFAULT_ORIGIN})[0] == 403
        assert request(status_port, "GET", "/snapshot", {"Origin": f"http://127.0.0.1:{status_port}"})[0] == 200
        data = home / ".local" / "share" / "tldrack"
        assert (data / "agent-rack" / "rack.sqlite").is_file() and (data / "agent-ledger").is_dir()
        made = {path.name for path in home.iterdir()}
        assert made <= {".config", ".local", ".cache"}, f"wrote outside its three directories: {made}"
    finally:
        try:
            request(status_port, "POST", "/shutdown", body={})
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A handler on a real loopback socket, so the guards in do_GET/do_POST run as they do live."""
    monkeypatch.setattr(main, "list_tmux_sessions", dict)
    monkeypatch.setattr(main, "list_tmux_clients", dict)
    servers = []

    def start(**kwargs) -> int:
        handler = main.build_status_handler(
            store=main.StatusStore(),
            icon_store=SessionIconStore(tmp_path / "icons.json"),
            model_store=SessionModelStore(tmp_path / "models.json"),
            default_session="test",
            bridge_info={"pid": 1, "started_at": 0.0},
            control=main.ShutdownControl("status-only"),
            **kwargs,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>rack</title>")
    (root / "assets" / "app-abc123.js").write_text("export const rack = 1")
    (tmp_path / "secret.txt").write_text("never served")
    return root


def test_the_page_is_served_at_root_from_the_build(serve, dist):
    port = serve(find_ui=lambda: dist)
    status, headers, body = request(port, "GET", "/")
    assert status == 200 and body.startswith(b"<!doctype html>")
    assert headers["Content-Type"] == "text/html; charset=utf-8" and headers["Cache-Control"] == "no-cache"
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, headers, body = request(port, "GET", "/assets/app-abc123.js")
    assert status == 200 and body == b"export const rack = 1" and "immutable" in headers["Cache-Control"]


def test_the_api_is_never_shadowed_and_a_missing_file_is_the_plain_404(serve, dist):
    (dist / "snapshot").write_text("a build cannot answer for the API")
    (dist / "status").write_text("nor for a host route")
    port = serve(find_ui=lambda: dist)
    assert json.loads(request(port, "GET", "/snapshot")[2])["sessions"] == []
    assert request(port, "GET", "/status")[0] != 200 or b"host route" not in request(port, "GET", "/status")[2]
    status, _, body = request(port, "GET", "/nope.js")
    assert status == 404 and b"unknown path" in body
    for escape in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        status, _, body = request(port, "GET", escape)
        assert status == 404 and b"never served" not in body, escape


def test_every_response_refuses_a_parent_that_may_not_call_the_bridge(serve, dist):
    """The rack page, the API and the 404 alike: framing.py owns why."""
    default = serve(find_ui=lambda: dist)
    for path in ("/", "/snapshot", "/nope.js"):
        headers = request(default, "GET", path)[1]
        assert headers["Content-Security-Policy"] == framing.frame_ancestors(()), path
    configured = serve(find_ui=lambda: dist, settings=Settings(allowed_origins=frozenset({HOSTED})))
    assert request(configured, "GET", "/")[1]["Content-Security-Policy"].endswith(f" {HOSTED}")


def test_the_page_obeys_the_guards_like_every_route(serve, dist):
    port = serve(find_ui=lambda: dist)
    assert request(port, "GET", "/", {"Origin": "https://evil.example"})[0] == 403
    assert request(port, "GET", "/", {"Host": "evil.example"})[0] == 403
    assert request(port, "GET", "/", {"Origin": f"http://localhost:{port}"})[0] == 200


def test_a_bridge_with_no_build_says_how_to_get_one(serve):
    port = serve(find_ui=lambda: None)
    status, headers, body = request(port, "GET", "/")
    assert status == 503 and b"tldrack up" in body and headers["Content-Type"].startswith("text/html")
    assert request(port, "GET", "/assets/app.js")[0] == 404
    assert request(serve(), "GET", "/")[0] == 503, "a handler built with no finder has no page either"


def test_a_build_that_lands_after_startup_is_served_without_a_restart(serve, dist):
    found = []
    port = serve(find_ui=lambda: found[0] if found else None)
    assert request(port, "GET", "/")[0] == 503
    found.append(dist)
    assert request(port, "GET", "/")[0] == 200


def test_loopback_only_by_default_and_the_operator_adds_origins(serve):
    default = serve()
    for origin in (CORE_DEFAULT_ORIGIN, HOSTED):
        status, headers, _ = request(default, "GET", "/snapshot", {"Origin": origin})
        assert status == 403 and "Access-Control-Allow-Origin" not in headers
        assert request(default, "OPTIONS", "/hook", {"Origin": origin})[0] == 403
    assert request(default, "GET", "/snapshot", {"Origin": "http://localhost:5173"})[0] == 200
    assert request(default, "GET", "/snapshot")[0] == 200, "a native client sends no Origin"

    configured = serve(settings=Settings(allowed_origins=frozenset({HOSTED})))
    status, headers, _ = request(configured, "GET", "/snapshot", {"Origin": HOSTED})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == HOSTED
    assert request(configured, "GET", "/snapshot", {"Origin": CORE_DEFAULT_ORIGIN})[0] == 403, (
        "the operator's set replaces the default: it never extends the core's"
    )


def test_the_module_level_guard_is_loopback_only_too():
    assert main.origin_allowed(None) and main.origin_allowed("http://127.0.0.1:7682")
    assert not main.origin_allowed(CORE_DEFAULT_ORIGIN)
    assert main.origin_allowed(HOSTED, frozenset({HOSTED}))
