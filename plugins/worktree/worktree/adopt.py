"""Getting work OUT of a worktree, and bringing a stale branch forward.

Two verbs the harbor was missing. `POST /remove` could destroy uncommitted work
and `POST /delete-branch` could destroy a ref, but nothing could RESCUE either —
a dirty scratch worktree with no commits was a dead end, exitable only by
copying files by hand. `adopt` is that exit; `catch_up` is the other half, for a
branch that has merely fallen behind.

── Why this is not `git diff | git apply` ─────────────────────────────────────
Measured on a real repository, the obvious implementation fails on the first real case
it meets. A worktree branched before main deleted a file, then edited that file,
produces a patch that cannot land:

    error: docs/notes/alpha-notes.html: does not exist in index

The base of the edit is gone from the target, so there is nothing to patch. A
patch is a diff against ONE tree; what is actually being asked for is a MERGE
against two. Linked worktrees share `.git/objects`, so the merge material is
already local — no copying, no network, no second checkout.

── The pipeline, and why each step is the one it is ───────────────────────────
1. `read-tree HEAD` + `add -A` + `write-tree` under a TEMPORARY `GIT_INDEX_FILE`
   turns "everything uncommitted, tracked and untracked" into a real tree object
   while the worktree's own index and working tree are never written. `git stash`
   would also do it and would MUTATE the source; this is a copy, by construction.
   `add -A` respects `.gitignore`, so `node_modules` and `.env` stay out for free.
2. `commit-tree` hangs that tree off the worktree's HEAD, giving the merge a
   commit to name and — after a successful adopt — something a ref can point at.
3. `merge-tree --write-tree --merge-base <worktree HEAD>` performs the whole
   three-way merge IN MEMORY and reports conflicts by exit status. Nothing has
   touched a working tree yet, which is what lets a plan be shown before a write.
4. `diff --raw` between the target and the merged tree is the delta: the only
   paths that change. A merge of a 49-commit-behind branch still touches four
   files if four files differ.
5. `read-tree` into a second temp index + ONE `checkout-index -f --` materialises
   every path in a single fork, with git deciding the modes. One `cat-file` per
   file would be N forks for a job git already does atomically.

── `modify/delete` is NOT a conflict, and calling it one would be a lie ───────
The measured case above resolves cleanly: git keeps the modified version, writes
NO conflict markers, and the merged blob is byte-identical to the file in the
worktree. Reporting that as a conflict would put a `force` in front of the one
outcome the user actually wants — their edits, rescued. It gets its own verdict
(`readd`) and needs no force. Every OTHER conflict type does write markers, so
those keep the force.

Hence `CONFLICT_SAFE_TYPES`: a strict allowlist. An unrecognised conflict type
is treated as a real conflict, because the failure that costs something is
calling a marker-writing merge safe.

── `blocked` is the rule protecting the target ───────────────────────────────
The main checkout is somebody's live working tree — on this machine it carries
~50 uncommitted files at rest. A path that is dirty on BOTH sides is refused
outright rather than merged: the target's copy has no other home, while the
source's is still sitting in the worktree. Refusing costs a second click; the
alternative costs work that was never committed anywhere.

Pure above the fold: builders, parsers, the verdict table. The I/O half at the
foot only runs commands. Every subprocess goes through `create_mod.run_bounded`
BY MODULE ATTRIBUTE — the test fence in `tests/` impersonates that attribute,
and an imported name slips past it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from worktree import changes as changes_mod
from worktree import create as create_mod

#: `git add -A` hashes every untracked, non-ignored file in the tree. Generous,
#: because a cold worktree on a spinning disk is the slow case and the user is
#: waiting on a plan, not a poll.
SNAPSHOT_TIMEOUT_SECONDS = 60

#: The in-memory merge, the delta, and the materialisation. All local object-DB
#: work on trees git already has.
MERGE_TIMEOUT_SECONDS = 30

#: `rev-parse`, `update-ref`, `rev-list --count` — index lookups, effectively.
REF_TIMEOUT_SECONDS = 10

#: A fast-forward that has to touch the working tree, plus an autostash pop.
FF_TIMEOUT_SECONDS = 120

#: Rows returned at most, matching `changes.MAX_FILES` so the two surfaces cut
#: at the same place. The COUNT still tells the truth; the list is what is cut.
MAX_FILES = changes_mod.MAX_FILES

#: Where a successful adopt anchors the rescued state. A ref, not a note in a
#: JSON file: an unreferenced commit is garbage-collectable, and the whole claim
#: this feature makes to the user is that the work cannot be lost afterwards.
#: Anchors written before the rename live under `refs/voidcraft/adopted/` and are
#: never moved: each adopt record names its own ref, and nothing enumerates them.
ADOPT_REF_PREFIX = "refs/tldrack/adopted/"

#: Conflict types git resolves WITHOUT writing conflict markers, and whose
#: resolution is the one the user is asking for. Strictly an allowlist —
#: see the module header. `modify/delete` keeps the modified version.
CONFLICT_SAFE_TYPES = frozenset({"modify/delete"})

#: Verdicts that may be adopted with no `force`. `conflict` is the only one
#: that writes markers into the target, so it is the only one that arms red.
SAFE_VERDICTS = frozenset({"new", "merge", "readd", "delete"})


def adopt_ref_for(branch: str) -> str:
    """The anchor ref for a branch. `refs/` is a flat namespace of our own, so
    a branch named `feature/x` nests harmlessly and cannot collide with
    `refs/heads`."""
    return f"{ADOPT_REF_PREFIX}{branch}"


# ── Argv builders (pure) ───────────────────────────────────────────────────


def build_read_tree_head_command(path: Path | str) -> list[str]:
    return ["git", "-C", str(path), "read-tree", "HEAD"]


def build_add_all_command(path: Path | str) -> list[str]:
    """`add -A`, not `add -u`: untracked files are most of what a scratch
    worktree holds, and they are exactly what a patch-based transfer drops."""
    return ["git", "-C", str(path), "add", "-A"]


def build_write_tree_command(path: Path | str) -> list[str]:
    return ["git", "-C", str(path), "write-tree"]


def build_commit_tree_command(repo_root: Path | str, tree: str, parent: str) -> list[str]:
    return ["git", "-C", str(repo_root), "commit-tree", tree, "-p", parent, "-m", "tldrack: adopt snapshot"]


def build_rev_parse_command(path: Path | str, rev: str) -> list[str]:
    return ["git", "-C", str(path), "rev-parse", "--verify", f"{rev}^{{commit}}"]


def build_merge_tree_command(repo_root: Path | str, base: str, ours: str, theirs: str) -> list[str]:
    """The oracle. `-z` because a path is arbitrary bytes and the newline form
    quotes anything with a space in it — a second escaping dialect to get wrong.
    `--name-only` trims the stage/mode columns we do not read; the conflict TYPE
    still arrives in the structured section, and the type is the whole point."""
    return ["git", "-C", str(repo_root), "merge-tree", "--write-tree", "-z", "--name-only", "--merge-base", base, ours, theirs]


def build_delta_command(repo_root: Path | str, ours: str, merged: str) -> list[str]:
    """`--no-renames` on purpose: a rename becomes delete + add, which
    materialises correctly with no rename bookkeeping. The target's working tree
    is not a git history and gains nothing from the similarity index."""
    return ["git", "-C", str(repo_root), "diff", "--raw", "-z", "--no-renames", ours, merged]


def build_target_read_tree_command(repo_root: Path | str, tree: str) -> list[str]:
    return ["git", "-C", str(repo_root), "read-tree", tree]


def build_checkout_index_command(repo_root: Path | str, paths: list[str]) -> list[str]:
    """ONE fork for every path. `--` fences the list, so a path beginning with
    a dash is an operand rather than an option."""
    return ["git", "-C", str(repo_root), "checkout-index", "-f", "--", *paths]


def build_update_ref_command(repo_root: Path | str, ref: str, oid: str) -> list[str]:
    return ["git", "-C", str(repo_root), "update-ref", ref, oid]


def build_ff_merge_command(worktree: Path | str, base: str, *, autostash: bool) -> list[str]:
    """`--ff-only` is the safety, not a preference: it refuses anything that
    would write a merge commit, so this verb can never invent history."""
    command = ["git", "-C", str(worktree), "merge", "--ff-only"]
    if autostash:
        command.append("--autostash")
    command.append(base)
    return command


def build_ff_fetch_command(repo_root: Path | str, base: str, branch: str) -> list[str]:
    """Advancing a branch that has no checkout. `fetch .` applies git's own
    fast-forward check to the ref update, so a non-FF is refused by git rather
    than by a test of ours that could disagree with it."""
    return ["git", "-C", str(repo_root), "fetch", "--no-tags", ".", f"{base}:{branch}"]


def build_count_command(path: Path | str, span: str) -> list[str]:
    return ["git", "-C", str(path), "rev-list", "--count", span]


# ── Parsers (pure) ─────────────────────────────────────────────────────────


def parse_merge_tree(text: object) -> dict:
    """`merge-tree -z --name-only` output → `{tree, conflicts: {path: type}}`.

    The stream is: the merged tree OID, then every conflicted path, then an
    EMPTY field closing that section, then one record per conflict —
    `<count>`, `<path>` × count, `<type>`, `<message>`.

    A stream we cannot parse is not an error: the paths came through in the
    first section regardless, and an unknown type already means "treat as a
    real conflict". So a parse failure degrades to the SAFE answer rather than
    to an exception on a request thread.
    """
    if not isinstance(text, str) or not text:
        return {"tree": None, "conflicts": {}}
    fields = text.split("\0")
    tree = fields[0].strip() or None
    conflicts: dict[str, str] = {}

    index = 1
    while index < len(fields) and fields[index] != "":
        conflicts[fields[index]] = "unknown"
        index += 1
    index += 1  # step over the empty separator

    while index < len(fields):
        raw_count = fields[index]
        if not raw_count.strip():
            break
        try:
            count = int(raw_count)
        except ValueError:
            break
        paths = fields[index + 1 : index + 1 + count]
        kind_field = fields[index + 1 + count] if index + 1 + count < len(fields) else ""
        for path in paths:
            conflicts[path] = conflict_type_of(kind_field)
        index += count + 3  # count + paths + type + message

    return {"tree": tree, "conflicts": conflicts}


def conflict_type_of(field: object) -> str:
    """`CONFLICT (modify/delete)` → `modify/delete`. Anything unrecognised
    stays `unknown`, which the verdict table treats as a real conflict."""
    if not isinstance(field, str):
        return "unknown"
    start = field.find("(")
    end = field.rfind(")")
    if start == -1 or end <= start:
        return "unknown"
    return field[start + 1 : end].strip() or "unknown"


def parse_delta(text: object) -> list[dict]:
    """`diff --raw -z` → `[{path, status, mode}]`, in git's order.

    Each record is `:<srcmode> <dstmode> <srcsha> <dstsha> <status>` in one
    NUL-terminated field, then the path in the next. Rename/copy records would
    carry a second path, but `--no-renames` means they cannot occur.
    """
    if not isinstance(text, str) or not text:
        return []
    fields = [field for field in text.split("\0")]
    rows: list[dict] = []
    index = 0
    while index + 1 < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        parts = meta[1:].split()
        if len(parts) < 5:
            index += 2
            continue
        rows.append(
            {
                "path": fields[index + 1],
                "status": parts[4][:1].upper(),
                "mode": parts[1],
            }
        )
        index += 2
    return rows


#: `diff --raw` status letter → what adopting that row does to the target.
#: `A` is "absent on the target", which for this feature is a NEW file.
STATUS_VERDICTS = {"A": "new", "D": "delete", "M": "merge", "T": "merge"}


def verdict_for(row: dict, conflicts: dict[str, str], target_dirty: set[str]) -> str:
    """One row's verdict. Order matters: `blocked` outranks everything, because
    a path the target has uncommitted edits on must not be written whatever the
    merge thought of it."""
    path = row["path"]
    if path in target_dirty:
        return "blocked"
    kind = conflicts.get(path)
    if kind is not None:
        return "readd" if kind in CONFLICT_SAFE_TYPES else "conflict"
    return STATUS_VERDICTS.get(row["status"], "merge")


def build_plan(delta: list[dict], conflicts: dict[str, str], target_dirty: set[str]) -> dict:
    """The whole answer both `/adopt-plan` and `POST /adopt` render from —
    computed once here so the plan the user approves and the plan that runs
    cannot differ by construction."""
    files = []
    for row in delta:
        verdict = verdict_for(row, conflicts, target_dirty)
        files.append({"path": row["path"], "verdict": verdict, "status": row["status"]})
    files.sort(key=lambda row: row["path"])
    blocked = [row["path"] for row in files if row["verdict"] == "blocked"]
    conflicted = [row["path"] for row in files if row["verdict"] == "conflict"]
    return {
        "files": files[:MAX_FILES],
        "total": len(files),
        "truncated": len(files) > MAX_FILES,
        "blocked": blocked,
        "conflicts": conflicted,
        "clean": not blocked and not conflicted,
        "adoptable": bool(files) and not blocked,
    }


def summarize_plan(plan: dict) -> str:
    """One mono line for the panel head. Non-zero terms only — a zero term is
    noise, and `0 BLOCKED` reads as a warning about nothing."""
    total = plan["total"]
    if total == 0:
        return "NOTHING TO ADOPT"
    terms = [f"{total} FILE" if total == 1 else f"{total} FILES"]
    counts: dict[str, int] = {}
    for row in plan["files"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    for verdict, label in (("new", "NEW"), ("readd", "RE-ADD"), ("conflict", "CONFLICT"), ("blocked", "BLOCKED")):
        if counts.get(verdict):
            terms.append(f"{counts[verdict]} {label}")
    return " · ".join(terms)


#: Wording shared by the two refusals that are about someone else's job.
CATCH_UP_REBASE = "{ahead} {noun} not on {base} — rebase by hand"
CATCH_UP_DIRTY = "uncommitted changes conflict with the {behind} incoming {noun} — ADOPT them first"


def decide_catch_up(
    *,
    known: bool,
    is_base: bool,
    creating: bool,
    behind: int | None,
    ahead: int | None,
    base_behind_remote: int | None,
    base: str,
    name_error: str | None = None,
) -> tuple[int, dict] | None:
    """`(status, body)` to refuse with, or None to proceed.

    The mirror of `branches.decide_delete`: a pure table, so the route and the
    row verdict cannot drift, and a test can assert that a refusal forked
    nothing. Order is by how badly the caller has misunderstood the situation,
    not by cost.

    The `ahead > 0` refusal is the whole safety argument of this verb. A
    fast-forward cannot lose a commit or invent one; a rebase can do both, and
    a rebase that stops halfway leaves a worktree in a state no button can get
    it out of. Refusing is not a limitation to be lifted later — it is what
    lets this run with no confirmation step at all.
    """
    if name_error is not None:
        return 400, {"error": name_error}
    if not known:
        return 404, {"error": "no local branch by that name"}
    if is_base:
        return 409, {"error": f"'{base}' is the base — it is what the others catch up TO"}
    if creating:
        return 409, {"error": branches_creating_reason()}
    if isinstance(base_behind_remote, int) and base_behind_remote > 0:
        return 409, {"error": f"{base} is ↓{base_behind_remote} behind its upstream — FETCH first"}
    if behind is None:
        return 409, {"error": f"how far behind {base} this is could not be read"}
    if behind == 0:
        return 409, {"error": f"already up to date with {base}"}
    if isinstance(ahead, int) and ahead > 0:
        noun = "commit is" if ahead == 1 else "commits are"
        return 409, {"error": CATCH_UP_REBASE.format(ahead=ahead, noun=noun, base=base), "needs_rebase": True, "ahead": ahead}
    return None


def branches_creating_reason() -> str:
    """Imported lazily so this module stays importable on its own — `branches`
    pulls in the whole ref sweep for one string."""
    from worktree import branches as branches_mod

    return branches_mod.CREATING_REASON


def refuse_dirty_conflicts(paths: list[str], behind: int) -> tuple[int, dict]:
    """The refusal that points at the other verb. `stash pop` stops on ANY
    conflict, so a dirty worktree whose edits collide with what is coming in
    cannot be fast-forwarded safely — but adopting those edits first empties
    the collision, which is exactly the flow this pairs with."""
    noun = "commit" if behind == 1 else "commits"
    return 409, {"error": CATCH_UP_DIRTY.format(behind=behind, noun=noun), "needs_adopt": True, "conflicts": paths[:MAX_FILES]}


# ── I/O ────────────────────────────────────────────────────────────────────


def _run(command: list[str], timeout: int, env: dict | None = None):
    return create_mod.run_bounded(command, timeout, env=env)


def fail_text(err: str, fallback: str) -> str:
    """git's stderr, trimmed to something a toast can hold. Absolute paths are
    scrubbed by the ROUTE (`plugin._branches`' invariant), not here — this
    module has no opinion about display."""
    return (err or fallback).strip()[:300]


def worktree_is_dirty(worktree: Path) -> bool:
    """Anything uncommitted, untracked included. `False` on a failed probe is
    deliberate HERE and nowhere else: its one caller uses it to decide whether
    a fast-forward needs `--autostash`, and `--ff-only` will refuse on its own
    if there is work in the way. Guessing wrong costs a clear git error, not
    data — unlike the harbor row's `dirty`, where unknown must never read as
    clean."""
    code, out, _err = _run(changes_mod.build_status_command(worktree), changes_mod.CHANGES_TIMEOUT_SECONDS)
    return bool((out or "").strip()) if code == 0 else False


def snapshot_dirty_tree(worktree: Path) -> tuple[str | None, str | None]:
    """Everything uncommitted in `worktree`, as a tree object. `(oid, error)`.

    The temp index lives in a temp DIRECTORY rather than a temp FILE: git wants
    to create the index itself, and handing it a path that already exists as an
    empty file is how you get `index file smaller than expected`.
    """
    with tempfile.TemporaryDirectory(prefix="worktree-adopt-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        code, _out, err = _run(build_read_tree_head_command(worktree), MERGE_TIMEOUT_SECONDS, env)
        if code != 0:
            return None, fail_text(err, "could not read the worktree's HEAD")
        code, _out, err = _run(build_add_all_command(worktree), SNAPSHOT_TIMEOUT_SECONDS, env)
        if code != 0:
            return None, fail_text(err, "could not stage the worktree's changes")
        code, out, err = _run(build_write_tree_command(worktree), MERGE_TIMEOUT_SECONDS, env)
        if code != 0:
            return None, fail_text(err, "could not snapshot the worktree")
        oid = (out or "").strip()
        return (oid, None) if oid else (None, "git wrote no tree")


def rev_of(path: Path, rev: str) -> tuple[str | None, str | None]:
    code, out, err = _run(build_rev_parse_command(path, rev), REF_TIMEOUT_SECONDS)
    if code != 0:
        return None, fail_text(err, f"could not resolve {rev}")
    oid = (out or "").strip()
    return (oid, None) if oid else (None, f"could not resolve {rev}")


def target_dirty_paths(repo_root: Path) -> tuple[set[str], str | None]:
    """Every path the TARGET has uncommitted work on — the `blocked` set."""
    code, out, err = _run(changes_mod.build_status_command(repo_root), changes_mod.CHANGES_TIMEOUT_SECONDS)
    if code != 0:
        return set(), fail_text(err, "could not read the target's status")
    return {row["path"] for row in changes_mod.parse_status(out)}, None


def plan_adopt(repo_root: Path, worktree: Path, target_rev: str = "HEAD") -> tuple[dict | None, str | None]:
    """The read half, end to end. Writes objects to the shared database — trees
    and one commit — but no working tree anywhere is touched."""
    wt_head, error = rev_of(worktree, "HEAD")
    if error:
        return None, error
    target_head, error = rev_of(repo_root, target_rev)
    if error:
        return None, error

    dirty_tree, error = snapshot_dirty_tree(worktree)
    if error:
        return None, error

    code, out, err = _run(build_commit_tree_command(repo_root, dirty_tree, wt_head), MERGE_TIMEOUT_SECONDS)
    if code != 0:
        return None, fail_text(err, "could not record the worktree snapshot")
    dirty_commit = (out or "").strip()

    # Exit 1 means "merged, with conflicts" and is an ANSWER; only a worse code
    # is a failure. `--quiet` is not used: the conflict TYPES are the payload.
    code, out, err = _run(build_merge_tree_command(repo_root, wt_head, target_head, dirty_commit), MERGE_TIMEOUT_SECONDS)
    if code not in (0, 1):
        return None, fail_text(err, "could not merge the worktree's changes")
    merge = parse_merge_tree(out)
    if not merge["tree"]:
        return None, fail_text(err, "git produced no merged tree")

    code, out, err = _run(build_delta_command(repo_root, target_head, merge["tree"]), MERGE_TIMEOUT_SECONDS)
    if code != 0:
        return None, fail_text(err, "could not compare the merge with the target")
    delta = parse_delta(out)

    dirty, error = target_dirty_paths(repo_root)
    if error:
        return None, error

    plan = build_plan(delta, merge["conflicts"], dirty)
    plan.update(
        {
            # EVERY conflict, `modify/delete` included. `build_plan`'s `conflicts`
            # is the narrower "would write markers" set that gates adopt's force;
            # `catch_up` needs the wider one, because `stash pop` has no notion of
            # a safe conflict and stops on all of them.
            "conflict_paths": sorted(merge["conflicts"]),
            "merged_tree": merge["tree"],
            "snapshot_commit": dirty_commit,
            "snapshot_tree": dirty_tree,
            "worktree_head": wt_head,
            "target_head": target_head,
            "summary": summarize_plan(plan),
        }
    )
    return plan, None


def materialize(repo_root: Path, merged_tree: str, files: list[dict]) -> tuple[list[str], str | None]:
    """Write the plan into the target's WORKING TREE. `(written, error)`.

    The target's real index is deliberately never touched — adopted files show
    up as ordinary unstaged edits and untracked files, which is the state a
    human reviews before committing. Staging them would hide the change inside
    a diff nobody looks at.
    """
    writes = [row["path"] for row in files if row["verdict"] in ("new", "merge", "readd", "conflict")]
    removes = [row["path"] for row in files if row["verdict"] == "delete"]

    if writes:
        with tempfile.TemporaryDirectory(prefix="worktree-adopt-") as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            code, _out, err = _run(build_target_read_tree_command(repo_root, merged_tree), MERGE_TIMEOUT_SECONDS, env)
            if code != 0:
                return [], fail_text(err, "could not stage the merged tree")
            code, _out, err = _run(build_checkout_index_command(repo_root, writes), MERGE_TIMEOUT_SECONDS, env)
            if code != 0:
                return [], fail_text(err, "could not write the adopted files")

    for path in removes:
        try:
            (repo_root / path).unlink()
        except OSError:
            pass  # already gone is the outcome we wanted

    return sorted(writes + removes), None


def anchor(repo_root: Path, branch: str, commit: str) -> str | None:
    """Point `refs/tldrack/adopted/<branch>` at the snapshot, so the rescued
    state survives a later force-delete of the worktree AND of the branch. This
    is what makes "nothing left to preserve" a fact rather than a claim."""
    code, _out, err = _run(build_update_ref_command(repo_root, adopt_ref_for(branch), commit), REF_TIMEOUT_SECONDS)
    return None if code == 0 else fail_text(err, "could not anchor the snapshot")


def unpreserved(repo_root: Path, worktree: Path, anchored_tree: str) -> tuple[list[str], str | None]:
    """Which of the worktree's CURRENT changes are not in the anchored tree.

    Empty means a forced remove destroys nothing — the whole point of the
    safe-delete signal. Compared as TREES, so an edit that was undone back to
    the adopted content correctly reads as preserved.
    """
    current, error = snapshot_dirty_tree(worktree)
    if error:
        return [], error
    if current == anchored_tree:
        return [], None
    code, out, err = _run(
        ["git", "-C", str(repo_root), "diff", "--name-only", "-z", anchored_tree, current], MERGE_TIMEOUT_SECONDS
    )
    if code != 0:
        return [], fail_text(err, "could not compare against the anchored snapshot")
    return sorted({path for path in (out or "").split("\0") if path}), None


def count_between(path: Path, span: str) -> int | None:
    code, out, _err = _run(build_count_command(path, span), REF_TIMEOUT_SECONDS)
    if code != 0:
        return None
    try:
        return int((out or "").strip())
    except ValueError:
        return None
