"""The dirty tier: `git status` per worktree, swept OFF every request thread and
shared by `GET /state` and the harbor.

── Why it exists ──────────────────────────────────────
`/state` used to run `git status --porcelain` for every managed worktree inline,
on the request thread, each time its 4 s cache lapsed. With dozens of trees at
100–400 ms apiece (the untracked-file scan is nearly all of it: `-uno` is
~10 ms) every recompute took 4–5 s — so EVERY poll, not just the first, outran
its client: the rack's 2.5 s `STATE_TIMEOUT_MS` and a batch launcher's 3 s
health check both read a healthy bridge as down. The harbor ran the same sweep a
second time on its own thread.

── The contract ─────────────────────────────────────────────────────────────
- `read(paths)` never runs git. It answers what the last sweep saw, `None` for a
  path no sweep has reached ("unknown, never clean" — the harbor's rule), and
  starts ONE background sweep when the answer is older than `ttl_seconds` or a
  path is new to it. `on_swept` fires when that background sweep lands, so a
  cache holding the old answer can drop it.
- `sweep_if_due(paths)` is the same sweep run INLINE, for a caller that is
  already off the request thread (the harbor runner). It never waits on a
  background sweep in flight — it answers the last map and lets that one land.
- One sweep at a time, whoever asked. The clock is read through a callable so a
  test's fake clock reaches it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

#: A sweep: `(paths, budget_seconds) -> {path: dirty | None}` — `branches.dirty_sweep`.
Sweep = Callable[[list[str], float], dict]


class DirtyTier:
    def __init__(
        self,
        *,
        sweep: Callable[[], Sweep],
        ttl_seconds: float,
        budget_seconds: float,
        start_thread: Callable[[Callable[[], None]], None],
        clock: Callable[[], float],
        on_swept: Callable[[], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        # `sweep` is a getter, not the function: tests monkeypatch
        # `worktree.branches.dirty_sweep` after the plugin is built.
        self._sweep = sweep
        self._ttl = ttl_seconds
        self._budget = budget_seconds
        self._start_thread = start_thread
        self._clock = clock
        self._on_swept = on_swept
        self._log = log
        self._lock = threading.Lock()
        self._taken_at: float | None = None
        self._dirty: dict[str, bool | None] = {}
        self._running = False

    def read(self, paths: list[str]) -> dict[str, bool | None]:
        """The last answer for `paths`; kicks a background sweep when it is due."""
        with self._lock:
            answer = {path: self._dirty.get(path) for path in paths}
            start = self._claim_if_due(paths)
        if start:
            try:
                self._start_thread(lambda: self._run(list(paths), background=True))
            except Exception:  # noqa: BLE001 — thread exhaustion: stay readable, retry next read
                with self._lock:
                    self._running = False
        return answer

    def sweep_if_due(self, paths: list[str]) -> dict[str, bool | None]:
        """`read`, but the due sweep runs here, on the caller's (background) thread."""
        with self._lock:
            start = self._claim_if_due(paths)
        if start:
            self._run(list(paths), background=False)
        with self._lock:
            return {path: self._dirty.get(path) for path in paths}

    def _claim_if_due(self, paths: list[str]) -> bool:
        if self._running:
            return False
        stale = self._taken_at is None or self._clock() - self._taken_at >= self._ttl
        unseen = any(path not in self._dirty for path in paths)
        if not (stale or unseen):
            return False
        self._running = True
        return True

    def _run(self, paths: list[str], *, background: bool) -> None:
        try:
            result = self._sweep()(paths, self._budget)
        except Exception as exc:  # noqa: BLE001 — a crashed sweep must not wedge the slot
            result = {}
            if self._log:
                self._log(f"  worktree: dirty sweep failed — {type(exc).__name__}: {exc}")
        with self._lock:
            # Stamped at the END: a slow sweep is not immediately due again.
            self._taken_at = self._clock()
            # Merged, not replaced: a path this sweep did not list keeps its
            # last answer rather than flipping back to unknown.
            self._dirty = {**self._dirty, **{path: result.get(path) for path in paths}}
            self._running = False
        if background and self._on_swept:
            self._on_swept()
