"""Claude Code's built-in slash commands — recovered from the CLI binary.

**Why this is a scrape.** Built-in commands are not files. The CLI ships as one
large native executable (a versioned file under `~/.local/share/claude/versions/`)
with no `skills/` directory, and there is no `claude skills` subcommand. The commands
are registered inside the bundled, minified JS:

    ou({name:"fewer-permission-prompts",requires:{workspace:!0},
        menuDescription:"Pre-approve safe read-only commands based on your usage",
        description:"Scan your transcripts for …",userInvocable:!0,…

so the only way to enumerate them is to read the binary and match that shape.

**It is therefore best-effort, and says so.** Minifier output is not a stable
interface: a future CLI can rename properties or restructure the call and this
returns fewer rows, or none. Two mitigations:

  1. Every row carries `source: "scraped" | "curated"`, so the wall can show
     provenance rather than implying the list is authoritative.
  2. A curated floor (`CURATED_BUILTINS`) is unioned in, so a scrape that breaks
     completely degrades to a known-good list instead of a blank panel.

The scrape earns its keep in the other direction: it surfaces every command a new
CLI build adds, which the floor cannot know about. That is exactly the
"what am I not using?" discovery this panel exists for.

**Cost is paid once per CLI upgrade.** The result is cached under
`~/.cache/tldrack/` keyed on the binary's identity (name + mtime +
size), and the scan runs on a background thread because the plugin host
contract forbids blocking a request handler.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "tldrack"
CACHE_PREFIX = "claude-builtins-"
CACHE_VERSION = 1

# Read the binary in chunks so a file of hundreds of megabytes never lands in memory whole.
# INVARIANT: the effective search window is `min(OVERLAP_BYTES, CHUNK_BYTES) +
# CHUNK_BYTES`, and it must comfortably exceed one registration block plus the
# NAME_LOOKBEHIND, or a match straddling a boundary is lost. Both values are
# orders of magnitude clear of that; keep it that way if you tune them.
CHUNK_BYTES = 8 * 1024 * 1024
OVERLAP_BYTES = 256 * 1024
# How far back from a `menuDescription:` to look for the `name:` it belongs to.
NAME_LOOKBEHIND = 4096

# A JS double-quoted string body: no bare quote or backslash, escapes allowed.
_JS_STRING = rb'((?:[^"\\]|\\.)*)'
MENU_DESCRIPTION_RE = re.compile(rb'menuDescription:"' + _JS_STRING + rb'"')
DESCRIPTION_RE = re.compile(rb'description:"' + _JS_STRING + rb'"')
NAME_RE = re.compile(rb'name:"([a-z0-9][a-z0-9-]{1,48})"')

# The known-good floor. Descriptions are the user-facing ones Claude Code shows.
# Union'd with the scrape — this list existing does NOT make the scrape
# redundant, it makes a broken scrape survivable.
CURATED_BUILTINS: dict[str, str] = {
    "artifact-capabilities": "Runtime capabilities a published Artifact page can be granted",
    "artifact-design": "Design guidance and fundamentals for Artifacts",
    "claude-api": "Build and debug apps that use the Claude API",
    "claude-in-chrome": "Let Claude browse and interact with pages in your Chrome",
    "dataviz": "Design charts, graphs, dashboards and data visualizations that read as one system",
    "fewer-permission-prompts": "Pre-approve safe read-only commands based on your usage",
    "init": "Initialize a new CLAUDE.md file with codebase documentation",
    "keybindings-help": "Customize keyboard shortcuts and chord bindings",
    "loop": "Repeat a prompt or command on an interval (e.g. /loop 5m /foo)",
    "review": "Review a GitHub pull request",
    "run": "Launch this project's app to see your change working",
    "schedule": "Create and manage routines: cloud agents on a schedule",
    "security-review": "Review the codebase for security vulnerabilities",
    "simplify": "Review changed code for reuse, simplification and efficiency, then apply the fixes",
    "update-config": "Change settings: hooks, permissions, environment variables",
}


def _unescape(raw: bytes) -> str:
    """A JS string literal body → text. Falls back to a literal read."""
    text = raw.decode("utf-8", errors="replace")
    try:
        # JSON covers \uXXXX, \n, \", \\ — the escapes esbuild actually emits.
        return json.loads(f'"{text}"')
    except ValueError:
        return text.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")


def find_cli_binary() -> Path | None:
    """The `claude` executable, symlinks resolved. None when not on PATH."""
    found = shutil.which("claude")
    if not found:
        return None
    try:
        resolved = Path(found).resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def cache_key(binary: Path) -> str | None:
    """Identity of a specific CLI build: version name + mtime + size.

    Keyed on all three because the versioned filename alone is not enough for
    npm-style installs, where the entry point keeps one name across upgrades.
    """
    try:
        stat = binary.stat()
    except OSError:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", binary.name)
    return f"{CACHE_VERSION}-{safe_name}-{stat.st_mtime_ns}-{stat.st_size}"


def extract_builtins(blob: bytes) -> dict[str, str]:
    """PURE — command name → menu description, from a chunk of bundled JS.

    Anchors on `menuDescription:` and searches BACKWARD for the nearest `name:`,
    rather than matching forward from the name. Forward matching has to guess a
    window width and silently loses any registration carrying extra properties
    between the two (`requires:{workspace:!0}` was the case that broke it);
    the backward search has no such dependency on what sits in between.
    """
    found: dict[str, str] = {}
    for match in MENU_DESCRIPTION_RE.finditer(blob):
        window = blob[max(0, match.start() - NAME_LOOKBEHIND) : match.start()]
        names = NAME_RE.findall(window)
        if not names:
            continue
        name = names[-1].decode("ascii")  # nearest preceding name wins
        menu = _unescape(match.group(1)).strip()
        if menu and name not in found:
            found[name] = menu
    return found


def scan_binary(binary: Path) -> dict[str, str]:
    """Chunked scrape of the whole CLI binary. Any read error → {}."""
    found: dict[str, str] = {}
    try:
        with binary.open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                for name, menu in extract_builtins(tail + chunk).items():
                    found.setdefault(name, menu)
                tail = chunk[-OVERLAP_BYTES:]
    except OSError:
        return {}
    return found


def merge_with_floor(scraped: dict[str, str]) -> list[dict]:
    """PURE — scrape ∪ curated floor → sorted records with provenance.

    The scrape wins on description when both have one (it is read from the
    running build); the floor supplies anything the scrape missed entirely.
    """
    records: list[dict] = []
    for name in sorted(set(scraped) | set(CURATED_BUILTINS)):
        scraped_description = scraped.get(name)
        records.append(
            {
                "tier": "builtin",
                "id": name,
                "name": name,
                "description": scraped_description or CURATED_BUILTINS.get(name),
                "path": None,  # compiled into the binary — nothing to reveal
                "plugin": None,
                "source": "scraped" if scraped_description else "curated",
            }
        )
    return records


class BuiltinsScanner:
    """Lazily scrapes the CLI once per build, off the request thread.

    The plugin host runs handlers on request threads and forbids blocking, so
    the first `get()` starts a worker and reports `"scanning"` with the floor as
    a placeholder; subsequent calls return the cached union.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or CACHE_DIR
        self._lock = threading.Lock()
        self._records: list[dict] | None = None
        self._scanning = False
        self._error: str | None = None

    def get(self) -> dict:
        """`{"status": "ready"|"scanning"|"unavailable", "skills": [...]}`."""
        binary = find_cli_binary()
        if binary is None:
            return {
                "status": "unavailable",
                "reason": "claude CLI not found on PATH",
                "skills": merge_with_floor({}),
            }
        with self._lock:
            if self._records is not None:
                return {"status": "ready", "binary": str(binary), "skills": self._records}
            cached = self._read_cache(binary)
            if cached is not None:
                self._records = cached
                return {"status": "ready", "binary": str(binary), "skills": cached}
            if not self._scanning:
                self._scanning = True
                threading.Thread(
                    target=self._scan_worker,
                    args=(binary,),
                    daemon=True,
                    name="claude-skills-builtins",
                ).start()
        return {
            "status": "scanning",
            "binary": str(binary),
            # The floor keeps the panel populated during the one-off scan.
            "skills": merge_with_floor({}),
        }

    def _scan_worker(self, binary: Path) -> None:
        records = merge_with_floor(scan_binary(binary))
        self._write_cache(binary, records)
        with self._lock:
            self._records = records
            self._scanning = False

    def _cache_path(self, binary: Path) -> Path | None:
        key = cache_key(binary)
        return None if key is None else self._cache_dir / f"{CACHE_PREFIX}{key}.json"

    def _read_cache(self, binary: Path) -> list[dict] | None:
        path = self._cache_path(binary)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def _write_cache(self, binary: Path, records: list[dict]) -> None:
        path = self._cache_path(binary)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a killed bridge must not leave a truncated
            # cache that the next start would happily parse as "no builtins".
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(records), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass  # cache is an optimisation — never fail the request over it
