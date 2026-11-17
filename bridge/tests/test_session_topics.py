import json
import subprocess

import pytest

from tldrack.claude_cli import PlanLedger
from tldrack.session_topics import (
    TOPIC_MODEL,
    TopicNamer,
    FILLER_WORDS,
    TOPIC_SYSTEM_PROMPT,
    TopicResult,
    ask_topic,
    build_topic_prompt,
    parse_topic_answer,
    pick_unique,
    title_label,
    title_slug,
    valid_slug,
)


@pytest.mark.parametrize(
    "value,want",
    [
        ("docs-voice", "docs-voice"),
        ("MCP-tools", "MCP-tools"),
        ("rack names", "rack-names"),
        ("worker_replays", "worker-replays"),
        ("  harbor-lamps  ", "harbor-lamps"),
        ("a-b-c", "a-b-c"),
        ("a-b-c-d", None),
        ("x" * 25, None),
        ("rack/names", None),
        ("ignore all; rm -rf", None),
        ("", None),
        (None, None),
        (12, None),
        ("--", None),
    ],
)
def test_valid_slug(value, want):
    assert valid_slug(value) == want


def test_pick_unique_takes_the_first_free_candidate():
    assert pick_unique(["rack", "rack-names", "rack-topic-names"], ["Rack"]) == "rack-names"
    assert pick_unique(["bad label!", "docs-voice"], []) == "docs-voice"


def test_pick_unique_suffixes_when_every_candidate_is_held():
    assert pick_unique(["rack", "rack-names"], ["rack", "RACK-NAMES", "rack-2"]) == "rack-3"
    assert pick_unique(["nope!"], []) is None
    assert pick_unique("rack", []) is None


def test_the_prompt_lists_what_is_taken_and_bounds_the_request():
    text = build_topic_prompt("\x1b[31mFix the rack" + "x" * 5000, ["docs-voice", "rack-names"])
    assert text.startswith("TAKEN: docs-voice, rack-names\n")
    assert "\x1b" not in text and len(text) < 3200
    assert build_topic_prompt("hi", []).startswith("TAKEN: (none)")


def stream(answer=None, *, source="none", overage=False, utilization=0.5, error=None):
    """The stream-json lines `run_agent` reads: init, the plan's rate limit, the result."""
    result = {"type": "result", "is_error": error is not None, "result": error or json.dumps(answer), "total_cost_usd": 0.0019}
    if answer is not None:
        result["structured_output"] = answer
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init", "apiKeySource": source, "model": TOPIC_MODEL},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed",
                    "isUsingOverage": overage,
                    "unifiedWindows": {"seven_day": {"utilization": utilization, "resetsAt": 4102444800}},
                },
            },
            result,
        )
    )


def test_parse_topic_answer():
    assert parse_topic_answer('{"candidates": ["a", 3, "b"], "vague": false}') == (["a", "b"], False, None)
    assert parse_topic_answer('{"candidates": [], "vague": true}') == ([], True, None)
    assert parse_topic_answer("prose")[2] == "the topic call returned nothing usable"
    assert parse_topic_answer("")[2] == "the topic call returned nothing usable"


class FakeProfiles:
    def __init__(self, billing="subscription", pick="claude-b", pick_billing=None, cli=True):
        self.billing, self.pick, self.pick_billing, self.cli = billing, pick, pick_billing, cli
        self.calls = []

    def pick_launch(self):
        return self.pick

    def auth_info(self, profile):
        if profile is None or profile == "claude":
            return {"profile": "claude", "billing": self.billing, "signed_in": True}
        if profile == "codex":
            return None
        return {"profile": profile, "billing": self.pick_billing or self.billing, "signed_in": True}

    def headless_call(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        return {"profile": profile, "argv": ["claude", "-p"], "env": {}} if self.cli else None


def runner(stdout, returncode=0):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    run.seen = seen
    return run


def ask(profiles, prompt="x", taken=(), *, run, plans=None):
    return ask_topic(profiles, prompt, list(taken), plans=plans or PlanLedger(), run=run)


def test_ask_names_the_topic_on_the_auto_account():
    profiles = FakeProfiles()
    run = runner(stream({"candidates": ["docs-voice"], "vague": False}))
    result = ask(profiles, "fix the docs voice", ["rack-names"], run=run)
    assert result == TopicResult(label="docs-voice", profile="claude-b")
    profile, kwargs = profiles.calls[0]
    assert profile == "claude-b" and kwargs["model"] == TOPIC_MODEL and kwargs["json_schema"]
    assert kwargs["stream"] is True  # the stream carries the billing proof
    assert run.seen["input"].startswith("TAKEN: rack-names") and run.seen["timeout"] == 30


def test_ask_falls_back_to_the_default_account_when_auto_is_not_a_subscription():
    for profiles in (FakeProfiles(pick="codex"), FakeProfiles(pick_billing="api"), FakeProfiles(pick=None)):
        ask(profiles, run=runner(stream({"candidates": ["x"], "vague": False})))
        assert profiles.calls[0][0] == "claude"  # the default account, resolved


def test_ask_never_bills_an_api_key():
    profiles = FakeProfiles(billing="api", pick_billing="api")
    result = ask(profiles, run=runner("unused"))
    assert result.label is None and "subscription" in result.error and profiles.calls == []


def test_a_call_that_ran_in_overage_or_on_a_key_lands_nothing():
    overage = ask(FakeProfiles(), run=runner(stream({"candidates": ["x"], "vague": False}, overage=True)))
    assert overage.label is None and overage.error.startswith("plan_overage")
    keyed = ask(FakeProfiles(), run=runner(stream({"candidates": ["x"], "vague": False}, source="ANTHROPIC_API_KEY")))
    assert keyed.label is None and keyed.error.startswith("not_subscription")


def test_a_plan_at_its_limit_refuses_the_next_call():
    plans, profiles = PlanLedger(), FakeProfiles()
    ask(profiles, run=runner(stream({"candidates": ["x"], "vague": False}, utilization=0.99)), plans=plans)
    refused = ask(profiles, run=runner("unused"), plans=plans)
    assert refused.error.startswith("plan_limit") and len(profiles.calls) == 1


@pytest.mark.parametrize(
    "run,error",
    [
        (runner(stream(error="budget exceeded")), "agent_error: budget exceeded"),
        (runner(stream({"candidates": ["no good!"], "vague": False})), "the topic call named no usable label"),
    ],
)
def test_ask_failures_keep_the_placeholder(run, error):
    assert ask(FakeProfiles(), run=run).error == error


def test_ask_survives_a_timeout_a_missing_cli_and_no_profiles():
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    assert ask(FakeProfiles(), run=slow).error == "timeout: no answer in 30 s"
    assert ask(FakeProfiles(cli=False), run=runner("")).error.startswith("cli_missing")
    assert ask(None, run=runner("")).error


def test_ask_marks_a_vague_prompt():
    result = ask(FakeProfiles(), "hi", run=runner(stream({"candidates": [], "vague": True})))
    assert result.vague and result.label is None and result.error is None


def test_the_namer_reads_taken_when_each_job_runs():
    landed = {}
    taken_seen = []

    def ask(prompt, taken):
        taken_seen.append(list(taken))
        return TopicResult(label=pick_unique(["rack-names"], taken))

    def taken(session):
        return sorted(label for name, label in landed.items() if name != session)

    def on_result(session, epoch, result):
        landed[session] = result.label

    namer = TopicNamer(ask, taken, on_result, worker=False)
    namer.submit("vc-1", 1, "rack names")
    namer.submit("vc-2", 1, "rack names too")
    assert namer.drain() == 2
    assert landed == {"vc-1": "rack-names", "vc-2": "rack-names-2"}
    assert taken_seen == [[], ["rack-names"]]


def test_the_namer_keeps_the_newest_job_and_survives_a_raising_ask():
    results = []

    def ask(prompt, taken):
        if prompt == "boom":
            raise RuntimeError("nope")
        return TopicResult(label=prompt)

    namer = TopicNamer(ask, lambda session: [], lambda s, e, r: results.append((s, e, r)), worker=False)
    namer.submit("vc-1", 1, "first")
    namer.submit("vc-1", 2, "second")
    namer.submit("vc-2", 1, "boom")
    namer.drain()
    assert [(s, e, r.label) for s, e, r in results] == [("vc-1", 2, "second"), ("vc-2", 1, None)]
    assert "RuntimeError" in results[1][2].error


@pytest.mark.parametrize(
    "title,want",
    [
        ("Laya training data", "laya-training-data"),
        ("Add batch match attachments", "batch-match-attachments"),
        ("MCP tool lines for readers", "MCP-tool-lines"),
        ("LEAD fixes: auto-mode worker, lead topic name", "LEAD-auto-mode"),
        ("Lead wears a topic name after the operator's first prompt", "lead-wears-topic"),
        ("Updating the rack's filler handling", "rack-filler-handling"),
        ("supercalifragilistic expialidocious", "supercalifragilistic"),
        ("x" * 40, "x" * 24),
        ("\x1b[31mHarbor lamps", "harbor-lamps"),
        ("Fix the code", None),
        ("", None),
        ("!!!", None),
        (None, None),
        (7, None),
    ],
)
def test_title_slug_follows_the_topic_rules_without_a_call(title, want):
    assert title_slug(title) == want
    assert want is None or valid_slug(want) == want


def test_title_label_is_unique_against_the_rack():
    assert title_label("Laya training data", []) == "laya-training-data"
    assert title_label("Laya training data", ["LAYA-training-data"]) == "laya-training-data-2"
    assert title_label("Fix it", ["x"]) is None


def test_the_model_and_the_title_share_one_filler_list():
    assert all(word in TOPIC_SYSTEM_PROMPT for word in FILLER_WORDS)
    assert all(title_slug(word) is None for word in FILLER_WORDS)
