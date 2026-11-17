"""Plan and argv for `POST /restore` — bring recorded sessions back after a reboot.

`session_records.py` owns what is remembered and when it is forgotten; this
owns what a record can be turned back into. Pure apart from three filesystem
reads (`os.path.isdir`, the transcript glob, the CLI probe via the registry),
so the refusal table is unit-testable without tmux.

── THE REFUSALS, AND WHY EACH IS A REFUSAL ────────────────────────────────────
- **cwd gone** — a removed worktree, a deleted scratch dir. Deliberately NOT a
  fallback to the bridge's start dir: unlike the coordinator's RESUME, a
  restored worktree agent landing in the MAIN checkout would carry on editing
  the wrong tree with every appearance of being fine.
- **unknown profile** — `profiles.json` changed since; guessing an account is
  how work lands on the wrong login.
- **cli missing** — the same 503 `/spawn` gives, per row.
- **conversation open in <name>** — the conversation is already running in
  another session. A second `--resume` on it would put two processes on one
  append-only transcript (`coordinator/resume.py` `live_session_ids` owns the
  full why).
- **already running** — only at POST time: a session of that name is live.

A record with no conversation, a Codex record, or a conversation whose
transcript is no longer on disk restores as a FRESH session in the same place
on the same profile (`resumable: false`) — still worth the click. Either way it
comes back in the launch permission mode it was spawned in (a LEAD worker's
`auto`) unless the operator switched it out since (`restore_mode`).
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from collections.abc import Mapping

from tldrack.agent_index import IndexEntry
from tldrack.profiles import clean_launch_model, clean_permission_mode, directory_args


@dataclass(frozen=True)
class RestorePlan:
    """One candidate. `row()` is the public shape; the rest drives the spawn."""

    session: str
    cwd: str | None
    profile: str | None  # the record's profile, as shown
    spawn_profile: str | None  # what is actually spawned (None → plain tmux)
    kind: str
    resume: list[str] | None  # `--resume=<id>` args, or None for a fresh start
    age_seconds: float
    reason: str | None
    #: The launch mode `restore_argv` replays (`restore_mode`), None for the default launch.
    permission_mode: str | None = None
    #: The launch models `restore_argv` replays (`profiles.py` § LAUNCH MODELS).
    model: str | None = None
    subagent_model: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None

    def row(self) -> dict:
        """The `GET /restorable` row. Never carries the conversation id."""
        out = {
            "session": self.session,
            "cwd": self.cwd,
            "profile": self.profile,
            "kind": self.kind,
            "resumable": self.resume is not None,
            "age_seconds": round(self.age_seconds, 1),
            "ok": self.ok,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.permission_mode is not None:
            # Said on the offer, so a RESTORE into automatic approval is never a surprise.
            out["permission_mode"] = self.permission_mode
        if self.model is not None:
            out["model"] = self.model
        if self.subagent_model is not None:
            out["subagent_model"] = self.subagent_model
        return out


def open_conversations(index: Mapping[str, IndexEntry]) -> dict[str, str]:
    """conversation id → the live session it is running in. Pure."""
    return {entry.agent_session_id: session for session, entry in index.items() if entry.agent_session_id}


def find_transcript(config_dir: str | None, agent_session_id: str) -> str | None:
    """`<config dir>/projects/*/<id>.jsonl` when it is on disk, else None: where a conversation's
    transcript lives with no hook to name it. The id must already be a validated UUID; the caller
    still checks the path against the transcript allowlist before reading it."""
    root = os.path.expanduser(config_dir or "~/.claude")
    found = glob.glob(os.path.join(glob.escape(root), "projects", "*", f"{glob.escape(agent_session_id)}.jsonl"))
    return found[0] if found else None


def transcript_exists(config_dir: str | None, agent_session_id: str) -> bool:
    """Is the conversation's transcript on disk? `--resume=` on a missing one opens an error
    screen instead of a conversation."""
    return find_transcript(config_dir, agent_session_id) is not None


def restore_mode(record: Mapping) -> str | None:
    """The launch mode a restore replays. Pure.

    The mode the session was LAUNCHED in, unless the last mode Claude reported
    differs: an operator who switched a worker out of `auto` (Shift+Tab) gets it
    back in the default launch, never in `auto` again. Unknown (no hook yet, or a
    Codex, whose hooks carry no mode) trusts the launch."""
    launch = clean_permission_mode(record.get("launch_permission_mode"))
    live = record.get("live_permission_mode")
    return launch if launch is not None and live in (None, launch) else None


def plan_restore(
    session: str, record: Mapping, profiles, *, open_ids: Mapping[str, str], now: float, check_transcript: bool = True
) -> RestorePlan:
    """One record → a plan. `profiles` is the ProfileRegistry. `check_transcript`
    is off for the `/snapshot` count, which runs every poll and only needs `ok`."""
    cwd = record.get("cwd")
    profile = record.get("profile")
    reason: str | None = None
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        reason = "cwd gone"
    spawn_profile = profile
    if profile is not None and profiles.get(profile) is None:
        reason = reason or "unknown profile"
        spawn_profile = None
    elif profile is None:
        # A session that was never stamped (a bare `?arg=` shell) comes back as
        # the shell profile when there is one, a plain tmux shell when not.
        spawn_profile = profiles.default_of_kind("shell")
    kind = (profiles.kind_of(spawn_profile) if spawn_profile else None) or record.get("kind") or "shell"
    if spawn_profile is not None and reason is None and not profiles.cli_found(spawn_profile):
        reason = "cli missing"
    agent_id = record.get("agent_session_id")
    other = open_ids.get(agent_id) if agent_id else None
    if other is not None and other != session and reason is None:
        reason = f"conversation open in {other}"
    resume = profiles.resume_args(kind, agent_id) if agent_id else None
    permission_mode = restore_mode(record)
    if (
        permission_mode is not None
        and check_transcript
        and (spawn_profile is None or profiles.permission_refusal(spawn_profile, permission_mode) is not None)
    ):
        # The CLI was downgraded or the profile now pins its own approvals: come back in the
        # default launch, the safe direction, rather than not at all. The /snapshot count
        # (check_transcript off) never probes a CLI.
        permission_mode = None
    model = clean_launch_model(record.get("launch_model"))
    subagent_model = clean_launch_model(record.get("subagent_model"))
    if (
        (model or subagent_model)
        and check_transcript
        and (spawn_profile is None or profiles.model_refusal(spawn_profile, model, subagent_model) is not None)
    ):
        # The same direction as the mode above: back on the account's own model, with
        # the stamps gone so the row no longer claims a cap it does not have.
        model, subagent_model = None, None
    if resume is not None and check_transcript:
        described = profiles.describe(spawn_profile) or {}
        if not transcript_exists(described.get("config_dir"), agent_id):
            resume = None
    return RestorePlan(
        session=session,
        cwd=cwd if isinstance(cwd, str) else None,
        profile=profile,
        spawn_profile=spawn_profile,
        kind=kind,
        resume=resume,
        age_seconds=max(0.0, now - float(record.get("last_seen") or now)),
        reason=reason,
        permission_mode=permission_mode,
        model=model,
        subagent_model=subagent_model,
    )


def restore_argv(plan: RestorePlan, profiles) -> list[str]:
    """THE spawn for a restored session: `spawn_argv` (stamped, profile env,
    `--resume=` fenced, the launch permission mode it ran in) when there is a
    profile, a bare detached shell when not. Only for an `ok` plan — the cwd
    is known to exist."""
    if plan.spawn_profile is not None:
        mode = {"permission_mode": plan.permission_mode} if plan.permission_mode else {}
        models = {key: value for key, value in (("model", plan.model), ("subagent_model", plan.subagent_model)) if value}
        return profiles.spawn_argv(plan.spawn_profile, plan.session, plan.cwd, extra_args=plan.resume or [], **mode, **models)
    return ["tmux", "new-session", "-d", "-s", plan.session, *directory_args(plan.cwd)]
