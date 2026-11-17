#!/usr/bin/env python3
"""Register a generated deliverable with the current agent's Canvas shelf."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--session", help="tmux session name; defaults to this pane")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TLDRACK_STATUS_PORT", "7682")))
    args = parser.parse_args()
    try:
        session = args.session
        if not session:
            if not os.environ.get("TMUX_PANE"):
                raise ValueError("no tmux pane; pass --session explicitly")
            session = subprocess.check_output(
                ["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"], "#S"], text=True, timeout=4
            ).strip()
        payload = json.dumps({"session": session, "path": str(args.path.absolute())}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/plugin/agent-canvas/artifacts/register",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Loopback registration must not travel through shell proxy settings.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            result = json.load(response)
        print(f"Artifact ready in {session}: {result['artifact']['name']}")
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Artifact registration failed ({exc.code}): {exc.read().decode()}", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Artifact registration failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
