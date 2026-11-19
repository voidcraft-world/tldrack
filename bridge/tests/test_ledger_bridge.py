"""The session ledger inside the bridge: the persisted activity log, the notifier's push record,
the ingest timer and the two routes (activity_log.py, ledger/scheduler.py, main.py)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from tldrack import activity_log as activity_mod
from tldrack.activity_log import ActivityLog, push_row, transition_row
from tldrack.icons import SessionIconStore
from tldrack.ledger.scheduler import LedgerScheduler, ingest_argv
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.notify import NotifyConfigStore, NotifyDaemon


def test_transition_rows_are_pointers_only():
    row = transition_row(
        {
            "ts": 1.0,
            "session": "vc-1",
            "from_state": "busy",
            "to_state": "attention",
            "event": "PermissionRequest",
            "activity": {"zone": "codebase", "tool": "Bash", "detail": "rm -rf secret-dir"},
        }
    )
    assert row == {
        "kind": "transition",
        "ts": 1.0,
        "session": "vc-1",
        "from": "busy",
        "to": "attention",
        "event": "PermissionRequest",
        "activity": {"zone": "codebase", "tool": "Bash"},
    }
    assert "secret-dir" not in json.dumps(row)


def test_activity_log_appends_private_and_rotates(tmp_path: Path, monkeypatch):
    log = ActivityLog(tmp_path / "rack-activity.jsonl")
    log.append(push_row("agents", ("vc-1", "vc-2"), "2 agents waiting", True, now=5.0))
    assert json.loads(log.path.read_text())["sessions"] == ["vc-1", "vc-2"]
    assert oct(log.path.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(activity_mod, "ROTATE_BYTES", 10)
    monkeypatch.setattr(activity_mod, "KEEP_SEGMENTS", 2)
    for i in range(4):
        log.append({"kind": "transition", "ts": i})
    segments = sorted(tmp_path.glob("rack-activity-*.jsonl"))
    assert 1 <= len(segments) <= 2 and log.path.exists()


class FakeSink:
    name = "fake"

    def __init__(self) -> None:
        self.sent = []

    def publish(self, **kwargs):
        self.sent.append(kwargs)
        return True


def test_notifier_records_every_push(tmp_path: Path):
    pushes = []
    config_path = tmp_path / "notify.json"
    config_path.write_text(json.dumps({"topic": "x" * 30, "enabled": True}))
    sink = FakeSink()
    daemon = NotifyDaemon(
        config_store=NotifyConfigStore(config_path),
        read_sessions=list,
        read_presence_count=lambda: 0,
        sink_factory=lambda _c: sink,
        on_push=lambda *args: pushes.append(args),
    )
    assert daemon.send_test() is True
    assert daemon.send_event("Batch done", "3 runs") == (True, "sent")
    assert pushes == [("test", (), "tldrack test", True), ("event", (), "Batch done", True)]


def test_scheduler_runs_the_child_and_reports(tmp_path: Path):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ledger · 3/9 files changed\n", stderr="")

    off = LedgerScheduler(None, run=run)
    assert off.start() is False and off.request() is False
    sched = LedgerScheduler(tmp_path, run=run)
    result = sched.run_once()
    assert calls[0][:3] == ["nice", "-n", "10"] and calls[0][-2:] == ["ingest", "--quiet"]
    assert result["exit"] == 0 and result["line"] == ["ledger · 3/9 files changed"] and result["error"] is None
    assert ingest_argv("py")[3:] == ["py", "-m", "tldrack.ledger.cli", "ingest", "--quiet"]


def handler(tmp_path: Path, ledger: LedgerScheduler | None):
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        ledger=ledger,
        activity_log=ActivityLog(tmp_path / "rack-activity.jsonl"),
    )
    h = object.__new__(cls)
    sent = {}
    h._send_json = lambda status, body: sent.update(status=status, body=body)
    h.headers = {"Content-Length": "2"}
    h.rfile = io.BytesIO(b"{}")
    return h, sent


def test_ledger_routes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TLDRACK_LEDGER_DIR", str(tmp_path / "agent-ledger"))
    h, sent = handler(tmp_path, None)
    h.path = "/ledger/status"
    h._handle_ledger_status()
    assert sent["status"] == 200 and sent["body"]["enabled"] is True and sent["body"]["ingested"] is False
    h.path = "/ledger/ingest"
    h._handle_ledger_ingest()
    assert sent["status"] == 503

    sched = LedgerScheduler(tmp_path, first_delay=3600, run=lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
    sched.start()
    try:
        h, sent = handler(tmp_path, sched)
        h.path = "/ledger/ingest"
        h._handle_ledger_ingest()
        assert sent["status"] == 202 and sent["body"]["queued"] is True
    finally:
        sched.stop()
