"""The local teacher's κ study (laya_lab/teacher_local.py): its own selection, resumable answers, the bar.

Every text is synthetic, Ollama is a fake, and no lease is configured (conftest), so every run just runs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from laya_lab import cli, infer, lease, teacher_local as T
from laya_bridge import lab_files, paths, rows

from conftest import synthetic

#: An invented weights digest.
DIGEST = "0a1b2c3d4e5f" + "0" * 52


def tags(digest: str = DIGEST, family: str = "gpt-oss") -> dict:
    return {"models": [{"name": "gpt-oss:20b", "size": 12.8 * 1024**3, "digest": digest, "details": {"family": family}}]}


class FakeOllama:
    """Answers `/api/chat` by a rule on each item's text; records every body it was sent."""

    def __init__(
        self, opens=lambda text: "open" in text, drop: set[str] = frozenset(), fail: bool = False, fail_after: int | None = None
    ) -> None:
        self.opens, self.drop, self.fail, self.fail_after, self.bodies = opens, set(drop), fail, fail_after, []

    def __call__(self, url: str, body: dict, timeout: float) -> dict:
        assert url.startswith("http://127.0.0.1:11434/"), url
        self.bodies.append(body)
        if self.fail or (self.fail_after is not None and len(self.bodies) > self.fail_after):
            raise OSError("connection refused")
        items = json.loads(body["messages"][1]["content"].split("\n\n", 1)[1])["items"]
        labels = [
            {"id": item["id"], "open": self.opens(item["message"]), "why": "synthetic"}
            for item in items
            if item["id"] not in self.drop
        ]
        return {"message": {"role": "assistant", "content": "Here: " + json.dumps({"labels": labels})}}


@pytest.fixture(autouse=True)
def as_command(monkeypatch):
    monkeypatch.setattr("sys.argv", ["laya-lab", "teacher-kappa"])
    monkeypatch.setattr(T, "BOOTSTRAP_RESAMPLES", 200)  # the interval's arithmetic, not its precision; 2,000 runs live
    monkeypatch.delenv(lease.OLLAMA_URL_ENV, raising=False)
    monkeypatch.delenv(lab_files.EVAL_RUN_ENV, raising=False)
    monkeypatch.setattr(infer, "HELD", {})


def seed_lab(lab, n_sonnet: int = 40, n_operator_only: int = 4):
    """Sonnet labels on `n_sonnet` texts (half open), the operator agreeing on twelve of each class and grading
    `n_operator_only` texts Sonnet never saw."""
    half = n_sonnet // 2
    labelled = synthetic("open sonnet", half) + synthetic("done sonnet", n_sonnet - half)
    keys = lab.unlabelled(labelled + synthetic("open operator", n_operator_only))
    sonnet_keys = keys[:n_sonnet]
    lab.labels({key: i < half for i, key in enumerate(sonnet_keys)})
    for i in (*range(12), *range(half, half + 12)):
        lab.operator(sonnet_keys[i], "agree", against_open=i < half)
    for key in keys[n_sonnet:]:
        lab.operator(key, "agree", against_open=True)
    lab.operator("golden:phases-c-not-started", "flip", against_open=True, by="golden", stratum="golden")
    return sonnet_keys, keys[n_sonnet:]


def test_the_selection_is_sonnets_keys_plus_every_operator_grade_label_would_skip(lab):
    sonnet_keys, operator_only = seed_lab(lab)
    lab.operator(rows.state_key("a text this tree no longer holds"), "agree", against_open=False)
    selection = T.select(lab.base)
    assert selection.keys == sorted(sonnet_keys + operator_only), (
        "operator-only keys are asked; golden: answers are not turn ends"
    )
    assert (len(selection.sonnet), len(selection.operator), selection.no_text) == (40, 24 + 4 + 1, 1)
    assert selection.dataset_version.startswith("s40-f29-")


def test_a_run_asks_the_teacher_sonnets_rubric_and_resumes_on_the_same_weights(lab, capsys):
    seed_lab(lab)
    fake = FakeOllama()
    assert T.run(lab.base, post=fake, get=lambda url: tags()) == 0
    body = fake.bodies[0]
    assert body["messages"][0]["content"] == T.LABEL_SYSTEM_PROMPT and body["format"] == T.LABEL_SCHEMA
    assert (body["think"], body["options"]["temperature"], body["options"]["num_ctx"]) == ("low", 0, T.TEACHER_NUM_CTX)
    assert len(fake.bodies) == -(-44 // T.TEACHER_BATCH)
    stored = [json.loads(line) for line in T.teacher_path(lab.base).read_text().splitlines()]
    assert len(stored) == 44 and {(r["by"], r["digest"], r["rubric"]) for r in stored} == {
        ("gpt-oss:20b", DIGEST, T.rubric_digest())
    }
    assert oct(T.teacher_path(lab.base).stat().st_mode & 0o777) == "0o600"

    again = FakeOllama()
    assert T.run(lab.base, post=again, get=lambda url: tags()) == 0
    assert again.bodies == [], "a key this teacher answered is never asked again"
    repulled = FakeOllama()
    assert T.run(lab.base, post=repulled, get=lambda url: tags(digest="ab" * 32)) == 0
    assert len(repulled.bodies) == len(fake.bodies), "new weights are a new teacher"
    out = capsys.readouterr().out
    assert "teacher_sonnet   κ 1.000" in out and "→ PASS" in out


def test_the_report_holds_keys_and_numbers_never_a_text(lab):
    seed_lab(lab)
    assert T.run(lab.base, post=FakeOllama(opens=lambda text: "operator" in text or "open" in text), get=lambda url: tags()) == 0
    report = T.newest_report(lab.base)
    text = json.dumps(report)
    assert "synthetic closing message" not in text and "why" not in text
    assert report["kind"] == "teacher_kappa" and report["coverage"] == 1.0
    assert report["pairs"]["teacher_sonnet"]["n"] == 40 and report["pairs"]["teacher_operator"]["n"] == 28


def test_a_left_out_key_is_asked_once_more_alone(lab):
    sonnet_keys, _ = seed_lab(lab)
    fake = FakeOllama(drop={sonnet_keys[0]})
    T.run(lab.base, post=fake, get=lambda url: tags())
    assert [len(json.loads(b["messages"][1]["content"].split("\n\n", 1)[1])["items"]) for b in fake.bodies][-1] == 1
    assert T.newest_report(lab.base)["answered"] == 43


def test_failed_calls_store_nothing_and_three_in_a_row_stop_the_run(lab):
    seed_lab(lab)
    fake = FakeOllama(fail=True)
    assert T.run(lab.base, post=fake, get=lambda url: tags()) == T.FAILED
    assert len(fake.bodies) == T.FAILURES_MAX and not T.teacher_path(lab.base).exists()
    assert T.newest_report(lab.base)["stopped"] == "failures"


def test_a_reply_with_no_readable_label_is_a_miss_never_a_down_transport(lab):
    sonnet_keys, _ = seed_lab(lab, n_sonnet=160)
    stubborn = set(sonnet_keys[-(T.FAILURES_MAX + 1) :])  # each asked alone again, and each unreadable alone too
    fake = FakeOllama(drop=stubborn)
    assert T.run(lab.base, post=fake, get=lambda url: tags()) == 0
    report = T.newest_report(lab.base)
    assert report["stopped"] is None and report["failed_calls"] == 0 and report["unreadable_calls"] == len(stubborn)
    assert report["answered"] == 164 - len(stubborn) and report["verdict"] == "PASS", "the verdict reaches the ledger"


def test_a_transport_that_goes_down_once_the_selection_is_covered_still_exits_with_the_verdict(lab):
    seed_lab(lab, n_sonnet=160, n_operator_only=4)  # 164 keys, 21 batches: the last one and its retries go down
    fake = FakeOllama(fail_after=-(-164 // T.TEACHER_BATCH) - 1)
    assert T.run(lab.base, post=fake, get=lambda url: tags()) == 0, "covered: the report's verdict stands"
    report = T.newest_report(lab.base)
    assert (report["stopped"], report["failed_calls"]) == ("failures", T.FAILURES_MAX)
    assert report["coverage"] >= T.COVERAGE_MIN and report["verdict"] == "PASS"
    fresh, _reason = T.check(lab.base, get=lambda url: tags())
    assert fresh, "the check calls it fresh, so the exit had to carry the verdict"


def test_an_ollama_off_this_machine_is_refused_before_anything_is_read(lab, monkeypatch):
    seed_lab(lab)
    monkeypatch.setenv(lease.OLLAMA_URL_ENV, "http://10.0.0.7:11434")
    assert T.ollama_url() == "http://10.0.0.7:11434"
    assert T.run(lab.base, post=FakeOllama(), get=lambda url: pytest.fail("asked an Ollama off this machine")) == T.REFUSED


def test_a_model_ollama_does_not_list_is_refused(lab):
    seed_lab(lab)
    assert T.run(lab.base, model="qwen9:huge", post=FakeOllama(), get=lambda url: tags()) == T.REFUSED


def test_a_run_with_no_time_left_asks_nothing_and_reads_inconclusive(lab, monkeypatch):
    seed_lab(lab)
    fake = FakeOllama()
    assert T.run(lab.base, post=fake, get=lambda url: tags(), minutes_left=lambda: 0) == 0
    report = T.newest_report(lab.base)
    assert fake.bodies == [] and report["stopped"] == "time" and report["verdict"] == "INCONCLUSIVE"


def test_the_deadline_stops_the_run_before_a_call_it_would_cut(lab, monkeypatch):
    seed_lab(lab)
    # Minutes left as the run goes on: the start needs the call timeout, then each call its own pace; three fit.
    left = iter([T.CALL_TIMEOUT_S / 60 + T.STOP_MARGIN_MIN, 30, 20, 10, T.STOP_MARGIN_MIN])
    fake = FakeOllama()
    assert T.run(lab.base, post=fake, get=lambda url: tags(), minutes_left=lambda: next(left)) == 0
    report = T.newest_report(lab.base)
    assert len(fake.bodies) == 3 and report["stopped"] == "time" and report["answered"] == 3 * T.TEACHER_BATCH


def test_a_call_starts_only_while_the_time_left_covers_it():
    assert infer.lease_module().run_minutes_left() is None, "no lease: nothing will stop the run"
    assert not T.call_fits(5, None), "the first call may take the whole timeout"
    assert T.call_fits(5, 60.0) and not T.call_fits(2.5, 60.0) and T.call_fits(None, None)


def test_a_non_level_family_is_told_not_to_think():
    teacher = T.Teacher("qwen3.5:9b", DIGEST, "qwen35", 7.0)
    assert T.chat_body(teacher, [])["think"] is False and T.Teacher("gpt-oss:20b", DIGEST, "gpt-oss", 14.7).level_only


def test_kappa_ci_needs_twenty_pairs_and_is_seeded():
    assert T.kappa_ci([(True, True)] * 19)["kappa"] is None
    pairs = [(True, True)] * 12 + [(False, False)] * 12 + [(True, False)] * 3 + [(False, True)] * 3
    one, two = T.kappa_ci(pairs, 2000), T.kappa_ci(pairs, 2000)
    assert one == two and one["kappa"] == pytest.approx(0.6, abs=0.01)
    assert one["ci95"][0] < one["kappa"] < one["ci95"][1]


def pair(kappa, ci=None):
    return {"n": 40, "kappa": kappa, "agree": None, "ci95": ci, "confusion": {}}


@pytest.mark.parametrize(
    ("coverage", "t_s", "t_f", "s_f", "verdict"),
    [
        (1.0, 0.80, 0.55, [0.50, 0.70], "PASS"),
        (1.0, 0.80, 0.75, [0.50, 0.70], "PASS"),  # above Sonnet's own interval is better, never a miss
        (1.0, 0.74, 0.65, [0.50, 0.70], "FAIL"),  # under the κ bar vs Sonnet
        (1.0, 0.90, 0.40, [0.50, 0.70], "FAIL"),  # agrees with Sonnet, not with the operator
        (0.90, 0.90, 0.65, [0.50, 0.70], "INCONCLUSIVE"),
        (1.0, 0.90, None, [0.50, 0.70], "INCONCLUSIVE"),
        (1.0, 0.90, 0.65, None, "INCONCLUSIVE"),
    ],
)
def test_the_bar(coverage, t_s, t_f, s_f, verdict):
    pairs = {"teacher_sonnet": pair(t_s), "teacher_operator": pair(t_f), "sonnet_operator": pair(0.6, s_f)}
    assert T.bar_verdict(coverage, pairs) == verdict


def test_the_verdict_probe_reads_only_this_runs_report(lab, monkeypatch):
    seed_lab(lab)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "a1b2c3d4e5f6")
    T.run(lab.base, post=FakeOllama(), get=lambda url: tags())
    report = T.newest_report(lab.base)
    probe = T.verdict_probe(report, "a1b2c3d4e5f6")
    assert probe["verdict"] == "PASS" and probe["model_digest"] == "gpt-oss:20b+0a1b2c3d4e5f"
    assert set(probe["headline"]) <= {"cases", "kappa", "accuracy", "latency_ms_p50", "latency_ms_p95"}
    assert probe["dataset_version"] == report["dataset_version"]
    assert T.verdict_probe(report, "000000000000")["verdict"] == "INCONCLUSIVE", "another run's report never speaks for this one"
    assert T.verdict_probe(None, None)["verdict"] == "INCONCLUSIVE"


def test_check_is_fresh_until_a_grade_lands(lab, monkeypatch):
    sonnet_keys, _ = seed_lab(lab)
    assert T.check(lab.base, get=lambda url: tags())[0] is False
    T.run(lab.base, post=FakeOllama(), get=lambda url: tags())
    assert T.check(lab.base, get=lambda url: tags())[0] is True
    assert T.check(lab.base, get=lambda url: tags(digest="cd" * 32))[0] is False
    lab.operator(sonnet_keys[35], "flip", against_open=False)  # a key the operator had not graded
    fresh, reason = T.check(lab.base, get=lambda url: tags())
    assert not fresh and "labels moved" in reason


def test_the_cli_reads_never_create_the_tree(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(paths.DATA_ENV, str(tmp_path))
    assert cli.main(["teacher-kappa", "--verdict-json"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["verdict"] == "INCONCLUSIVE"
    assert cli.main(["teacher-kappa", "--check"]) == 1
    assert not (tmp_path / "laya").exists()
    assert cli.reads_only(SimpleNamespace(command="teacher-kappa", check=False, verdict_json=False)) is False
