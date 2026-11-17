import json
from datetime import datetime, timezone

import pytest

from tldrack.codex_usage import CodexUsageReader, parse_codex_usage, TAIL_BYTES
from tldrack.profiles import Profile, ProfileRegistry

NOW = 1793030400


def event(used=41, *, at=NOW, minutes=10080, reset=NOW + 86400):
    return {
        "type": "event_msg",
        "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": used, "window_minutes": minutes, "resets_at": reset},
                "secondary": None,
            },
        },
    }


def write_rollout(root, name, *rows):
    path = root / "sessions/2026/10/26" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_weekly_primary_and_two_window_plans():
    assert parse_codex_usage(event())[1] == {"seven_day": {"used_percentage": 41.0, "resets_at": NOW + 86400}}
    row = event(minutes=300)
    row["payload"]["rate_limits"]["secondary"] = event(80)["payload"]["rate_limits"]["primary"]
    assert set(parse_codex_usage(row)[1]) == {"five_hour", "seven_day"}


@pytest.mark.parametrize("used", [True, None, "41", float("nan"), float("inf"), -1, 101])
def test_invalid_percent_is_unknown(used):
    assert parse_codex_usage(event(used)) is None


def test_other_buckets_and_malformed_events_are_not_account_usage():
    for row in (
        None,
        [],
        {},
        {"type": "event_msg", "payload": []},
        event(minutes=[]),
        event(minutes=60),
        event(reset=True),
        event(reset=float("nan")),
    ):
        assert parse_codex_usage(row) is None
    row = event()
    row["payload"]["rate_limits"]["limit_id"] = "codex_other_model"
    assert parse_codex_usage(row) is None
    row = event()
    row["timestamp"] = "bad"
    assert parse_codex_usage(row) is None


def test_newest_event_wins_over_file_mtime_and_null_reports(tmp_path):
    write_rollout(tmp_path, "new", event(61, at=NOW - 10))
    path = write_rollout(
        tmp_path, "old", event(20, at=NOW - 60), {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}}
    )
    with path.open("a") as handle:
        handle.write('{"partial":')
    reader = CodexUsageReader(clock=lambda: NOW)
    view = reader.get(str(tmp_path))
    assert view["seven_day"]["used_percentage"] == 61
    assert view["age_seconds"] == 10
    view["seven_day"]["used_percentage"] = 99
    assert reader.get(str(tmp_path))["seven_day"]["used_percentage"] == 61


def test_refresh_cache_expiry_and_restart(tmp_path, monkeypatch):
    now = [NOW]
    path = write_rollout(tmp_path, "one", event(10, reset=NOW + 30))
    reader = CodexUsageReader(clock=lambda: now[0])
    assert reader.get(str(tmp_path))["seven_day"]["used_percentage"] == 10
    from tldrack import codex_usage

    original = codex_usage.read_usage_tail
    reads = []

    def tracked(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(codex_usage, "read_usage_tail", tracked)
    now[0] += 16
    reader.get(str(tmp_path))
    assert reads == []
    with path.open("a") as handle:
        handle.write(json.dumps(event(20, at=now[0], reset=NOW + 30)) + "\n")
    assert reader.get(str(tmp_path))["seven_day"]["used_percentage"] == 10
    restarted = CodexUsageReader(clock=lambda: now[0])
    assert restarted.get(str(tmp_path))["seven_day"]["used_percentage"] == 20
    now[0] += 16
    assert reader.get(str(tmp_path)) is None


def test_bounded_tail_and_account_isolation(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    path = write_rollout(a, "large", event(50))
    path.write_bytes(b"x" * (TAIL_BYTES + 1) + b"\n" + json.dumps(event(72)).encode() + b"\n")
    write_rollout(b, "other", event(5))
    reader = CodexUsageReader(clock=lambda: NOW)
    assert reader.get(str(a))["seven_day"]["used_percentage"] == 72
    assert reader.get(str(b))["seven_day"]["used_percentage"] == 5
    assert reader.get(str(tmp_path / "missing")) is None
    assert reader.get(None) is None


def test_appended_report_refreshes_and_truncation_discards_old_reading(tmp_path):
    now = [NOW]
    path = write_rollout(tmp_path, "live", event(10))
    reader = CodexUsageReader(clock=lambda: now[0])
    assert reader.get(str(tmp_path))["seven_day"]["used_percentage"] == 10
    now[0] += 16
    with path.open("a") as handle:
        handle.write(json.dumps(event(25, at=now[0])) + "\n")
    assert reader.get(str(tmp_path))["seven_day"]["used_percentage"] == 25
    now[0] += 16
    path.write_text("")
    assert reader.get(str(tmp_path)) is None


def test_profile_summary_feeds_existing_chip_contract(tmp_path):
    write_rollout(tmp_path, "codex", event(42, reset=4102444800))
    registry = ProfileRegistry(
        [
            Profile("codex", "CODEX", "codex", str(tmp_path), False),
            Profile("claude", "A", "claude", str(tmp_path / "claude"), False),
            Profile("shell", "SHELL", "shell", None, False),
        ],
        "claude",
        home=tmp_path,
        source="file",
        which=lambda _: None,
    )
    claude_usage = {"seven_day": {"used_percentage": 7}}
    rows = registry.summary(7682, {}, {"claude": claude_usage})
    assert rows[0]["usage"]["seven_day"]["used_percentage"] == 42
    assert rows[1]["usage"] == claude_usage
    assert rows[2]["usage"] is None
