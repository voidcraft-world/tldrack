from tldrack.events import (
    HEARTBEAT_CHUNK,
    EventBroker,
    format_sse_event,
    next_chunk,
)


# ── format_sse_event ────────────────────────────────────────────────────


def test_format_shape():
    assert format_sse_event("snapshot", {"a": 1}) == b'event: snapshot\ndata: {"a": 1}\n\n'


def test_format_newline_in_string_stays_one_data_line():
    chunk = format_sse_event("activity", {"detail": "line1\nline2"})
    # json escapes the newline, so the frame still has exactly one data line.
    assert chunk.count(b"\ndata: ") == 1
    assert b"\\n" in chunk


# ── EventBroker ─────────────────────────────────────────────────────────


def test_register_and_count():
    broker = EventBroker()
    assert broker.client_count() == 0
    handle = broker.register()
    assert handle is not None
    assert broker.client_count() == 1
    broker.unregister(handle)
    assert broker.client_count() == 0


def test_register_cap_returns_none_and_slot_frees():
    broker = EventBroker(max_clients=2)
    first = broker.register()
    broker.register()
    assert broker.register() is None
    broker.unregister(first)
    assert broker.register() is not None


def test_double_unregister_is_harmless():
    broker = EventBroker()
    handle = broker.register()
    broker.unregister(handle)
    broker.unregister(handle)
    assert broker.client_count() == 0


def test_broadcast_reaches_all_registered():
    broker = EventBroker()
    a = broker.register()
    b = broker.register()
    broker.broadcast("snapshot", {"n": 1})
    expected = format_sse_event("snapshot", {"n": 1})
    assert a.chunks.get_nowait() == expected
    assert b.chunks.get_nowait() == expected


def test_broadcast_skips_unregistered():
    broker = EventBroker()
    handle = broker.register()
    broker.unregister(handle)
    broker.broadcast("snapshot", {})
    assert handle.chunks.empty()


def test_broadcast_overflow_drops_oldest():
    broker = EventBroker()
    handle = broker.register()
    capacity = handle.chunks.maxsize
    for i in range(capacity):
        broker.broadcast("snapshot", {"n": i})
    broker.broadcast("snapshot", {"n": "newest"})
    drained = []
    while not handle.chunks.empty():
        drained.append(handle.chunks.get_nowait())
    assert len(drained) == capacity
    assert drained[0] == format_sse_event("snapshot", {"n": 1})  # {"n": 0} dropped
    assert drained[-1] == format_sse_event("snapshot", {"n": "newest"})


# ── next_chunk ──────────────────────────────────────────────────────────


def test_next_chunk_returns_queued_event():
    broker = EventBroker()
    handle = broker.register()
    broker.broadcast("activity", {"x": 1})
    assert next_chunk(handle, timeout=0) == format_sse_event("activity", {"x": 1})


def test_next_chunk_heartbeat_when_idle():
    broker = EventBroker()
    handle = broker.register()
    assert next_chunk(handle, timeout=0) == HEARTBEAT_CHUNK
