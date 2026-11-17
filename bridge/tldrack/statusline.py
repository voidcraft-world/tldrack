#!/usr/bin/env python3
"""Claude Code statusLine command → tldrack usage forwarder.

Claude Code pipes a JSON payload to the configured statusLine command on every
refresh; for Max subscribers it includes `rate_limits` — the same 5-hour /
weekly percentages the /usage screen shows. This script forwards the RAW
payload to the bridge (POST /usage — the bridge does all parsing, so future
field changes never require re-merging settings.json) and prints a one-line
statusline. Print the ready-made stanza with `tldrack
--print-statusline` and merge it into ~/.claude/settings.json.

Deliberately self-contained — stdlib only, ZERO tldrack imports — so
the stanza can run it under the system `python3` directly: no venv coupling,
no `uv run` resolver overhead on every statusline refresh. A statusline must
never block or crash Claude Code, hence the blanket exception swallowing and
the 0.3s POST timeout; the exit code is always 0.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

DEFAULT_STATUS_PORT = 7682


def resolve_port(argv: list[str], env: dict) -> int:
    """--status-port N > TLDRACK_STATUS_PORT > TLDRACK_PORT + 1
    > 7682. Garbage at any step falls through to the next."""
    for i, arg in enumerate(argv):
        raw = None
        if arg == "--status-port" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif arg.startswith("--status-port="):
            raw = arg.split("=", 1)[1]
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                break
    try:
        return int(env["TLDRACK_STATUS_PORT"])
    except (KeyError, ValueError):
        pass
    try:
        return int(env["TLDRACK_PORT"]) + 1
    except (KeyError, ValueError):
        pass
    return DEFAULT_STATUS_PORT


def post_usage(body: bytes, status_port: int, timeout: float = 0.3) -> None:
    """Fire-and-forget POST of the raw stdin JSON to the bridge. Any failure
    (bridge down, slow, port squatted) is silently ignored — the statusline
    text still renders and Claude Code never sees an error."""
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{status_port}/usage",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except Exception:
        pass


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def context_left_percent(payload: dict) -> int | None:
    """Percent of the context window still free, 0–100, or None when unknown.

    THE owner of this formula: the bridge imports it (statusline_models.py) so the
    terminal's status line and the rack's row can never disagree. Claude Code's
    `context_window.remaining_percentage` first; `100 - used_percentage` when only
    that is sent. Both are documented as null early in a session and right after
    /compact, so None is a normal answer, not an error. Never raises.
    """
    try:
        window = payload.get("context_window")
        if not isinstance(window, dict):
            return None
        left = _number(window.get("remaining_percentage"))
        if left is None:
            used = _number(window.get("used_percentage"))
            left = None if used is None else 100.0 - used
        return None if left is None else max(0, min(100, round(left)))
    except Exception:
        return None


def render_statusline(payload: dict) -> str:
    """'62% left · 5h 24% · wk 41%' — context left, then one segment per usage
    window with a numeric used_percentage. Never raises.

    No model name: the rack row and the dock already say which model a session
    runs, and this line used to repeat it under every pane. It is printed only as
    the fallback when nothing else is known yet (a session before its first API
    response), so the line is never blank."""
    try:
        segments = []
        left = context_left_percent(payload)
        if left is not None:
            segments.append(f"{left}% left")
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            for name, tag in (("five_hour", "5h"), ("seven_day", "wk")):
                window = rate_limits.get(name)
                used = _number(window.get("used_percentage")) if isinstance(window, dict) else None
                if used is not None:
                    segments.append(f"{tag} {round(used)}%")
        if segments:
            return " · ".join(segments)
        model = payload.get("model")
        label = model.get("display_name") if isinstance(model, dict) else None
        return label if isinstance(label, str) and label else "claude"
    except Exception:
        return "claude"


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        raw = sys.stdin.buffer.read()
        if raw:
            post_usage(raw, resolve_port(args, dict(os.environ)))
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        print(render_statusline(payload if isinstance(payload, dict) else {}))
    except Exception:
        print("claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
