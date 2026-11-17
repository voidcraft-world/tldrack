import json
from types import SimpleNamespace

import pytest

from tldrack.session_lineage import clean_lineage, prepare_lineage, parse_lineage, lineage_args
from tldrack.snapshot import TMUX_FIELD_SEP, parse_tmux_panes, merge_snapshot
from tldrack.session_records import SessionRecordStore

PARENT = "00000000-0000-0000-0000-000000000001"
CHILD = "00000000-0000-0000-0000-000000000002"


def test_parent_is_resolved_by_explicit_session_and_preserves_its_identity(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": PARENT}))

    monkeypatch.setattr("tldrack.session_lineage.subprocess.run", run)
    first = prepare_lineage("parent")
    second = prepare_lineage("parent")
    assert first["parent_id"] == second["parent_id"] == PARENT
    assert first["id"] != second["id"]
    assert calls[0][4] == "=parent:"


def test_legacy_parent_is_stamped_before_launch(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("tldrack.session_lineage.subprocess.run", run)
    child = prepare_lineage("parent")
    assert calls[1][1] == "set-option"
    assert json.loads(calls[1][-1])["id"] == child["parent_id"]


def test_invalid_or_missing_parent_refuses(monkeypatch):
    with pytest.raises(ValueError):
        prepare_lineage("bad;name")
    monkeypatch.setattr(
        "tldrack.session_lineage.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="")
    )
    with pytest.raises(ValueError, match="no longer running"):
        prepare_lineage("gone")


def test_stamp_reaches_snapshot_and_keeps_title_intact():
    lineage = {"id": CHILD, "parent_id": PARENT, "parent_name": "parent"}
    raw = TMUX_FIELD_SEP.join([json.dumps(lineage), "child", "0", "1", "1", "/repo", "codex", "title\twith tabs"])
    panes = parse_tmux_panes(raw)
    assert panes["child"]["lineage"] == lineage
    assert panes["child"]["title"] == "title\twith tabs"
    assert merge_snapshot(panes, [])[0]["lineage"] == lineage


def test_records_keep_ancestry_across_rename_and_restart(tmp_path):
    path = tmp_path / "records.json"
    store = SessionRecordStore(path)
    lineage = {"id": CHILD, "parent_id": PARENT, "parent_name": "parent"}
    store.observe_live([{"session": "child", "live": True, "cwd": "/repo", "lineage": lineage}], lambda _: None)
    store.rename("child", "renamed")
    assert SessionRecordStore(path).get("renamed")["lineage"] == lineage
    assert json.loads(lineage_args("renamed", lineage)[-1]) == lineage


def test_bad_ids_and_self_parent_are_discarded():
    assert parse_lineage("invalid") is None
    assert clean_lineage({"id": "bad"}) is None
    assert clean_lineage({"id": PARENT, "parent_id": PARENT}) == {"id": PARENT}
