from tldrack.statusline_models import (
    MAX_STATUSLINE_PATHS,
    StatuslineReadingStore,
    parse_statusline_context_left,
    parse_statusline_model,
    resolve_context_left,
    resolve_fallback_models,
)

USAGE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_creation_input_tokens": 30,
    "cache_read_input_tokens": 40,
}


# ── parse_statusline_model ──────────────────────────────────────────────


def test_parse_reads_model_id():
    assert parse_statusline_model({"model": {"id": "claude-opus-5", "display_name": "Opus"}}) == "claude-opus-5"


def test_parse_accepts_bedrock_inference_profile_ids():
    # Shape-validated, not membership-validated — same rule as models.py.
    assert parse_statusline_model({"model": {"id": "us.anthropic.claude-sonnet-5-v1:0"}}) == "us.anthropic.claude-sonnet-5-v1:0"


def test_parse_prefers_id_over_display_name():
    # display_name is "Opus"; the label formatter needs the versioned id.
    payload = {"model": {"id": "claude-haiku-4-5-20251001", "display_name": "Haiku"}}
    assert parse_statusline_model(payload) == "claude-haiku-4-5-20251001"


def test_parse_returns_none_never_raises():
    # A malformed model must never 400 a statusline Claude Code is waiting on —
    # unlike rate_limits, which IS the payload's purpose and fails loud.
    for payload in (
        None,
        "nope",
        {},
        {"model": None},
        {"model": "claude-opus-5"},  # not an object
        {"model": {"display_name": "Opus"}},  # no id
        {"model": {"id": ""}},
        {"model": {"id": 5}},
        {"model": {"id": "has spaces"}},  # MODEL_TOKEN_RE violation
        {"model": {"id": "x" * 200}},  # over the 128-char cap
    ):
        assert parse_statusline_model(payload) is None


# ── resolve_fallback_models ─────────────────────────────────────────────


def test_statusline_fills_a_session_the_disk_store_never_saw():
    # THE BUG: a brand-new tmux name has no disk entry at all.
    resolved = resolve_fallback_models({}, {"/t/a.jsonl": "claude-opus-5"}, {"vc-9": "/t/a.jsonl"})
    assert resolved == {"vc-9": {"model": "claude-opus-5", "usage": None}}


def test_statusline_overrides_a_different_remembered_model():
    # Reused tmux name: the disk store answers for a PREVIOUS Claude session.
    resolved = resolve_fallback_models(
        {"vc-1": {"model": "claude-sonnet-5", "usage": USAGE}},
        {"/t/a.jsonl": "claude-opus-5"},
        {"vc-1": "/t/a.jsonl"},
    )
    assert resolved == {"vc-1": {"model": "claude-opus-5", "usage": None}}


def test_equal_model_keeps_the_remembered_usage():
    # Replacing like with like would silently cost the row its cost estimate.
    disk = {"vc-1": {"model": "claude-opus-5", "usage": USAGE}}
    resolved = resolve_fallback_models(disk, {"/t/a.jsonl": "claude-opus-5"}, {"vc-1": "/t/a.jsonl"})
    assert resolved == {"vc-1": {"model": "claude-opus-5", "usage": USAGE}}


def test_disk_survives_with_no_statusline_report():
    disk = {"vc-1": {"model": "claude-opus-5", "usage": USAGE}}
    assert resolve_fallback_models(disk, {}, {"vc-1": "/t/a.jsonl"}) == disk


def test_unbound_path_resolves_to_nothing():
    # A statusline from a non-tmux Claude session: no session name, no row.
    assert resolve_fallback_models({}, {"/t/other.jsonl": "claude-opus-5"}, {"vc-1": "/t/a.jsonl"}) == {}


def test_result_does_not_alias_the_disk_store():
    disk = {"vc-1": {"model": "claude-opus-5", "usage": USAGE}}
    resolved = resolve_fallback_models(disk, {}, {})
    resolved["vc-1"]["model"] = "mutated"
    assert disk["vc-1"]["model"] == "claude-opus-5"


# ── StatuslineReadingStore ────────────────────────────────────────────────


def test_store_reports_change_only_on_real_news():
    store = StatuslineReadingStore()
    assert store.set("/t/a.jsonl", "claude-opus-5") is True
    # A statusline refreshes constantly; an unchanged reading must not push SSE.
    assert store.set("/t/a.jsonl", "claude-opus-5") is False
    assert store.set("/t/a.jsonl", "claude-sonnet-5") is True
    assert store.get_all() == {"/t/a.jsonl": "claude-sonnet-5"}


def test_store_ignores_missing_halves():
    store = StatuslineReadingStore()
    assert store.set(None, "claude-opus-5") is False  # path refused by the guard
    assert store.set("/t/a.jsonl", None) is False  # payload had no usable model
    assert store.set("", "claude-opus-5") is False
    assert store.get_all() == {}


def test_store_get_all_does_not_alias():
    store = StatuslineReadingStore()
    store.set("/t/a.jsonl", "claude-opus-5")
    store.get_all()["/t/a.jsonl"] = "mutated"
    assert store.get_all() == {"/t/a.jsonl": "claude-opus-5"}


def test_store_is_bounded_against_a_caller_supplied_key():
    # The key comes off the wire and realpath accepts a path that does not
    # exist, so an unbounded store is a memory-growth route past the Origin guard.
    store = StatuslineReadingStore()
    for i in range(MAX_STATUSLINE_PATHS + 50):
        store.set(f"/t/{i}.jsonl", "claude-opus-5")
    assert len(store.get_all()) == MAX_STATUSLINE_PATHS


def test_eviction_keeps_the_hottest_path():
    """An unchanged reading still counts as a touch. Without that, the ONE path
    reporting every second would be the oldest by insertion order and the first
    thing evicted — the store would drop exactly the session it exists for."""
    store = StatuslineReadingStore()
    store.set("/t/hot.jsonl", "claude-opus-5")
    for i in range(MAX_STATUSLINE_PATHS):
        store.set(f"/t/{i}.jsonl", "claude-sonnet-5")
        assert store.set("/t/hot.jsonl", "claude-opus-5") is False  # unchanged, but a touch
    assert store.get_all()["/t/hot.jsonl"] == "claude-opus-5"


def test_store_keeps_a_zero_reading_and_can_discard():
    # 0% left is the most important reading there is — falsiness must not drop it.
    store = StatuslineReadingStore()
    assert store.set("/t/a.jsonl", 0) is True
    assert store.get_all() == {"/t/a.jsonl": 0}
    assert store.discard("/t/a.jsonl") is True
    assert store.discard("/t/a.jsonl") is False
    assert store.discard(None) is False
    assert store.get_all() == {}


# ── context left ─────────────────────────────────────────────────────────


def test_context_parse_distinguishes_absent_from_unknown():
    assert parse_statusline_context_left({}) == (False, None)  # older Claude Code: keep
    assert parse_statusline_context_left("nope") == (False, None)
    assert parse_statusline_context_left({"context_window": {"remaining_percentage": None}}) == (
        True,
        None,
    )  # post-/compact: discard
    assert parse_statusline_context_left({"context_window": {"remaining_percentage": 37.4}}) == (True, 37)


def test_context_resolves_through_the_transcript_join():
    contexts = {"/t/a.jsonl": 62, "/t/orphan.jsonl": 5}
    paths = {"vc-1": "/t/a.jsonl", "vc-2": "/t/b.jsonl"}
    assert resolve_context_left(contexts, paths) == {"vc-1": 62}
