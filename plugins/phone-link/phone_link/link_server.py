"""The link's own listener on 127.0.0.1:7683, the only port a phone is ever tunnelled to.

`LinkApp.respond` is the whole policy, socket-free: the gate (gate.py), the handshake (proof.py), the allowlist
(allowlist.py), the body, then one forward to the bridge (upstream.py). `LinkServer` only claims the link, binds,
serves and stops. Why the bridge's own port is never tunnelled instead, and the handshake: owned by
plugins/phone-link/README.md — see the note there.

Rules that keep a plugin from taking the bridge down, from this side:

- **Bounded threads.** At most `MAX_CONNECTIONS` handlers at once; past that a connection gets a 503 and is
  closed without a thread. Any app on the phone can open sockets to its own loopback port, and an unbounded
  `ThreadingHTTPServer` would turn that into one bridge thread each.
- **Bounded time, by the clock.** A socket timeout bounds each read, not a connection: one byte every few seconds
  would hold a slot forever. So each connection has a watchdog that shuts the socket at a wall-clock deadline:
  `PRE_PROOF_S` to send the request and prove the token, `CONNECTION_DEADLINE_S` in all (the forward's own
  timeout, the write and the linger fit inside it).
- **Bounded bytes.** A body is at most `MAX_BODY_BYTES` and must declare its length; no chunked bodies.
- **One link per Mac.** The listener first takes an exclusive `flock` on the state folder's `link.lock`, and holds
  it for its life; the kernel drops it when the process exits. A second bridge reading the same plugins.json (a
  spare `--port`, or a stray launch while the real one runs) then knows the link is someone else's, and its
  keeper leaves every phone's 7683 tunnel alone instead of removing the one the owner armed (keeper.py).
- **A busy port is a state, not a crash.** A held lock or a failed bind is written to the state and retried with
  backoff, so a listener left over by a bridge that is still exiting is simply waited out.
- **HTTP/1.0, one request per connection.** No keep-alive, so no second request can ride behind the first.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from phone_link import allowlist, gate, proof, upstream
from phone_link.config import TailnetDoor
from phone_link.logs import guarded
from phone_link.paths import PHONE_LINK_PORT
from phone_link.state import LinkState
from phone_link.token_store import TokenStore

HOST = "127.0.0.1"
MAX_BODY_BYTES = 16 * 1024
MAX_CONNECTIONS = 16
#: Per read. The watchdog below is what bounds a whole connection.
SOCKET_TIMEOUT_S = 5.0
#: From accept to a proven request: the request line, the headers and the body. The app sends all of it at once.
PRE_PROOF_S = 5.0
#: From accept to close, for a caller that proved the token: the forward's 10 s timeout fits inside.
CONNECTION_DEADLINE_S = 20.0
#: The lingering close: after the answer, read off what the client still sends (a body refused unread), for
#: at most this long in all and this much, so it reads its answer instead of a connection reset.
LINGER_S = 0.5
DRAIN_LIMIT_BYTES = 64 * 1024
BIND_RETRY_S = (2.0, 5.0, 10.0, 30.0, 60.0)
HEALTH_BODY = {"ok": True, "v": proof.VERSION}
#: `listener.error` while another bridge on this Mac holds the link.
HELD_ELSEWHERE = "another bridge holds the link"
#: ASCII digits only: `str.isdigit` also accepts `²`, which `int` then refuses.
_LENGTH = re.compile(r"[0-9]{1,9}")

Forward = Callable[..., upstream.Reply]


def json_bytes(payload: Mapping) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def error_body(error: str) -> bytes:
    return json_bytes({"ok": False, "error": error})


def _nothing() -> None:
    return None


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes
    #: `Phone-Link-Mac`: on every answer to a caller that proved the token, and on `/nonce`. Never on a refusal
    #: made before the proof, so an unproven caller cannot collect the link's signatures.
    mac: str | None = None


class _Refused(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


def _refuse_constant(value: str) -> float:
    raise ValueError(f"{value} is not JSON")


def declared_length(headers: Mapping[str, list[str]]) -> int | None:
    """The one Content-Length, or None when absent. Raises `_Refused` on a malformed or repeated one."""
    values = headers.get("content-length", [])
    if not values:
        return None
    if len(values) > 1 or not _LENGTH.fullmatch(values[0].strip()):
        raise _Refused(400, "bad_body")
    return int(values[0].strip())


def read_framed(method: str, headers: Mapping[str, list[str]], read_body: Callable[[int], bytes]) -> bytes:
    """The exact body bytes. A GET has none; anything else declares a length ≤ the cap. No chunked bodies."""
    if headers.get("transfer-encoding"):
        raise _Refused(400, "bad_body")
    length = declared_length(headers)
    if method == "GET":
        if length:
            raise _Refused(400, "bad_body")
        return b""
    if length is None:
        raise _Refused(411, "length_required")
    if length > MAX_BODY_BYTES:
        raise _Refused(413, "too_large")
    raw = read_body(length) if length else b""
    if len(raw) != length:
        raise _Refused(400, "bad_body")
    return raw


def json_object(raw: bytes) -> bytes:
    """A POST body: a JSON object of real text, re-serialised. A lone surrogate (`"\\ud800"`) parses, but it is not
    text and cannot be encoded, so it is refused here rather than handed to the bridge."""
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):  # UnicodeDecodeError and UnicodeEncodeError are ValueErrors
        raise _Refused(400, "bad_body") from None


class LinkApp:
    """The listener's policy, from request line and headers to an `Answer`."""

    def __init__(
        self,
        tokens: TokenStore,
        state: LinkState,
        upstream_port: int,
        forward: Forward = upstream.forward,
        nonces: proof.NonceBook | None = None,
        tailnet: TailnetDoor | None = None,
    ) -> None:
        self._tokens = tokens
        self._state = state
        self._upstream_port = upstream_port
        self._forward = forward
        #: The tailnet door, or None: off (gate.py owns what it admits).
        self._tailnet = tailnet
        self._nonces = nonces if nonces is not None else proof.NonceBook()  # an empty book is falsy

    def respond(
        self,
        method: str,
        target: str,
        headers: Mapping[str, list[str]],
        read_body: Callable[[int], bytes],
        *,
        proven: Callable[[], None] = _nothing,
    ) -> Answer:
        """`proven` is called once the caller has proved the token (the watchdog then grants the full deadline)."""
        refused = gate.refusal(headers, self._tailnet)
        if refused is not None:
            return self._refuse(refused.status, refused.error)
        parsed = allowlist.parse_target(target)
        if method == "GET" and parsed is not None and parsed[0] == allowlist.NONCE:
            return self._issue(parsed[1], headers, read_body)
        held = gate.credential(headers)
        if held is None or not self._nonces.take(held.nonce):
            return self._refuse(gate.UNLINKED.status, gate.UNLINKED.error)
        try:
            raw = read_framed(method, headers, read_body)
        except _Refused as refusal:
            return self._refuse(refusal.status, refusal.error)
        if not self._tokens.verify(proof.request_message(held.nonce, method, target, raw), held.mac):
            return self._refuse(gate.UNLINKED.status, gate.UNLINKED.error)
        proven()
        self._state.note_request()
        status, body = self._answer(method, target, raw)
        return Answer(status, body, self._tokens.sign(proof.reply_message(held.nonce, status, body)))

    def _refuse(self, status: int, error: str) -> Answer:
        self._state.note_refused()
        return Answer(status, error_body(error))

    def _issue(self, query: str, headers: Mapping[str, list[str]], read_body: Callable[[int], bytes]) -> Answer:
        """`GET /nonce?c=<challenge>`: a fresh nonce, the reply signed over the phone's challenge."""
        challenge = proof.challenge_of(query)
        try:
            read_framed("GET", headers, read_body)
        except _Refused as refusal:
            return self._refuse(refusal.status, refusal.error)
        if challenge is None:
            return self._refuse(400, "bad_challenge")
        if self._tokens.value() is None:  # no token on this Mac: nobody can be linked
            return self._refuse(gate.UNLINKED.status, gate.UNLINKED.error)
        body = json_bytes({"ok": True, "v": proof.VERSION, "nonce": self._nonces.issue(), "ttl_s": int(proof.NONCE_TTL_S)})
        return Answer(200, body, self._tokens.sign(proof.nonce_message(challenge, 200, body)))

    def _answer(self, method: str, target: str, raw: bytes) -> tuple[int, bytes]:
        """A proven request: the method, the allowlist, the body, then the forward."""
        if method not in ("GET", "POST"):
            return 405, error_body("method_not_allowed")
        route = allowlist.route_for(method, target)
        if route is None:
            return 404, error_body("not_allowed")
        if route.kind == "health":
            # With the tailnet door on, the signed health reply is how the app learns its address: a caller that
            # proved the token, and a reply only this Mac can sign.
            health: dict[str, object] = dict(HEALTH_BODY)
            if self._tailnet is not None:
                health["remote"] = self._tailnet.url
            return 200, json_bytes(health)
        body = None
        if method == "POST":
            try:
                body = json_object(raw)
            except _Refused as refusal:
                return refusal.status, error_body(refusal.error)
        try:
            reply = self._forward(self._upstream_port, method, route.target, body)
        except upstream.BridgeDown:
            return 502, error_body("bridge_down")
        except upstream.BadReply:
            return 502, error_body("bad_upstream")
        return reply.status, reply.body


class _Watchdog:
    """Shuts one connection's socket at its wall-clock deadline. `proven` moves the deadline from `pre_s` after
    the accept to `total_s` after it; `cancel` disarms it before the socket is closed."""

    def __init__(self, sock: socket.socket, *, pre_s: float, total_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._sock = sock
        self._clock = clock
        self._end = clock() + total_s
        self._lock = threading.Lock()
        self._generation = 0
        self._timer: threading.Timer | None = None
        self.fired = False
        with self._lock:
            self._arm_locked(pre_s)

    def _arm_locked(self, seconds: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._generation += 1
        timer = threading.Timer(max(0.0, seconds), self._cut, args=(self._generation,))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def proven(self) -> None:
        with self._lock:
            if self._timer is not None and not self.fired:
                self._arm_locked(self._end - self._clock())

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._generation += 1

    def _cut(self, generation: int) -> None:
        # Under the lock, so `cancel` (and with it the socket's close) waits for a shutdown already under way.
        with self._lock:
            if generation != self._generation:
                return
            self.fired = True
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "phone-link"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_S

    def __init__(self, request, client_address, server, watch: _Watchdog | None = None) -> None:
        self.watch = watch
        super().__init__(request, client_address, server)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's name
        return  # silent, like the bridge: nothing about a request is worth a log line

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's dispatch names
        self._serve()

    do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The stdlib's own refusals (bad request line, oversized headers, unknown method) as JSON."""
        self.close_connection = True
        try:
            self._write(Answer(code, error_body("bad_request")))
        except OSError:
            pass

    def _serve(self) -> None:
        headers: dict[str, list[str]] = {}
        for name, value in self.headers.items():
            headers.setdefault(name.lower(), []).append(value)
        proven = self.watch.proven if self.watch is not None else _nothing
        answer = self.server.app.respond(
            self.command,
            self._raw_target(),
            headers,  # type: ignore[attr-defined]
            self.rfile.read,
            proven=proven,
        )
        self._write(answer)
        self._linger()

    def _raw_target(self) -> str:
        """The target exactly as the request line sent it, which is also what the phone signed. `self.path` is not:
        http.server collapses a leading `//` into `/` (its open-redirect guard), which would turn a refused
        `//plugin/…` into an allowed one."""
        words = self.requestline.split()
        return words[1] if len(words) >= 2 else ""

    def _linger(self) -> None:
        deadline = time.monotonic() + LINGER_S
        try:
            self.connection.shutdown(socket.SHUT_WR)
            budget = DRAIN_LIMIT_BYTES
            while budget > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(budget, 16384))
                if not chunk:
                    return
                budget -= len(chunk)
        except OSError:
            pass

    def _write(self, answer: Answer) -> None:
        self.send_response(answer.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(answer.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if answer.mac is not None:
            self.send_header(proof.REPLY_HEADER, answer.mac)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(answer.body)


_BUSY_BODY = error_body("busy")
_BUSY = (
    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\n"
    b"Content-Length: " + str(len(_BUSY_BODY)).encode("ascii") + b"\r\n\r\n" + _BUSY_BODY
)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LinkApp, log: Callable[[str], None]) -> None:
        self.app = app
        self._log = log
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1.0)
                request.sendall(_BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        """ThreadingMixIn's own, with the watchdog armed for the connection's whole life."""
        watch = None
        try:  # the watchdog too: a thread that cannot start must still give its slot back
            watch = _Watchdog(request, pre_s=PRE_PROOF_S, total_s=CONNECTION_DEADLINE_S)
            self.RequestHandlerClass(request, client_address, self, watch)
        except Exception:
            if watch is None or not watch.fired:  # a connection cut at its deadline is the rule working
                self.handle_error(request, client_address)
        finally:
            if watch is not None:
                watch.cancel()
            self.shutdown_request(request)
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        kind = sys.exc_info()[0]
        self._log(f"phone-link: a request failed ({kind.__name__ if kind else 'unknown'})")


class LinkServer:
    """Claim the link, bind 127.0.0.1:<port> on a daemon thread, retrying while either is taken; serve until `close`."""

    def __init__(
        self,
        app: LinkApp,
        state: LinkState,
        *,
        log: Callable[[str], None],
        lock_path: Path,
        port: int = PHONE_LINK_PORT,
        retry_s: tuple[float, ...] = BIND_RETRY_S,
        poll_s: float = 0.5,
    ) -> None:
        self._app = app
        self._poll_s = poll_s
        self._state = state
        self._log = guarded(log)
        self._lock_path = Path(lock_path)
        self._port = port
        self._retry_s = retry_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._held: int | None = None
        self.bound_port: int | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="phone-link-listener", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._serve_when_free()
        finally:
            self._release()
            self._state.set_listener(False, None, owner=False)

    def _serve_when_free(self) -> None:
        attempt = 0
        last_error = None
        while not self._stop.is_set():
            server = None
            error = self._claim()
            if error is None:
                try:
                    server = _Server((HOST, self._port), self._app, self._log)
                except OSError as exc:
                    error = (
                        f"port {self._port} is in use"
                        if exc.errno == errno.EADDRINUSE
                        else f"{type(exc).__name__}: {exc.strerror or exc}"
                    )
            if server is None:
                self._state.set_listener(False, error, owner=self._held is not None)
                if error != last_error:
                    self._log(f"phone-link: cannot listen on {HOST}:{self._port} — {error}; retrying")
                    last_error = error
                self._stop.wait(self._retry_s[min(attempt, len(self._retry_s) - 1)])
                attempt += 1
                continue
            with self._lock:
                if self._stop.is_set():
                    server.server_close()
                    return
                self._server = server
            self.bound_port = server.server_address[1]
            self._state.set_listener(True, None, owner=True)
            self._log(f"phone-link: listening on {HOST}:{self.bound_port} (signed handshake, allow-listed)")
            try:
                server.serve_forever(poll_interval=self._poll_s)
            finally:
                server.server_close()
            return

    def _claim(self) -> str | None:
        """Take the link's lock, once. None while held; otherwise why it is not."""
        if self._held is not None:
            return None
        try:
            self._lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # os.open's descriptors are not inherited by the bridge's children, so no spawned process keeps the lock.
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            return f"cannot open {self._lock_path.name}: {type(exc).__name__}"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return HELD_ELSEWHERE
        except OSError as exc:
            os.close(fd)
            return f"cannot lock {self._lock_path.name}: {type(exc).__name__}"
        self._held = fd
        return None

    def _release(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            try:
                os.close(held)  # closing the descriptor drops the flock
            except OSError:
                pass

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._stop.set()
            server = self._server
        if server is not None:
            stopper = threading.Thread(target=server.shutdown, name="phone-link-stop", daemon=True)
            stopper.start()
            stopper.join(timeout)  # bounded: a stuck loop must never hold the bridge's exit
            server.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
