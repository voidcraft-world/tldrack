"""Server-Sent Events broker for the status listener (GET /events).

Queue-per-client so broadcasters NEVER touch sockets: a hook POST formats the
event once and drops it on every client's queue; each SSE handler thread owns
its own wfile and drains its own queue. A wedged client can therefore never
block a hook report, and no per-client write lock exists. The queue.get
timeout doubles as the heartbeat scheduler — no timer thread.

Dead clients reap themselves: the next heartbeat/event write in the client's
OWN handler thread raises (BrokenPipe/ConnectionReset/OSError) and its
`finally: unregister` runs. Worst-case zombie lifetime ≈ two heartbeats (the
first write to a dead socket can land in the OS buffer). Handler threads are
daemons (ThreadingHTTPServer.daemon_threads), so lingering SSE loops cannot
block bridge shutdown — teardown is plain process exit.
"""

from __future__ import annotations

import json
import queue
import threading

HEARTBEAT_SECONDS = 15.0
MAX_SSE_CLIENTS = 16
# SSE comment line — keeps intermediaries/clients aware the stream is alive.
HEARTBEAT_CHUNK = b": ping\n\n"


def format_sse_event(event: str, data: dict) -> bytes:
    """One SSE frame: event name + a single data line. json.dumps escapes
    newlines inside strings, so the payload can never break the framing."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class ClientHandle:
    """One connected SSE client. maxsize bounds memory if a client stalls;
    overflow drops the OLDEST chunk (see EventBroker.broadcast)."""

    def __init__(self) -> None:
        self.chunks: queue.Queue[bytes] = queue.Queue(maxsize=32)


class EventBroker:
    def __init__(self, max_clients: int = MAX_SSE_CLIENTS) -> None:
        self._lock = threading.Lock()
        self._max_clients = max_clients
        self._clients: list[ClientHandle] = []

    def register(self) -> ClientHandle | None:
        """New client handle, or None at the cap (handler answers 503)."""
        with self._lock:
            if len(self._clients) >= self._max_clients:
                return None
            handle = ClientHandle()
            self._clients.append(handle)
            return handle

    def unregister(self, handle: ClientHandle) -> None:
        with self._lock:
            try:
                self._clients.remove(handle)
            except ValueError:
                pass  # double-unregister is harmless

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, event: str, data: dict) -> None:
        """Format once, enqueue everywhere. On a full queue drop the oldest
        chunk first — snapshot events are full-state, so newest wins."""
        chunk = format_sse_event(event, data)
        with self._lock:
            clients = list(self._clients)
        for handle in clients:
            try:
                handle.chunks.put_nowait(chunk)
            except queue.Full:
                try:
                    handle.chunks.get_nowait()
                except queue.Empty:
                    pass
                try:
                    handle.chunks.put_nowait(chunk)
                except queue.Full:
                    pass  # racing producers — the client is drowning anyway


def next_chunk(handle: ClientHandle, timeout: float = HEARTBEAT_SECONDS) -> bytes:
    """Next bytes for the client's socket: a queued event, or a heartbeat
    after `timeout` idle seconds. The handler loop is just
    write(next_chunk(...)) — everything above this line is socket-free."""
    try:
        return handle.chunks.get(timeout=timeout)
    except queue.Empty:
        return HEARTBEAT_CHUNK
