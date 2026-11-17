"""Every tmux call this plugin makes, bounded.

Each runs on a bridge request thread or the watcher thread, so each carries a
timeout: a wedged tmux server (a stale socket is the classic one) would
otherwise hang the thread forever.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_canvas.paths import CANVAS_KEY_FORMAT, normalize_key

PROBE_TIMEOUT_SECONDS = 4

_SEP = "\x1f"  # unit separator — a session name cannot contain it


def run_bounded(command: list[str], timeout: int = PROBE_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Bounded `subprocess.run` → `(returncode, stdout, stderr)`; `-1` on a
    timeout or OSError. Copied from plugins/worktree/worktree/create.py
    `run_bounded` — see the note there. Tests fence THIS module attribute."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except OSError as exc:
        return -1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def pane_target(session: str) -> str:
    """Exact-match pane target. The trailing colon is required — see
    plugins/worktree/worktree/create.py `pane_target`."""
    return f"={session}:"


def sessions_by_key() -> dict[str, str]:
    """`{canvas key: session name}` for every live tmux session (one call)."""
    code, out, _err = run_bounded(["tmux", "list-sessions", "-F", f"{CANVAS_KEY_FORMAT}{_SEP}#{{session_name}}"])
    if code != 0:
        return {}
    found: dict[str, str] = {}
    for line in out.splitlines():
        raw_key, _, name = line.partition(_SEP)
        key = normalize_key(raw_key)
        if key and name:
            found[key] = name
    return found


def key_for_session(session: str) -> str | None:
    code, out, _err = run_bounded(["tmux", "display-message", "-p", "-t", pane_target(session), CANVAS_KEY_FORMAT])
    return normalize_key(out) if code == 0 else None


def workspace_for_session(session: str) -> Path | None:
    """Use tmux's starting directory, not a browser-supplied workspace."""
    code, out, _err = run_bounded(["tmux", "display-message", "-p", "-t", pane_target(session), "#{session_path}"])
    return Path(out.strip()) if code == 0 and out.strip() else None


def capture(session: str) -> str | None:
    """The pane's visible rows, or None when the session is gone."""
    code, out, _err = run_bounded(["tmux", "capture-pane", "-p", "-t", pane_target(session)])
    return out if code == 0 else None


def type_line(session: str, line: str) -> bool:
    """Type one line, then Enter — two calls, because `-l` would type `Enter`
    as five letters. `--` ends send-keys' options so a leading `-` is text."""
    for command in (
        ["tmux", "send-keys", "-t", pane_target(session), "-l", "--", line],
        ["tmux", "send-keys", "-t", pane_target(session), "Enter"],
    ):
        code, _out, _err = run_bounded(command)
        if code != 0:
            return False
    return True
