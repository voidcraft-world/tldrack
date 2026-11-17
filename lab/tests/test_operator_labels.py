"""The operator's REVIEW answers fold into training and sweeps through the bridge's owners, never a copy."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from laya_lab import golden, heldout, open_work_data, promote, score
from laya_lab.open_work_data import build, effective_labels, text_key, training_rows
from laya_bridge import label_budget, review_data, review_log, rows
from laya_bridge.ledger import Ledger
from laya_bridge.review_surfaces import REVIEW_SURFACES
from laya_bridge.wire import clean_request
from tldrack import open_work

from conftest import SUITE, SURFACE, FakeRouter, in_half, synthetic


def fold_of(lab):
    return review_log.read_fold(lab.base, SURFACE)[1]


def test_the_shared_owners_are_imported_not_copied():
    assert text_key is rows.state_key
    assert open_work_data.texts is review_data.texts
    spec = REVIEW_SURFACES[SURFACE]
    assert spec.question == open_work.OPEN_WORK_QUESTION["instructions"]
    assert spec.default_threshold == open_work.OPEN_WORK_AT
    assert spec.default_done_threshold == open_work.DONE_AT
    assert spec.fine_tune == open_work.FINE_TUNE_MODEL


def test_a_operator_flip_beats_a_later_model_row(lab):
    key = lab.unlabelled(synthetic("flip", 1))[0]
    lab.operator(key, "flip", against_open=True)  # Sonnet said open; the operator says done
    model_rows = [{"key": key, "open": True, "by": "sonnet"}, {"key": key, "open": True, "by": "sonnet-later"}]
    assert effective_labels(model_rows, fold_of(lab)) == {key: {"open": False, "by": "operator"}}


def test_a_skip_drops_the_key_and_an_undo_restores_sonnet(lab):
    skipped, undone = lab.unlabelled(synthetic("skip", 2))
    lab.operator(skipped, "skip")
    answer = lab.operator(undone, "flip", against_open=True)
    lab.undo(answer["answer_id"])
    model_rows = [{"key": skipped, "open": True, "by": "sonnet"}, {"key": undone, "open": True, "by": "sonnet"}]
    assert effective_labels(model_rows, fold_of(lab)) == {undone: {"open": True, "by": "sonnet"}}


def test_golden_keys_and_forged_operator_rows_are_ignored(lab):
    key = lab.unlabelled(synthetic("forged", 1))[0]
    lab.operator("golden:phases-c-not-started", "flip", against_open=True, by="golden", stratum="golden")
    forged = [{"key": key, "open": False, "by": "operator"}, {"key": "not-a-key", "open": True, "by": "sonnet"}]
    assert effective_labels(forged, fold_of(lab)) == {}


def test_training_rows_honour_exclude_by_key_or_text():
    texts = synthetic("t", 3)
    unlabelled = [{"key": text_key(t), "hand_off": t} for t in texts]
    labels = [{"key": text_key(t), "open": True} for t in texts]
    kept = training_rows(unlabelled, labels, frozenset({text_key(texts[0])}))
    assert [row["key"] for row in kept] == [text_key(texts[1]), text_key(texts[2])]
    assert training_rows(unlabelled, labels) == training_rows(unlabelled, labels, frozenset())


def test_training_rows_are_one_per_judged_state_and_exclude_by_it():
    full, fitted = long_hand_off("pure")
    full_key, short_key = text_key(full), text_key(fitted)
    unlabelled = [{"key": full_key, "hand_off": full}, {"key": short_key, "hand_off": fitted}]
    sonnet, operator = {"key": full_key, "open": True, "by": "sonnet"}, {"key": short_key, "open": False, "by": "operator"}
    for order in ([sonnet, operator], [operator, sonnet]):
        assert [(row["key"], row["expected"]["q"]) for row in training_rows(unlabelled, order)] == [(short_key, False)]
    later = {"key": short_key, "open": False, "by": "sonnet"}
    assert [row["key"] for row in training_rows(unlabelled, [sonnet, later])] == [short_key], "of one rank, the later"
    assert training_rows(unlabelled, [sonnet], frozenset({short_key})) == [], "the export key's judged state is excluded"
    assert open_work_data.held_out([full_key, "0" * 16], {full_key: full}) == {full_key, short_key, "0" * 16}


def record(lab, text, p=0.9):
    book = Ledger(lambda: lab.base, clock=lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))
    cleaned = clean_request({"state": text, "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
    return book.record_decision(
        {"surface": SURFACE, "threshold": 0.7, "question_version": 1},
        cleaned,
        {"answers": {"q": {"noul": p}}},
        "english",
        None,
        None,
        5,
    )


def test_build_joins_ledger_texts_drops_textless_keys_and_holds_back_golden(lab, public_golden, capsys):
    exported, skipped = synthetic("exported", 2)
    [ledger_only] = in_half(label_budget.TRAIN, "only the ledger kept", 1)  # an operator answer on it trains
    keys = lab.unlabelled([exported, skipped, "Phase C is not started."])  # the last is a public golden case
    assert record(lab, ledger_only) is not None
    ledger_key = text_key(ledger_only)
    lab.labels({keys[0]: True, keys[1]: False, keys[2]: True, "0" * 16: True})  # the last has no text anywhere
    lab.operator(ledger_key, "flip", against_open=True, by="english", stratum="flagged")
    lab.operator(keys[1], "skip")

    assert build(lab.base) == 0
    built = [json.loads(line) for line in (lab.base / "data" / "agent_open_work.jsonl").read_text().splitlines()]
    assert {row["key"]: row["expected"]["q"] for row in built} == {keys[0]: True, ledger_key: False}
    assert all(row["questions"]["q"] is not None and row["state"] in (exported, ledger_only) for row in built)
    out = capsys.readouterr().out
    assert "2 rows (1 open / 1 done; 1 operator, 1 skipped, 1 held back for golden, 0 held in reserve)" in out


def built_keys(lab):
    assert build(lab.base) == 0
    return {json.loads(line)["key"] for line in (lab.base / "data" / "agent_open_work.jsonl").read_text().splitlines()}


def test_build_holds_the_reserve_out_whole_and_trains_the_train_half_on_the_operators_answer(lab, public_golden, capsys):
    """The label budget (`laya_bridge.label_budget`): a reserve key never trains, not even on Sonnet's label."""
    reserve, train = in_half(label_budget.RESERVE, "r", 3), in_half(label_budget.TRAIN, "t", 2)
    keys = lab.unlabelled(reserve + train)
    lab.labels(dict.fromkeys(keys, True))  # Sonnet labelled every one
    lab.operator(keys[0], "flip", against_open=True)  # reserve, the operator's answer
    lab.operator(keys[1], "agree", against_open=True)  # reserve, agreeing with Sonnet: still held
    lab.operator(keys[3], "flip", against_open=True)  # train: the operator's DONE wins
    # keys[2] is drawn RESERVE but the operator never graded it: only an operator label is budgeted.
    assert built_keys(lab) == {keys[2], keys[3], keys[4]}
    assert "1 operator, 0 skipped, 0 held back for golden, 2 held in reserve)" in capsys.readouterr().out
    built = [json.loads(line) for line in (lab.base / "data" / "agent_open_work.jsonl").read_text().splitlines()]
    assert {row["key"]: row["expected"]["q"] for row in built}[keys[3]] is False

    answer = lab.operator(keys[2], "agree", against_open=True)
    assert built_keys(lab) == {keys[3], keys[4]}, "graded now, so held from the next build on"
    lab.undo(answer["answer_id"])
    assert keys[2] in built_keys(lab), "an undone answer is no label: Sonnet's stands again"


def test_a_reserve_key_some_checkpoint_already_trained_on_is_spent_and_trains(lab, public_golden):
    reserve = in_half(label_budget.RESERVE, "r", 2)
    keys = lab.unlabelled(reserve)
    lab.labels(dict.fromkeys(keys, True))
    lab.checkpoint(train_texts=[reserve[0]])  # on disk, never registered: the record still counts
    for key in keys:
        lab.operator(key, "flip", against_open=True)
    assert built_keys(lab) == {keys[0]}


def test_the_trainers_door_holds_back_a_reserve_row_a_stale_dataset_still_carries(lab, public_golden):
    """`train` reads whatever dataset is on disk: one built before a grade must not spend the reserve."""
    from laya_lab.train import withhold_reserve

    reserve, train = in_half(label_budget.RESERVE, "r", 2), in_half(label_budget.TRAIN, "t", 2)
    keys = lab.unlabelled(reserve + train)
    lab.labels(dict.fromkeys(keys, True))
    assert built_keys(lab) == set(keys)  # built before any grade
    stale = [json.loads(line) for line in (lab.base / "data" / "agent_open_work.jsonl").read_text().splitlines()]
    lab.operator(keys[0], "agree", against_open=True)
    lab.operator(keys[2], "agree", against_open=True)
    kept, held = withhold_reserve(SURFACE, lab.base, stale)
    assert held == 1 and {row["key"] for row in kept} == set(keys[1:])
    assert withhold_reserve("coordinator", lab.base, stale) == (stale, 0), "a surface the operator does not review has no budget"

    elsewhere = lab.path("elsewhere.jsonl")
    elsewhere.write_text("")
    operator_log = lab.path("data/agent-open-work-operator.jsonl")
    operator_log.unlink()
    operator_log.symlink_to(elsewhere)
    with pytest.raises(SystemExit, match=r"agent-open-work-operator\.jsonl is refused"):
        withhold_reserve(SURFACE, lab.base, stale)  # read as "no labels", the whole reserve would train


# -- a long hand-off has two keys, and one judged state (`open_work_data.judged_key`) --


def long_hand_off(prefix, full_half=None, fitted_half=None):
    """`(full, fitted)`: a synthetic hand-off past Laya's window and the state the rack judges for it, with each
    key drawn into the half asked (None: either)."""
    i = 0
    while True:
        full = f"{prefix} {i}: " + "the migration is half applied and the backfill has not started. " * 40
        fitted = open_work.decide_state(full)
        assert fitted != full and open_work.decide_state(fitted) == fitted
        drawn = (label_budget.drawn(text_key(full)), label_budget.drawn(text_key(fitted)))
        if all(want in (None, got) for want, got in zip((full_half, fitted_half), drawn)):
            return full, fitted
        i += 1


def built_rows(lab):
    assert build(lab.base) == 0
    return [json.loads(line) for line in (lab.base / "data" / "agent_open_work.jsonl").read_text().splitlines()]


def retrain(lab, built, name="voidcraft-agent-open-work-20261110-0300"):
    """What `train.run` does with a built set: the door, then the training record of a registered checkpoint."""
    from laya_lab.registry import register
    from laya_lab.train import withhold_reserve

    kept, held = withhold_reserve(SURFACE, lab.base, built)
    lab.write_jsonl(f"checkpoints/{name}.data/train.jsonl", kept)
    register(lab.base, SURFACE, lab.checkpoint(name, train_texts=None), {"eval_items": 1})
    return kept, held


def test_a_long_hand_off_graded_by_its_ledger_key_is_held_out_under_its_export_key_too(lab, public_golden, capsys):
    """Sonnet labelled the export key; the operator graded the state the rack judged, drawn RESERVE."""
    full, fitted = long_hand_off("rack", fitted_half=label_budget.RESERVE)
    [full_key] = lab.unlabelled([full, *synthetic("short", 1)])[:1]
    lab.labels({full_key: True})
    assert record(lab, fitted) is not None  # the ledger keeps the fitted state, under its own key
    short_key = text_key(fitted)
    assert short_key != full_key and review_data.texts(lab.base, SURFACE)[short_key] == fitted
    lab.operator(short_key, "flip", against_open=True, stratum="rack")
    reserve = label_budget.read(lab.base, SURFACE).reserve
    assert reserve == {short_key}

    built = built_rows(lab)
    assert "1 held in reserve" in capsys.readouterr().out
    assert [row for row in built if text_key(row["state"]) in reserve] == [], "Sonnet's label trained the reserve's state"
    assert full_key not in {row["key"] for row in built}
    kept, held = retrain(lab, built)
    assert held == 0 and kept == built, "the door agrees with a fresh build"
    assert label_budget.read(lab.base, SURFACE).reserve == {short_key}, "still untrained, so still the reserve's"


def test_a_long_hand_off_graded_at_both_doors_trains_through_neither_key_when_one_is_the_reserves(lab, public_golden):
    """The deck graded the export key (RESERVE), the rack the ledger key (TRAIN): one judged state, held."""
    full, fitted = long_hand_off("both", full_half=label_budget.RESERVE, fitted_half=label_budget.TRAIN)
    [train_text] = in_half(label_budget.TRAIN, "t", 1)
    full_key, train_key = lab.unlabelled([full, train_text])
    lab.labels({full_key: True, train_key: True})
    assert record(lab, fitted) is not None
    short_key = text_key(fitted)
    lab.operator(full_key, "agree", against_open=True)
    lab.operator(short_key, "agree", against_open=True, stratum="rack")
    lab.operator(train_key, "flip", against_open=True)
    assert label_budget.read(lab.base, SURFACE).reserve == {full_key}

    built = built_rows(lab)
    assert {row["key"] for row in built} == {train_key}
    stale = [*built, {"key": short_key, "state": fitted, "questions": {}, "expected": {"q": True}}]
    assert retrain(lab, stale) == (built, 1), "a set built before the fix: the door holds the other key's row"
    trained, _without = rows.trained_keys(lab.base, "voidcraft-agent-open-work")
    assert trained == {train_key} and open_work_data.judged_key(full) not in trained
    assert label_budget.read(lab.base, SURFACE).reserve == {full_key}
    ready = review_data.sweep_readiness(lab.base, SURFACE)
    assert ready["reserve"]["rows"] == 1 and ready["rows"] == 2, "the reserve row is clean in fact, not only by its key"


def test_one_judged_state_is_one_row_and_the_operators_label_on_it_wins(lab, public_golden):
    """A TRAIN-half grade on the ledger key beats Sonnet's label on the export key: same state, one answer."""
    full, fitted = long_hand_off("wins", fitted_half=label_budget.TRAIN)
    [full_key] = lab.unlabelled([full])
    lab.labels({full_key: True})
    assert record(lab, fitted) is not None
    short_key = text_key(fitted)
    lab.operator(short_key, "flip", against_open=True, stratum="rack")  # the operator says DONE
    assert [(row["key"], row["state"], row["expected"]["q"]) for row in built_rows(lab)] == [(short_key, fitted, False)]


@pytest.mark.parametrize("promoted", ["export", "ledger"])
def test_a_golden_case_holds_its_judged_state_out_under_the_other_key(lab, public_golden, capsys, promoted):
    full, fitted = long_hand_off("golden", full_half=label_budget.TRAIN, fitted_half=label_budget.TRAIN)
    [full_key] = lab.unlabelled([full])
    lab.labels({full_key: True})
    assert record(lab, fitted) is not None
    short_key = text_key(fitted)
    lab.operator(full_key, "flip", against_open=True)
    lab.operator(short_key, "flip", against_open=True, stratum="rack")
    key = full_key if promoted == "export" else short_key
    assert promote.promote(lab.base, SUITE, keys=[key], apply=True) == 0  # which rebuilds
    assert (lab.base / "data" / "agent_open_work.jsonl").read_text() == "", "the other key's row is the golden case's state"
    assert "0 rows (0 open / 0 done; 0 operator, 0 skipped, 2 held back for golden, 0 held in reserve)" in capsys.readouterr().out


# -- a source the bridge's readers refuse stops every command, by name ---------------


def commands(lab):
    router = FakeRouter(lambda state: 0.5)
    return {
        "build": lambda: build(lab.base),
        "sweep": lambda: heldout.run_sweep(SUITE, lab.base, labels="merged", router=router),
        "promote": lambda: promote.promote(lab.base, SUITE, flips=True, apply=True),
        "golden": lambda: golden.run(SUITE, lab.base, router=router),
        "score": lambda: score.run(lab.base, router=router),
    }


def trained_set(lab):
    """A training set from an earlier build, which a refused source must never overwrite."""
    keys = lab.unlabelled(synthetic("kept", 2))
    lab.labels({keys[0]: True, keys[1]: False})
    assert build(lab.base) == 0
    return (lab.base / "data" / "agent_open_work.jsonl").read_bytes()


@pytest.mark.parametrize("command", ["build", "sweep", "promote", "golden"])
def test_a_refused_operator_log_stops_every_command_that_reads_it(lab, public_golden, tmp_path, command):
    before = trained_set(lab)
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("")
    lab.path("data/agent-open-work-operator.jsonl").symlink_to(elsewhere)
    with pytest.raises(SystemExit, match=r"agent-open-work-operator\.jsonl is refused"):
        commands(lab)[command]()
    assert (lab.base / "data" / "agent_open_work.jsonl").read_bytes() == before
    assert not list(lab.base.glob("evals/*.json")), "refused before any report was written"


@pytest.mark.parametrize("cap,part", [("UNLABELLED_CAP_BYTES", "unlabelled"), ("LABELS_CAP_BYTES", "labels")])
@pytest.mark.parametrize("command", ["build", "sweep", "promote", "score"])
def test_a_source_past_the_readers_cap_is_refused_never_read_as_empty(lab, public_golden, monkeypatch, cap, part, command):
    before = trained_set(lab)
    monkeypatch.setattr(review_data, cap, 16)
    with pytest.raises(SystemExit, match=rf"agent-open-work-{part}\.jsonl is too_large"):
        commands(lab)[command]()
    assert (lab.base / "data" / "agent_open_work.jsonl").read_bytes() == before, "never rebuilt from part of the data"


def test_model_labels_read_through_the_decks_reader(lab):
    keys = lab.unlabelled(synthetic("m", 2))
    lab.labels({keys[0]: True, keys[1]: False})
    with lab.path("data/agent-open-work-labels.jsonl").open("a") as handle:
        handle.write('{"key": "' + keys[1] + '", "open": tr')  # a label() append cut short
    assert {row["key"]: row["open"] for row in open_work_data.model_labels(lab.base)} == {keys[0]: True, keys[1]: False}
    assert build(lab.base) == 0
