"""Automatic review evidence must belong to this conversation and current turn."""

import json

import pytest

from tldrack import codex_rollout

SESSION = "dddddddd-0000-4000-8000-000000000004"


def context(turn="current", reviewer="auto_review"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "approvals_reviewer": reviewer}}


@pytest.fixture
def rollout(tmp_path):
    root = tmp_path / "codex"
    path = root / "sessions" / f"rollout-date-{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    payload = {"transcript_path": str(path), "session_id": SESSION, "turn_id": "current", "tool_name": "Bash"}

    def read(*entries, suffix="", **overrides):
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries) + suffix)
        return codex_rollout.codex_uses_auto_review({**payload, **overrides}, str(root))

    return path, read


@pytest.mark.parametrize(
    "latest", [context("other"), context(reviewer="user"), context(reviewer=None), {"type": "turn_context", "payload": None}]
)
def test_never_reuses_review_settings_from_an_older_context(rollout, latest):
    _, read = rollout
    assert not read(context(), latest)


def test_requires_known_local_tool_and_matching_session_and_turn(rollout):
    _, read = rollout
    for overrides in (
        {"turn_id": None},
        {"turn_id": ""},
        {"session_id": "invalid"},
        {"tool_name": "unknown"},
        {"tool_name": "mcp__app__write"},
    ):
        assert not read(context(), **overrides)
    assert read(context(), tool_name="apply_patch")


def test_missing_truncated_or_out_of_window_context_stays_attention(rollout, monkeypatch):
    _, read = rollout
    assert not read()
    assert not read(suffix=json.dumps(context()))  # uncommitted record
    assert read(context(), {"type": "event_msg"}, suffix="{bad}\n{partial")
    monkeypatch.setattr(codex_rollout, "APPROVAL_TAIL_BYTES", 256)
    assert not read(context(), {"type": "event_msg", "payload": "x" * 512})
    assert read({"type": "event_msg", "payload": "x" * 512}, context())


def test_unconfigured_or_symlinked_transcript_cannot_hide_a_prompt(rollout, tmp_path):
    path, read = rollout
    outside = tmp_path / path.name
    outside.write_text(json.dumps(context()) + "\n")
    assert not read(context(), transcript_path=str(outside))
    path.unlink()
    path.symlink_to(outside)
    assert not codex_rollout.codex_uses_auto_review(
        {
            "transcript_path": str(path),
            "session_id": SESSION,
            "turn_id": "current",
            "tool_name": "Bash",
        },
        str(path.parent.parent),
    )
