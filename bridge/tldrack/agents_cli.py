"""`claude agents --json` — Claude Code's SUPPORTED session-state surface, as the
check on the private index and its fallback.

── WHY BOTH ────────────────────────────────────────────────────────────────────
The arbiter's fast path reads `<config dir>/sessions/<pid>.json` (agent_index.py).
Claude Code's docs call those files "not a stable interface" and name
`claude agents --json` (2.1.145+) "the supported way to read session state from
outside Claude Code". So why not just switch? Measured 2026-10-27 on 2.1.312:

- **It costs a process.** 0.19 CPU-s and 140 MB RSS per call, and it lists ONE
  config dir per call (`CLAUDE_CONFIG_DIR`). Several accounts every 2 s would hold
  a good part of a core on a machine where many agents already compete for them.
- **It says less.** `{pid, cwd, kind, startedAt, sessionId, name, status,
  waitingFor?, state?}` — no tmux pane and no status stamp, the two fields the
  arbiter's join (agent_index.py rule 3) and its hook-vs-index ordering
  (state_arbiter.py rule 2) are built on.

So the private index stays the 2 s fast path, and this module is the RECONCILER:
every PUBLIC_TICK_SECONDS it reads every root through the public CLI, counts
agreement, and watches for the private format drifting under us.

── THE FALLBACK ────────────────────────────────────────────────────────────────
A root DRIFTS when, on DRIFT_AFTER_TICKS public reads in a row, the public CLI
lists a live session that sits in a tmux pane while the private index yields
nothing usable for that root at all. Claude changed the file and we can no
longer read it. That root then runs on public entries (`mode: "public"`), read
every DRIFT_TICK_SECONDS — the cost is paid only while it is needed — and flips
back the first time the index parses again.

A public entry carries no status stamp, so it would always look newer than any
hook. It is therefore handed to the arbiter only once two consecutive reads
agree, stamped with the FIRST of them: a hook that landed between the two wins
by state_arbiter.py rule 2, exactly as it would against the index.

The pane comes from the process tree: walk the session's pid up its parents
until one is a tmux pane's pid (`tmux list-panes -F '#{pane_id} #{pane_pid}'`).

Never raises out of a read; every probe is injectable for tests
(tests/test_agents_cli.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable, Mapping

from tldrack.agent_index import (
    WAITING_FOR_MAX,
    IndexEntry,
    parse_ps_table,
    process_started,
    read_process_table,
)
from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.snapshot import PANE_ID_RE

#: Claude's public status word → the bridge's state vocabulary. Anything else
#: (a future word) is dropped, never guessed.
PUBLIC_STATES = {"busy": "busy", "idle": "free", "waiting": "attention"}

#: The reconciler's period, and its period while any root has drifted.
PUBLIC_TICK_SECONDS = 15.0
DRIFT_TICK_SECONDS = 5.0

#: Consecutive public reads that must show a drift before a root switches.
DRIFT_AFTER_TICKS = 2

#: One `claude agents --json` — a 0.2 s call; this is the wedged-CLI backstop.
CLI_TIMEOUT_SECONDS = 3.0

#: A Claude is a few processes below its pane's shell (zsh → env → claude).
MAX_PARENT_HOPS = 16


@dataclass(frozen=True)
class PublicEntry:
    """One session as `claude agents --json` reports it, in the bridge's vocabulary."""

    pid: int
    state: str
    waiting_for: str | None
    agent_session_id: str | None


def parse_public_entries(raw: object) -> list[PublicEntry]:
    """The CLI's JSON array → entries. Pure; a malformed item is skipped, a
    non-list is no entries."""
    if not isinstance(raw, list):
        return []
    entries: list[PublicEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        state = PUBLIC_STATES.get(item.get("status"))
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or state is None:
            continue
        waiting = item.get("waitingFor")
        session_id = item.get("sessionId")
        entries.append(
            PublicEntry(
                pid=pid,
                state=state,
                waiting_for=waiting[:WAITING_FOR_MAX] if isinstance(waiting, str) and waiting else None,
                agent_session_id=session_id
                if isinstance(session_id, str) and AGENT_SESSION_ID_RE.fullmatch(session_id)
                else None,
            )
        )
    return entries


def parse_pane_pids(text: str) -> dict[int, str]:
    """`tmux list-panes -a -F '#{pane_id} #{pane_pid}'` → pane pid → pane id. Pure."""
    panes: dict[int, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and PANE_ID_RE.fullmatch(parts[0]) and parts[1].isdigit():
            panes[int(parts[1])] = parts[0]
    return panes


def pane_for_pid(pid: int, parent_of: Mapping[int, int], pane_pids: Mapping[int, str]) -> str | None:
    """The tmux pane whose process tree holds `pid`, or None (not in tmux). Pure."""
    current = pid
    for _ in range(MAX_PARENT_HOPS):
        if current in pane_pids:
            return pane_pids[current]
        parent = parent_of.get(current)
        if parent is None or parent <= 1 or parent == current:
            return None
        current = parent
    return None


def read_pane_pids() -> dict[int, str]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_pane_pids(result.stdout) if result.returncode == 0 else {}


def public_env(root: str, default_root: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env for one root's call. The default account is never PINNED
    (profiles.py rule 3), so for it CLAUDE_CONFIG_DIR is unset instead."""
    env = dict(os.environ if base is None else base)
    if os.path.realpath(root) == os.path.realpath(default_root):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = root
    return env


def read_public(cli: str, root: str, default_root: str) -> list[PublicEntry] | None:
    """One root through the CLI; None when the call failed (missing, timed out,
    not JSON) — a failure is "no evidence", never "no sessions"."""
    try:
        result = subprocess.run(
            [cli, "agents", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=CLI_TIMEOUT_SECONDS,
            env=public_env(root, default_root),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return parse_public_entries(json.loads(result.stdout))
    except ValueError:
        return None


@dataclass
class RootHealth:
    """One root's reconciliation, as `/snapshot` `state_sources[root]` serves it."""

    mode: str = "index"  # index | public
    agree: int = 0  # in-rack sessions both sources read the same
    disagree: int = 0  # … read differently (a moment apart; small is normal)
    index_missing: int = 0  # in-rack sessions the public CLI lists and the index lacks
    outside_rack: int = 0  # live sessions in no tmux pane: invisible to the rack
    drift_ticks: int = 0
    checked_at: float | None = None
    cli_ok: bool = True

    def wire(self) -> dict:
        return {
            "mode": self.mode,
            "agree": self.agree,
            "disagree": self.disagree,
            "index_missing": self.index_missing,
            "outside_rack": self.outside_rack,
            "checked_at": self.checked_at,
            "cli_ok": self.cli_ok,
        }


def compare_root(
    public: Iterable[PublicEntry], private: Mapping[int, str], pane_of: Callable[[int], str | None]
) -> tuple[int, int, int, int]:
    """`(agree, disagree, index_missing, outside_rack)` for one root. Pure.

    `private` is pid → state from the index's believable entries. The index
    says `background` where the public CLI can only say idle, so the two
    turn-over words agree."""
    agree = disagree = missing = outside = 0
    for entry in public:
        if pane_of(entry.pid) is None:
            outside += 1
            continue
        state = private.get(entry.pid)
        if state is None:
            missing += 1
        elif state == entry.state or {state, entry.state} <= {"free", "background"}:
            agree += 1
        else:
            disagree += 1
    return agree, disagree, missing, outside


@dataclass
class _Pending:
    state: str
    first_seen: float
    confirmed: bool = False


@dataclass
class _RootState:
    health: RootHealth = field(default_factory=RootHealth)
    entries: dict[int, IndexEntry] = field(default_factory=dict)  # confirmed, for a drifted root
    pending: dict[int, _Pending] = field(default_factory=dict)


class PublicStatus:
    """The reconciler: its own thread, so the arbiter's 2 s tick never waits on a
    CLI. `observe` is fed the index's per-root read every arbiter tick;
    `drifted` / `entries_for` are what AgentIndexReader merges back in."""

    def __init__(
        self,
        roots: Callable[[], Iterable[str]],
        cli: Callable[[], str | None],
        default_root: str,
        *,
        read: Callable[[str, str, str], list[PublicEntry] | None] = read_public,
        pane_pids: Callable[[], Mapping[int, str]] = read_pane_pids,
        process_table: Callable[[], str | None] = read_process_table,
        started: Callable[[int], str | None] = process_started,
        clock: Callable[[], float] = time.time,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        # The operator's switch (features.py `PUBLIC_STATUS`), asked every tick.
        self._enabled = enabled
        self._roots = roots
        self._cli = cli
        self._default_root = default_root
        self._read = read
        self._pane_pids = pane_pids
        self._process_table = process_table
        self._started = started
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, _RootState] = {}
        self._private: dict[str, dict[int, str]] = {}
        self._starts: dict[int, str | None] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the arbiter's side ---------------------------------------------------
    def observe(self, by_root: Mapping[str, Iterable[IndexEntry]]) -> None:
        with self._lock:
            self._private = {root: {e.pid: e.state for e in entries} for root, entries in by_root.items()}

    def drifted(self) -> set[str]:
        with self._lock:
            return {root for root, st in self._state.items() if st.health.mode == "public"}

    def entries_for(self, root: str) -> list[IndexEntry]:
        with self._lock:
            st = self._state.get(root)
            return list(st.entries.values()) if st else []

    def health(self) -> dict[str, dict]:
        """root → its readout; empty until the first public read."""
        with self._lock:
            return {root: st.health.wire() for root, st in self._state.items() if st.health.checked_at is not None}

    # -- the timer ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="public-status", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        delay = 1.0  # the first read soon after start, so drift is caught early
        while not self._stop.wait(delay):
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 — the timer may never die
                print(f"tldrack: public status poll failed ({err})")
            delay = DRIFT_TICK_SECONDS if self.drifted() else PUBLIC_TICK_SECONDS

    def tick(self) -> None:
        """One turn of the timer: a public pass, unless the operator switched the cross-check off.
        Off forgets, it does not freeze: a root left in `public` mode would keep answering for the
        index from a read that is never refreshed again."""
        if self._enabled():
            self.poll_once()
        else:
            self.forget()

    def forget(self) -> None:
        """Drop every public read: no root is drifted, and `health` is empty, as before the first one."""
        with self._lock:
            self._state.clear()

    def poll_once(self) -> None:
        """One public pass over every root."""
        cli = self._cli()
        roots = list(dict.fromkeys(self._roots()))
        if cli is None or not roots:
            return
        with ThreadPoolExecutor(max_workers=len(roots)) as pool:
            reads = dict(zip(roots, pool.map(lambda root: self._read(cli, root, self._default_root), roots)))
        pane_pids = self._pane_pids()
        table = self._process_table()
        parent_of = {pid: ppid for pid, ppid, _cmd in parse_ps_table(table)} if table else {}
        now = self._clock()
        with self._lock:
            private = dict(self._private)
            for root, public in reads.items():
                st = self._state.setdefault(root, _RootState())
                self._reconcile(st, public, private.get(root, {}), parent_of, pane_pids, now)

    def _reconcile(
        self,
        st: _RootState,
        public: list[PublicEntry] | None,
        private: Mapping[int, str],
        parent_of: Mapping[int, int],
        pane_pids: Mapping[int, str],
        now: float,
    ) -> None:
        health = st.health
        health.checked_at = now
        health.cli_ok = public is not None
        if public is None:
            return  # no evidence either way: keep the mode, keep the counts
        panes = {entry.pid: pane_for_pid(entry.pid, parent_of, pane_pids) for entry in public}
        health.agree, health.disagree, health.index_missing, health.outside_rack = compare_root(public, private, panes.get)
        in_rack = [entry for entry in public if panes[entry.pid] is not None]
        if private:
            health.drift_ticks, health.mode = 0, "index"
        elif in_rack:
            health.drift_ticks += 1
            if health.drift_ticks >= DRIFT_AFTER_TICKS:
                health.mode = "public"
        else:
            health.drift_ticks = 0
        # Reads start counting toward confirmation on the first drift tick, so the
        # switch hands the arbiter entries at once rather than one read later.
        if health.mode == "public" or health.drift_ticks:
            confirmed = self._confirm(st, in_rack, panes, now)
        else:
            confirmed, st.pending = {}, {}
        st.entries = confirmed if health.mode == "public" else {}

    def _confirm(
        self, st: _RootState, in_rack: list[PublicEntry], panes: Mapping[int, str | None], now: float
    ) -> dict[int, IndexEntry]:
        """Entries two consecutive reads agree on, stamped with the first read."""
        confirmed: dict[int, IndexEntry] = {}
        live = {entry.pid for entry in in_rack}
        st.pending = {pid: p for pid, p in st.pending.items() if pid in live}
        for entry in in_rack:
            pending = st.pending.get(entry.pid)
            if pending is None or pending.state != entry.state:
                st.pending[entry.pid] = _Pending(entry.state, now)
                continue
            pending.confirmed = True
            proc_start = self._proc_start(entry.pid)
            if proc_start is None:
                continue
            confirmed[entry.pid] = IndexEntry(
                session="",
                state=entry.state,
                status_at=pending.first_seen,
                waiting_for=entry.waiting_for,
                agent_session_id=entry.agent_session_id,
                pid=entry.pid,
                proc_start=proc_start,
                pane=panes[entry.pid],
            )
        return confirmed

    def _proc_start(self, pid: int) -> str | None:
        """`ps -o lstart=` once per pid — what `still_running` checks it against."""
        if pid not in self._starts:
            if len(self._starts) > 512:
                self._starts.clear()
            started = self._started(pid)
            self._starts[pid] = " ".join(started.split()) if started else None
        return self._starts[pid]
