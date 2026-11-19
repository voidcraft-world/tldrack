"""The listener over real loopback sockets: the gate, the handshake, the allowlist, the body rules, the forward,
the watchdog and the link's lock."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import link_client
from fakes import FakeBridge
from link_client import NotTheLink
from phone_link import link_server, proof
from phone_link.link_server import HELD_ELSEWHERE, MAX_BODY_BYTES, LinkApp, LinkServer
from phone_link.state import LinkState, Listener
from phone_link.token_store import TokenStore


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def start_link(tmp_path, upstream_port, *, nonces=None, **server_options):
    logs: list[str] = []
    state = LinkState(7683)
    tokens = TokenStore(tmp_path / "state")
    server = LinkServer(
        LinkApp(tokens, state, upstream_port, nonces=nonces),
        state,
        log=logs.append,
        port=0,
        poll_s=0.02,
        lock_path=tmp_path / "state" / "link.lock",
        **server_options,
    )
    server.start()
    assert wait_for(state.listener_up)
    return SimpleNamespace(port=server.bound_port, token=tokens.value(), state=state, logs=logs, server=server)


@pytest.fixture
def bridge():
    fake = FakeBridge()
    yield fake
    fake.close()


@pytest.fixture
def link(tmp_path, bridge):
    started = start_link(tmp_path, bridge.port)
    started.bridge = bridge
    yield started
    started.server.close()


def send(link, method, target, *, auth=True, host="127.0.0.1:7683", extra=None, body=None, length=True, signed=True):
    """One request, sent as written. `auth=True` runs the handshake first and signs exactly what is sent; the
    answer must then be signed over its nonce, or (`signed=False`, a refusal made before the proof) carry no
    signature at all."""
    nonce = None
    if auth is True:
        nonce = link_client.nonce(link.port, link.token)
        auth = link_client.authorization(link.token, nonce, method, target, body or b"")
    conn = http.client.HTTPConnection("127.0.0.1", link.port, timeout=5)
    conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    if host is not None:
        conn.putheader("Host", host)
    if auth:
        conn.putheader("Authorization", auth)
    for name, value in (extra or {}).items():
        conn.putheader(name, value)
    if body is not None and length:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    data = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    if nonce is not None and signed:  # every answer to a proven request is signed over its nonce
        assert link_client.signed(link.token, nonce, response.status, data, headers), (response.status, data)
    elif nonce is not None:
        assert proof.REPLY_HEADER.lower() not in headers, (response.status, data)
    return response.status, (json.loads(data) if data else None), headers, data


# -- the gate and the handshake, over the wire --------------------------------------------------------
def test_no_credential_or_a_wrong_one_is_unlinked(link):
    unlinked = (401, {"ok": False, "error": "unlinked"})
    assert send(link, "GET", "/health", auth=None)[:2] == unlinked
    assert send(link, "GET", "/health", auth="Bearer " + link.token)[:2] == unlinked  # the token is no credential
    assert send(link, "GET", "/plugin/laya/review/recent", auth=None)[0] == 401
    assert send(link, "GET", "/no-such-path", auth=None)[0] == 401  # unlinked callers learn nothing about paths
    issued = link_client.nonce(link.port, link.token)
    assert send(link, "GET", "/health", auth=proof.authorization(issued, "0" * 64))[:2] == unlinked
    forged = link_client.authorization("F" * 43, link_client.nonce(link.port, link.token), "GET", "/health")
    assert send(link, "GET", "/health", auth=forged)[:2] == unlinked
    assert link.bridge.requests == []
    assert link.state.to_json()["requests"] == {"n": 0, "last_at": None, "refused": 6}


def test_health_is_the_linked_probe(link):
    status, body, headers, _ = send(link, "GET", "/health")
    assert (status, body) == (200, {"ok": True, "v": 2})
    assert headers["cache-control"] == "no-store"
    assert not any(name.startswith("access-control-") for name in headers)
    assert link.bridge.requests == []
    requests = link.state.to_json()["requests"]
    assert requests["n"] == 1 and requests["last_at"].endswith("Z")


def test_the_nonce_reply_is_signed_over_the_phones_challenge(link):
    challenge = "c" * 43
    status, body, headers = link_client.exchange(link.port, "GET", f"/nonce?c={challenge}")
    assert status == 200 and proof.NONCE_RE.fullmatch(json.loads(body)["nonce"])
    assert json.loads(body) | {"nonce": None} == {"ok": True, "v": 2, "nonce": None, "ttl_s": 30}
    assert link_client.signed(link.token, challenge, status, body, headers, nonce_reply=True)
    assert not link_client.signed(link.token, "d" * 43, status, body, headers, nonce_reply=True)
    # Not a signing oracle: the same signature is no proven reply over the caller's own challenge.
    assert not link_client.signed(link.token, challenge, status, body, headers)
    assert link.token not in body.decode() + json.dumps(headers)


@pytest.mark.parametrize(
    "target", ["/nonce", "/nonce?c=short", "/nonce?c=" + "c" * 43 + "&c=" + "c" * 43, "/nonce?x=" + "c" * 43]
)
def test_a_bad_challenge(link, target):
    status, body, headers = link_client.exchange(link.port, "GET", target)
    assert (status, json.loads(body)) == (400, {"ok": False, "error": "bad_challenge"})
    assert proof.REPLY_HEADER.lower() not in headers


def test_the_nonce_is_behind_the_same_gate(link):
    target = "/nonce?c=" + "c" * 43
    assert send(link, "GET", target, auth=None, extra={"Origin": "https://evil.example"})[0] == 403
    assert send(link, "GET", target, auth=None, host="rebind.evil.example")[0] == 403
    assert send(link, "GET", target, auth=None, extra={"X-Forwarded-For": "1.2.3.4"})[0] == 403
    assert send(link, "POST", target, auth=None, body=b"{}")[0] == 401  # only GET /nonce is answered unproven


def test_a_nonce_is_spent_on_first_use_even_by_a_wrong_mac(link):
    issued = link_client.nonce(link.port, link.token)
    good = link_client.authorization(link.token, issued, "GET", "/health")
    assert send(link, "GET", "/health", auth=good)[0] == 200
    assert send(link, "GET", "/health", auth=good)[0] == 401  # a captured request cannot be replayed
    burnt = link_client.nonce(link.port, link.token)
    assert send(link, "GET", "/health", auth=proof.authorization(burnt, "0" * 64))[0] == 401
    late = link_client.authorization(link.token, burnt, "GET", "/health")
    assert send(link, "GET", "/health", auth=late)[0] == 401


def test_a_mac_covers_the_method_the_target_and_the_body(link):
    def signed_for(method, target, body=b""):
        return link_client.authorization(link.token, link_client.nonce(link.port, link.token), method, target, body)

    assert send(link, "GET", "/topic-labels?session=vc-1", auth=signed_for("GET", "/health"))[0] == 401
    assert send(link, "GET", "/topic-labels?session=vc-2", auth=signed_for("GET", "/topic-labels?session=vc-1"))[0] == 401
    assert send(link, "POST", "/ledger/ingest", body=b"{}", auth=signed_for("GET", "/ledger/ingest"))[0] == 401
    assert (
        send(
            link,
            "POST",
            "/plugin/laya/review/answer",
            body=b'{"id":"b"}',
            auth=signed_for("POST", "/plugin/laya/review/answer", b'{"id":"a"}'),
        )[0]
        == 401
    )
    assert link.bridge.requests == []


def test_an_expired_nonce_is_unlinked(tmp_path, bridge):
    now = [1000.0]
    expired = start_link(tmp_path, bridge.port, nonces=proof.NonceBook(clock=lambda: now[0]))
    try:
        issued = link_client.nonce(expired.port, expired.token)
        now[0] += proof.NONCE_TTL_S + 1
        assert send(expired, "GET", "/health", auth=link_client.authorization(expired.token, issued, "GET", "/health"))[0] == 401
    finally:
        expired.server.close()


def test_refusals_before_the_proof_are_never_signed(link):
    for status, headers in [
        send(link, "GET", "/health", auth=None)[::2],
        send(link, "GET", "/health", auth=None, extra={"Origin": "null"})[::2],
    ]:
        assert status in (401, 403) and proof.REPLY_HEADER.lower() not in headers
    status, _, headers, _ = send(link, "GET", "/spawn")  # proven, then not allowed: signed
    assert status == 404 and proof.REPLY_HEADER.lower() in headers


def test_the_reference_client_round_trip(link):
    link.bridge.reply_body = b'{"ok":true,"rows":[]}'
    reply = link_client.call(link.port, link.token, "GET", "/plugin/laya/review/recent?limit=5")
    assert (reply.status, reply.json()) == (200, {"ok": True, "rows": []})
    assert link.bridge.requests[0]["path"] == "/plugin/laya/review/recent?limit=5"


# -- a stranger on the phone's 127.0.0.1:7683 -----------------------------------------------------------
class Squatter:
    """What any app on the phone can do while the tunnel is down: bind 127.0.0.1:7683, record every byte the app
    sends, and answer `/nonce` the way the link would, signed with a key it does not have (or with a signature it
    collected from the real link earlier)."""

    def __init__(self, reply: tuple[bytes, str] | None = None) -> None:
        self.received = bytearray()
        self.connections = 0
        self.served = 0
        self._reply = reply or (b'{"ok":true,"v":2,"nonce":"' + b"n" * 43 + b'","ttl_s":30}', "0" * 64)
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self.port = self._socket.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            with conn:
                self.connections += 1
                conn.settimeout(2)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    body, mac = self._reply
                    conn.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nPhone-Link-Mac: "
                        + mac.encode()
                        + b"\r\n\r\n"
                        + body
                    )
                    while chunk := conn.recv(4096):  # anything the client sends after: recorded too
                        data += chunk
                except OSError:
                    pass
                self.received += data
                self.served += 1

    def close(self) -> None:
        self._socket.close()


@pytest.mark.parametrize("collected", [False, True])
def test_a_squatter_never_sees_the_token_or_the_request(link, collected):
    reply = None
    if collected:  # the squatter replays the link's own signed answer to an earlier challenge
        status, body, headers = link_client.exchange(link.port, "GET", "/nonce?c=" + "s" * 43)
        reply = (body, headers[proof.REPLY_HEADER.lower()])
    squatter = Squatter(reply)
    note = b'{"id":"r1","verdict":"open","note":"the operator\'s words"}'
    try:
        with pytest.raises(NotTheLink):
            link_client.call(squatter.port, link.token, "POST", "/plugin/laya/review/answer", note)
        assert wait_for(lambda: squatter.served == 1)
        time.sleep(0.05)
        assert squatter.connections == 1  # it stopped after the nonce
        assert link.token.encode() not in squatter.received
        assert b"authorization" not in squatter.received.lower()
        assert b"/plugin/laya" not in squatter.received and note not in squatter.received
    finally:
        squatter.close()


# -- the allowlist, over the wire ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "target",
    [
        "/plugin/laya/review/recent/",
        "/plugin/laya/review/./recent",
        "/plugin/laya/x/../review/recent",
        "/plugin/laya/review/%72ecent",
        "/plugin/laya/review/%2e%2e/recent",
        "//plugin/laya/review/recent",
        "/plugin/laya/review/queue",
        "/spawn",
        "/kill-session?session=vc-1",
        "/snapshot",
        "http://127.0.0.1:7682/plugin/laya/review/recent",
    ],
)
def test_not_allowed_never_reaches_the_bridge(link, target):
    assert send(link, "GET", target)[:2] == (404, {"ok": False, "error": "not_allowed"})
    assert send(link, "POST", target, body=b"{}")[0] == 404
    assert link.bridge.requests == []


@pytest.mark.parametrize(
    "target",
    [
        "/plugin/laya/review/recent?limit=20",
        "/ledger/search?q=open+work",
        "/ledger/conversation?file_id=3&around=12",
        "/topic-labels?session=vc-1",
    ],
)
def test_a_get_is_replayed_with_none_of_the_phones_headers(link, target):
    link.bridge.reply_body = b'{"ok":true,"rows":[1,2,3]}'
    status, body, _, _ = send(link, "GET", target, extra={"Cookie": "session=1", "X-Phone": "yes", "User-Agent": "okhttp/4.12"})
    assert (status, body) == (200, {"ok": True, "rows": [1, 2, 3]})
    [seen] = link.bridge.requests
    assert seen["method"] == "GET" and seen["path"] == target
    assert set(seen["headers"]) <= {"host", "accept", "accept-encoding"}
    assert seen["headers"]["host"] == f"127.0.0.1:{link.bridge.port}"


@pytest.mark.parametrize("target", ["/plugin/laya/review/answer", "/plugin/laya/review/undo", "/ledger/ingest"])
def test_a_post_is_replayed_as_json_without_an_origin_or_the_credential(link, target):
    link.bridge.reply_status = 202
    link.bridge.reply_body = b'{"ok":true,"queued":true}'
    body = json.dumps({"id": "abc", "verdict": "open", "note": "ünïcode"}).encode()
    status, reply, _, _ = send(link, "POST", target, body=body, extra={"Content-Type": "text/plain", "X-Requested-With": "app"})
    assert (status, reply) == (202, {"ok": True, "queued": True})
    [seen] = link.bridge.requests
    assert seen["path"] == target
    assert seen["headers"]["content-type"] == "application/json"
    assert "origin" not in seen["headers"] and "authorization" not in seen["headers"]
    assert "x-requested-with" not in seen["headers"]
    assert json.loads(seen["body"]) == {"id": "abc", "verdict": "open", "note": "ünïcode"}


def test_the_bridges_status_and_body_pass_through_unchanged_and_signed(link):
    link.bridge.reply_status = 409
    link.bridge.reply_body = b'{"ok": false, "error": "reference_changed", "card": {"id": 7}}'
    status, body, _, raw = send(link, "POST", "/plugin/laya/review/answer", body=b'{"id":"x"}')
    assert status == 409 and raw == link.bridge.reply_body
    assert body == {"ok": False, "error": "reference_changed", "card": {"id": 7}}


def test_a_non_json_reply_is_not_passed_on(link):
    link.bridge.reply_type = "text/html"
    link.bridge.reply_body = b"<html>"
    assert send(link, "GET", "/topic-labels?session=vc-1")[:2] == (502, {"ok": False, "error": "bad_upstream"})


def test_a_bridge_that_is_down_is_a_signed_502(tmp_path):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    down = start_link(tmp_path, port)
    try:
        assert send(down, "GET", "/plugin/laya/review/recent")[:2] == (502, {"ok": False, "error": "bridge_down"})
        assert send(down, "GET", "/health")[0] == 200  # linked, with the bridge down: the app's BRIDGE DOWN
    finally:
        down.server.close()


# -- bodies -------------------------------------------------------------------------------------------
def test_the_body_cap(link):
    filler = "x" * (MAX_BODY_BYTES - len('{"pad":""}'))
    at_cap = json.dumps({"pad": filler}, separators=(",", ":")).encode()
    assert len(at_cap) == MAX_BODY_BYTES
    assert send(link, "POST", "/plugin/laya/review/answer", body=at_cap)[0] == 200
    over = json.dumps({"pad": filler + "x"}, separators=(",", ":")).encode()
    assert send(link, "POST", "/plugin/laya/review/answer", body=over, signed=False)[:2] == (
        413,
        {"ok": False, "error": "too_large"},
    )
    assert len(link.bridge.requests) == 1


@pytest.mark.parametrize(
    "body", [b"not json", b"[1,2]", b'"text"', b"{bad", b'{"x": NaN}', b"\xff\xfe", b"", b'{"a":"\\ud800"}', b'{"\\udfff":1}']
)
def test_a_post_body_must_be_a_json_object_of_real_text(link, body):
    assert send(link, "POST", "/plugin/laya/review/undo", body=body)[:2] == (400, {"ok": False, "error": "bad_body"})
    assert link.bridge.requests == []


def test_length_rules(link):
    """Framing is checked before the proof (the MAC covers the body), so these refusals are never signed."""
    assert send(link, "POST", "/ledger/ingest", signed=False)[:2] == (411, {"ok": False, "error": "length_required"})
    assert send(link, "GET", "/health", body=b"{}", signed=False)[0] == 400
    assert (
        send(link, "POST", "/ledger/ingest", body=b"{}", length=False, signed=False, extra={"Transfer-Encoding": "chunked"})[0]
        == 400
    )
    for bad in ("-2", "\u00b2", "1e3", "99999999999", "2, 2"):
        assert (
            send(link, "POST", "/ledger/ingest", body=b"{}", length=False, signed=False, extra={"Content-Length": bad})[0] == 400
        ), bad
    assert link.bridge.requests == []


def test_methods_other_than_get_and_post(link):
    status, body, _, _ = send(link, "PUT", "/plugin/laya/review/answer", body=b"{}")
    assert (status, body) == (405, {"ok": False, "error": "method_not_allowed"})
    assert send(link, "OPTIONS", "/health", auth=None)[0] == 401
    status, body, _, _ = send(link, "BREW", "/health", auth=None)
    assert status == 501 and body == {"ok": False, "error": "bad_request"}


def test_an_origin_is_refused_even_with_the_proof(link):
    for origin in ("https://voidcraft.world", "null", "http://127.0.0.1:7683"):
        status, body, _, _ = send(
            link,
            "GET",
            "/health",
            auth=link_client.authorization(link.token, link_client.nonce(link.port, link.token), "GET", "/health"),
            extra={"Origin": origin},
        )
        assert (status, body) == (403, {"ok": False, "error": "browser_refused"})


def test_a_rebinding_host_is_refused(link):
    assert send(link, "GET", "/health", auth=None, host="rebind.evil.example:7683")[:2] == (
        403,
        {"ok": False, "error": "host_refused"},
    )
    assert send(link, "GET", "/health", host=None)[0] == 200  # an HTTP/1.0 client may send none


# -- the watchdog: a deadline per connection, not per read ----------------------------------------------
def _trickle(port, until_s, interval_s=0.15):
    """Open a connection and send one header byte every `interval_s` (well inside the per-read timeout). Returns
    how long the server let it live."""
    started = time.monotonic()
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(0.05)
    try:
        sock.sendall(b"GET /health HTTP/1.0\r\n")
        while time.monotonic() - started < until_s:
            try:
                sock.sendall(b"X")
            except OSError:
                break
            try:
                if sock.recv(4096) == b"":
                    break
            except TimeoutError:
                pass
            except OSError:
                break
            time.sleep(interval_s)
    finally:
        sock.close()
    return time.monotonic() - started


def test_a_trickling_caller_is_cut_at_the_pre_proof_deadline(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(link_server, "PRE_PROOF_S", 0.5)
    trickled = start_link(tmp_path, bridge.port)
    try:
        assert _trickle(trickled.port, until_s=6.0) < 2.0
        assert not any("a request failed" in line for line in trickled.logs)  # a cut is the rule, not a failure
    finally:
        trickled.server.close()


def test_a_trickler_holds_the_only_slot_for_its_deadline_and_no_longer(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(link_server, "PRE_PROOF_S", 0.5)
    monkeypatch.setattr(link_server, "MAX_CONNECTIONS", 1)
    capped = start_link(tmp_path, bridge.port)
    holder = threading.Thread(target=_trickle, args=(capped.port, 6.0), daemon=True)
    holder.start()
    try:
        time.sleep(0.1)
        assert _status(capped) == 503
        assert wait_for(lambda: _status(capped) == 200, timeout=2.0)  # cut at 0.5 s; it trickles for 6
    finally:
        capped.server.close()


def test_the_linger_has_a_deadline_too(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(link_server, "MAX_CONNECTIONS", 1)
    capped = start_link(tmp_path, bridge.port)
    sock = socket.create_connection(("127.0.0.1", capped.port))
    try:  # answered at once (401, no proof), then the caller keeps trickling into the lingering close
        sock.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        started = time.monotonic()
        stop = threading.Event()

        def drip():
            while not stop.is_set():
                try:
                    sock.sendall(b"X")
                except OSError:
                    return
                time.sleep(0.1)

        threading.Thread(target=drip, daemon=True).start()
        assert wait_for(lambda: _status(capped) == 200, timeout=3.0)
        assert time.monotonic() - started < link_server.LINGER_S + 1.5
        stop.set()
    finally:
        sock.close()
        capped.server.close()


def test_a_proven_request_gets_the_full_deadline(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(link_server, "PRE_PROOF_S", 0.3)
    slow = start_link(tmp_path, bridge.port)
    bridge.reply_delay = 0.8  # past the pre-proof budget, inside the connection's
    try:
        assert send(slow, "GET", "/plugin/laya/review/recent")[0] == 200
    finally:
        slow.server.close()


def test_connections_past_the_cap_get_a_503_without_a_thread(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(link_server, "MAX_CONNECTIONS", 1)
    capped = start_link(tmp_path, bridge.port)
    holder = socket.create_connection(("127.0.0.1", capped.port))
    try:
        time.sleep(0.1)  # the first connection holds the only slot, sending nothing
        assert send(capped, "GET", "/health", auth=None)[:2] == (503, {"ok": False, "error": "busy"})
    finally:
        holder.close()
    assert wait_for(lambda: _status(capped) == 200)
    capped.server.close()


def _status(link):
    """Whether the listener would take a connection now: 200 free, 503 every slot held."""
    try:
        return link_client.exchange(link.port, "GET", "/nonce?c=" + "c" * 43, timeout=1.0)[0]
    except (OSError, http.client.HTTPException):
        return None


# -- the listener's lifecycle -------------------------------------------------------------------------
def test_a_port_in_use_is_a_state_then_a_retry(tmp_path, bridge):
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen()
    port = squatter.getsockname()[1]
    logs: list[str] = []
    state = LinkState(7683)
    server = LinkServer(
        LinkApp(TokenStore(tmp_path), state, bridge.port),
        state,
        log=logs.append,
        port=port,
        retry_s=(0.05,),
        poll_s=0.02,
        lock_path=tmp_path / "link.lock",
    )
    server.start()
    try:
        assert wait_for(lambda: state.listener_error() is not None)
        listener = state.listener()
        assert not listener.up and listener.owner and "in use" in listener.error  # ours to serve, a stranger's port
        assert len([line for line in logs if "cannot listen" in line]) == 1  # logged once, not per retry
        squatter.close()
        assert wait_for(state.listener_up)
        assert server.bound_port == port
    finally:
        squatter.close()
        server.close()
    assert wait_for(lambda: not state.listener_up() and not state.listener().owner)


def test_one_link_per_mac_the_second_bridge_waits_for_the_lock(tmp_path, bridge):
    lock = tmp_path / "state" / "link.lock"
    first = start_link(tmp_path, bridge.port)
    state = LinkState(7683)
    logs: list[str] = []
    second = LinkServer(
        LinkApp(TokenStore(tmp_path / "state"), state, bridge.port),
        state,
        log=logs.append,
        port=0,
        retry_s=(0.05,),
        poll_s=0.02,
        lock_path=lock,
    )
    second.start()
    try:
        assert wait_for(lambda: state.listener_error() == HELD_ELSEWHERE)
        assert state.listener() == Listener(up=False, error=HELD_ELSEWHERE, owner=False)
        assert second.bound_port is None  # it never even tried the port
        assert first.state.listener().owner
        first.server.close()  # the owner exits: the kernel drops its lock, the second takes over
        assert wait_for(state.listener_up)
        assert state.listener().owner
    finally:
        first.server.close()
        second.close()


def test_close_is_bounded_and_idempotent(link):
    started = time.monotonic()
    link.server.close()
    link.server.close()
    assert time.monotonic() - started < 3


def test_the_token_is_never_logged(link):
    send(link, "GET", "/health")
    send(link, "GET", "/health", auth=None)
    assert link.logs and not any(link.token in line for line in link.logs)


# -- the tailnet door -----------------------------------------------------------------------------------
def test_health_names_the_tailnet_door_only_when_it_is_on_and_only_to_a_proven_caller(tmp_path):
    from phone_link.config import TailnetDoor

    door = TailnetDoor("mymac.tail1234.ts.net", 7683, "operator@example.com")
    tokens = TokenStore(tmp_path / "state")
    token = tokens.value()
    served = {
        "host": [door.authority],
        "x-forwarded-for": ["100.64.0.7"],
        "x-forwarded-host": [door.authority],
        "x-forwarded-proto": ["https"],
        "tailscale-user-login": ["operator@example.com"],
    }

    def health(app, headers):
        """The whole handshake, socket-free, through the app's one policy function."""
        issued = app.respond("GET", "/nonce?c=" + "c" * 43, dict(headers), lambda n: b"")
        if issued.status != 200:
            return issued.status, json.loads(issued.body)
        nonce = json.loads(issued.body)["nonce"]
        auth = link_client.authorization(token, nonce, "GET", "/health")
        answer = app.respond("GET", "/health", {**headers, "authorization": [auth]}, lambda n: b"")
        assert link_client.signed(token, nonce, answer.status, answer.body, {"phone-link-mac": answer.mac})
        return answer.status, json.loads(answer.body)

    local = {"host": ["127.0.0.1:7683"]}
    off = LinkApp(tokens, LinkState(7683), 1)
    assert health(off, local) == (200, {"ok": True, "v": 2})
    assert health(off, served) == (403, {"ok": False, "error": "host_refused"})

    on = LinkApp(tokens, LinkState(7683), 1, tailnet=door)
    # Over the cable or over the tailnet, a proven caller is told the door's address.
    told = {"ok": True, "v": 2, "remote": "https://mymac.tail1234.ts.net:7683/"}
    assert health(on, local) == (200, told)
    assert health(on, served) == (200, told)
    # The door admits a caller to the handshake, never past it: without the token it is as unlinked as anyone.
    unproven = on.respond("GET", "/health", dict(served), lambda n: b"")
    assert (unproven.status, json.loads(unproven.body), unproven.mac) == (401, {"ok": False, "error": "unlinked"}, None)
    stranger = {**served, "tailscale-user-login": ["stranger@example.com"]}
    assert health(on, stranger) == (403, {"ok": False, "error": "tailnet_refused"})
