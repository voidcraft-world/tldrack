"""`ranked_by` survives a bridge restart: laya-lab trains only on Claude-ranked scans, so losing the
field on reload would make every stored scan unusable as labels."""

from coordinator.store import parse_store_file, serialize_store_file


def test_ranked_by_round_trips_and_rejects_unknown_values():
    scan = {"scanned_at": None, "scanned": 1, "ranked": True, "ranked_by": "claude", "error": None, "findings": []}
    assert parse_store_file(serialize_store_file(scan, set()))[0]["ranked_by"] == "claude"
    assert parse_store_file(serialize_store_file({**scan, "ranked_by": "gpt"}, set()))[0]["ranked_by"] is None
    assert parse_store_file('{"version": 1, "scan": {}}')[0]["ranked_by"] is None
