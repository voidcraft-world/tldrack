"""Synthetic gates of the four-way challenger and its shadow-only persistence. No model or live data."""

from types import SimpleNamespace
import json
import pytest
from laya_lab import needs_you as n
from laya_lab.data import read_jsonl
from laya_bridge import files, label_budget, rows


@pytest.mark.parametrize(
    "counts,kappa,ready",
    [
        ((20, 20, 20, 20), 0.6, True),
        ((19, 30, 30, 30), 0.8, False),
        ((30, 19, 30, 30), 0.8, False),
        ((30, 30, 19, 30), 0.8, False),
        ((30, 30, 30, 19), 0.8, False),
        ((20, 20, 20, 20), 0.59, False),
        ((20, 20, 20, 20), None, False),
    ],
)
def test_readiness(monkeypatch, tmp_path, counts, kappa, ready):
    monkeypatch.setattr(n.source, "require_sources", lambda base: None)
    monkeypatch.setattr(n.review_data, "snapshot", lambda *a: SimpleNamespace(fold=None, spec=None))
    monkeypatch.setattr(n.review_data, "three_way", lambda *a: dict(zip(n.CLASSES, counts)))
    monkeypatch.setattr(n.review_data, "intra_rater", lambda s: {"needs": {"you": {"kappa": kappa, "n": 20}}})
    assert n.status(tmp_path)["ready"] is ready


def test_gates_precede_model(monkeypatch, tmp_path):
    monkeypatch.setattr(n, "status", lambda b: {"ready": False, "reason": "grades"})
    monkeypatch.setattr(n, "dataset", lambda b: pytest.fail("must not read training data"))
    assert n.run(tmp_path)["state"] == "skipped"
    monkeypatch.setattr(n, "status", lambda b: {"ready": True, "reason": None})
    monkeypatch.setattr(n, "night_open", lambda: False)
    assert n.run(tmp_path)["reason"] == n.OUTSIDE_WINDOW


def test_reserve_never_trains(lab, public_golden):
    keys = lab.unlabelled([f"Synthetic choice hand-off {i}, no private content." for i in range(80)])
    for i, key in enumerate(keys):
        lab.operator(key, against_open=i % 3 != 0)
    path = lab.path("data/agent-open-work-operator.jsonl")
    labels = [json.loads(line) for line in path.read_text().splitlines()]
    for i, label in enumerate(labels):
        label["at"] = "2026-11-20T01:02:03Z"  # after READ, so a bare DONE is DONE (review_data.py READ_SINCE)
        if label["open"]:
            label["needs"] = "running" if i % 3 == 1 else "you"
        elif i % 2:
            label["needs"] = "read"
    path.write_text("".join(json.dumps(r) + "\n" for r in labels))
    fitted, held = n.dataset(lab.base)
    assert fitted and held
    assert all(label_budget.drawn(r["key"]) == "train" for r in fitted)
    assert all(label_budget.drawn(r["key"]) == "reserve" for r in held)
    assert {r["expected"]["q"] for r in fitted} == set(n.CLASSES)
    assert not {rows.state_key(r["state"]) for r in fitted} & {rows.state_key(r["state"]) for r in held}


def test_calibration_never_uses_reserve_or_registers(lab, monkeypatch):
    monkeypatch.setattr(n, "night_open", lambda: True)

    def row(label, i, prefix):
        return {
            "key": rows.state_key(f"{prefix} {label} {i}"),
            "state": {"hand_off": f"{prefix} {label} {i}"},
            "questions": {"q": n.QUESTION},
            "expected": {"q": label},
        }

    fitted = [row(c, i, "train") for c in n.CLASSES for i in range(80)]
    reserve = [row(c, 0, "reserve") for c in n.CLASSES]

    def fake_fit(base, plan):
        training = read_jsonl(plan.work / "train.jsonl")
        cal = read_jsonl(plan.work / "eval.jsonl")
        assert {r["key"] for r in training}.isdisjoint(r["key"] for r in cal)
        assert {r["key"] for r in training + cal} == {r["key"] for r in fitted}
        assert not {r["key"] for r in training + cal} & {r["key"] for r in reserve}
        assert set(n.CLASSES) == {r["expected"]["q"] for r in cal}
        (plan.out / "model.safetensors").write_bytes(b"synthetic")

    monkeypatch.setattr(n.train, "fit", fake_fit)
    doc = n.fit_head(lab.base, fitted, reserve, "cpu")
    assert n.manifest(lab.base) == doc
    assert not (lab.base / "models.json").exists()
    assert rows.checkpoint_keys(lab.base, doc["checkpoint"])
    assert all(r["key"] not in rows.checkpoint_keys(lab.base, doc["checkpoint"]) for r in reserve)


class ChoiceRouter:
    def __init__(self):
        self.calls = 0

    def predict(self, *a, **kw):
        self.calls += 1
        return {"answers": {"q": {"choice": "you", "probabilities": {"you": 0.7, "running": 0.2, "done": 0.1}}}}


def test_running_signal_preserves_raw_choice(monkeypatch):
    monkeypatch.setattr(n, "night_open", lambda: True)
    router = ChoiceRouter()
    assert n.predict(router, "checkpoint", {}, {"held": "background"}) == ("running", "you", 0.7)
    assert n.predict(router, "checkpoint", {}, {"held": None}) == ("you", "you", 0.7)
    monkeypatch.setattr(n, "night_open", lambda: False)
    with pytest.raises(ValueError, match=n.OUTSIDE_WINDOW):
        n.predict(router, "checkpoint", {})
    assert router.calls == 2


def test_resume_and_distinct_scoring_days(lab, monkeypatch):
    checkpoint = "tldrack-agent-open-work-20261109-010000-3"
    lab.checkpoint(checkpoint)
    monkeypatch.setattr(n, "manifest", lambda b: {"checkpoint": checkpoint, "at": "2026-11-09T00:00:00Z"})
    monkeypatch.setattr(n, "status", lambda b: {"ready": True})
    monkeypatch.setattr(n, "night_open", lambda: True)
    monkeypatch.setattr(n, "dataset", lambda b: ([], []))
    monkeypatch.setattr(n.landing, "landings_by_decision", lambda b: {})
    live = [SimpleNamespace(decision_id=f"d{i}", key=f"k{i}", at=f"2026-11-{i + 1:02d}T00:00:00Z", state={}) for i in range(3)]
    monkeypatch.setattr(n.shadow, "live_states", lambda *a: (live, {}))
    router = ChoiceRouter()
    first = n.run(lab.base, device="cpu", router=router)
    second = n.run(lab.base, device="cpu", router=router)
    assert first["new"] == 3 and second["new"] == 0 and router.calls == 3
    assert first["shadow_days"] == 1 and first["live_switch"] is False
    assert not (lab.base / "models.json").exists()
    assert not any("state" in r for r in read_jsonl(n.root(lab.base) / "shadow.jsonl"))


def test_old_and_symlink_manifest_refused(lab):
    path = n.root(lab.base) / "model.json"
    path.parent.mkdir()
    path.write_text('{"version":0}')
    with pytest.raises(ValueError):
        n.manifest(lab.base)
    path.unlink()
    path.symlink_to(lab.path("outside.json"))
    with pytest.raises(files.SourceError):
        n.manifest(lab.base)


def test_no_precision_without_denominator():
    measured = n.measurements([("done", "you"), ("running", "running")])
    assert measured["needs_you_precision"]["value"] is None
    assert measured["confusion"]["you"]["done"] == 1


def test_aliases_of_reserve_and_conflicting_labels_cannot_train(monkeypatch, tmp_path):
    fold = SimpleNamespace(
        labels={
            "aaa": {"open": True, "needs": "you"},
            "bbb": {"open": True, "needs": "you"},
            "ccc": {"open": False, "at": "2026-11-20T00:00:00Z"},
            "ddd": {"open": True, "needs": "running"},
        }
    )
    monkeypatch.setattr(n.source, "require_sources", lambda b: fold)
    monkeypatch.setattr(
        n.source, "texts", lambda *a: {"aaa": "same state", "bbb": "same state", "ccc": "conflict", "ddd": "conflict"}
    )
    monkeypatch.setattr(n.source, "reserve_held", lambda *a: (None, frozenset({"bbb"})))
    monkeypatch.setattr(n.source, "golden_held", lambda b: frozenset())
    monkeypatch.setattr(n.review_data, "golden_suites", lambda *a: ([], files.MISSING, [], files.MISSING))
    fitted, held = n.dataset(tmp_path)
    assert fitted == [] and len(held) == 1 and held[0]["expected"]["q"] == "you"


@pytest.mark.parametrize("confidence", [None, float("nan"), -1, 1.1, True])
def test_invalid_choice_confidence_refused(monkeypatch, confidence):
    monkeypatch.setattr(n, "night_open", lambda: True)
    router = SimpleNamespace(predict=lambda *a, **kw: {"answers": {"q": {"choice": "you", "confidence": confidence}}})
    with pytest.raises(ValueError):
        n.predict(router, "checkpoint", {})


def test_choice_checkpoint_stays_out_of_binary_candidates_and_retention_keeps_pointer(lab):
    from laya_lab import registry

    name = "tldrack-agent-open-work-20261101-010000-3"
    lab.checkpoint(name)
    lab.path(f"checkpoints/{name}.data/head.json").write_text('{"kind":"needs-you-choice"}')
    for i in range(2, 6):
        lab.checkpoint(f"tldrack-agent-open-work-2026110{i}-010000")
    lab.path("needs-you/model.json").write_text(json.dumps({"version": 1, "shadow_only": True, "checkpoint": name}))
    assert name not in registry.available_checkpoints(lab.base, n.SURFACE)
    assert name in registry.retention(lab.base, n.SURFACE)["keep"]
    registry.prune_rejects(lab.base, n.SURFACE)
    assert (lab.base / "checkpoints" / name / "model.safetensors").is_file()
    lab.path("needs-you/model.json").write_text("{broken")
    assert name in registry.retention(lab.base, n.SURFACE)["keep"]
