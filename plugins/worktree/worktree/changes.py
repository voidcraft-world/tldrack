"""What is UNCOMMITTED in a worktree, priced in two forks — the read behind the
harbor's CHANGES block and the forced remove it arms.

`git worktree remove` refuses a tree that holds modified or untracked files,
and that refusal is load-bearing (`create.build_worktree_remove_command` owns
why). Without this module the refusal is the end of the road in the rack: the
panel prints git's `fatal:` line and the only way to learn WHAT is at stake is
a terminal. This module is the other half of that sentence — the same question
`git status` answers, shaped for a surface that has to draw it.

── Two forks, never one per file ─────────────────────────────────────────────
`git status --porcelain=v1 -z -uall` is the file list and the only source of a
path: every path this module hands out came from git, which is what lets
`plugin.py` treat a requested path as a LOOKUP rather than an argument. `git
diff --numstat -z HEAD` prices the tracked half of that list in one more fork.
Untracked files are absent from `diff HEAD` by construction, so they are
counted by READING them (`count_untracked`) — a read is not a fork, and the
alternative (`--no-index` per file) is one fork per file for a number.

Both are `-z`: a path is arbitrary bytes, and the newline-separated forms of
both commands quote anything with a space in it. Parsing the quoted form is a
second escaping dialect to get wrong; `-z` has none.

── The `kind` is the whole point ─────────────────────────────────────────────
Two status columns and eleven meaningful pairs are a lookup table nobody wants
on the client, so the pair is collapsed HERE, once, into one word the view can
draw. Conflicts are collapsed first, because `UU` and `AA` mean "unmerged" no
matter which letter you read.

Pure above the fold: builders, parsers, the collapse table, the summary. The
I/O half at the foot only runs commands and reads files. Every subprocess goes
through `create_mod.run_bounded` BY MODULE ATTRIBUTE — the test fence in
`tests/` impersonates that attribute, and an imported name slips past it.
"""

from __future__ import annotations

import os
from pathlib import Path

from worktree import create as create_mod

#: Local git against a possibly cold worktree. `status -uall` walks the whole
#: tree; 20 s is generous for that and still bounded well under a request the
#: user is waiting on.
CHANGES_TIMEOUT_SECONDS = 20
#: One file's patch — a bounded read of blobs git already has.
PATCH_TIMEOUT_SECONDS = 15

#: Rows returned at most. A worktree with an untracked `dist/` in it can hold
#: thousands of paths, and a list that long is not a thing anyone reads — the
#: count still tells the truth (`total`), the list is what gets cut.
MAX_FILES = 500

#: An untracked file bigger than this is not line-counted: the number is worth
#: one read, not a 40 MB one. Its `bytes` still ships, so the row is never blank.
UNTRACKED_COUNT_MAX_BYTES = 512 * 1024

#: One patch's ceiling, whichever it hits first. A truncated patch says so.
PATCH_MAX_BYTES = 200_000
PATCH_MAX_LINES = 4000

#: git's own binary heuristic: a NUL byte in the head of the file.
BINARY_SNIFF_BYTES = 8000

#: XY pairs that mean "unmerged", collapsed before any other reading. Both
#: letters carry meaning in a conflict and neither is the answer on its own.
CONFLICT_PAIRS = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

#: Single-letter status → the word a view draws. Order matters at the call
#: site, not here (`kind_for` reads X then Y).
LETTER_KINDS = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
    "?": "untracked",
    "!": "ignored",
}


# ── builders (pure) ─────────────────────────────────────────────────────────


def build_status_command(path: Path | str) -> list[str]:
    """`git status --porcelain=v1 -z --untracked-files=all` in a worktree.

    `=v1` is pinned rather than left to `--porcelain`: v2 is a different
    document and a future git that changes the default must not silently
    change what this parses. `-uall` is what makes the list a FILE list — the
    default collapses an untracked directory to `dir/`, which is exactly the
    row a reader would click to find out what is inside it.
    """
    return ["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]


def build_numstat_command(path: Path | str) -> list[str]:
    """`git diff --numstat -z HEAD` — the tracked half's ± counts.

    Against HEAD rather than the index, so a staged change and an unstaged one
    are one number: the reader is about to destroy the working tree, and what
    they need is its distance from the last commit, not its distance from a
    staging area they never used.
    """
    return ["git", "-C", str(path), "diff", "--numstat", "-z", "HEAD"]


def build_patch_command(path: Path | str, file: str, *, untracked: bool) -> list[str]:
    """One file's unified diff. `--no-color` because this is data, not a TTY.

    An untracked file has no blob to diff against, so it is diffed against
    `/dev/null` with `--no-index` — which yields a real patch (git's own binary
    detection included) rather than a hand-rolled "every line is a +" that
    would lie about a binary file. `--no-index` exits 1 when there IS a
    difference, which is the expected case; the caller must not read that as
    failure.
    """
    if untracked:
        return ["git", "-C", str(path), "diff", "--no-color", "--no-index", "--", os.devnull, file]
    return ["git", "-C", str(path), "diff", "--no-color", "-U3", "HEAD", "--", file]


# ── parsers (pure) ──────────────────────────────────────────────────────────


def kind_for(index: str, worktree: str) -> str:
    """The XY pair as ONE word.

    Conflicts first (both letters are the state). Then X, then Y: a file that
    is staged-added and then modified reads as `added`, because what the
    reader is about to lose is a file that is not in any commit.
    """
    pair = f"{index}{worktree}"
    if pair in CONFLICT_PAIRS:
        return "conflicted"
    for letter in (index, worktree):
        kind = LETTER_KINDS.get(letter)
        if kind:
            return kind
    return "modified"


def parse_status(text: object) -> list[dict]:
    """`status --porcelain=v1 -z` → rows, in git's order.

    Wire shape: `XY<space><path>` per NUL-terminated field, and for a rename or
    a copy the ORIGINAL path follows as its own field. That trailing field is
    the reason this cannot be a comprehension: the record length is decided by
    the record's own first two bytes.
    """
    if not isinstance(text, str) or not text:
        return []
    fields = [field for field in text.split("\0") if field != ""]
    rows: list[dict] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4 or entry[2] != " ":
            continue  # not a status record — a git that changed shape, not our guess to make
        index, worktree, path = entry[0], entry[1], entry[3:]
        orig_path = None
        if index in ("R", "C") or worktree in ("R", "C"):
            if i < len(fields):
                orig_path = fields[i]
                i += 1
        rows.append(
            {
                "path": path,
                "orig_path": orig_path,
                "index": index,
                "worktree": worktree,
                "kind": kind_for(index, worktree),
                # `?` is not staged, it is unknown to the index — and a row that
                # claimed otherwise would arm the wrong verb in a future UI.
                "staged": index not in (" ", "?", "!"),
            }
        )
    return rows


def parse_numstat(text: object) -> dict[str, dict]:
    """`diff --numstat -z` → `{path: {added, removed, binary}}`.

    Records are `<added>\\t<removed>\\t<path>` NUL-terminated, EXCEPT a rename,
    where the path is empty and the two paths follow as their own fields
    (source then destination). `-\\t-` is git saying binary; that is carried as
    a flag rather than a zero, because "0 lines changed" and "not lines" are
    different facts and a view must be able to print them differently.
    """
    if not isinstance(text, str) or not text:
        return {}
    fields = [field for field in text.split("\0") if field != ""]
    out: dict[str, dict] = {}
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        parts = record.split("\t", 2)
        if len(parts) < 3:
            continue
        added_text, removed_text, path = parts
        if path == "":
            # Rename: source then destination. The destination is the path the
            # status list uses, so it is the one keyed here.
            if i + 1 >= len(fields):
                break
            path = fields[i + 1]
            i += 2
        binary = added_text == "-" or removed_text == "-"
        out[path] = {
            "added": None if binary else _int_or_none(added_text),
            "removed": None if binary else _int_or_none(removed_text),
            "binary": binary,
        }
    return out


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def truncate_patch(text: object) -> tuple[str, bool]:
    """A patch cut to `PATCH_MAX_LINES` / `PATCH_MAX_BYTES`, and whether it was.

    Lines first so the cut lands on a line boundary; the byte ceiling then
    catches a patch of few but enormous lines (a minified bundle).
    """
    if not isinstance(text, str):
        return "", False
    lines = text.split("\n")
    truncated = len(lines) > PATCH_MAX_LINES
    if truncated:
        lines = lines[:PATCH_MAX_LINES]
    out = "\n".join(lines)
    if len(out) > PATCH_MAX_BYTES:
        out = out[:PATCH_MAX_BYTES]
        truncated = True
    return out, truncated


def summarize(rows: list[dict]) -> dict:
    """The counts a caption prints. Kinds are counted by name, not by letter,
    so the summary and the rows can never disagree about what a row is."""
    kinds: dict[str, int] = {}
    added = removed = binary = 0
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        if row.get("binary"):
            binary += 1
            continue
        added += row.get("added") or 0
        removed += row.get("removed") or 0
    return {"files": len(rows), "added": added, "removed": removed, "binary": binary, "kinds": kinds}


def merge_rows(status_rows: list[dict], numstat: dict[str, dict]) -> list[dict]:
    """Status rows + their ± counts. Untracked rows are left uncounted here —
    the caller fills them from the filesystem, which is the only place that
    number lives (module header)."""
    merged = []
    for row in status_rows:
        entry = dict(row)
        counts = numstat.get(row["path"])
        entry["added"] = counts["added"] if counts else None
        entry["removed"] = counts["removed"] if counts else None
        entry["binary"] = bool(counts and counts["binary"])
        entry["bytes"] = None
        merged.append(entry)
    return merged


# ── I/O ─────────────────────────────────────────────────────────────────────


def count_untracked(root: Path, rel_path: str) -> dict:
    """An untracked file's size, and its line count when reading it is cheap.

    Returns the three keys `merge_rows` left null. A file that cannot be read
    (a dangling symlink, a permission, a race with the agent that made it) is
    reported as an unknown count rather than an error: the row exists because
    git said so, and one unreadable file must not blank the list.
    """
    result: dict = {"added": None, "removed": 0, "binary": False, "bytes": None}
    full = root / rel_path
    try:
        size = full.stat().st_size
    except OSError:
        return result
    result["bytes"] = size
    if size > UNTRACKED_COUNT_MAX_BYTES:
        return result
    try:
        blob = full.read_bytes()
    except OSError:
        return result
    if b"\0" in blob[:BINARY_SNIFF_BYTES]:
        result["binary"] = True
        return result
    if blob == b"":
        result["added"] = 0
        return result
    # A trailing newline terminates the last line rather than starting one —
    # the same count `git diff --numstat` reports for the same file.
    result["added"] = blob.count(b"\n") + (0 if blob.endswith(b"\n") else 1)
    return result


def collect(path: Path) -> tuple[list[dict], dict, str | None]:
    """Every uncommitted change in the worktree at `path`.

    → `(rows, summary, error)`. `error` non-null means git could not answer and
    the rows are empty — the caller turns that into a status code. The summary
    is computed over the FULL list before truncation, so `files` is the truth
    even when the rows are a prefix of it.
    """
    code, out, err = create_mod.run_bounded(build_status_command(path), CHANGES_TIMEOUT_SECONDS)
    if code != 0:
        return [], summarize([]), (err or "git status failed").strip()[:300]
    status_rows = parse_status(out)

    numstat: dict[str, dict] = {}
    if any(row["kind"] != "untracked" for row in status_rows):
        code, diff_out, _err = create_mod.run_bounded(build_numstat_command(path), CHANGES_TIMEOUT_SECONDS)
        if code == 0:
            numstat = parse_numstat(diff_out)

    rows = merge_rows(status_rows, numstat)
    for row in rows:
        if row["kind"] == "untracked":
            row.update(count_untracked(path, row["path"]))
    return rows, summarize(rows), None


def patch_for(path: Path, row: dict) -> tuple[str, bool, str | None]:
    """One row's unified diff → `(patch, truncated, error)`.

    `row` is a row `collect` produced, never a caller's dict: the path reaching
    git is git's own (`plugin.py` looks the row up rather than trusting an
    argument), and the untracked flag decides which of two commands runs.
    """
    untracked = row["kind"] == "untracked"
    code, out, err = create_mod.run_bounded(build_patch_command(path, row["path"], untracked=untracked), PATCH_TIMEOUT_SECONDS)
    # `--no-index` exits 1 on a difference, which is the whole point of asking;
    # a plain `git diff` says 0 either way, so a 1 there is only ever trusted
    # when it came with a patch attached.
    ok = code == 0 or (code == 1 and (untracked or bool(out)))
    if not ok:
        return "", False, (err or "git diff failed").strip()[:300]
    patch, truncated = truncate_patch(out)
    return patch, truncated, None
