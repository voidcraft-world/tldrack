"""`laya-lab question-challenger`: the served rows, labels and split, each state rebuilt at another question version; and
the bench asking such a challenger its own version of the same rows.

Every text here is invented for the test. No model loads.
"""

from __future__ import annotations

import json

import pytest

from laya_lab import bench, question
from laya_bridge import rows
from tldrack import open_work

from conftest import CHECKPOINT, SUITE, FakeRouter, synthetic

SUITE_SPEC = bench.SUITES[SUITE]


@pytest.fixture(autouse=True)
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "head_commit", lambda root: None)


def served_record(lab, train: list[str], held: list[str]) -> None:
    """A served fine-tune whose record holds version-1 rows (the state IS the text), as the first ones did."""
    lab.trained_on(train)
    record = lambda texts: [{"state": text, "questions": {}, "expected": {"q": "open" in text}} for text in texts]
    lab.write_jsonl(f"checkpoints/{CHECKPOINT}.data/train.jsonl", record(train))
    lab.write_jsonl(f"checkpoints/{CHECKPOINT}.data/eval.jsonl", record(held))


def requests_file(lab, requests: dict[str, str | None]) -> None:
    lab.write_jsonl(
        "data/agent-open-work-requests.jsonl",
        [{"key": rows.state_key(text), "request": request} for text, request in requests.items()],
    )


def test_a_rebuilt_record_keeps_every_row_its_label_and_its_side_and_reads_each_turns_request(lab):
    train = synthetic("still-open", 3) + synthetic("all-done", 3)
    held = synthetic("open-held", 1) + synthetic("done-held", 1)
    served_record(lab, train, held)
    requests_file(lab, {train[0]: "Ship all three phases.", train[1]: None, held[0]: "Finish the migration."})

    built = question.rebuild_record(lab.base, 3)
    assert built.like == CHECKPOINT and built.stats == {"train": 6, "eval": 2, "train_requests": 1, "eval_requests": 1}
    assert [row["key"] for row in built.train_rows] == [rows.state_key(text) for text in train]
    assert [row["expected"] for row in built.train_rows] == [{"q": "open" in text} for text in train]
    assert built.train_rows[0]["state"] == open_work.decide_state(train[0], "Ship all three phases.", version=3)
    assert built.train_rows[1]["state"] == train[1], "a turn nobody typed a request for reads as version 2"
    assert built.eval_rows[0]["state"].startswith(open_work.REQUEST_LABEL + "Finish the migration.")
    assert all(row["questions"] == {"q": open_work.OPEN_WORK_QUESTION} for row in built.train_rows + built.eval_rows)

    control = question.rebuild_record(lab.base, 2)
    assert [row["state"] for row in control.train_rows] == [open_work.decide_state(text) for text in train]
    assert control.stats["train_requests"] == control.stats["eval_requests"] == 0


def test_a_version_with_no_builder_is_refused_before_anything_is_read(lab, capsys):
    assert question.run(lab.base, 9, epochs=2, device="cpu") == question.REFUSED
    assert "no state builder" in capsys.readouterr().out


def test_the_bench_asks_a_challenger_its_own_version_of_the_same_rows(lab, public_golden, tmp_path, capsys):
    """Golden cases carry their request; the challenger is asked v3 states, the served model v2 ones, of one row set."""
    cases = [json.loads(line) for line in public_golden.read_text().splitlines() if line.startswith("{")]
    cases[0]["state"]["request"] = "Build phases A, B and C."
    public_golden.write_text("".join(json.dumps(case) + "\n" for case in cases))
    train = synthetic("trained-open", 12) + synthetic("trained-done", 12)
    served_record(lab, train, synthetic("held-open", 2))
    challenger = lab.checkpoint(f"{CHECKPOINT[:-4]}2000", train_texts=train)
    (challenger.parent / f"{challenger.name}.data" / question.QUESTION_FILE).write_text(json.dumps({"question_version": 3}))
    router = FakeRouter(lambda state: 0.9 if "not started" in state.lower() or "request:" in state.lower() else 0.1)

    assert bench.run(SUITE, lab.base, router=router, out_dir=tmp_path / "out", challengers=[challenger.name]) == 0
    payload = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    roster = {voter["name"]: voter for voter in payload["voters"]}
    assert list(roster) == [bench.BASE, bench.FINE_TUNE, bench.FLOOR, "laya-fine-tune-q3"]
    assert roster["laya-fine-tune-q3"] == {
        "name": "laya-fine-tune-q3",
        "label": "LAYA · FINE-TUNE · Q3",
        "kind": "fine_tune",
        "of": bench.BASE,
        "labels": 0,
        "params_m": bench.LAYA_PARAMS_M,
        "runtime": "mps",
        "licence": None,
        "served": False,
    }
    assert payload["protocol"]["description"].endswith(
        "each challenger is asked of the same rows: laya-fine-tune-q3 question v3."
    )
    assert payload["protocol"]["question_version"] == open_work.QUESTION_VERSION
    asked = {(state, model) for state, model in router.calls}
    requested = open_work.decide_state(cases[0]["state"]["hand_off"], "Build phases A, B and C.", version=3)
    assert (requested, challenger.name) in asked and (cases[0]["state"]["hand_off"], "voidcraft-agent-open-work") in asked
    assert "Build phases A, B and C." not in json.dumps(payload), "a request never reaches the file"
    assert not any(name.startswith("mean.") for name in roster), "a challenger brings no mean"


def test_a_challenger_whose_record_says_no_version_is_refused(lab, public_golden, tmp_path, capsys):
    served_record(lab, synthetic("trained-open", 2), synthetic("held-open", 1))
    other = lab.checkpoint(f"{CHECKPOINT[:-4]}2000", train_texts=[])
    assert (
        bench.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5), out_dir=tmp_path / "out", challengers=[other.name])
        == bench.REFUSED
    )
    assert "says no question version" in capsys.readouterr().out


def test_a_paired_delta_reads_the_same_draws_as_every_voters_interval():
    expected = [True, True, True, False, False, False] * 4
    better = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1] * 4
    worse = [0.6, 0.4, 0.7, 0.5, 0.2, 0.6] * 4
    point, lo, hi = bench.paired_auc_delta(better, worse, expected)
    assert point > 0 and lo > 0 and lo <= point <= hi
    assert bench.paired_auc_delta(better, better, expected) == (0.0, 0.0, 0.0)
    assert bench.paired_auc_delta(better, worse, [True] * 24) is None


def test_a_retrain_records_its_version_and_the_bench_names_it_apart(lab, public_golden, tmp_path):
    """`train` writes the question record a retrain needs to be benched, and the bench never calls it a refit."""
    from laya_lab import train

    rows_in = [
        {"key": f"k{i}", "state": text, "questions": {"q": open_work.OPEN_WORK_QUESTION}, "expected": {"q": "open" in text}}
        for i, text in enumerate(synthetic("retrain-open", 40) + synthetic("retrain-done", 40))
    ]
    dataset = lab.write_jsonl("data/retrain.jsonl", rows_in)
    plan = train.prepare(SUITE_SPEC.surface, lab.base, dataset, epochs=2, device="cpu")
    record = json.loads((plan.work / question.QUESTION_FILE).read_text())
    assert record == {
        "question_version": open_work.QUESTION_VERSION,
        "retrain": True,
        "train": plan.train_rows,
        "eval": plan.held_out,
    }
    assert question.read_question(lab.base, plan.out.name) == open_work.QUESTION_VERSION

    served_record(lab, synthetic("trained-open", 2), synthetic("held-open", 1))
    retrained = lab.checkpoint(f"{CHECKPOINT[:-4]}2000", train_texts=[])
    question.write_record(retrained.parent / f"{retrained.name}.data", open_work.QUESTION_VERSION, retrain=True)
    assert (
        bench.run(SUITE, lab.base, router=FakeRouter(lambda state: 0.5), out_dir=tmp_path / "out", challengers=[retrained.name])
        == 0
    )
    payload = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    roster = {voter["name"]: voter for voter in payload["voters"]}
    name = f"{bench.RETRAIN_PREFIX}{open_work.QUESTION_VERSION}"
    assert roster[name]["label"] == f"LAYA · RETRAIN · Q{open_work.QUESTION_VERSION}"
    assert payload["protocol"]["description"].endswith(
        f"each retrain is asked of the same rows: {name} question v{open_work.QUESTION_VERSION}."
    )
    assert "refitted" not in payload["protocol"]["description"]

    rebuilt = lab.checkpoint(f"{CHECKPOINT[:-4]}2100", train_texts=[])
    question.write_record(rebuilt.parent / f"{rebuilt.name}.data", 3, like=retrained.name)
    assert (
        bench.run(
            SUITE,
            lab.base,
            router=FakeRouter(lambda state: 0.5),
            out_dir=tmp_path / "again",
            challengers=[retrained.name, rebuilt.name],
        )
        == 0
    )
    payload = json.loads(next((tmp_path / "again").glob("*.json")).read_text())
    assert [voter["label"] for voter in payload["voters"]][-2:] == [
        f"LAYA · RETRAIN · Q{open_work.QUESTION_VERSION}",
        "LAYA · RETRAIN · Q3",
    ], "a rebuild of a retrain is the retrain's"
