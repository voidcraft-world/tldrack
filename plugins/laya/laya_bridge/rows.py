"""THE key: one state hash that ledger rows, Sonnet labels, operator answers and train rows join on.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

- `canonical_json` is the one serialisation of a non-string state. The ledger hashes it AND
  stores it, so a stored text always re-hashes to its `state_hash`.
- `state_key` of a string is byte-identical to laya-lab's `text_key` (`open_work_data.py`): the
  open-work state IS the hand-off string (tldrack/open_work.py `decide_state`).
- laya-lab's trainer split (`data.py` `state_bucket`) keeps its own serialisation on purpose.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from laya_bridge import files
from laya_bridge.paths import CHECKPOINTS_DIRNAME

#: A sweep of the cut needs this many clean operator labels, with this many of each class.
SWEEP_MIN_ROWS = 40
SWEEP_MIN_PER_CLASS = 10

#: One checkpoint's training record (`<checkpoint>.data/train.jsonl`, laya-lab `train.py`).
TRAIN_FILE = "train.jsonl"
TRAIN_CAP_BYTES = 8 * 1024 * 1024

_TRAINED = files.StampedCache()


def canonical_json(state: object) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False)


def state_digest(state: object) -> str:
    """sha256 hex of a string's UTF-8, else of its `canonical_json`. ValueError on NaN/±Infinity."""
    text = state if isinstance(state, str) else canonical_json(state)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def state_key(state: object) -> str:
    return state_digest(state)[:16]


def bucket(value: str, salt: str) -> int:
    """A stable 0–99 draw per value: membership never moves when the same text is judged again."""
    return int(hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:8], 16) % 100


def utc_stamp(now: datetime | None = None) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ`, the form Zod's `z.iso.datetime()` reads; never `isoformat()`'s `+00:00`."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


#: A label key as a train row carries it (laya-lab `train.py` keeps it; laya's loader ignores it).
LABEL_KEY = re.compile(r"[0-9a-f]{16}")


def _train_keys(data: bytes) -> frozenset[str]:
    """`state_key(row['state'])` of every row, plus the row's own label `key` when it carries one: a long
    hand-off trains as its shortened state (`open_work.fit_hand_off`), whose key is not the text's."""
    rows, _bad = files.parse_jsonl(data)
    keys = set()
    for row in rows:
        if isinstance(row.get("key"), str) and LABEL_KEY.fullmatch(row["key"]):
            keys.add(row["key"])
        state = row.get("state")
        if state is None:
            continue
        try:
            keys.add(state_key(state))
        except ValueError:
            continue
    return frozenset(keys)


def checkpoint_name(fine_tune: str) -> re.Pattern[str]:
    """The folder names laya-lab gives `fine_tune`'s checkpoints, `<fine_tune>-<stamp>`. fullmatch."""
    return re.compile(rf"{re.escape(fine_tune)}-[0-9][0-9-]*")


def checkpoint_keys(base: Path, checkpoint: str) -> frozenset[str] | None:
    """The keys ONE checkpoint trained on (`<checkpoint>.data/train.jsonl`), or None when it has no readable
    record. laya-lab's promotion gate reads two of these (candidate, served) to keep rows neither trained on."""
    if not checkpoint or "/" in checkpoint or checkpoint.startswith("."):
        return None
    folder = base / CHECKPOINTS_DIRNAME / f"{checkpoint}.data"
    if not files.is_real_dir(folder):
        return None
    code, found = _TRAINED.get(folder / TRAIN_FILE, TRAIN_CAP_BYTES, _train_keys)
    return found if code == files.OK else None


def trained_keys(base: Path, fine_tune: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """(every key a `<fine_tune>-*` checkpoint trained on, the checkpoints with no readable record).

    Keys are the union of `state_key(row['state'])` over `checkpoints/<fine_tune>-*.data/train.jsonl`;
    only the keys are kept, never the rows. A record that is a link, too large or unreadable counts
    as no record, so the caller can say "retrain it" instead of guessing.
    """
    folder = base / CHECKPOINTS_DIRNAME
    name = checkpoint_name(fine_tune)
    keys: set[str] = set()
    try:
        entries = sorted(folder.iterdir()) if files.is_real_dir(folder) else []
    except OSError:
        entries = []
    checkpoints = [e.name for e in entries if name.fullmatch(e.name) and files.is_real_dir(e)]
    records = {
        e.name[: -len(".data")]
        for e in entries
        if e.name.endswith(".data") and name.fullmatch(e.name[: -len(".data")]) and files.is_real_dir(e)
    }
    for stem in sorted(records):
        found = checkpoint_keys(base, stem)
        if found is not None:
            keys.update(found)
        else:
            records.discard(stem)
    without = [checkpoint for checkpoint in checkpoints if checkpoint not in records]
    return frozenset(keys), tuple(without)
