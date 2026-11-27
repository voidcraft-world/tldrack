"""The stack voter: one logistic regression over the other voters' logits, scored out of fold.

`compare` scores Laya and the TF-IDF floor (`floor.py`) on the same rows. Their `mean` asks whether averaging helps; the
stack asks the sharper question: do the two make DIFFERENT mistakes, so that a learned weighting of their two opinions
ranks the rows better than either alone? Measurement only. Nothing registers, serves or saves a stack: its weights live
for one `compare` run and are thrown away.

Honest by construction:
- **Out of fold on the very set it is measured on.** Each row's fold comes from a stable hash of its id, and a row is
  scored only by a stack fitted on the OTHER folds, so no row is ever scored by a stack that saw its label.
- **Deterministic.** The folds are a hash, the features are the voters' logits (clipped), and the solver is damped
  Newton with a fixed iteration count and a backtracking rule that compares exact objective values. Same rows, same
  numbers, on every machine numpy runs on.
- **The house rule decides the verdict** (`bench.paired_auc_delta`): the stack wins only when its paired ΔAUC against
  the best single voter has an interval that sits above zero. A positive point estimate inside a straddling interval
  is no win.

numpy only, like the floor. The stack's live cut is the code's, as `mean`'s is: a cut chosen on these labels would be
in-sample, and the report's `best_in_sample` column already says what one would be.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from laya_lab.floor import sigmoid

NAME = "stack"
FOLDS = 5
#: A voter's p is clipped to [EPS, 1 − EPS] before its logit, so a saturated voter is a large weight, never an infinity.
EPS = 1e-4
#: Inverse regularisation strength, in scikit-learn's convention, as the floor's.
C = 1.0
NEWTON_STEPS = 50
HALVINGS = 30


def fold_of(row_id: str) -> int:
    return int(hashlib.sha256(f"stack-fold:{row_id}".encode()).hexdigest()[:8], 16) % FOLDS


def logit(p: float) -> float:
    clipped = min(max(float(p), EPS), 1 - EPS)
    return math.log(clipped / (1 - clipped))


def features(columns: Sequence[Sequence[float]]) -> np.ndarray:
    """Rows × (voters + 1): each voter's logit, then a constant 1 for the bias."""
    n = len(columns[0]) if columns else 0
    matrix = np.ones((n, len(columns) + 1), dtype=np.float64)
    for column, ps in enumerate(columns):
        matrix[:, column] = [logit(p) for p in ps]
    return matrix


def _objective(matrix: np.ndarray, labels: np.ndarray, weights: np.ndarray, penalty: float, mask: np.ndarray) -> float:
    z = matrix @ weights
    # mean log-loss, written overflow-free: log(1 + e^z) − y·z
    loss = float(np.mean(np.logaddexp(0.0, z) - labels * z))
    return loss + 0.5 * penalty * float(np.sum(mask * weights * weights))


def fit_logistic(matrix: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Weights (bias last) minimising mean log-loss + (1 / 2Cn)·‖w‖² (bias unpenalised), by Newton's method with step
    halving. A few features only, so the exact Hessian is cheap; the penalty keeps it invertible on separable rows."""
    n, width = matrix.shape
    penalty = 1.0 / (C * n)
    mask = np.ones(width)
    mask[-1] = 0.0
    weights = np.zeros(width)
    current = _objective(matrix, labels, weights, penalty, mask)
    for _ in range(NEWTON_STEPS):
        p = sigmoid(matrix @ weights)
        gradient = matrix.T @ (p - labels) / n + penalty * mask * weights
        curvature = p * (1 - p)
        hessian = (matrix.T * curvature) @ matrix / n + np.diag(penalty * mask + 1e-12)
        direction = np.linalg.solve(hessian, gradient)
        step = 1.0
        for _halving in range(HALVINGS):
            trial = weights - step * direction
            value = _objective(matrix, labels, trial, penalty, mask)
            if value <= current:
                break
            step *= 0.5
        else:
            break  # no step lowers the objective: converged to the float floor
        weights, current = trial, value
    return weights


@dataclass(frozen=True)
class Stacked:
    """A stack's out-of-fold answer on one set."""

    #: Each row's p, from the stack fitted without that row's fold.
    probabilities: list[float]
    #: Each row's fold.
    folds: list[int]
    #: Per fold, the row indices its scorer was fitted on (never a row of that fold).
    trained_on: Mapping[int, frozenset[int]]
    #: The stack fitted on every row: the weights the report prints, never one that scored a row.
    weights: list[float]


def check(columns: Sequence[Sequence[float]], expected: Sequence[bool], ids: Sequence[str]) -> str | None:
    """Why these rows cannot be stacked out of fold, or None when they can."""
    if len(columns) < 2:
        return "a stack needs two voters or more"
    if any(len(ps) != len(ids) for ps in columns) or len(expected) != len(ids):
        return "the voters answered different row counts"
    if len(set(ids)) != len(ids):
        return "two rows share an id, so their folds would too"
    opened = sum(bool(e) for e in expected)
    if len(ids) < 2 * FOLDS or opened in (0, len(ids)):
        return f"{len(ids)} rows ({opened} open) cannot fit a two-class stack in {FOLDS} folds"
    folds = [fold_of(row_id) for row_id in ids]
    for fold in sorted(set(folds)):
        if len({bool(e) for e, f in zip(expected, folds) if f != fold}) < 2:
            return f"fold {fold} leaves one class to fit on"
    return None


def out_of_fold(columns: Sequence[Sequence[float]], expected: Sequence[bool], ids: Sequence[str]) -> Stacked:
    """The stack of `columns` (one list of p per voter, rows aligned with `ids`), each row scored out of its fold.
    ValueError with `check`'s reason when the rows cannot be stacked."""
    why = check(columns, expected, ids)
    if why is not None:
        raise ValueError(f"stack: {why}")
    matrix = features(columns)
    labels = np.array([float(bool(e)) for e in expected])
    folds = [fold_of(row_id) for row_id in ids]
    probabilities = [0.0] * len(ids)
    trained_on: dict[int, frozenset[int]] = {}
    for fold in sorted(set(folds)):
        train = [i for i, f in enumerate(folds) if f != fold]
        test = [i for i, f in enumerate(folds) if f == fold]
        trained_on[fold] = frozenset(train)
        weights = fit_logistic(matrix[train], labels[train])
        for i, p in zip(test, sigmoid(matrix[test] @ weights)):
            probabilities[i] = float(p)
    whole = fit_logistic(matrix, labels)
    return Stacked(probabilities, folds, trained_on, [round(float(w), 4) for w in whole])


def verdict(
    stacked: Sequence[float], singles: Mapping[str, Sequence[float]], expected: Sequence[bool]
) -> dict[str, object] | None:
    """The stack against the best single voter (by AUC on this set): the paired ΔAUC, its interval, and `win` by the
    house rule (`lo` > 0). None when a class is empty."""
    from laya_lab.bench import paired_auc_delta  # bench imports compare, which imports this module
    from laya_lab.metrics import auc

    best, best_auc = None, -1.0
    for name, ps in singles.items():  # ties go to the voter named first
        score = auc(list(ps), list(expected))
        if score is not None and score > best_auc:
            best, best_auc = name, score
    if best is None:
        return None
    delta = paired_auc_delta(stacked, singles[best], expected)
    if delta is None:
        return None
    point, lo, hi = delta
    return {"best_single": best, "delta_auc": round(point, 4), "lo": round(lo, 4), "hi": round(hi, 4), "win": lo > 0}


def mistakes(
    probabilities: Mapping[str, Sequence[float]], expected: Sequence[bool], cuts: Mapping[str, float]
) -> list[dict[str, object]]:
    """For every ordered pair of voters, at each one's OWN cut: how many rows `a` gets wrong, how many of those `b`
    gets right, and that share. Both directions, because "the floor rescues Laya" and "Laya rescues the floor" are
    different claims."""
    right = {name: [(p >= cuts[name]) == bool(e) for p, e in zip(ps, expected)] for name, ps in probabilities.items()}
    out: list[dict[str, object]] = []
    for a in probabilities:
        for b in probabilities:
            if a == b:
                continue
            wrong = [i for i, ok in enumerate(right[a]) if not ok]
            rescued = sum(right[b][i] for i in wrong)
            out.append(
                {
                    "a": a,
                    "b": b,
                    "a_wrong": len(wrong),
                    "b_right": rescued,
                    "share": round(rescued / len(wrong), 3) if wrong else None,
                }
            )
    return out
