"""The keeper against an in-memory adb: arm 7683, remove every bridge tunnel, push the token on stdin."""

from __future__ import annotations

import json
import stat
import sys
import time

import pytest

from fakes import FakePhone, FakeRunner
from phone_link.adb import PUSH_COMMAND, Result, Runner, parse_devices
from phone_link.keeper import Keeper
from phone_link.link_server import HELD_ELSEWHERE
from phone_link.state import LinkState
from phone_link.token_store import TokenStore

SERIAL = "0123456789A"
BRIDGE_PORTS = {7681, 7682, 7792}


class World:
    """`listening`/`owner`: the link as link_server.py reports it. Owner and listening: this bridge serves the link.
    Owner, not listening: a stranger holds the Mac's 7683. Not owner: another bridge holds the link (or this one
    has not claimed it yet)."""

    def __init__(
        self,
        tmp_path,
        *phones,
        listening=True,
        owner=None,
        listener_error=None,
        adb="/fake/adb",
        paired=frozenset(),
        mdns="",
        real_clock=False,
        runner=None,
        log=None,
        **options,
    ):
        self.runner = runner or FakeRunner(*phones, mdns=mdns)
        self.state = LinkState(7683)
        owner = listening if owner is None else owner
        error = (
            listener_error
            if listener_error is not None
            else (None if listening else ("port 7683 is in use" if owner else HELD_ELSEWHERE))
        )
        self.state.set_listener(listening, error, owner=owner)
        self.tokens = TokenStore(tmp_path / "state")
        self.token = self.tokens.value()
        self.logs: list[str] = []
        self.adb = adb
        self.paired = paired
        self.clock = [1000.0]
        self.keeper = Keeper(
            state=self.state,
            tokens=self.tokens,
            bridge_ports=BRIDGE_PORTS,
            log=log or self.logs.append,
            runner=self.runner,
            find=lambda: self.adb,
            known=lambda: self.paired,
            clock=time.monotonic if real_clock else (lambda: self.clock[0]),
            **options,
        )

    def attach(self):
        self.keeper.observe(parse_devices("\n".join(self.runner.tracker_lines())))

    def row(self, serial=SERIAL):
        return next(row for row in self.state.to_json()["devices"] if row["serial"] == serial)

    def verbs(self, serial=SERIAL):
        return [call.argv[3:] for call in self.runner.calls if call.argv[1:3] == ["-s", serial]]

    def assert_token_never_leaked(self):
        assert not any(self.token in arg for call in self.runner.calls for arg in call.argv)
        assert self.token not in json.dumps(self.state.to_json())
        assert not any(self.token in line for line in self.logs)


def test_a_new_phone_is_tunnelled_to_the_link_and_given_the_token(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL))
    world.attach()
    assert world.row()["error"] == "checking"
    world.keeper.reconcile_once()
    phone = world.runner.phones[SERIAL]
    assert phone.reverses == {"tcp:7683": "tcp:7683"}
    assert phone.token_file == world.token
    [push] = [call for call in world.runner.calls if PUSH_COMMAND in call.argv]
    assert push.argv == ["/fake/adb", "-s", SERIAL, "shell", "-T", PUSH_COMMAND]
    assert push.stdin == world.token.encode()
    row = world.row()
    assert row | {"checked_at": None} == {
        "serial": SERIAL,
        "model": "AB-C123D",
        "transport": "usb",
        "state": "device",
        "reverse": True,
        "token": "pushed",
        "error": None,
        "checked_at": None,
        "bridge_reverse_removed_at": None,
    }
    world.assert_token_never_leaked()


BRIDGE_TUNNELS = {
    "tcp:7682": "tcp:7682",
    "tcp:9000": "tcp:7682",
    "tcp:7681": "tcp:7681",
    "tcp:7792": "tcp:7792",
    "tcp:7683": "tcp:7682",
    # adb's socket spec is `tcp:[<host>:]<port>`: a host in front changes nothing
    "tcp:9001": "tcp:localhost:7682",
    "tcp:9002": "tcp:127.0.0.1:7681",
    "tcp:9003": "tcp:[::1]:7682",
    "tcp:9004": "tcp:0.0.0.0:7682",
    "tcp:9005": "tcp:mymac.local:7792",
    # fail closed: a TCP spec that does not parse, and any Mac-side socket that is not TCP (tmux's is a unix socket)
    "tcp:9006": "tcp:localhost:",
    "tcp:9007": "tcp:bridge",
    "localabstract:x": "localfilesystem:/tmp/tmux-501/default",
    "tcp:9008": "localabstract:tmux",
    "tcp:9009": "localreserved:x",
}


def test_every_tunnel_that_could_reach_the_bridge_is_removed_and_others_are_left_alone(tmp_path):
    others = {"tcp:5173": "tcp:5173", "tcp:8080": "tcp:localhost:5173", "tcp:9100": "tcp:10.0.0.5:22"}
    phone = FakePhone(SERIAL, reverses={**BRIDGE_TUNNELS, **others})
    world = World(tmp_path, phone)
    world.attach()
    world.keeper.reconcile_once()
    assert phone.reverses == {**others, "tcp:7683": "tcp:7683"}
    removed = [verb[2] for verb in world.verbs() if verb[:2] == ["reverse", "--remove"]]
    assert sorted(removed) == sorted(BRIDGE_TUNNELS)
    assert world.row()["bridge_reverse_removed_at"] is not None
    assert sum("REMOVED a reverse tunnel that could reach the bridge" in line for line in world.logs) == len(BRIDGE_TUNNELS)


def test_the_bridge_tunnel_is_removed_even_when_the_token_step_fails(tmp_path):
    phone = FakePhone(SERIAL, installed=False, reverses={"tcp:7682": "tcp:7682"})
    world = World(tmp_path, phone)
    world.attach()
    world.keeper.reconcile_once()
    assert "tcp:7682" not in phone.reverses and world.row()["token"] == "no_app"


def test_while_a_stranger_holds_the_macs_7683_the_tunnel_is_removed(tmp_path):
    """This bridge holds the link's lock, but something else took the port: the phone must not talk to it."""
    phone = FakePhone(SERIAL, reverses={"tcp:7683": "tcp:7683"})
    world = World(tmp_path, phone, listening=False, owner=True)
    world.attach()
    world.keeper.reconcile_once()
    assert phone.reverses == {}
    assert ["reverse", "tcp:7683", "tcp:7683"] not in world.verbs()
    row = world.row()
    assert row["reverse"] is False and "not listening" in row["error"] and "in use" in row["error"]


@pytest.mark.parametrize("error", [HELD_ELSEWHERE, None])
def test_a_bridge_without_the_link_never_touches_its_tunnel_or_token(tmp_path, error):
    """A second bridge (HELD_ELSEWHERE), or one that has not claimed the link yet (None), still sweeps the bridge
    tunnels, and leaves the owner's live 7683 tunnel and the token alone."""
    phone = FakePhone(SERIAL, reverses={"tcp:7683": "tcp:7683", "tcp:7682": "tcp:7682"})
    world = World(tmp_path, phone, listening=False, owner=False, listener_error=error or "")
    if error is None:
        world.state.set_listener(False, None, owner=False)
    world.attach()
    world.keeper.reconcile_once()
    assert phone.reverses == {"tcp:7683": "tcp:7683"}
    assert world.verbs() == [["reverse", "--list"], ["reverse", "--remove", "tcp:7682"]]
    assert phone.token_file is None
    row = world.row()
    assert row["reverse"] is True and "not served here" in row["error"]
    assert (error or "starting") in row["error"]


def test_a_link_tunnel_pointed_elsewhere_is_rebound_to_the_link(tmp_path):
    phone = FakePhone(SERIAL, reverses={"tcp:7683": "tcp:9999"})
    world = World(tmp_path, phone)
    world.attach()
    world.keeper.reconcile_once()
    assert phone.reverses == {"tcp:7683": "tcp:7683"}
    assert world.row()["reverse"] is True


def test_an_armed_phone_with_the_token_costs_two_reads(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL))
    world.attach()
    world.keeper.reconcile_once()
    world.runner.calls.clear()
    world.keeper.reconcile_once()
    assert world.verbs() == [["reverse", "--list"], ["shell", "-T", "run-as world.voidcraft.tldrack cat files/phone-link-token"]]
    assert world.row()["model"] == "AB-C123D"  # getprop, read once per attach
    assert world.row()["token"] == "pushed"


def test_a_stale_token_is_replaced(tmp_path):
    phone = FakePhone(SERIAL, token_file="S" * 43)
    world = World(tmp_path, phone)
    world.attach()
    world.keeper.reconcile_once()
    assert phone.token_file == world.token and world.row()["token"] == "pushed"
    world.assert_token_never_leaked()


def test_no_app_then_installed_is_retried(tmp_path):
    phone = FakePhone(SERIAL, installed=False)
    world = World(tmp_path, phone)
    world.attach()
    world.keeper.reconcile_once()
    assert world.row()["token"] == "no_app" and world.row()["reverse"] is True
    assert not any(PUSH_COMMAND in call.argv for call in world.runner.calls)
    phone.installed = True
    world.keeper.reconcile_once()
    assert world.row()["token"] == "pushed" and phone.token_file == world.token


def test_the_token_goes_to_the_configured_app_and_no_other(tmp_path):
    """A phone with only the other app reads `no_app`; the keeper never asks run-as about any other package."""
    other = "org.example.review"
    phone = FakePhone(SERIAL)  # the default app, not the configured one
    world = World(tmp_path, phone, app_package=other)
    world.attach()
    world.keeper.reconcile_once()
    assert world.row()["token"] == "no_app" and phone.token_file is None
    phone.package = other
    world.keeper.reconcile_once()
    assert world.row()["token"] == "pushed" and phone.token_file == world.token
    asked = {
        call.argv[5].split(" ")[1]
        for call in world.runner.calls
        if call.argv[3:5] == ["shell", "-T"] and call.argv[5].startswith("run-as ")
    }
    assert asked == {other}


def test_a_release_build_is_named(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL, debuggable=False))
    world.attach()
    world.keeper.reconcile_once()
    row = world.row()
    assert row["token"] == "error" and "installDebug" in row["error"]


def test_a_push_the_phone_refuses_is_an_error_in_the_phones_words(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL, writable=False))
    world.attach()
    world.keeper.reconcile_once()
    row = world.row()
    assert row["token"] == "error" and "Read-only file system" in row["error"]
    world.assert_token_never_leaked()


def test_a_push_that_says_nothing_and_does_not_read_back_is_an_error(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL))
    world.attach()
    real_run = world.runner.run

    def silent_push(argv, **kwargs):
        if PUSH_COMMAND in argv:
            return Result(0, "", "")  # the device side died before writing, and said nothing
        return real_run(argv, **kwargs)

    world.runner.run = silent_push
    world.keeper.reconcile_once()
    row = world.row()
    assert row["token"] == "error" and "did not read back" in row["error"]


def test_a_phone_adb_cannot_talk_to_yet(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL, state="unauthorized"))
    world.attach()
    world.keeper.reconcile_once()
    row = world.row()
    assert (row["reverse"], row["token"]) == (False, "error") and "allow USB debugging" in row["error"]
    assert world.verbs() == []


def test_reverse_list_failing_stops_that_phone_only(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL), FakePhone("SECOND"))
    world.attach()
    del world.runner.phones[SERIAL]  # it went away between the list and the check
    world.keeper.reconcile_once()
    assert "not found" in world.row()["error"] and world.row()["reverse"] is False
    assert world.row("SECOND")["token"] == "pushed"


def test_detaching_removes_the_row(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL))
    world.attach()
    world.keeper.reconcile_once()
    world.keeper.observe([])
    assert world.state.to_json()["devices"] == []
    assert any("detached" in line for line in world.logs)


def test_no_adb_is_a_state_not_a_crash(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL), adb=None)
    world.keeper.reconcile_once()
    assert world.state.to_json()["adb"] == "missing"
    assert world.runner.calls == []
    assert sum("adb not found" in line for line in world.logs) == 1
    world.keeper.reconcile_once()
    assert sum("adb not found" in line for line in world.logs) == 1
    world.adb = "/fake/adb"
    world.keeper.reconcile_once()
    assert world.state.to_json()["adb"] == "ok"


def test_a_wifi_phone(tmp_path):
    serial = "adb-0123456789A-x1Yz._adb-tls-connect._tcp"
    world = World(tmp_path, FakePhone(serial, usb=False))
    world.attach()
    world.keeper.reconcile_once()
    row = world.row(serial)
    assert row["transport"] == "wifi" and row["reverse"] and row["token"] == "pushed"


# -- Wireless debugging ---------------------------------------------------------------------------------
MDNS = (
    "adb-0123456789A-x1Yz\t_adb-tls-connect._tcp\t192.168.0.2:40101\n"
    "adb-STRANGER-0000\t_adb-tls-connect._tcp\t192.168.0.66:40000\n"
)


PAIRED = frozenset({"adb-0123456789A-x1Yz"})


def test_a_paired_phone_on_the_network_is_connected_and_a_stranger_is_not(tmp_path):
    world = World(tmp_path, mdns=MDNS, paired=PAIRED)
    world.keeper.reconcile_once()
    assert world.runner.connected == ["192.168.0.2:40101"]
    world.keeper.reconcile_once()  # inside MDNS_SECONDS: not asked again
    world.clock[0] += 61
    world.keeper.reconcile_once()  # past it, but inside the per-address quiet period
    assert world.runner.connected == ["192.168.0.2:40101"]
    world.clock[0] += 300
    world.keeper.reconcile_once()
    assert world.runner.connected == ["192.168.0.2:40101"] * 2


def test_a_connected_paired_phone_is_left_alone(tmp_path):
    serial = "adb-0123456789A-x1Yz._adb-tls-connect._tcp"
    world = World(tmp_path, FakePhone(serial, usb=False), mdns=MDNS, paired=PAIRED)
    world.attach()
    world.keeper.reconcile_once()
    assert world.runner.connected == []


@pytest.mark.parametrize(
    "instance",
    [
        "adb",
        "adb-",
        "a",
        "-",
        "0123",
        "adb-0123",
        "adb-0123456789A-x1Y",
        "0123456789A-x1Yz",
        "adb-0123456789A-x1Yz2",
        "xadb-0123456789A-x1Yz",
        "ADB-0123456789A-X1YZ",
    ],
)
def test_only_an_exact_pairing_name_is_connected(tmp_path, instance):
    """A substring of the pairing record is not a pairing: every GUID starts with `adb-`, so a stranger on café
    Wi-Fi advertising `adb` would otherwise be connected, tunnelled to and handed the token."""
    world = World(tmp_path, mdns=f"{instance}\t_adb-tls-connect._tcp\t192.168.0.66:40000\n", paired=PAIRED)
    world.keeper.reconcile_once()
    assert world.runner.connected == []


def test_nothing_paired_means_no_mdns_at_all(tmp_path):
    world = World(tmp_path, mdns=MDNS, paired=frozenset())
    world.keeper.reconcile_once()
    assert not any(call.argv[1:3] == ["mdns", "services"] for call in world.runner.calls)


# -- the threads ----------------------------------------------------------------------------------------
def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_track_devices_is_restarted_when_it_ends(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL), backoff=(0.01,), stable_run_s=999, real_clock=True)
    world.keeper.start()
    try:
        assert wait_for(lambda: world.keeper.track_starts >= 3)
        assert wait_for(lambda: world.runner.phones[SERIAL].token_file == world.token)
        assert wait_for(lambda: world.row()["token"] == "pushed")
        assert all(argv == ["/fake/adb", "track-devices", "-l"] for argv in world.runner.spawned)
        assert any("restarting" in line for line in world.logs)
    finally:
        world.keeper.close()
    world.assert_token_never_leaked()


def adb_error(world):
    return world.state.to_json()["adb_error"] or ""


def test_a_broken_frame_is_an_adb_error_until_a_good_list_clears_it(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL), backoff=(0.01,), stable_run_s=999, real_clock=True)
    world.runner.track_frames = [b"zzzz"] * 10_000
    world.keeper.start()
    try:
        assert wait_for(lambda: "track-devices sent a frame without a length" in adb_error(world))
        assert world.state.to_json()["adb"] == "ok"
        with world.runner.lock:
            world.runner.track_frames.clear()  # from here on, every run prints a good list
        assert wait_for(lambda: adb_error(world) == "" and world.state.to_json()["devices"])
    finally:
        world.keeper.close()


def test_a_tracker_that_dies_before_any_list_says_why(tmp_path):
    """The adb server would not start: the stream ends, cleanly and empty, with a non-zero exit. That is not
    "no phone plugged in"."""
    world = World(tmp_path, backoff=(0.01,), stable_run_s=999, real_clock=True)
    said = b"* daemon not running; starting now at tcp:5037\nerror: cannot connect to daemon\n"
    world.runner.track_frames = [(b"", 1, said)] * 10_000
    world.keeper.start()
    try:
        assert wait_for(lambda: adb_error(world) == "track-devices exited 1: error: cannot connect to daemon")
        with world.runner.lock:
            world.runner.track_frames.clear()
        assert wait_for(lambda: adb_error(world) == "")
    finally:
        world.keeper.close()


def test_a_silent_empty_exit_is_named_too(tmp_path):
    world = World(tmp_path, backoff=(0.01,), stable_run_s=999, real_clock=True)
    world.runner.track_frames = [b""] * 10_000
    world.keeper.start()
    try:
        assert wait_for(lambda: adb_error(world) == "track-devices exited 0 before any device list")
    finally:
        world.keeper.close()


class ExplodingRunner(FakeRunner):
    """`spawn` raises something no handler names, a few times, then behaves."""

    def __init__(self, *phones, failures=3):
        super().__init__(*phones)
        self.failures = failures

    def spawn(self, argv, *, stderr=None):
        if self.failures:
            self.failures -= 1
            raise TypeError("an unexpected failure")
        return super().spawn(argv, stderr=stderr)


def test_the_tracker_survives_an_exception_nobody_expected(tmp_path):
    runner = ExplodingRunner(FakePhone(SERIAL))
    world = World(tmp_path, runner=runner, backoff=(0.01,), stable_run_s=999, real_clock=True)
    world.keeper.start()
    try:
        assert wait_for(lambda: any("the tracker failed (TypeError)" in line for line in world.logs))
        assert wait_for(lambda: world.state.to_json()["devices"] and world.row()["token"] == "pushed")
    finally:
        world.keeper.close()


def test_a_log_sink_that_raises_takes_nothing_down(tmp_path):
    def broken(line):
        raise OSError("stdout is closed")

    world = World(tmp_path, FakePhone(SERIAL), log=broken, backoff=(0.01,), stable_run_s=999, real_clock=True)
    world.keeper.start()
    try:
        assert wait_for(lambda: world.keeper.track_starts >= 3)  # the tracker logs every restart
        assert wait_for(lambda: world.row()["token"] == "pushed")
        assert all(thread.is_alive() for thread in world.keeper._threads)
    finally:
        world.keeper.close()


def test_close_stops_both_threads_quickly(tmp_path):
    world = World(tmp_path, FakePhone(SERIAL), backoff=(5.0,), real_clock=True)
    world.keeper.start()
    assert wait_for(lambda: world.keeper.track_starts >= 1)
    started = time.monotonic()
    world.keeper.close()
    assert time.monotonic() - started < 3
    assert not any(thread.is_alive() for thread in world.keeper._threads)


# A real `track-devices`: one list, then it stays open, as adb's does. Every other call answers at once.
LIVE_ADB = f"""#!{sys.executable}
import sys, time
args = sys.argv[1:]
if args[:1] == ["track-devices"]:
    payload = b"{SERIAL}\\tdevice usb:1-1 model:AB_C123D\\n"
    sys.stdout.buffer.write(b"%04x" % len(payload) + payload)
    sys.stdout.flush()
    time.sleep(30)
sys.exit(0)
"""


class RecordingRunner(Runner):
    def __init__(self):
        self.procs = []

    def spawn(self, argv, *, stderr=None):
        proc = super().spawn(argv, stderr=stderr)
        self.procs.append(proc)
        return proc


def test_close_kills_a_live_track_devices_and_reaps_it(tmp_path):
    script = tmp_path / "adb"
    script.write_text(LIVE_ADB)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    runner = RecordingRunner()
    world = World(tmp_path, runner=runner, adb=str(script), real_clock=True)
    world.keeper.start()
    try:
        assert wait_for(lambda: bool(world.state.to_json()["devices"]), timeout=10.0)
        [proc] = runner.procs
        assert proc.poll() is None  # blocked in read_frames, mid-stream
    finally:
        started = time.monotonic()
        world.keeper.close()
    assert time.monotonic() - started < 3
    assert proc.poll() is not None
    assert not any(thread.is_alive() for thread in world.keeper._threads)


def test_only_the_push_has_a_stdin_and_no_call_carries_the_token_in_argv(tmp_path):
    phones = [FakePhone(SERIAL), FakePhone("B", token_file="old" * 14 + "x"), FakePhone("C", installed=False)]
    world = World(tmp_path, *phones)
    world.attach()
    for _ in range(3):
        world.keeper.reconcile_once()
    carriers = {tuple(call.argv[3:]) for call in world.runner.calls if call.stdin is not None}
    assert carriers == {("shell", "-T", PUSH_COMMAND)}
    world.assert_token_never_leaked()
