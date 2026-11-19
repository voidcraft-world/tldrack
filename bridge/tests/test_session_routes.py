"""POST /kill-session and POST /rename-session actually answer.

Both routes carried a `NameError` in their SUCCESS branch — an f-string
interpolating `origin`, a local that stopped existing when the Origin guard was
extracted into `_origin_allowed`. `/shutdown` re-bound it under a new name and
kept working; these two did not, so tmux did the work and the caller got a 500,
with `notifier.forget` / `notify_snapshot` / the 200 all skipped.

It survived 391 passing tests because every one of them targets a pure function.
Nothing in this suite had ever built the HTTP handler, so no test could reach a
handler body at all — which is the real gap, and the reason this file exists
rather than a narrower assertion about one name.
"""

import io
import json
import subprocess

import pytest

from tldrack.icons import SessionIconStore
from tldrack.main import (
    ShutdownControl,
    StatusStore,
    build_status_handler,
)
from tldrack.models import SessionModelStore
from tldrack.session_names import SessionNameStore


class Recorder:
    """Captures what the handler sent instead of writing to a socket."""

    def __init__(self):
        self.status = None
        self.body = None


@pytest.fixture
def handler(tmp_path):
    """A real handler instance with no socket behind it.

    `object.__new__` skips `BaseHTTPRequestHandler.__init__`, which would try to
    read and answer a request off a connection. Every attribute the routes under
    test touch is set by hand below; anything else missing surfaces as an
    AttributeError, which is the honest failure for "this route needs more than
    the harness gives it".
    """
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "started_at": 0},
        control=ShutdownControl("status-only"),
        auto_names=SessionNameStore(tmp_path / "names.json"),
    )
    h = object.__new__(cls)
    h.headers = {}
    h.path = "/"
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    # Loopback is allowed by the shipped default, so the guard passes and the
    # tests exercise the success branch rather than the 403.
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    return h


def _tmux_ok(monkeypatch, stdout="", stderr=""):
    """Make every tmux call report success."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, stdout, stderr),
    )


def test_kill_session_answers_200(handler, monkeypatch):
    _tmux_ok(monkeypatch)
    handler.path = "/kill-session?session=tldrack"

    handler._handle_kill_session()

    assert handler.recorded.status == 200, handler.recorded.body
    assert handler.recorded.body == {"ok": True, "killed": True}


def _with_body(handler, payload):
    """Give the handler a real request body — `_read_json_object` reads the socket."""
    raw = json.dumps(payload).encode()
    handler.headers = {**handler.headers, "Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    return handler


def test_rename_session_answers_200(handler, monkeypatch):
    _tmux_ok(monkeypatch)
    handler.path = "/rename-session"
    _with_body(handler, {"session": "tldrack", "name": "renamed"})

    handler._handle_rename_session()

    assert handler.recorded.status == 200, handler.recorded.body
    assert handler.recorded.body["ok"] is True
    assert handler.recorded.body["session"] == "renamed"


def test_origin_is_logged_not_crashed(handler, monkeypatch, capsys):
    """The log line that carried the bug still reports the caller's origin."""
    _tmux_ok(monkeypatch)
    handler.path = "/kill-session?session=tldrack"

    handler._handle_kill_session()

    assert "127.0.0.1:5173" in capsys.readouterr().out


def test_requesting_origin_reports_none_when_absent(handler):
    """A native client sends no Origin; the accessor must return None, not raise."""
    handler.headers = {}
    assert handler._requesting_origin() is None


# ── POST /spawn and the launch profiles ────────────────────────────────────

from tldrack import main as main_module  # noqa: E402
from tldrack.permission_ledger import PermissionLedger  # noqa: E402
from tldrack.snapshot import PaneSessionCache  # noqa: E402
from tldrack.profiles import Profile, ProfileRegistry  # noqa: E402


@pytest.fixture
def spawner(tmp_path):
    """A handler WITH a profile registry, plus the stores behind it."""
    (tmp_path / ".claude-a").mkdir()
    registry = ProfileRegistry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("codex", "CODEX", "codex", str(tmp_path / ".codex"), False, ("-m", "gpt-5-codex"), "gpt-5-codex"),
            Profile("shell", "SH", "shell", None, False),
        ],
        "claude-a",
        home=tmp_path,
        source="file",
        which=lambda name: None if name == "codex" else f"/usr/bin/{name}",
    )
    store = StatusStore()
    model_store = SessionModelStore(tmp_path / "models.json")
    cls = build_status_handler(
        store=store,
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=model_store,
        default_session="tldrack",
        bridge_info={"pid": 1234, "status_port": 7682, "started_at": 0},
        control=ShutdownControl("status-only"),
        start_dir=str(tmp_path),
        profiles=registry,
        permissions=PermissionLedger(None),
        pane_sessions=PaneSessionCache(read=lambda: {"%7": "vc-2"}),
    )
    h = object.__new__(cls)
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h.path = "/spawn"
    h.store, h.model_store, h.tmp = store, model_store, tmp_path
    return h


def _record_tmux(monkeypatch, live=(), returncode=0, stderr=""):
    calls = []
    monkeypatch.setattr(main_module, "list_tmux_sessions", lambda: {name: {} for name in live})

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_spawn_answers_201_and_runs_the_profiles_stamped_argv(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a"})

    spawner._handle_spawn()

    assert spawner.recorded.status == 201, spawner.recorded.body
    assert spawner.recorded.body == {"ok": True, "session": "vc-2", "profile": "claude-a"}
    (argv,) = calls
    assert argv[:7] == ["tmux", "new-session", "-d", "-s", "vc-2", "-c", str(spawner.tmp)]
    assert argv[18:24] == [";", "set-option", "-t", "=vc-2:", "@vc_profile", "claude-a"]
    assert f"CLAUDE_CONFIG_DIR={spawner.tmp}/.claude-a" in argv[17]


def test_spawn_hands_a_prompt_to_the_cli_as_its_first_message(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "codex-0", "profile": "claude-a", "prompt": "Read /tmp/01.txt and follow it; it's your task."})

    spawner._handle_spawn()

    assert spawner.recorded.status == 201, spawner.recorded.body
    assert spawner.recorded.body["prompt"] is True
    (argv,) = calls
    inner = argv[17]
    assert """'Read /tmp/01.txt and follow it; it'"'"'s your task.'; echo;""" in inner


def test_spawn_takes_a_launch_model_and_refuses_a_pair_that_would_not_hold(spawner, monkeypatch):
    # profiles.py § LAUNCH MODELS owns the rules; this is the route handing them through.
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a", "model": "opus"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 201, spawner.recorded.body
    assert spawner.recorded.body["model"] == "opus"
    (argv,) = calls
    assert " --model opus;" in argv[17] and argv[argv.index("@vc_model") + 1] == "opus"

    for body, why in (
        ({"model": "opus", "subagent_model": "opus"}, "not weaker"),
        ({"model": "gpt-5-codex"}, "unknown launch model"),
    ):
        _with_body(spawner, {"name": "vc-3", "profile": "claude-a", **body})
        spawner._handle_spawn()
        assert spawner.recorded.status == 400 and why in spawner.recorded.body["error"]
    assert len(calls) == 1, "a refused pair starts nothing"


def test_spawn_carries_explicit_parent_to_the_child_stamp(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    lineage = {
        "id": "00000000-0000-0000-0000-000000000002",
        "parent_id": "00000000-0000-0000-0000-000000000001",
        "parent_name": "parent",
    }
    parents = []
    monkeypatch.setattr(ProfileRegistry, "prepare_lineage", lambda self, parent: parents.append(parent) or lineage)
    _with_body(spawner, {"name": "child", "profile": "shell", "parent_session": "parent"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 201
    assert parents == ["parent"]
    assert json.loads(calls[0][-1]) == lineage


def test_bad_parent_does_not_start_or_clear_a_session(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    spawner.store.set("child", "attention", "old")
    _with_body(spawner, {"name": "child", "profile": "shell", "parent_session": "bad;name"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 400
    assert calls == []
    assert spawner.store.get("child")["state"] == "attention"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "vc-2", "profile": "claude-a", "prompt": "--dangerously-skip-permissions"},
        {"name": "vc-2", "profile": "claude-a", "prompt": "line one\nline two"},
        {"name": "vc-2", "profile": "claude-a", "prompt": "x" * 1001},
        {"name": "vc-2", "profile": "claude-a", "prompt": "   "},
        {"name": "vc-2", "profile": "claude-a", "prompt": 7},
        {"name": "vc-2", "profile": "shell", "prompt": "hello"},
    ],
)
def test_spawn_refuses_a_bad_prompt_with_400(spawner, monkeypatch, body):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, body)
    spawner._handle_spawn()
    assert spawner.recorded.status == 400, spawner.recorded.body
    assert calls == []


def test_spawn_on_a_taken_name_is_409_and_runs_nothing(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch, live=["vc-2"])
    _with_body(spawner, {"name": "vc-2", "profile": "shell"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 409
    assert calls == []


def test_spawn_losing_tmuxs_own_race_is_409_too(spawner, monkeypatch):
    _record_tmux(monkeypatch, returncode=1, stderr="duplicate session: vc-2")
    _with_body(spawner, {"name": "vc-2", "profile": "shell"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 409


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad name", "profile": "shell"},
        {"name": "vc-2", "profile": "Nope!"},
        {"name": "vc-2", "profile": "unknown-profile"},
        {"name": "vc-2"},
    ],
)
def test_spawn_refuses_a_bad_body_with_400(spawner, monkeypatch, body):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, body)
    spawner._handle_spawn()
    assert spawner.recorded.status == 400
    assert calls == []


def test_spawn_without_the_cli_is_503(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-2", "profile": "codex"})  # `which` finds no codex
    monkeypatch.setattr(main_module, "list_tmux_sessions", dict)
    import tldrack.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "CLI_PROBE_DIRS", ())
    spawner._handle_spawn()
    assert spawner.recorded.status == 503
    assert calls == []


def test_spawn_from_a_foreign_origin_is_403(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    spawner.headers = {"Origin": "https://evil.example"}
    _with_body(spawner, {"name": "vc-2", "profile": "shell"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 403
    assert calls == []


def test_a_reused_name_does_not_inherit_its_predecessor(spawner, monkeypatch):
    # `vc-3` last ran Opus; a fresh agent spawned under the freed name must not
    # open wearing that label (merge_snapshot's disk fallback) or that state.
    spawner.store.set("vc-3", "attention", "old question")
    spawner.model_store.set("vc-3", "claude-opus-4-8")
    _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-3", "profile": "shell"})

    spawner._handle_spawn()

    assert spawner.recorded.status == 201
    assert spawner.store.get("vc-3")["state"] == "unknown"
    assert "vc-3" not in spawner.model_store.get_all()


def test_spawn_starts_in_the_asked_permission_mode_or_refuses_before_tmux(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a", "permission_mode": "yolo"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 400 and "unknown permission mode" in spawner.recorded.body["error"]
    _with_body(spawner, {"name": "vc-2", "profile": "shell", "permission_mode": "auto"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 400 and "has no 'auto' mode" in spawner.recorded.body["error"]
    assert calls == []

    from tldrack.profiles import ProfileRegistry

    monkeypatch.setattr(ProfileRegistry, "permission_refusal", lambda *_args, **_kwargs: None)
    _with_body(spawner, {"name": "vc-2", "profile": "claude-a", "permission_mode": "auto"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 201, spawner.recorded.body
    assert spawner.recorded.body["permission_mode"] == "auto"
    (argv,) = calls
    assert "--permission-mode auto" in " ".join(argv)


def test_a_bridge_without_profiles_answers_spawn_with_503(handler, monkeypatch):
    _record_tmux(monkeypatch)
    handler.path = "/spawn"
    _with_body(handler, {"name": "vc-2", "profile": "shell"})
    handler._handle_spawn()
    assert handler.recorded.status == 503


# ── AUTO: "profile": "auto" is the freest account, picked at click time ─────


def test_snapshot_carries_the_launch_pick_beside_the_profiles(spawner, monkeypatch):
    _record_tmux(monkeypatch)
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    import tldrack.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "CLI_PROBE_DIRS", ())  # this Mac's real codex
    pick = spawner.snapshot_payload()["launch_pick"]
    # codex's CLI is missing and a shell is never a candidate.
    assert pick["id"] == "claude-a"
    assert [row["id"] for row in pick["ranking"]] == ["claude-a"]


def test_spawn_auto_launches_the_picked_profile_and_stamps_the_real_id(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    _with_body(spawner, {"name": "vc-2", "profile": "auto"})

    spawner._handle_spawn()

    assert spawner.recorded.status == 201, spawner.recorded.body
    assert spawner.recorded.body == {"ok": True, "session": "vc-2", "profile": "claude-a", "auto": True}
    (argv,) = calls
    assert argv[18:24] == [";", "set-option", "-t", "=vc-2:", "@vc_profile", "claude-a"]


def test_spawn_auto_with_nothing_launchable_is_503_and_runs_nothing(spawner, monkeypatch):
    calls = _record_tmux(monkeypatch)
    monkeypatch.setattr(ProfileRegistry, "pick_launch", lambda self, *_asked: None)
    _with_body(spawner, {"name": "vc-2", "profile": "auto"})
    spawner._handle_spawn()
    assert spawner.recorded.status == 503
    assert calls == []


# ── POST /hook: Codex events, the profile hint ─────────────────────────────


def _hook(h, event, payload, session="vc-2"):
    raw = json.dumps(payload).encode()
    h.path = f"/hook?session={session}&event={event}"
    h.headers = {**h.headers, "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_hook()
    return h.recorded.status


def _http_hook(h, event, payload, pane):
    raw = json.dumps(payload).encode()
    h.path = f"/hook?event={event}"
    h.headers = {**h.headers, "Content-Length": str(len(raw)), "X-VC-Pane": pane}
    h.rfile = io.BytesIO(raw)
    h._handle_hook()
    return h.recorded


def test_an_http_relay_names_its_pane_and_lands_on_that_session(spawner):
    assert _http_hook(spawner, "UserPromptSubmit", {"prompt": "go"}, "%7").status == 200
    assert spawner.store.get("vc-2")["state"] == "busy"


@pytest.mark.parametrize("pane", ["", "%99", "not-a-pane"])
def test_an_http_relay_outside_tmux_is_ignored_not_filed_under_the_default(spawner, pane):
    recorded = _http_hook(spawner, "UserPromptSubmit", {"prompt": "go"}, pane)
    assert (recorded.status, recorded.body.get("ignored")) == (200, "no tmux session")
    assert spawner.store.get("tldrack")["state"] == "unknown"
    assert spawner.rfile.read() == b""  # the body was drained


def test_the_installed_claude_hooks_are_http_and_codexs_stay_command():
    claude = main_module.build_hooks_settings(7682)["hooks"]["Stop"][0]["hooks"][0]
    assert (claude["type"], claude["url"], claude["headers"]) == (
        "http",
        "http://127.0.0.1:7682/hook?event=Stop",
        {"X-VC-Pane": "$TMUX_PANE"},
    )
    assert claude["allowedEnvVars"] == ["TMUX_PANE"]
    codex = main_module.build_hooks_settings(7682, ("Stop",), transport="command")["hooks"]["Stop"][0]["hooks"][0]
    assert codex["type"] == "command" and "curl" in codex["command"]


def test_permission_requests_land_in_the_ledger_and_its_routes(spawner):
    transcript = str(spawner.tmp / ".codex" / "sessions" / "rollout.jsonl")
    _hook(
        spawner,
        "PermissionRequest",
        {"tool_name": "shell", "tool_input": {"command": "git push"}, "transcript_path": transcript, "model": "gpt-5-codex"},
    )
    _hook(spawner, "PostToolUse", {"tool_name": "shell", "transcript_path": transcript})
    spawner.path = "/permission/ledger"
    spawner._handle_permission_ledger()
    [row] = spawner.recorded.body["rows"]
    assert (row["session"], row["kind"], row["profile"], row["outcome"], row["pattern"]) == (
        "vc-2",
        "codex",
        "codex",
        "approved",
        "shell",
    )
    spawner.path = "/permission/stats?since=0"
    spawner._handle_permission_stats()
    assert (spawner.recorded.body["requests"], spawner.recorded.body["approve_rate"]) == (1, 1.0)


def test_a_codex_permission_request_is_attention_and_names_its_model(spawner):
    status = _hook(
        spawner,
        "PermissionRequest",
        {
            "model": "gpt-5-codex",
            "transcript_path": str(spawner.tmp / ".codex" / "sessions" / "rollout.jsonl"),
        },
    )
    assert status == 200
    row = spawner.store.get("vc-2")
    assert row["state"] == "attention"
    assert row["model"] == "gpt-5-codex"
    assert row["profile"] == "codex"  # its transcript sits under the codex profile's CODEX_HOME


def test_post_tool_use_flips_codex_back_to_busy(spawner):
    _hook(spawner, "PermissionRequest", {})
    _hook(spawner, "PostToolUse", {})
    assert spawner.store.get("vc-2")["state"] == "busy"


@pytest.mark.parametrize("profile", ["codex", "claude-a", "unknown"])
def test_worktree_cwd_survives_codex_turn_hooks_but_claude_can_change_directory(spawner, profile, monkeypatch):
    root = spawner.tmp / {"codex": ".codex", "claude-a": ".claude-a", "unknown": ".other"}[profile]
    path = root / ("sessions/rollout.jsonl" if profile == "codex" else "projects/p/s.jsonl")
    home = str(spawner.tmp / "main")
    tree = str(spawner.tmp / "worktree")
    payload = {"transcript_path": str(path), "cwd": home}
    monkeypatch.setattr(
        main_module,
        "list_tmux_sessions",
        lambda: {
            "vc-2": {"title": None, "attached": False, "cwd": home, "profile": profile},
        },
    )
    _hook(spawner, "SessionStart", payload)
    # Codex local-tool hooks name the execution cwd; turn hooks name the launch cwd.
    _hook(spawner, "PreToolUse", {**payload, "cwd": tree, "tool_name": "Bash"})
    _hook(spawner, "PostToolUse", {**payload, "cwd": tree, "tool_name": "Bash"})
    for event in ("Stop", "UserPromptSubmit", "Interrupt"):
        _hook(spawner, event, payload)
        assert spawner.store.get("vc-2")["cwd"] == (tree if profile == "codex" else home)
        row = next(row for row in spawner.snapshot_payload()["sessions"] if row["session"] == "vc-2")
        assert row["cwd"] == (tree if profile == "codex" else home)
    # An actual command back in main is authoritative for both agents.
    _hook(spawner, "PreToolUse", {**payload, "tool_name": "Bash"})
    assert spawner.store.get("vc-2")["cwd"] == home
    _hook(spawner, "SessionEnd", payload)
    assert spawner.store.get("vc-2")["cwd"] is None


def test_codex_turn_cwd_initializes_missing_state_and_session_start_resets_it(spawner):
    payload = {"transcript_path": str(spawner.tmp / ".codex/sessions/rollout.jsonl"), "cwd": str(spawner.tmp / "main")}
    _hook(spawner, "UserPromptSubmit", payload)
    assert spawner.store.get("vc-2")["cwd"] == payload["cwd"]
    _hook(spawner, "PreToolUse", {**payload, "cwd": str(spawner.tmp / "worktree"), "tool_name": "Bash"})
    _hook(spawner, "SessionStart", payload)
    assert spawner.store.get("vc-2")["cwd"] == payload["cwd"]


@pytest.mark.parametrize(
    "reviewer,tool,turn,state",
    [
        ("auto_review", "Bash", "current", "busy"),
        ("auto_review", "apply_patch", "current", "busy"),
        ("user", "Bash", "current", "attention"),
        (None, "Bash", "current", "attention"),
        ("auto_review", "Bash", "previous", "attention"),
        ("auto_review", "mcp__app__write", "current", "attention"),
    ],
)
def test_codex_automatic_review_does_not_raise_a_human_question(spawner, reviewer, tool, turn, state):
    session_id = "dddddddd-0000-4000-8000-000000000004"
    transcript = spawner.tmp / ".codex" / "sessions" / f"rollout-date-{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn,
                    "approvals_reviewer": reviewer,
                },
            }
        )
        + "\n"
    )
    assert (
        _hook(
            spawner,
            "PermissionRequest",
            {
                "session_id": session_id,
                "turn_id": "current",
                "tool_name": tool,
                "transcript_path": str(transcript),
                "model": "gpt-5-codex",
            },
        )
        == 200
    )
    assert spawner.store.get("vc-2")["state"] == state
    # Classification only: the request remains pending, never auto-approved here.
    spawner.path = "/permission/ledger"
    spawner._handle_permission_ledger()
    [row] = spawner.recorded.body["rows"]
    assert row.get("outcome") is None


def test_task_progress_rides_the_hook_and_outlives_the_turn(spawner):
    _hook(spawner, "PreToolUse", {"tool_name": "TaskCreate", "tool_input": {"subject": "Plan"}})
    _hook(spawner, "PreToolUse", {"tool_name": "TaskCreate", "tool_input": {"subject": "Build"}})
    _hook(spawner, "PreToolUse", {"tool_name": "TaskUpdate", "tool_input": {"taskId": "1", "status": "completed"}})
    _hook(spawner, "Stop", {})
    assert spawner.store.get("vc-2")["tasks"] == {"done": 1, "total": 2, "current": "Build"}
    _hook(spawner, "SessionEnd", {})
    assert spawner.store.get("vc-2")["tasks"] is None


SUBAGENT = {"agent_id": "a7f3", "agent_type": "general-purpose"}


def test_a_subagents_tool_call_cannot_answer_the_main_threads_question(spawner):
    # The 2026-10-15 timeline (main.py § Subagent hooks), through the real route.
    _hook(spawner, "PreToolUse", {"tool_name": "AskUserQuestion", "tool_input": {}})
    _hook(spawner, "Notification", {"message": "Claude needs your input"})
    for _ in range(3):
        assert (
            _hook(spawner, "PreToolUse", {**SUBAGENT, "tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}})
            == 200
        )
    row = spawner.store.get("vc-2")
    assert row["state"] == "attention"
    assert row["activity"]["tool"] == "AskUserQuestion"  # still says what it asked with
    # The answer arrives as the MAIN thread's next tool call.
    _hook(spawner, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert spawner.store.get("vc-2")["state"] == "busy"


def test_a_subagent_still_working_after_the_turn_is_background(spawner):
    _hook(spawner, "Stop", {})
    _hook(
        spawner,
        "PreToolUse",
        {**SUBAGENT, "tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": str(spawner.tmp / "worktree")},
    )
    row = spawner.store.get("vc-2")
    assert row["state"] == "background"
    assert row["cwd"] is None  # a worktree subagent's checkout is not where the session is


def test_an_unknown_event_is_still_400(spawner):
    assert _hook(spawner, "PreCompact", {}) == 400


def test_a_claude_transcript_hints_its_account_and_session_end_clears_it(spawner):
    path = spawner.tmp / ".claude-a" / "projects" / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    _hook(spawner, "SessionStart", {"transcript_path": str(path)})
    assert spawner.store.get("vc-2")["profile"] == "claude-a"
    _hook(spawner, "SessionEnd", {"transcript_path": str(path)})
    assert spawner.store.get("vc-2")["profile"] is None


def test_print_hooks_is_unchanged_by_the_codex_table():
    # A Claude settings.json must not grow events Claude does not have.
    # (PermissionRequest IS Claude's too, since 2.0.45; these two are Codex-only.)
    hooks = main_module.build_hooks_settings(7682)["hooks"]
    assert list(hooks) == [*main_module.HOOK_STATES, *main_module.CLAUDE_MATCHED_HOOKS]
    assert "Interrupt" not in hooks
    # PostToolUse IS Claude's, but relayed for AskUserQuestion only (decisions.py): never every tool call.
    (group,) = hooks["PostToolUse"]
    assert group["matcher"] == "AskUserQuestion"
    assert all("matcher" not in g for event, groups in hooks.items() if event != "PostToolUse" for g in groups)


def test_permission_request_is_attention_and_task_events_are_busy():
    assert main_module.hook_state("PermissionRequest", {}) == "attention"
    assert main_module.hook_state("TaskCreated", {"task_id": "1"}) == "busy"
    assert main_module.hook_state("TaskCompleted", {"task_id": "1"}) == "busy"


# ── POST /profiles/install-hooks ───────────────────────────────────────────


def test_install_hooks_route_merges_into_the_profiles_settings(spawner):
    spawner.path = "/profiles/install-hooks"
    _with_body(spawner, {"profile": "claude-a"})
    spawner._handle_install_hooks()
    assert spawner.recorded.status == 200, spawner.recorded.body
    assert spawner.recorded.body["changed"] is True
    written = json.loads((spawner.tmp / ".claude-a" / "settings.json").read_text())
    assert set(main_module.HOOK_STATES) <= set(written["hooks"])
    assert written["env"]["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "1"

    _with_body(spawner, {"profile": "claude-a"})
    spawner._handle_install_hooks()
    assert spawner.recorded.body["changed"] is False


@pytest.mark.parametrize("body", [{"profile": "shell"}, {"profile": "nope"}, {"profile": 3}, {}])
def test_install_hooks_route_refuses_a_shell_or_unknown_profile(spawner, body):
    spawner.path = "/profiles/install-hooks"
    _with_body(spawner, body)
    spawner._handle_install_hooks()
    assert spawner.recorded.status == 400


def test_install_hooks_route_refuses_a_foreign_origin(spawner):
    spawner.path = "/profiles/install-hooks"
    spawner.headers = {"Origin": "https://evil.example"}
    _with_body(spawner, {"profile": "claude-a"})
    spawner._handle_install_hooks()
    assert not (spawner.tmp / ".claude-a" / "settings.json").exists()


# ── session records + POST /restore ────────────────────────────────────────

from tldrack.closed_sessions import ClosedSessionStore  # noqa: E402
from tldrack.session_records import SessionRecordStore  # noqa: E402
from tldrack.stars import SessionStarStore  # noqa: E402

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"


@pytest.fixture
def restorer(tmp_path):
    """A profiled handler WITH a session-record store (start_status_server's shape)."""
    (tmp_path / ".claude-a" / "projects" / "p").mkdir(parents=True)
    registry = ProfileRegistry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("shell", "SH", "shell", None, False),
        ],
        "claude-a",
        home=tmp_path,
        source="file",
        which=lambda name: f"/usr/bin/{name}",
    )
    records = SessionRecordStore(tmp_path / "records.json")
    closed = ClosedSessionStore(tmp_path / "closed.json")
    names = SessionNameStore(tmp_path / "names.json")
    stars = SessionStarStore(tmp_path / "stars.json")
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "status_port": 7682, "started_at": 0},
        control=ShutdownControl("status-only"),
        start_dir=str(tmp_path),
        profiles=registry,
        records=records,
        closed=closed,
        auto_names=names,
        stars=stars,
    )
    h = object.__new__(cls)
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h.path = "/"
    h.records, h.closed, h.names, h.stars, h.tmp = records, closed, names, stars, tmp_path
    return h


def _record_live(h, monkeypatch, name, cwd):
    """One snapshot pass that sees `name` live in `cwd` — how a record is born."""
    monkeypatch.setattr(
        main_module, "list_tmux_sessions", lambda: {name: {"title": None, "attached": False, "cwd": cwd, "profile": "claude-a"}}
    )
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    h.snapshot_payload()


def test_a_hook_records_the_conversation_and_only_a_user_exit_clears_it(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    _hook(restorer, "UserPromptSubmit", {"session_id": SID})
    assert restorer.records.get("vc-2")["agent_session_id"] == SID
    _hook(restorer, "SessionEnd", {"session_id": SID, "reason": "other"})  # e.g. SIGHUP at reboot
    assert restorer.records.get("vc-2")["agent_session_id"] == SID
    _hook(restorer, "SessionEnd", {"session_id": SID, "reason": "prompt_input_exit"})
    assert restorer.records.get("vc-2")["agent_session_id"] is None


def _rebooted(restorer, monkeypatch, name="wt-nav", cwd=None):
    """A record for `name`, then a tmux server with nothing in it."""
    _record_live(restorer, monkeypatch, name, cwd or str(restorer.tmp))
    restorer.records.note_agent_session(name, SID)
    return _record_tmux(monkeypatch)  # live=() — the reboot


def test_snapshot_advertises_how_many_sessions_can_come_back(restorer, monkeypatch):
    _rebooted(restorer, monkeypatch)
    assert restorer.snapshot_payload()["restorable"] == 1


def test_a_restorable_row_carries_the_name_the_rack_showed(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    _hook(restorer, "UserPromptSubmit", {"session_id": SID, "prompt": "fix the nav sidebar"}, session="vc-2")
    _record_tmux(monkeypatch)  # the reboot
    restorer.path = "/restorable"
    restorer._handle_restorable()
    (row,) = restorer.recorded.body["sessions"]
    assert row["session"] == "vc-2" and row["label"] == "fix the nav sidebar"


def test_restorable_lists_rows_without_the_conversation_id(restorer, monkeypatch):
    _rebooted(restorer, monkeypatch)
    restorer.path = "/restorable"
    restorer._handle_restorable()
    assert restorer.recorded.status == 200
    (row,) = restorer.recorded.body["sessions"]
    assert row["session"] == "wt-nav" and row["ok"] is True
    assert SID not in json.dumps(restorer.recorded.body)


def test_restore_all_resumes_the_conversation_under_the_old_name(restorer, monkeypatch):
    calls = _rebooted(restorer, monkeypatch)
    (restorer.tmp / ".claude-a" / "projects" / "p" / f"{SID}.jsonl").write_text("")
    _with_body(restorer, {})
    restorer._handle_restore()
    assert restorer.recorded.status == 200, restorer.recorded.body
    assert restorer.recorded.body["results"] == [{"session": "wt-nav", "restored": True, "resumed": True}]
    (argv,) = calls
    assert argv[:7] == ["tmux", "new-session", "-d", "-s", "wt-nav", "-c", str(restorer.tmp)]
    assert f"--resume={SID}" in argv[17]


def test_restore_replays_saved_family_identity(restorer, monkeypatch):
    calls = _rebooted(restorer, monkeypatch)
    lineage = {
        "id": "00000000-0000-0000-0000-000000000002",
        "parent_id": "00000000-0000-0000-0000-000000000001",
        "parent_name": "parent",
    }
    restorer.records.observe_live([{"session": "wt-nav", "live": True, "lineage": lineage}], lambda _: "claude")
    _with_body(restorer, {})
    restorer._handle_restore()
    assert restorer.recorded.body["results"][0]["restored"]
    assert json.loads(calls[0][-1]) == lineage


def test_restore_never_relocates_a_gone_cwd(restorer, monkeypatch):
    gone = restorer.tmp / "removed-worktree"
    gone.mkdir()
    calls = _rebooted(restorer, monkeypatch, cwd=str(gone))
    gone.rmdir()
    _with_body(restorer, {"sessions": ["wt-nav"]})
    restorer._handle_restore()
    (result,) = restorer.recorded.body["results"]
    assert result == {"session": "wt-nav", "restored": False, "reason": f"cwd gone: {gone}"}
    assert calls == []


def test_restore_from_a_foreign_origin_is_403(restorer, monkeypatch):
    calls = _rebooted(restorer, monkeypatch)
    restorer.headers = {"Origin": "https://evil.example"}
    _with_body(restorer, {})
    restorer._handle_restore()
    assert restorer.recorded.status == 403
    assert calls == []


def test_restore_refuses_a_live_name_and_a_bad_body(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "wt-nav", str(restorer.tmp))
    calls = _record_tmux(monkeypatch, live=["wt-nav"])
    _with_body(restorer, {"sessions": ["wt-nav"]})
    restorer._handle_restore()
    assert restorer.recorded.body["results"] == [{"session": "wt-nav", "restored": False, "reason": "already running"}]
    _with_body(restorer, {"sessions": ["bad name"]})
    restorer._handle_restore()
    assert restorer.recorded.status == 400
    assert calls == []


def test_restore_forget_drops_the_offer(restorer, monkeypatch):
    _rebooted(restorer, monkeypatch)
    _with_body(restorer, {"sessions": ["wt-nav", "never-recorded"]})
    restorer._handle_restore_forget()
    assert restorer.recorded.body == {"ok": True, "forgotten": ["wt-nav"]}
    assert "restorable" not in restorer.snapshot_payload()


def test_a_killed_session_is_not_offered_back(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    _tmux_ok(monkeypatch)
    restorer.path = "/kill-session?session=vc-2"
    restorer._handle_kill_session()
    assert restorer.records.get("vc-2") is None


# ── the undo list: GET /closed + POST /reopen ──────────────────────────────


def _killed(restorer, monkeypatch, name="vc-2", cwd=None):
    """A live `name` running conversation SID, labelled by its first prompt, then ✕."""
    _record_live(restorer, monkeypatch, name, cwd or str(restorer.tmp))
    _hook(restorer, "UserPromptSubmit", {"session_id": SID, "prompt": "fix the nav sidebar"}, session=name)
    (restorer.tmp / ".claude-a" / "projects" / "p" / f"{SID}.jsonl").write_text("")
    _tmux_ok(monkeypatch)
    restorer.path = f"/kill-session?session={name}"
    restorer._handle_kill_session()
    assert restorer.recorded.body == {"ok": True, "killed": True}


def _closed_rows(restorer):
    restorer.path = "/closed"
    restorer._handle_closed()
    assert restorer.recorded.status == 200, restorer.recorded.body
    return restorer.recorded.body["sessions"]


def _reopen(restorer, closed_id):
    _with_body(restorer, {"id": closed_id})
    restorer._handle_reopen()
    return restorer.recorded


def test_a_killed_session_lands_on_the_closed_list_with_its_label(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    assert row["session"] == "vc-2" and row["label"] == "fix the nav sidebar"
    assert row["how"] == "killed" and row["ok"] is True and row["resumable"] is True
    assert SID not in json.dumps(restorer.recorded.body)
    assert restorer.snapshot_payload()["closed"] == 1


def test_reopen_resumes_the_conversation_under_the_old_name(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    calls = _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    recorded = _reopen(restorer, row["id"])
    assert recorded.status == 200, recorded.body
    assert recorded.body == {"ok": True, "session": "vc-2", "profile": "claude-a", "resumed": True}
    (argv,) = calls
    assert argv[:7] == ["tmux", "new-session", "-d", "-s", "vc-2", "-c", str(restorer.tmp)]
    assert f"--resume={SID}" in argv[17]
    # Taken, and the rack row gets its name back.
    assert restorer.closed.entries() == []
    assert restorer.names.get_all()["vc-2"] == "fix the nav sidebar"


def test_reopen_on_a_reused_generated_name_takes_the_next_free_one(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    calls = _record_tmux(monkeypatch, live=["vc-2"])
    (row,) = _closed_rows(restorer)
    recorded = _reopen(restorer, row["id"])
    assert recorded.body == {"ok": True, "session": "vc-1", "profile": "claude-a", "resumed": True}
    assert calls[0][4] == "vc-1"


def test_a_double_click_reopens_once(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    calls = _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    assert _reopen(restorer, row["id"]).status == 200
    assert _reopen(restorer, row["id"]).status == 404
    assert len(calls) == 1


def test_a_session_closed_by_hand_is_on_the_list_too(restorer, monkeypatch):
    def tmux(*names):
        monkeypatch.setattr(
            main_module,
            "list_tmux_sessions",
            lambda: {name: {"title": None, "attached": False, "cwd": str(restorer.tmp), "profile": "claude-a"} for name in names},
        )

    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    tmux("vc-2", "vc-9", "deploy-dev")
    restorer.snapshot_payload()
    restorer.records.note_agent_session("vc-2", SID)
    tmux("vc-9")  # `tmux kill-session -t vc-2` in some other terminal; the deploy shell ended
    assert restorer.snapshot_payload()["closed"] == 1
    (row,) = _closed_rows(restorer)
    assert row["session"] == "vc-2" and row["how"] == "gone"  # the shell is not a close


def test_a_removed_worktree_is_left_off_the_list(restorer, monkeypatch):
    gone = restorer.tmp / "removed-worktree"
    gone.mkdir()
    _killed(restorer, monkeypatch, cwd=str(gone))
    gone.rmdir()
    _record_tmux(monkeypatch)
    assert _closed_rows(restorer) == []
    assert "closed" not in restorer.snapshot_payload()


def test_reopen_refuses_a_foreign_origin_a_bad_id_and_an_unknown_one(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    calls = _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    restorer.headers = {"Origin": "https://evil.example"}
    assert _reopen(restorer, row["id"]).status == 403
    restorer.headers = {"Origin": "http://127.0.0.1:5173"}
    assert _reopen(restorer, "--resume").status == 400
    assert _reopen(restorer, "0123456789ab").status == 404
    assert calls == []


# ── stars: POST /session-star, and what a close and a reopen do to one (stars.py) ──


def _star(h, session, starred=True):
    h.path = "/session-star"
    _with_body(h, {"session": session, "starred": starred})
    h._handle_session_star()
    return h.recorded


def _star_row(h, session):
    return next(row for row in h.snapshot_payload()["sessions"] if row["session"] == session)


def test_a_star_rides_the_row_and_every_row_says_whether_it_has_one(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    assert _star_row(restorer, "vc-2")["starred"] is False
    assert _star(restorer, "vc-2").body == {"ok": True, "session": "vc-2", "starred": True}
    assert _star_row(restorer, "vc-2")["starred"] is True
    first = restorer.stars.get_all()["vc-2"]
    # A second press is not a new star: its time stands.
    assert _star(restorer, "vc-2").status == 200
    assert restorer.stars.get_all() == {"vc-2": first}
    assert _star(restorer, "vc-2", False).status == 200
    assert _star_row(restorer, "vc-2")["starred"] is False


def test_a_star_needs_a_session_a_boolean_and_a_known_origin(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    assert _star(restorer, "vc-9").status == 404
    assert _star(restorer, "vc-2", "yes").status == 400
    assert _star(restorer, "bad name").status == 400
    restorer.headers = {"Origin": "https://evil.example"}
    assert _star(restorer, "vc-2").status == 403
    assert restorer.stars.get_all() == {}


def test_a_stale_star_can_always_be_cleared(restorer, monkeypatch):
    _record_tmux(monkeypatch)
    restorer.stars.adopt("vc-9", 5.0)
    assert _star(restorer, "vc-9", False).status == 200
    assert restorer.stars.get_all() == {}


def test_a_bridge_without_stars_answers_503_and_its_rows_carry_no_field(spawner, monkeypatch):
    monkeypatch.setattr(
        main_module, "list_tmux_sessions", lambda: {"vc-2": {"title": None, "attached": False, "cwd": str(spawner.tmp)}}
    )
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    assert _star(spawner, "vc-2").status == 503
    assert all("starred" not in row for row in spawner.snapshot_payload()["sessions"])


def test_a_rename_moves_the_star(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    _star(restorer, "vc-2")
    _tmux_ok(monkeypatch)
    restorer.path = "/rename-session"
    _with_body(restorer, {"session": "vc-2", "name": "nav-fix"})
    restorer._handle_rename_session()
    assert restorer.recorded.status == 200, restorer.recorded.body
    assert list(restorer.stars.get_all()) == ["nav-fix"]


def test_a_close_takes_the_star_and_a_reopen_hands_it_back(restorer, monkeypatch):
    _record_live(restorer, monkeypatch, "vc-2", str(restorer.tmp))
    _star(restorer, "vc-2")
    starred_at = restorer.stars.get_all()["vc-2"]
    _killed(restorer, monkeypatch)
    # The star left with the conversation: a `+ NEW` that reuses `vc-2` opens without it.
    assert restorer.stars.get_all() == {}
    _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    assert row["starred"] is True
    assert _reopen(restorer, row["id"]).status == 200
    assert restorer.stars.get_all() == {"vc-2": starred_at}


def test_an_unstarred_close_says_nothing_about_stars(restorer, monkeypatch):
    _killed(restorer, monkeypatch)
    _record_tmux(monkeypatch)
    (row,) = _closed_rows(restorer)
    assert "starred" not in row


def test_a_spawn_under_a_reused_name_opens_without_the_old_star(restorer, monkeypatch):
    # A star nothing took (its session ended while the bridge was down) must not land on a new agent.
    restorer.stars.adopt("vc-2", 5.0)
    _record_tmux(monkeypatch)
    _with_body(restorer, {"name": "vc-2", "profile": "claude-a"})
    restorer._handle_spawn()
    assert restorer.recorded.status == 201, restorer.recorded.body
    assert restorer.stars.get_all() == {}


def test_a_bridge_without_the_closed_list_answers_503(spawner, monkeypatch):
    _record_tmux(monkeypatch)
    spawner.path = "/closed"
    spawner._handle_closed()
    assert spawner.recorded.status == 503
    _with_body(spawner, {"id": "0123456789ab"})
    spawner._handle_reopen()
    assert spawner.recorded.status == 503


def test_a_bridge_without_records_answers_restore_with_503(spawner, monkeypatch):
    _record_tmux(monkeypatch)
    _with_body(spawner, {})
    spawner._handle_restore()
    assert spawner.recorded.status == 503


# ── POST /usage → per-row context_left ─────────────────────────────────────


def _usage(h, payload):
    raw = json.dumps(payload).encode()
    h.path = "/usage"
    h.headers = {**h.headers, "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_usage()
    return h.recorded.status


def _row(h, monkeypatch, name="vc-2"):
    monkeypatch.setattr(
        main_module,
        "list_tmux_sessions",
        lambda: {name: {"title": None, "attached": False, "cwd": str(h.tmp), "profile": "claude-a"}},
    )
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)
    return next(row for row in h.snapshot_payload()["sessions"] if row["session"] == name)


def test_context_left_rides_the_row_and_a_compact_clears_it(restorer, monkeypatch):
    from tldrack import transcript

    path = restorer.tmp / ".claude-a" / "projects" / "p" / "ctx.jsonl"
    path.write_text("")
    # start_status_server allowlists each profile's root at startup; the fixture
    # builds the handler directly, so do it here (and undo it — module state).
    transcript.configure_transcript_roots([str(restorer.tmp / ".claude-a")])
    try:
        _context_left_round_trip(restorer, monkeypatch, path)
    finally:
        transcript.configure_transcript_roots(())


def _context_left_round_trip(restorer, monkeypatch, path):
    _hook(restorer, "SessionStart", {"transcript_path": str(path)})  # binds vc-2 ↔ transcript
    assert _usage(restorer, {"transcript_path": str(path), "context_window": {"remaining_percentage": 37.4}}) == 200
    assert _row(restorer, monkeypatch)["context_left"] == 37
    # Right after /compact Claude Code sends nulls: a stale "37%" must not survive it.
    _usage(restorer, {"transcript_path": str(path), "context_window": {"remaining_percentage": None}})
    assert "context_left" not in _row(restorer, monkeypatch)


# ── POST /usage → GET /costs (session_costs.py) ────────────────────────────


def test_usage_books_cost_against_the_bound_session(tmp_path):
    from tldrack import transcript
    from tldrack.session_costs import SessionCostStore

    root = tmp_path / ".claude"
    (root / "projects" / "p").mkdir(parents=True)
    path = root / "projects" / "p" / "cost.jsonl"
    path.write_text("")
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        costs=SessionCostStore(tmp_path / "costs.json"),
    )
    h = object.__new__(cls)
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    transcript.configure_transcript_roots([str(root)])
    try:
        _hook(h, "SessionStart", {"transcript_path": str(path)}, session="deploy-fix")
        sid = "0123abcd-4567-89ef-0123-456789abcdef"
        assert _usage(h, {"transcript_path": str(path), "session_id": sid, "cost": {"total_cost_usd": 0.75}}) == 200
        assert _usage(h, {"transcript_path": str(path), "session_id": sid, "cost": {"total_cost_usd": 1.25}}) == 200
        # A malformed cost never fails the post.
        assert _usage(h, {"transcript_path": str(path), "session_id": sid, "cost": {"total_cost_usd": "x"}}) == 200
    finally:
        transcript.configure_transcript_roots(())
    h.path = "/costs"
    h._handle_get_costs()
    assert rec.status == 200
    assert rec.body["today_usd"] == 1.25
    assert rec.body["sessions"][0]["session"] == "deploy-fix"
    assert rec.body["fixers"]["today_usd"] == 1.25


def test_costs_without_a_store_is_503(handler):
    handler.path = "/costs"
    handler._handle_get_costs()
    assert handler.recorded.status == 503


# ── a turn the session limit cut off → per-row stop_failure ───────────────

LIMIT_ENTRY = {
    "type": "assistant",
    "isSidechain": False,
    "isApiErrorMessage": True,
    "error": "rate_limit",
    "apiErrorParams": {"rate_limit_info": {"resetsAt": 1700003600, "rateLimitType": "five_hour"}},
    "message": {
        "id": "e1",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": "You've hit your session limit · resets 3:00pm"}],
    },
}


@pytest.fixture
def limited(restorer):
    """`restorer` with vc-2 bound to a transcript under its profile's allowlisted root."""
    from tldrack import transcript

    path = restorer.tmp / ".claude-a" / "projects" / "p" / "limit.jsonl"
    path.write_text("")
    transcript.configure_transcript_roots([str(restorer.tmp / ".claude-a")])
    restorer.transcript_file = path
    _hook(restorer, "SessionStart", {"transcript_path": str(path)})
    _hook(restorer, "UserPromptSubmit", {"transcript_path": str(path)})
    yield restorer
    transcript.configure_transcript_roots(())


def _append(path, entry):
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def test_stop_failure_ends_the_turn_and_the_row_says_why(limited, monkeypatch):
    path = limited.transcript_file
    assert "stop_failure" not in _row(limited, monkeypatch)  # busy: nothing to say
    _append(path, LIMIT_ENTRY)
    assert _hook(limited, "StopFailure", {"transcript_path": str(path), "error": "rate_limit"}) == 200
    row = _row(limited, monkeypatch)
    assert (row["state"], row["activity"]) == ("free", None)
    assert row["stop_failure"] == {
        "error": "rate_limit",
        "limit": "five_hour",
        "resets_at": 1700003600,
        "message": "You've hit your session limit · resets 3:00pm",
    }


def test_the_arbiter_tick_finds_a_cut_off_turn_with_no_hook(limited, monkeypatch):
    # An account whose relay predates StopFailure: the turn ends silently, the
    # arbiter corrects busy → free, and the error entry lands after any hook.
    path = limited.transcript_file
    _hook(limited, "Stop", {"transcript_path": str(path)})
    _append(path, LIMIT_ENTRY)
    assert "stop_failure" not in _row(limited, monkeypatch)
    limited.arbiter.tick()
    assert _row(limited, monkeypatch)["stop_failure"]["error"] == "rate_limit"


def test_a_new_conversation_in_the_pane_drops_it(limited, monkeypatch):
    path = limited.transcript_file
    _append(path, LIMIT_ENTRY)
    _hook(limited, "StopFailure", {"transcript_path": str(path)})
    _hook(limited, "SessionStart", {"transcript_path": str(path)})  # e.g. `claude --resume`
    assert "stop_failure" not in _row(limited, monkeypatch)


@pytest.fixture
def no_ntfy_topic(tmp_path, monkeypatch):
    """No topic anywhere, so the handler's notifier is unconfigured whatever this Mac has."""
    monkeypatch.setenv("TLDRACK_NOTIFY_PATH", str(tmp_path / "notify.json"))
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)


def test_notify_send_refuses_a_malformed_body(no_ntfy_topic, handler):
    for payload in (
        {"body": "b"},
        {"title": " ", "body": "b"},
        {"title": "t", "body": 3},
        {"title": "t", "body": "b", "tags": 1},
    ):
        _with_body(handler, payload)
        handler._handle_notify_send()
        assert handler.recorded.status == 400, payload


def test_notify_send_reports_an_unsent_event_as_an_outcome_not_an_error(no_ntfy_topic, handler):
    _with_body(handler, {"title": "Batch: step 1/2 done", "body": "run A finished before run B"})
    handler._handle_notify_send()
    assert handler.recorded.status == 200
    assert handler.recorded.body["sent"] is False and handler.recorded.body["reason"] == "unconfigured"
    assert "topic" not in handler.recorded.body, "the topic is a secret and never answered"


def test_first_prompt_label_survives_followups_and_manual_rename_wins(handler, monkeypatch, tmp_path):
    handler.tmp = tmp_path
    _hook(handler, "UserPromptSubmit", {"prompt": "Please fix rack resizing"})
    assert _row(handler, monkeypatch)["auto_name"] == "fix rack resizing"
    _hook(handler, "UserPromptSubmit", {"prompt": "Now change the colors"})
    assert _row(handler, monkeypatch)["auto_name"] == "fix rack resizing"
    _tmux_ok(monkeypatch)
    _with_body(handler, {"session": "vc-2", "name": "rack-fix"})
    handler._handle_rename_session()
    assert handler.recorded.status == 200
    _hook(handler, "UserPromptSubmit", {"prompt": "Do more"}, session="rack-fix")
    assert "auto_name" not in _row(handler, monkeypatch, "rack-fix")


def test_a_first_prompt_gets_a_topic_label_unique_against_the_rack(tmp_path, monkeypatch):
    """The hook writes the placeholder, the topic job replaces it, and `taken`
    carries a custom tmux name so the label never collides with it."""
    from tldrack.session_topics import TopicResult, pick_unique

    asked = []

    def topic_ask(prompt, taken):
        asked.append((prompt, list(taken)))
        return TopicResult(label=pick_unique(["docs-voice", "docs-voice-lines"], taken))

    names = SessionNameStore(tmp_path / "names.json")
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "started_at": 0},
        control=ShutdownControl("status-only"),
        auto_names=names,
        topic_ask=topic_ask,
    )
    monkeypatch.setattr(main_module, "list_tmux_sessions", lambda: {"docs-voice": {}, "vc-2": {}, "vc-3": {}})
    h = object.__new__(cls)
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h.recorded = Recorder()
    h._send_json = lambda status, body: setattr(h.recorded, "status", status)
    names.adopt("vc-9", "old-replays")  # a session that ended by itself: its label is not in the rack

    assert _hook(h, "UserPromptSubmit", {"prompt": '<rack-wake run="R1" n="1"/> 1 event'}) == 200
    assert cls.topics.drain() == 0, "a line the rack typed asks for nothing"
    assert _hook(h, "UserPromptSubmit", {"prompt": "Fix the voice lines in the docs"}) == 200
    assert names.get_all() == {"vc-2": "Fix the voice lines in the docs", "vc-9": "old-replays"}
    assert cls.topics.drain() == 1
    assert asked == [("Fix the voice lines in the docs", ["docs-voice"])]
    assert names.get_all()["vc-2"] == "docs-voice-lines"
