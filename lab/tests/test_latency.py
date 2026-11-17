"""The latency read: the ledger's `ms` cut by state length, Mac load, cold start and gap, and the token overflow of the
states Laya is asked. Synthetic ledgers and a fake token meter only: no tokenizer, no torch, no operator data."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from laya_lab import cli, infer, latency
from laya_lab.golden import SUITES
from laya_lab.registry import register
from laya_bridge import lab_files, paths, rows
from laya_bridge.ledger import Ledger
from laya_bridge.ops import ops_state
from laya_bridge.wire import clean_request
from tldrack import open_work

from conftest import CHECKPOINT, SUITE, SURFACE, decide

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=2)


class Desk:
    """A ledger writer whose clock and load a test moves."""

    def __init__(self, lab) -> None:
        self.lab, self.now, self.load = lab, T0, None
        self.book = Ledger(lambda: lab.base, clock=lambda: self.now, load=lambda: self.load)

    def decide(self, text: str, ms: float, *, after: float, load: float | None = None, spawn: int | None = None, **row):
        self.now, self.load = T0 + timedelta(seconds=after), load
        return decide(self.lab, [text], ms=ms, book=self.book, spawn=spawn, **row)[0]

    def paste(self, *, after: float) -> None:
        """A decision on another surface: the same laya-serve answered it."""
        self.now = T0 + timedelta(seconds=after)
        cleaned = clean_request({"state": "pasted", "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
        self.book.record_decision({"surface": "paste"}, cleaned, {"answers": {"q": {"noul": 0.5}}}, "english", None, None, 9)


def buckets(cut: list[dict]) -> dict[str, tuple]:
    return {b["bucket"]: (b["n"], b["p50"]) for b in cut}


def fake_meter(chars_per_token: int = 4, room: int = 250):
    """A meter that reads tokens off the state's length, and the opener `truncation` takes."""

    def opener(_base, _suite):
        def meter(state) -> tuple[int, int]:
            tokens = len(state) // chars_per_token
            return tokens, max(0, tokens - room)

        return meter, {"model": "english", "checkpoint": None, "digest": "english.laya-0.4.19", "window": 512, "room": room}

    return opener


# ── the cuts ──────────────────────────────────────────────────────────────────


def test_ms_is_cut_by_length_load_start_and_gap(lab):
    desk = Desk(lab)
    desk.decide("s" * 150, 900, after=0, load=0.2, spawn=1)  # the model's first answer: cold, recorded
    desk.decide("t" * 150, 40, after=2, load=0.2, spawn=1)  # warm, recorded
    desk.decide("u" * 1000, 500, after=32, load=1.5, spawn=1)  # warm, a long state on a busy Mac
    desk.decide("v" * 300, 800, after=32 + 20 * 60)  # an older row: no cold, no load; 20 min idle
    desk.paste(after=32 + 20 * 60 + 1)
    desk.decide("w" * 300, 60, after=32 + 20 * 60 + 5)  # 4 s after the paste decision: warm by the gap rule
    timed = latency.read_decisions(lab.base, SUITES[SUITE], None)
    assert [(row.ms, row.gap_s, row.cold, row.cold_recorded) for row in timed] == [
        (900, None, True, True),
        (40, 2, False, True),
        (500, 30, False, True),
        (800, 1200, True, False),
        (60, 4, False, False),
    ]
    report = latency.measure(timed)
    assert report["latency_ms"] == {"n": 5, "p50": 500.0, "p90": 860.0, "p95": 880.0, "max": 900, "le_100": 0.4}
    assert buckets(report["by_length"]) == {"0-200": (2, 470.0), "201-400": (2, 430.0), "801-1300": (1, 500.0)}
    assert buckets(report["by_load"]) == {"<0.5": (2, 470.0), "1-2": (1, 500.0), "unrecorded": (2, 430.0)}
    assert buckets(report["by_start"]) == {"cold": (2, 850.0), "warm": (3, 60.0)}
    assert report["cold_source"] == {"recorded": 3, "gap_rule": 2, "unknown": 0}
    assert buckets(report["by_gap"]) == {"<10 s": (2, 50.0), "10-60 s": (1, 500.0), "15 min+": (1, 800.0), "first": (1, 900.0)}


def test_a_window_s_first_row_reads_its_gap_from_before_the_window(lab):
    desk = Desk(lab)
    desk.decide("before the window", 100, after=0)
    desk.decide("inside the window", 100, after=600)
    since = rows.utc_stamp(T0 + timedelta(seconds=300))
    [row] = latency.read_decisions(lab.base, SUITES[SUITE], since)
    assert (row.gap_s, row.cold) == (600, False)
    desk.decide("hours later", 100, after=600 + 3 * 3600)
    [late] = latency.read_decisions(lab.base, SUITES[SUITE], rows.utc_stamp(T0 + timedelta(hours=1)))
    assert (late.gap_s, late.cold) == (3 * 3600, True), "laya-serve had stopped itself long before"
    assert latency.read_decisions(lab.base, SUITES[SUITE], rows.utc_stamp(T0 + timedelta(days=1))) == []


def test_two_decisions_in_one_second_have_no_gap_and_no_row_is_its_own_predecessor(lab):
    desk = Desk(lab)
    desk.decide("first of a burst", 10, after=0)
    desk.decide("second of a burst", 10, after=0)
    assert sorted(row.gap_s for row in latency.read_decisions(lab.base, SUITES[SUITE], None) if row.gap_s is not None) == [0, 0]
    alone = Desk(type(lab)(lab.base.parent / "other"))
    alone.lab.base.mkdir()
    alone.decide("the only decision", 10, after=0)
    assert [row.gap_s for row in latency.read_decisions(alone.lab.base, SUITES[SUITE], None)] == [None]


def test_a_value_at_an_edge_lands_where_its_label_says():
    assert latency.bucket_of(200, latency.LENGTH_BUCKETS, inclusive=True) == "0-200"
    assert latency.bucket_of(open_work.STATE_MAX_CHARS, latency.LENGTH_BUCKETS, inclusive=True) == "801-1300"
    assert latency.bucket_of(open_work.STATE_MAX_CHARS + 1, latency.LENGTH_BUCKETS, inclusive=True) == "1301+"
    assert latency.bucket_of(None, latency.LENGTH_BUCKETS, inclusive=True) == "no text"
    assert latency.bucket_of(0.5, latency.LOAD_BUCKETS, inclusive=False) == "0.5-1"
    assert latency.bucket_of(2.0, latency.LOAD_BUCKETS, inclusive=False) == "2+"
    assert latency.bucket_of(paths.IDLE_STOP_SECONDS, latency.GAP_BUCKETS, inclusive=False) == "15 min+"
    assert latency.spread([]) == {"n": 0, "p50": None, "p90": None}


def test_a_text_the_ledger_cut_reads_as_longer_than_any_fitted_state(lab):
    book = Ledger(lambda: lab.base, load=lambda: None)
    cleaned = clean_request({"state": "x" * 9000, "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
    book.record_decision({"surface": SURFACE}, cleaned, {"answers": {"q": {"noul": 0.5}}}, "english", None, None, 700)
    [row] = latency.read_decisions(lab.base, SUITES[SUITE], None)
    assert latency.bucket_of(row.chars, latency.LENGTH_BUCKETS, inclusive=True) == "1301+"


# ── truncation ────────────────────────────────────────────────────────────────


def labelled(lab, n_short: int = 6, n_long: int = 6) -> tuple[list[str], list[str]]:
    """Short texts that fit and long ones that overflow the fake meter, Sonnet-labelled and scored by the base model:
    every short one scored right, every long one wrong."""
    short = [f"short text {i}: done" for i in range(n_short)]
    long = [f"long text {i}: " + "word " * 250 for i in range(n_long)]
    keys = lab.unlabelled(short + long)
    lab.labels({key: i >= n_short for i, key in enumerate(keys)})
    lab.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [
            lab_files.scores_row(
                key,
                0.1,
                "english",
                None,
                open_work.QUESTION_VERSION,
                0.7,
                "2026-11-08T00:00:00Z",
                device="mps",
                digest="english.laya-0.4.19",
            )
            for key in keys
        ],
    )
    return keys[:n_short], keys[n_short:]


def test_truncation_counts_the_states_that_overflow_as_they_are_asked_and_prices_them(lab):
    short, long = labelled(lab)
    block = latency.truncation(lab.base, SUITE, opener=fake_meter())
    assert block["state"] == "ran" and (block["states"], block["overflowed"], block["share"]) == (12, 6, 0.5)
    assert block["overflowed_keys"] == sorted(long), "keys only"
    fitted = block["tokens"]["max"]
    assert 250 < fitted <= open_work.STATE_MAX_CHARS // 4, "a long text is measured as its fitted state, never at full length"
    assert block["dropped"] == {"p50": fitted - 250.0, "max": fitted - 250}
    assert block["priced"] == {
        "cut": 0.7,
        "fits": {"labelled": 6, "accuracy": 1.0},
        "overflow": {"labelled": 6, "accuracy": 0.0},
        "delta": -1.0,
    }
    assert "word" not in json.dumps(block), "no text leaves the operator's files"


def test_no_overflow_prices_nothing_and_too_few_labels_give_no_delta(lab):
    labelled(lab, 6, 2)
    block = latency.truncation(lab.base, SUITE, opener=fake_meter(room=10_000))
    assert (block["overflowed"], block["dropped"], block["priced"]["delta"]) == (0, None, None)
    few = latency.truncation(lab.base, SUITE, opener=fake_meter())
    assert few["overflowed"] == 2 and few["priced"]["overflow"]["labelled"] == 2 and few["priced"]["delta"] is None


def test_truncation_is_skipped_without_texts_and_fails_without_a_tokenizer(lab):
    assert latency.truncation(lab.base, SUITE, opener=fake_meter())["state"] == "skipped"
    labelled(lab, 1, 1)
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=None), {"eval_items": 1})
    assert "no training record" in latency.truncation(lab.base, SUITE, opener=fake_meter())["priced"]["note"]
    block = latency.truncation(lab.base, SUITE)  # the real opener: the synthetic checkpoint ships no tokenizer
    assert block["state"] == "failed" and block["error"] == "LookupError" and "tokenizer" in block["why"]


# ── the command, its verdict and the rack's line ──────────────────────────────


def test_the_command_writes_one_report_that_the_verdict_and_ops_read(lab, monkeypatch, capsys):
    desk = Desk(lab)
    desk.decide("a live closing text", 900, after=0, load=0.3, spawn=1)
    desk.decide("another live closing text", 300, after=5, load=0.3, spawn=1)
    desk.decide("a decision from before the window", 5000, after=-30 * 86400)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "aaaaaaaaaaaa")
    assert latency.run(SUITE, lab.base, days=14, now=NOW, opener=fake_meter()) == 0
    path, report = lab_files.newest_latency(lab.base, SUITE)
    assert path.name == "latency-agent-open-work-20261108-140000.json" and report["window"]["days"] == 14
    assert report["latency_ms"]["n"] == 2 and report["truncation"]["states"] == 3
    out = capsys.readouterr().out
    assert "2 decisions · 14 d" in out and "of 3 states overflow" in out and "closing text" not in out
    verdict, why = lab_files.run_verdict(lab.base, "latency", SUITE)
    assert why == "" and verdict == {
        "verdict": "PASS",
        "headline": {"cases": 2, "latency_ms_p50": 600.0, "latency_ms_p95": 870.0},
        "model_digest": "english.laya-0.4.19",
        "dataset_version": f"q{open_work.QUESTION_VERSION}",
        "accelerator": None,
    }
    [row] = ops_state(lab.base, None, [], lab_ok=True)["latency"]
    assert row == {
        "surface": SURFACE,
        "at": "2026-11-08T14:00:00Z",
        "days": 14,
        "decisions": 2,
        "p50_ms": 600.0,
        "p90_ms": 840.0,
        "le_100": 0.0,
        "cold": {"n": 1, "p50_ms": 900.0},
        "warm": {"n": 1, "p50_ms": 300.0},
        "truncation": {"states": 3, "overflowed": 0},
    }
    assert (
        "  latency  agent_open_work · 2026-11-08T14:00:00Z · 2 decisions · p50 600 ms · p90 840 ms · cold p50 900 (1) / "
        "warm 300 (1) · 0 of 3 states overflow"
    ) in cli.status_lines(ops_state(lab.base, None, [], lab_ok=True))

    assert latency.run(SUITE, lab.base, days=0, now=NOW + timedelta(seconds=1), opener=fake_meter()) == 0
    assert lab_files.newest_latency(lab.base, SUITE)[1]["latency_ms"] == {
        **report["latency_ms"],
        "n": 3,
        "p50": 900.0,
        "p90": 4180.0,
        "p95": 4590.0,
        "max": 5000,
    }


def test_an_empty_ledger_is_inconclusive_and_a_missing_tokenizer_fails_the_verdict(lab, monkeypatch):
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "bbbbbbbbbbbb")
    assert latency.run(SUITE, lab.base, now=NOW, opener=fake_meter()) == 0
    assert lab_files.run_verdict(lab.base, "latency", SUITE)[0]["verdict"] == "INCONCLUSIVE"
    assert ops_state(lab.base, None, [], lab_ok=True)["latency"][0]["truncation"] is None

    def no_tokenizer(_base, _suite):
        raise LookupError("no snapshot on disk holds a tokenizer")

    Desk(lab).decide("a live closing text", 300, after=0)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "cccccccccccc")
    assert latency.run(SUITE, lab.base, now=NOW + timedelta(seconds=1), opener=no_tokenizer) == 0
    verdict = lab_files.run_verdict(lab.base, "latency", SUITE)[0]
    assert verdict["verdict"] == "FAIL" and verdict["headline"]["cases"] == 1, "the latency half still stands"


def test_the_latency_read_is_its_own_command_and_takes_no_slot_of_the_lease(lab, monkeypatch):
    args = cli.build_parser().parse_args(["latency", SUITE])
    assert args.days == latency.DEFAULT_DAYS and cli.COMMANDS["latency"] is cli.cmd_latency
    monkeypatch.setattr(infer, "hold_slot", lambda *_a, **_kw: pytest.fail("a read took a slot of the lease"))
    Desk(lab).decide("a live closing text", 300, after=0)
    assert (
        latency.run(
            SUITE,
            lab.base,
            now=NOW + timedelta(seconds=1),
            opener=lambda _base, _suite: (_ for _ in ()).throw(LookupError("no tokenizer")),
        )
        == 0
    )


def test_the_real_meter_is_built_from_laya_s_own_sequence_builder():
    """`open_meter` leans on four laya names, two of them private. The exact pin in pyproject.toml holds them, and this
    says so the day the pin moves. Read from the installed source: importing laya would load torch into this process."""
    spec = importlib.util.find_spec("laya")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("laya is not installed in this venv")
    package = Path(next(iter(spec.submodule_search_locations)))
    common, agent = (package / "common.py").read_text(), (package / "agent.py").read_text()
    for anchor in ("def build_sequence(", "return_truncation_stats: bool = False", '"state_tokens_dropped"', "def state_room("):
        assert anchor in common, anchor
    for anchor in ("def _load_tokenizer(tok_dir: str, cfg: Dict)", "def _to_internal(qdef: Dict)"):
        assert anchor in agent, anchor
    assert "def resolve_model_spec(" in (package / "router.py").read_text()
