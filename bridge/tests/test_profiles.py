"""Launch profiles (tldrack/profiles.py).

The load-bearing guarantees: Claude's DEFAULT account is spawned with
`CLAUDE_CONFIG_DIR` UNSET (its `.claude.json` lives in $HOME, so pointing the
var at `~/.claude` is a different, empty state); credentials that would outrank
a pinned account are shed; Codex runs as a bare `codex` (it may be a shell function, never
under `env`) and off the shared daemon when its CLI can be; the session is
stamped in the SAME tmux invocation; and a path
under `~/.claude-a` is never claimed by `~/.claude`.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tldrack.profiles import (
    CLAUDE_SETTINGS_ENV,
    PANE_CD_SCRIPT,
    PERMISSION_MODE_OPTION,
    PERMISSION_MODES,
    PROFILE_OPTION,
    SHED_ENV,
    Profile,
    ProfileRegistry,
    apply_profile_env,
    build_inner,
    choose_default,
    clean_permission_mode,
    cli_offers_permission,
    codex_has_no_daemon,
    detect_profiles,
    directory_args,
    hooks_missing,
    hooks_status,
    install_hooks,
    load_profiles,
    merge_hooks,
    parse_profile,
    permission_args,
    pinned_permission_flag,
    relay_marker,
    spawn_argv,
)

HOME = Path("/Users/someone")


def _registry(profiles, default=None, home=HOME):
    return ProfileRegistry(profiles, default or profiles[0].id, home=home, source="file", which=lambda _name: None)


# ── parse_profile ────────────────────────────────────────────────────────


def test_claude_default_dir_is_never_pinned_even_when_asked():
    profile = parse_profile({"id": "b", "kind": "claude", "config_dir": "~/.claude", "pin_env": True}, HOME)
    assert profile.config_dir == "/Users/someone/.claude"
    assert profile.pin_env is False


def test_a_second_account_dir_is_pinned_by_default():
    profile = parse_profile({"id": "claude-a", "kind": "claude", "config_dir": "~/.claude-a"}, HOME)
    assert profile.pin_env is True
    assert profile.config_dir == "/Users/someone/.claude-a"


def test_label_defaults_to_the_id_and_is_capped():
    profile = parse_profile({"id": "a-very-long-profile-id", "kind": "shell"}, HOME)
    assert profile.label == "A-VERY-LON"


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "Bad Id", "kind": "claude"},
        {"id": "ok", "kind": "gemini"},
        {"id": "ok", "kind": "claude", "args": "-m x"},
        {"id": "ok", "kind": "claude", "config_dir": "relative/dir"},
        {"id": "auto", "kind": "claude"},  # reserved: what a client sends for the AUTO pick
        "not a dict",
    ],
)
def test_unusable_entries_are_dropped(entry):
    assert parse_profile(entry, HOME) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.5, 0.5),
        (2, 2.0),
        (None, 1.0),
        ("0.5", 1.0),
        (0, 1.0),
        (-1, 1.0),
        (11, 1.0),
        (True, 1.0),
    ],
)
def test_capacity_weighs_a_plan_and_a_bad_value_reads_as_standard(raw, expected):
    entry = {"id": "claude-c", "kind": "claude", "config_dir": "~/.claude-c"}
    if raw is not None:
        entry["capacity"] = raw
    profile = parse_profile(entry, HOME)
    assert profile is not None and profile.capacity == expected


def test_pick_launch_reads_the_hosts_picker_and_refuses_an_unknown_id():
    registry = _registry([Profile("shell", "SH", "shell", None, False)])
    assert registry.pick_launch() is None  # no picker set: an embedder without AUTO
    registry.set_launch_picker(lambda: {"id": "shell"})
    assert registry.pick_launch() == "shell"
    registry.set_launch_picker(lambda: {"id": "gone"})
    assert registry.pick_launch() is None
    registry.set_launch_picker(lambda: None)
    assert registry.pick_launch() is None


def test_pick_launch_walks_past_an_account_that_cannot_start_what_was_asked():
    registry = _registry(
        [
            Profile("codex", "CODEX", "codex", None, False),
            Profile("b", "B", "claude", None, False),
            Profile("a", "A", "claude", None, False),
        ]
    )
    pick = {"id": "codex", "ranking": [{"id": "codex"}, {"id": "a", "spent": True}, {"id": "b"}, {"id": "gone"}]}
    registry.set_launch_picker(lambda: pick)
    assert registry.pick_launch() == "codex"  # nothing asked: the freest account
    assert registry.pick_launch(model="opus") == "b"  # a Codex account takes no launch model; spent goes last
    assert registry.pick_launch(model="no-such-model") == "codex"  # nobody can: the spawn refuses with a reason


def test_codex_keeps_its_args_and_model_label():
    profile = parse_profile({"id": "codex", "kind": "codex", "args": ["-m", "gpt-5-codex"], "model_label": "gpt-5-codex"}, HOME)
    assert profile.args == ("-m", "gpt-5-codex")
    assert profile.model_label == "gpt-5-codex"
    assert profile.config_dir == "/Users/someone/.codex"
    assert profile.pin_env is False


# ── detection + default ──────────────────────────────────────────────────


def test_detection_lists_every_signed_in_account(tmp_path):
    (tmp_path / ".claude-a").mkdir()
    (tmp_path / ".claude-a" / ".claude.json").write_text("{}")
    (tmp_path / ".claude-empty").mkdir()  # never logged in → no .claude.json
    ids = [p.id for p in detect_profiles(tmp_path, codex_found=True)]
    assert ids == ["claude", "claude-a", "codex", "shell"]


def test_detection_ignores_the_bridge_env(tmp_path, monkeypatch):
    # A bridge launched from a claude-a pane carries its dir; that must
    # not relabel the dir as `claude`.
    (tmp_path / ".claude-a").mkdir()
    (tmp_path / ".claude-a" / ".claude.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-a"))
    by_id = {p.id: p for p in detect_profiles(tmp_path, codex_found=False)}
    assert by_id["claude"].config_dir == str(tmp_path / ".claude")
    assert by_id["claude"].pin_env is False


def test_the_env_only_chooses_the_default(tmp_path):
    profiles = detect_profiles(tmp_path, codex_found=False) + [
        Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True)
    ]
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-a")}
    assert choose_default(profiles, None, env, tmp_path) == "claude-a"
    assert choose_default(profiles, None, {}, tmp_path) == "claude"
    assert choose_default(profiles, "shell", env, tmp_path) == "shell"
    assert choose_default(profiles, "nope", {}, tmp_path) == "claude"


# ── load_profiles ────────────────────────────────────────────────────────


def test_an_absent_file_detects(tmp_path, monkeypatch):
    # The fixed probe dirs are real (/opt/homebrew/bin) — fence them off.
    monkeypatch.setattr("tldrack.profiles.CLI_PROBE_DIRS", ())
    registry = load_profiles(tmp_path / "missing.json", home=tmp_path, env={}, which=lambda _n: None)
    assert registry.source == "detected"
    assert registry.ids() == ["claude", "shell"]


def test_a_corrupt_file_detects_and_says_so(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{ nope")
    warnings = []
    registry = load_profiles(path, home=tmp_path, env={}, which=lambda _n: None, log=warnings.append)
    assert registry.source == "detected"
    assert any("corrupt" in line for line in warnings)


def test_a_file_keeps_its_order_drops_bad_rows_and_always_has_a_shell(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "default": "codex",
                "profiles": [
                    {"id": "claude-a", "label": "A", "kind": "claude", "config_dir": "~/.claude-a"},
                    {"id": "BAD", "kind": "claude"},
                    {"id": "codex", "label": "CODEX", "kind": "codex", "args": ["-m", "gpt-5-codex"]},
                    {"id": "codex", "label": "DUP", "kind": "shell"},
                ],
            }
        )
    )
    registry = load_profiles(path, home=tmp_path, env={}, which=lambda _n: None)
    assert registry.source == "file"
    assert registry.ids() == ["claude-a", "codex", "shell"]
    assert registry.default_id == "codex"
    assert registry.get("codex").label == "CODEX"  # first wins


# ── profile_for_path ─────────────────────────────────────────────────────


def _two_accounts(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-a").mkdir()
    return _registry(
        [
            Profile("claude-b", "B", "claude", str(tmp_path / ".claude"), False),
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False),
        ],
        home=tmp_path,
    )


def test_a_primary_path_is_never_claimed_by_the_shorter_root(tmp_path):
    registry = _two_accounts(tmp_path)
    path = str(tmp_path / ".claude-a" / "projects" / "p" / "s.jsonl")
    assert registry.profile_for_path(path) == "claude-a"
    assert registry.profile_for_path(str(tmp_path / ".claude" / "projects" / "s.jsonl")) == "claude-b"


def test_a_shared_root_is_ambiguous_unless_any_account_will_do(tmp_path):
    registry = _two_accounts(tmp_path)
    path = str(tmp_path / ".codex" / "sessions" / "x.jsonl")
    assert registry.profile_for_path(path) is None
    assert registry.profile_for_path(path, first_on_tie=True) == "codex"


def test_kind_narrows_and_unknown_paths_are_none(tmp_path):
    registry = _two_accounts(tmp_path)
    assert registry.profile_for_path(str(tmp_path / ".codex" / "x"), kind="claude") is None
    assert registry.profile_for_path("/etc/hosts") is None
    assert registry.profile_for_path(None) is None


def test_claude_roots_are_deduplicated_realpaths(tmp_path):
    registry = _two_accounts(tmp_path)
    assert registry.claude_roots() == [os.path.realpath(tmp_path / ".claude"), os.path.realpath(tmp_path / ".claude-a")]


# ── build_inner / spawn_argv ─────────────────────────────────────────────


def test_the_default_account_unsets_the_config_dir():
    inner = build_inner(Profile("b", "B", "claude", "/h/.claude", False), cli_bin="/bin/claude")
    assert "-u CLAUDE_CONFIG_DIR" in inner
    assert "CLAUDE_CONFIG_DIR=" not in inner
    for name in SHED_ENV:
        assert f"-u {name}" in inner


def test_a_pinned_account_sets_a_quoted_config_dir():
    inner = build_inner(
        Profile("a", "A", "claude", "/h/my dir/.claude-a", True), ["--resume=abc"], cli_bin="/opt/my claude/claude"
    )
    assert "CLAUDE_CONFIG_DIR='/h/my dir/.claude-a'" in inner
    assert inner.endswith("'/opt/my claude/claude' --resume=abc")


def test_codex_is_a_bare_function_call_never_under_env():
    inner = build_inner(Profile("codex", "CODEX", "codex", "/h/.codex", False, ("-m", "gpt-5-codex")))
    assert inner == "codex -m gpt-5-codex"
    pinned = build_inner(Profile("work", "WORK", "codex", "/h/.codex-work", True))
    assert pinned == "CODEX_HOME=/h/.codex-work codex"


#: An excerpt of codex-cli 0.163's `--help`, the shape the probe reads.
CODEX_HELP = """Usage: codex [OPTIONS] [PROMPT]

Options:
      --remote <ADDR>
          Connect the TUI to a remote app server endpoint.
      --no-daemon
          Run without the shared background server, even if it is already running
  -h, --help
          Print help (see a summary with '-h')
"""
OLD_CODEX_HELP = CODEX_HELP.replace("--no-daemon", "--oss")


def test_codex_leaves_the_shared_daemon_when_asked():
    # Hooks run where the app-server runs; the relay needs this pane's env (build_inner owns why).
    inner = build_inner(
        Profile("codex", "CODEX", "codex", "/h/.codex", False, ("-m", "gpt-5-codex")), ["fix the nav"], codex_no_daemon=True
    )
    assert inner == "codex --no-daemon -m gpt-5-codex 'fix the nav'"
    pinned = build_inner(Profile("work", "WORK", "codex", "/h/.codex-work", True), codex_no_daemon=True)
    assert pinned == "CODEX_HOME=/h/.codex-work codex --no-daemon"
    claude = build_inner(Profile("b", "B", "claude", "/h/.claude", False), cli_bin="/bin/claude", codex_no_daemon=True)
    assert "--no-daemon" not in claude


# ── launch permission modes ─────────────────────────────────────────────

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --output-format <format>              Output format (only works with --print):
                                        "text" (default), "json" (single
                                        result) (choices: "text", "json",
                                        "stream-json")
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  --permission-prompts <target>         Who answers permission prompts with
                                        --print: "host" or "none"
"""
OLD_CLAUDE_HELP = CLAUDE_HELP.replace('"auto",', "")
CODEX_AUTO_HELP = CODEX_HELP.replace(
    "      --no-daemon\n",
    "      --approve-for-me\n          Route approval requests through automatic review\n      --no-daemon\n",
)


def test_the_permission_mode_maps_to_each_clis_own_flag_and_nothing_for_a_shell():
    assert permission_args("claude", "auto") == ["--permission-mode", "auto"]
    assert permission_args("codex", "auto") == ["--approve-for-me"]
    assert permission_args("shell", "auto") == []
    assert permission_args("antigravity", "auto") == []
    assert permission_args(None, "auto") == []
    for kind in ("claude", "codex", "shell"):
        assert permission_args(kind, None) == [], "no mode is today's launch"


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "AUTO", "", 1, ["auto"]])
def test_a_mode_outside_the_closed_set_refuses_rather_than_spawning(mode):
    with pytest.raises(ValueError):
        permission_args("claude", mode)
    assert clean_permission_mode(mode) is None


def test_no_mode_bypasses_approvals_or_the_sandbox():
    flags = [arg for mode in PERMISSION_MODES for kind in ("claude", "codex") for arg in permission_args(kind, mode)]
    assert flags
    assert not any("dangerously" in arg or "bypass" in arg.lower() for arg in flags)


def test_the_cli_must_offer_the_flag_and_its_choice_on_an_option_line():
    assert cli_offers_permission("claude", "auto", CLAUDE_HELP) is True
    assert cli_offers_permission("claude", "auto", OLD_CLAUDE_HELP) is False, "an older Claude has no auto choice"
    assert cli_offers_permission("codex", "auto", CODEX_AUTO_HELP) is True
    assert cli_offers_permission("codex", "auto", CODEX_HELP) is False, "a Codex before --approve-for-me"
    prose = "Options:\n  --sandbox <MODE>\n          see --approve-for-me in the docs\n"
    assert cli_offers_permission("codex", "auto", prose) is False
    assert cli_offers_permission("shell", "auto", CLAUDE_HELP) is False
    for junk in (None, "", 42):
        assert cli_offers_permission("claude", "auto", junk) is False


@pytest.mark.parametrize(
    ("kind", "args", "pinned"),
    [
        ("claude", ("--permission-mode", "plan"), "--permission-mode"),
        ("claude", ("--permission-mode=plan",), "--permission-mode"),
        ("claude", ("--dangerously-skip-permissions",), "--dangerously-skip-permissions"),
        ("codex", ("-s", "read-only"), "-s"),
        ("codex", ("--full-auto",), "--full-auto"),
        ("codex", ("-c", "approval_policy=never"), "-c approval_policy"),
        ("codex", ("-m", "gpt-5-codex"), None),
        ("claude", ("--model", "opus"), None),
    ],
)
def test_a_profile_that_decides_its_own_approvals_pins_them(kind, args, pinned):
    assert pinned_permission_flag(Profile("p", "P", kind, "/h/.x", False, args)) == pinned


def test_spawn_argv_puts_the_mode_before_the_prompt_and_stamps_only_what_it_applied():
    claude = Profile("b", "B", "claude", "/h/.claude", False, ("--model", "opus"))
    argv = spawn_argv(claude, "wt-a", "/repo", ["fix the nav"], cli_bin="/bin/claude", permission_mode="auto")
    assert "/bin/claude --model opus --permission-mode auto 'fix the nav';" in argv[17]
    assert argv[-6:] == [";", "set-option", "-t", "=wt-a:", PERMISSION_MODE_OPTION, "auto"]
    codex = spawn_argv(
        Profile("codex", "CODEX", "codex", "/h/.codex", False, ("-m", "gpt-5-codex")),
        "wt-c",
        "/repo",
        ["fix the nav"],
        codex_no_daemon=True,
        permission_mode="auto",
    )
    assert codex[17].startswith("codex --no-daemon -m gpt-5-codex --approve-for-me 'fix the nav'; ")
    plain = spawn_argv(claude, "wt-b", "/repo", cli_bin="/bin/claude")
    assert PERMISSION_MODE_OPTION not in plain and "--permission-mode" not in plain[17]
    shell = spawn_argv(Profile("shell", "SH", "shell", None, False), "vc-3", "/repo", permission_mode="auto")
    assert PERMISSION_MODE_OPTION not in shell, "a shell took no args, so nothing is recorded"


def _mode_registry(tmp_path, claude_help=CLAUDE_HELP, codex_help=CODEX_AUTO_HELP, calls=None):
    """Both CLIs are real files (the probe keys on realpath + mtime), each with its own `--help`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bins = {name: tmp_path / name for name in ("claude", "codex")}
    for path in bins.values():
        path.write_text("#!/bin/sh\n")
    helps = {str(bins["claude"]): claude_help, str(bins["codex"]): codex_help}

    def cli_help(path):
        (calls if calls is not None else []).append(path)
        return helps.get(os.path.realpath(path))

    registry = ProfileRegistry(
        [
            Profile("a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("plan", "PLAN", "claude", str(tmp_path / ".claude-plan"), True, ("--permission-mode", "plan")),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False, ("-m", "gpt-5-codex")),
            Profile("shell", "SH", "shell", None, False),
        ],
        "a",
        home=tmp_path,
        source="file",
        which=lambda name: str(bins[name]) if name in bins else None,
        cli_help=cli_help,
    )
    return registry, bins


def test_the_registry_starts_claude_and_codex_in_auto_once_their_clis_offer_it(tmp_path):
    calls = []
    registry, bins = _mode_registry(tmp_path, calls=calls)
    assert registry.permission_modes() == list(PERMISSION_MODES) == ["auto"]
    assert registry.permission_refusal("a", "auto") is None
    assert registry.permission_refusal("codex", "auto") is None
    codex = registry.spawn_argv("codex", "wt-c", "/repo", permission_mode="auto")
    assert codex[17].startswith("codex --no-daemon -m gpt-5-codex --approve-for-me; ")
    assert PERMISSION_MODE_OPTION in codex
    claude = registry.spawn_argv("a", "wt-d", "/repo", cli_bin=str(bins["claude"]), permission_mode="auto")
    assert f"{bins['claude']} --permission-mode auto;" in claude[17]
    assert sorted(set(calls)) == sorted(str(path) for path in bins.values()), "one --help per binary, shared"
    assert len(calls) == 2


def test_the_registry_refuses_with_a_reason_and_never_spawns_a_mode_it_cannot_start(tmp_path):
    registry, _ = _mode_registry(tmp_path / "old", claude_help=OLD_CLAUDE_HELP, codex_help=CODEX_HELP)
    assert "upgrade" in registry.permission_refusal("a", "auto")
    assert "--approve-for-me" in registry.permission_refusal("codex", "auto")
    with pytest.raises(ValueError, match="upgrade"):
        registry.spawn_argv("a", "wt-e", "/repo", permission_mode="auto")
    registry, _ = _mode_registry(tmp_path / "new")
    assert "pins its own approvals" in registry.permission_refusal("plan", "auto")
    assert "shell" in registry.permission_refusal("shell", "auto")
    assert "unknown profile" in registry.permission_refusal("nope", "auto")
    assert "unknown permission mode" in registry.permission_refusal("a", "bypassPermissions")
    unread, _ = _mode_registry(tmp_path / "unread", claude_help=None)
    assert "could not read" in unread.permission_refusal("a", "auto")


def test_the_no_daemon_probe_reads_an_option_line_not_prose():
    assert codex_has_no_daemon(CODEX_HELP) is True
    assert codex_has_no_daemon(OLD_CODEX_HELP) is False
    assert codex_has_no_daemon("see --no-daemon in the docs\n") is False
    assert codex_has_no_daemon("      --no-daemon-ish\n") is False
    for junk in ("", None, 42, b"      --no-daemon\n"):
        assert codex_has_no_daemon(junk) is False


def _codex_registry(tmp_path, help_text, calls):
    """A registry whose `codex` is a real file (the probe keys on its mtime)
    and whose `--help` is `help_text` — a string, None, or a callable."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\n")

    def cli_help(path):
        calls.append(path)
        return help_text() if callable(help_text) else help_text

    registry = ProfileRegistry(
        [
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False, ("-m", "gpt-5-codex")),
            Profile("a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("shell", "SH", "shell", None, False),
        ],
        "codex",
        home=tmp_path,
        source="file",
        which=lambda name: str(cli) if name == "codex" else f"/usr/bin/{name}",
        cli_help=cli_help,
    )
    return registry, cli


def test_a_codex_spawn_leaves_the_daemon_when_its_cli_offers_to(tmp_path):
    calls = []
    registry, _ = _codex_registry(tmp_path, CODEX_HELP, calls)
    assert registry.spawn_argv("codex", "vc-1", "/repo")[17].startswith("codex --no-daemon -m gpt-5-codex; ")
    assert "--no-daemon" not in registry.spawn_argv("a", "vc-2", "/repo")[17]
    assert registry.spawn_argv("shell", "vc-3", "/repo")[13] == ";"
    registry.spawn_argv("codex", "vc-4", "/repo", ["a prompt"])
    assert len(calls) == 1  # one probe per binary, then cached


def test_an_older_or_unreadable_codex_spawns_exactly_as_before(tmp_path):
    for case, help_text in enumerate((OLD_CODEX_HELP, None)):
        calls = []
        registry, _ = _codex_registry(tmp_path / str(case), help_text, calls)
        assert registry.spawn_argv("codex", "vc-1", "/repo")[17].startswith("codex -m gpt-5-codex; ")
        assert len(calls) == 1


def test_an_upgraded_codex_is_probed_again(tmp_path):
    answers = iter([OLD_CODEX_HELP, CODEX_HELP])
    calls = []
    registry, cli = _codex_registry(tmp_path, lambda: next(answers), calls)
    assert registry.codex_no_daemon("codex") is False
    later = cli.stat().st_mtime_ns + 10**9
    os.utime(cli, ns=(later, later))
    assert registry.codex_no_daemon("codex") is True
    assert len(calls) == 2


def test_no_probe_without_a_codex_cli_or_for_another_kind(tmp_path, monkeypatch):
    monkeypatch.setattr("tldrack.profiles.CLI_PROBE_DIRS", ())  # `which` alone decides
    registry = ProfileRegistry(
        [
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False),
            Profile("a", "A", "claude", str(tmp_path / ".claude-a"), True),
        ],
        "codex",
        home=tmp_path,
        source="file",
        which=lambda _name: None,
        cli_help=lambda _path: pytest.fail("probed with no CLI to probe"),
    )
    assert registry.codex_no_daemon("codex") is False
    assert registry.codex_no_daemon("a") is False
    assert registry.codex_no_daemon("nope") is False


def test_a_shell_has_no_inner_command():
    assert build_inner(Profile("shell", "SH", "shell", None, False)) is None


def test_spawn_stamps_the_session_in_the_same_invocation():
    argv = spawn_argv(Profile("codex", "CODEX", "codex", "/h/.codex", False), "vc-2", "/repo")
    assert argv[:7] == ["tmux", "new-session", "-d", "-s", "vc-2", "-c", "/repo"]
    # `=S:` — set-option targets a PANE, which takes exact-match only as `=session:`.
    assert argv[-6:] == [";", "set-option", "-t", "=vc-2:", PROFILE_OPTION, "codex"]
    assert argv[7:13] == ["/bin/zsh", "-f", "-c", PANE_CD_SCRIPT, "zsh", "/repo"], "the pane changes directory itself"
    assert argv[13:17] == ["/bin/zsh", "-l", "-i", "-c"]
    assert argv[17].startswith("codex; echo; ")
    assert argv[17].endswith("exec /bin/zsh -l")


def test_a_shell_spawn_ends_in_the_panes_login_shell():
    # No command after the directory: `PANE_CD_SCRIPT` execs `$SHELL -l`, tmux's own default shell.
    argv = spawn_argv(Profile("shell", "SH", "shell", None, False), "vc-1", "/repo")
    assert argv == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "vc-1",
        "-c",
        "/repo",
        "/bin/zsh",
        "-f",
        "-c",
        PANE_CD_SCRIPT,
        "zsh",
        "/repo",
        ";",
        "set-option",
        "-t",
        "=vc-1:",
        PROFILE_OPTION,
        "shell",
    ]


def test_a_cwd_ending_in_a_semicolon_cannot_split_the_command(tmp_path):
    # tmux splits a sequence on any argv element ENDING in `;`.
    argv = spawn_argv(Profile("shell", "SH", "shell", None, False), "vc-1", "/odd;")
    assert argv[6] == "/odd;" + os.sep
    assert argv[12] == "/odd;" + os.sep, "the directory the pane changes into is fenced the same way"
    assert not any(arg.endswith(";") for arg in directory_args("/odd;", ["/bin/zsh", "-l"]))


#: The pane command is zsh's, run for real below. A machine without it skips those tests; CI installs it.
needs_zsh = pytest.mark.skipif(not os.path.exists("/bin/zsh"), reason="the pane command runs /bin/zsh")


def run_pane_cd(target, command, cwd, home):
    """The pane command `directory_args` builds, run for real from `cwd`."""
    argv = directory_args(target, command)[2:]
    env = {**os.environ, "HOME": str(home), "SHELL": "/bin/sh"}
    return subprocess.run(argv, capture_output=True, text=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL)


@needs_zsh
def test_the_pane_changes_directory_itself_whatever_directory_tmux_left_it_in(tmp_path):
    """A tmux server whose start directory is gone ignores `-c` (`PANE_CD_SCRIPT`),
    so the directory must come from the pane command, not from where it started."""
    target = tmp_path / "re po;"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = run_pane_cd(target, ["/bin/sh", "-c", 'pwd -P; echo "pwd=$PWD"'], elsewhere, tmp_path)
    assert done.stdout.splitlines() == [str(target.resolve()), f"pwd={target}"]


@needs_zsh
def test_a_directory_that_vanished_falls_back_to_home_as_tmux_does(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    done = run_pane_cd(tmp_path / "gone", ["/bin/sh", "-c", "pwd -P"], tmp_path, home)
    assert done.stdout.strip() == str(home.resolve())


@needs_zsh
def test_no_command_after_the_directory_is_a_login_shell(tmp_path):
    # `$SHELL -l` with stdin closed: the login shell starts in the directory and exits at once.
    shell = tmp_path / "shell"
    shell.write_text('#!/bin/sh\necho "args=$* in=$(pwd -P)"\n')
    shell.chmod(shell.stat().st_mode | stat.S_IEXEC)
    argv = directory_args(tmp_path)[2:]
    done = subprocess.run(argv, capture_output=True, text=True, cwd="/", env={**os.environ, "SHELL": str(shell)})
    assert done.stdout.strip() == f"args=-l in={tmp_path.resolve()}"


def test_the_claude_inner_really_sheds_and_pins(tmp_path):
    """Run the inner command for real against a fake `claude` that reports
    its environment — the env(1) flags are the contract, not their spelling."""
    fake = tmp_path / "claude"
    fake.write_text(
        '#!/bin/sh\necho "cfg=${CLAUDE_CONFIG_DIR-UNSET} key=${ANTHROPIC_API_KEY-UNSET} cc=${CLAUDECODE-UNSET} args=$*"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": "/inherited", "ANTHROPIC_API_KEY": "sk-x", "CLAUDECODE": "1"}

    pinned = build_inner(Profile("a", "A", "claude", "/h/.claude-a", True), ["--resume=abc"], cli_bin=str(fake))
    out = subprocess.run(["/bin/sh", "-c", pinned], capture_output=True, text=True, env=env).stdout
    assert out.strip() == "cfg=/h/.claude-a key=UNSET cc=UNSET args=--resume=abc"

    default = build_inner(Profile("b", "B", "claude", "/h/.claude", False), cli_bin=str(fake))
    out = subprocess.run(["/bin/sh", "-c", default], capture_output=True, text=True, env=env).stdout
    assert out.strip() == "cfg=UNSET key=UNSET cc=UNSET args="


def test_apply_profile_env_for_the_bootstrap():
    base = {"CLAUDE_CONFIG_DIR": "/inherited", "PATH": "/bin"}
    assert apply_profile_env(Profile("a", "A", "claude", "/p", True), base)["CLAUDE_CONFIG_DIR"] == "/p"
    assert "CLAUDE_CONFIG_DIR" not in apply_profile_env(Profile("b", "B", "claude", "/c", False), base)
    assert apply_profile_env(Profile("shell", "SH", "shell", None, False), base) == base


# ── hooks ────────────────────────────────────────────────────────────────


def _curl_relay(port, event):
    """The `command` relay — Codex's, and Claude's before 2026-10-27."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"curl \"http://127.0.0.1:{port}/hook?session=$(tmux display-message -p '#S')&event={event}\"",
            }
        ]
    }


def _relay(port, event):
    """The `http` relay — Claude's (main.hook_http_entry)."""
    return {
        "hooks": [
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/hook?event={event}",
                "headers": {"X-VC-Pane": "$TMUX_PANE"},
                "allowedEnvVars": ["TMUX_PANE"],
                "timeout": 2,
            }
        ]
    }


def _block(port, events):
    return {event: [_relay(port, event)] for event in events}


STATUS_LINE = {"type": "command", "command": "python3 /x/statusline.py --status-port 7682"}
ENV = dict(CLAUDE_SETTINGS_ENV)


def test_hooks_status_needs_every_event_on_this_port_and_the_statusline():
    doc = {"hooks": _block(7682, ["Stop", "PreToolUse"]), "statusLine": STATUS_LINE, "env": ENV}
    assert hooks_status("claude", doc, 7682, ["Stop", "PreToolUse"]) is True
    assert hooks_status("claude", doc, 7690, ["Stop"]) is False  # wrong port
    assert hooks_status("claude", doc, 7682, ["Stop", "Notification"]) is False
    assert hooks_status("claude", {"hooks": doc["hooks"]}, 7682, ["Stop"]) is False  # no statusline
    assert hooks_status("codex", {"hooks": doc["hooks"]}, 7682, ["Stop"]) is True
    assert hooks_status("claude", {**doc, "env": {}}, 7682, ["Stop"]) is False  # no task-tools env


def test_hooks_missing_names_each_gap_in_order():
    doc = {"hooks": _block(7682, ["Stop"])}
    assert hooks_missing("claude", doc, 7682, ["Stop", "StopFailure", "PermissionRequest"]) == [
        "StopFailure",
        "PermissionRequest",
        "statusLine",
        "env:CLAUDE_CODE_ENABLE_TODO_TOOLS",
    ]
    assert hooks_missing("codex", doc, 7682, ["Stop"]) == []
    # A value the user chose counts as present: the install keeps it, so naming it would nag forever.
    own = {**doc, "statusLine": STATUS_LINE, "env": {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"}}
    assert hooks_missing("claude", own, 7682, ["Stop"]) == []
    assert hooks_missing("claude", None, 7682, ["Stop"])[0] == "Stop"


def test_merge_adds_env_only_where_absent():
    merged, notes = merge_hooks({"env": {"FOO": "1"}}, {}, STATUS_LINE, 7682, ENV)
    assert merged["env"] == {"FOO": "1", **ENV} and notes == []
    merged, notes = merge_hooks({"env": {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"}}, {}, STATUS_LINE, 7682, ENV)
    assert merged["env"]["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "0"
    assert notes and "CLAUDE_CODE_ENABLE_TODO_TOOLS" in notes[0]


def test_merge_replaces_our_relay_and_keeps_everyone_elses():
    theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
    doc = {"model": "opus", "hooks": {"Stop": [theirs, _relay(7600, "Stop")]}}
    merged, notes = merge_hooks(doc, _block(7682, ["Stop"]), STATUS_LINE, 7682)
    assert merged["model"] == "opus"
    assert merged["hooks"]["Stop"] == [theirs, _relay(7682, "Stop")]  # stale port replaced
    assert merged["statusLine"] == STATUS_LINE
    assert notes == []
    assert doc["hooks"]["Stop"][1] == _relay(7600, "Stop")  # input untouched


def test_merge_never_overwrites_a_users_own_statusline():
    doc = {"statusLine": {"type": "command", "command": "starship"}}
    merged, notes = merge_hooks(doc, {}, STATUS_LINE, 7682)
    assert merged["statusLine"]["command"] == "starship"
    assert notes and "statusLine" in notes[0]


def test_install_is_idempotent_and_backs_up_first(tmp_path):
    config = tmp_path / ".claude-a"
    config.mkdir()
    settings = config / "settings.json"
    settings.write_text(json.dumps({"theme": "auto"}))
    profile = Profile("claude-a", "A", "claude", str(config), True)

    path, changed, backup, _notes = install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682, now=lambda: 1000)
    assert changed is True and path == settings
    assert backup == config / "settings.json.bak-1000"
    assert json.loads(backup.read_text()) == {"theme": "auto"}
    assert backup.stat().st_mode & 0o777 == 0o600  # a settings file can hold tokens
    written = json.loads(settings.read_text())
    assert written["theme"] == "auto" and written["env"] == ENV
    assert hooks_status("claude", written, 7682, ["Stop"]) is True

    _path, changed, backup, _notes = install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682, now=lambda: 2000)
    assert changed is False and backup is None
    assert not (config / "settings.json.bak-2000").exists()


def test_install_writes_through_a_shared_symlinked_settings_file(tmp_path):
    # ~/.claude-c/settings.json → ~/.claude/settings.json: one file, two accounts. os.replace on the
    # link itself would leave C a private copy that drifts from B from then on.
    shared = tmp_path / ".claude"
    shared.mkdir()
    (shared / "settings.json").write_text(json.dumps({"theme": "auto"}))
    third = tmp_path / ".claude-c"
    third.mkdir()
    (third / "settings.json").symlink_to(shared / "settings.json")
    profile = Profile("claude-c", "C", "claude", str(third), False)

    path, changed, backup, _notes = install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682, now=lambda: 1000)
    assert changed is True and path == (shared / "settings.json").resolve()
    assert (third / "settings.json").is_symlink(), "the link survives the install"
    assert backup == path.with_name("settings.json.bak-1000")
    assert hooks_status("claude", json.loads((third / "settings.json").read_text()), 7682, ["Stop"]) is True


def test_install_refuses_a_broken_symlink(tmp_path):
    (tmp_path / "settings.json").symlink_to(tmp_path / "gone" / "settings.json")
    profile = Profile("a", "A", "claude", str(tmp_path), True)
    with pytest.raises(ValueError, match="broken symlink"):
        install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682)
    assert not (tmp_path / "gone").exists()


def test_install_writes_a_codex_hooks_file_without_a_statusline(tmp_path):
    profile = Profile("codex", "CODEX", "codex", str(tmp_path), False)
    path, changed, backup, _notes = install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682)
    assert path == tmp_path / "hooks.json" and changed is True and backup is None
    written = json.loads(path.read_text())
    assert "statusLine" not in written and "env" not in written


def test_install_refuses_a_file_that_is_not_a_json_object(tmp_path):
    (tmp_path / "settings.json").write_text("[1, 2]")
    profile = Profile("a", "A", "claude", str(tmp_path), True)
    with pytest.raises(ValueError):
        install_hooks(profile, _block(7682, ["Stop"]), STATUS_LINE, 7682)
    assert (tmp_path / "settings.json").read_text() == "[1, 2]"


def test_hooks_ok_rereads_only_when_the_file_changes(tmp_path):
    config = tmp_path / ".claude-a"
    config.mkdir()
    profile = Profile("claude-a", "A", "claude", str(config), True)
    registry = _registry([profile], home=tmp_path)
    events = {"claude": ["Stop"]}
    assert registry.hooks_ok(profile, 7682, events) is False  # no file
    (config / "settings.json").write_text(json.dumps({"hooks": _block(7682, ["Stop"]), "statusLine": STATUS_LINE, "env": ENV}))
    assert registry.hooks_ok(profile, 7682, events) is True
    assert registry.hooks_ok(Profile("shell", "SH", "shell", None, False), 7682, events) is None


def test_an_http_relay_counts_and_an_install_migrates_the_curl_one():
    from tldrack.main import build_hooks_settings, hook_http_entry

    from tldrack.profiles import is_relay_entry

    assert is_relay_entry(hook_http_entry(7682, "Stop"), 7682)
    assert not is_relay_entry(hook_http_entry(7682, "Stop"), 7690)
    assert is_relay_entry(hook_http_entry(7600, "Stop"))  # any port: what merge replaces
    assert not is_relay_entry({"type": "http", "url": "https://example.com/hook?event=Stop"})
    theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
    doc = {"hooks": {"Stop": [theirs, _curl_relay(7682, "Stop")]}, "statusLine": STATUS_LINE, "env": ENV}
    assert hooks_missing("claude", doc, 7682, ["Stop"]) == ["transport:http"]  # current, but forking
    assert hooks_missing("codex", doc, 7682, ["Stop"]) == []  # Codex keeps the curl relay
    merged, _notes = merge_hooks(doc, build_hooks_settings(7682, ("Stop",))["hooks"], STATUS_LINE, 7682)
    assert merged["hooks"]["Stop"] == [theirs, {"hooks": [hook_http_entry(7682, "Stop")]}]
    assert hooks_status("claude", merged, 7682, ["Stop"]) is True


def test_relay_marker_matches_the_real_hook_command():
    # Pinned against main.hook_command, which every installed relay carries.
    from tldrack.main import hook_command

    assert relay_marker(7682) in hook_command(7682, "Stop")


# ── the wire ─────────────────────────────────────────────────────────────


def test_summary_carries_exactly_the_contract_keys(tmp_path):
    registry = _registry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path), True, (), None),
            Profile("shell", "SH", "shell", None, False),
        ],
        default="claude-a",
        home=tmp_path,
    )
    usage = {"claude-a": {"five_hour": {"used_percentage": 3.0}, "age_seconds": 1.0}}
    rows = registry.summary(7682, {"claude": ["Stop"]}, usage)
    assert [set(row) for row in rows] == [
        {"id", "label", "kind", "default", "hooks_ok", "hooks_missing", "cli_found", "model_label", "usage", "standing", "paused"}
    ] * 2
    assert [row["paused"] for row in rows] == [False, False]
    registry.availability.save({"profile": "claude-a", "paused": True})
    assert registry.summary(7682, {"claude": ["Stop"]}, usage)[0]["paused"] is True
    assert [row["standing"] for row in rows] == [None, None]
    out = {"claude-a": {"state": "out", "error": "billing_error", "message": None, "since": 1.0}}
    assert registry.summary(7682, {"claude": ["Stop"]}, usage, out)[0]["standing"] == out["claude-a"]
    assert rows[0]["default"] is True and rows[0]["usage"] == usage["claude-a"]
    assert rows[1]["hooks_ok"] is None and rows[1]["usage"] is None
    assert rows[0]["hooks_missing"] == ["file"] and rows[1]["hooks_missing"] is None


def test_resolve_refuses_a_typo_rather_than_falling_back():
    registry = _registry([Profile("shell", "SH", "shell", None, False)])
    assert registry.resolve(None).id == "shell"
    assert registry.resolve("").id == "shell"
    assert registry.resolve("shel") is None
    with pytest.raises(KeyError):
        registry.spawn_argv("shel", "vc-1", "/repo")


def test_profile_identity_remains_importable_without_bridge_runtime():
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = """
from pathlib import Path
from tldrack.profiles import Profile, ProfileRegistry
registry = ProfileRegistry([Profile('shell', 'SH', 'shell', None, False)],
                           'shell', home=Path('/tmp'), source='file')
assert registry.describe('shell')['kind'] == 'shell'
assert registry.claude_roots() == []
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-S", "-c", code], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
