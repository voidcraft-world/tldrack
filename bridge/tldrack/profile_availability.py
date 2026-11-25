"""Reversible launch pauses, separate from profile identity and credentials.

A paused profile stays in the registry so running sessions retain their account labels and
transcript roots. Only new launches and AUTO exclude it. The sidecar is shared by clients and
survives restarts; a failed save never changes memory. Unreadable settings fail closed until
an operator explicitly resumes profiles, rather than spending an unavailable subscription.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from voidcraft_world_bridge.shape import typed

from tldrack import paths


class ProfileAvailability:
    def __init__(self, ids: list[str], path: Path | None = None):
        self.path = path or paths.config_file("profile-availability.json")
        self.ids = frozenset(ids)
        self._lock = RLock()
        self._paused: set[str] = set()
        self.error: str | None = None
        try:
            data = typed(json.loads(self.path.read_text()), dict, {})
            paused = data.get("paused")
            if not isinstance(paused, list) or any(not isinstance(item, str) for item in paused):
                raise ValueError("Invalid paused profiles")
            self._paused = set(paused) & self.ids
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self._paused = set(self.ids)
            self.error = "Could not read profile availability. Profiles are paused; resume the ones you want to use."

    def paused(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._paused

    def view(self) -> dict:
        with self._lock:
            return {"paused": sorted(self._paused), "error": self.error}

    def save(self, body: object) -> dict:
        data = typed(body, dict, {})
        profile_id, paused = data.get("profile"), data.get("paused")
        if not isinstance(profile_id, str) or profile_id not in self.ids or not isinstance(paused, bool):
            raise ValueError("Choose a known profile and a boolean paused value")
        with self._lock:
            updated = self._paused | {profile_id} if paused else self._paused - {profile_id}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="profile-availability-", suffix=".tmp")
                with os.fdopen(fd, "w") as handle:
                    json.dump({"paused": sorted(updated)}, handle)
                    handle.write("\n")
                os.replace(name, self.path)
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
            self._paused = updated
            self.error = None
            return self.view()
