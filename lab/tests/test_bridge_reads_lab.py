"""Decision 17's round trip: every file laya-lab writes, written by laya-lab's REAL writers, read back
by the plugin's readers (`laya_bridge.ops.ops_state`, `laya_bridge.review_data`). Synthetic text only."""

from __future__ import annotations

import json

from laya_lab import cli, golden, heldout, promote, score
from laya_lab.registry import register
from laya_lab.train import compact_report
from laya_bridge import label_budget, lab_files, ops, paths, review_data, rows

from conftest import CHECKPOINT, FINE_TUNE, SURFACE, SUITE, FakeRouter, in_half, synthetic

METRICS = {"accuracy": 0.8, "loss": 0.4, "ece": 0.05, "brier": 0.15}


def outside_sample(prefix: str) -> str:
    """A synthetic text whose key lands outside the deck's `sample` stratum (so it can be `disagree`)."""
    i = 0
    while rows.bucket(rows.state_key(f"{prefix} {i}"), "review-sample") < 50:
        i += 1
    return f"{prefix} {i}"


def p_of(state: str) -> float:
    if "Phase C" in state or "-open " in state:
        return 0.8
    if "disagree" in state:
        return 0.95
    return 0.2


def test_every_lab_artifact_reads_back_through_the_plugin(lab, public_golden):
    agreed = synthetic("f-open", 20) + synthetic("f-done", 20)
    [flip] = in_half(label_budget.TRAIN, "Sonnet called it open and the operator called it done", 1)  # TRAIN: promotable
    disagree = outside_sample("a synthetic disagree text Sonnet called done")
    keys = lab.unlabelled([*agreed, flip, disagree])
    sonnet = {key: i < 20 for i, key in enumerate(keys[:40])}
    lab.labels({**sonnet, keys[40]: True, keys[41]: False})
    for key, is_open in sonnet.items():
        lab.operator(key, "agree", against_open=is_open)
    lab.operator(keys[40], "flip", against_open=True)

    trained_text = "a synthetic text the fine-tune trained on"
    folder = lab.checkpoint(train_texts=[trained_text])
    trained = lab_files.trained_entry(2, 300, 70, "55cf4c4", "2026-01-02T03:04:00Z")
    register(lab.base, SURFACE, folder, compact_report({"eval_items": 70, "before": METRICS, "after": METRICS}), trained=trained)
    router = FakeRouter(p_of)

    assert golden.run(SUITE, lab.base, router=router) == 0
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=router) == 0
    assert score.run(lab.base, router=router) == 0
    assert review_data.promotion_candidates(lab.base, SUITE) == [keys[40]]
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0

    state = ops.ops_state(lab.base, None, [], lab_ok=True)
    served = state["served"][0]
    assert (served["name"], served["checkpoint"]) == (FINE_TUNE, CHECKPOINT)
    on_disk = json.loads((lab.base / paths.MODELS_FILENAME).read_text())
    cut = on_disk["versions"][CHECKPOINT]["cut"]  # v2: on its version
    assert on_disk["thresholds"][SURFACE] == cut, "and in the v1-shaped serving view a pre-v2 bridge reads"
    assert served["threshold"] == {"at": cut["at"], "source": "swept", "swept_at": cut["swept_at"], "done_at": cut["done_at"]}
    assert served["eval"]["items"] == 70
    assert served["golden"]["cases"] == 2 and served["golden"]["kappa"] == 1.0 and served["golden"]["at"].endswith("Z")
    assert served["scores"]["current"] is True and served["scores"]["rows"] == 42

    sweep = state["sweeps"][0]
    assert sweep["labels"] == "operator" and sweep["rows"] == 41 and sweep["best"] == cut["at"]
    assert sweep["live"] == cut["at"] and sweep["source"] == "swept" and sweep["kappa"] == cut["kappa"]

    balance = state["golden"][0]
    assert balance["public"] == {"cases": 2, "open": 1, "done": 1}
    assert balance["private"] == {"cases": 1, "open": 0, "done": 1}
    assert balance["candidates"] == 0 and balance["reserved"] == 0

    assert paths.registry_view(lab.base)["models"][FINE_TUNE]["trained"] == trained
    snap = review_data.snapshot(lab.base, SURFACE)
    assert snap.items.items[keys[41]].stratum == "disagree" and keys[41] in snap.remaining["disagree"]
    assert snap.items.items[keys[41]].laya["source"] == "scores"
    ready = state["review"][0]["sweep_ready"]
    assert ready["ready"] is True and ready["rows"] == 40, "the promoted flip is golden now, so no longer a sweep row"

    # `laya-lab status` reads every block of the same state: a renamed ops_state key fails here, not on the operator's tree.
    lines = [line.strip() for line in cli.status_lines(state)]
    assert f"served   {FINE_TUNE} · {CHECKPOINT} · cut {cut['at']} swept {cut['swept_at']}" in lines
    assert any(line.startswith("golden   ") and "2 cases · accuracy 1.0 · κ 1.00" in line for line in lines)
    assert any(line.startswith("scores   ") and line.endswith("42 rows · current") for line in lines)
    assert any(line.startswith("review   ") and "41 answered (40 agree / 1 flip / 0 skip)" in line for line in lines)
    assert 0 < ready["reserve"]["rows"] < 40, "the label budget's half of them"
    assert f"sweep    ready · 40 clean operator labels (20 open / 20 done, {ready['reserve']['rows']} in reserve)" in lines
    assert any(
        line.startswith("heldout  ") and f"41 rows operator · best {cut['at']} · live {cut['at']} swept" in line for line in lines
    )
    assert "suites   agent-open-work: public 1 open/1 done · private 0 open/1 done · 0 to promote" in lines
    registry_row = state["registry"][0]
    assert registry_row["version"] == 2 and registry_row["champion"]["name"] == CHECKPOINT
    assert registry_row["champion"]["cut"]["at"] == cut["at"] and registry_row["challenger"] is None
    assert f"registry {SURFACE} v2 · @champion {CHECKPOINT}" in lines


def serve_with_operator_labels(lab, n_each=20):
    """A registered fine-tune with no trained rows, and `2·n_each` texts the operator answered (agreeing with Sonnet)."""
    register(lab.base, SURFACE, lab.checkpoint(train_texts=[]), {"eval_items": 1})
    keys = lab.unlabelled(synthetic("f-open", n_each) + synthetic("f-done", n_each))
    labels = {key: i < n_each for i, key in enumerate(keys)}
    lab.labels(labels)
    for key, is_open in labels.items():
        lab.operator(key, "agree", against_open=is_open)


def test_the_sweep_headline_kappa_is_kappa_at_the_cut_it_recommends(lab):
    serve_with_operator_labels(lab)
    # Open texts sit at 0.62: the code's 0.7 calls every text done (κ 0); every cut ≤ 0.6 is perfect.
    router = FakeRouter(lambda state: 0.62 if "-open " in state else 0.2)
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=router) == 0
    _path, report = lab_files.newest_report(lab.base, SUITE, FINE_TUNE, "heldout")
    assert report["at_threshold"]["threshold"] == 0.7 and report["at_threshold"]["kappa"] == 0.0
    assert report["recommended"]["at"] == 0.6 and report["kappa"] == 1.0
    on_disk = json.loads((lab.base / paths.MODELS_FILENAME).read_text())
    cut = on_disk["versions"][CHECKPOINT]["cut"]  # v2: on its version
    assert on_disk["thresholds"][SURFACE] == cut, "and in the v1-shaped serving view a pre-v2 bridge reads"
    assert (cut["at"], cut["kappa"]) == (0.6, 1.0)
    sweep = ops.ops_state(lab.base, None, [], lab_ok=True)["sweeps"][0]
    assert (sweep["best"], sweep["live"], sweep["source"], sweep["kappa"]) == (0.6, 0.6, "swept", cut["kappa"])


def test_an_in_sample_sweep_never_replaces_the_held_out_one_on_ops(lab):
    serve_with_operator_labels(lab)
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=FakeRouter(p_of)) == 0
    before = ops.ops_state(lab.base, None, [], lab_ok=True)["sweeps"][0]
    assert before["labels"] == "operator" and before["rows"] == 40

    trained = synthetic("trained-open", 15)
    lab.checkpoint(train_texts=trained)  # the served checkpoint's record now names 15 rows it trained on
    keys = lab.unlabelled([*synthetic("f-open", 20), *synthetic("f-done", 20), *trained])
    lab.labels({key: i < 20 or i >= 40 for i, key in enumerate(keys)})
    assert heldout.run_sweep(SUITE, lab.base, labels="merged", heldout=False, router=FakeRouter(lambda state: 0.9)) == 0

    in_sample = [json.loads(path.read_text()) for path in (lab.base / "evals").glob("heldout-*-merged-*.json")]
    assert [(report["kind"], report["rows"]) for report in in_sample] == [(heldout.IN_SAMPLE, 55)]
    assert ops.ops_state(lab.base, None, [], lab_ok=True)["sweeps"][0] == before


def todays_report(cases, probabilities, model, threshold=0.7):
    """A golden report exactly as `golden.run` wrote it before this build: no kind, no at, no κ."""
    expected = [case["expected"] for case in cases]
    said = [p >= threshold for p in probabilities]
    tp = sum(s and e for s, e in zip(said, expected))
    fp = sum(s and not e for s, e in zip(said, expected))
    fn = sum(e and not s for s, e in zip(said, expected))
    head = {
        "threshold": threshold,
        "accuracy": round((tp + len(cases) - tp - fp - fn) / len(cases), 3),
        "false_warnings": fp,
        "missed_open": fn,
        "tp": tp,
        "tn": len(cases) - tp - fp - fn,
    }
    return {
        "cases": len(cases),
        "at_threshold": head,
        "sweep": [head],
        "brier": 0.05,
        "misses": [],
        "suite": SUITE,
        "model": model,
        "question_version": 1,
        "revisions": {},
        "probabilities": {case["id"]: p for case, p in zip(cases, probabilities)},
    }


def test_a_golden_report_in_todays_shape_still_reads(lab, public_golden):
    folder = lab.checkpoint(train_texts=[])
    register(lab.base, SURFACE, folder, {"eval_items": 1})
    public, _private = golden.load_suites(lab.base, SUITE)
    report = todays_report(public, [0.9, 0.1], FINE_TUNE)
    lab.path("evals/x").parent.mkdir(exist_ok=True)
    (lab.base / "evals" / f"{SUITE}-{FINE_TUNE}-20261010-120000.json").write_text(json.dumps(report, indent=2) + "\n")
    view = ops.ops_state(lab.base, None, [], lab_ok=True)["served"][0]["golden"]
    assert view == {"at": "2026-10-10T12:00:00", "cases": 2, "accuracy": 1.0, "kappa": None, "brier": 0.05}
