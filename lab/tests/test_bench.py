"""`laya-lab bench`: the plan, the bootstrap, and a results file that carries numbers and never a row.

Every text, id and voter here is invented for the test. No model loads: a voter is a function from a state to p.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laya_lab import bench, compare, metrics
from laya_bridge import paths, rows

from conftest import CHECKPOINT, SUITE, FakeRouter, synthetic

SUITE_SPEC = bench.SUITES[SUITE]


@pytest.fixture(autouse=True)
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file's `commit` is the checkout's; a test has none to name."""
    monkeypatch.setattr(bench, "head_commit", lambda root: None)


def fake(name: str, p_of, *, threshold: float | None = 0.7, kind: str = "zero_shot") -> bench.BenchVoter:
    return bench.BenchVoter(name, name.upper(), kind, "none", open=lambda: p_of, threshold=threshold)


def by_word(state) -> float:
    """Open-looking text scores high, with the row's length as a tie-breaker so the ranking is not degenerate."""
    text = json.dumps(state).lower()
    return round((0.8 if "open" in text or "not started" in text else 0.2) + (len(text) % 7) / 100, 4)


def labelled_store(lab, n: int = 6) -> tuple[list[str], list[str]]:
    """`n` open and `n` done labelled turn ends in the store: a private held-out split, since nothing trained on them."""
    texts = synthetic("still-open", n) + synthetic("all-done", n)
    keys = lab.unlabelled(texts)
    lab.labels({key: i < n for i, key in enumerate(keys)})
    return keys, texts


def written(folder: Path) -> dict:
    found = list(folder.glob("*.json"))
    assert len(found) == 1
    return json.loads(found[0].read_text(encoding="utf-8"))


# ── the bootstrap ─────────────────────────────────────────────────────────────

PS = [0.91, 0.80, 0.80, 0.62, 0.55, 0.41, 0.40, 0.33, 0.20, 0.12, 0.74, 0.28]
EXPECTED = [True, True, False, True, False, True, False, False, False, False, True, True]


def test_a_plain_draw_ranks_as_the_lab_auc_does():
    order = sorted(range(len(PS)), key=PS.__getitem__)
    assert round(bench.weighted_auc(order, PS, EXPECTED, [1] * len(PS)), 4) == metrics.auc(PS, EXPECTED)
    assert bench.weighted_auc(order, PS, EXPECTED, [int(e) for e in EXPECTED]) is None  # a draw with no done row


def test_the_interval_is_deterministic_and_holds_the_point():
    costs = metrics.warning_costs(PS, EXPECTED, 0.7, 2.0)
    first = bench.bootstrap(PS, EXPECTED, costs, seed=7, resamples=400)
    assert first == bench.bootstrap(PS, EXPECTED, costs, seed=7, resamples=400)
    assert first != bench.bootstrap(PS, EXPECTED, costs, seed=8, resamples=400)
    lo, hi = first["auc"]
    assert lo < metrics.auc(PS, EXPECTED) < hi
    assert first["brier"][0] < metrics.brier(PS, EXPECTED) < first["brier"][1]
    assert first["cost"][0] <= sum(costs) <= first["cost"][1]


def test_an_estimate_never_excludes_its_value():
    assert bench.estimate(0.9, (0.91, 0.95), 4) == {"value": 0.9, "lo": 0.9, "hi": 0.95}
    assert bench.estimate(0.5, None, 4) == {"value": 0.5, "lo": 0.5, "hi": 0.5}


def test_the_roc_runs_from_nothing_flagged_to_everything_flagged():
    points = bench.roc_points(PS, EXPECTED)
    assert (points[0]["fpr"], points[0]["tpr"]) == (0.0, 0.0) and (points[-1]["fpr"], points[-1]["tpr"]) == (1.0, 1.0)
    for before, after in zip(points, points[1:]):
        assert after["threshold"] < before["threshold"] and after["fpr"] >= before["fpr"] and after["tpr"] >= before["tpr"]
    many = [i / 2000 for i in range(2000)]
    assert len(bench.roc_points(many, [i % 2 == 0 for i in range(2000)])) == bench.ROC_MAX


# ── the sets ──────────────────────────────────────────────────────────────────


def test_a_store_without_a_private_split_scores_the_golden_set_alone(lab, public_golden, tmp_path, capsys):
    plan = bench.Plan([fake("word", by_word)])
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out", day="2026-01-02") == 0
    payload = written(tmp_path / "out")
    assert (tmp_path / "out" / "2026-01-02.json").is_file()
    assert [block["set"] for block in payload["sets"]] == ["golden"]
    assert payload["sets"][0]["n"] == {"open": 1, "done": 1}
    assert ".h" not in payload["protocol"]["id"] and "heldout · none" in capsys.readouterr().out


def test_a_labelled_store_adds_the_held_out_set(lab, public_golden, tmp_path):
    labelled_store(lab)
    assert bench.run(SUITE, lab.base, plan=bench.Plan([fake("word", by_word)]), out_dir=tmp_path / "out") == 0
    payload = written(tmp_path / "out")
    assert [block["set"] for block in payload["sets"]] == ["golden", "heldout"]
    assert payload["sets"][1]["n"] == {"open": 6, "done": 6}
    assert ".h12-" in payload["protocol"]["id"]


def test_a_provisional_public_case_is_not_scored_whatever_the_store_says(lab, public_golden):
    from conftest import add_provisional_case

    before = bench.protocol_id(SUITE_SPEC, {"golden": bench.golden_rows(SUITE, SUITE_SPEC)})
    add_provisional_case(public_golden)
    lab.operator("golden:case-new", "agree")
    scored = bench.golden_rows(SUITE, SUITE_SPEC)
    assert [row[0] for row in scored] == ["case-open", "case-done"]
    assert bench.protocol_id(SUITE_SPEC, {"golden": scored}) == before


def test_the_protocol_id_follows_the_rows_asked(lab, public_golden):
    golden = bench.golden_rows(SUITE, SUITE_SPEC)
    one = bench.protocol_id(SUITE_SPEC, {"golden": golden})
    assert one == bench.protocol_id(SUITE_SPEC, {"golden": list(reversed(golden))})
    assert one.startswith(f"{SUITE}.q{SUITE_SPEC.question_version}.g")
    flipped = [(golden[0][0], golden[0][1], not golden[0][2]), *golden[1:]]
    assert bench.protocol_id(SUITE_SPEC, {"golden": flipped}) != one
    assert len(bench.protocol_id(SUITE_SPEC, {"golden": golden, "heldout": golden})) <= 64


# ── the file ──────────────────────────────────────────────────────────────────

ESTIMATE = {"value", "lo", "hi"}
TOP = {"v", "protocol", "generated_at", "commit", "voters", "sets", "rack", "felt", "reproduce"}
PROTOCOL = {"id", "surface", "question", "question_version", "positive", "description", "interval", "costs"}
VOTER = {"name", "label", "kind", "of", "labels", "params_m", "runtime", "licence", "served"}
SCORE = {"voter", "auc", "brier", "ece", "roc", "reliability", "operating", "latency"}


def test_the_file_is_the_schema_shape(lab, public_golden, tmp_path):
    """The key sets of `schemas/src/bench-results.ts`, and the rules its refinements check."""
    labelled_store(lab)
    plan = bench.Plan(
        [
            fake("word", by_word),
            fake("flat", lambda state: 0.5, threshold=None),
            bench.BenchVoter("mean.word+flat", "mean(word, flat)", "ensemble", "none", of="word", members=("word", "flat")),
        ]
    )
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out") == 0
    payload = written(tmp_path / "out")
    assert (
        set(payload) == TOP
        and payload["v"] == 1
        and payload["rack"] is None
        and payload["felt"] is None
        and payload["commit"] is None
    )
    assert set(payload["protocol"]) == PROTOCOL
    assert payload["protocol"]["interval"] == {
        "level": 0.95,
        "method": "bootstrap",
        "resamples": bench.BOOTSTRAP_RESAMPLES,
        "seed": bench.BOOTSTRAP_SEED,
    }
    assert payload["protocol"]["costs"] == {"false_warning": SUITE_SPEC.false_warning_cost}
    assert set(payload["reproduce"]) == {"command", "set"} and payload["reproduce"]["set"] == "golden"
    assert [set(voter) for voter in payload["voters"]] == [VOTER] * 3
    assert [voter["name"] for voter in payload["voters"]] == ["word", "flat", "mean.word+flat"]
    assert payload["voters"][2]["of"] == "word"
    for block in payload["sets"]:
        assert set(block) == {"set", "n", "scores"} and set(block["n"]) == {"open", "done"}
        total = block["n"]["open"] + block["n"]["done"]
        assert [entry["voter"] for entry in block["scores"]] == ["word", "flat", "mean.word+flat"]
        for entry in block["scores"]:
            assert set(entry) == SCORE
            assert set(entry["auc"]) == ESTIMATE == set(entry["brier"]) == set(entry["operating"]["cost"])
            for found in (entry["auc"], entry["brier"], entry["operating"]["cost"]):
                assert found["lo"] <= found["value"] <= found["hi"]
            assert entry["ece"] == {"value": entry["ece"]["value"], "bins": 10}
            assert all(set(point) == {"threshold", "fpr", "tpr"} for point in entry["roc"]) and len(entry["roc"]) >= 2
            bins = entry["reliability"]
            assert all(set(found) == {"lo", "hi", "n", "p", "rate"} for found in bins) and len(bins) == 10
            assert bins[0]["lo"] == 0 and bins[-1]["hi"] == 1 and sum(found["n"] for found in bins) == total
            assert all(after["lo"] == before["hi"] for before, after in zip(bins, bins[1:]))
            assert all((found["n"] == 0) == (found["p"] is None and found["rate"] is None) for found in bins)
            assert set(entry["operating"]) == {"threshold", "source", "false_warnings", "missed_open", "cost"}
        word, flat, mean = block["scores"]
        assert word["operating"]["source"] == "live" and word["operating"]["threshold"] == 0.7
        assert flat["operating"]["source"] == "swept"  # no cut of its own: the cost rule's, on this set
        assert set(word["latency"]) == {"n", "p50_ms", "p90_ms"} and word["latency"]["n"] == total
        assert word["latency"]["p50_ms"] <= word["latency"]["p90_ms"] and mean["latency"] is None
        assert word["auc"]["value"] == 1.0 and flat["auc"]["value"] == 0.5


def structure(value):
    """A JSON value's key structure: objects by key, a list as the union of its items, every leaf alike. A null
    stands for a value the schema lets be absent, so it matches anything."""
    if isinstance(value, dict):
        return {key: structure(item) for key, item in value.items()}
    if isinstance(value, list):
        merged = None
        for item in value:
            merged = merge(merged, structure(item))
        return [merged]
    return None if value is None else "leaf"


def merge(a, b):
    if a is None or b is None:
        return a if b is None else b
    if isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b), f"{sorted(set(a) ^ set(b))} differ"
        return {key: merge(a[key], b[key]) for key in a}
    if isinstance(a, list) and isinstance(b, list):
        return [merge(a[0], b[0])]
    assert a == b, f"{a!r} is not {b!r}"
    return a


def test_the_file_has_the_fixture_s_key_structure(lab, public_golden, tmp_path):
    """Against the schema lane's own example, once it is in this checkout."""
    fixture = paths.REPO_ROOT / "bench" / "results" / "fixture.json"
    if not fixture.is_file():
        pytest.skip("bench/results/fixture.json is not in this checkout yet")
    labelled_store(lab)
    assert bench.run(SUITE, lab.base, plan=bench.Plan([fake("word", by_word)]), out_dir=tmp_path / "out") == 0
    example = json.loads(fixture.read_text(encoding="utf-8"))
    example.pop("fixture", None)  # the one key only an invented file carries
    merge(structure(written(tmp_path / "out")), structure(example))


def test_no_row_id_key_or_text_reaches_the_file(lab, public_golden, tmp_path):
    keys, texts = labelled_store(lab)
    plan = bench.Plan([fake("word", by_word), fake("flat", lambda state: 0.5, threshold=None)])
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out") == 0
    serialized = next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8")
    sets, _why = bench.load_sets(lab.base, SUITE, SUITE_SPEC)
    asked = [row for data in sets.values() for row in data]
    assert len(asked) == 2 + len(keys)
    for row_id, state, _expected in asked:
        assert row_id not in serialized
        assert json.dumps(state)[1:-1] not in serialized
    for needle in (
        *keys,
        *texts,
        "case-open",
        "case-done",
        "Phase C is not started.",
        "All three phases shipped.",
        CHECKPOINT,
        str(lab.base),
        "checkpoints",
    ):
        assert needle not in serialized, needle


def test_the_writer_refuses_a_payload_that_carries_a_row(tmp_path):
    sets = {"golden": [("case-with-an-id", "The import UI is not started.", True)]}
    for carried in ({"note": "case-with-an-id"}, {"note": "…and The import UI is not started. it said"}):
        with pytest.raises(RuntimeError, match="nothing was written"):
            bench.write_results(carried, sets, tmp_path / "out", "2026-01-02")
    assert not (tmp_path / "out").exists()
    assert bench.write_results({"n": 1}, sets, tmp_path / "out", "2026-01-02").read_text(encoding="utf-8") == '{\n  "n": 1\n}\n'


# ── voters that cannot run ────────────────────────────────────────────────────


def refuses():
    raise bench.Unavailable("its server is not installed here")


def breaks():
    raise OSError("/a/private/path could not be opened")


def test_the_rack_block_is_measured_only_when_asked(lab, public_golden, tmp_path, monkeypatch, capsys):
    """A results file is published; the operator's own use goes into it only behind `--rack`."""
    labelled_store(lab)
    asked: list[int] = []
    counts = json.loads((paths.REPO_ROOT / "bench" / "results" / "fixture.json").read_text(encoding="utf-8"))["rack"]
    monkeypatch.setattr(bench.rack, "measure", lambda base, suite: (asked.append(1) or counts, None))
    plan = bench.Plan([fake("word", by_word)])
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "quiet", day="2026-01-02") == 0
    assert asked == [] and written(tmp_path / "quiet")["rack"] is None
    assert "rack     withheld" in capsys.readouterr().out
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "asked", day="2026-01-02", with_rack=True) == 0
    assert len(asked) == 1 and written(tmp_path / "asked")["rack"] == counts
    assert written(tmp_path / "asked")["felt"] is None  # a store with no graded live sample has none
    assert "felt     none" in capsys.readouterr().out


def test_the_felt_block_rides_with_the_rack_and_is_numbers_only(lab, public_golden, tmp_path, monkeypatch, capsys):
    """`metrics.felt` reaches the file only behind `--rack`, and passes the `leaked` guard: counts and rates alone."""
    labelled_store(lab)
    felt = json.loads((paths.REPO_ROOT / "bench" / "results" / "fixture.json").read_text(encoding="utf-8"))["felt"]
    monkeypatch.setattr(bench.rack, "measure", lambda base, suite: (None, "no ledger"))
    monkeypatch.setattr(bench, "measure_felt", lambda base, suite: felt)
    plan = bench.Plan([fake("word", by_word)])
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "quiet", day="2026-01-02") == 0
    assert written(tmp_path / "quiet")["felt"] is None
    capsys.readouterr()
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "asked", day="2026-01-02", with_rack=True) == 0
    assert written(tmp_path / "asked")["felt"] == felt
    out = capsys.readouterr().out
    assert "false ⚠️   5/100 [2.1, 10.1] (6 of 120)" in out and "💬 share   0.258 [0.205, 0.317] (62 of 240)" in out


def test_a_voter_that_cannot_run_is_skipped_and_the_run_goes_on(lab, public_golden, tmp_path, capsys):
    plan = bench.Plan(
        [
            fake("word", by_word),
            bench.BenchVoter("absent", "Absent", "zero_shot", "none", open=refuses),
            bench.BenchVoter("broken", "Broken", "zero_shot", "none", open=breaks),
            fake("liar", lambda state: 1.5),
            bench.BenchVoter("mean.word+absent", "mean(word, absent)", "ensemble", "none", of="word", members=("word", "absent")),
        ],
        {"planned-out": "no champion is registered"},
    )
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out") == 0
    payload = written(tmp_path / "out")
    assert [voter["name"] for voter in payload["voters"]] == ["word"]
    assert [entry["voter"] for entry in payload["sets"][0]["scores"]] == ["word"]
    out = capsys.readouterr().out
    assert "skipped  absent: its server is not installed here" in out
    assert "skipped  broken: could not answer (OSError)" in out and "/a/private/path" not in out
    assert "skipped  liar: could not answer (ValueError)" in out
    assert "skipped  mean.word+absent: absent did not answer" in out
    assert "skipped  planned-out: no champion is registered" in out
    assert "/a/private/path" not in json.dumps(payload)


def test_no_voter_answering_writes_nothing(lab, public_golden, tmp_path, capsys):
    plan = bench.Plan([bench.BenchVoter("absent", "Absent", "zero_shot", "none", open=refuses)])
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out") == 1
    assert not (tmp_path / "out").exists() and "no voter answered" in capsys.readouterr().out


def test_a_dry_run_loads_nothing_and_writes_nothing(lab, public_golden, tmp_path, capsys):
    labelled_store(lab)
    plan = bench.Plan(
        [bench.BenchVoter("heavy", "Heavy", "base", "mps", open=lambda: pytest.fail("a dry run loaded a model"))],
        {bench.FINE_TUNE: "no champion is registered for agent_open_work"},
    )
    assert bench.run(SUITE, lab.base, plan=plan, out_dir=tmp_path / "out", dry_run=True) == 0
    assert not (tmp_path / "out").exists()
    out = capsys.readouterr().out
    assert "golden · 2 rows (1 open / 1 done)" in out and "heldout · 12 rows (6 open / 6 done)" in out
    assert "voters   heavy" in out and f"skipped  {bench.FINE_TUNE}: no champion" in out and "nothing written" in out


# ── the plan ──────────────────────────────────────────────────────────────────


def never_opened():
    return FakeRouter(lambda state: pytest.fail("the plan asked a model"))


def test_an_empty_store_plans_the_base_model_alone(lab, public_golden):
    plan = bench.plan_voters(lab.base, SUITE_SPEC, router=never_opened())
    assert [voter.name for voter in plan.voters] == [bench.BASE]
    base = plan.voters[0]
    assert (base.kind, base.served, base.licence, base.threshold, base.labels) == (
        "base",
        True,
        "Apache-2.0",
        SUITE_SPEC.threshold,
        None,
    )
    assert set(plan.skipped) == {bench.FINE_TUNE, bench.FLOOR}


def test_an_endpoint_voter_brings_its_mean_with_each_laya_voter(lab, public_golden):
    plan = bench.plan_voters(
        lab.base,
        SUITE_SPEC,
        ["other=http://127.0.0.1:8010,runtime=mlx,licence=MIT,params_m=4000,label=Other 4B"],
        router=never_opened(),
    )
    assert [voter.name for voter in plan.voters] == [bench.BASE, "other", "mean.laya-base+other"]
    other, mean = plan.voters[1:]
    assert (other.kind, other.runtime, other.licence, other.params_m, other.label, other.threshold) == (
        "zero_shot",
        "mlx",
        "MIT",
        4000.0,
        "Other 4B",
        None,
    )
    assert (mean.kind, mean.of, mean.members, mean.label) == (
        "ensemble",
        bench.BASE,
        (bench.BASE, "other"),
        "MEAN · LAYA-BASE + OTHER",
    )


def test_specs_the_bench_will_not_run():
    tuned = bench.parse_voter("tuned=http://127.0.0.1:8011,kind=fine_tune,labels=105")
    assert (tuned["labels"], tuned["label"], tuned["runtime"]) == (105, "TUNED", "none")
    for spec in (
        "laya:served",
        "floor",
        "tfidf-floor=http://127.0.0.1:1",
        "mean.a+b=http://127.0.0.1:1",
        "other=http://203.0.113.9:8010",
        "other=http://127.0.0.1:1,runtime=abacus",
        "other=http://127.0.0.1:1,kind=base",
        "other=http://127.0.0.1:1,colour=red",
        "other=http://127.0.0.1:1,params_m=many",
        "other=http://127.0.0.1:1,kind=fine_tune",
        "other=http://127.0.0.1:1,labels=3",
    ):
        with pytest.raises(compare.Refused):
            bench.parse_voter(spec)


def test_a_refused_spec_exits_3_before_anything_is_read(lab, public_golden, tmp_path, capsys):
    assert bench.run(SUITE, lab.base, voters=["laya:served"], out_dir=tmp_path / "out") == bench.REFUSED
    assert (
        bench.run(SUITE, lab.base, voters=["a=http://127.0.0.1:1", "a=http://127.0.0.1:2"], out_dir=tmp_path / "out")
        == bench.REFUSED
    )
    assert not (tmp_path / "out").exists() and "bench refused" in capsys.readouterr().out


def test_a_served_fine_tune_is_benched_beside_its_base_and_its_floor(lab, public_golden, tmp_path, capsys):
    """The machine's own plan, end to end, on a fake router: public names only, the fine-tune served and paired with
    its base, the floor fitted on the served checkpoint's rows."""
    trained = synthetic("trained-open", 12) + synthetic("trained-done", 12)
    lab.trained_on(trained)
    lab.write_jsonl(
        f"checkpoints/{CHECKPOINT}.data/train.jsonl",
        [{"state": text, "questions": {}, "expected": {"q": i < 12}} for i, text in enumerate(trained)],
    )
    lab.operator(rows.state_key(trained[0]))
    labelled_store(lab)
    router = FakeRouter(lambda state: 0.9 if "open" in state.lower() or "not started" in state.lower() else 0.1)

    assert bench.run(SUITE, lab.base, router=router, out_dir=tmp_path / "out") == 0
    payload = written(tmp_path / "out")
    roster = {voter["name"]: voter for voter in payload["voters"]}
    assert list(roster) == [bench.BASE, bench.FINE_TUNE, bench.FLOOR]
    assert (roster[bench.FINE_TUNE]["kind"], roster[bench.FINE_TUNE]["of"], roster[bench.FINE_TUNE]["served"]) == (
        "fine_tune",
        bench.BASE,
        True,
    )
    assert roster[bench.FINE_TUNE]["labels"] == 1 and roster[bench.FINE_TUNE]["licence"] is None
    assert not roster[bench.BASE]["served"] and roster[bench.FLOOR]["runtime"] == "cpu"
    assert [voter["labels"] for voter in payload["voters"]] == [None, 1, None]
    held = next(block for block in payload["sets"] if block["set"] == "heldout")
    assert held["n"] == {"open": 6, "done": 6}
    floor = next(entry for entry in held["scores"] if entry["voter"] == bench.FLOOR)
    assert floor["operating"]["source"] == "live" and floor["auc"]["value"] > 0.9
    assert {model for _state, model in router.calls} == {"english", "tldrack-agent-open-work"}
    assert CHECKPOINT not in json.dumps(payload)
    assert "results →" in capsys.readouterr().out


def test_a_floor_that_cannot_train_is_skipped_with_numbers_only(lab, public_golden, tmp_path, capsys):
    lab.trained_on(synthetic("trained-open", 12))  # one class: no floor
    router = FakeRouter(lambda state: 0.6)
    assert bench.run(SUITE, lab.base, router=router, out_dir=tmp_path / "out") == 0
    assert [voter["name"] for voter in written(tmp_path / "out")["voters"]] == [bench.BASE, bench.FINE_TUNE]
    assert "skipped  tfidf-floor: floor: 12 rows (12 open) cannot train" in capsys.readouterr().out
