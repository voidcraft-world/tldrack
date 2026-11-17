"""Every laya-lab → plugin file format: each builder round-trips through its reader."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from laya_bridge import lab_files, paths, rows

SUITE = "agent-open-work"


def write_report(tree, name, report):
    path = tree.path(f"evals/{name}")
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def todays_golden(model="english", **extra):
    """A golden report in TODAY's shape: no kind, at, run_id, kappa or top-level accuracy."""
    return {
        "cases": 32,
        "at_threshold": {"threshold": 0.7, "accuracy": 0.75, "false_warnings": 7, "missed_open": 1, "tp": 15, "tn": 9},
        "sweep": [{"threshold": 0.3, "accuracy": 0.56, "false_warnings": 14, "missed_open": 0, "tp": 16, "tn": 2}],
        "brier": 0.21,
        "misses": [{"id": "phases-c-not-started", "expected": True, "p": 0.4, "tags": ["open"]}],
        "suite": SUITE,
        "model": model,
        "question_version": 1,
        "revisions": {model: None},
        "probabilities": {"phases-c-not-started": 0.4},
        **extra,
    }


def test_report_names_match_their_patterns():
    stamp = lab_files.report_stamp(datetime(2026, 10, 27, 1, 2, 3, tzinfo=timezone.utc))
    assert stamp == "20261027-010203"
    golden = lab_files.golden_report_name(SUITE, "voidcraft-agent-open-work", stamp)
    assert lab_files.golden_report_pattern(SUITE).fullmatch(golden)["model"] == "voidcraft-agent-open-work"
    heldout = lab_files.heldout_report_name(SUITE, "english", "operator", stamp)
    match = lab_files.heldout_report_pattern(SUITE).fullmatch(heldout)
    assert (match["model"], match["labels"], match["stamp"]) == ("english", "operator", stamp)
    assert lab_files.golden_report_pattern(SUITE).fullmatch(heldout) is None


def test_todays_filenames_are_read():
    pattern = lab_files.golden_report_pattern(SUITE)
    for name, model in (
        ("agent-open-work-english-20260102-010101.json", "english"),
        ("agent-open-work-voidcraft-agent-open-work-20260102-030405.json", "voidcraft-agent-open-work"),
    ):
        assert pattern.fullmatch(name)["model"] == model


def test_newest_report_matches_name_and_the_reports_own_model(tree):
    write_report(tree, "agent-open-work-english-20260102-010101.json", todays_golden())
    write_report(tree, "agent-open-work-english-20260102-010203.json", todays_golden(cases=31))
    write_report(tree, "agent-open-work-english-20261017-000000.json", todays_golden(model="multilingual"))  # renamed
    write_report(tree, "agent-open-work-english-20261018-000000.json", {"model": "english", "kind": "heldout"})
    write_report(
        tree, "agent-open-work-voidcraft-agent-open-work-20261019-000000.json", todays_golden(model="voidcraft-agent-open-work")
    )
    path, report = lab_files.newest_report(tree.base, SUITE, "english", "golden")
    assert path.name == "agent-open-work-english-20260102-010203.json" and report["cases"] == 31
    assert lab_files.newest_report(tree.base, SUITE, "multilingual", "golden") is None
    assert lab_files.newest_report(tree.base, "other-suite", "english", "golden") is None


def test_golden_view_reads_todays_shape():
    path = paths.Path("agent-open-work-english-20260102-010101.json")
    assert lab_files.golden_view(path, todays_golden()) == {
        "at": "2026-01-02T01:01:01",
        "cases": 32,
        "accuracy": 0.75,
        "kappa": None,
        "brier": 0.21,
    }


def test_golden_view_reads_the_new_shape():
    report = {
        **todays_golden(),
        **lab_files.golden_report_fields("2026-10-27T01:02:03Z", "r1", 0.41, {"name": "ck", "path": "checkpoints/ck"}),
    }
    view = lab_files.golden_view(paths.Path("agent-open-work-english-20261027-010203.json"), report)
    assert view == {"at": "2026-10-27T01:02:03Z", "cases": 32, "accuracy": 0.75, "kappa": 0.41, "brier": 0.21}
    facts = lab_files.golden_facts(report)
    assert facts == {
        "threshold": 0.7,
        "question_version": 1,
        "checkpoint": "ck",
        "misses": ["phases-c-not-started"],
        "probabilities": {"phases-c-not-started": 0.4},
    }
    report["at_threshold"]["kappa"] = 0.2
    del report["kappa"]
    assert lab_files.golden_view(paths.Path("x-20261027-010203.json"), report)["kappa"] == 0.2


@pytest.mark.parametrize("voters", [None, [], "invalid", 7])
def test_a_compare_report_whose_voter_scores_are_not_an_object_is_inconclusive(voters):
    report = {
        "voters": [{"name": "laya:served", "kind": "laya"}],
        "sets": {"heldout": {"rows": 12, "voters": voters}},
    }
    assert lab_files._compare_verdict(report) == {"verdict": "INCONCLUSIVE", "headline": {}}
    report["sets"]["heldout"]["voters"] = {"laya:served": {"auc": 0.8}}
    assert lab_files._compare_verdict(report)["verdict"] == "PASS"


def test_golden_view_refuses_a_broken_report():
    assert lab_files.golden_view(paths.Path("x.json"), {"cases": 1}) is None
    assert lab_files.golden_view(paths.Path("a-english-20260102-010101.json"), {**todays_golden(), "brier": True}) is None


def test_heldout_round_trip(tree):
    stamp = "20261027-010203"
    name = lab_files.heldout_report_name(SUITE, "voidcraft-agent-open-work", "operator", stamp)
    head = lab_files.heldout_report_head("2026-10-27T01:02:03Z", SUITE, "voidcraft-agent-open-work", "operator", 48, 0.62, 0.55)
    write_report(tree, name, {**head, "sweep": [], "misses": [{"key": "0123456789abcdef"}]})
    path, report = lab_files.newest_report(tree.base, SUITE, "voidcraft-agent-open-work", "heldout")
    assert lab_files.heldout_view(path, report) == {
        "at": "2026-10-27T01:02:03Z",
        "rows": 48,
        "labels": "operator",
        "best": 0.62,
        "kappa": 0.55,
        "calibration": None,
    }


def test_a_operator_calibration_block_reaches_ops_as_numbers_only():
    """laya-lab `metrics.calibration` writes it; OPS keeps the numbers and leaves the reliability bins in the report."""
    block = {
        "rows": 44,
        "ece": 0.12,
        "teacher": {"rows": 40, "ece": 0.081},
        "temperature": 1.4,
        "ece_refit": 0.06,
        "reliability": [{"lo": 0.0, "hi": 0.3, "n": 3, "p": 0.1, "rate": 0.0, "cuts": []}],
    }
    assert lab_files.calibration_view(block) == {
        "rows": 44,
        "ece": 0.12,
        "teacher_rows": 40,
        "teacher_ece": 0.081,
        "temperature": 1.4,
        "ece_refit": 0.06,
    }
    assert lab_files.calibration_view(None) is None and lab_files.calibration_view({"rows": -1}) is None
    broken = lab_files.calibration_view({"rows": 44, "ece": 2, "teacher": "x", "temperature": -1, "ece_refit": True})
    assert broken == {"rows": 44, "ece": None, "teacher_rows": 0, "teacher_ece": None, "temperature": None, "ece_refit": None}


def test_scores_round_trip_and_bad_rows_are_dropped(tree):
    good = lab_files.scores_row(rows.state_key("t"), 0.81, "voidcraft-agent-open-work", "ck-1", 1, 0.7, "2026-10-27T01:02:03Z")
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [
            good,
            {**good, "p": 1.5},
            {**good, "key": "short"},
            {**good, "checkpoint": "a/b"},
            {**good, "question_version": True},
        ],
    )
    found, code = lab_files.read_scores(tree.base, "agent-open-work")
    assert code == "ok" and found == [good]
    assert lab_files.read_scores(tree.base, "nothing") == ([], "missing")


def test_private_golden_file_starts_with_its_header_and_round_trips(tree):
    key = rows.state_key("the operator's text")
    case = lab_files.private_golden_case(key, "the operator's text", True, False, "claude-sonnet-5-5", "2026-10-27T01:02:03Z")
    assert case["id"] == f"f-{key}" and case["state"] == {"hand_off": "the operator's text"}
    assert case["labels"] == {"operator": True, "model": False, "model_by": "claude-sonnet-5-5"}
    path = tree.write_jsonl(
        paths.private_golden_path(tree.base, SUITE).relative_to(tree.base).as_posix(),
        [case],
        header=lab_files.PRIVATE_GOLDEN_HEADER,
    )
    assert path.name == "agent-open-work.private.jsonl" and path.parent.name == "golden"
    assert path.read_text().splitlines()[0] == lab_files.PRIVATE_GOLDEN_HEADER
    assert lab_files.read_golden_suite(path) == ([case], "ok")


def test_the_public_suite_reads(tree):
    cases, code = lab_files.read_golden_suite(paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl")
    assert code == "ok" and len(cases) >= 10 and all(isinstance(case["expected"], bool) for case in cases)


def test_threshold_and_trained_entries_round_trip_through_the_registry(tree):
    tree.register(
        thresholds={
            "agent_open_work": lab_files.threshold_entry(
                0.62,
                "voidcraft-agent-open-work",
                "voidcraft-agent-open-work-20260102-0304",
                "2026-10-27T01:02:03Z",
                "operator",
                48,
                0.55,
                "heldout-agent-open-work-voidcraft-agent-open-work-operator-20261027-010203.json",
            )
        }
    )
    payload = json.loads((tree.base / "models.json").read_text())
    payload["models"]["voidcraft-agent-open-work"]["trained"] = lab_files.trained_entry(
        2, 400, 70, "55cf4c4", "2026-01-02T03:04:00Z"
    )
    tree.models(payload)
    view = paths.registry_view(tree.base)
    assert view["thresholds"]["agent_open_work"] == {
        "at": 0.62,
        "model": "voidcraft-agent-open-work",
        "checkpoint": "voidcraft-agent-open-work-20260102-0304",
        "swept_at": "2026-10-27T01:02:03Z",
        "labels": "operator",
        "rows": 48,
        "kappa": 0.55,
        "report": "heldout-agent-open-work-voidcraft-agent-open-work-operator-20261027-010203.json",
        "done_at": None,
    }
    model = view["models"]["voidcraft-agent-open-work"]
    assert model["trained"] == {
        "epochs": 2,
        "train_rows": 400,
        "held_out_rows": 70,
        "base_revision": "55cf4c4",
        "at": "2026-01-02T03:04:00Z",
    }
    assert model["eval"]["items"] == 12 and model["eval"]["after"]["loss"] == 0.4


def test_a_malformed_threshold_is_dropped(tree):
    tree.register(
        thresholds={
            "agent_open_work": {"at": True, "model": "voidcraft-agent-open-work", "checkpoint": "x"},
            "Bad Surface": {"at": 0.5, "model": "voidcraft-agent-open-work", "checkpoint": "x"},
        }
    )
    assert paths.registry_view(tree.base)["thresholds"] == {}


@pytest.mark.parametrize("done_at, kept", [(0.35, 0.35), (0.62, None), (0.8, None), (0, None), (True, None)])
def test_a_done_cut_rides_the_threshold_entry_only_under_the_open_cut(done_at, kept):
    entry = lab_files.threshold_entry(
        0.62,
        "voidcraft-agent-open-work",
        "voidcraft-agent-open-work-20260102-0304",
        "2026-10-27T01:02:03Z",
        "operator",
        48,
        0.55,
        None,
        0.35,
    )
    assert entry["done_at"] == 0.35
    assert "done_at" not in lab_files.threshold_entry(0.62, "m", "c", "2026-10-27T01:02:03Z", "operator", 1, None, None)
    assert lab_files.threshold_view({**entry, "done_at": done_at})["done_at"] == kept


# -- the latency report ----------------------------------------------------------------

LATENCY = {
    "kind": "latency",
    "suite": SUITE,
    "surface": "agent_open_work",
    "question_version": 2,
    "at": "2026-11-09T09:15:00Z",
    "window": {"days": 14, "since": "2026-10-26T09:15:00Z", "first": "2026-10-27T00:00:00Z", "last": "2026-11-09T09:00:00Z"},
    "latency_ms": {"n": 812, "p50": 566.0, "p90": 1210.5, "p95": 1480.0, "max": 9020, "le_100": 0.0148},
    "by_start": [
        {"bucket": "cold", "n": 31, "p50": 2210.0, "p90": 4100.0},
        {"bucket": "warm", "n": 781, "p50": 548.0, "p90": 1100.0},
    ],
    "truncation": {
        "state": "ran",
        "states": 900,
        "overflowed": 4,
        "tokenizer": {"model": "voidcraft-agent-open-work", "digest": "sha256:0123456789abcdef", "window": 512, "room": 470},
    },
}


def test_a_latency_report_reads_as_numbers_and_no_other_pattern_takes_it(tree, monkeypatch):
    name = lab_files.latency_report_name(SUITE, "20261109-091500")
    for pattern in (
        lab_files.golden_report_pattern,
        lab_files.heldout_report_pattern,
        lab_files.compare_report_pattern,
        lab_files.nightly_report_pattern,
        lab_files.gate_report_pattern,
    ):
        assert pattern(SUITE).fullmatch(name) is None
    assert lab_files.newest_latency(tree.base, SUITE) is None
    write_report(tree, name, {**LATENCY, "eval_run": "aaaaaaaaaaaa"})
    write_report(tree, lab_files.latency_report_name(SUITE, "20261110-091500"), {**LATENCY, "kind": "nightly"})
    path, report = lab_files.newest_latency(tree.base, SUITE)
    assert path.name == name, "a file that names another kind is not a latency report"
    assert lab_files.latency_view(path, report) == {
        "at": "2026-11-09T09:15:00Z",
        "days": 14,
        "decisions": 812,
        "p50_ms": 566.0,
        "p90_ms": 1210.5,
        "le_100": 0.0148,
        "cold": {"n": 31, "p50_ms": 2210.0},
        "warm": {"n": 781, "p50_ms": 548.0},
        "truncation": {"states": 900, "overflowed": 4},
    }
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "aaaaaaaaaaaa")
    verdict, why = lab_files.run_verdict(tree.base, "latency", SUITE)
    assert why == "" and verdict == {
        "verdict": "PASS",
        "headline": {"cases": 812, "latency_ms_p50": 566.0, "latency_ms_p95": 1480.0},
        "model_digest": "sha256:0123456789abcdef",
        "dataset_version": "q2",
        "accelerator": None,
    }


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"truncation": {"state": "failed", "why": "no tokenizer"}}, None),
        ({"truncation": {"state": "skipped", "why": "no reviewable text yet"}}, None),
        ({"truncation": {"state": "ran", "states": "many", "overflowed": 4}}, None),
    ],
)
def test_a_latency_view_carries_truncation_only_once_it_was_measured(tree, change, expected):
    path = write_report(tree, lab_files.latency_report_name(SUITE, "20261109-091500"), {**LATENCY, **change})
    view = lab_files.latency_view(path, {**LATENCY, **change})
    assert view["truncation"] is expected and view["decisions"] == 812


def test_a_broken_latency_report_has_no_view(tree):
    path = write_report(tree, lab_files.latency_report_name(SUITE, "20261109-091500"), {})
    assert lab_files.latency_view(path, {"kind": "latency", "latency_ms": {"n": -1}}) is None
    empty = lab_files.latency_view(path, {"kind": "latency", "at": "2026-11-09T09:15:00Z", "latency_ms": {"n": 0, "p50": None}})
    assert empty["p50_ms"] is None and empty["cold"] == {"n": 0, "p50_ms": None} and empty["days"] is None


# -- the stability meter ---------------------------------------------------------------

V1, V2, V3, V4 = (f"voidcraft-agent-open-work-2026110{day}-0900" for day in range(1, 5))


def scored(version, verdicts, *, cut=0.7, device="mps", digest="sha256:aaaa", at="2026-11-09T09:00:00Z"):
    """One stability row per verdict: state i reads open (p 0.9) or done (p 0.1)."""
    return [
        lab_files.stability_row(
            key=f"{i:016x}", version=version, digest=digest, device=device, p=0.9 if is_open else 0.1, cut=cut, scored_at=at
        )
        for i, is_open in enumerate(verdicts)
    ]


def test_stability_rows_round_trip_and_bad_rows_are_dropped(tree):
    rows_out = scored(V1, [True, False])
    tree.write_jsonl(
        "stability/agent_open_work.jsonl",
        [*rows_out, {**rows_out[0], "version": "../escape"}, {**rows_out[0], "device": "tpu"}, {**rows_out[0], "p": 1.5}],
    )
    assert lab_files.read_stability(tree.base, "agent_open_work") == (rows_out, "ok")
    assert lab_files.read_stability(tree.base, "paste") == ([], "missing")
    assert lab_files.stability_key(rows_out[0]) == ("0" * 16, V1, "sha256:aaaa", "mps")


def test_a_stop_set_row_must_carry_the_text_its_key_names(tree):
    state = "a synthetic closing message"
    good = lab_files.stop_set_row(
        key=rows.state_key(state), at="2026-11-08T00:00:00Z", state=state, added_at="2026-11-09T09:00:00Z"
    )
    tree.write_jsonl("stability/agent_open_work-stop-set.jsonl", [good, {**good, "state": "another text"}, good])
    assert lab_files.read_stop_set(tree.base, "agent_open_work") == ([good], "ok"), (
        "one row per key; a mismatched text is dropped"
    )
    assert paths.stop_set_path(tree.base, "agent_open_work").parent == paths.stability_path(tree.base, "agent_open_work").parent


def test_stable_kappa_averages_the_last_three_consecutive_pairs():
    base = [True] * 12 + [False] * 12
    flipped = [not v if i < 3 else v for i, v in enumerate(base)]
    summary = lab_files.stability_summary([*scored(V1, flipped), *scored(V2, base), *scored(V3, base), *scored(V4, base)])
    assert summary["versions"] == [V1, V2, V3, V4] and summary["states"] == 24 and summary["device"] == "mps"
    assert [(pair["from"], pair["to"], pair["n"], pair["flips"]) for pair in summary["pairs"]] == [
        (V1, V2, 24, 3),
        (V2, V3, 24, 0),
        (V3, V4, 24, 0),
    ]
    assert [pair["kappa"] for pair in summary["pairs"]] == [0.75, 1.0, 1.0]
    assert (summary["kappa_average"], summary["window"], summary["stable"]) == (0.917, 3, False)
    settled = lab_files.stability_summary([*scored(V1, base), *scored(V2, base), *scored(V3, base), *scored(V4, base)])
    assert (settled["kappa_average"], settled["window"], settled["stable"]) == (1.0, 3, True)
    two = lab_files.stability_summary([*scored(V1, base), *scored(V2, base)])
    assert (two["kappa_average"], two["window"], two["stable"]) == (1.0, 1, False), "a full window before it reads stable"


def test_stable_kappa_edges():
    assert lab_files.stability_summary([])["kappa_average"] is None
    few = lab_files.stability_summary([*scored(V1, [True] * 10 + [False] * 9), *scored(V2, [True] * 10 + [False] * 9)])
    assert few["pairs"][0]["kappa"] is None and few["pairs"][0]["agree"] == 1.0 and few["kappa_average"] is None, (
        "under the minimum of shared states there is no κ"
    )
    constant = lab_files.stability_summary([*scored(V1, [False] * 24), *scored(V2, [False] * 24)])
    assert constant["pairs"][0]["kappa"] == 1.0, "the same constant verdict everywhere: nothing moved"
    opposite = lab_files.stability_summary([*scored(V1, [False] * 24), *scored(V2, [True] * 24)])
    assert (opposite["pairs"][0]["kappa"], opposite["pairs"][0]["flips"], opposite["kappa_average"]) == (0.0, 24, 0.0)


def test_stable_kappa_reads_one_device_the_newest_digest_and_each_checkpoint_s_own_cut():
    base = [True] * 12 + [False] * 12
    rows_in = [
        *scored(V1, base),
        *scored(V2, base),
        *scored(V2, [not v for v in base], device="cpu", at="2026-11-08T00:00:00Z"),
        *scored(V1, [not v for v in base], digest="sha256:old", at="2026-11-01T00:00:00Z"),
    ]
    summary = lab_files.stability_summary(rows_in)
    assert summary["device"] == "mps" and summary["pairs"][0]["flips"] == 0
    # Every p is 0.9 or 0.1: a cut of 0.95 for V2 reads all of its states as done.
    assert lab_files.stability_summary(rows_in, {V2: 0.95})["pairs"][0]["flips"] == 12
    assert lab_files.stability_cuts(
        {
            "versions": {
                V1: {"surface": "agent_open_work", "cut": {"at": 0.6}},
                V2: {"surface": "agent_open_work", "cut": None},
                V3: {"surface": "paste", "cut": {"at": 0.5}},
                V4: {"surface": "agent_open_work", "cut": {"at": 1.0}},
            }
        },
        "agent_open_work",
    ) == {V1: 0.6}


def test_a_runners_variable_is_read_by_its_tldrack_name_first_then_its_older_one():
    from laya_bridge import env_names

    assert lab_files.EVAL_RUN_ENV == "TLDRACK_EVAL_RUN"
    assert lab_files.eval_run_field({"VOIDCRAFT_EVAL_RUN": "aaaaaaaaaaaa"}) == {"eval_run": "aaaaaaaaaaaa"}
    both = {"TLDRACK_EVAL_RUN": "bbbbbbbbbbbb", "VOIDCRAFT_EVAL_RUN": "aaaaaaaaaaaa"}
    assert lab_files.eval_run_field(both) == {"eval_run": "bbbbbbbbbbbb"}
    assert env_names.read({"TLDRACK_EVAL_RUN": "", "VOIDCRAFT_EVAL_RUN": "x"}, "TLDRACK_EVAL_RUN") == "x"
    assert env_names.read({}, "TLDRACK_EVAL_RUN") is None and env_names.legacy_name("OTHER") is None
    assert env_names.spelled("TLDRACK_EVAL_RUN") == "TLDRACK_EVAL_RUN (or VOIDCRAFT_EVAL_RUN)"
