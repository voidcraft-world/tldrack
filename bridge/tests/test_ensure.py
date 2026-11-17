"""`tldrack up` (`tldrack/ensure.py`): which of down / starting / wedged /
slow the bridge is, what it does about each, and the fix it prints. Nothing here touches
the network or spawns: every effect goes through a fake `UpIO`."""

import json

from tldrack import ensure


class FakeIO:
    """A scripted machine. `routes` maps a path to a list of answers consumed in order
    (the last one repeats); `listeners` likewise for the status-port holder."""

    def __init__(
        self,
        routes=None,
        listeners=None,
        supervisor=False,
        app_takes=False,
        spawn_error=None,
        on_spawn=None,
        request_cost=0.0,
        exits=False,
    ):
        self.routes = {path: list(answers) for path, answers in (routes or {}).items()}
        self.listeners = list(listeners or [None])
        self.supervisor = supervisor or app_takes
        self.app_takes = app_takes
        self.spawn_error = spawn_error
        self.on_spawn = on_spawn
        self.request_cost = request_cost
        self.exits = exits
        self.now = 0.0
        self.asked_app = 0
        self.spawned = 0
        self.gets: list[str] = []

    def _next(self, queue):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def get(self, path, timeout):
        self.gets.append(path)
        self.now += self.request_cost
        return self._next(self.routes.get(path, [None]))

    def listener(self):
        return self._next(self.listeners)

    def ask_supervisor(self):
        if not self.supervisor:
            return False
        self.asked_app += 1
        if self.app_takes:
            self.listeners = [(4242, "/x/bridge/.venv/bin/tldrack")]
            self.routes["/snapshot"] = [ok({"bridge": {"pid": 4242}})]
        return True

    def spawn(self):
        self.spawned += 1
        if self.on_spawn:
            self.on_spawn(self)
        return self.spawn_error

    def spawn_exited(self):
        return self.exits and self.spawned > 0

    def log_tail(self, lines):
        return "Traceback: boom"

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def ok(body):
    return 200, json.dumps(body).encode()


SNAP = ok({"plugins": {"worktree": {"routes": []}}, "bridge": {"pid": 7, "launched_by": {"kind": "tmux", "label": "tmux"}}})
WORKTREE = ("/plugin/worktree/state",)
STATE = (200, b'{"worktrees": []}')


def run(io, **kw):
    lines: list[str] = []
    progress = ensure.Progress(lines.append, tty=False)
    kw.setdefault("wait_for", WORKTREE)
    result = ensure.run_up(io, progress, timeout=kw.pop("timeout", 30.0), log_path="/log", **kw)
    return result.code, "".join(lines)


class TestUpAlready:
    def test_answers_and_waits_for_the_worktree_route(self):
        io = FakeIO(routes={"/snapshot": [SNAP], "/plugin/worktree/state": [STATE]})
        code, out = run(io)
        assert code == 0
        assert "bridge up · pid 7 · launched by tmux" in out
        assert io.asked_app == 0 and io.spawned == 0

    def test_a_slow_route_is_waited_on_not_failed(self):
        io = FakeIO(routes={"/snapshot": [SNAP], "/plugin/worktree/state": [None, None, (500, b""), STATE]}, request_cost=2.0)
        code, out = run(io)
        assert code == 0 and "/plugin/worktree/state answered" in out

    def test_a_route_that_never_answers_names_itself_and_says_wait(self):
        io = FakeIO(routes={"/snapshot": [SNAP], "/plugin/worktree/state": [None]}, request_cost=5.0)
        code, out = run(io, timeout=20.0)
        assert code == 1
        assert "/plugin/worktree/state did not answer within 20s" in out
        assert "The bridge is up" in out

    def test_an_unmounted_plugin_fails_at_once_with_the_fix(self):
        io = FakeIO(routes={"/snapshot": [ok({"plugins": {"other": {}}})]})
        code, out = run(io)
        assert code == 1 and "does not mount worktree" in out and "plugins.json" in out
        assert "/plugin/worktree/state" not in io.gets

    def test_a_404_route_means_an_old_bridge(self):
        io = FakeIO(routes={"/snapshot": [ok({})], "/plugin/worktree/state": [(404, b"")]})
        code, out = run(io)
        assert code == 1 and "predates the route" in out and "tldrack kill && tldrack up" in out

    def test_custom_routes_replace_the_default(self):
        io = FakeIO(routes={"/snapshot": [SNAP], "/status": [(200, b"{}")]})
        code, _out = run(io, wait_for=("/status",))
        assert code == 0 and "/plugin/worktree/state" not in io.gets

    def test_by_default_the_snapshot_answering_is_up(self):
        # No plugin is required: a bridge that mounts none is a whole bridge.
        io = FakeIO(routes={"/snapshot": [ok({"bridge": {"pid": 7}})]})
        lines: list[str] = []
        result = ensure.run_up(io, ensure.Progress(lines.append, tty=False), timeout=5.0)
        assert result == ensure.UpResult(0, started=False) and io.gets == ["/snapshot"]


class TestDown:
    def test_a_supervisor_is_asked_first_and_owns_the_bridge(self):
        io = FakeIO(routes={"/snapshot": [None], "/plugin/worktree/state": [STATE]}, app_takes=True)
        code, out = run(io)
        assert code == 0 and io.asked_app == 1 and io.spawned == 0
        assert "asked the supervisor" in out

    def test_starts_it_in_tmux_when_the_supervisor_does_not(self):
        def comes_up(io):
            io.routes["/snapshot"] = [None, ok({"bridge": {"pid": 9}})]

        io = FakeIO(routes={"/snapshot": [None], "/plugin/worktree/state": [STATE]}, supervisor=True, on_spawn=comes_up)
        code, out = run(io)
        assert code == 0 and io.asked_app == 1 and io.spawned == 1
        assert "the supervisor did not start it" in out and "starting it in tmux (log: /log)" in out

    def test_with_no_supervisor_it_starts_in_tmux_at_once(self):
        def comes_up(io):
            io.routes["/snapshot"] = [None, ok({"bridge": {"pid": 9}})]

        io = FakeIO(routes={"/snapshot": [None]}, on_spawn=comes_up)
        lines: list[str] = []
        result = ensure.run_up(
            io, ensure.Progress(lines.append, tty=False), timeout=30.0, log_path="/log", attach="tmux -L sock attach"
        )
        assert result == ensure.UpResult(0, started=True) and io.spawned == 1 and io.asked_app == 0
        out = "".join(lines)
        assert "supervisor" not in out and "its output: tmux -L sock attach" in out

    def test_no_start_only_reports(self):
        io = FakeIO(routes={"/snapshot": [None]})
        code, out = run(io, start=False)
        assert code == 1 and io.asked_app == 0 and io.spawned == 0
        assert "Start it: tldrack up" in out

    def test_a_spawn_error_is_the_reason(self):
        io = FakeIO(routes={"/snapshot": [None]}, spawn_error="tmux not found — install it")
        code, out = run(io)
        assert code == 1 and "could not start the bridge: tmux not found" in out

    def test_a_bridge_that_exits_as_it_starts_fails_at_once_with_its_log(self):
        io = FakeIO(routes={"/snapshot": [None]}, exits=True)
        code, out = run(io, timeout=90.0)
        assert code == 1 and "the bridge exited as it started" in out and "Traceback: boom" in out
        assert io.now < 5, "it did not wait out the timeout"

    def test_a_bridge_that_never_answers_shows_the_log_tail(self):
        io = FakeIO(routes={"/snapshot": [None]})
        code, out = run(io, timeout=15.0)
        assert code == 1 and "did not answer within 15s" in out
        assert "Traceback: boom" in out and "Full log: /log" in out


class TestHeldButSilent:
    BRIDGE = (31, "/u/code/project/.venv/bin/python3 -m tldrack.main --port 7681")

    def test_a_starting_bridge_is_waited_on(self):
        io = FakeIO(routes={"/snapshot": [None, None, SNAP], "/plugin/worktree/state": [STATE]}, listeners=[self.BRIDGE])
        code, out = run(io)
        assert code == 0 and "held by the bridge (pid 31)" in out
        assert io.spawned == 0 and io.asked_app == 0  # never a second bridge

    def test_a_wedged_bridge_names_the_restart_never_kills(self):
        io = FakeIO(routes={"/snapshot": [None]}, listeners=[self.BRIDGE])
        code, out = run(io, timeout=10.0)
        assert code == 1 and "wedged" in out and "tldrack kill && tldrack up" in out
        assert io.spawned == 0

    def test_a_squatter_is_named_and_left_alone(self):
        io = FakeIO(routes={"/snapshot": [None]}, listeners=[(55, "/usr/bin/python3 -m http.server 7682")])
        code, out = run(io, timeout=5.0)
        assert code == 1 and "not the bridge" in out and "kill 55" in out


class TestPure:
    def test_clean_env_sheds_tmux_agent_and_venv_state(self):
        env = ensure.clean_env(
            {
                "HOME": "/h",
                "USER": "u",
                "TMUX": "/tmp/tmux-501/default,1,0",
                "TMUX_PANE": "%3",
                "CLAUDE_CONFIG_DIR": "/h/.claude-a",
                "ANTHROPIC_API_KEY": "sk",
                "VIRTUAL_ENV": "/v",
                "SSH_AUTH_SOCK": "/s",
                "TLDRACK_PLUGINS": "/p/worktree",
                "PATH": "/v/bin:/x",
            },
            "/h",
        )
        # The caller's PATH, minus its virtualenv, plus the usual places.
        assert env["PATH"] == "/x:/opt/homebrew/bin:/usr/local/bin:/h/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        assert env["SSH_AUTH_SOCK"] == "/s" and env["TLDRACK_PLUGINS"] == "/p/worktree"
        for gone in ("TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "VIRTUAL_ENV"):
            assert gone not in env

    def test_plugin_of(self):
        assert ensure.plugin_of("/plugin/worktree/state") == "worktree"
        assert ensure.plugin_of("/snapshot") is None
        assert ensure.plugin_of("/plugin/") is None

    def test_an_older_bridge_without_a_plugins_key_is_unknown_not_missing(self):
        assert ensure.unmounted_plugins({}, ("/plugin/worktree/state",)) == []

    def test_is_bridge_command(self):
        assert ensure.is_bridge_command("/x/.venv/bin/python3 /x/.venv/bin/tldrack")
        assert ensure.is_bridge_command("python -m tldrack.main")
        assert not ensure.is_bridge_command("ttyd -p 7681")

    def test_a_bridge_gets_its_own_socket_and_log_per_port(self):
        assert ensure.bridge_socket(7682) != ensure.bridge_socket(7791)
        assert ensure.bridge_log_name(7682) != ensure.bridge_log_name(7791)

    def test_the_bridge_runs_in_a_session_off_the_default_tmux_server(self):
        command = ensure.bridge_argv("/v/bin/python", 7781, 7791, "/my project")
        assert command == [
            "/v/bin/python",
            "-m",
            "tldrack.main",
            "--port",
            "7781",
            "--status-port",
            "7791",
            "--start-dir",
            "/my project",
        ]
        argv = ensure.tmux_spawn_argv("sock", "/my project", command, "/s/bridge.log")
        assert argv[:8] == ["tmux", "-L", "sock", "new-session", "-d", "-s", ensure.BRIDGE_SESSION, "-c"]
        assert argv[8] == "/my project"
        assert (
            argv[9] == "/v/bin/python -m tldrack.main --port 7781 --status-port 7791 --start-dir "
            "'/my project' 2>&1 | tee -a /s/bridge.log"
        )


class TestUiBuild:
    def test_no_checkout_is_a_reason_not_an_error(self, tmp_path):
        commands, why = ensure.ui_build_commands(tmp_path / "ui", "/bin/npm")
        assert commands == [] and "no checkout" in why

    def test_a_manifest_without_a_build_script_is_not_built(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}))
        commands, why = ensure.ui_build_commands(tmp_path, "/bin/npm")
        assert commands == [] and "no build script" in why

    def test_npm_missing_names_the_fix(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
        commands, why = ensure.ui_build_commands(tmp_path, None)
        assert commands == [] and "npm not found" in why

    def test_installs_once_then_builds(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
        (tmp_path / "package-lock.json").write_text("{}")
        assert ensure.ui_build_commands(tmp_path, "/bin/npm") == ([["/bin/npm", "ci"], ["/bin/npm", "run", "build"]], None)
        (tmp_path / "node_modules").mkdir()
        assert ensure.ui_build_commands(tmp_path, "/bin/npm") == ([["/bin/npm", "run", "build"]], None)


class TestProgress:
    def test_a_terminal_gets_a_spinner_and_a_clean_line_after(self):
        out: list[str] = []
        progress = ensure.Progress(out.append, tty=True)
        progress.tick("waiting", 1.0)
        progress.line("done")
        assert out[0].startswith("\r") and out[-1] == "done\n" and "\r\033[K" in out

    def test_captured_output_gets_a_line_every_five_seconds(self):
        out: list[str] = []
        progress = ensure.Progress(out.append, tty=False)
        for second in range(12):
            progress.tick("waiting", float(second))
        assert len(out) == 3
