"""The local deep pass: Laya judges open-work and repeats; the overlap guard still gates collapses;
the gate keeps the audit on Claude until the coordinator has its own fine-tune; and `_rank_scan`
falls back to Claude — silently when Laya was never enabled for it, visibly otherwise."""

from __future__ import annotations

import pytest

from coordinator import plugin as plugin_module
from coordinator.laya_rank import (
    FINE_TUNE_MODEL,
    NOT_ENABLED,
    OPEN_WORK_AT,
    OPEN_WORK_QUESTION,
    SAME_TASK_AT,
    SAME_TASK_QUESTION,
    run_laya_rank,
)
from coordinator.rank import apply_ranking
from coordinator.signals import WORK_CLASS_OPEN, WORK_CLASS_ROUTINE

CANDIDATES = [
    {"key": "a", "headline": "write the migration for the habits table", "label": "Next:", "age_hours": 3},
    {"key": "b", "headline": "restart tldrack so docs-reader mounts", "label": "Next:", "age_hours": 5},
    {"key": "c", "headline": "restart the tldrack so the docs-reader plugin mounts", "label": "Next:", "age_hours": 9},
    {"key": "d", "headline": "decide the habits retention policy", "label": "Next:", "age_hours": 1},
]


class FakeLaya:
    """Answers the plugin routes; `open_p`/`same_p` script Laya's probabilities. Before `/start`,
    `/status` reports a stopped Laya with its registered `models`; after it, the scripted `states`."""

    def __init__(
        self,
        open_p,
        same_p=lambda first, second: 0.0,
        states=("ready",),
        start_status=200,
        models=("english", FINE_TUNE_MODEL),
        status_status=200,
    ):
        self.open_p, self.same_p = open_p, same_p
        self.states = list(states)
        self.start_status, self.status_status = start_status, status_status
        self.models = list(models)
        self.started = False
        self.paths: list[str] = []
        self.decides: list[dict] = []

    def __call__(self, path, body):
        self.paths.append(path)
        if path == "/start":
            self.started = True
            return self.start_status, ({"state": self.states.pop(0)} if self.start_status == 200 else {})
        if path == "/status":
            if self.status_status != 200:
                return self.status_status, {}
            state = (self.states.pop(0) if self.states else "ready") if self.started else "stopped"
            return 200, {"state": state, "models": self.models}
        assert path == "/decide"
        self.decides.append(body)
        question = body["questions"]["q"]
        state = body["state"]
        if question == OPEN_WORK_QUESTION:
            p = self.open_p(state["headline"])
        else:
            assert question == SAME_TASK_QUESTION
            p = self.same_p(state["first"], state["second"])
        return (200, {"answers": {"q": {"noul": p}}}) if p is not None else (503, {"error": "warming"})


def no_sleep(_):
    pass


def test_open_work_is_a_threshold_on_lay_as_probability():
    laya = FakeLaya(open_p=lambda headline: 0.9 if "habits" in headline else 0.1)
    ranking, error = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert error is None
    assert ranking["a"]["work_class"] == WORK_CLASS_OPEN
    assert ranking["b"]["work_class"] == WORK_CLASS_ROUTINE


def test_only_guard_admitted_pairs_are_asked_and_collapse_onto_the_earlier_row():
    laya = FakeLaya(open_p=lambda _: 0.2, same_p=lambda first, second: 0.95)
    ranking, _ = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    pairs = [(d["state"]["first"], d["state"]["second"]) for d in laya.decides if "first" in d["state"]]
    # b/c share "restart tldrack docs-reader mounts" → asked; a/d share only "habits" → never asked,
    # however sure Laya would have been.
    assert pairs == [(CANDIDATES[1]["headline"], CANDIDATES[2]["headline"])]
    assert ranking["c"]["duplicate_of"] == "b"
    assert ranking["d"]["duplicate_of"] is None


def test_a_low_same_task_probability_keeps_both_rows():
    laya = FakeLaya(open_p=lambda _: 0.2, same_p=lambda first, second: 0.6)
    ranking, _ = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert ranking["c"]["duplicate_of"] is None


def test_waits_out_a_cold_start():
    laya = FakeLaya(open_p=lambda _: 0.9, states=("starting", "starting", "ready"))
    ranking, error = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert error is None and ranking


def test_gives_up_on_a_start_that_outlasts_the_wait():
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]

    laya = FakeLaya(open_p=lambda _: 0.9, states=["starting"] * 50)
    ranking, error = run_laya_rank(CANDIDATES, laya, sleep=no_sleep, clock=clock)
    assert ranking == {} and "not ready" in error


def test_no_plugin_falls_back_silently():
    laya = FakeLaya(open_p=lambda _: 0.9, status_status=404)
    assert run_laya_rank(CANDIDATES, laya, sleep=no_sleep) == ({}, NOT_ENABLED)
    assert laya.paths == ["/status"]


def test_a_plugin_that_fails_its_status_is_a_visible_fallback():
    laya = FakeLaya(open_p=lambda _: 0.9, status_status=500)
    ranking, error = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert ranking == {} and error == "the laya plugin answered 500"
    assert "/start" not in laya.paths


@pytest.mark.parametrize("models", [("english",), (), ("english", "tldrack-agent-open-work")])
def test_without_the_coordinator_fine_tune_nothing_starts_and_nothing_is_said(models):
    """Research §05: without this gate, installing Laya would switch audits to the near-chance base model."""
    laya = FakeLaya(open_p=lambda _: 0.9, models=models)
    assert run_laya_rank(CANDIDATES, laya, sleep=no_sleep) == ({}, NOT_ENABLED)
    assert laya.paths == ["/status"] and laya.decides == []


def test_every_question_pins_the_fine_tune_and_is_tagged_for_the_ledger():
    laya = FakeLaya(open_p=lambda _: 0.2, same_p=lambda first, second: 0.95)
    run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert {d["model"] for d in laya.decides} == {FINE_TUNE_MODEL}
    open_tags = [d["ledger"] for d in laya.decides if "headline" in d["state"]]
    assert open_tags == [{"surface": "coordinator", "subject": key, "threshold": OPEN_WORK_AT} for key in "abcd"]
    pair_tags = [d["ledger"] for d in laya.decides if "first" in d["state"]]
    assert pair_tags == [{"surface": "coordinator", "subject": "b|c", "threshold": SAME_TASK_AT}]


def test_a_key_the_ledger_would_refuse_goes_untagged_not_unasked():
    long_key = "k" * 129
    candidates = [
        {"key": long_key, "headline": "write the migration", "label": "Next:"},
        {"key": "bad key!", "headline": "write the other migration", "label": "Next:"},
    ]
    laya = FakeLaya(open_p=lambda _: 0.9)
    ranking, error = run_laya_rank(candidates, laya, sleep=no_sleep)
    assert error is None and set(ranking) == {long_key, "bad key!"}
    assert [d["ledger"] for d in laya.decides if "headline" in d["state"]] == [
        {"surface": "coordinator", "threshold": OPEN_WORK_AT}
    ] * 2


def test_rows_keep_lay_as_raw_probabilities():
    laya = FakeLaya(open_p=lambda headline: 0.91 if "habits" in headline else 0.12, same_p=lambda first, second: 0.95)
    ranking, _ = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert ranking["a"] == {"work_class": WORK_CLASS_OPEN, "duplicate_of": None, "p": 0.91, "dup_p": None}
    assert ranking["c"] == {"work_class": WORK_CLASS_ROUTINE, "duplicate_of": "b", "p": 0.12, "dup_p": 0.95}


def test_dup_p_keeps_the_closest_call_when_nothing_collapses():
    laya = FakeLaya(open_p=lambda _: 0.2, same_p=lambda first, second: 0.6)
    ranking, _ = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert ranking["c"]["dup_p"] == 0.6 and ranking["c"]["duplicate_of"] is None
    assert ranking["b"]["dup_p"] is None  # never compared with an earlier row


def test_an_unanswered_question_keeps_p_none():
    laya = FakeLaya(open_p=lambda headline: None if "habits" in headline else 0.3)
    ranking, _ = run_laya_rank(CANDIDATES, laya, sleep=no_sleep)
    assert ranking["a"]["p"] is None and ranking["a"]["work_class"] is None
    assert ranking["b"]["p"] == 0.3


def test_apply_ranking_copies_lay_as_p_and_the_claude_path_has_none():
    findings = [dict(c, kind="stated-next-step") for c in CANDIDATES]
    laya_ranked = apply_ranking(
        findings,
        {
            "a": {"work_class": WORK_CLASS_OPEN, "duplicate_of": None, "p": 0.91234, "dup_p": None},
            "b": {"work_class": None, "duplicate_of": None, "p": None, "dup_p": None},
        },
    )
    by_key = {f["key"]: f for f in laya_ranked}
    assert by_key["a"]["laya_p"] == 0.912
    assert "laya_p" not in by_key["b"]
    claude_ranked = apply_ranking(findings, {"a": {"work_class": WORK_CLASS_OPEN, "duplicate_of": None}})
    assert all("laya_p" not in f for f in claude_ranked)


def test_rank_scan_prefers_laya_and_says_so(monkeypatch):
    monkeypatch.setattr(plugin_module, "run_rank", lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude must not run")))
    findings = [dict(c, kind="stated-next-step") for c in CANDIDATES]
    scan = plugin_module._rank_scan({"findings": findings}, profiles=None, laya=FakeLaya(open_p=lambda _: 0.9))
    assert scan["ranked"] is True and scan["ranked_by"] == "laya" and scan["error"] is None
    assert {f["laya_p"] for f in scan["findings"]} == {0.9}


def test_rank_scan_falls_back_to_claude_and_keeps_lay_as_reason(monkeypatch):
    monkeypatch.setattr(
        plugin_module, "run_rank", lambda *a, **k: ({"a": {"work_class": WORK_CLASS_OPEN, "duplicate_of": None}}, None)
    )
    findings = [dict(c, kind="stated-next-step") for c in CANDIDATES]
    scan = plugin_module._rank_scan(
        {"findings": findings}, profiles=None, laya=FakeLaya(open_p=lambda _: 0.9, states=("not_installed",))
    )
    assert scan["ranked_by"] == "claude"
    assert scan["error"] == "Laya is not installed — used Claude instead"


def test_rank_scan_without_the_fine_tune_falls_back_to_claude_silently(monkeypatch):
    monkeypatch.setattr(
        plugin_module, "run_rank", lambda *a, **k: ({"a": {"work_class": WORK_CLASS_OPEN, "duplicate_of": None}}, None)
    )
    findings = [dict(c, kind="stated-next-step") for c in CANDIDATES]
    laya = FakeLaya(open_p=lambda _: 0.9, models=("english",))
    scan = plugin_module._rank_scan({"findings": findings}, profiles=None, laya=laya)
    assert scan["ranked_by"] == "claude" and scan["error"] is None
    assert "/start" not in laya.paths
    assert all("laya_p" not in f for f in scan["findings"])
