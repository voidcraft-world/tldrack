"""What an agent hears at SessionStart, where the canvas directory is, and the hooks that relay it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_canvas.doc import load_doc
from agent_canvas.paths import CANVAS_DIR_ENV, DATA_DIR_ENV, canvas_dir, canvas_path, ensure_canvas_dir
from agent_canvas.session_start import REGISTER_SCRIPT, SMALLEST_DOC, session_start_text

KEY = "7-1760000000"
HOOKS = Path(__file__).resolve().parents[1] / "hooks"


def test_the_text_names_both_canvas_files_and_the_register_script_by_absolute_path(canvas_root):
    text = session_start_text(KEY)
    assert str(canvas_path(KEY, ".json")) in text and str(canvas_path(KEY, ".html")) in text
    assert f"python3 {REGISTER_SCRIPT} <path>" in text
    assert REGISTER_SCRIPT.is_absolute() and REGISTER_SCRIPT.is_file()


def test_the_smallest_doc_it_offers_is_valid_and_draws_a_diagram():
    loaded = load_doc(SMALLEST_DOC)
    assert loaded.issues == [] and loaded.doc["blocks"][0]["type"] == "mermaid"
    assert SMALLEST_DOC in session_start_text(KEY)


def test_the_directory_follows_the_bridges_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv(CANVAS_DIR_ENV, raising=False)
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert canvas_dir() == Path.home() / ".local" / "share" / "tldrack" / "canvas"
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    assert canvas_dir() == tmp_path / "data" / "canvas"
    monkeypatch.setenv(CANVAS_DIR_ENV, str(tmp_path / "mine"))
    assert canvas_dir() == tmp_path / "mine"


def test_the_directory_is_created_private(monkeypatch, tmp_path):
    monkeypatch.setenv(CANVAS_DIR_ENV, str(tmp_path / "canvas"))
    path = ensure_canvas_dir()
    assert path.is_dir() and (path.stat().st_mode & 0o777) == 0o700
    assert ensure_canvas_dir() == path  # a second call is a no-op


def run_hook(name: str, payload: str, **env) -> subprocess.CompletedProcess:
    """A hook against a port nothing listens on, so it never reaches a live bridge."""
    clean = {key: value for key, value in os.environ.items() if key != "TMUX"}
    clean.update(TLDRACK_STATUS_PORT="9", **env)
    return subprocess.run(
        ["/bin/sh", str(HOOKS / name)], input=payload, capture_output=True, text=True, env=clean, timeout=20, check=False
    )


@pytest.mark.parametrize("name", ["session-start.sh", "check.sh", "nudge.sh"])
def test_each_hook_parses_and_is_executable(name):
    assert subprocess.run(["/bin/sh", "-n", str(HOOKS / name)], capture_output=True, timeout=10).returncode == 0
    assert os.access(HOOKS / name, os.X_OK)


@pytest.mark.parametrize("name", ["session-start.sh", "nudge.sh"])
def test_outside_tmux_the_relays_say_nothing(name):
    result = run_hook(name, json.dumps({"hook_event_name": "SessionStart"}))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("path", ["/repo/src/main.py", "/repo/package.json", f"/canvas/{KEY}.html"])
def test_the_check_relay_leaves_every_other_write_alone(path):
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": path}})
    result = run_hook("check.sh", payload)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_check_relay_is_quiet_when_no_bridge_answers():
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": f"/canvas/{KEY}.json"}})
    result = run_hook("check.sh", payload)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
