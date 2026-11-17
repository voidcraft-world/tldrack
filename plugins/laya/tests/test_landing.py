"""landing.py: the landing record's body check, its one writer, its reader and the `/landing` route.

The shape against Zod: tests/test_contract.py. The bridge's body against this module: lab/tests/test_landing_wire.py.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from laya_bridge import landing
from laya_bridge.landing import LandingError, LandingLog, clean_landing, landings_by_decision
from laya_bridge.plugin import LayaPlugin

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
DECISION = "20261109T030000-0123456789ab"
BODY = {
    "decision_id": DECISION,
    "surface": "agent_open_work",
    "class": "held",
    "held": "children",
    "running": {"descendants": 0, "agents": 1, "workflows": 2, "shells": 0},
    "trace": {"ran_tests": True, "edited_files": 3, "opened_pr": False, "ended_on_question": True},
}
OPEN_WORK = {"surface": "agent_open_work", "subject": "vc-1", "question_version": 2, "threshold": 0.7}
NATIVE = {"host": "127.0.0.1:7682", "content-type": "application/json"}
BROWSER = {**NATIVE, "origin": "http://localhost:5173"}


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def plugin(tree, fake_runtime):
    return LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)


def decide_on(plugin, decide, tag=OPEN_WORK):
    status, payload = plugin.handle_post("/decide", {}, decide(ledger=tag))
    assert status == 200 and payload["decision_id"]
    return payload["decision_id"]


# ── the body ─────────────────────────────────────────────────────────────────


def test_a_clean_body_is_kept_as_it_came():
    assert clean_landing(BODY) == BODY
    assert clean_landing({**BODY, "class": None, "held": None, "trace": None})["trace"] is None


@pytest.mark.parametrize(
    "change, field",
    [
        ({"extra": 1}, "landing.extra"),
        ({"Bad Key!": 1}, "landing.<field>"),
        ({"decision_id": "../../etc"}, "landing.decision_id"),
        ({"surface": "paste"}, "landing.surface"),
        ({"class": "shown"}, "landing.class"),
        ({"held": "sleeping"}, "landing.held"),
        ({"held": None}, "landing.held"),
        ({"class": "new"}, "landing.held"),
        ({"class": "repeat", "held": "background"}, "landing.held"),
        ({"running": {"agents": 1}}, "landing.running"),
        ({"running": {**BODY["running"], "agents": -1}}, "landing.running.agents"),
        ({"running": {**BODY["running"], "shells": True}}, "landing.running.shells"),
        ({"running": {**BODY["running"], "workflows": landing.COUNT_MAX + 1}}, "landing.running.workflows"),
        ({"trace": {**BODY["trace"], "ran_tests": 1}}, "landing.trace.ran_tests"),
        ({"trace": {**BODY["trace"], "edited_files": 2.5}}, "landing.trace.edited_files"),
        ({"trace": "yes"}, "landing.trace"),
    ],
)
def test_a_bad_body_names_the_field_and_never_its_value(change, field):
    with pytest.raises(LandingError) as refused:
        clean_landing({**BODY, **change})
    assert str(refused.value).startswith(field)
    assert "etc" not in str(refused.value) and "sleeping" not in str(refused.value)


def test_a_missing_field_is_refused():
    body = dict(BODY)
    del body["trace"]
    with pytest.raises(LandingError, match="^landing.trace: required"):
        clean_landing(body)
    with pytest.raises(LandingError, match="^landing: the body"):
        clean_landing([BODY])


def test_a_retired_or_markless_verdict_may_carry_either_audience():
    for kind in ("retired", None):
        for held in ("background", "children", None):
            clean_landing({**BODY, "class": kind, "held": held})


# ── the writer and the reader ────────────────────────────────────────────────


def test_one_row_per_decision_in_a_private_file(tree):
    log = LandingLog(lambda: tree.base, clock=lambda: NOW)
    assert log.record(clean_landing(BODY)) == ("ok", False)
    assert log.record(clean_landing(BODY)) == ("ok", True), "a repeat writes nothing"
    path = tree.base / "landing" / "agent_open_work.jsonl"
    [row] = [json.loads(line) for line in path.read_text().splitlines()]
    assert row == {"kind": "landing", "v": 1, "at": "2026-11-09T03:00:00Z", **BODY}
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert landings_by_decision(tree.base) == {DECISION: row}


def test_no_laya_folder_means_the_record_is_off(tmp_path):
    log = LandingLog(lambda: tmp_path / "params" / "laya")
    assert log.record(clean_landing(BODY)) == ("off", False)
    assert not (tmp_path / "params").exists()
    assert landings_by_decision(tmp_path / "params" / "laya") == {} and landings_by_decision(None) == {}


def test_a_linked_landing_folder_is_refused(tree, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tree.base / "landing").symlink_to(elsewhere)
    logged = []
    log = LandingLog(lambda: tree.base, log=logged.append)
    assert log.record(clean_landing(BODY)) == ("off", False)
    assert list(elsewhere.iterdir()) == [] and logged == [
        "[laya] landing: landing/ is a link or outside laya/ — refusing to write"
    ]


def test_the_record_rotates_like_the_ledger_and_the_reader_spans_segments(tree):
    clock = {"t": NOW}
    log = LandingLog(lambda: tree.base, clock=lambda: clock["t"], rotate_bytes=300, keep_segments=1)
    ids = [f"20261109T0300{n:02d}-0123456789ab" for n in range(4)]
    for n, decision_id in enumerate(ids):
        clock["t"] = NOW + timedelta(seconds=n)
        assert log.record(clean_landing({**BODY, "decision_id": decision_id})) == ("ok", False)
    folder = tree.base / "landing"
    names = sorted(path.name for path in folder.iterdir())
    assert names[-1] == "agent_open_work.jsonl" and len(names) == 2, "one segment kept beside the active file"
    kept = landings_by_decision(tree.base)
    assert list(kept) == ids[-len(kept) :] and ids[-1] in kept
    assert all(mode(path) == 0o600 for path in folder.iterdir())


def test_a_damaged_line_is_skipped_by_the_reader(tree):
    LandingLog(lambda: tree.base, clock=lambda: NOW).record(clean_landing(BODY))
    path = tree.base / "landing" / "agent_open_work.jsonl"
    with path.open("a") as handle:
        handle.write('{"kind": "landing", "decision_id": "nope"}\nnot json\n')
    assert list(landings_by_decision(tree.base)) == [DECISION]


# ── the route ────────────────────────────────────────────────────────────────


def test_the_rack_records_a_landing_for_a_decision_the_ledger_holds(plugin, tree, decide):
    decision_id = decide_on(plugin, decide)
    status, body = plugin.handle_post("/landing", {}, {**BODY, "decision_id": decision_id})
    assert (status, body) == (200, {"ok": True, "decision_id": decision_id, "duplicate": False})
    assert plugin.handle_post("/landing", {}, {**BODY, "decision_id": decision_id})[1]["duplicate"] is True
    assert list(landings_by_decision(tree.base)) == [decision_id]
    assert "/plugin/laya/landing" in plugin.routes()


def test_a_restarted_plugin_still_finds_the_decision_on_disk(plugin, tree, fake_runtime, decide):
    decision_id = decide_on(plugin, decide)
    fresh = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    assert fresh.handle_post("/landing", {}, {**BODY, "decision_id": decision_id})[0] == 200


def test_an_unknown_or_off_surface_decision_is_409(plugin, decide):
    assert plugin.handle_post("/landing", {}, BODY)[0] == 409
    paste = decide_on(plugin, decide, {"surface": "paste"})
    status, body = plugin.handle_post("/landing", {}, {**BODY, "decision_id": paste})
    assert (status, body["error"]) == (409, "unknown_decision"), "a landing joins an agent_open_work decision only"


def test_a_bad_body_is_400_and_names_the_field(plugin):
    status, body = plugin.handle_post("/landing", {}, {**BODY, "class": "shown"})
    assert status == 400 and body["detail"].startswith("landing.class")


def test_only_a_native_caller_may_post_a_landing(plugin, decide):
    decision_id = decide_on(plugin, decide)
    status, body = plugin.handle_request("POST", "/landing", {}, {**BODY, "decision_id": decision_id}, BROWSER)
    assert (status, body["error"]) == (403, "forbidden")
    status, _body = plugin.handle_request("POST", "/landing", {}, {**BODY, "decision_id": decision_id}, NATIVE)
    assert status == 200


def test_with_the_ledger_off_a_landing_is_503(tmp_path, fake_runtime):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tmp_path / "absent")
    status, body = plugin.handle_post("/landing", {}, BODY)
    assert (status, body["error"]) == (503, "no_ledger")
