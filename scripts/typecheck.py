"""mypy over every Python project, held to a per-file count that only goes down.

The tree was not written under a type checker, so it does not start at zero. Waiting for zero
would leave the check off; a ratchet turns it on today. `tests/mypy-baseline.json` records how
many errors each file has. A file whose count RISES fails, and the new errors are printed; a file
the baseline does not name must be clean. When a count drops, `--tighten` banks it, so the
ground gained is kept. `mypy.ini` at the root is the one configuration; the version is pinned
here, because a newer mypy finds different errors.

A project is what `scripts/projects.py` says it is. Its tests are not checked: they are full of
fakes typed loosely on purpose.

    python3 scripts/typecheck.py                 # every project against the baseline (exit 1 on a rise)
    python3 scripts/typecheck.py --run bridge    # one project (CI's matrix)
    python3 scripts/typecheck.py --tighten       # bank every drop into the baseline
    python3 scripts/typecheck.py --record        # write today's counts as they are (a rise too: say why in the commit)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from projects import ROOT, SKIP_DIRS, find_projects, projects_at  # noqa: E402

MYPY = "mypy==1.19.1"
BASELINE = ROOT / "tests" / "mypy-baseline.json"
CONFIG = ROOT / "mypy.ini"
#: Directories of a project that hold no source of its own.
NOT_SOURCE = SKIP_DIRS | {"tests", "runtime"}
ERROR = re.compile(r"^(?P<path>[^:\n]+\.py):(?P<line>\d+): error: (?P<text>.*)$")


def targets(project: Path) -> list[str]:
    """What mypy is pointed at: the project's top-level modules and packages, never its tests."""
    found = []
    for entry in sorted(project.iterdir()):
        if entry.name.startswith(".") or entry.name in NOT_SOURCE:
            continue
        if entry.is_file() and entry.suffix == ".py" or entry.is_dir() and not projects_at(entry) and any(entry.rglob("*.py")):
            found.append(entry.name)
    return found


def parse(output: str, prefix: str) -> dict[str, list[str]]:
    """mypy's lines as `{file: [error line, …]}`, each file named from the repository root."""
    errors: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = ERROR.match(line)
        if match:
            errors.setdefault(f"{prefix}/{match['path']}", []).append(f"{prefix}/{line}")
    return errors


def compare(counts: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """`(risen, dropped)`: the files over their baseline, and the ones under it."""
    risen = sorted(path for path, count in counts.items() if count > baseline.get(path, 0))
    dropped = sorted(path for path, allowed in baseline.items() if counts.get(path, 0) < allowed)
    return risen, dropped


def check(project: Path) -> dict[str, list[str]] | None:
    """One project's errors by file; None when mypy itself could not run."""
    found = targets(project)
    if not found:
        return {}
    command = [
        "uv",
        "run",
        "--with",
        MYPY,
        "mypy",
        "--config-file",
        str(CONFIG),
        "--no-error-summary",
        "--no-color-output",
        *found,
    ]
    result = subprocess.run(command, cwd=project, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        print(result.stdout + result.stderr, file=sys.stderr)
        return None
    return parse(result.stdout, project.relative_to(ROOT).as_posix())


def read_baseline() -> dict[str, int]:
    try:
        return json.loads(BASELINE.read_text())
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, metavar="DIR", help="check only the project at DIR")
    parser.add_argument("--tighten", action="store_true", help="write every lower count into the baseline")
    parser.add_argument("--record", action="store_true", help="write today's counts into the baseline as they are")
    args = parser.parse_args(argv)
    chosen = [args.run.resolve()] if args.run else [p.path for p in find_projects() if p.kind == "python"]
    baseline = read_baseline()
    errors: dict[str, list[str]] = {}
    for project in chosen:
        found = check(project)
        if found is None:
            return 2
        errors.update(found)
    counts = {path: len(lines) for path, lines in errors.items()}
    inside = tuple(f"{project.relative_to(ROOT).as_posix()}/" for project in chosen)
    scoped = {path: allowed for path, allowed in baseline.items() if path.startswith(inside)}
    risen, dropped = compare(counts, scoped)
    if args.tighten or args.record:
        kept = {path: allowed for path, allowed in baseline.items() if not path.startswith(inside)}
        for path, count in counts.items():
            kept[path] = count if args.record else min(count, scoped.get(path, count))
        if not args.record:  # a risen file keeps the count it was allowed, and still fails below
            kept.update({path: scoped[path] for path in risen if path in scoped})
            kept = {path: allowed for path, allowed in kept.items() if path not in risen or path in scoped}
        BASELINE.write_text(json.dumps(dict(sorted(kept.items())), indent=2) + "\n")
        print(f"typecheck: baseline is {sum(kept.values())} errors in {len(kept)} files")
        if args.record:
            return 0
    for path in risen:
        print(f"{path}: {counts[path]} errors, the baseline allows {scoped.get(path, 0)}")
        print("\n".join(f"  {line}" for line in errors[path]))
    if dropped and not args.tighten:
        fewer = sum(scoped[path] - counts.get(path, 0) for path in dropped)
        print(f"typecheck: {fewer} fewer errors than the baseline in {len(dropped)} files; bank them with --tighten")
    total = Counter(counts).total()
    print(f"typecheck: {total} errors in {len(counts)} files ({len(chosen)} projects); {len(risen)} files over the baseline")
    return 1 if risen else 0


if __name__ == "__main__":
    sys.exit(main())
