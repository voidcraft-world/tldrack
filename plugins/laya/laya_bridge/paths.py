"""Where Laya's runtime venv, the operator's data and their fine-tuned checkpoints live.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

- The runtime venv: `plugins/laya/runtime/.venv` — beside this package, created by
  `POST /install` (`uv sync --project runtime`). It holds torch, so it is never the bridge's venv.
- The laya store, `<data>/laya` (`laya_dir`): the decision ledger, the operator's labels, checkpoints and
  `models.json`. Outside every checkout, because a checkpoint is ~1.6 GB and trained on the operator's own text.

    <data>/laya/{data,checkpoints,ledger,landing,golden,evals,shadow,stability,retrain}/ and models.json

WHERE `<data>` IS. The bridge's resolver decides (`bridge/tldrack/paths.py` owns the rule and the `paths.json`
format); `laya_dir` is its port for the one store this plugin needs, because laya-lab runs from its own venv with no
bridge to import, and must land in the same folder as the plugin. The order: `paths.json` `stores.laya`, then
`$TLDRACK_DATA_DIR/laya` (the bridge exports the data role at start), then `paths.json` `data`, then
`~/.local/share/tldrack/laya`. A mapped directory that is not there is an error, never a fresh empty store beside
the real one.

THE REGISTRY, version 2: versions and aliases, following MLflow's model registry. Additive over v1:

    {"version": 2,
     "versions": {"voidcraft-agent-open-work-20260102-0304": {"path": "checkpoints/voidcraft-agent-open-work-20260102-0304",
                   "surface": "agent_open_work", "registered_at": "…Z", "tags": {"gate": "PASS", …},
                   "cut": {"at": 0.62, "checkpoint": "voidcraft-agent-open-work-20260102-0304", …}}},
     "aliases": {"agent_open_work": {"champion": "voidcraft-agent-open-work-20260102-0304", "challenger": null,
                                     "previous": null}},
     "history": [{"at": "…Z", "surface": "agent_open_work", "alias": "champion", "from": null, "to": "…", "why": "…"}],
     "models": {"voidcraft-agent-open-work": {"path": "checkpoints/voidcraft-agent-open-work-20260102-0304", …}},
     "thresholds": {"agent_open_work": {"at": 0.62, "model": "voidcraft-agent-open-work", …}}}

`versions` and `aliases` are the truth. A version is keyed by its checkpoint folder's name and carries its own swept
cut, so moving an alias moves the cut with it: a promotion serves the challenger at the cut swept for it, and a
rollback restores the old champion's, never the code's 0.7. ONLY @champion is served: the serving view
(`registry_view`) has one entry per surface, under the surface's served name (`fine_tune_name`), and each becomes a
`LAYA_EXTRA_MODELS` registration. A @challenger is scored by laya-lab in shadow (the nightly pack) and never reaches
laya-serve. @previous is the champion a promotion replaced: the one-night rollback target.

`models` and `thresholds` are that serving view in v1's own shape, rewritten by the one writer on every write
(`with_serving_view`) and never read back by this version. They are why a v2 file is SAFE in any order: a bridge or
laya-lab that predates v2 (a revert, or a lane run before its merge) still serves each champion at its own cut.

Version 1 (`{"version": 1, "models": {"voidcraft-paste": {"path": …}}, "thresholds": {surface: cut}}`) meant "every
entry is served". It is still READ exactly so; `migrate_payload` is the one v1 → v2 rule, and laya-lab's strict
writer runs it in place before its first write: owned by `lab/laya_lab/registry.py` — see the note there. A v1
file that still holds `versions` (a pre-v2 writer rewrote a v2 file) keeps them, reconciled to what that writer served.

A relative `path` resolves inside the laya store, and every entry must stay there after
resolution — a planted link cannot make laya-serve load weights from elsewhere.

This module is THE path owner for everything under the laya store; laya-lab imports
its names. `registry_view` / `registry_versions` are the plugin's forgiving reads of `models.json` (laya-lab's
`registry.read_registry` stays the strict writer-side read).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from laya_bridge import files
from laya_bridge.wire import BUILTIN_MODELS, MODEL_NAME

#: The bridge resolver's names (`bridge/tldrack/paths.py`): the two role variables it exports, its mapping file, and
#: this plugin's store.
CONFIG_ENV, DATA_ENV = "TLDRACK_CONFIG_DIR", "TLDRACK_DATA_DIR"
PATHS_FILENAME = "paths.json"
STORE_NAME = "laya"
LAYA_DIRNAME = "laya"
PRIVATE_MODE = 0o700
MODELS_FILENAME = "models.json"
DATA_DIRNAME, EVALS_DIRNAME, LEDGER_DIRNAME, GOLDEN_DIRNAME, CHECKPOINTS_DIRNAME = (
    "data",
    "evals",
    "ledger",
    "golden",
    "checkpoints",
)
#: The landing record beside the ledger (landing.py): one row per rack verdict, where it landed.
LANDING_DIRNAME = "landing"
#: The nightly pack's shadow scores (`shadow/<surface>.jsonl`, laya-lab `shadow.py`): each @challenger's p on the
#: ledger's live states, beside the served p. Eval output, never training data, so not under `data/`.
SHADOW_DIRNAME = "shadow"
#: The stability meter's files (laya-lab `stability.py`): `<surface>-stop-set.jsonl`, the fixed states it asks every
#: checkpoint about (operator text), and `<surface>.jsonl`, each checkpoint's p on them (keys and numbers only).
STABILITY_DIRNAME = "stability"
#: The gated retrain's step journal (laya-lab `retrain.py`): `<suite>.json`, the cycle in flight or last closed, and
#: `label-calls.jsonl`, the week's subscription label calls. Ids, states and numbers only.
RETRAIN_DIRNAME = "retrain"
#: laya-serve stops itself after this long with no request (`runtime.py`), so the first decision after a longer
#: silence was answered by a fresh start: how laya-lab's latency read tells cold on a row that recorded none.
IDLE_STOP_SECONDS = 15 * 60
#: Every version of the golden suites and the operator label set a report was scored on (`golden/versions.jsonl`).
DATASET_VERSIONS_FILENAME = "versions.jsonl"

REGISTRY_VERSION = 2
LEGACY_REGISTRY_VERSION = 1
#: champion: served · challenger: shadow-scored, never served · previous: the champion a promotion replaced.
ALIASES = ("champion", "challenger", "previous")
FINE_TUNE_PREFIX = "voidcraft-"
#: How many alias moves `history` keeps (laya-lab appends; the oldest fall off).
HISTORY_MAX = 64

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PLUGIN_ROOT / "runtime"
#: <repo>/plugins/laya → up 2: the tldrack checkout this plugin was loaded from. laya-lab is its sibling `lab/`.
REPO_ROOT = PLUGIN_ROOT.parents[1]
LAB_DIR = REPO_ROOT / "lab"
PUBLIC_GOLDEN_DIR = LAB_DIR / "golden"

#: The files of one surface under `data/`, `<stem>-<part>.jsonl`. labels/unlabelled: laya-lab's
#: Sonnet labels and exported texts; operator: the REVIEW deck's log (`review_log.py`); laya-p: the
#: served model's scores (`lab_files.scores_row`); teacher: a local teacher's labels beside Sonnet's
#: (laya-lab `teacher_local.py`); requests: the request that opened each text's turn, keyed like the
#: texts (laya-lab `open_work_data.py` `export`); votes: a Claude model's p per state, asked once on the
#: subscription (laya-lab `claude_voter.py`).
DATA_PARTS = ("labels", "unlabelled", "operator", "laya-p", "teacher", "requests", "votes")
MODELS_CAP_BYTES = 1024 * 1024

SURFACE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: `LAYA_LOCAL_MODEL_ID` in schemas/src/decide.ts. fullmatch.
LAYA_LOCAL_MODEL_ID = re.compile(r"laya-local/[a-z][a-z0-9-]{0,63}@[A-Za-z0-9._-]{1,64}")

_REGISTRY = files.StampedCache()


def serve_bin(runtime_dir: Path = RUNTIME_DIR) -> Path:
    return runtime_dir / ".venv" / "bin" / "laya-serve"


class PathsError(ValueError):
    """`paths.json` or a role variable names something unusable (the bridge refuses to start on the same file)."""


def _home_dir(env: Mapping[str, str], *parts: str) -> Path:
    home = env.get("HOME", "").strip()
    return (Path(home) if os.path.isabs(home) else Path.home()).joinpath(*parts)


def _role_env(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PathsError(f"{name} is not an absolute path")
    return path


def _mapping(env: Mapping[str, str]) -> dict[str, Any]:
    """`paths.json` as written, `{}` when there is none. It is read from the config role's variable or default,
    never from a `config` entry of its own."""
    source = (_role_env(env, CONFIG_ENV) or _home_dir(env, ".config", "tldrack")) / PATHS_FILENAME
    try:
        payload = files.loads(source.read_bytes())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        raise PathsError(f"cannot read {PATHS_FILENAME}: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise PathsError(f"{PATHS_FILENAME}: expected an object")
    return payload


def _mapped(value: object, what: str) -> Path:
    path = Path(value).expanduser() if isinstance(value, str) and value.strip() else None
    if path is None or not path.is_absolute() or not path.is_dir():
        raise PathsError(f"{PATHS_FILENAME}: {what} is not an existing absolute directory")
    return path


def laya_dir(env: Mapping[str, str] = os.environ) -> Path:
    """The laya store, resolved (the module note owns the order). A read: it creates nothing, so the folder may not
    be there yet (`ensure_laya_dir` is the one creator). Raises `PathsError` on an unusable mapping."""
    mapping = _mapping(env)
    stores = mapping.get("stores")
    if isinstance(stores, dict) and STORE_NAME in stores:
        return _mapped(stores[STORE_NAME], f"stores.{STORE_NAME}").resolve()
    data = _role_env(env, DATA_ENV)
    if data is None:
        data = _mapped(mapping["data"], "data") if "data" in mapping else _home_dir(env, ".local", "share", "tldrack")
    return data.resolve() / LAYA_DIRNAME


def ensure_laya_dir(env: Mapping[str, str] = os.environ) -> Path:
    """`laya_dir`, created private to the user when absent: a first run has a ledger and a place for labels with
    nothing configured."""
    base = laya_dir(env)
    base.mkdir(parents=True, exist_ok=True, mode=PRIVATE_MODE)
    return base


def data_file(base: Path, stem: str, part: str) -> Path:
    if part not in DATA_PARTS:
        raise ValueError(f"unknown data part {part!r}")
    return base / DATA_DIRNAME / f"{stem}-{part}.jsonl"


def shadow_path(base: Path, surface: str) -> Path:
    return base / SHADOW_DIRNAME / f"{surface}.jsonl"


def stop_set_path(base: Path, surface: str) -> Path:
    return base / STABILITY_DIRNAME / f"{surface}-stop-set.jsonl"


def stability_path(base: Path, surface: str) -> Path:
    return base / STABILITY_DIRNAME / f"{surface}.jsonl"


def dataset_versions_path(base: Path) -> Path:
    return base / GOLDEN_DIRNAME / DATASET_VERSIONS_FILENAME


def fine_tune_name(surface: str) -> str:
    """Port of `fine_tune_name` in schemas/src/decide.ts — that file owns the convention."""
    return f"{FINE_TUNE_PREFIX}{surface.replace('_', '-')}"


def surface_of_name(name: str) -> str | None:
    """The surface a served name serves (`voidcraft-agent-open-work` → `agent_open_work`), or None."""
    if not name.startswith(FINE_TUNE_PREFIX):
        return None
    surface = name[len(FINE_TUNE_PREFIX) :].replace("-", "_")
    return surface if SURFACE_NAME.fullmatch(surface) else None


def private_golden_path(base: Path, suite: str) -> Path:
    """The operator's own golden cases: never the public suite's basename, never in the repo."""
    return base / GOLDEN_DIRNAME / f"{suite}.private.jsonl"


def laya_local_model_id(name: str, *, revision: str | None = None, path: str | None = None) -> str | None:
    """`laya-local/<name>@<version>`, the id a relayed verdict is stored under, or None.

    A fine-tune's version is its checkpoint folder's name (laya-lab dates them), so retraining
    re-judges like a model bump; a built-in's is the Hub revision laya-serve reports it loaded.
    Format owned by schemas/src/decide.ts (`is_laya_local_model_id`).
    """
    if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
        return None
    if path:
        version = Path(path).name[:64]
    elif isinstance(revision, str) and revision:
        version = revision[:7]
    else:
        return None
    model_id = f"laya-local/{name}@{version}"
    return model_id if LAYA_LOCAL_MODEL_ID.fullmatch(model_id) else None


def _parse_registry(data: bytes) -> dict | None:
    try:
        payload = files.loads(data)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def payload_version(payload: Mapping[str, Any]) -> int | None:
    """2 for a v2 registry, 1 for a v1 one (a missing version is v1: the first writer never omitted it, but a hand-made
    file might), else None (a version this reader does not know)."""
    version = payload.get("version", LEGACY_REGISTRY_VERSION)
    return version if version in (LEGACY_REGISTRY_VERSION, REGISTRY_VERSION) and not isinstance(version, bool) else None


def empty_payload() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "versions": {}, "aliases": {}, "history": [], "models": {}, "thresholds": {}}


def serving_block(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(models, thresholds)` in v1's shape from a v2 payload's versions and aliases (pure): each surface's champion
    under its served name, and its own cut. Unchecked here: `registry_view` checks what it serves."""
    versions = payload.get("versions") if isinstance(payload.get("versions"), dict) else {}
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), dict) else {}
    models: dict[str, Any] = {}
    thresholds: dict[str, Any] = {}
    for surface, block in sorted(aliases.items()):
        entry = versions.get(block.get("champion") or "") if isinstance(block, dict) else None
        if not isinstance(entry, dict) or entry.get("surface") != surface or not isinstance(surface, str):
            continue
        served = fine_tune_name(surface)
        models[served] = {key: entry[key] for key in ("path", "surface", "eval", "trained") if key in entry}
        if isinstance(entry.get("cut"), dict):
            thresholds[surface] = {**entry["cut"], "model": served}
    return models, thresholds


def with_serving_view(payload: dict[str, Any]) -> dict[str, Any]:
    """`payload` with its v1-shaped serving view rewritten from its versions and aliases (the writer calls it last)."""
    payload["models"], payload["thresholds"] = serving_block(payload)
    return payload


def _blank_aliases() -> dict[str, str | None]:
    return dict.fromkeys(ALIASES)


def migrate_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """THE v1 → v2 rule (pure): `(v2 payload, notes)`. Each v1 entry served under a surface's served name becomes a
    version keyed by its checkpoint folder and that surface's @champion; a v1 swept cut moves onto the version it was
    swept for. `versions` and `aliases` a pre-v2 writer left in a v1 file are kept and reconciled to what it served (a
    champion it replaced becomes @previous, one it unregistered clears, a cut it cleared goes). What cannot be carried
    (a name that serves no surface, a cut for another checkpoint) is a note, never a guess. A v2 payload comes back as
    it is."""
    if payload_version(payload) == REGISTRY_VERSION:
        return dict(payload), []
    notes: list[str] = []
    held_versions = payload.get("versions") if isinstance(payload.get("versions"), dict) else {}
    versions: dict[str, Any] = {name: dict(entry) for name, entry in held_versions.items() if isinstance(entry, dict)}
    held_aliases = payload.get("aliases") if isinstance(payload.get("aliases"), dict) else {}
    aliases: dict[str, dict] = {
        surface: {**_blank_aliases(), **{k: v for k, v in block.items() if k in ALIASES}}
        for surface, block in held_aliases.items()
        if isinstance(block, dict)
    }
    if versions:
        notes.append("kept the versions and aliases a pre-v2 writer left in place, reconciled to what it served")
    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    served: set[str] = set()
    for name, entry in sorted(models.items()):
        raw_path = entry.get("path") if isinstance(entry, dict) else None
        surface = entry.get("surface") if isinstance(entry, dict) else None
        surface = surface if isinstance(surface, str) and SURFACE_NAME.fullmatch(surface) else surface_of_name(str(name))
        if not isinstance(raw_path, str) or not raw_path or surface is None or name != fine_tune_name(surface):
            notes.append(f"dropped {name!r}: not a surface's served fine-tune")
            continue
        version = Path(raw_path).name
        if not MODEL_NAME.fullmatch(version) or version in BUILTIN_MODELS:
            notes.append(f"dropped {name!r}: its folder name {version!r} cannot name a version")
            continue
        kept = versions.get(version) or {
            "registered_at": (entry.get("trained") or {}).get("at") if isinstance(entry.get("trained"), dict) else None,
            "tags": {"gate": "MIGRATED"},
        }
        kept.update({k: v for k, v in entry.items() if k in ("path", "eval", "trained")}, surface=surface)
        cut = thresholds.get(surface)
        if isinstance(cut, dict) and cut.get("model") == name and cut.get("checkpoint") == version:
            kept["cut"] = dict(cut)
        else:
            kept.pop("cut", None)  # the v1 writer's served cut is the one that holds; none means it was cleared
            if isinstance(cut, dict):
                notes.append(f"dropped the {surface} cut: swept for {cut.get('checkpoint')!r}, not the served {version!r}")
        versions[version] = kept
        block = aliases.setdefault(surface, _blank_aliases())
        if block["champion"] not in (None, version):
            block["previous"] = block["champion"]
        block["champion"] = version
        for alias in ("challenger", "previous"):
            if block[alias] == version:
                block[alias] = None
        served.add(surface)
    for surface, block in aliases.items():
        if surface not in served and block["champion"] is not None:
            block["previous"], block["champion"] = block["champion"], None
    for surface in sorted(set(thresholds) - served):
        notes.append(f"dropped the {surface} cut: no fine-tune is served for {surface}")
    history = (
        [row for row in payload.get("history") or [] if isinstance(row, dict)] if isinstance(payload.get("history"), list) else []
    )
    out = {"version": REGISTRY_VERSION, "versions": versions, "aliases": aliases, "history": history}
    return with_serving_view(out), notes


def _resolved_folder(root: Path, raw_path: object) -> Path | None:
    """`raw_path` resolved under `root`, when it is a real folder that stays inside it."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        target = (root / raw_path).resolve()
    except (OSError, RuntimeError):
        return None
    return target if root in target.parents and target.is_dir() else None


def _version_view(root: Path, name: str, entry: object) -> dict | None:
    """One v2 version as readers see it, or None. A version's name is its checkpoint folder's; its cut must be swept
    for that folder."""
    from laya_bridge import lab_files  # lab_files imports this module

    if not isinstance(name, str) or not MODEL_NAME.fullmatch(name) or name in BUILTIN_MODELS or not isinstance(entry, dict):
        return None
    target = _resolved_folder(root, entry.get("path"))
    surface = entry.get("surface")
    if target is None or target.name != name or not isinstance(surface, str) or not SURFACE_NAME.fullmatch(surface):
        return None
    cut = lab_files.threshold_view(entry.get("cut"))
    return {
        "name": name,
        "path": str(target),
        "relative": target.relative_to(root).as_posix(),
        "checkpoint": target.name,
        "surface": surface,
        "eval": lab_files.eval_view(entry.get("eval")),
        "trained": lab_files.trained_view(entry.get("trained")),
        "registered_at": entry["registered_at"] if lab_files.is_iso(entry.get("registered_at")) else None,
        "tags": lab_files.tags_view(entry.get("tags")),
        "cut": cut if cut is not None and cut["checkpoint"] == name else None,
        "rejected_at": entry["rejected_at"] if lab_files.is_iso(entry.get("rejected_at")) else None,
    }


def _versions_of(root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("versions")
    versions = {}
    for name, entry in raw.items() if isinstance(raw, dict) else ():
        view = _version_view(root, name, entry)
        if view is not None:
            versions[name] = view
    aliases: dict[str, dict[str, str | None]] = {}
    raw_aliases = payload.get("aliases")
    for surface, block in raw_aliases.items() if isinstance(raw_aliases, dict) else ():
        if not isinstance(surface, str) or not SURFACE_NAME.fullmatch(surface) or not isinstance(block, dict):
            continue
        held = {
            alias: block.get(alias)
            if isinstance(block.get(alias), str) and block[alias] in versions and versions[block[alias]]["surface"] == surface
            else None
            for alias in ALIASES
        }
        if held["challenger"] in (held["champion"], held["previous"]):
            held["challenger"] = None  # one version holds one alias of a surface, the served one first
        if held["previous"] == held["champion"]:
            held["previous"] = None
        aliases[surface] = held
    return {"versions": versions, "aliases": aliases}


def registry_versions(base: Path | None) -> dict[str, Any]:
    """`{"version": 1 | 2 | None, "legacy": bool, "versions": {name: version view}, "aliases": {surface: {champion,
    challenger, previous}}}` under `base`. Never raises. A v1 file is read through `migrate_payload` (`legacy` True), so
    every reader sees v1's served entries as champions; an unreadable or unknown file reads as empty."""
    out: dict[str, Any] = {"version": None, "legacy": False, "versions": {}, "aliases": {}}
    if base is None:
        return out
    code, payload = _REGISTRY.get(base / MODELS_FILENAME, MODELS_CAP_BYTES, _parse_registry)
    if code != files.OK or payload is None or payload_version(payload) is None:
        return out
    try:
        root = base.resolve()
    except OSError:
        return out
    version = payload_version(payload)
    migrated = migrate_payload(payload)[0] if version == LEGACY_REGISTRY_VERSION else payload
    out.update(version=version, legacy=version == LEGACY_REGISTRY_VERSION, **_versions_of(root, migrated))
    return out


def _served_entry(view: Mapping[str, Any]) -> dict:
    return {key: view[key] for key in ("path", "relative", "checkpoint", "surface", "eval", "trained")} | {
        "version": view["name"]
    }


def _legacy_view(root: Path, payload: Mapping[str, Any]) -> dict[str, dict]:
    """v1: every model entry is served under its own name (what v1 meant), and `thresholds` as written."""
    from laya_bridge import lab_files  # lab_files imports this module

    view: dict[str, dict] = {"models": {}, "thresholds": {}, "cuts": {}}
    models = payload.get("models")
    for name, entry in models.items() if isinstance(models, dict) else ():
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name) or name in BUILTIN_MODELS:
            continue
        target = _resolved_folder(root, entry.get("path") if isinstance(entry, dict) else None)
        if target is None:
            continue
        surface = entry.get("surface")
        view["models"][name] = {
            "path": str(target),
            "relative": target.relative_to(root).as_posix(),
            "checkpoint": target.name,
            "surface": surface if isinstance(surface, str) and SURFACE_NAME.fullmatch(surface) else None,
            "eval": lab_files.eval_view(entry.get("eval")),
            "trained": lab_files.trained_view(entry.get("trained")),
            "version": target.name,
        }
    thresholds = payload.get("thresholds")
    for surface, entry in thresholds.items() if isinstance(thresholds, dict) else ():
        if isinstance(surface, str) and SURFACE_NAME.fullmatch(surface):
            checked = lab_files.threshold_view(entry)
            if checked is not None:
                view["thresholds"][surface] = checked
                view["cuts"][checked["checkpoint"]] = {"surface": surface, "cut": checked}
    return view


def registry_view(base: Path | None) -> dict[str, dict]:
    """THE serving view: `{"models": {served name: entry}, "thresholds": {surface: cut}, "cuts": {checkpoint: {surface,
    cut}}}` under `base`. Never raises.

    v2: one model per surface, its @champion, under `fine_tune_name(surface)`, from `versions` and `aliases` (the
    file's own `models` / `thresholds` are for pre-v2 readers and never read here); `thresholds` is the champion's own
    cut (its `model` the served name), and `cuts` every version's, by checkpoint folder (`thresholds_view` reads it
    when laya-serve still runs a checkpoint the alias has moved off). v1: every entry, as v1 meant (`_legacy_view`).
    A model entry is `{path (absolute), relative, checkpoint (basename), surface, eval, trained, version}`; one whose
    folder is missing or resolves outside `base` is dropped. Thresholds are checked by `lab_files.threshold_view`
    (the format owner).
    """
    view: dict[str, dict] = {"models": {}, "thresholds": {}, "cuts": {}}
    if base is None:
        return view
    code, payload = _REGISTRY.get(base / MODELS_FILENAME, MODELS_CAP_BYTES, _parse_registry)
    if code != files.OK or payload is None or payload_version(payload) is None:
        return view
    try:
        root = base.resolve()
    except OSError:
        return view
    if payload_version(payload) == LEGACY_REGISTRY_VERSION:
        return _legacy_view(root, payload)
    held = _versions_of(root, payload)
    for name, version in held["versions"].items():
        if version["cut"] is not None:
            view["cuts"][name] = {"surface": version["surface"], "cut": version["cut"]}
    for surface, aliases in held["aliases"].items():
        champion = held["versions"].get(aliases["champion"] or "")
        if champion is None:
            continue
        served = fine_tune_name(surface)
        view["models"][served] = _served_entry(champion)
        if champion["cut"] is not None:
            view["thresholds"][surface] = {**champion["cut"], "model": served}
    return view


def read_registry_view(env: Mapping[str, str] = os.environ) -> dict[str, dict]:
    """`registry_view` of the store `env` resolves to. Never raises: an unusable mapping serves no fine-tune."""
    try:
        return registry_view(laya_dir(env))
    except PathsError:
        return registry_view(None)


def thresholds_view(view: Mapping[str, Any], loaded: Mapping[str, str] | None = None) -> dict[str, dict]:
    """The swept cuts that apply to the checkpoint actually served.

    Kept only when `0 < at < 1`, its model is registered, and its checkpoint is the basename of
    `loaded[model]` when given (what laya-serve LOADED at spawn), else of the registered path. A
    cut swept for one checkpoint never moves the warning line of another. While laya-serve still runs a checkpoint
    the champion alias has since moved off (a promotion or rollback before RESTART), that checkpoint's OWN cut applies
    (`cuts`), because its weights are the ones answering.
    """
    out: dict[str, dict] = {}
    for surface, entry in view.get("thresholds", {}).items():
        model = view.get("models", {}).get(entry["model"])
        if model is None or not 0 < entry["at"] < 1:
            continue
        if loaded is not None:
            served = loaded.get(entry["model"])
            if served is None or Path(served).name != entry["checkpoint"]:
                continue
        elif entry["checkpoint"] != model["checkpoint"]:
            continue
        out[surface] = entry
    for name, served in (loaded or {}).items():
        own = view.get("cuts", {}).get(Path(served).name) if isinstance(served, str) else None
        if own is None or own["surface"] in out or name != fine_tune_name(own["surface"]) or not 0 < own["cut"]["at"] < 1:
            continue
        out[own["surface"]] = {**own["cut"], "model": name}
    return out


def read_thresholds(env: Mapping[str, str] = os.environ, *, loaded: Mapping[str, str] | None = None) -> dict[str, dict]:
    return thresholds_view(read_registry_view(env), loaded)


def read_extra_models(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """`{name: absolute checkpoint dir}` for every SERVED fine-tune (each surface's @champion). Never raises."""
    return {name: entry["path"] for name, entry in read_registry_view(env)["models"].items()}
