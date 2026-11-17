"""PURE — where Claude Code keeps skills, derived from a HOME and a project dir.

Everything is computed from its inputs so the whole layout can be pointed at a
tmpdir in tests. Nothing here touches the filesystem except `exists()`-style
checks the caller makes later and the realpath dedupe in `config_dirs()`.

The layout on disk (Claude Code 2.x):

    ~/.claude/skills/<slug>/SKILL.md                          global
    ~/.claude/skills/synced/<bucket>/<slug>/SKILL.md          synced (claude.ai account skills)
    <project>/.claude/skills/<slug>/SKILL.md                  project
    ~/.claude/plugins/installed_plugins.json                  which plugins are installed
    ~/.claude/plugins/cache/<mkt>/<plugin>/<ver>/skills/…     installed plugin skills
    ~/.claude/plugins/known_marketplaces.json                 marketplace → installLocation
    <installLocation>/.claude-plugin/marketplace.json         the marketplace's catalog

`~/.claude` is only the DEFAULT account's config dir. Every other launch profile
(`~/.claude-a`, `~/.claude-c`, …) has its own `skills/` and `plugins/`,
and an agent in that account sees those, not the default's. `extra_config_dirs`
carries them (the bridge's `profiles.claude_roots()`), and every per-account tree
below is taken over `config_dirs()`.

`reveal_allowlist()` is the security boundary for `POST /open` and `GET /skill`:
a target must resolve inside one of these trees. `plugins_dir` deliberately
covers both the install cache and the marketplace checkouts, since both live under it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillRoots:
    home: Path
    project: Path
    #: Config dirs of the OTHER Claude accounts; `claude_dir` is always first anyway.
    extra_config_dirs: tuple[Path, ...] = ()

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    def config_dirs(self) -> list[Path]:
        """Every Claude config dir, default first, deduplicated by real path."""
        seen: set[str] = set()
        result: list[Path] = []
        for directory in (self.claude_dir, *self.extra_config_dirs):
            real = os.path.realpath(directory)
            if real not in seen:
                seen.add(real)
                result.append(Path(directory))
        return result

    @property
    def global_skills(self) -> Path:
        return self.claude_dir / "skills"

    @property
    def project_skills(self) -> Path:
        return self.project / ".claude" / "skills"

    @property
    def plugins_dir(self) -> Path:
        return self.claude_dir / "plugins"

    @property
    def installed_plugins_json(self) -> Path:
        return self.plugins_dir / "installed_plugins.json"

    @property
    def known_marketplaces_json(self) -> Path:
        return self.plugins_dir / "known_marketplaces.json"

    def reveal_allowlist(self) -> list[Path]:
        """The only trees `POST /open` may reveal, and `GET /skill` read, a path inside."""
        trees = [self.project_skills]
        for directory in self.config_dirs():
            trees += [directory / "skills", directory / "plugins"]
        return trees


def profile_label(config_dir: Path) -> str:
    """PURE — the account a config dir belongs to, as the rack names it: `.claude-a` → `claude-a`."""
    return config_dir.name.lstrip(".") or config_dir.name


def default_roots(start_dir: str | Path, profiles: object = None) -> SkillRoots:
    """Roots for a live bridge: real HOME + the bridge's --start-dir (repo root), plus every
    config dir a Claude launch profile points at. A bridge without profiles scans `~/.claude` only."""
    claude_roots = getattr(profiles, "claude_roots", None)
    try:
        extra = tuple(Path(root) for root in claude_roots()) if callable(claude_roots) else ()
    except Exception:  # noqa: BLE001 — a profile registry hiccup must not cost the default scan
        extra = ()
    return SkillRoots(home=Path.home(), project=Path(start_dir), extra_config_dirs=extra)


def marketplace_catalog_path(install_location: str | Path) -> Path:
    """A marketplace checkout's catalog file.

    Both layouts occur — the canonical `.claude-plugin/marketplace.json` and a bare
    `marketplace.json` at the checkout root, which some marketplaces use. Prefer the canonical one, fall back to the bare one, and
    return the canonical path when neither exists so errors name the right file.
    """
    base = Path(install_location)
    canonical = base / ".claude-plugin" / "marketplace.json"
    if canonical.is_file():
        return canonical
    bare = base / "marketplace.json"
    if bare.is_file():
        return bare
    return canonical
