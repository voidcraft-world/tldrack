import json
import os

import pytest

from tldrack.transcript import (
    TranscriptTracker,
    fold_background_agents,
    is_allowed_transcript,
    normalize_transcript_path,
    parse_entries,
    scan_lines,
)


@pytest.fixture(autouse=True)
def _transcript_root(tmp_path, monkeypatch):
    """Point the transcript allowlist at tmp_path for every test in this file.

    `transcript_path` arrives in an HTTP body, so the tracker only opens files
    under the Claude config dir. These tests write their fixtures to tmp_path,
    which is outside it — so they relocate the root rather than bypass the
    check, keeping the guard on the code path under test.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


def _line(**message):
    return json.dumps({"type": "assistant", "message": message}).encode()


def _usage(i=0, o=0, cc=0, cr=0):
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def test_scan_lines_sums_usage_and_takes_last_model():
    lines = [
        _line(model="claude-opus-4-8", usage=_usage(i=1, o=2)),
        _line(model="claude-sonnet-5", usage=_usage(i=3, o=4, cr=5)),
    ]
    model, totals = scan_lines(lines)
    assert model == "claude-sonnet-5"  # last assistant line wins
    assert totals == _usage(i=4, o=6, cr=5)


def test_scan_lines_skips_non_assistant_and_garbage():
    lines = [
        b"not json",
        json.dumps({"type": "user", "message": {"model": "x", "usage": _usage(i=99)}}).encode(),
        _line(model="claude-haiku-4-5", usage=_usage(o=7)),
        b"",
    ]
    model, totals = scan_lines(lines)
    assert model == "claude-haiku-4-5"
    assert totals == _usage(o=7)  # the user line's usage is ignored


def test_scan_lines_empty():
    assert scan_lines([]) == (None, _usage())


def _write(path, *lines):
    with open(path, "wb") as handle:
        handle.writelines(line + b"\n" for line in lines)


def test_tracker_missing_path_returns_none():
    tracker = TranscriptTracker()
    assert tracker.update("s", None) == (None, None)
    assert tracker.update("s", "") == (None, None)


def test_tracker_reads_and_accumulates_incrementally(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()

    _write(path, _line(model="claude-opus-4-8", usage=_usage(i=1, o=2)))
    model, usage = tracker.update("s", path)
    assert model == "claude-opus-4-8"
    assert usage == _usage(i=1, o=2)

    # Append a second turn — only the new line should be folded in.
    with open(path, "ab") as handle:
        handle.write(_line(model="claude-opus-4-8", usage=_usage(o=3, cr=10)) + b"\n")
    model, usage = tracker.update("s", path)
    assert usage == _usage(i=1, o=5, cr=10)


def test_tracker_ignores_partial_trailing_line(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _line(model="claude-opus-4-8", usage=_usage(o=2)))
    # A half-written line with no trailing newline must not be counted yet.
    with open(path, "ab") as handle:
        handle.write(b'{"type":"assistant","message":{"model":"claude-opus-4-8","usage":{"output_tokens":9')
    model, usage = tracker.update("s", path)
    assert usage == _usage(o=2)


def test_tracker_new_transcript_path_resets_totals(tmp_path):
    tracker = TranscriptTracker()
    first = str(tmp_path / "a.jsonl")
    second = str(tmp_path / "b.jsonl")
    _write(first, _line(model="claude-opus-4-8", usage=_usage(o=5)))
    _write(second, _line(model="claude-sonnet-5", usage=_usage(o=7)))
    assert tracker.update("s", first)[1] == _usage(o=5)
    # New Claude session (different transcript) in the same tmux session → fresh totals.
    model, usage = tracker.update("s", second)
    assert model == "claude-sonnet-5"
    assert usage == _usage(o=7)


def test_tracker_truncation_resets_and_resums(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _line(usage=_usage(o=4)), _line(usage=_usage(o=6)))
    assert tracker.update("s", path)[1] == _usage(o=10)
    # File shrinks below our cursor (compaction/rotation) → re-sum from the top.
    _write(path, _line(model="claude-opus-4-8", usage=_usage(o=1)))
    assert tracker.update("s", path)[1] == _usage(o=1)


def test_tracker_same_path_vanishing_keeps_prior(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _line(model="claude-opus-4-8", usage=_usage(o=3)))
    assert tracker.update("s", path)[1] == _usage(o=3)
    os.remove(path)
    # Same path, now unreadable (transient race) → keep the last-known reading.
    model, usage = tracker.update("s", path)
    assert model == "claude-opus-4-8"
    assert usage == _usage(o=3)


# --- path allowlist -------------------------------------------------------
# `transcript_path` comes off the wire in POST /hook, so it is untrusted. Before
# the 2026-08-01 fix the tracker opened whatever it was handed and read the file
# whole, so any page could auto-submit a cross-origin form and make the bridge
# slurp an arbitrary file into RAM.


def test_allows_a_transcript_under_the_config_dir(tmp_path):
    path = tmp_path / "projects" / "proj" / "abc.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    assert is_allowed_transcript(str(path)) is True


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "/Users/someone/Movies/big.mov",
        "",
        None,
        42,
    ],
)
def test_refuses_paths_outside_the_config_dir(candidate):
    assert is_allowed_transcript(candidate) is False


def test_refuses_traversal_back_out_of_the_config_dir(tmp_path):
    # realpath collapses `..` before the prefix test, so this cannot escape.
    assert is_allowed_transcript(f"{tmp_path}/../../../../etc/hosts.jsonl") is False


def test_refuses_a_symlink_pointing_out_of_the_config_dir(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-secret.jsonl"
    outside.write_bytes(b"")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))

    link = root / "innocent.jsonl"
    link.symlink_to(outside)

    # Judged on the real target, not on where the link sits.
    assert is_allowed_transcript(str(link)) is False


def test_refuses_a_non_jsonl_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")
    assert is_allowed_transcript(str(path)) is False


def test_tracker_refused_path_keeps_prior_instead_of_raising(tmp_path):
    """A refused path behaves like a missing one — a bad hook must not 500."""
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _line(model="claude-opus-4-8", usage=_usage(o=3)))
    assert tracker.update("s", path)[1] == _usage(o=3)

    model, usage = tracker.update("s", "/etc/passwd")
    assert model == "claude-opus-4-8"
    assert usage == _usage(o=3)


def test_tracker_refused_path_with_no_prior_returns_nothing(tmp_path):
    assert TranscriptTracker().update("s", "/etc/passwd") == (None, None)


def test_read_is_bounded(tmp_path, monkeypatch):
    """The unbounded `handle.read()` is what turned a named path into an OOM."""
    import tldrack.transcript as transcript_module

    monkeypatch.setattr(transcript_module, "MAX_READ_BYTES", 64)
    path = str(tmp_path / "t.jsonl")
    # Far more than the cap; the tracker must not pull it all in at once.
    _write(path, *[_line(usage=_usage(o=1)) for _ in range(200)])

    tracker = TranscriptTracker()
    _, usage = tracker.update("s", path)

    assert usage["output_tokens"] < 200, "read was not capped"


# --- the join table -------------------------------------------------------
# `paths()` is how a statusline report (POST /usage, which names no tmux
# session) finds its session — see statusline_models.py.


def _transcript(tmp_path, name="abc.jsonl", body=b""):
    path = tmp_path / "projects" / "proj" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_paths_binds_on_a_hook_that_found_no_model(tmp_path):
    """THE regression test for this whole bug. At session start the transcript
    exists but has no assistant entry yet, so `update` returns no model — the
    binding must be recorded anyway, or the statusline tier has nothing to join
    on for exactly the sessions that need it."""
    path = _transcript(tmp_path, body=b'{"type":"user","message":{"role":"user"}}\n')
    tracker = TranscriptTracker()
    model, _ = tracker.update("vc-9", str(path))
    assert model is None
    assert tracker.paths() == {"vc-9": os.path.realpath(str(path))}


def test_paths_are_resolved_not_raw(tmp_path):
    # Both sides of the join normalize through normalize_transcript_path, so a
    # symlinked or `..`-laden path from one side still matches the other.
    path = _transcript(tmp_path)
    tracker = TranscriptTracker()
    tracker.update("vc-9", f"{path.parent}/../proj/{path.name}")
    assert tracker.paths() == {"vc-9": normalize_transcript_path(str(path))}


def test_paths_omits_a_session_whose_path_was_refused(tmp_path):
    tracker = TranscriptTracker()
    tracker.update("vc-9", "/etc/hosts.jsonl")
    assert tracker.paths() == {}


def test_paths_follows_a_new_claude_session_in_the_same_pane(tmp_path):
    """`/clear` mints a new transcript. The join key rotates with it, which a
    name-keyed store could not do."""
    first = _transcript(tmp_path, "one.jsonl")
    second = _transcript(tmp_path, "two.jsonl")
    tracker = TranscriptTracker()
    tracker.update("vc-9", str(first))
    tracker.update("vc-9", str(second))
    assert tracker.paths() == {"vc-9": os.path.realpath(str(second))}


def test_rename_moves_the_binding(tmp_path):
    # Without this a renamed session loses its statusline join and drops back
    # to an unknown model — same bug class as SessionModelStore.rename.
    path = _transcript(tmp_path)
    tracker = TranscriptTracker()
    tracker.update("old", str(path))
    assert tracker.rename("old", "new") is True
    assert tracker.paths() == {"new": os.path.realpath(str(path))}


def test_rename_of_an_unknown_session_is_false(tmp_path):
    assert TranscriptTracker().rename("nope", "new") is False


def test_paths_binds_before_the_transcript_file_exists(tmp_path):
    # SessionStart can fire before Claude has written the file. The binding is
    # still what the statusline tier joins on, so it must survive the OSError.
    path = tmp_path / "projects" / "proj" / "not-yet.jsonl"
    path.parent.mkdir(parents=True)
    tracker = TranscriptTracker()
    assert tracker.update("vc-9", str(path)) == (
        None,
        {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    assert tracker.paths() == {"vc-9": os.path.realpath(str(path))}


def test_normalize_returns_the_resolved_path_not_the_input(tmp_path):
    path = _transcript(tmp_path)
    messy = f"{path.parent}/../proj/{path.name}"
    assert normalize_transcript_path(messy) == os.path.realpath(str(path))
    assert normalize_transcript_path("/etc/hosts.jsonl") is None
    assert normalize_transcript_path(str(path).removesuffix(".jsonl")) is None
    assert normalize_transcript_path(None) is None


# ── multiple Claude accounts (launch profiles) ─────────────────────────────


@pytest.fixture
def _restore_roots():
    from tldrack import transcript

    yield
    transcript.configure_transcript_roots(())


def test_a_configured_second_account_root_is_allowed(tmp_path, _restore_roots):
    from tldrack.transcript import configure_transcript_roots

    primary = tmp_path.parent / f"{tmp_path.name}-primary"
    (primary / "projects" / "p").mkdir(parents=True)
    path = primary / "projects" / "p" / "s.jsonl"
    path.write_bytes(b"")
    assert is_allowed_transcript(str(path)) is False  # one root: the bug

    configure_transcript_roots([str(primary)])
    assert normalize_transcript_path(str(path)) == os.path.realpath(path)


def test_a_sibling_sharing_the_prefix_is_still_refused(tmp_path, _restore_roots):
    # `~/.claude` must never admit `~/.claude-evil` just because the string
    # starts the same — the test is separator-aware per root.
    from tldrack.transcript import configure_transcript_roots

    sibling = tmp_path.parent / f"{tmp_path.name}-evil"
    sibling.mkdir(exist_ok=True)
    path = sibling / "x.jsonl"
    path.write_bytes(b"")
    configure_transcript_roots([])
    assert is_allowed_transcript(str(path)) is False


def test_a_symlink_out_of_a_configured_root_is_refused(tmp_path, _restore_roots):
    from tldrack.transcript import configure_transcript_roots

    extra = tmp_path / "second"
    extra.mkdir()
    outside = tmp_path.parent / "elsewhere-secret.jsonl"
    outside.write_bytes(b"")
    link = extra / "innocent.jsonl"
    link.symlink_to(outside)
    configure_transcript_roots([str(extra)])
    assert is_allowed_transcript(str(link)) is False


def test_forget_drops_the_binding(tmp_path):
    path = str(tmp_path / "t.jsonl")
    _write(path, _line(model="claude-opus-4-8", usage=_usage(o=1)))
    tracker = TranscriptTracker()
    tracker.update("vc-3", path)
    assert "vc-3" in tracker.paths()
    assert tracker.forget("vc-3") is True
    assert "vc-3" not in tracker.paths()
    assert tracker.forget("vc-3") is False


# ── background agents (shapes measured on 2.1.299, 2026-10-14) ────────────────


def _launch(agent_id):
    return json.dumps(
        {
            "type": "user",
            "toolUseResult": {
                "isAsync": True,
                "status": "async_launched",
                "agentId": agent_id,
                "description": "Find the state switches",
            },
        }
    ).encode()


def _notification(task_id, status="completed", shape="str"):
    text = (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>toolu_01</tool-use-id>\n"
        f"<status>{status}</status>\n<summary>Agent finished</summary>\n</task-notification>"
    )
    if shape == "queue":
        return json.dumps({"type": "queue-operation", "operation": "enqueue", "content": text}).encode()
    content = text if shape == "str" else [{"type": "text", "text": text}]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}).encode()


def _fold(*lines, start=frozenset()):
    return fold_background_agents(start, parse_entries(list(lines)))


def test_a_launch_without_a_completion_is_open():
    assert _fold(_launch("a0000000000000001")) == {"a0000000000000001"}


def test_a_completion_closes_its_launch():
    assert _fold(_launch("a1"), _notification("a1")) == frozenset()


@pytest.mark.parametrize("status", ["failed", "killed"])
def test_every_terminal_status_closes(status):
    assert _fold(_launch("a1"), _notification("a1", status)) == frozenset()


def test_a_non_terminal_status_does_not_close():
    assert _fold(_launch("a1"), _notification("a1", "running")) == {"a1"}


def test_an_unknown_task_id_is_ignored():
    assert _fold(_launch("a1"), _notification("bzz99")) == {"a1"}


@pytest.mark.parametrize("shape", ["list", "queue"])
def test_list_content_and_queued_notifications_close_too(shape):
    assert _fold(_launch("a1"), _notification("a1", shape=shape)) == frozenset()


def test_shell_and_monitor_jobs_are_not_agents():
    # Measured: a Bash job's result carries `backgroundTaskId`, a Monitor's
    # `taskId` — the process tree counts both.
    bash = json.dumps({"type": "user", "toolUseResult": {"backgroundTaskId": "b0cccccc3"}}).encode()
    monitor = json.dumps({"type": "user", "toolUseResult": {"taskId": "b0bbbbbb2", "persistent": False}}).encode()
    assert _fold(bash, monitor) == frozenset()


def test_the_tracker_folds_agents_incrementally_and_resets_on_a_new_transcript(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _launch("a1"), _launch("a2"))
    tracker.update("s", path)
    assert tracker.open_agents("s") == 2
    with open(path, "ab") as handle:
        handle.write(_notification("a1") + b"\n")
    tracker.update("s", path)
    assert tracker.open_agents("s") == 1
    # A new conversation in the same tmux session starts from nothing.
    other = str(tmp_path / "u.jsonl")
    _write(other, _line(model="claude-opus-4-8"))
    tracker.update("s", other)
    assert tracker.open_agents("s") == 0
    assert tracker.open_agents("never-seen") == 0


# ── closing text (open_work.py judges it) ─────────────────────────────────────

from tldrack.transcript import CLOSING_TEXT_MAX_CHARS, fold_closing_text  # noqa: E402


def _assistant(message_id, *blocks, sidechain=False):
    entry = {"type": "assistant", "message": {"id": message_id, "content": list(blocks)}}
    if sidechain:
        entry["isSidechain"] = True
    return entry


def _text(value):
    return {"type": "text", "text": value}


def test_closing_text_joins_one_messages_blocks_and_a_new_message_replaces_it():
    entries = [
        _assistant("m1", _text("Looking at the store first.")),
        _assistant("m1", {"type": "tool_use", "name": "Read", "input": {}}),
        _assistant("m2", _text("Phases A and B are done.")),
        _assistant("m2", _text("Phase C is not started.")),
    ]
    assert fold_closing_text((None, ""), entries) == ("m2", "Phases A and B are done.\nPhase C is not started.")


def test_a_tool_only_last_message_leaves_no_closing_text():
    entries = [_assistant("m1", _text("Let me check.")), _assistant("m2", {"type": "tool_use", "name": "Bash"})]
    assert fold_closing_text((None, ""), entries) == ("m2", "")


def test_subagent_and_user_entries_never_speak_for_the_main_thread():
    entries = [
        _assistant("m1", _text("Main thread done.")),
        _assistant("s1", _text("Subagent: C not started."), sidechain=True),
        {"type": "user", "message": {"content": "thanks"}},
    ]
    assert fold_closing_text((None, ""), entries) == ("m1", "Main thread done.")


def test_closing_text_keeps_the_tail():
    entries = [_assistant("m1", _text("x" * CLOSING_TEXT_MAX_CHARS + "THE END"))]
    _, text = fold_closing_text((None, ""), entries)
    assert len(text) == CLOSING_TEXT_MAX_CHARS and text.endswith("THE END")


def test_the_tracker_serves_closing_text_across_incremental_reads(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_assistant("m1", _text("first part"))) + "\n")
    tracker = TranscriptTracker()
    tracker.update("vc-1", str(path))
    with path.open("a") as handle:
        handle.write(json.dumps(_assistant("m1", _text("second part"))) + "\n")
    tracker.update("vc-1", str(path))
    assert tracker.closing_text("vc-1") == "first part\nsecond part"
    assert tracker.closing_text("nobody") is None


# ── stop failure (a turn an API error cut off) ────────────────────────────────

from tldrack.transcript import fold_stop_failure  # noqa: E402

RESETS = 1700003600
LIMIT_TEXT = "You've hit your session limit · resets 3:00pm (Europe/Lisbon)"


def _limit_entry(text=LIMIT_TEXT, sidechain=False, **overrides):
    """An invented entry in the shape 2.1.306 writes for a session-limit turn."""
    entry = {
        "type": "assistant",
        "isSidechain": sidechain,
        "isApiErrorMessage": True,
        "error": "rate_limit",
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": RESETS, "rateLimitType": "five_hour"}},
        "message": {"id": "e1", "model": "<synthetic>", "content": [_text(text)]},
    }
    entry.update(overrides)
    return entry


def test_a_session_limit_entry_reads_as_its_window_and_exact_reset():
    assert fold_stop_failure(None, [_assistant("m1", _text("Working on it.")), _limit_entry()]) == {
        "error": "rate_limit",
        "limit": "five_hour",
        "resets_at": RESETS,
        "message": LIMIT_TEXT,
        "at": None,
    }


def test_the_next_real_reply_clears_it_and_a_user_entry_does_not():
    cut = fold_stop_failure(None, [_limit_entry()])
    assert fold_stop_failure(cut, [{"type": "user", "message": {"content": "continue"}}]) == cut
    assert fold_stop_failure(cut, [_assistant("m2", _text("Picking up where I left off."))]) is None


def test_a_subagent_hitting_the_limit_does_not_cut_off_the_main_thread():
    assert fold_stop_failure(None, [_assistant("m1", _text("done")), _limit_entry(sidechain=True)]) is None


def test_other_api_errors_keep_their_word_and_carry_no_window():
    overloaded = _limit_entry(text="API Error: overloaded", error="overloaded", apiErrorParams=None)
    assert fold_stop_failure(None, [overloaded]) == {
        "error": "overloaded",
        "limit": None,
        "resets_at": None,
        "message": "API Error: overloaded",
        "at": None,
    }


def test_malformed_fields_degrade_rather_than_reach_the_wire():
    entry = _limit_entry(text="", error="Rate Limit!", apiErrorParams={"rate_limit_info": {"rateLimitType": 7, "resetsAt": True}})
    assert fold_stop_failure(None, [entry]) == {
        "error": "unknown",
        "limit": None,
        "resets_at": None,
        "message": None,
        "at": None,
    }
    # Not an API error at all: an ordinary reply, whatever else it carries.
    assert fold_stop_failure(None, [_limit_entry(isApiErrorMessage="yes")]) is None


def test_a_cut_off_carries_its_entrys_own_time():
    # What orders it against the account's later turns (account_standing.py).
    stamped = _limit_entry(timestamp="2026-11-16T23:09:56.816Z")
    assert fold_stop_failure(None, [stamped])["at"] == pytest.approx(1794870596.816)
    for unreadable in ("yesterday", 1794870596, None, "2026-11-16T23:09:56"):
        assert fold_stop_failure(None, [_limit_entry(timestamp=unreadable)])["at"] is None


def test_refresh_finds_a_cut_off_turn_no_hook_announced(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_assistant("m1", _text("Starting phase B."))) + "\n")
    tracker = TranscriptTracker()
    tracker.update("vc-1", str(path))
    assert tracker.stop_failure("vc-1") is None
    assert tracker.refresh("vc-1") is False  # nothing new: one stat, no change
    with path.open("a") as handle:
        handle.write(json.dumps(_limit_entry()) + "\n")
    assert tracker.refresh("vc-1") is True
    assert tracker.stop_failure("vc-1")["resets_at"] == RESETS
    assert tracker.refresh("never-bound") is False


def test_clearing_sticks_until_a_new_entry_sets_it_again(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_limit_entry()) + "\n")
    tracker = TranscriptTracker()
    tracker.update("vc-1", str(path))
    assert tracker.clear_stop_failure("vc-1") is True
    assert tracker.refresh("vc-1") is False and tracker.stop_failure("vc-1") is None
    assert tracker.clear_stop_failure("vc-1") is False
    with path.open("a") as handle:
        handle.write(json.dumps(_limit_entry()) + "\n")  # "continue", still limited
    assert tracker.refresh("vc-1") is True


# ── workflow runs (shape measured on 2.1.319, 2026-11-09) ─────────────────────

from tldrack.transcript import (  # noqa: E402
    TRACE_EDITS_MAX,
    TRACE_PENDING_MAX,
    ends_on_question,
    fold_background_workflows,
    fold_turn_opening,
    fold_turn_trace,
    read_test_result,
)

#: A trace with nothing in it: what a prompt starts.
FRESH_TRACE = {"tests": False, "edits": frozenset(), "pr": False, "tests_passed": None, "commit": False, "pending": {}}


def _workflow(task_id, run_id="wf_00000000-aaa"):
    return json.dumps(
        {
            "type": "user",
            "toolUseResult": {
                "status": "async_launched",
                "taskId": task_id,
                "taskType": "local_workflow",
                "workflowName": "deep-research",
                "runId": run_id,
            },
        }
    ).encode()


def _wf_fold(*lines, start=frozenset()):
    return fold_background_workflows(start, parse_entries(list(lines)))


def test_a_workflow_launch_is_open_until_its_notification():
    assert _wf_fold(_workflow("w0aaaaaa1")) == {"w0aaaaaa1"}
    assert _wf_fold(_workflow("w0aaaaaa1"), _notification("w0aaaaaa1")) == frozenset()
    assert _wf_fold(_workflow("w1"), _notification("w1", "running")) == {"w1"}


def test_workflows_and_agents_never_cross():
    """A workflow names no `agentId`, an agent no `taskType`; a Monitor's `taskId` is neither."""
    monitor = json.dumps({"type": "user", "toolUseResult": {"taskId": "b0bbbbbb2", "persistent": False}}).encode()
    other = json.dumps(
        {"type": "user", "toolUseResult": {"status": "async_launched", "taskId": "r1", "taskType": "remote_agent"}}
    ).encode()
    assert _fold(_workflow("w1"), monitor) == frozenset()
    assert _wf_fold(_launch("a1"), monitor, other) == frozenset()


def test_the_tracker_counts_workflows_beside_agents(tmp_path):
    path = str(tmp_path / "t.jsonl")
    tracker = TranscriptTracker()
    _write(path, _launch("a1"), _workflow("w1"), _workflow("w2"))
    tracker.update("s", path)
    assert tracker.open_children("s") == (1, 2) and tracker.open_agents("s") == 3
    with open(path, "ab") as handle:
        handle.write(_notification("w1") + b"\n")
    tracker.update("s", path)
    assert tracker.open_children("s") == (1, 1)
    assert tracker.open_children("never-seen") == (0, 0)


# ── the turn's trace ──────────────────────────────────────────────────────────


def _prompt(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _tool(name, sidechain=False, **tool_input):
    return _assistant("m", {"type": "tool_use", "id": f"t-{name}", "name": name, "input": tool_input}, sidechain=sidechain)


def _result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


@pytest.mark.parametrize(
    "command, tests",
    [
        ("cd bridge && .venv/bin/python -m pytest -q tests/test_x.py", True),
        ("python3 .claude/hooks/affected_tests.py --run a.py", True),
        ("node --import ./loader.mjs --test tests/unit/a.test.ts", True),
        ("npm run test:unit", True),
        ("npm test", True),
        ("go test ./...", True),
        ("swift test", True),
        ("time node --test tests/unit/x.test.ts", True),
        ("cat tests/test_x.py", False),
        ("git log --oneline", False),
        ("ls pytest-cov", False),
        ("cat jest.config.js", False),
        ("python -m pytest", True),
    ],
)
def test_a_test_runner_is_read_off_the_command(command, tests):
    assert fold_turn_trace(None, [_tool("Bash", command=command)])["tests"] is tests


def test_a_turn_counts_distinct_edits_and_a_pull_request():
    trace = fold_turn_trace(
        None,
        [
            _tool("Edit", file_path="/a.py"),
            _tool("Write", file_path="/b.py"),
            _tool("MultiEdit", file_path="/a.py"),
            _tool("NotebookEdit", notebook_path="/n.ipynb"),
            _tool("Bash", command="gh pr create --fill"),
            _tool("Read", file_path="/c.py"),
        ],
    )
    assert (len(trace["edits"]), trace["pr"], trace["tests"]) == (3, True, False)


def test_a_prompt_starts_a_fresh_trace_and_context_inside_a_turn_does_not():
    worked = fold_turn_trace(None, [_tool("Edit", file_path="/a.py"), _tool("Bash", command="pytest")])
    skill = _prompt("Base directory for this skill: …", isMeta=True)
    assert fold_turn_trace(worked, [skill, _result()]) == worked, "a skill body and a tool result are mid-turn"
    for start in (
        _prompt("go on"),
        _prompt("<task-notification>…</task-notification>"),
        _prompt("Another Claude session sent a message", isMeta=True, turnOrigin="peer"),
        {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
    ):
        assert fold_turn_trace(worked, [start]) == FRESH_TRACE


def test_a_subagents_calls_never_speak_for_the_main_thread():
    trace = fold_turn_trace(
        None,
        [
            _tool("Edit", sidechain=True, file_path="/a.py"),
            _tool("Bash", sidechain=True, command="pytest"),
            _prompt("sub prompt", isSidechain=True),
        ],
    )
    assert trace == FRESH_TRACE


def test_the_edit_count_is_bounded():
    trace = fold_turn_trace(None, [_tool("Edit", file_path=f"/f{n}.py") for n in range(TRACE_EDITS_MAX + 5)])
    assert len(trace["edits"]) == TRACE_EDITS_MAX


@pytest.mark.parametrize(
    "text, asks",
    [
        ("Done. Want me to deploy?", True),
        ("Phase B shipped.\n\n**Ship phase C next?**\n", True),
        ("Shall I go on (y/n)?", True),
        ("Is it A? No: it was B.", False),
        ("", False),
        (None, False),
        ("What now?\n\nNothing left to do.", False),
        ("Ready to merge?\n`", True),
    ],
)
def test_ends_on_question_reads_the_last_line(text, asks):
    assert ends_on_question(text) is asks


def test_the_tracker_serves_the_turns_trace_as_counts(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        _prompt("fix it"),
        _tool("Edit", file_path="/a.py"),
        _tool("Bash", command="npm test"),
        _assistant("m2", _text("Fixed and tested. Deploy now?")),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    tracker = TranscriptTracker()
    assert tracker.turn_trace("vc-1") is None
    tracker.update("vc-1", str(path))
    assert tracker.turn_trace("vc-1") == {
        "ran_tests": True,
        "edited_files": 1,
        "opened_pr": False,
        "ended_on_question": True,
        "tests_passed": None,
        "made_commit": False,
    }


# ── the trace's two v4 facts: the last test run's result, and a commit ───────


def _call(call_id, command, sidechain=False):
    block = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    return _assistant(f"m-{call_id}", block, sidechain=sidechain)


def _answer(call_id, content, is_error=False, sidechain=False):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
    return {"type": "user", "isSidechain": sidechain, "message": {"content": [block]}}


@pytest.mark.parametrize(
    "output, errored, passed",
    [
        ("....\n12 passed in 0.41s", False, True),
        ("F...\nFAILED tests/test_x.py::test_a\n1 failed, 11 passed in 0.5s", False, False),
        ("# tests 9\n# pass 9\n# fail 0", False, True),
        ("# tests 9\n# pass 8\n# fail 1", False, False),
        ("Tests:       2 failed, 30 passed, 32 total", False, False),
        ("test result: ok. 4 passed; 0 failed", False, True),
        ("ok  \texample.com/pkg\t0.12s", False, True),
        ("Exit code 1\nsomething broke", True, False),
        ("Command running in background with ID: b1", False, None),
        ("collected 0 items", False, None),
        ("The user doesn't want to proceed with this tool use. The tool use was rejected", True, None),
    ],
)
def test_a_test_runs_result_says_passed_failed_or_nothing(output, errored, passed):
    assert read_test_result(output, errored=errored) is passed


def test_the_last_test_run_decides_and_unknown_stays_unknown():
    passed = fold_turn_trace(None, [_call("a", "pytest -q"), _answer("a", "3 passed in 0.1s")])
    assert (passed["tests"], passed["tests_passed"], passed["pending"]) == (True, True, {})
    failed = fold_turn_trace(passed, [_call("b", "pytest -q"), _answer("b", "Exit code 1\n1 failed", is_error=True)])
    assert failed["tests_passed"] is False, "a later failing run overrides an earlier pass"
    waiting = fold_turn_trace(failed, [_call("c", "npm test")])
    assert waiting["tests_passed"] is None, "a run whose result has not landed is unknown, never the previous one"
    assert fold_turn_trace(waiting, [_answer("c", "done")])["tests_passed"] is None, "output that says nothing"
    stale = fold_turn_trace(None, [_call("d", "pytest"), _call("e", "pytest"), _answer("d", "1 failed")])
    assert stale["tests_passed"] is None and stale["pending"] == {"e": "test"}, "only the newest run is waited on"


def test_a_result_pairs_with_its_call_across_reads():
    first = fold_turn_trace(None, [_call("a", "cd lab && uv run pytest -q tests/test_x.py")])
    assert first["pending"] == {"a": "test"}
    second = fold_turn_trace(first, [_answer("a", "5 passed")])
    assert second["tests_passed"] is True and first["pending"] == {"a": "test"}, "the fold is pure"


@pytest.mark.parametrize(
    "command, commits",
    [
        ('git commit -m "x"', True),
        ("git -C ~/r commit -am x", True),
        ("git add -A && git commit --amend --no-edit", True),
        ("git commit-tree abc", False),
        ("git log --grep commit", False),
        ("echo commit", False),
    ],
)
def test_a_commit_is_read_off_the_command(command, commits):
    trace = fold_turn_trace(None, [_call("a", command), _answer("a", "[main abc123] x")])
    assert trace["commit"] is commits


def test_a_commit_counts_only_once_its_result_came_back_clean():
    assert fold_turn_trace(None, [_call("a", "git commit -m x")])["commit"] is False, "no result yet"
    refused = [_call("a", "git commit -m x"), _answer("a", "Exit code 1\nnothing to commit", is_error=True)]
    assert fold_turn_trace(None, refused)["commit"] is False
    made = fold_turn_trace(None, [*refused, _call("b", "git commit -am x"), _answer("b", "[feat abc] x")])
    assert made["commit"] is True
    assert fold_turn_trace(made, [_prompt("next")])["commit"] is False, "a prompt starts a fresh trace"


def test_a_subagents_results_and_commits_never_speak_for_the_main_thread():
    trace = fold_turn_trace(None, [_call("a", "pytest"), _answer("a", "1 failed", sidechain=True)])
    assert trace["tests_passed"] is None
    trace = fold_turn_trace(None, [_call("b", "git commit -m x", sidechain=True), _answer("b", "[x] y", sidechain=True)])
    assert trace["commit"] is False


def test_the_calls_a_trace_waits_on_are_bounded():
    trace = fold_turn_trace(None, [_call(f"c{n}", "git commit -m x") for n in range(TRACE_PENDING_MAX + 5)])
    assert len(trace["pending"]) == TRACE_PENDING_MAX and "c0" not in trace["pending"]


def test_a_trace_folded_before_the_two_facts_still_reads():
    old = {"tests": True, "edits": frozenset({"/a.py"}), "pr": False}
    trace = fold_turn_trace(old, [_call("a", "git commit -m x"), _answer("a", "[x] y")])
    assert (trace["tests"], trace["tests_passed"], trace["commit"]) == (True, None, True)


def test_the_tracker_serves_the_last_runs_result_and_the_commit(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        _prompt("fix it"),
        _call("a", "pytest -q"),
        _answer("a", "7 passed in 0.2s"),
        _call("b", "git commit -m fix"),
        _answer("b", "[feat 1a2b3c] fix"),
        _assistant("m2", _text("Fixed, tested and committed.")),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    tracker = TranscriptTracker()
    tracker.update("vc-1", str(path))
    reading = tracker.turn_trace("vc-1")
    assert (reading["ran_tests"], reading["tests_passed"], reading["made_commit"]) == (True, True, True)


# ── the turn's opening (open_work.py § An answer to read) ────────────────────


def test_the_opening_is_the_prompt_that_started_the_current_turn():
    ask = _prompt("Why is CI red?")
    assert fold_turn_opening(None, [ask, _tool("Bash", command="pytest"), _result()]) == ask
    order = _prompt("Now fix it.")
    assert fold_turn_opening(ask, [order]) == order, "the next prompt opens the next turn"
    assert fold_turn_opening(None, []) is None


def test_context_inside_a_turn_and_a_subagents_prompt_never_open_one():
    ask = _prompt("Why is CI red?")
    skill = _prompt("Base directory for this skill: …", isMeta=True)
    sub = _prompt("sub prompt?", isSidechain=True)
    assert fold_turn_opening(ask, [skill, _result(), sub]) == ask


def test_a_machine_prompt_opens_the_turn_too_and_its_reader_finds_no_request():
    """The fold keeps whatever opened the turn, a task notification included: whether a person
    asked something is `open_work.turn_request`'s reading of it, never this fold's."""
    note = _prompt("<task-notification><task-id>a1</task-id><status>completed</status></task-notification>")
    assert fold_turn_opening(_prompt("Why is CI red?"), [note]) == note


@pytest.mark.parametrize(
    "prompt, asks",
    [
        ("Why does the ✅ vanish on restart?", True),
        ("Read the logs.\n\nWhat broke?\n", True),
        ("Why? Fix it.", False),
        ("Fix the flaky test.", False),
    ],
)
def test_ends_on_question_reads_a_prompt_by_the_same_last_line_rule(prompt, asks):
    """The landing trace's rule for a closing, reused for the request (open_work.py `asks_question`)."""
    assert ends_on_question(prompt) is asks


def test_the_tracker_serves_the_opening_and_keeps_it_across_reads(tmp_path):
    path = tmp_path / "t.jsonl"
    ask = _prompt("Is the build green?")
    path.write_text(json.dumps(ask) + "\n")
    tracker = TranscriptTracker()
    assert tracker.turn_opening("vc-1") is None
    tracker.update("vc-1", str(path))
    assert tracker.turn_opening("vc-1") == ask
    with path.open("a") as handle:
        handle.write(json.dumps(_assistant("m2", _text("Green."))) + "\n")
    tracker.update("vc-1", str(path))
    assert tracker.turn_opening("vc-1") == ask, "a reply is not a new turn: the incremental read keeps it"
