"""The floor voter: TF-IDF + logistic regression, the cheapest detector a fine-tune has to beat.

"From Confident Closing to Silent Failure" (Advani 2026) found that a bag-of-words detector beats LLM
judges at catching a false "done". So a fine-tuned Laya that cannot beat one on held-out ⚠️-cut cost
has learned nothing worth serving: the promotion gate's check (d) (`gate.py`) trains this floor on the
candidate's OWN training rows and scores it on the same held-out rows as the candidate.

numpy only (it ships with laya's torch), so the floor adds no dependency. Deterministic: features come
from the training texts alone (no labels), the folds from a stable hash of each text, and the solver
is full-batch accelerated gradient descent with a fixed step and iteration count.

The floor's cut is the cost rule's (`metrics.pick_cut`, `FALSE_WARNING_COST`) on OUT-OF-FOLD predictions
of its training rows, so it never sees a held-out label, exactly as the candidate never does.
`FloorRouter` answers like `laya.Router`, so `infer.predict_noul`, and `compare`, score it unchanged.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import numpy as np

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab.data import read_jsonl
from laya_lab.metrics import SWEEP_FINE, at_cut, pick_cut
from laya_bridge import rows
from laya_bridge.paths import CHECKPOINTS_DIRNAME

NAME = "floor:tfidf-logistic"
TOKEN = re.compile(r"[a-z0-9_]+(?:'[a-z]+)?")
#: A term in fewer training texts than this is noise to a floor of a few hundred rows.
MIN_DF = 2
MAX_FEATURES = 20_000
FOLDS = 5
ITERATIONS = 600
#: Inverse regularisation strength, in scikit-learn's convention (C = 1 is its default).
C = 1.0


def terms(text: str) -> list[str]:
    """Lower-cased word unigrams and bigrams."""
    words = TOKEN.findall(text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def fold_of(text: str) -> int:
    return int(hashlib.sha256(f"floor-fold:{text}".encode()).hexdigest()[:8], 16) % FOLDS


def vocabulary(texts: Sequence[str]) -> tuple[dict[str, int], np.ndarray]:
    """`({term: column}, idf)` from the training texts: terms in ≥ MIN_DF texts, the MAX_FEATURES most
    common (ties alphabetical), smoothed idf `ln((1 + n) / (1 + df)) + 1`."""
    df: Counter[str] = Counter()
    for text in texts:
        df.update(set(terms(text)))
    kept = sorted((term for term, count in df.items() if count >= MIN_DF), key=lambda term: (-df[term], term))[:MAX_FEATURES]
    vocab = {term: column for column, term in enumerate(sorted(kept))}
    n = len(texts)
    idf = np.array([math.log((1 + n) / (1 + df[term])) + 1 for term in sorted(kept)], dtype=np.float64)
    return vocab, idf


def vectorize(texts: Sequence[str], vocab: Mapping[str, int], idf: np.ndarray) -> np.ndarray:
    """Rows of sublinear tf (1 + ln tf) × idf, L2-normalised, with a constant 1 appended for the bias."""
    matrix = np.zeros((len(texts), len(vocab) + 1), dtype=np.float64)
    for row, text in enumerate(texts):
        for term, count in Counter(terms(text)).items():
            column = vocab.get(term)
            if column is not None:
                matrix[row, column] = (1 + math.log(count)) * idf[column]
        norm = float(np.linalg.norm(matrix[row, :-1]))
        if norm > 0:
            matrix[row, :-1] /= norm
    matrix[:, -1] = 1.0
    return matrix


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * z))  # overflow-free


def fit_logistic(matrix: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Weights (bias last) minimising mean log-loss + (1 / 2Cn)·‖w‖² (bias unpenalised), by Nesterov's
    accelerated gradient at step 1/L, where L bounds the gradient's Lipschitz constant."""
    n, width = matrix.shape
    penalty = 1.0 / (C * n)
    lipschitz = 0.25 * float(np.max(np.sum(matrix * matrix, axis=1))) + penalty
    step = 1.0 / lipschitz
    mask = np.ones(width)
    mask[-1] = 0.0
    weights = np.zeros(width)
    look = weights.copy()
    momentum = 1.0
    for _ in range(ITERATIONS):
        gradient = matrix.T @ (sigmoid(matrix @ look) - labels) / n + penalty * mask * look
        updated = look - step * gradient
        following = (1 + math.sqrt(1 + 4 * momentum * momentum)) / 2
        look = updated + ((momentum - 1) / following) * (updated - weights)
        weights, momentum = updated, following
    return weights


@dataclass(frozen=True)
class Floor:
    vocab: dict[str, int]
    idf: np.ndarray
    weights: np.ndarray
    #: The cost rule's cut on the floor's out-of-fold training predictions.
    cut: float
    rows: int
    open_rows: int

    def predict(self, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        return [float(p) for p in sigmoid(vectorize(texts, self.vocab, self.idf) @ self.weights)]


def _fit_once(texts: Sequence[str], labels: Sequence[bool]) -> tuple[dict[str, int], np.ndarray, np.ndarray]:
    vocab, idf = vocabulary(texts)
    weights = fit_logistic(vectorize(texts, vocab, idf), np.array([float(label) for label in labels]))
    return vocab, idf, weights


def fit(texts: Sequence[str], labels: Sequence[bool], fp_cost: float) -> Floor:
    """The floor trained on `texts`, with its cut chosen out of fold. ValueError on a set too small to fold
    or with one class only: a floor that cannot be measured is not a floor."""
    if len(texts) != len(labels):
        raise ValueError(f"floor: {len(texts)} texts vs {len(labels)} labels")
    opened = sum(bool(label) for label in labels)
    if len(texts) < 2 * FOLDS or opened in (0, len(texts)):
        raise ValueError(f"floor: {len(texts)} rows ({opened} open) cannot train a two-class floor in {FOLDS} folds")
    folds = [fold_of(text) for text in texts]
    out_of_fold = [0.0] * len(texts)
    for fold in range(FOLDS):
        train = [i for i, f in enumerate(folds) if f != fold]
        test = [i for i, f in enumerate(folds) if f == fold]
        if not test:
            continue
        if len({bool(labels[i]) for i in train}) < 2:
            raise ValueError(f"floor: fold {fold} leaves one class to train on")
        vocab, idf, weights = _fit_once([texts[i] for i in train], [labels[i] for i in train])
        predicted = sigmoid(vectorize([texts[i] for i in test], vocab, idf) @ weights)
        for i, p in zip(test, predicted):
            out_of_fold[i] = float(p)
    expected = [bool(label) for label in labels]
    cut = pick_cut([at_cut(out_of_fold, expected, c) for c in SWEEP_FINE], fp_cost)["at"]
    vocab, idf, weights = _fit_once(texts, labels)
    return Floor(vocab, idf, weights, cut, len(texts), opened)


def checkpoint_rows(base: Path, checkpoint: str) -> tuple[list[str], list[bool]]:
    """A checkpoint's own training rows, as the floor trains on them: (texts, open). The gate trains the floor on the
    candidate's rows; `compare`'s `floor` voter on the served checkpoint's."""
    texts, labels = [], []
    for row in read_jsonl(base / CHECKPOINTS_DIRNAME / f"{checkpoint}.data" / rows.TRAIN_FILE):
        state, expected = row.get("state"), (row.get("expected") or {}).get("q")
        if isinstance(state, str) and state.strip() and isinstance(expected, (bool, int, float)):
            texts.append(state)
            labels.append(bool(expected >= 0.5) if not isinstance(expected, bool) else expected)
    return texts, labels


def state_text(state: Any) -> str:
    """The text a state carries: the open-work state IS the hand-off string (`open_work.decide_state`)."""
    if isinstance(state, str):
        return state
    if isinstance(state, Mapping) and isinstance(state.get("hand_off"), str):
        return state["hand_off"]
    raise ValueError("the floor voter reads a text state only")


class FloorRouter:
    """A fitted `Floor`, answering like `laya.Router` (`{"answers": {qid: {"noul": p}}}`) for any `model`."""

    def __init__(self, floor: Floor) -> None:
        self.floor = floor
        self.loaded_revisions: dict[str, str | None] = {}

    def predict(self, state: Any, questions: Mapping[str, Any], model: str | None = None) -> dict:
        p = self.floor.predict([state_text(state)])[0]
        return {"answers": {question_id: {"noul": p} for question_id in questions}}
