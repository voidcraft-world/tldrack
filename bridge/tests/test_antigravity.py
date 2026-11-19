"""Antigravity CLI (`agy`) as a launch profile kind (antigravity_hooks.py, profiles.py, /hook).

The load-bearing guarantees: an `antigravity` profile spawns `agy` (never a bare
shell), runs one account only, its hooks land in agy's NAME-keyed hooks file
without touching the user's own and NEVER relay PreToolUse (empty stdout there
denies the tool), and its camelCase payloads reach the same fields Claude's do.
"""

import io
import json
from pathlib import Path

import pytest

from tldrack import antigravity_hooks as agy
from tldrack import main as main_module
from tldrack.activity import KEEP_ACTIVITY
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.permission_ledger import PermissionLedger
from tldrack.profiles import (
    Profile,
    ProfileRegistry,
    apply_profile_env,
    build_inner,
    detect_profiles,
    hooks_missing,
    parse_profile,
    resolve_cli,
)
from tldrack.snapshot import PaneSessionCache

HOME = Path("/Users/someone")


# ── the profile ──────────────────────────────────────────────────────────


def test_an_antigravity_entry_parses_onto_the_one_gemini_dir():
    profile = parse_profile(
        {"id": "gemini", "label": "GEMINI", "kind": "antigravity", "args": ["--model=Gemini 3.8 Flash"]}, HOME
    )
    assert profile is not None
    assert (profile.config_dir, profile.pin_env) == (str(HOME / ".gemini"), False)
    assert profile.args == ("--model=Gemini 3.8 Flash",)


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "gemini-2", "kind": "antigravity", "config_dir": "~/.gemini-second"},
        {"id": "gemini", "kind": "gemini"},  # Gemini CLI is not a kind: it no longer serves personal logins
    ],
)
def test_a_second_antigravity_account_or_a_gemini_cli_kind_is_refused(entry):
    assert parse_profile(entry, HOME) is None


def test_pin_env_cannot_be_forced_on_with_no_env_to_pin():
    profile = parse_profile({"id": "gemini", "kind": "antigravity", "pin_env": True}, HOME)
    assert profile is not None and profile.pin_env is False
    assert apply_profile_env(profile, {"PATH": "/bin"}) == {"PATH": "/bin"}


def test_detection_adds_gemini_only_when_agy_is_installed(tmp_path):
    assert [p.id for p in detect_profiles(tmp_path, False, antigravity_found=True)] == ["claude", "gemini", "shell"]
    assert [p.id for p in detect_profiles(tmp_path, False)] == ["claude", "shell"]


def test_the_cli_is_found_under_its_own_name_agy(tmp_path):
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "agy").write_text("")
    assert resolve_cli("antigravity", tmp_path, which=lambda _n: None) == str(tmp_path / ".local" / "bin" / "agy")
    assert resolve_cli("antigravity", tmp_path, which=lambda n: f"/opt/{n}") == "/opt/agy"


def test_the_spawn_runs_agy_by_path_with_quoted_args():
    profile = Profile("gemini", "GEMINI", "antigravity", "/Users/someone/.gemini", False, ("--model=Gemini 3.8 Flash",))
    assert build_inner(profile, (), "/Users/someone/.local/bin/agy") == "/Users/someone/.local/bin/agy '--model=Gemini 3.8 Flash'"
    assert build_inner(profile) == "agy '--model=Gemini 3.8 Flash'"


def test_the_registry_spawns_agy_never_a_bare_shell(tmp_path):
    registry = ProfileRegistry(
        [Profile("gemini", "GEMINI", "antigravity", str(tmp_path / ".gemini"), False)],
        "gemini",
        home=tmp_path,
        source="file",
        which=lambda n: f"/opt/{n}",
    )
    argv = registry.spawn_argv("gemini", "vc-9", tmp_path)
    inner = argv[argv.index("-i") + 2]  # past the pane's own cd hop, which is `/bin/zsh -f -c` too
    assert inner.startswith("/opt/agy;")


# ── the hooks file ───────────────────────────────────────────────────────


def _profile(tmp_path):
    return Profile("gemini", "GEMINI", "antigravity", str(tmp_path / ".gemini"), False)


def test_install_writes_agys_name_keyed_file_and_keeps_the_users_hooks(tmp_path):
    hooks_file = tmp_path / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    users = {"my-linter": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "./lint.sh"}]}]}}
    hooks_file.write_text(json.dumps(users))

    path, changed, backup, _notes = main_module.install_profile_hooks(_profile(tmp_path), 7682)
    assert (path, changed) == (hooks_file, True) and backup is not None
    written = json.loads(hooks_file.read_text())
    assert written["my-linter"] == users["my-linter"]
    ours = written[agy.HOOK_NAME]
    assert set(ours) == set(agy.HOOK_STATES) and "PreToolUse" not in ours
    assert ours["PostToolUse"][0]["matcher"] == ".*"  # a tool event: a matcher group
    stop = ours["Stop"][0]  # any other: a flat handler
    assert stop["type"] == "command" and stop["timeout"] == agy.HOOK_TIMEOUT_SECONDS
    assert "&event=Stop&agent=antigravity" in stop["command"] and '[ -n "$TMUX" ]' in stop["command"]
    assert hooks_missing("antigravity", written, 7682, agy.HOOK_STATES) == []

    _path, changed, _backup, _notes = main_module.install_profile_hooks(_profile(tmp_path), 7682)
    assert changed is False


def test_install_creates_the_config_dir_when_agy_never_wrote_one(tmp_path):
    path, changed, backup, _notes = main_module.install_profile_hooks(_profile(tmp_path), 7682)
    assert changed is True and backup is None and path.is_file()


def test_hooks_missing_reads_every_hook_name_and_ignores_a_disabled_one():
    relay = {"type": "command", "command": main_module.hook_command(7682, "Stop", "antigravity")}
    assert hooks_missing("antigravity", {"renamed": {"Stop": [relay]}}, 7682, ["Stop"]) == []
    assert hooks_missing("antigravity", {"renamed": {"enabled": False, "Stop": [relay]}}, 7682, ["Stop"]) == ["Stop"]
    assert hooks_missing("antigravity", {"renamed": {"Stop": [relay]}}, 7699, ["Stop"]) == ["Stop"]
    assert hooks_missing("antigravity", None, 7682, ["SessionStart", "Stop"]) == ["SessionStart", "Stop"]


# ── payloads ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,token",
    [
        ("Gemini 3.8 Flash (High)", "gemini-3.8-flash-high"),
        ("gemini-3.8-flash-n", "gemini-3.8-flash-n"),
        ("  ", None),
        (None, None),
    ],
)
def test_a_model_display_name_becomes_a_token(raw, token):
    assert agy.model_token(raw) == token


def test_normalize_maps_camel_case_onto_the_bridges_fields():
    payload = {
        "conversationId": "00000000-1111-4222-8333-444444444444",
        "transcriptPath": "/Users/someone/.gemini/antigravity-cli/brain/x/transcript.jsonl",
        "workspacePaths": ["/Users/someone/code/repo"],
        "modelName": "Gemini 3.8 Flash",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
    }
    out = agy.normalize_payload(payload)
    assert out["session_id"] == payload["conversationId"]
    assert out["transcript_path"] == payload["transcriptPath"]
    assert out["cwd"] == "/Users/someone/code/repo"
    assert out["model"] == "gemini-3.8-flash"
    assert (out["tool_name"], out["tool_input"]) == ("Bash", {"command": "git status"})
    assert "session_id" not in payload  # never mutates


@pytest.mark.parametrize(
    "call,expected",
    [
        ({"name": "view_file", "args": {"AbsolutePath": "/r/a.py"}}, ("Read", {"file_path": "/r/a.py"})),
        ({"name": "browser_navigate", "args": {"Url": "https://x.test"}}, ("WebFetch", {"url": "https://x.test"})),
        ({"name": "some_new_tool", "args": {}}, ("some_new_tool", {})),
        ({"args": {}}, (None, {})),
        ("not a dict", (None, {})),
    ],
)
def test_agy_tools_are_named_in_claude_terms(call, expected):
    assert agy.tool_in_claude_terms(call) == expected


def test_stop_with_background_agents_still_running_is_background():
    assert agy.hook_state("Stop", {"fullyIdle": False}) == "background"
    assert agy.hook_state("Stop", {"fullyIdle": True}) == "free"
    assert agy.hook_state("Stop", {}) == "free"
    assert agy.hook_state("PreInvocation", {}) == "busy"


def test_activity_comes_from_the_tool_that_just_ran():
    after = agy.normalize_payload({"toolCall": {"name": "grep_search", "args": {"Query": "TODO"}}})
    assert agy.activity_update("PostToolUse", after)["zone"] == "codebase"
    assert agy.activity_update("Stop", {}) is None
    assert agy.activity_update("PreInvocation", {}) is KEEP_ACTIVITY


# ── the route ────────────────────────────────────────────────────────────


class Recorder:
    status = None
    body = None


@pytest.fixture
def handler(tmp_path):
    registry = ProfileRegistry(
        [Profile("gemini", "GEMINI", "antigravity", str(tmp_path / ".gemini"), False)],
        "gemini",
        home=tmp_path,
        source="file",
        which=lambda n: f"/opt/{n}",
    )
    store = StatusStore()
    cls = build_status_handler(
        store=store,
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "status_port": 7682, "started_at": 0},
        control=ShutdownControl("status-only"),
        start_dir=str(tmp_path),
        profiles=registry,
        permissions=PermissionLedger(None),
        pane_sessions=PaneSessionCache(read=dict),
    )
    h = object.__new__(cls)
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h.store, h.tmp = store, tmp_path
    return h


def _post(h, event, payload, agent="antigravity"):
    raw = json.dumps(payload).encode()
    h.path = f"/hook?session=vc-9&event={event}" + (f"&agent={agent}" if agent else "")
    h.headers = {**h.headers, "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_hook()
    return h.recorded


def test_an_agy_turn_reads_busy_then_free_with_its_model_profile_and_cwd(handler):
    transcript = str(handler.tmp / ".gemini" / "antigravity-cli" / "brain" / "c1" / "transcript.jsonl")
    base = {
        "conversationId": "00000000-1111-4222-8333-444444444444",
        "transcriptPath": transcript,
        "workspacePaths": [str(handler.tmp)],
        "modelName": "Gemini 3.8 Flash (High)",
    }
    assert _post(handler, "SessionStart", base).status == 200
    assert handler.store.get("vc-9")["state"] == "free"

    _post(handler, "PreInvocation", {**base, "invocationNum": 0})
    row = handler.store.get("vc-9")
    assert row["state"] == "busy" and row["model"] == "gemini-3.8-flash-high"
    assert row["profile"] == "gemini" and row["cwd"] == str(handler.tmp)

    _post(handler, "PostToolUse", {**base, "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q"}}})
    assert handler.store.get("vc-9")["activity"]["zone"] == "tests"

    _post(handler, "Stop", {**base, "fullyIdle": True, "terminationReason": "model_stop"})
    assert handler.store.get("vc-9")["state"] == "free"


@pytest.mark.parametrize(
    "event,agent",
    [
        ("PreToolUse", "antigravity"),  # never relayed: see antigravity_hooks.py
        ("UserPromptSubmit", "antigravity"),
        ("Stop", "gemini-cli"),
    ],
)
def test_an_event_or_agent_outside_the_table_is_refused(handler, event, agent):
    assert _post(handler, event, {}, agent).status == 400


def test_pre_invocation_without_the_agent_param_is_still_refused(handler):
    assert _post(handler, "PreInvocation", {}, agent=None).status == 400
