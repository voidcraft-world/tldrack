"""Laya model configuration and installation; lifecycle is owned by the public bridge SDK."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from voidcraft_world_bridge.services import ManagedService, tail

from laya_bridge import install as installer

from laya_bridge.paths import IDLE_STOP_SECONDS, RUNTIME_DIR, laya_local_model_id, read_registry_view, serve_bin, thresholds_view

LOOPBACK = "127.0.0.1"
#: The public checkpoint laya-serve loads under the name `english`: what a first run serves, and what every
#: fine-tune here starts from.
BASE_MODEL = "convaiinnovations/laya"
DEFAULT_IDLE_STOP_SECONDS = IDLE_STOP_SECONDS
#: A cold first start downloads the base checkpoint from the Hub when `install` did not already (cli.py); later
#: starts load from disk in seconds.
START_TIMEOUT_SECONDS = 15 * 60
DECIDE_TIMEOUT_SECONDS = 15


STATES = ("not_installed", "installing", "stopped", "starting", "ready", "failed")


class LayaRuntime(ManagedService):
    def __init__(
        self,
        runtime_dir: Path = RUNTIME_DIR,
        extra_models: Callable[[], dict[str, str]] = dict,
        idle_stop_seconds: float = DEFAULT_IDLE_STOP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda message: None,
        registry: Callable[[], dict] = read_registry_view,
        first_fine_tune: Callable[[], dict[str, Any]] = dict,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._extra_models = extra_models
        self._registry = registry
        self._first_fine_tune = first_fine_tune
        self._served_models: dict[str, str] = {}
        self._spawns = 0
        super().__init__(
            lambda port: [str(serve_bin(self._runtime_dir))],
            environment=self._service_environment,
            log_path=self._serve_log,
            label="laya-serve",
            cwd=runtime_dir,
            idle_stop_seconds=idle_stop_seconds,
            start_timeout_seconds=START_TIMEOUT_SECONDS,
            clock=clock,
            log=log,
        )

    # -- read side ----------------------------------------------------------
    @property
    def installed(self) -> bool:
        return serve_bin(self._runtime_dir).is_file()

    def _current_state_locked(self) -> str:
        state = super()._current_state_locked()
        if state in ("stopped", "failed") and not self.installed:
            return "not_installed"
        return state

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._current_state_locked()
            out: dict[str, Any] = {
                "state": state,
                "installed": self.installed,
                "idle_stop_seconds": self._idle_stop_seconds,
                "models": sorted(set(self._served_models) | set(self._extra_models())),
            }
            if self._error and state in ("failed", "not_installed"):
                out["error"] = self._error
            if state == "ready" and self._health:
                out["device"] = self._health.get("device")
                out["model_ids"] = self._model_ids_locked()
            loaded = self._loaded_locked(state)
        # Additive: the swept cuts that apply to what is served (paths.thresholds_view owns the rule).
        out["thresholds"] = {
            surface: {
                "at": cut["at"],
                "model": cut["model"],
                **({"done_at": cut["done_at"]} if cut.get("done_at") is not None else {}),
            }
            for surface, cut in thresholds_view(self._registry(), loaded).items()
        }
        out["serving"] = self._serving(state, loaded)
        return out

    def _serving(self, state: str, loaded: dict[str, str] | None) -> dict[str, Any]:
        """Additive: which weights answer, in one place a first run can read. `base` is the public checkpoint
        (`BASE_MODEL`), which answers every question no fine-tune of the operator's own is served for; `ready` says
        it is answering NOW. While `base` is all there is, `first_fine_tune` says how many of the operator's labels
        each surface has toward one (`review_data.first_fine_tune` owns the bar); with a fine-tune served it is {}."""
        fine_tunes = sorted(loaded if loaded is not None else self._extra_models())
        return {
            "ready": state == "ready",
            "base": BASE_MODEL,
            "base_only": not fine_tunes,
            "fine_tunes": fine_tunes,
            "first_fine_tune": {} if fine_tunes else self._first_fine_tune(),
        }

    def _model_ids_locked(self) -> dict[str, str]:
        """`{served name: "laya-local/<name>@<version>"}` — the id a relayed verdict is stored under.

        `paths.laya_local_model_id` owns the version rule (a revision for a built-in, the checkpoint
        folder for a fine-tune).
        """
        ids: dict[str, str] = {}
        revisions = (self._health or {}).get("revisions") or {}
        for name, sha in revisions.items():
            model_id = laya_local_model_id(name, revision=sha) if isinstance(name, str) else None
            if model_id:
                ids[name] = model_id
        for name, path in self._served_models.items():
            model_id = laya_local_model_id(name, path=path)
            if model_id:
                ids[name] = model_id
        return ids

    def _loaded_locked(self, state: str) -> dict[str, str] | None:
        return dict(self._served_models) if state in ("starting", "ready") else None

    def loaded(self) -> dict[str, str] | None:
        """`{name: checkpoint dir}` laya-serve LOADED at spawn while it runs, else None (read the registry)."""
        with self._lock:
            return self._loaded_locked(self._current_state_locked())

    def spawn(self) -> int:
        """How many times this runtime has started laya-serve. A decision names the start it was answered by, so the
        ledger can tell a model's first answer since then (`ledger.py` `cold`)."""
        with self._lock:
            return self._spawns

    def served_identity(self, name: str) -> tuple[str | None, str | None]:
        """(`laya-local/…` model id, absolute checkpoint dir) of a served name; the dir is None for a built-in."""
        with self._lock:
            path = self._served_models.get(name)
            revisions = (self._health or {}).get("revisions") or {}
            revision = revisions.get(name) if isinstance(revisions, dict) else None
        model_id = laya_local_model_id(name, revision=revision if isinstance(revision, str) else None, path=path)
        return model_id, path

    @property
    def _serve_log(self) -> Path:
        return self._runtime_dir / ".serve.log"

    @property
    def _install_log(self) -> Path:
        return self._runtime_dir / ".install.log"

    # -- install ------------------------------------------------------------
    def install(self) -> str:
        """Start the install in the background: the runtime venv, then the base checkpoint (install.py owns the two
        steps). Returns the state now."""
        with self._lock:
            if self._state == "installing":
                return "installing"
            uv = installer.find_uv()
            if uv is None:
                self._state, self._error = "failed", "uv was not found on PATH or in ~/.local/bin, /opt/homebrew/bin"
                return "failed"
            self._state, self._error = "installing", None
        threading.Thread(target=self._run_install, args=(uv,), name="laya-install", daemon=True).start()
        return "installing"

    def _run_install(self, uv: str) -> None:
        with self._install_log.open("w", encoding="utf-8") as out:
            code, step = installer.run(uv, self._runtime_dir, out, self._log)
        with self._lock:
            if code == 0 and self.installed:
                self._state, self._error = "stopped", None
            else:
                self._failed_at = self._clock()
                self._state, self._error = "failed", f"uv {step} exited {code}: {tail(self._install_log)}"
        self._log(f"[laya] install finished: {self._state}")

    def _service_environment(self, port: int, key: str) -> dict[str, str]:
        # `extra_models` is the registry's SERVING view (paths.registry_view): each surface's @champion only, so a
        # registered @challenger is never loaded here; laya-lab's nightly pack scores it in shadow instead.
        self._served_models = self._extra_models()
        self._spawns += 1  # once per start, under the service lock `ensure_started` holds while it builds this
        env = {
            "LAYA_HOST": LOOPBACK,
            "LAYA_PORT": str(port),
            "LAYA_API_KEY": key,
            "LAYA_PRELOAD": "1",
            "LAYA_MODELS": "english",
            "LAYA_REVISION": "reviewed",
            "LAYA_LOG_LEVEL": "warning",
        }
        if self._served_models:
            env["LAYA_EXTRA_MODELS"] = json.dumps(self._served_models)
        return env

    # -- decide -------------------------------------------------------------
    def decide(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Forward one cleaned request. Callers must have seen `ensure_started() == "ready"`."""
        reply = self.request("POST", "/v1/systemone", body, DECIDE_TIMEOUT_SECONDS)
        if reply is None:
            return 502, {"error": "laya_unreachable", "detail": "OSError"}
        code, data = reply
        try:
            payload = json.loads(data)
        except ValueError:
            return 502, {"error": "laya_unreachable", "detail": "ValueError"}
        if not isinstance(payload, dict):
            return 502, {"error": "laya_unreachable", "detail": "ValueError"}
        if code >= 400:
            detail = payload.get("detail")
            return code, {"error": "laya_refused", "detail": detail if isinstance(detail, str) else str(detail or "")}
        return code, payload
