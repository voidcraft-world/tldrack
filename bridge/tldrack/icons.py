"""Per-session icon picks, shared by every client of the bridge.

A native client and the web UI both let the user pick an icon for
a tmux session; the bridge is the single store so a pick made on either surface
shows up on all of them (a dock or menu, the agent rack, agent bot).
Canonical icon ids are the SF Symbol names of the native client's 16-entry
catalog; the web UI maps them to MDI glyphs.

The bridge stores only EXPLICIT picks and validates ids by token shape, not
catalog membership — the catalog lives in the clients, so shipping a new icon
must never require a bridge upgrade. A client that doesn't recognize a stored
id falls back to its hash-computed default (both clients derive the same
FNV-1a default per session, so "no pick" also renders identically everywhere).

Unlike the StatusStore, picks survive restarts: they live in a JSON file
(session-icons.json under ~/.local/state/tldrack/, overridable via
TLDRACK_ICONS_PATH for tests). Entries are never pruned when a session
dies — a recreated session with the same name keeps its icon, matching the mac
app's original UserDefaults semantics. Unbounded growth is accepted: one short
string per session name ever customized.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tldrack import paths
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE

# Shape-only guard for stored icon ids (see module docstring for why membership
# in the catalog is deliberately NOT checked). Covers SF Symbol names, which use
# lowercase dotted words like "wrench.and.screwdriver".
ICON_TOKEN_RE = re.compile(r"[a-z0-9.-]{1,64}")

ICON_FILE_VERSION = 1

ICONS_FILE = "session-icons.json"


def resolve_icons_path() -> Path:
    env_path = os.environ.get("TLDRACK_ICONS_PATH")
    return Path(env_path) if env_path else paths.state_file(ICONS_FILE)


def parse_icon_file(text: str) -> dict[str, str]:
    """Parse session-icons.json text into a session → icon-id map.

    Raises ValueError when the document isn't the expected envelope (not JSON,
    not an object, or "icons" not an object) so the caller can warn about a
    corrupt file; individually malformed ENTRIES are silently dropped instead —
    one bad row must not cost the user every other pick.
    """
    payload = json.loads(text)  # ValueError on non-JSON
    if not isinstance(payload, dict) or not isinstance(payload.get("icons"), dict):
        raise ValueError('expected {"version": ..., "icons": {...}}')
    return {
        session: icon
        for session, icon in payload["icons"].items()
        if isinstance(session, str)
        and isinstance(icon, str)
        and SESSION_NAME_RE.fullmatch(session)
        and ICON_TOKEN_RE.fullmatch(icon)
    }


def serialize_icon_file(icons: dict[str, str]) -> str:
    """Stable (sorted, indented) file body — the file is user-inspectable state."""
    return json.dumps({"version": ICON_FILE_VERSION, "icons": dict(sorted(icons.items()))}, indent=2) + "\n"


class SessionIconStore(SessionStateFile[str]):
    """File-persisted per-session explicit icon picks.

    The file, the lock, the tolerant load and `rename` are owned by
    `session_state_file.py` — see the note there. Writes are rare (a user
    picking an icon), so holding the lock across the file write is fine.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_icon_file,
            serialize=serialize_icon_file,
            noun="pick",
            plural="session icons",
            file_stem="session-icons",
        )

    def get_all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._entries)

    def set(self, session: str, icon: str | None) -> None:
        """Record (icon id) or clear (None) a session's explicit pick, and persist."""
        with self._lock:
            if icon is None:
                self._entries.pop(session, None)
            else:
                self._entries[session] = icon
            self._persist_locked()
