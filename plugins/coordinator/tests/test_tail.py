"""Bounded-tail tests: the reader must survive every shape a live log takes.

A transcript is append-only and written by another process, so the scan races
its writer routinely. Every case below is one of those races (or one of the
format's quirks, listed in `tail.py`), and none of them may raise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from coordinator.tail import (
    age_hours,
    iso_utc,
    last_timestamp,
    parse_timestamp,
    tail_records,
)


def write_lines(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_discards_the_partial_first_line(tmp_path):
    # Seeking into the middle of the file lands inside a record; that fragment
    # must be dropped rather than parsed or raised on.
    path = tmp_path / "t.jsonl"
    records = [{"type": "assistant", "n": index, "pad": "x" * 200} for index in range(50)]
    write_lines(path, records)
    tailed = tail_records(path, tail_bytes=1024)
    assert tailed, "a bounded tail should still return records"
    assert tailed[-1]["n"] == 49
    assert all(isinstance(record, dict) and "n" in record for record in tailed)
    assert len(tailed) < len(records)  # genuinely bounded


def test_file_smaller_than_the_window_is_read_whole(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [{"a": 1}, {"a": 2}])
    assert tail_records(path, tail_bytes=1024 * 1024) == [{"a": 1}, {"a": 2}]


def test_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert tail_records(path) == []


def test_missing_file_is_not_fatal(tmp_path):
    assert tail_records(tmp_path / "nope.jsonl") == []


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n{"a": 2,\n{"a": 3}\n')  # a half-written record mid-file
    assert tail_records(path) == [{"a": 1}, {"a": 3}]


def test_non_object_json_lines_are_dropped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n"just a string"\n[1,2]\n{"a": 2}\n')
    assert tail_records(path) == [{"a": 1}, {"a": 2}]


def test_last_active_survives_timestamp_less_trailing_records(tmp_path):
    # `mode`, `ai-title`, `bridge-session` and friends carry no timestamp and
    # are commonly the FINAL lines — reading the last record would often
    # answer None.
    records = [
        {"type": "assistant", "timestamp": "2026-08-02T09:00:00.000Z"},
        {"type": "assistant", "timestamp": "2026-08-02T10:30:00.000Z"},
        {"type": "ai-title", "aiTitle": "Something"},
        {"type": "mode", "mode": "default"},
        {"type": "bridge-session", "id": "abc"},
    ]
    path = write_lines(tmp_path / "t.jsonl", records)
    assert iso_utc(last_timestamp(tail_records(path))) == "2026-08-02T10:30:00Z"


def test_last_active_is_none_when_nothing_is_timestamped(tmp_path):
    path = write_lines(tmp_path / "t.jsonl", [{"type": "mode"}, {"type": "ai-title"}])
    assert last_timestamp(tail_records(path)) is None


def test_parse_timestamp_accepts_the_transcript_z_form():
    parsed = parse_timestamp("2030-01-02T03:04:05.678Z")
    assert parsed == datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)


def test_parse_timestamp_normalizes_offsets_to_utc():
    assert parse_timestamp("2026-08-02T20:00:00+02:00") == datetime(2026, 8, 2, 18, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_rejects_junk():
    for value in (None, "", "not a date", 17856970, {"t": 1}):
        assert parse_timestamp(value) is None


def test_age_hours_is_rounded_and_none_safe():
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert age_hours(parse_timestamp("2026-08-01T12:00:00Z"), now) == 24.0
    assert age_hours(None, now) is None
