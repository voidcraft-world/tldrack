"""Feature switches: the parts of a running rack the operator may turn off, from the page.

A bridge with no file runs everything: that is the whole product, and it is what a stranger
gets. The rack's Settings page (`ui/src/settings/`) lists each part with what it costs and what
is lost without it, and writes the answer here. `<config>/features.json` holds only what differs
from the default, which for nearly every part is what is OFF:

    {"version": 1, "off": ["code_watch", "plugin:phone-link"]}

A part that reaches off this machine or acts on it with no click is the other way round: off until
the operator switches it on, and then listed under `"on"`. Those are `update_check` (a `git fetch`
on a timer: a stranger's rack makes no network request they did not ask for) and `auto_update`
(pull, build, restart). A build that predates `"on"` reads the file as if that key were absent.

WHY NOT settings.json. That file is the operator's own hand, read once at startup, and it holds
the security posture (`allowed_origins`): no page may write it. This one is written by a page, so
it can only ever switch a part of this bridge OFF or back on. It names no path, no origin and no
code to load, which is why a plugin is switched by name and never added by one: what may mount
is still decided by `plugin_sources.py` alone, and this file subtracts from it.

TWO KINDS. A `now` switch is read by a loop on its next tick (the callables handed to
`freshness.py`, `agents_cli.py` and `ledger/scheduler.py`). A `restart` switch is read once, when
the bridge starts (a plugin is mounted or it is not); the view marks it `pending` until then,
and a restart stays the operator's call.

THE FILE IS TOLERANT, THE REQUEST IS STRICT. A file this bridge cannot read switches nothing off
and says so in the view and in `doctor`: a bad byte on disk must never take a part away silently,
and must never stop the bridge. A stored key this build does not know (a plugin since removed, a
switch from a newer build) is kept on disk and shown nowhere. A POST naming an unknown key, or a
value that is not true or false, is a 400.

Edited by hand while the bridge runs, the file is read again at the next start, not before.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from voidcraft_world_bridge.plugins import PLUGIN_NAME_RE
from voidcraft_world_bridge.shape import typed

from tldrack import paths

FEATURES_FILE = "features.json"
VERSION = 1
NOW, RESTART = "now", "restart"
BRIDGE, PLUGINS = "bridge", "plugins"
PLUGIN_PREFIX = "plugin:"
#: Stored keys are capped so a hand-made file cannot make every read walk a large list.
MAX_OFF = 64
MAX_KEY = 80

CODE_WATCH, PUBLIC_STATUS, LEDGER_INGEST = "code_watch", "public_status", "ledger_ingest"
UPDATE_CHECK, AUTO_UPDATE = "update_check", "auto_update"


@dataclass(frozen=True)
class Feature:
    key: str
    group: str
    title: str
    #: What it is for, what running it costs, and what the rack is without it: the page's three lines.
    does: str
    cost: str
    without: str
    applies: str = NOW
    #: Whether the page's LITE preset switches it off.
    lite: bool = False
    #: What a bridge with no file runs it as. False for the parts the module docstring names.
    default: bool = True


#: The bridge's own loops that have a switch. The state arbiter and the hooks are the rack, not a part of it.
BRIDGE_FEATURES: tuple[Feature, ...] = (
    Feature(
        CODE_WATCH,
        BRIDGE,
        "Code watch",
        "Notices when the bridge's own source changed under it, so `tldrack status` can say a restart is needed.",
        "Hashes the bridge and plugin source every 5 s.",
        "The bridge no longer knows its code is stale. Nothing else changes: useful only while you edit tldrack itself.",
        lite=True,
    ),
    Feature(
        PUBLIC_STATUS,
        BRIDGE,
        "Agent CLI cross-check",
        "Asks `claude agents --json` what each session is doing and compares it with the session index.",
        "One CLI run per Claude account every 15 s, every 5 s while the two disagree.",
        "Session state comes from the hooks and the session index alone, with no stand-in if that index stops parsing.",
        lite=True,
    ),
    Feature(
        LEDGER_INGEST,
        BRIDGE,
        "Session ledger ingest",
        "Folds finished conversations into the searchable ledger behind the rack's history search.",
        "A low-priority child process every 15 minutes; the first run on a long history takes minutes.",
        "History search stops seeing new conversations until you ingest by hand (`tldrack ledger ingest`, or INGEST NOW).",
        lite=True,
    ),
    Feature(
        UPDATE_CHECK,
        BRIDGE,
        "Update check",
        "Asks the checkout's git remote whether its main branch moved, so the ⚙ button can say an update is waiting.",
        "One `git fetch` of the checkout's own remote every 5 minutes: a network request this machine makes unasked.",
        "No update indicator. CHECK NOW and UPDATE on the Settings page still work when pressed.",
        lite=True,
        default=False,
    ),
    Feature(
        AUTO_UPDATE,
        BRIDGE,
        "Auto-update",
        "Checks the remote every 5 minutes and, when main moved, pulls, rebuilds the page and restarts the bridge, with no click.",
        "The update check's `git fetch`, then a page build and a bridge restart of a few seconds each time main moves. "
        "Sessions keep running.",
        "You press UPDATE yourself, on the Settings page.",
        default=False,
    ),
)

#: The parts that are off until switched on. A plugin never is: its key is not one of these.
DEFAULT_OFF = frozenset(feature.key for feature in BRIDGE_FEATURES if not feature.default)

#: The checkout's plugins, in the page's words. `plugins/README.md` owns what each one is; a plugin
#: directory not named here still gets a switch, with `UNKNOWN_PLUGIN`'s lines.
PLUGIN_NOTES: dict[str, tuple[str, str, str]] = {
    "worktree": (
        "The + WORKTREE button, the branch harbor and the PR button.",
        "Reads git and `gh pr list` while a rack page is open.",
        "Agents still run; you make worktrees and pull requests by hand.",
    ),
    "agent-canvas": (
        "The canvas pane: what an agent draws beside its terminal, and its one-click questions.",
        "A file watcher every 1.5 s, and a page in the rack that polls it.",
        "No canvas, no ◧ DRAW, no ◇ SKILLS.",
    ),
    "claude-skills": (
        "The terminal's skill buttons (SYNC, DEPS, CHECKS) and the skills view.",
        "One scan when the bridge starts.",
        "Those buttons are not drawn.",
    ),
    "laya": (
        "The local review model: reads a finished turn and marks the row ✅ or ⚠️.",
        "About 2 GB of disk, and a model server that loads on demand and stops after 15 idle minutes.",
        "Rows show 💬 reply ready instead of a verdict, and there is nothing to grade.",
    ),
    "phone-link": (
        "Grade verdicts from an Android phone over USB.",
        "A long-lived `adb track-devices` when adb is installed, and a check every 30 s while a phone is attached.",
        "The phone app shows UNLINKED.",
    ),
    "coordinator": (
        "Audits local Claude Code sessions for unfinished work.",
        "A model call per audit, when one is asked for.",
        "No audit findings.",
    ),
    "devices": (
        "The rack's DEVICES section: CPU and memory of this machine and each compute host.",
        "A sampler every 2 s while the section is open.",
        "No DEVICES section.",
    ),
}
UNKNOWN_PLUGIN = (
    "A plugin from your own plugin list.",
    "Whatever it starts when it mounts.",
    "Its routes and its part of the page.",
)


def plugin_key(name: str) -> str:
    return f"{PLUGIN_PREFIX}{name}"


def plugin_feature(name: str) -> Feature:
    does, cost, without = PLUGIN_NOTES.get(name, UNKNOWN_PLUGIN)
    return Feature(plugin_key(name), PLUGINS, name, does, cost, without, applies=RESTART)


def features_path() -> Path:
    return paths.config_file(FEATURES_FILE)


def parse_stored(body: object) -> tuple[frozenset[str], frozenset[str]]:
    """`features.json`'s parsed body → `(the keys that are off, the keys that are on)`. `"on"` is
    optional: only a default-off part is ever listed there. Raises `ValueError`."""
    data: dict = typed(body, dict, {})
    if not data or data.get("version") != VERSION:
        raise ValueError(f"expected an object with version {VERSION}")
    lists: list[frozenset[str]] = []
    for name, keys in (("off", data.get("off")), ("on", data.get("on", []))):
        if not isinstance(keys, list) or len(keys) > MAX_OFF:
            raise ValueError(f"{name} must be a list of at most {MAX_OFF} keys")
        if not all(isinstance(key, str) and 0 < len(key) <= MAX_KEY and key.isprintable() for key in keys):
            raise ValueError(f"every key in {name} must be a short one-line string")
        lists.append(frozenset(keys))
    return lists[0], lists[1]


class FeatureSwitches:
    """What is off, in memory and on disk. Thread-safe: the loops ask `on()` from their own threads."""

    def __init__(self, path: Path | None = None, plugins: Iterable[str] = ()) -> None:
        self.path = path or features_path()
        self._lock = RLock()
        self._plugins: tuple[str, ...] = ()
        self.error: str | None = None
        self._off: frozenset[str] = frozenset()
        self._on: frozenset[str] = frozenset()
        try:
            self._off, self._on = parse_stored(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error = f"{self.path.name} could not be read, so nothing is switched off: {exc}"
        #: What this process started with: a `restart` switch runs as this says, whatever is saved since.
        self._boot_off = self._off
        self.offer_plugins(plugins)

    def offer_plugins(self, names: Iterable[str]) -> None:
        """The plugin directories the source named (`plugin_sources.py`), by directory name: each
        one gets a switch. A name no plugin could have is not offered, and so is never skipped."""
        with self._lock:
            self._plugins = tuple(dict.fromkeys(name for name in names if PLUGIN_NAME_RE.fullmatch(name)))

    def known(self) -> list[Feature]:
        with self._lock:
            return [*BRIDGE_FEATURES, *(plugin_feature(name) for name in self._plugins)]

    def on(self, key: str) -> bool:
        with self._lock:
            return key in self._on if key in DEFAULT_OFF else key not in self._off

    def started_on(self, key: str) -> bool:
        """Whether `key` was on when this bridge started: what a `restart` switch runs as."""
        with self._lock:
            return key not in self._boot_off

    def off_keys(self) -> list[str]:
        """The known switches the operator turned off, for `doctor` and the startup echo. A part that
        is off by default and still off is not one of them."""
        with self._lock:
            return [feature.key for feature in self.known() if feature.default and feature.key in self._off]

    def view(self) -> dict:
        with self._lock:
            rows = []
            for feature in self.known():
                on = self.on(feature.key)
                rows.append(
                    {
                        "key": feature.key,
                        "group": feature.group,
                        "title": feature.title,
                        "does": feature.does,
                        "cost": feature.cost,
                        "without": feature.without,
                        "on": on,
                        "applies": feature.applies,
                        "pending": feature.applies == RESTART and on != (feature.key not in self._boot_off),
                        "lite": feature.lite,
                        "default": feature.default,
                    }
                )
            return {"features": rows, "error": self.error}

    def save(self, body: object) -> dict:
        """`{"set": {key: bool}}` → the new view. `ValueError` for a body this bridge does not take,
        `OSError` when the disk refused: Settings may say Saved only after the write landed."""
        changes = typed(body, dict, {}).get("set")
        if not isinstance(changes, dict) or not changes:
            raise ValueError('Send {"set": {"<feature key>": true or false}}')
        with self._lock:
            known = {feature.key for feature in self.known()}
            off, on = set(self._off), set(self._on)
            for key, value in changes.items():
                if key not in known:
                    raise ValueError(f"Unknown feature: {key!r}")
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
                # Each list holds only what differs from that part's default.
                listed, differs = (on, value) if key in DEFAULT_OFF else (off, not value)
                if differs:
                    listed.add(key)
                else:
                    listed.discard(key)
            if len(off) > MAX_OFF:
                raise ValueError(f"At most {MAX_OFF} switches can be off")
            self._write(off, on)
            self._off, self._on = frozenset(off), frozenset(on)
            # A save replaces the file, so whatever made it unreadable is gone.
            self.error = None
            return self.view()

    def _write(self, off: set[str], on: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="features-", suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump({"version": VERSION, "off": sorted(off), **({"on": sorted(on)} if on else {})}, handle)
                handle.write("\n")
            os.replace(name, self.path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)


def mountable(dirs: list[Path], switches: FeatureSwitches) -> tuple[list[Path], list[str]]:
    """`(the plugin dirs to mount, the names switched off)`. Offers every dir its switch first."""
    switches.offer_plugins(path.name for path in dirs)
    offered = {feature.key for feature in switches.known()}
    skipped = [path for path in dirs if plugin_key(path.name) in offered and not switches.on(plugin_key(path.name))]
    return [path for path in dirs if path not in skipped], [path.name for path in skipped]
