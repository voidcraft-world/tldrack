"""The spine the plugin's four persisted stores share: one file, one lock, one
`{branch: row}` dict.

`store.PrStore`, `store.AdoptStore`, `harbor.PrIndexStore` and
`harbor.BriefStore` are the same machine four times over — a
`{version, <key>: {branch: row}}` file under `~/.local/state/tldrack/`,
a load that tolerates both an absent file and a corrupt one, an atomic write
that degrades to a stderr line instead of failing the request, and a lock every
accessor takes. What actually differs is the ENVELOPE and the row shape, so
those stay with each store: a `parse_*_file` function per file, a `_body` per
class, and everything around them lives here once.

`AdoptStore` used to say it mirrored `PrStore` rather than share a base class,
because a generic store "would have to be told which key to read". It is told —
by `_parse` and `_body` below, two hooks — and that bought back four copies of
the same twenty lines.

── The atomic write ─────────────────────────────────────────────────────────
The idiom, its temp-dir requirement and why every step sits inside the try:
owned by bridge/tldrack/atomic_store.py — see the note there.
Copied rather than imported because a bridge plugin may not import the host.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import TypeVar
from collections.abc import Callable

T = TypeVar("T")


def load_store_file(path: Path, parse: Callable[[str], T], fallback: T) -> T:
    """`parse(path's body)`, or `fallback` when the file is not there (every
    machine before its first write) or cannot be parsed — a corrupt file is
    warned about on stderr, never fatal."""
    try:
        text = path.read_text()
    except OSError:
        return fallback
    try:
        return parse(text)
    except (ValueError, json.JSONDecodeError) as err:
        print(f"worktree: ignoring unreadable {path} ({err})", file=sys.stderr)
        return fallback


# jscpd:ignore-start
# PLUGIN -> HOST port, and the harder direction. `plugins/worktree/scripts/wt`
# runs `python -m worktree.cli` under any bare Python >= 3.10 with only
# PYTHONPATH=<plugin dir> — macOS /usr/bin/python3 included — so this module cannot
# import tldrack, which lives in the bridge's own venv. The atomic-write idiom
# is owned by bridge/tldrack/atomic_store.py; this is the declared
# copy, per house rule 4 of the root CLAUDE.md.
def persist_atomically(path: Path, body: str, *, kept: str) -> None:
    """Write `body` to `path` through a sibling temp file and `os.replace`.

    Failure is reported and swallowed — losing the on-disk copy is not a reason
    to fail the request that produced it, and the caller has already applied the
    change under its lock (`kept` names what is left there). EVERY step is inside
    the try, `mkdir` and `mkstemp` included: those two are what an unusable state
    directory fails at, so without them the handler is unreachable in exactly the
    case it exists for (`tests/test_store_persist.py`).
    """
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
        os.replace(temp_name, path)
    except OSError as err:
        print(f"worktree: could not persist {path} ({err}) — {kept} kept in memory only", file=sys.stderr)
        # jscpd:ignore-end
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def read_rows(text: str, key: str, *, article: str = "a") -> tuple[dict, dict]:
    """A `{version, <key>: {branch: row}}` body → `(envelope, raw rows)`.

    Raises ValueError on a wrong ENVELOPE so the caller can warn about a corrupt
    file. Individually malformed rows are `clean_rows`' business and are dropped
    instead: one bad row must never cost the user every other branch's.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    rows = data.get(key)
    if not isinstance(rows, dict):
        raise ValueError(f"expected {article} '{key}' object")
    return data, rows


def clean_rows(rows: dict, build: Callable[[str, dict], dict | None]) -> dict[str, dict]:
    """`{branch: build(branch, row)}`, dropping anything that is not a
    `str: dict` pair and anything `build` rejects by returning None."""
    clean: dict[str, dict] = {}
    for branch, row in rows.items():
        if not isinstance(branch, str) or not isinstance(row, dict):
            continue
        cleaned = build(branch, row)
        if cleaned is not None:
            clean[branch] = cleaned
    return clean


class BranchRowStore:
    """Thread-safe `{branch: row}`, persisted.

    Every mutation happens under the lock and is persisted inside it, so a
    background worker and a `GET /state` on a request thread cannot interleave a
    half-updated row. A subclass supplies the envelope — `_parse` for the file it
    reads, `_body` for the one it writes, `_kept` for the degraded-write line —
    and whatever mutations it actually has; `set` is deliberately not here,
    because no two of these stores set a row the same way.
    """

    #: What the "kept in memory only" line calls this store's contents.
    _kept = "state"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._load()

    # ── The envelope: the one part each store owns ──────────────────────────

    def _parse(self, text: str) -> dict[str, dict]:
        raise NotImplementedError

    def _body(self) -> str:
        raise NotImplementedError

    def _load(self) -> None:
        self._rows = load_store_file(self._path, self._parse, {})

    def _persist(self) -> None:
        """Caller holds the lock."""
        persist_atomically(self._path, self._body(), kept=self._kept)

    # ── Accessors ───────────────────────────────────────────────────────────

    def get(self, branch: str) -> dict | None:
        with self._lock:
            row = self._rows.get(branch)
            return dict(row) if row else None

    def drop(self, branch: str) -> None:
        with self._lock:
            if self._rows.pop(branch, None) is not None:
                self._persist()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {branch: dict(row) for branch, row in self._rows.items()}
