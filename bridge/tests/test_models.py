import json

import pytest

from tldrack.models import (
    MODEL_TOKEN_RE,
    SessionModelStore,
    clean_usage,
    current_readings,
    parse_model_file,
    serialize_model_file,
)

USAGE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 5,
}


def test_parse_serialize_round_trip():
    sessions = {"vc-1": {"model": "claude-opus-5", "usage": USAGE, "at": 1700.5}}
    assert parse_model_file(serialize_model_file(sessions)) == sessions


def test_serialize_is_sorted_and_versioned():
    text = serialize_model_file(
        {"zeta": {"model": "claude-opus-5", "usage": None}, "alpha": {"model": "claude-sonnet-5", "usage": None}}
    )
    payload = json.loads(text)
    assert payload["version"] == 1
    assert list(payload["sessions"]) == ["alpha", "zeta"]


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-haiku-4-5-20251001", "us.anthropic.claude-sonnet-5-v1:0"],
)
def test_model_token_re_accepts_real_model_ids(model):
    # Bedrock inference-profile ids carry dots and a colon; the bridge must not
    # ship a model catalog it would then have to chase every release.
    assert MODEL_TOKEN_RE.fullmatch(model)


@pytest.mark.parametrize("model", ["", "has space", "x" * 129, "semi;colon"])
def test_model_token_re_rejects_garbage(model):
    assert not MODEL_TOKEN_RE.fullmatch(model)


def test_parse_drops_malformed_entries_keeps_good_ones():
    text = json.dumps(
        {
            "version": 1,
            "sessions": {
                "good": {"model": "claude-opus-5", "usage": USAGE},
                "bad session!": {"model": "claude-opus-5"},  # invalid session name
                "bad-model": {"model": "not a model"},  # invalid token
                "wrong-type": {"model": 7},  # non-string
                "not-an-object": "claude-opus-5",
            },
        }
    )
    assert parse_model_file(text) == {"good": {"model": "claude-opus-5", "usage": USAGE, "at": None}}


def test_parse_keeps_the_model_when_only_the_usage_is_bad():
    # The model is the part the label needs; a junk usage block must not cost
    # the session its name.
    text = json.dumps({"version": 1, "sessions": {"vc-1": {"model": "claude-opus-5", "usage": "nope"}}})
    assert parse_model_file(text) == {"vc-1": {"model": "claude-opus-5", "usage": None, "at": None}}


@pytest.mark.parametrize("text", ["not json", "[]", '{"sessions": "nope"}', '{"version": 1}'])
def test_parse_corrupt_document_raises(text):
    with pytest.raises(ValueError):
        parse_model_file(text)


def test_clean_usage_defaults_missing_counters_to_zero():
    assert clean_usage({"output_tokens": 20}) == {
        "input_tokens": 0,
        "output_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


@pytest.mark.parametrize("usage", [None, "nope", 7, {"output_tokens": "many"}, {"output_tokens": -1}])
def test_clean_usage_rejects_unusable_blocks(usage):
    assert clean_usage(usage) is None


# ── store ───────────────────────────────────────────────────────────────────


def test_store_set_get_persist(tmp_path):
    path = tmp_path / "session-models.json"
    store = SessionModelStore(path, clock=lambda: 1700.0)
    assert store.get_all() == {}
    store.set("vc-1", "claude-opus-5", USAGE)
    assert store.get_all() == {"vc-1": {"model": "claude-opus-5", "usage": USAGE, "at": 1700.0}}
    # A fresh store re-reads what the first one persisted — the whole point.
    assert SessionModelStore(path).get_all() == {"vc-1": {"model": "claude-opus-5", "usage": USAGE, "at": 1700.0}}


def test_store_ignores_a_none_model_rather_than_clearing(tmp_path):
    """The caller reaches set() on EVERY hook, and transcripts.update answers
    None for a session Claude has not spoken in yet. Treating that as 'forget'
    would erase a good reading on the first hook with no readable transcript."""
    store = SessionModelStore(tmp_path / "session-models.json", clock=lambda: 1700.0)
    store.set("vc-1", "claude-opus-5", USAGE)
    store.set("vc-1", None, None)
    assert store.get_all() == {"vc-1": {"model": "claude-opus-5", "usage": USAGE, "at": 1700.0}}


def test_store_ignores_a_malformed_model(tmp_path):
    store = SessionModelStore(tmp_path / "session-models.json")
    store.set("vc-1", "not a model", None)
    assert store.get_all() == {}


def test_store_does_not_rewrite_the_file_when_nothing_changed(tmp_path):
    """Unlike an icon pick, this is touched by every hook — a busy agent's
    PreToolUse storm must not rewrite the file on every tool call."""
    path = tmp_path / "session-models.json"
    store = SessionModelStore(path)
    store.set("vc-1", "claude-opus-5", USAGE)
    first = path.stat().st_mtime_ns
    for _ in range(5):
        store.set("vc-1", "claude-opus-5", dict(USAGE))
    assert path.stat().st_mtime_ns == first
    # A real change still lands.
    store.set("vc-1", "claude-sonnet-5", USAGE)
    assert store.get_all()["vc-1"]["model"] == "claude-sonnet-5"


def test_store_rename_moves_the_reading_and_persists(tmp_path):
    path = tmp_path / "session-models.json"
    store = SessionModelStore(path)
    store.set("vc-1", "claude-opus-5", USAGE)
    assert store.rename("vc-1", "backend") is True
    assert list(store.get_all()) == ["backend"]
    assert SessionModelStore(path).get_all()["backend"]["model"] == "claude-opus-5"


def test_store_rename_unknown_session_is_a_noop(tmp_path):
    store = SessionModelStore(tmp_path / "session-models.json")
    store.set("vc-1", "claude-opus-5", USAGE)
    assert store.rename("ghost", "backend") is False
    assert list(store.get_all()) == ["vc-1"]


def test_store_missing_file_starts_empty(tmp_path):
    assert SessionModelStore(tmp_path / "nope" / "session-models.json").get_all() == {}


def test_store_corrupt_file_tolerated_not_clobbered(tmp_path, capsys):
    path = tmp_path / "session-models.json"
    path.write_text("{corrupt")
    store = SessionModelStore(path)
    assert store.get_all() == {}
    assert "corrupt" in capsys.readouterr().err
    # Left alone until the next successful set() replaces it, so the user can
    # hand-repair rather than losing every reading.
    assert path.read_text() == "{corrupt"


def test_store_get_all_returns_independent_copies(tmp_path):
    """Including the NESTED usage dict. merge_snapshot drops these straight into
    rows handed to every client, so an aliased dict would let a caller mutate
    the store."""
    store = SessionModelStore(tmp_path / "session-models.json", clock=lambda: 1700.0)
    store.set("vc-1", "claude-opus-5", USAGE)
    snapshot = store.get_all()
    snapshot["vc-1"]["model"] = "mutated"
    snapshot["vc-1"]["usage"]["output_tokens"] = 999
    assert store.get_all()["vc-1"] == {"model": "claude-opus-5", "usage": USAGE, "at": 1700.0}


def test_store_does_not_alias_the_usage_it_was_handed(tmp_path):
    """The caller passes `transcripts.update`'s running totals, which that
    tracker keeps mutating — storing the same object would silently drift."""
    store = SessionModelStore(tmp_path / "session-models.json")
    live_totals = dict(USAGE)
    store.set("vc-1", "claude-opus-5", live_totals)
    live_totals["output_tokens"] = 999
    assert store.get_all()["vc-1"]["usage"]["output_tokens"] == USAGE["output_tokens"]


# ── a name is reused, a reading is not ───────────────────────────────────────


def reading(at: float | None, model: str = "claude-opus-5") -> dict:
    return {"model": model, "usage": None, "at": at}


def test_a_reading_older_than_the_session_holding_its_name_is_not_served():
    """An agent's pane, then days later a dev server under the same name: the server is not that agent."""
    readings = {"dev": reading(1000.0), "vc-1": reading(2500.0)}
    live = {"dev": {"created_at": 2000}, "vc-1": {"created_at": 2000}}
    assert current_readings(readings, live) == {"vc-1": reading(2500.0)}


def test_an_idle_agent_keeps_its_reading_across_a_restart():
    """Written after the session was created, read by a bridge that started later: the bug models.py exists for."""
    assert current_readings({"vc-1": reading(2500.0)}, {"vc-1": {"created_at": 2000}}) == {"vc-1": reading(2500.0)}
    # A pane the snapshot could not date, and a name with no live session, drop nothing.
    assert current_readings({"vc-1": reading(1.0), "gone": reading(1.0)}, {"vc-1": {}}) == {
        "vc-1": reading(1.0),
        "gone": reading(1.0),
    }


def test_an_undated_reading_is_served_except_under_a_machinery_name():
    """A file from before readings were dated: every idle agent keeps its label, and a service row gets none."""
    readings = {"vc-1": reading(None), "svc-vite": reading(None), "laya-sweep": reading(None)}
    live = {name: {"created_at": 2000} for name in readings}
    assert current_readings(readings, live) == {"vc-1": reading(None)}


def test_a_machinery_name_still_shows_a_reading_written_in_its_own_life():
    """A job that runs an agent in its pane is a hooked session: its own reading, dated since it began, is its own."""
    assert current_readings({"laya-label": reading(2500.0)}, {"laya-label": {"created_at": 2000}}) == {
        "laya-label": reading(2500.0)
    }


def test_an_undated_reading_earns_its_date_on_the_next_hook_and_is_then_left_alone(tmp_path):
    path = tmp_path / "session-models.json"
    path.write_text(json.dumps({"version": 1, "sessions": {"vc-1": {"model": "claude-opus-5", "usage": USAGE}}}))
    store = SessionModelStore(path, clock=lambda: 1700.0)
    assert store.get_all()["vc-1"]["at"] is None
    store.set("vc-1", "claude-opus-5", USAGE)
    assert store.get_all()["vc-1"]["at"] == 1700.0
    first = path.stat().st_mtime_ns
    store.set("vc-1", "claude-opus-5", dict(USAGE))
    assert path.stat().st_mtime_ns == first


def test_a_stored_date_that_is_not_a_time_reads_as_undated():
    text = json.dumps({"version": 1, "sessions": {"a": {"model": "m", "at": "soon"}, "b": {"model": "m", "at": True}}})
    assert {name: entry["at"] for name, entry in parse_model_file(text).items()} == {"a": None, "b": None}
