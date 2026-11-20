"""`laya-lab bench`: every voter that can answer on this machine, on one protocol, as one public results file.

    laya-lab bench                                    # agent-open-work: base Laya, the served fine-tune, the floor
    laya-lab bench --voter mica=http://127.0.0.1:8010,runtime=mlx,licence=Apache-2.0
    laya-lab bench --dry-run                          # the plan (voters, sets, n); loads nothing, writes nothing
    laya-lab bench --challenger <checkpoint>          # also an unregistered fine-tune, at its own question version
    laya-lab bench --panel served --panel base        # also the panel: many questions of a turn, one fitted line
    laya-lab bench --panel served/3q                  # also three questions and no line: the most confident answers

Two sets. `golden`: the public cases checked in under `lab/golden/`, always. `heldout`: the operator's labelled turn
ends the served fine-tune never trained on (`compare.heldout_rows`), only when the store holds some.

Voters, each under a public name:
- `laya-base`: the base Laya model. `laya-fine-tune`: the registry's champion for the surface, when there is one.
- `tfidf-floor`: the TF-IDF floor (`floor.py`), trained on the served checkpoint's own training rows.
- each `--voter <name>=<loopback url>[,key=value…]`: a `/v1/systemone` server (`infer.EndpointRouter`). The keys say
  what the lab cannot know about it: `kind` (zero_shot | fine_tune), `runtime`, `licence`, `params_m`, `labels`, `label`.
  `<name>=votes:<model>[/<arm>]` in the URL's place is a Claude model's recorded votes, read from a file and never
  asked here: owned by `claude_voter.py` — see the note there.
- `mean.<laya voter>+<name>`: the mean p of each Laya voter with each endpoint voter.
- each `--challenger <checkpoint>`: `laya-fine-tune-q<version>`, an unregistered fine-tune asked the question version
  its training record was built at (`question.py`), of the same rows: every row keeps its raw state (`Rows.raws`) for
  it. The protocol stays the served version's; its description names each challenger and its version. No mean. A
  checkpoint `train` made (a retrain on the labels since, its record says `retrain`), or a rebuild of one, is
  `laya-retrain-q<version>`.
- each `--panel <served | base>[/<shape>]`: `laya-panel` (the served model answers every question) or `laya-panel-base`
  (the base model answers the new ones). The shape is `line` when left out: every question and one fitted line, which
  answers a set at a time because it is fitted out of fold on the held-out rows. `3q` and `3q-mean` are three questions
  and no line (`laya-panel-3q`, `laya-panel-3q-mean`, each with its `-base`), which need no held-out rows. Owned by
  `panel.py` — see the note there. No mean.
A voter that cannot run is SKIPPED with a one-line reason, in the run's output; it never fails the run.

THE FILE is `bench/results/<YYYY-MM-DD>.json` in this checkout, one per day (a second run that day replaces it). Its
shape: owned by `schemas/src/bench-results.ts` — see the note there. Every key this module writes is spelled in the
"the file" section below and nowhere else, so a rename there is one edit here. **Aggregates only**: a row's id, key and
text stop at `Answers`, which holds p and labels; `write_results` then refuses a file in which any of them appears.
A voter is named by its public name, never by a checkpoint folder.

THE PROTOCOL ID is `<suite>.q<question version>.g<golden set>[.h<rows>-<held-out set>]`. Both set versions are digests
of what was scored (the states and their labels), so two files carry one id exactly when they asked the same question
of the same rows. The golden set here is the public file alone: every case not tagged provisional
(`lab_files.PROVISIONAL_TAG`), whatever this operator has agreed, so a stranger's run scores the same rows.

THE INTERVALS are one seeded percentile bootstrap over rows (`bootstrap`): AUC, Brier and the cost at the operating
point are redrawn from the SAME resamples for every voter of a set, so two voters' intervals are paired.

Exit 0 written (or planned) · 1 no voter answered, or the file was refused · 3 a voter spec refused.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import compare, rack, infer
from laya_lab import panel as panel_voter
from laya_lab import floor as floor_voter
from laya_lab.golden import SUITES, Suite, load_cases
from laya_lab.latency import spread
from laya_lab.metrics import at_cut, auc, brier, ece, fmt_num, pick_cut, reliability, warning_costs
from laya_lab.registry import resolve_model, served_model, served_threshold
from laya_bridge import agreement, files, lab_files, paths, review_log, rows

REFUSED = 3
RESULTS_VERSION = 1
RESULTS_DIR = ("bench", "results")

BASE, FINE_TUNE, FLOOR = "laya-base", "laya-fine-tune", "tfidf-floor"
ENSEMBLE_PREFIX = "mean."
#: The schema's enums and bounds (`BENCH_VOTER_KINDS`, `BENCH_RUNTIMES`, `BENCH_VOTERS_MAX`, `BENCH_ROC_MAX`).
KINDS = ("fine_tune", "base", "zero_shot", "floor", "ensemble", "panel")
RUNTIMES = ("mps", "cuda", "cpu", "mlx", "ollama", "llama.cpp", "none")
DEVICES = ("mps", "cuda", "cpu")
VOTERS_MAX = 16
ROC_MAX = 512
LICENCE = re.compile(r"[A-Za-z0-9.+-]{1,40}")
LABEL_MAX = 40
#: The base model's weights (`convaiinnovations/laya`), as its source states them: lab/NOTICE has the check. A
#: fine-tune keeps the architecture, so the parameter count; its weights are the operator's own and carry no
#: licence here until the operator ships them.
LAYA_LICENCE = "Apache-2.0"
LAYA_PARAMS_M = 421.3
#: The name the rack asks its question under (`infer.predict_noul`).
QUESTION_NAME = "q"

BOOTSTRAP_SEED = 20261113
BOOTSTRAP_RESAMPLES = 2000
LEVEL = 0.95
ECE_BINS = 10
#: `metrics.reliability` puts an edge `band` either side of each cut: these centres and half a bin give the ten equal
#: bins ECE sums over, so the diagram and the ECE beside it read the same rows.
EQUAL_BIN_CENTRES = tuple(round((i + 0.5) / ECE_BINS, 4) for i in range(ECE_BINS))
#: A row id or text shorter than this cannot be told from a field name; the guard checks the longer ones.
GUARD_MIN_CHARS = 8

#: From the checkout's root, on a new empty data directory: the public set, the base model, nothing of the operator's.
REPRODUCE = "TLDRACK_DATA_DIR=$(mktemp -d) uv run --project lab laya-lab bench {suite}"

Row = tuple[str, Any, bool]
Predict = Callable[[Any], float]


class Rows(list):
    """One set's rows `(id, state, expected)`, the state built at the protocol's question version, and beside them each
    row's raw state (`raws`: the case's text and its request), from which a challenger builds its own version's."""

    def __init__(self, rows: Sequence[Row] = (), raws: Sequence[Any] = ()) -> None:
        super().__init__(rows)
        self.raws = list(raws)


class Unavailable(RuntimeError):
    """A voter that cannot run here. Its message is the one-line reason: public names and numbers only."""


@dataclass
class BenchVoter:
    """One voter. `open` is the only place a model loads: it returns p(open) of one state."""

    name: str
    label: str
    kind: str
    runtime: str
    open: Callable[[], Predict] | None = None
    #: The cut it serves with; None has none of its own, and is swept on each set (in-sample, and marked so).
    threshold: float | None = None
    of: str | None = None
    labels: int | None = None
    params_m: float | None = None
    licence: str | None = None
    served: bool = False
    #: An ensemble's two voters: it answers with the mean of their p.
    members: tuple[str, str] | None = None
    #: A challenger asked another question version than the protocol's: its state from a row's raw state.
    restate: Callable[[Any], Any] | None = None
    question_version: int | None = None
    #: A challenger trained on its own rows and split (`train`), not a rebuild of the served model's.
    retrain: bool = False
    #: An endpoint voter's router, which times its own calls (`compare.Timed`): a votes file's are the recorded ones.
    timed: Any = None
    #: A voter that answers every set at once, labels in hand, in `open`'s place (a panel: `panel.py` owns why).
    whole: Callable[[Mapping[str, Sequence[Row]]], dict[str, Answers]] | None = None
    #: A panel's shape (`PANEL_SHAPES`).
    shape: str | None = None


@dataclass
class Plan:
    voters: list[BenchVoter]
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass
class Answers:
    """What a voter said on one set. No id, key or text gets past here."""

    probabilities: list[float]
    ms: list[float]


# ── the plan: which voters can answer here ────────────────────────────────────


def parse_voter(spec: str) -> dict[str, Any]:
    """`<name>=<loopback url | votes:<model>[/<arm>]>[,key=value…]` → `{name, url, kind, runtime, licence, params_m,
    labels, label}`.
    The name and the URL are `compare.parse_spec`'s to refuse; a Laya or floor spec is refused, because the bench
    names those voters itself and a checkpoint folder never names one in a public file."""
    head, *rest = spec.split(",")
    if head == compare.FLOOR or head.startswith(compare.LAYA):
        raise compare.Refused(
            f"{spec!r}: the bench adds its own Laya and floor voters; --voter is <name>=<loopback url | votes:<model>>"
        )
    name, url = compare.parse_spec(head)
    if name == FLOOR or name.startswith(ENSEMBLE_PREFIX):
        raise compare.Refused(f"{name!r} is a name the bench writes itself")
    out: dict[str, Any] = {
        "name": name,
        "url": url,
        "kind": "zero_shot",
        "runtime": "none",
        "licence": None,
        "params_m": None,
        "labels": None,
        "label": name.upper(),
    }
    for part in rest:
        key, sep, value = part.partition("=")
        try:
            if not sep or key not in out or key in ("name", "url"):
                raise ValueError("an unknown key")
            if key == "kind" and value not in ("zero_shot", "fine_tune"):
                raise ValueError("zero_shot or fine_tune")
            if key == "runtime" and value not in RUNTIMES:
                raise ValueError(f"one of {', '.join(RUNTIMES)}")
            if key == "licence" and not LICENCE.fullmatch(value):
                raise ValueError("an SPDX id")
            if key == "label" and not 0 < len(value) <= LABEL_MAX:
                raise ValueError(f"1 to {LABEL_MAX} characters")
            if key == "params_m":
                out[key] = float(value)
                if not (math.isfinite(out[key]) and out[key] > 0):
                    raise ValueError("a positive number of millions")
            elif key == "labels":
                out[key] = int(value)
                if out[key] < 0:
                    raise ValueError("a count")
            else:
                out[key] = value
        except ValueError as error:
            raise compare.Refused(f"{spec!r}: {part!r} is not {error}") from None
    if (out["kind"] == "fine_tune") != (out["labels"] is not None):
        raise compare.Refused(f"{spec!r}: labels=<operator labels it trained on> goes with kind=fine_tune, and only with it")
    return out


class SharedRouter:
    """The one Laya router of a run, opened by the first Laya voter that scores: torch loads there, never in the plan."""

    def __init__(self, base: Path, device: str, targets: dict[str, Path | None], router: Any = None) -> None:
        self.base, self.device, self.targets, self.router = base, device, targets, router

    def get(self) -> Any:
        if self.router is None:
            self.router = infer.open_router(self.base, self.device, self.targets)
        return self.router


def asks(router: Callable[[], Any], model: str, suite: Suite) -> Callable[[], Predict]:
    """An `open` over a router that answers like `laya.Router`: one state in, its p(open) out."""
    return lambda: lambda state: infer.predict_noul(router(), model, [state], suite.question)[0]


def operator_labels_trained(base: Path, suite: Suite, checkpoint: str) -> int | None:
    """How many operator labels a checkpoint trained on: the count its retrain cycle built with, else the operator's
    labelled keys in its training record. None when neither can say."""
    journal = lab_files.read_retrain_journal(base, suite.name)
    if journal is not None and journal.get("checkpoint") == checkpoint:
        steps = journal.get("steps")
        build = steps.get("build") if isinstance(steps, dict) else None
        if isinstance(build, dict) and files.is_count(build.get("operator")):
            return build["operator"]
    trained = rows.checkpoint_keys(base, checkpoint)
    if trained is None:
        return None
    _code, fold = review_log.read_fold(base, suite.surface)
    return sum(1 for key in fold.labels if key in trained)


def open_floor(base: Path, suite: Suite, voter: BenchVoter) -> Predict:
    """Fit the floor (`compare.floor_of`: on the served checkpoint's own rows) and take the cut it fitted."""
    fitted, why = compare.floor_of(base, suite)
    if fitted is None:
        raise Unavailable(why or "the floor could not train")
    voter.threshold = fitted.live_cut
    return asks(lambda: fitted.router, fitted.model, suite)()


#: A challenger's public name: `laya-fine-tune-q<version>`, and `-<n>` after it for a second one of that version.
CHALLENGER_PREFIX = f"{FINE_TUNE}-q"
#: A retrain's (`train` on the labels since, with its own split): `laya-retrain-q<version>`, numbered the same way.
RETRAIN_PREFIX = "laya-retrain-q"


def plan_challengers(
    base: Path, suite: Suite, checkpoints: Sequence[str], device: str, shared: SharedRouter, targets: dict[str, Path | None]
) -> list[BenchVoter]:
    """One voter per unregistered fine-tune named, each asked at the question version its record says it was built at
    (`question.read_question`). Never served. Raises `compare.Refused` on a checkpoint that is not one, or says no
    version."""
    from laya_lab import question

    voters: list[BenchVoter] = []
    for checkpoint in checkpoints:
        try:
            name, path = resolve_model(base, suite.surface, checkpoint)
        except SystemExit as error:
            raise compare.Refused(str(error)) from None
        record = question.read_record(base, name) if path is not None else None
        version = None if record is None else record["question_version"]
        if version is None or suite.build_state_at is None:
            raise compare.Refused(
                f"{checkpoint}: its record says no question version (`laya-lab question-challenger` writes one)"
            )
        targets[name] = path
        # A rebuild of a retrain's rows at another version (`question-challenger --like`) is that retrain's, too.
        like = record.get("like")
        retrain = record.get("retrain") is True or (
            isinstance(like, str) and (question.read_record(base, like) or {}).get("retrain") is True
        )
        public = f"{RETRAIN_PREFIX if retrain else CHALLENGER_PREFIX}{version}"
        taken = sum(1 for voter in voters if voter.name.startswith(public))
        public += f"-{taken + 1}" if taken else ""
        labels = operator_labels_trained(base, suite, name)
        build = suite.build_state_at
        # At the served version a rebuild is the control (the same rows refitted), and says so; a retrain is new rows.
        kind = "RETRAIN" if retrain else "REFIT" if version == suite.question_version else "FINE-TUNE"
        voters.append(
            BenchVoter(
                public,
                f"LAYA · {kind} · Q{version}",
                "fine_tune",
                device,
                asks(shared.get, name, suite),
                threshold=suite.threshold,
                of=BASE,
                labels=labels or 0,
                params_m=LAYA_PARAMS_M,
                restate=lambda raw, version=version, build=build: build(raw, version),
                question_version=version,
                retrain=retrain,
            )
        )
    return voters


#: `--panel <who>[/<shape>]`. Who answers the new questions; and the shape, a key of `PANEL_SHAPES`: the fitted line
#: over every question (None), or the three questions under one of `panel.RULES`.
PANEL_WHO = ("served", "base")
LINE = "line"
PANEL_SHAPES: dict[str, str | None] = {LINE: None, "3q": "confident", "3q-mean": "mean"}


def parse_panel(spec: str) -> tuple[str, str]:
    """`<who>[/<shape>]` → `(who, shape)`. Raises `compare.Refused` on anything else."""
    who, _sep, shape = spec.partition("/")
    shape = shape or LINE
    if who not in PANEL_WHO or shape not in PANEL_SHAPES:
        raise compare.Refused(f"--panel {spec!r}: <{' | '.join(PANEL_WHO)}>[/<{' | '.join(PANEL_SHAPES)}>]")
    return who, shape


def panel_name(who: str, shape: str) -> str:
    """A panel's public name: `laya-panel[-<shape>][-base]`."""
    return panel_voter.NAME + ("" if shape == LINE else f"-{shape}") + ("" if who == "served" else "-base")


def panel_label(who: str, shape: str) -> str:
    """A panel's label says how many questions it asks, so a legend needs no key. Short on purpose: the evals legend
    keeps a name on one line, and a longer one pushes its numbers out of a figure."""
    asked = len(panel_voter.ASKS if shape == LINE else panel_voter.THREE)
    tail = [part for part, said in (("MEAN", PANEL_SHAPES[shape] == "mean"), ("BASE", who == "base")) if said]
    return " · ".join(["LAYA", f"{asked} Q" if tail else f"{asked} QUESTIONS", *tail])


def ask_panel(
    asker: panel_voter.Asker, suite: Suite, name: str, served: str, others: str
) -> Callable[[Mapping[str, Sequence[Row]]], dict[str, Answers]]:
    """A panel's `whole`: every set's raw states and labels to `panel.answer_sets`, its refusals as `Unavailable`."""

    def whole(sets: Mapping[str, Sequence[Row]]) -> dict[str, Answers]:
        shaped: dict[str, panel_voter.Set] = {}
        for set_name, data in sets.items():
            raws = getattr(data, "raws", None)
            if raws is None or len(raws) != len(data):
                raise Unavailable(f"the {set_name} set carries no raw states to ask the panel's questions of")
            shaped[set_name] = (
                [row_id for row_id, _state, _expected in data],
                raws,
                [state for _id, state, _expected in data],
                [expected for _id, _state, expected in data],
            )
        try:
            found, line = panel_voter.answer_sets(asker, suite.question, shaped, served, others)
        except ValueError as refusal:
            raise Unavailable(str(refusal)) from None
        print(f"[laya-lab] bench: {name} weights, fitted on every held-out row (standardised, largest first)")
        for line_text in panel_voter.weight_lines(line):
            print(line_text)
        return {set_name: Answers(ps, ms) for set_name, (ps, ms) in found.items()}

    return whole


def ask_three(
    asker: panel_voter.Asker, suite: Suite, name: str, served: str, others: str, rule: str
) -> Callable[[Mapping[str, Sequence[Row]]], dict[str, Answers]]:
    """A three-question panel's `whole`: every set's closing states to `panel.answer_three`. Nothing is fitted, so it
    refuses no set; the labels are read only to print what each of the three votes was worth alone."""

    def whole(sets: Mapping[str, Sequence[Row]]) -> dict[str, Answers]:
        closings = {set_name: [state for _id, state, _expected in data] for set_name, data in sets.items()}
        found = panel_voter.answer_three(asker, suite.question, closings, served, others, rule)
        for set_name, (_ps, _ms, votes) in found.items():
            print(f"[laya-lab] bench: {name} votes on the {set_name} set")
            for line_text in panel_voter.vote_lines(votes, [expected for _id, _state, expected in sets[set_name]]):
                print(line_text)
        return {set_name: Answers(ps, ms) for set_name, (ps, ms, _votes) in found.items()}

    return whole


def plan_panels(suite: Suite, panels: Sequence[str], device: str, shared: SharedRouter, champion: str, plan: Plan) -> None:
    """Each `--panel` as a voter (`panel.py` owns what one is). `served`: the served model answers every question.
    `base`: the base model answers the new ones, which is the same panel when no fine-tune is served."""
    asker = panel_voter.Asker(shared.get)
    for who, shape in dict.fromkeys(parse_panel(spec) for spec in panels):
        name = panel_name(who, shape)
        if who == "base" and champion == "english":
            plan.skipped[name] = f"no fine-tune is served, so it is {panel_name('served', shape)}"
            continue
        others = champion if who == "served" else "english"
        rule = PANEL_SHAPES[shape]
        plan.voters.append(
            BenchVoter(
                name,
                panel_label(who, shape),
                "panel",
                device,
                of=BASE if champion == "english" else FINE_TUNE,
                params_m=LAYA_PARAMS_M,
                licence=LAYA_LICENCE if champion == "english" else None,
                whole=(
                    ask_panel(asker, suite, name, champion, others)
                    if rule is None
                    else ask_three(asker, suite, name, champion, others, rule)
                ),
                shape=shape,
            )
        )


def plan_voters(
    base: Path,
    suite: Suite,
    specs: Sequence[str] = (),
    *,
    device: str = "mps",
    router: Any = None,
    challengers: Sequence[str] = (),
    panels: Sequence[str] = (),
) -> Plan:
    """Every voter this machine can ask, in the file's order, and the ones it cannot with the reason. Reads the
    registry and counts rows; loads no model. Raises `compare.Refused` on a spec it will not run."""
    extras = [parse_voter(spec) for spec in specs]
    names = [extra["name"] for extra in extras]
    if len(set(names)) != len(names):
        raise compare.Refused("two --voter specs share a name")
    plan = Plan([])
    surface = suite.surface
    champion = served_model(base, surface)
    targets: dict[str, Path | None] = {"english": None}
    checkpoint: Path | None = None
    if champion != "english":
        champion, checkpoint = resolve_model(base, surface, "served")
        targets[champion] = checkpoint
    shared = SharedRouter(base, device, targets, router)
    measured = plan_challengers(base, suite, challengers, device, shared, targets)

    plan.voters.append(
        BenchVoter(
            BASE,
            "LAYA · BASE",
            "base",
            device,
            asks(shared.get, "english", suite),
            threshold=served_threshold(base, surface, "english", code=suite.threshold)[0],
            params_m=LAYA_PARAMS_M,
            licence=LAYA_LICENCE,
            served=checkpoint is None,
        )
    )
    if checkpoint is None:
        plan.skipped[FINE_TUNE] = f"no champion is registered for {surface}"
        plan.skipped[FLOOR] = "no fine-tune is served: the floor trains on the served checkpoint's own rows"
    else:
        labels = operator_labels_trained(base, suite, checkpoint.name)
        if labels is None:
            print(f"[laya-lab] bench: {FINE_TUNE} has no training record to count operator labels in: written as 0")
        plan.voters.append(
            BenchVoter(
                FINE_TUNE,
                "LAYA · FINE-TUNE",
                "fine_tune",
                device,
                asks(shared.get, champion, suite),
                threshold=served_threshold(base, surface, champion, code=suite.threshold)[0],
                of=BASE,
                labels=labels or 0,
                params_m=LAYA_PARAMS_M,
                served=True,
            )
        )
        if floor_voter.checkpoint_rows(base, checkpoint.name)[0]:
            floor = BenchVoter(FLOOR, "TF-IDF FLOOR", "floor", "cpu")
            floor.open = lambda: open_floor(base, suite, floor)
            plan.voters.append(floor)
        else:
            plan.skipped[FLOOR] = "the served checkpoint has no training rows on disk"
    laya = [voter.name for voter in plan.voters if voter.kind in ("base", "fine_tune")]
    plan.voters.extend(measured)
    for extra in extras:
        endpoint = compare.Timed(compare.endpoint_router(base, suite, extra["url"]))
        plan.voters.append(
            BenchVoter(
                extra["name"],
                extra["label"],
                extra["kind"],
                extra["runtime"],
                asks(lambda endpoint=endpoint: endpoint, extra["name"], suite),
                timed=endpoint,
                labels=extra["labels"],
                params_m=extra["params_m"],
                licence=extra["licence"],
            )
        )
    plan_panels(suite, panels, device, shared, champion, plan)
    for member in laya:
        for extra in extras:
            pair = (member, extra["name"])
            plan.voters.append(
                BenchVoter(
                    f"{ENSEMBLE_PREFIX}{'+'.join(pair)}",
                    f"MEAN · {' + '.join(pair)}".upper()[:LABEL_MAX],
                    "ensemble",
                    "none",
                    of=member,
                    members=pair,
                )
            )
    if len(plan.voters) > VOTERS_MAX:
        raise compare.Refused(f"{len(plan.voters)} voters: one file holds {VOTERS_MAX} (each --voter adds a mean per Laya voter)")
    return plan


# ── the sets ──────────────────────────────────────────────────────────────────


def golden_rows(name: str, suite: Suite) -> Rows:
    """The public cases the bench scores (the module note owns which, and why the operator's store is not read)."""
    public = load_cases(paths.PUBLIC_GOLDEN_DIR / f"{name}.jsonl")
    counted, _provisional = lab_files.counted_cases(public, frozenset())
    return Rows(
        [(case["id"], suite.build_state(case["state"]), case["expected"]) for case in counted],
        [case["state"] for case in counted],
    )


def with_facts(base: Path, held: Sequence[tuple[str, dict[str, Any], bool]]) -> list[tuple[str, dict[str, Any], bool]]:
    """Each held-out raw state with its turn's facts (`open_work_data.read_facts`), so a question-v4 challenger is asked
    what it would be served; a row whose turn has none reads as v3. The served version never reads them."""
    from laya_lab.open_work_data import read_facts

    facts = read_facts(base)
    return [(key, {**raw, "facts": facts[key]} if key in facts else raw, is_open) for key, raw, is_open in held]


def load_sets(base: Path, name: str, suite: Suite, labels: str = "auto") -> tuple[dict[str, list[Row]], str | None]:
    """`({"golden": rows[, "heldout": rows]}, why there is no held-out set)`. A store that is absent, empty or
    unreadable has no held-out set: the reason is for the terminal, never for the file."""
    sets = {"golden": golden_rows(name, suite)}
    try:
        held, meta = compare.heldout_raw(base, name, suite, labels)
    except (Exception, SystemExit) as error:  # noqa: BLE001  (an unreadable store is "no private split", never a crash)
        return sets, f"{type(error).__name__}: {error}"
    if held:
        held = with_facts(base, held)
        sets["heldout"] = Rows(
            [(key, suite.build_state(raw), is_open) for key, raw, is_open in held], [raw for _key, raw, _is_open in held]
        )
        return sets, None
    return sets, meta.get("skipped") or "the store holds no labelled turn ends outside the training rows"


def class_counts(data: Sequence[Row]) -> dict[str, int]:
    opened = sum(1 for _id, _state, expected in data if expected)
    return {"open": opened, "done": len(data) - opened}


def set_digest(data: Sequence[Row], length: int) -> str:
    """`length` hex of the states and labels a set asked, in no order: the set's version."""
    asked = sorted(json.dumps([state, expected], sort_keys=True, ensure_ascii=False) for _id, state, expected in data)
    return hashlib.sha256("\n".join(asked).encode("utf-8")).hexdigest()[:length]


def protocol_id(suite: Suite, sets: Mapping[str, Sequence[Row]]) -> str:
    """The module note owns the form."""
    out = f"{suite.name}.q{suite.question_version}.g{set_digest(sets['golden'], 12)}"
    if "heldout" in sets:
        out += f".h{len(sets['heldout'])}-{set_digest(sets['heldout'], 8)}"
    return out


# ── asking ────────────────────────────────────────────────────────────────────


def reason_of(error: BaseException) -> str:
    """The one line a skipped voter is listed with. An `Unavailable` wrote its own; any other error is named by its
    type alone, because its message can carry a path or a URL."""
    return str(error) if isinstance(error, Unavailable) else f"could not answer ({type(error).__name__})"


def ask(voter: BenchVoter, sets: Mapping[str, Sequence[Row]]) -> dict[str, Answers]:
    """One voter on every set: p per row and warm wall time per call (the first call of a set is asked twice, and
    its first answer dropped: a cold start is not latency)."""
    if voter.open is None:
        raise Unavailable("it has nothing to ask")
    predict = voter.open()
    out: dict[str, Answers] = {}
    for set_name, data in sets.items():
        answers = Answers([], [])
        states = [state for _id, state, _expected in data]
        if voter.restate is not None:
            raws = getattr(data, "raws", None)
            if raws is None or len(raws) != len(data):
                raise Unavailable(f"the {set_name} set carries no raw states to ask question v{voter.question_version} of")
            states = [voter.restate(raw) for raw in raws]
        if states:
            predict(states[0])
        for state in states:
            start = time.perf_counter()
            p = predict(state)
            answers.ms.append(voter.timed.ms[-1] if voter.timed is not None else (time.perf_counter() - start) * 1000)
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError(f"{voter.name} answered something that is not a probability")
            answers.probabilities.append(float(p))
        out[set_name] = answers
    return out


def ask_all(plan: Plan, sets: Mapping[str, Sequence[Row]]) -> tuple[dict[str, dict[str, Answers]], dict[str, str]]:
    """`({voter: {set: Answers}}, {skipped voter: reason})`, the plan's own skips included. A voter answers every
    set or is skipped whole; an ensemble answers when both its members did."""
    answered: dict[str, dict[str, Answers]] = {}
    skipped = dict(plan.skipped)
    for voter in plan.voters:
        if voter.members is not None:
            missing = [member for member in voter.members if member not in answered]
            if missing:
                skipped[voter.name] = f"{' and '.join(missing)} did not answer"
                continue
            first, second = (answered[member] for member in voter.members)
            answered[voter.name] = {
                set_name: Answers(
                    [statistics.fmean(pair) for pair in zip(first[set_name].probabilities, second[set_name].probabilities)], []
                )
                for set_name in sets
            }
            continue
        try:
            answered[voter.name] = voter.whole(sets) if voter.whole is not None else ask(voter, sets)
        except (Exception, SystemExit) as error:  # noqa: BLE001  (a voter that cannot run never fails the run)
            skipped[voter.name] = reason_of(error)
            print(f"[laya-lab] bench: {voter.name} skipped: {type(error).__name__}: {error}", file=sys.stderr)
    return answered, skipped


# ── the numbers ───────────────────────────────────────────────────────────────


def weighted_auc(
    order: Sequence[int], probabilities: Sequence[float], expected: Sequence[bool], counts: Sequence[int]
) -> float | None:
    """`metrics.auc` of a resample, in one pass: row i is drawn `counts[i]` times and `order` is the rows by rising
    p. A tie counts half, as there. None when the resample lost a class."""
    wins, below, positives = 0.0, 0, 0
    i, n = 0, len(order)
    while i < n:
        p, tied_open, tied_done = probabilities[order[i]], 0, 0
        while i < n and probabilities[order[i]] == p:
            if expected[order[i]]:
                tied_open += counts[order[i]]
            else:
                tied_done += counts[order[i]]
            i += 1
        wins += tied_open * (below + 0.5 * tied_done)
        below += tied_done
        positives += tied_open
    return wins / (positives * below) if positives and below else None


def bootstrap(
    probabilities: Sequence[float],
    expected: Sequence[bool],
    costs: Sequence[float],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
    level: float = LEVEL,
) -> dict[str, tuple[float, float] | None]:
    """`{auc, brier, cost}`: the `level` percentile interval of each over `resamples` draws of the rows with
    replacement. The draws depend on the seed and the row count alone, so every voter of a set is redrawn on the same
    rows. `cost` is the total of the per-row `costs`; `auc` is None when no draw kept both classes."""
    n = len(probabilities)
    if not n:
        return {"auc": None, "brier": None, "cost": None}
    rng = random.Random(seed)
    order = sorted(range(n), key=probabilities.__getitem__)
    squared = [(p - float(e)) ** 2 for p, e in zip(probabilities, expected)]
    drawn: dict[str, list[float]] = {"auc": [], "brier": [], "cost": []}
    for _ in range(resamples):
        counts = [0] * n
        for _row in range(n):
            counts[rng.randrange(n)] += 1
        drawn["brier"].append(sum(c * s for c, s in zip(counts, squared)) / n)
        drawn["cost"].append(sum(c * k for c, k in zip(counts, costs)))
        ranked = weighted_auc(order, probabilities, expected, counts)
        if ranked is not None:
            drawn["auc"].append(ranked)
    tail = (1 - level) / 2
    out: dict[str, tuple[float, float] | None] = {}
    for name, values in drawn.items():
        values.sort()
        out[name] = (agreement.percentile(values, tail), agreement.percentile(values, 1 - tail)) if values else None
    return out


def paired_auc_delta(
    first: Sequence[float],
    second: Sequence[float],
    expected: Sequence[bool],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
    level: float = LEVEL,
) -> tuple[float, float, float] | None:
    """`(AUC first − AUC second, lo, hi)`: the difference on the very draws `bootstrap` makes for every voter of the
    set (same seed, same row count), so its interval is the paired one. None when a class is empty."""
    whole_first, whole_second = auc(list(first), list(expected)), auc(list(second), list(expected))
    if whole_first is None or whole_second is None:
        return None
    n = len(expected)
    rng = random.Random(seed)
    orders = [sorted(range(n), key=ps.__getitem__) for ps in (first, second)]
    deltas: list[float] = []
    for _ in range(resamples):
        counts = [0] * n
        for _row in range(n):
            counts[rng.randrange(n)] += 1
        a = weighted_auc(orders[0], first, expected, counts)
        b = weighted_auc(orders[1], second, expected, counts)
        if a is not None and b is not None:
            deltas.append(a - b)
    deltas.sort()
    tail = (1 - level) / 2
    return whole_first - whole_second, agreement.percentile(deltas, tail), agreement.percentile(deltas, 1 - tail)


def accuracy_at(entry: Mapping[str, Any], n: Mapping[str, int]) -> float:
    """A score entry's accuracy at its operating point: the rows it neither warned falsely on nor missed."""
    at = entry["operating"]
    return 1 - (at["false_warnings"] + at["missed_open"]) / (n["open"] + n["done"])


def challenger_verdicts(
    payload: Mapping[str, Any], sets: Mapping[str, Sequence[Row]], answered: Mapping[str, Mapping[str, Answers]], plan: Plan
) -> list[dict[str, Any]]:
    """Each challenger and each panel against the served voter, by the rule a question version must clear to be served
    (lab/README.md "Question v3"): its held-out AUC beats the served one's by more than the paired interval (`lo` > 0), and its
    golden accuracy at its operating point does not fall. Numbers only; for the terminal and the commit, never the
    file, whose shape has no place for a pair."""
    served = next((voter for voter in plan.voters if voter.served and voter.name in answered), None)
    out: list[dict[str, Any]] = []
    if served is None:
        return out
    for voter in plan.voters:
        if (voter.restate is None and voter.whole is None) or voter.name not in answered:
            continue
        found: dict[str, Any] = {"voter": voter.name, "against": served.name, "question_version": voter.question_version}
        for block in payload["sets"]:
            expected = [is_open for _id, _state, is_open in sets[block["set"]]]
            delta = paired_auc_delta(
                answered[voter.name][block["set"]].probabilities, answered[served.name][block["set"]].probabilities, expected
            )
            entries = {entry["voter"]: entry for entry in block["scores"]}
            found[block["set"]] = {
                "delta_auc": delta,
                "accuracy": (accuracy_at(entries[served.name], block["n"]), accuracy_at(entries[voter.name], block["n"])),
            }
        held, golden = found.get("heldout"), found["golden"]
        delta = None if held is None else held["delta_auc"]
        beats = delta is not None and delta[1] > 0
        trails = delta is not None and delta[2] < 0
        keeps = golden["accuracy"][1] >= golden["accuracy"][0]
        found["wins"] = beats and keeps
        found["why"] = (
            "no held-out set to judge it on"
            if held is None
            else (
                "beats the served AUC past the interval"
                if beats
                else "held-out AUC below the served one past the interval"
                if trails
                else "held-out AUC within the interval of the served one"
            )
            + ("; golden accuracy holds" if keeps else "; golden accuracy falls")
        )
        out.append(found)
    return out


def verdict_lines(verdicts: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for found in verdicts:
        parts = []
        for set_name in BENCH_SET_ORDER:
            block = found.get(set_name)
            if block is None:
                continue
            delta = block["delta_auc"]
            moved = "—" if delta is None else f"{delta[0]:+.3f} [{delta[1]:+.3f}, {delta[2]:+.3f}]"
            parts.append(f"{set_name} ΔAUC {moved} · accuracy {block['accuracy'][0]:.3f} → {block['accuracy'][1]:.3f}")
        lines.append(
            f"  verdict  {found['voter']} vs {found['against']}: {' · '.join(parts)} → "
            f"{'WINS' if found['wins'] else 'measured no'} ({found['why']})"
        )
    return lines


BENCH_SET_ORDER = ("heldout", "golden")


def roc_points(probabilities: Sequence[float], expected: Sequence[bool]) -> list[dict[str, float]]:
    """The curve from the top threshold to the bottom: at each distinct p (to 4 places), the share of open rows at
    or over it and the share of done rows at or over it. At most ROC_MAX points, the two ends always kept."""
    scored = sorted(((round(p, 4), bool(e)) for p, e in zip(probabilities, expected)), reverse=True)
    opened = sum(e for _p, e in scored)
    done = len(scored) - opened
    points, at, hit, false = [], 0, 0, 0
    for threshold in sorted({1.0, 0.0, *(p for p, _e in scored)}, reverse=True):
        while at < len(scored) and scored[at][0] >= threshold:
            hit += scored[at][1]
            false += not scored[at][1]
            at += 1
        points.append(roc_point(threshold, false / done if done else 0.0, hit / opened if opened else 0.0))
    if len(points) > ROC_MAX:
        keep = sorted({round(i * (len(points) - 1) / (ROC_MAX - 1)) for i in range(ROC_MAX)})
        points = [points[i] for i in keep]
    return points


def operating_point(
    voter: BenchVoter, probabilities: Sequence[float], expected: Sequence[bool], fp_cost: float
) -> tuple[float, str]:
    """`(cut, "live" | "swept")`: the cut the voter serves with, else the cost rule's cut on this set
    (`metrics.pick_cut` over `compare.GRID`: in-sample, so it flatters, and the file says which it is)."""
    if voter.threshold is not None:
        return voter.threshold, "live"
    return pick_cut([at_cut(probabilities, expected, cut) for cut in compare.GRID], fp_cost)["at"], "swept"


def score(
    voter: BenchVoter,
    answers: Answers,
    expected: Sequence[bool],
    fp_cost: float,
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """One voter's numbers on one set, in the file's shape. Every point estimate is `metrics`' own."""
    ps = answers.probabilities
    threshold, source = operating_point(voter, ps, expected, fp_cost)
    head = at_cut(ps, expected, threshold)
    row_costs = warning_costs(ps, expected, threshold, fp_cost)
    drawn = bootstrap(ps, expected, row_costs, seed=seed, resamples=resamples)
    ranked = auc(ps, expected)
    bins = [reliability_bin(found) for found in reliability(ps, expected, EQUAL_BIN_CENTRES, band=0.5 / ECE_BINS)]
    timed = spread(answers.ms)
    return score_entry(
        voter=voter.name,
        auc=None if ranked is None else estimate(ranked, drawn["auc"], 4),
        brier=estimate(brier(ps, expected), drawn["brier"], 4),
        ece=ece(ps, expected, ECE_BINS),
        roc=roc_points(ps, expected),
        reliability=bins,
        operating=operating_entry(threshold, source, head, estimate(sum(row_costs), drawn["cost"], 2)),
        latency=latency_entry(timed) if timed["n"] else None,
    )


# ── the file: every key it carries is spelled here (the module note names the shape's owner) ──


def estimate(value: float, interval: tuple[float, float] | None, digits: int) -> dict[str, float]:
    """A value and its interval. The schema refuses an interval that does not hold its estimate, and a percentile
    interval of a tiny or one-sided sample can stop just short of it, so the interval is widened to the value,
    never narrowed. No interval (no draw kept both classes) is the value alone."""
    lo, hi = interval if interval is not None else (value, value)
    return {"value": round(value, digits), "lo": round(min(lo, value), digits), "hi": round(max(hi, value), digits)}


def roc_point(threshold: float, fpr: float, tpr: float) -> dict[str, float]:
    return {"threshold": threshold, "fpr": round(fpr, 6), "tpr": round(tpr, 6)}


def reliability_bin(found: Mapping[str, Any]) -> dict[str, Any]:
    return {"lo": found["lo"], "hi": found["hi"], "n": found["n"], "p": found["p"], "rate": found["rate"]}


def operating_entry(threshold: float, source: str, head: Mapping[str, Any], cost: dict[str, float]) -> dict[str, Any]:
    return {
        "threshold": threshold,
        "source": source,
        "false_warnings": head["false_warnings"],
        "missed_open": head["missed_open"],
        "cost": cost,
    }


def latency_entry(timed: Mapping[str, Any]) -> dict[str, Any]:
    return {"n": timed["n"], "p50_ms": timed["p50"], "p90_ms": timed["p90"]}


def score_entry(
    *, voter: str, auc: dict | None, brier: dict, ece: float, roc: list, reliability: list, operating: dict, latency: dict | None
) -> dict[str, Any]:
    return {
        "voter": voter,
        "auc": auc,
        "brier": brier,
        "ece": {"value": ece, "bins": ECE_BINS},
        "roc": roc,
        "reliability": reliability,
        "operating": operating,
        "latency": latency,
    }


def voter_entry(voter: BenchVoter, answered: Mapping[str, Any]) -> dict[str, Any]:
    """The roster row. `of` names a voter of this file or nothing: a fine-tune whose base was skipped has none."""
    return {
        "name": voter.name,
        "label": voter.label,
        "kind": voter.kind,
        "of": voter.of if voter.of in answered else None,
        "labels": voter.labels,
        "params_m": voter.params_m,
        "runtime": voter.runtime,
        "licence": voter.licence,
        "served": voter.served,
    }


def protocol_entry(
    suite: Suite,
    sets: Mapping[str, Sequence[Row]],
    *,
    seed: int,
    resamples: int,
    challengers: Sequence[BenchVoter] = (),
    panels: Sequence[BenchVoter] = (),
) -> dict[str, Any]:
    described = (
        f"Whether a finished agent turn still has open work ({suite.surface}, question v{suite.question_version}): "
        f"every voter on the {len(sets['golden'])} public golden cases"
    )
    if "heldout" in sets:
        described += f", and on {len(sets['heldout'])} operator-labelled turn ends no fine-tune of this surface trained on"

    def asked(voters: list) -> str:
        return ", ".join(f"{voter.name} question v{voter.question_version}" for voter in voters)

    refits = [voter for voter in challengers if not voter.retrain]
    retrains = [voter for voter in challengers if voter.retrain]
    if refits:
        described += (
            f"; refitted on the served model's own rows and split, each challenger is asked of the same rows: {asked(refits)}"
        )
    if retrains:
        described += (
            f"; retrained on every label since, with its own split, each retrain is asked of the same rows: {asked(retrains)}"
        )
    lines = [voter for voter in panels if voter.shape == LINE]
    #: Each three-question panel with its rule; a `whole` voter of no shape is described by neither.
    threes = [(voter, rule) for voter in panels if (rule := PANEL_SHAPES.get(voter.shape or "")) is not None]
    if lines:
        asked_n, signal_n = len(panel_voter.ASKS), len(panel_voter.SIGNALS)
        described += (
            f"; a panel ({', '.join(voter.name for voter in lines)}) asks {asked_n} questions of each turn (its request, "
            f"its closing, and every window of the whole closing) and fits one logistic line over the answers and "
            f"{signal_n} signals read off its text and its facts, out of fold on the held-out rows, the golden cases scored "
            f"by the fit on all of them"
        )
    if threes:
        rules = {"confident": "the most confident of the three answers", "mean": "their equal-weight mean"}
        described += (
            f"; a three-question panel asks {len(panel_voter.THREE)} questions of each closing (the served one, whether it "
            f"asks the reader to decide or approve, whether it says the work was finished and checked) and fits nothing: "
            + ", ".join(f"{voter.name} answers with {rules[rule]}" for voter, rule in threes)
        )
    return {
        "id": protocol_id(suite, sets),
        "surface": suite.surface,
        "question": QUESTION_NAME,
        "question_version": suite.question_version,
        "positive": "open",
        "description": described + ".",
        "interval": {"level": LEVEL, "method": "bootstrap", "resamples": resamples, "seed": seed},
        "costs": {"false_warning": suite.false_warning_cost},
    }


def results(
    suite: Suite,
    sets: Mapping[str, Sequence[Row]],
    plan: Plan,
    answered: Mapping[str, Mapping[str, Answers]],
    *,
    generated_at: str,
    commit: str | None,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """The whole file. `rack` (the rack's live use) is None here: `run` measures it (`rack.py` owns how)."""
    voters = [voter for voter in plan.voters if voter.name in answered]
    scored = []
    for set_name, data in sets.items():
        expected = [expected for _id, _state, expected in data]
        scored.append(
            {
                "set": set_name,
                "n": class_counts(data),
                "scores": [
                    score(
                        voter, answered[voter.name][set_name], expected, suite.false_warning_cost, seed=seed, resamples=resamples
                    )
                    for voter in voters
                ],
            }
        )
    challengers = [voter for voter in voters if voter.restate is not None]
    panels = [voter for voter in voters if voter.whole is not None]
    return {
        "v": RESULTS_VERSION,
        "protocol": protocol_entry(suite, sets, seed=seed, resamples=resamples, challengers=challengers, panels=panels),
        "generated_at": generated_at,
        "commit": commit,
        "voters": [voter_entry(voter, answered) for voter in voters],
        "sets": scored,
        "rack": None,
        "reproduce": {"command": REPRODUCE.format(suite=suite.name), "set": "golden"},
    }


# ── writing ───────────────────────────────────────────────────────────────────


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def leaked(serialized: str, sets: Mapping[str, Sequence[Row]]) -> int:
    """How many rows have their id, key or any text of their state or raw state (a request too) inside `serialized`
    (as written, or as JSON escapes it). 0 is the only count a results file may have."""
    found = 0
    for data in sets.values():
        raws = getattr(data, "raws", None) or [None] * len(data)
        for (row_id, state, _expected), raw in zip(data, raws):
            parts = [part for part in (row_id, *_strings(state), *_strings(raw)) if len(part) >= GUARD_MIN_CHARS]
            found += any(part in serialized or json.dumps(part)[1:-1] in serialized for part in parts)
    return found


def write_results(payload: Mapping[str, Any], sets: Mapping[str, Sequence[Row]], out_dir: Path, day: str) -> Path:
    """Write `<out_dir>/<day>.json` whole, or raise and write nothing when a row reached the payload."""
    text = json.dumps(payload, indent=2, allow_nan=False, ensure_ascii=False) + "\n"
    count = leaked(text, sets)
    if count:
        raise RuntimeError(f"{count} rows' ids or text reached the results file; nothing was written")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{day}.json"
    tmp = out_dir / f".{day}.json.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def head_commit(root: Path) -> str | None:
    """The commit the checkout stands at, or None outside one."""
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    found = done.stdout.strip()
    return found if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", found) else None


# ── the command ───────────────────────────────────────────────────────────────


def plan_lines(
    name: str, protocol: str, sets: Mapping[str, Sequence[Row]], no_heldout: str | None, plan: Plan, skipped: Mapping[str, str]
) -> list[str]:
    lines = [f"[laya-lab] bench {name} · protocol {protocol}"]
    for set_name, data in sets.items():
        n = class_counts(data)
        lines.append(f"  set      {set_name} · {len(data)} rows ({n['open']} open / {n['done']} done)")
    if no_heldout:
        lines.append(f"  set      heldout · none: {no_heldout}")
    lines.append("  voters   " + " · ".join(voter.name for voter in plan.voters if voter.name not in skipped))
    lines.extend(f"  skipped  {voter}: {why}" for voter, why in skipped.items())
    return lines


def result_lines(payload: Mapping[str, Any]) -> list[str]:
    lines = []
    for block in payload["sets"]:
        width = max(len(entry["voter"]) for entry in block["scores"])
        lines.append(f"  {block['set']}")
        lines.append(f"    {'voter':<{width}}   AUC [95%]                brier    ECE   cut       ⚠️✗ miss   cost   p50 ms")
        for entry in block["scores"]:
            ranked, at, timed = entry["auc"], entry["operating"], entry["latency"]
            interval = "—" if ranked is None else f"{ranked['value']:.3f} [{ranked['lo']:.3f}, {ranked['hi']:.3f}]"
            lines.append(
                f"    {entry['voter']:<{width}}   {interval:<22}  {fmt_num(entry['brier']['value'], 3)}  "
                f"{fmt_num(entry['ece']['value'], 3)}   {at['threshold']:.2f} {at['source']:<5} "
                f"{at['false_warnings']:>3} {at['missed_open']:>4}  {at['cost']['value']:>5}  "
                f"{timed['p50_ms'] if timed else '—':>7}"
            )
    return lines


def run(
    name: str,
    base: Path,
    *,
    voters: Sequence[str] = (),
    labels: str = "auto",
    device: str = "mps",
    dry_run: bool = False,
    router: Any = None,
    plan: Plan | None = None,
    out_dir: Path | None = None,
    day: str | None = None,
    challengers: Sequence[str] = (),
    panels: Sequence[str] = (),
    with_rack: bool = False,
) -> int:
    """The command. `plan` (tests: voters that load nothing) replaces the machine's own; `out_dir` and `day` say
    where the file goes (default: this checkout's `bench/results/`, today).

    `with_rack` (`--rack`) also measures the rack's own use into the file. Off by default: a results file is
    published, and the rack block is the operator's week in counts, so it goes out only when they ask for it."""
    suite = SUITES[name]
    try:
        plan = (
            plan
            if plan is not None
            else plan_voters(base, suite, voters, device=device, router=router, challengers=challengers, panels=panels)
        )
    except compare.Refused as refusal:
        print(f"[laya-lab] bench refused: {refusal}")
        return REFUSED
    sets, no_heldout = load_sets(base, name, suite, labels)
    day = day or date.today().isoformat()
    default_dir = paths.REPO_ROOT.joinpath(*RESULTS_DIR)
    out_dir = out_dir if out_dir is not None else default_dir
    # The checkout's own file is named from the checkout: a terminal line is pasted into reports.
    target = "/".join((*RESULTS_DIR, f"{day}.json")) if out_dir == default_dir else str(out_dir / f"{day}.json")
    if dry_run:
        for line in plan_lines(name, protocol_id(suite, sets), sets, no_heldout, plan, plan.skipped):
            print(line)
        print(f"  dry run: nothing loaded, nothing written (a run writes {target})")
        return 0
    answered, skipped = ask_all(plan, sets)
    for line in plan_lines(name, protocol_id(suite, sets), sets, no_heldout, plan, skipped):
        print(line)
    if not answered:
        print("[laya-lab] bench: no voter answered; nothing was written")
        return 1
    payload = results(suite, sets, plan, answered, generated_at=rows.utc_stamp(), commit=head_commit(paths.REPO_ROOT))
    if with_rack:
        payload["rack"], no_rack = rack.measure(base, suite)
        if no_rack:
            print(f"  rack     none: {no_rack}")
    else:
        print("  rack     withheld: the rack's own use stays out of a published file unless --rack asks for it")
    try:
        write_results(payload, sets, out_dir, day)
    except RuntimeError as error:
        print(f"[laya-lab] bench: {error}")
        return 1
    for line in result_lines(payload):
        print(line)
    for line in verdict_lines(challenger_verdicts(payload, sets, answered, plan)):
        print(line)
    print(f"  results → {target}")
    return 0
