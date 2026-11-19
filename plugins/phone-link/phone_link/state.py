"""What `GET /plugin/phone-link/state` serves: adb, the listener, each phone, and the request count.

One lock, written by the keeper and the listener, read by the route. The shape only ever gains keys; the
field list is owned by plugins/phone-link/README.md § State — see the note there. The token is not
in here and cannot be: nothing that holds it is ever handed to this module.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

STATE_VERSION = 1
#: The row's `error` before its first check has finished.
CHECKING = "checking"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class DeviceRow:
    serial: str
    model: str | None
    transport: str  # "usb" | "wifi"
    state: str  # adb's word: device | unauthorized | offline | …
    reverse: bool = False
    token: str = "error"  # "pushed" | "no_app" | "error"
    error: str | None = CHECKING
    checked_at: str | None = None
    bridge_reverse_removed_at: str | None = None


@dataclass
class _Requests:
    n: int = 0
    last_at: str | None = None
    refused: int = 0


@dataclass(frozen=True)
class Listener:
    up: bool = False
    error: str | None = None
    #: This bridge holds the link's lock (link_server.py): the one bridge on this Mac that serves 7683 and keeps
    #: each phone's 7683 tunnel. False while another bridge holds it, and before the first claim.
    owner: bool = False


class LinkState:
    def __init__(self, port: int, *, clock=now_iso, app: Mapping[str, object] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._port = port
        #: Which app the token goes to, and why a configured one was refused (config.py `LinkConfig.to_json`).
        self._app = dict(app) if app is not None else None
        self._adb = "missing"
        self._adb_error: str | None = None
        self._listener = Listener()
        self._devices: dict[str, DeviceRow] = {}
        self._requests = _Requests()
        self._door: dict | None = None
        #: Phones in adb's Wireless debugging pairing record (keeper.py reads it): 0 says "never paired".
        self._paired = 0
        self._listener_watchers: list[Callable[[], None]] = []

    def on_listener_change(self, callback: Callable[[], None]) -> None:
        """Call `callback` (outside the lock) whenever the listener comes up or goes down, or the lock changes hands."""
        with self._lock:
            self._listener_watchers.append(callback)

    # -- writers -----------------------------------------------------------------------------------
    def set_adb(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._adb, self._adb_error = status, error
            if status == "missing":
                self._devices = {}

    def set_paired(self, n: int) -> None:
        with self._lock:
            self._paired = n

    def set_adb_error(self, error: str | None) -> None:
        """Name what is failing without changing whether adb was found."""
        with self._lock:
            self._adb_error = error

    def set_listener(self, up: bool, error: str | None, *, owner: bool) -> None:
        with self._lock:
            changed = (self._listener.up, self._listener.owner) != (up, owner)
            self._listener = Listener(up, error, owner)
            watchers = list(self._listener_watchers) if changed else []
        for callback in watchers:
            callback()

    def set_devices(self, seen: list[DeviceRow]) -> None:
        """The attached set from adb. A phone still attached keeps what its last check found."""
        with self._lock:
            kept: dict[str, DeviceRow] = {}
            for row in seen:
                old = self._devices.get(row.serial)
                if old is not None and old.state == row.state:
                    old.model = row.model or old.model
                    old.transport = row.transport
                    kept[row.serial] = old
                else:
                    kept[row.serial] = row
            self._devices = kept

    def update_device(self, serial: str, **fields: object) -> None:
        with self._lock:
            row = self._devices.get(serial)
            if row is None:
                return
            for name, value in fields.items():
                setattr(row, name, value)

    def set_door(self, last: dict | None) -> None:
        """The daily grade door's last attempt (door.py owns the shape), or None before the first."""
        with self._lock:
            self._door = dict(last) if last is not None else None

    def note_request(self) -> None:
        with self._lock:
            self._requests.n += 1
            self._requests.last_at = self._clock()

    def note_refused(self) -> None:
        with self._lock:
            self._requests.refused += 1

    # -- readers -----------------------------------------------------------------------------------
    def listener(self) -> Listener:
        with self._lock:
            return self._listener

    def linked(self) -> bool:
        """Whether a phone can reach the link right now, as far as the keeper's last check knows: this bridge holds
        the link's lock and its port, and some phone in adb's `device` state has the tunnel armed and the token."""
        with self._lock:
            return (
                self._listener.up
                and self._listener.owner
                and any(row.state == "device" and row.reverse and row.token == "pushed" for row in self._devices.values())
            )

    def listener_up(self) -> bool:
        return self.listener().up

    def listener_error(self) -> str | None:
        return self.listener().error

    def to_json(self) -> dict:
        with self._lock:
            return {
                "v": STATE_VERSION,
                "adb": self._adb,
                "adb_error": self._adb_error,
                "port": self._port,
                "app": dict(self._app) if self._app is not None else None,
                "listener": asdict(self._listener),
                "devices": [asdict(row) for row in self._devices.values()],
                "requests": asdict(self._requests),
                "door": dict(self._door) if self._door is not None else None,
                "wireless": {"paired": self._paired},
            }
