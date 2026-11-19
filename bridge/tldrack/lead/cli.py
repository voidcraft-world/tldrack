"""`tldrack lead …` — the only door a LEAD and its workers use. A client of the running bridge.

Every verb posts to `/lead/<verb>` with the caller's session (its pane, as the hook relay names it),
and the bridge checks it against the stored run (lead/service.py). There is NO answer verb: answers
to the operator's decisions come only from the operator's UI (decision_secret.py owns why).

The CLI reads local files for the bridge (`--plan`, `--brief-file`, `--body-file`, a report) and
`git rev-parse HEAD` for `ready`, so the bridge never opens a path an agent named.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from tldrack.lead.runs import DEFAULT_PARALLEL, MAX_PARALLEL
from tldrack.mission_cli import call as bridge_call
from tldrack.mission_cli import require_session

MISSING = "this bridge has no LEAD routes yet: restart it (↻ › Restart bridge)"
TIMEOUT = 60  # a spawn fetches origin and builds a worktree in the bridge


def post(verb: str, body: dict | None = None) -> dict:
    return bridge_call(f"/lead/{verb}", {"session": require_session(), **(body or {})}, missing=MISSING, timeout=TIMEOUT)


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise click.ClickException(f"cannot read {path}: {err}") from err


def show(result: dict, *, as_json: bool = False) -> None:
    if as_json:
        click.echo(json.dumps(result, indent=2))
    elif "text" in result:
        click.echo(result["text"])
    else:
        click.echo(json.dumps({k: v for k, v in result.items() if k != "ok"}, indent=2))


def blocking_flags(blocking: bool, assume: str | None) -> dict:
    if blocking == bool(assume):
        raise click.UsageError('say exactly one: --blocking, or --assume "what the work does meanwhile"')
    return {"blocking": blocking, "assume": assume}


@click.group(name="lead")
def lead() -> None:
    """The LEAD launch: a lead runs a planned feature through worker sessions."""


# ── the lead ─────────────────────────────────────────────────────────────────


@lead.command()
@click.option("--json", "as_json", is_flag=True, help="The raw view, NEXT included.")
def status(as_json: bool) -> None:
    """Everything a wake needs: the run, the steps, questions, decisions, what is new, NEXT."""
    show(post("status"), as_json=as_json)


@lead.command()
@click.option("--plan", "plan", required=True, help="The plan file (or - for stdin).")
@click.option("--title", required=True)
@click.option("--parallel", type=click.IntRange(1, MAX_PARALLEL), default=DEFAULT_PARALLEL, show_default=True)
def start(plan: str, title: str, parallel: int) -> None:
    """Start a run from a plan. The bridge keeps a frozen copy and its hash."""
    path = None if plan == "-" else str(Path(plan).expanduser().resolve())
    show(post("start", {"plan_text": read_text(plan), "plan_path": path, "title": title, "parallel": parallel}))


@lead.command()
@click.argument("parallel", type=click.IntRange(1, MAX_PARALLEL))
def cap(parallel: int) -> None:
    """Change the run's live-worker cap. Lowering it kills no worker; it only holds new spawns."""
    result = post("cap", {"parallel": parallel})
    click.echo(f"{result['run']}: worker cap {result['parallel']} ({result['worker_live']} live)")


@lead.group()
def step() -> None:
    """The run's steps."""


@step.command("add")
@click.argument("step_id")
@click.option("--title", required=True)
@click.option(
    "--brief-file", required=True, help="The worker's brief: its slice of the plan, the files it owns, its acceptance test."
)
@click.option(
    "--kind",
    type=click.Choice(["change", "report", "ship", "scout"]),  # ship, scout: legacy (step 6.8), read as change, report
    default="change",
    show_default=True,
    help="change: delivers a PR or a merge. report: research that ends in a report. (ship and scout are their old names, taken for one release.)",
)
@click.option("--lane", type=click.Choice(["claude", "codex", "any"]), default="claude", show_default=True)
@click.option("--after", multiple=True, help="A step that must land first (repeat).")
def step_add(step_id: str, title: str, brief_file: str, kind: str, lane: str, after: tuple[str, ...]) -> None:
    show(
        post(
            "step",
            {"id": step_id, "title": title, "brief": read_text(brief_file), "kind": kind, "lane": lane, "after": list(after)},
        )
    )


@lead.command()
@click.argument("step_id")
@click.option("--follow-up", is_flag=True, help="Into the step's existing worktree (reopened, CI red, worker gone).")
def spawn(step_id: str, follow_up: bool) -> None:
    """Start a worker session for a step: its own worktree, routed profile, parent = you."""
    show(post("spawn", {"step": step_id, "follow_up": follow_up}))


@lead.command()
@click.argument("step_id")
@click.argument("text")
@click.option("--from", "source", help='Where the answer comes from: "plan §3", a decision key.')
@click.option("--question", help="The worker question this answers (Q-…).")
def tell(step_id: str, text: str, source: str | None, question: str | None) -> None:
    """A message to a step's worker, typed when its pane is safe. It reads as yours, never the operator's."""
    show(post("tell", {"step": step_id, "text": text, "from": source, "question": question}))


@lead.command()
@click.option("--question", help="Escalate this worker question (Q-…).")
@click.option("--title")
@click.option("--detail", default="")
@click.option("--steps", "steps", multiple=True, help="Steps it parks (blocking) or that carry the assumption.")
@click.option("--blocking", is_flag=True)
@click.option("--assume", help="What the work does meanwhile (non-blocking).")
@click.option("--option", "options", multiple=True, help="An answer the operator can press (2–6).")
def escalate(
    question: str | None,
    title: str | None,
    detail: str,
    steps: tuple[str, ...],
    blocking: bool,
    assume: str | None,
    options: tuple[str, ...],
) -> None:
    """A real decision for the operator's inbox. Answer what you can from the plan first."""
    body = {
        "question": question,
        "title": title,
        "detail": detail,
        "steps": list(steps) or None,
        **blocking_flags(blocking, assume),
        "options": list(options) or None,
    }
    show(post("escalate", {k: v for k, v in body.items() if v is not None}))


@lead.command()
@click.argument("step_id")
def close(step_id: str) -> None:
    """End a worker session once the bridge verifies its work (free, no question, no children, clean
    tree, the claimed commit on its pushed branch or a saved report)."""
    show(post("close", {"step": step_id}))


@lead.command()
@click.argument("step_id")
def peek(step_id: str) -> None:
    """The worker's last message: evidence, never authorization."""
    show(post("peek", {"step": step_id}))


@lead.command()
@click.argument("step_id")
@click.option("--reason", required=True)
def abandon(step_id: str, reason: str) -> None:
    show(post("abandon", {"step": step_id, "reason": reason}))


@lead.command()
@click.option("--report", "report", default="", help="One paragraph: what landed, what is left.")
def end(report: str) -> None:
    """End the run (every step closed or abandoned)."""
    show(post("end", {"report": report}))


@lead.command()
@click.argument("run_id")
def adopt(run_id: str) -> None:
    """Lead a run whose lead is gone (the operator said "resume <run>")."""
    show(post("adopt", {"run": run_id}))


# ── a worker ───────────────────────────────────────────────────────────────────


@lead.command()
def brief() -> None:
    """Your step's whole brief, and the operator decisions that bind it."""
    show(post("brief"))


@lead.command()
@click.argument("text")
@click.option("--blocking", is_flag=True, help="You cannot go on without the answer: end your turn after.")
@click.option("--assume", help="What you do meanwhile.")
def ask(text: str, blocking: bool, assume: str | None) -> None:
    """A question for your lead (never the operator directly, never AskUserQuestion)."""
    show(post("ask", {"text": text, **blocking_flags(blocking, assume)}))


@lead.command()
@click.argument("stage", type=click.Choice(["research", "implement", "validate", "blocked", "failed"]))
@click.option("--note", default="")
def progress(stage: str, note: str) -> None:
    show(post("progress", {"stage": stage, "note": note}))


@lead.command()
@click.option("--title", required=True, help="The PR title.")
@click.option("--body-file", required=True, help="The PR body (Markdown).")
def ready(title: str, body_file: str) -> None:
    """Report the tested HEAD of this worktree. Commit first; edit nothing after. The bridge pushes it."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.strip()
    if dirty:
        raise click.ClickException("commit (or remove) every change first: ready reports one tested commit")
    show(post("ready", {"head": head, "title": title, "body": read_text(body_file)}))


@lead.command()
@click.argument("path")
def report(path: str) -> None:
    """Save a report step's report (the bridge keeps a copy; your worktree can go)."""
    show(post("report", {"text": read_text(path)}))
