"""Correct the hook-reported state from Claude Code's own live index.

`agent_index.py` owns why the index is believed, what it fixes, and where the
`background` state comes from. This module owns the RULE and the timer that
applies it.

── THE RULE ────────────────────────────────────────────────────────────────────
For a row the index has a believable entry for, write the index's state when

1. it DISAGREES with the stored state (or nothing is stored — a Claude whose
   account has no hooks installed still gets a dot), and
2. the index changed status AFTER the stored state was written — so a hook that
   is newer than the index always wins, and
3. the index has held that status for HOLD_SECONDS — so a hook racing the index
   for the same transition gets to land first.

That one rule covers every measured leak with no per-case branches: Esc
(busy → free), a denied prompt (attention → free), a granted prompt
(attention → busy), the early prompt (busy → attention ~5 s before the
Notification hook), and an out-of-order Stop (free → busy).

Three refinements make room for `background` (the turn is over, work still runs):

4. **The settle zone.** When both the stored state and the index's verdict mean
   "the turn is over" (`free` / `background`), the verdict wins regardless of
   order. A hook can only ever say `free` there — it cannot see the work — so
   rule 2 would let a Stop hook pin a session to `free` with jobs running, or
   pin it to `background` after they ended. The verdict is the index's state,
   upgraded to `background` when it says idle but the process tree still holds
   work children (the version-proof second source).
5. **Delegated work.** An index `busy` that a hook has already ENDED (the stored
   state is turn-over and newer than the index's busy stamp), and that has
   outlasted that hook by HOLD_SECONDS, is `background`: Claude stays busy while
   a background agent runs, and would have flipped to idle/shell by now if its
   turn were all that was keeping it busy.

6. **An unbacked `shell`.** An index `shell` whose Claude has had no work child
   for UNBACKED_SHELL_SECONDS reads as `free`: Claude can keep a job in its task
   table long after the process is gone (agent_index.py § BACKGROUND WORK owns
   the measurement), and `background` means "closing this kills work". Three
   guards, because a wrong `free` invites the operator to close a session that is
   still working (`ShellBacking`):
   - the streak is every tick's read since the last work child, in any state, so
     a row that was already stale does not flash `background` at each turn end;
   - a process table that could not be read is not a zero, and ends every streak;
   - zero is believed only for a Claude version whose jobs this bridge has
     counted at least once. The count is a match on Claude's own wrapper command;
     a release that changes the wrapper would otherwise turn every real job into
     an unbacked one. Until some session on that version shows a job, its
     `shell` is believed as before.

Writes are compare-and-set on the store's `seq` (`StatusStore.correct`), so a
hook arriving between this read and this write is never overwritten. A
correction stamps `wall_at` = now, which is later than the index's `status_at`,
so rule 2 makes it fire exactly once per index transition.

── WHY A TIMER ─────────────────────────────────────────────────────────────────
The leaks are by definition the transitions that produce NO event, so nothing
could hang this off a request. It runs whether or not any client polls, which is
what keeps `notify.py` (phone push) and `/wait` honest with the rack page closed.
The same goes for a background job ending: nothing fires a hook for it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from tldrack.activity import KEEP_ACTIVITY
from tldrack.agent_index import (
    TURN_OVER_STATES,
    AgentIndexReader,
    IndexEntry,
    parse_ps_table,
    read_process_table,
    work_children,
)

#: How long the index must hold a status before it overrules the store. The
#: hook for the same transition, when there is one, lands within ~1 s.
HOLD_SECONDS = 2.0

#: Tick period. Latency of a correction is HOLD_SECONDS + up to one tick.
ARBITER_TICK_SECONDS = 2.0

#: How long a Claude must show no work child before its index `shell` is not
#: believed (rule 6): five ticks. A job that really ended flips the index to
#: idle within a second, so this only ever waits on a `shell` that is wrong.
UNBACKED_SHELL_SECONDS = 10.0


def index_verdict(entry: IndexEntry, shells: int, unbacked: bool = False) -> str:
    """What the index and the process tree say, before any hook is weighed. Pure.

    `shells` counts the work children of the entry's pid. It is consulted only
    when the index says idle — a foreground Bash in a busy turn is the same
    kind of child (agent_index.py § BACKGROUND WORK). `unbacked` is rule 6's
    answer for this entry (`ShellBacking`), consulted only when the index says
    `shell`.
    """
    if entry.state == "free" and shells > 0:
        return "background"
    if entry.state == "background" and unbacked:
        return "free"
    return entry.state


class ShellBacking:
    """Rule 6's memory: how long each Claude has gone without a work child, and
    which Claude versions the work-child match has been seen to work on. The
    module header owns why each guard exists. Not thread-safe: one tick owns it.
    """

    def __init__(self) -> None:
        # (pid, procStart) → when its current run of empty reads began.
        self._empty_since: dict[tuple[int, str], float] = {}
        self._counted_versions: set[str] = set()

    def observe(self, index: Mapping[str, IndexEntry], work: Mapping[str, int] | None, now: float) -> frozenset[str]:
        """One tick's read → the sessions whose index `shell` is unbacked.

        `work` is session → work children, or None when the process table could
        not be read. An entry missing from one read starts its streak again.
        """
        if work is None:
            self._empty_since = {}
            return frozenset()
        empty_since: dict[tuple[int, str], float] = {}
        unbacked: set[str] = set()
        for session, entry in index.items():
            if work.get(session, 0) > 0:
                self._counted_versions.add(entry.version)
                continue
            key = (entry.pid, entry.proc_start)
            since = empty_since[key] = self._empty_since.get(key, now)
            if entry.state == "background" and entry.version in self._counted_versions and now - since >= UNBACKED_SHELL_SECONDS:
                unbacked.add(session)
        self._empty_since = empty_since
        return frozenset(unbacked)


def decide_correction(
    current: Mapping | None, entry: IndexEntry, now: float, shells: int = 0, unbacked: bool = False
) -> str | None:
    """The state to write for one row, or None to leave it. Pure.

    `current` is `StatusStore.versions()[session]` (`state`, `wall_at`, `seq`),
    or None when the store has never heard of the session. `now` is epoch
    seconds, the same clock as `entry.status_at` and `wall_at`.
    """
    if now - entry.status_at < HOLD_SECONDS:
        return None
    verdict = index_verdict(entry, shells, unbacked)
    if current is None:
        return verdict
    stored = current.get("state")
    wall_at = current.get("wall_at")
    hook_newer = isinstance(wall_at, (int, float)) and not isinstance(wall_at, bool) and entry.status_at <= wall_at
    if verdict == "busy" and stored in TURN_OVER_STATES and hook_newer:
        if now - wall_at < HOLD_SECONDS:
            return None
        verdict = "background"
    if stored == verdict:
        return None
    if stored in TURN_OVER_STATES and verdict in TURN_OVER_STATES:
        return verdict
    if hook_newer:
        return None
    return verdict


def refine_turn_end(state: str, event: str, evidence: Callable[[], tuple[int, int]]) -> str:
    """A hook's `free`, refined at the moment it lands. Pure but for `evidence`.

    The clients count SSE frames as polls, so a Stop hook's `free` that the
    timer corrects to `background` 2–4 s later has already fired their
    "agent finished" notification. Every free-bound hook therefore asks
    `evidence()` — `(work children, open background agents)` right now — and
    stores `background` when either is non-zero. That covers Stop, the 60 s
    "waiting for your input" nag (which would otherwise flash a background
    row to free) and SessionStart; never SessionEnd, whose Claude is exiting.
    `evidence` is only called when it can change the answer.
    """
    if state != "free" or event == "SessionEnd":
        return state
    shells, agents = evidence()
    return "background" if shells + agents > 0 else state


def settled_state(stored: object, entry: IndexEntry | None, shells: Callable[[], int], unbacked: bool = False) -> object:
    """A turn-over row's state as rule 4 (the settle zone) reads it NOW, without waiting out
    HOLD_SECONDS — for a caller that must judge the row before the next tick has written it
    (main.py's landing record). Pure but for `shells`, the entry's work children, called only
    when the index says idle (`index_verdict` reads it nowhere else). `unbacked` is the last
    tick's rule 6 answer for the row (`StateArbiter.unbacked_shells`).

    A Stop stores `background` whenever the transcript holds a launch with no notification, because
    the index still says `busy` at that instant; a resumed conversation's old launch is one
    (attention.py § 1b). The index goes idle soon after, and the tick writes `free` once that has
    held HOLD_SECONDS, but the verdict lands in between. Outside the zone (an index still `busy`,
    at a prompt), or with no believable entry, the stored state stands.
    """
    if entry is None or stored not in TURN_OVER_STATES:
        return stored
    verdict = index_verdict(entry, shells() if entry.state == "free" else 0, unbacked)
    return verdict if verdict in TURN_OVER_STATES else stored


def message_for(entry: IndexEntry, state: str) -> str | None:
    """Only an attention row carries a message: Claude's own `waitingFor`."""
    return entry.waiting_for if state == "attention" else None


def activity_for(state: str) -> object:
    """A correction into a turn-over state ends the turn, as Stop would;
    anything else keeps the zone the agent was last seen in."""
    return None if state in TURN_OVER_STATES else KEEP_ACTIVITY


class StateArbiter:
    """The timer. `store` is a StatusStore (`versions`, `correct`); callbacks are
    the handler's own `record_transition` / `notify_snapshot` / records feed.

    Never started by `build_status_handler` — only by `start_status_server`, so
    a test that builds a handler is not reconciled against this machine's real
    Claude sessions.
    """

    def __init__(
        self,
        store,
        reader: AgentIndexReader,
        *,
        on_correction: Callable[[str, dict, str, object], None],
        on_changed: Callable[[], None],
        on_index: Callable[[dict[str, IndexEntry]], None] | None = None,
        on_settled: Callable[[], bool] | None = None,
        process_table: Callable[[], str | None] = read_process_table,
        interval: float = ARBITER_TICK_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._reader = reader
        self._on_correction = on_correction
        self._on_changed = on_changed
        self._on_index = on_index
        self._on_settled = on_settled
        self._process_table = process_table
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_index: dict[str, IndexEntry] = {}
        self._last_work: dict[str, int] = {}
        self._backing = ShellBacking()
        self._last_unbacked: frozenset[str] = frozenset()
        # When the last tick's index read BEGAN (the `clock`, epoch s); None until the first tick.
        self._last_read_at: float | None = None
        # Every entry believed at some point and not yet known to be dead —
        # how a session whose Claude crashed mid-background gets released.
        self._seen: dict[str, IndexEntry] = {}
        # The work counts clients can see (background rows only), so a 2 → 1
        # job change pushes a snapshot while a busy row's churn does not.
        self._visible_work: dict[str, int] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="state-arbiter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def last_index(self) -> dict[str, IndexEntry]:
        """The most recent read — `/restore` asks it which conversations are open."""
        with self._lock:
            return dict(self._last_index)

    def index_since(self, at: float) -> dict[str, IndexEntry]:
        """Claude's index as read no earlier than `at` (epoch s, the store's `wall_at` clock): the
        last tick's read when it began at or after `at`, else one fresh read, not cached, so
        `last_index` and `last_work` stay one tick's pair.

        A tick lags a hook by up to ARBITER_TICK_SECONDS, so the last read before a verdict lands
        ~SETTLE_SECONDS after its Stop was often taken while the turn still ran and says `busy`
        (attention.py § 1b; `settled_state`). A fresh read is a few small JSON files. An arbiter
        that has never ticked (one never started: a test-built handler) has no read to refresh and
        makes none, which keeps such a handler off this machine's real index.
        """
        with self._lock:
            if self._last_read_at is None or self._last_read_at >= at:
                return dict(self._last_index)
        return self._reader.read()

    def last_work(self) -> dict[str, int]:
        """session → work children of its Claude, as of the last tick. The
        `background_jobs` overlay reads it; meaningful on background rows only."""
        with self._lock:
            return dict(self._last_work)

    def unbacked_shells(self) -> frozenset[str]:
        """The sessions whose index `shell` the last tick did not believe (rule 6)."""
        with self._lock:
            return self._last_unbacked

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 — the timer may never die
                print(f"tldrack: state arbiter tick failed ({err})")

    def _read_work(self, index: Mapping[str, IndexEntry]) -> dict[str, int] | None:
        """ONE process-table read for every believable entry this tick; None when
        the table could not be read, which is not the same as nobody working."""
        if not index:
            return {}
        text = self._process_table()
        if text is None:
            return None
        by_pid = work_children(parse_ps_table(text), (entry.pid for entry in index.values()))
        return {session: by_pid.get(entry.pid, 0) for session, entry in index.items()}

    def tick(self) -> list[tuple[str, str]]:
        """One pass. Returns `(session, new_state)` for every correction written."""
        read_at = self._clock()
        index = self._reader.read()
        read = self._read_work(index)
        unbacked = self._backing.observe(index, read, read_at)
        work = read or {}
        with self._lock:
            self._last_index = dict(index)
            self._last_work = dict(work)
            self._last_unbacked = unbacked
            self._last_read_at = read_at
        if self._on_index is not None:
            self._on_index(index)
        now = self._clock()
        versions = self._store.versions()
        written: list[tuple[str, str]] = []
        for session, entry in sorted(index.items()):
            current = versions.get(session)
            state = decide_correction(current, entry, now, work.get(session, 0), session in unbacked)
            if state is not None and self._write(session, current, state, message_for(entry, state)):
                written.append((session, state))
        written.extend(self._release_departed(index, versions))
        changed = self._note_visible_work(work)
        if self._settle() or written or changed:
            self._on_changed()
        return written

    def _settle(self) -> bool:
        """`on_settled` — the caller's per-tick pass over the states this tick just
        settled (main.py re-tails turn-over transcripts) — True when it moved
        something clients see. Its failure must not swallow this tick's
        corrections, so it is caught here rather than by `_run`."""
        if self._on_settled is None:
            return False
        try:
            return bool(self._on_settled())
        except Exception as err:  # noqa: BLE001 — the corrections above still push
            print(f"tldrack: state arbiter settle pass failed ({err})")
            return False

    def _write(self, session: str, current: Mapping | None, state: str, message: str | None) -> bool:
        activity = activity_for(state)
        previous = self._store.correct(
            session,
            current.get("seq") if current else None,
            state,
            message,
            activity=activity,
        )
        if previous is None:
            return False  # a hook landed between the read and the write — it wins
        self._on_correction(session, previous, state, activity)
        return True

    def _release_departed(self, index: Mapping[str, IndexEntry], versions: Mapping[str, Mapping]) -> list[tuple[str, str]]:
        """`background` → `free` for a session whose Claude is gone.

        A crashed Claude fires no SessionEnd and its entry stops being believed,
        so no rule above ever looks at the row again — and unlike `busy`,
        `background` has no client-side staleness to age it out. Released only
        on positive evidence (`still_running` is False): an entry that merely
        failed to read this tick is kept and looked at again next tick.
        """
        released: list[tuple[str, str]] = []
        for session, entry in index.items():
            self._seen[session] = entry
        for session, entry in sorted(self._seen.items()):
            if session in index:
                continue
            if self._reader.still_running(entry):
                continue
            del self._seen[session]
            current = versions.get(session)
            if current is not None and current.get("state") == "background":
                if self._write(session, current, "free", None):
                    released.append((session, "free"))
        return released

    def _note_visible_work(self, work: Mapping[str, int]) -> bool:
        """Whether a background row's job count moved since the last tick."""
        states = self._store.versions()
        visible = {session: count for session, count in work.items() if states.get(session, {}).get("state") == "background"}
        changed = visible != self._visible_work
        self._visible_work = visible
        return changed
