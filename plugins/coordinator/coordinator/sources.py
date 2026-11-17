"""Where the evidence lives, and the one function that walks all of it.

Two sources, both under a Claude config dir — `~/.claude` (overridable with
`CLAUDE_CONFIG_DIR`, the same env var Claude Code itself honours), and every
OTHER account the bridge's launch profiles name (`claude_homes`): a second
account pinned by `CLAUDE_CONFIG_DIR=~/.claude-<name>` keeps its own copy of
both, and a single-root walk never saw one of its parked sessions.

- `sessions/<pid>.json` — a live index of running Claude processes, one small
  object each: `{pid, sessionId, cwd, startedAt, version, kind, entrypoint,
  name, status, updatedAt, waitingFor}` with `status ∈ busy | idle | waiting`.
  `waiting` is signal A and needs no transcript parsing at all. The same
  index, pid-checked and `~`-collapsed, is what `GET /live` serves
  (`live_agent_rows`) — the one place a Claude that never ran under tmux is
  visible.
- `projects/<slug>/<session-id>.jsonl` — the transcripts. A heavy user's
  directory runs to hundreds of MB, which is the whole reason for the bounded
  tail. What bounds the WALK is the age window, not the file cap — see below.

Everything here does I/O; the judgement it feeds is in `signals.py`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from coordinator.signals import (
    epoch_millis_to_datetime,
    scan_session_entry,
    scan_transcript,
    sort_findings,
)
from coordinator.tail import parse_timestamp, tail_records

#: Backstop against a transcript directory that grows without bound — NOT the
#: thing that decides what gets audited. The age window below is.
#:
#: A cap small enough to bind inside the window truncates the mtime-ordered list
#: and silently drops whatever sorts past it, severity-1 findings included, so
#: the cap sits well above a busy three weeks. Raising it costs little: each read
#: is a bounded 512 KB tail (`tail.TAIL_BYTES`), so a file costs the same to scan
#: however large it has grown.
DEFAULT_SCAN_LIMIT = 400

#: Findings older than this are dropped. Three weeks is roughly where "I never
#: finished that" stops being actionable and starts being archaeology.
DEFAULT_MAX_AGE_DAYS = 21

#: The `status` words the live index writes. Anything else — a value a newer
#: Claude Code invents, a half-written file — reads as `idle` on the wire: the
#: quietest rung, so an unknown state can never light a marker it did not earn.
#: `shell` is idle WITH background work running: owned by
#: bridge/tldrack/agent_index.py — see BACKGROUND WORK there.
LIVE_STATUSES = ("busy", "shell", "idle", "waiting")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def claude_homes(profiles: object = None) -> list[Path]:
    """Every Claude config dir to read: the env one, then each Claude launch
    profile's (`PluginContext.profiles.claude_roots()`), deduplicated by real
    path. A host without profiles reads exactly the one it always did."""
    homes = [claude_home()]
    roots = getattr(profiles, "claude_roots", None)
    for root in roots() if callable(roots) else []:
        homes.append(Path(root))
    unique: list[Path] = []
    seen: set[str] = set()
    for home in homes:
        real = os.path.realpath(home)
        if real not in seen:
            seen.add(real)
            unique.append(home)
    return unique


def read_live_sessions_all(homes: list[Path]) -> list[dict]:
    """`read_live_sessions` over every home's `sessions/` index, concatenated."""
    return [entry for home in homes for entry in read_live_sessions(home / "sessions")]


def owning_transcript(session_id: str, homes: list[Path]) -> Path | None:
    """The transcript file for `session_id` under any home, or None.

    Asked at RESUME time, never stored on a finding: findings persisted before
    a second account existed carry no owner, and a `claude --resume` run under
    the wrong `CLAUDE_CONFIG_DIR` answers "no conversation found". The caller
    has already pinned `session_id` to a uuid, so it cannot glob outward.
    """
    for home in homes:
        try:
            match = next((home / "projects").glob(f"*/{session_id}.jsonl"), None)
        except OSError:
            match = None
        if match is not None:
            return match
    return None


def sessions_dir() -> Path:
    return claude_home() / "sessions"


def projects_dir() -> Path:
    return claude_home() / "projects"


def read_live_sessions(directory: Path) -> list[dict]:
    """Every parseable entry in the live-session index. A half-written file (the
    index is rewritten by running Claude processes) is skipped, not fatal."""
    entries: list[dict] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return entries
    for path in files:
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def pid_alive(pid: object) -> bool:
    """Signal 0 is the loudest rung on the ladder, so a `waiting` entry left
    behind by a killed process must not earn it. `kill(pid, 0)` is the cheapest
    liveness probe there is; EPERM means the pid exists and is not ours."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def busy_session_ids(entries: list[dict]) -> set[str]:
    """Sessions Claude is actively working in right now.

    Excluded from the transcript scan: a busy session's tail is a snapshot of
    work in flight, not an abandoned thread, and one of them is very often the
    session that asked for this audit in the first place. `waiting` sessions are
    NOT excluded — being stuck is the whole point of signal A.
    """
    ids = set()
    for entry in entries:
        session_id = entry.get("sessionId")
        if entry.get("status") == "busy" and isinstance(session_id, str) and session_id:
            ids.add(session_id)
    return ids


def live_session_ids(entries: list[dict]) -> set[str]:
    """Every conversation a Claude process still has open, whatever its status.

    Deliberately WIDER than `busy_session_ids`, and for a different job. That one
    decides what is worth auditing; this one decides what must never be resumed.
    `claude --resume <id>` on a conversation that is already open puts two
    processes on one append-only transcript, interleaving their `parentUuid`
    chains and turning two agents loose in one repo — so `idle` and `waiting`
    are just as disqualifying as `busy` here.

    The pid check is what makes it safe to be this wide: the index is only
    rewritten by running processes, so a crashed session leaves its entry behind
    forever and a status-only test would refuse to resume it for good.
    """
    ids = set()
    for entry in entries:
        session_id = entry.get("sessionId")
        if isinstance(session_id, str) and session_id and pid_alive(entry.get("pid")):
            ids.add(session_id)
    return ids


def display_path(path: str | os.PathLike[str] | None) -> str | None:
    """`~`-collapsed, and the only form of a path `GET /live` ever returns.

    The home directory names the operator's account, and a GET is answered to
    any caller the host's guards admit, with no Origin check of its own. `~`
    keeps the path useful to a client that joins on it and keeps the account
    name off the wire. A path outside home is returned as it is.
    """
    if path is None:
        return None
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def session_timestamp_seconds(value: object) -> int | None:
    """An index timestamp as unix SECONDS, from either form it might arrive in.

    The index writes epoch milliseconds today (`signals.epoch_millis_to_datetime`
    owns that fact) while every other timestamp this plugin reads is an ISO
    string (`tail.parse_timestamp`). The file is Claude Code's private format
    with no contract behind it, so both are accepted rather than betting the
    "last seen" on the one a future release keeps. Seconds, and an int, because
    a client draws the row beside `/snapshot`'s `age_seconds` and git's
    `committerdate:unix` — one unit across three feeds is worth more than the
    millisecond.
    """
    moment = parse_timestamp(value) if isinstance(value, str) else epoch_millis_to_datetime(value)
    return int(moment.timestamp()) if moment is not None else None


def live_agent_rows(entries: list[dict], alive: Callable[[object], bool]) -> list[dict]:
    """The `GET /live` rows: every Claude process the index says is running.

    `alive` is injected (`pid_alive` in production) so this stays pure, the
    way `scan_session_entry` takes its liveness as a bool. The pid check is
    the same rule as `live_session_ids`, for the reason given there: the
    index is only rewritten by running processes, so a crashed session's entry
    lingers forever and a status-only read would report an agent that is not
    there. `pid` is typed here as well as probed — a permissive `alive` must
    not let a non-int onto the wire.

    Only these seven keys leave the file. The index also carries `waitingFor`,
    `entrypoint`, `version`, `messagingSocketPath` and `bridgeSessionId`, none
    of which a client needs to place an agent, and what a GET returns is the
    boundary (`display_path`). Sorted by pid so a client's change-detection
    sees the same document for the same rack regardless of glob order.
    """
    rows: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        pid = entry.get("pid")
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        if not alive(pid):
            continue
        cwd = entry.get("cwd")
        status = entry.get("status")
        name = entry.get("name")
        kind = entry.get("kind")
        rows.append(
            {
                "pid": pid,
                "session_id": session_id,
                "cwd": display_path(cwd) if isinstance(cwd, str) and cwd else None,
                "status": status if status in LIVE_STATUSES else "idle",
                "name": name if isinstance(name, str) and name else None,
                "kind": kind if isinstance(kind, str) and kind else None,
                "updated_at": session_timestamp_seconds(entry.get("updatedAt")),
            }
        )
    rows.sort(key=lambda row: row["pid"])
    return rows


def recent_transcripts(directory: Path, limit: int, min_mtime: float | None = None) -> list[Path]:
    """Transcripts touched since `min_mtime`, newest first, capped at `limit`.

    mtime is a PRE-FILTER only, and only ever used in the direction it is safe
    in. It can be off by days — anything that bulk-touches transcript files
    moves it — but always FRESH: a touch cannot make a file look older than its
    last record. So "mtime older than the age cutoff" does prove "nothing inside
    the window happened here" and is a sound way to skip a file unopened, while
    "mtime rank > N" proves nothing at all: the N most recent conversations are
    not the N most recently touched files, so a top-N slice hides live
    sessions. The true `last_active` still comes from a record timestamp once
    the file is open.
    """
    stamped = _stamped_transcripts(directory, min_mtime)
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _mtime, path in stamped[:limit]]


def _stamped_transcripts(directory: Path, min_mtime: float | None) -> list[tuple[float, Path]]:
    """`(mtime, path)` for every transcript under `directory` since `min_mtime`."""
    try:
        files = list(directory.glob("*/*.jsonl"))
    except OSError:
        return []
    stamped: list[tuple[float, Path]] = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # deleted between glob and stat
        if min_mtime is not None and mtime < min_mtime:
            continue
        stamped.append((mtime, path))
    return stamped


def scan_all(
    *,
    now: datetime | None = None,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    exclude_session_ids: set[str] | None = None,
    homes: list[Path] | None = None,
) -> dict:
    """One full heuristic audit → the `scan` half of a CoordinatorState.

    Returns `{scanned_at, scanned, ranked, error, findings}` with findings
    already sorted (severity ascending, then last_active descending) and
    age-filtered. Dismissals are NOT applied here — they are applied on the way
    out of the store, so dismissing takes effect without a re-scan.

    `homes` (default: just `claude_home()`) are walked TOGETHER: their
    transcripts are merged by mtime BEFORE the cap, so one busy account can
    never crowd the other out of the scan.
    """
    moment = now or datetime.now(timezone.utc)
    excluded = set(exclude_session_ids or set())
    homes = homes or [claude_home()]

    max_age_hours = max_age_days * 24
    entries = read_live_sessions_all(homes)
    excluded |= busy_session_ids(entries)
    # The age window, not the cap, is what bounds the walk: a file untouched
    # since the cutoff cannot hold a finding inside it (mtime only ever
    # over-reports freshness), so it is skipped without being opened.
    cutoff = moment.timestamp() - max_age_hours * 3600
    stamped = [pair for home in homes for pair in _stamped_transcripts(home / "projects", cutoff)]
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    transcripts = [path for _mtime, path in stamped[:scan_limit]]
    # The live index says nothing about which project a session belongs to, and
    # the directory name is a lossy encoding of the cwd — so it is LOOKED UP
    # from the transcript filename rather than reconstructed by string surgery.
    project_by_session = {path.stem: path.parent.name for path in transcripts}

    findings: list[dict] = []
    for entry in entries:
        blocked = scan_session_entry(entry, moment, pid_alive(entry.get("pid")))
        if blocked is not None:
            blocked["project"] = project_by_session.get(blocked["session_id"])
            findings.append(blocked)

    scanned = 0
    for path in transcripts:
        if path.stem in excluded:
            continue
        scanned += 1
        findings.extend(scan_transcript(path.stem, path.parent.name, tail_records(path), moment))

    findings = [f for f in findings if f["age_hours"] is None or f["age_hours"] <= max_age_hours]
    return {
        "scanned_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scanned": scanned,
        "ranked": False,
        "error": None,
        "findings": sort_findings(findings),
    }
