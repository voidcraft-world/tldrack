"""Which app on the phone the link serves: `app_package`, the one setting this plugin reads.

The first source that is set decides: `$TLDRACK_PHONE_LINK_APP`, then `app_package` in
`~/.config/tldrack/phone-link.json` (`$TLDRACK_PHONE_LINK_CONFIG` names another file, for tests), then
`DEFAULT_APP_PACKAGE`. Env over file, as the bridge's own settings files read. A missing file is the ordinary
case and says nothing.

The value is spliced into a device-side shell command (`run-as <package> …`, adb.py), so it must match Android's
package-name grammar before anything else sees it. A value that does not is refused, named in `error`, and the
default stands: a typo must never become a shell word on the phone. adb.py checks the shape again where it builds
the command.
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


@dataclass(frozen=True)
class LinkConfig:
    app_package: str = DEFAULT_APP_PACKAGE
    source: str = "default"  # "default" | "env" | "file"
    #: Why a configured value was refused (the default stands), or None.
    error: str | None = None

    def to_json(self) -> dict:
        return {"package": self.app_package, "source": self.source, "error": self.error}


def _refused(where: str, value: object) -> LinkConfig:
    shown = value if isinstance(value, str) else type(value).__name__
    return LinkConfig(error=f"{where}: {str(shown)[:80]!r} is not an Android package name; using {DEFAULT_APP_PACKAGE}")


def load_config(env: Mapping[str, str] | None = None) -> LinkConfig:
    env = os.environ if env is None else env
    from_env = env.get(APP_ENV)
    if from_env:
        return LinkConfig(from_env, "env") if valid_package(from_env) else _refused(APP_ENV, from_env)
    path = config_path(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LinkConfig()
    except (OSError, UnicodeDecodeError) as exc:
        return LinkConfig(error=f"{path.name}: unreadable ({type(exc).__name__}); using {DEFAULT_APP_PACKAGE}")
    try:
        data = json.loads(text)
    except ValueError:
        return LinkConfig(error=f"{path.name}: not JSON; using {DEFAULT_APP_PACKAGE}")
    if not isinstance(data, dict):
        return LinkConfig(error=f"{path.name}: not a JSON object; using {DEFAULT_APP_PACKAGE}")
    if CONFIG_KEY not in data:
        return LinkConfig()
    value = data[CONFIG_KEY]
    return LinkConfig(value, "file") if valid_package(value) else _refused(f"{path.name} {CONFIG_KEY}", value)
