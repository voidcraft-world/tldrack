"""Is this checkout behind its remote, and can the bridge bring itself up to date?

tldrack installs from a clone and runs that working tree. So "updating" is three separate things,
and an operator who does one of them by hand still sees the old rack:

1. the checkout moves (`git merge --ff-only` to the remote's main branch);
2. the page is rebuilt from it (`ui/dist`, served per request: a reload shows it);
3. the bridge runs the new Python (a restart: the process imported its code once).

This module watches all three, serves what it sees as `GET /update` (the ⚙ button's dot and the
Settings page's UPDATE card), and on `POST /update {"action": "apply"}` does whichever are due, in
that order. With the `auto_update` switch on, the timed check does the same with no click.

── What it will not do ────────────────────────────────────────────────────
It only ever FAST-FORWARDS the remote's default branch, and only when the checkout is on that
branch, has no uncommitted change to a tracked file, and has no commit the remote lacks. Anything
else is `blocked` with the reason in words: a checkout is where the operator and their agents
work, and an updater that switches a branch or merges over an edit destroys that work. It names
no remote, branch, path or command from a request: `origin` and its default branch are read from
the checkout, and the request carries one of two fixed words.

── Why the restart is in place ────────────────────────────────────────────
A bridge may run under a supervisor app, in tmux (`tldrack up`), or in the operator's own
terminal. Only the first has anything that would start it again. So the bridge re-executes
itself (`main.py` `restart_in_place`): the normal exit path runs first (ttyd stopped, plugins
closed), then the same pid becomes the new code. Whoever started it sees a process that never
exited. tmux sessions and their agents are not children of the bridge and keep running.

── Why a restart is checked first ─────────────────────────────────────────
A restart into code that does not import is a bridge that does not come back, with nobody at a
terminal. `freshness.py`'s preflight imports the tree in a child process first; a failure leaves
the old process running and says why. A plugin with a job in flight (`busy_reasons()`) holds the
restart: `waiting` says which, and the next tick tries again.

── The page's stamp ───────────────────────────────────────────────────────
A build made here writes `tldrack-build.json` into the build: the git tree ids of `ui/` and
`schemas/` it was built from. Tree ids, because "is the page current" is "same source bytes",
which a commit time or an mtime cannot say after a pull. A build with no stamp (`npm run build`
by hand, another tool) is `unknown`, never stale: unknown asks for nothing. `page.id` is a hash
of the served HTML, which names its hashed assets, so it changes with any build by anyone: a page
compares it with the one it loaded to know a reload would show something newer.

── Auto ───────────────────────────────────────────────────────────────────
Off by default (`features.py` owns why a part that acts with no click starts off). It acts only
on a checkout with no uncommitted tracked change at all, so an operator editing tldrack in place
is never restarted under their hands. A run that failed is not retried until the checkout or the
remote moves: a broken build must not loop every five minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from tldrack.features import AUTO_UPDATE, UPDATE_CHECK, FeatureSwitches

#: The timed check. The first one waits for the bridge to finish starting.
CHECK_SECONDS = 300.0
FIRST_CHECK_SECONDS = 20.0
#: How soon an auto-update held by a busy plugin is tried again.
RETRY_SECONDS = 30.0
GIT_TIMEOUT_SECONDS = 20.0
FETCH_TIMEOUT_SECONDS = 45.0
BUILD_TIMEOUT_SECONDS = 600.0
SYNC_TIMEOUT_SECONDS = 300.0
INCOMING_CAP = 8
SUBJECT_CHARS = 120
ERROR_CHARS = 400
REMOTE = "origin"
STAMP_FILE = "tldrack-build.json"
#: Where a build is staged: ignored by git, and on the same volume as `ui/dist` so the swap is a rename.
STAGE_PARTS = ("node_modules", ".cache", "tldrack-build")

IDLE, CHECKING, PULLING, BUILDING, SYNCING, VERIFYING, RESTARTING = (
    "idle",
    "checking",
    "pulling",
    "building",
    "syncing",
    "verifying",
    "restarting",
)
#: The phases a supervisor must not restart the bridge under (`busy_reasons`), in its words.
WORKING = {
    PULLING: "pulling new commits",
    BUILDING: "building the page",
    SYNCING: "installing bridge dependencies",
    VERIFYING: "checking that the new code starts",
}
ACTIONS = ("check", "apply")
CURRENT, STALE, UNKNOWN = "current", "stale", "unknown"

RESTART_BY_HAND = "the new code is on disk; restart the bridge to run it: `tldrack kill`, then `tldrack up`"
RESTART_WAITS = "the restart waits for:"
#: How this module's own entry in the busy list starts.
OWN_BUSY = "updating:"
#: When the remote was last asked, handed across a restart in place (exec keeps the environment).
#: Without it the process an update just started would say "never checked" about the very check
#: that update began with. Read once and removed, so no child process inherits it.
CHECKED_ENV = "TLDRACK_UPDATE_CHECKED_AT"


def inherited_check_time(environ: MutableMapping[str, str]) -> float | None:
    """Take `CHECKED_ENV` out of the environment: a past unix time, or None."""
    raw = environ.pop(CHECKED_ENV, None)
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and 0 < value <= time.time() else None


#: `(argv, cwd, timeout seconds) -> (exit code, output)`: the one seam every command goes through.
Run = Callable[[list[str], Path, float], tuple[int, str]]


class UpdateFailed(Exception):
    """A step that stopped the run. Its text is the view's `error`."""


def run_command(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run to completion with no terminal: a git that wants a password fails instead of waiting on
    a prompt nobody can see. And with no optional locks: a timed `git status` must never hold the
    index lock at the moment the operator's own git command wants it."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g} s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout or ""


def checkout_root() -> Path | None:
    """The git checkout this bridge is running from, or None for an install that has none (a
    wheel): there is nothing to pull and nothing to build."""
    package = Path(__file__).resolve().parent
    root = package.parent.parent
    return root if (root / ".git").exists() and (root / "bridge" / "tldrack") == package else None


def tail(output: str) -> str:
    """The end of a command's output as one line: where a failing tool says why."""
    return " ".join(output.split())[-ERROR_CHARS:]


@dataclass(frozen=True)
class GitState:
    """The checkout against the remote's default branch, as of the last look."""

    branch: str | None = None  # None on a detached commit
    upstream: str | None = None  # `origin/main`; None when the checkout has no such remote branch
    head: str | None = None
    target: str | None = None
    behind: int = 0
    ahead: int = 0
    dirty: int = 0  # tracked files with an uncommitted change
    incoming: tuple[tuple[str, str], ...] = ()
    #: `(tree of ui/, tree of schemas/)` at HEAD, or None while either has an uncommitted change.
    trees: tuple[str, str] | None = None


def why_blocked(state: GitState) -> str | None:
    """Why a pull cannot run, or None. Pure. Asked only when there is something to pull."""
    if state.behind <= 0:
        return None
    if state.upstream is None:
        return f"the checkout has no `{REMOTE}` remote branch to follow"
    main = state.upstream.split("/", 1)[-1]
    if state.branch is None:
        return f"the checkout is on a detached commit, not `{main}`"
    if state.branch != main:
        return f"the checkout is on branch `{state.branch}`, not `{main}`"
    if state.dirty:
        return f"{state.dirty} uncommitted change{'s' if state.dirty != 1 else ''} in the checkout"
    if state.ahead:
        return f"{state.ahead} local commit{'s' if state.ahead != 1 else ''} not on `{state.upstream}`"
    return None


def read_git(root: Path, run: Run) -> GitState:
    """What git says about the checkout right now. Reads only; a command that fails leaves its
    field at the default, so a half-readable checkout reads as nothing to do, never as an update."""

    def git(*args: str) -> str | None:
        code, output = run(["git", *args], root, GIT_TIMEOUT_SECONDS)
        # Trailing space only: a porcelain status line may START with a space, and its columns count.
        return output.rstrip() if code == 0 else None

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    upstream = git("symbolic-ref", "--quiet", "--short", f"refs/remotes/{REMOTE}/HEAD")
    if upstream is None and git("rev-parse", "--verify", "--quiet", f"refs/remotes/{REMOTE}/main") is not None:
        upstream = f"{REMOTE}/main"
    behind = ahead = 0
    target = None
    incoming: list[tuple[str, str]] = []
    if upstream is not None:
        target = git("rev-parse", "--short", upstream)
        counts = (git("rev-list", "--left-right", "--count", f"HEAD...{upstream}") or "").split()
        if len(counts) == 2 and all(part.isdigit() for part in counts):
            ahead, behind = int(counts[0]), int(counts[1])
        for line in (git("log", "--format=%h%x1f%s", "-n", str(INCOMING_CAP), f"HEAD..{upstream}") or "").splitlines():
            sha, _, subject = line.partition("\x1f")
            if sha:
                incoming.append((sha, subject[:SUBJECT_CHARS]))
    status = git("status", "--porcelain", "--untracked-files=no")
    changed = [line[3:] for line in (status or "").splitlines() if line.strip()]
    trees = None
    if status is not None and not any(path.startswith(("ui/", "schemas/")) for path in changed):
        ids = (git("rev-parse", "HEAD:ui", "HEAD:schemas") or "").split()
        trees = (ids[0], ids[1]) if len(ids) == 2 else None
    return GitState(
        branch=None if branch in (None, "HEAD") else branch,
        upstream=upstream,
        head=git("rev-parse", "--short", "HEAD"),
        target=target,
        behind=behind,
        ahead=ahead,
        dirty=len(changed),
        incoming=tuple(incoming),
        trees=trees,
    )


def read_stamp(dist: Path) -> tuple[str, str] | None:
    """The trees a build here was made from (module docstring § The page's stamp), or None."""
    try:
        data = json.loads((dist / STAMP_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ui, schemas = data.get("ui"), data.get("schemas")
    return (ui, schemas) if isinstance(ui, str) and isinstance(schemas, str) else None


def write_stamp(dist: Path, trees: tuple[str, str]) -> None:
    (dist / STAMP_FILE).write_text(json.dumps({"ui": trees[0], "schemas": trees[1]}) + "\n")


def stamp_build(root: Path, run: Run = run_command) -> None:
    """Stamp a build another door just made from this checkout (`tldrack up` builds the page when
    there is none). Best effort: an unstamped build is only `unknown`."""
    trees = read_git(root, run).trees
    dist = root / "ui" / "dist"
    if trees is not None and dist.is_dir():
        try:
            write_stamp(dist, trees)
        except OSError:
            pass


def page_id(dist: Path | None) -> str | None:
    """A short hash of the served HTML: it changes with every build, whoever made it."""
    if dist is None:
        return None
    digest = hashlib.sha256()
    try:
        pages = sorted(path for path in dist.iterdir() if path.suffix == ".html" and path.is_file())
        for path in pages:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    except OSError:
        return None
    return digest.hexdigest()[:16] if pages else None


def page_source(dist: Path | None, checkout_dist: Path, trees: tuple[str, str] | None) -> str:
    """Is the served page built from the checkout's current source? Pure apart from the stamp read.

    No build at all is stale: there is a page to make. A build served from somewhere else, source
    with uncommitted edits, and a build with no stamp are all unknown."""
    if dist is None:
        return STALE
    if dist.resolve() != checkout_dist.resolve() or trees is None:
        return UNKNOWN
    stamp = read_stamp(dist)
    if stamp is None:
        return UNKNOWN
    return CURRENT if stamp == trees else STALE


def install_due(ui_dir: Path) -> bool:
    """Whether `npm ci` must run before a build: nothing installed yet, or the lockfile is newer
    than the last install (npm rewrites `node_modules/.package-lock.json` on each one)."""
    try:
        installed = (ui_dir / "node_modules" / ".package-lock.json").stat().st_mtime
        return (ui_dir / "package-lock.json").stat().st_mtime > installed
    except OSError:
        return True


def build_page(root: Path, run: Run, npm: str, trees: tuple[str, str] | None) -> None:
    """Build `ui/dist`, staged: the served page is replaced only by a build that finished, so a
    failed one leaves the rack as it was. Raises `UpdateFailed`."""
    ui_dir = root / "ui"
    if install_due(ui_dir):
        code, output = run([npm, "ci", "--no-audit", "--no-fund"], ui_dir, BUILD_TIMEOUT_SECONDS)
        if code != 0:
            raise UpdateFailed(f"`npm ci` failed, so the page was not rebuilt: {tail(output)}")
    stage = ui_dir.joinpath(*STAGE_PARTS)
    shutil.rmtree(stage, ignore_errors=True)
    built = stage / "dist"
    try:
        stage.mkdir(parents=True)
        code, output = run([npm, "run", "build", "--", "--outDir", str(built), "--emptyOutDir"], ui_dir, BUILD_TIMEOUT_SECONDS)
        if code != 0:
            raise UpdateFailed(f"the page build failed, so the old page is still served: {tail(output)}")
        try:
            empty = (built / "index.html").stat().st_size == 0
        except OSError:
            empty = True
        if empty:
            raise UpdateFailed("the page build produced no rack page, so the old page is still served")
        if trees is not None:
            write_stamp(built, trees)
        dist, previous = ui_dir / "dist", stage / "previous"
        had = dist.exists()
        if had:
            dist.rename(previous)
        try:
            built.rename(dist)
        except OSError as exc:
            if had:
                previous.rename(dist)
            raise UpdateFailed(f"the new page could not be put in place: {exc}") from exc
    except OSError as exc:
        raise UpdateFailed(f"the page build could not run: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def runs_project_venv(root: Path) -> bool:
    """Whether this process runs `bridge/.venv`, the one `uv sync --project bridge` writes."""
    return Path(sys.prefix).resolve() == (root / "bridge" / ".venv").resolve()


class Updater:
    """The watch and the run. Thread-safe: `view()` from request threads, `tick()` from its own
    thread, one run at a time."""

    def __init__(
        self,
        root: Path | None,
        *,
        switches: FeatureSwitches,
        find_ui: Callable[[], Path | None] = lambda: None,
        run: Run = run_command,
        which: Callable[[str], str | None] = shutil.which,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        project_venv: Callable[[Path], bool] = runs_project_venv,
    ) -> None:
        self.root = root
        self._switches = switches
        self._find_ui = find_ui
        self._run = run
        self._which = which
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._project_venv = project_venv
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._phase = IDLE
        self._git = GitState()
        self._looked = False
        self._checked_at: float | None = inherited_check_time(os.environ)
        self._check_error: str | None = None
        self._error: str | None = None
        self._waiting: str | None = None
        #: The checkout and the remote as they were when a run ended with nothing more the timer can
        #: do (module docstring § Auto).
        self._settled_at: tuple[str | None, str | None] | None = None
        # exec keeps the pid, so the pid alone would not change across a restart in place.
        self._boot_id = f"{os.getpid()}-{int(clock())}"
        self._stale: Callable[[], bool | None] = lambda: None
        self._preflight: Callable[[], tuple[bool, str | None, str | None]] | None = None
        self._busy: Callable[[], list[str]] = list
        self._restart: Callable[[], bool] = lambda: False

    def bind(
        self,
        *,
        stale: Callable[[], bool | None],
        preflight: Callable[[], tuple[bool, str | None, str | None]],
        busy: Callable[[], list[str]],
        restart: Callable[[], bool],
    ) -> None:
        """The bridge's own half, known only once it is serving: is the running code stale, does
        the tree on disk start (`freshness.py`), which plugins hold a job, and the restart itself."""
        self._stale, self._preflight, self._busy, self._restart = stale, preflight, busy, restart

    # -- the wire ---------------------------------------------------------------
    def supported(self) -> str | None:
        """None when this bridge can update itself, else why not."""
        if self.root is None:
            return "this bridge is not running from a git checkout, so there is nothing to pull"
        if self._which("git") is None:
            return "git was not found, so the checkout cannot be read"
        return None

    def view(self) -> dict:
        reason = self.supported()
        with self._lock:
            state, looked = self._git, self._looked
            phase, error, waiting = self._phase, self._error, self._waiting
            checked_at, check_error = self._checked_at, self._check_error
        if reason is None and not looked and phase == IDLE:
            # The first reader before the first check: what the checkout says now, with no fetch.
            state = self._look()
        source = UNKNOWN if reason else self._page_source(state)
        stale = None if reason else self._stale()
        code = UNKNOWN if stale is None else (STALE if stale else CURRENT)
        blocked = None if reason else why_blocked(state)
        return {
            "supported": reason is None,
            "reason": reason,
            "branch": state.branch,
            "upstream": state.upstream,
            "head": state.head,
            "target": state.target,
            "behind": state.behind,
            "ahead": state.ahead,
            "dirty": state.dirty,
            "incoming": [{"sha": sha, "subject": subject} for sha, subject in state.incoming],
            "blocked": blocked,
            "page": {"id": page_id(self._find_ui()), "source": source},
            "code": code,
            "available": reason is None and ((state.behind > 0 and blocked is None) or source == STALE or code == STALE),
            "phase": phase,
            "waiting": waiting,
            "error": error,
            "check_error": check_error,
            "checked_at": checked_at,
            "auto": self._switches.on(AUTO_UPDATE),
            "checks": self._switches.on(UPDATE_CHECK),
            "boot_id": self._boot_id,
        }

    def request(self, action: object) -> tuple[int, dict]:
        """`POST /update`: start a check or a run on its own thread and answer at once. A run
        already in flight is the answer to a second press, not an error."""
        if action not in ACTIONS:
            return 400, {"error": 'Send {"action": "check"} or {"action": "apply"}'}
        reason = self.supported()
        if reason is not None:
            return 409, {"error": reason}
        if self._begin(CHECKING):
            target = self._check_run if action == "check" else self._apply_run
            threading.Thread(target=target, daemon=True, name=f"update-{action}").start()
        return 202, self.view()

    def busy_reasons(self) -> list[str]:
        """For `freshness.py`'s busy seam: a supervisor must not restart the bridge mid-update."""
        with self._lock:
            words = WORKING.get(self._phase)
        return [f"{OWN_BUSY} {words}"] if words else []

    # -- one run at a time ------------------------------------------------------
    def _begin(self, phase: str) -> bool:
        with self._lock:
            if self._phase != IDLE:
                return False
            self._phase = phase
            return True

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def _look(self) -> GitState:
        assert self.root is not None
        state = read_git(self.root, self._run)
        with self._lock:
            self._git, self._looked = state, True
        return state

    def _fetch(self) -> GitState:
        """Ask the remote, then look. A remote that cannot be reached is `check_error`, and the
        look still happens: the checkout can be read offline."""
        assert self.root is not None
        state = self._look()
        error = None
        if state.upstream is not None:
            branch = state.upstream.split("/", 1)[-1]
            refspec = f"+refs/heads/{branch}:refs/remotes/{REMOTE}/{branch}"
            code, output = self._run(["git", "fetch", "--quiet", "--no-tags", REMOTE, refspec], self.root, FETCH_TIMEOUT_SECONDS)
            if code != 0:
                error = f"could not reach `{REMOTE}`: {tail(output) or f'git fetch exited {code}'}"
            state = self._look()
        with self._lock:
            self._checked_at, self._check_error = self._clock(), error
        return state

    def _page_source(self, state: GitState) -> str:
        assert self.root is not None
        return page_source(self._find_ui(), self.root / "ui" / "dist", state.trees)

    # -- check ------------------------------------------------------------------
    def check(self) -> None:
        """One look at the remote. A no-op while a run is in flight."""
        if self.supported() is None and self._begin(CHECKING):
            self._check_run()

    def _check_run(self) -> None:
        try:
            self._fetch()
        except Exception as exc:  # noqa: BLE001 — a check that cannot run is a line on the card, never a dead thread
            with self._lock:
                self._check_error = f"the check could not run: {type(exc).__name__}: {exc}"
        finally:
            self._set_phase(IDLE)

    # -- apply ------------------------------------------------------------------
    def apply(self) -> None:
        """Do whatever is due: pull, build, restart. A no-op while a run is in flight."""
        if self.supported() is None and self._begin(CHECKING):
            self._apply_run()

    def _apply_run(self) -> None:
        error = None
        waiting: str | None = None
        restarting = False
        with self._lock:
            # The last run's verdict is about the last run: a card must not show it beside this one.
            self._error = self._waiting = None
        try:
            waiting, restarting = self._apply_steps()
        except UpdateFailed as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — the same: a crash is a sentence on the card
            error = f"the update could not finish: {type(exc).__name__}: {exc}"
        with self._lock:
            state = self._git
            self._error, self._waiting = error, waiting
            # Nothing more the timer can do here until something moves: a failure, or a restart
            # only the operator can make. A restart held by a busy plugin is tried again.
            settled = error is not None or waiting == RESTART_BY_HAND
            self._settled_at = (state.head, state.target) if settled else None
            if not restarting:
                self._phase = IDLE
        if error:
            self._log(f"  update: FAILED — {error}")

    def _apply_steps(self) -> tuple[str | None, bool]:
        """The run → `(what it is waiting for, whether the bridge is about to restart)`. While it
        restarts the phase stays `restarting`: the process is on its way out."""
        assert self.root is not None
        root = self.root
        state = self._fetch()
        before = state
        pulled = False
        if state.behind > 0 and why_blocked(state) is None:
            self._set_phase(PULLING)
            code, output = self._run(["git", "merge", "--ff-only", "--quiet", str(state.upstream)], root, GIT_TIMEOUT_SECONDS)
            if code != 0:
                raise UpdateFailed(f"the pull failed and the checkout is as it was: {tail(output)}")
            pulled = True
            state = self._look()
            self._log(
                f"  update: pulled {before.head} → {state.head} ({before.behind} commit{'s' if before.behind != 1 else ''})"
            )

        source = self._page_source(state)
        moved = pulled and state.trees is not None and state.trees != before.trees
        if source == STALE or (source == UNKNOWN and moved):
            npm = self._which("npm")
            if npm is None:
                raise UpdateFailed("npm was not found, so the page was not rebuilt: install Node.js, then press UPDATE again")
            self._set_phase(BUILDING)
            build_page(root, self._run, npm, state.trees)
            self._log("  update: rebuilt the page")

        if pulled and self._lock_moved(before, state):
            uv = self._which("uv")
            if uv is not None and self._project_venv(root):
                self._set_phase(SYNCING)
                code, output = self._run([uv, "sync", "--locked", "--project", str(root / "bridge")], root, SYNC_TIMEOUT_SECONDS)
                if code != 0:
                    raise UpdateFailed(f"`uv sync` failed, so the bridge was not restarted: {tail(output)}")

        self._set_phase(VERIFYING)
        if self._preflight is None:
            return (RESTART_BY_HAND if pulled else None), False
        stale, status, problem = self._preflight()
        if not stale:
            return None, False
        if status != "ok":
            raise UpdateFailed(f"the new code does not start, so the bridge was not restarted: {problem or status}")
        busy = [reason for reason in self._busy() if not reason.startswith(OWN_BUSY)]  # not this run itself
        if busy:
            return f"{RESTART_WAITS} {'; '.join(busy[:3])}", False
        self._set_phase(RESTARTING)
        self._log("  update: restarting to run the new code")
        with self._lock:
            checked_at = self._checked_at
        if checked_at is not None:
            os.environ[CHECKED_ENV] = repr(checked_at)
        if self._restart():
            return None, True
        os.environ.pop(CHECKED_ENV, None)
        return RESTART_BY_HAND, False

    def _lock_moved(self, before: GitState, after: GitState) -> bool:
        """Whether a pull changed the bridge's dependencies."""
        assert self.root is not None
        code, _output = self._run(
            ["git", "diff", "--quiet", str(before.head), str(after.head), "--", "bridge/uv.lock", "bridge/pyproject.toml"],
            self.root,
            GIT_TIMEOUT_SECONDS,
        )
        return code == 1

    # -- the timer --------------------------------------------------------------
    def auto_due(self) -> bool:
        """Whether the timer should run an update now (module docstring § Auto)."""
        if not self._switches.on(AUTO_UPDATE) or self.supported() is not None:
            return False
        view = self.view()
        with self._lock:
            settled = self._settled_at
        if view["dirty"] or not view["available"] or view["phase"] != IDLE:
            return False
        return settled != (view["head"], view["target"])

    def tick(self) -> None:
        """One turn of the timer: the check when its switch is on, then an update when auto is."""
        if not self._switches.on(UPDATE_CHECK):
            return
        self.check()
        if self.auto_due():
            self.apply()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="update-watch").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = FIRST_CHECK_SECONDS
        while not self._stop.wait(wait):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the timer must never take the bridge down
                self._log(f"  update: check failed — {type(exc).__name__}: {exc}")
            with self._lock:
                held = (self._waiting or "").startswith(RESTART_WAITS)
            wait = RETRY_SECONDS if held and self._switches.on(AUTO_UPDATE) else CHECK_SECONDS
