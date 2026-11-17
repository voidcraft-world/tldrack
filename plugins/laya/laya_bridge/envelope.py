"""The policy routes' envelope, and the one refusal every policy-route module raises.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

Wire shapes: owned by schemas/src/laya-review.ts — see the note there. Every policy-route body
carries `{v, ok, message}`. A refusal adds `code` and an optional `hint`, and repeats `message` as
`error`, so the shared front-end readers (bridge-error.ts, bridge-post.ts) show it unchanged.
`review`, `review_log` and `lab_jobs` raise `Refusal`; `refusal_response` is its one rendering.
"""

from __future__ import annotations

from typing import Any

from laya_bridge.files import clip

VERSION = 1
MESSAGE_MAX = 160
HINT_MAX = 200
PARAMS_HINT = "restart the bridge (it creates <data>/laya), or run laya-lab once"

Response = tuple[int, dict]


class Refusal(Exception):
    """A refused policy request: the route's status, a code, one sentence that never echoes a value, a hint."""

    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.hint = status, code, message, hint


def params_unavailable() -> Refusal:
    return Refusal(503, "params_unavailable", "The laya params folder is not there yet", PARAMS_HINT)


def ok_body(message: str, **fields: Any) -> dict:
    return {"v": VERSION, "ok": True, "message": clip(message, MESSAGE_MAX), **fields}


def error_response(http_status: int, code: str, message: str, /, **extra: Any) -> Response:
    """Every policy-route refusal: `{v, ok:false, code, message, error: message, hint?, …}`.

    Positional-only, so a body field named `status` (the restart result's) is just another extra.
    """
    message = clip(message, MESSAGE_MAX)
    body: dict[str, Any] = {"v": VERSION, "ok": False, "code": code, "message": message, "error": message}
    for name, value in extra.items():
        if name == "hint":
            if value:
                body["hint"] = clip(str(value), HINT_MAX)
        else:
            body[name] = value
    return http_status, body


def refusal_response(refusal: Refusal, /, **extra: Any) -> Response:
    """`refusal` as its route answers it; `extra` carries the route's own null result fields."""
    return error_response(refusal.status, refusal.code, refusal.message, hint=refusal.hint, **extra)
