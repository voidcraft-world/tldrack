"""The held-out sweep: its rows, its label sources, its refusals, and the bridge's gates agreeing with them."""

from __future__ import annotations

import json

import pytest

from laya_lab import heldout, promote, registry
from laya_lab.registry import clear_threshold, register, served_done_threshold, served_threshold, set_threshold, unregister
from laya_bridge import lab_files, paths, review_data

from conftest import CHECKPOINT, FINE_TUNE, SURFACE, SUITE, FakeRouter, synthetic


def serve(lab, train_texts=(), name=CHECKPOINT):
    folder = lab.checkpoint(name, train_texts=None if train_texts is None else list(train_texts))
    register(lab.base, SURFACE, folder, {"eval_items": 1})
    return folder


def operator_labels(lab, n_open, n_done, prefix="f"):
    """`n_open + n_done` exported texts the operator answered (agreeing with Sonnet). Returns {key: open}."""
    texts = synthetic(f"{prefix}-open", n_open) + synthetic(f"{prefix}-done", n_done)
    keys = lab.unlabelled(texts)
    labels = {key: i < n_open for i, key in enumerate(keys)}
    lab.labels(labels)
    for key, is_open in labels.items():
        lab.operator(key, "agree", against_open=is_open)
    return labels


def scored(labels):
    """A router that says p = 0.8 for open texts and 0.2 for done ones — a perfect, synthetic model."""
    return FakeRouter(lambda state: 0.8 if "-open " in state else 0.2)


def threshold(lab):
    """The champion's own cut, as models.json v2 stores it (on the champion's version)."""
    payload = json.loads((lab.base / paths.MODELS_FILENAME).read_text())
    champion = (payload.get("aliases", {}).get(SURFACE) or {}).get("champion")
    return (payload["versions"].get(champion) or {}).get("cut") if champion else None


# -- rows and labels -------------------------------------------------------------


def test_operator_beats_model_regardless_of_order(lab):
    key = lab.unlabelled(synthetic("x", 1))[0]
    lab.operator(key, "flip", against_open=True)
    lab.labels({key: True})  # written after the operator answered: still loses
    assert heldout.labels_of(lab.base, SURFACE, "merged") == {key: False}
    assert heldout.labels_of(lab.base, SURFACE, "model") == {key: True}
    assert heldout.labels_of(lab.base, SURFACE, "operator") == {key: False}


def test_clean_rows_drop_trained_golden_and_textless(lab, public_golden):
    trained, kept = synthetic("trained", 1)[0], synthetic("kept", 1)[0]
    serve(lab, train_texts=[trained])
    keys = lab.unlabelled([trained, kept, "Phase C is not started."])  # the last is a public golden case
    lab.labels({keys[0]: True, keys[1]: True, keys[2]: True, "f" * 16: False})
    clean = heldout.labelled_rows(lab.base, SUITE, "model")
    assert [key for key, _o, _t in clean.rows] == [keys[1]]
    assert clean.excluded == {"trained": 1, "golden": 1, "no_text": 1}
    assert clean.reason is None
    in_sample = heldout.labelled_rows(lab.base, SUITE, "model", in_sample=True)
    assert sorted(key for key, _o, _t in in_sample.rows) == sorted([keys[0], keys[1]])


def test_a_checkpoint_without_a_training_record_is_refused_with_the_shared_reason(lab, capsys):
    serve(lab, train_texts=None)
    operator_labels(lab, 20, 20)
    clean = heldout.labelled_rows(lab.base, SUITE, "operator")
    assert clean.reason == f"checkpoint {CHECKPOINT} has no training record — retrain it"
    assert heldout.run_sweep(SUITE, lab.base, labels="model", router=scored({})) == heldout.REFUSED
    assert "has no training record" in capsys.readouterr().out
    assert heldout.run_sweep(SUITE, lab.base, labels="model", heldout=False, router=scored({})) == 0  # in-sample still runs


def test_auto_switches_to_operator_at_40_rows_and_10_per_class(lab):
    serve(lab)
    texts = synthetic("a-open", 30) + synthetic("a-done", 10)
    keys = lab.unlabelled(texts)
    for i, key in enumerate(keys[:-1]):
        lab.operator(key, "agree", against_open=i < 30)
    assert review_data.sweep_readiness(lab.base, SURFACE)["rows"] == 39
    assert heldout.resolve_labels("auto", lab.base, SURFACE) == "merged"  # 39 rows, 9 done
    lab.operator(keys[-1], "agree", against_open=False)
    assert review_data.sweep_readiness(lab.base, SURFACE)["ready"] is True
    assert heldout.resolve_labels("auto", lab.base, SURFACE) == "operator"
    assert heldout.resolve_labels("model", lab.base, SURFACE) == "model"


# -- apply, refusals, and the cut it writes -----------------------------------------


def test_apply_writes_the_cut_scoped_to_the_served_checkpoint(lab, capsys):
    serve(lab)
    operator_labels(lab, 20, 20)
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=scored({})) == 0
    entry = threshold(lab)
    assert entry["model"] == FINE_TUNE and entry["checkpoint"] == CHECKPOINT and entry["labels"] == "operator"
    assert entry["rows"] == 40 and entry["kappa"] == 1.0 and 0.2 < entry["at"] <= 0.8
    assert entry["at"] == 0.8, "every cut in (0.2, 0.8] is perfect; ties go to the higher cut"
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.8, "swept")
    path, report = lab_files.newest_report(lab.base, SUITE, FINE_TUNE, "heldout")
    assert report["applied"] == {
        "at": 0.8,
        "done_at": 0.25,
        "previous": {"at": 0.7, "source": "code", "done_at": 0.4, "done_source": "code"},
    }
    assert entry["done_at"] == 0.25, "every ✅ cut in (0.2, 0.8) is perfect; ties go to the LOWER cut"
    assert report["done"]["live"] == {"at": 0.4, "source": "code"} and report["done"]["at_live"]["false_done"] == 0
    assert entry["report"] == path.name
    assert report["misses"] == [] and report["class_counts"] == {"open": 20, "done": 20}
    ready = review_data.sweep_readiness(lab.base, SURFACE)
    assert report["reserve"] == ready["reserve"] and 0 < report["reserve"]["rows"] < 40, "the rows a retrain leaves"
    assert all(set(miss) == {"key", "expected", "p"} for miss in report["misses"])
    assert "synthetic closing message" not in path.read_text(), "a held-out report carries keys, never text"
    assert "applied 0.8" in capsys.readouterr().out
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=scored({})) == 0
    assert "unchanged at 0.8" in capsys.readouterr().out


def test_a_operator_sweep_sets_operator_ece_beside_teacher_ece_and_refits_one_temperature(lab, capsys):
    """On 40 operator labels the report carries `calibration`, and the rack's SWEEP tip reads it."""
    serve(lab)
    labels = operator_labels(lab, 20, 20)
    # Sonnet disagrees with the operator on four open texts: the teacher's labels are not the operator's.
    lab.labels({key: (False if i < 4 else is_open) for i, (key, is_open) in enumerate(labels.items())})
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", router=scored({})) == 0
    path, report = lab_files.newest_report(lab.base, SUITE, FINE_TUNE, "heldout")
    fit = report["calibration"]
    assert fit["rows"] == 40 and fit["ece"] == 0.2, "p 0.8 on open, 0.2 on done: off by 0.2 everywhere"
    # Sonnet calls 4 of the 20 p-0.8 texts done, so its open rate there is 0.8: no gap in that bin, 0.2 in the other
    assert fit["teacher"]["rows"] == 40 and fit["teacher"]["ece"] == 0.1
    assert fit["temperature"] == 0.05 and fit["ece_refit"] < 0.01, "separable: the refit sharpens to the bound"
    assert [b["cuts"] for b in fit["reliability"]] == [[], [0.4], [], [0.7], []]
    assert report["ece"] == 0.2 and report["trust"]["kappa"] == 1.0 and report["trust"]["kappa_ci"]["half_width"] > 0
    assert report["trust"]["precision"]["value"] == 1.0 and report["trust"]["precision"]["n"] == 20
    assert lab_files.heldout_view(path, report)["calibration"] == {
        "rows": 40,
        "ece": 0.2,
        "teacher_rows": 40,
        "teacher_ece": 0.1,
        "temperature": 0.05,
        "ece_refit": fit["ece_refit"],
    }
    out = capsys.readouterr().out
    assert "calibration on 40 operator labels: ECE 0.200 · teacher ECE 0.100" in out and "at the live cut: precision 1.00" in out


@pytest.mark.parametrize("labels,n_each", [("merged", 20), ("operator", 19)])
def test_no_operator_calibration_on_sonnet_s_labels_or_below_forty(lab, labels, n_each):
    serve(lab)
    operator_labels(lab, n_each, n_each)
    assert heldout.run_sweep(SUITE, lab.base, labels=labels, router=scored({})) == 0
    path, report = lab_files.newest_report(lab.base, SUITE, FINE_TUNE, "heldout")
    assert report["rows"] == 2 * n_each and report["labels"] == labels
    assert report["calibration"] is None and report["ece"] == 0.2, "ECE vs the sweep's own labels is always there"
    assert lab_files.heldout_view(path, report)["calibration"] is None


def test_apply_refuses_non_operator_labels_another_model_and_in_sample(lab):
    operator_labels(lab, 20, 20)
    serve(lab)
    for labels in ("model", "merged"):
        assert heldout.run_sweep(SUITE, lab.base, labels=labels, apply=True, router=scored({})) == heldout.REFUSED
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, heldout=False, router=scored({})) == heldout.REFUSED
    assert (
        heldout.run_sweep(SUITE, lab.base, model="english", labels="operator", apply=True, router=scored({})) == heldout.REFUSED
    )
    assert threshold(lab) is None


def test_a_dry_run_on_a_built_in_measures_without_applying(lab, capsys):
    operator_labels(lab, 5, 5)
    assert heldout.run_sweep(SUITE, lab.base, router=scored({})) == 0
    out = capsys.readouterr().out
    assert "english" in out and "dry run" in out and "no fine-tune registered" in out
    assert not (lab.base / paths.MODELS_FILENAME).exists()


# -- the bridge's gates and the CLI's exit 3 can never disagree --------------------


@pytest.mark.parametrize(
    "n_open,n_done,trained", [(20, 20, False), (30, 9, False), (5, 5, False), (20, 20, True), (39, 10, True)]
)
def test_sweep_ready_is_false_exactly_when_apply_exits_3(lab, n_open, n_done, trained):
    operator_labels(lab, n_open, n_done)
    serve(lab, train_texts=synthetic("f-open", 3) if trained else [])  # trained rows never count
    ready = review_data.sweep_readiness(lab.base, SURFACE)["ready"]
    code = heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=scored({}))
    assert code in (0, heldout.REFUSED)
    assert (code == heldout.REFUSED) == (not ready)
    assert (threshold(lab) is not None) == ready


def test_a_built_in_served_is_not_ready_and_refused_alike(lab):
    operator_labels(lab, 20, 20)
    assert review_data.sweep_readiness(lab.base, SURFACE)["ready"] is False
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=scored({})) == heldout.REFUSED


def test_promotion_candidates_are_empty_exactly_when_promote_exits_3(lab, public_golden):
    texts = synthetic("p", 4)
    keys = lab.unlabelled(texts)
    lab.trained_on(texts[3:])  # trained on, so its flip is in the label budget's TRAIN half
    for key in keys[:3]:
        lab.operator(key, "agree", against_open=True)
    assert review_data.promotion_candidates(lab.base, SUITE) == []
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == promote.REFUSED
    lab.operator(keys[3], "flip", against_open=False)
    assert review_data.promotion_candidates(lab.base, SUITE) == [keys[3]]
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    assert review_data.promotion_candidates(lab.base, SUITE) == []
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == promote.REFUSED


# -- the swept cut's lifetime ---------------------------------------------------------


def entry(at=0.62, checkpoint=CHECKPOINT, model=FINE_TUNE):
    return lab_files.threshold_entry(at, model, checkpoint, "2026-10-27T00:00:00Z", "operator", 40, 0.5, None)


def test_a_cut_lives_on_its_checkpoint_and_moves_with_the_alias(lab):
    folder = serve(lab)
    set_threshold(lab.base, SURFACE, entry())
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.62, "swept")
    register(lab.base, SURFACE, folder, {"eval_items": 1})
    assert threshold(lab)["at"] == 0.62, "re-registering the same checkpoint keeps the cut swept for it"
    assert heldout.run_sweep(SUITE, lab.base, reset=True) == 0 and threshold(lab) is None
    assert clear_threshold(lab.base, SURFACE) is False
    set_threshold(lab.base, SURFACE, entry())
    assert unregister(lab.base, SURFACE) is True and served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.7, "code")
    register(lab.base, SURFACE, folder, {"eval_items": 1})
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.62, "swept"), "serving it again restores its own cut"

    newer = serve(lab, name="tldrack-agent-open-work-20261101-0900")
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.7, "code"), "a new checkpoint starts at the code's cut"
    assert registry.rollback(lab.base, SURFACE, "test") == (CHECKPOINT, newer.name)
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.62, "swept"), "a rollback restores the old cut, never 0.7"


def test_served_threshold_is_swept_only_for_the_model_and_checkpoint_it_was_swept_on(lab):
    serve(lab)
    with pytest.raises(SystemExit, match="not a registered version"):
        set_threshold(lab.base, SURFACE, entry(checkpoint="tldrack-agent-open-work-20261001-0900"))
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.7, "code")
    set_threshold(lab.base, SURFACE, entry())
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.62, "swept")
    assert served_threshold(lab.base, SURFACE, "english") == (0.7, "code")
    set_threshold(lab.base, SURFACE, entry(at=1.0))
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.7, "code")  # a cut must lie inside (0, 1)


def test_apply_on_the_challenger_keeps_the_served_cut_and_scopes_rows_to_the_challenger(lab):
    serve(lab)
    operator_labels(lab, 20, 20)
    set_threshold(lab.base, SURFACE, entry())
    rival = lab.checkpoint("tldrack-agent-open-work-20261101-0900", train_texts=None)
    register(lab.base, SURFACE, rival, {"eval_items": 1}, as_="challenger")
    assert (
        heldout.run_sweep(SUITE, lab.base, model="challenger", labels="operator", apply=True, router=scored({}))
        == heldout.REFUSED
    ), "its own training record is missing"
    lab.write_jsonl(f"checkpoints/{rival.name}.data/train.jsonl", [])
    assert heldout.run_sweep(SUITE, lab.base, model="challenger", labels="operator", apply=True, router=scored({})) == 0
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == (0.62, "swept"), "the served cut never moved"
    assert served_threshold(lab.base, SURFACE, rival.name)[1] == "swept"
    registry.promote_challenger(lab.base, SURFACE, "test")
    assert served_threshold(lab.base, SURFACE, FINE_TUNE) == served_threshold(lab.base, SURFACE, rival.name), (
        "a promotion serves the challenger at the cut swept for it"
    )


def test_the_done_cut_is_swept_under_the_open_cut_and_read_back_scoped(lab, capsys):
    serve(lab)
    operator_labels(lab, 20, 20)
    # Open texts at 0.5, done ones at 0.1: the ⚠️ cut lands at 0.5, so the ✅ cut must stay under it.
    router = FakeRouter(lambda state: 0.5 if "-open " in state else 0.1)
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=router) == 0
    entry = threshold(lab)
    assert entry["at"] == 0.5 and entry["done_at"] == 0.15
    assert served_done_threshold(lab.base, SURFACE, FINE_TUNE) == (0.15, "swept")
    assert served_done_threshold(lab.base, SURFACE, "english") == (0.4, "code")
    out = capsys.readouterr().out
    assert "✅ recommended 0.15" in out and "✅ 0.15 (was 0.4)" in out
