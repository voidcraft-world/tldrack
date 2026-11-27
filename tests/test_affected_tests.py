"""The edit hook runs tests on a snapshot of only what git or the agent vouches for.

Each case builds a throwaway repository holding copies of the hook, the purity test
and `scripts/projects.py`, with the hook's cache beside it (never inside it), then
plays tool calls in the way Claude Code does: `--pre` before an Edit, the plain call
after any tool. Test files and `test` scripts drop marker files in `$MARKERS`, OUTSIDE
both, so "did it execute" is a file on disk, not a log line. (The directory goes by
environment, never written into a scanned file: a temp path can hold a user name,
and the purity scan rightly refuses it.) The trust rules are owned by
the hook's docstring (`.claude/hooks/affected_tests.py`, WHAT IT WILL EXECUTE).

The last part plays several sessions on one checkout, by `session_id`, to see who is
woken (exit 2) and who is not; those rules are owned by the same docstring's WHO
GETS WOKEN.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import signal
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = Path(".claude/hooks/affected_tests.py")
COPIED = [HOOK, Path("tests/test_purity.py"), Path("scripts/projects.py")]

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("npm")), reason="needs git and npm")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for name in COPIED:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / name, repo / name)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test" + "@" + "example.com")
    git(repo, "config", "user.name", "test")
    commit(repo, "base")
    return repo


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def marker(repo: Path, name: str) -> Path:
    return repo.parent / name


def drops(repo: Path, name: str) -> str:
    """A node test file that drops a marker when it runs."""
    return f"require('fs').writeFileSync(process.env.MARKERS + '/{name}', '')\n"


def touches(name: str) -> dict:
    """A manifest whose `test` script drops a marker."""
    return {"scripts": {"test": f'touch "$MARKERS/{name}"'}}


def add_project(repo: Path, *, script_marker: str | None = None, committed: bool = True) -> None:
    """ui/: a `test` script that drops `script_marker`, or no script (then `node --test` runs ok.test.js)."""
    (repo / "ui").mkdir(exist_ok=True)
    manifest = touches(script_marker) if script_marker else {}
    (repo / "ui" / "package.json").write_text(json.dumps(manifest))
    (repo / "ui" / "a.js").write_text("x\n")
    (repo / "ui" / "ok.test.js").write_text(drops(repo, "RAN"))
    if committed:
        commit(repo, "ui")


def hook_env(repo: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(repo.parent)),
        "CLAUDE_PROJECT_DIR": str(repo),
        "XDG_CACHE_HOME": str(repo.parent / "cache"),
        "MARKERS": str(repo.parent),
    }


def hook_command(repo: Path, *args: str) -> list[str]:
    return [sys.executable, "-I", "-B", str(repo / HOOK), *args]


def run_hook(repo: Path, payload: dict | None, *args: str) -> subprocess.CompletedProcess[str]:
    stdin = "" if payload is None else json.dumps(payload)
    return subprocess.run(hook_command(repo, *args), input=stdin, text=True, capture_output=True, env=hook_env(repo))


def call_hook(repo: Path, payload: dict, *args: str) -> int:
    return run_hook(repo, payload, *args).returncode


def load_hook(root: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The hook as a module, with the environment a hook call gets, for the cases that call it directly."""
    for name, value in hook_env(root).items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", root / HOOK)
    hook = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, hook)  # dataclasses resolves annotations through sys.modules
    spec.loader.exec_module(hook)
    return hook


def bash() -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": "anything"}}


def edit(repo: Path, name: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(repo / name)}}


def write(repo: Path, name: str, content: str) -> dict:
    (repo / name).write_text(content)
    return {"tool_name": "Write", "tool_input": {"file_path": str(repo / name), "content": content}}


def agent_edit(repo: Path, name: str, content: str) -> int:
    """An Edit as Claude Code runs it: the PreToolUse half, the change, then PostToolUse."""
    call_hook(repo, edit(repo, name), "--pre")
    (repo / name).write_text(content)
    return call_hook(repo, edit(repo, name))


def as_session(payload: dict, session: str, duration_ms: int | None = None) -> dict:
    """The payload as one session's tool call sends it: its session, a fresh tool_use_id, maybe a duration."""
    ids = {"session_id": session, "tool_use_id": f"toolu_{uuid.uuid4().hex[:20]}"}
    return {**payload, **ids, **({} if duration_ms is None else {"duration_ms": duration_ms})}


def session_edit(repo: Path, name: str, content: str, session: str) -> subprocess.CompletedProcess[str]:
    """`agent_edit`, made by one session."""
    payload = as_session(edit(repo, name), session)
    run_hook(repo, payload, "--pre")
    (repo / name).write_text(content)
    return run_hook(repo, payload)


def next_call(repo: Path, session: str) -> subprocess.CompletedProcess[str]:
    """A later call of `session` that changes nothing in the tree: it delivers what waits for it."""
    return run_hook(repo, as_session(edit(repo, "../elsewhere.txt"), session))


def touch_tracked(repo: Path) -> None:
    (repo / "ui" / "a.js").write_text("changed\n")


# ---------------------------------------------------------------- what runs


def test_a_committed_project_runs_after_a_shell_edit(root):
    add_project(root, script_marker="SCRIPT")
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert marker(root, "SCRIPT").exists()


def test_git_add_is_the_gesture_that_trusts_a_new_project(root):
    add_project(root, script_marker="SCRIPT", committed=False)
    git(root, "add", "-A")
    assert call_hook(root, bash()) == 0
    assert marker(root, "SCRIPT").exists()


def test_a_manifest_the_agent_edited_runs(root):
    add_project(root, script_marker="SCRIPT")
    edited = json.dumps(touches("EDITED"))
    assert agent_edit(root, "ui/package.json", edited) == 0
    assert marker(root, "EDITED").exists()


def test_a_test_file_the_agent_wrote_whole_is_in_the_copy(root):
    add_project(root)
    assert call_hook(root, write(root, "ui/new.test.js", drops(root, "NEW"))) == 0
    assert marker(root, "RAN").exists() and marker(root, "NEW").exists()


def test_an_edit_to_a_file_the_agent_wrote_keeps_it_in_the_copy(root):
    add_project(root)
    call_hook(root, write(root, "ui/new.test.js", "// v1\n"))
    assert agent_edit(root, "ui/new.test.js", drops(root, "V2")) == 0
    assert marker(root, "V2").exists()


def test_installed_node_modules_are_linked_into_the_copy(root):
    add_project(root)
    (root / "ui" / "node_modules" / "dep").mkdir(parents=True)
    (root / "ui" / "node_modules" / "dep" / "index.js").write_text(drops(root, "DEP"))
    (root / "ui" / "ok.test.js").write_text("require('dep')\n" + drops(root, "RAN"))
    commit(root, "uses dep")
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert marker(root, "RAN").exists() and marker(root, "DEP").exists()


# ---------------------------------------------------------------- what never runs


def test_an_untracked_project_never_runs(root):
    add_project(root, script_marker="PWNED", committed=False)  # arrived by curl, tar or a patch
    call_hook(root, bash())
    call_hook(root, edit(root, "ui/a.js"))
    assert not marker(root, "PWNED").exists()


def test_a_manifest_changed_by_the_shell_does_not_run(root):
    add_project(root, script_marker="SCRIPT")
    (root / "ui" / "package.json").write_text(json.dumps(touches("PWNED")))
    touch_tracked(root)
    call_hook(root, bash())
    assert not marker(root, "PWNED").exists()


def test_a_manifest_the_shell_changed_stays_untrusted_after_an_edit(root):
    add_project(root, script_marker="SCRIPT")
    (root / "ui" / "package.json").write_text(json.dumps(touches("PWNED")))
    call_hook(root, bash())
    agent_edit(root, "ui/a.js", "changed\n")
    assert not marker(root, "PWNED").exists()


def test_a_partial_edit_cannot_launder_a_manifest_the_shell_changed(root):
    add_project(root, script_marker="SCRIPT")
    evil = touches("PWNED")
    (root / "ui" / "package.json").write_text(json.dumps(evil))
    agent_edit(root, "ui/package.json", json.dumps({**evil, "x": 1}))
    assert not marker(root, "PWNED").exists()


@pytest.mark.parametrize(
    "arrived",
    [
        "ui/evil.test.js",  # next to the project's own tests
        "ui/dist/evil.test.js",  # in build output, which `node --test` walks
        "ui/lib/evil.test.js",  # hidden by an arrived .gitignore (below)
        ".claude/.hook-state/x",  # beside a forged trust record (below)
    ],
)
def test_an_arrived_file_is_never_in_the_copy(root, arrived):
    add_project(root)
    (root / arrived).parent.mkdir(parents=True, exist_ok=True)
    (root / arrived).write_text(drops(root, "PWNED"))
    (root / "ui" / "lib" / ".gitignore").parent.mkdir(exist_ok=True)
    (root / "ui" / "lib" / ".gitignore").write_text("*\n")
    forged = {"ui/evil.test.js": hashlib.sha256(drops(root, "PWNED").encode()).hexdigest()}
    (root / ".claude" / ".hook-state").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / ".hook-state" / "authored.json").write_text(json.dumps(forged))
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert marker(root, "RAN").exists(), "the run happened"
    assert not marker(root, "PWNED").exists()


def test_a_partial_edit_cannot_launder_an_arrived_file(root):
    add_project(root)
    (root / "ui" / "evil.test.js").write_text(drops(root, "PWNED"))
    agent_edit(root, "ui/evil.test.js", "// touched\n" + drops(root, "PWNED"))
    assert marker(root, "RAN").exists() and not marker(root, "PWNED").exists()


def test_an_edit_with_no_pre_hash_fails_closed(root):
    add_project(root)
    (root / "ui" / "evil.test.js").write_text(drops(root, "PWNED"))
    call_hook(root, edit(root, "ui/evil.test.js"))
    assert marker(root, "RAN").exists() and not marker(root, "PWNED").exists()


def test_a_write_whose_content_is_not_the_file_is_not_trusted(root):
    add_project(root)
    payload = write(root, "ui/evil.test.js", "// what the agent sent\n")
    (root / "ui" / "evil.test.js").write_text(drops(root, "PWNED"))
    call_hook(root, payload)
    assert marker(root, "RAN").exists() and not marker(root, "PWNED").exists()


def test_a_glob_shaped_directory_cannot_borrow_a_tracked_manifest(root):
    (root / "ua").mkdir()
    (root / "ua" / "package.json").write_text("{}")
    commit(root, "ua")
    evil = root / "u*"
    evil.mkdir()
    (evil / "package.json").write_text(json.dumps(touches("PWNED")))
    (evil / "a.js").write_text("x\n")
    call_hook(root, edit(root, "u*/a.js"))
    assert not marker(root, "PWNED").exists()


def test_a_planted_bytecode_cache_is_never_executed(root):
    """A .pyc whose header matches projects.py's mtime and size is what an import would run."""
    add_project(root)
    source = root / "scripts" / "projects.py"
    real, stat = source.read_bytes(), source.stat()
    planted = f"open({str(marker(root, 'PWNED'))!r}, 'w')\n".encode()
    evil = root.parent / "evil_src" / "projects.py"
    evil.parent.mkdir()
    evil.write_bytes(planted + b"#" * (len(real) - len(planted) - 1) + b"\n")
    os.utime(evil, (stat.st_atime, stat.st_mtime))
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(evil), cfile=str(cache), invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    subprocess.run([sys.executable, "-c", "import projects"], cwd=source.parent, check=True)
    assert marker(root, "PWNED").exists(), "the plant is live: a plain import runs it"
    marker(root, "PWNED").unlink()
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert marker(root, "RAN").exists() and not marker(root, "PWNED").exists()


# ---------------------------------------------------------------- the copy itself


def test_the_copy_holds_no_arrived_config_from_any_parent_directory(root, monkeypatch):
    add_project(root)
    (root / "plugins" / "worktree").mkdir(parents=True)
    (root / "plugins" / "worktree" / "pyproject.toml").write_text("[project]\nname = 'w'\n")
    commit(root, "plugin")
    arrived = [
        "conftest.py",
        "pytest.ini",
        "package.json",
        ".npmrc",
        "uv.toml",
        "setup.cfg",
        "plugins/conftest.py",
        "plugins/pyproject.toml",
        "node_modules/evil/index.js",
    ]
    for name in arrived:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("arrived\n")
    (root / "ui" / "node_modules").mkdir()
    hook = load_hook(root, monkeypatch)
    dest = root.parent / "copy"
    hook.sync_snapshot(dest, {})
    for name in arrived:
        assert not (dest / name).exists(), name
    assert (dest / "plugins" / "worktree" / "pyproject.toml").is_file()
    assert (dest / "ui" / "ok.test.js").is_file()
    assert (dest / "ui" / "node_modules").is_symlink()
    assert root.resolve() not in hook.SNAPSHOTS.resolve().parents, "config discovery walks UP from the copy"
    (root / "ui" / "ok.test.js").unlink()  # a file that leaves the tree leaves the copy
    hook.sync_snapshot(dest, {})
    assert not (dest / "ui" / "ok.test.js").exists()


def test_a_stray_node_modules_above_a_project_skips_it(root):
    add_project(root)
    (root / "node_modules" / "fsevents").mkdir(parents=True)  # what a linked package would try-require
    (root / "node_modules" / "fsevents" / "index.js").write_text(drops(root, "PWNED"))
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert not marker(root, "RAN").exists(), "skipped, not run"
    (root / "package.json").write_text("{}")  # a tracked manifest makes it an installed tree
    commit(root, "root deps")  # this also commits the first change, so make a second one
    (root / "ui" / "a.js").write_text("changed again\n")
    assert call_hook(root, bash()) == 0
    assert marker(root, "RAN").exists()


def test_the_copy_holds_the_bytes_it_checked(root, monkeypatch):
    add_project(root)
    hook = load_hook(root, monkeypatch)
    (root / "ui" / "new.test.js").write_text("// the agent's\n")
    authored = {"ui/new.test.js": hashlib.sha256(b"// the agent's\n").hexdigest()}
    dest = root.parent / "copy"
    hook.sync_snapshot(dest, authored)
    assert (dest / "ui" / "new.test.js").read_text() == "// the agent's\n"
    (root / "ui" / "new.test.js").write_text("// swapped after the write\n")
    hook.sync_snapshot(dest, authored)
    assert not (dest / "ui" / "new.test.js").exists()
    # A tracked manifest is judged on the copy: changed bytes there fail, whatever the tree says now.
    tracked = hook.sync_snapshot(dest, {})
    project = hook.Project("node", root / "ui")
    assert hook.manifest_trusted(project, {}, tracked, dest)
    (dest / "ui" / "package.json").write_text('{"scripts": {"test": "evil"}}')
    assert not hook.manifest_trusted(project, {}, tracked, dest)


def test_links_in_the_copy_come_only_from_tracked_names(root, monkeypatch):
    add_project(root)
    (root / "ui" / "node_modules").mkdir()
    (root / "ui" / "foo-package.json").write_text("{}")  # ends with the name, is not the name
    commit(root, "a lookalike manifest")
    (root / "ui" / "foo-" / "node_modules").mkdir(parents=True)
    hook = load_hook(root, monkeypatch)
    (root / "ui" / "new.test.js").write_text("// the agent's\n")
    authored = {"ui/new.test.js": hashlib.sha256(b"// the agent's\n").hexdigest()}
    elsewhere = root.parent / "elsewhere.js"
    elsewhere.write_text(drops(root, "PWNED"))
    (root / "ui" / "new.test.js").unlink()
    (root / "ui" / "new.test.js").symlink_to(elsewhere)  # the shell swapped the agent's file for a link
    dest = root.parent / "copy"
    hook.sync_snapshot(dest, authored)
    assert not (dest / "ui" / "new.test.js").exists() and not (dest / "ui" / "new.test.js").is_symlink()
    assert (dest / "ui" / "node_modules").resolve() == (root / "ui" / "node_modules").resolve()
    assert not (dest / "ui" / "foo-").exists()


def test_a_configured_fsmonitor_never_runs_from_the_hook(root):
    add_project(root)
    monitor = root.parent / "monitor.sh"
    monitor.write_text('#!/bin/sh\ntouch "$MARKERS/PWNED"\n')
    monitor.chmod(0o755)
    git(root, "config", "core.fsmonitor", str(monitor))
    touch_tracked(root)
    assert call_hook(root, bash()) == 0
    assert marker(root, "RAN").exists() and not marker(root, "PWNED").exists()


# ---------------------------------------------------------------- who gets woken

# Home paths, built at run time so this file never holds one: the purity scan of the fixture refuses it.
HAZARD = "/" + "Users" + "/zed/notes\n"
OTHER_HAZARD = "/" + "Users" + "/kim/plans\n"
FILES = ("a.js", "b.js", "c.js", "d.js", "e.js")
UI = "node--ui"  # the ui project's runner: its lock and queue name


def hazard(name: str) -> str:
    """A home-path hit's signature in `name`, up to the digest of its masked line (`purity <file> | <rule> | <digest>`)."""
    return f"purity {name} | a home directory path | "


def only_hazard(done: subprocess.CompletedProcess[str], name: str) -> bool:
    """The call was woken for exactly one home-path hit, in `name`."""
    found = yours(done)
    return found.startswith(f"yours: {hazard(name)}") and len(found) == len(f"yours: {hazard(name)}") + 8


def each_file_test() -> str:
    """One node test per ui/*.js file, red while that file holds BROKEN; every run appends to $MARKERS/RUNS."""
    return (
        "const fs = require('fs'), path = require('path'), test = require('node:test');\n"
        "fs.appendFileSync(process.env.MARKERS + '/RUNS', 'x');\n"
        "for (const name of fs.readdirSync(__dirname).filter((n) => n.endsWith('.js') && !n.includes('.test.')).sort())\n"
        "  test('clean ' + name, () => { if (fs.readFileSync(path.join(__dirname, name), 'utf8').includes('BROKEN')) throw new Error(name); });\n"
    )


def add_rack_project(repo: Path, *, broken: tuple[str, ...] = (), directory: str = "ui", gate: bool = False) -> None:
    """A node project of FILES under `directory`; with `gate`, its test file throws at load while gate.txt holds BROKEN."""
    (repo / directory).mkdir(exist_ok=True)
    (repo / directory / "package.json").write_text("{}")
    for name in FILES:
        (repo / directory / name).write_text("BROKEN\n" if name in broken else "x\n")
    loads = "if (fs.readFileSync(path.join(__dirname, 'gate.txt'), 'utf8').includes('BROKEN')) throw new Error('gate');\n"
    head, rest = each_file_test().split("\n", 1)
    (repo / directory / "each.test.js").write_text(f"{head}\n{loads}{rest}" if gate else each_file_test())
    if gate:
        (repo / directory / "gate.txt").write_text("ok\n")
    commit(repo, directory)


def warm(repo: Path) -> None:
    """A first green pass of every check: the ledger exists, nothing is left to seed."""
    assert session_edit(repo, "ui/d.js", "x\n", "W").returncode == 0


def runs(repo: Path) -> int:
    return len(marker(repo, "RUNS").read_text()) if marker(repo, "RUNS").exists() else 0


def red(repo: Path) -> str:
    return run_hook(repo, None, "--red").stdout


def yours(done: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(line for line in done.stderr.splitlines() if line.startswith("yours: "))


def red_test(name: str) -> str:
    return f"tests ui (node) | each.test.js > clean {name}"


def test_the_culprit_is_woken_and_a_bystander_sleeps(root):
    add_rack_project(root)
    warm(root)
    broke = session_edit(root, "ui/a.js", "BROKEN\n", "A")
    assert broke.returncode == 2 and red_test("a.js") in yours(broke)
    assert "After editing ui/a.js:" in broke.stderr
    bystander = session_edit(root, "ui/b.js", "y\n", "B")
    assert bystander.returncode == 0, bystander.stderr  # (a) the project is red, and not by B
    again = session_edit(root, "ui/c.js", "y\n", "A")
    assert again.returncode == 2 and red_test("a.js") in yours(again)  # (b) still red: A hears again
    assert session_edit(root, "ui/a.js", "x\n", "A").returncode == 0
    assert "0 red signature(s)" in red(root)  # (j) the green run cleared it


def test_a_change_before_the_first_red_co_owns_it_and_one_after_does_not(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    signature = red_test("a.js")
    record = hook.Record("node tests (ui) FAILED", "tests ui (node)", {signature: None}, True, [], frozenset({"tests ui (node)"}))
    ledger = {**hook.new_ledger(), "created": 0.0, "vouched": {"tests ui (node)": 8.0}}  # a complete run began at 8: green

    def change(owner: str, at: float, ready: float | None = None) -> object:
        return hook.Entry(owner, f"toolu_{owner}", "ui/a.js", at, at if ready is None else ready)

    assert hook.judge(ledger, record, [change("A", 10.0)], 50.0, 100.0, True) == {"A": [signature]}
    # (c) B's change landed while that pass still read the tree (before 100): a culprit too. C's came after.
    # D's came before the green run at 8, which saw it: it made nothing red. E's command spanned 7 to 120: it may have.
    later = [change("B", 90.0), change("C", 110.0), change("D", 5.0), change("E", 7.0, ready=120.0)]
    assert hook.judge(ledger, record, later, 150.0, 200.0, True) == {"B": [signature], "E": [signature]}
    entry = ledger["reds"][signature]
    assert (entry["owners"], entry["first_red_end"], entry["last_red"], entry["after"]) == (["A", "B", "E"], 100.0, 200.0, 8.0)


def test_a_late_claim_of_a_change_a_pass_already_saw_joins_it_without_a_rerun(root):
    add_rack_project(root)
    warm(root)
    (root / "ui" / "a.js").write_text("BROKEN\n")  # two sessions' commands both ran while this happened
    first = run_hook(root, as_session(bash(), "A", duration_ms=2000))
    assert first.returncode == 2 and red_test("a.js") in yours(first)
    tested = runs(root)
    second = run_hook(root, as_session(bash(), "B", duration_ms=60_000))
    assert second.returncode == 2 and red_test("a.js") in yours(second)  # B's window held it too: both claim
    assert runs(root) == tested, "the change was already tested: B joins that verdict"


@pytest.mark.parametrize("edits", [1, 2])  # agents edit one file several times in a row: a run of Edits is one write
def test_a_neighbours_edit_inside_a_long_command_is_not_the_commands(root, monkeypatch, edits):
    add_rack_project(root)
    warm(root)
    time.sleep(2.5)  # nothing before A's edits changes inside B's window
    edited = time.time()
    for _ in range(edits - 1):
        assert session_edit(root, "ui/a.js", "y\n", "A").returncode == 0
    assert session_edit(root, "ui/a.js", "BROKEN\n", "A").returncode == 2
    long_command = round((time.time() - edited + 0.5) * 1000)  # B's command ran across A's whole edit
    bystander = run_hook(root, as_session(bash(), "B", duration_ms=long_command))
    assert bystander.returncode == 0, bystander.stderr
    owners = json.loads(load_hook(root, monkeypatch).OWNERS.read_text())
    assert owners["ui/a.js"]["session"] == "A"


def test_a_bash_call_claims_only_its_own_window(root):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/b.js", "y\n", "C").returncode == 0  # C owns b.js now
    (root / "ui" / "b.js").write_text(HAZARD)  # changed later by someone else, outside B's command
    (root / "ui" / "e.js").write_text(HAZARD)  # a file no session ever claimed
    time.sleep(2.2)
    (root / "ui" / "a.js").write_text(HAZARD)  # B's own command
    mine = run_hook(root, as_session(bash(), "B", duration_ms=300))
    assert mine.returncode == 2 and only_hazard(mine, "ui/a.js")  # (d)
    assert "ui/b.js" not in mine.stderr and "ui/e.js" not in mine.stderr
    last_owner = next_call(root, "C")
    assert last_owner.returncode == 2 and only_hazard(last_owner, "ui/b.js")
    unowned = red(root).split(hazard("ui/e.js"), 1)[1].splitlines()[1]
    assert "owners: - (unowned)" in unowned  # (e) recorded, and nobody woken for it
    assert next_call(root, "B").returncode == 0 and next_call(root, "W").returncode == 0


def test_the_first_ledger_ever_records_what_is_already_red_quietly(root):
    add_rack_project(root, broken=("a.js",))
    first = session_edit(root, "ui/c.js", "y\n", "A")
    assert first.returncode == 0, first.stderr  # (f) seeded, not A's
    listed = red(root)
    assert red_test("a.js") in listed and "owners: - (unowned)" in listed
    assert session_edit(root, "ui/c.js", "z\n", "A").returncode == 0
    new = session_edit(root, "ui/b.js", "BROKEN\n", "A")
    assert new.returncode == 2 and yours(new) == f"yours: {red_test('b.js')}"
    assert "also red here, not yours: 1" in new.stderr


def test_mail_a_waiter_left_reaches_its_session_on_the_next_call(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    hook = load_hook(root, monkeypatch)
    lock = hook.STATE / f"{UI}.lock"
    lock.write_text(str(os.getpid()))  # a live runner holds the project
    payload = as_session(write(root, "ui/a.js", "BROKEN\n"), "A")
    started = time.time()
    waiting = hook.claim(payload, "A", payload["tool_use_id"], started, started + 60)
    assert waiting == {"purity-files", UI}
    hook.settle(waiting, payload["tool_use_id"], time.time() + 1.0)  # its deadline comes first
    assert not hook.is_done(payload["tool_use_id"], UI) and hook.deliver("A") == 0
    lock.unlink()
    assert next_call(root, "B").returncode == 0  # B's call ran A's change, and keeps quiet about it
    later = next_call(root, "A")
    assert later.returncode == 2 and red_test("a.js") in yours(later)  # (g)
    assert next_call(root, "A").returncode == 0, "delivered once"


def test_a_waiter_takes_over_when_the_runner_lock_frees(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    lock = load_hook(root, monkeypatch).STATE / f"{UI}.lock"
    lock.write_text(str(os.getpid()))
    payload = as_session(write(root, "ui/a.js", "BROKEN\n"), "A")
    tested = runs(root)
    waiter = subprocess.Popen(
        hook_command(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=hook_env(root)
    )
    waiter.stdin.write(json.dumps(payload))
    waiter.stdin.close()
    time.sleep(1.5)
    assert waiter.poll() is None and runs(root) == tested, "it waits while another runner holds the project"
    lock.unlink()
    assert waiter.wait(timeout=60) == 2  # (h)
    assert red_test("a.js") in waiter.stderr.read() and runs(root) == tested + 1
    assert not lock.exists()


@pytest.mark.parametrize(
    "holder",
    [
        "a dead pid",  # the previous version's pid file, its runner killed at the hook timeout, mid-pass
        "a live stranger's pid",  # the previous version's, and a reboot gave the dead runner's pid away
        "a dead runner of this version",  # its flock went with it, whatever process its pid names now
    ],
)
def test_a_dead_runners_batch_runs_again(root, monkeypatch, holder):
    add_rack_project(root)
    warm(root)
    hook = load_hook(root, monkeypatch)
    lock = hook.STATE / f"{UI}.lock"
    if holder == "a dead pid":
        gone = subprocess.Popen(["true"])
        gone.wait()
        lock.write_text(str(gone.pid))
    elif holder == "a live stranger's pid":
        lock.write_text(str(os.getpid()))
        old = time.time() - hook.LOCK_STALE_S - 1
        os.utime(lock, (old, old))  # older than any hook of that version lives
    else:
        lock.write_text(str(os.getpid()))  # alive, and fresh: only the free flock says its runner is gone
        (hook.STATE / f"{UI}.flock").write_text(str(os.getpid()))
    (root / "ui" / "a.js").write_text("BROKEN\n")
    now = time.time()
    hook.write_entries(hook.INFLIGHT / f"{UI}.jsonl", [hook.Entry("A", "toolu_gone", "ui/a.js", now, now)])
    tested = runs(root)
    assert next_call(root, "B").returncode == 0
    assert runs(root) == tested + 1  # (i) the next call took the dead lock and ran the batch
    later = next_call(root, "A")
    assert later.returncode == 2 and red_test("a.js") in yours(later)
    assert not (hook.STATE / f"{UI}.lock").exists()


def test_only_an_authoritative_run_clears_its_namespace(root, monkeypatch):
    add_rack_project(root)
    hook = load_hook(root, monkeypatch)
    project = hook.Project("node", root / "ui")
    elsewhere = "purity ui/a.js | a home directory path"
    ledger = hook.new_ledger()
    red_since = {"first_red_end": 1.0, "last_red": 1.0, "after": 0.0}
    ledger["reds"] = {red_test("a.js"): {"owners": ["A"], **red_since}, elsewhere: {"owners": ["B"], **red_since}}
    timed_out = hook.tests_record(project, hook.Ran(None, "", timed_out=True))
    assert not timed_out.authoritative and list(timed_out.reds) == ["tests ui (node) | timed out"]
    hook.judge(ledger, timed_out, [], 2.0, 2.0, True)
    assert red_test("a.js") in ledger["reds"], "(j) a run that never finished clears nothing"
    unloaded = hook.tests_record(project, hook.Ran(1, "✖ failing tests:\n\ntest at each.test.js:1:1\n✖ each.test.js (3ms)"))
    assert not unloaded.authoritative, "a test file that failed to load hides its own tests"
    hook.judge(ledger, unloaded, [], 2.5, 2.5, True)
    assert red_test("a.js") in ledger["reds"]
    green = hook.tests_record(project, hook.Ran(0, "ok"))
    ledger["reds"][red_test("b.js")] = {"owners": ["C"], "first_red_end": 3.5, "last_red": 3.5, "after": 0.0}
    hook.judge(ledger, green, [], 3.0, 4.0, True)  # it began reading at 3: it cannot speak for what was red at 3.5
    assert sorted(ledger["reds"]) == [elsewhere, red_test("b.js")], "a green run clears its own namespace, and only its own"


def test_signatures_name_the_failure_never_the_run(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    node = "\n".join(
        [
            "✖ fails here (0.602ms)",
            "✖ failing tests:",
            "",
            "test at tests/unit/a.test.ts:4:1",
            "✖ fails here (0.602ms)",
            "  AssertionError [ERR_ASSERTION]: 1 == 2",
            "test at b.test.js:1:1",
            "✖ b.test.js (86.7ms)",
        ]
    )
    pytest_out = "\n".join(
        [
            "ERROR    root:test_a.py:5 noise",
            "=========================== short test summary info ============================",
            "FAILED tests/test_a.py::test_bad - assert 1 == 2",
            "FAILED tests/test_a.py::test_param[a b] - AssertionError: assert 'a b' == 'c'",
            "2 failed, 2 passed in 0.01s",
        ]
    )
    assert hook.failed_tests(node.splitlines()) == ["tests/unit/a.test.ts > fails here", "b.test.js > b.test.js"]
    assert hook.failed_tests(pytest_out.splitlines()) == ["tests/test_a.py::test_bad", "tests/test_a.py::test_param[a b]"]
    whole = ["b.test.js > b.test.js", "tests/test_a.py", "a.test.js"]  # node at load, pytest at collection, TAP
    assert all(map(hook.whole_file, whole))
    assert not any(map(hook.whole_file, ["tests/unit/a.test.ts > fails here", "tests/test_a.py::test_bad", "fails here"]))
    hit = "HAZARD ui/a.js:3: a home directory path: x ‹masked›/notes"
    moved = "HAZARD ui/a.js:9: a home directory path: x ‹masked›/notes"  # the same line, further down: the same hit
    other = "HAZARD ui/a.js:4: a home directory path: y ‹masked›/plans"  # a second one in that file: its own
    words = "RATCHET ui/b.js: 2 private word(s), baseline 0"
    record = hook.purity_record(hook.Ran(1, f"{hit}\n{other}\n{words}\npurity: 2 hazards"), "t", "purity --files", frozenset())
    assert record.authoritative
    hits = sorted(s for s in record.reds if s.startswith("purity ui/a.js | a home directory path | "))
    assert len(hits) == 2 and all(len(s) == len("purity ui/a.js | a home directory path | ") + 8 for s in hits)
    assert {s: a for s, a in record.reds.items() if s not in hits} == {"purity ui/b.js | private words": "ui/b.js"}
    assert record.counts["purity ui/b.js | private words"] == 2, "a count, so one more word is a new one"
    one = hook.purity_record(hook.Ran(1, hit), "t", "purity --files", frozenset())
    again = hook.purity_record(hook.Ran(1, f"{moved}\n{moved}"), "t", "purity --files", frozenset())
    assert list(again.reds) == list(one.reds) and again.counts[next(iter(one.reds))] == 2
    crashed = hook.purity_record(hook.Ran(1, "Traceback (most recent call last):"), "t", "purity --files", frozenset())
    assert not crashed.authoritative and list(crashed.reds) == ["purity | exit 1"]


def bash_since(started: float, session: str) -> dict:
    """A Bash call of `session` whose command ran from `started` until now."""
    return as_session(bash(), session, duration_ms=round((time.time() - started + 0.3) * 1000))


@pytest.mark.parametrize("command", ["read-only", "wrote the file before the Write"])
def test_a_write_sets_every_byte_so_a_command_it_overlapped_is_not_its_author(root, command):
    add_rack_project(root)
    warm(root)
    time.sleep(1.5)  # nothing before this changes inside S's window
    started = time.time()
    if command != "read-only":
        (root / "ui" / "a.js").write_text("BROKEN\n")  # early in S's command; C reads it and Writes it back whole
    assert run_hook(root, as_session(write(root, "ui/a.js", "BROKEN\n// C's comment\n"), "C")).returncode == 2
    reader = run_hook(root, bash_since(started, "S"))  # S's command (a test run, `gh pr checks`) ran across C's Write
    assert reader.returncode == 0, reader.stderr
    assert "owners: C;" in red(root)  # C chose every byte, BROKEN included, and C hears


def test_a_command_that_wrote_again_after_its_break_turned_red_still_owns_it(root):
    add_rack_project(root)
    warm(root)
    time.sleep(1.5)  # b.js was committed before C's window: B's Edit of it is not C's, so only a.js can make C an owner
    started = time.time()
    (root / "ui" / "a.js").write_text("BROKEN\n")  # C's command breaks a.js...
    assert session_edit(root, "ui/b.js", "y\n", "B").returncode == 2  # ...B's pass sees it red first...
    (root / "ui" / "a.js").write_text("BROKEN\n\n")  # ...and C's command writes it again (a formatter, a codegen rerun)
    command = run_hook(root, bash_since(started, "C"))
    assert command.returncode == 2 and red_test("a.js") in yours(command)  # dated from its window's start


def test_a_failure_a_load_error_hid_reaches_its_culprit_once_revealed(root):
    add_rack_project(root, gate=True)
    warm(root)
    hidden = session_edit(root, "ui/gate.txt", "BROKEN\n", "A")
    assert hidden.returncode == 2 and yours(hidden) == "yours: tests ui (node) | each.test.js > each.test.js"
    assert session_edit(root, "ui/b.js", "BROKEN\n", "B").returncode == 0  # its test never ran: nothing to tell yet
    assert "suspects of tests ui (node)" in red(root)
    assert session_edit(root, "ui/gate.txt", "ok\n", "A").returncode == 2  # A's fix reveals it: A may have caused it too
    revealed = next_call(root, "B")
    assert revealed.returncode == 2 and red_test("b.js") in yours(revealed)
    assert "After editing ui/b.js:" in revealed.stderr
    again = session_edit(root, "ui/c.js", "y\n", "B")
    assert again.returncode == 0, again.stderr  # A co-owns it: told once to each, never pinned on B
    listed = red(root)
    assert "owners: A, B;" in listed and "each.test.js > each.test.js" not in listed and "suspects" not in listed


def test_a_project_first_tested_after_the_ledger_exists_wakes_its_culprit(root):
    add_rack_project(root)
    add_rack_project(root, directory="lib")
    warm(root)  # ui only: the ledger exists from here on
    broke = session_edit(root, "lib/a.js", "BROKEN\n", "A")
    assert broke.returncode == 2 and yours(broke) == "yours: tests lib (node) | each.test.js > clean a.js"  # (f), a later project


@pytest.mark.parametrize(
    "ended_by",
    ["a commit, then a session's Bash call", "a commit from the operator's terminal", "a revert, then a session's Bash call"],
)
def test_a_claim_that_ended_names_no_owner_for_a_later_change(root, monkeypatch, ended_by):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/a.js", "y\n", "A").returncode == 0
    time.sleep(1.1)  # commit times are whole seconds: one in the claim's own second may have come first
    if ended_by.startswith("a revert"):
        git(root, "checkout", "--", "ui/a.js")
    else:
        commit(root, "A's work lands")
    if ended_by.endswith("Bash call"):
        assert run_hook(root, as_session(bash(), "A", duration_ms=100)).returncode == 0  # it sees a.js clean
    (root / "ui" / "a.js").write_text("BROKEN\n")  # much later, outside every window: the operator's editor
    time.sleep(1.5)
    assert run_hook(root, as_session(bash(), "B", duration_ms=100)).returncode == 0
    assert next_call(root, "A").returncode == 0, "A's claim ended with its change"
    assert "owners: - (unowned)" in red(root)
    hook = load_hook(root, monkeypatch)
    old = {"ui/b.js": {"session": "A", "ctime": 1, "base": None, "at": time.time() - hook.PRUNE_S - 1}}
    assert hook.current_claims(old, None, time.time(), time.time()) == {}, "and a claim older than PRUNE_S names nobody either"


def test_an_old_change_git_brings_back_owns_nothing_new(root):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/e.js", "y\n", "O").returncode == 0
    commit(root, "O's work")
    assert run_hook(root, as_session(bash(), "O", duration_ms=100)).returncode == 0
    assert session_edit(root, "ui/a.js", "BROKEN\n", "A").returncode == 2
    time.sleep(1.5)
    git(root, "reset", "--soft", "HEAD~1")  # e.js is dirty again, its bytes and its ctime untouched
    assert run_hook(root, as_session(bash(), "X", duration_ms=100)).returncode == 0
    assert next_call(root, "O").returncode == 0
    assert "owners: A;" in red(root)


def test_a_call_is_done_only_once_every_change_it_queued_is_judged(root, monkeypatch):
    add_rack_project(root)
    started = time.time()
    (root / "ui" / "b.js").write_text("y\n")  # early in B's command: before the next pass begins, so it reads it
    warm(root)
    (root / "ui" / "c.js").write_text("BROKEN\n")  # late in B's command: no pass has read it
    hook = load_hook(root, monkeypatch)
    (hook.STATE / f"{UI}.lock").write_text(str(os.getpid()))  # this test is the project's runner
    waiter = subprocess.Popen(
        hook_command(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=hook_env(root)
    )
    waiter.stdin.write(json.dumps(bash_since(started, "B")))
    waiter.stdin.close()
    for _ in range(200):
        if len(hook.read_entries(hook.QUEUE / f"{UI}.jsonl")) == 2:
            break
        time.sleep(0.05)
    hook.drain(UI, hook.NOBODY, time.time() + 120)  # b.js joins the last verdict; c.js gets a pass of its own
    assert waiter.wait(timeout=60) == 2, "it waited for the pass that judged c.js"
    assert red_test("c.js") in waiter.stderr.read()
    assert not list(hook.DONE.glob("*")), "every done mark was read by its waiter"


def test_one_delivery_shows_a_checks_block_once(root, monkeypatch, capsys):
    hook = load_hook(root, monkeypatch)
    title, mine = "node tests (ui) FAILED", [red_test("a.js")]
    hook.post_mail("A", UI, ["ui/a.js"], [{"title": title, "mine": mine, "text": f"{title}\nfrom the earlier verdict"}])
    hook.post_mail("A", UI, ["ui/b.js"], [{"title": title, "mine": mine, "text": f"{title}\nfrom the newer pass"}])
    hook.post_mail(
        "A", "purity-files", ["ui/b.js"], [{"title": "purity FAILED for the changed files", "mine": ["p"], "text": "purity"}]
    )
    assert hook.deliver("A") == 2
    shown = capsys.readouterr().err
    assert "newer pass" in shown and "earlier verdict" not in shown and "purity" in shown
    assert shown.startswith("After editing ui/a.js, ui/b.js:")


def test_a_slower_scan_never_clears_what_a_faster_one_found_after_it_read(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    (root / "README.md").write_text("hi\n")
    hook = load_hook(root, monkeypatch)
    now = time.time()
    batch = [hook.Entry("R", "toolu_root", "README.md", now, now)]
    verdict = hook.run_pass(hook.ROOT_SCAN, batch, time.time() + 120)  # the whole tree, read while it was clean
    assert verdict is not None and verdict.records[0].authoritative and not verdict.records[0].reds
    found = session_edit(root, "ui/a.js", HAZARD, "C")
    assert found.returncode == 2 and only_hazard(found, "ui/a.js")
    with hook.state_lock():
        hook.settle_batch(hook.ROOT_SCAN.name, verdict.records, batch, verdict.began, verdict.read, create=True)
    assert hazard("ui/a.js") in red(root)
    again = session_edit(root, "ui/a.js", HAZARD + "more\n", "C")
    assert again.returncode == 2 and hazard("ui/a.js") in yours(again)


def test_a_deletion_keeps_the_time_it_was_first_seen(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    (root / "ui" / "e.js").unlink()
    assert run_hook(root, as_session(bash(), "D", duration_ms=300)).returncode == 0  # D's `rm`: its own claim
    tested = runs(root)
    time.sleep(1.5)
    (root / "ui" / "notes.txt").write_text("x\n")  # anything made beside it moves the directory's ctime
    bystander = run_hook(root, as_session(bash(), "F", duration_ms=100))
    assert bystander.returncode == 0 and runs(root) == tested, "F's command changed nothing: nothing to test"
    assert json.loads(load_hook(root, monkeypatch).OWNERS.read_text())["ui/e.js"]["session"] == "D"


@pytest.mark.parametrize("change", ["an Edit", "a shell write", "a shell delete"])
def test_a_sessions_own_next_call_never_wakes_it_twice_for_one_change(root, change):
    add_rack_project(root, gate=True)
    warm(root)
    if change == "an Edit":
        first = session_edit(root, "ui/a.js", "BROKEN\n", "A")
    else:
        if change == "a shell write":
            (root / "ui" / "a.js").write_text("BROKEN\n")
        else:
            (root / "ui" / "gate.txt").unlink()  # each.test.js reads it at load: gone, no test file loads
        first = run_hook(root, as_session(bash(), "A", duration_ms=300))
    assert first.returncode == 2
    tested = runs(root)
    time.sleep(0.2)
    second = run_hook(root, as_session(bash(), "A", duration_ms=150))  # the `git diff` sent in the same message
    assert second.returncode == 0, second.stderr
    assert runs(root) == tested


def test_a_red_no_complete_run_ever_saw_green_is_told_once_and_never_nags(root):
    add_rack_project(root)
    add_rack_project(root, directory="lib", broken=("a.js",))  # red before any pass, and never by B
    warm(root)  # ui only: lib has no green run to hold a change against
    first = session_edit(root, "lib/c.js", "y\n", "B")
    assert first.returncode == 2 and "tests lib (node) | each.test.js > clean a.js" in yours(first)  # it may be B's
    for content in ("z\n", "w\n"):
        assert session_edit(root, "lib/c.js", content, "B").returncode == 0, "told once"
    assert "owners: - (unowned), B;" in red(root)


def fast_forward(repo: Path, name: str, content: str) -> None:
    """A pull of a merged change: a commit made elsewhere that rewrites `name`, then `git merge --ff-only` to it."""
    index = {**os.environ, "GIT_INDEX_FILE": str(repo.parent / "pulled.index")}

    def plumbing(*args: str, data: str | None = None) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], input=data, env=index, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    plumbing("read-tree", "HEAD")
    plumbing("update-index", "--cacheinfo", f"100644,{plumbing('hash-object', '-w', '--stdin', data=content)},{name}")
    merged = plumbing("commit-tree", plumbing("write-tree"), "-p", "HEAD", "-m", "a merged change")
    git(repo, "merge", "-q", "--ff-only", merged)


def test_a_red_a_head_move_brought_is_never_pinned_on_the_next_session(root):
    add_rack_project(root)
    add_rack_project(root, directory="lib")
    warm(root)
    assert session_edit(root, "lib/d.js", "x\n", "W").returncode == 0  # lib has a green run too
    fast_forward(root, "lib/b.js", "BROKEN\n")  # the tree moves with HEAD, and no hook sees it happen
    first = session_edit(root, "lib/c.js", "y\n", "B")
    assert first.returncode == 2 and "tests lib (node) | each.test.js > clean b.js" in yours(first)  # it may be B's
    for content in ("z\n", "w\n"):
        assert session_edit(root, "lib/c.js", content, "B").returncode == 0, "told once"
    assert "owners: - (unowned), B;" in red(root)


def test_a_bash_window_ends_when_the_hook_starts(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    # The CLI starts the hook after the command ends: nothing the command wrote is newer than that.
    assert hook.bash_window({"duration_ms": 40}, "F", 100.0) == (100.0 - 40 / 1000 - 1, 100.0)
    assert hook.bash_window({}, hook.NOBODY, 100.0) == (99.0, 100.0)
    (hook.SEEN).mkdir(parents=True, exist_ok=True)
    (hook.SEEN / "F").write_text("90.0")
    assert hook.bash_window({}, "F", 100.0) == (89.0, 100.0)


@pytest.mark.parametrize("broken", ["BROKEN\n", HAZARD])  # a project's test, and a purity hazard
def test_a_shell_revert_is_tested_so_the_next_break_wakes_its_culprit(root, broken):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/a.js", broken, "A").returncode == 2
    git(root, "checkout", "--", "ui/a.js")  # A's command reverts it
    assert run_hook(root, as_session(bash(), "A", duration_ms=300)).returncode == 0
    assert "0 red signature(s)" in red(root), "the revert is a change: it was tested, and the failure cleared"
    again = session_edit(root, "ui/a.js", broken, "C")
    assert again.returncode == 2, "C broke it again, and hears it"
    assert "owners: C;" in red(root)


def test_a_late_claim_that_joins_an_incomplete_verdict_stays_a_suspect(root):
    add_rack_project(root, gate=True)
    warm(root)
    (root / "ui" / "gate.txt").write_text("BROKEN\n")  # the operator: no test file loads
    time.sleep(1.2)
    assert run_hook(root, as_session(bash(), "X", duration_ms=100)).returncode == 0  # queued unowned, and run
    time.sleep(1.5)  # B's command, its window's second before it included, starts after that pass
    started = time.time()
    (root / "ui" / "b.js").write_text("BROKEN\n")  # early in B's long command
    time.sleep(1.2)
    assert run_hook(root, as_session(bash(), "Y", duration_ms=100)).returncode == 0  # not Y's; Y's call runs that pass
    assert run_hook(root, bash_since(started, "B")).returncode == 0  # B's command ends: that pass already saw b.js
    (root / "ui" / "gate.txt").write_text("ok\n")  # the operator again: now every test runs
    time.sleep(1.2)
    assert run_hook(root, as_session(bash(), "Z", duration_ms=100)).returncode == 0
    revealed = next_call(root, "B")
    assert revealed.returncode == 2 and red_test("b.js") in yours(revealed)


def test_a_complete_run_spends_only_the_suspects_it_could_see(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    ns, signature = "tests lib (node)", "tests lib (node) | each.test.js > clean b.js"
    ledger = {**hook.new_ledger(), "created": 0.0, "vouched": {ns: 10.0}}
    hook.suspect(ledger, ns, hook.NOBODY, 60.0, "lib/b.js")  # a pull at 60...
    green = hook.tests_record(hook.Project("node", root / "lib"), hook.Ran(0, "ok"))
    hook.vouch(ledger, green, [], 50.0, True)  # ...while a pass that began reading at 50 ran: it passed on its copy
    assert hook.NOBODY in ledger["suspects"][ns], "that pass never saw the pull"
    pulled = hook.Record("node tests (lib) FAILED", ns, {signature: None}, True, [], frozenset({ns}))

    def change(at: float) -> object:
        return hook.Entry("B", f"toolu_b{int(at)}", "lib/c.js", at, at)

    assert hook.judge(ledger, pulled, [change(70.0)], 80.0, 90.0, True) == {"B": [signature]}  # it may be B's
    assert ledger["reds"][signature]["owners"] == ["-", "B"]
    assert hook.judge(ledger, pulled, [change(100.0)], 110.0, 120.0, True) == {}, "told once, never pinned"


@pytest.mark.parametrize("first", ["an Edit", "a command"])
def test_a_claim_an_overlapping_command_joins_keeps_its_first_claimant_from_a_second_wake(root, monkeypatch, first):
    add_rack_project(root)
    warm(root)
    time.sleep(1.5)  # nothing before this changes inside B's window
    started = time.time()
    (root / "ui" / "a.js").write_text("y\n")  # B's long command writes a.js...
    if first == "an Edit":
        broke = session_edit(root, "ui/a.js", "BROKEN\n", "A")  # ...and A Edits what it wrote, while it still runs
    else:
        (root / "ui" / "a.js").write_text("BROKEN\n")  # ...or A's command, which overlapped B's, wrote it again
        broke = run_hook(root, bash_since(started, "A"))
    assert broke.returncode == 2
    overlapping = run_hook(root, bash_since(started, "B"))
    assert overlapping.returncode == 2, "A's change may keep what B's command wrote: both are told"
    diff = run_hook(root, bash_since(started, "A"))  # A's `git diff`, sent in the same message as its change
    assert diff.returncode == 0, diff.stderr
    assert json.loads(load_hook(root, monkeypatch).OWNERS.read_text())["ui/a.js"]["session"] == "A", "still its last owner"


def test_a_deletion_first_seen_after_a_neighbour_touched_its_directory_stays_its_deleters(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    hook = load_hook(root, monkeypatch)
    (root / "ui" / "e.js").unlink()  # D's `rm`
    started = time.time()  # D's hook starts...
    time.sleep(0.05)
    (root / "ui" / "scratch.tmp").write_text("x\n")  # ...and a neighbour's save lands beside it before D's hook looks
    payload = as_session(bash(), "D", duration_ms=300)
    hook.claim(payload, "D", payload["tool_use_id"], started, started + 60)
    queued = hook.read_entries(hook.QUEUE / f"{UI}.jsonl")
    assert [(e.owner, e.call) for e in queued if e.path == "ui/e.js"] == [("D", payload["tool_use_id"])]


def test_mail_whose_failure_was_fixed_meanwhile_is_not_delivered(root, monkeypatch, capsys):
    hook = load_hook(root, monkeypatch)
    ledger = hook.new_ledger()
    ledger["reds"][red_test("a.js")] = {"owners": ["A"], "first_red_end": 1.0, "last_red": 1.0, "after": 0.0}
    hook.write_json(hook.LEDGER, ledger)
    title = "node tests (ui) FAILED"
    fixed = {"title": title, "mine": [red_test("b.js")], "text": f"{title}\nb.js, fixed since"}
    hook.post_mail("A", UI, ["ui/b.js"], [fixed])  # left for A's next call, and b.js went green before it came
    assert hook.deliver("A") == 0
    hook.post_mail("A", UI, ["ui/b.js"], [fixed])
    hook.post_mail("A", "purity-files", ["ui/a.js"], [{"title": "p", "mine": [red_test("a.js")], "text": "a.js, still red"}])
    assert hook.deliver("A") == 2
    shown = capsys.readouterr().err
    assert "still red" in shown and "fixed since" not in shown and shown.startswith("After editing ui/a.js:")


def test_a_change_made_outside_every_window_tells_the_files_last_claimant_once(root):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/b.js", "y\n", "B").returncode == 0  # B claims b.js
    time.sleep(1.2)
    (root / "ui" / "b.js").write_text("BROKEN\n")  # later, and no session's: a background job, the operator's editor
    time.sleep(1.5)
    assert run_hook(root, as_session(bash(), "E", duration_ms=100)).returncode == 0  # E's `git status` sees it
    told = next_call(root, "B")
    assert told.returncode == 2 and red_test("b.js") in yours(told)  # it may be B's: B hears
    for name in ("c.js", "e.js"):
        assert session_edit(root, f"ui/{name}", "y\n", "B").returncode == 0, "once: it may be nobody's"
    assert "owners: - (unowned), B;" in red(root)


def test_a_change_queued_under_an_owner_only_as_its_files_last_claimant_never_tells_it_again(root):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/b.js", "BROKEN\n", "C").returncode == 2  # C broke it, and heard
    time.sleep(1.2)
    (root / "ui" / "b.js").write_text("BROKEN\n// saved by the operator's editor\n")  # outside every window
    time.sleep(1.5)
    assert run_hook(root, as_session(bash(), "E", duration_ms=100)).returncode == 0
    assert next_call(root, "C").returncode == 0, "queued under C as b.js's last claimant: a guess, not C's change"
    again = session_edit(root, "ui/c.js", "y\n", "C")
    assert again.returncode == 2 and red_test("b.js") in yours(again)  # C's own next change: still its red


def test_a_late_claim_of_a_file_an_earlier_scan_read_joins_that_scan(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    time.sleep(1.5)  # nothing before this changes inside E's window
    started = time.time()
    (root / "ui" / "a.js").write_text("y\n")  # written while both D's and E's commands ran
    assert run_hook(root, bash_since(started, "D")).returncode == 0  # D's call scans a.js...
    assert session_edit(root, "ui/b.js", "y\n", "B").returncode == 0  # ...and B's scans only b.js, after it
    hook = load_hook(root, monkeypatch)
    ran: list[str] = []
    real = hook.run_pass
    monkeypatch.setattr(
        hook, "run_pass", lambda runner, batch, deadline: ran.append(runner.name) or real(runner, batch, deadline)
    )
    payload = bash_since(started, "E")  # E's command overlapped a.js's write too: it claims it, late
    now = time.time()
    waiting = hook.claim(payload, "E", payload["tool_use_id"], now, now + 120)
    assert waiting == {"purity-files", UI}
    hook.settle(waiting, payload["tool_use_id"], now + 120)
    assert ran == [], "both checks already read that a.js: E joins their verdicts"
    kept = json.loads((hook.VERDICTS / "purity-files.json").read_text())
    assert {"ui/a.js", "ui/b.js"} <= set(kept["covers"]) and "scope" not in kept["records"][0]


def test_a_head_move_is_measured_from_the_latest_look_at_head(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    hook = load_hook(root, monkeypatch)
    early = time.time()
    time.sleep(0.2)
    assert session_edit(root, "ui/a.js", "y\n", "A").returncode == 0
    late = time.time()

    def call(session: str, started: float) -> None:
        payload = as_session(edit(root, "../elsewhere.txt"), session)
        hook.claim(payload, session, payload["tool_use_id"], started, started + 60)

    call("C", late)
    call("B", early)  # a call that started before A's Edit, and took the state lock after C
    assert json.loads(hook.HEAD_SEEN.read_text())["at"] == late
    commit(root, "A's edit lands")
    call("X", time.time())
    assert not hook.load_ledger()["suspects"], "a commit of A's own Edit rewrote nothing"


def test_a_second_hit_in_a_file_already_red_is_its_own_writers(root):
    add_rack_project(root)
    warm(root)
    assert session_edit(root, "ui/a.js", HAZARD, "A").returncode == 2
    second = session_edit(root, "ui/a.js", HAZARD + OTHER_HAZARD, "B")  # B writes one more home path there
    assert second.returncode == 2 and only_hazard(second, "ui/a.js"), "the moment it is written"
    assert session_edit(root, "ui/a.js", OTHER_HAZARD, "A").returncode == 0, "A took its own out: what is left is B's"
    copied = session_edit(root, "ui/a.js", OTHER_HAZARD * 2, "D")  # D copies B's line: one more of the same hit
    assert copied.returncode == 2 and only_hazard(copied, "ui/a.js")
    assert "owners: B, D;" in red(root)


def test_a_second_failure_behind_a_red_that_names_no_test_reaches_its_culprit(root):
    (root / "lib").mkdir()
    (root / "lib" / "package.json").write_text(json.dumps({"scripts": {"test": "node check.js"}}))
    for name in FILES:
        (root / "lib" / name).write_text("x\n")
    (root / "lib" / "check.js").write_text(  # a test that exits 1 and names nothing: a crash, a loader, a hang
        "const fs = require('fs');\n"
        "const bad = fs.readdirSync(__dirname).some((n) => n.endsWith('.js') && "
        "fs.readFileSync(__dirname + '/' + n, 'utf8').includes('BRO' + 'KEN'));\n"
        "process.exitCode = bad ? 1 : 0;\n"
    )
    commit(root, "lib")
    assert session_edit(root, "lib/d.js", "x\n", "W").returncode == 0  # a green run
    broke = session_edit(root, "lib/a.js", "BROKEN\n", "A")
    assert broke.returncode == 2 and yours(broke) == "yours: tests lib (node) | exit 1"
    assert session_edit(root, "lib/b.js", "BROKEN\n", "C").returncode == 0  # hidden behind A's red: nothing to tell yet
    still = session_edit(root, "lib/a.js", "x\n", "A")
    assert still.returncode == 2, "A's fix left it red: A hears that"
    told = next_call(root, "C")
    assert told.returncode == 2 and yours(told) == "yours: tests lib (node) | exit 1"  # what is left may be C's
    assert session_edit(root, "lib/c.js", "y\n", "A").returncode == 0, "and nobody is told it twice"


def test_a_test_seen_passing_clears_while_another_file_fails_to_load(root):
    add_rack_project(root, directory="lib")
    (root / "lib" / "gate.txt").write_text("ok\n")
    (root / "lib" / "other.test.js").write_text(  # throws at load while gate.txt holds BROKEN; each.test.js still runs
        "const fs = require('fs'), path = require('path');\n"
        "if (fs.readFileSync(path.join(__dirname, 'gate.txt'), 'utf8').includes('BROKEN')) throw new Error('gate');\n"
        "require('node:test')('other', () => {});\n"
    )
    commit(root, "a second test file")
    assert session_edit(root, "lib/d.js", "x\n", "W").returncode == 0  # a green run
    assert session_edit(root, "lib/a.js", "BROKEN\n", "A").returncode == 2
    assert session_edit(root, "lib/gate.txt", "BROKEN\n", "X").returncode == 2  # a neighbour's WIP: other.test.js won't load
    assert session_edit(root, "lib/a.js", "x\n", "A").returncode == 0  # each.test.js ran, and passed
    again = session_edit(root, "lib/a.js", "BROKEN\n", "C")
    assert again.returncode == 2 and yours(again) == "yours: tests lib (node) | each.test.js > clean a.js"
    assert next_call(root, "A").returncode == 0 and next_call(root, "X").returncode == 0, "both changes were seen green"


def test_a_bash_call_whose_git_ran_across_an_edit_keeps_that_edits_claim(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    time.sleep(1.5)  # nothing before this changes inside L's window
    hook = load_hook(root, monkeypatch)
    real = hook.dirty_files
    edited: list[float] = []

    def status_then_an_edit() -> object:
        found = real()
        edited.append(time.time())
        assert session_edit(root, "ui/a.js", "BROKEN\n", "A").returncode == 2  # after git looked, before Q's lock
        return found

    monkeypatch.setattr(hook, "dirty_files", status_then_an_edit)
    payload = as_session(bash(), "Q", duration_ms=100)
    now = time.time()
    hook.claim(payload, "Q", payload["tool_use_id"], now, now + 60)
    assert json.loads(hook.OWNERS.read_text())["ui/a.js"]["session"] == "A"
    assert "ui/a.js" in json.loads(hook.STAMPS.read_text())
    long_command = run_hook(root, bash_since(edited[0], "L"))  # it ran across A's Edit, and wrote nothing
    assert long_command.returncode == 0, long_command.stderr


def test_a_live_runner_keeps_its_lock_however_long_the_wall_clock_says_it_held_it(root, monkeypatch):
    add_rack_project(root)
    warm(root)
    hook = load_hook(root, monkeypatch)
    assert hook.try_lock(UI)  # this test is the project's runner now
    try:
        old = time.time() - hook.LOCK_STALE_S - 60
        os.utime(hook.STATE / f"{UI}.lock", (old, old))  # the laptop slept mid-pass: the wall clock jumped
        (root / "ui" / "a.js").write_text("BROKEN\n")
        now = time.time()
        hook.write_entries(hook.INFLIGHT / f"{UI}.jsonl", [hook.Entry("A", "toolu_held", "ui/a.js", now, now)])
        tested = runs(root)
        assert next_call(root, "B").returncode == 0
        assert runs(root) == tested, "a live runner's batch stays its own"
    finally:
        hook.release(UI)
    assert next_call(root, "B").returncode == 0 and runs(root) == tested + 1


def test_an_unowned_change_co_owns_a_red_however_it_reaches_the_ledger(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    signature = red_test("a.js")
    record = hook.Record("node tests (ui) FAILED", "tests ui (node)", {signature: None}, True, [], frozenset({"tests ui (node)"}))

    def change(owner: str, at: float) -> object:
        return hook.Entry(owner, f"toolu_{int(at)}", "ui/a.js", at, at)

    for late in (False, True):
        ledger = {**hook.new_ledger(), "created": 0.0, "vouched": {"tests ui (node)": 8.0}}
        if late:  # an editor's save the pass saw, queued after it by a later call
            hook.judge(ledger, record, [change("A", 10.0)], 50.0, 100.0, True)
            hook.judge(ledger, record, [change(hook.NOBODY, 20.0)], 50.0, 100.0, False)
        else:
            hook.judge(ledger, record, [change("A", 10.0), change(hook.NOBODY, 20.0)], 50.0, 100.0, True)
        assert ledger["reds"][signature]["owners"] == ["-", "A"], late
        assert hook.judge(ledger, record, [change("A", 150.0)], 160.0, 200.0, True) == {}, "told once"


def test_a_red_two_sessions_co_own_is_told_once_to_each_and_never_pinned_on_one(root, monkeypatch):
    hook = load_hook(root, monkeypatch)
    signature = red_test("a.js")
    record = hook.Record("node tests (ui) FAILED", "tests ui (node)", {signature: None}, True, [], frozenset({"tests ui (node)"}))
    ledger = {**hook.new_ledger(), "created": 0.0, "vouched": {"tests ui (node)": 8.0}}

    def change(owner: str, name: str, at: float) -> object:
        return hook.Entry(owner, f"toolu_{owner}{int(at)}", f"ui/{name}", at, at)

    # A broke a.js and B made a harmless edit to b.js in the same batch: the pass cannot tell them apart.
    assert hook.judge(ledger, record, [change("A", "a.js", 10.0), change("B", "b.js", 12.0)], 50.0, 100.0, True) == {
        "A": [signature],
        "B": [signature],
    }
    for at in (150.0, 250.0):  # B keeps editing b.js while A's failure stands: told once, so never again
        assert hook.judge(ledger, record, [change("B", "b.js", at)], at + 10, at + 50, True) == {}
    assert hook.judge(ledger, record, [change("A", "c.js", 350.0)], 360.0, 400.0, True) == {}
    assert ledger["reds"][signature]["owners"] == ["A", "B"]


def test_a_bystander_whose_command_spanned_a_break_is_told_once(root):
    add_rack_project(root)
    warm(root)
    (root / "ui" / "a.js").write_text("BROKEN\n")  # A's command wrote it; B's read-only command ran across it
    assert run_hook(root, as_session(bash(), "A", duration_ms=2000)).returncode == 2
    assert run_hook(root, as_session(bash(), "B", duration_ms=60_000)).returncode == 2  # the accepted ambiguity
    for content in ("y\n", "z\n"):
        later = session_edit(root, "ui/b.js", content, "B")
        assert later.returncode == 0, later.stderr  # and never again while A's failure stands
    assert "A, B" in red(root)


def test_mail_posted_while_a_delivery_reads_the_ledger_is_kept(root, monkeypatch, capsys):
    hook = load_hook(root, monkeypatch)
    signature = red_test("a.js")
    hook.write_json(hook.LEDGER, hook.new_ledger())
    title = "node tests (ui) FAILED"
    read = hook.load_ledger

    def a_runner_settles_meanwhile() -> dict:
        seen = read()
        ledger = read()  # a runner writes its new red, then mails its owner, right after this read
        ledger["reds"][signature] = {"owners": ["A"], "first_red_end": 1.0, "last_red": 1.0, "after": 0.0}
        hook.write_json(hook.LEDGER, ledger)
        hook.post_mail("A", UI, ["ui/a.js"], [{"title": title, "mine": [signature], "text": f"{title}\na.js"}])
        return seen

    monkeypatch.setattr(hook, "load_ledger", a_runner_settles_meanwhile)
    first = hook.deliver("A")
    monkeypatch.setattr(hook, "load_ledger", read)
    assert first == 2 or hook.deliver("A") == 2, "the report reaches A, now or on its next call"
    assert "a.js" in capsys.readouterr().err


def test_no_process_a_test_left_outlives_its_run(root):
    (root / "ui").mkdir()
    (root / "ui" / "package.json").write_text("{}")
    (root / "ui" / "a.js").write_text("x\n")
    (root / "ui" / "leak.test.js").write_text(
        "const left = require('child_process').spawn('sleep', ['60'], { stdio: 'ignore' });\n"
        "left.unref();\n"
        "require('fs').writeFileSync(process.env.MARKERS + '/LEFT', String(left.pid));\n"
        "require('node:test')('passes', () => {});\n"
    )
    commit(root, "ui")
    (root / "ui" / "a.js").write_text("changed\n")
    assert call_hook(root, as_session(bash(), "A", duration_ms=500)) == 0
    left = int(marker(root, "LEFT").read_text())
    try:
        for _ in range(50):
            try:
                os.kill(left, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("a server the test forgot kept running after its run passed")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(left, signal.SIGKILL)


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGKILL])  # Claude Code's abort, and its hook timeout
def test_no_test_outlives_the_hook_that_started_it(root, stop):
    (root / "ui").mkdir()
    (root / "ui" / "package.json").write_text("{}")
    (root / "ui" / "a.js").write_text("x\n")
    (root / "ui" / "slow.test.js").write_text(
        "require('fs').writeFileSync(process.env.MARKERS + '/TESTPID', String(process.pid));\n"
        "require('node:test')('slow', () => new Promise((done) => setTimeout(done, 60000)));\n"
    )
    commit(root, "ui")
    (root / "ui" / "a.js").write_text("changed\n")
    hook = subprocess.Popen(  # its own process group, as Claude Code starts a hook: that group is what it kills
        hook_command(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=hook_env(root),
        start_new_session=True,
    )
    hook.stdin.write(json.dumps(as_session(bash(), "A", duration_ms=500)).encode())
    hook.stdin.close()
    started = marker(root, "TESTPID")
    for _ in range(300):
        if started.exists() and started.read_text():
            break
        time.sleep(0.1)
    test = int(started.read_text())
    try:
        os.killpg(hook.pid, stop)
        hook.wait(timeout=10)
        for _ in range(50):
            try:
                os.kill(test, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the test kept running after its hook was killed")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(test, signal.SIGKILL)
