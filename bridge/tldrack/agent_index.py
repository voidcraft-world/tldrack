"""Claude Code's own live-session index — the busy/idle/waiting the hooks miss.

Every running interactive Claude keeps `<config dir>/sessions/<pid>.json` up to
date: `{pid, sessionId, cwd, procStart, version, tmux, status, statusUpdatedAt,
waitingFor, …}`, with `status ∈ busy | idle | waiting`. Since 2.1.294 it also
carries `tmux: "<session>:@<window>.%<pane>"`, whose PANE id is the join to a
rack row (rule 3 below says why not the session name).

── WHY THIS, AND NOT THE HOOKS OR THE TERMINAL TITLE ──────────────────────────
Hooks are an event stream with holes in it. Measured on 2.1.294 (2026-10-08),
three leaks:

- **Esc mid-turn fires no hook**, so the row stays `busy` forever. The index
  flips to `idle` the moment the turn is interrupted.
- **Answering a permission prompt fires no hook.** A DENY left the row on
  `attention` until the next prompt; a GRANT left it on `attention` for the
  whole of the work that followed. The index goes `waiting` → `idle` / `busy`.
- **The `Notification` hook lands ~6 s after the prompt appears.** The index
  says `waiting` within ~1 s.

The terminal title was the first candidate (herdr reads a braille spinner off
it). On 2.1.294 it carries NO spinner at all — it is `✳ <topic>` throughout a
generation and a tool run alike — so a "✳ means idle" rule would have flipped
every working agent to free. Rejected by measurement, not by taste.

── WHAT MAKES AN ENTRY BELIEVABLE ──────────────────────────────────────────────
The directory is never cleaned up after a crash: most of its files can
belong to processes long dead. So an entry counts only when

1. its filename pid is ALIVE (`kill(pid, 0)`), and the body agrees on the pid;
2. that pid is still the SAME process — `ps -o lstart=` read in UTC equals the
   entry's `procStart`. A crashed Claude's pid can be recycled by anything, and
   then (1) alone would let a stale `idle` overrule a live agent's hooks. One
   `ps` per live Claude per IDENTITY_CACHE_SECONDS, never per tick;
3. its tmux PANE is live, and no OTHER believable entry lands in the same
   session (two Claudes in one session's panes: which is the row? — neither).
   The row is the session that pane is in NOW (`place_on_panes`), never the
   session the field names: Claude writes `tmux` once, at startup, and never
   again. Measured 2026-10-15 on 2.1.300 — a session renamed through the
   rack's ✎ still reads its birth name, so every correction landed on a ghost
   row while the real row ran on hooks alone. A pane id survives
   `rename-session` (and `move-pane`); a name does not.

Older Claudes write no `tmux` field, so they contribute nothing and those rows
keep today's hook-only behaviour. That is the whole degradation story.

── BACKGROUND WORK: THE FOURTH STATE ──────────────────────────────────────────
A turn can end with work still running — a `run_in_background` Bash job, a
Monitor script, a background subagent. Hooks say `free` (Stop) and herdr, which
reads the screen, says idle; closing the session then kills the work. The
bridge calls this `background`. Measured 2026-10-14 on 2.1.294–2.1.299 (the
binaries themselves, and live sessions):

- **The index already says it for shells.** Its status enum is
  `["busy","shell","idle","waiting"]`, and Claude writes `shell` for "idle, and
  some `local_bash` task is not completed/failed/killed" — Monitor scripts are
  `local_bash` tasks too. A session goes `shell` a fraction of a second after
  its Stop hook while background jobs are alive. Hence `INDEX_STATES["shell"]`.
- **Background agents keep the index `busy`.** Claude reports busy while
  `isLoading || delegatedActive`, and delegatedActive is any live local agent,
  workflow, remote agent or teammate. So after a Stop hook the index stays
  `busy` with a stamp from the turn's START; `state_arbiter.py` reads "busy, but
  a hook already ended the turn" as background.
- **The process tree is the second source and the COUNT.** Every Bash and
  Monitor job is a direct child of the claude pid, wrapped as
  `/bin/zsh -c source <config dir>/shell-snapshots/snapshot-zsh-….sh … && <cmd>`
  until it exits. The match is POSITIVE on that wrapper (`WORK_CHILD_MARKER`),
  never "every child minus caffeinate": the children of an IDLE Claude
  include MCP servers (`uv tool uvx <server>`, alive all session), hook
  scripts (`bash ./.claude/hooks/<hook>.sh`, alive for minutes),
  `statusline.py` and `caffeinate -i -t 300`. None of them is work. A FOREGROUND
  Bash command is the same wrapper, so children count only once the index says
  the turn is over. One `ps -A` per arbiter tick, never one per row.
- **A `shell` the process tree contradicts is not believed.** A subagent may end
  its turn with a Bash job of its own still running: Claude parks it ("waiting
  on its own background work") and keeps the job in its task table. Measured
  2026-11-27 on 2.1.332: that job is a direct child of the claude pid behind the
  same wrapper, like any other. And when Claude misses a job's exit the task is
  never closed, so the index said `shell` for five hours with no work child
  alive, until the parked agent was stopped by hand. Closing that session kills
  nothing, so the row is `free`: `state_arbiter.py` rule 6 owns the rule and its
  guards, which is why an entry carries its Claude `version`.
- **Agents have no pid.** Their count comes from the transcript (`transcript.py`
  `fold_background_agents`, and `fold_background_workflows` for a Workflow run,
  whose launch names a `taskId` and no `agentId`; measured 2.1.319), a label
  only: a resumed agent notifies again under the same id, so the transcript
  never decides the state.

Stdlib only, like the rest of the bridge. The decisions made FROM this live in
`state_arbiter.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping

from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.snapshot import PANE_ID_RE, SESSION_NAME_RE, list_tmux_pane_sessions

#: Claude's status word → the bridge's state vocabulary. Anything else (a
#: future word) contributes nothing. `shell`: see BACKGROUND WORK above.
INDEX_STATES = {"busy": "busy", "idle": "free", "waiting": "attention", "shell": "background"}

#: The states that mean "the turn is over". A hook can only ever report `free`
#: for that; which of the two it is, only the index and the process tree know.
TURN_OVER_STATES = frozenset({"free", "background"})

#: What every Bash/Monitor job's wrapper command contains — the positive match
#: for a work child (BACKGROUND WORK above).
WORK_CHILD_MARKER = "/shell-snapshots/snapshot-"

#: The index lives in this directory under every Claude config dir.
INDEX_DIR_NAME = "sessions"

#: How long a pid's "still the same process" answer is trusted. A pid can only
#: be recycled after its process dies, so a minute of trust costs at most a
#: minute of a dead entry being believed — and the entry file itself is usually
#: gone by then (a clean exit deletes it).
IDENTITY_CACHE_SECONDS = 60.0

#: `ps` is a local process-table read; this is a wedged-system backstop.
PS_TIMEOUT_SECONDS = 2.0

#: A Claude version is a short dotted number; anything longer is not one.
VERSION_MAX = 40

#: `waitingFor` is Claude's own short label ("permission prompt"); it becomes
#: the row's `message`, which notify.py never sends anywhere. Capped anyway.
WAITING_FOR_MAX = 120


@dataclass(frozen=True)
class IndexEntry:
    """One believable index entry, already in the bridge's vocabulary."""

    session: str  # the tmux session it runs in
    state: str  # busy | background | free | attention
    status_at: float  # epoch seconds Claude last changed `status`
    waiting_for: str | None
    agent_session_id: str | None  # the conversation — what `--resume=` takes
    pid: int
    proc_start: str
    pane: str | None = None  # `%3` — the join to the row (rule 3 above)
    root: str = ""  # the config dir it was read under (agents_cli.py reconciles per root)
    version: str = ""  # the Claude that wrote it; "" when the entry names none (state_arbiter.py rule 6)


def tmux_session_of(raw: object) -> str | None:
    """`"vc-3:@2.%2"` → `"vc-3"`, or None for anything that is not that shape.

    A tmux session name cannot contain `:` (tmux rejects it), so the first
    colon always ends it. Re-validated: the field is whatever that file says.
    """
    if not isinstance(raw, str) or ":" not in raw:
        return None
    name = raw.split(":", 1)[0]
    return name if SESSION_NAME_RE.fullmatch(name) else None


def tmux_pane_of(raw: object) -> str | None:
    """`"vc-3:@2.%2"` → `"%2"`, or None for anything that is not that shape.

    The pane id is the LAST `.`-separated part — a session name cannot hold a
    `.` either (tmux rejects it), so nothing earlier can be mistaken for it.
    """
    if not isinstance(raw, str) or ":" not in raw or "." not in raw:
        return None
    pane = raw.rsplit(".", 1)[1]
    return pane if PANE_ID_RE.fullmatch(pane) else None


def parse_index_entry(raw: object, pid: int) -> IndexEntry | None:
    """One `sessions/<pid>.json` body → an entry, or None. Pure.

    `pid` is the FILENAME's — the body must agree, or a copied/planted file
    could borrow a live pid. Times are epoch milliseconds in the file.
    """
    if not isinstance(raw, dict) or raw.get("pid") != pid:
        return None
    state = INDEX_STATES.get(raw.get("status"))
    session = tmux_session_of(raw.get("tmux"))
    proc_start = raw.get("procStart")
    if state is None or session is None or not isinstance(proc_start, str) or not proc_start.strip():
        return None
    stamp = raw.get("statusUpdatedAt", raw.get("updatedAt"))
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    waiting_for = raw.get("waitingFor")
    agent_id = raw.get("sessionId")
    version = raw.get("version")
    return IndexEntry(
        session=session,
        state=state,
        status_at=stamp / 1000.0,
        waiting_for=waiting_for[:WAITING_FOR_MAX] if isinstance(waiting_for, str) and waiting_for else None,
        agent_session_id=agent_id if isinstance(agent_id, str) and AGENT_SESSION_ID_RE.fullmatch(agent_id) else None,
        pid=pid,
        proc_start=" ".join(proc_start.split()),
        pane=tmux_pane_of(raw.get("tmux")),
        version=version if isinstance(version, str) and len(version) <= VERSION_MAX else "",
    )


def place_on_panes(entries: Iterable[IndexEntry], panes: Mapping[str, str]) -> list[IndexEntry]:
    """Each entry moved onto the session its pane is in NOW; an entry whose
    pane is not live in this tmux server is dropped. Pure.

    `panes` is pane id → current session (`snapshot.list_tmux_pane_sessions`).
    An empty map — no server, or tmux did not answer — drops everything: an
    entry that cannot be placed must not be written under the name it was
    born with, which is the ghost-row bug rule 3 exists for.
    """
    placed: list[IndexEntry] = []
    for entry in entries:
        session = panes.get(entry.pane) if entry.pane else None
        if session is not None:
            placed.append(entry if session == entry.session else replace(entry, session=session))
    return placed


def one_per_session(entries: Iterable[IndexEntry]) -> dict[str, IndexEntry]:
    """session → entry, DROPPING any session two entries claim. Pure.

    Two Claudes in one tmux session (split panes) both report to the same row
    through their hooks; the index cannot say which one the row means, so it
    says nothing about that row at all.
    """
    by_session: dict[str, IndexEntry] = {}
    contested: set[str] = set()
    for entry in entries:
        if entry.session in by_session:
            contested.add(entry.session)
        by_session[entry.session] = entry
    for session in contested:
        del by_session[session]
    return by_session


def pid_alive(pid: int) -> bool:
    """Signal 0: the cheapest liveness probe. EPERM means it exists, not ours."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_started(pid: int) -> str | None:
    """`ps -o lstart=` in UTC and the C locale — the exact format Claude writes
    as `procStart` ("Thu Oct  1 09:00:00 2026"), whitespace-normalized."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(result.stdout.split())
    return started if result.returncode == 0 and started else None


def parse_ps_table(text: str) -> list[tuple[int, int, str]]:
    """`ps -o pid=,ppid=,command=` output → `(pid, ppid, command)` rows. Pure.

    The command is the rest of the line and may hold any number of spaces;
    a line that does not start with two integers is skipped, not fatal.
    """
    rows: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[2].strip() if len(parts) > 2 else ""))
    return rows


def work_children(rows: Iterable[tuple[int, int, str]], pids: Iterable[int]) -> dict[int, int]:
    """pid → how many of its DIRECT children are Bash/Monitor jobs. Pure.

    Positive match on `WORK_CHILD_MARKER`; why not "every child minus
    caffeinate" is BACKGROUND WORK in the module header. Every pid asked about
    gets an answer, 0 when it has none (or is not in the table at all).
    """
    counts = dict.fromkeys(pids, 0)
    for _pid, ppid, command in rows:
        if ppid in counts and WORK_CHILD_MARKER in command:
            counts[ppid] += 1
    return counts


def read_process_table() -> str | None:
    """ONE `ps` over the whole machine — the arbiter's per-tick read. `-ww`
    so no command is cut at a terminal width; None when ps fails."""
    try:
        result = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def default_index_roots() -> list[str]:
    """The one Claude config dir a bridge without launch profiles knows about."""
    return [os.path.realpath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))]


class AgentIndexReader:
    """Reads every believable entry under every Claude config dir.

    `roots` is called on each read so a caller can hand over the profile
    registry's live list. The probes are injectable for tests.
    """

    def __init__(
        self,
        roots: Callable[[], Iterable[str]],
        *,
        alive: Callable[[int], bool] = pid_alive,
        started: Callable[[int], str | None] = process_started,
        panes: Callable[[], Mapping[str, str]] = list_tmux_pane_sessions,
        clock: Callable[[], float] = time.monotonic,
        public=None,
    ) -> None:
        # `public`: an agents_cli.PublicStatus (duck-typed: observe / drifted /
        # entries_for). It is fed every read, and a root it has marked DRIFTED
        # is read from it instead of from the files — agents_cli.py owns why.
        self._public = public
        self._roots = roots
        self._alive = alive
        self._started = started
        self._panes = panes
        self._clock = clock
        self._lock = threading.Lock()
        self._identity: dict[tuple[int, str], tuple[float, bool]] = {}

    def read(self) -> dict[str, IndexEntry]:
        by_root = self.read_by_root()
        if self._public is not None:
            self._public.observe(by_root)
            for root in self._public.drifted() & set(by_root):
                by_root[root] = self._public.entries_for(root)
        entries = [entry for found in by_root.values() for entry in found]
        # One tmux read per pass, and only when there is something to place.
        return one_per_session(place_on_panes(entries, self._panes() if entries else {}))

    def read_by_root(self) -> dict[str, list[IndexEntry]]:
        """root → its believable entries, not yet placed on panes."""
        by_root: dict[str, list[IndexEntry]] = {}
        for root in self._roots():
            if root in by_root:
                continue
            by_root[root] = [replace(entry, root=root) for entry in self._read_dir(Path(root) / INDEX_DIR_NAME)]
        return by_root

    def _read_dir(self, directory: Path) -> list[IndexEntry]:
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            return []
        found: list[IndexEntry] = []
        for path in paths:
            if not path.stem.isdigit():
                continue
            pid = int(path.stem)
            # The liveness probe runs BEFORE the read: most files here belong to
            # processes that died long ago, and they cost one syscall, not a parse.
            if not self._alive(pid):
                continue
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                continue  # rewritten mid-read by its running Claude — next tick
            entry = parse_index_entry(raw, pid)
            if entry is not None and self._same_process(pid, entry.proc_start):
                found.append(entry)
        return found

    def still_running(self, entry: IndexEntry) -> bool:
        """Is the process behind a previously believed entry still that process?

        False only on positive evidence — the pid is dead, or it now belongs to
        a process that started at another time. An entry that merely failed to
        read this tick (rewritten mid-read) is still running.
        """
        return self._alive(entry.pid) and self._same_process(entry.pid, entry.proc_start)

    def _same_process(self, pid: int, proc_start: str) -> bool:
        now = self._clock()
        key = (pid, proc_start)
        with self._lock:
            cached = self._identity.get(key)
            if cached is not None and now - cached[0] < IDENTITY_CACHE_SECONDS:
                return cached[1]
        started = self._started(pid)
        verdict = started is not None and " ".join(started.split()) == proc_start
        with self._lock:
            # Bounded by the number of live Claudes: drop answers that aged out.
            self._identity = {k: v for k, v in self._identity.items() if now - v[0] < IDENTITY_CACHE_SECONDS}
            self._identity[key] = (now, verdict)
        return verdict
