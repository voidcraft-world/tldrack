"""`tldrack doctor`: what this machine has, what is missing, and the one line that fixes each.

A stranger's first run fails for a small set of reasons: no tmux, no ttyd, no agent CLI, hooks
never installed, a port already held, a `paths.json` or `settings.json` with a typo. Each is a
`Check` here with a level and, when it is not ok, the fix. `doctor` changes nothing.

LEVELS. `FAIL` stops the rack from starting (tmux, ttyd, a bad configuration file). `WARN` leaves
it running with a part missing (no agent CLI, hooks not installed, the page not built). `OFF` is an
optional part the operator has not turned on, which is the default and not a problem: it is
listed so "is the namer on?" has one place to look. `OK` needs no words.

The checks are pure functions of a `Facts` value; `main.py` `doctor_facts` is the only part that
reads the machine, so every line `doctor` can print is testable without one.

TWO READERS. `tldrack doctor` prints `Check.line()`. The rack's Settings page reads the same
sections as JSON (`wire`, served at `GET /doctor` and printed by `doctor --json`), so the page and
the terminal can never disagree about what is missing. A check the page can fix with one request
names it in `action`: a closed set of kinds, each a route that already exists and already guards
itself. The page offers a button for a kind it knows and prints `fix` for the rest.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
MARK = {OK: "✓", WARN: "!", FAIL: "✗", OFF: "·"}
#: Tools the bridge cannot start without, with the fix.
REQUIRED_TOOLS = {
    "tmux": "brew install tmux, or your package manager's tmux",
    "ttyd": "brew install ttyd, or https://github.com/tsl0922/ttyd",
}
#: Tools a part of the rack needs: the page's build, and running from a checkout.
OPTIONAL_TOOLS = {
    "node": "install Node.js to build the page (ui/dist)",
    "uv": "install uv to run from a checkout: https://docs.astral.sh/uv/",
}
#: An agent CLI is what a session runs. One is enough.
AGENT_CLIS = ("claude", "codex")
#: `action.kind`: `POST /profiles/install-hooks {"profile": action.profile}`.
ACTION_INSTALL_HOOKS = "install-hooks"


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    detail: str = ""
    fix: str = ""
    #: What a page may offer to do about it (`ACTION_*`), or None. The terminal prints `fix`.
    action: Mapping[str, str] | None = None

    def line(self) -> str:
        text = f" {MARK[self.level]} {self.name:<24} {self.detail}".rstrip()
        return f"{text}\n     → {self.fix}" if self.fix and self.level != OK else text

    def wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "detail": self.detail,
            "fix": self.fix if self.level != OK else "",
            "action": dict(self.action) if self.action is not None and self.level != OK else None,
        }


@dataclass
class Facts:
    """Everything `doctor` reads from the machine, gathered once (`gather`)."""

    tools: Mapping[str, str | None]
    #: `paths.describe()`: roles and stores, or `{"error": …}`.
    paths: Mapping[str, Any]
    #: The optional parts and whether each is on, or the settings file's error text.
    settings: Mapping[str, bool] | str
    #: `(profile id, kind, cli found, hooks installed or None when it has none to install)`.
    profiles: list[tuple[str, str, bool, bool | None]] = field(default_factory=list)
    #: port → `(pid, command)` of its listener, or None when free.
    ports: Mapping[int, tuple[int, str] | None] = field(default_factory=dict)
    status_port: int = 7682
    #: The running bridge's `/snapshot`, or None when it does not answer.
    snapshot: Mapping[str, Any] | None = None
    ui_dist: Path | None = None
    #: Whether phone pushes have a topic (notify.json), and the suggester's stored mode.
    notify_on: bool = False
    next_step_mode: str = "off"
    #: Where the plugin list comes from (`plugin_sources.PluginSource.describe`), None when unknown.
    plugin_source: str | None = None
    #: The plugin names settings.json `plugins` gives an object to.
    plugin_settings: tuple[str, ...] = ()
    #: The feature switches that are off (features.py), and why its file could not be read, if so.
    features_off: tuple[str, ...] = ()
    features_error: str | None = None


def tool_checks(tools: Mapping[str, str | None]) -> list[Check]:
    checks = []
    for name, fix in REQUIRED_TOOLS.items():
        found = tools.get(name)
        checks.append(Check(name, OK, found) if found else Check(name, FAIL, "not found", fix))
    for name, fix in OPTIONAL_TOOLS.items():
        found = tools.get(name)
        checks.append(Check(name, OK, found) if found else Check(name, WARN, "not found", fix))
    agents = [name for name in AGENT_CLIS if tools.get(name)]
    checks.append(
        Check("agent CLI", OK, ", ".join(agents))
        if agents
        else Check(
            "agent CLI", WARN, "none found", f"install one of: {', '.join(AGENT_CLIS)}. Sessions still open, as plain shells"
        )
    )
    return checks


def path_checks(described: Mapping[str, Any]) -> list[Check]:
    if "error" in described:
        return [Check("paths", FAIL, str(described["error"]), "fix or remove paths.json")]
    checks = [Check(role, OK, f"{described[role]['dir']}  ({described[role]['source']})") for role in ("config", "state", "data")]
    mapped = {name: entry for name, entry in described.get("stores", {}).items() if entry["source"] != "data"}
    checks += [Check(f"store {name}", OK, f"{entry['dir']}  (paths.json)") for name, entry in sorted(mapped.items())]
    return checks


def hook_checks(profiles: list[tuple[str, str, bool, bool | None]]) -> list[Check]:
    checks = []
    for profile_id, kind, cli, hooks in profiles:
        if hooks is None:
            continue  # a plain shell has no hooks
        name = f"hooks {profile_id}"
        if not cli:
            checks.append(Check(name, OFF, f"the {kind} CLI is not installed"))
        elif hooks:
            checks.append(Check(name, OK, "installed"))
        else:
            checks.append(
                Check(
                    name,
                    WARN,
                    "not installed: its rows stay 'unknown'",
                    "tldrack hooks --install",
                    {"kind": ACTION_INSTALL_HOOKS, "profile": profile_id},
                )
            )
    return checks


def port_checks(ports: Mapping[int, tuple[int, str] | None], status_port: int, snapshot: Mapping[str, Any] | None) -> list[Check]:
    checks = []
    for port, holder in sorted(ports.items()):
        name = f"port {port}"
        if holder is None:
            checks.append(Check(name, OK, "free"))
            continue
        pid, command = holder
        ours = "tldrack" in command or "ttyd" in command
        if port == status_port and snapshot is None:
            checks.append(
                Check(
                    name,
                    FAIL,
                    f"held by pid {pid}, and /snapshot does not answer",
                    f"if that is a wedged bridge: tldrack kill && tldrack up; else quit pid {pid}",
                )
            )
        elif ours:
            checks.append(Check(name, OK, f"this rack (pid {pid})"))
        else:
            checks.append(
                Check(
                    name,
                    FAIL,
                    f"held by another program (pid {pid}: {command[:60]})",
                    "quit it, or run the rack elsewhere: tldrack up --port <n> --status-port <n>",
                )
            )
    return checks


def optional_checks(facts: Facts) -> list[Check]:
    if isinstance(facts.settings, str):
        return [Check("settings", FAIL, facts.settings, "fix or remove settings.json")]
    parts = dict(facts.settings)
    parts["next step"] = facts.next_step_mode != "off"
    parts["phone pushes"] = facts.notify_on
    how = {
        "session namer": 'settings.json "session_namer": true',
        "next step": "tldrack next-step --mode shadow",
        "phone pushes": "tldrack notify --init",
        "live marks": 'settings.json "lease_module"',
        "supervisor": 'settings.json "supervisor_url"',
        "extra origins": 'settings.json "allowed_origins"',
    }
    checks = [
        Check(name, OK if on else OFF, "on" if on else f"off  (turn on: {how.get(name, 'settings.json')})")
        for name, on in sorted(parts.items())
    ]
    checks.append(features_check(facts.features_off, facts.features_error))
    checks.append(plugins_check(facts))
    checks.append(
        plugin_settings_check(facts.plugin_settings, facts.snapshot.get("plugins") if facts.snapshot is not None else None)
    )
    return checks


def features_check(off: tuple[str, ...], error: str | None) -> Check:
    """What the operator switched off on the Settings page (features.py). Off is a choice, so it
    is listed, never warned about; a file that cannot be read is the warning."""
    if error:
        return Check("switched off", WARN, error, "fix or remove features.json, or save any switch on the Settings page")
    if not off:
        return Check("switched off", OK, "nothing: every part runs")
    return Check("switched off", OFF, f"{', '.join(off)}  (change: the rack's Settings page)")


def plugins_check(facts: Facts) -> Check:
    """What the running bridge mounts, and where the list comes from (`plugin_sources.py`)."""
    source = f"  (from {facts.plugin_source})" if facts.plugin_source else ""
    if facts.snapshot is None:
        return Check("plugins", OFF, f"the bridge is not running: start it to see what it mounts{source}")
    mounted = facts.snapshot.get("plugins")
    names = sorted(mounted) if isinstance(mounted, Mapping) else []
    return Check("plugins", OK if names else OFF, (", ".join(names) or "none mounted") + source)


def plugin_settings_check(configured: tuple[str, ...], mounted: object) -> Check:
    """settings.json `plugins`: which plugins have an object, and, with the bridge up, any that
    names a plugin it did not mount (a typo, or a plugin that failed to load)."""
    if not configured:
        return Check("plugin settings", OFF, 'none: every plugin at its defaults  (set: settings.json "plugins")')
    names = ", ".join(sorted(configured))
    if not isinstance(mounted, Mapping):
        return Check("plugin settings", OK, names)
    stray = sorted(name for name in configured if name not in mounted)
    if stray:
        return Check(
            "plugin settings",
            WARN,
            f"{names}; not mounted: {', '.join(stray)}",
            "check the name against the plugins line, or why that plugin did not load (bridge log)",
        )
    return Check("plugin settings", OK, names)


def ui_check(ui_dist: Path | None) -> Check:
    if ui_dist is not None:
        return Check("page", OK, str(ui_dist))
    return Check(
        "page",
        WARN,
        "ui/dist is not built: / says so instead of showing the rack",
        "tldrack up builds it from a checkout (needs node and npm)",
    )


def run_checks(facts: Facts) -> list[tuple[str, list[Check]]]:
    """Every section and its checks, in the order `doctor` prints them. Pure."""
    bridge = (
        Check("bridge", OK, f"up on :{facts.status_port}")
        if facts.snapshot is not None
        else Check("bridge", OFF, f"not running on :{facts.status_port}  (start: tldrack up)")
    )
    return [
        ("tools", tool_checks(facts.tools)),
        ("directories", path_checks(facts.paths)),
        ("ports", [*port_checks(facts.ports, facts.status_port, facts.snapshot), bridge]),
        ("hooks", hook_checks(facts.profiles)),
        ("page", [ui_check(facts.ui_dist)]),
        ("optional parts", optional_checks(facts)),
    ]


def verdict(sections: list[tuple[str, list[Check]]]) -> tuple[int, str]:
    """`(exit code, closing line)`: 1 only when something stops the rack from starting."""
    levels = [check.level for _, checks in sections for check in checks]
    failed, warned = levels.count(FAIL), levels.count(WARN)
    if failed:
        return 1, f"{failed} problem{'s' if failed != 1 else ''} to fix before the rack starts"
    if warned:
        return 0, f"ready, with {warned} part{'s' if warned != 1 else ''} missing"
    return 0, "ready"


def wire(sections: list[tuple[str, list[Check]]]) -> dict[str, Any]:
    """The sections as JSON, with the verdict: what `GET /doctor` answers and `doctor --json` prints."""
    code, closing = verdict(sections)
    return {
        "sections": [{"title": title, "checks": [check.wire() for check in checks]} for title, checks in sections],
        "exit": code,
        "verdict": closing,
    }


def find_tools() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS, *AGENT_CLIS, "npm")}
