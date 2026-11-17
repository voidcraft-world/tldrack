"""Finding the `claude` CLI — shared by the deep pass and by RESUME.

It lives on its own because two modules with very different jobs both have to
answer the same awkward question, and answering it twice would mean two places
to fix when the installer moves.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_claude_bin() -> str | None:
    """The `claude` CLI, or None when it cannot be found.

    PATH is checked first, but the bridge is often launched from a GUI
    app, whose environment does not include `~/.local/bin` — where the installer
    actually puts it — so that location is a deliberate fallback rather than an
    assumption baked into the call.
    """
    override = os.environ.get("COORDINATOR_CLAUDE_BIN")
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None
