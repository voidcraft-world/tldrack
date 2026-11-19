"""Fakes for the LEAD tests: a bridge with no tmux, a worktree plugin with no git, a git with no repo.

Imported by tests/test_lead_*.py (pytest puts this directory on sys.path). Each fake keeps only the
behaviour the service reads, so a test states the world it needs in a few lines.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from tldrack.inbox import Inbox
from tldrack.lead.runs import LeadRuns
from tldrack.lead.service import Bridge, LeadService
from tldrack.pane_typist import PaneTypist
from tldrack.rack_db import RackDb
from tldrack.session_names import SessionNameStore
from tldrack.session_topics import title_label

H1 = "a" * 40
H2 = "b" * 40
MAIN = "c" * 40
CLAUDE_BOX = "────────────────\n❯  \n────────────────\n"


class Clock:
    def __init__(self, now: float = 1_900_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeWorktree:
    """`repos` is what `/repos` lists (None: a plugin from before it, which 404s the route); `calls` is every
    call as `(method, path, repo)`, the `repo` the router would have read (query for GET, body for POST)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.group: dict | None = None
        self.trees: dict[str, dict] = {}
        self.creates: list[dict] = []
        self.reports: list[dict] = []
        self.launch_models = True
        self.repos: list[dict] | None = [repo_row("repo-1", "/repo", default=True)]
        self.calls: list[tuple[str, str, str | None]] = []

    def worker(self, branch: str) -> dict:
        return next(w for w in self.group["workers"] if w["branch"] == branch)

    def handle_get(self, path, query):
        self.calls.append(("GET", path, (query.get("repo") or [None])[0]))
        if path == "/repos":
            return (404, {}) if self.repos is None else (200, {"repos": self.repos, "default": self.repos[0]["key"]})
        if path == "/groups":
            return 200, {"groups": [self.group] if self.group else []}
        if path == "/state":
            return 200, {
                "worktrees": list(self.trees.values()),
                "capabilities": {
                    "task_groups": True,
                    "task_group_modes": True,
                    "create_respawn": True,
                    "permission_modes": ["auto"],
                    "launch_models": self.launch_models,
                },
            }
        return 404, {}

    def handle_post(self, path, query, body):
        self.calls.append(("POST", path, body.get("repo")))
        if path == "/group-create":
            self.group = {"id": str(uuid.uuid4()), "workers": [], **{k: body[k] for k in ("membership", "release")}}
            return 200, {"group": self.group}
        if path == "/group-join":
            assert body.get("start_sha"), "a worker branch names the commit it was cut from"
            self.group["workers"].append(
                {
                    "branch": body["branch"],
                    "stage": "queued",
                    "note": "",
                    "head": None,
                    "pr_url": None,
                    "ci": None,
                    "pr_state": None,
                    "pr_head": None,
                    "start_sha": body["start_sha"],
                }
            )
            return 200, {"group": self.group}
        if path == "/group-report":
            self.reports.append(body)
            worker = self.worker(body["branch"])
            worker["stage"] = body["stage"]
            if body["stage"] == "ready":
                worker["head"] = body["head"]
            return 200, {"group": self.group}
        if path == "/create":
            self.creates.append(body)
            branch = body["branch"]
            path_ = self.root / branch.replace("/", "-")
            path_.mkdir(parents=True, exist_ok=True)
            self.trees[branch] = {"branch": branch, "path": str(path_)}
            return 202, {"status": "creating", "session": "wt-" + branch.replace("/", "-"), "branch": branch}
        return 404, {}


def repo_row(key: str, path: str, *, default: bool = False, label: str | None = None) -> dict:
    """One `/repos` row, the fields the service reads."""
    return {"key": key, "label": label or key, "path": path, "worktree_root": f"/trees/{key}", "default": default}


class FakeGit:
    """`clean`, the remote `tips` per branch, the `heads` per tree and which commits contain which. `mains` is
    origin/main per repository root (`git -C <root>`), MAIN for any other; `calls` is every argv."""

    def __init__(self) -> None:
        self.mains: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.clean = True
        self.tips: dict[str, str] = {}
        self.head = H1
        self.ancestry: set[tuple[str, str]] = set()
        self.fail_status = False

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        args = argv[3:] if argv[:2] == ["git", "-C"] else argv[1:]
        if args[:2] == ["status", "--porcelain"]:
            return (1, "") if self.fail_status else (0, "" if self.clean else " M x.py")
        if args[:1] == ["ls-remote"]:
            branch = args[2].removeprefix("refs/heads/")
            tip = self.tips.get(branch)
            return 0, f"{tip}\trefs/heads/{branch}" if tip else ""
        if args[:1] == ["cat-file"]:
            return 0, ""
        if args[:2] == ["rev-parse", "HEAD"]:
            return 0, self.head
        if args[:2] == ["merge-base", "--is-ancestor"]:
            older, newer = args[2], args[3]
            return (0, "") if older == newer or (older, newer) in self.ancestry else (1, "")
        if args[:2] == ["rev-parse", "refs/remotes/origin/main"]:
            return 0, self.mains.get(argv[2], MAIN) if argv[:2] == ["git", "-C"] else MAIN
        return 0, ""


class FakeBridge:
    def __init__(self, root: Path) -> None:
        self.rows: list[dict] = []
        self.spawned: list[dict] = []
        self.killed: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.panes: dict[str, str] = {}
        self.worktree = FakeWorktree(root / "trees")
        self.git = FakeGit()
        self.pushes: list[tuple[str, str]] = []
        self.names = SessionNameStore(root / "names.json")
        self.summaries = [
            {"id": "a", "kind": "claude", "cli_found": True, "usage": None},
            {"id": "b", "kind": "claude", "cli_found": True, "usage": None},
            {"id": "codex", "kind": "codex", "cli_found": True, "usage": None},
        ]

    def row(self, session: str, identity: str, *, parent: str | None = None, state: str = "free", profile: str = "a") -> dict:
        lineage = {"id": identity, **({"parent_id": parent} if parent else {})}
        existing = next((r for r in self.rows if r["session"] == session), None)
        if existing is not None:
            self.rows.remove(existing)
        row = {"session": session, "live": True, "state": state, "lineage": lineage, "profile": profile}
        self.rows.append(row)
        self.panes.setdefault(session, CLAUDE_BOX)
        return row

    def drop(self, session: str) -> None:
        self.rows = [r for r in self.rows if r["session"] != session]

    def spawn(self, name, profile, prompt, lineage, **models):
        """`models`: the `model` / `subagent_model` a launch named, stamped on the row as tmux would."""
        self.spawned.append({"name": name, "profile": profile, "prompt": prompt, "lineage": lineage, **models})
        row = self.row(name, lineage["id"], parent=lineage.get("parent_id"), profile=profile)
        row.update(
            {
                stamp: models[key]
                for key, stamp in (("model", "launch_model"), ("subagent_model", "subagent_model"))
                if key in models
            }
        )
        return 201, {"ok": True, "session": name}

    def kill(self, session):
        self.killed.append(session)
        self.drop(session)
        return 200, {"ok": True, "killed": True}

    def label_lead(self, session, title):
        """main.py's `label_from_title`, with every other row's label taken."""
        live = {r["session"] for r in self.rows}
        taken = [label for name, label in self.names.get_all().items() if name != session and name in live]
        label = title_label(title, taken)
        return label if label and self.names.title(session, label) else None

    def bridge(self) -> Bridge:
        kinds = {s["id"]: s["kind"] for s in self.summaries}
        return Bridge(
            rows=lambda: list(self.rows),
            summaries=lambda: self.summaries,
            ranked=lambda: ["b", "a", "codex"],
            kind_of=lambda p: kinds.get(p),
            prepare_lineage=lambda: {"id": str(uuid.uuid4())},
            spawn=self.spawn,
            kill=self.kill,
            restamp=self._restamp,
            worktree=lambda: self.worktree,
            closing=lambda s: f"closing words of {s}",
            notify=lambda t, b: self.pushes.append((t, b)),
            git=self.git,
            start_dir="/repo",
            label_lead=self.label_lead,
        )

    def _restamp(self, session, stamp):
        row = next(r for r in self.rows if r["session"] == session)
        row["lineage"] = dict(stamp)
        return True


class OpenGate:
    def refusal(self, profile_id):
        return None


def build(tmp_path: Path, clock: Clock | None = None, models=None) -> tuple[LeadService, FakeBridge, Clock]:
    clock = clock or Clock()
    fake = FakeBridge(tmp_path)
    db = RackDb(tmp_path / "rack.sqlite")
    typist = PaneTypist(
        db, capture=lambda s: fake.panes.get(s), clock=clock, send=lambda s, cli, text: fake.typed.append((s, text))
    )
    service = LeadService(
        runs=LeadRuns(db, clock=clock),
        inbox=Inbox(db, clock=clock),
        typist=typist,
        gate=OpenGate(),
        bridge=fake.bridge(),
        run_root=tmp_path / "runs",
        routing_table=dict,
        clock=clock,
        models=models,
    )
    return service, fake, clock


def ok(result):
    status, body = result
    assert status < 400, body
    return body


def launch_and_start(service: LeadService, fake: FakeBridge, steps=("S1", "S2", "S3"), parallel=3) -> tuple[str, str]:
    """A launched lead with a run of independent ship steps. Returns (lead session, run id)."""
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    run_id = ok(service.handle("start", {"title": "T", "plan_text": "# plan", "parallel": parallel}, lead))["run"]
    ok(service.handle("step", {"steps": [{"id": s, "title": f"step {s}", "brief": "do it"} for s in steps]}, lead))
    return lead, run_id


def worker_appears(service: LeadService, fake: FakeBridge, lead: str, step_id: str, run_id: str) -> str:
    """The worker session the worktree plugin would have started, live and parented to the lead."""
    step = service.runs.step(run_id, step_id)
    lead_identity = service.runs.run(run_id)["lead_identity"]
    fake.row(step["worker_session"], str(uuid.uuid4()), parent=lead_identity)
    service.tick()
    return step["worker_session"]


def publish(fake: FakeBridge, branch: str, head: str = H1, ci: str = "passing") -> None:
    worker = fake.worktree.worker(branch)
    worker.update(
        stage="ready", head=head, pr_url="https://github.com/o/r/pull/7", ci=ci, ci_head=head, pr_head=head, pr_state="open"
    )
    fake.git.tips[branch] = head
