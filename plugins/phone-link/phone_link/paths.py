"""The names both ends of the phone link agree on, and where this Mac keeps the link's token.

The contract these constants belong to is owned by plugins/phone-link/README.md — see the note there.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The link's own listener, and the ONLY port ever tunnelled to a phone. The Android app holds the same constant.
PHONE_LINK_PORT = 7683
#: `tcp:7683` on both sides of `adb reverse`: the phone's loopback port, then this Mac's.
LINK_SPEC = f"tcp:{PHONE_LINK_PORT}"

#: The bridge's own ports (ttyd, then the status listener) on a stock bridge. The plugin adds the ports the
#: running bridge was actually given; a reverse tunnel to any of them is removed on sight.
STOCK_BRIDGE_PORTS = (7681, 7682)

#: The app the token goes to when nothing else is configured: tldrack's own (`android/`), as a debug build, so
#: `run-as` reaches its private storage. The setting that overrides it: owned by phone_link/config.py — see the note there.
DEFAULT_APP_PACKAGE = "world.voidcraft.tldrack"
#: The token inside the app's private storage, relative to its data dir: `File(context.filesDir, "phone-link-token")`.
DEVICE_TOKEN_FILE = "phone-link-token"

#: This Mac's state folder for the link, under the bridge's own state root. Test-only override below.
STATE_DIR_NAME = "phone-link"
STATE_DIR_ENV = "PHONE_LINK_STATE_DIR"
TOKEN_FILE = "token"
#: Held (flock) by the one bridge on this Mac that serves the link: owned by phone_link/link_server.py.
LOCK_FILE = "link.lock"


def state_dir() -> Path:
    """`~/.local/state/tldrack/phone-link/`, or `$PHONE_LINK_STATE_DIR` in a test."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "tldrack" / STATE_DIR_NAME
