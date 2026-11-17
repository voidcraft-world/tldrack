"""resume.py — the pure halves (argv, session name, refusal table) plus the
route's two load-bearing behaviours: it refuses BEFORE spawning, and a second
RESUME on the same finding never forks a second agent.

Nothing here starts a process. The whole point of keeping the argv, the name and
the decision table pure is that the machine-acting route can be pinned without
a tmux server anywhere near the test run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from pathlib import Path

import pytest
from fakes import FakeContext

from coordinator import plugin as plugin_module
from coordinator import sources
from coordinator.plugin import CoordinatorPlugin
from coordinator.resume import (
    PANE_CD_SCRIPT,
    PROBE_TIMEOUT_SECONDS,
    SESSION_ID_CHARS,
    SLUG_MAX,
    build_resume_command,
    decide_resume,
    inline_env,
    is_duplicate_session_error,
    is_session_id,
    resolve_cwd,
    session_exists,
    session_name_for,
    slugify,
)

SESSION_ID = "1f2e3d4c-5b6a-7988-9a0b-c1d2e3f4a5b6"


def finding(key: str = "1f2e3d4c:open-offer", **overrides) -> dict:
    row = {
        "key": key,
        "kind": "open-offer",
        "severity": 3,
        "headline": "h",
        "label": "Agent rack pop-out",
        "session_id": SESSION_ID,
        "cwd": "/repo/web",
        "last_active": "2026-08-02T10:00:00Z",
        "age_hours": 2.0,
        "work_class": None,
    }
    row.update(overrides)
    return row


# ── the argv (quoting is load-bearing) ─────────────────────────────────────


def test_build_resume_command_exact_argv():
    command = build_resume_command("/Users/dev/.local/bin/claude", SESSION_ID, "resume-agent-rack-1f2e3d4c", Path("/repo"))
    assert command == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "resume-agent-rack-1f2e3d4c",
        "-c",
        "/repo",
        # The pane changes into /repo itself: a tmux server whose own directory is gone ignores `-c`.
        *("/bin/zsh", "-f", "-c", PANE_CD_SCRIPT, "zsh", "/repo"),
        "/bin/zsh",
        "-l",
        "-i",
        "-c",
        f'/Users/dev/.local/bin/claude --resume={SESSION_ID}; echo; echo "[claude exited $?]"; exec /bin/zsh -l',
    ]


def test_the_pane_survives_claude_exiting():
    # Without the trailing exec the tmux session dies the moment the user quits
    # claude, and the rack row vanishes with it.
    inner = build_resume_command("/bin/claude", SESSION_ID, "resume-x", Path("/repo"))[-1]
    assert inner.endswith("; exec /bin/zsh -l")


def test_build_resume_command_quotes_the_session_id_and_the_binary():
    # A session id is a uuid today, but it reaches this call from a
    # hand-editable JSON store — this quoting is the only thing between that
    # file and a login shell.
    command = build_resume_command("/o pt/claude", "id with 'quotes' ; rm -rf /", "resume-x", Path("/repo"))
    inner = command[-1]
    assert "'/o pt/claude'" in inner
    assert """'id with '"'"'quotes'"'"' ; rm -rf /'""" in inner
    assert "; rm -rf /;" not in inner  # the injection never reaches the shell unquoted


@pytest.mark.parametrize(
    "hostile",
    [
        "--dangerously-skip-permissions",
        "-p",
        "--append-system-prompt",
        "--resume",
    ],
)
def test_a_flag_shaped_session_id_can_never_become_a_flag(hostile):
    """ARGUMENT injection, which is a different hole from shell injection.

    `-` is inside shlex.quote's safe charset, so a session id that is really a
    flag rides through unquoted and `claude` reads it as one. The id is the STEM
    OF A TRANSCRIPT FILENAME, so anyone who can write into ~/.claude/projects
    picks it. Two layers, both asserted here.
    """
    # Layer 1 — the argv: the value is ATTACHED to --resume, so nothing after
    # the `=` can be read as an option however it starts.
    inner = build_resume_command("/bin/claude", hostile, "resume-x", Path("/repo"))[-1]
    assert f"--resume={shlex.quote(hostile)}" in inner
    # …and it is never a standalone word, which is the only way the shell would
    # hand it to `claude` as an option of its own.
    assert f" {hostile} " not in inner
    assert f" {hostile};" not in inner

    # Layer 2 — the refusal table: it never gets that far in the real route.
    code, payload = decide_resume(**{**clear(), "session_id": hostile})
    assert code == 400
    assert "uuid" in payload["error"]


def test_is_session_id_accepts_a_uuid_and_nothing_creative():
    assert is_session_id(SESSION_ID) is True
    for bad in [
        "",
        "-x",
        "../../etc/passwd",
        "not-a-uuid",
        SESSION_ID + "x",
        SESSION_ID.replace("-", ""),
        None,
        7,
        f"{SESSION_ID}\n",
    ]:
        assert is_session_id(bad) is False, bad


def test_a_cwd_with_spaces_rides_argv_not_the_shell():
    command = build_resume_command("/bin/claude", SESSION_ID, "resume-x", Path("/re po/my app"))
    assert command[6] == "/re po/my app"  # -c arg needs no quoting (argv)
    assert "/re po" not in command[-1]  # and it is not in the command string at all


def test_inline_env_is_quoted_into_the_command_string():
    # tmux sessions inherit the tmux SERVER's env, not ours, so a custom
    # CLAUDE_CONFIG_DIR has to ride inline or the resumed CLI looks for the
    # transcript in ~/.claude and finds nothing.
    inner = build_resume_command(
        "/bin/claude", SESSION_ID, "resume-x", Path("/repo"), {"CLAUDE_CONFIG_DIR": "/home/my dev/.claude"}
    )[-1]
    assert inner.startswith("CLAUDE_CONFIG_DIR='/home/my dev/.claude' /bin/claude --resume")


def test_inline_env_only_carries_what_is_set():
    assert inline_env({}) == {}
    assert inline_env({"CLAUDE_CONFIG_DIR": ""}) == {}
    assert inline_env({"CLAUDE_CONFIG_DIR": "/x/.claude"}) == {"CLAUDE_CONFIG_DIR": "/x/.claude"}


# ── the session name ───────────────────────────────────────────────────────


def test_slugify_lowercases_and_collapses():
    assert slugify("Agent Rack: Pop-out!! (v2)") == "agent-rack-pop-out-v2"
    assert slugify("  ") == ""
    assert slugify(None) == ""
    assert len(slugify("x" * 200)) == SLUG_MAX


def test_session_name_is_readable_and_a_valid_tmux_name():
    name = session_name_for("Agent Rack: Pop-out", SESSION_ID)
    assert name == "resume-agent-rack-pop-out-1f2e3d4c"
    # tmux refuses names containing ':' or '.'; the slug charset rules both out.
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


def test_session_name_is_deterministic_because_that_is_the_idempotency_guard():
    # Two clicks must land on the same name, or the second RESUME spawns a
    # second agent into one transcript. Nothing about live state may enter here.
    first = session_name_for("Agent Rack", SESSION_ID)
    second = session_name_for("Agent Rack", SESSION_ID)
    assert first == second


def test_two_conversations_with_the_same_label_do_not_collide():
    other = "9999aaaa-1111-2222-3333-444455556666"
    assert session_name_for("Agent Rack", SESSION_ID) != session_name_for("Agent Rack", other)
    assert session_name_for("Agent Rack", other).endswith(other[:SESSION_ID_CHARS])


def test_an_unlabelled_finding_still_gets_a_usable_name():
    assert session_name_for("", SESSION_ID) == "resume-1f2e3d4c"
    assert session_name_for("🙂🙂", SESSION_ID) == "resume-1f2e3d4c"  # slugs to nothing
    # signals.py labels a titleless transcript with `session_id[:8]`, which
    # would otherwise print the same eight characters twice.
    assert session_name_for(SESSION_ID[:8], SESSION_ID) == "resume-1f2e3d4c"
    assert session_name_for(None, None) == "resume-session"


# ── the refusal table (everything decided before anything is spawned) ──────


def clear() -> dict:
    return dict(key="1f2e3d4c:open-offer", session_id=SESSION_ID, key_known=True, claude_found=True, tmux_ok=True)


def test_decide_resume_clears_when_everything_is_present():
    assert decide_resume(**clear()) is None


def test_a_conversation_that_is_still_open_is_a_409():
    """The `blocked` rung sorts FIRST in the panel, so this is the likeliest
    row to be clicked and the one where resuming corrupts a live transcript."""
    code, payload = decide_resume(**{**clear(), "session_live": True})
    assert code == 409
    assert "still open" in payload["error"]


def test_the_live_guard_never_outranks_the_session_id_guard():
    """A hostile session id must still 400 rather than 409 — the uuid check is
    an authorization boundary and has to be the thing that answers."""
    code, _ = decide_resume(**{**clear(), "session_id": "--dangerously-skip-permissions", "session_live": True})
    assert code == 400


def test_live_session_ids_spans_every_status_but_needs_a_real_pid(monkeypatch):
    """Wider than `busy_session_ids` on purpose: idle and waiting conversations
    are just as un-resumable. But a crashed session's entry lingers forever, so
    liveness is the pid, never the status word."""
    monkeypatch.setattr(sources, "pid_alive", lambda pid: pid != 999)
    entries = [
        {"sessionId": "a", "status": "busy", "pid": 1},
        {"sessionId": "b", "status": "idle", "pid": 2},
        {"sessionId": "c", "status": "waiting", "pid": 3},
        {"sessionId": "d", "status": "waiting", "pid": 999},  # crashed, entry left behind
        {"sessionId": "", "status": "idle", "pid": 4},  # unusable id
        {"status": "idle", "pid": 5},  # no id at all
    ]
    assert sources.live_session_ids(entries) == {"a", "b", "c"}
    # The narrower predicate must not have moved.
    assert sources.busy_session_ids(entries) == {"a"}


@pytest.mark.parametrize("key", ["", "nope", "no colon here", 7, None, "x" * 200])
def test_a_key_failing_the_shape_guard_is_a_400(key):
    code, payload = decide_resume(**{**clear(), "key": key})
    assert code == 400
    assert "finding key" in payload["error"]


def test_an_unknown_key_is_a_400():
    code, payload = decide_resume(**{**clear(), "key_known": False})
    assert code == 400
    assert "no current finding" in payload["error"]


def test_a_finding_with_no_session_id_is_a_400():
    code, payload = decide_resume(**{**clear(), "session_id": None})
    assert code == 400
    assert "session id" in payload["error"]


def test_no_claude_binary_is_a_503():
    code, payload = decide_resume(**{**clear(), "claude_found": False})
    assert code == 503
    assert "COORDINATOR_CLAUDE_BIN" in payload["error"]


def test_no_tmux_is_a_503():
    code, payload = decide_resume(**{**clear(), "tmux_ok": False})
    assert code == 503
    assert "tmux" in payload["error"]


def test_a_bad_key_refuses_before_the_environment_is_blamed():
    # Shape first: telling the user to install tmux when they sent junk sends
    # them off fixing the wrong machine.
    code, _payload = decide_resume("junk", None, False, False, False)
    assert code == 400


# ── the tmux probe (bounded, because it runs on a request thread) ──────────


def test_session_exists_bounds_the_probe(monkeypatch):
    # A tmux server wedged on a stale socket hangs `has-session` forever. This
    # runs on a bridge REQUEST thread, so an unbounded probe leaks one thread
    # per RESUME click and stalls the status listener every client reads from.
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        seen["command"] = command
        raise AssertionError("unreachable — the assertions are on kwargs")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        session_exists("resume-x")
    assert seen["timeout"] == PROBE_TIMEOUT_SECONDS
    # Both budgets together must stay well inside a client's own request timeout,
    # or the panel reports a failure for a session that is in fact coming up.
    assert PROBE_TIMEOUT_SECONDS < 20


def test_a_wedged_probe_answers_not_there_rather_than_raising(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", timeout)
    assert session_exists("resume-x") is False


# ── the check-then-act race ────────────────────────────────────────────────


def test_tmux_refusing_a_duplicate_name_is_a_SUCCESS_not_a_500():
    # `session_exists` + `new-session` is a check-then-act, so a concurrent
    # RESUME can slip between them. tmux settles that atomically — but as a
    # FAILURE, and a 500 would tell the user their agent did not come up when
    # it did. Same outcome as the guard: it is there, `created` is False.
    assert is_duplicate_session_error("duplicate session: resume-x") is True
    assert is_duplicate_session_error("can't find session") is False
    assert is_duplicate_session_error(None) is False


def test_a_duplicate_session_reports_the_idempotent_answer(coordinator, spawns, monkeypatch):
    monkeypatch.setattr(
        plugin_module,
        "spawn_resume_session",
        lambda *args, **kwargs: (200, {"session": args[2], "created": False}),
    )
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 200
    assert payload["resumed"]["created"] is False
    assert payload["resumed"]["session"] == "resume-agent-rack-pop-out-1f2e3d4c"


def test_two_concurrent_resumes_spawn_exactly_once(coordinator, monkeypatch):
    """The guard and the spawn are ONE critical section.

    Without the lock both threads observe "not there" — the first has not
    created the session yet — and both spawn. Modeled here by making the probe
    slow: with the check-then-act serialized, thread #2 blocks and then sees
    what thread #1 made.
    """
    live: set[str] = set()
    spawned: list[str] = []
    spawn_lock = threading.Lock()

    def slow_exists(name: str) -> bool:
        there = name in live
        threading.Event().wait(0.05)  # the window the race lives in
        return there

    def fake_spawn(claude_bin, session_id, session, cwd, env=None):
        with spawn_lock:
            if session in live:
                return 200, {"session": session, "created": False}
            live.add(session)
            spawned.append(session)
        return 200, {"session": session, "created": True}

    monkeypatch.setattr(plugin_module, "tmux_found", lambda: True)
    monkeypatch.setattr(plugin_module, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(plugin_module, "session_exists", slow_exists)
    monkeypatch.setattr(plugin_module, "spawn_resume_session", fake_spawn)

    results: list[tuple] = []

    def click():
        results.append(coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"}))

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(spawned) == 1
    # Neither click is reported as a failure, and both name the same session.
    assert [status for status, _ in results] == [200, 200]
    assert {payload["resumed"]["session"] for _status, payload in results} == set(spawned)
    assert sorted(payload["resumed"]["created"] for _status, payload in results) == [False, True]


# ── the working directory ──────────────────────────────────────────────────


def test_resolve_cwd_prefers_the_findings_own_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert resolve_cwd(str(project), str(tmp_path)) == project


def test_a_vanished_cwd_falls_back_to_the_bridges_start_dir(tmp_path):
    # Worktrees and scratch clones get deleted; `tmux new-session -c <gone>`
    # fails outright, so this fallback is what keeps RESUME off a 500.
    gone = tmp_path / "deleted-worktree"
    assert resolve_cwd(str(gone), str(tmp_path)) == tmp_path
    assert resolve_cwd(None, str(tmp_path)) == tmp_path


def test_both_directories_gone_still_answers_something_that_exists(tmp_path):
    assert resolve_cwd(str(tmp_path / "a"), str(tmp_path / "b")) == Path.home()


# ── the route ──────────────────────────────────────────────────────────────


@pytest.fixture()
def coordinator(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_COORDINATOR_PATH", str(tmp_path / "store.json"))
    plugin = CoordinatorPlugin(FakeContext())
    # Seeded straight into the store rather than through an audit: this file is
    # about the resume path, and a scan would walk the test machine's transcripts.
    plugin._store.set_scan(
        {"scanned_at": "2026-08-02T12:00:00Z", "scanned": 1, "ranked": False, "error": None, "findings": [finding()]}
    )
    return plugin


@pytest.fixture()
def spawns(monkeypatch):
    """A machine where tmux and claude both exist and no session is open yet."""
    calls = []

    def fake_spawn(claude_bin, session_id, session, cwd, env=None):
        calls.append({"claude_bin": claude_bin, "session_id": session_id, "session": session, "cwd": cwd, "env": env})
        return 200, {"session": session, "created": True}

    monkeypatch.setattr(plugin_module, "tmux_found", lambda: True)
    monkeypatch.setattr(plugin_module, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(plugin_module, "session_exists", lambda name: False)
    monkeypatch.setattr(plugin_module, "spawn_resume_session", fake_spawn)
    return calls


def test_resume_is_advertised_as_a_route(coordinator):
    assert "/plugin/coordinator/resume" in coordinator.routes()


def test_resume_answers_a_full_state_plus_the_resumed_block(coordinator, spawns):
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 200
    # `resumed` is ADDITIVE: the envelope is still a CoordinatorState, because
    # a client's parser is total and would drop anything it did not expect.
    assert payload["status"] == "idle"
    assert payload["findings"][0]["key"] == "1f2e3d4c:open-offer"
    assert payload["resumed"] == {"key": "1f2e3d4c:open-offer", "session": "resume-agent-rack-pop-out-1f2e3d4c", "created": True}
    assert spawns[0]["session_id"] == SESSION_ID


def test_the_key_is_resolved_to_a_session_id_by_the_plugin(coordinator, spawns):
    # The bus only ever carries `{action, key}` — the id never crosses it.
    coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert spawns[0]["session_id"] == SESSION_ID
    assert spawns[0]["claude_bin"] == "/bin/claude"


def test_an_existing_session_short_circuits_the_spawn(coordinator, spawns, monkeypatch):
    # Idempotency: clicking RESUME twice must select the pane that is already
    # there, never fork a second agent into the same transcript.
    monkeypatch.setattr(plugin_module, "session_exists", lambda name: True)
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 200
    assert payload["resumed"] == {"key": "1f2e3d4c:open-offer", "session": "resume-agent-rack-pop-out-1f2e3d4c", "created": False}
    assert spawns == []


def test_the_second_click_asks_about_the_session_the_first_one_created(coordinator, spawns, monkeypatch):
    """The end-to-end idempotency property, with the tmux side faked as a set of
    live session names: click #2 must ask about the exact name click #1 made."""
    live: set[str] = set()
    asked: list[str] = []

    def fake_exists(name: str) -> bool:
        asked.append(name)
        return name in live

    def fake_spawn(claude_bin, session_id, session, cwd, env=None):
        live.add(session)
        spawns.append({"session": session, "session_id": session_id})
        return 200, {"session": session, "created": True}

    monkeypatch.setattr(plugin_module, "session_exists", fake_exists)
    monkeypatch.setattr(plugin_module, "spawn_resume_session", fake_spawn)

    first = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})[1]
    second = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})[1]

    assert asked == [first["resumed"]["session"]] * 2
    assert second["resumed"]["session"] == first["resumed"]["session"]
    assert first["resumed"]["created"] is True
    assert second["resumed"]["created"] is False
    assert len(spawns) == 1


def test_an_unknown_key_is_a_400_and_spawns_nothing(coordinator, spawns):
    status, payload = coordinator.handle_post("/resume", {}, {"key": "ffffffff:open-offer"})
    assert status == 400
    assert "error" in payload
    assert spawns == []


@pytest.mark.parametrize("body", [{}, None, {"key": ""}, {"key": "nope"}, {"key": 7}])
def test_a_malformed_resume_key_is_a_400_and_spawns_nothing(coordinator, spawns, body):
    status, payload = coordinator.handle_post("/resume", {}, body)
    assert status == 400
    assert "error" in payload
    assert spawns == []


def test_no_tmux_is_a_503_before_anything_is_spawned(coordinator, spawns, monkeypatch):
    monkeypatch.setattr(plugin_module, "tmux_found", lambda: False)
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 503
    assert "tmux" in payload["error"]
    assert spawns == []


def test_no_claude_binary_is_a_503_before_anything_is_spawned(coordinator, spawns, monkeypatch):
    monkeypatch.setattr(plugin_module, "resolve_claude_bin", lambda: None)
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 503
    assert "claude" in payload["error"]
    assert spawns == []


def test_a_vanished_cwd_does_not_fail_the_spawn(coordinator, spawns, tmp_path):
    coordinator._store.set_scan(
        {
            "scanned_at": None,
            "scanned": 1,
            "ranked": False,
            "error": None,
            "findings": [finding(cwd=str(tmp_path / "worktree-that-was-deleted"))],
        }
    )
    coordinator._context.start_dir = str(tmp_path)
    coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert spawns[0]["cwd"] == tmp_path


def test_a_failed_spawn_is_reported_as_is(coordinator, spawns, monkeypatch):
    monkeypatch.setattr(
        plugin_module, "spawn_resume_session", lambda *args, **kwargs: (500, {"error": "tmux new-session failed"})
    )
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 500
    assert payload["error"] == "tmux new-session failed"


def test_the_key_may_also_arrive_as_a_query_param(coordinator, spawns):
    status, payload = coordinator.handle_post("/resume", {"key": ["1f2e3d4c:open-offer"]}, None)
    assert status == 200
    assert payload["resumed"]["created"] is True


# ── RESUME under the account that owns the transcript ─────────────────────


class FakeProfiles:
    """The bridge's registry, as the plugin may use it."""

    def __init__(self, roots: dict):
        self.roots = roots  # profile id -> root dir
        self.default_id = "claude-b"
        self.spawned = []

    def claude_roots(self):
        return [str(root) for root in self.roots.values()]

    def profile_for_path(self, path, *, kind=None, first_on_tie=False):  # noqa: ARG002
        for profile_id, root in self.roots.items():
            if path.startswith(str(root) + "/"):
                return profile_id
        return None

    def default_of_kind(self, kind):  # noqa: ARG002
        return self.default_id

    def spawn_argv(self, profile_id, session, cwd, extra_args=(), cli_bin=None):
        self.spawned.append({"profile": profile_id, "session": session, "extra_args": list(extra_args), "cli_bin": cli_bin})
        return ["tmux", "new-session", "-s", session]


@pytest.fixture()
def profiled(coordinator, tmp_path, monkeypatch):
    primary = tmp_path / ".claude-a"
    secondary = tmp_path / ".claude"
    (primary / "projects" / "p").mkdir(parents=True)
    (primary / "projects" / "p" / f"{SESSION_ID}.jsonl").write_text("")
    (secondary / "projects").mkdir(parents=True)
    profiles = FakeProfiles({"claude-b": secondary, "claude-a": primary})
    coordinator._context.profiles = profiles
    ran = []
    monkeypatch.setattr(
        plugin_module, "run_resume_spawn", lambda argv, session: ran.append(argv) or (200, {"session": session, "created": True})
    )
    monkeypatch.setattr(plugin_module, "tmux_found", lambda: True)
    monkeypatch.setattr(plugin_module, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(plugin_module, "session_exists", lambda name: False)
    return coordinator, profiles, ran


def test_resume_runs_under_the_account_that_owns_the_transcript(profiled):
    coordinator, profiles, ran = profiled
    status, payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 200 and payload["resumed"]["created"] is True
    assert profiles.spawned == [
        {
            "profile": "claude-a",
            "session": "resume-agent-rack-pop-out-1f2e3d4c",
            "extra_args": [f"--resume={SESSION_ID}"],
            "cli_bin": "/bin/claude",
        }
    ]
    assert len(ran) == 1


def test_a_transcript_found_nowhere_falls_back_to_the_default_claude(profiled, tmp_path):
    coordinator, profiles, _ran = profiled
    (tmp_path / ".claude-a" / "projects" / "p" / f"{SESSION_ID}.jsonl").unlink()
    coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert profiles.spawned[-1]["profile"] == "claude-b"


def test_a_live_conversation_on_the_second_account_is_still_refused(profiled, tmp_path, monkeypatch):
    # The open-transcript guard must read EVERY account's live index.
    coordinator, profiles, _ran = profiled
    sessions = tmp_path / ".claude-a" / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": SESSION_ID, "status": "idle"}))
    status, _payload = coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert status == 409
    assert profiles.spawned == []


def test_resume_takes_its_args_from_the_hosts_resume_args(profiled):
    # tldrack profiles.resume_args owns the `=`-attached fence; a host
    # that has it is asked, rather than this plugin restating the shape.
    coordinator, profiles, _ran = profiled
    asked = []

    def resume_args(kind, agent_session_id):
        asked.append((kind, agent_session_id))
        return [f"--resume={agent_session_id}", "--from-host"]

    profiles.resume_args = resume_args
    coordinator.handle_post("/resume", {}, {"key": "1f2e3d4c:open-offer"})
    assert asked == [("claude", SESSION_ID)]
    assert profiles.spawned[-1]["extra_args"] == [f"--resume={SESSION_ID}", "--from-host"]
