"""Which projects this repository holds, and which one a file belongs to.

One owner for the question, because two callers ask it: CI (`--github-output`
builds the test matrix) and the edit hook (`.claude/hooks/affected_tests.py` runs
the project a changed file belongs to). A project is a directory with its own
manifest, below the root:

- Python: `pyproject.toml`, tested with `uv run pytest`.
- Node: `package.json`, tested with `npm test` when it names a test script,
  otherwise `node --test`.

A project that holds no test file (a pinned environment such as
`plugins/laya/runtime`: a manifest and a lockfile, no source) is still a project,
but it has nothing to run: CI's matrix leaves it out and `--run` passes without
starting a runner, which would only install its dependencies to fail on a missing
`pytest`.

Anything outside a project belongs to the root, whose only test is the purity
test. Stdlib only: it runs before anything is installed.

    python3 scripts/projects.py                   # one line per project
    python3 scripts/projects.py --github-output   # python=[…] and node=[…] for a matrix
    python3 scripts/projects.py --run ui --kind node   # that project's tests (CI and the hook)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories that hold someone else's manifests (installed dependencies, build
# output) or no source at all. A manifest under one of these is never a project.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
        ".gradle",
        ".vite",
        "coverage",
    }
)

MANIFESTS = {"pyproject.toml": "python", "package.json": "node"}

# What a test file is called, per kind: pytest's own two patterns, and node's `*.test.*`.
TEST_FILES = {"python": ("test_*.py", "*_test.py"), "node": ("*.test.*",)}


@dataclass(frozen=True)
class Project:
    kind: str  # "python" | "node"
    path: Path  # absolute

    @property
    def manifest(self) -> Path:
        return self.path / next(name for name, kind in MANIFESTS.items() if kind == self.kind)

    def rel(self, root: Path = ROOT) -> str:
        return self.path.relative_to(root).as_posix()

    def test_command(self) -> list[str]:
        if self.kind == "python":
            return ["uv", "run", "pytest", "-q"]
        return ["npm", "test"] if has_test_script(self.path) else ["node", "--test"]

    def has_tests(self) -> bool:
        """A test script of its own, or a test file below it that no nested project owns."""
        if self.kind == "node" and has_test_script(self.path):
            return True
        for current, dirs, files in os.walk(self.path):
            here = Path(current)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not projects_at(here / d)]
            if any(fnmatch.fnmatch(name, pattern) for name in files for pattern in TEST_FILES[self.kind]):
                return True
        return False


def has_test_script(directory: Path) -> bool:
    try:
        manifest = json.loads((directory / "package.json").read_text())
    except (OSError, ValueError):
        return False
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get("test"), str)


def projects_at(directory: Path) -> list[Project]:
    """One project per manifest in `directory`: a Node app with a pyproject is both."""
    return [Project(kind, directory) for manifest, kind in MANIFESTS.items() if (directory / manifest).is_file()]


def find_projects(root: Path = ROOT) -> list[Project]:
    found: list[Project] = []
    for current, dirs, _ in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if Path(current) != root:
            found += projects_at(Path(current))
    return found


def projects_of(path: Path, root: Path = ROOT) -> list[Project]:
    """The projects of the nearest manifest directory above `path`; [] means the root's."""
    path = path.resolve()
    root = root.resolve()
    if root not in path.parents or set(path.relative_to(root).parts) & SKIP_DIRS:
        return []
    for directory in [path, *path.parents] if path.is_dir() else path.parents:
        if directory == root:
            return []
        if found := projects_at(directory):
            return found
    return []


def matrix(projects: list[Project], root: Path = ROOT) -> dict[str, list[str]]:
    """CI's test matrix: per kind, the projects that have something to run."""
    return {kind: [p.rel(root) for p in projects if p.kind == kind and p.has_tests()] for kind in ("python", "node")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--github-output", action="store_true", help="append python=[…] and node=[…] to $GITHUB_OUTPUT (stdout without it)"
    )
    parser.add_argument("--run", type=Path, metavar="DIR", help="run the tests of the project at DIR")
    parser.add_argument("--kind", choices=sorted(set(MANIFESTS.values())), help="with --run: which manifest")
    args = parser.parse_args(argv)
    if args.run:
        found = [p for p in projects_at(args.run.resolve()) if args.kind in (None, p.kind)]
        if len(found) != 1:
            parser.error(f"{args.run}: {len(found)} matching projects; name one with --kind")
        if not found[0].has_tests():
            print(f"{args.run}: no tests")
            return 0
        return subprocess.run(found[0].test_command(), cwd=found[0].path, check=False).returncode
    projects = find_projects()
    if not args.github_output:
        for project in projects:
            print(f"{project.kind:7} {project.rel()}")
        return 0
    lines = [f"{kind}={json.dumps(paths)}" for kind, paths in matrix(projects).items()]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as out:
            out.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
