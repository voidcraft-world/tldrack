"""Codex account limits for the rack's existing launch chips.

Read only token_count events in the configured profile's local rollouts; no
credentials or API requests. Window duration owns the mapping: a weekly-only
plan puts its week in primary, not secondary. The bounded tail scan is cached
per file and throttled per account so snapshot/SSE readers share the work.
Rollouts persist the reading; its event timestamp (never file mtime) owns age.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

SCAN_SECONDS = 15
MAX_FILES = 16
TAIL_BYTES = 512 * 1024
WINDOWS = {300: "five_hour", 10080: "seven_day"}


def parse_codex_usage(row: object) -> tuple[float, dict] | None:
    if not isinstance(row, dict) or row.get("type") != "event_msg":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
        return None
    try:
        at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("window_minutes")
        if not isinstance(duration, (int, float)) or duration not in WINDOWS:
            continue
        used, resets = window.get("used_percent"), window.get("resets_at")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (used, resets)):
            continue
        if not 0 <= used <= 100:
            continue
        windows[WINDOWS[duration]] = {"used_percentage": float(used), "resets_at": float(resets)}
    return (at, windows) if windows else None


def read_usage_tail(path: Path) -> tuple[float, dict] | None:
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - TAIL_BYTES))
        lines = handle.read(TAIL_BYTES).splitlines()
        if size > TAIL_BYTES:
            lines = lines[1:]  # Discard the cut first record.
    for line in reversed(lines):
        if b'"token_count"' not in line:
            continue
        try:
            reading = parse_codex_usage(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
        if reading is not None:
            return reading
    return None


class CodexUsageReader:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._roots: dict[str, tuple[float, dict]] = {}

    def get(self, config_dir: str | None) -> dict | None:
        if not config_dir:
            return None
        with self._lock:
            now = self._clock()
            checked, cached = self._roots.get(config_dir, (float("-inf"), {}))
            if now - checked >= SCAN_SECONDS:
                candidates = []
                root = Path(config_dir) / "sessions"
                try:
                    for path in root.glob("*/*/*/rollout-*.jsonl"):
                        try:
                            stat = path.stat()
                            candidates.append((stat.st_mtime_ns, stat.st_size, path))
                        except OSError:
                            continue
                except OSError:
                    pass
                current = {}
                for modified, size, path in sorted(candidates, reverse=True)[:MAX_FILES]:
                    stamp = (modified, size)
                    previous = cached.get(path)
                    if previous is not None and previous[0] == stamp:
                        current[path] = previous
                        continue
                    try:
                        reading = read_usage_tail(path)
                    except OSError:
                        reading = None
                    retained = previous[1] if previous and size >= previous[0][1] else None
                    current[path] = (stamp, reading or retained)
                cached = current
                self._roots[config_dir] = (now, cached)
            readings = [entry[1] for entry in cached.values() if entry[1] is not None]
            if not readings:
                return None
            at, windows = max(readings, key=lambda reading: reading[0])
            live = {name: dict(window) for name, window in windows.items() if window["resets_at"] > now}
            return {**live, "age_seconds": round(max(0, now - at), 1)} if live else None
