"""restore.py — the refusal table and the spawn argv for a restored session."""

import os

import pytest

from tldrack.profiles import Profile, ProfileRegistry, resume_args
from tldrack.restore import open_conversations, plan_restore, restore_argv, restore_mode
from tldrack.agent_index import IndexEntry

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"
CLAUDE_AUTO_HELP = (
    'Options:\n  --permission-mode <mode>  Permission mode\n                            (choices: "auto", "plan")\n'
)


def registry(tmp_path, codex=True):
    (tmp_path / ".claude-a" / "projects" / "p").mkdir(parents=True, exist_ok=True)
    return ProfileRegistry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False),
            Profile("shell", "SH", "shell", None, False),
        ],
        "claude-a",
        home=tmp_path,
        source="file",
        which=lambda name: None if (name == "codex" and not codex) else f"/usr/bin/{name}",
    )


def record(cwd, profile="claude-a", kind="claude", agent=SID, last_seen=100.0):
    return {"cwd": cwd, "profile": profile, "kind": kind, "agent_session_id": agent, "last_seen": last_seen}


def plan(tmp_path, rec, open_ids=None, profiles=None, **kw):
    return plan_restore("wt-nav", rec, profiles or registry(tmp_path), open_ids=open_ids or {}, now=160.0, **kw)


def test_a_claude_record_with_its_transcript_resumes(tmp_path):
    profiles = registry(tmp_path)
    (tmp_path / ".claude-a" / "projects" / "p" / f"{SID}.jsonl").write_text("")
    p = plan(tmp_path, record(str(tmp_path)), profiles=profiles)
    assert p.ok and p.resume == [f"--resume={SID}"]
    assert p.row() == {
        "session": "wt-nav",
        "cwd": str(tmp_path),
        "profile": "claude-a",
        "kind": "claude",
        "resumable": True,
        "age_seconds": 60.0,
        "ok": True,
    }
    argv = restore_argv(p, profiles)
    assert argv[:7] == ["tmux", "new-session", "-d", "-s", "wt-nav", "-c", str(tmp_path)]
    assert f"--resume={SID}" in argv[17]


def test_a_missing_transcript_restores_fresh(tmp_path):
    p = plan(tmp_path, record(str(tmp_path)))
    assert p.ok and p.resume is None and p.row()["resumable"] is False


def test_the_snapshot_count_skips_the_transcript_glob(tmp_path):
    p = plan(tmp_path, record(str(tmp_path)), check_transcript=False)
    assert p.resume == [f"--resume={SID}"]


def test_a_gone_cwd_is_refused_never_relocated(tmp_path):
    p = plan(tmp_path, record(str(tmp_path / "removed-worktree")))
    assert (p.ok, p.reason) == (False, "cwd gone")


def test_an_unknown_profile_is_refused(tmp_path):
    p = plan(tmp_path, record(str(tmp_path), profile="claude-gone"))
    assert p.reason == "unknown profile"


def test_a_missing_cli_is_refused(tmp_path, monkeypatch):
    import tldrack.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "CLI_PROBE_DIRS", ())  # `which` alone decides
    p = plan(tmp_path, record(str(tmp_path), profile="codex", kind="codex", agent=None), profiles=registry(tmp_path, codex=False))
    assert p.reason == "cli missing"


def test_a_conversation_open_elsewhere_is_refused(tmp_path):
    p = plan(tmp_path, record(str(tmp_path)), open_ids={SID: "vc-7"})
    assert p.reason == "conversation open in vc-7"
    assert "agent_session_id" not in p.row() and SID not in str(p.row())


def test_an_unstamped_shell_comes_back_as_the_shell_profile(tmp_path):
    profiles = registry(tmp_path)
    p = plan(tmp_path, record(str(tmp_path), profile=None, kind=None, agent=None), profiles=profiles)
    assert (p.ok, p.spawn_profile, p.kind) == (True, "shell", "shell")
    assert restore_argv(p, profiles)[13:19] == [";", "set-option", "-t", "=wt-nav:", "@vc_profile", "shell"]


def test_codex_does_not_resume_yet():
    assert resume_args("codex", SID) is None


def test_resume_args_refuses_a_flag_shaped_id():
    assert resume_args("claude", "--dangerously-skip-permissions") is None
    assert resume_args("claude", SID) == [f"--resume={SID}"]


def test_open_conversations_maps_ids_to_sessions():
    index = {
        "vc-7": IndexEntry("vc-7", "busy", 1.0, None, SID, 1, "x"),
        "vc-8": IndexEntry("vc-8", "busy", 1.0, None, None, 2, "x"),
    }
    assert open_conversations(index) == {SID: "vc-7"}


def mode_registry(tmp_path, claude_help=CLAUDE_AUTO_HELP):
    """`registry`, with real CLI files whose `--help` offers (or not) the launch mode."""
    (tmp_path / ".claude-a" / "projects" / "p").mkdir(parents=True, exist_ok=True)
    bins = {name: tmp_path / name for name in ("claude", "codex")}
    for path in bins.values():
        path.write_text("#!/bin/sh\n")
    helps = {"claude": claude_help, "codex": "Options:\n      --approve-for-me\n          Route approvals\n"}
    return ProfileRegistry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False),
            Profile("shell", "SH", "shell", None, False),
        ],
        "claude-a",
        home=tmp_path,
        source="file",
        which=lambda name: str(bins[name]) if name in bins else None,
        cli_help=lambda path: helps[os.path.basename(path)],
    )


@pytest.mark.parametrize(
    ("launch", "live", "replayed"),
    [
        ("auto", None, "auto"),  # no hook yet: trust the launch
        ("auto", "auto", "auto"),
        ("auto", "manual", None),  # the operator switched it out: never replay auto over that
        ("auto", "plan", None),
        (None, "auto", None),  # switched INTO auto by hand: restore stays conservative
        ("bypassPermissions", None, None),
    ],
)
def test_restore_mode_replays_the_launch_only_while_the_session_stayed_in_it(launch, live, replayed):
    assert restore_mode({"launch_permission_mode": launch, "live_permission_mode": live}) == replayed


def test_a_restore_comes_back_in_auto_and_says_so_on_the_offer(tmp_path):
    profiles = mode_registry(tmp_path)
    (tmp_path / ".claude-a" / "projects" / "p" / f"{SID}.jsonl").write_text("")
    crew = plan(tmp_path, {**record(str(tmp_path)), "launch_permission_mode": "auto"}, profiles=profiles)
    assert crew.permission_mode == "auto" and crew.row()["permission_mode"] == "auto"
    argv = restore_argv(crew, profiles)
    assert "--permission-mode auto" in argv[17] and f"--resume={SID}" in argv[17]
    assert "@vc_permission_mode" in argv, "restamped, so a second restore keeps it too"
    plain = plan(tmp_path, record(str(tmp_path)), profiles=profiles)
    assert "permission_mode" not in plain.row()
    assert "--permission-mode" not in restore_argv(plain, profiles)[17]


def test_a_switched_out_crew_restores_in_the_default_launch(tmp_path):
    profiles = mode_registry(tmp_path)
    p = plan(
        tmp_path, {**record(str(tmp_path)), "launch_permission_mode": "auto", "live_permission_mode": "manual"}, profiles=profiles
    )
    assert p.permission_mode is None and "--permission-mode" not in restore_argv(p, profiles)[17]


def test_a_cli_that_can_no_longer_start_auto_restores_in_the_default_launch(tmp_path):
    profiles = mode_registry(tmp_path, claude_help='Options:\n  --permission-mode <mode>  (choices: "plan")\n')
    p = plan(tmp_path, {**record(str(tmp_path)), "launch_permission_mode": "auto"}, profiles=profiles)
    assert p.ok and p.permission_mode is None
    assert "--permission-mode" not in restore_argv(p, profiles)[17]
    counted = plan(
        tmp_path, {**record(str(tmp_path)), "launch_permission_mode": "auto"}, profiles=profiles, check_transcript=False
    )
    assert counted.ok, "the /snapshot count never probes a CLI"


def test_a_codex_crew_restores_fresh_and_still_in_auto_review(tmp_path):
    profiles = mode_registry(tmp_path)
    p = plan(
        tmp_path, {**record(str(tmp_path), profile="codex", kind="codex"), "launch_permission_mode": "auto"}, profiles=profiles
    )
    assert p.ok and p.resume is None
    assert "--approve-for-me" in restore_argv(p, profiles)[17]
