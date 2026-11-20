"""The panel voter: Laya asked many small questions of one turn, and one fitted line over the answers.

The served verdict is ONE question over the closing message (`open_work.OPEN_WORK_QUESTION`). The panel asks whether
breaking that judgement into parts ranks the turns better with the same model: what the request was, what kind of
closing the turn ended on, and the served question itself, over more of the message than one window holds. Measurement
only, like every voter of `bench`: nothing here registers, serves or saves a panel, and its weights live for one run.

── What is asked (`ASKS`) ────────────────────────────────────────────────────
Three parts of a turn, each a state of its own, because Laya reads one window (`open_work.STATE_MAX_CHARS`):
- `REQUEST`: the head of the prompt that opened the turn. A turn nobody typed has none, and every answer about it is
  "cannot tell" (logit 0), with the `has_request` signal saying which rows those are.
- `CLOSING`: the very state the served question is asked of (`Suite.build_state`). The served question is one of
  these asks, unchanged and never restated here, so the panel always holds the served verdict as one of its inputs.
- `WINDOWS`: every window of the whole closing message. A long message is fitted to one window by keeping its opening
  and its end, so its middle is never read; these asks re-ask a closing question of each window and keep the highest
  answer. A message that fits one window has no windows, and the answer is the closing's own.
Every question is short and positive: the served question's owner measured that this model reads a negated
instruction badly (`open_work.OPEN_WORK_QUESTION`).

── What is not asked (`SIGNALS`) ─────────────────────────────────────────────
Twelve inputs need no model, and are read exactly instead of asked about.
- Five off the text: whether there is a request, whether it and the closing end on a question
  (`transcript.ends_on_question`, the rack's own rule), how long the closing is, and whether it carries fenced code.
- Seven off the turn's facts (`open_work.read_facts`: what the turn DID, from its transcript): whether it has any,
  whether tests ran, passed or failed, how many files it edited, and whether a commit or a PR was made. A turn with no
  facts reads 0 on all seven, `has_facts` among them. No golden case carries facts yet, so on the golden set these
  seven move every case alike and change no ranking.

── The line, and why it is fitted out of fold ────────────────────────────────
One L2 logistic regression (`floor.fit_logistic`: one fit, one owner) over the standardised answers. Its weights need
labels the model never trained on, or the served question's answer (in-sample on its own training rows) would look
better than it is and take all the weight. So the panel is fitted on the HELD-OUT rows, and scored there out of fold:
a row's fold is a stable hash of its id, and a row is only ever scored by a line fitted on the other folds. The golden
cases are scored by the line fitted on every held-out row: they are never fitted on. A store with no held-out rows has
no panel.

── Which model answers ───────────────────────────────────────────────────────
The served question goes to the served model. The new questions go to `others`: the served fine-tune again (the same
model, asked more) or the base model. The fine-tune was trained on one question, so which of the two reads the new
questions better is itself measured; `bench --panel served --panel base` scores both.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from laya_lab import siblings  # noqa: F401  (puts tldrack on sys.path)
from laya_lab import infer
from laya_lab.floor import fit_logistic, sigmoid
from tldrack import open_work
from tldrack.transcript import ends_on_question

NAME = "laya-panel"
REQUEST, CLOSING, WINDOWS = "request", "closing", "windows"
#: The closing ask that is the served question: its wording is the suite's, and the served model answers it.
SERVED = "open"


@dataclass(frozen=True)
class Ask:
    name: str
    part: str
    #: None on `SERVED` (the suite's own question) and on a `WINDOWS` ask (its `over` ask's question).
    instructions: str | None = None
    #: A `WINDOWS` ask: the closing ask it re-asks of every window.
    over: str | None = None


ASKS: tuple[Ask, ...] = (
    Ask("request_question", REQUEST, "Is this message asking a question?"),
    Ask("request_build", REQUEST, "Is this message asking for code to be written or changed?"),
    Ask("request_broken", REQUEST, "Is this message reporting that something is broken?"),
    Ask("request_plan", REQUEST, "Is this message asking for a plan or a design before any work?"),
    Ask("request_document", REQUEST, "Is this message asking for a document or a write-up?"),
    Ask("request_phase", REQUEST, "Is this message starting one step or phase of a larger plan?"),
    Ask("request_many", REQUEST, "Does this message ask for more than one thing?"),
    Ask(SERVED, CLOSING),
    Ask("decide", CLOSING, "Does this message ask the reader to decide or approve something?"),
    Ask("reader_acts", CLOSING, "Does this message ask the reader to run or do something themselves?"),
    Ask("failed", CLOSING, "Does this message say that something failed or could not be done?"),
    Ask("waiting", CLOSING, "Does this message say it is waiting on something that is still running?"),
    Ask("finished", CLOSING, "Does this message say the work was finished and checked?"),
    Ask("offer", CLOSING, "Does this message offer optional extra work?"),
    Ask("skipped", CLOSING, "Does this message say a step was skipped or left for later?"),
    Ask("answer", CLOSING, "Is this message mainly an answer or an explanation?"),
    Ask("open_any", WINDOWS, over=SERVED),
    Ask("failed_any", WINDOWS, over="failed"),
)
TEXT_SIGNALS = ("has_request", "request_asks", "closing_asks", "length", "code")
FACT_SIGNALS = ("has_facts", "ran_tests", "tests_passed", "tests_failed", "edited_files", "made_commit", "opened_pr")
SIGNALS = TEXT_SIGNALS + FACT_SIGNALS
FEATURES = tuple(ask.name for ask in ASKS) + SIGNALS

FOLDS = 5
#: A p is clipped to [EPS, 1 − EPS] before its logit, so a saturated answer is a large value, never an infinity.
EPS = 1e-4
#: A closing this long or longer reads as 1.0 on the `length` signal: two windows.
LENGTH_FULL = 2 * open_work.STATE_MAX_CHARS
#: A turn that edited this many files or more reads as 1.0 on the `edited_files` signal.
EDITS_FULL = 10
#: Consecutive windows share half their text, so a sentence cut by one window's edge is whole in the next.
WINDOW_STEP = open_work.STATE_MAX_CHARS // 2


# ── the parts of a turn ───────────────────────────────────────────────────────


def request_state(request: str | None) -> str | None:
    """The request as a state: flattened (`open_work.flat_request`), its head kept to one window. None when there is
    no request."""
    flat = open_work.flat_request(request) if request else ""
    return flat[: open_work.STATE_MAX_CHARS] or None


def windows(hand_off: str) -> list[str]:
    """Every window of a closing message too long for one, fenced code collapsed first as the served state does. []
    when the served state already holds the whole message: there is nothing more to read."""
    size = open_work.STATE_MAX_CHARS
    if len(hand_off) <= size:
        return []
    text = open_work.CODE_FENCE.sub(open_work.CODE_PLACEHOLDER, hand_off)
    if len(text) <= size:
        return []
    starts = list(range(0, len(text) - size, WINDOW_STEP)) + [len(text) - size]
    return [text[start : start + size] for start in starts]


def signals(raw: Mapping[str, Any]) -> dict[str, float]:
    """Every `SIGNALS` input of one turn. The facts go through their one reader, so a mapping that does not read is
    no facts at all, never half of them."""
    hand_off, request = raw["hand_off"], raw.get("request")
    facts = open_work.read_facts(raw.get("facts")) or {}
    passed = facts.get("tests_passed")
    return {
        "has_request": float(bool(request)),
        "request_asks": float(ends_on_question(request)),
        "closing_asks": float(ends_on_question(hand_off)),
        "length": min(len(hand_off), LENGTH_FULL) / LENGTH_FULL,
        "code": float(open_work.CODE_FENCE.search(hand_off) is not None),
        "has_facts": float(bool(facts)),
        "ran_tests": float(bool(facts.get("ran_tests"))),
        "tests_passed": float(passed is True),
        "tests_failed": float(passed is False),
        "edited_files": min(facts.get("edited_files", 0), EDITS_FULL) / EDITS_FULL,
        "made_commit": float(bool(facts.get("made_commit"))),
        "opened_pr": float(bool(facts.get("opened_pr"))),
    }


# ── asking ────────────────────────────────────────────────────────────────────


class Asker:
    """Asks Laya and remembers: one (model, question, state) is asked once a run, so two panels that share the served
    question's answers pay for them once, and each still counts their time. `router` opens on the first ask."""

    def __init__(self, router: Callable[[], Any]) -> None:
        self.router = router
        self.kept: dict[tuple[str, str, str], tuple[float, float]] = {}
        self.warm: set[str] = set()

    def ask(self, model: str, question: Mapping[str, Any], state: str) -> tuple[float, float]:
        """`(p, ms)`. A model's first call is asked twice and its first answer dropped: a cold start is not latency."""
        key = (model, str(question["instructions"]), state)
        if key not in self.kept:
            if model not in self.warm:
                infer.predict_noul(self.router(), model, [state], question)
                self.warm.add(model)
            start = time.perf_counter()
            p = infer.predict_noul(self.router(), model, [state], question)[0]
            self.kept[key] = (p, (time.perf_counter() - start) * 1000)
        return self.kept[key]


def logit(p: float) -> float:
    clipped = min(max(float(p), EPS), 1 - EPS)
    return math.log(clipped / (1 - clipped))


def answer_row(
    asker: Asker, question: Mapping[str, Any], closing: str, raw: Mapping[str, Any], served: str, others: str
) -> tuple[list[float], float]:
    """One turn's features, in `FEATURES` order, and the wall time of every ask it took. `question` and `closing` are
    the suite's served question and state; `served` answers that question, `others` every new one."""
    request = request_state(raw.get("request"))
    spans = windows(raw["hand_off"])
    by_name = {ask.name: ask for ask in ASKS}
    #: p per ask; a request ask of a turn with no request is 0.5, "cannot tell", so its logit is 0.
    answered: dict[str, float] = {}
    total = 0.0

    def put(ask: Ask, state: str) -> float:
        nonlocal total
        source = by_name[ask.over] if ask.over else ask
        asked = question if source.name == SERVED else {"type": "noul", "instructions": source.instructions}
        p, ms = asker.ask(served if source.name == SERVED else others, asked, state)
        total += ms
        return p

    for ask in ASKS:
        if ask.over is not None:
            answered[ask.name] = max([answered[ask.over], *(put(ask, span) for span in spans)])
        elif ask.part == REQUEST:
            answered[ask.name] = put(ask, request) if request else 0.5
        else:
            answered[ask.name] = put(ask, closing)
    noted = signals(raw)
    row = [logit(answered[ask.name]) for ask in ASKS]
    return row + [noted[name] for name in SIGNALS], total


# ── the line ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Line:
    """A fitted panel: the training rows' mean and spread per feature, and the weights over the standardised
    features (bias last)."""

    mean: np.ndarray
    spread: np.ndarray
    weights: np.ndarray

    def design(self, matrix: np.ndarray) -> np.ndarray:
        return np.hstack([(matrix - self.mean) / self.spread, np.ones((len(matrix), 1))])

    def predict(self, matrix: np.ndarray) -> list[float]:
        return [float(p) for p in sigmoid(self.design(matrix) @ self.weights)]


def fit(matrix: np.ndarray, labels: Sequence[bool]) -> Line:
    """The line fitted on these rows. A feature that never varies on them is carried at spread 1, so it reads 0
    everywhere and takes no weight."""
    mean = matrix.mean(axis=0)
    spread = matrix.std(axis=0)
    spread = np.where(spread < 1e-9, 1.0, spread)
    empty = Line(mean, spread, np.zeros(matrix.shape[1] + 1))
    return Line(mean, spread, fit_logistic(empty.design(matrix), np.array([float(label) for label in labels])))


def fold_of(row_id: str) -> int:
    return int(hashlib.sha256(f"panel-fold:{row_id}".encode()).hexdigest()[:8], 16) % FOLDS


def check(ids: Sequence[str], labels: Sequence[bool]) -> str | None:
    """Why these rows cannot fit a panel out of fold, or None when they can. Numbers only."""
    if len(set(ids)) != len(ids):
        return "two held-out rows share an id, so their folds would too"
    opened = sum(bool(label) for label in labels)
    if len(ids) < 2 * FOLDS or opened in (0, len(ids)):
        return f"{len(ids)} held-out rows ({opened} open) cannot fit a two-class panel in {FOLDS} folds"
    folds = [fold_of(row_id) for row_id in ids]
    for fold in range(FOLDS):
        if len({bool(label) for label, f in zip(labels, folds) if f != fold}) < 2:
            return f"fold {fold} leaves one class to fit on"
    return None


def cross_fit(ids: Sequence[str], matrix: np.ndarray, labels: Sequence[bool]) -> list[float]:
    """Each row's p from the line fitted without that row's fold. ValueError when `check` says no."""
    why = check(ids, labels)
    if why:
        raise ValueError(why)
    folds = np.array([fold_of(row_id) for row_id in ids])
    marks = np.array([bool(label) for label in labels])
    out = np.zeros(len(ids))
    for fold in range(FOLDS):
        held = folds == fold
        if held.any():
            out[held] = fit(matrix[~held], marks[~held]).predict(matrix[held])
    return [float(p) for p in out]


# ── a run ─────────────────────────────────────────────────────────────────────

Set = tuple[Sequence[str], Sequence[Mapping[str, Any]], Sequence[str], Sequence[bool]]


def answer_sets(
    asker: Asker, question: Mapping[str, Any], sets: Mapping[str, Set], served: str, others: str, *, fit_on: str = "heldout"
) -> tuple[dict[str, tuple[list[float], list[float]]], Line]:
    """`({set: (p per row, ms per row)}, the line fitted on every row of `fit_on`)`. Each set is `(ids, raws, closing
    states, labels)`. `fit_on` is scored out of fold; every other set by the line fitted on all of `fit_on`.
    ValueError, with numbers only, when `fit_on` is missing or cannot be folded."""
    if fit_on not in sets:
        raise ValueError(f"it is fitted on the {fit_on} rows, and this run has none")
    ids, _raws, _closings, labels = sets[fit_on]
    why = check(ids, labels)
    if why:
        raise ValueError(why)
    asked: dict[str, tuple[np.ndarray, list[float]]] = {}
    for name, (_ids, raws, closings, _labels) in sets.items():
        rows = [answer_row(asker, question, closing, raw, served, others) for raw, closing in zip(raws, closings)]
        asked[name] = (
            np.array([row for row, _ms in rows], dtype=np.float64).reshape(len(rows), len(FEATURES)),
            [ms for _row, ms in rows],
        )
    matrix, _ms = asked[fit_on]
    whole = fit(matrix, labels)
    out = {
        name: (cross_fit(ids, found, labels) if name == fit_on else whole.predict(found), ms)
        for name, (found, ms) in asked.items()
    }
    return out, whole


def weight_lines(line: Line) -> list[str]:
    """The fitted weights, largest first: what the panel leaned on. Standardised features, so they compare."""
    ranked = sorted(zip(FEATURES, line.weights[:-1]), key=lambda pair: -abs(pair[1]))
    return [f"    {name:<18} {weight:+.3f}" for name, weight in ranked]
