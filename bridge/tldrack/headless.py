"""Headless agent calls: `claude -p` as ONE function call, on ONE account.

THE owner of the recipe every plugin uses to ask Claude something without a
terminal — worktree BRIEF, coordinator RANK, and the local-llm plugin's
`claude_cli` runtime (a batch caller). Plugins reach it as
`PluginContext.profiles.headless_call(...)` (duck-typed, plain values back),
because a plugin cannot import the host (plugins/README.md).

── The cheap part is the context, not the model ────────────────────────────
Measured 2026-09-05 (Claude Code 2.1.261, haiku 4.5, one trivial prompt): a
bare `claude -p --model haiku` from the repo costs $0.112 a call, because the
CLI loads its default system prompt plus every CLAUDE.md, rule and skill —
55,841 tokens of cache-write for a one-line answer. With `--system-prompt`
(REPLACING the default), `--setting-sources ""`, `--disable-slash-commands`,
`--strict-mcp-config` and `--tools ""`, from a scratch cwd, it costs $0.0018 in
1.7 s. `build_argv` is that recipe, flag by flag, and `tests/test_headless.py`
pins every flag, so a "helpful" removal costs 60×.

Traps that are not in the flag list:
- `--bare` is NOT used. It would skip more, but it also skips keychain and
  OAuth reads — and a subscription login IS a keychain/OAuth read.
- `MAX_THINKING_TOKENS=0` in the env is load-bearing: `-p` inherits
  `effortLevel` from the account's settings.json (it may be `xhigh`) and `--effort`
  does not override it on this path — RANK measured 40 s of thinking about ten
  headlines before finding that.
- `--json-schema` refuses a `$schema` key naming draft 2020-12 ("no schema
  with key or ref", measured 2026-10-05, 2.1.289), which is exactly what zod's
  `toJSONSchema` writes. `build_argv` drops that one key; the rest of the
  schema is what the CLI constrains the reply to.
- The prompt rides on STDIN: `claude` resolves its first positional against
  its SUBCOMMANDS before it is a prompt (`claude -- doctor` ran doctor), and
  stdin has no argv length cap.

── Whose bill: subscription or API key ─────────────────────────────────────
The env is the PROFILE's (its `CLAUDE_CONFIG_DIR`, or none for the default
account) minus `SHED_ENV` — every variable that outranks a config dir's own
login, `ANTHROPIC_API_KEY` first. So a call runs on the account's own login;
whether that login is a subscription is `billing_of(auth_status)`:
`claude auth status` prints `authMethod: "claude.ai"` and a `subscriptionType`
for a Pro/Max login, and anything else (`apiKey`, Bedrock, a console login) is
per-token billing. A caller that must stay on the subscription asks
`ProfileRegistry.auth_info` first and reads every call's own proof after it
(`apiKeySource` on the stream's init event, `isUsingOverage` on its
rate-limit event) — see `bridge/tldrack/claude_cli.py`.

Stdlib only, and pure: nothing here spawns. `ProfileRegistry` runs the one
probe (`auth status`) and caches it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

#: Variables a NESTED launch carries: a bridge started from inside a Claude
#: pane (or a tmux server started from one) hands them to every child, and a
#: child `claude -p` then reports itself as part of that session. `TMUX` is the
#: worst of them: every hook in the account's settings.json is
#: `[ -n "$TMUX" ]`-gated and POSTs to this very bridge, so a call with it set
#: shows up as a phantom agent in the rack.
NESTED_ENV = (
    "TMUX",
    "TMUX_PANE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)

#: The `authMethod` of a Pro/Max/Team login (`claude auth status`).
SUBSCRIPTION_AUTH_METHOD = "claude.ai"

#: The one `apiProvider` a subscription bills through. Bedrock, Vertex and
#: Foundry are an AWS/GCP/Azure bill, whatever the login says.
FIRST_PARTY = "firstParty"

#: What `build_argv` drops from a schema before the CLI sees it (module note).
SCHEMA_META_KEYS = ("$schema",)


def headless_env(base: Mapping[str, str], shed: tuple[str, ...], *, max_output_tokens: int | None = None) -> dict[str, str]:
    """`base` (already carrying the profile's config env) minus `shed` and
    `NESTED_ENV`, with thinking off. Pure.

    `max_output_tokens` caps the reply (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`): the
    CLI has no flag for it, and a caller with a token budget (a batch turn's
    `max_tokens`) must not get an essay because the flag does not exist.
    """
    dropped = set(shed) | set(NESTED_ENV)
    env = {key: value for key, value in base.items() if key not in dropped}
    env["MAX_THINKING_TOKENS"] = "0"
    if max_output_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
    return env


def clean_schema(schema: Mapping[str, object]) -> dict:
    """The schema as the CLI takes it: `SCHEMA_META_KEYS` dropped at the top."""
    return {key: value for key, value in schema.items() if key not in SCHEMA_META_KEYS}


def build_argv(
    binary: str,
    *,
    system_prompt: str,
    model: str,
    budget_usd: float,
    json_schema: Mapping[str, object] | None = None,
    stream: bool = False,
) -> list[str]:
    """The measured cheap recipe — every flag is load-bearing (module note). Pure.

    No positional prompt: it arrives on stdin. `--permission-prompts none` so an
    unattended run can never sit on a question; `--tools ""` so there is nothing
    to ask about; `--max-turns 1` so the answer is the first thing it says;
    `--max-budget-usd` so a runaway call is cut by the CLI itself.

    `stream=True` asks for `stream-json` (it needs `--verbose`): the events
    before the result carry the call's auth proof and its plan utilization,
    which the one-object `json` form leaves out.
    """
    argv = [
        binary,
        "-p",
        "--output-format",
        "stream-json" if stream else "json",
    ]
    if stream:
        argv.append("--verbose")
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(clean_schema(json_schema), separators=(",", ":"))]
    argv += [
        "--model",
        model,
        "--max-turns",
        "1",
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--system-prompt",
        system_prompt,
        "--max-budget-usd",
        f"{budget_usd:g}",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
    ]
    return argv


def parse_auth_status(stdout: object) -> dict | None:
    """`claude auth status` (JSON since 2.1.x) as a plain dict, or None when it
    printed something else. Only the fields a caller decides on are kept — the
    login's email and org stay in the CLI."""
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        document = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    subscription = document.get("subscriptionType")
    return {
        "logged_in": document.get("loggedIn") is True,
        "auth_method": document.get("authMethod") if isinstance(document.get("authMethod"), str) else None,
        "api_provider": document.get("apiProvider") if isinstance(document.get("apiProvider"), str) else None,
        "subscription": subscription if isinstance(subscription, str) and subscription else None,
    }


def billing_of(info: Mapping[str, object] | None) -> str:
    """`subscription`, `api` or `unknown`, from `parse_auth_status`'s dict.

    `subscription` needs all three: logged in, a claude.ai login, first party.
    A login that is first party but not claude.ai is a console/API-key login:
    `api`. No answer is `unknown`, never `subscription` — a caller that must
    stay on the plan treats unknown as no.
    """
    if not info or info.get("logged_in") is not True:
        return "unknown"
    if info.get("auth_method") == SUBSCRIPTION_AUTH_METHOD and info.get("api_provider") == FIRST_PARTY:
        return "subscription"
    return "api"
