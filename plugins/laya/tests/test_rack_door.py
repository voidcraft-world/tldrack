"""The agent rack's grade (`/review/rack`), the `challenger` stratum and the daily phone door's count —
over synthetic trees only."""

from __future__ import annotations

import pytest

from laya_bridge import lab_files, review, review_data, review_log, rows
from laya_bridge.plugin import LayaPlugin
from laya_bridge.review_surfaces import REVIEW_SURFACES

from conftest import CHECKPOINT, FINE_TUNE
from test_registry_v2 import RIVAL, v2
from test_review_data import SURFACE, operator, pick_where, public_golden, record, snap, strata  # noqa: F401

OTHER = f"{FINE_TUNE}-20261105-0900"


def plain(prefix: str, n: int) -> list[str]:
    """Texts no uniform sample draws: not `live_sample`, not the `unflagged` sample."""
    return pick_where(prefix, n, live=False, unflagged=False)


def shadow(
    tree,
    decision_id,
    text,
    served_p,
    challenger_p,
    *,
    challenger=RIVAL,
    served_checkpoint=CHECKPOINT,
    scored_at="2026-11-10T04:00:00Z",
    cut=0.7,
):
    tree.path("shadow/x")
    tree.append_jsonl(
        f"shadow/{SURFACE}.jsonl",
        lab_files.shadow_row(
            decision_id=decision_id,
            key=rows.state_key(text),
            at="2026-11-09T03:15:00Z",
            question_version=1,
            served_model=FINE_TUNE,
            served_checkpoint=served_checkpoint,
            served_p=served_p,
            served_threshold=0.7,
            served_done_threshold=0.4,
            challenger=challenger,
            challenger_digest=None,
            device="mps",
            challenger_p=challenger_p,
            challenger_threshold=cut,
            challenger_threshold_source="code",
            challenger_done_threshold=0.4,
            scored_at=scored_at,
        ),
    )


# -- the challenger stratum --------------------------------------------------------


def test_a_disagreement_on_the_newest_decision_is_dealt_as_challenger_never_as_disagree(tree):
    v2(tree)
    flagged, quiet, same = plain("rival", 3)
    ids = [
        record(tree, flagged, 0.9, model=FINE_TUNE),
        record(tree, quiet, 0.2, model=FINE_TUNE),
        record(tree, same, 0.9, model=FINE_TUNE),
    ]
    shadow(tree, ids[0], flagged, 0.9, 0.3)  # the champion flagged, the challenger would not
    shadow(tree, ids[1], quiet, 0.2, 0.8)  # the other way round
    shadow(tree, ids[2], same, 0.9, 0.95)  # both flag: no disagreement
    view = snap(tree)
    found = strata(view)
    assert found[rows.state_key(flagged)] == found[rows.state_key(quiet)] == "challenger"
    assert found[rows.state_key(same)] == "flagged"
    assert view.items.challenger == RIVAL and review_data.sources(view)["shadow"] == "ok"
    card = review_data.card(view, rows.state_key(flagged))
    assert card["challenger"] == {"name": RIVAL, "p": 0.3, "threshold": 0.7}
    assert card["reference"]["open"] is True, "AGREE sides with the champion, FLIP with the challenger"
    assert review_data.card(view, rows.state_key(same))["challenger"] is None
    assert "challenger" in dict(REVIEW_SURFACES[SURFACE].weights) and "challenger" in review_data.TO_REVIEW
    assert rows.state_key(flagged) in view.items.flagged_rival and rows.state_key(flagged) not in view.items.flagged_far


def test_no_challenger_no_stratum_and_the_shadow_file_is_not_read(tree):
    v2(tree, challenger=None)
    text = plain("alone", 1)[0]
    shadow(tree, record(tree, text, 0.9, model=FINE_TUNE), text, 0.9, 0.1)
    view = snap(tree)
    assert strata(view)[rows.state_key(text)] == "flagged" and view.items.challenger is None
    assert "shadow" not in review_data.sources(view)
    assert review_data.stats(view, tree.base)["challenger"] is None


def test_rows_of_another_challenger_another_champion_or_an_older_decision_are_not_disagreements(tree):
    v2(tree)
    gone, promoted, rejudged = plain("stale", 3)
    shadow(tree, record(tree, gone, 0.9, model=FINE_TUNE), gone, 0.9, 0.1, challenger=OTHER)
    shadow(tree, record(tree, promoted, 0.9, model=FINE_TUNE), promoted, 0.9, 0.1, served_checkpoint=OTHER)
    old = record(tree, rejudged, 0.9, model=FINE_TUNE)
    record(tree, rejudged, 0.9, model=FINE_TUNE)  # judged again: the card shows this one, which no shadow row scored
    shadow(tree, old, rejudged, 0.9, 0.1)
    assert set(strata(snap(tree)).values()) == {"flagged"}


def test_the_newest_score_of_a_decision_wins(tree):
    v2(tree)
    text = plain("rescored", 1)[0]
    decision_id = record(tree, text, 0.9, model=FINE_TUNE)
    shadow(tree, decision_id, text, 0.9, 0.1, scored_at="2026-11-10T04:00:00Z")
    shadow(tree, decision_id, text, 0.9, 0.8, scored_at="2026-11-11T04:00:00Z")  # the re-trained folder agrees
    assert strata(snap(tree))[rows.state_key(text)] == "flagged"


def test_a_uniform_sample_keeps_its_member_and_a_challenger_card_never_rides_a_wave(tree):
    v2(tree)
    member = pick_where("member", 1, live=True, unflagged=None)[0]
    shadow(tree, record(tree, member, 0.9, model=FINE_TUNE), member, 0.9, 0.1)
    assert strata(snap(tree))[rows.state_key(member)] == "live_sample"
    assert "challenger" in review_data.WAVE_NEVER


def test_sides_count_only_challenger_stratum_grades_of_this_challenger(tree):
    v2(tree)
    texts = plain("side", 4)
    ids = [record(tree, text, 0.9, model=FINE_TUNE) for text in texts]
    for decision_id, text in zip(ids, texts):
        shadow(tree, decision_id, text, 0.9, 0.3)
    block = {"name": RIVAL, "p": 0.3, "threshold": 0.7}
    keys = [rows.state_key(text) for text in texts]

    def grade(key, verdict, stratum="challenger", challenger=block):
        row = review_log.answer_row(
            answer_id=review_log.new_answer_id(),
            at="2026-11-10T05:00:00Z",
            surface=SURFACE,
            key=key,
            stratum=stratum,
            answer=verdict,
            against={"by": FINE_TUNE, "open": True},
            laya=None,
            decision_id=None,
            held_out=True,
            challenger=challenger,
        )
        tree.append_jsonl("data/agent-open-work-operator.jsonl", row)

    tree.path("data/x")
    grade(keys[0], "flip")  # DONE: the challenger was right
    grade(keys[1], "agree")  # OPEN: the champion was
    grade(keys[2], "flip", stratum="rack", challenger=None)  # the rack's door: not counted
    grade(keys[3], "flip", challenger={**block, "name": OTHER})  # another challenger's disagreement
    sides = review_data.stats(snap(tree), tree.base)["challenger"]
    assert sides == {"name": RIVAL, "graded": 2, "with_challenger": 1, "remaining": 0}


def test_an_answer_to_a_challenger_card_records_the_challengers_block(tree):
    v2(tree)
    text = plain("answered", 1)[0]
    shadow(tree, record(tree, text, 0.9, model=FINE_TUNE), text, 0.9, 0.3)
    key = rows.state_key(text)
    tree.path("data/x")
    status, body = review.answer(tree.base, {"surface": SURFACE, "key": key, "answer": "flip", "reference_open": True})
    assert status == 200, body
    row = review_log.read_fold(tree.base, SURFACE)[1].labels[key]
    assert row["stratum"] == "challenger" and row["challenger"] == {"name": RIVAL, "p": 0.3, "threshold": 0.7}
    assert body["stats"]["challenger"] == {"name": RIVAL, "graded": 1, "with_challenger": 1, "remaining": 0}


def test_a_malformed_challenger_block_is_a_bad_line(tree):
    row = operator(tree, "0" * 16, stratum="challenger")
    tree.append_jsonl(
        "data/agent-open-work-operator.jsonl",
        {**row, "answer_id": review_log.new_answer_id(), "key": "1" * 16, "challenger": {"name": RIVAL, "p": 2}},
    )
    fold = review_log.read_fold(tree.base, SURFACE)[1]
    assert list(fold.labels) == ["0" * 16] and fold.bad_lines == 1


# -- the rack's door ---------------------------------------------------------------


@pytest.fixture
def plugin(tree, fake_runtime):
    tree.path("data/x")
    return LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)


#: The headers of the rack's own press: the SPA on a dev port, posting JSON to the bridge on loopback.
RACK_HEADERS = {"host": "127.0.0.1:7682", "origin": "http://127.0.0.1:5173", "content-type": "application/json"}


def rack(plugin, decision_id, is_open, headers=RACK_HEADERS):
    return plugin.rack_grade({"decision_id": decision_id, "open": is_open}, headers)


def operator_log(tree):
    return tree.base / "data" / "agent-open-work-operator.jsonl"


def test_not_open_on_a_warning_is_a_operator_flip_to_done_in_stratum_rack(tree, plugin):
    text = plain("rack", 1)[0]
    decision_id = record(tree, text, 0.9)
    status, body = rack(plugin, decision_id, False)
    assert status == 200 and body["ok"] and body["answer"]["answer"] == "flip" and body["answer"]["open"] is False
    row = review_log.read_fold(tree.base, SURFACE)[1].labels[rows.state_key(text)]
    assert row["stratum"] == review_data.RACK_STRATUM == "rack" and row["by"] == "operator"
    assert row["against"] == {"by": "english", "open": True} and row["decision_id"] == decision_id
    assert row["laya"] == {"p": 0.9, "threshold": 0.7, "source": "ledger", "checkpoint_path": None}
    view = snap(tree)
    assert view.fold.answered(rows.state_key(text)) and rows.state_key(text) not in view.remaining["flagged"]
    assert review_data.stats(view, tree.base)["by_stratum"]["rack"] == {"reviewed": 1, "remaining": 0, "open": 0}


def test_still_open_is_an_agree_and_the_grade_is_undone_like_any_other(tree, plugin):
    text = plain("still", 1)[0]
    decision_id = record(tree, text, 0.9)
    status, body = rack(plugin, decision_id, True)
    assert status == 200 and body["answer"]["answer"] == "agree" and body["answer"]["open"] is True
    status, again = rack(plugin, decision_id, False)
    assert status == 409 and again["code"] == "already_answered" and again["answer"]["open"] is True
    status, undone = review.undo(tree.base, {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]})
    assert status == 200 and not snap(tree).fold.answered(rows.state_key(text))


def test_a_skipped_text_takes_the_rack_grade_and_keeps_one_live_answer(tree, plugin):
    """A deck SKIP is not a label: the press undoes it and records the answer, in one write."""
    text = plain("skipped", 1)[0]
    decision_id = record(tree, text, 0.9)
    key = rows.state_key(text)
    status, skipped = review.answer(tree.base, {"surface": SURFACE, "key": key, "answer": "skip", "reference_open": True})
    assert status == 200 and key in snap(tree).fold.skipped
    status, body = rack(plugin, decision_id, True)
    assert status == 200 and body["answer"]["answer"] == "agree" and body["answer"]["open"] is True
    fold = review_log.read_fold(tree.base, SURFACE)[1]
    assert fold.labels[key]["stratum"] == "rack" and key not in fold.skipped
    assert [row["key"] for row in fold.live.values()] == [key] and fold.undone == {skipped["answer"]["answer_id"]}
    assert [row["op"] for row in review_log.read_operator_log(operator_log(tree))[0]] == ["answer", "undo", "answer"]
    # The label now stands like any other: a second press keeps it, and UNDO leaves the text unanswered, not skipped.
    status, again = rack(plugin, decision_id, False)
    assert status == 409 and again["code"] == "already_answered" and again["answer"]["open"] is True
    assert review.undo(tree.base, {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]})[0] == 200
    assert not snap(tree).fold.answered(key)


def test_a_rack_grade_is_recorded_against_the_decision_named_never_the_sonnet_label(tree, plugin):
    """Sonnet said DONE, Laya drew a ⚠️: ✗ NOT OPEN overrules LAYA, so it is a flip (a PROMOTE candidate), not an
    agree with Sonnet. And a text judged twice is graded against the verdict the press named."""
    text = pick_where("sonnet", 1, sample=False)[0]
    key = tree.unlabelled([text])[0]
    tree.labels({key: False})
    first = record(tree, text, 0.9)
    record(tree, text, 0.5)  # judged again, under the cut
    status, body = rack(plugin, first, False)
    assert status == 200 and body["answer"]["answer"] == "flip"
    row = review_log.read_fold(tree.base, SURFACE)[1].labels[key]
    assert row["against"] == {"by": "english", "open": True} and row["laya"]["p"] == 0.9 and row["decision_id"] == first


def test_unknown_malformed_and_textless_decisions_record_nothing(tree, plugin):
    assert rack(plugin, "20261109T031500-0123456789ab", False)[1]["code"] == "unknown_decision"
    assert rack(plugin, "nope", False)[0] == 400 and rack(plugin, "20261109T031500-0123456789ab", "no")[0] == 400
    assert plugin.rack_grade({"decision_id": "20261109T031500-0123456789ab"}, RACK_HEADERS)[0] == 400
    cut_off = record(tree, "API Error: 529 overloaded", 0.9)  # a notice the surface does not admit: no card
    status, body = rack(plugin, cut_off, False)
    assert status == 409 and body["code"] == "unknown_item"
    assert not operator_log(tree).exists()


def test_the_rack_grade_has_no_http_door(tree, plugin):
    decision_id = record(tree, plain("http", 1)[0], 0.9)
    body = {"decision_id": decision_id, "open": False}
    for headers in (
        {"host": "127.0.0.1:7682"},
        {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"},
    ):
        status, refused = plugin.handle_request("POST", "/review/rack", {}, body, headers)
        assert status == 403 and refused["error"] == "forbidden"
    assert not any(route.endswith("/review/rack") for route in plugin.routes())
    assert not operator_log(tree).exists()


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({**RACK_HEADERS, "origin": "http://localhost:9999"}, 403, "origin_refused"),  # another page on this Mac
        ({**RACK_HEADERS, "origin": "https://example.com"}, 403, "origin_refused"),
        ({**RACK_HEADERS, "content-type": "text/plain"}, 415, "unsupported_media_type"),  # a simple request
        ({k: v for k, v in RACK_HEADERS.items() if k != "content-type"}, 415, "unsupported_media_type"),
        ({**RACK_HEADERS, "host": "mac.tailnet.ts.net:7682"}, 403, "host_refused"),
        ({**RACK_HEADERS, "x-forwarded-for": "10.0.0.9"}, 403, "host_refused"),
        ({"host": "127.0.0.1:7682", "sec-fetch-site": "cross-site", "content-type": "application/json"}, 403, "no_cors_refused"),
        (None, 403, "host_too_old"),
    ],
)
def test_the_press_that_causes_a_grade_passes_the_deck_s_caller_checks_or_records_nothing(tree, plugin, headers, status, code):
    """The bridge's routes admit any loopback origin; an operator label takes `/review/answer`'s posture."""
    decision_id = record(tree, plain("posture", 1)[0], 0.9)
    answered, refused = rack(plugin, decision_id, False, headers)
    assert (answered, refused["code"]) == (status, code) and not operator_log(tree).exists()
    # No headers at all is the old in-process pair: a caller the plugin cannot see.
    old = plugin.handle_post("/review/rack", {}, {"decision_id": decision_id, "open": False})
    assert (old[0], old[1]["code"]) == (403, "host_too_old") and not operator_log(tree).exists()


def test_a_native_caller_and_mixed_case_header_names_are_judged_like_the_deck_s(tree, plugin):
    native, browser = (record(tree, text, 0.9) for text in plain("native", 2))
    assert rack(plugin, native, False, {"Host": "127.0.0.1:7682", "Content-Type": "application/json; charset=utf-8"})[0] == 200
    assert (
        rack(
            plugin,
            browser,
            False,
            {"Host": "localhost:7682", "Origin": "http://localhost:5173", "Content-Type": "application/json"},
        )[0]
        == 200
    )


# -- the daily phone door ----------------------------------------------------------


def test_the_door_counts_cards_the_phones_recent_list_can_grade(tree):
    v2(tree)
    near = plain("near", 3)  # `uncertain`: a live verdict within 0.1 of the cut
    far = plain("far", 2)  # `flagged`: not a door lane
    rival = plain("doorrival", 1)
    for text in near:
        record(tree, text, 0.72, model=FINE_TUNE)
    for text in far:
        record(tree, text, 0.95, model=FINE_TUNE)
    shadow(tree, record(tree, rival[0], 0.95, model=FINE_TUNE), rival[0], 0.95, 0.2)
    sonnet = pick_where("doorsonnet", 1, sample=False)[0]  # `uncertain` by its SCORE row, but no live verdict
    key = tree.unlabelled([sonnet])[0]
    tree.labels({key: True})
    view = snap(tree)
    assert review_data.door_waiting(view) == 4
    door = review_data.stats(view, tree.base)["door"]
    assert door == {"waiting": 4, "min": review_data.DOOR_MIN} and review_data.DOOR_MIN == 5
    operator(tree, rows.state_key(near[0]), stratum="uncertain")
    assert review_data.stats(snap(tree), tree.base)["door"]["waiting"] == 3


# -- what a label write tells the agent rack (`review.label_changes`) ------------------------------------------------


def told(**fields):
    return {"surface": SURFACE, "needs": None, **fields}


@pytest.fixture
def deck(tree):
    tree.path("data/x")  # where the deck writes its answers
    return tree


def test_an_answer_tells_the_rack_what_now_stands_for_every_decision_of_the_text(deck):
    text = plain("told", 1)[0]
    key = rows.state_key(text)
    first, second = record(deck, text, 0.9), record(deck, text, 0.95)  # one text, judged twice
    heard = []
    body = {"surface": SURFACE, "key": key, "answer": "flip", "reference_open": True, "needs": "read"}
    assert review.answer(deck.base, body, on_labels=heard.append)[0] == 200
    by_id = lambda change: change["decision_id"]  # noqa: E731 — decision ids are random: compare as sets of rows
    assert sorted(heard[0], key=by_id) == sorted(
        [told(decision_id=first, open=False, needs="read"), told(decision_id=second, open=False, needs="read")], key=by_id
    )


def test_a_skip_and_an_undo_tell_the_rack_no_label_stands(deck):
    text = plain("unsaid", 1)[0]
    key = rows.state_key(text)
    decision_id = record(deck, text, 0.9)
    heard = []
    status, body = review.answer(
        deck.base, {"surface": SURFACE, "key": key, "answer": "skip", "reference_open": True}, on_labels=heard.append
    )
    assert status == 200 and heard == [[told(decision_id=decision_id, open=None)]]
    assert review.undo(deck.base, {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]})[0] == 200
    status, body = review.answer(
        deck.base, {"surface": SURFACE, "key": key, "answer": "agree", "reference_open": True}, on_labels=heard.append
    )
    assert heard[-1] == [told(decision_id=decision_id, open=True)]
    assert (
        review.undo(deck.base, {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]}, on_labels=heard.append)[0] == 200
    )
    assert heard[-1] == [told(decision_id=decision_id, open=None)]


def test_a_refused_press_tells_the_rack_nothing(deck):
    text = plain("refused", 1)[0]
    record(deck, text, 0.9)
    heard = []
    body = {"surface": SURFACE, "key": rows.state_key(text), "answer": "agree", "reference_open": False}
    assert review.answer(deck.base, body, on_labels=heard.append)[1]["code"] == "reference_changed"
    assert heard == []


def test_the_plugin_hands_every_label_write_to_its_watchers_and_a_raising_one_costs_nothing(tree, plugin):
    """The bridge's door (`watch_labels`): the deck's answer and undo, and the rack's own grade."""
    text = plain("watched", 1)[0]
    key = rows.state_key(text)
    decision_id = record(tree, text, 0.9)
    heard = []

    def exploding(changes):
        raise RuntimeError("listener on fire")

    plugin.watch_labels(exploding)
    plugin.watch_labels(heard.append)
    answer = {"surface": SURFACE, "key": key, "answer": "flip", "reference_open": True}
    status, body = plugin.handle_request("POST", "/review/answer", {}, answer, RACK_HEADERS)
    assert status == 200 and body["ok"], "a listener's bug never costs the operator the label"
    assert heard == [[told(decision_id=decision_id, open=False)]]
    undo = {"surface": SURFACE, "answer_id": body["answer"]["answer_id"]}
    assert plugin.handle_request("POST", "/review/undo", {}, undo, RACK_HEADERS)[0] == 200
    assert heard[-1] == [told(decision_id=decision_id, open=None)]
    assert rack(plugin, decision_id, True)[0] == 200
    assert heard[-1] == [told(decision_id=decision_id, open=True)]
    assert len(heard) == 3
