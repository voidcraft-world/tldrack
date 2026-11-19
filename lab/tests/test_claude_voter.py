"""`claude_voter`: the votes file, its reader, and the spec that names it. No call is ever made here."""

from __future__ import annotations

import json

import pytest

from laya_lab import bench, claude_voter, compare
from laya_lab.golden import SUITES
from laya_bridge import rows

from conftest import SUITE

SUITE_OBJECT = SUITES[SUITE]
MODEL = "claude-opus-5-5"
OPEN, DONE = "Phase C is not started.", "All three phases shipped."


def write_votes(lab, votes: list[dict]) -> None:
    lab.write_jsonl("data/agent-open-work-votes.jsonl", votes)


def vote_row(state: str, p: float, arm: str = "rubric", **over) -> dict:
    return {
        "state": rows.state_key(state),
        "p": p,
        "ms": 1200.0,
        "model": MODEL,
        "arm": arm,
        "prompt": claude_voter.prompt_digest(SUITE_OBJECT, arm),
        "qv": SUITE_OBJECT.question_version,
        "by": MODEL,
        **over,
    }


def test_a_votes_target_names_a_model_and_an_arm():
    assert claude_voter.parse_votes("votes:opus") == ("opus", "rubric")
    assert claude_voter.parse_votes(f"votes:{MODEL}/question") == (MODEL, "question")
    for target in ("opus", "votes:", "votes:gpt-9", "votes:opus/haiku", "votes:../x"):
        with pytest.raises(ValueError):
            claude_voter.parse_votes(target)


def test_both_spec_parsers_take_a_votes_target_and_refuse_a_bad_one():
    assert compare.parse_spec(f"opus=votes:{MODEL}") == ("opus", f"votes:{MODEL}")
    assert bench.parse_voter(f"opus=votes:{MODEL}/question,label=OPUS 5.5")["url"] == f"votes:{MODEL}/question"
    for parse in (compare.parse_spec, bench.parse_voter):
        with pytest.raises(compare.Refused):
            parse("opus=votes:gpt-9")


def test_the_two_arms_ask_for_a_probability_under_different_prompts():
    rubric, question = (claude_voter.system_prompt(SUITE_OBJECT, arm) for arm in claude_voter.ARMS)
    assert SUITE_OBJECT.question["instructions"] in question and SUITE_OBJECT.question["instructions"] not in rubric
    assert rubric.endswith(claude_voter.ASK_P) and question.endswith(claude_voter.ASK_P)
    assert rubric.count("Answer for every id given.") == 1, "the rubric's own closing line is replaced, not doubled"
    assert claude_voter.prompt_digest(SUITE_OBJECT, "rubric") != claude_voter.prompt_digest(SUITE_OBJECT, "question")


def test_a_batch_carries_the_state_and_its_key_and_nothing_else():
    document = json.loads(claude_voter.batch_prompt([OPEN, DONE]).split("\n\n", 1)[1])
    assert document == {"items": [{"id": rows.state_key(text), "message": text} for text in (OPEN, DONE)]}


def test_a_reply_keeps_only_asked_ids_with_a_probability():
    wanted = {rows.state_key(OPEN), rows.state_key(DONE)}
    reply = json.dumps(
        {
            "votes": [
                {"id": rows.state_key(OPEN), "p": 0.9},
                {"id": rows.state_key(DONE), "p": 1.4},
                {"id": "someone-else", "p": 0.2},
                {"id": rows.state_key(DONE), "p": True},
                "torn",
            ]
        }
    )
    assert claude_voter.read_reply(reply, wanted) == {rows.state_key(OPEN): 0.9}
    assert claude_voter.read_reply("not json", wanted) == {}


def test_the_reader_keeps_this_model_arm_prompt_and_version_and_the_last_row_wins(lab):
    write_votes(
        lab,
        [
            vote_row(OPEN, 0.2),
            vote_row(OPEN, 0.8),
            vote_row(DONE, 0.1, arm="question"),
            vote_row(DONE, 0.3, model="claude-sonnet-5-5"),
            vote_row(DONE, 0.4, prompt="an-older-prompt"),
            vote_row(DONE, 0.5, qv=SUITE_OBJECT.question_version + 1),
            vote_row(DONE, 7),
        ],
    )
    assert claude_voter.read_votes(lab.base, SUITE_OBJECT, MODEL, "rubric") == {rows.state_key(OPEN): (0.8, 1200.0)}
    assert claude_voter.read_votes(lab.base, SUITE_OBJECT, MODEL, "question") == {rows.state_key(DONE): (0.1, 1200.0)}
    assert claude_voter.read_votes(lab.base, SUITE_OBJECT, "claude-fable-5-2", "rubric") == {}


def test_the_router_answers_from_the_file_and_names_the_command_for_an_unasked_state(lab):
    write_votes(lab, [vote_row(OPEN, 0.8)])
    router = claude_voter.VotesRouter(lab.base, SUITE_OBJECT, MODEL, "rubric")
    assert router.predict(OPEN, {"q": SUITE_OBJECT.question})["answers"]["q"]["noul"] == 0.8
    assert router.last_ms == 1200.0
    with pytest.raises(claude_voter.Unasked, match="laya-lab claude-vote"):
        router.predict(DONE, {"q": SUITE_OBJECT.question})


def test_a_timed_votes_router_reports_the_recorded_cost_not_the_file_read(lab):
    write_votes(lab, [vote_row(OPEN, 0.8)])
    timed = compare.Timed(compare.endpoint_router(lab.base, SUITE_OBJECT, f"votes:{MODEL}"))
    timed.predict(OPEN, {"q": SUITE_OBJECT.question})
    assert timed.ms == [1200.0]


def test_the_bench_scores_a_votes_voter_and_skips_one_with_a_state_unasked(lab, public_golden):
    write_votes(lab, [vote_row(OPEN, 0.9), vote_row(DONE, 0.1)])
    plan = bench.plan_voters(lab.base, SUITE_OBJECT, [f"opus=votes:{MODEL}", f"half=votes:{MODEL}/question"])
    voters = {voter.name: voter for voter in plan.voters}
    sets = {"golden": bench.golden_rows(SUITE, SUITE_OBJECT)}
    answers = bench.ask(voters["opus"], sets)["golden"]
    assert (answers.probabilities, answers.ms) == ([0.9, 0.1], [1200.0, 1200.0])
    with pytest.raises(claude_voter.Unasked):
        bench.ask(voters["half"], sets)


def test_a_dry_run_counts_what_it_would_ask_and_calls_nothing(lab, public_golden, capsys):
    write_votes(lab, [vote_row(OPEN, 0.9)])
    assert claude_voter.vote(lab.base, SUITE, model=MODEL, arms=("rubric",), dry_run=True) == 0
    out = capsys.readouterr().out
    assert "1 of 2 voted, 1 to ask" in out and "1/2 states voted" in out
