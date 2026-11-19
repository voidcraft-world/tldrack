"""The zero-token watcher: live facts in, step moves and events out. No model call, ever.

The service ticks every
`TICK_SECONDS` while a run is open (nothing at all while none is) and hands each unfinished step's
facts to `observe`, which compares them with the step's persisted `observed` facts. Persisting them
is what makes a bridge restart neither replay an event (the old facts are still known) nor lose one
(a transition that happened while the bridge was down still differs from what was stored).

Events fire on TRANSITIONS only, from a known earlier value: a step first seen already merged is
recorded, not announced twice. Nothing fires for a heartbeat, a busy worker, or a pending check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TICK_SECONDS = 5.0
#: A worker that has not appeared this long after its spawn claim failed to start.
SPAWN_GRACE_SECONDS = 600.0
WORKING = ("busy", "background")


def worker_facts(row: Mapping[str, Any] | None, worker: Mapping[str, Any]) -> dict:
    lineage = (row or {}).get("lineage") or {}
    return {
        "worker_live": bool(row and row.get("live")),
        "worker_state": (row or {}).get("state") if row else None,
        "worker_lineage": lineage.get("id"),
        "worker_parent": lineage.get("parent_id"),
        "stage": worker.get("stage"),
        "note": worker.get("note") or "",
        "head": worker.get("head"),
        "pr_url": worker.get("pr_url"),
        "ci": worker.get("ci"),
        "ci_head": worker.get("ci_head"),
        "pr_head": worker.get("pr_head"),
        "pr_state": worker.get("pr_state"),
    }


def observe(
    step: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    run_identity: str,
    now: float,
    spawned_at: float | None,
    create_error: str | None,
    open_question: bool,
) -> tuple[dict, list[tuple[str, dict]]]:
    """(step field updates, events) for one step. Pure.

    `facts` comes from `worker_facts`; `step["observed"]` holds the previous ones."""
    prev: Mapping[str, Any] = step.get("observed") or {}
    state = step["state"]
    updates: dict[str, Any] = {}
    events: list[tuple[str, dict]] = []

    if state == "spawning":
        mine = facts["worker_live"] and facts["worker_parent"] == run_identity
        if mine:
            updates.update(state="working", worker_lineage=facts["worker_lineage"])
            state = "working"
        elif create_error:
            updates["state"] = "failed"
            events.append(("failed", {"error": create_error[:300]}))
            return {**updates, "observed": dict(facts)}, events
        elif spawned_at is not None and now - spawned_at > SPAWN_GRACE_SECONDS:
            updates["state"] = "failed"
            events.append(("failed", {"error": "the worker session never appeared"}))
            return {**updates, "observed": dict(facts)}, events
        else:
            return {}, []  # still being built: nothing to compare yet

    # A name taken by another agent is not our worker: judge the session facts as "gone".
    ours = facts["worker_live"] and (
        step.get("worker_lineage") in (None, facts["worker_lineage"]) or updates.get("worker_lineage") == facts["worker_lineage"]
    )
    live = bool(ours)

    stage, prev_stage = facts["stage"], prev.get("stage")
    if stage == "ready" and (prev_stage != "ready" or facts["head"] != prev.get("head")):
        if state in ("working", "reopened"):
            updates["state"] = state = "ready"
        events.append(("ready", {"head": (facts["head"] or "")[:12]}))
    if stage in ("blocked", "failed") and prev_stage != stage:
        events.append(("blocked", {"stage": stage, "note": facts["note"][:300]}))
    if facts["pr_url"] and not prev.get("pr_url"):
        if state == "ready":
            updates["state"] = state = "published"
        events.append(("pr", {"pr": facts["pr_url"]}))
    elif facts["pr_url"] and state == "ready" and facts["head"] and facts["head"] == facts.get("pr_head"):
        updates["state"] = state = "published"  # a follow-up head reached the open PR
    ci = facts["ci"]
    if ci in ("passing", "failing") and ci != prev.get("ci") and facts["pr_url"]:
        events.append(("ci", {"ci": ci}))
    if facts["pr_state"] == "merged" and prev.get("pr_state") != "merged":
        events.append(("merged", {"pr": facts["pr_url"]}))

    prev_live, prev_state = prev.get("worker_live"), prev.get("worker_state")
    if prev_live and not live and state not in ("closed", "abandoned"):
        events.append(("gone", {}))
    elif (
        live
        and prev_state in WORKING
        and facts["worker_state"] == "free"
        and state in ("working", "reopened")
        and stage == prev_stage
        and not open_question
    ):
        events.append(("stopped", {}))

    observed = {**facts, "worker_live": live}
    if observed != dict(prev):
        updates["observed"] = observed
    return updates, events
