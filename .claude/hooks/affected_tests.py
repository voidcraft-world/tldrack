#!/usr/bin/env python3
"""PostToolUse(Edit|Write|MultiEdit|NotebookEdit|Bash): test what the edit touched.
PreToolUse(Edit|MultiEdit|NotebookEdit) with `--pre`: hash the file before the edit.

For each changed file it runs two checks, and wakes the agent (exit 2, the report
on stderr) only when one fails:

1. The purity scan of that file alone (`tests/test_purity.py --files`), so a home
   path or a private word is caught the moment it is written, not at CI.
2. The tests of the project the file belongs to (`scripts/projects.py` owns what a
   project is and how it is tested). A file outside every project belongs to the
   root, whose test is the purity test itself.

Bash is in the matcher because a file edited through the shell (`sed -i`, a
heredoc, a codegen script) sends no `file_path`: the hook then takes every TRACKED
file whose mtime moved since its last stamp. Without that, a whole change made
through the shell would go unchecked.

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

One runner per project at a time. A call that finds a project busy leaves a
`pending` mark and exits; the runner sees the mark and runs that project once more,
re-checking trust and re-copying first, so the last edit is always the one tested.
Stdlib only. The PostToolUse half is wired async with rewake in
`.claude/settings.json`: it never blocks an edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

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


def changed_by_shell() -> list[Path]:
    """Tracked dirty files whose mtime differs from the last stamp; the stamps move to now."""
    listed = iter(git("status", "--porcelain", "-z", "--untracked-files=no").stdout.decode().split("\0"))
    dirty = []
    for entry in listed:
        if len(entry) <= 3:
            continue
        dirty.append(ROOT / entry[3:])
        if entry[0] in "RC":
            next(listed, None)  # -z prints a rename's source as its own field: skip it
    stamps = read_json(STAMPS)
    moved, fresh = [], {}
    for path in dirty:
        try:
            mtime = path.lstat().st_mtime_ns
        except OSError:
            mtime = -1  # deleted: still a change to its project
        fresh[rel(path)] = mtime
        if stamps.get(rel(path)) != mtime:
            moved.append(path)
    write_json(STAMPS, fresh)
    return moved


# ---------------------------------------------------------------- what the agent wrote


def pre_record(path: Path) -> Path:
    return PRE / f"{hashlib.sha256(rel(path).encode()).hexdigest()[:32]}.json"


def take_pre_hash(payload: dict) -> None:
    """PreToolUse: the file's hash before an Edit, for record_edits to judge the result by."""
    for path in [p for p in changed_by_edit(payload) if in_repo(p)]:
        write_json(pre_record(path), {"path": rel(path), "sha": sha256_of(path)})


def index_sha(name: str) -> str | None:
    shown = git("show", f":{name}")
    return hashlib.sha256(shown.stdout).hexdigest() if shown.returncode == 0 else None


def wrote_whole(payload: dict, path: Path, authored: dict) -> bool:
    """Did the agent determine this file's whole content? See WHAT IT WILL EXECUTE."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if payload.get("tool_name") == "Write":
        content = tool_input.get("content")
        return isinstance(content, str) and hashlib.sha256(content.encode()).hexdigest() == sha256_of(path)
    record = pre_record(path)
    before = read_json(record).get("sha")
    record.unlink(missing_ok=True)
    if not isinstance(before, str):
        return False  # no snapshot (a new file, or the PreToolUse half did not run): fail closed
    return before == authored.get(rel(path)) or before == index_sha(rel(path))


def record_edits(payload: dict, paths: list[Path]) -> None:
    """Move each file's stamp so Bash skips it, and keep its hash only if the agent wrote it whole."""
    stamps, authored = read_json(STAMPS), read_json(AUTHORED)
    for path in paths:
        try:
            stamps[rel(path)] = path.lstat().st_mtime_ns
        except OSError:
            continue
        digest = sha256_of(path)
        if digest and wrote_whole(payload, path, authored):
            authored[rel(path)] = digest
        else:
            authored.pop(rel(path), None)
    write_json(STAMPS, stamps)
    write_json(AUTHORED, authored)


# ---------------------------------------------------------------- the snapshot


def manifest_trusted(project: Project, authored: dict, tracked: set[str], copy: Path) -> bool:
    """Tracked (by its exact path), and the COPY's bytes are the index's or the agent's own write."""
    manifest = rel(project.manifest)
    if manifest not in tracked or (copy / manifest).is_symlink():
        return False
    digest = sha256_of(copy / manifest)
    return digest is not None and digest in {index_sha(manifest), authored.get(manifest)}


def no_stray_node_modules(project: Project, tracked: set[str]) -> bool:
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


def run(command: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(CACHE / "pycache"), **(extra_env or {})}
    try:
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except FileNotFoundError:
        return True, ""  # the tool is not installed here; CI still runs it
    except subprocess.TimeoutExpired:
        return False, f"timed out after {RUN_TIMEOUT_S} s"
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-TAIL_LINES:])
    return done.returncode == 0, tail


def purity(*args: str) -> list[str]:
    return [sys.executable, "-I", "-B", str(ROOT / "tests" / "test_purity.py"), *args]


def acquire(name: str) -> bool:
    """True when this call is the runner for `name`; otherwise it leaves a pending mark."""
    lock = STATE / f"{name}.lock"
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            os.kill(int(lock.read_text() or "0"), 0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            lock.unlink(missing_ok=True)  # its runner died at the hook timeout
            return acquire(name)
        (STATE / f"{name}.pending").touch()
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def run_once(project: Project | None, name: str) -> tuple[bool, str, str] | None:
    """One run: the root's purity scan, or the project's tests in its snapshot. None = skipped."""
    if project is None:
        return (*run(purity(), ROOT), "purity (root)")
    authored = read_json(AUTHORED)
    snapshot = SNAPSHOTS / name
    tracked = sync_snapshot(snapshot, authored)
    # Judged on every run, never only the first, and on the copy's bytes: what is checked is what runs.
    if not manifest_trusted(project, authored, tracked, snapshot):
        return None
    if project.kind == "node" and not no_stray_node_modules(project, tracked):
        return None
    copy = Project(project.kind, snapshot / rel(project.path))
    if not copy.has_tests():
        return None  # nothing to run: owned by scripts/projects.py — see the note there.
    extra = {"UV_PROJECT_ENVIRONMENT": str(VENVS / name)} if project.kind == "python" else None
    return (*run(copy.test_command(), copy.path, extra), f"{project.kind} tests ({rel(project.path)})")


def run_project(project: Project | None) -> str | None:
    """The project's tests, re-run while edits keep arriving; the failure text, or None."""
    name = "root" if project is None else f"{project.kind}--{rel(project.path).replace('/', '__')}"
    if not acquire(name):
        return None
    pending = STATE / f"{name}.pending"
    try:
        while True:
            pending.unlink(missing_ok=True)
            result = run_once(project, name)
            if not pending.exists():
                if result is None or result[0]:
                    return None
                return f"{result[2]} FAILED\n{result[1]}"
    finally:
        (STATE / f"{name}.lock").unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if "--pre" in argv:
        take_pre_hash(payload)
        return 0  # the PreToolUse half never blocks an edit
    if payload.get("tool_name") == "Bash":
        changed = [p for p in changed_by_shell() if in_repo(p)]
    else:
        changed = [p for p in changed_by_edit(payload) if in_repo(p)]
        record_edits(payload, changed)
    if not changed:
        return 0

    failures = []
    projects: dict[tuple[str, str], Project | None] = {}
    in_a_project = []
    for path in changed:
        found = projects_of(path, ROOT)
        if found:
            in_a_project.append(path)
        projects.update({(p.kind, rel(p.path)): p for p in found} if found else {("root", ""): None})
    # A root file's project run IS the whole-tree purity scan; only project files need their own.
    if in_a_project:
        ok, out = run(purity("--files", *map(str, in_a_project)), ROOT)
        if not ok:
            failures.append(f"purity FAILED for the changed files\n{out}")
    for project in projects.values():
        failure = run_project(project)
        if failure:
            failures.append(failure)
    if not failures:
        return 0
    names = ", ".join(rel(p) for p in changed[:6])
    print(f"After editing {names}:\n\n" + "\n\n".join(failures), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
