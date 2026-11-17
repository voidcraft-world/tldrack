"""Briefs — the cheap argv pinned flag by flag, the env, the prompt, the
parser, the staleness rule, the cap table, and the store. Nothing spawns:
`brief_one` is driven through an injected runner.
"""

import json
from pathlib import Path

import pytest

from worktree import brief
from worktree.harbor import BriefJobs, BriefStore, parse_brief_file

ROWS = [
    {"branch": "main", "sha": "a1b2c3d01", "is_base": True, "ahead": 0, "behind": 0},
    {
        "branch": "feat/x",
        "sha": "a1b2c3d03",
        "is_base": False,
        "ahead": 8,
        "behind": 2,
        "upstream": None,
        "worktree": {"path": "/Users/x/worktrees/demo-app/feat-x", "dirty": True},
        "pr": None,
        "delete_verdict": "needs_force",
        "delete_reason": None,
        "committed_at": 1_000_000,
        "author": "u",
        "subject": "cache everything",
    },
    {
        "branch": "old",
        "sha": "0123456ab",
        "is_base": False,
        "ahead": 0,
        "behind": 300,
        "upstream": {"ref": "origin/old", "ahead": 0, "behind": 0, "gone": True},
        "worktree": None,
        "pr": {"number": 7, "state": "merged", "draft": False},
        "delete_verdict": "ok",
        "delete_reason": None,
        "committed_at": 0,
        "author": "u",
        "subject": "old",
    },
]
FRESH = {"feat/x": {"branch": "feat/x", "sha": "a1b2c3d03", "base_sha": "a1b2c3d01", "what": "w", "next": []}}


# The `claude -p` recipe's flags and env are pinned where they are owned:
# bridge/tests/test_headless.py. A brief only runs what it is handed.
CALL = {"profile": "claude", "argv": ["/bin/claude", "-p"], "env": {"MAX_THINKING_TOKENS": "0"}}


class TestTargetsAndStaleness:
    def test_every_non_base_branch_without_an_entry(self):
        assert brief.select_targets(ROWS, {}) == ["feat/x", "old"]

    def test_a_fresh_entry_is_not_a_target(self):
        assert brief.select_targets(ROWS, FRESH) == ["old"]

    def test_either_tip_moving_makes_it_stale(self):
        moved_branch = {**FRESH["feat/x"], "sha": "deadbeef0"}
        moved_base = {**FRESH["feat/x"], "base_sha": "deadbeef0"}
        assert brief.is_stale(moved_branch, ROWS[1], "a1b2c3d01") is True
        assert brief.is_stale(moved_base, ROWS[1], "a1b2c3d01") is True
        assert brief.is_stale(FRESH["feat/x"], ROWS[1], "a1b2c3d01") is False
        assert brief.select_targets(ROWS, {"feat/x": moved_base}) == ["feat/x", "old"]

    def test_only_is_a_request_not_a_check(self):
        # RE-BRIEF on a fresh entry still briefs it…
        assert brief.select_targets(ROWS, FRESH, only="feat/x") == ["feat/x"]
        # …but the base is never a target, and an unknown branch is nothing.
        assert brief.select_targets(ROWS, {}, only="main") == []
        assert brief.select_targets(ROWS, {}, only="nope") == []

    def test_base_sha_is_read_off_the_rows(self):
        assert brief.base_sha_of(ROWS) == "a1b2c3d01"
        assert brief.base_sha_of([]) is None


class TestPrompt:
    def test_context_commands_follow_ahead(self):
        unlanded = brief.build_context_commands("/r", "main", ROWS[1])
        assert [c[3] for c in unlanded] == ["log", "diff"]
        assert unlanded[0][-1] == "main..feat/x" and unlanded[1][-1] == "main...feat/x"
        merged = brief.build_context_commands("/r", "main", ROWS[2])
        assert [c[3] for c in merged] == ["log"] and merged[0][-1] == "old"

    def test_facts_block_carries_what_the_model_must_not_contradict(self, monkeypatch):
        monkeypatch.setattr("worktree.names.Path.home", lambda: Path("/Users/x"))
        facts = brief.facts_block(ROWS[1], "main", now=1_000_000 + 3 * 86400)
        assert "ahead of main: 8 commits · behind main: 2 commits" in facts
        assert "merged into main: no" in facts
        assert "upstream: none (never pushed)" in facts
        assert "worktree: ~/worktrees/demo-app/feat-x · dirty (uncommitted changes)" in facts
        assert "delete verdict: needs_force" in facts
        assert "last commit: 3 days ago by u — cache everything" in facts
        gone = brief.facts_block(ROWS[2], "main", now=0)
        assert "upstream: origin/old — GONE" in gone and "pull request: #7 merged" in gone
        assert "merged into main: yes" in gone

    def test_a_squash_landed_branch_is_told_as_merged_not_as_work_to_land(self):
        # "merged: no" here had the model propose landing work that shipped.
        row = {**ROWS[1], "landed": True, "pr": {"number": 57, "state": "merged", "draft": False}}
        facts = brief.facts_block(row, "main", now=0)
        assert ("merged into main: yes — landed via PR #57; the 8 commits ahead are its pre-merge copies") in facts
        assert "merged into main: no" not in facts
        # `landed` alone, never a merged PR alone, earns the line.
        unproven = brief.facts_block({**row, "landed": False}, "main", now=0)
        assert "merged into main: no" in unproven

    def test_prompt_is_trimmed_to_the_budget(self):
        log = "x" * 10_000
        diff = "\n".join(f"file{i} | 1 +" for i in range(100))
        prompt = brief.build_prompt(ROWS[1], "main", log, diff, now=0)
        assert len(prompt) < brief.CONTEXT_MAX_CHARS + 1500  # facts + headings on top
        assert "… (60 more files)" in prompt  # the diffstat kept its head
        assert "x\n…\n" in prompt  # the log was cut, and says so


class TestParse:
    ENVELOPE = {
        "type": "result",
        "is_error": False,
        "result": '{"what":"defers GLSL validation to first render","next":["rebase onto main","push"]}',
        "structured_output": {
            "what": "defers GLSL validation to first render",
            "next": ["rebase onto main", "push", "open a PR", "a fourth step"],
        },
        "total_cost_usd": 0.0033,
        "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 1700}},
    }

    def test_structured_output_wins_and_is_capped_and_sentence_cased(self):
        parsed, why = brief.parse_brief_response(json.dumps(self.ENVELOPE))
        assert why is None
        assert parsed["what"] == "Defers GLSL validation to first render"
        assert parsed["next"] == ["rebase onto main", "push", "open a PR"]  # NEXT_MAX_STEPS
        assert parsed["model"] == "claude-haiku-4-5-20251001" and parsed["cost_usd"] == 0.0033

    def test_result_text_is_the_fallback_even_when_fenced(self):
        env = {**self.ENVELOPE, "structured_output": None, "result": '```json\n{"what": "w", "next": ["n"]}\n```'}
        parsed, _ = brief.parse_brief_response(json.dumps(env))
        assert parsed["what"] == "W" and parsed["next"] == ["n"]

    def test_the_clis_own_error_is_the_message(self):
        env = {**self.ENVELOPE, "is_error": True, "result": "Not logged in · Please run /login"}
        assert brief.parse_brief_response(json.dumps(env)) == (None, "Not logged in · Please run /login")

    def test_garbage_and_empty_answers_are_named(self):
        assert brief.parse_brief_response("nope")[1] == "the brief returned no JSON"
        assert brief.parse_brief_response(json.dumps({"result": "no json here"}))[1] == "the brief returned nothing usable"
        assert (
            brief.parse_brief_response(json.dumps({"structured_output": {"what": "", "next": []}}))[1]
            == "the brief had no description"
        )

    def test_home_paths_in_the_answer_are_scrubbed(self, monkeypatch):
        monkeypatch.setattr("worktree.names.Path.home", lambda: Path("/Users/x"))
        env = {**self.ENVELOPE, "structured_output": {"what": "touches /Users/x/code/a.ts", "next": []}}
        parsed, _ = brief.parse_brief_response(json.dumps(env))
        assert parsed["what"] == "Touches ~/code/a.ts"


class TestCaps:
    def test_the_cap_table(self):
        assert brief.decide_run(done=0, cost_usd=0.0) is None
        assert "64 briefs" in brief.decide_run(done=64, cost_usd=0.0)
        assert "$0.75" in brief.decide_run(done=3, cost_usd=0.75)
        assert brief.decide_run(done=3, cost_usd=0.10, max_calls=3) is not None

    def test_env_overrides_are_tolerant(self, monkeypatch):
        monkeypatch.setenv("WORKTREE_BRIEF_MAX_CALLS", "nope")
        monkeypatch.setenv("WORKTREE_BRIEF_MAX_COST_USD", "0.10")
        monkeypatch.setenv("WORKTREE_BRIEF_MODEL", "sonnet")
        assert brief.resolve_max_calls() == brief.DEFAULT_MAX_CALLS
        assert brief.resolve_max_cost_usd() == 0.10
        assert brief.resolve_model() == "sonnet"


class TestBriefOne:
    def test_git_then_claude_on_stdin_in_a_scratch_cwd(self):
        calls = []

        def runner(command, timeout, **kwargs):
            calls.append((command, timeout, kwargs))
            if command[0] == "git":
                return 0, "abc1234 2026-09-01 cache everything\n" if command[3] == "log" else " a.ts | 2 +-\n", ""
            return 0, json.dumps(TestParse.ENVELOPE), ""

        entry, why = brief.brief_one("/r", "main", "a1b2c3d01", ROWS[1], call=CALL, runner=runner, now=123.0)
        assert why is None
        assert entry["branch"] == "feat/x" and entry["sha"] == "a1b2c3d03" and entry["base_sha"] == "a1b2c3d01"
        assert entry["what"].startswith("Defers") and entry["at"] == 123.0 and entry["cost_usd"] == 0.0033
        git_log, git_diff, claude = calls
        assert git_log[0][3] == "log" and git_diff[0][3] == "diff"
        assert claude[0][0] == "/bin/claude" and claude[1] == brief.BRIEF_TIMEOUT_SECONDS
        assert claude[2]["input"].startswith("REPOSITORY\nr\n\nFACTS\nbranch: feat/x")
        assert "cache everything" in claude[2]["input"] and "a.ts" in claude[2]["input"]
        assert claude[2]["env"] is CALL["env"]
        assert "worktree-brief-" in str(claude[2]["cwd"])

    def test_a_git_failure_is_the_reason(self):
        def runner(command, timeout, **kwargs):  # noqa: ARG001
            return 128, "", "fatal: bad revision 'main..feat/x'"

        assert brief.brief_one("/r", "main", None, ROWS[1], call=CALL, runner=runner) == (
            None,
            "fatal: bad revision 'main..feat/x'",
        )

    def test_a_non_zero_claude_with_an_envelope_reports_the_envelope(self):
        def runner(command, timeout, **kwargs):  # noqa: ARG001
            if command[0] == "git":
                return 0, "", ""
            return 1, json.dumps({"is_error": True, "result": "budget exceeded"}), "exit 1"

        assert brief.brief_one("/r", "main", None, ROWS[2], call=CALL, runner=runner) == (None, "budget exceeded")


class TestBriefStore:
    ENTRY = {
        "branch": "feat/x",
        "sha": "a",
        "base_sha": "b",
        "what": "W",
        "next": ["n1"],
        "model": "haiku",
        "cost_usd": 0.002,
        "at": 5.0,
    }

    def test_set_persists_one_entry_at_a_time_and_survives_a_restart(self, tmp_path):
        store = BriefStore(tmp_path / "briefs.json")
        store.set(self.ENTRY)
        store.set({**self.ENTRY, "branch": "old", "what": "Old"})
        assert json.loads((tmp_path / "briefs.json").read_text())["version"] == 1
        again = BriefStore(tmp_path / "briefs.json")
        assert again.get("feat/x") == self.ENTRY and again.meta() == {"count": 2, "error": None}
        assert [p.name for p in tmp_path.iterdir()] == ["briefs.json"]  # atomic, no temp left

    def test_error_keeps_entries_and_set_clears_it(self, tmp_path):
        store = BriefStore(tmp_path / "b.json")
        store.set(self.ENTRY)
        store.set_error("Not logged in")
        assert BriefStore(tmp_path / "b.json").meta() == {"count": 1, "error": "Not logged in"}
        store.set({**self.ENTRY, "branch": "y"})
        assert store.meta()["error"] is None

    def test_drop_and_bad_rows(self, tmp_path, capsys):
        store = BriefStore(tmp_path / "b.json")
        store.set(self.ENTRY)
        store.drop("feat/x")
        assert store.get("feat/x") is None
        (tmp_path / "b.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "error": None,
                    "briefs": {"ok": {"what": "W", "next": ["a", 3], "cost_usd": "x"}, "bad": {"next": []}, "worse": 3},
                }
            )
        )
        assert BriefStore(tmp_path / "b.json").snapshot() == {
            "ok": {
                "branch": "ok",
                "sha": None,
                "base_sha": None,
                "what": "W",
                "next": ["a"],
                "model": None,
                "cost_usd": 0.0,
                "at": None,
            }
        }
        (tmp_path / "b.json").write_text("[]")
        assert BriefStore(tmp_path / "b.json").snapshot() == {}
        assert "ignoring unreadable" in capsys.readouterr().err

    def test_parse_rejects_a_wrong_envelope(self):
        with pytest.raises(ValueError):
            parse_brief_file(json.dumps({"version": 1}))


class TestBriefJobs:
    def test_claim_advance_finish_keeps_a_done_row_for_the_ttl(self):
        clock = [0.0]
        jobs = BriefJobs(clock=lambda: clock[0], wall=lambda: 1000.0)
        assert jobs.claim(3) is True and jobs.claim(3) is False
        jobs.advance("feat/x", 1, 0.0033)
        assert jobs.get() == {
            "status": "running",
            "step": "feat/x",
            "done": 1,
            "total": 3,
            "cost_usd": 0.0033,
            "note": None,
            "error": None,
            "at": None,
            "updated_at": 0.0,
        }
        jobs.finish("stopped at 3")
        row = jobs.get()
        assert row["status"] == "done" and row["note"] == "stopped at 3" and row["at"] == 1000.0
        assert jobs.claim(1) is True  # done does not block a new run
        jobs.fail("boom")
        clock[0] = 10_000.0
        assert jobs.get() is None  # expired
