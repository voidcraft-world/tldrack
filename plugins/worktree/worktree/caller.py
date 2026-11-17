"""Resolve the launching agent's pane in the caller process, never the server."""

import os
import re
import subprocess


def caller_session() -> str | None:
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%[0-9]+", pane):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = result.stdout.strip()
    return name if result.returncode == 0 and re.fullmatch(r"[A-Za-z0-9_-]+", name) else None
