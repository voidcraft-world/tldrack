"""Who may call the operator-facing routes, and who counts as the agent rack.

The host's guards already run ahead of every route. This narrows them for the POLICY routes (which:
`plugin.POLICY_GETS` / `POLICY_POSTS`), the ones that carry the operator's text or act on this machine, through the
core's `CallerPolicy`: a loopback `Host`, no proxy header, and an Origin from exactly three places.

- **The operator's origins** (`context.allowed_origins`): a page the operator hosts and configured for the bridge.
  Nothing hosted is allowed by default, and no site is named here.
- **The bridge's own page** (`context.status_port`): the rack UI and the REVIEW deck it serves.
- **The dev server** on loopback, `DEV_PORTS`: that UI while it is being worked on.

Every other loopback origin is refused. The host lets any loopback page through, because a read from one adds
nothing an attacker on this machine lacks; an operator label is not a read, so this check is narrower on purpose.

`is_native` decides who may tag a rack surface on `/decide`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from voidcraft_world_bridge.guards import canonical_origin
from voidcraft_world_bridge.policy import (
    CallerPolicy,
    Refusal,
    host_hostname,
    LOOPBACK_HOSTNAMES,
    PROXY_HEADERS,
    PROXY_HEADER_PREFIXES,
)

#: Vite's default port and the fallbacks it walks when that one is taken, on either loopback name, http or https.
DEV_PORTS = range(5173, 5180)
DEV_ORIGINS = frozenset(
    f"{scheme}://{host}:{port}" for scheme in ("http", "https") for host in ("localhost", "127.0.0.1") for port in DEV_PORTS
)

RefusalCheck = Callable[[Mapping[str, str] | None], Refusal | None]


def bridge_origins(status_port: object) -> frozenset[str]:
    """The bridge's own page on its status port; empty when the host names none."""
    if not isinstance(status_port, int) or isinstance(status_port, bool) or not 0 < status_port < 65536:
        return frozenset()
    return frozenset(f"http://{host}:{status_port}" for host in ("127.0.0.1", "localhost"))


def operator_origins(raw: object, log: Callable[[str], None]) -> frozenset[str]:
    """`context.allowed_origins` → the browser spelling of each, a bad entry dropped with one line.

    Absent means none. One string is refused whole rather than read as a set of letters.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        log(f"[laya] ignored allowed_origins: expected a list of origins, got {type(raw).__name__}")
        return frozenset()
    kept: set[str] = set()
    for entry in raw:
        try:
            kept.add(canonical_origin(entry))
        except (TypeError, ValueError) as error:
            log(f"[laya] ignored an allowed origin: {error}"[:200])
    return frozenset(kept)


def policy_for(status_port: object = None, allowed: Iterable[str] = ()) -> CallerPolicy:
    """The policy routes' caller check over `allowed` (already canonical), the bridge's page and the dev server."""
    return CallerPolicy(allowed_origins=DEV_ORIGINS | bridge_origins(status_port) | frozenset(allowed), subject="Laya")


def is_native(headers: Mapping[str, str] | None) -> bool:
    """A caller that is not a browser and came through no proxy: the agent rack.

    THE definition `LAYA_LEDGER_RACK_SURFACES` (schemas/src/decide.ts) points at. Native means: no
    `origin`, no `sec-fetch-site`, no proxy header, and a Host that is absent or loopback. The
    in-process caller (tldrack `plugin_caller`) passes no headers at all, so it is native;
    the coordinator's loopback urllib call sends none of the browser headers, so it is native too.
    A browser page always sends `Origin` on a POST, so it never is — whatever page it is.
    These are browser/proxy checks, not authentication against other local processes.
    """
    if headers is None:
        return True
    named = {str(k).lower(): v for k, v in headers.items()}
    if "origin" in named or "sec-fetch-site" in named:
        return False
    if any(k in PROXY_HEADERS or k.startswith(PROXY_HEADER_PREFIXES) for k in named):
        return False
    host = named.get("host")
    return host is None or host_hostname(host) in LOOPBACK_HOSTNAMES
