"""notify.py properties 6 and 7: once per wait (never on done), stuck rows, and the digest.

Kept apart from test_notify.py, which owns properties 1–5; the helpers here build a daemon whose
wall clock, digest reader and signal sink the test controls.
"""

from datetime import datetime

from tldrack.notify import (
    DEFAULT_DIGEST_HOURS,
    NOTIFY_SESSION_COOLDOWN_SECONDS,
    NotifyConfig,
    NotifyDaemon,
    digest_notice,
    digest_slot,
    parse_digest_hours,
    parse_notify_file,
    pending_notification,
    serialize_notify_file,
)
from tldrack.sinks import PRIORITY_LOW

TOPIC = "tldrack-ops-abcdefghijklmnop"


def row(session="vc-1", state="attention", age_seconds=120.0, stuck=None):
    out = {"session": session, "state": state, "age_seconds": age_seconds, "message": None}
    if stuck is not None:
        out["stuck"] = stuck
    return out


class Sink:
    name = "fake"

    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return True


def build(sessions, *, presence=0, hours=DEFAULT_DIGEST_HOURS, digest=None, wall=None):
    sink, signals = Sink(), []
    clock = {"wall": wall if wall is not None else datetime(2026, 11, 4, 10, 0).timestamp()}

    class Store:
        def get(self):
            return NotifyConfig(topic=TOPIC, digest_hours=hours)

    bridge = NotifyDaemon(
        config_store=Store(),
        read_sessions=lambda: sessions,
        read_presence_count=lambda: presence,
        sink_factory=lambda _config: sink,
        read_digest=digest,
        on_signal=lambda *args: signals.append(args),
        wall=lambda: clock["wall"],
    )
    return bridge, sink, signals, clock


# ── property 6: once per wait ────────────────────────────────────────────────


def test_a_long_wait_is_pinged_once_not_every_cooldown():
    """The bug this property fixes: a two-hour wait used to re-ping every 15 minutes."""
    bridge, sink, signals, _ = build([row()])
    assert bridge.tick(now=1000.0).should_send
    for minutes in (15, 30, 45, 120):
        assert bridge.tick(now=1000.0 + minutes * 60).reason == "episode"
    assert len(sink.published) == 1
    assert ("ping_sent", "vc-1", "wait", 1) in signals


def test_a_new_wait_after_the_old_one_ended_is_news_again():
    sessions = [row()]
    bridge, sink, _, _ = build(sessions)
    bridge.tick(now=1000.0)
    sessions[0] = row(state="busy")
    bridge.tick(now=1010.0)
    sessions[0] = row()
    assert bridge.tick(now=1000.0 + NOTIFY_SESSION_COOLDOWN_SECONDS).should_send
    assert len(sink.published) == 2


def test_the_cooldown_still_guards_between_episodes():
    sessions = [row()]
    bridge, sink, _, _ = build(sessions)
    bridge.tick(now=1000.0)
    sessions[0] = row(state="busy")
    bridge.tick(now=1010.0)
    sessions[0] = row()
    assert bridge.tick(now=1100.0).reason == "cooldown"
    assert len(sink.published) == 1


def test_a_finished_turn_is_never_a_ping():
    """Never on done: a free, background or reply-ready row is an optional review."""
    for state in ("free", "background", "busy"):
        assert (
            pending_notification(
                sessions=[row(state=state, age_seconds=9999.0)], presence_count=0, notified={}, sent_today=0, now=1000.0
            ).reason
            == "nothing-blocked"
        )


def test_a_held_ping_is_counted_once_per_episode():
    bridge, sink, signals, _ = build([row()], presence=1)
    for tick in range(5):
        assert bridge.tick(now=1000.0 + tick * 10).reason == "presence"
    assert signals.count(("ping_held", "vc-1", "presence", 1)) == 1
    assert sink.published == []


# ── stuck rows ───────────────────────────────────────────────────────────────


def test_a_stuck_row_pings_once_with_no_grace():
    bridge, sink, signals, _ = build([row(state="busy", age_seconds=1.0, stuck={"kind": "repeat_failure", "count": 4})])
    decision = bridge.tick(now=1000.0)
    assert decision.should_send and decision.notice.title == "vc-1 looks stuck"
    assert bridge.tick(now=1000.0 + NOTIFY_SESSION_COOLDOWN_SECONDS).reason == "episode"
    assert ("ping_sent", "vc-1", "stuck", 1) in signals
    assert len(sink.published) == 1


def test_waits_and_stuck_rows_coalesce_and_say_which_is_which():
    decision = pending_notification(
        sessions=[row("a"), row("b", state="busy", stuck={"kind": "no_progress", "seconds": 1300})],
        presence_count=0,
        notified={},
        sent_today=0,
        now=1000.0,
    )
    assert decision.notice.title == "2 agents need you"
    assert "a · b (stuck)" in decision.notice.body
    assert decision.notice.kinds == ("wait", "stuck")


# ── property 7: the digest ───────────────────────────────────────────────────


def test_digest_slot_is_the_newest_hour_at_or_before_now():
    at = datetime(2026, 11, 4, 16, 30).timestamp()
    day = datetime(2026, 11, 4).date().toordinal()
    assert digest_slot(at, (11, 15, 19)) == (day, 15)
    assert digest_slot(datetime(2026, 11, 4, 9, 0).timestamp(), (11, 15, 19)) == (day - 1, 19)
    assert digest_slot(at, ()) is None


def test_the_digest_is_names_and_counts_low_priority():
    notice = digest_notice(open_work=["NOTES", "vc-2"], done=["docs-voice"], parked=2)
    assert notice.title == "Digest · 5 to review"
    assert notice.body == "Open work: NOTES · vc-2\nDone: docs-voice\n2 parked (seen, not now)"
    assert notice.priority == PRIORITY_LOW
    assert digest_notice(open_work=[], done=[], parked=0) is None


def test_a_long_digest_line_folds_its_tail():
    notice = digest_notice(open_work=[f"s{i}" for i in range(7)], done=[], parked=0)
    assert notice.body == "Open work: s0 · s1 · s2 · s3 +3"


def test_the_digest_fires_once_per_slot_and_never_on_the_first_tick():
    seen = []

    def digest(since):
        seen.append(since)
        return {"open_work": ["NOTES"], "done": [], "parked": 0}

    bridge, sink, signals, clock = build([], digest=digest, wall=datetime(2026, 11, 4, 11, 5).timestamp())
    assert bridge.tick(now=1.0).reason == "nothing-blocked", "a (re)start counts from now"
    clock["wall"] = datetime(2026, 11, 4, 14, 0).timestamp()
    assert bridge.tick(now=2.0).reason == "nothing-blocked"
    clock["wall"] = datetime(2026, 11, 4, 15, 0).timestamp()
    assert bridge.tick(now=3.0).reason == "digest"
    assert bridge.tick(now=4.0).reason == "nothing-blocked", "once per slot"
    assert len(sink.published) == 1 and sink.published[0]["tags"] == "inbox_tray"
    assert ("digest_sent", None, "digest", 1) in signals
    assert seen == [datetime(2026, 11, 4, 11, 5).timestamp()]


def test_the_digest_is_skipped_on_screen_or_when_empty():
    bridge, sink, _, clock = build(
        [], presence=1, digest=lambda _: {"open_work": ["a"]}, wall=datetime(2026, 11, 4, 11, 5).timestamp()
    )
    bridge.tick(now=1.0)
    clock["wall"] = datetime(2026, 11, 4, 15, 1).timestamp()
    assert bridge.tick(now=2.0).reason == "digest-presence"

    empty, empty_sink, _, clock = build([], digest=lambda _: {}, wall=datetime(2026, 11, 4, 11, 5).timestamp())
    empty.tick(now=1.0)
    clock["wall"] = datetime(2026, 11, 4, 15, 1).timestamp()
    assert empty.tick(now=2.0).reason == "digest-empty"
    assert sink.published == [] and empty_sink.published == []


def test_a_wait_outranks_the_digest_in_one_tick():
    bridge, sink, _, clock = build([row()], digest=lambda _: {"open_work": ["a"]}, wall=datetime(2026, 11, 4, 11, 5).timestamp())
    bridge.tick(now=1.0)  # pings the wait; the digest slot is armed
    clock["wall"] = datetime(2026, 11, 4, 15, 1).timestamp()
    assert bridge.tick(now=2.0).reason == "digest"
    assert [message["tags"] for message in sink.published] == ["bell", "inbox_tray"]


def test_digest_hours_parse_and_round_trip():
    assert parse_digest_hours([19, 11, 11]) == (11, 19)
    assert parse_digest_hours([]) == (), "an empty list is how the digest is turned off"
    assert parse_digest_hours([25]) == DEFAULT_DIGEST_HOURS
    assert parse_digest_hours("noon") == DEFAULT_DIGEST_HOURS
    config = parse_notify_file('{"topic": "%s", "digest_hours": [9, 17]}' % TOPIC)
    assert config.digest_hours == (9, 17)
    assert parse_notify_file(serialize_notify_file(config)).digest_hours == (9, 17)
    assert parse_notify_file('{"topic": "%s"}' % TOPIC).digest_hours == DEFAULT_DIGEST_HOURS
