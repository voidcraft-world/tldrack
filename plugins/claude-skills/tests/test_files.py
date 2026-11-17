"""GET /skill — every test past the first is an escape attempt or a cap.

`handle_skill` hands file text to a browser page, so its two containment checks (the skill dir
inside the scan roots, the file inside the skill dir) are the security boundary of the route.
"""

from pathlib import Path

import pytest

from claude_skills import files
from claude_skills.files import handle_skill


@pytest.fixture
def skill(tmp_path: Path):
    root = tmp_path / "claude" / "skills"
    skill_dir = root / "audit-me"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: audit-me\n---\n# body\n")
    (skill_dir / "scripts" / "go.sh").write_text("echo go\n")
    (skill_dir / "reference.md").write_text("ref\n")
    (tmp_path / "secret.txt").write_text("do not read\n")
    return root, skill_dir, tmp_path


def ask(path, file=None):
    query = {"path": [str(path)]}
    if file is not None:
        query["file"] = [file]
    return query


def test_skill_md_and_the_file_list_skill_md_first(skill):
    root, skill_dir, _ = skill
    status, body = handle_skill(ask(skill_dir), [root])
    assert status == 200
    assert body["file"] == "SKILL.md" and body["text"].startswith("---") and not body["truncated"]
    assert [f["rel"] for f in body["files"]] == ["SKILL.md", "reference.md", "scripts/go.sh"]


def test_another_file_in_the_skill(skill):
    root, skill_dir, _ = skill
    status, body = handle_skill(ask(skill_dir, "scripts/go.sh"), [root])
    assert (status, body["text"]) == (200, "echo go\n")


def test_a_skill_dir_outside_the_roots_is_refused(skill):
    root, _, tmp_path = skill
    assert handle_skill(ask(tmp_path), [root])[0] == 403


@pytest.mark.parametrize("rel", ["../../../secret.txt", "/etc/hosts"])
def test_a_file_outside_the_skill_is_refused(skill, rel):
    root, skill_dir, _ = skill
    assert handle_skill(ask(skill_dir, rel), [root])[0] == 403


def test_a_symlink_inside_the_skill_is_judged_on_where_it_points(skill):
    root, skill_dir, tmp_path = skill
    (skill_dir / "leak.txt").symlink_to(tmp_path / "secret.txt")
    assert handle_skill(ask(skill_dir, "leak.txt"), [root])[0] == 403


def test_a_symlinked_skill_dir_is_judged_on_its_real_location(skill):
    root, _, tmp_path = skill
    (root / "sneaky").symlink_to(tmp_path)
    assert handle_skill(ask(root / "sneaky"), [root])[0] == 403


def test_missing_file_binary_and_cap(skill, monkeypatch):
    root, skill_dir, _ = skill
    assert handle_skill(ask(skill_dir, "nope.md"), [root])[0] == 404
    (skill_dir / "blob.bin").write_bytes(b"\x00\x01binary")
    assert handle_skill(ask(skill_dir, "blob.bin"), [root])[0] == 415
    monkeypatch.setattr(files, "MAX_TEXT_BYTES", 4)
    status, body = handle_skill(ask(skill_dir, "reference.md"), [root])
    assert (status, body["text"], body["truncated"]) == (200, "ref\n", False)
    status, body = handle_skill(ask(skill_dir), [root])
    assert (body["text"], body["truncated"]) == ("---\n", True)


def test_the_listing_is_capped_and_says_so(skill, monkeypatch):
    root, skill_dir, _ = skill
    monkeypatch.setattr(files, "MAX_FILES", 2)
    body = handle_skill(ask(skill_dir), [root])[1]
    assert len(body["files"]) == 2 and body["files_truncated"] is True
