"""The rack's words (CLAUDE.md rule 8): another project's vocabulary, and the host's brand, stay out of the tree.

Two scans over every tracked text file:

1. **Another project's words** for a worker, the rack and a step, as whole words in any case. The list lives once,
   in `docs/INSPIRATIONS.md`, beside the credit to the project they come from (its `<!-- vocabulary: … -->` note), so
   this file names none of them and scans itself like any other.
2. **A host-brand name**: the prefix the rack's wire messages, its default session and Laya's fine-tunes carried
   before step 6.8. The public organisation and the bridge core it publishes keep the name (`…-world`).

A hit is allowed only as a LEGACY ALIAS: a name a reader still takes for one release after its rename, with a comment
naming step 6.8 on its line or within `LEGACY_WINDOW` lines above it. Removing an alias removes its hit with it.
`docs/INSPIRATIONS.md` holds the list, so it is exempt; so is a file a build writes (`GENERATED`).

    uvx --from pytest==9.1.1 pytest -q tests/test_vocabulary.py
    python3 tests/test_vocabulary.py      # every hit, by file and line
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORDS_FILE = "docs/INSPIRATIONS.md"
#: Files a build writes from sources this test already reads (ui/scripts/export-evals.mjs).
GENERATED = frozenset({"docs/evals/index.html"})
LEGACY_WINDOW = 8
LEGACY_MARK = re.compile(r"\bstep 6\.8\b")
#: Spelled with a class so this file does not match itself.
BRAND = re.compile(r"voidcraf[t]-(?!world)")
_VOCABULARY_NOTE = re.compile(r"<!-- vocabulary: ([a-z ]+) -->")


def vocabulary() -> re.Pattern[str]:
    note = _VOCABULARY_NOTE.search((ROOT / WORDS_FILE).read_text(encoding="utf-8"))
    assert note, f"{WORDS_FILE} lost its vocabulary note"
    words = sorted(set(note.group(1).split()), key=len, reverse=True)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(map(re.escape, words))})(?![A-Za-z0-9])", re.IGNORECASE)


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [name for name in out.decode().split("\0") if name and name != WORDS_FILE and name not in GENERATED]


def hits(pattern: re.Pattern[str]) -> list[str]:
    found = []
    for name in tracked():
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            window = lines[max(0, i - LEGACY_WINDOW) : i + 1]
            if not any(LEGACY_MARK.search(above) for above in window):
                found.append(f"{name}:{i + 1}: {line.strip()[:120]}")
    return found


def test_no_other_projects_words_outside_a_legacy_alias():
    found = hits(vocabulary())
    assert not found, "another project's words (docs/INSPIRATIONS.md lists them):\n" + "\n".join(found)


def test_no_host_brand_names_outside_a_legacy_alias():
    found = hits(BRAND)
    assert not found, "a host-brand name (a legacy alias needs a comment naming step 6.8):\n" + "\n".join(found)


def test_the_word_boundaries_hold():
    words = vocabulary()
    assert words.search("CREW_ID") and words.search("a crew_session"), "an identifier's part is a word too"
    assert words.search("asyncRewake screwdriver estimate") is None
    assert BRAND.search("voidcraf" + "t-world-bridge") is None


if __name__ == "__main__":
    report = [*hits(vocabulary()), *hits(BRAND)]
    print("\n".join(report) or "clean")
    sys.exit(1 if report else 0)
