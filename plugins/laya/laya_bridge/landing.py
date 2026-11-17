"""Row shape: owned by schemas/src/decide.ts (layaLandingRowSchema) — see the note there.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

The landing record: where each agent-rack verdict LANDED, one row per `decision_id`, beside the
decision ledger. The ledger row says what Laya answered; this row says what the rack did with it.

── WHY IT EXISTS ──────────────────────────────────────────────────────────────
Precision per audience — "of the ⚠️ shown on a free row, how many were right" — needs the audience
of every verdict, and the rack already decides one: every landed ⚠️ is classified in
tldrack `main.py` as held (its own shell jobs, or children still running), new, a repeat
or retired (tldrack `attention.py`). Until this record that class reached only the rack's
attention signals, keyed by a session name and a time, never by the decision. Now it is a row per
decision, and the precision per landing class reads it (`review_data.live_rates`, on `live_sample`). The
classification was measured before it was trusted: what the fold missed, and the lineage fix,
are owned by bridge/tldrack/attention.py § 1b — see the note there.

── WHY A SIDECAR, NOT THE TAG OR THE DECISION ROW ─────────────────────────────
- Not at decide time, in `open_work.py`: the row's state is racy then. The verdict is asked ~1 s
  after the Stop hook, before the arbiter has had its tick to say `background`.
- Not in the decide tag: the tag is a strict wire on both sides (`layaLedgerTagSchema` is a
  strictObject, `ledger.py` `TAG_FIELDS`), and an unknown field makes an older plugin refuse the
  whole tag. How a real tag field is added: owned by tldrack open_work.py
  `OPTIONAL_TAG_FIELDS` — see the note there.
- Not appended to the decision row: decision rows are write-once (§ Ledger, laya-decisions).

So `main.py` posts one body to `POST /landing` after it classified the verdict, in process (a
native caller only: `guard.is_native`), and this module writes the row. A `decision_id` the ledger
does not hold is `409 unknown_decision`, so every row joins one decision.

── WHAT A ROW HOLDS ───────────────────────────────────────────────────────────
`class` (held · new · repeat · retired; null for a verdict that drew no ⚠️), `held` (the audience:
background · children; null = free, recorded for EVERY verdict, so recall splits by audience too),
`running` (tmux descendants, in-process agents and workflows, shell jobs: the counts that decided
`held`), and `trace`: the turn's Rubric-critic features (ran tests, edited files, opened a PR,
ended on a question; null when the bridge read no Claude transcript). Counts and booleans: no text,
no path, no session name.

── TRACE FEATURES ARE LOGGED, NEVER FED ───────────────────────────────────────
Nothing here reaches Laya's input. Whether a feature would lift the verdict is measured offline, in
`laya-lab compare`, before any change to what the bridge asks (`QUESTION_VERSION`).

── FILES ──────────────────────────────────────────────────────────────────────
`<laya>/landing/<surface>.jsonl` (`paths.laya_dir`), 0600 in a 0700 folder, rotated like the ledger
(`ledger.append_rotating`) and OFF until `laya/` exists (`ledger.private_folder`). The session
ledger keeps every row past rotation (tldrack `ledger/side_ledgers.py`, `laya-landing:`).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from laya_bridge import files, ledger, rows
from laya_bridge.paths import LANDING_DIRNAME

VERSION = 1
#: Only the rack lands a verdict on a row.
SURFACES = ("agent_open_work",)
CLASSES = ("held", "new", "repeat", "retired")
HELD_REASONS = ("background", "children")
#: The body's keys, in order. tldrack open_work.py copies them (`LANDING_*`), pinned by
#: lab/tests/test_landing_wire.py.
BODY_KEYS = ("decision_id", "surface", "class", "held", "running", "trace")
RUNNING_KEYS = ("descendants", "agents", "workflows", "shells")
TRACE_KEYS = ("ran_tests", "edited_files", "opened_pr", "ended_on_question")
TRACE_COUNTS = frozenset({"edited_files"})
#: `LAYA_LANDING_COUNT_MAX`: no live count comes near it; a larger one is a bug, refused.
COUNT_MAX = 100_000
ROTATE_BYTES = 4 * 1024 * 1024
KEEP_SEGMENTS = 2
READ_MAX_BYTES = (KEEP_SEGMENTS + 1) * ROTATE_BYTES
#: Recent ids already landed: a repeat writes nothing (a cache, never the truth).
SEEN_MAX = 4096


class LandingError(ValueError):
    """A refused `/landing` body: always a 400, and the message names the field, never its value."""


def _count(value: object) -> bool:
    return files.is_count(value) and value <= COUNT_MAX


def _clean_block(raw: object, name: str, keys: tuple[str, ...], counts: frozenset[str]) -> dict:
    if not isinstance(raw, dict):
        raise LandingError(f"landing.{name}: must be an object")
    if set(raw) != set(keys):
        raise LandingError(f"landing.{name}: must hold exactly {', '.join(keys)}")
    block: dict[str, Any] = {}
    for key in keys:
        value = raw[key]
        if key in counts:
            if not _count(value):
                raise LandingError(f"landing.{name}.{key}: must be a whole number from 0 to {COUNT_MAX}")
            block[key] = int(value)
        else:
            if not isinstance(value, bool):
                raise LandingError(f"landing.{name}.{key}: must be true or false")
            block[key] = value
    return block


def clean_landing(body: Any) -> dict:
    """A `/landing` body (`layaLandingRowSchema` minus what the plugin stamps), checked strictly."""
    if not isinstance(body, dict):
        raise LandingError("landing: the body must be a JSON object")
    unknown = [key for key in body if key not in BODY_KEYS]
    if unknown:
        named = unknown[0] if isinstance(unknown[0], str) and ledger.FIELD_NAME.fullmatch(unknown[0]) else "<field>"
        raise LandingError(f"landing.{named}: not a landing field")
    missing = [key for key in BODY_KEYS if key not in body]
    if missing:
        raise LandingError(f"landing.{missing[0]}: required")
    decision_id = body["decision_id"]
    if not isinstance(decision_id, str) or not ledger.DECISION_ID.fullmatch(decision_id):
        raise LandingError("landing.decision_id: not a decision id")
    if body["surface"] not in SURFACES:
        raise LandingError(f"landing.surface: must be one of {', '.join(SURFACES)}")
    landing_class, held = body["class"], body["held"]
    if landing_class is not None and landing_class not in CLASSES:
        raise LandingError(f"landing.class: must be null or one of {', '.join(CLASSES)}")
    if held is not None and held not in HELD_REASONS:
        raise LandingError(f"landing.held: must be null or one of {', '.join(HELD_REASONS)}")
    if landing_class == "held" and held is None:
        raise LandingError("landing.held: a held ⚠️ names why")
    if landing_class in ("new", "repeat") and held is not None:
        raise LandingError("landing.held: a new or repeat ⚠️ was shown, so it was not held")
    running = _clean_block(body["running"], "running", RUNNING_KEYS, frozenset(RUNNING_KEYS))
    trace = None if body["trace"] is None else _clean_block(body["trace"], "trace", TRACE_KEYS, TRACE_COUNTS)
    return {
        "decision_id": decision_id,
        "surface": body["surface"],
        "class": landing_class,
        "held": held,
        "running": running,
        "trace": trace,
    }


def landing_row(cleaned: Mapping[str, Any], now: datetime) -> dict:
    """The row as it is written: the cleaned body, stamped."""
    return {
        "kind": "landing",
        "v": VERSION,
        "decision_id": cleaned["decision_id"],
        "at": rows.utc_stamp(now),
        "surface": cleaned["surface"],
        "class": cleaned["class"],
        "held": cleaned["held"],
        "running": dict(cleaned["running"]),
        "trace": None if cleaned["trace"] is None else dict(cleaned["trace"]),
    }


class LandingLog:
    """The one writer. `base` is read per call, so a params folder created later turns it on."""

    def __init__(
        self,
        base: Callable[[], Path | None],
        clock: Callable[[], datetime] = ledger.utc_now,
        log: Callable[[str], None] = lambda _message: None,
        *,
        rotate_bytes: int = ROTATE_BYTES,
        keep_segments: int = KEEP_SEGMENTS,
    ) -> None:
        self._base, self._clock = base, clock
        self._rotate_bytes, self._keep_segments = rotate_bytes, keep_segments
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._log_once = ledger.LogOnce(log, "landing")

    def record(self, cleaned: Mapping[str, Any]) -> tuple[str, bool]:
        """`('ok' | 'off', duplicate)`. One row per decision: a repeat of a recent id writes nothing.
        Never raises: an unwritable folder is `off`, said once."""
        folder = ledger.private_folder(self._base, LANDING_DIRNAME, "landing record", self._log_once)
        if folder is None:
            return "off", False
        decision_id = cleaned["decision_id"]
        with self._lock:
            if decision_id in self._seen:
                return "ok", True
            try:
                ledger.append_rotating(
                    folder[1],
                    cleaned["surface"],
                    landing_row(cleaned, self._clock()),
                    rotate_bytes=self._rotate_bytes,
                    keep_segments=self._keep_segments,
                    clock=self._clock,
                    forget=ledger.forget_segment,
                )
            except (OSError, ValueError, files.SourceError) as exc:
                self._log_once("os", f"cannot write ({type(exc).__name__}) — landings go unrecorded")
                return "off", False
            self._seen[decision_id] = None
            while len(self._seen) > SEEN_MAX:
                self._seen.popitem(last=False)
        return "ok", False


# ── readers ───────────────────────────────────────────────────────────────────


def _row_ok(row: Mapping[str, Any]) -> bool:
    return (
        row.get("kind") == "landing"
        and isinstance(row.get("decision_id"), str)
        and ledger.DECISION_ID.fullmatch(row["decision_id"]) is not None
        and row.get("surface") in SURFACES
        and isinstance(row.get("at"), str)
        and row.get("class") in (None, *CLASSES)
        and row.get("held") in (None, *HELD_REASONS)
    )


def _parse_rows(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    return [row for row in parsed if _row_ok(row)]


def iter_landings(base: Path | None, surface: str = SURFACES[0], since: str | None = None) -> Iterator[dict]:
    """Every readable landing row of `surface`, oldest first; `since` (a row `at`) skips older ones."""
    if base is None:
        return
    yield from ledger.iter_folder_rows(base / LANDING_DIRNAME, surface, _parse_rows, _row_ok, since, READ_MAX_BYTES)


def landings_by_decision(base: Path | None, surface: str = SURFACES[0], since: str | None = None) -> dict[str, dict]:
    """`{decision_id: row}`, the FIRST row per decision (the writer allows one; a restart could repeat it)."""
    out: dict[str, dict] = {}
    for row in iter_landings(base, surface, since):
        out.setdefault(row["decision_id"], row)
    return out
