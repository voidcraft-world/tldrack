"""κ through the one owner, `at_cut`, and the cut rule — synthetic numbers only."""

from __future__ import annotations

import json

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


# ── felt: false ⚠️ per 100, missed 🙋 per 100, the 💬 share ─────────────────────


def turns(rows: list[tuple[int, str, bool, bool | None, str | None]]) -> list[metrics.FeltTurn]:
    """`(how many, band, shown, open, grade)` rows, expanded: an invented table."""
    return [metrics.FeltTurn(band, shown, is_open, grade) for count, band, shown, is_open, grade in rows for _ in range(count)]


TABLE = [
    (3, "open", T, F, "done"),  # shown ⚠️ graded not open: the false ⚠️
    (5, "open", T, T, "you"),  # shown ⚠️ that was needed
    (2, "done", F, T, "you"),  # NEEDS YOU under a ✅: the costly miss
    (1, "reply", F, T, "you"),  # NEEDS YOU under a 💬
    (1, "open", F, T, "you"),  # NEEDS YOU under a ⚠️ the rack held back
    (1, "reply", F, T, None),  # OPEN nobody split: counted apart
    (1, "reply", F, T, "running"),  # open, but the agent was waiting on its own work: no miss
    (2, "reply", F, F, "read"),
    (4, "done", F, F, "done"),
    (1, "reply", F, F, None),  # a bare DONE from before READ: counted done
    (4, "reply", F, None, None),  # sampled, never graded
    (2, "done", F, None, None),
]


def test_felt_counts_each_number_on_its_own_rows():
    found = metrics.felt(turns(TABLE))
    assert (found["basis"], found["sampled"], found["graded"], found["min_n"]) == ("live_sample", 27, 21, metrics.FELT_MIN_N)
    false = found["false_warning"]
    assert (false["count"], false["n"], false["value"]) == (3, 21, 14.3)
    missed = found["missed_you"]
    assert (missed["count"], missed["n"], missed["value"]) == (4, 21, 19.0)
    assert {name: missed[name]["count"] for name in metrics.MISSED_UNDER} == {"done": 2, "reply": 1, "held": 1}
    assert missed["unsplit"] == 1
    reply = found["reply"]
    assert (reply["count"], reply["n"], reply["value"], reply["graded"]) == (10, 27, 0.37, 6)
    assert {name: reply[name]["count"] for name in ("open", "done", "read")} == {"open": 3, "done": 1, "read": 2}


def test_under_the_minimum_n_a_rate_is_its_count_alone():
    found = metrics.felt(turns(TABLE))
    assert found["reply"]["open"] == {"count": 3, "n": 6, "value": None, "lo": None, "hi": None}  # 6 graded 💬 < 20
    few = metrics.felt(turns([(3, "open", T, F, "done"), (4, "done", F, F, "done")]))
    assert few["false_warning"] == {"count": 3, "n": 7, "value": None, "lo": None, "hi": None}
    assert metrics.felt([])["false_warning"] == {"count": 0, "n": 0, "value": None, "lo": None, "hi": None}
    assert metrics.felt(turns(TABLE), minimum=5)["reply"]["open"]["value"] == 0.5
    assert "3 of 7, no rate" in metrics.felt_lines(few)[0]


def test_the_interval_is_the_deck_s_jeffreys_one_pinned_at_the_ends():
    lo, hi = agreement.intervals((5, 15, 0, 0))["precision"]
    assert metrics.felt_rate(5, 20) == {"count": 5, "n": 20, "value": 0.25, "lo": lo, "hi": hi}
    assert lo < 0.25 < hi
    per = metrics.felt_rate(5, 20, 100)
    assert (per["value"], per["lo"], per["hi"]) == (25.0, round(lo * 100, 1), round(hi * 100, 1))
    none, every = metrics.felt_rate(0, 40, 100), metrics.felt_rate(40, 40, 100)
    assert none["lo"] == 0 == none["value"] < none["hi"] and every["lo"] < every["value"] == 100 == every["hi"]
    wider = metrics.felt_rate(50, 200)
    assert wider["hi"] - wider["lo"] < metrics.felt_rate(5, 20)["hi"] - metrics.felt_rate(5, 20)["lo"]


def test_felt_turns_reads_the_live_sample_of_a_store(lab):
    """End to end on a store: only `live_sample` members count, each by its newest decision's band and the rack's shown."""
    from conftest import SURFACE, decide
    from laya_bridge import review_data, review_log, rows
    from laya_bridge.ledger import Ledger
    from laya_bridge.review_surfaces import REVIEW_SURFACES

    spec = REVIEW_SURFACES[SURFACE]

    def live(prefix: str, n: int, member: bool = True) -> list[str]:
        found, i = [], 0
        while len(found) < n:
            text = f"{prefix} turn end {i}: the agent reported back"
            if review_data.is_live_member(spec, rows.state_key(text)) == member:
                found.append(text)
            i += 1
        return found

    wolf, quiet, outside = live("wolf", 1), live("quiet", 1), live("outside", 1, member=False)
    book = Ledger(lambda: lab.base)
    [wolf_id] = decide(lab, wolf, p=0.9, threshold=0.7, book=book)
    decide(lab, quiet, p=0.1, threshold=0.7, book=book)  # under the done cut (0.3): a ✅
    [outside_id] = decide(lab, outside, p=0.9, threshold=0.7, book=book)
    assert book.react(wolf_id, "shown")[0] == "ok" and book.react(outside_id, "shown")[0] == "ok"
    graded = [(wolf[0], F, None), (quiet[0], T, "you"), (outside[0], F, None)]
    with lab.path("data/agent-open-work-operator.jsonl").open("a", encoding="utf-8") as handle:
        for text, is_open, needs in graded:
            row = review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at="2026-11-19T01:02:03Z",
                surface=SURFACE,
                key=rows.state_key(text),
                stratum="live_sample",
                answer="agree",
                against={"by": "laya", "open": is_open},
                laya=None,
                decision_id=None,
                held_out=True,
                needs=needs,
            )
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    found = metrics.felt(metrics.felt_turns(review_data.snapshot(lab.base, SURFACE)), minimum=1)
    assert (found["sampled"], found["graded"]) == (2, 2)  # the non-member is nobody's sample
    assert found["false_warning"]["count"] == 1 and found["missed_you"]["done"]["count"] == 1
