"""Which requests the phone may make, matched exactly. Why these and nothing else: owned by
plugins/phone-link/README.md — see the note there.

The phone's path IS the bridge's path, so a match forwards the same string. Exactness is enforced by the
character set before any lookup: every allowed path is lowercase letters, digits, `-` and `/`, so a request
path holding anything else (`%`, `.`, `;`, `\\`, an uppercase letter) cannot be one of them, whatever it would
decode or normalise to. Nothing is decoded, so nothing can be decoded into a match. A doubled or trailing `/`
is refused by comparing against `posixpath.normpath`, and the lookup is a dict, so a prefix or an extra
segment is simply absent.

The query string is parsed and re-encoded, so the bridge only ever sees a well-formed one.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

#: The routes a phone may reach, phone path == bridge path, with the one method each answers.
FORWARDED: dict[str, str] = {
    "/plugin/laya/review/recent": "GET",  # the RECENT list
    "/plugin/laya/review/answer": "POST",
    "/plugin/laya/review/undo": "POST",
    "/ledger/search": "GET",  # conversation lookup (ui/src/laya-review/turn-context.ts)
    "/ledger/conversation": "GET",
    "/ledger/ingest": "POST",  # REFRESH INDEX
    "/topic-labels": "GET",  # the rack's display name for a session
}
#: The app's "linked?" probe, answered here and never forwarded.
HEALTH = "/health"
#: The handshake's first step, answered before any proof (proof.py). Not a route of `route_for`.
NONCE = "/nonce"

MAX_TARGET_CHARS = 4096
MAX_QUERY_FIELDS = 32
_PATH_CHARS = re.compile(r"/[a-z0-9/-]*")


@dataclass(frozen=True)
class Route:
    kind: str  # "health" | "forward"
    path: str
    query: str  # re-encoded, "" when there is none

    @property
    def target(self) -> str:
        return f"{self.path}?{self.query}" if self.query else self.path


def parse_target(target: str) -> tuple[str, str] | None:
    """`(path, re-encoded query)` for an origin-form request target, or None for anything else."""
    if not target or len(target) > MAX_TARGET_CHARS or not target.startswith("/") or target.startswith("//"):
        return None
    if "#" in target:
        return None
    path, _, query = target.partition("?")
    if not _PATH_CHARS.fullmatch(path) or posixpath.normpath(path) != path:
        return None
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
    except ValueError:
        return None
    return path, urlencode(pairs)


def route_for(method: str, target: str) -> Route | None:
    """The route this request may take, or None: then the answer is 404 `not_allowed`."""
    parsed = parse_target(target)
    if parsed is None:
        return None
    path, query = parsed
    if path == HEALTH:
        return Route("health", path, query) if method == "GET" else None
    if FORWARDED.get(path) != method:
        return None
    return Route("forward", path, query)
