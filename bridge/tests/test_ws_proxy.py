"""GET /ws — the origin-checked front door to ttyd's socket.

This file exists because of a five-week silent outage. On 2026-08-09 ttyd gained
`-O` (`--check-origin`), correctly: without it any page the user has open can
open `ws://127.0.0.1:7681/ws` and write to the tty. The hardening note recorded
that an app-origin handshake would be dropped correctly, since no
such handshake existed — but the native terminal panel had been opening exactly
that handshake since 2026-07-25, so it died on the spot while
the BRIDGE indicator stayed green (it polls the status server on the other port,
which `-O` does not touch).

Nothing type-checks prose, so the claim is asserted here instead. The end-to-end
cases below drive a real socket through a real handler into a stub ttyd, because
the pure-function tests above them would all have passed throughout the outage.
"""

import socket
import threading
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.settings import Settings
from tldrack.ws_proxy import (
    MAX_PROXY_SOCKETS,
    ProxySlot,
    active_count,
    build_upstream_request,
    is_websocket_upgrade,
)

# A hosted page's origin: served only because the operator's settings name it.
APP_ORIGIN = "https://rack.example"
APP_SETTINGS = Settings(allowed_origins=frozenset({APP_ORIGIN}))
UPSTREAM = "127.0.0.1:7681"


def headers(**kwargs) -> Message:
    msg = Message()
    for name, value in kwargs.items():
        msg[name.replace("_", "-")] = value
    return msg


# --------------------------------------------------------------------------
# is_websocket_upgrade
# --------------------------------------------------------------------------


def test_plain_get_is_not_an_upgrade():
    assert not is_websocket_upgrade(headers(Host=UPSTREAM))


def test_upgrade_token_is_found_in_a_connection_list():
    """Chrome sends `Connection: Upgrade`; some clients send a comma list."""
    assert is_websocket_upgrade(headers(Upgrade="websocket", Connection="keep-alive, Upgrade"))


def test_upgrade_matching_ignores_case():
    assert is_websocket_upgrade(headers(Upgrade="WebSocket", Connection="upgrade"))


def test_upgrade_header_alone_is_not_enough():
    assert not is_websocket_upgrade(headers(Upgrade="websocket", Connection="close"))


# --------------------------------------------------------------------------
# build_upstream_request — the security boundary
# --------------------------------------------------------------------------


def rewritten(**extra) -> str:
    base = dict(
        Host="127.0.0.1:7682",
        Origin=APP_ORIGIN,
        Upgrade="websocket",
        Connection="Upgrade",
        Sec_WebSocket_Key="dGhlIHNhbXBsZSBub25jZQ==",
        Sec_WebSocket_Version="13",
        Sec_WebSocket_Protocol="tty",
    )
    base.update(extra)
    return build_upstream_request("/ws?arg=attach:deploy-dev", headers(**base), UPSTREAM).decode()


def test_caller_origin_is_replaced_not_forwarded():
    """The caller's origin was already judged by origin_allowed(); forwarding it
    would only hand ttyd a value `-O` is guaranteed to reject."""
    out = rewritten()
    assert f"Origin: http://{UPSTREAM}" in out
    assert APP_ORIGIN not in out


def test_host_is_rewritten_to_match_the_origin():
    """`-O` compares the two, so a rewritten Origin with the original Host fails
    just as hard as no rewrite at all."""
    out = rewritten()
    assert f"Host: {UPSTREAM}" in out
    assert "Host: 127.0.0.1:7682" not in out


def test_websocket_key_survives():
    """ttyd derives the 101 accept token from it; a fresh key makes the browser
    reject the handshake it gets back."""
    assert "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" in rewritten()
    assert "Sec-WebSocket-Protocol: tty" in rewritten()


def test_path_and_query_are_forwarded_verbatim():
    """`?arg=` is how a session is chosen — dropping the query would silently
    attach every panel to the bridge's default session."""
    assert rewritten().splitlines()[0] == "GET /ws?arg=attach:deploy-dev HTTP/1.1"


def test_hop_by_hop_headers_are_dropped():
    out = rewritten(Transfer_Encoding="chunked", TE="trailers", Keep_Alive="timeout=5")
    assert "Transfer-Encoding" not in out
    assert "Keep-Alive" not in out
    assert "\r\nTE:" not in out


def test_upgrade_pair_is_rebuilt_exactly_once():
    """A `Connection: keep-alive, Upgrade` from one client must not reach ttyd as
    something it reads differently."""
    out = rewritten(Connection="keep-alive, Upgrade")
    assert out.count("Connection: Upgrade") == 1
    assert out.count("Upgrade: websocket") == 1
    assert "keep-alive" not in out


def test_request_ends_with_a_blank_line():
    assert rewritten().endswith("\r\n\r\n")


# --------------------------------------------------------------------------
# ProxySlot
# --------------------------------------------------------------------------


def test_slots_are_capped_and_released():
    assert active_count() == 0
    held = [ProxySlot().__enter__() for _ in range(MAX_PROXY_SOCKETS)]
    assert all(held)
    overflow = ProxySlot()
    with overflow as slot:
        assert not slot, "the cap must refuse the extra socket"
    for slot in held:
        slot.__exit__(None, None, None)
    assert active_count() == 0


# --------------------------------------------------------------------------
# End to end: browser → status server → stub ttyd
# --------------------------------------------------------------------------


class StubTtyd:
    """Stands in for ttyd: records the handshake it was given, then echoes.

    Records rather than validates, so a test can assert on what the proxy
    actually sent instead of only on whether the connection survived.
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.seen: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            request += chunk
        self.seen.append(request.decode())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                break
            if not data:
                break
            conn.sendall(data.upper())
        conn.close()


@pytest.fixture
def bridge(tmp_path):
    """A real status server on an ephemeral port, fronting a stub ttyd."""
    ttyd = StubTtyd()
    handler = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="voidcraft",
        bridge_info={"pid": 1234, "started_at": 0.0, "port": ttyd.port},
        control=ShutdownControl("status-only"),
        settings=APP_SETTINGS,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], ttyd
    server.shutdown()
    ttyd.sock.close()


def handshake(port: int, origin: str | None, path: str = "/ws?arg=attach:deploy-dev") -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Protocol: tty",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return client


def read_status_line(client: socket.socket) -> str:
    client.settimeout(5)
    data = b""
    while b"\r\n" not in data:
        chunk = client.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode().strip()


def test_app_origin_reaches_ttyd(bridge):
    """THE regression test. An app-origin handshake — the one the hardening note
    said did not exist — must come back 101 and relay in both directions."""
    port, ttyd = bridge
    client = handshake(port, APP_ORIGIN)
    assert read_status_line(client) == "HTTP/1.1 101 Switching Protocols"
    client.recv(4096)  # rest of the upstream's response headers
    client.sendall(b"hello")
    assert client.recv(4096) == b"HELLO", "bytes must relay both ways after the upgrade"
    client.close()


def test_ttyd_is_handed_a_matching_origin(bridge):
    """What `-O` actually checks. Asserted on the bytes ttyd received, not on the
    proxy's intent."""
    port, ttyd = bridge
    client = handshake(port, APP_ORIGIN)
    read_status_line(client)
    assert ttyd.seen, "the proxy never dialed upstream"
    seen = ttyd.seen[0]
    assert f"Origin: http://127.0.0.1:{ttyd.port}" in seen
    assert f"Host: 127.0.0.1:{ttyd.port}" in seen
    assert APP_ORIGIN not in seen
    client.close()


def test_a_loopback_dev_origin_is_allowed(bridge):
    """The vite dev server roams 5173-5179 and mkcert flips the scheme."""
    port, _ = bridge
    client = handshake(port, "http://localhost:5173")
    assert read_status_line(client) == "HTTP/1.1 101 Switching Protocols"
    client.close()


def test_a_foreign_origin_never_reaches_ttyd(bridge):
    """The drive-by RCE `-O` was added to stop. The bridge must refuse it BEFORE
    dialing, or the proxy would have re-opened the hole it is fronting."""
    port, ttyd = bridge
    client = handshake(port, "https://evil.example")
    assert "403" in read_status_line(client)
    assert ttyd.seen == [], "a rejected origin must not reach ttyd at all"
    client.close()


def test_a_plain_get_on_ws_is_a_400_not_a_hijack(bridge):
    port, ttyd = bridge
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {APP_ORIGIN}\r\n\r\n".encode())
    assert "400" in read_status_line(client)
    assert ttyd.seen == []
    client.close()


def test_ttyd_down_is_a_502_not_a_silent_hang(bridge, tmp_path):
    """A green bridge beside a dead terminal is the exact failure this route
    exists to fix; it must not be rebuilt one layer up."""
    _, ttyd = bridge
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()  # nothing listens here now
    handler = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "i.json"),
        model_store=SessionModelStore(tmp_path / "m.json"),
        default_session="voidcraft",
        bridge_info={"pid": 1, "started_at": 0.0, "port": dead_port},
        control=ShutdownControl("status-only"),
        settings=APP_SETTINGS,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = handshake(server.server_address[1], APP_ORIGIN)
        assert "502" in read_status_line(client)
        client.close()
    finally:
        server.shutdown()
