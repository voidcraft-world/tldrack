"""`settings.json`: what an operator turns on, in one file of the config role (paths.py).

A bridge with no settings file is the whole product for a stranger: loopback only, no model
calls, no pushes, nothing that names a host. Everything here is an opt-in, and every key is
optional. An unknown key or a wrong type raises `SettingsError` at startup, so a typo is a
message and not a feature that silently stays off.

    {
      "allowed_origins": ["https://rack.example"],
      "supervisor_url": "myapp://bridge/start",
      "supervisor_process": ["MyApp"],
      "ui_dist": "/abs/path/to/ui/dist",
      "session_namer": true,
      "lease_module": "/abs/path/to/lease.py",
      "decision_docs": ["docs/**/*.md"],
      "standing_missions": [
        {"key": "docs", "title": "Docs", "keeper": "Scribe",
         "brief": "Owns the documentation.", "domains": ["docs/*", "docs"]}
      ],
      "plugins": {"coordinator": {"enabled": true}}
    }

| Key | Off means | On means |
|---|---|---|
| `allowed_origins` | only loopback pages reach the bridge | those browser origins do too |
| `supervisor_url` | `tldrack up` starts the bridge itself, in tmux | it asks that URL's app first |
| `supervisor_process` | no launcher reads as a supervisor | a bridge whose parent is one says so (identity.py) |
| `ui_dist` | the UI is served from the checkout's `ui/dist` | from this directory |
| `session_namer` | a session keeps the name it was given | a small model names it from its first prompt |
| `lease_module` | live marks are not written | that file's lease functions are used |
| `decision_docs` | `decisions import` reads nothing | those globs, under `--root` |
| `standing_missions` | no mission is minted | each is minted once (missions.py) |
| `plugins` | every plugin keeps its safe defaults | each named plugin reads its own object |

`plugins` is an object of objects keyed by plugin name. This file checks that shape and nothing
deeper: each plugin owns its keys, validates them itself and documents them in its README's
§ Settings. A plugin reaches its object through the context's `plugin_settings(name)`
(plugin_context.py; the seam's contract is owned by plugins/README.md).

Two optional parts are not here, because each already has one owner: phone pushes (`notify.json`,
off until it names a topic) and the suggested next step (its stored mode, off until
`tldrack next-step --mode shadow`). `tldrack doctor` lists all of them, on or off.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from voidcraft_world_bridge.guards import canonical_origins
from voidcraft_world_bridge.plugins import PLUGIN_NAME_RE

from tldrack import paths

SETTINGS_FILE = "settings.json"
MAX_STANDING_MISSIONS = 16
MAX_DECISION_GLOBS = 32
MAX_PLUGIN_SETTINGS = 64


class SettingsError(ValueError):
    """`settings.json` cannot be read, or holds a key or a value this bridge does not take."""


@dataclass(frozen=True)
class Settings:
    #: Browser origins served besides loopback, in the browser's spelling. Empty: loopback only.
    allowed_origins: frozenset[str] = frozenset()
    supervisor_url: str | None = None
    #: Process names of the operator's supervisor app, for `tldrack status` (identity.py).
    supervisor_process: frozenset[str] = frozenset()
    ui_dist: Path | None = None
    session_namer: bool = False
    lease_module: Path | None = None
    decision_docs: tuple[str, ...] = ()
    #: Raw seed objects; missions.py owns their shape and checks them (`standing_seeds`).
    standing_missions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: Plugin name → that plugin's raw object, read-only. Read it through `plugin_settings`.
    plugins: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))

    def plugin_settings(self, name: str) -> dict[str, Any]:
        """`plugins.<name>`, `{}` when unset: a fresh deep copy on every call, so no plugin can
        change what another reads, or what it reads itself the next time."""
        return copy.deepcopy(dict(self.plugins.get(name, {})))

    def enabled(self) -> dict[str, bool]:
        """The optional parts and whether each is on, for `doctor` and the startup lines."""
        return {
            "session namer": self.session_namer,
            "live marks": self.lease_module is not None,
            "supervisor": self.supervisor_url is not None,
            "extra origins": bool(self.allowed_origins),
        }


def settings_path() -> Path:
    return paths.config_file(SETTINGS_FILE)


def _abs_path(value: object, key: str, source: Path, *, want_dir: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{source}: {key} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"{source}: {key} must be an absolute path: {value}")
    if want_dir and not path.is_dir():
        raise SettingsError(f"{source}: {key} is not a directory: {value}")
    if not want_dir and not path.is_file():
        raise SettingsError(f"{source}: {key} is not a file: {value}")
    return path


def _strings(value: object, key: str, source: Path, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(item, str) and item.strip() for item in value):
        raise SettingsError(f"{source}: {key} must be a list of at most {limit} non-empty strings")
    return tuple(item.strip() for item in value)


def parse_settings(body: object, source: Path) -> Settings:
    """`settings.json`'s parsed body → `Settings`. Pure apart from the path checks."""
    if not isinstance(body, dict):
        raise SettingsError(f"{source}: expected an object")
    known = set(Settings.__dataclass_fields__)
    unknown = sorted(set(body) - known)
    if unknown:
        raise SettingsError(f"{source}: unknown key {unknown[0]!r} (known: {', '.join(sorted(known))})")
    out: dict[str, Any] = {}
    if "allowed_origins" in body:
        try:
            out["allowed_origins"] = canonical_origins(_strings(body["allowed_origins"], "allowed_origins", source, 16))
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{source}: allowed_origins: {exc}") from exc
    if body.get("supervisor_url") is not None:
        url = body["supervisor_url"]
        if not isinstance(url, str) or "://" not in url or any(ch.isspace() for ch in url):
            raise SettingsError(f"{source}: supervisor_url must be a URL, such as myapp://bridge/start")
        out["supervisor_url"] = url
    if "supervisor_process" in body:
        out["supervisor_process"] = frozenset(_strings(body["supervisor_process"], "supervisor_process", source, 8))
    if body.get("ui_dist") is not None:
        out["ui_dist"] = _abs_path(body["ui_dist"], "ui_dist", source, want_dir=True)
    if "session_namer" in body:
        if not isinstance(body["session_namer"], bool):
            raise SettingsError(f"{source}: session_namer must be true or false")
        out["session_namer"] = body["session_namer"]
    if body.get("lease_module") is not None:
        out["lease_module"] = _abs_path(body["lease_module"], "lease_module", source, want_dir=False)
    if "decision_docs" in body:
        out["decision_docs"] = _strings(body["decision_docs"], "decision_docs", source, MAX_DECISION_GLOBS)
    if "standing_missions" in body:
        seeds = body["standing_missions"]
        if not isinstance(seeds, list) or len(seeds) > MAX_STANDING_MISSIONS or not all(isinstance(seed, dict) for seed in seeds):
            raise SettingsError(f"{source}: standing_missions must be a list of at most {MAX_STANDING_MISSIONS} objects")
        out["standing_missions"] = tuple(seeds)
    if "plugins" in body:
        out["plugins"] = _plugin_objects(body["plugins"], source)
    return Settings(**out)


def _plugin_objects(value: object, source: Path) -> Mapping[str, Mapping[str, Any]]:
    """`plugins`: the shape only. A name that no plugin could have is a typo, so it is an error;
    a well-formed name whose plugin does not mount is only echoed at startup (main.py)."""
    if not isinstance(value, dict) or len(value) > MAX_PLUGIN_SETTINGS:
        raise SettingsError(f"{source}: plugins must be an object of at most {MAX_PLUGIN_SETTINGS} objects, keyed by plugin name")
    out: dict[str, Mapping[str, Any]] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or PLUGIN_NAME_RE.fullmatch(name) is None:
            raise SettingsError(f"{source}: plugins: {name!r} is not a plugin name (lowercase letters, digits and dashes)")
        if not isinstance(entry, dict):
            raise SettingsError(f"{source}: plugins.{name} must be an object, got {type(entry).__name__}")
        out[name] = MappingProxyType(copy.deepcopy(entry))
    return MappingProxyType(out)


def load_settings(path: Path | None = None) -> Settings:
    """The operator's settings; a missing file is every default. Raises `SettingsError`."""
    source = path or settings_path()
    try:
        body = json.loads(source.read_text())
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        raise SettingsError(f"cannot read {source}: {exc}") from exc
    return parse_settings(body, source)
