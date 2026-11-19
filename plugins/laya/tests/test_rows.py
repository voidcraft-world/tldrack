"""THE key: one hash that ledger rows, labels, operator answers and train rows join on."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from laya_bridge import files, rows


def test_a_string_state_keys_exactly_like_laya_lab_text_key():
    assert rows.state_key("x") == hashlib.sha256(b"x").hexdigest()[:16]
    text = "Phases A and B are done — C is next ✅"
    assert rows.state_key(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_an_object_state_rehashes_from_its_stored_canonical_text():
    state = {"headline": "Café ✅", "label": "über", "n": [1, 2.5]}
    stored = rows.canonical_json(state)
    assert "Café ✅" in stored, "ensure_ascii=False keeps the text readable"
    assert rows.state_digest(stored) == rows.state_digest(state)
    assert rows.canonical_json({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_canonical_json_refuses_what_json_cannot_carry(bad):
    with pytest.raises(ValueError):
        rows.canonical_json({"a": bad})
    with pytest.raises(ValueError):
        rows.state_digest({"a": bad})


def test_bucket_is_stable_and_salted():
    assert rows.bucket("abc", "review-sample") == rows.bucket("abc", "review-sample")
    expected = int(hashlib.sha256(b"review-sample:abc").hexdigest()[:8], 16) % 100
    assert rows.bucket("abc", "review-sample") == expected
    draws = {rows.bucket(f"k{i}", "s") for i in range(500)}
    assert min(draws) >= 0 and max(draws) <= 99 and len(draws) > 80


def test_utc_stamp_is_the_z_form_zod_reads():
    stamp = rows.utc_stamp(datetime(2026, 10, 27, 1, 2, 3, 999, tzinfo=timezone(timedelta(hours=-5))))
    assert stamp == "2026-10-27T06:02:03Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", rows.utc_stamp())


def test_trained_keys_union_every_record_and_report_checkpoints_without_one(tree):
    tree.register(checkpoint="tldrack-agent-open-work-20260102-0203", train_texts=["alpha", "beta"])
    tree.register(checkpoint="tldrack-agent-open-work-20260102-0304", train_texts=["gamma"])
    tree.path("checkpoints/tldrack-agent-open-work-20261017-0900").mkdir()  # no .data
    tree.register(checkpoint="tldrack-agent-open-work-x-20260102-0304", train_texts=["other surface"])  # a longer name
    keys, without = rows.trained_keys(tree.base, "tldrack-agent-open-work")
    assert keys == {rows.state_key(t) for t in ("alpha", "beta", "gamma")}
    assert without == ("tldrack-agent-open-work-20261017-0900",)


def test_trained_keys_skip_bad_lines_and_never_keep_rows(tree):
    path = tree.write_jsonl("checkpoints/tldrack-agent-open-work-1.data/train.jsonl", [{"state": "one"}])
    with path.open("a") as handle:
        handle.write("{torn\n")
        handle.write(json.dumps({"state": "two"}) + "\n")
    tree.path("checkpoints/tldrack-agent-open-work-1").mkdir()
    keys, without = rows.trained_keys(tree.base, "tldrack-agent-open-work")
    assert keys == {rows.state_key("one"), rows.state_key("two")} and without == ()


def test_a_linked_or_oversized_record_counts_as_no_record(tree, tmp_path, monkeypatch):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"state": "leak"}) + "\n")
    tree.path("checkpoints/tldrack-agent-open-work-1").mkdir()
    data = tree.path("checkpoints/tldrack-agent-open-work-1.data/x").parent
    (data / "train.jsonl").symlink_to(outside)
    keys, without = rows.trained_keys(tree.base, "tldrack-agent-open-work")
    assert keys == frozenset() and without == ("tldrack-agent-open-work-1",)

    os.unlink(data / "train.jsonl")
    (data / "train.jsonl").write_text(json.dumps({"state": "big"}) + "\n")
    monkeypatch.setattr(rows, "TRAIN_CAP_BYTES", 4)
    assert rows.trained_keys(tree.base, "tldrack-agent-open-work") == (frozenset(), ("tldrack-agent-open-work-1",))


def test_trained_keys_are_cached_per_stamp(tree, monkeypatch):
    tree.register(train_texts=["alpha"])
    calls = []
    real = files.read_capped
    monkeypatch.setattr(files, "read_capped", lambda path, cap: calls.append(path) or real(path, cap))
    rows.trained_keys(tree.base, "tldrack-agent-open-work")
    rows.trained_keys(tree.base, "tldrack-agent-open-work")
    assert len(calls) == 1


def test_no_checkpoints_folder_is_no_keys(tree):
    assert rows.trained_keys(tree.base, "tldrack-agent-open-work") == (frozenset(), ())
