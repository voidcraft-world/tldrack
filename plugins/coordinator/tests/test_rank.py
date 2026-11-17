"""Deep-pass tests — the pure halves only.

`run_rank` shells out and is verified by hand against the real CLI; what is
tested here is everything that decides what a *garbage* answer can do to the
list, because the heuristic layer standing alone is the whole safety property.
"""

from __future__ import annotations

from coordinator.rank import (
    RANK_INSTRUCTIONS,
    RANK_MAX_CANDIDATES,
    apply_ranking,
    build_rank_prompt,
    parse_rank_response,
    rank_candidates,
    run_rank,
)


def finding(key: str, kind: str = "stated-next-step", severity: int = 4, headline: str | None = None) -> dict:
    return {
        "key": key,
        "kind": kind,
        "severity": severity,
        "headline": headline if headline is not None else f"do {key}",
        "label": key,
        "age_hours": 5.0,
        "detail": "a long detail nobody should send",
        "last_active": "2026-08-02T10:00:00Z",
        "work_class": None,
    }


# ── the payload ────────────────────────────────────────────────────────────


def test_candidates_are_compact_and_never_carry_a_transcript():
    payload = rank_candidates([finding("aaaaaaaa:stated-next-step")])
    assert set(payload[0]) == {"key", "headline", "label", "age_hours"}


def test_blocked_findings_are_never_sent_for_classification():
    # A live process at a prompt is neither routine nor a duplicate of anything.
    findings = [finding("aaaaaaaa:blocked", "blocked", 0), finding("bbbbbbbb:open-offer")]
    assert [c["key"] for c in rank_candidates(findings)] == ["bbbbbbbb:open-offer"]


def test_candidates_are_capped():
    findings = [finding(f"{index:08d}:open-offer") for index in range(RANK_MAX_CANDIDATES + 25)]
    assert len(rank_candidates(findings)) == RANK_MAX_CANDIDATES


def test_prompt_embeds_the_candidates_and_the_instructions_ask_for_bare_json():
    prompt = build_rank_prompt(rank_candidates([finding("aaaaaaaa:open-offer", "open-offer", 3)]))
    assert "aaaaaaaa:open-offer" in prompt
    assert "open-work" in RANK_INSTRUCTIONS and "routine" in RANK_INSTRUCTIONS
    assert "duplicate_of" in RANK_INSTRUCTIONS


# ── parsing a model answer ─────────────────────────────────────────────────


def test_parses_a_bare_array():
    ranking = parse_rank_response('[{"key": "a:x", "work_class": "open-work", "duplicate_of": null}]')
    assert ranking == {"a:x": {"work_class": "open-work", "duplicate_of": None}}


def test_parses_through_a_markdown_fence_and_surrounding_prose():
    text = 'Here you go:\n```json\n[{"key": "a:x", "work_class": "routine"}]\n```\n'
    assert parse_rank_response(text)["a:x"]["work_class"] == "routine"


def test_parses_an_items_wrapper():
    assert "a:x" in parse_rank_response('{"items": [{"key": "a:x", "work_class": "routine"}]}')


def test_an_out_of_vocabulary_work_class_becomes_none():
    ranking = parse_rank_response('[{"key": "a:x", "work_class": "URGENT!!!"}]')
    assert ranking["a:x"]["work_class"] is None


def test_junk_answers_parse_to_nothing_rather_than_raising():
    for text in ("", "   ", "I'm afraid I can't do that", "null", "[1, 2, 3]", '[{"no_key": 1}]', None):
        assert parse_rank_response(text) == {}


# ── folding a ranking into the findings ────────────────────────────────────


def test_open_work_is_promoted_and_routine_is_not():
    findings = [finding("aaaaaaaa:stated-next-step"), finding("bbbbbbbb:stated-next-step")]
    ranked = apply_ranking(
        findings,
        {
            "aaaaaaaa:stated-next-step": {"work_class": "open-work", "duplicate_of": None},
            "bbbbbbbb:stated-next-step": {"work_class": "routine", "duplicate_of": None},
        },
    )
    by_key = {f["key"]: f for f in ranked}
    assert by_key["aaaaaaaa:stated-next-step"]["severity"] == 2
    assert by_key["bbbbbbbb:stated-next-step"]["severity"] == 4
    assert by_key["bbbbbbbb:stated-next-step"]["work_class"] == "routine"


def test_duplicates_collapse_onto_the_survivor():
    # The real dedup case: one task, two sessions, nearly the same words.
    findings = [
        finding("aaaaaaaa:stated-next-step", headline="restart tldrack so docs-reader mounts"),
        finding("bbbbbbbb:stated-next-step", headline="restart the tldrack to pick up the docs plugin"),
    ]
    ranked = apply_ranking(
        findings,
        {
            "bbbbbbbb:stated-next-step": {"work_class": "routine", "duplicate_of": "aaaaaaaa:stated-next-step"},
        },
    )
    assert [f["key"] for f in ranked] == ["aaaaaaaa:stated-next-step"]


def test_a_duplicate_of_an_unrelated_finding_is_refused():
    # `duplicate_of` is the ONLY field that can delete a row, and the headlines
    # the classifier judges from are untrusted transcript text — so a collapse
    # the rows themselves do not support must not be honoured.
    findings = [
        finding("aaaaaaaa:stated-next-step", headline="run the tracker backfill against dev"),
        finding("bbbbbbbb:open-offer", "open-offer", 3, headline="want me to wire the pop-out window next?"),
    ]
    ranked = apply_ranking(
        findings,
        {
            "bbbbbbbb:open-offer": {"work_class": "routine", "duplicate_of": "aaaaaaaa:stated-next-step"},
        },
    )
    assert {f["key"] for f in ranked} == {"aaaaaaaa:stated-next-step", "bbbbbbbb:open-offer"}


def test_a_duplicate_chain_or_cycle_drops_nothing():
    # One hop only: if the survivor is itself a duplicate the answer is
    # incoherent, and losing a row to a confused classifier is the worse bug.
    same = "restart tldrack so docs-reader mounts"
    findings = [finding("aaaaaaaa:stated-next-step", headline=same), finding("bbbbbbbb:stated-next-step", headline=same)]
    ranked = apply_ranking(
        findings,
        {
            "aaaaaaaa:stated-next-step": {"work_class": None, "duplicate_of": "bbbbbbbb:stated-next-step"},
            "bbbbbbbb:stated-next-step": {"work_class": None, "duplicate_of": "aaaaaaaa:stated-next-step"},
        },
    )
    assert len(ranked) == 2


def test_a_duplicate_of_an_unknown_key_is_ignored():
    findings = [finding("aaaaaaaa:stated-next-step")]
    ranked = apply_ranking(
        findings,
        {
            "aaaaaaaa:stated-next-step": {"work_class": None, "duplicate_of": "zzzzzzzz:invented"},
        },
    )
    assert len(ranked) == 1


def test_an_empty_ranking_leaves_the_list_intact():
    findings = [finding("aaaaaaaa:blocked", "blocked", 0), finding("bbbbbbbb:open-offer", "open-offer", 3)]
    ranked = apply_ranking(findings, {})
    assert [f["key"] for f in ranked] == ["aaaaaaaa:blocked", "bbbbbbbb:open-offer"]
    assert all(f["work_class"] is None for f in ranked)


def test_ranking_re_sorts_by_the_new_severity():
    findings = [finding("aaaaaaaa:stated-next-step"), finding("bbbbbbbb:stated-next-step")]
    ranked = apply_ranking(
        findings,
        {
            "bbbbbbbb:stated-next-step": {"work_class": "open-work", "duplicate_of": None},
        },
    )
    assert [f["key"] for f in ranked] == ["bbbbbbbb:stated-next-step", "aaaaaaaa:stated-next-step"]


# ── the subprocess wrapper's refusal paths ─────────────────────────────────


class _NoCli:
    """The host's answer when the profile has no CLI (tldrack/headless.py owns the recipe)."""

    def headless_call(self, profile_id, **_kwargs):  # noqa: ARG002
        return None


def test_no_candidates_means_no_subprocess_and_no_error():
    assert run_rank([], profiles=None) == ({}, None)


def test_a_missing_cli_degrades_to_unranked_rather_than_raising():
    ranking, error = run_rank([{"key": "a:x"}], profiles=_NoCli())
    assert ranking == {}
    assert error and "unranked" in error


def test_a_host_without_headless_calls_degrades_to_unranked():
    ranking, error = run_rank([{"key": "a:x"}], profiles=None)
    assert ranking == {} and "restart" in error and "unranked" in error


def test_the_items_ride_on_stdin_and_the_instructions_are_the_system_prompt():
    prompt = build_rank_prompt([{"key": "a:x", "headline": "h"}])
    assert prompt.startswith("ITEMS:\n") and "work_class" not in prompt
    assert "ITEMS" not in RANK_INSTRUCTIONS
