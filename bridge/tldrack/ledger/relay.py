"""Prompts an AGENT typed into another session, told apart from the operator's.

Agents steer each other by typing into tmux panes: a coordinator's `tmux send-keys -t wt-x -l "The
base moved: …"`, a session relaying "User update: …" to a Codex lane. In the receiving transcript
that prompt is indistinguishable from typing — same entry, same `turnOrigin: human` — and counted as
a human decision it would teach a judge that the operator said things they never said. On a
backfill, a visible share of the prompts in a Codex lane were typed by another agent.

The sender's transcript is what gives it away: a Bash/shell call whose command runs
`send-keys` / `paste-buffer`, just before a prompt lands in another conversation. This pass joins
the two across sessions after every ingest:

- **Text match** — the call's (redacted, clipped) command contains the prompt's first words: that
  prompt was typed by that call, at any distance inside `WINDOW_SECONDS`.
- **Time match** — no text in the summary (a buffer paste, a variable): only the single NEAREST
  human prompt in another conversation within `NEAREST_SECONDS`, so an operator typing elsewhere at
  the same moment is not swept up with it.

A matched prompt becomes `actor = machine`, `scope = relay`, its turn's origin `relay`, and `data`
names the sending conversation. Each pass first reverts every earlier mark and re-applies the current
rules to all of history (`_revert_marks`): the fold only re-derives files that changed, and a rule fix
must reach the ones that did not.

- **Marked by the rack** — a prompt that STARTS with a rack marker (`pane_typist.RACK_TAGS`) was
  typed by the bridge itself (a LEAD's wake, a lead's message to its worker). The marker is the
  evidence; there is no sender transcript to join.

What it cannot see: a bridge PLUGIN that types an UNMARKED first prompt (worktree `/create`, the
deploy fix agent, the canvas DRAW delivery, a Codex launch) leaves no transcript of its own. Those
first prompts still read as human: a known gap.
"""

from __future__ import annotations

import json
import re
import sqlite3

from tldrack.pane_typist import RACK_TAGS

WINDOW_SECONDS = 120.0
#: `send-keys` then Enter lands the prompt within a second or two; 30 s swept up an operator prompt
#: typed in another pane at the same time on the first backfill.
NEAREST_SECONDS = 8.0
MATCH_CHARS = 24
IMAGE_MARK = "[Image #"
TYPING = ("send-keys", "paste-buffer")
_SPACE = re.compile(r"\s+")


def _norm(text: str | None) -> str:
    return _SPACE.sub(" ", text or "").strip().lower()


def _revert_marks(conn: sqlite3.Connection) -> set[int]:
    """Undo every relay mark, so each pass applies the CURRENT rules to all history, not only to the
    files that changed since a rule did."""
    touched: set[int] = set()
    for event_id, file_id, kind, turn_idx in conn.execute(
        "SELECT id, file_id, kind, turn_idx FROM events WHERE scope = 'relay'"
    ).fetchall():
        conn.execute("UPDATE events SET actor = 'human', scope = NULL, data = NULL WHERE id = ?", (event_id,))
        if turn_idx is not None:
            conn.execute(
                "UPDATE turns SET origin = ? WHERE file_id = ? AND idx = ? AND origin = 'relay'",
                ("command" if kind == "command" else "human", file_id, turn_idx),
            )
        touched.add(file_id)
    return touched


def _mark_rack_typed(conn: sqlite3.Connection, touched: set[int]) -> int:
    """Prompts the bridge typed itself, known by their marker (module docstring)."""
    rows = conn.execute(
        "SELECT id, file_id, turn_idx FROM events WHERE actor = 'human' AND kind = 'prompt' AND ("
        + " OR ".join("ltrim(text) LIKE ?" for _ in RACK_TAGS)
        + ")",
        [f"{tag}%" for tag in RACK_TAGS],
    ).fetchall()
    for event_id, file_id, turn_idx in rows:
        conn.execute(
            "UPDATE events SET actor = 'machine', scope = 'relay', data = ? WHERE id = ?",
            (json.dumps({"typed_by": "rack", "match": "marker"}), event_id),
        )
        if turn_idx is not None:
            conn.execute("UPDATE turns SET origin = 'relay' WHERE file_id = ? AND idx = ?", (file_id, turn_idx))
        touched.add(file_id)
    return len(rows)


def mark_relayed_prompts(conn: sqlite3.Connection) -> int:
    touched = _revert_marks(conn)
    calls = conn.execute(
        "SELECT c.file_id, c.ts, c.summary, s.session_id FROM tool_calls c JOIN sessions s ON s.file_id = c.file_id "
        "WHERE c.tool IN ('Bash', 'shell') AND c.ts IS NOT NULL AND (" + " OR ".join("c.summary LIKE ?" for _ in TYPING) + ")",
        [f"%{word}%" for word in TYPING],
    ).fetchall()
    marked = 0
    for file_id, ts, summary, session_id in calls:
        candidates = conn.execute(
            "SELECT id, file_id, ts, text, turn_idx FROM events WHERE actor = 'human' AND kind IN ('prompt', 'command') "
            "AND file_id != ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (file_id, ts, ts + WINDOW_SECONDS),
        ).fetchall()
        if not candidates:
            continue
        typed = _norm(summary)
        hits = [c for c in candidates if len(_norm(c["text"])) >= 8 and _norm(c["text"])[:MATCH_CHARS] in typed]
        match = "text"
        if not hits:
            match = "time"
            nearest = candidates[0]
            # A pasted image is a clipboard act at a keyboard; send-keys cannot type one.
            pasted = IMAGE_MARK in (nearest["text"] or "")
            hits = [nearest] if nearest["ts"] - ts <= NEAREST_SECONDS and not pasted else []
        for hit in hits:
            data = json.dumps({"typed_by": {"file_id": file_id, "session_id": session_id, "at": ts}, "match": match})
            conn.execute("UPDATE events SET actor = 'machine', scope = 'relay', data = ? WHERE id = ?", (data, hit["id"]))
            if hit["turn_idx"] is not None:
                conn.execute("UPDATE turns SET origin = 'relay' WHERE file_id = ? AND idx = ?", (hit["file_id"], hit["turn_idx"]))
            touched.add(hit["file_id"])
            marked += 1
    marked += _mark_rack_typed(conn, touched)
    for file_id in touched:
        conn.execute(
            "UPDATE sessions SET "
            "human_events = (SELECT COUNT(*) FROM events WHERE file_id = ? AND actor = 'human'), "
            "human_turns = (SELECT COUNT(*) FROM turns WHERE file_id = ? AND origin IN ('human', 'command')) "
            "WHERE file_id = ?",
            (file_id, file_id, file_id),
        )
    return marked
