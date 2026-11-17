"""`POST /open` — reveal a skill's folder in Finder.

This is the one route that acts on the machine rather than reporting about it,
so the path check is the security boundary of the whole plugin. Two independent
gates stand in front of it:

  1. The host's Origin guard, applied to every plugin POST before dispatch —
     plugins cannot weaken it (voidcraft_world_bridge/plugins.py, the bridge's public core).
  2. `resolve_reveal_target()` below: the target must resolve INSIDE one of the
     scan roots. `Path.resolve()` runs first, so a symlink planted inside a root
     that points at `/etc` resolves to `/etc` and is rejected on its real
     location, not its apparent one — and `..` traversal collapses for free.

`open -R` only ever selects the item in Finder; it does not execute it. The
argument is passed as an argv element (never a shell string), so a path with
spaces, quotes or semicolons is inert.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

OPEN_TIMEOUT_SECONDS = 5


def resolve_reveal_target(raw: object, allowlist: list[Path]) -> tuple[Path | None, str | None]:
    """PURE-ish — validate a requested reveal path against the allowed roots.

    Returns `(path, None)` when the target is safe to reveal, else
    `(None, reason)`. Touches the filesystem only to resolve symlinks and to
    confirm the target exists — deliberately, since resolution IS the check.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "missing 'path'"
    try:
        target = Path(raw).expanduser().resolve()
        exists = target.exists()
    # ValueError is the one that bites: an embedded NUL byte raises it from
    # os.lstat deep inside resolve(), so it must be caught alongside OSError
    # or a crafted body turns a 403 into an unhandled 500.
    except (OSError, RuntimeError, ValueError):
        return None, "unresolvable path"
    if not exists:
        return None, "path does not exist"
    for root in allowlist:
        try:
            resolved_root = root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue  # a malformed root must not abort the whole check
        if target == resolved_root or target.is_relative_to(resolved_root):
            return target, None
    return None, "path is outside the Claude skill directories"


def reveal(target: Path) -> tuple[int, dict]:
    """Select `target` in Finder. macOS only; anything else reports 501."""
    if sys.platform != "darwin":
        return 501, {"error": f"reveal is macOS-only (platform: {sys.platform})"}
    try:
        result = subprocess.run(
            ["open", "-R", str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=OPEN_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return 503, {"error": "`open` not available"}
    except subprocess.TimeoutExpired:
        return 504, {"error": "`open` timed out"}
    if result.returncode != 0:
        return 500, {"error": (result.stderr or "open failed").strip()}
    return 200, {"ok": True, "revealed": str(target)}


def handle_open(body: dict | None, allowlist: list[Path]) -> tuple[int, dict]:
    """The full `POST /open` decision: validate, then reveal."""
    target, reason = resolve_reveal_target((body or {}).get("path"), allowlist)
    if target is None:
        return 403, {"error": reason}
    return reveal(target)
