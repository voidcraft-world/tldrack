"""The ✅⚠️ training pipeline's pure parts: turn splitting, label parsing, row building."""

from __future__ import annotations

import json
import os
import stat

from laya_lab.data import jsonl_line
from laya_lab.open_work_data import (
    closings,
    export,
    merge_requests,
    merge_unlabelled,
    read_labels,
    read_requests_file,
    stored_request,
    text_key,
    training_rows,
    turn_ends,
)
from laya_bridge import review_data
from tldrack import open_work


def say(message_id, text, **extra):
    return {"type": "assistant", "message": {"id": message_id, "content": [{"type": "text", "text": text}]}, **extra}


def tool_use(message_id):
    return {"type": "assistant", "message": {"id": message_id, "content": [{"type": "tool_use", "name": "Bash"}]}}


def tool_result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}, "toolUseResult": {}}


def prompt(text):
    return {"type": "user", "message": {"content": text}}


def test_each_turn_yields_its_final_message_and_tool_results_do_not_split_a_turn():
    entries = [
        prompt("do A then B"),
        say("m1", "Looking first."),
        tool_use("m1"),
        tool_result(),
        say("m2", "A done; B is next."),
        prompt("go on"),
        say("m3", "B done."),
        say("m3", "All tests pass."),
        say("s1", "subagent chatter", isSidechain=True),
    ]
    assert list(turn_ends(entries)) == ["A done; B is next.", "B done.\nAll tests pass."]


def task_notification(text="<task-notification><task-id>t1</task-id><status>completed</status></task-notification>"):
    return {"type": "user", "message": {"content": text}}


def command(name, args):
    return prompt(f"<command-name>{name}</command-name><command-args>{args}</command-args>")


def test_each_closing_carries_the_request_that_opened_its_turn_and_none_when_nobody_typed_one():
    entries = [
        prompt("do A then B"),
        say("m1", "A done; B is next."),
        task_notification(),
        say("m2", "The agent finished; B is done."),
        command("/ship", "the release"),
        say("m3", "Shipped."),
        {"type": "user", "isMeta": True, "message": {"content": "skill body: the steps"}},  # context, not a turn
        say("m4", "Shipped, and the notes are up."),
    ]
    assert list(closings(entries)) == [
        ("A done; B is next.", "do A then B"),
        ("The agent finished; B is done.", None),
        ("Shipped, and the notes are up.", "/ship the release"),
    ]


def test_a_stored_request_fits_to_the_same_v3_state_as_the_whole_one():
    request = "Plan it:\n\n```\n" + "pasted log\n" * 400 + "```\n" + "then build every step of it " * 80
    hand_off = "Steps 1 and 2 are done; step 3 is next."
    assert len(request) > 2000
    whole = open_work.decide_state(hand_off, request, version=3)
    assert open_work.decide_state(hand_off, stored_request(request), version=3) == whole
    assert stored_request(None) is None and stored_request(" \n ") is None


def test_merge_requests_keeps_what_was_found_and_a_typed_request_beats_none():
    existing = {"a" * 16: "do A", "b" * 16: None}
    heard = {"b" * 16: "do B", "c" * 16: None, "d" * 16: "never wanted"}
    assert merge_requests(existing, heard, {"b" * 16, "c" * 16, "e" * 16}) == {"a" * 16: "do A", "b" * 16: "do B", "c" * 16: None}


def test_export_writes_each_turns_request_beside_the_export_keyed_like_its_texts(tmp_path, umask_022):
    out = tmp_path / "data" / "agent-open-work-unlabelled.jsonl"
    requests_out = out.with_name("agent-open-work-requests.jsonl")
    out.parent.mkdir()
    projects = tmp_path / "claude" / "projects" / "p"
    projects.mkdir(parents=True)
    asked = "Phase two is done; phase three has not started yet."
    woken = "The background agent finished; the report is written."
    ledger_only = "x" * 1400 + " phase four is next."  # past the window: the ledger keeps its fitted state
    entries = (
        prompt("do phases two and three"),
        say("m1", asked),
        task_notification(),
        say("m2", woken),
        prompt("and phase four"),
        say("m3", ledger_only),
    )
    (projects / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    fitted = text_key(open_work.fit_hand_off(ledger_only))
    export(out, [tmp_path / "claude"], limit=2, requests_out=requests_out, also_keys=[fitted])
    exported = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(set(row) == {"key", "hand_off"} for row in exported), "the export's own rows keep their shape"
    found = read_requests_file(requests_out)
    assert {key: found[key] for key in (row["key"] for row in exported)} == {
        row["key"]: {asked: "do phases two and three", woken: None, ledger_only: "and phase four"}[row["hand_off"]]
        for row in exported
    }
    assert found[fitted] == "and phase four"
    assert stat.S_IMODE(os.stat(requests_out).st_mode) == 0o600


def test_a_turn_that_ends_on_a_tool_call_has_no_closing_text():
    assert list(turn_ends([prompt("x"), say("m1", "checking"), tool_use("m2"), prompt("y")])) == []


def test_a_turn_an_api_error_cut_off_yields_nothing_and_a_real_reply_after_it_still_counts():
    notice = say(
        "e1",
        "API Error: Server error mid-response. The response above may be incomplete.",
        isApiErrorMessage=True,
        error="unknown",
    )
    entries = [
        prompt("do A"),
        say("m1", "A is half done."),
        notice,  # cut off: no hand-off
        prompt("retry"),
        say("m2", "Retrying."),
        notice,
        say("m3", "A done."),  # recovered: its reply counts
        prompt("do B"),
        say("m4", "B started."),
        notice,  # cut off at end of file
    ]
    assert list(turn_ends(entries)) == ["A done."]


def test_merge_unlabelled_prunes_cut_off_notices_exported_before_they_were_dropped():
    real = {"key": text_key("All done."), "hand_off": "All done."}
    notices = [
        {"key": text_key(text), "hand_off": text}
        for text in (
            "API Error: 529 Overloaded. This is a server-side issue, usually temporary.",
            "You've hit your weekly limit · resets 12am (Europe/Paris)",
        )
    ]
    assert merge_unlabelled([notices[0], real], [notices[1]], limit=5) == [real]


def test_read_labels_keeps_only_asked_ids_with_boolean_verdicts():
    reply = (
        '{"labels": [{"id": "a", "open": true, "why": "C left"}, {"id": "b", "open": "no", "why": ""},'
        ' {"id": "zzz", "open": false, "why": ""}]}'
    )
    assert read_labels(reply, {"a", "b"}) == {"a": {"open": True, "why": "C left"}}
    assert read_labels("not json", {"a"}) == {}


def test_training_rows_carry_the_served_question_and_state():
    text = "Phase C is not started."
    rows = training_rows([{"key": text_key(text), "hand_off": text}], [{"key": text_key(text), "open": True}])
    assert rows == [
        {
            "key": text_key(text),
            "state": open_work.decide_state(text),
            "questions": {"q": open_work.OPEN_WORK_QUESTION},
            "expected": {"q": True},
        }
    ]


def test_training_rows_leave_out_excluded_keys():
    text = "Phase C is not started."
    unlabelled = [{"key": text_key(text), "hand_off": text}]
    labels = [{"key": text_key(text), "open": True}]
    assert training_rows(unlabelled, labels, exclude=frozenset({text_key(text)})) == []


def row(text):
    return {"key": text_key(text), "hand_off": text}


def test_merge_keeps_every_existing_row_and_caps_only_new_ones():
    old = [row("an older synthetic turn end, still labelled"), row("another older one")]
    fresh = [row("another older one"), row("new one"), row("new two"), row("new three")]
    merged = merge_unlabelled(old, fresh, limit=2)
    assert merged == [*old, row("new one"), row("new two")]
    assert merge_unlabelled(old, fresh, limit=0) == old
    assert merge_unlabelled([{"key": "stale", "hand_off": "  "}, *old], [], 5) == old  # an empty text is not a row


def test_export_merges_into_the_file_and_writes_it_0600(tmp_path, umask_022):
    out = tmp_path / "data" / "agent-open-work-unlabelled.jsonl"
    out.parent.mkdir()
    out.write_text(json.dumps(row("a turn end from a transcript that is gone now, kept")) + "\n")
    projects = tmp_path / "claude" / "projects" / "p"
    projects.mkdir(parents=True)
    closing = "Phase two is done; phase three has not started yet."
    (projects / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (prompt("go"), say("m1", closing))))
    added, total, seen = export(out, [tmp_path / "claude"], limit=10)
    assert (added, total, seen) == (1, 2, 1)
    kept = [json.loads(line)["hand_off"] for line in out.read_text().splitlines()]
    assert kept == ["a turn end from a transcript that is gone now, kept", closing]
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert export(out, [tmp_path / "claude"], limit=10)[:2] == (0, 2)


def size(rows):
    return sum(len(jsonl_line(r).encode("utf-8")) for r in rows)


def test_merge_stops_before_the_file_passes_the_readers_cap():
    old = [row("an older synthetic turn end, still labelled")]
    fresh = [row("new one"), row("new two"), row("new three")]
    assert merge_unlabelled(old, fresh, limit=10, max_bytes=size([*old, fresh[0]])) == [*old, fresh[0]]
    assert merge_unlabelled(old, fresh, limit=10, max_bytes=size([*old, fresh[0]]) - 1) == old
    assert merge_unlabelled(old, fresh, limit=10, max_bytes=1) == old, "a row already exported is never dropped"
    assert merge_unlabelled(old, fresh, limit=10) == [*old, *fresh]


def test_export_stops_at_the_readers_cap_and_says_so(tmp_path, monkeypatch, capsys):
    out = tmp_path / "data" / "agent-open-work-unlabelled.jsonl"
    out.parent.mkdir()
    kept = row("a turn end from a transcript that is gone now, kept")
    out.write_text(json.dumps(kept) + "\n")
    projects = tmp_path / "claude" / "projects" / "p"
    projects.mkdir(parents=True)
    closings = [
        "Phase A is done; phase B has not started yet, still open.",
        "Phase C is done; phase D has not started yet, still open.",
    ]  # equal length: either fits alone
    entries = (prompt("go"), say("m1", closings[0]), prompt("next"), say("m2", closings[1]))
    (projects / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    monkeypatch.setattr(review_data, "UNLABELLED_CAP_BYTES", size([kept, row(closings[0])]))
    added, total, seen = export(out, [tmp_path / "claude"], limit=10)
    assert (added, total, seen) == (1, 2, 2)
    assert out.stat().st_size <= review_data.UNLABELLED_CAP_BYTES
    assert "1 new turn ends left out" in capsys.readouterr().out


def test_curated_sets_are_found_in_the_store_never_named_in_code(tmp_path):
    from laya_lab.open_work_data import CURATION_DIRNAME, read_supplements, supplement_names

    assert supplement_names(tmp_path) == () and read_supplements(tmp_path).missing == ()
    (tmp_path / CURATION_DIRNAME / "b-set").mkdir(parents=True)
    (tmp_path / CURATION_DIRNAME / "a-set").mkdir()
    (tmp_path / CURATION_DIRNAME / ".hidden").mkdir()
    (tmp_path / CURATION_DIRNAME / "note.txt").write_text("not a set")
    assert supplement_names(tmp_path) == ("a-set", "b-set")
    # Folders with no readable cases add nothing and are named, so a build says which set did not read.
    assert read_supplements(tmp_path).missing == ("a-set", "b-set")
    assert read_supplements(tmp_path, names=["c-set"]).missing == ("c-set",)
