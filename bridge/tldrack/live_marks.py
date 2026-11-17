"""Live work's marks on a machine-wide resource lease: a rack verdict or a next-step chip ran just now.

Some operators run scheduled heavy work on the same machine as the rack (a night eval, a
fine-tune) and arbitrate the GPU with a lease of their own. Live work never takes a ticket there:
it marks, and the scheduled work steps aside while a mark is fresh.

The lease is the operator's code, not this package's. `settings.json` `lease_module` names one
Python file; it is loaded by path, once, and asked for `mark_live(name)`. With no such setting
nothing is loaded and a mark is a no-op, which is the default.

A mark never raises and never waits: live work is never slowed or stopped by the lease.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

LAYA_DECIDE = "laya-decide"
NEXT_STEP = "next-step"
MODULE_NAME = "tldrack_lease"

_lock = threading.Lock()
_configured: list[Path | None] = [None]
_loaded: list[ModuleType | None] = []


def configure(lease_module: Path | None) -> None:
    """Name the lease file (or None for no lease). Called once at startup, before any mark."""
    with _lock:
        _configured[0] = lease_module
        _loaded.clear()
        sys.modules.pop(MODULE_NAME, None)


def _lease() -> ModuleType | None:
    with _lock:
        if not _loaded:
            _loaded.append(_load(_configured[0]))
        return _loaded[0]


def _load(path: Path | None) -> ModuleType | None:
    if path is None or not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
        sys.modules[MODULE_NAME] = module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — a broken lease module must never take a live path down
        sys.modules.pop(MODULE_NAME, None)
        return None
    return module if callable(getattr(module, "mark_live", None)) else None


def mark(name: str) -> None:
    """`name` (LAYA_DECIDE, NEXT_STEP) ran now."""
    try:
        lease = _lease()
        if lease is not None:
            lease.mark_live(name)
    except Exception:  # noqa: BLE001 — see the module docstring
        pass
