"""Who may frame a page this bridge serves: loopback pages, and the origins the operator named.

── WHY EVERY PAGE REFUSES OTHER PARENTS ───────────────────────────────────
A page that can be framed can be dressed up. Another site loads the rack, or a terminal, in an
invisible frame under a decoy button, and the operator's click lands on a control of ours: a spawn,
a canvas answer, a soft key. The Origin guard never sees it, because every request the framed page
makes is its own, same-origin. Only the framed page's own response can refuse the parent, with the
CSP directive `frame-ancestors`.

So the rule is the Origin guard's rule, spelt for the browser: a page may frame a bridge page when
it may call the bridge. That is any loopback page on any port, and each origin in settings.py
`allowed_origins`. Three places apply it:

- the status listener sends it on every response (`main.StatusHandler.end_headers`): the rack page,
  and every plugin page, because a plugin cannot set a header of its own;
- the terminal port stamps it on ttyd's responses (`ttyd_front.py`), because ttyd cannot;
- the terminal page takes soft keys and its config only from a parent on this same list
  (`index_patch.py`), which also covers a page that frames the rack legitimately and holds a
  hostile frame beside it, where `frame-ancestors` has nothing to refuse.

── WHAT CSP CANNOT SPELL ──────────────────────────────────────────────────
A CSP host is letters, digits, dots and hyphens, so an IPv6 literal (`[::1]` included) has no
spelling: a browser drops such a source with a console warning. A page on `[::1]` is framed by its
own origin only ('self'); use `localhost`. An `allowed_origins` entry with an IPv6 host still passes
the Origin guard, but frames nothing: `unframeable` names it so the bridge can say so at startup.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

#: Loopback on any port, both schemes: the guard's own reading (`guards.is_loopback_origin`).
LOOPBACK_SOURCES = (
    "http://localhost:*",
    "https://localhost:*",
    "http://127.0.0.1:*",
    "https://127.0.0.1:*",
)

_CSP_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")


def _frameable(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and parts.hostname is not None
        and _CSP_HOST.fullmatch(parts.hostname) is not None
        and origin == f"{parts.scheme}://{parts.netloc}"
    )


def framer_origins(allowed_origins: Iterable[str]) -> tuple[str, ...]:
    """The operator's origins that may frame a bridge page, sorted so the policy and the patched
    index's cache key are stable. Each entry is already in a browser's spelling (settings.py reads
    it through the core's `canonical_origins`); one CSP cannot spell is left out."""
    return tuple(sorted({origin for origin in allowed_origins if _frameable(origin)}))


def unframeable(allowed_origins: Iterable[str]) -> tuple[str, ...]:
    """The `allowed_origins` entries left out of `framer_origins`."""
    return tuple(sorted({origin for origin in allowed_origins if not _frameable(origin)}))


def frame_ancestors(allowed_origins: Iterable[str]) -> str:
    """The `Content-Security-Policy` value every bridge page is served with."""
    return " ".join(("frame-ancestors", "'self'", *LOOPBACK_SOURCES, *framer_origins(allowed_origins)))
