"""The daily grade door: one push a day, only with a phone linked, enough waiting and by day (door.py)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from phone_link import door, upstream
from phone_link.state import DeviceRow, LinkState

NOON = datetime(2026, 11, 9, 12, 0)


class Bridge:
    """The two bridge routes the door calls, scripted: the laya queue's `door`, and `/notify/send`."""

    def __init__(self, waiting=7, least=5, send=(True, "sent"), queue_status=200):
        self.waiting, self.least, self.send, self.queue_status = waiting, least, send, queue_status
        self.calls: list[tuple[str, str, dict | None]] = []
        self.down = False

    def forward(self, port, method, target, body, **_kwargs):
        if self.down:
            raise upstream.BridgeDown("ConnectionRefusedError")
        self.calls.append((method, target, json.loads(body) if body is not None else None))
        if target == door.QUEUE_TARGET:
            door_block = {} if self.waiting is None else {"door": {"waiting": self.waiting, "min": self.least}}
            payload = {"ok": self.queue_status == 200, "surfaces": [{"surface": "agent_open_work", **door_block}]}
            return upstream.Reply(self.queue_status, json.dumps(payload).encode())
        assert target == door.SEND_TARGET
        return upstream.Reply(200, json.dumps({"ok": True, "sent": self.send[0], "reason": self.send[1]}).encode())

    @property
    def pushes(self):
        return [body for method, target, body in self.calls if target == door.SEND_TARGET]


def linked_state(linked=True) -> LinkState:
    state = LinkState(7683)
    state.set_listener(True, None, owner=True)
    if linked:
        state.set_devices([DeviceRow("0123456789A", "Pixel", "usb", "device", reverse=True, token="pushed", error=None)])
    return state


@pytest.fixture
def make(tmp_path):
    clock = {"now": NOON}

    def build(bridge, state=None):
        return door.Door(
            state=state or linked_state(),
            bridge_port=7682,
            directory=tmp_path / "state",
            log=lambda _line: None,
            forward=bridge.forward,
            now=lambda: clock["now"],
        )

    build.clock = clock
    build.dir = tmp_path / "state"
    return build


def test_one_push_a_day_that_opens_the_laya_screen_and_carries_only_a_count(make):
    bridge = Bridge(waiting=7)
    state = linked_state()
    gate = make(bridge, state)
    assert gate.check() == "sent"
    assert bridge.pushes == [
        {"title": "Laya: 7 to grade", "body": "RECENT on the phone · tap to open", "tags": "memo", "open": "laya_recent"}
    ]
    assert gate.check() == "today" and len(bridge.pushes) == 1
    assert state.to_json()["door"]["sent"] is True and state.to_json()["door"]["waiting"] == 7
    # A restarted bridge reads the day back; the next day asks again.
    again = make(bridge)
    assert again.check() == "today"
    make.clock["now"] = datetime(2026, 11, 10, 9, 0)
    assert again.check() == "sent" and len(bridge.pushes) == 2


@pytest.mark.parametrize(("hour", "found"), [(8, "night"), (9, "sent"), (20, "sent"), (21, "night"), (3, "night")])
def test_never_at_night(make, hour, found):
    make.clock["now"] = NOON.replace(hour=hour)
    assert make(Bridge()).check() == found


def test_no_linked_phone_no_push_and_the_day_is_not_spent(make):
    bridge = Bridge()
    for state in (linked_state(linked=False), LinkState(7683)):
        assert make(bridge, state).check() == "no_phone"
    stale = linked_state()
    stale.update_device("0123456789A", reverse=False)
    assert make(bridge, stale).check() == "no_phone"
    not_owner = linked_state()
    not_owner.set_listener(True, None, owner=False)
    assert make(bridge, not_owner).check() == "no_phone"
    assert bridge.calls == [] and not (make.dir / door.DOOR_FILE).exists()
    assert make(bridge).check() == "sent"


def test_too_few_waiting_or_no_count_is_not_an_attempt(make):
    below = Bridge(waiting=4)
    gate = make(below)
    assert gate.check() == "below" and below.pushes == []
    below.waiting = 5
    assert gate.check() == "sent"


def test_an_older_laya_plugin_or_a_refusal_carries_no_count(make, tmp_path):
    for bridge in (Bridge(waiting=None), Bridge(queue_status=503)):
        gate = door.Door(
            state=linked_state(),
            bridge_port=7682,
            directory=tmp_path / "other",
            log=lambda _line: None,
            forward=bridge.forward,
            now=lambda: NOON,
        )
        assert gate.check() == "no_count" and bridge.pushes == []
    assert door.waiting_cards(b"not json") is None
    assert door.waiting_cards(json.dumps({"ok": True, "surfaces": [{"door": {"waiting": True, "min": 5}}]}).encode()) is None
    assert door.waiting_cards(
        json.dumps({"ok": True, "surfaces": [{"door": {"waiting": 3, "min": 5}}, {"door": {"waiting": 9, "min": 5}}]}).encode()
    ) == (9, 5)


def test_a_push_the_bridge_held_back_still_spends_the_day(make):
    held = Bridge(send=(False, "budget"))
    gate = make(held)
    assert gate.check() == "budget" and gate.check() == "today" and len(held.pushes) == 1
    assert door.read_last(make.dir) == {
        "date": "2026-11-09",
        "at": door.read_last(make.dir)["at"],
        "sent": False,
        "reason": "budget",
        "waiting": 7,
    }


def test_a_bridge_that_does_not_answer_is_not_an_attempt(make):
    bridge = Bridge()
    bridge.down = True
    gate = make(bridge)
    assert gate.check() == "bridge_down" and not (make.dir / door.DOOR_FILE).exists()
    bridge.down = False
    assert gate.check() == "sent"


def test_the_day_file_is_private_and_a_broken_one_reads_as_none(make):
    make(Bridge()).check()
    path = make.dir / door.DOOR_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("{not json")
    assert door.read_last(make.dir) is None
    path.unlink()
    path.symlink_to(make.dir / "elsewhere")
    assert door.read_last(make.dir) is None, "a symlink is never followed"


def test_the_plugin_owns_one_door_and_its_state_says_what_it_last_did(tmp_path):
    from test_plugin import Context
    from phone_link.plugin import PhoneLinkPlugin

    bridge = Bridge(waiting=6)
    plugin = PhoneLinkPlugin(
        Context(),
        start=False,
        state_dir=tmp_path / "state",
        find=lambda: None,
        door_options={"forward": bridge.forward, "now": lambda: NOON},
    )
    try:
        assert plugin.state()["door"] is None, "nothing attempted yet"
        assert plugin._door.check() == "no_phone", "the plugin's own state decides: no phone is attached"
        assert bridge.calls == [] and plugin.routes() == ["/plugin/phone-link/state"], "the door adds no route"
    finally:
        plugin.close()
