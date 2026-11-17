"""The per-launch decision secret (ladder rung 3): what an answer must carry to count as the operator's.

The bridge mints 32 random bytes at start; nothing persists it. A browser page fetches it from
`GET /decision-secret`, which answers only a request carrying an allowed `Origin` AND
`Sec-Fetch-Site` — headers every browser fetch sends and curl from an agent shell does not by
default. Every decision answer (`/inbox/answer`, `/inbox/defer`, `/inbox/dismiss`) must then carry it
in `X-VC-Decision-Secret`, compared in constant time, with an allowed Origin present.

THIS IS FRICTION, NOT A WALL. An agent shell runs as the same user as the bridge and can forge both
headers. The wall is the hard floor: an
agent reaches the bridge only through named CLI verbs (`tldrack lead …`), and none of them
answers. What this file buys is that an answer cannot arrive by accident, by a confused agent
reusing a route it saw, or from text an agent wrote — evidence is never authorization.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable, Mapping

SECRET_HEADER = "X-VC-Decision-Secret"
SECRET_BYTES = 32


class DecisionSecret:
    def __init__(self, value: str | None = None) -> None:
        self.value = value if value is not None else secrets.token_urlsafe(SECRET_BYTES)

    def check(self, presented: object) -> bool:
        return isinstance(presented, str) and hmac.compare_digest(presented.encode(), self.value.encode())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:  # http.server's message is case-insensitive; a plain dict in a test is not
        value = next((v for k, v in headers.items() if k.lower() == name.lower()), None)
    return value


def browser_origin(headers: Mapping[str, str], origin_allowed: Callable[[str | None], bool]) -> str | None:
    """The request's Origin when it is a real, allowed one; None for absent or refused."""
    origin = _header(headers, "Origin")
    if not origin or origin == "null" or not origin_allowed(origin):
        return None
    return origin


def secret_refusal(headers: Mapping[str, str], origin_allowed: Callable[[str | None], bool]) -> str | None:
    """Why `GET /decision-secret` refuses this request, or None to hand the secret over."""
    if browser_origin(headers, origin_allowed) is None:
        return "the decision secret is served to an allowed browser origin only"
    if not _header(headers, "Sec-Fetch-Site"):
        return "the decision secret is served to a browser fetch only"
    return None


def answer_refusal(
    headers: Mapping[str, str], secret: DecisionSecret, origin_allowed: Callable[[str | None], bool]
) -> str | None:
    """Why an operator verb refuses this request, or None when it carries the secret from a browser."""
    if browser_origin(headers, origin_allowed) is None:
        return "decisions are answered from the operator's UI only"
    if not secret.check(_header(headers, SECRET_HEADER)):
        return "missing or wrong decision secret; reload the page"
    return None
