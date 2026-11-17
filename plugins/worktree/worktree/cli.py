"""`wt` — the ONE command every worktree door calls.

    python -m worktree.cli create <branch>      # `git worktree add` + bootstrap → prints the path
    python -m worktree.cli remove <branch> [--force]
    python -m worktree.cli sync <branch>        # merge main in + bootstrap again → one JSON line (sync.py)
    python -m worktree.cli path <branch>        # where it lives     (names.worktree_path_for)
    python -m worktree.cli session <branch>     # its tmux session   (names.session_name_for)
    python -m worktree.cli trust [<sha256>]     # show the repository's bootstrap hook, or pin it (hook_trust.py)
    python -m worktree.cli hook-create          # Claude Code's WorktreeCreate hook (stdin JSON)
    python -m worktree.cli hook-remove          # Claude Code's WorktreeRemove hook (stdin JSON)

Four doors open a worktree: the rack's `+ WORKTREE`, a shell function, Claude
Code's own `claude -w` / `EnterWorktree` / `isolation: "worktree"`, and the
`/worktree` skill. One implementation serves them all: `names.py` decides the
names, `repo.py` reads the repository's own settings and hooks, `create.py` does
the git work, and this file is the only thing between a caller and those. The
repository is always the one the caller stands in (`--cwd`, or the hook
payload's `cwd`), so the same command works in every repository.

── stdout is the answer, stderr is everything else ─────────────────────────
Two callers PARSE stdout: the shell (`cd "$(wt create x)"`) and Claude Code
(the hook envelope). So stdout carries exactly one line per invocation and every
note, warning and bootstrap complaint goes to stderr. `sync` has no single
answer, so its one line is a JSON object for the agent that ran it. `create.run_bootstrap`
already swallows bootstrap.sh's own stdout for the same reason.

── The hook contract (Claude Code docs, hooks reference, 2026-09-05) ────────
`WorktreeCreate` stdin: `{session_id, hook_event_name, cwd, name, base_path,
worktree_path}`; stdout: the created directory, BARE, as the last line (the
reference's JSON envelope is not what is consumed — see `hook_create_output`);
ANY non-zero exit aborts the creation. `WorktreeRemove` stdin: `{session_id,
hook_event_name, cwd, worktree_path}`; exit 0 lets removal proceed. Neither
takes a matcher. `${CLAUDE_PROJECT_DIR}` stays at the main checkout, but the
main checkout is derived from the payload's `cwd` here (`git worktree list`
prints it first from ANY worktree), so a session already inside a worktree that
opens another still resolves the right repository.

── Why `create` REOPENS where `POST /create` REFUSES ───────────────────────
The rack's button refuses an existing branch (409) because a second click must
select the session that is already there, and the session is the rack's unit.
`claude -w <name>` on an existing name means "open that worktree again" — the
worktree is the CLI's unit — so a branch already checked out AT ITS OWN PATH is
the success case here, not a conflict. `decide_open` is that table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from worktree import create as create_mod
from worktree import hook_trust
from worktree import sync as sync_mod
from worktree.branches import build_branch_delete_command
from worktree.listing import list_worktrees, main_worktree_path, worktree_by_branch
from worktree.names import (
    display_path,
    is_managed_path,
    session_name_for,
    validate_branch,
    worktree_path_for,
)
from worktree.repo import read_repo_hooks, resolve_base_branch, worktree_root_for

#: Exit statuses. 1 is "refused, here is why" (a message on stderr); 2 is
#: argparse's own usage error; 3 is "the machine is missing something".
EXIT_REFUSED = 1
EXIT_MISSING_TOOL = 3
#: `sync` stopped on conflicts. The merge is left IN PROGRESS for the resolver.
EXIT_CONFLICT = 4

ACTION_NEW = "new"  # `git worktree add -b <branch> <path> <base>`
ACTION_EXISTING = "existing"  # `git worktree add <path> <branch>` — the branch is there, its worktree is not
ACTION_REOPEN = "reopen"  # nothing to do: the branch is already checked out at its own path


# ── pure ─────────────────────────────────────────────────────────────────────


def decide_open(
    branch_error: str | None, *, branch_exists: bool, held_at: str | None, path: Path, path_exists: bool
) -> tuple[str | None, str | None]:
    """`(action, None)` or `(None, reason)` — the CLI's refusal-or-reopen table.

    Ordered like `create.decide_create`: cheapest and most actionable first.
    `held_at` is the worktree currently holding the branch (None when none).
    Compared as resolved paths so a symlinked root and git's spelling agree.
    """
    if branch_error is not None:
        return None, branch_error
    if branch_exists:
        if held_at is not None:
            if _same_path(held_at, path):
                return ACTION_REOPEN, None
            return None, (f"already checked out at {display_path(held_at)} — attach to that instead")
        if path_exists:
            return None, "a worktree directory with that name is already on disk"
        return ACTION_EXISTING, None
    if path_exists:
        return None, "a worktree directory with that name is already on disk"
    return ACTION_NEW, None


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return False


def parse_hook_payload(text: object) -> dict:
    """The hook's stdin JSON as a dict — `{}` for anything that is not one.

    Tolerant on purpose: a hook that crashes on an unexpected shape aborts the
    worktree creation with a traceback nobody reads, and every field is
    re-checked by the caller anyway.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def hook_create_output(path: str | os.PathLike[str]) -> str:
    """The ONLY thing `hook-create` prints on success: the worktree path, bare.

    MEASURED against Claude Code 2.1.261, because the two doc pages disagree
    and the hooks reference is the wrong one. It shows a JSON envelope
    (`hookSpecificOutput.worktree_path`); the validator first demanded
    `hookEventName`, then `worktreePath` (camelCase), and once the envelope
    finally VALIDATED, Claude Code still took the last line of stdout LITERALLY
    as the directory and failed with `worktree directory …/{"hookSpecificOutput"…}
    does not exist`. Its own error names the contract: "the hook must print the
    directory it created as the last line of its stdout" — which is exactly the
    worktrees page's `echo "$DIR"` example. A wrong envelope aborts creation
    AFTER the worktree exists, so the next attempt reopens it: loud, recoverable.
    """
    return str(path)


def branch_held_at(repo_root: Path, branch: str) -> str | None:
    """Path of the worktree holding `branch`, or None. One bounded `git worktree list`."""
    record = worktree_by_branch(list_worktrees(repo_root)).get(branch)
    path = record.get("path") if record else None
    return path if isinstance(path, str) and path else None


def branch_at_path(repo_root: Path, path: str | os.PathLike[str]) -> str | None:
    """The branch checked out at `path`, or None (detached, unknown, or not a worktree)."""
    for record in list_worktrees(repo_root):
        if _same_path(record.get("path") or "", path):
            branch = record.get("branch")
            return branch if isinstance(branch, str) and branch else None
    return None


def resolve_repo_root(cwd: str | os.PathLike[str] | None) -> Path | None:
    """The MAIN checkout for whatever `cwd` is inside of — `git worktree list`
    prints it first from any worktree (`listing.main_worktree_path`)."""
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    main = main_worktree_path(list_worktrees(start))
    return Path(main) if main else None


# ── machine-acting ──────────────────────────────────────────────────────────


def _checked_branch(branch: str) -> tuple[str, tuple[int, dict] | None]:
    """`(stripped branch, None)`, or `(branch, refusal)`. The refusal comes when
    git is missing or the name is invalid. It is the shared first step of
    `create` and `sync`, and it runs before either one touches git."""
    if not create_mod.tool_found("git"):
        return branch, (EXIT_MISSING_TOOL, {"error": "git not found"})
    branch_error = validate_branch(branch)
    if branch_error is not None:
        return branch, (EXIT_REFUSED, {"error": branch_error})
    return branch.strip(), None


def create(branch: str, *, repo_root: Path, root: Path | None = None, base: str | None = None) -> tuple[int, dict]:
    """Open the worktree for `branch` — new, existing-branch, or already there.

    `(0, {path, branch, session, action, bootstrap_error})` or `(1, {error})`.
    Bootstrap failure is a NOTE, never a refusal (`create.run_bootstrap` says
    why): the caller gets its path either way.
    """
    hooks = read_repo_hooks(repo_root)
    root = root or worktree_root_for(repo_root)
    base = base or resolve_base_branch(hooks, repo_root)

    branch, refusal = _checked_branch(branch)
    if refusal is not None:
        return refusal
    path = worktree_path_for(branch, root)
    exists = create_mod.branch_exists(repo_root, branch)
    held_at = branch_held_at(repo_root, branch) if exists else None
    action, error = decide_open(None, branch_exists=exists, held_at=held_at, path=path, path_exists=path.exists())
    if error is not None:
        return EXIT_REFUSED, {"error": error}

    bootstrap_error = None
    if action != ACTION_REOPEN:
        failure = create_mod.add_worktree(
            repo_root, path, branch, base, existing=(action == ACTION_EXISTING), sparse_exclude=hooks.sparse_exclude
        )
        if failure is not None:
            return EXIT_REFUSED, {"error": failure[1].get("error", "git worktree add failed")}
        bootstrap_error = create_mod.run_bootstrap(hooks.bootstrap, path, repo_root)

    return 0, {
        "path": str(path),
        "branch": branch,
        "session": session_name_for(branch),
        "action": action,
        "bootstrap_error": bootstrap_error,
    }


def remove(branch: str, *, repo_root: Path, root: Path | None = None, force: bool = False) -> tuple[int, dict]:
    """Kill the session, remove the worktree, then `git branch -d` (never `-D`).

    The branch goes only when it is fully merged into the current branch —
    `-d`'s own rule — so a hook-created subagent worktree does not leave an
    atoll behind in the harbor, while an unmerged branch survives with a note.
    That is one step past `POST /remove`, which keeps every branch because a
    ref deleted from the rack is not recoverable there; here the caller (a
    shell, or Claude after the user chose "remove") already decided.
    """
    root = root or worktree_root_for(repo_root)
    branch_error = validate_branch(branch)
    if branch_error is not None:
        return EXIT_REFUSED, {"error": branch_error}
    branch = branch.strip()
    held_at = branch_held_at(repo_root, branch)
    if held_at is None:
        return EXIT_REFUSED, {"error": "no worktree holds that branch"}
    if not is_managed_path(held_at, root):
        return EXIT_REFUSED, {"error": f"{display_path(held_at)} is not under {display_path(root)} — not ours to remove"}
    path = Path(held_at)

    error = create_mod.remove_worktree(repo_root, path, [session_name_for(branch)], force=force)
    if error is not None:
        return EXIT_REFUSED, {"error": error, "needs_force": not force}

    code, _out, err = create_mod.run_bounded(
        build_branch_delete_command(repo_root, branch, force=False), create_mod.PROBE_TIMEOUT_SECONDS
    )
    branch_deleted = code == 0
    lines = (err or "").strip().splitlines()
    note = None if branch_deleted else (lines[-1] if lines else "branch kept")[:200]
    return 0, {"branch": branch, "path": str(path), "removed": True, "branch_deleted": branch_deleted, "branch_note": note}


def sync(branch: str, *, repo_root: Path, base: str | None = None) -> tuple[int, dict]:
    """Merge `base` into the worktree holding `branch` (`sync.sync_worktree`).

    Any worktree that holds the branch qualifies, managed or not. A merge only
    adds commits, so there is no reason to require the `is_managed_path` fence
    that `remove` needs. The main checkout is refused: there, "sync with main"
    would mean merging main into itself.
    """
    hooks = read_repo_hooks(repo_root)
    base = base or resolve_base_branch(hooks, repo_root)
    bootstrap = hooks.bootstrap
    branch, refusal = _checked_branch(branch)
    if refusal is not None:
        return refusal
    if branch == base:
        return EXIT_REFUSED, {"error": f"'{branch}' is the base branch — nothing to sync it with"}
    held_at = branch_held_at(repo_root, branch)
    if held_at is None:
        return EXIT_REFUSED, {"error": f"no worktree holds '{branch}' — open one with: wt create {branch}"}
    if _same_path(held_at, repo_root):
        return EXIT_REFUSED, {"error": f"'{branch}' is checked out in the main checkout, not a worktree"}

    body = sync_mod.sync_worktree(Path(held_at), repo_root=repo_root, base=base, bootstrap=bootstrap)
    body["branch"] = branch
    status = body.get("status")
    if status is None or status == sync_mod.STATUS_BLOCKED:
        return EXIT_REFUSED, body
    if status == sync_mod.STATUS_CONFLICT:
        return EXIT_CONFLICT, body
    return 0, body


# ── the command ─────────────────────────────────────────────────────────────


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _require_repo(cwd: str | None) -> Path:
    repo_root = resolve_repo_root(cwd)
    if repo_root is None:
        _say("wt: not inside a git repository")
        raise SystemExit(EXIT_REFUSED)
    return repo_root


def cmd_create(args: argparse.Namespace) -> int:
    repo_root = _require_repo(args.cwd)
    code, body = create(args.branch, repo_root=repo_root, base=args.base)
    if code != 0:
        _say(f"wt: {body['error']}")
        return code
    verb = {ACTION_NEW: "created", ACTION_EXISTING: "checked out", ACTION_REOPEN: "already open"}[body["action"]]
    _say(f"wt: {verb} '{body['branch']}' at {display_path(body['path'])} (tmux: {body['session']})")
    if body["bootstrap_error"]:
        _say(f"wt: bootstrap: {body['bootstrap_error']}")
    print(body["path"])
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    repo_root = _require_repo(args.cwd)
    code, body = remove(args.branch, repo_root=repo_root, force=args.force)
    if code != 0:
        hint = " (re-run with --force to discard uncommitted work)" if body.get("needs_force") else ""
        _say(f"wt: {body['error']}{hint}")
        return code
    kept = "" if body["branch_deleted"] else f"; branch kept — {body['branch_note']}"
    _say(f"wt: removed {display_path(body['path'])}{kept}")
    print(body["path"])
    return 0


def sync_summary(body: dict) -> str:
    """The human line for stderr: one line per outcome, naming the next step."""
    branch, base = body.get("branch"), body.get("base")
    where = display_path(body.get("path"))
    status = body.get("status")
    if status == sync_mod.STATUS_UP_TO_DATE:
        return f"wt: '{branch}' is up to date with {base} ({where})"
    if status == sync_mod.STATUS_MERGED:
        count = body.get("merged")
        what = f"{count} commit{'' if count == 1 else 's'}" if isinstance(count, int) else "new commits"
        return f"wt: merged {what} from {base} into '{branch}' ({where})"
    if status == sync_mod.STATUS_CONFLICT:
        files = body.get("conflicts") or []
        return (
            f"wt: CONFLICT in {len(files)} file{'' if len(files) == 1 else 's'} at {where} — "
            f"resolve, `git commit --no-edit`, then `wt sync {branch}` again "
            f"(or `git merge --abort`)"
        )
    if status == sync_mod.STATUS_BLOCKED:
        files = ", ".join(body.get("blocking") or [])
        return (
            f"wt: nothing merged — local changes in '{branch}' would be overwritten: {files}. "
            f"Commit them in {where}, then sync again"
        )
    return f"wt: {body.get('error', 'sync failed')}"


def cmd_sync(args: argparse.Namespace) -> int:
    repo_root = _require_repo(args.cwd)
    code, body = sync(args.branch, repo_root=repo_root, base=args.base)
    _say(sync_summary(body) if "path" in body else f"wt: {body['error']}")
    if body.get("bootstrap_error"):
        _say(f"wt: bootstrap: {body['bootstrap_error']}")
    print(json.dumps(body, sort_keys=True))
    return code


def cmd_path(args: argparse.Namespace) -> int:
    error = validate_branch(args.branch)
    if error is not None:
        _say(f"wt: {error}")
        return EXIT_REFUSED
    print(worktree_path_for(args.branch.strip(), worktree_root_for(_require_repo(args.cwd))))
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    error = validate_branch(args.branch)
    if error is not None:
        _say(f"wt: {error}")
        return EXIT_REFUSED
    print(session_name_for(args.branch.strip()))
    return 0


def cmd_hook_create(args: argparse.Namespace) -> int:
    payload = parse_hook_payload(sys.stdin.read())
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        _say("wt: WorktreeCreate payload carries no name")
        return EXIT_REFUSED
    repo_root = _require_repo(payload.get("cwd") if isinstance(payload.get("cwd"), str) else None)
    code, body = create(name, repo_root=repo_root)
    if code != 0:
        # A `#1234` PR ref lands here: `validate_branch` refuses `#`. Say so in
        # the words Claude will show the user.
        _say(f"wt: {body['error']}")
        return code
    if body["bootstrap_error"]:
        _say(f"wt: bootstrap: {body['bootstrap_error']}")
    print(hook_create_output(body["path"]))
    return 0


def cmd_hook_remove(args: argparse.Namespace) -> int:
    payload = parse_hook_payload(sys.stdin.read())
    target = payload.get("worktree_path")
    if not isinstance(target, str) or not target.strip():
        _say("wt: WorktreeRemove payload carries no worktree_path")
        return 0  # nothing to do is not a reason to block Claude's own exit
    # The repository the session stands in, else the one the worktree belongs to.
    # Neither found is not ours to judge, and must not block Claude's own exit.
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    repo_root = resolve_repo_root(cwd) if cwd else None
    repo_root = repo_root or (resolve_repo_root(target) if Path(target).is_dir() else None)
    if repo_root is None:
        _say(f"wt: {display_path(target)} is in no repository found here; left alone")
        return 0
    root = worktree_root_for(repo_root)
    if not is_managed_path(target, root):
        _say(f"wt: {display_path(target)} is not under {display_path(root)}; left alone")
        return 0
    branch = branch_at_path(repo_root, target)
    if branch is None:
        _say(f"wt: no branch checked out at {display_path(target)}; left alone")
        return 0
    # Claude asks this only after the user chose "remove" over "keep", so the
    # uncommitted work in it is already forfeited — `--force` honours that.
    code, body = remove(branch, repo_root=repo_root, root=root, force=True)
    if code != 0:
        _say(f"wt: {body['error']}")
        return code
    kept = "" if body["branch_deleted"] else f"; branch kept — {body['branch_note']}"
    _say(f"wt: removed {display_path(body['path'])}{kept}")
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    """`wt trust`: show the hook and its sha256. `wt trust <sha256>`: pin it.
    stdout is the digest either way; the hook itself goes to stderr to be read."""
    repo_root = _require_repo(args.cwd)
    script = read_repo_hooks(repo_root).bootstrap
    if args.sha256 is None:
        data = hook_trust.read_hook(script) if script is not None else None
        if data is None:
            _say("wt: this repository has no .tldrack/bootstrap.sh")
            return EXIT_REFUSED
        digest = hook_trust.digest_of(data)
        text, escaped = hook_trust.printable(data)
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
        if escaped:
            _say(
                f"wt: WARNING — {escaped} control or bidi character(s) shown escaped above; "
                "a script that needs them to hide from a reader is not one to trust"
            )
        trusted = hook_trust.pinned(repo_root) == digest
        _say(f"wt: {'trusted' if trusted else 'NOT trusted'}. To run it on every new worktree: wt trust {digest}")
        print(digest)
        return 0
    error = hook_trust.pin(repo_root, script, args.sha256)
    if error is not None:
        _say(f"wt: {error}")
        return EXIT_REFUSED
    _say(f"wt: trusted {display_path(script)}")
    print(args.sha256)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wt", description=__doc__.split("\n\n")[0])
    parser.add_argument("--cwd", help="resolve the repository from here (default: the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="git worktree add + bootstrap; prints the path")
    p.add_argument("branch")
    p.add_argument(
        "--base", help="branch to start from (default: $WORKTREE_BASE_BRANCH, the repository's .tldrack/worktree.json, or main)"
    )
    p.set_defaults(run=cmd_create)

    p = sub.add_parser("remove", help="remove the worktree; `git branch -d` if merged")
    p.add_argument("branch")
    p.add_argument("--force", action="store_true", help="discard uncommitted work")
    p.set_defaults(run=cmd_remove)

    p = sub.add_parser("sync", help="merge the base branch into this worktree, then bootstrap again")
    p.add_argument("branch")
    p.add_argument("--base", help="branch to merge in (default: as for create)")
    p.set_defaults(run=cmd_sync)

    p = sub.add_parser("path", help="print the worktree path for a branch")
    p.add_argument("branch")
    p.set_defaults(run=cmd_path)

    p = sub.add_parser("session", help="print the tmux session name for a branch")
    p.add_argument("branch")
    p.set_defaults(run=cmd_session)

    p = sub.add_parser("trust", help="show the repository's bootstrap hook, or pin it by its sha256")
    p.add_argument("sha256", nargs="?")
    p.set_defaults(run=cmd_trust)

    sub.add_parser("hook-create", help="Claude Code WorktreeCreate hook (JSON on stdin)").set_defaults(run=cmd_hook_create)
    sub.add_parser("hook-remove", help="Claude Code WorktreeRemove hook (JSON on stdin)").set_defaults(run=cmd_hook_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
