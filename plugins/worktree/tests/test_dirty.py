"""The dirty tier (`worktree/dirty.py`) and `GET /state` on top of it.

`/state` used to run `git status` for every worktree on the request thread and
took 4–5 s a recompute; these pin that it never does again, and that the tier
it reads instead is single-flight, TTL'd and honest about what it has not seen.
"""

import os
import tempfile

import pytest

from worktree.dirty import DirtyTier


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sweeps:
    """A recording sweep plus a thread starter that only queues."""

    def __init__(self, answer=None) -> None:
        self.calls: list[list[str]] = []
        self.queued: list = []
        self.answer = answer if answer is not None else (lambda path: path.endswith("dirty"))

    def sweep(self, paths, _budget):
        self.calls.append(list(paths))
        return {path: self.answer(path) for path in paths}

    def start(self, target) -> None:
        self.queued.append(target)

    def run(self) -> None:
        while self.queued:
            self.queued.pop(0)()


def make_tier(sweeps: Sweeps, clock: FakeClock, on_swept=None) -> DirtyTier:
    return DirtyTier(
        sweep=lambda: sweeps.sweep,
        ttl_seconds=20.0,
        budget_seconds=15.0,
        start_thread=sweeps.start,
        clock=clock,
        on_swept=on_swept,
    )


class TestRead:
    def test_a_cold_read_answers_unknown_and_sweeps_in_the_background(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        assert tier.read(["/a", "/b-dirty"]) == {"/a": None, "/b-dirty": None}
        assert sweeps.calls == [] and len(sweeps.queued) == 1  # nothing ran inline
        sweeps.run()
        assert tier.read(["/a", "/b-dirty"]) == {"/a": False, "/b-dirty": True}

    def test_one_sweep_at_a_time(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        tier.read(["/a"])
        tier.read(["/a"])
        tier.read(["/a", "/new"])
        assert len(sweeps.queued) == 1

    def test_fresh_answers_do_not_sweep_until_the_ttl_lapses(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        tier.read(["/a"])
        sweeps.run()
        clock.advance(19.0)
        tier.read(["/a"])
        assert sweeps.queued == []
        clock.advance(1.0)
        tier.read(["/a"])
        assert len(sweeps.queued) == 1

    def test_a_path_it_has_never_seen_is_due_at_once(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        tier.read(["/a"])
        sweeps.run()
        assert tier.read(["/a", "/b"])["/b"] is None
        sweeps.run()
        assert sweeps.calls[-1] == ["/a", "/b"]

    def test_what_the_budget_did_not_reach_stays_unknown_never_clean(self):
        sweeps, clock = Sweeps(answer=lambda path: None if path == "/far" else False), FakeClock()
        tier = make_tier(sweeps, clock)
        tier.read(["/a", "/far"])
        sweeps.run()
        assert tier.read(["/a", "/far"]) == {"/a": False, "/far": None}

    def test_a_background_sweep_says_when_it_landed(self):
        landed = []
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock, on_swept=lambda: landed.append(1))
        tier.read(["/a"])
        assert landed == []
        sweeps.run()
        assert landed == [1]

    def test_a_crashed_sweep_frees_the_slot(self):
        def boom(_paths, _budget):
            raise RuntimeError("git vanished")

        queued, clock = [], FakeClock()
        logs: list[str] = []
        tier = DirtyTier(
            sweep=lambda: boom, ttl_seconds=20.0, budget_seconds=15.0, start_thread=queued.append, clock=clock, log=logs.append
        )
        tier.read(["/a"])
        queued.pop()()
        assert tier.read(["/a"]) == {"/a": None}
        assert "git vanished" in logs[0]
        clock.advance(20.0)
        tier.read(["/a"])
        assert len(queued) == 1  # due again, and nothing wedged the slot

    def test_a_thread_that_will_not_start_frees_the_slot(self):
        def no_threads(_target):
            raise RuntimeError("can't start new thread")

        tier = DirtyTier(
            sweep=lambda: lambda paths, _b: {}, ttl_seconds=20.0, budget_seconds=15.0, start_thread=no_threads, clock=FakeClock()
        )
        assert tier.read(["/a"]) == {"/a": None}
        assert tier._running is False


class TestSweepIfDue:
    def test_runs_inline_when_due_and_skips_when_fresh(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        assert tier.sweep_if_due(["/x-dirty"]) == {"/x-dirty": True}
        assert len(sweeps.calls) == 1 and sweeps.queued == []
        clock.advance(4.0)
        tier.sweep_if_due(["/x-dirty"])
        assert len(sweeps.calls) == 1

    def test_never_waits_on_a_background_sweep_in_flight(self):
        sweeps, clock = Sweeps(), FakeClock()
        tier = make_tier(sweeps, clock)
        tier.read(["/a"])  # a background sweep is claimed but has not run
        assert tier.sweep_if_due(["/a"]) == {"/a": None}
        assert sweeps.calls == []

    def test_an_inline_sweep_does_not_announce_itself(self):
        # Its caller (the harbor runner) already holds the answer; dropping the
        # harbor's cache for it would rebuild the document for nothing.
        landed = []
        tier = make_tier(Sweeps(), FakeClock(), on_swept=lambda: landed.append(1))
        tier.sweep_if_due(["/a"])
        assert landed == []


# -- GET /state on top of it ---------------------------------------------------

REPO = "/Users/someone/code/demo-app"
MANAGED = "/Users/someone/worktrees/demo-app"


def stanza(path, branch, prunable=False):
    return {
        "path": path,
        "branch": branch,
        "head": "abc",
        "detached": branch is None,
        "prunable": prunable,
        "prunable_reason": None,
        "locked": False,
    }


RECORDS = [
    stanza(REPO, "main"),
    stanza(f"{MANAGED}/feat-a", "feat/a"),
    stanza(f"{MANAGED}/feat-b", "feat/b"),
    stanza("/Users/someone/other-tool/worktrees/x/demo-app", "spike"),
    stanza(f"{MANAGED}/gone", "gone", prunable=True),
]


class FakeContext:
    start_dir = REPO

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)


@pytest.fixture
def plugin(monkeypatch):
    from worktree.plugin import WorktreePlugin

    def no_spawn(command, timeout, **_kwargs):  # noqa: ARG001
        raise AssertionError(f"test spawned a subprocess: {command}")

    def no_inline_probe(*_a, **_k):
        raise AssertionError("/state probed a worktree on the request thread")

    calls = {"list": 0, "refs": 0, "sweeps": []}

    def listing(_root):
        calls["list"] += 1
        return [dict(r) for r in RECORDS]

    def refs(_root, base, **_kw):
        calls["refs"] += 1
        return (
            [
                {"branch": "feat/a", "ahead": 2, "behind": 5},
                {"branch": "feat/b", "ahead": 0, "behind": 1},
                {"branch": "gone", "ahead": 0, "behind": 9},
                {"branch": "main", "ahead": 0, "behind": 0},
            ],
            {"ahead_behind": True},
            None,
        )

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", os.path.join(tmp, "prs.json"))
        monkeypatch.setenv("TLDRACK_PR_INDEX_PATH", os.path.join(tmp, "pr-index.json"))
        monkeypatch.setenv("WORKTREE_ROOT", os.path.dirname(MANAGED))
        monkeypatch.setattr("worktree.create.run_bounded", no_spawn)
        monkeypatch.setattr("worktree.plugin.list_worktrees", listing)
        monkeypatch.setattr("worktree.branches.sweep_refs", refs)
        monkeypatch.setattr(
            "worktree.branches.dirty_sweep",
            lambda paths, _b, **_kw: (calls["sweeps"].append(list(paths)), {p: p.endswith("feat-b") for p in paths})[1],
        )
        monkeypatch.setattr("worktree.pr.is_dirty", no_inline_probe)
        monkeypatch.setattr("worktree.pr.ahead_behind", no_inline_probe)
        plugin = WorktreePlugin(FakeContext())
        queued: list = []
        # `/state` also kicks the PR index's own refresh while it polls (`_maybe_refresh_prs`); that job is
        # counted apart, so `queued` stays the dirty tier's.
        pr_refreshes: list = []
        plugin._start_background = lambda target, name: (pr_refreshes if name == "worktree-prs" else queued).append(target)
        plugin.calls, plugin.queued, plugin.pr_refreshes = calls, queued, pr_refreshes
        yield plugin


def rows(body):
    return {row["branch"]: row for row in body["worktrees"]}


class TestStateRoute:
    def test_a_recompute_is_one_listing_and_one_for_each_ref(self, plugin):
        _status, body = plugin.handle_get("/state", {})
        assert plugin.calls["list"] == 1 and plugin.calls["refs"] == 1
        assert {b: (r["ahead"], r["behind"]) for b, r in rows(body).items()} == {
            "feat/a": (2, 5),
            "feat/b": (0, 1),
            "gone": (0, 9),
        }

    def test_dirty_is_unknown_until_the_background_sweep_lands(self, plugin):
        _status, body = plugin.handle_get("/state", {})
        assert [r["dirty"] for r in body["worktrees"]] == [None, None, None]
        assert plugin.calls["sweeps"] == [] and len(plugin.queued) == 1
        plugin.queued.pop()()
        _status, body = plugin.handle_get("/state", {})
        # A prunable tree has no directory to probe: unknown, never clean.
        assert {b: r["dirty"] for b, r in rows(body).items()} == {"feat/a": False, "feat/b": True, "gone": None}
        # The sweep landed inside the 4 s cache: no second listing was needed.
        assert plugin.calls["list"] == 1

    def test_one_sweep_covers_every_live_tree_so_the_harbor_shares_it(self, plugin):
        plugin.handle_get("/state", {})
        plugin.queued.pop()()
        assert plugin.calls["sweeps"] == [[r["path"] for r in RECORDS if not r["prunable"]]]

    def test_old_git_falls_back_to_a_per_branch_count(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _root, _base, **_kw: ([{"branch": "feat/a", "ahead": None, "behind": None}], {"ahead_behind": False}, None),
        )
        counted = []
        monkeypatch.setattr("worktree.pr.ahead_behind", lambda path, _base: (counted.append(str(path)), (7, 1))[1])
        _status, body = plugin.handle_get("/state", {})
        # feat/a came back uncounted; feat/b and gone were not in the answer.
        assert rows(body)["feat/a"]["ahead"] == 7 and len(counted) == 3

    def test_the_cached_rows_are_never_mutated_by_the_overlay(self, plugin):
        plugin.handle_get("/state", {})
        cached_rows = plugin._cache[1]
        plugin.queued.pop()()
        plugin.handle_get("/state", {})
        assert all(row["dirty"] is None for row in cached_rows)


def test_state_advertises_the_permission_modes_the_host_can_start(plugin):
    # The LEAD reads this before it spawns a worker: an older plugin would drop `permission_mode`
    # and start the worker in the default mode.
    assert plugin.handle_get("/state", {})[1]["capabilities"]["permission_modes"] == []

    class Profiles:
        def permission_modes(self):
            return ["auto"]

        def permission_refusal(self, _profile_id, _mode, cli_bin=None):  # noqa: ARG002
            return None

    plugin._context.profiles = Profiles()
    plugin._invalidate()
    assert plugin.handle_get("/state", {})[1]["capabilities"]["permission_modes"] == ["auto"]


def test_state_advertises_create_base(plugin):
    # `launch.py --base` reads this before it creates anything: an older plugin
    # would ignore `base` and cut every lane from main.
    _status, body = plugin.handle_get("/state", {})
    # The other keys (`session_lineage`) are their own tests'; this one is `create_base`.
    assert body["capabilities"]["create_base"] is True
