"""Session costs: deltas booked per day, fixer spend against the cap, persistence and rollups."""

import json
from datetime import datetime

import pytest

from tldrack import session_costs
from tldrack.session_costs import (
    SessionCostStore,
    fix_cap_usd,
    is_fixer,
    parse_statusline_cost,
    resolve_tmux_session,
)

SID = "0123abcd-4567-89ef-0123-456789abcdef"
SID2 = "fedcba98-7654-3210-fedc-ba9876543210"
DAY1 = datetime(2030, 1, 10, 12).timestamp()
DAY2 = datetime(2030, 1, 11, 12).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_parse_statusline_cost():
    assert parse_statusline_cost({"session_id": SID, "cost": {"total_cost_usd": 1.25}}) == (SID, 1.25)
    assert parse_statusline_cost({"session_id": SID}) is None
    assert parse_statusline_cost({"session_id": "x", "cost": {"total_cost_usd": 1}}) is None
    assert parse_statusline_cost({"session_id": SID, "cost": {"total_cost_usd": -1}}) is None
    assert parse_statusline_cost({"session_id": SID, "cost": {"total_cost_usd": True}}) is None
    assert parse_statusline_cost({"session_id": SID, "cost": {"total_cost_usd": float("nan")}}) is None
    assert parse_statusline_cost({"session_id": SID, "cost": {"total_cost_usd": 1e9}}) is None


def test_deltas_book_into_the_day_they_happen(tmp_path):
    clock = Clock(DAY1)
    store = SessionCostStore(tmp_path / "c.json", clock=clock)
    assert store.record(SID, 1.0, "vc-1")
    assert store.record(SID, 3.0, "vc-1")
    assert not store.record(SID, 3.0, "vc-1"), "no new spend, no write"
    clock.now = DAY2
    store.record(SID, 4.5, "vc-1")
    summary = store.summary()
    assert summary["today_usd"] == 1.5
    assert summary["week_usd"] == 4.5
    assert summary["sessions"][0] == {"session": "vc-1", "today": 1.5, "week": 4.5}


def test_a_falling_total_is_booked_whole(tmp_path):
    store = SessionCostStore(tmp_path / "c.json", clock=Clock(DAY1))
    store.record(SID, 5.0, "vc-1")
    store.record(SID, 0.5, "vc-1")
    assert store.summary()["today_usd"] == 5.5


def test_fixers_and_the_cap(tmp_path, monkeypatch):
    store = SessionCostStore(tmp_path / "c.json", clock=Clock(DAY1))
    store.record(SID, 2.0, "deploy-fix-2")
    store.record(SID2, 7.0, "vc-3")
    assert store.fixer_spend_today() == 2.0
    monkeypatch.setenv(session_costs.FIX_CAP_ENV, "2")
    fixers = store.summary()["fixers"]
    assert fixers == {"today_usd": 2.0, "cap_usd": 2.0, "over_cap": True}
    monkeypatch.setenv(session_costs.FIX_CAP_ENV, "nope")
    assert fix_cap_usd() == session_costs.DEFAULT_FIX_CAP_USD


def test_is_fixer():
    for name in ("deploy-fix", "deploy-fix-9", "wt-fix-err-3fa9c1d2"):
        assert is_fixer(name)
    for name in ("vc-1", "deploy-fixer", "wt-feat-x", None, ""):
        assert not is_fixer(name)


def test_persistence_round_trip_and_prune(tmp_path):
    path = tmp_path / "c.json"
    clock = Clock(DAY1)
    store = SessionCostStore(path, clock=clock)
    store.record(SID, 1.0, "vc-1")
    reread = SessionCostStore(path, clock=clock)
    assert reread.summary()["today_usd"] == 1.0
    clock.now = DAY1 + 20 * 86_400
    reread.record(SID2, 0.25, "vc-2")
    data = json.loads(path.read_text())
    assert list(data["days"]) == [datetime.fromtimestamp(clock.now).strftime("%Y-%m-%d")]
    assert SID not in data["sessions"], "a conversation silent past the window is forgotten"


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{nope")
    store = SessionCostStore(path, clock=Clock(DAY1))
    assert store.summary()["today_usd"] == 0
    assert path.read_text() == "{nope", "never re-persisted until a real write"


def test_rename_and_missions(tmp_path):
    store = SessionCostStore(tmp_path / "c.json", clock=Clock(DAY1))
    store.record(SID, 2.0, "vc-1")
    store.rename("vc-1", "docs")
    summary = store.summary(lambda name: "m1" if name == "docs" else None)
    assert summary["sessions"][0]["session"] == "docs"
    assert summary["missions"] == [{"mission": "m1", "today": 2.0, "week": 2.0}]


def test_unbound_sessions_are_labelled_by_agent_id(tmp_path):
    store = SessionCostStore(tmp_path / "c.json", clock=Clock(DAY1))
    store.record(SID, 1.0, None)
    assert store.summary()["sessions"][0]["session"] == f"agent:{SID[:8]}"
    store.record(SID, 1.5, "bad name!")
    assert store.summary()["sessions"][0]["session"] == f"agent:{SID[:8]}"


def test_resolve_tmux_session():
    bound = {"vc-2": "/r/b.jsonl", "vc-1": "/r/a.jsonl"}
    normalize = lambda p: p if isinstance(p, str) and p.endswith(".jsonl") else None  # noqa: E731
    assert resolve_tmux_session("/r/a.jsonl", bound, normalize) == "vc-1"
    assert resolve_tmux_session("/r/zzz.jsonl", bound, normalize) is None
    assert resolve_tmux_session(None, bound, normalize) is None


@pytest.mark.parametrize("payload", [{}, {"session_id": SID, "cost": "x"}])
def test_parse_never_raises(payload):
    assert parse_statusline_cost(payload) is None
