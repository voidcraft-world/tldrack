"""The terminal port's front door: a Host check, then bytes to ttyd's Unix socket.

ttyd cannot be told which names it answers to. Its `-O` refuses a WebSocket whose `Origin` differs
from its `Host`, and under DNS rebinding the two agree: a page on the attacker's hostname, re-resolved
to 127.0.0.1, reaches a loopback port as a same-origin request. With no credential and `-W`, that
handshake was a writable shell. The status listener has had the answer since it was written
(`host_allowed`, the core's guard); the terminal port had none.

So ttyd no longer listens on the network. It serves a Unix socket in a directory only this account
can enter, and this module listens on the terminal port in its place:

    browser ──http://localhost:<port>/…──▶ TtydFront ──▶ ttyd.sock
                                              │
                               the first request's Host, through host_allowed()

Two kinds of request reach it, and each gets one Host check on its head:

- **A WebSocket upgrade** (a pane's shell) is then a byte relay, as `ws_proxy.relay` is: ttyd owns
  the WebSocket and its own `-O`, which still runs and still sees the browser's real `Host` and
  `Origin`. Nothing runs per keystroke: bytes are copied between two open sockets.
- **Anything else** (the page, its token) is one request on its own connection. It goes up with
  `Connection: close`, and ttyd's response head comes back with the frame policy (`framing.py` owns
  why) and `Connection: close` added; the body is relayed untouched. ttyd cannot set a header, and
  this is the only place the terminal page's response passes through. Closing after one answer
  also means the Host check reads EVERY plain request, never only a connection's first.

Stdlib only.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from tldrack import framing

#: A request head longer than this is refused: a terminal URL carries a theme, never this much.
HEAD_LIMIT = 64 * 1024
#: How long a connection may take to send its head (a slow sender holds one thread, no longer).
HEAD_TIMEOUT_S = 10.0
#: How long ttyd may take to start its answer to a plain request (it serves from memory: instantly).
RESPONSE_HEAD_TIMEOUT_S = 30.0
#: Panes, their page loads and a few stale tabs; a bound, so a loop cannot take every thread.
MAX_CONNECTIONS = 256
#: ttyd creates its socket a moment after it starts: a connection that arrives first waits this long.
CONNECT_WAIT_S = 3.0
CHUNK = 65536
SOCKET_NAME = "ttyd.sock"
SOCKET_DIR_PREFIX = "tldrack-ttyd-"
#: `sun_path` is 104 bytes on macOS and 108 on Linux.
SOCKET_PATH_MAX = 100


def _response(status: str, text: str) -> bytes:
    body = f"{text}\n".encode()
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


REFUSED = _response("403 Forbidden", "host not allowed")
BAD_REQUEST = _response("400 Bad Request", "bad request")
UNAVAILABLE = _response("502 Bad Gateway", "the terminal server is not answering")
BUSY = _response("503 Service Unavailable", "too many terminal connections")


def make_socket_dir() -> Path:
    """A fresh directory only this account can enter (mkdtemp: 0700), short enough for a socket path."""
    made = Path(tempfile.mkdtemp(prefix=SOCKET_DIR_PREFIX))
    if len(str(made / SOCKET_NAME).encode()) > SOCKET_PATH_MAX:
        shutil.rmtree(made, ignore_errors=True)
        made = Path(tempfile.mkdtemp(prefix=SOCKET_DIR_PREFIX, dir="/tmp"))
    return made


def orphan_ttyds(ps_output: str, prefix: str = SOCKET_DIR_PREFIX) -> list[tuple[int, Path]]:
    """`(pid, socket directory)` of every ttyd on one of our sockets whose bridge is gone, from
    `ps -axo pid=,ppid=,command=`. A bridge killed outright (SIGKILL) cannot stop its ttyd, and a ttyd on a
    socket holds no port a later start would trip over, so nothing else would ever find it. Named three
    ways before it is signalled: the command is ttyd, its parent is init, and its `-i` is a socket this
    module made."""
    found = []
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or fields[1] != "1":
            continue
        argv = fields[2].split(" ")
        if Path(argv[0]).name != "ttyd" or len(argv) < 3 or argv[1] != "-i":
            continue
        socket_path = Path(argv[2])
        if socket_path.name == SOCKET_NAME and socket_path.parent.name.startswith(prefix):
            found.append((int(fields[0]), socket_path.parent))
    return found


def read_head(conn: socket.socket, limit: int = HEAD_LIMIT, buffered: bytes = b"") -> bytes | None:
    """Everything received up to and including the end of the first head (and whatever arrived with
    it), starting from `buffered`; None when the peer closed, stalled, or sent more than `limit`
    without ending it."""
    data = buffered
    while b"\r\n\r\n" not in data:
        if len(data) > limit:
            return None
        try:
            chunk = conn.recv(CHUNK)
        except OSError:
            return None
        if not chunk:
            return None
        data += chunk
    return data


def host_header(head: bytes) -> tuple[bool, str | None]:
    """`(well_formed, host)` of a request head. Two `Host` headers, or bytes that are not a header
    block, are not well formed: which one a server behind us would believe is not ours to guess."""
    block = head.split(b"\r\n\r\n", 1)[0]
    try:
        lines = block.decode("latin-1").split("\r\n")
    except UnicodeDecodeError:  # latin-1 decodes every byte; kept so a change of codec stays a refusal
        return False, None
    if not lines or len(lines[0].split(" ")) != 3:
        return False, None
    hosts = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or name != name.strip() or not name:
            return False, None
        if name.lower() == "host":
            hosts.append(value.strip())
    if len(hosts) > 1:
        return False, None
    return True, (hosts[0] if hosts else None)


#: Headers that describe one connection, not the message: replaced on the way through.
HOP_HEADERS = frozenset({b"connection", b"keep-alive", b"proxy-connection"})


def _header_name(line: bytes) -> bytes:
    return line.split(b":", 1)[0].strip().lower()


def is_upgrade(head: bytes) -> bool:
    """Whether a request head asks for a WebSocket. A browser can put `Upgrade` only on a WebSocket
    handshake, never on a navigation, so a framed page load always takes the stamped path."""
    block = head.split(b"\r\n\r\n", 1)[0]
    for line in block.split(b"\r\n")[1:]:
        if _header_name(line) == b"upgrade" and b"websocket" in line.split(b":", 1)[1].lower():
            return True
    return False


def _with_headers(head: bytes, added: list[bytes]) -> bytes:
    """`head` (and whatever came after it) with its hop-by-hop headers swapped for `added`."""
    block, sep, rest = head.partition(b"\r\n\r\n")
    lines = block.split(b"\r\n")
    kept = [lines[0], *(line for line in lines[1:] if _header_name(line) not in HOP_HEADERS)]
    return b"\r\n".join([*kept, *added]) + sep + rest


def close_request(head: bytes) -> bytes:
    """A plain request as it goes up: one answer, then the connection ends."""
    return _with_headers(head, [b"Connection: close"])


def stamp_response(head: bytes, frame_policy: str) -> bytes:
    """ttyd's response head as it goes down: the frame policy added, and the connection ending."""
    return _with_headers(head, [b"Content-Security-Policy: " + frame_policy.encode("ascii"), b"Connection: close"])


def status_code(head: bytes) -> int | None:
    """The status of a response head, or None when its first line is not `HTTP/1.x NNN …`."""
    parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1.") or len(parts[1]) != 3 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _copy(source: socket.socket, sink: socket.socket) -> None:
    """One direction, until `source` ends or either side fails; then both are shut, which ends the other direction."""
    try:
        while True:
            data = source.recv(CHUNK)
            if not data:
                return
            sink.sendall(data)
    except OSError:
        return
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _forward_request_body(client: socket.socket, upstream: socket.socket) -> None:
    """The client's half of a plain request: a body, if any, goes up, and its end goes up as a
    half-close, the way a TCP relay carries a FIN. The answer still comes back: ttyd closes after it
    (`Connection: close`), and one that kept the connection would see the hang-up and close then."""
    try:
        while True:
            data = client.recv(CHUNK)
            if not data:
                upstream.shutdown(socket.SHUT_WR)
                return
            upstream.sendall(data)
    except OSError:
        return


def pump(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until either end hangs up. One thread a direction, so a full pipe one way
    never stops the other: a large paste fills the way in while its echo fills the way out, and a
    single loop that blocks on a send would then wait on itself."""
    back = threading.Thread(target=_copy, args=(upstream, client), name="ttyd-front-out", daemon=True)
    back.start()
    _copy(client, upstream)
    back.join()


class TtydFront:
    """Listens on the terminal port and hands each allowed connection to ttyd's socket."""

    def __init__(
        self,
        port: int,
        socket_path: Path,
        allowed: Callable[[str | None], bool],
        *,
        log: Callable[[str], object] | None = None,
        max_connections: int = MAX_CONNECTIONS,
        frame_policy: str = framing.frame_ancestors(()),
    ) -> None:
        self.port = port
        self.socket_path = socket_path
        self._allowed = allowed
        # Loopback only unless the caller passes the operator's list (main.py does): framing.py owns why.
        self.frame_policy = frame_policy
        self._log = log or (lambda _line: None)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._listener: socket.socket | None = None
        self._closed = threading.Event()
        self.refused = 0

    def start(self) -> None:
        """Bind and serve on a daemon thread. Raises OSError when the port is taken."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(128)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self.port = listener.getsockname()[1]
        threading.Thread(target=self._accept, name="ttyd-front", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                if self._closed.is_set():
                    return
                time.sleep(0.05)  # out of descriptors, say: do not spin
                continue
            threading.Thread(target=self._serve, args=(conn,), name="ttyd-front-conn", daemon=True).start()

    def _answer(self, conn: socket.socket, response: bytes) -> None:
        try:
            conn.sendall(response)
        except OSError:
            pass

    def _exchange(self, conn: socket.socket, upstream: socket.socket, head: bytes) -> None:
        """One plain request and its answer, stamped (module docstring). A response that is not
        HTTP is a 502: the page is never served without its frame policy."""
        upstream.sendall(close_request(head))
        # A request body goes up on its own thread, so neither direction waits on the other (`pump`).
        up = threading.Thread(target=_forward_request_body, args=(conn, upstream), name="ttyd-front-in", daemon=True)
        up.start()
        upstream.settimeout(RESPONSE_HEAD_TIMEOUT_S)
        buffered = b""
        while True:
            received = read_head(upstream, buffered=buffered)
            code = status_code(received) if received is not None else None
            if received is None or code is None or code == 101:
                self._answer(conn, UNAVAILABLE)
                return
            answer, _, buffered = received.partition(b"\r\n\r\n")
            if code >= 200:
                break
            conn.sendall(answer + b"\r\n\r\n")  # `100 Continue` and kin: the final answer follows
        conn.sendall(stamp_response(answer + b"\r\n\r\n", self.frame_policy) + buffered)
        upstream.settimeout(None)
        _copy(upstream, conn)
        up.join()

    def _connect(self) -> socket.socket | None:
        deadline = time.monotonic() + CONNECT_WAIT_S
        while True:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                upstream.connect(str(self.socket_path))
                return upstream
            except OSError:
                upstream.close()
                if time.monotonic() >= deadline or self._closed.is_set():
                    return None
                time.sleep(0.1)

    def _serve(self, conn: socket.socket) -> None:
        upstream: socket.socket | None = None
        held = self._slots.acquire(blocking=False)
        try:
            if not held:
                self._answer(conn, BUSY)
                return
            conn.settimeout(HEAD_TIMEOUT_S)
            head = read_head(conn)
            if head is None:
                return
            well_formed, host = host_header(head)
            if not well_formed:
                self._answer(conn, BAD_REQUEST)
                return
            if not self._allowed(host):
                self.refused += 1
                self._log(f"terminal port: refused a request addressed to {host!r} (not a name this bridge answers to)")
                self._answer(conn, REFUSED)
                return
            upstream = self._connect()
            if upstream is None:
                self._answer(conn, UNAVAILABLE)
                return
            conn.settimeout(None)
            if is_upgrade(head):
                upstream.sendall(head)
                pump(conn, upstream)
            else:
                self._exchange(conn, upstream, head)
        except OSError:
            pass
        finally:
            if held:
                self._slots.release()
            for sock in (conn, upstream):
                if sock is None:
                    continue
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
