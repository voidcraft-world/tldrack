"""Which of Laya's ⚠️ deserve the operator's attention: new, a repeat, parked, or held as ⏳.

THE PROBLEM (measured 2026-11-04). ⚠️ means "work is open", never "you are needed". Most ⚠️ came
from one orchestrator ending turns while its own child sessions and waves ran, and none of the ⚠️
shown in a week was dismissed: a mark that fires on every turn end of a working rack is wallpaper
(a string repeated on every row carries no information). This module decides, per ⚠️, whether it
is NEWS.

── 1. HELD: its own children are still running ──────────────────────────────
A session spawned with `parent_session` carries a lineage stamp (session_lineage.py). A parent whose
live DESCENDANTS are still busy, in the background or at a prompt is waiting on its own work, not on
the operator, so its ⚠️ draws as ⏳ — the same ruling a `background` row got on
2026-11-04 (`open_work_waiting_shown`, ui/src/config/terminal-status-flags.ts). A child at a
prompt keeps the parent held because the child's own 🙋 is the mark that needs the operator.
`running_descendants` is the one count; rows carry it as the additive `children_running`. Once the
last child stops, the parent's ⚠️ comes back: only then is the operator the one who can move it.

── 1b. HELD: its IN-PROCESS children are still running ──────────────────────
Most of an orchestrator's children never get a tmux session: a background Agent call or a Workflow
run (a wave of agents) lives inside the parent's own Claude process. Measured 2026-11-09
(`tldrack ledger audience`): most linked ⚠️ landed while such a child of that conversation was
running, a workflow agent more often than a subagent. Some drawn on a `free` row had a workflow
running, and most of those held as `background` were really waiting on children. Their lineage is the parent's
OWN transcript: the launch result names the child (`agentId`, or a workflow's `taskId`) and its
`<task-notification>` closes it by the same id (`transcript.py` `fold_background_agents` /
`fold_background_workflows`). Workflows were invisible to that fold until then, so the Stop hook
stored `free` and the ⚠️ landed before the arbiter's tick said `background`.

The transcript is the early witness; Claude's live index is the authority once it has spoken. A
live agent or workflow keeps the index `busy` after the turn ends (agent_index.py § BACKGROUND
WORK), so an index that says idle or shell means no in-process child runs, whatever the fold says:
a resumed conversation copies an old launch without its notification, and would otherwise hold its
parent forever (`in_process_children`). No index entry: none, the same hook-only rule
`main.py` `background_now` keeps. A landing is judged by an index read AFTER its Stop, never the
last tick's, which often watched the turn still running (state_arbiter.py `index_since`,
`settled_state`). A held row's REASON is then `children` when any child runs, tmux
or in-process, and `background` only for its own shell jobs (`held_reason`): the audience the
landing record logs (plugins/laya/laya_bridge/landing.py).

── 2. A REPEAT: the same open loop, retold ──────────────────────────────────
WHY NOT TEXT SIMILARITY. Measured 2026-11-04 on the open-work ledger's consecutive same-session
⚠️ pairs: word-3-shingle Jaccard has a median near zero, and almost no pair is near-identical. An orchestrator says "wave 3 merged, waiting on wave 4" in new words every turn.
What actually repeats is the LOOP: the agent woke ITSELF (a `<task-notification>`, a peer message)
and ended another turn with work still open, and the operator has not spoken to it since.

A loop opens at the first ⚠️ after a human prompt and closes at the next human prompt, a ✅
(Laya: the work looks done), a ✗ NOT OPEN dismiss, a new conversation or a kill. Every ⚠️ inside an
open loop is a `repeat`: still drawn while nobody has looked, but never a new interruption — no
ping, no new digest line, counted as `warning_repeat` in the telemetry. Whether a prompt is human is
read off its text the way the session ledger reads a transcript (`ledger/claude_fold.py`
`origin_of`): `UserPromptSubmit` fires for a task notification too (seen 2026-11-04).

── 3. RETIRED: "seen, not now" ──────────────────────────────────────────────
The operator's ⏸ on a ⚠️ (`POST /open-work/snooze`) parks the loop. Its current ⚠️ and every repeat
after it draw nothing until the loop closes; the reply mark (💬 / ○) still draws, so the row is
never silent, and the digest still counts parked loops ("+2 seen"), which is where "later" lives.
Parking posts nothing to Laya's ledger. A ✗ (`POST /open-work/dismiss`) posts the `dismissed`
reaction and closes the loop: telemetry, and a grade only when the rack's ✗ / ✓ write operator
answers. What precision is measured on: owned by
plugins/laya/laya_bridge/review_data.py `live_rates` — see the note there.

── WHAT IS KEPT ─────────────────────────────────────────────────────────────
Per session `{opened_at, parked, last}` in `session-attention.json` (env
`TLDRACK_ATTENTION_PATH`), because six restarts a day would otherwise resurrect every
parked loop. No text, ever. A row that left this map simply has no loop: the next ⚠️ is new.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from tldrack import paths
from tldrack.ledger.claude_fold import origin_of
from tldrack.session_state_file import SessionStateFile, parse_session_map, serialize_session_map

ATTENTION_FILE_VERSION = 1

ATTENTION_FILE = "session-attention.json"

#: A descendant in one of these is still somebody's work in progress, so its parent is held (§ 1).
UNFINISHED_STATES = frozenset({"busy", "background", "attention"})

#: What a ⚠️ was, when it landed (§ 2, § 3). `held` is decided at draw time, not here.
NEW, REPEAT, RETIRED = "new", "repeat", "retired"
CLASSES = (NEW, REPEAT, RETIRED)

#: Why a row is held (§ 1, § 1b): the `warning_held` signal's reason and the landing record's `held`.
BACKGROUND, CHILDREN = "background", "children"
HELD_REASONS = (BACKGROUND, CHILDREN)
#: A landed ⚠️'s class as the telemetry and the landing record name it: held, or its loop class.
HELD = "held"
LANDING_CLASSES = (HELD, NEW, REPEAT, RETIRED)


def resolve_attention_path() -> Path:
    env_path = os.environ.get("TLDRACK_ATTENTION_PATH")
    return Path(env_path) if env_path else paths.state_file(ATTENTION_FILE)


def prompt_origin(prompt: object) -> str:
    """`human`, `task`, `peer`, … for a `UserPromptSubmit` prompt. Pure. Anything that is not
    text is treated as human: a loop must never survive a prompt the bridge could not read."""
    if not isinstance(prompt, str):
        return "human"
    return origin_of({}, prompt, "main")


def running_descendants(rows: Iterable[Mapping]) -> dict[str, int]:
    """session → how many of its LIVE descendants are unfinished (§ 1). Pure.

    Joined by the lineage UUID, never by a name (session_lineage.py owns why). A cycle or a
    duplicated id cannot loop: each walk stops at an ancestor it has already counted. Sessions with
    no unfinished descendant are absent rather than 0, so a caller can treat the map as the set of
    held parents."""
    live = [row for row in rows if row.get("live") and isinstance(row.get("session"), str)]
    by_id: dict[str, Mapping] = {}
    for row in live:
        lineage = row.get("lineage") if isinstance(row.get("lineage"), dict) else {}
        node = lineage.get("id")
        if isinstance(node, str) and node not in by_id:
            by_id[node] = row
    counts: dict[str, int] = {}
    for row in live:
        if row.get("state") not in UNFINISHED_STATES:
            continue
        lineage = row.get("lineage") if isinstance(row.get("lineage"), dict) else {}
        seen = {lineage.get("id")}
        parent = lineage.get("parent_id")
        while isinstance(parent, str) and parent not in seen and parent in by_id:
            seen.add(parent)
            ancestor = by_id[parent]
            counts[ancestor["session"]] = counts.get(ancestor["session"], 0) + 1
            parent = (ancestor.get("lineage") or {}).get("parent_id")
    return counts


def in_process_children(agents: int, workflows: int, index_state: object) -> tuple[int, int]:
    """`(agents, workflows)` the transcript says still run, believed only while Claude's index says
    `busy` (§ 1b): idle, shell, a prompt or no believable entry → `(0, 0)`. Pure."""
    return (agents, workflows) if index_state == "busy" else (0, 0)


def held_reason(state: object, children_running: int, in_process: int = 0) -> str | None:
    """Why a ⚠️ on this row draws as ⏳, or None for a row that is free (§ 1, § 1b). Pure.
    Any running child, tmux or in-process, is `children`; a row's own shell jobs are `background`."""
    if children_running > 0 or in_process > 0:
        return CHILDREN
    return BACKGROUND if state == "background" else None


def is_held(state: object, children_running: int, in_process: int = 0) -> bool:
    """Does a ⚠️ on this row draw as ⏳? Its own background work, or unfinished children."""
    return held_reason(state, children_running, in_process) is not None


def landing_class(kind: str | None, reason: str | None) -> str | None:
    """The landed ⚠️'s class: `retired` stays retired (a parked loop draws nothing either way), a
    held one is `held`, else its loop class; None for a verdict that drew no ⚠️. Pure."""
    if kind is None:
        return None
    if kind != RETIRED and reason is not None:
        return HELD
    return kind


# ── the persisted loops ─────────────────────────────────────────────────────


def clean_loop(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    opened_at, parked, last = entry.get("opened_at"), entry.get("parked"), entry.get("last")
    if (
        isinstance(opened_at, bool)
        or not isinstance(opened_at, (int, float))
        or opened_at < 0
        or not isinstance(parked, bool)
        or last not in CLASSES
    ):
        return None
    return {"opened_at": float(opened_at), "parked": parked, "last": last}


def parse_attention_file(text: str) -> dict[str, dict]:
    return parse_session_map(text, clean_loop)


def serialize_attention_file(sessions: dict[str, dict]) -> str:
    return serialize_session_map(sessions, ATTENTION_FILE_VERSION)


class OpenLoops(SessionStateFile[dict]):
    """The open-work loop per session (§ 2, § 3), persisted. The file, the lock, the tolerant load,
    `rename` and `forget` are owned by `session_state_file.py` — see the note there."""

    def __init__(self, path: Path, *, clock=time.time) -> None:
        super().__init__(
            path,
            parse=parse_attention_file,
            serialize=serialize_attention_file,
            noun="attention",
            plural="open-work loops",
            file_stem="session-attention",
        )
        self._clock = clock

    def note_prompt(self, session: str, prompt: object) -> bool:
        """A main-thread prompt arrived. A human one closes the loop; True when it did."""
        if prompt_origin(prompt) != "human":
            return False
        return self.forget(session)

    def classify(self, session: str, *, is_open: bool, work_done: bool) -> str | None:
        """What a verdict that just landed is: `new`, `repeat` or `retired` for a ⚠️, None for any
        other mark. A ✅ closes the loop; the band between the cuts (💬) leaves it as it was,
        because "not sure it is open" is not "done" (open_work.py § Three bands)."""
        with self._lock:
            loop = self._entries.get(session)
            if not is_open:
                if work_done and loop is not None:
                    del self._entries[session]
                    self._persist_locked()
                return None
            if loop is None:
                self._entries[session] = {"opened_at": self._clock(), "parked": False, "last": NEW}
                self._persist_locked()
                return NEW
            kind = RETIRED if loop["parked"] else REPEAT
            if loop["last"] != kind:
                loop["last"] = kind
                self._persist_locked()
            return kind

    def park(self, session: str) -> bool:
        """ "Seen, not now" (§ 3): True when the loop is newly parked. Opens one if the ⚠️ on screen
        predates this store (a verdict restored from before the upgrade)."""
        with self._lock:
            loop = self._entries.get(session)
            if loop is not None and loop["parked"]:
                return False
            if loop is None:
                loop = {"opened_at": self._clock(), "parked": True, "last": RETIRED}
            else:
                loop = {**loop, "parked": True, "last": RETIRED}
            self._entries[session] = loop
            self._persist_locked()
            return True

    def parked(self) -> set[str]:
        with self._lock:
            return {session for session, loop in self._entries.items() if loop["parked"]}

    def last_class(self) -> dict[str, str]:
        with self._lock:
            return {session: loop["last"] for session, loop in self._entries.items()}

    def get(self, session: str) -> dict | None:
        with self._lock:
            loop = self._entries.get(session)
            return dict(loop) if loop is not None else None


def attention_row_fields(
    row: Mapping, *, children: Mapping[str, int], parked: set[str], classes: Mapping[str, str]
) -> dict[str, object]:
    """The additive `/snapshot` row fields this module owns, for one LIVE row. Omitted when false or
    zero, so an old client and a quiet row see byte-identical rows: `children_running` (§ 1),
    `open_work_seen` (§ 3: the ⚠️ is parked, draw nothing) and `open_work_repeat` (§ 2)."""
    session = row.get("session")
    fields: dict[str, object] = {}
    count = children.get(session, 0) if isinstance(session, str) else 0
    if count:
        fields["children_running"] = count
    if row.get("open_work") is True and isinstance(session, str):
        if session in parked:
            fields["open_work_seen"] = True
        elif classes.get(session) == REPEAT:
            fields["open_work_repeat"] = True
    return fields
