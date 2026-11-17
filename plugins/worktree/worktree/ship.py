"""SHIP the main checkout's whole dirty tree as a pull request, then SYNC `main`
forward once it lands: `GET /ship-state`, `POST /ship`, `/ship-sync`, `/ship-cancel`.

The operator's "iteration" commits used to go straight to `main` from a terminal.
SHIP puts a PR in between, and it does so without asking anyone to stop typing:
a rack of agents keeps editing this same checkout while the PR is open.

── Why SHIP never touches the working tree, the index or `main` ──────────────
`pr.py` auto-commits a WORKTREE because one agent owns it. The main checkout is
the opposite case, so SHIP builds its commit off to the side, from plumbing:

1. `git read-tree H` into a FRESH temp `GIT_INDEX_FILE`. The real `.git/index`
   is never opened for writing and its `index.lock` is never taken, so an
   agent's `git add` mid-ship can neither be lost nor fail on the lock.
2. `git add -A` against that temp index. The working tree is only READ, and
   `.gitignore` excludes exactly what it would exclude from a commit.
3. `write-tree` → T, `commit-tree T -p H` → S. Neither moves a ref; S is a
   dangling commit until the push names it.
4. `git push origin S:refs/heads/ship/<stamp>` — a ref on the REMOTE only. No
   local branch is created, so nothing is checked out or switched.

The polled probes pass `--no-optional-locks`, so even `git status` does not
rewrite the index's stat cache behind another agent. Nothing is stashed, ever
(the operator's standing rule for the shared tree). Every `gh` call carries
`--repo`, which makes gh skip its local-branch cleanup outright —
`gh pr merge --delete-branch` would otherwise be free to check out the base.

── Why SYNC is safe ──────────────────────────────────────────────────────────
After the merge, `main` has to reach `origin/main` without losing the edits made
since the snapshot. Each move refuses rather than clobbers:

1. `update-ref refs/heads/main S H` moves `main` from H to S as a compare-and-
   swap: a commit landing between the check and the move fails it instead of
   being orphaned. Then `reset --mixed` (no revision, so it moves NO ref) sets
   the index to S's tree; the working tree is neither read nor written. Against
   S, the dirty set is now exactly the edits made after the snapshot.
2. `checkout --no-overwrite-ignore -B main origin/main` is a two-way merge from
   S. A file equal in S and `origin/main` keeps its local edit; a file another
   PR changed AND that is dirty here makes git refuse the WHOLE checkout before
   writing anything. Squash, merge and rebase landings are the same case, since
   only tree content is compared. `--no-overwrite-ignore` closes the one hole:
   by default git silently replaces an ignored local file `origin/main` tracks.

A refusal at step 2 leaves `main` at S — the shipped work, nothing lost — and the
edits as they were. The operator resolves the named file and SYNCs again; the
retry sees HEAD == S and skips the ref move. A retry after `origin/main` already
reached HEAD (a restart between checkout and bookkeeping) just records the result.

── Why the jobs run on a background thread ───────────────────────────────────
Same reason and same 202 shape as `pr.py` — see the note there. `busy_reasons`
reports the in-process job; a persisted `shipping`/`syncing` from a dead process
loads as `failed`, so a restart never reads as work still happening.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from worktree import pr as pr_mod
from worktree import sync as sync_mod
from worktree.row_store import load_store_file, persist_atomically

#: `(argv, cwd, timeout, env=None) -> (returncode, stdout, stderr)` — `pr._run`'s
#: shape. Injected so the tests can answer `gh` without GitHub.
Runner = Callable[..., tuple[int, str, str]]

PHASES = ("idle", "shipping", "open", "syncing", "failed")
IN_FLIGHT = ("shipping", "syncing")
PR_STATES = ("OPEN", "MERGED", "CLOSED")
MERGEABLE_STATES = ("MERGEABLE", "CONFLICTING", "UNKNOWN")

SHIP_BRANCH_PREFIX = "ship/"
#: `gh pr view` is a network call; a client polls far faster than a PR changes.
PR_TTL_SECONDS = 20.0
#: The local probes (`status`, `rev-list`) — under a client's poll cadence.
LOCAL_TTL_SECONDS = 4.0
#: `git add -A` against an index with no stat cache hashes every tracked file.
SNAPSHOT_TIMEOUT_SECONDS = 180
TITLE_MAX = 200
TITLE_DIRS = 3
BODY_DIRS = 12
ERROR_TAIL = 400
SHIP_FOOTER = "Shipped from the rack's SHIP button."
STORE_FILE_VERSION = 1

#: A background thread has nobody to answer a prompt; a prompt there is a hang.
GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
#: Read-only probes must not refresh the shared index (header, SHIP § last para).
PROBE_GIT = ["git", "--no-optional-locks"]
PR_VIEW_FIELDS = "number,url,state,mergeable,statusCheckRollup,mergeCommit"

_FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_FAILING_STATES = {"FAILURE", "ERROR"}


# ── pure ─────────────────────────────────────────────────────────────────────


def idle_record(last: dict | None = None) -> dict:
    return {
        "phase": "idle",
        "base": None,
        "snapshot": None,
        "branch": None,
        "title": None,
        "pr": None,
        "error": None,
        "blocking": [],
        "files": None,
        "last": last,
    }


def ship_branch_name(now: datetime, *, with_seconds: bool = False) -> str:
    """`ship/<yyyymmdd-hhmm>` in local time; seconds only to dodge a name the
    remote already holds (two ships inside one minute)."""
    return SHIP_BRANCH_PREFIX + now.strftime("%Y%m%d-%H%M%S" if with_seconds else "%Y%m%d-%H%M")


def top_dirs(paths: list[str]) -> list[tuple[str, int]]:
    """`[(top-level dir or root file, changed-file count)]`, biggest first."""
    counts: dict[str, int] = {}
    for path in paths:
        head = path.split("/", 1)[0]
        counts[head] = counts.get(head, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def default_title(paths: list[str]) -> str:
    """`iteration: app, .claude, docs (+62 files)` — the history's own
    word for these commits, plus where the change actually is."""
    dirs = [name for name, _count in top_dirs(paths)[:TITLE_DIRS]]
    noun = "file" if len(paths) == 1 else "files"
    return f"iteration: {', '.join(dirs) or 'local commits'} (+{len(paths)} {noun})"


def clean_title(raw: object) -> str | None:
    """A caller's title, whitespace-collapsed and capped, or None for blank."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())[:TITLE_MAX]
    return text or None


def build_body(paths: list[str], *, ahead: int, base: str, snapshot: str) -> str:
    groups = top_dirs(paths)
    noun = "file" if len(paths) == 1 else "files"
    lines = [SHIP_FOOTER, "", f"**{len(paths)} {noun}** changed:", ""]
    lines += [f"- `{name}` — {count}" for name, count in groups[:BODY_DIRS]]
    if len(groups) > BODY_DIRS:
        lines.append(f"- … and {len(groups) - BODY_DIRS} more")
    if ahead:
        commits = "commit" if ahead == 1 else "commits"
        lines += ["", f"Includes {ahead} local {commits} on main that {'was' if ahead == 1 else 'were'} not on origin/main yet."]
    lines += ["", f"Snapshot `{snapshot[:12]}` on top of `{base[:12]}`."]
    return "\n".join(lines)


def summarize_checks(rollup: object) -> str:
    """`statusCheckRollup` → `pending | passing | failing | none`.

    The rollup mixes CheckRuns (`status` + `conclusion`) and legacy commit
    StatusContexts (`state`). Any failure wins; then anything unfinished.
    """
    if not isinstance(rollup, list) or not rollup:
        return "none"
    pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        if "state" in check and "status" not in check:
            state = str(check.get("state") or "").upper()
            if state in _FAILING_STATES:
                return "failing"
            pending = pending or state != "SUCCESS"
            continue
        status = str(check.get("status") or "").upper()
        if status and status != "COMPLETED":
            pending = True
            continue
        if str(check.get("conclusion") or "").upper() in _FAILING_CONCLUSIONS:
            return "failing"
    return "pending" if pending else "passing"


def summarize_pr(view: object) -> dict | None:
    """`gh pr view --json` → the contract's `pr` block (+ `merge_commit`)."""
    if not isinstance(view, dict) or not isinstance(view.get("number"), int):
        return None
    state = str(view.get("state") or "").upper()
    mergeable = str(view.get("mergeable") or "").upper()
    merge_commit = view.get("mergeCommit")
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    return {
        "number": view["number"],
        "url": view.get("url") if isinstance(view.get("url"), str) else None,
        "state": state if state in PR_STATES else "OPEN",
        "checks": summarize_checks(view.get("statusCheckRollup")),
        "mergeable": mergeable if mergeable in MERGEABLE_STATES else "UNKNOWN",
        "merge_commit": oid if isinstance(oid, str) and oid else None,
    }


def choose_merge_flag(info: object) -> str | None:
    """Squash first — the history is one `Title (#n)` commit per PR — then a
    merge commit, then rebase. SYNC compares trees, so any of them lands."""
    if not isinstance(info, dict):
        return None
    for key, flag in (("squashMergeAllowed", "--squash"), ("mergeCommitAllowed", "--merge"), ("rebaseMergeAllowed", "--rebase")):
        if info.get(key) is True:
            return flag
    return None


def pr_number_from_url(url: object) -> int | None:
    match = re.search(r"/pull/(\d+)", url) if isinstance(url, str) else None
    return int(match.group(1)) if match else None


def tail(text: object, fallback: str) -> str:
    """The last `ERROR_TAIL` characters of a git/gh stderr — the end is where
    git puts the line that names the problem."""
    body = text.strip() if isinstance(text, str) else ""
    return (body or fallback)[-ERROR_TAIL:]


def _refuse(status: int, error: str) -> tuple[int, dict]:
    return status, {"ok": False, "error": error}


def decide_ship(record: dict, local: dict, *, base: str, gh_problem: str | None) -> tuple[int, dict] | None:
    """The refusal table for POST /ship (pure). None = clear to ship."""
    phase = record.get("phase")
    pr = record.get("pr")
    if phase in IN_FLIGHT:
        return _refuse(409, f"a {'SHIP' if phase == 'shipping' else 'SYNC'} is already in flight")
    if pr:
        return _refuse(409, f"PR #{pr['number']} is already open — SYNC or CANCEL it first")
    if local.get("branch_now") != base:
        where = local.get("branch_now") or "a detached HEAD"
        return _refuse(409, f"the main checkout is on {where} — SHIP only ships {base}")
    if not local.get("head_full"):
        return _refuse(409, "could not read the main checkout's HEAD")
    if not local.get("dirty") and not local.get("ahead"):
        return _refuse(409, f"nothing to ship — no uncommitted changes and nothing ahead of origin/{base}")
    if gh_problem:
        return _refuse(503, gh_problem)
    return None


def decide_sync(record: dict, local: dict, *, base: str, gh_problem: str | None, landed_in_head: bool) -> tuple[int, dict] | None:
    """The refusal table for POST /ship-sync (pure). None = clear to sync.

    Read against the CACHED PR summary; the worker re-reads it fresh before it
    merges, so a stale "passing" can only delay a refusal, never skip one.
    """
    phase = record.get("phase")
    pr = record.get("pr")
    if phase == "shipping":
        return _refuse(409, "SHIP is still running — SYNC once the PR is open")
    if phase not in ("open", "failed") or not pr:
        return _refuse(409, "no open ship PR — SHIP first")
    if local.get("branch_now") != base:
        where = local.get("branch_now") or "a detached HEAD"
        return _refuse(409, f"the main checkout is on {where} — switch back to {base}, then SYNC")
    number = pr["number"]
    if pr.get("state") == "CLOSED":
        return _refuse(409, f"PR #{number} was closed without merging — CANCEL to clear it")
    if pr.get("state") == "OPEN":
        if pr.get("checks") == "failing":
            return _refuse(409, f"PR #{number} checks are failing — fix them on GitHub, or CANCEL")
        if pr.get("checks") == "pending":
            return _refuse(409, checks_running_error(number))
        if pr.get("mergeable") == "CONFLICTING":
            return _refuse(409, f"PR #{number} conflicts with {base} — resolve it on GitHub, or CANCEL and SHIP again")
    head = local.get("head_full")
    if head not in (record.get("base"), record.get("snapshot")) and not landed_in_head:
        return _refuse(409, moved_error(base, head, record.get("base")))
    if gh_problem:
        return _refuse(503, gh_problem)
    return None


def checks_running_error(number: int) -> str:
    return f"checks still running on PR #{number} — SYNC once they pass"


def pr_blocks_sync(pr: object) -> bool:
    """Would this cached summary make `decide_sync` refuse on the PR's own
    account? Then the click re-reads it first: a refusal must be about now, and
    the cache is up to `PR_TTL_SECONDS` behind a PR merged or passing on GitHub."""
    return (
        isinstance(pr, dict)
        and pr.get("state") in ("OPEN", "CLOSED")
        and (pr.get("state") == "CLOSED" or pr.get("checks") in ("pending", "failing") or pr.get("mergeable") == "CONFLICTING")
    )


def moved_error(base: str, head: object, shipped_from: object) -> str:
    head_short = head[:9] if isinstance(head, str) else "?"
    from_short = shipped_from[:9] if isinstance(shipped_from, str) else "?"
    return f"{base} moved since SHIP (HEAD {head_short}, shipped from {from_short}) — CANCEL and SHIP again"


# ── the persisted record ─────────────────────────────────────────────────────


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_ship_file(text: str) -> dict:
    """File body → the record. Raises ValueError on a wrong envelope.

    A job a dead process left `shipping` / `syncing` loads as `failed` with the
    way forward — the worker that phase describes no longer exists.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("ship"), dict):
        raise ValueError("expected a 'ship' object")
    raw = data["ship"]
    pr = raw.get("pr")
    pr = pr if isinstance(pr, dict) and isinstance(pr.get("number"), int) else None
    last = raw.get("last") if isinstance(raw.get("last"), dict) else None
    blocking = raw.get("blocking")
    record = {
        **idle_record(last),
        "phase": raw.get("phase") if raw.get("phase") in PHASES else "idle",
        "base": _str_or_none(raw.get("base")),
        "snapshot": _str_or_none(raw.get("snapshot")),
        "branch": _str_or_none(raw.get("branch")),
        "title": _str_or_none(raw.get("title")),
        "pr": pr,
        "error": _str_or_none(raw.get("error")),
        "blocking": [p for p in blocking if isinstance(p, str)] if isinstance(blocking, list) else [],
        "files": raw.get("files") if isinstance(raw.get("files"), int) else None,
    }
    if record["phase"] == "shipping":
        record.update(phase="failed", error="the bridge restarted mid-SHIP — SHIP again, or CANCEL to clean up")
    elif record["phase"] == "syncing":
        record.update(phase="failed", error="the bridge restarted mid-SYNC — SYNC again; it resumes from wherever it stopped")
    return record


class ShipStore:
    """One record in `{version, ship}`, in the plugin's store idiom
    (`row_store.py` owns the tolerant load and the atomic write)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict:
        return load_store_file(self._path, parse_ship_file, idle_record())

    def save(self, record: dict) -> None:
        body = json.dumps({"version": STORE_FILE_VERSION, "ship": record}, indent=2, sort_keys=True)
        persist_atomically(self._path, body, kept="the ship")


# ── the controller the plugin mounts ─────────────────────────────────────────


def default_gh_problem() -> str | None:
    """`gh` missing or logged out, worded as `pr.py` words it; probed per click."""
    if shutil.which("gh") is None:
        return pr_mod.GH_MISSING_ERROR
    if not pr_mod.gh_authed():
        return pr_mod.GH_UNAUTHED_ERROR
    return None


def _default_start_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, daemon=True, name=name).start()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ShipController:
    """SHIP / SYNC / CANCEL for the main checkout, and the `ship` block.

    `self._lock` guards the record and the in-process flags; no subprocess runs
    under it. The record is persisted on every change, so a restart keeps an
    open PR's H and S — SYNC cannot work without them.
    """

    def __init__(
        self,
        *,
        main_path: Callable[[], Path | None],
        store_path: Path,
        base: str = "main",
        remote: str = pr_mod.DEFAULT_REMOTE,
        run: Runner = pr_mod._run,
        start_thread: Callable[[Callable[[], None], str], None] = _default_start_thread,
        gh_problem: Callable[[], str | None] = default_gh_problem,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
        log: Callable[[str], None] = lambda _line: None,
    ) -> None:
        self._main_path = main_path
        self._store = ShipStore(store_path)
        self._base = base
        self._remote = remote
        self._run = run
        self._start_thread = start_thread
        self._gh_problem = gh_problem
        self._clock = clock
        self._now = now
        self._log = log
        self._lock = threading.Lock()
        self._record = self._store.load()
        self._local: tuple[float, dict] | None = None
        # The PR summary's freshness: -inf, so the first poll after a restart refreshes.
        self._pr_checked = float("-inf")
        self._pr_refreshing = False
        self._cancelling = False
        self._repo_info: dict | None = None

    # -- host seams ---------------------------------------------------------
    def busy_reasons(self) -> list[str]:
        with self._lock:
            phase, pr = self._record["phase"], self._record.get("pr")
        if phase == "shipping":
            return ["shipping main"]
        if phase == "syncing":
            return [f"syncing ship PR #{pr['number']}" if pr else "syncing ship"]
        return []

    def state(self) -> tuple[int, dict]:
        path = self._main_path()
        local = self._probe(path, fresh=False)
        with self._lock:
            block = self._block(local)
            stale = (
                block["pr"] is not None
                and block["phase"] in ("open", "failed")
                and not self._pr_refreshing
                and self._clock() - self._pr_checked >= PR_TTL_SECONDS
            )
            if stale and path is not None:
                self._pr_refreshing = True
        if stale and path is not None:
            self._spawn(lambda: self._refresh_pr(path, block["pr"]["number"]), "worktree-ship-pr", on_fail=self._clear_refreshing)
        return 200, {"ok": True, "ship": block}

    # -- POST /ship ---------------------------------------------------------
    def handle_ship(self, body: dict) -> tuple[int, dict]:
        title = clean_title(body.get("title"))
        path = self._main_path()
        if path is None:
            return _refuse(503, "could not find the main checkout")
        local = self._probe(path, fresh=True)
        problem = self._gh_problem()
        with self._lock:
            if self._cancelling:
                return _refuse(409, "a CANCEL is in progress")
            refusal = decide_ship(self._record, local, base=self._base, gh_problem=problem)
            if refusal is not None:
                return refusal
            stale_branch = self._record.get("branch")  # a failed ship's orphan, if pushed
            self._record = {
                **idle_record(self._record.get("last")),
                "phase": "shipping",
                "base": local["head_full"],
                "title": title,
            }
            self._store.save(self._record)
            block = self._block(local)
        head, ahead = local["head_full"], local["ahead"]
        started = self._spawn(
            lambda: self._ship_worker(path, head, title, ahead, stale_branch), "worktree-ship", on_fail=self._fail_start
        )
        if not started:
            return _refuse(500, "could not start the SHIP job")
        return 202, {"ok": True, "ship": block}

    def _ship_worker(self, path: Path, head: str, title: str | None, ahead: int, stale_branch: str | None) -> None:
        try:
            outcome = self._ship(path, head, title, ahead, stale_branch)
        except Exception as exc:  # noqa: BLE001 — a crash must not wedge `shipping`
            outcome = {"phase": "failed", "error": f"{type(exc).__name__}: {exc}"[:ERROR_TAIL]}
        self._update(**outcome)
        pr = outcome.get("pr")
        self._log(f"  worktree: SHIP → {outcome.get('phase')} {(pr or {}).get('url') or outcome.get('error') or ''}")

    def _ship(self, path: Path, head: str, title: str | None, ahead: int, stale_branch: str | None) -> dict:
        tree, error = self._snapshot_tree(path, head)
        if tree is None:
            return {"phase": "failed", "error": error}
        code, head_tree, err = self._git(path, ["rev-parse", f"{head}^{{tree}}"])
        if code != 0:
            return {"phase": "failed", "error": tail(err, "could not read HEAD's tree")}
        if tree == head_tree and not ahead:
            return {"phase": "failed", "error": "nothing to ship — the tree matches HEAD"}

        code, merge_base, _err = self._git(path, ["merge-base", f"{self._remote}/{self._base}", head])
        diff_from = merge_base if code == 0 and merge_base else head
        code, out, err = self._git(path, ["diff", "--name-only", "--no-renames", "-z", diff_from, tree])
        if code != 0:
            return {"phase": "failed", "error": tail(err, "git diff failed")}
        paths = [name for name in out.split("\0") if name]
        title = title or default_title(paths)

        if tree == head_tree:
            snapshot = head  # only local commits to ship — an empty commit would be noise
        else:
            code, snapshot, err = self._git(path, ["commit-tree", tree, "-p", head, "-m", title, "-m", SHIP_FOOTER])
            if code != 0 or not snapshot:
                return {"phase": "failed", "error": tail(err, "git commit-tree failed")}

        if stale_branch:
            self._delete_remote_branch(path, stale_branch)
        branch = ship_branch_name(self._now().astimezone())
        # Recorded BEFORE the push: a restart after it can still CANCEL the ref.
        self._update(snapshot=snapshot, title=title, files=len(paths), branch=branch)
        code, _out, err = self._push(path, snapshot, branch)
        if code != 0 and "rejected" in err:
            branch = ship_branch_name(self._now().astimezone(), with_seconds=True)
            self._update(branch=branch)
            code, _out, err = self._push(path, snapshot, branch)
        if code != 0:
            return {"phase": "failed", "error": tail(err, "git push failed")}

        info, error = self._repo(path)
        if info is None:
            return {"phase": "failed", "error": error}
        body = build_body(paths, ahead=ahead, base=head, snapshot=snapshot)
        code, out, err = self._gh(
            path,
            [
                "pr",
                "create",
                "--repo",
                info["nameWithOwner"],
                "--base",
                self._base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
        )
        if code != 0:
            return {"phase": "failed", "error": tail(err, "gh pr create failed")}
        url = next((line for line in out.splitlines() if line.startswith("http")), None)
        summary, _err = self._view(path, url or branch)
        if summary is None:
            number = pr_number_from_url(url)
            if number is None:
                return {"phase": "failed", "error": "gh pr create printed no PR URL"}
            summary = {
                "number": number,
                "url": url,
                "state": "OPEN",
                "checks": "none",
                "mergeable": "UNKNOWN",
                "merge_commit": None,
            }
        with self._lock:
            self._pr_checked = self._clock()
        return {"phase": "open", "pr": summary, "error": None}

    def _snapshot_tree(self, path: Path, head: str) -> tuple[str | None, str]:
        """The working tree as a tree object, through a temp index (header § SHIP)."""
        with tempfile.TemporaryDirectory(prefix="worktree-ship-") as tmp:
            env = {**GIT_ENV, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            out = ""
            for argv, timeout in (
                (["git", "read-tree", head], pr_mod.GIT_TIMEOUT_SECONDS),
                (["git", "add", "-A"], SNAPSHOT_TIMEOUT_SECONDS),
                (["git", "write-tree"], pr_mod.GIT_TIMEOUT_SECONDS),
            ):
                code, out, err = self._run(argv, path, timeout, env=env)
                if code != 0:
                    return None, tail(err, f"git {argv[1]} failed")
            return (out or None), "git write-tree printed no tree"

    def _push(self, path: Path, snapshot: str, branch: str) -> tuple[int, str, str]:
        return self._run(
            ["git", "push", self._remote, f"{snapshot}:refs/heads/{branch}"], path, pr_mod.NETWORK_TIMEOUT_SECONDS, env=GIT_ENV
        )

    def _delete_remote_branch(self, path: Path, branch: str) -> None:
        """Best effort: a ship ref nobody will open a PR for. Absent is fine."""
        self._run(
            ["git", "push", self._remote, "--delete", f"refs/heads/{branch}"], path, pr_mod.NETWORK_TIMEOUT_SECONDS, env=GIT_ENV
        )

    # -- POST /ship-sync ----------------------------------------------------
    def handle_sync(self) -> tuple[int, dict]:
        path = self._main_path()
        if path is None:
            return _refuse(503, "could not find the main checkout")
        local = self._probe(path, fresh=True)
        problem = self._gh_problem()
        with self._lock:
            pr = self._record.get("pr") or {}
        if pr_blocks_sync(pr) and not problem:
            fresh, _err = self._view(path, str(pr["number"]))
            with self._lock:
                current = self._record.get("pr")
                if fresh and current and current["number"] == fresh["number"]:
                    self._record = {**self._record, "pr": fresh}
                    self._store.save(self._record)
                    self._pr_checked = self._clock()
                    pr = fresh
        landed_in_head = bool(
            pr.get("state") == "MERGED"
            and pr.get("merge_commit")
            and local.get("head_full")
            and self._is_ancestor(path, pr["merge_commit"], local["head_full"])
        )
        with self._lock:
            if self._record["phase"] == "syncing":
                return 202, {"ok": True, "ship": self._block(local)}  # the same job, already running
            if self._cancelling:
                return _refuse(409, "a CANCEL is in progress")
            refusal = decide_sync(self._record, local, base=self._base, gh_problem=problem, landed_in_head=landed_in_head)
            if refusal is not None:
                return refusal
            self._record = {**self._record, "phase": "syncing", "error": None, "blocking": []}
            self._store.save(self._record)
            block = self._block(local)
        if not self._spawn(lambda: self._sync_worker(path), "worktree-ship-sync", on_fail=self._fail_start):
            return _refuse(500, "could not start the SYNC job")
        return 202, {"ok": True, "ship": block}

    def _sync_worker(self, path: Path) -> None:
        try:
            outcome = self._sync(path)
        except Exception as exc:  # noqa: BLE001 — a crash must not wedge `syncing`
            outcome = {"phase": "failed", "error": f"{type(exc).__name__}: {exc}"[:ERROR_TAIL]}
        if outcome.get("phase") == "idle":
            with self._lock:
                self._record = outcome
                self._store.save(self._record)
                self._local = None
        else:
            self._update(**outcome)
        self._log(
            f"  worktree: SYNC → {outcome.get('phase')} {outcome.get('error') or (outcome.get('last') or {}).get('landed') or ''}"
        )

    def _sync(self, path: Path) -> dict:
        with self._lock:
            record = dict(self._record)
        number, base_sha, snapshot = record["pr"]["number"], record["base"], record["snapshot"]
        main_ref, upstream = f"refs/heads/{self._base}", f"{self._remote}/{self._base}"

        local = self._probe(path, fresh=True)
        if local.get("branch_now") != self._base:
            return {"phase": "failed", "error": f"the main checkout is not on {self._base} — switch back, then SYNC"}
        pr, error = self._view(path, str(number))
        if pr is None:
            return {"phase": "failed", "error": error}
        if pr["state"] == "CLOSED":
            return {"phase": "failed", "pr": pr, "error": f"PR #{number} was closed without merging — CANCEL to clear it"}
        if pr["state"] == "OPEN":
            # Checked BEFORE merging: a PR merged while main cannot follow it is
            # the one outcome SYNC exists to prevent.
            if local.get("head_full") not in (base_sha, snapshot):
                return {"phase": "failed", "pr": pr, "error": moved_error(self._base, local.get("head_full"), base_sha)}
            if pr["checks"] == "failing":
                return {"phase": "failed", "pr": pr, "error": f"PR #{number} checks are failing"}
            if pr["checks"] == "pending":
                return {"phase": "failed", "pr": pr, "error": checks_running_error(number)}
            if pr["mergeable"] == "CONFLICTING":
                return {"phase": "failed", "pr": pr, "error": f"PR #{number} conflicts with {self._base} — resolve it on GitHub"}
            info, error = self._repo(path)
            if info is None:
                return {"phase": "failed", "pr": pr, "error": error}
            flag = choose_merge_flag(info)
            if flag is None:
                return {"phase": "failed", "pr": pr, "error": "the repo allows no merge method (squash, merge or rebase)"}
            code, _out, err = self._gh(
                path, ["pr", "merge", str(number), flag, "--delete-branch", "--repo", info["nameWithOwner"]]
            )
            if code != 0:
                return {"phase": "failed", "pr": pr, "error": tail(err, "gh pr merge failed")}
            pr, error = self._view(path, str(number))
            if pr is None:
                return {"phase": "failed", "error": error}
            if pr["state"] != "MERGED":
                return {
                    "phase": "failed",
                    "pr": pr,
                    "error": f"PR #{number} has not merged yet ({pr['state']}) — SYNC again once it has",
                }

        landed = pr.get("merge_commit")
        if not landed:
            return {
                "phase": "failed",
                "pr": pr,
                "error": f"GitHub reported no merge commit for PR #{number} — SYNC again in a moment",
            }
        code, _out, err = self._run(
            ["git", "fetch", self._remote, f"+{main_ref}:refs/remotes/{upstream}"],
            path,
            pr_mod.NETWORK_TIMEOUT_SECONDS,
            env=GIT_ENV,
        )
        if code != 0:
            return {"phase": "failed", "pr": pr, "error": tail(err, "git fetch failed")}
        if not self._is_ancestor(path, landed, upstream):
            return {"phase": "failed", "pr": pr, "error": f"{upstream} does not have {landed[:9]} yet — SYNC again in a moment"}

        head = self._rev(path, "HEAD")
        if head and self._is_ancestor(path, landed, head):
            return self._landed(path, pr, landed)  # an earlier SYNC got this far
        if head == base_sha and base_sha != snapshot:
            code, _out, err = self._git(path, ["update-ref", "-m", "worktree ship: sync", main_ref, snapshot, base_sha])
            if code != 0:
                return {"phase": "failed", "pr": pr, "error": tail(err, moved_error(self._base, head, base_sha))}
            head = snapshot
        if head != snapshot:
            return {"phase": "failed", "pr": pr, "error": moved_error(self._base, head, base_sha)}
        code, _out, err = self._git(path, ["reset", "-q", "--mixed"])
        if code != 0:
            return {
                "phase": "failed",
                "pr": pr,
                "error": tail(err, "git reset failed") + f" — {self._base} is at the snapshot; SYNC again",
            }
        if self._rev(path, "HEAD") != snapshot:  # the last look before the checkout
            return {"phase": "failed", "pr": pr, "error": moved_error(self._base, self._rev(path, "HEAD"), base_sha)}
        code, _out, err = self._git(path, ["checkout", "--no-overwrite-ignore", "-B", self._base, upstream])
        if code != 0:
            blocking = sync_mod.parse_blocking_paths(err)
            guidance = (
                f"resolve {', '.join(blocking[:3])}"
                f"{f' (+{len(blocking) - 3} more)' if len(blocking) > 3 else ''}, "
                "then SYNC again"
                if blocking
                else "fix that, then SYNC again"
            )
            return {
                "phase": "failed",
                "pr": pr,
                "blocking": blocking,
                "error": f"{tail(err, 'git checkout refused')}\n— {self._base} is safe at the snapshot; {guidance}",
            }
        return self._landed(path, pr, landed)

    def _landed(self, path: Path, pr: dict, landed: str) -> dict:
        with self._lock:
            title = self._record.get("title")
        return idle_record(
            {
                "pr": pr["number"],
                "url": pr.get("url"),
                "title": title,
                "landed": landed,
                "at": self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "still_dirty": self._dirty_count(path),
            }
        )

    # -- POST /ship-cancel --------------------------------------------------
    def handle_cancel(self) -> tuple[int, dict]:
        """Close the PR and forget the ship. Synchronous: one bounded `gh` call
        the operator is waiting on, and the answer is whether it worked."""
        path = self._main_path()
        with self._lock:
            phase = self._record["phase"]
            if phase in IN_FLIGHT:
                return _refuse(409, f"a {'SHIP' if phase == 'shipping' else 'SYNC'} is in flight — CANCEL once it ends")
            if self._cancelling:
                return _refuse(409, "a CANCEL is already in progress")
            self._cancelling = phase != "idle"
            record = dict(self._record)
        if phase != "idle":
            try:
                refusal = self._cancel(path, record)
            finally:
                with self._lock:
                    self._cancelling = False
            if refusal is not None:
                return refusal
            with self._lock:
                self._record = idle_record(self._record.get("last"))
                self._store.save(self._record)
                self._local = None
            self._log("  worktree: SHIP cancelled")
        local = self._probe(path, fresh=False)
        with self._lock:
            return 200, {"ok": True, "ship": self._block(local)}

    def _cancel(self, path: Path | None, record: dict) -> tuple[int, dict] | None:
        if path is None:
            return _refuse(503, "could not find the main checkout")
        branch, pr = record.get("branch"), record.get("pr")
        if pr or branch:
            problem = self._gh_problem()
            if problem:
                return _refuse(503, problem)
            # A PR the record never learned about (a restart mid-SHIP) is found by branch.
            fresh, _err = self._view(path, str(pr["number"]) if pr else branch)
            pr = fresh or pr
        if pr and pr.get("state") == "MERGED":
            return _refuse(409, f"PR #{pr['number']} already merged — SYNC instead")
        if pr and pr.get("state") == "OPEN":
            info, error = self._repo(path)
            if info is None:
                return _refuse(503, error)
            code, _out, err = self._gh(
                path, ["pr", "close", str(pr["number"]), "--delete-branch", "--repo", info["nameWithOwner"]]
            )
            if code != 0:
                return _refuse(409, tail(err, f"could not close PR #{pr['number']}"))
        elif branch:
            self._delete_remote_branch(path, branch)
        return None

    # -- shared plumbing ----------------------------------------------------
    def _git(self, path: Path, args: list[str]) -> tuple[int, str, str]:
        return self._run(["git", *args], path, pr_mod.GIT_TIMEOUT_SECONDS, env=GIT_ENV)

    def _gh(self, path: Path, args: list[str]) -> tuple[int, str, str]:
        return self._run(["gh", *args], path, pr_mod.NETWORK_TIMEOUT_SECONDS, env=GH_ENV)

    def _rev(self, path: Path, rev: str) -> str | None:
        code, out, _err = self._run([*PROBE_GIT, "rev-parse", "--verify", "-q", rev], path, pr_mod.PROBE_TIMEOUT_SECONDS)
        return out if code == 0 and out else None

    def _is_ancestor(self, path: Path, ancestor: str, descendant: str) -> bool:
        code, _out, _err = self._run(
            [*PROBE_GIT, "merge-base", "--is-ancestor", ancestor, descendant], path, pr_mod.PROBE_TIMEOUT_SECONDS
        )
        return code == 0

    def _dirty_count(self, path: Path) -> int:
        code, out, _err = self._run([*PROBE_GIT, "status", "--porcelain"], path, pr_mod.GIT_TIMEOUT_SECONDS)
        return len([line for line in out.splitlines() if line.strip()]) if code == 0 else 0

    def _repo(self, path: Path) -> tuple[dict | None, str]:
        """`nameWithOwner` + the allowed merge methods, once per process."""
        if self._repo_info is not None:
            return self._repo_info, ""
        code, out, err = self._gh(
            path, ["repo", "view", "--json", "nameWithOwner,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed"]
        )
        try:
            info = json.loads(out) if code == 0 else None
        except json.JSONDecodeError:
            info = None
        if not isinstance(info, dict) or not isinstance(info.get("nameWithOwner"), str):
            return None, tail(err, "gh repo view failed")
        self._repo_info = info
        return info, ""

    def _view(self, path: Path, selector: str) -> tuple[dict | None, str]:
        info, error = self._repo(path)
        if info is None:
            return None, error
        code, out, err = self._gh(path, ["pr", "view", selector, "--repo", info["nameWithOwner"], "--json", PR_VIEW_FIELDS])
        try:
            summary = summarize_pr(json.loads(out)) if code == 0 else None
        except json.JSONDecodeError:
            summary = None
        return summary, ("" if summary else tail(err, "gh pr view failed"))

    def _refresh_pr(self, path: Path, number: int) -> None:
        try:
            summary, _err = self._view(path, str(number))
        except Exception:  # noqa: BLE001 — a poll refresh must never wedge the flag
            summary = None
        with self._lock:
            self._pr_refreshing = False
            self._pr_checked = self._clock()  # a failure backs off one TTL too
            pr = self._record.get("pr")
            if summary and pr and pr["number"] == number and self._record["phase"] in ("open", "failed"):
                self._record = {**self._record, "pr": summary}
                self._store.save(self._record)

    def _clear_refreshing(self) -> None:
        with self._lock:
            self._pr_refreshing = False

    def _probe(self, path: Path | None, *, fresh: bool) -> dict:
        """`branch_now`, `head` (+ `head_full`), `dirty`, `ahead` for the main
        checkout. `fresh` for a click — a click is a promise about now."""
        now = self._clock()
        with self._lock:
            if not fresh and self._local and now - self._local[0] < LOCAL_TTL_SECONDS:
                return self._local[1]
        local = {"branch_now": None, "head": None, "head_full": None, "dirty": 0, "ahead": 0}
        if path is not None:
            code, out, _err = self._run([*PROBE_GIT, "symbolic-ref", "--short", "-q", "HEAD"], path, pr_mod.PROBE_TIMEOUT_SECONDS)
            local["branch_now"] = out if code == 0 and out else None
            code, out, _err = self._run([*PROBE_GIT, "log", "-1", "--format=%H%n%h"], path, pr_mod.PROBE_TIMEOUT_SECONDS)
            lines = out.splitlines() if code == 0 else []
            if len(lines) == 2:
                local["head_full"], local["head"] = lines
            local["dirty"] = self._dirty_count(path)
            code, out, _err = self._run(
                [*PROBE_GIT, "rev-list", "--count", f"{self._remote}/{self._base}..HEAD"], path, pr_mod.PROBE_TIMEOUT_SECONDS
            )
            local["ahead"] = int(out) if code == 0 and out.isdigit() else 0
        with self._lock:
            self._local = (now, local)
        return local

    def _block(self, local: dict) -> dict:
        """The contract's `ship` block. Caller holds the lock."""
        record = self._record
        return {
            "phase": record["phase"],
            "branch_now": local.get("branch_now"),
            "head": local.get("head"),
            "dirty": local.get("dirty", 0),
            "ahead": local.get("ahead", 0),
            "branch": record.get("branch"),
            "base": record.get("base"),
            "snapshot": record.get("snapshot"),
            "title": record.get("title"),
            "pr": dict(record["pr"]) if record.get("pr") else None,
            "error": record.get("error"),
            "blocking": list(record.get("blocking") or []),
            "files": record.get("files"),
            "last": record.get("last"),
        }

    def _update(self, **fields) -> None:
        with self._lock:
            self._record = {**self._record, **fields}
            self._store.save(self._record)
            self._local = None

    def _spawn(self, target: Callable[[], None], name: str, *, on_fail: Callable[[], None]) -> bool:
        try:
            self._start_thread(target, name)
        except Exception:  # noqa: BLE001 — thread/FD exhaustion, mostly
            on_fail()
            return False
        return True

    def _fail_start(self) -> None:
        """A job marked in flight whose thread never started would wedge the
        phase forever — `pr.py`'s undo, for the same reason."""
        self._update(phase="failed", error="could not start the background job")
