"""SHIP / SYNC / CANCEL for the main checkout (`worktree/ship.py`), against REAL
git: a temp `work` clone of a bare `origin`, with `gh` answered by `FakeGh`, which
performs merges on the bare remote the way GitHub would (squash = one commit on
main, merge = a two-parent commit) through `git merge-tree --write-tree`.

Every job runs inline (`start_thread` calls the target), so a POST returns after
its worker has finished and the record is what the next poll would read.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from worktree import pr as pr_mod
from worktree import ship as ship_mod
from worktree.ship import ShipController

IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}
REPO_SLUG = "owner/demo-app"
NOW = datetime(2026, 10, 16, 14, 3, 7, tzinfo=timezone.utc)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **IDENTITY}, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


class FakeGh:
    """`gh` against the bare remote. PRs live in `self.prs`; `checks` and
    `mergeable` are what `gh pr view` reports for every PR."""

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        self.prs: dict[int, dict] = {}
        self.next_number = 137
        self.allowed = {"squashMergeAllowed": True, "mergeCommitAllowed": True, "rebaseMergeAllowed": True}
        self.rollup: list = []
        self.mergeable = "MERGEABLE"
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        verb = argv[1:3]
        if verb == ["repo", "view"]:
            return 0, json.dumps({"nameWithOwner": REPO_SLUG, **self.allowed}), ""
        assert "--repo" in argv and argv[argv.index("--repo") + 1] == REPO_SLUG, argv
        if verb == ["pr", "create"]:
            return self._create(argv)
        pr = self._find(argv[3])
        if pr is None:
            return 1, "", f"no pull requests found for {argv[3]}"
        if verb == ["pr", "view"]:
            return 0, json.dumps(self._view(pr)), ""
        if verb == ["pr", "merge"]:
            return self._merge(pr, argv)
        if verb == ["pr", "close"]:
            pr["state"] = "CLOSED"
            if "--delete-branch" in argv:
                git(self.origin, "update-ref", "-d", f"refs/heads/{pr['head']}")
            return 0, "", ""
        raise AssertionError(f"unexpected gh call {argv}")

    def _arg(self, argv: list[str], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def _create(self, argv: list[str]) -> tuple[int, str, str]:
        head = self._arg(argv, "--head")
        git(self.origin, "rev-parse", "--verify", f"refs/heads/{head}")  # pushed first
        number = self.next_number
        self.next_number += 1
        self.prs[number] = {
            "number": number,
            "head": head,
            "base": self._arg(argv, "--base"),
            "title": self._arg(argv, "--title"),
            "body": self._arg(argv, "--body"),
            "state": "OPEN",
            "merge_commit": None,
        }
        return 0, f"https://github.com/{REPO_SLUG}/pull/{number}\n", ""

    def _find(self, selector: str) -> dict | None:
        number = selector.rsplit("/", 1)[-1]
        if number.isdigit():
            return self.prs.get(int(number))
        return next((pr for pr in self.prs.values() if pr["head"] == selector), None)

    def _view(self, pr: dict) -> dict:
        return {
            "number": pr["number"],
            "url": f"https://github.com/{REPO_SLUG}/pull/{pr['number']}",
            "state": pr["state"],
            "mergeable": self.mergeable,
            "statusCheckRollup": self.rollup,
            "mergeCommit": {"oid": pr["merge_commit"]} if pr["merge_commit"] else None,
        }

    def _merge(self, pr: dict, argv: list[str]) -> tuple[int, str, str]:
        main = git(self.origin, "rev-parse", "refs/heads/main")
        head = git(self.origin, "rev-parse", f"refs/heads/{pr['head']}")
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", main, head], cwd=self.origin, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            return 1, "", "Pull request is not mergeable: merge conflict"
        tree = result.stdout.splitlines()[0]
        if "--squash" in argv:
            commit = git(self.origin, "commit-tree", tree, "-p", main, "-m", f"{pr['title']} (#{pr['number']})")
        elif "--merge" in argv:
            commit = git(self.origin, "commit-tree", tree, "-p", main, "-p", head, "-m", f"Merge pull request #{pr['number']}")
        else:
            raise AssertionError(f"unexpected merge method {argv}")
        git(self.origin, "update-ref", "refs/heads/main", commit, main)
        if "--delete-branch" in argv:
            git(self.origin, "update-ref", "-d", f"refs/heads/{pr['head']}")
        pr.update(state="MERGED", merge_commit=commit)
        return 0, "", ""


class Repo:
    """A bare `origin`, the main checkout `work`, and `other` — a second clone
    that lands somebody else's PR straight onto origin/main."""

    def __init__(self, root: Path) -> None:
        self.origin, self.work, self.other = root / "origin.git", root / "work", root / "other"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.work))
        for rel, text in {
            "a.txt": "a1\n",
            "b/c.txt": "c1\n",
            "doomed.txt": "bye\n",
            "other.txt": "o1\n",
            ".gitignore": "*.log\n",
        }.items():
            write(self.work, rel, text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "init")
        git(self.work, "push", "-q", "origin", "main")
        git(root, "clone", "-q", str(self.origin), str(self.other))

    def land_other_pr(self, rel: str, text: str) -> str:
        git(self.other, "pull", "-q", "--ff-only")
        write(self.other, rel, text)
        git(self.other, "commit", "-q", "-am", f"other PR touches {rel}")
        git(self.other, "push", "-q", "origin", "main")
        return git(self.other, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.work, "rev-parse", "HEAD")

    def status(self) -> list[str]:
        # `--no-optional-locks`, or this very probe would rewrite the index the
        # tests compare byte for byte.
        result = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain"], cwd=self.work, capture_output=True, text=True, check=True
        )
        return result.stdout.splitlines()  # unstripped: " M" keeps its column

    def index_digest(self) -> str:
        return hashlib.sha256((self.work / ".git" / "index").read_bytes()).hexdigest()

    def read(self, rel: str) -> str:
        return (self.work / rel).read_text()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.fixture
def gh(repo):
    return FakeGh(repo.origin)


class Rig:
    """Builds controllers over one store file, so a 'restart' is a second one."""

    def __init__(self, repo: Repo, gh: FakeGh, store: Path) -> None:
        self.repo, self.gh, self.store = repo, gh, store
        self.deferred: list = []
        self.defer = False
        self.gh_problem: str | None = None

    def run(self, argv, cwd, timeout, env=None):
        if argv[0] == "gh":
            return self.gh(argv)
        return pr_mod._run(argv, cwd, timeout, env={**IDENTITY, **(env or {})})

    def start(self, target, _name):
        if self.defer:
            self.deferred.append(target)
        else:
            target()

    def controller(self) -> ShipController:
        return ShipController(
            main_path=lambda: self.repo.work,
            store_path=self.store,
            run=self.run,
            start_thread=self.start,
            gh_problem=lambda: self.gh_problem,
            now=lambda: NOW,
        )


@pytest.fixture
def rig(repo, gh, tmp_path):
    return Rig(repo, gh, tmp_path / "state" / "worktree-ship.json")


def dirty_the_tree(repo: Repo) -> None:
    """A tracked edit, a new untracked file in a new dir, a deletion, an ignored file."""
    write(repo.work, "a.txt", "a2\n")
    write(repo.work, "front-end/new.ts", "export const x = 1\n")
    (repo.work / "doomed.txt").unlink()
    write(repo.work, "debug.log", "ignored\n")


def ship(rig: Rig, ctl: ShipController, title=None) -> dict:
    status, body = ctl.handle_ship({"title": title} if title else {})
    assert status == 202, body
    assert body["ok"] is True and body["ship"]["phase"] == "shipping"
    return ctl.state()[1]["ship"]


def sync(ctl: ShipController) -> dict:
    status, body = ctl.handle_sync()
    assert status == 202, body
    return ctl.state()[1]["ship"]


# ── SHIP ─────────────────────────────────────────────────────────────────────


class TestShip:
    def test_ship_leaves_tree_index_and_head_untouched(self, repo, rig):
        dirty_the_tree(repo)
        before = (repo.head(), repo.index_digest(), repo.status(), repo.read("a.txt"))
        ctl = rig.controller()

        state = ship(rig, ctl)

        assert (repo.head(), repo.index_digest(), repo.status(), repo.read("a.txt")) == before
        assert git(repo.work, "branch", "--list", "ship/*") == ""  # no local branch made
        assert state["phase"] == "open" and state["error"] is None
        assert state["base"] == before[0]
        assert state["branch"] == "ship/" + NOW.astimezone().strftime("%Y%m%d-%H%M")
        assert state["pr"] == {
            "number": 137,
            "url": f"https://github.com/{REPO_SLUG}/pull/137",
            "state": "OPEN",
            "checks": "none",
            "mergeable": "MERGEABLE",
            "merge_commit": None,
        }
        # The snapshot sits on H and is what the remote branch holds.
        remote_tip = git(repo.origin, "rev-parse", f"refs/heads/{state['branch']}")
        assert remote_tip == state["snapshot"]
        assert git(repo.origin, "rev-parse", f"{remote_tip}^") == before[0]

    def test_untracked_and_deleted_are_captured_and_ignored_files_are_not(self, repo, rig):
        dirty_the_tree(repo)
        state = ship(rig, rig.controller())
        files = git(repo.origin, "ls-tree", "-r", "--name-only", state["snapshot"]).splitlines()
        assert "front-end/new.ts" in files
        assert "doomed.txt" not in files
        assert "debug.log" not in files
        assert git(repo.origin, "show", f"{state['snapshot']}:a.txt") == "a2"

    def test_default_title_and_body_name_the_changed_dirs(self, repo, rig, gh):
        dirty_the_tree(repo)
        state = ship(rig, rig.controller())
        assert state["title"] == "iteration: a.txt, doomed.txt, front-end (+3 files)"
        assert state["files"] == 3
        body = gh.prs[137]["body"]
        assert ship_mod.SHIP_FOOTER in body and "**3 files**" in body and "`front-end` — 1" in body

    def test_an_explicit_title_wins(self, repo, rig, gh):
        dirty_the_tree(repo)
        state = ship(rig, rig.controller(), title="  Retry:   tidy  ")
        assert state["title"] == "Retry: tidy" and gh.prs[137]["title"] == "Retry: tidy"

    def test_local_commits_ahead_ship_without_an_empty_commit(self, repo, rig, gh):
        write(repo.work, "a.txt", "local commit\n")
        git(repo.work, "commit", "-q", "-am", "local only")
        state = ship(rig, rig.controller())
        assert state["snapshot"] == repo.head()  # S is H: the tree had nothing new
        assert "Includes 1 local commit" in gh.prs[137]["body"]

    def test_nothing_to_ship_is_409(self, rig):
        status, body = rig.controller().handle_ship({})
        assert status == 409 and body["ok"] is False and "nothing to ship" in body["error"]

    def test_not_on_main_is_409(self, repo, rig):
        git(repo.work, "checkout", "-q", "-b", "elsewhere")
        write(repo.work, "a.txt", "a2\n")
        status, body = rig.controller().handle_ship({})
        assert status == 409 and "on elsewhere" in body["error"]

    def test_an_open_ship_refuses_a_second(self, repo, rig):
        dirty_the_tree(repo)
        ctl = rig.controller()
        ship(rig, ctl)
        status, body = ctl.handle_ship({})
        assert status == 409 and "PR #137 is already open" in body["error"]

    def test_gh_missing_is_503(self, repo, rig):
        dirty_the_tree(repo)
        rig.gh_problem = pr_mod.GH_MISSING_ERROR
        status, body = rig.controller().handle_ship({})
        assert status == 503 and "brew install gh" in body["error"]

    def test_a_push_failure_is_failed_and_ship_can_run_again(self, repo, rig):
        dirty_the_tree(repo)
        git(repo.work, "remote", "set-url", "origin", str(repo.work.parent / "nowhere.git"))
        ctl = rig.controller()
        state = ship(rig, ctl)
        assert state["phase"] == "failed" and state["pr"] is None and state["error"]
        git(repo.work, "remote", "set-url", "origin", str(repo.origin))
        assert ship(rig, ctl)["phase"] == "open"

    def test_a_taken_branch_name_gets_seconds(self, repo, rig):
        dirty_the_tree(repo)
        taken = "ship/" + NOW.astimezone().strftime("%Y%m%d-%H%M")
        write(repo.other, "x.txt", "x\n")
        git(repo.other, "add", "-A")
        git(repo.other, "commit", "-q", "-m", "squatter")
        git(repo.other, "push", "-q", "origin", f"HEAD:refs/heads/{taken}")
        state = ship(rig, rig.controller())
        assert state["phase"] == "open"
        assert state["branch"] == "ship/" + NOW.astimezone().strftime("%Y%m%d-%H%M%S")


# ── SYNC ─────────────────────────────────────────────────────────────────────


class TestSync:
    def _shipped(self, repo, rig):
        dirty_the_tree(repo)
        ctl = rig.controller()
        return ctl, ship(rig, ctl)

    def test_squash_sync_keeps_edits_made_after_the_snapshot(self, repo, rig, gh):
        ctl, shipped = self._shipped(repo, rig)
        write(repo.work, "a.txt", "a3 after the snapshot\n")
        write(repo.work, "later.txt", "new after the snapshot\n")

        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        landed = gh.prs[137]["merge_commit"]
        assert repo.head() == landed == git(repo.origin, "rev-parse", "main")
        assert len(git(repo.work, "rev-list", "--parents", "-n1", "HEAD").split()) == 2  # squash
        assert repo.read("a.txt") == "a3 after the snapshot\n"
        assert sorted(repo.status()) == [" M a.txt", "?? later.txt"]
        assert state["last"]["pr"] == 137 and state["last"]["landed"] == landed
        assert state["last"]["still_dirty"] == len(repo.status())
        assert state["pr"] is None and state["branch"] is None and state["snapshot"] is None
        assert "--squash" in next(c for c in gh.calls if c[1:3] == ["pr", "merge"])
        assert git(repo.origin, "branch", "--list", shipped["branch"]) == ""

    def test_merge_commit_path_when_squash_is_not_allowed(self, repo, rig, gh):
        gh.allowed["squashMergeAllowed"] = False
        ctl, _shipped = self._shipped(repo, rig)
        write(repo.work, "a.txt", "a3\n")

        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        assert len(git(repo.work, "rev-list", "--parents", "-n1", "HEAD").split()) == 3  # merge
        assert repo.read("a.txt") == "a3\n" and repo.status() == [" M a.txt"]

    def test_another_merged_pr_is_pulled_in(self, repo, rig):
        ctl, _shipped = self._shipped(repo, rig)
        repo.land_other_pr("other.txt", "o2 from someone else\n")
        write(repo.work, "a.txt", "a3\n")

        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        assert repo.read("other.txt") == "o2 from someone else\n"
        assert repo.read("a.txt") == "a3\n" and repo.status() == [" M a.txt"]
        assert (repo.work / "front-end/new.ts").exists() and not (repo.work / "doomed.txt").exists()

    def test_a_clean_sync_leaves_a_clean_tree(self, repo, rig):
        ctl, _shipped = self._shipped(repo, rig)
        state = sync(ctl)
        assert state["phase"] == "idle" and repo.status() == [] and state["last"]["still_dirty"] == 0

    def test_refused_when_head_moved_and_nothing_is_merged(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        write(repo.work, "b/c.txt", "c-local\n")
        git(repo.work, "commit", "-q", "-m", "a local commit after SHIP", "b/c.txt")

        status, body = ctl.handle_sync()

        assert status == 409 and "moved since SHIP" in body["error"]
        assert gh.prs[137]["state"] == "OPEN"
        assert ctl.state()[1]["ship"]["phase"] == "open"

    def test_dirty_file_collision_refuses_safely_and_a_retry_lands(self, repo, rig, gh):
        ctl, shipped = self._shipped(repo, rig)
        repo.land_other_pr("b/c.txt", "c2 from someone else\n")
        write(repo.work, "b/c.txt", "c-local edit after the snapshot\n")
        write(repo.work, "a.txt", "a3\n")

        state = sync(ctl)

        assert state["phase"] == "failed"
        assert state["blocking"] == ["b/c.txt"] and "b/c.txt" in state["error"]
        assert "SYNC again" in state["error"]
        assert gh.prs[137]["state"] == "MERGED" and state["pr"]["state"] == "MERGED"
        # Safe: main sits at the snapshot and every local edit is where it was.
        assert repo.head() == shipped["snapshot"]
        assert git(repo.work, "symbolic-ref", "--short", "HEAD") == "main"
        assert repo.read("b/c.txt") == "c-local edit after the snapshot\n"
        assert repo.read("a.txt") == "a3\n"

        write(repo.work, "b/c.txt", "c1\n")  # the operator resolves it (back to S's copy)
        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        assert repo.head() == git(repo.origin, "rev-parse", "main")
        assert repo.read("b/c.txt") == "c2 from someone else\n"
        assert repo.read("a.txt") == "a3\n" and repo.status() == [" M a.txt"]
        assert len([c for c in gh.calls if c[1:3] == ["pr", "merge"]]) == 1

    def test_an_ignored_local_file_the_other_pr_tracks_is_not_overwritten(self, repo, rig):
        ctl, _shipped = self._shipped(repo, rig)
        git(repo.other, "pull", "-q", "--ff-only")
        write(repo.other, "cache.log", "theirs\n")
        git(repo.other, "add", "-f", "cache.log")
        git(repo.other, "commit", "-q", "-m", "track an ignored name")
        git(repo.other, "push", "-q", "origin", "main")
        write(repo.work, "cache.log", "mine, ignored here\n")

        state = sync(ctl)

        assert state["phase"] == "failed" and state["blocking"] == ["cache.log"]
        assert repo.read("cache.log") == "mine, ignored here\n"

    def test_failing_checks_refuse(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh.rollup = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
        ctl._pr_checked = float("-inf")
        ctl.state()  # the poll refreshes the summary
        status, body = ctl.handle_sync()
        assert status == 409 and "checks are failing" in body["error"]
        assert gh.prs[137]["state"] == "OPEN"

    def test_pending_checks_refuse_with_one_line(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh.rollup = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]
        ctl._pr_checked = float("-inf")
        ctl.state()
        status, body = ctl.handle_sync()
        assert status == 409 and body["error"] == ship_mod.checks_running_error(137)
        assert "\n" not in body["error"] and gh.prs[137]["state"] == "OPEN"

    def test_a_stale_pending_cache_is_reread_on_the_click(self, repo, rig, gh):
        # The cache says pending; GitHub has since passed. The click must not
        # refuse on a 20 s old answer.
        ctl, _shipped = self._shipped(repo, rig)
        gh.rollup = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]
        ctl._pr_checked = float("-inf")
        ctl.state()
        gh.rollup = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        assert sync(ctl)["phase"] == "idle"

    def test_merged_by_hand_while_the_cache_says_pending_still_syncs(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh.rollup = [{"__typename": "StatusContext", "state": "PENDING"}]
        ctl._pr_checked = float("-inf")
        ctl.state()
        gh(["gh", "pr", "merge", "137", "--squash", "--repo", REPO_SLUG])  # on GitHub, by hand
        state = sync(ctl)
        assert state["phase"] == "idle", state["error"]
        assert len([c for c in gh.calls if c[1:3] == ["pr", "merge"]]) == 1

    def test_conflicting_refuses(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh.mergeable = "CONFLICTING"
        ctl._pr_checked = float("-inf")
        ctl.state()
        status, body = ctl.handle_sync()
        assert status == 409 and "conflicts with main" in body["error"]

    def test_a_pr_merged_on_github_syncs_without_merging_again(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh(["gh", "pr", "merge", "137", "--squash", "--repo", REPO_SLUG])

        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        assert repo.head() == gh.prs[137]["merge_commit"]
        assert len([c for c in gh.calls if c[1:3] == ["pr", "merge"]]) == 1

    def test_sync_without_a_ship_is_409(self, rig):
        status, body = rig.controller().handle_sync()
        assert status == 409 and "SHIP first" in body["error"]

    def test_a_restart_mid_sync_resumes(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        rig.defer = True
        assert ctl.handle_sync()[0] == 202
        assert ctl.busy_reasons() == ["syncing ship PR #137"]

        rig.defer = False
        restarted = rig.controller()  # the deferred worker died with the old process
        state = restarted.state()[1]["ship"]
        assert state["phase"] == "failed" and "restarted mid-SYNC" in state["error"]
        assert restarted.busy_reasons() == []
        assert sync(restarted)["phase"] == "idle"

    def test_a_restart_after_checkout_just_records_the_landing(self, repo, rig, gh):
        ctl, _shipped = self._shipped(repo, rig)
        gh(["gh", "pr", "merge", "137", "--squash", "--repo", REPO_SLUG])
        git(repo.work, "fetch", "-q", "origin")
        git(repo.work, "reset", "-q", "--hard", "origin/main")  # as if SYNC finished
        ctl._pr_checked = float("-inf")
        ctl.state()  # learn MERGED + the merge commit

        state = sync(ctl)

        assert state["phase"] == "idle", state["error"]
        assert state["last"]["landed"] == gh.prs[137]["merge_commit"]


# ── CANCEL, persistence, busy ────────────────────────────────────────────────


class TestCancelAndState:
    def test_cancel_closes_the_pr_and_never_touches_the_tree(self, repo, rig, gh):
        dirty_the_tree(repo)
        ctl = rig.controller()
        shipped = ship(rig, ctl)
        before = (repo.head(), repo.index_digest(), repo.status())

        status, body = ctl.handle_cancel()

        assert status == 200 and body["ok"] is True and body["ship"]["phase"] == "idle"
        assert gh.prs[137]["state"] == "CLOSED"
        assert git(repo.origin, "branch", "--list", shipped["branch"]) == ""
        assert (repo.head(), repo.index_digest(), repo.status()) == before

    def test_cancel_when_idle_is_ok(self, rig):
        status, body = rig.controller().handle_cancel()
        assert status == 200 and body["ok"] is True and body["ship"]["phase"] == "idle"

    def test_cancel_after_a_failed_ship_clears_it(self, repo, rig):
        dirty_the_tree(repo)
        git(repo.work, "remote", "set-url", "origin", str(repo.work.parent / "nowhere.git"))
        ctl = rig.controller()
        assert ship(rig, ctl)["phase"] == "failed"
        status, body = ctl.handle_cancel()
        assert status == 200 and body["ship"]["phase"] == "idle" and body["ship"]["branch"] is None

    def test_cancel_refuses_a_merged_pr(self, repo, rig, gh):
        dirty_the_tree(repo)
        ctl = rig.controller()
        ship(rig, ctl)
        gh(["gh", "pr", "merge", "137", "--squash", "--repo", REPO_SLUG])
        status, body = ctl.handle_cancel()
        assert status == 409 and "SYNC instead" in body["error"]

    def test_an_open_ship_survives_a_restart(self, repo, rig):
        dirty_the_tree(repo)
        shipped = ship(rig, rig.controller())
        again = rig.controller().state()[1]["ship"]
        for key in ("phase", "base", "snapshot", "branch", "title"):
            assert again[key] == shipped[key], key
        assert again["pr"]["number"] == 137

    def test_a_restart_mid_ship_is_failed_and_not_busy(self, repo, rig):
        dirty_the_tree(repo)
        rig.defer = True
        ctl = rig.controller()
        assert ctl.handle_ship({})[0] == 202
        assert ctl.busy_reasons() == ["shipping main"]
        restarted = rig.controller()
        state = restarted.state()[1]["ship"]
        assert state["phase"] == "failed" and "restarted mid-SHIP" in state["error"]
        assert restarted.busy_reasons() == []

    def test_the_block_carries_the_contract_keys(self, repo, rig):
        dirty_the_tree(repo)
        status, body = rig.controller().state()
        ship_block = body["ship"]
        assert status == 200 and body["ok"] is True
        for key in (
            "phase",
            "branch_now",
            "head",
            "dirty",
            "ahead",
            "branch",
            "base",
            "snapshot",
            "title",
            "pr",
            "error",
            "last",
        ):
            assert key in ship_block, key
        assert ship_block["phase"] == "idle" and ship_block["branch_now"] == "main"
        assert ship_block["dirty"] == 3 and ship_block["ahead"] == 0  # debug.log is ignored
        assert repo.head().startswith(ship_block["head"])

    def test_a_thread_that_never_starts_does_not_wedge_the_phase(self, repo, rig):
        dirty_the_tree(repo)
        ctl = rig.controller()
        ctl._start_thread = lambda _target, _name: (_ for _ in ()).throw(RuntimeError("no threads"))
        status, _body = ctl.handle_ship({})
        assert status == 500
        assert ctl.state()[1]["ship"]["phase"] == "failed" and ctl.busy_reasons() == []


# ── pure ─────────────────────────────────────────────────────────────────────


class TestPure:
    def test_checks_summary(self):
        run = lambda status, conclusion="": {
            "__typename": "CheckRun",
            "status": status,  # noqa: E731
            "conclusion": conclusion,
        }
        ctx = lambda state: {"__typename": "StatusContext", "state": state}  # noqa: E731
        assert ship_mod.summarize_checks([]) == "none"
        assert ship_mod.summarize_checks(None) == "none"
        assert ship_mod.summarize_checks([run("COMPLETED", "SUCCESS"), ctx("SUCCESS")]) == "passing"
        assert ship_mod.summarize_checks([run("COMPLETED", "SKIPPED"), run("IN_PROGRESS")]) == "pending"
        assert ship_mod.summarize_checks([ctx("PENDING"), run("COMPLETED", "FAILURE")]) == "failing"
        assert ship_mod.summarize_checks([ctx("ERROR")]) == "failing"

    def test_merge_flag_prefers_squash(self):
        assert ship_mod.choose_merge_flag({"squashMergeAllowed": True, "mergeCommitAllowed": True}) == "--squash"
        assert ship_mod.choose_merge_flag({"squashMergeAllowed": False, "mergeCommitAllowed": True}) == "--merge"
        assert ship_mod.choose_merge_flag({"rebaseMergeAllowed": True}) == "--rebase"
        assert ship_mod.choose_merge_flag({}) is None

    def test_default_title(self):
        paths = ["app/a", "app/b", "app/c", ".claude/x", ".claude/y", "docs/z", "CLAUDE.md"]
        assert ship_mod.default_title(paths) == "iteration: app, .claude, CLAUDE.md (+7 files)"
        assert ship_mod.default_title(["a"]) == "iteration: a (+1 file)"

    def test_pr_number_from_url(self):
        assert ship_mod.pr_number_from_url("https://github.com/o/r/pull/137") == 137
        assert ship_mod.pr_number_from_url("nope") is None

    def test_a_corrupt_store_loads_idle(self, tmp_path):
        path = tmp_path / "ship.json"
        path.write_text("{not json")
        assert ship_mod.ShipStore(path).load()["phase"] == "idle"


# ── the plugin routes ────────────────────────────────────────────────────────


def test_the_plugin_mounts_the_ship_routes(repo, rig, monkeypatch, tmp_path):
    from worktree.plugin import WorktreePlugin

    class Context:
        start_dir = str(repo.work)

        def log(self, _line):
            pass

    monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "prs.json"))
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "managed"))
    plugin = WorktreePlugin(Context())
    assert plugin._main_checkout() == repo.work.resolve() or plugin._main_checkout() == repo.work
    plugin._ship = rig.controller()
    for route in ("ship-state", "ship", "ship-sync", "ship-cancel"):
        assert f"/plugin/worktree/{route}" in plugin.routes()

    status, body = plugin.handle_get("/ship-state", {})
    assert status == 200 and body["ship"]["phase"] == "idle"
    dirty_the_tree(repo)
    status, body = plugin.handle_post("/ship", {}, {"title": "via the plugin"})
    assert status == 202 and body["ship"]["phase"] == "shipping"
    assert plugin.handle_get("/ship-state", {})[1]["ship"]["title"] == "via the plugin"
    assert plugin.handle_post("/ship-sync", {}, None)[0] == 202
    assert plugin.handle_get("/ship-state", {})[1]["ship"]["last"]["pr"] == 137
    assert plugin.handle_post("/ship-cancel", {}, None)[0] == 200
    assert plugin.busy_reasons() == []
