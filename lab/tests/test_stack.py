"""The stack voter (`stack.py`): deterministic, out of fold, a win where the voters err differently and none where one
voter carries nothing. Every row, id and number here is invented."""

from __future__ import annotations

import numpy as np
import pytest

from laya_lab import compare, stack
from laya_lab.metrics import auc

from conftest import SUITE, FakeRouter, synthetic


def rows(n: int, seed: int) -> tuple[list[str], list[bool], np.random.Generator]:
    rng = np.random.default_rng(seed)
    ids = [f"row-{seed}-{i}" for i in range(n)]
    expected = [bool(i % 2) for i in range(n)]
    return ids, expected, rng


def noisy_voter(expected: list[bool], noise: np.ndarray, signal: float = 1.0) -> list[float]:
    """p = σ(signal·(±1) + noise): an informative voter whose mistakes are its noise."""
    z = np.array([signal if e else -signal for e in expected]) + noise
    return [float(p) for p in 1 / (1 + np.exp(-z))]


def test_the_same_rows_give_the_same_stack_in_any_order():
    ids, expected, rng = rows(120, 1)
    a = noisy_voter(expected, rng.normal(0, 1.5, 120))
    b = noisy_voter(expected, rng.normal(0, 1.5, 120))
    first = stack.out_of_fold([a, b], expected, ids)
    again = stack.out_of_fold([a, b], expected, ids)
    assert first == again

    order = list(reversed(range(120)))
    shuffled = stack.out_of_fold(
        [[a[i] for i in order], [b[i] for i in order]], [expected[i] for i in order], [ids[i] for i in order]
    )
    by_id = dict(zip([ids[i] for i in order], shuffled.probabilities))
    assert [by_id[row_id] for row_id in ids] == pytest.approx(first.probabilities, abs=1e-9)  # a fold is its id's hash
    assert [stack.fold_of(row_id) for row_id in ids] == first.folds


def test_a_row_is_never_scored_by_a_stack_that_saw_its_label():
    ids, expected, rng = rows(80, 2)
    a = noisy_voter(expected, rng.normal(0, 1.0, 80))
    b = noisy_voter(expected, rng.normal(0, 1.0, 80))
    stacked = stack.out_of_fold([a, b], expected, ids)
    for fold, trained in stacked.trained_on.items():
        assert trained == frozenset(i for i, f in enumerate(stacked.folds) if f != fold)
    # The behavioural half: flip one row's label and its own p does not move, because its scorer never read it.
    for i in (0, 17, 41):
        flipped = list(expected)
        flipped[i] = not flipped[i]
        assert stack.out_of_fold([a, b], flipped, ids).probabilities[i] == stacked.probabilities[i]


def test_stacking_beats_both_voters_when_their_mistakes_differ():
    ids, expected, rng = rows(600, 3)
    a = noisy_voter(expected, rng.normal(0, 1.6, 600))
    b = noisy_voter(expected, rng.normal(0, 1.6, 600))
    stacked = stack.out_of_fold([a, b], expected, ids)
    found = stack.verdict(stacked.probabilities, {"a": a, "b": b}, expected)
    assert found is not None and found["win"] is True and found["lo"] > 0
    assert auc(stacked.probabilities, expected) > max(auc(a, expected), auc(b, expected))


def test_stacking_cannot_win_over_a_voter_that_carries_nothing():
    ids, expected, rng = rows(600, 4)
    a = noisy_voter(expected, rng.normal(0, 1.6, 600))
    noise = [float(p) for p in rng.uniform(0, 1, 600)]  # knows nothing the first voter does not
    stacked = stack.out_of_fold([a, noise], expected, ids)
    found = stack.verdict(stacked.probabilities, {"a": a, "noise": noise}, expected)
    assert found is not None and found["best_single"] == "a" and found["win"] is False
    assert found["lo"] <= 0 <= found["hi"] or found["hi"] < 0


def test_rows_that_cannot_fold_are_refused_with_the_reason():
    assert stack.check([[0.5] * 12], [True, False] * 6, [f"r{i}" for i in range(12)]) == "a stack needs two voters or more"
    assert "cannot fit" in (stack.check([[0.5] * 6, [0.5] * 6], [True, False] * 3, [f"r{i}" for i in range(6)]) or "")
    assert "cannot fit" in (stack.check([[0.5] * 12, [0.5] * 12], [True] * 12, [f"r{i}" for i in range(12)]) or "")
    assert "share an id" in (stack.check([[0.5] * 12, [0.5] * 12], [True, False] * 6, ["r"] * 12) or "")
    with pytest.raises(ValueError, match="stack:"):
        stack.out_of_fold([[0.5] * 6, [0.5] * 6], [True, False] * 3, [f"r{i}" for i in range(6)])


def test_mistakes_count_each_direction_at_each_voters_own_cut():
    expected = [True, True, False, False]
    found = stack.mistakes({"a": [0.9, 0.2, 0.1, 0.8], "b": [0.4, 0.6, 0.1, 0.1]}, expected, {"a": 0.5, "b": 0.3})
    # a is wrong on rows 1 and 3; b (cut 0.3) is right on both. b is wrong on nothing.
    assert found == [
        {"a": "a", "b": "b", "a_wrong": 2, "b_right": 2, "share": 1.0},
        {"a": "b", "b": "a", "a_wrong": 0, "b_right": 0, "share": None},
    ]


def test_compare_writes_a_stack_beside_mean_with_every_voters_columns(lab, public_golden, monkeypatch):
    """Two voters through `compare`: the stack row has the same columns as every voter, the ΔAUC and the mistakes sit
    in the set's `stack` block, the stack never enters the pairwise agreement, and no text enters the report."""
    open_texts, done_texts = synthetic("still-open", 12), synthetic("all-done", 12)
    keys = lab.unlabelled(open_texts + done_texts)
    lab.labels({key: i < 12 for i, key in enumerate(keys)})
    # Laya is wrong on three done rows ("all-done 1", "… 10", "… 11"); the other voter on one open row.
    laya = FakeRouter(lambda state: 0.9 if state.startswith("all-done 1") else (0.8 if "open" in state else 0.3))
    other = FakeRouter(lambda state: 0.1 if state.startswith("still-open 2:") else (0.9 if "open" in state else 0.1))
    monkeypatch.setattr(compare, "endpoint_router", lambda _base, _suite, _target: other)

    code, report, path = compare.evaluate(SUITE, lab.base, voters=["laya:english", "ep=http://127.0.0.1:1"], router=laya)

    assert code == 0 and report is not None and path is not None
    held = report["sets"]["heldout"]
    assert list(held["voters"]) == ["laya:english", "ep", "mean", "stack"]
    assert set(held["voters"]["stack"]) == set(held["voters"]["laya:english"])
    assert held["voters"]["stack"]["latency_ms"] is None
    assert [(pair["a"], pair["b"]) for pair in held["agreement"]] == [("laya:english", "ep")]
    block = held["stack"]
    assert block["over"] == ["laya:english", "ep"] and len(block["weights"]) == 3
    assert block["vs_best"]["best_single"] in ("laya:english", "ep") and isinstance(block["vs_best"]["win"], bool)
    assert {(m["a"], m["a_wrong"], m["b_right"]) for m in block["mistakes"]} == {("laya:english", 3, 3), ("ep", 1, 1)}
    assert set(held["p"][keys[0]]) == {"laya:english", "ep", "mean", "stack"}
    assert "skipped" in report["sets"]["golden"]["stack"]  # two golden rows cannot fold
    assert "stack" not in report["sets"]["golden"]["voters"]
    raw = path.read_text()
    for text in (*open_texts, *done_texts):
        assert text not in raw
    lines = "\n".join(compare.report_lines(report))
    assert "stack vs " in lines and "laya:english wrong 3 → ep right 3" in lines and "stack skipped:" in lines


def test_a_stack_or_mean_endpoint_name_is_refused():
    for spec in ("stack=http://127.0.0.1:1", "mean=http://127.0.0.1:1", "floor=http://127.0.0.1:1"):
        with pytest.raises(compare.Refused):
            compare.parse_spec(spec)
