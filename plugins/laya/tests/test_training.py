"""The TRAINING page's body, the RETRAIN press, and what the retrain's journal shows of a fit in flight.

Synthetic trees only: every name, row and number here is invented.
"""

from __future__ import annotations

import json

import pytest

from laya_bridge import lab_files, lab_jobs, review, training
from laya_bridge.envelope import Refusal
from laya_bridge.ops import ops_state

from conftest import FINE_TUNE
from test_contract import run_node

SURFACE = "agent_open_work"
SUITE = "agent-open-work"
SERVED = f"{FINE_TUNE}-20261001-0900"
CANDIDATE = f"{FINE_TUNE}-20261101-090000"
OLDER = f"{FINE_TUNE}-20261015-1200"
METRICS = {"accuracy": 0.8, "brier": 0.15, "ece": 0.05, "loss": 0.4}
RUN = "0123456789ab"


def gate_report(candidate: str = CANDIDATE, *, verdict: str = "REFUSE", golden: dict | None = None) -> dict:
    side = {"at": 0.7, "done_at": 0.4}
    rows_ = {
        "ids": ["a", "b", "c"],
        "expected": [True, False, True],
        "candidate": [0.91234567, 0.2, 0.4],
        "served": [0.6, 0.3, 0.8],
    }
    return {
        "kind": "gate",
        "mode": "promotion",
        "suite": SUITE,
        "at": "2026-11-01T10:00:00Z",
        "verdict": verdict,
        "promote": verdict == "PASS",
        "why": "heldout: held-out loss did not improve (0.4788 → 0.5158)",
        "checks": {
            "heldout": {"state": "fail"},
            "floor": {"state": "pass"},
            "Bad Name": {"state": "pass"},
            "odd": {"state": "?"},
        },
        "evidence": {
            "costs": {"false_warning": 2.0},
            "candidate": {"checkpoint": candidate, **side},
            "served": {"checkpoint": SERVED, **side},
            "golden": rows_ if golden is None else golden,
            "heldout": {**rows_, "labels": "merged"},
        },
    }


@pytest.fixture
def store(tree):
    """A served champion, a candidate a gate refused, and an older folder nothing judged."""
    for name in (SERVED, CANDIDATE, OLDER):
        tree.path(f"checkpoints/{name}").mkdir()
    tree.path(f"checkpoints/{CANDIDATE}/{lab_files.TRAIN_REPORT_FILENAME}").write_text(
        json.dumps({"eval_items": 76, "before": {**METRICS, "accuracy": 0.6}, "after": METRICS, "training": {"epochs": 2}})
    )
    tree.path("checkpoints/some-other-model-20261101-0900").mkdir()
    tree.models(
        {
            "version": 2,
            "versions": {
                SERVED: {
                    "path": f"checkpoints/{SERVED}",
                    "surface": SURFACE,
                    "eval": {"before": METRICS, "after": METRICS, "eval_items": 12},
                    "trained": lab_files.trained_entry(2, 900, 90, None, "2026-10-01T09:00:00Z"),
                    "tags": lab_files.gate_tags("PASS"),
                }
            },
            "aliases": {SURFACE: {"champion": SERVED, "challenger": None, "previous": None}},
        }
    )
    tree.path(f"evals/{lab_files.gate_report_name(SUITE, '20261101-100000', RUN)}").write_text(json.dumps(gate_report()))
    return tree


def test_every_snapshot_of_the_surface_is_listed_newest_first(store):
    body = training.training_state(store.base, SURFACE)
    names = [row["name"] for row in body["snapshots"]]
    assert names == [CANDIDATE, OLDER, SERVED], "another model's folder is not this surface's snapshot"
    legacy = "voidcraft-agent-open-work-20261020-1000"  # Legacy (step 6.8): a folder keeps the name it was trained under
    store.path(f"checkpoints/{legacy}").mkdir()
    assert [row["name"] for row in training.training_state(store.base, SURFACE)["snapshots"]] == [
        CANDIDATE,
        legacy,
        OLDER,
        SERVED,
    ]
    candidate, older, served = body["snapshots"]
    assert (served["alias"], served["registered"], served["gate"], served["epochs"], served["train_rows"]) == (
        "champion",
        True,
        "PASS",
        2,
        900,
    )
    assert (candidate["alias"], candidate["registered"], candidate["verdict"]) == (None, False, "REFUSE")
    assert candidate["at"] == "2026-11-01T09:00:00" and served["at"] == "2026-10-01T09:00:00"
    assert candidate["eval"] == {"items": 76, "before": {**METRICS, "accuracy": 0.6}, "after": METRICS}, "read beside the weights"
    assert older["eval"] is None and older["scored"] is False
    assert candidate["scored"] and served["scored"], "the served model has points wherever it was a gate's other side"


def test_a_gate_s_rows_cross_as_a_label_and_two_probabilities(store):
    [row] = training.training_state(store.base, SURFACE)["evidence"]
    assert (row["candidate"], row["served"], row["verdict"], row["promote"]) == (CANDIDATE, SERVED, "REFUSE", False)
    assert row["cuts"] == {"candidate": {"at": 0.7, "done_at": 0.4}, "served": {"at": 0.7, "done_at": 0.4}}
    assert row["false_warning_cost"] == 2.0
    assert row["checks"] == [{"name": "heldout", "state": "fail"}, {"name": "floor", "state": "pass"}]
    assert [block["set"] for block in row["sets"]] == ["golden", "heldout"]
    golden = row["sets"][0]
    assert set(golden) == {"set", "expected", "candidate", "served"}, "no id, no key, no text"
    assert golden["candidate"] == [0.9123, 0.2, 0.4] and golden["expected"] == [True, False, True]


@pytest.mark.parametrize(
    "golden",
    [
        {"expected": [True], "candidate": [0.5, 0.5], "served": [0.5]},
        {"expected": [1], "candidate": [0.5], "served": [0.5]},
        {"expected": [True], "candidate": [1.5], "served": [0.5]},
        {"expected": [], "candidate": [], "served": []},
        "rows",
    ],
)
def test_a_set_that_is_not_whole_is_left_out(store, golden):
    report = gate_report(golden=golden)
    assert [block["set"] for block in training.evidence_view(store.base / "x-20261101-100000.json", report)["sets"]] == [
        "heldout"
    ]


def test_the_newest_gate_of_a_candidate_is_the_one_read(store):
    newer = {**gate_report(verdict="PASS"), "at": "2026-11-02T10:00:00Z"}
    store.path(f"evals/{lab_files.gate_report_name(SUITE, '20261102-100000', RUN)}").write_text(json.dumps(newer))
    other = gate_report(OLDER)
    store.path(f"evals/{lab_files.gate_report_name(SUITE, '20261020-100000', RUN)}").write_text(json.dumps(other))
    rows_ = training.training_state(store.base, SURFACE)["evidence"]
    assert [(row["candidate"], row["verdict"]) for row in rows_] == [(CANDIDATE, "PASS"), (OLDER, "REFUSE")]


def test_the_last_run_says_why_nothing_was_owed(store):
    assert training.training_state(store.base, SURFACE)["last_run"] is None
    report = {
        "kind": "retrain",
        "suite": SUITE,
        "state": "noop",
        "at": "2026-11-03T01:00:00Z",
        "owed": {"reason": "3 of 10 new clean grades"},
    }
    store.path(f"evals/{lab_files.retrain_report_name(SUITE, '20261103-010000')}").write_text(json.dumps(report))
    assert training.training_state(store.base, SURFACE)["last_run"] == {
        "at": "2026-11-03T01:00:00Z",
        "state": "noop",
        "verdict": "NO-OP",
        "reason": "3 of 10 new clean grades",
    }


def test_a_missing_store_answers_empty_and_the_route_reads_its_surface(store, tmp_path):
    empty = training.training_state(tmp_path / "nowhere", SURFACE)
    assert empty["ok"] and empty["snapshots"] == [] and empty["evidence"] == [] and empty["last_run"] is None
    status, body = review.training(store.base, {"surface": [SURFACE]})
    assert status == 200 and body["surface"] == SURFACE and len(body["snapshots"]) == 3
    assert review.training(store.base, {})[1]["surface"] == SURFACE
    assert review.training(store.base, {"surface": ["nope"]})[0] == 400
    assert review.training(store.base, {"limit": ["1"]})[0] == 400


def journal(**train) -> dict:
    steps = {
        name: {"state": "done", "attempts": 1, "started_at": "2026-11-01T01:00:00Z", "seconds": 12.5}
        for name in ("export", "label", "build")
    }
    steps["train"] = {"state": "running", "attempts": 1, "started_at": "2026-11-01T01:05:00Z", "train_rows": 900, **train}
    return {
        "kind": "retrain",
        "v": lab_files.RETRAIN_VERSION,
        "suite": SUITE,
        "surface": SURFACE,
        "id": "20261101-010000-abc123",
        "state": "running",
        "started_at": "2026-11-01T01:00:00Z",
        "updated_at": "2026-11-01T01:20:00Z",
        "closed_at": None,
        "owed": {"reason": "started off schedule · 12 new clean grades (6 train / 6 reserve)", "clean": 48},
        "checkpoint": CANDIDATE,
        "steps": steps,
    }


def test_a_fit_in_flight_shows_how_far_it_is():
    view = lab_files.retrain_view(journal(progress=lab_files.train_progress(2, 2, 30, 120, 0.4321)))
    assert view["progress"] == {"epoch": 2, "epochs": 2, "step": 30, "steps": 120, "done": 0.625, "loss": 0.4321}
    assert view["reason"].startswith("started off schedule") and view["closed_at"] is None
    by_name = {step["name"]: step for step in view["steps"]}
    assert by_name["build"] == {"name": "build", "state": "done", "started_at": "2026-11-01T01:00:00Z", "seconds": 12.5}
    assert by_name["train"]["seconds"] is None and by_name["gate"] == {
        "name": "gate",
        "state": "pending",
        "started_at": None,
        "seconds": None,
    }


@pytest.mark.parametrize(
    "progress,done",
    [
        (lab_files.train_progress(1, 2, 5, None, 0.7), None),  # the trainer has not said how many items yet
        (lab_files.train_progress(1, 2, 500, 120, None), 0.5),  # never past the epoch it is in
        ({"epoch": 3, "epochs": 2, "step": 1}, "absent"),
        ("soon", "absent"),
    ],
)
def test_progress_is_whole_or_absent(progress, done):
    view = lab_files.retrain_view(journal(progress=progress))["progress"]
    assert view is None if done == "absent" else view["done"] == done


def test_a_finished_fit_shows_its_numbers_and_no_progress():
    done = journal(progress=lab_files.train_progress(2, 2, 120, 120, 0.3), eval_items=76, before=METRICS, after=METRICS)
    done["steps"]["train"].update(state="done", seconds=3540.2)
    done["steps"]["gate"] = {"state": "done", "gate": "REFUSED", "promote": False, "why": "heldout: loss did not improve"}
    view = lab_files.retrain_view(done)
    assert view["progress"] is None and view["eval"] == {"items": 76, "before": METRICS, "after": METRICS}
    assert view["gate_why"] == "heldout: loss did not improve"


def test_the_bodies_parse_as_the_schema_says(store, monkeypatch):
    store.path(f"retrain/{SUITE}.json").write_text(json.dumps(journal(progress=lab_files.train_progress(1, 2, 30, 120, 0.5))))
    ops = ops_state(store.base, None, lab_jobs.job_rows(None), lab_ok=True)
    [row] = ops["retrain"]
    assert row["progress"]["done"] == 0.125
    out = run_node({"accept": {"training": [training.training_state(store.base, SURFACE)], "ops": [ops]}, "refuse": {}})
    assert out["failures"] == []


def test_retrain_is_a_press_of_the_gated_retrain_and_never_of_train():
    args = lab_jobs.lab_args("retrain", SURFACE)
    assert args == ("retrain", "agent-open-work", "--now") and not lab_jobs.is_forbidden(args)
    assert lab_jobs.eval_id("retrain", SURFACE) == lab_jobs.RETRAIN_NOW_EVAL != "laya-retrain"
    assert lab_jobs.RETRAIN_NOW_EVAL not in lab_jobs.OCCUPYING_EVALS, "the scheduled retrain is another eval"
    assert lab_jobs.cli_text("retrain", SURFACE) == "cd lab && uv run laya-lab retrain agent-open-work --now"


def test_a_retrain_press_waits_for_the_same_clean_labels_a_sweep_does(tree, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / lab_jobs.LAB_BIN).parent.mkdir(parents=True)
    (repo / lab_jobs.LAB_BIN).write_text("")
    monkeypatch.setattr(lab_jobs, "tmux_available", lambda: True)
    with pytest.raises(Refusal) as refused:
        lab_jobs.gate("retrain", SURFACE, tree.base, repo=repo)
    assert refused.value.code == "too_few_labels"


def test_a_scheduled_retrain_reads_as_the_retrain_row_at_work():
    panes = lab_jobs.parse_lab_panes("\x1f".join(("evals-1", "0", "", "", "", "laya-retrain")))
    [row] = [row for row in lab_jobs.job_rows(panes) if row["verb"] == "retrain"]
    assert row["state"] == "working" and row["session"] == "evals-1"
