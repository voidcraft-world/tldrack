"""The edit hook runs tests on a snapshot of only what git or the agent vouches for.

Each case builds a throwaway repository holding copies of the hook, the purity test
and `scripts/projects.py`, with the hook's cache beside it (never inside it), then
plays tool calls in the way Claude Code does: `--pre` before an Edit, the plain call
after any tool. Test files and `test` scripts drop marker files in `$MARKERS`, OUTSIDE
both, so "did it execute" is a file on disk, not a log line. (The directory goes by
environment, never written into a scanned file: a temp path can hold a user name,
and the purity scan rightly refuses it.) The trust rules are owned by
the hook's docstring (`.claude/hooks/affected_tests.py`, WHAT IT WILL EXECUTE).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
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


def call_hook(repo: Path, payload: dict, *args: str) -> int:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(repo.parent)),
        "CLAUDE_PROJECT_DIR": str(repo),
        "XDG_CACHE_HOME": str(repo.parent / "cache"),
        "MARKERS": str(repo.parent),
    }
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(repo / HOOK), *args], input=json.dumps(payload), text=True, capture_output=True, env=env
    )
    return done.returncode


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
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root.parent / "cache"))
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", root / HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
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
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root.parent / "cache"))
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", root / HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
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
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root.parent / "cache"))
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", root / HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
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
