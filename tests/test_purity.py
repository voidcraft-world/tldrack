"""Nothing private ships: checked, never trusted.

tldrack was carved out of a private codebase, so every file a clone receives is
scanned, this one included. Two tiers:

1. **Hazards** are the shapes a leak takes: a home directory, an address, a cloud
   account, an auth id, a private host, a device serial, a token, a model
   checkpoint. Always zero. There is no allow comment: a false positive is fixed by
   rewriting the line, never by excusing it.
2. **Private words** name the private side, and only the OPERATOR list holds them:
   this file names none, because a public rule that spells a private name publishes
   it. The list covers the private host's structure and repository, its features,
   personal tools, account labels, a handle. It is never in this tree, in any
   form. A hashed list would not do: anyone can hash a dictionary, and every such
   word is in one. It is read from a file outside the tree (`$TLDRACK_PURITY_WORDS`,
   else `~/.config/tldrack/purity-words.txt`) or, in CI, from the
   `TLDRACK_PURITY_WORDS` secret (`$TLDRACK_PURITY_WORDS_TEXT`). Its hits print
   masked, rule and text both. A fork with private words of its own uses the same
   file; one with none spells the list `# none`.
   A per-file count kept in `tests/purity-baseline.json` that may only go DOWN.
   `--publish` ignores the baseline: any hit fails, and the operator list must be
   configured. The release job runs that mode.

Each rule is a pattern, never a list of secrets: the secrets themselves are never
written down here either. Patterns that would match their own source spell one
letter as a class (`toke[n]`), so this file can scan itself like any other.

    python3 tests/test_purity.py                  # both tiers; exit 1 on a failure
    python3 tests/test_purity.py --report         # every hit, grouped by rule and file
    python3 tests/test_purity.py --files A B      # only these files (the edit hook)
    python3 tests/test_purity.py --publish        # zero of everything, operator list required
    python3 tests/test_purity.py --tighten        # bank tier-2 counts that went down
    python3 tests/test_purity.py --rebaseline     # record today's counts (the import, step 1.6)

Stdlib only: it runs before any project is installed. A hit's report masks the
matched text, because CI logs on a public repository are public too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import types
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("purity-baseline.json")


def _skip_dirs() -> frozenset[str]:
    # scripts/projects.py owns the list. Compiled from source, never imported: why, owned by
    # .claude/hooks/affected_tests.py (`_load_projects`) — see the note there.
    path = ROOT / "scripts" / "projects.py"
    module = types.ModuleType("tldrack_projects")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module  # dataclasses resolves annotations through sys.modules
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module.SKIP_DIRS


SKIP_DIRS = _skip_dirs()

# A file this large cannot be reviewed by eye, and is most likely data.
MAX_BYTES = 5 * 1024 * 1024
CHECKPOINT_SUFFIXES = frozenset({".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".onnx"})
MASK = "‹masked›"


@dataclass(frozen=True)
class Rule:
    what: str
    pattern: re.Pattern[str]
    # A match that fully matches `allow` is a public lookalike, not a leak.
    allow: re.Pattern[str] | None = None
    # A path rule: a match naming a path that exists in the scanned tree is the tree's own.
    missing_only: bool = False
    # An operator rule: its hits print with the rule and the text masked (module docstring).
    masked: bool = False

    def matches(self, text: str, root: Path | None = None) -> list[re.Match[str]]:
        return [
            m
            for m in self.pattern.finditer(text)
            if not (self.allow and self.allow.fullmatch(m.group(0)))
            and not (self.missing_only and root is not None and _in_tree(root, m.group(0)))
        ]


def rule(what: str, pattern: str, allow: str | None = None, missing_only: bool = False) -> Rule:
    return Rule(what, re.compile(pattern), re.compile(allow) if allow else None, missing_only)


def _in_tree(root: Path, pointer: str) -> bool:
    return (root / pointer.rstrip(".,")).exists()


def word(body: str) -> str:
    """`body` as a whole word, case-insensitive, where `_` and `-` separate words."""
    return rf"(?i:(?<![a-z0-9]){body}(?![a-z0-9]))"


# Tier 1. Each `what` needs a planted sample in HAZARD_SAMPLES below.
#
# An `allow` is the fixture vocabulary: the placeholder names the tests and the docs use (a home
# called `x`, a tailnet called `tail1234`, an account directory called `a`). Each pattern captures
# the WHOLE name, so a name that only starts like a placeholder (`x-real`, `a2`) is a hazard, and a
# test that needs a new placeholder picks one from here. Each rule has a line in LOOKALIKES, and
# each rule's planted sample is a name the vocabulary does not hold.
HAZARDS: tuple[Rule, ...] = (
    # The name runs to the first character that ends a path, so `x~real` and `dev+work` are whole names.
    rule(
        "a home directory path",
        r"""/(?:Use[r]s|hom[e])/[^/\s"'`)\]{},;:<>|\\…]+|[A-Za-z]:\\Use[r]s\\""",
        allow=r"/(?:Use[r]s|hom[e])/(?:x|x2|u|me|my|you|dev|test|someone|someone2)",
    ),
    # A stack frame (`fn@file[.]js:1:2`) and a reserved `.example` host, one label each: never a mailbox.
    # A local part never ends in a dot, so `self.@synchronized.id` (a keyword list) is no address.
    rule(
        "an email address",
        r"(?i:(?<![\w.+-])[\w+-](?:[\w.+-]*[\w+-])?@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,})(?![\w-])",
        allow=r"(?i:git@github\.com|hunter2@github\.com|[\w.+-]+@example\.(?:com|org|net|test)"
        r"|[\w.+-]+@[\w-]+\.(?:example|js))",
    ),
    # A number inside a comma-separated run of numbers is a table (minified constants), not an id.
    rule("an AWS account id", r"(?<![\w.-])(?<!\d,)\d{12}(?![\w.-])(?!,\d)", allow=r"0{12}"),
    rule("an AWS ARN", r"\bar[n]:aws[a-z-]*:"),
    rule("an AWS access key", r"\b(?:AKI[A]|ASI[A])[0-9A-Z]{16}\b"),
    rule("a user-pool host", r"(?i:amazon[c]ognito\.com|cognit[o]-id[p]\.[a-z0-9-]+\.amazonaws)"),
    rule("a user-pool id", r"\b[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{9}\b|\b[a-z]{2}-[a-z]+-\d:[0-9a-f]{8}-[0-9a-f]{4}-"),
    rule("a user-pool client id", r"(?i:client_?i[d][\"']?\s*[:=]\s*[\"']?[a-z0-9]{25,26}\b)"),
    rule(
        "a private API host",
        r"(?i:[\w-]+\.execute-ap[i]\.[\w-]+\.amazonaws\.com|[\w-]+\.lambda-ur[l]\.[\w-]+\.on\.aws"
        r"|\b[a-z0-9]{10,16}\.cloudfron[t]\.net|\b(?:api|trp[c]|mcp|auth)[\w-]*\.(?:[\w-]+\.)*voidcraf[t]\.world)",
    ),
    rule(
        "a phone serial",
        r"\bR(?=[A-Z]*\d)[0-9A-Z]{10}\b|\badb-[A-Za-z0-9]{8,}-[A-Za-z0-9]{6}\._ad[b]"
        r"|(?:\badb\s+-s|ANDROID_SERIA[L]=)\s*(?!emulator-)[A-Za-z0-9]{6,}",
    ),
    # `-plugin` is Claude Code's own plugin directory; `-sonnet-4` is the tail of a model id.
    rule(
        "a Claude account directory",
        r"\.claud[e]-[a-z][\w-]*(?:\.[\w-]+)*",
        allow=r"\.claude-(?:[a-c]|plugin|alias|empty|evil|old|pin|plan|(?:sonnet|opus|haiku)-\d[\w.-]*)",
    ),
    # The whole dotted name, machine label included: a real machine on a placeholder tailnet is a hazard.
    rule(
        "a tailnet host",
        r"\b[\w-]+(?:\.[\w-]+)*\.ts\.ne[t]\b",
        allow=r"(?:[\w-]+\.)*(?:example|tailnet|your-tailnet)\.ts\.net"
        r"|(?:(?:mymac|mbp|mac|host|attacker|someone-else)\.)?"
        r"(?:tail(?:1234|9999|XXXX)?|mac|mymac|evil|anything|[abc])\.ts\.net(?:\.evil\.ts\.net)?",
    ),
    rule("a personal machine name", r"\b[A-Za-z]+s-(?:MacBoo[k]|Mac-min[i]|Mac-Studi[o]|iMa[c])\b"),
    rule("a private key", r"-----BEGIN (?:[A-Z]+ )*PRIVATE KE[Y]-----"),
    # The host's rules, plans and an agent's memory notes: a pointer a public reader cannot follow.
    # A path this tree holds is its own: a public directory of one of these names passes by existing.
    rule(
        "a pointer into the host",
        r"(?<![\w./-])(?:\.claude/rule[s]|docs/feature(?:-todo)?[s])/[\w./-]*|(?<![\w./-])memor[y]/[\w-]+\.md\b",
        missing_only=True,
    ),
    rule("a personal shell profile", r"\.config/zs[h]/"),
    rule(
        "an API token",
        r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pa[t]_[A-Za-z0-9_]{22,}|\bsk-an[t]-[\w-]{20,}"
        r"|\bsk-(?:pro[j]-)?[A-Za-z0-9]{32,}|\bxox[abposr]-[\w-]{10,}|\bh[f]_[A-Za-z0-9]{34,}"
        r"|\b\d{8,10}:A[A][\w-]{33}\b",
    ),
)
# Path-level hazards, reported against the file itself (line 0).
CHECKPOINT = "a model checkpoint"
OVERSIZED = f"a file over {MAX_BYTES // (1024 * 1024)} MB"
IMPORT_ONLY = "a report of the private import"
# The carve's own notes: they described the private tree, and their copy stays on the private side.
IMPORT_ONLY_FILES = frozenset({"docs/IMPORT-BASELINE.md"})

# Tier 2 has no public rules: every private word is the operator's (module docstring).

OPERATOR_WORD = "an operator word"
OPERATOR_FILE_ENV = "TLDRACK_PURITY_WORDS"
OPERATOR_TEXT_ENV = "TLDRACK_PURITY_WORDS_TEXT"


def operator_words_path() -> Path:
    if os.environ.get(OPERATOR_FILE_ENV):
        return Path(os.environ[OPERATOR_FILE_ENV]).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "tldrack" / "purity-words.txt"


def parse_operator_words(text: str) -> tuple[Rule, ...]:
    """One rule per line. `word` is a whole word, any case; `cs:WORD` keeps its case (a label
    like an upper-case account tag that is an everyday word in lower case); `re:PATTERN` is a raw
    pattern. `#` starts a comment. A line that does not compile names its line number only."""
    rules = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("re:"):
            body = line[3:]
        elif line.startswith("cs:"):
            body = rf"(?<![A-Za-z0-9]){re.escape(line[3:])}(?![A-Za-z0-9])"
        else:
            body = word(re.escape(line))
        try:
            pattern = re.compile(body)
        except re.error:
            raise ValueError(f"operator word list, line {lineno}: not a valid pattern") from None
        rules.append(Rule(OPERATOR_WORD, pattern, masked=True))
    return tuple(rules)


def load_operator_words() -> tuple[Rule, ...] | None:
    """The operator list, or None when none is configured. An empty list (`# none`) is ()."""
    if os.environ.get(OPERATOR_TEXT_ENV, "").strip():
        return parse_operator_words(os.environ[OPERATOR_TEXT_ENV])
    path = operator_words_path()
    if path.is_file():
        return parse_operator_words(path.read_text())
    return None


def _combined(rules: tuple[Rule, ...]) -> re.Pattern[str]:
    # One search per line first; a line that matches no rule (nearly all of them) costs one regex.
    return re.compile("|".join(f"(?:{r.pattern.pattern})" for r in rules))


_ANY_HAZARD = _combined(HAZARDS)


@dataclass(frozen=True)
class Hit:
    path: str  # relative, posix
    line: int  # 0 = the path itself
    what: str
    text: str  # masked for tier 1 and for operator words

    def __str__(self) -> str:
        where = self.path if self.line == 0 else f"{self.path}:{self.line}"
        return f"{where}: {self.what}: {self.text}"


def shipped_files(root: Path = ROOT) -> list[Path]:
    """Every file a commit would carry: tracked or untracked-but-not-ignored."""
    if (root / ".git").exists() and shutil.which("git"):
        listed = (
            subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .split("\0")
        )
        files = {root / name for name in listed if name}
        return sorted(p for p in files if p.is_symlink() or p.is_file())
    found = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found.extend(Path(current) / n for n in names)
    return sorted(found)


def _read_text(path: Path) -> str | None:
    """A symlink is read as its target string; a binary file is None."""
    if path.is_symlink():
        return os.readlink(path)
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _scan_line(rel: str, lineno: int, line: str, rules: tuple[Rule, ...], mask: bool, root: Path | None = None) -> list[Hit]:
    hits = []
    for r in rules:
        found = r.matches(line, root)
        if not found:
            continue
        shown = line.strip()
        if mask or r.masked:
            for m in found:
                shown = shown.replace(m.group(0), MASK)
        hits.append(Hit(rel, lineno, r.what, shown[:120]))
    return hits


class Scanner:
    """Both tiers over text: a file in the tree (`scan`) or a member of a release archive
    (`scripts/check_release.py`), so the two can never read different rules."""

    def __init__(self, root: Path = ROOT, operator: tuple[Rule, ...] | None = None):
        self.root = root
        self.words = operator or ()
        self._any_word = _combined(self.words) if self.words else None

    def path(self, rel: str) -> tuple[list[Hit], list[Hit]]:
        return (_scan_line(rel, 0, rel, HAZARDS, True, self.root), _scan_line(rel, 0, rel, self.words, False))

    def text(self, rel: str, text: str) -> tuple[list[Hit], list[Hit]]:
        hazards: list[Hit] = []
        words: list[Hit] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            if _ANY_HAZARD.search(line):
                hazards += _scan_line(rel, lineno, line, HAZARDS, True, self.root)
            if self._any_word and self._any_word.search(line):
                words += _scan_line(rel, lineno, line, self.words, False)
        return hazards, words


def scan(
    root: Path = ROOT, files: list[Path] | None = None, operator: tuple[Rule, ...] | None = None
) -> tuple[list[Hit], list[Hit]]:
    """(tier-1 hazards, tier-2 private words) across `files`, default every shipped file.
    `operator` is the operator list; the tree scan passes `load_operator_words()`."""
    scanner = Scanner(root, operator)
    hazards: list[Hit] = []
    words: list[Hit] = []
    for path in files if files is not None else shipped_files(root):
        rel = path.relative_to(root).as_posix()
        found = scanner.path(rel)
        hazards += found[0]
        words += found[1]
        if path.resolve() == BASELINE.resolve():
            continue  # derived data, checked by test_the_baseline_is_well_formed
        if path.suffix.lower() in CHECKPOINT_SUFFIXES:
            hazards.append(Hit(rel, 0, CHECKPOINT, rel))
        if rel in IMPORT_ONLY_FILES:
            hazards.append(Hit(rel, 0, IMPORT_ONLY, rel))
        if not path.is_symlink() and path.stat().st_size > MAX_BYTES:
            hazards.append(Hit(rel, 0, OVERSIZED, rel))
            continue
        text = _read_text(path)
        if text is None:
            continue
        found = scanner.text(rel, text)
        hazards += found[0]
        words += found[1]
    return hazards, words


def word_counts(words: list[Hit]) -> dict[str, int]:
    return dict(Counter(hit.path for hit in words))


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    if not path.exists():
        return {}
    counts = json.loads(path.read_text()).get("counts", {})
    return {str(k): int(v) for k, v in counts.items()}


def write_baseline(counts: dict[str, int], path: Path = BASELINE) -> None:
    body = {
        "_": "Tier-2 private-word hits per file (tests/test_purity.py). Counts only go down: "
        "--tighten banks a drop. Raising one is a hand edit with its reason in the commit.",
        "counts": dict(sorted((k, v) for k, v in counts.items() if v > 0)),
    }
    path.write_text(json.dumps(body, indent=2) + "\n")


def ratchet_failures(counts: dict[str, int], baseline: dict[str, int]) -> list[str]:
    return [
        f"{path}: {n} private word(s), baseline {baseline.get(path, 0)}"
        for path, n in sorted(counts.items())
        if n > baseline.get(path, 0)
    ]


def tightened(counts: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Lower every entry to today's count. Never raises one, never adds one."""
    return {path: min(n, counts.get(path, 0)) for path, n in baseline.items() if counts.get(path, 0) > 0}


def publish_failures(counts: dict[str, int]) -> list[str]:
    """`--publish`: the baseline excuses nothing."""
    return [f"{path}: {n} private word(s); a publication allows none" for path, n in sorted(counts.items())]


def _report(hazards: list[Hit], words: list[Hit]) -> str:
    out = [f"tier 1 hazards: {len(hazards)}   tier 2 private words: {len(words)} in {len(word_counts(words))} files"]
    for title, hits in (("TIER 1", hazards), ("TIER 2", words)):
        by_rule: dict[str, list[Hit]] = defaultdict(list)
        for hit in hits:
            by_rule[hit.what].append(hit)
        for what, group in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            out.append(f"\n{title} · {what} · {len(group)}")
            out += [f"  {hit}" for hit in group]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nothing private ships.")
    parser.add_argument("--files", nargs="+", type=Path, help="scan only these files")
    parser.add_argument("--report", action="store_true", help="print every hit")
    parser.add_argument(
        "--publish", action="store_true", help="zero hits whatever the baseline says, and the operator list configured"
    )
    parser.add_argument("--tighten", action="store_true", help="bank tier-2 counts that went down")
    parser.add_argument("--rebaseline", action="store_true", help="record today's tier-2 counts")
    args = parser.parse_args(argv)

    files = None
    if args.files:
        files = [p.resolve() for p in args.files if (p.is_symlink() or p.exists())]
        files = [p for p in files if ROOT in p.parents and not set(p.relative_to(ROOT).parts) & SKIP_DIRS]
    try:
        operator = load_operator_words()
    except ValueError as error:
        print(f"purity: {error}", file=sys.stderr)
        return 1
    if args.publish and operator is None:
        print(
            f"purity: --publish needs the operator word list: ${OPERATOR_FILE_ENV}, "
            f"{operator_words_path()}, or ${OPERATOR_TEXT_ENV} (a list with no words reads `# none`)",
            file=sys.stderr,
        )
        return 1
    hazards, words = scan(ROOT, files, operator)
    counts = word_counts(words)
    baseline = load_baseline()

    if args.rebaseline or args.tighten:
        if files is not None:
            parser.error("--tighten and --rebaseline read the whole tree, not --files")
        new = counts if args.rebaseline else tightened(counts, baseline)
        write_baseline(new)
        print(f"baseline: {sum(baseline.values())} -> {sum(new.values())} private words")
        return 0

    if args.report:
        print(_report(hazards, words))
    failures = publish_failures(counts) if args.publish else ratchet_failures(counts, baseline)
    for hit in hazards:
        print(f"HAZARD {hit}", file=sys.stderr)
    for line in failures:
        print(f"RATCHET {line}", file=sys.stderr)
    if not args.report:
        listed = "not configured" if operator is None else f"{len(operator)} rule(s)"
        print(
            f"purity: {len(hazards)} hazards, {sum(counts.values())} private words "
            f"(baseline {sum(baseline.values())}; operator list {listed})" + ("; publication mode" if args.publish else "")
        )
    return 1 if hazards or failures else 0


# ---------------------------------------------------------------- the tree itself


def test_tier1_hazards_are_zero():
    hazards, _ = scan()
    assert not hazards, "\n".join(map(str, hazards))


def test_tier2_private_words_only_go_down():
    _, words = scan(operator=load_operator_words())
    failures = ratchet_failures(word_counts(words), load_baseline())
    assert not failures, "\n".join(failures) + "\n\n" + "\n".join(map(str, words))


def test_the_baseline_is_well_formed():
    if not BASELINE.exists():
        return
    body = json.loads(BASELINE.read_text())
    assert set(body) <= {"_", "counts"}, set(body)
    for path, n in body["counts"].items():
        assert isinstance(n, int) and n > 0, (path, n)
        assert not Path(path).is_absolute() and ".." not in Path(path).parts, path


def test_this_file_scans_itself():
    assert Path(__file__).resolve() in [p.resolve() for p in shipped_files()]


def test_every_agent_guide_has_its_agents_md_symlink():
    """One guide for every coding agent: AGENTS.md points at CLAUDE.md, never a copy that drifts."""
    guides = [p for p in shipped_files() if p.name == "CLAUDE.md"]
    assert ROOT / "CLAUDE.md" in guides
    for guide in guides:
        agents = guide.with_name("AGENTS.md")
        assert agents.is_symlink() and agents.readlink() == Path("CLAUDE.md"), agents


# ---------------------------------------------------------------- the scanner


# Built by concatenation, so this source never holds one whole.
HAZARD_SAMPLES = {
    "a home directory path": "cd /Us" + "ers/jsmith/code",
    "an email address": "mail someone" + "@" + "mail.test",
    "an AWS account id": "account " + "1234" + "5678" + "9012",
    "an AWS ARN": "ar" + "n:aws:iam::role/x",
    "an AWS access key": "AKI" + "A" + "ABCDEFGHIJKLMNOP",
    "a user-pool host": "https://auth.x.amazonco" + "gnito.com",
    "a user-pool id": "us-east-1" + "_AbCdEfGhI",
    "a user-pool client id": "client" + "_id = 'abcdefghij0123456789abcde'",
    "a private API host": "https://abc.execute-" + "api.us-east-1.amazonaws.com/x",
    "a phone serial": "adb -" + "s 0123ABCD9876",
    "a Claude account directory": "~/.claude" + "-second/projects",
    "a tailnet host": "mac.tail1a2b.ts" + ".net",
    "a personal machine name": "Alexs-" + "MacBook-Pro.local",
    "a private key": "-----BEGIN RSA PRIVATE " + "KEY-----",
    "a pointer into the host": "owned by docs/fea" + "tures/secret-plan/plan.md",
    "a personal shell profile": "source ~/.con" + "fig/zsh/40-x.zsh",
    "an API token": "gh" + "p_" + "a" * 36,
}

LOOKALIKES = [
    "git" + "@" + "github.com:voidcraft-world/tldrack.git",
    "you" + "@" + "example.com",
    "ssh user" + "@" + "host",
    "npm i @tldrack/ui@0.1.0",
    "adb -" + "s <serial>  adb -" + "s $SERIAL  adb -" + "s emulator-5554",
    "https://mac.example.ts" + ".net",
    "id 00000000-0000-0000-0000-" + "0000" * 3,
    "https://voidcraft.world and https://www.voidcraft.world",
    "ttl 123456789 and 1731427200000",
    "~/.claude/skills",
    # The fixture vocabulary, one line per allowed shape.
    "cd /Us" + "ers/x/repo && ls /Us" + "ers/someone2 /hom" + "e/me '/Us" + "ers/'",
    "t" + "@" + "example.test, fn" + "@" + "file.js:1:2, https://mac.ts" + ".net" + "@" + "evil.example/x",
    "https://me:hunter2" + "@" + "github.com/a/b",
    "run " + "0000" * 3,
    "~/.claude" + "-a/projects, .claude" + "-plugin/plugin.json, anthropic.claude" + "-sonnet-4",
    "mymac.tail1234.ts" + ".net, mac.ts" + ".net, host.tailXXXX.ts" + ".net",
    "keyword:`copy.readwrite.self." + "@synchronized.id.typeof`",
    "const POW5 = [6103515625,30517578125," + "1525" + "8789" + "0625," + "7629" + "3945" + "3125]",
]


def test_every_hazard_rule_has_a_planted_sample():
    assert set(HAZARD_SAMPLES) == {r.what for r in HAZARDS}


def test_each_planted_hazard_is_caught_and_masked(tmp_path):
    for what, sample in HAZARD_SAMPLES.items():
        planted = tmp_path / "planted.md"
        planted.write_text(f"before\n{sample}\nafter\n")
        hazards, _ = scan(tmp_path, [planted])
        assert [h.what for h in hazards] == [what], (what, hazards)
        assert hazards[0].line == 2 and MASK in hazards[0].text


def test_a_planted_home_path_fails_the_tree(tmp_path):
    (tmp_path / "README.md").write_text("clean\n")
    assert scan(tmp_path) == ([], [])
    (tmp_path / "notes.py").write_text('LOG = "/Us' + 'ers/jsmith/log.txt"\n')
    hazards, _ = scan(tmp_path)
    assert [(h.path, h.what) for h in hazards] == [("notes.py", "a home directory path")]
    assert "jsmith" not in hazards[0].text


def test_public_lookalikes_pass(tmp_path):
    planted = tmp_path / "lookalikes.md"
    planted.write_text("\n".join(LOOKALIKES) + "\n")
    assert scan(tmp_path, [planted]) == ([], [])


def test_a_symlink_target_is_scanned(tmp_path):
    (tmp_path / "link").symlink_to("/Us" + "ers/jsmith/secret")
    hazards, _ = scan(tmp_path)
    assert [h.what for h in hazards] == ["a home directory path"]


def test_checkpoints_oversized_and_import_only_files_fail(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"\0weights")
    (tmp_path / "dump.jsonl").write_bytes(b"x" * (MAX_BYTES + 1))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "IMPORT-BASELINE.md").write_text("# notes\n")
    hazards, _ = scan(tmp_path)
    assert sorted(h.what for h in hazards) == sorted([OVERSIZED, CHECKPOINT, IMPORT_ONLY])


def test_private_words_are_counted_in_text_and_paths(tmp_path):
    (tmp_path / "zanzibar.py").write_text("# the zanzibar team wrote this\n")
    operator = parse_operator_words(OPERATOR_SAMPLE)
    assert scan(tmp_path) == ([], [])
    _, words = scan(tmp_path, operator=operator)
    assert word_counts(words) == {"zanzibar.py": 2}


def test_a_pointer_into_this_tree_is_its_own(tmp_path):
    (tmp_path / "docs" / ("fea" + "tures") / "rack").mkdir(parents=True)
    planted = tmp_path / "notes.md"
    planted.write_text("owned by docs/fea" + "tures/rack/ and docs/fea" + "tures/elsewhere/x.md\n")
    hazards, _ = scan(tmp_path, [planted])
    assert [h.what for h in hazards] == ["a pointer into the host"]
    assert hazards[0].text.count(MASK) == 1


OPERATOR_SAMPLE = "# a comment\n\nzanzibar\ncs:QRX\nre:(?i:wombat[0-9]+)\n"


def test_operator_words_parse_case_and_raw_patterns():
    rules = parse_operator_words(OPERATOR_SAMPLE)
    assert [r.what for r in rules] == [OPERATOR_WORD] * 3 and all(r.masked for r in rules)
    hit = lambda text: any(r.matches(text) for r in rules)
    assert hit("the Zanzibar lane") and not hit("zanzibarian")
    assert hit("on QRX today") and not hit("qrx in lower case")
    assert hit("WOMBAT42") and not hit("wombat")
    assert parse_operator_words("# none\n") == ()
    try:
        parse_operator_words("ok\nre:(\n")
    except ValueError as error:
        assert "line 2" in str(error) and "(" not in str(error).split("line 2")[1]
    else:
        raise AssertionError("a broken pattern must be refused")


def test_operator_hits_print_masked_rule_and_text(tmp_path):
    planted = tmp_path / "notes.md"
    planted.write_text("the zanzibar desk\n")
    _, words = scan(tmp_path, [planted], operator=parse_operator_words(OPERATOR_SAMPLE))
    assert [(w.what, w.text) for w in words] == [(OPERATOR_WORD, f"the {MASK} desk")]
    assert scan(tmp_path, [planted], operator=None) == ([], [])


def test_the_operator_list_comes_from_outside_the_tree(tmp_path, monkeypatch):
    monkeypatch.delenv(OPERATOR_TEXT_ENV, raising=False)
    monkeypatch.setenv(OPERATOR_FILE_ENV, str(tmp_path / "absent.txt"))
    assert load_operator_words() is None
    (tmp_path / "words.txt").write_text("zanzibar\n")
    monkeypatch.setenv(OPERATOR_FILE_ENV, str(tmp_path / "words.txt"))
    assert len(load_operator_words()) == 1
    monkeypatch.setenv(OPERATOR_TEXT_ENV, "# none")
    assert load_operator_words() == ()


def test_publish_ignores_the_baseline_and_needs_the_operator_list(tmp_path, monkeypatch):
    assert publish_failures({}) == []
    assert publish_failures({"a.py": 1}) == ["a.py: 1 private word(s); a publication allows none"]
    monkeypatch.delenv(OPERATOR_TEXT_ENV, raising=False)
    monkeypatch.setenv(OPERATOR_FILE_ENV, str(tmp_path / "absent.txt"))
    assert main(["--publish", "--files", __file__]) == 1


def test_the_ratchet_only_goes_down(tmp_path):
    counts = {"a.py": 2, "b.py": 1}
    assert ratchet_failures(counts, {"a.py": 2, "b.py": 1}) == []
    assert ratchet_failures(counts, {"a.py": 3, "b.py": 1}) == []
    assert ratchet_failures(counts, {"a.py": 1}) == ["a.py: 2 private word(s), baseline 1", "b.py: 1 private word(s), baseline 0"]
    assert tightened({"a.py": 1}, {"a.py": 3, "gone.py": 2}) == {"a.py": 1}
    assert tightened({"a.py": 5}, {"a.py": 3}) == {"a.py": 3}
    write_baseline({"a.py": 1, "z.py": 0}, tmp_path / "b.json")
    assert load_baseline(tmp_path / "b.json") == {"a.py": 1}


def test_the_git_listing_skips_ignored_files(tmp_path):
    if not shutil.which("git"):
        return
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("/Us" + "ers/jsmith\n")
    (tmp_path / "untracked.txt").write_text("/Us" + "ers/jsmith\n")
    hazards, _ = scan(tmp_path)
    assert [h.path for h in hazards] == ["untracked.txt"]


if __name__ == "__main__":
    sys.exit(main())
