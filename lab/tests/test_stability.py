"""STABLE κ: the fixed stop set, each checkpoint scored on it once, and κ between consecutive checkpoints' verdicts.
Synthetic trees and a fake router only: no torch, no operator data."""

from __future__ import annotations

import os

import pytest

from laya_lab import cli, registry, stability
from laya_lab.registry import register
from laya_bridge import lab_files, paths, rows
from laya_bridge.ops import ops_state
from tldrack import open_work

from conftest import CHECKPOINT, FINE_TUNE, SUITE, SURFACE, FakeRouter, decide, synthetic

RIVAL = f"{FINE_TUNE}-20261101-0900"
NEWER = f"{FINE_TUNE}-20261102-0900"


def key_of(text: str) -> str:
    return rows.state_key(open_work.decide_state(text))


class ByModel(FakeRouter):
    """A router whose answer depends on which checkpoint is asked: `flips[model]` holds the states it reads the other way."""

    def __init__(self, flips: dict[str, set[str]] | None = None) -> None:
        super().__init__(lambda state: 0.0)
        self.flips = flips or {}

    def predict(self, state, questions: dict, model: str | None = None) -> dict:
        self.calls.append((state, model))
        is_open = ("-open " in state) != (state in self.flips.get(model or "", set()))
        return {"answers": {name: {"noul": 0.9 if is_open else 0.1} for name in questions}}


def three_checkpoints(lab) -> None:
    """A champion and two later versions, all on disk and registered (the middle one rejected by its successor)."""
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    register(lab.base, SURFACE, lab.checkpoint(RIVAL, train_texts=[]), {"eval_items": 1}, as_="challenger")
    register(lab.base, SURFACE, lab.checkpoint(NEWER, train_texts=[]), {"eval_items": 1}, as_="challenger")


# ── the stop set ──────────────────────────────────────────────────────────────


def test_the_stop_set_is_fixed_and_holds_only_live_states_nobody_labelled(lab, public_golden, monkeypatch):
    monkeypatch.setattr(lab_files, "STOP_SET_SIZE", 10)
    trained, sonnet, graded, golden_text = (
        "a trained closing text",
        "a sonnet-labelled closing text",
        "an operator-graded closing text",
        "Phase C is not started.",
    )
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[trained]), {"eval_items": 1})
    [sonnet_key, graded_key] = lab.unlabelled([sonnet, graded])
    lab.labels({sonnet_key: True})
    lab.operator(graded_key, "agree", against_open=False, by=FINE_TUNE, stratum="recent")
    free = synthetic("live-open", 8) + synthetic("live-done", 8)
    decide(lab, [trained, sonnet, graded, golden_text, *free])
    decide(lab, ["an older question's text"], question_version=open_work.QUESTION_VERSION - 1)

    code, summary = stability.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5))
    assert code == 0 and (summary["states"], summary["added"], summary["retired"]) == (10, 10, 0)
    stop, _code = lab_files.read_stop_set(lab.base, SURFACE)
    chosen = [row["key"] for row in stop]
    assert chosen == sorted(key_of(text) for text in free)[:10], "the first ten eligible keys, in key order"
    assert not {key_of(text) for text in (trained, sonnet, graded, golden_text)} & set(chosen)
    assert all(row["state"] in free for row in stop)
    assert os.stat(paths.stop_set_path(lab.base, SURFACE)).st_mode & 0o777 == 0o600

    decide(lab, synthetic("later-open", 30))
    before = paths.stop_set_path(lab.base, SURFACE).read_bytes()
    router = FakeRouter(lambda state: 0.5)
    assert stability.run(SUITE, lab.base, router=router)[1]["added"] == 0 and router.calls == []
    assert paths.stop_set_path(lab.base, SURFACE).read_bytes() == before, "a full set is never re-drawn"

    lab.operator(chosen[0], "flip", against_open=True, by=FINE_TUNE, stratum="recent")
    router = FakeRouter(lambda state: 0.5)
    code, summary = stability.run(SUITE, lab.base, router=router)
    assert (summary["added"], summary["retired"], len(router.calls)) == (1, 1, 1), "a graded state leaves; one joins"
    stop, _code = lab_files.read_stop_set(lab.base, SURFACE)
    scored, _code = lab_files.read_stability(lab.base, SURFACE)
    assert chosen[0] not in {row["key"] for row in stop} and len(stop) == 10
    assert {row["key"] for row in scored} == {row["key"] for row in stop}, "its scores left with it"


def test_a_long_text_is_taken_under_the_state_it_is_asked_as(lab):
    """A label is keyed by its text; the ledger holds the fitted state, whose key is another."""
    long_text = "a long labelled closing text " + "word " * 400
    [text_key] = lab.unlabelled([long_text])
    lab.labels({text_key: True})
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    decide(lab, [long_text, "a short free closing text"])
    assert key_of(long_text) != text_key and key_of(long_text) in stability.taken_keys(lab.base, stability.SUITES[SUITE])
    stability.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5))
    assert [row["key"] for row in lab_files.read_stop_set(lab.base, SURFACE)[0]] == [key_of("a short free closing text")]


# ── the scores and the meter ──────────────────────────────────────────────────


def test_each_checkpoint_is_scored_once_and_kappa_reads_consecutive_pairs(lab):
    three_checkpoints(lab)
    texts = synthetic("live-open", 20) + synthetic("live-done", 20)
    decide(lab, texts)
    moved = {open_work.decide_state(text) for text in texts[:5]}
    router = ByModel({RIVAL: moved, NEWER: moved})
    code, summary = stability.run(SUITE, lab.base, router=router)
    assert code == 0 and summary["scored"] == {CHECKPOINT: 40, RIVAL: 40, NEWER: 40}
    assert {model for _state, model in router.calls} == {FINE_TUNE, RIVAL, NEWER}, "the champion is asked by its served name"
    meter = summary["meter"]
    assert meter["versions"] == [CHECKPOINT, RIVAL, NEWER] and meter["states"] == 40 and meter["device"] == "mps"
    first, second = meter["pairs"]
    assert (first["from"], first["to"], first["n"], first["flips"], first["agree"]) == (CHECKPOINT, RIVAL, 40, 5, 0.875)
    assert first["kappa"] == 0.75 and (second["flips"], second["kappa"]) == (0, 1.0)
    assert (meter["kappa_average"], meter["window"], meter["stable"]) == (0.875, 2, False)
    assert "closing message" not in paths.stability_path(lab.base, SURFACE).read_text(), "keys and numbers only"

    again = ByModel()
    assert stability.run(SUITE, lab.base, router=again)[1]["scored"] == {CHECKPOINT: 0, RIVAL: 0, NEWER: 0} and again.calls == []
    [row] = ops_state(lab.base, None, [], lab_ok=True)["stability"]
    assert row == {
        "surface": SURFACE,
        "device": "mps",
        "states": 40,
        "versions": 3,
        "pairs": meter["pairs"],
        "kappa_average": 0.875,
        "window": 2,
        "stable": False,
        "last": meter["last"],
    }
    assert f"  stable   {SURFACE} · κ 0.875 over 2 pairs · 40 states · 3 checkpoints · {meter['last']}" in cli.status_lines(
        ops_state(lab.base, None, [], lab_ok=True)
    )

    (lab.base / "checkpoints" / RIVAL / "model.safetensors").write_bytes(b"retrained in place")
    retrained = ByModel({NEWER: moved})
    code, summary = stability.run(SUITE, lab.base, router=retrained)
    assert summary["scored"] == {CHECKPOINT: 0, RIVAL: 40, NEWER: 0}, "a new digest is scored again"
    assert [pair["flips"] for pair in summary["meter"]["pairs"]] == [0, 5]
    assert len(lab_files.read_stability(lab.base, SURFACE)[0]) == 120, "the old digest's rows are gone"

    on_cpu = ByModel()
    assert stability.run(SUITE, lab.base, router=on_cpu, device="cpu")[1]["scored"][NEWER] == 40, "CPU p is not MPS p"
    assert len(lab_files.read_stability(lab.base, SURFACE)[0]) == 240


def test_a_retired_state_s_scores_leave_before_anything_is_scored(lab, monkeypatch):
    """A pass that stops mid-score still leaves the scores file and the stop set naming the same states."""
    monkeypatch.setattr(lab_files, "STOP_SET_SIZE", 30)
    three_checkpoints(lab)
    decide(lab, synthetic("live-open", 20) + synthetic("live-done", 20))
    stability.run(SUITE, lab.base, router=ByModel())
    [gone, *stayed] = [row["key"] for row in lab_files.read_stop_set(lab.base, SURFACE)[0]]
    lab.operator(gone, "flip", against_open=True, by=FINE_TUNE, stratum="recent")

    class WillNotLoad(FakeRouter):
        def predict(self, state, questions: dict, model: str | None = None) -> dict:
            raise RuntimeError("the checkpoint did not load")

    with pytest.raises(RuntimeError):
        stability.run(SUITE, lab.base, router=WillNotLoad(lambda state: 0.5))
    stop = [row["key"] for row in lab_files.read_stop_set(lab.base, SURFACE)[0]]
    assert gone not in stop and stop[:29] == stayed and len(stop) == 30, "the state left and one took its place"
    scored, _code = lab_files.read_stability(lab.base, SURFACE)
    assert {row["key"] for row in scored} == set(stayed), "no score outlives the state it was read on"
    [row] = ops_state(lab.base, None, [], lab_ok=True)["stability"]
    assert [pair["n"] for pair in row["pairs"]] == [29, 29]

    code, summary = stability.run(SUITE, lab.base, router=ByModel())
    assert code == 0 and summary["scored"] == {CHECKPOINT: 1, RIVAL: 1, NEWER: 1}, "the next pass scores the newcomer"
    assert [pair["n"] for pair in summary["meter"]["pairs"]] == [30, 30]


def test_a_swept_cut_moves_the_verdicts_it_is_read_at(lab):
    three_checkpoints(lab)
    decide(lab, synthetic("live-open", 12) + synthetic("live-done", 12))
    stability.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.65 if "-open " in state else 0.2))
    registry.set_threshold(
        lab.base, SURFACE, lab_files.threshold_entry(0.6, RIVAL, RIVAL, "2026-11-08T00:00:00Z", "operator", 40, 0.5, None)
    )
    cuts = lab_files.stability_cuts(paths.registry_versions(lab.base), SURFACE)
    assert cuts == {RIVAL: 0.6}
    meter = lab_files.stability_summary(lab_files.read_stability(lab.base, SURFACE)[0], cuts)
    assert [pair["flips"] for pair in meter["pairs"]] == [12, 12], "0.65 is ⚠️ at RIVAL's 0.6, not at the code's 0.7"
    assert [pair["flips"] for pair in lab_files.stability_summary(lab_files.read_stability(lab.base, SURFACE)[0])["pairs"]] == [
        0,
        0,
    ]


def test_the_file_keeps_a_deleted_checkpoint_s_rows_but_not_every_checkpoint_ever():
    def row(version: str, key: str, digest: str = "sha256:a", at: str = "2026-11-08T00:00:00Z") -> dict:
        return lab_files.stability_row(key=key, version=version, digest=digest, device="mps", p=0.5, cut=0.7, scored_at=at)

    many = [row(f"{FINE_TUNE}-202611{i:02d}-0900", "0" * 16) for i in range(1, stability.VERSIONS_KEPT + 4)]
    kept = stability.compact(many)
    assert len(kept) == stability.VERSIONS_KEPT and kept[0]["version"] == f"{FINE_TUNE}-20261104-0900"
    stale = [row(CHECKPOINT, "1" * 16), row(CHECKPOINT, "2" * 16, "sha256:b", "2026-11-09T00:00:00Z")]
    assert [r["digest"] for r in stability.compact(stale)] == ["sha256:b"]


def test_with_no_fine_tune_or_no_live_state_there_is_nothing_to_compare(lab):
    code, summary = stability.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5))
    assert code == 0 and summary["note"] == "the ledger holds no unlabelled live state yet"
    assert not paths.stop_set_path(lab.base, SURFACE).exists() and summary["meter"]["kappa_average"] is None
    decide(lab, ["a live closing text"])
    code, summary = stability.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5))
    assert code == 0 and summary["note"] == "no fine-tune is registered: nothing to compare" and summary["states"] == 1
    assert ops_state(lab.base, None, [], lab_ok=True)["stability"] == []


def test_a_file_its_reader_refuses_is_left_as_it_is(lab, tmp_path, capsys):
    three_checkpoints(lab)
    decide(lab, synthetic("live-open", 3))
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("")
    target = paths.stop_set_path(lab.base, SURFACE)
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)
    router = FakeRouter(lambda state: 0.5)
    code, summary = stability.run(SUITE, lab.base, router=router)
    assert code == 1 and "left as they are" in summary["note"] and router.calls == []
    assert target.is_symlink() and elsewhere.read_text() == "" and not paths.stability_path(lab.base, SURFACE).exists()
    with pytest.raises(stability.Unreadable):
        stability.plan(SUITE, lab.base)
    assert stability.cmd(SUITE, lab.base, device="mps") == 1 and "left as they are" in capsys.readouterr().out
    assert cli.COMMANDS["stability"] is cli.cmd_stability
