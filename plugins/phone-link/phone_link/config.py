"""What the operator may set for this plugin, in `~/.config/tldrack/phone-link.json` (`$TLDRACK_PHONE_LINK_CONFIG`
names another file, for tests). A missing file is the ordinary case and says nothing.

- **`app_package`**: which app on the phone the link serves. The first source that is set decides:
  `$TLDRACK_PHONE_LINK_APP`, then the file, then `DEFAULT_APP_PACKAGE`. Env over file, as the bridge's own settings
  files read. The value is spliced into a device-side shell command (`run-as <package> …`, adb.py), so it must match
  Android's package-name grammar before anything else sees it. A value that does not is refused, named in `error`,
  and the default stands: a typo must never become a shell word on the phone. adb.py checks the shape again where it
  builds the command.
- **`tailnet`**: the tailnet door, `{"host", "port", "login"}`. Absent, which is the default, the door does not
  exist and the listener answers loopback callers only. What it is and why each field is pinned: owned by
  plugins/phone-link/README.md § The tailnet door — see the note there. Any field that is missing or of another
  shape leaves the door OFF and names the reason in `tailnet_error`: a half-read door must never be an open one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from phone_link.paths import DEFAULT_APP_PACKAGE

APP_ENV = "TLDRACK_PHONE_LINK_APP"
CONFIG_ENV = "TLDRACK_PHONE_LINK_CONFIG"
CONFIG_KEY = "app_package"

#: Android's applicationId grammar: two or more dot-joined segments, each a letter then letters, digits or `_`.
PACKAGE_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
PACKAGE_MAX = 255


def config_path(env: Mapping[str, str]) -> Path:
    override = env.get(CONFIG_ENV)
    return Path(override) if override else Path.home() / ".config" / "tldrack" / "phone-link.json"


def valid_package(value: object) -> bool:
    return isinstance(value, str) and len(value) <= PACKAGE_MAX and PACKAGE_SHAPE.fullmatch(value) is not None


TAILNET_KEY = "tailnet"
#: One machine's own name on a tailnet, lowercase, at least `machine.tailnet.ts.net`. Never a suffix or a wildcard:
#: the grammar has no `*`, and the gate compares the whole name.
TAILNET_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\.ts\.net")
TAILNET_HOST_MAX = 253
#: The only ports Tailscale Funnel serves to the public internet. The door refuses to live on one, so a `funnel`
#: typed by mistake has no listener of ours to reach.
FUNNEL_PORTS = frozenset({443, 8443, 10000})
#: A tailnet login as `tailscale serve` reports it (`Tailscale-User-Login`): no spaces, no controls.
TAILNET_LOGIN = re.compile(r"[\x21-\x7e]{1,254}")


@dataclass(frozen=True)
class TailnetDoor:
    """The one name, port and login the tailnet door answers to."""

    host: str
    port: int
    login: str

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"https://{self.authority}/"


@dataclass(frozen=True)
class LinkConfig:
    app_package: str = DEFAULT_APP_PACKAGE
    source: str = "default"  # "default" | "env" | "file"
    #: Why a configured value was refused (the default stands), or None.
    error: str | None = None
    #: The tailnet door, or None: off.
    tailnet: TailnetDoor | None = None
    #: Why a configured door was refused (it stays off), or None.
    tailnet_error: str | None = None

    def to_json(self) -> dict:
        return {"package": self.app_package, "source": self.source, "error": self.error}

    def tailnet_json(self) -> dict:
        door = self.tailnet
        return {
            "on": door is not None,
            "host": door.host if door else None,
            "port": door.port if door else None,
            "error": self.tailnet_error,
        }


def _refused(where: str, value: object) -> LinkConfig:
    shown = value if isinstance(value, str) else type(value).__name__
    return LinkConfig(error=f"{where}: {str(shown)[:80]!r} is not an Android package name; using {DEFAULT_APP_PACKAGE}")


def read_tailnet(value: object) -> tuple[TailnetDoor | None, str | None]:
    """`(door, error)` for the file's `tailnet` value. Exactly the three fields, each of its shape, or no door."""
    if not isinstance(value, dict) or set(value) != {"host", "port", "login"}:
        return None, f"{TAILNET_KEY}: expected an object with exactly host, port and login; the door stays off"
    host, port, login = value["host"], value["port"], value["login"]
    if not isinstance(host, str) or len(host) > TAILNET_HOST_MAX or not TAILNET_HOST.fullmatch(host):
        return None, f"{TAILNET_KEY}.host: not one machine's lowercase *.ts.net name; the door stays off"
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None, f"{TAILNET_KEY}.port: not a port; the door stays off"
    if port in FUNNEL_PORTS:
        return None, f"{TAILNET_KEY}.port: {port} is a port Tailscale Funnel can publish; pick another. The door stays off"
    if not isinstance(login, str) or not TAILNET_LOGIN.fullmatch(login):
        return None, f"{TAILNET_KEY}.login: not a tailnet login; the door stays off"
    return TailnetDoor(host, port, login), None


def _read_file(path: Path) -> tuple[dict | None, str | None]:
    """`(the file's object, why it could not be read)`; `(None, None)` when there is no file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name}: unreadable ({type(exc).__name__}); using {DEFAULT_APP_PACKAGE}"
    try:
        data = json.loads(text)
    except ValueError:
        return None, f"{path.name}: not JSON; using {DEFAULT_APP_PACKAGE}"
    if not isinstance(data, dict):
        return None, f"{path.name}: not a JSON object; using {DEFAULT_APP_PACKAGE}"
    return data, None


def load_config(env: Mapping[str, str] | None = None) -> LinkConfig:
    env = os.environ if env is None else env
    path = config_path(env)
    data, file_error = _read_file(path)
    door, door_error = read_tailnet(data[TAILNET_KEY]) if data is not None and TAILNET_KEY in data else (None, None)
    from_env = env.get(APP_ENV)
    if from_env:
        app = LinkConfig(from_env, "env") if valid_package(from_env) else _refused(APP_ENV, from_env)
    elif file_error is not None:
        app = LinkConfig(error=file_error)
    elif data is None or CONFIG_KEY not in data:
        app = LinkConfig()
    else:
        value = data[CONFIG_KEY]
        app = LinkConfig(value, "file") if valid_package(value) else _refused(f"{path.name} {CONFIG_KEY}", value)
    return LinkConfig(app.app_package, app.source, app.error, door, door_error)
