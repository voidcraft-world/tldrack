"""state_arbiter.py — the measured hook leaks, each corrected once and only once.

The timelines follow the leaks measured on Claude Code 2.1.294 (2026-10-08,
agent_index.py § WHY THIS); the background ones on 2.1.299 (2026-10-14,
agent_index.py § BACKGROUND WORK).
"""

from tldrack.activity import KEEP_ACTIVITY
from tldrack.agent_index import IndexEntry
from tldrack.main import StatusStore
from tldrack.state_arbiter import HOLD_SECONDS, StateArbiter, decide_correction, refine_turn_end, settled_state


def entry(state, at, session="vc-2", waiting_for=None):
    return IndexEntry(session, state, at, waiting_for, None, 1, "x")


def test_esc_mid_turn_unsticks_busy():
    # Hook: busy at 100 and nothing after. Index: idle at 110.
    current = {"state": "busy", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("free", 110.0), 110.0 + HOLD_SECONDS) == "free"


def test_the_index_must_hold_before_it_overrules():
    current = {"state": "busy", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("free", 110.0), 110.5) is None


def test_a_hook_newer_than_the_index_wins():
    # UserPromptSubmit at 120 after an idle the index recorded at 110.
    current = {"state": "busy", "wall_at": 120.0, "seq": 4}
    assert decide_correction(current, entry("free", 110.0), 130.0) is None


def test_agreement_writes_nothing():
    current = {"state": "free", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("free", 110.0), 130.0) is None


def test_a_granted_prompt_goes_back_to_busy():
    # Notification at 384.9 (attention); the grant shows in the index at 388.7.
    current = {"state": "attention", "wall_at": 384.9, "seq": 5}
    assert decide_correction(current, entry("busy", 388.7), 391.0) == "busy"


def test_a_denied_prompt_clears_attention():
    current = {"state": "attention", "wall_at": 228.8, "seq": 5}
    assert decide_correction(current, entry("free", 238.5), 241.0) == "free"


def test_the_prompt_reaches_attention_before_the_notification_hook():
    # PreToolUse busy at 378.0; index waiting at 378.9; the hook lands at 384.9.
    current = {"state": "busy", "wall_at": 378.0, "seq": 4}
    assert decide_correction(current, entry("attention", 378.9), 381.0) == "attention"


def test_a_session_with_no_hooks_still_gets_a_state():
    assert decide_correction(None, entry("busy", 10.0), 20.0) == "busy"


# ── background: the turn is over, work still runs ──────────────────────────────


def test_stop_then_shell_is_background():
    # Stop hook at 200 (free); the index went `shell` a moment later.
    current = {"state": "free", "wall_at": 200.0, "seq": 6}
    assert decide_correction(current, entry("background", 200.152), 200.152 + HOLD_SECONDS) == "background"


def test_shell_must_hold_like_any_other_index_status():
    current = {"state": "free", "wall_at": 200.0, "seq": 6}
    assert decide_correction(current, entry("background", 200.152), 201.0) is None


def test_the_job_ending_settles_to_free_whatever_the_order():
    # The Stop hook already stored background at 300; the job ended just
    # before it and the index says idle with an OLDER stamp. Rule 2 would let
    # the hook pin it; in the settle zone the live evidence wins.
    current = {"state": "background", "wall_at": 300.0, "seq": 7}
    assert decide_correction(current, entry("free", 299.0), 305.0) == "free"


def test_a_stop_hook_cannot_pin_free_while_the_index_says_shell():
    current = {"state": "free", "wall_at": 300.0, "seq": 7}
    assert decide_correction(current, entry("background", 250.0), 305.0) == "background"


def test_caffeinate_only_is_free():
    # The process table had children, none of them work: shells == 0.
    current = {"state": "free", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("free", 99.0), 110.0, shells=0) is None
    assert decide_correction(None, entry("free", 99.0), 110.0, shells=0) == "free"


def test_idle_with_live_job_children_is_background():
    # The version-proof second source: the index says idle, the tree says work.
    current = {"state": "free", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("free", 99.0), 110.0, shells=2) == "background"


def test_a_foreground_child_in_a_busy_turn_stays_busy():
    # A foreground Bash is the same wrapper; children are not consulted in a turn.
    current = {"state": "busy", "wall_at": 100.0, "seq": 3}
    assert decide_correction(current, entry("busy", 99.0), 110.0, shells=1) is None
    assert decide_correction(None, entry("busy", 99.0), 110.0, shells=1) == "busy"


def test_background_agents_keep_the_index_busy_after_stop():
    # Turn began at 150 (index busy); Stop hook at 200; Claude is still busy
    # because a background agent runs (delegatedActive).
    current = {"state": "free", "wall_at": 200.0, "seq": 8}
    assert decide_correction(current, entry("busy", 150.0), 201.0) is None
    assert decide_correction(current, entry("busy", 150.0), 200.0 + HOLD_SECONDS) == "background"


def test_background_agents_agreeing_writes_nothing():
    current = {"state": "background", "wall_at": 205.0, "seq": 9}
    assert decide_correction(current, entry("busy", 150.0), 260.0) is None


def test_a_mid_turn_session_start_reads_background_not_free():
    # Compaction fires SessionStart (free) inside a turn the index still calls
    # busy. Not free — the next PreToolUse puts it back to busy.
    current = {"state": "free", "wall_at": 400.0, "seq": 10}
    assert decide_correction(current, entry("busy", 380.0), 403.0) == "background"


def test_a_new_turn_hook_still_beats_a_background_index():
    # UserPromptSubmit at 320 after the index went shell at 310.
    current = {"state": "busy", "wall_at": 320.0, "seq": 11}
    assert decide_correction(current, entry("background", 310.0), 330.0) is None


def test_the_index_starting_a_turn_overrules_background():
    current = {"state": "background", "wall_at": 300.0, "seq": 11}
    assert decide_correction(current, entry("busy", 310.0), 313.0) == "busy"


class FakeReader:
    def __init__(self, index, running=()):
        self.index = index
        self.running = set(running)

    def read(self):
        return dict(self.index)

    def still_running(self, entry):
        return entry.pid in self.running


class FakeProcesses:
    """A process table the test owns — never this machine's real `ps`."""

    def __init__(self, text=""):
        self.text = text
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.text


def job(pid, ppid):
    return f"{pid} {ppid} /bin/zsh -c source /h/.claude/shell-snapshots/snapshot-zsh-1-a.sh && eval 'sleep 300'\n"


def arbiter_for(store, index, now, log, processes=None, reader=None):
    return StateArbiter(
        store,
        reader or FakeReader(index),
        on_correction=lambda s, prev, state, act: log.append((s, prev.get("state"), state, act)),
        on_changed=lambda: log.append("changed"),
        process_table=processes or FakeProcesses(),
        clock=lambda: now,
    )


def test_tick_writes_a_correction_once_and_clears_the_zone_on_free():
    store = StatusStore()
    store.set("vc-2", "busy", None, activity={"zone": "codebase"})
    wall = store.versions()["vc-2"]["wall_at"]
    log = []
    arbiter = arbiter_for(store, {"vc-2": entry("free", wall + 1)}, wall + 1 + HOLD_SECONDS, log)

    assert arbiter.tick() == [("vc-2", "free")]
    row = store.get("vc-2")
    assert (row["state"], row["activity"], row["state_source"]) == ("free", None, "index")
    assert log == [("vc-2", "busy", "free", None), "changed"]
    # The correction stamped wall_at past the index's status_at: no second write.
    assert arbiter.tick() == []


def test_an_attention_correction_carries_claudes_waiting_label():
    store = StatusStore()
    store.set("vc-2", "busy", None, activity={"zone": "computer"})
    wall = store.versions()["vc-2"]["wall_at"]
    arbiter = arbiter_for(store, {"vc-2": entry("attention", wall + 1, waiting_for="permission prompt")}, wall + 5, [])
    arbiter.tick()
    row = store.get("vc-2")
    assert (row["state"], row["message"], row["activity"]) == ("attention", "permission prompt", {"zone": "computer"})


def test_correct_loses_to_a_hook_that_landed_in_between():
    store = StatusStore()
    store.set("vc-2", "busy", None)
    seen = store.versions()["vc-2"]["seq"]
    store.set("vc-2", "attention", None)  # the hook, after the arbiter looked
    assert store.correct("vc-2", seen, "free", None) is None
    assert store.get("vc-2")["state"] == "attention"


def test_correct_creates_a_row_only_while_there_is_still_none():
    store = StatusStore()
    assert store.correct("vc-9", None, "busy", None) == {}
    assert store.correct("vc-9", None, "free", None) is None


def test_on_index_sees_every_read():
    store = StatusStore()
    seen = []
    arbiter = StateArbiter(
        store,
        FakeReader({"vc-2": entry("busy", 1.0)}),
        on_correction=lambda *a: None,
        on_changed=lambda: None,
        on_index=seen.append,
        process_table=FakeProcesses(),
        clock=lambda: 100.0,
    )
    arbiter.tick()
    assert list(seen[0]) == ["vc-2"]
    assert list(arbiter.last_index()) == ["vc-2"]


def test_a_hook_write_is_tagged_hook():
    store = StatusStore()
    store.set("vc-2", "busy", None, activity=KEEP_ACTIVITY)
    assert store.get("vc-2")["state_source"] == "hook"


def test_tick_reads_the_process_table_once_not_once_per_row():
    store = StatusStore()
    processes = FakeProcesses(job(50, 1) + job(51, 2))
    index = {
        "vc-2": IndexEntry("vc-2", "free", 1.0, None, None, 1, "x"),
        "vc-3": IndexEntry("vc-3", "free", 1.0, None, None, 2, "x"),
        "vc-4": IndexEntry("vc-4", "busy", 1.0, None, None, 3, "x"),
    }
    arbiter = arbiter_for(store, index, 100.0, [], processes)
    arbiter.tick()
    assert processes.calls == 1
    assert arbiter.last_work() == {"vc-2": 1, "vc-3": 1, "vc-4": 0}
    assert [store.get(s)["state"] for s in ("vc-2", "vc-3", "vc-4")] == ["background", "background", "busy"]


def test_no_believable_entries_means_no_ps_at_all():
    processes = FakeProcesses()
    arbiter_for(StatusStore(), {}, 100.0, [], processes).tick()
    assert processes.calls == 0


def test_a_background_correction_clears_the_zone_like_stop():
    store = StatusStore()
    store.set("vc-2", "busy", None, activity={"zone": "tests"})
    wall = store.versions()["vc-2"]["wall_at"]
    arbiter = arbiter_for(store, {"vc-2": entry("background", wall + 1)}, wall + 5, [])
    arbiter.tick()
    assert (store.get("vc-2")["state"], store.get("vc-2")["activity"]) == ("background", None)


def test_a_job_count_change_pushes_a_snapshot_without_a_state_change():
    store = StatusStore()
    processes = FakeProcesses(job(50, 1) + job(51, 1))
    log = []
    arbiter = arbiter_for(store, {"vc-2": entry("background", 1.0)}, 100.0, log, processes)
    arbiter.tick()
    assert log[-1] == "changed"
    log.clear()
    arbiter.tick()
    assert log == []  # nothing moved
    processes.text = job(50, 1)
    arbiter.tick()
    assert log == ["changed"]  # 2 jobs → 1 job, still background
    assert arbiter.last_work() == {"vc-2": 1}


def test_busy_row_churn_does_not_push():
    store = StatusStore()
    store.set("vc-2", "busy", None)
    wall = store.versions()["vc-2"]["wall_at"]
    processes = FakeProcesses(job(50, 1))
    log = []
    arbiter = arbiter_for(store, {"vc-2": entry("busy", wall - 1)}, wall + 10, log, processes)
    arbiter.tick()
    processes.text = ""
    arbiter.tick()
    assert log == []


def test_a_dead_pid_releases_its_background_row():
    store = StatusStore()
    reader = FakeReader({"vc-2": entry("background", 1.0)}, running={1})
    log = []
    arbiter = arbiter_for(store, None, 100.0, log, reader=reader)
    arbiter.tick()
    assert store.get("vc-2")["state"] == "background"
    reader.index = {}  # the entry is no longer believed…
    reader.running = set()  # …because its Claude crashed: no SessionEnd fired
    assert arbiter.tick() == [("vc-2", "free")]
    assert store.get("vc-2")["state"] == "free"
    assert arbiter.tick() == []  # released once


def test_a_recycled_pid_releases_its_background_row():
    store = StatusStore()
    reader = FakeReader({"vc-2": entry("background", 1.0)}, running={1})
    arbiter = arbiter_for(store, None, 100.0, [], reader=reader)
    arbiter.tick()
    reader.index = {}
    reader.running = set()  # still_running is False for a pid now owned by another process
    arbiter.tick()
    assert store.get("vc-2")["state"] == "free"


def test_an_entry_missing_for_one_read_is_left_alone():
    # Rewritten mid-read: the entry vanished, the process did not.
    store = StatusStore()
    reader = FakeReader({"vc-2": entry("background", 1.0)}, running={1})
    arbiter = arbiter_for(store, None, 100.0, [], reader=reader)
    arbiter.tick()
    reader.index = {}
    assert arbiter.tick() == []
    assert store.get("vc-2")["state"] == "background"


def test_a_departed_session_that_was_not_background_is_not_touched():
    store = StatusStore()
    store.set("vc-2", "busy", None)
    wall = store.versions()["vc-2"]["wall_at"]
    reader = FakeReader({"vc-2": entry("busy", wall - 1)}, running={1})
    arbiter = arbiter_for(store, None, wall + 10, [], reader=reader)
    arbiter.tick()
    reader.index, reader.running = {}, set()
    assert arbiter.tick() == []
    assert store.get("vc-2")["state"] == "busy"


# ── refine_turn_end: the hook path, before any client sees a free frame ────────


def evidence(shells, agents, calls=None):
    def probe():
        if calls is not None:
            calls.append(1)
        return shells, agents

    return probe


def test_stop_with_a_live_job_stores_background():
    assert refine_turn_end("free", "Stop", evidence(1, 0)) == "background"


def test_stop_with_an_open_background_agent_stores_background():
    assert refine_turn_end("free", "Stop", evidence(0, 1)) == "background"


def test_stop_with_no_work_stays_free():
    assert refine_turn_end("free", "Stop", evidence(0, 0)) == "free"


def test_the_idle_nag_cannot_flash_a_background_row_to_free():
    # "Claude is waiting for your input" maps to free ~60 s after Stop.
    assert refine_turn_end("free", "Notification", evidence(2, 0)) == "background"


def test_session_end_is_never_refined_and_never_probes():
    calls = []
    assert refine_turn_end("free", "SessionEnd", evidence(3, 1, calls)) == "free"
    assert calls == []


def test_non_free_hooks_never_probe():
    calls = []
    for state, event in (("busy", "PreToolUse"), ("busy", "UserPromptSubmit"), ("attention", "Notification")):
        assert refine_turn_end(state, event, evidence(3, 1, calls)) == state
    assert calls == []


# ── the settle pass (main.py re-tails turn-over transcripts there) ────────────


def test_a_settle_pass_that_moved_something_pushes_a_snapshot():
    log = []
    moved = [False]
    arbiter = StateArbiter(
        StatusStore(),
        FakeReader({}),
        on_correction=lambda *a: None,
        on_changed=lambda: log.append("changed"),
        on_settled=lambda: moved[0],
        process_table=FakeProcesses(),
        clock=lambda: 100.0,
    )
    arbiter.tick()
    assert log == []
    moved[0] = True
    arbiter.tick()
    assert log == ["changed"]


def test_a_failing_settle_pass_still_lets_the_ticks_corrections_push(capsys):
    store = StatusStore()
    store.set("vc-2", "busy", None)
    wall = store.versions()["vc-2"]["wall_at"]
    log = []

    def broken():
        raise OSError("transcript vanished")

    arbiter = StateArbiter(
        store,
        FakeReader({"vc-2": entry("free", wall + 1)}),
        on_correction=lambda *a: None,
        on_changed=lambda: log.append("changed"),
        on_settled=broken,
        process_table=FakeProcesses(),
        clock=lambda: wall + 1 + HOLD_SECONDS,
    )
    assert arbiter.tick() == [("vc-2", "free")]
    assert log == ["changed"]
    assert "settle pass failed" in capsys.readouterr().out


# ── the landing read: index_since and settled_state (attention.py § 1b) ──────


class CountingReader(FakeReader):
    def __init__(self, index):
        super().__init__(index)
        self.reads = 0

    def read(self):
        self.reads += 1
        return super().read()


def test_index_since_refreshes_a_tick_older_than_the_write_and_keeps_the_ticks_pair():
    now = [100.0]
    reader = CountingReader({"vc-2": entry("busy", 90.0)})
    arbiter = StateArbiter(
        StatusStore(),
        reader,
        on_correction=lambda *a: None,
        on_changed=lambda: None,
        process_table=FakeProcesses(),
        clock=lambda: now[0],
    )
    assert arbiter.index_since(50.0) == {} and reader.reads == 0, "never ticked: no read of this Mac's index"
    arbiter.tick()
    assert reader.reads == 1
    assert arbiter.index_since(100.0)["vc-2"].state == "busy" and reader.reads == 1, "a tick at the write is fresh"
    reader.index = {"vc-2": entry("free", 101.0)}
    assert arbiter.index_since(101.0)["vc-2"].state == "free" and reader.reads == 2, "a Stop after the tick reads now"
    assert arbiter.last_index()["vc-2"].state == "busy", "the fresh read is not cached: last_index/last_work stay a pair"


def shells_probe(count, calls):
    def probe():
        calls.append(1)
        return count

    return probe


def test_a_stored_background_the_idle_index_overrules_settles_to_free():
    """A Stop over an unclosed launch stores background; the idle index after it is the authority."""
    calls = []
    assert settled_state("background", entry("free", 100.0), shells_probe(0, calls)) == "free"
    assert settled_state("background", entry("free", 100.0), shells_probe(2, calls)) == "background"
    assert settled_state("free", entry("background", 100.0), shells_probe(0, calls)) == "background"
    assert len(calls) == 2, "the process table is read only while the index says idle"


def test_settled_state_leaves_a_busy_index_a_prompt_and_an_unknown_row_alone():
    calls = []
    assert settled_state("background", entry("busy", 90.0), shells_probe(0, calls)) == "background"
    assert settled_state("background", None, shells_probe(0, calls)) == "background"
    assert settled_state("attention", entry("free", 100.0), shells_probe(0, calls)) == "attention"
    assert settled_state("free", entry("attention", 100.0), shells_probe(0, calls)) == "free"
    assert calls == []
