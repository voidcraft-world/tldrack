import pytest

from tldrack.usage import UsageLimitsStore, parse_usage_limits


# ── parse_usage_limits ──────────────────────────────────────────────────


def test_parse_both_windows():
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41, "resets_at": 1738857600},
        }
    }
    assert parse_usage_limits(payload) == {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600.0},
        "seven_day": {"used_percentage": 41.0, "resets_at": 1738857600.0},
    }


def test_parse_five_hour_only():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 7}}}
    assert parse_usage_limits(payload) == {"five_hour": {"used_percentage": 7.0}}


def test_parse_absent_rate_limits_is_empty():
    # Non-subscriber statuslines simply lack rate_limits — valid no-op.
    assert parse_usage_limits({}) == {}
    assert parse_usage_limits({"model": {"display_name": "Opus"}}) == {}


def test_parse_extra_window_keys_dropped():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 1, "surprise": True}}}
    assert parse_usage_limits(payload) == {"five_hour": {"used_percentage": 1.0}}


def test_parse_unknown_windows_ignored():
    payload = {"rate_limits": {"monthly": {"used_percentage": 99}}}
    assert parse_usage_limits(payload) == {}


def test_parse_iso_resets_at_becomes_epoch():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": "2026-07-18T12:00:00+00:00"}}}
    parsed = parse_usage_limits(payload)
    assert parsed["five_hour"]["resets_at"] == pytest.approx(1784376000.0)


def test_parse_rate_limits_not_dict_raises():
    with pytest.raises(ValueError):
        parse_usage_limits({"rate_limits": "nope"})


def test_parse_window_not_dict_raises():
    with pytest.raises(ValueError):
        parse_usage_limits({"rate_limits": {"five_hour": 42}})


def test_parse_missing_used_percentage_raises():
    with pytest.raises(ValueError):
        parse_usage_limits({"rate_limits": {"five_hour": {"resets_at": 1}}})


def test_parse_non_numeric_used_percentage_raises():
    for bad in ("41%", True, None, [41]):
        with pytest.raises(ValueError):
            parse_usage_limits({"rate_limits": {"five_hour": {"used_percentage": bad}}})


def test_parse_bad_resets_at_raises():
    for bad in (True, "soon", [1]):
        with pytest.raises(ValueError):
            parse_usage_limits({"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": bad}}})


# ── UsageLimitsStore ────────────────────────────────────────────────────


def test_store_empty_returns_none():
    assert UsageLimitsStore().get() is None


def test_store_roundtrip_with_age():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 12.0}})
    result = store.get()
    assert result["five_hour"] == {"used_percentage": 12.0}
    assert result["age_seconds"] >= 0


def test_store_last_writer_wins():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 12.0}})
    store.set({"seven_day": {"used_percentage": 50.0}})
    result = store.get()
    assert "five_hour" not in result
    assert result["seven_day"] == {"used_percentage": 50.0}


def test_store_returns_copies():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 12.0}})
    store.get()["five_hour"]["used_percentage"] = 99.0
    assert store.get()["five_hour"]["used_percentage"] == 12.0


# ── per-account slots ─────────────────────────────────────────────────────


def test_two_accounts_no_longer_overwrite_each_other():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 10.0}}, "claude-a")
    store.set({"five_hour": {"used_percentage": 80.0}}, "claude-b")
    assert store.get("claude-a")["five_hour"]["used_percentage"] == 10.0
    assert store.get("claude-b")["five_hour"]["used_percentage"] == 80.0
    assert set(store.by_profile()) == {"claude-a", "claude-b"}


def test_the_legacy_slot_prefers_the_default_account_over_the_latest():
    # The old single-slot readers (3D bar, a native client) must stop flipping: the
    # default account wins even when another reported more recently.
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 10.0}}, "claude-a")
    store.set({"five_hour": {"used_percentage": 80.0}}, "claude-b")
    assert store.get_preferred("claude-a")["five_hour"]["used_percentage"] == 10.0


def test_the_legacy_slot_falls_back_to_the_latest_report():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 80.0}}, "claude-b")
    assert store.get_preferred("claude-a")["five_hour"]["used_percentage"] == 80.0
    assert UsageLimitsStore().get_preferred("claude-a") is None


def test_an_unattributed_report_is_not_listed_per_profile():
    store = UsageLimitsStore()
    store.set({"five_hour": {"used_percentage": 1.0}})
    assert store.by_profile() == {}
    assert store.get()["five_hour"]["used_percentage"] == 1.0


# ── persistence + reset expiry (usage.py § WHY THE PER-ACCOUNT SLOTS PERSIST) ──


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


WEEK = {"seven_day": {"used_percentage": 100.0, "resets_at": 1_100_000.0}}


def test_a_spent_account_survives_a_restart(tmp_path):
    path = tmp_path / "usage-limits.json"
    clock = Clock()
    UsageLimitsStore(path, clock=clock).set(WEEK, "claude-a")
    clock.now += 600
    reloaded = UsageLimitsStore(path, clock=clock)
    view = reloaded.by_profile()["claude-a"]
    assert view["seven_day"]["used_percentage"] == 100.0
    assert view["age_seconds"] == 600.0


def test_a_window_past_its_reset_is_dropped_live_and_on_reload(tmp_path):
    path = tmp_path / "usage-limits.json"
    clock = Clock()
    store = UsageLimitsStore(path, clock=clock)
    store.set({**WEEK, "five_hour": {"used_percentage": 40.0, "resets_at": 1_000_100.0}}, "claude-a")
    clock.now = 1_000_200.0
    assert "five_hour" not in store.get("claude-a")
    assert UsageLimitsStore(path, clock=clock).get("claude-a")["seven_day"]["used_percentage"] == 100.0
    clock.now = 1_100_001.0
    assert store.get("claude-a") is None
    assert store.by_profile() == {}
    assert UsageLimitsStore(path, clock=clock).by_profile() == {}


def test_an_unchanged_report_neither_rewrites_the_file_nor_resets_the_age(tmp_path):
    path = tmp_path / "usage-limits.json"
    clock = Clock()
    store = UsageLimitsStore(path, clock=clock)
    store.set(WEEK, "claude-a")
    written = path.stat().st_mtime_ns
    clock.now += 30
    store.set(WEEK, "claude-a")
    assert path.stat().st_mtime_ns == written
    assert store.get("claude-a")["age_seconds"] == 30.0


def test_the_unattributed_slot_is_never_written(tmp_path):
    path = tmp_path / "usage-limits.json"
    UsageLimitsStore(path).set(WEEK)
    assert not path.exists()


def test_a_corrupt_file_or_entry_is_ignored(tmp_path):
    path = tmp_path / "usage-limits.json"
    path.write_text("{nope")
    assert UsageLimitsStore(path).by_profile() == {}
    path.write_text(
        '{"version": 1, "profiles": {"Bad Id": {"windows": {}, "at": 1}, '
        '"claude-a": {"windows": {"seven_day": {"used_percentage": "x"}}, "at": 1}, '
        '"claude-c": {"windows": {"seven_day": {"used_percentage": 5}}, "at": 1}}}'
    )
    assert set(UsageLimitsStore(path).by_profile()) == {"claude-c"}
