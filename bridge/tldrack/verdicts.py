"""Laya's turn-end verdict per session, kept across a bridge restart.

THE BUG THIS EXISTS FOR (2026-11-04). `StatusStore` is in-memory, so a restart
loses every row's ⚠️ / ✅. For `state` that is fine: hooks re-report on the next
event. A verdict is different, for the same reason `models.py` exists: it is
written once, at a `Stop`, and an IDLE session sends no further hooks. The index
rebuilds the row as a plain `free` and the mark is gone until the operator types
into that session. On 2026-11-04 a session's ✅ landed one second before a
`POST /shutdown`, on a day of several restarts. Every one of them blanked the
whole rack.

WHY A RESTORE CANNOT LIE. A verdict is a pure function of the text Laya judged
(plus the checkpoint and cuts it records). So a kept verdict comes back only
when all three of these hold (`restorable`, called from main.py's arbiter path):
  1. Claude's index rebuilds the row TURN-OVER (`free` / `background`);
  2. the index names the SAME conversation the verdict was judged in;
  3. that conversation's closing state still hashes to the one judged.
A new turn, a `/clear` or a respawn fails 2 or 3, and nothing comes back.
Text identical to what was judged earns the same verdict anyway. What the hash
cannot see is the operator's own act, so main.py drops the copy wherever the row
loses its verdict: dismiss, kill, spawn, any busy, SessionStart/End
(`verdict_left_row`).

WHAT IS KEPT. The verdict's own fields, a SHA-256 of the state, the transcript
path and the conversation id. The answer-to-read stamps `asked` and `read` ride
along (open_work.py § An answer to read): a read answer must not come back unread.
So does the operator's label, `operator` (open_work.py § The operator's label): a
correction must not come back as the mark it corrected. Never the text: the closing message stays in the
transcript and in the ledger's allow-listed copy.

A restart that loaded a NEW checkpoint restores the old checkpoint's verdict
until the next turn end. That is exactly what the rack showed before the
restart, and the verdict names its checkpoint.

NOT COVERED: Codex rows. Claude's index never describes them, so after a restart
they read `unknown` until their next hook, verdict included.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from tldrack import paths
from tldrack.open_work import DECISION_ID, MAX_MODEL_ID_CHARS, clean_operator_label, state_sha
from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.session_state_file import SessionStateFile, parse_session_map, serialize_session_map

VERDICT_FILE_VERSION = 1

VERDICTS_FILE = "session-verdicts.json"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_TRANSCRIPT_PATH_CHARS = 4096


def resolve_verdicts_path() -> Path:
    env_path = os.environ.get("TLDRACK_VERDICTS_PATH")
    return Path(env_path) if env_path else paths.state_file(VERDICTS_FILE)


def _unit(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= value <= 1


def _optional(value: object, ok: bool) -> bool:
    return value is None or ok


def clean_verdict(verdict: object) -> dict | None:
    """The verdict as `open_work._verdict` builds it plus its `state_sha`, or None when any field
    is the wrong shape. A file hand-edited into a bad verdict must never paint a row."""
    if not isinstance(verdict, dict):
        return None
    p, at, done_at = verdict.get("p"), verdict.get("at"), verdict.get("done_at")
    decision_id, model_id = verdict.get("decision_id"), verdict.get("model_id")
    checkpoint, sha = verdict.get("checkpoint"), verdict.get("state_sha")
    model, version = verdict.get("model"), verdict.get("v")
    asked, read = verdict.get("asked", False), verdict.get("read", False)
    label = verdict.get("operator")
    operator = clean_operator_label(label) if label is not None else None
    if not (
        (label is None or operator is not None)
        and isinstance(asked, bool)
        and isinstance(read, bool)
        and _unit(p)
        and _unit(at)
        and _optional(done_at, _unit(done_at))
        and isinstance(sha, str)
        and SHA256_RE.fullmatch(sha)
        and isinstance(model, str)
        and 0 < len(model) <= 64
        and isinstance(version, int)
        and not isinstance(version, bool)
        and _optional(decision_id, isinstance(decision_id, str) and DECISION_ID.fullmatch(decision_id) is not None)
        and _optional(model_id, isinstance(model_id, str) and 0 < len(model_id) <= MAX_MODEL_ID_CHARS)
        and _optional(
            checkpoint,
            isinstance(checkpoint, str)
            and 0 < len(checkpoint) <= 120
            and "/" not in checkpoint
            and "\\" not in checkpoint
            and checkpoint not in (".", ".."),
        )
    ):
        return None
    cleaned = {
        "p": float(p),
        "v": version,
        "model": model,
        "at": float(at),
        "done_at": float(done_at) if done_at is not None else None,
        "decision_id": decision_id,
        "model_id": model_id,
        "checkpoint": checkpoint,
        "state_sha": sha,
        "asked": asked,
        "read": read,
    }
    if operator is not None:
        cleaned["operator"] = operator
    return cleaned


def clean_entry(entry: object) -> dict | None:
    """`{verdict, transcript_path, agent_session_id}`, or None. A kept verdict with no conversation
    id can never pass rule 2, so it is refused here rather than kept forever."""
    if not isinstance(entry, dict):
        return None
    verdict = clean_verdict(entry.get("verdict"))
    path, conversation = entry.get("transcript_path"), entry.get("agent_session_id")
    if (
        verdict is None
        or not isinstance(path, str)
        or not 0 < len(path) <= MAX_TRANSCRIPT_PATH_CHARS
        or not isinstance(conversation, str)
        or not AGENT_SESSION_ID_RE.fullmatch(conversation)
    ):
        return None
    return {"verdict": verdict, "transcript_path": path, "agent_session_id": conversation}


def parse_verdict_file(text: str) -> dict[str, dict]:
    """session -> entry. ValueError on a wrong envelope; a malformed ENTRY is dropped alone."""
    return parse_session_map(text, clean_entry)


def serialize_verdict_file(sessions: dict[str, dict]) -> str:
    return serialize_session_map(sessions, VERDICT_FILE_VERSION)


def restorable(kept: dict | None, conversation: str | None, state: str | None) -> dict | None:
    """The kept verdict when it provably describes the turn on screen now (rules 2 and 3 in the
    module docstring; rule 1 is the caller's), else None."""
    if kept is None or conversation != kept["agent_session_id"] or not state:
        return None
    verdict = kept["verdict"]
    return dict(verdict) if state_sha(state) == verdict["state_sha"] else None


class SessionVerdictStore(SessionStateFile[dict]):
    """File-persisted last verdict per session. The file, the lock, the tolerant load, `rename`
    and `forget` are owned by `session_state_file.py` — see the note there."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_verdict_file,
            serialize=serialize_verdict_file,
            noun="verdict",
            plural="session verdicts",
            file_stem="session-verdicts",
        )

    def get(self, session: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(session)
            return {**entry, "verdict": dict(entry["verdict"])} if entry is not None else None

    def set(self, session: str, verdict: dict, transcript_path: object, agent_session_id: object) -> bool:
        """Keep the verdict that just landed on a row; False (and nothing kept) when it cannot be
        restored later anyway: no conversation id, no transcript path, or a malformed verdict."""
        entry = clean_entry({"verdict": verdict, "transcript_path": transcript_path, "agent_session_id": agent_session_id})
        if entry is None:
            return False
        with self._lock:
            if self._entries.get(session) != entry:
                self._entries[session] = entry
                self._persist_locked()
        return True

    def label(self, session: str, decision_id: str, label: dict | None) -> bool:
        """The operator labelled the kept verdict's decision (open_work.py § The operator's label): keep the
        label with the copy, or drop it (None). Compare-and-set on the decision id; False when nothing kept
        is that decision, or nothing changed."""
        with self._lock:
            entry = self._entries.get(session)
            if entry is None or entry["verdict"].get("decision_id") != decision_id:
                return False
            verdict = {key: value for key, value in entry["verdict"].items() if key != "operator"}
            if label is not None:
                verdict["operator"] = dict(label)
            if verdict == entry["verdict"]:
                return False
            entry["verdict"] = verdict
            self._persist_locked()
            return True

    def mark_read(self, session: str) -> bool:
        """The operator opened the pane of a kept answer to read: stamp the copy `read`, so a restart
        restores the ✅ without the mark. False when nothing is kept or it is already read."""
        with self._lock:
            entry = self._entries.get(session)
            if entry is None or entry["verdict"].get("read"):
                return False
            entry["verdict"] = {**entry["verdict"], "read": True}
            self._persist_locked()
            return True
