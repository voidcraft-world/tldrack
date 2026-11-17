"""The resource lease readout for this machine's row: who holds each shared resource, and how much.

A resource lease is the operator's own: a scheme where jobs take tickets for a slot (a GPU's
memory budget, a subscription login, the CPU) before they run. This plugin has no lease of its
own and never guesses at one. The operator names a Python file in the `lease` setting (README
§ Settings) whose `status()` returns the readout, shaped as `schemas/src/resource-lease.ts`
`LeaseStatusSchema`; the plugin loads it by path, once, and calls `status()` on the sampler's
thread. Without the setting nothing here runs and the row grows no `lease` key.

Loading a file the operator named runs it inside the bridge, the same trust the operator already
gives every plugin directory in plugins.json. A file that fails to load, or has no callable
`status`, costs the readout and never the plugin.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path


def module_name(path: Path) -> str:
    """One module name per file, so two configured paths can never shadow each other."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"devices_plugin_lease_{digest}"


def load_lease(path: Path) -> tuple[Callable[[], dict] | None, str | None]:
    """`(status, None)` from the module at `path`, or `(None, why)`.

    The module is registered in `sys.modules` while it executes, because a module that defines a
    dataclass looks itself up there. A failed load takes the registration back out.
    """
    name = module_name(path)
    module = sys.modules.get(name)
    if module is None:
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                return None, f"{path} is not loadable as a Python module"
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except Exception as error:  # noqa: BLE001 — a broken lease module costs the readout, never the plugin
            sys.modules.pop(name, None)
            return None, f"{path} failed to load: {type(error).__name__}: {error}"[:300]
    status = getattr(module, "status", None)
    if not callable(status):
        return None, f"{path} defines no callable status()"
    return status, None
