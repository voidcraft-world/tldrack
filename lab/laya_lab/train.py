"""One fine-tune: split, train + calibrate (`laya.train.finetune`), judge it, register it only if it passed.

laya's own `finetune` already trains, fits per-type temperatures with the runtime's fitter, and
scores the base and the result on the candidate's own held-out split. What this adds is the DECISION.
A surface with a golden suite is judged by the promotion gate against the model it SERVES (`gate.py`
owns the checks); any other surface by `report_verdict`, laya's own held-out numbers against the base.
A fine-tune that does not help never reaches a caller, and the report saying why sits next to it.

The run is four steps a caller can also take one at a time, each from what the last left on disk (`prepare`, `fit`,
`judge_fit`, `register_fit`): the gated retrain journals them (`retrain.py`), and `run` is the four in a row.

A checkpoint that passes registers as the surface's @challenger by default (`--as challenger`): never served, it is
shadow-scored by the nightly pack until a promotion moves @champion onto it (`challenger.py`). `--as champion` serves
it at once, the old champion kept as @previous. Either way the version carries the gate's verdict as its tags
(`lab_files.gate_tags`).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from laya_lab import infer
from laya_lab.data import atomic_write_text, make_private_dirs, read_jsonl, split_rows, tighten, write_jsonl
from laya_lab.gate import GateRefused, judge, loss_rule, suite_for_surface
from laya_lab.registry import CHALLENGER, CHECKPOINTS_DIRNAME, DATA_DIRNAME, fine_tune_name, read_registry, register
from laya_bridge import files, lab_files, rows as bridge_rows
from laya_bridge.review_surfaces import REVIEW_SURFACES


BASE_REPO = "convaiinnovations/laya"
#: The base's subfolders this English fine-tune never loads.
BASE_SKIP = ["multilingual/*", "typed-decisions/*", "*.md", ".gitattributes"]
#: The gpu lease a fine-tune claims: weights, gradients and AdamW's two moments are 4 × the 1.6 GB checkpoint, plus
#: activations. An estimate, not a measurement.
TRAIN_GB = 8.0


def base_revision() -> str:
    from laya.revisions import PINNED_REVISIONS

    return PINNED_REVISIONS[BASE_REPO]


def base_checkpoint() -> Path:
    """The base checkpoint at laya's reviewed revision, from the Hub cache (downloaded once)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(BASE_REPO, revision=base_revision(), ignore_patterns=BASE_SKIP))


def report_verdict(report: dict[str, Any]) -> tuple[bool, str]:
    """`(promote, why)` from a laya train report (base vs fine-tune on its own split): the rule for a surface
    with no golden suite, so no promotion gate. Pure, so it is testable without torch."""
    before, after = report.get("before"), report.get("after")
    if not report.get("is_held_out") or not before or not after:
        return False, "no held-out evaluation — refusing to register an unmeasured checkpoint"
    return loss_rule(before, after)


def gate_verdict(surface: str, base: Path, checkpoint: Path, device: str) -> tuple[bool, str, dict[str, Any]]:
    """`(promote, why, tags)` from the promotion gate: `checkpoint` against the model the surface serves now. `tags`
    (`lab_files.gate_tags`) is what the registered version carries: PASS with the verdict's run, file and checks."""
    suite = suite_for_surface(surface)
    assert suite is not None
    try:
        report, out = judge(suite.name, base, candidate=checkpoint.name, device=device)
    except GateRefused as refusal:
        return False, f"the gate could not judge it: {refusal}", lab_files.gate_tags("FORCED")
    states = {name: check["state"] for name, check in (report.get("checks") or {}).items()}
    tags = lab_files.gate_tags("PASS" if report["promote"] else "FORCED", report.get("run_id"), out.name, states)
    return report["promote"], f"{report['why']} (verdict: {out.name})", tags


def withhold_reserve(surface: str, base: Path, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """(the rows that may train, how many were held back): the label budget, checked again at the trainer's door.

    `open_work_data.build` already leaves the reserve out, but `train` reads whatever dataset is on disk: one built
    before the operator's newest grades, or handed in with `--data`. One run on such a file would train on reserve
    keys, and a trained key never returns to the reserve (`laya_bridge.label_budget` owns the rule). The door holds
    the set the build holds (`open_work_data.reserve_held`), so it holds nothing back from a current build. A surface
    the operator does not review has no budget. The sources are read through `require_sources`, which refuses one it
    cannot read: an operator log read as "no labels" would let the whole reserve through, and an export read as empty
    would let a long hand-off through under its other key."""
    if surface not in REVIEW_SURFACES:
        return rows, 0
    from laya_lab.open_work_data import require_sources, reserve_held

    budget, held_keys = reserve_held(base, require_sources(base, surface), surface)
    kept, held = budget.withhold(rows, held_keys)
    return list(kept), held


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """What `models.json` keeps: the headline numbers, not laya's whole training dump."""
    keep = ("accuracy", "loss", "ece", "brier")
    return {
        "eval_items": report.get("eval_items"),
        "before": {k: (report.get("before") or {}).get(k) for k in keep},
        "after": {k: (report.get("after") or {}).get(k) for k in keep},
    }


REPORT_FILE = lab_files.TRAIN_REPORT_FILENAME
#: The trainer prints its loss every this many micro-batches, which is how often a fit's progress moves
#: (`follow_fit`). laya's own default is 100: about once an epoch on a thousand rows. Printing only: the fit is the same.
PROGRESS_EVERY = 5
#: The three lines of `laya.train.finetune`'s output a fit's progress is read from.
_TRAIN_ITEMS = re.compile(r"^train items (\d+),")
_MICRO_BATCH = re.compile(r"\(micro_batch (\d+) x grad_accum \d+\)")
_STEP = re.compile(r"^epoch (\d+)/(\d+) step (\d+) loss (-?\d+(?:\.\d+)?)")
_EPOCH_END = re.compile(r"^epoch (\d+)/(\d+) mean loss (-?\d+(?:\.\d+)?)")


class FitProgress:
    """A fit's progress, read off the trainer's own lines (pure: `read` takes one line). The trainer says how many
    items it trains on and its micro-batch once, then a loss every PROGRESS_EVERY micro-batches and at each epoch's
    end. `read` answers a `lab_files.train_progress` block when a line moved it, else None."""

    def __init__(self) -> None:
        self.items: int | None = None
        self.micro_batch: int | None = None

    @property
    def steps(self) -> int | None:
        """Micro-batches in one epoch, once both of the trainer's opening lines were read."""
        if not self.items or not self.micro_batch:
            return None
        return -(-self.items // self.micro_batch)

    def read(self, line: str) -> dict[str, Any] | None:
        if (found := _TRAIN_ITEMS.match(line)) is not None:
            self.items = int(found[1])
        elif (found := _MICRO_BATCH.search(line)) is not None:
            self.micro_batch = int(found[1])
        elif (found := _STEP.match(line)) is not None:
            return lab_files.train_progress(int(found[1]), int(found[2]), int(found[3]), self.steps, float(found[4]))
        elif (found := _EPOCH_END.match(line)) is not None and self.steps is not None:
            return lab_files.train_progress(int(found[1]), int(found[2]), self.steps, self.steps, float(found[3]))
        return None


class _Follow(io.TextIOBase):
    """stdout while a fit runs: every write still reaches the pane, and each whole line is offered to `on_line`."""

    def __init__(self, inner: Any, on_line: Callable[[str], None]) -> None:
        self._inner, self._on_line, self._held = inner, on_line, ""

    def write(self, text: str) -> int:
        self._inner.write(text)
        self._held += text
        *lines, self._held = self._held.split("\n")
        for line in lines:
            self._on_line(line)
        return len(text)

    def flush(self) -> None:
        self._inner.flush()


@contextlib.contextmanager
def follow_fit(on_progress: Callable[[dict[str, Any]], None] | None) -> Iterator[None]:
    """While the block runs, `on_progress` gets each progress block the trainer's output moves to. A callback that
    raises is dropped, never the fit: an hour of GPU does not end because a journal write failed."""
    if on_progress is None:
        yield
        return
    reader = FitProgress()

    def on_line(line: str) -> None:
        nonlocal on_progress
        moved = reader.read(line)
        if moved is not None and on_progress is not None:
            try:
                on_progress(moved)
            except Exception as error:  # noqa: BLE001 (whatever the callback raised, the fit goes on)
                on_progress = None
                print(f"[laya-lab] progress is no longer recorded ({type(error).__name__})", file=sys.stderr)

    with contextlib.redirect_stdout(_Follow(sys.stdout, on_line)):  # type: ignore[type-var]
        yield


@dataclass(frozen=True)
class Fit:
    """One fine-tune, planned (`prepare`): where it lands, its training record, and its counts."""

    surface: str
    out: Path
    work: Path
    train_rows: int
    held_out: int
    reserved: int
    epochs: int
    device: str


def prepare(
    surface: str,
    base: Path,
    dataset: Path | None,
    *,
    epochs: int,
    device: str,
    now: datetime | None = None,
    split: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> Fit:
    """Split the dataset, hold the reserve back at the door, and write the training record. No torch.

    `split` hands in the two sides instead of a dataset (`question.py`: another checkpoint's own split, rebuilt), and
    the reserve is held back from each.

    The record (`<checkpoint>.data/train.jsonl`) is what `laya_bridge.rows.trained_keys` reads to tell a clean row
    from a trained one, so it exists from here on: a fit that never finishes must be taken back (`discard`), or its
    rows would read as trained on by a checkpoint that does not exist."""
    dataset = dataset or base / DATA_DIRNAME / f"{surface}.jsonl"
    # Rows keep their label `key` (laya's loader reads state/questions/expected only): the training record then
    # names each text by the key labels use, which a shortened long hand-off's own state key is not.
    if split is not None:
        (train_rows, train_held), (held_out, eval_held) = (withhold_reserve(surface, base, side) for side in split)
        reserved = train_held + eval_held
    else:
        rows, reserved = withhold_reserve(surface, base, read_jsonl(dataset))
        train_rows, held_out = split_rows(rows)
    if reserved and split is None:
        print(
            f"[laya-lab] {surface}: {reserved} row(s) of the label budget's reserve held back from {dataset.name} "
            "(a current build leaves them out: this file is older, or was handed in)"
        )
    if len(train_rows) < 50 or len(held_out) < 10:
        raise SystemExit(f"{dataset}: {len(train_rows)} train / {len(held_out)} held-out rows — too few to fine-tune on")

    read_registry(base)  # the registration this ends in: checked (a v1 file migrated) before an hour of GPU
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    out = base / CHECKPOINTS_DIRNAME / f"{fine_tune_name(surface)}-{stamp}"
    if out.exists() or out.with_name(out.name + ".data").exists():
        raise SystemExit("checkpoint already exists; refusing to overwrite its weights or training record")
    # The training record: `laya_bridge.rows.trained_keys` reads train.jsonl to tell held-out rows apart.
    work = out.with_name(out.name + ".data")
    write_jsonl(work / "train.jsonl", train_rows)
    write_jsonl(work / "eval.jsonl", held_out)
    if split is None and surface == "agent_open_work":
        # Its states were built at the served question version (`open_work_data.build`); `question.run` writes its own.
        from laya_lab import question
        from tldrack import open_work

        question.write_record(work, open_work.QUESTION_VERSION, retrain=True, train=len(train_rows), eval=len(held_out))
    make_private_dirs(out)
    print(f"[laya-lab] {surface}: {len(train_rows)} train / {len(held_out)} held-out · {epochs} epochs → {out}")
    return Fit(surface, out, work, len(train_rows), len(held_out), reserved, epochs, device)


def fit(base: Path, plan: Fit, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Train and calibrate `plan` under the gpu lease; laya's train report, also written beside the weights.
    `on_progress` is told how far the fit is as it goes (`follow_fit`)."""
    from laya.train import TrainConfig, finetune  # torch: only once the run is going ahead

    infer.hold_device(plan.device, gb=TRAIN_GB, models=[plan.out.name])
    config = TrainConfig(epochs=plan.epochs, eval_data=str(plan.work / "eval.jsonl"), log_every=PROGRESS_EVERY)
    with follow_fit(on_progress):
        summary = finetune(
            str(plan.work / "train.jsonl"), str(base_checkpoint()), str(plan.out), config=config, device=plan.device
        )
    report = summary.get("train_report") or {}
    atomic_write_text(plan.out / REPORT_FILE, json.dumps(report, indent=2, default=str) + "\n")
    tighten(base)  # the checkpoint folder laya wrote under the umask
    return report


def read_report(checkpoint: Path) -> dict[str, Any]:
    """The train report `fit` left beside the weights; empty when it is gone."""
    try:
        report = json.loads((checkpoint / REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return report if isinstance(report, dict) else {}


def discard(base: Path, surface: str, checkpoint: str) -> bool:
    """Take back a fit that never finished: its folder and its training record. True when something was removed.
    Never a registered version, and never a name that is not one of the surface's checkpoint folders."""
    if not bridge_rows.checkpoint_name(fine_tune_name(surface)).fullmatch(checkpoint):
        return False
    if checkpoint in read_registry(base, migrate=False)["versions"]:
        return False
    root = (base / CHECKPOINTS_DIRNAME).resolve()
    removed = False
    for folder in (base / CHECKPOINTS_DIRNAME / checkpoint, base / CHECKPOINTS_DIRNAME / f"{checkpoint}.data"):
        if files.is_real_dir(folder) and folder.resolve().parent == root:
            shutil.rmtree(folder)
            removed = True
    return removed


def judge_fit(
    surface: str, base: Path, checkpoint: Path, report: dict[str, Any], device: str
) -> tuple[bool, str, dict[str, Any]]:
    """`(promote, why, tags)`: the promotion gate for a surface with a golden suite, else laya's own held-out rule."""
    if suite_for_surface(surface) is not None:
        return gate_verdict(surface, base, checkpoint, device)
    promote, why = report_verdict(report)
    return promote, why, lab_files.gate_tags("HELDOUT" if promote else "FORCED")


def register_fit(
    base: Path,
    surface: str,
    checkpoint: Path,
    report: dict[str, Any],
    tags: dict[str, Any],
    *,
    epochs: int,
    train_rows: int,
    held_out: int,
    as_: str = CHALLENGER,
    at: str | None = None,
) -> str:
    """Register a judged checkpoint. `at` is its training cutoff, the moment its training set was built (now, for a
    `train` by hand): what `registry.training_cutoff` reads to tell which operator grades are newer than it."""
    trained = lab_files.trained_entry(epochs, train_rows, held_out, base_revision(), at or bridge_rows.utc_stamp())
    return register(base, surface, checkpoint, compact_report(report), trained=trained, as_=as_, tags=tags)


def run(surface: str, base: Path, dataset: Path | None, *, epochs: int, device: str, force: bool, as_: str = CHALLENGER) -> int:
    plan = prepare(surface, base, dataset, epochs=epochs, device=device)
    out = plan.out
    report = fit(base, plan)
    promote, why, tags = judge_fit(surface, base, out, report, device)
    if promote or force:
        name = register_fit(
            base, surface, out, report, tags, epochs=epochs, train_rows=plan.train_rows, held_out=plan.held_out, as_=as_
        )
        print(f"[laya-lab] registered {out.name} as @{as_} ({why}{'; forced' if not promote else ''})")
        if as_ == CHALLENGER:
            print(
                f"[laya-lab] {name} is NOT served: the nightly pack shadow-scores it on live turn ends; "
                "a promotion moves @champion onto it (AGENT RACK › LAYA)"
            )
        else:
            print(f"[laya-lab] {name} → {out.name}: restart Laya to serve it (AGENT RACK › LAYA › RESTART)")
        print(
            "[laya-lab] a new checkpoint starts at the code's cut: re-sweep it once the operator has labels "
            "(AGENT RACK › LAYA › RE-SWEEP)"
        )
        return 0
    print(f"[laya-lab] NOT registered: {why}. Report: {out / REPORT_FILE}")
    return 1
