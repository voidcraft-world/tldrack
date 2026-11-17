"""κ through the one owner, `at_cut`, and the cut rule — synthetic numbers only."""

from __future__ import annotations

import pytest

from laya_lab import metrics
from laya_bridge import agreement

T, F = True, False


def test_kappa_is_the_bridge_owner_not_a_copy():
    assert metrics.cohens_kappa is agreement.cohens_kappa


def test_kappa_vectors():
    kappa = metrics.cohens_kappa
    assert kappa(zip([T, F, T, F], [T, F, T, F])) == 1.0
    assert kappa(zip([T, T, F], [T, F, T])) == -0.5
    assert kappa(zip([T, T], [T, F])) == 0.0
    assert kappa(zip([T, T, T], [T, T, T])) is None  # constant on both sides: chance agreement is total
    assert kappa([]) is None


def test_at_cut_carries_kappa_and_the_confusion():
    entry = metrics.at_cut([0.9, 0.7, 0.2], [T, F, T], 0.6)
    assert entry == {"threshold": 0.6, "accuracy": 0.333, "false_warnings": 1, "missed_open": 1, "tp": 1, "tn": 0, "kappa": -0.5}
    assert metrics.at_cut([], [], 0.5)["accuracy"] is None
    assert metrics.brier([], []) is None


def test_the_fine_grid():
    assert metrics.SWEEP_FINE[0] == 0.3 and metrics.SWEEP_FINE[-1] == 0.9 and len(metrics.SWEEP_FINE) == 13
    assert 0.7 in metrics.SWEEP_FINE and 0.65 in metrics.SWEEP_FINE


def entry(cut, fp, fn, kappa):
    return {"threshold": cut, "false_warnings": fp, "missed_open": fn, "kappa": kappa}


def test_pick_cut_on_the_three_cut_table():
    table = [entry(0.5, 8, 11, 0.40), entry(0.6, 3, 13, 0.45), entry(0.7, 1, 16, 0.42)]
    assert metrics.pick_cut(table, 2.0) == {"at": 0.7, "cost": 18.0, "max_kappa_at": 0.6}
    assert metrics.pick_cut(list(reversed(table)), 2.0)["at"] == 0.7
    assert metrics.pick_cut(table, 1.0)["at"] == 0.6  # cost 19 / 16 / 17 when a false ⚠️ costs no more


def test_pick_cut_ties_go_to_the_higher_cut():
    tie = [entry(0.5, 2, 4, 0.3), entry(0.6, 1, 6, 0.3)]
    assert metrics.pick_cut(tie, 2.0) == {"at": 0.6, "cost": 8.0, "max_kappa_at": 0.6}
    assert metrics.pick_cut([entry(0.5, 1, 1, None)], 2.0)["max_kappa_at"] is None
    assert metrics.pick_cut([], 2.0) == {"at": None, "cost": None, "max_kappa_at": None}


def test_auc_ranks_without_a_cut():
    assert metrics.auc([0.9, 0.8, 0.2, 0.1], [T, T, F, F]) == 1.0
    assert metrics.auc([0.1, 0.2, 0.8, 0.9], [T, T, F, F]) == 0.0
    assert metrics.auc([0.5, 0.5], [T, F]) == 0.5  # a tie counts half
    assert metrics.auc([0.99, 0.98, 0.97], [T, F, T]) == 0.5  # runs hot, still ranks: AUC is blind to calibration
    assert metrics.auc([0.9], [T]) is None and metrics.auc([], []) is None


def test_ece_reads_calibration_the_auc_cannot():
    assert metrics.ece([1.0, 0.0], [T, F]) == 0.0
    assert metrics.ece([0.95, 0.95], [T, F]) == 0.45  # says 95%, comes true half the time
    assert metrics.ece([0.25, 0.25, 0.25, 0.25], [T, F, F, F]) == 0.0
    assert metrics.ece([], []) is None


def test_every_readout_prints_a_metric_one_way():
    assert (metrics.fmt_num(None), metrics.fmt_num(0.5), metrics.fmt_num(0.04, 4), metrics.fmt_num(-0.25)) == (
        "—",
        "0.50",
        "0.0400",
        "-0.25",
    )


def test_at_done_cut_counts_the_check_band():
    entry = metrics.at_done_cut([0.1, 0.3, 0.5, 0.9], [F, T, F, T], 0.4)
    assert entry == {"threshold": 0.4, "false_done": 1, "missed_done": 1, "shown": 2}


def test_pick_done_cut_stays_under_the_open_cut_and_ties_low():
    entries = [metrics.at_done_cut([0.1, 0.1, 0.6], [F, F, T], cut) for cut in metrics.SWEEP_DONE]
    picked = metrics.pick_done_cut(entries, 3.0, 0.5)
    assert picked == {"at": 0.15, "cost": 0.0}, "every cut in (0.1, 0.5) is perfect; the lowest wins"
    assert metrics.pick_done_cut(entries, 3.0, 0.05) == {"at": None, "cost": None}
    # A false ✅ costs three missed ones: one open text at 0.2 keeps the cut at or under it.
    costly = [metrics.at_done_cut([0.1, 0.25, 0.3, 0.2], [F, F, F, T], cut) for cut in metrics.SWEEP_DONE]
    assert metrics.pick_done_cut(costly, 3.0, 0.7)["at"] == 0.15
    assert metrics.SWEEP_DONE[0] == 0.05 and metrics.SWEEP_DONE[-1] == 0.65


# -- trust and calibration -----------------------------------------------------------------------


def test_trust_at_cut_is_the_deck_s_own_readout():
    probabilities = [0.9] * 12 + [0.1] * 12
    expected = [T] * 11 + [F] + [F] * 11 + [T]
    trust = metrics.trust_at_cut(probabilities, expected, 0.7)
    pairs = [(p >= 0.7, e) for p, e in zip(probabilities, expected)]
    assert trust["kappa_ci"] == agreement.kappa_summary(pairs)["kappa_ci"]
    assert trust["kappa"] == 0.833 and trust["n"] == 24
    assert trust["precision"] == agreement.rate_summary(pairs, "precision") and trust["recall"]["value"] == 0.917
    assert metrics.fmt_kappa(trust["kappa"], trust["kappa_ci"]) == f"0.83 ±{trust['kappa_ci']['half_width']:.2f}"
    assert metrics.fmt_kappa(0.5, None) == "0.50" and metrics.fmt_kappa(None, trust["kappa_ci"]) == "—"


def test_reliability_bins_sit_around_both_cuts():
    probabilities = [0.1, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0]
    bins = metrics.reliability(probabilities, [F, F, F, F, T, T, T, T], (0.7, 0.4))
    assert [(b["lo"], b["hi"]) for b in bins] == [(0.0, 0.3), (0.3, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.0)]
    assert [b["cuts"] for b in bins] == [[], [0.4], [], [0.7], []]
    assert [b["n"] for b in bins] == [1, 2, 1, 2, 2], "p = 1.0 lands in the last bin"
    assert bins[1]["p"] == 0.4 and bins[1]["rate"] == 0.0 and bins[3]["rate"] == 1.0


def test_reliability_keeps_empty_bins_and_merges_close_cuts():
    bins = metrics.reliability([0.5], [T], (0.45, 0.4, None, 1.2))
    assert [(b["lo"], b["hi"]) for b in bins] == [(0.0, 0.3), (0.3, 0.35), (0.35, 0.5), (0.5, 0.55), (0.55, 1.0)]
    assert bins[0] == {"lo": 0.0, "hi": 0.3, "n": 0, "p": None, "rate": None, "cuts": []}
    assert bins[2]["cuts"] == [0.4, 0.45]
    assert [(b["lo"], b["hi"]) for b in metrics.reliability([], [], ())] == [(0.0, 1.0)]


def calibrated_rows(n=2000, seed=1):
    import random

    rng = random.Random(seed)
    probabilities = [rng.random() for _ in range(n)]
    return probabilities, [rng.random() < p for p in probabilities]


def test_one_temperature_finds_the_sharpening_and_undoes_it():
    probabilities, expected = calibrated_rows()
    assert metrics.fit_temperature(probabilities, expected) == pytest.approx(1.0, abs=0.1)
    sharpened = metrics.scale(probabilities, 0.5)  # an over-confident voter: logits doubled
    assert metrics.fit_temperature(sharpened, expected) == pytest.approx(2.0, abs=0.2)
    assert metrics.ece(sharpened, expected) > 0.08
    assert metrics.refit_ece(sharpened, expected) < 0.04, "out of fold, one T undoes most of it"


def test_temperature_needs_both_classes_and_something_to_scale():
    assert metrics.fit_temperature([0.9, 0.8], [T, T]) is None
    assert metrics.fit_temperature([0.5, 0.5], [T, F]) is None
    assert metrics.fit_temperature([], []) is None
    assert metrics.fit_temperature([0.9, 0.1], [T, F]) == metrics.TEMPERATURE_BOUNDS[0], "separable: clamped, not infinite"
    assert metrics.scale([0.5, 0.9], 1.0) == pytest.approx([0.5, 0.9])
    assert metrics.refit_ece([], []) is None


def test_calibration_sets_operator_ece_beside_teacher_ece_on_the_same_rows():
    probabilities = [0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    operator = [T, F, T, F, T, F]
    teacher = [T, T, T, F, F, None]
    block = metrics.calibration(probabilities, operator, teacher, (0.7, 0.4))
    assert block["rows"] == 6 and block["ece"] == metrics.ece(probabilities, operator)
    assert block["teacher"] == {"rows": 5, "ece": metrics.ece(probabilities[:5], [T, T, T, F, F])}
    assert block["teacher"]["ece"] < block["ece"], "it agrees with its teacher better than with the operator"
    assert block["temperature"] > 1, "the operator disagrees a third of the time: a refit softens it"
    assert [b["cuts"] for b in block["reliability"]] == [[], [0.4], [], [0.7], []]
