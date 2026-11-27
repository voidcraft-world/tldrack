"""tldrack — launch ttyd serving persistent local tmux sessions for the agent rack.

Foreground process (Ctrl-C to stop), loopback-only. The rack's live pane points at
http://localhost:<port>; by default the URL's ?arg=<name> picks (attach-or-create) a
tmux session, so ONE server hosts any number of sessions, differing only in URL. Attach any of them from a terminal with
`tmux attach -t <name>`. Run `claude` inside a session to watch your Claude Code
session in the rack.

Alongside ttyd, a stdlib HTTP "status" listener (default: ttyd port + 1) receives
Claude Code state reports from hooks (POST /hook — the raw hook stdin JSON, from
which state AND an architecture-zone activity are derived; the legacy POST /status
state-only route still works) and serves them back to the rack page
(GET /status, GET /snapshot) so every rack surface can mark each session by state —
busy / attention / free — and the agent character can stand in the right zone. Print the
ready-made hooks config with --print-hooks.

The listener also pushes: GET /events streams every state change over SSE (the
poll routes stay as fallback), GET /activity returns recent transitions for the
rack's ticker, and POST /usage receives account usage-limit reports from the
Claude Code statusLine command (statusline.py — print its config with
--print-statusline) for the rack's usage bar.
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

import click

from tldrack import antigravity_hooks
from tldrack import own_page
from tldrack.activity import KEEP_ACTIVITY, activity_update_for_event
from tldrack.tasks import fold_task_event, task_progress
from tldrack.agent_index import (
    TURN_OVER_STATES,
    AgentIndexReader,
    IndexEntry,
    default_index_roots,
    parse_ps_table,
    read_process_table,
    work_children,
)
from tldrack.claude_cli import ClaudeCliRuntime, PlanLedger
from tldrack.events import EventBroker, format_sse_event, next_chunk
from tldrack.history import TransitionLog, transition_entry
from tldrack.activity_log import ActivityLog, push_row, transition_row
from tldrack.ledger.cli import ledger_group
from tldrack.ledger.ingest import discover_roots, ledger_status
from tldrack.ledger.rack_search import LiveTails, RowBinding, rack_search
from tldrack.ledger.reader import read_history
from tldrack.ledger.scheduler import INTERVAL_SECONDS as LEDGER_INTERVAL_SECONDS, LedgerScheduler

from tldrack.ledger.store import ledger_dir
from tldrack.icons import ICON_TOKEN_RE, SessionIconStore, resolve_icons_path
from tldrack.stars import SessionStarStore, resolve_stars_path
from tldrack.work_trees import SessionWorkTreeStore, TreeResolver, resolve_work_trees_path, row_field
from tldrack.work_trees import evidence as work_tree_evidence
from tldrack.models import MODEL_TOKEN_RE, SessionModelStore, current_readings, resolve_models_path
from tldrack.mission_cli import mission as mission_group
from tldrack.lead.cli import lead as lead_group
from tldrack.missions import MissionError, MissionNotFound, MissionStore, resolve_missions_path, standing_seeds
from tldrack.session_costs import SessionCostStore, fix_cap_usd, parse_statusline_cost, resolve_costs_path, resolve_tmux_session
from tldrack.rack_metrics import RackMetrics, SignalQueue, metrics_response, resolve_metrics_path
from tldrack.attention import (
    HELD,
    OpenLoops,
    attention_row_fields,
    held_reason,
    in_process_children,
    is_held,
    landing_class,
    resolve_attention_path,
    running_descendants,
)
from tldrack.stuck import StuckDetector
from tldrack import error_fix
from tldrack.decisions import ANSWER_HOOK, DecisionLedger, ShadowJudge, capture_blocked, decisions_dir, doc_files
from tldrack import paths
from tldrack.plugin_context import RackPluginContext, unclaimed_settings
from tldrack.settings import Settings, SettingsError, load_settings
from tldrack.personas import (
    PERSONAS,
    PERSONAS_BY_ID,
    SessionPersonaStore,
    prompt_hook_output,
    resolve_personas_path,
)
from tldrack.open_work import (
    GRADE_KEPT,
    GRADE_RECORDED,
    OpenWorkJudge,
    OpenWorkReactions,
    decide_state,
    grade_verdict,
    label_change,
    landing_body,
    ledger_judged,
    plugin_caller,
    post_landing,
    rack_grader,
    turn_facts,
    turn_request,
    verdict_row_fields,
    watch_operator_labels,
    with_request,
)
from tldrack.verdicts import SessionVerdictStore, resolve_verdicts_path, restorable
from tldrack import framing, live_marks, plugin_sources, ttyd_front, ui_static
from tldrack import next_step as next_step_mod
from tldrack import doctor as doctor_mod
from tldrack import update as update_mod
from tldrack.codex_closing import read_codex_closing
from tldrack.codex_rollout import codex_uses_auto_review
from tldrack.notify import (
    EVENT_DOORS,
    NOTIFY_DAILY_BUDGET,
    NOTIFY_GRACE_SECONDS,
    NotifyConfigStore,
    NotifyDaemon,
    generate_topic,
    resolve_notify_path,
)
from tldrack.freshness import FreshnessMonitor, describe_freshness, mounted_plugin_dirs, start_freshness
from tldrack.identity import capture_identity, format_uptime
from tldrack.index_patch import get_patched_index_path
from tldrack.presence import PresenceStore, parse_presence
from tldrack.closed_sessions import ClosedSessionStore, is_closed_id, reopen_name, resolve_closed_path
from tldrack.restore import find_transcript, open_conversations, plan_restore, restore_argv
from tldrack.session_lineage import lineage_args
from tldrack.decision_secret import SECRET_HEADER, DecisionSecret, answer_refusal, browser_origin, secret_refusal
from tldrack.inbox import Inbox, InboxError
from tldrack.lead.close import run_git
from tldrack.lead.models import LeadModels
from tldrack.lead.runs import LeadRuns
from tldrack.lead.service import BODY_MAX as LEAD_BODY_MAX, LEAD_VERBS, Bridge as LeadBridge, LeadService
from tldrack.lead.subscription import SubscriptionGate
from tldrack.pane_typist import PaneTypist
from tldrack.rack_db import RackDb, resolve_rack_db_path
from tldrack.session_records import RECORD_MAX_AGE_DAYS, SessionRecordStore, resolve_records_path
from tldrack.service_names import reserved_error, reserved_prefix
from tldrack.agents_cli import PublicStatus
from tldrack.permission_ledger import PermissionLedger
from tldrack.permission_ledger import resolve_path as resolve_permissions_path
from tldrack.state_arbiter import StateArbiter, refine_turn_end, settled_state
from tldrack.launch_pick import pick_launch_profile
from tldrack.launch_directories import LaunchDirectories
from tldrack.features import CODE_WATCH, LEDGER_INGEST, PUBLIC_STATUS, FeatureSwitches, mountable
from tldrack.profiles import (
    AUTO_PROFILE_ID,
    PROFILE_ID_RE,
    ProfileRegistry,
    auth_status,
    install_hooks,
    load_profiles,
    resolve_profiles_path,
)
from tldrack.snapshot import (
    SESSION_NAME_RE,
    PaneSessionCache,
    RepoResolver,
    apply_hook_cwds,
    capture_lines,
    list_tmux_clients,
    list_tmux_sessions,
    merge_snapshot,
    tail_visible,
)
from tldrack.wait import (
    POLL_SECONDS as WAIT_POLL_SECONDS,
    WAITABLE_STATES,
    parse_timeout_ms,
    parse_wait_states,
    tmux_session_id,
    wait_outcome,
)
from tldrack.statusline_models import (
    StatuslineReadingStore,
    parse_statusline_context_left,
    parse_statusline_model,
    resolve_context_left,
    resolve_fallback_models,
)
from tldrack.transcript import (
    TranscriptTracker,
    configure_transcript_roots,
    normalize_transcript_path,
)
from tldrack.account_standing import AccountStandingStore, resolve_standing_path
from tldrack.usage import UsageLimitsStore, parse_usage_limits, resolve_usage_path
from tldrack.images import MAX_IMAGE_BODY_BYTES, save_image
from tldrack.session_names import SessionNameStore, is_generated, resolve_names_path, valid_title
from tldrack.session_topics import TOPIC_MODEL, TopicNamer, TopicResult, ask_topic, title_label
from tldrack.topic_labels import VERDICTS as TOPIC_VERDICTS, TopicLabels, resolve_topic_labels_path
from tldrack.ws_proxy import (
    ProxySlot,
    build_upstream_request,
    is_websocket_upgrade,
    pending_buffered,
    relay,
)
from voidcraft_world_bridge.guards import (  # noqa: F401 — re-exported: tests and callers import them from here
    allowed_remote_hosts,
    host_allowed,
    host_header_hostname,
    is_loopback_origin,
    is_no_cors_browser_request,
)
from voidcraft_world_bridge.guards import origin_allowed as core_origin_allowed
from voidcraft_world_bridge.builtins import builtin_plugins
from voidcraft_world_bridge.plugins import close_plugins, load_plugins, plugin_routes
from voidcraft_world_bridge.server import BridgeHandler, bridge_block

#: GET /ledger/status is recounted at most this often.
LEDGER_STATUS_TTL = 30.0

DEFAULT_PORT = int(os.environ.get("TLDRACK_PORT", "7681"))
DEFAULT_SESSION = os.environ.get("TLDRACK_SESSION", "tldrack")
# New tmux sessions start where the operator started the bridge: `cd my-project && tldrack up`.
# The project is theirs, not this package's, so the package's own location is never the answer.
DEFAULT_START_DIR = os.environ.get("TLDRACK_START_DIR") or os.getcwd()

# The server-side default, for a PLAIN BROWSER VISIT to this port. Every
# rack surface sends its own `theme=` on the URL, which wins
# (applyPreferences merges query options last) and carries the full 16-colour
# ANSI palette this dict deliberately does not duplicate — the source of truth
# is TERMINAL_THEME in ui/src/engine/terminal/terminal-render-options.ts.
# These four keys track its background/foreground/cursor/selection so a raw
# visit is not a jarringly different terminal. Editing them needs a bridge
# restart; editing the TS constant does not.
DARK_THEME = {
    "background": "#2c2c2c",
    "foreground": "#acfcd9",
    "cursor": "#ffffff",
    "selectionBackground": "#b3d7ff",
}

# --- Claude Code status ------------------------------------------------------
# States reported by Claude Code hooks (see --print-hooks). "unknown" is never
# stored — it's what GET returns for a session nobody has reported on. Nor is
# "background" reportable: it is derived (state_arbiter.py), never claimed.
VALID_STATES = ("busy", "attention", "free")
# SESSION_NAME_RE lives in snapshot.py (re-imported above) so that module needs no
# import back into main; POST /status validation below still uses it.
MAX_MESSAGE_LEN = 300
# POST /hook bodies are raw Claude Code hook payloads — Write/Edit tool_input
# carries whole file contents, so the cap is generous. Loopback-only traffic.
MAX_HOOK_BODY_BYTES = 512 * 1024
# POST /usage bodies are raw statusline stdin payloads — a few KB of model /
# rate-limit / cost fields.
MAX_USAGE_BODY_BYTES = 64 * 1024
# POST /spawn runs one `tmux new-session -d`, which returns once the pane is
# forked — a wedged-tmux backstop, not a budget for the agent inside it.
SPAWN_TIMEOUT_SECONDS = 15
# POST /spawn's optional first message: why it is bounded, `_handle_spawn`.
SPAWN_PROMPT_MAX = 1000


def spawn_prompt_error(prompt: object, kind: str) -> str | None:
    """Why POST /spawn refuses this `prompt`, or None when it may ride. Pure."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if len(prompt) > SPAWN_PROMPT_MAX:
        return f"prompt is over {SPAWN_PROMPT_MAX} chars — point at a file instead"
    if "\n" in prompt or "\r" in prompt:
        return "prompt must be one line"
    if prompt.lstrip().startswith("-"):
        return "prompt must not start with '-' (the CLI would read a flag)"
    if kind not in ("claude", "codex"):
        return f"a {kind} profile takes no prompt"
    return None


# POST /restore bodies name sessions to bring back, nothing more.
MAX_RESTORE_BODY_BYTES = 16 * 1024
# A Claude `SessionEnd` whose `reason` means the USER closed the conversation —
# the only kind that clears a session record's conversation id. Anything else
# (`other`, a signal at shutdown) leaves it, so a reboot can still resume it.
USER_ENDED_REASONS = frozenset({"prompt_input_exit", "logout"})

# Which surface a POST /focus should steer. `world` = an embedding page's own
# view of the rack, `rack` = the popped-out agent-rack window. The DEFAULT must stay
# `world`: it is the behaviour every caller had before `target` existed, so an
# older client that omits the param keeps working unchanged.
FOCUS_TARGETS = frozenset({"world", "rack"})
FOCUS_TARGET_DEFAULT = "world"

# Claude Code hook event → state it implies for the session.
HOOK_STATES = {
    "SessionStart": "free",
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",  # also flips attention → busy after a permission grant
    "Notification": "attention",  # permission prompt / question — see hook_state()
    "Stop": "free",
    # Fires INSTEAD of Stop when an API error ended the turn — the session or
    # weekly limit, an overload, a lapsed login (2.1.306). Without it a cut-off
    # turn reaches no hook at all; why the row then says more than `free`:
    # transcript.py `fold_stop_failure`.
    "StopFailure": "free",
    "SessionEnd": "free",
    # The permission dialog itself, the moment it opens — `Notification` only
    # says so ~6 s later. The relay prints nothing and exits 0, which is "no
    # decision", so Claude's own dialog runs exactly as it would without us.
    "PermissionRequest": "attention",
    # The task list's own events (2.1.268+, with CLAUDE_CODE_ENABLE_TODO_TOOLS:
    # profiles.py `CLAUDE_SETTINGS_ENV`). They fire inside a TaskCreate/TaskUpdate
    # call, so the session is working; their real `task_id` is what tasks.py folds.
    "TaskCreated": "busy",
    "TaskCompleted": "busy",
}

# Codex CLI hook event → state. Codex (0.160+, `hooks` stable) fires
# Claude-shaped hooks from `$CODEX_HOME/hooks.json`, so the SAME relay command
# (`hook_command`) serves both. Kept a separate table on purpose: HOOK_STATES
# generates `--print-hooks`, and a Claude settings.json must not grow events
# Claude does not have. `PermissionRequest` defaults to attention; _handle_hook
# keeps proven automatic local-tool review busy. `PostToolUse` clears manual
# approval attention once the tool finishes; `Interrupt` is Esc mid-turn.
# Pre/PostCompact and Subagent* say nothing about whether the session wants you,
# so they are not installed. They reach the bridge only from a pane-local
# app-server: owned by profiles.py `build_inner` — see the note there.
CODEX_HOOK_STATES = {
    "SessionStart": "free",
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "PostToolUse": "busy",
    "PermissionRequest": "attention",
    "Stop": "free",
    "Interrupt": "free",
    "SessionEnd": "free",
}

#: Claude hooks relayed for ONE tool only, through a matcher: `PostToolUse` fires after every tool
#: call, and the bridge needs just the answer to an `AskUserQuestion` (decisions.py `ANSWER_HOOK`). Its
#: state is CODEX_HOOK_STATES' `busy` (hook_state), which is what an answered question leaves a turn in.
CLAUDE_MATCHED_HOOKS = dict([ANSWER_HOOK])

#: Which events a profile kind's hooks file must relay (profiles.hooks_status).
#: Antigravity's table, and why it never relays PreToolUse: antigravity_hooks.py.
HOOK_EVENTS_BY_KIND = {
    "claude": (*HOOK_STATES, *CLAUDE_MATCHED_HOOKS),
    "codex": tuple(CODEX_HOOK_STATES),
    antigravity_hooks.KIND: tuple(antigravity_hooks.HOOK_STATES),
}

# Claude Code's idle nag ("Claude is waiting for your input") arrives as a
# Notification hook — the same event as a permission prompt, but it only means
# the turn ended and nobody has typed yet. Without this carve-out a finished
# session flips to attention ~60s after Stop and sits on "needs input" until
# the next prompt (there is no hook that clears attention on mere idleness).
IDLE_NOTIFICATION_RE = re.compile(r"waiting for your input", re.IGNORECASE)


def hook_state(event: str, payload: dict) -> str:
    """State a hook event implies, refined by the Notification message.

    Deliberately narrow: only the known idle-waiting wording downgrades to
    free. Any other Notification — permission, question, or future wording we
    don't recognize — stays attention; never silently swallow a real "needs
    you". Pure (like activity.py) so pytest covers it directly.
    """
    if event == "Notification":
        message = payload.get("message")
        if isinstance(message, str) and IDLE_NOTIFICATION_RE.search(message):
            return "free"
    return HOOK_STATES[event] if event in HOOK_STATES else CODEX_HOOK_STATES[event]


# ── Fresh sessions ──────────────────────────────────────────────────────────
# A session that just started a NEW conversation and has not been prompted yet
# reads `free` — exactly like one whose reply you already read. The rack drew
# both as ○, so a just-opened agent was lost among finished ones (2026-10-29).
# `fresh` is that difference: set by a SessionStart that opens a conversation,
# kept while the row stays `free`, and gone for good at the first turn — any
# busy / attention / background write clears it (`StatusStore` enforces that,
# so a legacy POST /status or an arbiter correction cannot leave it standing).
#
#: SessionStart `source` values that CONTINUE a conversation which already had
#: turns: `resume` (a restore, `--resume`) and `compact`. `startup` and `clear`
#: open an empty one; a Codex payload without `source` is a startup.
NOT_FRESH_SOURCES = frozenset({"resume", "compact"})


def fresh_for_event(event: str, payload: dict) -> object:
    """What a hook says about `fresh`: True for a SessionStart that opens an
    empty conversation, False for one that continues a conversation or for
    SessionEnd (the agent is gone), else KEEP. Pure."""
    if event == "SessionStart":
        return payload.get("source") not in NOT_FRESH_SOURCES
    if event == "SessionEnd":
        return False
    return KEEP


# ── Subagent hooks ──────────────────────────────────────────────────────────
# A subagent's tool calls fire the SESSION's hooks — same tmux pane, same
# relay — and Claude Code marks them with `agent_id`, "present only when the
# hook fires from within a subagent… absent for the main thread" (its own hook
# schema, 2.1.301). A row's state is about the MAIN thread: is it working, is
# it done, is it asking you something. So a subagent's busy report is evidence
# that work runs and nothing more.
#
# Seen 2026-10-15: a session asked an AskUserQuestion while its
# research subagents kept fetching, and every one of their PreToolUse hooks
# repainted the row `busy`. The question waited 15 minutes behind a blue dot.


def is_subagent_hook(payload: dict) -> bool:
    """Did this hook fire inside a subagent? Pure. `agent_type` alone is NOT the
    signal: Claude also sets it on the main thread of an `--agent` session."""
    agent_id = payload.get("agent_id")
    return isinstance(agent_id, str) and bool(agent_id)


def subagent_work_state(stored: str | None) -> str | None:
    """The row's state after a subagent's busy hook, or None to leave it. Pure.

    - `attention` holds: the main thread is waiting on YOU, and a subagent's
      tool call cannot answer for you.
    - a finished turn (`free` / `background`) is `background` — the turn is
      over and work still runs, the definition of that state.
    - otherwise (`busy`, nothing stored yet) it is `busy`.
    """
    if stored == "attention":
        return None
    if stored in TURN_OVER_STATES:
        return "background"
    return "busy"


# Sentinel for StatusStore.set's model/usage params: carry the stored value
# forward. Same rationale as KEEP_ACTIVITY — the legacy POST /status path knows
# nothing about model/usage and must not wipe what a hook read from the transcript.
KEEP = object()


def _session_row(session: str, entry: dict, now: float) -> dict:
    """One stored session in the `GET /status` shape, aged against `now`.

    The single owner of that shape: `StatusStore.get` and `StatusStore.list` both
    answer with it, and a field added to only one of them is a field the rack sees
    on one route and not the other.
    """
    return {
        "session": session,
        "state": entry["state"],
        "age_seconds": round(now - entry["at"], 1),
        "message": entry["message"],
        "activity": entry["activity"],
        # Step progress from the agent's task-list tool calls (tasks.py).
        "tasks": task_progress(entry.get("tasks")),
        "model": entry.get("model"),
        "usage": entry.get("usage"),
        "cwd": entry.get("cwd"),
        "profile": entry.get("profile"),
        # Who wrote the state: "hook" (a hook or legacy POST /status) or
        # "index" (the state arbiter, from Claude's own live index — see
        # state_arbiter.py). Additive; clients may ignore it.
        "state_source": entry.get("source"),
        # A new conversation nobody has prompted yet (§ Fresh sessions).
        "fresh": bool(entry.get("fresh")),
        # Laya's turn-end verdict: is planned work still open? (open_work.py)
        **verdict_row_fields(entry.get("open_work")),
    }


def _open_work_fields(previous: dict, *, clear: bool) -> dict:
    """`open_work` + its `turn` token, carried forward or cleared. Clearing bumps the
    token, which is what voids a verdict still in flight (`set_open_work`)."""
    turn = previous.get("turn", 0)
    if clear:
        return {"open_work": None, "turn": turn + 1}
    return {"open_work": previous.get("open_work"), "turn": turn}


class StatusStore:
    """In-memory per-session Claude state. Lost on restart by design — hooks
    re-report on the next event, and the frontend treats "unknown" as no-glow.
    Laya's verdict is the exception: owned by verdicts.py — see the note there."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        # Bumped on every write, so `correct` can compare-and-set: the state
        # arbiter must never overwrite a hook that landed after it looked.
        self._seq = 0

    def set(
        self,
        session: str,
        state: str,
        message: str | None,
        activity: object = KEEP_ACTIVITY,
        model: object = KEEP,
        usage: object = KEEP,
        cwd: object = KEEP,
        profile: object = KEEP,
        fold_tasks: Callable[[dict | None], dict | None] | None = None,
        open_work: object = KEEP,
        fresh: object = KEEP,
    ) -> dict:
        """Record a report; returns the previous entry ({} on first report).

        `activity` is a zone dict to store, None to clear, or the default
        KEEP_ACTIVITY sentinel to carry the stored one forward — legacy
        POST /status reports know nothing about activity and must not wipe it.
        `model`/`usage` follow the same keep-forward rule (KEEP sentinel): a hook
        reads them from the Claude transcript, POST /status never touches them.

        `cwd` is where CLAUDE is, as its hook JSON reports it — which follows
        `EnterWorktree` and `/cd`, while tmux's `pane_current_path` stays with
        the pane's shell. Same keep-forward rule; `None` clears it (SessionEnd:
        the shell is the truth again). `snapshot.apply_hook_cwds` is the reader.

        `profile` is the launch profile whose config dir holds the session's
        transcript — the hint `merge_snapshot` prefers over the tmux stamp.
        Same keep-forward rule and the same SessionEnd clear.

        `fold_tasks` maps the stored task state to the next one (`tasks.py`),
        applied under the lock: parallel tool calls fire their PreToolUse hooks
        concurrently, and a read-then-write outside it would lose a create.
        None carries the state forward, so neither Stop nor POST /status
        touches it.

        `open_work` is KEEP or None: None clears Laya's verdict AND voids any
        judgement still in flight for this session (a new turn began).

        `fresh` is KEEP or a bool (`fresh_for_event`), and only a `free` row
        keeps it: any other state is a turn, which ends freshness for good.
        """
        with self._lock:
            previous = self._sessions.get(session, {})
            tasks = previous.get("tasks")
            was_fresh = previous.get("fresh", False) if fresh is KEEP else bool(fresh)
            self._sessions[session] = {
                "state": state,
                "message": message,
                "activity": previous.get("activity") if activity is KEEP_ACTIVITY else activity,
                "model": previous.get("model") if model is KEEP else model,
                "usage": previous.get("usage") if usage is KEEP else usage,
                "cwd": previous.get("cwd") if cwd is KEEP else cwd,
                "profile": previous.get("profile") if profile is KEEP else profile,
                "tasks": tasks if fold_tasks is None else fold_tasks(tasks),
                "fresh": state == "free" and was_fresh,
                **_open_work_fields(previous, clear=open_work is None),
                "at": time.monotonic(),
                **self._stamp_locked("hook"),
            }
            return previous

    def note_subagent_work(
        self,
        session: str,
        activity: object,
        model: object = KEEP,
        usage: object = KEEP,
    ) -> tuple[dict, str] | None:
        """Record a SUBAGENT's busy hook; returns `(previous, new_state)`, or
        None when the row's state was left alone (`subagent_work_state`).

        The decision runs under the lock, so a Notification landing beside a
        subagent's tool call can never be overwritten by it. What a subagent
        hook may move is what belongs to the whole session — model and usage,
        read off the MAIN transcript (a subagent's own is the separate
        `agent_transcript_path`) — and, on a busy row, the activity. Never:

        - `cwd` — a worktree-isolated subagent reports ITS checkout;
        - `tasks` — a subagent's task list is its own;
        - `wall_at` — the arbiter's "a hook is newer than the index" clock
          (state_arbiter.py rule 2). A subagent's hook says nothing about the
          main thread, so it must not out-date Claude's own index — which is
          what lets the index's `waiting` through while subagents keep working.
          A row a subagent creates gets 0, so the index always outranks it.
        """
        with self._lock:
            previous = self._sessions.get(session, {})
            state = subagent_work_state(previous.get("state"))
            if state is None:
                # Model/usage are not part of the state, so they move without a
                # stamp: `at` is an attention row's blocked duration (notify.py's
                # grace window), and `seq` would void an arbiter read for nothing.
                if model is not KEEP:
                    previous["model"] = model
                if usage is not KEEP:
                    previous["usage"] = usage
                return None
            wall_at = previous.get("wall_at", 0.0)
            self._sessions[session] = {
                "message": None,
                "cwd": None,
                "profile": None,
                "tasks": None,
                **previous,
                "state": state,
                # A subagent runs inside a turn, so the conversation has had one.
                "fresh": False,
                "activity": activity if state == "busy" and activity is not KEEP_ACTIVITY else previous.get("activity"),
                "model": previous.get("model") if model is KEEP else model,
                "usage": previous.get("usage") if usage is KEEP else usage,
                "at": time.monotonic(),
                **self._stamp_locked("hook"),
                "wall_at": wall_at,
            }
            return previous, state

    def correct(
        self,
        session: str,
        expect_seq: int | None,
        state: str,
        message: str | None,
        activity: object = KEEP_ACTIVITY,
    ) -> dict | None:
        """Write a state the ARBITER derived, only if nothing wrote since it
        looked (`expect_seq` is the `seq` it read, None for "no entry").

        Returns the previous entry ({} if none), or None when the CAS lost — a
        hook landed in between, and a hook that recent is the better witness.
        Everything but state/message/activity carries forward: the arbiter knows
        nothing about models, usage, cwd or profile.
        """
        with self._lock:
            current = self._sessions.get(session)
            if (current["seq"] if current else None) != expect_seq:
                return None
            previous = current or {}
            self._sessions[session] = {
                **previous,
                "state": state,
                "message": message,
                "activity": previous.get("activity") if activity is KEEP_ACTIVITY else activity,
                # A busy the hooks missed is still a new turn.
                **_open_work_fields(previous, clear=state == "busy"),
                "fresh": state == "free" and previous.get("fresh", False),
                "at": time.monotonic(),
                **self._stamp_locked("index"),
            }
            return previous

    def begin_open_work(self, session: str) -> int | None:
        """Open a judgement for the turn that just ended: clears the old verdict and
        returns the token `set_open_work` must present. None for an unknown session."""
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None:
                return None
            entry.update(_open_work_fields(entry, clear=True))
            return entry["turn"]

    def set_open_work(self, session: str, token: int, verdict: dict) -> bool:
        """Land a verdict, compare-and-set on the turn token — False when a newer
        turn (or a dismiss) began while Laya was thinking, or the row is busy."""
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None or entry.get("turn") != token or entry["state"] == "busy":
                return False
            entry["open_work"] = verdict
            return True

    def holds_open_work(self, session: str, token: int) -> bool:
        """True while the verdict landed on `token` is still on the row: no new turn, busy,
        dismiss, kill or rename since. `OpenWorkReactions.landed` asks it under its own lock."""
        with self._lock:
            entry = self._sessions.get(session)
            return entry is not None and entry.get("turn") == token and entry.get("open_work") is not None

    def dismiss_open_work(self, session: str) -> dict | None:
        """The operator cleared the ⚠️: the verdict it took off the row (the grade's subject), or None
        when there was none. Until the next turn end."""
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None or entry.get("open_work") is None:
                return None
            verdict = entry["open_work"]
            entry.update(_open_work_fields(entry, clear=True))
            return verdict

    def open_work_verdict(self, session: str) -> dict | None:
        """A copy of the verdict on the row, or None."""
        with self._lock:
            entry = self._sessions.get(session)
            verdict = entry.get("open_work") if entry is not None else None
            return dict(verdict) if verdict else None

    def mark_open_work_graded(self, session: str, decision_id: str) -> bool:
        """The operator answered the ⚠️ still on the row (✓ STILL OPEN). Compare-and-set on the verdict's
        own `decision_id`: a newer turn's verdict is not the one that was graded."""
        with self._lock:
            entry = self._sessions.get(session)
            verdict = entry.get("open_work") if entry is not None else None
            if not verdict or verdict.get("decision_id") != decision_id:
                return False
            entry["open_work"] = {**verdict, "graded": True}
            return True

    def label_open_work(self, decision_id: str, label: dict | None) -> list[str]:
        """The operator labelled the text of `decision_id` elsewhere (open_work.py § The operator's label):
        every row whose verdict IS that decision takes `label` as its `operator`, which also grades it
        (`graded_open_work`). None takes the label back, and a ✓ STILL OPEN's `graded` with it: that grade
        was a label too, and none stands now. Compare-and-set on the decision id, so a newer turn's verdict
        is never repainted. Returns the sessions whose verdict changed."""
        changed = []
        with self._lock:
            for session, entry in self._sessions.items():
                verdict = entry.get("open_work")
                if not verdict or verdict.get("decision_id") != decision_id:
                    continue
                relabelled = {key: value for key, value in verdict.items() if key not in ("operator", "graded")}
                if label is not None:
                    relabelled["operator"] = dict(label)
                if relabelled != verdict:
                    entry["open_work"] = relabelled
                    changed.append(session)
        return changed

    def mark_answer_read(self, session: str) -> tuple[bool, bool]:
        """The operator opened the pane of a turn-over row (open_work.py § An answer to read): stamp its verdict
        `read`, so `answer_to_read` drops and the ✅ stays. `(stamped, shown)`: whether the verdict was unread,
        and whether it drew a 📖 (the 📖 rule, an operator's READ label included, is `verdict_row_fields`'s
        alone). Any unread verdict is stamped, 📖 or not: a READ label given after the pane was opened asks for
        a read the operator already gave."""
        with self._lock:
            entry = self._sessions.get(session)
            verdict = entry.get("open_work") if entry is not None else None
            if not verdict or verdict.get("read") is True:
                return False, False
            entry["open_work"] = {**verdict, "read": True}
            return True, bool(verdict_row_fields(verdict)["answer_to_read"])

    def graded_open_work(self) -> frozenset[str]:
        """The sessions whose ⚠️ the operator already answered, by ✓ or by a label given elsewhere (open_work.py
        § The operator's label): the snapshot's `open_work_graded`."""
        with self._lock:
            return frozenset(
                session
                for session, entry in self._sessions.items()
                if (verdict := entry.get("open_work") or {}).get("graded") or verdict.get("operator")
            )

    def labelled_open_work(self) -> frozenset[str]:
        """The sessions whose mark is the operator's label, not Laya's band: the snapshot's `open_work_labelled`.
        (A `frozenset`, as `graded_open_work`: inside this class `set` names the method.)"""
        with self._lock:
            return frozenset(
                session for session, entry in self._sessions.items() if (entry.get("open_work") or {}).get("operator")
            )

    def void_open_work(self, session: str) -> None:
        """The session is gone (`/kill-session`): clear its verdict and void any judgement
        still in flight, so nothing lands — or is reported shown — on the dead row."""
        with self._lock:
            entry = self._sessions.get(session)
            if entry is not None:
                entry.update(_open_work_fields(entry, clear=True))

    def versions(self) -> dict[str, dict]:
        """session → {state, seq, wall_at}: what the arbiter compares against."""
        with self._lock:
            return {
                session: {"state": entry["state"], "seq": entry["seq"], "wall_at": entry["wall_at"]}
                for session, entry in self._sessions.items()
            }

    def _stamp_locked(self, source: str) -> dict:
        """The write's sequence number, wall-clock time and source. `at` stays
        monotonic (ages are differences); `wall_at` is compared with Claude's
        own epoch timestamps, so it has to be the wall clock."""
        self._seq += 1
        return {"seq": self._seq, "wall_at": time.time(), "source": source}

    def get(self, session: str) -> dict:
        with self._lock:
            entry = self._sessions.get(session)
        if entry is None:
            return {
                "session": session,
                "state": "unknown",
                "age_seconds": None,
                "message": None,
                "activity": None,
                "tasks": None,
                "model": None,
                "usage": None,
                "cwd": None,
                "profile": None,
                "state_source": None,
                "fresh": False,
                **verdict_row_fields(None),
            }
        return _session_row(session, entry, time.monotonic())

    def rename(self, old: str, new: str) -> bool:
        """Move a session's reported state onto a new name; True if there was one.

        MANDATORY after `tmux rename-session`, not a nicety. `merge_snapshot`
        union-merges tmux with this store, and a store row with no matching tmux
        session comes back `live: False` — which the agent rack renders as a
        dimmed `— session gone —` row. Leave the old key behind and the renamed
        session haunts the rack as a permanent ghost until the bridge restarts.

        The new name wins if something is already stored under it: the rename
        already happened in tmux, so that entry describes a session that no
        longer exists under that name.
        """
        with self._lock:
            entry = self._sessions.pop(old, None)
            if entry is None:
                return False
            self._sessions[new] = entry
            return True

    def forget(self, session: str) -> bool:
        """Drop a session's reported state; True if there was any.

        For a name a NEW session is about to take (POST /spawn). The old entry
        describes the previous occupant — its state, its model, its cwd — and a
        fresh agent must not open wearing them.
        """
        with self._lock:
            return self._sessions.pop(session, None) is not None

    def list(self) -> list[dict]:
        """Thread-safe snapshot of every reported session (`GET /status` shape each)."""
        with self._lock:
            entries = [(session, entry.copy()) for session, entry in self._sessions.items()]
        now = time.monotonic()  # after the copy, so age_seconds can't go negative
        return [_session_row(session, entry, now) for session, entry in entries]


# The Host + Origin guards are owned by voidcraft_world_bridge/guards.py — see the notes there.
def origin_allowed(origin: str | None, allowed: frozenset[str] = frozenset()) -> bool:
    """The core's Origin guard with THIS host's default: loopback pages and native clients only.
    The core's own default set names the site it was built for; a rack serves its own page from
    loopback and names no site. An operator adds origins in settings.py `allowed_origins`, and
    the handler passes that set (`StatusHandler.ALLOWED_ORIGINS`)."""
    return core_origin_allowed(origin, allowed)


# The name `/shutdown`'s guard went by before every route shared one; tests still use it.
shutdown_origin_allowed = origin_allowed

# Which plugins mount, and from where: owned by tldrack/plugin_sources.py — see the note there.
PLUGINS_ENV = plugin_sources.PLUGINS_ENV
PLUGINS_FILE = plugin_sources.PLUGINS_FILE


def plugins_config_path() -> Path:
    return paths.config_file(PLUGINS_FILE)


class ShutdownControl:
    """Shared kill switch between the status-server thread and the main thread.

    POST /shutdown calls trigger() from a handler thread; the main thread is
    blocked either on ttyd.wait() (full mode — terminating the child unblocks
    it) or on event.wait() (status-only mode). `requested` lets main exit 0 on
    a deliberate shutdown instead of mirroring ttyd's signal exit code.
    """

    def __init__(self, mode: str) -> None:
        self.event = threading.Event()
        self.requested = False
        #: Set by `trigger_restart`: the exit that follows ends in `restart_in_place`, not in an exit.
        self.restart = False
        self.ttyd_proc: subprocess.Popen | None = None
        self.mode = mode
        self.external_ttyd_pid: int | None = None

    def trigger(self) -> None:
        self.requested = True
        if self.ttyd_proc is not None:
            self.ttyd_proc.terminate()
        self.event.set()

    def trigger_restart(self) -> bool:
        """Stop the way `/shutdown` does, then come back as the code on disk. False in status-only
        mode, where the ttyd is somebody else's: stopping it to restart would take a terminal
        server this bridge did not start."""
        if self.mode != "full":
            return False
        self.restart = True
        self.trigger()
        return True


def restart_in_place(control: ShutdownControl) -> None:
    """The last step of an exit that `trigger_restart` asked for: become the code on disk.

    Registered with `atexit` BEFORE anything else this process registers, so it runs after all of
    it: by now ttyd has exited, the plugins are closed and the socket directory is gone, exactly
    as on a `/shutdown`. Then the same pid re-executes its own command line. Why in place and not
    exit-and-be-restarted: owned by update.py — see the note there. Listening sockets are not
    inherited across exec, so the new process binds its ports like any first start."""
    if not control.restart:
        return
    click.echo("tldrack: restarting in place to run the code on disk")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def build_status_handler(
    store: StatusStore,
    icon_store: SessionIconStore,
    model_store: SessionModelStore,
    default_session: str,
    bridge_info: dict,
    control: ShutdownControl,
    plugins: dict[str, object] | None = None,
    *,
    start_dir: str | None = None,
    profiles: ProfileRegistry | None = None,
    records: SessionRecordStore | None = None,
    closed: ClosedSessionStore | None = None,
    freshness: FreshnessMonitor | None = None,
    usage_limits: UsageLimitsStore | None = None,
    standing: AccountStandingStore | None = None,
    auto_names: SessionNameStore | None = None,
    permissions: PermissionLedger | None = None,
    pane_sessions: PaneSessionCache | None = None,
    personas: SessionPersonaStore | None = None,
    stars: SessionStarStore | None = None,
    missions: MissionStore | None = None,
    costs: SessionCostStore | None = None,
    decisions: DecisionLedger | None = None,
    judge_worker: bool = False,
    ledger: LedgerScheduler | None = None,
    activity_log: ActivityLog | None = None,
    topic_ask: Callable[[str, list[str]], TopicResult] | None = None,
    verdicts: SessionVerdictStore | None = None,
    work_trees: SessionWorkTreeStore | None = None,
    rack_metrics: RackMetrics | None = None,
    next_step_paths: tuple[Path, Path] | None = None,
    next_step_chat: next_step_mod.Chat | None = None,
    loops: OpenLoops | None = None,
    signal_worker: bool = False,
    rack_db: RackDb | None = None,
    decision_secret: DecisionSecret | None = None,
    topic_labels: TopicLabels | None = None,
    settings: Settings | None = None,
    find_ui: Callable[[], Path | None] | None = None,
    features: FeatureSwitches | None = None,
    updater: update_mod.Updater | None = None,
) -> type[BridgeHandler]:
    # `features` (features.py) is what the operator switched off on the Settings page. Absent, it is
    # read from the config role, which a test's own empty directories make "everything on".
    switches: FeatureSwitches = features if features is not None else FeatureSwitches()
    # `updater` (update.py) is the checkout's self-update; absent, `/update` answers 503, so a
    # test-built handler never runs git or npm in the tree the tests run from.
    # `settings` (settings.py) is what the operator turned on; absent, every optional part is off and
    # only loopback pages are served. `find_ui` finds the built rack page served at `/`
    # (ui_static.py), asked per request so a build that lands after startup is served without a
    # restart; absent, there is no page.
    settings = settings if settings is not None else Settings()
    # `topic_labels` (topic_labels.py) is the name-label log: absent (a test that gives none),
    # nothing is recorded and /topic-labels answers 503.
    # `rack_db` (rack_db.py) turns on the decision inbox and the LEAD launch (lead/); absent, their
    # routes answer 503 and no row grows `lead`/`worker`, so a test-built handler never touches a real
    # operator store. `decision_secret` is minted here when not given (decision_secret.py owns why).
    # `next_step_paths` = (state file, log file) turns the suggested next step on
    # (next_step.py); absent, the bridge has no suggester and the routes 503, so a
    # test-built handler never writes the operator's real log. `next_step_chat`
    # stands in for Ollama in tests.
    # `start_dir` is where POST /spawn opens a session — the same directory
    # ttyd's attach-or-create uses — and `profiles` is the launch-profile
    # registry (profiles.py). Both default to absent so a handler built
    # without them is the pre-profile bridge exactly: no `profiles` key on
    # /snapshot, and POST /spawn answers 503. `records` (session_records.py)
    # is the same: absent → no `restorable` key, and the restore routes 503.
    # `closed` (closed_sessions.py) too: absent → no `closed` key, and
    # GET /closed + POST /reopen answer 503.
    # One tracker per server: tails each session's Claude transcript incrementally
    # to surface the running model + cumulative token usage on POST /hook.
    transcripts = TranscriptTracker()
    # pane → session for the `http` hook relay (hook_http_entry).
    pane_sessions = pane_sessions if pane_sessions is not None else PaneSessionCache()
    # Advertised once at build time — routes() is static per plugin. Only added
    # to /snapshot when non-empty so a plugin-less bridge stays byte-identical.
    advertised_plugins = plugin_routes(plugins)
    # Account usage limits (POST /usage). The production path passes a
    # PERSISTED store (usage.py owns why); a test-built handler keeps one in
    # memory. SSE clients (GET /events) and the transition ring buffer
    # (GET /activity) are in-memory, per-server.
    usage_limits = usage_limits if usage_limits is not None else UsageLimitsStore()
    # Which accounts cannot run a turn at all (account_standing.py). Persisted on the production
    # path for the usage store's reason; a test-built handler keeps it in memory.
    account_standing = standing if standing is not None else AccountStandingStore()
    # The running model as the STATUSLINE reports it — the only source that
    # knows it before the session's first reply exists. Rides in on the same
    # POST /usage payload as the rate limits; see statusline_models.py.
    statusline_models = StatuslineReadingStore()
    # Context left (0–100) per Claude session, off the same payload — served as
    # each row's `context_left`. statusline.py owns the formula.
    statusline_contexts = StatuslineReadingStore()
    broker = EventBroker()
    transitions = TransitionLog()
    ledger_status_cache: dict[str, object] = {}
    # The rack search's folded live-transcript tails (ledger/rack_search.py owns why).
    rack_tails = LiveTails()
    # Which browser surfaces are on screen (POST /presence). The one
    # thing the bridge cannot observe for itself — see presence.py — and what
    # lets a native client offer "jump to where this session already is".
    presence = PresenceStore()
    # The attention telemetry (rack_metrics.py § Attention signals): every interruption the bridge
    # raises and every one the policy holds back, written off the hook path.
    signals = SignalQueue(rack_metrics, records.get if records is not None else (lambda _: None), worker=signal_worker)
    # Busy rows that are circling or silent (stuck.py). Evaluated at snapshot time.
    stuck = StuckDetector(on_flagged=lambda session, kind: signals.put("stuck_flagged", session, kind))
    # When each row's newest verdict landed (wall s): the digest's "done since the last one".
    verdict_landed: dict[str, float] = {}
    # Which linked worktree holds a path, read off git's files behind a short cache (work_trees.py).
    tree_resolver = TreeResolver()

    def served_worktree_repos_now() -> tuple[str, ...] | None:
        """The mounted worktree plugin's repositories, asked in process (doctor.py `worktree_repos_check`)."""
        worktree = capability("worktree")
        handle_get = getattr(worktree, "handle_get", None)
        if not callable(handle_get):
            return None
        try:
            status, body = handle_get("/repos", {})[:2]
        except Exception:  # noqa: BLE001 — a plugin bug is a missing doctor line, never a 500
            return None
        return doctor_mod.served_worktree_repos(body) if status == 200 else None

    def capability(name: str) -> object | None:
        """The mounted plugin that provides `name` (`laya`: turn-end verdicts; `worktree`: checkouts),
        or None. Every optional part asks here, once per use: a bridge without the plugin has the
        feature off, and nothing in this file assumes one is there."""
        return StatusHandler.plugins.get(name) if StatusHandler.plugins else None

    def children_now(rows: list[dict] | None = None) -> dict[str, int]:
        """session → unfinished live descendants (attention.py § 1), from tmux's lineage stamps and
        the store's states. `rows` reuses a snapshot's merged rows when the caller has them."""
        if rows is None:
            states = {row["session"]: row["state"] for row in store.list()}
            rows = [
                {"session": name, "live": True, "state": states.get(name), "lineage": pane.get("lineage")}
                for name, pane in list_tmux_sessions().items()
            ]
        return running_descendants(rows)

    def stuck_of(row: dict) -> dict | None:
        return stuck.evaluate(row["session"], row.get("state"), transcripts.tool_events(row["session"]))

    def notify_rows() -> list[dict]:
        """What the phone policy reads: the store's rows for LIVE sessions, each carrying `stuck`. A
        dead row's last state describes nobody, and a stale `busy` on one would read as stuck."""
        live = list_tmux_sessions()
        rows = [dict(row) for row in store.list() if row["session"] in live]
        labels = auto_names.get_all() if auto_names is not None else {}
        for row in rows:
            if (reading := stuck_of(row)) is not None:
                row["stuck"] = reading
            if row["session"] in labels:
                row["label"] = labels[row["session"]]  # the rack's name for it, as the digest uses
        return rows

    def account_roots() -> list:
        """Every account's transcript root, as the ledger walks them (`ingest.discover_roots`)."""
        described = [profiles.describe(pid) for pid in profiles.ids()] if profiles is not None else []
        return discover_roots([(d["id"], d["kind"], d["config_dir"]) for d in described if d], Path.home())

    def bind_rack_rows(sessions: list[str]) -> list[RowBinding]:
        """What the rack search needs of each row: the title its filter reads (the session name and
        the auto title the snapshot serves as `auto_name`), the conversation it runs and the
        transcript the tracker bound. ledger/rack_search.py owns how they are used."""
        labels = auto_names.get_all() if auto_names is not None else {}
        bound = transcripts.paths()
        rows = []
        for name in sessions:
            record = records.get(name) if records is not None else None
            rows.append(
                RowBinding(
                    session=name,
                    title=f"{name} {labels.get(name) or ''}",
                    conversation=(record or {}).get("agent_session_id"),
                    transcript=bound.get(name),
                )
            )
        return rows

    def digest_items(since: float) -> dict:
        """The optional reviews for notify.py's digest (property 7), by rack label: open work that is
        not held or parked, parked loops, and verdicts of done work that landed since `since`."""
        live = list_tmux_sessions()
        rows = [row for row in store.list() if row["session"] in live and row.get("state") in TURN_OVER_STATES]
        children = children_now()
        parked = loops.parked() if loops is not None else set()
        labels = auto_names.get_all() if auto_names is not None else {}
        open_work, done, parked_count = [], [], 0
        for row in rows:
            name = row["session"]
            if row.get("open_work"):
                if name in parked:
                    parked_count += 1
                elif not is_held(row.get("state"), children.get(name, 0), sum(in_process_now(name))):
                    open_work.append(labels.get(name, name))
            elif row.get("work_done") and verdict_landed.get(name, 0.0) >= since:
                done.append(labels.get(name, name))
        return {"open_work": open_work, "done": done, "parked": parked_count}

    def forget_attention(session: str) -> None:
        """A name about to describe a different session (kill, spawn, respawn): its open loop and
        stuck clocks belonged to the last occupant."""
        if loops is not None:
            loops.forget(session)
        stuck.forget(session)
        verdict_landed.pop(session, None)

    # cwd → repo name, cached for the life of the server. A session's repo is
    # fixed while it lives, so this is one `git rev-parse` per distinct directory
    # ever, not one per poll.
    repo_resolver = RepoResolver()
    # Phone push for a session that has been blocked a while with nobody looking
    # (notify.py). Reads the two stores above rather than hooking POST /hook: the
    # case it exists for is "the agent is stuck and NOTHING further happens",
    # which by definition produces no further events to hang off. Does nothing at
    # all until a topic is configured — see start() below.
    notifier = NotifyDaemon(
        config_store=NotifyConfigStore(resolve_notify_path()),
        read_sessions=notify_rows,
        read_presence_count=lambda: len(presence.list()),
        on_push=(
            None
            if activity_log is None
            else lambda kind, sessions, title, sent: activity_log.append(push_row(kind, sessions, title, sent))
        ),
        # Optional reviews as a digest, and every ping counted (notify.py properties 6 and 7).
        read_digest=digest_items,
        on_signal=lambda signal, session, reason, count: signals.put(signal, session, reason, count),
    )
    if notifier.start():
        state = notifier.state()
        status = "on" if state["enabled"] else "off (toggle to enable)"
        click.echo(f"  phone notify:      {status}  → {state['server']}, {state['daily_budget']}/day max")

    def freshness_block() -> dict | None:
        if freshness is None:
            return None
        try:
            return freshness.snapshot()
        except Exception:  # noqa: BLE001 — unknown beats a /snapshot 500
            return None

    # The LEAD launch (lead/service.py), built below once spawn and kill exist; read late by the
    # snapshot (rows' `lead` / `worker`) and the hook.
    lead_service: LeadService | None = None
    secret = decision_secret if decision_secret is not None else DecisionSecret()

    def snapshot_payload() -> dict:
        """The GET /snapshot envelope — also the body of every SSE `snapshot`
        event, so push clients reuse their poll-ingestion path unchanged."""
        # The legacy single slot prefers the DEFAULT account, so readers that
        # predate per-profile usage (an older page, a native client) stop flipping
        # between accounts — see UsageLimitsStore.get_preferred.
        usage = usage_limits.get_preferred(profiles.default_id if profiles else None)
        store_rows = store.list()
        # A hook-reported cwd (where Claude is) overrides the pane's (where the
        # shell is) BEFORE merge and repo resolution, so `cwd` and `repo` move
        # together when a session enters a worktree — see apply_hook_cwds.
        tmux_sessions = apply_hook_cwds(list_tmux_sessions(), store_rows)
        session_paths = transcripts.paths()
        sessions = merge_snapshot(
            tmux_sessions,
            store_rows,
            icon_store.get_all(),
            list_tmux_clients(),
            # Cached per cwd, so this is one `git rev-parse` the first time a
            # session's directory is seen and a dict hit on every poll after.
            repo_resolver.resolve_all(tmux_sessions),
            # Fallback only — fills the model for a session whose live
            # StatusStore row has none. Two tiers: the statusline (which
            # knows the model before the first reply exists) over the disk
            # store (which keeps an IDLE session labelled across a bridge
            # restart). See statusline_models.py for the precedence.
            # Only a reading of the session that holds the name NOW: models.py `current_readings`.
            resolve_fallback_models(
                current_readings(model_store.get_all(), tmux_sessions), statusline_models.get_all(), session_paths
            ),
        )
        # Additive per-row `context_left` (0–100): live rows only — a dead row's
        # last reading describes a conversation nobody can continue in it.
        context_left = resolve_context_left(statusline_contexts.get_all(), session_paths)
        for row in sessions:
            if row.get("live") and row["session"] in context_left:
                row["context_left"] = context_left[row["session"]]
        # Additive per-row `background_jobs` on background rows: the work
        # children the arbiter counted last tick plus the transcript's open
        # background agents. None when the state is known and the count is not
        # (agent_index.py § BACKGROUND WORK owns why each source is which).
        work = arbiter.last_work()
        for row in sessions:
            if row.get("live") and row.get("state") == "background":
                jobs = work.get(row["session"], 0) + transcripts.open_agents(row["session"])
                row["background_jobs"] = jobs or None
        # Additive per-row `stop_failure` on live turn-over rows: the turn did not
        # finish, an API error (the session limit, mostly) CUT IT OFF. Omitted
        # otherwise — a working or blocked row's last error is history.
        for row in sessions:
            if row.get("live") and row.get("state") in TURN_OVER_STATES:
                stop_failure = transcripts.stop_failure(row["session"])
                if stop_failure is not None:
                    row["stop_failure"] = stop_failure
        # A cut-off that is the ACCOUNT's (a lapsed plan, a dead login) marks its profile out,
        # here because this is the one place every row's cut-off and profile meet. A write only
        # when a mark is new (account_standing.py).
        account_standing.observe(sessions)
        # Additive per-row attention fields (attention.py): `children_running` on a parent whose
        # descendants still work, `open_work_seen` on a parked ⚠️, `open_work_repeat` on a ⚠️ that
        # retells an open loop; and `stuck` (stuck.py) on a busy row circling or silent.
        children = children_now(sessions)
        parked = loops.parked() if loops is not None else set()
        classes = loops.last_class() if loops is not None else {}
        graded, labelled = store.graded_open_work(), store.labelled_open_work()
        for row in sessions:
            if not row.get("live"):
                continue
            row.update(attention_row_fields(row, children=children, parked=parked, classes=classes))
            # Additive `open_work_graded: true`: a ✓ STILL OPEN, or a label, left this ⚠️'s turn graded.
            if row.get("open_work") is True and row["session"] in graded:
                row["open_work_graded"] = True
            # Additive `open_work_labelled: true`: the row's ⚠️ / ✅ / 📖 is the operator's label (open_work.py
            # § The operator's label), so its title reads as theirs, not as Laya's p.
            if row["session"] in labelled:
                row["open_work_labelled"] = True
            # Additive `work_tree` `{path, branch, main, by}`: the linked worktree this session works in, by
            # its cwd or by what its tools did (work_trees.py). Omitted on main, like `stop_failure`.
            if work_trees is not None:
                tree = row_field(row.get("cwd"), work_trees.get(row["session"]), tree_resolver)
                if tree is not None:
                    row["work_tree"] = tree
            if (reading := stuck_of(row)) is not None:
                row["stuck"] = reading
            if lead_service is not None:  # `lead` on a lead row, `worker` on a worker row (lead/service.py)
                row.update(lead_service.row_fields(row))
        # Additive per-row `fresh: true` on a live `free` row that has not been
        # prompted yet (§ Fresh sessions). Omitted otherwise, like `stop_failure`.
        fresh = {entry["session"] for entry in store_rows if entry.get("fresh")}
        for row in sessions:
            if row.get("live") and row.get("state") == "free" and row["session"] in fresh:
                row["fresh"] = True
        if auto_names is not None:
            names = auto_names.get_all()
            for row in sessions:
                if row["session"] in names:
                    row["auto_name"] = names[row["session"]]
        # Additive per-row `persona` (personas.py): the work persona picked for it.
        if personas is not None:
            picks = personas.get_all()
            for row in sessions:
                if row["session"] in picks:
                    row["persona"] = picks[row["session"]]
        # Additive per-row `starred`, on every row so a client knows this bridge keeps stars (stars.py).
        if stars is not None:
            starred = stars.get_all()
            for row in sessions:
                row["starred"] = row["session"] in starred
        # Who is alive, and which new children join their parent's mission (missions.py).
        if missions is not None:
            missions.observe_rows(sessions)
        # The suggested next step (next_step.py): this pass feeds it, and in `live` mode a turn-over
        # row carries the additive `next_step` it settled on. Shadow mode adds nothing to any row.
        if next_steps is not None:
            next_steps.observe(sessions)
            for row in sessions:
                field = next_steps.row_field(row)
                if field is not None:
                    row["next_step"] = field
        restorable = 0
        if records is not None:
            # Where each live session is, for a restore after a reboot. Rows
            # are post-apply_hook_cwds, so a worktree agent records where
            # CLAUDE is — the directory it must come back to.
            dropped = records.observe_live(sessions, profiles.kind_of if profiles is not None else (lambda _p: None))
            # Gone since the last pass while tmux is plainly fine: a close made
            # outside the bridge, and as undoable as a ✕ (closed_sessions.py).
            for name, record in dropped.items():
                file_close(name, record, "gone")
            restorable = sum(
                plan.ok for plan in restore_plans({row["session"] for row in sessions if row["live"]}, check_transcript=False)
            )
        reopenable = len(closed_plans(check_transcript=False)) if closed is not None else 0
        profile_summaries = (
            profiles.summary(
                bridge_info.get("status_port") or 0,
                HOOK_EVENTS_BY_KIND,
                usage_limits.by_profile(),
                account_standing.by_profile(),
            )
            if profiles is not None
            else None
        )
        return {
            "sessions": sessions,
            # Additive: old frontends and the Swift decoder ignore it. Starts from
            # the core's block (name, version, protocol) so this bridge answers
            # the same capability probe a stranger's does.
            "bridge": {
                **bridge_block(),
                **bridge_info,
                "uptime_seconds": round(time.time() - bridge_info["started_at"], 1),
                # Additive: GET /ws fronts ttyd's socket behind this server's
                # Origin guard. A browser cannot read a status code off a failed
                # WebSocket, so "does this bridge have the route" has to be
                # answerable over plain HTTP — see ws_proxy.py and the frontend's
                # TtydClient. An older bridge omits the key and the panel says
                # "restart the bridge" instead of retrying a socket forever.
                "ws_proxy": True,
                # Additive: POST /hook resolves an `http` relay's pane header
                # (hook_http_entry). `profiles --install-hooks` refuses to write
                # http relays for a running bridge that lacks this — it would
                # file every agent's events under the default session.
                "hook_transports": ["command", "http"],
                # Additive: is the running code still the code on disk, and
                # what would a restart kill — owned by freshness.py. null means
                # unknown (no watcher), never "stale".
                "freshness": freshness_block(),
            },
            # Additive again: which plugins are mounted (omitted when none).
            **({"plugins": advertised_plugins} if advertised_plugins else {}),
            **({"plugin_diagnostics": plugins.diagnostics} if hasattr(plugins, "diagnostics") else {}),
            # Additive again: account usage limits (omitted until the first
            # statusline report — see usage.py).
            **({"usage_limits": usage} if usage else {}),
            # Additive again: which rack surfaces are on screen right now
            # (omitted when nothing has reported — see presence.py). Expiry
            # happens on this read, so a record here is always within its TTL.
            **({"clients": clients} if (clients := presence.list()) else {}),
            # Additive again: per Claude config dir, does `claude agents --json`
            # agree with the private index, and is that root on the public
            # fallback (agents_cli.py). Omitted until the first public read.
            **({"state_sources": sources} if (sources := public_status.health()) else {}),
            # Additive again: the launch profiles (profiles.py), each with its
            # own account's usage. ALWAYS present on a bridge that has
            # POST /spawn — its presence IS that capability, the same
            # structural gating `worktrees` uses on the client.
            **({"profiles": profile_summaries} if profile_summaries is not None else {}),
            # Additive again, beside `profiles`: where `"profile": "auto"` would
            # launch right now (launch_pick.py owns the scoring). Its presence
            # is the client's cue to offer AUTO; POST /spawn re-picks at click
            # time rather than trusting this copy.
            **(
                {
                    "launch_pick": pick_launch_profile(
                        profile_summaries,
                        sessions,
                        time.time(),
                        profiles.capacities(),
                    ).as_wire()
                }
                if profile_summaries is not None
                else {}
            ),
            # Additive again: how many recorded sessions POST /restore could
            # bring back (session_records.py). Omitted at 0, so its presence is
            # the client's cue to show RESTORE.
            **({"restorable": restorable} if restorable else {}),
            # Additive again: how many recent closes the rack's ↺ CLOSED list
            # could reopen (closed_sessions.py). Omitted at 0 — its presence is
            # the client's cue to draw the button.
            **({"closed": reopenable} if reopenable else {}),
            # Additive again: the suggester's mode (next_step.py). Present on a bridge that has
            # one, so the rack knows to report its control presses (`POST /next-step/event`) —
            # the evidence shadow mode is scored against. Omitted when off or absent.
            **({"next_step": {"mode": next_steps.mode}} if next_steps is not None and next_steps.mode != "off" else {}),
        }

    if profiles is not None:
        # `"auto"` resolves against a FRESH snapshot — the rows a click just
        # changed (a spawn a second ago) — never a cached one.
        profiles.set_launch_picker(lambda: snapshot_payload().get("launch_pick"))

    # POST /restore spawns serially: two clicks must never race one name.
    restore_lock = threading.Lock()

    def restore_plans(live: set[str], *, check_transcript: bool) -> list:
        """A plan per recorded session with no live tmux session behind it."""
        if records is None or profiles is None:
            return []
        open_ids = open_conversations(arbiter.last_index())
        now = time.time()
        return [
            plan_restore(name, record, profiles, open_ids=open_ids, now=now, check_transcript=check_transcript)
            for name, record in sorted(records.not_live(live).items())
        ]

    def file_close(session: str, record: dict | None, how: str) -> None:
        """One close into the undo list, with the label the rack showed for it and the star it
        carried. The star leaves the live store here whether or not the close can be listed: it
        belongs to the conversation that just ended, never to the next one under this name."""
        starred_at = stars.take(session) if stars is not None else None
        if closed is None:
            return
        label = auto_names.get_all().get(session) if auto_names is not None else None
        closed.add(session, record, how, label, starred_at)

    def closed_plans(*, check_transcript: bool) -> list[tuple[str, dict, object]]:
        """`(id, entry, plan)` per close the rack can still offer, newest first.

        Two kinds are left out rather than shown dead: a close whose cwd is
        gone (a removed worktree — restore.py owns why it is never relocated),
        and one whose conversation is running again somewhere, under any name —
        it is not closed any more. Other refusals (`unknown profile`, `cli
        missing`) stay, with their reason, so the row explains itself."""
        if closed is None or profiles is None:
            return []
        open_ids = open_conversations(arbiter.last_index())
        now = time.time()
        plans = []
        for closed_id, entry in closed.entries():
            if entry.get("agent_session_id") in open_ids:
                continue
            plan = plan_restore(
                entry["session"],
                {**entry, "last_seen": entry["closed_at"]},
                profiles,
                open_ids=open_ids,
                now=now,
                check_transcript=check_transcript,
            )
            if plan.reason != "cwd gone":
                plans.append((closed_id, entry, plan))
        return plans

    def notify_snapshot() -> None:
        """Push the current snapshot to SSE clients. Skips the tmux subprocess
        entirely when nobody is listening."""
        if broker.client_count():
            broker.broadcast("snapshot", snapshot_payload())

    def record_transition(session: str, event: str, previous: dict, state: str, activity: object) -> None:
        """Ring-buffer + push a ticker entry when the report changed something
        user-visible (see history.py)."""
        if state == "attention" and previous.get("state") != "attention":
            signals.put("wait_started", session)  # the denominator for every ping (rack_metrics.py)
        entry = transition_entry(session, event, previous, state, activity)
        if entry is not None:
            transitions.append(entry)
            if activity_log is not None:  # the session ledger's copy (activity_log.py)
                activity_log.append(transition_row(entry))
            broker.broadcast("activity", entry)

    def on_arbiter_correction(session: str, previous: dict, state: str, activity: object) -> None:
        if state == "busy":  # `correct` cleared the ⚠️: a prompt the hooks missed is a new turn
            verdict_left_row(session, "expired")
        elif not previous.get("state") and state in TURN_OVER_STATES:
            recover_turn_end(session)  # the row a restart lost, rebuilt from the index
        record_transition(session, "index", previous, state, activity)
        click.echo(f"  status: {session}: {previous.get('state') or '(new)'} → {state}  (index)")

    def background_now(session: str) -> tuple[int, int]:
        """`(work children, open background agents and workflows)` behind a
        session this instant — the evidence `refine_turn_end` asks for. (0, 0)
        without a believable index entry: nothing could ever settle a background
        row the index does not describe, so such a session keeps hook-only
        behaviour. The agents are `in_process_now`'s: an index that already says
        idle overrules a launch that never notified (attention.py § 1b). It reads
        the last tick, never a fresh index: on the hook path, and a read taken at
        the Stop itself would still say busy."""
        index = arbiter.last_index()
        entry = index.get(session)
        if entry is None:
            return 0, 0
        return shells_of(entry), sum(in_process_now(session, index))

    def shells_of(entry: IndexEntry) -> int:
        """The work children of the entry's Claude this instant: one process-table read."""
        table = read_process_table()
        return work_children(parse_ps_table(table), [entry.pid])[entry.pid] if table else 0

    def refresh_turn_over_transcripts() -> bool:
        """Re-tail each turn-over row's transcript, every arbiter tick; True when a
        stop failure moved. A cut-off turn can land with no hook at all
        (TranscriptTracker.refresh owns why) — and is then free only because the
        arbiter corrected it, which is exactly the tick this runs at the end of."""
        changed = False
        for session, version in store.versions().items():
            if version["state"] in TURN_OVER_STATES:
                changed |= transcripts.refresh(session)
                # A rejection that ended the turn fires no hook; this re-tail is
                # what resolves it (permission_ledger.py § HOW IT IS RESOLVED).
                if permissions is not None:
                    permissions.observe(session, "tick", {}, transcripts.tool_events(session))
        return changed

    def on_index(index: dict) -> None:
        # Claude's own `sessionId` per session — the conversation a restore
        # resumes, even on an account with no hooks installed.
        if records is not None:
            for session, entry in index.items():
                records.note_agent_session(session, entry.agent_session_id)

    # Corrects what the hooks miss — Esc, an answered permission prompt — from
    # Claude Code's own live index (agent_index.py owns why, state_arbiter.py
    # the rule). Built here, STARTED only by start_status_server.
    def index_roots() -> list[str]:
        return (profiles.claude_roots() if profiles is not None else []) or default_index_roots()

    def claude_cli() -> str | None:
        profile_id = profiles.default_of_kind("claude") if profiles is not None else None
        return profiles.cli_path(profile_id) if profile_id else None

    # `claude agents --json`, the SUPPORTED surface, checks that index every
    # 15 s and stands in for a root whose index format drifted (agents_cli.py
    # owns why it is the check and not the fast path). Started with the arbiter.
    public_status = PublicStatus(
        index_roots,
        claude_cli,
        os.path.realpath(os.path.expanduser("~/.claude")),
        enabled=lambda: switches.on(PUBLIC_STATUS),
    )
    arbiter = StateArbiter(
        store,
        AgentIndexReader(index_roots, public=public_status),
        on_correction=on_arbiter_correction,
        on_changed=notify_snapshot,
        on_index=on_index,
        on_settled=refresh_turn_over_transcripts,
    )

    # session -> (token, transcript_path, conversation) of the turn the judge is reading, so a
    # verdict that lands can be kept for a restart against the exact turn it judged.
    judged_from: dict[str, tuple[int, object, object]] = {}

    def on_open_work(session: str, token: int, verdict: dict) -> None:
        # Did a question open this turn? Read off the transcript the judge just tailed, and stamped
        # before either copy is written (open_work.py § An answer to read).
        opening = transcripts.turn_opening(session)
        verdict = with_request(verdict, turn_request(opening) if opening is not None else None)
        # The copy is kept BEFORE the row shows the verdict. In the other order a restart between
        # the two forgot a mark the operator had already seen (and a reader that waits on the row
        # could not know the copy was on disk yet).
        origin = judged_from.get(session)
        kept = verdicts is not None and origin is not None and origin[0] == token
        if kept:
            verdicts.set(session, verdict, origin[1], origin[2])
        if not store.set_open_work(session, token, verdict):
            if kept:
                verdicts.forget(session)  # the turn moved on: the copy describes nothing on screen
            return
        open_work_reactions.landed(session, verdict, live=lambda: store.holds_open_work(session, token))
        verdict_landed[session] = time.time()
        kind = classify_verdict(session, verdict)
        click.echo(f"  open-work: {session}: p={verdict['p']:.2f}{f'  ({kind})' if kind else ''}")
        notify_snapshot()

    def index_after_write(session: str) -> dict[str, IndexEntry]:
        """Claude's index as read after the row's current state was written, so a turn-over row is
        judged by an index that has seen its Stop (`StateArbiter.index_since` owns why)."""
        written = (store.versions().get(session) or {}).get("wall_at")
        return arbiter.index_since(written if isinstance(written, (int, float)) else 0.0)

    def in_process_now(session: str, index: Mapping[str, IndexEntry] | None = None) -> tuple[int, int]:
        """`(agents, workflows)` running inside the session's own Claude (attention.py § 1b), judged
        by `index`: by default one read after the row's state was written (`index_after_write`)."""
        entry = (index_after_write(session) if index is None else index).get(session)
        return in_process_children(*transcripts.open_children(session), entry.state if entry else None)

    def classify_verdict(session: str, verdict: dict) -> str | None:
        """Where a landed verdict sits: its ⚠️'s place in the session's open loop (attention.py § 2–3),
        or held as ⏳ (§ 1, § 1b), recorded as the telemetry signal for what the operator was shown;
        then the landing record for every verdict the ledger holds (open_work.py § The landing record).
        The row's state and its in-process children are both read off ONE index taken after the Stop,
        as the arbiter's next ticks will settle them (state_arbiter.py `settled_state`)."""
        index = index_after_write(session)
        entry = index.get(session)
        shells_now = functools.cache(lambda: shells_of(entry) if entry is not None else arbiter.last_work().get(session, 0))
        state = settled_state(store.get(session)["state"], entry, shells_now)
        descendants = children_now().get(session, 0)
        agents, workflows = in_process_now(session, index)
        reason = held_reason(state, descendants, agents + workflows)
        kind = None
        if loops is not None:
            fields = verdict_row_fields(verdict)
            kind = landing_class(
                loops.classify(session, is_open=fields["open_work"] is True, work_done=fields["work_done"] is True), reason
            )
        if kind == HELD:
            signals.put("warning_held", session, reason)
        elif kind is not None:
            signals.put(f"warning_{kind}", session)
        post_landing_row(
            session,
            verdict,
            kind,
            reason,
            {
                "descendants": descendants,
                "agents": agents,
                "workflows": workflows,
                "shells": shells_now() if state == "background" else 0,
            },
        )
        return kind

    def post_landing_row(session: str, verdict: dict, kind: str | None, reason: str | None, running: dict) -> None:
        """The landing record (open_work.py § The landing record), on the judge's thread: one file append
        in the plugin. Silent on every failure, like a reaction (`plugin_caller` never raises): it is
        telemetry, never the mark."""
        call = laya_caller()
        body = landing_body(verdict, kind, reason, running, transcripts.turn_trace(session))
        if call is not None and body is not None:
            post_landing(call, body)

    def verdict_left_row(session: str, action: str) -> None:
        """The row's verdict is gone, so its ⚠️ reaction is posted and the copy kept for a restart
        is dropped (verdicts.py owns why that copy must follow the row). Call it AFTER the store's
        own clear: OpenWorkReactions.landed owns why."""
        open_work_reactions.cleared(session, action)
        if verdicts is not None:
            verdicts.forget(session)

    def on_operator_labels(changes: object) -> None:
        """The laya plugin wrote operator labels (open_work.py § The operator's label): each row still holding
        a labelled decision repaints from the label, its kept copy follows, and a DONE closes the row's open
        loop as ✗ NOT OPEN does (attention.py § 3). Runs on the plugin's request thread, after its write."""
        repainted = []
        for change in changes if isinstance(changes, list) else ():
            found = label_change(change)
            if found is None:
                continue
            decision_id, label = found
            for session in store.label_open_work(decision_id, label):
                if verdicts is not None:
                    verdicts.label(session, decision_id, label)
                if label is not None and not label["open"] and loops is not None:
                    loops.forget(session)
                word = "unlabelled" if label is None else ("open" if label["open"] else "done")
                click.echo(f"  open-work: {session}: operator label → {word}")
                repainted.append(session)
        if repainted:
            notify_snapshot()

    def recover_turn_end(session: str) -> None:
        """A row the index just rebuilt after a restart: its kept verdict comes back when it is
        provably the same turn (verdicts.py § WHY A RESTORE CANNOT LIE); otherwise a turn end whose
        `Stop` never reached this bridge is judged now (open_work.py `ledger_judged` owns why)."""
        entry = arbiter.last_index().get(session)
        if entry is None:
            return
        kept = verdicts.get(session) if verdicts is not None else None
        if kept is not None:
            verdict = restorable(
                kept, entry.agent_session_id, closing_state(session, kept["transcript_path"], kept["agent_session_id"])
            )
            if verdict is not None:
                token = store.begin_open_work(session)
                if token is not None and store.set_open_work(session, token, verdict):
                    open_work_reactions.restored(session, verdict)
                    click.echo(f"  open-work: {session}: p={verdict['p']:.2f}  (restored)")
                return
            verdicts.forget(session)  # another turn or conversation: the copy describes nothing on screen
        conversation = entry.agent_session_id
        transcript_path = find_transcript(entry.root, conversation) if conversation else None
        if transcript_path is None:
            return

        def unjudged_state() -> str | None:
            state = closing_state(session, transcript_path, conversation)
            if state is None or ledger_judged(capability("laya"), conversation, state) is not False:
                return None
            return state

        if judge_turn_end(session, store.begin_open_work(session), transcript_path, conversation, unjudged_state):
            click.echo(f"  open-work: {session}: judging a turn end the bridge missed")

    def judge_turn_end(
        session: str,
        token: int | None,
        transcript_path: object,
        conversation: object,
        read_state: Callable[[], str | None] | None = None,
    ) -> bool:
        """Queue Laya's judgement of the turn `token` (`StatusStore.begin_open_work`) opened: THE submit
        for a `Stop` and for a missed one (`recover_turn_end`). `read_state` runs on the judge's thread,
        after the settle; by default the closing message. False when the row holds no turn or this
        bridge has no Laya."""
        if token is None:
            return False
        judged_from[session] = (token, transcript_path, conversation)
        return open_work_judge.submit(
            session,
            token,
            read_state or (lambda: closing_state(session, transcript_path, conversation)),
            conversation=conversation,
        )

    def closing_state(session: str, transcript_path: object, agent_session_id: object) -> str | None:
        """Re-tail, then the closing message as Laya's state — read at judgement time. The turn's request and
        facts go with it, and `decide_state` reads each only at a question version that asks for it, so serving
        a challenger's version is `QUESTION_VERSION` alone. A Codex turn has neither: its v4 state is its v3."""
        request, facts = None, None
        codex_profile = profiles.profile_for_path(transcript_path, kind="codex", first_on_tie=True) if profiles else None
        if codex_profile:
            closing = read_codex_closing(transcript_path, profiles.describe(codex_profile)["config_dir"], agent_session_id)
        elif normalize_transcript_path(transcript_path) is not None:
            transcripts.update(session, transcript_path)
            # A turn an API error cut off has no hand-off: its "closing text" is Claude Code's own notice
            # (transcript.py `fold_stop_failure`), and the row already draws 🪫/🛑 for it.
            if transcripts.stop_failure(session) is not None:
                return None
            closing = transcripts.closing_text(session)
            opening = transcripts.turn_opening(session)
            request = turn_request(opening) if opening is not None else None
            facts = turn_facts(transcripts.turn_trace(session))
        else:
            return None
        return decide_state(closing, request, facts=facts) if closing else None

    # Laya's turn-end warning (open_work.py owns why). Reaches the
    # mounted laya plugin in process; a bridge without it never queues a thing.
    def laya_caller():
        laya = capability("laya")
        if laya is None:
            return None
        return plugin_caller(laya, on_decide=lambda: live_marks.mark(live_marks.LAYA_DECIDE))

    def laya_grader(headers):
        """The grade door for one operator press, with that request's headers (open_work.py § The operator's grade)."""
        laya = capability("laya")
        return rack_grader(laya, headers) if laya is not None else None

    open_work_judge = OpenWorkJudge(laya_caller, on_open_work)

    # Topic labels for generated rows (session_topics.py owns why). A real call only
    # on the production path (`judge_worker`) or when a test injects `topic_ask`.
    def topic_taken(session: str) -> list[str]:
        """Every other LIVE row's label and every custom tmux name: a label left on a session that
        ended by itself is not in the rack, and must not push a new one to `-2`."""
        live = list_tmux_sessions()
        held = {
            label
            for name, label in (auto_names.get_all() if auto_names is not None else {}).items()
            if name != session and name in live
        }
        held.update(name for name in live if name != session and not is_generated(name))
        return sorted(held, key=str.casefold)

    def on_topic(session: str, epoch: int, result: TopicResult) -> None:
        if result.label and auto_names is not None and auto_names.settle_topic(session, epoch, result.label):
            click.echo(f"  name: {session} → {result.label}")
            if topic_labels is not None and result.request:
                topic_labels.named(session, result.label, result.request, model=TOPIC_MODEL, profile=result.profile)
            notify_snapshot()
        elif result.error:
            click.echo(f"  name: {session}: {result.error}", err=True)

    # Off until the operator turns it on (settings.py `session_namer`): it is a model call per session.
    if topic_ask is None and judge_worker and profiles is not None and settings.session_namer:
        topic_plans = PlanLedger()  # gate 2's memory of each account's plan (claude_cli.py)

        def topic_ask(prompt: str, taken: list[str]) -> TopicResult:
            return ask_topic(profiles, prompt, taken, plans=topic_plans)

    topic_namer = (
        TopicNamer(topic_ask, topic_taken, on_topic, worker=judge_worker)
        if auto_names is not None and topic_ask is not None
        else None
    )

    def note_prompt(session: str, prompt: object) -> None:
        """The placeholder label now, the topic call that replaces it after (session_names.py)."""
        if auto_names is None:
            return
        epoch = auto_names.note_prompt(session, prompt)
        if epoch is not None and topic_namer is not None and isinstance(prompt, str):
            topic_namer.submit(session, epoch, prompt)

    def label_from_title(session: str, title: str) -> str | None:
        """A LEAD's label from its run's title: the topic rules with no model call (session_topics.py)."""
        label = title_label(title, topic_taken(session)) if auto_names is not None else None
        if label is None or not auto_names.title(session, label):
            return None
        click.echo(f"  name: {session} → {label} (run title)")
        return label  # LeadService.handle pushes the snapshot

    # The decision ledger's shadow judge (decisions.py): it predicts, it never answers.
    decision_judge = ShadowJudge(decisions, laya_caller, worker=judge_worker) if decisions is not None else None
    # What became of each ⚠️ shown, as ledger reactions (open_work.py § The ledger).
    open_work_reactions = OpenWorkReactions(laya_caller)

    # The suggested next step (next_step.py owns what, when and why). It reads the same closing
    # message Laya judges, fitted the same way, and the worktree plugin's rows in process.
    def next_step_message(session: str) -> str | None:
        origin = judged_from.get(session)
        if origin is not None:
            return closing_state(session, origin[1], origin[2])
        closing = transcripts.closing_text(session)  # a restart lost the Stop that bound the path
        return decide_state(closing) if closing else None

    def next_step_worktrees() -> list[dict]:
        plugin = capability("worktree")
        if plugin is None:
            return []
        status, payload = plugin.handle_get("/state", {})[:2]
        rows = payload.get("worktrees") if status == 200 and isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else []

    next_steps = (
        next_step_mod.NextSteps(
            next_step_message,
            next_step_worktrees,
            chat=next_step_chat,
            log=next_step_mod.NextStepLog(next_step_paths[1]),
            state_path=next_step_paths[0],
            on_change=notify_snapshot,
            idle_tick=lambda: snapshot_payload(),
            worker=judge_worker,
            on_model_call=lambda: live_marks.mark(live_marks.NEXT_STEP),
        )
        if next_step_paths is not None
        else None
    )

    def kill_session_now(session: str, by: str) -> tuple[int, dict]:
        """Kill ONE tmux session and forget it in every store a close owes. POST /kill-session and
        the LEAD's verified close (lead/close.py) both end a session here, so neither can skip one."""
        # '=' prefix = exact match; a bare -t would prefix-match some OTHER
        # session when the named one is already gone.
        result = subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}"],
            capture_output=True,
            text=True,
            check=False,
        )
        stderr = result.stderr or ""
        if result.returncode == 0:
            click.echo(f"tldrack: killed tmux session '{session}' via {by}")
            # Killed on purpose: nothing to bring back after a reboot — but
            # it IS the undo list's, until it ages out. Filed FIRST, while
            # the name store still holds the label the rack showed.
            if records is not None:
                file_close(session, records.get(session), "killed")
                records.forget(session)
            # A `+ NEW` reusing this name is a genuinely new agent, and must
            # not start life muted by the cooldown its predecessor earned.
            notifier.forget(session)
            forget_attention(session)
            # The store's clear first: OpenWorkReactions.landed owns why.
            store.void_open_work(session)
            verdict_left_row(session, "expired")
            if auto_names is not None:
                auto_names.forget(session)
            if personas is not None:
                personas.forget(session)
            if stars is not None:  # a no-op when `file_close` took it into the closed entry
                stars.forget(session)
            if missions is not None:
                missions.forget(session)
            if next_steps is not None:  # held for the press that killed it (next_step.py `forget`)
                next_steps.forget(session)
            if work_trees is not None:
                work_trees.forget(session)
            notify_snapshot()
            return 200, {"ok": True, "killed": True}
        if "can't find session" in stderr or "no server running" in stderr:
            store.void_open_work(session)
            verdict_left_row(session, "expired")
            if auto_names is not None:
                auto_names.forget(session)
            if stars is not None:
                stars.forget(session)
            if work_trees is not None:
                work_trees.forget(session)
            # Already gone (or no tmux server at all) — the caller's goal
            # state is met, so this is success, just nothing to do.
            return 200, {"ok": True, "killed": False, "detail": "session not found"}
        return 500, {"error": (stderr or "tmux kill-session failed").strip()}

    launch_directories = LaunchDirectories(start_dir)

    def spawn_session_now(
        name: str,
        profile_id: str,
        prompt: object,
        lineage_for: Callable[[], dict],
        by: str,
        *,
        auto: bool = False,
        model: object = None,
        subagent_model: object = None,
        permission_mode: object = None,
        cwd: str | None = None,
    ) -> tuple[int, dict]:
        """Start a NEW tmux session on a resolved profile. POST /spawn and the LEAD launch share it.
        `lineage_for` runs only after the name is known to be free (it may stamp a legacy parent).
        `model` / `subagent_model`: owned by profiles.py § LAUNCH MODELS — see the note there.
        `permission_mode`: owned by profiles.py (`PERMISSION_ARGS`) — see the note there."""
        profile = profiles.get(profile_id) if profiles is not None else None
        if profile is None or profiles is None:
            return 400, {"ok": False, "error": f"unknown profile '{profile_id}'"}
        refused = profiles.launch_refusal(profile_id)
        if refused:
            return 400, {"ok": False, "error": refused}
        if prompt is not None:
            prompt_error = spawn_prompt_error(prompt, profile.kind)
            if prompt_error:
                return 400, {"ok": False, "error": prompt_error}
        if not profiles.cli_found(profile_id):
            return 503, {"ok": False, "error": f"the `{profile.kind}` CLI was not found"}
        model_refused = profiles.model_refusal(profile_id, model, subagent_model)
        if model_refused:
            return 400, {"ok": False, "error": model_refused}
        if permission_mode is not None:
            mode_refused = profiles.permission_refusal(profile_id, permission_mode)
            if mode_refused:
                return 400, {"ok": False, "error": mode_refused}
        if name in list_tmux_sessions():
            return 409, {"ok": False, "error": f"a session named '{name}' already exists"}
        try:
            lineage = lineage_for()
        except ValueError as err:
            return 400, {"ok": False, "error": str(err)}
        # The name is free, so whatever the stores still hold under it
        # describes a PREVIOUS occupant — a reused `vc-3` must not open
        # wearing the last one's state, transcript binding or Opus label
        # (merge_snapshot's model fallback reads the disk store). Forgotten
        # BEFORE the spawn so a first hook from the new agent is never
        # wiped behind it. The icon is kept: a recreated name keeps its pick.
        store.forget(name)
        verdict_left_row(name, "expired")
        if auto_names is not None:
            auto_names.forget(name)
        if next_steps is not None:
            next_steps.forget(name)
        model_store.forget(name)
        transcripts.forget(name)
        notifier.forget(name)
        forget_attention(name)
        if personas is not None:
            personas.forget(name)
        if stars is not None:  # unlike the icon: a star is the earlier conversation's (stars.py)
            stars.forget(name)
        if missions is not None:
            missions.forget(name)
        if records is not None:
            records.forget(name)
        if work_trees is not None:
            work_trees.forget(name)
        cwd = cwd or (start_dir if start_dir and os.path.isdir(start_dir) else str(Path.home()))
        try:
            result = subprocess.run(
                profiles.spawn_argv(
                    profile_id,
                    name,
                    cwd,
                    [prompt] if prompt else (),
                    lineage=lineage,
                    model=model,
                    subagent_model=subagent_model,
                    permission_mode=permission_mode,
                ),
                capture_output=True,
                text=True,
                timeout=SPAWN_TIMEOUT_SECONDS,
                check=False,
            )
        except ValueError as err:
            return 400, {"ok": False, "error": str(err)}
        except subprocess.TimeoutExpired:
            return 500, {"ok": False, "error": "tmux new-session timed out"}
        except OSError as err:
            return 503, {"ok": False, "error": f"could not run tmux ({err})"[:200]}
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "duplicate session" in stderr.lower():
                # Lost a race the probe above could not see — same answer.
                return 409, {"ok": False, "error": f"a session named '{name}' already exists"}
            return 500, {"ok": False, "error": (stderr or "tmux new-session failed")[:300]}
        click.echo(f"tldrack: spawned '{name}' on profile '{profile_id}'{' (auto)' if auto else ''} ({by})")
        notify_snapshot()
        body: dict = {"ok": True, "session": name, "profile": profile_id}
        if auto:
            body["auto"] = True
        if model:
            body["model"] = model
        if subagent_model:
            body["subagent_model"] = subagent_model
        if permission_mode:
            body["permission_mode"] = permission_mode
        if prompt:
            note_prompt(name, prompt)
            body["prompt"] = True
        return 201, body

    def restamp_lineage(session: str, stamp: dict) -> bool:
        """Set a live session's `@vc_lineage` (lead adopt: the fresh lead wears its run's identity)."""
        try:
            done = subprocess.run(["tmux", *lineage_args(session, stamp)[1:]], capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        notify_snapshot()
        return done.returncode == 0

    def ranked_profiles() -> list[str]:
        pick = snapshot_payload().get("launch_pick") or {}
        ranking = [c for c in pick.get("ranking") or [] if isinstance(c, dict)]
        return [c["id"] for c in ranking if not c.get("spent")] + [c["id"] for c in ranking if c.get("spent")]

    # The decision inbox (rung 2) stands alone; the LEAD is its first source and needs the profiles.
    decision_inbox = Inbox(rack_db) if rack_db is not None else None
    if decision_inbox is not None and decisions is not None:
        decision_inbox.on_resolved(decisions.record_inbox)
    if decision_inbox is not None and profiles is not None:
        lead_service = LeadService(
            runs=LeadRuns(rack_db),
            inbox=decision_inbox,
            typist=PaneTypist(rack_db),
            gate=SubscriptionGate(profiles),
            run_root=rack_db.path.parent / "runs",
            models=LeadModels(),
            bridge=LeadBridge(
                rows=lambda: snapshot_payload().get("sessions") or [],
                summaries=lambda: snapshot_payload().get("profiles") or [],
                ranked=ranked_profiles,
                kind_of=profiles.kind_of,
                prepare_lineage=profiles.prepare_lineage,
                spawn=lambda name, profile_id, prompt, lineage, model=None, subagent_model=None: spawn_session_now(
                    name, profile_id, prompt, lambda: lineage, "LEAD launch", model=model, subagent_model=subagent_model
                ),
                model_refusal=profiles.model_refusal,
                kill=lambda session: kill_session_now(session, "the LEAD's verified close"),
                restamp=restamp_lineage,
                worktree=lambda: capability("worktree"),
                closing=transcripts.closing_text,
                notify=lambda title, body: notifier.send_event(title, body),
                git=run_git,
                start_dir=start_dir,
                label_lead=label_from_title,
                on_change=notify_snapshot,
                log=lambda message: click.echo(message, err=True),
            ),
        )

    class StatusHandler(BridgeHandler):
        """The core's handler (guards, CORS, plugins, /snapshot) plus this
        bridge's own routes. Every route here runs behind the core's Host and
        Origin guards, because those live in do_GET/do_POST, not in the routes."""

        # The topic-label queue, reachable so a test can `drain()` it.
        topics = topic_namer
        # The suggested next step (next_step.py): started by start_status_server, drained by tests.
        suggester = next_steps
        # The attention telemetry queue and the stuck clocks, reachable for the same reason.
        signal_queue = signals
        stuck_detector = stuck
        phone = notifier

        server_version = "tldrack"
        # Loopback only unless the operator names origins (settings.py `allowed_origins`): this
        # bridge serves its own page (`_serve_ui`), so no hosted site is ever allowed by default.
        ALLOWED_ORIGINS = settings.allowed_origins
        # Sent on every response: a page here may be framed only by a page that may call it (framing.py).
        FRAME_POLICY = framing.frame_ancestors(ALLOWED_ORIGINS)

        GET_ROUTES = {
            "/profile-availability": "_handle_get_profile_availability",
            "/launch-directories": "_handle_get_launch_directories",
            "/features": "_handle_get_features",
            "/update": "_handle_get_update",
            # Shells out (`ps`, `lsof`) on every call, so it also refuses a no-cors browser GET.
            "/doctor": "_route_doctor",
            "/ws": "_handle_ws_proxy",
            # /events, /capture and /wait each cost real resources per call (an
            # SSE slot, a tmux shell-out, a held thread), so they additionally
            # refuse a no-cors browser GET — see _expensive_route_guard.
            "/events": "_route_events",
            "/activity": "_handle_activity",
            "/notify": "_handle_notify_state",
            "/capture": "_route_capture",
            "/wait": "_route_wait",
            "/status": "_handle_get_status",
            "/restorable": "_handle_restorable",
            "/closed": "_handle_closed",
            "/permission/ledger": "_handle_permission_ledger",
            "/permission/stats": "_handle_permission_stats",
            "/persona": "_handle_get_persona",
            "/topic-labels": "_handle_get_topic_labels",
            "/missions": "_handle_get_missions",
            "/costs": "_handle_get_costs",
            "/rack-metrics": "_handle_rack_metrics",
            "/next-step": "_handle_get_next_step",
            "/decisions": "_handle_get_decisions",
            "/ledger/status": "_handle_ledger_status",
            "/ledger/search": "_handle_ledger_history",
            "/ledger/conversation": "_handle_ledger_history",
            "/ledger/threads": "_handle_ledger_history",
            "/ledger/rack-search": "_handle_rack_search",
            "/lead/state": "_handle_lead_state",
            "/inbox": "_handle_inbox",
            "/decision-secret": "_handle_decision_secret",
        }
        POST_ROUTES = {
            "/profile-availability": "_handle_save_profile_availability",
            "/launch-directories": "_handle_save_launch_directories",
            "/features": "_handle_save_features",
            "/update": "_handle_post_update",
            "/images": "_handle_image",
            "/shutdown": "_handle_shutdown",
            "/kill-session": "_handle_kill_session",
            "/spawn": "_handle_spawn",
            "/profiles/install-hooks": "_handle_install_hooks",
            "/rename-session": "_handle_rename_session",
            "/restore": "_handle_restore",
            "/restore/forget": "_handle_restore_forget",
            "/reopen": "_handle_reopen",
            "/focus": "_handle_focus",
            "/session-icon": "_handle_session_icon",
            "/session-star": "_handle_session_star",
            "/persona": "_handle_set_persona",
            "/topic-labels": "_handle_post_topic_labels",
            "/missions": "_handle_post_missions",
            "/errors/fix": "_handle_error_fix",
            "/decisions/import": "_handle_decisions_import",
            "/rack-metrics/event": "_handle_rack_metric_event",
            "/rack-metrics/settings": "_handle_rack_metric_settings",
            "/next-step/event": "_handle_next_step_event",
            "/next-step/settings": "_handle_next_step_settings",
            "/ledger/ingest": "_handle_ledger_ingest",
            "/open-work/dismiss": "_handle_open_work_dismiss",
            "/open-work/snooze": "_handle_open_work_snooze",
            "/open-work/confirm": "_handle_open_work_confirm",
            "/open-work/read": "_handle_open_work_read",
            "/presence": "_handle_presence",
            "/notify": "_handle_notify",
            "/notify/test": "_handle_notify_test",
            "/notify/send": "_handle_notify_send",
            "/hook": "_handle_hook",
            "/usage": "_handle_usage",
            "/status": "_handle_post_status",
            # The LEAD launch (lead/service.py): one handler, the verb is the path's last segment.
            **{f"/lead/{verb}": "_handle_lead_verb" for verb in LEAD_VERBS},
            # The operator's decision verbs, behind the per-launch secret (decision_secret.py).
            **{f"/inbox/{verb}": "_handle_inbox_verb" for verb in ("answer", "defer", "dismiss")},
        }

        def snapshot_payload(self) -> dict:
            return snapshot_payload()

        def do_GET(self) -> None:
            # A read from this bridge's own page names no Origin: stated here, ahead of every guard
            # and every plugin's caller policy (own_page.py owns why, and why loopback only).
            stated = own_page.own_page_origin(
                self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site"), self.headers.get("Host")
            )
            if stated is not None:
                self.headers["Origin"] = stated
            # A path that is no API route may be a file of the rack page (ui_static.py). The API's
            # own routes are asked first, so a build can never shadow one; the guards run here
            # exactly as the core runs them in front of every route.
            path = urlsplit(self.path).path
            if path != "/snapshot" and path not in self.GET_ROUTES and not path.startswith("/plugin/"):
                if not self._host_guard() or not self._origin_guard():
                    return
                if self._serve_ui(path):
                    return
            super().do_GET()

        def _serve_ui(self, path: str) -> bool:
            """Answer `path` from the built page. False when it names no file: the caller's 404."""
            ui_dist = find_ui() if find_ui is not None else None
            if ui_dist is None:
                if path != "/":
                    return False
                body, kind, cache, code = ui_static.NOT_BUILT.encode(), "text/html; charset=utf-8", "no-store", 503
            else:
                found = ui_static.resolve(ui_dist, path)
                if found is None:
                    return False
                try:
                    body = found.read_bytes()
                except OSError:
                    return False
                kind, cache, code = ui_static.content_type(found), ui_static.cache_control(ui_dist, found), 200
            self.send_response(code)
            self._send_cors_headers()
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return True

        def end_headers(self) -> None:
            if urlsplit(self.path).path in (
                "/ledger/search",
                "/ledger/conversation",
                "/ledger/threads",
                "/ledger/rack-search",
                "/rack-metrics",
                "/decision-secret",
                "/lead/state",
                "/inbox",
                "/topic-labels",
            ):
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", self.FRAME_POLICY)
            super().end_headers()

        def send_header(self, keyword: str, value: str) -> None:
            # The core's preflight allows Content-Type only; an operator verb also carries the decision
            # secret (decision_secret.py), a non-safelisted header the browser preflights.
            if keyword == "Access-Control-Allow-Headers" and urlsplit(self.path).path.startswith("/inbox/"):
                value = f"{value}, {SECRET_HEADER}"
            super().send_header(keyword, value)

        def _handle_image(self) -> None:
            if not self._origin_allowed():
                return
            payload = self._read_json_object(MAX_IMAGE_BODY_BYTES)
            if payload is None:
                return
            try:
                result = save_image(payload)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            except OSError:
                self._send_json(500, {"error": "Could not save image on the bridge host"})
                return
            self._send_json(201, result)

        def _query(self) -> dict:
            return parse_qs(urlparse(self.path).query)

        def _session_body(self) -> tuple[dict, str] | None:
            """The opening of a POST that acts on one session: the Origin guard, a JSON object body and its
            `session`, which must be a valid name. Answers the refusal itself and returns None when one fails."""
            if not self._origin_allowed():
                return None
            payload = self._read_json_object()
            if payload is None:
                return None
            session = payload.get("session")
            if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return None
            return payload, session

        def _route_events(self) -> None:
            if self._expensive_route_guard():
                self._handle_events()

        def _route_capture(self) -> None:
            if self._expensive_route_guard():
                self._handle_capture(self._query())

        def _route_wait(self) -> None:
            if self._expensive_route_guard():
                self._handle_wait(self._query())

        def _handle_notify_state(self) -> None:
            # Never carries the topic — see NotifyDaemon.state(). On hosted
            # ntfy the topic is the only secret there is, and a UI needs to
            # know it is SET, never what it is.
            self._send_json(200, notifier.state())

        def _handle_get_status(self) -> None:
            session = (self._query().get("session") or [default_session])[0]
            self._send_json(200, store.get(session))

        def _handle_shutdown(self) -> None:
            if not self._origin_allowed():
                return
            click.echo(f"tldrack: shutdown requested via POST /shutdown (origin: {self._requesting_origin() or 'none'})")
            # Respond first, then trigger — the process is about to exit.
            self._send_json(200, {"ok": True, "pid": bridge_info["pid"], "mode": control.mode})
            control.trigger()

        def _handle_post_status(self) -> None:
            """POST /status — the legacy state-only report (no activity/model)."""
            payload = self._read_json_object()
            if payload is None:
                return
            session = payload.get("session") or default_session
            state = payload.get("state")
            message = payload.get("message")
            if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            if state not in VALID_STATES:
                self._send_json(400, {"error": f"state must be one of: {', '.join(VALID_STATES)}"})
                return
            if message is not None and not isinstance(message, str):
                self._send_json(400, {"error": "message must be a string"})
                return
            previous = store.set(session, state, message[:MAX_MESSAGE_LEN] if message else None)
            record_transition(session, "status", previous, state, KEEP_ACTIVITY)
            if previous.get("state") != state:
                click.echo(f"  status: {session}: {previous.get('state') or '(new)'} → {state}")
            notify_snapshot()
            self._send_json(200, {"ok": True})

        def _handle_kill_session(self) -> None:
            """POST /kill-session?session=<name> — kill ONE tmux session (the rack's
            quick-close). Origin-guarded like /shutdown: it acts on
            the machine. Killing the session also ends any ttyd client attached to
            it. A session that's already gone reports killed: false but still 200 —
            the caller's goal state (\"session doesn't exist\") is met either way.
            """
            if not self._origin_allowed():
                return
            session = (parse_qs(urlparse(self.path).query).get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            self._send_json(*kill_session_now(session, f"POST /kill-session (origin: {self._requesting_origin() or 'none'})"))

        def _handle_spawn(self) -> None:
            """POST /spawn {"name": <session>, "profile": <id>} — start a NEW tmux
            session running a launch profile, stamped with it (profiles.py).

            `+ NEW` used to need no route: pointing ttyd at an unused
            `?arg=<name>` made the session, attach-or-create. That door can only
            ever open a bare shell in the one start dir; a session running a
            profile's CLI on a chosen account needs a real `tmux new-session`,
            and this is it. Origin-guarded like /kill-session: it acts on the
            machine.

            Optional `"prompt"`: the agent's first message, handed to the CLI as
            its positional argument (`codex "<prompt>"`, `claude "<prompt>"`),
            so a batch launcher needs no type-into-the-pane step. One line, at
            most SPAWN_PROMPT_MAX chars (the 4 KiB body cap: point at a file for
            anything longer), never starting with `-` (the CLI would read a
            flag). Refused on a plain-shell profile, which has no CLI to take it.

            Optional `"permission_mode"` (`"auto"`): the session starts in that
            mode, so a worker a launcher starts does not stop on its first
            approval. Refused (400) where the profile cannot start in it; the
            body echoes it when it was applied.

            201 created · 400 bad body, name, profile or prompt · 409 the name is taken
            · 503 no CLI for that profile (or a bridge built without profiles)
            · 500 tmux failed. 404 keeps its one meaning: a bridge too old to
            have this route.
            """
            if not self._origin_allowed():
                return
            payload = self._read_json_object()
            if payload is None:
                return
            name = payload.get("name")
            profile_id = payload.get("profile")
            if not isinstance(name, str) or not SESSION_NAME_RE.fullmatch(name):
                self._send_json(400, {"ok": False, "error": "invalid session name — must match [A-Za-z0-9_-]+"})
                return
            # An agent never takes a machinery name: service_names.py owns why.
            if reserved_error(name):
                self._send_json(400, {"ok": False, "error": reserved_error(name)})
                return
            if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
                self._send_json(400, {"ok": False, "error": "invalid profile id"})
                return
            if profiles is None:
                self._send_json(503, {"ok": False, "error": "this bridge has no launch profiles"})
                return
            auto = profile_id == AUTO_PROFILE_ID
            if auto:
                # The freest account, picked now (launch_pick.py). The stamp,
                # the echo and the log all name the REAL profile.
                profile_id = profiles.pick_launch(
                    payload.get("model"), payload.get("subagent_model"), payload.get("permission_mode")
                )
                if profile_id is None:
                    self._send_json(503, {"ok": False, "error": "no launchable agent profile for auto"})
                    return
            if profiles.get(profile_id) is None:
                self._send_json(400, {"ok": False, "error": f"unknown profile '{profile_id}'"})
                return
            try:
                cwd = launch_directories.resolve(payload.get("cwd"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            status, body = spawn_session_now(
                name,
                profile_id,
                payload.get("prompt"),
                lambda: profiles.prepare_lineage(payload.get("parent_session")),
                f"origin: {self._requesting_origin() or 'none'}",
                auto=auto,
                cwd=cwd,
                model=payload.get("model"),
                subagent_model=payload.get("subagent_model"),
                permission_mode=payload.get("permission_mode"),
            )
            self._send_json(status, body)

        def _handle_get_profile_availability(self) -> None:
            if profiles is None:
                self._send_json(503, {"error": "Launch profiles are unavailable"})
                return
            self._send_json(200, profiles.availability.view())

        def _handle_save_profile_availability(self) -> None:
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            if profiles is None:
                self._send_json(503, {"error": "Launch profiles are unavailable"})
                return
            try:
                state = profiles.availability.save(body)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(500, {"error": "Could not save profile availability to disk"})
                return
            notify_snapshot()
            self._send_json(200, state)

        def _handle_get_launch_directories(self) -> None:
            try:
                self._send_json(200, launch_directories.view())
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})

        def _handle_save_launch_directories(self) -> None:
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            try:
                state = launch_directories.save(body)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(500, {"error": "Could not save launch directories to disk"})
                return
            self._send_json(200, state)

        def _handle_get_features(self) -> None:
            """GET /features: every feature switch, on or off, in the Settings page's words (features.py)."""
            self._send_json(200, switches.view())

        def _handle_save_features(self) -> None:
            """POST /features `{"set": {key: bool}}`: switch parts of this bridge off or back on.
            Origin-guarded: it writes the operator's config. It can name no path and load no code."""
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            try:
                view = switches.save(body)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError:
                self._send_json(500, {"error": "Could not save the feature switches to disk"})
                return
            click.echo(f"  features: off → {', '.join(switches.off_keys()) or 'nothing'}")
            self._send_json(200, view)

        def _handle_get_update(self) -> None:
            """GET /update: is the checkout behind its remote, is the page built from it, is the
            running code the code on disk (update.py). The last look, never a fetch: cheap to poll."""
            if updater is None:  # a test-built handler: there is no checkout to speak for
                self._send_json(503, {"error": "this bridge cannot update itself"})
                return
            self._send_json(200, updater.view())

        def _handle_post_update(self) -> None:
            """POST /update `{"action": "check" | "apply"}`: look at the remote, or pull, rebuild the
            page and restart as due. Origin-guarded: it runs git and npm and can restart the bridge.
            The body names an action and nothing else; what is pulled is read from the checkout."""
            if not self._origin_allowed():
                return
            if updater is None:
                self._send_json(503, {"error": "this bridge cannot update itself"})
                return
            body = self._read_json_object()
            if body is None:
                return
            status, payload = updater.request(body.get("action"))
            if status == 202:
                click.echo(f"  update: {body.get('action')} requested (origin: {self._requesting_origin() or 'none'})")
            self._send_json(status, payload)

        def _route_doctor(self) -> None:
            if self._expensive_route_guard():
                self._handle_doctor()

        def _handle_doctor(self) -> None:
            """GET /doctor: `tldrack doctor` as JSON, for the Settings page's SETUP tab (doctor.py
            `wire`). The same facts the command gathers, with this bridge's own snapshot in place
            of the one the command fetches. Changes nothing."""
            status_port = bridge_info.get("status_port")
            if not status_port:  # a test-built handler: there are no ports to describe
                self._send_json(503, {"error": "this bridge does not know its ports"})
                return
            try:
                facts = doctor_facts(
                    bridge_info.get("port") or status_port - 1,
                    status_port,
                    self.snapshot_payload(),
                    switches,
                    worktree_repos=served_worktree_repos_now(),
                )
            except Exception as exc:  # noqa: BLE001 — a check that cannot run is an error line, never a traceback
                self._send_json(500, {"error": f"doctor could not read this machine: {type(exc).__name__}"})
                return
            self._send_json(200, doctor_mod.wire(doctor_mod.run_checks(facts)))

        def _handle_rename_session(self) -> None:
            """POST /rename-session {"session": <old>, "name": <new>} — rename ONE
            tmux session (the agent rack's ✎, which exists so a rack isn't a wall
            of `vc-1`, `vc-2`). Origin-guarded like /kill-session: it acts on the
            machine.

            A REAL rename, not a display alias, so the new name is what `tmux ls`,
            a terminal app, a native client and the status line all show. Claude Code hooks
            keep reporting correctly with no migration because they resolve the
            name at post time — see `hook_command`, which builds
            `?session=$(tmux display-message -p '#S')`. Claude's own index keeps
            the OLD name, so the arbiter joins it by pane id: agent_index.py rule 3.

            The caller must re-point its ttyd iframe afterwards: the live socket
            survives (tmux clients attach by id, not name), but the URL still says
            `?arg=<old>`, and with ttyd's `-a` that means `tmux new -A -s <old>` —
            a reload would silently create an empty session under the freed name.
            """
            if not self._origin_allowed():
                return
            payload = self._read_json_object()
            if payload is None:
                return
            old = payload.get("session")
            new = payload.get("name")
            # Both ends validated: `old` lands in a tmux target and `new` becomes
            # a session name every other route will look up by.
            if not isinstance(old, str) or not SESSION_NAME_RE.fullmatch(old):
                self._send_json(400, {"error": "invalid session name"})
                return
            if not isinstance(new, str) or not SESSION_NAME_RE.fullmatch(new):
                self._send_json(400, {"error": "invalid new name — must match [A-Za-z0-9_-]+"})
                return
            # Never INTO a machinery name (service_names.py owns why). Out of one stays open: it is
            # the way back for a session that already carries such a name.
            if reserved_error(new) and reserved_prefix(old) is None:
                self._send_json(400, {"error": reserved_error(new)})
                return
            if old == new:
                if auto_names is not None:
                    auto_names.manual(old, new)
                notify_snapshot()
                self._send_json(200, {"ok": True, "renamed": False, "detail": "name unchanged"})
                return
            # Answer the collision ourselves rather than forwarding tmux's stderr:
            # renaming ONTO a live session is the one failure a user hits by
            # accident, and "duplicate session" deserves a status code the client
            # can branch on.
            if new in list_tmux_sessions():
                self._send_json(409, {"error": f"a session named '{new}' already exists"})
                return
            # '=' prefix = exact match, for the same reason kill-session uses it:
            # a bare -t would prefix-match some OTHER session.
            result = subprocess.run(
                ["tmux", "rename-session", "-t", f"={old}", new],
                capture_output=True,
                text=True,
                check=False,
            )
            stderr = result.stderr or ""
            if result.returncode != 0:
                if "can't find session" in stderr or "no server running" in stderr:
                    # 410, NOT 404. Every client of this bridge reads 404 as
                    # "this bridge predates the route — go restart it", which is
                    # very different advice from "that session died while you
                    # were typing". Keep 404 meaning exactly one thing.
                    self._send_json(410, {"error": f"session '{old}' not found"})
                else:
                    self._send_json(500, {"error": (stderr or "tmux rename-session failed").strip()})
                return
            # Carry the per-session state across BEFORE answering. Skipping this
            # leaves `old` in the StatusStore with no tmux session behind it,
            # which merge_snapshot reports as live: false — a permanent
            # "— session gone —" ghost row in the rack. The icon pick would
            # likewise revert to its hash default.
            if topic_labels is not None:
                topic_labels.on_rename(old, new, auto_names.final_label(old) if auto_names is not None else None)
            store.rename(old, new)
            open_work_reactions.rename(old, new)
            if verdicts is not None:
                verdicts.rename(old, new)
            if work_trees is not None:
                work_trees.rename(old, new)
            if next_steps is not None:
                next_steps.rename(old, new)
            if loops is not None:
                loops.rename(old, new)
            stuck.rename(old, new)
            if old in verdict_landed:
                verdict_landed[new] = verdict_landed.pop(old)
            if auto_names is not None:
                auto_names.manual(old, new)
            icon_store.rename(old, new)
            if personas is not None:
                personas.rename(old, new)
            if stars is not None:
                stars.rename(old, new)
            if missions is not None:
                missions.rename(old, new)
            if costs is not None:
                costs.rename(old, new)
            # Without these two the renamed session drops back to an unknown
            # model — the very bug models.py and statusline_models.py exist to
            # fix, re-entered through rename. The tracker carries the transcript
            # binding the statusline tier joins on, so it moves too.
            model_store.rename(old, new)
            transcripts.rename(old, new)
            pane_sessions.invalidate()  # the pane kept its id; its name changed
            if records is not None:
                records.rename(old, new)
            # The notify cooldown is deliberately DROPPED rather than carried:
            # the rename is a deliberate act on a session the user is looking
            # at, so the 15-minute silence earned by its old name is not
            # something they would expect to inherit.
            notifier.forget(old)
            click.echo(f"tldrack: renamed tmux session '{old}' → '{new}' (origin: {self._requesting_origin() or 'none'})")
            notify_snapshot()
            self._send_json(200, {"ok": True, "renamed": True, "session": new})

        def _handle_restorable(self) -> None:
            """GET /restorable — every recorded session with no live tmux
            session behind it, and whether POST /restore could bring it back
            (`ok`, else `reason`). Never carries a conversation id. The refusal
            table is owned by restore.py."""
            if records is None or profiles is None:
                self._send_json(503, {"error": "this bridge keeps no session records"})
                return
            plans = restore_plans(set(list_tmux_sessions()), check_transcript=True)
            # The first-prompt name the rack showed, so a `vc-N` row says what the agent was doing
            # (the same join a close makes: `file_close`).
            labels = auto_names.get_all() if auto_names is not None else {}
            rows = []
            for plan in plans:
                row = plan.row()
                if labels.get(plan.session):
                    row["label"] = labels[plan.session]
                rows.append(row)
            self._send_json(200, {"max_age_days": RECORD_MAX_AGE_DAYS, "sessions": rows})

        def _handle_restore(self) -> None:
            """POST /restore {"sessions"?: [<name>, …]} — respawn recorded
            sessions under their old names, resuming each conversation where
            one can be (`claude --resume=<id>`). `{}` restores every `ok` row.

            A BUTTON, never automatic: nothing calls this at boot. Origin-guarded
            like /spawn — it starts agents. Serial under one lock, so two clicks
            cannot spawn one name twice. Per-row outcomes ride `results`
            (`restored`, `resumed`, or a `reason`); the request itself is 200.
            """
            if not self._origin_allowed():
                return
            if records is None or profiles is None:
                self._send_json(503, {"ok": False, "error": "this bridge keeps no session records"})
                return
            payload = self._read_json_object(MAX_RESTORE_BODY_BYTES)
            if payload is None:
                return
            requested = payload.get("sessions")
            if requested is not None and (
                not isinstance(requested, list)
                or not all(isinstance(name, str) and SESSION_NAME_RE.fullmatch(name) for name in requested)
            ):
                self._send_json(400, {"ok": False, "error": "sessions must be a list of session names"})
                return
            with restore_lock:
                live = set(list_tmux_sessions())
                plans = {plan.session: plan for plan in restore_plans(live, check_transcript=True)}
                names = (
                    list(dict.fromkeys(requested)) if requested is not None else [name for name, plan in plans.items() if plan.ok]
                )
                results = [self._restore_one(name, plans.get(name), live) for name in names]
            if any(result["restored"] for result in results):
                notify_snapshot()
            self._send_json(200, {"ok": True, "results": results})

        def _restore_one(self, name: str, plan, live: set[str]) -> dict:
            if name in live:
                return {"session": name, "restored": False, "reason": "already running"}
            if plan is None:
                return {"session": name, "restored": False, "reason": "no record"}
            if not plan.ok:
                reason = f"cwd gone: {plan.cwd}" if plan.reason == "cwd gone" and plan.cwd else plan.reason
                return {"session": name, "restored": False, "reason": reason}
            remembered = records.get(name) if records is not None else None
            reason = self._respawn(plan, (remembered or {}).get("lineage"), "restored")
            if reason is not None:
                return {"session": name, "restored": False, "reason": reason}
            return {"session": name, "restored": True, "resumed": plan.resume is not None}

        def _respawn(self, plan, lineage: dict | None, verb: str) -> str | None:
            """THE respawn behind /restore and /reopen: one `ok` plan, under
            `plan.session`. None on success, else the reason it failed."""
            name = plan.session
            # The name is free again, so whatever the live stores hold under it
            # describes the session that died — same hygiene as /spawn. The
            # model reading is KEPT when resuming: same conversation, same model.
            store.forget(name)
            transcripts.forget(name)
            notifier.forget(name)
            if plan.resume is None:
                model_store.forget(name)
                forget_attention(name)  # a resumed conversation keeps its loop
                if work_trees is not None:  # and where it worked (work_trees.py)
                    work_trees.forget(name)
            try:
                argv = restore_argv(plan, profiles)
                if lineage:
                    argv += lineage_args(name, lineage)
                result = subprocess.run(argv, capture_output=True, text=True, timeout=SPAWN_TIMEOUT_SECONDS, check=False)
            except subprocess.TimeoutExpired:
                return "tmux new-session timed out"
            except OSError as err:
                return f"could not run tmux ({err})"[:200]
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return "already running" if "duplicate session" in stderr.lower() else (stderr or "tmux failed")[:300]
            click.echo(
                f"tldrack: {verb} '{name}' on profile '{plan.spawn_profile or 'none'}'"
                f"{' (resumed)' if plan.resume else ''} (origin: {self._requesting_origin() or 'none'})"
            )
            return None

        def _handle_closed(self) -> None:
            """GET /closed — the recent closes POST /reopen can bring back,
            newest first. What counts as a close, and what is left off the
            list: closed_sessions.py and `closed_plans`. Never carries a
            conversation id."""
            if closed is None or profiles is None:
                self._send_json(503, {"error": "this bridge keeps no closed-session list"})
                return
            rows = [
                {
                    **plan.row(),
                    "id": closed_id,
                    "label": entry.get("label"),
                    "how": entry["how"],
                    "closed_at": entry["closed_at"],
                    # Additive: the close carried a star (stars.py). Absent otherwise.
                    **({"starred": True} if entry.get("starred_at") else {}),
                }
                for closed_id, entry, plan in closed_plans(check_transcript=True)
            ]
            self._send_json(200, {"max_age_days": RECORD_MAX_AGE_DAYS, "sessions": rows})

        def _handle_reopen(self) -> None:
            """POST /reopen {"id": <close id>} — bring one closed session back
            where it was, on the same account, resuming its conversation where
            one can be (`claude --resume=<id>`), under its old name when that
            is free (`closed_sessions.reopen_name` otherwise).

            Origin-guarded like /spawn — it starts an agent. Serial under the
            restore lock, and the reopen TAKES the entry, so a double click
            finds nothing the second time. 200 {ok, session, profile, resumed} · 400 bad
            body · 404 not on the list (reopened, aged out, or running again) ·
            409 refused (the plan's reason, or no free name) · 500 tmux failed ·
            503 no store."""
            if not self._origin_allowed():
                return
            if closed is None or profiles is None:
                self._send_json(503, {"ok": False, "error": "this bridge keeps no closed-session list"})
                return
            payload = self._read_json_object(MAX_RESTORE_BODY_BYTES)
            if payload is None:
                return
            closed_id = payload.get("id")
            if not is_closed_id(closed_id):
                self._send_json(400, {"ok": False, "error": "id must be a close id from GET /closed"})
                return
            with restore_lock:
                found = next(
                    ((entry, plan) for cid, entry, plan in closed_plans(check_transcript=True) if cid == closed_id), None
                )
                if found is None:
                    self._send_json(404, {"ok": False, "error": "that session is no longer on the closed list"})
                    return
                entry, plan = found
                if not plan.ok:
                    self._send_json(409, {"ok": False, "error": plan.reason})
                    return
                name = reopen_name(entry["session"], list_tmux_sessions())
                if name is None:
                    self._send_json(409, {"ok": False, "error": f"no free session name near {entry['session']}"})
                    return
                reason = self._respawn(replace(plan, session=name), entry.get("lineage"), "reopened")
                if reason is not None:
                    self._send_json(409 if reason == "already running" else 500, {"ok": False, "error": reason})
                    return
                closed.take(closed_id)
                if auto_names is not None:
                    auto_names.adopt(name, entry.get("label"))
                if stars is not None:
                    stars.adopt(name, entry.get("starred_at"))
            notify_snapshot()
            self._send_json(200, {"ok": True, "session": name, "profile": plan.spawn_profile, "resumed": plan.resume is not None})

        def _handle_restore_forget(self) -> None:
            """POST /restore/forget {"sessions": [<name>, …]} — drop records the
            user does not want offered again. Origin-guarded: it writes to disk."""
            if not self._origin_allowed():
                return
            if records is None:
                self._send_json(503, {"ok": False, "error": "this bridge keeps no session records"})
                return
            payload = self._read_json_object(MAX_RESTORE_BODY_BYTES)
            if payload is None:
                return
            names = payload.get("sessions")
            if not isinstance(names, list) or not all(
                isinstance(name, str) and SESSION_NAME_RE.fullmatch(name) for name in names
            ):
                self._send_json(400, {"ok": False, "error": "sessions must be a list of session names"})
                return
            forgotten = [name for name in dict.fromkeys(names) if records.forget(name)]
            if forgotten:
                notify_snapshot()
            self._send_json(200, {"ok": True, "forgotten": forgotten})

        def _handle_wait(self, query: dict) -> None:
            """GET /wait?session=<name>&for=<state[,state]>[&timeout_ms=] — block
            until a session reaches one of the wanted states, then answer.

            Makes the bridge *agent-addressable*: a script or a coordinating agent
            can rendezvous on another session's state instead of busy-polling
            /status and re-inventing a debounce. Convergent design — Orca ships
            `terminal wait --for tui-idle`, herdr a socket `agent.wait`.

            Deliberately a poll loop over the in-memory StatusStore, NOT a
            registration on the SSE broker: a broker client burns one of only 16
            slots and is fed whole snapshot payloads it would just discard, so
            waiters would starve real SSE consumers. ThreadingHTTPServer gives
            every request its own thread, and the store read is a dict lookup
            under a lock.

            Returns immediately when the state already matches, so
            prompt-then-wait can't lose a race against a fast agent.

            Guarded like every route (do_GET checks Host then Origin) and then
            once more: read-only does not mean cheap, and this is the most
            expensive route on the server — see _expensive_route_guard.
            """
            session = (query.get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            wanted = parse_wait_states((query.get("for") or [None])[0])
            if wanted is None:
                self._send_json(400, {"error": f"for= must be a comma-separated subset of: {', '.join(WAITABLE_STATES)}"})
                return
            timeout_ms = parse_timeout_ms((query.get("timeout_ms") or [None])[0])

            # Pin the occupant BEFORE the first state read, so a session that is
            # replaced mid-wait can never satisfy it (herdr's guard).
            pinned_id = tmux_session_id(session)
            started = time.monotonic()
            deadline = started + timeout_ms / 1000.0
            while True:
                state = store.get(session)["state"]
                outcome = wait_outcome(state, wanted, pinned_id, tmux_session_id(session))
                waited_ms = round((time.monotonic() - started) * 1000)
                if outcome != "waiting":
                    self._send_json(
                        200,
                        {
                            "session": session,
                            "state": state,
                            "matched": outcome == "matched",
                            "replaced": outcome == "replaced",
                            "timed_out": False,
                            "waited_ms": waited_ms,
                        },
                    )
                    return
                if time.monotonic() >= deadline:
                    self._send_json(
                        200,
                        {
                            "session": session,
                            "state": state,
                            "matched": False,
                            "replaced": False,
                            "timed_out": True,
                            "waited_ms": waited_ms,
                        },
                    )
                    return
                time.sleep(WAIT_POLL_SECONDS)

        def _handle_capture(self, query: dict) -> None:
            """GET /capture?session=<name>&lines=<n> — the CURRENTLY VISIBLE rows of
            a session's active pane, for the rack's cheap preview tiles.

            Visible only. Never `-S` scrollback, and that is a CORRECTNESS
            requirement, not an optimisation: full-screen TUI agents (Claude Code,
            OpenCode) draw into the terminal's ALTERNATE SCREEN, whose rows never
            enter scrollback — asking for history returns less, not more. Measured
            on tmux 3.7b: a `top` pane yields 20 live rows from `-p`, while
            `-S -100` adds nothing. herdr's docs describe the same trap, which is
            why it exposes a separate `detection` read source instead of scraping.

            Origin-guarded like every other route (do_GET checks before
            dispatch). "Read-only" is NOT a reason to skip it here: this returns
            whatever is on the user's screen — source, API responses, an echoed
            token — so wildcard CORS on it handed any open tab a live read of
            the terminal. Target is `=<session>:` — the `=` forces an
            exact session match (a bare name prefix-matches some OTHER session once
            the named one dies) and the trailing colon makes it a *pane* target;
            `-t =<session>` alone is a session target and errors here.
            """
            session = (query.get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            lines = capture_lines((query.get("lines") or [None])[0])
            try:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", f"={session}:"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                self._send_json(200, {"session": session, "lines": [], "live": False})
                return
            if result.returncode != 0:
                # Session gone (or no server). Not an error for the caller: the
                # tile just renders as dead, which it must be able to do anyway.
                self._send_json(200, {"session": session, "lines": [], "live": False})
                return
            self._send_json(
                200,
                {"session": session, "lines": tail_visible(result.stdout, lines), "live": True},
            )

        def _handle_focus(self) -> None:
            """POST /focus?session=<name>[&target=world|rack] — ask an already-running
            page to jump to a session, so a native client can land you in the
            surface you already have open instead of reloading one.

            This is a *pass-through to connected clients*: the bridge does not touch
            the machine, it broadcasts a `command` SSE frame and returns how many
            clients heard it. Origin-guarded anyway (it steers the user's UI), and
            `delivered: 0` is the honest answer when no page is listening — the caller
            then falls back to opening a deep link.

            A new SSE *event name*, not new infrastructure: the broker already
            formats-once/enqueue-everywhere. One caveat inherited from it — an
            overflowing client queue drops its OLDEST chunk, so a command can be
            dropped for a client that is already drowning in undelivered snapshots.

            `target` picks WHICH surface: `world` steers an embedding page's
            own view (the original behaviour, and the default so an older
            caller is unchanged), `rack` steers the popped-out agent-rack window.
            It rides the same frame rather than getting a route of its own —
            "which surface" is an argument to focusing, not a different verb.
            """
            if not self._origin_allowed():
                return
            query = parse_qs(urlparse(self.path).query)
            session = (query.get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            target = (query.get("target") or [FOCUS_TARGET_DEFAULT])[0]
            if target not in FOCUS_TARGETS:
                self._send_json(400, {"error": f"target must be one of {sorted(FOCUS_TARGETS)}"})
                return
            listeners = broker.client_count()
            broker.broadcast("command", {"action": "focus_session", "session": session, "target": target})
            click.echo(f"tldrack: focus '{session}' → {target} → {listeners} page client(s)")
            self._send_json(200, {"ok": True, "session": session, "target": target, "delivered": listeners})

        def _handle_presence(self) -> None:
            """POST /presence — a rack surface reporting that it is on screen.

            The bridge cannot observe the browser side for itself: `delivered` from
            /focus counts SSE clients without saying what they are showing, and the
            agent-rack POP-OUT window never talks to this process at all (by
            design). See presence.py.

            Deliberately quiet on the console: this is a heartbeat, and the startup
            banner's rule is that only state TRANSITIONS get echoed. The SSE push is
            gated on an actual change for the same reason — an unchanged keep-alive
            from N tabs must not fan a snapshot out to every listener N times a beat.
            """
            payload = self._read_json_object(require_object=False)
            if payload is None:
                return
            try:
                client_id, record = parse_presence(payload)
            except ValueError as err:
                self._send_json(400, {"error": str(err)})
                return
            if presence.set(client_id, record):
                notify_snapshot()
            self._send_json(200, {"ok": True, "client_id": client_id})

        def _handle_notify(self) -> None:
            """POST /notify {"enabled": bool} — turn phone notifications on or off.

            Origin-guarded like /session-icon because it WRITES TO DISK (the flag
            persists in notify.json so the choice survives a bridge restart).

            Deliberately the only writable field. The topic is a secret that
            grants both read and publish on hosted ntfy, so it is set by hand in
            the config file or the environment — never over an HTTP route any
            allowlisted page could reach. A UI can flip the switch; it cannot
            redirect where your alerts go.
            """
            if not self._origin_allowed():
                return
            payload = self._read_json_object(require_object=False)
            if payload is None:
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
                self._send_json(400, {"error": 'body must be {"enabled": true|false}'})
                return
            state = notifier.set_enabled(payload["enabled"])
            if not state["configured"]:
                # 409 rather than 400: the request was well-formed, the machine
                # just has nowhere to send to. Distinct from "you sent nonsense"
                # so a client can show the right thing — "set a topic first".
                self._send_json(409, {"error": "no ntfy topic configured", **state})
                return
            click.echo(f"tldrack: phone notifications {'enabled' if state['enabled'] else 'disabled'}")
            self._send_json(200, state)

        def _handle_notify_test(self) -> None:
            """POST /notify/test — publish a test notification right now.

            Bypasses grace, presence and cooldown on purpose: the user pressed a
            button and is owed a straight answer about whether the transport
            works. It still spends a budget unit, so the button cannot be used to
            climb over the daily ceiling.

            Answers `sent` rather than 5xx on a transport failure — a phone that
            did not buzz is a normal, expected outcome to report (bad topic, no
            network), not a bridge error.
            """
            if not self._origin_allowed():
                return
            sent = notifier.send_test()
            self._send_json(200, {"ok": True, "sent": sent, **notifier.state()})

        def _handle_notify_send(self) -> None:
            """POST /notify/send {"title", "body", "tags"?, "open"?} — a local tool reports an event
            finished (a benchmark, a long run). Policy and the text limits: owned by
            `NotifyDaemon.send_event` in notify.py — see the note there. `open` names where a tap
            lands (notify.py `EVENT_DOORS`); any other value is a 400.

            Origin-guarded like /notify/test: it spends a budget unit. Answers 200 with
            `sent` + `reason` on every policy outcome, as /notify/test does — a phone that
            did not buzz is an outcome, and the caller must never fail its run over it.
            """
            if not self._origin_allowed():
                return
            payload = self._read_json_object()
            if payload is None:
                return
            title, body, tags, door = payload.get("title"), payload.get("body"), payload.get("tags"), payload.get("open")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(body, str)
                or (tags is not None and not isinstance(tags, str))
                or (door is not None and (not isinstance(door, str) or door not in EVENT_DOORS))
            ):
                self._send_json(400, {"error": 'body must be {"title": str, "body": str, "tags"?: str, "open"?: a known door}'})
                return
            sent, reason = notifier.send_event(title, body, tags, door)
            self._send_json(200, {"ok": True, "sent": sent, "reason": reason, **notifier.state()})

        def _handle_session_icon(self) -> None:
            """POST /session-icon {"session": <name>, "icon": <id> | null} — record or
            clear a session's explicit icon pick (see icons.py for what an id is and
            why membership in the client catalogs is deliberately not checked).
            Origin-guarded like /kill-session: it writes to disk. Both clients pick
            up the change on their next /snapshot poll.
            """
            body = self._session_body()
            if body is None:
                return
            payload, session = body
            icon = payload.get("icon")
            if icon is not None and (not isinstance(icon, str) or not ICON_TOKEN_RE.fullmatch(icon)):
                self._send_json(400, {"error": "icon must be null or a short lowercase token"})
                return
            icon_store.set(session, icon)
            click.echo(f"  icon: {session}: {icon or '(auto)'}")
            notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "icon": icon})

        def _handle_session_star(self) -> None:
            """POST /session-star {"session": <name>, "starred": <bool>} — the operator stars a
            session to come back to, or clears that star (stars.py owns what a star is and what
            happens to it at a rename, a close and a reopen). Origin-guarded like /session-icon:
            it writes to disk. Idempotent: starring a starred session answers 200 and changes
            nothing. 200 {ok, session, starred} · 400 bad body · 404 no such session (a star on a
            name nothing holds would wait for whoever takes that name next) · 503 no store."""
            body = self._session_body()
            if body is None:
                return
            if stars is None:
                self._send_json(503, {"error": "this bridge keeps no stars"})
                return
            payload, session = body
            starred = payload.get("starred")
            if not isinstance(starred, bool):
                self._send_json(400, {"error": "starred must be true or false"})
                return
            # Clearing never needs the session to exist: that is how a stale star is removed.
            if starred and session not in list_tmux_sessions():
                self._send_json(404, {"error": f"no session named '{session}'"})
                return
            if stars.set(session, starred):
                click.echo(f"  star: {session}: {'on' if starred else 'off'}")
                notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "starred": starred})

        def _handle_get_persona(self) -> None:
            """GET /persona?session=<name> — the session's pick plus the catalog the
            pane's chips draw from, so labels have one owner (personas.py)."""
            if personas is None:
                self._send_json(503, {"error": "this bridge has no persona store"})
                return
            session = (self._query().get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            self._send_json(
                200, {"session": session, "persona": personas.get(session), "personas": [persona.card() for persona in PERSONAS]}
            )

        def _topic_since(self, raw: object) -> tuple[bool, float | None]:
            """`since` as a tmux `session_created` (whole epoch seconds), or None when absent."""
            if raw is None or raw == "":
                return True, None
            text = str(raw)
            if not text.isdigit() or len(text) != 10 or text.startswith("0"):
                self._send_json(400, {"error": "since must be whole epoch seconds"})
                return False, None
            return True, float(text)

        def _handle_get_topic_labels(self) -> None:
            """GET /topic-labels?session=<name>[&since=<epoch s>] — the NAME card: the session's auto
            name, the request it was named from, and the live verdict (topic_labels.py owns the rows).
            It serves the operator's prompt text, so a no-cors probe is refused like the ledger's."""
            if not self._expensive_route_guard():
                return
            if topic_labels is None:
                self._send_json(503, {"error": "this bridge has no name-label log"})
                return
            query = self._query()
            session = (query.get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            ok, since = self._topic_since((query.get("since") or [None])[0])
            if not ok:
                return
            body = topic_labels.view(session, since)
            body["shown"] = auto_names.get_all().get(session) if auto_names is not None else None
            self._send_json(200, body)

        def _handle_post_topic_labels(self) -> None:
            """POST /topic-labels {"session", "verdict": "fits"|"wrong", "better"?, "since"?} — the NAME
            card's answer on the session's auto name. A `better` name on a vc-N row becomes its label
            at once, as a final one (`SessionNameStore.adopt`); the tmux name stays, so the live pane
            and Laya's subject never move."""
            if not self._origin_allowed():
                return
            if topic_labels is None:
                self._send_json(503, {"error": "this bridge has no name-label log"})
                return
            payload = self._read_json_object()
            if payload is None:
                return
            if set(payload) - {"session", "verdict", "better", "since"}:
                self._send_json(400, {"error": "only session, verdict, better and since may be sent"})
                return
            session, verdict, better = payload.get("session"), payload.get("verdict"), payload.get("better")
            if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            if verdict not in TOPIC_VERDICTS:
                self._send_json(400, {"error": "verdict must be fits or wrong"})
                return
            if better is not None:
                better = better.strip() if isinstance(better, str) else better
                if verdict != "wrong" or not isinstance(better, str) or not better or not valid_title(better):
                    self._send_json(400, {"error": "better is a short name, sent only with wrong"})
                    return
            ok, since = self._topic_since(payload.get("since"))
            if not ok:
                return
            named = topic_labels.view(session, since)["named"]
            if named is None:
                self._send_json(409, {"error": "this session has no auto name to label"})
                return
            relabel = bool(better) and named["session"] == session and auto_names is not None
            if relabel and better.casefold() in {label.casefold() for label in topic_taken(session)}:
                self._send_json(409, {"error": f"'{better}' already names another session"})
                return
            relabeled = relabel and auto_names.adopt(session, better)
            topic_labels.label(named["session"], named["name"], verdict, via="card", better=better or None)
            if relabeled:
                click.echo(f"  name: {session} → {better} (operator)")
                notify_snapshot()
            self._send_json(
                200,
                {
                    **topic_labels.view(session, since),
                    "relabeled": relabeled,
                    "shown": auto_names.get_all().get(session) if auto_names is not None else None,
                },
            )

        def _handle_set_persona(self) -> None:
            """POST /persona {"session": <name>, "persona": <id> | null} — pick or clear
            a session's work persona. It reaches the agent on its next prompt, as
            the UserPromptSubmit hook's answer (personas.py owns why)."""
            if not self._origin_allowed():
                return
            if personas is None:
                self._send_json(503, {"error": "this bridge has no persona store"})
                return
            payload = self._read_json_object()
            if payload is None:
                return
            session = payload.get("session")
            persona = payload.get("persona")
            if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            if persona is not None and persona not in PERSONAS_BY_ID:
                self._send_json(400, {"error": f"persona must be null or one of: {', '.join(PERSONAS_BY_ID)}"})
                return
            personas.set(session, persona)
            click.echo(f"  persona: {session}: {persona or '(none)'}")
            notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "persona": persona})

        def _handle_get_missions(self) -> None:
            """GET /missions[?mission=<id, id prefix or title>] — every open mission, or
            one with all its notes. 410 for an unknown mission: 404 keeps its one
            meaning, "this bridge predates the route"."""
            if missions is None:
                self._send_json(503, {"error": "this bridge has no mission store"})
                return
            if (self._query().get("standing") or [None])[0] == "1":
                self._send_json(200, missions.standing_state())
                return
            ref = (self._query().get("mission") or [None])[0]
            if ref is None:
                self._send_json(200, missions.state())
                return
            try:
                self._send_json(200, missions.detail(ref))
            except MissionError as err:
                self._send_json(410, {"error": str(err)})

        def _note_decision(self, session: str, event: str, payload: dict) -> None:
            if event == "PreToolUse":
                asked = decisions.asked(session, payload.get("tool_input"))
                if decision_judge is not None:
                    decision_judge.asked(session, asked)
            elif event == "PostToolUse":
                rows = decisions.answered(
                    session,
                    payload.get("tool_input"),
                    payload.get("tool_response"),
                    mission=missions.mission_of(session) if missions is not None else None,
                    cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
                )
                if decision_judge is not None and rows:
                    decision_judge.answered(session, rows)

        def _handle_get_decisions(self) -> None:
            """GET /decisions — the ledger's counts, the shadow judge's agreement and the newest
            answers (decisions.py owns every number), plus `capture_blocked`: the Claude accounts whose
            hooks would drop every answer. Absent when this bridge cannot judge (no profiles, no port)."""
            if decisions is None:
                self._send_json(503, {"error": "this bridge has no decision ledger"})
                return
            summary = decisions.summary()
            port = bridge_info.get("status_port")
            if profiles is not None and port:
                summary["capture_blocked"] = capture_blocked(profiles.summary(port, HOOK_EVENTS_BY_KIND))
            self._send_json(200, summary)

        def _handle_rack_metrics(self) -> None:
            if not self._expensive_route_guard():
                return
            self._send_json(*metrics_response(rack_metrics, "summary", query=self._query()))

        def _write_rack_metrics(self, route: str) -> None:
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            self._send_json(
                *metrics_response(
                    rack_metrics, route, body=body, record_of=records.get if records is not None else lambda _: None
                )
            )

        def _handle_rack_metric_event(self) -> None:
            self._write_rack_metrics("event")

        def _handle_rack_metric_settings(self) -> None:
            self._write_rack_metrics("settings")

        def _handle_decisions_import(self) -> None:
            """POST /decisions/import {} — the DECISIONS row's IMPORT: append the operator calls written
            in the project's documents (decisions.py `DOC_DECISION_RES`), once each. Takes no path: the
            root is the bridge's start directory and the globs are the operator's (settings.py
            `decision_docs`), so a page cannot point the scan anywhere."""
            if not self._origin_allowed():
                return
            if self._read_json_object() is None:
                return
            if decisions is None or not decisions.enabled:
                self._send_json(503, {"error": "this bridge has no decision ledger"})
                return
            docs = doc_files(Path(start_dir or os.getcwd()), settings.decision_docs)
            added = decisions.import_docs(docs)
            self._send_json(200, {"ok": True, "added": added, "message": f"{added} new from {len(docs)} docs"})

        def _handle_ledger_history(self) -> None:
            # Local/tailnet reads inherit the host and origin guards; also reject no-cors
            # browser probes before doing a bounded SQLite search (reader.py owns limits).
            if not self._expensive_route_guard():
                return
            route = urlsplit(self.path).path.rsplit("/", 1)[-1]
            status, body = read_history(ledger_dir(), route, self._query())
            self._send_json(status, body)

        def _handle_rack_search(self) -> None:
            """GET /ledger/rack-search?q=&s=… — the rack's search box, past the titles: which of these
            live sessions SAID every term. Owner: ledger/rack_search.py — see the note there. The same
            guards as the history reads; it reads conversation text too."""
            if not self._expensive_route_guard():
                return
            status, body = rack_search(ledger_dir(), self._query(), bind_rack_rows, rack_tails, account_roots)
            self._send_json(status, body)

        def _handle_ledger_status(self) -> None:
            """GET /ledger/status — the session ledger's counts (`ledger/ingest.py` `ledger_status`,
            counts only, never text) plus the timer's last child run. Cached `LEDGER_STATUS_TTL`: the
            OPS row polls, and a count over a few hundred thousand rows is not free."""
            now = time.monotonic()
            cached = ledger_status_cache.get("value")
            if cached is None or now - ledger_status_cache.get("at", 0.0) > LEDGER_STATUS_TTL:
                try:
                    cached = ledger_status(ledger_dir(), roots=account_roots())
                except (OSError, sqlite3.Error) as exc:
                    cached = {"enabled": True, "ingested": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
                ledger_status_cache.update(value=cached, at=now)
            body = dict(cached)
            if ledger is not None:
                body["scheduler"] = {
                    "last": ledger.last,
                    "child_running": ledger.child_running,
                    "interval_s": LEDGER_INTERVAL_SECONDS,
                }
            self._send_json(200, body)

        def _handle_ledger_ingest(self) -> None:
            """POST /ledger/ingest {} — AGENT RACK › SESSION LEDGER's INGEST NOW: wake the timer. 202; the
            child takes the ingest lock, so a press during a run is a no-op, never a second run."""
            if not self._origin_allowed():
                return
            if self._read_json_object() is None:
                return
            if ledger is None or not ledger.request():
                self._send_json(503, {"error": "this bridge has no session ledger"})
                return
            ledger_status_cache.clear()
            self._send_json(202, {"ok": True, "queued": True})

        def _handle_get_costs(self) -> None:
            """GET /costs — agent spend today and this week, per rack session and mission, and the
            fixer total against its daily cap (session_costs.py owns every number)."""
            if costs is None:
                self._send_json(503, {"error": "this bridge has no cost store"})
                return
            mission_of = missions.mission_of if missions is not None else None
            self._send_json(200, costs.summary(mission_of))

        def _handle_error_fix(self) -> None:
            """POST /errors/fix {the OPS › ERRORS row} — write its brief and start a fixer agent in a new
            worktree (error_fix.py owns the brief, the branch and why the row is data). 429 past the
            daily fixer budget (session_costs.py); 503 without the worktree plugin."""
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            req, error = error_fix.parse_fix_request(body)
            if req is None:
                self._send_json(400, {"error": error})
                return
            if costs is not None and costs.fixer_spend_today() >= fix_cap_usd():
                self._send_json(
                    429,
                    {
                        "error": "fixer budget spent today — raise TLDRACK_FIX_CAP_USD or fix this one by hand",
                        "code": "fixer_budget",
                    },
                )
                return
            worktree = capability("worktree")
            if worktree is None:
                self._send_json(503, {"error": "the worktree plugin is not mounted"})
                return
            owner = (
                missions.owning_mission([req["route"], req["message_shape"], *req["frames"]]) if missions is not None else None
            )
            try:
                brief = error_fix.write_brief(req, error_fix.mission_note(owner))
            except OSError as err:
                self._send_json(500, {"error": f"could not write the brief ({err})"})
                return
            status, result = worktree.handle_post(
                "/create",
                {},
                {
                    "branch": error_fix.branch_for(req["fingerprint"]),
                    "prompt": error_fix.build_prompt(brief),
                    "profile": "auto",
                },
            )[:2]
            if status >= 400:
                self._send_json(status, result if isinstance(result, dict) else {"error": "worktree create failed"})
                return
            if owner is not None:
                try:
                    missions.add_note(
                        owner["id"],
                        "agent-ops",
                        f"Fixer {result.get('session')} started on production error {req['fingerprint']}.",
                    )
                except MissionError:
                    pass
            self._send_json(
                202,
                {
                    "ok": True,
                    "session": result.get("session"),
                    "branch": result.get("branch"),
                    "brief": str(brief),
                    "mission": owner["title"] if owner else None,
                },
            )

        def _handle_post_missions(self) -> None:
            """POST /missions {"op": create|assign|role|note|close, …} — one write
            (missions.py owns each). An Origin means a rack page, so its note is the
            operator's; a CLI has none and names its session. A label, not a credential."""
            if not self._origin_allowed():
                return
            if missions is None:
                self._send_json(503, {"error": "this bridge has no mission store"})
                return
            payload = self._read_json_object()
            if payload is None:
                return
            try:
                changed = missions.apply(payload.get("op"), payload, operator=self.headers.get("Origin") is not None)
            except MissionNotFound as err:
                self._send_json(410, {"error": str(err)})
                return
            except MissionError as err:
                self._send_json(400, {"error": str(err)})
                return
            self._send_json(200, {"ok": True, "mission": changed})

        def _handle_get_next_step(self) -> None:
            """GET /next-step[?days=N] — the suggester's mode and the log's verdict over the last N days
            (default 7): suggested, taken, dismissed, ignored, the take rate (next_step.py `summarize`).
            Counts only, never transcript text."""
            if next_steps is None:
                self._send_json(503, {"error": "this bridge has no next-step suggester"})
                return
            raw_days = (self._query().get("days") or ["7"])[0]
            try:
                days = float(raw_days)
            except ValueError:
                self._send_json(400, {"error": "days must be a number"})
                return
            since = time.time() - days * 86400 if 0 < days <= 365 else None
            self._send_json(200, next_steps.status(since))

        def _handle_next_step_event(self) -> None:
            """POST /next-step/event {session, event: shown|dismissed|press, hash?, action?, via?} —
            what the rack did about a suggestion (next_step.py `parse_event`). A press is REPORTED,
            never performed: the rack fired its own control already."""
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            if next_steps is None:
                self._send_json(503, {"error": "this bridge has no next-step suggester"})
                return
            event, error = next_step_mod.parse_event(body)
            if event is None:
                self._send_json(400, {"error": error})
                return
            session = event["session"]
            if event["event"] == "shown":
                recorded: object = next_steps.note_shown(session, event["hash"])
            elif event["event"] == "dismissed":
                recorded = next_steps.note_dismiss(session, event["hash"])
            else:
                recorded = next_steps.note_press(session, event["action"], event["via"], event["hash"])
            self._send_json(200, {"ok": True, "recorded": recorded})

        def _handle_next_step_settings(self) -> None:
            """POST /next-step/settings {"mode": off|shadow|live} — AGENT RACK › NEXT STEP's switch."""
            if not self._origin_allowed():
                return
            body = self._read_json_object()
            if body is None:
                return
            if next_steps is None:
                self._send_json(503, {"error": "this bridge has no next-step suggester"})
                return
            try:
                changed = next_steps.set_mode(body.get("mode"))
            except ValueError as err:
                self._send_json(400, {"error": str(err)})
                return
            if changed:
                click.echo(f"  next-step: mode → {next_steps.mode}")
            self._send_json(200, {"ok": True, "mode": next_steps.mode, "changed": changed})

        def _handle_open_work_dismiss(self) -> None:
            """POST /open-work/dismiss {"session": <name>} — ✗ NOT OPEN: clear Laya's ⚠️ on a row until
            its next turn end, and record the operator's answer to it as a label (open_work.py § The
            operator's grade). The row is cleared and repainted first; `graded` says whether a label
            now stands. A caller the plugin refuses for a label still clears the mark, ungraded."""
            body = self._session_body()
            if body is None:
                return
            _, session = body
            verdict = store.dismiss_open_work(session)
            grade = None
            if verdict is not None:
                verdict_left_row(session, "dismissed")
                # ✗ says no work is open, so the loop it belonged to is closed (attention.py § 3).
                if loops is not None:
                    loops.forget(session)
                signals.put("warning_dismissed", session)
                notify_snapshot()
                grade = grade_verdict(laya_grader(self.headers), verdict, is_open_now=False)
                click.echo(f"  open-work: {session}: dismissed (grade: {grade})")
            self._send_json(
                200,
                {"ok": True, "session": session, "cleared": verdict is not None, "graded": grade in (GRADE_RECORDED, GRADE_KEPT)},
            )

        def _handle_open_work_confirm(self) -> None:
            """POST /open-work/confirm {"session": <name>} — ✓ STILL OPEN: the operator says the ⚠️ is
            right. The ⚠️ and its loop stay as they are (the work IS open); the answer is recorded as a
            label (a text that already has one keeps it), an `accepted` reaction goes to the ledger,
            and the row carries `open_work_graded` from then on, so the menu stops offering a second
            ✓. `graded` false: no label stands (no ⚠️ on the row, or the grade could not be written),
            and the row is unchanged."""
            body = self._session_body()
            if body is None:
                return
            _, session = body
            verdict = store.open_work_verdict(session)
            grade = grade_verdict(laya_grader(self.headers), verdict, is_open_now=True)
            graded = grade in (GRADE_RECORDED, GRADE_KEPT) and store.mark_open_work_graded(session, verdict["decision_id"])
            if graded:
                open_work_reactions.accepted(session)
                signals.put("warning_confirmed", session)
                click.echo(f"  open-work: {session}: still open (grade: {grade})")
                notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "graded": graded})

        def _handle_open_work_snooze(self) -> None:
            """POST /open-work/snooze {"session": <name>} — ⏸ "seen, not now": park the row's open-work
            loop, so this ⚠️ and every repeat of it draw nothing until the operator next prompts that
            session (attention.py § 3). Not a grade: Laya's ledger hears nothing. Persisted."""
            body = self._session_body()
            if body is None:
                return
            _, session = body
            if loops is None:
                self._send_json(503, {"error": "this bridge keeps no open-work loops"})
                return
            snoozed = store.get(session).get("open_work") is True and loops.park(session)
            if snoozed:
                signals.put("warning_snoozed", session)
                click.echo(f"  open-work: {session}: seen, not now")
                notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "snoozed": snoozed})

        def _handle_open_work_read(self) -> None:
            """POST /open-work/read {"session": <name>} — the operator opened this session's pane, so its
            answer to read is read (open_work.py § An answer to read). The ✅ stays; only
            `answer_to_read` drops, on the row and in the kept copy. Not a grade: Laya hears nothing.
            `cleared` false: the row drew no 📖 (its verdict is still stamped read: `mark_answer_read`)."""
            body = self._session_body()
            if body is None:
                return
            _, session = body
            stamped, cleared = store.mark_answer_read(session)
            if stamped and verdicts is not None:
                verdicts.mark_read(session)
            if cleared:
                notify_snapshot()
            self._send_json(200, {"ok": True, "session": session, "cleared": cleared})

        def _note_permission(
            self, session: str, event: str, payload: dict, profile_hint: object, transcript_path: object
        ) -> None:
            """Feed the shadow permission ledger (permission_ledger.py). It decides
            nothing and must never cost the hook its answer, so it cannot raise."""
            try:
                events = transcripts.tool_events(session)
                if event != "PermissionRequest":
                    permissions.observe(session, event, payload, events)
                    return
                profile_id = profile_hint if isinstance(profile_hint, str) else None
                kind = (profiles.kind_of(profile_id) if profiles is not None and profile_id else None) or (
                    "claude" if normalize_transcript_path(transcript_path) else "codex"
                )
                permissions.request(session, payload, profile=profile_id, kind=kind, tool_events=events)
            except Exception as err:  # noqa: BLE001 — a ledger bug must not fail a hook
                click.echo(f"tldrack: permission ledger failed ({err})", err=True)

        def _permission_since(self) -> float | None:
            raw = (self._query().get("since") or [None])[0]
            try:
                return float(raw) if raw is not None else None
            except ValueError:
                return None

        # ── the LEAD launch and the decision inbox (lead/service.py, inbox.py) ────────────────

        def _unavailable(self, part: object, what: str) -> bool:
            if part is None:
                self._send_json(
                    503, {"ok": False, "error": f"no {what} on this bridge (it was built without one, or has no launch profiles)"}
                )
                return True
            return False

        def _handle_lead_state(self) -> None:
            if not self._unavailable(lead_service, "LEAD store"):
                self._send_json(200, lead_service.state_view())

        def _handle_inbox(self) -> None:
            if self._unavailable(decision_inbox, "decision inbox"):
                return
            run = (parse_qs(urlparse(self.path).query).get("run") or [None])[0]
            self._send_json(200, {"decisions": decision_inbox.open_rows(run)})

        def _handle_decision_secret(self) -> None:
            why = secret_refusal(self.headers, self._allows_origin)
            if why is not None:
                self._send_json(403, {"ok": False, "error": why})
                return
            self._send_json(200, {"secret": secret.value})

        def _lead_caller(self, payload: dict) -> str | None:
            """The CLI's own session: its pane (X-VC-Pane) first, the name it sent second."""
            pane = self.headers.get(PANE_HEADER)
            if pane:
                session = pane_sessions.session_of(pane.strip())
                if session is not None:
                    return session
            named = payload.get("session")
            return named if isinstance(named, str) else None

        def _handle_lead_verb(self) -> None:
            """POST /lead/<verb> — the LEAD's, a worker's, or (launch) the operator's verb."""
            if not self._origin_allowed() or self._unavailable(lead_service, "LEAD store"):
                return
            payload = self._read_json_object(LEAD_BODY_MAX)
            if payload is None:
                return
            verb = urlsplit(self.path).path.rsplit("/", 1)[-1]
            self._send_json(*lead_service.handle(verb, payload, self._lead_caller(payload)))

        def _handle_inbox_verb(self) -> None:
            """POST /inbox/answer|defer|dismiss — the operator's, from a browser page holding the secret."""
            if not self._origin_allowed() or self._unavailable(decision_inbox, "decision inbox"):
                return
            why = answer_refusal(self.headers, secret, self._allows_origin)
            if why is not None:
                self._send_json(403, {"ok": False, "error": why})
                return
            payload = self._read_json_object(16 * 1024)
            if payload is None:
                return
            verb = urlsplit(self.path).path.rsplit("/", 1)[-1]
            via = browser_origin(self.headers, self._allows_origin)
            inbox = decision_inbox
            try:
                if verb == "answer":
                    result = inbox.answer(payload.get("key"), payload.get("kind"), payload.get("answer"), via=via)
                elif verb == "defer":
                    result = inbox.defer(payload.get("key"), payload.get("until"), via=via)
                else:
                    result = inbox.dismiss(payload.get("key"), payload.get("reason"), via=via)
            except InboxError as err:
                self._send_json(err.status, {"ok": False, "error": str(err)})
                return
            notify_snapshot()
            self._send_json(200, {"ok": True, "reopened": result.get("reopened") or [], **result})

        def _handle_permission_ledger(self) -> None:
            """GET /permission/ledger?since=<epoch>&limit=N — request rows joined
            with outcomes, newest first. Summaries are redacted at write time."""
            if permissions is None:
                self._send_json(503, {"error": "this bridge keeps no permission ledger"})
                return
            raw_limit = (self._query().get("limit") or ["200"])[0]
            limit = int(raw_limit) if raw_limit.isdigit() and 0 < int(raw_limit) <= 2000 else 200
            self._send_json(200, {"rows": permissions.rows(self._permission_since(), limit)})

        def _handle_permission_stats(self) -> None:
            """GET /permission/stats?since=<epoch> — counts by pattern, the human
            approval rate and the median wait: what calibrates a future policy."""
            if permissions is None:
                self._send_json(503, {"error": "this bridge keeps no permission ledger"})
                return
            self._send_json(200, permissions.stats(self._permission_since()))

        def _handle_install_hooks(self) -> None:
            """POST /profiles/install-hooks `{"profile": <id>}` — the OPS row's
            INSTALL: the same merge as `tldrack profiles --install-hooks`
            (backed up, atomic, re-read). Origin-guarded: it writes an account's
            settings file. Only a configured profile id is accepted — never a path."""
            if not self._origin_allowed():
                return
            if profiles is None:
                self._send_json(503, {"ok": False, "error": "this bridge has no launch profiles"})
                return
            payload = self._read_json_object()
            if payload is None:
                return
            profile_id = payload.get("profile")
            profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
            if profile is None or profile.kind not in HOOK_EVENTS_BY_KIND or profile.config_dir is None:
                self._send_json(400, {"ok": False, "error": "unknown profile, or one with no hooks file"})
                return
            port = bridge_info.get("status_port")
            if not port:  # a test-built handler: the relay would name no port
                self._send_json(503, {"ok": False, "error": "this bridge does not know its status port"})
                return
            try:
                path, changed, backup, notes = install_profile_hooks(profile, port)
            except (ValueError, OSError) as error:
                self._send_json(500, {"ok": False, "error": f"could not install hooks for {profile.id}: {error}"})
                return
            click.echo(f"  hooks: {profile.id}: {'installed' if changed else 'already current'} → {path}")
            message = f"{profile.label}: hooks {'installed' if changed else 'already current'}"
            if changed and profile.kind == "codex":
                message += " — trust them once in Codex (/hooks)"
            elif changed and profile.kind == antigravity_hooks.KIND:
                message += " — new agy sessions pick them up (check with /hooks)"
            elif changed:
                message += " — new sessions pick them up"
            self._send_json(
                200,
                {"ok": True, "changed": changed, "message": message, "backup": str(backup) if backup else None, "notes": notes},
            )
            notify_snapshot()

        def _handle_hook(self) -> None:
            """POST /hook?session=<name>&event=<HookEvent> — body is the hook's raw
            stdin JSON. State comes from hook_state(event, payload); the activity
            (which architecture zone the agent is in) is classified from tool_name /
            tool_input. A malformed or oversized body degrades to a state-only
            update — the glow must never be lost to a classification problem.
            """
            query = parse_qs(urlparse(self.path).query)
            event = (query.get("event") or [""])[0]
            pane = self.headers.get(PANE_HEADER)
            # The curl relay names a session and sends this answer to /dev/null: missions.py
            # needs to know, or it marks a briefing nobody received as heard.
            answerable = "session" not in query
            if answerable and pane is not None:
                # The `http` relay (hook_http_entry): it names a pane, not a session.
                session = pane_sessions.session_of(pane.strip())
                if session is None:  # outside tmux, or a pane this server does not hold
                    length = int(self.headers.get("Content-Length") or 0)
                    if 0 < length <= MAX_HOOK_BODY_BYTES:
                        self.rfile.read(length)  # drain, so a kept-alive connection stays in sync
                    self._send_json(200, {"ok": True, "ignored": "no tmux session"})
                    return
            else:
                session = (query.get("session") or [default_session])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                self._send_json(400, {"error": "invalid session name"})
                return
            # A CLI whose payloads need translating names itself (hook_command's `agent`).
            agent = (query.get("agent") or [None])[0]
            antigravity = agent == antigravity_hooks.AGENT_PARAM
            if agent is not None and not antigravity:
                self._send_json(400, {"error": f"agent must be {antigravity_hooks.AGENT_PARAM}"})
                return
            if antigravity and event not in antigravity_hooks.HOOK_STATES:
                self._send_json(400, {"error": "event must be one of: " + ", ".join(antigravity_hooks.HOOK_STATES)})
                return
            if not antigravity and event not in HOOK_STATES and event not in CODEX_HOOK_STATES:
                known = list(dict.fromkeys([*HOOK_STATES, *CODEX_HOOK_STATES]))
                self._send_json(400, {"error": f"event must be one of: {', '.join(known)}"})
                return
            payload: dict = {}
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_HOOK_BODY_BYTES:
                try:
                    parsed = json.loads(self.rfile.read(length))
                    if isinstance(parsed, dict):
                        payload = parsed
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass  # state-only fallback
            if antigravity:
                # camelCase → the fields below; the event names stay agy's own.
                payload = antigravity_hooks.normalize_payload(payload)
                state = antigravity_hooks.hook_state(event, payload)
                activity = antigravity_hooks.activity_update(event, payload)
            else:
                state = hook_state(event, payload)
                activity = activity_update_for_event(event, payload)
            # `transcript_path` rides in every hook's raw stdin JSON — read the
            # running model + cumulative token usage from it (best-effort; a
            # missing/unreadable transcript keeps the last-known values).
            transcript_path = payload.get("transcript_path")
            if event == "PermissionRequest" and profiles is not None and not is_subagent_hook(payload):
                codex_profile = profiles.profile_for_path(transcript_path, kind="codex", first_on_tie=True)
                if codex_profile and codex_uses_auto_review(payload, profiles.describe(codex_profile)["config_dir"]):
                    # PermissionRequest precedes the reviewer, not necessarily a
                    # human prompt. This changes only display state, no decision.
                    state = "busy"
            model, usage = transcripts.update(session, transcript_path)
            # A turn that ends with work still running is `background`, not
            # `free` — decided HERE, before any client sees a free frame.
            # state_arbiter.py's refine_turn_end owns why.
            state = refine_turn_end(state, event, lambda: background_now(session))
            # A Codex hook names its model in the payload and keeps its
            # transcript under CODEX_HOME — outside the allowlist, correctly,
            # since the tracker parses Claude's JSONL. Only for a path the
            # allowlist refused: a Claude session with no reply yet is the
            # statusline tier's job (statusline_models.py), not this one.
            if model is None and normalize_transcript_path(transcript_path) is None:
                payload_model = payload.get("model")
                if isinstance(payload_model, str) and MODEL_TOKEN_RE.fullmatch(payload_model):
                    model = payload_model
            if missions is not None and event == "PreToolUse":
                missions.note_tool(session, payload)
            # Where the agent works when it is not where it stands (work_trees.py). Main thread only: a
            # worktree-isolated subagent reports its own checkout. The hook's closing snapshot carries it.
            if work_trees is not None and event == "PreToolUse" and not is_subagent_hook(payload):
                found = work_tree_evidence(payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd"))
                work_trees.observe(session, found)
            # A lead's edit is refused, a worker's AskUserQuestion goes to its lead (lead/service.py).
            # A subagent's call too: a lead delegating an edit to a subagent is still the lead editing.
            lead_answer = (
                lead_service.hook_tool(session, payload)
                if lead_service is not None and answerable and event == "PreToolUse"
                else None
            )
            # The operator's answers to an agent's questions (decisions.py). Main thread only: a
            # subagent cannot ask the operator anything.
            if decisions is not None and payload.get("tool_name") == ANSWER_HOOK[1] and not is_subagent_hook(payload):
                self._note_decision(session, event, payload)
            # Any hook at all is progress, a subagent's included (stuck.py rule 2).
            stuck.note_hook(session)
            if state == "busy" and is_subagent_hook(payload):
                self._record_subagent_work(session, event, payload, activity, model, usage, answer=lead_answer)
                return
            if event == "UserPromptSubmit":
                note_prompt(session, payload.get("prompt"))
                # Prompting answers a suggested `select`, and overrides any other (next_step.py).
                if next_steps is not None:
                    next_steps.note_prompt(session)
                stuck.note_turn(session)
                # A human prompt closes the open-work loop; a self-woken turn keeps it (attention.py § 2).
                if loops is not None:
                    loops.note_prompt(session, payload.get("prompt"))
            elif event == "SessionStart" and fresh_for_event(event, payload) is True:
                if loops is not None:
                    loops.forget(session)  # a new conversation (startup, /clear) has no loop yet
                if work_trees is not None:
                    work_trees.forget(session)  # nor anywhere it worked (work_trees.py)
            elif event == "SessionEnd" and payload.get("reason") in USER_ENDED_REASONS and work_trees is not None:
                work_trees.forget(session)
            # The work persona and the mission ride back as this hook's answer
            # (personas.py, missions.py).
            hook_contexts: list[str | None] = []
            if event == "UserPromptSubmit":
                if personas is not None:
                    hook_contexts.append(personas.context_for_prompt(session))
                if missions is not None:
                    hook_contexts.append(missions.context_for_prompt(session, answerable=answerable))
                if lead_service is not None and answerable:  # the role survives /compact (lead/brief.py)
                    hook_contexts.append(lead_service.hook_prompt(session))
            elif event == "PreToolUse" and missions is not None:
                hook_contexts.append(missions.context_for_tool(session, answerable=answerable))
            hook_output = prompt_hook_output("\n\n".join(filter(None, hook_contexts)) or None, event)
            # A conversation that started or ended in the pane is no longer the
            # one the limit cut off (TranscriptTracker.clear_stop_failure).
            if event in ("SessionStart", "SessionEnd"):
                transcripts.clear_stop_failure(session)
            # Which launch profile owns that transcript — the hint
            # merge_snapshot prefers over the spawn stamp. KEEP when unknown,
            # cleared with the cwd on SessionEnd.
            profile_hint: object = KEEP
            if event == "SessionEnd":
                profile_hint = None
            elif profiles is not None:
                profile_hint = profiles.profile_for_path(transcript_path) or KEEP
            # Claude's cwd follows EnterWorktree / `/cd`. Codex local-tool hooks
            # instead report the execution directory, while its turn hooks report
            # the launch directory. Letting Stop overwrite the last tool's cwd made
            # a Codex session's worktree island disappear when it finished answering.
            # Keep that location across turns; a tool or SessionStart can move it,
            # and SessionEnd hands it back to tmux (snapshot.apply_hook_cwds).
            cwd: object = KEEP
            if event == "SessionEnd":
                cwd = None
            elif isinstance(payload.get("cwd"), str) and payload["cwd"]:
                codex_turn = (
                    event in ("Stop", "UserPromptSubmit", "Interrupt")
                    and profiles is not None
                    and profiles.profile_for_path(transcript_path, kind="codex") is not None
                )
                if not (codex_turn and store.get(session).get("cwd")):
                    cwd = payload["cwd"]
            # A new turn (or a new/ended session) voids Laya's last verdict.
            clears_open_work = state == "busy" or event in ("SessionStart", "SessionEnd")
            previous = store.set(
                session,
                state,
                None,
                activity=activity,
                model=model,
                usage=usage,
                cwd=cwd,
                profile=profile_hint,
                fold_tasks=lambda tasks: fold_task_event(event, payload, tasks),
                open_work=None if clears_open_work else KEEP,
                fresh=fresh_for_event(event, payload),
            )
            if permissions is not None:
                self._note_permission(session, event, payload, profile_hint, transcript_path)
            # Each reaction AFTER the store's clear: OpenWorkReactions.landed owns why.
            if clears_open_work:
                verdict_left_row(session, "expired")
            if event == "Stop" and state in ("free", "background"):
                signals.put("turn_end", session)  # every turn end a "done" ping would have fired on
                token = store.begin_open_work(session)
                verdict_left_row(session, "expired")
                judge_turn_end(session, token, transcript_path, payload.get("session_id"))
            # A main-thread turn the API answered: its account is in good standing again
            # (account_standing.py § WHAT CLEARS IT). A subagent's Stop is not the session's turn.
            if event == "Stop" and not is_subagent_hook(payload) and isinstance(profile_hint, str):
                account_standing.mark_ok(profile_hint)
            # Remember it across restarts. No-ops on a None model and on an
            # unchanged reading, so a busy agent's PreToolUse storm doesn't
            # rewrite the file on every tool call.
            model_store.set(session, model, usage)
            if records is not None:
                # Which conversation this session runs, for a restore after a
                # reboot. Cleared only when the USER ended it (`/exit`, logout)
                # — a SessionEnd from the reboot's own SIGHUP must not erase the
                # very thing a restore needs.
                if event == "SessionEnd" and payload.get("reason") in USER_ENDED_REASONS:
                    records.clear_agent_session(session)
                elif event != "SessionEnd":
                    records.note_agent_session(session, payload.get("session_id"))
                    # The mode Claude is in NOW, so a restore never replays `auto` over a
                    # operator's switch out of it (`restore.restore_mode`).
                    records.note_live_mode(session, payload.get("permission_mode"))
            record_transition(session, event, previous, state, activity)
            zone = activity.get("zone") if isinstance(activity, dict) else None
            previous_zone = (previous.get("activity") or {}).get("zone")
            if previous.get("state") != state or (activity is not KEEP_ACTIVITY and zone != previous_zone):
                where = f"  [{zone}]" if zone else ""
                click.echo(f"  status: {session}: {previous.get('state') or '(new)'} → {state}{where}")
            notify_snapshot()
            # Documented hook-output fields ONLY when answering: Claude validates the
            # body against its schema, and a failure drops the whole answer.
            self._send_json(200, lead_answer or hook_output or {"ok": True})

        def _record_subagent_work(
            self,
            session: str,
            event: str,
            payload: dict,
            activity: object,
            model: object,
            usage: object,
            answer: dict | None = None,
        ) -> None:
            """The `/hook` tail for a subagent's busy hook. `is_subagent_hook`
            owns why it is not the main thread's report; `note_subagent_work`
            owns what it may and may not move."""
            noted = store.note_subagent_work(session, activity, model=model, usage=usage)
            model_store.set(session, model, usage)
            if records is not None:
                records.note_agent_session(session, payload.get("session_id"))
            if noted is not None:
                previous, state = noted
                kept = activity if state == "busy" else KEEP_ACTIVITY
                record_transition(session, event, previous, state, kept)
                if previous.get("state") != state:
                    click.echo(f"  status: {session}: {previous.get('state') or '(new)'} → {state}  [subagent]")
                notify_snapshot()
            self._send_json(200, answer or {"ok": True})

        def _handle_usage(self) -> None:
            """POST /usage — body is the raw statusline stdin JSON (see
            statusline.py). Host- and Origin-guarded at the top of do_POST like
            every other route; the "data-report routes are exempt" carve-out
            this used to claim was removed on 2026-08-01.
            Malformed rate_limits → 400 (never store garbage); a payload
            without them (a non-subscriber, or any session before its first API
            response) is a valid no-op FOR THE WINDOWS. No console echo —
            statusline refreshes are frequent.

            The payload also carries `model.id`, which is read FIRST — before
            the rate-limit parse and both of its early exits. Two reasons, and
            they are the whole point of statusline_models.py: `rate_limits` is
            absent for exactly the sessions whose model is not yet in any
            transcript, and a shape change in `rate_limits` must not cost the
            model its reading.
            """
            payload = self._read_json_object(MAX_USAGE_BODY_BYTES)
            if payload is None:
                return
            # The conversation's running cost (session_costs.py) — read before the rate-limit exits
            # for the same reason as the model below, and never a reason to fail the post.
            cost = parse_statusline_cost(payload) if costs is not None else None
            if cost is not None:
                tmux_name = resolve_tmux_session(payload.get("transcript_path"), transcripts.paths(), normalize_transcript_path)
                costs.record(cost[0], cost[1], tmux_name)
            path = normalize_transcript_path(payload.get("transcript_path"))
            changed = statusline_models.set(path, parse_statusline_model(payload))
            reported, left = parse_statusline_context_left(payload)
            if reported:
                # None = "unknown right now" (pre-first-call, post-/compact):
                # drop the old reading rather than show a stale percentage.
                changed |= statusline_contexts.discard(path) if left is None else statusline_contexts.set(path, left)
            if changed:
                # Only on a CHANGED reading. Unguarded, one open Claude pane
                # would fan a full snapshot to every SSE client every second.
                notify_snapshot()
            try:
                windows = parse_usage_limits(payload)
            except ValueError as err:
                self._send_json(400, {"error": str(err)})
                return
            if not windows:
                self._send_json(200, {"ok": True, "stored": False})
                return
            # Keyed by the account whose transcript this statusline describes,
            # so two accounts stop overwriting each other (usage.py).
            usage_limits.set(windows, profiles.profile_for_path(payload.get("transcript_path")) if profiles is not None else None)
            notify_snapshot()
            self._send_json(200, {"ok": True})

        def _handle_ws_proxy(self) -> None:
            """GET /ws — hand the caller ttyd's socket after checking WHO is asking.

            do_GET's guards have already run, so reaching here means a loopback
            `Host` and an `Origin` that is absent (a native client), loopback, or
            an origin the operator named. That is a STRICTER gate than ttyd's own
            `-O`, which can only ask "does Origin equal Host" and has no idea
            which origins are ours; `-O` stays on regardless, so anything that
            reaches 7681 directly is still refused.

            Restores two things `-O` took away on 2026-08-09: the native xterm
            panel (TerminalPanel.vue), and a no-Origin client attaching at all —
            CLAUDE.md listed the latter as an accepted cost on the grounds that
            nothing did it, which was true only because nothing could.
            """
            # Nothing after this may touch self.wfile: the socket stops being an
            # HTTP connection the moment ttyd answers 101 through the relay.
            self.close_connection = True
            if not is_websocket_upgrade(self.headers):
                self._send_json(400, {"error": "GET /ws takes a WebSocket upgrade"})
                return
            ttyd_port = bridge_info["port"]
            upstream_host = f"127.0.0.1:{ttyd_port}"
            with ProxySlot() as slot:
                if not slot:
                    self._send_json(503, {"error": "too many proxied terminal sockets"})
                    return
                try:
                    upstream = socket.create_connection(("127.0.0.1", ttyd_port), timeout=5)
                except OSError as err:
                    # Name the half that is actually down. The whole bug this
                    # route exists to fix was a green bridge indicator sitting
                    # beside a dead terminal, so a silent failure here would
                    # rebuild it one layer up.
                    self._send_json(502, {"error": f"ttyd unreachable at {upstream_host}: {err}"})
                    return
                try:
                    upstream.settimeout(None)
                    upstream.sendall(build_upstream_request(self.path, self.headers, upstream_host))
                    leftover = pending_buffered(self.rfile, self.connection)
                    if leftover:
                        upstream.sendall(leftover)
                    relay(self.connection, upstream)
                finally:
                    try:
                        upstream.close()
                    except OSError:
                        pass

        def _handle_events(self) -> None:
            """GET /events — SSE stream: an immediate `snapshot` event, then a
            `snapshot` on every state-affecting change, an `activity` event per
            ticker entry, and `: ping` heartbeats while idle. Streams until the
            client disconnects; this handler thread then reaps itself (see
            events.py for the broker design and shutdown notes).
            """
            handle = broker.register()
            if handle is None:
                self._send_json(503, {"error": "too many event streams"})
                return
            try:
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(format_sse_event("snapshot", snapshot_payload()))
                self.wfile.flush()
                while True:
                    self.wfile.write(next_chunk(handle))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # client went away — the only exit from the loop
            finally:
                broker.unregister(handle)

        def _handle_activity(self) -> None:
            """GET /activity?limit=N — recent transition entries, newest first,
            for seeding the ticker (live updates ride the SSE stream)."""
            raw_limit = (self._query().get("limit") or ["50"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 0
            if limit < 1:
                self._send_json(400, {"error": "limit must be a positive integer"})
                return
            self._send_json(200, {"entries": transitions.latest(limit)})

    # Set after the class body: inside it, `plugins = plugins` would look the
    # right-hand name up in the class namespace and never reach this closure.
    StatusHandler.plugins = plugins if plugins is not None else {}
    # A label given on LAYA REVIEW repaints the row it overrules (open_work.py § The operator's label).
    watch_operator_labels(capability("laya"), on_operator_labels)
    StatusHandler.arbiter = arbiter
    StatusHandler.public_status = public_status
    # The LEAD service: started by start_status_server only (its watcher types into panes), driven
    # directly by tests.
    StatusHandler.lead = lead_service
    StatusHandler.inbox = decision_inbox
    StatusHandler.decision_secret = secret
    return StatusHandler


def start_status_server(
    status_port: int,
    default_session: str,
    bridge_info: dict,
    control: ShutdownControl,
    plugins: dict[str, object] | None = None,
    *,
    start_dir: str | None = None,
    profiles: ProfileRegistry | None = None,
    freshness: FreshnessMonitor | None = None,
    settings: Settings | None = None,
    features: FeatureSwitches | None = None,
    updater: update_mod.Updater | None = None,
) -> None:
    """Bind loopback-only and serve from a daemon thread (dies with ttyd).

    The production path, and the only one that starts the state arbiter —
    a handler built by a test is never reconciled against this machine's
    real Claude sessions."""
    settings = settings if settings is not None else Settings()
    switches = features if features is not None else FeatureSwitches()
    live_marks.configure(settings.lease_module)
    rack_path = resolve_rack_db_path()
    # The session ledger's timer (ledger/scheduler.py) — production only, like the arbiter.
    ledger = LedgerScheduler(
        ledger_dir(),
        log=lambda message: click.echo(message, err=True),
        enabled=lambda: switches.on(LEDGER_INGEST),
    )
    handler = build_status_handler(
        StatusStore(),
        SessionIconStore(resolve_icons_path()),
        SessionModelStore(resolve_models_path()),
        default_session,
        bridge_info,
        control,
        plugins,
        start_dir=start_dir,
        profiles=profiles,
        records=SessionRecordStore(resolve_records_path()),
        closed=ClosedSessionStore(resolve_closed_path()),
        auto_names=SessionNameStore(resolve_names_path()),
        personas=SessionPersonaStore(resolve_personas_path()),
        stars=SessionStarStore(resolve_stars_path()),
        missions=seeded_missions(MissionStore(resolve_missions_path()), settings),
        costs=SessionCostStore(resolve_costs_path()),
        rack_metrics=RackMetrics(resolve_metrics_path()),
        decisions=DecisionLedger(decisions_dir()),
        judge_worker=True,
        freshness=freshness,
        usage_limits=UsageLimitsStore(resolve_usage_path()),
        standing=AccountStandingStore(resolve_standing_path()),
        permissions=PermissionLedger(resolve_permissions_path(), log=lambda message: click.echo(message, err=True)),
        ledger=ledger,
        activity_log=ActivityLog(),
        verdicts=SessionVerdictStore(resolve_verdicts_path()),
        work_trees=SessionWorkTreeStore(resolve_work_trees_path()),
        topic_labels=TopicLabels(resolve_topic_labels_path(), log=lambda message: click.echo(message, err=True)),
        next_step_paths=(next_step_mod.resolve_state_path(), next_step_mod.resolve_log_path()),
        loops=OpenLoops(resolve_attention_path()),
        signal_worker=True,
        rack_db=RackDb(rack_path),
        settings=settings,
        find_ui=lambda: ui_static.find_ui_dist(settings.ui_dist),
        features=switches,
        updater=updater,
    )
    server = ThreadingHTTPServer(("127.0.0.1", status_port), handler)
    handler.arbiter.start()
    handler.public_status.start()
    if handler.lead is not None:
        handler.lead.start()
        click.echo(f"  LEAD + inbox:      {rack_path}")
    if handler.suggester is not None:
        handler.suggester.start()
        click.echo(
            f"  next step:         {handler.suggester.mode}  ({next_step_mod.MODEL_VERSION}; "
            f"log {next_step_mod.resolve_log_path()})"
        )
    if ledger.start():
        click.echo(f"  session ledger:    {ledger_dir()}  (ingest every {LEDGER_INTERVAL_SECONDS // 60} min)")
    threading.Thread(target=server.serve_forever, daemon=True, name="status-server").start()


def load_bridge_profiles() -> ProfileRegistry:
    """Load the launch profiles (profiles.py), widen the transcript allowlist
    to every Claude account they name, and echo what is offered. Built once,
    BEFORE the plugins and the status server — both receive this one registry."""
    registry = load_profiles(log=lambda message: click.echo(message, err=True))
    configure_transcript_roots(registry.claude_roots())
    offered = " · ".join(registry.ids())
    click.echo(
        f"  profiles:          {offered}  (default {registry.default_id}; {registry.source} — edit {resolve_profiles_path()})"
    )
    return registry


def load_rack_plugins(
    port: int,
    status_port: int,
    session: str,
    start_dir: str | None,
    profiles: ProfileRegistry | None = None,
    settings: Settings | None = None,
    features: FeatureSwitches | None = None,
) -> tuple[dict[str, object], list[Path]]:
    """The core's built-ins (local-llm), then the configured plugin dirs (or the
    checkout's defaults: plugin_sources.py), and echo what mounted. This bridge hands local-llm one agent runtime the bare
    core does not have: Claude Code on the launch profiles (claude_cli.py). Every
    plugin gets the operator's `plugins.<name>` object and origins through the
    host-side context (plugin_context.py). Also returns the dirs that actually
    mounted — the freshness watcher's plugin set."""
    settings = settings if settings is not None else Settings()
    context = RackPluginContext(
        port=port,
        status_port=status_port,
        default_session=session,
        start_dir=start_dir or "",
        log=click.echo,
        profiles=profiles,
        allowed_origins=settings.allowed_origins,
        operator_settings=settings,
    )
    agent_runtimes = [ClaudeCliRuntime(profiles)] if profiles is not None else []
    source = plugin_sources.resolve_plugin_source(os.environ, plugins_config_path())
    dirs = source.dirs
    click.echo(f"  plugins from:      {source.describe()}")
    if features is not None:
        # The source decides what MAY mount; the operator's switches only subtract (features.py).
        dirs, skipped = mountable(dirs, features)
        if skipped:
            click.echo(f"  plugins off:       {', '.join(skipped)}  (switched off on the Settings page)")
    builtins = builtin_plugins(context, agent_runtimes=agent_runtimes)
    registry = load_plugins(dirs, context, preloaded=builtins)
    for name in registry:
        click.echo(f"  plugin:            {name}  → http://127.0.0.1:{status_port}/plugin/{name}/…")
    for name in unclaimed_settings(settings, registry):
        click.echo(f"  plugin settings:   plugins.{name} in settings.json, but no plugin by that name mounted", err=True)
    return registry, mounted_plugin_dirs(dirs, registry, claimed=set(builtins))


def serve_status(
    port: int,
    status_port: int,
    session: str,
    start_dir: str | None,
    bridge_info: dict,
    control: ShutdownControl,
    settings: Settings | None = None,
) -> None:
    """Profiles, then plugins, then the status server — the one startup order
    both modes share. ONE registry reaches the plugins and the handler, so a
    `/spawn` and a worktree create can never disagree about a profile.
    Raises OSError when the status port cannot be bound."""
    profiles = load_bridge_profiles()
    # One store reaches the plugin loader, the code watch and the handler (features.py).
    features = FeatureSwitches()
    if features.error:
        click.echo(f"  features:          {features.error}", err=True)
    plugins, plugin_dirs = load_rack_plugins(port, status_port, session, start_dir, profiles, settings, features)
    # The checkout's self-update (update.py). Built before the code watch, which asks it whether
    # an update is mid-run (its `busy_reasons`), and bound to that watch once it exists.
    ui_dist = settings.ui_dist if settings is not None else None
    updater = update_mod.Updater(
        update_mod.checkout_root(),
        switches=features,
        find_ui=lambda: ui_static.find_ui_dist(ui_dist),
        log=click.echo,
    )
    # After the plugins, so the baseline hashes exactly what was just imported.
    # The config file counts only when the env override did not shadow it (plugin_sources.py).
    # Watched even while it does not exist, so writing one over the checkout defaults is noticed.
    freshness = None
    try:
        freshness = start_freshness(
            plugin_dirs,
            {**plugins, "update": updater},
            plugins_config=None if os.environ.get(PLUGINS_ENV) else plugins_config_path(),
            profiles_path=resolve_profiles_path(),
            log=click.echo,
            enabled=lambda: features.on(CODE_WATCH),
        )
        if freshness is not None:
            updater.bind(
                stale=freshness.is_stale,
                preflight=freshness.preflight_now,
                busy=freshness.busy_now,
                restart=control.trigger_restart,
            )
        start_status_server(
            status_port,
            session,
            bridge_info,
            control,
            plugins,
            start_dir=start_dir,
            profiles=profiles,
            freshness=freshness,
            settings=settings,
            features=features,
            updater=updater,
        )
    except BaseException:
        if freshness is not None:
            freshness.stop()
        close_plugins(plugins, click.echo)
        raise
    updater.start()
    atexit.register(close_plugins, plugins, click.echo)
    atexit.register(updater.stop)
    if freshness is not None:
        atexit.register(freshness.stop)


def hook_command(status_port: int, event: str, agent: str | None = None) -> str:
    """Shell one-liner for a Claude Code hook: pipe the hook's raw stdin JSON to
    POST /hook for the current tmux session. The hook stays dumb on purpose —
    all parsing/classification lives in the bridge, so future changes there
    never require re-merging hooks into ~/.claude/settings.json again.

    Silent no-op outside tmux (so Claude sessions outside the rack don't fire it) —
    but stdin is still drained there, so the payload writer never sees EPIPE.
    2s curl timeout, and always exits 0 — a non-zero hook exit surfaces errors in
    Claude Code, and exit code 2 would block the hooked event outright.

    `agent` names a CLI whose payload /hook must translate first (`&agent=`,
    antigravity_hooks.AGENT_PARAM); Claude and Codex send none.
    """
    url = f"http://127.0.0.1:{status_port}/hook?session=$(tmux display-message -p '#S')&event={event}"
    if agent is not None:
        url += f"&agent={agent}"
    return (
        'if [ -n "$TMUX" ]; then '
        f'curl -s -m 2 -X POST "{url}" '
        "-H 'Content-Type: application/json' "
        "--data-binary @- >/dev/null 2>&1 || true; "
        "else cat >/dev/null 2>&1; fi"
    )


#: The header an `http` relay names its tmux pane in (`hook_http_entry`).
PANE_HEADER = "X-VC-Pane"


def seeded_missions(store: MissionStore, settings: Settings) -> MissionStore:
    """The production store, with the operator's standing missions minted once (missions.py
    § STANDING MISSIONS). None are listed by default, so by default nothing is minted."""
    store.seed_standing(standing_seeds(settings.standing_missions))
    return store


def hook_http_entry(status_port: int, event: str) -> dict:
    """A Claude Code `http` hook (2.1.63+) relaying the raw hook JSON to POST /hook.

    Replaced `hook_command` for Claude on 2026-10-27. The command form forks
    three processes per event (`sh`, `tmux display-message`, `curl`), and
    PreToolUse fires on every tool call of every agent; this one forks none —
    Claude POSTs in process. The tmux session cannot be computed without a
    shell, so the hook sends its PANE (`$TMUX_PANE`, interpolated by Claude
    because it is in `allowedEnvVars`) and the bridge resolves pane → session
    (`snapshot.PaneSessionCache`). Outside tmux the header is empty and the
    bridge ignores the event. A down bridge is a refused connection, which
    Claude treats as a non-blocking hook error — measured on 2.1.312."""
    return {
        "type": "http",
        "url": f"http://127.0.0.1:{status_port}/hook?event={event}",
        "headers": {PANE_HEADER: "$TMUX_PANE"},
        "allowedEnvVars": ["TMUX_PANE"],
        "timeout": 2,
    }


def build_hooks_settings(status_port: int, events: tuple[str, ...] | None = None, *, transport: str = "http") -> dict:
    """The `hooks` block to merge into ~/.claude/settings.json (`http` relays),
    or — with `events=tuple(CODEX_HOOK_STATES), transport="command"` — into a
    Codex `hooks.json`, which has no http hooks and keeps the curl relay."""

    def entry(event: str) -> dict:
        if transport == "http":
            return hook_http_entry(status_port, event)
        return {"type": "command", "command": hook_command(status_port, event)}

    def group(event: str) -> dict:
        matcher = CLAUDE_MATCHED_HOOKS.get(event) if transport == "http" else None
        return {"matcher": matcher, "hooks": [entry(event)]} if matcher else {"hooks": [entry(event)]}

    chosen = (*HOOK_STATES, *CLAUDE_MATCHED_HOOKS) if events is None else events
    return {"hooks": {event: [group(event)] for event in chosen}}


def statusline_command(status_port: int, script: Path | None = None) -> str:
    """Command for the Claude Code statusLine setting: run statusline.py under
    the system python3 — it's deliberately stdlib-only and self-contained, so
    no venv / `uv run` resolver overhead on every statusline refresh. The path is
    quoted: the setting is a shell command, and a checkout may sit under a
    directory with a space in its name."""
    script = script or Path(__file__).resolve().with_name("statusline.py")
    return f"python3 {shlex.quote(str(script))} --status-port {status_port}"


def build_statusline_settings(status_port: int) -> dict:
    """The `statusLine` block to merge into ~/.claude/settings.json."""
    return {"statusLine": {"type": "command", "command": statusline_command(status_port)}}


def running_bridge_takes_http_hooks(status_port: int) -> bool:
    """Does the bridge on `status_port` resolve an http relay's pane header?
    True when nothing answers: a down bridge will be this code when it starts."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{status_port}/snapshot", timeout=3) as response:
            body = json.loads(response.read())
    except (OSError, ValueError):
        return True
    bridge = body.get("bridge") if isinstance(body, dict) else None
    return isinstance(bridge, dict) and "http" in (bridge.get("hook_transports") or [])


def install_profile_hooks(profile, status_port: int) -> tuple[Path, bool, Path | None, list[str]]:
    """Merge this bridge's relay (+ statusLine and env for Claude) into one
    profile's hooks file — the one install shared by `profiles --install-hooks`
    and POST /profiles/install-hooks. Raises ValueError/OSError like
    `install_hooks`."""
    if profile.kind == antigravity_hooks.KIND:
        agent = antigravity_hooks.AGENT_PARAM
        block = antigravity_hooks.hooks_block(
            lambda event: {
                "type": "command",
                "command": hook_command(status_port, event, agent),
                "timeout": antigravity_hooks.HOOK_TIMEOUT_SECONDS,
            }
        )
        return install_hooks(profile, block, None, status_port)
    return install_hooks(
        profile,
        build_hooks_settings(
            status_port, HOOK_EVENTS_BY_KIND[profile.kind], transport="command" if profile.kind == "codex" else "http"
        )["hooks"],
        build_statusline_settings(status_port)["statusLine"],
        status_port,
    )


#: A tmux client's identity: which server, and which PANE, it is running inside.
#: A bridge started from inside a pane inherits both. That happens by hand, or
#: through a supervisor app when the app was itself `open`ed from a pane, because
#: `open` passes on the caller's environment. Then every ttyd connection's
#: `tmux attach` counts as a client nesting inside that pane, tmux 3.7b refuses it
#: with "sessions should be nested with care", and every attach-only rack pane
#: went dark on live sessions (a supervisor app relaunched from a pane,
#: then a bridge restart). Dropping EITHER variable is enough (measured); the
#: bridge is a server, never a resident of a pane, so it sheds both at startup.
PANE_IDENTITY_ENV = ("TMUX", "TMUX_PANE")


def shed_pane_identity(environ: MutableMapping[str, str]) -> list[str]:
    """Remove `PANE_IDENTITY_ENV` from `environ` in place; returns the names that
    were set. Pure apart from that mutation."""
    shed = [key for key in PANE_IDENTITY_ENV if key in environ]
    for key in shed:
        del environ[key]
    return shed


def ttyd_session_script(session: str, start_dir: str | None) -> str:
    """The `sh -c` body ttyd runs per connection, in `--url-arg` mode.

    ttyd -a appends the URL's ?arg= values as argv, so $1 picks the tmux session
    and --session is the fallback when the URL doesn't choose. -c only applies
    when a session is created; attaches keep their cwd.

    TWO GRAMMARS, and the distinction is load-bearing:

        ?arg=<name>          attach-or-create — a CONSTRUCTOR
        ?arg=attach:<name>   attach-only      — a VIEW

    `tmux new -A` creates whatever it cannot find, so a bare ttyd URL does not
    *show* a session, it *makes* one. Anything that saves or replays such a URL
    therefore resurrects a session the user just killed — which is exactly what
    happened: a page persisted `?arg=scratch` in its saved layout and re-created
    that session on every load, defeating six successive POST /kill-session calls.

    So everything PERSISTED asks for `attach:`; only the agent rack's live pane
    keeps the bare form — the legacy `+ NEW` against a bridge too old for
    POST /spawn, and plain sessions nothing ever stamped with a profile.

    `:` can never appear in a session name (SESSION_NAME_RE, and tmux rejects it
    outright), so the prefix is unambiguous. Note $1 stays a shell VARIABLE in
    both branches — the URL arg is still never interpreted as script text, which
    is the property the security posture in CLAUDE.md rests on.

    The `read` on a failed attach is not cosmetic: without it the script exits
    immediately, ttyd's client reconnects (~30ms) and retries forever, turning
    one dead panel into a process-spawn storm. Blocking gives a readable panel
    and makes Enter a free retry.

    A failed attach is asked WHY before anything is printed, because the two
    answers need opposite treatment:

    - The session is gone: tmux's own stderr is DROPPED and our sentence is the
      only telling. Shown, it opened with `can't find session: <name>`, a raw
      diagnostic about a target the user never typed, and then our sentence
      saying the same thing in words. Two tellings of one fact, the worse first.
    - The session is LIVE and tmux refused anyway: tmux's reason IS the
      information, so it is printed. Dropping it once made every attach-only pane
      in the rack say "session vc-6 is gone" about live sessions while the real
      cause, `sessions should be nested with care` from a bridge that had
      inherited a pane's TMUX (`PANE_IDENTITY_ENV`), stayed invisible.

    stderr goes to an UNLINKED temp file (fds 3 and 4), so nothing is left in
    $TMPDIR when ttyd SIGHUPs the attach, which is how most connections end; if
    mktemp fails, stderr goes to /dev/null. Nothing branches on the text: the
    exit status is what `&& exit 0` reads, and the HTTP routes that DO match on
    "can't find session" parse their own `subprocess` stderr, not this pane.
    """
    start_dir_arg = f" -c {shlex.quote(start_dir)}" if start_dir else ""
    return (
        'case "$1" in\n'
        "attach:*)\n"
        "  name=${1#attach:}\n"
        "  exec 3>/dev/null 4</dev/null\n"
        '  if err=$(mktemp "${TMPDIR:-/tmp}/ttyd-attach.XXXXXX" 2>/dev/null); then\n'
        '    exec 3>"$err" 4<"$err"; rm -f "$err"\n'
        "  fi\n"
        '  tmux attach -t "=$name" 2>&3 && exit 0\n'
        '  if tmux has-session -t "=$name" >/dev/null 2>&1; then\n'
        "    printf '\\n  session %s is running, but tmux refused to attach:\\n  %s\\n"
        '  press Enter to retry.\\n\' "$name" "$(head -c 300 <&4)"\n'
        "  else\n"
        "    printf '\\n  session %s is gone — press Enter to retry, "
        'or delete this panel.\\n\' "$name"\n'
        "  fi\n"
        "  read -r _ ;;\n"
        f'*) exec tmux new -A -s "${{1:-{session}}}"{start_dir_arg} ;;\n'
        "esac"
    )


def port_in_use(port: int) -> bool:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def listening_pid(port: int) -> int | None:
    """PID of the process LISTENing on a loopback TCP port (via lsof), or None."""
    result = subprocess.run(
        ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def ttyd_listen_args(port: int, socket_path: Path | None) -> list[str]:
    """Where ttyd listens. With the status listener in this process, a Unix socket that only this
    account can reach: the terminal port itself is `ttyd_front.TtydFront`, which checks `Host` first.
    `--no-status` execs ttyd and nothing else, so there it keeps the loopback port, unguarded
    against DNS rebinding as it always was."""
    if socket_path is None:
        return ["-p", str(port), "-i", "127.0.0.1"]
    return ["-i", str(socket_path)]


def reap_orphan_ttyds() -> list[tuple[int, Path]]:
    """SIGTERM every ttyd a killed bridge left behind (`ttyd_front.orphan_ttyds` owns what one is);
    the ones signalled, with the socket directory each was serving."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=False, timeout=10)
    reaped = []
    for pid, directory in ttyd_front.orphan_ttyds(result.stdout):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        reaped.append((pid, directory))
    return reaped


def stop_ttyd_on_signal(ttyd: subprocess.Popen, socket_dir: Path) -> None:
    """A SIGTERM or SIGHUP to the bridge also stops its ttyd, then ends the bridge by that same signal.

    A supervisor that restarts the bridge signals whoever holds its ports. ttyd used to hold the
    terminal port and was signalled with it; on a socket it holds none, so the bridge has to take
    its own child down. The default action is restored and the signal re-sent, so the bridge still
    dies BY the signal and whoever started it reads the same exit as before."""

    def stop(signum: int, _frame: object) -> None:
        # No wait: the main thread is inside `ttyd.wait()` already, and ttyd exits on SIGTERM by itself.
        ttyd.terminate()
        shutil.rmtree(socket_dir, ignore_errors=True)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)


def terminal_host_allowed(host: str | None) -> bool:
    """The terminal port's Host rule: the status listener's (`host_allowed`), and never an absent
    Host. Every client of a terminal is a browser or this bridge's own proxy, and both send one."""
    return host is not None and host_allowed(host)


def terminate_if_ttyd(pid: int | None) -> bool:
    """SIGTERM pid only after verifying it is actually a ttyd process.

    Redundant check by design: the pid comes from an lsof port lookup, and the
    listener could have been replaced by an arbitrary squatter in between —
    never SIGTERM something we can't name.
    """
    if pid is None:
        return False
    result = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, check=False)
    if os.path.basename(result.stdout.strip()) != "ttyd":
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def resolve_status_port(port: int, status_port: int | None) -> int:
    """--status-port, else env TLDRACK_STATUS_PORT, else ttyd port + 1."""
    if status_port is not None:
        return status_port
    env_status_port = os.environ.get("TLDRACK_STATUS_PORT")
    return int(env_status_port) if env_status_port else port + 1


def fetch_snapshot(status_port: int, timeout: float = 2.0) -> dict | None:
    """GET /snapshot from a running bridge, or None if unreachable/invalid."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{status_port}/snapshot", timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def post_shutdown(status_port: int, timeout: float = 2.0) -> dict | None:
    """POST /shutdown to a running bridge (no Origin header → guard admits it)."""
    request = urllib.request.Request(f"http://127.0.0.1:{status_port}/shutdown", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="Port ttyd listens on (env TLDRACK_PORT).")
@click.option("--session", default=DEFAULT_SESSION, show_default=True, help="Default tmux session name (env TLDRACK_SESSION).")
@click.option("--cmd", default=None, help="Full command override (disables tmux and ?arg= session selection).")
@click.option("--font-size", default=18, show_default=True, help="ttyd client fontSize (a page overrides it with ?fontSize=).")
@click.option("--read-only", is_flag=True, help="Disable keyboard input from the browser (omit ttyd -W).")
@click.option("--pinned", is_flag=True, help="Serve ONLY the default session — ignore ?arg= session selection from URLs.")
@click.option(
    "--start-dir",
    default=DEFAULT_START_DIR,
    show_default=True,
    help="Directory new tmux sessions start in (env TLDRACK_START_DIR).",
)
@click.option(
    "--status-port",
    default=None,
    type=int,
    help="Port for the Claude status listener (env TLDRACK_STATUS_PORT). [default: --port + 1]",
)
@click.option("--no-status", is_flag=True, help="Skip the status listener; exec ttyd exactly as before it existed.")
@click.option(
    "--no-index-patch",
    is_flag=True,
    help="Serve ttyd's stock index page (no live font size, no Shift+Enter newline, no pane controls).",
)
@click.option(
    "--print-hooks", is_flag=True, help="Print the Claude Code hooks JSON (merge into ~/.claude/settings.json) and exit."
)
@click.option(
    "--print-statusline",
    is_flag=True,
    help="Print the Claude Code statusLine JSON (usage-limit forwarding; merge into ~/.claude/settings.json) and exit.",
)
def main(
    ctx: click.Context,
    port: int,
    session: str,
    cmd: str | None,
    font_size: int,
    read_only: bool,
    pinned: bool,
    start_dir: str,
    status_port: int | None,
    no_status: bool,
    no_index_patch: bool,
    print_hooks: bool,
    print_statusline: bool,
) -> None:
    if ctx.invoked_subcommand == "doctor":
        return  # it reports a broken paths.json or settings.json itself, so it must still run
    try:
        # Before any store or plugin is built: every later path question has one answer (paths.py).
        paths.export_env()
        settings = load_settings()
        standing_seeds(settings.standing_missions)
    except (paths.PathsError, SettingsError, MissionError) as exc:
        raise click.ClickException(f"{exc}  (tldrack doctor shows the whole picture)") from exc
    ctx.obj = settings
    if ctx.invoked_subcommand is not None:
        return  # `tldrack status` / `kill` — don't start a server
    status_port = resolve_status_port(port, status_port)

    if print_hooks:
        click.echo(json.dumps(build_hooks_settings(status_port), indent=2))
        return

    if print_statusline:
        click.echo(json.dumps(build_statusline_settings(status_port), indent=2))
        return

    # Before ttyd or any tmux client is forked: see PANE_IDENTITY_ENV.
    shed = shed_pane_identity(os.environ)
    if shed:
        click.echo(f"tldrack: dropped {', '.join(shed)} inherited from a tmux pane (ttyd's attaches would read as nesting).")

    if shutil.which("ttyd") is None:
        click.echo("tldrack: ttyd not found — install with: brew install ttyd", err=True)
        sys.exit(1)
    if cmd is None and shutil.which("tmux") is None:
        click.echo("tldrack: tmux not found — install with: brew install tmux", err=True)
        sys.exit(1)
    # The default session name lands inside a shell snippet below — keep it to
    # characters tmux accepts anyway (it rejects ':' and '.' in session names).
    if cmd is None and not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        click.echo("tldrack: --session must contain only letters, digits, '-' or '_'", err=True)
        sys.exit(1)
    if port_in_use(port):
        click.echo(f"tldrack: port {port} already has a listener — assuming ttyd is running.")
        click.echo(f"  terminals:         http://localhost:{port}")
        click.echo(f"  attach:            tmux attach -t {session}")
        if no_status or port_in_use(status_port):
            if not no_status:
                click.echo(f"  claude status:     http://127.0.0.1:{status_port}/status  (already listening)")
            sys.exit(0)
        # ttyd is running but the status listener isn't (raw ttyd, or a bridge
        # from before status existed) — serve ONLY the status listener beside it.
        control = ShutdownControl(mode="status-only")
        control.external_ttyd_pid = listening_pid(port)
        bridge_info = capture_identity(
            "status-only",
            port,
            status_port,
            control.external_ttyd_pid,
            ttyd_owned=False,
            supervisor_comms=settings.supervisor_process,
        )
        try:
            serve_status(port, status_port, session, start_dir, bridge_info, control, settings)
        except OSError as err:
            click.echo(f"tldrack: status port {status_port} unavailable ({err}).", err=True)
            sys.exit(1)
        click.echo(
            f"  claude status:     http://127.0.0.1:{status_port}/status  (status-only mode — kept the existing ttyd; Ctrl-C to stop)"
        )
        try:
            control.event.wait()
        except KeyboardInterrupt:
            sys.exit(0)
        # Woken by POST /shutdown — also stop the foreign ttyd, but only after
        # re-verifying the pid still names a ttyd (never an arbitrary squatter).
        if terminate_if_ttyd(control.external_ttyd_pid):
            click.echo(f"tldrack: shutdown — stopped existing ttyd (pid {control.external_ttyd_pid})")
        else:
            click.echo("tldrack: shutdown — left the existing ttyd running (couldn't verify it)")
        sys.exit(0)

    if cmd is None and not os.path.isdir(start_dir):
        click.echo(f"tldrack: --start-dir {start_dir} is not a directory — tmux will use its default.", err=True)
        start_dir = None

    url_arg_mode = cmd is None and not pinned
    if cmd is not None:
        command = shlex.split(cmd)
    elif pinned:
        command = ["tmux", "new", "-A", "-s", session] + (["-c", start_dir] if start_dir else [])
    else:
        # See ttyd_session_script for the two ?arg= grammars and why the
        # attach-only one exists.
        command = ["sh", "-c", ttyd_session_script(session, start_dir), "ttyd-session"]

    # Where ttyd listens: owned by ttyd_listen_args — see the note there.
    socket_dir = None if no_status else ttyd_front.make_socket_dir()
    socket_path = socket_dir / ttyd_front.SOCKET_NAME if socket_dir else None
    argv = [
        "ttyd",
        *ttyd_listen_args(port, socket_path),
        # -O/--check-origin: refuse a WebSocket whose Origin doesn't match Host.
        #
        # WITHOUT this, ANY page the user has open can `new WebSocket(
        # 'ws://127.0.0.1:7681/ws', ['tty'])`, send ttyd's `{"AuthToken":""}`
        # handshake (accepted — no -c credential is configured) and write to the
        # tty. That is drive-by RCE, and CORS does not stop it: the same-origin
        # policy has never applied to the WebSocket handshake, which is exactly
        # why the protocol hands the server an Origin header to check. The
        # loopback bind is not a control either — the victim's own browser is on
        # loopback. Verified against ttyd 1.7.7: without -O a handshake bearing
        # `Origin: https://evil.example` gets 101 Switching Protocols.
        #
        # This file previously omitted -O on the theory that "the embedding
        # iframe's origin is the hosting page (https://…), not localhost, so
        # origin checking would break the embed". That conflates two
        # documents. The page INSIDE the iframe is served by ttyd, so its origin
        # is http://localhost:<port> — matching Host — and it is that document's
        # JS which opens the socket. The parent's origin never appears in the
        # handshake. Verified: with -O, `Origin: http://localhost:<port>`
        # still gets 101 while every foreign origin is dropped.
        #
        # Cost of -O: a handshake with NO Origin is also refused, so a native
        # client could no longer attach to the ttyd socket. Nothing does — native
        # clients and the CLI subcommands all talk to the STATUS port, a
        # different server. Use `tmux attach` to reach the session from a terminal.
        "-O",
        "-t",
        f"fontSize={font_size}",
        "-t",
        f"theme={json.dumps(DARK_THEME, separators=(',', ':'))}",
        "-t",
        "titleFixed=tldrack",
        # Suppress ttyd's own beforeunload handler. Its onWindowUnload calls
        # preventDefault() and sets returnValue, which Chrome renders as the
        # generic "Leave site? Changes you made may not be saved." — and the
        # agent rack navigates its terminal frame on EVERY session switch
        # (?arg=<name> is how a session is picked), so the dialog fired on the
        # single most common action in the rack. The frontend also appends
        # ?disableLeaveAlert=true (multi-terminal-layout.ts) so an
        # already-running bridge doesn't need a restart; this is the canonical
        # switch and covers every surface, including plain-browser visits.
        "-t",
        "disableLeaveAlert=true",
    ]
    for origin in framing.unframeable(settings.allowed_origins):
        click.echo(f"tldrack: allowed origin {origin} can call the bridge but never frame its pages (framing.py).", err=True)
    index_path = None
    if not no_index_patch:
        # Patched index: live font size, the Shift+Enter newline and the pane controls.
        index_path = get_patched_index_path(status_port, framing.framer_origins(settings.allowed_origins))
        if index_path is not None:
            argv += ["-I", str(index_path)]
    if url_arg_mode:
        argv.append("-a")
    if not read_only:
        # ttyd >= 1.7 is read-only without -W
        argv.append("-W")
    argv += command

    click.echo(f"tldrack: serving http://localhost:{port}  (Ctrl-C to stop)")
    if url_arg_mode:
        click.echo(f"  sessions:          ?arg=<name> attaches-or-creates a tmux session (default: {session})")
        click.echo("                     ?arg=attach:<name> attaches ONLY — never creates one")
    click.echo(f"  attach:            tmux attach -t {session}")
    if index_path is not None:
        click.echo("  patched index:     live font size, Shift+Enter newline, pane controls — disable with --no-index-patch")

    if no_status:
        os.execvp("ttyd", argv)

    control = ShutdownControl(mode="full")
    atexit.register(restart_in_place, control)  # first registered, so last to run: see the note there
    # ttyd's pid isn't known yet (Popen comes after the server so /snapshot is
    # never up without identity); the dict is patched in place right below.
    bridge_info = capture_identity(
        "full", port, status_port, ttyd_pid=None, ttyd_owned=True, supervisor_comms=settings.supervisor_process
    )
    try:
        serve_status(port, status_port, session, start_dir, bridge_info, control, settings)
    except OSError as err:
        click.echo(f"tldrack: status port {status_port} unavailable ({err}) — continuing without status.", err=True)
    else:
        click.echo(
            f"  claude status:     http://127.0.0.1:{status_port}/status  (hooks: --print-hooks · usage bar: --print-statusline)"
        )

    # The terminal port is this process's now; ttyd is behind it on a socket (ttyd_front.py owns why).
    assert socket_dir is not None and socket_path is not None
    atexit.register(shutil.rmtree, socket_dir, ignore_errors=True)
    for orphan_pid, orphan_dir in reap_orphan_ttyds():
        click.echo(f"tldrack: stopped a ttyd left by a bridge that was killed (pid {orphan_pid}).")
        shutil.rmtree(orphan_dir, ignore_errors=True)
    front = ttyd_front.TtydFront(
        port,
        socket_path,
        terminal_host_allowed,
        log=click.echo,
        frame_policy=framing.frame_ancestors(settings.allowed_origins),
    )
    try:
        front.start()
    except OSError as err:
        click.echo(f"tldrack: terminal port {port} unavailable ({err}).", err=True)
        sys.exit(1)

    # Not execvp: the status thread must share the process with ttyd. Ctrl-C
    # reaches both (same foreground process group); mirror ttyd's exit code —
    # except on POST /shutdown, where a deliberate stop must exit 0.
    ttyd = subprocess.Popen(argv)
    control.ttyd_proc = ttyd
    bridge_info["ttyd"] = {"pid": ttyd.pid, "owned": True}
    stop_ttyd_on_signal(ttyd, socket_dir)
    if control.requested:
        ttyd.terminate()  # /shutdown raced the Popen — don't leave ttyd orphaned
    try:
        code = ttyd.wait()
        sys.exit(0 if control.requested else code)
    except KeyboardInterrupt:
        ttyd.terminate()
        sys.exit(ttyd.wait())


@main.command()
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge to inspect (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def status(port: int, status_port: int | None) -> None:
    """Show whether a bridge is running, its pid, and who launched it."""
    status_port = resolve_status_port(port, status_port)
    snapshot = fetch_snapshot(status_port)
    if snapshot is None:
        if port_in_use(port):
            pid = listening_pid(port)
            click.echo(
                f"tldrack: no status listener on :{status_port}, but :{port} is taken"
                f" (pid {pid if pid is not None else '?'} — raw ttyd or a pre-status bridge)."
            )
            click.echo("  no identity available — restart it via: tldrack")
        else:
            click.echo(f"tldrack: OFFLINE — nothing listening on :{status_port} (ttyd :{port} also free)")
        sys.exit(1)

    sessions = snapshot.get("sessions") or []
    session_bits = [
        f"{row['session']} (attached)" if row.get("attached") else row["session"]
        for row in sessions
        if isinstance(row, dict) and row.get("live") and row.get("session")
    ]
    bridge = snapshot.get("bridge")
    if not isinstance(bridge, dict):
        click.echo(
            f"tldrack: ONLINE on :{port} / :{status_port} — pre-identity bridge"
            " (no `bridge` info; restart it for pid / launched-by)."
        )
        if session_bits:
            click.echo(f"  sessions:    {' · '.join(session_bits)}")
        return

    launched = bridge.get("launched_by") or {}
    ttyd_info = bridge.get("ttyd") or {}
    ttyd_pid = ttyd_info.get("pid")
    if ttyd_info.get("owned"):
        mode_line = f"{bridge.get('mode')} — owns ttyd (pid {ttyd_pid if ttyd_pid is not None else '?'})"
    else:
        mode_line = f"{bridge.get('mode')} — ttyd pid {ttyd_pid if ttyd_pid is not None else '?'} (not owned)"
    click.echo("tldrack: ONLINE")
    click.echo(f"  pid:         {bridge.get('pid')}  (up {format_uptime(bridge.get('uptime_seconds') or 0)})")
    click.echo(f"  mode:        {mode_line}")
    click.echo(f"  ports:       ttyd :{bridge.get('port')} · status :{bridge.get('status_port')}")
    click.echo(f"  cwd:         {bridge.get('cwd')}")
    click.echo(f"  tty:         {bridge.get('tty') or '(none — headless)'}")
    click.echo(f"  launched by: {launched.get('label') or launched.get('kind') or 'unknown'}")
    if launched.get("kind") == "menubar":
        click.echo(
            "               note: a supervisor app started this bridge and restarts it — quit it or"
            " turn its auto-start off before `tldrack kill`"
        )
    click.echo(f"  sessions:    {' · '.join(session_bits) if session_bits else '(none live)'}")
    click.echo(f"  code:        {describe_freshness(bridge.get('freshness'))}")


def build_ui_if_missing(settings: Settings, say: Callable[[str], None]) -> None:
    """Build `ui/dist` in the checkout when no build is found. Never fatal: the bridge starts
    either way, and `/` says the page is not built (ui_static.py `NOT_BUILT`)."""
    from tldrack import ensure  # noqa: PLC0415 — keeps every other subcommand's import cost

    if ui_static.find_ui_dist(settings.ui_dist) is not None:
        return
    ui_dir = ui_static.checkout_ui_dir()
    commands, why_not = ensure.ui_build_commands(ui_dir, shutil.which("npm"))
    if why_not is not None:
        say(f"tldrack up: the page is not built — {why_not}")
        return
    for command in commands:
        say(f"tldrack up: building the page — {' '.join(command[1:])} (in {ui_dir})")
        try:
            done = subprocess.run(command, cwd=ui_dir, stdout=sys.stderr, stderr=sys.stderr, check=False)
        except OSError as err:
            say(f"tldrack up: the page build could not run ({err}) — starting without it")
            return
        if done.returncode != 0:
            say(f"tldrack up: the page build failed (exit {done.returncode}) — starting without it")
            return
    root = update_mod.checkout_root()
    if root is not None:
        update_mod.stamp_build(root)  # so `/update` knows which source this page is (update.py)


@main.command(name="up")
@click.pass_context
@click.option(
    "--wait-for",
    "wait_for",
    multiple=True,
    metavar="ROUTE",
    help="A route that must answer 200 before `up` succeeds (repeatable), e.g. /plugin/worktree/state.",
)
@click.option("--timeout", default=90.0, show_default=True, help="Seconds to wait for the bridge and every route.")
@click.option("--no-start", is_flag=True, help="Only check: never start a bridge that is down.")
@click.option("--no-build", is_flag=True, help="Do not build the page (ui/dist) when it is missing.")
@click.option(
    "--open/--no-open",
    "open_page",
    default=None,
    help="Open the rack in a browser. [default: only when this run started the bridge, at a terminal]",
)
@click.option(
    "--start-dir",
    default=DEFAULT_START_DIR,
    show_default=True,
    help="Directory new sessions start in when this run starts the bridge (env TLDRACK_START_DIR).",
)
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def up(
    ctx: click.Context,
    wait_for: tuple[str, ...],
    timeout: float,
    no_start: bool,
    no_build: bool,
    open_page: bool | None,
    start_dir: str,
    port: int,
    status_port: int | None,
) -> None:
    """Start the rack: tmux, ttyd and the bridge, the page built when it is missing. Run again any
    time: a bridge that is up is left alone, and this only waits for its routes (ensure.py)."""
    import webbrowser  # noqa: PLC0415
    from tldrack import ensure  # noqa: PLC0415 — keeps every other subcommand's import cost

    settings: Settings = ctx.obj
    status_port = resolve_status_port(port, status_port)
    log_path = paths.state_dir() / ensure.bridge_log_name(status_port)
    io = ensure.MachineIO(
        port=port, status_port=status_port, start_dir=start_dir, log_path=log_path, supervisor_url=settings.supervisor_url
    )

    def say(text: str) -> None:
        click.echo(text, err=True)

    # Only on the way to starting one: an agent's routine `up` against a live bridge stays fast.
    if not no_start and not no_build and io.get("/snapshot", 2.0) is None and io.listener() is None:
        build_ui_if_missing(settings, say)
    progress = ensure.Progress(lambda text: (sys.stderr.write(text), sys.stderr.flush()), sys.stderr.isatty())
    result = ensure.run_up(
        io,
        progress,
        wait_for=wait_for or ensure.DEFAULT_WAIT_FOR,
        timeout=timeout,
        start=not no_start,
        status_port=status_port,
        log_path=str(log_path),
        attach=f"tmux -L {ensure.bridge_socket(status_port)} attach",
    )
    if result.code == 0:
        url = f"http://127.0.0.1:{status_port}/"
        say(f"tldrack up: the rack is at {url}")
        # An agent's or a script's `up` must never raise a browser tab: only a person at a
        # terminal who just started the rack gets one, unless a flag says otherwise.
        if open_page if open_page is not None else (result.started and sys.stderr.isatty() and sys.stdin.isatty()):
            webbrowser.open(url)
    sys.exit(result.code)


def port_holder(port: int) -> tuple[int, str] | None:
    """`(pid, command)` of the listener on `port`, or None when it is free."""
    pid = listening_pid(port)
    if pid is None:
        return (0, "?") if port_in_use(port) else None
    try:
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        command = ""
    return pid, command


@main.command(name="doctor")
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
@click.option("--json", "as_json", is_flag=True, help="Print the checks as JSON (what the rack's Settings page reads).")
def doctor_command(port: int, status_port: int | None, as_json: bool) -> None:
    """Check this machine: tmux, ttyd, node, uv, an agent CLI, the hooks, the ports, the directories,
    and which optional parts are on. Changes nothing. Exit 1 when something stops the rack starting."""
    status_port = resolve_status_port(port, status_port)
    facts = doctor_facts(port, status_port, fetch_snapshot(status_port), worktree_repos=fetch_worktree_repos(status_port))
    sections = doctor_mod.run_checks(facts)
    code, closing = doctor_mod.verdict(sections)
    if as_json:
        click.echo(json.dumps(doctor_mod.wire(sections), indent=2))
        sys.exit(code)
    for title, checks in sections:
        click.echo(f"{title}")
        for check in checks:
            click.echo(check.line())
    click.echo(f"\ntldrack doctor: {closing}")
    sys.exit(code)


def fetch_worktree_repos(status_port: int, timeout: float = 2.0) -> tuple[str, ...] | None:
    """The running bridge's worktree plugin's served repositories (doctor.py `served_worktree_repos`), or
    None when no bridge or no plugin answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{status_port}/plugin/worktree/repos", timeout=timeout) as resp:
            return doctor_mod.served_worktree_repos(json.loads(resp.read()))
    except (OSError, ValueError):
        return None


def doctor_facts(
    port: int,
    status_port: int,
    snapshot: dict | None,
    features: FeatureSwitches | None = None,
    worktree_repos: tuple[str, ...] | None = None,
) -> doctor_mod.Facts:
    """Everything `doctor` reads from this machine (doctor.py `Facts`), for the command and for
    `GET /doctor`. `snapshot` is the running bridge's, or None when none answers; `features` is the
    running bridge's store, or None to read the file. Raises nothing a broken config file causes:
    each becomes the check that names it."""
    try:
        settings: Settings | None = load_settings()
        parts: Mapping[str, bool] | str = settings.enabled()
        standing_seeds(settings.standing_missions)
    except (paths.PathsError, SettingsError, MissionError) as exc:
        settings, parts = None, str(exc)
    described = paths.describe()
    profiles: list[tuple[str, str, bool, bool | None]] = []
    notify_on, mode = False, next_step_mod.DEFAULT_MODE
    if "error" not in described:
        try:
            registry = load_profiles(log=lambda _message: None)
            profiles = [
                (
                    pid,
                    registry.get(pid).kind,
                    registry.cli_path(pid) is not None,
                    registry.hooks_ok(registry.get(pid), status_port, HOOK_EVENTS_BY_KIND),
                )
                for pid in registry.ids()
            ]
        except (OSError, ValueError) as exc:
            parts = parts if isinstance(parts, str) else f"profiles: {exc}"
        notify_on = NotifyConfigStore(resolve_notify_path()).get().topic is not None
        mode = next_step_mod.stored_mode(next_step_mod.resolve_state_path())
    source = plugin_sources.resolve_plugin_source(os.environ, plugins_config_path()) if "error" not in described else None
    switches: FeatureSwitches | None = features
    if switches is None and source is not None:
        # No bridge handed its store over: read the file, and offer the same plugins a start would.
        switches = FeatureSwitches(plugins=(path.name for path in source.dirs))
    return doctor_mod.Facts(
        tools=doctor_mod.find_tools(),
        paths=described,
        settings=parts,
        profiles=profiles,
        ports={port: port_holder(port), status_port: port_holder(status_port)},
        status_port=status_port,
        snapshot=snapshot,
        ui_dist=ui_static.find_ui_dist(settings.ui_dist if settings is not None else None),
        notify_on=notify_on,
        next_step_mode=mode,
        plugin_source=source.describe() if source is not None else None,
        plugin_settings=tuple(settings.plugins) if settings is not None else (),
        features_off=tuple(switches.off_keys()) if switches is not None else (),
        features_error=switches.error if switches is not None else None,
        worktree_repos=worktree_repos,
    )


@main.command()
@click.option("--init", "init", is_flag=True, help="Generate a strong random ntfy topic and write it to the config file.")
@click.option("--test", "send_test", is_flag=True, help="Publish a test notification through the running bridge.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def notify(init: bool, send_test: bool, port: int, status_port: int | None) -> None:
    """Set up or check the phone push for blocked agents (see CLAUDE.md § /notify).

    `--init` exists because the bridge REFUSES a topic under 24 characters, and
    a refusal without a way to comply is just an obstacle. On hosted ntfy.sh the
    topic name is the only secret protecting the channel, so it has to be
    generated rather than typed.
    """
    path = resolve_notify_path()
    store = NotifyConfigStore(path)

    if init:
        config = store.set_topic(generate_topic())
        click.echo(f"tldrack: wrote a new ntfy topic to {path}")
        click.echo("")
        click.echo("  Subscribe on your phone (ntfy app → + → 'Subscribe to topic'):")
        click.echo(f"    {config.topic}")
        click.echo("")
        click.echo("  TREAT THIS LIKE A PASSWORD. Hosted ntfy.sh has no access control:")
        click.echo("  anyone who knows the topic can read your alerts AND publish to them.")
        click.echo("")
        click.echo("  Restart the bridge to pick it up.")
        return

    if send_test:
        status_port = resolve_status_port(port, status_port)
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{status_port}/notify/test", method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                answer = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            click.echo(f"tldrack: no bridge answering on :{status_port} ({err})")
            sys.exit(1)
        if answer.get("sent"):
            click.echo("tldrack: test notification sent — check your phone.")
        else:
            click.echo("tldrack: the bridge accepted the request but ntfy did not take the message.")
            click.echo("  Check the bridge console for the reason (bad topic, rate limit, or no network).")
            sys.exit(1)
        return

    config = store.get()
    if not config.configured:
        click.echo(f"tldrack: phone notifications are OFF — no topic in {path}")
        click.echo("  set one up with: tldrack notify --init")
        return
    click.echo(f"tldrack: phone notifications {'ON' if config.enabled else 'OFF (topic set, toggle disabled)'}")
    click.echo(f"  config: {path}")
    click.echo(f"  server: {config.server}")
    click.echo(
        f"  budget: {NOTIFY_DAILY_BUDGET}/day · {int(NOTIFY_GRACE_SECONDS)}s grace · silent while a rack page is on screen"
    )


#: A `[hooks]` table in Codex's config.toml beside a hooks.json makes Codex load
#: both and warn; the install says so rather than writing a second copy blind.
CODEX_TOML_HOOKS_RE = re.compile(r"^\s*\[\[?hooks[.\]]", re.MULTILINE)


def _mark(value: bool | None) -> str:
    return "–" if value is None else ("✓" if value else "✗")


@main.command(name="permissions")
@click.option("--days", default=7.0, show_default=True, help="How far back to count.")
@click.option("--top", default=15, show_default=True, help="How many patterns to list.")
def permissions_command(days: float, top: int) -> None:
    """The shadow permission ledger as one screen: every prompt an agent raised,
    by pattern, and what the human did (permission_ledger.py). Reads the file,
    so it needs no running bridge."""
    path = resolve_permissions_path()
    stats = PermissionLedger(path).stats(time.time() - days * 86400)
    rate = f"{stats['approve_rate'] * 100:.0f}%" if stats["approve_rate"] is not None else "—"
    wait = f"{stats['median_wait_s']:.0f}s" if stats["median_wait_s"] is not None else "—"
    click.echo(f"permission ledger · last {days:g}d · {path}")
    click.echo(
        f"  {stats['requests']} prompts · {stats['approved']} approved · {stats['denied']} denied · "
        f"{stats['not_run']} not run · {stats['unknown']} unknown · {stats['pending']} pending"
    )
    click.echo(f"  human approve rate {rate} · median wait {wait}")
    if stats["patterns"]:
        click.echo(f"\n  {'pattern':<34} {'n':>4} {'ok':>4} {'no':>4} {'rate':>5}")
        for bucket in stats["patterns"][:top]:
            share = f"{bucket['approve_rate'] * 100:.0f}%" if bucket["approve_rate"] is not None else "—"
            click.echo(
                f"  {bucket['pattern'][:34]:<34} {bucket['requests']:>4} {bucket['approved']:>4} {bucket['denied']:>4} {share:>5}"
            )


@main.command(name="next-step")
@click.option("--days", default=7.0, show_default=True, help="How far back to count.")
@click.option(
    "--mode",
    type=click.Choice(next_step_mod.MODES),
    default=None,
    help="Switch the running bridge's suggester (the AGENT RACK › NEXT STEP row's switch).",
)
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def next_step_command(days: float, mode: str | None, port: int, status_port: int | None) -> None:
    """The suggested next step's week (next_step.py): how often the operator took a suggestion, by
    [Do it] or by hand. Reads the log file, so the counts need no running bridge; `--mode` does."""
    if mode is not None:
        status_port = resolve_status_port(port, status_port)
        request = urllib.request.Request(
            f"http://127.0.0.1:{status_port}/next-step/settings",
            method="POST",
            data=json.dumps({"mode": mode}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                answer = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise click.ClickException(f"no bridge answering on :{status_port} ({err})") from err
        click.echo(f"next step · mode {answer.get('mode')}{'' if answer.get('changed') else ' (unchanged)'}")
    log = next_step_mod.NextStepLog()
    stats = next_step_mod.summarize(log.read(time.time() - days * 86400))
    rate = f"{stats['take_rate'] * 100:.0f}%" if stats["take_rate"] is not None else "—"
    out = stats["outcomes"]
    click.echo(f"next step · last {days:g}d · {log.path}")
    click.echo(f"  {stats['suggested']} suggested · {stats['none']} none · took {stats['took']}/{stats['resolved']} ({rate})")
    click.echo(
        f"  clicked {out['clicked']} · taken by hand {out['taken']} · dismissed {out['dismissed']} · "
        f"other {out['other']} · ignored {out['ignored']} · shown {out['shown']}"
    )
    if stats["by_action"]:
        click.echo("  by action: " + " · ".join(f"{k} {v}" for k, v in sorted(stats["by_action"].items())))
    if stats["rejected"]:
        click.echo("  rejected: " + " · ".join(f"{k} {v}" for k, v in sorted(stats["rejected"].items())))


@main.group(name="decisions")
def decisions_group() -> None:
    """The decision ledger (decisions.py): the operator's answers, the canon a judge learns from."""


@decisions_group.command(name="import")
@click.option(
    "--root", default=".", show_default=True, help="Project whose documents to scan for `Decision (user, YYYY-MM-DD): …` lines."
)
@click.pass_context
def decisions_import(ctx: click.Context, root: str) -> None:
    """Append every operator decision written in the project's documents to the ledger, once each.
    Which documents: the globs under `decision_docs` in settings.json (none by default)."""
    globs = ctx.find_root().obj.decision_docs
    if not globs:
        raise click.ClickException(
            'no documents to read: list globs under "decision_docs" in '
            f'{paths.config_file("settings.json")}, e.g. ["docs/**/*.md"]'
        )
    ledger = DecisionLedger(decisions_dir())
    docs = doc_files(Path(root), globs)
    added = ledger.import_docs(docs)
    click.echo(f"decisions · {added} new from {len(docs)} docs → {ledger.path()}")


@decisions_group.command(name="status")
def decisions_status() -> None:
    """Counts, the shadow judge's agreement, and the newest answers. Reads the file; no bridge needed."""
    summary = DecisionLedger(decisions_dir()).summary()
    rate = f"{summary['agreement'] * 100:.0f}%" if summary["agreement"] is not None else "—"
    click.echo(
        f"{summary['decisions']} decisions ({summary['from_agents']} from agents, {summary['from_docs']} from docs)"
        f" · judge agreed {summary['agreed']}/{summary['judged']} ({rate})"
    )
    for row in summary["recent"]:
        click.echo(f"  {row['session'] or '?':<16} {str(row['question'])[:60]:<60} → {row['answer']}")


main.add_command(mission_group)
main.add_command(lead_group)
main.add_command(ledger_group)


def install_hooks_for(registry: ProfileRegistry, install_id: str, status_port: int) -> bool:
    """Merge this bridge's relay into one profile's hooks file and say what happened. The one
    install behind `profiles --install-hooks ID` and `hooks --install`. False when it could not."""
    profile = registry.get(install_id)
    if profile is None:
        click.echo(f"tldrack: no profile '{install_id}' — one of: {', '.join(registry.ids())}", err=True)
        return False
    if profile.kind == "shell":
        click.echo(f"tldrack: '{install_id}' is a plain shell — it has no hooks to install.", err=True)
        return False
    events = HOOK_EVENTS_BY_KIND[profile.kind]
    if profile.kind == "claude" and not running_bridge_takes_http_hooks(status_port):
        click.echo(
            "tldrack: the running bridge predates http hook relays — restart it first "
            "(tldrack kill && tldrack up), then run this again. Its events would land on the wrong row.",
            err=True,
        )
        return False
    try:
        path, changed, backup, notes = install_profile_hooks(profile, status_port)
    except (ValueError, OSError) as err:
        click.echo(f"tldrack: could not install hooks for '{install_id}': {err}", err=True)
        return False
    if changed:
        click.echo(f"tldrack: relayed {len(events)} hook events for '{install_id}' → {path}")
        if backup is not None:
            click.echo(f"  backup:  {backup}")
    else:
        click.echo(f"tldrack: '{install_id}' already relays to :{status_port} — {path} unchanged")
    for note in notes:
        click.echo(f"  note:    {note}")
    if profile.kind == "codex":
        toml = Path(profile.config_dir or "") / "config.toml"
        try:
            if CODEX_TOML_HOOKS_RE.search(toml.read_text()):
                click.echo(f"  WARNING: {toml} also has a [hooks] table — Codex loads both and warns.")
        except OSError:
            pass
        # Codex runs no non-managed hook until it has been reviewed, and
        # re-asks whenever the file's hash changes.
        click.echo("  next:    start Codex once and trust the new hooks (/hooks) — Codex runs none until reviewed.")
    return True


@main.command(name="profiles")
@click.option(
    "--install-hooks",
    "install_id",
    default=None,
    metavar="ID",
    help="Merge this bridge's status relay into profile ID's hooks file (backed up first).",
)
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def profiles_command(install_id: str | None, port: int, status_port: int | None) -> None:
    """List the launch profiles `+ NEW` / `+ WORKTREE` can start (profiles.py).

    Each line says whether the CLI is installed, whether that account's hooks
    relay to this bridge, and (Claude) whether it is signed in. A profile with
    `hooks ✗` still spawns, but its rack row never leaves "unknown" — that is
    what `--install-hooks` fixes.
    """
    status_port = resolve_status_port(port, status_port)
    registry = load_profiles(log=lambda message: click.echo(message, err=True))

    if install_id is not None:
        sys.exit(0 if install_hooks_for(registry, install_id, status_port) else 1)

    click.echo(f"tldrack: {len(registry.ids())} launch profiles ({registry.source}: {resolve_profiles_path()})")
    missing = []
    for profile_id in registry.ids():
        profile = registry.get(profile_id)
        cli = registry.cli_path(profile_id)
        hooks = registry.hooks_ok(profile, status_port, HOOK_EVENTS_BY_KIND)
        auth = auth_status(profile, cli)
        marker = "*" if profile_id == registry.default_id else " "
        # Only where it differs: the AUTO weight (launch_pick.py) of a smaller plan.
        capacity = f"  ×{profile.capacity:g}" if profile.capacity != 1.0 else ""
        click.echo(
            f" {marker} {profile_id:<18} {profile.label:<10} {profile.kind:<11}"
            f" cli {_mark(cli is not None)}  hooks {_mark(hooks)}  auth {_mark(auth)}"
            f"  {profile.config_dir or ''}{capacity}"
        )
        if hooks is False:
            missing.append(profile_id)
    for profile_id in missing:
        click.echo(f"  → tldrack profiles --install-hooks {profile_id}")


@main.command(name="hooks")
@click.option(
    "--install", is_flag=True, help="Merge the relay into every launch profile that lacks it (each file is backed up first)."
)
@click.option("--profile", "profile_id", default=None, metavar="ID", help="With --install: only this profile.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
def hooks_command(install: bool, profile_id: str | None, port: int, status_port: int | None) -> None:
    """The agent hooks that report each session's state to the rack.

    With no flag: print the Claude Code hooks JSON, to merge by hand (the same text as
    `--print-hooks`). With `--install`: do the merge for every launch profile whose CLI is
    installed and whose hooks do not relay to this bridge yet. A profile already set is left alone.
    """
    status_port = resolve_status_port(port, status_port)
    if not install:
        click.echo(json.dumps(build_hooks_settings(status_port), indent=2))
        return
    registry = load_profiles(log=lambda message: click.echo(message, err=True))
    if profile_id is not None:
        sys.exit(0 if install_hooks_for(registry, profile_id, status_port) else 1)
    wanted = [
        pid
        for pid in registry.ids()
        if registry.hooks_ok(registry.get(pid), status_port, HOOK_EVENTS_BY_KIND) is False and registry.cli_path(pid) is not None
    ]
    if not wanted:
        click.echo(f"tldrack: every installed agent CLI already relays to :{status_port} — nothing to install")
        return
    results = [install_hooks_for(registry, pid, status_port) for pid in wanted]
    sys.exit(0 if all(results) else 1)


@main.command()
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="ttyd port of the bridge to kill (env TLDRACK_PORT).")
@click.option(
    "--status-port", default=None, type=int, help="Status listener port (env TLDRACK_STATUS_PORT). [default: --port + 1]"
)
@click.option(
    "--with-tmux", is_flag=True, help="Also kill the live tmux session(s) the bridge reported (default: leave tmux running)."
)
def kill(port: int, status_port: int | None, with_tmux: bool) -> None:
    """Stop the running bridge (ttyd + status listener). tmux survives by default."""
    status_port = resolve_status_port(port, status_port)
    snapshot = fetch_snapshot(status_port)

    if snapshot is not None:
        bridge = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), dict) else None
        launched = (bridge or {}).get("launched_by") or {}
        if launched.get("kind") == "menubar":
            click.echo(
                "tldrack: WARNING — this bridge was started by a supervisor app, which"
                " restarts it. Quit it or turn its auto-start off, or it will come right back."
            )
        pid = (bridge or {}).get("pid")
        who = f" (launched via {launched['kind']})" if launched.get("kind") else ""
        click.echo(f"tldrack: shutting down{f' pid {pid}' if pid is not None else ''}{who}…")
        if post_shutdown(status_port) is None:
            click.echo("tldrack: POST /shutdown failed — falling back to SIGTERM.", err=True)
            # The port's listener is the bridge itself now; its ttyd is the pid it reported.
            ttyd_pid = ((bridge or {}).get("ttyd") or {}).get("pid")
            if not terminate_if_ttyd(ttyd_pid if isinstance(ttyd_pid, int) else listening_pid(port)):
                click.echo(
                    f"tldrack: couldn't stop the listener on :{port} — inspect it with: lsof -nP -iTCP:{port} -sTCP:LISTEN",
                    err=True,
                )
                sys.exit(1)
    elif port_in_use(port):
        # Raw ttyd or a pre-status bridge: no HTTP shutdown — SIGTERM a verified ttyd.
        pid = listening_pid(port)
        click.echo(f"tldrack: no status listener on :{status_port}; found listener pid {pid} on :{port}.")
        if not terminate_if_ttyd(pid):
            click.echo(
                f"tldrack: pid {pid} is not a ttyd process — refusing to kill it."
                f" Inspect it with: lsof -nP -iTCP:{port} -sTCP:LISTEN",
                err=True,
            )
            sys.exit(1)
        click.echo(f"  sent SIGTERM to ttyd (pid {pid})")
    else:
        click.echo(f"tldrack: nothing running on :{port} or :{status_port}.")
        sys.exit(1)

    for _ in range(10):  # ports can take a moment to free after the shutdown
        if not port_in_use(port) and not port_in_use(status_port):
            break
        time.sleep(0.5)
    ttyd_state = "stopped" if not port_in_use(port) else "STILL LISTENING"
    status_state = "stopped" if not port_in_use(status_port) else "STILL LISTENING"
    click.echo(f"  ttyd :{port} {ttyd_state} · status :{status_port} {status_state}")

    live_sessions = [
        row["session"]
        for row in (snapshot or {}).get("sessions") or []
        if isinstance(row, dict) and row.get("live") and row.get("session")
    ]
    if not with_tmux:
        if live_sessions:
            click.echo(f"  tmux sessions left running: {', '.join(live_sessions)}   (re-run with --with-tmux to kill them)")
        return
    # Only sessions the pre-kill snapshot listed as live (exactly the rack the
    # page shows) — never `tmux kill-server`, which would take unrelated sessions.
    targets = live_sessions or [DEFAULT_SESSION]
    for name in targets:
        result = subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True, check=False)
        click.echo(f"  tmux kill-session {name}: {'ok' if result.returncode == 0 else (result.stderr.strip() or 'failed')}")


@main.command(
    name="laya", add_help_option=False, context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False}
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def laya_command(args: tuple[str, ...]) -> None:
    """Laya's own CLI (`tldrack laya install`, `tldrack laya status`, `tldrack laya --help`): every word
    after `laya` goes to the checkout's plugins/laya/cli.py, run with this interpreter."""
    cli = plugin_sources.checkout_plugins_dir() / "laya" / "cli.py"
    if not cli.is_file():
        click.echo(f"tldrack laya: {cli} is not here: Laya's CLI runs from a checkout of tldrack", err=True)
        sys.exit(1)
    try:
        sys.exit(subprocess.run([sys.executable, str(cli), *args], check=False).returncode)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
