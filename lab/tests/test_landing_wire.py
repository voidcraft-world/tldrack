"""tldrack's `/landing` body against the plugin that accepts it: the two cannot import each other.

The row shape's owner is schemas/src/decide.ts (plugins/laya/tests/test_contract.py pins the plugin
side to it); this venv holds both Python sides, so it pins the bridge's copy to the plugin's.
"""

from __future__ import annotations

import pytest

from laya_bridge import landing
from tldrack import attention, open_work

DECISION_ID = "20261109T030000-0123456789ab"
RUNNING = {"descendants": 1, "agents": 0, "workflows": 2, "shells": 0}
TRACE = {"ran_tests": True, "edited_files": 4, "opened_pr": True, "ended_on_question": False}


def test_the_copied_names_match_the_plugin():
    assert open_work.LANDING_ROUTE == "/landing"
    assert open_work.LANDING_RUNNING_KEYS == landing.RUNNING_KEYS
    assert open_work.LANDING_TRACE_KEYS == landing.TRACE_KEYS
    assert open_work.LEDGER_SURFACE in landing.SURFACES
    assert attention.LANDING_CLASSES == landing.CLASSES
    assert attention.HELD_REASONS == landing.HELD_REASONS


@pytest.mark.parametrize(
    "kind, held, trace",
    [
        ("held", "children", TRACE),
        ("held", "background", None),
        ("new", None, TRACE),
        ("repeat", None, TRACE),
        ("retired", "children", TRACE),
        ("retired", None, None),
        (None, None, TRACE),
        (None, "background", None),
    ],
)
def test_every_body_the_bridge_builds_is_one_the_plugin_accepts(kind, held, trace):
    body = open_work.landing_body({"decision_id": DECISION_ID}, kind, held, RUNNING, trace)
    assert list(body) == list(landing.BODY_KEYS)
    assert landing.clean_landing(body) == body


def test_the_bridge_builds_only_what_the_classifier_can_produce():
    """`attention.landing_class` never pairs new/repeat with a reason, which the plugin refuses."""
    for kind in (None, *attention.CLASSES):
        for reason in (None, *attention.HELD_REASONS):
            body = open_work.landing_body(
                {"decision_id": DECISION_ID}, attention.landing_class(kind, reason), reason, RUNNING, TRACE
            )
            landing.clean_landing(body)


WARNING_STATE = "Phase C is not started."


def recorded_warning(tmp_path):
    """A synthetic laya store whose ledger holds one ⚠️ the rack drew: `(base, verdict)`."""
    from laya_bridge.ledger import Ledger, clean_tag
    from laya_bridge.wire import clean_request

    base = tmp_path / "data" / "laya"
    (base / "data").mkdir(parents=True)
    tag = clean_tag(open_work.ledger_tag("vc-1", open_work.OPEN_WORK_AT, open_work.DONE_AT), rack_ok=True)
    request = clean_request({"state": WARNING_STATE, "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
    decision_id = Ledger(lambda: base).record_decision(tag, request, {"answers": {"q": {"noul": 0.93}}}, "english", None, None, 5)
    return base, {"p": 0.93, "at": open_work.OPEN_WORK_AT, "decision_id": decision_id}


def test_the_bridge_s_rack_grade_is_one_the_plugin_records(tmp_path):
    """`open_work.grade_verdict` against the plugin's own handler: ✗ NOT OPEN on a ⚠️ becomes a
    operator flip to DONE in stratum `rack`, and a second answer is `kept`. The plugin object itself needs the bridge
    SDK this venv lacks, so the grader here is the handler `plugin.rack_grade` ends in, without its caller check
    (plugins/laya/tests/test_rack_door.py owns that)."""
    from laya_bridge import review, review_data, review_log

    base, verdict = recorded_warning(tmp_path)
    decision_id = verdict["decision_id"]
    sent: list[dict] = []

    def grade(body):
        sent.append(body)
        return review.rack(base, body)

    assert open_work.grade_verdict(grade, verdict, is_open_now=False) == open_work.GRADE_RECORDED
    assert open_work.grade_verdict(grade, verdict, is_open_now=True) == open_work.GRADE_KEPT
    assert sent == [{"decision_id": decision_id, "open": False}, {"decision_id": decision_id, "open": True}]
    [row] = review_log.read_fold(base, open_work.LEDGER_SURFACE)[1].labels.values()
    assert (row["stratum"], row["answer"], row["open"], row["decision_id"]) == (
        review_data.RACK_STRATUM,
        "flip",
        False,
        decision_id,
    )


def test_a_rack_grade_on_a_text_the_deck_skipped_is_recorded_never_reported_as_kept(tmp_path):
    """Both sides of the skip rule at once: the plugin records the press, so the bridge never reads `kept` over a
    skip. Why: owned by plugins/laya/laya_bridge/review.py `rack` — see the note there."""
    from laya_bridge import review, review_log, rows

    base, verdict = recorded_warning(tmp_path)
    surface, key = open_work.LEDGER_SURFACE, rows.state_key(WARNING_STATE)
    status, skipped = review.answer(base, {"surface": surface, "key": key, "answer": "skip", "reference_open": True})
    assert status == 200 and skipped["answer"]["open"] is None
    assert open_work.grade_verdict(lambda body: review.rack(base, body), verdict, is_open_now=True) == open_work.GRADE_RECORDED
    fold = review_log.read_fold(base, surface)[1]
    assert fold.labels[key]["open"] is True and key not in fold.skipped and len(fold.live) == 1
