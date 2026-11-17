"""POST /run's caller check: which browser pages may start an agent through this plugin.

The host's guards already run ahead of every route; this narrows them for `/run` alone,
through the core's `CallerPolicy`: a loopback `Host`, no proxy header, and an Origin from
exactly three places.

- **The operator's origins** (`context.allowed_origins`): a page the operator hosts and
  configured for the bridge. Nothing hosted is allowed by default, and no site is named here.
- **The dev server** on loopback, `DEV_PORTS`: the rack UI while it is being worked on.
- **The bridge's own terminal page** (ttyd, `context.port`), whose buttons start skills.
  Anything on that page is already in a shell, so admitting it grants nothing new.

Every other loopback origin is refused. The host lets any loopback page through, because a
read from one adds nothing an attacker on this machine lacks; starting an agent is not a read,
so this check is narrower on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from voidcraft_world_bridge.guards import canonical_origin
from voidcraft_world_bridge.policy import CallerPolicy, Refusal

#: Vite's default port and the fallbacks it walks when that one is taken.
DEV_PORTS = range(5173, 5180)
DEV_ORIGINS = frozenset(
    f"{scheme}://{host}:{port}" for scheme in ("http", "https") for host in ("localhost", "127.0.0.1") for port in DEV_PORTS
)

RefusalCheck = Callable[[Mapping[str, str] | None], Refusal | None]


def terminal_origins(ttyd_port: object) -> frozenset[str]:
    """The bridge's own ttyd page; empty when the host runs no terminals."""
    if not isinstance(ttyd_port, int) or isinstance(ttyd_port, bool) or not 0 < ttyd_port < 65536:
        return frozenset()
    return frozenset(f"http://{host}:{ttyd_port}" for host in ("127.0.0.1", "localhost"))


def operator_origins(raw: object, log: Callable[[str], None]) -> frozenset[str]:
    """`context.allowed_origins` → the browser spelling of each, a bad entry dropped with one line.

    Absent means none. One string is refused whole rather than read as a set of letters.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        log(f"  claude-skills: ignored allowed_origins — expected a list of origins, got {type(raw).__name__}")
        return frozenset()
    kept: set[str] = set()
    for entry in raw:
        try:
            kept.add(canonical_origin(entry))
        except (TypeError, ValueError) as exc:
            log(f"  claude-skills: ignored an allowed origin — {exc}"[:200])
    return frozenset(kept)


def run_refusal_for(ttyd_port: object, allowed: Iterable[str] = ()) -> RefusalCheck:
    """POST /run's caller check over `allowed` (already canonical), the dev server and the terminal page."""
    origins = DEV_ORIGINS | terminal_origins(ttyd_port) | frozenset(allowed)
    return CallerPolicy(allowed_origins=origins, subject="plugin").refusal
