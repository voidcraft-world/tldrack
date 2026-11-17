"""THE κ: agreement between two binary labellers (the operator, Sonnet, Laya at its cut), and how sure it is.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

Each pair is `(said, truth)`: the labeller being measured first, the one standing for the truth (the operator)
second. κ and the agree rate are symmetric, so the order only matters to `precision` and `recall`.
`cohens_kappa` is chance-corrected: two labellers that both say "open" 90% of the time agree 82% by luck alone,
and κ reads that as 0. Below `KAPPA_MIN_PAIRS` every caller shows no κ at all rather than a noisy one
(laya-lab imports this module too).

**The interval.** Past its minimum a κ still moves a lot: the n 24 table (8, 2, 1, 13) reads κ 0.74 with a 95%
interval of 0.34 to 0.90, so a 0.74 is anything from fair to near-perfect, and a readout without the ± passes it
off as settled. `intervals` draws it by a Bayesian bootstrap over the confusion table (Rubin 1981). Every
statistic here is a function of the four cell shares, so each draw reweights them by Dirichlet(count + ½) and
reads κ, precision and recall off that one draw. The ½ per cell is Jeffreys' prior, and it is what keeps a table
with an empty cell honest: 24 agreements in 24 read κ 1.00 ±0.00 under the plain bootstrap, 0.73 to 1.00 here.
For precision and recall it is exactly the Jeffreys binomial interval (Brown, Cai and DasGupta 2001). The
interval is the percentile one, seeded by the table, so one table always draws one interval and a readout never
jitters between polls.

**The ±.** That interval is lopsided round κ (0.40 below 0.74, 0.16 above), so `half_width` is the FARTHER side,
never the symmetric `(hi − lo) / 2`: a symmetric ±0.28 would claim a floor of 0.46 the interval does not have,
and would call the perfect table settled at 24. `κ ± half_width` therefore always spans `lo` to `hi`,
and the readouts that print it and the deck's settled test (`half_width ≤ KAPPA_TARGET_HALF_WIDTH`) stay honest
with no rule of their own. The width falls as 1/√n but the lopsidedness as 1/n, so `pairs_for` SEARCHES for the
size at which the same table reaches ±0.15 rather than extrapolating: the table above gets there at about 125
pairs, where a 1/√n projection of its 0.40 asks for 167 and one of the symmetric width says 82.
"""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Callable, Iterable

KAPPA_MIN_PAIRS = 20
#: The κ half-width a settled agreement reads at; `kappa_ci.pairs_for_target` counts toward it.
KAPPA_TARGET_HALF_WIDTH = 0.15
#: `pairs_for` stops here: a table this far from settled reads "about this many", never a search without end.
PAIRS_FOR_CAP = 100_000
#: A precision or recall on fewer rows than this in its denominator is None, never a number.
RATE_MIN = 5
CI_LEVEL = 0.95
CI_DRAWS = 4000
#: Jeffreys' pseudo-count per confusion cell (the module note owns why).
CI_PRIOR = 0.5


def confusion(pairs: Iterable[tuple[bool, bool]]) -> dict[str, int]:
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for said, truth in pairs:
        counts[("t" if said == truth else "f") + ("p" if said else "n")] += 1
    return counts


def cells(pairs: Iterable[tuple[bool, bool]]) -> tuple[int, int, int, int]:
    """`(tp, fp, fn, tn)`: the table every statistic here is a function of."""
    counts = confusion(pairs)
    return counts["tp"], counts["fp"], counts["fn"], counts["tn"]


def kappa_of(tp: float, fp: float, fn: float, tn: float) -> float | None:
    """(po − pe) / (1 − pe), pe = p1a·p1b + p0a·p0b, on counts or shares. None at n = 0 or when chance agreement
    is total (both labellers constant and identical)."""
    n = tp + fp + fn + tn
    if n <= 0:
        return None
    po = (tp + tn) / n
    p1a = (tp + fp) / n
    p1b = (tp + fn) / n
    pe = p1a * p1b + (1 - p1a) * (1 - p1b)
    if pe >= 1:
        return None
    return (po - pe) / (1 - pe)


def precision_of(tp: float, fp: float, fn: float, tn: float) -> float | None:
    """Of what `said` flagged, the share the truth agrees is open. None when nothing was flagged."""
    return tp / (tp + fp) if tp + fp > 0 else None


def recall_of(tp: float, fp: float, fn: float, tn: float) -> float | None:
    """Of what the truth calls open, the share `said` flagged. None when nothing is open."""
    return tp / (tp + fn) if tp + fn > 0 else None


STATISTICS: dict[str, Callable[[float, float, float, float], float | None]] = {
    "kappa": kappa_of,
    "precision": precision_of,
    "recall": recall_of,
}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def agree_rate(pairs: Iterable[tuple[bool, bool]]) -> float | None:
    tp, fp, fn, tn = cells(pairs)
    n = tp + fp + fn + tn
    return None if n == 0 else round((tp + tn) / n, 3)


def cohens_kappa(pairs: Iterable[tuple[bool, bool]]) -> float | None:
    return _rounded(kappa_of(*cells(pairs)))


def precision(pairs: Iterable[tuple[bool, bool]]) -> float | None:
    return _rounded(precision_of(*cells(pairs)))


def recall(pairs: Iterable[tuple[bool, bool]]) -> float | None:
    return _rounded(recall_of(*cells(pairs)))


def percentile(ordered: list[float], q: float) -> float:
    """Linear interpolation between the closest ranks (numpy's default), on an already sorted list."""
    at = q * (len(ordered) - 1)
    low = math.floor(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def _drawn(shape: tuple[float, ...], level: float, draws: int) -> dict[str, tuple[float, float]]:
    """The draw itself, on counts that may be fractional: `pairs_for` probes a table scaled to a size it has not
    reached. Seeded by the counts, so one shape always draws one interval."""
    rng = random.Random(f"laya-agreement:{shape}:{draws}")
    drawn: dict[str, list[float]] = {name: [] for name in STATISTICS}
    for _ in range(draws):
        weights = [rng.gammavariate(count + CI_PRIOR, 1.0) for count in shape]
        total = sum(weights)
        shares = [weight / total for weight in weights]
        for name, statistic in STATISTICS.items():
            value = statistic(*shares)
            if value is not None:  # every cell's share is > 0, so only a float underflow lands here
                drawn[name].append(value)
    tail = (1 - level) / 2
    out: dict[str, tuple[float, float]] = {}
    for name, values in drawn.items():
        values.sort()
        out[name] = (round(percentile(values, tail), 3), round(percentile(values, 1 - tail), 3))
    return out


def _check_table(table: tuple[int, int, int, int], caller: str) -> None:
    if len(table) != 4 or any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in table):
        raise ValueError(f"{caller}: a table is four counts (tp, fp, fn, tn), got {table!r}")


@functools.lru_cache(maxsize=1024)
def intervals(table: tuple[int, int, int, int], level: float = CI_LEVEL, draws: int = CI_DRAWS) -> dict[str, tuple[float, float]]:
    """`{statistic: (lo, hi)}` at `level` for every statistic in `STATISTICS`, from one table `(tp, fp, fn, tn)`.

    Unconditional: the callers gate on their own minimums (`kappa_summary`, `rate_summary`), because below them
    the interval is mostly the prior. Cached by its arguments, so a poll that re-reads one table costs nothing.
    """
    _check_table(table, "intervals")
    if not 0 < level < 1 or draws < 2:
        raise ValueError(f"intervals: level {level} must be in (0, 1) and draws {draws} at least 2")
    return _drawn(table, level, draws)


def half_width(point: float, lo: float, hi: float) -> float:
    """The ± a readout prints beside `point`: its distance to the FARTHER end of `(lo, hi)` (the module note owns why)."""
    return round(max(point - lo, hi - point, 0.0), 3)


@functools.lru_cache(maxsize=1024)
def pairs_for(table: tuple[int, int, int, int], target: float = KAPPA_TARGET_HALF_WIDTH) -> int:
    """About how many pairs, in this table's proportions, bring κ's `half_width` down to `target`; `n` once it is
    there, and `n` when κ is undefined. Searched, not extrapolated (the module note owns why).

    Each probe draws the table scaled to m pairs with its shares unchanged, so κ stays put and only the interval
    moves. A bisection to within 2% (or one pair) of the answer: about six draws, once per table, then cached.
    """
    _check_table(table, "pairs_for")
    n = sum(table)
    point = _rounded(kappa_of(*table))
    if n == 0 or point is None:
        return n

    def half_at(m: int) -> float:
        drawn = intervals(table) if m == n else _drawn(tuple(count * m / n for count in table), CI_LEVEL, CI_DRAWS)
        lo, hi = drawn["kappa"]
        return half_width(point, lo, hi)

    now = half_at(n)
    if now <= target:
        return n
    cap = max(PAIRS_FOR_CAP, n + 1)
    low = n  # always too few
    high = min(cap, max(n + 1, math.ceil(n * (now / target) ** 2)))  # 1/√n of the farther side: almost always enough
    while half_at(high) > target:
        if high >= cap:
            return cap
        low, high = high, min(high * 2, cap)
    while high - low > max(1, high // 50):
        mid = (low + high) // 2
        if half_at(mid) <= target:
            high = mid
        else:
            low = mid
    return high


def kappa_summary(pairs: Iterable[tuple[bool, bool]], *, min_pairs: int = KAPPA_MIN_PAIRS) -> dict:
    """`{n, kappa, agree_rate, kappa_ci}`: one agreement as every readout shows it. κ and its interval are None below
    `min_pairs` and whenever κ itself is undefined; the agree rate is there from the first pair."""
    pairs = list(pairs)
    table = cells(pairs)
    kappa = cohens_kappa(pairs) if len(pairs) >= min_pairs else None
    ci = None
    if kappa is not None:
        lo, hi = intervals(table)["kappa"]
        ci = {
            "lo": lo,
            "hi": hi,
            "half_width": half_width(kappa, lo, hi),
            "level": CI_LEVEL,
            "target_half_width": KAPPA_TARGET_HALF_WIDTH,
            "pairs_for_target": pairs_for(table),
        }
    return {"n": len(pairs), "kappa": kappa, "agree_rate": agree_rate(pairs), "kappa_ci": ci}


def rate_summary(pairs: Iterable[tuple[bool, bool]], name: str, *, minimum: int = RATE_MIN) -> dict:
    """`{n, value, lo, hi}` for `precision` or `recall` at the cut that made `said`. `n` is its denominator (rows
    flagged, or rows truly open); below `minimum` of them the value and the interval are None."""
    if name not in ("precision", "recall"):
        raise ValueError(f"rate_summary: {name!r} is not precision or recall")
    table = cells(pairs)
    tp, fp, fn, _tn = table
    n = tp + fp if name == "precision" else tp + fn
    if n < minimum:
        return {"n": n, "value": None, "lo": None, "hi": None}
    lo, hi = intervals(table)[name]
    return {"n": n, "value": _rounded(STATISTICS[name](*table)), "lo": lo, "hi": hi}


def at_cut(pairs: Iterable[tuple[bool, bool]]) -> dict:
    """`{precision, recall}` of a cut, each a `rate_summary`, from `(flagged at the cut, truly open)` pairs."""
    pairs = list(pairs)
    return {"precision": rate_summary(pairs, "precision"), "recall": rate_summary(pairs, "recall")}
