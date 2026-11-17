"""Where canvases live, and how one is named.

── WHY A CANVAS IS KEYED BY TMUX SESSION ID, NOT BY NAME ──────────────────
The rack renames sessions with a real `tmux rename-session`, and an agent
learns its canvas path ONCE, at SessionStart. Keyed by name, a rename would
leave the agent writing to a file no one reads any more. tmux's `session_id`
(`$37`) survives a rename; `session_created` is added because ids restart at
`$0` with every tmux server, so a fresh server's `$3` would otherwise open the
previous server's `$3` canvas. Together they name one session, once, forever.

── WHERE THE DIRECTORY IS ─────────────────────────────────────────────────
Under the bridge's data directory, because a canvas is the agent's output and
the shelf keeps copies the operator may still open. The bridge exports its
resolved data directory as `TLDRACK_DATA_DIR`, so a moved data directory moves
the canvases with it; `AGENT_CANVAS_DIR` overrides the folder alone (tests, a
second layout). Only this process resolves it: the hooks ask the plugin
(`session_start.py` owns why).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Overrides the canvas directory (tests; a second machine layout).
CANVAS_DIR_ENV = "AGENT_CANVAS_DIR"
#: The bridge's data directory, as the bridge exports it.
DATA_DIR_ENV = "TLDRACK_DATA_DIR"
DEFAULT_DATA_DIR = "~/.local/share/tldrack"
CANVAS_FOLDER = "canvas"

#: The tmux format a key is built from (`tmux.py`). `$` is stripped from the id
#: so the key is a plain filename token.
CANVAS_KEY_FORMAT = "#{session_id}-#{session_created}"

#: `37-1760630000` — what a key looks like after the `$` is stripped.
CANVAS_KEY_RE = re.compile(r"[0-9]{1,6}-[0-9]{1,12}")

#: A tmux session name as the bridge validates it (`snapshot.SESSION_NAME_RE`).
#: Plugins cannot import the host, so this is a copy; the host's is the original.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: A canvas is a small pane, not a document store. Past this the file is read
#: up to the cap and the page says it was cut.
MAX_FRAGMENT_BYTES = 256 * 1024


def canvas_dir() -> Path:
    """`AGENT_CANVAS_DIR`, else `<TLDRACK_DATA_DIR>/canvas`, else `~/.local/share/tldrack/canvas`."""
    override = os.environ.get(CANVAS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR).expanduser() / CANVAS_FOLDER


def ensure_canvas_dir() -> Path:
    """The directory, created private to this user when missing. A canvas is
    whatever an agent chose to show, so nobody else on the machine reads it."""
    path = canvas_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def normalize_key(raw: object) -> str | None:
    """`$37-1760630000` or `37-1760630000` → `37-1760630000`; anything else → None."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().replace("$", "", 1)
    return key if CANVAS_KEY_RE.fullmatch(key) else None


#: The two forms a canvas takes: agent-written body markup, or a typed doc
#: (`doc.py`; vocabulary owned by schemas/src/agent-canvas.ts).
CANVAS_SUFFIXES = (".html", ".json")


def canvas_path(key: str, suffix: str = ".html") -> Path:
    """The canvas file for a VALIDATED key. The regex is the containment: a key
    is digits and one dash, so it cannot name anything outside `canvas_dir()`."""
    if not CANVAS_KEY_RE.fullmatch(key) or suffix not in CANVAS_SUFFIXES:
        raise ValueError(f"not a canvas: {key!r}{suffix!r}")
    return canvas_dir() / f"{key}{suffix}"


def read_canvas(key: str) -> tuple[str, str, str, bool] | None:
    """`(suffix, text, rev, truncated)` for whichever form was written LAST.

    Newest wins rather than "JSON wins": an agent that switches back to markup
    would otherwise be shadowed forever by a JSON file it no longer writes.
    """
    found = [(suffix, read) for suffix in CANVAS_SUFFIXES if (read := read_fragment(canvas_path(key, suffix))) is not None]
    if not found:
        return None
    suffix, (text, rev, truncated) = max(found, key=lambda item: (int(item[1][1]), item[0] == ".json"))
    return suffix, text, rev, truncated


def read_fragment(path: Path) -> tuple[str, str, bool] | None:
    """`(text, rev, truncated)`, or None when there is no canvas yet.

    `rev` is the file's `mtime_ns` as a string: an answer names the rev it
    answers, so a canvas rewritten since the page loaded cannot be answered
    with the old question's buttons.

    Refuses a symlink: the directory is the agent's to write, and a link would
    let a canvas serve any file this user can read to whatever renders it.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        with path.open("rb") as handle:
            raw = handle.read(MAX_FRAGMENT_BYTES + 1)
    except OSError:
        return None
    truncated = len(raw) > MAX_FRAGMENT_BYTES
    text = raw[:MAX_FRAGMENT_BYTES].decode("utf-8", errors="replace")
    return text, str(stat.st_mtime_ns), truncated
