"""The keeper: every attached phone tunnelled to the link, never to the bridge, and holding the token.

Two daemon threads, both inside the bridge's process, so neither may raise out or wait unbounded: each loop body
is wrapped, and every log line goes through `logs.guarded`.

- **tracker** runs `adb track-devices -l`, a long-lived stream of whole device lists. It ends when the adb
  server restarts, which also drops every reverse tunnel, so it is restarted with backoff (`TRACK_BACKOFF_S`);
  a run that lasted `STABLE_RUN_S` resets the backoff. A run that ends before its first list is a FAILURE (the
  adb server would not start, port 5037 is taken, client and server disagree), and its exit status and last
  stderr line become `adb_error`. No adb on this Mac: the state says `missing` and the binary is looked for again
  every `ADB_RETRY_S`.
- **reconciler** runs on every new list, and every `CHECK_SECONDS` while a phone is attached. For each phone
  adb calls `device`, in this order:
  1. remove every reverse whose Mac side could reach the bridge (`adb.reaches_bridge`). Why the bridge port is
     never tunnelled: owned by plugins/phone-link/README.md — see the note there. First, and on every
     bridge, so nothing below can skip it.
  2. the phone's `tcp:7683` belongs to the bridge holding the link's lock (link_server.py), and ONLY that one
     touches it. While it also holds the port, the tunnel is armed when missing. While a stranger holds the
     port, the tunnel is removed: the phone would be talking to it, and would show UNLINKED while it does.
     A bridge without the lock (a second one, or one still starting) neither arms nor removes: removing would
     rip out the owner's live tunnel, and a removal shows up in no `track-devices` frame, so the owner would
     only notice at its next timed check.
  3. read the token back; push it on stdin when it is missing or stale, then read it back again. The lock's
     holder only, and **over USB only**: a phone attached over Wi-Fi is tunnelled, never written to
     (`USB_ONLY`). Why: owned by plugins/phone-link/README.md § Two limits — see the note there.
  At most every `MDNS_SECONDS` it also connects a phone paired over Wireless debugging that advertises itself
  on the network but is not connected: only an instance whose name EXACTLY matches a pairing record
  (`adb.known_hosts`). An address that did not attach is asked again on `CONNECT_LADDER_S` (5 s, then slower),
  and its ladder starts over once it attaches or stops advertising.

**The wireless path is the operator's nightly one**: Android turns Wireless debugging off whenever the phone
leaves its Wi-Fi, so the phone is not attached, the operator flips the tile, and the app should link within
seconds. adb's own auto-connect usually does it at once (a `track-devices` frame wakes the reconciler); the poll
here is the backstop for when it does not, so it is short while a paired phone is not attached over Wi-Fi.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from typing import BinaryIO

from phone_link.adb import (
    Adb,
    AdbError,
    Runner,
    Seen,
    classify_read,
    find_adb,
    known_hosts,
    parse_devices,
    parse_reverse_list,
    reaches_bridge,
    read_frames,
    scrub,
)
from phone_link.logs import guarded
from phone_link.paths import DEFAULT_APP_PACKAGE, LINK_SPEC
from phone_link.state import DeviceRow, LinkState, now_iso
from phone_link.token_store import TokenStore

CHECK_SECONDS = 30.0
MDNS_SECONDS = 15.0
#: Seconds until one advertised address is connected again, by how many times it was tried: quick at first (the
#: operator just flipped the tile), then slow (a phone that will not attach must not be hammered all night).
CONNECT_LADDER_S = (5.0, 15.0, 60.0, 300.0)
#: The row's `error` for a phone on Wi-Fi that does not hold the token yet.
USB_ONLY = "plug in over USB once: the token is only written over USB"
ADB_RETRY_S = 60.0
TRACK_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
STABLE_RUN_S = 60.0
KILL_WAIT_S = 2.0
JOIN_S = 2.0
#: How much of a dead tracker's stderr is read for its last line.
STDERR_TAIL_BYTES = 4096

#: The row's `error` for a phone adb sees but cannot talk to yet.
STATE_HINTS = {
    "unauthorized": "allow USB debugging on the phone (the RSA prompt)",
    "offline": "adb sees the phone offline: replug it, or turn USB debugging off and on",
    "authorizing": "waiting for the phone to authorize this Mac",
    "connecting": "connecting",
}


def _end(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _scratch() -> BinaryIO | None:
    try:
        return tempfile.TemporaryFile()
    except OSError:
        return None


def _tail(stream: BinaryIO | None) -> str:
    if stream is None:
        return ""
    try:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - STDERR_TAIL_BYTES))
        return stream.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _exit_reason(proc: subprocess.Popen, stderr: BinaryIO | None) -> str:
    """Why a track-devices ended before its first device list, in one short line."""
    code = proc.poll()
    head = f"track-devices exited {code}" if code is not None else "track-devices ended"
    said = scrub(_tail(stderr), None, last=True)
    return (f"{head}: {said}" if said else f"{head} before any device list")[:160]


class Keeper:
    def __init__(
        self,
        *,
        state: LinkState,
        tokens: TokenStore,
        bridge_ports: Iterable[int],
        log: Callable[[str], None],
        runner: Runner | None = None,
        app_package: str = DEFAULT_APP_PACKAGE,
        find: Callable[[], str | None] = find_adb,
        known: Callable[[], frozenset[str]] = known_hosts,
        clock: Callable[[], float] = time.monotonic,
        check_seconds: float = CHECK_SECONDS,
        mdns_seconds: float = MDNS_SECONDS,
        backoff: tuple[float, ...] = TRACK_BACKOFF_S,
        adb_retry_s: float = ADB_RETRY_S,
        stable_run_s: float = STABLE_RUN_S,
    ) -> None:
        self._state = state
        self._tokens = tokens
        self._bridge_ports = frozenset(port for port in bridge_ports if port)
        self._log = guarded(log)
        self._runner = runner or Runner()
        self._app_package = app_package
        self._find = find
        self._known = known
        self._clock = clock
        self._check_seconds = check_seconds
        self._mdns_seconds = mdns_seconds
        self._backoff = backoff
        self._adb_retry_s = adb_retry_s
        self._stable_run_s = stable_run_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._attached: list[Seen] = []
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._models: dict[str, str | None] = {}
        self._armed: set[str] = set()
        self._quiet: dict[str, float] = {}
        self._tries: dict[str, int] = {}
        self._next_mdns = 0.0
        self._adb_found: bool | None = None
        self.track_starts = 0
        # The link coming up, a busy port freeing, or the lock changing hands must act now, not at the next check.
        state.on_listener_change(self._wake.set)

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        for name, target in (("phone-link-tracker", self._track_loop), ("phone-link-keeper", self._reconcile_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _end(proc)
        for thread in self._threads:
            thread.join(JOIN_S)

    # -- adb ---------------------------------------------------------------------------------------
    def _resolve(self) -> Adb | None:
        path = self._find()
        found = path is not None
        if found != self._adb_found:
            if not found:
                self._state.set_adb("missing")
                self._log("phone-link: adb not found (install Android platform-tools); phones are not linked")
            else:
                self._state.set_adb("ok")
            self._adb_found = found
        return Adb(path, self._runner, self._app_package) if path is not None else None

    # -- tracker -----------------------------------------------------------------------------------
    def _track_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                adb = self._resolve()
                if adb is None:
                    self.observe([])
                    self._stop.wait(self._adb_retry_s)
                    continue
                started = self._clock()
                self._track_once(adb)
                if self._stop.is_set():
                    return
                if self._clock() - started >= self._stable_run_s:
                    attempt = 0
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                self._log(f"phone-link: adb track-devices ended; restarting in {delay:g}s")
            except Exception as exc:  # noqa: BLE001 — a dead tracker would never see a phone again
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                self._state.set_adb_error(f"tracker: {type(exc).__name__}")
                self._log(f"phone-link: the tracker failed ({type(exc).__name__}); restarting in {delay:g}s")
            attempt += 1
            self._stop.wait(delay)

    def _track_once(self, adb: Adb) -> None:
        stderr = _scratch()
        try:
            self._track_with(adb, stderr)
        finally:
            if stderr is not None:
                stderr.close()

    def _track_with(self, adb: Adb, stderr: BinaryIO | None) -> None:
        try:
            proc = adb.spawn_tracker(stderr if stderr is not None else subprocess.DEVNULL)
        except OSError as exc:
            self._state.set_adb_error(f"track-devices did not start: {type(exc).__name__}")
            return
        self.track_starts += 1
        with self._lock:
            if self._stop.is_set():
                _end(proc)
                return
            self._proc = proc
        frames = 0
        error = None
        try:
            for payload in read_frames(proc.stdout):
                frames += 1
                self._state.set_adb_error(None)
                self.observe(parse_devices(payload))
        except (AdbError, OSError, ValueError) as exc:
            error = f"track-devices: {exc}"[:160]
        finally:
            with self._lock:
                self._proc = None
            _end(proc)
        if self._stop.is_set():
            return
        if error is None and frames == 0:
            error = _exit_reason(proc, stderr)
        if error is not None:
            self._state.set_adb_error(error)

    def observe(self, seen: list[Seen]) -> None:
        """A whole device list from adb: the attached set becomes exactly this, and the reconciler wakes."""
        with self._lock:
            before = {device.serial for device in self._attached}
            self._attached = list(seen)
        after = {device.serial for device in seen}
        for device in seen:
            if device.serial not in before:
                self._log(f"phone-link: {device.serial} attached ({device.transport}, {device.state})")
        for serial in before - after:
            self._log(f"phone-link: {serial} detached")
            self._models.pop(serial, None)
            self._armed.discard(serial)
        self._state.set_devices(
            [
                DeviceRow(device.serial, self._models.get(device.serial) or device.model, device.transport, device.state)
                for device in seen
            ]
        )
        self._wake.set()

    # -- reconciler --------------------------------------------------------------------------------
    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._wake.wait(self._next_wait())
                if self._stop.is_set():
                    return
                self._wake.clear()
                self.reconcile_once()
            except Exception as exc:  # noqa: BLE001 — the keeper lives in the bridge's process
                self._log(f"phone-link: a check failed ({type(exc).__name__})")
                self._stop.wait(self._check_seconds)  # a failure that repeats must not spin

    def _next_wait(self) -> float | None:
        with self._lock:
            attached = list(self._attached)
        waits = []
        if any(device.state == "device" for device in attached):
            waits.append(self._check_seconds)
        # A paired phone that is not here over Wi-Fi may have just switched Wireless debugging on.
        if self._known() and not any(device.state == "device" and device.transport == "wifi" for device in attached):
            waits.append(self._mdns_seconds)
        return min(waits) if waits else None

    def reconcile_once(self) -> None:
        adb = self._resolve()
        if adb is None:
            return
        with self._lock:
            attached = list(self._attached)
        for device in attached:
            if self._stop.is_set():
                return
            if device.state != "device":
                hint = STATE_HINTS.get(device.state, f"adb reports the phone as {device.state}")
                self._state.update_device(device.serial, reverse=False, token="error", error=hint, checked_at=now_iso())
                continue
            try:
                self._reconcile_device(adb, device)
            except Exception as exc:  # noqa: BLE001 — one phone's failure never stops the others
                self._state.update_device(device.serial, error=f"check failed: {type(exc).__name__}", checked_at=now_iso())
        self._nudge_wireless(adb, attached)

    def _reconcile_device(self, adb: Adb, device: Seen) -> None:
        serial = device.serial
        listed = adb.reverse_list(serial)
        if not listed.ok:
            self._state.update_device(
                serial,
                reverse=False,
                checked_at=now_iso(),
                error=scrub(listed.err or listed.out, None) or "adb reverse --list failed",
            )
            return
        listener = self._state.listener()
        armed = False
        for remote, local in parse_reverse_list(listed.out):
            if reaches_bridge(local, self._bridge_ports):
                self._remove_bridge_tunnel(adb, serial, remote, local)
            elif remote == LINK_SPEC:
                if not listener.owner or listener.up:
                    # Another bridge's tunnel is reported, never touched. Ours counts only pointed at the link;
                    # pointed anywhere else, the arm below rebinds it.
                    armed = local == LINK_SPEC
                else:
                    adb.reverse_remove(serial, remote)  # we hold the link, a stranger holds the Mac's 7683
        if not listener.owner:
            self._armed.discard(serial)
            self._state.update_device(
                serial, reverse=armed, checked_at=now_iso(), error=f"the link is not served here ({listener.error or 'starting'})"
            )
            return
        error = None
        if not listener.up:
            error = f"the link is not listening ({listener.error or 'starting'})"
        elif not armed:
            made = adb.reverse(serial, LINK_SPEC, LINK_SPEC)
            armed = made.ok
            if not made.ok:
                error = f"adb reverse failed: {scrub(made.err or made.out, None) or made.code}"
        if armed and serial not in self._armed:
            self._log(f"phone-link: {serial} reverse {LINK_SPEC} {LINK_SPEC} armed")
            self._armed.add(serial)
        elif not armed:
            self._armed.discard(serial)
        if serial not in self._models:  # once per attach: `devices -l` turns `AB-C123D` into `AB_C123D`
            self._models[serial] = adb.model(serial)
        token, token_error = self._ensure_token(adb, device)
        self._state.update_device(
            serial,
            reverse=armed,
            token=token,
            error=token_error or error,
            model=self._models.get(serial) or device.model,
            checked_at=now_iso(),
        )

    def _remove_bridge_tunnel(self, adb: Adb, serial: str, remote: str, local: str) -> None:
        removed = adb.reverse_remove(serial, remote)
        if removed.ok:
            self._state.update_device(serial, bridge_reverse_removed_at=now_iso())
            self._log(
                f"phone-link: REMOVED a reverse tunnel that could reach the bridge on {serial}"
                f" ({remote} -> {local}); no tunnel may reach the bridge"
            )
        else:
            self._log(
                f"phone-link: could not remove the bridge tunnel {remote} -> {local} on {serial}:"
                f" {scrub(removed.err or removed.out, None)}"
            )

    def _ensure_token(self, adb: Adb, device: Seen) -> tuple[str, str | None]:
        """`(token, error)` for the row: `pushed` once the phone reads back exactly this Mac's token. Read on any
        transport (the read only brings the phone's bytes here), written over USB alone."""
        serial = device.serial
        token = self._tokens.value()
        if token is None:
            return "error", f"no token on this Mac ({self._tokens.error or 'unreadable'})"
        first = classify_read(adb.read_token(serial), token)
        if first.verdict == "match":
            return "pushed", None
        if first.verdict not in ("missing", "differs"):
            return _token_field(first.verdict), first.message
        if device.transport == "wifi":
            return "error", USB_ONLY
        pushed = adb.push_token(serial, token)
        second = classify_read(adb.read_token(serial), token)
        if second.verdict == "match":
            self._log(f"phone-link: token pushed to {serial}")
            return "pushed", None
        if second.verdict in ("missing", "differs"):
            return "error", scrub(pushed.err or pushed.out, token) or "the token did not read back after the push"
        return _token_field(second.verdict), second.message

    def _nudge_wireless(self, adb: Adb, attached: list[Seen]) -> None:
        now = self._clock()
        if now < self._next_mdns:
            return
        self._next_mdns = now + self._mdns_seconds
        paired = self._known() or frozenset()
        self._state.set_paired(len(paired))
        if not paired:
            return
        serials = [device.serial for device in attached]
        waiting: set[str] = set()
        for service in adb.mdns_services():
            if service.instance not in paired:  # exact: the name a pairing record holds, nothing shorter or longer
                continue
            if any(serial == service.address or serial.startswith(service.instance + ".") for serial in serials):
                continue
            waiting.add(service.address)
            if self._quiet.get(service.address, 0.0) > now:
                continue
            tries = self._tries.get(service.address, 0)
            self._tries[service.address] = tries + 1
            self._quiet[service.address] = now + CONNECT_LADDER_S[min(tries, len(CONNECT_LADDER_S) - 1)]
            result = adb.connect(service.address)
            self._log(
                f"phone-link: connecting paired phone {service.instance} at {service.address}:"
                f" {scrub(result.out or result.err, None) or result.code}"
            )
        # An address that attached, or went away, starts its ladder over: Wireless debugging takes a new port each
        # time it is switched on, so these would otherwise only ever grow.
        for address in [address for address in self._tries if address not in waiting]:
            self._tries.pop(address, None)
            self._quiet.pop(address, None)


def _token_field(verdict: str) -> str:
    return "no_app" if verdict == "no_app" else "error"
