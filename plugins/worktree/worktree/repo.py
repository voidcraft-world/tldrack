"""One repository's worktree facts: where its worktrees live, what a new branch
starts from, and the repository's own hooks. Every door reads them from here:
the plugin's routes, `wt`, and Claude Code's `WorktreeCreate` hook.

── Where worktrees live ────────────────────────────────────────────────────
`~/worktrees/<repo>/<branch slug>`, with `<repo>` the main checkout's directory
name. One root per repository is what lets one rack serve several: the
`is_managed_path` fence is per root, so a remove in one repository can never
reach a tree of another. `WORKTREE_ROOT` moves the parent (`~/worktrees`), never
one repository's directory, so every door that reads it agrees on all of them.

Deliberately NOT under the repository (`.claude/worktrees/`, say): a tool that
walks the checkout (a test runner, a file watcher, a search) would walk every
worktree inside it too. And not a directory another tool already writes worktrees
into: two systems sharing one root is how a `git worktree remove` eats somebody
else's session.

── The repository decides its own setup ───────────────────────────────────
A repository may carry a `.tldrack/` directory:

- `.tldrack/bootstrap.sh <worktree> <main checkout>`: best-effort setup after a
  worktree is created (link dependencies, write an env file). Absent, nothing
  runs; present, it runs only once the operator pinned it (`hook_trust.py`).
- `.tldrack/worktree.json`: `{"base": "<branch>", "sparse_exclude": ["<dir>", …]}`,
  both optional.

They live in the repository, not in the operator's `settings.json`, because `wt`
and the hook run without a bridge and must still agree with it. Only the main
checkout's copy counts: a branch cannot change how its own worktree is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from worktree import hook_trust
from worktree.names import display_path, slugify, validate_existing_branch
from worktree.store import resolve_store_path

#: Moves the PARENT of every repository's worktree directory.
WORKTREES_HOME_ENV = "WORKTREE_ROOT"
DEFAULT_WORKTREES_HOME = Path.home() / "worktrees"

#: Overrides every repository's base branch: one process, one answer (tests, a CI box).
BASE_BRANCH_ENV = "WORKTREE_BASE_BRANCH"
DEFAULT_BASE_BRANCH = "main"

HOOK_DIR = ".tldrack"
BOOTSTRAP_HOOK = "bootstrap.sh"
SETTINGS_FILE = "worktree.json"

#: The settings file is a few lines; anything bigger is not one.
SETTINGS_MAX_BYTES = 16 * 1024
#: Each pattern becomes one `sparse-checkout set` argument.
SPARSE_MAX = 32
SPARSE_ITEM_MAX = 200
_SPARSE_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")

#: A directory name used as-is: what is safe in a path and readable in `ls`.
_DIR_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True)
class RepoHooks:
    """What the repository's `.tldrack/` says. Every field has a safe empty value."""

    base: str | None = None
    sparse_exclude: tuple[str, ...] = ()
    bootstrap: Path | None = None
    #: Why a present settings file was ignored, for the `/repos` readout.
    errors: tuple[str, ...] = field(default=())


def worktrees_home() -> Path:
    return Path(os.environ.get(WORKTREES_HOME_ENV) or DEFAULT_WORKTREES_HOME).expanduser()


def repo_dir_name(main_checkout: str | os.PathLike[str]) -> str:
    """The main checkout's directory name, when it is a safe one; else its slug."""
    name = Path(main_checkout).name
    if _DIR_NAME_RE.fullmatch(name):
        return name
    return slugify(name) or "repo"


def worktree_root_for(main_checkout: str | os.PathLike[str]) -> Path:
    """`~/worktrees/<repo>`: the one directory this repository's worktrees live in."""
    return worktrees_home() / repo_dir_name(main_checkout)


def repo_key(main_checkout: str | os.PathLike[str]) -> str:
    """A stable key for one repository: its name plus a hash of where it is.

    The hash keeps two checkouts that share a name apart (a fork beside its
    upstream), and keeps one repository's key the same whatever else the
    operator configures. The key names state on disk and travels in requests;
    it never becomes a path the caller chooses.
    """
    resolved = str(Path(main_checkout).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(repo_dir_name(main_checkout)) or 'repo'}-{digest}"


def parse_repo_settings(text: object) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """`.tldrack/worktree.json` → `(base, sparse_exclude, errors)`. Total: a key
    that does not validate is dropped and named in `errors`, never raised."""
    errors: list[str] = []
    try:
        data = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None, (), (f"{SETTINGS_FILE} is not a JSON object",)
    base = data.get("base")
    if base is not None and (not isinstance(base, str) or validate_existing_branch(base) is not None):
        errors.append("`base` is not a branch name")
        base = None
    sparse: list[str] = []
    raw = data.get("sparse_exclude", [])
    if not isinstance(raw, list):
        errors.append("`sparse_exclude` is not a list")
        raw = []
    for item in raw[:SPARSE_MAX]:
        cleaned = item.strip().strip("/") if isinstance(item, str) else ""
        if not cleaned or len(cleaned) > SPARSE_ITEM_MAX or not _SPARSE_RE.fullmatch(cleaned) or ".." in cleaned.split("/"):
            errors.append(f"`sparse_exclude` entry {str(item)[:40]!r} is not a relative directory")
            continue
        sparse.append(cleaned)
    if len(raw) > SPARSE_MAX:
        errors.append(f"`sparse_exclude` keeps its first {SPARSE_MAX} entries")
    return (base.strip() if isinstance(base, str) else None), tuple(dict.fromkeys(sparse)), tuple(errors)


def read_repo_hooks(main_checkout: str | os.PathLike[str]) -> RepoHooks:
    """The repository's `.tldrack/` as `RepoHooks`. A missing directory is the
    common case and reads as every default."""
    hook_dir = Path(main_checkout) / HOOK_DIR
    script = hook_dir / BOOTSTRAP_HOOK
    bootstrap = script if script.is_file() else None
    settings = hook_dir / SETTINGS_FILE
    try:
        if not settings.is_file():
            return RepoHooks(bootstrap=bootstrap)
        if settings.stat().st_size > SETTINGS_MAX_BYTES:
            return RepoHooks(bootstrap=bootstrap, errors=(f"{SETTINGS_FILE} is larger than {SETTINGS_MAX_BYTES} bytes",))
        text = settings.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return RepoHooks(bootstrap=bootstrap, errors=(f"{SETTINGS_FILE}: {type(exc).__name__}",))
    base, sparse, errors = parse_repo_settings(text)
    return RepoHooks(base=base, sparse_exclude=sparse, bootstrap=bootstrap, errors=errors)


@dataclass(frozen=True)
class RepoSpec:
    """Everything the plugin needs to serve one repository."""

    key: str
    label: str
    root: Path  # the main checkout
    worktree_root: Path  # `~/worktrees/<repo>`
    base: str
    hooks: RepoHooks
    store_path: Path  # the PR store; every other state file sits beside it

    def view(self) -> dict:
        """The `/repos` row: `~`-collapsed, never a full home path. The hook's
        digest is what `POST /trust-hook` takes back once the operator read it."""
        trust = hook_trust.status(self.root, self.hooks.bootstrap)
        return {
            "key": self.key,
            "label": self.label,
            "path": display_path(self.root),
            "worktree_root": display_path(self.worktree_root),
            "base": self.base,
            "bootstrap": self.hooks.bootstrap is not None,
            "bootstrap_sha256": trust["sha256"],
            "bootstrap_trusted": trust["trusted"],
            "sparse_exclude": list(self.hooks.sparse_exclude),
            **({"settings_errors": list(self.hooks.errors)} if self.hooks.errors else {}),
        }


def repo_spec(main_checkout: Path, *, label: str | None = None) -> RepoSpec:
    """The spec for a repository the router serves: state keyed by `repo_key`,
    the base resolved against the repository (one bounded git call or two)."""
    hooks = read_repo_hooks(main_checkout)
    key = repo_key(main_checkout)
    return RepoSpec(
        key=key,
        label=label or repo_dir_name(main_checkout),
        root=main_checkout,
        worktree_root=worktree_root_for(main_checkout),
        base=resolve_base_branch(hooks, main_checkout),
        hooks=hooks,
        store_path=resolve_store_path(key),
    )


def start_dir_spec(context: object) -> RepoSpec:
    """The plugin built on its own: the bridge's start directory, with the
    unkeyed state paths and no git call, as a single-repository host had it."""
    root = Path(getattr(context, "start_dir", None) or Path.cwd())
    hooks = read_repo_hooks(root)
    return RepoSpec(
        key=repo_key(root),
        label=repo_dir_name(root),
        root=root,
        worktree_root=worktree_root_for(root),
        base=resolve_base_branch(hooks),
        hooks=hooks,
        store_path=resolve_store_path(),
    )


def resolve_base_branch(hooks: RepoHooks | None = None, main_checkout: str | os.PathLike[str] | None = None) -> str:
    """`WORKTREE_BASE_BRANCH`, else the repository's `base`, else `main`, else
    (a repository with no `main`) the branch its main checkout is on. The plugin
    and the CLI both call this, so the two doors agree on where a branch starts."""
    chosen = os.environ.get(BASE_BRANCH_ENV) or (hooks.base if hooks else None)
    if chosen:
        return chosen
    if main_checkout is not None:
        from worktree.create import branch_exists, current_branch  # create imports names, never this module

        if not branch_exists(Path(main_checkout), DEFAULT_BASE_BRANCH):
            return current_branch(Path(main_checkout)) or DEFAULT_BASE_BRANCH
    return DEFAULT_BASE_BRANCH
