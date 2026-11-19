"""routing.json (routing.py): order, the quota floor, waiting instead of a downgrade, tolerant parse."""

from tldrack.routing import parse_routing, quota_left, resolve

SUMMARIES = [
    {
        "id": "a",
        "kind": "claude",
        "cli_found": True,
        "usage": {"five_hour": {"used_percentage": 95}, "seven_day": {"used_percentage": 10}},
    },
    {"id": "b", "kind": "claude", "cli_found": True, "usage": None},
    {"id": "codex", "kind": "codex", "cli_found": True, "usage": {"seven_day": {"used_percentage": 50}}},
]


def test_defaults_follow_the_launch_ranking_within_the_lane():
    assert resolve({}, "ladder_step", "claude", SUMMARIES, ["a", "codex", "b"])[0] == "a"
    assert resolve({}, "ladder_step", "codex", SUMMARIES, ["a", "b"])[0] == "codex"


def test_the_quota_floor_skips_then_waits_never_downgrades():
    table = parse_routing('{"ladder_step": {"claude": {"profiles": ["a", "b"], "min_quota_pct": 15}}}')
    assert resolve(table, "ladder_step", "claude", SUMMARIES)[0] == "b", "a has 5% left in its 5-hour window"
    spent = [{**s, "usage": {"five_hour": {"used_percentage": 99}}} if s["id"] == "b" else s for s in SUMMARIES]
    profile, why = resolve(table, "ladder_step", "claude", spent)
    assert profile is None and "floor" in why
    lane = parse_routing('{"ladder_step": {"claude": {"profiles": ["codex"]}}}')
    assert resolve(lane, "ladder_step", "claude", SUMMARIES)[0] is None, "a Codex profile never fills a Claude lane"


def test_tolerant_parse_and_no_model_pins():
    logs = []
    table = parse_routing(
        '{"report": {"any": {"profiles": ["b"], "model": "haiku"}, "claude": {"profiles": ["a"]}},'
        ' "bogus": {}, "rank": {"claude": {"profiles": []}}}',
        logs.append,
    )
    assert set(table) == {"report"} and set(table["report"]) == {"claude"}
    assert any("profile's args" in line for line in logs), "a model pin is dropped, saying where it goes"
    assert parse_routing("not json") == {}
    assert quota_left({"usage": None}) == 100.0


def test_a_routing_file_written_before_the_rename_still_routes_its_report_steps():
    # Legacy (step 6.8): `scout` was the report kind's name.
    table = parse_routing('{"scout": {"any": {"profiles": ["b"]}}}')
    assert set(table) == {"report"} and table["report"]["any"].profiles == ("b",)
