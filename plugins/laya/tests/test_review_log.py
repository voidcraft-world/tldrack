"""The operator's answer log: append-only, 0600, fsync'd, one fold."""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from laya_bridge import files, review_log
from laya_bridge.envelope import Refusal
from laya_bridge.review_log import answer_row, fold, retest_row, undo_row

SURFACE = "agent_open_work"
KEY_A, KEY_B = "0123456789abcdef", "fedcba9876543210"


def answer(key=KEY_A, verdict="agree", against_open=True, answer_id=None, stratum="sample"):
    return answer_row(
        answer_id=answer_id or review_log.new_answer_id(),
        at="2026-10-27T01:02:03Z",
        surface=SURFACE,
        key=key,
        stratum=stratum,
        answer=verdict,
        against={"by": "claude-sonnet-5-5", "open": against_open},
        laya=None,
        decision_id=None,
        held_out=True,
    )


@pytest.fixture
def data(tree):
    tree.path("data/.keep")
    return tree.base


def write(base, row):
    _code, current = review_log.read_fold(base, SURFACE)
    with review_log.writing(base, SURFACE):
        review_log.append(base, SURFACE, row, current)


def test_open_follows_the_answer_and_is_absent_on_skip():
    assert answer(verdict="agree", against_open=True)["open"] is True
    assert answer(verdict="flip", against_open=True)["open"] is False
    assert "open" not in answer(verdict="skip")
    assert answer()["at"].endswith("Z") and answer()["answer_id"].startswith("a_")


def test_append_creates_a_0600_log_and_fsyncs_it(data, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    row = answer()
    write(data, row)
    path = review_log.operator_path(data, SURFACE)
    assert path.name == "agent-open-work-operator.jsonl"
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600 and len(synced) >= 2, "the file and, on creation, its folder"
    assert json.loads(path.read_text()) == row
    lock = data / "data" / ".agent-open-work-operator.lock"
    assert stat.S_IMODE(os.lstat(lock).st_mode) == 0o600


def test_fold_precedence_undo_and_skip():
    first, second = answer(KEY_A, "agree"), answer(KEY_B, "skip")
    redo = answer(KEY_A, "flip")
    rows = [
        first,
        second,
        undo_row(answer_id=first["answer_id"], at="2026-10-27T01:02:04Z", surface=SURFACE),
        redo,
        undo_row(answer_id="a_0000000000000000", at="2026-10-27T01:02:05Z", surface=SURFACE),
    ]
    out = fold(rows)
    assert out.labels == {KEY_A: redo} and out.skipped == {KEY_B}
    assert list(out.live) == [second["answer_id"], redo["answer_id"]] and out.undone == {first["answer_id"]}
    assert out.bad_lines == 0 and out.rows == 5
    assert out.live_answer(KEY_A) == redo and out.answered(KEY_B)


def retest(of, key=KEY_A, said=False, needs=None, answer_id=None):
    return retest_row(
        answer_id=answer_id or review_log.new_answer_id(),
        at="2026-11-05T01:02:03Z",
        surface=SURFACE,
        key=key,
        of=of["answer_id"],
        said=said,
        needs=needs,
    )


def test_a_retest_never_moves_a_label_a_count_or_a_live_answer():
    first, other = answer(KEY_A, "agree"), answer(KEY_B, "flip")
    again = retest(first, said=False)  # the operator said DONE this time, against an OPEN label
    before, out = fold([first, other]), fold([first, other, again])
    assert out.labels == before.labels == {KEY_A: first, KEY_B: other}, "the label is the first look's"
    assert out.live == before.live and out.skipped == before.skipped and out.undone == set()
    assert out.live_answer(KEY_A) == first and out.answered(KEY_A)
    assert out.retests == {again["answer_id"]: again} and out.retest_of(first["answer_id"]) == again
    assert out.retest_of(other["answer_id"]) is None and (out.rows, out.bad_lines) == (3, 0)


def test_a_retest_row_holds_only_what_the_second_look_said():
    first = answer()
    assert retest(first, said=True, needs="you", answer_id="a_00000000000000aa") == {
        "v": 1,
        "op": "retest",
        "answer_id": "a_00000000000000aa",
        "at": "2026-11-05T01:02:03Z",
        "by": "operator",
        "surface": SURFACE,
        "key": KEY_A,
        "of": first["answer_id"],
        "open": True,
        "needs": "you",
    }
    cannot_say = retest(first, said=None)
    assert "open" not in cannot_say and "needs" not in cannot_say and "against" not in cannot_say
    assert review_log.retest_ref(cannot_say) == {
        "answer_id": cannot_say["answer_id"],
        "key": KEY_A,
        "of": first["answer_id"],
        "open": None,
        "needs": None,
    }


def test_a_retest_is_undone_by_its_own_id_and_an_id_is_never_shared():
    first = answer()
    again = retest(first)
    undo = undo_row(answer_id=again["answer_id"], at="2026-11-05T01:02:04Z", surface=SURFACE)
    out = fold([first, again, undo, undo])
    assert out.retests == {} and out.undone == {again["answer_id"]} and out.labels == {KEY_A: first}
    assert out.bad_lines == 0, "a second undo of the same id is ignored, as for an answer"
    redo = retest(first, said=True)
    assert fold([first, again, undo, redo]).retest_of(first["answer_id"]) == redo
    # An id is written once, whichever op wrote it: a retest reusing an answer's id (or its own) is a bad line.
    clash = fold([first, retest(first, answer_id=first["answer_id"]), again, dict(again)])
    assert clash.retests == {again["answer_id"]: again} and clash.bad_lines == 2 and clash.labels == {KEY_A: first}
    # ...and an answer reusing a retest's id never becomes a label.
    assert fold([first, again, answer(KEY_B, answer_id=again["answer_id"])]).labels == {KEY_A: first}


@pytest.mark.parametrize(
    "change",
    [
        {"of": "nope"},
        {"of": None},
        {"key": "not-a-key"},
        {"open": "yes"},
        {"open": None},
        {"open": False, "needs": "you"},
        {"needs": "you"},
        {"open": True, "needs": "later"},
    ],
)
def test_a_malformed_retest_is_a_bad_line(change):
    first = answer()
    row = {**retest(first, said=None), **change}
    row = {k: v for k, v in row.items() if not (k == "of" and v is None)}
    out = fold([first, row])
    assert out.retests == {} and out.bad_lines == 1 and out.labels == {KEY_A: first}


def test_fold_counts_malformed_rows_and_never_reuses_an_id():
    row = answer()
    out = fold([row, dict(row), {"op": "answer"}, {**row, "answer_id": "a_x"}, {**answer(), "open": None}], bad_lines=2)
    assert list(out.live) == [row["answer_id"]] and out.bad_lines == 6


def test_a_torn_trailing_line_waits_and_bad_lines_are_counted(data):
    path = review_log.operator_path(data, SURFACE)
    good = answer()
    path.write_text(json.dumps(good) + "\n{not json\n" + json.dumps(answer(KEY_B))[:20])
    found, bad = review_log.read_operator_log(path)
    assert found == [good] and bad == 1


def test_parallel_appends_are_whole_lines(data):
    threads = [threading.Thread(target=lambda: [write(data, answer(f"{i:016x}")) for i in range(50)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = review_log.operator_path(data, SURFACE).read_text().splitlines()
    assert len(lines) == 400 and all(json.loads(line)["v"] == 1 for line in lines)


def test_a_linked_log_is_refused(data, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("")
    review_log.operator_path(data, SURFACE).symlink_to(outside)
    with pytest.raises(Refusal) as caught:
        write(data, answer())
    assert caught.value.code == "source_refused" and outside.read_text() == ""


def test_a_linked_lock_is_refused(data, tmp_path):
    (data / "data" / ".agent-open-work-operator.lock").symlink_to(tmp_path / "elsewhere.lock")
    with pytest.raises(Refusal) as caught, review_log.writing(data, SURFACE):
        pass
    assert caught.value.code == "source_refused"


def test_the_log_refuses_past_its_caps(data, monkeypatch):
    write(data, answer())
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 1)
    with pytest.raises(Refusal) as caught:
        write(data, answer(KEY_B))
    assert (caught.value.status, caught.value.code) == (413, "review_full")
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 20_000)
    monkeypatch.setattr(review_log, "LOG_CAP_BYTES", 10)
    with pytest.raises(Refusal, match="full"):
        write(data, answer(KEY_B))


def test_append_many_checks_every_row_and_both_caps_before_the_first_byte(data, monkeypatch):
    path = review_log.operator_path(data, SURFACE)
    big = {**answer(KEY_B), "pad": "x" * review_log.ROW_MAX_BYTES}
    with review_log.writing(data, SURFACE), pytest.raises(Refusal) as caught:
        review_log.append_many(data, SURFACE, [answer(KEY_A), big], review_log.read_fold(data, SURFACE)[1])
    assert (caught.value.status, caught.value.code) == (500, "review_unwritable") and not path.exists()
    pair = [answer(KEY_A), answer(KEY_B)]
    with review_log.writing(data, SURFACE):
        review_log.append_many(data, SURFACE, pair, review_log.read_fold(data, SURFACE)[1])
    assert review_log.read_operator_log(path)[0] == pair
    size = os.lstat(path).st_size
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 3)
    with review_log.writing(data, SURFACE), pytest.raises(Refusal, match="full"):
        review_log.append_many(data, SURFACE, pair, review_log.read_fold(data, SURFACE)[1])
    monkeypatch.setattr(review_log, "LOG_MAX_ROWS", 20_000)
    monkeypatch.setattr(review_log, "LOG_CAP_BYTES", size + 1)  # under the cap now (a row-by-row check let row 1 in)
    with review_log.writing(data, SURFACE), pytest.raises(Refusal, match="full"):
        review_log.append_many(data, SURFACE, pair, review_log.read_fold(data, SURFACE)[1])
    assert os.lstat(path).st_size == size, "a refused press writes nothing"


def test_no_data_folder_is_params_unavailable(tree):
    with pytest.raises(Refusal) as caught, review_log.writing(tree.base, SURFACE):
        pass
    assert (caught.value.status, caught.value.code) == (503, "params_unavailable")
    assert not (tree.base / "data").exists(), "the plugin never creates the params tree"


def test_a_wider_log_is_tightened_on_append(data):
    path = review_log.operator_path(data, SURFACE)
    path.write_text("")
    os.chmod(path, 0o644)
    write(data, answer())
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


def test_reads_parse_only_new_lines(data):
    write(data, answer())
    path = review_log.operator_path(data, SURFACE)
    review_log.read_operator_log(path)
    before = files.shared_tail(path, review_log.READ_CAP_BYTES).parsed_lines
    write(data, answer(KEY_B))
    found, _bad = review_log.read_operator_log(path)
    assert len(found) == 2 and files.shared_tail(path, review_log.READ_CAP_BYTES).parsed_lines == before + 1


def test_an_unwritable_data_folder_is_review_unwritable_not_refused(data):
    write(data, answer())  # the lock file and the log exist now
    path = review_log.operator_path(data, SURFACE)
    os.chmod(path, 0o400)
    try:
        with pytest.raises(Refusal) as caught:
            write(data, answer(KEY_B))
        assert (caught.value.status, caught.value.code) == (503, "review_unwritable")
    finally:
        os.chmod(path, 0o600)


def run_bounded(call, seconds=5.0):
    """`call()` on a daemon thread: (finished, its result or exception). A hang fails the test, never the run."""
    out: dict = {}

    def target():
        try:
            out["value"] = call()
        except BaseException as exc:  # noqa: BLE001 — handed back to the test
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), out


def test_a_fifo_at_the_log_is_refused_without_hanging(data):
    os.mkfifo(review_log.operator_path(data, SURFACE))
    finished, out = run_bounded(lambda: write(data, answer()))
    assert finished, "a FIFO with no reader must fail at once, not block the request thread"
    assert isinstance(out.get("error"), Refusal) and (out["error"].status, out["error"].code) == (503, "review_unwritable")


def test_a_fifo_at_the_lock_is_source_refused(data):
    os.mkfifo(data / "data" / ".agent-open-work-operator.lock")
    finished, out = run_bounded(lambda: write(data, answer()))
    assert finished and isinstance(out.get("error"), Refusal)
    assert (out["error"].status, out["error"].code) == (503, "source_refused")
