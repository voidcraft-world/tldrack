"""attention.py (held / new / repeat / retired), stuck.py, and their telemetry signals — pure parts
first, then the whole path from a Stop hook through `/open-work/snooze` on a test-built bridge."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

from tldrack import open_work
from tldrack.agent_index import IndexEntry
from tldrack.attention import (
    BACKGROUND,
    CHILDREN,
    HELD,
    NEW,
    REPEAT,
    RETIRED,
    OpenLoops,
    attention_row_fields,
    held_reason,
    in_process_children,
    is_held,
    landing_class,
    prompt_origin,
    running_descendants,
)
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.rack_metrics import AVOIDED, INTERRUPTIONS, RackMetrics, SignalQueue
from tldrack.stuck import (
    NO_PROGRESS,
    NO_PROGRESS_SECONDS,
    REPEAT_FAILURE,
    REPEAT_FAILURES,
    StuckDetector,
    repeat_failures,
    stuck_reading,
)

TASK = "<task-notification>\n<task-id>wz1</task-id>\n<status>completed</status>\n</task-notification>"
UUIDS = [f"00000000-0000-4000-8000-{n:012d}" for n in range(8)]


def lineage(n, parent=None):
    out = {"id": UUIDS[n]}
    if parent is not None:
        out["parent_id"] = UUIDS[parent]
    return out


def live(session, state, n, parent=None, alive=True):
    return {"session": session, "state": state, "live": alive, "lineage": lineage(n, parent)}


# ── § 1 held: running descendants ────────────────────────────────────────────


def test_a_parent_is_held_by_every_unfinished_descendant():
    rows = [
        live("NOTES", "free", 0),
        live("wave-1", "busy", 1, 0),
        live("wave-2", "background", 2, 0),
        live("wave-2a", "attention", 3, 2),
        live("wave-3", "free", 4, 0),
    ]
    assert running_descendants(rows) == {"NOTES": 3, "wave-2": 1}


def test_dead_rows_cycles_and_names_never_count():
    rows = [
        live("p", "free", 0),
        live("gone", "busy", 1, 0, alive=False),
        live("a", "busy", 2, 3),
        live("b", "busy", 3, 2),  # a cycle
        {"session": "p-child", "state": "busy", "live": True},
    ]  # no lineage: a name is not ancestry
    assert running_descendants(rows) == {"a": 1, "b": 1}


def test_held_is_background_or_children():
    assert is_held("background", 0) and is_held("free", 2)
    assert not is_held("free", 0) and not is_held("done", 0)


# ── § 1b held: in-process children ───────────────────────────────────────────


def test_in_process_children_are_believed_only_while_the_index_says_busy():
    assert in_process_children(1, 2, "busy") == (1, 2)
    for state in ("free", "background", "attention", None):
        assert in_process_children(1, 2, state) == (0, 0), "a resumed launch with no notification holds nobody"


def test_any_running_child_is_children_and_only_shells_are_background():
    assert held_reason("free", 0, 1) == CHILDREN, "a wave the arbiter has not yet ticked on"
    assert held_reason("background", 0, 2) == CHILDREN and held_reason("background", 3) == CHILDREN
    assert held_reason("background", 0) == BACKGROUND
    assert held_reason("free", 0) is None and held_reason("busy", 0, 0) is None
    assert is_held("free", 0, 1) and not is_held("free", 0, 0)


def test_the_landing_class_keeps_retired_and_names_held():
    assert landing_class(NEW, CHILDREN) == HELD and landing_class(REPEAT, BACKGROUND) == HELD
    assert landing_class(RETIRED, CHILDREN) == RETIRED, "a parked loop draws nothing, held or not"
    assert landing_class(NEW, None) == NEW and landing_class(None, CHILDREN) is None


# ── § 2 the loop: human prompts vs self-woken turns ──────────────────────────


def test_prompt_origin_reads_the_text_like_the_session_ledger():
    assert prompt_origin("fix the rack") == "human"
    assert prompt_origin(TASK) == "task"
    assert prompt_origin("<teammate-message from='x'>hi</teammate-message>") == "peer"
    assert prompt_origin(None) == "human", "an unreadable prompt must never keep a loop open"


def test_a_loop_opens_repeats_parks_and_closes(tmp_path):
    loops = OpenLoops(tmp_path / "a.json", clock=lambda: 100.0)
    assert loops.classify("NOTES", is_open=True, work_done=False) == NEW
    assert loops.note_prompt("NOTES", TASK) is False
    assert loops.classify("NOTES", is_open=True, work_done=False) == REPEAT
    assert loops.classify("NOTES", is_open=False, work_done=False) is None, "the 💬 band leaves the loop"
    assert loops.park("NOTES") is True and loops.park("NOTES") is False
    assert loops.classify("NOTES", is_open=True, work_done=False) == RETIRED
    assert loops.note_prompt("NOTES", "ok, merge them") is True
    assert loops.classify("NOTES", is_open=True, work_done=False) == NEW


def test_a_done_verdict_closes_the_loop(tmp_path):
    loops = OpenLoops(tmp_path / "a.json")
    loops.classify("vc-1", is_open=True, work_done=False)
    assert loops.classify("vc-1", is_open=False, work_done=True) is None
    assert loops.get("vc-1") is None


def test_parked_loops_survive_a_restart_and_follow_a_rename(tmp_path):
    path = tmp_path / "a.json"
    first = OpenLoops(path)
    first.classify("vc-1", is_open=True, work_done=False)
    first.park("vc-1")
    second = OpenLoops(path)
    assert second.parked() == {"vc-1"}
    second.rename("vc-1", "NOTES")
    assert OpenLoops(path).parked() == {"NOTES"}
    assert "Phase" not in path.read_text(), "no text is ever kept"


def test_a_corrupt_entry_is_dropped_alone(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "ok": {"opened_at": 1.0, "parked": True, "last": "retired"},
                    "bad": {"opened_at": "x", "parked": True, "last": "retired"},
                    "bad name!": {"opened_at": 1.0, "parked": True, "last": "new"},
                },
            }
        )
    )
    assert OpenLoops(path).parked() == {"ok"}


def test_row_fields_are_omitted_when_quiet():
    quiet = {"session": "vc-1", "open_work": True}
    assert attention_row_fields(quiet, children={}, parked=set(), classes={"vc-1": NEW}) == {}
    assert attention_row_fields(quiet, children={"vc-1": 2}, parked=set(), classes={}) == {"children_running": 2}
    assert attention_row_fields(quiet, children={}, parked={"vc-1"}, classes={}) == {"open_work_seen": True}
    assert attention_row_fields(quiet, children={}, parked=set(), classes={"vc-1": REPEAT}) == {"open_work_repeat": True}
    no_warning = {"session": "vc-1", "open_work": False}
    assert attention_row_fields(no_warning, children={}, parked={"vc-1"}, classes={"vc-1": REPEAT}) == {}


# ── stuck.py ─────────────────────────────────────────────────────────────────


def bash(key, failed, at=1000.0):
    return {"name": "Bash", "key": key, "at": at, "outcome": "approved", "failed": failed}


def test_repeat_failures_count_one_command_since_it_last_succeeded():
    events = {f"t{i}": bash("npm", True) for i in range(3)}
    events["ok"] = bash("npm", False)
    events.update({f"u{i}": bash("npm", True) for i in range(2)})
    events["other"] = bash("ls", True)
    assert repeat_failures(events) == 2
    events.update({f"v{i}": bash("npm", True) for i in range(2)})
    assert repeat_failures(events) == REPEAT_FAILURES


def test_repeat_failures_ignore_other_tools_rejections_and_earlier_turns():
    events = {
        "e1": {**bash("f", True), "name": "Edit"},
        "d": {**bash("x", False), "outcome": "denied"},
        **{f"old{i}": bash("npm", True, at=10.0) for i in range(4)},
    }
    assert repeat_failures(events, since=500.0) == 0
    assert repeat_failures(events) == 4


def test_stuck_reading_only_on_busy_rows_and_names_the_cause():
    assert stuck_reading("attention", 9, 9999) is None
    assert stuck_reading("busy", REPEAT_FAILURES, 9999) == {"kind": REPEAT_FAILURE, "count": REPEAT_FAILURES}
    assert stuck_reading("busy", 0, NO_PROGRESS_SECONDS) == {"kind": NO_PROGRESS, "seconds": NO_PROGRESS_SECONDS}
    assert stuck_reading("busy", 3, NO_PROGRESS_SECONDS - 1) is None


def test_the_detector_flags_silence_once_and_a_hook_clears_it():
    now = {"t": 0.0}
    flagged = []
    detector = StuckDetector(clock=lambda: now["t"], on_flagged=lambda s, k: flagged.append((s, k)))
    detector.note_turn("vc-1")
    now["t"] = NO_PROGRESS_SECONDS + 1
    assert detector.evaluate("vc-1", "busy", {})["kind"] == NO_PROGRESS
    assert detector.evaluate("vc-1", "busy", {})["kind"] == NO_PROGRESS
    assert flagged == [("vc-1", NO_PROGRESS)], "once per episode, never per poll"
    detector.note_hook("vc-1")
    assert detector.evaluate("vc-1", "busy", {}) is None


def test_the_clock_restarts_after_a_turn_over():
    now = {"t": 0.0}
    detector = StuckDetector(clock=lambda: now["t"])
    assert detector.evaluate("vc-1", "busy", {}) is None  # first seen: its clock starts now
    assert detector.evaluate("vc-1", "free", {}) is None
    now["t"] = NO_PROGRESS_SECONDS * 3
    assert detector.evaluate("vc-1", "busy", {}) is None, "a long idle is not a silent turn"


def test_a_repeat_failure_counts_only_this_turn():
    detector = StuckDetector(clock=lambda: 0.0, wall=lambda: 500.0)
    detector.note_turn("vc-1")
    old = {f"o{i}": bash("npm", True, at=100.0) for i in range(5)}
    assert detector.evaluate("vc-1", "busy", old) is None
    new = {**old, **{f"n{i}": bash("npm", True, at=600.0) for i in range(4)}}
    assert detector.evaluate("vc-1", "busy", new) == {"kind": REPEAT_FAILURE, "count": 4}


# ── telemetry ────────────────────────────────────────────────────────────────


def test_signals_sum_into_interruptions_and_avoided(tmp_path):
    metrics = RackMetrics(tmp_path / "m.sqlite")
    queue = SignalQueue(metrics, worker=False)
    for name in ("warning_new", "ping_sent", "warning_repeat", "warning_held", "turn_end"):
        queue.put(name, "vc-1", "children" if name == "warning_held" else None)
    queue.put("digest_sent", None, "digest", 3)
    queue.put("not_a_signal", "vc-1")
    assert queue.drain() == 6
    summary = metrics.summary()
    assert summary["total"]["interruptions"] == 1 + 1 + 3
    assert summary["total"]["avoided"] == 2
    assert {"signal": "warning_held", "reason": "children", "count": 1} in summary["signals"]
    assert summary["sessions"][0]["session"] == "vc-1" and summary["sessions"][0]["clicks"] == 0
    assert INTERRUPTIONS.isdisjoint(AVOIDED)


def test_a_paused_recorder_writes_no_signal(tmp_path):
    metrics = RackMetrics(tmp_path / "m.sqlite")
    metrics.configure({"enabled": False})
    assert metrics.signal("warning_new", "vc-1") is False
    with sqlite3.connect(tmp_path / "m.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


# ── the whole path on a test-built bridge ────────────────────────────────────


class Laya:
    def __init__(self):
        self.decides = 0
        self.landings = []

    def handle_get(self, subpath, query):
        return 200, {"state": "ready", "models": ["english"]}

    def handle_post(self, subpath, query, body):
        if subpath == "/react":
            return 200, {"ok": True}
        if subpath == "/landing":
            self.landings.append(body)
            return 200, {"ok": True}
        self.decides += 1
        return 200, {
            "answers": {"q": {"noul": 0.95 if "not started" in body["state"] else 0.05}},
            "decision_id": f"20261109T0300{self.decides:02d}-0123456789ab",
        }

    def rack_grade(self, body, headers):
        return 200, {"ok": True}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    monkeypatch.setenv("TLDRACK_NOTIFY_PATH", str(tmp_path / "notify.json"))
    monkeypatch.delenv("TLDRACK_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(open_work, "SETTLE_SECONDS", 0)
    # Never this Mac's real tmux: no sessions, so nothing is anybody's child.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", ""))
    metrics = RackMetrics(tmp_path / "m.sqlite")
    loops = OpenLoops(tmp_path / "attention.json")
    store = StatusStore()
    laya = Laya()
    cls = build_status_handler(
        store=store,
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1},
        control=ShutdownControl("status-only"),
        plugins={"laya": laya},
        rack_metrics=metrics,
        loops=loops,
    )
    transcript = claude_root / "session.jsonl"
    transcript.write_text("")

    def post(route, event=None, payload=None):
        h = object.__new__(cls)
        raw = json.dumps(payload or {}).encode()
        h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        h.path = f"{route}?session=vc-1" + (f"&event={event}" if event else "")
        sent = {}
        h._send_json = lambda status, body: sent.update(status=status, body=body)
        getattr(h, cls.POST_ROUTES[route])()
        return sent

    count = {"n": 0}

    def turn(text, prompt="go", before=()):
        """One whole turn: the prompt, the closing message (after any `before` entries), the Stop, and
        Laya's verdict landing."""
        count["n"] += 1
        post("/hook", "UserPromptSubmit", {"transcript_path": str(transcript), "prompt": prompt})
        with transcript.open("a") as handle:
            handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            for entry in before:
                handle.write(json.dumps(entry) + "\n")
            handle.write(
                json.dumps(
                    {"type": "assistant", "message": {"id": f"m{count['n']}", "content": [{"type": "text", "text": text}]}}
                )
                + "\n"
            )
        post("/hook", "Stop", {"transcript_path": str(transcript)})
        want = "not started" in text
        deadline = time.monotonic() + 5
        while store.get("vc-1")["open_work"] is not want and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # the verdict lands, THEN on_open_work classifies it, on the judge's thread
        return store.get("vc-1")

    def signals():
        cls.signal_queue.drain()
        return [(s["signal"], s["reason"], s["count"]) for s in metrics.summary()["signals"]]

    return SimpleNamespace(post=post, turn=turn, loops=loops, store=store, signals=signals, cls=cls, laya=laya)


def test_self_woken_turns_repeat_and_a_human_prompt_makes_news(bridge):
    bridge.turn("Wave 2 is not started.")
    assert bridge.loops.last_class() == {"vc-1": NEW}
    bridge.turn("Wave 3 is not started.", prompt=TASK)
    assert bridge.loops.last_class() == {"vc-1": REPEAT}
    bridge.turn("Wave 4 is not started.", prompt="what now?")
    assert bridge.loops.last_class() == {"vc-1": NEW}
    assert ("warning_new", None, 2) in bridge.signals()
    assert ("warning_repeat", None, 1) in bridge.signals()
    assert ("turn_end", None, 3) in bridge.signals()


def test_seen_not_now_parks_the_loop_until_the_operator_speaks(bridge):
    assert bridge.post("/open-work/snooze", payload={"session": "vc-1"})["body"]["snoozed"] is False, (
        "nothing to park without a ⚠️"
    )
    bridge.turn("Phase C is not started.")
    assert bridge.post("/open-work/snooze", payload={"session": "vc-1"})["body"]["snoozed"] is True
    bridge.turn("Phase C is still not started.", prompt=TASK)
    assert bridge.loops.last_class()["vc-1"] == RETIRED
    assert bridge.loops.parked() == {"vc-1"}
    bridge.turn("Phase C is not started.", prompt="status?")
    assert bridge.loops.parked() == set()
    assert ("warning_snoozed", None, 1) in bridge.signals()
    assert ("warning_retired", None, 1) in bridge.signals()
    assert bridge.post("/open-work/snooze", payload={"session": "bad name!"})["status"] == 400


def test_dismiss_is_the_grade_and_closes_the_loop(bridge):
    bridge.turn("Phase C is not started.")
    assert bridge.post("/open-work/dismiss", payload={"session": "vc-1"})["body"]["cleared"] is True
    assert bridge.loops.get("vc-1") is None
    assert ("warning_dismissed", None, 1) in bridge.signals()


def test_still_open_is_counted_and_leaves_the_loop_open(bridge):
    bridge.turn("Phase C is not started.")
    bridge.post("/open-work/confirm", payload={"session": "vc-1"})
    assert bridge.loops.get("vc-1") is not None, "✓ says the work is open: the loop is not closed"
    assert ("warning_confirmed", None, 1) in bridge.signals()


def test_attention_is_counted_as_a_wait_once(bridge):
    bridge.post("/hook", "PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    bridge.post("/hook", "Notification", {"message": "Claude needs your permission to use Bash"})
    assert ("wait_started", None, 1) in bridge.signals()


# ── the landing record ──────────────────────────────────────────────────────

WORKFLOW_LAUNCH = {
    "type": "user",
    "toolUseResult": {
        "status": "async_launched",
        "taskId": "w0aaaaaa1",
        "taskType": "local_workflow",
        "runId": "wf_00000000-aaa",
    },
}
QUIET = {"descendants": 0, "agents": 0, "workflows": 0, "shells": 0}


class IndexSays:
    """Claude's live index for vc-1, as the arbiter's reader: each read takes the next state, the last
    one repeating. `status_at` is the read itself, so the index never holds long enough to correct."""

    def __init__(self, states):
        self.states = list(states)

    def read(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {
            "vc-1": IndexEntry(
                session="vc-1",
                state=state,
                status_at=time.time(),
                waiting_for=None,
                agent_session_id=None,
                pid=4242,
                proc_start="Mon Nov  9 03:00:00 2026",
            )
        }

    def still_running(self, entry):
        return True


def index_says(bridge, monkeypatch, *states):
    """The arbiter's tick BEFORE the turn reads the first state (what the Stop's `background_now` sees);
    every read after it, the landing's own, the next."""
    monkeypatch.setattr(bridge.cls.arbiter, "_reader", IndexSays(states))
    bridge.cls.arbiter.tick()


def test_every_landed_verdict_records_its_class_audience_and_trace(bridge):
    bridge.turn(
        "Wave 2 is not started.",
        before=[
            {
                "type": "assistant",
                "message": {
                    "id": "t1",
                    "content": [
                        {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/repo/a.py"}},
                        {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pytest -q tests/test_a.py"}},
                    ],
                },
            }
        ],
    )
    bridge.turn("All done, nothing left.")
    first, second = bridge.laya.landings
    assert first == {
        "decision_id": "20261109T030001-0123456789ab",
        "surface": "agent_open_work",
        "class": NEW,
        "held": None,
        "running": QUIET,
        "trace": {"ran_tests": True, "edited_files": 1, "opened_pr": False, "ended_on_question": False},
    }
    assert (second["class"], second["held"]) == (None, None), "a verdict that drew no ⚠️ still lands: recall needs it"
    assert second["trace"]["edited_files"] == 0, "the trace is the turn's own"


def test_a_running_wave_holds_its_parent_as_children_from_the_first_landing(bridge, monkeypatch):
    """The miss this guards: a Workflow launch named no agentId, so the Stop stored `free` and the
    ⚠️ landed as news before the arbiter's tick. Now the Stop itself stores `background`."""
    index_says(bridge, monkeypatch, "busy")
    row = bridge.turn("Wave 3 is not started.", before=[WORKFLOW_LAUNCH])
    assert row["state"] == "background"
    [landing] = bridge.laya.landings
    assert (landing["class"], landing["held"], landing["running"]["workflows"]) == (HELD, CHILDREN, 1)
    assert ("warning_held", "children", 1) in bridge.signals()


def test_an_idle_index_overrules_a_launch_that_never_notified(bridge, monkeypatch):
    """A resumed conversation copies the launch without its notification: the index is the authority."""
    index_says(bridge, monkeypatch, "free")
    bridge.turn("Wave 3 is not started.", before=[WORKFLOW_LAUNCH])
    [landing] = bridge.laya.landings
    assert (landing["class"], landing["held"], landing["running"]["workflows"]) == (NEW, None, 0)
    assert ("warning_new", None, 1) in bridge.signals()


def test_a_landing_reads_the_index_after_its_stop_not_the_tick_that_watched_the_turn(bridge, monkeypatch):
    """The real timing: the last tick saw the turn busy, so the Stop stored `background` over a resumed
    launch; by the time the verdict lands the index says idle, and the ⚠️ the operator sees is news."""
    index_says(bridge, monkeypatch, "busy", "free")
    row = bridge.turn("Wave 3 is not started.", before=[WORKFLOW_LAUNCH])
    assert row["state"] == "background", "the Stop's last-tick read still said busy"
    [landing] = bridge.laya.landings
    assert (landing["class"], landing["held"], landing["running"]) == (NEW, None, QUIET)
    assert ("warning_new", None, 1) in bridge.signals()
    assert not any(name == "warning_held" for name, _reason, _n in bridge.signals())


def test_a_verdict_without_a_decision_id_posts_no_landing(bridge, monkeypatch):
    original = bridge.laya.handle_post

    def no_ledger(subpath, query, body):
        status, payload = original(subpath, query, body)
        payload.pop("decision_id", None)
        return status, payload

    monkeypatch.setattr(bridge.laya, "handle_post", no_ledger)
    bridge.turn("Wave 2 is not started.")
    assert bridge.laya.landings == []
