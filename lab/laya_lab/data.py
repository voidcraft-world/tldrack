"""Training data: where each surface's labels come from, and the held-out split.

- **coordinator** — findings the `claude -p` pass labelled open-work / routine
  (`ranked_by == "claude"`), ACCUMULATED across audits: the store keeps only the latest scan, so
  every export merges into `<data>/laya/data/coordinator.jsonl`, keyed by finding.
  Laya-labelled scans are never exported — a model must not be trained on its own answers.
- **agent_open_work** — the agent rack's turn ends, labelled by a teacher and graded by the operator:
  owned by `laya_lab/open_work_data.py` — see the note there.

A surface trains from `<data>/laya/data/<surface>.jsonl`, one laya-train row a line (`state`, `questions`,
`expected`), wherever those rows came from.

The split is by a hash of the STATE, not by row order, so a state never lands on both sides of it
and re-running an export never reshuffles what was held out.

Every file laya-lab writes holds the operator's text, so this module is its ONE writer: files are
created 0600 and folders 0700 whatever the umask (`write_jsonl`, `atomic_write_text`,
`open_private`), and a link at the target is never written through. Files written before this
rule are repaired by `laya_bridge.files.tighten_params`, which every command runs first.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import IO, Any
from collections.abc import Iterable

from laya_lab import siblings  # noqa: F401  (puts the coordinator package on sys.path)
from coordinator.laya_rank import OPEN_WORK_QUESTION
from coordinator.signals import WORK_CLASS_OPEN, WORK_CLASS_ROUTINE
from coordinator.store import parse_store_file, resolve_store_path
from laya_bridge import files

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700

#: Held-out share, in percent of states.
EVAL_PERCENT = 15

#: The surfaces `train` knows; `agent_open_work` rows are built by `open_work_data.py`.
#: A fine-tune's epochs. Why 2, not 4: owned by lab/README.md — see "agent_open_work data". It lives here, not in
#: `train`, so `cli` reads it without importing numpy or torch (tests/test_cli_imports.py).
DEFAULT_EPOCHS = 2

SURFACES = ("coordinator", "agent_open_work")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def make_private_dirs(folder: Path) -> None:
    """Create `folder` and its missing parents 0700. `mkdir`'s mode is umask-masked, so each folder
    this creates is chmodded too; one that already exists is left to `tighten_params`."""
    missing: list[Path] = []
    probe = folder
    while not os.path.lexists(probe):
        missing.append(probe)
        probe = probe.parent
    for created in reversed(missing):
        try:
            os.mkdir(created, PRIVATE_DIR)
        except FileExistsError:
            continue
        os.chmod(created, PRIVATE_DIR)


def open_private(path: Path, mode: str) -> IO[str]:
    """A text handle on a 0600 regular file: `'w'` truncates, `'a'` appends. Never through a link.

    A file that exists with a wider mode is tightened on open, so an append never keeps a 0644 file.
    """
    if mode not in ("w", "a"):
        raise ValueError(f"open_private mode must be 'w' or 'a', not {mode!r}")
    make_private_dirs(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if mode == "w" else os.O_APPEND)
    try:
        fd = files.open_regular(path, flags)
    except files.SourceError:
        raise SystemExit(f"[laya-lab] refusing to write {path}: it is a link or not a regular file") from None
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            os.fchmod(fd, PRIVATE_FILE)
        # open_regular adds O_NONBLOCK against FIFOs; a regular file then blocks as usual.
        return os.fdopen(fd, mode, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` whole: a reader (the bridge plugin, mid-poll) sees the old file or the new one,
    never half of either. mkstemp creates 0600; `os.replace` swaps a link at `path`, never follows it."""
    make_private_dirs(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def tighten(base: Path) -> None:
    """THE mode repair (`laya_bridge.files.tighten_params`), with a line per path it changed."""
    for changed in files.tighten_params(base):
        target = base / changed
        mode = "0700" if target.is_dir() and not target.is_symlink() else "0600"
        print(f"[laya-lab] tightened {'laya/' if changed == '.' else changed} → {mode}")


def jsonl_line(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, allow_nan=False) + "\n"


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """THE JSONL writer: atomic, 0600 under a 0700 folder. Returns the row count."""
    lines = [jsonl_line(row) for row in rows]
    atomic_write_text(path, "".join(lines))
    return len(lines)


def state_bucket(state: Any) -> int:
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100


def split_rows(rows: list[dict[str, Any]], eval_percent: int = EVAL_PERCENT) -> tuple[list, list]:
    train, held_out = [], []
    for row in rows:
        (held_out if state_bucket(row.get("state")) < eval_percent else train).append(row)
    return train, held_out


def coordinator_rows_from_scan(scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{finding key: training row}` for a Claude-ranked scan; empty for any other scan."""
    if scan.get("ranked_by") != "claude":
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for finding in scan.get("findings") or []:
        key = finding.get("key")
        work_class = finding.get("work_class")
        if not isinstance(key, str) or work_class not in (WORK_CLASS_OPEN, WORK_CLASS_ROUTINE):
            continue
        # The state `laya_rank.run_laya_rank` asks about, and the key it asks it under.
        state = {"headline": finding.get("headline") or "", "label": finding.get("label") or ""}
        rows[key] = {
            "state": state,
            "questions": {"q": OPEN_WORK_QUESTION},
            # laya-train reads a noul's `expected` as a boolean.
            "expected": {"q": work_class == WORK_CLASS_OPEN},
        }
    return rows


def export_coordinator(dataset: Path, store: Path | None = None) -> tuple[int, int]:
    """Merge the store's latest Claude-ranked scan into the dataset. Returns `(added, total)`."""
    scan, _dismissed = parse_store_file((store or resolve_store_path()).read_text(encoding="utf-8"))
    fresh = coordinator_rows_from_scan(scan)
    existing: dict[str, dict[str, Any]] = {}
    if dataset.exists():
        for row in read_jsonl(dataset):
            key = row.pop("key", None)
            if isinstance(key, str):
                existing[key] = row
    added = sum(1 for key in fresh if key not in existing)
    existing.update(fresh)
    total = write_jsonl(dataset, ({"key": key, **row} for key, row in sorted(existing.items())))
    return added, total
