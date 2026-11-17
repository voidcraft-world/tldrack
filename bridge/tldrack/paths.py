"""Where tldrack keeps its files: one resolver, three roles.

| Role | Default | Holds |
|---|---|---|
| `config` | `~/.config/tldrack` | what the operator writes: profiles, plugins, settings, notify |
| `state` | `~/.local/state/tldrack` | what the bridge writes and could lose: session stores, logs |
| `data` | `~/.local/share/tldrack` | what must be kept: the ledger, decisions, the rack database |

A STORE is a named directory of kept data (`STORES`). Each one defaults to a folder under the data
role, and may be mapped on its own: a ledger on a bigger disk, decisions in a synced folder.

WHY ONE RESOLVER. Every store used to work out its own path, and the kept ones stayed off until
an environment variable named a parent directory. A bridge started without that variable (from a
launcher, from a login item) quietly lost its inbox and its ledger. Now the data role always has an
answer, so those routes never refuse for want of a directory.

THE ORDER, for a role: its environment variable (`ROLE_ENV`), then `paths.json`, then the default.
For a store: `paths.json` `stores`, then `<data>/<its folder>`. `paths.json` is read from the config
role's own default (or its environment variable), so the file that can move the config role is
never itself moved by it.

    {"data": "/Volumes/big/tldrack", "stores": {"ledger": "/Volumes/archive/agent-ledger"}}

A mapped directory must be absolute and must exist: a typo fails at startup (`PathsError`) instead
of starting an empty store beside the real one. A default is created on first use, private to the
user. Nothing is ever moved or migrated.

`export_env` writes the three roles into the process environment once, at CLI entry, so a plugin
or a child process asks the environment and gets the same answer without importing this module.
It also pins which `paths.json` was read (`PATHS_FILE_ENV`), because the stores are mapped there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROLES = ("config", "state", "data")
ROLE_ENV = {"config": "TLDRACK_CONFIG_DIR", "state": "TLDRACK_STATE_DIR", "data": "TLDRACK_DATA_DIR"}
_ROLE_DEFAULT = {
    "config": (".config", "tldrack"),
    "state": (".local", "state", "tldrack"),
    "data": (".local", "share", "tldrack"),
}
PATHS_FILE = "paths.json"
#: Set by `export_env` to the file it read, so a process that inherits the exported roles still
#: reads the same mapping (the stores are in it) and not one beside the moved config role.
PATHS_FILE_ENV = "TLDRACK_PATHS_FILE"
#: store name → its folder under the data role. The folder names are the ones older installs
#: used under their own data directory, so mapping `data` there adopts all of them at once.
STORES = {"ledger": "agent-ledger", "decisions": "decisions", "rack": "agent-rack", "laya": "laya"}
PRIVATE_MODE = 0o700


class PathsError(ValueError):
    """`paths.json` or a role's environment variable names something unusable."""


def _default(role: str) -> Path:
    return Path.home().joinpath(*_ROLE_DEFAULT[role])


def paths_file() -> Path:
    """Where `paths.json` is read from: never moved by its own `config` entry."""
    pinned = os.environ.get(PATHS_FILE_ENV)
    if pinned:
        return Path(pinned)
    env = os.environ.get(ROLE_ENV["config"])
    return (Path(env).expanduser() if env else _default("config")) / PATHS_FILE


def _mapped(value: object, what: str, source: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PathsError(f"{source}: {what} must name an existing absolute directory")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise PathsError(f"{source}: {what} is not an existing absolute directory: {value}")
    return path


def read_mapping() -> tuple[dict[str, Path], dict[str, Path]]:
    """`(roles, stores)` as `paths.json` maps them. A missing file maps nothing; a file that
    cannot be read, or names an unknown role or store, raises `PathsError`."""
    source = paths_file()
    try:
        body = json.loads(source.read_text())
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as exc:
        raise PathsError(f"cannot read {source}: {exc}") from exc
    if not isinstance(body, dict):
        raise PathsError(f"{source}: expected an object")
    unknown = sorted(set(body) - set(ROLES) - {"stores"})
    if unknown:
        raise PathsError(f'{source}: unknown key {unknown[0]!r} (roles: {", ".join(ROLES)}; or "stores")')
    roles = {role: _mapped(body[role], role, source) for role in ROLES if role in body}
    raw_stores = body.get("stores", {})
    if not isinstance(raw_stores, dict):
        raise PathsError(f"{source}: stores must be an object")
    unknown = sorted(set(raw_stores) - set(STORES))
    if unknown:
        raise PathsError(f"{source}: unknown store {unknown[0]!r} (stores: {', '.join(sorted(STORES))})")
    stores = {name: _mapped(value, f"stores.{name}", source) for name, value in raw_stores.items()}
    return roles, stores


def _made(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODE)
    except OSError as exc:
        raise PathsError(f"cannot create {path}: {exc}") from exc
    return path


def role_dir(role: str, *, create: bool = True) -> Path:
    """The directory of one role, created unless `create` is false. The one exception is a
    directory `paths.json` names: that must already exist (`_mapped`)."""
    if role not in ROLES:
        raise KeyError(role)
    env = os.environ.get(ROLE_ENV[role])
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            raise PathsError(f"{ROLE_ENV[role]} is not an absolute path: {env}")
        return _made(path) if create else path
    mapped = read_mapping()[0].get(role)
    if mapped is not None:
        return mapped
    return _made(_default(role)) if create else _default(role)


def config_dir(*, create: bool = True) -> Path:
    return role_dir("config", create=create)


def state_dir(*, create: bool = True) -> Path:
    return role_dir("state", create=create)


def data_dir(*, create: bool = True) -> Path:
    return role_dir("data", create=create)


def config_file(name: str) -> Path:
    """A file of the config role. Reading never creates the directory: an absent file is the
    common case, and a stranger's first run should leave no empty folders behind it."""
    return config_dir(create=False) / name


def state_file(name: str) -> Path:
    return state_dir(create=False) / name


def store_dir(name: str, *, create: bool = True) -> Path:
    """The directory of one named store: its own mapping, else its folder under the data role."""
    if name not in STORES:
        raise KeyError(name)
    mapped = read_mapping()[1].get(name)
    if mapped is not None:
        return mapped
    path = data_dir(create=create) / STORES[name]
    return _made(path) if create else path


def export_env() -> dict[str, str]:
    """Resolve the three roles once and write them into the environment. Returns what it set.
    Raises `PathsError` when a mapping is unusable, which is the startup check."""
    resolved = {PATHS_FILE_ENV: str(paths_file()), **{ROLE_ENV[role]: str(role_dir(role)) for role in ROLES}}
    for name in STORES:
        store_dir(name, create=False)  # a bad `stores` entry fails here, not on first use
    os.environ.update(resolved)
    return resolved


def describe() -> dict[str, object]:
    """What `doctor` prints: each role and store, where it is, and who decided."""
    try:
        roles, stores = read_mapping()
    except PathsError as exc:
        return {"error": str(exc)}
    out: dict[str, object] = {}
    for role in ROLES:
        source = "env" if os.environ.get(ROLE_ENV[role]) else "paths.json" if role in roles else "default"
        out[role] = {"dir": str(role_dir(role, create=False)), "source": source}
    out["stores"] = {
        name: {"dir": str(store_dir(name, create=False)), "source": "paths.json" if name in stores else "data"}
        for name in sorted(STORES)
    }
    return out
