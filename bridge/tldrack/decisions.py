"""The decision ledger: every question an agent asks the operator, and the answer — the canon.

Spawn turned two years of operator decisions into precedent an AI judge applies, and the operator now
decides a few dozen questions out of thousands. This is the same idea at one operator's size, built
step by step. The steps, and which ones exist:

1. CAPTURE (here). Claude's `AskUserQuestion` reaches the bridge twice through the `http` relay:
   `PreToolUse` with the questions and options, `PostToolUse` with the answers. Each answered
   question becomes one row in `ledger.jsonl` of the `decisions` store (paths.py). Operator decisions
   written in prose in a project's documents (`Decision (user, YYYY-MM-DD): …`) are imported into the
   same file by `tldrack decisions import`, marked `source: doc`. Which documents: the globs the
   operator lists (settings.py `decision_docs`); none by default (`doc_files`). The `PostToolUse` half reaches the
   bridge only from an account whose hooks were installed after it joined the relay (`ANSWER_HOOK`);
   an older install sends every question and no answer, so nothing is written and nothing errors.
   `capture_blocked` names those accounts, and `since_start` counts both halves, on the DECISIONS
   readout. Both were added 2026-11-09, after a run of questions with no recorded answers.
2. SHADOW JUDGE (here). On `PreToolUse` the bridge asks Laya, locally and for $0, which option the
   operator will pick, given the nearest precedents in the ledger, as a typed `choice` question
   (`schemas/src/decide.ts`). It only RECORDS the prediction (`decisions/judge.jsonl`); when the real
   answer lands, the row says whether the judge agreed. Nothing is answered for the operator.
3. ACTIVE JUDGE (not built). Once the agreement rate is high enough on enough rows, a PreToolUse
   answer above a confidence cut denies the question with the precedent quoted, and the agent goes
   on. That switch is an operator decision, made from the AGENT RACK › DECISIONS readout, never on its own.

── WHAT IS STORED ──────────────────────────────────────────────────────────────
The question, its header and option labels, the answer, the session, the mission and the repo-relative
cwd tail. This is the operator's own Mac and the operator's own answers; it never leaves it (the bridge
has no network path out). Text is bounded (`MAX_TEXT`) and each row is one JSON line.

── NEVER IN THE HOOK'S WAY ─────────────────────────────────────────────────────
`/hook` holds the agent's tool call while it runs, so capture is an append under a lock and the judge
runs on its own worker thread, latest-wins per session. A Laya that is cold, missing or slow costs a
missing prediction, never a slow hook.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from tldrack import paths

DECISIONS_DIR_ENV = "TLDRACK_DECISIONS_DIR"
LEDGER_NAME = "ledger.jsonl"
JUDGE_NAME = "judge.jsonl"
MAX_TEXT = 600
MAX_OPTIONS = 8
#: How many prior decisions the judge sees for one question.
PRECEDENTS = 6
#: The ledger is read whole for precedent lookups; past this it reads the newest rows only.
MAX_READ_ROWS = 5_000
#: The Claude hook that carries an `AskUserQuestion`'s answers, as `(event, matcher)`. main.py installs
#: it (`CLAUDE_MATCHED_HOOKS`) and routes on its tool name; `capture_blocked` checks the accounts for it.
ANSWER_HOOK = ("PostToolUse", "AskUserQuestion")
SOURCE_HOOK = "hook"
SOURCE_DOC = "doc"
#: Answered in the decision inbox (inbox.py) — the LEAD launch's escalations, first.
SOURCE_INBOX = "inbox"
#: The two ways a document records an operator call with a date, each `(date, text)`. Both
#: examples are invented:
#:   `Decision (user, 2000-01-02): keep the broad rules.`
#:   `The operator, 2000-01-04: *"Keep the sidebar closed until someone opens it."*`
#: An undated `Decided:` / `Operator decision:` is left out on purpose: it catches list fragments
#: more often than calls, and a precedent the judge cannot trust is worse than none.
DOC_DECISION_RES = (
    re.compile(r"[Dd]ecision \(user, (\d{4}-\d{2}-\d{2})\)\)?:\s*(.{10,400})"),
    re.compile(r"(?:operator|user)[^.\n]{0,40}?(\d{4}-\d{2}-\d{2})[^\n]{0,20}?[:,]\s*\*?\"([^\"\n]{10,400})\""),
)
_WORD = re.compile(r"[a-z0-9]{3,}")


def decisions_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where the decision ledger lives: the override, else the `decisions` store (paths.py)."""
    override = (os.environ if env is None else env).get(DECISIONS_DIR_ENV)
    return Path(override).expanduser() if override else paths.store_dir("decisions")


def doc_files(root: Path, globs: Iterable[str]) -> list[Path]:
    """The files `globs` name under `root`, sorted, each once. A glob is relative to `root` and
    stays inside it: an absolute one, or one that climbs with `..`, names nothing."""
    try:
        base = root.resolve()
    except OSError:
        return []
    found: set[Path] = set()
    for pattern in globs:
        if not pattern or os.path.isabs(pattern) or ".." in Path(pattern).parts:
            continue
        try:
            matches = list(base.glob(pattern))
        except (OSError, ValueError):
            continue
        for match in matches:
            if match.is_file() and match.resolve().is_relative_to(base):
                found.add(match)
    return sorted(found)


def _text(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def parse_questions(tool_input: object) -> list[dict]:
    """AskUserQuestion's `questions` → `[{question, header, options: [label], multi}]` (pure).
    Anything malformed is dropped rather than half-stored."""
    questions = tool_input.get("questions") if isinstance(tool_input, Mapping) else None
    out: list[dict] = []
    for q in questions if isinstance(questions, list) else ():
        if not isinstance(q, Mapping) or not _text(q.get("question")):
            continue
        options = [_text(o.get("label"), 120) for o in q.get("options") or () if isinstance(o, Mapping)]
        options = [o for o in options if o][:MAX_OPTIONS]
        if len(options) < 2:
            continue
        out.append(
            {
                "question": _text(q.get("question")),
                "header": _text(q.get("header"), 40),
                "options": options,
                "multi": bool(q.get("multiSelect")),
            }
        )
    return out


def parse_answers(tool_input: object, tool_response: object) -> dict[str, str]:
    """`{question text: answer}` from a PostToolUse payload (pure). Claude Code puts the answers in
    `tool_input.answers` (the permission component fills it) and echoes them in the response; either
    is read, input first."""
    for source in (tool_input, tool_response):
        answers = source.get("answers") if isinstance(source, Mapping) else None
        if isinstance(answers, Mapping) and answers:
            return {_text(k): _text(v) for k, v in answers.items() if _text(k) and _text(v)}
    return {}


def tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def nearest(rows: Iterable[Mapping[str, Any]], question: str, k: int = PRECEDENTS) -> list[dict]:
    """The `k` prior decisions sharing the most words with `question`, newest first on ties (pure)."""
    want = tokens(question)
    if not want:
        return []
    scored = []
    for index, row in enumerate(rows):
        overlap = len(want & tokens(f"{row.get('question', '')} {row.get('header', '')}"))
        if overlap:
            scored.append((overlap, index, row))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [dict(row) for _, _, row in scored[:k]]


def doc_decisions(path: Path, text: str) -> list[dict]:
    """Every operator call written in one doc (`DOC_DECISION_RES`) → ledger rows, once per text (pure)."""
    rows, seen = [], set()
    for pattern in DOC_DECISION_RES:
        for match in pattern.finditer(text):
            said = _text(match.group(2).strip(" *_"))
            if said and said not in seen:
                seen.add(said)
                rows.append(
                    {"source": SOURCE_DOC, "date": match.group(1) or None, "question": said, "answer": said, "doc": str(path)}
                )
    return rows


def capture_blocked(profiles: Iterable[Mapping[str, Any]]) -> list[str]:
    """Labels of the Claude accounts whose hooks file relays no `ANSWER_HOOK` (or has no file), so an
    answer given there never becomes a row (pure). `profiles` is `/snapshot`'s `profiles` list
    (`profiles.ProfileRegistry.summary`); an account with no CLI on this Mac spawns nothing and is left out."""
    blocked = []
    for profile in profiles:
        missing = profile.get("hooks_missing") or ()
        if (
            profile.get("kind") == "claude"
            and profile.get("cli_found") is not False
            and (ANSWER_HOOK[0] in missing or "file" in missing)
        ):
            blocked.append(str(profile.get("label") or profile.get("id")))
    return blocked


class DecisionLedger:
    """Append-only JSONL, plus the pending questions of each session between PreToolUse and PostToolUse."""

    def __init__(self, directory: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, list[dict]] = {}
        #: Both hook halves since this bridge started, and the rows they made (`capture_blocked` says why
        #: `answered` can stay 0). In memory only: a restart starts the count again.
        self._since_start = {"asked": 0, "answered": 0, "rows": 0}

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def path(self, name: str = LEDGER_NAME) -> Path | None:
        return self._dir / name if self._dir is not None else None

    def _append(self, name: str, rows: Iterable[Mapping[str, Any]]) -> None:
        path = self.path(name)
        if path is None:
            return
        lines = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        if not lines:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(lines)
        except OSError as err:
            print(f"tldrack: could not append to {path} ({err})")

    def read(self, name: str = LEDGER_NAME, limit: int = MAX_READ_ROWS) -> list[dict]:
        path = self.path(name)
        if path is None or not path.is_file():
            return []
        rows: list[dict] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return []
        return rows[-limit:]

    def asked(self, session: str, tool_input: object) -> list[dict]:
        """PreToolUse: hold the questions until their answers arrive. Returns them for the judge."""
        questions = parse_questions(tool_input)
        with self._lock:
            if questions:
                self._pending[session] = questions
                self._since_start["asked"] += 1
            else:
                self._pending.pop(session, None)
        return questions

    def answered(
        self, session: str, tool_input: object, tool_response: object, *, mission: str | None = None, cwd: str | None = None
    ) -> list[dict]:
        """PostToolUse: one row per answered question. Returns the rows written."""
        answers = parse_answers(tool_input, tool_response)
        with self._lock:
            questions = self._pending.pop(session, None) or parse_questions(tool_input)
            self._since_start["answered"] += 1
        now = self._clock()
        rows = []
        for q in questions:
            answer = answers.get(q["question"])
            if not answer:
                continue
            rows.append(
                {
                    "source": SOURCE_HOOK,
                    "at": round(now, 3),
                    "session": session,
                    "mission": mission,
                    "cwd": Path(cwd).name if isinstance(cwd, str) and cwd else None,
                    "question": q["question"],
                    "header": q["header"],
                    "options": q["options"],
                    "multi": q["multi"],
                    "answer": answer,
                    "picked_offered": answer in q["options"],
                }
            )
        with self._lock:
            self._since_start["rows"] += len(rows)
            self._append(LEDGER_NAME, rows)
        return rows

    def record_inbox(self, decision: Mapping[str, Any]) -> None:
        """An operator answer from the decision inbox (inbox.py) joins the canon as a precedent. A
        dismissal is not an answer, so it stays out."""
        if decision.get("verb") != "answer" or not decision.get("answer"):
            return
        options = json.loads(decision["options"]) if isinstance(decision.get("options"), str) else []
        row = {
            "source": SOURCE_INBOX,
            "at": round(self._clock(), 3),
            "session": decision.get("session"),
            "mission": None,
            "cwd": None,
            "question": _text(decision.get("title")),
            "header": _text(decision.get("source"), 40),
            "options": options,
            "multi": False,
            "answer": _text(decision.get("answer")),
            "picked_offered": decision.get("answer") in options,
            "key": decision.get("key"),
        }
        with self._lock:
            self._append(LEDGER_NAME, [row])

    def since_start(self) -> dict[str, int]:
        with self._lock:
            return dict(self._since_start)

    def record_judge(self, row: Mapping[str, Any]) -> None:
        with self._lock:
            self._append(JUDGE_NAME, [row])

    def import_docs(self, paths: Iterable[Path]) -> int:
        """Append every doc decision not already in the ledger (by doc + text). Returns the count added."""
        seen = {(r.get("doc"), r.get("question")) for r in self.read() if r.get("source") == SOURCE_DOC}
        rows = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for row in doc_decisions(path, text):
                if (row["doc"], row["question"]) not in seen:
                    seen.add((row["doc"], row["question"]))
                    rows.append(row)
        with self._lock:
            self._append(LEDGER_NAME, rows)
        return len(rows)

    def summary(self, recent: int = 5) -> dict:
        """The AGENT RACK › DECISIONS readout: counts, the judge's agreement, the newest rows."""
        rows = self.read()
        judged = self.read(JUDGE_NAME)
        scored = [j for j in judged if isinstance(j.get("agreed"), bool)]
        agreed = sum(1 for j in scored if j["agreed"])
        hook_rows = [r for r in rows if r.get("source") == SOURCE_HOOK]
        return {
            "enabled": self.enabled,
            "decisions": len(rows),
            "from_agents": len(hook_rows),
            "from_docs": len(rows) - len(hook_rows),
            "judged": len(scored),
            "agreed": agreed,
            "agreement": round(agreed / len(scored), 4) if scored else None,
            "since_start": self.since_start(),
            "recent": [
                {"question": r.get("question"), "answer": r.get("answer"), "at": r.get("at"), "session": r.get("session")}
                for r in reversed(hook_rows[-recent:])
            ],
        }


# ── the shadow judge ────────────────────────────────────────────────────────

Caller = Callable[[str, dict | None], tuple[int, dict]]


def judge_request(question: Mapping[str, Any], precedents: list[dict]) -> dict:
    """A typed `choice` question for Laya (`schemas/src/decide.ts` `decideRequestSchema`) (pure)."""
    state = {
        "question": question["question"],
        "header": question.get("header", ""),
        "precedents": [{"asked": p.get("question"), "operator_chose": p.get("answer")} for p in precedents],
    }
    criteria = {f"o{i}": label for i, label in enumerate(question["options"])}
    return {
        "state": state,
        "questions": {
            "pick": {
                "type": "choice",
                "instructions": (
                    "An agent asked the operator this question. Using the operator's earlier decisions in "
                    "`precedents`, which option will the operator pick?"
                ),
                "criteria": criteria,
            }
        },
    }


def read_pick(payload: Mapping[str, Any], options: list[str]) -> tuple[str, float] | None:
    """`(option label, confidence)` from a `/decide` answer, or None (pure)."""
    answer = (payload.get("answers") or {}).get("pick") if isinstance(payload, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    if not isinstance(choice, str) or not choice.startswith("o") or not choice[1:].isdigit():
        return None
    index = int(choice[1:])
    if index >= len(options):
        return None
    confidence = answer.get("confidence")
    return options[index], float(confidence) if isinstance(confidence, (int, float)) else 0.0


class ShadowJudge:
    """One worker thread: predict, record, and score the prediction once the answer is in."""

    def __init__(self, ledger: DecisionLedger, laya: Callable[[], Caller | None], *, worker: bool = True) -> None:
        self._ledger = ledger
        self._laya = laya
        self._queue: queue.Queue[tuple[str, list[dict]]] = queue.Queue(maxsize=32)
        self._predictions: dict[str, dict[str, dict]] = {}
        #: Answers that landed before their prediction; dropped with the session's next question.
        self._answers: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()
        if worker:
            threading.Thread(target=self._run, daemon=True, name="decision-judge").start()

    def asked(self, session: str, questions: list[dict]) -> None:
        with self._lock:
            # A new question voids what was waiting from the last one in this session.
            self._predictions.pop(session, None)
            self._answers.pop(session, None)
        if not questions or not self._ledger.enabled or self._laya() is None:
            return
        try:
            self._queue.put_nowait((session, questions))
        except queue.Full:
            pass

    def predict_now(self, session: str, questions: list[dict]) -> None:
        call = self._laya()
        if call is None:
            return
        history = self._ledger.read()
        for q in questions:
            status, payload = call("/decide", judge_request(q, nearest(history, q["question"])))
            pick = read_pick(payload, q["options"]) if status == 200 else None
            if pick is None:
                continue
            with self._lock:
                self._predictions.setdefault(session, {})[q["question"]] = {"pick": pick[0], "confidence": pick[1]}
                ready = self._match_locked(session)
            self._score(session, ready)

    def answered(self, session: str, rows: list[dict]) -> None:
        """Score this session's predictions against the operator's answers. Either may arrive first:
        a quick answer waits here for a slow prediction, and the other way round."""
        with self._lock:
            waiting = self._answers.setdefault(session, {})
            for row in rows:
                waiting[row["question"]] = row
            ready = self._match_locked(session)
        self._score(session, ready)

    def _match_locked(self, session: str) -> list[tuple[dict, dict]]:
        predicted = self._predictions.get(session, {})
        answers = self._answers.get(session, {})
        pairs = [(predicted.pop(q), answers.pop(q)) for q in list(answers) if q in predicted]
        return pairs

    def _score(self, session: str, pairs: list[tuple[dict, dict]]) -> None:
        for guess, row in pairs:
            self._ledger.record_judge(
                {
                    "at": row["at"],
                    "session": session,
                    "question": row["question"],
                    "predicted": guess["pick"],
                    "confidence": round(guess["confidence"], 4),
                    "answer": row["answer"],
                    "agreed": guess["pick"] == row["answer"],
                }
            )

    def _run(self) -> None:
        while True:
            session, questions = self._queue.get()
            try:
                self.predict_now(session, questions)
            except Exception as err:  # noqa: BLE001 — a judge bug must not kill the worker
                print(f"tldrack: decision judge failed ({type(err).__name__})")
