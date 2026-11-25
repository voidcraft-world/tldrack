"""Launch profiles: WHICH agent CLI a new session runs, on WHICH account.

One machine runs several agent CLIs, and several accounts of the same one —
plain `claude`, `claude` pinned to a second account by `CLAUDE_CONFIG_DIR`, and
Codex. A profile names one of them: `{id, label, kind, config_dir, pin_env,
args, model_label, capacity}`. The bridge owns the list because the bridge is what
spawns; every client names a profile by id and nothing more.

THE SESSION → PROFILE LINK LIVES IN TMUX, not here. `spawn_argv` stamps the new
session with a `@vc_profile` user option in the SAME tmux invocation that
creates it, so no poll can ever see it unstamped. The option dies with the
session (it cannot go stale) and survives a bridge restart (it needs no store);
`snapshot.py` reads it back as a pane-format field. A hook's transcript path is
the second, stronger source — see `profile_for_path`.

Source: `~/.config/tldrack/profiles.json` (env
`TLDRACK_PROFILES_PATH`), tolerant-loaded like plugins.json:

    {"version": 1, "default": "claude-a", "profiles": [
      {"id": "claude-a", "label": "A", "kind": "claude",
       "config_dir": "~/.claude-a"},
      {"id": "codex", "label": "CODEX", "kind": "codex",
       "args": ["-m", "gpt-5-codex"], "model_label": "gpt-5-codex"}]}

Absent or unusable → DETECTED defaults (`detect_profiles`). Detection never
reads the bridge's own env to name a profile — a bridge launched from a
claude-a pane carries `CLAUDE_CONFIG_DIR=~/.claude-a`, and labelling
by env would call that account `claude`. The env only picks the DEFAULT.

Stdlib only: this runs inside the bridge venv, and plugins reach it through
`PluginContext.profiles` (they cannot import the host).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping

from tldrack import paths
from tldrack.profile_availability import ProfileAvailability
from tldrack import antigravity_hooks, headless
from tldrack.atomic_store import atomic_write
from tldrack.session_lineage import prepare_lineage, lineage_args
from tldrack.codex_usage import CodexUsageReader

PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PROFILE_KINDS = ("claude", "codex", antigravity_hooks.KIND, "shell")

#: The id a client sends for "the freest account" (`launch_pick.py`), never a
#: profile of its own — a configured row under it is refused.
AUTO_PROFILE_ID = "auto"

#: A profile's subscription size relative to a standard one: a half-size plan
#: is 0.5. Weighs its remaining usage in the AUTO pick (`launch_pick.py`).
DEFAULT_CAPACITY = 1.0
CAPACITY_MAX = 10.0
PROFILES_FILE_VERSION = 1
LABEL_MAX = 10
MODEL_LABEL_MAX = 64
PROFILES_FILE = "profiles.json"

#: The tmux user option a spawn stamps onto its session. Session-scoped, so
#: `#{@vc_profile}` in a `list-panes -a` format resolves it for every pane.
PROFILE_OPTION = "@vc_profile"

#: The env var each kind reads its config dir from, and that dir's default.
#: Antigravity has no such variable (agy keeps everything under ~/.gemini and
#: documents no way to move it), so it runs ONE account: `parse_profile` refuses
#: any other config dir rather than spawn on the wrong login.
CONFIG_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
DEFAULT_CONFIG_DIRS = {"claude": "~/.claude", "codex": "~/.codex", antigravity_hooks.KIND: "~/.gemini"}

#: A kind whose binary is not named after it. Everything else runs `<kind>`.
CLI_NAMES = {antigravity_hooks.KIND: antigravity_hooks.CLI_NAME}

#: Where a spawned Claude's hooks live (merged into `settings.json`) and a
#: Codex's (`hooks.json` — confirmed against codex-cli 0.160: the binary loads
#: `$CODEX_HOME/hooks.json` as a `HooksFile` in Claude's own shape, and
#: learn.chatgpt.com/docs/hooks documents the same path and schema).
HOOKS_FILES = {"claude": "settings.json", "codex": "hooks.json", antigravity_hooks.KIND: antigravity_hooks.HOOKS_FILE}

#: Variables that OUTRANK a config dir's own login, so a pinned account that
#: inherited one would silently run on the wrong credentials — the list
#: FirstMate's `fm-worker-account-lib.sh` sheds, plus `CLAUDECODE`, which a
#: tmux server started from inside a Claude session carries and which newer
#: Claude Code reads as a nested launch. A tmux session inherits the SERVER's
#: environment, usually far older than the bridge's, so none of this can be
#: assumed clean.
SHED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDECODE",
    # A model the tmux server inherited would decide every agent's model, and its
    # subagents', over the launch's own choice (`LAUNCH MODELS` below).
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
)

#: Fixed install locations probed after PATH. The bridge is often launched by
#: a supervisor, whose PATH holds none of them.
CLI_PROBE_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

#: `claude auth status` reads a keychain entry; a wedged one must not hang the CLI.
AUTH_TIMEOUT_SECONDS = 10

#: How long one `auth status` reading is trusted by `ProfileRegistry.auth_info`.
#: A batch caller asks before every turn; a login does not change between turns,
#: and a probe is a keychain read plus a Node start (~0.5 s).
AUTH_CACHE_SECONDS = 60.0

#: How long a CLI lookup is trusted. `/snapshot` reports `cli_found` per profile
#: on every poll and every SSE push; a PATH walk each time is wasted stats,
#: while an install still shows up within half a minute.
CLI_CACHE_SECONDS = 30.0

#: Codex's opt-out from its shared app-server daemon — why every spawn wants it
#: is owned by `build_inner`. Matched as an option at the start of a `--help`
#: line, so prose that merely mentions the flag does not count.
CODEX_NO_DAEMON_FLAG = "--no-daemon"
CODEX_NO_DAEMON_RE = re.compile(r"^\s*--no-daemon(?![\w-])", re.MULTILINE)

#: `codex --help` is a native binary start; a wedged one must not hold a spawn.
CODEX_HELP_TIMEOUT_SECONDS = 5

#: Marks a hook command as the bridge's relay (`main.hook_command`), so an
#: install replaces its own entries and never touches anyone else's.
RELAY_MARKER = "/hook?session=$(tmux display-message -p '#S')"
#: …and the same for an `http` relay (Claude's form since 2026-10-27,
#: `main.hook_http_entry`): its url, which names no session.
HTTP_RELAY_PATH = "/hook?event="
STATUSLINE_MARKER = "statusline.py"

#: The `env` a Claude account's settings.json must carry, merged by `install_hooks`.
#: Settings `env` reaches EVERY launch of that account — the rack, the dock, a bare
#: iTerm `claude` — where a spawn-time prefix would reach only the rack's.
#: CLAUDE_CODE_ENABLE_TODO_TOOLS: since 2.1.268 the task tools (TaskCreate/…,
#: TodoWrite) exist only on Opus 4.7 / Sonnet 4.6 / Haiku 4.5 and older unless
#: this is set, and without them the rack map's task strip stays dark (tasks.py).
CLAUDE_SETTINGS_ENV = {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}

#: The tmux user option a spawn stamps with its launch permission mode, beside
#: `@vc_profile`, so a restore or reopen brings the session back in the same mode.
PERMISSION_MODE_OPTION = "@vc_permission_mode"

#: Launch permission modes a client may ask a spawn for: a CLOSED set, each
#: mapped per kind to that CLI's own flag. A shell (or an unknown kind) has no
#: approvals and takes nothing. Never a bypass: `bypassPermissions`,
#: `--dangerously-skip-permissions` and Codex's
#: `--dangerously-bypass-approvals-and-sandbox` have no row here, on purpose.
#: - claude `auto`: `--permission-mode auto` (`claude --help`, 2.1.318: choices
#:   acceptEdits, auto, bypassPermissions, manual, dontAsk, plan).
#: - codex `auto`: `--approve-for-me` (`codex --help`, 0.165: "Route approval
#:   requests through automatic review using the workspace-write sandbox";
#:   `codex_rollout.py` reads the resulting `approvals_reviewer=auto_review`).
#: Before a spawn asks for one, `ProfileRegistry.permission_refusal` says whether
#: that profile's CLI can start in it (a shell, a pinned profile, an older CLI
#: cannot). A restore replays the launch mode unless the operator switched out of
#: it since (`restore.restore_mode`).
PERMISSION_ARGS: dict[str, dict[str, tuple[str, ...]]] = {
    "auto": {"claude": ("--permission-mode", "auto"), "codex": ("--approve-for-me",)},
}
PERMISSION_MODES = tuple(PERMISSION_ARGS)


def clean_permission_mode(value: object) -> str | None:
    """A known launch permission mode, or None. Pure — for every reader of a
    stored or stamped mode (tmux options and the state files are writable by
    anything running as the user)."""
    return value if isinstance(value, str) and value in PERMISSION_ARGS else None


def permission_args(kind: object, mode: object) -> list[str]:
    """THE launch permission mode → CLI args mapping. Pure. No mode → no args; a
    kind without a row (shell) → no args; an unknown mode RAISES ValueError, so a
    caller validates first (`clean_permission_mode`) and a typo never spawns an
    agent in a mode nobody asked for."""
    if mode is None:
        return []
    if clean_permission_mode(mode) is None:
        raise ValueError(f"unknown permission mode {mode!r}")
    return list(PERMISSION_ARGS[mode].get(kind, ())) if isinstance(kind, str) else []


#: Flags in a profile's own `args` that already decide its approvals. A profile
#: carrying one PINS its mode, and a launch mode on top is refused rather than
#: appended — Claude takes the LAST `--permission-mode`, so appending would
#: quietly override a profile the operator set to `plan`. Codex's `-c` keys that
#: do the same are matched as `-c <key>=…` / `--config <key>=…`.
PERMISSION_PINS = {
    "claude": ("--permission-mode", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"),
    "codex": (
        "--approve-for-me",
        "-a",
        "--ask-for-approval",
        "-s",
        "--sandbox",
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
    ),
}
PERMISSION_CONFIG_KEYS = ("approval_policy", "approvals_reviewer", "sandbox_mode")


def pinned_permission_flag(profile: Profile) -> str | None:
    """The flag in `profile.args` that already decides its approvals, or None. Pure."""
    flags = PERMISSION_PINS.get(profile.kind, ())
    args = list(profile.args)
    for index, arg in enumerate(args):
        if any(arg == flag or arg.startswith(flag + "=") for flag in flags):
            return arg.split("=", 1)[0]
        if profile.kind == "codex" and arg in ("-c", "--config") and index + 1 < len(args):
            if args[index + 1].split("=", 1)[0].strip() in PERMISSION_CONFIG_KEYS:
                return f"{arg} {args[index + 1].split('=', 1)[0].strip()}"
    return None


#: An option line's indent in a CLI's `--help`; continuation lines sit deeper.
HELP_OPTION_INDENT_MAX = 8


def cli_offers_permission(kind: object, mode: object, help_text: object) -> bool:
    """Does this `<cli> --help` offer `permission_args(kind, mode)`? Pure.

    The FLAG must be an option line (not prose that mentions it), and a flag's
    value must be one of its printed choices — Commander prints them quoted
    (`(choices: "acceptEdits", "auto", …)`), wrapped over deeper-indented lines.
    An older CLI without the flag or the choice would refuse the argument and
    the pane would fall straight to `[<id> exited 1]`, which is why a launch mode
    is checked against the binary that will run, the way `--no-daemon` is."""
    if not isinstance(help_text, str) or clean_permission_mode(mode) is None or not isinstance(kind, str):
        return False
    wanted = PERMISSION_ARGS[mode].get(kind)
    if not wanted:
        return False
    flag, values = wanted[0], wanted[1:]
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if indent > HELP_OPTION_INDENT_MAX or not re.match(rf"{re.escape(flag)}(?![\w-])", line.lstrip()):
            continue
        block = [line]
        for more in lines[index + 1 :]:
            if not more.strip() or len(more) - len(more.lstrip()) <= indent + 2:
                break
            block.append(more)
        text = " ".join(block)
        return all(f'"{value}"' in text for value in values)
    return False


# -- LAUNCH MODELS -------------------------------------------------------------
#: A launch may name the model its agent runs on, and a WEAKER one for everything
#: that agent delegates in process (its subagents, workflow agents, teammates).
#: Claude only: a Codex profile's model is its own `args` (`-m …`).
#: - `model` rides as `--model`, after the profile's args, like a permission mode.
#: - `subagent_model` rides as two env operands. `CLAUDE_CODE_SUBAGENT_MODEL` alone
#:   is only a default: an agent definition's `model:` or the model Claude passes
#:   on the call outranks it. `…_FORCE=1` makes it the model of EVERY subagent
#:   (Claude Code 2.1.257+; an older CLI ignores it silently, hence the version
#:   gate in `ProfileRegistry.model_refusal`). A FORK is the one exception: it
#:   always runs on its parent's model, so a caller that needs the rule to hold
#:   denies forks itself (`lead/service.py` `hook_tool`).
#: - Both are stamped on the session (`MODEL_OPTION`, `SUBAGENT_MODEL_OPTION`) and
#:   replayed by a restore, so a capped agent never comes back uncapped.
#: Families, weakest first. "Weaker" is a strictly lower index.
MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable")
LAUNCH_MODEL_ID_RE = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,63}")
MODEL_OPTION = "@vc_model"
SUBAGENT_MODEL_OPTION = "@vc_subagent_model"
SUBAGENT_MODEL_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"
SUBAGENT_FORCE_ENV = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
SUBAGENT_FORCE_MIN_VERSION = (2, 1, 257)
#: A profile whose own `args` carry one of these PINS its model: a launch model on
#: top is refused, never appended (Claude takes the LAST `--model`).
MODEL_PINS = ("--model",)
CLI_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def model_tier(value: object) -> int | None:
    """A model's rank in `MODEL_FAMILIES`, or None when it names no family. Pure.
    An alias is its own family; an id (`claude-opus-5-5`) is the family among its
    dash-separated parts."""
    if not isinstance(value, str):
        return None
    if value in MODEL_FAMILIES:
        return MODEL_FAMILIES.index(value)
    if not LAUNCH_MODEL_ID_RE.fullmatch(value):
        return None
    named = [family for family in MODEL_FAMILIES if family in value.split("-")]
    return MODEL_FAMILIES.index(named[0]) if len(named) == 1 else None


def clean_launch_model(value: object) -> str | None:
    """A family alias or a `claude-…` id that names exactly one family, or None.
    Pure — for every reader of a requested, stored or stamped model. An id whose
    family cannot be read is refused: its rank is what "weaker" is checked against."""
    return value if model_tier(value) is not None else None


def model_args(kind: object, model: object) -> list[str]:
    """THE launch model → CLI args mapping. Pure. None → no args; a kind other than
    claude → no args; an unknown model RAISES ValueError (validate first)."""
    if model is None:
        return []
    if clean_launch_model(model) is None:
        raise ValueError(f"unknown launch model {model!r}")
    return ["--model", model] if kind == "claude" else []


def subagent_env(kind: object, model: object) -> dict[str, str]:
    """THE subagent model → spawn env mapping, in operand order. Pure; same
    contract as `model_args`."""
    if model is None:
        return {}
    if clean_launch_model(model) is None:
        raise ValueError(f"unknown subagent model {model!r}")
    return {SUBAGENT_MODEL_ENV: model, SUBAGENT_FORCE_ENV: "1"} if kind == "claude" else {}


def pinned_model_flag(profile: Profile) -> str | None:
    """The flag in a Claude profile's `args` that already picks its model, or None. Pure."""
    if profile.kind != "claude":
        return None
    for arg in profile.args:
        if any(arg == flag or arg.startswith(flag + "=") for flag in MODEL_PINS):
            return arg.split("=", 1)[0]
    return None


def pinned_model(profile: Profile) -> str | None:
    """The model a pinning profile's `args` name, cleaned, or None. Pure."""
    args = list(profile.args)
    for index, arg in enumerate(args):
        if arg in MODEL_PINS and index + 1 < len(args):
            return clean_launch_model(args[index + 1])
        if any(arg.startswith(flag + "=") for flag in MODEL_PINS):
            return clean_launch_model(arg.split("=", 1)[1])
    return None


def parse_cli_version(text: object) -> tuple[int, int, int] | None:
    """`claude --version` stdout (`2.1.322 (Claude Code)`) → its triple, or None. Pure."""
    match = CLI_VERSION_RE.search(text) if isinstance(text, str) else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def read_cli_version(cli_bin: str) -> str | None:
    """`<cli> --version`'s stdout, or None when it could not be read. Never raises."""
    try:
        result = subprocess.run(
            [cli_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=CODEX_HELP_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


#: A Claude Code session id — a uuid, and ONLY a uuid. Every source hands it
#: over as untrusted text (a hook body, Claude's own `sessions/<pid>.json`, a
#: transcript filename), and it ends up on a CLI's argv. The argument-injection
#: reasoning is owned by plugins/coordinator/coordinator/resume.py
#: `SESSION_ID_RE` — see the note there; plugins cannot import this module, so
#: that copy stays.
AGENT_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def resume_args(kind: object, agent_session_id: object) -> list[str] | None:
    """The CLI args that reopen one conversation, or None when this kind cannot
    (or the id is not a uuid). Pure — THE resume shape for every door that
    reopens a conversation (`POST /restore`, coordinator RESUME).

    **`--resume=<id>`, attached, never `--resume <id>`.** shlex quoting defeats
    the shell but not the CLI: `-` is in its safe charset, so an id shaped like
    `--dangerously-skip-permissions` would ride through and reach `claude` as
    its own flag. Commander binds an `=`-attached value literally, whatever it
    starts with, which closes that channel for every flag at once.
    (`--resume -- <id>` does NOT work: `--resume [value]` takes an OPTIONAL
    value, so `--` would end the options and the id would land as the prompt.)
    The uuid check is the second, independent layer.

    **Codex: `resume -- <id>`, LAST.** `codex resume` is a subcommand, and
    `build_inner` puts every other arg (the profile's `-m`, `--no-daemon`, a
    launch mode's `--approve-for-me`) before `extra_args`, so they ride as the
    ROOT command's flags, which codex merges into the resumed session (measured,
    codex-cli 0.170: `codex --no-daemon -m M --approve-for-me resume --help`
    parses). Codex is clap, where `--` does end the options and the id lands as
    the `SESSION_ID` positional, so the fence is `--` here, not `=`.
    """
    if not isinstance(agent_session_id, str) or not AGENT_SESSION_ID_RE.fullmatch(agent_session_id):
        return None
    if kind == "claude":
        return [f"--resume={agent_session_id}"]
    if kind == "codex":
        return ["resume", "--", agent_session_id]
    return None


def resolve_profiles_path() -> Path:
    env_path = os.environ.get("TLDRACK_PROFILES_PATH")
    return Path(env_path) if env_path else paths.config_file(PROFILES_FILE)


def expand_dir(raw: str, home: Path) -> str:
    """`~`-expand against an injectable home, then normalize. Pure."""
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    return os.path.normpath(raw)


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    kind: str
    #: Absolute. The transcript/usage root for `claude`, CODEX_HOME for `codex`,
    #: `~/.gemini` for `antigravity`, None for `shell`. Always a real path even
    #: when it is not pinned.
    config_dir: str | None
    #: True → the spawn sets the kind's config env var to `config_dir`; False →
    #: it UNSETS it (claude) or leaves it alone (codex). For Claude's default
    #: dir this is always False: its `.claude.json` sits in $HOME, not in
    #: `~/.claude`, so `CLAUDE_CONFIG_DIR=~/.claude` is a DIFFERENT, empty state.
    pin_env: bool
    args: tuple[str, ...] = ()
    model_label: str | None = None
    capacity: float = DEFAULT_CAPACITY

    def describe(self) -> dict:
        """Plain-dict view — what plugins and the wire get, never the class."""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "config_dir": self.config_dir,
            "pin_env": self.pin_env,
            "args": list(self.args),
            "model_label": self.model_label,
            "capacity": self.capacity,
        }


def parse_capacity(raw: object) -> float:
    """profiles.json `capacity` → a usable weight. Pure. Anything that is not a
    number in (0, CAPACITY_MAX] reads as the default — a typo costs the
    weighting, never the profile."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_CAPACITY
    value = float(raw)
    return value if 0 < value <= CAPACITY_MAX else DEFAULT_CAPACITY


def parse_profile(entry: object, home: Path) -> Profile | None:
    """One profiles.json entry → a Profile, or None when it is unusable. Pure.

    Tolerant per ENTRY: one bad row is dropped, never the whole file.
    """
    if not isinstance(entry, dict):
        return None
    profile_id = entry.get("id")
    kind = entry.get("kind")
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        return None
    if profile_id == AUTO_PROFILE_ID:
        return None
    if kind not in PROFILE_KINDS:
        return None
    raw_label = entry.get("label")
    label = raw_label.strip()[:LABEL_MAX] if isinstance(raw_label, str) and raw_label.strip() else profile_id[:LABEL_MAX].upper()
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    model_label = entry.get("model_label")
    if not (
        isinstance(model_label, str) and model_label.strip() and len(model_label) <= MODEL_LABEL_MAX and model_label.isprintable()
    ):
        model_label = None
    capacity = parse_capacity(entry.get("capacity", DEFAULT_CAPACITY))
    if kind == "shell":
        return Profile(profile_id, label, kind, None, False, (), model_label, capacity)
    raw_dir = entry.get("config_dir") or DEFAULT_CONFIG_DIRS[kind]
    if not isinstance(raw_dir, str):
        return None
    config_dir = expand_dir(raw_dir, home)
    if not os.path.isabs(config_dir):
        return None
    is_default_dir = os.path.realpath(config_dir) == os.path.realpath(expand_dir(DEFAULT_CONFIG_DIRS[kind], home))
    pin = entry.get("pin_env")
    pin_env = (not is_default_dir) if not isinstance(pin, bool) else pin
    if kind == "claude" and is_default_dir:
        pin_env = False  # see Profile.pin_env — never pin Claude's default dir
    if kind not in CONFIG_ENV:
        if not is_default_dir:
            return None  # see CONFIG_ENV — a second dir could not be honoured
        pin_env = False
    return Profile(profile_id, label, kind, config_dir, pin_env, tuple(args), model_label, capacity)


def detect_profiles(home: Path, codex_found: bool, antigravity_found: bool = False) -> list[Profile]:
    """The no-config answer: every account on disk, plus Codex, Antigravity
    (labelled GEMINI, the model family it is for) and a shell.

    Claude's default account is ALWAYS listed (whether or not it has signed
    in — `cli_found` and `hooks_ok` say the rest); every `~/.claude-<name>/`
    holding a `.claude.json` is a second account, which is exactly what the
    `CLAUDE_CONFIG_DIR=~/.claude-<name> claude` alias leaves behind.
    """
    profiles = [Profile("claude", "CLAUDE", "claude", expand_dir(DEFAULT_CONFIG_DIRS["claude"], home), False)]
    try:
        extra = sorted(home.glob(".claude-*"))
    except OSError:
        extra = []
    for directory in extra:
        profile_id = directory.name.lstrip(".").lower()
        if not PROFILE_ID_RE.fullmatch(profile_id):
            continue
        if not (directory / ".claude.json").is_file():
            continue
        suffix = profile_id[len("claude-") :] or profile_id
        profiles.append(Profile(profile_id, suffix[:LABEL_MAX].upper(), "claude", str(directory), True))
    if codex_found:
        profiles.append(Profile("codex", "CODEX", "codex", expand_dir(DEFAULT_CONFIG_DIRS["codex"], home), False))
    if antigravity_found:
        kind = antigravity_hooks.KIND
        profiles.append(Profile("gemini", "GEMINI", kind, expand_dir(DEFAULT_CONFIG_DIRS[kind], home), False))
    profiles.append(Profile("shell", "SHELL", "shell", None, False))
    return profiles


def choose_default(profiles: list[Profile], requested: object, env: Mapping[str, str], home: Path) -> str:
    """The profile `+ NEW` uses when the client names none. Pure.

    The file's `default` wins; otherwise the account this bridge itself runs
    on (its `CLAUDE_CONFIG_DIR`), otherwise the unpinned Claude, otherwise the
    first profile. The env only ever CHOOSES here — it never names a profile.
    """
    ids = [p.id for p in profiles]
    if isinstance(requested, str) and requested in ids:
        return requested
    claude = [p for p in profiles if p.kind == "claude"]
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        real = os.path.realpath(expand_dir(configured, home))
        for profile in claude:
            if profile.config_dir and os.path.realpath(profile.config_dir) == real:
                return profile.id
    unpinned = next((p for p in claude if not p.pin_env), None)
    if unpinned is not None:
        return unpinned.id
    return claude[0].id if claude else ids[0]


def resolve_cli(kind: str, home: Path, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Path to the kind's CLI, or None. PATH first, then the fixed install
    locations — see CLI_PROBE_DIRS."""
    if kind == "shell":
        return "/bin/zsh" if Path("/bin/zsh").exists() else which("zsh")
    name = CLI_NAMES.get(kind, kind)
    found = which(name)
    if found:
        return found
    for directory in CLI_PROBE_DIRS:
        candidate = Path(expand_dir(directory, home)) / name
        if candidate.is_file():
            return str(candidate)
    return None


def codex_has_no_daemon(help_text: object) -> bool:
    """Does this `codex --help` output offer `--no-daemon`? Pure."""
    return isinstance(help_text, str) and CODEX_NO_DAEMON_RE.search(help_text) is not None


def read_cli_help(cli_bin: str) -> str | None:
    """`<cli> --help`'s stdout, or None when it could not be read. Never raises."""
    try:
        result = subprocess.run(
            [cli_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=CODEX_HELP_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def build_inner(
    profile: Profile,
    extra_args: Iterable[str] = (),
    cli_bin: str | None = None,
    *,
    codex_no_daemon: bool = False,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """The command the spawned login shell runs — None for a plain shell. Pure.

    Every interpolated value is shlex-quoted. A caller that passes an argument
    which a CLI could read as a flag must fence it itself (`--resume=<id>`,
    attached: see `resume_args`).

    `spawn_argv` puts a launch mode's args (`permission_args`) at the HEAD of
    `extra_args`: after the profile's own args, before Claude's positional prompt.

    - claude: `env -u … [-u CLAUDE_CONFIG_DIR | CLAUDE_CONFIG_DIR=<dir>] [NAME=VALUE…] <bin>`.
      `env -u` options must precede its NAME=VALUE operands. `env` is the launch's
      own operands (`subagent_env`), Claude only; they follow the shed, so a name
      in both is set, not unset.
    - codex: BARE `codex`, never the binary path and never under `env` — the
      user's `codex` may be a shell FUNCTION (set up in their shell profile),
      which only the interactive shell running this can see. A
      pinned home rides as a prefix assignment, which zsh exports into a
      function call for its duration.
    - codex `--no-daemon` (`codex_no_daemon`, set by the registry when the CLI
      offers it). Codex 0.163+ hands every turn to a shared, auto-started
      `codex app-server` daemon, and RUNS HOOKS THERE, in the daemon's own
      environment, not in the pane. The relay resolves its session from
      `$TMUX`/`$TMUX_PANE`, so a daemon started outside tmux swallows every
      hook: the row reads `unknown` for its whole life and finishes on ◌,
      never ✅ (measured 2026-10-23, `age_seconds: null` across a full run).
      A daemon auto-started from a rack pane would be worse, posting EVERY
      Codex session's hooks to that one pane's session. `--no-daemon` keeps
      the app-server in this pane's process tree, so its hooks carry the
      pane's env. The cost: the thread is absent from `codex agents` /
      remote-control on the shared daemon. The rack supervises it instead.
    - antigravity: `<agy path>` — a native binary, run by path (its installer
      puts it in ~/.local/bin, which a login shell's PATH may lack). No config
      env: see CONFIG_ENV.
    """
    args = [*profile.args, *extra_args]
    tail = "".join(f" {shlex.quote(arg)}" for arg in args)
    if profile.kind == "claude":
        shed = " ".join(f"-u {name}" for name in SHED_ENV)
        if profile.pin_env and profile.config_dir:
            config = f"CLAUDE_CONFIG_DIR={shlex.quote(profile.config_dir)}"
        else:
            config = "-u CLAUDE_CONFIG_DIR"
        operands = "".join(f" {name}={shlex.quote(value)}" for name, value in (env or {}).items())
        return f"env {shed} {config}{operands} {shlex.quote(cli_bin or 'claude')}{tail}"
    if profile.kind == "codex":
        prefix = f"CODEX_HOME={shlex.quote(profile.config_dir)} " if profile.pin_env and profile.config_dir else ""
        flags = f" {CODEX_NO_DAEMON_FLAG}" if codex_no_daemon else ""
        return f"{prefix}codex{flags}{tail}"
    if profile.kind == antigravity_hooks.KIND:
        return f"{shlex.quote(cli_bin or antigravity_hooks.CLI_NAME)}{tail}"
    return None


#: The pane's own `cd`, run before the user's shell profile. tmux gives a new pane
#: its `-c` directory from inside the SERVER, and a server whose own start directory
#: has been deleted leaves every new pane in that dead directory instead (measured,
#: tmux 3.7b: `-c` is ignored by new-session, new-window and respawn-pane alike, and
#: recreating the directory does not bring it back). The server is born wherever
#: its first client stood and outlives the bridge, so a checkout that moves takes
#: every later session with it: 2026-11-17, `claude` refused to start in each new
#: pane ("The current directory no longer exists") after the bridge's old checkout
#: went away. So the pane changes directory itself.
#:
#: - `-f`: this hop reads no shell profile; the command after it does.
#: - A directory that vanished since the caller checked falls back to home, as
#:   tmux's own `-c` does.
#: - No command after the directory: the pane's login shell (`$SHELL` is tmux's
#:   `default-shell` inside a pane), which is what tmux runs when given none.
PANE_CD_SCRIPT = 'cd -- "$1" 2>/dev/null || cd; shift; [[ -n "$1" ]] && exec "$@"; exec "${SHELL:-/bin/zsh}" -l'


def directory_args(cwd: str | os.PathLike[str], command: Iterable[str] = ()) -> list[str]:
    """The tail of a `tmux new-session` that opens its pane in `cwd`: `-c CWD` plus
    the pane command that changes into it (`PANE_CD_SCRIPT` owns why both). Pure.

    `command` is the argv the pane runs once it is there; none means a login shell.
    tmux splits a command sequence on any argv element ENDING in `;`, so a cwd
    ending in one gets a trailing separator; no element of the wrapper ends in one.
    """
    cwd_arg = str(cwd)
    if cwd_arg.endswith(";"):
        cwd_arg += os.sep
    return ["-c", cwd_arg, "/bin/zsh", "-f", "-c", PANE_CD_SCRIPT, "zsh", cwd_arg, *command]


def spawn_argv(
    profile: Profile,
    session: str,
    cwd: str | os.PathLike[str],
    extra_args: Iterable[str] = (),
    cli_bin: str | None = None,
    *,
    codex_no_daemon: bool = False,
    permission_mode: str | None = None,
    model: str | None = None,
    subagent_model: str | None = None,
) -> list[str]:
    """THE spawn shape, for every door that starts an agent. Pure.

    `tmux new-session -d -s S -c CWD <cd CWD> /bin/zsh -l -i -c '<inner>; …' ";"
    set-option -t =S: @vc_profile ID` — ONE invocation, so the session is
    stamped before any poll can list it. A `permission_mode` that gave the CLI
    args is stamped the same way (`PERMISSION_MODE_OPTION`); one that gave none
    (a shell) is not, so the stamp only ever records what the spawn applied.
    A launch `model` / `subagent_model` (§ LAUNCH MODELS) follows the same rule.

    - `/bin/zsh -l -i`: login + interactive, so the user's PATH and functions
      (`codex`) resolve, and an exotic-PATH failure surfaces INSIDE the
      visible pane rather than in a 500 nobody reads.
    - The trailing `exec /bin/zsh -l` keeps the pane alive after the agent
      exits; the echo before it is the only place that exit status is shown.
    - `=S:` with the colon: `set-option -t` parses a target PANE, which
      accepts the exact-match `=` only in `=session:` form (measured, tmux
      3.7b — bare `=S` answers "no such session").
    - `<cd CWD>`: the pane changes into CWD itself, and a plain shell (no
      `inner`) is the login shell that hop ends in: owned by `PANE_CD_SCRIPT` —
      see the note there. `inner` never ends in `;` (`directory_args`).
    """
    applied = permission_args(profile.kind, permission_mode)
    model_applied = model_args(profile.kind, model)
    env = subagent_env(profile.kind, subagent_model)
    inner = build_inner(profile, [*model_applied, *applied, *extra_args], cli_bin, codex_no_daemon=codex_no_daemon, env=env)
    command = (
        ["/bin/zsh", "-l", "-i", "-c", f'{inner}; echo; echo "[{profile.id} exited $?]"; exec /bin/zsh -l']
        if inner is not None
        else []
    )
    argv = ["tmux", "new-session", "-d", "-s", session, *directory_args(cwd, command)]
    argv += [";", "set-option", "-t", f"={session}:", PROFILE_OPTION, profile.id]
    if applied:
        argv += [";", "set-option", "-t", f"={session}:", PERMISSION_MODE_OPTION, permission_mode]
    if model_applied:
        argv += [";", "set-option", "-t", f"={session}:", MODEL_OPTION, model]
    if env:
        argv += [";", "set-option", "-t", f"={session}:", SUBAGENT_MODEL_OPTION, subagent_model]
    return argv


def apply_profile_env(profile: Profile, base: Mapping[str, str]) -> dict[str, str]:
    """`base` with the profile's config env applied — for a child process the
    bridge runs ITSELF (the worktree bootstrap), which must land the memory
    symlink under the right account's `projects/`."""
    env = dict(base)
    var = CONFIG_ENV.get(profile.kind)
    if var is None:
        return env
    if profile.pin_env and profile.config_dir:
        env[var] = profile.config_dir
    elif profile.kind == "claude":
        env.pop(var, None)
    return env


def is_relay_entry(entry: object, status_port: int | None = None) -> bool:
    """Is this hook entry the bridge's relay — the `command` (curl) form or the
    `http` form — to `status_port`, or to any port when it is None? Pure."""
    if not isinstance(entry, dict):
        return False
    host = f"127.0.0.1:{status_port}" if status_port is not None else "127.0.0.1:"
    command = entry.get("command")
    if isinstance(command, str) and RELAY_MARKER in command:
        return status_port is None or f"{host}{RELAY_MARKER}" in command
    url = entry.get("url")
    return entry.get("type") == "http" and isinstance(url, str) and url.startswith(f"http://{host}") and HTTP_RELAY_PATH in url


def _relay_events(document: object, status_port: int | None, transport: str | None = None) -> set[str]:
    """Event names whose hook groups carry a relay (`is_relay_entry`) — of one
    `transport` (`command` | `http`) when given."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    found: set[str] = set()
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else ():
            entries = group.get("hooks") if isinstance(group, dict) else None
            for entry in entries if isinstance(entries, list) else ():
                if is_relay_entry(entry, status_port) and (
                    transport is None or (entry.get("type") == "http") == (transport == "http")
                ):
                    found.add(event)
    return found


def relay_marker(status_port: int) -> str:
    return f"127.0.0.1:{status_port}{RELAY_MARKER}"


def hooks_missing(kind: str, document: object, status_port: int, events: Iterable[str]) -> list[str]:
    """What an install would add to this hooks document, as short readouts:
    each event not relayed to this bridge (in `events` order), then (claude)
    `statusLine` and `env:<KEY>` for each CLAUDE_SETTINGS_ENV key absent. Empty
    means current. A key the user set to another value counts as present —
    the install keeps theirs, so naming it here would nag forever. Pure."""
    relayed = _relay_events(document, status_port)
    missing = [event for event in events if event not in relayed]
    if kind == antigravity_hooks.KIND:
        relayed = antigravity_hooks.relay_events(document, lambda e: is_relay_entry(e, status_port))
        return [event for event in events if event not in relayed]
    if kind == "claude" and _relay_events(document, status_port, "command"):
        # Still the curl relay (three forks per event): an install moves it to
        # `http` (main.hook_http_entry owns why).
        missing.append("transport:http")
    if kind == "claude":
        status_line = document.get("statusLine") if isinstance(document, dict) else None
        command = status_line.get("command") if isinstance(status_line, dict) else None
        if not (isinstance(command, str) and STATUSLINE_MARKER in command):
            missing.append("statusLine")
        env = document.get("env") if isinstance(document, dict) else None
        missing.extend(f"env:{key}" for key in CLAUDE_SETTINGS_ENV if not (isinstance(env, dict) and key in env))
    return missing


def hooks_status(kind: str, document: object, status_port: int, events: Iterable[str]) -> bool:
    """Does this hooks document relay every `events` event to this bridge,
    and (claude) forward its statusline and carry its env? Pure."""
    return not hooks_missing(kind, document, status_port, events)


def merge_hooks(
    document: dict, hooks_block: dict, status_line: dict | None, status_port: int, env: Mapping[str, str] | None = None
) -> tuple[dict, list[str]]:
    """Merge the bridge's relay into a hooks document. Pure; returns
    `(merged, notes)` and never mutates `document`.

    A group is OURS when one of its commands carries the relay marker (any
    port — an install after a port change replaces the stale relay). Ours are
    replaced, everyone else's are kept in order. A `statusLine` that is not
    the bridge's is the user's own and is LEFT ALONE, with a note — usage then
    does not flow, and the user is the one to decide that trade. `env` keys
    are added only where absent, by the same rule: a value the user set wins.
    """
    merged = json.loads(json.dumps(document))  # deep copy, JSON-shaped
    notes: list[str] = []
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, groups in hooks_block.items():
        kept = [
            g
            for g in (hooks.get(event) if isinstance(hooks.get(event), list) else [])
            if not _relay_events({"hooks": {event: [g]}}, None)
        ]
        hooks[event] = kept + groups
    merged["hooks"] = hooks
    if status_line is not None:
        current = merged.get("statusLine")
        command = current.get("command") if isinstance(current, dict) else None
        if current is None or (isinstance(command, str) and STATUSLINE_MARKER in command):
            merged["statusLine"] = status_line
        else:
            notes.append("kept your own statusLine — usage for this account will not reach the bridge")
    if env:
        current_env = merged.get("env")
        current_env = dict(current_env) if isinstance(current_env, dict) else {}
        for key, value in env.items():
            if key not in current_env:
                current_env[key] = value
            elif current_env[key] != value:
                notes.append(f"kept your own {key}={current_env[key]!r}")
        merged["env"] = current_env
    return merged, notes


def install_hooks(
    profile: Profile, hooks_block: dict, status_line: dict | None, status_port: int, *, now: Callable[[], float] = time.time
) -> tuple[Path, bool, Path | None, list[str]]:
    """Merge the relay into the profile's hooks file. Idempotent.

    Returns `(path, changed, backup, notes)`. A file that exists but is not a
    JSON object RAISES rather than being replaced: it is the user's config,
    and an unparseable one is a thing to tell them about, not to overwrite.
    The original is copied to `<name>.bak-<epoch>` before the write, and the
    write is re-read afterwards — `atomic_write` reports a failure and keeps
    going, which is right for a request thread and wrong for a CLI that is
    about to print "installed".
    """
    if profile.config_dir is None or profile.kind not in HOOKS_FILES:
        raise ValueError(f"profile '{profile.id}' ({profile.kind}) has no hooks file")
    path = Path(profile.config_dir) / HOOKS_FILES[profile.kind]
    if path.is_symlink():
        # One file shared by two accounts (`~/.claude-c/settings.json` → `~/.claude/`): write THROUGH
        # the link. `atomic_write`'s `os.replace` would swap the link for a copy, and the two would drift.
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as err:
            raise ValueError(f"{path} is a broken symlink") from err
    try:
        original = path.read_text()
    except FileNotFoundError:
        original = None
    document: dict = {}
    if original is not None and original.strip():
        parsed = json.loads(original)  # ValueError → the caller reports it
        if not isinstance(parsed, dict):
            raise ValueError(f"{path} is not a JSON object")
        document = parsed
    claude = profile.kind == "claude"
    if profile.kind == antigravity_hooks.KIND:
        # agy's file is keyed by hook NAME, and the bridge owns one: antigravity_hooks.py.
        merged, notes = antigravity_hooks.merge(document, hooks_block), []
    else:
        merged, notes = merge_hooks(
            document, hooks_block, status_line if claude else None, status_port, CLAUDE_SETTINGS_ENV if claude else None
        )
    if merged == document and original is not None:
        return path, False, None, notes
    backup = None
    if original is not None:
        backup = path.with_name(f"{path.name}.bak-{int(now())}")
        # Owner-only whatever the umask: an agent's settings file can hold tokens under `env`.
        backup.touch(mode=0o600, exist_ok=True)
        os.chmod(backup, 0o600)
        backup.write_text(original)
    text = json.dumps(merged, indent=2) + "\n"
    atomic_write(path, text, f"{profile.id} hooks")
    if path.read_text() != text:
        raise OSError(f"could not write {path}")
    return path, True, backup, notes


def auth_probe(profile: Profile, cli_bin: str | None) -> tuple[bool | None, dict | None]:
    """`claude auth status` under the profile's HEADLESS env (the one a
    `headless_call` runs with, so the answer is about that call's login):
    `(signed_in, details)`. `signed_in` is None when unknown (not Claude, no
    CLI, or the probe failed); `details` is `headless.parse_auth_status`'s
    dict, or None when the CLI printed no JSON."""
    if profile.kind != "claude" or cli_bin is None:
        return None, None
    env = headless.headless_env(apply_profile_env(profile, os.environ), SHED_ENV)
    try:
        result = subprocess.run(
            [cli_bin, "auth", "status"], capture_output=True, text=True, timeout=AUTH_TIMEOUT_SECONDS, check=False, env=env
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    return result.returncode == 0, headless.parse_auth_status(result.stdout)


def auth_status(profile: Profile, cli_bin: str | None) -> bool | None:
    """`claude auth status` under the profile's env: True signed in, False
    not, None unknown (not Claude, no CLI, or the probe failed)."""
    return auth_probe(profile, cli_bin)[0]


class ProfileRegistry:
    """The loaded profiles plus everything the bridge asks of them.

    Plugins receive this through `PluginContext.profiles` and must treat it as
    duck-typed: every method a plugin calls returns plain dicts, lists and
    strings (`describe`, `default_id`, `spawn_argv`, `claude_roots`,
    `child_env`, `cli_found`, `kind_of`, `headless_call`, `auth_info`,
    `resume_args`, `pick_launch`, `permission_modes`, `permission_refusal`,
    `model_refusal`).
    """

    def __init__(
        self,
        profiles: list[Profile],
        default_id: str,
        *,
        home: Path,
        source: str,
        which: Callable[[str], str | None] = shutil.which,
        cli_help: Callable[[str], str | None] = read_cli_help,
        cli_version: Callable[[str], str | None] = read_cli_version,
    ) -> None:
        self._profiles = list(profiles)
        self._by_id = {p.id: p for p in self._profiles}
        self.availability = ProfileAvailability(list(self._by_id))
        self.default_id = default_id
        self.source = source  # "file" | "detected" — for the startup echo
        self._home = home
        self._which = which
        self._cli_help = cli_help
        self._cli_version = cli_version
        self._version_cache: dict[tuple[str, int], str | None] = {}
        self._lock = threading.Lock()
        self._hooks_cache: dict[str, tuple[int, object]] = {}
        self._cli_cache: dict[str, tuple[float, str | None]] = {}
        self._help_cache: dict[tuple[str, int], str | None] = {}
        self._auth_cache: dict[str, tuple[float, dict]] = {}
        self._codex_usage = CodexUsageReader()
        self._launch_picker: Callable[[], dict | None] | None = None

    # -- lookup ---------------------------------------------------------------
    def ids(self) -> list[str]:
        return [p.id for p in self._profiles]

    def get(self, profile_id: object) -> Profile | None:
        return self._by_id.get(profile_id) if isinstance(profile_id, str) else None

    def resolve(self, profile_id: object) -> Profile | None:
        """The named profile, the default when none is named, None when the
        name is unknown — a typo must refuse, never fall back silently."""
        if profile_id is None or profile_id == "":
            return self._by_id.get(self.default_id)
        return self.get(profile_id)

    def describe(self, profile_id: object = None) -> dict | None:
        profile = self.resolve(profile_id)
        return profile.describe() if profile else None

    def kind_of(self, profile_id: object) -> str | None:
        profile = self.resolve(profile_id)
        return profile.kind if profile else None

    def launch_refusal(self, profile_id: object) -> str | None:
        profile = self.resolve(profile_id)
        if profile is not None and self.availability.paused(profile.id):
            return f"Profile '{profile.label}' is paused; resume it in Launch profiles"
        return None

    # -- AUTO -----------------------------------------------------------------
    def capacities(self) -> dict[str, float]:
        return {p.id: p.capacity for p in self._profiles}

    def set_launch_picker(self, picker: Callable[[], dict | None]) -> None:
        """The host's live `launch_pick` block (main.py builds it from the same
        rows `/snapshot` serves). Injected because the registry knows the
        accounts but not the sessions."""
        self._launch_picker = picker

    def pick_launch(self, model: object = None, subagent_model: object = None, permission_mode: object = None) -> str | None:
        """The profile id `"auto"` means right now, or None when nothing is
        launchable (or this host never set a picker). The scoring is owned by
        `launch_pick.py`; plugins reach it through here.

        A launch that asks for a model or a permission mode gets the freest
        account that can START it: the pick's ranking is walked in order, past
        every profile that would refuse (a Codex account takes no launch model,
        so before this walk an `auto` worker answered 400 most of the
        time). When none can, the plain pick is returned and the spawn refuses
        with that profile's own reason."""
        if self._launch_picker is None:
            return None
        pick = self._launch_picker()
        chosen = pick.get("id") if isinstance(pick, dict) else None
        if not isinstance(chosen, str) or chosen not in self._by_id or self.launch_refusal(chosen):
            return None
        if model is None and subagent_model is None and permission_mode is None:
            return chosen
        ranking = pick.get("ranking") if isinstance(pick.get("ranking"), list) else []
        rows = [row for row in ranking if isinstance(row, dict) and row.get("id") in self._by_id]
        ordered = [chosen, *(row["id"] for row in rows if not row.get("spent")), *(row["id"] for row in rows if row.get("spent"))]
        for candidate in dict.fromkeys(ordered):
            if self.launch_refusal(candidate):
                continue
            if self.model_refusal(candidate, model, subagent_model) is not None:
                continue
            if permission_mode is not None and self.permission_refusal(candidate, permission_mode) is not None:
                continue
            return candidate
        return chosen

    # -- roots ----------------------------------------------------------------
    def claude_roots(self) -> list[str]:
        """Realpaths of every Claude profile's config dir, deduplicated —
        the transcript allowlist and the coordinator's scan roots."""
        roots: list[str] = []
        for profile in self._profiles:
            if profile.kind == "claude" and profile.config_dir:
                real = os.path.realpath(profile.config_dir)
                if real not in roots:
                    roots.append(real)
        return roots

    def profile_for_path(self, path: object, *, kind: str | None = None, first_on_tie: bool = False) -> str | None:
        """The profile whose config dir holds `path`, by LONGEST root, with a
        separator-aware prefix test — `.claude` must never claim a file under
        `.claude-a`. None when no root matches, or when the deepest root
        is shared by two profiles (two Codex profiles on one CODEX_HOME): the
        path then cannot say which, and the spawn stamp is the better answer.

        `first_on_tie` is for a caller that only needs the ACCOUNT (a resume):
        two profiles on one config dir are one account, so the first in config
        order serves. `kind` narrows the candidates.

        Only realpaths the string; never opens it.
        """
        if not isinstance(path, str) or not path:
            return None
        real = os.path.realpath(os.path.expanduser(path))
        best_len = -1
        best: list[str] = []
        for profile in self._profiles:
            if not profile.config_dir or (kind is not None and profile.kind != kind):
                continue
            root = os.path.realpath(profile.config_dir)
            if real != root and not real.startswith(root + os.sep):
                continue
            if len(root) > best_len:
                best_len, best = len(root), [profile.id]
            elif len(root) == best_len:
                best.append(profile.id)
        return best[0] if len(best) == 1 or (best and first_on_tie) else None

    def default_of_kind(self, kind: str) -> str | None:
        """The default profile when it is of `kind`, else the first that is."""
        if self.kind_of(self.default_id) == kind:
            return self.default_id
        return next((p.id for p in self._profiles if p.kind == kind), None)

    # -- spawning -------------------------------------------------------------
    def cli_path(self, profile_id: object) -> str | None:
        """The profile's CLI, cached per kind for CLI_CACHE_SECONDS."""
        profile = self.resolve(profile_id)
        if profile is None:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._cli_cache.get(profile.kind)
        if cached is not None and now - cached[0] < CLI_CACHE_SECONDS:
            return cached[1]
        found = resolve_cli(profile.kind, self._home, self._which)
        with self._lock:
            self._cli_cache[profile.kind] = (now, found)
        return found

    def cli_found(self, profile_id: object) -> bool:
        return self.cli_path(profile_id) is not None

    def spawn_argv(
        self,
        profile_id: object,
        session: str,
        cwd: str | os.PathLike[str],
        extra_args: Iterable[str] = (),
        cli_bin: str | None = None,
        *,
        lineage: dict | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        subagent_model: str | None = None,
    ) -> list[str]:
        """`spawn_argv` for a named profile. `cli_bin` lets a plugin keep its
        own binary override (`WORKTREE_CLAUDE_BIN`); it applies to Claude only.
        Raises KeyError for an unknown profile and ValueError for a
        `permission_mode` this profile cannot start in — ask `permission_refusal`
        first and refuse with its reason. The same for `model` / `subagent_model`
        and `model_refusal`."""
        profile = self.resolve(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        refused = self.launch_refusal(profile.id)
        if refused:
            raise ValueError(refused)
        binary = cli_bin if profile.kind == "claude" else None
        if profile.kind in ("claude", antigravity_hooks.KIND) and binary is None:
            binary = self.cli_path(profile.id)
        if permission_mode is not None:
            refused = self.permission_refusal(profile.id, permission_mode, cli_bin=binary)
            if refused is not None:
                raise ValueError(refused)
        if model is not None or subagent_model is not None:
            refused = self.model_refusal(profile.id, model, subagent_model, cli_bin=binary)
            if refused is not None:
                raise ValueError(refused)
        argv = spawn_argv(
            profile,
            session,
            cwd,
            extra_args,
            binary,
            codex_no_daemon=profile.kind == "codex" and self.codex_no_daemon(profile.id),
            permission_mode=permission_mode,
            model=model,
            subagent_model=subagent_model,
        )
        return argv + lineage_args(session, lineage or prepare_lineage())

    def permission_modes(self) -> list[str]:
        """The launch modes this HOST can start — the capability a client checks
        before it asks (`/create`'s `capabilities.permission_modes`). Whether one
        PROFILE can, right now, is `permission_refusal`'s."""
        return list(PERMISSION_MODES)

    def permission_refusal(self, profile_id: object, mode: object, cli_bin: str | None = None) -> str | None:
        """Why `profile_id` cannot start in `mode` now, or None when it can — the
        one gate before any spawn asks for a mode (worktree `/create`, a restore).

        Refused: an unknown mode or profile; a kind with no row in
        `PERMISSION_ARGS` (a shell); a profile that pins its own approvals
        (`pinned_permission_flag`); a CLI whose `--help` does not offer the flag
        (`cli_offers_permission`), or that could not be read. `cli_bin` keeps a
        plugin's Claude override (`WORKTREE_CLAUDE_BIN`), so the binary probed is
        the binary that will run."""
        if clean_permission_mode(mode) is None:
            return f"unknown permission mode {str(mode)[:40]!r}"
        profile = self.resolve(profile_id)
        if profile is None:
            return f"unknown profile {str(profile_id)[:40]!r}"
        wanted = PERMISSION_ARGS[mode].get(profile.kind)
        if not wanted:
            return f"a {profile.kind} profile has no '{mode}' mode"
        pinned = pinned_permission_flag(profile)
        if pinned is not None:
            return f"profile '{profile.id}' pins its own approvals ({pinned} in its args)"
        binary = (cli_bin if profile.kind == "claude" and cli_bin else None) or self.cli_path(profile.id)
        if binary is None:
            return f"no {profile.kind} CLI found"
        help_text = self._help_of(binary)
        if help_text is None:
            return f"could not read `{profile.kind} --help` to confirm {' '.join(wanted)}"
        if not cli_offers_permission(profile.kind, mode, help_text):
            return f"this {profile.kind} CLI does not offer {' '.join(wanted)} — upgrade it"
        return None

    def model_refusal(
        self, profile_id: object, model: object = None, subagent_model: object = None, cli_bin: str | None = None
    ) -> str | None:
        """Why `profile_id` cannot launch on `model` with its subagents forced to
        `subagent_model`, or None when it can — the one gate before any spawn names
        either (§ LAUNCH MODELS). Naming neither is always fine.

        Refused: a model that names no family; an unknown profile; a kind other
        than claude; a profile that pins its own model while the launch names one;
        **a `subagent_model` that is not strictly weaker than the agent's model**
        (the launch's, else the profile's pin; unchecked when neither is known);
        and, for a `subagent_model`, a CLI too old to force it or whose version
        could not be read."""
        if model is None and subagent_model is None:
            return None
        for label, value in (("launch", model), ("subagent", subagent_model)):
            if value is not None and clean_launch_model(value) is None:
                return f"unknown {label} model {str(value)[:40]!r}"
        profile = self.resolve(profile_id)
        if profile is None:
            return f"unknown profile {str(profile_id)[:40]!r}"
        if profile.kind != "claude":
            return f"a {profile.kind} profile takes no launch model (its model is its own args)"
        pinned = pinned_model_flag(profile)
        if model is not None and pinned is not None:
            return f"profile '{profile.id}' pins its own model ({pinned} in its args)"
        if subagent_model is None:
            return None
        own = model if model is not None else pinned_model(profile)
        if own is not None and model_tier(subagent_model) >= model_tier(own):
            return f"subagent model '{subagent_model}' is not weaker than '{own}'"
        binary = cli_bin or self.cli_path(profile.id)
        if binary is None:
            return "no claude CLI found"
        version = parse_cli_version(self._version_of(binary))
        needed = ".".join(map(str, SUBAGENT_FORCE_MIN_VERSION))
        if version is None:
            return f"could not read `claude --version` to confirm it forces a subagent model ({needed}+)"
        if version < SUBAGENT_FORCE_MIN_VERSION:
            return f"this claude CLI ({'.'.join(map(str, version))}) cannot force a subagent model; {needed}+ can"
        return None

    def _version_of(self, cli: str) -> str | None:
        """`<cli> --version`, cached like `_help_of`."""
        return self._probe(cli, self._version_cache, self._cli_version)

    def _help_of(self, cli: str) -> str | None:
        """`<cli> --help`, cached by the binary's realpath + mtime so an upgrade
        re-probes and every other call is a stat. None (unreadable) is cached too:
        a wedged binary must not cost every spawn its timeout."""
        return self._probe(cli, self._help_cache, self._cli_help)

    def _probe(self, cli: str, cache: dict[tuple[str, int], str | None], read: Callable[[str], str | None]) -> str | None:
        real = os.path.realpath(cli)
        try:
            key = (real, os.stat(real).st_mtime_ns)
        except OSError:
            return None
        with self._lock:
            if key in cache:
                return cache[key]
        text = read(real)
        with self._lock:
            cache[key] = text
        return text

    def prepare_lineage(self, parent_session: object = None) -> dict:
        return prepare_lineage(parent_session)

    def codex_no_daemon(self, profile_id: object) -> bool:
        """Should a spawn of this Codex profile pass `--no-daemon`? Exactly when
        its CLI offers the flag, since a CLI without one has no daemon to leave
        and would refuse the unknown option. `build_inner` owns why.

        Probes the BINARY's `--help`, never a shell function of that name (no secret
        is read), cached by realpath + mtime so an upgrade re-probes. A failed
        probe answers False, which spawns exactly what an older bridge did."""
        if self.kind_of(profile_id) != "codex":
            return False
        cli = self.cli_path(profile_id)
        return cli is not None and codex_has_no_daemon(self._help_of(cli))

    def resume_args(self, kind: object, agent_session_id: object) -> list[str] | None:
        """`resume_args` — exposed on the registry so a plugin, which cannot
        import this module, reaches the same fence (coordinator RESUME)."""
        return resume_args(kind, agent_session_id)

    def child_env(self, profile_id: object, base: Mapping[str, str] | None = None) -> dict[str, str]:
        profile = self.resolve(profile_id)
        source = os.environ if base is None else base
        return apply_profile_env(profile, source) if profile else dict(source)

    # -- headless calls (tldrack/headless.py owns the recipe) ---------
    def headless_call(
        self,
        profile_id: object,
        *,
        system_prompt: str,
        model: str,
        budget_usd: float,
        json_schema: Mapping[str, object] | None = None,
        stream: bool = False,
        max_output_tokens: int | None = None,
        cli_bin: str | None = None,
        base: Mapping[str, str] | None = None,
    ) -> dict | None:
        """`{"profile", "argv", "env"}` for one `claude -p` on the named (or
        default) Claude profile's own login, or None when that profile is not
        Claude or its CLI is missing. The caller runs it with the prompt on
        stdin, from a scratch cwd. `cli_bin` keeps a plugin's own override
        (`WORKTREE_CLAUDE_BIN`), as `spawn_argv` does."""
        profile = self.resolve(profile_id)
        if profile is None or profile.kind != "claude":
            return None
        binary = cli_bin or self.cli_path(profile.id)
        if binary is None:
            return None
        source = os.environ if base is None else base
        return {
            "profile": profile.id,
            "argv": headless.build_argv(
                binary, system_prompt=system_prompt, model=model, budget_usd=budget_usd, json_schema=json_schema, stream=stream
            ),
            "env": headless.headless_env(apply_profile_env(profile, source), SHED_ENV, max_output_tokens=max_output_tokens),
        }

    def auth_info(self, profile_id: object, *, fresh: bool = False) -> dict | None:
        """Who a headless call on this profile bills: `{"profile", "signed_in",
        "auth_method", "api_provider", "subscription", "billing"}` with
        `billing` in `subscription | api | unknown` (`headless.billing_of`), or
        None for a profile that is not Claude. Cached AUTH_CACHE_SECONDS;
        `fresh` skips the cache."""
        profile = self.resolve(profile_id)
        if profile is None or profile.kind != "claude":
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._auth_cache.get(profile.id)
        if not fresh and cached is not None and now - cached[0] < AUTH_CACHE_SECONDS:
            return dict(cached[1])
        signed_in, details = auth_probe(profile, self.cli_path(profile.id))
        info = {
            "profile": profile.id,
            "signed_in": signed_in,
            "auth_method": (details or {}).get("auth_method"),
            "api_provider": (details or {}).get("api_provider"),
            "subscription": (details or {}).get("subscription"),
            "billing": headless.billing_of(details) if signed_in else "unknown",
        }
        with self._lock:
            self._auth_cache[profile.id] = (now, info)
        return dict(info)

    # -- hooks ----------------------------------------------------------------
    def hooks_ok(self, profile: Profile, status_port: int, events: Mapping[str, Iterable[str]]) -> bool | None:
        """Whether this profile's hooks relay to this bridge — None for a shell."""
        missing = self.hooks_missing(profile, status_port, events)
        return None if missing is None else not missing

    def hooks_missing(self, profile: Profile, status_port: int, events: Mapping[str, Iterable[str]]) -> list[str] | None:
        """`hooks_missing` for this profile's file — None for a shell, `["file"]`
        when it does not exist. Cached by the file's mtime: `/snapshot` asks on
        every poll."""
        if profile.kind not in HOOKS_FILES or profile.config_dir is None:
            return None
        path = Path(profile.config_dir) / HOOKS_FILES[profile.kind]
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return ["file"]
        with self._lock:
            cached = self._hooks_cache.get(str(path))
        if cached is not None and cached[0] == stamp:
            document = cached[1]
        else:
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError):
                document = None
            with self._lock:
                self._hooks_cache[str(path)] = (stamp, document)
        return hooks_missing(profile.kind, document, status_port, events.get(profile.kind, ()))

    # -- wire -----------------------------------------------------------------
    def summary(
        self,
        status_port: int,
        events: Mapping[str, Iterable[str]],
        usage: Mapping[str, dict] | None = None,
        standing: Mapping[str, dict] | None = None,
    ) -> list[dict]:
        """The `/snapshot` `profiles` list, in config order."""
        return [
            {
                "id": profile.id,
                "label": profile.label,
                "kind": profile.kind,
                "default": profile.id == self.default_id,
                "paused": self.availability.paused(profile.id),
                "hooks_ok": None if (missing := self.hooks_missing(profile, status_port, events)) is None else not missing,
                # What INSTALL would add (`hooks_missing`), for the OPS row's readout.
                "hooks_missing": missing,
                "cli_found": self.cli_found(profile.id),
                "model_label": profile.model_label,
                "usage": (
                    self._codex_usage.get(profile.config_dir) if profile.kind == "codex" else (usage or {}).get(profile.id)
                ),
                # Additive: `{"state": "out", …}` while the account cannot run a turn at all, else
                # null (account_standing.py). `usage` above may still hold its last windows.
                "standing": (standing or {}).get(profile.id),
            }
            for profile in self._profiles
        ]


def load_profiles(
    path: Path | None = None,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    log: Callable[[str], None] | None = None,
) -> ProfileRegistry:
    """The registry this bridge runs with. Never raises: an absent, corrupt or
    empty file falls back to detection with a warning, so a typo in
    profiles.json costs the custom list, never the bridge."""
    home = home or Path.home()
    env = os.environ if env is None else env
    path = path or resolve_profiles_path()
    warn = log or (lambda _msg: None)
    profiles: list[Profile] = []
    requested: object = None
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = None
    except OSError as err:
        warn(f"tldrack: could not read {path} ({err}) — detecting profiles")
        text = None
    if text is not None:
        try:
            data = json.loads(text)
        except ValueError as err:
            warn(f"tldrack: corrupt profiles file {path} ({err}) — detecting profiles")
            data = None
        entries = data.get("profiles") if isinstance(data, dict) else None
        if isinstance(entries, list):
            seen: set[str] = set()
            for entry in entries:
                profile = parse_profile(entry, home)
                if profile is None:
                    warn(f"tldrack: skipping unusable profile entry {entry!r:.80}")
                    continue
                if profile.id in seen:
                    continue  # duplicate id: first wins, like plugin names
                seen.add(profile.id)
                profiles.append(profile)
            requested = data.get("default")
        elif data is not None:
            warn(f'tldrack: {path} has no "profiles" list — detecting profiles')
    source = "file" if profiles else "detected"
    if not profiles:
        profiles = detect_profiles(
            home, resolve_cli("codex", home, which) is not None, resolve_cli(antigravity_hooks.KIND, home, which) is not None
        )
    if not any(p.kind == "shell" for p in profiles):
        # The bare terminal `+ NEW` always made, kept reachable whatever the file says.
        profiles.append(Profile("shell", "SHELL", "shell", None, False))
    return ProfileRegistry(profiles, choose_default(profiles, requested, env, home), home=home, source=source, which=which)
