"""Reading the END of a Claude Code transcript, cheaply and without lying.

Three properties of Claude Code transcripts drive everything here (the README's
§ What the detection relies on lists them all):

1. **`tail -n` is unbounded.** One record can carry a whole file's contents, so
   the last few dozen lines range from kilobytes to megabytes. The window is a
   fixed BYTE tail; the record count falls out of it. The first line after a
   mid-file seek is almost certainly a fragment, so it is dropped.

2. **Many record types carry no `timestamp`** (`mode`, `last-prompt`,
   `ai-title`, `permission-mode`, `agent-name`, `bridge-session`,
   `file-history-snapshot`) and are commonly the FINAL line, so a naive "read
   the last record" often answers `None`. Hence the backward scan.

3. **File mtime lies.** Anything that bulk-touches the directory moves it, by
   hours or days. mtime is only ever used as a cheap pre-filter (it
   over-reports freshness, so it never hides a live session); `last_active`
   always comes from a record timestamp.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: Generous but bounded. Big enough that the last ~30 conversational turns —
#: the recency window signals C/D need — are in it for all but the most
#: attachment-heavy sessions, small enough that a whole audit reads tens of MB
#: however large the transcript directory has grown.
TAIL_BYTES = 512 * 1024


def tail_records(path: Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """Parse the last `tail_bytes` of a JSONL transcript into records.

    Every kind of damage is survivable by design: a truncated first line, a
    half-written last line, a line that is not JSON at all, a line that is JSON
    but not an object. A transcript is an append-only log written by another
    process — being strict here would mean an unreadable audit whenever the
    scan raced a live session's write.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(-tail_bytes, os.SEEK_END)
                handle.readline()  # discard the partial line the seek landed inside
            blob = handle.read()
    except OSError:
        return []

    records: list[dict] = []
    for line in blob.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def parse_timestamp(value: object) -> datetime | None:
    """An ISO-8601 record timestamp as an aware UTC datetime, or None.

    Transcripts write `2030-01-02T03:04:05.678Z`, which `fromisoformat` refuses
    before Python 3.11 — the bridge supports 3.10, so the `Z` is rewritten.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def last_timestamp(records: list[dict]) -> datetime | None:
    """The newest real activity in the tail: scan BACKWARD to the first record
    that actually carries a timestamp (property 2 in the module docstring)."""
    for record in reversed(records):
        stamp = parse_timestamp(record.get("timestamp"))
        if stamp is not None:
            return stamp
    return None


def iso_utc(moment: datetime | None) -> str | None:
    """Second-resolution `...Z` — the wire format the contract specifies."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_hours(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return round((now - moment).total_seconds() / 3600.0, 1)
