"""The rack's CLEAN: `/state` rows carrying the harbor's `landed`, and
`POST /cleanup` — its synchronous refusals, its worker's refusals, and the
press that removes a landed tree, its sessions and its branch.

Same fenced plugin as `test_plugin_harbor.py` (every subprocess raises unless a
test scripts it), so a refusal that would have killed a session or removed a
tree fails the test by spawning.
"""

import pytest

from test_branches import FIXTURE, MANAGED, PRUNABLE_DIR, REPO, full
from test_plugin_harbor import RECORDS, FakeThread, plugin  # noqa: F401 — `plugin` is the fixture

from worktree import branches as branches_mod
from worktree import create as create_mod
from worktree import landing as landing_mod
from worktree.names import validate_session_name
from worktree.store import CREATE_ERROR_TTL_SECONDS, CleanupStore

#: A managed tree 8 commits ahead of main, clean in the fixture — landed only
#: once a merged PR shipped its very tip (a squash-merge).
OPEN = "feat/cache-all-pages"
OPEN_PATH = f"{MANAGED}/cache-all-pages"
OPEN_TIP = full("a1b2c3d03")

#: A managed tree 0 ahead (an ancestor of main: landed), but DIRTY in the fixture.
DIRTY = "pr-demo"
DIRTY_TIP = full("a1b2c3d02")

#: A managed tree 0 ahead whose directory is gone (`prunable`).
GONE = "stale-wt"
GONE_TIP = full("89abcdef0")


def merged_pr(number: int, head: str) -> dict:
    """A PR index row: merged into main as #`number` with head `head`."""
    return {
        "number": number,
        "state": "merged",
        "url": f"https://x/pull/{number}",
        "merged_at": "2036-01-01T00:00:00Z",
        "updated_at": "2036-01-01T00:00:00Z",
        "head_oid": head,
        "base_ref": "main",
        "draft": False,
    }


def squash_index(number: int = 77, head: str = OPEN_TIP) -> dict:
    """A PR index naming `OPEN` as merged PR #`number` with head `head`."""
    return {OPEN: merged_pr(number, head)}


def state_rows(plugin) -> dict[str, dict]:  # noqa: F811
    _status, body = plugin.handle_get("/state", {})
    return {row["branch"]: row for row in body["worktrees"]}


def post(plugin, body: dict) -> tuple:  # noqa: F811
    return plugin.handle_post("/cleanup", {}, body)


class Runner:
    """`run_bounded` for a cleanup that goes through. Removing a tree updates
    what git reports afterwards — the branch is no longer checked out — so
    the `/delete-branch` step sees what it would see on the machine."""

    def __init__(self, monkeypatch, path: str, *, branch_delete=(0, "Deleted branch", "")):
        self.calls: list[list[str]] = []
        self.removed = False
        self.branch_delete = branch_delete
        self.path = path
        monkeypatch.setattr("worktree.create.run_bounded", self)
        monkeypatch.setattr("worktree.branches.sweep_refs", self.sweep)
        listing = lambda _root: [
            dict(r)
            for r in RECORDS  # noqa: E731
            if not (self.removed and r["path"] == path)
        ]
        monkeypatch.setattr("worktree.plugin.list_worktrees", listing)
        monkeypatch.setattr("worktree.create.list_worktrees", listing)

    def sweep(self, _root, base, **_kw):
        text = FIXTURE.replace(self.path, "") if self.removed else FIXTURE
        return branches_mod.parse_for_each_ref(text, base, ahead_behind=True), {"ahead_behind": True}, None

    def __call__(self, command, timeout, **_kwargs):  # noqa: ARG002
        self.calls.append(command)
        if command[:2] == ["tmux", "kill-session"]:
            return 0, "", ""
        if command[3:5] == ["worktree", "remove"]:
            self.removed = True
            return 0, "", ""
        if command[3:4] == ["branch"]:
            return self.branch_delete
        raise AssertionError(f"unscripted subprocess: {command}")


def run_worker(plugin) -> None:  # noqa: F811
    worker = next(t for t in reversed(FakeThread.started) if t.target.__name__ == "_cleanup_worker")
    worker.run_now()


class TestStateRows:
    def test_every_row_carries_landed_landing_and_cleanup(self, plugin):
        for row in state_rows(plugin).values():
            assert {"landed", "landing", "cleanup_status", "cleanup_error"} <= set(row)
            assert row["cleanup_status"] is None and row["cleanup_error"] is None

    def test_the_capability_is_advertised(self, plugin):
        _status, body = plugin.handle_get("/state", {})
        assert body["capabilities"]["cleanup"] is True

    def test_an_ancestor_of_main_is_landed_in_main(self, plugin):
        # What a branch cut from main and never committed to reads as, too: the
        # null `pr` is what keeps the rack's button on PR (`work_landed`).
        rows = state_rows(plugin)
        assert rows[DIRTY]["landed"] is True
        assert rows[DIRTY]["landing"] == {"how": "ancestor", "pr": None}

    def test_an_ancestor_whose_merged_pr_shipped_its_tip_names_it(self, plugin):
        plugin._pr_index.replace({DIRTY: merged_pr(91, DIRTY_TIP)}, 1.0)
        assert state_rows(plugin)[DIRTY]["landing"] == {"how": "ancestor", "pr": 91}

    def test_an_old_pr_of_the_same_name_names_nothing(self, plugin):
        # A branch re-cut under a name whose PR merged long ago: not this tip.
        plugin._pr_index.replace({DIRTY: merged_pr(12, full("0dd"))}, 1.0)
        assert state_rows(plugin)[DIRTY]["landing"] == {"how": "ancestor", "pr": None}

    def test_ahead_with_no_pr_index_is_unknown_not_false(self, plugin):
        # A squash-merge is invisible without the index: "no" is not known yet.
        rows = state_rows(plugin)
        assert rows[OPEN]["landed"] is None and rows[OPEN]["landing"] is None

    def test_a_merged_pr_that_shipped_this_tip_is_landed_by_pr(self, plugin):
        plugin._pr_index.replace(squash_index(), 1.0)
        row = state_rows(plugin)[OPEN]
        assert row["landed"] is True and row["landing"] == {"how": "pr", "pr": 77}

    def test_it_is_the_harbors_answer(self, plugin):
        plugin._pr_index.replace(squash_index(), 1.0)
        plugin._harbor.refresh_synchronously()
        _status, harbor = plugin.handle_get("/branches", {})
        harbor_rows = {r["branch"]: r for r in harbor["branches"]}
        for branch, row in state_rows(plugin).items():
            assert row["landing"] == (harbor_rows[branch]["landing"] if row["landed"] else None), branch
            if row["landed"] is not None:
                assert row["landed"] is harbor_rows[branch]["landed"], branch

    def test_a_new_pr_index_reaches_the_very_next_poll(self, plugin):
        assert state_rows(plugin)[OPEN]["landed"] is None  # warms the 4 s cache
        plugin._pr_index.replace(squash_index(), 2.0)  # no _invalidate()
        assert state_rows(plugin)[OPEN]["landed"] is True

    def test_a_merged_pr_whose_head_is_another_commit_is_not_landed(self, plugin, monkeypatch):
        # Merged, but committed to since: the shipped head is not this tip.
        monkeypatch.setattr("worktree.create.run_bounded", lambda *_a, **_k: (1, "", ""))
        plugin._pr_index.replace(squash_index(head=full("eeee44444")), 1.0)
        assert state_rows(plugin)[OPEN]["landed"] is not True

    def test_the_cached_rows_are_never_mutated_by_the_overlays(self, plugin):
        plugin._cleanups.claim(OPEN)
        state_rows(plugin)
        assert all("landed" not in row and "cleanup_status" not in row for row in plugin._cache[1])

    def test_cleanup_progress_rides_the_row(self, plugin):
        plugin._cleanups.claim(OPEN)
        assert state_rows(plugin)[OPEN]["cleanup_status"] == "running"
        plugin._cleanups.fail(OPEN, "not landed in main — nothing to clean up")
        row = state_rows(plugin)[OPEN]
        assert row["cleanup_status"] == "error"
        assert row["cleanup_error"] == "not landed in main — nothing to clean up"

    def test_a_synthesized_create_row_carries_the_full_shape(self, plugin):
        plugin._creates.claim("brand-new")
        row = state_rows(plugin)["brand-new"]
        assert row["landed"] is None and row["landing"] is None
        assert row["cleanup_status"] is None and row["cleanup_error"] is None


class TestLandedOrUnknown:
    def test_a_proof_is_true_whatever_else_is_unknown(self):
        assert branches_mod.landed_or_unknown({"landed": True, "merged": None}, pr_known=False) is True

    def test_each_unknown_is_none(self):
        measured = {"landed": False, "merged": False, "content": "measured"}
        assert branches_mod.landed_or_unknown(None, pr_known=True) is None
        assert branches_mod.landed_or_unknown({**measured, "merged": None}, pr_known=True) is None
        assert branches_mod.landed_or_unknown(measured, pr_known=False) is None
        assert branches_mod.landed_or_unknown({**measured, "content": "pending"}, pr_known=True) is None

    def test_measured_and_unproven_is_false(self):
        row = {"landed": False, "merged": False, "content": "measured"}
        assert branches_mod.landed_or_unknown(row, pr_known=True) is False


class TestSynchronousRefusals:
    @pytest.mark.parametrize("branch", ["bad name", "-D", ""])
    def test_a_bad_branch_name_is_400(self, plugin, branch):
        status, _body = post(plugin, {"branch": branch})
        assert status == 400 and plugin._cleanups.snapshot() == {}

    @pytest.mark.parametrize("session", ["a b", "x;y", "-" * 200, 7])
    def test_a_bad_session_name_is_400(self, plugin, session):
        status, _body = post(plugin, {"branch": OPEN, "session": session})
        assert status == 400 and plugin._cleanups.snapshot() == {}

    def test_a_tree_this_plugin_does_not_manage_is_404(self, plugin):
        # Checked out under ~/other-tool/worktrees/ — `_managed()` is the write gate.
        status, body = post(plugin, {"branch": "add-stopwatch-feed"})
        assert status == 404 and "managed" in body["error"]

    def test_a_create_mid_flight_is_409(self, plugin):
        plugin._creates.claim(OPEN)
        status, body = post(plugin, {"branch": OPEN})
        assert status == 409 and body["error"] == branches_mod.CREATING_REASON

    def test_a_second_press_while_cleaning_is_409(self, plugin):
        assert post(plugin, {"branch": OPEN}) == (202, {"ok": True, "branch": OPEN})
        status, _body = post(plugin, {"branch": OPEN})
        assert status == 409

    def test_a_claim_is_running_and_busy(self, plugin):
        post(plugin, {"branch": OPEN})
        assert state_rows(plugin)[OPEN]["cleanup_status"] == "running"
        assert plugin.busy_reasons() == [f"cleaning up worktree {OPEN}"]

    def test_a_worker_that_never_starts_leaves_an_error_not_a_wedge(self, plugin):
        FakeThread.fail_start = True
        status, _body = post(plugin, {"branch": OPEN})
        assert status == 500
        assert state_rows(plugin)[OPEN]["cleanup_status"] == "error"
        assert plugin.busy_reasons() == []

    def test_pr_refuses_while_a_cleanup_runs(self, plugin):
        plugin._cleanups.claim(OPEN)
        status, body = plugin.handle_post("/pr", {}, {"branch": OPEN})
        assert status == 409 and "cleaned up" in body["error"]

    def test_the_route_is_advertised_and_named(self, plugin):
        assert "/plugin/worktree/cleanup" in plugin.routes()
        assert "/cleanup" in plugin.handle_post("/nope", {}, {})[1]["error"]


class TestWorkerRefusals:
    """Each refusal lands on the row and touches nothing — the fixture's
    `run_bounded` raises on any spawn, so a stray kill fails the test."""

    def refused(self, plugin, branch: str) -> str:  # noqa: F811
        assert post(plugin, {"branch": branch, "session": "main-3"})[0] == 202
        run_worker(plugin)
        row = state_rows(plugin)[branch]
        assert row["cleanup_status"] == "error"
        assert not any(reason.startswith("cleaning up") for reason in plugin.busy_reasons())
        return row["cleanup_error"]

    def test_not_landed_is_refused(self, plugin):
        assert self.refused(plugin, OPEN) == "not landed in main — nothing to clean up"

    def test_a_tip_moved_past_the_merged_head_is_refused(self, plugin, monkeypatch):
        # The PR merged; a commit since moved the tip off its head. Fresh, not cached.
        plugin._pr_index.replace(squash_index(), 1.0)
        assert state_rows(plugin)[OPEN]["landed"] is True
        moved = FIXTURE.replace(OPEN_TIP, full("feed5"))
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _r, base, **_kw: (
                branches_mod.parse_for_each_ref(moved, base, ahead_behind=True),
                {"ahead_behind": True},
                None,
            ),
        )
        asked = []

        def ancestry_only(command, _timeout, **_kw):
            # `landed` asks once whether the new tip is inside the shipped head: no.
            assert command[3:5] == ["merge-base", "--is-ancestor"], command
            asked.append(command)
            return 1, "", ""

        monkeypatch.setattr("worktree.create.run_bounded", ancestry_only)
        assert self.refused(plugin, OPEN) == "not landed in main — nothing to clean up"
        assert len(asked) == 1, "the ancestry cache answers every later judge"

    def test_a_branch_with_no_work_of_its_own_is_refused(self, plugin, monkeypatch):
        # Cut from main, never committed to, clean: an ancestor, so landed — and
        # its agent mid-turn. Nothing is killed (the fixture raises on a spawn).
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _b, **_kw: dict.fromkeys(paths, False))
        assert self.refused(plugin, DIRTY) == "nothing of this branch's own has landed — no merged PR shipped it"

    def test_uncommitted_work_is_refused_and_never_forced(self, plugin):
        plugin._pr_index.replace({DIRTY: merged_pr(91, DIRTY_TIP)}, 1.0)
        assert self.refused(plugin, DIRTY) == "uncommitted work in the worktree — nothing removed"

    def test_an_unreadable_status_is_refused(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _b, **_kw: dict.fromkeys(paths))
        plugin._pr_index.replace(squash_index(), 1.0)
        assert "could not read" in self.refused(plugin, OPEN)

    def test_a_pr_job_in_flight_is_refused(self, plugin):
        plugin._pr_jobs.add(OPEN)
        assert "PR is being opened" in self.refused(plugin, OPEN)

    def test_a_worker_crash_is_an_error_row(self, plugin, monkeypatch):
        def boom(*_a):
            raise RuntimeError("boom")

        monkeypatch.setattr(plugin, "_fresh_row", boom)
        assert self.refused(plugin, OPEN) == "RuntimeError: boom"

    def test_a_refusal_can_be_pressed_again(self, plugin):
        self.refused(plugin, OPEN)
        assert post(plugin, {"branch": OPEN})[0] == 202

    def test_an_error_expires_like_a_create_error(self):
        clock = [0.0]
        store = CleanupStore(lambda: clock[0])
        store.claim(OPEN)
        store.fail(OPEN, "boom")
        assert store.get(OPEN) == {"status": "error", "error": "boom", "updated_at": 0.0}
        clock[0] = CREATE_ERROR_TTL_SECONDS
        assert store.get(OPEN) is None


class TestCleanupGoesThrough:
    def test_sessions_then_tree_then_branch(self, plugin, monkeypatch):
        plugin._pr_index.replace(squash_index(), 1.0)
        plugin._store.set(OPEN, status="open", url="https://x/pull/77")
        runner = Runner(monkeypatch, OPEN_PATH)

        assert post(plugin, {"branch": OPEN, "session": "main-3"})[0] == 202
        run_worker(plugin)

        assert runner.calls == [
            ["tmux", "kill-session", "-t", "=wt-feat-cache-all-pages"],
            ["tmux", "kill-session", "-t", "=main-3"],
            ["git", "-C", REPO, "worktree", "remove", OPEN_PATH],  # never --force
            # Squash-landed: `-d` is the ancestor test and would refuse it.
            ["git", "-C", REPO, "branch", "-D", OPEN],
        ]
        assert plugin._cleanups.get(OPEN) is None and plugin.busy_reasons() == []
        assert plugin._store.get(OPEN) is None, "a re-created branch must not inherit the PR link"
        assert OPEN not in state_rows(plugin)
        assert any(f"cleaned up '{OPEN}'" in line for line in plugin._context.lines)

    def test_the_derived_session_is_killed_once(self, plugin, monkeypatch):
        plugin._pr_index.replace(squash_index(), 1.0)
        runner = Runner(monkeypatch, OPEN_PATH)
        post(plugin, {"branch": OPEN, "session": "wt-feat-cache-all-pages"})
        run_worker(plugin)
        assert [c for c in runner.calls if c[0] == "tmux"] == [["tmux", "kill-session", "-t", "=wt-feat-cache-all-pages"]]

    def test_a_refused_branch_delete_is_not_a_failed_cleanup(self, plugin, monkeypatch):
        plugin._pr_index.replace(squash_index(), 1.0)
        Runner(monkeypatch, OPEN_PATH, branch_delete=(1, "", "error: something odd"))
        post(plugin, {"branch": OPEN})
        run_worker(plugin)
        assert plugin._cleanups.get(OPEN) is None, "the tree is gone; the harbor can finish the branch"
        assert any("kept branch" in line for line in plugin._context.lines)

    def test_a_tree_whose_directory_is_gone_skips_the_probes(self, plugin, monkeypatch):
        def no_probe(*_a, **_kw):
            raise AssertionError("probed a directory that does not exist")

        monkeypatch.setattr("worktree.branches.dirty_sweep", no_probe)
        monkeypatch.setattr("worktree.landing.await_vacated", no_probe)
        plugin._pr_index.replace({GONE: merged_pr(64, GONE_TIP)}, 1.0)
        runner = Runner(monkeypatch, PRUNABLE_DIR)
        post(plugin, {"branch": GONE})
        run_worker(plugin)
        assert runner.calls[-2:] == [
            ["git", "-C", REPO, "worktree", "remove", PRUNABLE_DIR],
            ["git", "-C", REPO, "branch", "-d", GONE],
        ]
        assert plugin._cleanups.get(GONE) is None


class TestOccupants:
    """Anyone left in the tree once the press's own sessions are killed keeps
    it: a `+ NEW` that cwd'd in, a dev server. Unknown refuses too."""

    def press(self, plugin, monkeypatch, occupants) -> Runner:  # noqa: F811
        plugin._pr_index.replace(squash_index(), 1.0)
        runner = Runner(monkeypatch, OPEN_PATH)
        monkeypatch.setattr("worktree.landing.OCCUPANT_SETTLE_SECONDS", 0.0)
        monkeypatch.setattr(
            "worktree.landing.sweep_occupants",
            lambda paths, **_kw: {p: (list(occupants) if occupants is not None else None) for p in paths},
        )
        post(plugin, {"branch": OPEN})
        run_worker(plugin)
        return runner

    def test_a_third_occupant_keeps_the_tree(self, plugin, monkeypatch):
        runner = self.press(plugin, monkeypatch, ["claude", "node"])
        assert runner.calls == [["tmux", "kill-session", "-t", "=wt-feat-cache-all-pages"]]
        row = state_rows(plugin)[OPEN]
        assert row["cleanup_status"] == "error"
        assert row["cleanup_error"] == "claude, node still running in the worktree — it was kept"

    def test_an_lsof_that_cannot_answer_keeps_the_tree(self, plugin, monkeypatch):
        runner = self.press(plugin, monkeypatch, None)
        assert not any(c[3:5] == ["worktree", "remove"] for c in runner.calls)
        assert "could not tell" in state_rows(plugin)[OPEN]["cleanup_error"]

    def test_an_empty_tree_goes(self, plugin, monkeypatch):
        runner = self.press(plugin, monkeypatch, [])
        assert ["git", "-C", REPO, "worktree", "remove", OPEN_PATH] in runner.calls
        assert plugin._cleanups.get(OPEN) is None

    def test_the_killed_agent_is_given_time_to_leave(self, monkeypatch):
        answers = iter([["claude"], ["claude"], []])
        monkeypatch.setattr("worktree.landing.sweep_occupants", lambda paths, **_kw: {p: next(answers) for p in paths})
        clock, naps = [0.0], []

        def sleep(seconds):
            naps.append(seconds)
            clock[0] += seconds

        assert landing_mod.await_vacated("/t", sleep=sleep, clock=lambda: clock[0]) == []
        assert naps == [landing_mod.OCCUPANT_POLL_SECONDS] * 2

    def test_whoever_outstays_the_settle_is_named(self, monkeypatch):
        monkeypatch.setattr("worktree.landing.sweep_occupants", lambda paths, **_kw: {p: ["node"] for p in paths})
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        assert landing_mod.await_vacated("/t", sleep=sleep, clock=lambda: clock[0]) == ["node"]
        assert clock[0] >= landing_mod.OCCUPANT_SETTLE_SECONDS


class TestStaleCleanupRows:
    """A failed clean-up is forgotten with its tree, so a re-created branch of
    the same name never inherits `CLEAN !` — but a RUNNING one is its own."""

    def failed(self, plugin, branch: str) -> None:  # noqa: F811
        plugin._cleanups.claim(branch)
        plugin._cleanups.fail(branch, "uncommitted work in the worktree — nothing removed")

    def test_remove_forgets_a_failed_cleanup(self, plugin, monkeypatch):
        self.failed(plugin, OPEN)
        Runner(monkeypatch, OPEN_PATH)
        assert plugin.handle_post("/remove", {}, {"branch": OPEN})[0] == 200
        assert plugin._cleanups.get(OPEN) is None

    def test_delete_branch_forgets_a_failed_cleanup(self, plugin, monkeypatch):
        self.failed(plugin, "test123")
        Runner(monkeypatch, OPEN_PATH)
        assert plugin.handle_post("/delete-branch", {}, {"branch": "test123"})[0] == 200
        assert plugin._cleanups.get("test123") is None

    def test_a_running_cleanup_outlives_its_own_tree_removal(self, plugin):
        plugin._cleanups.claim(OPEN)
        plugin._tree_removed(OPEN)
        assert plugin._cleanups.get(OPEN)["status"] == "running"
        assert plugin.busy_reasons() == [f"cleaning up worktree {OPEN}"]


class TestSharedInternals:
    def test_remove_worktree_kills_first_dedupes_and_never_forces(self, monkeypatch):
        calls = []
        monkeypatch.setattr("worktree.create.run_bounded", lambda command, _t, **_k: (calls.append(command), (0, "", ""))[1])
        assert create_mod.remove_worktree(REPO, OPEN_PATH, ["wt-a", "b", "wt-a"]) is None
        assert calls == [
            ["tmux", "kill-session", "-t", "=wt-a"],
            ["tmux", "kill-session", "-t", "=b"],
            ["git", "-C", REPO, "worktree", "remove", OPEN_PATH],
        ]

    def test_remove_worktree_answers_gits_reason(self, monkeypatch):
        monkeypatch.setattr(
            "worktree.create.run_bounded",
            lambda command, _t, **_k: (
                (128, "", "fatal: contains modified or untracked files\n") if command[0] == "git" else (1, "", "")
            ),
        )
        assert create_mod.remove_worktree(REPO, OPEN_PATH, ["wt-a"]) == "fatal: contains modified or untracked files"

    @pytest.mark.parametrize("name", ["wt-feat-x", "main_2", "A-1"])
    def test_session_names_the_bridge_lists_pass(self, name):
        assert validate_session_name(name) is None

    @pytest.mark.parametrize("name", ["", "a b", "x;y", "=wt", "-" * 129, None, 3])
    def test_anything_else_is_refused(self, name):
        assert validate_session_name(name) is not None
