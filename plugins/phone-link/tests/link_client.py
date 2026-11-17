"""The phone's half of the handshake, in Python: the executable twin of the Android client, driven by the tests.

The contract: owned by plugins/phone-link/README.md § The handshake — see the note there. Like the app, it
holds the token only as an HMAC key: nothing it sends is the token, and it stops before sending a request to
anything that cannot sign its challenge.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
from dataclasses import dataclass

from phone_link import proof


class NotTheLink(Exception):
    """What answered did not prove it holds the token. The app stops here and shows UNLINKED."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes

    def json(self):
        return json.loads(self.body)


def sign(token: str, message: bytes) -> str:
    return hmac.new(token.encode("ascii"), message, hashlib.sha256).hexdigest()


def signed(token: str, binding: str, status: int, body: bytes, headers: dict[str, str], *, nonce_reply: bool = False) -> bool:
    """The link's signature over a reply: over our challenge for a `/nonce` reply, over the nonce for any other."""
    presented = headers.get(proof.REPLY_HEADER.lower(), "")
    message = proof.nonce_message if nonce_reply else proof.reply_message
    expected = sign(token, message(binding, status, body))
    return hmac.compare_digest(presented.encode("ascii", "replace"), expected.encode("ascii"))


def exchange(
    port: int, method: str, target: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 5.0
) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read(), {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()


def nonce(port: int, token: str, *, challenge: str | None = None) -> str:
    """Step 1: a nonce from the link, believed only when the reply is signed over our own challenge."""
    challenge = challenge or secrets.token_urlsafe(32)
    status, body, headers = exchange(port, "GET", f"/nonce?c={challenge}")
    if status != 200 or not signed(token, challenge, status, body, headers, nonce_reply=True):
        raise NotTheLink(f"/nonce answered {status} without the link's signature")
    return json.loads(body)["nonce"]


def authorization(token: str, issued: str, method: str, target: str, body: bytes = b"") -> str:
    return proof.authorization(issued, sign(token, proof.request_message(issued, method, target, body)))


def call(port: int, token: str, method: str, target: str, body: bytes = b"") -> Reply:
    """Steps 1–3: nonce, signed request, signed reply. `target` is sent, and signed, exactly as given."""
    issued = nonce(port, token)
    status, reply, headers = exchange(
        port,
        method,
        target,
        body=body if method != "GET" else None,
        headers={"Authorization": authorization(token, issued, method, target, body)},
    )
    if not signed(token, issued, status, reply, headers):
        raise NotTheLink(f"{target} answered {status} without the link's signature")
    return Reply(status, reply)
