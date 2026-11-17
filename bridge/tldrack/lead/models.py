"""Which model a LEAD runs on, and which weaker one everything under it runs on.

THE RULE (operator, 2026-11-09): a lead coordinates on the strongest model and never does the work, so
the work runs on a weaker one. "Under it" is two layers, and one pair covers both:

- **Worker sessions** (`lead spawn`, each in its own worktree): a Claude worker launches with
  `--model <crew_model>`, so its own subagents inherit that model too. A Codex worker is unchanged: its
  model is its profile's `args`.
- **The lead's in-process subagents** (Agent tool, workflow agents, teammates): the lead launches with
  its subagent model FORCED to `crew_model`. A fork is the one thing the force does not reach, so
  `LeadService.hook_tool` denies a capped lead's forks.

How a model reaches a spawn, the family order, and the "strictly weaker" check: owned by
`tldrack/profiles.py` § LAUNCH MODELS — see the note there.

THE DEFAULT IS ON: `DEFAULT_PAIR`, with no file. The operator changes the pair, or turns the split off
(`crew_model: null`: everything runs the lead's model, as before 2026-11-09), from the rack UI's
LEAD row; `POST /lead/models` writes this file. A run keeps the pair it STARTED with
(`lead_runs.lead_model` / `crew_model`), so a change here reaches the next launch, never a run midway,
and a RESUME brings a lead back on its run's pair.

`model: null` means "the account's own model": the lead launches with no `--model`, and the weaker
check is skipped because the bridge cannot know what that account runs.

`~/.local/state/tldrack/lead-models.json` (env `TLDRACK_LEAD_MODELS_PATH`). Tolerant
load: a missing or malformed file is the default, never a refusal to launch.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from tldrack import paths
from tldrack.atomic_store import atomic_write
from tldrack.profiles import MODEL_FAMILIES, clean_launch_model, model_tier

PATH_ENV = "TLDRACK_LEAD_MODELS_PATH"
FILE_NAME = "lead-models.json"
DEFAULT_PAIR = {"model": "fable", "crew_model": "opus"}
FIELDS = ("model", "crew_model")


def resolve_lead_models_path() -> Path:
    env = os.environ.get(PATH_ENV)
    return Path(env) if env else paths.state_file(FILE_NAME)


def pair_error(model: object, crew_model: object) -> str | None:
    """Why this pair cannot be a lead's, or None. Pure. Either may be None."""
    for label, value in (("lead", model), ("crew", crew_model)):
        if value is not None and clean_launch_model(value) is None:
            return f"unknown {label} model {str(value)[:40]!r} (a family: {', '.join(MODEL_FAMILIES)}, or a claude-… id)"
    if model is not None and crew_model is not None and model_tier(crew_model) >= model_tier(model):
        return f"the worker model '{crew_model}' must be weaker than the lead's '{model}'"
    return None


def clean_pair(raw: object) -> dict[str, str | None] | None:
    """A stored or requested pair, or None when it is not one. Pure. Both keys must be present:
    an absent key is not "off", it is a document this module did not write."""
    if not isinstance(raw, Mapping) or not all(field in raw for field in FIELDS):
        return None
    model, crew_model = raw["model"], raw["crew_model"]
    if pair_error(model, crew_model) is not None:
        return None
    return {"model": model, "crew_model": crew_model}


class LeadModels:
    """The standing pair. `pair()` on every launch; `set()` from the operator's row."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or resolve_lead_models_path()
        self._lock = threading.Lock()

    def pair(self) -> dict[str, str | None]:
        try:
            stored = clean_pair(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            stored = None
        return stored or dict(DEFAULT_PAIR)

    def view(self) -> dict:
        """The `/lead/state` `models` block: the pair, the default, and the choices."""
        return {**self.pair(), "default": dict(DEFAULT_PAIR), "families": list(MODEL_FAMILIES)}

    def set(self, raw: Mapping) -> dict[str, str | None]:
        """Merge the given keys over the current pair and store it. Raises ValueError with the reason."""
        merged = {**self.pair(), **{field: raw[field] for field in FIELDS if field in raw}}
        error = pair_error(merged["model"], merged["crew_model"])
        if error is not None:
            raise ValueError(error)
        with self._lock:
            atomic_write(self._path, json.dumps(merged, indent=2) + "\n", "the LEAD model pair")
        return merged
