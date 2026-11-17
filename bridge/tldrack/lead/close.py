"""The verified close: what must be true before the bridge ends a worker session.

The checks are a table, run in order. The FIRST failing check refuses, with its code, so the lead learns exactly what is
missing. Nothing an agent wrote is an input: not its "done", not Laya's ✅ (that only means "claims
done"), not a PR body. Every fact comes from the bridge's own state, tmux or git.

Closing ends only the tmux session. The worktree and the branch stay for the PR, and the harbor's
CLEAN removes them once it lands; the conversation is already archived by the session ledger, so a
closed worker loses nothing.

A worker whose session is already GONE can still be closed: there is nothing to kill, so the session
checks (state, questions in its terminal, descendants) have nothing to say, while the name check,
the tree and the claim still decide. That is how a worker that crashed after its PR opened is put away.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT = 20
#: The state words /snapshot serves; only `free` lets a session be closed.
CLOSABLE_STATE = "free"


@dataclass
class CloseFacts:
    run_open: bool
    step_known: bool
    kind: str | None
    crew_session: str | None
    recorded_lineage: str | None
    run_identity: str
    #: The live session by the worker's name: its lineage id and parent id; None when no session has the name.
    live_lineage: str | None = None
    live_parent: str | None = None
    live: bool = False
    state: str | None = None
    open_questions: list[str] = field(default_factory=list)
    children_running: int = 0
    #: True clean, False dirty, None git could not tell (refuses).
    tree_clean: bool | None = None
    claim_head: str | None = None
    #: Is the claim on the pushed branch tip (or the merged PR's head)? None: could not tell.
    claim_published: bool | None = None
    #: Is the tree's HEAD on that tip too (nothing left unpushed)? None: could not tell.
    head_published: bool | None = None
    report_ok: bool = False


def decide_close(f: CloseFacts) -> tuple[str | None, str]:
    """(None, "closed") to close, else (code, why). Pure: the truth table, in the plan's order."""
    if not f.run_open or not f.step_known:
        return "unknown_step", "that step is not in an open run"
    if not f.crew_session:
        return "unknown_step", "that step has no worker to close"
    if f.live and (f.recorded_lineage is None or f.live_lineage != f.recorded_lineage):
        return "not_the_crew", f"the live '{f.crew_session}' is another agent wearing the name"
    if f.live and f.live_parent != f.run_identity:
        return "not_crew_of_run", f"'{f.crew_session}' was not spawned by this run's lead"
    if f.live and f.state != CLOSABLE_STATE:
        return (f.state or "unknown"), f"'{f.crew_session}' is {f.state or 'unknown'}, not free"
    if f.open_questions:
        return "open_question", f"open questions: {', '.join(f.open_questions)}"
    if f.live and f.children_running:
        return "children_running", f"{f.children_running} of its own sessions still run"
    if f.tree_clean is None:
        return "git_error", "git could not read the worktree"
    if not f.tree_clean:
        return "dirty", "the worktree has uncommitted changes"
    if f.kind == "scout":
        if not f.report_ok:
            return "no_report", "no saved report (tldrack lead report <file>)"
        return None, "closed"
    if not f.claim_head:
        return "no_claim", "the worker has not reported ready with a tested commit"
    if f.claim_published is None or f.head_published is None:
        return "git_error", "could not read the pushed branch"
    if not f.claim_published:
        return "claim_unpublished", "the claimed commit is not on the pushed branch or PR head"
    if not f.head_published:
        return "unpublished_commits", "the worktree has commits after the claim that are not pushed"
    return None, "closed"


# ── the git half, injectable ────────────────────────────────────────────────

Runner = Callable[[list[str]], tuple[int, str]]


def run_git(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout.strip()


def tree_clean(path: Path | None, git: Runner = run_git) -> bool | None:
    """`git status --porcelain`, untracked files included. A failure is None, never clean: a pr.py
    helper reads a git failure as clean, which is right for a button and wrong for a kill."""
    if path is None or not path.is_dir():
        return None
    code, out = git(["git", "-C", str(path), "status", "--porcelain"])
    return None if code else out == ""


def _ancestor(path: Path, older: str, newer: str, git: Runner) -> bool | None:
    code, _ = git(["git", "-C", str(path), "merge-base", "--is-ancestor", older, newer])
    return True if code == 0 else False if code == 1 else None


def publication(
    path: Path | None, branch: str | None, claim: str | None, worker: Mapping, git: Runner = run_git
) -> tuple[bool | None, bool | None]:
    """(claim on the pushed tip, HEAD on the pushed tip). The tip is the remote branch as
    `ls-remote` sees it now; after a merge deleted the branch, the merged PR's head stands in."""
    if path is None or not branch or not claim or not path.is_dir():
        return None, None
    code, out = git(["git", "-C", str(path), "ls-remote", "origin", f"refs/heads/{branch}"])
    if code:
        return None, None
    tip = out.split()[0] if out else None
    if tip is None and worker.get("pr_state") == "merged":
        tip = worker.get("pr_head")
    if not tip:
        return False, False
    if git(["git", "-C", str(path), "cat-file", "-e", f"{tip}^{{commit}}"])[0]:
        git(["git", "-C", str(path), "fetch", "--no-tags", "origin", f"refs/heads/{branch}"])
    code, head = git(["git", "-C", str(path), "rev-parse", "HEAD"])
    if code:
        return None, None
    return _ancestor(path, claim, tip, git), _ancestor(path, head, tip, git)
