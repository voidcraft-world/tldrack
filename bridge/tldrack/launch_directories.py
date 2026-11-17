"""Saved launch directories, edited in the rack's Settings.

`<config>/launch-directories.json` owns the list and its default across clients and restarts.
The bridge's startup directory seeds a missing file. An explicit launch is pinned to a saved
path; deleting or moving that directory refuses the launch instead of choosing another project.
Worktrees and LEAD still use their own repository configuration, never this default.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from voidcraft_world_bridge.shape import typed

from tldrack import paths


def directory_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Directory must be a non-empty absolute path")
    try:
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("Directory must be absolute (or start with ~/)")
        return str(path.resolve())
    except (OSError, RuntimeError) as exc:
        raise ValueError("Could not resolve that directory") from exc


def parse_directories(body: object) -> dict:
    data: dict = typed(body, dict, {})
    rows = data.get("directories")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("Keep between 1 and 16 launch directories")
    entries = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each directory needs a label and path")
        path = directory_path(row.get("path"))
        label = row.get("label")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label.strip()) > 40
            or any(ord(c) < 32 or ord(c) == 127 for c in label)
        ):
            raise ValueError("Directory labels must be 1–40 characters on one line")
        if path in seen:
            raise ValueError("That directory is already in the list")
        seen.add(path)
        entries.append({"label": label.strip(), "path": path})
    default = directory_path(data.get("default_path"))
    if default not in seen:
        raise ValueError("Choose a default from the saved directories")
    return {"directories": entries, "default_path": default}


class LaunchDirectories:
    def __init__(self, start_dir: str | None, path: Path | None = None):
        self.path = path or paths.config_file("launch-directories.json")
        self.bridge_path = directory_path(start_dir or str(Path.home()))
        self._lock = RLock()

    def _read(self) -> dict:
        try:
            return parse_directories(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return {
                "directories": [{"label": Path(self.bridge_path).name or self.bridge_path, "path": self.bridge_path}],
                "default_path": self.bridge_path,
            }
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read launch directories: {exc}") from exc

    def view(self) -> dict:
        with self._lock:
            return {**self._read(), "bridge_path": self.bridge_path}

    def save(self, body: object) -> dict:
        value = parse_directories(body)
        for row in value["directories"]:
            if not Path(row["path"]).is_dir():
                raise ValueError(f"Not a directory: {row['path']}")
        # Unlike transient session state, Settings may say Saved only after the disk write lands.
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="launch-directories-", suffix=".tmp")
                with os.fdopen(fd, "w") as handle:
                    json.dump(value, handle)
                    handle.write("\n")
                os.replace(name, self.path)
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
            return {**value, "bridge_path": self.bridge_path}

    def resolve(self, requested: object = None) -> str:
        with self._lock:
            state = self._read()
            selected = state["default_path"] if requested is None else directory_path(requested)
            if not any(row["path"] == selected for row in state["directories"]):
                raise ValueError("Launch directory is no longer saved; choose it again in Settings")
            if not Path(selected).is_dir():
                raise ValueError(f"Launch directory is missing: {selected}")
            return selected
