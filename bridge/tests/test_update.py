"""update.py: the checkout against its remote, the staged page build, the run and its routes.

git is real here (a remote, a clone, commits), because what the module must get right is what git
does with a branch, an edit and a fast-forward. npm, uv, the preflight and the restart are fakes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tldrack import main, update
from tldrack.features import AUTO_UPDATE, UPDATE_CHECK, FeatureSwitches
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.update import GitState, Updater
from test_freshness import monitor_for, tree, write  # noqa: F401
from test_session_routes import Recorder, _with_body

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, path: str, text: str, message: str) -> None:
    write(repo / path, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class Rack:
    """A remote, the operator's checkout of it, and a second clone that plays "a pull request merged"."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.upstream = tmp / "upstream"
        git(tmp, "clone", "-q", str(self.origin), str(self.upstream))
        for path, text in {
            "ui/package.json": '{"scripts": {"build": "vite build"}}\n',
            "ui/package-lock.json": "{}\n",
            "ui/src/app.ts": "export const page = 1;\n",
            "schemas/index.ts": "export {};\n",
            "bridge/uv.lock": "version = 1\n",
            "bridge/tldrack/code.py": "VALUE = 1\n",
        }.items():
            write(self.upstream / path, text)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", "first")
        git(self.upstream, "push", "-q", "origin", "main")
        self.root = tmp / "checkout"
        git(tmp, "clone", "-q", str(self.origin), str(self.root))
        git(self.root, "remote", "set-head", "origin", "main")
        # An installed checkout: dependencies in place, and a page somebody built with no stamp.
        write(self.root / "ui" / "node_modules" / ".package-lock.json", "{}\n")
        write(self.root / "ui" / "dist" / "index.html", "<html>old page</html>\n")
        self.calls: list[list[str]] = []
        self.build_exit = 0
        self.stale = False
        self.preflight = ("ok", None)
        self.busy: list[str] = []
        self.can_restart = True
        self.restarts = 0

    def land(self, path: str, text: str, message: str) -> None:
        """A commit reaches the remote's main branch."""
        commit(self.upstream, path, text, message)
        git(self.upstream, "push", "-q", "origin", "main")

    def run(self, argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] == "npm" and argv[1:3] == ["run", "build"]:
            if self.build_exit:
                return self.build_exit, "error TS2304: cannot find name"
            out = Path(argv[argv.index("--outDir") + 1])
            write(out / "index.html", f"<html>{(cwd / 'src' / 'app.ts').read_text()}</html>\n")
            return 0, "built"
        if argv[0] in ("npm", "uv"):
            return 0, ""
        done = subprocess.run(argv, cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout + done.stderr

    def ran(self, *head: str) -> bool:
        return any(call[: len(head)] == list(head) for call in self.calls)

    def restart(self) -> bool:
        self.restarts += 1
        return self.can_restart

    def updater(self, switches: FeatureSwitches, *, bound: bool = True) -> Updater:
        updater = Updater(
            self.root,
            switches=switches,
            find_ui=lambda: self.root / "ui" / "dist" if (self.root / "ui" / "dist" / "index.html").is_file() else None,
            run=self.run,
            which=lambda name: name,
            project_venv=lambda root: True,
        )
        if bound:
            updater.bind(
                stale=lambda: self.stale,
                preflight=lambda: (self.stale, *self.preflight) if self.stale else (False, None, None),
                busy=lambda: list(self.busy),
                restart=self.restart,
            )
        return updater

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def rack(tmp_path):
    return Rack(tmp_path)


@pytest.fixture
def switches(tmp_path):
    return FeatureSwitches(tmp_path / "features.json")


# --- reading the checkout -----------------------------------------------------


def test_a_checkout_level_with_its_remote_has_nothing_to_do(rack, switches):
    updater = rack.updater(switches)
    updater.check()
    view = updater.view()
    assert view["supported"] is True and view["reason"] is None
    assert view["branch"] == "main" and view["upstream"] == "origin/main"
    assert view["behind"] == 0 and view["ahead"] == 0 and view["dirty"] == 0 and view["incoming"] == []
    assert view["head"] == view["target"] and view["blocked"] is None
    assert view["available"] is False and view["phase"] == "idle" and view["error"] is None
    assert view["checked_at"] is not None and view["check_error"] is None
    assert view["page"]["source"] == "unknown"  # a build nobody stamped asks for nothing
    assert json.dumps(view)


def test_a_commit_on_the_remote_is_seen_by_the_next_check_not_before(rack, switches):
    updater = rack.updater(switches)
    updater.check()
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    assert updater.view()["behind"] == 0  # a view is the last look, never a fetch
    updater.check()
    view = updater.view()
    assert view["behind"] == 1 and view["available"] is True and view["blocked"] is None
    assert view["incoming"] == [{"sha": view["target"], "subject": "A new page"}]
    assert view["head"] != view["target"]


def test_the_first_view_reads_the_checkout_with_no_fetch(rack, switches):
    view = rack.updater(switches).view()
    assert view["branch"] == "main" and view["checked_at"] is None
    assert not rack.ran("git", "fetch")


def test_a_remote_that_cannot_be_reached_is_said_and_the_checkout_is_still_read(rack, switches):
    git(rack.root, "remote", "set-url", "origin", str(rack.root.parent / "gone.git"))
    updater = rack.updater(switches)
    updater.check()
    view = updater.view()
    assert view["check_error"].startswith("could not reach `origin`") and view["branch"] == "main"
    assert view["phase"] == "idle"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (GitState(branch="main", upstream="origin/main", behind=0, dirty=3), None),
        (GitState(branch="main", upstream="origin/main", behind=2), None),
        (GitState(branch="docs/x", upstream="origin/main", behind=2), "the checkout is on branch `docs/x`, not `main`"),
        (GitState(branch=None, upstream="origin/main", behind=2), "the checkout is on a detached commit, not `main`"),
        (GitState(branch="main", upstream="origin/main", behind=2, dirty=1), "1 uncommitted change in the checkout"),
        (GitState(branch="main", upstream="origin/main", behind=2, dirty=2), "2 uncommitted changes in the checkout"),
        (GitState(branch="main", upstream="origin/main", behind=2, ahead=1), "1 local commit not on `origin/main`"),
        (GitState(branch="main", upstream=None, behind=2), "the checkout has no `origin` remote branch to follow"),
    ],
)
def test_why_a_pull_is_blocked(state, expected):
    assert update.why_blocked(state) == expected


# --- the page -----------------------------------------------------------------


def test_the_page_is_current_only_when_its_stamp_is_the_checkouts_source(rack, switches, tmp_path):
    dist = rack.root / "ui" / "dist"
    trees = update.read_git(rack.root, rack.run).trees
    assert trees is not None and update.page_source(dist, dist, trees) == "unknown"  # no stamp
    update.write_stamp(dist, trees)
    assert update.page_source(dist, dist, trees) == "current"
    assert update.page_source(dist, dist, ("other", trees[1])) == "stale"
    assert update.page_source(None, dist, trees) == "stale"  # no page at all: there is one to build
    assert update.page_source(dist, dist, None) == "unknown"  # source with uncommitted edits
    elsewhere = tmp_path / "served"
    write(elsewhere / "index.html", "x")
    update.write_stamp(elsewhere, ("other", "other"))
    assert update.page_source(elsewhere, dist, trees) == "unknown"  # not this checkout's build


def test_an_edit_under_ui_makes_the_source_unknown(rack):
    assert update.read_git(rack.root, rack.run).trees is not None
    write(rack.root / "ui" / "src" / "app.ts", "export const page = 99;\n")
    state = update.read_git(rack.root, rack.run)
    assert state.trees is None and state.dirty == 1
    write(rack.root / "bridge" / "tldrack" / "code.py", "VALUE = 2\n")
    assert update.read_git(rack.root, rack.run).dirty == 2


def test_the_page_id_follows_the_served_html(tmp_path):
    assert update.page_id(None) is None and update.page_id(tmp_path) is None
    write(tmp_path / "index.html", "one")
    first = update.page_id(tmp_path)
    write(tmp_path / "assets" / "app.js", "ignored: the html names its assets")
    assert update.page_id(tmp_path) == first
    write(tmp_path / "settings.html", "two")
    assert update.page_id(tmp_path) != first and len(first) == 16


def test_an_install_is_due_only_when_the_lockfile_is_newer(tmp_path):
    ui = tmp_path / "ui"
    write(ui / "package-lock.json", "{}")
    assert update.install_due(ui)  # nothing installed
    write(ui / "node_modules" / ".package-lock.json", "{}")
    os.utime(ui / "package-lock.json", (1000, 1000))
    os.utime(ui / "node_modules" / ".package-lock.json", (2000, 2000))
    assert not update.install_due(ui)
    os.utime(ui / "package-lock.json", (3000, 3000))
    assert update.install_due(ui)


def test_a_build_made_by_another_door_can_be_stamped(rack):
    update.stamp_build(rack.root, rack.run)
    dist = rack.root / "ui" / "dist"
    assert update.page_source(dist, dist, update.read_git(rack.root, rack.run).trees) == "current"


# --- the run ------------------------------------------------------------------


def test_an_update_pulls_and_rebuilds_the_page_and_leaves_python_alone(rack, switches):
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    updater.apply()
    view = updater.view()
    assert view["behind"] == 0 and view["head"] == view["target"] and view["error"] is None
    assert rack.head() == git(rack.upstream, "rev-parse", "HEAD")
    dist = rack.root / "ui" / "dist"
    assert "page = 2" in (dist / "index.html").read_text()
    assert view["page"]["source"] == "current" and view["available"] is False and view["phase"] == "idle"
    assert not rack.ran("npm", "ci") and not rack.ran("uv") and rack.restarts == 0
    assert not (rack.root / "ui" / "node_modules" / ".cache" / "tldrack-build").exists()
    assert git(rack.root, "status", "--porcelain", "--untracked-files=no") == ""  # the stamp changes no tracked file


def test_a_pull_that_leaves_the_page_source_alone_does_not_rebuild(rack, switches):
    updater = rack.updater(switches)
    rack.land("README.md", "words\n", "Docs only")
    updater.apply()
    assert updater.view()["behind"] == 0 and not rack.ran("npm")


def test_new_python_is_checked_and_then_the_bridge_restarts(rack, switches):
    updater = rack.updater(switches)
    rack.land("bridge/tldrack/code.py", "VALUE = 2\n", "New code")
    rack.stale = True
    updater.apply()
    assert rack.restarts == 1
    view = updater.view()
    assert view["phase"] == "restarting" and view["error"] is None and view["waiting"] is None
    assert updater.busy_reasons() == []  # the restart itself is not something to wait for


def test_the_restarted_bridge_knows_when_the_remote_was_last_asked(rack, switches, monkeypatch):
    monkeypatch.delenv(update.CHECKED_ENV, raising=False)
    updater = rack.updater(switches)
    rack.land("bridge/tldrack/code.py", "VALUE = 2\n", "New code")
    rack.stale = True
    updater.apply()
    checked_at = updater.view()["checked_at"]
    assert os.environ[update.CHECKED_ENV] == repr(checked_at)  # exec carries the environment across
    after = rack.updater(switches)  # the process the restart becomes
    assert after.view()["checked_at"] == checked_at
    assert update.CHECKED_ENV not in os.environ  # read once: no child process inherits it
    # A restart that did not happen hands nothing on, and a value nobody could have written is ignored.
    rack.can_restart = False
    updater = rack.updater(switches)
    updater.apply()
    assert update.CHECKED_ENV not in os.environ
    for bad in ("soon", "-5", "99999999999"):
        assert update.inherited_check_time({update.CHECKED_ENV: bad}) is None


def test_code_that_does_not_start_is_never_restarted_into(rack, switches):
    switches.save({"set": {AUTO_UPDATE: True}})
    updater = rack.updater(switches)
    rack.land("bridge/tldrack/code.py", "VALUE = 2\n", "New code")
    rack.stale, rack.preflight = True, ("failed", "import tldrack.main: SyntaxError")
    updater.apply()
    view = updater.view()
    assert rack.restarts == 0 and view["phase"] == "idle"
    assert view["error"] == "the new code does not start, so the bridge was not restarted: import tldrack.main: SyntaxError"
    assert view["available"] is True  # the code on disk is still not the code running
    # The timer leaves a failure alone until something moves.
    assert updater.auto_due() is False
    rack.land("bridge/tldrack/code.py", "VALUE = 3\n", "The fix")
    rack.preflight = ("ok", None)
    updater.check()
    assert updater.auto_due() is True
    updater.apply()
    assert rack.restarts == 1 and updater.view()["error"] is None


def test_a_busy_plugin_holds_the_restart(rack, switches):
    updater = rack.updater(switches)
    rack.land("bridge/tldrack/code.py", "VALUE = 2\n", "New code")
    rack.stale, rack.busy = True, ["creating worktree feat-x", "updating: checking that the new code starts"]
    updater.apply()
    view = updater.view()
    assert rack.restarts == 0 and view["phase"] == "idle" and view["error"] is None
    assert view["waiting"] == "the restart waits for: creating worktree feat-x"  # its own entry is not a reason
    rack.busy = []
    updater.apply()
    assert rack.restarts == 1 and updater.view()["waiting"] is None


def test_a_bridge_that_cannot_restart_itself_says_how(rack, switches):
    switches.save({"set": {AUTO_UPDATE: True}})
    updater = rack.updater(switches)
    rack.land("bridge/tldrack/code.py", "VALUE = 2\n", "New code")
    rack.stale, rack.can_restart = True, False
    updater.apply()
    view = updater.view()
    assert view["waiting"] == update.RESTART_BY_HAND and view["phase"] == "idle"
    assert updater.auto_due() is False  # the timer cannot do it either, so it does not keep trying


def test_changed_dependencies_are_installed_before_the_check(rack, switches):
    updater = rack.updater(switches)
    rack.land("bridge/uv.lock", "version = 2\n", "A new dependency")
    rack.stale = True
    updater.apply()
    sync = next(i for i, call in enumerate(rack.calls) if call[:2] == ["uv", "sync"])
    assert rack.calls[sync] == ["uv", "sync", "--locked", "--project", str(rack.root / "bridge")]
    assert rack.restarts == 1


def test_a_changed_lockfile_reinstalls_the_page_dependencies(rack, switches):
    updater = rack.updater(switches)
    os.utime(rack.root / "ui" / "node_modules" / ".package-lock.json", (1000, 1000))
    rack.land("ui/package-lock.json", '{"v": 2}\n', "A new page dependency")
    updater.apply()
    assert rack.ran("npm", "ci") and rack.ran("npm", "run", "build")


def test_a_failed_build_leaves_the_served_page_alone(rack, switches):
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    rack.build_exit = 2
    updater.apply()
    view = updater.view()
    assert "the page build failed, so the old page is still served" in view["error"] and "TS2304" in view["error"]
    assert (rack.root / "ui" / "dist" / "index.html").read_text() == "<html>old page</html>\n"
    assert view["behind"] == 0 and view["phase"] == "idle"  # the pull landed; the page is what is owed
    assert not (rack.root / "ui" / "node_modules" / ".cache" / "tldrack-build").exists()


@pytest.mark.parametrize("how", ["branch", "edit", "local commit"])
def test_a_checkout_in_use_is_never_pulled_over(rack, switches, how):
    if how == "branch":
        git(rack.root, "switch", "-q", "-c", "docs/x")
    elif how == "edit":
        write(rack.root / "bridge" / "tldrack" / "code.py", "VALUE = 'mine'\n")
    else:
        commit(rack.root, "notes.md", "mine\n", "A local commit")
    before = rack.head()
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    updater.apply()
    view = updater.view()
    assert rack.head() == before and view["behind"] == 1 and view["blocked"] and view["available"] is False
    assert not rack.ran("git", "merge") and not rack.ran("npm") and view["error"] is None
    if how == "edit":
        assert (rack.root / "bridge" / "tldrack" / "code.py").read_text() == "VALUE = 'mine'\n"


def test_a_second_request_joins_the_run_in_flight(rack, switches):
    updater = rack.updater(switches)
    assert updater._begin(update.BUILDING)
    status, view = updater.request("apply")
    assert status == 202 and view["phase"] == "building" and not rack.ran("git", "fetch")
    assert updater.busy_reasons() == ["updating: building the page"]


def test_a_request_takes_two_words_and_nothing_else(rack, switches):
    updater = rack.updater(switches)
    for action in ("pull", None, ["apply"], "origin/evil"):
        status, body = updater.request(action)
        assert status == 400 and "action" in body["error"]
    assert rack.calls == []


def test_an_install_with_no_checkout_cannot_update_and_says_so(switches):
    updater = Updater(None, switches=switches)
    view = updater.view()
    assert view["supported"] is False and "not running from a git checkout" in view["reason"]
    assert view["available"] is False and view["page"]["source"] == "unknown" and json.dumps(view)
    assert updater.request("apply")[0] == 409
    updater.tick()  # and the timer is a no-op


def test_this_checkout_is_found_from_the_package():
    root = update.checkout_root()
    assert root is None or (root / "bridge" / "tldrack" / "update.py").is_file()


# --- the timer ----------------------------------------------------------------


def test_a_rack_nobody_configured_asks_the_remote_nothing(rack, switches):
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    updater.tick()
    assert rack.calls == []  # no fetch, no git at all: the timer is off until a switch says otherwise
    view = updater.view()
    assert view["checks"] is False and view["auto"] is False and not rack.ran("git", "fetch")
    updater.apply()  # a press still works
    assert updater.view()["behind"] == 0


def test_the_update_check_only_looks(rack, switches):
    switches.save({"set": {UPDATE_CHECK: True}})
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    before = rack.head()
    updater.tick()
    view = updater.view()
    assert view["behind"] == 1 and view["checks"] is True and view["auto"] is False and rack.head() == before


def test_auto_update_brings_its_own_check(rack, switches):
    switches.save({"set": {AUTO_UPDATE: True}})  # one switch, not two
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    before = rack.head()
    updater.tick()
    view = updater.view()
    assert view["behind"] == 0 and view["checks"] is True and view["auto"] is True and rack.head() != before


def test_auto_never_acts_on_a_checkout_with_an_edit_in_it(rack, switches):
    switches.save({"set": {AUTO_UPDATE: True}})
    updater = rack.updater(switches)
    write(rack.root / "bridge" / "tldrack" / "code.py", "VALUE = 'mid-edit'\n")
    rack.stale = True  # the code on disk differs from the code running, and it starts
    updater.tick()
    assert rack.restarts == 0 and updater.view()["available"] is True
    updater.apply()  # the operator's own press is their call
    assert rack.restarts == 1


def test_auto_brings_a_hand_pulled_checkout_the_rest_of_the_way(rack, switches):
    switches.save({"set": {AUTO_UPDATE: True}})
    update.stamp_build(rack.root, rack.run)
    updater = rack.updater(switches)
    rack.land("ui/src/app.ts", "export const page = 2;\n", "A new page")
    git(rack.root, "pull", "-q", "--ff-only")  # the operator pulls, and stops there
    updater.tick()
    assert updater.view()["page"]["source"] == "current"
    assert "page = 2" in (rack.root / "ui" / "dist" / "index.html").read_text()


# --- the code watch's half ----------------------------------------------------


def test_the_code_watch_answers_an_update_even_while_switched_off(tree):  # noqa: F811
    ran: list[int] = []

    def preflight(spec, py_files):
        ran.append(len(py_files))
        return "ok", None

    monitor = monitor_for(tree, preflight=preflight, enabled=lambda: False)
    assert monitor.is_stale() is None and monitor.preflight_now() == (False, None, None) and ran == []
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    assert monitor.preflight_now() == (True, "ok", None) and len(ran) == 1


def test_the_code_watch_says_stale_without_a_busy_round(tree):  # noqa: F811
    monitor = monitor_for(tree)
    assert monitor.is_stale() is False
    write(tree["pkg"] / "main.py", "VALUE = 2\n")
    monitor.check()
    assert monitor.is_stale() is True and monitor.busy_now() == []


# --- the restart --------------------------------------------------------------


def test_only_a_bridge_that_owns_its_ttyd_restarts_in_place():
    full = ShutdownControl("full")
    assert full.trigger_restart() is True and full.restart and full.requested and full.event.is_set()
    beside = ShutdownControl("status-only")
    assert beside.trigger_restart() is False and not beside.restart and not beside.requested


def test_restart_in_place_re_executes_this_command_line_and_only_when_asked(monkeypatch):
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(main.os, "execv", lambda path, argv: calls.append((path, argv)))
    control = ShutdownControl("full")
    main.restart_in_place(control)
    assert calls == []  # an ordinary exit stays an exit
    control.trigger_restart()
    main.restart_in_place(control)
    assert calls == [(sys.executable, [sys.executable, *sys.orig_argv[1:]])]


# --- the routes ---------------------------------------------------------------


def handler_for(tmp_path: Path, updater: Updater | None):
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        updater=updater,
    )
    handler = object.__new__(cls)
    handler.recorded = Recorder()
    handler._send_json = lambda status, body: (
        setattr(handler.recorded, "status", status),
        setattr(handler.recorded, "body", body),
    )
    handler.headers = {"Origin": "http://127.0.0.1:7682"}
    handler.path = "/update"
    return handler


def test_the_update_routes_answer_the_view_and_start_a_run(rack, switches, tmp_path):
    updater = rack.updater(switches)
    handler = handler_for(tmp_path, updater)
    assert handler.GET_ROUTES["/update"] == "_handle_get_update" and handler.POST_ROUTES["/update"] == "_handle_post_update"
    handler._handle_get_update()
    assert handler.recorded.status == 200 and handler.recorded.body["branch"] == "main"
    assert updater._begin(update.BUILDING)  # a run in flight: the route answers and starts no thread
    _with_body(handler, {"action": "apply"})._handle_post_update()
    assert handler.recorded.status == 202 and handler.recorded.body["phase"] == "building"
    _with_body(handler, {"action": "rm -rf"})._handle_post_update()
    assert handler.recorded.status == 400


def test_an_update_needs_an_allowed_origin(rack, switches, tmp_path):
    handler = handler_for(tmp_path, rack.updater(switches))
    handler.headers = {"Origin": "https://hostile.example"}
    _with_body(handler, {"action": "apply"})._handle_post_update()
    assert handler.recorded.status == 403 and rack.calls == []


def test_a_handler_with_no_updater_answers_503(tmp_path):
    handler = handler_for(tmp_path, None)
    handler._handle_get_update()
    assert handler.recorded.status == 503
    _with_body(handler, {"action": "check"})._handle_post_update()
    assert handler.recorded.status == 503
