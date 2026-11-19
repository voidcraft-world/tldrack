"""Who may talk to the link's listener at all, decided from the request headers before anything else.

In order, and the first refusal wins. These run before EVERY request, `/nonce` included:

1. **Host** — absent, or exactly a loopback name with an optional port. A DNS-rebinding page addresses the
   listener by its own hostname, and the bind cannot tell (bridge/CLAUDE.md § Security posture owns
   the attack). Matched by a strict pattern, not by parsing, so `evil@127.0.0.1` is not loopback. Two Host
   headers are refused: which one counts is exactly what a smuggling trick relies on.
2. **No proxy headers.** Nothing legitimate proxies to this port; `adb reverse` is a raw byte tunnel.
3. **No browser.** ANY `Origin` (even `null`) or any `sec-fetch-*` header means a browser. The app sends none.

**The tailnet door** (`tailnet`, off unless the operator configured it: config.py) is a second way in, for a request
`tailscale serve` proxied from the tailnet. It is told apart by its `Host`, the one configured machine name and port,
and it must then look exactly like what serve sends: serve's own headers and no others, no Funnel marker, and the
login serve vouches for equal to the configured one. Why each, and what these headers are NOT (proof: anything on
this Mac's loopback can forge them): owned by plugins/phone-link/README.md § The tailnet door — see the note there.
A request with a loopback `Host` never takes this path, so rule 2 still refuses a tailnet caller that claims to be
local: serve always adds its `X-Forwarded-*`.

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
from phone_link.config import TailnetDoor

_LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


@dataclass(frozen=True)
class Refusal:
    status: int
    error: str


HOST_REFUSED = Refusal(403, "host_refused")
PROXY_REFUSED = Refusal(403, "proxy_refused")
BROWSER_REFUSED = Refusal(403, "browser_refused")
TAILNET_REFUSED = Refusal(403, "tailnet_refused")
UNLINKED = Refusal(401, "unlinked")

#: Every proxy or identity header `tailscale serve` adds to a tailnet request (its `ipn/ipnlocal/serve.go`). The
#: door takes these and no other header of their families: a new one is refused until it is read and listed here.
_SERVE_FORWARDED = frozenset({"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"})
_SERVE_IDENTITY = frozenset(
    {"tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic", "tailscale-headers-info"}
)
#: Serve sets it on a request that came from the public internet through Funnel.
_FUNNEL = "tailscale-funnel-request"


def _is_browser(headers: Mapping[str, list[str]]) -> bool:
    return "origin" in headers or any(name.startswith("sec-fetch-") for name in headers)


def refusal(headers: Mapping[str, list[str]], tailnet: TailnetDoor | None = None) -> Refusal | None:
    """Why this caller is refused before any proof, or None. `headers` maps LOWERCASE names to every value sent."""
    hosts = headers.get("host", [])
    if len(hosts) > 1:
        return HOST_REFUSED
    host = hosts[0].strip() if hosts else None
    if tailnet is not None and host is not None and host.lower() == tailnet.authority:
        return _tailnet_refusal(headers, tailnet)
    if host is not None and not _LOOPBACK_HOST.fullmatch(host):
        return HOST_REFUSED
    if any(name in PROXY_HEADERS or name.startswith(PROXY_HEADER_PREFIXES) for name in headers):
        return PROXY_REFUSED
    if _is_browser(headers):
        return BROWSER_REFUSED
    return None


def _one(headers: Mapping[str, list[str]], name: str) -> str | None:
    values = headers.get(name, [])
    return values[0].strip() if len(values) == 1 else None


def _tailnet_refusal(headers: Mapping[str, list[str]], tailnet: TailnetDoor) -> Refusal | None:
    """A request addressed to the tailnet door: exactly what `tailscale serve` sends for the configured login."""
    if _FUNNEL in headers:
        return TAILNET_REFUSED
    for name in headers:
        if name in PROXY_HEADERS and name not in _SERVE_FORWARDED:
            return TAILNET_REFUSED
        if name.startswith("x-forwarded-") and name not in _SERVE_FORWARDED:
            return TAILNET_REFUSED
        if name.startswith("tailscale-") and name not in _SERVE_IDENTITY:
            return TAILNET_REFUSED
    if _one(headers, "x-forwarded-for") is None or (_one(headers, "x-forwarded-proto") or "").lower() != "https":
        return TAILNET_REFUSED
    forwarded_host = (_one(headers, "x-forwarded-host") or "").lower()
    if forwarded_host not in (tailnet.authority, tailnet.host):
        return TAILNET_REFUSED
    if (_one(headers, "tailscale-user-login") or "").lower() != tailnet.login.lower():
        return TAILNET_REFUSED
    if _is_browser(headers):
        return BROWSER_REFUSED
    return None


def credential(headers: Mapping[str, list[str]]) -> proof.Credential | None:
    """The request's one `Authorization: PhoneLink n=…, mac=…`, else None (then the answer is 401 `unlinked`)."""
    return proof.credential(headers.get("authorization", []))
