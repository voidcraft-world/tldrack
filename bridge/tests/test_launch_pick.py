"""AUTO launch pick — the freest account for a new agent (launch_pick.py)."""

from __future__ import annotations

import pytest

from tldrack.launch_pick import (
    ENGAGED_WITHIN_S,
    IDLE_SESSION_LOAD,
    PACE_FLOOR_S,
    is_engaged,
    pick_launch_profile,
    window_left,
    window_rate,
)

NOW = 1_800_000_000.0
LATER = NOW + 3_600
HOUR = 3_600.0
DAY = 86_400.0


def _profile(profile_id, kind="claude", five=None, week=None, cli=True, five_reset=None, week_reset=None):
    usage = {}
    if five is not None:
        usage["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if week is not None:
        usage["seven_day"] = {"used_percentage": week, "resets_at": week_reset}
    return {"id": profile_id, "label": profile_id.upper()[:5], "kind": kind, "cli_found": cli, "usage": usage or None}


def _row(profile, state="busy", live=True, age=30.0, created=NOW - 7_200, **extra):
    return {
        "session": f"s-{id(extra)}-{state}",
        "profile": profile,
        "state": state,
        "live": live,
        "age_seconds": age,
        "created_at": created,
        **extra,
    }


def _idle(profile):
    """Open for hours, last touched an hour ago — parked."""
    return _row(profile, state="free", age=3_600.0)


RACK = [
    _profile("a", week=40),
    _profile("b", week=50),
    _profile("c", week=10),
    _profile("codex", kind="codex", week=47),
    {"id": "shell", "label": "SHELL", "kind": "shell", "cli_found": True, "usage": None},
]
CAPACITY = {"c": 0.5}


def test_two_busy_accounts_leave_the_idle_one_picked():
    rows = [_row("a"), _row("a"), _row("b"), _row("b", state="background"), _row("codex")]
    pick = pick_launch_profile(RACK, rows, NOW, CAPACITY)
    assert pick.id == "c"
    assert pick.spent is False
    assert pick.reason == "C · 90% left · idle"


def test_a_half_size_plan_is_worth_half_of_what_it_has_left():
    # Everyone idle and equally used: c's 0.5 capacity puts it below the
    # full-size accounts, and order breaks the tie between those.
    rack = [_profile("a", week=20), _profile("b", week=20), _profile("c", week=20)]
    pick = pick_launch_profile(rack, [], NOW, CAPACITY)
    assert pick.id == "a"
    assert [c.id for c in pick.ranking] == ["a", "b", "c"]
    assert pick.ranking[2].headroom == pytest.approx(0.4)


def test_the_tighter_window_binds():
    rack = [_profile("a", five=90, week=10), _profile("b", five=10, week=40)]
    pick = pick_launch_profile(rack, [], NOW)
    assert pick.id == "b"
    assert pick.ranking[1].left == pytest.approx(0.1)


def test_a_missing_window_or_reading_counts_as_full():
    assert window_left(None, "five_hour") == (1.0, None)
    assert window_left({"seven_day": {"used_percentage": 30}}, "five_hour") == (1.0, None)
    rack = [_profile("a", week=5), _profile("codex", kind="codex")]  # codex: no reading yet
    assert pick_launch_profile(rack, [], NOW).id == "codex"


def test_shells_and_missing_clis_are_never_picked():
    rack = [
        _profile("codex", kind="codex", cli=False),
        {"id": "shell", "label": "SHELL", "kind": "shell", "cli_found": True, "usage": None},
        _profile("a", week=99),
    ]
    pick = pick_launch_profile(rack, [], NOW)
    assert pick.id == "a"
    assert [c.id for c in pick.ranking] == ["a"]


def test_nothing_launchable_is_a_pick_of_none():
    pick = pick_launch_profile([_profile("a", cli=False)], [], NOW)
    assert pick.id is None
    assert pick.as_wire()["ranking"] == []


def test_a_burst_spreads_a_just_spawned_session_counts_as_engaged():
    rack = [_profile("a", week=40), _profile("b", week=40)]
    first = pick_launch_profile(rack, [], NOW)
    assert first.id == "a"
    # The first click's session: born a second ago, no turn reported yet.
    fresh = _row("a", state="unknown", age=None, created=NOW - 1)
    assert pick_launch_profile(rack, [fresh], NOW).id == "b"


def test_engaged_is_working_or_touched_within_the_window():
    assert is_engaged({"state": "attention"}, NOW)
    assert is_engaged({"state": "free", "age_seconds": 30.0}, NOW)  # its turn just ended
    assert is_engaged({"state": "free", "age_seconds": None, "created_at": NOW - 10}, NOW)
    stale = ENGAGED_WITHIN_S + 1
    assert not is_engaged({"state": "free", "age_seconds": stale, "created_at": NOW - stale}, NOW)
    assert not is_engaged({"state": "unknown", "age_seconds": None, "created_at": None}, NOW)


def test_a_parked_session_is_only_a_tie_breaker():
    rack = [_profile("a", week=40), _profile("b", week=40)]
    pick = pick_launch_profile(rack, [_idle("a")], NOW)
    assert pick.id == "b"
    assert pick.ranking[1].load == IDLE_SESSION_LOAD
    # …and never outweighs real headroom.
    rack = [_profile("a", week=10), _profile("b", week=40)]
    assert pick_launch_profile(rack, [_idle("a"), _idle("a")], NOW).id == "a"


def test_dead_rows_and_unattributed_rows_count_for_nothing():
    rack = [_profile("a", week=40), _profile("b", week=40)]
    rows = [_row("a", live=False), _row(None), _row("a", live=False)]
    assert pick_launch_profile(rack, rows, NOW).ranking[0].load == 0


def test_an_almost_spent_account_is_skipped_however_idle():
    rack = [_profile("a", five=97, week=10), _profile("b", week=60)]
    rows = [_row("b"), _row("b"), _row("b")]
    pick = pick_launch_profile(rack, rows, NOW)
    assert pick.id == "b"
    assert next(c for c in pick.ranking if c.id == "a").spent is True


def test_a_rate_limit_cut_off_marks_the_account_spent_before_its_reading_catches_up():
    rack = [_profile("a", week=20), _profile("b", week=60)]
    cut = _row("a", state="free", age=5_000.0, stop_failure={"error": "rate_limit", "limit": "five_hour", "resets_at": LATER})
    assert pick_launch_profile(rack, [cut], NOW).id == "b"


def test_a_cut_off_that_has_reset_or_was_no_limit_does_not_count():
    rack = [_profile("a", week=20), _profile("b", week=60)]
    past = _row("a", state="free", age=5_000.0, stop_failure={"error": "rate_limit", "limit": "five_hour", "resets_at": NOW - 1})
    overload = _row("a", state="free", age=5_000.0, stop_failure={"error": "overloaded", "limit": None, "resets_at": None})
    assert pick_launch_profile(rack, [past], NOW).id == "a"
    assert pick_launch_profile(rack, [overload], NOW).id == "a"


def test_when_every_account_is_spent_the_soonest_reset_wins_and_says_so():
    rack = [_profile("a", week=100, week_reset=NOW + 9_000), _profile("b", five=99, five_reset=NOW + 600)]
    pick = pick_launch_profile(rack, [], NOW)
    assert pick.id == "b"
    assert pick.spent is True
    assert "spent" in pick.reason


def test_the_wire_block_is_plain_and_rounded():
    wire = pick_launch_profile(RACK, [_row("a")], NOW, CAPACITY).as_wire()
    assert set(wire) == {"id", "label", "reason", "spent", "ranking"}
    assert set(wire["ranking"][0]) == {"id", "score", "left", "rate", "headroom", "load", "spent"}
    assert all(isinstance(entry["score"], float) for entry in wire["ranking"])


# ── pacing against the reset ───────────────────────────────────────────────


def test_less_left_but_resetting_tomorrow_wins():
    # One agent on each. Account a has 50% of its week for 4 more days (0.875 of an
    # even pace); b only 20%, but for one more day (1.4) — budget it
    # loses at tomorrow's reset unless something spends it.
    rack = [_profile("a", week=50, week_reset=NOW + 4 * DAY), _profile("b", week=80, week_reset=NOW + 1 * DAY)]
    pick = pick_launch_profile(rack, [_row("a"), _row("b")], NOW)
    assert pick.id == "b"
    # Its week could burn at 1.4× an even pace; a fresh 5-hour window holds it to 1.0.
    assert pick.ranking[0].week_rate == pytest.approx(1.4)
    assert pick.ranking[0].rate == pytest.approx(1.0)
    assert pick.ranking[1].rate == pytest.approx(0.875)
    assert pick.reason == "B · 20% left · resets in 24h · 1 busy"


def test_far_from_its_reset_the_bigger_budget_still_wins():
    rack = [_profile("a", week=50, week_reset=NOW + 4 * DAY), _profile("b", week=80, week_reset=NOW + 6 * DAY)]
    assert pick_launch_profile(rack, [], NOW).id == "a"


def test_the_five_hour_window_still_bounds_a_week_about_to_reset():
    # b's week resets in an hour — but its 5-hour window is nearly
    # gone for the next four, so the week's abundance is not spendable now.
    rack = [
        _profile("a", five=20, five_reset=NOW + 4 * HOUR, week=50, week_reset=NOW + 4 * DAY),
        _profile("b", five=85, five_reset=NOW + 4 * HOUR, week=80, week_reset=NOW + HOUR),
    ]
    pick = pick_launch_profile(rack, [], NOW)
    assert pick.id == "a"
    b = next(c for c in pick.ranking if c.id == "b")
    assert b.rate == pytest.approx(0.15 * 5 / 4)


def test_window_rate_paces_floors_and_treats_a_passed_reset_as_fresh():
    assert window_rate(0.5, None, "seven_day", NOW) == 0.5  # no clock: face value
    assert window_rate(1.0, NOW + 7 * DAY, "seven_day", NOW) == pytest.approx(1.0)
    assert window_rate(0.5, NOW + 3.5 * DAY, "seven_day", NOW) == pytest.approx(1.0)
    # Minutes from the reset reads as PACE_FLOOR_S away: generous, never infinite.
    assert window_rate(0.1, NOW + 60, "seven_day", NOW) == pytest.approx(0.1 * 7 * DAY / PACE_FLOOR_S)
    assert window_rate(0.02, NOW - 1, "five_hour", NOW) == 1.0


def test_equal_scores_go_to_the_week_with_more_to_burn():
    # Both held to the same 5-hour rate; b's week would otherwise be wasted.
    rack = [
        _profile("a", five=50, five_reset=NOW + 5 * HOUR, week=10, week_reset=NOW + 6 * DAY),
        _profile("b", five=50, five_reset=NOW + 5 * HOUR, week=60, week_reset=NOW + 12 * HOUR),
    ]
    assert pick_launch_profile(rack, [], NOW).id == "b"


def test_spent_reads_face_value_however_close_the_reset():
    rack = [_profile("a", week=97, week_reset=NOW + 600), _profile("b", week=70)]
    pick = pick_launch_profile(rack, [], NOW)
    assert pick.id == "b"
    assert next(c for c in pick.ranking if c.id == "a").spent is True
