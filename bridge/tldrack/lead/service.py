"""The LEAD routes' logic and the watcher's loop, wired to the bridge by main.py through `Bridge`.

main.py stays thin: every `/lead/*` and `/inbox/*` route hands its body here and sends back the
`(status, body)` it gets. The service never reaches into the HTTP layer and the bridge's stores
reach it only through the `Bridge` callables, so a test builds one with fakes and drives a whole
run — spawn, ready, close, an answer that reopens, a lead killed and resumed — with no tmux, git or
network (tests/test_lead_*.py).

Who may call what (the caller is the session the CLI runs in, resolved from its pane by main.py):
- a LEAD verb must come from the live session that wears the run's lead identity;
- a WORKER verb must come from the live session recorded as that step's worker (name and lineage);
- the operator's verbs are not here: they are inbox.py's, behind decision_secret.py.
Labels, not credentials: an agent shell runs as the same user (decision_secret.py owns that
caveat). The guard that matters is that every verb is re-checked against the stored state.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tldrack import routing
from tldrack.attention import running_descendants
from tldrack.inbox import Inbox, InboxError
from tldrack.lead import brief
from tldrack.lead.close import CloseFacts, decide_close, publication, tree_clean
from tldrack.lead.models import LeadModels, pair_error, renamed
from tldrack.lead.names import lead_name
from tldrack.lead.runs import FINAL, SPAWNABLE, LeadError, LeadRuns, branch_for, deps_met, next_actions
from tldrack.lead.subscription import SubscriptionGate
from tldrack.lead.watcher import TICK_SECONDS, worker_facts, observe
from tldrack.pane_typist import UNSUBMITTED, PaneTypist

BODY_MAX = 600 * 1024
#: Every `POST /lead/<verb>`; each is a `verb_<name>` method below. `launch` is the operator's (the rack's
#: LEAD fork and RESUME), the rest are the CLI's.
LEAD_VERBS = (
    "launch",
    "models",
    "status",
    "start",
    "step",
    "spawn",
    "tell",
    "escalate",
    "close",
    "peek",
    "abandon",
    "end",
    "adopt",
    "cap",
    "brief",
    "ask",
    "progress",
    "ready",
    "report",
)
MAX_PLAN_BYTES = 256 * 1024
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
#: The tool that starts a subagent (`Task` is its older name), and the one type that always runs on
#: its parent's model. Why a capped lead may not use it: owned by lead/models.py — see the note there.
AGENT_TOOLS = frozenset({"Agent", "Task"})
FORK_TYPE = "fork"
NO_PAIR = {"model": None, "worker_model": None}
WORKER_STAGES = ("research", "implement", "validate", "blocked", "failed")
SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
GIT_TIMEOUT = 30
#: Every worker, Claude or Codex, starts in the CLI's automatic-approval mode, so the operator never
#: switches one by hand. What it means per CLI: owned by `tldrack/profiles.py`
#: `PERMISSION_ARGS` — see the note there.
WORKER_PERMISSION_MODE = "auto"
#: A queued line held this long shows on its step's `lead status` line (a flush is every 5 s).
WAITING_SHOWN_SECONDS = 60.0


@dataclass
class Bridge:
    """Everything the service needs from the running bridge, as plain callables."""

    rows: Callable[[], list[dict]]  # /snapshot session rows
    summaries: Callable[[], list[dict]]  # /snapshot `profiles`
    ranked: Callable[[], list[str]]  # launch_pick's order (best first)
    kind_of: Callable[[str], str | None]
    prepare_lineage: Callable[[], dict]
    # name, profile, prompt, lineage; `model=` / `subagent_model=` only when the launch names one
    spawn: Callable[..., tuple[int, dict]]
    kill: Callable[[str], tuple[int, dict]]
    restamp: Callable[[str, dict], bool]  # set a live session's lineage stamp
    worktree: Callable[[], Any]  # the mounted worktree plugin, or None
    closing: Callable[[str], str | None]  # a session's last assistant message
    notify: Callable[[str, str], None]  # one pointer push
    git: Callable[[list[str]], tuple[int, str]]
    start_dir: str | None
    # A lead's rack label from its run's title (session_names.py `title`); the label, or None.
    label_lead: Callable[[str, str], str | None] = lambda session, title: None
    label_worker: Callable[[str, str], None] = lambda session, prompt: None
    on_change: Callable[[], None] = lambda: None
    log: Callable[[str], None] = print
    # profiles.ProfileRegistry.model_refusal: why a profile cannot launch on a model pair, or None.
    model_refusal: Callable[..., str | None] = lambda profile, model=None, subagent_model=None: None


class LeadService:
    def __init__(
        self,
        *,
        runs: LeadRuns,
        inbox: Inbox,
        typist: PaneTypist,
        gate: SubscriptionGate,
        bridge: Bridge,
        run_root: Path,
        routing_table: Callable[[], dict] = routing.load_routing,
        clock: Callable[[], float] = time.time,
        models: LeadModels | None = None,
    ) -> None:
        self.runs, self.inbox, self.typist, self.gate, self.bridge = runs, inbox, typist, gate, bridge
        self.run_root = run_root
        self._routing = routing_table
        self._clock = clock
        # None (a test-built service): no pair, so a launch names no model, as before lead/models.py.
        self.models = models
        self._roles: dict[str, tuple] = {}
        self._capped: set[str] = set()  # live leads launched with a forced subagent model
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        inbox.add_listener(runs.on_decision)

    # ── identity ──────────────────────────────────────────────────────────────

    def _live(self) -> dict[str, dict]:
        return {r["session"]: r for r in self.bridge.rows() if r.get("live") and isinstance(r.get("session"), str)}

    @staticmethod
    def _identity(row: Mapping | None) -> str | None:
        return ((row or {}).get("lineage") or {}).get("id")

    def _caller(self, caller: str | None, live: Mapping[str, dict]) -> dict:
        if not caller or not NAME_RE.fullmatch(caller):
            raise LeadError("run this from your rack session (no tmux pane was named)", 403)
        row = live.get(caller)
        if row is None or self._identity(row) is None:
            raise LeadError(f"'{caller}' is not a live rack session with a lineage stamp", 403)
        return row

    def _lead(self, caller: str | None, live: Mapping[str, dict]) -> tuple[dict, dict]:
        row = self._caller(caller, live)
        run = self.runs.run_for_lead(self._identity(row))
        if run is None:
            raise LeadError(f"'{caller}' leads no open run (`tldrack lead start` or `adopt`)", 409)
        if run["lead_session"] != caller:
            self.runs.set_run(run["id"], lead_session=caller)
            run = self.runs.run(run["id"])
        return row, run

    def _worker(self, caller: str | None, live: Mapping[str, dict]) -> tuple[dict, dict, dict]:
        row = self._caller(caller, live)
        step = self.runs.worker_step(caller, self._identity(row))
        if step is None:
            raise LeadError(f"'{caller}' is not a worker on an open LEAD run", 403)
        return row, self.runs.run(step["run_id"]), step

    def _lead_row(self, run: Mapping, live: Mapping[str, dict]) -> dict | None:
        return next((r for r in live.values() if self._identity(r) == run["lead_identity"]), None)

    # ── the worktree plugin ───────────────────────────────────────────────────

    def _wt(self, method: str, path: str, body: dict | None = None, *, repo: str | None = None) -> tuple[int, dict]:
        """One worktree-plugin call. `repo` is the run's repository key: it rides in the query (GET) or the
        body (POST), the two places the plugin's router reads it. None sends none, so the plugin's first
        repository answers, as it did for every run before runs named one."""
        plugin = self.bridge.worktree()
        if plugin is None:
            return 503, {"error": "the worktree plugin is not mounted"}
        try:
            if method == "POST":
                result = plugin.handle_post(path, {}, {**(body or {}), **({"repo": repo} if repo else {})})
            else:
                result = plugin.handle_get(path, {"repo": [repo]} if repo else {})
        except Exception as err:  # a plugin fault is a refusal here, never a bridge crash
            return 500, {"error": f"worktree plugin: {type(err).__name__}: {err}"[:300]}
        status, payload = result[0], result[1]
        return status, payload if isinstance(payload, dict) else {}

    def _groups(self, repo: str | None = None) -> dict[str, dict]:
        status, payload = self._wt("GET", "/groups", repo=repo)
        groups = payload.get("groups") if status == 200 else None
        return {g["id"]: g for g in groups if isinstance(g, dict) and "id" in g} if isinstance(groups, list) else {}

    def _workers(self, run: Mapping, groups: Mapping[str, dict] | None = None) -> dict[str, dict]:
        group = (groups if groups is not None else self._groups(run.get("repo"))).get(run.get("group_id") or "")
        return {w["branch"]: w for w in (group or {}).get("workers", []) if isinstance(w, dict)}

    def _trees(self, repo: str | None = None) -> dict[str, dict]:
        status, payload = self._wt("GET", "/state", repo=repo)
        rows = payload.get("worktrees") if status == 200 else None
        return {r["branch"]: r for r in rows if isinstance(r, dict) and r.get("branch")} if isinstance(rows, list) else {}

    def _repos(self) -> list[dict] | None:
        """The plugin's `/repos` rows (key, label, `~`-collapsed path and worktree root, default), or None
        when no plugin that names repositories is mounted (none at all, or one from before `/repos`)."""
        status, payload = self._wt("GET", "/repos")
        rows = payload.get("repos") if status == 200 else None
        if not isinstance(rows, list):
            return None
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str) and isinstance(r.get("path"), str)]

    @staticmethod
    def _default_repo(repos: list[dict]) -> dict | None:
        return next((r for r in repos if r.get("default")), repos[0] if repos else None)

    def _repo_of_path(self, value: str, repos: list[dict]) -> dict | None:
        """The served repository `value` is inside: its main checkout, its worktree root, or (asked of git)
        any worktree of it. None for anything else, a relative path included."""
        target = _absolute(value)
        if target is None:
            return None
        bases = [(r, [b for b in (_absolute(r["path"]), _absolute(r.get("worktree_root"))) if b]) for r in repos]
        for row, roots in bases:
            if any(target == base or base in target.parents for base in roots):
                return row
        code, common = self.bridge.git(["git", "-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir"])
        main = _absolute(common.strip()) if code == 0 and common.strip() else None
        if main is None:
            return None
        return next((row for row, _roots in bases if main.parent == _absolute(row["path"])), None)

    def _resolve_repo(self, wanted: object, row: Mapping) -> str | None:
        """The repository a new run leads, as the plugin's key. `--repo` (a key or a path) wins; else the
        repository the lead's working directory is in, when the plugin serves it; else the plugin's first.

        Stored on the run (rack_db.py V4) and sent on every plugin call the run makes, so the run stays on
        its repository if the operator reorders `plugins.worktree.repos` later. None only when no plugin
        names repositories: the run then sends none, which is exactly what runs did before this."""
        if wanted is not None and (not isinstance(wanted, str) or not wanted.strip() or len(wanted) > 4096):
            raise LeadError("--repo is a repository key or a path")
        repos = self._repos()
        if not repos:
            if wanted is not None:
                raise LeadError(
                    "the worktree plugin names no repositories (not mounted, or too old): restart the bridge, "
                    "or start without --repo",
                    503,
                )
            return None
        if wanted is not None:
            found = next((r for r in repos if r["key"] == wanted), None) or self._repo_of_path(wanted, repos)
            if found is None:
                served = ", ".join(f"{r['key']} ({r.get('path')})" for r in repos)
                raise LeadError(
                    f"the worktree plugin does not serve {wanted!r}. It serves: {served}. A repository is added "
                    "to `plugins.worktree.repos` in the operator's settings.json, then the bridge restarted",
                )
            return found["key"]
        cwd = row.get("cwd")
        found = self._repo_of_path(cwd, repos) if isinstance(cwd, str) else None
        return (found or self._default_repo(repos) or {}).get("key")

    def _repo_root(self, run: Mapping) -> str:
        """The main checkout of the run's repository: where its workers branch from. A run with no repo
        (started before V4, or on a plugin without `/repos`) reads as the plugin's first repository."""
        key = run.get("repo")
        repos = self._repos()
        if repos:
            row = next((r for r in repos if r["key"] == key), None) if key else self._default_repo(repos)
            if row is None:
                raise LeadError(
                    f"{run['id']} leads {key}, which the worktree plugin no longer serves (`plugins.worktree.repos`)",
                    409,
                )
            root = _absolute(row["path"])
            if root is not None:
                return str(root)
        if key:
            raise LeadError(f"{run['id']} leads {key}, but the worktree plugin names no repositories", 503)
        if not self.bridge.start_dir:
            raise LeadError("the bridge has no start dir to branch from", 503)
        return self.bridge.start_dir

    def _repo_line(self, run: Mapping) -> str:
        """`lead status`'s repository: the label and key, or what a run with none reads as."""
        repos = self._repos() or []
        key = run.get("repo")
        row = next((r for r in repos if r["key"] == key), None) if key else self._default_repo(repos)
        if row is None:
            return f"{key} (not served by the worktree plugin)" if key else "the worktree plugin's first repository"
        name = f"{row.get('label') or row['key']} ({row['key']}) · {row.get('path')}"
        return name if key else f"{name} (the first repository: this run names none)"

    def _origin_base(self, run: Mapping) -> str:
        root = self._repo_root(run)
        base = run["base"]
        ref = f"refs/remotes/origin/{base}"
        code, _ = self.bridge.git(["git", "-C", root, "fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{base}:{ref}"])
        if code:
            raise LeadError(f"could not fetch origin/{base}", 503)
        code, sha = self.bridge.git(["git", "-C", root, "rev-parse", ref])
        if code or not SHA_RE.fullmatch(sha):
            raise LeadError(f"could not read origin/{base}", 503)
        return sha

    # ── dispatch ──────────────────────────────────────────────────────────────

    def handle(self, verb: str, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        handler = getattr(self, f"verb_{verb.replace('-', '_')}", None)
        if handler is None:
            return 404, {"ok": False, "error": f"no lead verb '{verb}'"}
        try:
            status, body = handler(payload, caller)
        except InboxError as err:
            return err.status, {"ok": False, "error": str(err)}
        self.bridge.on_change()
        return status, body

    # ── the operator's door: launch / resume ───────────────────────────────────

    def _pair(self) -> dict:
        return self.models.pair() if self.models is not None else dict(NO_PAIR)

    def verb_models(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        """`POST /lead/models {model?, worker_model?}` — the operator's standing pair (lead/models.py).
        Refused from a rack pane: an agent does not pick its own worker's model. A label, like every
        caller check here (see the module docstring)."""
        if caller is not None:
            raise LeadError("the LEAD model pair is the operator's: set it from the rack UI's LEAD row", 403)
        if self.models is None:
            raise LeadError("this bridge keeps no LEAD model pair", 503)
        try:
            pair = self.models.set(payload)
        except ValueError as err:
            raise LeadError(str(err)) from err
        return 200, {"ok": True, **pair}

    def verb_launch(self, payload: Mapping[str, Any], _caller: str | None) -> tuple[int, dict]:
        """`POST /lead/launch {profile, resume?, model?, worker_model?}` — a new LEAD (or a fresh one
        for a run whose lead is gone). Always a Claude subscription, decided 2026-11-04
        (like AUTO, a LEAD may run on any Claude subscription). The model pair: a
        RESUME takes its run's, a new lead the request's keys over the standing pair (lead/models.py)."""
        payload = renamed(payload)  # a legacy `crew_model` key (lead/models.py, step 6.8)
        wanted = payload.get("profile")
        if not isinstance(wanted, str) or not wanted:
            raise LeadError('profile is required ("auto" or a Claude profile id)')
        profile = self._pick_claude() if wanted == "auto" else wanted
        if profile is None:
            raise LeadError("no Claude account can take a lead right now", 503)
        if self.bridge.kind_of(profile) != "claude":
            raise LeadError("a LEAD is always a Claude subscription; pick AUTO or a Claude account")
        refusal = self.gate.refusal(profile)
        if refusal:
            raise LeadError(refusal, 403)
        live = self._live()
        resume = payload.get("resume")
        run = None
        if resume is not None:
            run = self.runs.run(str(resume))
            if run["state"] != "open":
                raise LeadError(f"{resume} has ended", 409)
            if self._lead_row(run, live) is not None:
                raise LeadError(f"{resume} already has a live lead", 409)
            lineage = {"id": run["lead_identity"]}
        else:
            lineage = self.bridge.prepare_lineage()
        if run is not None:
            pair = {"model": run.get("lead_model"), "worker_model": run.get("worker_model")}
        else:
            pair = {**self._pair(), **{k: payload[k] for k in ("model", "worker_model") if k in payload}}
        refusal = pair_error(pair["model"], pair["worker_model"]) or self.bridge.model_refusal(
            profile, pair["model"], pair["worker_model"]
        )
        if refusal:
            raise LeadError(refusal)
        models = {key: value for key, value in (("model", pair["model"]), ("subagent_model", pair["worker_model"])) if value}
        name = lead_name(profile, self._clock(), live)
        status, body = self.bridge.spawn(name, profile, brief.launch_prompt(run["id"] if run else None), lineage, **models)
        if status >= 400:
            return status, {"ok": False, "error": body.get("error") or "spawn failed"}
        self.runs.note_lead(lineage["id"], name, profile)
        if run is not None:
            self.runs.set_run(run["id"], lead_session=name, profile=profile)
            self.runs.event(run["id"], "resumed", None, {"session": name})
            self.bridge.label_lead(name, run["title"])
        self._refresh_roles(self._live())
        return 201, {"ok": True, "session": name, "profile": profile, "run": run["id"] if run else None, **pair}

    def _pick_claude(self) -> str | None:
        claude = [p for p in self.bridge.ranked() if self.bridge.kind_of(p) == "claude"]
        return claude[0] if claude else None

    # ── the lead's verbs ──────────────────────────────────────────────────────

    def verb_status(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        row = self._caller(caller, live)
        run = self.runs.run_for_lead(self._identity(row))
        if run is None:
            orphans = [r for r in self.runs.runs() if self._lead_row(r, live) is None]
            text = brief.status_text(
                None,
                steps=[],
                step_lines=[],
                questions=[],
                open_decisions=[],
                resolved=[],
                events=[],
                actions=[],
                orphans=orphans,
            )
            return 200, {"ok": True, "run": None, "text": text, "orphans": [o["id"] for o in orphans]}
        view = self._view(run, live)
        events = self.runs.take_undelivered(run["id"])
        text = brief.status_text(
            run,
            steps=view["steps"],
            step_lines=view["lines"],
            questions=view["questions"],
            open_decisions=view["decisions"],
            resolved=view["resolved"],
            events=events,
            actions=view["next"],
            worker_live=view["worker_live"],
            repo=self._repo_line(run),
        )
        return 200, {
            "ok": True,
            "run": run["id"],
            "text": text,
            "next": view["next"],
            "repo": run.get("repo"),
            "base": run["base"],
        }

    def _view(self, run: Mapping, live: Mapping[str, dict]) -> dict:
        steps = self.runs.steps(run["id"])
        workers = self._workers(run)
        parked = self.inbox.parked_steps(run["id"])
        decisions = self.inbox.open_rows(run["id"])
        parked_by = {s: [d["key"] for d in decisions if d["blocking"] and s in d["steps"]] for s in parked}
        worker_rows = {s["step_id"]: self._worker_row(s, live) for s in steps}
        waiting = self._waiting()
        lines = [
            brief.step_line(
                s,
                worker_state=(worker_rows[s["step_id"]] or {}).get("state"),
                worker=workers.get(s.get("branch") or "") or {},
                parked_by=parked_by.get(s["step_id"], []),
                waiting=waiting.get(s.get("worker_session") or ""),
            )
            for s in steps
        ]
        questions = self.runs.questions(run["id"])
        actions = next_actions(run, steps, live=worker_rows, workers=workers, parked=parked, questions=questions)
        worker_live = sum(1 for s in steps if worker_rows[s["step_id"]] is not None or s["state"] == "spawning")
        return {
            "steps": steps,
            "workers": workers,
            "lines": lines,
            "questions": questions,
            "decisions": decisions,
            "resolved": self.inbox.resolved_rows(run["id"]),
            "next": actions,
            "worker_live": worker_live,
            "worker_rows": worker_rows,
            "parked": parked,
        }

    def _waiting(self) -> dict[str, dict]:
        """Per session, the oldest queued line held past `WAITING_SHOWN_SECONDS`, with its `age`: a stuck
        brief shows on its step's `lead status` line instead of as silence."""
        now = self._clock()
        out: dict[str, dict] = {}
        for item in self.typist.pending():
            age = now - item["queued_at"]
            if age >= WAITING_SHOWN_SECONDS and item["session"] not in out:
                out[item["session"]] = {**item, "age": age}
        return out

    def _worker_row(self, step: Mapping, live: Mapping[str, dict]) -> dict | None:
        row = live.get(step.get("worker_session") or "")
        if row is None or step["state"] in FINAL:
            return None
        if step.get("worker_lineage") and self._identity(row) != step["worker_lineage"]:
            return None  # the name came back on another agent
        return row

    def verb_start(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        row = self._caller(caller, live)
        identity = self._identity(row)
        if identity not in self.runs.lead_identities():
            raise LeadError(f"'{caller}' was not launched as a LEAD (rack: an account chip, then LEAD)", 403)
        plan_path = payload.get("plan_path")
        text = payload.get("plan_text")
        if not isinstance(text, str) or not text.strip():
            raise LeadError("the plan text is required (the CLI reads --plan for you)")
        if len(text.encode()) > MAX_PLAN_BYTES:
            raise LeadError("a plan is at most 256 KiB; split it, or point steps at sections")
        base = payload.get("base", "main")
        if (
            not isinstance(base, str)
            or not 1 <= len(base) <= 255
            or base.startswith("-")
            or base in ("HEAD", "@")
            or "\x00" in base
            or self.bridge.git(["git", "check-ref-format", f"refs/heads/{base}"])[0]
        ):
            raise LeadError("--base must be a branch name on origin")
        repo = self._resolve_repo(payload.get("repo"), row)
        run = self.runs.start(
            lead_identity=identity,
            lead_session=caller,
            profile=row.get("profile"),
            title=payload.get("title"),
            plan_path=plan_path if isinstance(plan_path, str) else None,
            plan_text=text,
            run_dir=lambda run_id: self.run_root / run_id,
            parallel_max=payload.get("parallel", 3),
            # The pair this lead was LAUNCHED with, read off its own stamps.
            lead_model=row.get("launch_model"),
            worker_model=row.get("subagent_model"),
            base=base,
            repo=repo,
        )
        self._refresh_roles(live)
        # The title is what the lead is about, so it names the rack row (lead/names.py owns why a label).
        label = self.bridge.label_lead(caller, run["title"])
        return 201, {
            "ok": True,
            "run": run["id"],
            "plan": run["plan_path"],
            "base": run["base"],
            "repo": run.get("repo"),
            **({"label": label} if label else {}),
        }

    def verb_step(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run = self._lead(caller, self._live())
        items = payload.get("steps") if isinstance(payload.get("steps"), list) else [payload]
        added = []
        for item in items:
            if not isinstance(item, Mapping):
                raise LeadError("each step is an object")
            step = self.runs.add_step(
                run["id"],
                step_id=item.get("id"),
                title=item.get("title"),
                brief=item.get("brief"),
                kind=item.get("kind", "change"),
                lane=item.get("lane", "claude"),
                after=item.get("after") or (),
            )
            added.append(step["step_id"])
        return 201, {"ok": True, "run": run["id"], "added": added}

    def verb_spawn(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        _, run = self._lead(caller, live)
        step = self.runs.step(run["id"], str(payload.get("step")))
        mode = SPAWNABLE.get(step["state"])
        follow_up = bool(payload.get("follow_up"))
        if mode is None:
            raise LeadError(f"{step['step_id']} is {step['state']}: nothing to spawn", 409)
        if mode == "follow_up" and not follow_up:
            raise LeadError(f"{step['step_id']} already has work: use `spawn {step['step_id']} --follow-up`", 409)
        if mode == "fresh" and follow_up:
            follow_up = False
        if self._worker_row(step, live) is not None:
            raise LeadError(f"{step['step_id']} has a live worker ({step['worker_session']}): `lead tell` it instead", 409)
        if step["step_id"] in self.inbox.parked_steps(run["id"]):
            raise LeadError(f"{step['step_id']} is parked by a blocking decision", 409)
        groups = self._groups(run.get("repo"))
        workers = self._workers(run, groups)
        steps = self.runs.steps(run["id"])
        if not follow_up:
            merged = {s["step_id"] for s in steps if (workers.get(s.get("branch") or "") or {}).get("pr_state") == "merged"}
            if not deps_met(step, {s["step_id"]: s for s in steps}, merged):
                raise LeadError(f"{step['step_id']} waits for {', '.join(step['after'])}", 409)
        worker_live = sum(1 for s in steps if self._worker_row(s, live) is not None or s["state"] == "spawning")
        if worker_live >= run["parallel_max"]:
            raise LeadError(f"{worker_live} workers are live (cap {run['parallel_max']}); close one first", 429)
        kind = "ladder_step" if step["kind"] == "change" else "report"
        profile, why = routing.resolve(self._routing(), kind, step["lane"], self.bridge.summaries(), self.bridge.ranked())
        if profile is None:
            return self._route_wait(run, step, why)
        refusal = self.gate.refusal(profile)
        if refusal:
            return self._route_wait(run, step, refusal)
        # A Claude worker runs the run's worker model (lead/models.py); a Codex worker its profile's own.
        worker_model = run.get("worker_model") if self.bridge.kind_of(profile) == "claude" else None
        if worker_model:
            refusal = self.bridge.model_refusal(profile, worker_model)
            if refusal:
                return self._route_wait(run, step, refusal)
        status, state = self._wt("GET", "/state", repo=run.get("repo"))
        caps = (state.get("capabilities") or {}) if status == 200 else {}
        if not (
            caps.get("task_group_modes")
            and (caps.get("task_group_base") or run["base"] == state.get("base", "main"))
            and caps.get("create_respawn")
            and WORKER_PERMISSION_MODE in (caps.get("permission_modes") or ())
            and (caps.get("launch_models") or not worker_model)
        ):
            raise LeadError("the worktree plugin is too old for a LEAD run; restart the bridge", 503)
        branch = branch_for(run["id"], step["step_id"])
        # A retry after a failed start reuses the tree it already made: /create refuses a branch twice.
        reuse = follow_up or branch in self._trees(run.get("repo"))
        base = None if reuse else self._origin_base(run)
        if step["kind"] == "change" and not run.get("group_id"):
            run = self._ensure_group(run, base or self._origin_base(run))
        session = f"wt-{branch.replace('/', '-')}"
        self.runs.note_spawn(
            run["id"],
            step["step_id"],
            expect=(step["state"],),
            branch=branch,
            session=session,
            profile=profile,
            follow_up=follow_up,
        )
        if step["kind"] == "change" and branch not in workers:
            # `start_sha` is the commit this branch is cut from, so the PR's published-base check holds
            # it to the run’s base at spawn, which may advance after group creation.
            join = {"id": run["group_id"], "branch": branch, "start_sha": base or run.get("start_sha")}
            status, body = self._wt("POST", "/group-join", join, repo=run.get("repo"))
            if status >= 400:
                return self._spawn_failed(run, step, body.get("error") or "could not join the run's task group")
        cli = self.bridge.kind_of(profile)
        request = {
            "branch": branch,
            "profile": profile,
            "parent_session": run["lead_session"],
            "permission_mode": WORKER_PERMISSION_MODE,
        }
        if reuse:
            request["existing"] = True
        else:
            request["base"] = base
        if worker_model:
            request["model"] = worker_model
        prompt = brief.worker_prompt(run, step)
        if cli == "claude":
            request["prompt"] = prompt
        # A respawn's new brief replaces any the last attempt never delivered, or both would go.
        self.typist.drop_pending(session, "superseded", kind="brief")
        status, body = self._wt("POST", "/create", request, repo=run.get("repo"))
        if status >= 400:
            return self._spawn_failed(run, step, body.get("error") or f"worktree create answered {status}")
        session = body.get("session") or session
        if session != f"wt-{branch.replace('/', '-')}":
            self.runs.update_step(run["id"], step["step_id"], worker_session=session)
        if cli != "claude":
            # Unpinned: /create mints the worker's lineage in its job; `_pin_lines` pins it once seen.
            self.typist.queue(session, None, "brief", prompt)
        return 202, {
            "ok": True,
            "step": step["step_id"],
            "session": session,
            "profile": profile,
            "branch": branch,
            "route": why,
            "follow_up": follow_up,
        }

    def _ensure_group(self, run: Mapping, base: str) -> dict:
        status, body = self._wt(
            "POST",
            "/group-create",
            {
                "parent_session": run["lead_session"],
                "label": f"LEAD {run['id']} · {run['title']}"[:120],
                "start_sha": base,
                "base": run["base"],
                "auto_pr": True,
                "membership": "open",
                "release": "each",
                "workers": [],
            },
            repo=run.get("repo"),
        )
        group = body.get("group") if isinstance(body.get("group"), dict) else {}
        if status >= 400 or not group.get("id"):
            raise LeadError(f"could not create the run's task group: {body.get('error') or status}", 503)
        self.runs.set_run(run["id"], group_id=group["id"], start_sha=base)
        return self.runs.run(run["id"])

    def _spawn_failed(self, run: Mapping, step: Mapping, error: str) -> tuple[int, dict]:
        self.runs.update_step(run["id"], step["step_id"], state="failed", event=("failed", {"error": error[:300]}))
        return 502, {"ok": False, "error": error}

    def _route_wait(self, run: Mapping, step: Mapping, why: str) -> tuple[int, dict]:
        self.runs.event(run["id"], "route_wait", step["step_id"], {"why": why[:300]})
        self.inbox.ask(
            source="routing",
            run_id=run["id"],
            steps=[step["step_id"]],
            blocking=True,
            session=run["lead_session"],
            title=f"{step['step_id']} waits for an account",
            detail=f"LEAD {run['id']} cannot start {step['step_id']} ({step['title']}): {why}. "
            "Free an account or edit routing.json; DISMISS once it can go.",
        )
        return 503, {"ok": False, "error": f"no profile for {step['step_id']}: {why}", "code": "route_wait"}

    def verb_tell(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        _, run = self._lead(caller, live)
        step = self.runs.step(run["id"], str(payload.get("step")))
        text = " ".join(str(payload.get("text") or "").split())
        if not text:
            raise LeadError("tell needs text")
        worker = self._worker_row(step, live)
        if worker is None:
            raise LeadError(f"{step['step_id']} has no live worker; `spawn {step['step_id']} --follow-up` instead", 409)
        source = payload.get("from") if isinstance(payload.get("from"), str) else None
        qid = payload.get("question")
        if isinstance(qid, str) and qid:
            self.runs.settle_question(qid, state="resolved", resolution=text[:1000], source=source)
        self.typist.queue(
            step["worker_session"], self._identity(worker), "tell", brief.tell_line(run, step["step_id"], text, source)
        )
        if step["state"] == "reopened":
            self.runs.update_step(run["id"], step["step_id"], state="working", expect=("reopened",))
        self.runs.event(run["id"], "told", step["step_id"], {"question": qid, "from": source})
        self._flush(live)
        return 202, {"ok": True, "queued": True}

    def verb_escalate(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run = self._lead(caller, self._live())
        qid = payload.get("question")
        question = self.runs.question(qid) if isinstance(qid, str) and qid else None
        blocking = payload.get("blocking")
        if not isinstance(blocking, bool):
            raise LeadError('say whether it blocks: --blocking, or --assume "what the work does meanwhile"')
        steps = payload.get("steps") or ([question["step_id"]] if question else [])
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            raise LeadError("steps is a list of step ids")
        known = {s["step_id"] for s in self.runs.steps(run["id"])}
        if set(steps) - known:
            raise LeadError(f"unknown steps: {', '.join(sorted(set(steps) - known))}")
        result = self.inbox.ask(
            source="lead",
            run_id=run["id"],
            steps=steps,
            blocking=blocking,
            assumption=payload.get("assume"),
            options=payload.get("options"),
            session=run["lead_session"],
            session_lineage=run["lead_identity"],
            title=payload.get("title") or (question or {}).get("text"),
            detail=payload.get("detail") or (question or {}).get("text") or "",
        )
        if question is not None and question["state"] == "open":
            if result["outcome"] in ("answered", "dismissed"):
                self.runs.settle_question(question["id"], state="resolved", resolution=result["answer"], source=result["key"])
            else:
                self.runs.settle_question(question["id"], state="escalated", decision_key=result["key"])
        return (201 if result["outcome"] == "opened" else 200), {"ok": True, **result}

    def verb_close(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        _, run = self._lead(caller, live)
        step = self.runs.step(run["id"], str(payload.get("step")))
        facts = self.close_facts(run, step, live)
        code, why = decide_close(facts)
        if code is not None:
            return 409, {"ok": False, "code": code, "error": why}
        killed = False
        if facts.live:
            again = self._live().get(step["worker_session"])
            if again is not None and self._identity(again) != step["worker_lineage"]:
                return 409, {"ok": False, "code": "not_the_worker", "error": "the name changed hands mid-check"}
            if again is not None:
                status, body = self.bridge.kill(step["worker_session"])
                if status >= 400:
                    return 500, {"ok": False, "error": body.get("error") or "the kill failed"}
                killed = bool(body.get("killed"))
        record = {"claim": facts.claim_head, "killed": killed, "at": self._clock(), "kind": step["kind"]}
        self.runs.update_step(
            run["id"],
            step["step_id"],
            state="closed",
            closed_at=self._clock(),
            close_facts=record,
            expect=(step["state"],),
            event=("closed", {"killed": killed}),
        )
        return 200, {"ok": True, "step": step["step_id"], "killed": killed}

    def close_facts(self, run: Mapping, step: Mapping, live: Mapping[str, dict]) -> CloseFacts:
        row = live.get(step.get("worker_session") or "")
        lineage = (row or {}).get("lineage") or {}
        children = running_descendants(live.values()).get(step.get("worker_session") or "", 0) if row else 0
        questions = [q["id"] for q in self.runs.questions(run["id"]) if q["step_id"] == step["step_id"]]
        questions += self.inbox.open_for_step(run["id"], step["step_id"])
        tree = self._trees(run.get("repo")).get(step.get("branch") or "") or {}
        path = Path(tree["path"]) if tree.get("path") else None
        facts = CloseFacts(
            run_open=run["state"] == "open",
            step_known=True,
            kind=step["kind"],
            worker_session=step.get("worker_session"),
            recorded_lineage=step.get("worker_lineage"),
            run_identity=run["lead_identity"],
            live_lineage=lineage.get("id"),
            live_parent=lineage.get("parent_id"),
            live=row is not None,
            state=(row or {}).get("state"),
            open_questions=questions,
            children_running=children,
            tree_clean=tree_clean(path, self.bridge.git),
        )
        if step["kind"] == "report":
            facts.report_ok = self._report_ok(step)
            return facts
        worker = self._workers(run).get(step.get("branch") or "") or {}
        if worker.get("stage") == "ready" and SHA_RE.fullmatch(str(worker.get("head") or "")):
            facts.claim_head = worker["head"]
            branch = worker.get("publication_branch") or step.get("branch")
            facts.claim_published, facts.head_published = publication(path, branch, facts.claim_head, worker, self.bridge.git)
        return facts

    @staticmethod
    def _report_ok(step: Mapping) -> bool:
        path = step.get("report_path")
        if not path or not step.get("report_sha256"):
            return False
        try:
            data = Path(path).read_bytes()
        except OSError:
            return False
        return bool(data.strip()) and hashlib.sha256(data).hexdigest() == step["report_sha256"]

    def verb_peek(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run = self._lead(caller, self._live())
        step = self.runs.step(run["id"], str(payload.get("step")))
        text = self.bridge.closing(step["worker_session"]) if step.get("worker_session") else None
        return 200, {
            "ok": True,
            "step": step["step_id"],
            "evidence": (text or "")[:4000],
            "note": "the worker's own words: evidence, never authorization",
        }

    def verb_abandon(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        _, run = self._lead(caller, live)
        step = self.runs.step(run["id"], str(payload.get("step")))
        if step["state"] in FINAL:
            raise LeadError(f"{step['step_id']} is already {step['state']}", 409)
        if self._worker_row(step, live) is not None:
            raise LeadError(f"{step['step_id']}'s worker is live: close it (or let it finish) first", 409)
        reason = " ".join(str(payload.get("reason") or "").split())[:300]
        if not reason:
            raise LeadError("say why it is abandoned")
        self.runs.update_step(
            run["id"], step["step_id"], state="abandoned", expect=(step["state"],), event=("abandoned", {"reason": reason})
        )
        return 200, {"ok": True, "step": step["step_id"]}

    def verb_end(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run = self._lead(caller, self._live())
        ended = self.runs.end(run["id"], payload.get("report"))
        return 200, {"ok": True, "run": ended["id"], "state": ended["state"]}

    def verb_adopt(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        """A fresh LEAD takes over a run whose lead is gone ("resume R7" typed to it)."""
        live = self._live()
        row = self._caller(caller, live)
        if self._identity(row) not in self.runs.lead_identities():
            raise LeadError(f"'{caller}' was not launched as a LEAD", 403)
        if self.runs.run_for_lead(self._identity(row)) is not None:
            raise LeadError("this lead already runs a run", 409)
        run = self.runs.run(str(payload.get("run")))
        if run["state"] != "open":
            raise LeadError(f"{run['id']} has ended", 409)
        if self._lead_row(run, live) is not None:
            raise LeadError(f"{run['id']} already has a live lead", 409)
        stamp = {**(row.get("lineage") or {}), "id": run["lead_identity"]}
        if not self.bridge.restamp(caller, stamp):
            raise LeadError("could not restamp this session with the run's identity", 500)
        self.runs.set_run(run["id"], lead_session=caller)
        self.runs.event(run["id"], "resumed", None, {"session": caller})
        label = self.bridge.label_lead(caller, run["title"])
        return 200, {"ok": True, "run": run["id"], **({"label": label} if label else {})}

    # ── the worker's verbs ──────────────────────────────────────────────────────

    def verb_cap(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        live = self._live()
        _, run = self._lead(caller, live)
        run = self.runs.set_cap(run["id"], payload.get("parallel"))
        steps = self.runs.steps(run["id"])
        worker_live = sum(1 for s in steps if self._worker_row(s, live) is not None or s["state"] == "spawning")
        return 200, {"ok": True, "run": run["id"], "parallel": run["parallel_max"], "worker_live": worker_live}

    def verb_brief(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run, step = self._worker(caller, self._live())
        return 200, {"ok": True, "text": brief.brief_text(run, step, self.inbox.resolved_rows(run["id"]))}

    def verb_ask(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run, step = self._worker(caller, self._live())
        blocking = payload.get("blocking")
        if not isinstance(blocking, bool):
            raise LeadError('--blocking, or --assume "what you do meanwhile"')
        question = self.runs.ask(
            run["id"], step["step_id"], caller, payload.get("text"), blocking=blocking, assumption=payload.get("assume")
        )
        return 201, {
            "ok": True,
            "question": question["id"],
            "next": "end your turn" if blocking else "carry on under your assumption",
        }

    def verb_progress(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run, step = self._worker(caller, self._live())
        stage = payload.get("stage")
        if stage not in WORKER_STAGES:
            raise LeadError(f"stage is one of {', '.join(WORKER_STAGES)} (finish with `lead ready`)")
        return self._report_stage(run, step, {"stage": stage, "note": str(payload.get("note") or "")[:1000]})

    def verb_ready(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run, step = self._worker(caller, self._live())
        if step["kind"] != "change":
            raise LeadError("a report step finishes with `tldrack lead report <file>`")
        head = payload.get("head")
        if not isinstance(head, str) or not SHA_RE.fullmatch(head):
            raise LeadError("ready needs the full tested commit (the CLI reads HEAD for you)")
        body = {"stage": "ready", "validated": True, "head": head, "title": payload.get("title"), "body": payload.get("body")}
        return self._report_stage(run, step, body)

    def _report_stage(self, run: Mapping, step: Mapping, fields: dict) -> tuple[int, dict]:
        if not run.get("group_id"):
            raise LeadError("this run has no task group yet", 409)
        report = {"id": run["group_id"], "branch": step["branch"], **fields}
        status, body = self._wt("POST", "/group-report", report, repo=run.get("repo"))
        if status >= 400:
            return status, {"ok": False, "error": body.get("error") or "the task group refused the report"}
        return 200, {"ok": True, "stage": fields["stage"]}

    def verb_report(self, payload: Mapping[str, Any], caller: str | None) -> tuple[int, dict]:
        _, run, step = self._worker(caller, self._live())
        if step["kind"] != "report":
            raise LeadError("a change step finishes with `tldrack lead ready`")
        text = payload.get("text")
        if not isinstance(text, str):
            raise LeadError("the report text is required (the CLI reads the file)")
        saved = self.runs.save_report(run["id"], step["step_id"], text, self.run_root / run["id"] / "reports")
        return 200, {"ok": True, "report": saved["report_path"]}

    # ── reads for the UI, the snapshot and the hooks ──────────────────────────

    def state_view(self) -> dict:
        live = self._live()
        groups = _PerRepo(self._groups)
        out = []
        for run in self.runs.runs():
            workers = self._workers(run, groups(run.get("repo")))
            steps = []
            parked = self.inbox.parked_steps(run["id"])
            for s in self.runs.steps(run["id"]):
                w = workers.get(s.get("branch") or "") or {}
                steps.append(
                    {
                        "step_id": s["step_id"],
                        "title": s["title"],
                        "kind": s["kind"],
                        "lane": s["lane"],
                        "state": s["state"],
                        "parked": s["step_id"] in parked,
                        "worker_session": s.get("worker_session"),
                        "worker_live": self._worker_row(s, live) is not None,
                        "profile": s.get("profile"),
                        "branch": s.get("branch"),
                        "pr_url": w.get("pr_url"),
                        "ci": w.get("ci") if w.get("pr_url") else None,
                    }
                )
            lead = self._lead_row(run, live)
            out.append(
                {
                    "id": run["id"],
                    "title": run["title"],
                    "state": run["state"],
                    "lead_session": lead["session"] if lead else run.get("lead_session"),
                    "lead_live": lead is not None,
                    "profile": run.get("profile"),
                    "plan_path": run.get("plan_path"),
                    "repo": run.get("repo"),
                    "created_at": run["created_at"],
                    "steps": steps,
                    "base": run["base"],
                    "lead_model": run.get("lead_model"),
                    "worker_model": run.get("worker_model"),
                    "decisions": self.inbox.open_rows(run["id"]),
                }
            )
        return {"runs": out, **({"models": self.models.view()} if self.models is not None else {})}

    def _refresh_roles(self, live: Mapping[str, dict]) -> None:
        identities = self.runs.lead_identities()
        runs = {r["lead_identity"]: r for r in self.runs.runs()}
        roles: dict[str, tuple] = {}
        for session, row in live.items():
            ident = self._identity(row)
            if ident in identities:
                run = runs.get(ident)
                roles[session] = ("lead", run["id"] if run else None)
        for run in runs.values():
            for s in self.runs.steps(run["id"]):
                if s["state"] not in FINAL and s.get("worker_session") in live:
                    row = live[s["worker_session"]]
                    if s.get("worker_lineage") in (None, self._identity(row)):
                        roles[s["worker_session"]] = ("worker", run["id"], s["step_id"])
        capped = {session for session, role in roles.items() if role[0] == "lead" and live[session].get("subagent_model")}
        with self._lock:
            self._roles = roles
            self._capped = capped

    def role_of(self, session: str) -> tuple | None:
        with self._lock:
            return self._roles.get(session)

    def row_fields(self, row: Mapping) -> dict:
        """Additive /snapshot fields: `lead` on a lead row, `worker` on a worker row. Absent otherwise."""
        role = self.role_of(row.get("session") or "")
        if role is None:
            return {}
        if role[0] == "lead":
            return {"lead": {"run": role[1], "open_decisions": len(self.inbox.open_rows(role[1])) if role[1] else 0}}
        worker = {"run": role[1], "step": role[2]}
        # Legacy (step 6.8): the key was `crew` before 0.1.0. /snapshot is additive, so it is still written, for one release.
        return {"worker": worker, "crew": dict(worker)}

    def hook_tool(self, session: str, payload: Mapping[str, Any]) -> dict | None:
        """PreToolUse on the http relay: a deny for a lead's edit, and a worker's AskUserQuestion turned
        into a question for its lead. None leaves the tool alone."""
        role = self.role_of(session)
        tool = payload.get("tool_name")
        if role is None:
            return None
        if role[0] == "lead" and tool in EDIT_TOOLS:
            return _deny(brief.EDIT_DENIAL)
        if role[0] == "lead" and tool in AGENT_TOOLS and session in self._capped:
            tool_input = payload.get("tool_input")
            if isinstance(tool_input, dict) and tool_input.get("subagent_type") == FORK_TYPE:
                return _deny(brief.FORK_DENIAL)
        if role[0] == "worker" and tool == "AskUserQuestion":
            questions = (
                (payload.get("tool_input") or {}).get("questions") if isinstance(payload.get("tool_input"), dict) else None
            )
            texts = []
            for q in questions if isinstance(questions, list) else []:
                if isinstance(q, dict) and isinstance(q.get("question"), str):
                    labels = [o.get("label") for o in q.get("options") or [] if isinstance(o, dict)]
                    texts.append(q["question"] + (f" (options: {', '.join(map(str, labels))})" if labels else ""))
            if not texts:
                return None
            qids = [self.runs.ask(role[1], role[2], session, text, blocking=True)["id"] for text in texts]
            self.bridge.on_change()
            return _deny(brief.ask_denial(", ".join(qids)))
        return None

    def hook_prompt(self, session: str) -> str | None:
        role = self.role_of(session)
        if role is None:
            return None
        return brief.lead_context(role[1]) if role[0] == "lead" else brief.worker_context(role[1], role[2])

    # ── the watcher ───────────────────────────────────────────────────────────

    def tick(self) -> None:
        runs = self.runs.runs()
        due = self.inbox.due()
        if due:
            titles = ", ".join(d["key"] for d in due[:3])
            self.bridge.notify(f"{len(due)} decision{'s' if len(due) > 1 else ''} back", f"LEAD inbox: {titles}")
        if not runs and not self.typist.pending():
            return
        live = self._live()
        self._refresh_roles(live)
        changed = False
        if runs:
            # One `/groups` and one `/state` per repository the open runs lead, not per run.
            groups, trees = _PerRepo(self._groups), _PerRepo(self._trees)
            for run in runs:
                changed |= self._observe_run(run, live, groups(run.get("repo")), trees(run.get("repo")))
        self._pin_lines(runs)
        typed = self._flush(live)
        for run in self.runs.runs():
            changed |= self._wake(run, live)
        if changed or typed:
            self.bridge.on_change()

    def _observe_run(self, run: Mapping, live: Mapping[str, dict], groups: Mapping[str, dict], trees: Mapping[str, dict]) -> bool:
        workers = self._workers(run, groups)
        open_q = {q["step_id"] for q in self.runs.questions(run["id"])}
        spawned = {e["step_id"]: e["at"] for e in self.runs.history(run["id"], 200) if e["kind"] == "spawned"}
        changed = False
        for step in self.runs.steps(run["id"]):
            if step["state"] in FINAL or step["state"] == "pending":
                continue
            row = live.get(step.get("worker_session") or "")
            facts = worker_facts(row, workers.get(step.get("branch") or "") or {})
            tree = trees.get(step.get("branch") or "") or {}
            error = tree.get("create_error") if tree.get("create_status") == "error" else None
            updates, events = observe(
                step,
                facts,
                run_identity=run["lead_identity"],
                now=self._clock(),
                spawned_at=spawned.get(step["step_id"]),
                create_error=error,
                open_question=step["step_id"] in open_q,
            )
            if not updates and not events:
                continue
            for kind, detail in events:
                self.runs.event(run["id"], kind, step["step_id"], detail)
            if updates:
                self.runs.update_step(run["id"], step["step_id"], **updates)
            if updates.get("worker_lineage"):
                self.bridge.label_worker(step["worker_session"], f"{step['title']}\n{step['brief']}")
            changed = True
        return changed

    def _pin_lines(self, runs: list[dict]) -> None:
        """Pin each unpinned queued line (a Codex worker's brief) to its worker, once `observe` has recorded
        `worker_lineage`, which it takes only off a live row whose parent is the run's lead. A line whose
        session is no open step's worker any more is dropped: nobody it was meant for can appear."""
        unpinned = {item["session"] for item in self.typist.pending() if not item["lineage"]}
        if not unpinned:
            return
        steps = {
            s["worker_session"]: s
            for run in runs
            for s in self.runs.steps(run["id"])
            if s.get("worker_session") in unpinned and s["state"] not in FINAL
        }
        for session in unpinned:
            step = steps.get(session)
            if step is None:
                self.typist.drop_pending(session, "no open worker step", unpinned_only=True)
            elif step.get("worker_lineage"):
                self.typist.pin(session, step["worker_lineage"])

    def _cli_of(self, row: Mapping) -> str | None:
        return self.bridge.kind_of(row.get("profile")) if isinstance(row.get("profile"), str) else None

    def _flush(self, live: Mapping[str, dict]) -> list[dict]:
        return self.typist.flush(live, self._cli_of)

    def _wake(self, run: Mapping, live: Mapping[str, dict]) -> bool:
        events = self.runs.unwoken(run["id"])
        if not events:
            return False
        lead = self._lead_row(run, live)
        if lead is None:
            return False  # RESUME's first prompt says `lead status`; the events wait for it there
        hold = self.typist.try_now(lead["session"], self._cli_of(lead), lead.get("state"), brief.wake_line(run["id"], events))
        if hold == UNSUBMITTED:
            # In the lead's input but never seen to leave it: typing it again would double it.
            self.bridge.log(f"lead watcher: the wake for {run['id']} sits unsent in {lead['session']}")
        elif hold is not None:
            return False
        self.runs.mark_woken(e["id"] for e in events)
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="lead-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as err:  # the watcher must outlive any one bad tick
                self.bridge.log(f"lead watcher: {type(err).__name__}: {err}")


class _PerRepo:
    """A read of the worktree plugin, made once per repository key in one pass (a tick, a state view)."""

    def __init__(self, read: Callable[[str | None], dict[str, dict]]) -> None:
        self._read = read
        self._seen: dict[str | None, dict[str, dict]] = {}

    def __call__(self, repo: str | None) -> dict[str, dict]:
        if repo not in self._seen:
            self._seen[repo] = self._read(repo)
        return self._seen[repo]


def _absolute(value: object) -> Path | None:
    """`value` as a resolved absolute path (`~` expanded, as `/repos` collapses it), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = Path(value).expanduser()
        return candidate.resolve() if candidate.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    }
