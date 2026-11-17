"""The just-in-time canvas reminder: what counts as new structure, how often, and the route."""

from __future__ import annotations

import json

import pytest
from conftest import Clock, write_doc

from agent_canvas.desk import CanvasDesk
from agent_canvas.nudge import COOLDOWN_SECONDS, Nudger, created_structure
from agent_canvas.paths import canvas_path
from agent_canvas.plugin import AgentCanvasPlugin

KEY = "7-1760000000"
ROOT = "/repo"


def write_payload(path: str, *, kind: str = "create", **extra) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": ROOT,
        "tool_input": {"file_path": path, "content": "x"},
        "tool_response": {"type": kind, "filePath": path},
        **extra,
    }


def only(name: str):
    """A listdir that finds `name` alone in every directory."""
    return lambda _directory: [name]


def crowded(_directory):
    return ["a.py", "b.py"]


def test_the_first_file_in_a_directory_is_a_new_directory():
    assert created_structure(write_payload(f"{ROOT}/tools/sync/main.py"), only("main.py")) == "a new directory, tools/sync/"


def test_a_new_file_beside_others_is_not_structure():
    assert created_structure(write_payload(f"{ROOT}/tools/sync/main.py"), crowded) is None


@pytest.mark.parametrize(
    "rel,phrase",
    [
        ("ops/launchd/com.example.pull.plist", "a launchd job"),
        ("ops/systemd/notes-sync.service", "a systemd unit"),
        ("plugins/notes/bridge_plugin.py", "a rack plugin"),
        ("server/src/router/notes.ts", "a route module"),
        ("app/routes/notes.py", "a route module"),
    ],
)
def test_a_process_plugin_or_route_counts_even_in_a_crowded_directory(rel, phrase):
    assert created_structure(write_payload(f"{ROOT}/{rel}"), crowded) == f"{phrase}, {rel}"


@pytest.mark.parametrize(
    "payload",
    [
        write_payload(f"{ROOT}/tools/sync/main.py", kind="update"),
        write_payload(f"{ROOT}/tools/sync/main.py", agent_id="sub-1"),
        write_payload(f"{ROOT}/tools/sync/main.py", tool_name="Edit"),
        write_payload(f"{ROOT}/tools/sync/main.py", hook_event_name="PreToolUse"),
        write_payload("/tmp/scratch/notes.md"),
        write_payload("relative/notes.md"),
        write_payload(f"{ROOT}/docs/guide/notes.md"),
        write_payload(f"{ROOT}/server/src/routes/README.md"),
        write_payload(f"{ROOT}/web/node_modules/x/index.js"),
        write_payload(f"{ROOT}/tests/fixtures/notes/case.json"),
        write_payload(f"{ROOT}/README.md"),
        write_payload(f"{ROOT}/../elsewhere/x.py"),
        "not a dict",
    ],
)
def test_updates_subagents_other_tools_and_quiet_paths_never_count(payload):
    assert created_structure(payload, only("main.py")) is None


def test_once_per_cooldown_per_canvas():
    clock = Clock(1_000_000.0)
    nudger = Nudger(clock=clock, listdir=only("main.py"), drawn_at=lambda _key: None)
    first = nudger.consider(KEY, write_payload(f"{ROOT}/tools/sync/main.py"))
    text = first["hookSpecificOutput"]["additionalContext"]
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "tools/sync/" in text and str(canvas_path(KEY, ".json")) in text
    assert nudger.consider(KEY, write_payload(f"{ROOT}/tools/other/main.py")) is None
    assert nudger.consider("8-1760000000", write_payload(f"{ROOT}/tools/other/main.py")) is not None
    clock.now += COOLDOWN_SECONDS
    assert nudger.consider(KEY, write_payload(f"{ROOT}/tools/other/main.py")) is not None


def test_an_agent_that_drew_recently_is_not_reminded():
    clock = Clock(1_000_000.0)
    nudger = Nudger(clock=clock, listdir=only("main.py"), drawn_at=lambda _key: clock.now - 60)
    assert nudger.consider(KEY, write_payload(f"{ROOT}/tools/sync/main.py")) is None


class FakeContext:
    def __init__(self, start_dir):
        self.start_dir = str(start_dir)
        self.status_port = 7682

    def log(self, message: str) -> None:
        pass


@pytest.fixture()
def plugin(tmp_path, canvas_root, tmux, host):
    tmux.keys["vc-1"] = KEY
    return AgentCanvasPlugin(
        FakeContext(tmp_path), desk=CanvasDesk(host, tmux=tmux), nudger=Nudger(listdir=only("main.py")), start_watcher=False
    )


def post_nudge(plugin, payload, headers=None, session="vc-1"):
    return plugin.handle_request("POST", "/nudge", {"session": [session]}, payload, headers or {})


def test_the_route_prints_the_reminder_then_nothing(plugin):
    status, data, ctype = post_nudge(plugin, write_payload(f"{ROOT}/tools/sync/main.py"))
    assert status == 200 and ctype == "application/json"
    assert "tools/sync/" in json.loads(data)["hookSpecificOutput"]["additionalContext"]
    assert post_nudge(plugin, write_payload(f"{ROOT}/tools/other/main.py"))[1] == b""


def test_the_route_is_quiet_once_the_canvas_was_drawn(plugin, canvas_root):
    write_doc(canvas_root, KEY, {"type": "text", "text": "hi"})
    assert post_nudge(plugin, write_payload(f"{ROOT}/tools/sync/main.py"))[1] == b""


def test_the_route_is_quiet_for_an_unknown_session_and_refuses_bad_names(plugin):
    assert post_nudge(plugin, write_payload(f"{ROOT}/tools/sync/main.py"), session="vc-9")[1] == b""
    assert post_nudge(plugin, write_payload(f"{ROOT}/tools/sync/main.py"), session="../x")[0] == 400


@pytest.mark.parametrize("headers", [{"origin": "http://localhost:5173"}, {"sec-fetch-site": "same-site"}])
def test_the_route_refuses_browsers(plugin, headers):
    assert post_nudge(plugin, write_payload(f"{ROOT}/tools/sync/main.py"), headers)[0] == 403


def test_the_route_is_advertised_and_takes_a_whole_write(plugin):
    assert "/plugin/agent-canvas/nudge" in plugin.routes()
    assert plugin.max_body_bytes >= 512 * 1024
