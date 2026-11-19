"""Answers require the per-launch secret (decision_secret.py), over real HTTP, and no agent door answers."""

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from tldrack.decision_secret import DecisionSecret, answer_refusal, secret_refusal
from tldrack.icons import SessionIconStore
from tldrack.lead.cli import lead as lead_cli
from tldrack.lead.service import LEAD_VERBS
from tldrack.main import ShutdownControl, StatusStore, build_status_handler, origin_allowed
from tldrack.models import SessionModelStore
from tldrack.rack_db import RackDb

SECRET = "s" * 43
BROWSER = {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "cross-site"}


@pytest.fixture
def server(tmp_path):
    handler = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        rack_db=RackDb(tmp_path / "rack.sqlite"),
        decision_secret=DecisionSecret(SECRET),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], handler.inbox
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        method,
        path,
        body=json.dumps(body) if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    return response.status, payload


def test_the_secret_goes_to_a_browser_fetch_only(server):
    port, _ = server
    assert call(port, "GET", "/decision-secret")[0] == 403, "curl: no Origin"
    assert call(port, "GET", "/decision-secret", headers={"Origin": "http://localhost:5173"})[0] == 403, (
        "an Origin without Sec-Fetch-Site is not a browser fetch"
    )
    assert call(port, "GET", "/decision-secret", headers={**BROWSER, "Origin": "https://evil.example"})[0] == 403
    status, body = call(port, "GET", "/decision-secret", headers=BROWSER)
    assert status == 200 and body["secret"] == SECRET


def test_every_operator_verb_needs_the_secret_and_a_browser_origin(server):
    port, inbox = server
    key = inbox.ask(source="lead", title="Q", detail="", blocking=True)["key"]
    answer = {"key": key, "kind": "text", "answer": "yes"}
    assert call(port, "POST", "/inbox/answer", answer, BROWSER)[0] == 403, "no secret"
    assert call(port, "POST", "/inbox/answer", answer, {**BROWSER, "X-VC-Decision-Secret": "wrong"})[0] == 403
    assert call(port, "POST", "/inbox/answer", answer, {"X-VC-Decision-Secret": SECRET})[0] == 403, (
        "the secret alone, from a shell with no Origin, is refused"
    )
    assert inbox.get(key)["state"] == "open"
    status, body = call(port, "POST", "/inbox/answer", answer, {**BROWSER, "X-VC-Decision-Secret": SECRET})
    assert status == 200 and body["ok"] and body["decision"]["state"] == "answered"
    for verb, extra in (("defer", {"until": 2_000_000_000}), ("dismiss", {"reason": "x"})):
        other = inbox.ask(source="lead", title=f"Q {verb}", detail="", blocking=True)["key"]
        assert call(port, "POST", f"/inbox/{verb}", {"key": other, **extra}, BROWSER)[0] == 403
    status, listing = call(port, "GET", "/inbox")
    assert status == 200 and len(listing["decisions"]) == 2


def test_the_preflight_allows_the_secret_header_on_operator_verbs_only(server):
    port, _ = server

    def allowed(path):
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("OPTIONS", path, headers={"Origin": "http://localhost:5173"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status, response.getheader("Access-Control-Allow-Headers") or ""

    status, headers = allowed("/inbox/answer")
    assert status == 204 and "X-VC-Decision-Secret" in headers and "Content-Type" in headers
    assert "X-VC-Decision-Secret" not in allowed("/spawn")[1]


def test_no_agent_door_answers():
    assert not {"answer", "defer", "dismiss"} & set(LEAD_VERBS)
    assert not {"answer", "defer", "dismiss"} & set(lead_cli.commands)


def test_refusal_helpers_are_pure():
    secret = DecisionSecret(SECRET)
    assert secret_refusal(BROWSER, origin_allowed) is None
    assert answer_refusal({**BROWSER, "x-vc-decision-secret": SECRET}, secret, origin_allowed) is None
    assert answer_refusal({"Origin": "null", "X-VC-Decision-Secret": SECRET}, secret, origin_allowed) is not None
    assert DecisionSecret().value != DecisionSecret().value, "minted per launch"
