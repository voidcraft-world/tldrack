"""The running model, known from the STATUSLINE — before the first reply exists.

THE BUG THIS EXISTS FOR. `model` used to be written from exactly one place:
`POST /hook`, which reads it out of the Claude transcript (transcript.py). But a
transcript only carries a model on its **assistant** entries, and a brand-new
session has none — its first twenty lines are `mode`, `permission-mode`,
`bridge-session`, `file-history-snapshot`, `user`, and not one of them names a
model. So `SessionStart` and `UserPromptSubmit` both reported None, and every
rack surface labelled a freshly opened session "claude disconnected" until
the agent's first reply landed. `models.py` does not cover this: it is keyed by
session NAME and only answers for a name seen in a PREVIOUS run.

THE SOURCE. Claude Code's statusLine payload carries `model.id` from its very
first render, seconds before any assistant message exists — and statusline.py
already forwards that whole payload to `POST /usage`, where the bridge parsed
`rate_limits` out of it and threw the model away. Nothing new is installed here;
a field already on the wire simply stops being discarded.

THE JOIN. `POST /usage` names no tmux session, so a reading is stored under its
`transcript_path` and resolved to a session at snapshot time via the binding
`TranscriptTracker` already keeps. `transcript_path` is a strictly better key
than the session name: it is unique per CLAUDE session, so a reused tmux name
(`vc-1`, `vc-2`) can never inherit a stale reading. It also makes arrival order
irrelevant — both halves are read at snapshot time, so whichever of the first
hook and the first statusline lands second completes the join.

NOT PERSISTED, unlike models.py. A statusline refresh re-reports within a second
of anything happening, so a file would buy nothing — the same reasoning
UsageLimitsStore already uses for the rate-limit windows it holds.
"""

from __future__ import annotations

import threading

from tldrack.models import MODEL_TOKEN_RE
from tldrack.statusline import context_left_percent

# Cap on distinct transcript paths held at once. The key comes OFF THE WIRE, and
# `normalize_transcript_path` resolves a path without requiring the file to
# exist — so a local caller past the Origin guard (no Origin at all passes, i.e.
# plain curl) could otherwise insert unbounded keys by POSTing
# ~/.claude/a1.jsonl, a2.jsonl, ... Same hazard, same answer as PresenceStore's
# MAX_PRESENCE_CLIENTS, whose `client_id` is likewise caller-chosen. Evicting
# costs one label for about a second, until the next statusline tick.
MAX_STATUSLINE_PATHS = 256


def parse_statusline_model(payload: object) -> str | None:
    """`{"model": {"id": "claude-opus-5"}}` → `"claude-opus-5"`, else None.

    NEVER raises, which is the difference from `parse_usage_limits`. That one
    fails loud on a malformed `rate_limits` because rate limits are the payload's
    PURPOSE — garbage there means the shape changed and the user should hear it.
    The model is a bonus rider on the same request, so a shape we don't
    recognize costs a label, never a 400 on a statusline Claude Code is waiting on.

    Validated by shape (`MODEL_TOKEN_RE`), not membership, for the reason
    models.py gives: ids are minted by Anthropic and by Bedrock's inference
    profiles, and the bridge ships no catalog it would then have to chase.
    """
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if not isinstance(model, dict):
        return None
    model_id = model.get("id")
    if not isinstance(model_id, str) or not MODEL_TOKEN_RE.fullmatch(model_id):
        return None
    return model_id


def resolve_fallback_models(
    disk_models: dict[str, dict],
    statusline_models: dict[str, str],
    session_paths: dict[str, str],
) -> dict[str, dict]:
    """The fallback map `merge_snapshot` takes, with the statusline folded in.

    Precedence among the FALLBACKS is statusline over disk: a statusline reading
    is keyed by `transcript_path`, so it describes the Claude session running
    right now, while `models.py` is keyed by a tmux name that gets reused.
    (Neither tier outranks a live transcript reading — `merge_snapshot` consults
    this map only for a session whose StatusStore row has no model at all.)

    An equal model is deliberately NOT overridden: the disk entry carries token
    `usage` and the statusline carries none, so replacing like with like would
    silently cost an idle session its cost estimate.
    """
    # Shallow per entry, so the nested `usage` stays aliased to what the caller
    # passed. Safe because SessionModelStore.get_all already hands out a deep
    # copy — the reason it does is owned there; see the note on that method.
    resolved = {session: dict(entry) for session, entry in disk_models.items()}
    for session, path in session_paths.items():
        model = statusline_models.get(path)
        if model is None:
            continue
        remembered = resolved.get(session)
        if remembered is not None and remembered.get("model") == model:
            continue  # same answer, and the remembered one also has usage
        # No usage: the statusline payload carries none. merge_snapshot's
        # "only alongside a remembered model" guard keeps that honest.
        resolved[session] = {"model": model, "usage": None}
    return resolved


def parse_statusline_context_left(payload: object) -> tuple[bool, int | None]:
    """(reported, percent left) for the payload's `context_window`.

    `reported` False means the payload has no `context_window` at all (an older
    Claude Code) — keep whatever reading exists. Reported with None means Claude
    Code said "unknown right now" (before the first API call, or right after
    /compact): the caller DISCARDS the old reading, because a pre-compact "4% left"
    shown after a compact is exactly backwards. The percentage itself is computed
    by `context_left_percent` (statusline.py owns the formula). Never raises.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("context_window"), dict):
        return False, None
    return True, context_left_percent(payload)


def resolve_context_left(contexts: dict[str, int], session_paths: dict[str, str]) -> dict[str, int]:
    """Statusline context readings (keyed by transcript path) → tmux session name.

    The same join `resolve_fallback_models` makes, for the same reason: a
    statusline names no tmux session, and the transcript path is the key that
    survives a reused session name.
    """
    return {session: contexts[path] for session, path in session_paths.items() if path in contexts}


class StatuslineReadingStore:
    """Last value the statusline reported, per normalized transcript path — one
    instance for the model, one for the context percentage.

    Handler threads race (ThreadingHTTPServer), so reads copy under the lock.
    Bounded and least-recently-set-evicted (MAX_STATUSLINE_PATHS) because the
    key is caller-supplied — see the note there. A path whose session is gone is
    never pruned deliberately; it simply stops resolving to a tmux name, and
    ages out on its own once the cap bites.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, object] = {}

    def set(self, path: str | None, value: object) -> bool:
        """Record a reading; True when it CHANGED something.

        The return value is what lets the caller push an SSE frame only on real
        news — a statusline refreshes constantly, and re-broadcasting an
        unchanged model to every client would make the busiest route on the
        bridge the noisiest one too.

        An UNCHANGED reading still counts as a touch for eviction (pop, then
        re-insert). Without that, the busiest path in the store — a session
        reporting the same model every second — would be the oldest key by
        insertion order and so the first one evicted.
        """
        # `is None`, not falsiness: 0 is a real reading ("0% left").
        if not path or value is None:
            return False
        with self._lock:
            changed = self._values.pop(path, None) != value
            self._values[path] = value  # re-inserted → newest in insertion order
            while len(self._values) > MAX_STATUSLINE_PATHS:
                # dict preserves insertion order, so the front is the coldest.
                self._values.pop(next(iter(self._values)))
            return changed

    def discard(self, path: str | None) -> bool:
        """Forget a path's reading; True when there was one to forget."""
        if not path:
            return False
        with self._lock:
            return self._values.pop(path, None) is not None

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._values)
