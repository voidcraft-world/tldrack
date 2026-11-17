"""scripts/projects.py: what a project is, for CI's matrix and the edit hook alike."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import projects  # noqa: E402
from projects import Project, find_projects, projects_of  # noqa: E402


def make_tree(root: Path) -> None:
    (root / "bridge").mkdir()
    (root / "bridge" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "bridge" / "src").mkdir()
    (root / "ui" / "node_modules" / "dep").mkdir(parents=True)
    (root / "ui" / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    (root / "ui" / "node_modules" / "dep" / "package.json").write_text("{}")
    (root / "schemas").mkdir()
    (root / "schemas" / "package.json").write_text("{}")
    (root / "both").mkdir()
    (root / "both" / "package.json").write_text("{}")
    (root / "both" / "pyproject.toml").write_text("")
    (root / "pyproject.toml").write_text("")  # the root is never a project


def test_find_projects_skips_the_root_and_installed_dependencies(tmp_path):
    make_tree(tmp_path)
    found = sorted((p.kind, p.rel(tmp_path)) for p in find_projects(tmp_path))
    assert found == [("node", "both"), ("node", "schemas"), ("node", "ui"), ("python", "both"), ("python", "bridge")]


def test_a_file_belongs_to_its_nearest_project(tmp_path):
    make_tree(tmp_path)
    assert projects_of(tmp_path / "bridge" / "src" / "x.py", tmp_path) == [Project("python", tmp_path / "bridge")]
    assert projects_of(tmp_path / "bridge" / "src" / "deleted.py", tmp_path) == [Project("python", tmp_path / "bridge")]
    assert {p.kind for p in projects_of(tmp_path / "both" / "a.ts", tmp_path)} == {"node", "python"}
    assert projects_of(tmp_path / "README.md", tmp_path) == []
    assert projects_of(tmp_path / "ui" / "node_modules" / "dep" / "i.js", tmp_path) == []
    assert projects_of(tmp_path.parent / "elsewhere.py", tmp_path) == []


def test_the_test_command_follows_the_manifest(tmp_path):
    make_tree(tmp_path)
    assert Project("python", tmp_path / "bridge").test_command() == ["uv", "run", "pytest", "-q"]
    assert Project("node", tmp_path / "ui").test_command() == ["npm", "test"]
    assert Project("node", tmp_path / "schemas").test_command() == ["node", "--test"]


def test_a_project_without_a_test_file_has_nothing_to_run(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "bridge" / "tests").mkdir()
    (tmp_path / "bridge" / "tests" / "test_x.py").write_text("")
    (tmp_path / "bridge" / "runtime").mkdir()  # a pinned environment: a manifest, no source
    (tmp_path / "bridge" / "runtime" / "pyproject.toml").write_text("")
    (tmp_path / "both" / "runtime").mkdir()
    (tmp_path / "both" / "runtime" / "pyproject.toml").write_text("")
    (tmp_path / "both" / "runtime" / "test_y.py").write_text("")  # a nested project's test is not its parent's
    (tmp_path / "schemas" / "tests").mkdir()
    (tmp_path / "schemas" / "tests" / "a.test.ts").write_text("")
    assert Project("python", tmp_path / "bridge").has_tests()
    assert not Project("python", tmp_path / "bridge" / "runtime").has_tests()
    assert not Project("python", tmp_path / "both").has_tests()
    assert Project("python", tmp_path / "both" / "runtime").has_tests()
    assert Project("node", tmp_path / "ui").has_tests()  # its own test script
    assert Project("node", tmp_path / "schemas").has_tests()
    assert not Project("node", tmp_path / "both").has_tests()


def test_the_matrix_and_run_leave_out_a_project_with_nothing_to_run(tmp_path, monkeypatch, capsys):
    make_tree(tmp_path)
    (tmp_path / "bridge" / "test_x.py").write_text("")
    assert projects.matrix(find_projects(tmp_path), tmp_path) == {"python": ["bridge"], "node": ["ui"]}
    monkeypatch.setattr(projects.subprocess, "run", lambda *a, **k: pytest.fail("a runner was started"))
    assert projects.main(["--run", str(tmp_path / "both"), "--kind", "python"]) == 0
    assert "no tests" in capsys.readouterr().out
