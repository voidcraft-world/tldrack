"""The promotion gate (`gate.py`): the paired cost tests, the floor voter, the held-out rows NEITHER checkpoint trained
on, and the golden-report fixtures. The fixtures are INVENTED golden reports: the public suite's own case ids, with
probabilities no model produced. Every text is synthetic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections.abc import Callable

import pytest

from laya_lab import floor as floor_voter
from laya_lab import gate
from laya_lab.compare import untrained_rows, was_trained
from laya_lab.golden import GOLDEN_DIR, SUITES, load_cases
from laya_lab.metrics import SWEEP_FINE, at_cut, at_done_cut, done_costs, paired_test, pick_cut, pick_done_cut, warning_costs
from laya_lab.registry import register, set_threshold
from laya_lab.train import report_verdict
from laya_bridge import lab_files, paths, rows
from tldrack import open_work

from conftest import FINE_TUNE, SUITE, SURFACE, add_provisional_case, synthetic

FIXTURES = Path(__file__).parent / "fixtures" / "promotion-gate"
#: Three invented golden reports on the suite's first 34 cases: a served fine-tune, a candidate that draws more false
#: ⚠️ than it, and the base model.
SERVED = "golden-served.json"
CANDIDATE_REPORT = "golden-candidate.json"
BASE = "golden-base.json"
SUITE_OBJ = SUITES[SUITE]


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def public_cases() -> list[dict[str, Any]]:
    return load_cases(GOLDEN_DIR / f"{SUITE}.jsonl")


# ── the paired test ───────────────────────────────────────────────────────────


def test_row_costs_are_the_cut_rules_own_and_sum_to_the_sets():
    p = [0.95, 0.75, 0.2, 0.5, 0.35, 0.1]
    e = [True, False, True, False, True, False]
    fwc, fdc = open_work.FALSE_WARNING_COST, open_work.FALSE_DONE_COST
    assert sum(warning_costs(p, e, 0.7, fwc)) == pick_cut([at_cut(p, e, 0.7)], fwc)["cost"]
    assert sum(done_costs(p, e, 0.4, fdc)) == pick_done_cut([at_done_cut(p, e, 0.4)], fdc, None)["cost"]
    # a false ⚠️ weighs FALSE_WARNING_COST, a missed open 1; a false ✅ FALSE_DONE_COST, a missed ✅ 1
    assert warning_costs(p, e, 0.7, fwc) == [0, fwc, 1, 0, 1, 0]
    assert done_costs(p, e, 0.4, fdc) == [0, 1, fdc, 1, fdc, 0]


def test_paired_test_is_exact_mcnemar_with_equal_weights():
    # three rows only the candidate gets wrong, none the other way: 1/2³
    assert paired_test([1, 1, 1, 0, 0], [0, 0, 0, 0, 0])["p"] == 0.125
    # 5 worse, 1 better of 6 discordant: P(Binom(6, ½) ≥ 5) = 7/64
    test = paired_test([1] * 5 + [0] + [1] * 4, [0] * 5 + [1] + [1] * 4)
    assert (test["discordant"], test["worse"], test["better"], test["p"]) == (6, 5, 1, round(7 / 64, 6))
    same = paired_test([1, 0, 2], [1, 0, 2])
    assert same["discordant"] == 0 and same["delta"] == 0 and same["p"] == 1.0
    better = paired_test([0, 0], [2, 2])
    assert better["delta"] == -4 and better["p"] == 1.0
    with pytest.raises(ValueError, match="rows"):
        paired_test([1], [1, 0])


def test_paired_test_weighs_the_costlier_error():
    # +2 (a false ⚠️) against two −1s (missed opens): of the 8 sign patterns of ±2 ±1 ±1, five total ≥ 0
    test = paired_test([2, 0, 0], [0, 1, 1])
    assert test["delta"] == 0 and test["p"] == 0.625
    # the same three rows unweighted would be one worse, two better: weights change the answer
    assert paired_test([1, 0, 0], [0, 1, 1])["p"] == 0.875


# ── the golden-report fixtures ────────────────────────────────────────────────


def fixture_evidence(candidate: str, served: str) -> dict[str, Any]:
    return gate.evidence_from_reports(SUITE_OBJ, public_cases(), fixture(candidate), fixture(served))


def test_every_fixture_says_it_is_invented_and_carries_public_case_ids_only():
    public = {case["id"] for case in public_cases()}
    names = sorted(path.name for path in FIXTURES.glob("*.json"))
    assert names == sorted([BASE, CANDIDATE_REPORT, SERVED])
    for name in names:
        report = fixture(name)
        assert report["fixture"].startswith("invented"), name
        assert set(report["probabilities"]) <= public and report["cases"] == len(report["probabilities"]) == 34, name


def test_a_candidate_with_more_false_warnings_on_golden_alone_refuses_only_as_unmeasured():
    """A candidate that draws more false ⚠️ than the served model on the golden cases. On golden alone the cost rule
    PASSES it, so this REFUSE is vacuous: every golden-only pair refuses this way."""
    evidence = fixture_evidence(CANDIDATE_REPORT, SERVED)
    assert len(evidence["golden"]["ids"]) == 34
    # batch-2 is settled (its sample accepted), so no public case is held back as provisional
    assert evidence["golden"]["provisional"] == []
    verdict = gate.promotion_verdict(evidence)
    assert verdict["verdict"] == "REFUSE" and verdict["promote"] is False
    warning, done = verdict["checks"]["warning_cost"], verdict["checks"]["done_cost"]
    # the repo's cost rule, from the fixtures: 6 false ⚠️ + 1 miss vs 2 + 2, and 0 false ✅ + 13 missed vs 1 + 8
    assert (warning["test"]["candidate"], warning["test"]["served"]) == (13, 6)
    assert (done["test"]["candidate"], done["test"]["served"]) == (13, 11)
    # on golden alone that is noise: 4 rows worse and 1 better is p = 1/16, so golden can never refuse it by itself
    assert (warning["test"]["worse"], warning["test"]["better"]) == (4, 1)
    assert warning["p"] == 0.0625 and warning["state"] == gate.PASS
    assert done["p"] >= gate.PAIRED_ALPHA and done["state"] == gate.PASS
    # what refuses it here: no held-out rows neither trained on, so (a) and (d) are unmeasured
    assert verdict["unmeasured"] == ["heldout", "floor"]
    assert verdict["checks"]["private_golden"]["state"] == gate.SKIPPED
    assert "unmeasured" in verdict["why"]


def test_a_fine_tune_against_the_base_passes_every_golden_check_and_waits_for_the_floor():
    """Golden only: nothing failed, and it is still refused until (a) and (d) are measured on held-out rows."""
    verdict = gate.promotion_verdict(fixture_evidence(SERVED, BASE))
    warning, done = verdict["checks"]["warning_cost"], verdict["checks"]["done_cost"]
    assert (warning["test"]["candidate"], warning["test"]["served"]) == (6, 12) and warning["state"] == gate.PASS
    assert (done["test"]["candidate"], done["test"]["served"]) == (11, 16) and done["state"] == gate.PASS
    assert verdict["failed"] == [] and verdict["unmeasured"] == ["heldout", "floor"]
    assert verdict["verdict"] == "REFUSE"


def test_fixture_cuts_are_the_served_ones():
    evidence = fixture_evidence(CANDIDATE_REPORT, SERVED)
    assert evidence["candidate"]["at"] == evidence["served"]["at"] == open_work.OPEN_WORK_AT
    assert evidence["candidate"]["done_at"] == open_work.DONE_AT


# ── the verdict on full evidence ──────────────────────────────────────────────


def evidence_with(
    golden: tuple[list[bool], list[float], list[float]],
    heldout: tuple[list[bool], list[float], list[float]] | None,
    floor_p: list[float] | None = None,
    floor_cut: float = 0.7,
    private: tuple[list[str], list[bool], list[float], list[float]] | None = None,
) -> dict[str, Any]:
    side = {"at": 0.7, "done_at": 0.4}
    evidence: dict[str, Any] = {
        "costs": gate.costs_of(SUITE_OBJ),
        "candidate": {"name": "cand", **side},
        "served": {"name": "served", **side},
        "golden": gate.paired_block([f"g{i}" for i in range(len(golden[0]))], *golden),
        "heldout": gate.paired_block([f"h{i}" for i in range(len(heldout[0]))], *heldout) if heldout else None,
        "floor": {"p": floor_p, "cut": floor_cut, "rows": 60, "open_rows": 30} if floor_p is not None else None,
        "private": gate.paired_block(*private) if private else {"contaminated": []},
        "notes": {},
    }
    return evidence


def split(n_open: int, n_done: int) -> list[bool]:
    return [True] * n_open + [False] * n_done


def test_a_better_candidate_passes_every_check():
    e = split(20, 20)
    served = [0.75] * 20 + [0.3] * 15 + [0.8] * 5  # 5 false ⚠️
    candidate = [0.9] * 20 + [0.1] * 20  # none
    verdict = gate.promotion_verdict(evidence_with((e, candidate, served), (e, candidate, served), floor_p=served))
    assert verdict["verdict"] == "PASS", verdict["why"]
    assert verdict["checks"]["floor"]["state"] == gate.PASS


def test_a_significantly_worse_candidate_is_refused_on_the_cost_rule():
    e = split(30, 30)
    served = [0.9] * 30 + [0.1] * 30
    candidate = [0.9] * 30 + [0.1] * 22 + [0.85] * 8  # 8 new false ⚠️, nothing better
    good_loss = [0.99] * 30 + [0.01] * 30  # (a) alone would let it through
    evidence = evidence_with((e, candidate, served), (e, good_loss, served), floor_p=[0.5] * 60)
    evidence["heldout"]["candidate"] = candidate
    verdict = gate.promotion_verdict(evidence)
    assert verdict["checks"]["warning_cost"]["state"] == gate.FAIL
    assert verdict["checks"]["warning_cost"]["p"] < gate.PAIRED_ALPHA
    assert "warning_cost" in verdict["failed"] and verdict["verdict"] == "REFUSE"


def test_noise_on_a_small_set_never_refuses_by_itself():
    e = split(17, 17)
    served = [0.9] * 17 + [0.1] * 15 + [0.8] * 2  # 2 false ⚠️ of 17
    candidate = [0.9] * 17 + [0.1] * 12 + [0.8] * 5  # 5 of 17: three more, none fewer
    check = gate.check_cost(evidence_with((e, candidate, served), None), "warning")
    assert check["state"] == gate.PASS and check["p"] == 0.125 and "within noise" in check["why"]


def test_held_out_loss_must_improve_and_accuracy_may_fall_only_the_slack():
    e = split(10, 10)
    served = [0.8] * 10 + [0.2] * 10
    assert gate.check_heldout(evidence_with((e, served, served), (e, served, served)))["state"] == gate.FAIL  # no gain
    sharper = [0.95] * 10 + [0.05] * 10
    assert gate.check_heldout(evidence_with((e, sharper, served), (e, sharper, served)))["state"] == gate.PASS
    worse_acc = [0.95] * 9 + [0.45] + [0.05] * 10  # better loss on 19 rows, one more top-1 miss (−0.05)
    check = gate.check_heldout(evidence_with((e, worse_acc, served), (e, worse_acc, served)))
    assert check["state"] == gate.FAIL and "accuracy fell" in check["why"]


def test_the_floor_must_be_beaten_on_held_out_cost():
    e = split(10, 10)
    candidate = [0.9] * 10 + [0.1] * 8 + [0.8] * 2  # cost 4
    served = [0.6] * 10 + [0.1] * 10  # cost 10
    tie = gate.check_floor(evidence_with((e, candidate, served), (e, candidate, served), floor_p=candidate))
    assert tie["state"] == gate.FAIL and "does not beat" in tie["why"]
    weak_floor = [0.6] * 10 + [0.4] * 10
    assert (
        gate.check_floor(evidence_with((e, candidate, served), (e, candidate, served), floor_p=weak_floor))["state"] == gate.PASS
    )
    assert gate.check_floor(evidence_with((e, candidate, served), (e, candidate, served)))["state"] == gate.UNMEASURED


def test_a_new_private_miss_refuses_and_an_empty_private_suite_is_skipped():
    e = split(2, 2)
    p = [0.9, 0.9, 0.1, 0.1]
    private = (["f-1", "f-2"], [True, False], [0.2, 0.1], [0.9, 0.8])  # f-1 newly missed, f-2 fixed
    check = gate.check_private(evidence_with((e, p, p), None, private=private))
    assert check["state"] == gate.FAIL and check["new_misses"] == ["f-1"] and check["fixed"] == ["f-2"]
    assert gate.check_private(evidence_with((e, p, p), None))["state"] == gate.SKIPPED


def test_report_verdict_keeps_todays_rule_for_surfaces_without_a_suite():
    def report(before_loss, after_loss, before_acc=0.80, after_acc=0.80, held_out=True):
        return {
            "is_held_out": held_out,
            "before": {"loss": before_loss, "accuracy": before_acc},
            "after": {"loss": after_loss, "accuracy": after_acc},
        }

    assert report_verdict(report(0.60, 0.40))[0] is True
    assert report_verdict(report(0.60, 0.40, 0.80, 0.795))[0] is True
    assert report_verdict(report(0.60, 0.61))[0] is False
    assert report_verdict(report(0.60, 0.40, 0.80, 0.70))[0] is False
    assert report_verdict(report(0.60, 0.40, held_out=False))[0] is False
    assert report_verdict({})[0] is False
    assert gate.suite_for_surface("agent_open_work") is SUITE_OBJ and gate.suite_for_surface("coordinator") is None


# ── the prospective test ──────────────────────────────────────────────────────


def prospective_block(candidate: list[float], champion: list[float], expected: list[bool]) -> dict[str, Any]:
    cuts = {"at": 0.7, "done_at": 0.4}
    return {"expected": expected, "candidate": candidate, "champion": champion, "candidate_cuts": cuts, "champion_cuts": cuts}


def test_prospective_waits_for_twenty_operator_labels():
    e = split(10, 9)
    result = gate.prospective_check(prospective_block([0.9] * 19, [0.9] * 19, e), gate.costs_of(SUITE_OBJ))
    assert result["state"] == gate.PENDING and result["labels"] == 19 and result["needs"] == 20
    assert gate.prospective_check(None, gate.costs_of(SUITE_OBJ))["state"] == gate.PENDING


def test_holm_holds_the_smaller_p_to_half_alpha_and_steps_down():
    assert gate.holm([0.03, 0.04], 0.05) == [(False, 0.025), (False, 0.05)]  # 0.03 ≥ α/2 ends it
    assert gate.holm([0.01, 0.04], 0.05) == [(True, 0.025), (True, 0.05)]
    assert gate.holm([0.04, 0.01], 0.05) == [(True, 0.05), (True, 0.025)]  # in input order
    assert gate.holm([0.01, 0.06], 0.05) == [(True, 0.025), (False, 0.05)]


def test_prospective_tests_both_cuts_under_holm():
    e = split(10, 10)
    champion = [0.9] * 10 + [0.1] * 10
    same = gate.prospective_check(prospective_block(champion, champion, e), gate.costs_of(SUITE_OBJ))
    assert same["state"] == gate.PASS and [s["cut"] for s in same["steps"]] == ["warning", "done"]
    assert same["correction"] == "holm" and "either cut" in same["why"]
    worse = [0.9] * 10 + [0.85] * 6 + [0.1] * 4  # 6 new false ⚠️ (and 6 missed ✅)
    flagged = gate.prospective_check(prospective_block(worse, champion, e), gate.costs_of(SUITE_OBJ))
    assert flagged["state"] == gate.FAIL and [s["worse"] for s in flagged["steps"]] == [True, True]
    assert all(s["p"] < s["level"] for s in flagged["steps"])


def test_prospective_fails_when_only_the_done_cut_regresses():
    """A candidate exactly as good at ⚠️ that loses the ✅ on every done row: the fixed sequence used to stop at the
    ⚠️ cut and PASS it; under Holm the ✅ cut is tested too."""
    e = split(10, 15)
    champion = [0.9] * 10 + [0.1] * 15
    candidate = [0.9] * 10 + [0.5] * 15  # under the ⚠️ cut, over the ✅ one: 15 missed ✅
    result = gate.prospective_check(prospective_block(candidate, champion, e), gate.costs_of(SUITE_OBJ))
    warning, done = result["steps"]
    assert warning["p"] == 1.0 and warning["worse"] is False
    assert done["p"] == round(1 / 2**15, 6) and done["level"] == gate.PAIRED_ALPHA / 2 and done["worse"] is True
    assert result["state"] == gate.FAIL and "✅ cut" in result["why"]


# ── the floor voter ───────────────────────────────────────────────────────────


def floor_data(n: int = 40) -> tuple[list[str], list[bool]]:
    texts = [f"phase {i} is not started yet and the import ui still needs work" for i in range(n // 2)]
    texts += [f"renamed helper {i}, all tests pass and the docs are updated" for i in range(n // 2)]
    return texts, [True] * (n // 2) + [False] * (n // 2)


def test_the_floor_learns_words_and_picks_its_cut_out_of_fold():
    texts, labels = floor_data()
    fitted = floor_voter.fit(texts, labels, open_work.FALSE_WARNING_COST)
    assert fitted.cut in SWEEP_FINE and fitted.rows == 40 and fitted.open_rows == 20
    p_open, p_done = fitted.predict(["the import ui is not started yet", "all tests pass and the docs are updated"])
    assert p_open > 0.5 > p_done
    again = floor_voter.fit(texts, labels, open_work.FALSE_WARNING_COST)
    assert again.predict(texts) == fitted.predict(texts)  # deterministic
    router = floor_voter.FloorRouter(fitted)
    assert router.predict("not started yet", {"q": {}})["answers"]["q"]["noul"] == fitted.predict(["not started yet"])[0]


def test_the_floor_refuses_what_it_cannot_measure():
    texts, labels = floor_data()
    with pytest.raises(ValueError, match="two-class"):
        floor_voter.fit(texts, [True] * len(texts), 2.0)
    with pytest.raises(ValueError, match="two-class"):
        floor_voter.fit(texts[:6], labels[:6], 2.0)
    with pytest.raises(ValueError, match="text state"):
        floor_voter.state_text({"request": "no hand-off"})


# ── rows neither checkpoint trained on ────────────────────────────────────────


def test_a_long_text_trained_as_its_shortened_state_is_never_held_out(lab):
    """A record written before train rows kept their key holds `state_key(decide_state(text))`, which a long
    hand-off does not share with its label key."""
    long_text = "Finished the first part. " + "x" * (open_work.STATE_MAX_CHARS + 200) + " Phase C is not started yet."
    short_text = synthetic("short", 1)[0]
    fitted = open_work.decide_state(long_text)
    assert fitted != long_text and rows.state_key(fitted) != rows.state_key(long_text)
    keys = lab.unlabelled([long_text, short_text])
    lab.labels({keys[0]: True, keys[1]: False})
    lab.write_jsonl("checkpoints/old.data/train.jsonl", [{"state": fitted, "questions": {}, "expected": {"q": True}}])
    trained = rows.checkpoint_keys(lab.base, "old")
    assert keys[0] not in trained and was_trained(keys[0], fitted, trained)
    held, meta = untrained_rows(lab.base, SUITE, SUITE_OBJ, "merged", trained)
    assert [key for key, _s, _e in held] == [keys[1]] and meta["excluded"]["trained"] == 1


def test_a_train_row_that_keeps_its_key_records_it(lab):
    text = "y" * (open_work.STATE_MAX_CHARS + 50)
    key = rows.state_key(text)
    lab.write_jsonl(
        "checkpoints/new.data/train.jsonl",
        [
            {"key": key, "state": open_work.decide_state(text), "questions": {}, "expected": {"q": False}},
            {"key": "not-a-key", "state": "z", "questions": {}, "expected": {"q": False}},
        ],
    )
    trained = rows.checkpoint_keys(lab.base, "new")
    assert key in trained and "not-a-key" not in trained
    assert rows.checkpoint_keys(lab.base, "missing") is None
    assert rows.checkpoint_keys(lab.base, "../escape") is None


# ── end to end: `laya-lab gate` with a fake router ────────────────────────────


class ModelRouter:
    """`laya.Router`-shaped: each model name answers by its own function of the state."""

    def __init__(self, by_model: dict[str, Callable[[Any], float]]) -> None:
        self.by_model = by_model
        self.loaded_revisions: dict[str, str | None] = {}

    def predict(self, state: Any, questions: dict, model: str | None = None) -> dict:
        return {"answers": {name: {"noul": self.by_model[model or "english"](state)} for name in questions}}


CANDIDATE = f"{FINE_TUNE}-20261101-0900"


def build_lab(lab, *, n_held: int = 12) -> tuple[list[str], list[str]]:
    """A served english, a candidate trained on 30 synthetic rows, and 2·n_held labelled rows it never saw."""
    train_open, train_done = synthetic("train still open", 15), synthetic("train all done", 15)
    lab.write_jsonl(
        f"checkpoints/{CANDIDATE}.data/train.jsonl",
        [
            {"key": rows.state_key(t), "state": t, "questions": {}, "expected": {"q": i < 15}}
            for i, t in enumerate(train_open + train_done)
        ],
    )
    folder = lab.base / "checkpoints" / CANDIDATE
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.safetensors").write_bytes(b"synthetic")
    held_open, held_done = synthetic("held still open", n_held), synthetic("held all done", n_held)
    keys = lab.unlabelled(train_open + train_done + held_open + held_done)
    lab.labels({key: ("open" in text) for key, text in zip(keys, train_open + train_done + held_open + held_done)})
    return held_open, held_done


def sharp(state: Any) -> float:
    return 0.95 if "open" in str(state).lower() or "not started" in str(state).lower() else 0.05


def blunt(state: Any) -> float:
    return 0.65 if "open" in str(state).lower() or "not started" in str(state).lower() else 0.35


def test_gate_judges_the_newest_unserved_checkpoint_and_writes_no_text(lab, public_golden, capsys):
    held_open, held_done = build_lab(lab)
    router = ModelRouter({CANDIDATE: sharp, "english": blunt})
    assert gate.run(SUITE, lab.base, router=router, device="cpu") == 0
    [path] = list((lab.base / paths.EVALS_DIRNAME).glob("gate-*.json"))
    raw = path.read_text()
    report = json.loads(raw)
    assert report["kind"] == "gate" and report["mode"] == "promotion"
    assert report["candidate"]["checkpoint"] == CANDIDATE and report["served"]["name"] == "english"
    assert report["candidate"]["at_source"] == "code"
    held = report["evidence"]["heldout"]
    assert len(held["ids"]) == 24 and held["excluded"]["trained"] == 30  # training rows are never held out
    assert report["evidence"]["floor"]["rows"] == 30
    assert report["checks"]["heldout"]["state"] == gate.PASS
    assert report["checks"]["warning_cost"]["state"] == gate.PASS
    assert report["checks"]["floor"]["state"] == gate.PASS and report["verdict"] == "PASS"
    assert report["prospective"]["state"] == gate.PENDING
    for text in (*held_open, *held_done, "Phase C is not started.", "All three phases shipped."):
        assert text not in raw
    # none of the rack's readers takes a gate verdict for a golden, held-out or compare report
    assert lab_files.newest_report(lab.base, SUITE, "english", "golden") is None
    assert lab_files.newest_compare(lab.base, SUITE) is None
    assert [p for p, _r in lab_files.gate_reports(lab.base, SUITE)] == [path]
    out = capsys.readouterr().out
    assert "gate agent-open-work" in out and "warning_cost" in out and "report →" in out


def test_gate_refuses_a_candidate_without_a_training_record(lab, public_golden, capsys):
    build_lab(lab)
    lab.checkpoint(f"{FINE_TUNE}-20261102-0900", train_texts=None)  # newer, unrecorded
    assert gate.run(SUITE, lab.base, router=ModelRouter({}), device="cpu") == gate.REFUSED
    assert "no training record" in capsys.readouterr().out
    assert not list((lab.base / paths.EVALS_DIRNAME).glob("gate-*.json"))


def test_gate_refuses_with_nothing_to_judge(lab, public_golden, capsys):
    assert gate.run(SUITE, lab.base, router=ModelRouter({}), device="cpu") == gate.REFUSED
    assert "train one first" in capsys.readouterr().out


def test_prospective_reads_the_champion_from_the_promotion_verdict(lab, public_golden):
    build_lab(lab)
    router = ModelRouter({CANDIDATE: sharp, FINE_TUNE: sharp, "english": blunt})
    gate.judge(SUITE, lab.base, router=router, device="cpu")
    register(
        lab.base,
        SURFACE,
        lab.base / "checkpoints" / CANDIDATE,
        {"eval_items": 1},
        trained=lab_files.trained_entry(2, 30, 6, None, "2026-10-26T00:00:00Z"),
    )
    fresh = synthetic("fresh still open", 10) + synthetic("fresh all done", 10)
    keys = lab.unlabelled(
        [
            *synthetic("train still open", 15),
            *synthetic("train all done", 15),
            *synthetic("held still open", 12),
            *synthetic("held all done", 12),
            *fresh,
        ]
    )
    fresh_keys = keys[-20:]
    for key, text in zip(fresh_keys[:-1], fresh[:-1]):
        lab.operator(key, against_open="open" in text)  # at 2026-10-27, after the registration
    report, _out = gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu")
    assert report["mode"] == "prospective" and report["served"]["name"] == "english"
    assert report["prospective"]["state"] == gate.PENDING and report["prospective"]["labels"] == 19
    lab.operator(fresh_keys[-1], against_open=False)
    report, _out = gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu")
    assert report["prospective"]["state"] == gate.PASS and report["verdict"] == "PASS"
    assert report["since"] == "2026-10-26T00:00:00Z" and len(report["keys"]) == 20


OLD = f"{FINE_TUNE}-20261020-0900"


def registered(lab, folder: str, at: str) -> None:
    register(
        lab.base,
        SURFACE,
        lab.base / "checkpoints" / folder,
        {"eval_items": 1},
        trained=lab_files.trained_entry(2, 30, 6, None, at),
    )


def sweep(lab, checkpoint: str, swept_at: str, at: float = 0.75, done_at: float = 0.35) -> None:
    set_threshold(
        lab.base, SURFACE, lab_files.threshold_entry(at, FINE_TUNE, checkpoint, swept_at, "operator", 40, 0.5, None, done_at)
    )


def test_prospective_scores_the_champion_at_the_cuts_its_verdict_recorded(lab, public_golden):
    """A champion that served at a swept cut is compared at that cut. Registering the candidate dropped the sweep,
    so a registry lookup could only fall back to the code's cuts, which the champion never served at."""
    build_lab(lab)
    lab.checkpoint(OLD, train_texts=synthetic("old trained", 4))
    registered(lab, OLD, "2026-10-20T00:00:00Z")
    sweep(lab, OLD, "2026-10-21T00:00:00Z", at=0.8, done_at=0.3)
    router = ModelRouter({CANDIDATE: sharp, FINE_TUNE: sharp, OLD: sharp})
    promotion, _out = gate.judge(SUITE, lab.base, candidate=CANDIDATE, router=router, device="cpu")
    assert promotion["served"]["checkpoint"] == OLD and promotion["served"]["at_source"] == "swept"
    registered(lab, CANDIDATE, "2026-10-26T00:00:00Z")
    report, _out = gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu")
    served = report["served"]
    assert served["checkpoint"] == OLD
    assert (served["at"], served["at_source"], served["done_at"], served["done_source"]) == (0.8, "swept", 0.3, "swept")
    assert served["cuts_from"] == f"promotion verdict {promotion['run_id']}"
    assert gate.verdict_cuts({"served": {"at": 0.3, "done_at": 0.8}}) is None  # bands that overlap are not a pair


def test_prospective_window_opens_after_the_sweep_that_picked_the_candidates_cut(lab, public_golden):
    """A sweep reads operator labels: the rows before it chose the candidate's cut, so they never score it."""
    build_lab(lab)
    router = ModelRouter({CANDIDATE: sharp, FINE_TUNE: sharp, "english": blunt})
    gate.judge(SUITE, lab.base, router=router, device="cpu")
    registered(lab, CANDIDATE, "2026-10-26T00:00:00Z")
    sweep(lab, CANDIDATE, "2026-10-28T00:00:00Z")
    seen = synthetic("swept still open", 5) + synthetic("swept all done", 5)
    fresh = synthetic("fresh still open", 10) + synthetic("fresh all done", 10)
    keys = lab.unlabelled(
        [
            *synthetic("train still open", 15),
            *synthetic("train all done", 15),
            *synthetic("held still open", 12),
            *synthetic("held all done", 12),
            *seen,
            *fresh,
        ]
    )
    for key, text in zip(keys[-30:-20], seen):
        lab.operator(key, against_open="open" in text, at="2026-10-27T01:02:03Z")  # after registering, before the sweep
    for key, text in zip(keys[-20:], fresh):
        lab.operator(key, against_open="open" in text, at="2026-10-29T01:02:03Z")
    report, _out = gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu")
    assert report["window"] == {
        "since": "2026-10-28T00:00:00Z",
        "registered_at": "2026-10-26T00:00:00Z",
        "swept_at": "2026-10-28T00:00:00Z",
        "opens_at": "sweep",
    }
    cand = report["candidate"]
    assert (cand["at"], cand["at_source"], cand["done_at"], cand["done_source"]) == (0.75, "swept", 0.35, "swept")
    assert report["prospective"]["labels"] == 20 and set(report["keys"]) == set(keys[-20:])
    # a sweep that records no time cannot be told from the test's rows: the code's cuts, from registration
    sweep(lab, CANDIDATE, "not a time")
    report, _out = gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu")
    assert report["candidate"]["at_source"] == "code" and report["window"]["opens_at"] == "registration"
    assert "no sweep time" in report["window"]["note"] and report["prospective"]["labels"] == 30


def test_a_provisional_golden_case_counts_toward_the_gate_only_once_the_operator_agrees_it(lab, public_golden):
    build_lab(lab)
    add_provisional_case(public_golden)
    router = ModelRouter({CANDIDATE: sharp, "english": blunt})
    report, _out = gate.judge(SUITE, lab.base, router=router, device="cpu")
    golden = report["evidence"]["golden"]
    assert golden["ids"] == ["case-open", "case-done"] and golden["provisional"] == ["case-new"]
    lab.operator("golden:case-new", "agree", True, by="golden", stratum="golden")
    report, _out = gate.judge(SUITE, lab.base, router=router, device="cpu")
    golden = report["evidence"]["golden"]
    assert golden["ids"] == ["case-open", "case-done", "case-new"] and golden["provisional"] == []
    lab.operator("golden:case-new", "flip", True, by="golden", stratum="golden")  # a dispute holds it back again
    assert gate.judge(SUITE, lab.base, router=router, device="cpu")[0]["evidence"]["golden"]["provisional"] == ["case-new"]


def test_a_fixture_keeps_numbers_only_and_reproduces_its_verdict(lab, public_golden, tmp_path):
    held_open, held_done = build_lab(lab)
    out = tmp_path / "gate-fixture.json"
    router = ModelRouter({CANDIDATE: sharp, "english": blunt})
    report, _path = gate.judge(SUITE, lab.base, router=router, device="cpu", fixture=out)
    raw = out.read_text(encoding="utf-8")
    fixture = json.loads(raw)
    for secret in (*report["evidence"]["heldout"]["ids"], *held_open, *held_done):
        assert secret not in raw, "a fixture carries no held-out key or text"
    assert fixture["heldout"]["ids"][:2] == ["h0", "h1"] and fixture["from_run"] == report["run_id"]
    verdict = gate.promotion_verdict(fixture)
    assert verdict["verdict"] == fixture["recorded"]["verdict"] == "PASS"
    assert {name: check["state"] for name, check in verdict["checks"].items()} == fixture["recorded"]["states"]
    with pytest.raises(gate.GateRefused, match="promotion verdict"):
        gate.judge(SUITE, lab.base, prospective=True, router=router, device="cpu", fixture=out)


def test_train_registers_only_what_the_gate_passes(monkeypatch, tmp_path):
    from laya_lab import train

    seen: list[str | None] = []

    def judged(promote: bool) -> Callable[..., tuple[dict[str, Any], Path]]:
        def fake(name, base, *, candidate=None, device="mps", **_kw):
            seen.append(candidate)
            return {"promote": promote, "why": "warning_cost: worse"}, tmp_path / "gate-agent-open-work-x.json"

        return fake

    monkeypatch.setattr(train, "judge", judged(False))
    promote, why, tags = train.gate_verdict(SURFACE, tmp_path, tmp_path / CANDIDATE, "cpu")
    assert promote is False and "warning_cost" in why and "gate-agent-open-work-x.json" in why and seen == [CANDIDATE]
    assert tags["gate"] == "FORCED" and tags["gate_report"] == "gate-agent-open-work-x.json"
    monkeypatch.setattr(train, "judge", judged(True))
    promote, _why, tags = train.gate_verdict(SURFACE, tmp_path, tmp_path / CANDIDATE, "cpu")
    assert promote is True and tags["gate"] == "PASS"

    def refused(*_a, **_kw):
        raise gate.GateRefused("tldrack-agent-open-work has no training record")

    monkeypatch.setattr(train, "judge", refused)
    promote, why, _tags = train.gate_verdict(SURFACE, tmp_path, tmp_path / CANDIDATE, "cpu")
    assert promote is False and "could not judge" in why
