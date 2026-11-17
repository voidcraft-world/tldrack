"""Unit tests for the phone-notification policy (tldrack/notify.py).

No HTTP, no sleeping, no bridge. `pending_notification` takes `now` and every
threshold as a parameter precisely so the whole "is this worth interrupting a
human over" question can be asserted directly — the same reason `hook_state` and
`activity.py` are pure.

The five properties under test are the feature. A push that fires while the user
is looking at the screen, or three pushes for three agents, or an unbounded
stream of them, is not a smaller version of this feature — it is the annoying
thing this exists to avoid.
"""

import pytest

from tldrack.notify import (
    EVENT_BODY_MAX,
    EVENT_DOORS,
    NOTIFY_DAILY_BUDGET,
    NOTIFY_GRACE_SECONDS,
    NOTIFY_SESSION_COOLDOWN_SECONDS,
    Decision,
    NotifyConfig,
    NotifyConfigStore,
    NotifyDaemon,
    NotifyLedger,
    blocked_sessions,
    format_duration,
    generate_topic,
    parse_notify_file,
    pending_notification,
    serialize_notify_file,
)
from tldrack.sinks.ntfy import NtfySink, header_safe, valid_topic

TOPIC = "tldrack-ops-abcdefghijklmnop"


def row(session="vc-1", state="attention", age_seconds=120.0, message=None):
    """One `StatusStore.list()` row."""
    return {
        "session": session,
        "state": state,
        "age_seconds": age_seconds,
        "message": message,
        "activity": None,
        "model": None,
        "usage": None,
    }


def decide(**overrides):
    kwargs = {
        "sessions": [row()],
        "presence_count": 0,
        "notified": {},
        "sent_today": 0,
        "now": 1000.0,
    }
    kwargs.update(overrides)
    return pending_notification(**kwargs)


# ── blocked_sessions ────────────────────────────────────────────────────────


def test_only_attention_rows_count():
    """busy and free are not blocked, whatever their age."""
    sessions = [row(session="a", state="busy"), row(session="b", state="free"), row(session="c")]
    assert blocked_sessions(sessions) == [("c", 120.0)]


def test_grace_window_excludes_a_freshly_blocked_session():
    """The whole point of the grace delay: an agent that just asked something is
    noise, not signal. You are probably about to answer it."""
    assert blocked_sessions([row(age_seconds=NOTIFY_GRACE_SECONDS - 1)]) == []
    assert blocked_sessions([row(age_seconds=NOTIFY_GRACE_SECONDS)]) == [("vc-1", NOTIFY_GRACE_SECONDS)]


def test_a_background_row_is_never_blocked():
    """`background` is an agent working on its own — the opposite of waiting on
    you, however long it lasts (agent_index.py § BACKGROUND WORK)."""
    assert blocked_sessions([row(state="background", age_seconds=NOTIFY_GRACE_SECONDS * 40)]) == []


def test_never_reported_sessions_are_skipped():
    """`age_seconds: None` means the session has never reported. Unknown is not
    attention, and treating it as such would notify about every tmux session."""
    assert blocked_sessions([row(age_seconds=None)]) == []


def test_results_are_sorted_so_a_notification_body_is_stable():
    sessions = [row(session="zeta"), row(session="alpha")]
    assert [name for name, _ in blocked_sessions(sessions)] == ["alpha", "zeta"]


# ── pending_notification: the five properties ───────────────────────────────


def test_nothing_blocked_is_not_a_notification():
    assert decide(sessions=[row(state="busy")]) == Decision(None, "nothing-blocked")


def test_presence_suppresses_everything():
    """PROPERTY 2, the biggest lever. A rack surface is on screen, so the
    in-app toast and the macOS notifier already have this covered. Buzzing a
    phone about something the user is looking at is the definition of annoying.
    """
    assert decide(presence_count=1).should_send is False
    assert decide(presence_count=1).reason == "presence"


def test_a_single_blocked_agent_notifies_with_its_name_and_duration():
    decision = decide(sessions=[row(session="vc-1", age_seconds=125.0)])
    assert decision.should_send
    assert decision.notice.sessions == ("vc-1",)
    assert decision.notice.title == "vc-1 needs you"
    assert "2m" in decision.notice.body


def test_a_message_prints_the_racks_name_and_keeps_the_ledger_on_the_session():
    named = {**row(session="vc-3", age_seconds=125.0), "label": "wave-1"}
    decision = decide(sessions=[named])
    assert decision.notice.title == "wave-1 needs you"
    assert decision.notice.sessions == ("vc-3",)  # cooldown and episode are per session, not per label
    stuck = {**row(session="vc-4", state="busy"), "stuck": {"seconds": 1500.0}, "label": "export repair"}
    assert decide(sessions=[stuck]).notice.title == "export repair looks stuck"
    both = decide(sessions=[named, stuck, row(session="vc-5"), {**row(session="vc-6"), "label": "  "}])
    assert both.notice.title == "4 agents need you"
    assert "wave-1 · export repair (stuck) · vc-5 · vc-6" in both.notice.body


def test_three_blocked_agents_coalesce_into_one_notification():
    """PROPERTY 4. Three pushes for three agents reads as a malfunction even
    when every one of them is individually correct."""
    sessions = [row(session="vc-1"), row(session="vc-2"), row(session="docs")]
    decision = decide(sessions=sessions)
    assert decision.notice.title == "3 agents need you"
    assert decision.notice.sessions == ("docs", "vc-1", "vc-2")
    assert "docs · vc-1 · vc-2" in decision.notice.body


def test_the_body_reports_the_longest_wait():
    sessions = [row(session="a", age_seconds=100.0), row(session="b", age_seconds=600.0)]
    assert "10m" in decide(sessions=sessions).notice.body


def test_a_notified_session_is_not_notified_again_within_the_cooldown():
    """PROPERTY 1, edge- not level-triggered. Without this the same stuck agent
    would notify on every tick for as long as it stayed stuck."""
    notified = {"vc-1": 1000.0}
    assert decide(notified=notified, now=1000.0 + NOTIFY_SESSION_COOLDOWN_SECONDS - 1).reason == "cooldown"
    assert decide(notified=notified, now=1000.0 + NOTIFY_SESSION_COOLDOWN_SECONDS).should_send


def test_a_second_agent_still_notifies_while_the_first_is_cooling_down():
    """The cooldown is per session, not global — a NEW agent blocking is new
    information even if another one alerted a minute ago."""
    sessions = [row(session="vc-1"), row(session="vc-2")]
    decision = decide(sessions=sessions, notified={"vc-1": 1000.0}, now=1000.0)
    assert decision.notice.sessions == ("vc-2",)


def test_the_daily_budget_is_a_hard_ceiling():
    """PROPERTY 5. A runaway loop must be structurally impossible, not merely
    unlikely — this thing vibrates the user's pocket."""
    assert decide(sent_today=NOTIFY_DAILY_BUDGET).reason == "budget"
    assert decide(sent_today=NOTIFY_DAILY_BUDGET - 1).should_send


def test_presence_is_checked_before_the_budget():
    """A quiet day at the desk must not report itself as 'budget exhausted' —
    the reason code is what a future debugger reads first."""
    assert decide(presence_count=1, sent_today=NOTIFY_DAILY_BUDGET).reason == "presence"


def test_claude_message_text_never_reaches_the_notification():
    """The security boundary, asserted rather than merely documented. An
    attention row's `message` names the file or command an agent wants
    permission for, and hosted ntfy topics are readable by anyone who guesses
    the name."""
    secret = "Claude needs permission to read /Users/me/secrets/prod.env"
    decision = decide(sessions=[row(message=secret)])
    assert secret not in decision.notice.body
    assert secret not in decision.notice.title
    assert "prod.env" not in decision.notice.body


# ── format_duration ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, "0s"), (45.0, "45s"), (95.0, "1m"), (600.0, "10m"), (7200.0, "2h")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# ── NotifyLedger ────────────────────────────────────────────────────────────


def test_ledger_records_a_budget_unit_and_a_cooldown_stamp_per_session():
    ledger = NotifyLedger()
    ledger.record(("vc-1", "vc-2"), now=500.0, day=1)
    notified, sent_today = ledger.snapshot(day=1)
    assert sent_today == 1
    assert notified == {"vc-1": 500.0, "vc-2": 500.0}


def test_the_budget_resets_on_a_new_day_but_cooldowns_do_not():
    """Cooldowns are wall-time bound already; resetting them at midnight would
    let a session that alerted at 23:58 alert again three minutes later."""
    ledger = NotifyLedger()
    ledger.record(("vc-1",), now=500.0, day=1)
    notified, sent_today = ledger.snapshot(day=2)
    assert sent_today == 0
    assert notified == {"vc-1": 500.0}


def test_forget_clears_a_cooldown_so_a_recreated_name_is_not_muted():
    ledger = NotifyLedger()
    ledger.record(("vc-1",), now=500.0, day=1)
    ledger.forget("vc-1")
    assert ledger.snapshot(day=1)[0] == {}


# ── configuration ───────────────────────────────────────────────────────────


def test_setting_a_topic_is_the_opt_in():
    """Absent `enabled` means the user configured a topic and never said
    otherwise, which resolves to ON."""
    config = parse_notify_file(f'{{"version": 1, "topic": "{TOPIC}"}}')
    assert config.configured
    assert config.enabled


def test_an_explicit_false_turns_it_off_without_destroying_the_topic():
    """What the macOS toggle writes. Losing the topic on every toggle would make
    the control useless."""
    config = parse_notify_file(f'{{"version": 1, "enabled": false, "topic": "{TOPIC}"}}')
    assert config.configured
    assert config.enabled is False


def test_no_topic_means_off_whatever_enabled_says():
    assert parse_notify_file('{"version": 1, "enabled": true}').enabled is False


def test_a_guessable_topic_is_dropped_rather_than_used():
    """A short topic is brute-forceable, and on hosted ntfy the topic is the
    only secret there is. Refusing beats publishing the user's alerts."""
    assert parse_notify_file('{"version": 1, "topic": "ops"}').topic is None


def test_a_plaintext_server_is_refused():
    config = parse_notify_file(f'{{"topic": "{TOPIC}", "server": "http://ntfy.example"}}')
    assert config.server == "https://ntfy.sh"


def test_corrupt_json_raises_so_the_caller_can_warn():
    with pytest.raises(ValueError):
        parse_notify_file("not json at all")
    with pytest.raises(ValueError):
        parse_notify_file('["a", "list"]')


def test_config_round_trips_through_the_file_body():
    config = NotifyConfig(topic=TOPIC, explicit_enabled=False)
    assert parse_notify_file(serialize_notify_file(config)).enabled is False


def test_store_starts_empty_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    store = NotifyConfigStore(tmp_path / "notify.json")
    assert store.get().configured is False


def test_store_persists_the_toggle(tmp_path, monkeypatch):
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    path = tmp_path / "notify.json"
    path.write_text(f'{{"version": 1, "topic": "{TOPIC}"}}')

    store = NotifyConfigStore(path)
    assert store.get().enabled
    store.set_enabled(False)

    assert NotifyConfigStore(path).get().enabled is False


def test_env_topic_wins_over_the_file(tmp_path, monkeypatch):
    """Same precedence as plugins.py: the environment beats the config file."""
    env_topic = "env-topic-abcdefghijklmnopqrst"
    monkeypatch.setenv("TLDRACK_NTFY_TOPIC", env_topic)
    path = tmp_path / "notify.json"
    path.write_text(f'{{"version": 1, "topic": "{TOPIC}"}}')
    assert NotifyConfigStore(path).get().topic == env_topic


def test_a_guessable_env_topic_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_NTFY_TOPIC", "short")
    assert NotifyConfigStore(tmp_path / "notify.json").get().configured is False


def test_generated_topics_pass_our_own_validator_and_are_unique():
    """`--init` exists because we REFUSE short topics, and a refusal with no way
    to comply is just an obstacle. Whatever it generates must satisfy the rule it
    was written to satisfy."""
    topics = {generate_topic() for _ in range(50)}
    assert len(topics) == 50
    assert all(valid_topic(topic) for topic in topics)


def test_set_topic_refuses_a_guessable_one(tmp_path, monkeypatch):
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    store = NotifyConfigStore(tmp_path / "notify.json")
    with pytest.raises(ValueError):
        store.set_topic("ops")


def test_set_topic_persists_and_keeps_the_enabled_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    path = tmp_path / "notify.json"
    path.write_text('{"version": 1, "enabled": false}')

    fresh = generate_topic()
    NotifyConfigStore(path).set_topic(fresh)

    reloaded = NotifyConfigStore(path).get()
    assert reloaded.topic == fresh
    assert reloaded.enabled is False  # an explicit off is not undone by re-topicking


# ── the ntfy sink ───────────────────────────────────────────────────────────


def test_topic_validation_refuses_guessable_and_malformed_names():
    assert valid_topic(TOPIC)
    assert not valid_topic("ops")
    assert not valid_topic("has spaces in it and is long enough")
    assert not valid_topic(None)
    with pytest.raises(ValueError):
        NtfySink("short")


def test_header_safe_strips_crlf_so_a_value_cannot_inject_headers():
    assert header_safe("one\r\nPriority: 5", 100) == "one Priority: 5"


def test_header_safe_survives_non_ascii_without_raising():
    """urllib cannot encode these, and the whole contract of a sink is that it
    does not raise into its caller."""
    assert header_safe("café \U0001f514", 100) == "caf"


def test_sink_url_is_server_plus_topic():
    assert NtfySink(TOPIC).url == f"https://ntfy.sh/{TOPIC}"


# ── the daemon ──────────────────────────────────────────────────────────────


class FakeSink:
    name = "fake"

    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return True


class ExplodingSink:
    name = "exploding"

    def publish(self, **kwargs):
        raise RuntimeError("network is on fire")


def daemon(sessions, presence=0, config=None, sink=None):
    sink = sink or FakeSink()

    class Store:
        def get(self):
            return config or NotifyConfig(topic=TOPIC)

        def set_enabled(self, enabled):
            return self.get()

    return NotifyDaemon(
        config_store=Store(),
        read_sessions=lambda: sessions,
        read_presence_count=lambda: presence,
        sink_factory=lambda _config: sink,
    ), sink


def test_an_event_sends_once_with_its_text_flattened_and_cut():
    bridge, sink = daemon([])
    assert bridge.send_event("Batch\nrun done", "x" * 400, tags="trophy") == (True, "sent")
    sent = sink.published[0]
    assert sent["title"] == "Batch run done"
    assert len(sent["body"]) == EVENT_BODY_MAX and sent["body"].endswith("…")
    assert sent["tags"] == "trophy"
    assert bridge.state()["sent_today"] == 1


def test_an_event_ignores_presence_but_not_the_switch_or_the_budget():
    bridge, sink = daemon([], presence=3)
    assert bridge.send_event("t", "b", tags="bad tag!")[0] is True, "a terminal run is on no surface"
    assert sink.published[0]["tags"] == "checkered_flag", "an unshaped tag falls back"

    off, off_sink = daemon([], config=NotifyConfig(topic=TOPIC, explicit_enabled=False))
    assert off.send_event("t", "b") == (False, "disabled") and off_sink.published == []

    capped, capped_sink = daemon([])
    for _ in range(NOTIFY_DAILY_BUDGET):
        capped.send_event("t", "b")
    assert capped.send_event("t", "b") == (False, "budget")
    assert len(capped_sink.published) == NOTIFY_DAILY_BUDGET


def test_an_event_opens_a_named_door_never_a_url_of_the_callers():
    bridge, sink = daemon([])
    assert bridge.send_event("Laya: 7 to grade", "RECENT on the phone", tags="memo", door="laya_recent") == (True, "sent")
    assert sink.published[0]["click"] == EVENT_DOORS["laya_recent"] == "tldrack://review"
    assert bridge.send_event("t", "b", door="https://evil.example") == (False, "bad-door")
    assert len(sink.published) == 1 and bridge.state()["sent_today"] == 1, "a refused door spends nothing"
    bridge.send_event("t", "b")
    assert sink.published[1]["click"] != EVENT_DOORS["laya_recent"], "no door: the configured click, as before"


def test_an_event_survives_a_raising_sink():
    bridge, _ = daemon([], sink=ExplodingSink())
    assert bridge.send_event("t", "b") == (False, "sink-failed")


def test_tick_publishes_and_stamps_the_cooldown():
    bridge, sink = daemon([row()])
    assert bridge.tick(now=1000.0).should_send
    assert sink.published[0]["title"] == "vc-1 needs you"

    # Second tick, same still-blocked session: silent.
    assert bridge.tick(now=1001.0).reason == "cooldown"
    assert len(sink.published) == 1


def test_tick_is_a_no_op_when_disabled():
    config = NotifyConfig(topic=TOPIC, explicit_enabled=False)
    bridge, sink = daemon([row()], config=config)
    assert bridge.tick(now=1000.0).reason == "disabled"
    assert sink.published == []


def test_tick_stays_silent_while_a_rack_surface_is_on_screen():
    bridge, sink = daemon([row()], presence=2)
    assert bridge.tick(now=1000.0).reason == "presence"
    assert sink.published == []


def test_a_failing_sink_does_not_take_down_the_tick():
    """A notifier that can break its caller is worse than no notifier.

    The sink contract forbids raising, so a sink that does is buggy — but the
    tick still has to survive it, and the cooldown must already be stamped so a
    broken transport cannot become a per-tick retry storm.
    """
    bridge, _ = daemon([row()], sink=ExplodingSink())
    assert bridge.tick(now=1000.0).reason == "sink-failed"
    assert bridge.tick(now=1001.0).reason == "cooldown"


def test_send_test_bypasses_policy_but_still_spends_budget():
    """The user pressed a button and is owed an answer about the transport. It
    must not become a way around the daily ceiling."""
    bridge, sink = daemon([], presence=99)
    assert bridge.send_test() is True
    assert sink.published[0]["title"] == "tldrack test"
    assert bridge.state()["sent_today"] == 1


def test_the_daemon_polices_its_own_ledgers_budget_not_the_module_default():
    """A ledger built with a different ceiling must actually be enforced at that
    ceiling — otherwise the constructor argument is a lie."""
    sessions = [row()]
    bridge, sink = daemon(sessions)
    bridge._ledger = NotifyLedger(daily_budget=1)
    assert bridge.tick(now=1000.0).should_send
    sessions[0] = row(state="busy")  # the wait ends …
    bridge.tick(now=1001.0)
    sessions[0] = row()  # … and a new one starts past the cooldown: only the budget stands in the way
    assert bridge.tick(now=1000.0 + NOTIFY_SESSION_COOLDOWN_SECONDS).reason == "budget"
    assert len(sink.published) == 1


def test_state_never_leaks_the_topic():
    """The topic is the only secret hosted ntfy has. A UI needs to know it is
    set, never what it is."""
    bridge, _ = daemon([])
    assert TOPIC not in repr(bridge.state())
    assert bridge.state()["configured"] is True
