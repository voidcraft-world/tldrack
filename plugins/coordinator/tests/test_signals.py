"""Detection tests: every signal fires when it should, and NOT on its near-miss.

The near-misses are the reason this file is long. Each signal has a shape that
looks almost identical in the transcript but means the opposite — an approved
plan, an answered question, a summary that says nothing is pending, a soft ask
the user already replied to, a refusal from 100 turns ago. Getting those wrong
is how an audit becomes noise, so they are tested as hard as the hits.

Records here are hand-built dicts with invented values, deliberately: the
detection must be provable without anyone's transcripts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coordinator.signals import (
    KIND_DISMISSED_QUESTION,
    KIND_OPEN_OFFER,
    KIND_REJECTED_PLAN,
    KIND_STATED_NEXT_STEP,
    KIND_UNACTIONED_RECOMMENDATION,
    SOFT_ASK_PHRASES,
    WORK_CLASS_OPEN,
    WORK_CLASS_ROUTINE,
    finding_key,
    next_step_clause,
    scan_session_entry,
    scan_transcript,
    severity_for,
    sort_findings,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
SESSION = "abcdef01-2345-4678-9abc-def012345678"
REFUSED = "The user doesn't want to proceed with this tool use"


# ── record builders ────────────────────────────────────────────────────────


def assistant(text: str = "", *, tool: tuple[str, str] | None = None, at: str = "2026-08-02T10:00:00.000Z") -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool is not None:
        name, use_id = tool
        content.append({"type": "tool_use", "id": use_id, "name": name, "input": {}})
    return {"type": "assistant", "timestamp": at, "message": {"content": content}}


def tool_result(use_id: str, text: str, *, at: str = "2026-08-02T10:00:01.000Z") -> dict:
    return {
        "type": "user",
        "timestamp": at,
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": [{"type": "text", "text": text}]}]},
    }


def user(text: str = "ok", *, at: str = "2026-08-02T10:00:02.000Z") -> dict:
    return {"type": "user", "timestamp": at, "message": {"content": [{"type": "text", "text": text}]}}


def away(content: str, *, at: str = "2026-08-02T10:05:00.000Z") -> dict:
    return {"type": "system", "subtype": "away_summary", "timestamp": at, "content": content}


def kinds_of(findings: list[dict]) -> set[str]:
    return {finding["kind"] for finding in findings}


def scan(records: list[dict]) -> list[dict]:
    return scan_transcript(SESSION, "-Users-me-repo", records, NOW)


# ── signal A: blocked ──────────────────────────────────────────────────────


def test_blocked_fires_for_a_waiting_live_session():
    entry = {
        "pid": 4242,
        "sessionId": SESSION,
        "cwd": "/repo",
        "name": "agent-36",
        "status": "waiting",
        "waitingFor": "permission prompt",
        "updatedAt": 1785668400000,
    }  # NOW minus one hour, in epoch ms
    finding = scan_session_entry(entry, NOW, alive=True)
    assert finding["kind"] == "blocked"
    assert finding["severity"] == 0
    assert finding["label"] == "agent-36"
    assert finding["age_hours"] == 1.0
    assert "permission prompt" in finding["headline"]
    assert finding["last_active"] is not None


@pytest.mark.parametrize("status", ["idle", "busy", None])
def test_blocked_does_not_fire_for_a_session_that_is_not_waiting(status):
    entry = {"pid": 4242, "sessionId": SESSION, "status": status}
    assert scan_session_entry(entry, NOW, alive=True) is None


def test_blocked_does_not_fire_when_the_process_is_gone():
    # A leftover index entry for a killed process must not earn severity 0.
    entry = {"pid": 4242, "sessionId": SESSION, "status": "waiting"}
    assert scan_session_entry(entry, NOW, alive=False) is None


# ── signal B: stated-next-step ─────────────────────────────────────────────


def test_stated_next_step_extracts_the_next_clause():
    summary = (
        "Goal was the docs graph; done and verified. "
        "Next: restart tldrack so docs-reader mounts, then verify visually. "
        "(disable recaps in /config)"
    )
    findings = scan([assistant("done"), away(summary)])
    step = next(f for f in findings if f["kind"] == KIND_STATED_NEXT_STEP)
    assert step["headline"].startswith("restart tldrack so docs-reader mounts")
    assert "(disable recaps in /config)" not in step["headline"]
    # Unranked it sits on the bottom rung — only the deep pass can promote it.
    assert step["severity"] == 4


@pytest.mark.parametrize("marker", ["Next:", "Next step:", "Next action is", "Next,"])
def test_next_clause_accepts_the_real_markers(marker):
    assert next_step_clause(f"All done. {marker} run the backfill against dev.")


def test_stated_next_step_does_not_fire_when_nothing_is_pending():
    for summary in (
        "All done; nothing pending. Next: nothing pending.",
        "Researched it and wrote it up. Next: no outstanding work.",
        "We covered what uv is. No pending action.",
        # The guard reads the WHOLE summary, not just the clause: a
        # summary can declare itself empty in one sentence and then
        # hand the ball back in the `Next` one.
        "You asked me to check the cwd. Nothing else is in progress; next action is whatever task you'd like to start.",
    ):
        assert next_step_clause(summary) is None


@pytest.mark.parametrize(
    "summary, opening",
    [
        ("Wired the hint. Next is you visually checking it on localhost.", "you visually"),
        ("Done. Next step is your visual check on localhost.", "your visual"),
        ("Shipped. Next action is for you to confirm the ring shows.", "for you to"),
    ],
)
def test_the_clause_never_opens_on_a_dangling_copula(summary, opening):
    # "next step is …" is a common phrasing; the verb belongs to
    # the marker. The clause IS the headline and the deep pass's only input, so a
    # sentence starting mid-verb degrades the panel and the classification.
    assert next_step_clause(summary).startswith(opening)


def test_next_clause_ignores_a_mid_sentence_next():
    # "your next build" is not a hand-off; only a sentence-opening Next counts,
    # or the real clause gets truncated away by the later match.
    summary = "Next: run `grep -c pdfjs dist/index.html` after your next build, which must return 0."
    clause = next_step_clause(summary)
    assert clause.startswith("run `grep")


def test_stated_next_step_needs_an_away_summary():
    assert kinds_of(scan([assistant("all done, looks good")])) == set()


# ── signals C/D: refused structured prompts ────────────────────────────────


def test_rejected_plan_fires_on_a_refused_final_exitplanmode():
    records = [
        assistant("here is the plan", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", REFUSED),
        assistant("understood, stopping here"),
    ]
    findings = scan(records)
    assert KIND_REJECTED_PLAN in kinds_of(findings)
    assert next(f for f in findings if f["kind"] == KIND_REJECTED_PLAN)["severity"] == 1


def test_rejected_plan_does_not_fire_on_an_approved_plan():
    records = [
        assistant("here is the plan", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", "User has approved your plan and it's ready to implement."),
        assistant("building it now"),
    ]
    assert KIND_REJECTED_PLAN not in kinds_of(scan(records))


def test_rejected_plan_uses_only_the_final_plan_call():
    # Rejected, then replaced with an approved plan — the normal working rhythm.
    records = [
        assistant("plan v1", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", REFUSED),
        assistant("plan v2", tool=("ExitPlanMode", "tu2")),
        tool_result("tu2", "User has approved your plan"),
        assistant("done"),
    ]
    assert KIND_REJECTED_PLAN not in kinds_of(scan(records))


def test_rejected_plan_does_not_fire_when_the_refusal_is_ancient():
    # A refusal followed by 100 more conversational turns means the user
    # STEERED, not abandoned (`signals.RECENT_CONVERSATIONAL_RECORDS`).
    records = [assistant("plan", tool=("ExitPlanMode", "tu1")), tool_result("tu1", REFUSED)]
    records += [assistant("working"), user("keep going")] * 50
    assert KIND_REJECTED_PLAN not in kinds_of(scan(records))


def test_rejected_plan_survives_the_agents_own_tool_loop():
    # THE regression that made signal C fire on nothing. Every tool call is an
    # assistant record plus a user record carrying the tool_result, so counting
    # raw records made 15 of the agent's own greps look like 30 turns of the
    # user steering. The gate is about STEERING: here the user said nothing at
    # all after refusing the plan.
    records = [
        assistant("here is the plan", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", REFUSED),
        assistant("understood, just looking around then"),
    ]
    for index in range(40):
        records += [assistant(tool=("Read", f"t{index}")), tool_result(f"t{index}", "file contents")]
    assert KIND_REJECTED_PLAN in kinds_of(scan(records))


def test_dismissed_question_fires_on_a_refused_final_askuserquestion():
    records = [
        assistant("which one?", tool=("AskUserQuestion", "tu9")),
        tool_result("tu9", "User rejected tool use"),
        assistant("no problem"),
    ]
    assert KIND_DISMISSED_QUESTION in kinds_of(scan(records))


@pytest.mark.parametrize("answer", ["Your questions have been answered", "The user answered: option B"])
def test_dismissed_question_does_not_fire_when_answered(answer):
    records = [assistant("which one?", tool=("AskUserQuestion", "tu9")), tool_result("tu9", answer), assistant("great, doing B")]
    assert KIND_DISMISSED_QUESTION not in kinds_of(scan(records))


def test_unrecognised_tool_result_is_treated_as_accepted():
    # Guessing on a marker shape we have never seen would fire severity 1 on
    # noise.
    records = [
        assistant("plan", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", "some future marker nobody has written yet"),
        assistant("carrying on"),
    ]
    assert KIND_REJECTED_PLAN not in kinds_of(scan(records))


# ── signals E/F: the closing message ───────────────────────────────────────


def test_open_offer_fires_on_a_soft_ask_in_the_final_message():
    records = [assistant("Shipped the rack. Want me to wire the pop-out next?")]
    findings = scan(records)
    offer = next(f for f in findings if f["kind"] == KIND_OPEN_OFFER)
    assert offer["severity"] == 3
    assert "pop-out" in offer["headline"]  # the sentence, not just the phrase


def test_open_offer_does_not_fire_when_the_phrase_is_in_a_non_final_message():
    records = [
        assistant("Want me to also add the toggle?"),
        user("no, skip it"),
        assistant("Skipped. The migration is applied and the tests pass."),
    ]
    assert KIND_OPEN_OFFER not in kinds_of(scan(records))


def test_open_offer_does_not_fire_when_the_user_spoke_last():
    records = [assistant("Want me to deploy it?"), user("nah I'll do it")]
    assert KIND_OPEN_OFFER not in kinds_of(scan(records))


def test_unactioned_recommendation_fires_on_a_recommendation_phrase():
    records = [assistant("All 1022 tests pass. Worth running /code-review before you deploy.")]
    findings = scan(records)
    hit = next(f for f in findings if f["kind"] == KIND_UNACTIONED_RECOMMENDATION)
    assert hit["severity"] == 4


@pytest.mark.parametrize("phrase", ["should i", "shall i", "would you prefer"])
def test_zero_hit_phrases_are_not_in_the_soft_ask_set(phrase):
    # These read like obvious additions but do not appear in closing hand-offs.
    # Adding them costs precision and buys nothing — keep them out.
    assert phrase not in SOFT_ASK_PHRASES
    assert KIND_OPEN_OFFER not in kinds_of(scan([assistant(f"Done. {phrase} rebase it?")]))


# ── finding shape ──────────────────────────────────────────────────────────


def test_key_is_stable_and_scoped_to_the_session():
    assert finding_key(SESSION, KIND_OPEN_OFFER) == "abcdef01:open-offer"
    first = scan([assistant("Want me to keep going?")])[0]
    second = scan([assistant("Want me to keep going?")])[0]
    assert first["key"] == second["key"] == "abcdef01:open-offer"


def test_finding_carries_the_contract_fields():
    records = [
        {"type": "ai-title", "aiTitle": "Rack header cleanup"},
        {
            "type": "assistant",
            "timestamp": "2026-08-02T09:00:00.000Z",
            "cwd": "/Users/me/repo",
            "gitBranch": "main",
            "message": {"content": [{"type": "text", "text": "Want me to keep going?"}]},
        },
    ]
    finding = scan(records)[0]
    assert finding["label"] == "Rack header cleanup"
    assert finding["cwd"] == "/Users/me/repo"
    assert finding["branch"] == "main"
    assert finding["project"] == "-Users-me-repo"
    assert finding["last_active"] == "2026-08-02T09:00:00Z"
    assert finding["age_hours"] == 3.0
    assert finding["work_class"] is None
    assert finding["live_session"] is None


def test_detail_comes_from_whichever_source_explains_the_finding():
    records = [
        assistant("Shipped it. Want me to wire the pop-out next?"),
        away("Goal was the rack. Next: decide on the pop-out."),
    ]
    by_kind = {f["kind"]: f for f in scan(records)}
    # Summary-derived findings quote the summary; phrase-derived ones quote the
    # closing message that actually contains the phrase.
    assert by_kind[KIND_STATED_NEXT_STEP]["detail"].startswith("Goal was the rack.")
    assert by_kind[KIND_OPEN_OFFER]["detail"].startswith("Shipped it.")


def test_the_recap_footer_is_stripped_from_the_detail_too():
    # `detail` is where the footer hurts most: it burns budget in a 600-char
    # field on boilerplate.
    records = [assistant("done"), away("Goal was the rack. Next: decide on the pop-out. (disable recaps in /config)")]
    for finding in scan(records):
        assert "disable recaps" not in finding["detail"]
        assert "disable recaps" not in finding["headline"]


def test_headline_and_detail_are_clipped_to_one_line():
    long_offer = "x " * 400 + "want me to keep going?"
    finding = scan([assistant(long_offer)])[0]
    assert len(finding["headline"]) <= 160
    assert len(finding["detail"]) <= 600
    assert "\n" not in finding["headline"] and "\n" not in finding["detail"]


def test_ai_title_beats_slug_and_slug_beats_the_session_prefix():
    text = [
        {
            "type": "assistant",
            "timestamp": "2026-08-02T09:00:00.000Z",
            "slug": "fix-the-thing",
            "message": {"content": [{"type": "text", "text": "Want me to keep going?"}]},
        }
    ]
    assert scan(text)[0]["label"] == "fix-the-thing"
    assert scan([assistant("Want me to keep going?")])[0]["label"] == "abcdef01"


def test_severity_ladder():
    assert severity_for("blocked", None) == 0
    assert severity_for(KIND_REJECTED_PLAN, None) == 1
    assert severity_for(KIND_STATED_NEXT_STEP, WORK_CLASS_OPEN) == 2
    assert severity_for(KIND_STATED_NEXT_STEP, WORK_CLASS_ROUTINE) == 4
    assert severity_for(KIND_STATED_NEXT_STEP, None) == 4
    assert severity_for(KIND_OPEN_OFFER, None) == 3
    # An unknown kind is kept at the bottom rung, never dropped.
    assert severity_for("some-future-signal", None) == 4


def test_sort_is_severity_then_recency_with_unknown_last():
    findings = [
        {"key": "a:x", "severity": 3, "last_active": "2026-08-01T00:00:00Z"},
        {"key": "b:x", "severity": 0, "last_active": "2026-07-01T00:00:00Z"},
        {"key": "c:x", "severity": 3, "last_active": "2026-08-02T00:00:00Z"},
        {"key": "d:x", "severity": 3, "last_active": None},
    ]
    assert [f["key"] for f in sort_findings(findings)] == ["b:x", "c:x", "a:x", "d:x"]


def test_a_transcript_can_carry_several_signals_at_once():
    records = [
        assistant("plan", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", REFUSED),
        assistant("Stopping. Let me know if you want the other approach."),
        away("Goal was X. Next: decide which approach to take."),
    ]
    assert kinds_of(scan(records)) == {KIND_REJECTED_PLAN, KIND_OPEN_OFFER, KIND_STATED_NEXT_STEP}


def test_empty_and_junk_records_produce_nothing():
    assert scan([]) == []
    assert scan([{"type": "mode"}, {"type": "file-history-snapshot"}]) == []


def test_the_final_plan_call_is_the_last_one_even_inside_one_turn():
    # An agent can fire several tools without saying a word between them, so
    # turn index TIES — the call order is what decides which plan was last.
    records = [
        assistant("thinking", tool=("ExitPlanMode", "tu1")),
        tool_result("tu1", REFUSED),
        {
            "type": "assistant",
            "timestamp": "2026-08-02T10:00:03.000Z",
            "message": {"content": [{"type": "tool_use", "id": "tu2", "name": "ExitPlanMode", "input": {}}]},
        },
        tool_result("tu2", "User has approved your plan"),
        assistant("building it"),
    ]
    assert KIND_REJECTED_PLAN not in kinds_of(scan(records))
