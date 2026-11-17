"""Capped, link-refusing reads; the two parse caches; and operator file modes."""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from laya_bridge import files


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# -- reads ----------------------------------------------------------------------


def test_read_capped_reads_whole_and_refuses_past_the_cap(tmp_path):
    path = tmp_path / "f.json"
    path.write_bytes(b"12345")
    assert files.read_capped(path, 5) == b"12345"
    with pytest.raises(files.SourceError) as caught:
        files.read_capped(path, 4)
    assert caught.value.code == files.TOO_LARGE
    assert files.read_capped(tmp_path / "absent", 5) is None


def test_read_capped_refuses_a_link_and_a_fifo(tmp_path):
    real = tmp_path / "real"
    real.write_text("x")
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(files.SourceError) as caught:
        files.read_capped(tmp_path / "link", 10)
    assert caught.value.code == files.REFUSED
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(files.SourceError):
        files.read_capped(tmp_path / "fifo", 10)  # never blocks: O_NONBLOCK, then fstat refuses it


def test_parse_jsonl_skips_comments_torn_tails_and_counts_bad_lines():
    data = b'# a comment\n{"a": 1}\n[1]\n{bad\n{"nan": NaN}\n{"b": 2}\n{"torn": '
    found, bad = files.parse_jsonl(data)
    assert found == [{"a": 1}, {"b": 2}] and bad == 3


def test_loads_refuses_nan_and_overflowing_floats():
    for text in ("NaN", "Infinity", "-Infinity", "1e999"):
        with pytest.raises(ValueError):
            files.loads(text)
    assert files.loads(b'{"p": 0.5}') == {"p": 0.5}


def test_bool_is_never_a_number():
    assert not files.is_number(True) and not files.is_p(False) and not files.is_count(True)
    assert files.is_p(0) and files.is_p(1.0) and not files.is_p(1.01) and files.is_count(3) and not files.is_count(-1)


def test_a_count_is_one_javascript_reads_exactly():
    assert files.is_count(files.MAX_SAFE_INTEGER) and not files.is_count(files.MAX_SAFE_INTEGER + 1)
    assert not files.is_count(10**400) and not files.is_count(2.0)


# -- lengths as JavaScript counts them -------------------------------------------


def test_utf16_len_counts_what_javascript_length_counts():
    assert files.utf16_len("abc") == 3 and files.utf16_len("é") == 1
    assert files.utf16_len("😀") == 2 and files.utf16_len("a😀b") == 4
    assert files.utf16_len("\ud83d") == 1, "a lone surrogate (JSON can carry one) is one unit"


@pytest.mark.parametrize("text", ["plain text here", "😀" * 50, "a😀" * 30, "😀a" * 30, "x" + "😀" * 40])
@pytest.mark.parametrize("limit", [1, 2, 3, 10, 41])
def test_utf16_cuts_stay_within_the_limit_on_a_code_point_boundary(text, limit):
    tail, head, clipped = files.utf16_tail(text, limit), files.utf16_head(text, limit), files.clip(text, limit)
    for part in (tail, head, clipped):
        assert files.utf16_len(part) <= limit
        assert all(not 0xD800 <= ord(ch) <= 0xDFFF for ch in part), "never half an emoji"
    assert text.endswith(tail) and text.startswith(head)
    assert files.utf16_len(tail) >= limit - 1 or tail == text, "as long as the boundary allows"
    assert clipped == text if files.utf16_len(text) <= limit else clipped.endswith("…")
    assert files.utf16_tail(text, 0) == "" and files.utf16_head(text, 0) == ""


# -- caches ---------------------------------------------------------------------


def test_stamped_cache_parses_once_per_stamp(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a": 1}\n')
    parses = []

    def parse(data: bytes):
        parses.append(data)
        return files.parse_jsonl(data)[0]

    cache = files.StampedCache()
    assert cache.get(path, 1024, parse) == (files.OK, [{"a": 1}])
    assert cache.get(path, 1024, parse) == (files.OK, [{"a": 1}])
    assert len(parses) == 1
    path.write_text('{"a": 1}\n{"b": 2}\n')
    assert cache.get(path, 1024, parse)[1] == [{"a": 1}, {"b": 2}] and len(parses) == 2
    assert cache.get(tmp_path / "absent", 1024, parse) == (files.MISSING, None)
    assert cache.get(path, 3, parse) == (files.TOO_LARGE, None)
    (tmp_path / "link").symlink_to(path)
    assert cache.get(tmp_path / "link", 1024, parse) == (files.REFUSED, None)


def test_stamped_cache_drops_a_gone_file_and_forgets_on_request(tmp_path):
    cache = files.StampedCache()
    parse = lambda data: data  # noqa: E731
    gone, kept = tmp_path / "gone.jsonl", tmp_path / "kept.jsonl"
    gone.write_text("{}\n")
    kept.write_text("{}\n")
    cache.get(gone, 1024, parse)
    cache.get(kept, 1024, parse)
    gone.unlink()
    assert cache.get(gone, 1024, parse) == (files.MISSING, None)
    assert [key[0] for key in cache._entries] == [str(kept)]
    cache.forget(kept)
    assert cache._entries == {}


def test_jsonl_tail_parses_only_what_was_appended(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n')
    tail = files.JsonlTail(path, 1024)
    assert tail.read() == (files.OK, [{"n": 1}, {"n": 2}], 0)
    assert tail.parsed_lines == 2
    with path.open("a") as handle:
        handle.write('{"n": 3}\n{"n": ')  # a torn line waits for its newline
    code, found, _bad = tail.read()
    assert [row["n"] for row in found] == [1, 2, 3] and tail.parsed_lines == 3
    with path.open("a") as handle:
        handle.write("4}\n")
    assert [row["n"] for row in tail.read()[1]] == [1, 2, 3, 4] and tail.parsed_lines == 4
    assert tail.read()[1] == [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}] and tail.parsed_lines == 4


def test_jsonl_tail_rereads_after_truncation_or_replacement(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n')
    tail = files.JsonlTail(path, 1024)
    tail.read()
    path.write_text('{"n": 9}\n')  # shrank
    assert tail.read()[1] == [{"n": 9}]
    replacement = tmp_path / "new.jsonl"
    replacement.write_text('{"n": 7}\n{"n": 8}\n{"n": 9}\n')  # longer, new inode
    os.replace(replacement, path)
    assert [row["n"] for row in tail.read()[1]] == [7, 8, 9]
    path.unlink()
    assert tail.read() == (files.MISSING, [], 0)


def test_jsonl_tail_refuses_a_link_and_a_file_past_its_cap(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text('{"n": 1}\n')
    (tmp_path / "link.jsonl").symlink_to(real)
    assert files.JsonlTail(tmp_path / "link.jsonl", 1024).read()[0] == files.REFUSED
    assert files.JsonlTail(real, 3).read()[0] == files.TOO_LARGE


# -- appends --------------------------------------------------------------------


def test_append_line_creates_0600_and_tightens_a_wider_file(tmp_path):
    path = tmp_path / "out.jsonl"
    assert files.append_line(path, b'{"a": 1}\n') is True
    assert mode(path) == 0o600
    os.chmod(path, 0o644)
    assert files.append_line(path, b'{"b": 2}\n') is False
    assert mode(path) == 0o600
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"a": 1}, {"b": 2}]


def test_append_line_never_waits_on_a_fifo(tmp_path):
    fifo = tmp_path / "ledger.jsonl"
    os.mkfifo(fifo)
    out: dict = {}

    def target():
        try:
            files.append_line(fifo, b"x\n")
        except (OSError, files.SourceError) as exc:
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "O_NONBLOCK: a FIFO with no reader fails at once"
    assert isinstance(out.get("error"), OSError)


def test_append_line_refuses_a_link(tmp_path):
    real = tmp_path / "real"
    real.write_text("")
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(files.SourceError):
        files.append_line(tmp_path / "link", b"x\n")
    assert real.read_text() == ""


# -- modes ----------------------------------------------------------------------


def test_tighten_params_tightens_only_what_is_wider_and_never_follows_a_link(tmp_path):
    base = tmp_path / "laya"
    for folder in ("data", "evals", "golden", "ledger", "stability", "checkpoints/ck-1", "checkpoints/ck-1.data"):
        (base / folder).mkdir(parents=True)
    os.chmod(base, 0o755)
    os.chmod(base / "data", 0o755)
    (base / "data" / "labels.jsonl").write_text("{}\n")
    os.chmod(base / "data" / "labels.jsonl", 0o644)
    (base / "evals" / "r.json").write_text("{}")
    os.chmod(base / "evals" / "r.json", 0o600)  # already tight: left alone
    stop_set = base / "stability" / "agent_open_work-stop-set.jsonl"  # operator text, restored or copied in wide
    stop_set.write_text("{}\n")
    os.chmod(stop_set, 0o644)
    (base / "checkpoints" / "ck-1.data" / "train.jsonl").write_text("{}\n")
    os.chmod(base / "checkpoints" / "ck-1.data" / "train.jsonl", 0o664)
    (base / "checkpoints" / "ck-1" / "model.safetensors").write_text("w")
    os.chmod(base / "checkpoints" / "ck-1" / "model.safetensors", 0o644)  # weights are not the operator's text
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    (base / "data" / "link.jsonl").symlink_to(outside)
    for folder in ("evals", "golden", "ledger", "stability", "checkpoints", "checkpoints/ck-1", "checkpoints/ck-1.data"):
        os.chmod(base / folder, 0o700)

    changed = files.tighten_params(base)
    assert sorted(changed) == [
        ".",
        "checkpoints/ck-1.data/train.jsonl",
        "data",
        "data/labels.jsonl",
        "stability/agent_open_work-stop-set.jsonl",
    ]
    assert mode(base) == 0o700 and mode(base / "data") == 0o700 and mode(base / "data" / "labels.jsonl") == 0o600
    assert mode(stop_set) == 0o600
    assert mode(base / "checkpoints" / "ck-1.data" / "train.jsonl") == 0o600
    assert mode(base / "checkpoints" / "ck-1" / "model.safetensors") == 0o644
    assert mode(outside) == 0o644, "a link is never followed"
    assert files.tighten_params(base) == [], "idempotent"


def test_tighten_params_never_raises(tmp_path):
    assert files.tighten_params(tmp_path / "absent") == []
    link = tmp_path / "linked"
    link.symlink_to(tmp_path)
    assert files.tighten_params(link) == []
