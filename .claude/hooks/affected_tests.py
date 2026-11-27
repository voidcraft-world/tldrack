#!/usr/bin/env python3
"""PostToolUse(Edit|Write|MultiEdit|NotebookEdit|Bash): test what the edit touched.
PreToolUse(Edit|MultiEdit|NotebookEdit) with `--pre`: hash the file before the edit.
`--red`, run by hand: print the red ledger (WHO GETS WOKEN).

For each changed file it runs two checks, and wakes the session whose change failed
one (exit 2, the report on stderr; WHO GETS WOKEN says which sessions those are):

1. The purity scan of that file alone (`tests/test_purity.py --files`), so a home
   path or a private word is caught the moment it is written, not at CI.
2. The tests of the project the file belongs to (`scripts/projects.py` owns what a
   project is and how it is tested). A file outside every project belongs to the
   root, whose test is the purity test itself.

Bash is in the matcher because a file edited through the shell (`sed -i`, a
heredoc, a codegen script) sends no `file_path`: the hook then takes every TRACKED
file the command changed, dirty or made clean again by a revert, and every dirty one
whose mtime moved since its last stamp. Without that, a whole change made through
the shell would go unchecked.

WHAT IT WILL EXECUTE. A test runner executes whatever it discovers: a manifest's
`test` script, a `conftest.py`, a config file in any parent directory, a test file
in `dist/`. Each runner discovers differently, so the hook never guesses file by
file what one would load. It runs the tests in a SNAPSHOT, a copy outside the
repository of only what git or the agent vouches for:

- every tracked file, as the working tree has it (a `git apply` or a checkout put
  it there by the agent's own choice);
- every untracked file the agent wrote whole: a Write whose `content` is exactly
  the file, or an Edit of a file trusted just before it. The PreToolUse half takes
  that "before" hash, so a one-line Edit cannot launder a file that arrived;
- each Node project's installed `node_modules`, linked. Node resolves a linked
  package's own imports from its real path, back inside the tree, so a Node project
  is skipped while an untracked `node_modules` sits in any directory above it that
  has no tracked `package.json`. A Python project gets a virtualenv the hook owns,
  synced from the project's own lock.

What arrived by `curl`, `tar` or a patch to an untracked path is simply not in the
copy, whatever the runner's discovery rules. One narrow extra sits on top: a
project's own manifest that the shell changed and the agent did not write skips the
project. It is defense in depth, not a promise about config: every other tracked
file (a parent `pyproject.toml`, a `conftest.py`, a `vitest.config.ts`) is in the
tracked tier below, and listing them one by one is the game the snapshot replaced. Every file whose trust rests on its content (an untracked file
the agent wrote, a manifest) is read once, checked, and those same bytes are what
the copy holds, so nothing can be swapped between the check and the run. The snapshot and every piece of the hook's state live in the user
cache (`$XDG_CACHE_HOME/tldrack/edit-hook/`), so nothing that lands in the tree can
forge them, and no Python the hook starts reads bytecode from the tree (`-I -B`,
modules compiled from source, `PYTHONPYCACHEPREFIX`).

Trusted, and not this hook's to police: tracked files as the working tree has them
(a shell edit to one is the change under test, and testing the index copy instead
would test nothing), installed dependencies, and the hook's own code: this file,
`scripts/projects.py` and `tests/test_purity.py`. Claude Code runs this file from
the working tree, so loading the other two from the index would protect nothing this
file does not already decide. A skipped project is silent; CI still tests it.

One runner per project at a time, and one for the purity scan of changed files. A
call that finds a runner busy queues its change and waits; the runner takes the
queue as one batch per pass, re-checking trust and re-copying first, so the last
edit is always the one tested. Stdlib only. The PostToolUse half is wired async with
rewake in `.claude/settings.json`: it never blocks an edit.

WHO GETS WOKEN. Many sessions share one checkout, and a failure stays red until it
is fixed, so "wake whoever ran the tests" wakes bystanders over and over. A session
is woken only for a failure it plausibly caused, and the one that did is never
dropped: where the hook cannot tell, it wakes every plausible culprit.

- A claim says who changed a file, and when: somewhere between the earliest the
  change could have happened and the moment a pass could first see it. An Edit or
  Write claims its own file, at its ctime. A Bash call claims every tracked file its
  command changed (each dirty one, and each one a revert made clean again: a checkout,
  a stash, a restore; a commit takes a file as it is) whose ctime falls inside the
  command: the payload's `duration_ms` and a second before it for the CLI to start
  the hook, ending when the hook starts, since nothing the command wrote is newer (an
  older CLI sends no duration: since the session's previous call, at most ten
  minutes). It dates the change from the start of that window: a command that wrote
  the file early and again late changed it early. ctime, because a rename or a `cp -p`
  keeps the mtime, and no command can set a ctime back; a deleted file is dated once,
  when it is first seen gone, by its directory's ctime capped at the end of that
  call's window (a neighbour's save beside it after the call started cannot push it
  out), since that ctime moves with every later file made or removed beside it. Two
  sessions whose commands overlap both claim, unless the file is exactly as another
  session's tools left it and the command can have written none of it: a run of Edits,
  by any sessions, that began from a state older than the window, or a Write, which
  chose every byte (Claude Code writes only a file it has read, so what a Write kept
  of a shell change it chose, and its session hears for all of it). Claims of one
  state join: the session whose tool wrote it first, then each command that overlapped
  it, so none of them claims it twice (the `git diff` after an Edit). A dirty file
  that moved outside the window was changed by someone else (a neighbour mid-command,
  a background job, the operator's editor): it is queued under the first claimant of
  the file's last claim, and under `-`, since it may be nobody's. A claim is forgotten
  once its file is clean (a Bash call sees that; a claim made while its git ran
  stands), a commit touched it after the claim, or it is PRUNE_S old, so a change
  that landed never names an owner for a later one.
- A HEAD move (a pull, a checkout, a reset) rewrites files no session claims: each
  one rewritten since the latest look at the old HEAD makes `-` a SUSPECT (below) of
  its project's tests. No pass runs for it.
- Each change is queued for each runner that checks it. A pass reads one batch; a
  change a finished pass already read (it was there before the pass began, in a file
  the pass covers) joins that pass's verdict instead of running it again, and the
  changed files' scan keeps each file's latest scan for that. A call is done with a
  runner only once every change it queued there has been judged.
- A failure has a SIGNATURE, stable across runs (`tests <project> | <test>`, and
  `purity <file> | <rule> | <digest>`, the digest of the hit's masked line, so two
  hits in one file are two failures), and the RED LEDGER records each red one with
  its owners, the time it first turned red, the time it was last seen red and how
  many of it there were. A change is plausible for a signature when it could have
  made it red: it came after the last pass that ran what the signature names and saw
  it green (tests only: a purity hit is one file's bytes, and only changes to that
  file count), and, for a signature already red, before it first turned red, or in a
  batch that finds more of it (one more private word, a copied line). A new signature
  is owned by the plausible owners among those whose changes the check saw (per file
  for purity, the whole batch for a project's tests); one already red gains only
  plausible owners, so a change made after it turned red is a bystander's. `-` is
  plausible like a session (a change no session made, a HEAD move), and so is the
  unknown past of a project's tests that no run has seen green: a red can arrive with
  no session behind it. A session is woken for a signature when it becomes an owner,
  and again whenever a batch holds a change its own call claimed while the failure
  stands (one queued under it only as a file's last claimant is a guess), so the
  culprit hears again on its next change, unless `-` or another session co-owns it:
  a failure that may be nobody's, or one of several sessions', is told once to each
  session that may have caused it, and never pinned on one. A signature that names
  no single failure (a test file that failed to load, a run that names no test) can
  hide a second one: when a batch with an owner's change finds it still red, every
  change since its last green run that no complete run has checked (the SUSPECTS
  below, and this batch's) may be what stands, so their sessions become owners with
  `-`, each told once.
- A run that reports test by test (or passes) is complete: it clears every signature
  of its own that it no longer sees and that was last seen red before it began to
  read the tree, so a slower runner never clears what a faster one found after it
  read. A run whose only gap is test files that failed to load does the same for
  every other test file, and saw those green from when it began; any other run that
  cannot vouch for everything (an interrupted or timed-out run, a failure that names
  no test, a skipped project, a scan that crashed) clears nothing. Both keep their
  batch's owners as SUSPECTS of that check, a change that joins their verdict late
  included: a signature that turns up new there, once a run can see it, counts them
  as plausible too, until a complete run that began after their changes spends them.
  A pass whose every change was made before the ledger existed (the first ever,
  wherever its runner sorts) records what is already red in a project's tests as
  unowned, and wakes nobody for it; a pass with any later change attributes, so a
  project first touched long after still tells its culprit. Purity needs no seed:
  only the sessions that changed the file a hit is about are ever woken for it.
- A call that queued a change of its own waits for each runner it queued into,
  taking over any whose lock frees, until DEADLINE_S: 285 s after it started, under
  the 300 s kill in `.claude/settings.json`, with room for the interpreter and the
  report. A pass that cannot finish by then is stopped and left in flight; so is the
  batch of a runner killed mid-pass, and the next runner to take its lock runs it. A
  runner's lock is a flock the kernel frees when its process ends, so a machine that
  sleeps through a pass keeps its runner. A report is mail in the session's
  mailbox: every call of a session prints and clears that session's mail, so a
  report that missed its waiter reaches the next call unless a complete run has seen
  its failures fixed meanwhile, and one delivery shows a check's block once.
- `python3 .claude/hooks/affected_tests.py --red` prints the ledger: each red
  signature, its owners and its age, the unowned ones too, and each check's suspects.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import types
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

STARTED = time.time()  # before projects.py compiles: as close as the hook can see to when its tool ended

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]).resolve()
CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "tldrack"
    / "edit-hook"
    / hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
)
STATE = CACHE / "state"
STAMPS = STATE / "stamps.json"
AUTHORED = STATE / "authored.json"
PRE = STATE / "pre"
SNAPSHOTS = CACHE / "snapshots"
VENVS = CACHE / "venvs"
RUN_TIMEOUT_S = 240
TAIL_LINES = 60

# Who gets woken: owned by the WHO GETS WOKEN note above. New names (`v2`), so a runner of the
# previous version, alive during the rollout, never parses these lines; the runner locks keep their
# old names, so the two versions never test one snapshot at once.
WOKEN = STATE / "v2"
QUEUE = WOKEN / "queue"
INFLIGHT = WOKEN / "inflight"
VERDICTS = WOKEN / "verdicts"
DONE = WOKEN / "done"
MAIL = WOKEN / "mail"
SEEN = WOKEN / "seen"
OWNERS = WOKEN / "owners.json"
GONE = WOKEN / "gone.json"
HEAD_SEEN = WOKEN / "head.json"
LEDGER = WOKEN / "ledger.json"
DURATIONS = WOKEN / "durations.json"
STATE_LOCK = WOKEN / "state.lock"
NOBODY = "-"
DEADLINE_S = 285
LOCK_STALE_S = 330  # the previous version's pid file: past the 300 s kill in `.claude/settings.json`, its runner is gone
SLACK_S = 10
MIN_PASS_S = 10
POLL_S = 0.25
FALLBACK_WINDOW_S = 600
PRE_FRESH_S = 600  # a permission prompt can hold an Edit between its two halves
PRUNE_S = 6 * 3600
MAX_SIGNATURES = 8
MAX_SUSPECT_FILES = 6


def _load_projects() -> types.ModuleType:
    # Compiled from source, never imported: an import can read a planted __pycache__ .pyc,
    # and a directory on sys.path is a place to shadow a module from.
    path = ROOT / "scripts" / "projects.py"
    module = types.ModuleType("tldrack_projects")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module  # dataclasses resolves annotations through sys.modules
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


_projects = _load_projects()
SKIP_DIRS, Project, projects_of = _projects.SKIP_DIRS, _projects.Project, _projects.projects_of
find_projects = _projects.find_projects
ProjectT = Any  # scripts/projects.py's Project: compiled at run time (above), so a type checker cannot see it


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    # --literal-pathspecs: a path is a path. `x*/package.json` must never match a tracked sibling.
    # core.fsmonitor=false: a command-line -c outranks .git/config, so a configured monitor never runs
    # here. A hostile .git/config is otherwise outside this hook: every git call anyone makes runs it.
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "--literal-pathspecs", "-C", str(ROOT), *args], capture_output=True, check=False
    )


def git_paths(*args: str) -> list[str]:
    return [name for name in git(*args).stdout.decode().split("\0") if name]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def in_repo(path: Path) -> bool:
    return ROOT in path.parents and not set(path.relative_to(ROOT).parts) & SKIP_DIRS


def repo_path(name: str) -> Path | None:
    """A repo-relative name read from state or from git, as a path in the tree; None when it points anywhere else."""
    parts = PurePosixPath(name)
    if not name or parts.is_absolute() or ".." in parts.parts or not in_repo(ROOT / name):
        return None
    return ROOT / name


# ---------------------------------------------------------------- what changed


def changed_by_edit(payload: dict) -> list[Path]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return []
    path = Path(raw)
    return [(path if path.is_absolute() else ROOT / path).resolve()]


def dirty_files() -> list[Path] | None:
    """Tracked files that differ from HEAD or the index, deleted ones included; None when git cannot say."""
    status = git("status", "--porcelain", "-z", "--untracked-files=no")
    if status.returncode != 0:
        return None  # not "nothing is dirty": the stamps and claims stand until git can answer
    listed = iter(status.stdout.decode().split("\0"))
    dirty = []
    for entry in listed:
        if len(entry) <= 3:
            continue
        dirty.append(ROOT / entry[3:])
        if entry[0] in "RC":
            next(listed, None)  # -z prints a rename's source as its own field: skip it
    return dirty


def file_times(path: Path, now: float) -> tuple[int, int | None, float]:
    """(mtime stamp, ctime in ns, change time in s). A deleted file: stamp -1, no ctime, and the
    ctime of its nearest directory, which an unlink moves: never earlier than the deletion. That
    ctime moves again with every later file made or removed beside it: `deleted_at` keeps the first."""
    try:
        status = path.lstat()
    except OSError:
        for directory in path.parents:
            with contextlib.suppress(OSError):
                return -1, None, directory.lstat().st_ctime_ns / 1e9
            if directory == ROOT:
                break
        return -1, None, now
    return status.st_mtime_ns, status.st_ctime_ns, status.st_ctime_ns / 1e9


def deleted_at(name: str, at: float, stamps: dict, gone: dict) -> float:
    """A deleted file's change time: its directory's ctime when it was first seen gone (stamp -1), kept
    from then on, so a later neighbour in that directory never dates the deletion into a later window."""
    return number(gone.get(name), at) if stamps.get(name) == -1 else at


def reverts(stamped: dict, dirty: dict[Path, tuple[int, int | None, float]], now: float) -> dict:
    """Files dirty at the last look and clean now that were rewritten since (a checkout, a stash, a
    restore): a change like any other, and the one that clears what it fixed. A commit takes a file
    as it is, so its mtime still matches the stamp and it is not one."""
    names = {rel(p) for p in dirty}
    found = {}
    for name, stamp in stamped.items():
        path = repo_path(name) if name not in names else None
        if path is not None and (times := file_times(path, now))[0] != stamp:
            found[path] = times
    return found


# ---------------------------------------------------------------- what the agent wrote


def pre_record(path: Path) -> Path:
    return PRE / f"{hashlib.sha256(rel(path).encode()).hexdigest()[:32]}.json"


def take_pre_hash(payload: dict) -> None:
    """PreToolUse: the file's hash before an Edit, for record_edits to judge the result by.

    Its ctime too, for the claim (`claim_base`); trust reads only the hash."""
    for path in [p for p in changed_by_edit(payload) if in_repo(p)]:
        try:
            ctime: int | None = path.lstat().st_ctime_ns
        except OSError:
            ctime = None
        write_json(pre_record(path), {"path": rel(path), "sha": sha256_of(path), "ctime": ctime, "at": time.time()})


def index_sha(name: str) -> str | None:
    shown = git("show", f":{name}")
    return hashlib.sha256(shown.stdout).hexdigest() if shown.returncode == 0 else None


def wrote_whole(payload: dict, path: Path, authored: dict) -> bool:
    """Did the agent determine this file's whole content? See WHAT IT WILL EXECUTE."""
    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if payload.get("tool_name") == "Write":
        content = tool_input.get("content")
        return isinstance(content, str) and hashlib.sha256(content.encode()).hexdigest() == sha256_of(path)
    record = pre_record(path)
    before = read_json(record).get("sha")
    record.unlink(missing_ok=True)
    if not isinstance(before, str):
        return False  # no snapshot (a new file, or the PreToolUse half did not run): fail closed
    return before == authored.get(rel(path)) or before == index_sha(rel(path))


def claim_base(payload: dict, before: dict, owner: object, now: float, written: bool) -> tuple[int | None, bool]:
    """(base, whole) of the run of tool writes this one ends: the ctime the file had before it (None when
    unknown), and whether it began with a Write, which chose every byte (`written`: the file still holds
    them). Who gets woken: the WHO GETS WOKEN note."""
    if payload.get("tool_name") == "Write":
        return None, written
    ctime, taken = before.get("ctime"), before.get("at")
    if not (isinstance(ctime, int) and isinstance(taken, (int, float)) and 0 <= now - taken <= PRE_FRESH_S):
        return None, False
    if isinstance(owner, dict) and owner.get("ctime") == ctime:  # exactly as the previous tool write left it
        if owner.get("whole") is True:
            return None, True
        if isinstance(owner.get("base"), int):
            return int(owner["base"]), False  # the same run of Edits
    return ctime, False


def record_edits(payload: dict, paths: list[Path], now: float, owners: dict) -> dict[str, tuple[int | None, bool]]:
    """Move each file's stamp so Bash skips it, and keep its hash only if the agent wrote it whole.

    Returns each file's `claim_base`."""
    stamps, authored = read_json(STAMPS), read_json(AUTHORED)
    bases: dict[str, tuple[int | None, bool]] = {}
    for path in paths:
        try:
            stamps[rel(path)] = path.lstat().st_mtime_ns
        except OSError:
            continue
        before = read_json(pre_record(path))  # read before wrote_whole consumes it
        digest = sha256_of(path)
        whole = bool(digest) and wrote_whole(payload, path, authored)
        if whole and digest:
            authored[rel(path)] = digest
        else:
            authored.pop(rel(path), None)
        bases[rel(path)] = claim_base(payload, before, owners.get(rel(path)), now, whole)
    write_json(STAMPS, stamps)
    write_json(AUTHORED, authored)
    return bases


# ---------------------------------------------------------------- the snapshot


def manifest_trusted(project: ProjectT, authored: dict, tracked: set[str], copy: Path) -> bool:
    """Tracked (by its exact path), and the COPY's bytes are the index's or the agent's own write."""
    manifest = rel(project.manifest)
    if manifest not in tracked or (copy / manifest).is_symlink():
        return False
    digest = sha256_of(copy / manifest)
    return digest is not None and digest in {index_sha(manifest), authored.get(manifest)}


def no_stray_node_modules(project: ProjectT, tracked: set[str]) -> bool:
    """No untracked node_modules above the project: a linked package would resolve into it."""
    directory = project.path.parent
    while directory == ROOT or ROOT in directory.parents:
        manifest = (directory / "package.json").relative_to(ROOT).as_posix()
        if (directory / "node_modules").exists() and manifest not in tracked:
            return False
        directory = directory.parent
    return True


def vouched_files(tracked: set[str], authored: dict) -> set[str]:
    """Every tracked file, plus every untracked file the agent once wrote (its bytes are checked on copy)."""
    return {
        name
        for name in tracked | set(authored)
        if not set(name.split("/")) & SKIP_DIRS and ((ROOT / name).is_symlink() or (ROOT / name).is_file())
    }


def copy_checked(src: Path, dst: Path, expected: str | None) -> bool:
    """Read once, check those bytes against `expected` (None: no check), write those same bytes."""
    before = src.stat()
    data = src.read_bytes()
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        return False
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(tmp, dst)
    return True


def sync_snapshot(dest: Path, authored: dict) -> set[str]:
    """Make `dest` hold exactly the vouched files, plus each Node project's node_modules (linked).

    Returns the tracked set it was built from, for the checks that run on the copy."""
    tracked = set(git_paths("ls-files", "-z"))
    wanted = vouched_files(tracked, authored)
    manifests = set(_projects.MANIFESTS)
    dest.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(dest):
        here = Path(current)
        for name in [d for d in dirs if (here / d).is_symlink()]:
            dirs.remove(name)
            (here / name).unlink()  # a linked node_modules, or a tracked dir link: recreated below
        for name in files:
            if (here / name).relative_to(dest).as_posix() not in wanted:
                (here / name).unlink()
    for name in sorted(wanted):
        src, dst = ROOT / name, dest / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        if src.is_symlink():
            if name not in tracked:
                dst.unlink(missing_ok=True)  # the agent wrote a file here, not this link: a link has no bytes to check
                continue
            if not (dst.is_symlink() and os.readlink(dst) == os.readlink(src)):
                dst.unlink(missing_ok=True)
                os.symlink(os.readlink(src), dst)
            continue
        if dst.is_symlink():
            dst.unlink()
        content_trusted = name not in tracked or name.rsplit("/", 1)[-1] in manifests
        if not content_trusted and dst.is_file():
            have, need = dst.stat(), src.stat()
            if (have.st_size, have.st_mtime_ns) == (need.st_size, need.st_mtime_ns):
                continue  # an ordinary tracked file, unchanged since the last copy
        if not copy_checked(src, dst, None if name in tracked else authored.get(name)):
            dst.unlink(missing_ok=True)  # an untracked file whose bytes are no longer the agent's
    for name in wanted & tracked:
        if name.rsplit("/", 1)[-1] != "package.json":
            continue
        target = (ROOT / name).parent / "node_modules"  # the folder checked is the folder linked
        link = (dest / name).parent / "node_modules"
        if target.is_dir() and not target.is_symlink() and not link.exists():
            os.symlink(target, link)
    return tracked


# ---------------------------------------------------------------- running


@dataclass(frozen=True)
class Ran:
    rc: int | None  # None: it never finished (timed out), or the tool is not installed here
    output: str
    timed_out: bool = False

    @property
    def missing(self) -> bool:
        return self.rc is None and not self.timed_out

    @property
    def ok(self) -> bool:
        return self.rc == 0 or self.missing  # a tool not installed here passes; CI still runs it

    def lines(self) -> list[str]:
        return self.output.strip().splitlines()


# Every command a pass runs is on a LEASH. It runs in its own process group, so a timeout here stops the
# whole tree (npm -> node, uv -> pytest) and never leaves a test writing into a snapshot the next pass
# re-syncs. That takes it out of the hook's group, which is how Claude Code stops a hook (SIGTERM on an
# abort, SIGKILL at the 300 s timeout), so the leash leads the new group and holds one end of a socket
# pair whose other end only this process holds. However this process ends, SIGKILL included, the kernel
# closes that end, and the leash kills its whole group: no test outlives the hook that started it. Nor its
# own run: once the command ends, the leash reports its status on the socket and kills the group, itself
# included, while it still leads it, so a server a test forgot goes too and the kill can reach no other
# group. The leash is this constant run by the same interpreter with -I -B, so it reads nothing from the
# tree; it reports a command that cannot start as "missing".
LEASH = """
import os, signal, subprocess, sys, threading
line = int(sys.argv[1])
try:
    child = subprocess.Popen(sys.argv[2:])
except OSError:
    os.write(line, b"missing")
    sys.exit(127)
def watch():
    while os.read(line, 64):
        pass
    os.killpg(0, signal.SIGKILL)
threading.Thread(target=watch, daemon=True).start()
os.write(line, b"rc %d" % child.wait())
os.killpg(0, signal.SIGKILL)
"""
LEASH_STATUS = re.compile(rb"rc (-?\d+)")


def run(command: list[str], cwd: Path, extra_env: dict[str, str] | None = None, timeout: float = RUN_TIMEOUT_S) -> Ran:
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(CACHE / "pycache"), **(extra_env or {})}
    ours, theirs = socket.socketpair()
    try:
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", LEASH, str(theirs.fileno()), *command],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
                pass_fds=(theirs.fileno(),),
            )
        except OSError:
            return Ran(None, "")
        finally:
            theirs.close()  # the leash holds its end now: this process keeps only its own
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
            try:
                out, err = child.communicate(timeout=SLACK_S)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return Ran(None, f"{out}{err}\ntimed out after {round(timeout)} s", timed_out=True)
        ours.setblocking(False)
        said = b""
        with contextlib.suppress(OSError):
            said = ours.recv(32)
        if said == b"missing":
            return Ran(None, "")  # the tool is not installed here; CI still runs it
        status = LEASH_STATUS.fullmatch(said)
        return Ran(int(status[1]) if status else child.returncode, out + err)  # the leash itself died by its own kill
    finally:
        ours.close()


def purity(*args: str) -> list[str]:
    return [sys.executable, "-I", "-B", str(ROOT / "tests" / "test_purity.py"), *args]


def run_tests(project: ProjectT, name: str, timeout: float) -> tuple[Ran | None, float, float] | None:
    """The project's tests in its snapshot, and when the copy began and finished reading the
    tree (who gets woken: a change after that is not in the copy). None = nothing to run; a
    None run = skipped, untrusted."""
    began = time.time()  # before AUTHORED is read: an untracked file is in the copy only once it is there
    authored = read_json(AUTHORED)
    snapshot = SNAPSHOTS / name
    tracked = sync_snapshot(snapshot, authored)
    copied = time.time()
    # Judged on every run, never only the first, and on the copy's bytes: what is checked is what runs.
    if not manifest_trusted(project, authored, tracked, snapshot):
        return None, began, copied
    if project.kind == "node" and not no_stray_node_modules(project, tracked):
        return None, began, copied
    copy = Project(project.kind, snapshot / rel(project.path))
    if not copy.has_tests():
        return None  # nothing to run: owned by scripts/projects.py — see the note there.
    extra = {"UV_PROJECT_ENVIRONMENT": str(VENVS / name)} if project.kind == "python" else None
    return run(copy.test_command(), copy.path, extra, timeout), began, copied


# ---------------------------------------------------------------- runners and their queues
# Who gets woken: owned by the WHO GETS WOKEN note in this file's docstring.


@dataclass(frozen=True)
class Runner:
    """One lock, one queue: the root's whole-tree purity scan, the changed files' purity scan, or a project's tests."""

    name: str
    check: str  # "root" | "files" | "tests"
    project: ProjectT | None = None


ROOT_SCAN = Runner("root", "root")
FILES_SCAN = Runner("purity-files", "files")


def label(project: ProjectT) -> str:
    return f"{rel(project.path)} ({project.kind})"


def tests_runner(project: ProjectT) -> Runner:
    return Runner(f"{project.kind}--{rel(project.path).replace('/', '__')}", "tests", project)


def runners_for(path: Path) -> list[Runner]:
    found = projects_of(path, ROOT)
    # A root file's project run IS the whole-tree purity scan; only project files need their own.
    return [FILES_SCAN, *map(tests_runner, found)] if found else [ROOT_SCAN]


def runner_named(name: str, paths: list[str]) -> Runner | None:
    """The runner a queue belongs to, found again from its changes (or the tree): None when it is gone."""
    for path in paths:
        for runner in runners_for(ROOT / path):
            if runner.name == name:
                return runner
    if name in (ROOT_SCAN.name, FILES_SCAN.name):
        return ROOT_SCAN if name == ROOT_SCAN.name else FILES_SCAN
    return next((r for r in map(tests_runner, find_projects(ROOT)) if r.name == name), None)


def safe_id(value: object) -> str:
    """A payload id as a file-name part: [A-Za-z0-9_-] only."""
    return re.sub(r"[^A-Za-z0-9_-]", "", value)[:128] if isinstance(value, str) else ""


@dataclass(frozen=True)
class Entry:
    """One queued change: whose, which call waits on it, which file, the earliest it could have
    happened (`t`), and when a pass could first see it (`ready`, never before its last write; an
    untracked file the agent wrote: once AUTHORED holds it)."""

    owner: str
    call: str
    path: str
    t: float
    ready: float
    pid: int = 0
    deadline: float = 0.0

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("owner", "call", "path", "t", "ready", "pid", "deadline")}

    @staticmethod
    def parse(data: object) -> Entry | None:
        """State is in the user cache, and still read as untrusted: a bad line is dropped."""
        if not isinstance(data, dict):
            return None
        owner, call, path = data.get("owner"), data.get("call"), data.get("path")
        t, ready, pid, deadline = data.get("t"), data.get("ready"), data.get("pid", 0), data.get("deadline", 0.0)
        if not (isinstance(owner, str) and isinstance(call, str) and isinstance(path, str)):
            return None
        if not (isinstance(t, (int, float)) and isinstance(ready, (int, float))):
            return None
        if not (isinstance(pid, int) and isinstance(deadline, (int, float))):
            return None
        if not owner or safe_id(owner) != owner or not call or safe_id(call) != call or repo_path(path) is None:
            return None
        return Entry(owner, call, path, float(t), float(ready), pid, float(deadline))


def read_entries(path: Path) -> list[Entry]:
    try:
        text = path.read_text()
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        try:
            entry = Entry.parse(json.loads(line))
        except ValueError:
            continue
        if entry is not None:
            entries.append(entry)
    return entries


def write_entries(path: Path, entries: list[Entry]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(e.to_json()) + "\n" for e in entries))
    os.replace(tmp, path)


@contextlib.contextmanager
def state_lock() -> Iterator[None]:
    """One short critical section for every call of this checkout: stamps, owners, queues, ledger, mail."""
    WOKEN.mkdir(parents=True, exist_ok=True)
    with open(STATE_LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def enqueue(entries: list[Entry]) -> None:
    """Append each change to every runner that checks it. The caller holds the state lock."""
    lines: dict[str, list[str]] = {}
    for entry in entries:
        for runner in runners_for(ROOT / entry.path):
            lines.setdefault(runner.name, []).append(json.dumps(entry.to_json()) + "\n")
    QUEUE.mkdir(parents=True, exist_ok=True)
    for name, chunk in lines.items():
        with open(QUEUE / f"{name}.jsonl", "a") as out:
            out.write("".join(chunk))


def take_batch(name: str) -> list[Entry]:
    """In flight (a runner died or ran out of time) plus queued, now in flight. Caller holds the state lock."""
    inflight, queued = INFLIGHT / f"{name}.jsonl", QUEUE / f"{name}.jsonl"
    entries = read_entries(inflight) + read_entries(queued)
    write_entries(inflight, entries)
    queued.unlink(missing_ok=True)
    return entries


def names_with_work() -> list[str]:
    names = {p.stem for d in (QUEUE, INFLIGHT) for p in d.glob("*.jsonl")}
    names |= {p.stem for p in STATE.glob("*.pending")}  # rollout: a call of the previous version found it busy
    return sorted(names)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False  # PermissionError: the pid is another user's now, so ours is gone
    return True


HELD: dict[str, int] = {}  # runner name -> the descriptor whose flock this process holds while it runs it


def previous_version_holds(lock: Path, ours: str) -> bool:
    """`<name>.lock` names a live runner of the previous version, which knows no flock: a pid written by
    anything but our own last runner (`ours`, which the flock says is gone), alive, and younger than any
    hook of that version lives (past LOCK_STALE_S a reboot or a pid wrap gave the dead runner's pid away)."""
    try:
        pid = lock.read_text().strip()
        return pid != ours and pid_alive(int(pid or "0")) and time.time() - lock.stat().st_mtime < LOCK_STALE_S
    except (OSError, ValueError):
        return False


def try_lock(name: str) -> bool:
    """True when this call is now the runner for `name`; a dead runner's lock, and its batch, are taken over.

    A runner holds a flock on `<name>.flock` for as long as it runs: the kernel frees it the moment that
    process ends, however it ends, so no sleep of the machine, pid reuse or reboot can make a live runner
    look dead, or a dead one alive. `<name>.lock` is the previous version's pid file, still taken, so a
    runner of either version keeps the other out of its snapshot during the rollout."""
    STATE.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE / f"{name}.flock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False  # a live runner holds it: the common case
    ours = os.read(fd, 32).decode(errors="replace").strip()  # the pid our last runner here wrote
    lock = STATE / f"{name}.lock"
    for _ in range(2):
        try:
            pid_file = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if previous_version_holds(lock, ours):
                break
            lock.unlink(missing_ok=True)  # its runner died (at the hook timeout, say): its batch is still in flight
            continue
        os.write(pid_file, str(os.getpid()).encode())
        os.close(pid_file)
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        HELD[name] = fd
        return True
    os.close(fd)  # frees the flock
    return False


def release(name: str) -> None:
    lock = STATE / f"{name}.lock"
    with contextlib.suppress(OSError, ValueError):
        if int(lock.read_text() or "0") == os.getpid():
            lock.unlink()  # before the flock goes, so the next runner of either version finds it free
    if (fd := HELD.pop(name, None)) is not None:
        os.close(fd)


def is_done(call: str, name: str) -> bool:
    return (DONE / f"{call}@{name}").exists()


def needed(name: str) -> float:
    """Seconds a pass of `name` needs before the deadline: its last pass's time, with room."""
    last = read_json(DURATIONS).get(name)
    typical = float(last) if isinstance(last, (int, float)) else 0.0
    return SLACK_S + min(RUN_TIMEOUT_S, max(MIN_PASS_S, 1.25 * typical))


def note_duration(name: str, seconds: float, *, at_least: bool) -> None:
    durations = read_json(DURATIONS)
    previous = durations.get(name)
    floor = float(previous) if at_least and isinstance(previous, (int, float)) else 0.0
    durations[name] = max(floor, seconds)
    write_json(DURATIONS, durations)


# ---------------------------------------------------------------- claims: who changed which file


def last_seen(session: str) -> float | None:
    try:
        return float((SEEN / session).read_text())
    except (OSError, ValueError):
        return None


def bash_window(payload: dict, session: str, now: float) -> tuple[float, float]:
    """The caller's command: its `duration_ms`, else since the session's previous call (at most ten minutes),
    and a second before for the CLI to start the hook. It ends at `now`: the hook starts after the command."""
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        return now - duration / 1000 - 1, now
    last = last_seen(session) if session != NOBODY else None
    if last is not None and last <= now:
        return max(last, now - FALLBACK_WINDOW_S) - 1, now
    return now - 1, now


def claimants(owner: object) -> list[str]:
    """The sessions that claimed a file exactly as it is: the first (its last owner), then each one whose
    command overlapped that change."""
    if not isinstance(owner, dict):
        return []
    joined = owner.get("with")
    named = [owner.get("session"), *(joined if isinstance(joined, list) else [])]
    return [s for s in dict.fromkeys(named) if isinstance(s, str) and s and safe_id(s) == s]


def note_claim(
    owners: dict, name: str, session: str, ctime: int | None, now: float, tool: tuple[int | None, bool] | None
) -> None:
    """Record `session`'s claim of the file as it is now; `tool`: (base, whole) of the Edit or Write that wrote it.
    A claim of the very state another claim holds (the same ctime) joins it: one change, claimed by its
    tool's session and by each command that overlapped it, so none of them claims it twice and a later
    change no session made goes to the first claimant, not to the latest reader."""
    held = owners.get(name)
    claimed = claimants(held) if isinstance(held, dict) and held.get("ctime") == ctime else []
    if claimed and tool is None and isinstance(held, dict):
        joined = list(dict.fromkeys([*claimed, session]))
        owners[name] = {**held, "session": joined[0], "with": joined[1:], "at": max(number(held.get("at"), now), now)}
        return
    base, whole = tool or (None, False)  # a tool claim leads: its session wrote this state
    others = [s for s in claimed if s != session]
    owners[name] = {"session": session, "ctime": ctime, "base": base, "whole": whole, "with": others, "at": now}


def left_by_another_tool(owner: object, session: str, ctime: int | None, start: float) -> bool:
    """The file is exactly as another session's tools left it, and this command can have written none of it:
    a Write, which chose every byte, or a run of Edits whose `base` is older than this window."""
    if not isinstance(owner, dict) or session in claimants(owner) or not claimants(owner) or ctime is None:
        return False
    base = owner.get("base")
    return owner.get("ctime") == ctime and (owner.get("whole") is True or (isinstance(base, int) and base / 1e9 < start))


def claimed_already(owner: object, session: str, ctime: int | None, at: float) -> bool:
    """This session's claim holds the file exactly as it is (a deletion: one no newer than that claim).
    Its change is queued under it already; a second claim (the `git diff` after an Edit) would only wake it twice."""
    if not isinstance(owner, dict) or session not in claimants(owner) or owner.get("ctime") != ctime:
        return False
    claimed = owner.get("at")
    return isinstance(claimed, (int, float)) and at <= claimed


SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def git_head() -> str | None:
    shown = git("rev-parse", "-q", "--verify", "HEAD")
    head = shown.stdout.decode(errors="replace").strip()
    return head if shown.returncode == 0 and SHA.fullmatch(head) else None


def rewritten_by_git(last: dict, head: str | None, now: float) -> list[tuple[str, float, str]]:
    """(tests namespace, change time, file) of each file in the diff of a HEAD move since HEAD was last seen
    (a pull, a checkout, a reset) whose ctime is after that look: one git rewrote, or one a command wrote
    and committed before any hook call saw it. A commit of a change a call already saw rewrites nothing:
    its files keep their ctime, which is older than that call's look."""
    old, seen_at = last.get("head"), last.get("at")
    if head is None or not isinstance(old, str) or old == head or not SHA.fullmatch(old) or not isinstance(seen_at, (int, float)):
        return []  # never seen, unchanged, or state that is not a commit id (it reaches git's arguments)
    diff = git("diff", "--name-only", "-z", "--no-renames", old, head)
    found = []
    for name in diff.stdout.decode(errors="replace").split("\0") if diff.returncode == 0 else []:
        path = repo_path(name)
        if path is not None and (at := file_times(path, now)[2]) > seen_at:
            found += [(f"tests {label(project)}", at, name) for project in projects_of(path, ROOT)]
    return found


def note_rewritten(rewritten: list[tuple[str, float, str]]) -> None:
    """A HEAD move's rewrites are changes no session made here: each is an unowned suspect of its project's
    tests. No pass runs for them (I3); a red that turns up there is told once, never pinned on one session.
    The caller holds the state lock. With no ledger yet, the first pass seeds instead."""
    if not rewritten or not LEDGER.exists():
        return
    ledger = load_ledger()
    for ns, at, name in rewritten:
        suspect(ledger, ns, NOBODY, at, name)
    write_json(LEDGER, ledger)


def last_owner(owners: dict, name: str) -> str:
    owner = owners.get(name)
    session = owner.get("session") if isinstance(owner, dict) else None
    return session if isinstance(session, str) and session and safe_id(session) == session else NOBODY


def landed_since_claimed(owners: dict, names: list[str]) -> dict[str, float]:
    """Of `names`, those a commit touched after their claim (with the claim's time): that change has
    landed, so it names no owner for a later one, even when no hook call saw the file clean in between
    (a commit from the operator's terminal)."""
    claimed = {n: float(o["at"]) for n in names if isinstance(o := owners.get(n), dict) and isinstance(o.get("at"), (int, float))}
    if not claimed:
        return {}
    since = int(min(claimed.values()))
    shown = git("log", f"--since={since}", "--format=%x01%ct", "--name-only", "--", *sorted(claimed))
    landed: dict[str, float] = {}
    committed = 0
    for line in shown.stdout.decode(errors="replace").splitlines():
        if line.startswith("\x01"):
            committed = int(line[1:]) if line[1:].isdigit() else 0
        # Whole seconds: a commit in the claim's own second may have come first, and the claim stands (toward
        # waking). A name git had to quote never matches here: its claim stands too, as before.
        elif line in claimed and committed > claimed[line]:
            landed[line] = claimed[line]
    return landed


def current_claims(owners: dict, dirty: set[str] | None, now: float, looked: float) -> dict:
    """The claims still worth an owner: younger than PRUNE_S and, when a Bash call knows the dirty set,
    on a file that is still dirty. A committed or reverted change names no owner for the next one. A claim
    made after `looked`, when git began to read the tree, is of a change that set may predate: it stands
    (a change is claimed only by a call that starts after it, and `at` is when that call started)."""
    return {
        name: owner
        for name, owner in owners.items()
        if isinstance(owner, dict)
        and isinstance(owner.get("at"), (int, float))
        and now - owner["at"] < PRUNE_S
        and (dirty is None or name in dirty or owner["at"] >= looked)
    }


def claim(payload: dict, session: str, call: str, now: float, deadline: float) -> set[str]:
    """Queue every change under its owner; the runners this call's own changes went to."""
    bash = payload.get("tool_name") == "Bash"
    start, end = bash_window(payload, session, now) if bash else (now, now)
    # Read before git looks: whatever moves after this, another call moved while git ran (kept below).
    stamped, gone_before = (read_json(STAMPS), read_json(GONE)) if bash else ({}, {})
    looked = time.time()
    dirty = dirty_files() if bash else None  # outside the lock: git is the slow part
    seen = {path: file_times(path, now) for path in dirty or []}
    reverted = reverts(stamped, seen, now) if dirty is not None else {}
    outside = [rel(p) for p, (mtime, _, at) in seen.items() if not start <= at <= end and stamped.get(rel(p)) != mtime]
    landed = landed_since_claimed(read_json(OWNERS), outside)
    head, last_head = git_head(), read_json(HEAD_SEEN)
    rewritten = rewritten_by_git(last_head, head, now)
    mine = call if session != NOBODY else NOBODY
    queued: list[Entry] = []
    with state_lock():
        owners = current_claims(read_json(OWNERS), None if dirty is None else {rel(p) for p in dirty}, now, looked)
        if head is not None:
            held_head = read_json(HEAD_SEEN)
            if held_head.get("head") == last_head.get("head"):  # no other call took this move first
                note_rewritten(rewritten)
            # Its latest look at this HEAD: a call that started earlier and locks later never sets it back.
            since = max(number(held_head.get("at"), now), now) if held_head.get("head") == head else now
            write_json(HEAD_SEEN, {"head": head, "at": since})
        if bash and dirty is not None:
            stamps, gone = read_json(STAMPS), read_json(GONE)
            fresh: dict[str, int] = {}
            still_gone: dict[str, float] = {}
            for path, (mtime, ctime, at) in [*seen.items(), *reverted.items()]:
                name = rel(path)
                if path in seen:  # the stamps hold what is dirty; a revert leaves them
                    fresh[name] = mtime
                    if mtime == -1:  # first seen gone now: no later than this call's own window can end
                        at = still_gone[name] = deleted_at(name, min(at, end), stamps, gone)
                if not in_repo(path) or claimed_already(owners.get(name), session, ctime, at):
                    continue
                if start <= at <= end and not left_by_another_tool(owners.get(name), session, ctime, start):
                    if path in seen:  # a revert is queued as the caller's too, but leaves its file clean: no claim to keep
                        note_claim(owners, name, session, ctime, now, None)
                    # From the window's start: a command that wrote the file early and again late changed it early.
                    queued.append(Entry(session, mine, name, start, at, os.getpid(), deadline))
                elif stamps.get(name) != mtime and (path in seen or name in stamps):  # no other call queued it yet
                    if name in landed and owners.get(name, {}).get("at") == landed[name]:
                        del owners[name]  # the claim git judged, and it has landed: this change is someone else's
                    owner = last_owner(owners, name)
                    queued.append(Entry(owner, NOBODY, name, at, at))
                    if owner != NOBODY:  # and maybe nobody's: told once, never pinned on that session
                        queued.append(Entry(NOBODY, NOBODY, name, at, at))
            # What another call stamped while git ran: a change our look may predate.
            fresh.update({n: v for n, v in stamps.items() if n not in fresh and stamped.get(n) != v})
            still_gone.update({n: v for n, v in gone.items() if n not in still_gone and gone_before.get(n) != v})
            write_json(STAMPS, fresh)
            write_json(GONE, still_gone)
        elif not bash:
            changed = [p for p in changed_by_edit(payload) if in_repo(p)]
            bases = record_edits(payload, changed, now, owners)
            ready = time.time()  # AUTHORED now holds what this call vouched for
            for path in changed:
                _, ctime, at = file_times(path, now)
                note_claim(owners, rel(path), session, ctime, now, bases.get(rel(path), (None, False)))
                queued.append(Entry(session, mine, rel(path), at, ready, os.getpid(), deadline))
        write_json(OWNERS, owners)
        enqueue(queued)
        if session != NOBODY:
            SEEN.mkdir(parents=True, exist_ok=True)
            (SEEN / session).write_text(str(now))
    return {r.name for e in queued if e.call != NOBODY for r in runners_for(ROOT / e.path)}


# ---------------------------------------------------------------- signatures


@dataclass
class Record:
    """One check's verdict on one batch. Only what a late change that joins it needs is kept on disk; the
    rest serves the pass that made it."""

    title: str  # the report block's first line
    check: str  # `tests <project>`, also its namespace, or which purity scan
    reds: dict[str, str | None]  # signature -> the file it is about, or None: about the whole batch
    authoritative: bool  # it ran completely enough that what it does not report is green
    lines: list[str]  # what the report shows; purity lines are filtered per session
    scope: frozenset[str] = field(default_factory=frozenset)  # the namespaces it speaks for
    counts: dict[str, int] = field(default_factory=dict)  # signature -> how many of it (1 when absent)
    hides: frozenset[str] = frozenset()  # its signatures that name no single failure: others can stand behind them
    unloaded: frozenset[str] | None = None  # complete but for these test files, which failed to load

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "check": self.check,
            "reds": self.reds,
            "authoritative": self.authoritative,  # a late change that joins this verdict is a suspect when it is not
            "lines": self.lines,
        }

    @staticmethod
    def parse(data: object) -> Record | None:
        if not isinstance(data, dict):
            return None
        title, check, reds, lines = data.get("title"), data.get("check"), data.get("reds"), data.get("lines")
        if not (isinstance(title, str) and isinstance(check, str) and isinstance(reds, dict) and isinstance(lines, list)):
            return None
        kept = {s: a for s, a in reds.items() if isinstance(s, str) and (a is None or isinstance(a, str))}
        return Record(
            title,
            check,
            kept,
            data.get("authoritative") is True,  # absent or odd: it vouched for nothing, toward keeping suspects
            [line for line in lines if isinstance(line, str)],
        )

    def speaks_for(self, signature: str) -> bool:
        """It ran what `signature` names: all of it when complete, else every test file but those that failed to load."""
        if self.authoritative:
            return namespace(signature) in self.scope
        file = file_of(signature)
        return self.unloaded is not None and namespace(signature) in self.scope and file is not None and file not in self.unloaded

    def only(self, files: set[str]) -> Record:
        """This purity verdict as it speaks of `files` alone: their signatures, and their hit lines."""
        reds: dict[str, str | None] = {s: a for s, a in self.reds.items() if a in files}
        lines = [line for line in self.lines if (hit := purity_hit(line)) is not None and hit[0] in files]
        return Record(self.title, self.check, reds, self.authoritative, lines)


def namespace(signature: str) -> str:
    return signature.split(" | ", 1)[0]


PURITY_HAZARD = re.compile(r"^HAZARD (?P<path>.+?)(?::\d+)?: (?P<what>[^:]+?)(?::(?: (?P<text>.*))?)?$")
PURITY_RATCHET = re.compile(r"^RATCHET (?P<path>.+?): (?P<count>\d+) private word")


def purity_hit(line: str) -> tuple[str, str, int] | None:
    """(file, what, how many) of a purity report line. `tests/test_purity.py` prints a Hit as
    `path[:line]: rule: text`, its secret masked: one file can hold two hits of a rule, so a hit is its
    rule and a digest of that masked line, which moves with no line number. Private words come as a count."""
    if found := PURITY_HAZARD.match(line):
        text = found["text"] or ""
        return found["path"], f"{found['what']} | {hashlib.sha256(text.encode()).hexdigest()[:8]}", 1
    if found := PURITY_RATCHET.match(line):
        return found["path"], "private words", int(found["count"])
    return None


def catch_all(ran: Ran) -> str:
    return "timed out" if ran.timed_out else f"exit {ran.rc}"


def purity_record(ran: Ran, title: str, check: str, scope: frozenset[str]) -> Record:
    """`purity <file> | <rule> | <digest>` per hit, `purity <file> | private words` per file with too many;
    a failure that names no file is `purity | exit N`."""
    lines = ran.lines()
    reds: dict[str, str | None] = {}
    counts: dict[str, int] = {}
    for line in lines:
        if hit := purity_hit(line):
            signature = f"purity {hit[0]} | {hit[1]}"
            reds[signature] = hit[0]
            counts[signature] = counts.get(signature, 0) + hit[2]
    complete = ran.rc == 0 or (ran.rc == 1 and bool(reds))
    hides: frozenset[str] = frozenset()
    if not ran.ok and not complete:
        hidden = f"purity | {catch_all(ran)}"
        reds[hidden] = None
        hides = frozenset({hidden})
    return Record(title, check, reds, complete, lines, scope, counts, hides)


PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (?P<id>\S.*?)(?: - .*)?$")
NODE_AT = re.compile(r"^test at (?P<file>.+?):\d+:\d+$")
NODE_FAILED = re.compile(r"^\s*✖ (?P<name>.+?)(?: \(\d[\d.]*m?s\))?$")
TAP_FAILED = re.compile(r"^\s*not ok \d+ - (?P<name>.+?)(?: # .*)?$")


def failed_tests(lines: list[str]) -> list[str]:
    """Test ids from pytest's short summary, node's `failing tests:` list (spec), or TAP `not ok`."""
    if any(line.startswith("=") and "short test summary info" in line for line in lines):
        start = next(i for i, line in enumerate(lines) if "short test summary info" in line)
        return [m["id"] for line in lines[start:] if (m := PYTEST_FAILED.match(line))]
    if "✖ failing tests:" in lines:
        found, where = [], ""
        for line in lines[lines.index("✖ failing tests:") + 1 :]:
            if at := NODE_AT.match(line):
                where = at["file"]
            elif (failed := NODE_FAILED.match(line)) and not line.startswith(" "):
                found.append(f"{where} > {failed['name']}" if where else failed["name"])
        return found
    named = [m["name"] for line in lines if (m := NODE_FAILED.match(line) or TAP_FAILED.match(line))]
    return [name for name in named if name != "failing tests:"]


def whole_file(name: str) -> bool:
    """A failed test id that is a whole test file (it failed to load or collect): its own tests never ran."""
    where, _, test = name.partition(" > ")
    if test:
        return test == where  # node's spec reporter names a file that threw at load by its path
    base = name.rsplit("/", 1)[-1]
    patterns = [p for kind in _projects.TEST_FILES.values() for p in kind]
    return "::" not in name and any(fnmatch.fnmatch(base, p) for p in patterns)  # pytest `ERROR f.py`, TAP


def file_of(signature: str) -> str | None:
    """The test file a tests signature names (`file > test`, `file::test`, or a whole file); None when none."""
    rest = signature.split(" | ", 1)[1] if " | " in signature else ""
    for mark in (" > ", "::"):
        if mark in rest:
            return rest.split(mark, 1)[0]
    return rest if rest and whole_file(rest) else None


def tests_record(project: ProjectT, ran: Ran) -> Record:
    """`tests <project> | <test>` per failed test; a failure that names none is `tests <project> | exit N`.
    A test file that failed to load hides its own tests, and every other file still speaks for its own."""
    lines = ran.lines()
    where = f"tests {label(project)}"
    names = [] if ran.ok or ran.timed_out else list(dict.fromkeys(failed_tests(lines)))
    interrupted = any(line.startswith("!") and "Interrupted:" in line for line in lines)
    reds: dict[str, str | None] = {f"{where} | {name}": None for name in names}
    hides = {f"{where} | {name}" for name in names if whole_file(name)}
    complete = ran.rc == 0 or (bool(names) and not interrupted and not hides)
    unloaded = None
    if hides and not interrupted:
        unloaded = frozenset(file for file in map(file_of, hides) if file is not None)
    if not ran.ok and not names:
        reds[f"{where} | {catch_all(ran)}"] = None
        hides.add(f"{where} | {catch_all(ran)}")
    title = f"{project.kind} tests ({rel(project.path)}) FAILED"
    return Record(title, where, reds, complete, lines[-TAIL_LINES:], frozenset({where}), {}, frozenset(hides), unloaded)


def skipped_record(project: ProjectT) -> Record:
    """A project skipped as untrusted: it judges nothing, and its batch's sessions become suspects."""
    where = f"tests {label(project)}"
    return Record(f"{project.kind} tests ({rel(project.path)}) skipped", where, {}, False, [], frozenset({where}))


# ---------------------------------------------------------------- a pass


@dataclass
class Verdict:
    """A finished pass: what it found, and which changes it saw."""

    records: list[Record]
    began: float  # the pass began reading the tree: a change before this is in what it saw...
    read: float  # ...and one after it finished reading is not
    covers: dict[str, float] | None  # the files it read, each with when its scan began; None: a whole project, or tree
    seconds: float = 0.0

    def saw(self, entry: Entry) -> bool:
        if self.covers is None:
            return entry.ready <= self.began
        return entry.path in self.covers and entry.ready <= self.covers[entry.path]

    def joined(self, entries: list[Entry]) -> list[Record]:
        """The records late changes join: a scan of files speaks of theirs alone."""
        return self.records if self.covers is None else [r.only({e.path for e in entries}) for r in self.records]

    def to_json(self) -> dict:
        return {"began": self.began, "read": self.read, "covers": self.covers, "records": [r.to_json() for r in self.records]}

    @staticmethod
    def parse(data: dict) -> Verdict | None:
        began, read, covers, records = data.get("began"), data.get("read"), data.get("covers"), data.get("records")
        if not (isinstance(began, (int, float)) and isinstance(read, (int, float)) and isinstance(records, list)):
            return None
        if covers is not None and not (
            isinstance(covers, dict) and all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in covers.values())
        ):
            return None
        return Verdict([r for r in map(Record.parse, records) if r is not None], float(began), float(read), covers)


def remembered(last: Verdict | None, verdict: Verdict) -> Verdict:
    """What a late change joins from now on. A runner that reads a whole project or the tree keeps its last
    pass. The changed files' scan keeps each file's latest complete scan, so a late claim of a file any
    recent pass read joins that pass instead of scanning it again (I3); a scan that crashed adds nothing."""
    if verdict.covers is None:
        return verdict
    cutoff = time.time() - PRUNE_S
    old = last if last is not None and last.covers is not None else None
    covers = {f: t for f, t in (old.covers.items() if old and old.covers else []) if t > cutoff}
    parts = [old.records[0].only(set(covers))] if old and old.records else []
    if verdict.records and verdict.records[0].authoritative:
        covers.update(verdict.covers)
        parts = [part.only(set(covers) - set(verdict.covers)) for part in parts] + [verdict.records[0].only(set(verdict.covers))]
    if not parts:
        return Verdict([], verdict.began, verdict.read, covers)
    merged = Record(
        parts[-1].title,
        parts[-1].check,
        {s: a for p in parts for s, a in p.reds.items()},
        True,
        [x for p in parts for x in p.lines],
    )
    return Verdict([merged], verdict.began, verdict.read, covers)


def run_pass(runner: Runner, batch: list[Entry], deadline: float) -> Verdict | None:
    """One pass over the batch; None when the deadline cut it short (the batch stays in flight)."""
    began = time.time()
    timeout = min(RUN_TIMEOUT_S, deadline - began - SLACK_S)
    files = sorted({e.path for e in batch})
    if runner.check == "tests":
        tested = run_tests(runner.project, runner.name, timeout)
        if tested is None:
            return Verdict([], began, time.time(), None, time.time() - began)
        ran, began, read = tested
        if ran is None:
            return Verdict([skipped_record(runner.project)], began, read, None, time.time() - began)
        records = [tests_record(runner.project, ran)]  # a green record too: it is what clears its namespace
    elif runner.check == "files" and not files:
        return Verdict([], began, began, {})
    else:
        root = runner.check == "root"
        shipped = git_paths("ls-files", "-z", "--cached", "--others", "--exclude-standard") if root else files
        scope = frozenset({"purity", *(f"purity {name}" for name in shipped)})
        ran = run(purity() if root else purity("--files", *(str(ROOT / f) for f in files)), ROOT, timeout=timeout)
        read = time.time()  # purity reads the live tree for as long as it runs
        title = "purity (root) FAILED" if root else "purity FAILED for the changed files"
        records = [purity_record(ran, title, "purity (root)" if root else "purity --files", scope)]
    if ran.timed_out and timeout < RUN_TIMEOUT_S:
        return None
    return Verdict(records, began, read, None if runner.check != "files" else dict.fromkeys(files, began), time.time() - began)


def changes(entries: list[Entry], about: str | None) -> list[tuple[str, float, float]]:
    """(owner, earliest, ready) of each change a signature is about: all of them for one about the whole
    batch, else the changes to its file. `-` too: a change no session made can turn a signature red."""
    return [(e.owner, e.t, e.ready) for e in entries if about is None or e.path == about]


def made(entries: list[Entry], about: str | None) -> set[str]:
    """The sessions whose own calls claimed a change a signature is about. One queued under a session only as
    its file's last claimant (a change made outside every window) is a guess: it can make that session an
    owner, and so tell it once, but never tells an owner again."""
    return {e.owner for e in entries if e.call != NOBODY and (about is None or e.path == about)}


def suspect_namespace(record: Record, entry: Entry) -> str:
    return record.check if record.check.startswith("tests ") else f"purity {entry.path}"


def vouched_for(ledger: dict, signature: str) -> float | None:
    """When the latest run that saw what `signature` names report began: a complete one, or one whose only
    gap was other test files that failed to load. None: no such run yet."""
    ns = namespace(signature)
    full, partial, file = ledger["vouched"].get(ns), ledger["partial"].get(ns), file_of(signature)
    if partial is not None and file is not None and file not in partial["unloaded"]:
        return max(full or 0.0, partial["began"])
    return full


def judge(ledger: dict, record: Record, entries: list[Entry], began: float, read: float, create: bool) -> dict[str, list[str]]:
    """The ledger rule: {session: the signatures it is woken for}. `create` is False when changes join
    a finished pass's verdict: they can co-own what is still red, never open or clear anything."""
    reds: dict = ledger["reds"]
    seeding = create and record.check.startswith("tests ") and bool(entries) and all(e.ready < ledger["created"] for e in entries)
    woken: dict[str, list[str]] = {}
    for signature, about in record.reds.items():
        found = changes(entries, about)
        known = reds.get(signature)
        held = ledger["suspects"].get(namespace(signature), {})
        candidates = found + [(owner, s["ready"], s["ready"]) for owner, s in held.items()]
        count = record.counts.get(signature, 1)
        if known is None:
            if not create:
                continue
            vouched = vouched_for(ledger, signature)
            after = vouched or 0.0
            owners = set() if seeding else {o for o, _, ready in candidates if ready > after}
            if signature.startswith("tests ") and vouched is None:
                owners.add(NOBODY)  # no run ever saw it green: it may be older than every change here
            reds[signature] = {
                "owners": sorted(owners or {NOBODY}),
                "first_red_end": read,
                "last_red": read,
                "after": after,
                "count": count,
            }
            wake = owners
        else:
            had = set(known["owners"])
            plausible = {o for o, t, ready in found if t <= known["first_red_end"] and ready > known["after"]}
            if create:
                if count > known.get("count", 1):  # one more of it: this batch's changes there made it
                    plausible |= {o for o, _, ready in found if ready > known["after"]}
                elif signature in record.hides and had & made(entries, about):
                    # An owner's change left it red: what stands may be a later change's, hidden behind it.
                    if hidden := {o for o, _, ready in candidates if ready > known["after"]} - had:
                        plausible |= hidden | {NOBODY}
                known["last_red"] = max(known["last_red"], read)
                known["count"] = count
            known["owners"] = sorted(had | plausible)
            # Told on becoming an owner; told again on a later change only while it is the one plausible culprit.
            again = made(entries, about) & had if NOBODY not in had and len(had) == 1 else set()
            wake = (plausible - had) | again
        for session in sorted(wake - {NOBODY}):
            woken.setdefault(session, []).append(signature)
    if create:
        for signature in [s for s, e in reds.items() if s not in record.reds and record.speaks_for(s) and e["last_red"] < began]:
            del reds[signature]
    return woken


def suspect(ledger: dict, ns: str, owner: str, ready: float, path: str) -> None:
    """Keep `owner` as a suspect of `ns`: a change of its there that no complete run has checked."""
    mine = ledger["suspects"].setdefault(ns, {}).setdefault(owner, {"ready": ready, "files": []})
    mine["ready"] = max(mine["ready"], ready)
    if path not in mine["files"] and len(mine["files"]) < MAX_SUSPECT_FILES:
        mine["files"].append(path)


def vouch(ledger: dict, record: Record, entries: list[Entry], began: float, create: bool) -> None:
    """After judging: a complete run vouches for its namespaces (a project's tests, from when it began
    reading) and spends their suspects; one that could not see everything keeps its batch's owners
    (`-` too) as suspects there. A change that joins a finished verdict (`create` False) vouches for
    nothing anew, and is kept as a suspect when that verdict could not see everything either."""
    suspects: dict = ledger["suspects"]
    if record.authoritative:
        if not create:
            return
        if record.check.startswith("tests "):
            ledger["vouched"][record.check] = began
            ledger["partial"].pop(record.check, None)
        for ns in [ns for ns in suspects if ns in record.scope]:
            # Only what it could see: a change ready after it began reading (a pull mid-pass) is not in its copy.
            if unseen := {owner: s for owner, s in suspects[ns].items() if s["ready"] > began}:
                suspects[ns] = unseen
            else:
                del suspects[ns]
        return
    if create and record.unloaded is not None:  # it saw every test file but these: each other one vouches from here
        ledger["partial"][record.check] = {"began": began, "unloaded": sorted(record.unloaded)}
    for entry in entries:
        suspect(ledger, suspect_namespace(record, entry), entry.owner, entry.ready, entry.path)


def new_ledger() -> dict:
    """An empty red ledger. A batch of changes all made before `created` seeds it (the WHO GETS WOKEN note)."""
    return {"created": time.time(), "reds": {}, "vouched": {}, "partial": {}, "suspects": {}}


def number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def load_ledger() -> dict:
    """The red ledger, read as untrusted: a bad entry is dropped, a suspect older than PRUNE_S expires."""
    if not LEDGER.exists():
        return new_ledger()
    data = read_json(LEDGER)
    ledger: dict = {"created": number(data.get("created"), 0.0), "reds": {}, "vouched": {}, "partial": {}, "suspects": {}}
    raw = data.get("reds")
    for signature, entry in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("owners"), list):
            continue
        first = entry.get("first_red_end")
        if isinstance(first, (int, float)):
            ledger["reds"][signature] = {
                "owners": [o for o in entry["owners"] if isinstance(o, str)] or [NOBODY],
                "first_red_end": float(first),
                "last_red": number(entry.get("last_red"), float(first)),
                "after": number(entry.get("after"), 0.0),
                "count": int(number(entry.get("count"), 1.0)),
            }
    vouched = data.get("vouched")
    for ns, began in (vouched if isinstance(vouched, dict) else {}).items():
        if isinstance(began, (int, float)):
            ledger["vouched"][ns] = float(began)
    partial = data.get("partial")
    for ns, seen in (partial if isinstance(partial, dict) else {}).items():
        unloaded = seen.get("unloaded") if isinstance(seen, dict) else None
        if isinstance(seen, dict) and isinstance(seen.get("began"), (int, float)) and isinstance(unloaded, list):
            ledger["partial"][ns] = {"began": float(seen["began"]), "unloaded": [f for f in unloaded if isinstance(f, str)]}
    cutoff = time.time() - PRUNE_S
    suspects = data.get("suspects")
    for ns, held in (suspects if isinstance(suspects, dict) else {}).items():
        kept = {}
        for owner, mine in (held if isinstance(held, dict) else {}).items():
            ready = mine.get("ready") if isinstance(mine, dict) else None
            files = mine.get("files") if isinstance(mine, dict) else None
            if safe_id(owner) == owner and isinstance(ready, (int, float)) and ready > cutoff and isinstance(files, list):
                kept[owner] = {"ready": float(ready), "files": [f for f in files if isinstance(f, str)]}
        if kept:
            ledger["suspects"][ns] = kept
    return ledger


def report_block(record: Record, mine: list[str]) -> dict:
    """This session's signatures, how many others are red here, and the output that shows them."""
    others = len(record.reds) - len(mine)
    shown = "; ".join(mine[:MAX_SIGNATURES]) + (f"; and {len(mine) - MAX_SIGNATURES} more" if len(mine) > MAX_SIGNATURES else "")
    head = [record.title, f"yours: {shown}"] + ([f"also red here, not yours: {others}"] if others else [])
    lines = record.lines
    if record.check.startswith("purity"):  # a purity line names its file: show this session only its own
        files = {record.reds.get(s) for s in mine}
        lines = [line for line in lines if (hit := purity_hit(line)) is None or hit[0] in files]
    return {"title": record.title, "mine": mine, "text": "\n".join(head + lines[-TAIL_LINES:])}


def post_mail(session: str, name: str, files: list[str], blocks: list[dict]) -> None:
    box = MAIL / session
    box.mkdir(parents=True, exist_ok=True)
    stem = f"{time.time_ns()}-{os.getpid()}-{name}"
    tmp = box / f".{stem}.tmp"
    tmp.write_text(json.dumps({"files": files, "blocks": blocks}))
    os.replace(tmp, box / f"{stem}.json")  # whole or absent: a delivery never reads half a report


def settle_batch(
    name: str,
    records: list[Record],
    entries: list[Entry],
    began: float,
    read: float,
    create: bool,
    hold: frozenset[str] = frozenset(),
) -> None:
    """Judge, then mail each woken session, then mark each call done, but those in `hold`: they have
    changes in this batch a later pass judges, and are done only then. The caller holds the state lock."""
    ledger = load_ledger()
    suspected = {o: m["files"] for held in ledger["suspects"].values() for o, m in held.items()}  # before vouch spends them
    woken: dict[str, dict[int, list[str]]] = {}
    for index, record in enumerate(records):
        for session, signatures in judge(ledger, record, entries, began, read, create).items():
            woken.setdefault(session, {})[index] = signatures
    for record in records:
        vouch(ledger, record, entries, began, create)
    if create or LEDGER.exists():
        write_json(LEDGER, ledger)
    for session, by_record in woken.items():
        files = list(dict.fromkeys(e.path for e in entries if e.owner == session)) or suspected.get(session, [])
        post_mail(session, name, files, [report_block(records[i], s) for i, s in sorted(by_record.items())])
    DONE.mkdir(parents=True, exist_ok=True)
    for call in {e.call for e in entries if e.call != NOBODY} - hold:
        (DONE / f"{call}@{name}").touch()


def prune() -> None:
    """Mail nobody collected, done marks nobody read, and last-seen times older than PRUNE_S."""
    cutoff = time.time() - PRUNE_S
    for path in [*DONE.glob("*"), *MAIL.glob("*/*"), *SEEN.glob("*")]:
        with contextlib.suppress(OSError):
            if path.lstat().st_mtime < cutoff:
                path.unlink()
    for box in MAIL.glob("*"):
        with contextlib.suppress(OSError):
            box.rmdir()  # only an empty one goes


def live_waiter(name: str) -> bool:
    """Another call waits on this queue and has the time to run it: it reports sooner than our next pass."""
    need, now = needed(name), time.time()
    return any(
        e.call != NOBODY and e.pid not in (0, os.getpid()) and e.deadline - now >= need and pid_alive(e.pid)
        for e in read_entries(QUEUE / f"{name}.jsonl")
    )


def drain(name: str, call: str, deadline: float) -> None:
    """Hold `name`'s lock (the caller took it) and test its queue, pass by pass."""
    pending = STATE / f"{name}.pending"  # rollout: a call of the previous version found this runner busy
    inflight = INFLIGHT / f"{name}.jsonl"
    try:
        while True:
            rerun = pending.exists()
            pending.unlink(missing_ok=True)
            with state_lock():
                entries = take_batch(name)
                last = Verdict.parse(read_json(VERDICTS / f"{name}.json"))
                seen = [e for e in entries if last and last.saw(e)]
                fresh = [e for e in entries if not (last and last.saw(e))]
                if last and seen:
                    hold = frozenset(e.call for e in fresh)  # a call is done once ALL its changes here are judged
                    settle_batch(name, last.joined(seen), seen, last.began, last.read, create=False, hold=hold)
                write_entries(inflight, fresh)
            if not fresh and not rerun:
                return
            runner = runner_named(name, [e.path for e in fresh])
            if runner is None:
                with state_lock():
                    now = time.time()
                    settle_batch(name, [], fresh, now, now, create=False)  # the project is gone: nothing to run
                    inflight.unlink(missing_ok=True)
                continue
            if deadline - time.time() < needed(name):
                return  # stays in flight for the next runner
            started = time.time()
            verdict = run_pass(runner, fresh, deadline)
            with state_lock():
                if verdict is None:
                    note_duration(name, time.time() - started, at_least=True)  # a floor: the next runner allows more
                    return  # cut short by the deadline: stays in flight for the next runner
                settle_batch(name, verdict.records, fresh, verdict.began, verdict.read, create=True)
                write_json(VERDICTS / f"{name}.json", remembered(last, verdict).to_json())
                note_duration(name, verdict.seconds, at_least=False)
                inflight.unlink(missing_ok=True)
                prune()
            if (call == NOBODY or is_done(call, name)) and live_waiter(name):
                return  # hand over: that waiter delivers its own report sooner
    finally:
        release(name)


def settle(waiting: set[str], call: str, deadline: float) -> None:
    """Wait for this call's runners, taking over any that frees; with none to wait for, drain what is queued."""
    if not waiting:
        for name in names_with_work():
            if deadline - time.time() >= needed(name) and try_lock(name):
                drain(name, NOBODY, deadline)
        return
    while True:
        waiting = {name for name in waiting if not is_done(call, name)}
        if not waiting or time.time() >= deadline:
            return
        for name in sorted(waiting):
            if deadline - time.time() >= needed(name) and try_lock(name):
                drain(name, call, deadline)
        if any(not is_done(call, name) for name in waiting):
            time.sleep(POLL_S)


def deliver(session: str) -> int:
    """Print and clear this session's mail; exit 2 wakes it. No mail, exit 0. A check's block shows once:
    one that a later block of the same check repeats (a fold and a pass can both report it) is dropped,
    and so is one whose every failure a complete run has seen fixed since (it waited for this call)."""
    if session == NOBODY:
        return 0
    posted = sorted((MAIL / session).glob("*.json"))  # by name: oldest first
    # Listed before the ledger is read: a report is posted after its ledger write, so this read sees its reds.
    standing = set(load_ledger()["reds"]) if LEDGER.exists() else None
    files: list[str] = []
    blocks: list[tuple[str, set[str], str]] = []
    for path in posted:
        taken = path.with_name(f".{path.stem}.{os.getpid()}.taken")
        try:
            os.rename(path, taken)  # one call of the session takes each report, once
        except OSError:
            continue
        mail = read_json(taken)
        taken.unlink(missing_ok=True)
        named, written = mail.get("files"), mail.get("blocks")
        kept = False
        for block in written if isinstance(written, list) else []:
            title, mine, text = (block.get(k) for k in ("title", "mine", "text")) if isinstance(block, dict) else (None,) * 3
            if isinstance(title, str) and isinstance(mine, list) and isinstance(text, str):
                signatures = {m for m in mine if isinstance(m, str)}
                if standing is None or signatures & standing:
                    blocks.append((title, signatures, text))
                    kept = True
        files += [f for f in named if isinstance(f, str)] if kept and isinstance(named, list) else []
    shown = [
        text for i, (title, mine, text) in enumerate(blocks) if not any(t == title and mine <= m for t, m, _ in blocks[i + 1 :])
    ]
    if not shown:
        return 0
    names = ", ".join(list(dict.fromkeys(files))[:6]) or "your changes"
    print(f"After editing {names}:\n\n" + "\n\n".join(shown), file=sys.stderr)
    return 2


def show_red() -> int:
    """`--red`: every red signature, its owners and how long it has been red; then the suspects."""
    ledger = load_ledger() if LEDGER.exists() else new_ledger()
    reds = ledger["reds"]
    now = time.time()

    def who(owners: Iterable[str]) -> str:
        return ", ".join("- (unowned)" if o == NOBODY else o for o in sorted(owners))

    for signature, entry in sorted(reds.items()):
        age = max(0, int(now - entry["first_red_end"]))
        since = f"{age // 3600} h" if age >= 3600 else f"{age // 60} min" if age >= 60 else f"{age} s"
        print(f"{signature}\n    owners: {who(entry['owners'])}; red for {since}")
    unowned = sum(1 for e in reds.values() if e["owners"] == [NOBODY])
    print(f"{len(reds)} red signature(s), {unowned} unowned")
    for ns, held in sorted(ledger["suspects"].items()):
        print(f"suspects of {ns} (no complete run has checked their changes): {who(held)}")
    if waiting := names_with_work():
        print(f"queued or in flight: {', '.join(waiting)}")
    return 0


def main(argv: list[str]) -> int:
    began = STARTED
    if "--red" in argv:
        return show_red()
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if "--pre" in argv:
        take_pre_hash(payload)
        return 0  # the PreToolUse half never blocks an edit
    session = safe_id(payload.get("session_id")) or NOBODY
    call = safe_id(payload.get("tool_use_id")) or f"pid{os.getpid()}-{uuid.uuid4().hex[:12]}"
    deadline = began + DEADLINE_S
    waiting = claim(payload, session, call, began, deadline)
    if waiting or session == NOBODY or not any((MAIL / session).glob("*.json")):
        settle(waiting, call, deadline)  # mail left for this session goes out now; the queue keeps for the next call
    for name in waiting:
        (DONE / f"{call}@{name}").unlink(missing_ok=True)
    return deliver(session)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
