"""The whole path, `PreToolUse` hook → `SessionWorkTreeStore` → `/snapshot` row `work_tree`, through a real
handler and a real git repository: an agent that ran `git worktree add` from the main checkout is drawn
on that tree's island (work_trees.py owns why)."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

import tldrack.main as main_module
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.work_trees import SessionWorkTreeStore


def git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def rack(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    main.mkdir()
    git("init", "-q", "-b", "main", cwd=main)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one", cwd=main)
    tree = tmp_path / "trees" / "x"
    store_path = tmp_path / "work-trees.json"
    # vc-1 is live in tmux, its pane on the main checkout; nothing here reads this Mac's real tmux.
    monkeypatch.setattr(
        main_module,
        "list_tmux_sessions",
        lambda: {"vc-1": {"title": None, "attached": False, "cwd": str(main), "profile": None}},
    )
    monkeypatch.setattr(main_module, "list_tmux_clients", dict)

    def build():
        return build_status_handler(
            store=StatusStore(),
            icon_store=SessionIconStore(tmp_path / "icons.json"),
            model_store=SessionModelStore(tmp_path / "models.json"),
            default_session="tldrack",
            bridge_info={"pid": 1234, "started_at": 0.0},
            control=ShutdownControl("status-only"),
            work_trees=SessionWorkTreeStore(store_path),
        )

    holder = {"cls": build()}

    def hook(event, **payload):
        h = object.__new__(holder["cls"])
        raw = json.dumps({"cwd": str(main), **payload}).encode()
        h.headers = {"Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        h.path = f"/hook?event={event}&session=vc-1"
        h._send_json = lambda status, body: None
        h._handle_hook()

    def row():
        snapshot = object.__new__(holder["cls"]).snapshot_payload()
        return next(item for item in snapshot["sessions"] if item["session"] == "vc-1")

    def restart():
        holder["cls"] = build()

    def bash(command):
        hook("PreToolUse", tool_name="Bash", tool_input={"command": command})

    return main, tree, hook, bash, row, restart


def test_git_worktree_add_from_main_puts_the_agent_on_the_new_tree(rack):
    main, tree, _hook, bash, row, _restart = rack
    assert "work_tree" not in row(), "on main: no island"
    bash(f"git worktree add {tree} -b feat/x")  # seen before git runs it: no tree yet
    assert "work_tree" not in row()
    git("worktree", "add", "-q", "-b", "feat/x", str(tree), cwd=main)  # the command runs
    assert row()["work_tree"] == {"path": str(tree), "branch": "feat/x", "main": str(main), "by": "added"}
    assert row()["cwd"] == str(main), "the agent still stands on main"


def test_an_edit_back_on_main_lets_the_tree_go_and_a_subagent_moves_nothing(rack):
    main, tree, hook, _bash, row, _restart = rack
    git("worktree", "add", "-q", "-b", "feat/x", str(tree), cwd=main)
    hook("PreToolUse", tool_name="Edit", tool_input={"file_path": str(tree / "a.py")}, agent_id="sub-1")
    assert "work_tree" not in row(), "a subagent's edit is its own checkout's"
    hook("PreToolUse", tool_name="Edit", tool_input={"file_path": str(tree / "a.py")})
    assert row()["work_tree"]["by"] == "edited"
    hook("PreToolUse", tool_name="Write", tool_input={"file_path": str(main / "README.md")})
    assert "work_tree" not in row()


def test_the_tree_survives_a_restart_and_a_new_conversation_forgets_it(rack):
    main, tree, hook, bash, row, restart = rack
    git("worktree", "add", "-q", "-b", "feat/x", str(tree), cwd=main)
    bash(f"cd {tree} && npm test")
    assert row()["work_tree"]["by"] == "cd"
    restart()
    assert row()["work_tree"]["path"] == str(tree), "an idle session sends no hook after a restart"
    hook("SessionStart", source="resume")
    assert "work_tree" in row(), "a resume is the same conversation"
    hook("SessionStart", source="clear")
    assert "work_tree" not in row()


def test_the_cwd_in_a_tree_wins_and_a_removed_tree_draws_nothing(rack):
    main, tree, hook, bash, row, _restart = rack
    git("worktree", "add", "-q", "-b", "feat/x", str(tree), cwd=main)
    hook("PreToolUse", tool_name="Read", tool_input={"file_path": "x"}, cwd=str(tree))
    assert row()["work_tree"]["by"] == "cwd"
    hook("PreToolUse", tool_name="Read", tool_input={"file_path": "x"})
    bash(f"git worktree remove {tree}")
    git("worktree", "remove", str(tree), cwd=main)
    assert "work_tree" not in row()
