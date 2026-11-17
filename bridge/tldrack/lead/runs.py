"""Runs, steps, worker questions and events — the LEAD's memory, kept out of its conversation.

A lead may be compacted, killed or replaced at any point (lead/__init__.py owns the rule), so
everything a wake needs is here, in rack.sqlite, and `lead status` prints it. The store is also what
makes a confused or fresh lead safe: every verb is checked against the stored state (`spawn`
refuses a step that already has a worker, is closed or is parked), never against the lead's memory.

── THE STEP STATE MACHINE ────────────────────────────────────────────────────
    pending ─spawn→ spawning ─worker seen→ working ─ready→ ready ─PR→ published ─close→ closed
                                         └─(report) report→ reported ─close→ closed
    any live state ─contradicted assumption→ reopened ─tell / spawn --follow-up→ working / spawning
    spawning ─create failed→ failed ─spawn→ spawning          any ─abandon→ abandoned
`parked` is not a state: it is computed from the inbox (an open blocking decision links the step).

── EVENTS ────────────────────────────────────────────────────────────────────
`lead_events` is the run's history AND its wake queue. `wakes` says whether the event is worth waking
the lead for; `woken_at` is when a wake line covered it; `delivered_at` is when `lead status` showed it.
A wake is typed only for events not yet woken AND not yet delivered (the lead may have read them by
itself), so one event never costs two wakes.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from tldrack.inbox import InboxError, flat
from tldrack.rack_db import RackDb

RUN_ID_RE = re.compile(r"R\d{4}-[0-9a-f]{4}")
STEP_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,15}")
#: A step's kind, as stored in rack.sqlite and sent on the wire. People and the LEAD use the words in
#: KIND_WORDS: a `change` step delivers a branch, a PR or a merge; a `report` step a write-up. The stored
#: tokens are renamed to the words, with a read-old fallback, before v0.1.0.
KINDS = ("ship", "scout")
KIND_WORDS = {"ship": "change", "scout": "report"}
_KIND_TOKENS = {word: token for token, word in KIND_WORDS.items()} | {token: token for token in KINDS}


def kind_token(value: object) -> str | None:
    """The stored kind for `change` / `report`, or for a legacy `ship` / `scout`; None for anything else."""
    return _KIND_TOKENS.get(value) if isinstance(value, str) else None


def kind_word(token: str) -> str:
    """The word a person reads for a stored kind."""
    return KIND_WORDS.get(token, token)


LANES = ("claude", "codex", "any")
STATES = ("pending", "spawning", "working", "ready", "published", "reported", "closed", "reopened", "failed", "abandoned")
#: A worker may be spawned (fresh or as a follow-up) only from these.
SPAWNABLE = {
    "pending": "fresh",
    "failed": "fresh",
    "reopened": "follow_up",
    "working": "follow_up",
    "ready": "follow_up",
    "published": "follow_up",
    "reported": "follow_up",
}
#: States whose work an answer can overturn.
REOPENABLE = ("working", "ready", "published", "reported", "closed")
FINAL = ("closed", "abandoned")
DEFAULT_PARALLEL = 3
MAX_PARALLEL = 8
MAX_STEPS = 64
MAX_BRIEF = 12000
MAX_REPORT_BYTES = 512 * 1024
#: Events that wake the lead. `pr`, `spawned`, `due`, `route_wait` and a held assumption do not.
WAKING = frozenset({"ready", "ci", "merged", "stopped", "blocked", "asked", "gone", "answered", "dismissed", "failed"})


class LeadError(InboxError):
    """A refused lead verb; `status` is the HTTP status."""


def spawn_cap(step_count: int) -> int:
    """Total worker spawns a run may make: one per step, one retry each, two spare. A looping lead
    hits this long before it hurts a subscription."""
    return 2 * step_count + 2


def new_run_id(now: float) -> str:
    return f"R{time.strftime('%m%d', time.localtime(now))}-{secrets.token_hex(2)}"


def branch_for(run_id: str, step_id: str) -> str:
    return f"lead/{run_id.lower()}-{step_id.lower()}"


def deps_met(step: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], merged: set[str]) -> bool:
    """A change dependency must be MERGED (its code is on main for the next worker to branch from); a
    report dependency must be CLOSED (its report is saved)."""
    for dep in step["after"]:
        other = steps.get(dep)
        if other is None:
            return False
        if other["kind"] == "ship" and dep not in merged:
            return False
        if other["kind"] == "scout" and other["state"] != "closed":
            return False
    return True


def _row(r: sqlite3.Row) -> dict:
    out = dict(r)
    if "after" in out:
        out["after"] = json.loads(out["after"] or "[]")
    if "observed" in out:
        out["observed"] = json.loads(out["observed"] or "{}")
    if out.get("close_facts"):
        out["close_facts"] = json.loads(out["close_facts"])
    if "detail" in out and isinstance(out["detail"], str) and out["detail"].startswith("{"):
        out["detail"] = json.loads(out["detail"])
    return out


class LeadRuns:
    def __init__(self, db: RackDb, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock

    # ── lead sessions ─────────────────────────────────────────────────────────

    def note_lead(self, identity: str, session: str, profile: str | None) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO lead_sessions VALUES (?, ?, ?, ?) ON CONFLICT(identity) DO UPDATE "
                "SET session = excluded.session, profile = excluded.profile",
                (identity, session, profile, self._clock()),
            )

    def lead_identities(self) -> set[str]:
        """Every lineage id that is a lead: launched as one, or leading an open run."""
        with self.db.read() as conn:
            launched = {r[0] for r in conn.execute("SELECT identity FROM lead_sessions")}
            running = {r[0] for r in conn.execute("SELECT lead_identity FROM lead_runs WHERE state = 'open'")}
        return launched | running

    # ── runs ──────────────────────────────────────────────────────────────────

    def start(
        self,
        *,
        lead_identity: str,
        lead_session: str,
        profile: str | None,
        title: object,
        plan_path: str | None,
        plan_text: str,
        run_dir: Callable[[str], Path],
        parallel_max: object = DEFAULT_PARALLEL,
        lead_model: str | None = None,
        crew_model: str | None = None,
    ) -> dict:
        name = flat(title, 200)
        if not name:
            raise LeadError("a run needs a title")
        if not plan_text.strip():
            raise LeadError("the plan is empty")
        if isinstance(parallel_max, bool) or not isinstance(parallel_max, int) or not 1 <= parallel_max <= MAX_PARALLEL:
            raise LeadError(f"parallel must be 1–{MAX_PARALLEL}")
        now = self._clock()
        with self.db.write() as conn:
            busy = conn.execute(
                "SELECT id FROM lead_runs WHERE state = 'open' AND lead_identity = ?", (lead_identity,)
            ).fetchone()
            if busy is not None:
                raise LeadError(f"this lead already runs {busy[0]}; end it first", 409)
            run_id = new_run_id(now)
            while conn.execute("SELECT 1 FROM lead_runs WHERE id = ?", (run_id,)).fetchone():
                run_id = new_run_id(now)
            folder = run_dir(run_id)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy = folder / "plan.md"
            copy.write_text(plan_text, encoding="utf-8")
            digest = hashlib.sha256(plan_text.encode()).hexdigest()
            conn.execute(
                "INSERT INTO lead_runs (id, title, plan_path, plan_sha256, lead_identity, lead_session, profile, "
                "parallel_max, spawn_cap, state, created_at, lead_model, crew_model) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?)",
                (
                    run_id,
                    name,
                    plan_path or str(copy),
                    digest,
                    lead_identity,
                    lead_session,
                    profile,
                    parallel_max,
                    spawn_cap(0),
                    now,
                    lead_model,
                    crew_model,
                ),
            )
            self._event(conn, run_id, "started", None, {"plan": plan_path or str(copy), "sha256": digest[:12]})
        return self.run(run_id)

    def run(self, run_id: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM lead_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise LeadError("no such run", 404)
        return dict(row)

    def runs(self, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM lead_runs" + (" WHERE state = 'open'" if open_only else "") + " ORDER BY created_at DESC"
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def run_for_lead(self, identity: str) -> dict | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM lead_runs WHERE state = 'open' AND lead_identity = ?", (identity,)).fetchone()
        return dict(row) if row is not None else None

    def set_cap(self, run_id: str, parallel_max: object) -> dict:
        """Change a run's live-worker cap. Lowering it below the number of live workers kills nobody: it only holds
        new spawns (`next_actions`, `verb_spawn`) until enough have closed."""
        if isinstance(parallel_max, bool) or not isinstance(parallel_max, int) or not 1 <= parallel_max <= MAX_PARALLEL:
            raise LeadError(f"the cap must be 1–{MAX_PARALLEL}")
        with self.db.write() as conn:
            row = conn.execute("SELECT parallel_max, state FROM lead_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["state"] != "open":
                raise LeadError("only an open run's worker cap can change", 404)
            if row["parallel_max"] != parallel_max:
                conn.execute("UPDATE lead_runs SET parallel_max = ? WHERE id = ?", (parallel_max, run_id))
                self._event(conn, run_id, "cap", None, {"from": row["parallel_max"], "to": parallel_max})
        return self.run(run_id)

    def set_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"lead_session", "group_id", "start_sha", "profile", "lead_identity"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"unknown run fields: {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db.write() as conn:
            conn.execute(f"UPDATE lead_runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def end(self, run_id: str, report: object) -> dict:
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM lead_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["state"] != "open":
                raise LeadError("no open run by that id", 404)
            live = [
                r["step_id"]
                for r in conn.execute(
                    "SELECT step_id FROM lead_steps WHERE run_id = ? AND state NOT IN ('closed', 'abandoned')", (run_id,)
                )
            ]
            if live:
                raise LeadError(f"close or abandon {', '.join(live)} first", 409)
            conn.execute(
                "UPDATE lead_runs SET state = 'ended', ended_at = ?, report = ? WHERE id = ?",
                (self._clock(), flat(report, 4000) or None, run_id),
            )
            self._event(conn, run_id, "ended", None, {})
        return self.run(run_id)

    # ── steps ─────────────────────────────────────────────────────────────────

    def add_step(
        self,
        run_id: str,
        *,
        step_id: object,
        title: object,
        brief: object,
        kind: object = "change",
        lane: object = "claude",
        after: Iterable[str] = (),
    ) -> dict:
        if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id):
            raise LeadError("a step id is a short token such as S1")
        kind = kind_token(kind)
        if kind is None:
            raise LeadError("kind must be change or report")
        if lane not in LANES:
            raise LeadError("lane must be claude, codex or any")
        name = flat(title, 200)
        text = brief.strip() if isinstance(brief, str) else ""
        if not name or not text:
            raise LeadError("a step needs a title and a brief")
        if len(text) > MAX_BRIEF:
            raise LeadError(f"a brief is at most {MAX_BRIEF} characters; point at the plan for the rest")
        deps = list(dict.fromkeys(after))
        with self.db.write() as conn:
            run = conn.execute("SELECT * FROM lead_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["state"] != "open":
                raise LeadError("no open run by that id", 404)
            known = {r[0] for r in conn.execute("SELECT step_id FROM lead_steps WHERE run_id = ?", (run_id,))}
            if step_id in known:
                raise LeadError(f"{step_id} already exists in {run_id}", 409)
            if len(known) >= MAX_STEPS:
                raise LeadError(f"a run holds at most {MAX_STEPS} steps")
            missing = [d for d in deps if d not in known]
            if missing:
                raise LeadError(f"after names unknown steps: {', '.join(missing)} (add them first)")
            conn.execute(
                "INSERT INTO lead_steps (run_id, step_id, ord, title, brief, kind, lane, after, state) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (run_id, step_id, len(known), name, text, kind, lane, json.dumps(deps)),
            )
            conn.execute("UPDATE lead_runs SET spawn_cap = ? WHERE id = ?", (spawn_cap(len(known) + 1), run_id))
        return self.step(run_id, step_id)

    def steps(self, run_id: str) -> list[dict]:
        with self.db.read() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM lead_steps WHERE run_id = ? ORDER BY ord", (run_id,))]

    def step(self, run_id: str, step_id: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM lead_steps WHERE run_id = ? AND step_id = ?", (run_id, step_id)).fetchone()
        if row is None:
            raise LeadError(f"no step {step_id} in {run_id}", 404)
        return _row(row)

    def crew_step(self, session: str, lineage: str | None = None) -> dict | None:
        """The open step this worker session works, joined by name AND (once known) lineage id."""
        with self.db.read() as conn:
            for row in conn.execute(
                "SELECT s.* FROM lead_steps s JOIN lead_runs r ON r.id = s.run_id "
                "WHERE r.state = 'open' AND s.crew_session = ? AND s.state NOT IN ('closed', 'abandoned')",
                (session,),
            ):
                if lineage is None or row["crew_lineage"] in (None, lineage):
                    return _row(row)
        return None

    def update_step(
        self,
        run_id: str,
        step_id: str,
        *,
        event: tuple[str, dict] | None = None,
        expect: Iterable[str] | None = None,
        **fields: Any,
    ) -> dict:
        """Compare-and-set on `state` when `expect` is given, so a stale caller cannot move a step
        another path already moved."""
        allowed = {
            "state",
            "branch",
            "crew_session",
            "crew_lineage",
            "profile",
            "attempts",
            "report_path",
            "report_sha256",
            "observed",
            "closed_at",
            "close_facts",
        }
        if not set(fields) <= allowed:
            raise ValueError(f"unknown step fields: {set(fields) - allowed}")
        values = {k: json.dumps(v) if k in ("observed", "close_facts") and v is not None else v for k, v in fields.items()}
        with self.db.write() as conn:
            row = conn.execute("SELECT state FROM lead_steps WHERE run_id = ? AND step_id = ?", (run_id, step_id)).fetchone()
            if row is None:
                raise LeadError(f"no step {step_id} in {run_id}", 404)
            if expect is not None and row["state"] not in set(expect):
                raise LeadError(f"{step_id} is {row['state']} now", 409)
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE lead_steps SET {sets} WHERE run_id = ? AND step_id = ?", (*values.values(), run_id, step_id)
                )
            if event is not None:
                self._event(conn, run_id, event[0], step_id, event[1])
        return self.step(run_id, step_id)

    def note_spawn(
        self, run_id: str, step_id: str, *, expect: Iterable[str], branch: str, session: str, profile: str, follow_up: bool
    ) -> dict:
        """Claim the spawn BEFORE the worker exists: the cap is spent and the step leaves its spawnable
        state in one transaction, so two quick `spawn` calls cannot both launch a worker."""
        with self.db.write() as conn:
            run = conn.execute("SELECT * FROM lead_runs WHERE id = ?", (run_id,)).fetchone()
            row = conn.execute("SELECT * FROM lead_steps WHERE run_id = ? AND step_id = ?", (run_id, step_id)).fetchone()
            if run is None or row is None:
                raise LeadError("no such step", 404)
            if row["state"] not in set(expect):
                raise LeadError(f"{step_id} is {row['state']} now", 409)
            if run["spawns"] >= run["spawn_cap"]:
                raise LeadError(f"{run_id} has spent its {run['spawn_cap']} worker spawns; end or abandon steps", 429)
            conn.execute("UPDATE lead_runs SET spawns = spawns + 1 WHERE id = ?", (run_id,))
            conn.execute(
                "UPDATE lead_steps SET state = 'spawning', branch = ?, crew_session = ?, crew_lineage = NULL, "
                "profile = ?, attempts = attempts + 1, observed = '{}' WHERE run_id = ? AND step_id = ?",
                (branch, session, profile, run_id, step_id),
            )
            self._event(conn, run_id, "spawned", step_id, {"session": session, "profile": profile, "follow_up": follow_up})
        return self.step(run_id, step_id)

    def save_report(self, run_id: str, step_id: str, text: str, folder: Path) -> dict:
        data = text.encode("utf-8")
        if not data.strip():
            raise LeadError("the report is empty")
        if len(data) > MAX_REPORT_BYTES:
            raise LeadError("the report is over 512 KiB")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{step_id}.md"
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return self.update_step(
            run_id,
            step_id,
            report_path=str(path),
            report_sha256=digest,
            state="reported",
            expect=("working", "reopened"),
            event=("ready", {"report": path.name, "bytes": len(data)}),
        )

    # ── worker questions ────────────────────────────────────────────────────────

    def ask(self, run_id: str, step_id: str, session: str, text: object, *, blocking: bool, assumption: object = None) -> dict:
        question = flat(text, 1000)
        if not question:
            raise LeadError("a question needs text")
        assumed = None if blocking else flat(assumption, 400) or None
        if not blocking and assumed is None:
            raise LeadError("say what you are doing meanwhile (--assume), or mark it --blocking")
        qid = f"Q-{secrets.token_hex(3)}"
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO crew_questions VALUES (?,?,?,?,?,?,?,?, 'open', NULL, NULL, NULL)",
                (qid, run_id, step_id, session, question, int(blocking), assumed, self._clock()),
            )
            self._event(conn, run_id, "asked", step_id, {"question": qid, "blocking": blocking})
        return self.question(qid)

    def question(self, qid: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM crew_questions WHERE id = ?", (qid,)).fetchone()
        if row is None:
            raise LeadError("no such question", 404)
        return dict(row)

    def questions(self, run_id: str, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM crew_questions WHERE run_id = ?" + (" AND state = 'open'" if open_only else "")
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY opened_at", (run_id,))]

    def settle_question(
        self, qid: str, *, state: str, resolution: str | None = None, source: str | None = None, decision_key: str | None = None
    ) -> dict:
        with self.db.write() as conn:
            row = conn.execute("SELECT state FROM crew_questions WHERE id = ?", (qid,)).fetchone()
            if row is None:
                raise LeadError("no such question", 404)
            if row["state"] != "open":
                raise LeadError(f"{qid} is already {row['state']}", 409)
            conn.execute(
                "UPDATE crew_questions SET state = ?, resolution = ?, source = ?, decision_key = ? WHERE id = ?",
                (state, resolution, source, decision_key, qid),
            )
        return self.question(qid)

    # ── events ────────────────────────────────────────────────────────────────

    def _event(self, conn: sqlite3.Connection, run_id: str, kind: str, step_id: str | None, detail: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO lead_events (run_id, at, kind, step_id, detail, wakes) VALUES (?,?,?,?,?,?)",
            (run_id, self._clock(), kind, step_id, json.dumps(dict(detail)), int(kind in WAKING)),
        )

    def event(self, run_id: str, kind: str, step_id: str | None = None, detail: Mapping[str, Any] | None = None) -> None:
        with self.db.write() as conn:
            self._event(conn, run_id, kind, step_id, detail or {})

    def unwoken(self, run_id: str) -> list[dict]:
        with self.db.read() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    "SELECT * FROM lead_events WHERE run_id = ? AND wakes = 1 AND woken_at IS NULL "
                    "AND delivered_at IS NULL ORDER BY id",
                    (run_id,),
                )
            ]

    def mark_woken(self, ids: Iterable[int]) -> None:
        now = self._clock()
        with self.db.write() as conn:
            conn.executemany("UPDATE lead_events SET woken_at = ? WHERE id = ?", [(now, i) for i in ids])

    def take_undelivered(self, run_id: str) -> list[dict]:
        """Every event `lead status` has not shown yet, marked shown. Each is printed once."""
        now = self._clock()
        with self.db.write() as conn:
            rows = [
                _row(r)
                for r in conn.execute(
                    "SELECT * FROM lead_events WHERE run_id = ? AND delivered_at IS NULL ORDER BY id", (run_id,)
                )
            ]
            conn.execute("UPDATE lead_events SET delivered_at = ? WHERE run_id = ? AND delivered_at IS NULL", (now, run_id))
        return rows

    def history(self, run_id: str, limit: int = 40) -> list[dict]:
        with self.db.read() as conn:
            rows = [
                _row(r)
                for r in conn.execute("SELECT * FROM lead_events WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit))
            ]
        return rows[::-1]

    # ── the inbox's answer, inside its transaction ────────────────────────────

    def on_decision(self, conn: sqlite3.Connection, decision: Mapping[str, Any], verb: str) -> dict | None:
        """inbox.py listener. A contradicted assumption reopens the work built on it; a blocking
        answer unparks; a dismissal hands the call back to the lead. A held assumption changes
        nothing and wakes nobody."""
        run_id = decision.get("run_id")
        if decision.get("source") != "lead" or not run_id:
            return None
        steps = list(decision.get("steps") or ())
        detail = {
            "decision": decision["key"],
            "title": decision["title"],
            "answer": decision.get("answer"),
            "verb": verb,
            "steps": steps,
        }
        if verb == "dismiss":
            self._event(conn, run_id, "dismissed", None, detail)
            return {"reopened": []}
        reopened: list[str] = []
        if decision.get("contradicts") == 1:
            for step_id in steps:
                row = conn.execute("SELECT state FROM lead_steps WHERE run_id = ? AND step_id = ?", (run_id, step_id)).fetchone()
                if row is not None and row["state"] in REOPENABLE:
                    conn.execute("UPDATE lead_steps SET state = 'reopened' WHERE run_id = ? AND step_id = ?", (run_id, step_id))
                    reopened.append(step_id)
        detail["reopened"] = reopened
        if decision.get("blocking") or reopened or decision.get("contradicts") == 1:
            self._event(conn, run_id, "answered", None, detail)
        else:
            self._event(conn, run_id, "held", None, detail)  # the assumption held: recorded, no wake
        return {"reopened": reopened}


def next_actions(
    run: Mapping[str, Any],
    steps: list[Mapping[str, Any]],
    *,
    live: Mapping[str, Mapping | None],
    workers: Mapping[str, Mapping],
    parked: set[str],
    questions: list[Mapping],
) -> list[dict]:
    """What the lead should do now, worked out from stored and live facts alone. Pure.

    `live` maps a step id to its worker's live snapshot row (absent or None: no live worker); `workers`
    maps a branch to its task-group worker. Every verb this suggests is re-checked by its route."""
    by_id = {s["step_id"]: s for s in steps}
    merged = {s["step_id"] for s in steps if (workers.get(s.get("branch") or "") or {}).get("pr_state") == "merged"}
    actions: list[dict] = []
    for q in questions:
        actions.append(
            {
                "verb": "answer",
                "step": q["step_id"],
                "question": q["id"],
                "why": "answer it from the plan or an earlier decision, or escalate it",
            }
        )
    crew_live = sum(1 for s in steps if live.get(s["step_id"]) is not None or s["state"] == "spawning")
    room = max(0, run["parallel_max"] - crew_live)
    spare = max(0, run["spawn_cap"] - run["spawns"])
    for s in steps:
        sid, state = s["step_id"], s["state"]
        worker = workers.get(s.get("branch") or "") or {}
        row = live.get(sid)
        if state in ("pending", "failed"):
            if sid in parked:
                continue
            if not deps_met(s, by_id, merged):
                continue
            if room > 0 and spare > 0:
                actions.append({"verb": "spawn", "step": sid, "why": "dependencies met" if s["after"] else "ready to start"})
                room -= 1
                spare -= 1
        elif state == "reopened":
            verb = "tell" if row is not None else "spawn --follow-up"
            actions.append({"verb": verb, "step": sid, "why": "an answer overturned the assumption it was built on"})
        elif state == "published":
            ci = worker.get("ci")
            if ci == "failing":
                verb = "tell" if row is not None else "spawn --follow-up"
                actions.append({"verb": verb, "step": sid, "why": "CI is failing on its PR"})
            elif ci in ("passing", "none") and (row is None or row.get("state") == "free"):
                actions.append({"verb": "close", "step": sid, "why": f"PR open, CI {ci}"})
        elif state == "reported" and (row is None or row.get("state") == "free"):
            actions.append({"verb": "close", "step": sid, "why": "report saved"})
        elif state == "working" and row is None and s.get("crew_lineage"):
            actions.append({"verb": "spawn --follow-up", "step": sid, "why": "its worker session is gone"})
    if steps and all(s["state"] in FINAL for s in steps):
        actions.append({"verb": "end", "step": None, "why": "every step is closed or abandoned"})
    return actions
