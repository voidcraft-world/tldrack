"""Who may read `GET /plugin/phone-link/state` on the bridge: this Mac only.

A loopback `Host`, no proxy header, and no browser request that hid its Origin (`no-cors`). Which browser origins
may read is the host's call, not this plugin's: the host's origin guard runs ahead of every plugin route, and it
admits loopback pages plus whatever the operator configured. So `allowed_origins` is None here, which leaves that
policy in charge instead of naming any site in code. The state names each phone and whether it is linked, never the
token.

This guards the bridge-side route only. The link's own listener has a stricter gate of its own (gate.py).
"""

from __future__ import annotations

from voidcraft_world_bridge.policy import CallerPolicy

POLICY = CallerPolicy(allowed_origins=None, subject="phone link")
request_refusal = POLICY.refusal
