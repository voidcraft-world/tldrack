"""The resource lease a lab command takes before it loads a model: optional, and absent by default.

A lab command loads a 1.6 GB checkpoint per router target, a fine-tune takes several times that, and a local teacher
is a 20B Ollama model. On a machine that runs other heavy jobs (another eval, a second agent's model), a LEASE is how
they take turns: a slot with a capacity, one ticket per holder, a refusal when there is no room.

tldrack ships no lease, so this module is both the seam and the answer when there is none:

- **No lease configured (the default): the lab just runs.** Every take succeeds at once, nothing is asked to yield,
  the night window is always open, and a run has no deadline. The names below are that answer.
- **`TLDRACK_LEASE_MODULE=/abs/path/to/lease.py`**: the operator's own lease. The file is loaded once, by path, and
  every lab command takes its slots from it. A path that does not load stops the command: a lease the operator
  configured is never silently skipped.

THE PROTOCOL is the public names of this module, and a configured module provides every one of them:

    LEASE_SCHEDULED_ENV   the variable a scheduler sets to "1" on a command it started (a scheduled command may queue
                          for its slot and steps aside for live work; a hand-started one never waits)
    WAIT_POLL_S           seconds between two looks while a scheduled command is asked to yield
    OLLAMA_URL_ENV, DEFAULT_OLLAMA_URL
                          where the local teacher's Ollama answers (the lease sizes its models at the same URL)
    GIB, OLLAMA_RESIDENT_FACTOR, ROUTER_TARGET_GB, router_gb(targets)
                          what a claim is sized by
    take(slot, units, holder, *, kind, models, wait_s) -> held
                          `held.reentered` (the process already held the slot), `held.release()`,
                          `held.should_yield()` -> None or `(code, reason)`. Raises `LeaseRefused` when there is no room.
    LeaseRefused          the refusal; `str()` of it names who holds the slot
    night_window_open(now=None) -> bool
                          may scheduled gpu work start now (`now`: epoch seconds)
    run_minutes_left() -> float | None
                          minutes until whatever started this run stops it; None when nothing will

`infer.py` is the one caller of `take`: `hold_slot`, `hold_device` and `yield_point` are what every command uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

#: An absolute path to the operator's lease module. Unset: no lease.
MODULE_ENV = "TLDRACK_LEASE_MODULE"
#: `sys.modules` name of the configured module (dataclasses resolve their annotations through it).
CONFIGURED_NAME = "laya_lab_configured_lease"

LEASE_SCHEDULED_ENV = "TLDRACK_LEASE_SCHEDULED"
WAIT_POLL_S = 2.0
OLLAMA_URL_ENV = "TLDRACK_OLLAMA_URL"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
GIB = 1024**3
#: A loaded Ollama model takes about this much more than its listed size.
OLLAMA_RESIDENT_FACTOR = 1.15
#: One Laya checkpoint in memory.
ROUTER_TARGET_GB = 1.6


class LeaseRefused(Exception):
    """A slot had no room. Never raised without a configured lease."""


class NoLease:
    """What `take` hands back when no lease is configured: nothing is held, so nothing is released or yielded."""

    reentered = False

    def release(self) -> bool:
        return False

    def should_yield(self) -> tuple[str, str] | None:
        return None


def router_gb(targets: int) -> float:
    return round(max(1, targets) * ROUTER_TARGET_GB, 1)


_NO_LEASE = NoLease()


def take(slot: str, units: float, holder: str, *, kind: str, models: Sequence[str] = (), wait_s: float = 0.0) -> NoLease:
    return _NO_LEASE


def night_window_open(now: float | None = None) -> bool:
    return True


def run_minutes_left() -> float | None:
    return None


def configured_path(env: Mapping[str, str] = os.environ) -> Path | None:
    raw = env.get(MODULE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def module(env: Mapping[str, str] = os.environ) -> ModuleType:
    """The lease in force: the operator's module when `MODULE_ENV` names one, else this module (no lease)."""
    path = configured_path(env)
    if path is None:
        return sys.modules[__name__]
    held = sys.modules.get(CONFIGURED_NAME)
    if held is not None and getattr(held, "__file__", None) == str(path):
        return held
    if not path.is_absolute() or not path.is_file():
        raise SystemExit(f"[laya-lab] {MODULE_ENV} does not name a file by an absolute path: unset it to run without a lease")
    spec = importlib.util.spec_from_file_location(CONFIGURED_NAME, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[laya-lab] {MODULE_ENV} is not a Python module: unset it to run without a lease")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[CONFIGURED_NAME] = loaded
    try:
        spec.loader.exec_module(loaded)
    except Exception as error:  # noqa: BLE001 (whatever the operator's module raises, the command stops here)
        sys.modules.pop(CONFIGURED_NAME, None)
        raise SystemExit(
            f"[laya-lab] {MODULE_ENV} did not load ({type(error).__name__}): unset it to run without a lease"
        ) from None
    return loaded
