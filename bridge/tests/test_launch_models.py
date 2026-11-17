"""Launch models (profiles.py § LAUNCH MODELS): the agent's `--model`, the model FORCED on its
subagents, the "strictly weaker" gate, the stamps, and their replay through snapshot → records → restore."""

import os

import pytest

from tldrack.profiles import (
    MODEL_OPTION,
    SHED_ENV,
    SUBAGENT_MODEL_OPTION,
    Profile,
    ProfileRegistry,
    clean_launch_model,
    model_args,
    model_tier,
    parse_cli_version,
    pinned_model_flag,
    spawn_argv,
    subagent_env,
)
from tldrack.restore import plan_restore, restore_argv
from tldrack.session_records import clean_record
from tldrack.snapshot import TMUX_FIELD_SEP, TMUX_MODELS_FIELD, TMUX_PANE_FORMAT, parse_tmux_panes

A = Profile("a", "A", "claude", "/h/.claude-a", True)
SID = "11111111-1111-4111-8111-111111111111"


def registry(tmp_path, version="2.1.322 (Claude Code)", extra=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    return ProfileRegistry(
        [
            Profile("a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("pinned", "PIN", "claude", str(tmp_path / ".claude-pin"), True, ("--model", "opus")),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False, ("-m", "gpt-5-codex")),
            *extra,
        ],
        "a",
        home=tmp_path,
        source="file",
        which=lambda name: str(claude) if name == "claude" else None,
        cli_help=lambda _path: None,
        cli_version=lambda _path: version,
    )


@pytest.mark.parametrize(
    ("model", "tier"),
    [
        ("haiku", 0),
        ("sonnet", 1),
        ("opus", 2),
        ("fable", 3),
        ("claude-opus-5-5", 2),
        ("claude-fable-5-2", 3),
        ("claude-haiku-4-5-20251001", 0),
        ("claude-3-5-sonnet-20241022", 1),
        ("claude-x", None),
        ("gpt-5-codex", None),
        ("Opus", None),
        ("", None),
        (None, None),
        (3, None),
        ("claude-opus-sonnet", None),  # two families: no single rank
        ("opus; rm -rf ~", None),
    ],
)
def test_a_model_is_ranked_by_its_family_or_not_accepted_at_all(model, tier):
    assert model_tier(model) == tier
    assert clean_launch_model(model) == (model if tier is not None else None)


def test_the_models_map_to_claudes_flag_and_two_env_operands_and_nothing_for_another_kind():
    assert model_args("claude", "fable") == ["--model", "fable"]
    assert subagent_env("claude", "opus") == {"CLAUDE_CODE_SUBAGENT_MODEL": "opus", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}
    for kind in ("codex", "shell", "antigravity", None):
        assert model_args(kind, "fable") == [] and subagent_env(kind, "opus") == {}
    assert model_args("claude", None) == [] and subagent_env("claude", None) == {}
    for call in (model_args, subagent_env):
        with pytest.raises(ValueError):
            call("claude", "gpt-5-codex")


def test_spawn_argv_sheds_inherited_models_then_sets_and_stamps_only_what_it_applied():
    argv = spawn_argv(
        A, "lead-1", "/repo", ["go"], cli_bin="/bin/claude", permission_mode="auto", model="fable", subagent_model="opus"
    )
    inner = argv[11]
    for name in ("ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        assert name in SHED_ENV and f"-u {name} " in inner
    # `env` reads its -u options before its NAME=VALUE operands, so the operands win.
    assert inner.index("-u CLAUDE_CODE_SUBAGENT_MODEL ") < inner.index("CLAUDE_CODE_SUBAGENT_MODEL=opus")
    assert (
        "CLAUDE_CONFIG_DIR=/h/.claude-a CLAUDE_CODE_SUBAGENT_MODEL=opus "
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1 /bin/claude --model fable --permission-mode auto go;"
    ) in inner
    assert argv[-12:] == [
        ";",
        "set-option",
        "-t",
        "=lead-1:",
        MODEL_OPTION,
        "fable",
        ";",
        "set-option",
        "-t",
        "=lead-1:",
        SUBAGENT_MODEL_OPTION,
        "opus",
    ]
    plain = spawn_argv(A, "vc-1", "/repo", cli_bin="/bin/claude")
    assert MODEL_OPTION not in plain and SUBAGENT_MODEL_OPTION not in plain
    assert "--model" not in plain[11] and "CLAUDE_CODE_SUBAGENT_MODEL=" not in plain[11]
    codex = spawn_argv(
        Profile("codex", "CODEX", "codex", "/h/.codex", False, ("-m", "gpt-5-codex")),
        "wt-c",
        "/repo",
        model="opus",
        subagent_model="haiku",
    )
    assert MODEL_OPTION not in codex and "opus" not in codex[11], "a Codex took nothing, so nothing is recorded"


def test_the_registry_refuses_what_would_not_hold(tmp_path):
    reg = registry(tmp_path)
    assert reg.model_refusal("a") is None, "naming neither is today's launch"
    assert reg.model_refusal("a", "fable", "opus") is None
    assert reg.model_refusal("a", "claude-fable-5-2", "claude-opus-5-5") is None
    assert reg.model_refusal("a", None, "opus") is None, "the account's own model: nothing to rank against"
    assert "not weaker" in reg.model_refusal("a", "opus", "opus")
    assert "not weaker" in reg.model_refusal("a", "opus", "fable")
    assert "unknown subagent model" in reg.model_refusal("a", "fable", "gpt-5-codex")
    assert "unknown launch model" in reg.model_refusal("a", "best")
    assert "unknown profile" in reg.model_refusal("nope", "fable")
    assert "takes no launch model" in reg.model_refusal("codex", "opus")
    assert pinned_model_flag(reg.get("pinned")) == "--model"
    assert "pins its own model" in reg.model_refusal("pinned", "fable")
    assert reg.model_refusal("pinned", None, "sonnet") is None, "weaker than the pin"
    assert "not weaker" in reg.model_refusal("pinned", None, "opus"), "ranked against the pin"
    with pytest.raises(ValueError, match="not weaker"):
        reg.spawn_argv("a", "s", "/repo", model="sonnet", subagent_model="opus")


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ("2.1.322 (Claude Code)", None),
        ("2.1.257 (Claude Code)", None),
        ("3.0.0", None),
        ("2.1.256 (Claude Code)", "cannot force"),
        ("2.0.999", "cannot force"),
        (None, "could not read"),
        ("garbage", "could not read"),
    ],
)
def test_a_cli_that_would_ignore_the_force_is_refused_not_trusted(tmp_path, version, refused):
    reg = registry(tmp_path, version=version)
    answer = reg.model_refusal("a", "fable", "opus")
    assert (answer is None) if refused is None else (refused in answer)
    assert reg.model_refusal("a", "opus") is None, "a launch model alone needs no version"
    assert parse_cli_version("2.1.322 (Claude Code)") == (2, 1, 322)


def test_the_stamps_ride_one_leading_field_and_reach_the_row(tmp_path):
    assert TMUX_PANE_FORMAT.startswith(TMUX_MODELS_FIELD + TMUX_FIELD_SEP)
    tail = ("", "", "1700000000", "", "lead-1", "0", "1", "1", "/repo", "a", "a title")
    both = parse_tmux_panes(TMUX_FIELD_SEP.join(("fable opus", *tail)))["lead-1"]
    assert (both["launch_model"], both["subagent_model"]) == ("fable", "opus")
    crew = parse_tmux_panes(TMUX_FIELD_SEP.join(("opus ", *tail)))["lead-1"]
    assert crew["launch_model"] == "opus" and "subagent_model" not in crew
    capped = parse_tmux_panes(TMUX_FIELD_SEP.join((" opus", *tail)))["lead-1"]
    assert capped["subagent_model"] == "opus" and "launch_model" not in capped
    for unset in (" ", "", "x; y"):  # tmux prints a lone space for two unset options
        row = parse_tmux_panes(TMUX_FIELD_SEP.join((unset, *tail)))["lead-1"]
        assert "launch_model" not in row and "subagent_model" not in row and row["title"] == "a title"


def test_a_restore_replays_the_models_and_drops_them_when_they_can_no_longer_hold(tmp_path):
    reg = registry(tmp_path)
    (tmp_path / ".claude-a" / "projects" / "p").mkdir(parents=True)
    (tmp_path / ".claude-a" / "projects" / "p" / f"{SID}.jsonl").write_text("")
    stored = clean_record(
        {
            "cwd": str(tmp_path),
            "profile": "a",
            "kind": "claude",
            "agent_session_id": SID,
            "last_seen": 100.0,
            "launch_model": "fable",
            "subagent_model": "opus",
        }
    )
    assert (stored["launch_model"], stored["subagent_model"]) == ("fable", "opus")
    assert "launch_model" not in clean_record({"last_seen": 100.0, "launch_model": "gpt-5-codex"})

    lead = plan_restore("lead-1", stored, reg, open_ids={}, now=200.0)
    assert lead.ok and lead.row()["model"] == "fable" and lead.row()["subagent_model"] == "opus"
    argv = restore_argv(lead, reg)
    assert "CLAUDE_CODE_SUBAGENT_MODEL=opus" in argv[11] and "--model fable" in argv[11]
    assert f"--resume={SID}" in argv[11] and SUBAGENT_MODEL_OPTION in argv, "restamped for the next restore"

    downgraded = plan_restore("lead-1", stored, registry(tmp_path / "old", version="2.1.200"), open_ids={}, now=200.0)
    assert downgraded.model is None and downgraded.subagent_model is None
    plain = plan_restore("vc-1", {k: v for k, v in stored.items() if "model" not in k}, reg, open_ids={}, now=200.0)
    assert "model" not in plain.row() and "--model" not in restore_argv(plain, reg)[11]
    assert os.path.isdir(stored["cwd"])
