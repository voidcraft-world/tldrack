"""Account standing — is a profile's account able to run a turn at all (account_standing.py)."""

from __future__ import annotations

import json

from tldrack.account_standing import (
    AccountStandingStore,
    account_failure,
    parse_standing_file,
    serialize_standing_file,
)

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def _row(profile="claude-a", session="vc-1", error="billing_error", at=NOW - 60, message="Your plan has ended", live=True):
    failure = {"error": error, "limit": None, "resets_at": None, "message": message, "at": at}
    return {"session": session, "profile": profile, "live": live, "state": "free", "stop_failure": failure}


def test_only_the_accounts_own_errors_count():
    assert account_failure(_row()) == {"error": "billing_error", "message": "Your plan has ended", "at": NOW - 60}
    assert account_failure(_row(error="authentication_failed"))["error"] == "authentication_failed"
    for other in ("rate_limit", "overloaded", "server_error", "unknown"):
        assert account_failure(_row(error=other)) is None
    assert account_failure({"session": "vc-1", "profile": "claude-a", "live": True}) is None


def test_a_cut_off_marks_the_profile_and_a_reread_changes_nothing():
    store = AccountStandingStore(clock=Clock())
    assert store.observe([_row()]) is True
    assert store.by_profile() == {
        "claude-a": {"state": "out", "error": "billing_error", "message": "Your plan has ended", "since": NOW - 60}
    }
    # Every snapshot re-reads the same notice: not a new failure.
    assert store.observe([_row()]) is False
    # A dead row runs nothing, and a row with no profile names no account.
    assert AccountStandingStore().observe([_row(live=False), _row(profile=None)]) is False


def test_a_finished_turn_clears_it_and_the_old_notice_cannot_bring_it_back():
    clock = Clock()
    store = AccountStandingStore(clock=clock)
    store.observe([_row()])
    assert store.mark_ok("claude-a") is True
    assert store.by_profile() == {}
    # The cut-off session is still on the rack with its notice from before the good turn.
    assert store.observe([_row()]) is False
    assert store.by_profile() == {}
    # A failure AFTER the good turn is news.
    clock.now += 600
    assert store.observe([_row(session="vc-2", at=clock.now - 5)]) is True
    assert store.by_profile()["claude-a"]["since"] == clock.now - 5


def test_a_good_turn_on_a_healthy_account_is_remembered_but_not_written(tmp_path):
    path = tmp_path / "account-standing.json"
    store = AccountStandingStore(path, clock=Clock())
    assert store.mark_ok("claude-a") is False
    assert not path.exists()
    assert store.observe([_row(at=NOW - 60)]) is False  # older than the turn that just finished
    assert store.mark_ok(None) is False and store.mark_ok("Not An Id") is False


def test_a_notice_with_no_time_counts_once_from_when_it_was_first_seen():
    clock = Clock()
    store = AccountStandingStore(clock=clock)
    assert store.observe([_row(at=None)]) is True
    assert store.by_profile()["claude-a"]["since"] == NOW
    clock.now += 300
    store.mark_ok("claude-a")
    clock.now += 300
    assert store.observe([_row(at=None)]) is False  # the same notice, re-read


def test_marks_survive_a_restart(tmp_path):
    path = tmp_path / "account-standing.json"
    AccountStandingStore(path, clock=Clock()).observe([_row()])
    again = AccountStandingStore(path, clock=Clock())
    assert again.by_profile()["claude-a"]["error"] == "billing_error"
    again.mark_ok("claude-a")
    cleared = AccountStandingStore(path, clock=Clock(NOW + 10))
    assert cleared.by_profile() == {}
    assert cleared.observe([_row()]) is False  # the good turn's time came back with the file


def test_the_file_drops_one_malformed_account_alone():
    good = {"out": {"error": "billing_error", "message": None, "at": 5.0}, "ok_at": None}
    text = json.dumps(
        {
            "version": 1,
            "profiles": {
                "claude-a": good,
                "claude-b": {"out": {"error": "rate_limit", "at": 5.0}, "ok_at": None},
                "claude-c": {"out": None, "ok_at": 9.0},
                "Bad Id": good,
                "claude-d": "nope",
            },
        }
    )
    parsed = parse_standing_file(text)
    assert parsed == {"claude-a": good, "claude-c": {"out": None, "ok_at": 9.0}}
    assert parse_standing_file(serialize_standing_file(parsed)) == parsed
