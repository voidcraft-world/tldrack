"""Which agent is holding the memory: this machine's RSS, folded onto its tmux sessions.

The rack's RAM meter answers "how full is this machine". On a machine every agent shares,
the next question is "who", and the total cannot say. A session's tree is its pane's
process and every descendant: the CLI, its shells, its MCP servers.

── THE NUMBER ──────────────────────────────────────────────────────────────────
`ps` CPU percent and RSS, summed. CPU is the OS process average (100% = one
core), not an instantaneous interval measurement. Adding its column to the same
process listing costs no extra subprocess or sampling loop. RSS double-counts pages
children share with a parent, and macOS does
not report compressed pages in it, so it is a RANKING and not an accounting: it says
which session to look at, and the rows will not add up to the RAM meter.

── THE COST ────────────────────────────────────────────────────────────────────
One `ps` and one `tmux` fork, on the sampler's own thread, every
`SESSIONS_EVERY_SAMPLES` samples, and only while DEVICES is open (`sampler.py`). That
is why this is not on the 2 s path the Mach calls take, and why a GET never runs it.

── AN ORPHAN IS AN AGENT PROCESS NOBODY CAN REACH ──────────────────────────────
A process launchd (or init) adopted (ppid 1) that is an agent CLI or an MCP server, plus
its descendants. No rack row owns it, so its memory is otherwise invisible. ppid 1 also
holds long-lived daemons that are NOT strays: the tmux server itself, Codex's shared
`app-server` daemon, and any MCP server the operator runs as a service. The first two are
excluded by name here; the operator names the rest in the `daemons` setting (README
§ Settings), which arrives as `read_session_memory`'s `daemons`.

The command line is read for that test and never leaves this module: it can carry a
token, and the route answers any client the bridge's guards admit.
"""

from __future__ import annotations

import os
import math
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

COMMAND_TIMEOUT_SECONDS = 3
#: tmux's words for "nothing is running", which is an answer rather than a failure.
_TMUX_NOT_RUNNING = ("no server running", "error connecting")
#: How a process is adopted once its parent is gone.
LAUNCHD_PID = 1
#: The rows a poll carries. The rest are counted, so a cut is never silent.
MAX_SESSIONS = 32

_AGENT_BINARIES = frozenset({"claude", "codex", "agy"})
#: Daemons every machine running these agents may have; the operator's own are `daemons`.
_NEVER_ORPHANS = ("app-server",)
_MCP_RE = re.compile(r"(^|[/\s_-])mcp([/\s_.-]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    rss_kb: int
    command: str
    cpu_pct: float | None = None


def parse_ps(text: str, *, cpu_column: bool = False) -> dict[int, Process]:
    """Read pid, ppid, RSS, optional CPU percent, then command. Malformed rows are skipped."""
    processes = {}
    for line in text.splitlines():
        fields = line.split(None, 4 if cpu_column else 3)
        if len(fields) < 3:
            continue
        try:
            pid, ppid, rss = int(fields[0]), int(fields[1]), int(fields[2])
        except ValueError:
            continue
        cpu = None
        if cpu_column:
            try:
                cpu = float(fields[3])
            except (ValueError, IndexError):
                continue
            if not math.isfinite(cpu) or cpu < 0:
                cpu = None
        command_index = 4 if cpu_column else 3
        processes[pid] = Process(pid, ppid, rss, fields[command_index] if len(fields) > command_index else "", cpu)
    return processes


def parse_panes(text: str) -> dict[int, str]:
    """`tmux list-panes -a -F '#{session_name} #{pane_pid}'` → pane pid → session.
    Split from the right: a session made by hand can hold a space."""
    panes = {}
    for line in text.splitlines():
        name, _, pid = line.rpartition(" ")
        if name and pid.isdigit():
            panes[int(pid)] = name
    return panes


def _is_orphan_root(process: Process, daemons: tuple[str, ...] = ()) -> bool:
    if process.ppid != LAUNCHD_PID or not process.command:
        return False
    binary = os.path.basename(process.command.split(None, 1)[0])
    if binary == "tmux" or any(marker in process.command for marker in (*_NEVER_ORPHANS, *daemons)):
        return False
    return binary in _AGENT_BINARIES or _MCP_RE.search(process.command) is not None


def fold_session_memory(processes: dict[int, Process], panes: dict[int, str], daemons: tuple[str, ...] = ()) -> dict:
    """`{sessions: [{session, rss, procs}], more, orphans: {rss, procs}}`, bytes, largest first.
    `daemons`: command-line substrings never counted as orphans (module header)."""
    owners: dict[int, str | None] = {}
    orphan_roots: set[int] = set()

    def walk(start: int) -> None:
        """Up to the nearest pane or to the top, memoised along the way. `seen` stops a
        cycle, which a pid recycled between `ps` reading two lines could fake."""
        path = []
        seen = set()
        current: int | None = start
        answer: str | None = None
        while current is not None and current not in seen:
            if current in owners:
                answer = owners[current]
                break
            if current in panes:
                answer = panes[current]
                break
            seen.add(current)
            path.append(current)
            process = processes.get(current)
            if process is None:
                break
            if _is_orphan_root(process, daemons):
                orphan_roots.add(current)
                answer = f"\0{current}"
                break
            current = process.ppid
        for pid in path:
            owners[pid] = answer

    totals: dict[str, list[int]] = {}
    orphans = [0, 0]
    cpu_totals: dict[str, float] = {}
    cpu_missing: set[str] = set()
    for pid, process in processes.items():
        if pid not in owners:
            walk(pid)
        owner = panes.get(pid) or owners.get(pid)
        if owner is None:
            continue
        slot = orphans if owner.startswith("\0") else totals.setdefault(owner, [0, 0])
        slot[0] += process.rss_kb * 1024
        slot[1] += 1
        if process.cpu_pct is None:
            cpu_missing.add(owner)
        else:
            cpu_totals[owner] = cpu_totals.get(owner, 0) + process.cpu_pct
    ranked = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    return {
        "sessions": [
            {
                "session": name,
                "rss": rss,
                "procs": procs,
                **({"cpu_pct": round(cpu_totals[name], 1)} if name not in cpu_missing else {}),
            }
            for name, (rss, procs) in ranked[:MAX_SESSIONS]
        ],
        "more": max(0, len(ranked) - MAX_SESSIONS),
        "orphans": {"rss": orphans[0], "procs": orphans[1]},
    }


def _run(argv: list[str]) -> str:
    """stdout, or a raise: an empty string from a failed command would read as an empty machine."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False, errors="replace")
    if result.returncode != 0 and not (argv[0] == "tmux" and any(words in result.stderr for words in _TMUX_NOT_RUNNING)):
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result.stdout


def read_session_memory(run: Callable[[list[str]], str] = _run, *, daemons: tuple[str, ...] = ()) -> dict:
    """One sample. Raises OSError or a SubprocessError, which the sampler reads as 'not this tick'."""
    processes = parse_ps(run(["ps", "-Ao", "pid=,ppid=,rss=,%cpu=,command="]), cpu_column=True)
    panes = parse_panes(run(["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"]))
    return fold_session_memory(processes, panes, daemons)
