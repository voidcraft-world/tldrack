"""Laya's turn-end verdict: the question pin, the judge, the store's compare-and-set, and the
whole path from a Stop hook to a row's `open_work` (open_work.py owns why)."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from tldrack import open_work
from tldrack.agent_index import IndexEntry
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.profiles import Profile, ProfileRegistry
from tldrack.open_work import (
    FALSE_WARNING_COST,
    FINE_TUNE_MODEL,
    DONE_AT,
    FALSE_DONE_COST,
    GRADE_KEPT,
    GRADE_NONE,
    GRADE_OFF,
    GRADE_RECORDED,
    LEDGER_SURFACE,
    MAX_PENDING_REACTIONS,
    OPEN_WORK_AT,
    OPEN_WORK_QUESTION,
    QUESTION_VERSION,
    VERDICT_ROW_KEYS,
    OpenWorkJudge,
    OpenWorkReactions,
    ask_laya,
    done_work_cut,
    clean_operator_label,
    grade_verdict,
    label_change,
    ledger_judged,
    ledger_tag,
    open_work_cut,
    plugin_caller,
    rack_grader,
    state_sha,
    verdict_row_fields,
    watch_operator_labels,
)
from tldrack.state_arbiter import HOLD_SECONDS
from tldrack.verdicts import SessionVerdictStore
from voidcraft_world_bridge.policy import CallerPolicy


#: Where the test bridge's rack posts from: a dev-server origin, sending JSON as the SPA does.
DEV_ORIGIN = "http://127.0.0.1:5173"
RACK_HEADERS = {"Origin": DEV_ORIGIN, "Content-Type": "application/json"}


def decision_id(n):
    return f"20261027T120000-{n:012x}"


class FakeLayaPlugin:
    """The laya plugin's handler surface. `p(state)` scripts the noul; `warm` 503s first;
    `ledger` hands each decide a `decision_id` (the plugin's ledger is on); `/react` is recorded, and so is
    each `rack_grade` (a second one on the same decision answers 409 `already_answered` with the label that
    stands, as the plugin does). `LABEL_POLICY` stands in for the plugin's own label posture: the dev origin
    the test bridge posts from, and a JSON body."""

    LABEL_POLICY = CallerPolicy(allowed_origins=frozenset({DEV_ORIGIN}), subject="Laya")

    def __init__(self, p=lambda state: 0.9, warm=0, models=("english",), thresholds=None, ledger=False, react_raises=False):
        self.p, self.warm, self.models = p, warm, list(models)
        #: `judged_before`'s answer when it cannot say (the real plugin with its ledger off): None, never a guess.
        self.ledger_unsure = False
        self.thresholds, self.ledger, self.react_raises = thresholds, ledger, react_raises
        self.decides: list[dict] = []
        self.reacts: list[tuple[str, str]] = []
        self.grades: list[tuple[str, bool]] = []
        self.react_attempts = 0
        #: The bridge's `watch_labels` subscription: one per bridge process, so a restart's replaces the last.
        self.label_listener = None

    def handle_get(self, subpath, query):
        assert subpath == "/status"
        status = {"state": "ready" if not self.warm else "starting", "models": self.models}
        if self.thresholds is not None:
            status["thresholds"] = self.thresholds
        return 200, status

    def handle_post(self, subpath, query, body):
        if subpath == "/react":
            self.react_attempts += 1
            if self.react_raises:
                raise RuntimeError("ledger on fire")
            self.reacts.append((body["decision_id"], body["action"]))
            return 200, {"ok": True, "decision_id": body["decision_id"], "action": body["action"], "duplicate": False}
        assert subpath == "/decide"
        self.decides.append(body)
        if self.warm:
            self.warm -= 1
            return 503, {"error": "warming", "retry_after_ms": 1500}
        payload = {"answers": {"q": {"noul": self.p(body["state"])}}}
        if self.ledger:
            payload.update(decision_id=decision_id(len(self.decides)), model_id="laya-local/english@abc1234", checkpoint=None)
        return 200, payload

    def judged_before(self, conversation, state_hash):
        """The plugin's ledger lookup (`Ledger.judged`), over the decides this fake has answered."""
        if self.ledger_unsure:
            return None
        return any(
            body.get("ledger", {}).get("conversation") == conversation and state_sha(body["state"]) == state_hash
            for body in self.decides
        )

    def watch_labels(self, listener):
        self.label_listener = listener

    def label(self, decision, is_open, needs=None, surface=LEDGER_SURFACE):
        """The operator labels `decision`'s text on LAYA REVIEW: the plugin's report (`review.label_changes`)."""
        self.label_listener([{"surface": surface, "decision_id": decision, "open": is_open, "needs": needs}])

    def rack_grade(self, body, headers):
        if self.LABEL_POLICY.refusal(headers) is not None or headers.get("content-type") != "application/json":
            return 403, {"ok": False, "code": "origin_refused"}
        standing = next((is_open for held, is_open in self.grades if held == body["decision_id"]), None)
        if standing is not None:
            return 409, {"ok": False, "code": "already_answered", "answer": {"answer": "agree", "open": standing}}
        self.grades.append((body["decision_id"], body["open"]))
        return 200, {"ok": True}


# ── the question ──────────────────────────────────────────────────────────────


def test_the_question_is_pinned_to_its_version():
    """Edit the wording or the state builder → bump QUESTION_VERSION and re-run
    `laya-lab golden agent-open-work`."""
    assert QUESTION_VERSION == 2
    assert OPEN_WORK_QUESTION == {
        "type": "noul",
        "instructions": "Does this message say that some work is still not done, not started, blocked, or waiting on an answer?",
    }


# ── the state ─────────────────────────────────────────────────────────────────


def test_a_hand_off_that_fits_is_judged_as_written():
    text = "Phases A and B are done.\n\n```\ncode\n```\n\nPhase C is not started."
    assert open_work.decide_state(text) == text


def test_a_long_hand_offs_code_is_collapsed_before_anything_is_cut():
    block = "```text\n" + "pasted line\n" * 200 + "```"
    text = f"Here is the note.\n\n{block}\n\nStep two has not started. Say when to begin."
    state = open_work.decide_state(text)
    assert state == ("Here is the note.\n\n" + open_work.CODE_PLACEHOLDER + "\n\nStep two has not started. Say when to begin.")


def test_a_long_hand_off_keeps_its_opening_and_its_end():
    """The 2026-10-30 miss: Laya keeps a long state's HEAD, so the closing lines (where the
    unstarted work is named) were the part it never read. The end must survive, inside the budget."""
    text = "Opening line. " + "middle filler " * 300 + "Phase C is not started yet."
    state = open_work.decide_state(text)
    assert len(state) <= open_work.STATE_MAX_CHARS
    assert state.startswith("Opening line.")
    assert state.endswith("Phase C is not started yet.")
    assert open_work.ELISION in state


# ── question v3: the request, then the closing ───────────────────────────────


def test_a_v3_state_reads_the_request_then_the_closing_and_a_turn_nobody_asked_for_reads_as_v2():
    hand_off = "Renamed the flag and migrated the callers. Tests pass."
    request = "Do all three: rename the flag, migrate the callers, delete the old config path."
    assert open_work.decide_state(hand_off, request) == hand_off, "the served version is 2: the request is not read"
    assert open_work.decide_state(hand_off, request, version=3) == f"Request: {request}\n\nReply: {hand_off}"
    assert open_work.decide_state(hand_off, None, version=3) == open_work.decide_state(hand_off)
    assert open_work.decide_state(hand_off, " \n ", version=3) == open_work.decide_state(hand_off)
    with pytest.raises(ValueError):
        open_work.decide_state(hand_off, request, version=5)


def test_a_v3_state_keeps_the_requests_head_and_the_closings_end_inside_the_same_budget():
    request = "Ship the export in three phases. " + "Context pasted below.\n" * 100
    hand_off = "Opening line. " + "middle filler " * 300 + "Phase C is not started yet."
    state = open_work.decide_state(hand_off, request, version=3)
    assert len(state) <= open_work.STATE_MAX_CHARS
    assert state.startswith("Request: Ship the export in three phases.")
    head = state.split(open_work.PART_BREAK)[0]
    assert len(head) - len(open_work.REQUEST_LABEL) == open_work.REQUEST_MAX_CHARS and head.endswith("…")
    assert state.endswith("Phase C is not started yet.") and open_work.ELISION in state


# ── question v4: the request, the turn's facts, then the closing ─────────────

FACTS = {"ran_tests": True, "tests_passed": True, "edited_files": 3, "made_commit": True, "opened_pr": False}


def test_a_v4_state_reads_the_request_then_the_facts_then_the_closing():
    hand_off = "Renamed the flag and migrated the callers. Tests pass."
    request = "Rename the flag and migrate the callers."
    assert open_work.decide_state(hand_off, request, facts=FACTS, version=4) == (
        f"Request: {request}\n\nFacts: tests passed, 3 files edited, commit made\n\nReply: {hand_off}"
    )


def test_the_served_version_ignores_the_facts_byte_for_byte():
    """Served behaviour at version 2 must not move by one byte: the bridge now passes request and facts."""
    for hand_off in ("All done, tests pass.", "Opening line. " + "middle filler " * 300 + "Phase C is not started yet."):
        served = open_work.decide_state(hand_off)
        assert open_work.decide_state(hand_off, "Ship it.", facts=FACTS) == served
        assert open_work.decide_state(hand_off, "Ship it.", facts=FACTS, version=2) == served
        assert open_work.decide_state(hand_off, "Ship it.", facts=FACTS, version=3) == open_work.decide_state(
            hand_off, "Ship it.", version=3
        ), "v3 never reads the facts"


def test_a_v4_state_with_no_facts_is_its_v3_state_and_with_no_request_its_facts_then_the_closing():
    hand_off, request = "All done.", "Do it."
    for missing in (None, {}, {"ran_tests": "yes"}, {**FACTS, "edited_files": -1}, {**FACTS, "tests_passed": "ok"}):
        assert open_work.decide_state(hand_off, request, facts=missing, version=4) == open_work.decide_state(
            hand_off, request, version=3
        )
    assert open_work.decide_state(hand_off, None, facts=FACTS, version=4) == (
        "Facts: tests passed, 3 files edited, commit made\n\nReply: All done."
    )
    assert open_work.decide_state(hand_off, None, facts=None, version=4) == hand_off


@pytest.mark.parametrize(
    "facts, line",
    [
        (
            {**FACTS, "ran_tests": False, "tests_passed": None, "edited_files": 0, "made_commit": False},
            "Facts: no tests ran, no files edited",
        ),
        ({**FACTS, "tests_passed": None, "edited_files": 1}, "Facts: tests ran, 1 file edited, commit made"),
        (
            {**FACTS, "tests_passed": False, "made_commit": False, "opened_pr": True},
            "Facts: tests failed, 3 files edited, PR opened",
        ),
        ({**FACTS, "opened_pr": True, "ended_on_question": True}, "Facts: tests passed, 3 files edited, commit made, PR opened"),
    ],
)
def test_the_facts_line_is_a_fixed_vocabulary(facts, line):
    assert open_work.fit_facts(facts) == line


def test_the_facts_line_has_a_hard_cap_and_drops_whole_phrases():
    longest = {"ran_tests": True, "tests_passed": False, "edited_files": 256, "made_commit": True, "opened_pr": True}
    assert len(open_work.fit_facts(longest)) <= open_work.FACTS_MAX_CHARS, "the vocabulary fits its own cap"
    assert open_work.fit_facts(longest, 40) == "Facts: tests failed, 256 files edited"
    assert open_work.fit_facts(longest, 10) == ""
    assert open_work.fit_facts({**longest, "ran_tests": False}) == "", "a result without a run does not read"


def test_a_v4_state_is_never_longer_than_the_budget():
    request = "Ship the export in three phases. " + "Context pasted below.\n" * 100
    hand_off = "Opening line. " + "middle filler " * 300 + "Phase C is not started yet."
    longest = {"ran_tests": True, "tests_passed": False, "edited_files": 256, "made_commit": True, "opened_pr": True}
    state = open_work.decide_state(hand_off, request, facts=longest, version=4)
    assert len(state) <= open_work.STATE_MAX_CHARS
    request_part, facts_part, reply = state.split(open_work.PART_BREAK, 2)
    assert request_part.startswith("Request: Ship the export") and facts_part == open_work.fit_facts(longest)
    assert reply.startswith("Reply: ") and state.endswith("Phase C is not started yet.")


def test_turn_facts_reads_a_landing_trace_and_nothing_else():
    trace = {"ran_tests": True, "edited_files": 2, "opened_pr": False, "ended_on_question": False}
    assert open_work.turn_facts(trace) is None, "a trace from before the two facts does not read"
    trace.update(tests_passed=None, made_commit=False)
    assert open_work.turn_facts(trace) == {
        "ran_tests": True,
        "tests_passed": None,
        "edited_files": 2,
        "made_commit": False,
        "opened_pr": False,
    }
    assert open_work.turn_facts(None) is None


def test_fit_hand_off_at_the_whole_budget_is_unchanged_by_the_budget_parameter():
    text = "Opening line. " + "middle filler " * 300 + "the end."
    assert open_work.fit_hand_off(text) == open_work.fit_hand_off(text, open_work.STATE_MAX_CHARS)
    assert len(open_work.fit_hand_off(text, 900)) == 900


def _user(text, **extra):
    return {"type": "user", "message": {"content": text}, **extra}


def test_a_turns_request_is_what_the_operator_typed_and_never_what_a_machine_did():
    assert open_work.turn_request(_user("Fix the flaky test.")) == "Fix the flaky test."
    assert (
        open_work.turn_request(_user("<command-name>/plan</command-name><command-args>run step 4.7</command-args>"))
        == "/plan run step 4.7"
    )
    note = "<task-notification><task-id>a1</task-id><status>completed</status></task-notification>"
    for machine in (
        _user(note),
        _user("hello", turnOrigin="peer"),
        _user("[Request interrupted by user]"),
        _user("<local-command-stdout>ok</local-command-stdout>"),
        _user(""),
    ):
        assert open_work.turn_request(machine) is None


def test_the_request_fold_holds_across_tool_calls_and_resets_at_the_next_turn():
    tool_result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    skill_body = _user("skill steps", isMeta=True)
    note = _user("<task-notification><task-id>a1</task-id><status>completed</status></task-notification>")
    assert open_work.fold_turn_request(None, [_user("Do A."), tool_result, skill_body]) == "Do A."
    assert open_work.fold_turn_request("Do A.", [note]) is None
    assert open_work.fold_turn_request(None, [note, _user("Now B.")]) == "Now B."


def test_row_fields_threshold_the_probability():
    assert verdict_row_fields(None) == dict.fromkeys(("open_work", "work_done", "open_work_p", "answer_to_read", "answer_read"))
    assert verdict_row_fields({"p": OPEN_WORK_AT})["open_work"] is True
    assert verdict_row_fields({"p": OPEN_WORK_AT - 0.01})["open_work"] is False
    assert verdict_row_fields({"p": 0.12345})["open_work_p"] == 0.123


def test_row_fields_use_the_verdicts_own_cut():
    assert verdict_row_fields({"p": 0.65, "at": 0.6})["open_work"] is True
    assert verdict_row_fields({"p": 0.65, "at": 0.7})["open_work"] is False


def test_three_bands_from_two_cuts():
    """p < done_at → ✅, done_at ≤ p < at → the plain 💬, p ≥ at → ⚠️."""
    band = lambda p: (
        verdict_row_fields({"p": p, "at": 0.7, "done_at": 0.4})["open_work"],
        verdict_row_fields({"p": p, "at": 0.7, "done_at": 0.4})["work_done"],
    )
    assert band(0.39) == (False, True)
    assert band(0.4) == (False, False)
    assert band(0.69) == (False, False)
    assert band(0.7) == (True, False)


# ── An answer to read (open_work.py § An answer to read) ─────────────────────


@pytest.mark.parametrize(
    ("request_text", "asks"),
    [
        ("Why does the rack drop the ✅ on restart?", True),
        ("Look at the logs.\n\nWhat broke?\n  \n", True),  # the LAST non-empty line decides
        ("**Ship it?**", True),  # trailing markup is peeled, as for a closing
        ("Why? Fix it.", False),  # a question mid-prompt is not the ask
        ("Fix the flaky test.", False),
        ("/plan run step 4.8", False),
        ("", False),
        (None, False),  # a hook, a task or a child opened the turn
    ],
)
def test_a_turn_asks_a_question_when_its_prompts_last_line_ends_in_a_question_mark(request_text, asks):
    assert open_work.asks_question(request_text) is asks


DONE = {"p": 0.05, "at": 0.7, "done_at": 0.4}


@pytest.mark.parametrize(
    ("verdict", "unread", "read"),
    [
        ({**DONE, "asked": True}, True, False),  # ✅ on a question: an answer to read, 📖
        ({**DONE, "asked": True, "read": True}, False, True),  # its pane was opened: ☑️📖, the 📖 stays
        ({**DONE, "asked": False}, False, False),  # ✅ on an order: nothing to read
        ({**DONE, "asked": False, "read": True}, False, False),  # and opening it makes no answer of it
        (DONE, False, False),  # a verdict from before the stamp
        ({"p": 0.5, "at": 0.7, "done_at": 0.4, "asked": True}, False, False),  # 💬: already an unread reply
        ({"p": 0.9, "at": 0.7, "done_at": 0.4, "asked": True, "read": True}, False, False),  # ⚠️: asks for a look
        ({"p": 0.05, "at": 0.7, "asked": True}, False, False),  # from before the done band: never ✅
    ],
)
def test_the_mark_rides_only_a_done_verdict_on_a_question_and_stays_once_read(verdict, unread, read):
    fields = verdict_row_fields(verdict)
    assert (fields["answer_to_read"], fields["answer_read"]) == (unread, read)


def test_with_request_stamps_a_copy_and_leaves_the_judges_verdict_alone():
    verdict = dict(DONE)
    assert open_work.with_request(verdict, "Is it green?")["asked"] is True
    assert open_work.with_request(verdict, None)["asked"] is False
    assert "asked" not in verdict


def test_a_verdict_from_before_the_done_band_is_never_done():
    """The rack drew 💬 for it; the row must not start claiming ✅ after a bridge upgrade."""
    assert verdict_row_fields({"p": 0.05, "at": 0.7})["work_done"] is False


def test_the_done_cut_sits_below_the_open_cut_and_costs_a_false_check_more():
    assert DONE_AT < OPEN_WORK_AT
    assert FALSE_DONE_COST > 1


@pytest.mark.parametrize(
    "thresholds,model,open_at,expected",
    [
        ({"agent_open_work": {"at": 0.7, "done_at": 0.3, "model": "m"}}, "m", 0.7, 0.3),
        ({"agent_open_work": {"at": 0.7, "done_at": 0.3, "model": "m"}}, None, 0.7, DONE_AT),  # a built-in never borrows
        ({"agent_open_work": {"at": 0.7, "done_at": 0.3, "model": "other"}}, "m", 0.7, DONE_AT),
        ({"agent_open_work": {"at": 0.7, "done_at": True, "model": "m"}}, "m", 0.7, DONE_AT),
        ({"agent_open_work": {"at": 0.7, "done_at": 1.0, "model": "m"}}, "m", 0.7, DONE_AT),
        ({"agent_open_work": {"at": 0.7, "model": "m"}}, "m", 0.7, DONE_AT),
        ({"agent_open_work": {"at": 0.35, "model": "m"}}, "m", 0.35, 0.35),  # never above the open cut
        ({"agent_open_work": {"at": 0.5, "done_at": 0.6, "model": "m"}}, "m", 0.5, 0.5),
        (None, None, OPEN_WORK_AT, DONE_AT),
    ],
)
def test_done_work_cut(thresholds, model, open_at, expected):
    assert done_work_cut(thresholds, model, open_at) == expected


def test_the_cut_rule_picks_the_fallback_cut_from_a_sweep_of_three():
    """FALSE_WARNING_COST's comment claims its rule picks OPEN_WORK_AT from a sweep shaped like this
    one (invented counts: lower cuts miss less and warn falsely more); hold it to that."""
    assert FALSE_WARNING_COST == 2.0
    sweep = {0.7: (2, 10), 0.6: (4, 8), 0.5: (9, 6)}  # cut: (false ⚠️, missed open)
    cost = {at: FALSE_WARNING_COST * fp + fn for at, (fp, fn) in sweep.items()}
    assert cost == {0.7: 14, 0.6: 16, 0.5: 24}
    assert min(sweep, key=lambda at: (cost[at], -at)) == OPEN_WORK_AT


# ── asking Laya ───────────────────────────────────────────────────────────────


def test_ask_waits_out_a_cold_start_then_answers():
    laya = FakeLayaPlugin(p=lambda _: 0.8, warm=2)
    verdict = ask_laya(plugin_caller(laya), "Phase C is not started.", sleep=lambda _: None)
    assert verdict == {
        "p": 0.8,
        "v": QUESTION_VERSION,
        "model": "english",
        "at": 0.7,
        "done_at": DONE_AT,
        "decision_id": None,
        "model_id": None,
        "checkpoint": None,
    }
    assert len(laya.decides) == 3
    assert "model" not in laya.decides[-1]


def test_ask_pins_the_fine_tune_when_it_is_served():
    laya = FakeLayaPlugin(models=("english", FINE_TUNE_MODEL))
    verdict = ask_laya(plugin_caller(laya), "done", sleep=lambda _: None)
    assert laya.decides[0]["model"] == FINE_TUNE_MODEL
    assert verdict["model"] == FINE_TUNE_MODEL


def test_ask_tags_the_ledger_with_the_session():
    laya = FakeLayaPlugin()
    ask_laya(plugin_caller(laya), "done", subject="vc-1", sleep=lambda _: None)
    assert laya.decides[0]["ledger"] == {
        "surface": "agent_open_work",
        "subject": "vc-1",
        "question_version": QUESTION_VERSION,
        "threshold": OPEN_WORK_AT,
        "done_threshold": DONE_AT,
    }


@pytest.mark.parametrize("subject", [None, "bad name!", "x" * 129, "", 7])
def test_a_subject_the_ledger_would_refuse_is_left_out(subject):
    laya = FakeLayaPlugin()
    assert ask_laya(plugin_caller(laya), "done", subject=subject, sleep=lambda _: None) is not None
    assert "subject" not in laya.decides[0]["ledger"]
    assert laya.decides[0]["ledger"]["surface"] == "agent_open_work"


#: A Claude Code session id, as a Stop hook carries it (`session_id`).
CONVERSATION = "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab"
CODEX_THREAD = "019a5f3e-7b2c-7d41-9e8f-0123456789ab"  # Codex thread ids are UUIDv7


@pytest.mark.parametrize("conversation", [CONVERSATION, CODEX_THREAD, CONVERSATION.upper()])
def test_ask_tags_the_ledger_with_the_conversation(conversation):
    laya = FakeLayaPlugin()
    ask_laya(plugin_caller(laya), "done", subject="vc-1", conversation=conversation, sleep=lambda _: None)
    assert laya.decides[0]["ledger"] == {
        "surface": "agent_open_work",
        "subject": "vc-1",
        "conversation": conversation,
        "question_version": QUESTION_VERSION,
        "threshold": OPEN_WORK_AT,
        "done_threshold": DONE_AT,
    }


@pytest.mark.parametrize(
    "conversation",
    [
        None,
        "",
        "not-a-uuid",
        CONVERSATION + "\n",
        CONVERSATION[:-1],
        "../" + CONVERSATION,
        "--dangerously-skip-permissions",
        7,
        ["x"],
        {"id": CONVERSATION},
    ],
)
def test_a_conversation_that_is_not_a_uuid_is_left_out(conversation):
    """It is hook text: the tag carries a UUID or nothing, so the plugin never 400s the verdict over it."""
    assert "conversation" not in ledger_tag("vc-1", OPEN_WORK_AT, DONE_AT, conversation)
    laya = FakeLayaPlugin()
    assert ask_laya(plugin_caller(laya), "done", subject="vc-1", conversation=conversation, sleep=lambda _: None)
    assert "conversation" not in laya.decides[0]["ledger"] and laya.decides[0]["ledger"]["subject"] == "vc-1"


def test_a_swept_cut_is_used_for_the_fine_tune_it_was_swept_on():
    laya = FakeLayaPlugin(
        models=("english", FINE_TUNE_MODEL), thresholds={"agent_open_work": {"at": 0.62, "model": FINE_TUNE_MODEL}}
    )
    verdict = ask_laya(plugin_caller(laya), "done", subject="vc-1", sleep=lambda _: None)
    assert verdict["at"] == 0.62
    assert laya.decides[0]["ledger"]["threshold"] == 0.62


def test_a_swept_done_cut_reaches_the_verdict_and_the_ledger():
    laya = FakeLayaPlugin(
        models=("english", FINE_TUNE_MODEL),
        thresholds={"agent_open_work": {"at": 0.7, "done_at": 0.25, "model": FINE_TUNE_MODEL}},
    )
    verdict = ask_laya(plugin_caller(laya), "done", sleep=lambda _: None)
    assert verdict["done_at"] == 0.25
    assert laya.decides[0]["ledger"]["done_threshold"] == 0.25


@pytest.mark.parametrize(
    "thresholds,models",
    [
        ({"agent_open_work": {"at": 0.62, "model": "tldrack-other"}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": {"at": 0.62, "model": FINE_TUNE_MODEL}}, ("english",)),  # the fine-tune is not served
        ({"agent_open_work": {"at": 0.62, "model": "english"}}, ("english",)),  # a built-in never borrows a cut
        ({"agent_open_work": {"at": 1.0, "model": FINE_TUNE_MODEL}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": {"at": 0, "model": FINE_TUNE_MODEL}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": {"at": True, "model": FINE_TUNE_MODEL}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": {"at": "0.6", "model": FINE_TUNE_MODEL}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": {"at": float("nan"), "model": FINE_TUNE_MODEL}}, ("english", FINE_TUNE_MODEL)),
        ({"agent_open_work": "0.6"}, ("english", FINE_TUNE_MODEL)),
        (["0.6"], ("english", FINE_TUNE_MODEL)),
        (None, ("english", FINE_TUNE_MODEL)),
    ],
)
def test_any_other_threshold_falls_back_to_the_code_cut(thresholds, models):
    laya = FakeLayaPlugin(models=models, thresholds=thresholds)
    verdict = ask_laya(plugin_caller(laya), "done", sleep=lambda _: None)
    assert verdict["at"] == OPEN_WORK_AT
    assert laya.decides[0]["ledger"]["threshold"] == OPEN_WORK_AT


def test_open_work_cut_is_pure():
    assert open_work_cut({"agent_open_work": {"at": 0.55, "model": "m"}}, "m") == 0.55
    assert open_work_cut({"agent_open_work": {"at": 0.55, "model": "m"}}, None) == OPEN_WORK_AT


def test_the_verdict_carries_the_ledgers_handle():
    class Ledgered(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            return 200, {
                "answers": {"q": {"noul": 0.9}},
                "decision_id": decision_id(1),
                "model_id": f"laya-local/{FINE_TUNE_MODEL}@ckpt-20261026",
                "checkpoint": "ckpt-20261026",
            }

    verdict = ask_laya(plugin_caller(Ledgered()), "x", sleep=lambda _: None)
    assert verdict["decision_id"] == decision_id(1)
    assert verdict["model_id"] == f"laya-local/{FINE_TUNE_MODEL}@ckpt-20261026"
    assert verdict["checkpoint"] == "ckpt-20261026"


@pytest.mark.parametrize(
    "extra",
    [
        {"decision_id": "not-an-id", "model_id": "", "checkpoint": "laya/checkpoints/ckpt"},
        {"decision_id": decision_id(1) + "\n", "model_id": 7, "checkpoint": ".."},
        {"decision_id": "٢٠٢٦١٠٢٧T120000-000000000001", "model_id": "x" * 141, "checkpoint": "a\\b"},
        {"decision_id": None, "model_id": None, "checkpoint": None},
        {},
    ],
)
def test_a_malformed_handle_is_dropped_but_the_verdict_stands(extra):
    class Odd(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            return 200, {"answers": {"q": {"noul": 0.9}}, **extra}

    verdict = ask_laya(plugin_caller(Odd()), "x", sleep=lambda _: None)
    assert verdict["p"] == 0.9
    assert (verdict["decision_id"], verdict["model_id"], verdict["checkpoint"]) == (None, None, None)


def test_a_refused_tag_never_costs_the_verdict_and_is_said_once(monkeypatch, capsys):
    class TagRefusing(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            self.decides.append(body)
            if "ledger" in body:
                return 400, {"error": "bad_request", "detail": "ledger.surface: needs a native caller"}
            return 200, {"answers": {"q": {"noul": 0.9}}}

    monkeypatch.setattr(open_work, "_tag_refusal_reported", False)
    laya = TagRefusing()
    for _ in range(2):
        verdict = ask_laya(plugin_caller(laya), "x", subject="vc-1", sleep=lambda _: None)
        assert verdict["p"] == 0.9
    assert ["ledger" in body for body in laya.decides] == [True, False, True, False]
    said = capsys.readouterr().out
    assert said.count("laya refused the open-work ledger tag (ledger.surface: needs a native caller)") == 1


class ConversationRefusing(FakeLayaPlugin):
    """A plugin from before the `conversation` field: it refuses that field by name, the way
    `clean_tag` refuses any field it does not know."""

    def __init__(self, detail="ledger.conversation: not a ledger field", **kwargs):
        super().__init__(**kwargs)
        self.detail = detail

    def handle_post(self, subpath, query, body):
        if "conversation" in body.get("ledger", {}):
            self.decides.append(body)
            return 400, {"error": "bad_request", "detail": self.detail}
        return super().handle_post(subpath, query, body)


def test_a_refused_conversation_drops_only_that_field_and_is_said_once(monkeypatch, capsys):
    """The skew guard: an older plugin must cost the row its band, never its tag (and so its text)."""
    monkeypatch.setattr(open_work, "_field_refusals_reported", set())
    monkeypatch.setattr(open_work, "_tag_refusal_reported", False)
    laya = ConversationRefusing(ledger=True)
    for _ in range(2):
        verdict = ask_laya(plugin_caller(laya), "x", subject="vc-1", conversation=CONVERSATION, sleep=lambda _: None)
        assert verdict["p"] == 0.9 and verdict["decision_id"] is not None
    assert [sorted(body["ledger"]) for body in laya.decides] == [
        ["conversation", "done_threshold", "question_version", "subject", "surface", "threshold"],
        ["done_threshold", "question_version", "subject", "surface", "threshold"],
    ] * 2
    said = capsys.readouterr().out
    assert said.count("laya refused the open-work conversation id (ledger.conversation: not a ledger field)") == 1
    assert "asking untagged" not in said


def test_a_conversation_refused_again_after_the_strip_cannot_loop(monkeypatch):
    """A plugin that keeps naming the field once it is gone falls through to the untagged ask: three
    asks at most, and the verdict still lands."""
    monkeypatch.setattr(open_work, "_field_refusals_reported", {"conversation"})
    monkeypatch.setattr(open_work, "_tag_refusal_reported", True)

    class Stubborn(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            self.decides.append(body)
            if "ledger" in body:
                return 400, {"error": "bad_request", "detail": "ledger.conversation: must be a UUID"}
            return 200, {"answers": {"q": {"noul": 0.9}}}

    laya = Stubborn()
    verdict = ask_laya(plugin_caller(laya), "x", subject="vc-1", conversation=CONVERSATION, sleep=lambda _: None)
    assert verdict["p"] == 0.9
    assert [("ledger" in body, "conversation" in body.get("ledger", {})) for body in laya.decides] == [
        (True, True),
        (True, False),
        (False, False),
    ]


def test_any_other_tag_refusal_still_asks_untagged_with_a_conversation(monkeypatch):
    """Only `ledger.conversation…` is stripped; every other refusal keeps the existing path."""
    monkeypatch.setattr(open_work, "_tag_refusal_reported", True)
    laya = ConversationRefusing(detail="ledger.surface: only the agent rack may tag this surface")
    verdict = ask_laya(plugin_caller(laya), "x", subject="vc-1", conversation=CONVERSATION, sleep=lambda _: None)
    assert verdict["p"] == 0.9
    assert ["ledger" in body for body in laya.decides] == [True, False]


def test_a_400_that_is_not_about_the_tag_is_no_verdict():
    class Refusing(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            self.decides.append(body)
            return 400, {"error": "bad_request", "detail": "state is over 8000 chars"}

    laya = Refusing()
    assert ask_laya(plugin_caller(laya), "x", sleep=lambda _: None) is None
    assert len(laya.decides) == 1


@pytest.mark.parametrize(
    "reply",
    [
        (503, {"error": "not_installed"}),
        (400, {}),
        (200, {"answers": {}}),
        (200, {"answers": {"q": {"noul": True}}}),
        (200, {"answers": {"q": {"noul": 1.5}}}),
    ],
)
def test_ask_degrades_to_no_verdict(reply):
    class Refusing(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            return reply

    assert ask_laya(plugin_caller(Refusing()), "x", sleep=lambda _: None) is None


def test_a_plugin_that_raises_is_a_500_not_a_crash():
    class Broken:
        def handle_get(self, subpath, query):
            raise RuntimeError("boom")

    assert plugin_caller(Broken())("/status", None) == (500, {})


# ── the judge ─────────────────────────────────────────────────────────────────


def test_judge_keeps_only_the_latest_turn_per_session():
    landed = []
    judge = OpenWorkJudge(
        lambda: plugin_caller(FakeLayaPlugin(p=lambda s: 0.9 if "C" in s else 0.1)),
        lambda session, token, verdict: landed.append((session, token, verdict["p"])),
        settle_seconds=60,
    )
    judge._thread = object()  # keep the worker from starting: drain() judges on this thread
    assert judge.submit("vc-1", 1, lambda: "A done")
    assert judge.submit("vc-1", 2, lambda: "C not started")
    assert judge.submit("vc-2", 7, lambda: "all done")
    assert judge.drain() == 2
    assert sorted(landed) == [("vc-1", 2, 0.9), ("vc-2", 7, 0.1)]


def test_judge_without_laya_queues_nothing_and_no_text_is_no_verdict():
    assert not OpenWorkJudge(lambda: None, lambda *a: None).submit("vc-1", 1, lambda: "x")
    landed = []
    judge = OpenWorkJudge(lambda: plugin_caller(FakeLayaPlugin()), lambda *a: landed.append(a))
    judge._thread = object()
    judge.submit("vc-1", 1, lambda: None)
    assert judge.drain() == 0 and landed == []


def test_judge_tags_each_decision_with_its_session():
    laya = FakeLayaPlugin()
    judge = OpenWorkJudge(lambda: plugin_caller(laya), lambda *a: None, settle_seconds=60)
    judge._thread = object()
    judge.submit("vc-7", 1, lambda: "done")
    judge.drain()
    assert laya.decides[0]["ledger"]["subject"] == "vc-7"
    assert "conversation" not in laya.decides[0]["ledger"], "a submit without an id tags none"


def test_judge_tags_each_decision_with_the_conversation_of_the_turn_it_judges():
    """Latest-wins per session keeps the id that came with the kept turn, never an earlier one's."""
    laya = FakeLayaPlugin()
    judge = OpenWorkJudge(lambda: plugin_caller(laya), lambda *a: None, settle_seconds=60)
    judge._thread = object()
    assert judge.submit("vc-1", 1, lambda: "first conversation's turn", conversation=CONVERSATION)
    assert judge.submit("vc-1", 2, lambda: "a new conversation in vc-1", conversation=CODEX_THREAD)
    assert judge.submit("vc-2", 3, lambda: "hook text", conversation="not-a-uuid")
    assert judge.drain() == 2
    by_state = {body["state"]: body["ledger"] for body in laya.decides}
    assert set(by_state) == {"a new conversation in vc-1", "hook text"}
    assert by_state["a new conversation in vc-1"]["conversation"] == CODEX_THREAD
    assert "conversation" not in by_state["hook text"] and by_state["hook text"]["subject"] == "vc-2"


# ── reactions ─────────────────────────────────────────────────────────────────


def shown_verdict(n, p=0.9, at=OPEN_WORK_AT):
    return {
        "p": p,
        "v": QUESTION_VERSION,
        "model": "english",
        "at": at,
        "decision_id": decision_id(n),
        "model_id": None,
        "checkpoint": None,
    }


@pytest.fixture
def reactions():
    laya = FakeLayaPlugin()
    r = OpenWorkReactions(lambda: plugin_caller(laya), worker=False)
    r.laya = laya
    return r


def test_shown_only_when_the_verdict_drew_a_warning(reactions):
    reactions.landed("vc-1", shown_verdict(1, p=0.65, at=0.7))  # below its own cut
    reactions.landed("vc-2", shown_verdict(2, p=0.65, at=0.6))  # above its own (swept) cut
    reactions.landed("vc-3", {**shown_verdict(3), "decision_id": None})  # ledger off
    reactions.drain()
    assert reactions.laya.reacts == [(decision_id(2), "shown")]
    assert reactions.outstanding("vc-1") is None and reactions.outstanding("vc-3") is None


def test_a_cleared_warning_reports_once_and_an_ordinary_turn_posts_nothing(reactions):
    reactions.cleared("vc-1", "expired")  # a busy hook with nothing on screen
    reactions.landed("vc-1", shown_verdict(1))
    reactions.cleared("vc-1", "dismissed")
    reactions.cleared("vc-1", "expired")
    reactions.drain()
    assert reactions.laya.reacts == [(decision_id(1), "shown"), (decision_id(1), "dismissed")]


def test_a_second_landing_expires_the_first(reactions):
    reactions.landed("vc-1", shown_verdict(1))
    reactions.landed("vc-1", shown_verdict(2, p=0.1))  # the new turn drew no ⚠️
    reactions.drain()
    assert reactions.laya.reacts == [(decision_id(1), "shown"), (decision_id(1), "expired")]
    assert reactions.outstanding("vc-1") is None


def test_a_verdict_voided_before_it_was_tracked_is_never_shown(reactions):
    reactions.landed("vc-1", shown_verdict(1), live=lambda: False)
    reactions.drain()
    assert reactions.laya.reacts == [] and reactions.outstanding("vc-1") is None


def test_rename_moves_the_warning_and_expires_the_names_old_occupant(reactions):
    reactions.landed("vc-1", shown_verdict(1))
    reactions.landed("vc-2", shown_verdict(2))
    reactions.rename("vc-1", "vc-2")
    assert reactions.outstanding("vc-1") is None and reactions.outstanding("vc-2") == decision_id(1)
    reactions.cleared("vc-2", "dismissed")
    reactions.forget("vc-2")
    reactions.drain()
    assert reactions.laya.reacts == [
        (decision_id(1), "shown"),
        (decision_id(2), "shown"),
        (decision_id(2), "expired"),
        (decision_id(1), "dismissed"),
    ]


def test_forget_drops_without_a_reaction(reactions):
    reactions.landed("vc-1", shown_verdict(1))
    reactions.forget("vc-1")
    reactions.cleared("vc-1", "expired")
    reactions.drain()
    assert reactions.laya.reacts == [(decision_id(1), "shown")]


def test_a_broken_ledger_never_raises():
    def exploding():
        raise RuntimeError("no plugin table")

    for factory in (exploding, lambda: None, lambda: plugin_caller(FakeLayaPlugin(react_raises=True))):
        r = OpenWorkReactions(factory, worker=False)
        r.landed("vc-1", shown_verdict(1))
        r.cleared("vc-1", "expired")
        assert r.drain() == 2


def test_a_ledger_that_stops_answering_cannot_grow_the_queue(reactions):
    for n in range(MAX_PENDING_REACTIONS + 10):
        reactions.landed("vc-1", shown_verdict(n))
    assert reactions.drain() == MAX_PENDING_REACTIONS
    assert reactions.laya.reacts[-1] == (decision_id(MAX_PENDING_REACTIONS + 9), "shown")


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_worker_thread_posts_in_order_and_outlives_a_raising_plugin():
    laya = FakeLayaPlugin(react_raises=True)
    r = OpenWorkReactions(lambda: plugin_caller(laya))
    r.landed("vc-1", shown_verdict(1))
    wait_until(lambda: laya.react_attempts == 1)
    laya.react_raises = False
    r.landed("vc-2", shown_verdict(2))
    r.cleared("vc-2", "expired")
    wait_until(lambda: len(laya.reacts) == 2)
    assert laya.reacts == [(decision_id(2), "shown"), (decision_id(2), "expired")]


# ── the store ─────────────────────────────────────────────────────────────────


def test_a_verdict_lands_only_on_its_own_turn():
    store = StatusStore()
    store.set("vc-1", "free", None)
    token = store.begin_open_work("vc-1")
    assert store.set_open_work("vc-1", token, {"p": 0.9, "v": 1, "model": "english"})
    assert store.get("vc-1")["open_work"] is True

    stale = store.begin_open_work("vc-1")
    store.set("vc-1", "busy", None, open_work=None)  # the operator typed a prompt
    assert not store.set_open_work("vc-1", stale, {"p": 0.9, "v": 1, "model": "english"})
    assert store.get("vc-1")["open_work"] is None


def test_the_verdict_survives_attention_and_free_and_clears_on_busy_and_dismiss():
    store = StatusStore()
    store.set("vc-1", "free", None)
    store.set_open_work("vc-1", store.begin_open_work("vc-1"), {"p": 0.9, "v": 1, "model": "english"})
    store.set("vc-1", "free", None)  # the idle nag
    assert store.get("vc-1")["open_work"] is True
    assert store.dismiss_open_work("vc-1")
    assert store.get("vc-1")["open_work"] is None
    assert not store.dismiss_open_work("vc-1")

    store.set_open_work("vc-1", store.begin_open_work("vc-1"), {"p": 0.9, "v": 1, "model": "english"})
    store.correct("vc-1", store.versions()["vc-1"]["seq"], "busy", None)  # a prompt the hooks missed
    assert store.get("vc-1")["open_work"] is None


def test_a_dismiss_voids_a_verdict_still_in_flight():
    store = StatusStore()
    store.set("vc-1", "free", None)
    token = store.begin_open_work("vc-1")
    store.set_open_work("vc-1", token, {"p": 0.9, "v": 1, "model": "english"})
    store.dismiss_open_work("vc-1")
    assert not store.set_open_work("vc-1", token, {"p": 0.9, "v": 1, "model": "english"})


def test_an_unknown_row_reports_no_verdict():
    assert StatusStore().get("nobody")["open_work"] is None


# ── Stop hook → row ───────────────────────────────────────────────────────────


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    # Never this Mac's real phone-push config: a test bridge has nowhere to send.
    monkeypatch.setenv("TLDRACK_NOTIFY_PATH", str(tmp_path / "notify.json"))
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(open_work, "SETTLE_SECONDS", 0)
    laya = FakeLayaPlugin(p=lambda state: 0.95 if "not started" in state else 0.05, ledger=True)
    verdict_path = tmp_path / "verdicts.json"

    def build(store):
        """One bridge process. The verdict file is re-read on every build, as a restart would."""
        return build_status_handler(
            store=store,
            icon_store=SessionIconStore(tmp_path / "icons.json"),
            model_store=SessionModelStore(tmp_path / "models.json"),
            default_session="tldrack",
            bridge_info={"pid": 1234, "started_at": 0.0},
            control=ShutdownControl("status-only"),
            plugins={"laya": laya},
            profiles=ProfileRegistry(
                [
                    Profile("codex", "Codex", "codex", str(tmp_path / "codex"), True),
                    Profile("sol", "Sol", "codex", str(tmp_path / "codex"), True),
                ],
                "codex",
                home=tmp_path,
                source="file",
                which=lambda name: str(tmp_path / "bin" / name),
                cli_help=lambda _: None,
            ),
            verdicts=SessionVerdictStore(verdict_path),
        )

    store = StatusStore()
    cls = build(store)
    # Claude's own layout, so a restart can find the conversation's transcript with no hook (`find_transcript`).
    transcript = claude_root / "projects" / "-repo" / f"{CONVERSATION}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")

    def post(route, event=None, payload=None, headers=RACK_HEADERS):
        h = object.__new__(cls)
        raw = json.dumps(payload or {}).encode()
        h.headers = {**headers, "Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        h.path = f"{route}?session=vc-1" + (f"&event={event}" if event else "")
        sent = {}
        h._send_json = lambda status, body: sent.update(status=status, body=body)
        getattr(h, cls.POST_ROUTES[route])()
        return sent

    def say(text, message_id, **extra):
        with transcript.open("a") as handle:
            handle.write(
                json.dumps(
                    {"type": "assistant", "message": {"id": message_id, "content": [{"type": "text", "text": text}]}, **extra}
                )
                + "\n"
            )

    def hook(event, **extra):
        return post("/hook", event, {"transcript_path": str(transcript), **extra})

    def wait_for(open_work, session="vc-1"):
        """The row once its `open_work` reads `open_work` (the judge runs on its own thread)."""
        deadline = time.monotonic() + 5
        while store.get(session)["open_work"] is not open_work and time.monotonic() < deadline:
            time.sleep(0.01)
        return store.get(session)

    def reacts(count):
        """The plugin's `/react` calls once there are `count` (the poster runs on its own thread)."""
        deadline = time.monotonic() + 5
        while len(laya.reacts) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # and nothing more arrives behind them
        return list(laya.reacts)

    def tmux_says(returncode, stderr=""):
        """Every tmux call this test makes answers this, never touching this Mac's real sessions."""
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], returncode, "", stderr)
        )

    return SimpleNamespace(
        post=post,
        say=say,
        hook=hook,
        wait_for=wait_for,
        reacts=reacts,
        tmux_ok=lambda: tmux_says(0),
        tmux_says=tmux_says,
        store=store,
        laya=laya,
        cls=cls,
        build=build,
        verdict_path=verdict_path,
        claude_root=claude_root,
    )


def test_a_stop_with_open_work_paints_the_row_and_the_next_prompt_clears_it(bridge):
    bridge.hook("UserPromptSubmit")
    bridge.say("Phases A and B are done. Phase C is not started.", "msg_1")
    bridge.hook("Stop")
    row = bridge.wait_for(True)
    assert row["open_work"] is True and row["open_work_p"] == 0.95
    assert bridge.laya.decides[-1]["state"] == "Phases A and B are done. Phase C is not started."

    bridge.hook("UserPromptSubmit")
    assert bridge.store.get("vc-1")["open_work"] is None


@pytest.mark.parametrize(
    ("text", "open_work", "work_done"),
    [
        ("All done, tests pass.", False, True),
        ("Phases A and B are done. Phase C is not started.", True, False),
    ],
)
def test_the_snapshot_row_carries_the_verdict_the_status_row_has(bridge, text, open_work, work_done):
    """The rack reads `/snapshot` (and its SSE twin) only. Until 2026-11-05 `merge_snapshot`
    dropped the verdict, so `/status` said ✅ while the rack drew 💬 on every finished turn."""
    bridge.tmux_ok()
    end_turn(bridge, text, "msg_1")
    status = bridge.wait_for(open_work)
    assert (status["open_work"], status["work_done"]) == (open_work, work_done)
    snapshot = object.__new__(bridge.cls).snapshot_payload()
    row = next(item for item in snapshot["sessions"] if item["session"] == "vc-1")
    assert {key: row[key] for key in VERDICT_ROW_KEYS} == {key: status[key] for key in VERDICT_ROW_KEYS}


def _worked_turn(bridge):
    """A turn that ran passing tests and committed, then said so."""
    ask(bridge, "Fix the flaky test and commit.")
    entries = [
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "4 passed"}]}},
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "git commit -am fix"}}],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "[x 1a2b] fix"}]}},
    ]
    with bridge.claude_root.joinpath("projects", "-repo", f"{CONVERSATION}.jsonl").open("a") as handle:
        handle.write("".join(json.dumps(entry) + "\n" for entry in entries))
    end_turn(bridge, "Fixed and committed.", "m3")


def test_a_stop_at_the_served_version_judges_the_closing_alone_whatever_the_turn_did(bridge):
    _worked_turn(bridge)
    bridge.wait_for(False)
    assert bridge.laya.decides[-1]["state"] == "Fixed and committed."


def test_a_stop_at_version_4_judges_the_request_the_facts_and_the_closing(bridge, monkeypatch):
    """Serving v4 is `QUESTION_VERSION` alone: the bridge already threads the request and the trace."""
    from tldrack import main as bridge_main

    monkeypatch.setattr(bridge_main, "decide_state", lambda *a, **k: open_work.decide_state(*a, **{**k, "version": 4}))
    _worked_turn(bridge)
    bridge.wait_for(False)
    assert bridge.laya.decides[-1]["state"] == (
        "Request: Fix the flaky test and commit.\n\nFacts: tests passed, no files edited, commit made\n\n"
        "Reply: Fixed and committed."
    )


def test_a_clean_stop_has_no_warning_and_dismiss_clears_a_warning(bridge):
    bridge.say("All done, tests pass.", "msg_1")
    bridge.hook("Stop")
    assert bridge.wait_for(False)["open_work"] is False

    bridge.say("Phase C is not started.", "msg_2")
    bridge.hook("Stop")
    assert bridge.wait_for(True)["open_work"] is True
    assert bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]["cleared"] is True
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.post("/open-work/dismiss", payload={"session": "bad name!"})["status"] == 400


def test_a_turn_an_api_error_cut_off_is_never_judged(bridge):
    # Claude Code's synthetic notice is the newest entry: no hand-off, so Laya is never asked about it.
    bridge.say("Phase C is not started.", "msg_1")
    bridge.say(
        "API Error: Server error mid-response. The response above may be incomplete.",
        "msg_2",
        isApiErrorMessage=True,
        error="unknown",
    )
    bridge.hook("Stop")
    deadline = time.monotonic() + 0.5
    while not bridge.laya.decides and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.laya.decides == [] and bridge.store.get("vc-1")["open_work"] is None

    bridge.say("All done, tests pass.", "msg_3")  # the next real reply is judged as ever
    bridge.hook("Stop")
    assert bridge.wait_for(False)["open_work"] is False
    assert [decide["state"] for decide in bridge.laya.decides] == ["All done, tests pass."]


def test_codex_stop_judges_its_final_reply_not_commentary_or_an_old_claude_reply(bridge, tmp_path):
    bridge.say("Old Claude reply: all done.", "old")
    bridge.hook("Stop")
    assert bridge.wait_for(False)["open_work"] is False
    bridge.hook("UserPromptSubmit")
    session_id = "dddddddd-0000-4000-8000-000000000004"
    path = tmp_path / "codex" / "sessions" / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": phase,
                        "content": [{"type": "output_text", "text": text}],
                    },
                }
            )
            for phase, text in [("commentary", "All done"), ("final_answer", "Phase C is not started.")]
        )
        + "\n"
    )
    bridge.post("/hook", "Stop", {"session_id": session_id, "transcript_path": str(path)})
    assert bridge.wait_for(True)["open_work"] is True
    assert bridge.laya.decides[-1]["state"] == "Phase C is not started."


def ask(bridge, prompt):
    """The operator types `prompt`: its transcript entry, then the hook (the turn's opening)."""
    with bridge.claude_root.joinpath("projects", "-repo", f"{CONVERSATION}.jsonl").open("a") as handle:
        handle.write(json.dumps({"type": "user", "message": {"content": prompt}}) + "\n")
    bridge.hook("UserPromptSubmit", prompt=prompt)


def test_a_question_answered_done_carries_the_mark_until_its_pane_is_opened(bridge):
    bridge.tmux_ok()
    ask(bridge, "Why does the ✅ vanish on restart?")
    end_turn(bridge, "All done, tests pass.", "msg_1")
    row = bridge.wait_for(False)
    assert (row["work_done"], row["answer_to_read"]) == (True, True)
    snapshot = object.__new__(bridge.cls).snapshot_payload()
    assert next(r for r in snapshot["sessions"] if r["session"] == "vc-1")["answer_to_read"] is True

    assert bridge.post("/open-work/read", payload={"session": "vc-1"})["body"] == {"ok": True, "session": "vc-1", "cleared": True}
    row = bridge.store.get("vc-1")
    assert (row["work_done"], row["answer_to_read"], row["answer_read"]) == (True, False, True), "☑️📖: read, still an answer"
    assert bridge.post("/open-work/read", payload={"session": "vc-1"})["body"]["cleared"] is False
    assert bridge.post("/open-work/read", payload={"session": "bad name!"})["status"] == 400
    assert bridge.laya.grades == [], "opening a pane grades nothing"


def test_a_done_turn_that_asked_nothing_draws_the_check_alone(bridge):
    ask(bridge, "Fix the flaky test.")
    end_turn(bridge, "All done, tests pass.", "msg_1")
    row = bridge.wait_for(False)
    assert (row["work_done"], row["answer_to_read"]) == (True, False)
    assert bridge.post("/open-work/read", payload={"session": "vc-1"})["body"]["cleared"] is False


def test_a_turn_a_task_woke_has_no_prompt_and_no_mark(bridge):
    ask(bridge, "Why is CI red?")
    ask(bridge, "<task-notification><task-id>a1</task-id><status>completed</status></task-notification>")
    end_turn(bridge, "All done, tests pass.", "msg_1")
    assert bridge.wait_for(False)["answer_to_read"] is False


def test_an_open_answer_keeps_its_warning_and_no_mark(bridge):
    ask(bridge, "Can you finish phase C?")
    end_turn(bridge, "Phase C is not started.", "msg_1")
    row = bridge.wait_for(True)
    assert (row["open_work"], row["answer_to_read"]) == (True, False)


def test_the_mark_goes_when_the_session_turns_busy_again(bridge):
    ask(bridge, "Is the build green?")
    end_turn(bridge, "All done, tests pass.", "msg_1")
    assert bridge.wait_for(False)["answer_to_read"] is True
    bridge.hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
    assert bridge.store.get("vc-1")["answer_to_read"] is None


def test_a_restart_keeps_an_unread_answer_unread_and_a_read_one_read(bridge, monkeypatch):
    ask(bridge, "Is the build green?")
    end_turn(bridge, "All done, tests pass.", "msg_1")
    assert bridge.wait_for(False)["answer_to_read"] is True
    store, cls = restart(bridge, monkeypatch)
    assert store.get("vc-1")["answer_to_read"] is True

    h = object.__new__(cls)  # the press reaches the RESTARTED bridge, which owns the kept copy now
    raw = json.dumps({"session": "vc-1"}).encode()
    h.headers = {**RACK_HEADERS, "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.path = "/open-work/read"
    sent = {}
    h._send_json = lambda status, body: sent.update(status=status, body=body)
    getattr(h, cls.POST_ROUTES["/open-work/read"])()
    assert sent["body"]["cleared"] is True
    store, _ = restart(bridge, monkeypatch)
    row = store.get("vc-1")
    assert (row["work_done"], row["answer_to_read"], row["answer_read"]) == (True, False, True)


# ── Stop hook → ledger reactions ──────────────────────────────────────────────


def warn(bridge, message_id="msg_1"):
    """A turn that ends with open work: the ⚠️ lands and its `shown` reaches the ledger."""
    before = len(bridge.laya.reacts)
    bridge.say(f"Phase C is not started ({message_id}).", message_id)
    bridge.hook("Stop")
    assert bridge.wait_for(True)["open_work"] is True
    return bridge.reacts(before + 1)[-1]


def test_a_shown_warning_is_reported_and_dismissed_through_the_route(bridge):
    assert warn(bridge) == (decision_id(1), "shown")
    assert bridge.laya.decides[-1]["ledger"]["subject"] == "vc-1"
    assert bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]["cleared"] is True
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "dismissed")]


# ── the ⚠️ menu's grades ──────────────────────────────────────────────────────


def test_not_open_clears_the_warning_and_records_the_operators_grade(bridge):
    warn(bridge)
    body = bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]
    assert body == {"ok": True, "session": "vc-1", "cleared": True, "graded": True}
    assert bridge.laya.grades == [(decision_id(1), False)]
    again = bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]
    assert (again["cleared"], again["graded"]) == (False, False) and len(bridge.laya.grades) == 1


def test_still_open_keeps_the_warning_grades_it_once_and_says_so_on_the_row(bridge):
    assert bridge.post("/open-work/confirm", payload={"session": "vc-1"})["body"]["graded"] is False, "no ⚠️, no grade"
    warn(bridge)
    assert bridge.post("/open-work/confirm", payload={"session": "vc-1"})["body"] == {
        "ok": True,
        "session": "vc-1",
        "graded": True,
    }
    assert bridge.laya.grades == [(decision_id(1), True)]
    assert bridge.store.get("vc-1")["open_work"] is True, "the work IS open: the ⚠️ stays"
    assert bridge.store.graded_open_work() == {"vc-1"}
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "accepted")]
    # A second press finds the label already there: still graded, nothing new recorded.
    assert bridge.post("/open-work/confirm", payload={"session": "vc-1"})["body"]["graded"] is True
    assert bridge.laya.grades == [(decision_id(1), True)]
    assert bridge.post("/open-work/confirm", payload={"session": "bad name!"})["status"] == 400
    # The next turn's ⚠️ is a new verdict, ungraded; the old one expires as before.
    bridge.hook("UserPromptSubmit")
    assert bridge.store.graded_open_work() == set()
    assert bridge.reacts(4)[-1] == (decision_id(1), "expired")


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://localhost:9999", "Content-Type": "application/json"},  # another page on this Mac
        {"Origin": DEV_ORIGIN, "Content-Type": "text/plain"},  # a simple request: no preflight
        {"Origin": DEV_ORIGIN, "Content-Type": "application/json", "X-Forwarded-For": "10.0.0.9"},
    ],
)
def test_a_caller_the_plugin_refuses_clears_the_mark_and_records_no_label(bridge, headers):
    """The bridge's own guard passes any loopback origin; an operator label needs the plugin's posture."""
    warn(bridge)
    confirm = bridge.post("/open-work/confirm", payload={"session": "vc-1"}, headers=headers)["body"]
    assert confirm["graded"] is False and bridge.store.graded_open_work() == set()
    body = bridge.post("/open-work/dismiss", payload={"session": "vc-1"}, headers=headers)["body"]
    assert body == {"ok": True, "session": "vc-1", "cleared": True, "graded": False}
    assert bridge.laya.grades == [] and bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "dismissed")], "no `accepted`"


def test_a_done_verdict_is_never_graded_from_the_menu(bridge):
    bridge.say("All done, tests pass.", "msg_1")
    bridge.hook("Stop")
    assert bridge.wait_for(False)["open_work"] is False
    assert bridge.post("/open-work/confirm", payload={"session": "vc-1"})["body"]["graded"] is False
    body = bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]
    assert (body["cleared"], body["graded"]) == (True, False) and bridge.laya.grades == []


def test_grade_verdict_names_each_outcome():
    laya = FakeLayaPlugin()
    grade = rack_grader(laya, RACK_HEADERS)
    flagged = {"p": 0.9, "at": 0.7, "decision_id": decision_id(1)}
    assert grade_verdict(grade, flagged, is_open_now=False) == GRADE_RECORDED
    assert grade_verdict(grade, flagged, is_open_now=True) == GRADE_KEPT, "the first answer stands"
    assert grade_verdict(None, flagged, is_open_now=False) == GRADE_OFF
    assert grade_verdict(grade, {"p": 0.9, "at": 0.7}, is_open_now=False) == GRADE_NONE, "the ledger is off: no id"
    assert grade_verdict(grade, {"p": 0.5, "at": 0.7, "decision_id": decision_id(2)}, is_open_now=False) == GRADE_NONE
    assert grade_verdict(grade, None, is_open_now=False) == GRADE_NONE
    assert grade_verdict(rack_grader(object(), RACK_HEADERS), flagged, is_open_now=False) == GRADE_OFF, (
        "a plugin without the door"
    )
    assert grade_verdict(rack_grader(laya, {"Origin": "http://localhost:9999"}), flagged, is_open_now=False) == GRADE_OFF
    # `kept` needs the label that stands: a skip is no label, and neither is a 409 that names none.
    skipped = {"ok": False, "code": "already_answered", "answer": {"answer": "skip", "open": None}}
    assert grade_verdict(lambda body: (409, skipped), flagged, is_open_now=False) == GRADE_OFF
    assert grade_verdict(lambda body: (409, {"code": "already_answered"}), flagged, is_open_now=False) == GRADE_OFF
    assert (
        grade_verdict(lambda body: (409, {"code": "unknown_decision", "answer": {"open": True}}), flagged, is_open_now=False)
        == GRADE_OFF
    )
    assert len(laya.grades) == 1
    assert laya.grades == [(decision_id(1), False)]


def test_a_grade_marks_only_the_verdict_it_answered():
    store = StatusStore()
    store.set("vc-1", "free", None)
    store.set_open_work(
        "vc-1", store.begin_open_work("vc-1"), {"p": 0.9, "v": 1, "model": "english", "decision_id": decision_id(1)}
    )
    assert not store.mark_open_work_graded("vc-1", decision_id(2)), "a newer turn's verdict is not the one graded"
    assert store.mark_open_work_graded("vc-1", decision_id(1)) and store.graded_open_work() == {"vc-1"}
    assert store.open_work_verdict("vc-1")["graded"] is True and store.open_work_verdict("nobody") is None
    assert store.dismiss_open_work("vc-1")["decision_id"] == decision_id(1)
    assert store.graded_open_work() == set()


def test_the_next_prompt_expires_a_warning_and_a_clean_turn_reports_nothing(bridge):
    bridge.say("All done, tests pass.", "msg_0")
    bridge.hook("Stop")
    assert bridge.wait_for(False)["open_work"] is False
    bridge.hook("UserPromptSubmit")
    assert bridge.reacts(0) == []

    assert warn(bridge) == (decision_id(2), "shown")
    bridge.hook("UserPromptSubmit")
    assert bridge.reacts(2) == [(decision_id(2), "shown"), (decision_id(2), "expired")]


def test_a_second_warning_expires_the_first(bridge):
    warn(bridge, "msg_1")
    bridge.say("Phase D is not started either.", "msg_2")
    bridge.hook("Stop")
    assert bridge.reacts(3) == [(decision_id(1), "shown"), (decision_id(1), "expired"), (decision_id(2), "shown")]


def test_a_rename_carries_the_warning_to_the_new_name(bridge):
    bridge.tmux_ok()
    warn(bridge)
    assert bridge.post("/rename-session", payload={"session": "vc-1", "name": "vc-9"})["status"] == 200
    assert bridge.post("/open-work/dismiss", payload={"session": "vc-9"})["body"]["cleared"] is True
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "dismissed")]


def test_a_kill_expires_the_warning(bridge):
    bridge.tmux_ok()
    warn(bridge)
    assert bridge.post("/kill-session")["body"] == {"ok": True, "killed": True}
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "expired")]


def test_a_kill_of_a_session_already_gone_expires_the_warning(bridge):
    warn(bridge)
    bridge.tmux_says(1, "can't find session: vc-1")
    assert bridge.post("/kill-session")["body"]["killed"] is False
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "expired")]


def test_a_kill_voids_a_verdict_still_in_flight(bridge, monkeypatch):
    """Killed while Laya was still thinking: the verdict loses its compare-and-set, so the dead
    row never gets a ⚠️ and the ledger never hears `shown` for a session that is gone."""
    bridge.tmux_ok()
    release = threading.Event()
    judge = bridge.laya.p

    def thinking(state):
        release.wait(5)
        return judge(state)

    bridge.laya.p = thinking
    landings = []
    set_open_work = bridge.store.set_open_work

    def recorded(*args):
        landings.append(set_open_work(*args))
        return landings[-1]

    monkeypatch.setattr(bridge.store, "set_open_work", recorded)
    bridge.say("Phase C is not started.", "msg_1")
    bridge.hook("Stop")
    wait_until(lambda: len(bridge.laya.decides) == 1)  # the judge is inside /decide
    assert bridge.post("/kill-session")["body"] == {"ok": True, "killed": True}
    release.set()
    wait_until(lambda: landings)
    assert landings == [False]
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(0) == []


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
def test_a_session_start_or_end_expires_the_warning(bridge, event):
    warn(bridge)
    bridge.hook(event)
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "expired")]


def test_a_spawn_reusing_the_name_expires_the_warning(bridge):
    warn(bridge)
    bridge.tmux_ok()
    assert bridge.post("/spawn", payload={"name": "vc-1", "profile": "codex"})["status"] == 201
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "expired")]


def test_an_arbiter_busy_expires_the_warning(bridge, monkeypatch):
    """A prompt the hooks missed, caught from Claude's own index (state_arbiter.py): a new turn."""
    warn(bridge)
    arbiter = bridge.cls.arbiter
    wall = bridge.store.versions()["vc-1"]["wall_at"]
    index = {"vc-1": IndexEntry("vc-1", "busy", wall + 1, None, None, 1, "x")}
    monkeypatch.setattr(arbiter, "_reader", SimpleNamespace(read=lambda: dict(index), still_running=lambda _: True))
    monkeypatch.setattr(arbiter, "_process_table", lambda: "")
    monkeypatch.setattr(arbiter, "_clock", lambda: wall + 1 + HOLD_SECONDS)
    assert arbiter.tick() == [("vc-1", "busy")]
    assert bridge.store.get("vc-1")["open_work"] is None
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "expired")]


# ── Bridge restart → the verdict comes back (verdicts.py) ─────────────────────


def end_turn(bridge, text, message_id, conversation=CONVERSATION):
    bridge.say(text, message_id)
    bridge.hook("Stop", session_id=conversation)


def restart(bridge, monkeypatch, *, state="free", conversation=CONVERSATION):
    """A new bridge process: an empty StatusStore and the verdict file read back from disk. The row
    returns the only way an idle session's row can, from Claude's index (the arbiter's new-row path)."""
    store = StatusStore()
    cls = bridge.build(store)
    arbiter = cls.arbiter
    now = time.time()
    index = {"vc-1": IndexEntry("vc-1", state, now, None, conversation, 1, "x", root=str(bridge.claude_root))}
    monkeypatch.setattr(arbiter, "_reader", SimpleNamespace(read=lambda: dict(index), still_running=lambda _: True))
    monkeypatch.setattr(arbiter, "_process_table", lambda: "")
    monkeypatch.setattr(arbiter, "_clock", lambda: now + HOLD_SECONDS)
    assert arbiter.tick() == [("vc-1", state)]
    return store, cls


def test_a_stop_tags_its_verdict_with_the_hooks_conversation(bridge):
    """main.py hands the Stop hook's `session_id` to the judge, so the REVIEW page can band the row."""
    end_turn(bridge, "All done, tests pass.", "msg_1")
    bridge.wait_for(False)
    assert bridge.laya.decides[-1]["ledger"]["conversation"] == CONVERSATION
    end_turn(bridge, "All done again.", "msg_2", conversation="$(not a uuid)")
    deadline = time.monotonic() + 5
    while len(bridge.laya.decides) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "conversation" not in bridge.laya.decides[-1]["ledger"]


def test_a_restart_brings_back_the_done_mark_of_the_same_turn(bridge, monkeypatch):
    """The 2026-11-04 report: ✅ landed, the bridge restarted a second later, ○."""
    end_turn(bridge, "All done, tests pass.", "msg_1")
    assert bridge.wait_for(False)["work_done"] is True
    decides = len(bridge.laya.decides)

    store, _ = restart(bridge, monkeypatch)
    row = store.get("vc-1")
    assert row["work_done"] is True and row["open_work"] is False and row["open_work_p"] == 0.05
    assert len(bridge.laya.decides) == decides  # restored, never re-asked


def test_a_restored_warning_is_not_shown_twice_and_its_dismiss_still_reaches_the_ledger(bridge, monkeypatch):
    end_turn(bridge, "Phase C is not started.", "msg_1")
    assert bridge.wait_for(True)["open_work"] is True
    assert bridge.reacts(1) == [(decision_id(1), "shown")]

    store, cls = restart(bridge, monkeypatch)
    assert store.get("vc-1")["open_work"] is True
    h = object.__new__(cls)
    raw = json.dumps({"session": "vc-1"}).encode()
    h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.path = "/open-work/dismiss"
    sent = {}
    h._send_json = lambda status, body: sent.update(status=status, body=body)
    getattr(h, cls.POST_ROUTES["/open-work/dismiss"])()
    assert sent["body"]["cleared"] is True
    assert bridge.reacts(2) == [(decision_id(1), "shown"), (decision_id(1), "dismissed")]
    assert SessionVerdictStore(bridge.verdict_path).get("vc-1") is None  # and it stays dismissed


def test_a_background_row_gets_its_verdict_back_too(bridge, monkeypatch):
    end_turn(bridge, "Phase C is not started.", "msg_1")
    bridge.wait_for(True)
    store, _ = restart(bridge, monkeypatch, state="background")
    assert store.get("vc-1")["open_work"] is True


def wait_row(store, key, value, session="vc-1"):
    """A restarted bridge's row once `key` reads `value` (its judge runs on its own thread)."""
    deadline = time.monotonic() + 5
    while store.get(session)[key] is not value and time.monotonic() < deadline:
        time.sleep(0.01)
    return store.get(session)


def test_a_turn_that_ended_while_the_bridge_was_down_is_judged_on_its_own_text(bridge, monkeypatch):
    """2026-11-11: a turn's Stop met ECONNREFUSED three seconds before the bridge was back, so Laya
    never judged it and the REVIEW page had nothing to grade. It never gets the OLD turn's mark, and it is
    judged now, on its own closing message (open_work.py `ledger_judged` owns why)."""
    end_turn(bridge, "Phase C is not started.", "msg_1")
    bridge.wait_for(True)
    bridge.say("Phase C is done too, all green.", "msg_2")  # its Stop reached no bridge

    store, _ = restart(bridge, monkeypatch)
    row = wait_row(store, "work_done", True)
    assert row["open_work"] is False and row["work_done"] is True
    assert bridge.laya.decides[-1]["state"] == "Phase C is done too, all green."
    assert bridge.laya.decides[-1]["ledger"]["conversation"] == CONVERSATION
    assert SessionVerdictStore(bridge.verdict_path).get("vc-1")["verdict"]["state_sha"] == state_sha(
        "Phase C is done too, all green."
    )


def test_a_missed_turn_end_is_judged_once_across_restarts(bridge, monkeypatch):
    bridge.say("Phase C is not started.", "msg_1")  # a Stop that reached no bridge
    store, _ = restart(bridge, monkeypatch)
    assert wait_row(store, "open_work", True)["open_work"] is True
    decides = len(bridge.laya.decides)
    store, _ = restart(bridge, monkeypatch)
    assert store.get("vc-1")["open_work"] is True  # the kept copy, restored
    assert len(bridge.laya.decides) == decides


def test_a_dismissed_turn_is_not_judged_again_after_a_restart(bridge, monkeypatch):
    """✗ drops the kept copy (verdicts.py), so only the ledger still knows the turn was judged; a restart
    that judged it again would put back the ⚠️ the operator took down."""
    end_turn(bridge, "Phase C is not started.", "msg_1")
    bridge.wait_for(True)
    assert bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]["cleared"] is True
    decides = len(bridge.laya.decides)

    store, _ = restart(bridge, monkeypatch)
    time.sleep(0.1)  # a judge, had one been queued, runs with no settle in this fixture
    assert store.get("vc-1")["open_work"] is None
    assert len(bridge.laya.decides) == decides


@pytest.mark.parametrize("plugin_says", ["unsure", "no door"])
def test_a_missed_turn_end_is_not_judged_when_the_ledger_cannot_say(bridge, monkeypatch, plugin_says):
    if plugin_says == "unsure":
        bridge.laya.ledger_unsure = True
    else:
        monkeypatch.setattr(bridge.laya, "judged_before", None)  # a plugin from before the door
    bridge.say("Phase C is not started.", "msg_1")
    store, _ = restart(bridge, monkeypatch)
    time.sleep(0.1)
    assert store.get("vc-1")["open_work"] is None
    assert bridge.laya.decides == []


def test_ledger_judged_answers_only_what_the_plugin_can_say():
    class Plugin:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def judged_before(self, conversation, state_hash):
            self.asked.append((conversation, state_hash))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    found = Plugin(True)
    assert ledger_judged(found, CONVERSATION, "All done.") is True
    assert found.asked == [(CONVERSATION, state_sha("All done."))]
    assert ledger_judged(Plugin(False), CONVERSATION, "All done.") is False
    assert ledger_judged(Plugin("yes"), CONVERSATION, "All done.") is None
    assert ledger_judged(Plugin(RuntimeError("ledger on fire")), CONVERSATION, "All done.") is None
    assert ledger_judged(object(), CONVERSATION, "All done.") is None
    assert ledger_judged(None, CONVERSATION, "All done.") is None
    not_asked = Plugin(False)
    assert ledger_judged(not_asked, "$(not a uuid)", "All done.") is None and not_asked.asked == []


def test_another_conversation_in_the_same_session_is_not_given_the_old_mark(bridge, monkeypatch):
    end_turn(bridge, "All done, tests pass.", "msg_1")
    bridge.wait_for(False)
    store, _ = restart(bridge, monkeypatch, conversation="9f9f9f9f-0000-4000-8000-000000000000")
    assert store.get("vc-1")["work_done"] is None


@pytest.mark.parametrize("clear", ["UserPromptSubmit", "SessionStart", "dismiss"])
def test_whatever_clears_the_row_also_drops_the_copy_kept_for_a_restart(bridge, clear):
    end_turn(bridge, "Phase C is not started.", "msg_1")
    bridge.wait_for(True)
    assert SessionVerdictStore(bridge.verdict_path).get("vc-1")["verdict"]["p"] == 0.95
    if clear == "dismiss":
        bridge.post("/open-work/dismiss", payload={"session": "vc-1"})
    else:
        bridge.hook(clear)
    assert SessionVerdictStore(bridge.verdict_path).get("vc-1") is None


def test_a_verdict_without_its_conversation_is_never_kept(bridge):
    bridge.say("All done, tests pass.", "msg_1")
    bridge.hook("Stop")  # no session_id: rule 2 could never pass, so nothing is written
    bridge.wait_for(False)
    assert SessionVerdictStore(bridge.verdict_path).get("vc-1") is None


def test_a_ledger_that_raises_never_breaks_the_hook_path(bridge):
    bridge.laya.react_raises = True
    bridge.say("Phase C is not started.", "msg_1")
    bridge.hook("Stop")
    assert bridge.wait_for(True)["open_work"] is True
    wait_until(lambda: bridge.laya.react_attempts >= 1)  # `shown` was tried, and raised
    assert bridge.hook("UserPromptSubmit")["status"] == 200
    assert bridge.store.get("vc-1")["open_work"] is None
    bridge.say("Phase D is not started.", "msg_2")
    bridge.hook("Stop")
    assert bridge.wait_for(True)["open_work"] is True
    assert bridge.laya.reacts == []


# ── the landing record (open_work.py § The landing record) ──────────────────

LANDING_ID = "20261109T030000-0123456789ab"
LANDING_TRACE = {"ran_tests": True, "edited_files": 2, "opened_pr": False, "ended_on_question": True}


def test_a_landing_body_needs_the_ledgers_decision_id():
    running = {"descendants": 1, "agents": 0, "workflows": 2, "shells": 0}
    assert open_work.landing_body({"decision_id": None}, "new", None, running, LANDING_TRACE) is None
    assert open_work.landing_body({"decision_id": "not-an-id"}, "new", None, running, LANDING_TRACE) is None
    body = open_work.landing_body({"decision_id": LANDING_ID, "p": 0.9}, "held", "children", running, LANDING_TRACE)
    assert body == {
        "decision_id": LANDING_ID,
        "surface": "agent_open_work",
        "class": "held",
        "held": "children",
        "running": running,
        "trace": LANDING_TRACE,
    }


def test_a_landing_body_never_sends_a_half_trace_or_a_bad_count():
    body = open_work.landing_body(
        {"decision_id": LANDING_ID}, None, None, {"agents": True, "workflows": -1, "shells": 3}, {"ran_tests": True}
    )
    assert body["running"] == {"descendants": 0, "agents": 0, "workflows": 0, "shells": 3}
    assert body["trace"] is None


def test_post_landing_returns_the_plugins_status_and_never_raises_on_an_old_plugin():
    class Old(FakeLayaPlugin):
        def handle_post(self, subpath, query, body):
            return (404, {"error": "not found"}) if subpath == "/landing" else super().handle_post(subpath, query, body)

    body = open_work.landing_body({"decision_id": LANDING_ID}, "new", None, {}, None)
    assert open_work.post_landing(plugin_caller(Old()), body) == 404


# ── a label given elsewhere (open_work.py § The operator's label) ─────────────


@pytest.fixture
def live(monkeypatch):
    """vc-1 is a live tmux session: the snapshot's attention fields ride live rows only."""
    import tldrack.main as main_module

    monkeypatch.setattr(
        main_module, "list_tmux_sessions", lambda: {"vc-1": {"title": None, "attached": False, "cwd": None, "profile": None}}
    )


def snapshot_row(bridge, cls=None):
    snapshot = object.__new__(cls or bridge.cls).snapshot_payload()
    return next(item for item in snapshot["sessions"] if item["session"] == "vc-1")


def marks(row):
    """(⚠️, ✅, 📖 unread, 📖 read)."""
    return row["open_work"], row["work_done"], row["answer_to_read"], row["answer_read"]


@pytest.mark.parametrize(
    ("label", "fields"),
    [
        ({"open": True, "needs": "you"}, (True, False, False, False)),
        ({"open": True, "needs": "running"}, (True, False, False, False)),
        ({"open": True, "needs": None}, (True, False, False, False)),
        ({"open": False, "needs": None}, (False, True, False, False)),
        ({"open": False, "needs": "read"}, (False, True, True, False)),
    ],
)
def test_a_label_decides_the_band_and_layas_p_stays(label, fields):
    for p in (0.05, 0.55, 0.95):  # ✅, 💬 and ⚠️ by Laya's own cuts
        verdict = {"p": p, "at": OPEN_WORK_AT, "done_at": DONE_AT, "asked": True, "operator": label}
        assert marks(verdict_row_fields(verdict)) == fields
        assert verdict_row_fields(verdict)["open_work_p"] == p
    read = verdict_row_fields({"p": 0.9, "at": OPEN_WORK_AT, "done_at": DONE_AT, "read": True, "operator": label})
    assert read["answer_to_read"] is False, "a read answer is read, whoever said it needed one"
    assert read["answer_read"] is (label == {"open": False, "needs": "read"})


def test_a_bare_done_label_draws_the_check_even_on_a_question():
    """DONE without `read` is the operator saying nothing is owed a read: 📖 is Laya's guess, the label is theirs."""
    asked = {"p": 0.05, "at": OPEN_WORK_AT, "done_at": DONE_AT, "asked": True}
    assert verdict_row_fields(asked)["answer_to_read"] is True
    assert verdict_row_fields({**asked, "operator": {"open": False, "needs": None}})["answer_to_read"] is False


@pytest.mark.parametrize(
    ("value", "cleaned"),
    [
        ({"open": True, "needs": "you"}, {"open": True, "needs": "you"}),
        ({"open": False}, {"open": False, "needs": None}),
        ({"open": False, "needs": "read", "extra": 1}, {"open": False, "needs": "read"}),
        ({"open": True, "needs": "read"}, None),  # `read` rides beside DONE only
        ({"open": False, "needs": "you"}, None),
        ({"open": "yes"}, None),
        ({"open": 1}, None),
        ({"open": True, "needs": "later"}, None),
        ("open", None),
        (None, None),
    ],
)
def test_clean_operator_label(value, cleaned):
    assert clean_operator_label(value) == cleaned


def test_label_change_reads_only_this_racks_surface_and_well_formed_entries():
    entry = {"surface": LEDGER_SURFACE, "decision_id": decision_id(1), "open": True, "needs": "you"}
    assert label_change(entry) == (decision_id(1), {"open": True, "needs": "you"})
    assert label_change({**entry, "open": None, "needs": None}) == (decision_id(1), None), "no label stands"
    assert label_change({**entry, "surface": "paste"}) is None
    assert label_change({**entry, "decision_id": "../x"}) is None
    assert label_change({**entry, "needs": "read"}) is None
    assert label_change([entry]) is None


def test_watch_operator_labels_never_raises():
    class Exploding:
        def watch_labels(self, listener):
            raise RuntimeError("plugin on fire")

    assert watch_operator_labels(None, print) is False
    assert watch_operator_labels(object(), print) is False, "a plugin from before the door"
    assert watch_operator_labels(Exploding(), print) is False
    laya = FakeLayaPlugin()
    assert watch_operator_labels(laya, print) is True and laya.label_listener is print


def test_a_done_turn_the_operator_labels_open_draws_the_warning_and_an_undo_brings_the_check_back(bridge, live):
    """The report of 2026-11-26: a ✅ flipped to NEEDS YOU on LAYA REVIEW stayed ✅ on the rack."""
    end_turn(bridge, "All done, tests pass.", "msg_1")
    assert marks(bridge.wait_for(False)) == (False, True, False, False)

    bridge.laya.label(decision_id(1), True, "you")
    row = snapshot_row(bridge)
    assert marks(row) == (True, False, False, False) and row["open_work_p"] == 0.05
    assert row["open_work_labelled"] is True
    assert row["open_work_graded"] is True, "the label is the grade: the menu offers no second ✓"

    bridge.laya.label(decision_id(1), None)  # UNDO on the deck
    row = snapshot_row(bridge)
    assert marks(row) == (False, True, False, False)
    assert "open_work_labelled" not in row and "open_work_graded" not in row
    assert bridge.laya.grades == [], "the bridge records nothing back: the label is already the plugin's"


def test_a_warning_labelled_done_draws_the_check_and_read_draws_the_book_until_the_pane_is_opened(bridge, live):
    warn(bridge)
    bridge.laya.label(decision_id(1), False)
    assert marks(snapshot_row(bridge)) == (False, True, False, False)

    bridge.laya.label(decision_id(1), False, "read")  # regraded on the RECENT list: READ
    assert marks(snapshot_row(bridge)) == (False, True, True, False)
    assert bridge.post("/open-work/read", payload={"session": "vc-1"})["body"]["cleared"] is True
    assert marks(snapshot_row(bridge)) == (False, True, False, True), "☑️📖: the 📖 stays once read"


def test_a_label_of_another_decision_or_surface_repaints_nothing(bridge, live):
    end_turn(bridge, "All done, tests pass.", "msg_1")
    bridge.wait_for(False)
    bridge.laya.label(decision_id(9), True, "you")  # an older turn's text
    bridge.laya.label(decision_id(1), True, "you", surface="paste")
    bridge.laya.label_listener(["not a change", {"surface": LEDGER_SURFACE, "decision_id": decision_id(1), "open": "x"}])
    row = snapshot_row(bridge)
    assert marks(row) == (False, True, False, False) and "open_work_labelled" not in row


def test_a_label_of_the_last_turn_never_repaints_the_next_one(bridge, live):
    end_turn(bridge, "All done, tests pass.", "msg_1")
    bridge.wait_for(False)
    bridge.hook("UserPromptSubmit")
    end_turn(bridge, "Phase C is not started.", "msg_2")
    assert bridge.wait_for(True)["open_work"] is True
    bridge.laya.label(decision_id(1), True, "you")
    bridge.laya.label(decision_id(1), False)
    assert marks(snapshot_row(bridge)) == (True, False, False, False), "decision 2 is Laya's until it is labelled"


def test_a_label_survives_a_restart_and_its_undo_does_too(bridge, monkeypatch):
    end_turn(bridge, "All done, tests pass.", "msg_1")
    bridge.wait_for(False)
    bridge.laya.label(decision_id(1), True, "running")
    store, cls = restart(bridge, monkeypatch)
    row = store.get("vc-1")
    assert (row["open_work"], row["work_done"]) == (True, False), "a correction never comes back as what it corrected"
    assert store.graded_open_work() == {"vc-1"} and store.labelled_open_work() == {"vc-1"}

    bridge.laya.label(decision_id(1), None)  # reaches the RESTARTED bridge, the one subscribed now
    assert store.get("vc-1")["work_done"] is True
    store, _ = restart(bridge, monkeypatch)
    assert store.get("vc-1")["work_done"] is True and store.labelled_open_work() == set()


def test_a_read_label_given_after_the_pane_was_opened_draws_it_read(bridge, live):
    """Opening the pane read the answer, 📖 or not: a READ label given later never asks for that read again."""
    end_turn(bridge, "Phase C is not started.", "msg_1")
    assert bridge.wait_for(True)["open_work"] is True
    assert bridge.post("/open-work/read", payload={"session": "vc-1"})["body"]["cleared"] is False, "no 📖 to clear"
    bridge.laya.label(decision_id(1), False, "read")
    assert marks(snapshot_row(bridge)) == (False, True, False, True), "read already: ☑️📖, never 📖 again"
