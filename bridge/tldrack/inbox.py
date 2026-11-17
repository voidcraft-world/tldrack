"""The decision inbox: one durable list of questions only the operator can answer (ladder rung 2).

The LEAD launch is the first source (`source: "lead"`); permission prompts, blocked rows,
scheduled jobs and deploy offers join the same table later as their own sources. One store, never a second list.

── WHO WRITES WHAT ───────────────────────────────────────────────────────────
Agents may WRITE questions (`ask`). Answers come only from the operator's UI: `answer`, `defer` and
`dismiss` are reached through routes that demand the per-launch secret (decision_secret.py), and no
CLI verb calls them. A row's `answered_via` records the Origin the answer came from.

── ASK ONCE ──────────────────────────────────────────────────────────────────
A question is fingerprinted (normalised title + detail). Asking again in the same run returns the
open row's key, or the answer already given, or the operator's dismissal, and never opens a second
row. Meaning-level duplicates are the asker's job: the lead reads every answered decision of its
run on each wake (`lead status`).

── BLOCKING, ASSUMPTIONS, CONTRADICTION ──────────────────────────────────────
A blocking row parks the steps it links (`decision_steps`) until it is answered or dismissed; a
deferral keeps them parked. A non-blocking row MUST carry the assumption the work goes on under.
Whether an answer contradicts that assumption is the operator's own press, never a model's reading:
KEEP, or the option equal to the assumption, holds it; anything else contradicts it. The reaction
(reopening the linked steps) runs INSIDE the answer's transaction, through the listeners a source
registers (lead/runs.py), which is why the inbox and the runs share one file (rack_db.py).

── DEFER ─────────────────────────────────────────────────────────────────────
`defer_until` hides a row from the open list until that time. `due()` returns each row whose time
has passed exactly once (`resurfaced_at`), so the caller can push one pointer for it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from tldrack.rack_db import RackDb

MAX_TITLE = 200
MAX_DETAIL = 2000
MAX_ASSUMPTION = 400
MAX_ANSWER = 2000
MAX_REASON = 400
MAX_OPTION = 60
MAX_OPTIONS = 6
MAX_LINKED_STEPS = 64
MAX_DEFER_SECONDS = 30 * 24 * 3600
SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
STEP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
ANSWER_KINDS = ("keep", "option", "text")

#: Called inside the answer/dismiss transaction: (conn, decision row as a dict, verb).
Listener = Callable[[sqlite3.Connection, dict, str], None]


class InboxError(ValueError):
    """A refused request; `status` is the HTTP status the route answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def flat(value: object, limit: int) -> str:
    """One line, bounded. A question is one agent's text landing in front of the operator and of
    other agents, so it can never forge a heading or a second line (missions.py, the same rule)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fingerprint(title: str, detail: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", f"{title} {detail}".lower()).split()
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:20]


def _same(a: object, b: object) -> bool:
    return isinstance(a, str) and isinstance(b, str) and flat(a, MAX_ANSWER).casefold() == flat(b, MAX_ANSWER).casefold()


def contradicts(row: Mapping[str, Any], kind: str, answer: str | None) -> bool | None:
    """Does this answer overturn the row's assumption? None for a blocking row (it had none)."""
    if row["blocking"]:
        return None
    if kind == "keep":
        return False
    return not _same(answer, row["assumption"])


def _view(row: sqlite3.Row | Mapping[str, Any], steps: list[str], now: float) -> dict:
    defer_until = row["defer_until"]
    return {
        "key": row["key"],
        "source": row["source"],
        "session": row["session"],
        "run_id": row["run_id"],
        "title": row["title"],
        "detail": row["detail"],
        "options": json.loads(row["options"]) if row["options"] else None,
        "opened_at": row["opened_at"],
        "defer_until": defer_until,
        "deferred": row["state"] == "open" and defer_until is not None and defer_until > now,
        "blocking": bool(row["blocking"]),
        "assumption": row["assumption"],
        "state": row["state"],
        "steps": steps,
    }


def _steps_of(conn: sqlite3.Connection, key: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT step_id FROM decision_steps WHERE decision_key = ? ORDER BY step_id", (key,))]


class Inbox:
    def __init__(self, db: RackDb, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock
        self._listeners: list[Listener] = []
        self._on_resolved: list[Callable[[dict], None]] = []

    def add_listener(self, listener: Listener) -> None:
        """A reaction that must commit with the answer (lead/runs.py reopens and parks)."""
        self._listeners.append(listener)

    def on_resolved(self, callback: Callable[[dict], None]) -> None:
        """Called AFTER the commit, outside the lock: the canon append, the snapshot push."""
        self._on_resolved.append(callback)

    # ── agents: ask ───────────────────────────────────────────────────────────

    def ask(
        self,
        *,
        source: str,
        title: object,
        detail: object,
        blocking: object,
        assumption: object = None,
        options: object = None,
        session: str | None = None,
        session_lineage: str | None = None,
        run_id: str | None = None,
        steps: Iterable[str] = (),
        key: str | None = None,
    ) -> dict:
        """Open a question, or return what an identical one already has. Never answers anything."""
        if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
            raise InboxError("source must be a short lowercase token")
        title_text, detail_text = flat(title, MAX_TITLE), flat(detail, MAX_DETAIL)
        if not title_text:
            raise InboxError("a question needs a title")
        if not isinstance(blocking, bool):
            raise InboxError("blocking must be true or false")
        assumed = flat(assumption, MAX_ASSUMPTION) or None
        if not blocking and assumed is None:
            raise InboxError("a non-blocking question must state the assumption the work goes on under")
        if blocking:
            assumed = None
        labels = self._options(options)
        linked = sorted(set(steps))
        if len(linked) > MAX_LINKED_STEPS or any(not STEP_RE.fullmatch(s) for s in linked):
            raise InboxError("steps must be short step ids")
        if key is not None and not KEY_RE.fullmatch(key):
            raise InboxError("invalid key")
        mark = fingerprint(title_text, detail_text)
        now = self._clock()
        with self.db.write() as conn:
            prior = conn.execute(
                "SELECT * FROM decisions WHERE fingerprint = ? AND source = ? AND "
                "COALESCE(run_id, '') = COALESCE(?, '') AND state != 'withdrawn' "
                "ORDER BY opened_at DESC LIMIT 1",
                (mark, source, run_id),
            ).fetchone()
            if prior is not None:
                return {
                    "key": prior["key"],
                    "outcome": "existing" if prior["state"] == "open" else prior["state"],
                    "answer": prior["answer"],
                    "answer_kind": prior["answer_kind"],
                }
            key = key or f"D-{secrets.token_hex(4)}"
            conn.execute(
                "INSERT INTO decisions (key, source, session, session_lineage, run_id, title, detail, options, "
                "opened_at, blocking, assumption, state, fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', ?)",
                (
                    key,
                    source,
                    session,
                    session_lineage,
                    run_id,
                    title_text,
                    detail_text,
                    json.dumps(labels) if labels else None,
                    now,
                    int(blocking),
                    assumed,
                    mark,
                ),
            )
            conn.executemany("INSERT INTO decision_steps VALUES (?, ?, ?)", [(key, run_id or "", step) for step in linked])
        return {"key": key, "outcome": "opened", "answer": None, "answer_kind": None}

    @staticmethod
    def _options(options: object) -> list[str] | None:
        if options is None:
            return None
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS:
            raise InboxError(f"options must be 2–{MAX_OPTIONS} labels")
        labels = [flat(o, MAX_OPTION) for o in options]
        if any(not label for label in labels) or len({label.casefold() for label in labels}) != len(labels):
            raise InboxError("options must be distinct, non-empty labels")
        return labels

    def withdraw(self, key: str, *, session: str | None = None) -> bool:
        """The asker takes back an open question it no longer needs. Answered rows stay."""
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone()
            if row is None or row["state"] != "open" or (session is not None and row["session"] != session):
                return False
            conn.execute("UPDATE decisions SET state = 'withdrawn' WHERE key = ?", (key,))
        return True

    # ── the operator: answer, defer, dismiss (secret-gated at the route) ───────

    def answer(self, key: object, kind: object, answer: object = None, *, via: str | None = None) -> dict:
        if kind not in ANSWER_KINDS:
            raise InboxError("kind must be keep, option or text")
        text = flat(answer, MAX_ANSWER) or None

        def apply(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> dict:
            if kind == "keep":
                if row["blocking"]:
                    raise InboxError("KEEP is for a question with an assumption; this one blocks", 409)
                value = row["assumption"]
            elif kind == "option":
                labels = json.loads(row["options"]) if row["options"] else []
                value = next((label for label in labels if _same(label, text)), None)
                if value is None:
                    raise InboxError("not one of this question's options")
            else:
                if text is None:
                    raise InboxError("an answer needs text")
                value = text
            against = contradicts(row, kind, value)
            conn.execute(
                "UPDATE decisions SET state = 'answered', answer = ?, answer_kind = ?, contradicts = ?, "
                "answered_at = ?, answered_via = ? WHERE key = ?",
                (value, kind, None if against is None else int(against), now, via, row["key"]),
            )
            return {"contradicts": against}

        return self._resolve(key, "answer", apply)

    def dismiss(self, key: object, reason: object, *, via: str | None = None) -> dict:
        why = flat(reason, MAX_REASON)
        if not why:
            raise InboxError("a dismissal needs a reason")

        def apply(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> dict:
            conn.execute(
                "UPDATE decisions SET state = 'dismissed', answer = ?, answer_kind = NULL, "
                "answered_at = ?, answered_via = ? WHERE key = ?",
                (why, now, via, row["key"]),
            )
            return {"contradicts": None}

        return self._resolve(key, "dismiss", apply)

    def defer(self, key: object, until: object, *, via: str | None = None) -> dict:
        now = self._clock()
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise InboxError("until must be epoch seconds")
        if not now < until <= now + MAX_DEFER_SECONDS:
            raise InboxError("until must be in the future and at most 30 days out")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise InboxError("invalid key")
        with self.db.write() as conn:
            self._open_row(conn, key)
            conn.execute(
                "UPDATE decisions SET defer_until = ?, resurfaced_at = NULL, answered_via = ? WHERE key = ?",
                (float(until), via, key),
            )
            updated = conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone()
            return {"decision": _view(updated, _steps_of(conn, key), now)}

    def _resolve(self, key: object, verb: str, apply: Callable[[sqlite3.Connection, sqlite3.Row, float], dict]) -> dict:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise InboxError("invalid key")
        now = self._clock()
        with self.db.write() as conn:
            row = self._open_row(conn, key)
            outcome = apply(conn, row, now)
            updated = dict(conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone())
            steps = _steps_of(conn, key)
            reactions: dict[str, Any] = {}
            for listener in self._listeners:
                result = listener(conn, {**updated, "steps": steps}, verb)
                if isinstance(result, dict):
                    reactions.update(result)
            view = _view(updated, steps, now)
        resolved = {**updated, "steps": steps, "verb": verb}
        for callback in self._on_resolved:
            try:
                callback(resolved)
            except Exception as err:  # a canon append or a push must never undo an answer
                print(f"tldrack: inbox follow-up failed ({type(err).__name__}: {err})")
        return {"decision": view, **outcome, **reactions}

    @staticmethod
    def _open_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise InboxError("no such decision", 404)
        if row["state"] != "open":
            raise InboxError(f"this decision is already {row['state']}", 409)
        return row

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, key: str) -> dict | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone()
            return _view(row, _steps_of(conn, key), self._clock()) if row is not None else None

    def open_rows(self, run_id: str | None = None, *, include_deferred: bool = True) -> list[dict]:
        """Open rows, newest first. Deferred ones carry `deferred: true` unless left out."""
        now = self._clock()
        sql = "SELECT * FROM decisions WHERE state = 'open'"
        args: tuple = ()
        if run_id is not None:
            sql += " AND run_id = ?"
            args = (run_id,)
        with self.db.read() as conn:
            rows = [_view(r, _steps_of(conn, r["key"]), now) for r in conn.execute(sql + " ORDER BY opened_at DESC", args)]
        return rows if include_deferred else [r for r in rows if not r["deferred"]]

    def resolved_rows(self, run_id: str) -> list[dict]:
        """Every answered or dismissed row of a run, oldest first, with what was said."""
        with self.db.read() as conn:
            return [
                {
                    "key": r["key"],
                    "title": r["title"],
                    "state": r["state"],
                    "answer": r["answer"],
                    "answer_kind": r["answer_kind"],
                    "assumption": r["assumption"],
                    "contradicts": None if r["contradicts"] is None else bool(r["contradicts"]),
                    "steps": _steps_of(conn, r["key"]),
                }
                for r in conn.execute(
                    "SELECT * FROM decisions WHERE run_id = ? AND state IN ('answered', 'dismissed') ORDER BY answered_at",
                    (run_id,),
                )
            ]

    def parked_steps(self, run_id: str) -> set[str]:
        """Steps an open (or deferred) BLOCKING decision holds. Computed, never stored."""
        with self.db.read() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT s.step_id FROM decision_steps s JOIN decisions d ON d.key = s.decision_key "
                    "WHERE d.run_id = ? AND d.state = 'open' AND d.blocking = 1",
                    (run_id,),
                )
            }

    def open_for_step(self, run_id: str, step_id: str) -> list[str]:
        """Keys of open or deferred decisions that link this step (the verified close's check 5)."""
        with self.db.read() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT d.key FROM decision_steps s JOIN decisions d ON d.key = s.decision_key "
                    "WHERE d.run_id = ? AND s.step_id = ? AND d.state = 'open'",
                    (run_id, step_id),
                )
            ]

    def due(self) -> list[dict]:
        """Deferred rows whose time has come, each returned ONCE (it is the caller's push)."""
        now = self._clock()
        with self.db.write() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE state = 'open' AND defer_until IS NOT NULL "
                "AND defer_until <= ? AND resurfaced_at IS NULL",
                (now,),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE decisions SET resurfaced_at = ? WHERE key = ?", (now, row["key"]))
            return [_view(r, _steps_of(conn, r["key"]), now) for r in rows]
