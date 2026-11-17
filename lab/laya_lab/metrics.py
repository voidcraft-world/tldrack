"""Scoring a binary verdict at a cut: the confusion, κ, Brier, calibration, and which cut a sweep recommends. Pure.

κ is `laya_bridge.agreement.cohens_kappa` — THE κ, shared with the REVIEW deck, never a copy here; so is its
interval, and the precision and recall at a cut (`trust_at_cut`).
The cut rule (`pick_cut`): owned by bridge/tldrack/open_work.py
`FALSE_WARNING_COST` — see the note there.

**Calibration** (`reliability`, `fit_temperature`, `calibration`). ECE is one number over ten equal bins, and most
of them sit where no verdict turns. `reliability` puts an edge `CUT_BAND` either side of each live cut, so the
bins that decide a ✅ (0.4) and a ⚠️ (0.7) are read on their own. Laya's temperatures are fitted on the teacher's
labels (Sonnet's, inside `laya.train.finetune`), but the operator is the one it answers to. So once a sweep runs on
`rows.SWEEP_MIN_ROWS` operator labels, `calibration` sets operator-ECE beside teacher-ECE on the SAME rows and the
same p, so only the labels differ. It then refits one temperature on the operator's labels (Guo et al. 2017,
"On Calibration of Modern Neural Networks": a single T fixes most miscalibration) and reports the ECE that T
reaches out of fold, since a T fitted and scored on the same rows flatters itself. It is a measurement only:
nothing here writes a temperature back to a checkpoint.
"""

from __future__ import annotations

import math
from typing import Any
from collections.abc import Iterable, Sequence

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_bridge import agreement
from laya_bridge.agreement import cohens_kappa

#: The golden suite's coarse sweep (its reports have always carried these seven).
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
#: The held-out sweep's grid: 0.30, 0.35, …, 0.90.
SWEEP_FINE = tuple(round(0.30 + 0.05 * i, 2) for i in range(13))
#: The ✅ cut's grid: 0.05, 0.10, …, 0.65. `pick_done_cut` keeps the pick under the ⚠️ cut.
SWEEP_DONE = tuple(round(0.05 * i, 2) for i in range(1, 14))


def at_cut(probabilities: Sequence[float], expected: Sequence[bool], cut: float) -> dict[str, Any]:
    """Accuracy, false ⚠️ (fp), missed open work (fn), tp, tn and κ when `p ≥ cut` says open."""
    said = [p >= cut for p in probabilities]
    pairs = list(zip(said, expected))
    tp = sum(s and e for s, e in pairs)
    fp = sum(s and not e for s, e in pairs)
    fn = sum(e and not s for s, e in pairs)
    tn = len(pairs) - tp - fp - fn
    return {
        "threshold": cut,
        "accuracy": round((tp + tn) / len(pairs), 3) if pairs else None,
        "false_warnings": fp,
        "missed_open": fn,
        "tp": tp,
        "tn": tn,
        "kappa": cohens_kappa(pairs),
    }


def fmt_num(value: float | None, digits: int = 2) -> str:
    """A metric as every laya-lab readout prints it: fixed digits, `—` when there is none (κ at n = 0)."""
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_kappa(value: float | None, ci: dict | None) -> str:
    """κ as every laya-lab readout prints it: `0.74 ±0.40` once it has its interval, `fmt_num`'s otherwise."""
    return fmt_num(value) if value is None or not ci else f"{value:.2f} ±{ci['half_width']:.2f}"


def fmt_report_kappa(report: dict[str, Any]) -> str:
    """A golden or sweep report's κ at its live cut, with the interval its `trust` block carries (an older report
    has none, and reads the bare value)."""
    return fmt_kappa(report["at_threshold"]["kappa"], (report.get("trust") or {}).get("kappa_ci"))


def trust_at_cut(probabilities: Sequence[float], expected: Sequence[bool], cut: float) -> dict[str, Any]:
    """`{kappa, kappa_ci, n, precision, recall}` of `p ≥ cut` against `expected`, with their 95% intervals: the
    REVIEW deck's own readout (`agreement.kappa_summary`, `agreement.at_cut`), so a report and the deck agree."""
    pairs = [(p >= cut, e) for p, e in zip(probabilities, expected)]
    summary = agreement.kappa_summary(pairs)
    return {"n": summary["n"], "kappa": summary["kappa"], "kappa_ci": summary["kappa_ci"], **agreement.at_cut(pairs)}


def brier(probabilities: Sequence[float], expected: Sequence[bool]) -> float | None:
    if not probabilities:
        return None
    return round(sum((p - float(e)) ** 2 for p, e in zip(probabilities, expected)) / len(probabilities), 4)


def auc(probabilities: Sequence[float], expected: Sequence[bool]) -> float | None:
    """ROC AUC (Mann–Whitney, a tie counts half): how well `p` RANKS open above done, at no cut.
    The fair number between voters calibrated differently. None without both classes."""
    pos = [p for p, e in zip(probabilities, expected) if e]
    neg = [p for p, e in zip(probabilities, expected) if not e]
    if not pos or not neg:
        return None
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def ece(probabilities: Sequence[float], expected: Sequence[bool], bins: int = 10) -> float | None:
    """Expected calibration error over `bins` equal-width bins: how far a stated `p` sits from the
    open rate it came true at. 0 is calibrated; a voter can rank well (AUC) and still be far off here."""
    if not probabilities:
        return None
    total = 0.0
    for b in range(bins):
        idx = [i for i, p in enumerate(probabilities) if b / bins <= p < (b + 1) / bins or (b == bins - 1 and p == 1.0)]
        if idx:
            confidence = sum(probabilities[i] for i in idx) / len(idx)
            rate = sum(float(expected[i]) for i in idx) / len(idx)
            total += len(idx) / len(probabilities) * abs(confidence - rate)
    return round(total, 4)


#: Reliability bins put an edge this far either side of each live cut (the module note owns why).
CUT_BAND = 0.1
#: How far one refit temperature may go: past these a fit is chasing a separable sample, not calibrating.
TEMPERATURE_BOUNDS = (0.05, 20.0)
#: The refit's ECE is read out of fold: row i is scaled by a T fitted on the rows outside fold `i % REFIT_FOLDS`.
REFIT_FOLDS = 5
_EPS = 1e-6


def reliability(
    probabilities: Sequence[float], expected: Sequence[bool], cuts: Iterable[float | None], band: float = CUT_BAND
) -> list[dict[str, Any]]:
    """Calibration bins with an edge `band` either side of each cut: with the ✅ cut 0.4 and the ⚠️ cut 0.7,
    [0, .3) [.3, .5) [.5, .6) [.6, .8) [.8, 1]. Each bin: `lo`, `hi`, `n`, mean `p`, the open `rate` it came true
    at (None in an empty bin, which is kept so every checkpoint's table lines up) and the `cuts` inside it."""
    live = sorted({round(cut, 4) for cut in cuts if cut is not None and 0 < cut < 1})
    edges = sorted({0.0, 1.0, *(round(min(1.0, max(0.0, cut + side)), 4) for cut in live for side in (-band, band))})
    bins = []
    for lo, hi in zip(edges, edges[1:]):
        idx = [i for i, p in enumerate(probabilities) if lo <= p < hi or (hi == 1.0 and p == 1.0)]
        bins.append(
            {
                "lo": lo,
                "hi": hi,
                "n": len(idx),
                "p": round(sum(probabilities[i] for i in idx) / len(idx), 4) if idx else None,
                "rate": round(sum(float(expected[i]) for i in idx) / len(idx), 4) if idx else None,
                "cuts": [cut for cut in live if lo <= cut < hi],
            }
        )
    return bins


def _logit(p: float) -> float:
    q = min(max(p, _EPS), 1 - _EPS)
    return math.log(q / (1 - q))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def _log_sigmoid(x: float) -> float:
    """log σ(x) without overflow at either end."""
    return -math.log1p(math.exp(-x)) if x >= 0 else x - math.log1p(math.exp(x))


def scale(probabilities: Sequence[float], temperature: float) -> list[float]:
    """Each p through one temperature: σ(logit(p) / T). T > 1 softens, T < 1 sharpens, T = 1 is the identity."""
    return [_sigmoid(_logit(p) / temperature) for p in probabilities]


def fit_temperature(probabilities: Sequence[float], expected: Sequence[bool]) -> float | None:
    """The one T in `TEMPERATURE_BOUNDS` minimising the log loss of `scale(p, T)` against `expected`: binary
    temperature scaling (Guo et al. 2017). The loss is convex in 1/T, so a golden-section search over 1/T finds it.
    None without both classes, or when every p is 0.5 (there is nothing to scale)."""
    pairs = [(_logit(p), bool(e)) for p, e in zip(probabilities, expected)]
    if not any(e for _z, e in pairs) or all(e for _z, e in pairs) or all(abs(z) < 1e-9 for z, _e in pairs):
        return None

    def loss(beta: float) -> float:
        return -sum(_log_sigmoid(beta * z) if e else _log_sigmoid(-beta * z) for z, e in pairs)

    lo, hi = 1 / TEMPERATURE_BOUNDS[1], 1 / TEMPERATURE_BOUNDS[0]
    ratio = (math.sqrt(5) - 1) / 2
    a, b = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
    fa, fb = loss(a), loss(b)
    for _ in range(100):
        if fa <= fb:
            hi, b, fb = b, a, fa
            a = hi - ratio * (hi - lo)
            fa = loss(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + ratio * (hi - lo)
            fb = loss(b)
    return round(1 / ((lo + hi) / 2), 3)


def refit_ece(probabilities: Sequence[float], expected: Sequence[bool], folds: int = REFIT_FOLDS) -> float | None:
    """ECE after one temperature, out of fold: each row is scaled by a T fitted on the other folds' rows (a fold
    whose rest lacks a class keeps its raw p). The honest number for what a refit would buy."""
    if not probabilities:
        return None
    scaled = list(probabilities)
    for fold in range(folds):
        rest = [i for i in range(len(probabilities)) if i % folds != fold]
        temperature = fit_temperature([probabilities[i] for i in rest], [expected[i] for i in rest])
        if temperature is None:
            continue
        for i in range(fold, len(probabilities), folds):
            scaled[i] = scale([probabilities[i]], temperature)[0]
    return ece(scaled, expected)


def calibration(
    probabilities: Sequence[float], operator: Sequence[bool], teacher: Sequence[bool | None], cuts: Iterable[float | None]
) -> dict[str, Any]:
    """Operator-ECE beside teacher-ECE on the same rows, one temperature refit on the operator's labels and the ECE
    it reaches out of fold, and the operator's reliability bins. `teacher[i]` is Sonnet's label for row i, None
    where it has none; teacher-ECE reads only those rows. The module note owns why."""
    taught = [(p, t) for p, t in zip(probabilities, teacher) if t is not None]
    return {
        "rows": len(probabilities),
        "ece": ece(probabilities, operator),
        "teacher": {"rows": len(taught), "ece": ece([p for p, _t in taught], [t for _p, t in taught])},
        "temperature": fit_temperature(probabilities, operator),
        "ece_refit": refit_ece(probabilities, operator),
        "reliability": reliability(probabilities, operator, cuts),
    }


def at_done_cut(probabilities: Sequence[float], expected: Sequence[bool], cut: float) -> dict[str, Any]:
    """The ✅ band at `cut` (`p < cut` draws ✅): false ✅ (open work drawn done), missed ✅ (done work left
    without one) and how many ✅ it draws."""
    said = [p < cut for p in probabilities]
    pairs = list(zip(said, expected))
    return {
        "threshold": cut,
        "false_done": sum(s and e for s, e in pairs),
        "missed_done": sum(not s and not e for s, e in pairs),
        "shown": sum(said),
    }


def pick_done_cut(entries: Iterable[dict[str, Any]], fp_cost: float, ceiling: float | None) -> dict[str, Any]:
    """`{at, cost}` over `at_done_cut` entries at or under `ceiling` (the ⚠️ cut): the cut minimising
    `fp_cost·false_done + missed_done`, ties to the LOWER cut, since a false ✅ is the costly side. The cost
    rule: owned by bridge/tldrack/open_work.py `FALSE_DONE_COST` — see the note there."""
    allowed = [entry for entry in entries if ceiling is None or entry["threshold"] < ceiling]
    if not allowed:
        return {"at": None, "cost": None}
    cost = {entry["threshold"]: fp_cost * entry["false_done"] + entry["missed_done"] for entry in allowed}
    best = min(allowed, key=lambda entry: (round(cost[entry["threshold"]], 9), entry["threshold"]))
    return {"at": best["threshold"], "cost": round(cost[best["threshold"]], 4)}


def pick_cut(entries: Iterable[dict[str, Any]], fp_cost: float) -> dict[str, Any]:
    """`{at, cost, max_kappa_at}` over `at_cut` entries: the cut minimising `fp_cost·fp + fn`, ties
    to the HIGHER cut; `max_kappa_at` is the best-κ cut beside it (ties higher too; None without κ)."""
    entries = list(entries)
    if not entries:
        return {"at": None, "cost": None, "max_kappa_at": None}
    cost = {entry["threshold"]: fp_cost * entry["false_warnings"] + entry["missed_open"] for entry in entries}
    best = min(entries, key=lambda entry: (round(cost[entry["threshold"]], 9), -entry["threshold"]))
    with_kappa = [entry for entry in entries if entry.get("kappa") is not None]
    top = max(with_kappa, key=lambda entry: (entry["kappa"], entry["threshold"])) if with_kappa else None
    return {"at": best["threshold"], "cost": round(cost[best["threshold"]], 4), "max_kappa_at": top["threshold"] if top else None}


# ── paired: two voters on the same rows ───────────────────────────────────────


def warning_costs(probabilities: Sequence[float], expected: Sequence[bool], cut: float, fp_cost: float) -> list[float]:
    """Each row's ⚠️-cut cost: `pick_cut`'s rule on that one row's confusion, so the rows sum to the set's
    cost and the weights are never written out a second time."""
    return [pick_cut([at_cut([p], [e], cut)], fp_cost)["cost"] for p, e in zip(probabilities, expected)]


def done_costs(probabilities: Sequence[float], expected: Sequence[bool], cut: float, fp_cost: float) -> list[float]:
    """Each row's ✅-cut cost: `pick_done_cut`'s rule on that one row (no ceiling: the cut is already chosen)."""
    return [pick_done_cut([at_done_cut([p], [e], cut)], fp_cost, None)["cost"] for p, e in zip(probabilities, expected)]


def paired_test(candidate: Sequence[float], served: Sequence[float]) -> dict[str, Any]:
    """McNemar's paired test on per-row costs: is the candidate WORSE than the served model on these rows?

    Only discordant rows count (a row both got right, or both got wrong the same way, says nothing about
    which is better). Under the null the two voters are exchangeable, so each discordant row's cost
    difference is as likely to point either way; `p` is the exact one-sided chance of a total at least
    this far against the candidate. With equal weights it IS the exact McNemar (binomial) test; the cost
    rule's weights make a false ⚠️ count FALSE_WARNING_COST times a miss. Pure; exact up to ~10³ rows.
    """
    if len(candidate) != len(served):
        raise ValueError(f"paired_test: {len(candidate)} candidate rows vs {len(served)} served rows")
    deltas = [round(c - s, 6) for c, s in zip(candidate, served) if round(c - s, 6) != 0]
    observed = round(sum(deltas), 6)
    counts: dict[float, int] = {0.0: 1}
    for delta in deltas:
        step: dict[float, int] = {}
        for total, ways in counts.items():
            for signed in (delta, -delta):
                key = round(total + signed, 6)
                step[key] = step.get(key, 0) + ways
        counts = step
    at_least = sum(ways for total, ways in counts.items() if total >= observed - 1e-9)
    return {
        "candidate": round(sum(candidate), 4),
        "served": round(sum(served), 4),
        "delta": observed,
        "rows": len(candidate),
        "discordant": len(deltas),
        "worse": sum(d > 0 for d in deltas),
        "better": sum(d < 0 for d in deltas),
        "p": round(at_least / 2 ** len(deltas), 6),
    }


def log_loss(probabilities: Sequence[float], expected: Sequence[bool], eps: float = 1e-6) -> float | None:
    """Mean binary cross-entropy of `p(open)` against the label; `p` clipped to [eps, 1 − eps]."""
    if not probabilities:
        return None
    total = 0.0
    for p, e in zip(probabilities, expected):
        q = min(max(p, eps), 1 - eps)
        total -= math.log(q if e else 1 - q)
    return round(total / len(probabilities), 4)
