"""`laya-lab retrain agent-open-work`: the gated retrain, one command a scheduler can run every night, as a
durable step journal that ends in a @challenger. It never serves what it trains: a promotion is another act.

    laya-lab retrain agent-open-work          # resume the open cycle, or open one if a retrain is owed, else NO-OP
    laya-lab retrain agent-open-work --owed   # a scheduler's check: one JSON line, exit 0 nothing owed / 1 owed
    laya-lab retrain agent-open-work --now    # open a cycle off schedule (the two gates still hold)

OWED (`owed`), in the style of Label Studio's online-learning trigger: retrain when enough new labels arrived, not on
a clock. A new cycle opens when ALL hold:
- the two gates of "measure before training": the board holds 40 clean operator
  grades with 10 of each (`review_data.sweep_readiness`, the RE-SWEEP gate's own rule), and ATTENTION METRICS holds
  ATTENTION_DAYS whole days of signals (`attention_days`);
- MIN_NEW_GRADES clean operator grades are newer than the last training cutoff (`registry.training_cutoff`: the newer
  of @champion's and @challenger's, and never older than the last cycle this journal closed). Counted from
  @champion's cutoff alone, the grades of the label budget's reserve stay clean for good, so a
  waiting challenger or a refused candidate would be retrained on the same grades every week;
- it is Sunday OPEN_WINDOW on the machine's own clock (`clock.local`). `--now` skips this line and the one above.
An OPEN cycle is always owed: the next run resumes it.

THE JOURNAL (`retrain/<suite>.json`; its states and the rack's view of it are owned by `laya_bridge/lab_files.py`). Each
step's output is written (atomically) the moment it lands, so a reboot, a killed pane or a bridge restart costs the
step that was running and nothing before it: the next run starts at the first step that is not `done` or `skipped`.

1. `export`    new turn ends into the export (`open_work_data.export`), no more than the teacher can label this run.
2. `label`     the teacher labels them, in a child process, so its lease ends with it: Sonnet on the subscription
               under the week's cap, or the local teacher once it cleared its bar (`open_work_data.teacher_bar` and
               `WEEKLY_SONNET_LABELS` own both rules). A label that landed is never asked again (`label_todo`).
3. `build`     the training set (`open_work_data.write_set`): operator answers win, the label budget's reserve and
               every golden case stay out, the curated supplements join, operator rows are copied toward class
               balance. Its end is the cycle's TRAINING CUTOFF.
4. `train`     `train.prepare` + `train.fit`: DEFAULT_EPOCHS epochs under the gpu lease. A fit that was cut off is
               taken back first (`train.discard`), so its training record never marks a row as trained. While it
               runs the block holds `progress` (`train.follow_fit`), which the TRAINING page draws.
5. `gate`      the promotion gate against the served model (`train.judge_fit`; `gate.py` owns the checks).
6. `register`  `--as challenger`, never served. Skipped when the gate refused: the cycle then closes `refused`.
7. `sweep`     a RE-SWEEP dry run on the challenger, on operator labels it never trained on, which under the label
               budget are the reserve. Nothing is applied: a cut moves by the operator's armed press only.
8. `card`      `MODEL_CARD.md` beside the weights (`model_card`: Mitchell et al.'s sections).

A step that fails leaves the cycle `failed` and the next run retries that step; ATTEMPTS_MAX failures of one step
close the cycle `abandoned` (its unfinished fit taken back), so nothing waits on a person: the next owed retrain
starts over. A held lease is not a failure: the cycle is `waiting` and the run exits 75. Its gpu steps
start only in the lease's night window, whatever hour the cycle opened (no lease: always open, `lease.py`).

A cycle that closes leaves a copy of its journal in `evals/` (`lab_files.retrain_report_name`), stamped with the
scheduled run that closed it: what `laya-lab verdict retrain` reads. PASS a @challenger is registered · REFUSE the gate refused
the candidate · FAIL abandoned · NO-OP nothing was owed.

Exit codes: 0 the cycle closed `done` or `refused`, or nothing was owed (read the verdict) · 75 a lease is held
· 1 a step failed. Names, states and numbers only reach the journal: never a text.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab import clock, infer, registry
from laya_lab import open_work_data as data
from laya_lab.data import DEFAULT_EPOCHS, atomic_write_text, open_private
from laya_lab.golden import SUITES
from laya_bridge import lab_files, review_data, rows
from laya_bridge.files import clip
from laya_bridge.paths import CHECKPOINTS_DIRNAME, EVALS_DIRNAME
from tldrack import open_work, rack_metrics

KIND = "retrain"
STEPS = lab_files.RETRAIN_STEPS
PENDING, RUNNING, DONE, SKIPPED, FAILED = lab_files.RETRAIN_STEP_STATES
WAITING = "waiting"
CLOSED_DONE, CLOSED_REFUSED, CLOSED_ABANDONED = lab_files.RETRAIN_CLOSED_STATES
#: The steps that load a model on the gpu: they start in the lease's night window only.
GPU_STEPS = ("label", "train", "gate", "sweep")

#: New clean operator grades since the last training cutoff before a retrain is owed.
MIN_NEW_GRADES = 10
#: Whole days of attention signals before any training lane starts.
ATTENTION_DAYS = 7
#: A new cycle opens on this weekday (`datetime.weekday`: Sunday), inside OPEN_WINDOW, on the machine's own clock.
OPEN_WEEKDAY = 6
OPEN_WINDOW = ("00:30", "07:30")
#: Failures of ONE step before its cycle closes `abandoned`.
ATTEMPTS_MAX = 3
#: New turn ends an export takes when the local teacher labels (it has no weekly cap).
EXPORT_LIMIT = 600
WHY_MAX = 200
LEASE_REFUSED_EXIT = infer.LEASE_REFUSED_EXIT


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── owed ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Owed:
    """Whether a new cycle may open now, why in one line of names and numbers, and the numbers themselves."""

    owed: bool
    reason: str
    numbers: dict[str, Any] = field(default_factory=dict)


def attention_days(now: datetime, path: Path | None = None) -> int | None:
    """The whole local days before today on which the rack recorded an attention signal, or None when the store
    does not read. The store, its table and its PAUSE switch are owned by tldrack/rack_metrics.py; this opens
    it read-only, so a check never creates or migrates it."""
    path = path or rack_metrics.resolve_metrics_path()
    if not path.is_file():
        return None
    today = now.astimezone().strftime("%Y-%m-%d")  # the system's zone, as SQLite's 'localtime' below reads it
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            found = db.execute(
                "SELECT COUNT(DISTINCT date(at / 1000, 'unixepoch', 'localtime')) FROM signals "
                "WHERE date(at / 1000, 'unixepoch', 'localtime') < ?",
                (today,),
            ).fetchone()
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return None
    return int(found[0]) if found and isinstance(found[0], int) else None


def in_open_window(now: datetime) -> bool:
    local = clock.local(now)
    return local.weekday() == OPEN_WEEKDAY and OPEN_WINDOW[0] <= local.strftime("%H:%M") < OPEN_WINDOW[1]


def last_cutoff(base: Path, surface: str, closed: dict[str, Any] | None) -> tuple[datetime | None, str | None]:
    """`(cutoff, what it belongs to)`: the registry's newest training cutoff, or the cutoff of the last cycle this
    journal closed when that is newer (a refused or abandoned cycle registers nothing)."""
    cutoff, version = registry.training_cutoff(base, surface)
    built = registry.instant((((closed or {}).get("steps") or {}).get("build") or {}).get("at"))
    if built is not None and (cutoff is None or built > cutoff):
        return built, f"cycle {closed['id']}"
    return cutoff, version


def new_grades(base: Path, surface: str, fold: Any, cutoff: datetime | None) -> dict[str, int]:
    """The operator's clean grades newer than `cutoff` (every clean one when there is none), split by the label
    budget's halves: `train` is what the next training set gains, `reserve` what the next sweep does."""
    labels = {key: row["open"] for key, row in fold.labels.items()}
    clean = review_data.clean_label_rows(base, surface, labels, fold=fold)
    fresh = [
        key
        for key, _open, _text in clean.rows
        if cutoff is None or (registry.instant(fold.labels[key].get("at")) or cutoff) > cutoff
    ]
    reserve = sum(1 for key in fresh if key in clean.reserve)
    return {"new": len(fresh), "train": len(fresh) - reserve, "reserve": reserve}


def owed(
    name: str, base: Path, now: datetime, *, closed: dict[str, Any] | None = None, forced: bool = False, days: int | None = None
) -> Owed:
    """May a NEW cycle open (the module note owns the rule)? `days` is `attention_days` when the caller already
    read it. Unreadable sources stop the command (`open_work_data.require_sources`): unknown is never owed."""
    surface = SUITES[name].surface
    fold = data.require_sources(base, surface)
    ready = review_data.sweep_readiness(base, surface, fold)
    days = attention_days(now) if days is None else days
    cutoff, cutoff_of = last_cutoff(base, surface, closed)
    grades = new_grades(base, surface, fold, cutoff)
    numbers = {
        "clean": ready["rows"],
        "clean_open": ready["open"],
        "clean_done": ready["done"],
        "attention_days": days,
        "cutoff": rows.utc_stamp(cutoff) if cutoff is not None else None,
        "cutoff_of": cutoff_of,
        "new_grades": grades["new"],
        "new_train": grades["train"],
        "new_reserve": grades["reserve"],
        "forced": forced,
    }
    counted = f"{grades['new']} new clean grades ({grades['train']} train / {grades['reserve']} reserve)"
    if not ready["ready"]:
        return Owed(
            False,
            f"gate: {ready['rows']} clean grades ({ready['open']} open / {ready['done']} done), "
            f"a retrain needs {rows.SWEEP_MIN_ROWS} with {rows.SWEEP_MIN_PER_CLASS} of each",
            numbers,
        )
    if days is None or days < ATTENTION_DAYS:
        return Owed(
            False, f"gate: ATTENTION METRICS holds {days if days is not None else 'no'} of {ATTENTION_DAYS} days", numbers
        )
    if forced:
        return Owed(True, f"started off schedule · {counted}", numbers)
    if grades["new"] < MIN_NEW_GRADES:
        return Owed(False, f"{grades['new']} of {MIN_NEW_GRADES} new clean grades since the last training cutoff", numbers)
    if not in_open_window(now):
        return Owed(False, f"{counted} · opens Sunday {OPEN_WINDOW[0]}–{OPEN_WINDOW[1]} local time", numbers)
    return Owed(True, counted, numbers)


# ── the journal ───────────────────────────────────────────────────────────────


def is_open(journal: dict[str, Any] | None) -> bool:
    return journal is not None and journal["state"] in lab_files.RETRAIN_OPEN_STATES


def standing_at(journal: dict[str, Any]) -> str | None:
    """The first step that is not done or skipped: where the next run starts."""
    return next((step for step in STEPS if _block(journal, step).get("state") not in (DONE, SKIPPED)), None)


def _block(journal: dict[str, Any], step: str) -> dict[str, Any]:
    return journal["steps"].setdefault(step, {"state": PENDING, "attempts": 0})


def new_journal(name: str, device: str, decision: Owed, now: datetime) -> dict[str, Any]:
    stamp = rows.utc_stamp(now)
    return {
        "kind": KIND,
        "v": lab_files.RETRAIN_VERSION,
        "suite": name,
        "surface": SUITES[name].surface,
        "id": f"{lab_files.report_stamp(now)}-{secrets.token_hex(3)}",
        "state": RUNNING,
        "started_at": stamp,
        "updated_at": stamp,
        "closed_at": None,
        "device": device,
        "runs": 0,
        "question_version": open_work.QUESTION_VERSION,
        "owed": {"reason": decision.reason, **decision.numbers},
        "checkpoint": None,
        "model_digest": None,
        "dataset_version": None,
        "steps": {step: {"state": PENDING, "attempts": 0} for step in STEPS},
    }


def save(base: Path, journal: dict[str, Any], now: datetime | None = None) -> None:
    journal["updated_at"] = rows.utc_stamp(now)
    atomic_write_text(
        lab_files.retrain_journal_path(base, journal["suite"]), json.dumps(journal, indent=2, allow_nan=False, default=str) + "\n"
    )


def write_report(base: Path, name: str, body: dict[str, Any]) -> Path:
    """A closed cycle (or a run that found nothing owed) as `laya-lab verdict retrain` reads it."""
    report = {**body, "at": rows.utc_stamp(), "laya_version": infer.laya_version(), **lab_files.eval_run_field()}
    out = base / EVALS_DIRNAME / lab_files.retrain_report_name(name, lab_files.report_stamp())
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False, default=str) + "\n")
    return out


# ── the steps ─────────────────────────────────────────────────────────────────


def label_child(base: Path) -> int:
    """`laya-lab label-open-work --teacher auto --weekly` as a child of this run: its leases (the subscription slot,
    or the gpu for a local teacher's weights) end with it, before this process asks for the gpu to train."""
    return subprocess.call(
        [sys.executable, "-m", "laya_lab.cli", "label-open-work", "--teacher", "auto", "--weekly"], env=dict(os.environ)
    )


@dataclass
class Cycle:
    """One run's hold on a cycle: where it works, the journal it writes, and the seams a test swaps."""

    name: str
    base: Path
    journal: dict[str, Any]
    device: str = "mps"
    clock: Callable[[], datetime] = now_utc
    label: Callable[[Path], int] = label_child
    router: Any = None

    @property
    def surface(self) -> str:
        return SUITES[self.name].surface

    def checkpoint(self) -> Path:
        """The cycle's checkpoint folder, as the train step recorded it."""
        name = self.journal.get("checkpoint")
        if not isinstance(name, str) or not rows.checkpoint_name(registry.fine_tune_name(self.surface)).fullmatch(name):
            raise RuntimeError("the journal names no checkpoint: the train step has not finished")
        folder = self.base / CHECKPOINTS_DIRNAME / name
        if not (folder / registry.WEIGHTS_FILE).is_file():
            raise RuntimeError(f"{name} holds no weights: the train step must run again")
        return folder


def step_export(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    bar = data.teacher_bar(cycle.base)
    limit = EXPORT_LIMIT if bar.clear else data.weekly_left(cycle.base, cycle.clock())
    added, total, seen = data.export_turn_ends(cycle.base, data.transcript_roots(), limit=limit)
    return {"added": added, "total": total, "seen": seen, "limit": limit}


def step_label(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    before = len(data.read_label_calls(cycle.base))
    code = cycle.label(cycle.base)
    calls = data.read_label_calls(cycle.base)[before:]
    bar = data.teacher_bar(cycle.base)
    teacher = calls[-1]["teacher"] if calls else (bar.model if bar.clear else data.LABEL_MODEL)
    block.update(
        teacher=teacher,
        calls=sum(row.get("calls", 0) for row in calls if type(row.get("calls")) is int),
        asked=sum(row["asked"] for row in calls),
        labelled=sum(row.get("labelled", 0) for row in calls if type(row.get("labelled")) is int),
        weekly_left=data.weekly_left(cycle.base, cycle.clock()),
        teacher_why=clip(bar.why, WHY_MAX),
    )
    if code == LEASE_REFUSED_EXIT:
        raise SystemExit(LEASE_REFUSED_EXIT)
    if code != 0:
        raise RuntimeError(f"label-open-work exited {code}")
    return {}


#: The build's counts the journal keeps (`open_work_data.build_set`'s `stats`, minus its two strings).
BUILD_COUNTS = (
    "rows",
    "unique",
    "copies",
    "open",
    "done",
    "operator",
    "local",
    "curated",
    "curated_cases",
    "curated_held_out",
    "curated_conflicts",
    "skipped",
    "golden",
    "reserved",
    "train_open",
    "train_done",
)


def step_build(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    built = data.write_set(cycle.base)
    digest = hashlib.sha256(data.dataset_path(cycle.base).read_bytes()).hexdigest()
    cycle.journal["dataset_version"] = f"q{open_work.QUESTION_VERSION}.t{built.stats['rows']}.{digest[:12]}"
    return {**{key: built.stats[key] for key in BUILD_COUNTS}, "at": rows.utc_stamp(cycle.clock())}


def step_train(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    from laya_lab import train

    stale = block.get("checkpoint")
    if isinstance(stale, str) and train.discard(cycle.base, cycle.surface, stale):
        print(f"[laya-lab] retrain: took back {stale}, a fit that never finished")
    plan = train.prepare(cycle.surface, cycle.base, None, epochs=DEFAULT_EPOCHS, device=cycle.device)
    block.update(
        checkpoint=plan.out.name,
        train_rows=plan.train_rows,
        held_out_rows=plan.held_out,
        reserved=plan.reserved,
        epochs=plan.epochs,
    )
    cycle.journal.update(checkpoint=plan.out.name, model_digest=None)
    save(cycle.base, cycle.journal, cycle.clock())  # the folder is named before an hour of GPU: a kill can be taken back

    def on_progress(progress: dict[str, Any]) -> None:
        block["progress"] = progress
        save(cycle.base, cycle.journal, cycle.clock())

    report = train.compact_report(train.fit(cycle.base, plan, on_progress))
    block.pop("progress", None)  # the fit ended: its numbers below are what it left
    cycle.journal["model_digest"] = infer.checkpoint_digest(plan.out.name, plan.out)
    return {
        "eval_items": report["eval_items"],
        "before": report["before"],
        "after": report["after"],
        "base_revision": train.base_revision(),
    }


def step_gate(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    from laya_lab import train

    out = cycle.checkpoint()
    promote, why, tags = train.judge_fit(cycle.surface, cycle.base, out, train.read_report(out), cycle.device)
    return {
        "promote": bool(promote),
        "gate": tags["gate"] if promote else "REFUSED",
        "run": tags.get("gate_run"),
        "report": tags.get("gate_report"),
        "checks": tags.get("checks") or {},
        "why": clip(why, WHY_MAX),
    }


def step_register(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    from laya_lab import train

    out = cycle.checkpoint()
    gate, fit, build = (_block(cycle.journal, step) for step in ("gate", "train", "build"))
    tags = lab_files.gate_tags(gate["gate"], gate.get("run"), gate.get("report"), gate.get("checks"))
    held = registry.alias_target(cycle.base, cycle.surface, registry.CHALLENGER)
    name = train.register_fit(
        cycle.base,
        cycle.surface,
        out,
        train.read_report(out),
        tags,
        epochs=fit["epochs"],
        train_rows=fit["train_rows"],
        held_out=fit["held_out_rows"],
        as_=registry.CHALLENGER,
        at=build.get("at"),
    )
    return {"name": name, "alias": registry.CHALLENGER, "replaced": held[0] if held is not None and held[0] != name else None}


def step_sweep(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    from laya_lab import heldout

    checkpoint = cycle.checkpoint().name
    code = heldout.run_sweep(
        cycle.name, cycle.base, model=checkpoint, labels="operator", device=cycle.device, router=cycle.router
    )
    if code == heldout.REFUSED:
        return {"state": SKIPPED, "exit": code, "rows": 0, "why": "no clean operator grade the challenger never trained on yet"}
    if code != 0:
        raise RuntimeError(f"sweep exited {code}")
    target = registry.resolve_model(cycle.base, cycle.surface, checkpoint)[0]
    found = lab_files.newest_report(cycle.base, cycle.name, target, "heldout")
    view = lab_files.heldout_view(*found) if found else None
    if found is None or view is None:
        raise RuntimeError("the sweep wrote no report this could read")
    done = found[1].get("done") if isinstance(found[1].get("done"), dict) else {}
    return {
        "exit": code,
        "report": found[0].name,
        "best": view["best"],
        "done_best": done.get("at"),
        "rows": view["rows"],
        "reserve": (found[1].get("reserve") or {}).get("rows", 0),
        "kappa": view["kappa"],
    }


def step_card(cycle: Cycle, block: dict[str, Any]) -> dict[str, Any]:
    out = cycle.checkpoint()
    atomic_write_text(out / lab_files.MODEL_CARD_FILENAME, model_card(cycle.journal))
    return {"file": lab_files.MODEL_CARD_FILENAME}


BODIES: dict[str, Callable[[Cycle, dict[str, Any]], dict[str, Any]]] = {
    "export": step_export,
    "label": step_label,
    "build": step_build,
    "train": step_train,
    "gate": step_gate,
    "register": step_register,
    "sweep": step_sweep,
    "card": step_card,
}


# ── the model card ────────────────────────────────────────────────────────────


def _num(value: Any, digits: int = 4) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def model_card(journal: dict[str, Any]) -> str:
    """`MODEL_CARD.md` for the cycle's checkpoint, in Mitchell et al.'s sections (Model Cards for Model Reporting):
    details, intended use, training data, evaluation data, metrics, caveats. Pure: names and numbers from the
    journal, never a text from the operator's files."""
    steps = journal["steps"]
    label, build, fit, gate, register, sweep = (
        steps.get(step) or {} for step in ("label", "build", "train", "gate", "register", "sweep")
    )
    before, after = fit.get("before") or {}, fit.get("after") or {}
    registered = register.get("state") == DONE
    checks = " · ".join(f"{check} {state}" for check, state in sorted((gate.get("checks") or {}).items())) or "—"
    swept = (
        f"best ⚠️ cut {_num(sweep.get('best'), 2)}, ✅ cut {_num(sweep.get('done_best'), 2)} on {sweep.get('rows', 0)} operator rows "
        f"it never trained on ({sweep.get('reserve', 0)} of the label budget's reserve), κ {_num(sweep.get('kappa'), 3)}. "
        f"A dry run: nothing was applied."
        if sweep.get("state") == DONE
        else f"not swept: {sweep.get('why') or 'the cycle closed before the sweep'}."
    )
    lines = [
        f"# Model card · {journal.get('checkpoint')}",
        "",
        "## Model details",
        f"- Laya fine-tune for `{journal['surface']}` (question version {journal.get('question_version')}): ModernBERT-large "
        f"with a 2-layer decision head, from base revision `{fit.get('base_revision')}`.",
        f"- Trained {fit.get('epochs')} epochs on `{journal.get('device')}` by retrain cycle `{journal['id']}` "
        f"(opened {journal.get('started_at')}; training cutoff {build.get('at')}).",
        f"- Weights digest `{journal.get('model_digest')}` · training set `{journal.get('dataset_version')}`.",
        f"- Registry: {'@challenger, not served' if registered else 'not registered: the promotion gate refused it'}.",
        "",
        "## Intended use",
        "- Reads the closing message of a coding agent's turn and answers one yes/no question: does the session still "
        "have open work. The agent rack draws ⚠️ or ✅ from the probability at two cuts.",
        "- It HELPS a person decide where to look. It is not a reason to close, kill or auto-answer a session.",
        "- Out of scope: any text but an English turn-ending message; any decision without a person in the loop.",
        "",
        "## Training data",
        f"- {build.get('unique')} labelled turn ends ({build.get('open')} open / {build.get('done')} done), {build.get('rows')} rows "
        f"with {build.get('copies')} operator copies for class balance: the train split stands at "
        f"{build.get('train_open')} open / {build.get('train_done')} done.",
        f"- Labels: {build.get('operator')} from the operator (the TRAIN half of the label budget; {build.get('reserved')} more are "
        f"held in reserve and never trained on), {build.get('curated')} from the curated supplements, the rest from the teacher "
        f"(`{label.get('teacher')}`; {label.get('labelled', 0)} new this cycle).",
        f"- Held out of training: {build.get('golden')} golden cases, the reserve, and {fit.get('held_out_rows')} rows of the "
        f"15% split by state hash ({fit.get('train_rows')} rows trained).",
        "",
        "## Evaluation data",
        f"- Its own held-out split: {fit.get('held_out_rows')} rows.",
        f"- The promotion gate (`{gate.get('report')}`): public golden cases and labelled turn ends neither this "
        "checkpoint nor the served one trained on, each model at the cuts it would serve at.",
        "- The RE-SWEEP dry run: the operator's clean grades this checkpoint never trained on.",
        "",
        "## Metrics",
        f"- Held-out, base → fine-tune: loss {_num(before.get('loss'))} → {_num(after.get('loss'))} · accuracy "
        f"{_num(before.get('accuracy'), 3)} → {_num(after.get('accuracy'), 3)} · Brier {_num(before.get('brier'))} → "
        f"{_num(after.get('brier'))} · ECE {_num(before.get('ece'))} → {_num(after.get('ece'))}.",
        f"- Promotion gate: {gate.get('gate')} · {checks}.",
        f"- Cut: {swept}",
        "",
        "## Caveats",
        "- Most labels come from a teacher model that read the same message alone, so the model inherits the teacher's "
        "reading wherever the operator has not graded.",
        "- A new checkpoint starts at the code's cuts; its own cut is swept only on rows it never trained on.",
        "- The held-out split and the gate's rows are small: read every number with its row count.",
        "- A message past the model's window is judged from its head and tail, not in full.",
        "- A @challenger is shadow-scored on live turn ends before any promotion; this card is not a promotion.",
        "",
    ]
    return "\n".join(lines)


# ── the run ───────────────────────────────────────────────────────────────────


def _fail(cycle: Cycle, step: str, block: dict[str, Any], error: BaseException, started: float) -> int:
    """A failed step: the cycle stays open for a retry, or closes `abandoned` at ATTEMPTS_MAX."""
    block.update(
        state=FAILED,
        error=type(error).__name__,
        why=infer.why_line(error),
        ended_at=rows.utc_stamp(cycle.clock()),
        seconds=round(time.monotonic() - started, 1),
    )
    print(f"[laya-lab] retrain: {step} failed ({block['error']}: {block['why']})", file=sys.stderr)
    if block["attempts"] < ATTEMPTS_MAX:
        cycle.journal["state"] = FAILED
        save(cycle.base, cycle.journal, cycle.clock())
        return 1
    stale = _block(cycle.journal, "train").get("checkpoint")
    if isinstance(stale, str) and _block(cycle.journal, "register").get("state") != DONE:
        from laya_lab import train

        if train.discard(cycle.base, cycle.surface, stale):
            print(f"[laya-lab] retrain: took back {stale}")
    close(cycle, CLOSED_ABANDONED)
    return 1


def close(cycle: Cycle, state: str) -> Path:
    cycle.journal.update(state=state, closed_at=rows.utc_stamp(cycle.clock()))
    save(cycle.base, cycle.journal, cycle.clock())
    out = write_report(cycle.base, cycle.name, cycle.journal)
    for line in report_lines(cycle.journal):
        print(line)
    print(f"  report → {out}")
    return out


def work(cycle: Cycle) -> int:
    """Run the cycle from the step it stands at to its close (the module note owns the exit codes)."""
    journal = cycle.journal
    journal.update(state=RUNNING, runs=int(journal.get("runs") or 0) + 1, device=cycle.device)
    for step in STEPS:
        block = _block(journal, step)
        if block.get("state") in (DONE, SKIPPED):
            continue
        refused = step in ("register", "sweep") and _block(journal, "gate").get("promote") is False
        if refused:
            block.update(state=SKIPPED, why="the promotion gate refused the candidate")
            save(cycle.base, journal, cycle.clock())
            continue
        if step in GPU_STEPS and not infer.lease_module().night_window_open(cycle.clock().timestamp()):
            journal["state"] = WAITING
            save(cycle.base, journal, cycle.clock())
            return LEASE_REFUSED_EXIT
        for stale in ("error", "why", "ended_at", "seconds"):
            block.pop(stale, None)
        block.update(state=RUNNING, attempts=int(block.get("attempts") or 0) + 1, started_at=rows.utc_stamp(cycle.clock()))
        save(cycle.base, journal, cycle.clock())
        started = time.monotonic()
        try:
            result = BODIES[step](cycle, block)
        except SystemExit as stop:
            if stop.code == LEASE_REFUSED_EXIT:  # a busy machine is no attempt: the step waits its turn
                block.update(state=PENDING, attempts=block["attempts"] - 1)
                journal["state"] = WAITING
                save(cycle.base, journal, cycle.clock())
                print(f"[laya-lab] retrain: {journal['id']} waits at {step} for a lease", file=sys.stderr)
                return LEASE_REFUSED_EXIT
            return _fail(cycle, step, block, stop, started)
        except Exception as error:  # noqa: BLE001 (torch and laya raise their own; any of them is this step's failure)
            return _fail(cycle, step, block, error, started)
        block.update(
            {"state": DONE, **result, "ended_at": rows.utc_stamp(cycle.clock()), "seconds": round(time.monotonic() - started, 1)}
        )
        save(cycle.base, journal, cycle.clock())
    close(cycle, CLOSED_REFUSED if _block(journal, "gate").get("promote") is False else CLOSED_DONE)
    return 0


def run(name: str, base: Path, **kwargs: Any) -> int:
    # One cycle at a time, whoever started the run: a scheduler or a terminal.
    with open_private(lab_files.retrain_journal_path(base, name).with_suffix(".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return LEASE_REFUSED_EXIT
        return run_locked(name, base, **kwargs)


def run_locked(
    name: str,
    base: Path,
    *,
    device: str = "mps",
    forced: bool = False,
    now: datetime | None = None,
    label: Callable[[Path], int] = label_child,
    router: Any = None,
) -> int:
    clock = (lambda: now) if now is not None else now_utc
    journal = lab_files.read_retrain_journal(base, name)
    if journal is None and lab_files.retrain_journal_path(base, name).exists():
        raise ValueError("retrain journal is unreadable; refusing to replace its unfinished steps")
    if is_open(journal):
        if journal.get("question_version") != SUITES[name].question_version:
            raise ValueError("retrain rubric changed; preserve this journal and start a reviewed new cycle")
        print(f"[laya-lab] retrain: resuming {journal['id']} at {standing_at(journal)} (run {int(journal.get('runs') or 0) + 1})")
    else:
        decision = owed(name, base, clock(), closed=journal, forced=forced)
        if not decision.owed:
            out = write_report(
                base,
                name,
                {
                    "kind": KIND,
                    "v": lab_files.RETRAIN_VERSION,
                    "suite": name,
                    "surface": SUITES[name].surface,
                    "state": lab_files.RETRAIN_NOOP,
                    "device": device,
                    "owed": {"reason": decision.reason, **decision.numbers},
                    "steps": {},
                },
            )
            print(f"[laya-lab] retrain {name} · NO-OP · {decision.reason}")
            print(f"  report → {out}")
            return 0
        journal = new_journal(name, device, decision, clock())
        print(f"[laya-lab] retrain: opening {journal['id']} · {decision.reason}")
    return work(Cycle(name, base, journal, device=device, clock=clock, label=label, router=router))


def check(
    name: str, base: Path | None, now: datetime | None = None, *, scheduled_gpu_open: bool | None = None
) -> tuple[bool, str]:
    """`(fresh, reason)` for a scheduler's check: fresh while nothing is owed. An open cycle is owed, except while it
    stands at a gpu step outside the lease's night window: a run started now would only be refused the gpu."""
    if base is None or not base.is_dir():
        return True, "Laya is not set up on this machine"
    now = now or now_utc()
    journal = lab_files.read_retrain_journal(base, name)
    if journal is None and lab_files.retrain_journal_path(base, name).exists():
        raise ValueError("retrain journal is unreadable; refusing to replace its unfinished steps")
    if is_open(journal):
        step = standing_at(journal)
        gpu_open = infer.lease_module().night_window_open(now.timestamp()) if scheduled_gpu_open is None else scheduled_gpu_open
        if step in GPU_STEPS and not gpu_open:
            return True, f"{journal['id']} waits at {step} for the gpu night window"
        return False, f"resume {journal['id']} at {step}"
    decision = owed(name, base, now, closed=journal)
    return not decision.owed, decision.reason


def report_lines(journal: dict[str, Any]) -> list[str]:
    """Names, states and numbers: no text from the operator's files."""
    owed_block = journal.get("owed") or {}
    lines = [
        f"[laya-lab] retrain {journal['suite']} · {journal['state']} · {journal.get('id', '-')}"
        + (f" · {journal['checkpoint']}" if journal.get("checkpoint") else "")
        + (f" · {owed_block['reason']}" if owed_block.get("reason") else "")
    ]
    for step in STEPS:
        block = (journal.get("steps") or {}).get(step)
        if not block:
            continue
        detail = block.get("why") or ""
        if step == "export" and block.get("state") == DONE:
            detail = f"+{block.get('added')} turn ends ({block.get('total')} exported)"
        elif step == "label" and "teacher" in block:
            detail = (
                f"{block['teacher']} · {block.get('labelled', 0)}/{block.get('asked', 0)} labelled in {block.get('calls', 0)} calls"
                + (f" · {detail}" if detail else "")
            )
        elif step == "build" and block.get("state") == DONE:
            detail = (
                f"{block['rows']} rows ({block['open']} open / {block['done']} done · {block['operator']} operator · "
                f"{block['reserved']} reserved · {block['curated']} curated · {block['copies']} copies)"
            )
        elif step == "train" and "train_rows" in block:
            after = block.get("after") or {}
            detail = (
                f"{block['train_rows']} train / {block['held_out_rows']} held-out"
                + (f" · loss {_num(after.get('loss'))} · accuracy {_num(after.get('accuracy'), 3)}" if after else "")
                + (f" · {detail}" if detail else "")
            )
        elif step == "gate" and "gate" in block:
            detail = f"{block['gate']} · {detail}"
        elif step == "sweep" and block.get("state") == DONE:
            detail = f"best {block.get('best')} on {block.get('rows')} rows ({block.get('reserve')} reserve) · dry run"
        seconds = f"{block['seconds']:>7}s" if "seconds" in block else " " * 8
        lines.append(f"  {step:<9} {block.get('state', PENDING):<8} {seconds}  {detail}")
    return lines
