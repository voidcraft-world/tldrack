"""Row shape: owned by schemas/src/laya-review.ts (operatorAnswerRowSchema) — see the note there.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

The operator's REVIEW answers: one append-only log per surface, `data/<stem>-operator.jsonl`. Never
Sonnet's labels file, never a `review/` folder. This plugin is its only writer; `fold` is THE one
fold of it, and laya-lab imports it rather than re-reading the log its own way.

An answer is never edited: UNDO appends an `undo` row naming the `answer_id`, and the fold drops
it. So the file is the history, and a relabel can never move what an earlier answer recorded.

RETEST. A blind second look at a text the operator already graded (`review_data.retest_due` owns which
and when) is its own op, never an `answer`. The fold keeps the LAST live answer per key and the stats
count every live `answer_id`, so a retest written as an answer would replace the label it re-tests and
count twice in `by_stratum`, agree/flip and every κ. A `retest` row names the answer it re-tests (`of`)
and what the operator said this time (`open`, with `needs` on its side; no `open` = could not say).
The fold holds it in `retests` only: never in `labels`, `skipped` or `live`. One reader pairs it with
its answer, the intra-rater κ (`review_data.intra_rater`). It has its own `answer_id`, so UNDO takes it
back like an answer.

GREAT EXAMPLES. An `example` row sets a text key's `great_example` flag for future synthetic
training candidates. The last setting wins; it survives grading and label undo. It is curation,
not a label or admission to a golden evaluation suite: no counts, splits or training eligibility
change. Only the key and decision reference are saved here; the private source keeps the text.
`examples` exposes the latest setting, including removals, to downstream readers.

Writes run under `REVIEW_LOCK` (threads) plus `fcntl.flock` on `data/.<stem>-operator.lock`
(processes); `data/` must already exist, because this plugin never creates the params tree.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laya_bridge import files, ledger
from laya_bridge.envelope import Refusal
from laya_bridge.files import is_p
from laya_bridge.paths import DATA_DIRNAME, data_file
from laya_bridge.review_surfaces import REVIEW_SURFACES

REVIEW_LOCK = threading.Lock()
#: A write that would carry the log past this size or row count is refused whole (413 `review_full`); reads allow
#: twice the bytes, so a full log is still read whole.
LOG_CAP_BYTES = 8 * 1024 * 1024
LOG_MAX_ROWS = 20_000
READ_CAP_BYTES = 2 * LOG_CAP_BYTES
ROW_MAX_BYTES = 2048
FLOCK_WAIT_S = 2.0

ANSWER_ID = re.compile(r"a_[0-9a-f]{16}")
#: One press that graded a whole wave (`review_data.py` owns waves): every answer row it wrote carries the same id, so ONE
#: undo takes them all back. TS twin: schemas/src/laya-review.ts REVIEW_WAVE_ID.
WAVE_ID = re.compile(r"w_[0-9a-f]{16}")
REVIEW_KEY = re.compile(r"(?:[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63})")
ANSWERS = ("agree", "flip", "skip")
#: The longest version name an answer row's `challenger` block carries (a checkpoint folder's basename).
CHALLENGER_NAME_MAX = 120
#: copy of schemas/src/laya-review.ts REVIEW_NEEDS and REVIEW_NEEDS_OPEN — pinned by tests/test_contract.py. Why each
#: half rides beside its own `open`: owned by schemas/src/laya-review.ts REVIEW_NEEDS — see the note there.
NEEDS = ("you", "running", "read")
NEEDS_OPEN = ("you", "running")


def operator_path(base: Path, surface: str) -> Path:
    return data_file(base, REVIEW_SURFACES[surface].stem, "operator")


def _lock_path(base: Path, surface: str) -> Path:
    return base / DATA_DIRNAME / f".{REVIEW_SURFACES[surface].stem}-operator.lock"


def read_operator_source(path: Path) -> tuple[str, list[dict], int]:
    """(source code, rows, bad_lines), parsing only what was appended since the last read."""
    return files.shared_tail(path, READ_CAP_BYTES).read()


def read_operator_log(path: Path) -> tuple[list[dict], int]:
    _code, rows, bad = read_operator_source(path)
    return rows, bad


@dataclass
class OperatorFold:
    """`labels`: key → its live non-skip answer row; `skipped`: keys whose live answer is a skip;
    `live`: answer_id → row, in log order; `undone`: answer_ids an undo removed; `retests`: answer_id → its live
    `retest` row, in log order (the module doc: RETEST)."""

    labels: dict[str, dict] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    live: dict[str, dict] = field(default_factory=dict)
    undone: set[str] = field(default_factory=set)
    retests: dict[str, dict] = field(default_factory=dict)
    examples: dict[str, dict] = field(default_factory=dict)
    bad_lines: int = 0
    rows: int = 0

    def answered(self, key: str) -> bool:
        return key in self.labels or key in self.skipped

    def live_answer(self, key: str) -> dict | None:
        found = None
        for row in self.live.values():
            if row["key"] == key:
                found = row
        return found

    def retest_of(self, answer_id: str) -> dict | None:
        """The live retest of that answer: the last one, should the log hold two."""
        found = None
        for row in self.retests.values():
            if row["of"] == answer_id:
                found = row
        return found

    def wave_rows(self, row: Mapping[str, Any]) -> list[dict]:
        """Every live answer the press that wrote `row` wrote, in log order: just `row` when it graded one text."""
        wave = row.get("wave_id")
        if not isinstance(wave, str):
            return [dict(row)]
        return [live for live in self.live.values() if live.get("wave_id") == wave]


def _row_ok(row: Mapping[str, Any]) -> bool:
    if row.get("v") != 1 or row.get("by") != "operator" or not isinstance(row.get("answer_id"), str):
        return False
    if not ANSWER_ID.fullmatch(row["answer_id"]) or not isinstance(row.get("surface"), str):
        return False
    op = row.get("op")
    if op == "undo":
        return True
    if not (isinstance(row.get("key"), str) and REVIEW_KEY.fullmatch(row["key"]) is not None and _row_needs_ok(row)):
        return False
    if op == "example":
        return isinstance(row.get("great_example"), bool) and (
            row.get("decision_id") is None
            or (isinstance(row["decision_id"], str) and ledger.DECISION_ID.fullmatch(row["decision_id"]) is not None)
        )
    if op == "retest":
        return (
            isinstance(row.get("of"), str)
            and ANSWER_ID.fullmatch(row["of"]) is not None
            and ("open" not in row or isinstance(row["open"], bool))
        )
    against = row.get("against")
    return (
        op == "answer"
        and row.get("answer") in ANSWERS
        and isinstance(against, dict)
        and isinstance(against.get("open"), bool)
        and (row["answer"] == "skip" or isinstance(row.get("open"), bool))
        and ("wave_id" not in row or (isinstance(row["wave_id"], str) and WAVE_ID.fullmatch(row["wave_id"]) is not None))
        and ("challenger" not in row or _challenger_ok(row["challenger"]))
    )


def _row_needs_ok(row: Mapping[str, Any]) -> bool:
    """A stored row's `needs`: absent, or a half on its own side (a null one is no row this log writes)."""
    return "needs" not in row or (row["needs"] is not None and needs_ok(row["needs"], row.get("open")))


def needs_ok(needs: Any, open_: Any) -> bool:
    """`needs` rides on its own side, on an answer and on a retest alike: `you` and `running` beside `open: true`,
    `read` beside `open: false`. Absent always does. TS twin: schemas/src/laya-review.ts `needs_side_ok`."""
    return needs is None or (needs in NEEDS and isinstance(open_, bool) and open_ == (needs in NEEDS_OPEN))


def _challenger_ok(block: Any) -> bool:
    return (
        isinstance(block, dict)
        and isinstance(block.get("name"), str)
        and 0 < len(block["name"]) <= CHALLENGER_NAME_MAX
        and is_p(block.get("p"))
        and is_p(block.get("threshold"))
    )


def fold(rows: Iterable[Mapping[str, Any]], bad_lines: int = 0) -> OperatorFold:
    """Pure. Per key, the LAST live answer decides; an undo of an unknown or already-undone id is ignored.
    A `retest` row goes to `retests` and nowhere else (the module doc: RETEST)."""
    out = OperatorFold(bad_lines=bad_lines)
    example_ids: set[str] = set()
    for row in rows:
        out.rows += 1
        if not _row_ok(row):
            out.bad_lines += 1
            continue
        answer_id = row["answer_id"]
        if row["op"] != "undo":
            if answer_id in out.live or answer_id in out.retests or answer_id in out.undone or answer_id in example_ids:
                out.bad_lines += 1  # an id is written once; a repeat is not a second answer
                continue
            if row["op"] == "example":
                example_ids.add(answer_id)
                out.examples[row["key"]] = dict(row)
            else:
                (out.live if row["op"] == "answer" else out.retests)[answer_id] = dict(row)
        elif answer_id in out.live:
            del out.live[answer_id]
            out.undone.add(answer_id)
        elif answer_id in out.retests:
            del out.retests[answer_id]
            out.undone.add(answer_id)
    last: dict[str, dict] = {}
    for row in out.live.values():
        last[row["key"]] = row
    for key, row in last.items():
        if row["answer"] == "skip":
            out.skipped.add(key)
        else:
            out.labels[key] = row
    return out


def read_fold(base: Path, surface: str) -> tuple[str, OperatorFold]:
    code, rows, bad = read_operator_source(operator_path(base, surface))
    return code, fold(rows, bad)


def answer_ref(row: Mapping[str, Any]) -> dict:
    """`answerRefSchema`: THE wire ref of one live answer row, for every route that names one (the deck's answer and
    undo results, each RECENT row's `operator`), so no route can serve a partial one. `open` is null on a skip."""
    return {
        "answer_id": row["answer_id"],
        "key": row["key"],
        "answer": row["answer"],
        "open": row.get("open"),
        "needs": row.get("needs"),
        "wave_id": row.get("wave_id"),
    }


# ── writing ───────────────────────────────────────────────────────────────────


def new_answer_id() -> str:
    return f"a_{secrets.token_hex(8)}"


def new_wave_id() -> str:
    return f"w_{secrets.token_hex(8)}"


def answer_row(
    *,
    answer_id: str,
    at: str,
    surface: str,
    key: str,
    stratum: str,
    answer: str,
    against: Mapping[str, Any],
    laya: Mapping[str, Any] | None,
    decision_id: str | None,
    held_out: bool | None,
    needs: str | None = None,
    wave_id: str | None = None,
    challenger: Mapping[str, Any] | None = None,
) -> dict:
    """One `op: answer` row. `open` is `against.open` on agree, its negation on flip, and ABSENT on skip.
    `needs` rides on its own side of `open` (`needs_ok`); the caller (review.py `answer`) refuses it anywhere else.
    `wave_id` names the one press that graded a wave; absent on an answer to one text.
    `challenger` (`{name, p, threshold}`) rides only on an answer to a `challenger` card: the @challenger's own
    answer the card showed, so the side the operator took reads off this row (`review_data.challenger_sides`)."""
    row: dict[str, Any] = {
        "v": 1,
        "op": "answer",
        "answer_id": answer_id,
        "at": at,
        "by": "operator",
        "surface": surface,
        "key": key,
        "stratum": stratum,
        "answer": answer,
        "against": dict(against),
        "laya": dict(laya) if laya is not None else None,
        "decision_id": decision_id,
        "held_out": held_out,
    }
    if answer != "skip":
        row["open"] = against["open"] if answer == "agree" else not against["open"]
    if needs is not None:
        row["needs"] = needs
    if wave_id is not None:
        row["wave_id"] = wave_id
    if challenger is not None:
        row["challenger"] = {"name": challenger["name"], "p": challenger["p"], "threshold": challenger["threshold"]}
    return row


def retest_row(*, answer_id: str, at: str, surface: str, key: str, of: str, said: bool | None, needs: str | None = None) -> dict:
    """One `op: retest` row (the module doc: RETEST). `of` is the answer it re-tests. `said` is the second look's
    verdict, written as `open`, and ABSENT when the operator could not say (None); `needs` rides on its own side of
    `open` (`needs_ok`; review.py `retest` refuses it anywhere else)."""
    row: dict[str, Any] = {
        "v": 1,
        "op": "retest",
        "answer_id": answer_id,
        "at": at,
        "by": "operator",
        "surface": surface,
        "key": key,
        "of": of,
    }
    if said is not None:
        row["open"] = said
    if needs is not None:
        row["needs"] = needs
    return row


def retest_ref(row: Mapping[str, Any]) -> dict:
    """`retestRefSchema`: the wire ref of one live retest row. `open` is null when the operator could not say."""
    return {"answer_id": row["answer_id"], "key": row["key"], "of": row["of"], "open": row.get("open"), "needs": row.get("needs")}


def undo_row(*, answer_id: str, at: str, surface: str) -> dict:
    return {"v": 1, "op": "undo", "answer_id": answer_id, "at": at, "by": "operator", "surface": surface}


@contextmanager
def writing(base: Path, surface: str) -> Iterator[None]:
    """Hold REVIEW_LOCK and the flock for one read-check-append. Raises `envelope.Refusal`."""
    if not files.is_real_dir(base / DATA_DIRNAME):
        raise Refusal(
            503,
            "params_unavailable",
            "The laya data folder does not exist yet",
            "laya-lab creates <data>/laya/data on its first export",
        )
    with REVIEW_LOCK:
        try:
            fd = files.open_regular(_lock_path(base, surface), os.O_RDWR | os.O_CREAT)
        except files.SourceError:
            raise Refusal(
                503, "source_refused", "The review lock file is a link or not a regular file: refusing to write"
            ) from None
        except OSError:
            raise Refusal(503, "review_unwritable", "The review lock file cannot be opened") from None
        try:
            deadline = time.monotonic() + FLOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Refusal(503, "review_unwritable", "Another process holds the review log: try again") from None
                    time.sleep(0.05)
                except OSError:
                    raise Refusal(503, "review_unwritable", "The review lock file cannot be locked") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append(base: Path, surface: str, row: Mapping[str, Any], current: OperatorFold) -> None:
    """Append one row (call inside `writing`): `append_many` of one."""
    append_many(base, surface, (row,), current)


def append_many(base: Path, surface: str, written: Sequence[Mapping[str, Any]], current: OperatorFold) -> None:
    """Append every row of one press, or none (call inside `writing`). fsync'd once, before it returns.

    Every check runs before the first byte: each row's size (500), and the log's bytes and rows after ALL of them
    (413 `review_full`), so a wave press or a wave undo is never half recorded. Checked row by row, a cap crossed at
    row k refused the press after rows 1..k-1 were already on disk under its `wave_id`, and the row cap, read off a
    fold that never moved inside the loop, let a wave write past it. The lines go down in one write."""
    lines = [(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") for row in written]
    if not lines:
        return
    if any(len(line) > ROW_MAX_BYTES for line in lines):
        raise Refusal(500, "review_unwritable", "An answer row is over its size limit: nothing was recorded")
    path = operator_path(base, surface)
    stamp = files.stamp_of(path)
    size = stamp[2] if stamp is not None else 0
    if size + sum(map(len, lines)) > LOG_CAP_BYTES or current.rows + len(lines) > LOG_MAX_ROWS:
        raise Refusal(413, "review_full", "The review log is full", f"move data/{path.name} aside once laya-lab has read it")
    try:
        files.append_line(path, b"".join(lines), fsync=True)
    except files.SourceError:
        raise Refusal(503, "source_refused", "The review log is a link or not a regular file") from None
    except OSError:
        raise Refusal(503, "review_unwritable", "The review log cannot be written") from None
