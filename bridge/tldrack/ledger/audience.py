"""Where the rack's ⚠️ landed, against what was running inside their conversation: the audience check.

The question, asked BEFORE any per-audience precision is trusted: of the ⚠️ the rack drew on a
`free` row, how many had their own in-process children (a background agent, a Workflow wave) still
running? Those are misclassified: the operator was shown news that was really "waiting on my agents".
The answer, and the lineage fix it led to: owned by tldrack/attention.py § 1b — see the note there.
`tldrack ledger audience` re-measures it any time, so the fix is checked, not asserted.

── WHAT IT READS (all read-only) ──────────────────────────────────────────────
- **The verdicts**: every `laya:agent_open_work` decision row the session ledger keeps (`bridge_rows`),
  a ⚠️ when its p reached the row's own `threshold` (`OPEN_WORK_AT` for a row from before the field).
- **The conversation**: `laya_links` (laya_links.py owns the join); an unlinked verdict is counted apart.
- **The children**: that conversation's `subagent` and `workflow` sessions (`sessions.parent_session_id`,
  the transcript's place on disk: claude_fold.py `session_shape`), RUNNING at the verdict when their first
  and last entries span its time within `CHILD_SLACK_SECONDS`.
- **The class**: the landing record's (`laya-landing:agent_open_work` rows, plugins/laya/laya_bridge/
  landing.py) when the verdict has one; else the rack's landing signal (`LANDING_SIGNALS`) for that
  conversation within `SIGNAL_MATCH_SECONDS` (rack-metrics.sqlite, since 2026-11-05); else `unknown`.

A `free` row is a ⚠️ that was not held: new, repeat or retired. Counts only: no text leaves.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from tldrack.attention import LANDING_CLASSES
from tldrack.open_work import LEDGER_SURFACE, OPEN_WORK_AT

VERDICT_SOURCE = f"laya:{LEDGER_SURFACE}"
LANDING_SOURCE = f"laya-landing:{LEDGER_SURFACE}"
#: A child counts as running when its [first entry, last entry] reaches this close to the verdict.
CHILD_SLACK_SECONDS = 1.0
#: A `warning_*` signal and its verdict land within a second or two of each other (main.py classifies
#: on the judge's thread right after the verdict lands); this is the widest gap that still joins them.
SIGNAL_MATCH_SECONDS = 10.0
#: The signals a landing writes (main.py `classify_verdict`), and only those: a `warning_dismissed` or
#: `warning_snoozed` is the operator acting on a ⚠️, and one near a verdict would otherwise outbid its class.
LANDING_SIGNALS = tuple(f"warning_{name}" for name in LANDING_CLASSES)
FREE_CLASSES = ("new", "repeat", "retired")
CLASS_ORDER = ("held:background", "held:children", "new", "repeat", "retired", "unknown")
CHILD_KINDS = ("workflow", "subagent", "none")


def _class_of(landing_class: object, held: object) -> str:
    if landing_class == "held":
        return f"held:{held}" if held in ("background", "children") else "unknown"
    return landing_class if landing_class in FREE_CLASSES else "unknown"


def _warnings(conn: sqlite3.Connection, since: float) -> list[dict]:
    out = []
    for ts, data in conn.execute(
        "SELECT ts, data FROM bridge_rows WHERE source = ? AND kind = 'decision' AND ts >= ?", (VERDICT_SOURCE, since)
    ):
        try:
            row = json.loads(data)
        except ValueError:
            continue
        decisions = row.get("decisions") if isinstance(row, dict) else None
        first = decisions[0] if isinstance(decisions, list) and decisions and isinstance(decisions[0], dict) else {}
        p, threshold = first.get("p"), row.get("threshold", OPEN_WORK_AT)
        if isinstance(p, (int, float)) and isinstance(threshold, (int, float)) and p >= threshold:
            out.append({"decision_id": row.get("decision_id"), "at": ts})
    return out


def _landings(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    for (data,) in conn.execute("SELECT data FROM bridge_rows WHERE source = ?", (LANDING_SOURCE,)):
        try:
            row = json.loads(data)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("decision_id"), str):
            out.setdefault(row["decision_id"], _class_of(row.get("class"), row.get("held")))
    return out


def _signals(metrics_path: Path | None) -> dict[str, list[tuple[float, str]]]:
    """conversation → [(at s, class)] from the rack's landing signals; empty when there is no database."""
    if metrics_path is None or not metrics_path.is_file():
        return {}
    out: dict[str, list[tuple[float, str]]] = defaultdict(list)
    conn = sqlite3.connect(f"file:{metrics_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            f"SELECT at, session_key, signal, reason FROM signals WHERE signal IN "
            f"({', '.join('?' * len(LANDING_SIGNALS))}) AND session_key IS NOT NULL",
            LANDING_SIGNALS,
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for at, key, signal, reason in rows:
        name = signal[len("warning_") :]
        out[key.rsplit(":", 1)[-1]].append((at / 1000.0, _class_of(name, reason)))
    return out


def measure(conn: sqlite3.Connection, *, since: float = 0.0, metrics_path: Path | None = None) -> dict:
    """The audience table over every ⚠️ since `since` (epoch s). Pure but for the two reads."""
    links = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT decision_id, conversation, at FROM laya_links")}
    children: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for parent, kind, started, ended in conn.execute(
        "SELECT parent_session_id, kind, started_at, ended_at FROM sessions WHERE kind IN ('subagent', 'workflow') "
        "AND parent_session_id IS NOT NULL AND started_at IS NOT NULL AND ended_at IS NOT NULL"
    ):
        children[parent].append((kind, started, ended))
    landings = _landings(conn)
    signals = _signals(metrics_path)
    table = {name: dict.fromkeys(CHILD_KINDS, 0) for name in CLASS_ORDER}
    unlinked = sourced_landing = sourced_signal = 0
    for warning in _warnings(conn, since):
        conversation, linked_at = links.get(warning["decision_id"], (None, None))
        if not conversation:
            unlinked += 1
            continue
        at = linked_at if isinstance(linked_at, (int, float)) else warning["at"]
        running = {
            kind
            for kind, started, ended in children.get(conversation, ())
            if started - CHILD_SLACK_SECONDS <= at <= ended + CHILD_SLACK_SECONDS
        }
        child = "workflow" if "workflow" in running else "subagent" if "subagent" in running else "none"
        name = landings.get(warning["decision_id"])
        if name is not None:
            sourced_landing += 1
        else:
            near = [
                (abs(signal_at - at), cls)
                for signal_at, cls in signals.get(conversation, ())
                if abs(signal_at - at) <= SIGNAL_MATCH_SECONDS
            ]
            name = min(near)[1] if near else "unknown"
            sourced_signal += int(bool(near))
        table[name][child] += 1
    free = [table[name] for name in FREE_CLASSES]
    held = [table[name] for name in ("held:background", "held:children")]
    known = free + held

    def with_children(cells: list[dict]) -> dict:
        return {"n": sum(c["workflow"] + c["subagent"] for c in cells), "of": sum(sum(c.values()) for c in cells)}

    return {
        "since": since,
        "warnings": sum(sum(cells.values()) for cells in table.values()) + unlinked,
        "unlinked": unlinked,
        "classified_by": {"landing": sourced_landing, "signal": sourced_signal},
        "classes": table,
        "free_with_children": with_children(free),
        "held_background_with_children": with_children([table["held:background"]]),
        "classified_with_children": with_children(known),
        "all_with_children": with_children(list(table.values())),
    }
