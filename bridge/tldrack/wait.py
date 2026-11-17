"""Blocking state rendezvous for `GET /wait` — pure parts.

Why this exists: the bridge already knows every session's state, but only ever
*reported* it. Nothing could BLOCK until a session reached a state, so a script
(or a coordinating agent) that wanted to run after an agent went idle had to
busy-poll `/status` and invent its own debounce. Both of the tools in this space
independently grew the same primitive — Orca ships
`orca terminal wait --for tui-idle --timeout-ms N --json`, herdr ships a socket
`agent.wait` over idle/working/blocked/done — which is about as strong a signal
as design convergence gets.

Two guards make the difference between a useful primitive and a footgun:

  1. **Pin the occupant.** herdr's docs are explicit that `agent.wait` "pins the
     resolved pane occupant so a replacement cannot satisfy the wait". Without it,
     killing a wedged session and starting a fresh one under the same name
     instantly satisfies every waiter — they'd conclude the ORIGINAL work
     finished. tmux's `#{session_id}` (`$3`, `$7`…) is the right pin: it is unique
     per session for the life of the server and a recreated session gets a new one,
     where the *name* is deliberately reusable.

  2. **Always bounded.** A wedged agent must not be able to hold a caller open
     forever, so an absent/garbage timeout falls back to a default rather than
     meaning "wait indefinitely".
"""

from __future__ import annotations

import re
import subprocess

from tldrack.snapshot import TMUX_FIELD_SEP

#: States a caller may wait for. `unknown` is includable on purpose — "the bridge
#: has lost track of this session" is a legitimate thing to wait for. A turn that
#: ends with background work running reads `background`, not `free`, so
#: `for=free` waits for the work too (agent_index.py § BACKGROUND WORK).
WAITABLE_STATES = ("busy", "background", "attention", "free", "unknown")

DEFAULT_TIMEOUT_MS = 60_000
MAX_TIMEOUT_MS = 600_000
#: Store re-read interval while blocked. The StatusStore is in-memory, so this is
#: a dict lookup under a lock — cheap enough that latency, not cost, sets it.
POLL_SECONDS = 0.1

_SESSION_ID_RE = re.compile(r"^\$\d+$")

TMUX_SESSION_ID_FORMAT = TMUX_FIELD_SEP.join(("#{session_name}", "#{session_id}"))


def parse_wait_states(raw: str | None) -> set[str] | None:
    """Parse `for=` into a set of target states, or None if nothing is valid.

    Comma-separated so a caller can rendezvous on "stopped doing anything"
    (`for=free,attention`) without two requests racing each other. Returns None
    rather than a default: waiting for an unparseable state is a caller bug and
    should 400, unlike a malformed preview line count.
    """
    if not raw:
        return None
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    if not wanted or not wanted.issubset(WAITABLE_STATES):
        return None
    return wanted


def parse_timeout_ms(raw: str | None) -> int:
    """Requested timeout, clamped to (0, MAX_TIMEOUT_MS]. Garbage → default.

    Never returns 0/None: an unbounded wait would let one wedged agent pin a
    client (and a server thread) forever.
    """
    if raw is None:
        return DEFAULT_TIMEOUT_MS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    if value <= 0:
        return DEFAULT_TIMEOUT_MS
    return min(MAX_TIMEOUT_MS, value)


def parse_session_ids(output: str) -> dict[str, str]:
    """Parse `tmux list-sessions -F TMUX_SESSION_ID_FORMAT` into name → id.

    Ids look like `$0`, `$7`; anything else is dropped rather than trusted.
    """
    result: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(TMUX_FIELD_SEP, 1)
        if len(fields) != 2:
            continue
        name, session_id = fields
        if not _SESSION_ID_RE.fullmatch(session_id):
            continue
        result[name] = session_id
    return result


def tmux_session_id(session: str) -> str | None:
    """tmux's `#{session_id}` for a session, or None when it doesn't exist.

    The occupant pin. Unlike the name — reusable the moment a session dies —
    this id is never handed out twice by a running server.

    Enumerates and matches in Python rather than asking tmux for one session,
    because `-t` here takes a *pane* target: `-t =<name>` (the session-target
    form used by kill-session) returns an EMPTY string instead of erroring, which
    would silently disable the pin and turn this guard into a no-op. Measured on
    tmux 3.7b: `display-message -p -t '=vc_dbg' '#{session_id}'` → ``,
    `-t 'vc_dbg:'` → `$8`. An exact dict lookup has no such failure mode.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", TMUX_SESSION_ID_FORMAT],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None  # no server running
    return parse_session_ids(result.stdout).get(session)


def wait_outcome(
    state: str,
    wanted: set[str],
    pinned_id: str | None,
    current_id: str | None,
) -> str:
    """Classify one poll: 'matched', 'replaced', or 'waiting'.

    `replaced` outranks `matched`: if the pinned session is gone, a same-named
    successor sitting in the wanted state must NOT be reported as success — that
    is precisely the false positive the pin exists to prevent.

    A pin of None means the session had no tmux presence when the wait began
    (store-only), so there is nothing to invalidate and only the state matters.
    """
    if pinned_id is not None and current_id != pinned_id:
        return "replaced"
    return "matched" if state in wanted else "waiting"
