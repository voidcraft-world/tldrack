"""Session names the rack keeps for machinery, and refuses for an agent.

The rack files a tmux session under SERVICES by its NAME prefix, because a prefix is a contract with whatever
names the session: a plugin finds its pane again by that name. The page owns what the rack does with such a
row and the full list, which a host may extend (`ui/src/config/service-session-flags.ts`). This file owns the
prefixes this repository's own plugins use, so the bridge can refuse them at the two doors that name an AGENT
session: `POST /spawn` and `POST /rename-session`.

WHY A REFUSAL. An agent session named `laya-limits` sat among the services: it left the agents' urgency ladder,
and a service row offers no rename, so the page had no way back. A name is cheap to refuse and expensive to
discover wrong.

Plugins make their panes with tmux directly, never through those two doors, so nothing here stops a job or a
service being started. A host's extra prefixes are the page's alone; the page refuses those itself before it
posts (`ui/src/engine/terminal/session-name.ts`). `tests/test_service_names.py` holds the two default lists equal.

Import-free.
"""

from __future__ import annotations

#: Long-lived services a supervisor plugin keeps up.
SERVICE_PREFIXES = ("svc-",)
#: One-shot jobs a plugin starts in a pane the operator can watch (`plugins/laya`).
JOB_PREFIXES = ("laya-",)
RESERVED_PREFIXES = SERVICE_PREFIXES + JOB_PREFIXES


def reserved_prefix(name: object) -> str | None:
    """The reserved prefix `name` starts with, or None. Pure."""
    if not isinstance(name, str):
        return None
    return next((prefix for prefix in RESERVED_PREFIXES if name.startswith(prefix)), None)


def reserved_error(name: object) -> str | None:
    """Why an agent session may not take `name`, or None when it may. Pure."""
    prefix = reserved_prefix(name)
    if prefix is None:
        return None
    return f"names starting with '{prefix}' are kept for the rack's services and jobs — pick another"
