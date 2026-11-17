"""`tldrack up`: make sure the bridge is up and its routes answer, from any shell.

The one command an operator runs to start the rack, and the one an agent or a script runs before
it needs the bridge. It never stops a running bridge (a restart is the operator's call:
`tldrack kill`, then `tldrack up`); it only fills the gap when nothing is there:

1. `/snapshot` answers → go to 4.
2. Something holds the status port but does not answer → wait (a bridge mid-start is exactly
   this for a few seconds); past the deadline, name the pid and the fix.
3. Nothing listens → start it. With a supervisor configured (settings.py `supervisor_url`: an app
   that owns the bridge, restarts it and keeps its log) that app is asked first, and gets
   `SUPERVISOR_GRACE_SECONDS` to put a listener on the port. Otherwise, or when it does not act,
   the bridge is started here, in tmux (`MachineIO.spawn`).
4. Wait until every `--wait-for` route answers 200. A plugin the snapshot says is not mounted
   fails at once with the fix; a route that is merely slow is waited on.

WHY IN TMUX. The bridge must outlive the shell that ran `up`, and its output must stay readable.
tmux is already required, so the bridge gets a session of its own. That session lives on its OWN
tmux socket (`bridge_socket`), never the default one: the rack lists the default server's
sessions as its rows, and the bridge is not a row an operator should see, rename or kill there.

WHY IT SAYS WHICH. A bridge can be down, starting, wedged or just slow, and each needs a different
answer. A caller that reads all four as "not answering" restarts a healthy bridge, or gives up on
one that needed ten more seconds. This walk names the case and prints the fix.

Exit 0 when everything answered, 1 otherwise (the last line says why and the fix).
"""

from __future__ import annotations

import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from collections.abc import Callable

#: How long a supervisor gets to put a listener on the status port before we start one here.
SUPERVISOR_GRACE_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 90.0
#: The routes `up` waits on by default: none. `/snapshot` answering is "up"; a caller that needs
#: a plugin's route names it (`--wait-for /plugin/worktree/state`).
DEFAULT_WAIT_FOR: tuple[str, ...] = ()
#: One request's budget while waiting on a route — above the slowest healthy answer.
ROUTE_REQUEST_TIMEOUT_SECONDS = 20.0
POLL_SECONDS = 0.5
#: The session the bridge runs in, on its own socket (module docstring § WHY IN TMUX).
BRIDGE_SESSION = "bridge"

#: The variables a bridge inherits from us — everything else is shed. A bridge started from an
#: agent's shell would otherwise carry `TMUX` (main.py `PANE_IDENTITY_ENV` owns why — see the note
#: there), the agent's `CLAUDE_CONFIG_DIR` / API keys (`profiles.py` § 4), and a `VIRTUAL_ENV` from
#: whatever venv the agent ran in.
KEEP_ENV = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TERM",
    "SSH_AUTH_SOCK",
    "__CF_USER_TEXT_ENCODING",
    "XDG_RUNTIME_DIR",
    "TMUX_TMPDIR",
)
#: The bridge's own configuration rides through (ports, directories, plugin dirs, allowed hosts).
KEEP_ENV_PREFIXES = ("TLDRACK_", "VOIDCRAFT_BRIDGE_")
#: Where `uv`, `tmux`, `ttyd`, `gh` and the agent CLIs usually are, added when the caller's PATH
#: lacks them (a login item or a cron line starts with a bare PATH).
PATH_HEAD = ("/opt/homebrew/bin", "/usr/local/bin", "{home}/.local/bin")
PATH_TAIL = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def clean_env(environ: dict[str, str], home: str) -> dict[str, str]:
    """The environment a bridge started by `up` gets. Pure.

    PATH is the caller's, so a CLI installed somewhere unusual is still found, minus the bin
    directory of the caller's virtualenv (the bridge is not that project), plus the usual places.
    """
    env = {key: value for key, value in environ.items() if key in KEEP_ENV or key.startswith(KEEP_ENV_PREFIXES)}
    env.setdefault("HOME", home)
    venv = environ.get("VIRTUAL_ENV")
    shed = {os.path.join(venv, "bin")} if venv else set()
    entries: list[str] = []
    usual = [part.format(home=home) for part in PATH_HEAD] + list(PATH_TAIL)
    for part in [*environ.get("PATH", "").split(os.pathsep), *usual]:
        if part and part not in shed and part not in entries:
            entries.append(part)
    env["PATH"] = os.pathsep.join(entries)
    return env


def plugin_of(route: str) -> str | None:
    """`/plugin/worktree/state` → `worktree`; any other route → None. Pure."""
    parts = route.strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "plugin" and parts[1] else None


def unmounted_plugins(snapshot: dict, routes: tuple[str, ...]) -> list[str]:
    """Plugins the routes need that the snapshot says are NOT mounted. Pure.

    An absent `plugins` key is a bridge that mounts none or does not advertise them: unknown,
    so nothing is reported and the route's own answer decides.
    """
    mounted = snapshot.get("plugins")
    if not isinstance(mounted, dict):
        return []
    needed = [name for name in (plugin_of(route) for route in routes) if name]
    return sorted({name for name in needed if name not in mounted})


def is_bridge_command(command: str) -> bool:
    """Does a `ps -o command=` line look like this bridge? Pure."""
    return "tldrack" in command


def bridge_socket(status_port: int) -> str:
    """The tmux socket name (`tmux -L`) of the bridge on `status_port`: one per port, so a dev
    bridge beside a live one has its own."""
    return f"tldrack-bridge-{status_port}"


def bridge_log_name(status_port: int) -> str:
    """The log file's name in the state directory, one per port for the same reason."""
    return f"bridge-{status_port}.log"


def bridge_argv(python: str, port: int, status_port: int, start_dir: str) -> list[str]:
    """The foreground bridge, run by the interpreter that is running `up`: the same install, so
    there is no second copy of the package to find. Pure."""
    return [python, "-m", "tldrack.main", "--port", str(port), "--status-port", str(status_port), "--start-dir", start_dir]


def tmux_spawn_argv(socket: str, start_dir: str, command: list[str], log_path: str) -> list[str]:
    """`tmux new-session` for the bridge, its output copied to the log. Pure."""
    shell = f"{shlex.join(command)} 2>&1 | tee -a {shlex.quote(log_path)}"
    return ["tmux", "-L", socket, "new-session", "-d", "-s", BRIDGE_SESSION, "-c", start_dir, shell]


def launched_line(snapshot: dict) -> str:
    """`pid 39224 · launched by tmux` — for the success line. Pure."""
    bridge = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), dict) else {}
    parts = []
    if bridge.get("pid") is not None:
        parts.append(f"pid {bridge['pid']}")
    launched = bridge.get("launched_by") if isinstance(bridge.get("launched_by"), dict) else {}
    who = launched.get("label") or launched.get("kind")
    if who:
        parts.append(f"launched by {who}")
    return " · ".join(parts)


# -- the page's build ----------------------------------------------------------


def ui_build_commands(ui_dir: Path, npm: str | None) -> tuple[list[list[str]], str | None]:
    """`(commands, why_not)` to build `ui/dist` in a checkout. Pure apart from reading
    `package.json`. No commands and a reason when it cannot be built here: the bridge still
    starts, and `/` says the page is not built."""
    manifest = ui_dir / "package.json"
    try:
        scripts = json.loads(manifest.read_text()).get("scripts")
    except FileNotFoundError:
        return [], f"no checkout beside this install ({ui_dir} has no package.json)"
    except (OSError, ValueError, AttributeError):
        return [], f"cannot read {manifest}"
    if not isinstance(scripts, dict) or "build" not in scripts:
        return [], f"{manifest} has no build script"
    if npm is None:
        return [], "npm not found: install Node.js, then run tldrack up again"
    install = (
        [] if (ui_dir / "node_modules").is_dir() else [[npm, "ci" if (ui_dir / "package-lock.json").is_file() else "install"]]
    )
    return [*install, [npm, "run", "build"]], None


# -- the I/O seam --------------------------------------------------------------


class UpIO(Protocol):
    def get(self, path: str, timeout: float) -> tuple[int, bytes] | None: ...
    def listener(self) -> tuple[int, str] | None: ...
    def ask_supervisor(self) -> bool: ...
    def spawn(self) -> str | None: ...
    def spawn_exited(self) -> bool: ...
    def log_tail(self, lines: int) -> str: ...
    def clock(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass
class MachineIO:
    """The real machine: loopback HTTP, `lsof`, the supervisor's URL, and a tmux session."""

    port: int
    status_port: int
    start_dir: str
    log_path: Path
    supervisor_url: str | None = None

    def get(self, path: str, timeout: float) -> tuple[int, bytes] | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.status_port}{path}", timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (OSError, ValueError):
            return None

    def listener(self) -> tuple[int, str] | None:
        try:
            found = subprocess.run(
                ["lsof", "-t", "-nP", f"-iTCP:{self.status_port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            pid = int(found.stdout.split()[0])
        except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
            return None
        try:
            command = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            command = ""
        return pid, command

    def ask_supervisor(self) -> bool:
        """Hand the start to the configured supervisor. False with none configured, or when the
        URL could not be opened: the caller then starts the bridge itself."""
        if self.supervisor_url is None:
            return False
        opener = ["open", "-g"] if sys.platform == "darwin" else ["xdg-open"]
        if shutil.which(opener[0]) is None:
            return False
        try:
            return subprocess.run([*opener, self.supervisor_url], capture_output=True, timeout=10, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def spawn(self) -> str | None:
        home = str(Path.home())
        env = clean_env(dict(os.environ), home)
        for tool, fix in (
            ("tmux", "brew install tmux, or your package manager's tmux"),
            ("ttyd", "brew install ttyd, or https://github.com/tsl0922/ttyd"),
        ):
            if shutil.which(tool, path=env["PATH"]) is None:
                return f"{tool} not found — install it: {fix}"
        socket = bridge_socket(self.status_port)
        try:
            # A session already there is a bridge that has not bound its port yet (or one that
            # died with its pane held open): never a second one. The caller waits on the port.
            if (
                subprocess.run(
                    ["tmux", "-L", socket, "has-session", "-t", f"={BRIDGE_SESSION}"],
                    capture_output=True,
                    env=env,
                    timeout=10,
                    check=False,
                ).returncode
                == 0
            ):
                return None
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as log:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime())
                log.write(f"\n--- spawn {stamp} (tldrack up) ---\n".encode())
            command = bridge_argv(sys.executable, self.port, self.status_port, self.start_dir)
            started = subprocess.run(
                tmux_spawn_argv(socket, self.start_dir, command, str(self.log_path)),
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not run tmux ({type(exc).__name__}: {exc})"
        if started.returncode != 0:
            return f"tmux refused to start the bridge: {started.stderr.strip() or started.returncode}"
        return None

    def spawn_exited(self) -> bool:
        """The session `spawn` made is gone and nothing took the port: the bridge started and died."""
        try:
            alive = (
                subprocess.run(
                    ["tmux", "-L", bridge_socket(self.status_port), "has-session", "-t", f"={BRIDGE_SESSION}"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return not alive and self.listener() is None

    def log_tail(self, lines: int) -> str:
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 8192))
                text = handle.read().decode(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def clock(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# -- the walk ------------------------------------------------------------------


class Progress:
    """A spinner on a terminal; one line every few seconds anywhere else (an agent's
    captured output), so a long wait is never silent and never a wall of frames."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, write: Callable[[str], None], tty: bool) -> None:
        self._write = write
        self._tty = tty
        self._frames = itertools.cycle(self.FRAMES)
        self._last_line = -1e9
        self._open = False

    def tick(self, label: str, elapsed: float) -> None:
        if self._tty:
            self._write(f"\r{next(self._frames)} {label} {elapsed:.0f}s\033[K")
            self._open = True
        elif elapsed - self._last_line >= 5:
            self._write(f"  … {label} ({elapsed:.0f}s)\n")
            self._last_line = elapsed

    def line(self, text: str) -> None:
        if self._open:
            self._write("\r\033[K")
            self._open = False
        self._write(text + "\n")


@dataclass(frozen=True)
class UpResult:
    code: int
    #: True when this run started the bridge (here or through the supervisor), not found it up.
    started: bool = False


def run_up(
    io: UpIO,
    progress: Progress,
    *,
    wait_for: tuple[str, ...] = DEFAULT_WAIT_FOR,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    start: bool = True,
    status_port: int = 7682,
    log_path: str = "",
    attach: str = "",
) -> UpResult:
    started = io.clock()
    deadline = started + timeout
    started_now = False
    restart = "tldrack kill && tldrack up"

    def fail(text: str) -> UpResult:
        progress.line(f"tldrack up: FAILED — {text}")
        return UpResult(1, started_now)

    def snapshot(request_timeout: float) -> dict | None:
        answer = io.get("/snapshot", request_timeout)
        if answer is None or answer[0] != 200:
            return None
        try:
            body = json.loads(answer[1])
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def wait_snapshot(label: str, until: float, *, spawned: bool = False) -> dict | None:
        while io.clock() < until:
            found = snapshot(min(5.0, max(0.5, until - io.clock())))
            if found is not None:
                return found
            # The bridge we started has already exited (a port taken, a bad setting): its log
            # says why now, and waiting out the whole timeout would only hide that.
            if spawned and io.spawn_exited():
                return None
            progress.tick(label, io.clock() - started)
            io.sleep(POLL_SECONDS)
        return None

    snap = snapshot(3.0)
    if snap is None:
        holder = io.listener()
        if holder is not None:
            pid, command = holder
            who = "the bridge" if is_bridge_command(command) else (command or "an unknown process")
            progress.line(f"tldrack up: :{status_port} is held by {who} (pid {pid}) but not answering yet — waiting")
            snap = wait_snapshot(f"waiting for :{status_port}/snapshot", deadline)
            if snap is None:
                if not is_bridge_command(command):
                    return fail(
                        f":{status_port} is held by {command or '?'} (pid {pid}), not the bridge. "
                        f"Quit it (kill {pid}), then run: tldrack up"
                    )
                return fail(
                    f"the bridge (pid {pid}) holds :{status_port} but has not answered in "
                    f"{timeout:.0f}s — it is wedged. Restart it: {restart}"
                )
        else:
            if not start:
                return fail("the bridge is down. Start it: tldrack up")
            started_now = True
            taken = False
            if io.ask_supervisor():
                progress.line("tldrack up: the bridge is down — asked the supervisor to start it")
                taken = _wait_listener(io, progress, min(deadline, io.clock() + SUPERVISOR_GRACE_SECONDS), started)
                if not taken:
                    progress.line("tldrack up: the supervisor did not start it")
            if not taken:
                progress.line(f"tldrack up: the bridge is down — starting it in tmux (log: {log_path})")
                error = io.spawn()
                if error:
                    return fail(f"could not start the bridge: {error}")
            snap = wait_snapshot("starting the bridge", deadline, spawned=not taken)
            if snap is None:
                tail = io.log_tail(4)
                what = (
                    "the bridge exited as it started."
                    if not taken and io.spawn_exited()
                    else f"the bridge did not answer within {timeout:.0f}s."
                )
                return fail(what + (f" Last log lines:\n{tail}\n" if tail else " ") + f"Full log: {log_path}")

    missing = unmounted_plugins(snap, wait_for)
    if missing:
        names = ", ".join(missing)
        return fail(
            f"the bridge is up but does not mount {names}. Add the plugin dir to "
            f"plugins.json in the config directory, then restart it: {restart}"
        )

    for route in wait_for:
        waited_from = io.clock()
        while True:
            remaining = deadline - io.clock()
            if remaining <= 0:
                return fail(
                    f"{route} did not answer within {timeout:.0f}s. The bridge is up — wait "
                    f"a minute and re-run, or raise --timeout"
                )
            answer = io.get(route, min(ROUTE_REQUEST_TIMEOUT_SECONDS, max(0.5, remaining)))
            if answer is not None and answer[0] == 200:
                break
            if answer is not None and answer[0] == 404:
                return fail(f"{route} answered 404 — this bridge predates the route. Restart it: {restart}")
            progress.tick(f"waiting for {route}", io.clock() - started)
            io.sleep(POLL_SECONDS)
        took = io.clock() - waited_from
        progress.line(f"tldrack up: {route} answered" + (f" ({took:.1f}s)" if took >= 1 else ""))

    who = launched_line(snap)
    progress.line("tldrack up: bridge up" + (f" · {who}" if who else ""))
    if started_now and attach:
        progress.line(f"  its output: {attach}")
    return UpResult(0, started_now)


def _wait_listener(io: UpIO, progress: Progress, until: float, started: float) -> bool:
    while io.clock() < until:
        if io.listener() is not None:
            return True
        progress.tick("waiting for the supervisor to start the bridge", io.clock() - started)
        io.sleep(POLL_SECONDS)
    return False
