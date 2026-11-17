"""Read the ledger back out as training and evaluation data. Every export is JSONL, newest last.

- **`pairs`** — one row per finished agent turn on a main thread: what the agent said last
  (`agent_said`), and what came NEXT — a human prompt and how long after, a machine prompt, or
  nothing. This is the golden-set shape for "did this turn end need a human?" (Laya's
  `agent_open_work` question), labelled by what the operator actually did rather than by a teacher.
- **`decisions`** — every human event with its context: prompts, interrupts, permission picks,
  AskUserQuestion answers, plans sent back. `--machine` adds the machine's (auto-review verdicts,
  auto-mode blocks), for comparing the two on the same kinds of call.

Search (`search`) is full text over messages, FTS5 where the build has it, else LIKE.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

PAIRS_SQL = """
SELECT s.source, s.profile, s.session_id, s.cwd, s.git_branch, s.model,
       t.idx, t.origin, t.prompt_ts, t.end_ts, t.tool_calls, t.interrupted, t.closing_text,
       n.origin AS next_origin, n.prompt_ts AS next_ts,
       (SELECT e.text FROM events e WHERE e.file_id = t.file_id AND e.turn_idx = t.idx + 1
          AND e.kind IN ('prompt', 'command') ORDER BY e.ts LIMIT 1) AS next_text,
       (SELECT COUNT(*) FROM events e WHERE e.file_id = t.file_id AND e.turn_idx = t.idx
          AND e.actor = 'human' AND e.kind NOT IN ('prompt', 'command')) AS in_turn_human
FROM turns t
JOIN sessions s ON s.file_id = t.file_id
LEFT JOIN turns n ON n.file_id = t.file_id AND n.idx = t.idx + 1
WHERE s.kind = 'main' AND t.closing_text IS NOT NULL AND COALESCE(t.end_ts, 0) >= ?
ORDER BY t.end_ts
"""

DECISIONS_SQL = """
SELECT s.source, s.profile, s.session_id, s.kind AS session_kind, s.cwd, s.git_branch,
       e.ts, e.actor, e.kind, e.decision, e.turn_idx, e.tool, e.summary, e.text, e.scope,
       e.latency_s, e.context, e.data
FROM events e
JOIN sessions s ON s.file_id = e.file_id
WHERE COALESCE(e.ts, 0) >= ? AND (e.actor = 'human' OR ?)
ORDER BY e.ts
"""


def export_pairs(conn: sqlite3.Connection, since: float = 0.0) -> Iterator[dict[str, Any]]:
    for row in conn.execute(PAIRS_SQL, (since,)):
        nxt = None
        if row["next_origin"] is not None:
            after = (row["next_ts"] - row["end_ts"]) if row["next_ts"] is not None and row["end_ts"] is not None else None
            nxt = {
                "origin": row["next_origin"],
                "after_s": round(after, 3) if after is not None else None,
                "text": row["next_text"],
            }
        yield {
            "source": row["source"],
            "profile": row["profile"],
            "session": row["session_id"],
            "cwd": row["cwd"],
            "branch": row["git_branch"],
            "model": row["model"],
            "turn": row["idx"],
            "origin": row["origin"],
            "started_at": row["prompt_ts"],
            "ended_at": row["end_ts"],
            "tool_calls": row["tool_calls"],
            "interrupted": bool(row["interrupted"]),
            "in_turn_human_events": row["in_turn_human"],
            "agent_said": row["closing_text"],
            "next": nxt,
        }


def export_decisions(conn: sqlite3.Connection, since: float = 0.0, machine: bool = False) -> Iterator[dict[str, Any]]:
    for row in conn.execute(DECISIONS_SQL, (since, 1 if machine else 0)):
        out = {k: row[k] for k in row.keys() if k != "data"}
        out["data"] = json.loads(row["data"]) if row["data"] else None
        yield out


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    from tldrack.ledger.store import fts_enabled

    if fts_enabled(conn):
        sql = (
            "SELECT s.session_id, s.source, s.profile, m.ts, m.role, "
            "snippet(messages_fts, 0, '[', ']', '…', 16) AS snippet "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN sessions s ON s.file_id = m.file_id WHERE messages_fts MATCH ? "
            "ORDER BY m.ts DESC LIMIT ?"
        )
        try:
            return [dict(r) for r in conn.execute(sql, (query, limit))]
        except sqlite3.OperationalError:
            pass  # not FTS syntax (a bare `-` or quote): fall through to LIKE
    sql = (
        "SELECT s.session_id, s.source, s.profile, m.ts, m.role, substr(m.text, 1, 200) AS snippet "
        "FROM messages m JOIN sessions s ON s.file_id = m.file_id WHERE m.text LIKE ? "
        "ORDER BY m.ts DESC LIMIT ?"
    )
    return [dict(r) for r in conn.execute(sql, (f"%{query}%", limit))]
