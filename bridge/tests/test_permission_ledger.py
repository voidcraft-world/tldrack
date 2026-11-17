import json

from tldrack.permission_ledger import (
    OPEN_MAX_SECONDS,
    REDACTED,
    PermissionLedger,
    redact,
    summarize,
    tool_key,
    tool_pattern,
)
from tldrack.transcript import fold_tool_events

REJECT = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what you are doing"
)
REJECT_WITH_FEEDBACK = REJECT.replace("STOP what you are doing", "To tell you how to proceed, the user said:\nuse a worktree")


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _use(use_id, name, tool_input, ts="2026-10-27T20:00:00.000Z", sidechain=False):
    return {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": use_id, "name": name, "input": tool_input}]},
    }


def _result(use_id, text="ok", is_error=False, ts="2026-10-27T20:00:30.000Z"):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": is_error}]},
    }


def _request(name, tool_input, **extra):
    return {"hook_event_name": "PermissionRequest", "tool_name": name, "tool_input": tool_input, "cwd": "/repo", **extra}


# ── the pure pieces ──────────────────────────────────────────────────────────


def test_redact_strips_credentials_and_keeps_the_shape():
    assert redact("GITHUB_TOKEN=ghp_abc123 gh pr list") == f"GITHUB_TOKEN={REDACTED} gh pr list"
    assert redact("curl -H 'Authorization: Bearer abcdef123456' x") == f"curl -H 'Authorization: Bearer {REDACTED}' x"
    assert redact("git clone https://me:hunter2@github.com/a/b") == f"git clone https://{REDACTED}@github.com/a/b"
    assert REDACTED in redact("echo " + "a" * 40)
    assert redact("npm test -- tests/unit/a.test.ts") == "npm test -- tests/unit/a.test.ts"


def test_summary_never_carries_a_files_contents():
    assert summarize("Write", {"file_path": "/r/a.ts", "content": "SECRET BODY"}) == "/r/a.ts"
    assert summarize("Edit", {"file_path": "/r/a.ts", "old_string": "x", "new_string": "y"}) == "/r/a.ts"
    assert len(summarize("Bash", {"command": "echo " + "word " * 200})) <= 160


def test_patterns_bucket_what_a_rule_would_match():
    assert tool_pattern("Bash", {"command": "FOO=1 git push origin main"}) == "Bash git push"
    assert tool_pattern("Bash", {"command": "rm -rf build"}) == "Bash rm"
    assert tool_pattern("Bash", {"command": "/usr/bin/npm install left-pad"}) == "Bash npm install"
    assert tool_pattern("Edit", {"file_path": "/r/a.ts"}) == "Edit .ts"
    assert tool_pattern("WebFetch", {"url": "https://docs.github.com/x"}) == "WebFetch docs.github.com"
    assert tool_pattern("mcp__voidcraft__create", {"x": 1}) == "mcp__voidcraft__create"


def test_tool_key_is_the_same_from_the_hook_and_the_transcript():
    assert tool_key("Bash", {"command": "git  push\n origin", "description": "a"}) == tool_key(
        "Bash", {"command": "git push origin"}
    )
    assert tool_key("Edit", {"file_path": "/a", "new_string": "x"}) != tool_key("Edit", {"file_path": "/b"})


def test_fold_tool_events_reads_outcomes_and_skips_subagents():
    events = fold_tool_events(
        {},
        [
            _use("t1", "Bash", {"command": "ls"}),
            _result("t1", "Exit code 2", is_error=True),
            _use("t2", "Bash", {"command": "git push"}),
            _result("t2", REJECT, is_error=True),
            _use("t3", "Bash", {"command": "rm x"}),
            _result("t3", REJECT_WITH_FEEDBACK, is_error=True),
            _use("t4", "Read", {"file_path": "/a"}),
            _use("s1", "Bash", {"command": "ls"}, sidechain=True),
        ],
    )
    assert [(k, v["outcome"], v["feedback"]) for k, v in events.items()] == [
        ("t1", "approved", False),  # it RAN; that it failed is not a refusal
        ("t2", "denied", False),
        ("t3", "denied", True),
        ("t4", None, False),
    ]


# ── the ledger ───────────────────────────────────────────────────────────────


def _transcript(*entries):
    return fold_tool_events({}, entries)


def test_a_claude_grant_resolves_from_the_transcript():
    clock = Clock(1793131200.0)  # 2026-10-27T20:00:00Z
    ledger = PermissionLedger(None, clock=clock)
    use = _use("toolu_1", "Bash", {"command": "git push"})
    rid = ledger.request(
        "vc-1", _request("Bash", {"command": "git push"}), profile="claude-a", kind="claude", tool_events=_transcript(use)
    )
    clock.now += 40
    assert ledger.observe("vc-1", "PreToolUse", {}, _transcript(use, _result("toolu_1"))) == [(rid, "approved")]
    [row] = ledger.rows()
    assert (row["outcome"], row["via"], row["tool_use_id"], row["wait_s"]) == ("approved", "transcript", "toolu_1", 30.0)


def test_a_rejection_that_ended_the_turn_resolves_on_the_arbiter_tick():
    ledger = PermissionLedger(None, clock=Clock(1793131200.0))
    use = _use("toolu_2", "Bash", {"command": "rm -rf build"})
    ledger.request(
        "vc-1", _request("Bash", {"command": "rm -rf build"}), profile=None, kind="claude", tool_events=_transcript(use)
    )
    ledger.observe("vc-1", "tick", {}, _transcript(use, _result("toolu_2", REJECT_WITH_FEEDBACK, is_error=True)))
    [row] = ledger.rows()
    assert (row["outcome"], row["feedback"]) == ("denied", True)


def test_a_late_flushed_tool_use_still_binds():
    ledger = PermissionLedger(None, clock=Clock(1793131200.0))
    ledger.request("vc-1", _request("Edit", {"file_path": "/r/a.ts"}), profile=None, kind="claude", tool_events={})
    use = _use("toolu_3", "Edit", {"file_path": "/r/a.ts", "new_string": "x"})
    ledger.observe("vc-1", "Stop", {}, _transcript(use, _result("toolu_3")))
    assert ledger.rows()[0]["outcome"] == "approved"


def test_unresolved_at_turn_end_is_not_run_and_a_subagents_is_unknown():
    ledger = PermissionLedger(None, clock=Clock())
    ledger.request("vc-1", _request("Bash", {"command": "a"}), profile=None, kind="claude", tool_events={})
    ledger.request("vc-1", _request("Bash", {"command": "b"}, agent_id="agent-7"), profile=None, kind="claude", tool_events={})
    ledger.observe("vc-1", "UserPromptSubmit", {}, {})
    assert sorted(r["outcome"] for r in ledger.rows()) == ["not_run", "unknown"]


def test_codex_post_tool_use_approves_the_oldest_open_request_for_that_tool():
    ledger = PermissionLedger(None, clock=Clock())
    first = ledger.request("codex-1", _request("shell", {"command": "a"}), profile="codex", kind="codex", tool_events={})
    ledger.request("codex-1", _request("shell", {"command": "b"}), profile="codex", kind="codex", tool_events={})
    assert ledger.observe("codex-1", "PostToolUse", {"tool_name": "shell"}, {}) == [(first, "approved")]


def test_a_request_past_the_open_window_is_never_matched():
    clock = Clock()
    ledger = PermissionLedger(None, clock=clock)
    ledger.request("vc-3", _request("Bash", {"command": "a"}), profile=None, kind="claude", tool_events={})
    clock.now += OPEN_MAX_SECONDS + 1
    assert ledger.observe("vc-3", "Stop", {}, {}) == []
    assert ledger.stats()["pending"] == 1


def test_stats_by_pattern_and_the_human_approval_rate():
    clock = Clock(1793131200.0)
    ledger = PermissionLedger(None, clock=clock)
    for n, (cmd, text, err) in enumerate(
        [("git push", "ok", False), ("git push origin x", REJECT, True), ("npm install a", "ok", False)]
    ):
        use = _use(f"u{n}", "Bash", {"command": cmd})
        ledger.request("vc-1", _request("Bash", {"command": cmd}), profile=None, kind="claude", tool_events=_transcript(use))
        ledger.observe("vc-1", "PreToolUse", {}, _transcript(use, _result(f"u{n}", text, is_error=err)))
    stats = ledger.stats()
    assert (stats["requests"], stats["approved"], stats["denied"], stats["approve_rate"]) == (3, 2, 1, 0.667)
    assert [(p["pattern"], p["requests"], p["approve_rate"]) for p in stats["patterns"]] == [
        ("Bash git push", 2, 0.5),
        ("Bash npm install", 1, 1.0),
    ]
    assert stats["median_wait_s"] == 30.0


def test_the_ledger_survives_a_restart_and_a_broken_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    clock = Clock()
    ledger = PermissionLedger(path, clock=clock)
    rid = ledger.request(
        "vc-1", _request("Bash", {"command": "GITHUB_TOKEN=ghp_x123 gh pr merge"}), profile="p", kind="claude", tool_events={}
    )
    ledger.observe("vc-1", "Stop", {}, {})
    with path.open("a") as handle:
        handle.write("{not json\n")
    reloaded = PermissionLedger(path, clock=clock)
    [row] = reloaded.rows()
    assert (row["id"], row["outcome"]) == (rid, "not_run")
    assert "ghp_x123" not in path.read_text()  # redacted on the way IN
    assert all(json.loads(line)["t"] in ("request", "outcome") for line in path.read_text().splitlines()[:2])


def test_a_write_failure_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    messages = []
    ledger = PermissionLedger(blocker / "nested" / "ledger.jsonl", clock=Clock(), log=messages.append)
    ledger.request("vc-1", _request("Bash", {"command": "a"}), profile=None, kind="claude", tool_events={})
    assert ledger.rows() and messages
