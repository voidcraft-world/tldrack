"""The skills linker: plan, apply, idempotency, and the rule that nothing of the operator's is touched."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_skills.link import (
    CREATE,
    CURRENT,
    PRUNE,
    REMOVE,
    REPLACE,
    SKIP,
    apply_links,
    default_dests,
    main,
    plan_links,
    source_skills,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "link-skills"


def make_source(tmp_path: Path, *names: str) -> Path:
    source = tmp_path / "checkout" / ".claude" / "skills"
    for name in names:
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    source.mkdir(parents=True, exist_ok=True)
    return source


def actions(steps) -> dict[str, str]:
    return {step.link.name: step.action for step in steps}


def link_all(source: Path, *dests: Path, **kwargs):
    results = apply_links(plan_links(source, list(dests), **kwargs))
    assert [error for _, error in results if error is not None] == []
    return [step for step, _ in results]


# ── what counts as a skill ───────────────────────────────────────────────────


def test_only_directories_holding_skill_md_are_skills(tmp_path):
    source = make_source(tmp_path, "my-skill", "release-notes")
    (source / "_shared").mkdir()  # helpers, no SKILL.md
    (source / ".hidden").mkdir()
    (source / ".hidden" / "SKILL.md").write_text("x")
    (source / "README.md").write_text("x")
    assert source_skills(source) == ["my-skill", "release-notes"]


def test_a_missing_source_has_no_skills(tmp_path):
    assert source_skills(tmp_path / "nowhere") == []


# ── linking ──────────────────────────────────────────────────────────────────


def test_each_skill_gets_an_absolute_link_and_the_destination_is_created(tmp_path):
    source = make_source(tmp_path, "my-skill", "release-notes")
    dest = tmp_path / "home" / ".claude" / "skills"
    steps = link_all(source, dest)
    assert actions(steps) == {"my-skill": CREATE, "release-notes": CREATE}
    for name in ("my-skill", "release-notes"):
        assert (dest / name).is_symlink()
        assert os.readlink(dest / name) == str(source / name)
        assert (dest / name / "SKILL.md").is_file()


def test_a_second_run_is_current_and_changes_nothing(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    link_all(source, dest)
    before = os.lstat(dest / "my-skill").st_mtime_ns
    assert actions(link_all(source, dest)) == {"my-skill": CURRENT}
    assert os.lstat(dest / "my-skill").st_mtime_ns == before


def test_a_relative_link_to_the_same_skill_is_current(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(os.path.relpath(source / "my-skill", dest), dest / "my-skill")
    assert actions(plan_links(source, [dest])) == {"my-skill": CURRENT}


def test_every_destination_is_linked_once_per_real_directory(tmp_path):
    source = make_source(tmp_path, "my-skill")
    first, second = tmp_path / "a" / "skills", tmp_path / "b" / "skills"
    first.mkdir(parents=True)
    os.symlink(tmp_path / "a", tmp_path / "a-alias")
    steps = plan_links(source, [first, second, tmp_path / "a-alias" / "skills", first])
    assert [step.link for step in steps] == [first / "my-skill", second / "my-skill"]


def test_the_source_is_never_its_own_destination(tmp_path):
    source = make_source(tmp_path, "my-skill")
    steps = plan_links(source, [source])
    assert [(step.action, step.reason) for step in steps] == [(SKIP, "the destination is the source")]


# ── nothing of the operator's is touched ─────────────────────────────────────


@pytest.mark.parametrize("force", [False, True])
def test_a_real_directory_or_file_is_skipped_even_with_force(tmp_path, force):
    source = make_source(tmp_path, "my-skill", "release-notes")
    dest = tmp_path / "dest"
    (dest / "my-skill").mkdir(parents=True)
    (dest / "my-skill" / "SKILL.md").write_text("the operator's own")
    (dest / "release-notes").write_text("a file")
    steps = link_all(source, dest, force=force)
    assert actions(steps) == {"my-skill": SKIP, "release-notes": SKIP}
    assert all("not a link" in step.reason for step in steps)
    assert (dest / "my-skill" / "SKILL.md").read_text() == "the operator's own"
    assert (dest / "release-notes").read_text() == "a file"


def test_a_link_pointing_elsewhere_is_skipped_and_reported(tmp_path):
    source = make_source(tmp_path, "my-skill")
    elsewhere = tmp_path / "theirs" / "my-skill"
    elsewhere.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(elsewhere, dest / "my-skill")
    [step] = link_all(source, dest)
    assert step.action == SKIP and str(elsewhere) in step.reason
    assert os.readlink(dest / "my-skill") == str(elsewhere)


def test_force_replaces_only_that_link(tmp_path):
    source = make_source(tmp_path, "my-skill")
    elsewhere = tmp_path / "theirs" / "my-skill"
    elsewhere.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(elsewhere, dest / "my-skill")
    [step] = link_all(source, dest, force=True)
    assert step.action == REPLACE
    assert os.readlink(dest / "my-skill") == str(source / "my-skill")
    assert elsewhere.is_dir()
    assert sorted(os.listdir(dest)) == ["my-skill"]  # no temporary left behind


def test_a_stale_link_into_the_source_is_ours_and_re_pointed_without_force(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(source / "old-name", dest / "my-skill")
    [step] = link_all(source, dest)
    assert step.action == REPLACE
    assert (dest / "my-skill" / "SKILL.md").is_file()


def test_a_link_whose_skill_is_gone_from_the_source_is_pruned(tmp_path):
    source = make_source(tmp_path, "my-skill", "renamed")
    dest = tmp_path / "dest"
    link_all(source, dest)
    os.rename(source / "renamed", source / "release-notes")
    steps = link_all(source, dest)
    assert actions(steps) == {"my-skill": CURRENT, "release-notes": CREATE, "renamed": PRUNE}
    assert sorted(os.listdir(dest)) == ["my-skill", "release-notes"]


def test_a_foreign_dangling_link_is_never_pruned(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(tmp_path / "theirs" / "gone", dest / "theirs")
    assert "theirs" not in actions(link_all(source, dest))
    assert (dest / "theirs").is_symlink()


# ── remove ───────────────────────────────────────────────────────────────────


def test_remove_deletes_only_links_into_the_source(tmp_path):
    source = make_source(tmp_path, "my-skill", "release-notes")
    dest = tmp_path / "dest"
    link_all(source, dest)
    (dest / "own-dir").mkdir()
    os.symlink(tmp_path, dest / "theirs")
    steps = link_all(source, dest, remove=True)
    assert actions(steps) == {"my-skill": REMOVE, "release-notes": REMOVE}
    assert sorted(os.listdir(dest)) == ["own-dir", "theirs"]
    assert source_skills(source) == ["my-skill", "release-notes"]  # the source is untouched


def test_remove_works_after_the_checkout_is_gone(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    link_all(source, dest)
    os.rename(tmp_path / "checkout", tmp_path / "moved")
    assert actions(link_all(source, dest, remove=True)) == {"my-skill": REMOVE}
    assert os.listdir(dest) == []


# ── apply reports, never raises ──────────────────────────────────────────────


def test_a_link_that_changed_since_the_plan_is_left_alone(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    link_all(source, dest)
    steps = plan_links(source, [dest], remove=True)
    os.unlink(dest / "my-skill")
    (dest / "my-skill").mkdir()
    [(_, error)] = apply_links(steps)
    assert error == "no longer a link, left alone"
    assert (dest / "my-skill").is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_destination_is_a_failure_not_a_crash(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    dest.mkdir()
    dest.chmod(0o500)
    try:
        [(step, error)] = apply_links(plan_links(source, [dest]))
    finally:
        dest.chmod(0o700)
    assert step.action == CREATE and error is not None and "PermissionError" in error


# ── the command line ─────────────────────────────────────────────────────────


def run_main(*argv: str, env=None, home=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=env or {}, home=home, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_the_default_destinations_follow_claude_config_dir(tmp_path):
    home = tmp_path / "home"
    assert default_dests({}, home) == [home / ".claude" / "skills"]
    assert default_dests({"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, home) == [
        home / ".claude" / "skills",
        tmp_path / "cfg" / "skills",
    ]
    assert default_dests({"CLAUDE_CONFIG_DIR": str(home / ".claude")}, home) == [home / ".claude" / "skills"]


def test_main_links_into_the_defaults_and_exits_zero(tmp_path):
    source = make_source(tmp_path, "my-skill")
    home = tmp_path / "home"
    code, out, _ = run_main("--source", str(source), env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, home=home)
    assert code == 0
    assert (home / ".claude" / "skills" / "my-skill").is_symlink()
    assert (tmp_path / "cfg" / "skills" / "my-skill").is_symlink()
    assert out.splitlines()[-1] == "link-skills: 2 created"


def test_dry_run_prints_the_plan_and_changes_nothing(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    code, out, _ = run_main("--source", str(source), "--dest", str(dest), "--dry-run")
    assert code == 0
    assert out.startswith("would create") and "link-skills: 1 to create" in out
    assert not dest.exists()


def test_skips_are_reported_but_are_not_failures(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    (dest / "my-skill").mkdir(parents=True)
    code, out, _ = run_main("--source", str(source), "--dest", str(dest))
    assert code == 0
    assert out.startswith("skipped: a directory that is not a link")


def test_json_carries_every_step(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    code, out, _ = run_main("--source", str(source), "--dest", str(dest), "--json")
    report = json.loads(out)
    assert code == 0 and report["failed"] == 0 and report["dry_run"] is False
    assert report["steps"] == [
        {"action": "create", "link": str(dest / "my-skill"), "target": str(source / "my-skill"), "reason": None, "error": None}
    ]


def test_a_failure_exits_one(tmp_path):
    source = make_source(tmp_path, "my-skill")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the destination should be")
    code, out, _ = run_main("--source", str(source), "--dest", str(blocker / "skills"))
    assert code == 1 and "failed:" in out


def test_no_source_exits_two_unless_removing(tmp_path):
    code, _, err = run_main("--source", str(tmp_path / "nowhere"), "--dest", str(tmp_path / "dest"))
    assert code == 2 and "no skills directory" in err
    assert run_main("--source", str(tmp_path / "nowhere"), "--dest", str(tmp_path / "dest"), "--remove")[0] == 0


def test_the_script_runs_with_any_python_and_no_venv(tmp_path):
    source = make_source(tmp_path, "my-skill")
    dest = tmp_path / "dest"
    env = {key: value for key, value in os.environ.items() if key not in ("VIRTUAL_ENV", "PYTHONPATH")}
    result = subprocess.run(
        [str(SCRIPT), "--source", str(source), "--dest", str(dest), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["steps"][0]["action"] == "create"
    assert (dest / "my-skill").is_symlink()


def test_the_default_source_is_this_checkouts_skills(tmp_path):
    from claude_skills.link import default_source

    assert default_source() == Path(__file__).resolve().parents[3] / ".claude" / "skills"
