"""A LEAD session's tmux name: how the launch builds it and how the rack recognises it, in one place.

`lead-<profile>-MMDD-HHMM`, with `-2`, `-3`… when two leads launch on one account in one minute. It
says which account and when, never what the lead is about, so `session_names.py` counts it as
GENERATED, like `+ NEW`'s `vc-N`: the operator's first real prompt labels it, and the run's title
names it (`LeadService.verb_start`). The name itself never changes. The run store, the typed queue
and every worker's `parent_session` hold it in rack.sqlite, and a rename would orphan them.
"""

from __future__ import annotations

import re
import time
from collections.abc import Container

from tldrack.profiles import PROFILE_ID_RE

#: The optional `-n` is both a same-minute launch and `closed_sessions.reopen_name`'s suffix, so a
#: reopened lead stays recognisable and takes its label back.
LEAD_NAME_RE = re.compile(rf"lead-(?:{PROFILE_ID_RE.pattern})-\d{{4}}-\d{{4}}(?:-\d+)?")


def lead_name(profile: str, now: float, live: Container[str]) -> str:
    """The name a LEAD launched on `profile` at `now` takes: the first one `live` does not hold."""
    base = f"lead-{profile}-{time.strftime('%m%d-%H%M', time.localtime(now))}"
    name, n = base, 2
    while name in live:
        name, n = f"{base}-{n}", n + 1
    return name
