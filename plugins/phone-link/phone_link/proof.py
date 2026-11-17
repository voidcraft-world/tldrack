"""The handshake's wire format: how the phone proves it holds the link's token without sending it, and how the
link proves it back. Why a bearer token is not enough, and the test vector both sides check: owned by
plugins/phone-link/README.md § The handshake — see the note there.

This module is the one Python spelling of that contract: the strings that get signed, the two header shapes, and
the book of nonces this link has issued. The token itself never reaches here; `TokenStore.sign` / `verify` key
the HMAC (token_store.py), so nothing in this file can leak it.

- **Domain-separated.** Every signed string starts with its own first line (`phone-link/2 request`,
  `phone-link/2 nonce`, `phone-link/2 reply`), so a signature made for one purpose is never valid for another.
  The `/nonce` reply needs its own: it is signed over a challenge the CALLER picks, so in the `reply` domain anyone
  who could reach `/nonce` could have the link sign a "proven" reply to a nonce it caught.
- **Single use.** A nonce is taken out of the book the first time it is presented, valid MAC or not, and it lives
  `NONCE_TTL_S`. A captured request cannot be replayed, and a wrong guess burns the nonce it guessed with.
- **Bounded.** At most `MAX_OUTSTANDING` nonces; past that the OLDEST goes, so a flood of `/nonce` calls can only
  expire other nonces, never block a new one.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl

#: The handshake's version, in every signed string and in `/health` and `/nonce` bodies.
VERSION = 2
SCHEME = "PhoneLink"
#: The header that carries the link's signature over each reply.
REPLY_HEADER = "Phone-Link-Mac"
NONCE_TTL_S = 30.0
MAX_OUTSTANDING = 64
NONCE_BYTES = 32

#: `secrets.token_urlsafe(32)`, as the link mints them.
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
#: The phone's challenge `c`: URL-safe base64, at least 128 bits.
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{22,128}")
#: `PhoneLink n=<nonce>, mac=<64 lowercase hex>`; the scheme is matched without case, the rest exactly.
_PARAMS = re.compile(r"n=([A-Za-z0-9_-]{43})[ ]*,[ ]*mac=([0-9a-f]{64})")


@dataclass(frozen=True)
class Credential:
    nonce: str
    mac: str


def credential(values: list[str]) -> Credential | None:
    """The nonce and MAC of exactly one `Authorization: PhoneLink n=…, mac=…` header, else None."""
    if len(values) != 1:
        return None
    scheme, _, params = values[0].strip().partition(" ")
    if scheme.lower() != SCHEME.lower():
        return None
    found = _PARAMS.fullmatch(params.strip())
    return Credential(found.group(1), found.group(2)) if found else None


def authorization(nonce: str, mac: str) -> str:
    return f"{SCHEME} n={nonce}, mac={mac}"


def challenge_of(query: str) -> str | None:
    """The `c` of a `/nonce` query: exactly one field, `c`, of the challenge's shape."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=2)
    except ValueError:
        return None
    if len(pairs) != 1 or pairs[0][0] != "c" or not CHALLENGE_RE.fullmatch(pairs[0][1]):
        return None
    return pairs[0][1]


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_message(nonce: str, method: str, target: str, body: bytes) -> bytes:
    """What the phone signs: the nonce, the method, the raw request target (path and query exactly as sent on the
    request line) and the SHA-256 of the exact body bytes (of `b""` when there is none)."""
    return _message("request", nonce, method, target, body_digest(body))


def nonce_message(challenge: str, status: int, body: bytes) -> bytes:
    """What the link signs on a `/nonce` reply: the phone's challenge, the status, and the SHA-256 of the exact
    body bytes."""
    return _message("nonce", challenge, str(status), body_digest(body))


def reply_message(nonce: str, status: int, body: bytes) -> bytes:
    """What the link signs on every reply to a proven request: the request's nonce, the status, and the SHA-256 of
    the exact body bytes."""
    return _message("reply", nonce, str(status), body_digest(body))


def _message(kind: str, *parts: str) -> bytes:
    # A request line arrives decoded as Latin-1, so every part encodes back to the exact bytes that were sent.
    return "\n".join((f"phone-link/{VERSION} {kind}", *parts)).encode("latin-1", "replace")


class NonceBook:
    """The nonces this link has issued and not yet seen back."""

    def __init__(
        self, *, ttl_s: float = NONCE_TTL_S, capacity: int = MAX_OUTSTANDING, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._issued: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._issued)

    def issue(self) -> str:
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        with self._lock:
            now = self._clock()
            self._prune(now)
            while len(self._issued) >= self._capacity:
                self._issued.popitem(last=False)
            self._issued[nonce] = now
        return nonce

    def take(self, nonce: str) -> bool:
        """True once, for a nonce this book issued inside the last `ttl_s`. Presented means spent."""
        with self._lock:
            issued = self._issued.pop(nonce, None)
            return issued is not None and self._clock() - issued <= self._ttl_s

    def _prune(self, now: float) -> None:
        while self._issued:
            oldest = next(iter(self._issued.values()))
            if now - oldest <= self._ttl_s:
                return
            self._issued.popitem(last=False)
