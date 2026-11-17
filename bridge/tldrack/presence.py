"""Which rack surfaces are on screen right now (POST /presence).

The bridge already knows everything about *tmux* — which sessions exist, which
have a terminal attached, what each agent is doing. It knows nothing about the
BROWSER side, and that gap is structural rather than accidental:

  * `POST /focus` answers with a count of SSE clients, which cannot tell "a
    rack tab exists somewhere" from "the rack is on screen and the
    engine is running".
  * The agent-rack POP-OUT window (agent-rack-window.html) is invisible here by
    DESIGN. Its whole architecture (the pop-out's window composable, rule 3) makes
    the main SPA window the only thing that polls this bridge — the satellite is
    fed over a BroadcastChannel and never opens a socket to us. So it cannot
    report itself, and nothing downstream can discover it.

Both matter to a native client, which wants to offer "jump to where this session
already is" instead of spawning yet another iTerm window attached to the same
tmux session. So the main SPA window heartbeats a small record here and we serve
it back on /snapshot.

THREE RULES, each load-bearing:

 1. IN-MEMORY ONLY, never persisted. models.py persists a session's last-known
    model precisely because an idle Claude emits no hooks; the opposite applies
    here. "The rack is on screen" restored from disk after a restart is an
    outright lie, and the client re-reports within one heartbeat anyway.
 2. TTL'd, not cleared on disconnect. There is no disconnect: a closed tab, a
    crashed renderer and a sleeping laptop all just stop beating. Records expire
    on READ so a stale one can never outlive the tab that wrote it.
 3. An unchanged heartbeat is a NO-OP. `set()` reports whether anything actually
    moved so the caller can skip notify_snapshot(); without that, every open tab
    would push a full SSE snapshot to every client every few seconds forever.
    Same reasoning as SessionModelStore.set()'s unchanged-reading guard.
"""

from __future__ import annotations

import re
import threading
import time

from tldrack.snapshot import SESSION_NAME_RE

# Shape-only guards, in the same spirit as icons.py's ICON_TOKEN_RE: the bridge
# validates that a value cannot break anything downstream, not that it belongs to
# some vocabulary the clients own.
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

#: Clients beat every 5s, so this survives two missed beats before a record is
#: considered gone. Tighter than that and a busy main thread reads as "closed".
PRESENCE_TTL_SECONDS = 15.0

#: Matches events.py's MAX_SSE_CLIENTS. A bound is required, not tidiness: a
#: client_id is caller-chosen, so a loop posting fresh ones is otherwise
#: unbounded memory growth.
MAX_PRESENCE_CLIENTS = 16

#: Every field we keep, with the value used when a client omits it. Clients are
#: allowed to send a subset — a future non-SPA reporter (a native client itself,
#: say) should not have to invent rack fields to be listed.
PRESENCE_DEFAULTS: dict[str, object] = {
    "kind": "spa",
    "world": None,
    "running": False,
    "rack_open": False,
    "rack_attached": False,
    "rack_focused": None,
}


def _require_bool(payload: dict, key: str) -> bool:
    value = payload.get(key, PRESENCE_DEFAULTS[key])
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_token(payload: dict, key: str) -> str | None:
    """A short opaque token, or None. Absent and null both mean None — the SPA
    reports `world: null` when it is in the world SELECTOR rather than a world,
    and that is a real state, not missing data."""
    value = payload.get(key, PRESENCE_DEFAULTS[key])
    if value is None:
        return None
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ValueError(f"{key} must be null or a short token")
    return value


def parse_presence(payload: dict) -> tuple[str, dict]:
    """`(client_id, record)` from a POST /presence body.

    Fails LOUD on anything malformed (the caller 400s) rather than storing a
    coerced guess — same posture as parse_usage_limits. A record that is wrong
    about which session the rack is showing sends the user to the wrong window,
    which is worse than no record at all.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id):
        raise ValueError("client_id must be 8-64 chars of [A-Za-z0-9_-]")

    rack_focused = payload.get("rack_focused", PRESENCE_DEFAULTS["rack_focused"])
    if rack_focused is not None and (not isinstance(rack_focused, str) or not SESSION_NAME_RE.fullmatch(rack_focused)):
        raise ValueError("rack_focused must be null or a valid session name")

    record = {
        "kind": _require_token(payload, "kind") or PRESENCE_DEFAULTS["kind"],
        "world": _require_token(payload, "world"),
        "running": _require_bool(payload, "running"),
        "rack_open": _require_bool(payload, "rack_open"),
        "rack_attached": _require_bool(payload, "rack_attached"),
        "rack_focused": rack_focused,
    }
    return client_id, record


class PresenceStore:
    """TTL'd per-client presence records. In-memory, bounded, never persisted."""

    def __init__(
        self,
        ttl_seconds: float = PRESENCE_TTL_SECONDS,
        max_clients: int = MAX_PRESENCE_CLIENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_clients = max_clients
        # client_id → (record, last_seen_monotonic)
        self._clients: dict[str, tuple[dict, float]] = {}

    def set(self, client_id: str, record: dict, now: float | None = None) -> bool:
        """Record a heartbeat. Returns True when something a client could SEE
        changed — a new client, or any field differing from the stored record.

        A pure keep-alive returns False so the caller skips notify_snapshot().
        Rule 3 in the module docstring: without this, N open tabs push N full
        SSE snapshots to every listener every heartbeat, forever.
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(stamp)
            previous = self._clients.get(client_id)
            changed = previous is None or previous[0] != record
            if previous is None and len(self._clients) >= self._max_clients:
                # At the cap with a genuinely new client: drop the least recently
                # seen rather than refusing. The oldest is the one most likely to
                # be a tab that closed without its TTL having elapsed yet.
                oldest = min(self._clients.items(), key=lambda item: item[1][1])[0]
                del self._clients[oldest]
            self._clients[client_id] = (dict(record), stamp)
            return changed

    def list(self, now: float | None = None) -> list[dict]:
        """Live records, newest-first, each with `client_id` + `age_seconds`.

        Expiry happens HERE rather than on a timer: there is no disconnect event
        to hang a cleanup off (a closed tab, a crashed renderer and a sleeping
        laptop are indistinguishable — they all just stop beating), and a reader
        must never see a record that has outlived its TTL.
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(stamp)
            entries = sorted(self._clients.items(), key=lambda item: item[1][1], reverse=True)
            return [
                {"client_id": client_id, **record, "age_seconds": round(stamp - seen, 1)} for client_id, (record, seen) in entries
            ]

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, (_, seen) in self._clients.items() if now - seen > self._ttl]
        for key in expired:
            del self._clients[key]
