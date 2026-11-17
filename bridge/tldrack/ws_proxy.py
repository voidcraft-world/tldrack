"""Front ttyd's WebSocket behind the bridge's own Origin/Host guards.

ttyd runs with `-O` (`--check-origin`), which drops any handshake whose `Origin`
differs from its `Host`. That is load-bearing — without it any page the user has
open can `new WebSocket('ws://127.0.0.1:7681/ws', ['tty'])`, pass the empty
`{"AuthToken":""}` handshake and write to the tty. See bridge/CLAUDE.md.

The cost is that a rack surface which is NOT an iframe served by ttyd cannot
reach the socket at all: its handshake carries the *app's* origin. The agent
rack's panes are iframes (ttyd serves the page, so origin == host and `-O`
passes); a terminal panel that drives a native xterm.js over a raw socket is
not, and `-O` killed it on 2026-08-09. The hardening note asserted that no
such handshake existed — it did, and nothing type-checks prose.

So the app-origin socket gets a door that is CHECKED rather than absent:

    browser ──ws://127.0.0.1:<status>/ws?arg=x──▶ bridge ──▶ ttyd :<ttyd>
                                                    │
                          host_allowed() + origin_allowed(), then Origin
                          rewritten to the upstream's own origin so `-O` passes

`-O` stays on as defence in depth: it still refuses anything that reaches 7681
directly. The bridge, not ttyd, is now the thing that decides WHICH origins may
speak to a shell. (Rebinding against the terminal port itself: owned by ttyd_front.py — see the note there.)

Nothing here parses WebSocket frames. After the upstream answers, this is a byte
relay; ttyd owns the protocol, exactly as it does for the iframes.
"""

from __future__ import annotations

import select
import socket
import threading

# One thread is held for the life of each proxied socket (ThreadingHTTPServer
# gives a thread per connection), so this is capped for the same reason
# MAX_SSE_CLIENTS is: a bounded resource a page could otherwise exhaust. A
# terminal panel, a popped-out rack and a couple of stale tabs is the real
# ceiling; 8 leaves room without letting a loop eat the process.
MAX_PROXY_SOCKETS = 8

# Hop-by-hop headers are per-connection by definition (RFC 9110 §7.6.1) and must
# not be forwarded. `Upgrade` and `Connection` are the exception that proves the
# rule: they are the hop-by-hop headers this hop is *about*, so they are rebuilt
# below rather than copied.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Rewritten, never forwarded: these two are the entire point of the proxy.
_REWRITTEN = {"host", "origin"}

_active_lock = threading.Lock()
_active_count = 0


def is_websocket_upgrade(headers) -> bool:
    """True when this request is a WebSocket handshake and not a plain GET.

    Checked before anything is dialed so a stray `GET /ws` in a browser address
    bar gets a JSON 400 rather than a hijacked socket.
    """
    if headers.get("Upgrade", "").strip().lower() != "websocket":
        return False
    # `Connection` is a comma list and its casing is not normative.
    tokens = {t.strip().lower() for t in (headers.get("Connection", "") or "").split(",")}
    return "upgrade" in tokens


def build_upstream_request(path: str, headers, upstream_host: str) -> bytes:
    """The handshake to replay at ttyd, with Host and Origin made to match.

    This is the whole security boundary in one pure function, so it is the one
    thing worth testing exhaustively:

    - `Origin` becomes `http://<upstream_host>` — NOT the caller's. The caller's
      origin was already judged by `origin_allowed()`; forwarding it would only
      hand ttyd a value it is guaranteed to reject.
    - `Host` becomes `<upstream_host>`, because `-O` compares the two and a
      rewritten Origin with the original Host fails just as hard.
    - Hop-by-hop headers are dropped and the upgrade pair is rebuilt, so a
      `Connection: keep-alive, Upgrade` from one client cannot leak through as
      something ttyd reads differently.

    `Sec-WebSocket-Key`/`-Version`/`-Protocol` ARE forwarded: ttyd computes the
    accept token from the key, so the client's own value has to survive or the
    browser rejects the 101 it gets back.
    """
    lines = [f"GET {path} HTTP/1.1"]
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or lowered in _REWRITTEN:
            continue
        lines.append(f"{name}: {value}")
    lines.append(f"Host: {upstream_host}")
    lines.append(f"Origin: http://{upstream_host}")
    lines.append("Connection: Upgrade")
    lines.append("Upgrade: websocket")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def pending_buffered(rfile, sock: socket.socket) -> bytes:
    """Bytes the HTTP header parser pulled into its buffer past the request.

    `BaseHTTPRequestHandler` reads headers through a buffered reader, so a client
    that pipelined anything after the handshake has it sitting in Python's buffer
    where a raw `recv` on the socket will never see it. Browsers wait for the 101
    before sending a frame, so this is almost always empty — "almost" is why it
    is drained instead of assumed.

    Non-blocking for the duration: with an empty buffer `peek` performs one raw
    read, and that must not block waiting for a frame the client has not sent.
    """
    sock.setblocking(False)
    try:
        buffered = rfile.peek(0)
        return rfile.read(len(buffered)) if buffered else b""
    except (BlockingIOError, OSError, ValueError):
        return b""
    finally:
        sock.setblocking(True)


def relay(client: socket.socket, upstream: socket.socket, chunk: int = 65536) -> None:
    """Pump bytes both ways until either end hangs up.

    Deliberately protocol-blind. ttyd already owns framing, ping/pong and the
    `{"AuthToken":""}` handshake for every iframe pane; teaching a second
    component the wire format would be two implementations to keep in step.
    """
    socks = [client, upstream]
    try:
        while True:
            readable, _, errored = select.select(socks, [], socks)
            if errored:
                return
            for sock in readable:
                other = upstream if sock is client else client
                try:
                    data = sock.recv(chunk)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return
    finally:
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ProxySlot:
    """Context manager holding one of MAX_PROXY_SOCKETS. Falsy when full."""

    def __init__(self) -> None:
        self.acquired = False

    def __enter__(self) -> ProxySlot:
        global _active_count
        with _active_lock:
            if _active_count < MAX_PROXY_SOCKETS:
                _active_count += 1
                self.acquired = True
        return self

    def __exit__(self, *_exc) -> None:
        global _active_count
        if self.acquired:
            with _active_lock:
                _active_count -= 1

    def __bool__(self) -> bool:
        return self.acquired


def active_count() -> int:
    """Live proxied sockets — for tests and for /snapshot if it ever wants it."""
    with _active_lock:
        return _active_count
