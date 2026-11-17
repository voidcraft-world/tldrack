"""Is a WORKING agent stuck? The two shapes that need no model to see.

A busy row looks busy whether the agent is making progress or circling. Finding out meant opening
the pane, which is exactly the babysitting the rack exists to end. Two loops are visible from what
the bridge already reads, so this flags them instead:

 1. **The same command failed `REPEAT_FAILURES` times this turn.** A `Bash` call whose tool_result
    came back `is_error` (transcript.py `fold_tool_events` `failed`), keyed by the permission
    ledger's hash of the command (`tool_key`, so no command text is held), counted since that
    command last SUCCEEDED and since the turn began. Other tools are left out: four failed Edits on
    one file are four different edits, and "same command" is the claim made.
 2. **No progress for `NO_PROGRESS_SECONDS`.** No hook at all — main thread or subagent — on a busy
    row. A foreground `Bash` tops out at 10 minutes and a foreground subagent fires its own hooks,
    so 20 minutes of silence is a hung call, an API stall, or a busy the hooks never cleared.
    `time.monotonic` does not advance while the Mac sleeps, so a closed lid is not silence.

Only a `busy` row can be stuck: an `attention` row is waiting on the operator, a `background` row
finished its turn on purpose. A flag clears itself the moment its cause does (the command succeeds,
a hook lands, the turn ends); there is nothing to dismiss. Rows carry it as the additive
`stuck: {kind, count?, seconds?}`, and notify.py pings once per stuck episode, the same as a prompt.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

#: Failures of one command, since it last succeeded, that make a turn a loop. Three is a normal
#: fix-and-retry; the fourth identical failure is the agent not learning from the third.
REPEAT_FAILURES = 4

#: Silence on a busy row that counts as stuck. Twice the foreground Bash ceiling (10 min).
NO_PROGRESS_SECONDS = 20 * 60

REPEAT_FAILURE, NO_PROGRESS = "repeat_failure", "no_progress"


def repeat_failures(events: Mapping[str, Mapping], since: float | None = None) -> int:
    """The longest run of failures of one `Bash` command since it last succeeded. Pure.

    `events` is `TranscriptTracker.tool_events` (insertion order = call order); `since` (epoch s)
    drops calls from before the current turn. A call with no result yet counts as neither."""
    runs: dict[str, int] = {}
    for event in events.values():
        if event.get("name") != "Bash" or event.get("outcome") != "approved":
            continue
        at = event.get("at")
        if since is not None and isinstance(at, (int, float)) and at < since:
            continue
        key = event.get("key")
        if not isinstance(key, str):
            continue
        runs[key] = runs.get(key, 0) + 1 if event.get("failed") else 0
    return max(runs.values(), default=0)


def stuck_reading(state: object, failures: int, quiet_seconds: float) -> dict | None:
    """The row's `stuck` field, or None. Pure. A repeat failure outranks silence: it names a cause."""
    if state != "busy":
        return None
    if failures >= REPEAT_FAILURES:
        return {"kind": REPEAT_FAILURE, "count": failures}
    if quiet_seconds >= NO_PROGRESS_SECONDS:
        return {"kind": NO_PROGRESS, "seconds": int(quiet_seconds)}
    return None


class StuckDetector:
    """Progress clocks per session, and which stuck episodes have been reported.

    `note_hook` is every hook for the session (a subagent's too); `note_turn` is a main-thread
    prompt, which also starts the failure count over. `evaluate` is called at snapshot time, so a
    flag needs no timer of its own; `on_flagged(session, kind)` fires once per episode (the
    telemetry's `stuck_flagged`), never per poll."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        on_flagged: Callable[[str, str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._on_flagged = on_flagged
        self._lock = threading.Lock()
        self._progress: dict[str, float] = {}
        self._turn_at: dict[str, float] = {}
        self._flagged: dict[str, str] = {}

    def note_hook(self, session: str) -> None:
        with self._lock:
            self._progress[session] = self._clock()

    def note_turn(self, session: str) -> None:
        with self._lock:
            self._progress[session] = self._clock()
            self._turn_at[session] = self._wall()

    def evaluate(self, session: str, state: object, events: Mapping[str, Mapping]) -> dict | None:
        """The row's `stuck` field right now. A busy row never heard from starts its clock here, so
        an arbiter-only busy (an account with no hooks) is judged from when it was first seen."""
        now = self._clock()
        with self._lock:
            if state != "busy":
                # The clock restarts with the next busy: a prompt or a granted permission is not
                # twenty minutes of silence just because the last one ended long ago.
                self._flagged.pop(session, None)
                self._progress.pop(session, None)
                return None
            since = self._turn_at.get(session)
            quiet = now - self._progress.setdefault(session, now)
        reading = stuck_reading(state, repeat_failures(events, since), quiet)
        newly = None
        with self._lock:
            if reading is None:
                self._flagged.pop(session, None)
            elif self._flagged.get(session) != reading["kind"]:
                self._flagged[session] = reading["kind"]
                newly = reading["kind"]
        if newly is not None and self._on_flagged is not None:
            try:
                self._on_flagged(session, newly)
            except Exception:  # noqa: BLE001 — telemetry never costs the row its flag
                pass
        return reading

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            for table in (self._progress, self._turn_at, self._flagged):
                if old in table:
                    table[new] = table.pop(old)
                else:
                    table.pop(new, None)

    def forget(self, session: str) -> None:
        with self._lock:
            for table in (self._progress, self._turn_at, self._flagged):
                table.pop(session, None)
