"""The golden suites: well-formed, balanced, scored against the SERVED question, and never operator-shaped."""

from __future__ import annotations

import json
import os
import stat

import pytest

from laya_lab import golden, infer
from laya_lab.golden import GOLDEN_DIR, SUITES, eval_rows, load_cases, load_suites, score
from laya_lab.registry import register
from laya_bridge import lab_files, ops, paths, rows
from tldrack import open_work

from conftest import CHECKPOINT, FINE_TUNE, SURFACE, FakeRouter, add_provisional_case


def test_agent_open_work_cases_are_well_formed_and_balanced():
    cases = load_cases(GOLDEN_DIR / "agent-open-work.jsonl")
    assert len(cases) >= 200
    opened = sum(case["expected"] for case in cases)
    assert opened == len(cases) - opened


def test_the_agent_written_batch_is_balanced_stratified_and_sampled_by_the_rule():
    """`lab_files.BATCH_TAG`: batch-2 is every agent-written case, balanced, and its sample spans every stratum."""
    cases = load_cases(GOLDEN_DIR / "agent-open-work.jsonl")
    batch = [case for case in cases if lab_files.case_batch(case) == "batch-2"]
    assert len(batch) == 166 and sum(case["expected"] for case in batch) == 83
    assert {lab_files.case_batch(case) for case in cases} == {None, "batch-2"}
    assert all(lab_files.case_batch(case) or not lab_files.is_provisional(case) for case in cases), "provisional means batched"
    sample = lab_files.batch_sample("batch-2", batch)
    assert len(sample) == lab_files.BATCH_SAMPLE == 40 and sample == lab_files.batch_sample("batch-2", list(reversed(batch)))
    strata = {lab_files._stratum(case) for case in batch if case["id"] in sample}
    assert {(True, "failing-tests"), (True, "waiting"), (False, "caveat"), (False, "clean")} <= strata
    # settled: the operator accepted the sample, so the file carries the sign-off and an EMPTY store reads it accepted
    assert not any(map(lab_files.is_provisional, cases))
    assert lab_files.batch_standing(cases, {})["batch-2"] | {"sample": None} == {
        "size": 166,
        "sample": None,
        "agreed": 40,
        "defects": [],
        "state": "accepted",
        "bound": 10,
    }


def test_the_public_suite_fits_the_verifier_card():
    """The card carries the suite verbatim; past its bound the card would be refused (`card_files.FILE_MAX_CHARS`)."""
    from laya_bridge import card_files, files

    text = (GOLDEN_DIR / "agent-open-work.jsonl").read_text(encoding="utf-8")
    assert files.utf16_len(text) <= card_files.FILE_MAX_CHARS


def test_the_public_suite_names_no_host_and_quotes_no_one():
    """Invented, neutral text: the batch writers' word list, read over every case."""
    import re

    banned = re.compile(r"(?i)\b(?:rack|laya|tldrack|operator|voidcraf[t]|worlds?|entity|entities)\b|/Users/|~/")
    for case in load_cases(GOLDEN_DIR / "agent-open-work.jsonl"):
        for text in (case["state"]["hand_off"], case["state"].get("request", ""), case["why"]):
            assert not banned.search(text), f"{case['id']}: {banned.search(text).group()}"


def test_batch_bound_is_the_hypergeometric_zero_defect_limit():
    assert lab_files.batch_bound(166, 40) == 10 and lab_files.batch_bound(40, 40) == 0
    # at D = 11 a clean sample of 40 is drawn under 5% of the time; at D = 10 it is not
    from math import comb

    assert comb(166 - 11, 40) / comb(166, 40) <= 0.05 < comb(166 - 10, 40) / comb(166, 40)


def test_settle_drops_provisional_only_from_signed_off_lines(lab, public_golden, capsys):
    from conftest import add_provisional_case

    add_provisional_case(public_golden, "case-agreed")
    add_provisional_case(public_golden, "case-waits")
    path = public_golden
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"The docs pair is still missing; the button itself works."', '"Still missing: the docs."', 1
        ),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8").splitlines()
    assert golden.settle("agent-open-work", lab.base) == 3, "nothing signed off: nothing to settle"
    lab.operator("golden:case-agreed", "agree", True, by="golden", stratum="golden")
    assert golden.settle("agent-open-work", lab.base) == 0 and path.read_text(encoding="utf-8").splitlines() == before, "dry run"
    assert golden.settle("agent-open-work", lab.base, apply=True) == 0
    after = path.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(after) == len(before) and len(changed) == 1 and '"case-agreed"' in changed[0][1]
    public = load_cases(path)
    assert [c["id"] for c in public if lab_files.is_provisional(c)] == ["case-waits"]
    assert "1 signed-off cases to settle" in capsys.readouterr().out


def test_eval_rows_carry_the_question_the_bridge_asks():
    suite = SUITES["agent-open-work"]
    rows_out = eval_rows(suite, load_cases(GOLDEN_DIR / "agent-open-work.jsonl"))
    assert all(row["questions"]["q"] is open_work.OPEN_WORK_QUESTION for row in rows_out)
    assert rows_out[0]["state"] == open_work.decide_state(
        load_cases(GOLDEN_DIR / "agent-open-work.jsonl")[0]["state"]["hand_off"]
    )
    assert suite.threshold == open_work.OPEN_WORK_AT
    assert suite.false_warning_cost == open_work.FALSE_WARNING_COST
    assert suite.surface == SURFACE and suite.fine_tune == open_work.FINE_TUNE_MODEL


def test_the_public_suite_is_the_one_the_plugin_reads():
    assert GOLDEN_DIR == paths.PUBLIC_GOLDEN_DIR


def test_score_counts_false_warnings_and_missed_open_work():
    cases = [{"id": "a", "expected": True}, {"id": "b", "expected": False}, {"id": "c", "expected": True}]
    report = score(cases, [0.9, 0.7, 0.2], threshold=0.6)
    assert report["at_threshold"] == {
        "threshold": 0.6,
        "accuracy": 0.333,
        "false_warnings": 1,
        "missed_open": 1,
        "tp": 1,
        "tn": 0,
        "kappa": -0.5,
    }
    assert [miss["id"] for miss in report["misses"]] == ["b", "c"]
    assert report["brier"] == round((0.01 + 0.49 + 0.64) / 3, 4)
    assert report["kappa"] == -0.5
    assert all("kappa" in entry for entry in report["sweep"])
    assert report["ece"] is not None and report["trust"]["kappa_ci"] is None, "3 cases: no interval below 20 pairs"
    assert [b["cuts"] for b in report["reliability"]] == [[], [0.6], []], "no ✅ cut given: bins around the ⚠️ one only"
    with_done = score(cases, [0.9, 0.7, 0.2], threshold=0.6, done_threshold=0.4)
    assert [b["cuts"] for b in with_done["reliability"]] == [[], [0.4], [0.6], []]


# -- the public-suite pin: an operator row can never be committed --------------------

PUBLIC_FIELDS = {"id", "expected", "state", "tags", "why"}


def public_rows():
    for path in sorted(GOLDEN_DIR.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() and not line.strip().startswith("#"):
                yield f"{path.name}:{lineno}", json.loads(line)


def test_every_public_row_is_hand_written_shape_never_a_operator_case():
    found = list(public_rows())
    assert found, "the public suites must be found for this pin to mean anything"
    for where, row in found:
        assert set(row) == PUBLIC_FIELDS, f"{where}: fields {sorted(row)}"
        assert set(row["state"]) <= {"hand_off", "request"}, f"{where}: state {sorted(row['state'])}"
        assert "operator" not in row["tags"], f"{where}: an operator tag belongs in the PRIVATE suite"
        assert not row["id"].startswith("f-"), f"{where}: an f- id is a promoted operator case"
    assert not list(GOLDEN_DIR.glob("*.private.jsonl")), "a private suite never lives in the repo"


def test_the_pin_refuses_what_promote_writes():
    case = lab_files.private_golden_case("0123456789abcdef", "synthetic", True, False, "sonnet", "2026-10-27T00:00:00Z")
    assert set(case) != PUBLIC_FIELDS and case["id"].startswith("f-") and "operator" in case["tags"]


# -- private suite + reports ----------------------------------------------------------


def private_suite(lab, cases):
    path = paths.private_golden_path(lab.base, "agent-open-work")
    lab.write_jsonl(path.relative_to(lab.base).as_posix(), cases, header=lab_files.PRIVATE_GOLDEN_HEADER)
    return path


def operator_case(text, expected=True):
    return lab_files.private_golden_case(rows.state_key(text), text, expected, not expected, "sonnet", "2026-10-27T00:00:00Z")


def test_load_suites_reads_the_private_suite_and_refuses_a_repeat(lab, public_golden):
    public, private = load_suites(lab.base, "agent-open-work")
    assert [c["id"] for c in public] == ["case-open", "case-done"] and private == []
    private_suite(lab, [operator_case("a synthetic operator flip")])
    assert len(load_suites(lab.base, "agent-open-work")[1]) == 1
    private_suite(lab, [operator_case("Phase C is not started.")])  # the public case-open's text
    with pytest.raises(ValueError, match="repeats the text"):
        load_suites(lab.base, "agent-open-work")


def test_run_writes_a_private_golden_report_the_plugin_reads(lab, public_golden, umask_022):
    trained_text = "a synthetic operator flip the fine-tune trained on"
    folder = lab.checkpoint(train_texts=[trained_text])
    register(lab.base, SURFACE, folder, {"eval_items": 1})
    private_suite(lab, [operator_case("a synthetic held-out operator flip", True), operator_case(trained_text, False)])
    router = FakeRouter(lambda state: 0.9 if "Phase C" in state or "held-out" in state else 0.1)
    assert golden.run("agent-open-work", lab.base, router=router) == 0
    assert {model for _state, model in router.calls} == {"voidcraft-agent-open-work"}

    found = lab_files.newest_report(lab.base, "agent-open-work", "voidcraft-agent-open-work", "golden")
    assert found is not None
    path, report = found
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert report["kind"] == "golden" and report["at"].endswith("Z") and report["threshold_source"] == "code"
    assert report["checkpoint"] == {
        "name": CHECKPOINT,
        "path": f"checkpoints/{CHECKPOINT}",
        "revision": None,
        "model_id": f"laya-local/voidcraft-agent-open-work@{CHECKPOINT}",
        "registered": True,
        "digest": infer.checkpoint_digest(FINE_TUNE, folder),
    }
    assert report["checkpoint"]["digest"].startswith("sha256:") and report["device"] == "mps"
    assert report["dataset_version"].startswith(f"q{golden.SUITES['agent-open-work'].question_version}.g")
    assert report["at_threshold"]["accuracy"] == 1.0 and report["kappa"] == 1.0
    assert report["private"]["contaminated"] == [f"f-{rows.state_key(trained_text)}"]
    assert report["private"]["cases"] == 1 and report["private"]["accuracy"] == 1.0
    assert trained_text not in path.read_text(encoding="utf-8"), "a report carries ids and keys, never text"
    assert lab_files.golden_view(path, report)["kappa"] == 1.0
    assert ops.ops_state(lab.base, None, [], lab_ok=True)["served"][0]["golden"]["cases"] == 2


def test_all_checkpoints_reports_each_under_one_run(lab, public_golden, capsys):
    registered = lab.checkpoint(train_texts=[])
    register(lab.base, SURFACE, registered, {"eval_items": 1})
    older = "voidcraft-agent-open-work-20261001-0900"
    lab.checkpoint(older, train_texts=[])
    lab.checkpoint("voidcraft-agent-open-work-20261002-0900", weights=False)  # no weights: not loadable
    router = FakeRouter(lambda state: 0.8)
    assert golden.run("agent-open-work", lab.base, all_checkpoints=True, router=router) == 0
    assert [model for _s, model in router.calls[::2]] == ["english", "voidcraft-agent-open-work", older]
    reports = [
        lab_files.newest_report(lab.base, "agent-open-work", model, "golden")[1]
        for model in ("english", "voidcraft-agent-open-work", older)
    ]
    assert len({report["run_id"] for report in reports}) == 1
    assert [report["checkpoint"]["registered"] for report in reports] == [False, True, False]
    out = capsys.readouterr().out
    assert "acc@cut" in out and "base" in out and "ECE" in out
    # ECE per checkpoint: every checkpoint says 0.8 on one open and one done case, a 0.3 gap in the one bin it fills
    assert [report["ece"] for report in reports] == [0.3, 0.3, 0.3]
    assert all([b["cuts"] for b in report["reliability"]] == [[], [0.4], [], [0.7], []] for report in reports)


def test_a_provisional_case_is_scored_apart_until_the_operator_agrees_it(lab, public_golden, capsys):
    """`lab_files.PROVISIONAL_TAG`: an agent-written public case is out of the headline until its AGREE."""
    add_provisional_case(public_golden)
    router = FakeRouter(lambda state: 0.9 if "Phase C" in state else 0.1)  # misses only the provisional case
    assert golden.run("agent-open-work", lab.base, router=router) == 0
    report = lab_files.newest_report(lab.base, "agent-open-work", "english", "golden")[1]
    assert report["cases"] == 2 and report["misses"] == [] and report["at_threshold"]["accuracy"] == 1.0
    assert report["provisional"] == {"cases": 1, "ids": ["case-new"], "misses": [{"id": "case-new", "expected": True, "p": 0.1}]}
    assert "case-new" in report["probabilities"], "scored, so the deck can show its p"
    out = capsys.readouterr().out
    assert "2 public + 0 private cases · 1 provisional" in out and "provisional: 1 cases" in out
    public = load_suites(lab.base, "agent-open-work")[0]
    assert golden.counted_public(lab.base, SURFACE, public)[1] == ["case-new"]
    lab.operator("golden:case-new", "agree", True, by="golden", stratum="golden")
    counted, held = golden.counted_public(lab.base, SURFACE, public)
    assert [c["id"] for c in counted] == ["case-open", "case-done", "case-new"] and held == []


def test_operator_disputes_name_flipped_public_cases(lab, public_golden):
    lab.operator("golden:case-open", "flip", True, by="golden", stratum="golden")
    lab.operator("golden:case-done", "agree", False, by="golden", stratum="golden")
    assert golden.operator_disputes(lab.base, SURFACE) == ["case-open"]


def test_an_eval_run_finds_its_own_golden_report_and_reads_a_verdict(lab, public_golden, monkeypatch, capsys):
    """A scheduler names its run in `lab_files.EVAL_RUN_ENV`; the report carries it, and `laya-lab verdict` reads THAT report."""
    from laya_lab import cli

    router = FakeRouter(lambda state: 0.9 if "Phase C" in state else 0.1)
    monkeypatch.delenv(lab_files.EVAL_RUN_ENV, raising=False)
    assert golden.run("agent-open-work", lab.base, router=router) == 0  # a terminal run: no eval run to name
    _path, report = lab_files.newest_report(lab.base, "agent-open-work", "english", "golden")
    assert report["eval_run"] is None
    verdict, why = lab_files.run_verdict(lab.base, "golden", "agent-open-work")
    assert verdict is None and lab_files.EVAL_RUN_ENV in why

    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "ab12cd34ef56")
    assert lab_files.run_verdict(lab.base, "golden", "agent-open-work")[0] is None, "an older report never answers"
    assert golden.run("agent-open-work", lab.base, router=router, device="cpu") == 0
    verdict, _why = lab_files.run_verdict(lab.base, "golden", "agent-open-work")
    _path, report = lab_files.newest_report(lab.base, "agent-open-work", "english", "golden")
    assert verdict == {
        "verdict": "PASS",
        "headline": {"cases": 2, "accuracy": 1.0, "kappa": 1.0, "brier": 0.01, "ece": 0.1},
        "model_digest": infer.checkpoint_digest("english", None),
        "dataset_version": report["dataset_version"],
        "accelerator": "cpu",
    }

    monkeypatch.setenv(paths.DATA_ENV, str(lab.base.parent))
    capsys.readouterr()
    assert cli.main(["verdict", "golden", "agent-open-work"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == verdict
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "000000000000")
    assert cli.main(["verdict", "golden", "agent-open-work"]) == 1
    assert "carries eval run 000000000000" in capsys.readouterr().err
