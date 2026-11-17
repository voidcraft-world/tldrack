"""Deciding whether an answer may be TYPED into an agent's pane right now (pure).

── WHY TYPING NEEDS A GATE AT ALL ─────────────────────────────────────────
A canvas answer reaches the agent the way the rack's other doors deliver a
prompt: `tmux send-keys -l`, then Enter. Typing is blind, so what decides
safety is what the pane is showing at that moment:

- a PERMISSION DIALOG or an AskUserQuestion menu: Enter confirms the
  highlighted option. "yes" typed into a "Run rm -rf?" dialog is the outcome
  worth any amount of queueing to avoid. The bridge reports that pane as
  `attention` — unless the attention is the one THIS plugin raised for a
  canvas question, which it recognises by its own message.
- a DRAFT in the input box: our keys would append to the user's half-written
  prompt and submit both.
- a bare SHELL: the answer would run as a command.
- a WORKING agent: Claude Code would queue it, but "answered while it was
  still writing the question" is not an answer to that question.

Every one of those QUEUES the answer rather than dropping it; the plugin's
watcher retries until the pane is safe (`watcher.py`).
"""

from __future__ import annotations

import unicodedata
from html.parser import HTMLParser

#: The `/status` message this plugin raises `attention` with. Its prefix is
#: how a later delivery tells "our question" from a real TUI prompt.
QUESTION_MESSAGE_PREFIX = "canvas: "

#: One typed line, bounded — `send-keys -l` sends a newline as ENTER.
REPLY_MAX = 2000
QUESTION_MAX = 120

_RULE_MIN = 10


# jscpd:ignore-start
# A declared cross-package port: owned by plugins/README.md — see the note there.
def looks_ready(pane_text: object) -> bool:
    """Has Claude drawn its input box? Copied from
    plugins/worktree/worktree/create.py `looks_ready` — see the note
    there (plugins cannot import each other; plugins/README.md)."""
    if not isinstance(pane_text, str) or not pane_text.strip():
        return False
    if any(char in pane_text for char in "│╭╰┌└") and ">" in pane_text:
        return True
    lines = pane_text.splitlines()
    for above, line in zip(lines, lines[1:]):
        rule = above.strip()
        if len(rule) >= _RULE_MIN and set(rule) == {"─"} and line.lstrip().startswith(("❯", ">")):
            return True
    return False


# jscpd:ignore-end


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= _RULE_MIN and set(stripped) == {"─"}


def input_box_draft(pane_text: object) -> str:
    """The text sitting in Claude's input box, `""` when it is empty.

    The box is the LAST caret line directly under a rule, running down to the
    next rule — a multi-line draft continues on the lines below the caret.
    Whitespace includes the NBSP Claude pads an empty caret line with.
    """
    if not isinstance(pane_text, str):
        return ""
    lines = pane_text.splitlines()
    start = None
    for index in range(1, len(lines)):
        if _is_rule(lines[index - 1]) and lines[index].lstrip().startswith(("❯", ">")):
            start = index
    if start is None:
        return ""
    first = lines[start].lstrip()[1:]
    body = [first]
    for line in lines[start + 1 :]:
        if _is_rule(line):
            break
        body.append(line)
    return " ".join(" ".join(body).replace(" ", " ").split())


def decide_delivery(state: object, message: object, pane_text: object) -> tuple[str, str | None]:
    """`("send", None)` or `("queue", why)`. Pure, so every refusal is tested.

    `state` / `message` are the bridge's row for the session; `pane_text` is
    `tmux capture-pane -p` of it right now.
    """
    if state == "busy":
        return "queue", "agent is working"
    if state == "attention" and not (isinstance(message, str) and message.startswith(QUESTION_MESSAGE_PREFIX)):
        return "queue", "agent is asking something in the terminal"
    if not looks_ready(pane_text):
        return "queue", "no Claude prompt in the pane"
    if input_box_draft(pane_text):
        return "queue", "a draft is in the input box"
    return "send", None


def one_line(text: object, limit: int) -> str:
    """One typed line with no control characters. The text is an agent's (a question, an option) or the
    page's, and `send-keys -l` types it as KEYS: an ESC in it would reach the agent's input box as a
    keypress (`ESC [ Z` is Shift+Tab), not as text."""
    if not isinstance(text, str):
        return ""
    spaced = " ".join(text.split())
    return "".join(ch for ch in spaced if unicodedata.category(ch) != "Cc")[:limit].strip()


def format_reply(question: object, answer: object) -> str:
    """What gets typed: `canvas › <question> → <answer>`, one line.

    The question rides along so the answer reads on its own in the transcript
    — "yes" alone, three turns later, answers nothing.
    """
    reply = one_line(answer, REPLY_MAX)
    if not reply:
        return ""
    asked = one_line(question, QUESTION_MAX)
    line = f"canvas › {asked} → {reply}" if asked else f"canvas → {reply}"
    return line[:REPLY_MAX]


class _ChoiceFinder(HTMLParser):
    """Finds the first `.choice` holding a `data-reply` button, and its question:
    the `data-q` attribute, else the choice's first run of text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0  # >0 while inside a .choice
        self.question: str | None = None
        self.text: list[str] = []
        self.has_reply = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        values = dict(attrs)
        if self.depth == 0:
            classes = (values.get("class") or "").split()
            if "choice" in classes:
                self.depth = 1
                self.question = values.get("data-q") or None
                self.text, self.has_reply = [], False
            return
        if tag not in _VOID_TAGS:
            self.depth += 1
        if "data-reply" in values:
            self.has_reply = True

    def handle_endtag(self, tag):
        if self.done or self.depth == 0:
            return
        self.depth -= 1
        if self.depth == 0:
            if self.has_reply:
                self.done = True
            else:
                self.question = None

    def handle_data(self, data):
        if self.depth > 0 and not self.done and not self.has_reply:
            self.text.append(data)


_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


def pending_question(fragment: object) -> str | None:
    """The question a canvas is asking, or None when it asks nothing.

    A canvas "asks" when it holds a `.choice` with at least one `data-reply`
    button. The question is that choice's `data-q`, else its leading text.
    """
    if not isinstance(fragment, str) or "data-reply" not in fragment:
        return None
    finder = _ChoiceFinder()
    try:
        finder.feed(fragment)
        finder.close()
    except Exception:  # noqa: BLE001 — html.parser is lenient; never let a fragment 500
        return None
    if not (finder.done or (finder.depth > 0 and finder.has_reply)):
        return None
    asked = one_line(finder.question or " ".join(finder.text), QUESTION_MAX)
    return asked or "question"
