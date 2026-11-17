"""Last-known model + token usage per tmux session, surviving bridge restarts.

THE BUG THIS EXISTS FOR. `StatusStore` is in-memory and lost on restart *by
design* — hooks re-report on the next event. That reasoning holds for `state`,
which is a live claim, but not for `model`, which is written from exactly one
place (`POST /hook`, read out of the Claude transcript). An **idle** Claude
session — one sitting at its prompt, whose `Stop` hook already fired before the
restart — emits no further hooks at all. So its model stayed unknown forever and
every rack surface rendered "claude disconnected" for a session that was
plainly connected, until the user happened to type into it.

WHAT IS AND IS NOT PERSISTED. `model` and `usage` are *facts about what ran*:
re-serving a slightly stale one is at worst a beat behind, and the next hook
corrects it. `state` / `message` / `activity` are live claims about right now —
a `busy` restored from disk after a crash would be an outright lie, and the
frontend's staleness handling keys off `age_seconds`, which a restored row has
no honest value for. So this store deliberately holds two keys and not five.

FALLBACK, NEVER OVERRIDE. `merge_snapshot` consults this only when the live
StatusStore has no model for that session. A hook that fired this run is always
more current than the file.

Same shape as icons.py (JSON envelope under ~/.local/state/tldrack/,
atomic write, tolerant load, entries never pruned) — a recreated session with
the same name keeps its reading, and a reading with no current session stays
dormant rather than synthesizing a row.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tldrack import paths
from tldrack.session_state_file import SessionStateFile
from tldrack.snapshot import SESSION_NAME_RE

# Shape-only guard for a model id. Deliberately permissive about the VOCABULARY
# (same reasoning as icons.py's ICON_TOKEN_RE): ids are minted by Anthropic and
# by Bedrock's inference profiles, so `claude-opus-5`, `claude-haiku-4-5-20251001`
# and `us.anthropic.claude-sonnet-5-v1:0` must all round-trip without the bridge
# shipping a model catalog it would then have to chase.
MODEL_TOKEN_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# The four counters `transcript.py` sums out of a transcript. Anything else in a
# stored usage block is dropped rather than trusted.
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

MODEL_FILE_VERSION = 1

MODELS_FILE = "session-models.json"


def resolve_models_path() -> Path:
    env_path = os.environ.get("TLDRACK_MODELS_PATH")
    return Path(env_path) if env_path else paths.state_file(MODELS_FILE)


def clean_usage(usage: object) -> dict[str, int] | None:
    """Coerce a usage block to the four int counters, or None if unusable.

    Missing counters default to 0 rather than rejecting the block: a transcript
    that only ever reported output tokens is still worth keeping.
    """
    if not isinstance(usage, dict):
        return None
    cleaned = {key: usage.get(key, 0) for key in USAGE_KEYS}
    if not all(isinstance(value, int) and value >= 0 for value in cleaned.values()):
        return None
    return cleaned


def parse_model_file(text: str) -> dict[str, dict]:
    """Parse session-models.json text into a session -> {model, usage} map.

    Raises ValueError when the envelope itself is wrong so the caller can warn
    about a corrupt file; individually malformed ENTRIES are dropped instead —
    one bad row must not cost every other session its label. An entry whose
    usage is unusable keeps its model with `usage: None`, because the model is
    the part the label actually needs.
    """
    payload = json.loads(text)  # ValueError on non-JSON
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), dict):
        raise ValueError('expected {"version": ..., "sessions": {...}}')
    parsed: dict[str, dict] = {}
    for session, entry in payload["sessions"].items():
        if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
            continue
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if not isinstance(model, str) or not MODEL_TOKEN_RE.fullmatch(model):
            continue
        parsed[session] = {"model": model, "usage": clean_usage(entry.get("usage"))}
    return parsed


def serialize_model_file(sessions: dict[str, dict]) -> str:
    """Stable (sorted, indented) file body — the file is user-inspectable state."""
    body = {
        "version": MODEL_FILE_VERSION,
        "sessions": {name: sessions[name] for name in sorted(sessions)},
    }
    return json.dumps(body, indent=2) + "\n"


class SessionModelStore(SessionStateFile[dict]):
    """File-persisted last-known model/usage per session.

    The file, the lock, the tolerant load and `rename` are owned by
    `session_state_file.py` — see the note there.

    Writes are NOT rare here — unlike an icon pick, this is touched by every
    hook — so `set` returns early when nothing changed. Without that, a busy
    agent's PreToolUse storm would rewrite the file on every tool call.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=parse_model_file,
            serialize=serialize_model_file,
            noun="reading",
            plural="session models",
            file_stem="session-models",
        )

    def get_all(self) -> dict[str, dict]:
        """A fully independent copy — `usage` is nested, so a shallow `dict()`
        would hand callers a live reference into the store. `merge_snapshot`
        drops these straight into snapshot rows that are then handed to every
        client, which is precisely where an aliased dict would be dangerous."""
        with self._lock:
            return {
                name: {
                    "model": entry["model"],
                    "usage": dict(entry["usage"]) if entry["usage"] is not None else None,
                }
                for name, entry in self._entries.items()
            }

    def set(self, session: str, model: str | None, usage: object = None) -> None:
        """Record a session's model (and usage), persisting only on a real change.

        A `None` model is IGNORED, not stored and not cleared: the caller reaches
        here on every hook, and `transcripts.update` legitimately answers None
        for a session Claude has not spoken in yet. Treating that as "forget what
        you knew" would erase a good reading the moment a hook arrived without a
        readable transcript — exactly the transient miss `TranscriptTracker`
        already guards against on its own side.
        """
        if model is None or not MODEL_TOKEN_RE.fullmatch(model):
            return
        entry = {"model": model, "usage": clean_usage(usage)}
        with self._lock:
            if self._entries.get(session) == entry:
                return  # unchanged — don't rewrite the file on every PreToolUse
            self._entries[session] = entry
            self._persist_locked()
