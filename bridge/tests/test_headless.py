"""Headless calls (tldrack/headless.py + ProfileRegistry.headless_call).

The load-bearing guarantees: every flag of the measured cheap recipe is
present (removing one costs 60×), there is no positional prompt, `--bare` never
appears (it skips the subscription login), the env is the PROFILE's minus every
credential that outranks its login and every nested-session variable, and a
login only counts as a subscription when `auth status` says so outright.
"""

import json
from pathlib import Path

from tldrack import headless
from tldrack.profiles import SHED_ENV, Profile, ProfileRegistry

HOME = Path("/Users/someone")
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"what": {"type": "string"}},
    "required": ["what"],
}


def _registry(profiles):
    return ProfileRegistry(profiles, profiles[0].id, home=HOME, source="file", which=lambda name: f"/bin/{name}")


def _pairs(argv):
    return {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1) if argv[i].startswith("--")}


class TestArgv:
    def test_every_cheap_flag_is_present_and_there_is_no_positional_prompt(self):
        argv = headless.build_argv("/bin/claude", system_prompt="SYS", model="haiku", budget_usd=0.05, json_schema=SCHEMA)
        assert argv[:2] == ["/bin/claude", "-p"]
        pairs = _pairs(argv)
        # The $0.112 → $0.002 recipe (module note). Remove one and pay 60×.
        assert pairs["--output-format"] == "json"
        assert pairs["--model"] == "haiku"
        assert pairs["--max-turns"] == "1"
        assert pairs["--tools"] == ""
        assert "--disable-slash-commands" in argv
        assert "--strict-mcp-config" in argv
        assert pairs["--setting-sources"] == ""
        assert pairs["--system-prompt"] == "SYS"
        assert pairs["--max-budget-usd"] == "0.05"
        assert "--no-session-persistence" in argv
        assert pairs["--permission-prompts"] == "none"
        assert "--bare" not in argv  # it skips the keychain a subscription login lives in
        # Nothing after the flags: the prompt rides on stdin.
        assert not any(not a.startswith("--") and a not in pairs.values() for a in argv[2:])

    def test_the_schema_loses_only_its_meta_key(self):
        pairs = _pairs(headless.build_argv("/c", system_prompt="S", model="m", budget_usd=1, json_schema=SCHEMA))
        sent = json.loads(pairs["--json-schema"])
        assert "$schema" not in sent
        assert sent == {k: v for k, v in SCHEMA.items() if k != "$schema"}

    def test_no_schema_means_no_flag(self):
        assert "--json-schema" not in headless.build_argv("/c", system_prompt="S", model="m", budget_usd=1)

    def test_stream_asks_for_stream_json_with_verbose(self):
        argv = headless.build_argv("/c", system_prompt="S", model="m", budget_usd=1, stream=True)
        assert _pairs(argv)["--output-format"] == "stream-json" and "--verbose" in argv


class TestEnv:
    def test_credentials_and_nested_session_vars_are_shed_and_thinking_is_off(self):
        base = {
            "PATH": "/x",
            "TMUX": "/tmp/t,1,0",
            "TMUX_PANE": "%3",
            "CLAUDE_CODE_SESSION_ID": "s",
            **dict.fromkeys(SHED_ENV, "set"),
        }
        env = headless.headless_env(base, SHED_ENV)
        assert env == {"PATH": "/x", "MAX_THINKING_TOKENS": "0"}

    def test_a_token_cap_rides_in_the_env(self):
        env = headless.headless_env({}, SHED_ENV, max_output_tokens=400)
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "400"


class TestBilling:
    MAX = json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "email": "x@y.z", "subscriptionType": "max"}
    )

    def test_a_claude_ai_first_party_login_is_the_subscription(self):
        info = headless.parse_auth_status(self.MAX)
        assert info == {"logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty", "subscription": "max"}
        assert headless.billing_of(info) == "subscription"

    def test_the_email_never_leaves_the_parse(self):
        assert "x@y.z" not in json.dumps(headless.parse_auth_status(self.MAX))

    def test_an_api_key_or_a_cloud_provider_is_api_billing(self):
        key = headless.parse_auth_status(json.dumps({"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"}))
        bedrock = headless.parse_auth_status(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}))
        assert headless.billing_of(key) == "api" and headless.billing_of(bedrock) == "api"

    def test_no_answer_is_unknown_never_subscription(self):
        assert headless.billing_of(headless.parse_auth_status("Logged in as x")) == "unknown"
        assert headless.billing_of(headless.parse_auth_status("")) == "unknown"
        assert headless.billing_of({"logged_in": False, "auth_method": "claude.ai", "api_provider": "firstParty"}) == "unknown"


class TestRegistry:
    def test_a_pinned_profile_runs_on_its_own_config_dir(self):
        registry = _registry([Profile("claude-a", "A", "claude", "/h/.claude-a", True)])
        call = registry.headless_call(
            None, system_prompt="S", model="haiku", budget_usd=0.05, base={"ANTHROPIC_API_KEY": "sk", "TMUX": "t"}
        )
        assert call["profile"] == "claude-a" and call["argv"][0] == "/bin/claude"
        assert call["env"] == {"CLAUDE_CONFIG_DIR": "/h/.claude-a", "MAX_THINKING_TOKENS": "0"}

    def test_the_default_account_unsets_the_config_dir(self):
        registry = _registry([Profile("claude", "CLD", "claude", "/h/.claude", False)])
        call = registry.headless_call(
            "claude", system_prompt="S", model="m", budget_usd=1, base={"CLAUDE_CONFIG_DIR": "/h/.claude-a"}
        )
        assert "CLAUDE_CONFIG_DIR" not in call["env"]

    def test_a_plugin_override_binary_wins(self):
        registry = _registry([Profile("claude", "CLD", "claude", "/h/.claude", False)])
        call = registry.headless_call("claude", system_prompt="S", model="m", budget_usd=1, cli_bin="/opt/claude", base={})
        assert call["argv"][0] == "/opt/claude"

    def test_codex_and_unknown_profiles_have_no_headless_call(self):
        registry = _registry(
            [Profile("claude", "CLD", "claude", "/h/.claude", False), Profile("codex", "CODEX", "codex", "/h/.codex", False)]
        )
        assert registry.headless_call("codex", system_prompt="S", model="m", budget_usd=1) is None
        assert registry.auth_info("codex") is None

    def test_auth_info_is_cached_and_reports_billing(self, monkeypatch):
        calls = []

        def probe(profile, cli_bin):
            calls.append(profile.id)
            return True, headless.parse_auth_status(TestBilling.MAX)

        monkeypatch.setattr("tldrack.profiles.auth_probe", probe)
        registry = _registry([Profile("claude", "CLD", "claude", "/h/.claude", False)])
        first = registry.auth_info("claude")
        assert first["billing"] == "subscription" and first["subscription"] == "max"
        assert registry.auth_info("claude") == first and calls == ["claude"]
        registry.auth_info("claude", fresh=True)
        assert calls == ["claude", "claude"]

    def test_a_failed_probe_is_unknown_billing(self, monkeypatch):
        monkeypatch.setattr("tldrack.profiles.auth_probe", lambda profile, cli_bin: (None, None))
        registry = _registry([Profile("claude", "CLD", "claude", "/h/.claude", False)])
        assert registry.auth_info("claude")["billing"] == "unknown"
