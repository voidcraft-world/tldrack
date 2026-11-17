"""The decision ledger: one seam on /decide, text only where it is kept, and its reactions."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest

from laya_bridge import ledger
from laya_bridge.ledger import Ledger, LedgerTagError, clean_tag, decisions_from
from laya_bridge.plugin import LayaPlugin
from laya_bridge.wire import clean_request

KNOWN = frozenset({"english", "multilingual"})
CONVERSATION = "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab"
NOUL = {"type": "noul", "instructions": "Is work open?"}
CHOICE = {"type": "choice", "instructions": "Which?", "criteria": {"todo": "a task", "note": "prose", "link": "a url"}}
SCORE = {"type": "score", "instructions": "How bad?", "criteria": ["fine", "meh", "bad"]}


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def cleaned(state="phase C is next", **questions):
    return clean_request({"state": state, "questions": questions or {"q": NOUL}}, KNOWN)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def book(tree):
    return Ledger(lambda: tree.base, clock=Clock(), load=lambda: 0.25)


# -- tags ------------------------------------------------------------------------


def test_clean_tag_keeps_the_documented_fields():
    assert clean_tag(None, rack_ok=False) is None
    tag = clean_tag(
        {"surface": "agent_open_work", "subject": "wt-feat.1", "question_version": 2, "threshold": 0.7, "done_threshold": 0.4},
        rack_ok=True,
    )
    assert tag == {
        "surface": "agent_open_work",
        "subject": "wt-feat.1",
        "question_version": 2,
        "threshold": 0.7,
        "done_threshold": 0.4,
    }


@pytest.mark.parametrize(
    "raw, field",
    [
        ([], "ledger"),
        ({"surface": "agent_open_work", "keep_text": True}, "ledger.keep_text"),
        ({"surface": "untagged"}, "ledger.surface"),
        ({"surface": "Mail"}, "ledger.surface"),
        ({"surface": "has-dash"}, "ledger.surface"),
        ({"surface": "x" * 65}, "ledger.surface"),
        ({}, "ledger.surface"),
        ({"surface": "paste", "subject": "has space"}, "ledger.subject"),
        ({"surface": "paste", "subject": "x" * 129}, "ledger.subject"),
        ({"surface": "paste", "question_version": True}, "ledger.question_version"),
        ({"surface": "paste", "question_version": -1}, "ledger.question_version"),
        ({"surface": "paste", "question_version": 1.5}, "ledger.question_version"),
        ({"surface": "paste", "question_version": 1e300}, "ledger.question_version"),  # whole, but no JS integer
        ({"surface": "paste", "question_version": 2**53}, "ledger.question_version"),
        ({"surface": "paste", "threshold": True}, "ledger.threshold"),
        ({"surface": "paste", "threshold": 1.2}, "ledger.threshold"),
        ({"surface": "paste", "threshold": "0.7"}, "ledger.threshold"),
        ({"surface": "paste", "done_threshold": -0.1}, "ledger.done_threshold"),
        ({"surface": "paste", "done_threshold": False}, "ledger.done_threshold"),
        ({"surface": "paste", "conversation": CONVERSATION}, "ledger.conversation"),  # a rack surface's only
        ({"surface": "news_gate", "conversation": CONVERSATION}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": "not-a-uuid"}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": CONVERSATION + "\n"}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": CONVERSATION[:-1]}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": "../" + CONVERSATION}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": 7}, "ledger.conversation"),
        ({"surface": "agent_open_work", "conversation": None}, "ledger.conversation"),
    ],
)
def test_clean_tag_refuses_naming_the_field(raw, field):
    with pytest.raises(LedgerTagError) as caught:
        clean_tag(raw, rack_ok=True)
    assert str(caught.value).startswith(field)


def test_an_unknown_field_name_is_not_echoed():
    with pytest.raises(LedgerTagError) as caught:
        clean_tag({"surface": "paste", "<script>": 1}, rack_ok=True)
    assert "<script>" not in str(caught.value)


@pytest.mark.parametrize("surface", ["agent_open_work", "coordinator"])
@pytest.mark.parametrize("conversation", [CONVERSATION, CONVERSATION.upper(), "019a5f3e-7b2c-7d41-9e8f-0123456789ab"])
def test_a_rack_surface_records_its_conversation(surface, conversation):
    """Claude's session id or Codex's thread id (a UUIDv7): how the REVIEW page bands a verdict."""
    tag = clean_tag({"surface": surface, "subject": "vc-8", "conversation": conversation}, rack_ok=True)
    assert tag == {"surface": surface, "subject": "vc-8", "conversation": conversation}


def test_a_refused_conversation_never_echoes_its_value():
    """The detail names the field so tldrack strips only it (open_work.py `OPTIONAL_TAG_FIELDS`)."""
    with pytest.raises(LedgerTagError) as caught:
        clean_tag({"surface": "agent_open_work", "conversation": "<script>alert(1)</script>"}, rack_ok=True)
    assert str(caught.value).startswith("ledger.conversation:") and "<script>" not in str(caught.value)


@pytest.mark.parametrize("surface", ["agent_open_work", "coordinator"])
def test_a_rack_surface_needs_a_native_caller(surface):
    with pytest.raises(LedgerTagError, match="ledger.surface"):
        clean_tag({"surface": surface}, rack_ok=False)
    assert clean_tag({"surface": surface}, rack_ok=True) == {"surface": surface}
    assert clean_tag({"surface": "paste"}, rack_ok=False) == {"surface": "paste"}


# -- answers ---------------------------------------------------------------------


def test_decisions_record_each_type_as_the_table_says():
    questions = {"open": NOUL, "kind": CHOICE, "bad": SCORE}
    payload = {
        "answers": {
            "open": {"noul": 0.81},
            "kind": {"choice": "todo", "confidence": 0.5, "probabilities": {"todo": 0.7, "note": 0.2, "link": 0.1}},
            "bad": {"score": 1.6, "confidence": 0.66, "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}},
        }
    }
    open_, kind, bad = decisions_from(questions, payload)
    assert open_ == {"q": "open", "qh": ledger.question_hash(NOUL), "type": "noul", "choice": "yes", "p": 0.81}
    assert kind["choice"] == "todo" and kind["p"] == 0.7 and kind["type"] == "choice"
    assert kind["alt"] == [{"choice": "note", "p": 0.2}, {"choice": "link", "p": 0.1}]
    assert bad["choice"] == "2" and bad["score"] == 1.6 and bad["p"] == 0.66
    assert bad["alt"] == [{"choice": "1", "p": 0.3}, {"choice": "0", "p": 0.1}]
    assert re.fullmatch(r"[0-9a-f]{12}", open_["qh"]) and open_["qh"] != kind["qh"]


def test_choice_without_a_distribution_uses_confidence_and_has_no_alt():
    [answer] = decisions_from({"kind": CHOICE}, {"answers": {"kind": {"choice": "note", "confidence": 0.4}}})
    assert answer["p"] == 0.4 and "alt" not in answer


@pytest.mark.parametrize(
    "question, raw",
    [
        (CHOICE, {"choice": "todo"}),  # no p at all
        (CHOICE, {"choice": "other", "confidence": 0.9}),  # an option it was not offered
        (SCORE, {"score": 1.0}),  # no confidence
        (SCORE, {"score": 9.0, "confidence": 0.9}),  # past its top level
        (NOUL, {"noul": 1.5}),
        (NOUL, {"noul": True}),
        (NOUL, None),  # missing answer
        (NOUL, "yes"),
    ],
)
def test_anything_unreadable_is_an_abstain_of_the_questions_own_type(question, raw):
    payload = {"answers": {} if raw is None else {"q": raw}}
    [answer] = decisions_from({"q": question}, payload)
    assert answer == {
        "q": "q",
        "qh": ledger.question_hash(question),
        "type": question["type"],
        "choice": "",
        "p": 0,
        "abstain": True,
    }


def test_a_payload_without_answers_abstains_everywhere():
    assert all(a["abstain"] for a in decisions_from({"a": NOUL, "b": CHOICE}, {}))


# -- writing ---------------------------------------------------------------------


def test_a_decision_row_has_every_field_and_safe_modes(tree, book):
    checkpoint = tree.register()
    decision_id = book.record_decision(
        {"surface": "agent_open_work", "subject": "wt-a", "question_version": 1, "threshold": 0.7, "done_threshold": 0.4},
        cleaned(),
        {"answers": {"q": {"noul": 0.9}}},
        "voidcraft-agent-open-work",
        "laya-local/voidcraft-agent-open-work@x",
        str(checkpoint),
        23.7,
    )
    assert re.fullmatch(r"20261027T120000-[0-9a-f]{12}", decision_id)
    path = tree.base / "ledger" / "agent_open_work.jsonl"
    [row] = read_rows(path)
    assert row == {
        "kind": "decision",
        "v": 1,
        "decision_id": decision_id,
        "at": "2026-10-27T12:00:00Z",
        "surface": "agent_open_work",
        "subject": "wt-a",
        "question_version": 1,
        "threshold": 0.7,
        "done_threshold": 0.4,
        "model": "voidcraft-agent-open-work",
        "model_id": "laya-local/voidcraft-agent-open-work@x",
        "checkpoint_path": "checkpoints/voidcraft-agent-open-work-20260102-0304",
        "state_hash": ledger.rows.state_digest("phase C is next"),
        "state": "phase C is next",
        "state_truncated": False,
        "decisions": [{"q": "q", "qh": ledger.question_hash(NOUL), "type": "noul", "choice": "yes", "p": 0.9}],
        "ms": 23,
        "load": 0.25,
    }
    assert mode(path) == 0o600 and mode(tree.base / "ledger") == 0o700


def test_a_row_says_how_busy_the_mac_was_and_whether_the_model_was_cold(tree):
    """`load` and `cold` are what laya-lab's latency read buckets `ms` by."""
    loads = iter([0.4, 1.23456, None, float("nan"), -1.0, 0.1, 0.1, 0.1])
    book = Ledger(lambda: tree.base, clock=Clock(), load=lambda: next(loads))
    answer = {"answers": {"q": {"noul": 0.9}}}

    def record(model="english", spawn=None):
        book.record_decision({"surface": "paste"}, cleaned(), answer, model, None, None, 1, spawn)
        return read_rows(tree.base / "ledger" / "paste.jsonl")[-1]

    first = record(spawn=1)
    assert (first["load"], first["cold"]) == (0.4, True)
    second = record(spawn=1)
    assert (second["load"], second["cold"]) == (1.235, False), "the same model, the same laya-serve start: warm"
    for _unreadable in range(3):
        assert "load" not in record(spawn=1), "no sample, NaN or a negative one: the field is left out"
    assert record("voidcraft-paste", spawn=1)["cold"] is True, "each model warms up on its own first answer"
    assert record(spawn=2)["cold"] is True, "laya-serve restarted: cold again"
    assert "cold" not in record(), "a caller that names no start says nothing about cold"


def test_a_first_answer_is_counted_while_the_ledger_is_off(tmp_path):
    base = tmp_path / "params" / "laya"
    book = Ledger(lambda: base, clock=Clock(), load=lambda: 0.0)
    assert book.record_decision(None, cleaned(), {}, "english", None, None, 1, 7) is None
    base.mkdir(parents=True)
    book.record_decision(None, cleaned(), {}, "english", None, None, 1, 7)
    [row] = read_rows(base / "ledger" / "untagged.jsonl")
    assert row["cold"] is False and row["load"] == 0.0


def test_the_default_load_is_this_machine_s_load_per_core():
    load = ledger.load_per_core()
    assert load is None or (isinstance(load, float) and load >= 0)


def test_a_row_copies_every_field_clean_tag_keeps(tree, book):
    """`COPIED_TAG_FIELDS` is the one list: a field the tag accepts can never be dropped on the way to disk."""
    raw = {
        "surface": "agent_open_work",
        "subject": "vc-8",
        "conversation": CONVERSATION,
        "question_version": 2,
        "threshold": 0.7,
        "done_threshold": 0.4,
    }
    assert set(raw) == set(ledger.TAG_FIELDS)
    book.record_decision(clean_tag(raw, rack_ok=True), cleaned(), {"answers": {"q": {"noul": 0.9}}}, "english", None, None, 1)
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert {field: row[field] for field in raw} == raw


def test_a_row_without_a_conversation_has_no_such_key(tree, book):
    book.record_decision(
        {"surface": "agent_open_work", "subject": "vc-8"}, cleaned(), {"answers": {"q": {"noul": 0.9}}}, "english", None, None, 1
    )
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert "conversation" not in row


@pytest.mark.parametrize("surface", ["paste", "coordinator", "news_gate", "notes_priority", None])
def test_every_other_surface_is_hash_only(tree, book, surface):
    tag = {"surface": surface} if surface else None
    book.record_decision(tag, cleaned("secret text"), {"answers": {"q": {"noul": 0.2}}}, "english", None, None, 1)
    [row] = read_rows(tree.base / "ledger" / f"{surface or 'untagged'}.jsonl")
    assert row["state"] is None and row["state_truncated"] is False
    assert row["state_hash"] == ledger.rows.state_digest("secret text") and "secret" not in json.dumps(row)


def test_kept_text_is_cut_to_its_tail(tree, book):
    text = "x" * 9_000 + "THE END"
    book.record_decision({"surface": "agent_open_work"}, cleaned(text), {}, "english", None, None, 1)
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert row["state_truncated"] is True and len(row["state"]) == 8_000 and row["state"].endswith("THE END")
    assert row["state_hash"] == ledger.rows.state_digest(text)


def test_kept_text_is_cut_in_utf16_units_as_zod_counts_them(tree, book):
    text = "x" + "😀" * 4_500  # 4,501 code points, 9,001 UTF-16 units
    book.record_decision({"surface": "agent_open_work"}, cleaned(text), {}, "english", None, None, 1)
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert row["state_truncated"] is True and row["state"] == "😀" * 4_000
    assert ledger.files.utf16_len(row["state"]) == ledger.STATE_MAX


def test_an_object_state_is_kept_as_its_canonical_json(tree, book):
    state = {"hand_off": "Café", "n": 1}
    book.record_decision(
        {"surface": "agent_open_work"},
        clean_request({"state": state, "questions": {"q": NOUL}}, KNOWN),
        {},
        "english",
        None,
        None,
        1,
    )
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert row["state"] == '{"hand_off": "Café", "n": 1}'
    assert ledger.rows.state_digest(row["state"]) == row["state_hash"]


def test_no_laya_folder_means_the_ledger_is_off(tmp_path):
    book = Ledger(lambda: tmp_path / "params" / "laya", clock=Clock())
    assert book.record_decision(None, cleaned(), {}, "english", None, None, 1) is None
    assert book.react("20261027T120000-0123456789ab", "shown") == ("off", False)
    assert not (tmp_path / "params").exists()
    assert Ledger(lambda: None).record_decision(None, cleaned(), {}, "english", None, None, 1) is None


def test_a_linked_laya_or_ledger_folder_is_refused(tree, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tree.base / "ledger").symlink_to(elsewhere)
    logged = []
    book = Ledger(lambda: tree.base, clock=Clock(), log=logged.append)
    assert book.record_decision(None, cleaned(), {}, "english", None, None, 1) is None
    assert list(elsewhere.iterdir()) == [] and len(logged) == 1
    linked_base = tmp_path / "linked-laya"
    linked_base.symlink_to(tree.base)
    assert Ledger(lambda: linked_base).record_decision(None, cleaned(), {}, "english", None, None, 1) is None


def test_a_read_only_ledger_still_answers_with_no_id(tree, book):
    (tree.base / "ledger").mkdir(mode=0o500)
    try:
        assert book.record_decision(None, cleaned(), {}, "english", None, None, 1) is None
    finally:
        os.chmod(tree.base / "ledger", 0o700)


def test_an_unwritable_laya_folder_turns_the_ledger_off_without_raising(tree, fake_runtime):
    os.chmod(tree.base, 0o500)  # laya/ exists, ledger/ cannot be created
    try:
        logged = []
        book = Ledger(lambda: tree.base, clock=Clock(), log=logged.append)
        assert book.record_decision(None, cleaned(), {}, "english", None, None, 1) is None
        assert book.react("20261027T120000-0123456789ab", "shown") == ("off", False)
        assert len(logged) == 1 and "cannot open ledger/" in logged[0]
        status, body = plugin_on(tree, fake_runtime).handle_post(
            "/react", {}, {"decision_id": "20261027T120000-0123456789ab", "action": "shown"}
        )
        assert status == 503 and body["error"] == "no_ledger", "never a 500 from the host"
    finally:
        os.chmod(tree.base, 0o700)
    assert not (tree.base / "ledger").exists()


def run_bounded(call, seconds=5.0):
    """`call()` on a daemon thread: (finished, its result). A hang fails the test, never the run."""
    out: dict = {}
    thread = threading.Thread(target=lambda: out.setdefault("value", call()), daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), out.get("value")


def test_a_fifo_at_a_ledger_file_never_hangs_a_decide(tree, book):
    (tree.base / "ledger").mkdir(mode=0o700)
    os.mkfifo(tree.base / "ledger" / "paste.jsonl")
    finished, decision_id = run_bounded(
        lambda: book.record_decision({"surface": "paste"}, cleaned(), {}, "english", None, None, 1)
    )
    assert finished, "a FIFO with no reader must fail at once, not block under the ledger lock"
    assert decision_id is None
    finished, other = run_bounded(lambda: book.record_decision({"surface": "news_gate"}, cleaned(), {}, "english", None, None, 1))
    assert finished and other is not None, "the lock was released: other surfaces still record"


def test_a_nan_state_is_not_recorded(tree, book):
    state = {"x": float("nan")}
    assert book.record_decision(None, {"state": state, "questions": {"q": NOUL}}, {}, "english", None, None, 1) is None


def test_a_checkpoint_outside_laya_is_recorded_as_none(tree, book, tmp_path):
    outside = tmp_path / "ck"
    outside.mkdir()
    book.record_decision(None, cleaned(), {}, "voidcraft-x", None, str(outside), 1)
    [row] = read_rows(tree.base / "ledger" / "untagged.jsonl")
    assert row["checkpoint_path"] is None


def test_rotation_keeps_the_newest_segments(tree):
    clock = Clock()
    book = Ledger(lambda: tree.base, clock=clock, rotate_bytes=400, keep_segments=2)
    for minute in range(12):
        clock.now += timedelta(minutes=1)
        book.record_decision({"surface": "paste"}, cleaned(f"s{minute}"), {}, "english", None, None, 1)
    folder = tree.base / "ledger"
    segments = ledger.segments(folder, "paste")
    assert len(segments) == 2 and all(mode(p) == 0o600 for p in segments)
    rows_now = list(ledger.iter_rows(tree.base, "paste"))
    assert rows_now[-1]["state_hash"] == ledger.rows.state_digest("s11")
    ats = [row["at"] for row in rows_now]
    assert ats == sorted(ats), "oldest first, across segments"


def test_the_kept_segments_fit_the_read_window():
    # Retention is the read window: owned by ledger.py's module docstring — see the note there.
    assert ledger.KEEP_SEGMENTS >= 1
    assert ledger.KEEP_SEGMENTS * ledger.ROTATE_BYTES <= ledger.READ_MAX_BYTES


def test_a_pruned_segment_leaves_the_parse_cache(tree):
    clock = Clock()
    book = Ledger(lambda: tree.base, clock=clock, rotate_bytes=400, keep_segments=2)
    seen: set[str] = set()
    for minute in range(12):
        clock.now += timedelta(minutes=1)
        book.record_decision({"surface": "paste"}, cleaned(f"s{minute}"), {}, "english", None, None, 1)
        list(ledger.iter_rows(tree.base, "paste"))  # a reader caches every segment it sees
        seen |= {str(path) for path in ledger.segments(tree.base / "ledger", "paste")}
    live = {str(path) for path in ledger.segments(tree.base / "ledger", "paste")}
    cached = {key[0] for key in ledger._SEGMENTS._entries}
    assert len(seen) > len(live) and cached & seen == live, "only the kept segments stay parsed in memory"


def test_iter_rows_honours_since_and_the_byte_budget(tree):
    clock = Clock()
    book = Ledger(lambda: tree.base, clock=clock, rotate_bytes=300)
    for day in range(6):
        clock.now = datetime(2026, 10, 20 + day, tzinfo=timezone.utc)
        book.record_decision({"surface": "paste"}, cleaned(f"d{day}"), {}, "english", None, None, 1)
    recent = list(ledger.iter_rows(tree.base, "paste", since="2026-10-24T00:00:00Z"))
    assert [row["at"][:10] for row in recent] == ["2026-10-24", "2026-10-25"]
    small = list(ledger.iter_rows(tree.base, "paste", max_bytes=800))
    assert 0 < len(small) < 6 and small[-1]["at"].startswith("2026-10-25")


# -- reactions -------------------------------------------------------------------


def test_react_records_once_and_knows_unknown_ids(tree, book):
    decision_id = book.record_decision({"surface": "paste"}, cleaned(), {}, "english", None, None, 1)
    assert book.react(decision_id, "shown") == ("ok", False)
    assert book.react(decision_id, "shown") == ("ok", True)
    assert book.react(decision_id, "dismissed") == ("ok", False)
    assert book.react("20261027T120000-0123456789ab", "shown") == ("unknown", False)
    reactions = [row for row in read_rows(tree.base / "ledger" / "paste.jsonl") if row["kind"] == "reaction"]
    assert [(r["action"], r["surface"]) for r in reactions] == [("shown", "paste"), ("dismissed", "paste")]


def test_a_reaction_after_a_restart_finds_its_decision_on_disk(tree, book):
    decision_id = book.record_decision({"surface": "paste"}, cleaned(), {}, "english", None, None, 1)
    book.react(decision_id, "shown")
    restarted = Ledger(lambda: tree.base, clock=Clock())
    assert restarted.react(decision_id, "shown") == ("ok", True)
    assert restarted.react(decision_id, "accepted") == ("ok", False)


def test_a_reaction_finds_a_decision_long_gone_from_the_index(tree):
    book = Ledger(lambda: tree.base, clock=Clock(), index_max=8)
    assert book.react("20261027T120000-0123456789ab", "shown") == ("unknown", False)  # a stray id first
    first = book.record_decision({"surface": "agent_open_work"}, cleaned(), {}, "english", None, None, 1)
    assert book.react(first, "shown") == ("ok", False)
    for i in range(20):  # the relay records one decision per post: the open-work one leaves the index
        book.record_decision({"surface": "news_gate"}, cleaned(f"post {i}"), {}, "english", None, None, 1)
    assert first not in book._index
    assert book.react(first, "dismissed") == ("ok", False)
    assert book.react(first, "shown") == ("ok", True), "the actions came back from disk too"
    reactions = [
        (row["decision_id"], row["action"])
        for row in read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
        if row["kind"] == "reaction"
    ]
    assert reactions == [(first, "shown"), (first, "dismissed")]


def test_a_lookup_skips_segments_rotated_before_the_decision(tree, monkeypatch):
    clock = Clock()
    book = Ledger(lambda: tree.base, clock=clock, rotate_bytes=300, index_max=1, keep_segments=8)
    for day in range(4):
        clock.now = datetime(2026, 10, 20 + day, tzinfo=timezone.utc)
        book.record_decision({"surface": "paste"}, cleaned(f"d{day}"), {}, "english", None, None, 1)
    clock.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    target = book.record_decision({"surface": "paste"}, cleaned("late"), {}, "english", None, None, 1)
    book.record_decision({"surface": "news_gate"}, cleaned("evicts it"), {}, "english", None, None, 1)
    read = []
    real = ledger.files.StampedCache.get
    monkeypatch.setattr(
        ledger.files.StampedCache, "get", lambda self, path, *rest: read.append(path.name) or real(self, path, *rest)
    )
    assert book.react(target, "shown") == ("ok", False)
    older = {p.name for p in ledger.segments(tree.base / "ledger", "paste") if ledger._segment_at(p) < "2026-10-25T00:00:00Z"}
    assert len(older) >= 3 and not older & set(read), "a segment rotated before the decision's second is never read"


def test_summary_counts_decisions_in_the_window_with_their_reactions(tree):
    clock = Clock(datetime(2026, 10, 10, tzinfo=timezone.utc))
    book = Ledger(lambda: tree.base, clock=clock)
    old = book.record_decision({"surface": "paste"}, cleaned("old"), {}, "english", None, None, 1)
    clock.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    fresh = [book.record_decision({"surface": "paste"}, cleaned(f"n{i}"), {}, "english", None, None, 1) for i in range(3)]
    for decision_id in fresh:
        book.react(decision_id, "shown")
    book.react(fresh[0], "dismissed")
    book.react(fresh[1], "accepted")
    book.react(old, "dismissed")
    out = ledger.summary(tree.base, 7, now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert out == {
        "paste": {
            "decisions": 3,
            "shown": 3,
            "dismissed": 1,
            "accepted": 1,
            "converted": 0,
            "expired": 0,
            "last_at": "2026-10-25T00:00:00Z",
        }
    }
    assert ledger.summary(None, 7) == {}


# -- through the plugin ----------------------------------------------------------


def plugin_on(tree, runtime):
    return LayaPlugin(context=None, runtime=runtime, params_dir=lambda: tree.base)


def test_a_bad_tag_is_a_400_naming_the_field(tree, fake_runtime, decide):
    plugin = plugin_on(tree, fake_runtime)
    status, body = plugin.handle_post("/decide", {}, decide(ledger={"surface": "paste", "question_version": True}))
    assert status == 400 and body["error"] == "bad_request" and "ledger.question_version" in body["detail"]
    assert fake_runtime.decided == [], "a refused tag never reaches Laya"


def test_a_conversation_off_the_rack_surfaces_is_a_400_and_never_reaches_laya(tree, fake_runtime, decide):
    plugin = plugin_on(tree, fake_runtime)
    status, body = plugin.handle_post("/decide", {}, decide(ledger={"surface": "paste", "conversation": CONVERSATION}))
    assert status == 400 and body["error"] == "bad_request" and body["detail"].startswith("ledger.conversation")
    assert fake_runtime.decided == []
    status, body = plugin.handle_post("/decide", {}, decide(ledger={"surface": "agent_open_work", "conversation": CONVERSATION}))
    assert status == 200
    [row] = read_rows(tree.base / "ledger" / "agent_open_work.jsonl")
    assert row["conversation"] == CONVERSATION and row["decision_id"] == body["decision_id"]


def test_the_tag_is_never_forwarded_and_the_row_names_the_served_model(tree, fake_runtime, decide):
    fake_runtime.payload_model = "multilingual"
    plugin = plugin_on(tree, fake_runtime)
    status, body = plugin.handle_post("/decide", {}, decide(ledger={"surface": "paste"}))
    assert status == 200 and "ledger" not in fake_runtime.decided[0]
    [row] = read_rows(tree.base / "ledger" / "paste.jsonl")
    assert row["model"] == "multilingual" and body["decision_id"] == row["decision_id"]


def test_react_routes(tree, fake_runtime, decide):
    plugin = plugin_on(tree, fake_runtime)
    decision_id = plugin.handle_post("/decide", {}, decide(ledger={"surface": "paste"}))[1]["decision_id"]
    assert plugin.handle_post("/react", {}, {"decision_id": decision_id, "action": "shown"}) == (
        200,
        {"ok": True, "decision_id": decision_id, "action": "shown", "duplicate": False},
    )
    assert plugin.handle_post("/react", {}, {"decision_id": decision_id, "action": "shown"})[1]["duplicate"] is True
    status, body = plugin.handle_post("/react", {}, {"decision_id": "20261027T120000-0123456789ab", "action": "shown"})
    assert status == 409 and body["error"] == "unknown_decision"
    for bad in (
        [],
        {"decision_id": decision_id},
        {"decision_id": decision_id, "action": "liked"},
        {"decision_id": "nope", "action": "shown"},
        {"decision_id": decision_id, "action": "shown", "x": 1},
        # Arabic-Indic digits: Python's `\d` takes them, LAYA_DECISION_ID (JavaScript) does not.
        {"decision_id": "٢٠٢٦١٠٢٧T١٢٠٠٠٠-0123456789ab", "action": "shown"},
    ):
        status, body = plugin.handle_post("/react", {}, bad)
        assert status == 400 and body["error"] == "bad_request", bad


def test_react_without_a_ledger_is_503(fake_runtime):
    status, body = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: None).handle_post(
        "/react", {}, {"decision_id": "20261027T120000-0123456789ab", "action": "shown"}
    )
    assert status == 503 and body["error"] == "no_ledger"


# -- judged: has the rack's turn end been decided already? ------------------------


def test_judged_finds_the_turn_by_its_conversation_and_state(book):
    """tldrack asks before judging a turn end its bridge missed (open_work.py `ledger_judged`)."""
    from laya_bridge import rows

    state = "Phase C is not started."
    book.record_decision(
        {"surface": "agent_open_work", "subject": "vc-27", "conversation": CONVERSATION},
        cleaned(state),
        {"answers": {"q": {"noul": 0.9}}},
        "english",
        None,
        None,
        1,
    )
    other = "9f9f9f9f-0000-4000-8000-000000000000"
    assert book.judged("agent_open_work", CONVERSATION, rows.state_digest(state)) is True
    assert book.judged("agent_open_work", CONVERSATION, rows.state_digest("Phase C is done.")) is False
    assert book.judged("agent_open_work", other, rows.state_digest(state)) is False
    assert book.judged("coordinator", CONVERSATION, rows.state_digest(state)) is False


@pytest.mark.parametrize(
    ("surface", "conversation", "state_hash"),
    [
        ("paste", CONVERSATION, "a" * 64),
        ("agent_open_work", "$(not a uuid)", "a" * 64),
        ("agent_open_work", CONVERSATION, "not-hex"),
        ("agent_open_work", None, "a" * 64),
    ],
)
def test_judged_cannot_say_for_a_malformed_question(book, surface, conversation, state_hash):
    assert book.judged(surface, conversation, state_hash) is None


def test_judged_cannot_say_while_the_ledger_is_off():
    assert Ledger(lambda: None).judged("agent_open_work", CONVERSATION, "a" * 64) is None


def test_judged_is_false_on_a_ledger_that_has_recorded_nothing_yet(tmp_path):
    """A params tree with no `ledger/` yet: the ledger is ON and holds no row, so nothing was judged."""
    base = tmp_path / "params" / "laya"
    base.mkdir(parents=True)
    assert Ledger(lambda: base).judged("agent_open_work", CONVERSATION, "a" * 64) is False


# -- surfaces: open by shape, one file each, capped ------------------------------------


def test_a_ledger_holds_the_surfaces_it_has_files_for_and_the_racks_own_always(tree):
    assert ledger.surfaces(None) == ledger.surfaces(tree.base) == ("agent_open_work", "coordinator", "untagged")
    folder = tree.path("ledger/x").parent
    for name in (
        "notes_triage.jsonl",
        "paste-20260102T030405.jsonl",
        "paste-20260102T030405-2.jsonl",
        "Bad.jsonl",
        "notes.txt",
        "has-dash.jsonl",
    ):
        (folder / name).write_text("")
    assert ledger.surfaces(tree.base) == ("agent_open_work", "coordinator", "notes_triage", "paste", "untagged")


def test_a_host_surface_is_recorded_and_found_again_and_the_cap_refuses_one_more(tree, fake_runtime):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    body = {"state": "a feed item", "questions": {"q": {"type": "noul", "instructions": "Is it worth reading?"}}}
    status, payload = plugin.handle_post("/decide", {}, {**body, "ledger": {"surface": "news_gate"}})
    assert status == 200 and (tree.base / "ledger" / "news_gate.jsonl").is_file()
    assert plugin.handle_post("/react", {}, {"decision_id": payload["decision_id"], "action": "shown"})[0] == 200
    assert ledger.summary(tree.base, 7)["news_gate"]["decisions"] == 1
    for i in range(ledger.MAX_SURFACES):
        (tree.base / "ledger" / f"filler_{i}.jsonl").write_text("")
    status, refused = plugin.handle_post("/decide", {}, {**body, "ledger": {"surface": "one_more"}})
    assert status == 400 and refused["detail"].startswith("ledger.surface:") and "one_more" not in refused["detail"]
    assert not (tree.base / "ledger" / "one_more.jsonl").exists() and fake_runtime.decided[-1]["state"] == "a feed item"
    assert plugin.handle_post("/decide", {}, {**body, "ledger": {"surface": "news_gate"}})[0] == 200, (
        "a held surface still records"
    )
