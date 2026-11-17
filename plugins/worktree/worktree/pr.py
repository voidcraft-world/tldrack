"""Open a pull request for a worktree branch: commit what is dirty, push, then
`gh pr create`.

── Why auto-committing is safe HERE and nowhere else ──────────────────────
`git add -A` on a `main` shared by a rack of agents would sweep up hundreds of
uncommitted files belonging to the others. A worktree branch is
the opposite case by construction: it was cut from `main` moments ago and only
one agent has ever written to it, so its entire diff IS the feature. The whole
point of the worktree is to make this operation safe, and refusing to do it
would leave the isolation without an exit ramp.

── Why it runs on a background thread ─────────────────────────────────────
`git push` and `gh pr create` are network calls against GitHub — seconds at
best, and unbounded when the network is bad. Plugin handlers execute on the
bridge's REQUEST threads; blocking one for that long stalls the status listener
the whole SPA polls. So the route answers `202 {status: "opening"}` and the
outcome lands in the store, which `GET /state` already reports. That is the same
202 + client-rescan shape `coordinator/plugin.py::_handle_audit` uses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Local git bookkeeping — fast, but `git add -A` over a big tree is not free.
GIT_TIMEOUT_SECONDS = 60
#: Anything that talks to GitHub. Generous: a slow network answering eventually
#: is a better outcome than a timeout the user has to interpret.
NETWORK_TIMEOUT_SECONDS = 120
#: Read-only local probes.
PROBE_TIMEOUT_SECONDS = 10

DEFAULT_REMOTE = "origin"

#: The two `gh` refusals, worded once: `decide_pr` answers them as a 503, and
#: the harbor's fetch worker records them on the PR index instead of failing
#: the git half of a fetch over them.
GH_MISSING_ERROR = "the `gh` CLI was not found — install with: brew install gh"
GH_UNAUTHED_ERROR = "`gh` is not authenticated — run: gh auth login"


def build_commit_commands(message: str) -> list[list[str]]:
    """Stage everything, then commit. Pure.

    `-A` rather than `-u`: a new file is the most common thing an agent adds, and
    a PR missing its new files is worse than no PR. Everything the branch should
    not carry belongs in `.gitignore`, which is version-controlled and shared.
    """
    return [
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
    ]


def build_push_command(branch: str, remote: str = DEFAULT_REMOTE) -> list[str]:
    """`-u` so the branch gets an upstream on its first push; `gh pr create`
    needs one and will otherwise prompt — which, on a detached background
    thread, is a hang rather than a question."""
    return ["git", "push", "-u", remote, branch]


def build_pr_view_command() -> list[str]:
    """The idempotency probe: does this branch already have a PR?"""
    return ["gh", "pr", "view", "--json", "url", "-q", ".url"]


def build_pr_create_command(title: str | None) -> list[str]:
    """`--fill` derives title and body from the commits when the caller named
    neither. An explicit title still gets `--body` from the commits, because a
    PR with a title and no body is a worse artifact than either alone."""
    if title:
        return ["gh", "pr", "create", "--title", title, "--fill"]
    return ["gh", "pr", "create", "--fill"]


def commit_message_for(branch: str, title: str | None) -> str:
    """What the sweep-up commit is called.

    The branch name is a poor commit subject, but it is an HONEST one — it says
    exactly as much as the machine actually knows. Inventing a summary of a diff
    nobody read would be worse.
    """
    return title.strip() if isinstance(title, str) and title.strip() else branch


def decide_pr(
    *, known: bool, gh_found: bool, gh_authed: bool, has_commits: bool, dirty: bool, in_flight: bool
) -> tuple[int, dict] | None:
    """The refusal table for POST /pr (pure). None = clear to open.

    Ordered so the message a user sees is the one they can act on. `in_flight`
    is a success rather than a 409, for the same reason `decide_resume` treats a
    live session as one: the job they asked for is already happening.
    """
    if not known:
        return 404, {"error": "no managed worktree for that branch"}
    if not has_commits and not dirty:
        return 400, {"error": "nothing to open a PR for — this branch has no commits and no uncommitted changes"}
    if not gh_found:
        return 503, {"error": GH_MISSING_ERROR}
    if not gh_authed:
        return 503, {"error": GH_UNAUTHED_ERROR}
    if in_flight:
        return 202, {"status": "opening"}
    return None


def _run(command: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Bounded `subprocess.run` in `cwd` → `(returncode, stdout, stderr)`.

    `cwd=` rather than `os.chdir`: this runs on a background thread inside a
    process serving every other route, and a chdir there is global. Likewise
    `env` is laid OVER a copy of the process environment, never set on it —
    `ship.py` points one command's `GIT_INDEX_FILE` at a temp index, and that
    must not leak into a concurrent request's git.
    """
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **env} if env else None,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"`{' '.join(command[:2])}` timed out after {timeout}s"
    except OSError as exc:
        return -1, "", f"could not run {command[0]} ({type(exc).__name__}: {exc})"[:200]
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def is_dirty(path: Path) -> bool:
    code, out, _err = _run(["git", "status", "--porcelain"], path, PROBE_TIMEOUT_SECONDS)
    return code == 0 and bool(out)


def has_commits(path: Path, base: str) -> bool:
    """Any commit on this branch that `base` does not have.

    `<base>..HEAD` counts one way on purpose: a branch that is merely BEHIND
    main still has its own work and must still be able to open a PR.
    """
    code, out, _err = _run(["git", "rev-list", "--count", f"{base}..HEAD"], path, PROBE_TIMEOUT_SECONDS)
    if code != 0:
        return False
    try:
        return int(out) > 0
    except ValueError:
        return False


def ahead_behind(path: Path, base: str) -> tuple[int, int]:
    """`(ahead, behind)` versus base — display only, so a failure is (0, 0)
    rather than an error the caller has to thread through `/state`."""
    code, out, _err = _run(["git", "rev-list", "--left-right", "--count", f"{base}...HEAD"], path, PROBE_TIMEOUT_SECONDS)
    if code != 0:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[1]), int(parts[0])
    except ValueError:
        return 0, 0


def gh_authed() -> bool:
    """`gh auth status` exits non-zero when no host is logged in.

    Probed at CLICK time rather than cached: a token expires, and a cached yes
    would turn a fixable "run gh auth login" into an opaque push failure.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def existing_pr_url(path: Path) -> str | None:
    code, out, _err = _run(build_pr_view_command(), path, NETWORK_TIMEOUT_SECONDS)
    return out if code == 0 and out.startswith("http") else None


def open_pull_request(path: Path, branch: str, base: str, title: str | None) -> tuple[str, str | None, str | None]:
    """Commit → push → create. Returns `(status, url, error)` for the store.

    Runs on a background thread. Never raises: every failure becomes an `error`
    status the panel can render, because an exception here would only reach a
    thread nobody is joining.
    """
    if is_dirty(path):
        message = commit_message_for(branch, title)
        for command in build_commit_commands(message):
            code, _out, err = _run(command, path, GIT_TIMEOUT_SECONDS)
            if code != 0:
                return "error", None, (err or "commit failed")[:300]

    if not has_commits(path, base):
        # Reachable when the tree was dirty with only ignored files: `git add -A`
        # staged nothing and the commit failed, or committed nothing. Pushing an
        # empty branch would open a PR with no diff.
        return "error", None, "nothing to push — no commits on this branch"

    code, _out, err = _run(build_push_command(branch), path, NETWORK_TIMEOUT_SECONDS)
    if code != 0:
        return "error", None, (err or "push failed")[:300]

    # AFTER the push, not before: a PR opened by an earlier click is the common
    # case, and asking GitHub is the only answer that survives a bridge restart.
    existing = existing_pr_url(path)
    if existing:
        return "open", existing, None

    code, out, err = _run(build_pr_create_command(title), path, NETWORK_TIMEOUT_SECONDS)
    if code != 0:
        return "error", None, (err or "gh pr create failed")[:300]
    url = next((line for line in out.splitlines() if line.startswith("http")), None)
    if not url:
        # `gh` succeeded but printed something unexpected. The PR almost certainly
        # exists, so ask rather than reporting a failure the user cannot act on.
        url = existing_pr_url(path)
    return ("open", url, None) if url else ("error", None, "gh pr create printed no URL")
