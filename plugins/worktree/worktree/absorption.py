"""Has a branch's work reached the base by CONTENT, when nothing can PROVE it —
the harbor's evidence tier. It sits beside `landing.py`'s proof ladder and is
never mixed into it: a row this module calls absorbed is still not `landed`,
CLEANUP never takes it, and the only verb that acts on it (DELETE ABSORBED)
pins every tip under `archive.ARCHIVE_PREFIX` first, so acting on evidence
stays reversible.

── Why evidence at all ────────────────────────────────────────────────────────
Proof misses whole classes of finished work. Lanes that branch off a baseline
snapshot commit and land on main as squashed snapshots never match a patch-id.
Branches amended after their PR's head, or replayed under new SHAs by a port,
never match either. Without this tier every such branch reads unlanded for
ever, and the operator has to run this measurement by hand to see it.

── The measurement ────────────────────────────────────────────────────────────
For one branch, against the base tip (three forks, all reads):

1. `merge-base` → `mb`.
2. `git diff -U0 mb tip`: the branch's NET additions, i.e. the lines that are
   at its tip and were not at the merge-base.
3. `git diff -U0 tip base -- <those paths>`: a branch line the base LACKS shows
   up here as a `-` line on the same path. Each added line is matched against
   that multiset, so a line added twice and taken once counts once each way.
   Matching runs on stripped lines of at least `MIN_LINE_CHARS`, so braces and
   blank lines do not vote.
4. For paths the base does not have at all, `git log --diff-filter=D mb..base`
   tells "the base took it and later deleted it" (excluded: that work landed)
   from "the base never had it" (a MISSING file, which blocks the verdict).

`absorbed` = no missing files, at least one measured line, and at least
`ABSORBED_MIN_SHARE` of the lines present. Measured on one busy repository's branches:
every branch in main scored 84–100% with no missing file, and every branch not in
main scored 0–18% or was missing files. The threshold sits in that gap, and
the panel prints the number beside every verdict, so a person reads the
evidence, not just the bit.

Superseded work (a feature that landed in a different form under other file
names) is NOT absorbed by this measure, and that is correct: deciding it is
superseded takes a person.

── Cost ───────────────────────────────────────────────────────────────────────
The `ContentTier` shape: `judge` reads caches and never forks, and a worker
fills them under `ABSORPTION_BUDGET_SECONDS`, then calls `on_ready`. A result
is keyed by (base tip, branch tip). The branch diff, which is the expensive
fork, is keyed by (merge-base, tip), and a base that moves forward rarely
moves a merge-base, so a main commit costs one path-limited diff per branch.
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path
from collections.abc import Callable

from worktree.background_tier import BackgroundTier
from worktree.branches import is_full_oid
from worktree.listing import PROBE_TIMEOUT_SECONDS

#: The share of a branch's measured lines the base must hold. The module
#: header owns the measured gap this sits in.
ABSORBED_MIN_SHARE = 0.75

#: Lines shorter than this do not vote: `}`, `});`, `</div>`, blank lines.
MIN_LINE_CHARS = 8

#: Paths that are DATA, not work: generated bakes, lockfiles, fixtures, and
#: binary media. Their lines would drown a branch's code in either direction,
#: and a baked asset is content-hashed (`tile.1024.<hash>.webp`), so a re-bake
#: on the base renames every one, and thousands of "missing" files would bury a
#: branch whose code is nearly all in main. The code that references an asset
#: still votes.
SKIPPED_SUFFIXES = (
    ".json",
    ".jsonl",
    ".lock",
    ".svg",
    ".map",
    ".png",
    ".webp",
    ".jpg",
    ".jpeg",
    ".gif",
    ".avif",
    ".ktx2",
    ".glb",
    ".gltf",
    ".bin",
    ".wasm",
    ".spz",
    ".ply",
    ".splat",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".webm",
    ".pdf",
)

#: How many missing files the wire names; the count says the rest.
MISSING_FILES_MAX = 6

#: One worker pass, end to end. A pass that runs out leaves the rest
#: `pending`, and the next sweep schedules another.
ABSORPTION_BUDGET_SECONDS = 30.0

#: A failed pass backs off this long, as `landing.CONTENT_BACKOFF_SECONDS`.
ABSORPTION_BACKOFF_SECONDS = 60.0

#: A branch diff past this many characters is not measured. One snapshot
#: commit can carry the whole tree, and its number would mean nothing.
MAX_DIFF_CHARS = 24_000_000

#: Pathspecs per fork, which keeps argv far from the OS limit on a branch that
#: touches thousands of files.
PATHSPEC_CHUNK = 300

#: Per-fork budget: local git reads only.
GIT_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS
DIFF_TIMEOUT_SECONDS = 120

#: Bounds on the in-memory caches. A full cache is cleared, not evicted:
#: re-measuring costs background time, never correctness.
RESULTS_MAX = 512
BRANCH_DIFFS_MAX = 64

DIFF_OPTIONS = ("-U0", "--no-renames", "--no-ext-diff", "--no-color")


# ── argv builders (pure) ──────────────────────────────────────────────────


def _git(repo_root: Path | str) -> list[str]:
    # quotePath off: non-ASCII paths print raw, so only control characters and
    # quotes still arrive C-quoted (`unquote_path`).
    return ["git", "-C", str(repo_root), "-c", "core.quotePath=false"]


def build_merge_base_command(repo_root: Path | str, base_tip: str, tip: str) -> list[str]:
    return [*_git(repo_root), "merge-base", base_tip, tip]


def build_branch_diff_command(repo_root: Path | str, merge_base: str, tip: str) -> list[str]:
    return [*_git(repo_root), "diff", *DIFF_OPTIONS, merge_base, tip]


def build_base_diff_command(repo_root: Path | str, tip: str, base_tip: str, paths: list[str]) -> list[str]:
    """Literal pathspecs: a path is a NAME here, never a glob or `:(magic)`."""
    return [*_git(repo_root), "--literal-pathspecs", "diff", *DIFF_OPTIONS, tip, base_tip, "--", *paths]


def build_base_deleted_command(repo_root: Path | str, merge_base: str, base_tip: str, paths: list[str]) -> list[str]:
    return [
        *_git(repo_root),
        "--literal-pathspecs",
        "log",
        "--format=",
        "--name-only",
        "--no-renames",
        "--diff-filter=D",
        f"{merge_base}..{base_tip}",
        "--",
        *paths,
    ]


# ── parsers (pure, total) ─────────────────────────────────────────────────


def unquote_path(text: str) -> str:
    """git's C-quoted path (`"a\\tb"`, octal UTF-8 bytes) → the path. An
    unquoted path comes back as-is."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            digits = body[i + 1 : i + 4]
            if len(digits) == 3 and all(d in "01234567" for d in digits):
                out.append(int(digits, 8))
                i += 4
                continue
        out.extend(char.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _header_path(text: str) -> str | None:
    """`--- a/x` / `+++ b/x` → `x`; `/dev/null` → None."""
    path = unquote_path(text.rstrip("\t").split("\t")[0])
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_diff(text: object) -> dict[str, dict]:
    """`git diff -U0` → `{path: {added: Counter, removed: Counter, new, deleted}}`.

    A state machine, not a line regex: inside a hunk, a content line can begin
    `+++` or `---` (a Markdown rule, a `++i`), and only the hunk header tells
    the two apart. Lines are stripped. A binary file has no lines, only its
    `new` / `deleted` flags."""
    files: dict[str, dict] = {}
    if not isinstance(text, str):
        return files
    current: dict | None = None
    old_path: str | None = None
    in_hunk = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current, old_path, in_hunk = None, None, False
            continue
        if not in_hunk:
            if line.startswith("--- "):
                old_path = _header_path(line[4:])
            elif line.startswith("+++ "):
                new_path = _header_path(line[4:])
                path = new_path or old_path
                if path:
                    current = files.setdefault(path, {"added": Counter(), "removed": Counter(), "new": False, "deleted": False})
                    current["new"] = current["new"] or old_path is None
                    current["deleted"] = current["deleted"] or new_path is None
            elif line.startswith("Binary files ") and line.endswith(" differ"):
                # `Binary files /dev/null and b/x differ` — no `---`/`+++` pair.
                pair = line[len("Binary files ") : -len(" differ")].split(" and ")
                if len(pair) == 2:
                    old, new = _header_path(pair[0]), _header_path(pair[1])
                    path = new or old
                    if path:
                        entry = files.setdefault(path, {"added": Counter(), "removed": Counter(), "new": False, "deleted": False})
                        entry["new"] = entry["new"] or old is None
                        entry["deleted"] = entry["deleted"] or new is None
            elif line.startswith("@@") and current is not None:
                in_hunk = True
            continue
        if line.startswith("@@"):
            continue
        if current is None:
            continue
        if line.startswith("+"):
            current["added"][line[1:].strip()] += 1
        elif line.startswith("-"):
            current["removed"][line[1:].strip()] += 1
    return files


def parse_name_only(text: object) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {unquote_path(line) for line in text.split("\n") if line.strip()}


def is_skipped(path: str) -> bool:
    return path.lower().endswith(SKIPPED_SUFFIXES)


def measure(branch: dict[str, dict], base: dict[str, dict], base_deleted: set[str]) -> dict:
    """The verdict from the three parsed reads — pure, so the threshold and the
    file rule are tested without a repository."""
    present = absent = 0
    missing: list[str] = []
    for path, entry in branch.items():
        if is_skipped(path) or path in base_deleted:
            # Skipped is data; deleted-by-the-base is work the base TOOK and
            # later removed. Neither says anything about this branch.
            continue
        against = base.get(path)
        removed = Counter(against["removed"]) if against else Counter()
        for line, count in entry["added"].items():
            if len(line) < MIN_LINE_CHARS:
                continue
            lacking = min(count, removed[line])
            removed[line] -= lacking
            absent += lacking
            present += count - lacking
        if entry["new"] and against is not None and against["deleted"]:
            missing.append(path)
    lines = present + absent
    absorbed = not missing and lines > 0 and present >= ABSORBED_MIN_SHARE * lines
    return {
        "status": "measured",
        "lines": lines,
        "present": present,
        "missing_files": sorted(missing)[:MISSING_FILES_MAX],
        "missing_file_count": len(missing),
        "absorbed": absorbed,
    }


def _chunks(items: list[str]) -> list[list[str]]:
    return [items[i : i + PATHSPEC_CHUNK] for i in range(0, len(items), PATHSPEC_CHUNK)]


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="worktree-absorption").start()


class AbsorptionTier(BackgroundTier):
    """Evidence for every open row, behind caches only a worker writes — the
    `landing.ContentTier` contract, one tier further along.

    `judge` sets `absorption` on every row: None where it does not apply (the
    base, anything proven landed or contained, nothing ahead, or content proofs
    still pending, since a proof may yet land), `{"status": "pending"}` while the
    worker has not measured this (base tip, tip) pair, else the `measure` result
    (or `{"status": "too_big"}`).
    """

    backoff_seconds = ABSORPTION_BACKOFF_SECONDS

    def __init__(
        self, repo_root: Path | str, *, start_thread: Callable[[Callable[[], None]], None] = _start_daemon, **tier
    ) -> None:
        super().__init__(repo_root, start_thread=start_thread, **tier)
        self._results: dict[tuple[str, str], dict] = {}
        self._branch_diffs: dict[tuple[str, str], dict[str, dict] | None] = {}

    # -- the runner's half: read, decide, schedule ----------------------------
    def judge(self, rows: list[dict]) -> None:
        base_row = next((row for row in rows if row.get("is_base")), None)
        base_tip = (base_row or {}).get("oid")
        wanted: list[tuple[str, str]] = []
        with self._lock:
            for row in rows:
                tip = row.get("oid")
                if (
                    row.get("is_base")
                    or row.get("landed") is True
                    or row.get("contained_in")
                    or not isinstance(row.get("ahead"), int)
                    or row["ahead"] <= 0
                    or row.get("content") != "measured"
                    or not is_full_oid(base_tip)
                    or not is_full_oid(tip)
                ):
                    row["absorption"] = None
                    continue
                result = self._results.get((base_tip, tip))
                if result is None:
                    row["absorption"] = {"status": "pending"}
                    wanted.append((base_tip, tip))
                else:
                    row["absorption"] = dict(result)
        if wanted:
            self._schedule(wanted)

    # -- the worker's half: fill the caches ---------------------------------
    def _fill(self, want: list[tuple[str, str]], started: float) -> bool:
        """Measure each wanted pair until the budget runs out; the rest stay
        `pending` and the next sweep schedules another pass."""
        ok = True
        for base_tip, tip in want:
            if self._clock() - started >= ABSORPTION_BUDGET_SECONDS:
                break
            result = self._measure_one(base_tip, tip)
            if result is None:
                ok = False
                continue
            with self._lock:
                if len(self._results) >= RESULTS_MAX:
                    self._results.clear()
                self._results[(base_tip, tip)] = result
        return ok

    def _measure_one(self, base_tip: str, tip: str) -> dict | None:
        """One branch, or None when git could not answer (the pass backs off)."""
        code, out, err = self._run(build_merge_base_command(self._repo_root, base_tip, tip), GIT_TIMEOUT_SECONDS)
        merge_base = out.strip()
        if code != 0 or not is_full_oid(merge_base):
            # No merge-base is an unrelated history: an ANSWER with nothing to
            # compare, never a retry loop.
            if code == 1 and not err.strip():
                return {
                    "status": "measured",
                    "lines": 0,
                    "present": 0,
                    "missing_files": [],
                    "missing_file_count": 0,
                    "absorbed": False,
                }
            self.last_error = (err or "git merge-base failed").strip()[:300]
            return None

        key = (merge_base, tip)
        with self._lock:
            cached = self._branch_diffs.get(key, False)
        if cached is False:
            code, out, err = self._run(build_branch_diff_command(self._repo_root, merge_base, tip), DIFF_TIMEOUT_SECONDS)
            if code != 0:
                self.last_error = (err or "git diff failed").strip()[:300]
                return None
            cached = parse_diff(out) if len(out) <= MAX_DIFF_CHARS else None
            with self._lock:
                if len(self._branch_diffs) >= BRANCH_DIFFS_MAX:
                    self._branch_diffs.clear()
                self._branch_diffs[key] = cached
        if cached is None:
            return {"status": "too_big"}
        branch = cached

        paths = sorted(path for path in branch if not is_skipped(path))
        base: dict[str, dict] = {}
        for chunk in _chunks(paths):
            code, out, err = self._run(build_base_diff_command(self._repo_root, tip, base_tip, chunk), DIFF_TIMEOUT_SECONDS)
            if code != 0:
                self.last_error = (err or "git diff failed").strip()[:300]
                return None
            base.update(parse_diff(out))

        gone = sorted(path for path, entry in base.items() if entry["deleted"])
        base_deleted: set[str] = set()
        for chunk in _chunks(gone):
            code, out, err = self._run(
                build_base_deleted_command(self._repo_root, merge_base, base_tip, chunk), DIFF_TIMEOUT_SECONDS
            )
            if code != 0:
                self.last_error = (err or "git log failed").strip()[:300]
                return None
            base_deleted |= parse_name_only(out)
        return measure(branch, base, base_deleted)
