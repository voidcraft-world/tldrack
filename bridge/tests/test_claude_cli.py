"""The `claude_cli` agent runtime (tldrack/claude_cli.py) and its routes.

The load-bearing guarantee is "subscription or nothing": a login that is not a
subscription never gets a call, a plan at its limit or in overage never gets
one, and a call whose own stream says it ran on a key or in overage FAILS. The
CLI is stubbed with streams shaped like the real ones (measured 2026-10-05,
Claude Code 2.1.289); the host's `headless_call` is faked (its recipe is
pinned in tests/test_headless.py). The routes are the public core's
`local-llm` plugin with this runtime registered, exactly as `main.py` mounts it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

import urllib.error

from tldrack import claude_cli
from voidcraft_world_bridge.local_llm.plugin import LocalLlmPlugin


def no_local_model(request, timeout=None):  # noqa: ARG001 — an agent turn never reaches a model server
    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


NOW = 1_791_000_000.0
STRUCTURED = {"why": "The queue is short, so I add a job.", "steps": [{"op": "enqueue", "type": "job", "count": 2}]}


def rate_limit(*, overage=False, five=0.25, seven=0.75, resets=NOW + 3600):
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "isUsingOverage": overage,
            "unifiedWindows": {
                "five_hour": {"utilization": five, "resetsAt": resets},
                "seven_day": {"utilization": seven, "resetsAt": resets},
            },
        },
    }


def stream(*, source="none", overage=False, result=None, seven=0.75):
    result = (
        result
        if result is not None
        else {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(STRUCTURED),
            "structured_output": STRUCTURED,
            "total_cost_usd": 0.0034,
            "duration_ms": 1745,
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9645, "output_tokens": 148},
            "modelUsage": {"claude-sonnet-5-5": {}},
        }
    )
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": source, "model": "claude-sonnet-5-5"},
        {"type": "assistant"},
        rate_limit(overage=overage, seven=seven),
        result,
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@dataclass
class FakeProfiles:
    billing: str = "subscription"
    calls: list = field(default_factory=list)

    def ids(self):
        return ["claude-a", "codex"]

    def describe(self, profile_id):
        return {
            "claude-a": {"id": "claude-a", "label": "A", "kind": "claude"},
            "codex": {"id": "codex", "label": "CODEX", "kind": "codex"},
        }.get(profile_id)

    def cli_found(self, profile_id):  # noqa: ARG002
        return True

    def auth_info(self, profile_id, fresh=False):  # noqa: ARG002
        if profile_id != "claude-a":
            return None
        return {"profile": profile_id, "signed_in": True, "billing": self.billing}

    def headless_call(self, profile_id, **kwargs):
        self.calls.append((profile_id, kwargs))
        return {"profile": profile_id, "argv": ["/bin/claude", "-p"], "env": {"MAX_THINKING_TOKENS": "0"}}


class FakeRun:
    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


REQUEST = {
    "agent": {"runtime": "claude_cli", "profile": "claude-a", "budget_usd": 0.4},
    "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "STATE"}],
    "model": "sonnet",
    "max_tokens": 400,
    "format": {"type": "object"},
}


def run(profiles=None, runner=None, ledger=None):
    return claude_cli.run_agent(
        REQUEST, profiles or FakeProfiles(), ledger or claude_cli.PlanLedger(), run=runner or FakeRun(stream()), clock=lambda: NOW
    )


class TestAnswer:
    def test_a_subscription_turn_answers_like_ollama_and_carries_its_proof(self):
        profiles, runner = FakeProfiles(), FakeRun(stream())
        answer = run(profiles, runner)
        assert json.loads(answer["text"]) == STRUCTURED
        assert answer["model"] == "claude-sonnet-5-5" and answer["profile"] == "claude-a"
        assert answer["prompt_tokens"] == 9647 and answer["output_tokens"] == 148 and answer["total_ms"] == 1745
        assert answer["cut"] is False and answer["billing"] == "subscription" and answer["list_usd"] == 0.0034
        assert answer["plan"]["seven_day"] == {"utilization": 0.75, "resets_at": int(NOW + 3600)}

    def test_the_prompt_rides_on_stdin_and_the_host_builds_the_call(self):
        profiles, runner = FakeProfiles(), FakeRun(stream())
        run(profiles, runner)
        profile, kwargs = profiles.calls[0]
        assert profile == "claude-a" and kwargs["system_prompt"] == "SYS" and kwargs["model"] == "sonnet"
        assert kwargs["stream"] is True and kwargs["max_output_tokens"] == 400 and kwargs["budget_usd"] == 0.4
        assert kwargs["json_schema"] == {"type": "object"}
        assert runner.calls[0][1]["input"] == "STATE" and "local-llm-agent-" in runner.calls[0][1]["cwd"]

    def test_a_reply_cut_at_the_token_cap_is_a_cut_turn_not_a_failure(self):
        cut = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "duration_ms": 900,
            "usage": {"output_tokens": 401},
            "result": "API Error: Claude's response exceeded the 400 output token maximum.",
        }
        answer = run(runner=FakeRun(stream(result=cut), returncode=1))
        assert answer["text"] == "" and answer["cut"] is True


class TestGates:
    def test_a_login_that_is_not_a_subscription_never_gets_a_call(self):
        runner = FakeRun(stream())
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(FakeProfiles(billing="api"), runner)
        assert refused.value.code == "not_subscription" and runner.calls == []

    def test_unknown_billing_is_refused_too(self):
        with pytest.raises(claude_cli.AgentRefused):
            run(FakeProfiles(billing="unknown"))

    def test_a_call_that_ran_on_an_api_key_fails(self):
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(runner=FakeRun(stream(source="ANTHROPIC_API_KEY")))
        assert refused.value.code == "not_subscription" and "ANTHROPIC_API_KEY" in refused.value.message

    def test_a_stream_with_no_init_proof_fails_closed(self):
        lines = "\n".join(line for line in stream().splitlines() if '"init"' not in line)
        with pytest.raises(claude_cli.AgentRefused):
            run(runner=FakeRun(lines))

    def test_a_call_that_ran_in_overage_fails_and_blocks_the_next(self):
        ledger = claude_cli.PlanLedger()
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(runner=FakeRun(stream(overage=True)), ledger=ledger)
        assert refused.value.code == "plan_overage"
        runner = FakeRun(stream())
        with pytest.raises(claude_cli.AgentRefused) as second:
            run(runner=runner, ledger=ledger)
        assert second.value.code == "plan_limit" and runner.calls == []

    def test_a_full_window_blocks_until_it_resets(self):
        ledger = claude_cli.PlanLedger()
        run(runner=FakeRun(stream(seven=0.99)), ledger=ledger)
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(ledger=ledger)
        assert refused.value.code == "plan_limit" and "7-day" in refused.value.message
        assert claude_cli.plan_block(ledger.get("claude-a"), NOW + 7200) is None

    def test_a_cli_error_is_the_reason(self):
        failed = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "budget exceeded"}
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(runner=FakeRun(stream(result=failed), returncode=1))
        assert refused.value.code == "agent_error" and refused.value.message == "budget exceeded"

    def test_a_timeout_is_named(self):
        with pytest.raises(claude_cli.AgentRefused) as refused:
            run(runner=FakeRun(raises=subprocess.TimeoutExpired("claude", 180)))
        assert refused.value.code == "timeout"


@dataclass
class Context:
    profiles: object = None
    start_dir: str = "."
    log: object = None


def mounted(profiles):
    """The plugin as this bridge mounts it: the runtime registered when there are profiles."""
    runtimes = [claude_cli.ClaudeCliRuntime(profiles)] if profiles is not None else []
    return LocalLlmPlugin(Context(profiles=profiles), opener=no_local_model, start_worker=False, agent_runtimes=runtimes)


class TestRoutes:
    def plugin(self, profiles=None):
        return mounted(profiles or FakeProfiles())

    BODY = {
        "system": "SYS",
        "user": "STATE",
        "model": "sonnet",
        "max_tokens": 400,
        "format": {"type": "object"},
        "agent": {"runtime": "claude_cli", "profile": "claude-a"},
    }

    def test_an_agent_body_queues_on_the_agent_store_and_job_finds_it(self, monkeypatch):
        plugin = self.plugin()
        monkeypatch.setattr(claude_cli.subprocess, "run", FakeRun(stream()))
        status, job = plugin.handle_post("/chat", {}, dict(self.BODY))
        assert status == 202 and plugin._jobs.counts() == {"queued": 0, "running": 0}
        assert plugin._agent_jobs.run_next() is True
        status, done = plugin.handle_get("/job", {"id": [job["job_id"]]})
        assert status == 200 and done["state"] == "done" and done["result"]["billing"] == "subscription"

    def test_a_refusal_fails_the_job_with_its_code(self):
        plugin = self.plugin(FakeProfiles(billing="api"))
        _, job = plugin.handle_post("/chat", {}, dict(self.BODY))
        plugin._agent_jobs.run_next()
        done = plugin.handle_get("/job", {"id": [job["job_id"]]})[1]
        assert done["state"] == "failed" and done["error"]["code"] == "not_subscription"

    @pytest.mark.parametrize(
        "agent, why",
        [
            ({"runtime": "codex_cli", "profile": "codex"}, "agent.runtime"),
            ({"runtime": "claude_cli", "profile": "../etc"}, "agent.profile"),
            ({"runtime": "claude_cli", "profile": "claude-a", "budget_usd": 50}, "budget_usd"),
            ("claude", "agent must be an object"),
        ],
    )
    def test_bad_agents_are_400(self, agent, why):
        status, body = self.plugin().handle_post("/chat", {}, dict(self.BODY, agent=agent))
        assert status == 400 and why in body["message"]

    def test_an_agent_takes_one_question_and_a_known_model(self):
        plugin = self.plugin()
        tools = dict(self.BODY, tools=[{"type": "function", "function": {"name": "f"}}])
        assert plugin.handle_post("/chat", {}, tools)[0] == 400
        assert plugin.handle_post("/chat", {}, dict(self.BODY, model="gpt-6"))[0] == 400
        assert plugin.handle_post("/chat", {}, dict(self.BODY, model="claude-opus-5-5"))[0] == 202
        no_model = {k: v for k, v in self.BODY.items() if k != "model"}
        assert plugin.handle_post("/chat", {}, no_model)[1]["model"] == "sonnet"

    def test_status_lists_claude_profiles_as_agents_only_when_asked(self):
        plugin = self.plugin()
        assert "agents" not in plugin.handle_get("/status", {})[1]
        agents = plugin.handle_get("/status", {"agents": ["1"]})[1]["agents"]
        assert agents == [
            {
                "id": "claude:claude-a",
                "runtime": "claude_cli",
                "profile": "claude-a",
                "label": "A",
                "model": "sonnet",
                "models": list(claude_cli.MODEL_ALIASES),
                "cli_found": True,
                "auth_ok": True,
                "billing": "subscription",
                "plan": None,
            }
        ]

    def test_a_host_without_profiles_lists_no_agents(self):
        assert mounted(None).handle_get("/status", {"agents": ["1"]})[1]["agents"] == []

    def test_a_host_without_profiles_refuses_an_agent_body(self):
        # A stranger's bridge registers no agent runtime, so `claude_cli` is just an unknown name.
        status, body = mounted(None).handle_post("/chat", {}, dict(self.BODY))
        assert status == 400 and "agent.runtime" in body["message"]
