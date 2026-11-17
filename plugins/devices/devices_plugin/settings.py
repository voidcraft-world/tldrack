"""The operator's settings for this plugin: `plugins.devices` of settings.json, read once at load.

The seam and its rules: owned by plugins/README.md — see the note there. What each key means is
this plugin's README § Settings, and only there; this module validates them. A key that fails
validation is logged and left at its default, and every default is the one that does less: off,
no lease readout, no jobs stream, no extra daemons.

While the plugin is off, nothing past `enabled` is read: an off plugin resolves no command and
touches no file.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "devices"
ENABLE_HINT = f"set plugins.{PLUGIN_NAME}.enabled in settings.json"

#: The placeholders a `jobs` command may carry: the host's hosts.json name, and the sample window.
HOST_PLACEHOLDER = "{host}"
SECONDS_PLACEHOLDER = "{seconds}"
#: Bounds on what a settings list may hold. A config file is the operator's, but a typo that
#: pastes a whole file into a list should not become a thousand-argument command.
MAX_JOBS_ARGS = 32
MAX_DAEMONS = 32
MAX_ITEM_CHARS = 512

Log = Callable[[str], None]


@dataclass(frozen=True)
class DevicesSettings:
    enabled: bool = False
    #: A Python file whose `status()` returns the resource lease (README § Settings, `lease`).
    lease: Path | None = None
    #: The jobs command, its first word resolved to an absolute path, placeholders intact.
    jobs: tuple[str, ...] | None = None
    #: Command-line substrings of processes launchd adopts on purpose (`sessions.py`).
    daemons: tuple[str, ...] = ()


def read_settings(context: object, log: Log) -> DevicesSettings:
    """The validated settings for `context`'s host. A host without the seam, a reader that
    raises, or a value that is not an object all read as every key at its default."""
    reader = getattr(context, "plugin_settings", None)
    raw: object = {}
    if callable(reader):
        try:
            raw = reader(PLUGIN_NAME)
        except Exception:  # noqa: BLE001 — a broken seam costs the settings, never the plugin
            raw = {}
    return parse_settings(raw if isinstance(raw, Mapping) else {}, log)


def parse_settings(raw: Mapping, log: Log) -> DevicesSettings:
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        _reject(log, "enabled", "must be true or false; staying off")
        enabled = False
    if not enabled:
        return DevicesSettings()
    return DevicesSettings(
        enabled=True,
        lease=_lease(raw.get("lease"), log) if "lease" in raw else None,
        jobs=_jobs(raw.get("jobs"), log) if "jobs" in raw else None,
        daemons=_daemons(raw.get("daemons"), log) if "daemons" in raw else (),
    )


def _reject(log: Log, key: str, why: str) -> None:
    log(f"  {PLUGIN_NAME}: plugins.{PLUGIN_NAME}.{key} {why}")


def _strings(value: object, limit: int) -> tuple[str, ...] | None:
    """A list of non-empty strings within the bounds, or None."""
    if not isinstance(value, list) or not value or len(value) > limit:
        return None
    if not all(isinstance(item, str) and item and len(item) <= MAX_ITEM_CHARS for item in value):
        return None
    return tuple(value)


def _lease(value: object, log: Log) -> Path | None:
    """An absolute path (after `~`) to an existing `.py` file. The bridge's working directory is
    wherever it was started from, so a relative path would name a different file each time."""
    if not isinstance(value, str) or not value.strip():
        _reject(log, "lease", "must be a path to a .py file; no lease readout")
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        _reject(log, "lease", f"must be an absolute path (or start with ~), not {value!r}; no lease readout")
        return None
    if path.suffix != ".py" or not path.is_file():
        _reject(log, "lease", f"names no .py file at {path}; no lease readout")
        return None
    return path


def _jobs(value: object, log: Log) -> tuple[str, ...] | None:
    """The command as a list of words, one of them carrying `{host}`, its first word an
    executable. It is run directly, never through a shell, so a word is never re-split."""
    words = _strings(value, MAX_JOBS_ARGS)
    if words is None:
        _reject(log, "jobs", f"must be a list of 1–{MAX_JOBS_ARGS} non-empty strings; no jobs readout")
        return None
    if not any(HOST_PLACEHOLDER in word for word in words):
        _reject(log, "jobs", f"must name the host with {HOST_PLACEHOLDER} in one of its words; no jobs readout")
        return None
    program = _resolve(words[0])
    if program is None:
        _reject(
            log,
            "jobs",
            f"runs {words[0]!r}, which is not an executable on the bridge's PATH; give its absolute path. No jobs readout",
        )
        return None
    return (program, *words[1:])


def _resolve(program: str) -> str | None:
    """An absolute path to `program`: as given when it names a path, else found on PATH. A bridge
    started from a desktop launcher often has a shorter PATH than a login shell."""
    expanded = os.path.expanduser(program)
    if os.sep in expanded:
        path = Path(expanded)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(expanded)
    return os.path.abspath(found) if found else None


def _daemons(value: object, log: Log) -> tuple[str, ...]:
    words = _strings(value, MAX_DAEMONS)
    if words is None:
        _reject(log, "daemons", f"must be a list of 1–{MAX_DAEMONS} non-empty strings; ignored")
        return ()
    return words
