"""`<data>/laya/models.json` — the registry the laya bridge plugin serves fine-tunes from.

The READ side, the file's location, its v2 shape (versions + @champion / @challenger / @previous aliases) and the one
v1 → v2 rule are owned by `plugins/laya/laya_bridge/paths.py` — see the note there; the shape of each entry
this writes (`trained`, `cut`, `tags`) by `laya_bridge/lab_files.py`. This module is the only WRITER, and it writes
atomically so a crash mid-write never leaves the plugin a half-file.

- `register(..., as_="champion")` serves a checkpoint now (the old champion becomes @previous); `as_="challenger"`
  never touches what is served: the nightly pack shadow-scores it, and only a promotion moves @champion onto it.
- A swept cut belongs to the checkpoint it was swept on (`versions.<version>.cut`), so it moves with the alias: a
  promotion serves the challenger at its own cut and a rollback restores the old champion's, never the code's 0.7.
- Every writer reads through `read_registry`, which migrates a v1 file in place first (`migrate_registry`: lossless,
  nothing served changes, the v1 file kept beside it). A write on v1's shape would silently lose a challenger or a
  cut, and refusing instead would break every armed lab press until a night had run, with a terminal command as
  the only way out. `laya-lab registry migrate` and the nightly pack's first step run the same migration on purpose.
- Retention: champion, challenger, previous and the `REJECTS_KEPT` newest rejects stay on disk; `retention` names the
  rest, and only `prune_rejects` (the armed REJECT) deletes them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab.data import atomic_write_text
from laya_bridge import files, lab_files, rows
from laya_bridge.paths import (
    ALIASES,
    CHECKPOINTS_DIRNAME,
    DATA_DIRNAME,  # noqa: F401 (re-exported)
    HISTORY_MAX,  # noqa: F401 (re-exported)
    LEGACY_REGISTRY_VERSION,
    MODELS_FILENAME,
    REGISTRY_VERSION,
    empty_payload,
    PathsError,
    ensure_laya_dir,
    fine_tune_name as _fine_tune_name,
    served_name,
    laya_dir,
    migrate_payload,
    payload_version,
    registry_versions,
    registry_view,
    thresholds_view,
    with_serving_view,
)
from laya_bridge.wire import BUILTIN_MODELS, MODEL_NAME
from tldrack import open_work

#: A checkpoint folder laya-lab can load holds this (`laya.train.save_checkpoint`).
WEIGHTS_FILE = "model.safetensors"
SERVED = "served"
CHAMPION, CHALLENGER, PREVIOUS = ALIASES
AS_CHOICES = (CHAMPION, CHALLENGER)
REJECTS_KEPT = lab_files.REJECTS_KEPT


def fine_tune_name(surface: str) -> str:
    """The surface's served name: owned by `laya_bridge.paths.fine_tune_name` — see the note there."""
    return _fine_tune_name(surface)


def base_dir(env: Mapping[str, str] = os.environ, *, create: bool = True) -> Path:
    """The laya store, `<data>/laya` (`laya_bridge.paths` owns where that is). A command that writes creates it 0700
    when absent, so a first run needs nothing configured; `create=False` is a read, and the folder may not be there.
    An unusable `paths.json` stops the command: it never falls back to a fresh store beside the operator's own."""
    try:
        return ensure_laya_dir(env) if create else laya_dir(env)
    except PathsError as error:
        raise SystemExit(f"[laya-lab] {error}") from None


def _read_raw(base: Path) -> dict[str, Any] | None:
    """The file as JSON, strictly (None when absent): an unparseable or non-object file stops the command."""
    try:
        payload = json.loads((base / MODELS_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        raise SystemExit(f"{base / MODELS_FILENAME} is not JSON; fix or move it") from None
    if not isinstance(payload, dict) or payload_version(payload) is None:
        raise SystemExit(f"{base / MODELS_FILENAME} is not a registry this version reads; fix or move it")
    return payload


def read_registry(base: Path, *, migrate: bool = True) -> dict[str, Any]:
    """The strict writer-side read, always in v2's shape. A v1 file is migrated in place first (`migrate_registry`; the
    module note owns why), its notes on stderr; `migrate=False` (a command that only reads) migrates it in memory and
    writes nothing. A shape this version cannot rewrite safely stops the command."""
    payload = _read_raw(base)
    if payload is None:
        return empty_payload()
    if payload_version(payload) == LEGACY_REGISTRY_VERSION:
        if not migrate:
            payload = migrate_payload(payload)[0]
        else:
            _migrated, notes = migrate_registry(base)
            for note in notes:
                print(f"[laya-lab] registry: migrated v{LEGACY_REGISTRY_VERSION} → v{REGISTRY_VERSION} · {note}", file=sys.stderr)
            payload = _read_raw(base)
            if payload is None or payload_version(payload) != REGISTRY_VERSION:
                raise SystemExit(f"{base / MODELS_FILENAME} changed while it was migrated; run the command again")
    if (
        not isinstance(payload.get("versions"), dict)
        or not isinstance(payload.get("aliases", {}), dict)
        or not isinstance(payload.get("history", []), list)
    ):
        raise SystemExit(f"{base / MODELS_FILENAME} is not a registry this version reads; fix or move it")
    payload.setdefault("aliases", {})
    payload.setdefault("history", [])
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_registry(base: Path, payload: dict[str, Any]) -> None:
    """Write v2 whole, its v1-shaped serving view rewritten from the aliases first (`paths.with_serving_view`: why a
    pre-v2 bridge still serves the champion at its own cut)."""
    payload["version"] = REGISTRY_VERSION
    payload["history"] = list(payload.get("history") or [])[-HISTORY_MAX:]
    atomic_write_json(base / MODELS_FILENAME, with_serving_view(payload))


def migrate_registry(base: Path) -> tuple[bool, list[str]]:
    """v1 → v2 in place (`paths.migrate_payload` owns the rule), the v1 file kept beside it as `models.v1-<stamp>.json`.
    `(migrated, notes)`; a missing or v2 file is left alone. Nothing served changes: each v1 entry becomes its
    surface's @champion with its own cut."""
    payload = _read_raw(base)
    if payload is None or payload_version(payload) == REGISTRY_VERSION:
        return False, []
    migrated, notes = migrate_payload(payload)
    stem = f"models.v{LEGACY_REGISTRY_VERSION}-{lab_files.report_stamp()}"
    backup = next(
        path for n in range(1, 1000) if not os.path.lexists(path := base / (f"{stem}.json" if n == 1 else f"{stem}-{n}.json"))
    )  # a backup is never overwritten
    atomic_write_text(backup, (base / MODELS_FILENAME).read_text(encoding="utf-8"))
    at = rows.utc_stamp()
    for surface, block in migrated["aliases"].items():
        _record(migrated, surface, CHAMPION, None, block[CHAMPION], f"migrated from v{LEGACY_REGISTRY_VERSION}", at)
    write_registry(base, migrated)
    return True, [*notes, f"v{LEGACY_REGISTRY_VERSION} kept as {backup.name}"]


# ── aliases ───────────────────────────────────────────────────────────────────


def _aliases(payload: dict[str, Any], surface: str) -> dict[str, str | None]:
    block = payload["aliases"].setdefault(surface, {})
    for alias in ALIASES:
        block.setdefault(alias, None)
    return block


def _record(
    payload: dict[str, Any], surface: str, alias: str, old: str | None, new: str | None, why: str, at: str | None = None
) -> None:
    payload.setdefault("history", []).append(
        {"at": at or rows.utc_stamp(), "surface": surface, "alias": alias, "from": old, "to": new, "why": why}
    )


def _set_alias(payload: dict[str, Any], surface: str, alias: str, new: str | None, why: str) -> str | None:
    """Move one alias; returns the version it held. A no-op move records nothing."""
    block = _aliases(payload, surface)
    old = block[alias]
    if old != new:
        block[alias] = new
        _record(payload, surface, alias, old, new, why)
    return old


def _reject(payload: dict[str, Any], version: str | None) -> None:
    """Stamp a version rejected (why: the alias move `history` records beside it)."""
    entry = payload["versions"].get(version or "")
    if entry is not None and not entry.get("rejected_at"):
        entry["rejected_at"] = rows.utc_stamp()


def register(
    base: Path,
    surface: str,
    checkpoint: Path,
    report: dict[str, Any],
    trained: dict[str, Any] | None = None,
    *,
    as_: str = CHAMPION,
    tags: dict[str, Any] | None = None,
) -> str:
    """Register `checkpoint` (which must live under `base`) as a version of `surface` and point an alias at it.
    Returns the name it answers to: the served name for a champion, its folder name for a challenger.

    `as_="champion"` serves it (the old champion becomes @previous); `as_="challenger"` never touches the served
    entry (a challenger it replaces is rejected: superseded before it was promoted). The version keeps its own cut,
    so re-registering the same checkpoint keeps the cut swept for it, and a new one starts at the code's.
    """
    if as_ not in AS_CHOICES:
        raise SystemExit(f"--as must be one of {', '.join(AS_CHOICES)}")
    checkpoint = checkpoint.resolve()
    if base.resolve() not in checkpoint.parents:
        raise SystemExit(f"{checkpoint} is outside {base}; the plugin would refuse to serve it")
    version = checkpoint.name
    if not MODEL_NAME.fullmatch(version) or version in BUILTIN_MODELS:
        raise SystemExit(f"{version!r} cannot name a version: a checkpoint folder is [a-z][a-z0-9-]{{0,63}}")
    payload = read_registry(base)
    held = payload["versions"].get(version)
    if held is not None and held.get("surface") != surface:
        raise SystemExit(f"{version} is registered for {held.get('surface')}, not {surface}")
    entry: dict[str, Any] = {
        "path": str(checkpoint.relative_to(base.resolve())),
        "surface": surface,
        "eval": report,
        "registered_at": rows.utc_stamp(),
    }
    if trained is not None:
        entry["trained"] = trained
    if tags is not None:
        entry["tags"] = tags
    if held is not None and isinstance(held.get("cut"), dict):
        entry["cut"] = held["cut"]
    payload["versions"][version] = entry
    block = _aliases(payload, surface)
    if as_ == CHAMPION:
        old = _set_alias(payload, surface, CHAMPION, version, "registered as champion")
        if old is not None and old != version:
            _set_alias(payload, surface, PREVIOUS, old, f"replaced by {version}")
        if block[CHALLENGER] == version:
            _set_alias(payload, surface, CHALLENGER, None, "registered as champion")
        if block[PREVIOUS] == version:
            _set_alias(payload, surface, PREVIOUS, None, "registered as champion")
        name = fine_tune_name(surface)
    else:
        if block[CHAMPION] == version:
            raise SystemExit(f"{version} is the served champion of {surface}; a challenger must be another checkpoint")
        old = _set_alias(payload, surface, CHALLENGER, version, "registered as challenger")
        if old is not None and old != version:
            _reject(payload, old)
        if block[PREVIOUS] == version:
            _set_alias(payload, surface, PREVIOUS, None, "registered as challenger")
        name = version
    write_registry(base, payload)
    return name


def unregister(base: Path, surface: str) -> bool:
    """Stop serving the surface's fine-tune: @champion clears (it becomes @previous), so the surface falls back to the
    base model. The version and its cut stay registered, so serving it again restores that cut. True when a champion
    was cleared."""
    payload = read_registry(base)
    old = _aliases(payload, surface)[CHAMPION]
    if old is None:
        return False
    _set_alias(payload, surface, CHAMPION, None, "unregistered")
    _set_alias(payload, surface, PREVIOUS, old, "unregistered")
    write_registry(base, payload)
    return True


def promote_challenger(base: Path, surface: str, why: str) -> tuple[str, str | None]:
    """@champion moves onto @challenger, carrying that checkpoint's own cut; the old champion becomes @previous (the
    rollback target). `(new champion, previous)`. The armed PROMOTE CHALLENGER (`challenger.act`) owns WHEN."""
    payload = read_registry(base)
    block = _aliases(payload, surface)
    challenger = block[CHALLENGER]
    if challenger is None:
        raise SystemExit(f"{surface} has no challenger to promote")
    old = _set_alias(payload, surface, CHAMPION, challenger, why)
    _set_alias(payload, surface, CHALLENGER, None, why)
    _set_alias(payload, surface, PREVIOUS, old, f"replaced by {challenger}")
    write_registry(base, payload)
    return challenger, old


def rollback(base: Path, surface: str, why: str) -> tuple[str, str | None]:
    """@champion moves back onto @previous (its own cut with it); the demoted champion is rejected. `(champion, demoted)`.
    The next-night rollback guard (`challenger.nightly`) owns WHEN."""
    payload = read_registry(base)
    block = _aliases(payload, surface)
    previous = block[PREVIOUS]
    if previous is None:
        raise SystemExit(f"{surface} has no previous champion to roll back to")
    demoted = _set_alias(payload, surface, CHAMPION, previous, why)
    _set_alias(payload, surface, PREVIOUS, None, why)
    _reject(payload, demoted)
    write_registry(base, payload)
    return previous, demoted


def reject_challenger(base: Path, surface: str, why: str) -> str:
    """The challenger is rejected: its alias clears and it joins the rejects (`retention` decides what stays on disk)."""
    payload = read_registry(base)
    challenger = _aliases(payload, surface)[CHALLENGER]
    if challenger is None:
        raise SystemExit(f"{surface} has no challenger to reject")
    _set_alias(payload, surface, CHALLENGER, None, why)
    _reject(payload, challenger)
    write_registry(base, payload)
    return challenger


# ── retention ─────────────────────────────────────────────────────────────────


def retention(base: Path, surface: str) -> dict[str, Any]:
    """What stays on disk and what the armed REJECT would prune: owned by `laya_bridge.lab_files.retention`."""
    return lab_files.retention(base, surface, registry_versions(base))


def prune_rejects(base: Path, surface: str) -> list[dict[str, Any]]:
    """Delete what `retention` names prunable: each folder, its `.data` record, its registry version and its rows in
    the shadow file (each item gains `shadow_rows`, how many went). Only the armed REJECT runs this:
    a deleted checkpoint cannot come back.

    `retention` reads the plugin's forgiving view, which reads an unreadable file as empty, so a plan is only trusted
    when that view read the same v2 file the strict read did, and never names a version an alias still holds there:
    a deletion is never driven by a read that failed open."""
    payload = read_registry(base)
    view = registry_versions(base)
    if (base / MODELS_FILENAME).exists() and view["version"] != REGISTRY_VERSION:
        raise SystemExit(
            f"{base / MODELS_FILENAME} does not read as v{REGISTRY_VERSION} the way the plugin reads it; nothing pruned"
        )
    held = {
        name
        for block in payload["aliases"].values()
        if isinstance(block, dict)
        for name in block.values()
        if isinstance(name, str)
    }
    plan = [item for item in lab_files.retention(base, surface, view)["prunable"] if item["name"] not in held]
    if not plan:
        return []
    root = (base / CHECKPOINTS_DIRNAME).resolve()
    for item in plan:
        folder = Path(item["path"])
        for path in (folder, folder.with_name(folder.name + ".data")):
            if files.is_real_dir(path) and path.resolve().parent == root:
                shutil.rmtree(path)
        payload["versions"].pop(item["name"], None)
    write_registry(base, payload)
    from laya_lab import shadow  # shadow.py imports this module

    dropped = shadow.drop_versions(base, surface, {item["name"] for item in plan})
    for item in plan:
        item["shadow_rows"] = dropped.get(item["name"], 0)
    return plan


# ── what is served, and what else could be ────────────────────────────────────


def served_model(base: Path, surface: str) -> str:
    """The surface's registered fine-tune name, else `english` — what the bridge serves it with.

    Through the plugin's own reader (`paths.registry_view`), so a registered folder that is gone or
    outside `base` counts as unregistered here exactly as it does there.
    """
    name = fine_tune_name(surface)
    return name if name in registry_view(base)["models"] else "english"


def alias_target(base: Path, surface: str, alias: str) -> tuple[str, Path] | None:
    """`(version name, checkpoint folder)` an alias points at, through the plugin's reader, or None."""
    view = registry_versions(base)
    name = (view["aliases"].get(surface) or {}).get(alias)
    version = view["versions"].get(name or "")
    return (name, Path(version["path"])) if version is not None else None


def available_checkpoints(base: Path, surface: str) -> dict[str, Path]:
    """`{name: checkpoint folder}`: the served fine-tune (by its served name), every other registered version of the
    surface (by folder name), then every `checkpoints/<fine_tune>-*` folder on disk holding weights. Deduped by
    resolved path (the served name wins), never a `.data` folder, a link out of `base`, or a name a caller could not pin."""
    fine_tune = fine_tune_name(surface)
    found: dict[str, Path] = {}
    seen: set[Path] = set()
    entry = registry_view(base)["models"].get(fine_tune)
    if entry is not None:
        found[fine_tune] = Path(entry["path"])
        seen.add(Path(entry["path"]))
    for name, version in sorted(registry_versions(base)["versions"].items()):
        path = Path(version["path"])
        if version["surface"] == surface and path not in seen and (path / WEIGHTS_FILE).is_file():
            found[name] = path
            seen.add(path)
    folder = base / CHECKPOINTS_DIRNAME
    if not files.is_real_dir(folder):
        return found
    root = base.resolve()
    pattern = rows.checkpoint_name(fine_tune)
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return found
    for child in children:
        if lab_files.is_choice_head(base, child.name):
            continue
        if not pattern.fullmatch(child.name) or not MODEL_NAME.fullmatch(child.name):
            continue
        resolved = child.resolve()
        if root not in resolved.parents or resolved in seen or not (resolved / WEIGHTS_FILE).is_file():
            continue
        seen.add(resolved)
        found[child.name] = resolved
    return found


def resolve_model(base: Path, surface: str, model: str) -> tuple[str, Path | None]:
    """`(name, checkpoint folder | None for a built-in)` for `--model served|champion|challenger|previous|english|
    <checkpoint>`. The served checkpoint's own folder name resolves to the name it is served under
    (`available_checkpoints` lists it so); an alias resolves to the folder it points at."""
    if model in (SERVED, CHAMPION):
        model = served_model(base, surface)
    elif served_name(model) == fine_tune_name(surface):
        model = fine_tune_name(surface)  # its legacy name too (paths.served_name)
    elif model in (CHALLENGER, PREVIOUS):
        target = alias_target(base, surface, model)
        if target is None:
            raise SystemExit(f"[laya-lab] {surface} has no @{model}")
        model = target[0]
    if model in BUILTIN_MODELS:
        return model, None
    available = available_checkpoints(base, surface)
    registered = registry_view(base)["models"].get(fine_tune_name(surface))
    if model not in available and registered is not None and model == registered["checkpoint"]:
        model = fine_tune_name(surface)
    if model in available:
        return model, available[model]
    names = ", ".join([SERVED, CHALLENGER, *BUILTIN_MODELS, *available])
    raise SystemExit(f"[laya-lab] no model {model!r} for {surface}: one of {names}")


# ── training cutoffs ──────────────────────────────────────────────────────────

#: A checkpoint folder ends in the LOCAL minute its fit was planned (`train.prepare`).
CHECKPOINT_STAMP = re.compile(r"-(\d{8}-\d{4}(?:\d{2})?)$")


def instant(stamp: object) -> datetime | None:
    """An ISO stamp as an aware instant (one with no zone is UTC, as every stamp laya-lab writes is), or None."""
    if not isinstance(stamp, str):
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def version_cutoff(version: dict[str, Any]) -> datetime | None:
    """When a registered version took its training set: `trained.at`, else when it was registered, else the minute
    its folder is named for (a v1 entry carries neither of the first two)."""
    trained = version.get("trained") or {}
    found = instant(trained.get("at")) or instant(version.get("registered_at"))
    if found is not None:
        return found
    named = CHECKPOINT_STAMP.search(version.get("name") or "")
    try:
        return (
            datetime.strptime(named.group(1), "%Y%m%d-%H%M%S" if len(named.group(1)) == 15 else "%Y%m%d-%H%M").astimezone()
            if named
            else None
        )
    except ValueError:
        return None


def training_cutoff(base: Path, surface: str) -> tuple[datetime | None, str | None]:
    """`(cutoff, version)`: the newest training cutoff among the surface's @champion and @challenger, and the version
    it belongs to. An operator grade newer than it is in no checkpoint that serves or shadows; the gated retrain counts
    those (`retrain.owed`). (None, None) with neither alias set, or when no cutoff reads."""
    view = registry_versions(base)
    aliases = view["aliases"].get(surface) or {}
    found: list[tuple[datetime, str]] = []
    for alias in (CHAMPION, CHALLENGER):
        version = view["versions"].get(aliases.get(alias) or "")
        cutoff = version_cutoff(version) if version is not None else None
        if cutoff is not None:
            found.append((cutoff, version["name"]))
    return max(found) if found else (None, None)


# ── swept cuts ────────────────────────────────────────────────────────────────


def set_threshold(base: Path, surface: str, entry: dict[str, Any]) -> None:
    """Record a swept cut on the version it was swept for (`entry` = `lab_files.threshold_entry(...)`, its `checkpoint`
    the version's folder name). A cut for a checkpoint that is not a registered version of `surface` is refused: a cut
    with no checkpoint to ride would be served for whatever the alias points at next."""
    payload = read_registry(base)
    version = payload["versions"].get(entry.get("checkpoint") or "")
    if version is None or version.get("surface") != surface:
        raise SystemExit(
            f"[laya-lab] {entry.get('checkpoint')!r} is not a registered version of {surface}: "
            "a cut is kept on the checkpoint it was swept for"
        )
    version["cut"] = entry
    write_registry(base, payload)


def clear_threshold(base: Path, surface: str, version: str | None = None) -> bool:
    """Back to the code's cut for the champion (or `version`). True when a swept one was removed."""
    payload = read_registry(base)
    name = version or _aliases(payload, surface)[CHAMPION]
    entry = payload["versions"].get(name or "")
    cleared = entry is not None and entry.pop("cut", None) is not None
    if cleared:
        write_registry(base, payload)
    return cleared


def swept_cut(base: Path, surface: str, model: str) -> dict[str, Any] | None:
    """The swept cut (`lab_files.threshold_view`) that applies to `model`: for the served name, the champion's own
    (`paths.thresholds_view` owns that rule); for a registered version's folder name, that version's own; else None."""
    if served_name(model) == fine_tune_name(surface):
        cut = thresholds_view(registry_view(base)).get(surface)
        return cut if cut is not None and cut["model"] == fine_tune_name(surface) else None
    version = registry_versions(base)["versions"].get(model)
    if version is None or version["surface"] != surface or version["cut"] is None or not 0 < version["cut"]["at"] < 1:
        return None
    return {**version["cut"], "model": model}


def served_done_threshold(base: Path, surface: str, model: str, *, code: float = open_work.DONE_AT) -> tuple[float, str]:
    """`(✅ cut, 'swept' | 'code')` under `swept_cut`'s scoping rule."""
    cut = swept_cut(base, surface, model)
    if cut is not None and cut.get("done_at") is not None:
        return cut["done_at"], "swept"
    return code, "code"


def served_threshold(base: Path, surface: str, model: str, *, code: float = open_work.OPEN_WORK_AT) -> tuple[float, str]:
    """`(cut, 'swept' | 'code')` for `model` on `surface`: its `swept_cut`, else `code`."""
    cut = swept_cut(base, surface, model)
    return (cut["at"], "swept") if cut is not None else (code, "code")
