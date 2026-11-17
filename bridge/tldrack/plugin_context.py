"""The context this bridge hands its plugins: the core's `PluginContext` plus what an operator set.

The core guarantees `status_port` and `log` and nothing about settings, so the two operator facts a
plugin may need ride on a host-side subclass instead of a core release. Plugins read both with
`getattr` and keep a safe default when a host lacks them; the contract is owned by
plugins/README.md § What the context carries — see the note there.

- `plugin_settings(name)`: the `plugins.<name>` object of settings.json, `{}` when unset, a fresh
  copy per call (settings.py `Settings.plugin_settings`).
- `allowed_origins`: the origins settings.json lists, in the browser's spelling. Loopback is never
  in it: every plugin already trusts loopback, and a plugin that widened its policy with this set
  must not learn to trust a hosted page the operator never named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from voidcraft_world_bridge.plugins import PluginContext

from tldrack.settings import Settings


@dataclass(frozen=True)
class RackPluginContext(PluginContext):
    allowed_origins: frozenset[str] = frozenset()
    operator_settings: Settings = field(default_factory=Settings, repr=False)

    def plugin_settings(self, name: str) -> dict[str, Any]:
        return self.operator_settings.plugin_settings(name)


def unclaimed_settings(settings: Settings, mounted: object) -> list[str]:
    """Plugin names with settings that no mounted plugin answers to: a typo, or a plugin that
    failed to load. Echoed at startup, never an error, because a plugin may be off on purpose."""
    names = set(mounted) if mounted is not None else set()
    return sorted(name for name in settings.plugins if name not in names)
