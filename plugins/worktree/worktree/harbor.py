"""The harbor's state: a persisted PR index, an in-memory fetch job, and the
cache that keeps `GET /branches` off the request thread.

── Three stores, three lifetimes ─────────────────────────────────────────────
`PrIndexStore` is on disk because it is the outcome of a NETWORK call (`gh pr
list`) — `store.py::PrStore`'s reasoning, applied to a whole index. `FetchJobs`
is in memory because its worker is a thread in THIS process — `store.py::BranchJobs`'
reasoning; a persisted `running` would describe a job a restart already
killed. `HarborCache` is in memory because everything in it is recomputable
from git in ~50 ms.

── GET never does work ───────────────────────────────────────────────────────
Plugin GETs bypass the bridge's expensive-route guard, and the client gives up
on a slow `/branches` at 2.5 s and reads it as "plugin absent". So `HarborCache
.get()` NEVER runs the sweep on the caller's thread. A fresh document is
returned; a stale or missing one is returned as-is (or as None, which the
route renders as `status: "warming"`) and ONE background refresh is scheduled
under the lock — the single-flight flag is what makes a burst of polls from
four rack surfaces cost one sweep. Contrast a cache that holds its lock
across the runner and blocks the request thread, and `plugin.py::_state`,
which releases the lock and lets a
burst stampede. A runner that RAISES keeps the last document, records why,
and backs off for a full TTL — a wedged git must cost one thread per TTL,
not one per poll.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from collections.abc import Callable

from worktree.branches import is_full_oid
from worktree.row_store import (
    BranchRowStore,
    clean_rows,
    load_store_file,
    read_rows,
)
from worktree.store import CREATE_ERROR_TTL_SECONDS

PR_INDEX_FILE_VERSION = 1

#: The two steps `POST /fetch` walks, in order: the git fetch, then the PR
#: list. The front end renders the CURRENT one as a label and its index as a
#: determinate meter, so this tuple is a contract — `agent-rack/worktree-
#: client.ts` re-declares it, and a test on each side pins the order.
FETCH_STEPS = ("fetch", "prs")

#: A failed fetch stays visible exactly as long as a failed create does, for
#: the same reason (`store.CREATE_ERROR_TTL_SECONDS`).
FETCH_ERROR_TTL_SECONDS = CREATE_ERROR_TTL_SECONDS

#: `GET /branches` shares `/state`'s pollers, so the same TTL: a normal poll
#: recomputes, a burst does not.
BRANCHES_TTL_SECONDS = 4.0

#: The dirty tier — `git status --porcelain` per worktree — is the only
#: expensive part of a sweep (100–400 ms a tree, so seconds for a few dozen
#: trees). One tier serves the harbor AND `/state` (`dirty.py` owns why),
#: on its own, longer TTL: four rack beats of forks per twenty seconds instead
#: of per four.
DIRTY_TTL_SECONDS = 20.0

#: And its own budget per sweep, so a cold disk cannot hold the single-flight
#: slot for a minute. `branches.dirty_sweep` leaves what it did not reach None.
#: Sized for the whole rack: the sweep never runs on a request thread.
DIRTY_SWEEP_BUDGET_SECONDS = 15.0

PR_STATES = ("open", "merged", "closed")

#: The PR index refreshes ITSELF (`plugin._maybe_refresh_prs`) once it is
#: this old, while the harbor is being polled. It used to refresh only on the
#: harbor's FETCH button; every `git pull` from a terminal left it alone, and
#: a month-old index read most "unlanded" branches wrong: they were
#: squash-merged PRs it had never heard of. One `gh pr list` per ten minutes
#: of looking is nothing against GitHub's rate limit.
PR_INDEX_TTL_SECONDS = 600.0

#: A terminal's `git fetch` (FETCH_HEAD newer than the index) also triggers a
#: refresh — but not more often than this, since agents fetch constantly.
PR_INDEX_MIN_INTERVAL_SECONDS = 60.0

#: Past this age (or with an error recorded) `pr_index.stale` is true and the
#: panel says so — the readout that would have caught the month-old index.
PR_INDEX_STALE_SECONDS = 3600.0

#: Merged-PR heads this repo never received, fetched by id per refresh. A cap,
#: so one refresh is one bounded network read; the next refresh takes the rest.
PR_FETCH_HEADS_MAX = 20


def _index_row(_branch: str, row: dict) -> dict | None:
    number, state = row.get("number"), row.get("state")
    if not isinstance(number, int) or state not in PR_STATES:
        return None
    return {
        "number": number,
        "state": state,
        "url": row.get("url") if isinstance(row.get("url"), str) else None,
        "draft": row.get("draft") is True,
        "merged_at": row.get("merged_at") if isinstance(row.get("merged_at"), str) else None,
        "updated_at": row.get("updated_at") if isinstance(row.get("updated_at"), str) else "",
        # Absent from an index written before the field existed: `landed` then
        # refuses every PR-only verdict until the next FETCH rewrites it.
        "head_oid": row.get("head_oid") if is_full_oid(row.get("head_oid")) else None,
        # Same rule for `base_ref`: absent → `branches.landed` refuses with a base.
        "base_ref": row.get("base_ref") if isinstance(row.get("base_ref"), str) else None,
    }


def parse_index_file(text: str) -> tuple[float | None, str | None, bool, dict[str, dict]]:
    """File body → `(fetched_at, error, truncated, {headRefName: row})`. Bad
    rows are dropped; a wrong ENVELOPE raises ValueError so the caller can warn.
    `truncated` absent (an index written before it existed) reads False."""
    envelope, rows = read_rows(text, "prs")
    fetched_at = envelope.get("fetched_at")
    fetched_at = float(fetched_at) if isinstance(fetched_at, (int, float)) else None
    error = envelope.get("error") if isinstance(envelope.get("error"), str) else None
    return fetched_at, error, envelope.get("truncated") is True, clean_rows(rows, _index_row)


class PrIndexStore(BranchRowStore):
    """Thread-safe `{headRefName: {number, state, url, draft, merged_at,
    updated_at, head_oid, base_ref}}` plus when it was fetched and why it last could not be.

    `replace` is the only way rows change here — the index is a snapshot of one
    `gh pr list`, never edited a row at a time, so the base's per-row `drop`
    goes unused. `set_error` leaves the rows
    alone: a `gh` that is missing today does not make yesterday's index wrong,
    it makes it OLD, and `fetched_at` already says so. `truncated` = the list
    that built it filled `branches.PR_LIST_CEILING`, so its oldest PRs may be
    missing (that constant owns why it matters).

    `get` and the load-and-write plumbing are `BranchRowStore`'s; the envelope
    carries two fields of its own, so `_load` and `snapshot` are widened here.
    """

    _kept = "PR index"

    def __init__(self, path: Path) -> None:
        # Set before the base's __init__, which loads the file over them.
        self._fetched_at: float | None = None
        self._error: str | None = None
        self._truncated = False
        super().__init__(path)

    def _load(self) -> None:
        self._fetched_at, self._error, self._truncated, self._rows = load_store_file(
            self._path, parse_index_file, (None, None, False, {})
        )

    def _body(self) -> str:
        return json.dumps(
            {
                "version": PR_INDEX_FILE_VERSION,
                "fetched_at": self._fetched_at,
                "error": self._error,
                "truncated": self._truncated,
                "prs": self._rows,
            },
            indent=2,
        )

    def replace(self, rows: dict[str, dict], fetched_at: float, *, truncated: bool = False) -> None:
        with self._lock:
            self._rows = {branch: dict(row) for branch, row in rows.items()}
            self._fetched_at = fetched_at
            self._error = None
            self._truncated = truncated
            self._persist()

    def set_error(self, error: str) -> None:
        with self._lock:
            self._error = error[:300]
            self._persist()

    def snapshot(self) -> tuple[float | None, dict[str, dict]]:
        with self._lock:
            return self._fetched_at, {b: dict(r) for b, r in self._rows.items()}

    def meta(self) -> dict:
        """The `pr_index` block of `/branches`."""
        with self._lock:
            return {"fetched_at": self._fetched_at, "count": len(self._rows), "error": self._error, "truncated": self._truncated}


class SingleJob:
    """The half `FetchJobs` and `BriefJobs` share: ONE optional row behind a
    lock, and a `get` that forgets an expired one on the way out — the
    subtract-on-read shape `CreateStore` uses per branch. A subclass says what
    `_expired` means, and owns claiming, advancing and ending the row.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._row: dict | None = None
        self._clock = clock

    def _expired(self, row: dict, now: float) -> bool:
        raise NotImplementedError

    def get(self) -> dict | None:
        with self._lock:
            if not self._row:
                return None
            if self._expired(self._row, self._clock()):
                self._row = None
                return None
            return dict(self._row)


class FetchJobs(SingleJob):
    """The ONE fetch job: `{status, step, error, updated_at}` or nothing.

    A single-key `CreateStore`: `claim` is the test-and-set that is the whole
    mutual exclusion for `POST /fetch`, `advance` moves the step, `fail`
    records why (and expires after `FETCH_ERROR_TTL_SECONDS`), `finish`
    forgets. There is no per-branch key because a fetch is repo-wide.
    """

    def _expired(self, row: dict, now: float) -> bool:
        return row["status"] == "error" and now - row["updated_at"] >= FETCH_ERROR_TTL_SECONDS

    def claim(self) -> bool:
        """True if THIS caller now owns the fetch. A stale error does not
        block a retry — pressing FETCH again is the retry."""
        with self._lock:
            if self._row and self._row["status"] == "running":
                return False
            self._row = {"status": "running", "step": FETCH_STEPS[0], "error": None, "updated_at": self._clock()}
            return True

    def advance(self, step: str) -> None:
        with self._lock:
            if not self._row or self._row["status"] != "running":
                return
            self._row["step"] = step
            self._row["updated_at"] = self._clock()

    def fail(self, error: str) -> None:
        with self._lock:
            step = (self._row or {}).get("step")
            self._row = {"status": "error", "step": step, "error": error[:300], "updated_at": self._clock()}

    def finish(self) -> None:
        with self._lock:
            self._row = None


BRIEF_FILE_VERSION = 1

#: A failed brief run stays visible as long as a failed fetch does.
BRIEF_ERROR_TTL_SECONDS = FETCH_ERROR_TTL_SECONDS

BRIEF_KEYS = ("sha", "base_sha", "what", "next", "model", "cost_usd", "at")


def _brief_row(branch: str, row: dict) -> dict | None:
    what = row.get("what")
    if not isinstance(what, str) or not what:
        return None
    steps = row.get("next")
    return {
        "branch": branch,
        "sha": row.get("sha") if isinstance(row.get("sha"), str) else None,
        "base_sha": row.get("base_sha") if isinstance(row.get("base_sha"), str) else None,
        "what": what,
        "next": [s for s in steps if isinstance(s, str)] if isinstance(steps, list) else [],
        "model": row.get("model") if isinstance(row.get("model"), str) else None,
        "cost_usd": float(row["cost_usd"]) if isinstance(row.get("cost_usd"), (int, float)) else 0.0,
        "at": float(row["at"]) if isinstance(row.get("at"), (int, float)) else None,
    }


def parse_brief_file(text: str) -> tuple[str | None, dict[str, dict]]:
    """File body → `(error, {branch: entry})`. Bad entries are dropped; a
    wrong ENVELOPE raises ValueError so the caller can warn — `parse_index_file`'s
    contract, for the same file shape."""
    envelope, rows = read_rows(text, "briefs")
    error = envelope.get("error") if isinstance(envelope.get("error"), str) else None
    return error, clean_rows(rows, _brief_row)


class BriefStore(BranchRowStore):
    """Thread-safe `{branch: brief entry}` on disk (`brief.py` says what an
    entry is). Unlike the PR index this IS edited an entry at a time — each
    brief lands as it is made, so a run killed halfway keeps what it paid
    for. `set_error` leaves the entries alone for the same reason the PR
    index's does: a login that failed today does not unwrite yesterday's briefs.

    `get` / `drop` / `snapshot` and the load-and-write plumbing are
    `BranchRowStore`'s; the envelope carries an `error` of its own, so `_load` is
    widened here.
    """

    _kept = "briefs"

    def __init__(self, path: Path) -> None:
        # Set before the base's __init__, which loads the file over it.
        self._error: str | None = None
        super().__init__(path)

    def _load(self) -> None:
        self._error, self._rows = load_store_file(self._path, parse_brief_file, (None, {}))

    def _body(self) -> str:
        return json.dumps({"version": BRIEF_FILE_VERSION, "error": self._error, "briefs": self._rows}, indent=2)

    def set(self, entry: dict) -> None:
        with self._lock:
            self._rows[entry["branch"]] = {k: entry.get(k) for k in ("branch", *BRIEF_KEYS)}
            self._error = None
            self._persist()

    def set_error(self, error: str) -> None:
        with self._lock:
            self._error = error[:300]
            self._persist()

    def meta(self) -> dict:
        with self._lock:
            return {"count": len(self._rows), "error": self._error}


class BriefJobs(SingleJob):
    """The ONE brief job: `{status, step, done, total, cost_usd, note, error,
    updated_at}` or nothing — `FetchJobs` with progress.

    `step` is the BRANCH being briefed (the front end prints it as the
    label), `done / total` is the determinate meter, `cost_usd` the running
    sum of the CLI's own estimates. `note` is how a run that hit a cap ends:
    not an error, the briefs it made are on disk, and the note says which
    cap. A finished run keeps its row (status `done`) for one error-TTL so
    the panel can print `briefed just now · $0.05`, then forgets.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        super().__init__(clock)
        self._wall = wall

    def _expired(self, row: dict, now: float) -> bool:
        return row["status"] in ("error", "done") and now - row["updated_at"] >= BRIEF_ERROR_TTL_SECONDS

    def claim(self, total: int) -> bool:
        with self._lock:
            if self._row and self._row["status"] == "running":
                return False
            self._row = {
                "status": "running",
                "step": None,
                "done": 0,
                "total": total,
                "cost_usd": 0.0,
                "note": None,
                "error": None,
                "at": None,
                "updated_at": self._clock(),
            }
            return True

    def advance(self, step: str | None, done: int, cost_usd: float) -> None:
        with self._lock:
            if not self._row or self._row["status"] != "running":
                return
            self._row.update(step=step, done=done, cost_usd=round(cost_usd, 6), updated_at=self._clock())

    def fail(self, error: str) -> None:
        with self._lock:
            row = self._row or {"done": 0, "total": 0, "cost_usd": 0.0}
            self._row = {
                **row,
                "status": "error",
                "step": None,
                "error": error[:300],
                "at": self._wall(),
                "updated_at": self._clock(),
            }

    def finish(self, note: str | None = None) -> None:
        with self._lock:
            if not self._row:
                return
            self._row = {
                **self._row,
                "status": "done",
                "step": None,
                "note": note[:300] if note else None,
                "at": self._wall(),
                "updated_at": self._clock(),
            }


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="worktree-harbor-refresh").start()


class HarborCache:
    """Single-flight, stale-while-revalidate cache for the branch document.
    The module header carries the reasoning; this docstring carries the
    mechanics.

    `get()` → `(doc | None, age_seconds | None, error | None, refreshing)`.
    Fresh: return it. Stale or absent: if no refresh is running, mark one
    running and hand `_refresh` to `start_thread`; then return what there is,
    immediately. `invalidate()` marks the document stale AND bumps a
    generation, so a refresh that was already in flight when the action
    happened cannot land a pre-action document as fresh — it lands as stale,
    and the next poll schedules the one that will see the action.

    Two clocks, on purpose. `_at` is when the document LANDED and is what
    `age_seconds` reports; `_backoff_until` is until when a failed refresh
    holds further attempts off. They used to be one field, and a wedged git
    then reported a minute-old document as zero seconds old for as long as
    it stayed wedged — exactly when the client's STALE readout must not say
    fresh. The back-off is a scheduling fact, not a document fact.

    `clock` and `start_thread` are injected for the tests: no sleeps, no
    threads — a scheduler that runs its target inline is legal because the
    start happens outside the lock — and `refresh_synchronously()` runs the
    runner on demand.
    """

    def __init__(
        self,
        runner: Callable[[], dict],
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        start_thread: Callable[[Callable[[], None]], None] = _start_daemon,
    ) -> None:
        self._runner = runner
        self._ttl = ttl_seconds
        self._clock = clock
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._doc: dict | None = None
        self._at: float | None = None
        self._backoff_until: float | None = None
        self._error: str | None = None
        self._refreshing = False
        self._generation = 0

    def get(self) -> tuple[dict | None, float | None, str | None, bool]:
        with self._lock:
            now = self._clock()
            fresh = (self._at is not None and (now - self._at) < self._ttl) or (
                self._backoff_until is not None and now < self._backoff_until
            )
            schedule = not fresh and not self._refreshing
            if schedule:
                self._refreshing = True  # taken under the lock: the single flight
            doc, error, refreshing = self._doc, self._error, self._refreshing
            age = (now - self._at) if self._at is not None else None
        if schedule:
            # Started OUTSIDE the lock: the flag already excludes a second
            # start, and a test's inline scheduler runs the refresh right here.
            try:
                self._start_thread(self._refresh)
            except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
                # Undo the flag or nothing would ever refresh again. No
                # back-off: a start is cheap, and the next poll may succeed.
                with self._lock:
                    self._refreshing = False
                    self._error = f"could not start refresh: {type(exc).__name__}: {exc}"[:300]
                    error, refreshing = self._error, False
        return doc, age, error, refreshing

    def _refresh(self) -> None:
        with self._lock:
            generation = self._generation
        try:
            doc = self._runner()
        except Exception as exc:  # noqa: BLE001 — keep the last doc, surface why
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"[:300]
                # A full TTL of silence, even if an invalidate landed while
                # this ran: a broken git backs off instead of costing a
                # thread per poll. `_at` is untouched — the document did not
                # get any younger (class docstring).
                self._backoff_until = self._clock() + self._ttl
                self._refreshing = False
            return
        with self._lock:
            self._doc = doc
            self._error = None
            self._backoff_until = None
            # Landed as fresh only if nothing was invalidated meanwhile.
            self._at = self._clock() if generation == self._generation else None
            self._refreshing = False

    def invalidate(self) -> None:
        """The next `get()` schedules a refresh — and serves the old document
        until it lands, which is what makes an action feel instant on the
        surfaces that did not click. Lifts a back-off too: an action is one
        thread, not one per poll."""
        with self._lock:
            self._at = None
            self._backoff_until = None
            self._generation += 1

    def refresh_synchronously(self) -> None:
        """TESTS ONLY: run the refresh on the caller's thread. Takes the
        single-flight slot like a scheduled refresh would."""
        with self._lock:
            self._refreshing = True
        self._refresh()
