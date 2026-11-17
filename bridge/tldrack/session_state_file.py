"""A session -> value map on disk: tolerant load, locked reads, atomic writes.

The shape `icons.py` (explicit icon picks) and `models.py` (last-known model +
usage) both need, and both used to carry their own copy of. `atomic_store.py`
owns the WRITE; this owns everything wrapped around it — which file, which lock,
what happens when the file is missing, unreadable or corrupt, and what a
`tmux rename-session` does to a stored entry.

── A CORRUPT FILE IS IGNORED, NEVER RE-PERSISTED ────────────────────────────
A document that fails to parse leaves the store empty in memory and the file
untouched on disk. Writing an empty map back would clobber a file the user might
want to hand-repair, and the whole reason `atomic_write` exists is that this
loader discards what it cannot parse.

── RENAME IS NOT OPTIONAL ───────────────────────────────────────────────────
Without it a `tmux rename-session` silently drops the stored value back to the
default — the entry would still be in the file, but under a name no session has
any more, so it stays dormant forever. The moved entry REPLACES anything under
the new name: the rename already happened in tmux, so a value sitting on the new
key belonged to a session that no longer exists under it.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Generic, TypeVar
from collections.abc import Callable

from tldrack.atomic_store import atomic_write
from tldrack.snapshot import SESSION_NAME_RE

V = TypeVar("V")


def parse_session_map(text: str, clean: Callable[[object], V | None]) -> dict[str, V]:
    """`{"version": …, "sessions": {name: entry}}` → name → cleaned entry. ValueError on a wrong
    envelope; an entry `clean` refuses, or one under a name tmux could never hold, is dropped ALONE."""
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), dict):
        raise ValueError('expected {"version": ..., "sessions": {...}}')
    parsed: dict[str, V] = {}
    for session, entry in payload["sessions"].items():
        cleaned = clean(entry)
        if isinstance(session, str) and SESSION_NAME_RE.fullmatch(session) and cleaned is not None:
            parsed[session] = cleaned
    return parsed


def serialize_session_map(sessions: dict[str, V], version: int) -> str:
    """The same envelope, sessions sorted, indented: a file a human may hand-repair."""
    body = {"version": version, "sessions": {name: sessions[name] for name in sorted(sessions)}}
    return json.dumps(body, indent=2) + "\n"


class SessionStateFile(Generic[V]):
    """File-persisted `session -> V` map.

    Handler threads race (ThreadingHTTPServer), so reads copy and writes persist
    under one lock. Subclasses own the accessors — what a read hands back and
    when a write is worth making differ per store — and reach the map through
    `self._entries` while holding `self._lock`.

    `plural` and `file_stem` only shape the two warnings ("starting with no
    <plural>", "corrupt <file_stem> file"); `noun` names what stays in memory
    when a write fails and is handed straight to `atomic_write`.
    """

    def __init__(
        self,
        path: Path,
        *,
        parse: Callable[[str], dict[str, V]],
        serialize: Callable[[dict[str, V]], str],
        noun: str,
        plural: str,
        file_stem: str,
    ) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._serialize = serialize
        self._noun = noun
        self._entries: dict[str, V] = {}
        try:
            text = path.read_text()
        except FileNotFoundError:
            return  # first run — created on the first write
        except OSError as err:
            print(f"tldrack: could not read {path} ({err}) — starting with no {plural}", file=sys.stderr)
            return
        try:
            self._entries = parse(text)
        except ValueError as err:
            print(f"tldrack: corrupt {file_stem} file {path} ({err}) — ignoring it", file=sys.stderr)

    def rename(self, old: str, new: str) -> bool:
        """Move an entry onto a renamed session; True if there was one to move."""
        with self._lock:
            entry = self._entries.pop(old, None)
            if entry is None:
                return False
            self._entries[new] = entry
            self._persist_locked()
            return True

    def forget(self, session: str) -> bool:
        """Drop one session's entry; True if there was one to drop. For a name
        about to be reused by a NEW session, which must not inherit this one."""
        with self._lock:
            if self._entries.pop(session, None) is None:
                return False
            self._persist_locked()
            return True

    def _persist_locked(self) -> None:
        """Atomic write (temp file + os.replace) so a crash mid-write can never
        leave the half-written file that the tolerant loader would then discard.
        Mechanism owned by atomic_store.py — see the note there."""
        atomic_write(self._path, self._serialize(self._entries), self._noun)
