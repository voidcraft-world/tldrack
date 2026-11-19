"""The token-free task-group supervisor: fixed heads, bounded git/gh, one worker.

Automatic publication intentionally does NOT call the manual PR button: that button
commits dirty work, whereas a completion report promises a specific validated commit.
Snapshot starts must be replaced by a worker-prepared clean publication branch.
Outcomes are read through rack/OPS polling; never send terminal input for status.

A follow-up head is pushed WITHOUT `--force`, after proving the open PR's head is its
ancestor: an agent can add commits to its PR, never rewrite what a reviewer has seen.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from worktree import pr
from worktree.ship import summarize_checks
from worktree.task_groups import (
    PR_FINISHED,
    SHA,
    batch_due,
    follow_up_due,
    follows_open_pr,
    launch_base,
    publishable,
    releasable,
    release_of,
)

#: The least time between two CI reads of one worker's PR.
CI_REFRESH_SECONDS = 60
#: One `gh pr view` — a read, so a short bound; a timeout keeps the last reading.
CI_VIEW_TIMEOUT_SECONDS = 30
CI_VIEW_FIELDS = "statusCheckRollup,headRefOid,state"
FOLLOW_UP_REFUSAL = "a follow-up must fast-forward the open PR"
QUIET = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}


def checked(command, path, timeout=pr.PROBE_TIMEOUT_SECONDS):
    code, out, err = pr._run(command, path, timeout, env=QUIET)
    if code:
        raise ValueError((err or f"{command[0]} command failed")[:500])
    return out


def publication_path(worker, managed):
    branch = worker.get("publication_branch", worker["branch"])
    row = next((r for r in managed if r["branch"] == branch), None)
    if row is None:
        raise ValueError("publication branch has no managed worktree")
    return Path(row["path"]), branch


def preflight(group, worker, managed, base):
    path, branch = publication_path(worker, managed)
    start = launch_base(group, worker)
    current = checked(["git", "symbolic-ref", "--short", "HEAD"], path)
    head = checked(["git", "rev-parse", "HEAD"], path)
    if current != branch or head != worker["head"]:
        raise ValueError("branch moved after validation; report the tested commit again")
    if checked(["git", "status", "--porcelain"], path):
        raise ValueError("publication worktree has uncommitted changes")
    remote = f"refs/remotes/origin/{base}"
    checked(["git", "rev-parse", "--verify", remote], path)
    anchor = start if branch == worker["branch"] else checked(["git", "merge-base", remote, head], path)
    code, _, _ = pr._run(["git", "merge-base", "--is-ancestor", anchor, remote], path, pr.PROBE_TIMEOUT_SECONDS)
    if code:
        raise ValueError(
            "launch base is not published on origin; prepare a clean publication branch (snapshot ancestry must stay local)"
        )
    checked(["git", "merge-base", "--is-ancestor", anchor, head], path)
    if not checked(["git", "diff", "--name-only", f"{remote}...{head}"], path):
        raise ValueError("publication branch has no changes against origin")
    if branch != worker["branch"]:
        # A new name alone cannot launder the local launch snapshot into a PR.
        code, _, _ = pr._run(["git", "merge-base", "--is-ancestor", start, head], path, pr.PROBE_TIMEOUT_SECONDS)
        published, _, _ = pr._run(["git", "merge-base", "--is-ancestor", start, remote], path, pr.PROBE_TIMEOUT_SECONDS)
        if code == 0 and published != 0:
            raise ValueError("clean publication branch still contains the local snapshot")
    return path, branch


def fast_forward(path, branch, previous, head):
    """Move the PR branch from `previous` to the tested `head`, or refuse."""
    if previous == head:
        return
    if not SHA.fullmatch(str(previous or "")):
        raise ValueError(FOLLOW_UP_REFUSAL)
    code, _, _ = pr._run(["git", "merge-base", "--is-ancestor", previous, head], path, pr.PROBE_TIMEOUT_SECONDS)
    if code:
        raise ValueError(FOLLOW_UP_REFUSAL)
    # No force: the remote refuses a non-fast-forward even if the PR moved after the read.
    checked(["git", "push", "--no-follow-tags", "origin", f"{head}:refs/heads/{branch}"], path, pr.NETWORK_TIMEOUT_SECONDS)


def publish(group, worker, managed, base):
    path, branch = preflight(group, worker, managed, base)
    raw = checked(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "url,headRefOid,headRefName,baseRefName",
        ],
        path,
        pr.NETWORK_TIMEOUT_SECONDS,
    )
    existing = json.loads(raw)
    for item in existing:
        if item.get("headRefName") == branch and item.get("baseRefName") == base:
            if item.get("headRefOid") != worker["head"]:
                # release "each" adopts an open PR it can fast-forward (one opened by hand,
                # not yet in the harbor index); a batch keeps its exact-head rule.
                if release_of(group) != "each":
                    raise ValueError("existing PR does not match the tested commit")
                fast_forward(path, branch, item.get("headRefOid"), worker["head"])
            return item["url"]
    # Push the immutable tested object, never whichever HEAD an agent writes next.
    checked(
        ["git", "push", "--no-follow-tags", "origin", f"{worker['head']}:refs/heads/{branch}"], path, pr.NETWORK_TIMEOUT_SECONDS
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md") as body:
        body.write(worker["body"])
        body.flush()
        out = checked(
            ["gh", "pr", "create", "--head", branch, "--base", base, "--title", worker["title"], "--body-file", body.name],
            path,
            pr.NETWORK_TIMEOUT_SECONDS,
        )
    url = next((line for line in out.splitlines() if line.startswith("https://github.com/")), None)
    if url is None:
        raise ValueError("PR create returned no GitHub URL; retry reconciles any existing PR")
    return url


def follow_up(group, worker, managed, base) -> dict:
    """Push a re-reported tested head onto the worker's open PR → the worker's new fields.

    The PR is read first: a branch whose PR merged may already be deleted, and a push
    would silently recreate it.
    """
    path, branch = preflight(group, worker, managed, base)
    view = json.loads(
        checked(
            ["gh", "pr", "view", worker["pr_url"], "--json", "state,headRefOid,headRefName,baseRefName"],
            path,
            pr.NETWORK_TIMEOUT_SECONDS,
        )
    )
    state = str(view.get("state") or "").lower()
    if state in PR_FINISHED:
        return {**pr_state_fields(state), "error": f"the PR is {state}; ship the follow-up as a new step"}
    if state != "open" or view.get("headRefName") != branch or view.get("baseRefName") != base:
        raise ValueError("the open PR no longer heads the publication branch")
    fast_forward(path, branch, view.get("headRefOid"), worker["head"])
    return {"pr_head": worker["head"], "pr_state": "open", "error": None}


def pr_state_fields(state: str) -> dict:
    """A PR state read from GitHub itself. CLOSED is stamped so an older index cannot
    revive it (`TaskGroups.reconcile_prs`)."""
    return {"pr_state": state, **({"closed_seen_at": time.time()} if state == "closed" else {})}


def ci_reading(view: object) -> dict | None:
    """`gh pr view --json statusCheckRollup,headRefOid,state` → the worker's CI fields."""
    if not isinstance(view, dict):
        return None
    state = str(view.get("state") or "").lower()
    if state not in ("open", *PR_FINISHED):
        return None
    reading = {"ci": summarize_checks(view.get("statusCheckRollup")), **pr_state_fields(state)}
    if SHA.fullmatch(str(view.get("headRefOid") or "")):
        reading["ci_head"] = view["headRefOid"]
    return reading


class GroupRunner:
    def __init__(self, store, managed, repo, base, log, reconcile=None, clock=time.monotonic):
        self.store, self.managed, self.repo, self.base, self.log = store, managed, repo, base, log
        self.reconcile = reconcile
        self.clock = clock
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.active = None
        # (group id, branch) → when its CI was last read. In memory: a restart reads once more.
        self.ci_read_at: dict[tuple[str, str], float] = {}

    def start(self):
        with self.lock:
            if self.thread is None:
                self.store.recover()
                self.thread = threading.Thread(target=self.run, name="worktree-task-groups", daemon=True)
                try:
                    self.thread.start()
                except Exception:
                    self.thread = None
                    raise
        self.wake.set()

    def run(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            try:
                self.tick()
            except Exception as exc:
                self.log(f"worktree task groups: {type(exc).__name__}: {exc}")

    def tick(self):
        if self.reconcile:
            self.reconcile()
        for group in self.store.list():
            if release_of(group) == "each":
                self.release_each(group)
                self.refresh_ci(group)
            else:
                self.release_batch(group)

    def fetch_base(self):
        checked(
            ["git", "fetch", "origin", f"+refs/heads/{self.base}:refs/remotes/origin/{self.base}"],
            self.repo,
            pr.NETWORK_TIMEOUT_SECONDS,
        )

    def release_batch(self, group):
        if not releasable(group) or not any(batch_due(w) for w in group["workers"]):
            return
        self.active = group["label"]
        try:
            self.fetch_base()
            managed = self.managed()
            # Validate the WHOLE group before the first outward mutation.
            for worker in group["workers"]:
                if batch_due(worker):
                    preflight(group, worker, managed, self.base)
            for worker in group["workers"]:
                if not batch_due(worker):
                    continue
                with self.store.lock:
                    latest = self.store.get(group["id"])
                    if not releasable(latest):
                        break
                    current = next(w for w in latest["workers"] if w["branch"] == worker["branch"])
                    if current != worker:
                        break
                    self.store.worker_result(group["id"], worker["branch"], pr_status="opening")
                try:
                    url = publish(group, worker, managed, self.base)
                    self.store.worker_result(
                        group["id"],
                        worker["branch"],
                        pr_url=url,
                        pr_status="open",
                        pr_state="open",
                        pr_head=worker["head"],
                        error=None,
                    )
                except Exception as exc:
                    self.store.worker_result(group["id"], worker["branch"], pr_status="error", error=str(exc)[:500])
                    break
        except Exception as exc:
            # A failed preflight gates the entire batch; only its first pending row needs the error.
            worker = next(w for w in group["workers"] if batch_due(w))
            self.store.worker_result(group["id"], worker["branch"], pr_status="error", error=str(exc)[:500])
        finally:
            self.active = None

    def release_each(self, group):
        """Publish, or follow up, every worker that is ready on its own; one failure stays its own."""
        due = [w for w in group["workers"] if publishable(group, w) or follow_up_due(group, w)]
        if not due:
            return
        self.active = group["label"]
        try:
            try:
                self.fetch_base()
                managed = self.managed()
            except Exception as exc:
                for worker in due:
                    self.store.worker_result(
                        group["id"], worker["branch"], error=str(exc)[:500], pr_status="open" if worker.get("pr_url") else "error"
                    )
                return
            for worker in due:
                self.release_one(group["id"], worker, managed)
        finally:
            self.active = None

    def release_one(self, group_id, worker, managed):
        with self.store.lock:
            latest = self.store.get(group_id)
            current = next((w for w in latest["workers"] if w["branch"] == worker["branch"]), None)
            # A report between the read and this claim re-runs on the next tick, against what it says.
            if current != worker or not (publishable(latest, current) or follow_up_due(latest, current)):
                return
            self.store.worker_result(group_id, worker["branch"], pr_status="opening")
        try:
            if worker.get("pr_url"):
                result = follow_up(latest, worker, managed, self.base)
                # A new head has new checks: read them on the next tick, not a minute later.
                self.ci_read_at.pop((group_id, worker["branch"]), None)
                self.store.worker_result(group_id, worker["branch"], pr_status="open", **result)
            else:
                url = publish(latest, worker, managed, self.base)
                self.store.worker_result(
                    group_id, worker["branch"], pr_url=url, pr_status="open", pr_state="open", pr_head=worker["head"], error=None
                )
        except Exception as exc:
            self.store.worker_result(
                group_id, worker["branch"], error=str(exc)[:500], pr_status="open" if worker.get("pr_url") else "error"
            )

    def refresh_ci(self, group):
        """Each followed PR's checks, at most once a minute; a failed read keeps the last one."""
        now = self.clock()
        failed = []
        for worker in group["workers"]:
            key = (group["id"], worker["branch"])
            if not follows_open_pr(group, worker) or worker["pr_status"] == "opening":
                self.ci_read_at.pop(key, None)
                continue
            if now - self.ci_read_at.get(key, now - CI_REFRESH_SECONDS) < CI_REFRESH_SECONDS:
                continue
            self.ci_read_at[key] = now
            try:
                reading = ci_reading(
                    json.loads(
                        checked(
                            ["gh", "pr", "view", worker["pr_url"], "--json", CI_VIEW_FIELDS], self.repo, CI_VIEW_TIMEOUT_SECONDS
                        )
                    )
                )
            except (ValueError, TypeError) as exc:
                failed.append(f"{worker['branch']}: {str(exc)[:120]}")
                continue
            if reading is not None:
                self.store.change(
                    group["id"], lambda current, w=worker, r=reading: apply_reading(current, w, r), only_if_changed=True
                )
        if failed:
            # One line per group a minute, not one per PR, when gh is down for all of them.
            self.log(f"worktree task groups: {len(failed)} CI read(s) failed in {group['label']}; {failed[0]}")


def apply_reading(group, worker, reading):
    row = next((w for w in group["workers"] if w["branch"] == worker["branch"]), None)
    # The PR moved on (a re-publication, a claim) while gh answered: this reading is not its.
    if row is None or row.get("pr_url") != worker["pr_url"] or row["pr_status"] == "opening":
        return
    row.update(reading)
