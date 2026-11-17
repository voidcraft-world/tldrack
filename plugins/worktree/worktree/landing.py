"""Has a branch's work reached main — proven from CONTENT, not only from `gh` —
and, when it has not, exactly what deleting it would lose.

`branches.landed` answers two ways: the tip is an ancestor of the base, or a
merged PR shipped this very tip. Both are right and both can be starved:
the PR index once only refreshed when the
harbor's own FETCH was pressed, every `git pull` from a terminal left it alone,
and for a month most "unlanded" branches were squash-merged PRs. Refreshing
the index fixes most of that; this module makes the answer stop depending on
it, and gives the rows that are left a reason a person can act on.

── The proof ladder, after `branches.landed` ──────────────────────────────────
Each rung is a PROOF; a row that passes none of them is not landed, and every
unknown — an unmeasured row, a missing object, an unparseable stamp — fails the
rung rather than passing it.

1. **shipped inside another PR** (`how: "pr"`) — the tip is reachable from the
   head of ANY merged PR into the base, committed no later than that merge. The
   stacked-branch case: `a` was built on, `b`'s PR took both and was squashed.
   `branches.landed` only asks the PR that carries the branch's own NAME.
2. **squash** — the branch's whole diff, merge-base → tip, is patch-identical
   (`git patch-id --stable`) to one commit on the base since that merge-base.
   That commit is the squash; it stays a proof however much the base edits the
   same lines afterwards, which is exactly where a three-way "would merging
   change anything?" test goes blind.
3. **commits** — every commit on the branch is patch-identical to a commit on
   the base, or to one inside a merged PR's head range (rebased, then landed).

── `contained_in` is not landed, and says so ─────────────────────────────────
A branch whose tip is an ancestor of another NOT-landed branch's tip loses
nothing when deleted: its commits stay on the other one. That is not "in main",
so it is its own field, and the rule that makes it safe is that only MAXIMAL
branches keep — equal tips tie-break on `containment_key`, so a chain or a tie
can never mark every copy deletable at once.

── Hints are not proofs ───────────────────────────────────────────────────────
The ledger's `hint` names a PR that PROBABLY carried a commit — the base has a
squash titled `<subject> (#N)`. Titles are text a person typed twice; they are
shown to the person deciding, and never read by a verdict.

── Patch-ids, not `commit-tree` ───────────────────────────────────────────────
The well-known squash detector (`git commit-tree <tip>^{tree} -p <merge-base>`
then `git cherry`) WRITES a loose object per branch per sweep. Everything here
is a read: `git diff | git patch-id` for the branch, `git log -p | git patch-id`
for the base. Measured: the same 49/65 squash hits `git cherry` finds.

── Cost ───────────────────────────────────────────────────────────────────────
The cold build is the expensive part — 109 base commits of `log -p` took 8 s,
271 took 56 s on a loaded machine — so it is a background TIER (`ContentTier`)
in the dirty tier's shape: the harbor runner only ever READS its caches, a
worker fills them under a budget, and an unmeasured row says `pending`. A
commit's patch-id never changes, so it is persisted
(`worktree-patch-ids.json`) and paid once per commit, ever.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from collections.abc import Callable

from worktree import create as create_mod
from worktree.background_tier import BackgroundTier
from worktree.branches import CREATING_REASON, FIELD_SEP, is_full_oid, parse_iso8601, shipped_by_pr
from worktree.listing import PROBE_TIMEOUT_SECONDS
from worktree.row_store import load_store_file, persist_atomically
from worktree.store import PATCH_ID_FILE_NAME

#: Local git only — the budget every read here gets, per fork.
GIT_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS

#: `log -p` over many commits is the one fork that can take real time on a
#: cold disk (8 s for 109 commits measured), so it gets its own budget and is
#: fed in chunks of `PATCH_CHUNK` commits.
PATCH_LOG_TIMEOUT_SECONDS = 120
PATCH_CHUNK = 60

#: One worker pass, end to end. A pass that runs out leaves the rest
#: `pending` and the next sweep schedules another — progress persists.
CONTENT_BUDGET_SECONDS = 90.0

#: A failed pass backs off this long, the same posture as `HarborCache`.
CONTENT_BACKOFF_SECONDS = 60.0

#: The ledger's length on the wire. A branch with 40 commits main lacks does
#: not need 40 rows to say "this is real work"; the count says the rest.
LEDGER_MAX_COMMITS = 6

#: Command names `lsof` reports, per worktree, on the wire.
OCCUPANT_NAMES_MAX = 3

#: How long `await_vacated` lets just-killed sessions' processes leave a tree,
#: and how often it looks. `tmux kill-session` returns once the panes are
#: signalled, not once their processes have exited.
OCCUPANT_SETTLE_SECONDS = 3.0
OCCUPANT_POLL_SECONDS = 0.25

#: Claude Code installs its binary as `~/.local/share/claude/versions/<semver>`
#: and `~/.local/bin/claude` symlinks to it, so `lsof` names every agent
#: `2.1.289`. A bare version as a command name is read as the agent it is.
VERSION_NAMED_BINARY = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_NAMED_AS = "claude"

#: The persisted cache's bounds. A full section is cleared, not evicted —
#: re-measuring is a background cost, never a correctness one.
STORE_FILE_VERSION = 1
STORE_MAX_COMMITS = 20000
STORE_MAX_DIFFS = 2000

#: `Some title (#71)` — GitHub's squash-merge subject. Anchored at the end so
#: a subject that merely mentions a PR is not read as one.
SQUASH_TITLE = re.compile(r"^(?P<title>.+) \(#(?P<number>\d+)\)$")

#: The log record format: oid, short sha, parents, committer time, subject.
LOG_FORMAT = FIELD_SEP.join(("%H", "%h", "%P", "%ct", "%s"))

#: The diff options BOTH sides of every patch-id comparison use. They must
#: match exactly, or two identical changes hash differently: no rename
#: detection (a user's `diff.renames` would otherwise apply to one side and
#: not the other), no external diff driver, no colour.
DIFF_OPTIONS = ("--no-renames", "--no-ext-diff", "--no-color")

LANDING_HOWS = ("ancestor", "pr", "squash", "commits")

#: Recorded for a commit or diff git would not hash (an empty patch, a
#: binary-only one). An ANSWER — so it is never asked again — that matches
#: nothing: two unhashable changes are not thereby the same change.
UNHASHABLE = "0" * 40

OUTSIDE_NOT_LANDED_REASON = "not in main — its worktree is kept"


# ── argv builders (pure) ──────────────────────────────────────────────────


def build_merge_base_command(repo_root: Path | str, base: str, tip: str) -> list[str]:
    return ["git", "-C", str(repo_root), "merge-base", base, tip]


def build_log_records_command(repo_root: Path | str, include: list[str], exclude: list[str], *, no_merges: bool) -> list[str]:
    """`git log` records for `include --not exclude`. Revisions are full
    object ids or the base's name — never anything a caller typed."""
    command = ["git", "-C", str(repo_root), "log", f"--format={LOG_FORMAT}"]
    if no_merges:
        command.append("--no-merges")
    return command + list(include) + ["--not"] + list(exclude) + ["--"]


def build_patch_log_command(repo_root: Path | str) -> list[str]:
    """The patches of the commits named on STDIN, one per line, in the order
    given — `--stdin` keeps a 60-sha chunk off the argv."""
    return [
        "git",
        "-C",
        str(repo_root),
        "log",
        "-p",
        *DIFF_OPTIONS,
        "--no-merges",
        "--no-walk=unsorted",
        "--stdin",
        "--format=commit %H",
    ]


def build_diff_command(repo_root: Path | str, merge_base: str, tip: str) -> list[str]:
    return ["git", "-C", str(repo_root), "diff", *DIFF_OPTIONS, merge_base, tip, "--"]


def build_patch_id_command(repo_root: Path | str) -> list[str]:
    return ["git", "-C", str(repo_root), "patch-id", "--stable"]


def build_contains_command(repo_root: Path | str, oid: str) -> list[str]:
    """Every local branch whose tip has `oid` in its history — itself included."""
    return ["git", "-C", str(repo_root), "for-each-ref", f"--contains={oid}", "--format=%(refname:short)", "refs/heads/"]


def build_batch_check_command(repo_root: Path | str) -> list[str]:
    """`<oid> missing` for every id on STDIN this repo does not have — one
    fork for the whole PR index rather than one `cat-file -e` per head."""
    return ["git", "-C", str(repo_root), "cat-file", "--batch-check"]


def build_fetch_heads_command(repo_root: Path | str, remote: str, oids: list[str]) -> list[str]:
    """Fetch PR heads by object id. `--no-write-fetch-head` is load-bearing:
    the plugin reads `FETCH_HEAD`'s mtime as "a terminal fetched — refresh the
    PR index", and this fetch must not read as one, or every refresh would
    schedule the next. GitHub serves any id reachable from its refs, a PR's
    `refs/pull/N/head` included."""
    return ["git", "-C", str(repo_root), "fetch", "--no-write-fetch-head", "--no-tags", remote, *oids]


def build_lsof_command() -> list[str]:
    """Every process's working directory: `p<pid>`, `c<command>`, `f…`,
    `n<path>` lines. `-w` silences the warnings an unreadable process
    prints; `-n -P` skips the name lookups."""
    return ["lsof", "-n", "-P", "-w", "-d", "cwd", "-Fcn"]


# ── parsers (pure, total) ─────────────────────────────────────────────────


def parse_patch_ids(text: object) -> dict[str, str]:
    """`git patch-id` output → `{commit oid: patch-id}`. A line that is not
    two full ids is skipped, never guessed at."""
    if not isinstance(text, str):
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and is_full_oid(parts[0]) and is_full_oid(parts[1]):
            found[parts[1]] = parts[0]
    return found


def parse_single_patch_id(text: object) -> str | None:
    """The patch-id of ONE diff fed without a commit header (git prints a zero
    id beside it). None for an empty diff — nothing to compare."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and is_full_oid(parts[0]):
            return parts[0]
    return None


def parse_log_records(text: object) -> list[dict]:
    """`LOG_FORMAT` lines → `{oid, sha, parents, committed_at, subject}`."""
    if not isinstance(text, str):
        return []
    records: list[dict] = []
    for line in text.splitlines():
        parts = line.split(FIELD_SEP)
        if len(parts) != 5 or not is_full_oid(parts[0]):
            continue
        oid, sha, parents, committed, subject = parts
        records.append(
            {
                "oid": oid,
                "sha": sha,
                "parents": [p for p in parents.split() if is_full_oid(p)],
                "committed_at": int(committed) if committed.isdigit() else 0,
                "subject": subject,
            }
        )
    return records


def parse_missing_objects(text: object) -> set[str]:
    """`git cat-file --batch-check` → the ids it answered `missing` for."""
    if not isinstance(text, str):
        return set()
    return {
        parts[0]
        for parts in (line.split() for line in text.splitlines())
        if len(parts) == 2 and parts[1] == "missing" and is_full_oid(parts[0])
    }


def parse_lsof_cwds(text: object) -> list[tuple[str, str]]:
    """`lsof -Fcn` → `[(command, cwd)]`. The command line precedes its
    process's file lines; a path with no command before it is skipped."""
    if not isinstance(text, str):
        return []
    found: list[tuple[str, str]] = []
    command: str | None = None
    for line in text.splitlines():
        if line.startswith("p"):
            command = None
        elif line.startswith("c"):
            command = line[1:]
            if VERSION_NAMED_BINARY.match(command):
                command = VERSION_NAMED_AS
        elif line.startswith("n") and command:
            found.append((command, line[1:]))
    return found


def occupants_by_path(paths: list[str], cwds: list[tuple[str, str]]) -> dict[str, list[str]]:
    """`{worktree path: [command names]}` — who is standing inside each tree.

    A segment-boundary match (`/a/b` holds `/a/b` and `/a/b/c`, never
    `/a/bc`), and the LONGEST worktree wins, so a process in a tree nested
    under another counts for the inner one. Both sides must already be
    resolved — `/tmp` is `/private/tmp` on macOS, and `lsof` prints the
    second. Names are unique and capped at `OCCUPANT_NAMES_MAX`.
    """
    ordered = sorted({p.rstrip("/") for p in paths if p}, key=len, reverse=True)
    result: dict[str, list[str]] = {path: [] for path in paths if path}
    for command, cwd in cwds:
        for root in ordered:
            if cwd == root or cwd.startswith(root + "/"):
                names = result.setdefault(root, [])
                if command not in names and len(names) < OCCUPANT_NAMES_MAX:
                    names.append(command)
                break
    return result


def squash_titles(records: list[dict]) -> dict[str, int]:
    """`{title: PR number}` from the base's `Title (#N)` squash subjects."""
    titles: dict[str, int] = {}
    for record in records:
        match = SQUASH_TITLE.match(record.get("subject") or "")
        if match:
            titles.setdefault(match.group("title"), int(match.group("number")))
    return titles


# ── PRs that reached the base (pure) ──────────────────────────────────────


def merged_heads(prs: dict[str, dict], base: str) -> dict[str, dict]:
    """`{head oid: pr row}` for every PR merged INTO THE BASE with a full head
    id and a readable merge stamp. A PR merged into another branch (a stacked
    PR merged into its parent) is not in the base and is not here."""
    heads: dict[str, dict] = {}
    for pr in prs.values():
        if pr.get("state") != "merged" or pr.get("base_ref") != base:
            continue
        head = pr.get("head_oid")
        if is_full_oid(head) and parse_iso8601(pr.get("merged_at")) is not None:
            heads[head] = pr
    return heads


def pr_reach(records: list[dict], heads: dict[str, dict]) -> dict[str, list[int]]:
    """`{commit oid: [PR numbers]}` — every commit reachable from a merged
    head inside `records` (the heads' history minus the base). A walk over
    parent links in memory; merges are walked through, so a PR that merged
    main into itself still reaches its own commits."""
    by_oid = {record["oid"]: record for record in records}
    reach: dict[str, list[int]] = {}
    for head, pr in heads.items():
        number = pr.get("number")
        if not isinstance(number, int) or head not in by_oid:
            continue
        stack, seen = [head], set()
        while stack:
            oid = stack.pop()
            if oid in seen or oid not in by_oid:
                continue
            seen.add(oid)
            reach.setdefault(oid, [])
            if number not in reach[oid]:
                reach[oid].append(number)
            stack.extend(by_oid[oid]["parents"])
    return reach


def shipped_by(row: dict, reach: dict[str, list[int]], prs_by_number: dict[int, dict]) -> int | None:
    """The merged PR whose head reaches this tip, committed no later than the
    merge (`branches.landed` owns why the clock test stays). The earliest
    such merge wins; None when there is none."""
    tip, committed = row.get("oid"), row.get("committed_at")
    if not is_full_oid(tip) or isinstance(committed, bool) or not isinstance(committed, (int, float)) or committed <= 0:
        return None
    best: tuple[float, int] | None = None
    for number in reach.get(tip, ()):
        merged_at = parse_iso8601((prs_by_number.get(number) or {}).get("merged_at"))
        if merged_at is None or committed > merged_at:
            continue
        if best is None or merged_at < best[0]:
            best = (merged_at, number)
    return best[1] if best else None


# ── the content verdict (pure) ────────────────────────────────────────────


def decide_content(
    *,
    commits: list[dict],
    commit_pids: dict[str, str],
    cumulative_pid: str | None,
    cumulative_empty: bool,
    base_pids: set[str],
    pr_pids: dict[str, int],
    titles: dict[str, int],
) -> tuple[dict | None, dict]:
    """`(landing | None, ledger)` for one measured row.

    `commits` are the branch's non-merge commits main lacks, newest first.
    A commit is FOUND when its patch-id is on the base or inside a merged
    PR's range (`pr_pids`). The ledger is always built — the caller ships it
    only for a row nothing proved. An unmeasured commit (no patch-id) is
    never found: unknown is missing.
    """
    entries: list[dict] = []
    found_prs: list[int | None] = []
    for commit in commits:
        pid = commit_pids.get(commit["oid"])
        if pid == UNHASHABLE:
            pid = None
        on_base = pid is not None and pid in base_pids
        via_pr = pr_pids.get(pid) if pid is not None and not on_base else None
        found = on_base or via_pr is not None
        hint = None
        if via_pr is not None:
            hint = {"pr": via_pr, "why": "pr_patch"}
        elif not found and commit["subject"] in titles:
            hint = {"pr": titles[commit["subject"]], "why": "title"}
        if found:
            found_prs.append(via_pr)
        entries.append({"sha": commit["sha"], "subject": commit["subject"], "found": found, "hint": hint})
    missing = sum(1 for entry in entries if not entry["found"])
    ordered = [e for e in entries if not e["found"]] + [e for e in entries if e["found"]]
    ledger = {"found": len(entries) - missing, "missing": missing, "commits": ordered[:LEDGER_MAX_COMMITS]}

    if cumulative_pid not in (None, UNHASHABLE) and cumulative_pid in base_pids:
        return {"how": "squash", "pr": None}, ledger
    if cumulative_empty:
        # merge-base → tip changes nothing: there is no work on it to lose.
        return {"how": "commits", "pr": None}, ledger
    if entries and missing == 0:
        # A PR is named only when it carried EVERY commit — naming one that
        # carried half would read as the whole story.
        prs = set(found_prs)
        return {"how": "commits", "pr": prs.pop() if len(prs) == 1 and None not in prs else None}, ledger
    return None, ledger


def attach_squash_pr(
    landing: dict, cumulative_pid: str | None, base_pid_owner: dict[str, str], base_subjects: dict[str, str]
) -> dict:
    """Name the PR a `squash` proof matched, from the matched base commit's
    own `(#N)` subject — decoration on a proof already made."""
    if landing.get("how") != "squash" or cumulative_pid is None:
        return landing
    oid = base_pid_owner.get(cumulative_pid)
    match = SQUASH_TITLE.match(base_subjects.get(oid or "", ""))
    return {**landing, "pr": int(match.group("number"))} if match else landing


# ── containment (pure) ────────────────────────────────────────────────────


def containment_key(row: dict) -> tuple:
    """Which of two branches at the SAME tip keeps: the one with a worktree,
    then the newer commit, then the name. Total, so a tie always resolves."""
    return (0 if row.get("has_worktree") else 1, -(row.get("committed_at") or 0), row.get("branch") or "")


def containment(candidates: list[dict], holders: dict[str, set[str]]) -> dict[str, str]:
    """`{branch: the kept branch that holds all of it}`.

    `candidates` are the not-landed, non-base rows; `holders[b]` is every
    local branch whose history contains `b`'s tip (`for-each-ref
    --contains`), itself included. A branch is contained when a candidate
    other than itself holds it AND either has a different tip (strictly
    more) or the same tip and a smaller `containment_key`. The named
    container is always one that is NOT contained itself — one exists by
    transitivity, so deleting every contained branch can never take the
    last copy of a commit.
    """
    by_name = {row["branch"]: row for row in candidates}

    def beats(holder: dict, row: dict) -> bool:
        if holder["oid"] != row["oid"]:
            return True
        return containment_key(holder) < containment_key(row)

    held_by: dict[str, list[dict]] = {}
    for row in candidates:
        names = holders.get(row["branch"]) or set()
        held_by[row["branch"]] = [
            by_name[name] for name in sorted(names) if name != row["branch"] and name in by_name and beats(by_name[name], row)
        ]
    contained = {name for name, holders_of in held_by.items() if holders_of}
    result: dict[str, str] = {}
    for name in sorted(contained):
        keepers = [holder for holder in held_by[name] if holder["branch"] not in contained]
        if keepers:
            result[name] = min(keepers, key=containment_key)["branch"]
    return result


# ── the outside-worktree refusal table (pure) ──────────────────────────────


def decide_outside_remove(
    *,
    name_error: str | None,
    held_path: str | None,
    is_main: bool,
    managed: bool,
    creating: bool,
    locked: bool,
    landed: bool,
    contained: bool,
    dirty: bool | None,
    occupants: list[str] | None,
) -> tuple[int, dict] | None:
    """The refusal table for `POST /remove-landed-worktree`. None = clear.

    The managed-worktree route (`POST /remove`) is unchanged and keeps its own
    gate; this is the narrower door, opened on 2026-10-08, for trees the
    plugin did not create (a build script's under `/tmp`, say):
    ONLY a tree whose branch is proven in main or contained, with nothing
    uncommitted and nobody standing in it. There is no force row — the table
    has nothing for a flag to unlock, so the parameter does not exist.

    Unknown is a refusal everywhere: `dirty` None means not probed, and
    `occupants` None means `lsof` could not answer.
    """
    if name_error is not None:
        return 400, {"error": name_error}
    if not held_path:
        return 404, {"error": "no worktree holds that branch"}
    if is_main:
        return 409, {"error": "the main checkout is never removed from here"}
    if managed:
        return 409, {"error": "a managed worktree — use REMOVE WORKTREE"}
    if creating:
        return 409, {"error": CREATING_REASON}
    if locked:
        return 409, {"error": "the worktree is locked — `git worktree unlock` it first"}
    if not (landed or contained):
        return 409, {"error": OUTSIDE_NOT_LANDED_REASON}
    if dirty is None:
        return 409, {"error": "could not read the worktree's status — refusing"}
    if dirty:
        return 409, {"error": "the worktree has uncommitted changes"}
    if occupants is None:
        return 409, {"error": "could not tell whether anything is running in it — refusing"}
    if occupants:
        return 409, {"error": f"in use — {', '.join(occupants)} is running in it"}
    return None


# ── the persisted patch-id cache ──────────────────────────────────────────


def resolve_patch_id_path(sibling_of: Path) -> Path:
    """`TLDRACK_PATCH_ID_PATH`, else beside the PR index — derived,
    not defaulted (`store.resolve_pr_index_path` owns why)."""
    env_path = os.environ.get("TLDRACK_PATCH_ID_PATH")
    return Path(env_path) if env_path else sibling_of.parent / PATCH_ID_FILE_NAME


def parse_patch_id_file(text: str) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != STORE_FILE_VERSION:
        raise ValueError("not a patch-id cache")
    commits = {k: v for k, v in (data.get("commits") or {}).items() if is_full_oid(k) and is_full_oid(v)}
    diffs = {
        k: v
        for k, v in (data.get("diffs") or {}).items()
        if isinstance(k, str) and isinstance(v, str) and (v == "" or is_full_oid(v))
    }
    return commits, diffs


class PatchIdStore:
    """`{commit oid: patch-id}` and `{"<merge-base>:<tip>": patch-id | ""}`
    (an empty string records an EMPTY diff, which is an answer, not a miss).
    Both keys name immutable things, so nothing here is ever invalidated."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._commits, self._diffs = load_store_file(path, parse_patch_id_file, ({}, {}))

    def commits(self) -> dict[str, str]:
        with self._lock:
            return dict(self._commits)

    def diff(self, merge_base: str, tip: str) -> str | None:
        with self._lock:
            return self._diffs.get(f"{merge_base}:{tip}")

    def add(self, *, commits: dict[str, str] | None = None, diffs: dict[str, str] | None = None) -> None:
        with self._lock:
            if commits:
                if len(self._commits) + len(commits) > STORE_MAX_COMMITS:
                    self._commits.clear()
                self._commits.update(commits)
            if diffs:
                if len(self._diffs) + len(diffs) > STORE_MAX_DIFFS:
                    self._diffs.clear()
                self._diffs.update(diffs)
            persist_atomically(
                self._path,
                json.dumps({"version": STORE_FILE_VERSION, "commits": self._commits, "diffs": self._diffs}),
                kept="patch-id cache",
            )


# ── the tier: the runner reads, a worker fills ────────────────────────────

Runner = Callable[..., tuple[int, str, str]]


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="worktree-content").start()


class ContentTier(BackgroundTier):
    """The harbor's content proofs, behind caches only a worker writes.

    `judge(rows, …)` runs on the harbor runner's thread and touches NO
    subprocess: it reads what the caches hold, decides every row it can,
    marks the rest `content: "pending"`, and — when anything was missing —
    schedules ONE worker pass (single flight). The pass fills the caches
    under `CONTENT_BUDGET_SECONDS` and calls `on_ready`, which invalidates the
    harbor so the next poll re-judges with the new answers.

    What it caches, keyed by what makes each answer immutable:

    * merge-bases and each branch's commits — by (base tip, branch tip);
    * the base's records since the oldest merge-base, and every merged PR
      head's history minus the base — by (base tip, that set);
    * patch-ids — persisted (`PatchIdStore`), per commit and per diff.
    """

    backoff_seconds = CONTENT_BACKOFF_SECONDS

    def __init__(
        self,
        repo_root: Path | str,
        store: PatchIdStore,
        *,
        start_thread: Callable[[Callable[[], None]], None] = _start_daemon,
        **tier,
    ) -> None:
        super().__init__(repo_root, start_thread=start_thread, **tier)
        self._store = store
        self._merge_bases: dict[tuple[str, str], str] = {}
        self._branch_commits: dict[tuple[str, str], list[dict]] = {}
        self._base_records: tuple[tuple[str, str], list[dict]] | None = None
        self._pr_records: tuple[tuple[str, frozenset], list[dict]] | None = None

    # -- the runner's half: read, decide, schedule ----------------------------
    def judge(self, rows: list[dict], *, base: str, prs: dict[str, dict]) -> None:
        """Decide `landing`, `content` and `ledger` on every row, in place.

        Rows `branches.join_landing` already proved keep its answer
        (`how` ancestor/pr) and read `content: "skipped"`; so do the base and
        every row with nothing main lacks."""
        base_row = next((row for row in rows if row.get("is_base")), None)
        base_tip = (base_row or {}).get("oid")
        heads = merged_heads(prs, base)
        prs_by_number = {pr["number"]: pr for pr in heads.values()}
        open_rows: list[dict] = []
        for row in rows:
            row["ledger"] = None
            if row.get("landed") is True:
                ancestor = row.get("merged") is True
                # An ancestor names the PR only when that PR shipped THIS tip
                # (exact head, so no fork here): `work_landed` owns why.
                shipped = not ancestor or shipped_by_pr(row, None, base)
                row["landing"] = {
                    "how": "ancestor" if ancestor else "pr",
                    "pr": (row.get("pr") or {}).get("number") if shipped else None,
                }
                row["content"] = "skipped"
            elif row.get("is_base") or not isinstance(row.get("ahead"), int) or row["ahead"] <= 0:
                row["landing"] = None
                row["content"] = "skipped"
            else:
                row["landing"] = None
                row["content"] = "pending"
                open_rows.append(row)
        if not open_rows or not is_full_oid(base_tip):
            return

        with self._lock:
            merge_bases = {key[1]: mb for key, mb in self._merge_bases.items() if key[0] == base_tip}
            branch_commits = {key[1]: list(c) for key, c in self._branch_commits.items() if key[0] == base_tip}
            base_records = self._base_records[1] if self._base_records and self._base_records[0][0] == base_tip else None
            base_since = self._base_records[0][1] if base_records is not None else None
            pr_key = (base_tip, frozenset(heads))
            pr_records = self._pr_records[1] if self._pr_records and self._pr_records[0] == pr_key else None
        pids = self._store.commits()

        reach = pr_reach(pr_records or [], heads)
        pr_pids: dict[str, int] = {}
        pr_ready = pr_records is not None and all(record["oid"] in pids for record in pr_records if len(record["parents"]) <= 1)
        for oid, numbers in reach.items():
            pid = pids.get(oid)
            if pid is not None and pid != UNHASHABLE:
                pr_pids.setdefault(pid, min(numbers))

        oldest = self._oldest_needed(open_rows, merge_bases)
        base_ready = base_records is not None and oldest is not None and base_since == oldest
        base_pids: set[str] = set()
        base_pid_owner: dict[str, str] = {}
        base_subjects: dict[str, str] = {}
        if base_ready:
            for record in base_records:
                pid = pids.get(record["oid"])
                base_subjects[record["oid"]] = record["subject"]
                if pid is not None and pid != UNHASHABLE:
                    base_pids.add(pid)
                    base_pid_owner.setdefault(pid, record["oid"])
            if any(record["oid"] not in pids for record in base_records if len(record["parents"]) <= 1):
                base_ready = False
        titles = squash_titles(base_records or [])

        missing = False
        for row in open_rows:
            # Rung 1 needs no patch-ids: only the PR heads' history.
            if pr_records is not None:
                number = shipped_by(row, reach, prs_by_number)
                if number is not None:
                    row.update({"landed": True, "landing": {"how": "pr", "pr": number}, "content": "measured"})
                    continue
            tip = row.get("oid")
            mb = merge_bases.get(tip)
            commits = branch_commits.get(tip)
            # `mb == ""`: git found no merge-base (an unrelated history). The
            # squash rung has nothing to diff against; the ledger still holds.
            cumulative = self._store.diff(mb, tip) if mb else (UNHASHABLE if mb == "" else None)
            if (
                not pr_ready
                or not base_ready
                or mb is None
                or commits is None
                or cumulative is None
                or any(c["oid"] not in pids for c in commits)
            ):
                missing = True
                continue
            landing, ledger = decide_content(
                commits=commits,
                commit_pids=pids,
                cumulative_pid=cumulative or None,
                cumulative_empty=cumulative == "",
                base_pids=base_pids,
                pr_pids=pr_pids,
                titles=titles,
            )
            row["content"] = "measured"
            if landing is not None:
                row.update(
                    {"landed": True, "landing": attach_squash_pr(landing, cumulative or None, base_pid_owner, base_subjects)}
                )
            else:
                row["ledger"] = ledger

        if missing:
            self._schedule(
                {
                    "base": base,
                    "base_tip": base_tip,
                    "heads": sorted(heads),
                    "rows": [(row["oid"], row.get("behind")) for row in open_rows if row.get("content") == "pending"],
                }
            )

    @staticmethod
    def _oldest_needed(rows: list[dict], merge_bases: dict[str, str]) -> str | None:
        """The merge-base furthest behind the base among the open rows — the
        base's records since it cover every other row's range too (the base
        is a squash-merge line, so its merge-bases are totally ordered)."""
        best: tuple[int, str] | None = None
        for row in rows:
            mb = merge_bases.get(row.get("oid"))
            behind = row.get("behind")
            if mb is None or not isinstance(behind, int):
                continue
            if best is None or behind > best[0]:
                best = (behind, mb)
        return best[1] if best else None

    # -- the worker's half: fill the caches ---------------------------------
    def _over(self, started: float) -> bool:
        return self._clock() - started >= CONTENT_BUDGET_SECONDS

    def _fill(self, want: dict, started: float) -> bool:
        """One pass, cheapest first. False = git failed (the tier backs off)."""
        base_tip = want["base_tip"]
        repo = self._repo_root

        for tip, _behind in want["rows"]:
            if (base_tip, tip) in self._merge_bases:
                continue
            code, out, _err = self._run(build_merge_base_command(repo, base_tip, tip), GIT_TIMEOUT_SECONDS)
            # Exit 1 with no output is git's "no common ancestor" — an answer,
            # recorded as "" so one orphan branch cannot stall every other row.
            if code == 1 and not out.strip():
                merge_base = ""
            elif code == 0 and is_full_oid(out.strip()):
                merge_base = out.strip()
            else:
                self.last_error = f"git merge-base failed for {tip[:9]}"
                return False
            with self._lock:
                self._merge_bases[(base_tip, tip)] = merge_base

        with self._lock:
            mbs = {tip: self._merge_bases[(base_tip, tip)] for tip, _b in want["rows"] if self._merge_bases.get((base_tip, tip))}
        oldest = max(want["rows"], key=lambda item: item[1] if isinstance(item[1], int) else -1)
        oldest_mb = mbs.get(oldest[0])

        if oldest_mb and not (self._base_records and self._base_records[0] == (base_tip, oldest_mb)):
            code, out, _err = self._run(
                build_log_records_command(repo, [base_tip], [oldest_mb], no_merges=False), GIT_TIMEOUT_SECONDS
            )
            if code != 0:
                self.last_error = "git log of the base failed"
                return False
            with self._lock:
                self._base_records = ((base_tip, oldest_mb), parse_log_records(out))

        heads = [h for h in want["heads"] if is_full_oid(h)]
        pr_key = (base_tip, frozenset(want["heads"]))
        if not (self._pr_records and self._pr_records[0] == pr_key):
            records: list[dict] = []
            if heads:
                present = self._present(heads)
                if present:
                    code, out, _err = self._run(
                        build_log_records_command(repo, present, [base_tip], no_merges=False), GIT_TIMEOUT_SECONDS
                    )
                    if code != 0:
                        self.last_error = "git log of the PR heads failed"
                        return False
                    records = parse_log_records(out)
            with self._lock:
                self._pr_records = (pr_key, records)

        for tip, _behind in want["rows"]:
            if (base_tip, tip) in self._branch_commits:
                continue
            code, out, _err = self._run(build_log_records_command(repo, [tip], [base_tip], no_merges=True), GIT_TIMEOUT_SECONDS)
            if code != 0:
                self.last_error = f"git log failed for {tip[:9]}"
                return False
            with self._lock:
                self._branch_commits[(base_tip, tip)] = parse_log_records(out)

        known = self._store.commits()
        with self._lock:
            pool = list(self._base_records[1] if self._base_records else [])
            pool += list(self._pr_records[1] if self._pr_records else [])
            for (key_base, _tip), commits in self._branch_commits.items():
                if key_base == base_tip:
                    pool += commits
        todo, seen = [], set()
        for record in pool:
            oid = record["oid"]
            if oid in known or oid in seen or len(record["parents"]) > 1:
                continue
            seen.add(oid)
            todo.append(oid)
        for start in range(0, len(todo), PATCH_CHUNK):
            if self._over(started):
                return True
            chunk = todo[start : start + PATCH_CHUNK]
            code, patch, _err = self._run(build_patch_log_command(repo), PATCH_LOG_TIMEOUT_SECONDS, input="\n".join(chunk) + "\n")
            if code != 0:
                self.last_error = "git log -p failed"
                return False
            code, out, _err = self._run(build_patch_id_command(repo), PATCH_LOG_TIMEOUT_SECONDS, input=patch)
            if code != 0:
                self.last_error = "git patch-id failed"
                return False
            found = parse_patch_ids(out)
            # A commit whose patch is EMPTY (a pure mode change, an emptied
            # cherry-pick) prints no patch-id at all; it is recorded as such
            # so it is not asked again, and it can never match anything.
            for oid in chunk:
                found.setdefault(oid, UNHASHABLE)
            self._store.add(commits=found)

        for tip, mb in mbs.items():
            if self._over(started):
                return True
            if self._store.diff(mb, tip) is not None:
                continue
            code, patch, _err = self._run(build_diff_command(repo, mb, tip), PATCH_LOG_TIMEOUT_SECONDS)
            if code != 0:
                self.last_error = f"git diff failed for {tip[:9]}"
                return False
            pid = ""
            if patch.strip():
                code, out, _err = self._run(build_patch_id_command(repo), PATCH_LOG_TIMEOUT_SECONDS, input=patch)
                if code != 0:
                    self.last_error = "git patch-id failed"
                    return False
                pid = parse_single_patch_id(out) or ""
                if not pid:
                    # A non-empty diff git would not hash (binary-only, say)
                    # is NOT the empty diff — record a value nothing matches.
                    pid = UNHASHABLE
            self._store.add(diffs={f"{mb}:{tip}": pid})
        self.last_error = None
        return True

    def _present(self, oids: list[str]) -> list[str]:
        """The ids this repo actually has — a `log` naming a missing one fails."""
        missing = missing_objects(self._repo_root, oids, runner=self._runner)
        return [oid for oid in oids if oid not in missing] if missing is not None else []


# ── the other I/O ──────────────────────────────────────────────────────────


def missing_objects(repo_root: Path | str, oids: list[str], *, runner: Runner | None = None) -> set[str] | None:
    """The ids in `oids` this repo lacks; None when git could not answer."""
    if not oids:
        return set()
    run = runner or create_mod.run_bounded
    code, out, _err = run(build_batch_check_command(repo_root), GIT_TIMEOUT_SECONDS, input="\n".join(oids) + "\n")
    return parse_missing_objects(out) if code == 0 else None


def build_local_branches_command(repo_root: Path | str) -> list[str]:
    return ["git", "-C", str(repo_root), "for-each-ref", "--format=%(refname:short)", "refs/heads/"]


def local_branch_names(repo_root: Path | str, *, runner: Runner | None = None) -> set[str]:
    """Every local branch name; empty when git cannot answer."""
    run = runner or create_mod.run_bounded
    code, out, _err = run(build_local_branches_command(repo_root), GIT_TIMEOUT_SECONDS)
    return {line.strip() for line in out.splitlines() if line.strip()} if code == 0 else set()


def contains_sets(repo_root: Path | str, rows: list[dict], *, runner: Runner | None = None) -> dict[str, set[str]] | None:
    """`{branch: every local branch holding its tip}` — one fork per row.
    None when any fork fails, and the caller then marks nothing contained:
    a holder git could not name must never be assumed."""
    run = runner or create_mod.run_bounded
    result: dict[str, set[str]] = {}
    for row in rows:
        oid = row.get("oid")
        if not is_full_oid(oid):
            continue
        code, out, _err = run(build_contains_command(repo_root, oid), GIT_TIMEOUT_SECONDS)
        if code != 0:
            return None
        result[row["branch"]] = {line.strip() for line in out.splitlines() if line.strip()}
    return result


def sweep_occupants(paths: list[str], *, runner: Runner | None = None) -> dict[str, list[str] | None]:
    """`{worktree path: [command names]}` for every path, from ONE `lsof`.

    Paths are resolved before the match (`/tmp` → `/private/tmp`) and the
    answer is keyed by the path AS GIVEN. Every value is None when `lsof`
    could not answer — "not probed", which every reader treats as occupied.
    `lsof` exits 1 when some process could not be read; its output is still
    the answer for every process it COULD read, so only a missing binary or
    a timeout (negative / no output) is a failure.
    """
    run = runner or create_mod.run_bounded
    code, out, _err = run(build_lsof_command(), GIT_TIMEOUT_SECONDS)
    if code < 0 or (code != 0 and not out.strip()):
        return dict.fromkeys(paths)
    resolved = {path: os.path.realpath(path) for path in paths if path}
    occupants = occupants_by_path(list(resolved.values()), parse_lsof_cwds(out))
    return {path: occupants.get(real.rstrip("/"), []) for path, real in resolved.items()}


def await_vacated(
    path: str, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic
) -> list[str] | None:
    """Who is still standing in `path` once just-killed sessions have had
    `OCCUPANT_SETTLE_SECONDS` to leave it: `[]`, the command names left, or
    None when `lsof` could not answer (unknown — every reader refuses on it,
    as `decide_outside_remove` does). One probe straight after `kill-session`
    would name the agent that is already on its way out."""
    deadline = clock() + OCCUPANT_SETTLE_SECONDS
    while True:
        left = sweep_occupants([path]).get(path)
        if left == [] or clock() >= deadline:
            return left
        sleep(OCCUPANT_POLL_SECONDS)


def work_landed(landing: dict | None) -> bool:
    """Did work of the branch's OWN reach the base? The rack's CLEAN asks this
    of a landed row, on both sides of HTTP (`rack-view.ts::work_landed` is the
    twin and points here).

    `landed` alone cannot say: a branch cut from the base and never committed
    to is an ancestor of it, so it reads landed the moment it exists — a fresh
    worktree session would be offered CLEAN, and pressing it would kill its
    agent mid-turn. Every other proof is of commits the base did not have by
    tip (`pr`, `squash`, `commits`), so it carried work; an `ancestor` only
    with the merged PR that shipped its tip (`ContentTier.judge` attaches it).
    A branch merged into the base by hand, with no PR, therefore keeps its PR
    button — the harbor's CLEANUP still takes it.
    """
    if not landing:
        return False
    return landing.get("how") != "ancestor" or landing.get("pr") is not None
