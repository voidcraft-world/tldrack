"""Walk the skill roots into flat records. Set math is pure and unit-tested.

A record is a plain dict so the JSON envelope needs no serialisation step:

    {"tier": "global", "id": "release-notes", "name": "release-notes",
     "description": "Drafts the next…", "path": "/Users/you/.claude/skills/release-notes",
     "plugin": null, "mtime": 1793001234, "bytes": 4120, "profiles": ["claude"]}

`mtime` is the newest file in the skill's directory (when it was last touched, for
an audit), `bytes` the SKILL.md alone (what loading it costs), and `profiles` the
accounts whose config dir holds it — one row per real directory, however many
accounts symlink to it.

`path` is the *directory* (what `POST /open` reveals in Finder), not the
SKILL.md. It is `None` for anything with no local checkout — built-in commands,
and marketplace entries sourced from a remote URL.

Tiers, and what each answers:

  global     — skills you wrote for every project
  synced     — skills your claude.ai account syncs down (`anthropic-skills:*`)
  project    — skills that belong to this repo
  plugin     — skills you get from the 7 plugins you have installed
  available  — plugins you could install but haven't ("what am I missing")

Filesystem reads are wrapped so a permission error or a half-written JSON file
degrades that tier to empty instead of failing the whole request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_skills.frontmatter import parse_frontmatter
from claude_skills.roots import SkillRoots, marketplace_catalog_path, profile_label

SKILL_FILE = "SKILL.md"
# Cap on a single description before it reaches the wire. The wall truncates
# again for display; this only stops a pathological file bloating the payload.
MAX_DESCRIPTION_CHARS = 2000
#: Claude Code's namespace for the account-synced skills under `skills/synced/`.
SYNCED_NAMESPACE = "anthropic-skills"
#: The newest-file walk stops here: a skill that vendors a node_modules is not worth a slow /state.
MAX_STAT_ENTRIES = 400
MAX_STAT_DEPTH = 4


def _read_json(path: Path):
    """Parse a JSON file, or None on any read/parse failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_skill(skill_dir: Path, tier: str, plugin: str | None = None) -> dict | None:
    """One `<dir>/SKILL.md` → a record, or None if there's no SKILL.md.

    A SKILL.md with unreadable or absent frontmatter still yields a record —
    the directory name is always a usable identity, and a skill you can't parse
    is exactly the kind of thing worth seeing on the wall.
    """
    skill_file = skill_dir / SKILL_FILE
    if not skill_file.is_file():
        return None
    try:
        meta = parse_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        meta = {}
    name = meta.get("name") or skill_dir.name
    description = (meta.get("description") or "").strip() or None
    if description and len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    mtime, size = skill_stats(skill_dir)
    return {
        "tier": tier,
        "id": f"{plugin}:{name}" if plugin else name,
        "name": name,
        "description": description,
        "path": str(skill_dir),
        "plugin": plugin,
        "supersedes": parse_supersedes(meta.get("supersedes")),
        "mtime": mtime,
        "bytes": size,
        "profiles": [],
    }


def skill_stats(skill_dir: Path) -> tuple[int | None, int | None]:
    """`(newest file mtime, SKILL.md size)` for one skill dir; None where unreadable.

    The walk is bounded (`MAX_STAT_ENTRIES`, `MAX_STAT_DEPTH`) and never follows a
    symlinked directory, so a skill that links somewhere huge costs one stat.
    """
    try:
        size: int | None = (skill_dir / SKILL_FILE).stat().st_size
    except OSError:
        size = None
    newest: float | None = None
    seen = 0
    stack: list[tuple[Path, int]] = [(skill_dir, 0)]
    while stack and seen < MAX_STAT_ENTRIES:
        directory, depth = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_STAT_ENTRIES:
                break
            try:
                if entry.is_dir():
                    if not entry.is_symlink() and depth + 1 < MAX_STAT_DEPTH and not entry.name.startswith("."):
                        stack.append((entry, depth + 1))
                    continue
                stamp = entry.stat().st_mtime
            except OSError:
                continue
            newest = stamp if newest is None else max(newest, stamp)
    return (int(newest) if newest is not None else None), size


# ── supersession ─────────────────────────────────────────────────────────────
#
# A skill you wrote can declare which installed plugin skills it replaces:
#
#     supersedes: engineering:debug, engineering:tech-debt
#
# The declaration lives on the WINNER because that is the file you own. A plugin's
# SKILL.md is rewritten on every plugin update, so a note there would not survive,
# and a table in this plugin would be a second owner of what the skill already says.
# The wall reads the result as `superseded_by` on the plugin's row.


def parse_supersedes(value) -> list[str]:
    """PURE — `"a:b, c:d"` (commas and/or spaces) → `["a:b", "c:d"]`, deduplicated, in order."""
    if not isinstance(value, str):
        return []
    seen: list[str] = []
    for token in value.replace(",", " ").split():
        if token not in seen:
            seen.append(token)
    return seen


def mark_superseded(plugin_skills: list[dict], winners: list[dict]) -> None:
    """PURE (mutates) — stamp `superseded_by` on each plugin record a winner names.

    The first winner wins on a conflict, and the caller passes global before
    project, so a repo skill never silently takes a row a global one claimed.
    """
    claims: dict[str, str] = {}
    for winner in winners:
        for target in winner.get("supersedes") or []:
            claims.setdefault(target, winner["name"])
    for record in plugin_skills:
        record["superseded_by"] = claims.get(record["id"])


def scan_skills_dir(skills_dir: Path, tier: str, plugin: str | None = None) -> list[dict]:
    """Every `<skills_dir>/*/SKILL.md`, sorted by name. Missing dir → []."""
    try:
        entries = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    records = [read_skill(entry, tier, plugin) for entry in entries]
    return [r for r in records if r is not None]


# ── every account ────────────────────────────────────────────────────────────


def merge_accounts(per_account: list[tuple[str, list[dict]]], key) -> list[dict]:
    """PURE (mutates the kept rows) — one row per `key(record)`, first account wins.

    Each kept row's `profiles` lists every account that holds it, in account order.
    The default account comes first, so a skill every account symlinks to reads as
    the one directory it is, and the order is stable.
    """
    kept: dict[str, dict] = {}
    for label, records in per_account:
        for record in records:
            existing = kept.setdefault(key(record), record)
            if label not in existing["profiles"]:
                existing["profiles"].append(label)
    return sorted(kept.values(), key=lambda r: (r["name"].lower(), r["id"]))


def real_dir(record: dict) -> str:
    return os.path.realpath(record["path"])


def scan_global_skills(roots: SkillRoots) -> list[dict]:
    """Every account's `skills/*/SKILL.md`, one row per real directory."""
    return merge_accounts(
        [(profile_label(d), scan_skills_dir(d / "skills", "global")) for d in roots.config_dirs()],
        real_dir,
    )


def scan_synced_skills(roots: SkillRoots) -> list[dict]:
    """The claude.ai account skills under `skills/synced/<bucket>/`, one row per name.

    Buckets are per signed-in account and two of them often hold the same skill
    (`docs` in each), so the newest copy stands for the name.
    """
    per_account: list[tuple[str, list[dict]]] = []
    for config_dir in roots.config_dirs():
        try:
            buckets = sorted(p for p in (config_dir / "skills" / "synced").iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            buckets = []
        records = [r for bucket in buckets for r in scan_skills_dir(bucket, "synced", SYNCED_NAMESPACE)]
        records.sort(key=lambda r: -(r["mtime"] or 0))
        per_account.append((profile_label(config_dir), records))
    return merge_accounts(per_account, lambda r: r["id"])


# ── installed plugins ────────────────────────────────────────────────────────


def installed_plugin_names(data) -> set[str]:
    """PURE — plugin names from an `installed_plugins.json` document.

    Keys are `"<plugin>@<marketplace>"`; the marketplace half is dropped because
    the catalog indexes by bare name. Tolerates a missing/!dict document.
    """
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return set()
    return {key.split("@", 1)[0] for key in plugins if isinstance(key, str) and key}


def installed_plugin_paths(data) -> dict[str, Path]:
    """PURE — plugin name → its newest `installPath`.

    A plugin may hold several install records (different scopes or versions);
    the last one listed wins, which matches Claude Code's own append order.
    """
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    result: dict[str, Path] = {}
    for key, records in plugins.items():
        if not isinstance(key, str) or not isinstance(records, list):
            continue
        for record in records:
            install_path = record.get("installPath") if isinstance(record, dict) else None
            if isinstance(install_path, str) and install_path:
                result[key.split("@", 1)[0]] = Path(install_path)
    return result


def scan_installed_plugin_skills(roots: SkillRoots) -> list[dict]:
    """Skills contributed by every account's installed plugins, namespaced `plugin:skill`.

    One row per id: two accounts on different versions of a plugin show the
    default account's copy, with both accounts in `profiles`.
    """
    per_account: list[tuple[str, list[dict]]] = []
    for config_dir in roots.config_dirs():
        data = _read_json(config_dir / "plugins" / "installed_plugins.json")
        records: list[dict] = []
        for plugin_name, install_path in sorted(installed_plugin_paths(data).items()):
            records.extend(scan_skills_dir(install_path / "skills", "plugin", plugin_name))
        per_account.append((profile_label(config_dir), records))
    merged = merge_accounts(per_account, lambda r: r["id"])
    return sorted(merged, key=lambda r: (r["plugin"] or "", r["name"]))


# ── marketplace catalog ──────────────────────────────────────────────────────


def is_vendored(entry) -> bool:
    """PURE — True when a catalog entry's source is a path inside the checkout.

    Vendored entries (`"source": "./plugins/foo"`) are the first-party ones
    Anthropic ships in the marketplace repo itself, so their skills are already
    on disk and revealable. The other 220 are remote (`url` / `git-subdir` /
    `github` dicts) and would need a network fetch to describe.
    """
    source = entry.get("source") if isinstance(entry, dict) else None
    return isinstance(source, str) and source.startswith("./")


def select_available(catalog: list, installed: set[str], vendored_only: bool = True) -> list[dict]:
    """PURE — catalog entries that are NOT installed, newest-first by name.

    This is the "what could I be using" set. `vendored_only` keeps it to the
    first-party plugins whose files are on disk; the caller reports the full
    catalog size alongside so the filtering is visible rather than silent.
    """
    result = []
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in installed:
            continue
        if vendored_only and not is_vendored(entry):
            continue
        result.append(entry)
    return sorted(result, key=lambda e: e["name"])


def catalog_entry_dir(install_location: Path, entry: dict) -> Path | None:
    """The on-disk dir of a vendored catalog entry (`./plugins/foo` → …/plugins/foo)."""
    if not is_vendored(entry):
        return None
    relative = entry["source"].lstrip("./")
    return install_location / relative


def scan_available(roots: SkillRoots, vendored_only: bool = True) -> tuple[list[dict], int]:
    """Uninstalled marketplace plugins as records, plus the FULL catalog size.

    Returns `(records, total_catalog_entries)`. The second value is what lets
    the wall say "N shown of M available" instead of quietly truncating.
    Each record's `description` is the plugin's own catalog description, and
    `skill_count` says how many skills installing it would bring.
    """
    marketplaces = _read_json(roots.known_marketplaces_json)
    if not isinstance(marketplaces, dict):
        return [], 0
    installed = installed_plugin_names(_read_json(roots.installed_plugins_json))
    records: list[dict] = []
    total = 0
    for marketplace_name, config in sorted(marketplaces.items()):
        install_location = (config or {}).get("installLocation") if isinstance(config, dict) else None
        if not isinstance(install_location, str):
            continue
        catalog_doc = _read_json(marketplace_catalog_path(install_location))
        entries = catalog_doc.get("plugins") if isinstance(catalog_doc, dict) else None
        if not isinstance(entries, list):
            continue
        total += len(entries)
        base = Path(install_location)
        for entry in select_available(entries, installed, vendored_only):
            entry_dir = catalog_entry_dir(base, entry)
            skills_dir = (entry_dir / "skills") if entry_dir else None
            skill_count = len(scan_skills_dir(skills_dir, "available")) if skills_dir else 0
            description = entry.get("description")
            records.append(
                {
                    "tier": "available",
                    "id": f"{marketplace_name}:{entry['name']}",
                    "name": entry["name"],
                    "description": description if isinstance(description, str) else None,
                    "path": str(entry_dir) if entry_dir and entry_dir.is_dir() else None,
                    "plugin": entry["name"],
                    "marketplace": marketplace_name,
                    "category": entry.get("category"),
                    "skill_count": skill_count,
                    "install_command": f"/plugin install {entry['name']}@{marketplace_name}",
                }
            )
    return records, total
