"""Every local branch, priced in ONE fork, and the one table that decides
whether a branch may be deleted.

This is the data half of the Branch Harbor (`GET /branches`). The pure half —
the `for-each-ref` format and its parser, the delete refusal table, the argv
builders, `gh pr list`'s parser — carries every judgement, so it is testable
against a fixture of what git really prints. The I/O half at the foot of the
file only runs commands and hands the text up.

── One `for-each-ref`, not N probes ─────────────────────────────────────────
`pr.py::ahead_behind` runs `git rev-list` per worktree; on 41 branches that is
41 forks. `git for-each-ref refs/heads/` with `%(ahead-behind:<base>)` answers
the same question for every branch in ~11 ms, and `%(worktreepath)` folds the
checkout join in with it. Fields are joined by U+001F (`FIELD_SEP`), the same
trick `bridge/tldrack/snapshot.py` uses for tmux: a subject
or a path is arbitrary text, and a tab or a pipe is not a separator you can
trust inside it.

── `merged` is tri-state, and it is exactly `ahead == 0` ─────────────────────
`git branch -d` refuses a branch whose tip is not reachable from HEAD's
upstream or from the branch it is measured against; "merged" here means the
same thing — zero commits the LOCAL base lacks — and nothing else. `git branch
--merged` is not a second signal, it is the same predicate. When `ahead` is
unknown (git older than 2.41, a failed sweep) `merged` is None and stays None:
the safe-looking default must never become permission, which is why
`decide_delete` refuses an unknown even when asked to force.

── `landed` is the wider answer, and it is what every verdict reads ──────────
`merged` is blind to a squash-merge — many repositories' default — so a branch whose
PR shipped reads `ahead > 0` forever. `landed` adds exactly one more proof: a
MERGED pull request whose head IS this tip. A merged PR alone is not that
(a branch can have a merged PR and a
commit `main` still lacks); the tip-equals-head test is what tells those apart.
`join_landing` is the one door that computes it — see `landed` for the rules.

── The remote tier is a SECOND sweep, with its OWN verdict ───────────────────
`for-each-ref refs/heads/` is local refs and nothing else, so for as long as
the harbor existed a branch that lived only on a remote was invisible to it,
and there can be several times more of those than local ones. `sweep_remote_
refs` walks `refs/remotes/<remote>/` once per remote so each remote's branches
are measured against its OWN base, and `remote_only` drops the ones that are
already local islands.

These rows do NOT carry `delete_verdict`, and never will: that table reasons
about a local ref against a local base, and deleting one of these is `git push
--delete`, a network write other people can see. They carry `scuttle_verdict`
instead — a SEPARATE, narrower table whose only clear answer is "this has
already landed on its remote's base". See `landed` for why the ancestor test
alone is not that answer.

── The delete refusal table is shipped, not re-derived ───────────────────────
`delete_verdict` runs the same table `POST /delete-branch` runs, once per row
at sweep time, so the harbor only ever offers a delete the route will honour.
The route runs it again on a FRESH sweep at click time; the row's verdict is
a promise about the last four seconds, the click is a promise about now.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from worktree import create as create_mod
from worktree.listing import PROBE_TIMEOUT_SECONDS
from worktree.names import display_path, validate_existing_branch

#: U+001F between fields — see the module header.
FIELD_SEP = "\x1f"

#: The `%(ahead-behind:<base>)` field, formatted with the base branch. It is
#: the one field that needs git ≥ 2.41, and the one the ladder drops.
AHEAD_BEHIND_FIELD = "ahead-behind:{base}"

#: Every field, in wire order. `upstream:remoteref` yields `refs/heads/<name>`
#: (stripped in the parser); `upstream:track` is the bracketed summary
#: (`[ahead 1, behind 2]`, `[gone]`, or empty). The full `objectname` rides
#: beside the short one because `landed` compares it to a PR's head: an
#: abbreviation is only unambiguous among objects THIS repo has, and a PR head
#: that was never fetched is exactly the object it does not.
REF_FIELDS = (
    "refname:short",
    "objectname:short",
    "objectname",
    "upstream:short",
    "upstream:remotename",
    "upstream:remoteref",
    "upstream:track",
    AHEAD_BEHIND_FIELD,
    "worktreepath",
    "committerdate:unix",
    "authorname",
    "subject",
)

#: The REMOTE sweep's fields. `symref` is the one that is not decoration:
#: `refs/remotes/<remote>/HEAD` is git's pointer to that remote's default
#: branch, and its `refname:short` is bare `origin`. Read without this field it
#: looks exactly like a branch NAMED `origin`, which is what it was mistaken
#: for when this was written. It is a symref; filter on that, not on the name.
REMOTE_REF_FIELDS = (
    "refname:short",
    "symref",
    "objectname:short",
    "objectname",
    AHEAD_BEHIND_FIELD,
    "committerdate:unix",
    "authorname",
    "subject",
)

#: Local git only — the same budget `listing.list_worktrees` gives itself.
GIT_LOCAL_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS

#: `git push --delete`'s budget. Its own number because it is the only
#: SYNCHRONOUS network write here: `pr.NETWORK_TIMEOUT_SECONDS` (120) is sized
#: for a background worker nobody waits on, and a browser holding a request
#: open that long would give up first and report a failure for a delete that
#: actually landed. Deleting one ref is sub-second on a healthy link; 30 s is
#: a hung link, not a slow one. The client's budget must stay ABOVE this.
SCUTTLE_TIMEOUT_SECONDS = 30

#: What a git older than 2.41 says about `%(ahead-behind:)`: exit 128 and
#: `fatal: unknown field name: ahead-behind:main`. The ladder retries WITHOUT
#: that field on this marker and on nothing else.
OLD_GIT_MARKER = "unknown field name"

#: What git says when the BASE ref does not exist (`fatal: failed to find
#: 'main'` on 2.54; older wordings kept for the same failure). Not retried: a
#: repo with no base branch has no merged/unmerged answer to give.
MISSING_BASE_MARKERS = ("failed to find", "not a valid", "bad revision", "unknown revision")

#: How many PRs one `gh pr list` may return. The index is a REPLACED snapshot
#: (`harbor.PrIndexStore`), so a PR the list omits is gone from it, and with it
#: every `pr` landing proof that PR carried. A repository outgrew a ceiling of
#: 200: its oldest PRs fell off, and a long-lived branch whose tip sat inside a
#: merged PR's head read as unlanded. gh pages 100 at a time up to this ceiling, so the
#: cost is the repo's real size; a list that FILLS it may have been cut, and
#: says so (`pr_list_truncated`).
PR_LIST_CEILING = 5000
PR_LIST_FIELDS = ("number", "headRefName", "headRefOid", "baseRefName", "state", "url", "isDraft", "mergedAt", "updatedAt")

#: A full object id: SHA-1 (40 hex) or SHA-256 (64). `landed` compares only
#: these — never an abbreviation (see `REF_FIELDS`), never anything `gh` or a
#: persisted file could hand over half-formed. Lowercase, as git prints them.
FULL_OID_LENGTHS = (40, 64)
HEX_DIGITS = frozenset("0123456789abcdef")

#: How long an ancestry probe git could NOT answer is believed. That happens
#: when a PR's head was never fetched here (another checkout pushed it), and a
#: later fetch can bring it, so the miss is retried — but not on every 4 s
#: sweep. A definite yes/no is never retried: ancestry between two fixed
#: commits cannot change.
ANCESTRY_UNKNOWN_TTL_SECONDS = 300.0

#: The probe cache's bound. One entry per (PR head, branch tip) ever asked
#: about; a full cache is cleared rather than evicted — re-asking is one fork.
ANCESTRY_CACHE_LIMIT = 512

#: Which PR wins when a head branch has several: an open one is the live fact,
#: a merged one is the history that matters, a closed one is the least.
PR_STATE_RANK = {"open": 0, "merged": 1, "closed": 2}

#: The wording a row and the route share for a branch a `/create` is mid-way
#: through checking out. Refused rather than raced: the worktree may not be on
#: disk yet, so `%(worktreepath)` cannot see it, but `git branch -d` would
#: pull the ref out from under `git worktree add`.
CREATING_REASON = "a worktree is being created for this branch — wait for it"

UNKNOWN_MERGED_REASON = "could not determine whether this branch is merged — refusing to delete; run git yourself"

#: The remote tier's two refusals. Kept apart from `UNKNOWN_MERGED_REASON`
#: because that one ends "run git yourself" — right for a local branch you can
#: inspect in a second, wrong for a ref on someone else's server.
REMOTE_UNKNOWN_MERGED_REASON = "could not measure this branch against its remote's base — fetch, then look again"
REMOTE_UNLANDED_REASON = "has work its remote's base does not — not a cleanup"


# ── argv builders (pure) ──────────────────────────────────────────────────


def ref_format(base: str, *, ahead_behind: bool) -> str:
    """The `--format` string. `%1F` is git's hex escape for U+001F, so the argv
    stays printable in a log while the OUTPUT carries the real byte."""
    fields = [field for field in REF_FIELDS if ahead_behind or field != AHEAD_BEHIND_FIELD]
    return "%1F".join(f"%({field.format(base=base)})" for field in fields)


def build_for_each_ref_command(repo_root: Path | str, base: str, *, ahead_behind: bool) -> list[str]:
    return ["git", "-C", str(repo_root), "for-each-ref", "refs/heads/", "--format", ref_format(base, ahead_behind=ahead_behind)]


def remote_ref_format(remote: str, base: str, *, ahead_behind: bool) -> str:
    """The remote sweep's `--format`. The ahead-behind base is `<remote>/<base>`,
    not the local base: "has this landed?" about a branch on a remote is a
    question about THAT remote's main, and a local `main` that has not been
    pulled in a week would answer it wrong."""
    fields = [field for field in REMOTE_REF_FIELDS if ahead_behind or field != AHEAD_BEHIND_FIELD]
    return "%1F".join(f"%({field.format(base=f'{remote}/{base}')})" for field in fields)


def build_remote_for_each_ref_command(repo_root: Path | str, remote: str, base: str, *, ahead_behind: bool) -> list[str]:
    """Scoped to ONE remote so the ahead-behind base can be that remote's own.
    The trailing `/` matters: `refs/remotes/origin` would also match a remote
    named `origin-mirror`."""
    return [
        "git",
        "-C",
        str(repo_root),
        "for-each-ref",
        f"refs/remotes/{remote}/",
        "--format",
        remote_ref_format(remote, base, ahead_behind=ahead_behind),
    ]


def build_remote_list_command(repo_root: Path | str) -> list[str]:
    """`git remote` — names only, no URLs. Touches no objects."""
    return ["git", "-C", str(repo_root), "remote"]


def build_branch_delete_command(repo_root: Path | str, branch: str, force: bool) -> list[str]:
    """`-d` unless forced; `-D` is `-d --force`, and the distinction is the
    whole point of the arm the panel shows."""
    return ["git", "-C", str(repo_root), "branch", "-D" if force else "-d", branch]


def build_prune_command(repo_root: Path | str) -> list[str]:
    """`-v` so git NAMES what it dropped — the route reports those names."""
    return ["git", "-C", str(repo_root), "worktree", "prune", "-v"]


def build_fetch_command(repo_root: Path | str, remote: str) -> list[str]:
    """`--prune` here is REMOTE-tracking refs (an `[gone]` upstream becomes
    visible); `git worktree prune` is a different verb and a different route."""
    return ["git", "-C", str(repo_root), "fetch", "--prune", remote]


def build_push_delete_command(repo_root: Path | str, remote: str, branch: str) -> list[str]:
    """`git push <remote> --delete <branch>` — the remote tier's only write.

    The plain porcelain, not `gh api`, for one reason: a repository may have a
    second remote that is not on GitHub. A verb that worked on `origin` and
    silently did nothing on the other would be worse than no verb.

    `--` is not available here (git push has no such separator for refspecs),
    which is exactly why `validate_existing_branch` runs FIRST in the route:
    a name starting with `-` must never reach this argv.
    """
    return ["git", "-C", str(repo_root), "push", remote, "--delete", branch]


def fetch_env() -> dict[str, str]:
    """The child environment for the fetch worker: fail fast, never ask.

    A `git fetch` that wants a credential or an ssh passphrase PROMPTS, and on
    a detached background thread a prompt is a hang for the whole
    NETWORK_TIMEOUT — the trap `pr.py::build_push_command` names. Both knobs
    turn the question into an immediate failure the row can render.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


def build_is_ancestor_command(repo_root: Path | str, ancestor: str, descendant: str) -> list[str]:
    """`git merge-base --is-ancestor`: exit 0 yes, 1 no, anything else unknown.
    Only ever handed full object ids (`is_full_oid`), so neither can read as
    an option."""
    return ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", ancestor, descendant]


def build_pr_list_command(limit: int = PR_LIST_CEILING) -> list[str]:
    """Runs with `cwd` = the repo (gh has no `-C`; it reads the working
    directory's remotes)."""
    return ["gh", "pr", "list", "--state", "all", "--limit", str(limit), "--json", ",".join(PR_LIST_FIELDS)]


# ── parsers (pure, total) ─────────────────────────────────────────────────


def parse_upstream_track(text: object) -> tuple[int, int, bool]:
    """`%(upstream:track)` → `(ahead, behind, gone)`.

    Forms: `` (in sync, or no upstream), `[ahead N]`, `[behind M]`,
    `[ahead N, behind M]`, `[gone]`. Anything else reads as in sync — a
    surprise wording must not invent a number.
    """
    if not isinstance(text, str):
        return 0, 0, False
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        return 0, 0, False
    body = body[1:-1].strip()
    if body == "gone":
        return 0, 0, True
    ahead = behind = 0
    for part in body.split(","):
        words = part.split()
        if len(words) != 2 or not words[1].isdigit():
            continue
        if words[0] == "ahead":
            ahead = int(words[1])
        elif words[0] == "behind":
            behind = int(words[1])
    return ahead, behind, False


def parse_ahead_behind(text: object) -> tuple[int, int] | None:
    """`%(ahead-behind:<base>)` → `(ahead, behind)`, or None when it is not
    two integers. None, never `(0, 0)`: see the module header."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def merged_flag(ahead: int | None) -> bool | None:
    """The tri-state, in one place."""
    return None if ahead is None else ahead == 0


def is_full_oid(value: object) -> bool:
    """A complete lowercase object id — `FULL_OID_LENGTHS` owns why only these."""
    return isinstance(value, str) and len(value) in FULL_OID_LENGTHS and HEX_DIGITS.issuperset(value)


def upstream_kind(upstream_branch: str, branch: str, base: str) -> str:
    """`own` = the remote counterpart of this branch, `base` = the remote base
    (seven branches here track `origin/main` — their upstream numbers repeat
    the vs-base reading and the client hides them), `other` = anything else.

    `own` is tested first so the base branch itself, tracking `origin/main`,
    is `own`: its `behind` is the "↓N behind origin/main" the main island shows.
    """
    if upstream_branch == branch:
        return "own"
    if upstream_branch == base:
        return "base"
    return "other"


def strip_heads(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def parse_for_each_ref(text: object, base: str, *, ahead_behind: bool) -> list[dict]:
    """The sweep's stdout → one dict per branch. Total: a line with the wrong
    field count is skipped, never guessed at.

    `ahead_behind` says which format PRODUCED the text; without the field every
    row's `ahead`/`behind`/`merged` is None. The row's `worktree_path` is the
    ABSOLUTE path git printed — the join against `git worktree list` happens on
    it, and only the assembled document collapses paths.
    """
    if not isinstance(text, str):
        return []
    expected = len(REF_FIELDS) - (0 if ahead_behind else 1)
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.split(FIELD_SEP)
        if len(parts) != expected:
            continue
        if ahead_behind:
            (branch, sha, oid, up_ref, up_remote, up_remoteref, up_track, ab, worktree_path, committed, author, subject) = parts
            counts = parse_ahead_behind(ab)
        else:
            (branch, sha, oid, up_ref, up_remote, up_remoteref, up_track, worktree_path, committed, author, subject) = parts
            counts = None
        if not branch:
            continue
        ahead, behind = counts or (None, None)
        upstream = None
        if up_ref:
            track_ahead, track_behind, gone = parse_upstream_track(up_track)
            up_branch = strip_heads(up_remoteref)
            upstream = {
                "ref": up_ref,
                "remote": up_remote,
                "branch": up_branch,
                "kind": upstream_kind(up_branch, branch, base),
                "ahead": track_ahead,
                "behind": track_behind,
                "gone": gone,
            }
        rows.append(
            {
                "branch": branch,
                "sha": sha,
                "oid": oid if is_full_oid(oid) else None,
                "is_base": branch == base,
                "committed_at": int(committed) if committed.isdigit() else 0,
                "author": author,
                "subject": subject,
                "ahead": ahead,
                "behind": behind,
                "merged": merged_flag(ahead),
                "upstream": upstream,
                "worktree_path": worktree_path or None,
            }
        )
    return rows


def parse_remote_list(text: object) -> list[str]:
    """`git remote` → the remote names, in git's order, deduped."""
    if not isinstance(text, str):
        return []
    seen: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def parse_remote_refs(text: object, remote: str, base: str, *, ahead_behind: bool) -> list[dict]:
    """One remote sweep's stdout → one dict per remote branch on `remote`.

    Total, like `parse_for_each_ref`: a line with the wrong field count is
    skipped rather than guessed at.

    Two refs are dropped here and nowhere else, because both would render as
    branches that do not exist:

    * anything with a `symref` — `refs/remotes/<remote>/HEAD`, whose short name
      is the bare remote name (see `REMOTE_REF_FIELDS`);
    * `<remote>/<base>` itself, which is the thing everything else is measured
      against and is never cleanup.

    `merged` is the same tri-state and the same rule as a local row's, read
    against the remote base: `ahead == 0` means the remote's main already has
    every commit, which is the whole cleanup signal this tier exists to give.
    """
    if not isinstance(text, str):
        return []
    expected = len(REMOTE_REF_FIELDS) - (0 if ahead_behind else 1)
    prefix = f"{remote}/"
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.split(FIELD_SEP)
        if len(parts) != expected:
            continue
        if ahead_behind:
            (short, symref, sha, oid, ab, committed, author, subject) = parts
            counts = parse_ahead_behind(ab)
        else:
            (short, symref, sha, oid, committed, author, subject) = parts
            counts = None
        if not short or symref:
            continue
        if not short.startswith(prefix):
            continue
        branch = short[len(prefix) :]
        if not branch or branch == base:
            continue
        ahead, behind = counts or (None, None)
        rows.append(
            {
                "ref": short,
                "remote": remote,
                "branch": branch,
                "sha": sha,
                "oid": oid if is_full_oid(oid) else None,
                "committed_at": int(committed) if committed.isdigit() else 0,
                "author": author,
                "subject": subject,
                "ahead": ahead,
                "behind": behind,
                "merged": merged_flag(ahead),
            }
        )
    return rows


def remote_only(remote_rows: list[dict], local_branches: object) -> list[dict]:
    """The remote rows with no LOCAL branch of the same name — the tier the
    harbor could not see at all.

    A remote branch that HAS a local counterpart is not dropped for tidiness:
    it is already an island, and its `upstream` line carries this exact ref.
    Rendering it twice would be the repeat test's wallpaper — the same string
    on two objects, informing on neither.

    Matched on the branch NAME across every remote, so a branch pushed to two
    remotes and checked out locally is hidden on both.
    """
    names = set(local_branches or ())
    return [row for row in remote_rows if row.get("branch") not in names]


def parse_prune_output(text: object) -> list[str]:
    """`git worktree prune -v` → the admin names it removed.

    Lines read `Removing worktrees/<name>: <reason>`; the name is git's
    per-worktree directory under `.git/worktrees/`, which is the only handle a
    pruned (directory-less) worktree still has.
    """
    if not isinstance(text, str):
        return []
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("Removing worktrees/"):
            continue
        rest = line[len("Removing worktrees/") :]
        name = rest.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return names


def parse_pr_list(text: object) -> dict[str, dict]:
    """`gh pr list --json …` → `{headRefName: row}`, one PR per branch.

    Precedence when a branch has several: open > merged > closed, then the
    newest `updatedAt` (ISO text; it sorts). Raises ValueError on a body that
    is not a JSON list — the worker records that on the index instead of
    replacing a good index with an empty one. Individual bad rows are dropped.
    """
    index: dict[str, dict] = {}
    for item in _pr_list_items(text):
        if not isinstance(item, dict):
            continue
        head = item.get("headRefName")
        number = item.get("number")
        state = item.get("state")
        if not isinstance(head, str) or not head or not isinstance(number, int):
            continue
        if not isinstance(state, str) or state.lower() not in PR_STATE_RANK:
            continue
        head_oid = item.get("headRefOid")
        base_ref = item.get("baseRefName")
        row = {
            "number": number,
            "state": state.lower(),
            "url": item.get("url") if isinstance(item.get("url"), str) else None,
            "draft": item.get("isDraft") is True,
            "merged_at": item.get("mergedAt") if isinstance(item.get("mergedAt"), str) else None,
            "updated_at": item.get("updatedAt") if isinstance(item.get("updatedAt"), str) else "",
            "head_oid": head_oid if is_full_oid(head_oid) else None,
            "base_ref": base_ref if isinstance(base_ref, str) and base_ref else None,
        }
        current = index.get(head)
        if current is None or _pr_beats(row, current):
            index[head] = row
    return index


def pr_list_truncated(text: object, ceiling: int = PR_LIST_CEILING) -> bool:
    """True when gh returned as many PRs as it was allowed to — the list may
    have been cut, so the index cannot vouch for the oldest PRs. Counts RAW
    items, before `parse_pr_list` folds several PRs per branch into one."""
    return len(_pr_list_items(text)) >= ceiling


def _pr_list_items(text: object) -> list:
    """`gh pr list --json` → its raw list; ValueError when it is not one."""
    try:
        data = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError as err:
        raise ValueError(f"gh pr list printed no JSON ({err.msg})") from None
    if not isinstance(data, list):
        raise ValueError("gh pr list printed no JSON list")
    return data


def _pr_beats(candidate: dict, current: dict) -> bool:
    rank_c, rank_k = PR_STATE_RANK[candidate["state"]], PR_STATE_RANK[current["state"]]
    if rank_c != rank_k:
        return rank_c < rank_k
    return candidate["updated_at"] > current["updated_at"]


# ── the delete refusal table (pure) ───────────────────────────────────────


def decide_delete(
    *,
    known: bool,
    is_base: bool,
    creating: bool,
    checked_out_at: str | None,
    managed: bool,
    merged: bool | None,
    ahead: int | None,
    force: bool,
    base: str,
    name_error: str | None = None,
    landed: bool = False,
    contained: bool = False,
) -> tuple[int, dict] | None:
    """The refusal table for POST /delete-branch. None = clear to delete.

    The order is the contract: name invalid → unknown → base → creating →
    checked out → LANDED or CONTAINED (clear) → merged unknown → unmerged
    (needs force) → clear. `force` is consulted by exactly ONE row; nothing above it can be
    forced from here, and the message a user sees is the one they can act on.

    `landed` clears ahead of both merge rows because it is a PROOF, not a
    default: a merged PR whose head is this very tip (`landed` owns the
    rules). It answers even where `merged` is None — an old git cannot count
    `ahead`, but the PR's head is still the tip. It defaults to False, so a
    caller that never joined the PR index can only refuse more, never less.
    `contained` clears on the same footing: every commit is on another
    branch that is kept (`landing.containment` owns the rule), so a delete
    loses nothing — and it, too, defaults to False.

    The name row is FIRST and it is in the table, not only in the route: a
    row that said `ok` for a name the route answers 400 to would promise a
    delete that cannot happen. The route passes the reason in;
    `delete_verdict` computes it.

    That reason comes from `names.validate_existing_branch` — git's rules —
    and NOT from `validate_branch`, which is the create path's narrower
    minting alphabet. Judging an existing ref by the minting alphabet would
    strand `fix-+-retry` as an undeletable atoll: git lists it, `git branch -d`
    takes it, and the harbor would refuse it over a `+`.

    Checked out is refused BEFORE the merge state, whatever that state is,
    because `git branch -d` refuses it anyway and the useful answer is which
    worktree holds it: one of ours ("remove the worktree first", and the
    body says `worktree_removable` so the panel can offer that), or one that
    is not ours to remove (Claude Code's own `~/other-tool/worktrees/` trees).
    """
    if name_error is not None:
        return 400, {"error": name_error}
    if not known:
        return 404, {"error": "no local branch by that name"}
    if is_base:
        return 409, {"error": f"'{base}' is the base branch and is never deleted from here"}
    if creating:
        return 409, {"error": CREATING_REASON}
    if checked_out_at:
        where = display_path(checked_out_at)
        if managed:
            return 409, {"error": f"checked out at {where} — remove the worktree first", "worktree_removable": True}
        return 409, {"error": f"checked out at {where} — that worktree is not ours to remove"}
    if landed or contained:
        return None
    if merged is None:
        return 409, {"error": UNKNOWN_MERGED_REASON}
    if merged is False and not force:
        count = ahead if isinstance(ahead, int) else 0
        noun = "commit is" if count == 1 else "commits are"
        return 409, {"error": f"{count} {noun} not on {base}", "needs_force": True, "ahead": count}
    return None


def delete_verdict(row: dict, *, base: str, creating: bool = False) -> tuple[str, str | None, bool]:
    """`(verdict, reason, worktree_removable)` for an assembled branch row —
    the same table, asked with `force=False`, so the row promises exactly what
    the route will do on a plain click. The row's `worktree.path` may already
    be collapsed; `display_path` leaves a collapsed path alone."""
    worktree = row.get("worktree") or {}
    refusal = decide_delete(
        name_error=validate_existing_branch(row.get("branch")),
        known=True,
        is_base=bool(row.get("is_base")),
        creating=creating,
        checked_out_at=worktree.get("path") or None,
        managed=bool(worktree.get("managed")),
        merged=row.get("merged"),
        ahead=row.get("ahead"),
        force=False,
        base=base,
        landed=row.get("landed") is True,
        contained=bool(row.get("contained_in")),
    )
    if refusal is None:
        return "ok", None, False
    _status, body = refusal
    if body.get("needs_force"):
        return "needs_force", body["error"], False
    return "refused", body["error"], body.get("worktree_removable", False)


def needs_dash_d_upper(*, force: bool, merged: bool | None, landed: bool, contained: bool = False) -> bool:
    """Whether the delete runs `-D`: asked to force, or LANDED / CONTAINED
    without being an ancestor. git's own `-d` check is the ancestor test, so
    it refuses every squash-landed branch `decide_delete` just cleared — the
    table has already proven what `-d` cannot see, and `-D` is how that proof
    acts."""
    return force or ((landed or contained) and merged is not True)


def parse_iso8601(text: object) -> float | None:
    """`2026-09-06T18:44:52Z` → unix seconds. None for anything else.

    `gh` stamps UTC with a trailing `Z`, which `datetime.fromisoformat` only
    learned to read in 3.11; this package supports 3.10, so the suffix is
    rewritten rather than trusted. Anything unparseable answers None, and
    every caller treats None as "cannot tell" — never as zero.

    A stamp with NO offset is rejected rather than read. `.timestamp()` reads
    a naive datetime in the machine's LOCAL zone, which would slide the result
    by up to a day depending on where the laptop is — and half of those slides
    move a branch toward "landed", which is the one direction a wrong answer
    here must never go.
    """
    if not isinstance(text, str) or not text:
        return None
    stamp = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.timestamp()


#: `contains(head, tip)` — is `tip` an ancestor of `head`? True / False / None
#: (git could not say). `AncestryCache.contains` in the plugin; a lambda in a test.
Contains = Callable[[str, str], "bool | None"]


def landed(row: dict, contains: Contains | None = None, base: str | None = None) -> bool:
    """Has this branch's work reached its base? Local rows and remote rows
    alike — the base is local `main` for one, `<remote>/<base>` for the other.

    Two ways, and the second is the whole reason this function exists:

    1. `merged is True` — the tip is an ancestor of the base. This is the
       definitive answer and needs nothing else.

    2. A MERGED pull request that shipped THIS tip. A squash- or rebase-merge
       rewrites the commits, so a squash-merged branch is landed while never
       being an ancestor of anything — `merged` is False and the ancestor test
       alone would call live-and-dead exactly backwards. This repo has
       `allow_squash_merge` on, so this is the common case, not the exotic one.

    (2) is two independent tests, and BOTH must pass:

    * **The tip is the PR's head**, or an ancestor of it (`contains` — the PR
      took more commits after this checkout last moved, and squashed those
      too). This is what separates a shipped branch from one that was merged
      and then committed to again: `pr.state` reads `merged` forever after,
      and a commit made before the merge but pushed after it would pass any
      clock test. It needs FULL object ids on both sides (`is_full_oid`).
    * **`committed_at <= merged_at`.** Redundant while GitHub freezes a merged
      PR's head, and kept for the day it does not: a commit pushed after the
      merge is newer than the merge, whatever its id.

    With `base`, the PR must also have been merged INTO it: a stacked PR
    merged into its parent branch is `merged` and is not in main. An index written before
    `base_ref` existed answers None and refuses until the next refresh.

    Every unknown resolves to False. A missing PR, an index fetched before
    `head_oid` existed, a head never fetched here, an unparseable stamp, a
    committer clock running fast — each one can only move a row AWAY from
    deletable, which is the only direction a wrong answer is allowed to go.
    The cheap tests run first, so `contains` (a fork, cached) is asked last.
    """
    return row.get("merged") is True or shipped_by_pr(row, contains, base)


def shipped_by_pr(row: dict, contains: Contains | None = None, base: str | None = None) -> bool:
    """Rule (2) of `landed`, alone: a merged PR shipped THIS tip — `landed`
    owns why each test is there. Asked on its own of a row that is already an
    ancestor of the base (`landing.ContentTier.judge`), where it is what tells
    a branch whose PR merged from one cut from the base and never committed to."""
    pr = row.get("pr") or {}
    if pr.get("state") != "merged":
        return False
    if base is not None and pr.get("base_ref") != base:
        return False
    merged_at = parse_iso8601(pr.get("merged_at"))
    if merged_at is None:
        return False
    committed_at = row.get("committed_at")
    if (
        isinstance(committed_at, bool)
        or not isinstance(committed_at, (int, float))
        or committed_at <= 0
        or committed_at > merged_at
    ):
        return False
    tip, head = row.get("oid"), pr.get("head_oid")
    if not (is_full_oid(tip) and is_full_oid(head)):
        return False
    if tip == head:
        return True
    return contains is not None and contains(head, tip) is True


def landed_or_unknown(row: dict | None, *, pr_known: bool) -> bool | None:
    """A judged row's `landed` as a tri-state, for a reader that must tell
    "not landed" from "cannot say yet" — `/state`, whose answer decides the
    rack's CLEAN. Never a second rule: True only where `row["landed"]` already
    is (`landed` and `landing.ContentTier.judge` own the proofs).

    None wherever a proof could still arrive: no row, a merge state git could
    not measure, a PR index never fetched (a squash-merge is invisible without
    it), or content proofs still `pending`. The refusing readers treat None as
    False anyway; this only stops a "no" being claimed before it is known.
    """
    if row is None:
        return None
    if row.get("landed") is True:
        return True
    if row.get("merged") is None or not pr_known or row.get("content") == "pending":
        return None
    return False


def join_landing(
    rows: list[dict], pr_for: Callable[[str], dict | None], contains: Contains | None = None, base: str | None = None
) -> list[dict]:
    """Attach each row's PR and decide `landed`, in place — the ONE door.

    Every verdict that reads `landed` (`delete_verdict`, `scuttle_verdict`,
    the delete route) needs the PR joined first, and it used to be three call
    sites each remembering to. Now it is this function or nothing: a row that
    never passed through here has no `landed`, which every reader takes as
    False — the refusing direction.
    """
    for row in rows:
        row["pr"] = pr_for(row["branch"])
        row["landed"] = landed(row, contains, base)
    return rows


def scuttle_verdict(row: dict) -> tuple[str, str | None]:
    """`(verdict, reason)` for a remote-only row — may `git push --delete` take
    this branch? `"clear"` or `"refused"`, and nothing in between. Reads the
    `landed` that `join_landing` decided; a row that skipped it refuses.

    Deliberately NOT `decide_delete`'s shape. That table has a `needs_force`
    row because a local branch is cheap to lose and trivial to restore from
    the reflog; this one has no force at all. A remote delete is a network
    write other people can see, and "merged only" is a PREDICATE gate, not a
    confirm gate — there is nothing here for a force flag to unlock, so the
    field does not exist to be passed.

    An unmeasurable merge state is named separately from a measured-and-
    unlanded one. Both refuse, but only the second is a fact about the branch;
    the first is a fact about the sweep, and telling a user to look at a
    branch that is fine would be the wrong instruction.
    """
    if row.get("landed") is True:
        return "clear", None
    if row.get("merged") is None:
        return "refused", REMOTE_UNKNOWN_MERGED_REASON
    return "refused", REMOTE_UNLANDED_REASON


# ── I/O half — every fork through `create_mod.run_bounded` ────────────────

Runner = Callable[..., tuple[int, str, str]]


def sweep_refs(repo_root: Path | str, base: str, *, runner: Runner | None = None) -> tuple[list[dict], dict, str | None]:
    """`(rows, capabilities, error)` for every local branch — the ladder.

    Rung one asks for `%(ahead-behind:<base>)`. If git answers "unknown field
    name" (older than 2.41) rung two asks without it and every `merged` is
    None; `capabilities.ahead_behind` says which rung answered. Any other
    failure is final — a missing base branch is reported as exactly that,
    because retrying without the field would quietly turn "your base branch
    does not exist" into "merge state unknown" on every row.
    """
    run = runner or create_mod.run_bounded
    code, out, err = run(build_for_each_ref_command(repo_root, base, ahead_behind=True), GIT_LOCAL_TIMEOUT_SECONDS)
    if code == 0:
        return (parse_for_each_ref(out, base, ahead_behind=True), {"ahead_behind": True}, None)
    if any(marker in err for marker in MISSING_BASE_MARKERS):
        return [], {"ahead_behind": True}, f"base branch {base} not found"
    if OLD_GIT_MARKER not in err:
        return [], {"ahead_behind": True}, (err or "git for-each-ref failed").strip()[:300]
    code, out, err = run(build_for_each_ref_command(repo_root, base, ahead_behind=False), GIT_LOCAL_TIMEOUT_SECONDS)
    if code != 0:
        return [], {"ahead_behind": False}, (err or "git for-each-ref failed").strip()[:300]
    return (parse_for_each_ref(out, base, ahead_behind=False), {"ahead_behind": False}, None)


class AncestryCache:
    """`landed`'s `contains`, answered at most once per (PR head, tip) pair.

    A definite answer is kept for good — ancestry between two fixed commits
    cannot change — so a branch whose PR took more commits after it last moved
    costs one fork, ever, not one per 4 s sweep. An answer git could NOT give
    (the head was pushed from another checkout and never fetched here) is kept
    for `ANCESTRY_UNKNOWN_TTL_SECONDS` and then asked again, because a fetch
    can bring the object. Thread-safe: the sweep thread and a delete route's
    request thread both ask.

    The runner is resolved per call, not captured, so a monkeypatched
    `create.run_bounded` reaches it the way it reaches every other fork here.
    """

    def __init__(
        self, repo_root: Path | str, *, runner: Runner | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._repo_root = repo_root
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._known: dict[tuple[str, str], tuple[bool | None, float]] = {}

    def forget_unknown(self) -> None:
        """Drop every "git could not say" answer — after a fetch brought the
        objects, waiting out the TTL would only delay a proof now possible."""
        with self._lock:
            self._known = {key: hit for key, hit in self._known.items() if hit[0] is not None}

    def contains(self, head: str, tip: str) -> bool | None:
        key = (head, tip)
        with self._lock:
            hit = self._known.get(key)
        if hit is not None:
            answer, at = hit
            if answer is not None or self._clock() - at < ANCESTRY_UNKNOWN_TTL_SECONDS:
                return answer
        run = self._runner or create_mod.run_bounded
        code, _out, _err = run(build_is_ancestor_command(self._repo_root, tip, head), GIT_LOCAL_TIMEOUT_SECONDS)
        answer = True if code == 0 else False if code == 1 else None
        with self._lock:
            if len(self._known) >= ANCESTRY_CACHE_LIMIT:
                self._known.clear()
            self._known[key] = (answer, self._clock())
        return answer


def list_remotes(repo_root: Path | str, *, runner: Runner | None = None) -> list[str]:
    """The configured remotes; `[]` when git cannot answer.

    A count, not a verdict — a repo with no remote has no remote tier, which
    is a true and complete answer, not an error to surface.
    """
    run = runner or create_mod.run_bounded
    code, out, _err = run(build_remote_list_command(repo_root), GIT_LOCAL_TIMEOUT_SECONDS)
    return parse_remote_list(out) if code == 0 else []


def sweep_remote_refs(
    repo_root: Path | str, base: str, *, remotes: list[str] | None = None, runner: Runner | None = None
) -> tuple[list[dict], str | None]:
    """`(rows, error)` for every branch on every remote — one sweep PER remote.

    N+1 forks (`git remote`, then one `for-each-ref` each) where the local
    sweep is one. That is the price of measuring each remote's branches
    against its OWN base, and at ~11 ms a sweep on two remotes it is paid on
    the harbor's background thread behind a TTL, never on a request.

    The failure posture is deliberately softer than `sweep_refs`'s. This tier
    is ADDITIVE — the harbor was complete without it for months — so one bad
    remote must not empty the others or fail the document. Each remote walks
    the same ladder `sweep_refs` walks (drop `%(ahead-behind:…)` on an old
    git); a remote with no `<remote>/<base>` ref, or any other failure, is
    skipped and NAMED in the returned error. Rows already gathered stand.
    """
    run = runner or create_mod.run_bounded
    names = remotes if remotes is not None else list_remotes(repo_root, runner=run)
    rows: list[dict] = []
    failures: list[str] = []
    for remote in names:
        gathered, failure = _sweep_one_remote(repo_root, remote, base, run)
        rows.extend(gathered)
        if failure is not None:
            failures.append(f"{remote}: {failure}")
    return rows, ("; ".join(failures) if failures else None)


def _sweep_one_remote(repo_root: Path | str, remote: str, base: str, run: Runner) -> tuple[list[dict], str | None]:
    """One remote's ladder. `sweep_remote_refs` owns the reasoning."""
    for ahead_behind in (True, False):
        code, out, err = run(
            build_remote_for_each_ref_command(repo_root, remote, base, ahead_behind=ahead_behind), GIT_LOCAL_TIMEOUT_SECONDS
        )
        if code == 0:
            return parse_remote_refs(out, remote, base, ahead_behind=ahead_behind), None
        # A missing `<remote>/<base>` and an old git are the SAME retry here,
        # unlike the local sweep: without the field the rows are still worth
        # having (name, author, subject), only their merge state is unknown.
        retryable = OLD_GIT_MARKER in err or any(marker in err for marker in MISSING_BASE_MARKERS)
        if ahead_behind and retryable:
            continue
        return [], (err or "git for-each-ref failed").strip()[:300]
    return [], None


def stash_count(repo_root: Path | str, *, runner: Runner | None = None) -> int:
    """Repo-global (stashes belong to the repository, not a worktree); 0 when
    git cannot answer — a count, not a verdict, so a failure is not an error."""
    run = runner or create_mod.run_bounded
    code, out, _err = run(["git", "-C", str(repo_root), "stash", "list"], GIT_LOCAL_TIMEOUT_SECONDS)
    if code != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def dirty_sweep(
    paths: list[str], budget_seconds: float, *, clock: Callable[[], float] = time.monotonic, runner: Runner | None = None
) -> dict[str, bool | None]:
    """`{path: dirty}` from `git status --porcelain` per worktree, under ONE
    budget for the whole sweep.

    25 worktrees cost ~0.7 s on a warm disk and unboundedly more on a cold
    one; this runs on the harbor's background thread, but a sweep that takes
    a minute still holds the single-flight slot for a minute. Paths the budget
    did not reach are None — "not probed", which the client renders as
    unknown, never as clean. Each probe's own timeout is the budget's
    remainder, so one wedged worktree cannot spend the others' share.
    """
    run = runner or create_mod.run_bounded
    started = clock()
    result: dict[str, bool | None] = {}
    for path in paths:
        remaining = budget_seconds - (clock() - started)
        if remaining <= 0:
            result[path] = None
            continue
        code, out, _err = run(["git", "-C", str(path), "status", "--porcelain"], max(1, math.ceil(remaining)))
        result[path] = bool(out.strip()) if code == 0 else None
    return result


def fetch_head_mtime(repo_root: Path | str) -> float | None:
    """When ANY fetch last landed — the CLI's included — from `.git/FETCH_HEAD`.
    None when there is no such file (never fetched, or `.git` is a worktree's
    pointer file rather than the main checkout's directory)."""
    try:
        return Path(repo_root, ".git", "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None
