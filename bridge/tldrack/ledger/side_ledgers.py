"""The bridge's own ledgers, and the CLIs' prompt histories, folded into `bridge_rows`.

Each of these has one writer elsewhere and its own retention, and several FORGET: the permission
ledger keeps one `.1` (`permission_ledger.ROTATE_BYTES`), Laya's verdict ledger keeps two segments
(`plugins/laya/laya_bridge/ledger.py` `KEEP_SEGMENTS`). The session ledger reads them often
enough to hold every row before it rotates away, and keeps it forever. The writers do not change:
this is a reader, and their own readers keep their own windows.

Every source here is append-only JSONL, so a row's identity is the SHA-256 of its line: a re-read
after a rotation, a restart or a shrink inserts nothing twice (`bridge_rows` is UNIQUE on
`(source, row_key)`). Paths are spelled here, not imported from a plugin (a bridge module may not
import a plugin), so a plugin that lists these stores spells the same names.

The CLIs' `history.jsonl` files are the prompt log each keeps for up-arrow recall. Claude's outlives
its transcripts (it reaches back months while `projects/` holds 30 days), so it is the only record
left of every prompt typed before this ledger existed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tldrack import paths, permission_ledger
from tldrack.decisions import JUDGE_NAME, LEDGER_NAME, decisions_dir
from tldrack.ledger.claude_fold import iso_epoch
from tldrack.ledger.fold import clip

ROW_DATA_MAX = 64 * 1024
ACTIVITY_LOG_ENV = "TLDRACK_ACTIVITY_LOG_PATH"
ACTIVITY_LOG_NAME = "rack-activity.jsonl"
LAYA_OPERATOR_LOG = "agent-open-work-operator.jsonl"


@dataclass(frozen=True)
class SideLedger:
    source: str
    path: Path


def activity_log_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(ACTIVITY_LOG_ENV)
    return Path(override).expanduser() if override else paths.state_file(ACTIVITY_LOG_NAME)


def side_ledgers(
    claude_dirs: list[tuple[str, Path]], codex_dirs: list[tuple[str, Path]], env: dict[str, str] | None = None
) -> list[SideLedger]:
    """Every side source that exists right now. A missing file is not an error — most are optional."""
    env = os.environ if env is None else env
    out: list[SideLedger] = []
    permissions = permission_ledger.resolve_path(env)
    for path in (permissions, permissions.with_name(permissions.name + ".1")):
        out.append(SideLedger("permissions", path))
    activity = activity_log_path(env)
    try:  # the live file and its rotated `<stem>-<stamp>.jsonl` segments (activity_log.py)
        activity_files = sorted(activity.parent.glob(f"{activity.stem}*{activity.suffix}"))
    except OSError:
        activity_files = []
    out.extend(SideLedger("rack-activity", path) for path in activity_files)
    decisions = decisions_dir(env)
    out.append(SideLedger("decisions", decisions / LEDGER_NAME))
    out.append(SideLedger("decision-judge", decisions / JUDGE_NAME))
    # Laya's own ledgers, when that plugin has written any: an absent folder is no rows.
    laya = paths.store_dir("laya", create=False)
    if laya.is_dir():
        try:
            segments = sorted((laya / "ledger").glob("*.jsonl"))
        except OSError:
            segments = []
        for segment in segments:
            surface = segment.stem.split("-2", 1)[0]  # `<surface>-<YYYYMMDDTHHMMSS>` → surface
            out.append(SideLedger(f"laya:{surface}", segment))
        try:  # the landing record beside it, rotated the same way (laya_bridge/landing.py)
            landings = sorted((laya / "landing").glob("*.jsonl"))
        except OSError:
            landings = []
        for segment in landings:
            out.append(SideLedger(f"laya-landing:{segment.stem.split('-2', 1)[0]}", segment))
        out.append(SideLedger("laya-operator", laya / "data" / LAYA_OPERATOR_LOG))
    for profile, root in claude_dirs:
        out.append(SideLedger(f"claude-history:{profile}", root / "history.jsonl"))
    for profile, root in codex_dirs:
        out.append(SideLedger(f"codex-history:{profile}", root / "history.jsonl"))
    return [ledger for ledger in out if ledger.path.is_file()]


def _epoch(value: Any) -> float | None:
    if isinstance(value, str):
        return iso_epoch(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value / 1000.0 if value > 1e12 else value


def parse_row(source: str, line: bytes) -> tuple[str, float | None, str | None, str | None, str] | None:
    """One JSONL line → (row_key, ts, session, kind, data). None for a line that is not a JSON object."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        row = json.loads(stripped)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    key = hashlib.sha256(stripped).hexdigest()[:32]
    ts = None
    for field in ("at", "ts", "timestamp", "time", "answered_at"):
        ts = _epoch(row.get(field))
        if ts is not None:
            break
    session = next(
        (row[f] for f in ("session", "sessionId", "session_id", "subject") if isinstance(row.get(f), str) and row[f]), None
    )
    kind = next(
        (row[f] for f in ("t", "kind", "type", "action", "event", "outcome") if isinstance(row.get(f), str) and row[f]), None
    )
    if source.startswith("claude-history"):
        kind = "prompt"
    data = stripped.decode("utf-8", "replace")
    if len(data) > ROW_DATA_MAX:
        data = json.dumps({"truncated": True, "head": clip(data, ROW_DATA_MAX - 100)})
    return key, ts, session, kind, data
