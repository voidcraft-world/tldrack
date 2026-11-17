"""The defer wake-up: a deferred decision hides, comes back once, and keeps its block."""

import pytest

from tldrack.inbox import Inbox, InboxError
from tldrack.rack_db import RackDb

HOUR = 3600.0


class Clock:
    now = 1_900_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def inbox(tmp_path):
    clock = Clock()
    return clock, Inbox(RackDb(tmp_path / "rack.sqlite"), clock=clock)


def test_deferred_row_hides_then_returns_exactly_once(inbox):
    clock, box = inbox
    key = box.ask(source="lead", run_id="R1104-aaaa", title="Ship on Friday?", detail="", blocking=True, steps=["S2"])["key"]
    box.defer(key, clock.now + HOUR)
    clock.now += HOUR / 2
    (row,) = box.open_rows("R1104-aaaa")
    assert row["deferred"] is True
    assert box.open_rows("R1104-aaaa", include_deferred=False) == []
    assert box.due() == [], "not due yet"
    assert box.parked_steps("R1104-aaaa") == {"S2"}, "a deferral keeps the block"
    clock.now += HOUR
    (back,) = box.due()
    assert back["key"] == key and back["deferred"] is False
    assert box.due() == [], "a row comes back ONCE: one push, not one per tick"
    assert [r["key"] for r in box.open_rows("R1104-aaaa", include_deferred=False)] == [key]


def test_deferring_again_rearms_the_wake_up(inbox):
    clock, box = inbox
    key = box.ask(source="lead", title="Q", detail="", blocking=True)["key"]
    box.defer(key, clock.now + 10)
    clock.now += 20
    assert len(box.due()) == 1
    box.defer(key, clock.now + 10)
    clock.now += 20
    assert len(box.due()) == 1


@pytest.mark.parametrize("offset", [-1, 0, 31 * 24 * HOUR])
def test_defer_must_be_in_the_future_and_within_thirty_days(inbox, offset):
    clock, box = inbox
    key = box.ask(source="lead", title="Q", detail="", blocking=True)["key"]
    with pytest.raises(InboxError):
        box.defer(key, clock.now + offset)


def test_an_answered_row_cannot_be_deferred(inbox):
    clock, box = inbox
    key = box.ask(source="lead", title="Q", detail="", blocking=True)["key"]
    box.answer(key, "text", "yes")
    with pytest.raises(InboxError) as err:
        box.defer(key, clock.now + 60)
    assert err.value.status == 409
