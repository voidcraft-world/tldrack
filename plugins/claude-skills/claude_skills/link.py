"""Link a checkout's own skills into Claude Code's skills dirs, so they load in every repository.

Claude Code reads `<project>/.claude/skills/` only inside that project, and an account's
`skills/` everywhere. tldrack's skills are for every repository the operator works in, so each
one gets a symlink in the account: `<dest>/<name> -> <source>/<name>`. A symlink, not a copy,
so a `git pull` of the checkout updates every account at once.

The rules, all enforced by `plan_links` before anything is touched:

- **A skill is a directory holding `SKILL.md`.** Anything else in the source is not linked.
- **Idempotent.** A link that already resolves to the skill is `current`, and left alone.
- **Nothing of the operator's is replaced.** A real directory or file of the same name is
  skipped, and so is a link that points outside the source. `force` replaces such a link, and
  only a link: a real directory or file is never touched. A link that points into the source
  is ours, so a stale one is re-pointed without `force`.
- **Only our own links are deleted.** `remove` deletes the links that point into the source and
  nothing else. A plain run also prunes such a link once its skill is gone from the source (a
  skill renamed upstream), so a pull never leaves a link dangling.

`plan_links` reads the filesystem and writes nothing; `apply_links` carries a plan out and
reports each step's error rather than stopping. `main` is the command line, run through
`scripts/link-skills`. Standard library only, so any Python >= 3.10 runs it without a venv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

SKILL_FILE = "SKILL.md"

CREATE, CURRENT, REPLACE, PRUNE, REMOVE, SKIP = "create", "current", "replace", "prune", "remove", "skip"
#: Actions that change the filesystem; the rest only report.
CHANGES = frozenset({CREATE, REPLACE, PRUNE, REMOVE})
DONE = {CREATE: "created", CURRENT: "current", REPLACE: "replaced", PRUNE: "pruned", REMOVE: "removed", SKIP: "skipped"}


@dataclass(frozen=True)
class LinkStep:
    action: str
    link: Path
    target: Path | None = None
    reason: str = ""


# -- reading ------------------------------------------------------------------


def source_skills(source: Path) -> list[str]:
    """The skill directory names in `source`, sorted. Empty when it is not a directory."""
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.name.startswith(".") and entry.is_dir() and os.path.isfile(os.path.join(entry.path, SKILL_FILE))
    ]


def link_destination(link: Path) -> str | None:
    """Where a symlink points, made absolute against its own folder; None when it is not a link."""
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    return os.path.normpath(os.path.join(os.path.dirname(link), raw))


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def points_into(link: Path, source: Path) -> bool:
    """Whether a symlink points inside `source`, by its literal path or by real paths."""
    destination = link_destination(link)
    if destination is None:
        return False
    return is_under(destination, os.path.normpath(os.path.abspath(source))) or is_under(
        os.path.realpath(destination), os.path.realpath(source)
    )


def is_current(link: Path, target: Path) -> bool:
    return link_destination(link) == str(target) or os.path.realpath(link) == os.path.realpath(target)


def unique_dirs(paths: Iterable[Path]) -> list[Path]:
    """Absolute, in order, one per real path."""
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
        real = os.path.realpath(absolute)
        if real not in seen:
            seen.add(real)
            result.append(absolute)
    return result


# -- planning -----------------------------------------------------------------


def plan_one(link: Path, target: Path, source: Path, force: bool) -> LinkStep:
    if not os.path.lexists(link):
        return LinkStep(CREATE, link, target)
    if link.is_symlink():
        if is_current(link, target):
            return LinkStep(CURRENT, link, target)
        elsewhere = link_destination(link)
        if force or points_into(link, source):
            return LinkStep(REPLACE, link, target, f"was a link to {elsewhere}")
        return LinkStep(SKIP, link, target, f"a link to {elsewhere} (--force replaces a link)")
    kind = "a directory" if link.is_dir() else "a file"
    return LinkStep(SKIP, link, target, f"{kind} that is not a link, never replaced")


def own_links(dest: Path, source: Path) -> list[Path]:
    """The links in `dest` that point into `source`."""
    try:
        names = sorted(os.listdir(dest))
    except OSError:
        return []
    return [dest / name for name in names if (dest / name).is_symlink() and points_into(dest / name, source)]


def plan_links(source: Path, dests: Sequence[Path], *, force: bool = False, remove: bool = False) -> list[LinkStep]:
    """Every step, for every destination, in order. Reads the filesystem; writes nothing."""
    source = Path(os.path.abspath(os.path.expanduser(str(source))))
    skills = [] if remove else source_skills(source)
    steps: list[LinkStep] = []
    for dest in unique_dirs(dests):
        if os.path.realpath(dest) == os.path.realpath(source):
            steps.append(LinkStep(SKIP, dest, source, "the destination is the source"))
            continue
        if remove:
            steps += [LinkStep(REMOVE, link, Path(link_destination(link) or link)) for link in own_links(dest, source)]
            continue
        steps += [plan_one(dest / name, source / name, source, force) for name in skills]
        for link in own_links(dest, source):
            if link.name not in skills and not os.path.isfile(os.path.join(link, SKILL_FILE)):
                steps.append(LinkStep(PRUNE, link, Path(link_destination(link) or link), "its skill is gone from the source"))
    return steps


# -- applying -----------------------------------------------------------------


def apply_step(step: LinkStep) -> str | None:
    """Carry out one step; the error to report, or None. A link is re-checked just before it is changed."""
    if step.action not in CHANGES:
        return None
    try:
        if step.action == CREATE:
            step.link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(step.target, step.link, target_is_directory=True)
            return None
        if not step.link.is_symlink():
            return "no longer a link, left alone"
        if step.action == REPLACE:
            # A new link renamed over the old one: there is never a moment with neither.
            temporary = step.link.with_name(f".{step.link.name}.link-skills")
            if os.path.lexists(temporary):
                if not temporary.is_symlink():
                    return f"{temporary} is in the way"
                os.unlink(temporary)
            os.symlink(step.target, temporary, target_is_directory=True)
            os.replace(temporary, step.link)
            return None
        os.unlink(step.link)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc.strerror or exc}"


def apply_links(steps: Sequence[LinkStep]) -> list[tuple[LinkStep, str | None]]:
    return [(step, apply_step(step)) for step in steps]


# -- the command line -----------------------------------------------------------


def default_source() -> Path:
    """The checkout's `.claude/skills`: this file is `<checkout>/plugins/claude-skills/claude_skills/link.py`."""
    return Path(__file__).resolve().parents[3] / ".claude" / "skills"


def default_dests(env: Mapping[str, str], home: Path) -> list[Path]:
    """The default account's skills dir, plus `$CLAUDE_CONFIG_DIR`'s when that is set."""
    dests = [home / ".claude" / "skills"]
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        dests.append(Path(config_dir).expanduser() / "skills")
    return unique_dirs(dests)


def format_line(step: LinkStep, error: str | None, dry_run: bool) -> str:
    if error is not None:
        return f"failed: {error}  {step.link}"
    if step.action == SKIP:
        return f"skipped: {step.reason}  {step.link}"
    verb = f"would {step.action}" if dry_run and step.action in CHANGES else DONE[step.action]
    arrow = "" if step.target is None else f" -> {step.target}"
    return f"{verb:<16}{step.link}{arrow}"


def summary_line(results: Sequence[tuple[LinkStep, str | None]], dry_run: bool) -> str:
    counts: Counter[str] = Counter()
    for step, error in results:
        if error is not None:
            counts["failed"] += 1
        elif dry_run and step.action in CHANGES:
            counts[f"to {step.action}"] += 1
        else:
            counts[DONE[step.action]] += 1
    return "link-skills: " + (", ".join(f"{count} {label}" for label, count in sorted(counts.items())) or "nothing to link")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="link-skills",
        description="Link a checkout's .claude/skills/<name>/ into Claude Code's skills dirs, "
        "so those skills load in every repository.",
    )
    parser.add_argument("--source", type=Path, help="the skills directory to link from (default: this checkout's .claude/skills)")
    parser.add_argument(
        "--dest",
        type=Path,
        action="append",
        help="a skills directory to link into; repeatable (default: ~/.claude/skills, "
        "plus $CLAUDE_CONFIG_DIR/skills when it is set)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace a link that points elsewhere (never a real directory or file)"
    )
    parser.add_argument("--remove", action="store_true", help="delete the links that point into the source, and nothing else")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    parser.add_argument("--json", action="store_true", help="one JSON object on stdout instead of lines")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """0 when nothing failed, 1 when a step failed, 2 when there is no source to link from."""
    args = parse_args(argv)
    out = out or sys.stdout
    err = err or sys.stderr
    source = Path(os.path.abspath(os.path.expanduser(str(args.source or default_source()))))
    if not args.remove and not source.is_dir():
        print(f"link-skills: no skills directory at {source}", file=err)
        return 2
    dests = args.dest or default_dests(os.environ if env is None else env, home or Path.home())
    steps = plan_links(source, dests, force=args.force, remove=args.remove)
    results = [(step, None) for step in steps] if args.dry_run else apply_links(steps)
    failed = sum(1 for _, error in results if error is not None)
    if args.json:
        json.dump(
            {
                "source": str(source),
                "dry_run": args.dry_run,
                "failed": failed,
                "steps": [
                    {
                        "action": step.action,
                        "link": str(step.link),
                        "target": None if step.target is None else str(step.target),
                        "reason": step.reason or None,
                        "error": error,
                    }
                    for step, error in results
                ],
            },
            out,
        )
        out.write("\n")
    else:
        for step, error in results:
            print(format_line(step, error, args.dry_run), file=out)
        print(summary_line(results, args.dry_run), file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
