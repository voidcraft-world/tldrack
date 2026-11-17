"""One allow-listed request, replayed to the bridge on this Mac's loopback.

What reaches the bridge is rebuilt, never relayed: the path and re-encoded query the allowlist matched, a
JSON body this plugin re-serialised, and three headers of our own (`Host`, `Accept`, and `Content-Type` on a
POST). None of the phone's headers cross, the `Authorization` proof above all, and no `Origin` is ever sent, so the
bridge reads the call as the native client it is. `http.client` rather than urllib: no proxy settings from
the environment, no redirects followed, nothing to configure away.

The reply passes through unchanged when it is JSON. An unreachable or silent bridge is `BridgeDown`.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass

LOOPBACK = "127.0.0.1"
TIMEOUT_S = 10.0
#: A ledger conversation page is the largest reply; the bridge's reader budgets keep it well under this.
MAX_REPLY_BYTES = 8 * 1024 * 1024


class BridgeDown(Exception):
    """The bridge refused the connection, timed out, or broke the HTTP exchange."""


class BadReply(Exception):
    """The bridge answered, but not with JSON this plugin will pass on."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes


def forward(port: int, method: str, target: str, body: bytes | None, *, timeout: float = TIMEOUT_S) -> Reply:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_REPLY_BYTES + 1)
        media = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise BridgeDown(type(exc).__name__) from exc
    finally:
        connection.close()
    if len(data) > MAX_REPLY_BYTES:
        raise BadReply("too_large")
    if media != "application/json":
        raise BadReply("not_json")
    return Reply(status, data)
