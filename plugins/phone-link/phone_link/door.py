"""The daily grade door: at most ONE push a day that opens the app's LAYA screen, when grading there is worth it.

Grading from the phone only happens when the operator thinks of it. This asks once, and only when all of these hold:

- **A phone is linked.** A tap opens RECENT over this link, so a push to an unlinked phone would open a screen
  that says UNLINKED. Linked means what the keeper last found: adb's `device` state, the 7683 tunnel armed, the
  token read back (`LinkState.linked`), on the bridge that holds the link's lock.
- **Enough waits that RECENT can show.** The laya plugin counts it and owns the threshold (`door: {waiting, min}`
  on its review stats; `review_data.py` DOOR_STRATA / DOOR_MIN). This module only compares the two.
- **It is day.** Local hours `DOOR_HOURS`; a card that waited the night waits until morning.
- **Nothing was sent today.** One ATTEMPT a local day, whatever came of it (`door.json` in the link's state
  folder): a push the bridge held back (switch off, budget spent, no topic) or could not deliver is not retried
  every check, because whatever held it back still holds and each try can spend a unit of the daily budget. A day
  with no phone, too few cards or a bridge that did not answer is not an attempt.

The push itself is the bridge's: `POST /notify/send` with `open: "laya_recent"`, so the daily budget, the on/off
switch, the pointer-only text limits and the tap target all stay owned by tldrack `notify.py`
(`send_event`, `EVENT_DOORS`). The text is a count and nothing else. Both calls go to the bridge on this Mac's
loopback through `upstream.forward`, as the native caller this plugin already is. They are this plugin's own
calls: the phone's allowlist (allowlist.py) gains nothing.

One daemon thread inside the bridge's process, so the loop body cannot raise out and every log line goes through
`logs.guarded`.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from phone_link import upstream
from phone_link.logs import guarded
from phone_link.state import LinkState

#: The first look after the bridge starts, then one every `CHECK_SECONDS`.
FIRST_CHECK_SECONDS = 120.0
CHECK_SECONDS = 600.0
JOIN_S = 2.0
#: Local hours a push may go out: from the first, before the second.
DOOR_HOURS = (9, 21)
DOOR_FILE = "door.json"
DOOR_VERSION = 1
MAX_FILE_BYTES = 4096
#: The laya plugin's review stats, with no cards: every surface's `door`.
QUEUE_TARGET = "/plugin/laya/review/queue?limit=0"
SEND_TARGET = "/notify/send"
#: tldrack notify.py `EVENT_DOORS`: the Android app's LAYA screen.
OPEN_DOOR = "laya_recent"
PUSH_TAGS = "memo"
REASON_MAX = 32

Forward = Callable[..., upstream.Reply]


def push_text(waiting: int) -> tuple[str, str]:
    """(title, body): a count and where to grade it. Never a session name, never a word of any hand-off."""
    return f"Laya: {waiting} to grade", "RECENT on the phone · tap to open"


def read_last(directory: Path) -> dict | None:
    """The last attempt (`{v, date, at, waiting, sent, reason}`), or None when there is none or it does not read."""
    try:
        fd = os.open(directory / DOOR_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        found = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_FILE_BYTES else None
    except ValueError:
        return None
    if (
        not isinstance(found, dict)
        or found.get("v") != DOOR_VERSION
        or not isinstance(found.get("date"), str)
        or not isinstance(found.get("sent"), bool)
        or not isinstance(found.get("reason"), str)
    ):
        return None
    waiting = found.get("waiting")
    return {
        "date": found["date"][:10],
        "at": str(found.get("at") or "")[:40],
        "sent": found["sent"],
        "reason": found["reason"][:REASON_MAX],
        "waiting": waiting if isinstance(waiting, int) and not isinstance(waiting, bool) and waiting >= 0 else 0,
    }


def write_last(directory: Path, row: dict) -> None:
    """Replace `door.json` atomically (0600 in the link's 0700 folder). Raises OSError."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".door-")  # mkstemp creates it 0600
    try:
        os.write(fd, (json.dumps({"v": DOOR_VERSION, **row}, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, directory / DOOR_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def waiting_cards(body: bytes) -> tuple[int, int] | None:
    """(waiting, min) of the surface with the most waiting, off a `/review/queue` body; None when it carries no
    `door` (an older laya plugin, a refusal)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    surfaces = payload.get("surfaces") if isinstance(payload, dict) and payload.get("ok") is True else None
    best: tuple[int, int] | None = None
    for stats in surfaces if isinstance(surfaces, list) else ():
        door = stats.get("door") if isinstance(stats, dict) else None
        if not isinstance(door, dict):
            continue
        waiting, least = door.get("waiting"), door.get("min")
        if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (waiting, least)) or least < 1:
            continue
        if best is None or waiting > best[0]:
            best = (waiting, least)
    return best


class Door:
    def __init__(
        self,
        *,
        state: LinkState,
        bridge_port: int,
        directory: Path,
        log: Callable[[str], None],
        forward: Forward = upstream.forward,
        now: Callable[[], datetime] = datetime.now,
        first_check_s: float = FIRST_CHECK_SECONDS,
        check_s: float = CHECK_SECONDS,
    ) -> None:
        self._state = state
        self._port = bridge_port
        self._dir = Path(directory)
        self._log = guarded(log)
        self._forward = forward
        self._now = now
        self._first_check_s = first_check_s
        self._check_s = check_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict | None = read_last(self._dir)
        state.set_door(self._last)

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="phone-link-door", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(JOIN_S)

    def _loop(self) -> None:
        delay = self._first_check_s
        while not self._stop.wait(delay):
            delay = self._check_s
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 — a dead door thread would never ask again
                self._log(f"phone-link: the grade door failed ({type(exc).__name__})")

    # -- one look ----------------------------------------------------------------------------------
    def check(self) -> str:
        """One look. What it found: `today` (already attempted) · `night` · `no_phone` · `bridge_down` · `no_count`
        · `below` · else the attempt's own reason (`sent`, or why the bridge held it back)."""
        now = self._now()
        today = now.date().isoformat()
        if self._last is not None and self._last["date"] == today:
            return "today"
        if not DOOR_HOURS[0] <= now.hour < DOOR_HOURS[1]:
            return "night"
        if not self._state.linked():
            return "no_phone"
        try:
            counted = self._forward(self._port, "GET", QUEUE_TARGET, None)
        except (upstream.BridgeDown, upstream.BadReply):
            return "bridge_down"
        found = waiting_cards(counted.body) if counted.status == 200 else None
        if found is None:
            return "no_count"
        waiting, least = found
        if waiting < least:
            return "below"
        sent, reason = self._send(waiting)
        if reason == "bridge_down":
            return reason  # nothing was asked of the budget: not an attempt
        self._last = {
            "date": today,
            "at": now.astimezone().isoformat(timespec="seconds"),
            "waiting": waiting,
            "sent": sent,
            "reason": reason,
        }
        self._state.set_door(self._last)
        try:
            write_last(self._dir, self._last)
        except OSError as exc:  # kept in memory, so this bridge still asks once; a restart may ask again
            self._log(f"phone-link: cannot keep the grade door's day ({type(exc).__name__})")
        self._log(f"phone-link: grade door: {waiting} waiting → {reason}")
        return reason

    def _send(self, waiting: int) -> tuple[bool, str]:
        title, body = push_text(waiting)
        payload = json.dumps({"title": title, "body": body, "tags": PUSH_TAGS, "open": OPEN_DOOR}).encode("utf-8")
        try:
            reply = self._forward(self._port, "POST", SEND_TARGET, payload)
        except (upstream.BridgeDown, upstream.BadReply):
            return False, "bridge_down"
        try:
            answer: Any = json.loads(reply.body.decode("utf-8"))
        except ValueError:
            answer = None
        if reply.status != 200 or not isinstance(answer, dict):
            return False, f"http_{reply.status}"
        reason = answer.get("reason")
        return answer.get("sent") is True, reason[:REASON_MAX] if isinstance(reason, str) and reason else "unknown"
