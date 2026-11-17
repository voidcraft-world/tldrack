"""Branch → worktree path → tmux session name. PURE, and the only place those
three names are derived from one another.

Determinism is the idempotency guard, exactly as in `coordinator/resume.py`:
CREATE clicked twice must produce the same session name so the second click can
short-circuit instead of forking a second agent into the same worktree.

The SLUG is the join key. A branch may contain `/` and `.` (git allows both) but
a tmux session name may not — the bridge pins session names to
`SESSION_NAME_RE = ^[A-Za-z0-9_-]+$` and the front end re-validates. So one slug
feeds both the directory name and the session name, which also means two branches
that slug alike collide on the DIRECTORY first — and a path that already exists
is a refusal, not a silent overwrite. That makes the filesystem the uniqueness
guard and keeps this module free of any collision bookkeeping.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

#: `wt-` is a PREFIX on purpose. `multi-terminal-layout.ts::WORKTREE_SUFFIX_RE`
#: strips a trailing `-wt`/`-worktree` marker when it derives a cohort label, so
#: a suffix here would be eaten by the rack and every worktree session would
#: render under a label it does not have.
SESSION_PREFIX = "wt"

#: Long enough to recognise the branch in `tmux ls`, short enough that the rack
#: row's name is not the whole row. Matches the coordinator's SLUG_MAX.
SLUG_MAX = 32

#: Where worktrees live, one root per repository: owned by `repo.py`
#: (`worktree_root_for`) — see the note there.

#: What we are willing to MINT. Much stricter than git's own rules by choice:
#: this string becomes a directory name and part of a slug, and the narrow set
#: costs nothing to type. It is NOT "what git accepts" — a branch that already
#: exists is judged by `validate_existing_branch` instead, and the difference
#: between the two is the whole reason that function exists.
BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")

BRANCH_MAX = 80

#: What `git check-ref-format --branch` refuses ANYWHERE in a name, probed
#: against the installed git rather than recalled from the man page: space,
#: `~ ^ : ? * [ \`, and every ASCII control character (DEL included).
#: Deliberately NOT a list of shell metacharacters — see
#: `validate_existing_branch` for why `a;b` is safe here and `-b` is not.
_REF_BAD_CHARS = frozenset(" ~^:?*[\\\x7f") | {chr(code) for code in range(0x20)}

#: A ref that already EXISTS may be longer than one we would mint — it was
#: named elsewhere, by someone who did not know about `BRANCH_MAX`. Still
#: bounded: the name reaches an argv and a log line.
EXISTING_BRANCH_MAX = 255

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def validate_branch(branch: object) -> str | None:
    """None when the branch name is usable, else the reason it is not.

    Returning the REASON rather than a bool is what lets the refusal table
    surface something a user can act on; every message here is written to be
    read in a rack row, not a log.
    """
    if not isinstance(branch, str) or not branch.strip():
        return "a branch name is required"
    name = branch.strip()
    if len(name) > BRANCH_MAX:
        return f"branch name is longer than {BRANCH_MAX} characters"
    if not BRANCH_RE.fullmatch(name):
        return "branch names may use letters, digits, '.', '_', '-' and '/', and must start with a letter or digit"
    # git's own refusals, spelled here so the failure arrives before anything is
    # created rather than as a `git worktree add` stderr dump.
    if ".." in name:
        return "branch names may not contain '..'"
    if "//" in name:
        return "branch names may not contain '//'"
    if name.endswith("/") or name.endswith(".") or name.endswith(".lock"):
        return "branch names may not end with '/', '.' or '.lock'"
    if slugify(name) == "":
        return "branch name has no letters or digits to build a session name from"
    return None


def validate_existing_branch(branch: object) -> str | None:
    """None when a branch that ALREADY EXISTS is safe to name in a git
    command, else the reason it is not.

    Deliberately not `validate_branch`, and the split is the point.
    `validate_branch` is a MINTING policy: on the create path the name becomes
    a directory, a tmux session and a slug, so a narrow alphabet costs nothing
    and buys determinism. This answers a different question — "git already has
    this ref; may we hand it to `git branch -d`?" — where the name becomes one
    argv element and nothing else.

    Holding the minting alphabet over that question strands branches git lists
    happily. A branch like `fix-+-retry` would sit in the harbor as an
    undeletable atoll: `+` is not in `BRANCH_RE`, so the refusal table's first
    row would fire, while `git branch -d` takes the same name without complaint.

    So the rules are git's own (`git check-ref-format --branch`) plus ONE of
    ours: a leading `-` is refused, because `git branch -d -D` is an option
    and not a branch. Shell metacharacters are NOT refused — every fork goes
    through `create.run_bounded`, which hands `subprocess.run` a LIST and
    never a shell string, so `a;b` is one argument and means nothing else.
    """
    if not isinstance(branch, str) or not branch.strip():
        return "a branch name is required"
    name = branch.strip()
    if len(name) > EXISTING_BRANCH_MAX:
        return f"branch name is longer than {EXISTING_BRANCH_MAX} characters"
    # Ours, not git's: option injection is the one way an argv element can
    # stop being an operand.
    if name.startswith("-"):
        return "branch names may not start with '-'"
    if any(char in _REF_BAD_CHARS for char in name):
        return "branch names may not contain spaces, control characters, or any of '~ ^ : ? * [ \\'"
    # git's own refusals, spelled here so the failure arrives as a sentence a
    # rack row can print rather than a `git branch -d` stderr dump.
    if ".." in name:
        return "branch names may not contain '..'"
    if "@{" in name:
        return "branch names may not contain '@{'"
    if name == "@":
        return "'@' is not a branch name"
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return "branch names may not start or end with '/', or contain '//'"
    if name.endswith("."):
        return "branch names may not end with '.'"
    for part in name.split("/"):
        if part.startswith("."):
            return "no part of a branch name may start with '.'"
        if part.endswith(".lock"):
            return "no part of a branch name may end with '.lock'"
    return None


#: The bridge's own session alphabet: owned by bridge/tldrack/snapshot.py
#: (`SESSION_NAME_RE`) — a plugin cannot import the host, so this is its copy.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

#: A caller-supplied name reaches a `tmux` argv and a log line; the bridge
#: mints nothing near this long.
SESSION_NAME_MAX = 128


def validate_session_name(session: object) -> str | None:
    """None when `session` is a name the bridge could have listed, else why
    not. For a session a CALLER names (`/cleanup`'s cwd-joined pane); the ones
    this plugin derives come from `session_name_for` and always pass."""
    if not isinstance(session, str) or not session:
        return "a session name is required"
    if len(session) > SESSION_NAME_MAX:
        return f"session name is longer than {SESSION_NAME_MAX} characters"
    if not SESSION_NAME_RE.fullmatch(session):
        return "session names may use letters, digits, '_' and '-'"
    return None


def slugify(text: object) -> str:
    """Lowercase, non-alphanumerics collapsed to `-`, truncated.

    Total: anything unusable slugs to the empty string rather than raising, and
    the caller decides what that means (here: `validate_branch` refuses it).
    """
    if not isinstance(text, str):
        return ""
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    return slug[:SLUG_MAX].strip("-")


def session_name_for(branch: object) -> str:
    """`wt-<branch slug>` — pure and deterministic.

    Always matches the bridge's `SESSION_NAME_RE`, because the slug alphabet is
    a subset of it and the prefix is literal.
    """
    return f"{SESSION_PREFIX}-{slugify(branch)}"


def worktree_path_for(branch: object, root: Path) -> Path:
    """The directory this branch's worktree occupies.

    Built from the SLUG, not the raw branch: a branch called `fix/nav/tooltip`
    would otherwise create three nested directories, and `git worktree remove`
    would leave the empty parents behind forever.
    """
    return root / slugify(branch)


def is_managed_path(path: object, root: Path) -> bool:
    """Is this worktree one of OURS, under this repository's `root`?

    Other tools make worktrees too, and a repository's main checkout is one.
    Those must never appear in this plugin's managed list and, far more
    importantly, must never be reachable by `POST /remove`. Containment is
    checked on RESOLVED paths so a `..` cannot walk out.
    """
    if not isinstance(path, (str, Path)) or not str(path):
        return False
    base = Path(root).expanduser()
    try:
        candidate = Path(path).expanduser().resolve()
        base_resolved = base.resolve()
    except OSError:
        return False
    return candidate != base_resolved and base_resolved in candidate.parents


def display_path(path: str | os.PathLike[str] | None) -> str | None:
    """`~`-collapsed: the only form a GET body of this plugin's harbor routes
    carries. Every page the bridge serves can read a GET body, and the
    operator's account name is not that page's business.
    """
    if path is None:
        return None
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def scrub_home(text: str | None) -> str | None:
    """`display_path` for FREE TEXT: every home-directory prefix inside a
    message becomes `~`.

    `display_path` collapses a value that IS a path. Error text is where paths
    hide instead — git's `fatal: cannot change to '/Users/x/…'`, a worker's
    `FileNotFoundError: [Errno 2] … '/Users/x/…'` — and it reaches the same
    GET body through `git_error`, `error`, `fetch.error`, `pr_index.error`,
    `create_error` and `pr_job.error`. The invariant is the whole body, not
    the path fields, so the text channel gets the same treatment. Matched on
    a path boundary, as `display_path` is: `/Users/x2/…` is a sibling, not
    home. Idempotent (a scrubbed message has nothing left to match); None
    passes through.
    """
    if text is None:
        return None
    home = str(Path.home())
    if not home or home == os.sep:
        return text  # a root home would turn every path into `~/…`
    return _home_in_text_re(home).sub("~", text)


@lru_cache(maxsize=4)
def _home_in_text_re(home: str) -> re.Pattern[str]:
    # `home` followed by anything that cannot continue a path segment — a
    # separator, a quote, whitespace, punctuation, or the end of the text.
    return re.compile(re.escape(home) + r"(?![A-Za-z0-9._-])")


def is_primary_checkout(path: object, main_path: object) -> bool:
    """Is `path` the main checkout — the one `git worktree list` prints FIRST?

    Compared on RESOLVED paths, like `is_managed_path`, so a symlinked
    `start_dir` and the path git stores for the same directory agree. Total:
    anything unresolvable is not the main checkout.
    """
    if not isinstance(path, (str, Path)) or not str(path):
        return False
    if not isinstance(main_path, (str, Path)) or not str(main_path):
        return False
    try:
        return Path(path).expanduser().resolve() == Path(main_path).expanduser().resolve()
    except OSError:
        return False
