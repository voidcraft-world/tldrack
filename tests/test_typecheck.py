"""scripts/typecheck.py: what mypy is pointed at, how its lines are read, and what counts as a rise."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import typecheck  # noqa: E402


def test_the_targets_are_a_projects_modules_and_packages_never_its_tests(tmp_path):
    for name in ("pkg", "tests", "runtime", ".venv", "empty", "nested"):
        (tmp_path / name).mkdir()
    (tmp_path / "pkg" / "a.py").write_text("")
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / ".venv" / "x.py").write_text("")
    (tmp_path / "nested" / "pyproject.toml").write_text("")  # another project: it is checked on its own
    (tmp_path / "nested" / "b.py").write_text("")
    (tmp_path / "cli.py").write_text("")
    (tmp_path / "notes.md").write_text("")
    assert typecheck.targets(tmp_path) == ["cli.py", "pkg"]


def test_mypy_lines_are_grouped_by_file_from_the_repository_root():
    output = (
        "pkg/a.py:3: error: Incompatible types  [assignment]\n"
        'pkg/a.py:9: error: Item "None" has no attribute "get"  [union-attr]\n'
        "pkg/a.py:9: note: a note is not an error\n"
        'cli.py:1: error: Name "x" is not defined  [name-defined]\n'
        "Found 3 errors in 2 files\n"
    )
    errors = typecheck.parse(output, "plugins/demo")
    assert {path: len(lines) for path, lines in errors.items()} == {"plugins/demo/pkg/a.py": 2, "plugins/demo/cli.py": 1}
    assert errors["plugins/demo/cli.py"] == ['plugins/demo/cli.py:1: error: Name "x" is not defined  [name-defined]']


def test_a_rise_is_a_file_over_its_count_and_a_new_file_must_be_clean():
    baseline = {"a.py": 2, "b.py": 1, "gone.py": 3}
    risen, dropped = typecheck.compare({"a.py": 3, "b.py": 1, "new.py": 1}, baseline)
    assert risen == ["a.py", "new.py"]
    assert dropped == ["gone.py"]
    assert typecheck.compare({"a.py": 1}, {"a.py": 2}) == ([], ["a.py"])


def test_the_committed_baseline_names_files_this_tree_holds():
    baseline = json.loads(typecheck.BASELINE.read_text())
    assert baseline and all(isinstance(count, int) and count > 0 for count in baseline.values())
    assert [path for path in baseline if not (typecheck.ROOT / path).is_file()] == []
