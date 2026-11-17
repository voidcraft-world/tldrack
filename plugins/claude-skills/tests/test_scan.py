"""Scanner set-math and directory walking, over a synthetic ~/.claude tree."""

import json
from pathlib import Path

import pytest

from claude_skills.roots import SkillRoots, marketplace_catalog_path
from claude_skills.scan import (
    installed_plugin_names,
    installed_plugin_paths,
    is_vendored,
    mark_superseded,
    parse_supersedes,
    read_skill,
    scan_available,
    scan_global_skills,
    scan_installed_plugin_skills,
    scan_skills_dir,
    scan_synced_skills,
    select_available,
    skill_stats,
)


def write_skill(directory: Path, name: str, description: str | None = None, supersedes: str | None = None) -> Path:
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\n"
    if description is not None:
        body += f"description: {description}\n"
    if supersedes is not None:
        body += f"supersedes: {supersedes}\n"
    body += "---\n\n# heading\n"
    (skill_dir / "SKILL.md").write_text(body)
    return skill_dir


@pytest.fixture
def roots(tmp_path: Path) -> SkillRoots:
    home, project = tmp_path / "home", tmp_path / "project"
    (home / ".claude" / "skills").mkdir(parents=True)
    (project / ".claude" / "skills").mkdir(parents=True)
    (home / ".claude" / "plugins").mkdir(parents=True)
    return SkillRoots(home=home, project=project)


# ── directory walking ────────────────────────────────────────────────────────


def test_scan_skills_dir_reads_and_sorts(roots):
    write_skill(roots.global_skills, "zebra", "last alphabetically")
    write_skill(roots.global_skills, "alpha", "first alphabetically")
    records = scan_skills_dir(roots.global_skills, "global")
    assert [r["name"] for r in records] == ["alpha", "zebra"]
    assert records[0]["tier"] == "global"
    assert records[0]["description"] == "first alphabetically"
    assert records[0]["path"] == str(roots.global_skills / "alpha")


def test_missing_directory_is_empty_not_an_error(roots):
    assert scan_skills_dir(roots.global_skills / "nope", "global") == []


def test_directory_without_skill_md_is_skipped(roots):
    (roots.global_skills / "not-a-skill").mkdir()
    write_skill(roots.global_skills, "real")
    assert [r["name"] for r in scan_skills_dir(roots.global_skills, "global")] == ["real"]


def test_skill_without_frontmatter_falls_back_to_dir_name(roots):
    skill_dir = roots.global_skills / "bare"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# no frontmatter here\n")
    record = read_skill(skill_dir, "global")
    assert record["name"] == "bare"
    assert record["description"] is None


def test_plugin_skills_are_namespaced(roots):
    record = read_skill(write_skill(roots.global_skills, "deploy"), "plugin", plugin="cloud-kit")
    assert record["id"] == "cloud-kit:deploy"
    assert record["plugin"] == "cloud-kit"


# ── installed_plugins.json ───────────────────────────────────────────────────

INSTALLED_DOC = {
    "version": 2,
    "plugins": {
        "cloud-kit@claude-plugins-official": [{"installPath": "/cache/cloud-kit/1.1.0"}],
        "notes@notes": [{"installPath": "/cache/notes/0.1.0"}],
    },
}


def test_installed_plugin_names_drops_the_marketplace_suffix():
    assert installed_plugin_names(INSTALLED_DOC) == {"cloud-kit", "notes"}


def test_installed_plugin_paths_last_record_wins():
    doc = {"plugins": {"x@m": [{"installPath": "/old"}, {"installPath": "/new"}]}}
    assert installed_plugin_paths(doc) == {"x": Path("/new")}


@pytest.mark.parametrize("doc", [None, {}, {"plugins": []}, {"plugins": None}, "nonsense"])
def test_malformed_installed_doc_degrades_to_empty(doc):
    assert installed_plugin_names(doc) == set()
    assert installed_plugin_paths(doc) == {}


def test_scan_installed_plugin_skills_walks_each_install_path(roots):
    install = roots.plugins_dir / "cache" / "cloud-kit" / "1.1.0"
    write_skill(install / "skills", "deploy-stack", "Deploy things")
    write_skill(install / "skills", "read-logs", "Log things")
    roots.installed_plugins_json.write_text(
        json.dumps({"plugins": {"cloud-kit@claude-plugins-official": [{"installPath": str(install)}]}})
    )
    records = scan_installed_plugin_skills(roots)
    assert [r["id"] for r in records] == ["cloud-kit:deploy-stack", "cloud-kit:read-logs"]
    assert all(r["tier"] == "plugin" for r in records)


# ── marketplace catalog ──────────────────────────────────────────────────────


def test_is_vendored_only_for_relative_string_sources():
    assert is_vendored({"source": "./plugins/hookify"})
    assert is_vendored({"source": "./external_plugins/discord"})
    assert not is_vendored({"source": {"source": "url", "url": "https://…"}})
    assert not is_vendored({"source": {"source": "git-subdir"}})
    assert not is_vendored({})


def test_select_available_excludes_installed_and_remote():
    catalog = [
        {"name": "hookify", "source": "./plugins/hookify"},
        {"name": "cloud-kit", "source": "./plugins/cloud-kit"},  # installed
        {"name": "asana", "source": "./external_plugins/asana"},
        {"name": "remote-thing", "source": {"source": "url"}},  # not vendored
    ]
    names = [e["name"] for e in select_available(catalog, {"cloud-kit"})]
    assert names == ["asana", "hookify"]  # sorted, installed + remote dropped


def test_select_available_can_include_remote_entries():
    catalog = [{"name": "remote-thing", "source": {"source": "url"}}]
    assert select_available(catalog, set(), vendored_only=False)[0]["name"] == "remote-thing"


def test_select_available_skips_malformed_entries():
    catalog = [{"source": "./plugins/x"}, "junk", None, {"name": "", "source": "./p/y"}]
    assert select_available(catalog, set()) == []


def test_scan_available_reports_full_catalog_total(roots):
    """The vendored filter must be visible, not silent — hence the total."""
    marketplace = roots.plugins_dir / "marketplaces" / "official"
    (marketplace / ".claude-plugin").mkdir(parents=True)
    write_skill(marketplace / "plugins" / "hookify" / "skills", "hookify")
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "hookify", "source": "./plugins/hookify", "description": "Hooks", "category": "development"},
                    {"name": "cloud-kit", "source": "./plugins/cloud-kit"},
                    {"name": "remote-a", "source": {"source": "url"}},
                    {"name": "remote-b", "source": {"source": "url"}},
                ]
            }
        )
    )
    roots.known_marketplaces_json.write_text(json.dumps({"official": {"installLocation": str(marketplace)}}))
    roots.installed_plugins_json.write_text(json.dumps({"plugins": {"cloud-kit@official": [{"installPath": "/wherever"}]}}))

    records, total = scan_available(roots)
    assert total == 4  # every catalog entry counted
    assert [r["name"] for r in records] == ["hookify"]  # only the shown one
    assert records[0]["skill_count"] == 1
    assert records[0]["install_command"] == "/plugin install hookify@official"
    assert records[0]["path"] == str(marketplace / "plugins" / "hookify")


def test_scan_available_without_marketplaces_is_empty(roots):
    assert scan_available(roots) == ([], 0)


def test_marketplace_catalog_path_prefers_canonical_then_bare(tmp_path):
    base = tmp_path / "mkt"
    (base / ".claude-plugin").mkdir(parents=True)
    assert marketplace_catalog_path(base).name == "marketplace.json"

    (base / "marketplace.json").write_text("{}")
    assert marketplace_catalog_path(base) == base / "marketplace.json"

    (base / ".claude-plugin" / "marketplace.json").write_text("{}")
    assert marketplace_catalog_path(base) == base / ".claude-plugin" / "marketplace.json"


# ── supersession ─────────────────────────────────────────────────────────────


def test_parse_supersedes_splits_commas_and_spaces_and_dedupes():
    assert parse_supersedes("a:b, c:d  e:f,a:b") == ["a:b", "c:d", "e:f"]


@pytest.mark.parametrize("value", [None, "", "   ", 3, ["a:b"]])
def test_parse_supersedes_is_total(value):
    assert parse_supersedes(value) == []


def test_read_skill_carries_supersedes(roots):
    record = read_skill(write_skill(roots.project_skills, "root-cause", "x", supersedes="engineering:debug"), "project")
    assert record["supersedes"] == ["engineering:debug"]
    assert read_skill(write_skill(roots.project_skills, "plain"), "project")["supersedes"] == []


def test_mark_superseded_stamps_named_plugin_rows_only():
    plugin = [{"id": "engineering:debug"}, {"id": "engineering:standup"}]
    mark_superseded(plugin, [{"name": "root-cause", "supersedes": ["engineering:debug"]}])
    assert plugin[0]["superseded_by"] == "root-cause"
    assert plugin[1]["superseded_by"] is None


def test_mark_superseded_first_winner_keeps_a_contested_row():
    plugin = [{"id": "design:design-system"}]
    mark_superseded(
        plugin,
        [
            {"name": "global-one", "supersedes": ["design:design-system"]},
            {"name": "project-one", "supersedes": ["design:design-system"]},
        ],
    )
    assert plugin[0]["superseded_by"] == "global-one"


# ── every account, synced skills, stats ─────────────────────────────────────


def account(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / "home" / name
    (directory / "skills").mkdir(parents=True, exist_ok=True)
    (directory / "plugins").mkdir(parents=True, exist_ok=True)
    return directory


def test_config_dirs_default_first_and_deduplicated_by_real_path(tmp_path, roots):
    primary = account(tmp_path, ".claude-a")
    alias = tmp_path / "alias"
    alias.symlink_to(primary)
    multi = SkillRoots(home=roots.home, project=roots.project, extra_config_dirs=(primary, alias, roots.claude_dir))
    assert multi.config_dirs() == [roots.claude_dir, primary]


def test_a_skill_every_account_links_to_is_one_row_naming_both(tmp_path, roots):
    write_skill(roots.global_skills, "shared", "in the default account")
    primary = account(tmp_path, ".claude-a")
    (primary / "skills" / "shared").symlink_to(roots.global_skills / "shared")
    write_skill(primary / "skills", "only-primary")
    multi = SkillRoots(home=roots.home, project=roots.project, extra_config_dirs=(primary,))
    records = scan_global_skills(multi)
    assert [(r["name"], r["profiles"]) for r in records] == [
        ("only-primary", ["claude-a"]),
        ("shared", ["claude", "claude-a"]),
    ]


def test_synced_skills_are_their_own_tier_one_row_per_name_newest_copy(tmp_path, roots):
    import os

    old = write_skill(roots.global_skills / "synced" / "bucket-a", "docs", "old copy")
    new = write_skill(roots.global_skills / "synced" / "bucket-b", "docs", "new copy")
    write_skill(roots.global_skills / "synced" / "bucket-b", "pdf")
    os.utime(old / "SKILL.md", (1_000_000, 1_000_000))
    os.utime(new / "SKILL.md", (2_000_000, 2_000_000))
    (roots.global_skills / "synced" / ".bucket-a").mkdir()  # the sync's own bookkeeping
    records = scan_synced_skills(roots)
    assert [(r["id"], r["description"]) for r in records] == [
        ("anthropic-skills:docs", "new copy"),
        ("anthropic-skills:pdf", None),
    ]
    assert all(r["tier"] == "synced" for r in records)
    # …and the global walk does not mistake `synced/` for a skill.
    assert scan_global_skills(roots) == []


def test_plugins_from_every_account_merge_by_id(tmp_path, roots):
    primary = account(tmp_path, ".claude-a")
    for directory, version in ((roots.claude_dir, "1.0"), (primary, "2.0")):
        install = directory / "plugins" / "cache" / "notes" / version
        write_skill(install / "skills", "notes", f"v{version}")
        (directory / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"notes@notes": [{"installPath": str(install)}]}})
        )
    write_skill(primary / "plugins" / "cache" / "x" / "skills", "only")
    (primary / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "notes@notes": [{"installPath": str(primary / "plugins" / "cache" / "notes" / "2.0")}],
                    "x@m": [{"installPath": str(primary / "plugins" / "cache" / "x")}],
                }
            }
        )
    )
    multi = SkillRoots(home=roots.home, project=roots.project, extra_config_dirs=(primary,))
    records = scan_installed_plugin_skills(multi)
    assert [(r["id"], r["description"], r["profiles"]) for r in records] == [
        ("notes:notes", "v1.0", ["claude", "claude-a"]),
        ("x:only", None, ["claude-a"]),
    ]


def test_skill_stats_newest_file_and_skill_md_size(roots):
    import os

    skill_dir = write_skill(roots.global_skills, "statted")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.sh").write_text("echo")
    os.utime(skill_dir / "SKILL.md", (1_000_000, 1_000_000))
    os.utime(skill_dir / "scripts" / "run.sh", (3_000_000, 3_000_000))
    mtime, size = skill_stats(skill_dir)
    assert mtime == 3_000_000
    assert size == (skill_dir / "SKILL.md").stat().st_size
    record = read_skill(skill_dir, "global")
    assert (record["mtime"], record["bytes"], record["profiles"]) == (3_000_000, size, [])


def test_skill_stats_never_follows_a_symlinked_directory(tmp_path, roots):
    import os

    skill_dir = write_skill(roots.global_skills, "linked")
    os.utime(skill_dir / "SKILL.md", (1_000_000, 1_000_000))
    huge = tmp_path / "huge"
    huge.mkdir()
    (huge / "new.txt").write_text("x")
    (skill_dir / "vendor").symlink_to(huge)
    assert skill_stats(skill_dir)[0] == 1_000_000
