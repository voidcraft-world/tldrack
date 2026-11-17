"""Who may talk to the link's listener at all, decided from the request headers before anything else.

In order, and the first refusal wins. These run before EVERY request, `/nonce` included:

1. **Host** — absent, or exactly a loopback name with an optional port. A DNS-rebinding page addresses the
   listener by its own hostname, and the bind cannot tell (bridge/CLAUDE.md § Security posture owns
   the attack). Matched by a strict pattern, not by parsing, so `evil@127.0.0.1` is not loopback. Two Host
   headers are refused: which one counts is exactly what a smuggling trick relies on.
2. **No proxy headers.** Nothing legitimate proxies to this port; `adb reverse` is a raw byte tunnel.
3. **No browser.** ANY `Origin` (even `null`) or any `sec-fetch-*` header means a browser. The app sends none.

What comes after, the proof that the caller holds the token, is the handshake (proof.py); `credential` only
reads its header. Everything past the proof is answered only to a caller holding the token, so an unlinked caller
learns nothing about which paths exist.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from voidcraft_world_bridge.policy import PROXY_HEADER_PREFIXES, PROXY_HEADERS

from phone_link import proof

_LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


@dataclass(frozen=True)
class Refusal:
    status: int
    error: str


HOST_REFUSED = Refusal(403, "host_refused")
PROXY_REFUSED = Refusal(403, "proxy_refused")
BROWSER_REFUSED = Refusal(403, "browser_refused")
UNLINKED = Refusal(401, "unlinked")


def refusal(headers: Mapping[str, list[str]]) -> Refusal | None:
    """Why this caller is refused before any proof, or None. `headers` maps LOWERCASE names to every value sent."""
    hosts = headers.get("host", [])
    if len(hosts) > 1 or (hosts and not _LOOPBACK_HOST.fullmatch(hosts[0].strip())):
        return HOST_REFUSED
    if any(name in PROXY_HEADERS or name.startswith(PROXY_HEADER_PREFIXES) for name in headers):
        return PROXY_REFUSED
    if "origin" in headers or any(name.startswith("sec-fetch-") for name in headers):
        return BROWSER_REFUSED
    return None


def credential(headers: Mapping[str, list[str]]) -> proof.Credential | None:
    """The request's one `Authorization: PhoneLink n=…, mac=…`, else None (then the answer is 401 `unlinked`)."""
    return proof.credential(headers.get("authorization", []))
