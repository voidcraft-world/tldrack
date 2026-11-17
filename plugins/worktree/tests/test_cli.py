"""`wt` — the reopen-or-refuse table, the hook envelope, and stdout purity.

Fenced like `test_create_progress.py`: `worktree.create.run_bounded` raises, so
a test that reaches a subprocess fails loudly instead of touching the machine.
"""

import json
from pathlib import Path

import pytest

from worktree import cli
from worktree import create as create_mod
from worktree.repo import RepoHooks

HOME = Path("/tmp/wt-home")
REPO = Path("/tmp/wt-repo")
ROOT = HOME / "wt-repo"  # `repo.worktree_root_for(REPO)`
BRANCH = "feat/foo"
PATH = ROOT / "feat-foo"


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    def boom(command, *_a, **_k):
        raise AssertionError(f"subprocess reached: {command[:3]}")

    monkeypatch.setattr("worktree.create.run_bounded", boom)
    monkeypatch.setattr("worktree.cli.list_worktrees", lambda _root: [])
    monkeypatch.setattr("worktree.listing.list_worktrees", lambda _root: [])
    monkeypatch.setenv("WORKTREE_ROOT", str(HOME))
    monkeypatch.delenv("WORKTREE_BASE_BRANCH", raising=False)


class TestDecideOpen:
    def test_fresh_branch_is_new(self):
        assert cli.decide_open(None, branch_exists=False, held_at=None, path=PATH, path_exists=False) == (cli.ACTION_NEW, None)

    def test_branch_error_wins(self):
        action, reason = cli.decide_open("bad name", branch_exists=False, held_at=None, path=PATH, path_exists=False)
        assert action is None and reason == "bad name"

    def test_branch_at_its_own_path_reopens(self):
        # `claude -w <name>` twice: the second is "open it again", not a conflict.
        assert cli.decide_open(None, branch_exists=True, held_at=str(PATH), path=PATH, path_exists=True) == (
            cli.ACTION_REOPEN,
            None,
        )

    def test_branch_held_elsewhere_is_refused_with_where(self):
        action, reason = cli.decide_open(None, branch_exists=True, held_at="/elsewhere/x", path=PATH, path_exists=False)
        assert action is None and "already checked out at" in reason and "/elsewhere/x" in reason

    def test_branch_without_worktree_checks_out_existing(self):
        # `POST /remove` keeps the branch; reopening it later is this arm.
        assert cli.decide_open(None, branch_exists=True, held_at=None, path=PATH, path_exists=False) == (
            cli.ACTION_EXISTING,
            None,
        )

    def test_directory_squatting_is_refused_either_way(self):
        for exists in (True, False):
            action, reason = cli.decide_open(None, branch_exists=exists, held_at=None, path=PATH, path_exists=True)
            assert action is None and "already on disk" in reason, exists


class TestHookPayload:
    def test_dict_passes_through(self):
        assert cli.parse_hook_payload('{"name": "x", "cwd": "/r"}') == {"name": "x", "cwd": "/r"}

    def test_anything_else_is_empty(self):
        for text in ("", "   ", "not json", "[1,2]", '"str"', None, 42):
            assert cli.parse_hook_payload(text) == {}, text

    def test_hook_output_is_the_bare_path(self):
        # Claude Code reads the LAST LINE of stdout as the directory, literally
        # (measured; its error text says so). Not JSON, not quoted, one line.
        out = cli.hook_create_output(PATH)
        assert out == str(PATH) and "\n" not in out


class TestCreateOrchestration:
    """`create()` with every machine call fenced to a canned answer."""

    @pytest.fixture
    def fence(self, monkeypatch):
        calls = {"add": [], "bootstrap": []}
        monkeypatch.setattr(create_mod, "tool_found", lambda _n: True)
        monkeypatch.setattr(create_mod, "branch_exists", lambda _r, _b: False)
        monkeypatch.setattr(create_mod, "add_worktree", lambda *a, **k: calls["add"].append((a, k)) or None)
        monkeypatch.setattr(create_mod, "run_bootstrap", lambda *a: calls["bootstrap"].append(a) or "no baselines")
        monkeypatch.setattr(cli, "read_repo_hooks", lambda _r: RepoHooks(bootstrap=Path("/b.sh")))
        monkeypatch.setattr(create_mod, "current_branch", lambda _r: None)
        monkeypatch.setattr(Path, "exists", lambda _self: False)
        return calls

    def test_new_branch_adds_bootstraps_and_names(self, fence):
        code, body = cli.create(BRANCH, repo_root=REPO, base="main")
        assert code == 0
        assert body["path"] == str(PATH) and body["session"] == "wt-feat-foo"
        assert body["action"] == cli.ACTION_NEW
        assert body["bootstrap_error"] == "no baselines"  # a NOTE, not a refusal
        ((args, kwargs),) = fence["add"]
        assert args == (REPO, PATH, BRANCH, "main") and kwargs == {"existing": False, "sparse_exclude": ()}
        assert fence["bootstrap"] == [(Path("/b.sh"), PATH, REPO)]

    def test_reopen_touches_nothing(self, fence, monkeypatch):
        monkeypatch.setattr(create_mod, "branch_exists", lambda _r, _b: True)
        monkeypatch.setattr(cli, "branch_held_at", lambda _r, _b: str(PATH))
        monkeypatch.setattr(Path, "exists", lambda _self: True)
        code, body = cli.create(BRANCH, repo_root=REPO, root=ROOT, base="main")
        assert code == 0 and body["action"] == cli.ACTION_REOPEN
        assert fence["add"] == [] and fence["bootstrap"] == []

    def test_invalid_name_is_refused_before_any_git(self, fence):
        code, body = cli.create("#1234", repo_root=REPO, root=ROOT)
        assert code == cli.EXIT_REFUSED and "letters, digits" in body["error"]
        assert fence["add"] == []

    def test_git_add_failure_is_the_refusal(self, fence, monkeypatch):
        monkeypatch.setattr(create_mod, "add_worktree", lambda *a, **k: (500, {"error": "fatal: nope"}))
        code, body = cli.create(BRANCH, repo_root=REPO, root=ROOT)
        assert code == cli.EXIT_REFUSED and body["error"] == "fatal: nope"
        assert fence["bootstrap"] == []


class TestStdoutPurity:
    """Two callers parse stdout — the shell and Claude Code. One line, nothing else."""

    def test_hook_create_prints_only_the_envelope(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: REPO)
        monkeypatch.setattr(
            cli,
            "create",
            lambda *a, **k: (
                0,
                {
                    "path": str(PATH),
                    "branch": BRANCH,
                    "session": "wt-feat-foo",
                    "action": cli.ACTION_NEW,
                    "bootstrap_error": "could not link x",
                },
            ),
        )
        monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"name": BRANCH, "cwd": str(REPO)})))
        assert cli.main(["hook-create"]) == 0
        out, err = capsys.readouterr()
        assert out == cli.hook_create_output(PATH) + "\n"
        assert "could not link x" in err  # the note went to stderr, not into the envelope

    def test_hook_create_without_a_name_fails_creation(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", _Stdin("{}"))
        assert cli.main(["hook-create"]) == cli.EXIT_REFUSED
        assert capsys.readouterr().out == ""

    def test_create_prints_the_path_and_talks_on_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: REPO)
        monkeypatch.setattr(
            cli,
            "create",
            lambda *a, **k: (
                0,
                {
                    "path": str(PATH),
                    "branch": BRANCH,
                    "session": "wt-feat-foo",
                    "action": cli.ACTION_EXISTING,
                    "bootstrap_error": None,
                },
            ),
        )
        assert cli.main(["create", BRANCH]) == 0
        out, err = capsys.readouterr()
        assert out == str(PATH) + "\n"
        assert "checked out" in err and "wt-feat-foo" in err

    def test_path_and_session_are_pure_names(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: REPO)
        assert cli.main(["path", BRANCH]) == 0
        assert cli.main(["session", BRANCH]) == 0
        assert capsys.readouterr().out == f"{PATH}\nwt-feat-foo\n"


class TestHookRemove:
    def test_unmanaged_path_is_left_alone_and_does_not_block(self, monkeypatch, capsys):
        called = []
        monkeypatch.setattr(cli, "remove", lambda *a, **k: called.append(a) or (0, {}))
        monkeypatch.setattr(
            "sys.stdin", _Stdin(json.dumps({"worktree_path": "/tmp/demo-app/.claude/worktrees/foo", "cwd": str(REPO)}))
        )
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: REPO)
        assert cli.main(["hook-remove"]) == 0
        assert called == []
        assert "left alone" in capsys.readouterr().err

    def test_managed_path_removes_by_its_branch_with_force(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: REPO)
        monkeypatch.setattr(cli, "branch_at_path", lambda _r, _p: BRANCH)
        monkeypatch.setattr(
            cli,
            "remove",
            lambda branch, **k: (
                seen.update(branch=branch, **k)
                or (0, {"path": str(PATH), "branch": branch, "removed": True, "branch_deleted": True, "branch_note": None})
            ),
        )
        monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"worktree_path": str(PATH), "cwd": str(REPO)})))
        assert cli.main(["hook-remove"]) == 0
        # The user already chose "remove" over "keep" — the work is forfeited.
        assert seen["branch"] == BRANCH and seen["force"] is True

    def test_missing_path_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", _Stdin("{}"))
        assert cli.main(["hook-remove"]) == 0


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class TestEveryRepository:
    """The repository is always the caller's: its root, its base, its hook."""

    def test_the_root_follows_the_repository(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: Path("/tmp/other-app"))
        assert cli.main(["path", BRANCH]) == 0
        assert capsys.readouterr().out == f"{HOME / 'other-app' / 'feat-foo'}\n"

    def test_the_repositorys_base_and_sparse_paths_reach_git(self, monkeypatch):
        calls = []
        monkeypatch.setattr(create_mod, "tool_found", lambda _n: True)
        monkeypatch.setattr(create_mod, "branch_exists", lambda _r, _b: False)
        monkeypatch.setattr(create_mod, "add_worktree", lambda *a, **k: calls.append((a, k)) or None)
        monkeypatch.setattr(Path, "exists", lambda _self: False)
        monkeypatch.setattr(cli, "read_repo_hooks", lambda _r: RepoHooks(base="trunk", sparse_exclude=("assets/big",)))
        code, body = cli.create(BRANCH, repo_root=REPO)
        assert code == 0 and body["bootstrap_error"] is None  # no hook: nothing ran, nothing failed
        ((args, kwargs),) = calls
        assert args[3] == "trunk" and kwargs["sparse_exclude"] == ("assets/big",)

    def test_a_hook_remove_outside_any_repository_does_not_block(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_repo_root", lambda _cwd: None)
        monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"worktree_path": "/tmp/nowhere/x"})))
        assert cli.main(["hook-remove"]) == 0
        assert "left alone" in capsys.readouterr().err
