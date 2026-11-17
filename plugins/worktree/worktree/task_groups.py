"""Durable parent-owned batches. Explicit milestones, never terminal-idle inference.

A sealed membership prevents the first fast worker from releasing an unfinished launch.
SQLite commits precede side effects; PR jobs pin a validated commit and never sweep dirty
files. A restart retries only interrupted publication, using GitHub's exact-head lookup.

── Two modes, each defaulting to the original batch ──────────────────────────
`membership`: `fixed` (every lane named at create, 1–32) or `open` (0–64, grown by
`join`). `release`: `batch` (`releasable`) or `each` (`publishable`, `follow_up_due`).
A stored group without the keys reads as `fixed` / `batch`, and behaves as before.
Which caller uses which mode, and why: owned by plugins/worktree/README.md § Parent task groups — see the
note there.
"""

from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from worktree.names import validate_branch, session_name_for

STAGES = {"queued": 0, "research": 20, "implement": 50, "validate": 80, "ready": 100, "blocked": 0, "failed": 0}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,200}")
MEMBERSHIPS = ("fixed", "open")
RELEASES = ("batch", "each")
FIXED_MAX_WORKERS = 32
OPEN_MAX_WORKERS = 64
#: A PR the runner no longer follows (no follow-up push, no CI read).
PR_FINISHED = ("merged", "closed")


class TaskGroupConflict(ValueError):
    """A well-formed request this group's mode refuses (the route's 409)."""


def membership_of(group: dict) -> str:
    return group.get("membership", "fixed")


def release_of(group: dict) -> str:
    return group.get("release", "batch")


def launch_base(group: dict, worker: dict) -> str:
    """The commit a worker's branch was cut from: its own when it joined later, else the group's."""
    return worker.get("start_sha") or group["start_sha"]


def summary(group: dict) -> dict:
    workers = group["workers"]
    blockers = sum(
        w.get("pr_state") != "merged" and (w["stage"] in ("blocked", "failed") or bool(w.get("error"))) for w in workers
    )
    ready = sum(w["stage"] == "ready" for w in workers)
    opened = sum(bool(w.get("pr_url")) for w in workers)

    def delivered(w: dict) -> bool:
        return w.get("pr_state") == "merged"

    complete = all(delivered(w) or (w["stage"] == "ready" and w.get("pr_url")) for w in workers)
    # An open group starts empty; "all of nothing" is neither ready nor complete.
    percent = sum(100 if delivered(w) else STAGES[w["stage"]] for w in workers) // len(workers) if workers else 0
    return {
        **group,
        "percent": percent,
        "total": len(workers),
        "ready": ready,
        "opened": opened,
        "blockers": blockers,
        "phase": "working"
        if not workers
        else "blocked"
        if blockers
        else "complete"
        if complete
        else "opening"
        if any(w.get("pr_status") == "opening" for w in workers)
        else "ready"
        if ready == len(workers)
        else "working",
    }


def new_member(lane: object, members: list[dict]) -> dict:
    """One worker record, validated against the lanes already in its group."""
    branch = lane.get("branch") if isinstance(lane, dict) else None
    error = validate_branch(branch)
    if error:
        raise ValueError(error)
    session = session_name_for(branch)
    if any(w["session"] == session for w in members):
        raise ValueError("worker branches must have distinct session names")
    publication = lane.get("publication_branch", branch)
    approved = [w.get("approved_publication_branch", w["branch"]) for w in members]
    if validate_branch(publication) or publication in approved:
        raise ValueError("publication branches must be valid and distinct")
    if {branch, publication} & _lane_names(members):
        raise ValueError("worker and publication branches cannot overlap other lanes")
    worker = {
        "branch": branch,
        "session": session,
        "stage": "queued",
        "note": "",
        "head": None,
        "pr_url": None,
        "pr_status": "pending",
        "error": None,
        "approved_publication_branch": publication,
    }
    start = lane.get("start_sha")
    if start is not None:
        if not isinstance(start, str) or not SHA.fullmatch(start):
            raise ValueError("start_sha must be the full commit the worker branch was cut from")
        worker["start_sha"] = start
    return worker


def new_group(body: dict, parent: dict) -> dict:
    membership = body.get("membership", "fixed")
    if membership not in MEMBERSHIPS:
        raise ValueError('membership must be "fixed" or "open"')
    release = body.get("release", "batch")
    if release not in RELEASES:
        raise ValueError('release must be "batch" or "each"')
    lanes = body.get("workers", [] if membership == "open" else None)
    if membership == "fixed" and (not isinstance(lanes, list) or not 1 <= len(lanes) <= FIXED_MAX_WORKERS):
        raise ValueError(f"workers must contain 1–{FIXED_MAX_WORKERS} fixed members")
    if membership == "open" and (not isinstance(lanes, list) or len(lanes) > OPEN_MAX_WORKERS):
        raise ValueError(f"an open group starts with 0–{OPEN_MAX_WORKERS} workers")
    label = body.get("label")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise ValueError("label must contain 1–120 characters")
    start = body.get("start_sha")
    if not isinstance(start, str) or not SHA.fullmatch(start):
        raise ValueError("start_sha must be the full launch commit")
    auto = body.get("auto_pr", True)
    if not isinstance(auto, bool):
        raise ValueError("auto_pr must be boolean")
    workers: list[dict] = []
    for lane in lanes:
        workers.append(new_member(lane, workers))
    return {
        "id": str(uuid.uuid4()),
        "label": label.strip(),
        "parent_id": parent["parent_id"],
        "parent_session": parent["parent_name"],
        "start_sha": start,
        "auto_pr": auto,
        "membership": membership,
        "release": release,
        "sealed": False,
        "workers": workers,
        "updated_at": time.time(),
    }


def _lane_names(workers: list[dict]) -> set[str]:
    return {name for w in workers for name in (w["branch"], w.get("approved_publication_branch", w["branch"]))}


class TaskGroups:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        return db

    def list(self) -> list[dict]:
        with self.lock, closing(self._connect()) as db, db:
            return [summary(json.loads(row[0])) for row in db.execute("SELECT body FROM groups ORDER BY rowid")]

    def views(self) -> list[dict]:
        # PR prose is stored for publication, not resent with every rack poll.
        return [{**g, "workers": [{k: v for k, v in w.items() if k != "body"} for w in g["workers"]]} for g in self.list()]

    def reconcile_prs(self, prs: dict, base: str, fetched_at: float | None = None):
        """Reuse the harbor's GitHub index, including manual publication and merges.

        Match only an explicit publication branch or the worker's reserved namespace.
        A merge completes delivery, not an invented visual-validation report. Missing,
        ambiguous, closed, wrong-base or mismatched-head records never imply completion.

        A worker the runner's own PR read saw CLOSED is not revived by an index older
        than that read (`fetched_at`, wall clock): the stale `open` row would flip it
        back every tick until the index refreshed. A newer index that shows it reopened does.
        """
        for group in self.list():

            def update(current):
                for worker in current["workers"]:
                    if worker.get("pr_state") == "merged" or worker["pr_status"] == "opening":
                        continue
                    if (
                        worker.get("pr_state") == "closed"
                        and worker.get("closed_seen_at")
                        and (fetched_at is None or fetched_at <= worker["closed_seen_at"])
                    ):
                        continue
                    explicit = worker.get("publication_branch")
                    if explicit == worker["branch"] and not worker.get("pr_url"):
                        explicit = None
                    approved = worker.get("approved_publication_branch", worker["branch"])
                    prefix = f"pr/{current['id'][:8]}/{worker['session']}/"
                    candidates = [
                        (branch, row)
                        for branch, row in prs.items()
                        if (branch == explicit if explicit else branch == approved or branch.startswith(prefix))
                        and row.get("base_ref") == base
                        and row.get("state") in ("open", "merged")
                        and not row.get("draft")
                        and row.get("url")
                        and SHA.fullmatch(str(row.get("head_oid", "")))
                    ]
                    if len(candidates) != 1:
                        continue
                    branch, row = candidates[0]
                    if worker.get("head") and worker["head"] != row["head_oid"]:
                        continue
                    worker.update(
                        publication_branch=branch,
                        pr_url=row["url"],
                        pr_status="open",
                        pr_state=row["state"],
                        pr_head=row["head_oid"],
                    )
                    if row["state"] == "merged":
                        worker["error"] = None

            self.change(group["id"], update, only_if_changed=True)

    def get(self, group_id: str) -> dict:
        with self.lock, closing(self._connect()) as db, db:
            return self._read(db, group_id)

    @staticmethod
    def _read(db, group_id):
        row = db.execute("SELECT body FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            raise ValueError("unknown task group")
        return json.loads(row[0])

    def create(self, group: dict) -> dict:
        with self.lock, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._refuse_claimed(db, _lane_names(group["workers"]))
            db.execute("INSERT INTO groups VALUES (?, ?)", (group["id"], json.dumps(group)))
        return summary(group)

    @staticmethod
    def _refuse_claimed(db, branches: set[str], own_group: str | None = None):
        """A branch belongs to one group at most, or two runners could publish it."""
        for row in db.execute("SELECT body FROM groups"):
            previous = json.loads(row[0])
            if previous["id"] != own_group and branches & _lane_names(previous["workers"]):
                raise ValueError(f"worker branch already belongs to task group {previous['id']}; resume that group")

    def join(self, group_id: str, lane: dict) -> dict:
        """Add one worker to an OPEN group. An exact repeat is a no-op, so a caller
        that crashed between the join and its own record can simply ask again."""
        with self.lock, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            group = self._read(db, group_id)
            if membership_of(group) != "open":
                raise TaskGroupConflict("task group membership is fixed; only an open group takes new workers")
            branch = lane.get("branch") if isinstance(lane, dict) else None
            member = next((w for w in group["workers"] if w["branch"] == branch), None)
            if member is not None:
                same = lane.get("publication_branch", branch) == member.get("approved_publication_branch", branch) and lane.get(
                    "start_sha", member.get("start_sha")
                ) == member.get("start_sha")
                if not same:
                    raise TaskGroupConflict("branch already joined this group with a different publication branch or start")
                return summary(group)
            if group["sealed"]:
                raise TaskGroupConflict("task group membership is sealed")
            if len(group["workers"]) >= OPEN_MAX_WORKERS:
                raise TaskGroupConflict(f"an open group holds at most {OPEN_MAX_WORKERS} workers")
            worker = new_member(lane, group["workers"])
            self._refuse_claimed(db, _lane_names([worker]), own_group=group_id)
            group["workers"].append(worker)
            group["updated_at"] = time.time()
            db.execute("UPDATE groups SET body = ? WHERE id = ?", (json.dumps(group), group_id))
        return summary(group)

    def change(self, group_id: str, mutate, *, only_if_changed=False) -> dict:
        with self.lock, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            group = self._read(db, group_id)
            before = json.dumps(group) if only_if_changed else None
            mutate(group)
            if only_if_changed and json.dumps(group) == before:
                return summary(group)
            group["updated_at"] = time.time()
            db.execute("UPDATE groups SET body = ? WHERE id = ?", (json.dumps(group), group_id))
        return summary(group)

    def report(self, group_id: str, body: dict) -> dict:
        stage = body.get("stage")
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError("unknown worker milestone")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("note must be at most 1000 characters")
        if stage in ("blocked", "failed") and not note.strip():
            raise ValueError("a blocker needs an explanation")
        if stage == "ready":
            if body.get("validated") is not True or not SHA.fullmatch(str(body.get("head", ""))):
                raise ValueError("ready requires validated=true and a full tested commit")
            for field, limit in (("title", 200), ("body", 12000)):
                if not isinstance(body.get(field), str) or not 1 <= len(body[field].strip()) <= limit:
                    raise ValueError(f"ready requires a nonempty PR {field} (max {limit})")

        def update(group):
            worker = next((w for w in group["workers"] if w["branch"] == body.get("branch")), None)
            if worker is None:
                raise ValueError("branch is not a member of this group")
            if worker["pr_status"] == "opening" or worker.get("pr_state") == "merged":
                raise ValueError("publication is already claimed; worker report is frozen")
            publication = body.get(
                "publication_branch",
                worker.get("publication_branch", worker.get("approved_publication_branch", worker["branch"])),
            )
            # A snapshot worker may prepare its own clean, task-specific publication branch.
            prefix = f"pr/{group['id'][:8]}/{worker['session']}/"
            if validate_branch(publication) or (
                publication != worker.get("approved_publication_branch", worker["branch"]) and not publication.startswith(prefix)
            ):
                raise ValueError(f"publication branch must be the worker branch or start with {prefix}")
            if worker.get("pr_url") and publication != worker.get("publication_branch"):
                raise ValueError("published worker cannot switch publication branches")
            worker.update(
                stage=stage,
                note=note,
                error=None,
                pr_status="open" if worker.get("pr_url") else "pending",
                publication_branch=publication,
            )
            if stage == "ready":
                worker.update(head=body["head"], title=body["title"], body=body["body"], publication_branch=publication)

        return self.change(group_id, update)

    def control(self, group_id: str, action: str) -> dict:
        def update(group):
            if action == "seal":
                group["sealed"] = True
            elif action in ("pause", "resume"):
                group["auto_pr"] = action == "resume"
            elif action == "retry":
                for worker in group["workers"]:
                    if worker.get("error") and not worker.get("pr_url"):
                        worker.update(error=None, pr_status="pending")
                    elif worker.get("error") and follows_open_pr(group, worker):
                        worker.update(error=None, pr_status="open")
            else:
                raise ValueError("unknown task-group action")

        return self.change(group_id, update)

    def worker_result(self, group_id: str, branch: str, **values):
        return self.change(group_id, lambda g: next(w for w in g["workers"] if w["branch"] == branch).update(values))

    def recover(self):
        for group in self.list():
            for worker in group["workers"]:
                if worker["pr_status"] == "opening":
                    # An interrupted follow-up still has its PR; only a first publication is pending.
                    self.worker_result(group["id"], worker["branch"], pr_status="open" if worker.get("pr_url") else "pending")


def releasable(group: dict) -> bool:
    """release "batch": the whole sealed group, at once."""
    return bool(
        group["sealed"] and group["auto_pr"] and all(w["stage"] == "ready" and not w.get("error") for w in group["workers"])
    )


def follows_open_pr(group: dict, worker: dict) -> bool:
    """release "each" keeps following a worker's PR until it merges or closes."""
    return release_of(group) == "each" and bool(worker.get("pr_url")) and worker.get("pr_state") not in PR_FINISHED


def _settled_ready(group: dict, worker: dict) -> bool:
    return (
        release_of(group) == "each"
        and group["auto_pr"]
        and worker["stage"] == "ready"
        and not worker.get("error")
        and worker["pr_status"] != "opening"
    )


def publishable(group: dict, worker: dict) -> bool:
    """release "each": this worker's first PR is due. Siblings and the seal do not hold it."""
    return _settled_ready(group, worker) and not worker.get("pr_url") and worker.get("pr_state") != "merged"


def follow_up_due(group: dict, worker: dict) -> bool:
    """release "each": the worker re-reported a tested head its open PR does not carry yet."""
    return _settled_ready(group, worker) and follows_open_pr(group, worker) and worker.get("head") != worker.get("pr_head")
