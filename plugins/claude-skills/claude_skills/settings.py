"""The operator's settings for this plugin, read once when the plugin is built.

The seam is the host's (owned by plugins/README.md — see the note there): `plugin_settings(name)`
returns the `plugins.<name>` object of the bridge's settings.json. A host without it, a call that
fails, or a value that is not an object all read as no settings, so every key keeps its safe
default. The keys themselves are documented in this plugin's README, § Settings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


def read_settings(context: object, name: str, log: Callable[[str], None]) -> Mapping[str, object]:
    reader = getattr(context, "plugin_settings", None)
    if not callable(reader):
        return {}
    try:
        settings = reader(name)
    except Exception as exc:  # noqa: BLE001 — a host bug must not cost the plugin its routes
        log(f"  claude-skills: settings unreadable ({type(exc).__name__}); using the defaults")
        return {}
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        log(f"  claude-skills: ignored settings — expected an object, got {type(settings).__name__}")
        return {}
    return settings
