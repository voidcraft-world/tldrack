"""The bridge runs `tldrack ledger ingest` on a timer, in a CHILD process.

A child, not a thread: the first backfill reads gigabytes and folds hundreds of thousands of JSON
lines, and on the bridge's own interpreter that work would hold the GIL that `/hook` — which an
agent's tool call is blocked on — needs. `nice` keeps it behind the dev server and every agent. The
same rule as freshness preflight (`freshness.py`). The child takes the ingest lock itself, so a
hand-run CLI ingest at the same moment makes this tick a no-op, not a race.

The first run waits `FIRST_DELAY_SECONDS` after startup, so a bridge restart does not start a
backfill while a supervisor is still waiting for `/snapshot`. `POST /ledger/ingest` (the rack
UI's INGEST NOW) wakes the loop early. The ledger off (no ledger directory) → never starts.

The operator's switch (features.py `LEDGER_INGEST`) stops the TIMER only. An ingest somebody asked
for still runs: a switch that also swallowed INGEST NOW would leave no way to ingest from the page.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

INTERVAL_SECONDS = 15 * 60
FIRST_DELAY_SECONDS = 90
#: A first backfill of a heavy month measured ~5 min on a laptop; the cap only stops a wedged child.
CHILD_TIMEOUT_SECONDS = 2 * 3600
NICE = "10"


def ingest_argv(python: str = sys.executable) -> list[str]:
    return ["nice", "-n", NICE, python, "-m", "tldrack.ledger.cli", "ingest", "--quiet"]


class LedgerScheduler:
    def __init__(
        self,
        root_dir: Path | None,
        *,
        interval: float = INTERVAL_SECONDS,
        first_delay: float = FIRST_DELAY_SECONDS,
        run: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        log: Callable[[str], None] | None = None,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._enabled = enabled
        self._root = root_dir
        self._interval = interval
        self._first_delay = first_delay
        self._run = run or (
            lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=CHILD_TIMEOUT_SECONDS, check=False)
        )
        self._log = log or (lambda _msg: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.child_running = False

    def start(self) -> bool:
        if self._root is None or self._thread is not None:
            return self._thread is not None
        self._thread = threading.Thread(target=self._loop, name="ledger-ingest", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request(self) -> bool:
        """Run as soon as the current child (if any) is done. False when the ledger is off."""
        if self._thread is None:
            return False
        self._wake.set()
        return True

    def _loop(self) -> None:
        asked = self._wake.wait(self._first_delay)
        while not self._stop.is_set():
            self._wake.clear()
            self.tick(asked)
            asked = self._wake.wait(self._interval)

    def tick(self, asked: bool) -> dict | None:
        """One turn of the loop. `asked` is a `request()`; the timer's own turn runs only while the
        operator's switch is on."""
        return self.run_once() if asked or self._enabled() else None

    def run_once(self) -> dict:
        started = time.time()
        with self._lock:
            self.child_running = True
        try:
            done = self._run(ingest_argv())
            result = {
                "started_at": started,
                "seconds": round(time.time() - started, 1),
                "exit": done.returncode,
                "line": (done.stdout or "").strip().splitlines()[-1:] or None,
                "error": (done.stderr or "").strip()[-300:] or None if done.returncode else None,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            result = {
                "started_at": started,
                "seconds": round(time.time() - started, 1),
                "exit": None,
                "line": None,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        finally:
            with self._lock:
                self.child_running = False
        self.last = result
        if result["exit"] != 0:
            self._log(f"  ledger: ingest failed ({result['error']})")
        return result
