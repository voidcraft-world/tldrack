"""A read from a page this bridge served: the origin the browser left unsaid.

A browser sends `Origin` on a cross-origin request, and on a same-origin one only when the method
is not GET or HEAD. So the rack page, REVIEW and EVALS, served by the bridge itself (`ui_static.py`),
read the bridge with no `Origin` at all. What they do send is `Sec-Fetch-Site: same-origin`, a
header a page cannot set.

The core reads "no Origin, yet a Sec-Fetch-Site" as a `no-cors` read from another site: right for
`cross-site` and `same-site`, wrong for this value. The effect was that the same page worked when
another site hosted it (that page sends an Origin) and answered 403 on the bridge's own port: every
plugin route behind a caller policy (the REVIEW deck, the EVALS page, the skills list) and every
route behind `_expensive_route_guard` (`/events`, `/ledger/…`, `/topic-labels`).

`own_page_origin` names the origin such a request has, `http://<Host>`, and the handler writes it
into the request before any guard reads it (`main.py` `StatusHandler.do_GET`). Every check that
follows, the core's or a plugin's, then judges a stated origin by its own rule, and none of them
needs to know this case exists.

ONLY FOR A LOOPBACK HOST. The listener is plain HTTP on loopback, so there the spelling is exact.
Behind a proxy the page's scheme and name are the proxy's, which this process cannot know, so
nothing is inferred and the request stays as it came. Under DNS rebinding the Host is the
attacker's name: not loopback, so nothing is inferred either, and the Host guard refuses it first.

Stdlib and the core's two guard helpers only.
"""

from __future__ import annotations

from voidcraft_world_bridge.guards import LOOPBACK_HOSTS, canonical_origin, host_header_hostname

#: What a browser sends when the page that asked and the server that answers are one origin.
SAME_ORIGIN = "same-origin"


def own_page_origin(origin: str | None, sec_fetch_site: str | None, host: str | None) -> str | None:
    """The origin of a same-origin browser read that states none, or None: every other request.

    None when an Origin is already stated, when the browser did not call it same-origin, and when
    the Host is absent, unreadable or not loopback (the module note owns why).
    """
    if origin is not None or sec_fetch_site is None or not host:
        return None
    if sec_fetch_site.strip().lower() != SAME_ORIGIN:
        return None
    if host_header_hostname(host) not in LOOPBACK_HOSTS:
        return None
    try:
        return canonical_origin(f"http://{host.strip()}")
    except (TypeError, ValueError):
        return None
