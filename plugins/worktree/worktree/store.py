"""Per-branch job state: durable PR outcomes, and in-memory create and cleanup
progress.

`PrStore` is the persisted one, in the bridge's own store idiom. `CreateStore`
and `CleanupStore` below it are deliberately not persisted — `BranchJobs`'
docstring carries the reason, which is the mirror image of the section below.

The store idiom itself — the `{version, ...}` envelope, the tolerant load, the
atomic write: owned by `plugins/coordinator/coordinator/store.py` — see
the note there. The plumbing every persisted store here shares lives in
`row_store.py`.

── Why THIS is the only thing persisted ───────────────────────────────────
There is deliberately no session→worktree mapping stored anywhere. `git worktree
list --porcelain` already knows every worktree and its branch, and
`names.session_name_for` derives the session name from the branch by a pure
function — so the whole join is recomputable, and a store would only be a second
copy able to disagree with git. It would also inherit `/rename-session`'s
key-moving problem for free, which is a maintenance burden bought for nothing.

PR state is different in kind: it is the OUTCOME of a background job, it cannot
be recomputed without a network call to GitHub, and it has to survive a bridge
restart or a `gh pr create` that finished after the window was closed.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from worktree.row_store import BranchRowStore, clean_rows, read_rows

STORE_FILE_VERSION = 1

STORE_FILE_NAME = "worktree-prs.json"
DEFAULT_STORE_PATH = Path.home() / ".local" / "state" / "tldrack" / STORE_FILE_NAME

#: Each repository's state sits one directory down, `worktree/<repo key>/`.
REPOS_DIR_NAME = "worktree"

#: The states a branch's PR job can be in. `opening` is the only transient one,
#: and it is what the front end's rescan ladder keys off.
PR_STATES = ("opening", "open", "error")


def resolve_store_path(repo_key: str | None = None) -> Path:
    """`TLDRACK_WORKTREE_PATH`, else the default. With a `repo_key`, the same
    file name under `worktree/<key>/` beside it: state is keyed by repository,
    so one rack serving two repositories never mixes their PRs, briefs or task
    groups. Every other store here derives from this path
    (`resolve_pr_index_path` owns why)."""
    env_path = os.environ.get("TLDRACK_WORKTREE_PATH")
    base = Path(env_path) if env_path else DEFAULT_STORE_PATH
    return base if repo_key is None else base.parent / REPOS_DIR_NAME / repo_key / base.name


#: The persisted PR index (`harbor.PrIndexStore`) — the outcome of `gh pr
#: list`, so it is persisted for the same reason `PrStore` is.
PR_INDEX_FILE_NAME = "worktree-pr-index.json"


#: The persisted briefs (`harbor.BriefStore`) — each one cost a model call,
#: so it is kept for the same reason the PR index is. Same derivation rule.
BRIEF_FILE_NAME = "worktree-briefs.json"


def resolve_brief_path(store_path: Path | None = None) -> Path:
    """`TLDRACK_BRIEF_PATH`, else a sibling of the PR store's file —
    see `resolve_pr_index_path` for why it derives rather than defaults."""
    env_path = os.environ.get("TLDRACK_BRIEF_PATH")
    if env_path:
        return Path(env_path)
    return (store_path or resolve_store_path()).parent / BRIEF_FILE_NAME


#: The persisted adopt anchors (`AdoptStore`) — which branches have had their
#: uncommitted work rescued onto the target, and the tree that proves it. Kept
#: for the same reason the PR index is: it is the OUTCOME of a job, and it is
#: what lets a later forced remove say whether it destroys anything.
ADOPT_FILE_NAME = "worktree-adopted.json"


def resolve_adopt_path(store_path: Path | None = None) -> Path:
    """`TLDRACK_ADOPT_PATH`, else a sibling of the PR store's file —
    see `resolve_pr_index_path` for why it derives rather than defaults."""
    env_path = os.environ.get("TLDRACK_ADOPT_PATH")
    if env_path:
        return Path(env_path)
    return (store_path or resolve_store_path()).parent / ADOPT_FILE_NAME


#: The persisted ship of the main checkout (`ship.ShipStore`) — an open PR and
#: the two commits SYNC needs, which nothing else on the machine remembers.
SHIP_FILE_NAME = "worktree-ship.json"


def resolve_ship_path(store_path: Path | None = None) -> Path:
    """`TLDRACK_SHIP_PATH`, else a sibling of the PR store's file —
    see `resolve_pr_index_path` for why it derives rather than defaults."""
    env_path = os.environ.get("TLDRACK_SHIP_PATH")
    if env_path:
        return Path(env_path)
    return (store_path or resolve_store_path()).parent / SHIP_FILE_NAME


def resolve_pr_index_path(store_path: Path | None = None) -> Path:
    """`TLDRACK_PR_INDEX_PATH`, else a sibling of the PR store's file.

    Derived from `resolve_store_path()` rather than from a second default on
    purpose: a test fixture that relocates the PR store into a tmpdir has
    thereby relocated this file too, and can never read the developer's real
    index by forgetting a second env var.
    """
    env_path = os.environ.get("TLDRACK_PR_INDEX_PATH")
    if env_path:
        return Path(env_path)
    return (store_path or resolve_store_path()).parent / PR_INDEX_FILE_NAME


#: The task groups' database, beside the PR store.
TASK_GROUPS_FILE_NAME = "worktree-task-groups.sqlite"

#: The content proofs' patch-id cache (`landing.PatchIdStore`), beside the PR index.
PATCH_ID_FILE_NAME = "worktree-patch-ids.json"

#: Every file a repository's state is made of; `adopt_unkeyed_state` copies exactly these.
STATE_FILE_NAMES = (
    STORE_FILE_NAME,
    PR_INDEX_FILE_NAME,
    BRIEF_FILE_NAME,
    ADOPT_FILE_NAME,
    SHIP_FILE_NAME,
    TASK_GROUPS_FILE_NAME,
    PATCH_ID_FILE_NAME,
)


def adopt_unkeyed_state(store_path: Path) -> list[str]:
    """Copy the state a single-repository plugin left in the parent state
    directory into one repository's keyed directory, once. → the names copied.

    Only for the repository the bridge starts in: that is whose state the
    unkeyed files are. Runs only while the keyed directory does not exist yet,
    so a second start never overwrites newer state, and it COPIES: the unkeyed
    files stay for an older bridge to read after a rollback.
    """
    keyed_dir = store_path.parent
    unkeyed_dir = keyed_dir.parent.parent
    if keyed_dir.exists() or keyed_dir.parent.name != REPOS_DIR_NAME:
        return []
    copied = []
    for name in STATE_FILE_NAMES:
        source = unkeyed_dir / name
        if source.is_file():
            keyed_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, keyed_dir / name)
            copied.append(name)
    return copied


def _pr_row(_branch: str, row: dict) -> dict | None:
    status = row.get("status")
    if status not in PR_STATES:
        return None
    return {
        "status": status,
        "url": row.get("url") if isinstance(row.get("url"), str) else None,
        "error": row.get("error") if isinstance(row.get("error"), str) else None,
        "updated_at": row.get("updated_at") if isinstance(row.get("updated_at"), (int, float)) else None,
    }


def parse_store_file(text: str) -> dict[str, dict]:
    """File body → `{branch: row}`. Bad rows are dropped, not fatal.

    Raises ValueError on a wrong ENVELOPE so the caller can warn about a corrupt
    file; a single malformed row must never cost the user every other branch's
    PR link.
    """
    _envelope, rows = read_rows(text, "prs")
    return clean_rows(rows, _pr_row)


class PrStore(BranchRowStore):
    """Thread-safe `{branch: {status, url, error, updated_at}}`.

    Every mutation happens under the lock and is persisted inside it, so a
    background PR worker and a `GET /state` on a request thread cannot interleave
    a half-updated row. `get` / `drop` / `snapshot` and the load-and-write
    plumbing are `BranchRowStore`'s; this class owns the envelope and `set`.
    """

    _kept = "PR state"
    _parse = staticmethod(parse_store_file)

    def _body(self) -> str:
        return json.dumps({"version": STORE_FILE_VERSION, "prs": self._rows}, indent=2)

    def set(self, branch: str, *, status: str, url: str | None = None, error: str | None = None) -> None:
        with self._lock:
            self._rows[branch] = {"status": status, "url": url, "error": error, "updated_at": time.time()}
            self._persist()

    def drop(self, branch: str) -> None:
        """Forget a branch — called by `/remove`, so a re-created branch of the
        same name does not inherit the previous one's PR link."""
        super().drop(branch)


# ── Create-job state ───────────────────────────────────────────────────────
#
# The four steps `POST /create` walks, in order. The front end renders the
# CURRENT one as a label and its index as a determinate meter, so this tuple is
# a contract: `rack-view.ts::create_row_view` re-declares it, and the two must
# stay in the same order.
CREATE_STEPS = ("worktree", "bootstrap", "session", "prompt")

#: `creating` is the transient one; `error` is terminal and expires.
CREATE_STATES = ("creating", "error")

#: How long a FAILED create stays visible. A row that says "failed" forever with
#: no way to dismiss it is worse than one that lingers; five minutes is long
#: enough to walk back to the machine and short enough not to become furniture.
#: A retry of the same branch clears it immediately regardless.
CREATE_ERROR_TTL_SECONDS = 300.0


class BranchJobs:
    """Thread-safe `{branch: {status, error, updated_at, ...}}` — IN MEMORY,
    one in-process job per branch, a failed one expiring after
    `CREATE_ERROR_TTL_SECONDS`. `CreateStore` and `CleanupStore` are its two
    shapes; a subclass names its transient status and the fields a claim
    starts with.

    ── Why these are not persisted, when `PrStore` is ─────────────────────
    Read `PrStore`'s docstring first: PR state is persisted because it is the
    outcome of a NETWORK call that must survive a bridge restart, and cannot be
    recomputed without asking GitHub again.

    A create or cleanup job is the opposite in every respect. Its worker is a
    thread in THIS process, so a bridge restart kills the work — and a
    persisted running row would then describe a job that no longer exists and
    can never resolve. That is exactly the wedged-`opening` failure
    `_handle_pr` spends an exception handler preventing, except permanent and
    un-clearable. Restarting the bridge is the one thing guaranteed to make
    this state garbage, so the process lifetime is precisely the right
    lifetime for it.

    Nothing is lost by forgetting: `git worktree list` already knows whether the
    worktree is there, which is the only durable question.
    """

    #: The transient status — the one a claim takes and `claim` refuses over.
    running = "running"

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        # Injected so the TTL is testable without sleeping. Monotonic rather
        # than wall-clock: an NTP step must not retire a live error early or
        # strand a dead one.
        self._clock = clock

    def _claimed(self) -> dict:
        """The subclass's own fields on a fresh claim; a failure keeps them."""
        return {}

    def _expired(self, row: dict, now: float) -> bool:
        return row["status"] == "error" and now - row["updated_at"] >= CREATE_ERROR_TTL_SECONDS

    def claim(self, branch: str) -> bool:
        """Test-and-set: True if THIS caller now owns the job for `branch`.

        This is the whole mutual exclusion for the route, and it replaces
        holding a lock across the job's subprocesses. Two overlapping clicks
        (a double-click, or two rack windows) both reach here; exactly one
        gets True and starts a worker.

        A stale `error` row does not block a retry — it is cleared here, which
        is what makes "press it again" work without waiting out the TTL.
        """
        with self._lock:
            existing = self._rows.get(branch)
            if existing and existing["status"] == self.running:
                return False
            self._rows[branch] = {"status": self.running, **self._claimed(), "error": None, "updated_at": self._clock()}
            return True

    def fail(self, branch: str, error: str) -> None:
        with self._lock:
            row = self._rows.get(branch) or {}
            self._rows[branch] = {
                **{key: row.get(key) for key in self._claimed()},
                "status": "error",
                "error": error[:300],
                "updated_at": self._clock(),
            }

    def finish(self, branch: str) -> None:
        """Forget a branch that succeeded. `git worktree list` is the answer
        by now, and this row would only be a second copy able to disagree."""
        with self._lock:
            self._rows.pop(branch, None)

    def drop(self, branch: str) -> None:
        """Forget a branch outright — `/remove`'s hook, so a re-created branch
        of the same name does not inherit a stale error."""
        with self._lock:
            self._rows.pop(branch, None)

    def drop_settled(self, branch: str) -> None:
        """`drop`, except a job still running keeps its row. CLEAN's worker
        removes its tree and deletes its branch through `/remove`'s and
        `/delete-branch`'s own internals, whose hooks call this — and a running
        row is what `busy_reasons` holds a bridge restart on until the job
        settles itself (`finish` / `fail`)."""
        with self._lock:
            row = self._rows.get(branch)
            if row and row["status"] != self.running:
                del self._rows[branch]

    def get(self, branch: str) -> dict | None:
        with self._lock:
            row = self._rows.get(branch)
            if not row:
                return None
            if self._expired(row, self._clock()):
                del self._rows[branch]
                return None
            return dict(row)

    def snapshot(self) -> dict[str, dict]:
        """Every live row, expiring stale errors on the way out — the same
        subtract-on-read shape the coordinator plugin uses for dismissals."""
        with self._lock:
            now = self._clock()
            for branch in [b for b, row in self._rows.items() if self._expired(row, now)]:
                del self._rows[branch]
            return {branch: dict(row) for branch, row in self._rows.items()}


class CreateStore(BranchJobs):
    """`/create`'s jobs: `{branch: {status, step, error, updated_at}}`, walking
    `CREATE_STEPS`. A losing `claim` is the same create already running, and
    `/create` reports it as the idempotent success it is."""

    running = "creating"

    def _claimed(self) -> dict:
        return {"step": CREATE_STEPS[0]}

    def advance(self, branch: str, step: str) -> None:
        """Move a claimed branch to `step`. A no-op for a branch that is not
        creating, so a late worker cannot resurrect a row `/remove` dropped."""
        with self._lock:
            row = self._rows.get(branch)
            if not row or row["status"] != self.running:
                return
            row["step"] = step
            row["updated_at"] = self._clock()


#: `running` is the transient one; `error` is terminal and expires like a create's.
CLEANUP_STATES = ("running", "error")


class CleanupStore(BranchJobs):
    """`/cleanup`'s jobs: `{branch: {status, error, updated_at}}`. A losing
    `claim` is a second click on a cleanup already running — a 409, because
    unlike a create there is nothing to hand back."""


# ── Adopt anchors ──────────────────────────────────────────────────────────
#
# One row per branch whose uncommitted work has been adopted onto the target.
# Persisted, because the question it answers is asked days later and by a
# DIFFERENT surface: "is this forced remove destroying anything, or did I
# already rescue it?" A bridge restart must not turn a safe delete back into a
# scary one.
#
# `tree` is the load-bearing field. It is the git tree of everything the
# worktree held at adopt time, so comparing it against a fresh snapshot answers
# staleness exactly — including the case where an edit was made and then undone,
# which a timestamp would get wrong in both directions.


def _adopt_row(_branch: str, row: dict) -> dict | None:
    tree = row.get("tree")
    if not isinstance(tree, str) or not tree:
        return None  # without the tree the row cannot answer anything
    files = row.get("files")
    return {
        "tree": tree,
        "commit": row.get("commit") if isinstance(row.get("commit"), str) else None,
        "ref": row.get("ref") if isinstance(row.get("ref"), str) else None,
        "files": [f for f in files if isinstance(f, str)] if isinstance(files, list) else [],
        "at": row.get("at") if isinstance(row.get("at"), (int, float)) else None,
    }


def parse_adopt_file(text: str) -> dict[str, dict]:
    """File body → `{branch: row}`. Bad rows are dropped, not fatal — the same
    tolerance `parse_store_file` applies, and for the same reason."""
    _envelope, rows = read_rows(text, "adopted", article="an")
    return clean_rows(rows, _adopt_row)


class AdoptStore(BranchRowStore):
    """Thread-safe `{branch: {tree, commit, ref, files, at}}`, persisted.

    `PrStore`'s twin: same plumbing from `BranchRowStore`, a different envelope
    and a different `set`.
    """

    _kept = "adopt anchors"
    _parse = staticmethod(parse_adopt_file)

    def _body(self) -> str:
        return json.dumps({"version": STORE_FILE_VERSION, "adopted": self._rows}, indent=2)

    def set(self, branch: str, *, tree: str, commit: str | None, ref: str | None, files: list[str]) -> None:
        with self._lock:
            self._rows[branch] = {"tree": tree, "commit": commit, "ref": ref, "files": list(files), "at": time.time()}
            self._persist()

    def drop(self, branch: str) -> None:
        """Forget a branch. Called when the BRANCH is deleted, never when its
        worktree is removed: the anchor ref outlives the worktree on purpose,
        and a re-created branch of the same name is a different thing."""
        super().drop(branch)
