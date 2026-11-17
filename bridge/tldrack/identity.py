"""Bridge self-identity: who launched this process, when, and from where.

Answers the "I can't find the window running tldrack" problem. At startup
main.py calls `capture_identity()` once and serves the result under the `bridge`
key of `GET /snapshot`, so the SPA, a native client, and `tldrack status`
can all say things like "pid 41320, up 2h 14m, launched by tmux".

Everything OS-facing shells out to `ps` (stdlib only — no psutil). The parsing
and classification are pure functions with injectable readers so they unit-test
without a real process table. macOS `ps -o comm=` prints the *full executable
path* (which can contain spaces, e.g. /Applications/iTerm.app/.../iTerm2), so
lines are split once from the left and classification matches on basenames.
"""

from __future__ import annotations

import os
import subprocess
import time

# Wrappers and shells between the bridge and whatever actually launched it —
# skipped when classifying so `zsh → login → iTerm2` reads as iTerm2. Python is
# here because the bridge itself runs as `uv run` → python.
SKIP_COMMS = {"zsh", "bash", "sh", "fish", "dash", "login", "script", "sudo", "uv"}

# comm basenames that indicate a headless AI agent / script runner. Checked
# before terminal emulators: nearest-first order in the chain already prefers
# them, and an agent running inside an iTerm tab should still read as "agent".
AGENT_COMMS = {"node", "claude", "bun", "deno"}

#: The `kind` of a bridge started by a supervisor app. The value is a wire name clients already
#: read, so it stays; which processes ARE a supervisor is the operator's to say (settings.py
#: `supervisor_process`), and with none named nothing is ever classified as one.
SUPERVISED_KIND = "menubar"


def parse_ps_line(line: str) -> tuple[int, str] | None:
    """Parse one `ps -o ppid=,comm=` output line into (ppid, comm), or None.

    comm is the remainder after the first whitespace run — on macOS it's a full
    path that may itself contain spaces, so only the leading pid is split off.
    """
    parts = line.strip().split(None, 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


def read_process(pid: int) -> tuple[int, str] | None:
    """(ppid, comm) for a live pid via `ps`, or None if it's gone."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return parse_ps_line(result.stdout)


def own_tty(pid: int) -> str | None:
    """Controlling tty name ("ttys003") or None for headless ("??").

    Asks `ps` rather than os.ttyname(0): a supervisor-spawned bridge has stdio
    redirected to a log file, but ps still knows there's no controlling tty.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    tty = result.stdout.strip()
    return None if not tty or tty == "??" else tty


def walk_parent_chain(start_pid: int, read=read_process, max_depth: int = 15) -> list[dict]:
    """Ancestry from start_pid upward: [{"pid", "comm"}, ...], full comm paths kept.

    Stops at pid <= 1 (launchd is included as the final row when reached), on a
    reader miss (process died mid-walk), on a pid cycle, or at max_depth.
    """
    chain: list[dict] = []
    seen: set[int] = set()
    pid = start_pid
    while pid > 1 and pid not in seen and len(chain) < max_depth:
        seen.add(pid)
        info = read(pid)
        if info is None:
            break
        ppid, comm = info
        chain.append({"pid": pid, "comm": comm})
        pid = ppid
    if pid == 1 and len(chain) < max_depth:
        chain.append({"pid": 1, "comm": "launchd"})
    return chain


def classify_launcher(parent_chain: list[dict], tty: str | None, supervisor_comms: frozenset[str] = frozenset()) -> dict:
    """Nearest-first launcher classification → {"kind", "label"}.

    Walks the chain from the immediate parent outward, skipping shells/wrappers
    (SKIP_COMMS, python*), and returns the first recognizable launcher. Kinds:
    agent | menubar (`SUPERVISED_KIND`) | tmux | iterm2 | apple-terminal | launchd | headless.
    `supervisor_comms` are the process names the operator named as their supervisor.
    """
    tty_hint = f" (tty {tty})" if tty else ""
    first_other: str | None = None
    for row in parent_chain:
        comm = row["comm"]
        # Login shells report with a leading dash ("-zsh") — strip it so they
        # hit the skip set. Verified against a live iTerm parent chain.
        base = os.path.basename(comm).lstrip("-")
        if base in SKIP_COMMS or base.lower().startswith("python"):
            continue
        if base in AGENT_COMMS:
            return {"kind": "agent", "label": f"headless agent ({base})"}
        if base in supervisor_comms:
            return {"kind": SUPERVISED_KIND, "label": f"a supervisor app ({base})"}
        if base == "tmux" or base.startswith("tmux:"):
            return {"kind": "tmux", "label": "inside tmux (a pane hosts this bridge)"}
        # iTerm shells hang off "iTermServer-<version>", not iTerm2 directly.
        if base == "iTerm2" or base.startswith("iTermServer"):
            return {"kind": "iterm2", "label": f"iTerm2{tty_hint}"}
        if base == "Terminal" or "Terminal.app" in comm:
            return {"kind": "apple-terminal", "label": f"Apple Terminal{tty_hint}"}
        if base == "launchd" or row["pid"] == 1:
            break
        if first_other is None:
            first_other = base
    if first_other is not None:
        return {"kind": "headless", "label": f"headless, parent: {first_other}"}
    return {"kind": "launchd", "label": "launchd (login item or daemon)"}


def capture_identity(
    mode: str, port: int, status_port: int, ttyd_pid: int | None, ttyd_owned: bool, supervisor_comms: frozenset[str] = frozenset()
) -> dict:
    """One-shot identity snapshot of THIS bridge process (the `bridge` object).

    `uptime_seconds` is intentionally absent — the /snapshot handler computes it
    per request from `started_at`.
    """
    pid = os.getpid()
    tty = own_tty(pid)
    chain = walk_parent_chain(os.getppid())
    return {
        "pid": pid,
        "started_at": time.time(),
        "cwd": os.getcwd(),
        "tty": tty,
        "mode": mode,
        "port": port,
        "status_port": status_port,
        "parent_chain": [{"pid": row["pid"], "comm": os.path.basename(row["comm"])} for row in chain],
        "launched_by": classify_launcher(chain, tty, supervisor_comms),
        "ttyd": {"pid": ttyd_pid, "owned": ttyd_owned},
    }


def format_uptime(seconds: float) -> str:
    """Human uptime: "45s", "3m 20s", "2h 14m", "1d 3h"."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"
