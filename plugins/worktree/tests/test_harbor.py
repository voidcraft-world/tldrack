"""The harbor's three stores: the persisted PR index, the in-memory fetch
job, and the cache that keeps `GET /branches` off the request thread.

No sleeps and no real threads: the cache takes an injected `start_thread`
that RECORDS its target (the test runs it when it chooses) and a fake clock.
"""

import json

from worktree.harbor import (
    BRANCHES_TTL_SECONDS,
    DIRTY_SWEEP_BUDGET_SECONDS,
    DIRTY_TTL_SECONDS,
    FETCH_ERROR_TTL_SECONDS,
    FETCH_STEPS,
    FetchJobs,
    HarborCache,
    PrIndexStore,
    parse_index_file,
)
from worktree.store import CREATE_ERROR_TTL_SECONDS

ROWS = {
    "pr-demo": {
        "number": 31,
        "state": "merged",
        "url": "https://x/31",
        "draft": False,
        "merged_at": "2026-08-31T02:00:00Z",
        "updated_at": "2026-08-31T02:00:00Z",
        "head_oid": "a1b2c3d02" + "0" * 31,
        "base_ref": "main",
    },
    "add-stopwatch-feed": {
        "number": 40,
        "state": "open",
        "url": "https://x/40",
        "draft": True,
        "merged_at": None,
        "updated_at": "2026-09-01T00:00:00Z",
        "head_oid": None,
        "base_ref": "main",
    },
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestConstants:
    def test_fetch_steps_are_pinned(self):
        # Re-declared in the UI's `agent-rack/worktree-client.ts`; a
        # reordering here without one there draws the meter backwards.
        assert FETCH_STEPS == ("fetch", "prs")

    def test_ttls(self):
        assert BRANCHES_TTL_SECONDS == 4.0
        assert DIRTY_TTL_SECONDS == 20.0
        assert DIRTY_SWEEP_BUDGET_SECONDS == 15.0
        assert FETCH_ERROR_TTL_SECONDS == CREATE_ERROR_TTL_SECONDS == 300.0


class TestPrIndexStore:
    def test_starts_empty_without_a_file(self, tmp_path):
        store = PrIndexStore(tmp_path / "index.json")
        assert store.meta() == {"fetched_at": None, "count": 0, "error": None, "truncated": False}
        assert store.get("anything") is None

    def test_replace_persists_rows_and_fetched_at(self, tmp_path):
        path = tmp_path / "index.json"
        PrIndexStore(path).replace(ROWS, 1788600000.0)

        reloaded = PrIndexStore(path)
        assert reloaded.get("pr-demo") == ROWS["pr-demo"]
        fetched_at, rows = reloaded.snapshot()
        assert fetched_at == 1788600000.0 and set(rows) == set(ROWS)
        assert reloaded.meta()["count"] == 2

    def test_an_index_written_before_head_oid_loads_with_it_none(self, tmp_path):
        # The on-disk file of every machine that upgrades: `landed` must read
        # "cannot tell" until the next FETCH, never trip over a missing key.
        legacy = {name: {k: v for k, v in row.items() if k != "head_oid"} for name, row in ROWS.items()}
        path = tmp_path / "index.json"
        path.write_text(json.dumps({"version": 1, "fetched_at": 1.0, "error": None, "prs": legacy}))
        assert PrIndexStore(path).get("pr-demo")["head_oid"] is None

    def test_a_malformed_head_oid_on_disk_loads_as_none(self, tmp_path):
        path = tmp_path / "index.json"
        rows = {"pr-demo": {**ROWS["pr-demo"], "head_oid": "a1b2c3d02"}}
        path.write_text(json.dumps({"version": 1, "fetched_at": 1.0, "error": None, "prs": rows}))
        assert PrIndexStore(path).get("pr-demo")["head_oid"] is None

    def test_truncated_persists_and_the_next_whole_list_clears_it(self, tmp_path):
        path = tmp_path / "index.json"
        PrIndexStore(path).replace(ROWS, 1.0, truncated=True)
        reloaded = PrIndexStore(path)
        assert reloaded.meta()["truncated"] is True
        reloaded.set_error("gh not found")
        assert PrIndexStore(path).meta()["truncated"] is True, "an error keeps the old list, cut or not"
        reloaded.replace(ROWS, 2.0)
        assert PrIndexStore(path).meta()["truncated"] is False

    def test_an_index_written_before_truncated_loads_as_whole(self, tmp_path):
        path = tmp_path / "index.json"
        path.write_text(json.dumps({"version": 1, "fetched_at": 1.0, "error": None, "prs": ROWS}))
        assert PrIndexStore(path).meta()["truncated"] is False

    def test_the_envelope_is_versioned(self, tmp_path):
        path = tmp_path / "index.json"
        PrIndexStore(path).replace(ROWS, 1.0)
        assert json.loads(path.read_text())["version"] == 1

    def test_set_error_keeps_the_rows_and_survives_a_restart(self, tmp_path):
        # A gh that is missing today does not make yesterday's index wrong.
        path = tmp_path / "index.json"
        store = PrIndexStore(path)
        store.replace(ROWS, 1.0)
        store.set_error("gh not found")
        assert store.meta()["count"] == 2 and store.meta()["error"] == "gh not found"
        assert PrIndexStore(path).meta()["error"] == "gh not found"

    def test_replace_clears_the_error(self, tmp_path):
        store = PrIndexStore(tmp_path / "index.json")
        store.set_error("gh not found")
        store.replace(ROWS, 2.0)
        assert store.meta()["error"] is None

    def test_get_returns_a_copy(self, tmp_path):
        store = PrIndexStore(tmp_path / "index.json")
        store.replace(ROWS, 1.0)
        store.get("pr-demo")["number"] = 999
        assert store.get("pr-demo")["number"] == 31

    def test_bad_rows_are_dropped_not_fatal(self):
        text = json.dumps(
            {
                "version": 1,
                "fetched_at": 5,
                "prs": {
                    "good": {"number": 1, "state": "open"},
                    "bad-state": {"number": 2, "state": "OPEN"},
                    "bad-number": {"number": "3", "state": "open"},
                    "not-a-row": 4,
                },
            }
        )
        fetched_at, error, truncated, rows = parse_index_file(text)
        assert fetched_at == 5.0 and error is None and truncated is False
        assert set(rows) == {"good"}
        assert rows["good"]["draft"] is False and rows["good"]["url"] is None

    def test_a_corrupt_envelope_loads_empty_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "index.json"
        path.write_text("[1, 2, 3]")
        store = PrIndexStore(path)
        assert store.meta()["count"] == 0
        assert "ignoring unreadable" in capsys.readouterr().err

    def test_unparseable_json_loads_empty_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "index.json"
        path.write_text("{not json")
        assert PrIndexStore(path).meta()["count"] == 0
        assert "ignoring unreadable" in capsys.readouterr().err

    def test_persist_is_atomic_no_temp_file_left_behind(self, tmp_path):
        path = tmp_path / "index.json"
        PrIndexStore(path).replace(ROWS, 1.0)
        assert [p.name for p in tmp_path.iterdir()] == ["index.json"]


class TestFetchJobs:
    def test_claim_wins_once_and_starts_at_the_first_step(self):
        jobs = FetchJobs(FakeClock())
        assert jobs.claim() is True
        assert jobs.get() == {"status": "running", "step": "fetch", "error": None, "updated_at": 1000.0}

    def test_second_claim_loses_while_running(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        assert jobs.claim() is False

    def test_advance_moves_the_step(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        jobs.advance("prs")
        assert jobs.get()["step"] == "prs"

    def test_advance_is_a_no_op_when_not_running(self):
        jobs = FetchJobs(FakeClock())
        jobs.advance("prs")
        assert jobs.get() is None

    def test_finish_forgets(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        jobs.finish()
        assert jobs.get() is None
        assert jobs.claim() is True

    def test_fail_keeps_the_step_it_died_on(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        jobs.advance("prs")
        jobs.fail("gh exploded")
        assert jobs.get() == {"status": "error", "step": "prs", "error": "gh exploded", "updated_at": 1000.0}

    def test_an_error_clears_on_reclaim(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        jobs.fail("boom")
        assert jobs.claim() is True
        assert jobs.get()["status"] == "running" and jobs.get()["error"] is None

    def test_an_error_expires_at_the_ttl(self):
        clock = FakeClock()
        jobs = FetchJobs(clock)
        jobs.claim()
        jobs.fail("boom")
        clock.advance(FETCH_ERROR_TTL_SECONDS - 1)
        assert jobs.get() is not None
        clock.advance(1)
        assert jobs.get() is None

    def test_a_running_job_never_expires(self):
        clock = FakeClock()
        jobs = FetchJobs(clock)
        jobs.claim()
        clock.advance(FETCH_ERROR_TTL_SECONDS * 10)
        assert jobs.get()["status"] == "running"

    def test_a_long_error_is_truncated(self):
        jobs = FetchJobs(FakeClock())
        jobs.claim()
        jobs.fail("x" * 5000)
        assert len(jobs.get()["error"]) == 300


class Scheduler:
    """Records the refresh targets `HarborCache` hands it; `run()` runs them."""

    def __init__(self) -> None:
        self.targets: list = []

    def __call__(self, target) -> None:
        self.targets.append(target)

    def run(self) -> None:
        while self.targets:
            self.targets.pop(0)()


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.raise_next = False

    def __call__(self) -> dict:
        self.calls += 1
        if self.raise_next:
            raise RuntimeError("git exploded")
        return {"n": self.calls}


def make_cache(ttl: float = 4.0):
    clock, scheduler, runner = FakeClock(), Scheduler(), CountingRunner()
    cache = HarborCache(runner, ttl, clock=clock, start_thread=scheduler)
    return cache, clock, scheduler, runner


class TestHarborCache:
    def test_a_cold_get_returns_none_and_schedules_one_refresh(self):
        cache, _clock, scheduler, runner = make_cache()
        doc, age, error, refreshing = cache.get()
        assert doc is None and age is None and error is None
        assert refreshing is True
        assert len(scheduler.targets) == 1
        assert runner.calls == 0  # NOTHING ran on the caller's thread

    def test_two_cold_gets_schedule_ONE_refresh(self):
        # The single flight: four rack surfaces polling cost one sweep.
        cache, _clock, scheduler, _runner = make_cache()
        cache.get()
        cache.get()
        assert len(scheduler.targets) == 1

    def test_the_scheduled_refresh_lands_the_document(self):
        cache, _clock, scheduler, _runner = make_cache()
        cache.get()
        scheduler.run()
        doc, age, error, refreshing = cache.get()
        assert doc == {"n": 1} and age == 0.0 and error is None and refreshing is False

    def test_a_get_within_the_ttl_runs_nothing(self):
        cache, clock, scheduler, runner = make_cache()
        cache.get()
        scheduler.run()
        clock.advance(3.9)
        cache.get()
        assert scheduler.targets == [] and runner.calls == 1

    def test_a_get_after_the_ttl_returns_the_STALE_doc_and_schedules_one(self):
        cache, clock, scheduler, runner = make_cache()
        cache.get()
        scheduler.run()
        clock.advance(4.0)
        doc, age, _e, refreshing = cache.get()
        assert doc == {"n": 1} and age == 4.0 and refreshing is True
        cache.get()  # a second stale poll joins the same flight
        assert len(scheduler.targets) == 1
        scheduler.run()
        assert runner.calls == 2 and cache.get()[0] == {"n": 2}

    def test_invalidate_then_get_schedules_exactly_one(self):
        cache, _clock, scheduler, _runner = make_cache()
        cache.get()
        scheduler.run()
        cache.invalidate()
        doc, age, _e, _r = cache.get()
        assert doc == {"n": 1} and age is None  # the old doc serves meanwhile
        cache.get()
        assert len(scheduler.targets) == 1

    def test_a_raising_runner_keeps_the_previous_doc_and_exposes_the_error(self):
        cache, clock, scheduler, runner = make_cache()
        cache.get()
        scheduler.run()
        clock.advance(4.0)
        runner.raise_next = True
        cache.get()
        scheduler.run()
        doc, age, error, refreshing = cache.get()
        assert doc == {"n": 1}
        assert age == 4.0  # the REAL age: a failed refresh made nothing younger
        assert error == "RuntimeError: git exploded"
        assert refreshing is False

    def test_the_age_keeps_growing_while_git_stays_wedged(self):
        # A minute-old document under a wedged git must read as a minute
        # old, never as fresh — the client's STALE readout depends on it.
        cache, clock, scheduler, runner = make_cache()
        cache.get()
        scheduler.run()
        runner.raise_next = True
        for _ in range(15):
            clock.advance(4.0)
            cache.get()
            scheduler.run()
        doc, age, error, _r = cache.get()
        assert doc == {"n": 1} and age == 60.0 and error
        assert runner.calls == 16  # one attempt per TTL, not per poll

    def test_a_raising_runner_backs_off_a_full_ttl(self):
        # A wedged git must cost one thread per TTL, not one per poll.
        cache, clock, scheduler, runner = make_cache()
        runner.raise_next = True
        cache.get()
        scheduler.run()
        assert runner.calls == 1
        clock.advance(3.9)
        cache.get()
        assert scheduler.targets == []
        clock.advance(0.1)
        cache.get()
        assert len(scheduler.targets) == 1

    def test_a_success_after_a_failure_clears_the_error(self):
        cache, clock, scheduler, runner = make_cache()
        runner.raise_next = True
        cache.get()
        scheduler.run()
        runner.raise_next = False
        clock.advance(4.0)
        cache.get()
        scheduler.run()
        assert cache.get()[2] is None

    def test_a_failure_with_no_document_yet_reports_no_age(self):
        cache, _clock, scheduler, runner = make_cache()
        runner.raise_next = True
        cache.get()
        scheduler.run()
        doc, age, error, refreshing = cache.get()
        assert doc is None and age is None and error and refreshing is False

    def test_invalidate_lifts_a_back_off(self):
        # An action is one thread, not one per poll — and it must not wait
        # out a back-off the polls earned.
        cache, _clock, scheduler, runner = make_cache()
        runner.raise_next = True
        cache.get()
        scheduler.run()
        cache.get()
        assert scheduler.targets == []  # backing off
        cache.invalidate()
        cache.get()
        assert len(scheduler.targets) == 1

    def test_a_start_thread_that_raises_leaves_refreshing_false_and_error_set(self):
        def broken(_target):
            raise RuntimeError("can't start new thread")

        cache = HarborCache(CountingRunner(), 4.0, clock=FakeClock(), start_thread=broken)
        doc, _age, error, refreshing = cache.get()
        assert doc is None and refreshing is False
        assert error.startswith("could not start refresh: RuntimeError")
        # …and the next get tries again rather than staying wedged.
        assert cache.get()[3] is False

    def test_an_invalidate_WHILE_the_runner_reads_lands_the_doc_as_STALE(self):
        # The refresh read git BEFORE the action landed; its document must
        # not serve as fresh for a TTL after it.
        clock, scheduler = FakeClock(), Scheduler()
        calls = []

        def runner():
            calls.append(1)
            if len(calls) == 1:
                cache.invalidate()  # the action happens while git is being read
            return {"n": len(calls)}

        cache = HarborCache(runner, 4.0, clock=clock, start_thread=scheduler)
        cache.get()
        scheduler.run()
        doc, age, _e, refreshing = cache.get()
        assert doc == {"n": 1} and age is None  # served, but stale
        assert refreshing is True and len(scheduler.targets) == 1  # rescheduled
        scheduler.run()
        assert len(calls) == 2 and cache.get()[0] == {"n": 2}

    def test_an_invalidate_BEFORE_the_runner_reads_lands_the_doc_as_fresh(self):
        # The scheduled refresh had not started; whatever it reads is
        # post-action, so a second sweep would only repeat it.
        cache, _clock, scheduler, runner = make_cache()
        cache.get()
        cache.invalidate()
        scheduler.run()
        doc, age, _e, refreshing = cache.get()
        assert doc == {"n": 1} and age == 0.0 and refreshing is False
        assert runner.calls == 1 and scheduler.targets == []

    def test_refresh_synchronously_runs_the_runner_inline(self):
        cache, _clock, scheduler, runner = make_cache()
        cache.refresh_synchronously()
        assert runner.calls == 1 and scheduler.targets == []
        doc, age, _e, refreshing = cache.get()
        assert doc == {"n": 1} and age == 0.0 and refreshing is False

    def test_an_inline_scheduler_does_not_deadlock(self):
        # The start happens OUTSIDE the lock, so a scheduler that runs its
        # target right away is legal — and the runner's write lands.
        runner = CountingRunner()
        cache = HarborCache(runner, 4.0, clock=FakeClock(), start_thread=lambda target: target())
        doc, _age, _e, refreshing = cache.get()
        assert runner.calls == 1
        assert doc is None and refreshing is True  # the snapshot predates the start
        doc, _age, _e, refreshing = cache.get()
        assert doc == {"n": 1} and refreshing is False and runner.calls == 1
