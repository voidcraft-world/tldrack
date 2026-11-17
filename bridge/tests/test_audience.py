"""`tldrack ledger audience` (tldrack/ledger/audience.py): ⚠️ by landing class × in-process children."""

from __future__ import annotations

import json

import pytest

from tldrack.ledger.audience import LANDING_SOURCE, measure
from tldrack.ledger.side_ledgers import parse_row, side_ledgers
from tldrack.ledger.store import connect
from tldrack.rack_metrics import RackMetrics

from test_laya_links import A, B, C, T0, add_decision, add_file


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "ledger")
    yield connection
    connection.close()


def link(conn, decision_id, conversation, at):
    conn.execute(
        "INSERT INTO laya_links (decision_id, conversation, method, at) VALUES (?, ?, 'id', ?)", (decision_id, conversation, at)
    )


def child(conn, file_id, parent, kind, started, ended):
    add_file(conn, file_id, f"child-{file_id}", started, ended, kind=kind)
    conn.execute("UPDATE sessions SET parent_session_id = ? WHERE file_id = ?", (parent, file_id))


def landing(conn, decision_id, landing_class, held):
    raw = (
        json.dumps(
            {
                "kind": "landing",
                "v": 1,
                "decision_id": decision_id,
                "at": "2026-11-09T03:00:00Z",
                "surface": "agent_open_work",
                "class": landing_class,
                "held": held,
            }
        )
        + "\n"
    ).encode()
    conn.execute(
        "INSERT INTO bridge_rows (source, row_key, ts, session, kind, data) VALUES (?, ?, ?, ?, ?, ?)",
        (LANDING_SOURCE, *parse_row(LANDING_SOURCE, raw)),
    )


def signal(path, conversation, at, name, reason=None):
    """Written by the rack's own recorder, keyed by the conversation as main.py's SignalQueue keys it."""
    metrics = RackMetrics(path, clock=lambda: at)
    assert metrics.signal(
        f"warning_{name}", "vc-1", reason=reason, record_of=lambda _: {"agent_session_id": conversation, "kind": "claude"}
    )


def test_a_free_warning_with_a_running_wave_is_a_miss_and_the_landing_record_wins(conn, tmp_path):
    metrics = tmp_path / "m.sqlite"
    shown = add_decision(conn, T0, "Wave 3 is not started.", conversation=A, n=1)  # wave running, shown as new
    held = add_decision(conn, T0 + 100, "Wave 4 is not started.", conversation=A, n=2)  # held by its own shells
    landed = add_decision(conn, T0 + 200, "Wave 5 is not started.", conversation=B, n=3)  # the landing record says
    unlinked = add_decision(conn, T0 + 300, "Phase C is not started.", n=4)
    for decision_id, conversation, at in ((shown, A, T0), (held, A, T0 + 100), (landed, B, T0 + 200)):
        link(conn, decision_id, conversation, at)
    child(conn, 10, A, "workflow", T0 - 50, T0 + 40)  # running at the first verdict only
    child(conn, 11, B, "subagent", T0 + 150, T0 + 260)
    child(conn, 12, C, "workflow", T0 - 50, T0 + 400)  # another conversation's: never counted
    signal(metrics, A, T0 + 1.2, "new")
    signal(metrics, A, T0 + 101, "held", "background")
    signal(metrics, B, T0 + 201, "new")  # overruled by the landing record below
    landing(conn, landed, "held", "children")
    result = measure(conn, metrics_path=metrics)
    assert (result["warnings"], result["unlinked"]) == (4, 1)
    assert result["classified_by"] == {"landing": 1, "signal": 2}
    assert result["classes"]["new"] == {"workflow": 1, "subagent": 0, "none": 0}
    assert result["classes"]["held:background"] == {"workflow": 0, "subagent": 0, "none": 1}
    assert result["classes"]["held:children"] == {"workflow": 0, "subagent": 1, "none": 0}
    assert result["free_with_children"] == {"n": 1, "of": 1}
    assert result["all_with_children"] == {"n": 2, "of": 3}
    assert unlinked


def test_a_operator_act_near_a_verdict_never_outbids_its_landing_signal(conn, tmp_path):
    """`warning_dismissed` / `warning_snoozed` are acts on a ⚠️, not landing classes: only the four count."""
    metrics = tmp_path / "m.sqlite"
    landed = add_decision(conn, T0, "Wave 3 is not started.", conversation=A, n=1)
    lone = add_decision(conn, T0 + 100, "Wave 4 is not started.", conversation=A, n=2)
    link(conn, landed, A, T0)
    link(conn, lone, A, T0 + 100)
    signal(metrics, A, T0 + 1, "dismissed")  # nearer than its own class
    signal(metrics, A, T0 + 2, "new")
    signal(metrics, A, T0 + 101, "snoozed")  # a verdict whose landing wrote no signal
    result = measure(conn, metrics_path=metrics)
    assert result["classes"]["new"]["none"] == 1
    assert result["classes"]["unknown"]["none"] == 1
    assert result["classified_by"] == {"landing": 0, "signal": 1}


def test_a_verdict_under_its_cut_is_no_warning_and_a_missing_metrics_db_is_unknown(conn, tmp_path):
    raw = add_decision(conn, T0, "Done.", conversation=A, n=1)
    conn.execute("UPDATE bridge_rows SET data = json_set(data, '$.decisions[0].p', 0.2)")
    assert measure(conn)["warnings"] == 0
    conn.execute("UPDATE bridge_rows SET data = json_set(data, '$.decisions[0].p', 0.9)")
    link(conn, raw, A, T0)
    result = measure(conn, metrics_path=tmp_path / "absent.sqlite")
    assert result["classes"]["unknown"]["none"] == 1 and result["classified_by"] == {"landing": 0, "signal": 0}
    assert measure(conn, since=T0 + 1)["warnings"] == 0


def test_the_landing_record_is_a_side_ledger_the_session_ledger_keeps(tmp_path, monkeypatch):
    monkeypatch.setenv("TLDRACK_DATA_DIR", str(tmp_path / "params"))
    laya = tmp_path / "params" / "laya"
    for relative in (
        "ledger/agent_open_work.jsonl",
        "landing/agent_open_work.jsonl",
        "landing/agent_open_work-20261109T030000.jsonl",
    ):
        (laya / relative).parent.mkdir(parents=True, exist_ok=True)
        (laya / relative).write_text("{}\n")
    env = {"TLDRACK_PERMISSIONS_PATH": str(tmp_path / "p.jsonl"), "TLDRACK_ACTIVITY_LOG_PATH": str(tmp_path / "a.jsonl")}
    sources = [(ledger.source, ledger.path.name) for ledger in side_ledgers([], [], env=env)]
    assert ("laya:agent_open_work", "agent_open_work.jsonl") in sources
    assert [s for s in sources if s[0].startswith("laya-landing")] == [
        (LANDING_SOURCE, "agent_open_work-20261109T030000.jsonl"),
        (LANDING_SOURCE, "agent_open_work.jsonl"),
    ]
