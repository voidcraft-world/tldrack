"""Rack labels for generated session names, without a tmux rename.

Two stages. The first prompt writes a PLACEHOLDER at once, which is local text
with no model call (`prompt_name`), so the row is never nameless. The session
then stays `pending` until a topic label lands (`settle_topic`, asked for by
`session_topics.py`) or `MAX_TOPIC_ATTEMPTS` prompts have gone by without one.
After that the placeholder is final.

Only GENERATED names qualify (`is_generated`): `+ NEW`'s `vc-N` and a LEAD
launch's `lead-<profile>-MMDD-HHMM` (`lead/names.py`). Neither says what the
session is about. An explicit rename suppresses the label, even when the chosen
name also looks generated. Empty values record that choice.

A prompt the RACK typed (`pane_typist.RACK_TAGS`: a lead's launch line, every
wake, a lead's message to its worker) is not the operator's and names nothing: no
placeholder, no topic call, no attempt spent. A lead's first prompt is always
one, so the operator's first real prompt is what labels it.

A TITLE someone wrote names a session outright (`title`, a LEAD run's
`--title`): final at once, over a placeholder or a topic, never over a rename.

A result is applied only for the OCCUPANT that asked for it. Each placeholder
gets an in-memory epoch; `manual`, `forget` and `adopt` drop it, so a topic call
still in flight when the session is renamed, killed or respawned lands on
nothing. A file written before `pending` existed loads with every label final:
an upgrade never renames a session behind the user's back.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

from tldrack import paths
from tldrack.lead.names import LEAD_NAME_RE
from tldrack.pane_typist import RACK_TAGS
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE

MAX_NAME = 56
#: Prompts a placeholder may wait through for a topic before it becomes final.
MAX_TOPIC_ATTEMPTS = 3
#: `+ NEW`'s names. A LEAD's are `lead/names.py`'s; `is_generated` takes both.
VC_NAME = re.compile(r"vc-\d+")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NAMES_FILE = "session-names.json"


def resolve_names_path() -> Path:
    return paths.state_file(NAMES_FILE)


INTRO = re.compile(
    r"^(?:(?:please|hey|hi)[,!:]?\s+|(?:can|could|would) you\s+|"
    r"(?:help me|i want to|i'd like to|let's)\s+)",
    re.IGNORECASE,
)


def is_generated(name: object) -> bool:
    """A name the rack minted, which therefore carries no topic: the only kind a label replaces."""
    return isinstance(name, str) and bool(VC_NAME.fullmatch(name) or LEAD_NAME_RE.fullmatch(name))


def rack_typed(prompt: object) -> bool:
    """Did the rack type this prompt (`pane_typist.py` marks every line it types)?"""
    return isinstance(prompt, str) and prompt.lstrip().startswith(RACK_TAGS)


def valid_title(title: object) -> bool:
    """A stored label's shape: a short line with no control characters. Empty
    is valid on disk — it records an explicit rename (`manual`)."""
    return isinstance(title, str) and len(title) <= MAX_NAME and not any(ord(c) < 32 or ord(c) == 127 for c in title)


def prompt_name(prompt: object) -> str | None:
    if not isinstance(prompt, str):
        return None
    # A bounded first line: later pasted code and paragraphs are not the title.
    line = next((line.strip() for line in prompt[:4096].splitlines() if line.strip()), "")
    line = ANSI_RE.sub("", line)
    line = re.sub(r"^[#>*\s]+", "", line)
    while INTRO.match(line):
        line = INTRO.sub("", line, count=1)
    words = re.findall(r"[\w]+(?:[-'][\w]+)*", line, re.UNICODE)[:8]
    title = " ".join(words)
    if len(title) > MAX_NAME:
        title = title[:MAX_NAME].rsplit(" ", 1)[0] or title[:MAX_NAME]
    return title or None


def _document(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("names"), dict):
        raise ValueError("expected a names object")
    return data


def parse_names(text: str) -> dict[str, str]:
    return {
        name: title for name, title in _document(text)["names"].items() if SESSION_NAME_RE.fullmatch(name) and valid_title(title)
    }


def parse_pending(text: str, names: dict[str, str]) -> dict[str, int]:
    """Attempts made per placeholder still waiting for a topic. Absent in a
    file from before topics, which therefore loads with every label final."""
    pending = _document(text).get("pending")
    if not isinstance(pending, dict):
        return {}
    return {
        name: attempts
        for name, attempts in pending.items()
        if names.get(name) and is_generated(name) and type(attempts) is int and 0 <= attempts <= MAX_TOPIC_ATTEMPTS
    }


def _prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.strip()[:4096].encode("utf-8", "replace")).hexdigest()


class SessionNameStore(SessionStateFile[str]):
    def __init__(self, path: Path):
        self._pending: dict[str, int] = {}
        self._epochs: dict[str, int] = {}
        self._last_prompt: dict[str, str] = {}
        self._epoch_seq = itertools.count(1)
        super().__init__(
            path, parse=self._load, serialize=self._dump, noun="session name", plural="session names", file_stem="session-names"
        )

    def _load(self, text: str) -> dict[str, str]:
        names = parse_names(text)
        self._pending = parse_pending(text, names)
        return names

    def _dump(self, names: dict[str, str]) -> str:
        document: dict[str, object] = {"names": names}
        if self._pending:
            document["pending"] = self._pending
        return json.dumps(document, ensure_ascii=False) + "\n"

    def _drop_locked(self, session: str) -> None:
        self._pending.pop(session, None)
        self._epochs.pop(session, None)
        self._last_prompt.pop(session, None)

    def note_prompt(self, session: str, prompt: object) -> int | None:
        """Write the placeholder on a first prompt, and answer the epoch to ask
        a topic for, or None when no topic call is owed: a custom or manual name,
        a final label, a line the rack typed, the same prompt twice (`/spawn`'s
        prompt, then the hook that carries it), or the attempts spent."""
        if not is_generated(session) or rack_typed(prompt):
            return None
        title = prompt_name(prompt)
        if title is None:
            return None
        key = _prompt_key(prompt)
        with self._lock:
            if session not in self._entries:
                self._entries[session] = title
                self._pending[session] = 0
                self._epochs[session] = next(self._epoch_seq)
            elif session not in self._pending:
                return None
            if self._last_prompt.get(session) == key:
                return None
            if self._pending[session] >= MAX_TOPIC_ATTEMPTS:
                self._drop_locked(session)
                self._persist_locked()
                return None
            self._pending[session] += 1
            self._last_prompt[session] = key
            epoch = self._epochs.setdefault(session, next(self._epoch_seq))
            self._persist_locked()
            return epoch

    def settle_topic(self, session: str, epoch: int, label: object) -> bool:
        """Replace the placeholder with a topic label — only while the occupant
        that asked is still the one holding the name, and still pending."""
        if not isinstance(label, str) or not label or not valid_title(label):
            return False
        with self._lock:
            if session not in self._pending or self._epochs.get(session) != epoch:
                return False
            self._entries[session] = label
            self._drop_locked(session)
            self._persist_locked()
            return True

    def title(self, session: str, label: object) -> bool:
        """Name a session from a title someone wrote (`session_topics.title_label`):
        final at once, superseding a placeholder or a topic and voiding a topic call
        still in flight. An explicit rename still wins."""
        if not is_generated(session) or not isinstance(label, str) or not label or not valid_title(label):
            return False
        with self._lock:
            if self._entries.get(session) == "":
                return False
            if self._entries.get(session) != label or session in self._pending:
                self._entries[session] = label
                self._drop_locked(session)
                self._persist_locked()
            return True

    def forget(self, session: str) -> bool:
        with self._lock:
            had_pending = session in self._pending
            self._drop_locked(session)
            if self._entries.pop(session, None) is None and not had_pending:
                return False
            self._persist_locked()
            return True

    def manual(self, old: str, new: str) -> None:
        with self._lock:
            self._entries.pop(old, None)
            self._drop_locked(old)
            self._drop_locked(new)
            self._entries[new] = ""
            self._persist_locked()

    def adopt(self, session: str, title: object) -> bool:
        """`title` as the session's final label: a reopened session taking back the
        label it was closed with (`POST /reopen`, `closed_sessions.py`), or the
        operator's better name from the NAME card (`POST /topic-labels`). A topic
        call still in flight lands on nothing. False: not a generated row, or not a title."""
        if not is_generated(session) or not title or not valid_title(title):
            return False
        with self._lock:
            if self._entries.get(session) == title and session not in self._pending:
                return True
            self._entries[session] = title
            self._drop_locked(session)
            self._persist_locked()
            return True

    def final_label(self, session: str) -> str | None:
        """The landed label (a topic or an adopted one), never a placeholder still waiting on its call."""
        with self._lock:
            if session in self._pending or not is_generated(session):
                return None
            return self._entries.get(session) or None

    def get_all(self) -> dict[str, str]:
        with self._lock:
            return {name: title for name, title in self._entries.items() if title and is_generated(name)}
