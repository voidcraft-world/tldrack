"""THE κ: chance-corrected agreement between two binary labellers, and how sure it is."""

from __future__ import annotations

import pytest

from laya_bridge import agreement
from laya_bridge.agreement import KAPPA_MIN_PAIRS, agree_rate, cohens_kappa, confusion

T, F = True, False


def pairs(said, truth):
    return list(zip(said, truth))


def test_identical_varied_labels_are_kappa_one():
    assert cohens_kappa(pairs([T, F, T, F], [T, F, T, F])) == 1.0


def test_worse_than_chance_is_negative():
    assert cohens_kappa(pairs([T, T, F], [T, F, T])) == -0.5


def test_one_constant_labeller_is_kappa_zero():
    assert cohens_kappa(pairs([T, T], [T, F])) == 0.0


def test_constant_identical_labellers_have_no_kappa():
    assert cohens_kappa(pairs([T, T, T], [T, T, T])) is None
    assert cohens_kappa(pairs([F, F], [F, F])) is None


def test_empty_has_no_kappa_and_no_rate():
    assert cohens_kappa([]) is None and agree_rate([]) is None


def test_confusion_counts_said_against_truth():
    assert confusion(pairs([T, T, F, F, T], [T, F, T, F, T])) == {"tp": 2, "fp": 1, "fn": 1, "tn": 1}


def test_agree_rate_and_the_minimum():
    assert agree_rate(pairs([T, F, T], [T, F, F])) == 0.667
    assert KAPPA_MIN_PAIRS == 20


def test_kappa_accepts_a_generator():
    assert cohens_kappa((s, s) for s in (T, F, T)) == 1.0


# -- how sure: the interval, and precision/recall at a cut --------------------------------------


def table_pairs(tp, fp, fn, tn):
    return [(T, T)] * tp + [(T, F)] * fp + [(F, T)] * fn + [(F, F)] * tn


def test_precision_and_recall_read_said_against_truth():
    pairs = table_pairs(8, 2, 1, 13)
    assert agreement.precision(pairs) == 0.8 and agreement.recall(pairs) == 0.889
    assert agreement.precision(table_pairs(0, 0, 3, 3)) is None, "nothing flagged: no precision, never 0"
    assert agreement.recall(table_pairs(0, 3, 0, 3)) is None, "nothing open: no recall"


def test_kappa_of_reads_counts_and_shares_alike():
    assert agreement.kappa_of(8, 2, 1, 13) == pytest.approx(agreement.kappa_of(8 / 24, 2 / 24, 1 / 24, 13 / 24))
    assert agreement.kappa_of(0, 0, 0, 0) is None
    assert cohens_kappa(table_pairs(8, 2, 1, 13)) == round(agreement.kappa_of(8, 2, 1, 13), 3)


def test_kappa_at_n24_reads_its_downside_and_how_far_settled_is():
    """A worked case: a κ of ~0.74 at n 24 is anything from 0.34 to 0.90, so its ± is the 0.40 below it,
    and ±0.15 in the same proportions takes about 125 pairs."""
    summary = agreement.kappa_summary(table_pairs(8, 2, 1, 13))
    assert summary["n"] == 24 and summary["kappa"] == 0.739 and summary["agree_rate"] == 0.875
    ci = summary["kappa_ci"]
    assert ci["lo"] < summary["kappa"] < ci["hi"] <= 1
    assert ci["lo"] == pytest.approx(0.34, abs=0.03) and ci["hi"] == pytest.approx(0.90, abs=0.03)
    assert ci["half_width"] == round(summary["kappa"] - ci["lo"], 3), "the farther side is below κ"
    assert ci["level"] == 0.95 and ci["target_half_width"] == agreement.KAPPA_TARGET_HALF_WIDTH == 0.15
    assert 110 <= ci["pairs_for_target"] <= 140
    assert ci["pairs_for_target"] == agreement.pairs_for((8, 2, 1, 13))


def test_no_readout_claims_a_floor_or_ceiling_the_interval_does_not_have():
    """κ ± half_width spans the whole interval on every table, lopsided or not: a symmetric ± read 0.74 ±0.28,
    a floor of 0.46 where the interval's is 0.34."""
    for table in ((8, 2, 1, 13), (12, 0, 0, 12), (1, 0, 0, 19), (6, 3, 2, 13), (15, 3, 2, 4), (2, 9, 8, 5), (60, 6, 8, 76)):
        summary = agreement.kappa_summary(table_pairs(*table))
        kappa, ci = summary["kappa"], summary["kappa_ci"]
        assert kappa - ci["half_width"] <= ci["lo"] + 1e-9, table
        assert kappa + ci["half_width"] >= ci["hi"] - 1e-9, table
        assert 0 <= ci["half_width"] <= 1, f"{table}: the wire's p01"
    assert agreement.half_width(0.739, 0.344, 0.898) == 0.395
    assert agreement.half_width(1.0, 0.729, 0.998) == 0.271, "κ past hi: the ± still reaches lo"


def test_settled_means_the_downside_is_within_the_target():
    """75 pairs at κ 0.81 used to read settled by the symmetric width; its downside is 0.18, so it is not yet."""
    short = agreement.kappa_summary(table_pairs(30, 3, 4, 38))["kappa_ci"]
    assert short["half_width"] > 0.15 and 80 <= short["pairs_for_target"] <= 115
    settled = agreement.kappa_summary(table_pairs(60, 6, 8, 76))["kappa_ci"]
    assert settled["half_width"] <= 0.15 and settled["pairs_for_target"] == 150, "once settled, the count is n itself"


def test_a_perfect_table_still_gets_a_width_and_is_not_settled_at_24():
    """The Jeffreys ½ per cell: 24 of 24 agreements is not κ 1.00 ±0.00, and its 0.73 floor is not ±0.15 away."""
    summary = agreement.kappa_summary(table_pairs(12, 0, 0, 12))
    ci = summary["kappa_ci"]
    assert summary["kappa"] == 1.0 and ci["lo"] < 0.8 and ci["hi"] < 1.0
    assert ci["half_width"] == round(1.0 - ci["lo"], 3) > 0.15
    assert 30 < ci["pairs_for_target"] < 80


def test_below_the_minimum_or_undefined_there_is_no_kappa_and_no_interval():
    short = agreement.kappa_summary(table_pairs(8, 1, 1, 9))  # 19 pairs
    assert short["kappa"] is None and short["kappa_ci"] is None and short["agree_rate"] == 0.895
    constant = agreement.kappa_summary(table_pairs(25, 0, 0, 0))
    assert constant["kappa"] is None and constant["kappa_ci"] is None
    assert agreement.kappa_summary([]) == {"n": 0, "kappa": None, "agree_rate": None, "kappa_ci": None}


def test_one_table_always_draws_one_interval():
    """Seeded by the table, never by call order: a poll never jitters, and a fresh cache draws the same."""
    first = agreement.intervals((8, 2, 1, 13))
    agreement.intervals.cache_clear()
    agreement.intervals((30, 3, 4, 38))
    assert agreement.intervals((8, 2, 1, 13)) == first
    assert (
        agreement.kappa_summary(list(reversed(table_pairs(8, 2, 1, 13))))["kappa_ci"]
        == agreement.kappa_summary(table_pairs(8, 2, 1, 13))["kappa_ci"]
    ), "order of pairs never matters"


def test_the_interval_narrows_with_n_and_its_lopsidedness_faster():
    """The width falls as 1/√n and the skew as 1/n, which is why `pairs_for` searches instead of extrapolating."""
    widths, skews = [], []
    for k in (1, 2, 4):
        lo, hi = agreement.intervals((8 * k, 2 * k, k, 13 * k))["kappa"]
        widths.append((hi - lo) / 2)
        skews.append(abs(0.739 - (lo + hi) / 2))
    assert widths[0] > widths[1] > widths[2]
    assert widths[2] == pytest.approx(widths[0] / 2, abs=0.04), "the width falls as 1/√n"
    assert skews[2] < skews[0] / 3, "the skew falls faster than the width"
    extrapolated = 24 * (agreement.kappa_summary(table_pairs(8, 2, 1, 13))["kappa_ci"]["half_width"] / 0.15) ** 2
    assert agreement.pairs_for((8, 2, 1, 13)) < extrapolated * 0.85, "a 1/√n projection of the farther side over-asks"


def test_rates_at_a_cut_have_jeffreys_intervals_and_their_own_minimum():
    rates = agreement.at_cut(table_pairs(8, 2, 1, 13))
    precision, recall = rates["precision"], rates["recall"]
    assert precision["n"] == 10 and precision["value"] == 0.8 and precision["lo"] < 0.8 < precision["hi"] <= 1
    assert recall["n"] == 9 and recall["value"] == 0.889
    # Beta(8.5, 2.5), the Jeffreys interval for 8 of 10: its 2.5% and 97.5% points are about 0.49 and 0.96
    assert precision["lo"] == pytest.approx(0.49, abs=0.03) and precision["hi"] == pytest.approx(0.96, abs=0.02)
    few = agreement.at_cut(table_pairs(3, 1, 1, 19))
    assert few["precision"] == {"n": 4, "value": None, "lo": None, "hi": None}, "under RATE_MIN flagged: no number"
    assert agreement.RATE_MIN == 5


def test_pairs_for_and_the_refusals():
    assert agreement.pairs_for((0, 0, 0, 0)) == 0 and agreement.pairs_for((25, 0, 0, 0)) == 25, "no κ: nothing to count toward"
    assert agreement.pairs_for((60, 6, 8, 76), target=0.5) == 150
    with pytest.raises(ValueError):
        agreement.pairs_for((1, 2, 3))
    with pytest.raises(ValueError):
        agreement.intervals((1, 2, 3))
    with pytest.raises(ValueError):
        agreement.intervals((1, 2, 3, -1))
    with pytest.raises(ValueError):
        agreement.intervals((1, 2, 3, 4), level=1.0)
    with pytest.raises(ValueError):
        agreement.rate_summary(table_pairs(1, 1, 1, 1), "kappa")
