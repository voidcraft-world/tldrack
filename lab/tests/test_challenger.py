"""Promotion/rollback boundaries without loading a model or touching the bridge."""

from datetime import datetime, timedelta, timezone
import pytest
from laya_lab import challenger, registry, retrain
from laya_bridge import challenger as preflight, lab_files
from conftest import SUITE, SURFACE, CHECKPOINT

OTHER = "tldrack-agent-open-work-20261101-120001"


def state():
    return {"name": OTHER, "token": "a" * 64, "ready": True, "requirements": {"gate": True}}


def pair(lab):
    first = lab.checkpoint(CHECKPOINT)
    second = lab.checkpoint(OTHER)
    registry.register(lab.base, SURFACE, first, {})
    registry.register(lab.base, SURFACE, second, {}, as_="challenger")
    registry.set_threshold(
        lab.base,
        SURFACE,
        lab_files.threshold_entry(0.65, OTHER, OTHER, "2026-11-09T10:00:00Z", "operator", 40, 0.7, "sweep.json", 0.25),
    )


def test_armed_decision_refuses_stale_and_not_ready(lab, monkeypatch):
    pair(lab)
    monkeypatch.setattr(preflight, "view", lambda *_: state())
    with pytest.raises(ValueError, match="changed"):
        challenger.act(lab.base, SURFACE, "promote", "b" * 64)
    monkeypatch.setattr(preflight, "view", lambda *_: {**state(), "ready": False})
    with pytest.raises(ValueError, match="promotion needs"):
        challenger.act(lab.base, SURFACE, "promote", "a" * 64)
    assert registry.alias_target(lab.base, SURFACE, "champion")[0] == CHECKPOINT


def test_promotion_carries_cut_and_first_three_are_manual(lab, monkeypatch):
    pair(lab)
    monkeypatch.setattr(preflight, "view", lambda *_: state())
    with pytest.raises(ValueError, match="first three"):
        challenger.act(lab.base, SURFACE, "promote", "a" * 64, auto=True)
    challenger.act(lab.base, SURFACE, "promote", "a" * 64)
    assert registry.alias_target(lab.base, SURFACE, "champion")[0] == OTHER
    assert registry.alias_target(lab.base, SURFACE, "previous")[0] == CHECKPOINT
    assert registry.served_threshold(lab.base, SURFACE, registry.served_model(lab.base, SURFACE))[0] == 0.65
    assert not challenger.automatic(lab.base, SURFACE)


def test_three_agreeing_manual_promotions_enable_auto_then_reject_stops(lab):
    for _ in range(3):
        challenger.record(lab.base, {"surface": SURFACE, "action": "promote", "automatic": False, "agrees": True})
    assert challenger.automatic(lab.base, SURFACE)
    challenger.record(lab.base, {"surface": SURFACE, "action": "reject", "agrees": False})
    assert not challenger.automatic(lab.base, SURFACE)


def test_regression_rolls_back_to_previous_cut(lab, monkeypatch):
    from laya_lab import gate

    pair(lab)
    monkeypatch.setattr(preflight, "view", lambda *_: state())
    challenger.act(lab.base, SURFACE, "promote", "a" * 64)
    monkeypatch.setattr(
        gate, "judge", lambda *a, **k: ({"verdict": "REFUSE", "checks": {"private_golden": {"state": "fail"}}}, None)
    )
    restarted = []
    monkeypatch.setattr(challenger, "restart", lambda: restarted.append(True))
    assert challenger.nightly(SUITE, lab.base, device="cpu", now=datetime.now(timezone.utc) + timedelta(days=1))["rollback"]
    assert registry.alias_target(lab.base, SURFACE, "champion")[0] == CHECKPOINT
    assert restarted == [True, True]  # recovery of the manual restart, then the rollback restart


def test_changed_rubric_preserves_open_journal(lab):
    from datetime import datetime, timezone

    journal = retrain.new_journal(SUITE, "mps", retrain.Owed(True, "fixture"), datetime.now(timezone.utc))
    journal["question_version"] = -1
    retrain.save(lab.base, journal, datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="rubric changed"):
        retrain.run(SUITE, lab.base)


def test_same_night_retry_does_not_consume_rollback_guard(lab, monkeypatch):
    from laya_lab import gate

    pair(lab)
    monkeypatch.setattr(preflight, "view", lambda *_: state())
    challenger.act(lab.base, SURFACE, "promote", "a" * 64)
    monkeypatch.setattr(challenger, "restart", lambda: None)
    monkeypatch.setattr(gate, "judge", lambda *a, **k: pytest.fail("same-night gate"))
    assert challenger.nightly(SUITE, lab.base, device="cpu")["state"] == "skipped"
    assert all(r["action"] != "night-check" for r in challenger.history(lab.base))


def test_cut_after_alias_write_recovers_promotion_event_before_restart(lab, monkeypatch):
    pair(lab)
    monkeypatch.setattr(preflight, "view", lambda *_: state())
    real = challenger.record
    monkeypatch.setattr(challenger, "record", lambda *a: (_ for _ in ()).throw(RuntimeError("cut")))
    with pytest.raises(RuntimeError):
        challenger.act(lab.base, SURFACE, "promote", "a" * 64)
    assert registry.alias_target(lab.base, SURFACE, "champion")[0] == OTHER
    assert challenger.history(lab.base) == []
    monkeypatch.setattr(challenger, "record", real)
    monkeypatch.setattr(challenger, "restart", lambda: None)
    challenger.nightly(SUITE, lab.base, device="cpu")
    assert len(challenger.history(lab.base)) == 1
    assert challenger.history(lab.base)[0]["previous"] == CHECKPOINT
    assert not (lab.base / challenger.RESTART_PENDING).exists()


def test_preflight_binds_rubric_champion_weights_reserve_and_three_scoring_days(lab, monkeypatch):
    version = {
        "name": OTHER,
        "surface": SURFACE,
        "path": str(lab.base / "checkpoints" / OTHER),
        "cut": {"at": 0.65, "done_at": 0.25, "report": "reserve.json", "swept_at": "2026-11-09T10:00:00Z"},
        "tags": {"gate": "PASS", "gate_report": "gate.json"},
    }
    versions = {"aliases": {SURFACE: {"champion": None, "challenger": OTHER, "previous": None}}, "versions": {OTHER: version}}
    gate = {
        "promote": True,
        "evidence": {"question_version": 2},
        "candidate": {"checkpoint": OTHER, "at": 0.65, "done_at": 0.25, "stamp": {"digest": "hash"}},
        "served": {"name": "english", "checkpoint": None, "at": 0.7, "done_at": 0.4},
        "checks": {key: {"state": "pass", "p": 0.1} for key in preflight.CHECKS},
    }
    reserve = {
        "kind": "heldout",
        "suite": SUITE,
        "checkpoint": {"name": OTHER, "digest": "hash"},
        "labels": "operator",
        "question_version": 2,
        "reserve_only": True,
        "reserve": {"rows": 40, "open": 20, "done": 20},
        "applied": {"at": 0.65, "done_at": 0.25},
    }
    monkeypatch.setattr(preflight.paths, "registry_versions", lambda *_: versions)
    monkeypatch.setattr(preflight, "cached_digest", lambda v: "hash")
    monkeypatch.setattr(preflight, "read_report", lambda _b, name: gate if name == "gate.json" else reserve)
    monkeypatch.setattr(preflight.review_data, "snapshot", lambda *_: None)
    monkeypatch.setattr(preflight.review_data, "challenger_sides", lambda *_: {"graded": 10, "with_challenger": 7})
    shadow = [
        {
            "question_version": 2,
            "challenger": OTHER,
            "challenger_digest": "hash",
            "at": f"2026-11-0{i}T10:00:00Z",
            "scored_at": f"2026-11-0{i}T10:00:00Z",
            "served_p": 0.9,
            "served_threshold": 0.7,
            "served_done_threshold": 0.4,
            "challenger_p": 0.8,
            "challenger_threshold": 0.65,
            "challenger_done_threshold": 0.25,
        }
        for i in (1, 2, 3)
    ]
    monkeypatch.setattr(preflight.lab_files, "read_shadow", lambda *_: (shadow, "ok"))
    assert preflight.view(lab.base, SURFACE)["ready"]
    token = preflight.view(lab.base, SURFACE)["token"]
    gate["run_id"] = "new evidence"
    assert preflight.view(lab.base, SURFACE)["token"] != token
    gate["evidence"]["question_version"] = 1
    assert not preflight.view(lab.base, SURFACE)["requirements"]["gate"]
    gate["evidence"]["question_version"] = 2
    reserve["checkpoint"]["name"] = CHECKPOINT
    assert not preflight.view(lab.base, SURFACE)["requirements"]["reserve"]
    reserve["checkpoint"]["name"] = OTHER
    for row in shadow:
        row["scored_at"] = "2026-11-09T10:00:00Z"
    assert not preflight.view(lab.base, SURFACE)["requirements"]["shadow"]
