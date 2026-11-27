"""Live launch defaults, owned by the settings page rather than account identity.

`profile-launch-settings.json` overlays model/reasoning on profiles.json for future launches.
A null field inherits the profile's arguments (then the CLI's own settings). Explicit launch
models outrank these defaults. Running processes and headless calls are untouched. Saving
replaces the sidecar atomically before publishing memory; the shared registry used by /spawn,
worktrees and restores sees the new defaults immediately, without reloading account roots or
triggering the code freshness watcher. RESET removes the override, never the profile's args.

Model ids are tokens, not a hardcoded model catalog that goes stale. Codex validates model and
reasoning compatibility when it starts. Only Codex has a reasoning control here. Unknown file
entries are kept for profiles absent from this bridge. An unreadable file blocks saves and
launches until repaired, rather than silently launching with a different model or effort.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from voidcraft_world_bridge.shape import typed

from tldrack import paths

if TYPE_CHECKING:
    from tldrack.profiles import Profile

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
KINDS = ("claude", "codex")


def clean_defaults(body: object) -> dict:
    data = typed(body, dict, {})
    if set(data) != {"model", "reasoning_effort"}:
        raise ValueError("Provide model and reasoning_effort, using null to inherit profile defaults")
    model, effort = data.get("model"), data.get("reasoning_effort")
    if model is not None and (not isinstance(model, str) or not MODEL_RE.fullmatch(model)):
        raise ValueError("Model must be an id of at most 128 letters, digits or . _ : / -")
    if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
        raise ValueError("Choose a supported reasoning effort or null for the profile default")
    return {"model": model, "reasoning_effort": effort}


def override_args(args: tuple[str, ...], kind: str, model: str | None, effort: str | None) -> tuple[str, ...]:
    """Replace only the selected defaults, including Codex -c spellings; keep every other argument."""
    kept: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        count = 1
        discard = False
        if model is not None:
            if arg in ("-m", "--model"):
                discard, count = True, 2
            elif arg.startswith("--model=") or (kind == "codex" and arg.startswith("-m") and len(arg) > 2):
                discard = True
        if kind == "codex":
            config = None
            if arg in ("-c", "--config") and index + 1 < len(args):
                config, count = args[index + 1], 2
            elif arg.startswith("--config="):
                config = arg[len("--config=") :]
            elif arg.startswith("-c") and len(arg) > 2:
                config = arg[2:].removeprefix("=")
            if config is not None:
                key = config.partition("=")[0].strip().strip("\"'")
                discard = (key == "model" and model is not None) or (key == "model_reasoning_effort" and effort is not None)
        if not discard:
            kept.extend(args[index : index + count])
        index += count
    if model is not None:
        kept.extend(("--model", model))
    if effort is not None:
        kept.extend(("-c", f'model_reasoning_effort="{effort}"'))
    return (*kept, *args[index:])


class ProfileLaunchSettings:
    def __init__(self, profiles: list[Profile], path: Path | None = None):
        self.path = path or paths.config_file("profile-launch-settings.json")
        self._profiles = {profile.id: profile for profile in profiles if profile.kind in KINDS}
        self._lock = RLock()
        self._overrides: dict[str, dict] = {}
        self.error: str | None = None
        try:
            data = typed(json.loads(self.path.read_text()), dict, {})
            if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
                raise ValueError("Invalid launch defaults")
            self._overrides = {key: clean_defaults(value) for key, value in data["profiles"].items()}
            for key, value in self._overrides.items():
                if key in self._profiles and self._profiles[key].kind != "codex" and value["reasoning_effort"] is not None:
                    raise ValueError("Reasoning effort requires Codex")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "Could not read profile-launch-settings.json. Repair that file and restart the bridge before launching."

    def apply(self, profile: Profile, *, explicit_model: bool = False) -> Profile:
        with self._lock:
            values = self._overrides.get(profile.id)
            if not values or profile.kind not in KINDS:
                return profile
            model = None if explicit_model else values["model"]
            effort = values["reasoning_effort"]
            label = " · ".join(value for value in (model or profile.model_label, effort) if value) or None
            return replace(profile, args=override_args(profile.args, profile.kind, model, effort), model_label=label)

    def view(self) -> dict:
        with self._lock:
            return {
                "profiles": [
                    {
                        "id": profile.id,
                        "label": profile.label,
                        "kind": profile.kind,
                        "profile_model_label": profile.model_label,
                        **self._overrides.get(profile.id, {"model": None, "reasoning_effort": None}),
                    }
                    for profile in self._profiles.values()
                ],
                "reasoning_efforts": list(REASONING_EFFORTS),
                "error": self.error,
            }

    def save(self, body: object) -> dict:
        data = typed(body, dict, {})
        profile_id = data.get("profile")
        if not isinstance(profile_id, str) or profile_id not in self._profiles:
            raise ValueError("Choose a Claude or Codex launch profile")
        values = clean_defaults({key: value for key, value in data.items() if key != "profile"})
        if self._profiles[profile_id].kind != "codex" and values["reasoning_effort"] is not None:
            raise ValueError("Reasoning effort requires a Codex profile")
        with self._lock:
            if self.error:
                raise ValueError(self.error)
            updated = {**self._overrides, profile_id: values}
            if all(value is None for value in values.values()):
                updated.pop(profile_id)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="profile-launch-", suffix=".tmp")
                with os.fdopen(fd, "w") as handle:
                    json.dump({"version": 1, "profiles": updated}, handle, indent=2)
                    handle.write("\n")
                os.replace(name, self.path)
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
            self._overrides = updated
            return self.view()
