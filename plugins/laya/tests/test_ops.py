"""AGENT RACK › LAYA's state, read from laya-lab's real outputs (synthetic trees only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from laya_bridge import lab_files, lab_jobs, paths, rows
from laya_bridge.ledger import Ledger
from laya_bridge.ops import ops_state
from laya_bridge.plugin import LayaPlugin
from laya_bridge.wire import clean_request

FINE_TUNE = "voidcraft-agent-open-work"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
SUITE = "agent-open-work"


def golden_report(model, **extra):
    return {
        "cases": 32,
        "at_threshold": {"threshold": 0.7, "accuracy": 0.75},
        "brier": 0.21,
        "suite": SUITE,
        "model": model,
        "question_version": 1,
        "misses": [],
        "probabilities": {},
        **extra,
    }


def full_tree(tree):
    checkpoint = tree.register(
        train_texts=["trained"],
        thresholds={
            "agent_open_work": lab_files.threshold_entry(
                0.62, FINE_TUNE, "voidcraft-agent-open-work-20260102-0304", "2026-10-27T01:00:00Z", "operator", 48, 0.55, None
            )
        },
        extra_models={"example-news-gate": {"path": "checkpoints/example-news-gate-1", "surface": "news_gate"}},
    )
    tree.path("checkpoints/example-news-gate-1").mkdir()
    tree.path(f"evals/agent-open-work-{FINE_TUNE}-20260102-030405.json").write_text(json.dumps(golden_report(FINE_TUNE)))
    tree.path(f"evals/agent-open-work-{FINE_TUNE}-20261027-010000.json").write_text(
        json.dumps(
            golden_report(
                FINE_TUNE, **lab_files.golden_report_fields("2026-10-27T01:00:00Z", "run-1", 0.5, {"name": checkpoint.name})
            )
        )
    )
    tree.path(f"evals/{lab_files.heldout_report_name(SUITE, FINE_TUNE, 'operator', '20261027-010500')}").write_text(
        json.dumps(lab_files.heldout_report_head("2026-10-27T01:05:00Z", SUITE, FINE_TUNE, "operator", 48, 0.62, 0.55))
    )
    key = rows.state_key("scored text")
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [lab_files.scores_row(key, 0.4, FINE_TUNE, checkpoint.name, 1, 0.62, "2026-10-27T02:00:00Z")],
    )
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    question = {"type": "noul", "instructions": "Open?"}
    decision_id = book.record_decision(
        {"surface": "agent_open_work", "threshold": 0.62},
        clean_request({"state": "a flag", "questions": {"q": question}}, frozenset({"english"})),
        {"answers": {"q": {"noul": 0.9}}},
        FINE_TUNE,
        None,
        str(checkpoint),
        4,
    )
    book.react(decision_id, "shown")
    return checkpoint


def test_the_full_state(tree):
    checkpoint = full_tree(tree)
    status = {"state": "ready", "model_ids": {FINE_TUNE: f"laya-local/{FINE_TUNE}@{checkpoint.name}"}}
    out = ops_state(tree.base, status, [], lab_ok=True, now=NOW)
    assert out["v"] == 1 and out["ok"] is True and out["status"] == status
    [served] = out["served"]
    assert served == {
        "name": FINE_TUNE,
        "surface": "agent_open_work",
        "checkpoint": checkpoint.name,
        "model_id": f"laya-local/{FINE_TUNE}@{checkpoint.name}",
        "threshold": {"at": 0.62, "source": "swept", "swept_at": "2026-10-27T01:00:00Z", "done_at": 0.4},
        "eval": {
            "items": 12,
            "before": {"accuracy": 0.8, "brier": 0.15, "ece": 0.05, "loss": 0.4},
            "after": {"accuracy": 0.8, "brier": 0.15, "ece": 0.05, "loss": 0.4},
        },
        "golden": {"at": "2026-10-27T01:00:00Z", "cases": 32, "accuracy": 0.75, "kappa": 0.5, "brier": 0.21},
        "scores": {"at": "2026-10-27T02:00:00Z", "rows": 1, "current": True},
    }
    assert out["flags_7d"]["agent_open_work"] == {"decisions": 1, "shown": 1, "dismissed": 0, "accepted": 0}
    assert out["sweeps"] == [
        {
            "surface": "agent_open_work",
            "at": "2026-10-27T01:05:00Z",
            "rows": 48,
            "labels": "operator",
            "best": 0.62,
            "live": 0.62,
            "source": "swept",
            "kappa": 0.55,
            "done_live": 0.4,
            "calibration": None,
        }
    ]
    [golden] = out["golden"]
    assert golden["suite"] == SUITE and golden["private"] == {"cases": 0, "open": 0, "done": 0} and golden["candidates"] == 0
    assert golden["public"]["cases"] >= 10
    [review] = out["review"]
    # the flagged decision, and each public case the deck deals for the operator's sign-off (lab_files.BATCH_TAG: a
    # batch's sample, not all of it)
    public, _code = lab_files.read_golden_suite(paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl")
    dealt = lab_files.dealt_golden_ids(public, {})
    assert dealt <= {case["id"] for case in public if lab_files.is_provisional(case)}
    assert review["surface"] == "agent_open_work" and review["to_review"] == 1 + len(dealt)
    assert out["lab"] == {"ok": True, "hint": None}
    assert len(out["jobs"]) == len(lab_jobs.LAB_VERBS) and all(job["cli"] for job in out["jobs"])


def test_todays_golden_report_shape_alone(tree):
    tree.register(train_texts=[])
    tree.path(f"evals/agent-open-work-{FINE_TUNE}-20260102-030405.json").write_text(json.dumps(golden_report(FINE_TUNE)))
    [served] = ops_state(tree.base, None, [], lab_ok=True, now=NOW)["served"]
    assert served["golden"] == {"at": "2026-01-02T03:04:05", "cases": 32, "accuracy": 0.75, "kappa": None, "brier": 0.21}
    assert served["threshold"] == {"at": 0.7, "source": "code", "swept_at": None, "done_at": 0.4}
    assert served["model_id"] == f"laya-local/{FINE_TUNE}@voidcraft-agent-open-work-20260102-0304"


def test_a_cut_swept_for_another_checkpoint_is_not_live(tree):
    full_tree(tree)
    loaded = {FINE_TUNE: str(tree.base / "checkpoints" / "voidcraft-agent-open-work-20261101-0000")}
    [served] = ops_state(tree.base, None, [], loaded=loaded, lab_ok=True, now=NOW)["served"]
    assert served["threshold"]["source"] == "code" and served["checkpoint"] == "voidcraft-agent-open-work-20261101-0000"
    assert served["scores"]["current"] is False


def test_no_fine_tune_is_one_english_row(tree):
    out = ops_state(tree.base, None, [], lab_ok=False, now=NOW)
    assert [(row["name"], row["checkpoint"], row["eval"]) for row in out["served"]] == [("english", None, None)]
    assert out["lab"] == {"ok": False, "hint": lab_jobs.LAB_HINT}
    assert out["sweeps"][0]["at"] is None and out["sweeps"][0]["live"] == 0.7


def test_no_params_is_a_message_and_defaults(tmp_path):
    for base, fragment in ((None, "not there yet"), (tmp_path / "laya", "not there yet")):
        out = ops_state(base, None, [], lab_ok=True)
        assert out["ok"] is True and fragment in out["message"]
        assert out["review"] == [] and out["sweeps"] == [] and out["golden"] == [] and out["flags_7d"] == {}
        assert [row["name"] for row in out["served"]] == ["english"]
        assert len(out["jobs"]) == len(lab_jobs.LAB_VERBS) and all(job["state"] == "idle" for job in out["jobs"])


def test_the_ops_route(tree, fake_runtime, monkeypatch):
    monkeypatch.setattr(lab_jobs, "list_panes", lambda: "laya-score\x1f0\x1f\x1fscore\x1fagent_open_work\x1flaya-score")
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    status, body = plugin.handle_request("GET", "/ops", {}, None, {"host": "localhost:7682", "origin": "http://localhost:5173"})
    assert status == 200 and body["status"]["state"] == "ready"
    assert [job["state"] for job in body["jobs"] if job["verb"] == "score"] == ["working"]
    assert body["compare"] == [], "no compare report yet: no COMPARE line"
    json.dumps(body, allow_nan=False)
