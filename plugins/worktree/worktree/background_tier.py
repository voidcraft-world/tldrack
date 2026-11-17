"""The single-flight background pass the harbor's two content tiers share:
`landing.ContentTier` (proofs) and `absorption.AbsorptionTier` (evidence).

A tier's `judge` runs on the harbor runner's thread and never forks. When its
caches miss, it calls `_schedule(want)`: the latest `want` replaces any earlier
one, and at most ONE worker runs (single flight). The worker runs the
subclass's `_fill(want, started)`. A pass that answers False, or raises, backs
the tier off for `backoff_seconds`, so a wedged git costs one thread per
back-off, not one per poll. Every pass ends with `on_ready`, which invalidates
the harbor so the next poll re-judges against the filled caches.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from collections.abc import Callable

from worktree import create as create_mod

#: `create.run_bounded`'s shape, injectable so a test scripts every fork.
Runner = Callable[..., tuple[int, str, str]]


class BackgroundTier:
    #: How long a failed pass keeps the next one from starting.
    backoff_seconds: float = 60.0

    def __init__(
        self,
        repo_root: Path | str,
        *,
        start_thread: Callable[[Callable[[], None]], None],
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_ready: Callable[[], None] = lambda: None,
    ) -> None:
        self._repo_root = repo_root
        self._runner = runner
        self._start_thread = start_thread
        self._clock = clock
        self._on_ready = on_ready
        self._lock = threading.Lock()
        self._want: object | None = None
        self._running = False
        self._backoff_until = 0.0
        self.last_error: str | None = None

    def _run(self, command: list[str], timeout: int, *, input: str | None = None) -> tuple[int, str, str]:
        """Every fork goes through `create.run_bounded` BY MODULE ATTRIBUTE, so
        the plugin test fence sees it; a test hands `runner` instead."""
        run = self._runner or create_mod.run_bounded
        return run(command, timeout, input=input) if input is not None else run(command, timeout)

    def _fill(self, want, started: float) -> bool:
        """One pass. False = git failed (the tier backs off)."""
        raise NotImplementedError

    def _schedule(self, want) -> None:
        with self._lock:
            self._want = want
            if self._running or self._clock() < self._backoff_until:
                return
            self._running = True
        try:
            self._start_thread(self._work)
        except Exception as exc:  # noqa: BLE001 — thread exhaustion; the next sweep retries
            with self._lock:
                self._running = False
            self.last_error = f"could not start: {type(exc).__name__}: {exc}"[:300]

    def _work(self) -> None:
        started = self._clock()
        failed = False
        try:
            with self._lock:
                want = self._want
            if want:
                failed = not self._fill(want, started)
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the tier
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            failed = True
        finally:
            with self._lock:
                self._running = False
                if failed:
                    self._backoff_until = self._clock() + self.backoff_seconds
        self._on_ready()
