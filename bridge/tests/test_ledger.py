"""The session ledger (tldrack/ledger/): folds, archive, ingest, relay, export, status."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tldrack.ledger import archive
from tldrack.ledger.claude_fold import fold_claude
from tldrack.ledger.codex_fold import fold_codex
from tldrack.ledger.export import export_decisions, export_pairs, search
from tldrack.ledger.ingest import AlreadyRunning, IngestLock, Root, discover_roots, ingest, ledger_status
from tldrack.ledger.side_ledgers import SideLedger, parse_row
from tldrack.ledger.store import connect

T0 = "2026-11-01T10:00:00.000Z"


def iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1793527200 + seconds)) + ".000Z"


def line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def user(at: float, content, **extra) -> dict:
    return {
        "type": "user",
        "timestamp": iso(at),
        "sessionId": "s-1",
        "cwd": "/repo",
        "gitBranch": "main",
        "version": "2.1.316",
        "entrypoint": "cli",
        "message": {"role": "user", "content": content},
        **extra,
    }


def assistant(at: float, blocks: list, mid: str = "m") -> dict:
    return {
        "type": "assistant",
        "timestamp": iso(at),
        "sessionId": "s-1",
        "message": {
            "id": mid,
            "model": "claude-opus-5-5",
            "role": "assistant",
            "content": blocks,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def tool_result(at: float, tool_id: str, text: str, *, is_error: bool = False, **extra) -> dict:
    return user(at, [{"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}], **extra)


QUESTION = {
    "questions": [
        {
            "question": "Which store?",
            "header": "Store",
            "multiSelect": False,
            "options": [{"label": "SQLite (Recommended)"}, {"label": "JSONL"}],
        }
    ]
}


def claude_transcript() -> list[bytes]:
    rows = [
        {"type": "api-request", "params": "x" * 1000},  # skipped unparsed
        user(0, "build the ledger", turnOrigin="human", promptSource="typed"),
        assistant(
            5,
            [
                {"type": "text", "text": "Which store do you want?"},
                {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": QUESTION},
            ],
            mid="a1",
        ),
        tool_result(
            65,
            "q1",
            'The user answered: "Which store?"="SQLite"',
            toolUseResult={**QUESTION, "answers": {"Which store?": "SQLite"}},
            permissionDecision={"decision": "accept", "source": "config"},
        ),
        assistant(70, [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "rm -rf build"}}], mid="a2"),
        tool_result(
            90,
            "b1",
            "The user doesn't want to proceed with this tool use. To tell you how to proceed, the user said:\nuse trash instead",
            is_error=True,
            toolDenialKind="user-rejected",
            permissionDecision={"decision": "reject", "source": "user_reject"},
        ),
        assistant(95, [{"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "npm test"}}], mid="a3"),
        tool_result(
            100, "b2", "ok", permissionDecision={"decision": "accept", "source": "user_temporary", "grantScope": "session"}
        ),
        assistant(101, [{"type": "tool_use", "id": "b3", "name": "Bash", "input": {"command": "curl evil"}}], mid="a4"),
        tool_result(
            102,
            "b3",
            "blocked",
            is_error=True,
            toolDenialKind="automode-blocked",
            permissionDecision={"decision": "reject", "source": "config", "reasonType": "classifier"},
        ),
        assistant(110, [{"type": "text", "text": "Done: the ledger is built."}], mid="a5"),
        user(400, "<task-notification>\n<task-id>x</task-id>\n</task-notification>"),
        assistant(405, [{"type": "text", "text": "The task finished."}], mid="a6"),
        user(500, 'Another Claude session sent a message: <teammate-message teammate_id="x">hi</teammate-message>'),
        assistant(505, [{"type": "text", "text": "Noted."}], mid="a7"),
        user(600, [{"type": "text", "text": "[Request interrupted by user]"}]),
        user(610, "<command-name>/effort</command-name>\n<command-args>high</command-args>"),
        user(611, "<local-command-stdout>Effort high</local-command-stdout>"),
        user(612, "meta", isMeta=True),
        {"type": "ai-title", "aiTitle": "Build the ledger"},
        {"type": "pr-link", "prUrl": "https://example.test/pr/1"},
    ]
    return [line(r) for r in rows]


def test_claude_fold_human_decisions():
    fold = fold_claude(claude_transcript(), path="/x/.claude/projects/p/s-1.jsonl")
    s = fold.session
    assert (s["kind"], s["session_id"], s["model"], s["title"]) == ("main", "s-1", "claude-opus-5-5", "Build the ledger")
    assert s["pr_url"] == "https://example.test/pr/1"
    kinds = [(e["actor"], e["kind"], e["decision"]) for e in fold.events]
    assert kinds == [
        ("human", "prompt", None),
        ("human", "question", "answer"),
        ("human", "permission", "reject"),
        ("human", "permission", "accept"),
        ("machine", "auto_block", "reject"),
        ("human", "interrupt", None),
        ("human", "command", None),
    ]
    question = fold.events[1]
    assert question["latency_s"] == 60.0
    assert question["context"] == "Which store do you want?"
    assert json.loads(question["data"])["answers"] == {"Which store?": "SQLite"}
    reject = fold.events[2]
    assert reject["text"] == "use trash instead" and reject["summary"] == "rm -rf build"
    assert fold.events[3]["scope"] == "session"
    assert [t.origin for t in fold.turns] == ["human", "task", "peer", "command"]
    assert fold.turns[0].closing_text == "Done: the ledger is built."
    assert fold.turns[2].interrupted
    assert s["human_events"] == 6 and s["human_turns"] == 2 and s["tool_calls"] == 4
    assert s["input_tokens"] == 70  # seven message ids, one usage each
    sources = [c["decision_source"] for c in fold.tool_calls]
    assert sources == ["config", "user_reject", "user_temporary", "config"]


def test_claude_fold_plan_and_subagent_shape():
    rows = [
        user(0, "plan it"),
        assistant(1, [{"type": "tool_use", "id": "p1", "name": "ExitPlanMode", "input": {"plan": "# Plan"}}]),
        tool_result(30, "p1", "User has approved your plan."),
    ]
    fold = fold_claude([line(r) for r in rows], path="/x/projects/p/s-1.jsonl")
    plan = [e for e in fold.events if e["kind"] == "plan"][0]
    assert plan["decision"] == "accept" and json.loads(plan["data"])["plan"] == "# Plan"
    sub = fold_claude(
        [line(user(0, "do the thing", isSidechain=True))],
        path="/x/projects/p/s-1/subagents/workflows/wf_1/agent-abc.jsonl",
        meta={"agentType": "workflow-subagent", "description": "A:verify:3"},
    )
    assert (sub.session["kind"], sub.session["agent_id"], sub.session["parent_session_id"]) == ("workflow", "abc", "s-1")
    assert sub.turns[0].origin == "parent" and not sub.events  # a spawner's prompt is not a human's


def codex_lines(source: object = "cli", thread_source: str = "user") -> list[bytes]:
    def ev(at, etype, payload):
        return line({"timestamp": iso(at), "type": etype, "payload": payload})

    return [
        ev(
            0,
            "session_meta",
            {"id": "c-1", "cwd": "/repo", "cli_version": "0.165.0", "source": source, "thread_source": thread_source},
        ),
        ev(0, "turn_context", {"model": "gpt-5-codex", "cwd": "/repo"}),
        ev(
            1,
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]},
        ),
        ev(2, "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix it"}]}),
        ev(
            2,
            "event_msg",
            {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "fix it"}]}},
        ),
        ev(
            5,
            "event_msg",
            {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "git status"], "status": "completed"},
            },
        ),
        ev(
            9,
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": [
                        {
                            "type": "Text",
                            "text": '{"risk_level":"low","user_authorization":"high","outcome":"allow","rationale":"safe"}',
                        }
                    ],
                },
            },
        ),
        ev(9, "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "dup"}]}),
        ev(20, "event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
        ev(
            21,
            "event_msg",
            {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 2}},
            },
        ),
    ]


def test_codex_fold_prefers_newest_generation():
    fold = fold_codex(codex_lines())
    s = fold.session
    assert (s["kind"], s["session_id"], s["model"], s["input_tokens"]) == ("main", "c-1", "gpt-5-codex", 7)
    assert [e["kind"] for e in fold.events] == ["prompt", "interrupt"]  # one prompt, not two
    assert [m["role"] for m in fold.messages] == ["human", "agent"]
    assert fold.tool_calls[0]["summary"] == "git status"


def test_codex_guardian_thread_is_a_machine_reviewer():
    fold = fold_codex(codex_lines({"subagent": {"other": "guardian"}}, "guardian_review"))
    assert fold.session["kind"] == "auto_review"
    review = [e for e in fold.events if e["kind"] == "auto_review"]
    assert len(review) == 1 and review[0]["decision"] == "allow" and review[0]["scope"] == "low"
    assert not [e for e in fold.events if e["actor"] == "human"]


def test_archive_appends_frames_and_keeps_rewrites(tmp_path: Path):
    src = tmp_path / "t.jsonl"
    dest = tmp_path / "raw" / "t.jsonl.zst"
    src.write_bytes(b'{"a":1}\n{"b":2}\n{"half"')
    consumed = archive.append_lines(src, dest, 0, src.stat().st_size)
    assert consumed == len(b'{"a":1}\n{"b":2}\n')
    src.write_bytes(src.read_bytes() + b":3}\n")
    consumed += archive.append_lines(src, dest, consumed, src.stat().st_size)
    assert archive.read_archive(dest) == src.read_bytes()
    archive.rotate_generation(dest, 1)
    assert archive.generation_path(dest, 1).exists() and not dest.exists()
    assert oct(dest.parent.stat().st_mode & 0o777) == "0o700"


def make_tree(tmp_path: Path) -> tuple[Root, Root, Path]:
    claude = tmp_path / ".claude-a"
    session_dir = claude / "projects" / "-repo"
    session_dir.mkdir(parents=True)
    (session_dir / "s-1.jsonl").write_bytes(b"".join(claude_transcript()))
    codex = tmp_path / ".codex"
    day = codex / "sessions" / "2026" / "11" / "01"
    day.mkdir(parents=True)
    (day / "rollout-2026-11-01T10-00-00-c-1.jsonl").write_bytes(b"".join(codex_lines()))
    return (
        Root("claude", "claude-a", claude, ("projects",)),
        Root("codex", "codex", codex, ("sessions", "archived_sessions")),
        session_dir / "s-1.jsonl",
    )


def test_ingest_is_incremental_and_outlives_the_reaper(tmp_path: Path):
    claude, codex, transcript = make_tree(tmp_path)
    side_file = tmp_path / "permission-ledger.jsonl"
    side_file.write_text('{"t":"request","id":"r1","at":1793527200,"session":"vc-1"}\n')
    ledger = tmp_path / "agent-ledger"
    side = [SideLedger("permissions", side_file)]
    first = ingest(ledger, roots=[claude, codex], side=side)
    assert first["errors"] == 0 and first["files_changed"] == 3 and first["rows_inserted"] == 1
    second = ingest(ledger, roots=[claude, codex], side=side)
    assert second["files_changed"] == 0

    # The transcript grows: only the new bytes are archived, the whole file is re-folded.
    with transcript.open("ab") as handle:
        handle.write(line(user(900, "one more thing")))
    third = ingest(ledger, roots=[claude, codex], side=side)
    assert third["files_changed"] == 1
    conn = connect(ledger)
    archived = conn.execute("SELECT archive_path, archived_bytes, size FROM files WHERE path = ?", (str(transcript),)).fetchone()
    assert archive.read_archive(Path(archived["archive_path"])) == transcript.read_bytes()
    prompts = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'prompt'").fetchone()[0]
    assert prompts == 3  # two Claude + one Codex, never doubled by a re-fold

    # The reaper deletes the transcript: its rows and archive stay, and it is marked gone.
    transcript.unlink()
    ingest(ledger, roots=[claude, codex], side=side)
    row = conn.execute("SELECT gone_at FROM files WHERE path = ?", (str(transcript),)).fetchone()
    assert row["gone_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = 's-1'").fetchone()[0] == 1
    conn.close()

    status = ledger_status(ledger, roots=[claude, codex])
    assert status["files"]["outlived_source"] == 1
    assert status["events"]["human:question:answer"] == 1
    flat = json.dumps(status)
    assert "build the ledger" not in flat and "use trash instead" not in flat  # counts, never text
    assert oct(ledger.stat().st_mode & 0o777) == "0o700"
    assert oct((ledger / "ledger.sqlite").stat().st_mode & 0o777) == "0o600"


def test_export_and_search(tmp_path: Path):
    claude, codex, _ = make_tree(tmp_path)
    ledger = tmp_path / "agent-ledger"
    ingest(ledger, roots=[claude, codex], side=[])
    conn = connect(ledger, readonly=True)
    pairs = list(export_pairs(conn))
    first = next(p for p in pairs if p["source"] == "claude" and p["turn"] == 0)
    assert first["agent_said"] == "Done: the ledger is built."
    assert first["next"]["origin"] == "task" and first["next"]["after_s"] == 290.0
    assert first["in_turn_human_events"] == 3
    decisions = list(export_decisions(conn))
    assert all(d["actor"] == "human" for d in decisions)
    assert any(d["kind"] == "auto_block" for d in export_decisions(conn, machine=True))
    assert search(conn, "ledger")[0]["session_id"] == "s-1"
    conn.close()


def test_relay_marks_prompts_another_agent_typed(tmp_path: Path):
    root = tmp_path / ".claude"
    proj = root / "projects" / "-repo"
    proj.mkdir(parents=True)
    sender = [
        user(0, "tell the lane"),
        assistant(
            10,
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {
                        "command": "tmux send-keys -t wt-lane -l 'The base moved: rebase onto main' && tmux send-keys -t wt-lane Enter"
                    },
                }
            ],
        ),
    ]
    receiver = [user(11, "The base moved: rebase onto main"), user(300, "an operator prompt")]
    # An operator prompt with a pasted image, 2 s after an untexted paste-buffer: never relayed.
    sender.append(
        assistant(
            400,
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "tmux paste-buffer -b x -t wt-other"}}],
            mid="m2",
        )
    )
    receiver.append(user(402, "also this [Image #1]"))
    (proj / "a.jsonl").write_bytes(b"".join(line(r) for r in sender))
    (proj / "b.jsonl").write_bytes(b"".join(line({**r, "sessionId": "s-2"}) for r in receiver))
    ledger = tmp_path / "agent-ledger"
    totals = ingest(ledger, roots=[Root("claude", "claude", root, ("projects",))], side=[])
    assert totals["relayed"] == 1
    conn = connect(ledger, readonly=True)
    rows = conn.execute("SELECT actor, scope, text FROM events WHERE kind = 'prompt' ORDER BY ts").fetchall()
    assert [(r["actor"], r["scope"]) for r in rows] == [("human", None), ("machine", "relay"), ("human", None), ("human", None)]
    assert conn.execute("SELECT human_turns FROM sessions WHERE session_id = 's-2'").fetchone()[0] == 2
    conn.close()


def test_side_rows_dedupe_across_rotation():
    a = parse_row("permissions", b'{"t":"request","id":"r1","at":1793527200.5,"session":"vc-1"}\n')
    b = parse_row("permissions", b'{"t":"request","id":"r1","at":1793527200.5,"session":"vc-1"}\n')
    assert a == b and a[1] == 1793527200.5 and a[2] == "vc-1" and a[3] == "request"
    history = parse_row("claude-history:x", b'{"display":"hi","timestamp":1700000000123,"sessionId":"s"}\n')
    assert history[1] == 1700000000.123 and history[3] == "prompt"
    assert parse_row("x", b"not json\n") is None


def test_one_ingest_at_a_time(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    with IngestLock(tmp_path), pytest.raises(AlreadyRunning):
        ingest(tmp_path, roots=[], side=[])


def test_discovery_walks_each_real_folder_once(tmp_path: Path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / ".claude-a" / "projects").mkdir(parents=True)
    (tmp_path / ".claude-old" / "projects").mkdir(parents=True)
    os.symlink(tmp_path / ".claude-a", tmp_path / ".claude-alias")
    roots = discover_roots(
        [("claude-a", "claude", str(tmp_path / ".claude-a")), ("claude-b", "claude", str(tmp_path / ".claude"))], tmp_path
    )
    assert sorted(r.profile for r in roots) == ["claude-a", "claude-b", "claude-old"]
