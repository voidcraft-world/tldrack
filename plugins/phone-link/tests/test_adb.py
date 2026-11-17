"""adb: the parsers, the argv, and — through a real subprocess — that the token crosses on stdin only."""

from __future__ import annotations

import io
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from fakes import frame, known_hosts_pb
from phone_link import adb as adb_module
from phone_link.adb import (
    PUSH_COMMAND,
    READ_COMMAND,
    Adb,
    AdbError,
    Result,
    Runner,
    classify_read,
    find_adb,
    known_hosts,
    parse_devices,
    parse_known_hosts,
    parse_mdns,
    parse_reverse_list,
    reaches_bridge,
    read_frames,
    scrub,
    tcp_port,
)

TOKEN = "Q" * 43

FAKE_ADB = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
stdin = sys.stdin.buffer.read().decode() if args[-1:] == [{PUSH_COMMAND!r}] else None
with open(os.environ["FAKE_ADB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv, "stdin": stdin}}) + "\\n")
phone_token = os.environ["FAKE_ADB_PHONE_TOKEN"]
if args[:1] == ["track-devices"]:
    payload = b"0123456789A\\tdevice usb:1-1 product:ab1c model:AB_C123D device:ab1c transport_id:3\\n"
    sys.stdout.buffer.write(b"%04x" % len(payload) + payload)
    sys.stdout.flush()
    sys.exit(0)
if args[:1] == ["slow"]:
    time.sleep(30)
if args[-1:] == [{PUSH_COMMAND!r}]:
    with open(phone_token, "w") as handle:
        handle.write(stdin)
    sys.exit(0)
if args[-1:] == [{READ_COMMAND!r}]:
    if not os.path.exists(phone_token):
        sys.stderr.write("cat: files/phone-link-token: No such file or directory\\n")
        sys.exit(1)
    sys.stdout.write(open(phone_token).read())
sys.exit(0)
"""


@pytest.fixture
def fake_adb(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "adb"
    script.parent.mkdir()
    script.write_text(FAKE_ADB)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "adb-calls.jsonl"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    monkeypatch.setenv("FAKE_ADB_PHONE_TOKEN", str(tmp_path / "phone-token"))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return str(script), calls, tmp_path / "phone-token"


# -- through a real process ---------------------------------------------------------------------------
def test_the_push_sends_the_token_on_stdin_and_never_in_argv(fake_adb):
    path, calls, phone = fake_adb
    adb = Adb(path, Runner())
    assert classify_read(adb.read_token("0123456789A"), TOKEN).verdict == "missing"
    assert adb.push_token("0123456789A", TOKEN).ok
    assert classify_read(adb.read_token("0123456789A"), TOKEN).verdict == "match"
    assert phone.read_text() == TOKEN
    recorded = calls()
    assert [call["stdin"] for call in recorded if PUSH_COMMAND in call["argv"]] == [TOKEN]
    assert not any(TOKEN in arg for call in recorded for arg in call["argv"])
    push = next(call for call in recorded if PUSH_COMMAND in call["argv"])
    assert push["argv"][1:] == ["-s", "0123456789A", "shell", "-T", PUSH_COMMAND]
    assert not any("exec-in" in call["argv"] for call in recorded), "exec-in drops the device side: adb.py"


def test_a_slow_call_times_out_into_a_result(fake_adb):
    path, _, _ = fake_adb
    started = time.monotonic()
    result = Runner().run([path, "slow"], timeout=0.3)
    assert result.code == -1 and "timed out" in result.err
    assert time.monotonic() - started < 5


def test_a_missing_binary_is_a_result_not_an_exception(tmp_path):
    result = Runner().run([str(tmp_path / "no-adb"), "devices"])
    assert result.code == -1 and "FileNotFoundError" in result.err


def test_track_devices_frames_from_a_real_process(fake_adb, tmp_path):
    path, _, _ = fake_adb
    with open(tmp_path / "stderr", "w+b") as stderr:
        proc = Adb(path, Runner()).spawn_tracker(stderr)
        try:
            [payload] = list(read_frames(proc.stdout))
        finally:
            proc.wait(5)
    [seen] = parse_devices(payload)
    assert (seen.serial, seen.state, seen.model, seen.transport) == ("0123456789A", "device", "AB C123D", "usb")


# -- the device-side commands -------------------------------------------------------------------------
def test_the_device_side_commands():
    assert READ_COMMAND == "run-as world.voidcraft.tldrack cat files/phone-link-token"
    assert PUSH_COMMAND == (
        "run-as world.voidcraft.tldrack sh -c 'umask 077 && mkdir -p files"
        " && cat > files/phone-link-token.tmp && mv -f files/phone-link-token.tmp"
        " files/phone-link-token'"
    )


# -- parsers ------------------------------------------------------------------------------------------
def test_read_frames_handles_empty_lists_and_eof():
    stream = io.BytesIO(b"0000" + frame("a\tdevice") + b"00")
    assert list(read_frames(stream)) == ["", "a\tdevice\n"]


def test_read_frames_refuses_a_frame_without_a_length():
    with pytest.raises(AdbError):
        list(read_frames(io.BytesIO(b"zzzzserial\tdevice\n")))


def test_parse_devices_both_forms_and_transports():
    payload = "\n".join(
        [
            "0123456789A            device usb:1-1 product:ab1c model:AB_C123D device:ab1c transport_id:3",
            "adb-0123456789A-x1Yz._adb-tls-connect._tcp device product:ab1c model:AB_C123D transport_id:4",
            "192.168.0.2:5555\tdevice",
            "emulator-5554\tunauthorized",
            "bad$(rm)\tdevice",
            "",
        ]
    )
    seen = parse_devices(payload)
    assert [(s.serial, s.state, s.transport) for s in seen] == [
        ("0123456789A", "device", "usb"),
        ("adb-0123456789A-x1Yz._adb-tls-connect._tcp", "device", "wifi"),
        ("192.168.0.2:5555", "device", "wifi"),
        ("emulator-5554", "unauthorized", "usb"),
    ]
    assert seen[2].model is None


def test_parse_reverse_list():
    text = "UsbFfs tcp:7683 tcp:7683\nhost-19 tcp:9000 tcp:7682\nlocalabstract:x tcp:7681\n\n"
    assert parse_reverse_list(text) == [("tcp:7683", "tcp:7683"), ("tcp:9000", "tcp:7682"), ("localabstract:x", "tcp:7681")]


@pytest.mark.parametrize(
    "spec, port",
    [
        ("tcp:7682", 7682),
        ("tcp:localhost:7682", 7682),
        ("tcp:127.0.0.1:7681", 7681),
        ("tcp:[::1]:7682", 7682),
        ("tcp:0.0.0.0:7682", 7682),
        ("tcp:", None),
        ("tcp:localhost:", None),
        ("tcp:abc", None),
        ("tcp:7682x", None),
        ("tcp:\u00b2", None),
        ("localabstract:7682", None),
        ("localfilesystem:/tmp/x", None),
    ],
)
def test_tcp_port_reads_adbs_socket_spec(spec, port):
    assert tcp_port(spec) == port


@pytest.mark.parametrize(
    "local, reaches",
    [
        ("tcp:7682", True),
        ("tcp:localhost:7682", True),
        ("tcp:[::1]:7681", True),
        ("tcp:mymac.local:7792", True),
        ("tcp:", True),
        ("tcp:localhost:", True),
        ("tcp:bridge", True),  # unparsable: closed
        ("localfilesystem:/private/tmp/tmux-501/default", True),
        ("localabstract:x", True),
        ("localreserved:x", True),
        ("tcp:7683", False),
        ("tcp:5173", False),
        ("tcp:localhost:5173", False),
        ("tcp:10.0.0.5:22", False),
    ],
)
def test_reaches_bridge_fails_closed(local, reaches):
    assert reaches_bridge(local, {7681, 7682, 7792}) is reaches


def test_parse_mdns_keeps_connect_services_only():
    text = (
        "List of discovered mdns services\n"
        "adb-0123456789A-x1Yz\t_adb-tls-connect._tcp\t192.168.0.2:40101\n"
        "adb-0123456789A-x1Yz\t_adb-tls-pairing._tcp\t192.168.0.2:40102\n"
        "adb-other\t_adb-tls-connect._tcp.\t10.0.0.9:40000\n"
        "junk line\n"
    )
    assert [(s.instance, s.address) for s in parse_mdns(text)] == [
        ("adb-0123456789A-x1Yz", "192.168.0.2:40101"),
        ("adb-other", "10.0.0.9:40000"),
    ]


@pytest.mark.parametrize(
    "result, verdict",
    [
        (Result(0, TOKEN + "\n", ""), "match"),
        (Result(0, "Z" * 43, ""), "differs"),
        (Result(0, "", ""), "differs"),
        (Result(1, "", "cat: files/phone-link-token: No such file or directory"), "missing"),
        (Result(1, "", "run-as: unknown package: world.voidcraft.tldrack"), "no_app"),
        (Result(1, "", "run-as: Package 'world.voidcraft.tldrack' is unknown"), "no_app"),
        (Result(1, "", "run-as: package not debuggable: world.voidcraft.tldrack"), "release"),
        (Result(1, "", "error: device offline"), "error"),
        (Result(-1, "", "timed out after 10s"), "error"),
    ],
)
def test_classify_read(result, verdict):
    assert classify_read(result, TOKEN).verdict == verdict


def test_scrub_never_returns_the_token():
    assert scrub(f"\n  oops {TOKEN} echoed\nsecond", TOKEN) == "oops [token] echoed"
    assert scrub(f"first\n  last {TOKEN}\n\n", TOKEN, last=True) == "last [token]"
    assert scrub("", None, last=True) == ""
    assert len(scrub("x" * 500, None)) == 160


def test_find_adb_looks_past_path(tmp_path):
    sdk = tmp_path / "sdk" / "platform-tools"
    sdk.mkdir(parents=True)
    binary = sdk / "adb"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert find_adb({"ANDROID_HOME": str(tmp_path / "sdk")}, which=lambda _: None, home=tmp_path) == str(binary)
    assert find_adb({}, which=lambda _: "/on/path/adb", home=tmp_path) == "/on/path/adb"
    nothing = find_adb({}, which=lambda _: None, home=tmp_path)
    assert nothing is None or not nothing.startswith(str(tmp_path))


def test_known_hosts_reads_the_guids_exactly(tmp_path):
    env = {"ANDROID_USER_HOME": str(tmp_path)}
    assert known_hosts(env) == frozenset()
    (tmp_path / adb_module.KNOWN_HOSTS_FILE).write_bytes(known_hosts_pb("adb-0123456789A-x1Yz", "adb-OTHER-0000"))
    paired = known_hosts(env)
    assert paired == frozenset({"adb-0123456789A-x1Yz", "adb-OTHER-0000"})
    assert "adb" not in paired and "adb-0123" not in paired  # membership, never a substring
    assert known_hosts({}, home=Path(tmp_path / "nohome")) == frozenset()


def test_parse_known_hosts_walks_the_protobuf():
    long_guid = "adb-" + "x" * 200  # a two-byte length varint
    assert parse_known_hosts(known_hosts_pb("adb-A-1", long_guid)) == frozenset({"adb-A-1", long_guid})
    assert parse_known_hosts(b"") == frozenset()
    # unknown fields are skipped: a varint field 2 at the top, a fixed64 field 3 inside a HostInfo
    host = b"\x19" + b"\x00" * 8 + b"\x0a\x07adb-B-2"
    assert parse_known_hosts(b"\x10\x05" + b"\x0a" + bytes([len(host)]) + host) == frozenset({"adb-B-2"})


@pytest.mark.parametrize(
    "data",
    [
        b"\n\x1aadb-0123456789A-x1Yz",  # a length past the end: the old test's fixture
        b"\x0a\x05\x0a\x09adb-x",  # an inner length past its message
        b"\x0b",  # wire type 3 (a group): not adb's format
        b"\x0a\x80",  # a truncated varint
        known_hosts_pb("adb-A-1") + b"\x0a\x03\x0a\x01\xff",  # a GUID that is not UTF-8
    ],
)
def test_an_unparsable_record_is_nothing_paired(data):
    assert parse_known_hosts(data) == frozenset()
