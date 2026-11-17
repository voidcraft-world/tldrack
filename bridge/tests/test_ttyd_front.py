"""ttyd_front.py: the terminal port answers only to names the bridge answers to, then relays bytes."""

from __future__ import annotations

import shutil
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from tldrack import framing, main, ttyd_front
from tldrack.ttyd_front import (
    TtydFront,
    close_request,
    host_header,
    is_upgrade,
    read_head,
    stamp_response,
    status_code,
)

HEAD = b"GET /?arg=attach:demo HTTP/1.1\r\nHost: localhost:7681\r\nUpgrade: websocket\r\n\r\n"
REPLY = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
PAGE_HEAD = b"GET /?arg=demo HTTP/1.1\r\nHost: localhost:7681\r\nConnection: keep-alive\r\nKeep-Alive: 300\r\n\r\n"
PAGE = b"<!doctype html><title>ttyd</title>" + b"x" * 200_000
#: What ttyd answers for its page: lowercase names, and a keep-alive the front must not pass on.
PAGE_REPLY = (
    b"HTTP/1.1 200 OK\r\nserver: ttyd\r\ncontent-type: text/html\r\nconnection: keep-alive\r\n"
    + f"content-length: {len(PAGE)}\r\n\r\n".encode()
    + PAGE
)


class Upstream:
    """Stands in for ttyd on a Unix socket: a WebSocket is answered once, then echoed; a plain request
    gets `plain_reply`, and the connection ends unless `keep_open`. Keeps what it was sent."""

    def __init__(self, path: Path, plain_reply: bytes = PAGE_REPLY, keep_open: bool = False) -> None:
        self.path = path
        self.plain_reply = plain_reply
        self.keep_open = keep_open
        self.received: list[bytes] = []
        self.connections = 0
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            first = True
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                self.received.append(data)
                if first and not is_upgrade(data):
                    conn.sendall(self.plain_reply)
                    if not self.keep_open:
                        return
                else:
                    conn.sendall(REPLY if first else data)
                first = False

    def close(self) -> None:
        self._server.close()


@pytest.fixture(autouse=True)
def socket_dirs(monkeypatch):
    """Every socket directory a test makes is removed when it ends (they live outside tmp_path: a socket
    path has to be short)."""
    made: list[Path] = []
    real = ttyd_front.make_socket_dir

    def tracked() -> Path:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(ttyd_front, "make_socket_dir", tracked)
    yield
    for directory in made:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def rig():
    """A front on a free port in front of a fake ttyd; only `localhost` and `mac.example` are answered to."""
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME)
    logged: list[str] = []
    front = TtydFront(
        0,
        upstream.path,
        lambda host: host is not None and host.split(":")[0] in ("localhost", "mac.example"),
        log=logged.append,
    )
    front.start()
    yield front, upstream, logged
    front.close()
    upstream.close()


def dial(front: TtydFront) -> socket.socket:
    conn = socket.create_connection(("127.0.0.1", front.port), timeout=5)
    conn.settimeout(5)
    return conn


def read_all(conn: socket.socket) -> bytes:
    data = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk


def test_an_allowed_host_reaches_ttyd_and_bytes_flow_both_ways(rig):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(HEAD)
        assert conn.recv(65536) == REPLY
        conn.sendall(b"keys typed in a pane")
        assert conn.recv(65536) == b"keys typed in a pane"
    assert b"".join(upstream.received) == HEAD + b"keys typed in a pane", "ttyd is sent the head unchanged"


def test_what_arrives_with_the_head_is_not_lost(rig):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(HEAD + b"first frame")
        assert conn.recv(65536) == REPLY
    assert b"".join(upstream.received).endswith(b"\r\n\r\nfirst frame")


def test_a_large_paste_and_its_echo_do_not_wait_on_each_other(rig):
    front, upstream, _ = rig
    payload = b"x" * (4 * 1024 * 1024)
    with dial(front) as conn:
        conn.sendall(HEAD)
        assert conn.recv(65536) == REPLY
        conn.settimeout(20)
        sender = threading.Thread(target=conn.sendall, args=(payload,), daemon=True)
        sender.start()  # the fake ttyd echoes every byte back while more is still being sent
        got = 0
        while got < len(payload):
            chunk = conn.recv(1 << 20)
            assert chunk, "the relay stopped before the echo was whole"
            got += len(chunk)
        sender.join(20)
    assert got == len(payload) and not sender.is_alive()


def test_the_rebinding_shape_is_refused_before_ttyd_hears_of_it(rig):
    front, upstream, logged = rig
    with dial(front) as conn:
        # What a rebound page sends: Host and Origin are both the attacker's name, so ttyd's -O would pass.
        conn.sendall(
            b"GET /ws HTTP/1.1\r\nHost: evil.example:7681\r\nOrigin: http://evil.example:7681\r\nUpgrade: websocket\r\n\r\n"
        )
        answer = read_all(conn)
    assert answer.startswith(b"HTTP/1.1 403 ") and b"host not allowed" in answer
    assert upstream.connections == 0 and front.refused == 1
    assert "evil.example:7681" in logged[0]


def test_a_configured_remote_name_is_answered(rig):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(b"GET / HTTP/1.1\r\nHost: mac.example\r\n\r\n")
        assert read_all(conn).startswith(b"HTTP/1.1 200 OK\r\n")
    assert upstream.connections == 1


def split_answer(answer: bytes) -> tuple[list[bytes], bytes]:
    head, _, body = answer.partition(b"\r\n\r\n")
    return head.split(b"\r\n"), body


def test_the_page_is_stamped_with_the_frame_policy_and_its_connection_ends(rig):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(PAGE_HEAD)
        lines, body = split_answer(read_all(conn))
    assert lines[0] == b"HTTP/1.1 200 OK" and body == PAGE, "the body is relayed untouched"
    assert b"Content-Security-Policy: " + framing.frame_ancestors(()).encode() in lines
    assert b"Connection: close" in lines and b"connection: keep-alive" not in lines
    sent = b"".join(upstream.received).split(b"\r\n")
    assert b"Connection: close" in sent and b"Connection: keep-alive" not in sent and b"Keep-Alive: 300" not in sent
    assert sent[0] == b"GET /?arg=demo HTTP/1.1" and b"Host: localhost:7681" in sent


def test_the_operators_origins_ride_the_policy_main_passes():
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME)
    policy = framing.frame_ancestors({"https://rack.example"})
    front = TtydFront(0, upstream.path, lambda host: True, frame_policy=policy)
    front.start()
    try:
        with dial(front) as conn:
            conn.sendall(PAGE_HEAD)
            lines, _ = split_answer(read_all(conn))
        assert b"Content-Security-Policy: " + policy.encode() in lines and policy.endswith(" https://rack.example")
    finally:
        front.close()
        upstream.close()


def test_a_websocket_is_never_stamped(rig):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(HEAD)
        assert conn.recv(65536) == REPLY, "the handshake's answer passes as ttyd sent it"
    assert b"".join(upstream.received) == HEAD


@pytest.mark.parametrize(
    "reply",
    [
        b"not http at all\r\n\r\n",
        b"HTTP/1.1 101 Switching Protocols\r\n\r\n",  # a plain request never asked for it
        b"HTTP/1.1 200 OK\r\ncontent-type: text/html",  # ttyd hung up inside the head
        b"",
    ],
)
def test_an_answer_the_front_cannot_stamp_is_a_502_never_an_unstamped_page(reply):
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME, plain_reply=reply)
    front = TtydFront(0, upstream.path, lambda host: True)
    front.start()
    try:
        with dial(front) as conn:
            conn.sendall(PAGE_HEAD)
            assert read_all(conn).startswith(b"HTTP/1.1 502 ")
    finally:
        front.close()
        upstream.close()


def test_an_interim_answer_passes_and_the_final_one_is_stamped():
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME, plain_reply=b"HTTP/1.1 100 Continue\r\n\r\n" + PAGE_REPLY)
    front = TtydFront(0, upstream.path, lambda host: True)
    front.start()
    try:
        with dial(front) as conn:
            conn.sendall(PAGE_HEAD)
            answer = read_all(conn)
        assert answer.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n")
        final, body = split_answer(answer.split(b"\r\n\r\n", 1)[1])
        assert any(line.startswith(b"Content-Security-Policy: frame-ancestors ") for line in final) and body == PAGE
    finally:
        front.close()
        upstream.close()


def test_a_ttyd_that_keeps_the_connection_open_still_lets_it_go_when_the_browser_does(monkeypatch):
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME, keep_open=True)
    front = TtydFront(0, upstream.path, lambda host: True, max_connections=1)
    front.start()
    try:
        with dial(front) as conn:
            conn.sendall(PAGE_HEAD)
            got = b""
            while not got.endswith(PAGE):
                got += conn.recv(65536)
        # The browser read its Content-Length and closed: the one slot must come back.
        answer = b""
        for _ in range(50):
            with dial(front) as later:
                later.sendall(HEAD)
                answer = later.recv(65536)
            if answer == REPLY:
                break
            time.sleep(0.05)
        assert answer == REPLY
    finally:
        front.close()
        upstream.close()


def test_the_head_helpers():
    assert is_upgrade(HEAD) and is_upgrade(b"GET / HTTP/1.1\r\nupgrade: WebSocket\r\n\r\n")
    assert not is_upgrade(PAGE_HEAD) and not is_upgrade(b"GET / HTTP/1.1\r\nUpgrade: h2c\r\n\r\nUpgrade: websocket")
    assert close_request(PAGE_HEAD + b"body") == (
        b"GET /?arg=demo HTTP/1.1\r\nHost: localhost:7681\r\nConnection: close\r\n\r\nbody"
    )
    assert stamp_response(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nX: y\r\n\r\n", "frame-ancestors 'self'") == (
        b"HTTP/1.1 200 OK\r\nX: y\r\nContent-Security-Policy: frame-ancestors 'self'\r\nConnection: close\r\n\r\n"
    )
    assert status_code(b"HTTP/1.1 200 OK\r\n\r\n") == 200 and status_code(b"HTTP/1.0 404\r\n\r\n") == 404
    assert status_code(b"HTTP/2 200\r\n\r\n") is None and status_code(b"HTTP/1.1 2x0 OK\r\n\r\n") is None


@pytest.mark.parametrize(
    "head",
    [
        b"GET / HTTP/1.1\r\nHost: localhost\r\nHost: evil.example\r\n\r\n",  # which one would ttyd believe?
        b"GET / HTTP/1.1\r\n Host: localhost\r\n\r\n",  # a folded line
        b"GET /\r\nHost: localhost\r\n\r\n",  # not a request line
        b"GET / HTTP/1.1\r\nno colon here\r\n\r\n",
    ],
)
def test_a_head_that_is_not_one_clear_request_is_a_bad_request(rig, head):
    front, upstream, _ = rig
    with dial(front) as conn:
        conn.sendall(head)
        assert read_all(conn).startswith(b"HTTP/1.1 400 ")
    assert upstream.connections == 0


def test_a_request_with_no_host_is_refused_by_the_bridges_rule():
    assert main.terminal_host_allowed(None) is False
    assert main.terminal_host_allowed("localhost:7681") is True and main.terminal_host_allowed("127.0.0.1:7681") is True
    assert main.terminal_host_allowed("evil.example:7681") is False


def test_a_head_that_never_ends_is_dropped(rig, monkeypatch):
    front, upstream, _ = rig
    monkeypatch.setattr(ttyd_front, "HEAD_LIMIT", 256)
    with dial(front) as conn:
        try:
            conn.sendall(b"GET / HTTP/1.1\r\nX: " + b"a" * 100_000)
            assert read_all(conn) == b""
        except OSError:
            pass  # reset while still sending: dropped all the same
    assert upstream.connections == 0


def test_a_ttyd_that_is_not_there_is_a_502(monkeypatch):
    monkeypatch.setattr(ttyd_front, "CONNECT_WAIT_S", 0.2)
    directory = ttyd_front.make_socket_dir()
    front = TtydFront(0, directory / ttyd_front.SOCKET_NAME, lambda host: True)
    front.start()
    try:
        with dial(front) as conn:
            conn.sendall(HEAD)
            assert read_all(conn).startswith(b"HTTP/1.1 502 ")
    finally:
        front.close()


def test_connections_past_the_cap_are_told_so(monkeypatch):
    directory = ttyd_front.make_socket_dir()
    upstream = Upstream(directory / ttyd_front.SOCKET_NAME)
    front = TtydFront(0, upstream.path, lambda host: True, max_connections=1)
    front.start()
    try:
        with dial(front) as held:
            held.sendall(HEAD)
            assert held.recv(65536) == REPLY
            with dial(front) as extra:
                extra.sendall(HEAD)
                assert read_all(extra).startswith(b"HTTP/1.1 503 ")
        answer = b""
        for _ in range(50):  # the slot comes back once the first connection's relay has wound down
            with dial(front) as later:
                later.sendall(HEAD)
                answer = later.recv(65536)
            if answer == REPLY:
                break
            time.sleep(0.05)
        assert answer == REPLY
    finally:
        front.close()
        upstream.close()


def test_the_port_is_refused_when_taken():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        with pytest.raises(OSError):
            TtydFront(taken.getsockname()[1], Path("/nowhere.sock"), lambda host: True).start()
    finally:
        taken.close()


def test_host_header_reads_one_host_case_insensitively():
    assert host_header(b"GET / HTTP/1.1\r\nhOsT:  localhost:7681 \r\nX: y\r\n\r\nbody Host: no") == (True, "localhost:7681")
    assert host_header(b"GET / HTTP/1.0\r\n\r\n") == (True, None)


def test_read_head_returns_everything_received_or_none():
    ours, theirs = socket.socketpair()
    with ours, theirs:
        theirs.sendall(b"GET / HTTP/1.1\r\nHost: a\r\n")
        theirs.sendall(b"\r\nrest")
        assert read_head(ours) == b"GET / HTTP/1.1\r\nHost: a\r\n\r\nrest"
    ours, theirs = socket.socketpair()
    with ours:
        theirs.sendall(b"GET / HTTP/1.1\r\n")
        theirs.close()
        assert read_head(ours) is None


def test_the_socket_lives_in_a_directory_only_this_account_can_enter():
    directory = ttyd_front.make_socket_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert len(str(directory / ttyd_front.SOCKET_NAME).encode()) <= ttyd_front.SOCKET_PATH_MAX


def test_ttyd_listens_on_the_socket_and_on_the_port_only_without_the_status_listener():
    assert main.ttyd_listen_args(7681, Path("/run/x/ttyd.sock")) == ["-i", "/run/x/ttyd.sock"]
    assert main.ttyd_listen_args(7681, None) == ["-p", "7681", "-i", "127.0.0.1"]


def test_an_orphan_is_a_ttyd_on_one_of_our_sockets_whose_parent_is_init():
    ours = "/var/tmp/tldrack-ttyd-abc123/ttyd.sock"
    ps = "\n".join(
        [
            f"  501     1 ttyd -i {ours} -O -t fontSize=18 sh -c x",  # its bridge was killed
            f"  502   400 ttyd -i {ours} -O sh -c x",  # its bridge is alive
            "  503     1 ttyd -p 7681 -i 127.0.0.1 -O sh -c x",  # a ttyd on a port: not ours to judge
            "  504     1 ttyd -i /var/tmp/other-tool/ttyd.sock sh",  # someone else's socket
            f"  505     1 /bin/sh -c ttyd -i {ours}",  # not a ttyd
            "garbage",
        ]
    )
    assert ttyd_front.orphan_ttyds(ps) == [(501, Path("/var/tmp/tldrack-ttyd-abc123"))]
    assert ttyd_front.orphan_ttyds("") == []
