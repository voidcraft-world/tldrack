"""Crash-safe replacement of a small JSON-ish state file.

Three stores here persist the same way — the icon picks, the model readings and
the notify config — and each had its own byte-identical copy of this. They all
share a tolerant loader that DISCARDS a file it cannot parse, which is what
makes the atomicity load-bearing rather than tidy: a half-written file is not a
recoverable error here, it is silent data loss.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str, noun: str) -> None:
    """Write `text` to `path` via a temp file and `os.replace`.

    The temp file MUST be created in the destination's own directory:
    `os.replace` is only atomic within a filesystem, and the default temp dir is
    routinely on another one. That is the detail a fourth copy of this would get
    wrong.

    A failure is reported and swallowed, never raised — every caller is holding
    a lock inside a request handler, and losing the on-disk copy is not a reason
    to fail the request that produced it. `noun` names what stays in memory.

    EVERY step is inside the `try`, `mkdir` and `mkstemp` included, and that is
    the load-bearing detail rather than a style choice. Those two are the calls
    most likely to fail — a read-only or full state dir fails THERE, not at the
    write — so leaving them above the `try` makes the "kept in memory only"
    degradation unreachable in exactly the cases it was written for. The caller
    has already mutated in-memory state under its lock, so an escaping OSError
    surfaces as a 500 for a change that did happen.

    Pre-declaring `temp_name` is the price of that: `mkstemp` can now raise
    before binding it, and the handler must not trip over its own cleanup.
    """
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except OSError as err:
        print(
            f"tldrack: could not persist {path} ({err}) — {noun} kept in memory only",
            file=sys.stderr,
        )
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
