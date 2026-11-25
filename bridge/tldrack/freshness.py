"""Is the code this bridge is RUNNING still the code on disk?

The bridge runs its working tree (an editable install, started with `uv run` by
a supervisor) and imports every plugin exactly once, at startup. So an edit to
`main.py` or to a plugin's package changes nothing until the process restarts —
and nothing used to say so. This module watches what the process loaded and
serves the answer as `bridge.freshness` on `GET /snapshot`, so a supervisor can
badge "restart needed" and restart on its own when that is safe.

── What is watched ─────────────────────────────────────────────────────────
Captured once, at startup (`bridge_watch_spec`):
- `tldrack/**/*.py` — the running package itself (index_patch's injected
  script is a string inside it, so it is covered too);
- `pyproject.toml` and `uv.lock` — `uv run` re-syncs to the lock on every start,
  so a lock change IS a code change for the next process;
- the plugins config file, only when the env override is unset (when it is set,
  the file is never read, so it cannot make this process stale);
- `profiles.json` — `load_profiles` reads it once, at startup;
- every MOUNTED plugin dir's `**/*.py`, minus `PRUNED_DIRS` and dot-dirs.
A dir that failed to load is not watched: preflight would import it, fail on the
breakage that kept it out, and report every later edit as unsafe.

── Why a content hash, not mtime ───────────────────────────────────────────
A `git checkout` there and back, a formatter that rewrites nothing, an editor's
save-without-change: each moves mtimes and changes no code. Staleness is
"different BYTES from what was loaded", so an edit reverted to identical content
is not stale. The hash is cached by `(mtime_ns, size)`, so a poll is a stat walk
and a file is re-read only when its stat moved.

The one gap is the window between importing a file and hashing it at startup:
an edit landing there would be baked into the baseline. `loaded_at` closes it —
a file whose mtime is newer than that instant starts as changed.

── Why preflight runs in a subprocess ──────────────────────────────────────
An auto-restart into code that does not import is a bridge that does not come
back. So once the tree has been quiet for `QUIET_SECONDS`, a child process on the
same interpreter compiles every watched `.py`, imports `tldrack.main` and
loads each mounted plugin's `bridge_plugin.py` the way the core's loader does
(without `create_plugin`, which starts workers). In-process, a re-import would
replace live modules under running threads and keep whatever a failed import half
built; a child has none of that, and a hang costs a timeout, not the bridge.
`tldrack.main` is safe to import there: its top level is definitions,
constants and env reads — nothing binds, spawns or writes. `-B` keeps the child
from writing `.pyc` files into the tree it is checking.

Preflight cannot see a NEW dependency: a `uv.lock` that adds a package has not
been synced into this venv yet, so the import fails and the verdict is `failed`
— the safe direction (no auto-restart; a manual one syncs and works).

── The busy seam ───────────────────────────────────────────────────────────
A restart kills in-memory jobs. A plugin that runs any declares them through an
optional, duck-typed `busy_reasons() -> list[str]` — short human strings such as
"creating worktree feat-x". It must be a lock-and-read with no I/O. `BusyProbe`
asks every plugin on each snapshot, under a time budget: a raise contributes
nothing (logged once — a buggy probe must not pin the bridge to the old code
that a restart would fix), while a hang reads as busy (a probe stuck on a lock
is most likely waiting on the very job it would report).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Mapping

from voidcraft_world_bridge.plugins import plugin_module_name

#: How often the watcher re-walks the tree.
POLL_SECONDS = 5.0
#: How long the tree must sit unchanged before preflight runs — long enough that
#: a multi-file edit (or an agent mid-refactor) is not checked half-done.
QUIET_SECONDS = 20.0
PREFLIGHT_TIMEOUT_SECONDS = 60.0
#: `changed` is a display list, not an inventory; `changed_count` is the truth.
CHANGED_CAP = 20
PREFLIGHT_ERROR_CHARS = 400
#: The whole budget for one round of `busy_reasons()` calls.
BUSY_BUDGET_SECONDS = 0.5
MAX_BUSY_REASONS = 20
MAX_BUSY_REASON_CHARS = 120

#: Directory names never walked inside a plugin dir. Dot-dirs are pruned too.
PRUNED_DIRS = frozenset({"tests", "__pycache__", ".venv", "node_modules"})

#: Baseline digest for a file edited between its import and the startup hash.
EDITED_DURING_STARTUP = "edited-during-startup"

#: The core's `PLUGIN_NAME` rule (voidcraft_world_bridge/plugins.py).
PLUGIN_NAME_RE = r"[a-z0-9][a-z0-9-]{0,31}"

#: When this module was imported: main imports it at its own import, so this is
#: the earliest instant this process could have read any watched file.
IMPORTED_AT = time.time()


@dataclass(frozen=True)
class WatchSpec:
    """Everything the watcher and preflight need, injectable for tests."""

    code_dirs: tuple[Path, ...]  # walked for **/*.py
    plugin_dirs: tuple[Path, ...]  # walked for **/*.py, and preflight-loaded
    files: tuple[Path, ...]  # watched whether or not they exist yet
    display_root: Path  # paths under it render repo-relative
    home: Path
    import_modules: tuple[str, ...] = ("tldrack.main",)
    sys_path: tuple[str, ...] = ()  # prepended in the preflight child
    plugin_module_names: Mapping[str, str] = field(default_factory=dict)


def mounted_plugin_dirs(dirs: list[Path], registry: Mapping[str, object], claimed: set[str]) -> list[Path]:
    """The configured dirs whose plugin actually mounted, in load order.

    `claimed` is the built-in names: a configured dir that collides with one is
    skipped by the core, as is a second dir under an already-mounted name (first
    wins), and so it is here."""
    taken = set(claimed)
    mounted: list[Path] = []
    for directory in dirs:
        module = sys.modules.get(plugin_module_name(directory))
        name = getattr(module, "PLUGIN_NAME", None)
        if isinstance(name, str) and name in registry and name not in taken:
            taken.add(name)
            mounted.append(directory)
    return mounted


def bridge_watch_spec(
    plugin_dirs: list[Path], plugins_config: Path | None, profiles_path: Path, home: Path | None = None
) -> WatchSpec:
    """The production watch set for THIS running package."""
    package_dir = Path(__file__).resolve().parent
    project_dir = package_dir.parent
    files = [project_dir / "pyproject.toml", project_dir / "uv.lock", profiles_path]
    if plugins_config is not None:
        files.append(plugins_config)
    resolved = tuple(Path(d).resolve() for d in plugin_dirs)
    return WatchSpec(
        code_dirs=(package_dir,),
        plugin_dirs=resolved,
        files=tuple(files),
        display_root=project_dir.parent,
        home=home or Path.home(),
        sys_path=(str(project_dir),),
        # Named from the dirs as CONFIGURED, which is what the core named them by.
        plugin_module_names={str(r): plugin_module_name(Path(d)) for r, d in zip(resolved, plugin_dirs)},
    )


def display_path(path: str, root: Path, home: Path) -> str:
    """Repo-relative under `root`, `~`-collapsed under `home`, else absolute."""
    for base, prefix in ((str(root), ""), (str(home), "~/")):
        if path.startswith(base + os.sep):
            return prefix + path[len(base) + 1 :]
    return path


def watched_paths(spec: WatchSpec) -> list[str]:
    """Every path in the watch set right now. Explicit files are listed whether
    or not they exist; the fingerprint simply has no entry for a missing one."""
    paths: list[str] = [str(f) for f in spec.files]
    for top in (*spec.code_dirs, *spec.plugin_dirs):
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")]
            paths.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".py"))
    return paths


class Fingerprinter:
    """`{path: sha256}` over a path list, re-reading a file only when its
    `(mtime_ns, size)` moved since the last call."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, int, str]] = {}

    def fingerprint(self, paths: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                result[path] = cached[2]
                continue
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            self._cache[path] = (st.st_mtime_ns, st.st_size, digest)
            result[path] = digest
        for gone in self._cache.keys() - result.keys():
            del self._cache[gone]
        return result

    def mtime_ns(self, path: str) -> int | None:
        cached = self._cache.get(path)
        return cached[0] if cached is not None else None


def diff_fingerprints(baseline: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    """Paths added, removed or with different content, sorted."""
    return sorted(p for p in baseline.keys() | current.keys() if baseline.get(p) != current.get(p))


def fingerprint_key(fingerprint: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(fingerprint.items())).encode()).hexdigest()


# Runs in the child (`python -I -B -c`), fed a JSON spec on stdin. Every failure
# is ONE stderr line, so the first PREFLIGHT_ERROR_CHARS are worth reading.
PREFLIGHT_SCRIPT = r"""
import importlib, importlib.util, json, os, re, sys, traceback
spec = json.load(sys.stdin)
for entry in reversed(spec["sys_path"]):
    sys.path.insert(0, entry)
root, home = spec["root"] + os.sep, spec["home"] + os.sep
def short(text):
    return text.replace(root, "").replace(home, "~/")
failed = []
def fail(label, exc):
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" @ {frames[-1].filename}:{frames[-1].lineno}" if frames and not isinstance(exc, SyntaxError) else ""
    message = " ".join(line.strip() for line in traceback.format_exception_only(type(exc), exc) if line.strip())
    print(short(f"{label}: {message}{where}"), file=sys.stderr)
    failed.append(label)
for path in spec["compile"]:
    try:
        with open(path, "rb") as handle:
            compile(handle.read(), path, "exec", dont_inherit=True)
    except Exception as exc:
        fail("compile", exc)
if not failed:
    for name in spec["modules"]:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            fail(f"import {name}", exc)
    for directory, module_name in spec["plugins"]:
        try:
            entry = os.path.join(directory, "bridge_plugin.py")
            if not os.path.isfile(entry):
                raise FileNotFoundError(f"no bridge_plugin.py in {directory}")
            if directory not in sys.path:
                sys.path.insert(0, directory)
            module_spec = importlib.util.spec_from_file_location(module_name, entry)
            if module_spec is None or module_spec.loader is None:
                raise ImportError(f"cannot load {entry}")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            module_spec.loader.exec_module(module)
            plugin_name = getattr(module, "PLUGIN_NAME", None)
            if not isinstance(plugin_name, str) or not re.fullmatch(spec["name_re"], plugin_name):
                raise ValueError(f"invalid PLUGIN_NAME: {plugin_name!r}")
            if not callable(getattr(module, "create_plugin", None)):
                raise ValueError("no create_plugin")
        except BaseException as exc:
            fail(f"plugin {os.path.basename(directory)}", exc)
sys.exit(1 if failed else 0)
"""


def run_preflight(spec: WatchSpec, py_files: list[str], timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> tuple[str, str | None]:
    """`("ok", None)` or `("failed", first PREFLIGHT_ERROR_CHARS of stderr)`."""
    payload = {
        "sys_path": list(spec.sys_path),
        "root": str(spec.display_root),
        "home": str(spec.home),
        "compile": py_files,
        "modules": list(spec.import_modules),
        "plugins": [[str(d), spec.plugin_module_names.get(str(d)) or plugin_module_name(d)] for d in spec.plugin_dirs],
        "name_re": PLUGIN_NAME_RE,
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", PREFLIGHT_SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(spec.display_root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "failed", f"preflight timed out after {timeout:g}s"
    except OSError as exc:
        return "failed", f"preflight could not start: {exc}"
    if proc.returncode == 0:
        return "ok", None
    error = proc.stderr.strip() or f"preflight exited {proc.returncode}"
    return "failed", error[:PREFLIGHT_ERROR_CHARS]


class BusyProbe:
    """Live `busy` list: every plugin's `busy_reasons()`, under one time budget.

    Calls run on one worker thread per round, in plugin order, so a round costs
    one thread and a hung probe can leak at most one: while it is still stuck,
    later rounds skip it and report it busy instead of stacking threads on it.
    """

    def __init__(
        self, plugins: Mapping[str, object] | None, log: Callable[[str], None] | None = None, budget: float = BUSY_BUDGET_SECONDS
    ) -> None:
        self._probes = [
            (name, plugin.busy_reasons)
            for name, plugin in (plugins or {}).items()
            if callable(getattr(plugin, "busy_reasons", None))
        ]
        self._log = log or (lambda _message: None)
        self._budget = budget
        self._lock = threading.Lock()
        self._hung: dict[str, threading.Thread] = {}
        self._warned: set[str] = set()

    def reasons(self) -> list[str]:
        if not self._probes:
            return []
        with self._lock:
            for name in [n for n, t in self._hung.items() if not t.is_alive()]:
                del self._hung[name]
            hung = set(self._hung)
        probes = [(name, call) for name, call in self._probes if name not in hung]
        results: dict[str, list[str]] = {}
        current: list[str | None] = [None]

        def run() -> None:
            for name, call in probes:
                current[0] = name
                try:
                    raw = call()
                except Exception as exc:  # noqa: BLE001 — a plugin bug must never break /snapshot
                    self._warn_once(name, f"{type(exc).__name__}: {exc}")
                    results[name] = []
                    continue
                results[name] = clean_reasons(raw)
            current[0] = None

        worker = threading.Thread(target=run, daemon=True, name="freshness-busy")
        worker.start()
        worker.join(self._budget)
        if worker.is_alive() and current[0] is not None:
            with self._lock:
                self._hung[current[0]] = worker
            hung.add(current[0])
        reasons: list[str] = []
        for name, _call in self._probes:
            if name in results:
                reasons.extend(results[name])
            elif name in hung or worker.is_alive():
                reasons.append(f"{name}: busy check not answering")
        return reasons[:MAX_BUSY_REASONS]

    def _warn_once(self, name: str, detail: str) -> None:
        if name not in self._warned:
            self._warned.add(name)
            self._log(f"  freshness: plugin '{name}' busy_reasons() raised — {detail}")


def clean_reasons(raw: object) -> list[str]:
    """A plugin's answer, kept only as short strings."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [r.strip()[:MAX_BUSY_REASON_CHARS] for r in raw if isinstance(r, str) and r.strip()]


IDLE_PREFLIGHT = {"status": None, "error": None, "checked_at": None}


class FreshnessMonitor:
    """The watcher: a baseline at construction, `check()` per poll, and
    `snapshot()` for the `bridge.freshness` block. Thread-safe; `start()` runs
    `check()` on a daemon thread every `poll_seconds`."""

    def __init__(
        self,
        spec: WatchSpec,
        *,
        plugins: Mapping[str, object] | None = None,
        clock: Callable[[], float] = time.time,
        poll_seconds: float = POLL_SECONDS,
        quiet_seconds: float = QUIET_SECONDS,
        loaded_at: float | None = None,
        preflight: Callable[[WatchSpec, list[str]], tuple[str, str | None]] = run_preflight,
        log: Callable[[str], None] | None = None,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._spec = spec
        # The operator's switch (features.py `CODE_WATCH`), asked every poll, so it applies without a
        # restart. Off: no hashing and no preflight, and the wire says unknown, never current.
        self._enabled = enabled
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._quiet_seconds = quiet_seconds
        self._preflight_fn = preflight
        self._log = log or (lambda _message: None)
        self._busy = BusyProbe(plugins, self._log)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fingerprinter = Fingerprinter()
        self._current = self._fingerprinter.fingerprint(watched_paths(spec))
        self._baseline = dict(self._current)
        now = clock()
        self._last_change: float | None = None
        if loaded_at is not None:
            for path in self._baseline:
                mtime = self._fingerprinter.mtime_ns(path)
                if mtime is not None and mtime > loaded_at * 1e9:
                    self._baseline[path] = EDITED_DURING_STARTUP
                    self._last_change = now
        self._changed = diff_fingerprints(self._baseline, self._current)
        self._checked_at = now
        self._preflight = dict(IDLE_PREFLIGHT)
        self._preflight_key: str | None = None
        self._preflight_thread: threading.Thread | None = None

    # -- the poll ---------------------------------------------------------------
    def tick(self) -> None:
        """One turn of the loop: a `check()`, unless the operator switched the watch off."""
        if self._enabled():
            self.check()

    def check(self) -> None:
        current = self._fingerprinter.fingerprint(watched_paths(self._spec))
        now = self._clock()
        with self._lock:
            if current != self._current:
                self._current = current
                self._last_change = now
            self._changed = diff_fingerprints(self._baseline, current)
            self._checked_at = now
            if not self._changed:
                self._preflight = dict(IDLE_PREFLIGHT)
                self._preflight_key = None
                return
            key = fingerprint_key(current)
            if self._preflight_key == key:
                return  # this exact tree is checked, or being checked
            self._preflight = {"status": "pending", "error": None, "checked_at": None}
            running = self._preflight_thread is not None and self._preflight_thread.is_alive()
            quiet = self._last_change is None or now - self._last_change >= self._quiet_seconds
            if running or not quiet:
                return
            self._preflight_key = key
            py_files = sorted(p for p in current if p.endswith(".py"))
            self._preflight_thread = threading.Thread(
                target=self._run_preflight, args=(key, py_files), daemon=True, name="freshness-preflight"
            )
            self._preflight_thread.start()

    def _run_preflight(self, key: str, py_files: list[str]) -> None:
        try:
            status, error = self._preflight_fn(self._spec, py_files)
        except Exception as exc:  # noqa: BLE001 — a verdict of "failed" beats a dead thread
            status, error = "failed", f"preflight crashed: {type(exc).__name__}: {exc}"
        # Re-read the tree NOW rather than trust the last poll: an edit that
        # landed mid-run would otherwise wear this verdict until the next poll,
        # and an "ok" is exactly what licenses an auto-restart. A private
        # Fingerprinter, because the poll thread owns the shared one.
        try:
            after = fingerprint_key(Fingerprinter().fingerprint(watched_paths(self._spec)))
        except Exception:  # noqa: BLE001 — unverifiable means unrecorded
            after = None
        with self._lock:
            # A tree that moved while this ran keeps `pending`; the next quiet
            # poll checks the tree that is actually there.
            if self._preflight_key == key and after == key and fingerprint_key(self._current) == key:
                self._preflight = {"status": status, "error": error, "checked_at": self._clock()}
            elif self._preflight_key == key:
                self._preflight_key = None
        if status == "failed":
            self._log(f"  freshness: preflight FAILED — {error}")

    def join_preflight(self, timeout: float | None = None) -> None:
        thread = self._preflight_thread
        if thread is not None:
            thread.join(timeout)

    # -- the wire ---------------------------------------------------------------
    def snapshot(self) -> dict | None:
        """The `bridge.freshness` block. `busy` is asked live, every call. None while switched off:
        a tree nobody is hashing is unknown, and the consumer already reads null that way."""
        if not self._enabled():
            return None
        busy = self._busy.reasons()
        with self._lock:
            stale = bool(self._changed)
            root, home = self._spec.display_root, self._spec.home
            return {
                "stale": stale,
                "changed": [display_path(p, root, home) for p in self._changed[:CHANGED_CAP]],
                "changed_count": len(self._changed),
                "changed_at": self._last_change if stale else None,
                "checked_at": self._checked_at,
                "preflight": dict(self._preflight),
                "busy": busy,
            }

    # -- the thread -------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="freshness-watch").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the watcher must never take the bridge down
                self._log(f"  freshness: check failed — {type(exc).__name__}: {exc}")


def start_freshness(
    plugin_dirs: list[Path],
    plugins: Mapping[str, object] | None,
    plugins_config: Path | None,
    profiles_path: Path,
    log: Callable[[str], None],
    enabled: Callable[[], bool] = lambda: True,
) -> FreshnessMonitor | None:
    """Build, baseline and start the watcher; None (→ `freshness: null`, which
    the consumer reads as unknown, never as stale) when any of that fails."""
    try:
        monitor = FreshnessMonitor(
            bridge_watch_spec(plugin_dirs, plugins_config, profiles_path),
            plugins=plugins,
            loaded_at=IMPORTED_AT,
            log=log,
            enabled=enabled,
        )
        monitor.start()
    except Exception as exc:  # noqa: BLE001 — a broken watcher must never stop the bridge
        log(f"  freshness:         OFF — {type(exc).__name__}: {exc}")
        return None
    return monitor


def describe_freshness(block: object) -> str:
    """One line for `tldrack status`."""
    if not isinstance(block, dict):
        return "unknown (this bridge does not watch its code)"
    if not block.get("stale"):
        return "current"
    count = block.get("changed_count") or 0
    preflight = (block.get("preflight") or {}).get("status") or "pending"
    busy = block.get("busy") or []
    line = f"RESTART NEEDED — {count} file{'s' if count != 1 else ''} changed, preflight {preflight}"
    return line + (f", busy: {'; '.join(busy)}" if busy else "")
