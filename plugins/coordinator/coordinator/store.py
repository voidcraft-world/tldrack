"""Durable findings + dismissals, in the bridge's own store idiom.

Shape borrowed wholesale from `tldrack/models.py` and `icons.py`:
a `{version, ...}` JSON envelope under `~/.local/state/tldrack/`, a
tolerant load that drops individually bad rows rather than the whole file, an
atomic mkstemp + `os.replace` write, and a no-op when nothing actually changed.

── Why dismissals are the point, not polish ───────────────────────────────
A scan of a busy machine surfaces dozens of items, and most of them are things
the user has already looked at and decided about. Without a durable dismissal the
audit re-reports the identical list forever, which trains the user to ignore
the whole surface — the feature would be worse than not existing. So the
dismissal set is persisted next to the findings and applied on the way OUT,
which also means `POST /dismiss` needs no re-scan to take effect.

The findings themselves are persisted because `GET /state` must be cheap: a
client polls it, and a scan reads tens of megabytes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

STORE_FILE_VERSION = 1

DEFAULT_STORE_PATH = Path.home() / ".local" / "state" / "tldrack" / "coordinator-findings.json"

#: Shape-only guard, same reasoning as models.py's MODEL_TOKEN_RE: the KEY
#: vocabulary (session prefix, kind) is owned by signals.py and will gain kinds,
#: so the store validates that a key looks like a key and nothing more.
FINDING_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}:[a-z][a-z-]{0,39}")

_EMPTY_SCAN = {
    "scanned_at": None,
    "scanned": 0,
    "ranked": False,
    # Which pass sorted the list (`plugin._rank_scan`) — and what Laya's training trusts as a label.
    "ranked_by": None,
    "error": None,
    "findings": [],
}


def resolve_store_path() -> Path:
    env_path = os.environ.get("TLDRACK_COORDINATOR_PATH")
    return Path(env_path) if env_path else DEFAULT_STORE_PATH


def parse_store_file(text: str) -> tuple[dict, set[str]]:
    """Parse the file body into `(scan, dismissed)`.

    Raises ValueError when the ENVELOPE is wrong so the caller can warn about a
    corrupt file; individually malformed findings and dismissal keys are dropped
    instead — one bad row must never cost the user their whole dismissal set.
    """
    payload = json.loads(text)  # ValueError on non-JSON
    if not isinstance(payload, dict):
        raise ValueError('expected {"version": ..., "scan": {...}, "dismissed": [...]}')

    raw_scan = payload.get("scan")
    scan = dict(_EMPTY_SCAN)
    if isinstance(raw_scan, dict):
        scan["scanned_at"] = raw_scan["scanned_at"] if isinstance(raw_scan.get("scanned_at"), str) else None
        scan["scanned"] = raw_scan["scanned"] if isinstance(raw_scan.get("scanned"), int) else 0
        scan["ranked"] = bool(raw_scan.get("ranked"))
        scan["ranked_by"] = raw_scan["ranked_by"] if raw_scan.get("ranked_by") in ("laya", "claude") else None
        scan["error"] = raw_scan["error"] if isinstance(raw_scan.get("error"), str) else None
        rows = raw_scan.get("findings")
        scan["findings"] = [row for row in rows if _is_finding(row)] if isinstance(rows, list) else []

    raw_dismissed = payload.get("dismissed")
    dismissed = {key for key in raw_dismissed if is_finding_key(key)} if isinstance(raw_dismissed, list) else set()
    return scan, dismissed


def serialize_store_file(scan: dict, dismissed: set[str]) -> str:
    """Stable (sorted, indented) body — the file is user-inspectable state."""
    body = {
        "version": STORE_FILE_VERSION,
        "scan": scan,
        "dismissed": sorted(dismissed),
    }
    return json.dumps(body, indent=2) + "\n"


def is_finding_key(value: object) -> bool:
    return isinstance(value, str) and bool(FINDING_KEY_RE.fullmatch(value))


def empty_scan() -> dict:
    return json.loads(json.dumps(_EMPTY_SCAN))


def _is_finding(row: object) -> bool:
    return isinstance(row, dict) and is_finding_key(row.get("key")) and isinstance(row.get("kind"), str)


class CoordinatorStore:
    """File-persisted last scan + dismissal set.

    Handler threads race (ThreadingHTTPServer) and the scan worker is a third
    thread, so reads copy and writes persist under one lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._scan: dict = empty_scan()
        self._dismissed: set[str] = set()
        try:
            text = path.read_text()
        except FileNotFoundError:
            return  # first run — created on the first scan or dismissal
        except OSError as err:
            print(f"coordinator: could not read {path} ({err}) — starting empty", file=sys.stderr)
            return
        try:
            self._scan, self._dismissed = parse_store_file(text)
        except ValueError as err:
            # Deliberately NOT re-persisted: an empty in-memory state must not
            # clobber a file the user might want to hand-repair.
            print(f"coordinator: corrupt store {path} ({err}) — ignoring it", file=sys.stderr)

    def snapshot(self) -> tuple[dict, set[str]]:
        """A fully independent copy. Findings are nested dicts handed straight
        to every client, which is exactly where an aliased row would be
        dangerous."""
        with self._lock:
            return json.loads(json.dumps(self._scan)), set(self._dismissed)

    def set_scan(self, scan: dict) -> None:
        """Replace the stored scan, persisting only on a real change.

        The deep pass re-writes the same findings with `work_class` filled in,
        so an unchanged-write guard is not theoretical here.
        """
        with self._lock:
            if self._scan == scan:
                return
            self._scan = json.loads(json.dumps(scan))
            self._persist_locked()

    def dismiss(self, key: str) -> bool:
        """True when the key was newly suppressed; False when already dismissed
        (the caller's goal state is met either way, so it is not an error)."""
        with self._lock:
            if key in self._dismissed:
                return False
            self._dismissed.add(key)
            self._persist_locked()
            return True

    def restore(self) -> int:
        """Clear every dismissal; returns how many were cleared."""
        with self._lock:
            count = len(self._dismissed)
            if count:
                self._dismissed = set()
                self._persist_locked()
            return count

    def _persist_locked(self) -> None:
        """Atomic write (temp file + os.replace) so a crash mid-write can never
        leave the half-written file the tolerant loader would then discard.

        EVERY step is inside the try, mkdir and mkstemp included. Those two are
        the likeliest to fail — a read-only or full state dir fails there, not
        at the write — and an OSError escaping this method escapes the store: a
        `POST /dismiss` 500s after `self._dismissed` already changed, so the
        panel is told the row is still live when it is not, and the audit
        worker's own error handler re-raises the same error while handling it.
        Degrading to "kept in memory only" is the documented behaviour; it has
        to actually be reachable.
        """
        temp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                handle.write(serialize_store_file(self._scan, self._dismissed))
            os.replace(temp_name, self._path)
        except OSError as err:
            print(f"coordinator: could not persist {self._path} ({err}) — kept in memory only", file=sys.stderr)
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
