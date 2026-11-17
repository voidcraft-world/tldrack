"""Work personas: how hard an agent should verify, picked per session in the rack.

The operator works two ways. Focused, one agent on one feature, where the usual
care (run the affected tests, check nothing else broke) is right. And SPRAWL:
many agents on many features at once, where that same care is waste — every
agent re-runs suites and long simulations for changes that never touched them,
while one end-to-end pass is coming later anyway. A persona says which way this
session is working, so the agent stops guessing.

── DELIVERED BY THE HOOK, NEVER TYPED INTO THE DRAFT ───────────────────────────
The first idea was to rewrite the prompt on Enter. That needs the page to read
and edit Claude Code's TUI input box, which is not text the page owns. Instead
the bridge already receives every `UserPromptSubmit` (the http relay) and Claude
Code reads that response as hook output, so the answer carries the persona as
`additionalContext`. Every door gets it — the rack pane, the dock's iTerm, the
phone — and the draft is never touched.

── RESTATED ON EVERY PROMPT ───────────────────────────────────────────────────
Not once per change: `/compact` drops old context, and a mode the agent forgot
is worse than ~150 tokens a turn. Clearing a persona sends ONE "cleared" note on
the next prompt, or the agent keeps working the old way on its memory of it.
Which persona was last delivered is in memory only: a restart can at worst cost
one "cleared" note, while a stored claim about a conversation is a lie the
moment it restarts.

── CLAUDE ONLY ────────────────────────────────────────────────────────────────
Codex keeps the curl relay, which sends the response to /dev/null. A persona on
a Codex session is stored and shown, and reaches nothing.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from tldrack import paths
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE

PERSONA_ID_RE = re.compile(r"[a-z]{1,16}")

PERSONA_FILE_VERSION = 1

PERSONAS_FILE = "session-personas.json"


@dataclass(frozen=True)
class Persona:
    id: str
    glyph: str
    label: str
    title: str
    text: str

    def card(self) -> dict:
        """What a client draws: never the instruction text, which is the bridge's."""
        return {"id": self.id, "glyph": self.glyph, "label": self.label, "title": self.title}


# Fast to careful, which is the order the chips draw in.
PERSONAS: tuple[Persona, ...] = (
    Persona(
        id="ship",
        glyph="⚡",
        label="SHIP",
        title="Midnight mode: build it and get it out. No test runs, no long runs, no refactors",
        text=(
            "It is midnight and the job is to get this feature out. Build it end to end and stop. "
            "Do not run tests, test suites, affected-test selections, simulations, benchmarks, builds, "
            "typecheck or lint: the operator tests the whole batch later. "
            "Write a new test only when it is the fastest way to get a tricky pure function right. "
            "No refactors, cleanups or fixes the feature does not need."
        ),
    ),
    Persona(
        id="sprawl",
        glyph="⧉",
        label="SPRAWL",
        title="Many agents at once: build it, write tests for new logic, run only those new tests. A full pass comes later",
        text=(
            "Many agents are building features in this repo at once, and a full end-to-end test pass "
            "comes later. Build the feature properly and write unit tests for the new logic. Run ONLY "
            "the new test files you wrote, and only when each one finishes in about 30 s. Do not run "
            "existing tests, test suites, affected-test selections, simulations, benchmarks, builds or "
            "typecheck. A failure in a file you did not touch is another agent mid-edit: leave it."
        ),
    ),
    Persona(
        id="steady",
        glyph="◉",
        label="STEADY",
        title="Slow and steady: do it properly and prove nothing else broke",
        text=(
            "Do this properly and prove it breaks nothing else. Read the callers of what you change. "
            "Run the tests that import your changes, the way this repo's own guide says to select them, "
            "and fix what they flag at the root. Run a simulation or a benchmark only when you changed "
            "code whose behaviour it measures."
        ),
    ),
)

PERSONAS_BY_ID = {persona.id: persona for persona in PERSONAS}

CLEARED_TEXT = (
    "Work persona cleared in the agent rack: the SHIP / SPRAWL / STEADY instructions from earlier "
    "turns no longer apply. Follow the repo's normal rules for testing and verification."
)


def persona_context(persona: Persona) -> str:
    """The `additionalContext` for one prompt under `persona`."""
    names = " / ".join(p.label for p in PERSONAS)
    return (
        f"Work persona: {persona.label} (one of {names}, picked by the operator in the agent rack; it holds "
        "for this prompt and every later one until they change it, and it overrides the repo's testing "
        "and verification rules and any skill step that runs tests or simulations). "
        f"{persona.text} Say in your hand-off what you did not verify. Pass this persona on to any "
        "subagent you brief."
    )


def prompt_hook_output(context: str | None, event: str = "UserPromptSubmit") -> dict:
    """The hook-output fields for one hook's answer; empty for no context."""
    if context is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def resolve_personas_path() -> Path:
    env_path = os.environ.get("TLDRACK_PERSONAS_PATH")
    return Path(env_path) if env_path else paths.state_file(PERSONAS_FILE)


def parse_persona_file(text: str) -> dict[str, str]:
    """Raises ValueError on a bad envelope; drops bad entries and retired ids."""
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("personas"), dict):
        raise ValueError('expected {"version": ..., "personas": {...}}')
    return {
        session: persona
        for session, persona in payload["personas"].items()
        if isinstance(session, str) and SESSION_NAME_RE.fullmatch(session) and persona in PERSONAS_BY_ID
    }


def serialize_persona_file(personas: dict[str, str]) -> str:
    return json.dumps({"version": PERSONA_FILE_VERSION, "personas": dict(sorted(personas.items()))}, indent=2) + "\n"


class SessionPersonaStore(SessionStateFile[str]):
    """File-persisted per-session persona picks, plus which one each session's
    agent last heard (in memory — see the module docstring)."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_persona_file,
            serialize=serialize_persona_file,
            noun="persona pick",
            plural="session personas",
            file_stem="session-personas",
        )
        self._delivered: dict[str, str] = {}
        self._delivered_lock = threading.Lock()

    def get(self, session: str) -> str | None:
        with self._lock:
            return self._entries.get(session)

    def get_all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._entries)

    def set(self, session: str, persona: str | None) -> None:
        """Pick (a known id) or clear (None). Raises ValueError on an unknown id."""
        if persona is not None and persona not in PERSONAS_BY_ID:
            raise ValueError(f"unknown persona {persona!r}")
        with self._lock:
            if persona is None:
                self._entries.pop(session, None)
            else:
                self._entries[session] = persona
            self._persist_locked()

    def context_for_prompt(self, session: str) -> str | None:
        """What this prompt carries: the persona, the one-time cleared note, or nothing."""
        persona = self.get(session)
        with self._delivered_lock:
            if persona is not None:
                self._delivered[session] = persona
                return persona_context(PERSONAS_BY_ID[persona])
            if self._delivered.pop(session, None) is not None:
                return CLEARED_TEXT
            return None

    def rename(self, old: str, new: str) -> bool:
        with self._delivered_lock:
            heard = self._delivered.pop(old, None)
            if heard is not None:
                self._delivered[new] = heard
        return super().rename(old, new)

    def forget(self, session: str) -> bool:
        """For a killed session, or a name a NEW agent is about to take: that agent
        never heard the old persona, so no cleared note is owed either."""
        with self._delivered_lock:
            self._delivered.pop(session, None)
        return super().forget(session)
