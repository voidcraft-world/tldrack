"""The panel voter: the parts of a turn it asks, who answers which question, and a line no row ever scores itself with.

Every text and number here is invented. No model loads: the router is `conftest.FakeRouter`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from laya_lab import bench, panel
from tldrack import open_work

from conftest import SUITE, FakeRouter, synthetic

QUESTION = bench.SUITES[SUITE].question
SIZE = open_work.STATE_MAX_CHARS


class ByQuestion:
    """A router whose answer depends on the model and the question, so a test can tell who was asked what."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def predict(self, state, questions: dict, model: str | None = None) -> dict:
        (question,) = questions.values()
        self.calls.append((model or "", question["instructions"], state))
        served = question["instructions"] == QUESTION["instructions"]
        return {
            "answers": {name: {"noul": 0.9 if served and "MIDDLE" in state else 0.2 if served else 0.6} for name in questions}
        }


# ── the parts of a turn ───────────────────────────────────────────────────────


def test_a_closing_that_fits_one_window_has_no_windows():
    assert panel.windows("done, tests pass") == []
    assert panel.windows("x" * SIZE) == []


def test_a_long_closing_is_read_whole_by_its_windows():
    text = "".join(f"{i:05d} " for i in range(SIZE))  # every six characters name their own position
    found = panel.windows(text)
    assert len(found) > 1 and all(len(window) == SIZE for window in found)
    assert found[0] == text[:SIZE] and found[-1] == text[-SIZE:]
    for a, b in zip(found, found[1:]):
        assert text.index(b) - text.index(a) <= panel.WINDOW_STEP  # no gap between two windows
    middle = text[len(text) // 2 : len(text) // 2 + 60]
    assert any(middle in window for window in found)
    assert middle not in open_work.fit_hand_off(text)  # what the served state never reads


def test_fenced_code_is_collapsed_before_a_closing_is_windowed():
    text = "summary\n```\n" + "code\n" * SIZE + "```\nall done"
    assert panel.windows(text) == []  # collapsed, it fits one window: the served state holds it whole


def test_a_request_is_flattened_and_kept_to_one_window():
    assert panel.request_state(None) is None and panel.request_state("") is None
    assert panel.request_state("fix   the\n\nbuild") == "fix the build"
    assert len(panel.request_state("word " * SIZE)) == SIZE


def test_the_signals_are_read_off_the_text():
    found = panel.signals({"hand_off": "x" * (panel.LENGTH_FULL * 2) + "\nshall I go on?", "request": "why does it fail?"})
    assert {name: found[name] for name in panel.TEXT_SIGNALS} == {
        "has_request": 1.0,
        "request_asks": 1.0,
        "closing_asks": 1.0,
        "length": 1.0,
        "code": 0.0,
    }
    bare = panel.signals({"hand_off": "```\nls\n```\ndone.", "request": None})
    assert (bare["has_request"], bare["request_asks"], bare["closing_asks"], bare["code"]) == (0.0, 0.0, 0.0, 1.0)
    assert set(found) == set(panel.SIGNALS)


def test_the_turn_s_facts_are_inputs_and_a_turn_with_none_reads_zero():
    facts = {"ran_tests": True, "tests_passed": False, "edited_files": 25, "made_commit": True, "opened_pr": False}
    found = panel.signals({"hand_off": "all done", "request": None, "facts": facts})
    assert {name: found[name] for name in panel.FACT_SIGNALS} == {
        "has_facts": 1.0,
        "ran_tests": 1.0,
        "tests_passed": 0.0,
        "tests_failed": 1.0,
        "edited_files": 1.0,
        "made_commit": 1.0,
        "opened_pr": 0.0,
    }
    unknown = panel.signals({"hand_off": "all done", "facts": {**facts, "tests_passed": None, "edited_files": 5}})
    assert (unknown["tests_passed"], unknown["tests_failed"], unknown["edited_files"]) == (0.0, 0.0, 0.5)
    for raw in ({"hand_off": "all done"}, {"hand_off": "all done", "facts": {"ran_tests": "yes"}}):  # none, or unreadable
        assert all(panel.signals(raw)[name] == 0.0 for name in panel.FACT_SIGNALS)


def test_the_panel_holds_the_served_question_once_and_names_every_feature_once():
    assert [ask.name for ask in panel.ASKS if ask.instructions is None and ask.over is None] == [panel.SERVED]
    assert len(set(panel.FEATURES)) == len(panel.FEATURES) == len(panel.ASKS) + len(panel.SIGNALS)
    by_name = {ask.name: ask for ask in panel.ASKS}
    for ask in panel.ASKS:
        assert (ask.part == panel.WINDOWS) == (ask.over is not None)
        if ask.over:
            assert by_name[ask.over].part == panel.CLOSING
        elif ask.name != panel.SERVED:
            assert ask.instructions.endswith("?") and len(ask.instructions) < 90  # one short question


# ── asking ────────────────────────────────────────────────────────────────────


def test_the_served_model_answers_the_served_question_and_the_others_the_rest():
    router = ByQuestion()
    raw = {"hand_off": "all done", "request": "fix the build"}
    row, ms = panel.answer_row(panel.Asker(lambda: router), QUESTION, "all done", raw, "fine-tune", "english")
    assert len(row) == len(panel.FEATURES) and ms >= 0
    asked_by = {}
    for model, instructions, _state in router.calls:
        asked_by.setdefault(instructions, set()).add(model)
    assert asked_by.pop(QUESTION["instructions"]) == {"fine-tune"}
    assert all(models == {"english"} for models in asked_by.values()) and len(asked_by) == len(panel.ASKS) - 3
    states = {instructions: state for _model, instructions, state in router.calls}
    assert states["Is this message asking a question?"] == "fix the build" and states[QUESTION["instructions"]] == "all done"


def test_a_turn_nobody_typed_asks_nothing_of_a_request():
    router = ByQuestion()
    row, _ms = panel.answer_row(
        panel.Asker(lambda: router), QUESTION, "all done", {"hand_off": "all done", "request": None}, "m", "m"
    )
    features = dict(zip(panel.FEATURES, row))
    assert all(features[ask.name] == 0.0 for ask in panel.ASKS if ask.part == panel.REQUEST)
    assert features["has_request"] == 0.0
    assert not any("asking" in instructions for _model, instructions, _state in router.calls)


def test_a_windows_ask_keeps_the_highest_answer_of_the_whole_closing():
    """The served state drops a long closing's middle; the windows read it, and their highest answer is the feature."""
    hand_off = "a" * SIZE + " MIDDLE " + "b" * SIZE
    closing = open_work.fit_hand_off(hand_off)
    assert "MIDDLE" not in closing
    row, _ms = panel.answer_row(
        panel.Asker(lambda: ByQuestion()), QUESTION, closing, {"hand_off": hand_off, "request": None}, "m", "m"
    )
    features = dict(zip(panel.FEATURES, row))
    assert features[panel.SERVED] == pytest.approx(panel.logit(0.2))
    assert features["open_any"] == pytest.approx(panel.logit(0.9))


def test_one_question_of_one_state_is_asked_once_a_run():
    router = FakeRouter(lambda state: 0.7)
    asker = panel.Asker(lambda: router)
    first = asker.ask("m", QUESTION, "the same state")
    assert asker.ask("m", QUESTION, "the same state") == first
    assert len(router.calls) == 2  # the dropped cold call, then the kept one
    asker.ask("m", QUESTION, "another state")
    assert len(router.calls) == 3  # warm now: one call


# ── the line ──────────────────────────────────────────────────────────────────


def rows_of(n: int, seed: int = 7) -> tuple[list[str], np.ndarray, list[bool]]:
    """`n` rows whose first feature carries the label and whose others are noise."""
    rng = np.random.default_rng(seed)
    labels = [i % 2 == 0 for i in range(n)]
    matrix = rng.normal(size=(n, len(panel.FEATURES)))
    matrix[:, 0] = [1.5 if label else -1.5 for label in labels] + rng.normal(scale=0.5, size=n)
    return [f"row-{i}" for i in range(n)], matrix, labels


def test_the_line_leans_on_the_feature_that_carries_the_label():
    _ids, matrix, labels = rows_of(120)
    line = panel.fit(matrix, labels)
    weights = np.abs(line.weights[:-1])
    assert int(np.argmax(weights)) == 0 and line.weights[0] > 0
    assert panel.weight_lines(line)[0].split()[0] == panel.FEATURES[0]


def test_a_feature_that_never_varies_takes_no_weight():
    _ids, matrix, labels = rows_of(60)
    matrix[:, 3] = 4.0
    line = panel.fit(matrix, labels)
    assert line.weights[3] == 0.0 and all(np.isfinite(line.predict(matrix)))


def test_no_row_is_scored_by_a_line_that_saw_its_label():
    ids, matrix, labels = rows_of(80)
    before = panel.cross_fit(ids, matrix, labels)
    flipped = list(labels)
    flipped[11] = not flipped[11]
    after = panel.cross_fit(ids, matrix, flipped)
    fold = panel.fold_of(ids[11])
    for i, row_id in enumerate(ids):
        if panel.fold_of(row_id) == fold:
            assert after[i] == before[i]  # its own fold's line never read the flipped label
    assert any(after[i] != before[i] for i, row_id in enumerate(ids) if panel.fold_of(row_id) != fold)


def test_rows_that_cannot_be_folded_are_refused_with_numbers_only():
    ids, matrix, labels = rows_of(8)
    assert "8 held-out rows" in panel.check(ids, labels)
    ids, matrix, labels = rows_of(40)
    assert "cannot fit a two-class panel" in panel.check(ids, [True] * 40)
    assert "share an id" in panel.check(["same"] * 40, labels)
    with pytest.raises(ValueError, match="share an id"):
        panel.cross_fit(["same"] * 40, matrix, labels)


def test_the_set_it_is_fitted_on_is_scored_out_of_fold_and_the_others_by_the_whole_fit():
    router = FakeRouter(lambda state: 0.9 if "open" in state else 0.1)
    texts = synthetic("still-open", 20) + synthetic("all-done", 20)
    held = ([f"h{i}" for i in range(40)], [{"hand_off": t, "request": None} for t in texts], texts, [i < 20 for i in range(40)])
    unseen = [synthetic("still-open", 30)[-1], synthetic("all-done", 30)[-1]]  # two texts the fit never read
    other = (["g0", "g1"], [{"hand_off": text, "request": None} for text in unseen], unseen, [True, False])
    found, line = panel.answer_sets(panel.Asker(lambda: router), QUESTION, {"golden": other, "heldout": held}, "m", "m")
    assert len(found["heldout"][0]) == 40 and len(found["heldout"][1]) == 40
    golden_ps, _ms = found["golden"]
    assert golden_ps[0] > 0.5 > golden_ps[1]
    assert line.weights[panel.FEATURES.index(panel.SERVED)] > 0
    with pytest.raises(ValueError, match="this run has none"):
        panel.answer_sets(panel.Asker(lambda: router), QUESTION, {"golden": other}, "m", "m")


# ── in the bench ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "head_commit", lambda root: None)


def written(folder: Path) -> dict:
    (found,) = list(folder.glob("*.json"))
    return json.loads(found.read_text(encoding="utf-8"))


def labelled(lab, n: int) -> None:
    texts = synthetic("still-open", n) + synthetic("all-done", n)
    keys = lab.unlabelled(texts)
    lab.labels({key: i < n for i, key in enumerate(keys)})


def test_a_panel_is_a_voter_of_the_file_fitted_on_the_held_out_rows(lab, public_golden, tmp_path, capsys):
    labelled(lab, 20)
    router = FakeRouter(lambda state: 0.9 if "open" in state.lower() or "not started" in state.lower() else 0.1)
    assert bench.run(SUITE, lab.base, router=router, out_dir=tmp_path / "out", panels=["served", "base"]) == 0
    payload = written(tmp_path / "out")
    roster = {voter["name"]: voter for voter in payload["voters"]}
    assert list(roster) == [bench.BASE, panel.NAME]  # no fine-tune is served: the base panel is the same panel
    assert (roster[panel.NAME]["kind"], roster[panel.NAME]["of"], roster[panel.NAME]["labels"]) == ("panel", bench.BASE, None)
    held = next(block for block in payload["sets"] if block["set"] == "heldout")
    entry = next(score for score in held["scores"] if score["voter"] == panel.NAME)
    assert entry["auc"]["value"] > 0.9 and entry["operating"]["source"] == "swept" and entry["latency"] is not None
    assert f"asks {len(panel.ASKS)} questions" in payload["protocol"]["description"]
    out = capsys.readouterr().out
    assert f"skipped  {panel.NAME}-base" in out and f"verdict  {panel.NAME} vs {bench.BASE}" in out
    assert f"{panel.NAME} weights" in out


def test_a_voter_that_ranks_worse_past_the_interval_is_said_to_trail(lab, public_golden, tmp_path, capsys):
    """A measured no has two shapes, and the verdict line says which: inside the interval, or below it."""
    labelled(lab, 20)
    served = bench.BenchVoter(
        "served",
        "SERVED",
        "fine_tune",
        "none",
        open=lambda: lambda state: 0.9 if "open" in state else 0.1,
        threshold=0.7,
        labels=0,
        served=True,
    )
    backwards = bench.BenchVoter(
        "backwards",
        "BACKWARDS",
        "panel",
        "none",
        whole=lambda sets: {
            name: bench.Answers([0.1 if expected else 0.9 for _id, _state, expected in data], []) for name, data in sets.items()
        },
    )
    assert bench.run(SUITE, lab.base, plan=bench.Plan([served, backwards]), out_dir=tmp_path / "out") == 0
    assert "measured no (held-out AUC below the served one past the interval" in capsys.readouterr().out


def test_a_store_with_no_held_out_rows_has_no_panel(lab, public_golden, tmp_path, capsys):
    router = FakeRouter(lambda state: 0.5)
    assert bench.run(SUITE, lab.base, router=router, out_dir=tmp_path / "out", panels=["served"]) == 0
    assert [voter["name"] for voter in written(tmp_path / "out")["voters"]] == [bench.BASE]
    assert f"skipped  {panel.NAME}: it is fitted on the heldout rows, and this run has none" in capsys.readouterr().out


def test_a_panel_the_bench_does_not_know_is_refused(lab, public_golden, tmp_path, capsys):
    assert bench.run(SUITE, lab.base, out_dir=tmp_path / "out", panels=["mica"], dry_run=True) == bench.REFUSED
    assert "--panel 'mica'" in capsys.readouterr().out
