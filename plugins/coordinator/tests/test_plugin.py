"""Route tests: off until the operator turns it on, the CoordinatorState wire
shape, the non-blocking audit, and the `/live` index.

These shapes cross every boundary in the feature — plugin → bridge → page →
pop-out window — so they are pinned here rather than left to integration luck.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from fakes import FakeContext

from coordinator import plugin as plugin_module
from coordinator.plugin import CoordinatorPlugin

STATE_KEYS = {"status", "scanned_at", "scanned", "ranked", "ranked_by", "dismissed", "error", "findings"}


def finding(key: str, kind: str = "open-offer", severity: int = 3) -> dict:
    return {
        "key": key,
        "kind": kind,
        "severity": severity,
        "headline": "h",
        "label": "l",
        "last_active": "2026-08-02T10:00:00Z",
        "age_hours": 2.0,
        "work_class": None,
    }


@pytest.fixture()
def coordinator(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_COORDINATOR_PATH", str(tmp_path / "store.json"))
    return CoordinatorPlugin(FakeContext())


def settle(coordinator, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _status, state = coordinator.handle_get("/state", {})
        if state["status"] == "idle":
            return state
        time.sleep(0.01)
    raise AssertionError("the audit thread never settled")


def stub_scan(monkeypatch, findings: list[dict], **extra):
    calls = []

    def fake_scan_all(**kwargs):
        calls.append(kwargs)
        scan = {"scanned_at": "2026-08-02T12:00:00Z", "scanned": 58, "ranked": False, "error": None, "findings": findings}
        scan.update(extra)
        return scan

    monkeypatch.setattr(plugin_module, "scan_all", fake_scan_all)
    monkeypatch.setattr(plugin_module, "_rank_scan", lambda scan, *_: dict(scan, ranked=True))
    return calls


# ── off until the operator turns it on ─────────────────────────────────────


class BareHost:
    """A host that predates the settings seam: `status_port` and `log`, nothing else."""

    status_port = 7682

    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


@pytest.fixture()
def nothing_may_run(monkeypatch):
    """Every way the plugin could touch the machine, wired to fail the test."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an off plugin touched the machine")

    for name in ("CoordinatorStore", "scan_all", "read_live_sessions_all", "resolve_claude_bin", "tmux_found"):
        monkeypatch.setattr(plugin_module, name, forbidden)
    monkeypatch.setattr(plugin_module.threading, "Thread", forbidden)


@pytest.mark.parametrize("host", [BareHost(), FakeContext(settings={})], ids=["no seam", "no settings"])
def test_off_by_default_builds_nothing_and_answers_like_an_absent_plugin(host, nothing_may_run):
    coordinator = CoordinatorPlugin(host)
    assert coordinator.enabled is False
    assert host.lines == ["  coordinator: off (set plugins.coordinator.enabled in settings.json)"]
    # No routes on /snapshot, and every request is the 404 a bridge without the
    # plugin answers — what every client already reads as "no coordinator here".
    assert coordinator.routes() == []
    for subpath in ("/state", "/live"):
        status, body = coordinator.handle_get(subpath, {})
        assert (status, body["error"], body["enabled"]) == (404, "off", False)
    for subpath in ("/audit", "/dismiss", "/restore", "/resume"):
        status, body = coordinator.handle_post(subpath, {}, {"key": "abcdef01:open-offer"})
        assert (status, body["error"]) == (404, "off")
    assert coordinator.busy_reasons() == []


def raising_reader(_name):
    raise RuntimeError("a broken seam")


@pytest.mark.parametrize(
    "settings, warns",
    [
        ({"enabled": False}, False),
        ({"enabled": "true"}, True),
        ({"enabled": 1}, True),
        ({"enabled": None}, True),
        (["enabled"], False),  # not an object at all: every setting at its default
    ],
)
def test_only_a_json_true_turns_it_on(settings, warns, nothing_may_run):
    host = FakeContext(settings=settings)
    coordinator = CoordinatorPlugin(host)
    assert coordinator.enabled is False and coordinator.routes() == []
    assert any("must be true or false" in line for line in host.lines) is warns
    assert host.lines[-1].endswith("off (set plugins.coordinator.enabled in settings.json)")


def test_a_seam_that_raises_reads_as_off(nothing_may_run):
    host = FakeContext()
    host.plugin_settings = raising_reader
    assert CoordinatorPlugin(host).enabled is False


def test_enabled_mounts_every_route_and_logs_no_off_line(coordinator):
    assert coordinator.enabled is True
    assert len(coordinator.routes()) == 6
    assert not any("off (" in line for line in coordinator._context.lines)


def test_routes_are_the_six_full_paths(coordinator):
    assert coordinator.routes() == [
        "/plugin/coordinator/state",
        "/plugin/coordinator/audit",
        "/plugin/coordinator/dismiss",
        "/plugin/coordinator/restore",
        "/plugin/coordinator/resume",
        "/plugin/coordinator/live",
    ]


def test_cold_state_is_a_valid_empty_view(coordinator):
    status, state = coordinator.handle_get("/state", {})
    assert status == 200
    assert set(state) == STATE_KEYS
    assert state == {
        "status": "idle",
        "scanned_at": None,
        "scanned": 0,
        "ranked": False,
        "ranked_by": None,
        "dismissed": 0,
        "error": None,
        "findings": [],
    }


def test_audit_returns_scanning_immediately(coordinator, monkeypatch):
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])
    status, state = coordinator.handle_post("/audit", {}, {})
    assert status == 200
    # The handler runs on a bridge request thread; it must never wait for the scan.
    assert state["status"] == "scanning"
    assert settle(coordinator)["findings"][0]["key"] == "aaaaaaaa:open-offer"


def test_deep_is_read_from_the_body_and_the_query(coordinator, monkeypatch):
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])
    coordinator.handle_post("/audit", {}, {"deep": True})
    assert settle(coordinator)["ranked"] is True

    stub_scan(monkeypatch, [finding("bbbbbbbb:open-offer")])
    coordinator.handle_post("/audit", {}, {})
    assert settle(coordinator)["ranked"] is False

    stub_scan(monkeypatch, [finding("cccccccc:open-offer")])
    coordinator.handle_post("/audit", {"deep": ["1"]}, None)
    assert settle(coordinator)["ranked"] is True


def test_audit_forwards_the_tuning_knobs(coordinator, monkeypatch):
    calls = stub_scan(monkeypatch, [])
    coordinator.handle_post("/audit", {}, {"scan_limit": 5, "max_age_days": 3, "session_id": "abc-123"})
    settle(coordinator)
    assert calls[0]["scan_limit"] == 5
    assert calls[0]["max_age_days"] == 3
    assert calls[0]["exclude_session_ids"] == {"abc-123"}


def test_junk_knobs_fall_back_to_the_defaults(coordinator, monkeypatch):
    calls = stub_scan(monkeypatch, [])
    coordinator.handle_post("/audit", {}, {"scan_limit": "lots", "max_age_days": -4})
    settle(coordinator)
    assert calls[0]["scan_limit"] == plugin_module.DEFAULT_SCAN_LIMIT
    assert calls[0]["max_age_days"] == plugin_module.DEFAULT_MAX_AGE_DAYS


def test_a_crashing_scan_reports_an_error_instead_of_wedging(coordinator, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(plugin_module, "scan_all", boom)
    coordinator.handle_post("/audit", {}, {})
    state = settle(coordinator)
    assert "disk went away" in state["error"]
    # And the plugin is still usable afterwards.
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])
    coordinator.handle_post("/audit", {}, {})
    assert len(settle(coordinator)["findings"]) == 1


def test_dismiss_filters_the_view_without_a_rescan(coordinator, monkeypatch):
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer"), finding("bbbbbbbb:open-offer")])
    coordinator.handle_post("/audit", {}, {})
    settle(coordinator)

    status, state = coordinator.handle_post("/dismiss", {}, {"key": "aaaaaaaa:open-offer"})
    assert status == 200
    assert state["dismissed"] == 1
    assert [f["key"] for f in state["findings"]] == ["bbbbbbbb:open-offer"]

    status, state = coordinator.handle_post("/restore", {}, {})
    assert state["dismissed"] == 0
    assert len(state["findings"]) == 2


def test_a_dismissal_survives_the_next_scan(coordinator, monkeypatch):
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])
    coordinator.handle_post("/audit", {}, {})
    settle(coordinator)
    coordinator.handle_post("/dismiss", {}, {"key": "aaaaaaaa:open-offer"})

    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer"), finding("bbbbbbbb:open-offer")])
    coordinator.handle_post("/audit", {}, {})
    state = settle(coordinator)
    assert [f["key"] for f in state["findings"]] == ["bbbbbbbb:open-offer"]
    assert state["dismissed"] == 1


@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": "nope"}, {"key": 7}, None])
def test_a_malformed_dismiss_key_is_a_400(coordinator, body):
    status, payload = coordinator.handle_post("/dismiss", {}, body)
    assert status == 400
    assert "error" in payload


def test_unknown_routes(coordinator):
    assert coordinator.handle_get("/nope", {})[0] == 404
    assert coordinator.handle_post("/nope", {}, {})[0] == 404


def test_a_thread_that_will_not_start_does_not_wedge_the_plugin(coordinator, monkeypatch):
    # `_scanning` is set before the start so a second POST cannot race in a
    # second worker — which means a start that raises (thread/FD exhaustion)
    # used to leave it True for the life of the bridge: `/state` reported
    # `scanning` forever and every later `/audit` took the early return.
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])

    def refuse_to_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(plugin_module.threading.Thread, "start", refuse_to_start)
    status, payload = coordinator.handle_post("/audit", {}, {})
    assert status == 500
    assert "error" in payload
    assert coordinator.handle_get("/state", {})[1]["status"] == "idle"

    # And the very next audit runs normally.
    monkeypatch.undo()
    stub_scan(monkeypatch, [finding("aaaaaaaa:open-offer")])
    assert coordinator.handle_post("/audit", {}, {})[0] == 200
    assert len(settle(coordinator)["findings"]) == 1


# ── GET /live ────────────────────────────────────────────────────────────────

LIVE_KEYS = {"agents", "count"}
LIVE_ROW_KEYS = {"pid", "session_id", "cwd", "status", "name", "kind", "updated_at"}


def reaped_pid() -> int:
    """A pid that is guaranteed dead: a child this process has already waited on."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait() == 0
    return child.pid


def write_index_entry(sessions: os.PathLike[str], pid: int, **fields) -> None:
    body = {
        "pid": pid,
        "sessionId": f"{pid:08x}-0000-4000-8000-000000000000",
        "status": "idle",
        "updatedAt": 1900000000123,
        **fields,
    }
    (sessions / f"{pid}.json").write_text(json.dumps(body))


@pytest.fixture()
def sessions_index(tmp_path, monkeypatch):
    """`sessions_dir()` resolves under CLAUDE_CONFIG_DIR — the same env var the
    `claude` CLI honours — so pointing that at tmp fences the read completely."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return sessions


def test_live_is_the_pid_checked_index_with_collapsed_paths(coordinator, sessions_index, tmp_path):
    me = os.getpid()
    dead = reaped_pid()
    write_index_entry(
        sessions_index, me, cwd=str(tmp_path / "code" / "repo"), status="busy", name="fix-login", kind="interactive"
    )
    write_index_entry(sessions_index, dead, cwd=str(tmp_path / "gone"))
    (sessions_index / "half-written.json").write_text('{"pid": ')  # mid-rewrite, skipped
    (sessions_index / f"{me}.deadbeef.key").write_text("not json")  # the sibling key files

    status, body = coordinator.handle_get("/live", {})
    assert status == 200
    assert set(body) == LIVE_KEYS
    assert body["count"] == 1 == len(body["agents"])
    row = body["agents"][0]
    assert set(row) == LIVE_ROW_KEYS
    assert row == {
        "pid": me,
        "session_id": f"{me:08x}-0000-4000-8000-000000000000",
        "cwd": "~/code/repo",
        "status": "busy",
        "name": "fix-login",
        "kind": "interactive",
        "updated_at": 1900000000,
    }
    # The whole point of the collapse: the home directory never crosses the wire.
    assert str(tmp_path) not in json.dumps(body)


def test_live_is_empty_not_an_error_without_an_index(coordinator, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert coordinator.handle_get("/live", {}) == (200, {"agents": [], "count": 0})


def test_live_reads_fresh_on_every_get(coordinator, sessions_index):
    # No cache, by design (`_live`): an agent that appears between two polls
    # is on the very next answer.
    assert coordinator.handle_get("/live", {})[1]["count"] == 0
    write_index_entry(sessions_index, os.getpid())
    assert coordinator.handle_get("/live", {})[1]["count"] == 1


def test_busy_reasons_reports_an_audit_only_while_it_runs(coordinator):
    # The host's freshness seam (bridge/tldrack/freshness.py):
    # a restart mid-audit kills the worker thread.
    assert coordinator.busy_reasons() == []
    coordinator._scanning = True
    assert coordinator.busy_reasons() == ["coordinator audit running"]
