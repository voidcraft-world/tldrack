"""Every adb call the keeper makes: which binary, which argv, which timeout, and what the output means.

The argv builders and parsers are pure functions; `Adb` binds them to one binary and a `Runner`, which is the
only thing that starts a process (a test swaps in a fake). Four rules live here:

- **The token travels on stdin, never in argv.** argv is readable by every process on this Mac (`ps`), and the
  device's shell would see it too. `push_command` reads the token from `cat`'s stdin, which adb pipes from ours.
- **The whole device-side command is ONE argv element after `shell -T`.** adb appends later elements
  to it (escaped or not, by version), and the device's `sh -c` parses the result, so one pre-quoted string is
  the only form whose meaning does not depend on the adb release.
- **The push rides `shell -T`, never `exec-in`.** adb 37's `exec-in` returns the moment our stdin closes and
  tears the device side down with it: on the operator's phone (2026-11-07) the token landed in the `.tmp` and the
  rename never ran. `shell -T` (no PTY, so the bytes pass untouched) forwards stdin, waits for the device side, and
  carries its exit status. A push is still believed only after it reads back.
- **Every call has a timeout**, and a timed-out or unstartable call is a `Result`, never an exception: the
  keeper runs in the bridge's process and must not take it down.
- **What the keeper trusts is parsed, never searched.** A reverse tunnel's Mac side is read with adb's own
  socket-spec grammar (`reaches_bridge`), and a paired phone is a GUID read out of adb's pairing record
  (`parse_known_hosts`), matched exactly. A substring test there is a door: `adb` is a substring of every GUID.

`adb` is found past PATH because a bridge started outside a login shell has no Homebrew on its PATH.
"""

from __future__ import annotations

import hmac
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from phone_link.config import valid_package
from phone_link.paths import DEFAULT_APP_PACKAGE, DEVICE_TOKEN_FILE

TIMEOUT_S = 10.0
PUSH_TIMEOUT_S = 15.0


def _checked(app_package: str) -> str:
    """The package, once more proven a package name: it becomes a word of the phone's shell command."""
    if not valid_package(app_package):
        raise ValueError("not an Android package name")
    return app_package


def read_command(app_package: str) -> str:
    """Read the app's token back. Exit status, stderr and the text all come home over the shell protocol."""
    return f"run-as {_checked(app_package)} cat files/{DEVICE_TOKEN_FILE}"


def push_command(app_package: str) -> str:
    """Write it from stdin into the app's private storage: private mode, then an atomic rename, so the app never
    reads half a token."""
    return (
        f"run-as {_checked(app_package)} sh -c 'umask 077 && mkdir -p files"
        f" && cat > files/{DEVICE_TOKEN_FILE}.tmp && mv -f files/{DEVICE_TOKEN_FILE}.tmp files/{DEVICE_TOKEN_FILE}'"
    )


#: The two commands for the default app.
READ_COMMAND = read_command(DEFAULT_APP_PACKAGE)
PUSH_COMMAND = push_command(DEFAULT_APP_PACKAGE)

MODEL_COMMAND = "getprop ro.product.model"

#: Wireless debugging's connect service on mDNS, and the file adb records paired phones in.
TLS_CONNECT_SERVICE = "_adb-tls-connect._tcp"
KNOWN_HOSTS_FILE = "adb_known_hosts.pb"

_SERIAL = re.compile(r"[A-Za-z0-9._:%@\[\]-]{1,128}")
_NETWORK_SERIAL = re.compile(r".+:[0-9]{1,5}")
_FRAME_HEAD = re.compile(rb"[0-9a-fA-F]{4}")
_ADDRESS = re.compile(r"[0-9A-Za-z.:%\[\]-]+:[0-9]{1,5}")
_PORT = re.compile(r"[0-9]{1,5}")


# -- the binary ------------------------------------------------------------------------------------
def adb_candidates(env: Mapping[str, str], home: Path) -> list[Path]:
    found: list[Path] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if env.get(name):
            found.append(Path(env[name]) / "platform-tools" / "adb")
    found += [
        home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
        Path("/opt/homebrew/bin/adb"),
        Path("/usr/local/bin/adb"),
        Path("/opt/homebrew/share/android-commandlinetools/platform-tools/adb"),
    ]
    return found


def find_adb(
    env: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which, home: Path | None = None
) -> str | None:
    """PATH first, then the SDK and Homebrew locations a GUI-started process cannot see."""
    on_path = which("adb")
    if on_path:
        return on_path
    for candidate in adb_candidates(os.environ if env is None else env, home or Path.home()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# -- running it ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Runner:
    """Starts adb. `run` is one bounded call; `spawn` is the long-lived `track-devices` stream."""

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = TIMEOUT_S) -> Result:
        try:
            done = subprocess.run(
                argv,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(-1, "", f"timed out after {timeout:g}s")
        except OSError as exc:
            return Result(-1, "", f"{type(exc).__name__}: {exc.strerror or exc}")
        return Result(done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def spawn(self, argv: list[str], *, stderr: BinaryIO | int = subprocess.DEVNULL) -> subprocess.Popen:
        """`stderr` is a FILE (never a pipe): nothing reads it while the stream runs, so a pipe could fill and
        block adb. The keeper reads it once the process has ended, to say why it ended."""
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr)


class AdbError(Exception):
    """adb's output did not mean what it must (a broken track-devices frame)."""


# -- reading what it says --------------------------------------------------------------------------
@dataclass(frozen=True)
class Seen:
    serial: str
    state: str
    model: str | None
    transport: str  # "usb" | "wifi"


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frames(stream: BinaryIO) -> Iterator[str]:
    """`adb track-devices` frames: four hex digits of length, then the WHOLE device list. Ends at EOF."""
    while True:
        head = _read_exact(stream, 4)
        if head is None:
            return
        if not _FRAME_HEAD.fullmatch(head):
            raise AdbError("track-devices sent a frame without a length")
        payload = _read_exact(stream, int(head, 16))
        if payload is None:
            return
        yield payload.decode("utf-8", "replace")


def transport_of(serial: str, attrs: Mapping[str, str]) -> str:
    if "usb" in attrs:
        return "usb"
    if TLS_CONNECT_SERVICE in serial or _NETWORK_SERIAL.fullmatch(serial):
        return "wifi"
    return "usb"


def parse_devices(payload: str) -> list[Seen]:
    """One line per device: `serial state [key:value …]` (the `-l` form), or the bare `serial\\tstate`."""
    seen: list[Seen] = []
    for line in payload.splitlines():
        parts = line.split()
        if len(parts) < 2 or not _SERIAL.fullmatch(parts[0]):
            continue
        attrs = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
        model = attrs.get("model", "").replace("_", " ").strip() or None
        seen.append(Seen(parts[0], parts[1], model, transport_of(parts[0], attrs)))
    return seen


def parse_reverse_list(text: str) -> list[tuple[str, str]]:
    """`adb reverse --list` lines are `<transport> <remote> <local>`: the last two, device side first."""
    entries = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append((parts[-2], parts[-1]))
    return entries


def tcp_port(spec: str) -> int | None:
    """The port of a `tcp:[<host>:]<port>` socket spec (adb's grammar; `[::1]` brackets an IPv6 host), or None
    when `spec` is not TCP or its port is not plain digits."""
    kind, _, rest = spec.partition(":")
    if kind != "tcp":
        return None
    port = rest.rsplit(":", 1)[-1]
    return int(port) if _PORT.fullmatch(port) else None


def reaches_bridge(local: str, bridge_ports: Collection[int]) -> bool:
    """Whether a reverse tunnel whose Mac side is `local` could reach the bridge, so must not exist.

    - A TCP spec on a bridge port, whatever its host: `tcp:7682`, `tcp:localhost:7682`, `tcp:[::1]:7682`. A host
      name or address that resolves to this Mac is as good as loopback, so the host is never what decides.
    - A TCP spec whose port does not parse. Fail closed: a spelling this parser does not know is not proof that
      it misses the bridge.
    - Anything that is not TCP: `localfilesystem:`, `localabstract:`, `localreserved:` are unix sockets on this
      Mac, and tmux's own server socket is one. It drives every pane the bridge does.
    """
    kind, _, _ = local.partition(":")
    if kind != "tcp":
        return True
    port = tcp_port(local)
    return port is None or port in bridge_ports


@dataclass(frozen=True)
class Service:
    instance: str
    address: str


def parse_mdns(text: str) -> list[Service]:
    """`adb mdns services`: `<instance> <service type> <ip:port>`, wireless-debugging connect services only."""
    services = []
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) == 3
            and parts[1].rstrip(".") == TLS_CONNECT_SERVICE
            and _ADDRESS.fullmatch(parts[2])
            and _SERIAL.fullmatch(parts[0])
        ):
            services.append(Service(parts[0], parts[2]))
    return services


def _varint(data: bytes, at: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if at >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """A protobuf message's `(field number, wire type, value)`s, in order. Raises ValueError on a malformed one."""
    at = 0
    while at < len(data):
        key, at = _varint(data, at)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, at = _varint(data, at)
            yield number, wire, value
            continue
        if wire == 2:
            size, at = _varint(data, at)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError(f"wire type {wire}")
        if at + size > len(data):
            raise ValueError("truncated field")
        yield number, wire, data[at : at + size]
        at += size


def parse_known_hosts(data: bytes) -> frozenset[str]:
    """The GUIDs in adb's pairing record: `AdbKnownHosts { repeated HostInfo host_infos = 1 }`,
    `HostInfo { string guid = 1 }` (adb's `adb_known_hosts.proto`). Unparsable: nothing is paired."""
    guids: set[str] = set()
    try:
        for number, wire, host in _fields(data):
            if number != 1 or wire != 2:
                continue
            for inner, inner_wire, guid in _fields(host):  # type: ignore[arg-type]
                if inner == 1 and inner_wire == 2 and guid:
                    guids.add(guid.decode("utf-8"))  # type: ignore[union-attr]
    except ValueError:  # UnicodeDecodeError is one
        return frozenset()
    return frozenset(guids)


def known_hosts(env: Mapping[str, str] | None = None, home: Path | None = None) -> frozenset[str]:
    """The phones paired with this Mac over Wireless debugging (`adb pair`), by the GUID each one advertises.

    Used only to decide whether a phone advertising itself on the network is one of OURS, by EXACT name, the
    check adb's own auto-connect makes (`adb_wifi_is_known_host`). Connecting any other advertiser would hand
    it a tunnel to the link.
    """
    env = os.environ if env is None else env
    folder = Path(env["ANDROID_USER_HOME"]) if env.get("ANDROID_USER_HOME") else (home or Path.home()) / ".android"
    try:
        data = (folder / KNOWN_HOSTS_FILE).read_bytes()
    except OSError:
        return frozenset()
    return parse_known_hosts(data)


@dataclass(frozen=True)
class TokenRead:
    verdict: str  # match | missing | differs | no_app | release | error
    message: str | None


def scrub(text: str, secret: str | None, *, last: bool = False) -> str:
    """One line of adb's words (the first, or the `last`), short, and never the token (should a phone ever echo it)."""
    lines = [part.strip() for part in text.splitlines() if part.strip()]
    line = (lines[-1] if last else lines[0]) if lines else ""
    if secret:
        line = line.replace(secret, "[token]")
    return line[:160]


def classify_read(result: Result, token: str) -> TokenRead:
    """What `read_command` found on the phone. The text it returns is compared, then forgotten."""
    if result.ok and hmac.compare_digest(result.out.strip().encode("utf-8", "replace"), token.encode("ascii")):
        return TokenRead("match", None)
    said = f"{result.err}\n{result.out}"
    lowered = said.lower()
    if "unknown package" in lowered or "is unknown" in lowered:
        return TokenRead("no_app", None)
    if "not debuggable" in lowered:
        return TokenRead("release", "the installed app is a release build; run-as needs ./gradlew installDebug")
    if "no such file" in lowered:
        return TokenRead("missing", None)
    if result.ok:
        return TokenRead("differs", None)
    return TokenRead("error", scrub(result.err or result.out, token) or f"adb exited {result.code}")


class Adb:
    """One adb binary, every call this plugin makes with it, for one app on the phone."""

    def __init__(self, path: str, runner: Runner, app_package: str = DEFAULT_APP_PACKAGE) -> None:
        self.path = path
        self._runner = runner
        self._read = read_command(app_package)
        self._push = push_command(app_package)

    def _on(self, serial: str, *args: str) -> list[str]:
        return [self.path, "-s", serial, *args]

    def spawn_tracker(self, stderr: BinaryIO | int = subprocess.DEVNULL) -> subprocess.Popen:
        return self._runner.spawn([self.path, "track-devices", "-l"], stderr=stderr)

    def reverse_list(self, serial: str) -> Result:
        return self._runner.run(self._on(serial, "reverse", "--list"))

    def reverse(self, serial: str, remote: str, local: str) -> Result:
        return self._runner.run(self._on(serial, "reverse", remote, local))

    def reverse_remove(self, serial: str, remote: str) -> Result:
        return self._runner.run(self._on(serial, "reverse", "--remove", remote))

    def read_token(self, serial: str) -> Result:
        return self._runner.run(self._on(serial, "shell", "-T", self._read))

    def push_token(self, serial: str, token: str) -> Result:
        return self._runner.run(self._on(serial, "shell", "-T", self._push), stdin=token.encode("ascii"), timeout=PUSH_TIMEOUT_S)

    def model(self, serial: str) -> str | None:
        result = self._runner.run(self._on(serial, "shell", "-T", MODEL_COMMAND))
        text = result.out.strip() if result.ok else ""
        return text[:64] or None

    def mdns_services(self) -> list[Service]:
        result = self._runner.run([self.path, "mdns", "services"])
        return parse_mdns(result.out) if result.ok else []

    def connect(self, address: str) -> Result:
        return self._runner.run([self.path, "connect", address])
