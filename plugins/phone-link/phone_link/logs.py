"""The host's log sink, made unable to raise.

The keeper and the listener run on daemon threads inside the bridge's process, and they log from their own
failure paths. A sink that raises there (`click.echo` on a closed stdout, an encoding error) would end the thread
silently: the keeper would stop seeing phones while the state still read `adb: ok`. So every line goes through
`guarded`, and a line that cannot be written is dropped.
"""

from __future__ import annotations

from collections.abc import Callable


def guarded(log: Callable[[str], None]) -> Callable[[str], None]:
    def write(line: str) -> None:
        try:
            log(line)
        except Exception:  # noqa: BLE001 — a lost log line must never take a thread down with it
            pass

    return write
