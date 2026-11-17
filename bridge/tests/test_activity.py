from tldrack.activity import (
    KEEP_ACTIVITY,
    MAX_DETAIL_LEN,
    activity_update_for_event,
    classify_activity,
    classify_bash_zone,
    first_command_word,
)
from tldrack.main import StatusStore, hook_state


# ── first_command_word ──────────────────────────────────────────────────


def test_first_word_plain():
    assert first_command_word("git status") == "git"


def test_first_word_skips_env_assignments():
    assert first_command_word("FOO=1 BAR=x aws s3 ls") == "aws"


def test_first_word_skips_wrappers():
    assert first_command_word("sudo npx cdk deploy") == "cdk"


def test_first_word_strips_path_prefix():
    assert first_command_word("/usr/bin/git log") == "git"


def test_first_word_empty_segment():
    assert first_command_word("   ") is None


# ── classify_bash_zone ──────────────────────────────────────────────────


def test_bash_aws_cli():
    assert classify_bash_zone("aws s3 ls") == "aws"


def test_bash_cdk_via_npx():
    assert classify_bash_zone("npx -y aws-cdk@latest -c env=dev deploy") == "aws"
    assert classify_bash_zone("npx cdk deploy --all") == "aws"


def test_bash_git():
    assert classify_bash_zone("git diff HEAD~1") == "codebase"


def test_bash_grep_pipe_stays_codebase():
    assert classify_bash_zone("grep -rn foo src | head -20") == "codebase"


def test_bash_cd_chain_classified_by_real_command():
    assert classify_bash_zone("cd infra && aws sts get-caller-identity") == "aws"


def test_bash_aws_outranks_codebase():
    assert classify_bash_zone("git pull && cdk deploy") == "aws"


def test_bash_unknown_is_computer():
    assert classify_bash_zone("npm run build") == "computer"


def test_bash_empty_is_computer():
    assert classify_bash_zone("") == "computer"


def test_bash_test_runners():
    assert classify_bash_zone("pytest tests/ -q") == "tests"
    assert classify_bash_zone("uv run pytest") == "tests"
    assert classify_bash_zone("npm run test:e2e:headless") == "tests"
    assert classify_bash_zone("node --test tests/unit/agent-zone-logic.test.ts") == "tests"


def test_bash_tests_outrank_codebase_but_not_aws():
    assert classify_bash_zone("pytest | grep FAILED") == "tests"
    assert classify_bash_zone("pytest && cdk deploy") == "aws"


def test_bash_aws_behind_uv_run():
    assert classify_bash_zone("uv run aws s3 ls") == "aws"
    assert classify_bash_zone("uv run cdk diff") == "aws"


# ── classify_activity ───────────────────────────────────────────────────


def test_classify_file_tools_are_codebase():
    for tool in ("Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"):
        activity = classify_activity(tool, {})
        assert activity["zone"] == "codebase", tool


def test_classify_web_tools():
    assert classify_activity("WebFetch", {"url": "https://docs.aws.amazon.com/lambda"})["zone"] == "web"
    assert classify_activity("WebSearch", {"query": "three.js capsule"})["zone"] == "web"


def test_classify_unknown_tool_is_computer():
    assert classify_activity("SomeNewTool", {})["zone"] == "computer"


def test_classify_no_tool_name():
    assert classify_activity(None, {}) is None
    assert classify_activity("", {}) is None


def test_detail_bash_command_squashed_and_clipped():
    activity = classify_activity("Bash", {"command": "aws   s3\n  ls " + "x" * 200})
    assert activity["detail"].startswith("bash: aws s3 ls")
    assert len(activity["detail"]) <= MAX_DETAIL_LEN


def test_detail_edit_uses_basename():
    activity = classify_activity("Edit", {"file_path": "/Users/dev/repo/src/main.py"})
    assert activity["detail"] == "edit: main.py"


def test_detail_webfetch_uses_host():
    activity = classify_activity("WebFetch", {"url": "https://docs.aws.amazon.com/lambda/latest"})
    assert activity["detail"] == "webfetch: docs.aws.amazon.com"


# ── activity_update_for_event ───────────────────────────────────────────


def test_pretooluse_sets_activity():
    payload = {"tool_name": "Bash", "tool_input": {"command": "aws s3 ls"}}
    update = activity_update_for_event("PreToolUse", payload)
    assert update == {"zone": "aws", "tool": "Bash", "detail": "bash: aws s3 ls"}


def test_pretooluse_malformed_tool_input_still_classifies():
    update = activity_update_for_event("PreToolUse", {"tool_name": "Read", "tool_input": "not a dict"})
    assert update["zone"] == "codebase"


def test_clearing_events():
    for event in ("UserPromptSubmit", "Stop", "StopFailure", "SessionStart", "SessionEnd"):
        assert activity_update_for_event(event, {}) is None, event


def test_notification_keeps_activity():
    assert activity_update_for_event("Notification", {}) is KEEP_ACTIVITY


# ── StatusStore activity persistence ────────────────────────────────────


def _zone(store, session):
    return (store.get(session)["activity"] or {}).get("zone")


def test_store_activity_set_and_cleared():
    store = StatusStore()
    store.set("s", "busy", None, activity={"zone": "aws", "tool": "Bash", "detail": "bash: aws s3 ls"})
    assert _zone(store, "s") == "aws"
    store.set("s", "free", None, activity=None)
    assert store.get("s")["activity"] is None


def test_store_keep_sentinel_preserves_activity():
    store = StatusStore()
    store.set("s", "busy", None, activity={"zone": "codebase", "tool": "Grep", "detail": "grep: foo"})
    # Notification (attention) and a legacy POST /status report both leave the zone alone.
    store.set("s", "attention", None, activity=KEEP_ACTIVITY)
    assert _zone(store, "s") == "codebase"
    store.set("s", "busy", None)  # legacy default
    assert _zone(store, "s") == "codebase"


def test_store_set_returns_previous_entry():
    store = StatusStore()
    assert store.set("s", "busy", None) == {}
    previous = store.set("s", "free", None)
    assert previous["state"] == "busy"


def test_store_unreported_session_has_null_activity():
    assert StatusStore().get("nobody")["activity"] is None


# ── hook_state: Notification refinement ─────────────────────────────────


def test_permission_notification_stays_attention():
    assert hook_state("Notification", {"message": "Claude needs your permission to use Bash"}) == "attention"


def test_idle_notification_downgrades_to_free():
    assert hook_state("Notification", {"message": "Claude is waiting for your input"}) == "free"


def test_idle_notification_match_is_case_insensitive():
    assert hook_state("Notification", {"message": "CLAUDE IS WAITING FOR YOUR INPUT"}) == "free"


def test_notification_without_message_stays_attention():
    # Fail loud: missing/odd payloads must never soften a possible "needs you".
    assert hook_state("Notification", {}) == "attention"
    assert hook_state("Notification", {"message": None}) == "attention"
    assert hook_state("Notification", {"message": 42}) == "attention"


def test_unrecognized_notification_wording_stays_attention():
    assert hook_state("Notification", {"message": "Some future notification text"}) == "attention"


def test_non_notification_events_ignore_the_message():
    assert hook_state("Stop", {"message": "waiting for your input"}) == "free"
    assert hook_state("PreToolUse", {"message": "waiting for your input"}) == "busy"
