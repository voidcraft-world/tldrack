"""Fakes: an in-memory adb that records every argv and stdin, and a loopback stand-in for the bridge."""

from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from phone_link.adb import Result, push_command, read_command
from phone_link.paths import DEFAULT_APP_PACKAGE, DEVICE_TOKEN_FILE


# -- adb --------------------------------------------------------------------------------------------
@dataclass
class FakePhone:
    serial: str
    state: str = "device"
    usb: bool = True
    model: str = "AB_C123D"  # as `adb devices -l` sanitises it
    getprop_model: str = "AB-C123D"  # as `getprop ro.product.model` reports it
    installed: bool = True
    #: The app that is installed, when `installed`: `run-as` answers for that package and no other.
    package: str = DEFAULT_APP_PACKAGE
    debuggable: bool = True
    token_file: str | None = None
    #: False: the push's device side fails (a full disk, a read-only data dir) and writes nothing
    writable: bool = True
    #: device side → Mac side, as `adb reverse --list` reports them
    reverses: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        attrs = (" usb:1-1" if self.usb else "") + f" product:ab1c model:{self.model} device:ab1c transport_id:3"
        return f"{self.serial}\t{self.state}{attrs}"


@dataclass
class Call:
    argv: list[str]
    stdin: bytes | None


class FakeProc:
    """A finished `track-devices`: it prints the given frames, says `stderr`, and exits with `code`."""

    def __init__(self, frames: bytes, *, code: int = 0) -> None:
        self.stdout = io.BytesIO(frames)
        self.killed = False
        self.returncode = code

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode


def frame(*lines: str) -> bytes:
    payload = "".join(f"{line}\n" for line in lines).encode()
    return f"{len(payload):04x}".encode() + payload


def _length_delimited(number: int, payload: bytes) -> bytes:
    size, encoded = len(payload), bytearray()
    while True:
        byte = size & 0x7F
        size >>= 7
        encoded.append(byte | (0x80 if size else 0))
        if not size:
            break
    return bytes([number << 3 | 2]) + bytes(encoded) + payload


def known_hosts_pb(*guids: str) -> bytes:
    """adb's `adb_known_hosts.pb` holding these GUIDs: `AdbKnownHosts { repeated HostInfo host_infos = 1 }`."""
    return b"".join(_length_delimited(1, _length_delimited(1, guid.encode())) for guid in guids)


class FakeRunner:
    """Answers the argv this plugin builds the way adb and the phone would. `calls` keeps every one."""

    def __init__(self, *phones: FakePhone, mdns: str = "") -> None:
        self.phones = {phone.serial: phone for phone in phones}
        self.calls: list[Call] = []
        self.spawned: list[list[str]] = []
        self.mdns = mdns
        self.connected: list[str] = []
        #: Each spawn takes the next: bytes for the stream, or `(bytes, exit code, stderr)`. Empty: one good list.
        self.track_frames: list = []
        self.lock = threading.Lock()

    def tracker_lines(self) -> list[str]:
        return [phone.line() for phone in self.phones.values()]

    def spawn(self, argv: list[str], *, stderr=None) -> FakeProc:
        with self.lock:
            self.spawned.append(list(argv))
            script = self.track_frames.pop(0) if self.track_frames else frame(*self.tracker_lines())
        frames, code, said = script if isinstance(script, tuple) else (script, 0, b"")
        if said and hasattr(stderr, "write"):
            stderr.write(said)
        return FakeProc(frames, code=code)

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 10.0) -> Result:
        with self.lock:
            self.calls.append(Call(list(argv), stdin))
        args = argv[1:]
        if args[:2] == ["mdns", "services"]:
            return Result(0, "List of discovered mdns services\n" + self.mdns, "")
        if args[:1] == ["connect"]:
            self.connected.append(args[1])
            return Result(0, f"connected to {args[1]}\n", "")
        assert args[0] == "-s", argv
        phone = self.phones.get(args[1])
        if phone is None:
            return Result(1, "", f"error: device '{args[1]}' not found\n")
        verb = args[2:]
        if verb == ["reverse", "--list"]:
            return Result(0, "".join(f"UsbFfs {r} {m}\n" for r, m in phone.reverses.items()), "")
        if verb[:2] == ["reverse", "--remove"]:
            phone.reverses.pop(verb[2], None)
            return Result(0, "", "")
        if verb[:1] == ["reverse"] and len(verb) == 3:
            phone.reverses[verb[1]] = verb[2]
            return Result(0, "", "")
        asked = verb[2].split(" ")[1] if verb[:2] == ["shell", "-T"] and verb[2].startswith("run-as ") else None
        if asked is not None and verb[2] == read_command(asked):
            return self._read(phone, asked)
        if asked is not None and verb[2] == push_command(asked):
            if not (phone.installed and phone.package == asked and phone.debuggable):
                return self._read(phone, asked)  # run-as refuses the push exactly as it refuses the read
            if not phone.writable or stdin is None:
                return Result(1, "", "sh: can't create files/phone-link-token.tmp: Read-only file system\n")
            phone.token_file = stdin.decode()
            return Result(0, "", "")
        if verb == ["shell", "-T", "getprop ro.product.model"]:
            return Result(0, phone.getprop_model + "\n", "")
        raise AssertionError(f"unexpected adb call {argv}")

    @staticmethod
    def _read(phone: FakePhone, asked: str) -> Result:
        if not phone.installed or phone.package != asked:
            return Result(1, "", f"run-as: unknown package: {asked}\n")
        if not phone.debuggable:
            return Result(1, "", f"run-as: package not debuggable: {asked}\n")
        if phone.token_file is None:
            return Result(1, "", f"cat: files/{DEVICE_TOKEN_FILE}: No such file or directory\n")
        return Result(0, phone.token_file, "")


# -- the bridge ---------------------------------------------------------------------------------------
class FakeBridge:
    """A loopback HTTP server that records what reaches it and answers with a scripted JSON reply."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reply_status = 200
        self.reply_body: bytes = b'{"ok":true}'
        self.reply_type = "application/json"
        self.reply_delay = 0.0
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                bridge.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                time.sleep(bridge.reply_delay)
                self.send_response(bridge.reply_status)
                self.send_header("Content-Type", bridge.reply_type)
                self.send_header("Content-Length", str(len(bridge.reply_body)))
                self.end_headers()
                self.wfile.write(bridge.reply_body)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def json_of(raw: bytes) -> dict:
    return json.loads(raw.decode())
