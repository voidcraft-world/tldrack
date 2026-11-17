"""The plugin's state: which canvas questions are answered, queued, or raised.

All of it is IN MEMORY. A restart loses queued answers (the page says QUEUED
and the user can still type in the terminal) and the "already raised" set —
which is why raising only considers canvases written after this process
started (`RAISE_WINDOW_SECONDS`): otherwise every unanswered canvas on disk
would turn its agent 🙋 on each bridge restart.

A canvas may ask SEVERAL questions (a typed doc, `doc.py`), so an answer is a
`{question id: value}` map, sent as ONE typed line. One line, not one per
question: the first would start the agent's turn and every later one would
queue behind it, answering a canvas the agent may already have rewritten.
A markup canvas asks at most one, under the id `q1`.

Collaborators are injected (`tmux`, `host`, a clock), so every rule here is
tested without a tmux server or a bridge.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

from agent_canvas import tmux as tmux_mod
from agent_canvas.delivery import (
    QUESTION_MESSAGE_PREFIX,
    decide_delivery,
    format_reply,
    one_line,
    pending_question,
)
from agent_canvas.doc import (
    Issue,
    Question,
    check_answers,
    format_answers,
    load_doc,
    summary,
)
from agent_canvas.draw import draw_request
from agent_canvas.paths import CANVAS_KEY_RE, CANVAS_SUFFIXES, canvas_dir, read_canvas

#: An answer the pane never became safe for is dropped after this.
QUEUE_TTL_SECONDS = 10 * 60
#: Only a canvas written this recently (and since startup) can raise attention.
RAISE_WINDOW_SECONDS = 30 * 60
#: Turn-over states — the agent has finished writing and is waiting.
WAITING_STATES = frozenset({"free", "background"})
#: The id a markup canvas's one `.choice` answers under.
HTML_QUESTION_ID = "q1"


#: The pane's faces, default first. `skills` is the audit of every skill an agent can
#: call, drawn by `assets/skills.js` from the claude-skills plugin's routes.
VIEWS = ("canvas", "skills")
DEFAULT_VIEW = VIEWS[0]


@dataclass
class Canvas:
    """One revision of one canvas, parsed once (the page polls every second)."""

    kind: str  # "html" · "doc"
    rev: str
    text: str
    truncated: bool
    doc: dict | None = None
    issues: list[Issue] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    @property
    def question(self) -> str | None:
        return summary(self.questions)


@dataclass
class Queued:
    session: str
    rev: str
    line: str
    answers: dict
    reason: str
    at: float


def parse_canvas(suffix: str, text: str, rev: str, truncated: bool) -> Canvas:
    if suffix == ".json":
        loaded = load_doc(text)
        return Canvas("doc", rev, text, truncated, loaded.doc, loaded.issues, loaded.questions)
    asked = pending_question(text)
    questions = [Question(HTML_QUESTION_ID, "html", asked)] if asked else []
    return Canvas("html", rev, text, truncated, questions=questions)


class CanvasDesk:
    def __init__(self, host, *, tmux=tmux_mod, clock: Callable[[], float] = time.time) -> None:
        self._host = host
        self._tmux = tmux
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self._answered: dict[str, tuple[str, dict]] = {}  # key → (rev, answers)
        self._queued: dict[str, Queued] = {}  # key → latest answer waiting
        self._raised: dict[str, str] = {}  # key → rev raised for
        self._parsed: dict[str, Canvas] = {}  # key → last parse
        self._views: dict[str, str] = {}  # key → a view other than "canvas"

    def canvas(self, key: str) -> Canvas | None:
        found = read_canvas(key)
        if found is None:
            return None
        suffix, text, rev, truncated = found
        with self._lock:
            cached = self._parsed.get(key)
        if cached and cached.rev == rev and (cached.kind == "doc") == (suffix == ".json"):
            return cached
        parsed = parse_canvas(suffix, text, rev, truncated)
        with self._lock:
            self._parsed[key] = parsed
        return parsed

    # -- views ------------------------------------------------------------
    def set_view(self, session: str, wanted: object) -> tuple[int, dict]:
        """`POST /view`: which face the pane shows — the agent's canvas, or the skills audit.

        It lives HERE, not in the page, because the button that flips it (◇ SKILLS) is on
        the terminal page, a cross-origin sibling of this one: the bridge is the only
        thing both can reach. Keyed like everything else, so a rename keeps the view.
        In memory: a restart shows the canvas again, which is the right default.
        """
        if wanted not in VIEWS and wanted != "toggle":
            return 400, {"error": f"view must be one of {', '.join(VIEWS)} or toggle"}
        key = self._tmux.key_for_session(session)
        if key is None:
            return 404, {"error": f"no tmux session named {session}"}
        with self._lock:
            current = self._views.get(key, DEFAULT_VIEW)
            view = (VIEWS[1] if current == DEFAULT_VIEW else DEFAULT_VIEW) if wanted == "toggle" else wanted
            if view == DEFAULT_VIEW:
                self._views.pop(key, None)
            else:
                self._views[key] = view
        return 200, {"session": session, "view": view}

    def view_of(self, key: str) -> str:
        with self._lock:
            return self._views.get(key, DEFAULT_VIEW)

    # -- reads ------------------------------------------------------------
    def fragment_view(self, session: str) -> dict:
        """What the page polls: the canvas plus this desk's view of it."""
        key = self._tmux.key_for_session(session)
        if key is None:
            return {"rev": None, "gone": True, "view": DEFAULT_VIEW}
        canvas = self.canvas(key)
        if canvas is None:
            return {"rev": None, "key": key, "view": self.view_of(key)}
        with self._lock:
            answered = self._answered.get(key)
            queued = self._queued.get(key)
        view = {
            "key": key,
            "view": self.view_of(key),
            "rev": canvas.rev,
            "kind": canvas.kind,
            "truncated": canvas.truncated,
            "question": canvas.question,
            "answered": {"answers": answered[1]} if answered and answered[0] == canvas.rev else None,
            "queued": ({"answers": queued.answers, "reason": queued.reason} if queued and queued.rev == canvas.rev else None),
        }
        if canvas.kind == "doc":
            view.update(
                doc=canvas.doc,
                errors=[issue.as_json() for issue in canvas.issues],
                questions=[question.as_json() for question in canvas.questions],
            )
        else:
            view["html"] = canvas.text
        return view

    def key_for(self, session: str) -> str | None:
        return self._tmux.key_for_session(session)

    def draw_view(self, session: str) -> tuple[int, dict]:
        """The ◧ DRAW button's paste for one session (`draw.py` owns why)."""
        key = self.key_for(session)
        if key is None:
            return 404, {"error": f"no tmux session named {session}"}
        return 200, {"key": key, "paste": draw_request(key)}

    def check_view(self, key: str) -> dict:
        """What the post-write hook reads: is the canvas the agent just wrote valid?"""
        canvas = self.canvas(key)
        if canvas is None:
            return {
                "ok": False,
                "kind": None,
                "drawn": False,
                "errors": [{"path": "(root)", "code": "missing", "message": "no canvas file"}],
            }
        return {
            "ok": not canvas.issues,
            "kind": canvas.kind,
            "drawn": canvas.kind == "html" or canvas.doc is not None,
            "errors": [issue.as_json() for issue in canvas.issues],
            "questions": [question.id for question in canvas.questions],
        }

    def queued_sessions(self) -> list[str]:
        with self._lock:
            return sorted({entry.session for entry in self._queued.values()})

    # -- the reply --------------------------------------------------------
    def reply(self, session: str, answer: object, rev: object, question: object) -> tuple[int, dict]:
        """A single answer — a markup canvas's button, or a client that sends one value.

        On a doc canvas it answers the FIRST question, so an old client still works.
        """
        key, canvas, refused = self._current(session, rev)
        if refused:
            return refused
        if canvas.kind == "doc":
            if not canvas.questions:
                return 409, {"error": "this canvas asks nothing"}
            return self._reply_doc(session, key, canvas, {canvas.questions[0].id: answer})
        asked = question if isinstance(question, str) and question.strip() else canvas.question
        line = format_reply(asked, answer)
        if not line:
            return 400, {"error": "empty answer"}
        return self._send(
            key,
            Queued(
                session=session,
                rev=canvas.rev,
                line=line,
                answers={HTML_QUESTION_ID: one_line(answer, 2000)},
                reason="",
                at=self._clock(),
            ),
        )

    def reply_answers(self, session: str, answers: object, rev: object) -> tuple[int, dict]:
        """Every answer on a canvas, sent as one typed line."""
        key, canvas, refused = self._current(session, rev)
        if refused:
            return refused
        if canvas.kind == "html":
            value = answers.get(HTML_QUESTION_ID) if isinstance(answers, dict) else None
            return self.reply(session, value, canvas.rev, None)
        return self._reply_doc(session, key, canvas, answers)

    def _current(self, session: str, rev: object) -> tuple[str, Canvas, None] | tuple[None, None, tuple]:
        """`(key, canvas, None)`, or `(None, None, refusal)` when there is nothing
        current to answer."""
        key = self._tmux.key_for_session(session)
        if key is None:
            return None, None, (410, {"error": "session gone"})
        canvas = self.canvas(key)
        if canvas is None:
            return None, None, (409, {"error": "no canvas to answer"})
        if isinstance(rev, str) and rev != canvas.rev:
            return None, None, (409, {"error": "the canvas changed — answer the new one"})
        return key, canvas, None

    def _reply_doc(self, session: str, key: str, canvas: Canvas, answers: object) -> tuple[int, dict]:
        why = check_answers(canvas.questions, answers)
        if why:
            return 400, {"error": why}
        line = format_answers(canvas.questions, answers)  # type: ignore[arg-type]
        if line is None:
            return 400, {"error": "the answers are too long for one prompt — shorten the free text"}
        return self._send(
            key,
            Queued(
                session=session,
                rev=canvas.rev,
                line=line,
                answers=dict(answers),  # type: ignore[arg-type]
                reason="",
                at=self._clock(),
            ),
        )

    def _send(self, key: str, entry: Queued) -> tuple[int, dict]:
        pane = self._tmux.capture(entry.session)
        if pane is None:
            with self._lock:
                self._queued.pop(key, None)
            return 410, {"error": "session gone"}
        row = self._host.session_rows().get(entry.session, {})
        action, why = decide_delivery(row.get("state"), row.get("message"), pane)
        if action == "send":
            if not self._tmux.type_line(entry.session, entry.line):
                entry.reason = "tmux refused the keys"
                return self._queue(key, entry)
            with self._lock:
                self._answered[key] = (entry.rev, entry.answers)
                self._queued.pop(key, None)
            return 200, {"delivered": True, "queued": False}
        entry.reason = why or "waiting"
        return self._queue(key, entry)

    def _queue(self, key: str, entry: Queued) -> tuple[int, dict]:
        with self._lock:
            # Latest wins: a second click is the user changing their answer.
            self._queued[key] = entry
        return 202, {"delivered": False, "queued": True, "reason": entry.reason}

    # -- the watcher's tick -----------------------------------------------
    def tick(self) -> None:
        self._flush_queue()
        self._raise_questions()

    def _flush_queue(self) -> None:
        now = self._clock()
        with self._lock:
            pending = list(self._queued.items())
        for key, entry in pending:
            if now - entry.at > QUEUE_TTL_SECONDS:
                with self._lock:
                    if self._queued.get(key) is entry:
                        del self._queued[key]
                continue
            self._send(key, entry)

    def _raise_questions(self) -> None:
        """Turn a waiting agent with an unanswered canvas question `attention`.

        Raised once per canvas revision, and only after the turn is over — a
        question raised mid-turn would be overwritten by the agent's own `Stop`
        hook a moment later.
        """
        now = self._clock()
        candidates: dict[str, tuple[str, str]] = {}
        try:
            keys = {path.stem for suffix in CANVAS_SUFFIXES for path in canvas_dir().glob(f"*{suffix}")}
        except OSError:
            return
        for key in keys:
            if not CANVAS_KEY_RE.fullmatch(key):
                continue
            canvas = self.canvas(key)
            if canvas is None:
                continue
            written = int(canvas.rev) / 1e9
            if written < self._started_at or now - written > RAISE_WINDOW_SECONDS:
                continue
            with self._lock:
                if self._raised.get(key) == canvas.rev or (self._answered.get(key) or ("",))[0] == canvas.rev:
                    continue
            if canvas.question:
                candidates[key] = (canvas.rev, canvas.question)
        if not candidates:
            return
        names = self._tmux.sessions_by_key()
        rows = self._host.session_rows()
        for key, (rev, asked) in candidates.items():
            session = names.get(key)
            if session is None:
                continue
            if (rows.get(session) or {}).get("state") not in WAITING_STATES:
                continue
            if self._host.raise_attention(session, f"{QUESTION_MESSAGE_PREFIX}{asked}"):
                with self._lock:
                    self._raised[key] = rev
