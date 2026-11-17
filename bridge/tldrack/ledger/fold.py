"""The shape every conversation fold produces — Claude's and Codex's — and the turn bookkeeping
both share. Pure: the folds hand `Fold` to `ingest.py`, which writes it; nothing here does I/O.

── TURNS, AND WHAT A HUMAN EVENT CARRIES ───────────────────────────────────────
A turn opens at a prompt (a human's, a task notification's, a peer agent's, an SDK caller's) and
closes just before the next one. Its `closing_text` is the agent's last words in it. A HUMAN event
that answers the agent — a prompt, an interrupt, a permission pick, an AskUserQuestion answer, a
plan sent back — carries the two things a judge needs to learn from it:

- `context`: the agent text the human was looking at (the open turn's newest agent message, or the
  previous turn's closing text when the agent had stopped);
- `latency_s`: how long the agent had been waiting on the human — from its last activity to the
  human's act. Small for a reflex, large for "it sat there while I was away", which is exactly the
  ping a better judge would have spared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: A message or event text longer than this is cut (and says so). Prompts with a pasted log can be
#: megabytes; the raw archive keeps every byte, the index keeps what is useful to read and search.
TEXT_MAX = 20_000
CONTEXT_MAX = 2_000
CLOSING_MAX = 4_000
TRUNCATED = "\n…[truncated]"

HUMAN_ORIGINS = frozenset({"human", "command"})


def clip(text: object, limit: int = TEXT_MAX) -> str:
    if not isinstance(text, str):
        return ""
    return text if len(text) <= limit else text[: limit - len(TRUNCATED)] + TRUNCATED


def tail(text: object, limit: int = CONTEXT_MAX) -> str:
    """The END of a text — what the reader saw last is what an answer answers."""
    if not isinstance(text, str):
        return ""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass
class Turn:
    idx: int
    origin: str
    prompt_ts: float | None
    end_ts: float | None = None
    tool_calls: int = 0
    interrupted: bool = False
    closing_text: str = ""


@dataclass
class Fold:
    session: dict[str, Any] = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: The newest agent activity (any assistant entry or tool result) — what latency counts from.
    last_agent_ts: float | None = None
    #: The newest agent TEXT in the open turn, else the previous turn's closing text.
    last_agent_text: str = ""

    # ── turn bookkeeping ───────────────────────────────────────────────────

    @property
    def turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def open_turn(self, origin: str, ts: float | None) -> Turn:
        turn = Turn(len(self.turns), origin, ts)
        self.turns.append(turn)
        return turn

    def prompt_turn(self, origin: str, ts: float | None, role: str, text: str, event: dict[str, Any] | None = None) -> Turn:
        """Open the turn a prompt starts and file the prompt as its first message. `event` is the human decision
        the prompt was, recorded just before this so its context is the text it answered; it moves onto the new turn."""
        self.last_agent_text = ""
        turn = self.open_turn(origin, ts)
        if event is not None:
            event["turn_idx"] = turn.idx
        self.add_message(ts, role, text)
        return turn

    def agent_activity(self, ts: float | None) -> None:
        if ts is not None:
            self.last_agent_ts = ts if self.last_agent_ts is None else max(self.last_agent_ts, ts)
            if self.turn is not None:
                self.turn.end_ts = ts if self.turn.end_ts is None else max(self.turn.end_ts, ts)

    def agent_text(self, ts: float | None, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.add_message(ts, "agent", text)
        self.last_agent_text = text
        if self.turn is not None:
            self.turn.closing_text = clip(text, CLOSING_MAX)
        self.agent_activity(ts)

    def add_message(self, ts: float | None, role: str, text: str) -> None:
        self.messages.append(
            {
                "seq": len(self.messages),
                "ts": ts,
                "role": role,
                "turn_idx": self.turn.idx if self.turn else None,
                "text": clip(text),
            }
        )

    def latency(self, ts: float | None) -> float | None:
        if ts is None or self.last_agent_ts is None:
            return None
        return round(max(0.0, ts - self.last_agent_ts), 3)

    def human_event(self, kind: str, ts: float | None, **fields: Any) -> dict[str, Any]:
        """One human decision, stamped with what it answered and how long the agent waited."""
        event = {
            "ts": ts,
            "actor": "human",
            "kind": kind,
            "decision": None,
            "turn_idx": self.turn.idx if self.turn else None,
            "tool": None,
            "summary": None,
            "text": None,
            "scope": None,
            "latency_s": self.latency(ts),
            "context": tail(self.last_agent_text) or None,
            "data": None,
        }
        event.update(fields)
        self.events.append(event)
        return event

    def machine_event(self, kind: str, ts: float | None, **fields: Any) -> dict[str, Any]:
        event = {
            "ts": ts,
            "actor": "machine",
            "kind": kind,
            "decision": None,
            "turn_idx": self.turn.idx if self.turn else None,
            "tool": None,
            "summary": None,
            "text": None,
            "scope": None,
            "latency_s": None,
            "context": None,
            "data": None,
        }
        event.update(fields)
        self.events.append(event)
        return event

    def finish(self) -> None:
        """Roll the per-turn counts into the session row."""
        s = self.session
        s["turns"] = len(self.turns)
        s["human_turns"] = sum(1 for t in self.turns if t.origin in HUMAN_ORIGINS)
        s["human_events"] = sum(1 for e in self.events if e["actor"] == "human")
        s["tool_calls"] = len(self.tool_calls)
        stamps = [m["ts"] for m in self.messages if m["ts"] is not None]
        stamps += [e["ts"] for e in self.events if e["ts"] is not None]
        stamps += [c["ts"] for c in self.tool_calls if c["ts"] is not None]
        if self.last_agent_ts is not None:
            stamps.append(self.last_agent_ts)
        if stamps:
            s.setdefault("started_at", min(stamps))
            if s.get("started_at") is None:
                s["started_at"] = min(stamps)
            s["ended_at"] = max(stamps)
        if not s.get("first_prompt"):
            first = next((m["text"] for m in self.messages if m["role"] == "human"), None)
            s["first_prompt"] = clip(first, 500) if first else None
