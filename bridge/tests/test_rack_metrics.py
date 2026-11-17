import io
import json
import uuid
from datetime import datetime

import pytest

from tldrack.rack_metrics import RackMetrics, metrics_response, validate_event


@pytest.fixture
def metrics(tmp_path):
    now = datetime(2026, 11, 4, 12).timestamp()
    return RackMetrics(tmp_path / "rack-metrics.sqlite", clock=lambda: now)


def event(metrics, **patch):
    return (
        dict(
            id=str(uuid.uuid4()),
            client=str(uuid.uuid4()),
            at=round(metrics.clock() * 1000),
            kind="select",
            session="alpha",
            state="busy",
            mark="none",
            action="select",
            source="window",
            reselect=False,
            duration_ms=0,
            fleet_size=4,
            context_left=60,
            model="model-a",
            profile="primary",
        )
        | patch
    )


def test_retry_is_idempotent_and_history_survives_reopen(metrics):
    row = event(metrics)
    assert metrics.record(row)["recorded"]
    assert not metrics.record(row)["recorded"]
    again = RackMetrics(metrics.path, clock=metrics.clock)
    summary = again.summary("2026-11-04", "2026-11-04")
    assert summary["total"]["clicks"] == 1
    assert summary["total"]["clicks_per_hour"] is None
    assert metrics.path.stat().st_mode & 0o777 == 0o600


def test_revisits_follow_conversation_across_names_and_dates(metrics):
    now = round(metrics.clock() * 1000)
    identity = lambda _: {"kind": "claude", "agent_session_id": "conversation-a"}
    metrics.record(event(metrics, at=now - 86_000_000), identity)
    metrics.record(event(metrics, session="renamed", at=now - 2000, state="attention", mark="open"), identity)
    metrics.record(event(metrics, session="renamed", at=now - 1000, reselect=True), identity)
    summary = metrics.summary("2026-11-04", "2026-11-04")
    assert summary["total"]["clicks"] == 2
    assert summary["total"]["sessions"] == 1
    assert summary["total"]["revisits"] == 1
    assert summary["total"]["reselects"] == 1
    assert summary["total"]["attention_clicks"] == 1
    assert summary["total"]["warning_clicks"] == 1
    assert summary["total"]["identified_clicks"] == 2
    assert summary["sessions"][0]["session"] == "renamed"


def test_same_name_new_conversation_is_not_a_revisit(metrics):
    at = round(metrics.clock() * 1000)
    for i in range(2):
        metrics.record(
            event(metrics, at=at - 1000 + i), lambda _, i=i: {"kind": "claude", "agent_session_id": f"conversation-{i}"}
        )
    assert metrics.summary()["total"]["sessions"] == 2
    assert metrics.summary()["total"]["revisits"] == 0


def test_overlapping_exposure_unions_windows_and_splits_midnight(metrics):
    midnight = round(datetime(2026, 11, 4).timestamp() * 1000)
    # Nested and overlapping intervals, deliberately inserted in reverse order.
    for end, duration in [(midnight + 10_000, 20_000), (midnight + 5000, 5000), (midnight, 15_000)]:
        metrics.record(event(metrics, kind="exposure", action="exposure", at=end, duration_ms=duration))
    report = metrics.summary("2026-11-03", "2026-11-04")
    assert report["total"]["visible_ms"] == 25_000
    assert [d["visible_ms"] for d in report["days"]] == [15_000, 10_000]
    assert metrics.summary("2026-11-03", "2026-11-03")["total"]["visible_ms"] == 15_000
    assert report["total"]["clicks"] == 0


def test_pause_markers_and_action_requests(metrics):
    metrics.configure({"label": "Qwen explanations enabled"})
    metrics.configure({"enabled": False})
    assert not metrics.record(event(metrics))["recorded"]
    metrics.configure({"enabled": True})
    metrics.record(event(metrics, kind="action", action="kill"))
    report = metrics.summary()
    assert len(report["markers"]) == 3
    assert report["actions"] == [{"action": "kill", "count": 1}]
    assert report["total"]["clicks"] == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"duration_ms": float("nan")},
        {"at": 0},
        {"fleet_size": True},
        {"session": "../private"},
        {"kind": ["select"]},
        {"mark": "invented"},
        {"prompt": "must never be stored"},
        {"kind": "select", "duration_ms": 1000},
        {"id": "not-an-id"},
        {"session": None},
    ],
)
def test_boundary_rejects_bad_or_sensitive_input(metrics, patch):
    with pytest.raises(ValueError):
        validate_event(event(metrics, **patch), round(metrics.clock() * 1000))


def test_range_validation_and_zero_filled_days(metrics):
    report = metrics.summary("2026-11-01", "2026-11-04")
    assert len(report["days"]) == 4
    assert report["started_at"] is None
    for start, end in [("bad", None), ("2026-01-01", "2026-11-04"), ("2026-11-04", "2026-11-03")]:
        assert metrics_response(metrics, "summary", query={"from": [start], "to": [end]})[0] == 400


def test_storage_errors_are_reported_without_throwing(tmp_path):
    path = tmp_path / "bad.sqlite"
    path.write_text("not sqlite")
    assert metrics_response(RackMetrics(path), "summary")[0] == 503
    assert metrics_response(None, "summary")[0] == 503


def test_routes_use_real_body_validation_and_origin_guard(metrics, tmp_path):
    from tldrack.main import build_status_handler, ShutdownControl, StatusStore
    from tldrack.icons import SessionIconStore
    from tldrack.models import SessionModelStore

    cls = build_status_handler(
        StatusStore(),
        SessionIconStore(tmp_path / "icons.json"),
        SessionModelStore(tmp_path / "models.json"),
        "test",
        {},
        ShutdownControl("status-only"),
        rack_metrics=metrics,
    )
    assert cls.GET_ROUTES["/rack-metrics"] == "_handle_rack_metrics"
    h = object.__new__(cls)
    answers = []
    h._send_json = lambda status, body: answers.append((status, body))
    raw = json.dumps(event(metrics)).encode()
    h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_rack_metric_event()
    assert answers[-1][0] == 200
    h.headers = {"Origin": "https://untrusted.example", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_rack_metric_event()
    assert answers[-1][0] == 403
    assert metrics.summary()["total"]["clicks"] == 1
