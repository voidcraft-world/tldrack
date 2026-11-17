"""`laya-lab fleet`: the rack's live use as counts, from a synthetic ledger, metrics store and next-step log.

Every session name and text here is invented. No model loads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from laya_lab import fleet
from laya_lab.golden import SUITES
from laya_bridge.ledger import Ledger
from tldrack import rack_metrics

from conftest import SUITE, decide

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)
SUITE_SPEC = SUITES[SUITE]


class Desk:
    """A ledger writer and a metrics store whose clock a test moves."""

    def __init__(self, lab, metrics_path: Path) -> None:
        self.lab, self.now = lab, NOW
        self.book = Ledger(lambda: lab.base, clock=lambda: self.now)
        self.metrics = rack_metrics.RackMetrics(metrics_path, clock=lambda: self.now.timestamp())

    def verdict(self, p: float, *, days_ago: float = 0, ms: float = 400, **row) -> None:
        self.now = NOW - timedelta(days=days_ago)
        decide(self.lab, [f"invented turn end {p} {days_ago}"], p=p, ms=ms, book=self.book, **row)

    def raised(self, *, days_ago: float = 0, count: int = 1) -> None:
        self.now = NOW - timedelta(days=days_ago)
        assert self.metrics.signal(fleet.REACHED_SIGNAL, "invented-session", count=count)


@pytest.fixture
def desk(lab, tmp_path):
    return Desk(lab, tmp_path / "metrics" / "rack-metrics.sqlite")


def write_next_steps(path: Path, events: list[tuple[str, str, float]]) -> Path:
    """`(event, kind, days ago)` rows in the log's own shape; pointers only, as the bridge writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event, kind, days_ago in events:
            handle.write(
                json.dumps(
                    {
                        "ts": (NOW - timedelta(days=days_ago)).timestamp(),
                        "event": event,
                        "kind": kind,
                        "session": "invented-session",
                        "mode": "shadow",
                        "action": "select",
                        "source": "rules",
                        "model_version": "rules@r1",
                        "hash": "0" * 16,
                    }
                )
                + "\n"
            )
    return path


def measure(desk, tmp_path, **kwargs):
    return fleet.measure(
        desk.lab.base,
        SUITE_SPEC,
        now=NOW,
        metrics_path=desk.metrics.path,
        next_step_path=kwargs.pop("next_step_path", tmp_path / "none.jsonl"),
        **kwargs,
    )


def test_each_verdict_is_banded_at_the_cuts_it_was_served_with(desk, tmp_path):
    desk.verdict(0.80)  # open at the row's 0.62
    desk.verdict(0.65)  # open at 0.62, though under the code's 0.7
    desk.verdict(0.50)  # reply
    desk.verdict(0.10)  # done under the row's 0.3
    desk.raised(count=2)
    got, why = measure(desk, tmp_path)
    assert why is None
    assert got["judged"] == 4 and got["verdicts"] == {"done": 1, "reply": 1, "open": 2}
    assert got["reached"] == 2 and got["days"] == fleet.DEFAULT_DAYS
    assert got["latency"] == {"n": 4, "p50_ms": 400.0, "p90_ms": 400.0}
    assert got["next_step"] is None


def test_the_window_leaves_older_decisions_and_signals_out(desk, tmp_path):
    desk.verdict(0.9, days_ago=1)
    desk.raised(days_ago=1)
    desk.verdict(0.9, days_ago=9)
    desk.raised(days_ago=9)
    got, _ = measure(desk, tmp_path)
    assert got["judged"] == 1 and got["reached"] == 1
    wider, _ = measure(desk, tmp_path, days=14)
    assert wider["judged"] == 2 and wider["reached"] == 2 and wider["days"] == 14


def test_the_next_step_rate_is_what_the_log_says_was_taken(desk, tmp_path):
    desk.verdict(0.9)
    desk.raised()
    log = write_next_steps(
        tmp_path / "next-step-log.jsonl",
        [
            ("suggested", "suggest", 0),
            ("suggested", "suggest", 0),
            ("suggested", "suggest", 0),
            ("suggested", "none", 0),
            ("clicked", "suggest", 0),
            ("taken", "suggest", 0),
            ("dismissed", "suggest", 0),
            ("suggested", "suggest", 20),
        ],
    )
    got, _ = measure(desk, tmp_path, next_step_path=log)
    assert got["next_step"] == {"suggested": 3, "taken": 2}


def test_no_decision_or_no_metrics_store_is_no_fleet_with_a_reason(desk, tmp_path):
    got, why = measure(desk, tmp_path)
    assert got is None and "no agent_open_work decision" in why
    desk.verdict(0.9)
    got, why = fleet.measure(desk.lab.base, SUITE_SPEC, now=NOW, metrics_path=tmp_path / "absent.sqlite")
    assert got is None and "metrics" in why


def test_more_warnings_than_open_verdicts_is_refused_not_written(desk, tmp_path):
    desk.verdict(0.1)
    desk.raised(count=3)
    got, why = measure(desk, tmp_path)
    assert got is None and "disagree" in why


def test_fold_sets_fleet_and_keeps_every_other_key(desk, tmp_path):
    desk.verdict(0.9)
    desk.raised()
    got, _ = measure(desk, tmp_path)
    results = tmp_path / "results" / "2026-11-13.json"
    results.parent.mkdir()
    results.write_text(json.dumps({"v": 1, "voters": [], "sets": [], "fleet": None}), encoding="utf-8")
    fleet.fold_into(results, got)
    written = json.loads(results.read_text(encoding="utf-8"))
    assert written["fleet"] == got and written["voters"] == [] and written["v"] == 1
    assert not list(results.parent.glob(".*.tmp"))


def test_fold_refuses_a_fixture_or_a_missing_file(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"fixture": True, "fleet": None}), encoding="utf-8")
    with pytest.raises(ValueError):
        fleet.fold_into(fixture, {})
    with pytest.raises(FileNotFoundError):
        fleet.fold_into(tmp_path / "absent.json", {})


def test_the_cli_folds_into_the_named_day(desk, tmp_path, monkeypatch, capsys):
    desk.verdict(0.9)
    desk.raised()
    real = fleet.measure
    monkeypatch.setattr(
        fleet,
        "measure",
        lambda base, suite, days: real(
            base, suite, days=days, now=NOW, metrics_path=desk.metrics.path, next_step_path=tmp_path / "none.jsonl"
        ),
    )
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    (out_dir / "2026-11-13.json").write_text(json.dumps({"v": 1, "fleet": None}), encoding="utf-8")
    assert fleet.cmd(SUITE, desk.lab.base, write=True, day="2026-11-13", out_dir=out_dir) == 0
    assert json.loads((out_dir / "2026-11-13.json").read_text(encoding="utf-8"))["fleet"]["judged"] == 1
    assert "invented" not in capsys.readouterr().out
    assert fleet.cmd(SUITE, desk.lab.base, write=True, day="2026-11-14", out_dir=out_dir) == 1
