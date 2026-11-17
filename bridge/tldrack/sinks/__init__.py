"""Where a notification actually GOES. One module per transport.

The bridge already knows when an agent is blocked — `hook_state()` classifies it
and `PresenceStore` says whether anyone is looking. What it has never had is a
way to tell you when you are not at the machine. This package is that half, kept
separate from the policy in `notify.py` for one reason: the transport is the part
most likely to change, and the policy is the part that must not.

A sink is duck-typed, exactly like a bridge plugin:

    name: str
    publish(*, title, body, priority, click=None, tags=None) -> bool

`publish` returns True when the transport accepted the message. It must NEVER
raise and must NEVER block for long — it is called from the notify tick thread,
and a wedged notifier that stalls the bridge would be a far worse bug than a
missed push. Same contract as `hook_command`'s 2s curl timeout and progress.py's
"the ledger is observability, never control flow".

Deliberately kwargs rather than a shared dataclass: a dataclass here would have
to be imported by `notify.py` AND every sink, which is a cycle waiting to happen
the moment a sink wants to consult policy. Five keyword arguments cost nothing.
"""

from __future__ import annotations

#: How long any sink may spend talking to the outside world. Matches the 2s
#: budget `hook_command` gives its curl — for the same reason. A notification is
#: never worth making the caller wait.
SINK_TIMEOUT_SECONDS = 2.0

#: Priority vocabulary, in the one place both the policy and the sinks can see
#: it. The numbers are ntfy's 1-5 scale because that is the first sink; a
#: transport without priorities is free to ignore them, and one with a different
#: scale maps them at its own boundary rather than leaking its scale up here.
PRIORITY_MIN = 1
PRIORITY_LOW = 2
PRIORITY_DEFAULT = 3
PRIORITY_HIGH = 4
PRIORITY_URGENT = 5
