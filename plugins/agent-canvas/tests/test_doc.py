"""A JSON canvas: the validator agrees with Zod, bad blocks are cut, answers are checked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_canvas.delivery import REPLY_MAX
from agent_canvas.doc import (
    check_answers,
    collect_questions,
    contract,
    format_answers,
    load_doc,
    summary,
    validate,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "docs.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", FIXTURES, ids=[fixture["name"] for fixture in FIXTURES])
def test_the_validator_agrees_with_zod(fixture):
    """schemas/tests/agent-canvas.test.ts runs Zod over the same file."""
    issues = validate(fixture["doc"])
    assert (not issues) is fixture["valid"], [issue.as_json() for issue in issues]


def test_the_schema_twin_is_the_generated_one():
    assert contract()["_source"] == "schemas/src/agent-canvas.ts"
    assert contract()["_generated"] == "DO NOT EDIT — regenerate via schemas/scripts/export-agent-canvas.ts"
    assert set(contract()["question_types"]) == {"choice", "form"}


def doc_text(*blocks, **extra) -> str:
    return json.dumps({"blocks": list(blocks), **extra})


def test_an_unknown_block_is_cut_and_named():
    loaded = load_doc(doc_text({"type": "text", "text": "kept"}, {"type": "chart"}))
    assert loaded.doc == {"blocks": [{"type": "text", "text": "kept"}]}
    [issue] = loaded.issues
    assert (issue.code, issue.as_json()["path"]) == ("unknown-block", "blocks[1]")
    assert '"chart"' in issue.message and "heading" in issue.message


def test_a_misspelt_key_names_the_real_keys():
    loaded = load_doc(doc_text({"type": "table", "colums": ["a"], "rows": []}, {"type": "text", "text": "x"}))
    codes = {(issue.code, issue.as_json()["path"]) for issue in loaded.issues}
    assert ("unknown-key", "blocks[0].colums") in codes
    assert ("missing-required", "blocks[0].columns") in codes
    assert loaded.doc["blocks"] == [{"type": "text", "text": "x"}]


def test_a_bad_block_inside_a_card_cuts_only_that_block():
    loaded = load_doc(doc_text({"type": "card", "blocks": [{"type": "text", "text": "ok"}, {"type": "nope"}]}))
    assert loaded.doc["blocks"] == [{"type": "card", "blocks": [{"type": "text", "text": "ok"}]}]
    assert [issue.as_json()["path"] for issue in loaded.issues] == ["blocks[0].blocks[1]"]


def test_a_card_emptied_by_the_cut_goes_too_and_only_the_cause_is_reported():
    loaded = load_doc(doc_text({"type": "card", "blocks": [{"type": "nope"}]}, {"type": "text", "text": "x"}))
    assert loaded.doc["blocks"] == [{"type": "text", "text": "x"}]
    assert [issue.code for issue in loaded.issues] == ["unknown-block"]


def test_a_bad_tab_body_cuts_the_block_in_the_tab():
    tabs = {
        "type": "tabs",
        "tabs": [
            {"label": "A", "blocks": [{"type": "text", "text": "a"}, {"type": "x"}]},
            {"label": "B", "blocks": [{"type": "text", "text": "b"}]},
        ],
    }
    loaded = load_doc(doc_text(tabs))
    assert loaded.doc["blocks"][0]["tabs"][0]["blocks"] == [{"type": "text", "text": "a"}]
    assert loaded.issues[0].as_json()["path"] == "blocks[0].tabs[0].blocks[1]"


def test_a_bad_title_and_an_unknown_root_key_are_dropped_not_fatal():
    loaded = load_doc(doc_text({"type": "text", "text": "x"}, title="", theme="dark"))
    assert loaded.doc == {"blocks": [{"type": "text", "text": "x"}]}
    assert {issue.code for issue in loaded.issues} == {"too-short", "unknown-key"}


@pytest.mark.parametrize(
    "text, code",
    [
        ("{not json", "parse-failed"),
        ("NaN", "parse-failed"),
        ('{"blocks": []}', "too-few"),
        ('{"title": "x"}', "missing-required"),
        ('{"blocks": [{"type": "nope"}]}', "unknown-block"),
    ],
)
def test_a_root_that_is_wrong_renders_nothing(text, code):
    loaded = load_doc(text)
    assert loaded.doc is None
    assert loaded.issues[0].code == code


def test_a_duplicate_question_id_cuts_the_later_question():
    loaded = load_doc(
        doc_text(
            {"type": "choice", "id": "a", "q": "First", "options": ["y"]},
            {"type": "choice", "id": "a", "q": "Second", "options": ["y"]},
        )
    )
    assert [question.q for question in loaded.questions] == ["First"]
    assert loaded.issues[0].code == "duplicate-id"


def test_question_ids_follow_the_ts_walk():
    """Same doc as the TS test `question ids: explicit, else q<n>…`."""
    doc = {
        "blocks": [
            {"type": "choice", "q": "a", "options": ["x"]},
            {
                "type": "tabs",
                "tabs": [
                    {
                        "label": "A",
                        "blocks": [
                            {"type": "form", "id": "layout", "q": "b", "fields": [{"type": "toggle", "name": "t", "label": "T"}]}
                        ],
                    },
                    {"label": "B", "blocks": [{"type": "card", "blocks": [{"type": "choice", "q": "c", "options": ["x"]}]}]},
                ],
            },
        ]
    }
    questions, issues = collect_questions(doc)
    assert [question.id for question in questions] == ["q1", "layout", "q3"] and issues == []
    assert summary(questions) == "a (+2)"


def test_the_emoji_limit_counts_code_points_like_zod():
    assert not validate({"blocks": [{"type": "heading", "text": "😀" * 120}]})
    assert validate({"blocks": [{"type": "heading", "text": "😀" * 121}]})[0].code == "too-long"


# -- answers ---------------------------------------------------------------

QUESTIONS = load_doc(
    doc_text(
        {"type": "choice", "id": "ship", "q": "Ship it?", "options": ["yes", {"label": "LATER", "reply": "defer"}]},
        {"type": "choice", "id": "areas", "q": "Which areas?", "options": ["api", "ui"], "multi": True, "other": False},
        {
            "type": "form",
            "id": "layout",
            "q": "Layout",
            "fields": [
                {"type": "text", "name": "note", "label": "Note"},
                {"type": "select", "name": "pick", "label": "Pick", "options": ["A", "B"]},
                {"type": "toggle", "name": "dark", "label": "Dark"},
                {"type": "rank", "name": "order", "label": "Order", "options": ["x", "y", "z"]},
            ],
        },
    )
).questions


@pytest.mark.parametrize(
    "answers",
    [
        {"ship": "yes"},
        {"ship": "defer"},
        {"ship": "only after the 3pm window"},  # free text: `other` defaults on
        {"areas": ["ui", "api"]},
        {"layout": {"note": "two\nlines", "pick": "B", "dark": False, "order": ["z", "x", "y"]}},
        {"layout": {}},  # a form sent as-is
    ],
)
def test_answers_that_fit(answers):
    assert check_answers(QUESTIONS, answers) is None


@pytest.mark.parametrize(
    "answers, why",
    [
        ({}, "answers required"),
        ([], "answers required"),
        ({"nope": "x"}, 'no question "nope"'),
        ({"ship": "  "}, "empty"),
        ({"ship": ["yes"]}, "expected text"),
        ({"areas": ["db"]}, "not an option"),  # `other: false`
        ({"areas": "api"}, "list of picks"),
        ({"areas": ["api", "api"]}, "repeated"),
        ({"layout": {"color": "red"}}, 'unknown field "color"'),
        ({"layout": {"pick": "C"}}, "not one of its options"),
        ({"layout": {"dark": "yes"}}, "true or false"),
        ({"layout": {"order": ["x", "y"]}}, "reordered"),
        ({"layout": {"note": 3}}, "expects text"),
    ],
)
def test_answers_that_do_not(answers, why):
    assert why in (check_answers(QUESTIONS, answers) or "")


def test_one_line_for_every_question_with_skips_named():
    line = format_answers(QUESTIONS, {"ship": "yes", "layout": {"note": "a\nb", "dark": True}})
    assert line == ('canvas › Ship it? → yes · Which areas? → (skipped) · Layout → {"note":"a\\nb","dark":true}')
    assert "\n" not in line


def test_one_question_reads_like_the_markup_canvas():
    assert format_answers(QUESTIONS[:1], {"ship": "defer"}) == "canvas › Ship it? → defer"


def test_a_line_too_long_to_type_is_refused():
    assert format_answers(QUESTIONS, {"ship": "x" * 1400, "layout": {"note": "y" * 1000}}) is None
    assert len(format_answers(QUESTIONS, {"ship": "x" * 100})) < REPLY_MAX


def test_the_skill_example_is_a_valid_doc_and_reads_as_promised():
    import re

    skill = (Path(__file__).resolve().parents[3] / ".claude/skills/agent-canvas/SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"## Example.*?```json\n(.*?)```", skill, re.DOTALL)
    assert match, "SKILL.md example block not found"
    loaded = load_doc(match.group(1))
    assert loaded.issues == [] and [question.id for question in loaded.questions] == ["first", "scope"]
    line = format_answers(loaded.questions, {"first": "yes", "scope": {"layout": "under", "order": ["plugin", "rack", "docs"]}})
    assert f"`{line}`" in skill
