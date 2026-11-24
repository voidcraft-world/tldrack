"""The plugin object the bridge mounts: GET /state and /branches; POST /create,
/pr, /remove, /cleanup, /fetch, /delete-branch, /prune-worktrees and
/remove-landed-worktree (plus the brief / adopt / catch-up / scuttle / archive
routes below), and the main
checkout's SHIP routes — GET /ship-state, POST /ship, /ship-sync, /ship-cancel,
whose safety argument is owned by `worktree/ship.py` — see the note there.

Route handlers return `(status, dict)` per the host contract
(plugins/README.md). The host already applied the Origin guard to POSTs
and parsed any JSON body — every route here ACTS on the machine, and they
inherit that guard rather than carrying weaker ones of their own.

── The shape of the interaction ───────────────────────────────────────────
`GET /state` is what the front end polls. It runs one `git worktree list` and
one `git for-each-ref` (every branch's ahead/behind at once), behind a short TTL
cache so several rack windows (the page, a pop-out, a phone, a second browser)
cost one computation rather than four. `dirty` is overlaid from the background
dirty tier and never probed on the request thread — `dirty.py` owns why.

It deliberately does NOT ask tmux anything. The session name is a pure function
of the branch, and every client polling this is ALREADY polling `/snapshot`,
which carries live state for every session on the machine. Probing `tmux
has-session` per worktree here would be a second, slower, racier answer to a
question the caller can join locally for free.

NONE of `POST /create`, `/pr` and `/cleanup` does its work on the request
thread. Each validates synchronously, marks the branch, answers 202, and
finishes in a worker that reports through `/state` — the shape
`coordinator/plugin.py::_handle_audit` uses.

`/create` used to be synchronous on the theory that `tmux new-session -d` returns
as soon as the pane is forked. True, and beside the point: the spawn is the LAST
of three steps, behind `git worktree add` and a bootstrap that runs a `tsc` emit.
Their budgets total 435s, while the browser client gives up at 90s — so a slow
bootstrap reported failure for work that had actually succeeded, and until then
the rack showed nothing at all. Every caller polls `/state` already; the steps
belong there, where all four rack surfaces can see them rather than only the one
that clicked.

── The harbor routes (Branch Harbor) ──────────────────────────────────────
`GET /branches` is EVERY local branch and EVERY worktree — the 21 trees Claude
Code owns under `~/other-tool/worktrees/` included — read unfiltered and stamped
`managed` / `is_main`. `_managed()` is untouched by that: it is the WRITE gate
for `/pr` and `/remove`, and a read that lists more must never widen what a
mutation can reach. The document comes from `harbor.HarborCache` (a GET never
runs git; `harbor.py` carries why), and `pr_job` / create progress are merged
AFTER the cache with spreads, exactly as `_merge_creates` does for `/state`.

`POST /delete-branch` re-sweeps INLINE at click time — never the cached
document — because a click is a promise about now, and `branches.decide_delete`
is the only thing that decides. `POST /fetch` is the one network route, 202 +
worker like `/pr`. Every path in a harbor GET body is `~`-collapsed
(`names.display_path`), and so is every error STRING in it (`names.scrub_home`,
applied in `_branches`).
"""

from __future__ import annotations

import functools
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from worktree import absorption as absorption_mod
from worktree import hook_trust
from worktree import adopt as adopt_mod
from worktree import archive as archive_mod
from worktree import branches as branches_mod
from worktree import changes as changes_mod
from worktree import create as create_mod
from worktree import brief as brief_mod
from worktree import landing as landing_mod
from worktree import pr as pr_mod
from worktree import ship as ship_mod
from worktree.task_groups import TaskGroupConflict, TaskGroups, new_group
from worktree.task_group_runner import GroupRunner
from worktree.dirty import DirtyTier
from worktree.harbor import (
    BRANCHES_TTL_SECONDS,
    DIRTY_SWEEP_BUDGET_SECONDS,
    DIRTY_TTL_SECONDS,
    FETCH_STEPS,
    PR_FETCH_HEADS_MAX,
    PR_INDEX_MIN_INTERVAL_SECONDS,
    PR_INDEX_STALE_SECONDS,
    PR_INDEX_TTL_SECONDS,
    PR_OPEN_WATCH_SECONDS,
    BriefJobs,
    BriefStore,
    FetchJobs,
    HarborCache,
    PrIndexStore,
)
from worktree.listing import list_worktrees, main_worktree_path, worktree_by_branch
from worktree.names import (
    display_path,
    is_managed_path,
    is_primary_checkout,
    scrub_home,
    session_name_for,
    validate_branch,
    validate_existing_branch,
    validate_session_name,
    worktree_path_for,
)
from worktree.repo import RepoSpec, start_dir_spec
from worktree.store import (
    TASK_GROUPS_FILE_NAME,
    AdoptStore,
    CleanupStore,
    CreateStore,
    PrStore,
    resolve_adopt_path,
    resolve_brief_path,
    resolve_pr_index_path,
    resolve_ship_path,
)

PLUGIN_NAME = "worktree"

#: The `profile` meaning "the freest account" — tldrack `profiles.py`
#: owns the id; a plugin cannot import the host, so this is its copy.
AUTO_PROFILE_ID = "auto"

#: `GET /state` is polled by up to four surfaces at once. Slightly under the
#: rack's 5 s preview cadence, so a normal poll recomputes but a burst does not.
STATE_TTL_SECONDS = 4.0

#: Every verb that touches a tree is gated by `_managed()`; this is its 404.
NO_MANAGED_REASON = "no managed worktree for that branch"

#: A `/state` row's landing when nothing could be judged — unknown, never "no".
UNJUDGED = {"landed": None, "landing": None}

#: What the task-group routes take beyond their first shape, on `/state` and `/groups`.
GROUP_CAPABILITIES = {"task_group_modes": True}


class WorktreePlugin:
    """One repository's worktrees. The bridge mounts `repos.RepoRouter`, which
    holds one of these per configured repository; built alone (the tests, a
    host that mounts it directly) it serves the repository the bridge starts in."""

    name = PLUGIN_NAME
    max_body_bytes = 65536

    def __init__(self, context, repo: RepoSpec | None = None) -> None:
        self._context = context
        self._repo = repo or start_dir_spec(context)
        store_path = self._repo.store_path
        self._store = PrStore(store_path)
        # Create-job progress. `CreateStore.claim` is a test-and-set, so it IS
        # the mutual exclusion for `/create` — there is deliberately no lock
        # held across `git worktree add` + bootstrap + spawn any more. One used
        # to be, and it made a second click block for the whole create.
        self._creates = CreateStore()
        # Cleanup-job progress (`POST /cleanup`) — the same test-and-set shape.
        self._cleanups = CleanupStore()
        self._cache_lock = threading.Lock()
        # (taken, git rows, non-main trees, dirty probe list, every local ref).
        self._cache: tuple[float, list[dict], list[dict], list[str], list[dict]] | None = None
        # `/state`'s landing overlay, keyed on (cache fill, PR index fetch) — `_state_landing`.
        self._landing_memo: tuple[tuple, dict[str, dict]] | None = None
        self._repo_root = self._repo.root
        self._root = self._repo.worktree_root
        # Base branch, bootstrap hook, sparse paths and worktree root come from
        # `repo.py`, which `wt` (the CLI) and the `WorktreeCreate` hook read too:
        # one answer for every door.
        self._base = self._repo.base
        self._bootstrap = self._repo.hooks.bootstrap
        self._sparse = self._repo.hooks.sparse_exclude
        # The harbor. The PR index is the one persisted piece; the fetch job
        # and the document cache live and die with the process (harbor.py).
        self._pr_index = PrIndexStore(resolve_pr_index_path(store_path))
        # `landed`'s ancestry half, cached per (PR head, tip) — branches.py.
        self._ancestry = branches_mod.AncestryCache(self._repo_root)
        self._fetch_jobs = FetchJobs()
        # Secondary remotes FETCH could not reach, joined into `remote_error`.
        # One writer (the fetch worker) and one reader (the harbor runner), so
        # a plain rebind is the whole synchronisation it needs.
        self._remote_fetch_error: str | None = None
        self._fetched_label = pr_mod.DEFAULT_REMOTE
        # Adopt anchors: which branches have had their uncommitted work
        # rescued onto this checkout, and the tree that proves it (adopt.py).
        self._adopts = AdoptStore(resolve_adopt_path(store_path))
        # Briefs: the persisted entries and the one job (brief.py).
        self._briefs = BriefStore(resolve_brief_path(store_path))
        self._brief_jobs = BriefJobs()
        self._harbor = HarborCache(self._harbor_runner, BRANCHES_TTL_SECONDS)
        # `git status` per worktree, for `/state` AND the harbor — one tier, swept
        # off every request thread (dirty.py). The clock is an attribute so a
        # test can drive the TTL without sleeping; the tier reads it through
        # the lambda, so a test's replacement reaches it.
        self._clock = time.monotonic
        self._dirty = DirtyTier(
            sweep=lambda: branches_mod.dirty_sweep,
            ttl_seconds=DIRTY_TTL_SECONDS,
            budget_seconds=DIRTY_SWEEP_BUDGET_SECONDS,
            start_thread=lambda target: self._start_background(target, "worktree-dirty"),
            clock=lambda: self._clock(),
            # `/state` overlays the tier at read time and needs no drop; the
            # harbor document embeds it.
            on_swept=lambda: self._harbor.invalidate(),
            log=context.log,
        )
        # Who is standing in each worktree (`landing.sweep_occupants`), on the
        # dirty tier's TTL — it costs one `lsof` (~1.4 s), not one per tree.
        self._occupants: dict[str, list[str] | None] = {}
        self._occupants_at: float | None = None
        # The content proofs (landing.py): squash / rebase / shipped-in-another-
        # PR, read by every verdict, filled by a background worker.
        self._content = landing_mod.ContentTier(
            self._repo_root,
            landing_mod.PatchIdStore(landing_mod.resolve_patch_id_path(resolve_pr_index_path(store_path))),
            start_thread=lambda target: self._start_background(target, "worktree-content"),
            on_ready=lambda: self._harbor.invalidate(),
        )
        # The evidence tier (absorption.py): how much of an UNPROVEN branch the
        # base already holds. Never a proof; DELETE ABSORBED is what reads it.
        self._absorption = absorption_mod.AbsorptionTier(
            self._repo_root,
            start_thread=lambda target: self._start_background(target, "worktree-absorption"),
            on_ready=lambda: self._harbor.invalidate(),
        )
        # `contained_in`, keyed by every local (branch, tip): any tip moving
        # can change who holds whom, and nothing else can.
        self._containment: tuple[frozenset, dict[str, str] | None] | None = None
        # The PR index's own refresh (`_maybe_refresh_prs`): single flight, and
        # a monotonic back-off after a failure.
        self._pr_refresh_lock = threading.Lock()
        self._pr_refreshing = False
        self._pr_refresh_after = 0.0
        # The open-PR watch's next turn (`_maybe_watch_open_prs`), on the same clock.
        self._pr_watch_after = 0.0
        # Branches whose `/pr` worker is running in THIS process. `PrStore`'s
        # `opening` cannot answer that: it is persisted, so it outlives the
        # worker a restart kills.
        self._pr_jobs_lock = threading.Lock()
        self._pr_jobs: set[str] = set()
        # SHIP for the main checkout — its own record, jobs and refusals (ship.py).
        self._ship = ship_mod.ShipController(
            main_path=self._main_checkout,
            store_path=resolve_ship_path(store_path),
            base=self._base,
            start_thread=self._start_background,
            log=context.log,
        )

        self._groups = TaskGroups(store_path.with_name(TASK_GROUPS_FILE_NAME))
        self._group_runner = GroupRunner(
            self._groups, self._managed, self._repo_root, self._base, context.log, self._sync_group_prs
        )
        if self._groups.path.exists():
            self._group_runner.start()

    def routes(self) -> list[str]:
        base = f"/plugin/{self.name}"
        return [
            f"{base}/groups",
            f"{base}/group-create",
            f"{base}/group-join",
            f"{base}/group-report",
            f"{base}/group-control",
            f"{base}/state",
            f"{base}/create",
            f"{base}/pr",
            f"{base}/remove",
            f"{base}/cleanup",
            f"{base}/branches",
            f"{base}/changes",
            f"{base}/fetch",
            f"{base}/delete-branch",
            f"{base}/prune-worktrees",
            f"{base}/brief",
            f"{base}/adopt-plan",
            f"{base}/adopt",
            f"{base}/catch-up",
            f"{base}/scuttle-remote",
            f"{base}/remove-landed-worktree",
            f"{base}/ship-state",
            f"{base}/ship",
            f"{base}/ship-sync",
            f"{base}/ship-cancel",
            f"{base}/restore-branch",
            f"{base}/forget-archived",
            f"{base}/trust-hook",
        ]

    def busy_reasons(self) -> list[str]:
        """The in-process jobs a bridge restart would kill — the host's busy
        seam: owned by bridge/tldrack/freshness.py — see the
        note there. Lock-and-read only."""
        reasons = [
            f"creating worktree {branch}"
            for branch, row in sorted(self._creates.snapshot().items())
            if row["status"] == "creating"
        ]
        reasons += [
            f"cleaning up worktree {branch}"
            for branch, row in sorted(self._cleanups.snapshot().items())
            if row["status"] == CleanupStore.running
        ]
        with self._pr_jobs_lock:
            reasons += [f"opening PR {branch}" for branch in sorted(self._pr_jobs)]
        fetch = self._fetch_jobs.get()
        if fetch and fetch["status"] == "running":
            reasons.append("fetching remotes")
        brief = self._brief_jobs.get()
        if brief and brief["status"] == "running":
            reasons.append(f"writing branch briefs ({brief['done']}/{brief['total']})")
        if self._group_runner.active:
            reasons.append(f"publishing task group {self._group_runner.active}")
        return reasons + self._ship.busy_reasons()

    # -- GET ----------------------------------------------------------------
    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if subpath == "/groups":
            # The flags ride here too, so an in-process caller checks them without a `/state`.
            return 200, {"groups": self._groups.views(), "capabilities": GROUP_CAPABILITIES}
        if subpath == "/state":
            return 200, self._state()
        if subpath == "/branches":
            return 200, self._branches()
        if subpath == "/changes":
            return self._handle_changes(query)
        if subpath == "/adopt-plan":
            return self._handle_adopt_plan(query)
        if subpath == "/ship-state":
            return self._ship.state()
        return 404, {"error": "unknown route — GET /state, /branches, /changes, /adopt-plan or /ship-state"}

    # -- POST ---------------------------------------------------------------
    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        payload = body if isinstance(body, dict) else {}
        if subpath in ("/group-create", "/group-join", "/group-report", "/group-control"):
            return self._handle_group(subpath, payload)
        if subpath == "/create":
            return self._handle_create(query, payload)
        if subpath == "/pr":
            return self._handle_pr(query, payload)
        if subpath == "/remove":
            return self._handle_remove(query, payload)
        if subpath == "/cleanup":
            return self._handle_cleanup(query, payload)
        if subpath == "/fetch":
            return self._handle_fetch()
        if subpath == "/delete-branch":
            return self._handle_delete_branch(query, payload)
        if subpath == "/prune-worktrees":
            return self._handle_prune_worktrees()
        if subpath == "/brief":
            return self._handle_brief(query, payload)
        if subpath == "/adopt":
            return self._handle_adopt(query, payload)
        if subpath == "/catch-up":
            return self._handle_catch_up(query, payload)
        if subpath == "/scuttle-remote":
            return self._handle_scuttle_remote(query, payload)
        if subpath == "/remove-landed-worktree":
            return self._handle_remove_landed_worktree(query, payload)
        if subpath == "/restore-branch":
            return self._handle_restore_branch(query, payload)
        if subpath == "/forget-archived":
            return self._handle_forget_archived(query, payload)
        if subpath == "/ship":
            return self._ship.handle_ship(payload)
        if subpath == "/ship-sync":
            return self._ship.handle_sync()
        if subpath == "/ship-cancel":
            return self._ship.handle_cancel()
        if subpath == "/trust-hook":
            return self._handle_trust_hook(payload)
        return 404, {
            "error": "unknown route — POST /create, /pr, /remove, /cleanup, /fetch, "
            "/delete-branch, /prune-worktrees, /brief, /adopt, "
            "/catch-up, /scuttle-remote, /remove-landed-worktree, "
            "/restore-branch, /forget-archived, "
            "/ship, /ship-sync, /ship-cancel or /trust-hook"
        }

    def _handle_trust_hook(self, body: dict) -> tuple:
        """`POST /trust-hook {sha256}`: pin this repository's bootstrap hook, by
        the digest the operator was shown (`hook_trust.py` owns why)."""
        error = hook_trust.pin(self._repo_root, self._bootstrap, body.get("sha256"))
        if error is not None:
            return (404 if "no bootstrap hook" in error else 409), {"error": error}
        self._context.log(f"  worktree: {self._repo.label}: bootstrap hook trusted ({body['sha256'][:12]}…)")
        return 200, {"trusted": True, "sha256": body["sha256"], "repo_key": self._repo.key}

    # -- state --------------------------------------------------------------
    def _sync_group_prs(self):
        """The group runner's reconcile: each worker's own PR from the index, then its branch's landing
        (`task_groups.py` owns why either delivers). The landing half is `/state`'s own verdict and cache,
        and is asked only while a worker waits on it."""
        self._maybe_refresh_prs(branches_mod.fetch_head_mtime(self._repo_root))
        fetched_at, prs = self._pr_index.snapshot()
        self._groups.reconcile_prs(prs, self._base, fetched_at)
        if self._groups.awaiting_delivery():
            taken, _rows, _trees, _probe, refs = self._git_snapshot()
            self._groups.reconcile_landing(own_landings(self._state_landing(taken, refs, fetched_at)))

    def _managed(self, records: list[dict] | None = None) -> list[dict]:
        """Every worktree of OURS, newest git order preserved.

        Filtered by `is_managed_path`, which is the guard that keeps the 20+
        `~/other-tool/worktrees/` trees Claude Code owns out of this list — and,
        far more importantly, out of `POST /remove`'s reach. `records` is a
        listing the caller already holds; absent, it lists afresh.
        """
        rows = []
        for record in list_worktrees(self._repo_root) if records is None else records:
            if not is_managed_path(record.get("path"), self._root):
                continue
            if not record.get("branch"):
                continue  # detached HEAD — not something this plugin created
            rows.append(record)
        return rows

    def _state(self) -> dict:
        """The polled view.

        The TTL cache holds the GIT rows only, and every live overlay — `dirty`
        from the dirty tier, `landed` from the PR index and the content tier,
        create and cleanup progress from the job stores — is applied after it
        is read, so a step transition, a finished sweep or a fresh PR index is
        visible on the very next poll while the git part (one `worktree list`,
        one `for-each-ref`) still runs once every four seconds. Caching the
        merged list instead would give a four-step ladder a four-second cache
        in front of it, which is most of the latency this route exists to remove.
        """
        taken, rows, worktree_trees, probe, refs = self._git_snapshot()
        dirty = self._dirty.read(probe)
        # Each tree's PR, live like `dirty`: the rack's island turns from "no PR yet" to "PR handled" by it
        # (`tree_pr`). The index refreshes itself while someone polls, here as from the harbor; not due is a
        # stat and a compare. The trees' branches are the ones whose open PRs it watches for a merge.
        self._maybe_refresh_prs(
            branches_mod.fetch_head_mtime(self._repo_root), [tree["branch"] for tree in worktree_trees if tree["branch"]]
        )
        fetched_at, prs = self._pr_index.snapshot()
        landing = self._state_landing(taken, refs, fetched_at)
        cleanups = self._cleanups.snapshot()
        # Rebuilt with a spread, never mutated: the cached rows are read again
        # on every poll for the next four seconds.
        rows = [
            {
                **row,
                "dirty": dirty.get(row["path"]),
                **landing.get(row["branch"], UNJUDGED),
                **_cleanup_fields(cleanups.get(row["branch"])),
            }
            for row in rows
        ]
        # ADDITIVE: `landing` (`{how, pr}`, or None) is the harbor's verdict on the tree's branch, the same
        # one its `worktrees` row carries. The island reads it first: a branch that landed inside another
        # branch's PR is handled whatever its own PR says (`rack-view.ts::island_pr`).
        trees = [
            {
                **tree,
                "pr": tree_pr(prs.get(tree["branch"] or ""), self._store.get(tree["branch"] or "")),
                "landing": landing.get(tree["branch"] or "", UNJUDGED)["landing"],
            }
            for tree in worktree_trees
        ]
        return {
            "worktrees": self._merge_creates(rows),
            "task_groups": self._groups.views(),
            "root": str(self._root),
            "base": self._base,
            # Which repository this is: `/repos` lists them, and `repo` on any request picks one.
            "repo_key": self._repo.key,
            # `worktree_paths` stays for a client that predates the trees.
            "worktree_paths": [tree["path"] for tree in worktree_trees],
            "worktree_trees": trees,
            # False until the PR index has been fetched once: a tree's `pr: null` then means "unknown",
            # never "no PR".
            "pr_known": fetched_at is not None,
            # What `/create` accepts beyond its original shape, so a caller can
            # refuse BEFORE creating against a plugin that would ignore it.
            # `cleanup`: rows carry `landed` / `landing` / `cleanup_status`, and `POST /cleanup` exists.
            # `task_group_modes`: `/group-create` takes `membership` / `release` and `/group-join`
            # exists. `create_respawn`: `/create {existing}` re-enters a managed tree whose session died.
            "capabilities": {
                "create_base": True,
                "task_groups": True,
                "cleanup": True,
                # `gh` missing: every PR verb refuses, so a client hides them.
                "gh": gh_found(),
                "repos": True,
                **GROUP_CAPABILITIES,
                "create_respawn": True,
                "session_lineage": hasattr(getattr(self._context, "profiles", None), "prepare_lineage"),
                # The `/create {permission_mode}` values this host can start (`_permission_mode_for`).
                "permission_modes": self._permission_modes() or [],
                # `/create {model}` is honoured (`_model_for`).
                "launch_models": hasattr(getattr(self._context, "profiles", None), "model_refusal"),
            },
        }

    def _handle_group(self, route: str, body: dict) -> tuple:
        try:
            if route == "/group-create":
                profiles = getattr(self._context, "profiles", None)
                if profiles is None or not hasattr(profiles, "prepare_lineage"):
                    return 503, {"error": "session lineage is unavailable"}
                if not body.get("parent_session"):
                    raise ValueError("task groups require an explicit parent_session")
                parent = profiles.prepare_lineage(body["parent_session"])
                group = new_group(body, parent)
                group = self._groups.create(group)
            elif route == "/group-join":
                group = self._groups.join(body.get("id", ""), body)
            elif route == "/group-report":
                group = self._groups.report(body.get("id", ""), body)
            else:
                group = self._groups.control(body.get("id", ""), body.get("action"))
            self._group_runner.start()
            return 200, {"group": group}
        except TaskGroupConflict as exc:
            return 409, {"error": str(exc)}
        except (ValueError, TypeError) as exc:
            return 400, {"error": str(exc)}

    def _git_snapshot(self) -> tuple:
        """`(taken, rows, trees, probe, refs)`: `/state`'s git half, cached for `STATE_TTL_SECONDS`. The group
        runner reads the same snapshot, so its landing and the island's can never disagree."""
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache if self._cache and now - self._cache[0] < STATE_TTL_SECONDS else None
        if cached is None:
            records = list_worktrees(self._repo_root)
            # ADDITIVE: every non-main worktree on the machine, `~`-collapsed and
            # deliberately unfiltered — the rack's attach-only set, so a session
            # whose cwd is ANY worktree is focused with `attach:` (README § THE
            # INVARIANT) rather than only the ones this plugin created. Read off
            # the same listing `_managed` filters: that one is the write gate,
            # and it stays exactly as it is.
            # The sweep list is the harbor's (every live tree, not only ours),
            # so one sweep answers both routes.
            probe = [r["path"] for r in records if r.get("path") and not r.get("prunable")]
            managed = self._managed(records)
            trees = _non_main_trees(records)
            refs = self._state_refs(bool(trees) or self._groups.awaiting_delivery())
            cached = (now, self._git_rows(managed, refs), trees, probe, refs)
            with self._cache_lock:
                self._cache = cached
        return cached

    def _state_refs(self, wanted: bool) -> list[dict]:
        """EVERY local branch's ref row, from ONE `for-each-ref` — the harbor's
        own ladder. `/state` reads it for each managed tree's ahead/behind, and
        for `landed`, judged over all of them (`_prove_landed` owns why), on its
        rows, its trees and its groups' workers. `[]` with none of those to
        answer for (`wanted`), or when the sweep failed."""
        if not wanted:
            return []
        refs, _capabilities, _error = branches_mod.sweep_refs(self._repo_root, self._base)
        return refs

    def _git_rows(self, records: list[dict], refs: list[dict]) -> list[dict]:
        """The cacheable half of each `/state` row: branch, path, ahead/behind,
        PR outcome. `dirty` and `landed` are overlaid per read and not decided
        here. A branch `refs` could not count (git < 2.41, an unparsable field,
        a failed sweep) falls back to its own `rev-list`."""
        counts = {
            ref["branch"]: (ref["ahead"], ref["behind"])
            for ref in refs
            if isinstance(ref.get("ahead"), int) and isinstance(ref.get("behind"), int)
        }
        rows = []
        for record in records:
            branch = record["branch"]
            path = Path(record["path"])
            ahead, behind = counts.get(branch) or pr_mod.ahead_behind(path, self._base)
            row = {
                "branch": branch,
                "path": str(path),
                # Derived, never stored. The caller joins this against the
                # `/snapshot` it is already polling to learn whether it is live.
                "session": session_name_for(branch),
                "dirty": None,
                "ahead": ahead,
                "behind": behind,
                "pr_status": None,
                "pr_url": None,
                "pr_error": None,
            }
            stored = self._store.get(branch)
            if stored:
                row["pr_status"] = stored.get("status")
                row["pr_url"] = stored.get("url")
                row["pr_error"] = stored.get("error")
            rows.append(row)
        return rows

    def _state_landing(self, taken: float, refs: list[dict], fetched_at: float | None) -> dict[str, dict]:
        """`{branch: {landed, landing}}` for `/state` — the harbor's own verdict
        (`_prove_landed`) over COPIES of the cached refs, so the cache stays git
        rows only. `landed` is the tri-state `branches.landed_or_unknown`;
        `landing` (`{how, pr}`) rides only on a True.

        Memoized on (cache fill, PR index fetch): every poll inside one 4 s
        window reads the same answer, and a freshly fetched index re-judges on
        the very next poll. Judging forks only on an ancestry-cache miss
        (`branches.AncestryCache`, once per pair, ever)."""
        key = (taken, fetched_at)
        with self._cache_lock:
            memo = self._landing_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        landing = {}
        for row in self._prove_landed([dict(ref) for ref in refs]):
            landed = branches_mod.landed_or_unknown(row, pr_known=fetched_at is not None)
            landing[row["branch"]] = {"landed": landed, "landing": row.get("landing") if landed else None}
        with self._cache_lock:
            self._landing_memo = (key, landing)
        return landing

    def _merge_creates(self, worktrees: list[dict]) -> list[dict]:
        """Fold create-job progress into the row list.

        Order matters, and it is not "append the in-flight ones". `git worktree
        add` is the FIRST step, so from `bootstrap` onward the worktree is real
        and `git worktree list` already reports it — that row needs the create
        fields merged ON, not a duplicate beside it. Only a branch git has never
        heard of (still in `worktree`, or failed before the add landed) needs a
        row synthesized.

        A synthesized row carries the full shape. The client's `is_row` guard
        only requires `branch` and `session`, but a half-row would make every
        consumer of `ahead` / `dirty` special-case this one case forever.
        """
        creates = self._creates.snapshot()
        if not creates:
            return worktrees

        # Never mutates `worktrees` or its rows: the caller may be handing us
        # the CACHED list, which is read again on every poll for the next four
        # seconds. Rows are rebuilt with a spread, and the new list is fresh.
        merged = []
        for row in worktrees:
            job = creates.pop(row["branch"], None)
            if job:
                row = {**row, "create_status": job["status"], "create_step": job["step"], "create_error": job["error"]}
            merged.append(row)

        for branch, job in creates.items():
            merged.append(
                {
                    "branch": branch,
                    "path": str(worktree_path_for(branch, self._root)),
                    "session": session_name_for(branch),
                    "dirty": False,
                    "ahead": 0,
                    "behind": 0,
                    "pr_status": None,
                    "pr_url": None,
                    "pr_error": None,
                    **UNJUDGED,
                    **_cleanup_fields(None),
                    "create_status": job["status"],
                    "create_step": job["step"],
                    "create_error": job["error"],
                }
            )
        return merged

    def _invalidate(self) -> None:
        """Drop the TTL caches so the next poll reflects an action immediately.

        Without this a freshly created worktree is invisible for up to
        STATE_TTL_SECONDS, which reads as the button having failed. The harbor
        document is dropped with it: every action here changes what the
        harbor draws, and its cache serves the old document until the new one
        lands (`HarborCache.invalidate`), so this costs nothing on a poll.
        """
        with self._cache_lock:
            self._cache = None
        self._harbor.invalidate()

    # -- create -------------------------------------------------------------
    def _handle_create(self, query: dict[str, list[str]], body: dict) -> tuple:
        branch = _text(query, body, "branch")
        prompt = create_mod.normalize_prompt(_raw(query, body, "prompt"))
        # `existing`: check out a branch that is already there (a harbor
        # island's WORKTREE verb) instead of cutting a new one from base.
        existing = _flag(query, body, "existing")
        # `base`: cut the new branch from this ref instead of `self._base` (a
        # batch launcher's `--base`). Resolved to a SHA here, before any refusal
        # that could leave a half-made worktree (`create.resolve_base_ref`).
        base_ref = _text(query, body, "base")
        base_sha = None
        if base_ref:
            if existing:
                return 400, {"error": "`base` applies to a NEW branch — an existing one starts at its own tip"}
            base_sha, base_error = create_mod.resolve_base_ref(self._repo_root, base_ref)
            if base_error is not None:
                return 400, {"error": base_error}
        branch_error = validate_branch(branch)
        path = worktree_path_for(branch, self._root) if branch_error is None else None
        launch, launch_error = self._launch_for(_text(query, body, "profile"))
        if launch_error is not None:
            return launch_error
        permission_mode, mode_error = self._permission_mode_for(_raw(query, body, "permission_mode"), launch)
        if mode_error is not None:
            return mode_error
        model, model_error = self._model_for(_raw(query, body, "model"), launch)
        if model_error is not None:
            return model_error
        lineage = None
        parent_session = _raw(query, body, "parent_session")
        if parent_session is not None:
            profiles = getattr(self._context, "profiles", None)
            if profiles is None or not hasattr(profiles, "prepare_lineage"):
                return 400, {"error": "restart the bridge to track parent sessions"}
            try:
                lineage = profiles.prepare_lineage(parent_session)
            except ValueError as exc:
                return 400, {"error": str(exc)}
        cli_bin = launch["cli_bin"]
        # The first-turn prompt is TYPED (create.py header), and only Claude's
        # input box is recognised. A Codex in a fresh tree opens on its
        # directory-trust list, marked with the same `›` its composer uses —
        # typing a task plus Enter there would pick "trust, no approvals".
        prompt_reason = None
        if prompt and launch["kind"] != "claude":
            prompt_reason = f"not typed into {launch['kind']} — paste it once the agent is up"
            prompt = ""
        echo = {
            "profile": launch["profile"],
            **({"prompt_reason": prompt_reason} if prompt_reason else {}),
            **({"base": base_ref, "base_sha": base_sha} if base_sha else {}),
        }

        # ALREADY BUILDING is answered before the conflict table, and the order
        # is load-bearing. `git worktree add` is step one, so from `bootstrap`
        # onward the path exists on disk and `decide_create` would refuse a
        # second click with a 409 about a directory THIS VERY JOB is creating —
        # a double-click on `+ WORKTREE` reading as an error. It is not a
        # conflict and not a new job; it is the same job, so say so.
        if branch_error is None and (self._creates.get(branch) or {}).get("status") == "creating":
            return 202, {
                "status": "creating",
                "session": session_name_for(branch),
                "branch": branch,
                "path": str(path),
                "created": False,
                "prompt_queued": False,
                "existing": existing,
                **echo,
            }

        branch_exists = branch_error is None and create_mod.branch_exists(self._repo_root, branch)
        # One bounded `git worktree list`, and only when it can matter: a
        # branch checked out ANYWHERE cannot be checked out again, and the
        # answer the user needs is where it already is.
        checked_out_at = None
        respawn = False
        if existing and branch_exists:
            held = create_mod.checkout_path_for(self._repo_root, branch)
            checked_out_at = str(held) if held else None
            # A tree of OURS whose session died is re-entered, not refused: its work (and a
            # LEAD worker's open PR) is still in it. Anyone else's tree, or a live session, keeps the 409.
            respawn = bool(
                held
                and is_managed_path(held, self._root)
                and create_mod.tree_present(held)
                and not create_mod.session_exists(session_name_for(branch))
            )
            if respawn:
                path = held
        refusal = create_mod.decide_create(
            branch_error,
            git_ok=create_mod.tool_found("git"),
            tmux_ok=create_mod.tool_found("tmux"),
            claude_found=cli_bin is not None,
            branch_exists=branch_exists,
            path_exists=(path is not None and path.exists()),
            existing=existing,
            checked_out_at=checked_out_at,
            respawn=respawn,
            cli=launch["kind"] if launch["kind"] != "shell" else "zsh",
        )
        if refusal is not None:
            return refusal
        if respawn:
            echo["respawn"] = True

        session = session_name_for(branch)
        if create_mod.session_exists(session):
            # Session names are tmux-global and a branch name is not unique
            # across repositories: `wt-fix` may be another repository's agent.
            # Selecting it would hand the click to the wrong tree, so it is a refusal.
            home = create_mod.session_home(session)
            if home is not None and not is_managed_path(home, self._root):
                return 409, {
                    "error": f"a session named {session} already runs in {display_path(home)},"
                    " outside this repository's worktrees — use another branch name",
                    "session": session,
                    "branch": branch,
                }
            # IDEMPOTENT, and deliberately a success: a second CREATE on the
            # same branch must select the session that is already there.
            return 200, {
                "session": session,
                "branch": branch,
                "path": str(path),
                "created": False,
                "prompt_queued": False,
                "existing": existing,
                **echo,
            }

        # THE critical section, and it is now a test-and-set rather than a lock
        # held across three subprocesses. The check above catches the ordinary
        # double-click; this catches the genuine RACE, where two requests both
        # read "not creating" before either claimed. Exactly one wins, and the
        # loser is reported as the same job it is — never as a failure.
        if not self._creates.claim(branch):
            return 202, {
                "status": "creating",
                "session": session,
                "branch": branch,
                "path": str(path),
                "created": False,
                "prompt_queued": bool(prompt),
                "existing": existing,
                **echo,
            }
        # A new tree starts with no clean-up history, even when the old one was
        # removed by hand rather than through `_tree_removed`.
        self._cleanups.drop_settled(branch)

        worker = threading.Thread(
            # The profile is bound, not appended to `args`: the positional
            # tail is a contract the harbor tests read (`existing` is last).
            target=functools.partial(
                self._create_worker,
                profile=launch["profile"],
                base=base_sha,
                lineage=lineage,
                respawn=respawn,
                permission_mode=permission_mode,
                model=model,
            ),
            args=(path, branch, session, cli_bin, prompt, existing),
            daemon=True,
            name=f"worktree-create-{branch}",
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            # The claim is taken before the start, so a start that never happens
            # would wedge this branch as `creating` forever. Undo it — the same
            # guard, for the same reason, as `_handle_pr`'s.
            self._creates.fail(branch, f"could not start: {type(exc).__name__}: {exc}")
            self._invalidate()
            return 500, {"error": "could not start the create job"}

        self._invalidate()
        # `permission_mode` only here, where THIS request starts the session: the idempotent 200
        # and the two "already creating" 202s describe a launch that may not have asked for it.
        return 202, {
            "status": "creating",
            "session": session,
            "branch": branch,
            "path": str(path),
            "created": True,
            "prompt_queued": bool(prompt),
            "existing": existing,
            **echo,
            **({"permission_mode": permission_mode} if permission_mode else {}),
            **({"model": model} if model else {}),
        }

    def _launch_for(self, requested: str) -> tuple[dict, tuple | None]:
        """Which agent `/create` starts: `({profile, kind, cli_bin}, refusal)`.

        Through the bridge's launch profiles when the host has them (an absent
        `profile` is the registry's default; an UNKNOWN one refuses rather than
        quietly starting the default — that would be the wrong account). A host
        without profiles can only start bare `claude`, so asking it for a
        profile refuses too: honouring the request is impossible, and ignoring
        it is the same wrong-account outcome.

        Claude keeps `WORKTREE_CLAUDE_BIN` (`_resolve_claude_bin`); Codex is
        found by the registry, and a shell always is.

        `"auto"` is the freest account, resolved by the host right now — the
        scoring is owned by tldrack `launch_pick.py`. A host that
        predates it refuses, like an unknown profile.
        """
        profiles = getattr(self._context, "profiles", None)
        if profiles is None:
            if requested:
                return {}, (400, {"error": "this bridge has no launch profiles — restart it"})
            return {"profile": None, "kind": "claude", "cli_bin": _resolve_claude_bin()}, None
        if requested == AUTO_PROFILE_ID:
            if not hasattr(profiles, "pick_launch"):
                return {}, (400, {"error": "this bridge cannot pick a profile — restart it"})
            requested = profiles.pick_launch()
            if requested is None:
                return {}, (503, {"error": "no launchable agent profile for auto"})
        described = profiles.describe(requested or None)
        if described is None:
            return {}, (400, {"error": f"unknown profile '{requested}'"})
        kind = described["kind"]
        if kind == "claude":
            cli_bin = _resolve_claude_bin()
        else:
            cli_bin = profiles.cli_path(described["id"])
        return {"profile": described["id"], "kind": kind, "cli_bin": cli_bin}, None

    def _permission_modes(self) -> list[str] | None:
        """The launch permission modes the host can start, or None for a host
        that predates them (no profiles, or a registry without the two methods)."""
        profiles = getattr(self._context, "profiles", None)
        if not (hasattr(profiles, "permission_modes") and hasattr(profiles, "permission_refusal")):
            return None
        return list(profiles.permission_modes())

    def _permission_mode_for(self, requested: object, launch: dict) -> tuple[str | None, tuple | None]:
        """`/create {permission_mode}` → `(mode, refusal)`. Absent or empty is
        today's launch (None). What a mode means, and whether THIS profile's CLI
        can start in it (a shell, a pinned profile, an older CLI), is the host's
        `permission_refusal` — asked with the Claude binary this plugin will run.
        Every "cannot" REFUSES rather than launching in the default mode: the
        caller asked for unattended approvals and would wait on prompts nobody is
        watching, or on a CLI that exits at its first argument (`_launch_for`'s rule)."""
        if requested is None or requested == "":
            return None, None
        modes = self._permission_modes()
        if modes is None:
            return None, (400, {"error": "this bridge cannot start a permission mode — restart it"})
        if not isinstance(requested, str) or requested not in modes:
            return None, (
                400,
                {"error": f"unknown permission_mode {str(requested)[:40]!r} (one of: {', '.join(modes) or 'none'})"},
            )
        refused = self._context.profiles.permission_refusal(launch["profile"], requested, cli_bin=launch["cli_bin"])
        if refused is not None:
            return None, (400, {"error": f"cannot start in '{requested}': {refused}"})
        return requested, None

    def _model_for(self, requested: object, launch: dict) -> tuple[str | None, tuple | None]:
        """`/create {model}` → `(model, refusal)`: the model the new agent launches on. Absent or
        empty is the profile's own. What a launch model is, and whether THIS profile can take one,
        is the host's `model_refusal`. Every "cannot" REFUSES, by `_permission_mode_for`'s rule: a
        caller that asked for a weaker model must never get a stronger one quietly."""
        if requested is None or requested == "":
            return None, None
        profiles = getattr(self._context, "profiles", None)
        if not hasattr(profiles, "model_refusal"):
            return None, (400, {"error": "this bridge cannot start a launch model — restart it"})
        refused = profiles.model_refusal(launch["profile"], requested)
        if refused is not None:
            return None, (400, {"error": f"cannot launch on that model: {refused}"})
        return requested, None

    def _create_worker(
        self,
        path: Path,
        branch: str,
        session: str,
        cli_bin: str,
        prompt: str,
        existing: bool = False,
        profile: str | None = None,
        base: str | None = None,
        lineage: dict | None = None,
        respawn: bool = False,
        permission_mode: str | None = None,
        model: str | None = None,
    ) -> None:
        """Walk the four steps off the request thread, reporting through `/state`.

        `_invalidate()` after every transition is not an optimisation: `/state`
        is cached for STATE_TTL_SECONDS, so without it each step label would lag
        up to 4 s behind the work and the ladder would look stuck.

        Steps are dropped into the store BEFORE the work they name, so the label
        says what is happening now rather than what just finished.

        `respawn` starts a session in a tree that is already built: `worktree` and
        `bootstrap` ran when it was made, so both complete at once.

        `permission_mode` was validated by `/create`; it reaches the host's
        `spawn_argv` (never a host without profiles: `_permission_mode_for` refused).
        The same for `model` and `_model_for`.
        """
        profiles = getattr(self._context, "profiles", None)
        bootstrap_error = None
        try:
            if not respawn:
                failure = create_mod.add_worktree(
                    self._repo_root, path, branch, base or self._base, existing=existing, sparse_exclude=self._sparse
                )
                if failure is not None:
                    return self._create_failed(branch, failure[1].get("error", "git worktree add failed"))

                # Non-fatal by design — the session is usable either way. Kept as a
                # note on the row rather than a failure, so the user learns why the
                # verify hook is unhappy in this tree without losing the session.
                self._creates.advance(branch, "bootstrap")
                self._invalidate()
                bootstrap_error = create_mod.run_bootstrap(
                    self._bootstrap, path, self._repo_root, env=profiles.child_env(profile) if profiles is not None else None
                )

            self._creates.advance(branch, "session")
            self._invalidate()
            if profiles is not None:
                code, payload = create_mod.run_spawn(
                    profiles.spawn_argv(
                        profile,
                        session,
                        str(path),
                        cli_bin=cli_bin,
                        **({"lineage": lineage} if lineage else {}),
                        **({"permission_mode": permission_mode} if permission_mode else {}),
                        **({"model": model} if model else {}),
                    ),
                    session,
                )
            else:
                code, payload = create_mod.spawn_session(cli_bin, session, path, inline_env())
            if code != 200:
                return self._create_failed(branch, payload.get("error", "tmux new-session failed"))

            if prompt:
                # Folded in as the last STEP rather than given a thread of its
                # own. It waits up to READY_TIMEOUT_SECONDS for Claude's input
                # box — already off the request thread here, and one worker is
                # one thing to reason about instead of two racing ones.
                self._creates.advance(branch, "prompt")
                self._invalidate()
                if not create_mod.send_prompt(session, prompt):
                    self._context.log(f"  worktree: prompt NOT typed into '{session}' — paste: {prompt}")
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the branch
            return self._create_failed(branch, f"{type(exc).__name__}: {exc}")

        # Forget the row: the worktree is real now, so `git worktree list` is
        # the answer and this would only be a second copy able to disagree.
        self._creates.finish(branch)
        self._invalidate()
        note = f" ({bootstrap_error})" if bootstrap_error else ""
        self._context.log(
            f"  worktree: {'respawned' if respawn else 'created'} '{branch}' at {path} in tmux session '{session}'{note}"
        )

    def _create_failed(self, branch: str, error: str) -> None:
        self._creates.fail(branch, error)
        self._invalidate()
        self._context.log(f"  worktree: create '{branch}' failed — {error}")

    # -- pr -----------------------------------------------------------------
    def _handle_pr(self, query: dict[str, list[str]], body: dict) -> tuple:
        branch = _text(query, body, "branch")
        title = _text(query, body, "title") or None
        record = self._managed_record(branch)
        path = Path(record["path"]) if record else None
        stored = self._store.get(branch) or {}
        if record is not None and (self._cleanups.get(branch) or {}).get("status") == CleanupStore.running:
            # A push from a tree that is being removed would race its own deletion.
            return 409, {"error": "this worktree is being cleaned up — nothing to PR"}

        refusal = pr_mod.decide_pr(
            known=record is not None,
            gh_found=shutil.which("gh") is not None,
            gh_authed=pr_mod.gh_authed() if shutil.which("gh") else False,
            has_commits=pr_mod.has_commits(path, self._base) if path else False,
            dirty=pr_mod.is_dirty(path) if path else False,
            in_flight=stored.get("status") == "opening",
        )
        if refusal is not None:
            return refusal

        # Marked BEFORE the worker starts, so a second click takes the
        # `in_flight` branch rather than racing a second push in.
        self._store.set(branch, status="opening")
        with self._pr_jobs_lock:
            self._pr_jobs.add(branch)
        self._invalidate()
        worker = threading.Thread(
            target=self._pr_worker,
            args=(path, branch, title),
            daemon=True,
            name=f"worktree-pr-{branch}",
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            # `opening` is set before the start, so a start that never happens
            # would wedge this branch forever: `/state` reports `opening` and
            # every later click takes the early return. Undo it.
            with self._pr_jobs_lock:
                self._pr_jobs.discard(branch)
            self._store.set(branch, status="error", error=f"could not start: {type(exc).__name__}: {exc}"[:200])
            self._invalidate()
            return 500, {"error": "could not start the PR job"}
        return 202, {"status": "opening", "branch": branch}

    def _pr_worker(self, path: Path, branch: str, title: str | None) -> None:
        try:
            status, url, error = pr_mod.open_pull_request(path, branch, self._base, title)
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the branch
            status, url, error = "error", None, f"{type(exc).__name__}: {exc}"[:300]
        self._store.set(branch, status=status, url=url, error=error)
        with self._pr_jobs_lock:
            self._pr_jobs.discard(branch)
        self._invalidate()
        self._context.log(f"  worktree: PR for '{branch}' → {status} {url or error or ''}")

    # -- remove -------------------------------------------------------------
    def _handle_remove(self, query: dict[str, list[str]], body: dict) -> tuple:
        branch = _text(query, body, "branch")
        force = _flag(query, body, "force")
        record = self._managed_record(branch)
        if record is None:
            return 404, {"error": NO_MANAGED_REASON}

        error = create_mod.remove_worktree(self._repo_root, Path(record["path"]), [session_name_for(branch)], force=force)
        if error is not None:
            # The overwhelmingly common cause is uncommitted work, and destroying
            # that silently is the one thing this route must never do. `force` is
            # a separate, explicit request.
            return 409, {"error": error, "needs_force": not force}
        self._tree_removed(branch)
        # The BRANCH is deliberately kept. It may already have a PR, and deleting
        # a ref is not recoverable from this UI — `/cleanup` is the verb that
        # takes it, and only once it has landed.
        return 200, {"branch": branch, "removed": True}

    def _managed_record(self, branch: str) -> dict | None:
        """The managed worktree holding `branch`, from a fresh listing —
        `_managed()` is the write gate, so every verb that touches a tree
        finds it here."""
        return next((row for row in self._managed() if row["branch"] == branch), None)

    def _tree_removed(self, branch: str) -> None:
        """Forget a removed tree's job rows — only once it is really gone, so a
        re-created branch of the same name inherits neither the old PR link
        nor a stale create or clean-up error."""
        self._store.drop(branch)
        self._creates.drop(branch)
        self._cleanups.drop_settled(branch)
        self._invalidate()

    # -- cleanup: a landed worktree, its session and its branch, in one press --
    def _handle_cleanup(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /cleanup {branch, session?}` — the rack's CLEAN. 202 and a
        worker that reports through `/state` (`cleanup_status`), the `/pr`
        shape.

        This half refuses only what the CALLER got wrong, in order: a bad
        branch or session name (400) → no managed tree (404, `_managed()`
        stays the only write gate) → a create mid-flight (409) → a cleanup
        already running (409, `CleanupStore.claim`'s test-and-set). Whether
        the branch landed and the tree is clean are facts about NOW, asked by
        the worker (`_clean_up`).

        `session` is the caller's cwd-joined pane — a `+ NEW` session that ran
        `EnterWorktree`, whose name is not the derived `wt-` one. It goes with
        the tree; it grants nothing the rack's ✕ cannot already do to a
        session by name.
        """
        branch = _text(query, body, "branch")
        raw_session = _raw(query, body, "session")
        session = None if raw_session in (None, "") else raw_session
        name_error = validate_existing_branch(branch)
        if name_error is None and session is not None:
            name_error = validate_session_name(session)
        if name_error is not None:
            return 400, {"error": name_error}
        if self._managed_record(branch) is None:
            return 404, {"error": NO_MANAGED_REASON}
        if (self._creates.get(branch) or {}).get("status") == "creating":
            return 409, {"error": branches_mod.CREATING_REASON}
        if not self._cleanups.claim(branch):
            return 409, {"error": "already cleaning up this worktree"}
        self._invalidate()
        worker = threading.Thread(
            target=self._cleanup_worker, args=(branch, session), daemon=True, name=f"worktree-cleanup-{branch}"
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            # `_handle_pr`'s guard: a claim whose worker never starts would
            # read `running` forever.
            self._cleanups.fail(branch, f"could not start: {type(exc).__name__}: {exc}")
            self._invalidate()
            return 500, {"error": "could not start the cleanup job"}
        return 202, {"ok": True, "branch": branch}

    def _cleanup_worker(self, branch: str, session: str | None) -> None:
        try:
            refusal = self._clean_up(branch, session)
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the branch
            refusal = f"{type(exc).__name__}: {exc}"
        if refusal is None:
            self._cleanups.finish(branch)
        else:
            self._cleanups.fail(branch, refusal)
            self._context.log(f"  worktree: cleanup of '{branch}' stopped — {refusal}")
        self._invalidate()

    def _clean_up(self, branch: str, session: str | None) -> str | None:
        """The cleanup: None once the tree is gone, else why nothing was removed.

        Every question is asked FRESH and before anything is destroyed
        (`_fresh_row` owns why a click is never acted on from the cached row).
        In order: no PR job pushing from it → LANDED, by the harbor's own
        verdict (so CLEAN and the harbor cannot disagree) → work of its OWN
        landed (`landing.work_landed`: a branch cut from the base and never
        committed to is an ancestor too, and its agent is mid-turn) → still a
        managed tree → nothing uncommitted (never forced: work made after the
        merge is new work, and PR is its button) → the sessions → nobody else
        left standing in the tree (`landing.await_vacated`: a third session or
        a dev server that cwd'd in would be cut off; unknown refuses) → the
        tree (`create.remove_worktree`, `/remove`'s own) → the branch
        (`_delete_branch`, `/delete-branch`'s own — `-D` when squash-landed).

        A tree an occupant keeps is refused AFTER the kills: those sessions
        were the press's to end either way, and the refusal names what is
        left. A branch the delete then refuses is NOT a failed cleanup: the
        tree is gone, so the rack row is too, and the harbor can finish it.
        """
        with self._pr_jobs_lock:
            if branch in self._pr_jobs:
                return "a PR is being opened for this branch — wait for it"
        _rows, row, failure = self._fresh_row(branch, None)
        if failure is not None:
            return failure[1]["error"]
        if row is None:
            return "its branch is gone — nothing to clean up"
        if row.get("landed") is not True:
            return f"not landed in {self._base} — nothing to clean up"
        if not landing_mod.work_landed(row.get("landing")):
            return "nothing of this branch's own has landed — no merged PR shipped it"
        record = self._managed_record(branch)
        if record is None:
            return NO_MANAGED_REASON
        path = record["path"]
        # A prunable tree's directory is already gone, so nothing uncommitted
        # can be in it and nobody can stand in it — and `git worktree remove`
        # drops just its entry (measured on git 2.54), so it needs neither
        # probe and no `prune`.
        if not record.get("prunable"):
            dirty = branches_mod.dirty_sweep([path], DIRTY_SWEEP_BUDGET_SECONDS).get(path)
            if dirty is None:
                return "could not read the worktree's status — nothing removed"
            if dirty:
                return "uncommitted work in the worktree — nothing removed"
        sessions = [session_name_for(branch)] + ([session] if session else [])
        create_mod.kill_sessions(sessions)
        if not record.get("prunable"):
            left = landing_mod.await_vacated(path)
            if left is None:
                return "could not tell whether anything still runs in the worktree — it was kept"
            if left:
                return f"{', '.join(left)} still running in the worktree — it was kept"
        error = create_mod.remove_worktree(self._repo_root, Path(path), [])
        if error is not None:
            return error
        self._tree_removed(branch)
        status, body = self._delete_branch(branch, force=False)
        if status != 200:
            self._context.log(f"  worktree: cleanup kept branch '{branch}' — {body.get('error')}")
        self._context.log(
            f"  worktree: cleaned up '{branch}' — worktree and session(s) {', '.join(dict.fromkeys(sessions))} removed"
        )
        return None

    # -- changes: what a forced remove would destroy ------------------------
    def _handle_changes(self, query: dict[str, list[str]]) -> tuple:
        """`GET /changes?branch=<name>[&path=<file>]` — the uncommitted work in
        a worktree, and on request one file's patch.

        Read-only, so unlike `POST /remove` this is NOT gated on
        `is_managed_path`: every worktree git lists may be inspected, including
        the `~/other-tool/worktrees/` trees Claude Code owns. Looking is what
        makes the refusal legible, and the write gate is unchanged — a row here
        ships `managed` so a caller can see which verb it may offer next.

        The `path` parameter is a LOOKUP, never an argument. It is matched
        against the paths `git status` just printed, and only the matched ROW
        reaches `patch_for` — so no string a caller invents is ever handed to
        git, and `--` never has to carry the weight of a path that starts with
        a dash.
        """
        branch = _text(query, {}, "branch")
        if not branch:
            return 400, {"error": "branch is required"}
        record = worktree_by_branch(list_worktrees(self._repo_root)).get(branch)
        if record is None:
            return 404, {"error": "no worktree holds that branch"}
        path = Path(record["path"])

        rows, summary, error = changes_mod.collect(path)
        if error:
            return 502, {"error": error}

        patch = None
        wanted = _text(query, {}, "path")
        if wanted:
            row = next((candidate for candidate in rows if candidate["path"] == wanted), None)
            if row is None:
                return 404, {"error": "no such change in that worktree"}
            text, truncated, patch_error = changes_mod.patch_for(path, row)
            patch = {"path": wanted, "text": text, "truncated": truncated, "error": patch_error}

        shown = rows[: changes_mod.MAX_FILES]
        return 200, {
            "branch": branch,
            "path_label": display_path(str(path)),
            "managed": is_managed_path(record.get("path"), self._root),
            "files": shown,
            "summary": summary,
            # The summary counts every row; the list is a prefix. Say so rather
            # than letting a reader believe 500 is all there is.
            "truncated": len(rows) > len(shown),
            "patch": patch,
            "adopted": self._adoption(branch, path),
        }

    def _adoption(self, branch: str, worktree: Path) -> dict | None:
        """Has this worktree's work been rescued, and is that still true?

        Lives on `/changes` rather than on the polled `/branches` row because
        answering it costs a whole-tree snapshot, and `/changes` is PULLED —
        it runs when the fold opens, not 12 times a minute. The row carries the
        cheap half (the store's record) so the panel can label a button; this
        is the half that decides whether a forced remove destroys anything.

        `unpreserved` empty is the safe-delete signal. It is a TREE comparison,
        so an edit made and then undone reads as preserved, which a timestamp
        would get wrong in both directions.
        """
        record = self._adopts.get(branch)
        if record is None:
            return None
        missing, error = adopt_mod.unpreserved(self._repo_root, worktree, record["tree"])
        return {
            "at": record["at"],
            "files": record["files"],
            "ref": record["ref"],
            # A failed comparison is NOT "nothing unpreserved". Unknown must
            # read as unsafe, or a broken probe silently downgrades a destroy
            # warning — `dirty: null` is the same rule one tier up.
            "unpreserved": None if error else missing,
            "error": scrub_home(error),
        }

    # -- adopt: getting work OUT of a worktree ------------------------------
    def _adopt_target(self, branch: str) -> tuple[tuple[dict, Path] | None, tuple | None]:
        """Resolve `branch` to `(source record, target checkout)`, or the
        refusal to return.

        The source is a LOOKUP against what `git worktree list` printed — the
        `/changes` rule. It is deliberately NOT gated on `is_managed_path`:
        reading a tree and copying out of it destroys nothing, and the trees
        Claude Code owns hold work worth rescuing too.

        The target is not a caller's choice at all. It is the PRIMARY checkout
        — `git worktree list`'s first stanza — and not `self._repo_root`, which
        is `context.start_dir` and is whatever directory the bridge happened to
        be launched in. Those are usually the same path and occasionally are
        not: a bridge started inside a worktree would otherwise adopt a
        worktree's changes into another worktree, silently.
        """
        if not branch:
            return None, (400, {"error": "branch is required"})
        name_error = validate_existing_branch(branch)
        if name_error is not None:
            return None, (400, {"error": name_error})
        if not create_mod.tool_found("git"):
            return None, (503, {"error": "git not found"})
        records = list_worktrees(self._repo_root)
        record = worktree_by_branch(records).get(branch)
        if record is None:
            return None, (404, {"error": "no worktree holds that branch"})
        main_path = main_worktree_path(records)
        if main_path is None:
            return None, (500, {"error": "could not find the main checkout"})
        if is_primary_checkout(record.get("path"), main_path):
            return None, (409, {"error": "that IS the main checkout — nothing to adopt from"})
        return (record, Path(main_path)), None

    def _plan_body(self, branch: str, record: dict, plan: dict) -> dict:
        return {
            "branch": branch,
            "path_label": display_path(record.get("path")),
            "managed": is_managed_path(record.get("path"), self._root),
            "target": self._base,
            "files": plan["files"],
            "total": plan["total"],
            "truncated": plan["truncated"],
            "summary": plan["summary"],
            "blocked": plan["blocked"],
            "conflicts": plan["conflicts"],
            "clean": plan["clean"],
            "adoptable": plan["adoptable"],
        }

    def _handle_adopt_plan(self, query: dict[str, list[str]]) -> tuple:
        """`GET /adopt-plan?branch=<name>` — what adopting WOULD do, per file.

        Read-only in the sense that matters: no working tree, index or ref is
        written anywhere. It does add trees and one commit to the shared object
        database, because that is how git is asked to perform a merge without
        performing it on a checkout; those objects are unreferenced and are
        collected like any other, unless `POST /adopt` later anchors one.
        """
        branch = _text(query, {}, "branch")
        resolved, refusal = self._adopt_target(branch)
        if refusal is not None:
            return refusal
        record, target = resolved
        plan, error = adopt_mod.plan_adopt(target, Path(record["path"]))
        if error:
            return 502, {"error": scrub_home(error)}
        return 200, self._plan_body(branch, record, plan)

    def _handle_adopt(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /adopt {branch, force?}` — land the worktree's uncommitted work
        in THIS checkout's working tree.

        The plan is recomputed here and never taken from the caller: a plan is
        a statement about two trees at a moment, and the moment the user read it
        is not this one.

        Two refusals, and the order matters. `blocked` is absolute — a path this
        checkout has uncommitted edits on has nowhere else to keep them, while
        the source's copy is still sitting in its worktree, so the cheap
        recovery is the one that refuses. `conflict` is the ordinary
        `needs_force` handshake every other harbor verb speaks, so the panel's
        red re-arm needs no new vocabulary.
        """
        branch = _text(query, body, "branch")
        force = _flag(query, body, "force")
        resolved, refusal = self._adopt_target(branch)
        if refusal is not None:
            return refusal
        record, target = resolved
        worktree = Path(record["path"])

        plan, error = adopt_mod.plan_adopt(target, worktree)
        if error:
            return 502, {"error": scrub_home(error)}
        if plan["total"] == 0:
            return 409, {"error": "nothing uncommitted to adopt"}
        if plan["blocked"]:
            count = len(plan["blocked"])
            noun = "file has" if count == 1 else "files have"
            return 409, {
                "error": f"{count} {noun} uncommitted edits here too — commit or revert them first",
                "blocked": plan["blocked"],
            }
        if plan["conflicts"] and not force:
            count = len(plan["conflicts"])
            noun = "file" if count == 1 else "files"
            return 409, {
                "error": f"{count} {noun} would land with conflict markers",
                "needs_force": True,
                "conflicts": plan["conflicts"],
            }

        written, error = adopt_mod.materialize(target, plan["merged_tree"], plan["files"])
        if error:
            return 500, {"error": scrub_home(error)}

        # Anchor AFTER the write, so a ref never claims a rescue that failed.
        # A failed anchor is reported but does not fail the adopt: the files are
        # already on disk, and the honest answer is "landed, not anchored".
        anchor_error = adopt_mod.anchor(target, branch, plan["snapshot_commit"])
        ref = None if anchor_error else adopt_mod.adopt_ref_for(branch)
        self._adopts.set(branch, tree=plan["snapshot_tree"], commit=plan["snapshot_commit"], ref=ref, files=written)
        self._invalidate()
        self._context.log(f"  worktree: adopted {len(written)} file(s) from '{branch}' into {self._base}")
        return 200, {
            "branch": branch,
            "target": self._base,
            "adopted": written,
            "count": len(written),
            "ref": ref,
            "forced": force,
            "summary": plan["summary"],
            "anchor_error": scrub_home(anchor_error),
        }

    # -- one branch's row, swept fresh --------------------------------------
    def _fresh_row(self, branch: str, name_error: str | None) -> tuple[list[dict], dict | None, tuple | None]:
        """`(rows, row, failure)` for a branch, from a FRESH sweep.

        `failure` is a ready-to-return `(status, dict)` when git is missing or
        the sweep failed, else None. The sweep is skipped for a name the table
        refuses first: the 400 is row one of the caller's decision either way,
        and a fork it cannot use is a fork not made — so a `name_error` yields
        `([], None, None)` and the caller's refusal still fires.

        Every mutating verb here sweeps rather than reading the cached harbor
        document: the cached row promised a verdict about the last four
        seconds; a click is a promise about now, and a branch that gained a
        commit or a checkout in between must be refused, not acted on.
        """
        if name_error is not None:
            return [], None, None
        if not create_mod.tool_found("git"):
            return [], None, (503, {"error": "git not found"})
        rows, _caps, sweep_error = branches_mod.sweep_refs(self._repo_root, self._base)
        if sweep_error is not None:
            return [], None, (500, {"error": scrub_home(sweep_error)})
        row = next((r for r in rows if r["branch"] == branch), None)
        if row is not None:
            # Fresh means `landed` too: the cached row's answer is four seconds
            # old. Every row is judged, because `contained_in` is a fact about
            # the others.
            self._judge(rows)
        return rows, row, None

    def _join_landing(self, rows: list[dict]) -> list[dict]:
        """`branches.join_landing` against this plugin's PR index and ancestry
        cache — the one door every verdict reads `landed` through."""
        return branches_mod.join_landing(rows, self._pr_index.get, self._ancestry.contains, self._base)

    def _prove_landed(self, rows: list[dict]) -> list[dict]:
        """`landed` and `landing` on every LOCAL row, in place: `_join_landing`,
        then the content proofs and ledger (`landing.ContentTier.judge` —
        caches only, never a fork). The harbor, the click-time sweeps and
        `/state`'s CLEAN all read `landed` through here, so none can disagree.

        Hand it EVERY local row, never a subset: the content tier keeps ONE
        slot of base records, sized by the oldest merge-base among the rows it
        is shown, so two callers judging different sets would evict each
        other's on every pass. The remote tier stops at `_join_landing`: its
        rows are measured against a remote's base, and the content tier's
        caches are keyed by the LOCAL base."""
        self._join_landing(rows)
        _fetched_at, prs = self._pr_index.snapshot()
        self._content.judge(rows, base=self._base, prs=prs)
        return rows

    def _judge(self, rows: list[dict]) -> list[dict]:
        """The LOCAL tier's whole verdict, in place: `landed` (`_prove_landed`),
        then `contained_in` among what is left, then the evidence tier."""
        self._prove_landed(rows)
        open_rows = [
            row
            for row in rows
            if not row.get("is_base") and row.get("landed") is not True and isinstance(row.get("ahead"), int) and row["ahead"] > 0
        ]
        for row in rows:
            row["contained_in"] = None
        held = self._contained(rows, open_rows)
        for row in open_rows:
            row["contained_in"] = held.get(row["branch"])
            if row["contained_in"]:
                row["ledger"] = None
        # Last, because it reads every verdict above: evidence is asked only of
        # a row no proof and no containment could answer.
        self._absorption.judge(rows)
        return rows

    def _contained(self, rows: list[dict], open_rows: list[dict]) -> dict[str, str]:
        """`landing.containment` over the open rows, cached on every local
        (branch, tip). A failed `for-each-ref --contains` contains nothing."""
        if len(open_rows) < 2:
            return {}
        key = frozenset((row["branch"], row.get("oid")) for row in rows)
        if self._containment is not None and self._containment[0] == key:
            return self._containment[1] or {}
        holders = landing_mod.contains_sets(self._repo_root, open_rows)
        candidates = [
            {
                "branch": row["branch"],
                "oid": row.get("oid"),
                "committed_at": row.get("committed_at"),
                "has_worktree": bool(row.get("worktree_path")),
            }
            for row in open_rows
        ]
        result = landing_mod.containment(candidates, holders) if holders is not None else None
        self._containment = (key, result)
        return result or {}

    def _main_checkout(self) -> Path | None:
        """The primary checkout SHIP acts on — git's first worktree stanza, not
        `start_dir` (`listing.main_worktree_path` owns why)."""
        found = main_worktree_path(list_worktrees(self._repo_root))
        return Path(found) if found else None

    def _start_background(self, target, name: str) -> None:
        """Every background worker here starts through `threading.Thread` as
        this module names it — one seam for the tests' fake thread."""
        threading.Thread(target=target, daemon=True, name=name).start()

    # -- catch up: bringing a stale branch forward --------------------------
    def _handle_catch_up(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /catch-up {branch}` — fast-forward a branch onto the base.

        Fast-forward ONLY. `adopt_mod.decide_catch_up` holds the refusals and
        the reasoning; the short version is that a FF cannot lose or invent a
        commit, which is what lets this fire with no confirmation while every
        other mutating verb here arms twice.

        Re-sweeps INLINE rather than reading the cached document — `_fresh_row`
        owns why.
        """
        branch = _text(query, body, "branch")
        name_error = validate_existing_branch(branch)
        rows, row, failure = self._fresh_row(branch, name_error)
        if failure is not None:
            return failure
        base_row = next((r for r in rows if r.get("is_base")), None)

        upstream = (base_row or {}).get("upstream") or {}
        refusal = adopt_mod.decide_catch_up(
            name_error=name_error,
            known=row is not None,
            is_base=bool(row and row["is_base"]),
            creating=(self._creates.get(branch) or {}).get("status") == "creating",
            behind=row["behind"] if row else None,
            ahead=row["ahead"] if row else None,
            base_behind_remote=upstream.get("behind"),
            base=self._base,
        )
        if refusal is not None:
            return refusal

        behind = row["behind"]
        held = worktree_by_branch(list_worktrees(self._repo_root)).get(branch)

        # No checkout: advance the ref alone. `fetch .` applies git's OWN
        # fast-forward check, so a non-FF is refused by git rather than by a
        # test of ours that could disagree with it.
        if held is None:
            # FF_TIMEOUT, not REF_TIMEOUT: `fetch .` reads no network but still
            # runs a connectivity check over the objects it is advancing past,
            # which on a repo this size is seconds rather than milliseconds.
            code, _out, err = create_mod.run_bounded(
                adopt_mod.build_ff_fetch_command(self._repo_root, self._base, branch), adopt_mod.FF_TIMEOUT_SECONDS
            )
            if code != 0:
                return 500, {"error": scrub_home(adopt_mod.fail_text(err, "could not advance the branch"))}
            return self._caught_up(branch, behind, autostashed=False, worktree=False)

        worktree = Path(held["path"])
        dirty = adopt_mod.worktree_is_dirty(worktree)

        # A dirty tree is autostashed across the fast-forward, and `stash pop`
        # stops on ANY conflict — so predict it first. Refusing here is what
        # keeps a button from leaving a worktree mid-conflict with no button to
        # get it out; the refusal names ADOPT, which empties the collision.
        if dirty:
            plan, error = adopt_mod.plan_adopt(self._repo_root, worktree, target_rev=self._base)
            if error:
                return 502, {"error": scrub_home(error)}
            if plan["conflict_paths"]:
                return adopt_mod.refuse_dirty_conflicts(plan["conflict_paths"], behind)

        code, _out, err = create_mod.run_bounded(
            adopt_mod.build_ff_merge_command(worktree, self._base, autostash=dirty), adopt_mod.FF_TIMEOUT_SECONDS
        )
        if code != 0:
            return 500, {"error": scrub_home(adopt_mod.fail_text(err, "git merge --ff-only failed"))}
        return self._caught_up(branch, behind, autostashed=dirty, worktree=True)

    def _caught_up(self, branch: str, behind: int, *, autostashed: bool, worktree: bool) -> tuple:
        """The single success exit of `/catch-up`, from either advance path.

        One log site, so the line can name WHICH path moved the branch: two
        branches printing the same sentence left the bridge's log unable to say
        whether a checkout had been fast-forwarded or only a ref advanced.
        """
        self._invalidate()
        where = "worktree" if worktree else "ref"
        self._context.log(f"  worktree: caught '{branch}' up to {self._base} (+{behind}, {where})")
        return 200, {"branch": branch, "advanced": behind, "autostashed": autostashed, "worktree": worktree}

    # -- harbor: the document ---------------------------------------------
    def _harbor_runner(self) -> dict:
        """Build the `/branches` document. Runs on the cache's background
        thread (or inline from a test) — never on a request thread.

        Three cheap forks (`for-each-ref`, `worktree list`, `stash list`) and
        one expensive tier (`git status` per worktree) behind its own TTL and
        budget. A sweep that git cannot answer is a DOCUMENT with `git_ok:
        false` and the reason, not an exception: the harbor then says "base
        branch main not found" instead of warming forever, and the cache's
        exception path stays for what it is — a bug.

        Rows are joined to worktrees by ABSOLUTE path, both directions (a
        prunable worktree may or may not carry a `%(worktreepath)`), and only
        the finished rows are collapsed.
        """
        rows, capabilities, sweep_error = branches_mod.sweep_refs(self._repo_root, self._base)
        self._judge(rows)
        records = list_worktrees(self._repo_root)
        main_path = main_worktree_path(records)
        repo = main_path or str(self._repo_root)

        probe = [r["path"] for r in records if r.get("path") and not r.get("prunable")]
        dirty = self._dirty.sweep_if_due(probe)
        now = self._clock()
        if self._occupants_at is None or now - self._occupants_at >= DIRTY_TTL_SECONDS:
            self._occupants = landing_mod.sweep_occupants(probe)
            self._occupants_at = now

        worktrees = []
        by_abs_path: dict[str, dict] = {}
        for record in records:
            abs_path = record.get("path") or ""
            info = {
                "path": display_path(abs_path),
                "branch": record.get("branch"),
                "head": record.get("head"),
                "detached": bool(record.get("detached")),
                "managed": is_managed_path(abs_path, self._root),
                "is_main": is_primary_checkout(abs_path, main_path),
                "dirty": dirty.get(abs_path),
                "occupied_by": self._occupants.get(abs_path),
                "prunable": bool(record.get("prunable")),
                "prunable_reason": record.get("prunable_reason"),
                "locked": bool(record.get("locked")),
            }
            worktrees.append(info)
            by_abs_path[abs_path] = info
        by_branch = worktree_by_branch(records)
        creating = {b for b, job in self._creates.snapshot().items() if job.get("status") == "creating"}

        branch_rows = []
        for ref in rows:
            branch = ref["branch"]
            held = by_abs_path.get(ref.get("worktree_path") or "")
            if held is None and branch in by_branch:
                held = by_abs_path.get(by_branch[branch].get("path") or "")
            worktree = None
            if held is not None:
                worktree = {
                    "path": held["path"],
                    "managed": held["managed"],
                    "is_main": held["is_main"],
                    "dirty": held["dirty"],
                    "occupied_by": held["occupied_by"],
                    "prunable": held["prunable"],
                    "locked": held["locked"],
                }
            row = {
                "branch": branch,
                "sha": ref["sha"],
                "session": session_name_for(branch),
                "is_base": ref["is_base"],
                "committed_at": ref["committed_at"],
                "author": ref["author"],
                "subject": ref["subject"],
                "ahead": ref["ahead"],
                "behind": ref["behind"],
                "merged": ref["merged"],
                "landed": ref["landed"],
                "landing": ref.get("landing"),
                "contained_in": ref.get("contained_in"),
                "content": ref.get("content", "skipped"),
                "ledger": ref.get("ledger"),
                "absorption": ref.get("absorption"),
                "upstream": ref["upstream"],
                "worktree": worktree,
                "pr": ref["pr"],
                "pr_job": None,
            }
            verdict, reason, removable = branches_mod.delete_verdict(row, base=self._base, creating=branch in creating)
            row["delete_verdict"], row["delete_reason"] = verdict, reason
            if removable:
                row["worktree_removable"] = True
            if (
                worktree
                and not worktree["managed"]
                and not worktree["is_main"]
                and self._outside_refusal(row, worktree, creating=branch in creating) is None
            ):
                row["worktree_removable_outside"] = True
            branch_rows.append(row)

        fetch_at = branches_mod.fetch_head_mtime(repo)
        self._maybe_refresh_prs(fetch_at)
        remote_rows, sweep_failure = self._remote_rows({r["branch"] for r in rows})
        # A remote FETCH could not reach and a remote the sweep could not read
        # are the same fact to a reader — this tier is stale — so they share
        # the one field the panel already renders.
        remote_error = "; ".join(part for part in (sweep_failure, self._remote_fetch_error) if part) or None

        return {
            "generated_at": time.time(),
            "git_ok": sweep_error is None,
            "git_error": sweep_error,
            "base": self._base,
            "pr_remote": pr_mod.DEFAULT_REMOTE,
            "capabilities": capabilities,
            "repo": display_path(repo),
            "stash_count": branches_mod.stash_count(self._repo_root),
            "fetch_at": fetch_at,
            "branches": branch_rows,
            "remote_branches": remote_rows,
            "remote_error": remote_error,
            "worktrees": worktrees,
            "archived": self._archived(),
        }

    def _remote_rows(self, local_names: set[str]) -> tuple[list[dict], str | None]:
        """The REMOTE-ONLY tier: branches that exist on a remote and have no
        local ref at all, so the harbor's local sweep never saw them.

        This is a cleanup surface, not a work surface. It carries no
        `delete_verdict` and no session, on purpose: `branches.decide_delete`'s
        whole safety argument is about a LOCAL ref measured against a LOCAL
        base, and it has a `needs_force` row because a local branch is cheap to
        lose and sits in the reflog afterwards. Neither is true here.

        It carries `scuttle_verdict` instead — a narrower table with no force
        at all, whose one clear answer is "already landed on its remote's
        base" (`branches.landed`, through `_join_landing`).

        Sorted newest first — the only ordering a cleanup list wants, since
        the oldest are the ones you already know are dead.
        """
        rows, error = branches_mod.sweep_remote_refs(self._repo_root, self._base)
        rows = self._join_landing(branches_mod.remote_only(rows, local_names))
        for row in rows:
            verdict, reason = branches_mod.scuttle_verdict(row)
            row["scuttle_verdict"], row["scuttle_reason"] = verdict, reason
        rows.sort(key=lambda row: row["committed_at"], reverse=True)
        return rows, error

    def _branches(self) -> dict:
        """The polled harbor view. Reads the cache and the cheap stores; runs
        nothing (harbor.py § GET never does work).

        This is where subprocess TEXT enters the body — git's stderr as
        `git_error`, a fetch's as `fetch.error`, gh's as `pr_index.error`, a
        runner exception as `error` — and text is where absolute paths hide
        after every path FIELD was collapsed. `names.scrub_home` on each,
        here, at the one assembly point, keeps the whole body under the
        `~`-only invariant rather than each producer remembering to.
        """
        doc, age, error, _refreshing = self._harbor.get()
        job = self._fetch_jobs.get()
        fetch = {
            "status": job["status"] if job else "idle",
            "step": job["step"] if job else None,
            "error": scrub_home(job["error"]) if job else None,
            "at": doc["fetch_at"] if doc else None,
        }
        pr_index = self._pr_index_block()
        brief = self._brief_block()
        if doc is None:
            return {
                "status": "warming",
                "generated_at": None,
                "age_seconds": None,
                "error": scrub_home(error),
                "git_ok": None,
                "git_error": None,
                "base": self._base,
                "pr_remote": pr_mod.DEFAULT_REMOTE,
                "capabilities": {"ahead_behind": True, "gh": gh_found()},
                "repo": display_path(self._repo_root),
                "repo_key": self._repo.key,
                "stash_count": 0,
                "fetch": fetch,
                "pr_index": pr_index,
                "brief": brief,
                "branches": [],
                "remote_branches": [],
                "remote_error": None,
                "worktrees": [],
                "archived": [],
            }
        body = {key: value for key, value in doc.items() if key != "fetch_at"}
        body.update(
            {
                "status": "ready",
                "age_seconds": age,
                "error": scrub_home(error),
                "git_error": scrub_home(doc["git_error"]),
                "remote_error": scrub_home(doc["remote_error"]),
                "fetch": fetch,
                "pr_index": pr_index,
                "brief": brief,
                "branches": self._merge_jobs(doc["branches"]),
                "repo_key": self._repo.key,
                "capabilities": {**doc.get("capabilities", {}), "gh": gh_found()},
            }
        )
        return body

    def _pr_index_block(self) -> dict:
        """The `pr_index` block of `/branches`: the store's meta plus `stale`
        (`PR_INDEX_STALE_SECONDS` owns the rule; a `truncated` list is stale
        at any age, since no refresh brings its oldest PRs back) and whether
        the plugin's own refresh is running right now."""
        meta = self._pr_index.meta()
        age = time.time() - meta["fetched_at"] if meta["fetched_at"] else None
        with self._pr_refresh_lock:
            refreshing = self._pr_refreshing
        return {
            **meta,
            "error": scrub_home(meta["error"]),
            "stale": meta["error"] is not None or meta["truncated"] or age is None or age > PR_INDEX_STALE_SECONDS,
            "refreshing": refreshing,
        }

    def _brief_block(self) -> dict:
        """The `brief` block of `/branches`: the job (or `idle`) plus the
        store's count and last error. Additive, like `fetch` and `pr_index`."""
        job = self._brief_jobs.get()
        meta = self._briefs.meta()
        return {
            "status": job["status"] if job else "idle",
            "step": job["step"] if job else None,
            "done": job["done"] if job else 0,
            "total": job["total"] if job else 0,
            "cost_usd": job["cost_usd"] if job else 0.0,
            "note": scrub_home(job["note"]) if job else None,
            "error": scrub_home(job["error"] if job else meta["error"]),
            "at": job["at"] if job else None,
            "count": meta["count"],
            "model": brief_mod.resolve_model(),
        }

    def _merge_jobs(self, rows: list[dict]) -> list[dict]:
        """Fold `pr_job` and create progress onto the cached rows — with
        spreads, never in place (`_merge_creates`' rule: the list is the
        cache's, read again on every poll). A branch mid-create is re-judged
        here rather than four seconds later: its row says `refused` with the
        same reason the route would give.

        The two error strings are scrubbed as they cross in: `PrStore` and
        `CreateStore` also feed `/state`, which stays absolute, so they cannot
        collapse at the source — `_branches` carries the invariant."""
        prs = self._store.snapshot()
        creates = self._creates.snapshot()
        briefs = self._briefs.snapshot()
        adopts = self._adopts.snapshot()
        if not prs and not creates and not briefs and not adopts:
            return rows  # the common case: THE SAME list, nothing rebuilt
        base_sha = brief_mod.base_sha_of(rows)
        merged = []
        for row in rows:
            branch = row["branch"]
            pr_job = prs.get(branch)
            job = creates.get(branch)
            entry = briefs.get(branch)
            adopted = adopts.get(branch)
            if pr_job is None and job is None and entry is None and adopted is None:
                merged.append(row)
                continue
            row = {
                **row,
                "pr_job": (
                    {"status": pr_job["status"], "url": pr_job["url"], "error": scrub_home(pr_job["error"])} if pr_job else None
                ),
            }
            if entry:
                # Read here, in the merge, so a brief that just landed shows on
                # the very next poll instead of after the 4 s cache. `stale`
                # is judged against the CURRENT tips (brief.py § Staleness).
                row["brief"] = {
                    "what": entry["what"],
                    "next": list(entry["next"]),
                    "model": entry["model"],
                    "at": entry["at"],
                    "stale": brief_mod.is_stale(entry, row, base_sha),
                }
            if adopted:
                # The cheap half: no probe, no fork. `/changes` computes whether
                # it is still true (`_adoption`), because that costs a
                # whole-tree snapshot and this row is polled.
                row["adopted"] = {"at": adopted["at"], "files": adopted["files"], "ref": adopted["ref"]}
            if job:
                row.update({"create_status": job["status"], "create_step": job["step"], "create_error": scrub_home(job["error"])})
                if job["status"] == "creating" and row["delete_verdict"] != "refused":
                    row.update({"delete_verdict": "refused", "delete_reason": branches_mod.CREATING_REASON})
                    row.pop("worktree_removable", None)
            merged.append(row)
        return merged

    # -- harbor: fetch ------------------------------------------------------
    def _handle_fetch(self) -> tuple:
        """`git fetch --prune origin`, then `gh pr list` into the PR index —
        202 and a worker, the `/pr` shape. `started: false` is the same job
        already running, never a failure."""
        if not create_mod.tool_found("git"):
            return 503, {"error": "git not found"}
        if not self._fetch_jobs.claim():
            job = self._fetch_jobs.get() or {}
            return 202, {"status": "running", "step": job.get("step") or FETCH_STEPS[0], "started": False}
        worker = threading.Thread(target=self._fetch_worker, daemon=True, name="worktree-fetch")
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            # The claim is taken before the start; a start that never happens
            # would report `running` forever. Undo it — `_handle_pr`'s guard.
            self._fetch_jobs.fail(f"could not start: {type(exc).__name__}: {exc}")
            return 500, {"error": "could not start the fetch job"}
        return 202, {"status": "running", "step": FETCH_STEPS[0], "started": True}

    def _fetch_worker(self) -> None:
        """The two FETCH_STEPS, off the request thread.

        The git half and the `gh` half fail independently: a missing or
        logged-out `gh` is recorded on the PR index (`pr_index.error`) and the
        fetch still counts, because the `[gone]` upstreams and the base's
        `↓N behind origin/main` it just learned are worth having on their own.
        `gh_authed` is probed here, at click time, never cached (pr.py).
        """
        try:
            self._fetch_jobs.advance(FETCH_STEPS[0])
            if not self._fetch_remotes():
                return None

            self._fetch_jobs.advance(FETCH_STEPS[1])
            self._refresh_pr_index()
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the job
            return self._fetch_failed(f"{type(exc).__name__}: {exc}")

        self._fetch_jobs.finish()
        self._harbor.invalidate()
        meta = self._pr_index.meta()
        note = f" ({meta['error']})" if meta["error"] else f" — {meta['count']} PRs indexed{_cut_note(meta)}"
        partial = f"; {self._remote_fetch_error}" if self._remote_fetch_error else ""
        self._context.log(f"  worktree: fetched {self._fetched_label}{note}{partial}")

    def _refresh_pr_index(self) -> bool:
        """`gh pr list` into the PR index, then fetch the merged heads this
        repo never received. True = the index was replaced.

        The ONE copy of this step: FETCH's worker runs it as its second step,
        and `_maybe_refresh_prs` runs it alone. A missing or logged-out `gh`
        is recorded on the index (`pr_index.error`), never raised — the rows
        it already holds stay, because a `gh` broken today does not make
        yesterday's index wrong, only old (`PrIndexStore`).
        """
        if shutil.which("gh") is None:
            self._pr_index.set_error(pr_mod.GH_MISSING_ERROR)
            return False
        if not pr_mod.gh_authed():
            self._pr_index.set_error(pr_mod.GH_UNAUTHED_ERROR)
            return False
        code, out, err = create_mod.run_bounded(
            branches_mod.build_pr_list_command(), pr_mod.NETWORK_TIMEOUT_SECONDS, cwd=self._repo_root
        )
        if code != 0:
            self._pr_index.set_error((err or "gh pr list failed").strip()[:300])
            return False
        try:
            self._pr_index.replace(branches_mod.parse_pr_list(out), time.time(), truncated=branches_mod.pr_list_truncated(out))
        except ValueError as exc:
            self._pr_index.set_error(str(exc))
            return False
        self._fetch_missing_heads()
        return True

    def _fetch_missing_heads(self) -> None:
        """Fetch, by id, the heads of merged PRs whose branch is local here
        and whose head object is not (`landing.build_fetch_heads_command`
        owns why `--no-write-fetch-head`). Best effort: a failure costs a
        proof, never a verdict — an unfetched head already refuses."""
        _fetched_at, prs = self._pr_index.snapshot()
        local = landing_mod.local_branch_names(self._repo_root)
        if not local:
            return
        wanted = [
            pr["head_oid"]
            for branch, pr in prs.items()
            if branch in local and pr.get("state") == "merged" and branches_mod.is_full_oid(pr.get("head_oid"))
        ]
        missing = landing_mod.missing_objects(self._repo_root, wanted)
        if not missing:
            return
        oids = [oid for oid in wanted if oid in missing][:PR_FETCH_HEADS_MAX]
        code, _out, err = create_mod.run_bounded(
            landing_mod.build_fetch_heads_command(self._repo_root, pr_mod.DEFAULT_REMOTE, oids),
            pr_mod.NETWORK_TIMEOUT_SECONDS,
            env=branches_mod.fetch_env(),
        )
        if code == 0:
            self._ancestry.forget_unknown()
            self._context.log(f"  worktree: fetched {len(oids)} merged PR head(s) by id")
        else:
            self._context.log(f"  worktree: could not fetch PR heads — {scrub_home((err or '').strip()[:200])}")

    def _maybe_refresh_prs(self, fetch_at: float | None, watched: Iterable[str] = ()) -> None:
        """Schedule the PR index's own refresh when it is due — called from the
        harbor runner, so only while someone is looking. Due: never fetched,
        older than `PR_INDEX_TTL_SECONDS`, older than a terminal's last fetch
        (and `PR_INDEX_MIN_INTERVAL_SECONDS` old), or written before
        `head_oid` / `base_ref` existed. Never while FETCH runs (it refreshes
        the index itself), never inside a failure's back-off.

        Not due, it still watches the open PRs of `watched` (the branches
        the caller is drawing): `_maybe_watch_open_prs`."""
        fetched_at, prs = self._pr_index.snapshot()
        age = time.time() - fetched_at if fetched_at else None
        due = (
            age is None
            or age >= PR_INDEX_TTL_SECONDS
            or (fetch_at is not None and fetch_at > fetched_at and age >= PR_INDEX_MIN_INTERVAL_SECONDS)
            or any(pr.get("state") == "merged" and (not pr.get("head_oid") or not pr.get("base_ref")) for pr in prs.values())
        )
        if due:
            self._start_pr_job(self._pr_refresh_worker)
        else:
            self._maybe_watch_open_prs(prs, watched)

    def _start_pr_job(self, worker: Callable[[], None]) -> bool:
        """Start `worker` as THE PR index job, single flight; False when one
        runs, FETCH runs, or a failure's back-off still holds. The worker
        clears `_pr_refreshing` when it ends."""
        job = self._fetch_jobs.get()
        with self._pr_refresh_lock:
            if self._pr_refreshing or self._clock() < self._pr_refresh_after or (job and job["status"] == "running"):
                return False
            self._pr_refreshing = True
        try:
            self._start_background(worker, "worktree-prs")
        except Exception:  # noqa: BLE001 — thread exhaustion; the next sweep retries
            with self._pr_refresh_lock:
                self._pr_refreshing = False
            return False
        return True

    def _maybe_watch_open_prs(self, prs: dict[str, dict], watched: Iterable[str]) -> None:
        """The open-PR watch (`harbor.PR_OPEN_WATCH_SECONDS` owns why): when the
        index shows an open PR on a watched branch, ask GitHub which PRs are
        still open, once a watch interval, and refresh the index at once if
        one of them is not. A merge made on GitHub then reaches `landed`, and
        the rack's CLEAN, within the interval and with no fetch here."""
        numbers = branches_mod.watched_open_prs(prs, watched)
        if not numbers or self._clock() < self._pr_watch_after:
            return
        if self._start_pr_job(lambda: self._pr_watch_worker(numbers)):
            self._pr_watch_after = self._clock() + PR_OPEN_WATCH_SECONDS

    def _pr_watch_worker(self, numbers: frozenset[int]) -> None:
        """One turn of the watch: the short question, then the whole refresh
        only when a watched PR left the open list. A question GitHub did not
        answer proves nothing and refreshes nothing: the TTL still comes."""
        moved = False
        try:
            moved = self._open_prs_moved(numbers)
        except Exception:  # noqa: BLE001 — a probe crash must not wedge the refresh
            moved = False
        if moved:
            self._pr_refresh_worker()
            return
        with self._pr_refresh_lock:
            self._pr_refreshing = False

    def _open_prs_moved(self, numbers: frozenset[int]) -> bool:
        """Is one of `numbers` no longer open on GitHub? False on every unknown
        (`branches.parse_open_pr_numbers`)."""
        if shutil.which("gh") is None:
            return False
        code, out, _err = create_mod.run_bounded(
            branches_mod.build_open_prs_command(), pr_mod.NETWORK_TIMEOUT_SECONDS, cwd=self._repo_root
        )
        if code != 0:
            return False
        still_open = branches_mod.parse_open_pr_numbers(out)
        return still_open is not None and not numbers <= still_open

    def _pr_refresh_worker(self) -> None:
        ok = False
        try:
            ok = self._refresh_pr_index()
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the refresh
            self._pr_index.set_error(f"{type(exc).__name__}: {exc}")
        finally:
            with self._pr_refresh_lock:
                self._pr_refreshing = False
                if not ok:
                    self._pr_refresh_after = self._clock() + PR_INDEX_TTL_SECONDS
        self._harbor.invalidate()
        meta = self._pr_index.meta()
        self._context.log(
            f"  worktree: PR index refreshed — {meta['count']} PRs{_cut_note(meta)}"
            if ok
            else f"  worktree: PR index refresh failed ({meta['error']})"
        )

    def _fetch_remotes(self) -> bool:
        """`git fetch --prune` EVERY configured remote. False = the primary
        failed and the job is already marked; the caller stops.

        One remote was the bug. `sweep_remote_refs` walks every remote from
        `list_remotes`, so the harbor listed `gitlab/*` rows while FETCH only
        ever pruned `origin` — branches deleted on the second remote months earlier
        kept rendering, because nothing local had been told they were gone.
        A tier the harbor DISPLAYS and never refreshes is a tier that lies.

        The two halves fail differently, on purpose. The primary remote is
        where the base's `↓N behind` and every PR comes from, so its failure
        is the fetch's failure. A secondary is additive — the same posture
        `sweep_remote_refs` takes — so it is NAMED on `remote_error` and the
        rest of the pass still counts.
        """
        primary = pr_mod.DEFAULT_REMOTE
        remotes = branches_mod.list_remotes(self._repo_root)
        if primary not in remotes:
            # No `git remote` answer, or a repo whose primary is not there:
            # try the primary anyway, so the failure names the real reason.
            remotes = [primary] + remotes
        failures: list[str] = []
        fetched: list[str] = []
        for remote in remotes:
            code, _out, err = create_mod.run_bounded(
                branches_mod.build_fetch_command(self._repo_root, remote),
                pr_mod.NETWORK_TIMEOUT_SECONDS,
                env=branches_mod.fetch_env(),
            )
            if code == 0:
                fetched.append(remote)
                continue
            reason = (err or "git fetch failed").strip()[:300]
            if remote == primary:
                self._fetch_failed(reason)
                return False
            failures.append(f"{remote}: {reason}")
        self._fetched_label = ", ".join(fetched) or primary
        self._remote_fetch_error = "; ".join(failures) if failures else None
        return True

    def _fetch_failed(self, error: str) -> None:
        self._fetch_jobs.fail(error)
        self._harbor.invalidate()
        self._context.log(f"  worktree: fetch failed — {error}")

    # -- harbor: scuttle a remote branch -----------------------------------------
    def _handle_scuttle_remote(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`git push <remote> --delete <branch>` — the remote tier's one write.

        The inventory is swept FRESH, the same rule `/delete-branch` follows:
        the cached row promised a verdict about the last four seconds, and a
        branch that was pushed to in between must be refused rather than
        deleted on stale evidence. Here the sweep is LOCAL (`for-each-ref
        refs/remotes/`), so "fresh" means fresh as of the last FETCH — which
        is the strongest claim anything can make without a network read, and
        the reason a stale ref is worth a refusal rather than a guess.

        There is NO force. `decide_delete` has one because a local branch sits
        in the reflog afterwards; a ref deleted on a server other people pull
        from does not, so the only clear answer is a predicate — "this already
        landed" — and a flag that overrode it would be the whole safety
        argument with an off switch.

        The remote is checked against `git remote`, not a pattern. An
        allowlist git itself produces cannot be talked into naming something
        that is not a configured remote, and no regex has to be trusted.
        """
        remote = _text(query, body, "remote")
        branch = _text(query, body, "branch")
        # The existing-ref rule, and the reason it runs FIRST: `git push` has
        # no `--` separator for refspecs, so a leading `-` must never reach
        # the argv (`branches.build_push_delete_command`).
        name_error = validate_existing_branch(branch)
        if name_error is not None:
            return 400, {"error": name_error}
        if not create_mod.tool_found("git"):
            return 503, {"error": "git not found"}
        if remote not in branches_mod.list_remotes(self._repo_root):
            return 400, {"error": f"'{remote}' is not a configured remote"}

        rows, sweep_error = branches_mod.sweep_remote_refs(self._repo_root, self._base, remotes=[remote])
        if sweep_error is not None:
            return 500, {"error": scrub_home(sweep_error)}
        row = next((r for r in rows if r["branch"] == branch), None)
        if row is None:
            return 404, {"error": f"no branch '{branch}' on '{remote}'"}
        self._join_landing([row])
        verdict, reason = branches_mod.scuttle_verdict(row)
        if verdict != "clear":
            return 409, {"error": reason}

        code, _out, err = create_mod.run_bounded(
            branches_mod.build_push_delete_command(self._repo_root, remote, branch),
            branches_mod.SCUTTLE_TIMEOUT_SECONDS,
            env=branches_mod.fetch_env(),
        )
        if code != 0:
            text = scrub_home((err or "git push --delete failed").strip()[:300])
            # A ref the server already lost is the outcome asked for, not a
            # failure: a local tracking ref outlives the branch until a prune.
            if "remote ref does not exist" in text:
                self._harbor.invalidate()
                return 200, {"deleted": False, "ref": f"{remote}/{branch}", "note": "already gone on the remote"}
            return 500, {"error": text}

        self._harbor.invalidate()
        self._context.log(f"  worktree: scuttled {remote}/{branch}")
        return 200, {"deleted": True, "ref": f"{remote}/{branch}"}

    # -- harbor: brief ----------------------------------------------------------
    def _handle_brief(self, query: dict[str, list[str]], body: dict) -> tuple:
        """One branch, or every branch with no brief or a stale one — 202 and
        a worker, the `/fetch` shape. The targets are chosen from the CURRENT
        document (a warming harbor has none, and says so with 409), so the
        response can carry `total` for the meter before the first call runs.

        Never `_managed()`-gated: a brief is a read-only annotation on rows
        `/branches` already lists unfiltered, and it mutates nothing in the repo.
        """
        branch = _text(query, body, "branch") or None
        if branch is not None:
            # An existing row is being annotated, so it is the existing-ref
            # rule here too: a `+` in a name is no reason to refuse a brief.
            branch_error = validate_existing_branch(branch)
            if branch_error is not None:
                return 400, {"error": branch_error}
        if not create_mod.tool_found("git"):
            return 503, {"error": "git not found"}
        call = self._brief_call()
        if isinstance(call, str):
            return 503, {"error": call}
        doc, _age, _error, _refreshing = self._harbor.get()
        if doc is None:
            return 409, {"error": "the harbor is still warming — try again in a moment"}
        targets = brief_mod.select_targets(doc["branches"], self._briefs.snapshot(), only=branch)
        if branch is not None and not targets:
            return 404, {"error": "no such branch on the harbor (the base is never briefed)"}
        if not self._brief_jobs.claim(len(targets)):
            job = self._brief_jobs.get() or {}
            return 202, {"status": "running", "started": False, "done": job.get("done", 0), "total": job.get("total", 0)}
        if not targets:
            self._brief_jobs.finish("nothing to brief — every branch is current")
            return 202, {"status": "done", "started": True, "done": 0, "total": 0}
        worker = threading.Thread(
            target=self._brief_worker, args=(targets, doc["branches"], call), daemon=True, name="worktree-brief"
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            self._brief_jobs.fail(f"could not start: {type(exc).__name__}: {exc}")
            return 500, {"error": "could not start the brief job"}
        return 202, {"status": "running", "started": True, "done": 0, "total": len(targets)}

    def _brief_call(self) -> dict | str:
        """The host's `headless_call` for a brief, on the default Claude
        profile (`tldrack/headless.py` owns the recipe), or why not.
        A host without launch profiles cannot build it: restart the bridge."""
        profiles = getattr(self._context, "profiles", None)
        if profiles is None or not hasattr(profiles, "headless_call"):
            return "this bridge cannot make headless calls — restart it"
        call = profiles.headless_call(
            None,
            system_prompt=brief_mod.SYSTEM_PROMPT,
            model=brief_mod.resolve_model(),
            budget_usd=brief_mod.PER_CALL_BUDGET_USD,
            json_schema=brief_mod.BRIEF_SCHEMA,
            cli_bin=_resolve_claude_bin(),
        )
        return call if call is not None else "the `claude` CLI was not found on PATH"

    def _brief_worker(self, targets: list[str], rows: list[dict], call: dict) -> None:
        """One `claude -p` per target, sequential, each landing in the store
        as it is made; the cap table (`brief.decide_run`) is asked before every
        call. A branch that fails is skipped with its reason kept as the job's
        `error` only if NONE succeeded — one bad branch must not read as a
        failed run.
        """
        base_sha = brief_mod.base_sha_of(rows)
        by_branch = {row["branch"]: row for row in rows}
        model = brief_mod.resolve_model()
        max_calls = brief_mod.resolve_max_calls()
        max_cost = brief_mod.resolve_max_cost_usd()
        done, cost, made, last_error = 0, 0.0, 0, None
        try:
            for branch in targets:
                note = brief_mod.decide_run(done=done, cost_usd=cost, max_calls=max_calls, max_cost_usd=max_cost)
                if note:
                    self._brief_jobs.finish(note)
                    self._context.log(f"  worktree: brief {note}")
                    return
                self._brief_jobs.advance(branch, done, cost)
                row = by_branch.get(branch)
                if row is None:
                    done += 1
                    continue
                entry, error = brief_mod.brief_one(self._repo_root, self._base, base_sha, row, call=call, model=model)
                done += 1
                if entry is None:
                    last_error = error
                    self._context.log(f"  worktree: brief '{branch}' failed — {error}")
                    continue
                cost += entry["cost_usd"]
                made += 1
                self._briefs.set(entry)
                self._brief_jobs.advance(branch, done, cost)
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the job
            self._brief_jobs.fail(f"{type(exc).__name__}: {exc}")
            self._context.log(f"  worktree: brief job crashed — {exc}")
            return
        if made == 0 and last_error:
            self._brief_jobs.fail(last_error)
            self._briefs.set_error(last_error)
        else:
            self._brief_jobs.finish(f"{done - made} skipped — {last_error}" if last_error else None)
        self._context.log(f"  worktree: briefed {made}/{len(targets)} branches (${cost:.3f}, {model})")

    # -- harbor: delete-branch ------------------------------------------------
    def _handle_delete_branch(self, query: dict[str, list[str]], body: dict) -> tuple:
        return self._delete_branch(_text(query, body, "branch"), force=_flag(query, body, "force"))

    def _delete_branch(self, branch: str, *, force: bool) -> tuple:
        """`git branch -d` (or `-D` with `force`), synchronous and bounded —
        `POST /delete-branch`, and the last step of `/cleanup`.

        The inventory is swept FRESH here — `_fresh_row` (`branches.sweep_refs`
        plus `git worktree list`, ~25 ms) — never read from the harbor cache;
        the note there owns why.
        """
        # The EXISTING-ref rule, not the minting alphabet — `branches
        # .decide_delete` owns why, and `delete_verdict` must ask the same
        # question or the row and the click disagree.
        name_error = validate_existing_branch(branch)
        _rows, row, failure = self._fresh_row(branch, name_error)
        if failure is not None:
            return failure
        checked_out_at = row.get("worktree_path") if row else None
        if row is not None and not checked_out_at:
            held = worktree_by_branch(list_worktrees(self._repo_root)).get(branch)
            checked_out_at = held.get("path") if held else None

        refusal = branches_mod.decide_delete(
            name_error=name_error,
            known=row is not None,
            is_base=bool(row and row["is_base"]),
            creating=(self._creates.get(branch) or {}).get("status") == "creating",
            checked_out_at=checked_out_at,
            managed=is_managed_path(checked_out_at, self._root) if checked_out_at else False,
            merged=row["merged"] if row else None,
            ahead=row["ahead"] if row else None,
            force=force,
            base=self._base,
            landed=bool(row and row.get("landed") is True),
            contained=bool(row and row.get("contained_in")),
        )
        if refusal is not None:
            return refusal
        # A group worker whose landed branch goes now keeps its delivery: the
        # runner's next landing sweep would no longer find the branch.
        if row is not None and row.get("landed") is True:
            self._groups.reconcile_landing(own_landings({branch: {"landed": True, "landing": row.get("landing")}}))

        # A forced delete of work nothing PROVED safe pins its tip first, and
        # refuses when it cannot (`archive.py` owns why).
        pinned = None
        if force and row.get("landed") is not True and not row.get("contained_in"):
            pinned, pin_failure = self._pin_archive(branch, row.get("oid"))
            if pin_failure is not None:
                return pin_failure

        landed_via_pr = row.get("landed") is True and row["merged"] is not True
        dash_d_upper = branches_mod.needs_dash_d_upper(
            force=force, merged=row["merged"], landed=row.get("landed") is True, contained=bool(row.get("contained_in"))
        )
        code, _out, err = create_mod.run_bounded(
            branches_mod.build_branch_delete_command(self._repo_root, branch, dash_d_upper),
            branches_mod.GIT_LOCAL_TIMEOUT_SECONDS,
        )
        if code != 0:
            text = scrub_home((err or "git branch -d failed").strip()[:300])
            # git's own belt-and-braces for the merge check, worded its way.
            if "not fully merged" in text and not force:
                return 409, {"error": text, "needs_force": True}
            return 500, {"error": text}

        # A re-created branch of the same name must not inherit this one's
        # PR link or a stale create or clean-up error — `/remove`'s rule.
        self._store.drop(branch)
        self._creates.drop(branch)
        self._cleanups.drop_settled(branch)
        # The RECORD goes; the adopt anchor (`adopt.adopt_ref_for`) deliberately stays.
        # A re-created branch of this name must not inherit a stale "already
        # rescued" claim, but the rescued work itself is the last copy there is.
        self._adopts.drop(branch)
        self._invalidate()
        landing = row.get("landing") or {}
        if force:
            how = f" (forced; tip pinned at {pinned})" if pinned else " (forced)"
        elif row.get("contained_in") and row.get("landed") is not True:
            how = f" (every commit is on '{row['contained_in']}')"
        elif landed_via_pr and landing.get("pr"):
            how = f" (landed via PR #{landing['pr']})"
        elif landed_via_pr:
            how = f" (landed — {landing.get('how') or 'pr'})"
        else:
            how = ""
        self._context.log(f"  worktree: deleted branch '{branch}'{how}")
        return 200, {"branch": branch, "deleted": True, "forced": force, "archived": pinned}

    # -- harbor: the archive (archive.py) -------------------------------------
    def _archived(self) -> list[dict]:
        """Every pinned tip, for the harbor's ARCHIVED fold."""
        return archive_mod.list_archived(self._repo_root)

    def _archive_pins(self, branch: str) -> list[dict]:
        return archive_mod.pins_for(self._repo_root, branch)

    def _pin_archive(self, branch: str, tip: object) -> tuple[str | None, tuple | None]:
        """`(ref, None)` once `tip` is pinned, or `(None, refusal)`. Every
        unknown refuses, because the delete that follows cannot be undone
        without the pin."""
        if not branches_mod.is_full_oid(tip):
            return None, (409, {"error": "could not read the branch tip to archive it — not deleted"})
        pins = self._archive_pins(branch)
        existing = archive_mod.newest_tip(pins)
        existing_in_tip = None
        if existing is not None and existing != tip:
            code, _out, _err = create_mod.run_bounded(
                branches_mod.build_is_ancestor_command(self._repo_root, existing, tip), branches_mod.GIT_LOCAL_TIMEOUT_SECONDS
            )
            existing_in_tip = True if code == 0 else False if code == 1 else None
        decision = archive_mod.decide_pin(existing=existing, tip=tip, existing_in_tip=existing_in_tip)
        if decision == "conflict":
            return None, (
                409,
                {
                    "error": f"an archived '{branch}' with other commits already exists — restore or forget it first",
                    "archive_conflict": True,
                },
            )
        if decision == "write":
            code, _out, err = create_mod.run_bounded(
                archive_mod.build_pin_command(self._repo_root, branch, tip, archive_mod.written_pin(pins, branch)),
                branches_mod.GIT_LOCAL_TIMEOUT_SECONDS,
            )
            if code != 0:
                return None, (
                    500,
                    {
                        "error": "could not archive the tip — not deleted: "
                        + scrub_home((err or "git update-ref failed").strip()[:240])
                    },
                )
        return archive_mod.archive_ref(branch), None

    def _handle_restore_branch(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /restore-branch {branch}` — the archived tip back as
        `refs/heads/<branch>`, then the pin dropped. Never moves an existing
        branch: the create refuses on any branch of that name."""
        branch = _text(query, body, "branch")
        name_error = validate_existing_branch(branch)
        pins: list[dict] = []
        pinned, exists = None, False
        if name_error is None:
            if not create_mod.tool_found("git"):
                return 503, {"error": "git not found"}
            pins = self._archive_pins(branch)
            pinned = archive_mod.newest_tip(pins)
            code, _out, _err = create_mod.run_bounded(
                ["git", "-C", str(self._repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                branches_mod.GIT_LOCAL_TIMEOUT_SECONDS,
            )
            exists = code == 0
        refusal = archive_mod.decide_restore(
            name_error=name_error, pinned=pinned, branch_exists=exists, is_base=branch == self._base
        )
        if refusal is not None:
            return refusal
        code, _out, err = create_mod.run_bounded(
            archive_mod.build_restore_command(self._repo_root, branch, pinned), branches_mod.GIT_LOCAL_TIMEOUT_SECONDS
        )
        if code != 0:
            return 409, {"error": scrub_home((err or "git update-ref failed").strip()[:300])}
        # Best effort: a pin left behind is a harmless second copy of a tip
        # that is a branch again.
        for ref in archive_mod.refs_holding(pins, pinned):
            create_mod.run_bounded(
                archive_mod.build_unpin_command(self._repo_root, ref, pinned), branches_mod.GIT_LOCAL_TIMEOUT_SECONDS
            )
        self._invalidate()
        self._context.log(f"  worktree: restored branch '{branch}' at {pinned[:9]} from the archive")
        return 200, {"branch": branch, "restored": True, "sha": pinned[:9]}

    def _handle_forget_archived(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /forget-archived {branch, sha}` — drop one pin, the tip the
        caller was shown and no other. Its commits become collectable."""
        branch = _text(query, body, "branch")
        expected = _text(query, body, "sha")
        name_error = validate_existing_branch(branch)
        pins: list[dict] = []
        pinned = None
        if name_error is None:
            if not create_mod.tool_found("git"):
                return 503, {"error": "git not found"}
            pins = self._archive_pins(branch)
            pinned = archive_mod.newest_tip(pins)
        refusal = archive_mod.decide_forget(name_error=name_error, pinned=pinned, expected=expected)
        if refusal is not None:
            return refusal
        for ref in archive_mod.refs_holding(pins, pinned):
            code, _out, err = create_mod.run_bounded(
                archive_mod.build_unpin_command(self._repo_root, ref, pinned), branches_mod.GIT_LOCAL_TIMEOUT_SECONDS
            )
            if code != 0:
                return 500, {"error": scrub_home((err or "git update-ref -d failed").strip()[:300])}
        self._invalidate()
        self._context.log(f"  worktree: forgot archived '{branch}' ({pinned[:9]})")
        return 200, {"branch": branch, "forgotten": True}

    # -- harbor: prune-worktrees ---------------------------------------------
    def _handle_prune_worktrees(self) -> tuple:
        """`git worktree prune -v`: drop the bookkeeping for worktrees whose
        directory is gone. Touches no directory and no branch — the one
        harbor mutation that cannot lose work."""
        if not create_mod.tool_found("git"):
            return 503, {"error": "git not found"}
        code, out, err = create_mod.run_bounded(
            branches_mod.build_prune_command(self._repo_root), branches_mod.GIT_LOCAL_TIMEOUT_SECONDS
        )
        if code != 0:
            return 500, {"error": scrub_home((err or "git worktree prune failed").strip()[:300])}
        pruned = branches_mod.parse_prune_output(out)
        self._invalidate()
        if pruned:
            self._context.log(f"  worktree: pruned {', '.join(pruned)}")
        return 200, {"pruned": pruned, "count": len(pruned)}

    # -- harbor: remove a landed worktree we did not create --------------------
    def _outside_refusal(
        self, row: dict, worktree: dict, *, creating: bool, dirty: object = "cached", occupants: object = "cached"
    ) -> tuple | None:
        """`landing.decide_outside_remove` for one assembled row. The row's
        cached facts by default (the sweep's promise); the route passes the
        fresh probes in."""
        return landing_mod.decide_outside_remove(
            name_error=validate_existing_branch(row.get("branch")),
            held_path=worktree.get("path"),
            is_main=bool(worktree.get("is_main")),
            managed=bool(worktree.get("managed")),
            creating=creating,
            locked=bool(worktree.get("locked")),
            landed=row.get("landed") is True,
            contained=bool(row.get("contained_in")),
            dirty=worktree.get("dirty") if dirty == "cached" else dirty,
            occupants=worktree.get("occupied_by") if occupants == "cached" else occupants,
        )

    def _handle_remove_landed_worktree(self, query: dict[str, list[str]], body: dict) -> tuple:
        """`POST /remove-landed-worktree {branch}` — `git worktree remove` for a
        tree OUTSIDE the managed root whose branch is already in main.

        CLEANUP may take a clean, landed, unoccupied tree wherever it lives (a
        build script that leaves its trees under `/tmp`, say), a rule since
        2026-10-08. `landing.decide_outside_remove` is the whole gate, and
        it runs on a FRESH sweep and FRESH probes (`git status`, `lsof`), never
        the row's 20 s-old facts. There is no force: git's own refusal of a
        dirty tree is the last line, not the first. `_managed()` and
        `POST /remove` are untouched — they still gate `/pr` and the forced
        remove. The branch is kept; the cleanup walk deletes it next.
        """
        branch = _text(query, body, "branch")
        name_error = validate_existing_branch(branch)
        _rows, row, failure = self._fresh_row(branch, name_error)
        if failure is not None:
            return failure
        if name_error is None and row is None:
            return 404, {"error": "no local branch by that name"}
        records = list_worktrees(self._repo_root)
        held = worktree_by_branch(records).get(branch) if name_error is None else None
        abs_path = (held or {}).get("path")
        worktree = {
            "path": abs_path,
            "locked": bool((held or {}).get("locked")),
            "is_main": is_primary_checkout(abs_path, main_worktree_path(records)),
            "managed": is_managed_path(abs_path, self._root) if abs_path else False,
        }
        creating = (self._creates.get(branch) or {}).get("status") == "creating"
        row = row or {"branch": branch}
        # Every cheap row first, so the probes only run for a tree that could
        # otherwise go; then the table again with what the probes said.
        refusal = self._outside_refusal(row, worktree, creating=creating, dirty=False, occupants=[])
        if refusal is None:
            dirty = branches_mod.dirty_sweep([abs_path], DIRTY_SWEEP_BUDGET_SECONDS).get(abs_path)
            occupants = landing_mod.sweep_occupants([abs_path]).get(abs_path)
            refusal = self._outside_refusal(row, worktree, creating=creating, dirty=dirty, occupants=occupants)
        if refusal is not None:
            status, payload = refusal
            return status, {**payload, "error": scrub_home(payload["error"])}

        code, _out, err = create_mod.run_bounded(
            create_mod.build_worktree_remove_command(self._repo_root, Path(abs_path), force=False), create_mod.GIT_TIMEOUT_SECONDS
        )
        if code != 0:
            return 409, {"error": scrub_home((err or "git worktree remove failed").strip()[:300])}
        self._invalidate()
        self._context.log(f"  worktree: removed landed worktree {display_path(abs_path)} ('{branch}')")
        return 200, {"branch": branch, "removed": True}


def _cut_note(meta: dict) -> str:
    """The log's suffix for a PR list that filled `branches.PR_LIST_CEILING`."""
    return f" (CUT at {branches_mod.PR_LIST_CEILING} — oldest PRs missing)" if meta["truncated"] else ""


def own_landings(judged: dict[str, dict]) -> dict[str, dict]:
    """`{branch: {how, pr}}` for each branch of `_state_landing`'s verdict whose OWN work landed
    (`landing.work_landed`) — what delivers a group worker (`TaskGroups.reconcile_landing`)."""
    return {
        branch: verdict["landing"]
        for branch, verdict in judged.items()
        if verdict.get("landed") is True and landing_mod.work_landed(verdict.get("landing"))
    }


def tree_pr(indexed: dict | None, job: dict | None) -> dict | None:
    """A tree's PR for `/state`: `{number, state, url}`, or None for none known.

    The PR button's own outcome wins while it says `open`, since the index learns of a new PR only on its next
    refresh; otherwise the index's row (`gh pr list`, every state), which also knows PRs made outside the button.
    Once the index says THAT pull request (the same url) is merged or closed, the index is the newer word: the
    button's record stays `open` for good, and a tree whose PR merged read `open` until its branch was removed.
    """
    over = (
        indexed is not None
        and job is not None
        and indexed.get("url") == job.get("url")
        and indexed.get("state") in ("merged", "closed")
    )
    if not over and job and job.get("status") == "open" and isinstance(job.get("url"), str):
        number = indexed.get("number") if indexed else None
        return {"number": number, "state": "open", "url": job["url"]}
    if indexed and indexed.get("state") in ("open", "merged", "closed"):
        return {"number": indexed.get("number"), "state": indexed["state"], "url": indexed.get("url")}
    return None


def _cleanup_fields(job: dict | None) -> dict:
    """A `/state` row's cleanup progress: `cleanup_status` (`running` ·
    `error` · None) and `cleanup_error` — on every row, so a client reads
    absent as an older plugin rather than as "no cleanup"."""
    return {"cleanup_status": job["status"] if job else None, "cleanup_error": job["error"] if job else None}


def _non_main_trees(records: list[dict]) -> list[dict]:
    """Every worktree but the main checkout, `~`-collapsed, with its branch
    (`None` when detached) — the rack's island chip names the branch a session
    stands on (the UI's `rack-view.ts`, `island_for_session`)."""
    main_path = main_worktree_path(records)
    return [
        {"path": display_path(record["path"]), "branch": record.get("branch") or None}
        for record in records
        if record.get("path") and not is_primary_checkout(record["path"], main_path)
    ]


def gh_found() -> bool:
    """Is GitHub's CLI on PATH? Optional: without it the PR verbs (`/pr`, SHIP,
    the PR index) refuse with a sentence and every other route works."""
    return shutil.which("gh") is not None


def inline_env() -> dict[str, str]:
    """Env vars that have to ride INLINE in the spawned command — the fallback
    for a host without launch profiles, whose `spawn_argv` owns this now.

    A tmux session inherits the tmux SERVER's environment, not the bridge's, and
    the server is usually much older than the bridge. Same reasoning and same
    variable as `coordinator/resume.py::inline_env`.
    """
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return {"CLAUDE_CONFIG_DIR": value} if value else {}


def _resolve_claude_bin() -> str | None:
    """`WORKTREE_CLAUDE_BIN`, else PATH, else `~/.local/bin/claude` — the
    fallback plugins/coordinator/coordinator/claude_cli.py explains:
    the bridge is often launched from a GUI app, whose PATH lacks the
    directory the installer actually uses."""
    override = os.environ.get("WORKTREE_CLAUDE_BIN")
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.is_file() else None


# ── request-value helpers (query string wins; body is the fallback) ─────────


def _raw(query: dict[str, list[str]], body: dict, name: str):
    values = query.get(name)
    if values:
        return values[0]
    return body.get(name)


def _text(query: dict[str, list[str]], body: dict, name: str) -> str:
    raw = _raw(query, body, name)
    return raw.strip() if isinstance(raw, str) else ""


# jscpd:ignore-start
# A declared cross-package port: owned by plugins/README.md — see the note there.
def _flag(query: dict[str, list[str]], body: dict, name: str) -> bool:
    # Valueless query keys (`?force` vs `?force=1`): owned by
    # plugins/coordinator/coordinator/plugin.py — see the note there.
    if name in query:
        return (query.get(name) or [""])[0].strip().lower() not in ("0", "false", "no")
    value = body.get(name)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return value is True


# jscpd:ignore-end
