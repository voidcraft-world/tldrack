"""`wt sync` — the merge classifier, git's refusal text, and the orchestration.

The stderr fixtures are git 2.54's wording, with invented paths. A refusal and a
conflict BOTH exit 1; only `MERGE_HEAD` separates them. Fenced like `test_cli.py`: no test reaches a real subprocess.
"""

from pathlib import Path

import pytest

from worktree import create as create_mod
from worktree import sync

PATH = Path("/tmp/wt-root/long-lived")
REPO = Path("/tmp/wt-repo")
BOOT = Path("/b.sh")

REFUSED_STDERR = (
    "error: Your local changes to the following files would be overwritten by merge:\n"
    "\tapp/vite.config.ts\n"
    "\tapp/src/main.ts\n"
    "Please commit your changes or stash them before you merge.\n"
    "Aborting\n"
)
UNTRACKED_STDERR = (
    "error: The following untracked working tree files would be overwritten by merge:\n"
    "\tapp/src/config/flags.ts\n"
    "Please move or remove them before you merge.\n"
    "Aborting\n"
)
CONFLICT_STDOUT = (
    "Auto-merging f\nCONFLICT (content): Merge conflict in f\nAutomatic merge failed; fix conflicts and then commit the result.\n"
)


class TestParseBlockingPaths:
    def test_local_changes(self):
        assert sync.parse_blocking_paths(REFUSED_STDERR) == ["app/vite.config.ts", "app/src/main.ts"]

    def test_untracked_files(self):
        assert sync.parse_blocking_paths(UNTRACKED_STDERR) == ["app/src/config/flags.ts"]

    def test_both_headers_in_one_refusal_dedupe(self):
        text = REFUSED_STDERR + UNTRACKED_STDERR + "\tapp/src/main.ts\n"
        assert sync.parse_blocking_paths(text) == ["app/vite.config.ts", "app/src/main.ts", "app/src/config/flags.ts"]

    def test_tab_lines_outside_a_header_do_not_count(self):
        assert sync.parse_blocking_paths("hint: something\n\tnot-a-path\n") == []

    def test_non_text_is_empty(self):
        for value in (None, 1, b"bytes"):
            assert sync.parse_blocking_paths(value) == [], value


class TestClassifyMerge:
    def test_merge_head_means_conflict_whatever_the_code(self):
        assert sync.classify_merge(1, CONFLICT_STDOUT, "", merge_in_progress=True) == sync.STATUS_CONFLICT

    def test_already_up_to_date(self):
        assert sync.classify_merge(0, "Already up to date.\n", "", merge_in_progress=False) == sync.STATUS_UP_TO_DATE

    def test_fast_forward_or_merge_commit(self):
        for out in ("Updating a..b\nFast-forward\n f | 2 +-\n", "Merge made by the 'ort' strategy.\n"):
            assert sync.classify_merge(0, out, "", merge_in_progress=False) == sync.STATUS_MERGED

    def test_refusal_exits_1_without_merge_head(self):
        assert sync.classify_merge(1, "Updating a..b\n", REFUSED_STDERR, merge_in_progress=False) == sync.STATUS_BLOCKED

    def test_anything_else_is_unknown(self):
        assert sync.classify_merge(128, "", "fatal: not something we can merge", merge_in_progress=False) is None


class TestSyncWorktree:
    """`sync_worktree()` against a scripted git: each command answers from a table."""

    @pytest.fixture
    def git(self, monkeypatch):
        state = {
            "merge": (0, "Merge made by the 'ort' strategy.\n", ""),
            "merge_head_before": False,
            "merge_head_after": False,
            "behind": (0, "7\n", ""),
            "unmerged": "",
            "calls": [],
            "boot": [],
        }

        def fake(command, _timeout, **_kw):
            state["calls"].append(command)
            verb = command[3]
            if verb == "rev-parse":
                ran_merge = any(c[3] == "merge" for c in state["calls"][:-1])
                on = state["merge_head_after"] if ran_merge else state["merge_head_before"]
                return (0, "abc\n", "") if on else (1, "", "")
            if verb == "rev-list":
                return state["behind"]
            if verb == "merge":
                return state["merge"]
            if verb == "diff":
                return 0, state["unmerged"], ""
            raise AssertionError(f"unexpected git call: {command}")

        monkeypatch.setattr(create_mod, "run_bounded", fake)
        monkeypatch.setattr(create_mod, "run_bootstrap", lambda *a: state["boot"].append(a) or None)
        return state

    def run(self):
        return sync.sync_worktree(PATH, repo_root=REPO, base="main", bootstrap=BOOT)

    def test_merge_reports_count_and_bootstraps(self, git):
        body = self.run()
        assert body["status"] == sync.STATUS_MERGED and body["merged"] == 7
        assert git["boot"] == [(BOOT, PATH, REPO)]
        (merge_call,) = [c for c in git["calls"] if c[3] == "merge"]
        assert merge_call == ["git", "-C", str(PATH), "merge", "--no-edit", "main"]

    def test_up_to_date_still_bootstraps(self, git):
        # The contract: exit 0 means merged AND bootstrapped, so a sync run right
        # after committing a conflict resolution still refreshes the types.
        git["merge"] = (0, "Already up to date.\n", "")
        body = self.run()
        assert body["status"] == sync.STATUS_UP_TO_DATE and body["merged"] == 0
        assert len(git["boot"]) == 1

    def test_conflict_lists_paths_and_skips_bootstrap(self, git):
        git["merge"] = (1, CONFLICT_STDOUT, "")
        git["merge_head_after"] = True
        git["unmerged"] = "app/src/main.ts\napp/vite.config.ts\n"
        body = self.run()
        assert body["status"] == sync.STATUS_CONFLICT
        assert body["conflicts"] == ["app/src/main.ts", "app/vite.config.ts"]
        assert git["boot"] == []

    def test_merge_already_in_progress_is_reported_not_restarted(self, git):
        git["merge_head_before"] = True
        git["unmerged"] = "f\n"
        body = self.run()
        assert body["status"] == sync.STATUS_CONFLICT and body["conflicts"] == ["f"]
        assert not any(c[3] == "merge" for c in git["calls"])

    def test_blocked_names_the_files_and_changes_nothing(self, git):
        git["merge"] = (1, "Updating a..b\n", REFUSED_STDERR)
        body = self.run()
        assert body["status"] == sync.STATUS_BLOCKED
        assert body["blocking"] == ["app/vite.config.ts", "app/src/main.ts"]
        assert git["boot"] == []

    def test_unknown_failure_is_an_error_without_status(self, git):
        git["merge"] = (128, "", "fatal: 'main' does not point to a commit\n")
        body = self.run()
        assert "status" not in body and "does not point to a commit" in body["error"]
        assert git["boot"] == []

    def test_uncountable_behind_is_none_not_zero(self, git):
        git["behind"] = (128, "", "fatal")
        assert self.run()["merged"] is None


class TestSyncCommand:
    """`cli.sync()` refusals and `wt sync` stdout: one JSON line, prose on stderr."""

    @pytest.fixture
    def cli(self, monkeypatch):
        from worktree import cli as cli_mod

        monkeypatch.setattr(create_mod, "tool_found", lambda _n: True)
        monkeypatch.setattr(cli_mod, "branch_held_at", lambda _r, _b: str(PATH))
        monkeypatch.setattr(cli_mod, "resolve_repo_root", lambda _cwd: REPO)
        return cli_mod

    def test_base_branch_is_refused(self, cli):
        code, body = cli.sync("main", repo_root=REPO, base="main")
        assert code == cli.EXIT_REFUSED and "is the base branch" in body["error"]

    def test_no_worktree_names_the_create_command(self, cli, monkeypatch):
        monkeypatch.setattr(cli, "branch_held_at", lambda _r, _b: None)
        code, body = cli.sync("long-lived", repo_root=REPO, base="main")
        assert code == cli.EXIT_REFUSED and "wt create long-lived" in body["error"]

    def test_main_checkout_is_refused(self, cli, monkeypatch):
        monkeypatch.setattr(cli, "branch_held_at", lambda _r, _b: str(REPO))
        code, body = cli.sync("long-lived", repo_root=REPO, base="main")
        assert code == cli.EXIT_REFUSED and "main checkout" in body["error"]

    @pytest.mark.parametrize(
        "status, expected",
        [
            (sync.STATUS_MERGED, 0),
            (sync.STATUS_UP_TO_DATE, 0),
            (sync.STATUS_CONFLICT, 4),
            (sync.STATUS_BLOCKED, 1),
            (None, 1),
        ],
    )
    def test_exit_code_per_status(self, cli, monkeypatch, status, expected):
        def fake(path, **_k):
            body = {
                "path": str(path),
                "base": "main",
                "merged": 3,
                "conflicts": ["f"],
                "blocking": ["g"],
                "bootstrap_error": None,
            }
            if status is None:
                body["error"] = "fatal: nope"
            else:
                body["status"] = status
            return body

        monkeypatch.setattr(sync, "sync_worktree", fake)
        code, body = cli.sync("long-lived", repo_root=REPO, base="main")
        assert code == expected and body["branch"] == "long-lived"

    def test_stdout_is_one_json_line(self, cli, monkeypatch, capsys):
        import json

        monkeypatch.setattr(
            sync,
            "sync_worktree",
            lambda path, **_k: {
                "path": str(path),
                "base": "main",
                "merged": 1,
                "conflicts": [],
                "blocking": [],
                "bootstrap_error": "could not link x",
                "status": sync.STATUS_MERGED,
            },
        )
        assert cli.main(["sync", "long-lived", "--base", "main"]) == 0
        out, err = capsys.readouterr()
        assert out.count("\n") == 1 and json.loads(out)["status"] == sync.STATUS_MERGED
        assert "merged 1 commit from main into 'long-lived'" in err and "could not link x" in err

    def test_conflict_summary_names_the_next_step(self, cli):
        line = cli.sync_summary(
            {"branch": "long-lived", "base": "main", "path": str(PATH), "status": sync.STATUS_CONFLICT, "conflicts": ["a", "b"]}
        )
        assert "CONFLICT in 2 files" in line and "git commit --no-edit" in line
