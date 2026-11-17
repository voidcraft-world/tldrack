"""Check a release tarball by what it really holds, not by the tree it was packed from.

    python3 scripts/check_archive.py dist/release/tldrack-ui-0.1.0.tgz [more.tgz …]

The tree scan (`tests/test_purity.py`) reads tracked source. A tarball also carries what no source scan
sees: build output (`ui/dist/`, gitignored), anything a package's `files` pulls in, and compressed
assets. So each member is read here with the SAME rules, through the purity test's own `Scanner`:

- tier 1 and the public tier 2 on every member's path and text, a gzip member decompressed first;
- the operator's word list when it is configured (`tests/test_purity.py` owns where it comes from);
- any tier-2 hit fails: a release has no baseline;
- no model checkpoint, key or database file, and no file over the purity test's size limit;
- the legal files (`LICENSE`, `NOTICE`) at the package root.

A finding prints masked, like the tree scan's: a CI log on a public repository is public too.
`scripts/pack.py` runs this on every tarball it writes; a refused tarball is deleted there.
Stdlib only.
"""

from __future__ import annotations

import gzip
import sys
import tarfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Data, keys and credentials: never in a package, whatever its `files` says.
FORBIDDEN_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"})
FORBIDDEN_NAMES = frozenset({".env", ".npmrc", ".netrc", "id_rsa", "id_ed25519"})


def _purity() -> types.ModuleType:
    # Compiled from source, never imported: why, owned by .claude/hooks/affected_tests.py
    # (`_load_projects`) — see the note there.
    path = ROOT / "tests" / "test_purity.py"
    module = types.ModuleType("tldrack_purity")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


def _member_text(name: str, data: bytes) -> str | None:
    if name.endswith(".gz"):
        try:
            data = gzip.decompress(data)
        except OSError:
            return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def check(path: Path, required: tuple[str, ...] = ("LICENSE", "NOTICE")) -> list[str]:
    """Every problem in the tarball at `path`, as masked one-line findings. [] = clean."""
    purity = _purity()
    scanner = purity.Scanner(ROOT, purity.load_operator_words())
    problems: list[str] = []
    seen: set[str] = set()
    with tarfile.open(path) as tar:
        for member in tar.getmembers():
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if member.issym() or member.islnk():
                problems.append(f"{rel}: a link in a package")
                continue
            if not member.isfile():
                continue
            seen.add(rel)
            leaf = rel.rsplit("/", 1)[-1]
            suffix = "." + leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""
            if suffix in purity.CHECKPOINT_SUFFIXES:
                problems.append(f"{rel}: {purity.CHECKPOINT}")
            if suffix in FORBIDDEN_SUFFIXES or leaf in FORBIDDEN_NAMES or leaf.startswith(".env."):
                problems.append(f"{rel}: a data, key or credential file")
            if member.size > purity.MAX_BYTES:
                problems.append(f"{rel}: {purity.OVERSIZED}")
            hits = scanner.path(rel)
            text = _member_text(leaf, tar.extractfile(member).read())
            if text is not None:
                found = scanner.text(rel, text)
                hits = (hits[0] + found[0], hits[1] + found[1])
            problems += [str(hit) for hit in hits[0] + hits[1]]
    problems += [f"{name}: missing at the package root" for name in required if name not in seen]
    return problems


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        problems = check(path)
        failed |= bool(problems)
        print(f"{'✗' if problems else '✓'} {path.name}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
