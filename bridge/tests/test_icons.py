import json

import pytest

from tldrack.icons import ICON_TOKEN_RE, SessionIconStore, parse_icon_file, serialize_icon_file


def test_parse_serialize_round_trip():
    icons = {"tldrack": "flame", "research": "wrench.and.screwdriver"}
    assert parse_icon_file(serialize_icon_file(icons)) == icons


def test_serialize_is_sorted_and_versioned():
    text = serialize_icon_file({"zeta": "leaf", "alpha": "star"})
    payload = json.loads(text)
    assert payload["version"] == 1
    assert list(payload["icons"]) == ["alpha", "zeta"]


def test_parse_drops_malformed_entries_keeps_good_ones():
    text = json.dumps(
        {
            "version": 1,
            "icons": {
                "good": "flame",
                "bad session!": "flame",  # invalid session name
                "bad-icon": "Not A Token",  # invalid icon token
                "wrong-type": 7,  # non-string icon
            },
        }
    )
    assert parse_icon_file(text) == {"good": "flame"}


@pytest.mark.parametrize("text", ["not json", "[]", '{"icons": "nope"}', '{"version": 1}'])
def test_parse_corrupt_document_raises(text):
    with pytest.raises(ValueError):
        parse_icon_file(text)


def test_icon_token_re_accepts_sf_symbol_names():
    for name in ("terminal", "wrench.and.screwdriver", "server.rack", "chart.bar"):
        assert ICON_TOKEN_RE.fullmatch(name)


def test_icon_token_re_rejects_garbage():
    for name in ("", "UPPER", "has space", "x" * 65, "emoji🔥"):
        assert not ICON_TOKEN_RE.fullmatch(name)


def test_store_set_get_persist(tmp_path):
    path = tmp_path / "session-icons.json"
    store = SessionIconStore(path)
    assert store.get_all() == {}
    store.set("tldrack", "flame")
    assert store.get_all() == {"tldrack": "flame"}
    # A fresh store re-reads what the first one persisted.
    assert SessionIconStore(path).get_all() == {"tldrack": "flame"}


def test_store_set_none_deletes(tmp_path):
    path = tmp_path / "session-icons.json"
    store = SessionIconStore(path)
    store.set("tldrack", "flame")
    store.set("tldrack", None)
    assert store.get_all() == {}
    assert SessionIconStore(path).get_all() == {}


def test_store_delete_unknown_session_is_noop(tmp_path):
    store = SessionIconStore(tmp_path / "session-icons.json")
    store.set("ghost", None)
    assert store.get_all() == {}


def test_store_missing_file_starts_empty(tmp_path):
    assert SessionIconStore(tmp_path / "nope" / "session-icons.json").get_all() == {}


def test_store_corrupt_file_tolerated_not_clobbered(tmp_path, capsys):
    path = tmp_path / "session-icons.json"
    path.write_text("{corrupt")
    store = SessionIconStore(path)
    assert store.get_all() == {}
    assert "corrupt" in capsys.readouterr().err
    # The corrupt file is left alone until the next successful set() replaces it.
    assert path.read_text() == "{corrupt"


def test_store_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "session-icons.json"
    SessionIconStore(path).set("tldrack", "star")
    assert json.loads(path.read_text())["icons"] == {"tldrack": "star"}


def test_store_get_all_returns_copy(tmp_path):
    store = SessionIconStore(tmp_path / "session-icons.json")
    store.set("tldrack", "flame")
    snapshot = store.get_all()
    snapshot["tldrack"] = "mutated"
    assert store.get_all() == {"tldrack": "flame"}


# ── rename ──────────────────────────────────────────────────────────────────
# POST /rename-session moves the pick across. Without it a renamed session
# silently drops back to its hash-computed default while the pick lingers under
# a name nothing will ever look up again.


def test_store_rename_moves_the_pick_and_persists(tmp_path):
    path = tmp_path / "session-icons.json"
    store = SessionIconStore(path)
    store.set("vc-1", "flame")
    assert store.rename("vc-1", "backend") is True
    assert store.get_all() == {"backend": "flame"}
    # Persisted, not just in memory — the pick has to survive a bridge restart.
    assert SessionIconStore(path).get_all() == {"backend": "flame"}


def test_store_rename_unknown_session_is_a_noop(tmp_path):
    path = tmp_path / "session-icons.json"
    store = SessionIconStore(path)
    store.set("vc-1", "flame")
    # A session that never had an explicit pick renders its hash default; there
    # is nothing to carry, and the rename must not invent an entry.
    assert store.rename("ghost", "backend") is False
    assert store.get_all() == {"vc-1": "flame"}


def test_store_rename_replaces_a_stale_pick_on_the_new_name(tmp_path):
    store = SessionIconStore(tmp_path / "session-icons.json")
    store.set("vc-1", "flame")
    store.set("backend", "leaf")  # a dormant pick from some earlier session
    assert store.rename("vc-1", "backend") is True
    # tmux already accepted the rename, so the dormant pick described a session
    # that no longer exists under that name. The moved one wins.
    assert store.get_all() == {"backend": "flame"}
