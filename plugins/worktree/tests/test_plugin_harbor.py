"""The harbor routes on `WorktreePlugin`: `GET /branches`, `POST /fetch`,
`POST /delete-branch`, `POST /prune-worktrees`, `/state.worktree_paths` and
`/create {existing: true}` — against a plugin that CANNOT spawn.

Same fence as `test_create_progress.py` (`run_bounded` raises), plus every
git reader the harbor runner uses answered from `test_branches`' fixture, so
the document under test is the one this machine would produce.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from test_branches import (
    AUTHOR,
    FIXTURE,
    HOME,
    INTENT,
    MANAGED,
    PRUNABLE_DIR,
    PRUNE_OUTPUT,
    PR_LIST_JSON,
    REMOTE_FIXTURE,
    REPO,
    full,
    row,
)
from worktree import branches as branches_mod
from worktree import pr as pr_mod
from worktree.harbor import FETCH_STEPS, HarborCache
from worktree.names import worktree_path_for


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Scheduler:
    def __init__(self) -> None:
        self.targets: list = []

    def __call__(self, target) -> None:
        self.targets.append(target)

    def run(self) -> None:
        while self.targets:
            self.targets.pop(0)()


class FakeProfiles:
    """The host's `headless_call` (tldrack/headless.py owns the real
    recipe and its tests): no CLI → None, as the host answers."""

    def headless_call(
        self,
        profile_id,
        *,
        system_prompt,
        model,
        budget_usd,
        json_schema=None,  # noqa: ARG002
        stream=False,
        max_output_tokens=None,
        cli_bin=None,
        base=None,
    ):  # noqa: ARG002
        if cli_bin is None:
            return None
        return {
            "profile": "claude",
            "argv": [cli_bin, "-p", "--model", model, "--system-prompt", system_prompt],
            "env": {"MAX_THINKING_TOKENS": "0"},
        }


class FakeContext:
    start_dir = REPO

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)


class FakeThread:
    """Stands in for `threading.Thread`: records the target, runs it on
    demand. `fail_start` makes `.start()` raise, for the undo paths."""

    fail_start = False
    started: list = []

    def __init__(self, target=None, args=(), daemon=None, name=None):  # noqa: ARG002
        self.target, self.args = target, args

    def start(self) -> None:
        if FakeThread.fail_start:
            raise RuntimeError("can't start new thread")
        FakeThread.started.append(self)

    def run_now(self) -> None:
        self.target(*self.args)


def stanza(path: str, branch: str | None, *, detached=False, prunable=False, locked=False) -> dict:
    return {
        "path": path,
        "branch": branch,
        "head": "abc123",
        "detached": detached,
        "prunable": prunable,
        "prunable_reason": "gitdir file points to non-existent location" if prunable else None,
        "locked": locked,
    }


RECORDS = [
    stanza(REPO, "main"),
    stanza(f"{INTENT}/spike-one/demo-app", "add-stopwatch-feed"),
    stanza(f"{MANAGED}/pr-demo", "pr-demo", locked=True),
    stanza(f"{MANAGED}/cache-all-pages", "feat/cache-all-pages"),
    stanza(PRUNABLE_DIR, "stale-wt", prunable=True),
    stanza(f"{INTENT}/spike/demo-app", None, detached=True),
]


@pytest.fixture
def plugin(monkeypatch):
    """A fenced plugin whose every harbor reader answers from the fixture."""
    from worktree.plugin import WorktreePlugin

    def no_spawn(command, timeout, **_kwargs):  # noqa: ARG001
        raise AssertionError(f"test spawned a subprocess: {command}")

    FakeThread.started, FakeThread.fail_start = [], False
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", os.path.join(tmp, "prs.json"))
        monkeypatch.setenv("TLDRACK_PR_INDEX_PATH", os.path.join(tmp, "pr-index.json"))
        monkeypatch.setenv("TLDRACK_BRIEF_PATH", os.path.join(tmp, "briefs.json"))
        monkeypatch.setenv("WORKTREE_ROOT", os.path.dirname(MANAGED))
        monkeypatch.setattr("worktree.create.run_bounded", no_spawn)
        monkeypatch.setattr("worktree.plugin.gh_found", lambda: True)
        monkeypatch.setattr("worktree.plugin.list_worktrees", lambda _root: [dict(r) for r in RECORDS])
        monkeypatch.setattr("worktree.create.list_worktrees", lambda _root: [dict(r) for r in RECORDS])
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _root, base, **_kw: (
                branches_mod.parse_for_each_ref(FIXTURE, base, ahead_behind=True),
                {"ahead_behind": True},
                None,
            ),
        )
        monkeypatch.setattr(
            "worktree.branches.sweep_remote_refs",
            lambda _root, base, **_kw: (branches_mod.parse_remote_refs(REMOTE_FIXTURE, "origin", base, ahead_behind=True), None),
        )
        monkeypatch.setattr(
            "worktree.branches.dirty_sweep", lambda paths, _budget, **_kw: {p: (p == f"{MANAGED}/pr-demo") for p in paths}
        )
        monkeypatch.setattr("worktree.branches.stash_count", lambda _root, **_kw: 1)
        # The content tier's readers (landing.py): nobody holds anybody, nobody
        # stands in any tree, every object is here, no local branch to fetch for.
        # Tests that need otherwise patch these again.
        monkeypatch.setattr("worktree.landing.contains_sets", lambda _root, _rows, **_kw: {})
        monkeypatch.setattr("worktree.landing.sweep_occupants", lambda paths, **_kw: {p: [] for p in paths})
        monkeypatch.setattr("worktree.landing.missing_objects", lambda _root, _oids, **_kw: set())
        monkeypatch.setattr("worktree.landing.local_branch_names", lambda _root, **_kw: set())
        # The archive's readers (archive.py): nothing pinned. Tests that need
        # a pin patch these again.
        monkeypatch.setattr("worktree.archive.list_archived", lambda _root: [])
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, _branch: [])
        monkeypatch.setattr("worktree.pr.ahead_behind", lambda *_a: (0, 0))
        monkeypatch.setattr("worktree.pr.is_dirty", lambda *_a: False)
        monkeypatch.setattr("worktree.plugin.threading.Thread", FakeThread)
        plugin = WorktreePlugin(FakeContext())
        plugin.clock, plugin.scheduler = FakeClock(), Scheduler()
        plugin._clock = plugin.clock
        plugin._harbor = HarborCache(plugin._harbor_runner, 4.0, clock=plugin.clock, start_thread=plugin.scheduler)
        yield plugin


def pin(branch: str, oid: str, *, legacy: bool = False, at: int = 1) -> dict:
    """One archive pin as `archive.pins_for` returns it."""
    prefix = "refs/voidcraft/archived/" if legacy else "refs/tldrack/archived/"
    return {"ref": prefix + branch, "branch": branch, "oid": oid, "committed_at": at, "subject": "s"}


def ready(plugin) -> dict:
    plugin._harbor.refresh_synchronously()
    _status, body = plugin.handle_get("/branches", {})
    return body


#: A name `names.validate_existing_branch` rejects (git refuses a space in a
#: ref). Merged and unheld, so every OTHER row of the table says ok — which is
#: what makes it a test of the NAME row and nothing else.
INVALID_NAME = "bad name"
INVALID_NAME_ROW = row(INVALID_NAME, "0badc0ffee", full("0badc0ffee"), "", "", "", "", "0 12", "", "1780000000", AUTHOR, "plan")

#: A real branch on the measured machine, and the regression that split the
#: validator: git lists it and `git branch -d` takes it, but the CREATE
#: alphabet has no `+`, so the harbor showed it as an undeletable atoll.
PLUS_NAME = "fix-+-retry"
PLUS_NAME_ROW = row(PLUS_NAME, "0badc0ffee", full("0badc0ffee"), "", "", "", "", "0 12", "", "1780000000", AUTHOR, "plan")


def sweep_from(text: str):
    """A `sweep_refs` stand-in answering from `text` with rung one's shape."""
    return lambda _root, base, **_kw: (
        branches_mod.parse_for_each_ref(text, base, ahead_behind=True),
        {"ahead_behind": True},
        None,
    )


def by_branch(body: dict) -> dict[str, dict]:
    return {row["branch"]: row for row in body["branches"]}


#: `fill-docstring-gaps-now` is 2 ahead of main in the fixture, unheld: the
#: exact shape of a squash-merged branch (`a lane branch` was 5 ahead).
SQUASHED = "fill-docstring-gaps-now"
SQUASHED_TIP = full("a1b2c3d05")


def squash_pr(head_oid: str, merged_at: str = "2036-01-01T00:00:00Z", base_ref: str = "main") -> dict:
    """A PR index naming `SQUASHED` as merged PR #57 with head `head_oid`."""
    return branches_mod.parse_pr_list(
        json.dumps(
            [
                {
                    "headRefName": SQUASHED,
                    "number": 57,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergedAt": merged_at,
                    "updatedAt": merged_at,
                    "url": "u",
                    "headRefOid": head_oid,
                    "baseRefName": base_ref,
                }
            ]
        )
    )


class TestRoutes:
    def test_the_four_harbor_routes_are_advertised(self, plugin):
        routes = plugin.routes()
        for name in ("branches", "fetch", "delete-branch", "prune-worktrees"):
            assert f"/plugin/worktree/{name}" in routes

    def test_unknown_routes_name_the_new_ones(self, plugin):
        assert "/branches" in plugin.handle_get("/nope", {})[1]["error"]
        assert "/delete-branch" in plugin.handle_post("/nope", {}, {})[1]["error"]


class TestBranchesWarming:
    def test_a_cold_get_answers_warming_and_schedules_one_runner(self, plugin):
        status, body = plugin.handle_get("/branches", {})
        assert status == 200
        assert body["status"] == "warming"
        assert body["branches"] == [] and body["worktrees"] == []
        assert body["generated_at"] is None and body["git_ok"] is None
        assert body["base"] == "main" and body["pr_remote"] == "origin"
        assert body["fetch"] == {"status": "idle", "step": None, "error": None, "at": None}
        assert body["pr_index"] == {
            "fetched_at": None,
            "count": 0,
            "error": None,
            "truncated": False,
            "stale": True,
            "refreshing": False,
        }
        assert len(plugin.scheduler.targets) == 1

    def test_two_cold_gets_schedule_one_runner(self, plugin):
        plugin.handle_get("/branches", {})
        plugin.handle_get("/branches", {})
        assert len(plugin.scheduler.targets) == 1

    def test_the_warming_body_holds_no_home_path(self, plugin):
        _s, body = plugin.handle_get("/branches", {})
        assert HOME not in json.dumps(body)


class TestRemoteBranchTier:
    """Branches that live only on a remote — invisible to the local sweep, and
    read-only here because deleting one is a network write."""

    def rows(self, plugin) -> list[dict]:
        return ready(plugin)["remote_branches"]

    def test_only_branches_with_no_local_ref_appear(self, plugin):
        names = {r["branch"] for r in self.rows(plugin)}
        # In the fixture: `test123` is a local island, so it is NOT repeated
        # here; the rest of the remote refs have no local counterpart.
        assert "test123" not in names
        assert names == {"rss-item-entity", "claude/feature/web-components", "unlanded-thing", "feat/end-to-end-validation"}

    def test_the_head_symref_and_the_remote_base_never_appear(self, plugin):
        refs = {r["ref"] for r in self.rows(plugin)}
        assert "origin" not in refs and "origin/main" not in refs

    def test_a_row_carries_its_remote_and_its_landed_state(self, plugin):
        row_ = next(r for r in self.rows(plugin) if r["branch"] == "rss-item-entity")
        assert row_["remote"] == "origin" and row_["ref"] == "origin/rss-item-entity"
        assert row_["merged"] is True and row_["behind"] == 240
        assert row_["author"] == AUTHOR and row_["subject"]

    def test_rows_carry_no_delete_verb_at_all(self, plugin):
        # The invariant: `decide_delete` reasons about a LOCAL ref against a
        # LOCAL base, and `git push --delete` is a different verb this plugin
        # does not implement. A verdict field here would promise one.
        for row_ in self.rows(plugin):
            assert "delete_verdict" not in row_ and "delete_reason" not in row_
            assert "session" not in row_ and "worktree" not in row_

    def test_a_pr_is_attached_by_branch_name(self, plugin):
        # `gh` keys PRs by head branch NAME, with no remote prefix, so the
        # index joins to this tier unchanged — a merged PR whose branch was
        # never deleted on the remote is most of what lands here.
        plugin._pr_index.replace(branches_mod.parse_pr_list(PR_LIST_JSON), 1788600000.0)
        rows = {r["branch"]: r for r in self.rows(plugin)}
        assert rows["feat/end-to-end-validation"]["pr"]["number"] == 32
        assert rows["feat/end-to-end-validation"]["pr"]["state"] == "merged"
        assert rows["rss-item-entity"]["pr"] is None

    def test_newest_first(self, plugin):
        stamps = [r["committed_at"] for r in self.rows(plugin)]
        assert stamps == sorted(stamps, reverse=True)

    def test_a_warming_body_carries_the_empty_tier(self, plugin):
        _status, body = plugin.handle_get("/branches", {})
        assert body["status"] == "warming"
        assert body["remote_branches"] == [] and body["remote_error"] is None

    def test_a_remote_failure_is_reported_without_emptying_the_document(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.sweep_remote_refs", lambda *_a, **_kw: ([], "gitlab: `git` timed out after 10s"))
        body = ready(plugin)
        assert body["status"] == "ready" and body["git_ok"] is True
        assert len(body["branches"]) == 10 and body["remote_branches"] == []
        assert "timed out" in body["remote_error"]

    def test_the_remote_error_is_home_scrubbed(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.branches.sweep_remote_refs", lambda *_a, **_kw: ([], f"origin: fatal: cannot change to '{HOME}/gone'")
        )
        assert ready(plugin)["remote_error"] == "origin: fatal: cannot change to '~/gone'"


class TestBranchesReady:
    def test_ready_after_the_runner_lands(self, plugin):
        plugin.handle_get("/branches", {})
        plugin.scheduler.run()
        _s, body = plugin.handle_get("/branches", {})
        assert body["status"] == "ready" and body["git_ok"] is True
        assert body["git_error"] is None
        assert body["capabilities"] == {"ahead_behind": True, "gh": True}
        assert body["age_seconds"] == 0.0 and body["error"] is None
        assert isinstance(body["generated_at"], float)

    def test_rows_and_worktrees_are_all_there(self, plugin):
        body = ready(plugin)
        assert len(body["branches"]) == 10
        assert len(body["worktrees"]) == 6
        assert body["stash_count"] == 1
        assert body["repo"] == "~/code/demo-app"

    def test_no_absolute_home_path_anywhere_in_the_body(self, plugin):
        assert HOME not in json.dumps(ready(plugin))


class TestNoHomePathOnAnyErrorPath:
    """The `~`-only invariant covers TEXT, not only path fields: every error
    string a subprocess can print reaches the body through `_branches`, and
    each is scrubbed there (`names.scrub_home`)."""

    LEAK = f"fatal: cannot change to '{HOME}/no-such-repo-xyz': No such file or directory"

    def test_a_sweep_error_document(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda *_a, **_kw: ([], {"ahead_behind": True}, self.LEAK))
        body = ready(plugin)
        assert body["status"] == "ready" and body["git_ok"] is False
        assert body["git_error"] == "fatal: cannot change to '~/no-such-repo-xyz': No such file or directory"
        assert HOME not in json.dumps(body)

    def test_a_raising_runner(self, plugin, monkeypatch):
        ready(plugin)
        monkeypatch.setattr(
            "worktree.branches.sweep_refs", lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError(self.LEAK))
        )
        plugin.clock.advance(4.0)
        plugin.handle_get("/branches", {})
        plugin.scheduler.run()
        _s, body = plugin.handle_get("/branches", {})
        assert body["error"].startswith("FileNotFoundError: fatal: cannot change to '~/")
        assert HOME not in json.dumps(body)

    def test_a_raising_runner_while_warming(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError(self.LEAK)))
        plugin.handle_get("/branches", {})
        plugin.scheduler.run()
        _s, body = plugin.handle_get("/branches", {})
        assert body["status"] == "warming" and "~/no-such-repo-xyz" in body["error"]
        assert HOME not in json.dumps(body)

    def test_a_failed_fetch(self, plugin, monkeypatch):
        plugin._fetch_jobs.claim()
        plugin._fetch_jobs.fail(f"fatal: '{HOME}/code/demo-app' is not a git repository")
        _s, body = plugin.handle_get("/branches", {})
        assert body["fetch"]["error"] == "fatal: '~/code/demo-app' is not a git repository"
        assert HOME not in json.dumps(body)

    def test_a_failed_pr_index(self, plugin):
        plugin._pr_index.set_error(f"could not find git repo at {HOME}/code/demo-app")
        _s, body = plugin.handle_get("/branches", {})
        assert body["pr_index"]["error"] == "could not find git repo at ~/code/demo-app"
        assert HOME not in json.dumps(body)

    def test_a_failed_pr_job_and_a_failed_create(self, plugin):
        ready(plugin)
        plugin._store.set("test123", status="error", error=f"gh: {HOME}/code/demo-app has no remote")
        plugin._creates.claim("old-feature")
        plugin._creates.fail("old-feature", f"fatal: '{MANAGED}/old-feature' already exists")
        _s, body = plugin.handle_get("/branches", {})
        rows = by_branch(body)
        assert rows["test123"]["pr_job"]["error"] == "gh: ~/code/demo-app has no remote"
        assert rows["old-feature"]["create_error"] == "fatal: '~/worktrees/demo-app/old-feature' already exists"
        assert HOME not in json.dumps(body)

    def test_a_ready_body_with_every_error_at_once(self, plugin, monkeypatch):
        ready(plugin)
        plugin._fetch_jobs.claim()
        plugin._fetch_jobs.fail(self.LEAK)
        plugin._pr_index.set_error(self.LEAK)
        plugin._store.set("test123", status="error", error=self.LEAK)
        plugin._creates.claim("old-feature")
        plugin._creates.fail("old-feature", self.LEAK)
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError(self.LEAK)))
        plugin.clock.advance(4.0)
        plugin.handle_get("/branches", {})
        plugin.scheduler.run()
        _s, body = plugin.handle_get("/branches", {})
        assert body["status"] == "ready" and len(body["branches"]) == 10
        assert HOME not in json.dumps(body)

    def test_the_base_row(self, plugin):
        main = by_branch(ready(plugin))["main"]
        assert main["is_base"] is True and main["merged"] is True
        assert main["session"] == "wt-main"
        assert main["worktree"]["is_main"] is True
        assert main["worktree"]["path"] == "~/code/demo-app"
        assert main["worktree"]["managed"] is False
        assert main["delete_verdict"] == "refused" and "base branch" in main["delete_reason"]
        assert main["upstream"]["kind"] == "own"

    def test_a_claude_code_worktree_row_is_not_ours(self, plugin):
        row = by_branch(ready(plugin))["add-stopwatch-feed"]
        assert row["worktree"]["path"] == "~/other-tool/worktrees/spike-one/demo-app"
        assert row["worktree"]["managed"] is False and row["worktree"]["is_main"] is False
        assert row["delete_verdict"] == "refused"
        assert row["delete_reason"].endswith("that worktree is not ours to remove")
        assert "worktree_removable" not in row

    def test_a_managed_worktree_row_says_remove_it_first(self, plugin):
        row = by_branch(ready(plugin))["pr-demo"]
        assert row["worktree"]["managed"] is True
        assert row["worktree"]["dirty"] is True and row["worktree"]["locked"] is True
        assert row["delete_verdict"] == "refused"
        assert row["delete_reason"] == "checked out at ~/worktrees/demo-app/pr-demo — remove the worktree first"
        assert row["worktree_removable"] is True

    def test_an_unmerged_unheld_row_needs_force(self, plugin):
        row = by_branch(ready(plugin))["fill-docstring-gaps-now"]
        assert row["worktree"] is None
        assert row["delete_verdict"] == "needs_force"
        assert row["delete_reason"] == "2 commits are not on main"
        assert row["upstream"] == {
            "ref": "gitlab/main",
            "remote": "gitlab",
            "branch": "main",
            "kind": "base",
            "ahead": 2,
            "behind": 213,
            "gone": False,
        }

    def test_a_merged_unheld_row_is_ok(self, plugin):
        row = by_branch(ready(plugin))["test123"]
        assert row["delete_verdict"] == "ok" and row["delete_reason"] is None
        assert row["upstream"] is None and row["pr"] is None and row["pr_job"] is None

    def test_a_branch_the_validator_rejects_is_refused_with_its_reason(self, plugin, monkeypatch):
        # Merged, unheld, and still never `ok`: the route would answer 400.
        from worktree.names import validate_existing_branch

        monkeypatch.setattr("worktree.branches.sweep_refs", sweep_from(FIXTURE + INVALID_NAME_ROW + "\n"))
        row = by_branch(ready(plugin))[INVALID_NAME]
        assert row["merged"] is True and row["worktree"] is None
        assert row["delete_verdict"] == "refused"
        assert row["delete_reason"] == validate_existing_branch(INVALID_NAME)
        assert "worktree_removable" not in row

    def test_a_plus_in_the_name_reaches_the_row_as_ok(self, plugin, monkeypatch):
        # End to end for the split: the sweep, the row assembly and the
        # verdict all carry a `+` now, so the island offers the delete.
        monkeypatch.setattr("worktree.branches.sweep_refs", sweep_from(FIXTURE + PLUS_NAME_ROW + "\n"))
        row = by_branch(ready(plugin))[PLUS_NAME]
        assert row["merged"] is True and row["worktree"] is None
        assert row["delete_verdict"] == "ok" and row["delete_reason"] is None

    def test_a_prunable_worktree_joins_by_path_and_is_flagged(self, plugin):
        body = ready(plugin)
        row = by_branch(body)["stale-wt"]
        assert row["worktree"]["prunable"] is True and row["worktree"]["managed"] is True
        assert row["worktree"]["dirty"] is None  # never probed: the dir is gone
        info = next(w for w in body["worktrees"] if w["branch"] == "stale-wt")
        assert info["prunable_reason"] == "gitdir file points to non-existent location"

    def test_a_worktree_the_ref_did_not_name_still_joins_by_branch(self, plugin, monkeypatch):
        # `%(worktreepath)` for a prunable worktree is unverified; the join
        # must also work from `git worktree list`'s side.
        text = FIXTURE.replace(PRUNABLE_DIR, "")
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _r, base, **_kw: (
                branches_mod.parse_for_each_ref(text, base, ahead_behind=True),
                {"ahead_behind": True},
                None,
            ),
        )
        row = by_branch(ready(plugin))["stale-wt"]
        assert row["worktree"]["path"] == "~/worktrees/demo-app/stale-wt"

    def test_the_detached_worktree_is_listed_with_no_branch(self, plugin):
        info = next(w for w in ready(plugin)["worktrees"] if w["detached"])
        assert info["branch"] is None and info["managed"] is False
        assert info["path"] == "~/other-tool/worktrees/spike/demo-app"

    def test_dirty_is_probed_from_existing_non_prunable_worktrees_only(self, plugin, monkeypatch):
        seen = []
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _b, **_kw: (seen.extend(paths), {})[1])
        ready(plugin)
        assert PRUNABLE_DIR not in seen and len(seen) == 5

    def test_the_dirty_tier_keeps_its_own_ttl(self, plugin, monkeypatch):
        calls = []
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _b, **_kw: (calls.append(1), {})[1])
        ready(plugin)
        plugin.clock.advance(4.0)
        ready(plugin)
        assert len(calls) == 1  # the 4 s sweep did not re-probe
        plugin.clock.advance(16.0)
        ready(plugin)
        assert len(calls) == 2

    def test_pr_rows_come_from_the_index(self, plugin):
        plugin._pr_index.replace(branches_mod.parse_pr_list(PR_LIST_JSON), 1788600000.0)
        body = ready(plugin)
        assert by_branch(body)["pr-demo"]["pr"]["state"] == "merged"
        assert by_branch(body)["add-stopwatch-feed"]["pr"]["number"] == 40
        assert body["pr_index"]["count"] == 4 and body["pr_index"]["fetched_at"] == 1788600000.0

    def test_a_cut_pr_list_is_stale_however_fresh(self, plugin):
        # No refresh brings the oldest PRs back, so the chip must stay up.
        plugin._pr_index.replace(branches_mod.parse_pr_list(PR_LIST_JSON), time.time(), truncated=True)
        block = ready(plugin)["pr_index"]
        assert block["truncated"] is True and block["stale"] is True
        plugin._pr_index.replace(branches_mod.parse_pr_list(PR_LIST_JSON), time.time())
        assert ready(plugin)["pr_index"]["stale"] is False

    def test_a_squash_landed_branch_is_landed_and_deletable(self, plugin):
        # The bug this row exists for: 2 "ahead" commits that shipped as one.
        plugin._pr_index.replace(squash_pr(SQUASHED_TIP), 1.0)
        row = by_branch(ready(plugin))[SQUASHED]
        assert row["merged"] is False and row["ahead"] == 2
        assert row["landed"] is True and row["pr"]["number"] == 57
        assert row["delete_verdict"] == "ok" and row["delete_reason"] is None

    def test_every_row_says_landed_and_an_ancestor_needs_no_pr(self, plugin):
        rows = by_branch(ready(plugin))
        assert all(isinstance(r["landed"], bool) for r in rows.values())
        assert rows["test123"]["landed"] is True and rows["test123"]["pr"] is None
        assert rows[SQUASHED]["landed"] is False

    def test_a_merged_pr_that_shipped_another_head_still_needs_force(self, plugin, monkeypatch):
        rec = Recorder({("git", "-C", REPO, "merge-base"): (1, "", "")})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        plugin._pr_index.replace(squash_pr(full("eeee44444")), 1.0)
        row = by_branch(ready(plugin))[SQUASHED]
        assert row["landed"] is False and row["delete_verdict"] == "needs_force"
        assert [c[0] for c in rec.calls] == [["git", "-C", REPO, "merge-base", "--is-ancestor", SQUASHED_TIP, full("eeee44444")]]

    def test_the_ancestry_probe_is_paid_once_across_sweeps(self, plugin, monkeypatch):
        rec = Recorder({("git", "-C", REPO, "merge-base"): (0, "", "")})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        plugin._pr_index.replace(squash_pr(full("eeee44444")), 1.0)
        assert by_branch(ready(plugin))[SQUASHED]["landed"] is True  # behind the PR head
        plugin.clock.advance(60.0)
        assert by_branch(ready(plugin))[SQUASHED]["landed"] is True
        assert len(rec.calls) == 1

    def test_a_missing_base_is_a_ready_document_with_git_error(self, plugin, monkeypatch):
        # Never an empty harbor that warms forever.
        monkeypatch.setattr(
            "worktree.branches.sweep_refs", lambda _r, base, **_kw: ([], {"ahead_behind": True}, f"base branch {base} not found")
        )
        body = ready(plugin)
        assert body["status"] == "ready" and body["git_ok"] is False
        assert body["git_error"] == "base branch main not found"
        assert body["branches"] == [] and len(body["worktrees"]) == 6

    def test_old_git_yields_merged_unknown_everywhere_and_refuses(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _r, base, **_kw: (
                [
                    {**row, "ahead": None, "behind": None, "merged": None}
                    for row in branches_mod.parse_for_each_ref(FIXTURE, base, ahead_behind=True)
                ],
                {"ahead_behind": False},
                None,
            ),
        )
        body = ready(plugin)
        assert body["capabilities"] == {"ahead_behind": False, "gh": True}
        row = by_branch(body)["test123"]
        assert row["merged"] is None and row["delete_verdict"] == "refused"
        assert row["delete_reason"] == branches_mod.UNKNOWN_MERGED_REASON

    def test_a_raising_runner_keeps_the_last_document(self, plugin, monkeypatch):
        body = ready(plugin)
        assert body["status"] == "ready"
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
        plugin.clock.advance(4.0)
        plugin.handle_get("/branches", {})
        plugin.scheduler.run()
        _s, body = plugin.handle_get("/branches", {})
        assert body["status"] == "ready" and len(body["branches"]) == 10
        assert body["error"] == "RuntimeError: boom"

    def test_the_get_never_runs_the_sweep_on_the_caller(self, plugin, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "worktree.branches.sweep_refs", lambda _r, base, **_kw: (calls.append(1), [], {"ahead_behind": True}, None)[1:]
        )
        plugin.handle_get("/branches", {})
        assert calls == []
        plugin.scheduler.run()
        assert calls == [1]


class TestMergeJobs:
    def test_nothing_to_merge_returns_THE_SAME_LIST(self, plugin):
        rows = ready(plugin)["branches"]
        assert plugin._merge_jobs(rows) is rows

    def test_a_pr_job_is_merged_after_the_cache(self, plugin):
        ready(plugin)
        plugin._store.set("pr-demo", status="opening")
        _s, body = plugin.handle_get("/branches", {})  # no refresh, no invalidate
        assert by_branch(body)["pr-demo"]["pr_job"] == {"status": "opening", "url": None, "error": None}

    def test_create_progress_is_merged_and_refuses_delete(self, plugin):
        ready(plugin)
        plugin._creates.claim("test123")
        plugin._creates.advance("test123", "bootstrap")
        _s, body = plugin.handle_get("/branches", {})
        row = by_branch(body)["test123"]
        assert row["create_status"] == "creating" and row["create_step"] == "bootstrap"
        assert row["delete_verdict"] == "refused"
        assert row["delete_reason"] == branches_mod.CREATING_REASON

    def test_the_cached_rows_are_never_mutated(self, plugin):
        ready(plugin)
        plugin._creates.claim("test123")
        plugin.handle_get("/branches", {})
        plugin._creates.finish("test123")
        _s, body = plugin.handle_get("/branches", {})
        row = by_branch(body)["test123"]
        assert "create_status" not in row and row["delete_verdict"] == "ok"

    def test_untouched_rows_gain_no_create_fields(self, plugin):
        ready(plugin)
        plugin._creates.claim("test123")
        _s, body = plugin.handle_get("/branches", {})
        assert "create_status" not in by_branch(body)["main"]


class TestManagedIsUntouched:
    """`_managed()` is the write gate for `/pr` and `/remove`. The unfiltered
    read must not widen it: same rows before and after, and never a tree
    under ~/other-tool/worktrees/."""

    def test_same_output_before_and_after_the_new_reads(self, plugin):
        before = plugin._managed()
        plugin.handle_get("/state", {})
        ready(plugin)
        plugin.handle_get("/branches", {})
        after = plugin._managed()
        assert before == after
        assert [r["branch"] for r in after] == ["pr-demo", "feat/cache-all-pages", "stale-wt"]

    def test_it_never_lists_a_claude_code_tree_or_a_detached_one(self, plugin):
        paths = [r["path"] for r in plugin._managed()]
        assert all(p.startswith(MANAGED) for p in paths)
        assert not any("other-tool/worktrees" in p for p in paths)


class TestStateWorktreePaths:
    def test_every_non_main_worktree_collapsed_and_unfiltered(self, plugin):
        _s, body = plugin.handle_get("/state", {})
        assert body["worktree_paths"] == [
            "~/other-tool/worktrees/spike-one/demo-app",
            "~/worktrees/demo-app/pr-demo",
            "~/worktrees/demo-app/cache-all-pages",
            "~/worktrees/demo-app/stale-wt",
            "~/other-tool/worktrees/spike/demo-app",
        ]

    def test_trees_carry_each_path_with_its_branch(self, plugin):
        _s, body = plugin.handle_get("/state", {})
        assert body["worktree_trees"] == [
            {"path": "~/other-tool/worktrees/spike-one/demo-app", "branch": "add-stopwatch-feed", "pr": None, "landing": None},
            {"path": "~/worktrees/demo-app/pr-demo", "branch": "pr-demo", "pr": None, "landing": {"how": "ancestor", "pr": None}},
            {"path": "~/worktrees/demo-app/cache-all-pages", "branch": "feat/cache-all-pages", "pr": None, "landing": None},
            {
                "path": "~/worktrees/demo-app/stale-wt",
                "branch": "stale-wt",
                "pr": None,
                "landing": {"how": "ancestor", "pr": None},
            },
            {"path": "~/other-tool/worktrees/spike/demo-app", "branch": None, "pr": None, "landing": None},  # detached
        ]
        assert body["pr_known"] is False, "no index fetched yet: every null is unknown, never 'no PR'"
        assert [tree["path"] for tree in body["worktree_trees"]] == body["worktree_paths"]

    def test_the_existing_keys_are_unchanged(self, plugin):
        _s, body = plugin.handle_get("/state", {})
        # Additive only: a frozen client still reads every older key.
        assert set(body) == {
            "worktrees",
            "root",
            "base",
            "worktree_paths",
            "worktree_trees",
            "capabilities",
            "pr_known",
            "task_groups",
            "repo_key",
        }
        assert [r["branch"] for r in body["worktrees"]] == ["pr-demo", "feat/cache-all-pages", "stale-wt"]
        assert body["worktrees"][0]["path"] == f"{MANAGED}/pr-demo"  # /state stays absolute

    def test_each_tree_carries_its_pr_from_the_index_and_the_button(self, plugin):
        plugin._pr_index.replace(
            {
                "pr-demo": {"number": 41, "state": "open", "url": "https://github.com/o/r/pull/41"},
                "add-stopwatch-feed": {"number": 7, "state": "merged", "url": "https://github.com/o/r/pull/7"},
                "stale-wt": {"number": 3, "state": "closed", "url": "https://github.com/o/r/pull/3"},
            },
            time.time(),
        )
        plugin._store.set("feat/cache-all-pages", status="open", url="https://github.com/o/r/pull/99")
        _s, body = plugin.handle_get("/state", {})
        prs = {tree["branch"]: tree["pr"] for tree in body["worktree_trees"]}
        assert body["pr_known"] is True
        assert prs["pr-demo"] == {"number": 41, "state": "open", "url": "https://github.com/o/r/pull/41"}
        assert prs["add-stopwatch-feed"]["state"] == "merged", "any tree, not only the managed ones"
        assert prs["stale-wt"]["state"] == "closed"
        assert prs["feat/cache-all-pages"] == {"number": None, "state": "open", "url": "https://github.com/o/r/pull/99"}, (
            "the button's PR shows before the index's next refresh"
        )
        assert prs[None] is None

    def test_it_rides_the_same_ttl_cache(self, plugin, monkeypatch):
        plugin.handle_get("/state", {})
        monkeypatch.setattr("worktree.plugin.list_worktrees", lambda _r: [])
        _s, body = plugin.handle_get("/state", {})
        assert len(body["worktree_paths"]) == 5  # cached with the rows


class Recorder:
    """A `run_bounded` stand-in scripted per command prefix."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def __call__(self, command, timeout, **kwargs):
        self.calls.append((command, timeout, kwargs))
        for prefix, answer in self.answers.items():
            if tuple(command[: len(prefix)]) == prefix:
                return answer
        raise AssertionError(f"unscripted subprocess: {command}")


class TestDeleteBranch:
    def delete(self, plugin, monkeypatch, body: dict, *, git=None):
        recorder = Recorder(git or {})
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        return plugin.handle_post("/delete-branch", {}, body), recorder

    def test_a_bad_name_is_400(self, plugin, monkeypatch):
        (status, _b), rec = self.delete(plugin, monkeypatch, {"branch": "bad name!"})
        assert status == 400 and rec.calls == []

    def test_a_leading_dash_is_400_and_never_reaches_git(self, plugin, monkeypatch):
        # Option injection is the one argv hazard the existing-ref rule owns:
        # `git branch -d -D` names an option, not a branch.
        (status, _b), rec = self.delete(plugin, monkeypatch, {"branch": "-D"})
        assert status == 400 and rec.calls == []

    def test_a_real_branch_with_an_invalid_name_is_400_and_never_swept(self, plugin, monkeypatch):
        # The row said `refused` for it (TestBranchesReady); the route agrees,
        # from the same table, without spending a fork on a name it refuses.
        from worktree.names import validate_existing_branch

        calls = []
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda *a, **kw: (calls.append(1), sweep_from(FIXTURE + INVALID_NAME_ROW + "\n")(*a, **kw))[1],
        )
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": INVALID_NAME, "force": True})
        assert status == 400 and body == {"error": validate_existing_branch(INVALID_NAME)}
        assert calls == [] and rec.calls == []

    def test_a_plus_name_is_swept_and_deleted(self, plugin, monkeypatch):
        # The other half of the regression: the route must now spend the fork
        # and run `git branch -d` for a name it used to 400 on. The `+` also
        # has to survive as ONE argv element — it is not shell-quoted anywhere.
        monkeypatch.setattr("worktree.branches.sweep_refs", sweep_from(FIXTURE + PLUS_NAME_ROW + "\n"))
        (status, body), rec = self.delete(
            plugin, monkeypatch, {"branch": PLUS_NAME}, git={("git", "-C", REPO, "branch", "-d"): (0, "", "")}
        )
        assert status == 200 and body == {"branch": PLUS_NAME, "deleted": True, "forced": False, "archived": None}
        assert rec.calls[0][0] == ["git", "-C", REPO, "branch", "-d", PLUS_NAME]

    def test_a_sweep_error_body_is_home_scrubbed(self, plugin, monkeypatch):
        leak = f"fatal: cannot change to '{HOME}/gone': No such file or directory"
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda *_a, **_kw: ([], {"ahead_behind": True}, leak))
        (status, body), _rec = self.delete(plugin, monkeypatch, {"branch": "test123"})
        assert status == 500 and body["error"] == "fatal: cannot change to '~/gone': No such file or directory"

    def test_a_git_failure_body_is_home_scrubbed(self, plugin, monkeypatch):
        (status, body), _rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": "test123"},
            git={("git",): (128, "", f"fatal: {HOME}/code/demo-app/.git/index.lock exists")},
        )
        assert status == 500 and body["error"] == "fatal: ~/code/demo-app/.git/index.lock exists"

    def test_unknown_is_404(self, plugin, monkeypatch):
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "never-heard-of"})
        assert status == 404 and body["error"] == "no local branch by that name"
        assert rec.calls == []

    def test_base_is_409_even_with_force(self, plugin, monkeypatch):
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "main", "force": True})
        assert status == 409 and "base branch" in body["error"] and rec.calls == []

    def test_a_claude_code_checkout_is_409_even_with_force(self, plugin, monkeypatch):
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "add-stopwatch-feed", "force": True})
        assert status == 409
        assert body["error"] == "checked out at ~/other-tool/worktrees/spike-one/demo-app — that worktree is not ours to remove"
        assert rec.calls == []

    def test_a_managed_checkout_is_409_with_worktree_removable(self, plugin, monkeypatch):
        (status, body), _rec = self.delete(plugin, monkeypatch, {"branch": "pr-demo"})
        assert status == 409 and body["worktree_removable"] is True
        assert body["error"] == "checked out at ~/worktrees/demo-app/pr-demo — remove the worktree first"

    def test_merged_unknown_is_409_even_with_force(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _r, base, **_kw: (
                [
                    {**row, "ahead": None, "behind": None, "merged": None}
                    for row in branches_mod.parse_for_each_ref(FIXTURE, base, ahead_behind=True)
                ],
                {"ahead_behind": False},
                None,
            ),
        )
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "test123", "force": True})
        assert status == 409 and body["error"] == branches_mod.UNKNOWN_MERGED_REASON
        assert rec.calls == []

    def test_a_creating_branch_is_409_even_with_force(self, plugin, monkeypatch):
        plugin._creates.claim("test123")
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "test123", "force": True})
        assert status == 409 and body["error"] == branches_mod.CREATING_REASON
        assert rec.calls == []

    def test_unmerged_without_force_is_409_needs_force_with_ahead(self, plugin, monkeypatch):
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "fill-docstring-gaps-now"})
        assert status == 409 and body["needs_force"] is True and body["ahead"] == 2
        assert rec.calls == []

    def test_a_squash_landed_branch_runs_dash_D_without_being_forced(self, plugin, monkeypatch):
        # `git branch -d` is the ancestor test and would refuse it; the table
        # already proved what `-d` cannot see. `forced` stays the CALLER's word.
        plugin._pr_index.replace(squash_pr(SQUASHED_TIP), 1.0)
        (status, body), rec = self.delete(
            plugin, monkeypatch, {"branch": SQUASHED}, git={("git", "-C", REPO, "branch", "-D"): (0, "Deleted branch", "")}
        )
        assert status == 200 and body == {"branch": SQUASHED, "deleted": True, "forced": False, "archived": None}, (
            "a landed delete pins nothing"
        )
        assert rec.calls[0][0] == ["git", "-C", REPO, "branch", "-D", SQUASHED]
        assert "landed via PR #57" in plugin._context.lines[-1]

    def test_a_merged_pr_that_shipped_another_head_is_409_needs_force(self, plugin, monkeypatch):
        plugin._pr_index.replace(squash_pr(full("eeee44444")), 1.0)
        (status, body), rec = self.delete(
            plugin, monkeypatch, {"branch": SQUASHED}, git={("git", "-C", REPO, "merge-base"): (1, "", "")}
        )
        assert status == 409 and body["needs_force"] is True and body["ahead"] == 2
        assert [c[0][3] for c in rec.calls] == ["merge-base"], "never reached `git branch`"

    def test_the_click_re_judges_landed_on_a_fresh_sweep(self, plugin, monkeypatch):
        # The cached row said landed; a commit since moved the tip off the PR head.
        plugin._pr_index.replace(squash_pr(SQUASHED_TIP), 1.0)
        assert by_branch(ready(plugin))[SQUASHED]["delete_verdict"] == "ok"
        moved = FIXTURE.replace(SQUASHED_TIP, full("feed5"))
        monkeypatch.setattr("worktree.branches.sweep_refs", sweep_from(moved))
        (status, body), rec = self.delete(
            plugin, monkeypatch, {"branch": SQUASHED}, git={("git", "-C", REPO, "merge-base"): (1, "", "")}
        )
        assert status == 409 and body["needs_force"] is True
        assert all(c[0][3] != "branch" for c in rec.calls)

    def test_unmerged_with_force_pins_the_tip_then_runs_dash_D(self, plugin, monkeypatch):
        (status, body), rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={("git", "-C", REPO, "update-ref"): (0, "", ""), ("git", "-C", REPO, "branch", "-D"): (0, "Deleted branch", "")},
        )
        assert status == 200 and body == {
            "branch": SQUASHED,
            "deleted": True,
            "forced": True,
            "archived": f"refs/tldrack/archived/{SQUASHED}",
        }
        pin, delete = rec.calls[0][0], rec.calls[1][0]
        assert pin[3:4] == ["update-ref"] and pin[-3:] == [f"refs/tldrack/archived/{SQUASHED}", SQUASHED_TIP, "0" * 40], (
            "must not exist yet"
        )
        assert delete == ["git", "-C", REPO, "branch", "-D", SQUASHED]
        assert "tip pinned at refs/tldrack/archived/" in plugin._context.lines[-1]

    def test_a_pin_that_cannot_be_written_refuses_the_delete(self, plugin, monkeypatch):
        (status, body), rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={("git", "-C", REPO, "update-ref"): (128, "", f"fatal: {HOME}/x.lock exists")},
        )
        assert status == 500 and "not deleted" in body["error"] and HOME not in body["error"]
        assert [c[0][3] for c in rec.calls] == ["update-ref"], "never reached `git branch`"

    def test_a_divergent_old_pin_is_a_409_and_nothing_is_deleted(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, b: [pin(b, full("0dd"))])
        (status, body), rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={("git", "-C", REPO, "merge-base", "--is-ancestor"): (1, "", "")},
        )
        assert status == 409 and body["archive_conflict"] is True
        assert [c[0][3] for c in rec.calls] == ["merge-base"]

    def test_an_old_pin_inside_the_new_tip_is_overwritten_against_its_value(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, b: [pin(b, full("0dd"))])
        (status, _body), rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={
                ("git", "-C", REPO, "merge-base", "--is-ancestor"): (0, "", ""),
                ("git", "-C", REPO, "update-ref"): (0, "", ""),
                ("git", "-C", REPO, "branch", "-D"): (0, "", ""),
            },
        )
        assert status == 200
        assert rec.calls[1][0][-2:] == [SQUASHED_TIP, full("0dd")], "old-value guarded"

    def test_a_legacy_pin_inside_the_new_tip_is_kept_and_the_new_name_written_fresh(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, b: [pin(b, full("0dd"), legacy=True)])
        (status, body), rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={
                ("git", "-C", REPO, "merge-base", "--is-ancestor"): (0, "", ""),
                ("git", "-C", REPO, "update-ref"): (0, "", ""),
                ("git", "-C", REPO, "branch", "-D"): (0, "", ""),
            },
        )
        assert status == 200 and body["archived"] == f"refs/tldrack/archived/{SQUASHED}"
        assert rec.calls[1][0][-3:] == [f"refs/tldrack/archived/{SQUASHED}", SQUASHED_TIP, "0" * 40]

    def test_a_divergent_legacy_pin_still_refuses(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, b: [pin(b, full("0dd"), legacy=True)])
        (status, body), _rec = self.delete(
            plugin,
            monkeypatch,
            {"branch": SQUASHED, "force": True},
            git={("git", "-C", REPO, "merge-base", "--is-ancestor"): (1, "", "")},
        )
        assert status == 409 and body["archive_conflict"] is True

    def test_merged_runs_dash_d_and_answers_200(self, plugin, monkeypatch):
        (status, body), rec = self.delete(
            plugin, monkeypatch, {"branch": "test123"}, git={("git", "-C", REPO, "branch", "-d"): (0, "", "")}
        )
        assert status == 200 and body["forced"] is False
        assert rec.calls[0][0] == ["git", "-C", REPO, "branch", "-d", "test123"]
        assert rec.calls[0][1] == branches_mod.GIT_LOCAL_TIMEOUT_SECONDS

    def test_success_drops_the_pr_row_and_invalidates_the_harbor(self, plugin, monkeypatch):
        ready(plugin)
        plugin._store.set("test123", status="open", url="https://x/1")
        plugin._creates.claim("other")
        self.delete(plugin, monkeypatch, {"branch": "test123"}, git={("git", "-C", REPO, "branch", "-d"): (0, "", "")})
        assert plugin._store.get("test123") is None
        _doc, age, _e, _r = plugin._harbor.get()
        assert age is None  # invalidated: the next poll reschedules
        assert "deleted branch 'test123'" in plugin._context.lines[-1]

    def test_the_click_time_sweep_is_fresh_not_the_cache(self, plugin, monkeypatch):
        ready(plugin)  # cache says test123 is merged
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _r, base, **_kw: (
                [
                    {**row, "ahead": 1, "merged": False} if row["branch"] == "test123" else row
                    for row in branches_mod.parse_for_each_ref(FIXTURE, base, ahead_behind=True)
                ],
                {"ahead_behind": True},
                None,
            ),
        )
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "test123"})
        assert status == 409 and body["needs_force"] is True and rec.calls == []

    def test_gits_own_not_fully_merged_is_409_needs_force(self, plugin, monkeypatch):
        (status, body), _rec = self.delete(
            plugin, monkeypatch, {"branch": "test123"}, git={("git",): (1, "", "error: the branch 'test123' is not fully merged")}
        )
        assert status == 409 and body["needs_force"] is True

    def test_any_other_git_failure_is_500(self, plugin, monkeypatch):
        (status, body), _rec = self.delete(
            plugin, monkeypatch, {"branch": "test123"}, git={("git",): (128, "", "fatal: something else")}
        )
        assert status == 500 and "something else" in body["error"]

    def test_a_sweep_error_is_500_and_deletes_nothing(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.branches.sweep_refs", lambda *_a, **_kw: ([], {"ahead_behind": True}, "base branch main not found")
        )
        (status, body), rec = self.delete(plugin, monkeypatch, {"branch": "test123"})
        assert status == 500 and body["error"] == "base branch main not found"
        assert rec.calls == []

    def test_query_string_force_form(self, plugin, monkeypatch):
        (status, _b), rec = self.delete(plugin, monkeypatch, {"branch": "fill-docstring-gaps-now"})
        assert status == 409
        monkeypatch.setattr("worktree.create.run_bounded", Recorder({("git",): (0, "", "")}))
        status, body = plugin.handle_post("/delete-branch", {"force": ["1"]}, {"branch": "fill-docstring-gaps-now"})
        assert status == 200 and body["forced"] is True


class TestScuttleRemote:
    """`POST /scuttle-remote` — the remote tier's only write."""

    def scuttle(self, plugin, monkeypatch, body, *, remotes="origin\n", sweep=None, push=(0, "", ""), pr_json=None):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        answers = {
            ("git", "-C", REPO, "remote"): (0, remotes, ""),
            ("git", "-C", REPO, "for-each-ref"): (0, REMOTE_FIXTURE if sweep is None else sweep, ""),
            ("git", "-C", REPO, "push"): push,
        }
        rec = Recorder(answers)
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        if pr_json is not None:
            plugin._pr_index.replace(branches_mod.parse_pr_list(pr_json), 1.0)
        return plugin.handle_post("/scuttle-remote", {}, body), rec

    @staticmethod
    def pushes(rec):
        return [c for c in rec.calls if c[0][:1] == ["git"] and "push" in c[0]]

    def test_a_landed_branch_is_pushed_with_the_delete_refspec(self, plugin, monkeypatch):
        (status, body), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "rss-item-entity"})
        assert status == 200 and body == {"deleted": True, "ref": "origin/rss-item-entity"}
        command, timeout, kwargs = self.pushes(rec)[0]
        assert command == ["git", "-C", REPO, "push", "origin", "--delete", "rss-item-entity"]
        # Its OWN budget: the client must not give up before the server does.
        assert timeout == branches_mod.SCUTTLE_TIMEOUT_SECONDS
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_an_unlanded_branch_is_409_and_never_reaches_git_push(self, plugin, monkeypatch):
        (status, body), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "unlanded-thing"})
        assert status == 409 and body["error"] == branches_mod.REMOTE_UNLANDED_REASON
        assert self.pushes(rec) == []

    def test_there_is_no_force_that_unlocks_a_refusal(self, plugin, monkeypatch):
        # The whole safety argument is a predicate; a flag that overrode it
        # would be that argument with an off switch.
        for extra in ({"force": True}, {"force": "1"}):
            body = dict({"remote": "origin", "branch": "unlanded-thing"}, **extra)
            (status, _b), rec = self.scuttle(plugin, monkeypatch, body)
            assert status == 409 and self.pushes(rec) == []

    @staticmethod
    def merged_pr(head_oid: str, merged_at: str = "2036-01-01T00:00:00Z") -> str:
        return json.dumps(
            [
                {
                    "headRefName": "unlanded-thing",
                    "isDraft": False,
                    "mergedAt": merged_at,
                    "number": 7,
                    "state": "MERGED",
                    "updatedAt": merged_at,
                    "url": "u",
                    "headRefOid": head_oid,
                    "baseRefName": "main",
                }
            ]
        )

    def test_a_squash_merged_branch_is_allowed_though_it_is_no_ancestor(self, plugin, monkeypatch):
        # `unlanded-thing` is ahead 2 — the ancestor test says no. A merged PR
        # whose head IS this tip says the work landed anyway.
        (status, _b), rec = self.scuttle(
            plugin, monkeypatch, {"remote": "origin", "branch": "unlanded-thing"}, pr_json=self.merged_pr(full("cccc33333"))
        )
        assert status == 200 and len(self.pushes(rec)) == 1

    def test_a_merged_pr_that_shipped_a_different_head_is_refused(self, plugin, monkeypatch):
        # The stamp passes; the tip carries a commit the PR never shipped.
        monkeypatch.setattr(plugin, "_ancestry", branches_mod.AncestryCache(REPO, runner=lambda *_a, **_kw: (1, "", "")))
        (status, body), rec = self.scuttle(
            plugin, monkeypatch, {"remote": "origin", "branch": "unlanded-thing"}, pr_json=self.merged_pr(full("eeee44444"))
        )
        assert status == 409 and body["error"] == branches_mod.REMOTE_UNLANDED_REASON
        assert self.pushes(rec) == []

    def test_a_merged_pr_older_than_the_last_commit_is_still_refused(self, plugin, monkeypatch):
        # Merged, then pushed to again: real work the base has never seen —
        # refused on the stamp even where the index names this very tip.
        (status, _b), rec = self.scuttle(
            plugin,
            monkeypatch,
            {"remote": "origin", "branch": "unlanded-thing"},
            pr_json=self.merged_pr(full("cccc33333"), merged_at="1970-01-01T00:00:01Z"),
        )
        assert status == 409 and self.pushes(rec) == []

    def test_a_bad_name_is_400_before_any_subprocess(self, plugin, monkeypatch):
        (status, _b), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "bad name!"})
        assert status == 400 and rec.calls == []

    def test_a_leading_dash_is_400_and_never_reaches_the_argv(self, plugin, monkeypatch):
        # `git push` has no `--` separator for refspecs, so the name rule is
        # the only thing standing between a `-D` and the command line.
        (status, _b), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "--delete"})
        assert status == 400 and rec.calls == []

    def test_an_unconfigured_remote_is_400(self, plugin, monkeypatch):
        (status, body), rec = self.scuttle(plugin, monkeypatch, {"remote": "evil", "branch": "rss-item-entity"})
        assert status == 400 and "not a configured remote" in body["error"]
        assert self.pushes(rec) == []

    def test_a_branch_the_remote_does_not_have_is_404(self, plugin, monkeypatch):
        (status, _b), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "no-such-branch"})
        assert status == 404 and self.pushes(rec) == []

    def test_the_remote_base_is_never_scuttleable(self, plugin, monkeypatch):
        # `parse_remote_refs` drops `<remote>/<base>`, so it cannot be found.
        (status, _b), rec = self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "main"})
        assert status == 404 and self.pushes(rec) == []

    def test_a_ref_the_server_already_lost_is_the_outcome_not_a_failure(self, plugin, monkeypatch):
        (status, body), _rec = self.scuttle(
            plugin,
            monkeypatch,
            {"remote": "origin", "branch": "rss-item-entity"},
            push=(1, "", "error: unable to delete 'x': remote ref does not exist"),
        )
        assert status == 200 and body["deleted"] is False
        assert "already gone" in body["note"]

    def test_any_other_push_failure_is_500(self, plugin, monkeypatch):
        (status, body), _rec = self.scuttle(
            plugin,
            monkeypatch,
            {"remote": "origin", "branch": "rss-item-entity"},
            push=(1, "", "fatal: could not read from remote repository"),
        )
        assert status == 500 and "could not read from remote" in body["error"]

    def test_a_success_invalidates_the_harbor_so_the_row_goes(self, plugin, monkeypatch):
        ready(plugin)
        assert plugin._harbor.get()[1] is not None
        self.scuttle(plugin, monkeypatch, {"remote": "origin", "branch": "rss-item-entity"})
        assert plugin._harbor.get()[1] is None

    def test_the_route_is_registered(self, plugin):
        assert f"/plugin/{plugin.name}/scuttle-remote" in plugin.routes()

    def test_no_git_is_503(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: False)
        status, _b = plugin.handle_post("/scuttle-remote", {}, {"remote": "origin", "branch": "rss-item-entity"})
        assert status == 503


class TestFetch:
    def test_claims_once_and_answers_202_with_the_first_step(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        status, body = plugin.handle_post("/fetch", {}, {})
        assert status == 202 and body == {"status": "running", "step": FETCH_STEPS[0], "started": True}
        assert len(FakeThread.started) == 1

    def test_a_second_click_is_the_same_job(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        plugin.handle_post("/fetch", {}, {})
        status, body = plugin.handle_post("/fetch", {}, {})
        assert status == 202 and body["started"] is False
        assert len(FakeThread.started) == 1

    def test_no_git_is_503(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: False)
        assert plugin.handle_post("/fetch", {}, {})[0] == 503

    def test_a_thread_that_cannot_start_undoes_the_claim(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        FakeThread.fail_start = True
        status, _b = plugin.handle_post("/fetch", {}, {})
        assert status == 500
        job = plugin._fetch_jobs.get()
        assert job["status"] == "error" and "could not start" in job["error"]
        FakeThread.fail_start = False
        assert plugin.handle_post("/fetch", {}, {})[1]["started"] is True

    def run_worker(self, plugin, monkeypatch, *, git, gh=None, which=None, authed=False, remotes="origin\n", per_remote=None):
        """`git` scripts every fetch; `per_remote` overrides it for one remote,
        which is the only way to exercise a partial (secondary-only) failure."""
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.plugin.shutil.which", lambda _n: which)
        monkeypatch.setattr("worktree.pr.gh_authed", lambda: authed)
        answers = {("git", "-C", REPO, "remote"): (0, remotes, "")}
        for remote, answer in (per_remote or {}).items():
            answers[("git", "-C", REPO, "fetch", "--prune", remote)] = answer
        answers[("git", "-C", REPO, "fetch")] = git
        if gh is not None:
            answers[("gh",)] = gh
        recorder = Recorder(answers)
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        plugin.handle_post("/fetch", {}, {})
        FakeThread.started[-1].run_now()
        return recorder

    @staticmethod
    def fetches(rec):
        """The fetch calls only — `git remote` now runs first, so a positional
        index would pin these tests to the number of remotes."""
        return [call for call in rec.calls if call[0][:1] == ["git"] and "fetch" in call[0]]

    def test_the_git_half_runs_with_a_no_prompt_env_and_the_network_timeout(self, plugin, monkeypatch):
        rec = self.run_worker(plugin, monkeypatch, git=(0, "", ""))
        command, timeout, kwargs = self.fetches(rec)[0]
        assert command == ["git", "-C", REPO, "fetch", "--prune", "origin"]
        assert timeout == pr_mod.NETWORK_TIMEOUT_SECONDS
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -oBatchMode=yes"

    def test_no_gh_records_the_index_error_and_still_finishes(self, plugin, monkeypatch):
        ready(plugin)
        self.run_worker(plugin, monkeypatch, git=(0, "", ""), which=None)
        assert plugin._fetch_jobs.get() is None
        assert plugin._pr_index.meta()["error"] == pr_mod.GH_MISSING_ERROR
        assert plugin._harbor.get()[1] is None  # invalidated
        assert "fetched origin" in plugin._context.lines[-1]

    def test_an_unauthed_gh_records_the_index_error(self, plugin, monkeypatch):
        self.run_worker(plugin, monkeypatch, git=(0, "", ""), which="/opt/gh", authed=False)
        assert plugin._pr_index.meta()["error"] == pr_mod.GH_UNAUTHED_ERROR

    def test_an_authed_gh_replaces_the_index(self, plugin, monkeypatch):
        rec = self.run_worker(plugin, monkeypatch, git=(0, "", ""), which="/opt/gh", authed=True, gh=(0, PR_LIST_JSON, ""))
        meta = plugin._pr_index.meta()
        assert meta["count"] == 4 and meta["error"] is None and meta["fetched_at"]
        gh_call = next(c for c in rec.calls if c[0][:1] == ["gh"])
        assert gh_call[0][:3] == ["gh", "pr", "list"] and gh_call[2]["cwd"] == Path(REPO)
        assert "4 PRs indexed" in plugin._context.lines[-1]

    def test_a_gh_failure_keeps_the_old_index(self, plugin, monkeypatch):
        plugin._pr_index.replace(
            {"x": {"number": 1, "state": "open", "url": None, "draft": False, "merged_at": None, "updated_at": ""}}, 5.0
        )
        self.run_worker(plugin, monkeypatch, git=(0, "", ""), which="/opt/gh", authed=True, gh=(1, "", "HTTP 502"))
        meta = plugin._pr_index.meta()
        assert meta["count"] == 1 and meta["error"] == "HTTP 502"

    def test_garbage_from_gh_keeps_the_old_index(self, plugin, monkeypatch):
        self.run_worker(plugin, monkeypatch, git=(0, "", ""), which="/opt/gh", authed=True, gh=(0, "not json", ""))
        assert "no JSON" in plugin._pr_index.meta()["error"]

    def test_every_configured_remote_is_fetched_and_pruned(self, plugin, monkeypatch):
        # The bug this exists for: `sweep_remote_refs` walks every remote, so a
        # remote FETCH never pruned kept rendering branches deleted months ago.
        rec = self.run_worker(plugin, monkeypatch, git=(0, "", ""), remotes="origin\ngitlab\n")
        assert [c[0] for c in self.fetches(rec)] == [
            ["git", "-C", REPO, "fetch", "--prune", "origin"],
            ["git", "-C", REPO, "fetch", "--prune", "gitlab"],
        ]

    def test_a_secondary_remote_failing_is_named_not_fatal(self, plugin, monkeypatch):
        self.run_worker(
            plugin,
            monkeypatch,
            git=(0, "", ""),
            remotes="origin\ngitlab\n",
            per_remote={"gitlab": (128, "", "fatal: no such host")},
        )
        assert plugin._fetch_jobs.get() is None  # the pass still finished
        assert "gitlab: fatal: no such host" in plugin._remote_fetch_error
        assert "origin" in plugin._context.lines[-1]

    def test_the_primary_failing_is_still_fatal_with_others_configured(self, plugin, monkeypatch):
        self.run_worker(
            plugin,
            monkeypatch,
            git=(0, "", ""),
            remotes="origin\ngitlab\n",
            per_remote={"origin": (128, "", "fatal: could not read from remote")},
        )
        job = plugin._fetch_jobs.get()
        assert job["status"] == "error" and "could not read from remote" in job["error"]

    def test_a_secondary_failure_reaches_the_document_as_remote_error(self, plugin, monkeypatch):
        self.run_worker(
            plugin,
            monkeypatch,
            git=(0, "", ""),
            remotes="origin\ngitlab\n",
            per_remote={"gitlab": (128, "", "fatal: no such host")},
        )
        # A remote FETCH could not reach and one the sweep could not read are
        # the same fact to a reader: this tier is stale.
        plugin._remote_rows = lambda _names: ([], None)
        assert "gitlab" in plugin._harbor_runner()["remote_error"]

    def test_a_remote_list_that_omits_the_primary_still_tries_it(self, plugin, monkeypatch):
        rec = self.run_worker(plugin, monkeypatch, git=(0, "", ""), remotes="")
        assert [c[0][-1] for c in self.fetches(rec)] == ["origin"]

    def test_a_failed_fetch_fails_the_job_on_the_fetch_step(self, plugin, monkeypatch):
        self.run_worker(plugin, monkeypatch, git=(128, "", "fatal: could not read from remote repository"))
        job = plugin._fetch_jobs.get()
        assert job["status"] == "error" and job["step"] == "fetch"
        assert "could not read from remote" in job["error"]
        assert plugin._pr_index.meta()["error"] is None  # the gh half never ran

    def test_the_fetch_status_rides_on_branches(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        plugin.handle_post("/fetch", {}, {})
        _s, body = plugin.handle_get("/branches", {})
        assert body["fetch"]["status"] == "running" and body["fetch"]["step"] == "fetch"


class TestPruneWorktrees:
    def test_parses_the_verbose_output(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr(
            "worktree.create.run_bounded", Recorder({("git", "-C", REPO, "worktree", "prune", "-v"): (0, PRUNE_OUTPUT, "")})
        )
        ready(plugin)
        status, body = plugin.handle_post("/prune-worktrees", {}, {})
        assert status == 200 and body == {"pruned": ["stale-wt", "old"], "count": 2}
        assert plugin._harbor.get()[1] is None  # invalidated
        assert "pruned stale-wt, old" in plugin._context.lines[-1]

    def test_nothing_to_prune(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.run_bounded", Recorder({("git",): (0, "", "")}))
        status, body = plugin.handle_post("/prune-worktrees", {}, {})
        assert status == 200 and body == {"pruned": [], "count": 0}

    def test_a_git_failure_is_500(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.run_bounded", Recorder({("git",): (128, "", "fatal: not a git repository")}))
        status, body = plugin.handle_post("/prune-worktrees", {}, {})
        assert status == 500 and "not a git repository" in body["error"]

    def test_a_git_failure_body_is_home_scrubbed(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr(
            "worktree.create.run_bounded", Recorder({("git",): (128, "", f"fatal: cannot change to '{HOME}/gone'")})
        )
        status, body = plugin.handle_post("/prune-worktrees", {}, {})
        assert status == 500 and body["error"] == "fatal: cannot change to '~/gone'"


class TestCreateExisting:
    """`POST /create {existing: true}` checks out a branch that is already
    there; without the flag the route is byte-identical to before."""

    def test_the_argv_has_no_dash_b_and_no_base(self):
        from worktree.create import build_worktree_add_command

        argv = build_worktree_add_command(Path("/repo"), Path("/wt/x"), "feat", "main", existing=True)
        assert argv == ["git", "-C", "/repo", "worktree", "add", "--no-checkout", "/wt/x", "feat"]

    def test_the_default_argv_is_unchanged(self):
        from worktree.create import build_worktree_add_command

        argv = build_worktree_add_command(Path("/repo"), Path("/wt/x"), "feat", "main")
        assert argv == ["git", "-C", "/repo", "worktree", "add", "--no-checkout", "-b", "feat", "/wt/x", "main"]

    CLEAR = dict(git_ok=True, tmux_ok=True, claude_found=True, branch_exists=True, path_exists=False, existing=True)

    def test_decide_existing_and_missing_is_404(self):
        from worktree.create import decide_create

        status, body = decide_create(None, **{**self.CLEAR, "branch_exists": False})
        assert status == 404 and body["error"] == "no local branch by that name"

    def test_decide_existing_and_checked_out_is_409_with_the_collapsed_path(self):
        from worktree.create import decide_create

        status, body = decide_create(None, **self.CLEAR, checked_out_at=f"{INTENT}/x/demo-app")
        assert status == 409
        assert body["error"] == "already checked out at ~/other-tool/worktrees/x/demo-app — attach to that instead"

    def test_decide_existing_and_present_is_clear(self):
        from worktree.create import decide_create

        assert decide_create(None, **self.CLEAR) is None

    def test_decide_existing_still_refuses_a_path_on_disk_and_missing_tools(self):
        from worktree.create import decide_create

        assert decide_create(None, **{**self.CLEAR, "path_exists": True})[0] == 409
        assert decide_create(None, **{**self.CLEAR, "tmux_ok": False})[0] == 503

    def test_decide_without_the_flag_is_unchanged(self):
        from worktree.create import decide_create

        clear = {**self.CLEAR, "existing": False, "branch_exists": False}
        assert decide_create(None, **clear) is None
        assert decide_create(None, **{**clear, "branch_exists": True})[0] == 409

    def test_the_route_refuses_an_unknown_branch_with_404(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: False)
        status, body = plugin.handle_post("/create", {}, {"branch": "nope", "existing": True})
        assert status == 404 and body["error"] == "no local branch by that name"

    def test_the_route_refuses_a_checked_out_branch_with_409(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: True)
        status, body = plugin.handle_post("/create", {}, {"branch": "add-stopwatch-feed", "existing": True})
        assert status == 409
        assert body["error"] == "already checked out at ~/other-tool/worktrees/spike-one/demo-app — attach to that instead"

    def test_the_route_builds_an_existing_branch_and_says_so(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: True)
        monkeypatch.setattr("worktree.create.session_exists", lambda _n: False)
        monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: "/bin/claude")
        status, body = plugin.handle_post("/create", {}, {"branch": "test123", "existing": True})
        assert status == 202 and body["existing"] is True and body["created"] is True
        assert body["path"] == str(worktree_path_for("test123", plugin._root))
        worker = FakeThread.started[-1]
        assert worker.args[-1] is True  # `existing` reaches the worker

    def test_a_plain_create_of_an_existing_branch_is_still_409(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: True)
        status, body = plugin.handle_post("/create", {}, {"branch": "test123"})
        assert status == 409 and "already exists" in body["error"]
        assert "existing" not in body

    # ── respawn: a managed tree whose session died ───────────────────────────
    HELD = "feat/cache-all-pages"  # checked out at {MANAGED}/cache-all-pages in RECORDS

    @staticmethod
    def respawnable(monkeypatch, live=False, on_disk=True) -> list[str]:
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: True)
        monkeypatch.setattr("worktree.create.tree_present", lambda _p: on_disk)
        asked: list[str] = []
        monkeypatch.setattr("worktree.create.session_exists", lambda name: asked.append(name) or live)
        monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: "/bin/claude")
        return asked

    def test_decide_respawn_lifts_only_the_held_and_on_disk_refusals(self):
        from worktree.create import decide_create

        held = {**self.CLEAR, "checked_out_at": f"{MANAGED}/x"}
        assert decide_create(None, **held, respawn=True) is None
        assert decide_create(None, **{**held, "path_exists": True}, respawn=True) is None
        assert decide_create(None, **{**held, "tmux_ok": False}, respawn=True)[0] == 503
        assert decide_create(None, **{**held, "branch_exists": False}, respawn=True)[0] == 404
        fresh = {**self.CLEAR, "existing": False, "branch_exists": False, "path_exists": True}
        assert decide_create(None, **fresh, respawn=True)[0] == 409  # never without `existing`

    def test_a_managed_tree_with_no_live_session_is_respawned_in_place(self, plugin, monkeypatch):
        from worktree.names import session_name_for

        asked = self.respawnable(monkeypatch)
        status, body = plugin.handle_post("/create", {}, {"branch": self.HELD, "existing": True, "prompt": "carry on"})
        session = session_name_for(self.HELD)
        assert status == 202 and body["respawn"] is True and body["created"] is True
        assert body["path"] == f"{MANAGED}/cache-all-pages" and asked[0] == session
        worker = FakeThread.started[-1]
        assert worker.target.keywords["respawn"] is True and worker.args[-1] is True
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: pytest.fail("the tree is built"))
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, **_k: pytest.fail("bootstrap already ran"))
        spawned, steps = [], []
        monkeypatch.setattr(
            "worktree.create.spawn_session",
            lambda _cli, name, cwd, _env: spawned.append((name, str(cwd))) or (200, {"created": True}),
        )
        monkeypatch.setattr("worktree.create.send_prompt", lambda name, prompt: steps.append(prompt) or True)
        advance = plugin._creates.advance
        monkeypatch.setattr(plugin._creates, "advance", lambda branch, step: steps.append(step) or advance(branch, step))
        worker.run_now()
        assert spawned == [(session, f"{MANAGED}/cache-all-pages")]
        assert steps == ["session", "prompt", "carry on"]
        assert plugin._creates.get(self.HELD) is None

    def test_a_managed_tree_with_a_live_session_still_says_attach(self, plugin, monkeypatch):
        self.respawnable(monkeypatch, live=True)
        status, body = plugin.handle_post("/create", {}, {"branch": self.HELD, "existing": True})
        assert status == 409 and body["error"].endswith("attach to that instead")
        assert not FakeThread.started

    def test_a_managed_tree_gone_from_disk_is_not_respawned(self, plugin, monkeypatch):
        asked = self.respawnable(monkeypatch, on_disk=False)
        status, _body = plugin.handle_post("/create", {}, {"branch": self.HELD, "existing": True})
        assert status == 409 and asked == []

    def test_a_tree_this_plugin_does_not_own_is_never_respawned(self, plugin, monkeypatch):
        asked = self.respawnable(monkeypatch)
        status, body = plugin.handle_post("/create", {}, {"branch": "add-stopwatch-feed", "existing": True})
        assert status == 409 and asked == []  # not even asked whether its session lives


# ── POST /brief ─────────────────────────────────────────────────────────────

BRIEF_ENVELOPE = json.dumps(
    {
        "is_error": False,
        "structured_output": {"what": "caches every entity locally", "next": ["rebase onto main", "push"]},
        "total_cost_usd": 0.004,
        "modelUsage": {"claude-haiku-4-5-20251001": {}},
    }
)


class TestBrief:
    @pytest.fixture(autouse=True)
    def headless_host(self, plugin):
        plugin._context.profiles = FakeProfiles()

    def start(self, plugin, monkeypatch, body: dict, *, claude=BRIEF_ENVELOPE, git=(0, "log text", "")):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: "/bin/claude")
        recorder = Recorder(
            {("git", "-C", REPO, "log"): git, ("git", "-C", REPO, "diff"): (0, " a | 1 +", ""), ("/bin/claude",): (0, claude, "")}
        )
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        status, answer = plugin.handle_post("/brief", {}, body)
        return status, answer, recorder

    def test_warming_harbor_is_409_and_a_bad_name_400(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: "/bin/claude")
        assert plugin.handle_post("/brief", {}, {"branch": "bad name"})[0] == 400
        assert plugin.handle_post("/brief", {}, {})[0] == 409
        monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: None)
        assert plugin.handle_post("/brief", {}, {})[0] == 503

    def test_all_targets_every_non_base_branch_and_the_meter_knows_the_total(self, plugin, monkeypatch):
        ready(plugin)
        status, answer, _rec = self.start(plugin, monkeypatch, {})
        assert status == 202 and answer["started"] is True
        assert answer["total"] == 9  # 10 fixture rows minus main
        doc = plugin.handle_get("/branches", {})[1]
        assert doc["brief"]["status"] == "running" and doc["brief"]["total"] == 9
        assert doc["brief"]["model"] == "haiku"

    def test_the_worker_lands_each_brief_as_it_is_made_and_the_rows_carry_it(self, plugin, monkeypatch):
        ready(plugin)
        _s, _a, rec = self.start(plugin, monkeypatch, {"branch": "feat/cache-all-pages"})
        FakeThread.started[-1].run_now()
        claude_calls = [c for c in rec.calls if c[0][0] == "/bin/claude"]
        assert len(claude_calls) == 1
        command, timeout, kwargs = claude_calls[0]
        assert "--system-prompt" in command and kwargs["input"].startswith(
            "REPOSITORY\ndemo-app\n\nFACTS\nbranch: feat/cache-all-pages"
        )
        assert "TMUX" not in kwargs["env"] and kwargs["env"]["MAX_THINKING_TOKENS"] == "0"
        job = plugin._brief_jobs.get()
        assert job["status"] == "done" and job["done"] == 1 and job["cost_usd"] == 0.004
        doc = plugin.handle_get("/branches", {})[1]
        row = next(r for r in doc["branches"] if r["branch"] == "feat/cache-all-pages")
        assert row["brief"] == {
            "what": "Caches every entity locally",
            "next": ["rebase onto main", "push"],
            "model": "claude-haiku-4-5-20101001".replace("20101001", "20251001"),
            "at": row["brief"]["at"],
            "stale": False,
        }
        assert doc["brief"]["count"] == 1 and doc["brief"]["at"] is not None
        # the rest are still missing…
        status, answer, _ = self.start(plugin, monkeypatch, {})
        assert status == 202 and answer["total"] == 8
        FakeThread.started[-1].run_now()
        assert plugin._briefs.meta()["count"] == 9
        assert plugin.handle_post("/brief", {}, {})[1] == {"status": "done", "started": True, "done": 0, "total": 0}
        # …and RE-BRIEF is a request, not a staleness check
        assert self.start(plugin, monkeypatch, {"branch": "feat/cache-all-pages"})[1]["total"] == 1

    def test_a_moved_tip_reads_stale_and_is_re_targeted(self, plugin, monkeypatch):
        ready(plugin)
        plugin._briefs.set(
            {
                "branch": "feat/cache-all-pages",
                "sha": "0000000",
                "base_sha": "a1b2c3d01",
                "what": "Old",
                "next": [],
                "model": "haiku",
                "cost_usd": 0.0,
                "at": 1.0,
            }
        )
        doc = plugin.handle_get("/branches", {})[1]
        row = next(r for r in doc["branches"] if r["branch"] == "feat/cache-all-pages")
        assert row["brief"]["stale"] is True and row["brief"]["what"] == "Old"
        status, answer, _ = self.start(plugin, monkeypatch, {})
        assert answer["total"] == 9  # the stale one is back in the set

    def test_one_failed_branch_does_not_fail_the_run_but_all_failed_does(self, plugin, monkeypatch):
        ready(plugin)
        bad = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})
        self.start(plugin, monkeypatch, {"branch": "pr-demo"}, claude=bad)
        FakeThread.started[-1].run_now()
        job = plugin._brief_jobs.get()
        assert job["status"] == "error" and "Not logged in" in job["error"]
        assert plugin._briefs.meta()["error"].startswith("Not logged in")
        assert plugin.handle_get("/branches", {})[1]["brief"]["error"].startswith("Not logged in")

    def test_the_run_stops_at_the_cost_cap_with_a_note(self, plugin, monkeypatch):
        ready(plugin)
        monkeypatch.setenv("WORKTREE_BRIEF_MAX_COST_USD", "0.01")  # the floor `resolve_max_cost_usd` allows
        _s, _a, rec = self.start(plugin, monkeypatch, {})
        FakeThread.started[-1].run_now()
        # $0.004 · $0.008 are under the cap; the third lands it at $0.012 → stop before the fourth
        assert len([c for c in rec.calls if c[0][0] == "/bin/claude"]) == 3
        job = plugin._brief_jobs.get()
        assert job["status"] == "done" and "stopped at $0.01" in job["note"]
        assert plugin._briefs.meta()["count"] == 3

    def test_a_failed_thread_start_undoes_the_claim(self, plugin, monkeypatch):
        ready(plugin)
        FakeThread.fail_start = True
        status, answer, _ = self.start(plugin, monkeypatch, {})
        assert status == 500
        assert plugin._brief_jobs.get()["status"] == "error"
        FakeThread.fail_start = False
        assert self.start(plugin, monkeypatch, {})[1]["started"] is True


class TestChanges:
    """`GET /changes` — the read that makes `git worktree remove`'s refusal
    legible. Parsers are `test_changes.py`'s; this is the ROUTE: which
    worktrees it will look at, and what it will let a caller ask for."""

    #: Two forks, answered by subcommand. `test_changes.STATUS` is the shape
    #: fixture; the route only has to pass it through.
    def runner(self, monkeypatch, *, status=None, numstat=None):
        from test_changes import NUMSTAT, STATUS

        calls: list[list[str]] = []

        def run(command, timeout, **_kwargs):  # noqa: ARG001
            calls.append(command)
            if "status" in command:
                return 0, status if status is not None else STATUS, ""
            if "--numstat" in command:
                return 0, numstat if numstat is not None else NUMSTAT, ""
            return 0, "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b", ""

        monkeypatch.setattr("worktree.create.run_bounded", run)
        return calls

    def test_a_managed_worktree_lists_its_uncommitted_work(self, plugin, monkeypatch):
        self.runner(monkeypatch)
        status, body = plugin.handle_get("/changes", {"branch": ["pr-demo"]})
        assert status == 200
        assert body["branch"] == "pr-demo"
        assert body["managed"] is True
        assert body["path_label"] == "~/worktrees/demo-app/pr-demo"
        assert body["summary"]["files"] == 9
        assert body["truncated"] is False
        assert body["patch"] is None

    def test_looking_is_not_gated_on_managed_the_way_removing_is(self, plugin, monkeypatch):
        """A checkout another tool made under `~/other-tool/` may be READ. `POST /remove`
        still refuses it — the write gate is `_managed()` and is untouched."""
        self.runner(monkeypatch)
        status, body = plugin.handle_get("/changes", {"branch": ["add-stopwatch-feed"]})
        assert status == 200
        assert body["managed"] is False
        assert body["path_label"].startswith("~/other-tool/")

    def test_a_branch_with_no_worktree_is_404_not_an_empty_list(self, plugin, monkeypatch):
        self.runner(monkeypatch)
        status, body = plugin.handle_get("/changes", {"branch": ["fix-pdf"]})
        assert status == 404
        assert "no worktree" in body["error"]

    def test_a_missing_branch_is_400(self, plugin, monkeypatch):
        self.runner(monkeypatch)
        assert plugin.handle_get("/changes", {})[0] == 400

    def test_a_git_failure_is_502_and_never_an_empty_worktree(self, plugin, monkeypatch):
        def run(command, timeout, **_kwargs):  # noqa: ARG001
            return 128, "", "fatal: not a git repository"

        monkeypatch.setattr("worktree.create.run_bounded", run)

        status, body = plugin.handle_get("/changes", {"branch": ["pr-demo"]})
        assert status == 502
        assert "not a git repository" in body["error"]

    def test_a_patch_is_asked_for_by_a_path_git_itself_printed(self, plugin, monkeypatch):
        calls = self.runner(monkeypatch)
        status, body = plugin.handle_get("/changes", {"branch": ["pr-demo"], "path": ["docs/notes/gamma-notes.md"]})
        assert status == 200
        assert body["patch"]["path"] == "docs/notes/gamma-notes.md"
        assert body["patch"]["text"].startswith("diff --git")
        assert body["patch"]["truncated"] is False
        # Untracked → the `--no-index` command, not a `HEAD` diff.
        assert "--no-index" in calls[-1]

    def test_an_invented_path_is_refused_rather_than_handed_to_git(self, plugin, monkeypatch):
        """The `path` parameter is a LOOKUP against the rows `git status` just
        printed. Nothing a caller writes reaches an argv."""
        calls = self.runner(monkeypatch)
        status, body = plugin.handle_get("/changes", {"branch": ["pr-demo"], "path": ["../../../../etc/passwd"]})
        assert status == 404
        assert "no such change" in body["error"]
        assert not any("diff" in command and "--numstat" not in command for command in calls)

    def test_the_file_list_is_capped_while_the_count_still_tells_the_truth(self, plugin, monkeypatch):
        many = "\0".join(f"?? file-{i}.txt" for i in range(20)) + "\0"
        self.runner(monkeypatch, status=many, numstat="")
        monkeypatch.setattr("worktree.changes.MAX_FILES", 5)

        status, body = plugin.handle_get("/changes", {"branch": ["pr-demo"]})
        assert status == 200
        assert len(body["files"]) == 5
        assert body["summary"]["files"] == 20
        assert body["truncated"] is True


# ── 2026-10-08: the PR index refreshes itself; content proofs; outside trees ──

STOPWATCH = "add-stopwatch-feed"  # held by a Claude-Code tree under INTENT
STOPWATCH_TIP = full("0a1b2c3d4")
STOPWATCH_PATH = f"{INTENT}/spike-one/demo-app"


def merged_index(
    branch: str, head_oid: str, number: int = 61, merged_at: str = "2036-01-01T00:00:00Z", base_ref: str = "main"
) -> dict:
    return branches_mod.parse_pr_list(
        json.dumps(
            [
                {
                    "headRefName": branch,
                    "number": number,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergedAt": merged_at,
                    "updatedAt": merged_at,
                    "url": "u",
                    "headRefOid": head_oid,
                    "baseRefName": base_ref,
                }
            ]
        )
    )


class TestPrIndexRefreshesItself:
    def refreshes(self):
        return [t for t in FakeThread.started if t.target.__name__ == "_pr_refresh_worker"]

    def test_a_never_fetched_index_schedules_one_refresh(self, plugin):
        ready(plugin)
        ready(plugin)
        assert len(self.refreshes()) == 1  # single flight across sweeps

    def test_a_fresh_index_schedules_nothing(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.fetch_head_mtime", lambda _repo: None)
        plugin._pr_index.replace(merged_index(SQUASHED, SQUASHED_TIP), __import__("time").time())
        ready(plugin)
        assert self.refreshes() == []

    def test_a_terminal_fetch_newer_than_the_index_schedules_one(self, plugin, monkeypatch):
        now = __import__("time").time()
        plugin._pr_index.replace(merged_index(SQUASHED, SQUASHED_TIP), now - 120)
        monkeypatch.setattr("worktree.branches.fetch_head_mtime", lambda _repo: now - 5)
        ready(plugin)
        assert len(self.refreshes()) == 1

    def test_but_not_more_often_than_the_minimum_interval(self, plugin, monkeypatch):
        now = __import__("time").time()
        plugin._pr_index.replace(merged_index(SQUASHED, SQUASHED_TIP), now - 10)
        monkeypatch.setattr("worktree.branches.fetch_head_mtime", lambda _repo: now - 5)
        ready(plugin)
        assert self.refreshes() == []

    def test_an_index_written_before_base_ref_existed_is_due(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.branches.fetch_head_mtime", lambda _repo: None)
        old = merged_index(SQUASHED, SQUASHED_TIP)
        old[SQUASHED]["base_ref"] = None
        plugin._pr_index.replace(old, __import__("time").time())
        ready(plugin)
        assert len(self.refreshes()) == 1

    def test_never_while_fetch_runs(self, plugin):
        assert plugin._fetch_jobs.claim()
        ready(plugin)
        assert self.refreshes() == []

    def test_a_failed_refresh_backs_off_one_ttl(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.plugin.shutil.which", lambda _name: None)
        ready(plugin)
        self.refreshes()[0].run_now()
        assert plugin._pr_index.meta()["error"] == pr_mod.GH_MISSING_ERROR
        FakeThread.started.clear()
        ready(plugin)
        assert self.refreshes() == []

    def test_the_block_says_stale_and_refreshing(self, plugin):
        body = ready(plugin)
        assert body["pr_index"]["stale"] is True and body["pr_index"]["refreshing"] is True
        self.refreshes()[0].run_now()  # gh absent in the fence → recorded, not raised
        assert plugin.handle_get("/branches", {})[1]["pr_index"]["refreshing"] is False

    def test_a_successful_refresh_fetches_missing_heads_by_id(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.plugin.shutil.which", lambda _name: "/bin/gh")
        monkeypatch.setattr("worktree.pr.gh_authed", lambda: True)
        monkeypatch.setattr("worktree.landing.local_branch_names", lambda _root, **_kw: {SQUASHED})
        monkeypatch.setattr("worktree.landing.missing_objects", lambda _root, oids, **_kw: set(oids))
        pr_json = json.dumps(
            [
                {
                    "headRefName": SQUASHED,
                    "number": 57,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergedAt": "2036-01-01T00:00:00Z",
                    "updatedAt": "2036-01-01T00:00:00Z",
                    "url": "u",
                    "headRefOid": SQUASHED_TIP,
                    "baseRefName": "main",
                }
            ]
        )
        rec = Recorder({("gh", "pr", "list"): (0, pr_json, ""), ("git", "-C", REPO, "fetch"): (0, "", "")})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        ready(plugin)
        self.refreshes()[0].run_now()
        fetches = [c[0] for c in rec.calls if c[0][3] == "fetch"]
        assert fetches == [["git", "-C", REPO, "fetch", "--no-write-fetch-head", "--no-tags", "origin", SQUASHED_TIP]]
        assert plugin._pr_index.meta()["count"] == 1


class TestContentFieldsOnTheDocument:
    def test_every_row_carries_the_new_fields(self, plugin):
        rows = by_branch(ready(plugin))
        for name, entry in rows.items():
            assert entry["content"] in ("measured", "pending", "skipped"), name
            assert "landing" in entry and "contained_in" in entry and "ledger" in entry
        assert rows["test123"]["landing"] == {"how": "ancestor", "pr": None}
        assert rows["feat/cache-all-pages"]["content"] == "pending"

    def test_a_contained_row_is_ok_to_delete(self, plugin, monkeypatch):
        # fill-docstring-gaps-now is unheld and ahead 2; say feat/cache-all-pages holds it.
        monkeypatch.setattr(
            "worktree.landing.contains_sets",
            lambda _root, rows, **_kw: {
                row["branch"]: ({row["branch"], "feat/cache-all-pages"} if row["branch"] == SQUASHED else {row["branch"]})
                for row in rows
            },
        )
        row = by_branch(ready(plugin))[SQUASHED]
        assert row["landed"] is False and row["contained_in"] == "feat/cache-all-pages"
        assert row["delete_verdict"] == "ok" and row["ledger"] is None

    def test_the_delete_route_runs_dash_D_for_a_contained_row(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.landing.contains_sets",
            lambda _root, rows, **_kw: {
                row["branch"]: ({row["branch"], "feat/cache-all-pages"} if row["branch"] == SQUASHED else {row["branch"]})
                for row in rows
            },
        )
        rec = Recorder({("git", "-C", REPO, "branch", "-D", SQUASHED): (0, "", "")})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        status, body = plugin.handle_post("/delete-branch", {}, {"branch": SQUASHED})
        assert status == 200 and body["forced"] is False
        assert "every commit is on 'feat/cache-all-pages'" in plugin._context.lines[-1]

    def test_a_containment_git_could_not_answer_contains_nothing(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.landing.contains_sets", lambda *_a, **_kw: None)
        row = by_branch(ready(plugin))[SQUASHED]
        assert row["contained_in"] is None and row["delete_verdict"] == "needs_force"

    def test_a_pr_merged_into_another_branch_is_not_landed(self, plugin):
        plugin._pr_index.replace(squash_pr(SQUASHED_TIP, base_ref="feat/parent"), 1.0)
        row = by_branch(ready(plugin))[SQUASHED]
        assert row["landed"] is False and row["delete_verdict"] == "needs_force"

    def test_occupants_ride_on_the_worktree(self, plugin, monkeypatch):
        monkeypatch.setattr(
            "worktree.landing.sweep_occupants", lambda paths, **_kw: {p: (["zsh"] if p == STOPWATCH_PATH else []) for p in paths}
        )
        rows = by_branch(ready(plugin))
        assert rows[STOPWATCH]["worktree"]["occupied_by"] == ["zsh"]
        assert rows["feat/cache-all-pages"]["worktree"]["occupied_by"] == []


class TestRemoveLandedWorktree:
    def landed_stopwatch(self, plugin):
        plugin._pr_index.replace(merged_index(STOPWATCH, STOPWATCH_TIP), 1.0)

    def post(self, plugin, monkeypatch, branch=STOPWATCH, *, dirty=False, occupants=(), git=None):
        rec = Recorder(git or {("git", "-C", REPO, "worktree", "remove"): (0, "", "")})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _budget, **_kw: dict.fromkeys(paths, dirty))
        monkeypatch.setattr(
            "worktree.landing.sweep_occupants",
            lambda paths, **_kw: {p: (list(occupants) if occupants is not None else None) for p in paths},
        )
        return plugin.handle_post("/remove-landed-worktree", {}, {"branch": branch}), rec

    def removes(self, rec):
        return [c[0] for c in rec.calls if c[0][3:5] == ["worktree", "remove"]]

    def test_the_route_is_advertised(self, plugin):
        assert "/plugin/worktree/remove-landed-worktree" in plugin.routes()

    def test_the_row_offers_it_only_when_the_table_would_clear(self, plugin):
        assert "worktree_removable_outside" not in by_branch(ready(plugin))[STOPWATCH]
        self.landed_stopwatch(plugin)
        plugin._harbor.invalidate()
        row = by_branch(ready(plugin))[STOPWATCH]
        assert row["worktree_removable_outside"] is True
        # managed trees keep their own door
        assert "worktree_removable_outside" not in by_branch(ready(plugin))["pr-demo"]

    def test_a_landed_clean_unoccupied_tree_is_removed_without_force(self, plugin, monkeypatch):
        self.landed_stopwatch(plugin)
        (status, body), rec = self.post(plugin, monkeypatch)
        assert status == 200 and body == {"branch": STOPWATCH, "removed": True}
        assert self.removes(rec) == [["git", "-C", REPO, "worktree", "remove", STOPWATCH_PATH]]

    @pytest.mark.parametrize(
        "kwargs, words",
        [
            ({"dirty": True}, "uncommitted"),
            ({"dirty": None}, "could not read"),
            ({"occupants": ("zsh",)}, "zsh is running"),
            ({"occupants": None}, "could not tell"),
        ],
    )
    def test_fresh_probes_refuse_and_remove_nothing(self, plugin, monkeypatch, kwargs, words):
        self.landed_stopwatch(plugin)
        (status, body), rec = self.post(plugin, monkeypatch, **kwargs)
        assert status == 409 and words in body["error"] and self.removes(rec) == []

    def test_an_unlanded_tree_is_refused_before_any_probe(self, plugin, monkeypatch):
        def no_probe(*_a, **_k):
            raise AssertionError("probed a tree the cheap rows already refuse")

        rec = Recorder({})
        monkeypatch.setattr("worktree.create.run_bounded", rec)
        monkeypatch.setattr("worktree.branches.dirty_sweep", no_probe)
        monkeypatch.setattr("worktree.landing.sweep_occupants", no_probe)
        status, body = plugin.handle_post("/remove-landed-worktree", {}, {"branch": STOPWATCH})
        assert status == 409 and "not in main" in body["error"] and rec.calls == []

    def test_a_managed_tree_is_sent_to_its_own_route(self, plugin, monkeypatch):
        (status, body), rec = self.post(plugin, monkeypatch, branch="pr-demo")
        assert status == 409 and "REMOVE WORKTREE" in body["error"] and self.removes(rec) == []

    def test_the_main_checkout_is_never_removed(self, plugin, monkeypatch):
        (status, body), rec = self.post(plugin, monkeypatch, branch="main")
        assert status == 409 and "main checkout" in body["error"] and self.removes(rec) == []

    def test_a_branch_with_no_tree_is_404(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, branch=SQUASHED)
        assert status == 404 and self.removes(rec) == []

    def test_a_bad_name_is_400_and_never_swept(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, branch="-D")
        assert status == 400 and rec.calls == []

    def test_gits_refusal_is_a_409_with_home_scrubbed(self, plugin, monkeypatch):
        self.landed_stopwatch(plugin)
        (status, body), _rec = self.post(
            plugin, monkeypatch, git={("git", "-C", REPO, "worktree", "remove"): (128, "", f"fatal: {HOME}/x is dirty\n")}
        )
        assert status == 409 and HOME not in body["error"] and "~/x" in body["error"]


class TestArchive:
    """`POST /restore-branch` and `POST /forget-archived` (archive.py)."""

    def post(self, plugin, monkeypatch, route: str, body: dict, *, git=None, pinned=None):
        recorder = Recorder(git or {})
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        monkeypatch.setattr("worktree.create.tool_found", lambda _name: True)
        pins = pinned if isinstance(pinned, list) else [pin("feat/x", pinned)] if pinned else []
        monkeypatch.setattr("worktree.archive.pins_for", lambda _root, _b: pins)
        return plugin.handle_post(route, {}, body), recorder

    def test_the_routes_are_advertised(self, plugin):
        routes = plugin.routes()
        assert "/plugin/worktree/restore-branch" in routes
        assert "/plugin/worktree/forget-archived" in routes

    def test_restore_recreates_the_branch_only_if_absent_then_unpins(self, plugin, monkeypatch):
        tip = full("7a7")
        (status, body), rec = self.post(
            plugin,
            monkeypatch,
            "/restore-branch",
            {"branch": "feat/x"},
            pinned=tip,
            git={("git", "-C", REPO, "show-ref"): (1, "", ""), ("git", "-C", REPO, "update-ref"): (0, "", "")},
        )
        assert status == 200 and body == {"branch": "feat/x", "restored": True, "sha": tip[:9]}
        create, unpin = rec.calls[1][0], rec.calls[2][0]
        assert create[-3:] == ["refs/heads/feat/x", tip, "0" * 40]
        assert unpin[-3:] == ["-d", "refs/tldrack/archived/feat/x", tip]

    def test_restore_refuses_a_taken_name_without_touching_refs(self, plugin, monkeypatch):
        (status, _body), rec = self.post(
            plugin,
            monkeypatch,
            "/restore-branch",
            {"branch": "feat/x"},
            pinned=full("7a7"),
            git={("git", "-C", REPO, "show-ref"): (0, "", "")},
        )
        assert status == 409 and [c[0][3] for c in rec.calls] == ["show-ref"]

    def test_restore_without_a_pin_is_404_and_a_bad_name_never_reaches_git(self, plugin, monkeypatch):
        (status, _b), _rec = self.post(
            plugin, monkeypatch, "/restore-branch", {"branch": "feat/x"}, git={("git", "-C", REPO, "show-ref"): (1, "", "")}
        )
        assert status == 404
        (status, _b), rec = self.post(plugin, monkeypatch, "/restore-branch", {"branch": "-x"})
        assert status == 400 and rec.calls == []

    def test_forget_drops_exactly_the_tip_it_was_shown(self, plugin, monkeypatch):
        tip = full("7a7")
        (status, body), rec = self.post(
            plugin,
            monkeypatch,
            "/forget-archived",
            {"branch": "feat/x", "sha": tip},
            pinned=tip,
            git={("git", "-C", REPO, "update-ref", "-d"): (0, "", "")},
        )
        assert status == 200 and body == {"branch": "feat/x", "forgotten": True}
        assert rec.calls[0][0][-2:] == ["refs/tldrack/archived/feat/x", tip]

    def test_a_legacy_pin_is_restorable_and_dropped_where_it_lives(self, plugin, monkeypatch):
        tip = full("7a7")
        (status, body), rec = self.post(
            plugin,
            monkeypatch,
            "/restore-branch",
            {"branch": "feat/x"},
            pinned=[pin("feat/x", tip, legacy=True)],
            git={("git", "-C", REPO, "show-ref"): (1, "", ""), ("git", "-C", REPO, "update-ref"): (0, "", "")},
        )
        assert status == 200 and body["restored"] is True
        assert rec.calls[2][0][-3:] == ["-d", "refs/voidcraft/archived/feat/x", tip]

    def test_a_tip_pinned_under_both_names_is_forgotten_under_both(self, plugin, monkeypatch):
        tip = full("7a7")
        (status, _body), rec = self.post(
            plugin,
            monkeypatch,
            "/forget-archived",
            {"branch": "feat/x", "sha": tip},
            pinned=[pin("feat/x", tip), pin("feat/x", tip, legacy=True)],
            git={("git", "-C", REPO, "update-ref", "-d"): (0, "", "")},
        )
        assert status == 200
        assert [c[0][-2] for c in rec.calls] == ["refs/tldrack/archived/feat/x", "refs/voidcraft/archived/feat/x"]

    def test_forgetting_the_newer_pin_leaves_a_divergent_older_one(self, plugin, monkeypatch):
        new, old = full("7a7"), full("0dd")
        (status, _body), rec = self.post(
            plugin,
            monkeypatch,
            "/forget-archived",
            {"branch": "feat/x", "sha": new},
            pinned=[pin("feat/x", new), pin("feat/x", old, legacy=True)],
            git={("git", "-C", REPO, "update-ref", "-d"): (0, "", "")},
        )
        assert status == 200 and [c[0][-1] for c in rec.calls] == [new], "the older tip is never dropped unseen"

    def test_forget_refuses_a_tip_that_moved(self, plugin, monkeypatch):
        (status, _b), rec = self.post(
            plugin, monkeypatch, "/forget-archived", {"branch": "feat/x", "sha": full("0dd")}, pinned=full("7a7")
        )
        assert status == 409 and rec.calls == []

    def test_the_harbor_document_lists_the_archive(self, plugin, monkeypatch):
        listed = [{"branch": "feat/x", "oid": full("7a7"), "sha": "7a7000000", "committed_at": 1, "subject": "s"}]
        monkeypatch.setattr("worktree.archive.list_archived", lambda _root: listed)
        assert ready(plugin)["archived"] == listed
