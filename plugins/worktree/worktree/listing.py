"""Enumerate worktrees. `git worktree list --porcelain` is the whole source of
truth; nothing here is remembered.

── Why there is no stored session→worktree mapping ────────────────────────
git already knows every worktree and its branch, and `names.session_name_for` is
a pure function of the branch — so the join is fully recomputable on every call.
A store would be a second copy able to disagree with git (a worktree removed by
hand, a branch renamed) and would inherit `/rename-session`'s key-moving problem
for nothing. See `store.py`, which persists only what genuinely cannot be
recomputed: the outcome of the PR job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 10

BRANCH_REF_PREFIX = "refs/heads/"


def parse_worktree_list(text: object) -> list[dict]:
    """Parse `git worktree list --porcelain` into records. Pure.

    The format is stanzas separated by blank lines, each starting with a
    `worktree <path>` line. A stanza carries either `branch refs/heads/<name>`
    or the bare word `detached`, and may carry `locked [<reason>]` and
    `prunable <reason>` (a worktree whose directory is gone — `git worktree
    prune` will drop it). Unknown keys are ignored rather than refused — git
    has added lines to this format before and will again.
    """
    if not isinstance(text, str):
        return []
    records: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("worktree "):
            current = {
                "path": line[len("worktree ") :],
                "branch": None,
                "head": None,
                "detached": False,
                "prunable": False,
                "prunable_reason": None,
                "locked": False,
            }
            records.append(current)
            continue
        if current is None:
            continue  # a stray line before any stanza — skip rather than guess
        if line.startswith("branch "):
            ref = line[len("branch ") :]
            current["branch"] = ref.removeprefix(BRANCH_REF_PREFIX)
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line == "detached":
            current["detached"] = True
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
            current["prunable_reason"] = line[len("prunable ") :].strip() or None
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    return records


def main_worktree_path(records: list[dict]) -> str | None:
    """The main checkout's path: the FIRST stanza, which is where git puts it.

    Not `context.start_dir`: the bridge may be started inside a worktree (or a
    subdirectory), and every worktree still lists the same main checkout first.
    """
    if not records:
        return None
    path = records[0].get("path")
    return path if isinstance(path, str) and path else None


def worktree_by_branch(records: list[dict]) -> dict[str, dict]:
    """`{branch: record}` for every stanza that holds a branch. Pure.

    A branch can be checked out in at most one worktree (git refuses a second
    `worktree add` of it), so the first stanza per branch is the only one.
    """
    by_branch: dict[str, dict] = {}
    for record in records:
        branch = record.get("branch")
        if isinstance(branch, str) and branch and branch not in by_branch:
            by_branch[branch] = record
    return by_branch


def list_worktrees(repo_root: Path) -> list[dict]:
    """Every worktree git knows about, or `[]` when git cannot answer.

    An empty list on failure rather than an exception: `GET /state` is polled,
    and a transiently wedged git must render as "no worktrees" for one tick
    rather than a 500 the panel has to special-case.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return parse_worktree_list(result.stdout)
