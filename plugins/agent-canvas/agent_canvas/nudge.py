"""The just-in-time canvas reminder: when an agent CREATES structure, say once that its canvas exists.

── WHY AT THE WRITE, NOT AT SESSION START ─────────────────────────────────
A line read once at SessionStart is forgotten by the turn that matters
(README.md § Getting agents to draw at all). This reminder lands
the moment the structure appears, as `additionalContext` on that PostToolUse,
so it adds no turn; the agent still decides whether anything is worth drawing.

── WHAT COUNTS ────────────────────────────────────────────────────────────
Only a Write that CREATED a file (`tool_response.type == "create"`), inside the
session's own tree, from the main thread (a subagent's turn is not the one the
user reads), and one of:
  - the first file in its directory: the Write just made the directory;
  - a launchd job or a systemd unit, a rack plugin's entry, a route module (a
    file directly in a `router`, `routers` or `routes` directory): a new
    process, plugin or route, however familiar the directory.
Docs, generated files, fixtures and caches never count (`QUIET_PARTS`).

── HOW OFTEN ──────────────────────────────────────────────────────────────
At most once per `COOLDOWN_SECONDS` per canvas, and never while the canvas was
drawn within that window: an agent already drawing needs no reminder, and a
burst of new directories is one build, not twenty. In memory, like the desk: a
restart can repeat one reminder, which is harmless.

The hook is `hooks/nudge.sh` beside this package, a dumb relay; every rule is
here. The user-initiated half is `draw.py`.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from collections.abc import Callable

from agent_canvas.paths import CANVAS_SUFFIXES, canvas_path

COOLDOWN_SECONDS = 20 * 60

#: A path with any of these parts never nudges: prose, build output, caches.
QUIET_PARTS = frozenset(
    {
        "docs",
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "dist",
        "generated",
        "fixtures",
        ".cache",
        ".pytest_cache",
    }
)

#: A directory whose source files are routes, in the frameworks that name it so.
ROUTE_DIRS = frozenset({"router", "routers", "routes"})
ROUTE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".mjs", ".py", ".go", ".rb", ".rs", ".php"})


def _kind_of(rel: Path) -> tuple[str, str] | None:
    """`(kind, phrase)` for a file that names a new process, plugin or route wherever it lands."""
    if rel.suffix == ".plist":
        return "launchd", "a launchd job"
    if rel.suffix in (".service", ".timer"):
        return "systemd", "a systemd unit"
    if rel.name == "bridge_plugin.py":
        return "plugin", "a rack plugin"
    if rel.parent.name in ROUTE_DIRS and rel.suffix in ROUTE_SUFFIXES:
        return "route", "a route module"
    return None


def created_structure(payload: object, listdir: Callable[[str], list[str]] = os.listdir) -> str | None:
    """What this hook payload created, as the reminder's phrase — or None."""
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Write":
        return None
    if payload.get("agent_id"):
        return None
    response, tool_input, cwd = payload.get("tool_response"), payload.get("tool_input"), payload.get("cwd")
    if not isinstance(response, dict) or response.get("type") != "create":
        return None
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not os.path.isabs(raw):
        return None
    path, root = Path(os.path.normpath(raw)), Path(os.path.normpath(cwd))
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if not rel.parts or QUIET_PARTS.intersection(rel.parts):
        return None
    kind = _kind_of(rel)
    if kind is not None:
        return f"{kind[1]}, {rel.as_posix()}"
    if rel.parent == Path("."):
        return None
    try:
        entries = listdir(str(path.parent))
    except OSError:
        return None
    return f"a new directory, {rel.parent.as_posix()}/" if entries == [path.name] else None


def reminder(what: str, key: str) -> str:
    return (
        f"agent canvas: you just created {what}. The user takes in structure faster as a picture "
        f"than as prose, so before your final reply this turn, draw what you built on "
        f"{canvas_path(key, '.json')} (a mermaid flowchart of the new layout, or of who starts what; "
        "the agent-canvas skill has the blocks). Skip it if this is throwaway scaffolding."
    )


def hook_output(text: str) -> dict:
    """The PostToolUse JSON Claude Code reads off the hook's stdout."""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def last_drawn(key: str) -> float | None:
    """When this canvas was last written, either form; None when never."""
    times = []
    for suffix in CANVAS_SUFFIXES:
        try:
            times.append(canvas_path(key, suffix).stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


class Nudger:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        listdir: Callable[[str], list[str]] = os.listdir,
        drawn_at: Callable[[str], float | None] = last_drawn,
    ) -> None:
        self._clock = clock
        self._listdir = listdir
        self._drawn_at = drawn_at
        self._lock = threading.Lock()
        self._nudged: dict[str, float] = {}  # canvas key → when last reminded

    def consider(self, key: str, payload: object) -> dict | None:
        """The hook output for this payload, or None to stay quiet."""
        what = created_structure(payload, self._listdir)
        if what is None:
            return None
        now = self._clock()
        drawn = self._drawn_at(key)
        if drawn is not None and now - drawn < COOLDOWN_SECONDS:
            return None
        with self._lock:
            last = self._nudged.get(key)
            if last is not None and now - last < COOLDOWN_SECONDS:
                return None
            self._nudged[key] = now
        return hook_output(reminder(what, key))
