"""Row shape: owned by schemas/src/decide.ts (layaLedgerRowSchema) — see the note there.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

The Laya decision ledger: one `decision` row per answered `/decide`, one `reaction` row per
`/react`, under `<laya>/ledger/<surface>.jsonl` (`paths.laya_dir`). The plugin's `/decide` is its one
seam; callers only tag requests and post reactions. It exists so that every verdict a caller acted on can be
scored later against what the operator says was true: the REVIEW deck, laya-lab's sweeps and the latency read
all start from these rows.

- A SURFACE is whatever a caller tags: any name of `paths.SURFACE_NAME`'s shape (also its file's stem, so the
  shape is the path check), one file each. `untagged` is the ledger's
  own name for a request with no tag. The set is open because the callers are the operator's own programs, and
  capped (`MAX_SURFACES`) because each new name is a new file.
- OFF (decision_id null) unless `laya/` already exists as a real directory. This creates only
  `ledger/` (0700), refusing a link there or a path that resolves outside `laya/`; files are 0600.
- Text is kept only for `KEEP_TEXT` surfaces; every other row, `untagged` included, is hash-only.
- `RACK_SURFACES` may be tagged only by a native caller (`guard.is_native` owns what that means),
  and only they may carry a `conversation`.
- The active file rotates at `ROTATE_BYTES` to `<surface>-<YYYYMMDDTHHMMSS>.jsonl`; the newest
  `KEEP_SEGMENTS` segments are kept. Retention IS the read window: every reader goes through
  `iter_rows`, which reads at most `READ_MAX_BYTES` of a surface, so a segment past it would be disk
  that nothing reads. Training text never depends on it: laya-lab exports turn ends from transcripts.
- A decision row also says how busy this machine was (`load`, `load_per_core`) and whether the answer was the model's
  first since laya-serve started (`cold`, `Ledger._first_answer`), beside its `ms`: laya-lab's latency read
  (`laya_lab/latency.py`) buckets by both. Older rows may carry neither.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_bridge import files, rows
from laya_bridge.files import MAX_SAFE_INTEGER, is_number, is_p, utf16_len
from laya_bridge.paths import LAYA_LOCAL_MODEL_ID, LEDGER_DIRNAME, SURFACE_NAME
from laya_bridge.wire import MAX_OPTION_NAME_CHARS, MODEL_NAME

VERSION = 1
#: The surface of a request that carried no tag. Never a tag itself.
UNTAGGED = "untagged"
#: How many surfaces one ledger holds; a tag that would add another is refused (`Ledger.admits`).
MAX_SURFACES = 32
ACTIONS = ("shown", "accepted", "converted", "dismissed", "expired")
KEEP_TEXT = ("agent_open_work",)
RACK_SURFACES = ("agent_open_work", "coordinator")
ROTATE_BYTES = 16 * 1024 * 1024
READ_MAX_BYTES = 32 * 1024 * 1024
KEEP_SEGMENTS = READ_MAX_BYTES // ROTATE_BYTES
INDEX_MAX = 4096
STATE_MAX = 8_000
MAX_ALTERNATIVES = 63
CHECKPOINT_PATH_MAX = 256

#: `LAYA_DECISION_ID`. `[0-9]`, never `\d`: on a str, Python's `\d` takes any Unicode digit and JavaScript's only ASCII.
DECISION_ID = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{12}")
SUBJECT = re.compile(r"[A-Za-z0-9_.:#|-]{1,128}")
#: `LAYA_LEDGER_CONVERSATION`: owned by schemas/src/decide.ts — see the note there. fullmatch.
CONVERSATION = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: A decision row's `state_hash`: `rows.state_digest`, sha256 hex. fullmatch.
STATE_HASH = re.compile(r"[0-9a-f]{64}")
#: `LAYA_PARAMS_RELATIVE_PATH` in schemas/src/decide.ts. fullmatch.
PARAMS_RELATIVE_PATH = re.compile(r"(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
#: The tag fields a decision row copies as they came (`ledgerTagFields`, schemas/src/decide.ts, in its
#: order); `surface` is the row's own. One list, so a new field cannot be accepted and then not copied.
COPIED_TAG_FIELDS = ("subject", "conversation", "question_version", "threshold", "done_threshold")
TAG_FIELDS = ("surface", *COPIED_TAG_FIELDS)
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


class LedgerTagError(ValueError):
    """A refused `ledger` tag or `/react` body: always a 400, and the message names the field, never its value."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_per_core() -> float | None:
    """This Mac's 1-minute load average per core, now (1.0 = every core busy); None where it cannot be read.

    The kernel's own number, and not the devices plugin's sampler: that sampler stops 45 s after the rack's DEVICES
    section leaves the screen, so most decisions would find no sample, and a plugin never imports another. One
    `getloadavg` costs microseconds, which a decision of hundreds of milliseconds never notices.
    """
    try:
        cores = os.cpu_count()
        return round(os.getloadavg()[0] / cores, 3) if cores else None
    except (OSError, AttributeError):
        return None


class LogOnce:
    """`(kind, message)` → one `[laya] <noun>: …` line per kind for the life of the writer: a full disk
    or a planted link repeats on every call, and one line says it."""

    def __init__(self, log: Callable[[str], None], noun: str) -> None:
        self._log, self._noun = log, noun
        self._logged: set[str] = set()

    def __call__(self, kind: str, message: str) -> None:
        if kind not in self._logged:
            self._logged.add(kind)
            self._log(f"[laya] {self._noun}: {message}")


# ── the request side ──────────────────────────────────────────────────────────


def clean_tag(raw: Any, *, rack_ok: bool) -> dict | None:
    """The `ledger` field of a `/decide` body (`layaLedgerTagSchema`), or None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LedgerTagError("ledger: must be an object")
    for key in raw:
        if key not in TAG_FIELDS:
            named = key if isinstance(key, str) and FIELD_NAME.fullmatch(key) else "<field>"
            raise LedgerTagError(f"ledger.{named}: not a ledger field")
    surface = raw.get("surface")
    if not is_surface(surface) or surface == UNTAGGED:
        raise LedgerTagError("ledger.surface: a lowercase name of a-z 0-9 _, at most 64 characters")
    if surface in RACK_SURFACES and not rack_ok:
        raise LedgerTagError("ledger.surface: only the agent rack may tag this surface")
    tag: dict[str, Any] = {"surface": surface}
    if "subject" in raw:
        subject = raw["subject"]
        if not isinstance(subject, str) or not SUBJECT.fullmatch(subject):
            raise LedgerTagError("ledger.subject: 1–128 of A-Z a-z 0-9 _ . : # | -")
        tag["subject"] = subject
    if "conversation" in raw:  # the agent's session id: the REVIEW page bands verdicts by it
        conversation = raw["conversation"]
        if surface not in RACK_SURFACES:
            raise LedgerTagError(f"ledger.conversation: only {', '.join(RACK_SURFACES)} record a conversation")
        if not isinstance(conversation, str) or not CONVERSATION.fullmatch(conversation):
            raise LedgerTagError("ledger.conversation: must be a UUID (8-4-4-4-12 hex)")
        tag["conversation"] = conversation
    if "question_version" in raw:
        version = raw["question_version"]
        if not is_number(version) or not 0 <= version <= MAX_SAFE_INTEGER or version != int(version):
            raise LedgerTagError("ledger.question_version: must be a whole number from 0 to 2^53 - 1")
        tag["question_version"] = int(version)
    if "threshold" in raw:
        threshold = raw["threshold"]
        if not is_p(threshold):
            raise LedgerTagError("ledger.threshold: must be a number in [0, 1]")
        tag["threshold"] = float(threshold)
    if "done_threshold" in raw:  # the agent rack's ✅ cut (tldrack/open_work.py DONE_AT)
        done = raw["done_threshold"]
        if not is_p(done):
            raise LedgerTagError("ledger.done_threshold: must be a number in [0, 1]")
        tag["done_threshold"] = float(done)
    return tag


def clean_react(body: Any) -> tuple[str, str]:
    """`(decision_id, action)` from a `/react` body (`layaReactRequestSchema`, strict)."""
    if not isinstance(body, dict):
        raise LedgerTagError("the body must be a JSON object")
    if set(body) - {"decision_id", "action"}:
        raise LedgerTagError("only decision_id and action are allowed")
    decision_id, action = body.get("decision_id"), body.get("action")
    if not isinstance(decision_id, str) or not DECISION_ID.fullmatch(decision_id):
        raise LedgerTagError("decision_id: not a decision id")
    if not isinstance(action, str) or action not in ACTIONS:
        raise LedgerTagError(f"action: must be one of {', '.join(ACTIONS)}")
    return decision_id, action


# ── answers ───────────────────────────────────────────────────────────────────


def question_hash(question: Mapping[str, Any]) -> str:
    """12 hex chars over the question as asked: a wording change never joins the old answers."""
    blob = json.dumps(
        {
            "type": question.get("type"),
            "instructions": question.get("instructions"),
            "criteria": question.get("criteria") or None,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _distribution(raw: Mapping[str, Any]) -> dict[str, float] | None:
    """`probabilities`, all or nothing — the TS reader (`read_confidence_fields`) drops a partly bad one."""
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        return None
    if not all(isinstance(k, str) and k and utf16_len(k) <= MAX_OPTION_NAME_CHARS and is_p(v) for k, v in probabilities.items()):
        return None
    return {k: float(v) for k, v in probabilities.items()}


def _alternatives(distribution: Mapping[str, float], choice: str) -> list[dict]:
    others = sorted(((p, k) for k, p in distribution.items() if k != choice), key=lambda item: (-item[0], item[1]))
    return [{"choice": k, "p": p} for p, k in others[:MAX_ALTERNATIVES]]


def _answer(name: str, question: Mapping[str, Any], raw: Any) -> dict:
    qtype = question.get("type")
    head = {"q": name, "qh": question_hash(question), "type": qtype}
    abstain = {**head, "choice": "", "p": 0, "abstain": True}
    if not isinstance(raw, dict):
        return abstain
    if qtype == "noul":
        return {**head, "choice": "yes", "p": float(raw["noul"])} if is_p(raw.get("noul")) else abstain
    distribution = _distribution(raw)
    confidence = float(raw["confidence"]) if is_p(raw.get("confidence")) else None
    criteria = question.get("criteria")
    if qtype == "choice":
        choice = raw.get("choice")
        if not isinstance(choice, str) or not choice or utf16_len(choice) > MAX_OPTION_NAME_CHARS:
            return abstain
        if isinstance(criteria, dict) and choice not in criteria:
            return abstain
        p = distribution[choice] if distribution and choice in distribution else confidence
        if p is None:
            return abstain
        out = {**head, "choice": choice, "p": p}
    elif qtype == "score":
        score = raw.get("score")
        top = len(criteria) - 1 if isinstance(criteria, list) else math.inf
        if not is_number(score) or not 0 <= score <= top or confidence is None:
            return abstain
        choice = str(round(score))
        out = {**head, "choice": choice, "score": float(score), "p": confidence}
    else:
        return abstain
    alternatives = _alternatives(distribution, choice) if distribution else []
    if alternatives:
        out["alt"] = alternatives
    return out


def decisions_from(questions: Mapping[str, Mapping[str, Any]], payload: Mapping[str, Any]) -> list[dict]:
    """One recorded answer per ASKED question, in ask order; `type` is always the question's own.

    noul → `{choice:'yes', p}`; choice → `{choice, p: probabilities[choice] ?? confidence, alt}`;
    score → `{choice: str(round(score)), score, p: confidence, alt}`; anything missing or unreadable →
    `{choice:'', p:0, abstain:true}`. `alt` = the other options by p, descending, only when the
    distribution came back.
    """
    answers = payload.get("answers") if isinstance(payload, Mapping) else None
    answers = answers if isinstance(answers, dict) else {}
    return [_answer(name, question, answers.get(name)) for name, question in questions.items()]


# ── storage ───────────────────────────────────────────────────────────────────


def _line(row: Mapping[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


SEGMENT = re.compile(r"(?P<surface>[a-z][a-z0-9_]{0,63})-(?P<stamp>\d{8}T\d{6})(?:-(?P<n>\d{1,4}))?\.jsonl")


def segments(folder: Path, surface: str) -> list[Path]:
    """`folder`'s rotated segments of `surface`, oldest first."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        match = SEGMENT.fullmatch(name)
        if match is not None and match["surface"] == surface:
            found.append(((match["stamp"], int(match["n"] or 0)), folder / name))
    return [path for _key, path in sorted(found)]


def private_folder(
    base_of: Callable[[], Path | None], dirname: str, noun: str, log_once: Callable[[str, str], None]
) -> tuple[Path, Path] | None:
    """(resolved `laya/`, `laya/<dirname>/`), creating the folder 0700; None while `laya/` is not a
    real folder, or the folder is a link or resolves outside it. Never raises. The ledger's rule, and
    every append-only sidecar beside it (landing.py) takes the same one."""
    try:
        base = base_of()
        if not files.is_real_dir(base):
            return None
        assert base is not None
        root = base.resolve()
        folder = base / dirname
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        linked = not files.is_real_dir(folder) or folder.resolve().parent != root
    except (OSError, RuntimeError) as exc:  # an unwritable laya/, a full disk, a link loop
        log_once("mkdir", f"cannot open {dirname}/ ({type(exc).__name__}) — the {noun} is off")
        return None
    if linked:
        log_once("link", f"{dirname}/ is a link or outside laya/ — refusing to write")
        return None
    return root, folder


def append_rotating(
    folder: Path,
    surface: str,
    row: Mapping[str, Any],
    *,
    rotate_bytes: int,
    keep_segments: int,
    clock: Callable[[], datetime],
    forget: Callable[[Path], None],
) -> None:
    """Append one row to `<surface>.jsonl` (0600), first rotating a file at `rotate_bytes` to
    `<surface>-<YYYYMMDDTHHMMSS>.jsonl` and pruning all but the newest `keep_segments` segments.
    The caller holds its own lock; `forget` drops a pruned segment from the caller's read cache."""
    line = _line(row)  # a ValueError (NaN/Infinity) here, before anything moves
    path = folder / f"{surface}.jsonl"
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        size = 0
    if size >= rotate_bytes:
        stamp = f"{clock():%Y%m%dT%H%M%S}"
        target = folder / f"{surface}-{stamp}.jsonl"
        n = 0
        while os.path.lexists(target):
            n += 1
            target = folder / f"{surface}-{stamp}-{n}.jsonl"
        os.rename(path, target)
        for old in segments(folder, surface)[:-keep_segments]:
            try:
                os.unlink(old)
            except OSError:
                continue
            forget(old)  # no reader asks for a pruned segment again
    files.append_line(path, line)


def _stamp_at(stamp: str) -> str:
    """`YYYYMMDDTHHMMSS` (a segment's or a decision id's second) as a row `at`."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"


def _segment_at(path: Path) -> str:
    """A segment's rotation time as a row `at`: every row inside it is no later."""
    return _stamp_at(SEGMENT.fullmatch(path.name)["stamp"])  # type: ignore[index]


class Ledger:
    """The one writer. `base` is read per call, so a params folder created later turns the ledger on."""

    def __init__(
        self,
        base: Callable[[], Path | None],
        clock: Callable[[], datetime] = utc_now,
        log: Callable[[str], None] = lambda _message: None,
        *,
        rotate_bytes: int = ROTATE_BYTES,
        keep_segments: int = KEEP_SEGMENTS,
        index_max: int = INDEX_MAX,
        load: Callable[[], float | None] = load_per_core,
    ) -> None:
        self._base, self._clock, self._load = base, clock, load
        #: The laya-serve start the answers below came from, and the models that have answered since (`_first_answer`).
        self._spawn: int | None = None
        self._answered: set[str] = set()
        self._rotate_bytes, self._keep_segments, self._index_max = rotate_bytes, keep_segments, index_max
        self._lock = threading.Lock()
        #: A cache of recent decisions, never the truth: a miss reads the disk (`_lookup`).
        self._index: OrderedDict[str, tuple[str, set[str]]] = OrderedDict()
        self._log_once = LogOnce(log, "ledger")

    def _folder(self) -> tuple[Path, Path] | None:
        """(resolved `laya/`, `ledger/`), creating `ledger/` 0700; None while the ledger is off. Never raises."""
        return private_folder(self._base, LEDGER_DIRNAME, "ledger", self._log_once)

    def _append(self, folder: Path, surface: str, row: Mapping[str, Any]) -> None:
        append_rotating(
            folder,
            surface,
            row,
            rotate_bytes=self._rotate_bytes,
            keep_segments=self._keep_segments,
            clock=self._clock,
            forget=forget_segment,
        )

    def _remember(self, decision_id: str, surface: str, actions: Iterable[str] = ()) -> tuple[str, set[str]]:
        entry = self._index.get(decision_id)
        if entry is None:
            entry = self._index[decision_id] = (surface, set(actions))
        else:
            entry[1].update(actions)
        while len(self._index) > self._index_max:
            self._index.popitem(last=False)
        return entry

    def _first_answer(self, spawn: int | None, model: Any) -> bool | None:
        """`cold`: whether this is `model`'s first answer since laya-serve start number `spawn` (`LayaRuntime.spawn`),
        or None when the caller named no start. Weights are loaded before laya-serve reads `ready`, so what a first
        answer pays is the device warming up (on MPS, the kernels it compiles for that model). Counted whether or not
        the row is then written: a ledger that was off for the first answer must not call the second one cold."""
        if spawn is None or not isinstance(model, str):
            return None
        with self._lock:
            if spawn != self._spawn:
                self._spawn, self._answered = spawn, set()
            cold = model not in self._answered
            self._answered.add(model)
        return cold

    def record_decision(
        self,
        tag: Mapping[str, Any] | None,
        cleaned: Mapping[str, Any],
        payload: Mapping[str, Any],
        model: str,
        model_id: str | None,
        checkpoint_abs: str | Path | None,
        ms: float,
        spawn: int | None = None,
    ) -> str | None:
        """Append one decision row; its `decision_id`, or None (off, unwritable, or an unhashable state). Never raises."""
        try:
            cold = self._first_answer(spawn, model)
            folder = self._folder()
            if folder is None:
                return None
            root, ledger_dir = folder
            if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
                self._log_once("model", "a decision named no served model — not recorded")
                return None
            surface = tag["surface"] if tag else "untagged"
            state = cleaned["state"]
            state_hash = rows.state_digest(state)
            text: str | None = None
            truncated = False
            if surface in KEEP_TEXT:
                text = state if isinstance(state, str) else rows.canonical_json(state)
                if utf16_len(text) > STATE_MAX:  # the tail, as fold_closing_text keeps it
                    text, truncated = files.utf16_tail(text, STATE_MAX), True
            now = self._clock()
            decision_id = f"{now:%Y%m%dT%H%M%S}-{secrets.token_hex(6)}"
            row: dict[str, Any] = {
                "kind": "decision",
                "v": VERSION,
                "decision_id": decision_id,
                "at": rows.utc_stamp(now),
                "surface": surface,
                "model": model,
                "model_id": model_id if isinstance(model_id, str) and LAYA_LOCAL_MODEL_ID.fullmatch(model_id) else None,
                "checkpoint_path": _relative_checkpoint(root, checkpoint_abs),
                "state_hash": state_hash,
                "state": text,
                "state_truncated": truncated,
                "decisions": decisions_from(cleaned["questions"], payload),
                "ms": max(0, int(ms)) if is_number(ms) else 0,
            }
            for field in COPIED_TAG_FIELDS:
                if tag and field in tag:
                    row[field] = tag[field]
            load = self._load()
            if is_number(load) and load >= 0:
                row["load"] = round(float(load), 3)
            if cold is not None:
                row["cold"] = cold
            with self._lock:
                self._append(ledger_dir, surface, row)
                self._remember(decision_id, surface)
            return decision_id
        except ValueError:
            self._log_once("value", "a state or answer JSON cannot carry (NaN/Infinity) — not recorded")
        except (OSError, files.SourceError) as exc:
            self._log_once("os", f"cannot write ({type(exc).__name__}) — decisions go unrecorded")
        except Exception as exc:  # noqa: BLE001 — a ledger bug must never cost the caller its answer
            self._log_once("bug", f"not recorded ({type(exc).__name__})")
        return None

    def admits(self, surface: str) -> bool:
        """May a decision be recorded under `surface`: one the ledger already holds, or a new one under the cap
        (`MAX_SURFACES`). True while the ledger is off: nothing is written then, so there is nothing to bound."""
        try:
            base = self._base()
        except Exception:  # noqa: BLE001 — a store that cannot be resolved records nothing
            return True
        held = surfaces(base)
        return surface in held or len(held) < MAX_SURFACES

    def _lookup(self, base: Path, decision_id: str) -> tuple[str, set[str]] | None:
        """(surface, recorded actions) of a decision the index no longer holds, read from disk.

        A bridge restart, or thousands of relay decisions since, must not orphan a pending reaction.
        Only rows at or after the id's own second are read (a decision's `at` is its id's time, and
        its reactions come later), so segments rotated before it are skipped by their stamp and a
        miss costs one pass over what came since: cached segments and an incremental tail.
        """
        since = _stamp_at(decision_id)
        for surface in surfaces(base):
            seen, actions = False, set()
            for row in iter_rows(base, surface, since=since):
                if row["decision_id"] != decision_id:
                    continue
                if row["kind"] == "decision":
                    seen = True
                elif row.get("action") in ACTIONS:
                    actions.add(row["action"])
            if seen:
                return surface, actions
        return None

    def judged(self, surface: str, conversation: object, state_hash: object) -> bool | None:
        """Does a `surface` decision row of this conversation hash to `state_hash` (a row's own
        `rows.state_digest`)? The agent rack asks before judging a turn end its bridge missed, so one turn
        is never judged twice: owned by tldrack `open_work.py` `ledger_judged` — see the note there.
        None while the ledger is off or for a malformed question; never raises. Reads the window `react` does,
        which is every row the ledger keeps (`KEEP_SEGMENTS`)."""
        if (
            surface not in RACK_SURFACES
            or not isinstance(conversation, str)
            or not CONVERSATION.fullmatch(conversation)
            or not isinstance(state_hash, str)
            or not STATE_HASH.fullmatch(state_hash)
        ):
            return None
        try:
            base = self._base()
            if base is None:
                return None
            return any(
                row["kind"] == "decision" and row.get("conversation") == conversation and row.get("state_hash") == state_hash
                for row in iter_rows(base, surface)
            )
        except Exception:  # noqa: BLE001 — an unsure answer judges nothing, which is the safe side
            return None

    def surface_of(self, decision_id: str) -> tuple[str, str | None]:
        """`('ok', surface)` for a decision this ledger holds, else `('unknown', None)` or `('off', None)`.
        The index first, then the disk window `react` reads (`_lookup`)."""
        folder = self._folder()
        if folder is None:
            return "off", None
        with self._lock:
            entry = self._index.get(decision_id)
        if entry is not None:
            return "ok", entry[0]
        found = self._lookup(folder[0], decision_id)
        if found is None:
            return "unknown", None
        with self._lock:
            self._remember(decision_id, *found)
        return "ok", found[0]

    def react(self, decision_id: str, action: str) -> tuple[str, bool]:
        """`('ok' | 'unknown' | 'off', duplicate)`. A repeated (decision, action) writes nothing."""
        folder = self._folder()
        if folder is None:
            return "off", False
        with self._lock:
            held = decision_id in self._index
        # Outside the lock, so a disk pass never stalls a /decide; the index is re-checked below.
        found = None if held else self._lookup(folder[0], decision_id)
        with self._lock:
            entry = self._index.get(decision_id)
            if entry is None:
                if held:  # evicted between the two locks: rare enough to read under this one
                    found = self._lookup(folder[0], decision_id)
                if found is None:
                    return "unknown", False
                entry = self._remember(decision_id, *found)
            surface, actions = entry
            if action in actions:
                return "ok", True
            row = {
                "kind": "reaction",
                "v": VERSION,
                "decision_id": decision_id,
                "at": rows.utc_stamp(self._clock()),
                "surface": surface,
                "action": action,
            }
            try:
                self._append(folder[1], surface, row)
            except (OSError, files.SourceError) as exc:
                self._log_once("os-react", f"cannot write a reaction ({type(exc).__name__})")
                return "off", False
            actions.add(action)
            return "ok", False


def _relative_checkpoint(root: Path, checkpoint_abs: str | Path | None) -> str | None:
    """The checkpoint relative to `laya/` (disk only; HTTP carries the basename), or None for a built-in."""
    if checkpoint_abs is None:
        return None
    try:
        relative = Path(checkpoint_abs).resolve().relative_to(root).as_posix()
    except (ValueError, OSError, RuntimeError):
        return None
    ok = len(relative) <= CHECKPOINT_PATH_MAX and PARAMS_RELATIVE_PATH.fullmatch(relative)
    return relative if ok else None


# ── readers ───────────────────────────────────────────────────────────────────

_SEGMENTS = files.StampedCache()


def forget_segment(path: Path) -> None:
    """Drop a pruned segment from the one segment cache every `iter_folder_rows` reader shares."""
    _SEGMENTS.forget(path)


def is_surface(value: object) -> bool:
    return isinstance(value, str) and SURFACE_NAME.fullmatch(value) is not None


def surfaces(base: Path | None) -> tuple[str, ...]:
    """Every surface this ledger holds a file of (active or rotated), sorted, the rack's own and `untagged` always
    among them. Never raises: a folder that cannot be listed reads as the always-present ones."""
    found = {*RACK_SURFACES, UNTAGGED}
    try:
        names = os.listdir(base / LEDGER_DIRNAME) if base is not None else []
    except OSError:
        names = []
    for name in names:
        rotated = SEGMENT.fullmatch(name)
        surface = rotated["surface"] if rotated is not None else name[: -len(".jsonl")] if name.endswith(".jsonl") else ""
        if is_surface(surface):
            found.add(surface)
    return tuple(sorted(found))


def _row_ok(row: Mapping[str, Any]) -> bool:
    return (
        row.get("kind") in ("decision", "reaction")
        and isinstance(row.get("decision_id"), str)
        and DECISION_ID.fullmatch(row["decision_id"]) is not None
        and is_surface(row.get("surface"))
        and isinstance(row.get("at"), str)
    )


def _parse_rows(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    return [row for row in parsed if _row_ok(row)]


def iter_rows(base: Path, surface: str, since: str | None = None, max_bytes: int = READ_MAX_BYTES) -> Iterator[dict]:
    """Every readable row of `surface`, oldest first: the newest rotated segments that fit in
    `max_bytes` with the active file, then the active file. `since` (a row `at`) skips older rows.
    """
    yield from iter_folder_rows(base / LEDGER_DIRNAME, surface, _parse_rows, _row_ok, since, max_bytes)


def iter_folder_rows(
    folder: Path,
    surface: str,
    parse_rows: Callable[[bytes], list[dict]],
    row_ok: Callable[[Mapping[str, Any]], bool],
    since: str | None = None,
    max_bytes: int = READ_MAX_BYTES,
) -> Iterator[dict]:
    """`iter_rows` over any folder `append_rotating` writes: `parse_rows` (module-level, so the
    segment cache keys on it) keeps a segment's readable rows, `row_ok` the active file's."""
    if not files.is_real_dir(folder):
        return
    active = folder / f"{surface}.jsonl"
    stamp = files.stamp_of(active)
    budget = max_bytes - (stamp[2] if stamp else 0)
    chosen: list[Path] = []
    for segment in reversed(segments(folder, surface)):
        if since is not None and _segment_at(segment) < since:
            break
        segment_stamp = files.stamp_of(segment)
        if segment_stamp is None or segment_stamp[2] > budget:
            break
        budget -= segment_stamp[2]
        chosen.append(segment)
    for segment in reversed(chosen):
        code, parsed = _SEGMENTS.get(segment, max_bytes, parse_rows)
        for row in parsed if code == files.OK else ():
            if since is None or row["at"] >= since:
                yield row
    _code, parsed, _bad = files.shared_tail(active, max_bytes).read()
    for row in parsed:
        if row_ok(row) and (since is None or row["at"] >= since):
            yield row


def fold(rows_in: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """`{decision_id: {"decision": row | None, "actions": {action: first at}}}`, in first-seen order."""
    out: dict[str, dict] = {}
    for row in rows_in:
        entry = out.setdefault(row["decision_id"], {"decision": None, "actions": {}})
        if row["kind"] == "decision":
            entry["decision"] = row
        elif row.get("action") in ACTIONS:
            entry["actions"].setdefault(row["action"], row["at"])
    return out


def active_code(base: Path, surface: str) -> str:
    """The active file's source code (ok | missing | too_large | refused), from the shared reader."""
    return files.shared_tail(base / LEDGER_DIRNAME / f"{surface}.jsonl", READ_MAX_BYTES).read()[0]


def summary(base: Path | None, days: int, now: datetime | None = None) -> dict[str, dict]:
    """`{surface: {decisions, shown, dismissed, accepted, converted, expired, last_at}}` over the
    decisions made in the last `days` (their reactions counted whenever they came). Surfaces with
    no decision in the window are left out.
    """
    out: dict[str, dict] = {}
    if base is None:
        return out
    since = rows.utc_stamp((now or utc_now()) - timedelta(days=days))
    for surface in surfaces(base):
        counts = {"decisions": 0, **dict.fromkeys(ACTIONS, 0), "last_at": None}
        for entry in fold(iter_rows(base, surface, since=since)).values():
            decision = entry["decision"]
            if decision is None:
                continue
            counts["decisions"] += 1
            for action in entry["actions"]:
                counts[action] += 1
            if counts["last_at"] is None or decision["at"] > counts["last_at"]:
                counts["last_at"] = decision["at"]
        if counts["decisions"]:
            out[surface] = counts
    return out
