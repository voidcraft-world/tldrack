"""Browser clipboard images become private local files, never terminal bytes.

The browser owns the clipboard (including on a remote viewer). The PTY only
accepts text, so the page uploads the bytes here, then pastes the returned path
into the draft. No tmux input or Enter is sent by this route. Files stay in the
OS temporary directory so an agent can read them after the page navigates away.
"""

from __future__ import annotations

import base64
import binascii
import os
import shlex
import tempfile
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BODY_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 1024


def image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Use a PNG, JPEG, GIF or WebP image")


def save_image(payload: dict) -> dict:
    encoded = payload.get("data")
    if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BODY_BYTES - 1024:
        raise ValueError("Image must be base64 data, at most 8 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid image encoding") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or exceeds 8 MB")
    extension = image_extension(data)
    fd, name = tempfile.mkstemp(prefix="tldrack-image-", suffix=extension)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    # The browser uses xterm.paste, not input, so bracketed-paste mode and its
    # control-character protection apply. Quoting also keeps shell drafts inert.
    return {"path": name, "paste": shlex.quote(name) + " "}
