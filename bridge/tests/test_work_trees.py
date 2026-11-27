"""Where an agent works (work_trees.py) and which checkout holds a path (checkouts.py), over real git
repositories in `tmp_path`: a linked tree's `.git` file is git's own, never a hand-written fake."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tldrack.checkouts import checkout_root, linked_tree, linked_tree_holding, repo_identity
from tldrack.work_trees import (
    ADDED,
    CD,
    EDITED,
    REMOVED,
    SessionWorkTreeStore,
    TreeResolver,
    evidence,
    fold,
    row_field,
)


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A main checkout with one commit, and a linked tree `feat/x` beside it."""
    main = tmp_path / "repo"
    main.mkdir()
    git("init", "-q", "-b", "main", cwd=main)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one", cwd=main)
    tree = tmp_path / "trees" / "x"
    git("worktree", "add", "-q", "-b", "feat/x", str(tree), cwd=main)
    return main, tree


# ── checkouts.py ─────────────────────────────────────────────────────────────


def test_a_linked_tree_names_its_branch_and_main_checkout(repo):
    main, tree = repo
    found = linked_tree(str(tree))
    assert found is not None and found.path == str(tree) and found.branch == "feat/x" and found.main == str(main)
    assert linked_tree(str(main)) is None, "a main checkout is not a linked tree"
    assert repo_identity(str(tree)) == repo_identity(str(main)) == os.path.realpath(main / ".git")


def test_a_detached_tree_has_no_branch(repo):
    main, _ = repo
    detached = main.parent / "trees" / "d"
    git("worktree", "add", "-q", "--detach", str(detached), cwd=main)
    found = linked_tree(str(detached))
    assert found is not None and found.branch is None


def test_any_path_inside_a_tree_finds_it_existing_or_not(repo):
    main, tree = repo
    assert linked_tree_holding(str(tree / "src" / "new_file.py")).path == str(tree)
    assert linked_tree_holding(str(main / "README.md")) is None
    assert checkout_root(str(main / "a" / "b")) == str(main)
    assert checkout_root("/") is None or checkout_root("/") == "/"


# ── evidence: what one tool call says ────────────────────────────────────────

CWD = "/repo"


@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("git worktree add ~/worktrees/tldrack/x -b feat/x", [(ADDED, os.path.expanduser("~/worktrees/tldrack/x"))]),
        ("git worktree add -b feat/x ../trees/x", [(ADDED, "/trees/x")]),
        ("git worktree add -bfeat/x ../trees/x main", [(ADDED, "/trees/x")]),
        ("git worktree add --reason 'long run' -f --lock /abs/t HEAD", [(ADDED, "/abs/t")]),
        ("git worktree add --detach -- -odd-name", [(ADDED, "/repo/-odd-name")]),
        ("git -C /other worktree add t", [(ADDED, "/other/t")]),
        ("git -C /other -C sub worktree add t", [(ADDED, "/other/sub/t")]),
        ("git -c core.x=1 worktree add t", [(ADDED, "/repo/t")]),
        ("cd /main && git worktree add ../t -b b", [(CD, "/main"), (ADDED, "/t")]),
        ('cd "/with space" ; git worktree add "a b"', [(CD, "/with space"), (ADDED, "/with space/a b")]),
        ("FOO=1 sudo git worktree add t", [(ADDED, "/repo/t")]),
        ("git worktree remove --force /trees/x", [(REMOVED, "/trees/x")]),
        ("git worktree move /trees/x /trees/y", [(REMOVED, "/trees/x"), (ADDED, "/trees/y")]),
        ("pushd /trees/x\nnpm test", [(CD, "/trees/x")]),
        # Read as text, never run: none of these is git adding a tree.
        ("echo git worktree add x", []),
        ("git worktree list", []),
        ("git worktree add", []),
        ("git status && ls", []),
        ("cd -", []),
        ("cd", []),
        ("# git worktree add x", []),
        ("git worktree add 'unterminated", [(ADDED, "/repo/'unterminated")]),
    ],
)
def test_bash_evidence(command, found):
    assert evidence("Bash", {"command": command}, CWD) == found


def test_edit_evidence_reads_the_file_the_tool_writes():
    assert evidence("Edit", {"file_path": "/trees/x/a.py", "old_string": "a"}, CWD) == [(EDITED, "/trees/x/a.py")]
    assert evidence("Write", {"file_path": "rel/b.py"}, CWD) == [(EDITED, "/repo/rel/b.py")]
    assert evidence("NotebookEdit", {"notebook_path": "/n.ipynb"}, CWD) == [(EDITED, "/n.ipynb")]
    assert evidence("Write", {"file_path": "rel.py"}, None) == [], "a relative path with no cwd says nothing"


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [("Read", {"file_path": "/trees/x/a.py"}), ("Bash", {"command": 5}), ("Bash", "git worktree add x"), ("Edit", {})],
)
def test_nothing_else_is_evidence(tool, tool_input):
    assert evidence(tool, tool_input, CWD) == []


def test_a_huge_command_is_read_by_its_head():
    command = "git worktree add /t\n" + "x" * 100_000
    assert evidence("Bash", {"command": command}, CWD) == [(ADDED, "/t")]


# ── fold: newest wins, and what lets go ──────────────────────────────────────


def test_newest_evidence_wins_and_a_main_edit_lets_go(repo):
    main, tree = repo
    held = fold(None, [(ADDED, str(tree))], 1.0)
    assert held == {"path": str(tree), "kind": ADDED, "at": 1.0}
    held = fold(held, [(EDITED, str(tree / "pkg" / "a.py"))], 2.0)
    assert held == {"path": str(tree), "kind": EDITED, "at": 2.0}, "the tree's root is held, not the file"
    assert fold(held, [(EDITED, "/tmp/notes.md")], 3.0) == held, "outside every checkout: no word"
    assert fold(held, [(CD, str(main))], 3.0) == held, "a cd to main is a read, not a move"
    assert fold(held, [(EDITED, str(main / "README.md"))], 4.0) is None, "editing main: it went back"


def test_a_cd_moves_only_into_a_linked_tree(repo):
    _, tree = repo
    assert fold(None, [(CD, str(tree / "sub"))], 1.0) == {"path": str(tree), "kind": CD, "at": 1.0}
    assert fold(None, [(CD, "/nowhere")], 1.0) is None


def test_a_removed_tree_is_let_go_and_another_is_not(repo):
    _, tree = repo
    held = {"path": str(tree), "kind": ADDED, "at": 1.0}
    assert fold(held, [(REMOVED, "/elsewhere")], 2.0) == held
    assert fold(held, [(REMOVED, str(tree))], 2.0) is None


# ── the store ────────────────────────────────────────────────────────────────


def test_the_store_keeps_the_tree_across_a_restart_and_writes_once_per_move(repo, tmp_path):
    _, tree = repo
    path = tmp_path / "work-trees.json"
    store = SessionWorkTreeStore(path)
    assert store.observe("vc-1", [(ADDED, str(tree))], now=1.0)
    assert not store.observe("vc-1", [(ADDED, str(tree))], now=2.0), "the same tree by the same evidence"
    mtime = path.stat().st_mtime_ns
    assert store.observe("vc-1", [(EDITED, str(tree / "a.py"))], now=3.0), "new evidence kind"
    assert not store.observe("vc-1", [(EDITED, str(tree / "b.py"))], now=4.0)
    assert not store.observe("vc-1", [], now=5.0)
    assert path.stat().st_mtime_ns >= mtime
    again = SessionWorkTreeStore(path)
    assert again.get("vc-1") == {"path": str(tree), "kind": EDITED, "at": 3.0}
    assert again.rename("vc-1", "vc-2") and again.get("vc-2") is not None and again.get("vc-1") is None
    assert again.forget("vc-2") and SessionWorkTreeStore(path).get("vc-2") is None


def test_a_hand_edited_entry_is_dropped_alone(tmp_path):
    path = tmp_path / "work-trees.json"
    path.write_text(
        '{"version": 1, "sessions": {"ok": {"path": "/t", "kind": "cd", "at": 1},'
        ' "bad": {"path": "relative", "kind": "cd", "at": 1}, "worse": {"path": "/t", "kind": "removed", "at": 1}}}'
    )
    store = SessionWorkTreeStore(path)
    assert store.get("ok") == {"path": "/t", "kind": "cd", "at": 1.0}
    assert store.get("bad") is None and store.get("worse") is None


# ── the row ──────────────────────────────────────────────────────────────────


def test_the_cwd_wins_then_the_evidence_then_nothing(repo):
    main, tree = repo
    resolver = TreeResolver()
    held = {"path": str(tree), "kind": ADDED, "at": 1.0}
    expected = {"path": str(tree), "branch": "feat/x", "main": str(main)}
    assert row_field(str(tree / "sub"), None, resolver) == {**expected, "by": "cwd"}
    assert row_field(str(main), held, resolver) == {**expected, "by": ADDED}
    assert row_field(str(main), None, resolver) is None
    assert row_field(None, {"path": "/never/created", "kind": ADDED, "at": 1.0}, resolver) is None


def test_a_tree_resolves_once_it_exists_and_stops_once_removed(repo):
    """A `PreToolUse` sees `git worktree add` before git runs it: the row waits for the tree."""
    main, _ = repo
    clock = [0.0]
    resolver = TreeResolver(clock=lambda: clock[0])
    later = main.parent / "trees" / "later"
    held = {"path": str(later), "kind": ADDED, "at": 1.0}
    assert row_field(str(main), held, resolver) is None
    git("worktree", "add", "-q", "-b", "feat/later", str(later), cwd=main)
    assert row_field(str(main), held, resolver)["branch"] == "feat/later", "a miss is never cached: no lag"
    git("worktree", "remove", str(later), cwd=main)
    assert row_field(str(main), held, resolver) is not None, "a found tree is believed for a moment"
    clock[0] += 10
    assert row_field(str(main), held, resolver) is None
