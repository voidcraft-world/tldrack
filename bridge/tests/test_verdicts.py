"""The verdict kept across a bridge restart (verdicts.py). The whole path, Stop → file → restart →
row, is in test_open_work.py; this covers the file and the restore rule on their own."""

from __future__ import annotations

import json

from tldrack.open_work import state_sha
from tldrack.verdicts import (
    SessionVerdictStore,
    clean_verdict,
    parse_verdict_file,
    restorable,
)

CONVERSATION = "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab"
STATE = "Phase C is not started."


def verdict(**over):
    return {
        "p": 0.9,
        "v": 2,
        "model": "voidcraft-agent-open-work",
        "at": 0.7,
        "done_at": 0.4,
        "decision_id": "20261104T090002-000000000003",
        "model_id": "laya-local/x@y",
        "checkpoint": "voidcraft-agent-open-work-20240101-1200",
        "state_sha": state_sha(STATE),
        "asked": False,
        "read": False,
        **over,
    }


def kept(**over):
    return {"verdict": verdict(**over), "transcript_path": "/x/s.jsonl", "agent_session_id": CONVERSATION}


def test_a_kept_verdict_round_trips_through_the_file(tmp_path):
    path = tmp_path / "verdicts.json"
    store = SessionVerdictStore(path)
    assert store.set("vc-1", verdict(), "/x/s.jsonl", CONVERSATION)
    assert SessionVerdictStore(path).get("vc-1") == kept()
    assert STATE not in path.read_text()  # a hash, never the closing text


def test_nothing_is_kept_without_a_conversation_or_with_a_bad_verdict(tmp_path):
    store = SessionVerdictStore(tmp_path / "verdicts.json")
    assert not store.set("vc-1", verdict(), "/x/s.jsonl", None)
    assert not store.set("vc-1", verdict(p=1.5), "/x/s.jsonl", CONVERSATION)
    assert not store.set("vc-1", verdict(state_sha="nope"), "/x/s.jsonl", CONVERSATION)
    assert store.get("vc-1") is None


def test_one_bad_entry_is_dropped_and_the_rest_load():
    text = json.dumps(
        {
            "version": 1,
            "sessions": {
                "vc-1": kept(),
                "vc-2": kept(checkpoint="../../etc"),
                "bad name!": kept(),
            },
        }
    )
    assert list(parse_verdict_file(text)) == ["vc-1"]


def test_the_answer_to_read_stamps_are_kept_and_an_old_verdict_reads_as_asking_nothing():
    """open_work.py § An answer to read: a read answer must not come back unread after a restart."""
    old = {key: value for key, value in verdict().items() if key not in ("asked", "read")}
    assert (clean_verdict(old)["asked"], clean_verdict(old)["read"]) == (False, False)
    assert clean_verdict(verdict(asked=True))["asked"] is True
    assert clean_verdict(verdict(asked="yes")) is None and clean_verdict(verdict(read=1)) is None


def test_mark_read_stamps_the_kept_copy_once_and_persists_it(tmp_path):
    path = tmp_path / "verdicts.json"
    store = SessionVerdictStore(path)
    assert not store.mark_read("vc-1"), "nothing kept"
    store.set("vc-1", verdict(asked=True), "/x/s.jsonl", CONVERSATION)
    assert store.mark_read("vc-1") and not store.mark_read("vc-1")
    assert SessionVerdictStore(path).get("vc-1")["verdict"]["read"] is True


def test_an_old_verdict_from_before_the_done_band_still_parses():
    assert clean_verdict(verdict(done_at=None))["done_at"] is None


def test_restore_needs_the_same_conversation_and_the_same_text():
    assert restorable(kept(), CONVERSATION, STATE) == verdict()
    assert restorable(kept(), "9f9f9f9f-0000-4000-8000-000000000000", STATE) is None
    assert restorable(kept(), None, STATE) is None
    assert restorable(kept(), CONVERSATION, STATE + " Now it is.") is None
    assert restorable(kept(), CONVERSATION, None) is None
    assert restorable(None, CONVERSATION, STATE) is None


def test_rename_and_forget_follow_the_row(tmp_path):
    store = SessionVerdictStore(tmp_path / "verdicts.json")
    store.set("vc-1", verdict(), "/x/s.jsonl", CONVERSATION)
    assert store.rename("vc-1", "docs-voice")
    assert store.get("vc-1") is None and store.get("docs-voice") is not None
    assert store.forget("docs-voice") and store.get("docs-voice") is None
