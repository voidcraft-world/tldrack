"""Which plugin directories the bridge mounts, and where that list came from.

ORDER. `TLDRACK_PLUGINS` (colon-separated dirs) wins. Then `<config>/plugins.json`
(`{"version": 1, "plugins": [dirs]}`): a file that EXISTS wins outright, even
`{"plugins": []}` or one the core cannot parse, because writing it is the operator's choice.
Neither present: the checkout's default-on plugins (`DEFAULT_PLUGINS`), so a fresh clone
mounts worktree controls and Laya verdicts without a config file. An installed wheel has no
checkout beside it, so it mounts none, as before.

The env var and the file are read by the core's `discover_plugin_dirs`; this module only adds
the third source and names which one answered (`tldrack doctor`, the startup echo).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from voidcraft_world_bridge.plugins import ENTRY_MODULE, discover_plugin_dirs

# This bridge's own names, so the core's plugin variable never reaches it.
PLUGINS_ENV = "TLDRACK_PLUGINS"
PLUGINS_FILE = "plugins.json"

#: The plugins on by default, in mount order. `plugins/README.md` lists each one's default;
#: coordinator and devices are off. A test holds every name to a `plugins/<name>/` in the tree.
DEFAULT_PLUGINS = ("worktree", "agent-canvas", "claude-skills", "laya", "phone-link")

ENV, FILE, DEFAULTS, NONE = "env", "plugins.json", "checkout defaults", "none"


def checkout_plugins_dir() -> Path:
    """`plugins/` of the checkout this package runs from (it may not exist: an installed wheel).
    Found the way `ui_static.checkout_ui_dir` finds `ui/`."""
    return Path(__file__).resolve().parents[2] / "plugins"


def default_plugin_dirs(root: Path | None = None) -> list[Path]:
    """Each default-on plugin whose entry module is in the checkout, in `DEFAULT_PLUGINS` order."""
    root = checkout_plugins_dir() if root is None else root
    return [root / name for name in DEFAULT_PLUGINS if (root / name / ENTRY_MODULE).is_file()]


@dataclass(frozen=True)
class PluginSource:
    kind: str  # ENV | FILE | DEFAULTS | NONE
    dirs: list[Path]
    where: str = ""  # the file or directory the list was read from

    def describe(self) -> str:
        if self.kind == ENV:
            return f"env {PLUGINS_ENV}"
        if self.kind == FILE:
            return f"plugins.json ({self.where})"
        if self.kind == DEFAULTS:
            return f"checkout defaults ({self.where})"
        return "none: no plugins.json and no checkout beside this package"


def resolve_plugin_source(env: Mapping[str, str], config_path: Path, checkout: Path | None = None) -> PluginSource:
    """The dirs to mount and which source named them. Never raises: a corrupt `plugins.json`
    is still the operator's file, and mounts nothing (the core's rule)."""
    if env.get(PLUGINS_ENV):
        return PluginSource(ENV, discover_plugin_dirs(env, config_path, env_var=PLUGINS_ENV))
    if config_path.exists():
        return PluginSource(FILE, discover_plugin_dirs({}, config_path, env_var=PLUGINS_ENV), str(config_path))
    root = checkout_plugins_dir() if checkout is None else checkout
    dirs = default_plugin_dirs(root)
    return PluginSource(DEFAULTS, dirs, str(root)) if dirs else PluginSource(NONE, [])
