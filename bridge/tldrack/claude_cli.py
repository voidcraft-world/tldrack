"""The `claude_cli` agent runtime: one system + user question to Claude Code's
headless CLI, answered as the same result Ollama gives — ON THE SUBSCRIPTION
OR NOT AT ALL.

PRIVATE to this bridge. The public core's `local-llm` plugin knows only the
agent-runtime contract (`voidcraft_world_bridge/local_llm/agents.py`); this
bridge registers `ClaudeCliRuntime` into it at startup (`main.py`, where the
plugins are loaded), so a stranger's bridge has no Claude path at all.

The call (argv + env) is `profiles.headless_call(...)`, owned by
`headless.py` — see the note there.
This module only decides whether a call may run, reads what came back, and
proves whose bill it landed on.

── Subscription or nothing (2026-10-05) ─────────────────────────────────────
The Claude CLI is called if and only if the call stays on subscription
pricing. Three gates, each of which refuses rather than degrades:

1. BEFORE: the profile's login must be a subscription
   (`profiles.auth_info(...)["billing"] == "subscription"`: a claude.ai
   first-party login). An API key, Bedrock or an unknown login is refused, so
   nothing is sent.
2. BEFORE: the plan must have room. Every call's stream carries the plan's
   `rate_limit_info` (5-hour and 7-day utilization, `isUsingOverage`); the
   plugin keeps the last one per profile (`PlanLedger`), and a profile at or
   past `PLAN_STOP_UTILIZATION` in a window that has not reset yet, or one
   already in overage, is refused. Past 100% a Max plan with extra usage on
   bills per token — "overage" — and that is not subscription pricing.
3. AFTER: the call's own proof. The stream's init event names its
   `apiKeySource` (`none` for a subscription login, measured 2026-10-05;
   `ANTHROPIC_API_KEY` when a key outranked it), and its rate-limit event says
   `isUsingOverage`. Either proof failing fails the JOB with that reason, so
   the caller (a long run) stops at once — a call that already landed on
   the wrong bill is at most one call, capped by `--max-budget-usd`.

`total_cost_usd` is still reported on a subscription; it is the LIST-PRICE
equivalent of the call, not a charge, and is passed on as `list_usd` so a
readout can say so.

── What the reply is ───────────────────────────────────────────────────────
`--json-schema` makes the CLI answer through a `StructuredOutput` tool;
`structured_output` is the parsed object and `result` the same JSON as text.
The text handed back is `structured_output` re-serialised (the parse the
caller would do anyway), else `result`. A reply cut at the token cap
(`CLAUDE_CODE_MAX_OUTPUT_TOKENS`) comes back as an ERROR from the CLI; it is
answered as `text: ""` with `cut: true`, which is what a cut Ollama reply
means to a batch caller: no answer this turn, counted as cut.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

from voidcraft_world_bridge.local_llm.agents import AgentRefused

__all__ = ["AgentRefused", "ClaudeCliRuntime", "PlanLedger", "run_agent"]

RUNTIME = "claude_cli"

PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
"""A launch profile id (`profiles.PROFILE_ID_RE`, by shape)."""

#: The CLI's own aliases for the latest model of each family (`claude --help`),
#: newest-first by what a batch caller should default to. A full model id
#: (`claude-sonnet-5-5`) is accepted too, by shape.
MODEL_ALIASES = ("sonnet", "opus", "fable", "haiku")
DEFAULT_MODEL = "sonnet"
MODEL_ID_RE = re.compile(r"claude-[a-z0-9][a-z0-9.-]{1,62}")

#: One turn's budget, enforced by the CLI (`--max-budget-usd`), in LIST-price
#: dollars. A Sonnet 5.5 batch turn measured $0.040 cold and $0.0034 with the
#: system prompt cached (2026-10-05); the default is ten times the cold turn,
#: and the ceiling is what a caller may ask for.
DEFAULT_TURN_BUDGET_USD = 0.40
MAX_TURN_BUDGET_USD = 2.00

#: A cold Opus turn is under 20 s; this cap is for a wedged login or network.
CALL_TIMEOUT_SECONDS = 180

#: At or past this utilization, in a window that has not reset, no call starts:
#: the next one could be the one that tips the plan into overage.
PLAN_STOP_UTILIZATION = 0.98

#: The `apiKeySource` of a call that ran on the login's own OAuth credentials.
SUBSCRIPTION_KEY_SOURCE = "none"

CUT_MARKER = "output token maximum"


# ── pure ─────────────────────────────────────────────────────────────────────


def model_error(model: object) -> str | None:
    """Why `model` is not a model this runtime takes, or None."""
    if not isinstance(model, str) or not model:
        return "an agent needs a model"
    if model in MODEL_ALIASES or MODEL_ID_RE.fullmatch(model):
        return None
    return f"model must be one of {', '.join(MODEL_ALIASES)} or a full claude-… id"


def parse_stream(stdout: object) -> dict:
    """`stream-json` lines → `{init, rate_limit, result}`, each the last event
    of its kind or None. Lines that are not JSON objects are skipped."""
    parsed: dict = {"init": None, "rate_limit": None, "result": None}
    if not isinstance(stdout, str):
        return parsed
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            parsed["init"] = event
        elif kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            parsed["rate_limit"] = event["rate_limit_info"]
        elif kind == "result":
            parsed["result"] = event
    return parsed


def plan_of(rate_limit: dict | None) -> dict | None:
    """The plan readout a caller shows: each window's utilization (0–1) and
    reset (epoch seconds), and whether the call ran in overage."""
    if not isinstance(rate_limit, dict):
        return None
    windows = rate_limit.get("unifiedWindows") if isinstance(rate_limit.get("unifiedWindows"), dict) else {}
    out: dict = {
        "overage": rate_limit.get("isUsingOverage") is True,
        "status": rate_limit.get("status") if isinstance(rate_limit.get("status"), str) else None,
    }
    for name in ("five_hour", "seven_day"):
        window = windows.get(name) if isinstance(windows.get(name), dict) else {}
        utilization = window.get("utilization")
        resets_at = window.get("resetsAt")
        out[name] = {
            "utilization": float(utilization) if isinstance(utilization, (int, float)) else None,
            "resets_at": int(resets_at) if isinstance(resets_at, (int, float)) else None,
        }
    return out


def plan_block(plan: dict | None, now: float) -> str | None:
    """Why the plan must not take another call, or None (gate 2)."""
    if not plan:
        return None
    if plan.get("overage"):
        return "the plan is already in overage (extra usage bills per token)"
    for name, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
        window = plan.get(name) or {}
        utilization, resets_at = window.get("utilization"), window.get("resets_at")
        if utilization is None or utilization < PLAN_STOP_UTILIZATION:
            continue
        if resets_at is not None and resets_at <= now:
            continue
        return f"the plan's {label} window is {round(utilization * 100)}% used"
    return None


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def read_answer(parsed: dict) -> dict:
    """The proofs (gate 3), then the result as the plugin's chat result.
    Raises `AgentRefused` on a wrong bill or a failed call."""
    init = parsed.get("init") or {}
    source = init.get("apiKeySource")
    if source != SUBSCRIPTION_KEY_SOURCE:
        raise AgentRefused(
            "not_subscription", f"the call ran on {source or 'an unknown credential'}, not the subscription login — stopped"
        )
    plan = plan_of(parsed.get("rate_limit"))
    if plan and plan["overage"]:
        raise AgentRefused("plan_overage", "the call ran in the plan's overage (extra usage bills per token) — stopped")
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise AgentRefused("agent_error", "the CLI ended without a result")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    message = result.get("result") if isinstance(result.get("result"), str) else ""
    cut = result.get("is_error") is True and CUT_MARKER in message
    if result.get("is_error") is True and not cut:
        raise AgentRefused("agent_error", (message or str(result.get("subtype") or "the call failed"))[:300])
    structured = result.get("structured_output")
    text = "" if cut else (json.dumps(structured, separators=(",", ":")) if structured is not None else message)
    model_usage = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    model = init.get("model") if isinstance(init.get("model"), str) else next(iter(model_usage), None)
    cost = result.get("total_cost_usd")
    return {
        "text": text,
        "tool_calls": [],
        "model": model,
        "prompt_tokens": _count(usage.get("input_tokens"))
        + _count(usage.get("cache_creation_input_tokens"))
        + _count(usage.get("cache_read_input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "total_ms": _count(result.get("duration_ms")) or None,
        "load_ms": None,
        "cut": cut,
        "runtime": RUNTIME,
        "billing": "subscription",
        "list_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "plan": plan,
    }


# ── state ────────────────────────────────────────────────────────────────────


class PlanLedger:
    """The last plan readout per profile — what gate 2 reads before a call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, dict] = {}

    def get(self, profile: str) -> dict | None:
        with self._lock:
            plan = self._plans.get(profile)
            return json.loads(json.dumps(plan)) if plan else None

    def set(self, profile: str, plan: dict | None) -> None:
        if plan is None:
            return
        with self._lock:
            self._plans[profile] = plan


# ── I/O ──────────────────────────────────────────────────────────────────────

Run = Callable[..., subprocess.CompletedProcess]


def run_agent(
    request: dict,
    profiles: object,
    ledger: PlanLedger,
    *,
    run: Run | None = None,
    clock: Callable[[], float] = time.time,
    timeout: float = CALL_TIMEOUT_SECONDS,
) -> dict:
    """One question to one Claude profile, behind the three gates. Raises
    `AgentRefused` for every refusal and failure. `run` defaults to
    `subprocess.run`, read at call time."""
    run = run or subprocess.run
    if profiles is None or not hasattr(profiles, "headless_call"):
        raise AgentRefused("no_agents", "this bridge cannot make headless calls — restart it")
    agent = request["agent"]
    profile = agent["profile"]
    auth = profiles.auth_info(profile)
    if auth is None:
        raise AgentRefused("unknown_agent", f"{profile} is not a Claude profile on this bridge")
    if auth.get("billing") != "subscription":
        why = "is not signed in" if not auth.get("signed_in") else f"bills {auth.get('billing')}, not a subscription"
        raise AgentRefused("not_subscription", f"{profile} {why} — refused")
    blocked = plan_block(ledger.get(profile), clock())
    if blocked:
        raise AgentRefused("plan_limit", f"{profile}: {blocked} — refused")
    messages = request["messages"]
    call = profiles.headless_call(
        profile,
        system_prompt=messages[0]["content"],
        model=request["model"],
        budget_usd=agent["budget_usd"],
        json_schema=request["format"] if isinstance(request.get("format"), dict) else None,
        stream=True,
        max_output_tokens=request["max_tokens"],
    )
    if call is None:
        raise AgentRefused("cli_missing", f"the `claude` CLI for {profile} was not found")
    try:
        with tempfile.TemporaryDirectory(prefix="local-llm-agent-") as workdir:
            completed = run(
                call["argv"],
                input=messages[1]["content"],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                env=call["env"],
            )
    except subprocess.TimeoutExpired as err:
        raise AgentRefused("timeout", f"no answer in {timeout:g} s") from err
    except OSError as err:
        raise AgentRefused("cli_missing", f"could not run the CLI ({type(err).__name__})") from err
    parsed = parse_stream(completed.stdout)
    ledger.set(profile, plan_of(parsed.get("rate_limit")))
    if parsed.get("result") is None and completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()
        raise AgentRefused("agent_error", (tail[-1] if tail else f"claude exited {completed.returncode}")[:300])
    answer = read_answer(parsed)
    answer["profile"] = profile
    return answer


# ── the runtime the plugin is handed ────────────────────────────────────────


class ClaudeCliRuntime:
    """The agent runtime `local-llm` calls for `agent.runtime == "claude_cli"`.
    Duck-typed to the contract in `voidcraft_world_bridge/local_llm/agents.py`;
    holds the launch profiles and the plan ledger gate 2 reads."""

    name = RUNTIME
    default_model = DEFAULT_MODEL

    def __init__(self, profiles: object, *, run: Run | None = None, clock: Callable[[], float] = time.time) -> None:
        self._profiles = profiles
        self._plans = PlanLedger()
        self._run = run
        self._clock = clock

    def model_error(self, model: object) -> str | None:
        return model_error(model)

    def validate_agent(self, raw: dict) -> tuple[dict | None, str | None]:
        """`{profile, budget_usd?}` — the profile by shape, the budget clamped to
        the ceiling. Whether the profile exists and may bill is `run`'s job."""
        profile = raw.get("profile")
        if not isinstance(profile, str) or not PROFILE_ID_RE.fullmatch(profile):
            return None, "agent.profile must be a launch profile id"
        budget = raw.get("budget_usd", DEFAULT_TURN_BUDGET_USD)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 < budget <= MAX_TURN_BUDGET_USD:
            return None, f"agent.budget_usd must be above 0 and at most {MAX_TURN_BUDGET_USD}"
        return {"profile": profile, "budget_usd": float(budget)}, None

    def list_agents(self) -> list[dict]:
        """Every Claude launch profile as an agent a caller may name:
        `{id, runtime, profile, label, model, models, cli_found, auth_ok,
        billing, plan}`. `billing` must be `subscription` for a call to run;
        `plan` is the last readout a call brought back."""
        profiles = self._profiles
        if profiles is None or not hasattr(profiles, "auth_info"):
            return []
        agents = []
        for profile_id in profiles.ids():
            described = profiles.describe(profile_id) or {}
            if described.get("kind") != "claude":
                continue
            auth = profiles.auth_info(profile_id) or {}
            agents.append(
                {
                    "id": f"claude:{profile_id}",
                    "runtime": RUNTIME,
                    "profile": profile_id,
                    "label": described.get("label") or profile_id,
                    "model": DEFAULT_MODEL,
                    "models": list(MODEL_ALIASES),
                    "cli_found": bool(profiles.cli_found(profile_id)),
                    "auth_ok": auth.get("signed_in") is True,
                    "billing": auth.get("billing") or "unknown",
                    "plan": self._plans.get(profile_id),
                }
            )
        return agents

    def run(self, request: dict) -> dict:
        return run_agent(request, self._profiles, self._plans, run=self._run, clock=self._clock)
