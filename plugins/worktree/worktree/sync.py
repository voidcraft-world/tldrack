"""`wt sync <branch>` — bring a LONG-LIVED worktree branch up to date with main.

Most worktrees here live for one task and leave through `PR`. A few do not: a
months-long migration branch, say. A branch like that drifts
unless main flows into it regularly, and this command is that flow. It stops at
the one part a command should not do: resolving conflicts is left to the caller,
with the merge in progress and the files named on stdout.

── Merge, never rebase ─────────────────────────────────────────────────────
A rebase rewrites the branch. Every sync would then need a force-push, and the
same conflicts would come back for every replayed commit. A merge resolves each
conflict once and never rewrites history that has already been pushed or reviewed.

── What this refuses to do with local changes ─────────────────────────────
Nothing is stashed and nothing is committed on the user's behalf. `refs/stash`
is SHARED by every worktree of a repository, so a stash here can be popped by an
agent in the main checkout. `git merge` already runs safely over a dirty tree:
it proceeds when the dirty paths are untouched, and when they would be
overwritten it refuses BEFORE changing anything. This module reports those
paths (`blocked`) and leaves the decision to the caller.

── Local `main`, not `origin/main` ─────────────────────────────────────────
Work is committed to the local main checkout and pushed later, so the local ref
is the newest truth. `repo.resolve_base_branch` is the same default `wt create` uses.

── After a merge: bootstrap again ─────────────────────────────────────────
A repository's `.tldrack/bootstrap.sh` must be idempotent, because a merge can
change what it sets up: a dependency the base grew, generated files built from
source that are stale until something rebuilds them. The contract: **when
`wt sync` exits 0, the worktree is merged AND bootstrapped** (a repository with
no hook has nothing to bootstrap).
"""

from __future__ import annotations

from pathlib import Path

from worktree import create as create_mod

STATUS_UP_TO_DATE = "up-to-date"
STATUS_MERGED = "merged"
STATUS_CONFLICT = "conflict"  # merge stopped with unmerged paths; MERGE_HEAD is set
STATUS_BLOCKED = "blocked"  # git refused before touching anything: local changes in the way

#: git's two "refused before touching anything" headers. A tab-indented path
#: list follows each one.
_BLOCKING_HEADERS = (
    "your local changes to the following files would be overwritten",
    "the following untracked working tree files would be overwritten",
)


# ── pure ─────────────────────────────────────────────────────────────────────


def build_merge_command(path: Path, base: str) -> list[str]:
    """`git merge --no-edit <base>` in the worktree. It fast-forwards when the
    branch has no commits of its own, and otherwise makes git's standard merge
    commit. No `--no-ff`: a merge commit that only records "nothing happened
    here" is noise in the harbor."""
    return ["git", "-C", str(path), "merge", "--no-edit", base]


def parse_blocking_paths(stderr: object) -> list[str]:
    """The paths git names when it refuses a merge over local changes.

    git prints a header and then one TAB-indented path per line. Only lines
    under one of `_BLOCKING_HEADERS` count, so an unrelated tab-indented hint
    cannot show up as a blocking file.
    """
    if not isinstance(stderr, str):
        return []
    paths: list[str] = []
    collecting = False
    for line in stderr.splitlines():
        lowered = line.lower()
        if any(header in lowered for header in _BLOCKING_HEADERS):
            collecting = True
            continue
        if collecting and line.startswith("\t"):
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
            continue
        collecting = False
    return paths


def classify_merge(code: int, stdout: object, stderr: object, *, merge_in_progress: bool) -> str | None:
    """Map a finished `git merge` to a status, or None when it failed another way.

    The order is significant. `MERGE_HEAD` is the only reliable conflict signal,
    because git's exit status is 1 for BOTH a conflict and some refusals. The
    parsed path list is what distinguishes "blocked" from an unknown failure.
    """
    if merge_in_progress:
        return STATUS_CONFLICT
    if code == 0:
        text = stdout if isinstance(stdout, str) else ""
        return STATUS_UP_TO_DATE if "already up to date" in text.lower() else STATUS_MERGED
    if parse_blocking_paths(stderr):
        return STATUS_BLOCKED
    return None


def parse_name_list(text: object) -> list[str]:
    """`git diff --name-only` output → paths, blanks dropped."""
    if not isinstance(text, str):
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


# ── machine-acting (every call through the fenced `create_mod.run_bounded`) ──


def merge_in_progress(path: Path) -> bool:
    code, _out, _err = create_mod.run_bounded(
        ["git", "-C", str(path), "rev-parse", "-q", "--verify", "MERGE_HEAD"], create_mod.PROBE_TIMEOUT_SECONDS
    )
    return code == 0


def conflicted_paths(path: Path) -> list[str]:
    _code, out, _err = create_mod.run_bounded(
        ["git", "-C", str(path), "diff", "--name-only", "--diff-filter=U"], create_mod.PROBE_TIMEOUT_SECONDS
    )
    return parse_name_list(out)


def behind_count(path: Path, base: str) -> int | None:
    """Commits on `base` that the worktree's HEAD lacks, or None if git cannot say."""
    code, out, _err = create_mod.run_bounded(
        ["git", "-C", str(path), "rev-list", "--count", f"HEAD..{base}"], create_mod.PROBE_TIMEOUT_SECONDS
    )
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def sync_worktree(path: Path, *, repo_root: Path, base: str, bootstrap: Path | None) -> dict:
    """Merge `base` into the worktree at `path`, then bootstrap on success.

    Returns the body the CLI prints: `status` is one of the four STATUS_* values,
    or absent with `error` set. `merged` is the number of base commits that came
    in (for display; None when git could not count them).
    """
    body: dict = {"path": str(path), "base": base, "merged": None, "conflicts": [], "blocking": [], "bootstrap_error": None}

    # A merge someone already started: report it, do not stack a second one.
    if merge_in_progress(path):
        body.update(status=STATUS_CONFLICT, conflicts=conflicted_paths(path))
        return body

    behind = behind_count(path, base)
    code, out, err = create_mod.run_bounded(build_merge_command(path, base), create_mod.GIT_TIMEOUT_SECONDS)
    in_progress = merge_in_progress(path) if code != 0 else False
    status = classify_merge(code, out, err, merge_in_progress=in_progress)

    if status is None:
        body["error"] = (err or out or "git merge failed").strip()[:300]
        return body
    body["status"] = status
    if status == STATUS_CONFLICT:
        body["conflicts"] = conflicted_paths(path)
        return body
    if status == STATUS_BLOCKED:
        body["blocking"] = parse_blocking_paths(err)
        return body

    body["merged"] = 0 if status == STATUS_UP_TO_DATE else behind
    body["bootstrap_error"] = create_mod.run_bootstrap(bootstrap, path, repo_root)
    return body
