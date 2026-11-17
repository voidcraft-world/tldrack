"""The two things this plugin asks of its own bridge, over loopback HTTP.

A plugin cannot import the host (plugins/README.md), but it can talk to
it like any other client: `GET /snapshot` for a session's state, and the
legacy `POST /status` to raise `attention` for a canvas question. That second
call is the whole integration with the rack — the 🙋 dot, the rack board, the
coalesced banner and the phone push all already follow `attention`, so a canvas
question reaches every one of them without a line of UI code.

No Origin header is sent, which the host's guard reads as a native client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

HOST_TIMEOUT_SECONDS = 2


class HostClient:
    def __init__(self, status_port: int) -> None:
        self._base = f"http://127.0.0.1:{int(status_port)}"

    def session_rows(self) -> dict[str, dict]:
        """`{session name: snapshot row}`; `{}` when the bridge does not answer."""
        try:
            with urllib.request.urlopen(f"{self._base}/snapshot", timeout=HOST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            return {}
        rows = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}
        return {row["session"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("session"), str)}

    def raise_attention(self, session: str, message: str) -> bool:
        body = json.dumps({"session": session, "state": "attention", "message": message}).encode()
        request = urllib.request.Request(
            f"{self._base}/status", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=HOST_TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False
