"""The operator's local time: where their nights, weekdays and weeks fall.

A retrain opens on a Sunday night, the week's label cap resets on Monday, and the rollback guard waits for the next
day. All three read the zone of the machine the lab runs on, through `local`, and never a named one: the lab keeps the
hours of whoever runs it. Stamps on disk stay UTC.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

#: None: the system's zone, read at each call. A test pins a fixed one.
ZONE: tzinfo | None = None


def local(at: datetime) -> datetime:
    """`at` (an aware instant) on the machine's own clock."""
    return at.astimezone(ZONE)
