"""Codex reply boundaries and the hook-controlled path fence."""

import json

import pytest

from tldrack.codex_closing import read_codex_closing
from tldrack.transcript import CLOSING_TEXT_MAX_CHARS

SESSION = "dddddddd-0000-4000-8000-000000000004"


def reply(text, phase="final_answer"):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


@pytest.fixture
def rollout(tmp_path):
    root = tmp_path / "codex"
    path = root / "sessions" / "2026" / "10" / "26" / f"rollout-date-{SESSION}.jsonl"
    path.parent.mkdir(parents=True)

    def read(*entries, suffix="", source=None, session_id=SESSION):
        path.write_text("".join(json.dumps(row) + "\n" for row in entries) + suffix)
        return read_codex_closing(str(source or path), str(root), session_id)

    return root, path, read


def test_research_reply_is_available_without_tool_use(rollout):
    _, _, read = rollout
    assert read(reply("Looking at the options", "commentary"), reply("Three options; pick one.")) == "Three options; pick one."
    assert read(reply("I will investigate", "analysis")) is None


@pytest.mark.parametrize(
    "new_turn",
    [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Now implement A"}},
        {"type": "event_msg", "payload": {"type": "turn_aborted"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user"}},
        {"type": "response_item", "payload": {"type": "function_call"}},
        reply("Working on your follow-up", "commentary"),
    ],
)
def test_never_reuses_an_earlier_turn_or_intermediate_answer(rollout, new_turn):
    _, _, read = rollout
    assert read(reply("Old answer"), new_turn) is None


def test_task_complete_supports_rollouts_without_message_phase(rollout):
    _, _, read = rollout
    assert (
        read(
            reply("old", None),
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Current final reply",
                },
            },
        )
        == "Current final reply"
    )


def test_bounds_tail_and_reply_and_skips_partial_or_malformed_records(rollout):
    from tldrack.codex_closing import TAIL_BYTES

    _, _, read = rollout
    final = "x" * (CLOSING_TEXT_MAX_CHARS + 50)
    assert read({"ignored": "x" * TAIL_BYTES}, reply(final), suffix='{broken}\n{"type":') == final[-CLOSING_TEXT_MAX_CHARS:]
    assert read(reply("valid"), None, [], {"payload": []}) == "valid"


def test_refuses_wrong_conversation_and_paths_outside_sessions(rollout, tmp_path):
    root, path, read = rollout
    assert read(reply("answer"), session_id="00000000-0000-0000-0000-000000000000") is None
    assert read(reply("answer"), session_id="../../secrets") is None
    for parent in (root, tmp_path / "codex-evil" / "sessions"):
        parent.mkdir(parents=True, exist_ok=True)
        outside = parent / path.name
        outside.write_text(json.dumps(reply("outside")) + "\n")
        assert read_codex_closing(str(outside), str(root), SESSION) is None
    path.unlink()
    path.symlink_to(outside)
    assert read_codex_closing(str(path), str(root), SESSION) is None


def test_missing_or_invalid_paths_are_no_verdict(rollout):
    root, path, _ = rollout
    assert read_codex_closing(str(path), str(root), SESSION) is None
    assert read_codex_closing(None, str(root), SESSION) is None


def test_sessions_directory_symlink_cannot_widen_the_profile_root(tmp_path):
    root, outside = tmp_path / "codex", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "sessions").symlink_to(outside, target_is_directory=True)
    name = f"rollout-date-{SESSION}.jsonl"
    (outside / name).write_text(json.dumps(reply("outside")) + "\n")
    assert read_codex_closing(str(root / "sessions" / name), str(root), SESSION) is None
