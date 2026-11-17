"""Unit tests for the browser-surface presence store (POST /presence).

No subprocess, no HTTP — `PresenceStore` takes an injectable `now` precisely so
TTL behaviour can be asserted without sleeping.
"""

import pytest

from tldrack.presence import (
    MAX_PRESENCE_CLIENTS,
    PRESENCE_TTL_SECONDS,
    PresenceStore,
    parse_presence,
)


def body(**overrides):
    payload = {
        "client_id": "tab-abc12345",
        "kind": "spa",
        "world": "terminal",
        "running": True,
        "rack_open": True,
        "rack_attached": True,
        "rack_focused": "vc-1",
    }
    payload.update(overrides)
    return payload


# ── parse_presence ──────────────────────────────────────────────────────────


def test_parses_a_full_record():
    client_id, record = parse_presence(body())
    assert client_id == "tab-abc12345"
    assert record == {
        "kind": "spa",
        "world": "terminal",
        "running": True,
        "rack_open": True,
        "rack_attached": True,
        "rack_focused": "vc-1",
    }


def test_omitted_fields_take_their_defaults():
    """A future non-SPA reporter should not have to invent rack fields."""
    _, record = parse_presence({"client_id": "tab-abc12345"})
    assert record == {
        "kind": "spa",
        "world": None,
        "running": False,
        "rack_open": False,
        "rack_attached": False,
        "rack_focused": None,
    }


def test_null_world_is_a_real_state_not_missing_data():
    """The SPA reports world: null while in the world SELECTOR."""
    _, record = parse_presence(body(world=None))
    assert record["world"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no client_id at all
        {"client_id": "short"},  # under 8 chars
        {"client_id": "a" * 65},  # over 64
        {"client_id": "has spaces here"},  # outside the charset
        {"client_id": 12345678},  # not a string
        body(running="yes"),  # bool fields are strict
        body(rack_open=1),  # 1 is not True here
        body(rack_focused="not a session!"),  # fails SESSION_NAME_RE
        body(rack_focused=7),
        body(world="a" * 65),
    ],
)
def test_malformed_payloads_fail_loud(payload):
    """Fail loud (caller 400s) rather than storing a coerced guess — a record
    that is wrong about which session the rack shows sends the user to the
    wrong window, which is worse than having no record."""
    with pytest.raises(ValueError):
        parse_presence(payload)


def test_non_dict_body_rejected():
    with pytest.raises(ValueError):
        parse_presence(["not", "an", "object"])


# ── PresenceStore ───────────────────────────────────────────────────────────


def test_new_client_reports_changed():
    store = PresenceStore()
    assert store.set("tab-a1234567", body(), now=100.0) is True


def test_unchanged_heartbeat_is_a_noop():
    """Rule 3: without this, every open tab pushes a full SSE snapshot to every
    listener on every beat, forever."""
    store = PresenceStore()
    record = parse_presence(body())[1]
    assert store.set("tab-a1234567", record, now=100.0) is True
    assert store.set("tab-a1234567", record, now=105.0) is False
    assert store.set("tab-a1234567", record, now=110.0) is False


def test_any_field_change_reports_changed():
    store = PresenceStore()
    store.set("tab-a1234567", parse_presence(body())[1], now=100.0)
    moved = parse_presence(body(rack_focused="vc-2"))[1]
    assert store.set("tab-a1234567", moved, now=105.0) is True


def test_heartbeat_refreshes_the_ttl():
    """An unchanged beat must still keep the record alive — it reports False for
    the SSE push, not for liveness."""
    store = PresenceStore()
    record = parse_presence(body())[1]
    store.set("tab-a1234567", record, now=100.0)
    store.set("tab-a1234567", record, now=110.0)
    assert len(store.list(now=110.0 + PRESENCE_TTL_SECONDS - 1)) == 1


def test_records_expire_on_read():
    store = PresenceStore()
    store.set("tab-a1234567", parse_presence(body())[1], now=100.0)
    assert len(store.list(now=100.0 + PRESENCE_TTL_SECONDS - 1)) == 1
    assert store.list(now=100.0 + PRESENCE_TTL_SECONDS + 1) == []


def test_list_carries_client_id_and_age():
    store = PresenceStore()
    store.set("tab-a1234567", parse_presence(body())[1], now=100.0)
    (row,) = store.list(now=103.5)
    assert row["client_id"] == "tab-a1234567"
    assert row["age_seconds"] == 3.5
    assert row["world"] == "terminal"


def test_list_is_newest_first():
    store = PresenceStore()
    store.set("tab-older-01", parse_presence(body())[1], now=100.0)
    store.set("tab-newer-01", parse_presence(body(world="other"))[1], now=105.0)
    assert [row["client_id"] for row in store.list(now=106.0)] == ["tab-newer-01", "tab-older-01"]


def test_cap_evicts_the_least_recently_seen():
    """client_id is caller-chosen, so an unbounded store is a memory leak a
    looping page could drive."""
    store = PresenceStore()
    for index in range(MAX_PRESENCE_CLIENTS):
        # Same instant for all, then bump one so "oldest" is unambiguous.
        store.set(f"tab-{index:08d}", parse_presence(body())[1], now=100.0)
    store.set("tab-00000000", parse_presence(body(world="other"))[1], now=101.0)

    store.set("tab-newcomer", parse_presence(body())[1], now=102.0)
    ids = {row["client_id"] for row in store.list(now=102.0)}
    assert len(ids) == MAX_PRESENCE_CLIENTS
    assert "tab-newcomer" in ids
    assert "tab-00000001" not in ids  # least recently seen went
    assert "tab-00000000" in ids  # refreshed one survived


def test_expired_clients_free_the_cap_without_eviction():
    store = PresenceStore()
    for index in range(MAX_PRESENCE_CLIENTS):
        store.set(f"tab-{index:08d}", parse_presence(body())[1], now=100.0)
    later = 100.0 + PRESENCE_TTL_SECONDS + 1
    store.set("tab-newcomer", parse_presence(body())[1], now=later)
    assert [row["client_id"] for row in store.list(now=later)] == ["tab-newcomer"]


def test_stored_record_is_copied():
    """The handler builds a dict per request; a later mutation of it must not
    reach into the store."""
    store = PresenceStore()
    record = parse_presence(body())[1]
    store.set("tab-a1234567", record, now=100.0)
    record["rack_focused"] = "mutated"
    assert store.list(now=100.0)[0]["rack_focused"] == "vc-1"
