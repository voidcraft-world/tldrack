"""Which git checkout holds a path, and is it a linked worktree? Read off git's own files, never a fork.

Pure and stdlib-only: the hook path calls it, so no `git` subprocess runs per tool call, and nothing
here caches. A caller that asks often caches for itself, with a lifetime that suits it: `missions.py`
keeps its answers for good (a file an agent edited does not change checkout), `work_trees.py` only
for seconds (a tree an agent is about to create does not exist yet when its command is first seen).

THE FILES. A main checkout's `.git` is a directory. A linked worktree's `.git` is a FILE,
`gitdir: <common dir>/worktrees/<name>`, whose private directory holds its own `HEAD` and a
`commondir` leading back to the shared one; the main checkout is the shared directory's parent. A
submodule's `.git` file points at `<super>/.git/modules/<name>`, which has no `commondir`: a
repository of its own, never a linked tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: What `HEAD` holds on a branch; anything else (a bare sha) is a detached head.
BRANCH_REF = "ref: refs/heads/"


def checkout_root(directory: str) -> str | None:
    """The nearest ancestor of `directory` (itself included) holding `.git`, a file in a worktree and a
    directory otherwise; None when no ancestor does. `directory` need not exist: the walk climbs past it."""
    current = os.path.normpath(directory)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _gitdir_of(root: str) -> str | None:
    """The private git directory a `.git` FILE names, or None (a `.git` directory, or no pointer)."""
    try:
        with open(os.path.join(root, ".git"), encoding="utf-8") as handle:
            pointer = handle.read().strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    return os.path.normpath(os.path.join(root, pointer.removeprefix("gitdir:").strip()))


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def repo_identity(root: str) -> str:
    """One name for a repository and all its worktrees: its common git directory (module note). A
    submodule is a repository of its own; a checkout git cannot describe is named by its own path."""
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return os.path.realpath(dot_git)
    private = _gitdir_of(root)
    if private is None:
        return os.path.realpath(root)
    common = _read(os.path.join(private, "commondir"))
    return os.path.realpath(os.path.join(private, common) if common is not None else private)


@dataclass(frozen=True)
class LinkedTree:
    """A linked worktree: where it is, the branch it has checked out (None when detached), and the main
    checkout it belongs to."""

    path: str
    branch: str | None
    main: str


def linked_tree(root: str) -> LinkedTree | None:
    """`root` as a linked worktree, or None for a main checkout, a submodule, a bare repository's
    worktree with no main checkout, or anything git's files do not describe (module note)."""
    private = _gitdir_of(root)
    if private is None:
        return None
    common = _read(os.path.join(private, "commondir"))
    if common is None:
        return None  # a submodule
    common_dir = os.path.normpath(os.path.join(private, common))
    if os.path.basename(common_dir) != ".git":
        return None  # a bare repository: no main checkout to fold the tree onto
    head = _read(os.path.join(private, "HEAD")) or ""
    branch = head.removeprefix(BRANCH_REF) if head.startswith(BRANCH_REF) else None
    return LinkedTree(path=os.path.normpath(root), branch=branch or None, main=os.path.dirname(common_dir))


def linked_tree_holding(path: str) -> LinkedTree | None:
    """The linked worktree whose checkout holds `path` (a file or a directory, existing or not), or None."""
    root = checkout_root(path)
    return linked_tree(root) if root is not None else None
