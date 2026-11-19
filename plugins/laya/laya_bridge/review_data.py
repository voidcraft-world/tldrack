"""THE shared label rules, and the REVIEW deck's strata, cards and stats, computed live.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

The label rules are imported by laya-lab (direction laya-lab → laya_bridge only), so the plugin's
`sweep_ready`, the `/lab` gates, OPS `candidates` and the CLI's exit 3 can never disagree:
`texts`, `golden_keys`, `clean_label_rows`, `sweep_readiness`, `promotion_candidates`. Every
function that needs the served checkpoint takes `loaded`: what laya-serve LOADED (the plugin passes
`runtime.loaded()`), or None to read the registry (laya-lab). Which operator label trains, which is promoted and which
is held in reserve: owned by `label_budget.py` — see the note there.

The deck: every reviewable key sits in exactly one stratum, taken in this order.
- `sample`: Sonnet keys with `bucket(key, 'review-sample') < sample_percent`; the only basis for κ;
  held-out rows first.
- `live_sample`: live verdicts (any key with a ledger decision) with `bucket(key, 'review-live') <
  live_percent`, dealt in a second hash's order, so the deck grades it in an order p never sets. The one
  uniform sample of what the rack judged live: precision per landing class and recall per audience are measured
  on it alone, through P1-20's landing record joined on `decision_id` (`live_rates` owns why). Membership is the
  key's hash, so a member stays one whatever its p does.
- `challenger`: live verdicts where the surface's @challenger would have drawn the other ⚠️ answer. laya-lab's
  nightly shadow pass writes `challenger_p` beside the served p (`lab_files.read_shadow`); a key is here when the
  shadow row of its NEWEST decision, served by the checkpoint serving now, puts the two on different sides of
  their own ⚠️ cuts (`shadow_rivals`). Its own lane and weight, never folded into `disagree`: that one is Laya
  against Sonnet on a Sonnet key, this one is two checkpoints on a text the rack judged live. Taken from what
  would be `suspect`, `disagree`, `uncertain`, `flagged`, `recent` or `rest`, never from a sample. Empty without
  a @challenger. `challenger_sides` counts which side each grade took: what a promotion reads.
- `suspect`: Sonnet labels a text-only surrogate thinks are wrong. laya-lab SCORE writes each Sonnet key's
  out-of-fold self-confidence under a 5-fold TF-IDF + logistic regression trained on Sonnet's own labels
  (`teacher_open`, `teacher_conf` on its scores row); `suspect_ranked` keeps the confident-learning
  off-diagonal (the other class at or over that class's own mean self-confidence), least confident first.
- `disagree`: other Sonnet keys whose CURRENT scores row (scored by the served checkpoint) disagrees.
- `uncertain`: the turns one nudge from another mark. p (the card's own: the live verdict on a ledger text,
  the SCORE file's on a Sonnet one) within `UNCERTAIN_WITHIN` of a cut by `edge_of`, taken from what would
  be `flagged`, `recent` or `rest`, never from a sample. Held-out texts first (only they make clean labels),
  then while a class is short of `rows.SWEEP_MIN_PER_CLASS` clean labels, the cut that class sits behind
  (`class_deficit`), then nearest first.
- `golden`: misses in the newest golden report of the served model, then every public case still
  provisional (`lab_files.PROVISIONAL_TAG`: AGREE is its sign-off); a case whose text the operator
  already answered is left out. A case in a batch is dealt only while its batch sign-off asks for it, missed
  or not: the sample, or the whole batch once rejected (`lab_files.BATCH_TAG`).
- `flagged` / `unflagged`: Laya's own ledger verdicts on texts Sonnet never labelled, split on p ≥ the
  row's threshold; `unflagged` keeps `bucket(state_key, 'review-unflagged') < unflagged_percent`,
  per text, so membership never moves when a text is judged again.
- `recent`: the other 80% of `unflagged`. Never dealt and never counted in `to_review`: it exists so
  every live verdict on the RECENT list (`recent_rows`) can be answered, not only the sampled ones.
- `rest`: every other Sonnet key, the backlog.
Answered or skipped keys leave the queue. The queue is a weighted round robin (`ReviewSurface.weights`).

THE BAND DEAL. A filter across the strata, never a stratum of its own: the strata stay a partition, which the κ
sample and the deal weights rest on. `queue(band=...)` keeps the cards whose LIVE verdict (`Item.band`, a ledger p
at the cuts it was judged at) falls in one of `BANDS`, from every lane but `BAND_NEVER`, in the UNCERTAIN lane's
order (`_near_order`). The 💬 band is the reason: about a fifth of live verdicts land between the two cuts, and one
in the middle of it is near no cut, so `uncertain` never deals it and it mostly sits in `recent`, which nothing
deals. A grade on those rows is what a re-sweep and a retrain learn the band from. Each card still records its own
stratum, so every per-stratum count reads as before.

A WAVE is adjacent turn ends of one rack session (its conversation, else its rack name) that drew the same
mark, each within `WAVE_GAP_S` of the last: an orchestrator ending a turn per child report. Never grouped by
text: two turn ends of one wave share almost no words (Jaccard median 0.012). The deck deals a wave as ONE
card (`Snapshot.waves`, at most `WAVE_MAX` texts): its lead is the member in the most valuable stratum, the
others ride on it and leave their lanes, and one press records the same verdict on each (one `wave_id`, so
one UNDO). The uniform samples (`sample`, `live_sample`, `unflagged`) and `challenger` never join one, as lead or
rider: their grades estimate something (κ, precision, recall's unflagged side, the side taken), so each is one
text, one look.

THE RACK'S DOOR. The ⚠️'s own menu on the agent rack answers the verdict it is drawn from (`review.py` `rack`):
✗ NOT OPEN records a flip to DONE, ✓ STILL OPEN an agree, each against that decision's own p and cut. Such a row
records `RACK_STRATUM`: a door, never a lane, so no key is ever placed in it and nothing deals it. The text is
then answered like any other (the deck and RECENT show the grade, and UNDO takes it back).

THE FOUR-WAY GRADE. On a surface with `ReviewSurface.needs` an OPEN answer says which OPEN: RUNNING (the agent
was waiting on its own work) or NEEDS YOU; a DONE answer may say READ (📖: the agent owes nothing, but its answer
owes the operator a read, where a bare DONE is closed unread). That is four classes (`grade_of`), and the challenger
can only be evaluated once each holds `needs_min_per_class` grades. A DONE recorded before READ existed
(`READ_SINCE`) is no class: it could not say which DONE it was. Until both OPEN halves hold it the deck leans
toward the half shortest of it, whatever DONE holds (`three_way`, its `lean`): a live card whose verdict LANDED the
way that half does (`needs_hint`, from `landing.py`'s record) leads `uncertain` and `flagged`, and `flagged` deals
up to `NEEDS_BOOST_WEIGHT` of them a round instead of its one. DONE and READ have no hint and no lean of their own:
a short DONE is the open/done deficit's to chase (`class_deficit`), and READ's candidates are the 📖 rows on the
RECENT list. The hint orders cards and nothing else: it is never a label,
and no card shows it. The samples are never reordered: their order is a hash's, so no signal sets which of them is
graded first.

THE BLIND RETEST. About 1 graded text in 10 (`retest_due`) is shown again once its label is `retest_after_days`
old, with nothing but its text: no reference, no Laya score, no earlier answer (`retest_card`). The second answer
is a `retest` row (`review_log.py` owns why it is its own op), paired with the label it re-tests by
`intra_rater` alone: the operator's agreement with themselves, per answer. That κ is the ceiling of every Laya
metric measured against these labels (the Saturation Trap: annotators barely agree on when to interrupt).

The RECENT list is the other way in: the newest live decisions in time order, each with the mark the
rack drew and the band it falls in at today's cuts, so the operator grades what they actually saw.
Its EDGE order ranks every live verdict in the ledger by how close p sits to a cut instead (`edge_of`):
the turns one nudge from another mark, whose labels are the ones that move a cut or a fine-tune.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_bridge import agreement, files, label_budget, lab_files, landing, ledger, paths, review_log, rows
from laya_bridge.files import clip, is_p, utf16_len, utf16_tail
from laya_bridge.review_log import OperatorFold
from laya_bridge.review_surfaces import REVIEW_SURFACES, ReviewSurface, surface_for_suite
from laya_bridge.wire import BUILTIN_MODELS

STRATA = (
    "sample",
    "live_sample",
    "challenger",
    "suspect",
    "disagree",
    "uncertain",
    "golden",
    "flagged",
    "unflagged",
    "recent",
    "rest",
)
TO_REVIEW = ("sample", "live_sample", "challenger", "suspect", "disagree", "uncertain", "golden", "flagged", "unflagged")
#: What an answer from the agent rack's ⚠️ menu records as its stratum (the module doc: THE RACK'S DOOR).
RACK_STRATUM = "rack"
#: The daily phone door (plugins/phone-link `door.py` sends it): the lanes whose next grade moves a number, counted
#: on the texts the phone's RECENT list can show (a live verdict), and how many must wait before a push is worth one.
DOOR_STRATA = ("challenger", "uncertain", "live_sample")
DOOR_MIN = 5
#: `uncertain` is p within this of a cut: two steps of the sweep grid (ui/src/laya-review/recent-model.ts EDGE_NEAR is one), so the
#: stratum outlasts a grading sprint while its order still deals the nearest first.
UNCERTAIN_WITHIN = 0.1
#: A wave: turn ends at most this far apart, and at most this many texts on one card (a longer run deals as more cards).
WAVE_GAP_S = 20 * 60
WAVE_MAX = 12
#: Each rider's text on its lead's card: enough to see it says the same, not the whole hand-off.
WAVE_TEXT_MAX = 600
#: Which member of a wave leads its card: the first stratum here it has, so a wave is dealt where it counts most.
WAVE_LEAD = ("uncertain", "suspect", "disagree", "flagged", "rest", "recent")
#: Strata whose grades estimate something, so a member is graded on its own and never joins a wave: the uniform samples
#: (`unflagged` is recall's unflagged side), the challenger's disagreements (each grade takes a side) and the golden misses.
WAVE_NEVER = ("sample", "live_sample", "challenger", "unflagged", "golden")
#: The four-way grade's classes, in the order a readout prints them (`grade_of`). TS twin: REVIEW_GRADES.
GRADES = ("done", "read", "running", "you")
#: When READ became a grade: a DONE before it is no class (the module doc: THE FOUR-WAY GRADE). TS twin:
#: schemas/src/laya-review.ts REVIEW_READ_SINCE, pinned by tests/test_contract.py.
READ_SINCE = "2026-11-19T00:00:00Z"
#: The live lanes the needs lean reorders (the module doc: THE FOUR-WAY GRADE), and how many hinted cards the first
#: of them may deal a round (`queue`): `uncertain`'s own weight.
NEEDS_LEAN_LANES = ("flagged", "uncertain")
NEEDS_BOOST_LANE = "flagged"
NEEDS_BOOST_WEIGHT = 3
#: One retest card per this many cards a deck page asks for: the deal's "1 in 10" (`retest_cards`).
RETEST_DEAL_EVERY = 10
#: The RECENT list's default and largest page.
RECENT_DEFAULT = 100
RECENT_MAX = 200
#: Its two orders: `newest` (time, the default) and `edge` (closest to a cut first, `edge_of`).
RECENT_ORDERS = ("newest", "edge")
#: A RECENT page's cursor (`summary.next`, sent back as `?after=`): where the page's last row sits in its order,
#: `n~<at>~<key>` or `e~<distance>~<at>~<key>` (`recent_rows` owns why). TS twin: schemas/src/laya-review.ts
#: REVIEW_RECENT_CURSOR.
RECENT_CURSOR = re.compile(
    r"(?:n|e~(?P<distance>[01]\.[0-9]{6}))~(?P<at>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z)"
    r"~(?P<key>[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63})"
)
#: The three marks a turn end can draw on the rack (tldrack/open_work.py § Three bands).
BANDS = ("done", "reply", "open")
#: The lanes the band deal never takes from (the module doc: THE BAND DEAL): the uniform samples, whose order is a
#: hash's so no signal sets which of them is graded first. Dealt nearest a cut, a half-graded sample would lean.
BAND_NEVER = ("sample", "live_sample", "unflagged")
UNLABELLED_CAP_BYTES = 16 * 1024 * 1024
LABELS_CAP_BYTES = 8 * 1024 * 1024
CARD_TEXT_MAX = 4_000
WHY_MAX = 400
#: A golden case's written request on its card (`golden.request`): for the operator's eyes; Laya scores the closing alone.
REQUEST_MAX = 600
BY_MAX = 120
#: The window of `precision`'s reaction counts (`shown`, `dismissed`): telemetry beside the precision, never its basis.
PRECISION_WINDOW_DAYS = 7
RECALL_MIN_PER_SIDE = 5
#: Where a SHOWN ⚠️ landed (`landing.CLASSES`), or `unknown`: a decision the landing record never saw (made before the
#: record existed, or while it was off) or saw without a class. Precision splits on these (`live_rates`).
LANDING_SPLIT = (*landing.CLASSES, "unknown")
#: The classes of a ⚠️ drawn on a FREE row, in front of the operator: `held` drew ⏳ and `retired` drew nothing.
FREE_CLASSES = ("new", "repeat")
#: The row a verdict landed on, flagged or not (`landing.HELD_REASONS`, `free` when nothing held it), or `unknown`.
#: Recall splits on these.
AUDIENCES = ("free", *landing.HELD_REASONS, "unknown")


@dataclass(frozen=True)
class Served:
    """The model answering a surface: its name, its checkpoint folder (absolute; None for a built-in)."""

    name: str
    checkpoint: str | None

    @property
    def fine_tune(self) -> bool:
        return self.checkpoint is not None

    @property
    def basename(self) -> str | None:
        return Path(self.checkpoint).name if self.checkpoint else None


def served_model(base: Path, surface: str, loaded: Mapping[str, str] | None = None) -> Served:
    """The registered fine-tune for the surface, else `english`. While laya-serve runs, what it
    LOADED wins over the registry: a checkpoint registered since is not what is answering."""
    spec = REVIEW_SURFACES[surface]
    if loaded is not None:
        path = loaded.get(spec.fine_tune)
    else:
        entry = paths.registry_view(base)["models"].get(spec.fine_tune)
        path = entry["path"] if entry else None
    return Served(spec.fine_tune, path) if path else Served("english", None)


def is_laya_model(name: str) -> bool:
    """A reference `by` that is a Laya model (a built-in or a `tldrack-*` fine-tune, its legacy name too), not a
    labeller."""
    return name in BUILTIN_MODELS or paths.served_name(name).startswith(paths.FINE_TUNE_PREFIX)


def training_record(base: Path, spec: ReviewSurface, served: Served) -> tuple[frozenset[str], bool]:
    """(the keys any `<fine_tune>-*` checkpoint trained on, whether the SERVED one's record is missing).

    The keys stay the union: a key some checkpoint saw is never called held out. But only the served
    checkpoint's own record decides "unknown": laya-lab trains every checkpoint from the base model,
    so an older, unrecorded one never taught the served one anything. A served folder laya-lab did
    not name is unknown too, since its record was never read.
    """
    trained, without = rows.trained_keys(base, spec.fine_tune)
    name = served.basename or ""
    return trained, name in without or rows.checkpoint_name(spec.fine_tune).fullmatch(name) is None


# ── sources ───────────────────────────────────────────────────────────────────

_SOURCES = files.StampedCache()
_PARSERS: dict[str, Any] = {}
_MEMO_LOCK = threading.Lock()


def _unlabelled_parser(spec: ReviewSurface):
    """One parser per surface (the cache keys on the function), keyed by `rows.state_key(text)`; a text the
    surface does not admit (`ReviewSurface.admits`) is not read at all."""
    parser = _PARSERS.get(spec.name)
    if parser is None:

        def parse(data: bytes) -> dict[str, str]:
            parsed, _bad = files.parse_jsonl(data)
            found: dict[str, str] = {}
            for row in parsed:
                text = row.get(spec.text_field)
                if isinstance(text, str) and text.strip() and spec.admits(text):
                    found[rows.state_key(text)] = text
            return found

        parser = _PARSERS[spec.name] = parse
    return parser


def _parse_labels(data: bytes) -> dict[str, dict]:
    """laya-lab's Sonnet labels, `{key, open, why, by}`; a re-label of a key wins."""
    parsed, _bad = files.parse_jsonl(data)
    found: dict[str, dict] = {}
    for row in parsed:
        key = row.get("key")
        if isinstance(key, str) and lab_files.KEY.fullmatch(key) and isinstance(row.get("open"), bool):
            by = row.get("by") if isinstance(row.get("by"), str) and row["by"].strip() else "sonnet"
            why = row.get("why") if isinstance(row.get("why"), str) else ""
            found[key] = {"open": row["open"], "why": clip(why, WHY_MAX), "by": clip(by, BY_MAX)}
    return found


def unlabelled_texts(base: Path, spec: ReviewSurface) -> tuple[str, dict[str, str]]:
    code, found = _SOURCES.get(paths.data_file(base, spec.stem, "unlabelled"), UNLABELLED_CAP_BYTES, _unlabelled_parser(spec))
    return code, found or {}


def sonnet_labels(base: Path, spec: ReviewSurface) -> tuple[str, dict[str, dict]]:
    code, found = _SOURCES.get(paths.data_file(base, spec.stem, "labels"), LABELS_CAP_BYTES, _parse_labels)
    return code, found or {}


def _decision_key(decision: Mapping[str, Any] | None, surface: str) -> str | None:
    """A ledger decision's text key (`state_hash[:16]`, `rows.state_key`'s), or None for a row of another surface or
    without a whole hash."""
    if decision is None or decision.get("surface") != surface:
        return None
    digest = decision.get("state_hash")
    return digest[:16] if isinstance(digest, str) and len(digest) == 64 else None


def _ledger_decisions(base: Path, surface: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """(newest decision entry per state_key, every folded entry): `{key: {"decision", "actions"}}`."""
    folded = ledger.fold(ledger.iter_rows(base, surface))
    newest: dict[str, dict] = {}
    for entry in folded.values():
        decision = entry["decision"]
        key = _decision_key(decision, surface)
        if key is None:
            continue
        held = newest.get(key)
        if held is None or decision["at"] >= held["decision"]["at"]:
            newest[key] = entry
    return newest, folded


def ledger_keys(base: Path, surface: str) -> frozenset[str]:
    """The keys (`state_hash[:16]`) of `surface`'s ledger decisions, text kept or not: the ones laya-lab scores."""
    return frozenset(_ledger_decisions(base, surface)[0])


_TEXTS: dict[tuple[str, str], tuple[tuple, dict[str, str]]] = {}


def _merge_texts(spec: ReviewSurface, newest: Mapping[str, dict], unlabelled: Mapping[str, str]) -> dict[str, str]:
    """The ledger's kept states the surface admits, then the export (already admitted by its parser) over them."""
    found = {
        key: state
        for key, entry in newest.items()
        if isinstance(state := entry["decision"].get("state"), str) and state and spec.admits(state)
    }
    found.update(unlabelled)
    return found


def texts(base: Path, surface: str) -> dict[str, str]:
    """`{key: text}`: exported texts ∪ the ledger's kept states (key = state_hash[:16]); exported wins. A text the
    surface does not admit (`ReviewSurface.admits`: a cut-off notice, never a hand-off) is in neither.

    Memoised on the stamps of its sources, so a polled stats call does not re-fold the ledger.
    """
    spec = REVIEW_SURFACES[surface]
    ledger_dir = base / paths.LEDGER_DIRNAME
    stamps = tuple(
        files.stamp_of(path)
        for path in (paths.data_file(base, spec.stem, "unlabelled"), ledger_dir, ledger_dir / f"{surface}.jsonl")
    )
    with _MEMO_LOCK:
        held = _TEXTS.get((str(base), surface))
    if held is not None and held[0] == stamps:
        return held[1]
    found = _merge_texts(spec, _ledger_decisions(base, surface)[0], unlabelled_texts(base, spec)[1])
    with _MEMO_LOCK:
        _TEXTS[(str(base), surface)] = (stamps, found)
    return found


def golden_suites(base: Path, suite: str) -> tuple[list[dict], str, list[dict], str]:
    """(public cases, code, private cases, code)."""
    public, public_code = lab_files.read_golden_suite(paths.PUBLIC_GOLDEN_DIR / f"{suite}.jsonl")
    private, private_code = lab_files.read_golden_suite(paths.private_golden_path(base, suite))
    return public, public_code, private, private_code


def _case_keys(case: Mapping[str, Any]) -> set[str]:
    keys = {rows.state_key(case["state"]["hand_off"])}
    if isinstance(case.get("key"), str):
        keys.add(case["key"])
    return keys


def golden_keys(base: Path, suite: str) -> frozenset[str]:
    """`state_key(state.hand_off)` of every public and private golden case: never training, never a sweep."""
    public, _pc, private, _vc = golden_suites(base, suite)
    found: set[str] = set()
    for case in (*public, *private):
        found |= _case_keys(case)
    return frozenset(found)


# ── the shared label rules ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CleanRows:
    rows: list[tuple[str, bool, str]]
    excluded: dict[str, int]
    reason: str | None
    #: The keys of `rows` in the label budget's reserve: the rows no retrain and no PROMOTE takes (`label_budget.py`).
    reserve: frozenset[str] = frozenset()


def clean_label_rows(
    base: Path,
    surface: str,
    labels: Mapping[str, bool],
    *,
    loaded: Mapping[str, str] | None = None,
    fold: OperatorFold | None = None,
) -> CleanRows:
    """The labels a sweep may use: never a golden case, never a text without text, never a row the
    served fine-tune trained on. `reason` says why no sweep can run at all. `fold` is the caller's read of the
    operator log (the label budget reads which keys the operator labelled); without it, it is read here."""
    spec = REVIEW_SURFACES[surface]
    served = served_model(base, surface, loaded)
    found_texts = texts(base, surface)
    golden = golden_keys(base, spec.golden_suite)
    reason = None
    trained: frozenset[str] = frozenset()
    if not served.fine_tune:
        reason = f"no fine-tune registered for {surface}"
    else:
        trained, missing = training_record(base, spec, served)
        if missing:
            reason = clip(f"checkpoint {served.basename} has no training record — retrain it", 160)
    excluded = {"trained": 0, "golden": 0, "no_text": 0}
    kept: list[tuple[str, bool, str]] = []
    for key in sorted(labels):
        if key.startswith("golden:") or key in golden:
            excluded["golden"] += 1
        elif key not in found_texts:
            excluded["no_text"] += 1
        elif key in trained:
            excluded["trained"] += 1
        else:
            kept.append((key, bool(labels[key]), found_texts[key]))
    current = _fold_for(base, surface, fold)
    # The budget's training record is the same union `training_record` just read, whatever is served.
    budget = label_budget.budget_of(current.labels, trained) if served.fine_tune else label_budget.read(base, surface, current)
    return CleanRows(kept, excluded, reason, frozenset(key for key, _open, _text in kept if budget.is_reserve(key)))


def _fold_for(base: Path, surface: str, fold: OperatorFold | None) -> OperatorFold:
    return fold if fold is not None else review_log.read_fold(base, surface)[1]


def sweep_readiness(
    base: Path, surface: str, fold: OperatorFold | None = None, *, loaded: Mapping[str, str] | None = None
) -> dict:
    """`reviewStatsSchema.sweep_ready` over the operator's live labels on clean rows. `reserve` counts the ones the
    label budget holds back from training and from golden: what is still clean after the next retrain."""
    current = _fold_for(base, surface, fold)
    labels = {key: row["open"] for key, row in current.labels.items()}
    clean = clean_label_rows(base, surface, labels, loaded=loaded, fold=current)
    n = len(clean.rows)
    n_open = sum(1 for _key, is_open, _text in clean.rows if is_open)
    n_done = n - n_open
    ready = clean.reason is None and n >= rows.SWEEP_MIN_ROWS and min(n_open, n_done) >= rows.SWEEP_MIN_PER_CLASS
    reason = clean.reason
    if reason is None and not ready:
        reason = (
            f"{n} clean labels ({n_open} open, {n_done} done): a sweep needs {rows.SWEEP_MIN_ROWS}, "
            f"with {rows.SWEEP_MIN_PER_CLASS} of each"
        )
    return {
        "rows": n,
        "open": n_open,
        "done": n_done,
        "ready": ready,
        "reason": reason,
        "reserve": label_budget.counts(clean.reserve, labels),
    }


def first_fine_tune(base: Path | None) -> dict:
    """How far each review surface is from a first fine-tune of its own: `{surface: {labels, open, done, need,
    need_per_class, ready}}`, from the operator's live labels. The bar is the retrain's own (`rows.SWEEP_MIN_ROWS`
    with `rows.SWEEP_MIN_PER_CLASS` of each): below it there is nothing to hold out, so nothing can show a fine-tune
    beat the base. Never raises: a surface whose log cannot be read counts no labels."""
    out: dict[str, dict] = {}
    for name in REVIEW_SURFACES:
        n_open = n_done = 0
        try:
            if base is not None and files.is_real_dir(base):
                labels = review_log.read_fold(base, name)[1].labels
                n_open = sum(1 for row in labels.values() if row["open"])
                n_done = len(labels) - n_open
        except Exception:  # noqa: BLE001 — a status read never fails on one unreadable log
            n_open = n_done = 0
        out[name] = {
            "labels": n_open + n_done,
            "open": n_open,
            "done": n_done,
            "need": rows.SWEEP_MIN_ROWS,
            "need_per_class": rows.SWEEP_MIN_PER_CLASS,
            "ready": n_open + n_done >= rows.SWEEP_MIN_ROWS and min(n_open, n_done) >= rows.SWEEP_MIN_PER_CLASS,
        }
    return out


def class_deficit(readiness: Mapping[str, Any]) -> str | None:
    """The class a sweep is shortest of, `open` or `done`, or None once both have `SWEEP_MIN_PER_CLASS` clean labels.
    A tie goes to `open`: the rarer class in every grading so far, and the ⚠️ whose false alarm costs most."""
    short_open = rows.SWEEP_MIN_PER_CLASS - readiness["open"]
    short_done = rows.SWEEP_MIN_PER_CLASS - readiness["done"]
    if max(short_open, short_done) <= 0:
        return None
    return "open" if short_open >= short_done else "done"


def waiting_flips(base: Path, suite: str, fold: OperatorFold | None = None) -> dict[str, list[str]]:
    """The keys the operator FLIPPED that have text and are not golden yet, sorted, by the half the label budget
    deals each (`label_budget.HALVES`). PROMOTE offers the TRAIN half and never the reserve: `label_budget.py` owns
    why, and why that replaced the keep-ten-per-class guard."""
    found: dict[str, list[str]] = {half: [] for half in label_budget.HALVES}
    spec = surface_for_suite(suite)
    if spec is None:
        return found
    current = _fold_for(base, spec.name, fold)
    found_texts = texts(base, spec.name)
    golden = golden_keys(base, suite)
    budget = label_budget.read(base, spec.name, current)
    for key in sorted(current.labels):
        if current.labels[key]["answer"] == "flip" and not key.startswith("golden:") and key in found_texts and key not in golden:
            found[budget.half(key)].append(key)
    return found


def promotion_candidates(base: Path, suite: str, fold: OperatorFold | None = None) -> list[str]:
    """What PROMOTE would add, sorted: the waiting flips of the label budget's TRAIN half."""
    return waiting_flips(base, suite, fold)[label_budget.TRAIN]


def reserved_flips(base: Path, suite: str, fold: OperatorFold | None = None) -> list[str]:
    """The waiting flips PROMOTE never offers: the reserve's. OPS counts them beside `candidates`, so a flip that
    never becomes a golden case is not a mystery."""
    return waiting_flips(base, suite, fold)[label_budget.RESERVE]


# ── the deck ──────────────────────────────────────────────────────────────────


@dataclass
class Item:
    key: str
    stratum: str
    text: str
    order: tuple
    sonnet: dict | None = None
    golden: dict | None = None
    laya: dict | None = None
    decision: dict | None = None
    held_out: bool | None = None
    #: The text keys behind a golden item: answering one of them retires the golden case.
    underlying: frozenset[str] = frozenset()
    #: `edge_of` on the card's own p: the cut it sits nearest and p minus it. None without a p.
    edge: tuple[str, float] | None = None
    #: The @challenger's answer on this key's newest decision, when it is the other one (`shadow_rivals`).
    challenger: dict | None = None
    #: Which OPEN its newest verdict's landing points at (`needs_hint`): an order key, never shown and never a label.
    needs_hint: str | None = None
    #: `band_of` on the card's LIVE verdict (a ledger p), at the cuts `edge` reads; None without one. What the band
    #: deal filters on (the module doc: THE BAND DEAL).
    band: str | None = None


@dataclass(frozen=True)
class Verdict:
    """One live decision as precision and recall read it (`live_rates`): its text, whether it drew a ⚠️ at its own cut,
    whether the rack SHOWED that ⚠️ (the ledger's `shown` reaction), and where it landed: a class in `LANDING_SPLIT`
    and an audience in `AUDIENCES`, both `unknown` without a landing row (`landing.py`)."""

    key: str
    flagged: bool
    shown: bool
    landing_class: str
    audience: str


def _verdict(key: str, flagged: bool, shown: bool, landed: Mapping[str, Any] | None) -> Verdict:
    if landed is None:
        return Verdict(key, flagged, shown, "unknown", "unknown")
    found_class = landed["class"] if landed["class"] in landing.CLASSES else "unknown"
    return Verdict(key, flagged, shown, found_class, landed["held"] or "free")


@dataclass
class Items:
    """Everything that does not depend on the operator's answers (rebuilt when a source changes)."""

    spec: ReviewSurface
    served: Served
    items: dict[str, Item]
    sources: dict[str, str]
    #: Laya's verdicts on texts Sonnet never labelled, before sampling: (flagged, unflagged).
    flagged_total: int
    unflagged_total: int
    reactions: list[tuple[str, dict]]  # (decision at, actions) for every decision of the surface
    #: Every live verdict with text, newest first: (key, `_recent_meta`). Not capped at a page: the
    #: EDGE order ranks all of them, and the texts are already held in `items`.
    recent: list[tuple[str, dict]] = field(default_factory=list)
    #: Every run of adjacent same-mark turn ends of one session, oldest first, before answers thin them (`snapshot`).
    runs: list[tuple[str, ...]] = field(default_factory=list)
    #: recall's populations. Every Sonnet-free live verdict at or over its cut, split the way the deck deals them:
    #: `flagged_near` within `UNCERTAIN_WITHIN` of a cut (`is_near`, dealt as `uncertain` at a heavier weight) and
    #: `flagged_far` the rest (`flagged`); `flagged_rival` the ones the @challenger would not have flagged (dealt as
    #: `challenger`, at its own weight). Each is a census, estimated from its own answers (`stats` owns why). Then the
    #: ones under the cut that a uniform sample drew (`unflagged`, or `live_sample`).
    flagged_near: frozenset[str] = frozenset()
    flagged_far: frozenset[str] = frozenset()
    flagged_rival: frozenset[str] = frozenset()
    unflagged_sampled: frozenset[str] = frozenset()
    #: Every live decision of the surface with a verdict and a text key, by decision_id; and each key's ids, newest
    #: first. What precision per landing class and recall per audience read (`live_rates`).
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    decisions_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Each of those decisions' own p, cut and checkpoint (`_ledger_laya`): what a rack grade is recorded against.
    decision_laya: dict[str, dict] = field(default_factory=dict)
    #: The surface's @challenger (its version name), or None: whose disagreements fill `challenger`.
    challenger: str | None = None
    #: The public golden cases, and the provisional ones in a batch: which of those `snapshot` deals turns on the
    #: operator's answers (`lab_files.dealt_golden_ids`), so the filter runs there, not here.
    golden_public: tuple[dict, ...] = ()
    golden_batched: frozenset[str] = frozenset()


@dataclass
class Snapshot:
    items: Items
    fold: OperatorFold
    operator_code: str
    remaining: dict[str, list[str]] = field(default_factory=dict)
    #: `sweep_readiness` at this fold, and the class it is shortest of (`class_deficit`), which `uncertain` leans toward.
    readiness: dict = field(default_factory=dict)
    deficit: str | None = None
    #: lead key → every unanswered text its card grades, oldest first, the lead included (a WAVE: see the module doc).
    waves: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: `three_way` at this fold (None on a surface without `needs`), and its `lean`: the OPEN half the live lanes
    #: lean toward.
    grades: dict | None = None
    needs_short: str | None = None

    @property
    def spec(self) -> ReviewSurface:
        return self.items.spec


def _decision_block(entry: Mapping[str, Any]) -> dict:
    actions = entry["actions"]
    reactions = sorted((at, action) for action, at in actions.items() if action != "shown")
    return {
        "decision_id": entry["decision"]["decision_id"],
        "at": entry["decision"]["at"],
        "shown": "shown" in actions,
        "reaction": reactions[-1][1] if reactions else None,
    }


def _checkpoint_path(name: str | None) -> str | None:
    """`checkpoints/<name>`, the params-relative path an answer row records, when the name can be one."""
    relative = f"{paths.CHECKPOINTS_DIRNAME}/{name}" if name else None
    return relative if relative and ledger.PARAMS_RELATIVE_PATH.fullmatch(relative) else None


def _ledger_laya(decision: Mapping[str, Any], spec: ReviewSurface) -> dict | None:
    answers = decision.get("decisions")
    first = answers[0] if isinstance(answers, list) and answers and isinstance(answers[0], dict) else None
    if first is None or first.get("abstain") or not is_p(first.get("p")):
        return None
    threshold = decision.get("threshold") if is_p(decision.get("threshold")) else spec.default_threshold
    relative = decision.get("checkpoint_path")
    if (
        not isinstance(relative, str)
        or len(relative) > ledger.CHECKPOINT_PATH_MAX
        or not ledger.PARAMS_RELATIVE_PATH.fullmatch(relative)
    ):
        relative = None
    version = decision.get("question_version")
    return {
        "p": float(first["p"]),
        "threshold": float(threshold),
        "model": paths.served_name(str(decision.get("model") or "english")),
        "checkpoint": Path(relative).name if relative else None,
        "question_version": version if files.is_count(version) else 0,
        "source": "ledger",
        "checkpoint_path": relative,
    }


def _scores_laya(row: Mapping[str, Any]) -> dict:
    checkpoint = row["checkpoint"]
    return {
        "p": float(row["p"]),
        "threshold": float(row["threshold"]),
        "model": row["model"],
        "checkpoint": checkpoint,
        "question_version": row["question_version"],
        "source": "scores",
        "checkpoint_path": _checkpoint_path(checkpoint),
    }


def _build_items(base: Path, spec: ReviewSurface, loaded: Mapping[str, str] | None) -> Items:
    served = served_model(base, spec.name, loaded)
    sources: dict[str, str] = {}
    sources["unlabelled"], unlabelled = unlabelled_texts(base, spec)
    sources["labels"], labels = sonnet_labels(base, spec)
    newest, folded = _ledger_decisions(base, spec.name)
    sources["ledger"] = ledger.active_code(base, spec.name)
    scores, sources["scores"] = lab_files.read_scores(base, spec.stem)
    public, sources["golden"], private, sources["golden_private"] = golden_suites(base, spec.golden_suite)
    report = lab_files.newest_report(base, spec.golden_suite, served.name, "golden")
    sources["eval"] = files.OK if report else files.MISSING
    landed: dict[str, dict] = {}
    if spec.name in landing.SURFACES:  # only the rack lands a verdict on a row
        landed = landing.landings_by_decision(base, spec.name)
        sources["landing"] = files.OK if landed else files.MISSING

    all_texts = _merge_texts(spec, newest, unlabelled)
    challenger = challenger_of(base, spec)
    rivals: dict[str, dict] = {}
    if challenger is not None:
        shadow, sources["shadow"] = lab_files.read_shadow(base, spec.name)
        rivals = shadow_rivals(shadow, challenger, served, newest)

    trained, missing = training_record(base, spec, served) if served.fine_tune else (frozenset(), False)

    def held_out(key: str) -> bool | None:
        if not served.fine_tune:
            return True
        return None if missing else key not in trained

    def hint_for(entry: Mapping[str, Any] | None) -> str | None:
        return needs_hint(landed.get(entry["decision"]["decision_id"])) if entry else None

    current: dict[str, dict] = {}
    for row in scores:
        if row["model"] == served.name and row["checkpoint"] == served.basename:
            held = current.get(row["key"])
            if held is None or row["at"] >= held["at"]:
                current[row["key"]] = row

    def laya_for(key: str) -> dict | None:
        entry = newest.get(key)
        found = _ledger_laya(entry["decision"], spec) if entry else None
        if found is None and key in current:
            found = _scores_laya(current[key])
        return found

    def done_for(key: str, laya: Mapping[str, Any]) -> float:
        """The ✅ cut the p was judged at: a live verdict's own when it recorded one, else today's."""
        entry = newest.get(key)
        if laya["source"] == "ledger" and entry is not None and is_p(entry["decision"].get("done_threshold")):
            return float(entry["decision"]["done_threshold"])
        return spec.default_done_threshold

    def edge_for(key: str, laya: Mapping[str, Any] | None) -> tuple[str, float] | None:
        """`edge_of` at the cuts the p was judged at."""
        return None if laya is None else edge_of(laya["p"], laya["threshold"], done_for(key, laya))

    def band_for(key: str, laya: Mapping[str, Any] | None) -> str | None:
        """`band_of` on a live verdict at the same cuts as `edge_for`; None for a p that is not one (a score, an eval)."""
        if laya is None or laya["source"] != "ledger":
            return None
        return band_of(laya["p"], laya["threshold"], done_for(key, laya))

    teacher_at: dict[str, tuple[str, bool, float]] = {}
    for row in scores:  # the surrogate reads text alone, so a row from any checkpoint serves, the newest winning
        if "teacher_open" in row and (row["key"] not in teacher_at or row["at"] >= teacher_at[row["key"]][0]):
            teacher_at[row["key"]] = (row["at"], row["teacher_open"], row["teacher_conf"])
    # A label Sonnet changed since SCORE was not the one the surrogate judged.
    teacher = {
        key: (is_open, conf)
        for key, (_at, is_open, conf) in teacher_at.items()
        if key in labels and labels[key]["open"] is is_open
    }
    suspect_rank = {key: rank for rank, key in enumerate(suspect_ranked(teacher))}

    items: dict[str, Item] = {}
    for key, label in labels.items():
        if key not in all_texts:
            continue
        sonnet = {"by": label["by"], "open": label["open"], "why": label["why"] or None}
        is_held = held_out(key)
        laya = laya_for(key)
        edge = edge_for(key, laya)
        backlog = (0, rows.bucket(key, "review-order"), key)
        if rows.bucket(key, "review-sample") < spec.sample_percent:
            stratum, order = "sample", (0 if is_held is True else 1, rows.bucket(key, "review-order"), key)
        elif key in newest and is_live_member(spec, key):
            stratum, order = "live_sample", _live_order(key)
        elif key in rivals:
            stratum, order = "challenger", _newest_first(newest[key], key)
        elif key in suspect_rank:
            stratum, order = "suspect", (suspect_rank[key], key)
        elif key in current and (current[key]["p"] >= current[key]["threshold"]) != label["open"]:
            stratum, order = "disagree", backlog
        elif is_near(edge):
            stratum, order = "uncertain", backlog  # ordered per fold: `snapshot` owns its order
        else:
            stratum, order = "rest", backlog
        entry = newest.get(key)
        items[key] = Item(
            key,
            stratum,
            all_texts[key],
            order,
            sonnet=sonnet,
            laya=laya,
            decision=_decision_block(entry) if entry else None,
            held_out=is_held,
            edge=edge,
            challenger=rivals.get(key) if stratum == "challenger" else None,
            needs_hint=hint_for(entry),
            band=band_for(key, laya),
        )

    facts = lab_files.golden_facts(report[1]) if report is not None else None

    def add_golden(case: Mapping[str, Any], rank: int) -> None:
        case_id = case["id"]
        key = f"golden:{case_id}"
        if key in items or not lab_files.CASE_ID.fullmatch(case_id):
            return
        text = case["state"]["hand_off"]
        text_key = rows.state_key(text)
        found = laya_for(text_key)
        if found is None and facts is not None and case_id in facts["probabilities"]:
            checkpoint = facts["checkpoint"]
            found = {
                "p": facts["probabilities"][case_id],
                "threshold": facts["threshold"] or spec.default_threshold,
                "model": served.name,
                "checkpoint": checkpoint,
                "question_version": facts["question_version"],
                "source": "eval",
                "checkpoint_path": _checkpoint_path(checkpoint),
            }
        why = case.get("why") if isinstance(case.get("why"), str) else ""
        golden = {"case_id": case_id, "expected": case["expected"], "why": clip(why, WHY_MAX)}
        # a written case has no session: the request it was written with is all the context its card can show
        request = case["state"].get("request")
        if isinstance(request, str) and request.strip():
            golden["request"] = clip(request.strip(), REQUEST_MAX)
        items[key] = Item(
            key,
            "golden",
            text,
            (rank, case_id),
            laya=found,
            held_out=held_out(text_key),
            golden=golden,
            underlying=frozenset(_case_keys(case)),
            edge=edge_for(text_key, found),
            band=band_for(text_key, found),
        )

    cases = {case["id"]: case for case in (*public, *private)}
    misses = facts["misses"] if facts is not None else []
    for rank, case_id in enumerate(misses):
        if case_id in cases:
            add_golden(cases[case_id], rank)
    # the operator's sign-off on an agent-written public case (`lab_files.PROVISIONAL_TAG`), missed or not; a batch
    # case is dealt only while the batch sign-off asks for it (`lab_files.BATCH_TAG`), so `snapshot` filters on the fold
    for rank, case in enumerate((case for case in public if lab_files.is_provisional(case)), len(misses)):
        add_golden(case, rank)
    batched = frozenset(case["id"] for case in public if lab_files.is_provisional(case) and lab_files.case_batch(case))

    flagged_total = unflagged_total = 0
    flagged_near: set[str] = set()
    flagged_far: set[str] = set()
    flagged_rival: set[str] = set()
    unflagged_sampled: set[str] = set()
    for key, entry in newest.items():
        if key in labels or key not in all_texts:
            continue
        found = _ledger_laya(entry["decision"], spec)
        if found is None:
            continue
        edge = edge_for(key, found)
        is_flagged = found["p"] >= found["threshold"]
        member = is_live_member(spec, key)
        if is_flagged:
            flagged_total += 1
            # The census a flagged text is graded in follows the lane that deals it (`stats` owns why).
            (flagged_rival if key in rivals and not member else flagged_near if is_near(edge) else flagged_far).add(key)
        else:
            unflagged_total += 1
        order = _newest_first(entry, key)
        if member:
            stratum, order = "live_sample", _live_order(key)
            if not is_flagged:
                unflagged_sampled.add(key)
        elif is_flagged:
            stratum = "challenger" if key in rivals else "uncertain" if is_near(edge) else "flagged"
        elif rows.bucket(key, "review-unflagged") < spec.unflagged_percent:
            stratum = "unflagged"
            unflagged_sampled.add(key)
        else:
            stratum = "challenger" if key in rivals else "uncertain" if is_near(edge) else "recent"
        items[key] = Item(
            key,
            stratum,
            all_texts[key],
            order,
            laya=found,
            decision=_decision_block(entry),
            held_out=held_out(key),
            edge=edge,
            challenger=rivals.get(key) if stratum == "challenger" else None,
            needs_hint=hint_for(entry),
            band=band_for(key, found),
        )

    reactions = [
        (entry["decision"]["at"], entry["actions"])
        for entry in folded.values()
        if entry["decision"] is not None and entry["decision"].get("surface") == spec.name
    ]
    recent: list[tuple[str, dict]] = []
    for key, entry in newest.items():
        meta = _recent_meta(entry["decision"], spec) if key in all_texts else None
        if meta is not None:
            recent.append((key, {**meta, "text": all_texts[key]}))
    recent.sort(key=lambda pair: (pair[1]["at"], pair[0]), reverse=True)

    verdicts: dict[str, Verdict] = {}
    decision_laya: dict[str, dict] = {}
    dated: dict[str, list[tuple[str, str]]] = {}
    for decision_id, entry in folded.items():
        decision = entry["decision"]
        key = _decision_key(decision, spec.name)
        found = _ledger_laya(decision, spec) if key is not None else None
        if found is None:
            continue
        verdicts[decision_id] = _verdict(
            key, found["p"] >= found["threshold"], "shown" in entry["actions"], landed.get(decision_id)
        )
        decision_laya[decision_id] = found
        dated.setdefault(key, []).append((decision["at"], decision_id))
    decisions_of = {key: tuple(decision_id for _at, decision_id in sorted(pairs, reverse=True)) for key, pairs in dated.items()}
    return Items(
        spec,
        served,
        items,
        sources,
        flagged_total,
        unflagged_total,
        reactions,
        recent,
        runs=wave_runs(recent),
        flagged_near=frozenset(flagged_near),
        flagged_far=frozenset(flagged_far),
        flagged_rival=frozenset(flagged_rival),
        unflagged_sampled=frozenset(unflagged_sampled),
        verdicts=verdicts,
        decisions_of=decisions_of,
        decision_laya=decision_laya,
        challenger=challenger,
        golden_public=tuple(public),
        golden_batched=batched,
    )


def _recent_meta(decision: Mapping[str, Any], spec: ReviewSurface) -> dict | None:
    """What the RECENT list needs from one ledger decision, or None for one with no verdict (an abstain)."""
    laya = _ledger_laya(decision, spec)
    if laya is None:
        return None
    done = decision.get("done_threshold")
    subject = decision.get("subject")
    conversation = decision.get("conversation")  # absent from a row recorded before 2026-11-07
    if not (isinstance(conversation, str) and ledger.CONVERSATION.fullmatch(conversation)):
        conversation = None
    return {
        "decision_id": decision["decision_id"],
        "at": decision["at"],
        "subject": subject if isinstance(subject, str) and ledger.SUBJECT.fullmatch(subject) else None,
        "conversation": conversation,
        "p": laya["p"],
        "threshold": laya["threshold"],
        "done_threshold": float(done) if is_p(done) else None,
        "model": laya["model"],
        "checkpoint": laya["checkpoint"],
    }


def band_of(p: float, open_at: float, done_at: float | None) -> str:
    """`open` at or past the open cut, `done` under the done cut, else `reply` (the plain 💬).
    `done_at` None is a verdict from before the done band existed: it can only be `reply` or `open`."""
    if p >= open_at:
        return "open"
    return "done" if done_at is not None and p < min(done_at, open_at) else "reply"


def edge_of(p: float, open_at: float, done_at: float) -> tuple[str, float]:
    """The cut p sits nearest, `open` (⚠️) or `done` (✅), and p minus that cut, signed.

    −0.02 on `open` is 0.02 short of a ⚠️; +0.01 on `done` is 0.01 short of a ✅; 0 on `open` is a ⚠️
    by nothing. These are the turns one nudge from another mark: a label on one says which side of
    the cut it belongs on, which is exactly what a RE-SWEEP moves the cut by and what a fine-tune
    learns most from. A tie goes to `open`, the mark whose false alarm costs more
    (`tldrack/open_work.py` FALSE_WARNING_COST)."""
    done = min(done_at, open_at)  # band_of's clamp: the ✅ cut never sits above the ⚠️ one
    to_open, to_done = p - open_at, p - done
    return ("open", to_open) if abs(to_open) <= abs(to_done) else ("done", to_done)


def _at_number(at: str) -> int:
    digits = "".join(ch for ch in at if ch.isdigit())[:14]
    return int(digits) if digits else 0


_AT = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})")


def _at_seconds(at: str) -> float | None:
    """Epoch seconds of a `utc_stamp` (a fraction ignored), or None for one that is not."""
    match = _AT.match(at)
    if match is None:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# ── the acquisition strata ────────────────────────────────────────────────────


def is_live_member(spec: ReviewSurface, key: str) -> bool:
    """Whether a live verdict's key is in `live_sample`'s hash. The membership test `live_rates` reads too, so a
    member graded as `sample` (its Sonnet label puts it there first) still counts as drawn."""
    return rows.bucket(key, "review-live") < spec.live_percent


def _live_order(key: str) -> tuple:
    """A second hash, so the deck deals the sample in an order its p never set."""
    return (rows.bucket(key, "review-live-order"), key)


def _newest_first(entry: Mapping[str, Any], key: str) -> tuple:
    """A live lane's order (`flagged`, `recent`, `challenger`): the operator judges what the rack showed most recently."""
    return (-_at_number(entry["decision"]["at"]), key)


def challenger_of(base: Path, spec: ReviewSurface) -> str | None:
    """The version name the surface's @challenger alias points at (models.json, `paths.registry_versions`), or None.
    Read off the registry whatever laya-serve loaded: a @challenger is never served."""
    name = (paths.registry_versions(base)["aliases"].get(spec.name) or {}).get("challenger")
    return name if isinstance(name, str) and name else None


def shadow_rivals(
    shadow: Sequence[Mapping[str, Any]], challenger: str, served: Served, newest: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict]:
    """`{key: {"name", "p", "threshold"}}`: the texts where `challenger` and the champion answer the ⚠️ question
    differently, each at its own cut, from laya-lab's shadow rows (`lab_files.read_shadow`).

    Three rules keep a card honest. Only a row whose decision was SERVED by the checkpoint serving now: after a
    promotion or a rollback the old rows compare against a champion that is gone. Only the row of the key's NEWEST
    decision: its p is the one the card shows and the answer records, so the side an answer took can be read off
    the answer row alone. And the newest score of that decision wins (a re-trained folder, or another device),
    so a disagreement the challenger no longer has leaves the lane."""
    scored: dict[str, tuple[str, dict | None]] = {}
    for row in shadow:
        if row["challenger"] != challenger or row["served_model"] != served.name or row["served_checkpoint"] != served.basename:
            continue
        key = row["key"]
        entry = newest.get(key)
        if entry is None or entry["decision"]["decision_id"] != row["decision_id"]:
            continue
        if key in scored and row["scored_at"] < scored[key][0]:
            continue
        differs = (row["served_p"] >= row["served_threshold"]) != (row["challenger_p"] >= row["challenger_threshold"])
        scored[key] = (
            row["scored_at"],
            {"name": challenger, "p": float(row["challenger_p"]), "threshold": float(row["challenger_threshold"])}
            if differs
            else None,
        )
    return {key: block for key, (_at, block) in scored.items() if block is not None}


def is_near(edge: tuple[str, float] | None) -> bool:
    """Within `UNCERTAIN_WITHIN` of a cut, at `edge_distance`'s six decimals (0.8 − 0.7 is 0.10000000000000009)."""
    return edge is not None and round(abs(edge[1]), 6) <= UNCERTAIN_WITHIN


def suspect_ranked(teacher: Mapping[str, tuple[bool, float]]) -> list[str]:
    """Confident learning (Northcutt et al., cleanlab's `self_confidence` rank) over Sonnet's labels, without the
    dependency: `teacher` is key → (Sonnet's label, the surrogate's out-of-fold probability of that label).

    Each class's threshold is the mean self-confidence of the keys labelled it. A key is suspect when the OTHER class
    clears that class's threshold and wins the pair (cleanlab's argmax over the classes past their thresholds: a tie
    keeps the given label). Least self-confident first; ties by key."""
    means: dict[bool, float | None] = {}
    for label in (True, False):
        confs = [conf for is_open, conf in teacher.values() if is_open is label]
        means[label] = sum(confs) / len(confs) if confs else None
    found = []
    for key, (is_open, conf) in teacher.items():
        other_t, own_t = means[not is_open], means[is_open]
        other = 1.0 - conf
        if other_t is not None and other >= other_t and (own_t is None or conf < own_t or other > conf):
            found.append((conf, key))
    return [key for _conf, key in sorted(found)]


def wave_runs(recent: list[tuple[str, dict]]) -> list[tuple[str, ...]]:
    """Every run of two or more adjacent turn ends of one session that drew the same mark, each within `WAVE_GAP_S`
    of the one before, oldest first. A session is its conversation, else its rack name (tmux reuses a `vc-N`, but a
    reused name's turns are hours apart and the gap splits them); a verdict with neither never joins one."""
    by_session: dict[str, list[tuple[float, str, str]]] = {}
    for key, meta in recent:
        session = meta.get("conversation") or meta.get("subject")
        seconds = _at_seconds(meta["at"])
        if session is None or seconds is None:
            continue
        mark = band_of(meta["p"], meta["threshold"], meta["done_threshold"])  # what the rack drew
        by_session.setdefault(session, []).append((seconds, key, mark))
    runs: list[tuple[str, ...]] = []
    for name in sorted(by_session):
        turns = sorted(by_session[name])
        run: list[tuple[float, str, str]] = []
        for turn in [*turns, None]:
            if run and (turn is None or turn[2] != run[-1][2] or turn[0] - run[-1][0] > WAVE_GAP_S):
                if len(run) >= 2:
                    runs.append(tuple(key for _s, key, _m in run))
                run = []
            if turn is not None:
                run.append(turn)
    return runs


# ── memo ──────────────────────────────────────────────────────────────────────

_ITEMS: dict[tuple[str, str], tuple[tuple, Items]] = {}


def _source_stamps(base: Path, spec: ReviewSurface, loaded: Mapping[str, str] | None) -> tuple:
    """Cheap: a handful of lstat calls. Folders are stamped too, so a new report, segment or checkpoint counts."""
    watched = [
        paths.data_file(base, spec.stem, "unlabelled"),
        paths.data_file(base, spec.stem, "labels"),
        paths.data_file(base, spec.stem, "laya-p"),
        base / paths.MODELS_FILENAME,
        base / paths.LEDGER_DIRNAME,
        base / paths.LEDGER_DIRNAME / f"{spec.name}.jsonl",
        base / paths.LANDING_DIRNAME,
        base / paths.LANDING_DIRNAME / f"{spec.name}.jsonl",
        base / paths.EVALS_DIRNAME,
        base / paths.CHECKPOINTS_DIRNAME,
        paths.shadow_path(base, spec.name),
        paths.PUBLIC_GOLDEN_DIR / f"{spec.golden_suite}.jsonl",
        paths.private_golden_path(base, spec.golden_suite),
    ]
    return (tuple(files.stamp_of(path) for path in watched), None if loaded is None else tuple(sorted(loaded.items())))


def snapshot(base: Path, surface: str, *, loaded: Mapping[str, str] | None = None) -> Snapshot:
    """The deck for one surface: items memoised on the source stamps, the operator fold read incrementally."""
    spec = REVIEW_SURFACES[surface]
    stamps = _source_stamps(base, spec, loaded)
    memo_key = (str(base), surface)
    with _MEMO_LOCK:
        held = _ITEMS.get(memo_key)
    if held is not None and held[0] == stamps:
        built = held[1]
    else:
        built = _build_items(base, spec, loaded)
        with _MEMO_LOCK:
            _ITEMS[memo_key] = (stamps, built)
    operator_code, current = review_log.read_fold(base, surface)
    readiness = sweep_readiness(base, surface, current, loaded=loaded)
    grades = three_way(current, spec)
    snap = Snapshot(
        built,
        current,
        operator_code,
        readiness=readiness,
        deficit=class_deficit(readiness),
        grades=grades,
        needs_short=grades["lean"] if grades else None,
    )
    riders = _deal_waves(snap)
    answered_text = current.labels.keys() | current.skipped
    dealt = (
        lab_files.dealt_golden_ids(list(built.golden_public), current.labels, current.skipped)
        if built.golden_batched
        else frozenset()
    )
    lanes: dict[str, list[Item]] = {stratum: [] for stratum in STRATA}
    for item in built.items.values():
        held = item.golden is not None and item.golden["case_id"] in built.golden_batched and item.golden["case_id"] not in dealt
        if not (held or current.answered(item.key) or item.underlying & answered_text or item.key in riders):
            lanes[item.stratum].append(item)
    for stratum, lane in lanes.items():  # each stratum orders by its own key shape
        snap.remaining[stratum] = [
            item.key for item in sorted(lane, key=lambda it: _lane_order(it, snap.deficit, snap.needs_short))
        ]
    return snap


def _lane_order(item: Item, deficit: str | None, needs_short: str | None = None) -> tuple:
    """An item's place in its lane. Two orders move with the fold. `NEEDS_LEAN_LANES` put the cards hinted for the
    short three-way class first (the module doc: THE THREE-WAY GRADE). `uncertain` is then: held-out texts first (a
    label on a trained one is never clean), that lean, the cut the short class sits behind (`class_deficit`), then
    nearest the cut."""
    if item.stratum != "uncertain" or item.edge is None:
        lean = 0 if needs_short is None or item.needs_hint == needs_short else 1
        return (lean, *item.order) if item.stratum in NEEDS_LEAN_LANES else item.order
    return _near_order(item, deficit, needs_short)


def _near_order(item: Item, deficit: str | None, needs_short: str | None) -> tuple:
    """The UNCERTAIN lane's order (`_lane_order` owns why), which the band deal reads too. `item.edge` is set."""
    assert item.edge is not None
    lean = 0 if needs_short is None or item.needs_hint == needs_short else 1
    cut, delta = item.edge
    return (0 if item.held_out is True else 1, lean, 0 if deficit in (None, cut) else 1, round(abs(delta), 6), item.key)


# ── the four-way grade ────────────────────────────────────────────────────────


def grade_of(row: Mapping[str, Any]) -> str | None:
    """An operator row's four-way class (an answer's or a retest's): `read` or `done`, or which OPEN (`running`,
    `you`). None for a skip, for an OPEN nobody split (that one says open and nothing more), and for a bare DONE from
    before `READ_SINCE` (it could not say whether the answer owed a read). ISO stamps in UTC compare as text."""
    needs = row.get("needs")
    if not isinstance(row.get("open"), bool) or not review_log.needs_ok(needs, row["open"]):
        return None
    if row["open"]:
        return needs
    if needs == "read":
        return "read"
    at = row.get("at")
    return "done" if isinstance(at, str) and at >= READ_SINCE else None


def three_way(fold: OperatorFold, spec: ReviewSurface) -> dict | None:
    """`reviewStatsSchema.three_way` (the name predates READ): the live labels per class, the floor, and two readings of
    who is short of it. `deficit` is the GATE's: the class furthest under the floor of all four, None once every class
    holds it. `lean` is the DECK's: the OPEN half furthest under it, None once both OPEN halves hold it. They differ
    when DONE or READ is the shortest class while a half is still short (done 1, running 3, you 2): the gate waits on
    DONE, and the live lanes still lean toward `you`, since only the OPEN halves have a hint to lean on (`needs_hint`). None on a surface without
    `needs`. A tie goes to `you`, then `running`: the rarer halves in every grading so far (2 'needs: you' labels in
    all on 2026-11-09)."""
    if not spec.needs:
        return None
    counts = dict.fromkeys(GRADES, 0)
    for row in fold.labels.values():
        grade = grade_of(row)
        if grade in counts:
            counts[grade] += 1
    short = {name: spec.needs_min_per_class - counts[name] for name in GRADES}

    def shortest(names: tuple[str, ...]) -> str | None:
        worst = max(short[name] for name in names)
        return None if worst <= 0 else next(name for name in reversed(names) if short[name] == worst)

    halves = tuple(name for name in GRADES if name in spec.needs and name in review_log.NEEDS_OPEN)
    return {**counts, "min": spec.needs_min_per_class, "deficit": shortest(GRADES), "lean": shortest(halves)}


def needs_hint(landed: Mapping[str, Any] | None) -> str | None:
    """Which OPEN a turn is LIKELY to be graded, read off where its verdict landed (`landing.py`): a row held for
    its own jobs or its children was `running`; a free row whose hand-off ended on a question waited on `you`.
    None without a landing row, or when it says neither."""
    if landed is None:
        return None
    if landed.get("held") in landing.HELD_REASONS:
        return "running"
    trace = landed.get("trace")
    return "you" if isinstance(trace, dict) and trace.get("ended_on_question") is True else None


def _deal_waves(snap: Snapshot) -> set[str]:
    """Fill `snap.waves` from the runs, minus answered texts and the uniform samples; return the riders (every member
    but each card's lead), which leave their lanes. A run past `WAVE_MAX` deals as consecutive cards."""
    items, fold = snap.items.items, snap.fold
    riders: set[str] = set()
    for run in snap.items.runs:
        members = [key for key in run if key in items and items[key].stratum not in WAVE_NEVER and not fold.answered(key)]
        for start in range(0, len(members), WAVE_MAX):
            chunk = members[start : start + WAVE_MAX]
            if len(chunk) < 2:
                continue
            # The most valuable stratum leads; the newest member of it on a tie (what the rack showed last).
            lead = min(range(len(chunk)), key=lambda i: (WAVE_LEAD.index(items[chunk[i]].stratum), -i))
            snap.waves[chunk[lead]] = tuple(chunk)
            riders.update(key for i, key in enumerate(chunk) if i != lead)
    return riders


def sources(snap: Snapshot) -> dict[str, str]:
    return {**snap.items.sources, "operator": snap.operator_code}


# ── cards, queue, stats ───────────────────────────────────────────────────────


def card(snap: Snapshot, key: str) -> dict | None:
    """The `reviewCardSchema` card for `key`, answered or not; None when no current source has it."""
    item = snap.items.items.get(key)
    if item is None:
        return None
    text = item.text
    text_chars = utf16_len(text)  # JavaScript's `.length`: the deck compares it with `card.text.length`
    if item.sonnet is not None:
        reference = dict(item.sonnet)
    elif item.golden is not None:
        reference = {"by": "golden", "open": item.golden["expected"], "why": item.golden["why"] or None}
    elif item.laya is not None:
        reference = {"by": item.laya["model"], "open": item.laya["p"] >= item.laya["threshold"], "why": None}
    else:
        return None
    laya = None
    if item.laya is not None:
        laya = {k: item.laya[k] for k in ("p", "threshold", "model", "checkpoint", "question_version", "source")}
    return {
        "great_example": snap.fold.examples.get(key, {}).get("great_example", False),
        "item_id": f"{snap.spec.name}:{key}",
        "key": key,
        "surface": snap.spec.name,
        "stratum": item.stratum,
        "text": utf16_tail(text, CARD_TEXT_MAX),
        "text_chars": text_chars,
        "truncated": text_chars > CARD_TEXT_MAX,
        "held_out": item.held_out,
        "reference": reference,
        "sonnet": dict(item.sonnet) if item.sonnet else None,
        "laya": laya,
        "golden": dict(item.golden) if item.golden else None,
        "decision": dict(item.decision) if item.decision else None,
        "answers": list(snap.spec.answers),
        "edge": {"cut": item.edge[0], "delta": round(item.edge[1], 4)} if item.edge is not None else None,
        "wave": _wave_block(snap, key),
        "challenger": dict(item.challenger) if item.challenger else None,
    }


def _wave_block(snap: Snapshot, key: str) -> dict | None:
    """`reviewCardSchema.wave` for a lead: how many texts one press grades, and each rider's tail, oldest first."""
    members = snap.waves.get(key)
    if members is None:
        return None
    riders = []
    for member in members:
        item = snap.items.items[member]
        if member == key or item.decision is None or item.laya is None:
            continue
        riders.append(
            {
                "key": member,
                "at": item.decision["at"],
                "p": item.laya["p"],
                "stratum": item.stratum,
                "text": utf16_tail(item.text, WAVE_TEXT_MAX),
                "text_chars": utf16_len(item.text),
            }
        )
    return {"n": len(riders) + 1, "members": riders}


def holding_card(snap: Snapshot, keys: Sequence[str]) -> dict | None:
    """The card the deck deals holding the first of `keys` that has one: the lead's card when that text rides a wave
    (its `wave` block whole), else the text's own. What an UNDO hands back, so the press it took back is dealt again
    as the card it was answered on, whichever of its rows the undo named (`review.py` `undo`)."""
    for key in keys:
        lead = next((lead for lead, members in snap.waves.items() if key in members), None)
        if lead is not None:
            return card(snap, lead)
    return card(snap, keys[0]) if keys else None


def wave_members(snap: Snapshot, key: str) -> tuple[str, ...]:
    """The riders one press on `key`'s card grades with it, oldest first: () when its card is not a wave's lead."""
    return tuple(member for member in snap.waves.get(key, ()) if member != key)


def against_of(snap: Snapshot, key: str) -> dict | None:
    """What an answer to `key` is recorded against: the card's reference, plus the golden case id."""
    found = card(snap, key)
    if found is None:
        return None
    against = {"by": found["reference"]["by"], "open": found["reference"]["open"]}
    if found["golden"] is not None:
        against["case_id"] = found["golden"]["case_id"]
    return against


def laya_of(snap: Snapshot, key: str) -> dict | None:
    """The answer row's `laya` block: p, cut, source and the params-relative checkpoint path."""
    item = snap.items.items.get(key)
    if item is None or item.laya is None:
        return None
    return {k: item.laya[k] for k in ("p", "threshold", "source", "checkpoint_path")}


def band_waiting(snap: Snapshot, band: str) -> list[str]:
    """The unanswered keys whose live verdict sits in `band`, from every lane but `BAND_NEVER`, unordered (the module
    doc: THE BAND DEAL). A wave's riders already left their lanes, so a wave counts once, as its lead."""
    if band not in BANDS:
        raise ValueError(f"band must be one of {', '.join(BANDS)}")
    items = snap.items.items
    return [key for name, keys in snap.remaining.items() if name not in BAND_NEVER for key in keys if items[key].band == band]


def queue(snap: Snapshot, limit: int, stratum: str | None = None, band: str | None = None) -> list[dict]:
    """Up to `limit` cards: a weighted round robin over the strata; an empty stratum yields its turn. With `band`, the
    cards whose live verdict sits in it, in the UNCERTAIN lane's order (the module doc: THE BAND DEAL)."""
    if band is not None:
        if stratum is not None:  # review.py refuses the pair before here
            raise ValueError("queue takes a stratum or a band, not both")
        items = snap.items.items
        ordered = sorted(band_waiting(snap, band), key=lambda key: _near_order(items[key], snap.deficit, snap.needs_short))
        found = (card(snap, key) for key in ordered[:limit])
        return [c for c in found if c is not None]
    lanes = {name: list(snap.remaining.get(name, ())) for name, _w in snap.spec.weights}
    weights = [(name, weight) for name, weight in snap.spec.weights if stratum is None or name == stratum]

    def turn(name: str, weight: int) -> int:
        """The lane's take this round. `NEEDS_BOOST_LANE` takes up to `NEEDS_BOOST_WEIGHT` while the cards it leads
        with are hinted for the short class: the extra turns go to those cards only, never to the ones behind them."""
        if name != NEEDS_BOOST_LANE or snap.needs_short is None:
            return weight
        hinted = 0
        for key in lanes[name][:NEEDS_BOOST_WEIGHT]:
            if snap.items.items[key].needs_hint != snap.needs_short:
                break
            hinted += 1
        return max(weight, hinted)

    picked: list[str] = []
    while len(picked) < limit and any(lanes[name] for name, _w in weights):
        for name, weight in weights:
            take = turn(name, weight)
            picked.extend(lanes[name][:take])
            lanes[name] = lanes[name][take:]
    found = (card(snap, key) for key in picked[:limit])
    return [c for c in found if c is not None]


# ── the blind retest ──────────────────────────────────────────────────────────


def retest_due(snap: Snapshot, now: datetime | None = None) -> list[str]:
    """The keys due a blind retest (the module doc: THE BLIND RETEST), oldest label first.

    A key is due when its hash draws it (`bucket(key, 'review-retest') < retest_percent`: by the key, so the draw
    never follows the answer), its live label is `retest_after_days` old (long enough to forget the first look),
    a current source still has its text, and that label has no live retest yet. A label one press gave a whole wave
    is never retested: that look judged the run, and a second look at one of its texts alone asks something else."""
    spec, fold = snap.spec, snap.fold
    if spec.retest_percent <= 0:
        return []
    before = (now or datetime.now(timezone.utc)).timestamp() - spec.retest_after_days * 86_400
    retested = {row["of"] for row in fold.retests.values()}  # one pass: stats are polled, and labels are many
    due: list[tuple[float, str]] = []
    for key, row in fold.labels.items():
        if rows.bucket(key, "review-retest") >= spec.retest_percent or "wave_id" in row or key not in snap.items.items:
            continue
        at = _at_seconds(row["at"]) if isinstance(row.get("at"), str) else None
        if at is not None and at <= before and row["answer_id"] not in retested:
            due.append((at, key))
    return [key for _at, key in sorted(due)]


def retest_card(snap: Snapshot, key: str) -> dict | None:
    """The `reviewRetestCardSchema` card for a labelled `key`, or None. BLIND: the text, the answer id it re-tests
    (`of`, which says nothing of what was answered), when the turn ended (for the conversation round it) and the
    OPEN halves the surface takes. Never the reference, Sonnet's or Laya's verdict, the stratum, or the first answer."""
    item, label = snap.items.items.get(key), snap.fold.labels.get(key)
    if item is None or label is None:
        return None
    text_chars = utf16_len(item.text)
    return {
        "item_id": f"{snap.spec.name}:retest:{key}",
        "key": key,
        "surface": snap.spec.name,
        "of": label["answer_id"],
        "text": utf16_tail(item.text, CARD_TEXT_MAX),
        "text_chars": text_chars,
        "truncated": text_chars > CARD_TEXT_MAX,
        "at": item.decision["at"] if item.decision else None,
        "needs": list(snap.spec.needs),
    }


def retest_cards(snap: Snapshot, limit: int, now: datetime | None = None) -> list[dict]:
    """The retest cards one deck page of `limit` cards carries: one per `RETEST_DEAL_EVERY` asked for (at least one
    while any is due), oldest label first. None on a stats-only read (`limit` 0)."""
    if limit <= 0:
        return []
    found = (retest_card(snap, key) for key in retest_due(snap, now)[: max(1, limit // RETEST_DEAL_EVERY)])
    return [c for c in found if c is not None]


def intra_rater(snap: Snapshot) -> dict:
    """The operator against themselves, from the retests: `{pairs, open, needs}`, each κ an `agreementSchema` block.

    A pair is a live retest that said OPEN or DONE and the label it re-tests, while that label still stands for its
    key: an undone or replaced label takes its retest out with it. `open` is κ on OPEN against DONE over every pair.
    `needs` is one κ per half (that class against the other three) over the pairs where BOTH looks drew a four-way
    class, since an OPEN nobody split, or a DONE from before READ, has no half to agree on. Nothing else reads a retest: not the
    labels, not a count, not any other κ (`review_log.py` owns why)."""
    fold = snap.fold
    latest = {row["of"]: row for row in fold.retests.values()}  # the last live retest per answer, as `retest_of`
    binary: list[tuple[bool, bool]] = []
    split: list[tuple[str, str]] = []
    for of, row in latest.items():
        label = fold.labels.get(row["key"])
        if label is None or label["answer_id"] != of or not isinstance(row.get("open"), bool):
            continue
        binary.append((row["open"], label["open"]))
        again, first = grade_of(row), grade_of(label)
        if again is not None and first is not None:
            split.append((again, first))

    def block(pairs: list[tuple[bool, bool]]) -> dict:
        return {**agreement.kappa_summary(pairs), "basis": "retest"}

    return {
        "pairs": len(binary),
        "open": block(binary),
        "needs": {name: block([(again == name, first == name) for again, first in split]) for name in snap.spec.needs},
    }


def edge_distance(meta: Mapping[str, Any], done_at: float) -> float:
    """How far p sits from its nearest cut, at the cursor's six decimals: the EDGE sort and its cursor read this one
    value, so a page's last row compares equal to itself."""
    return float(f"{abs(edge_of(meta['p'], meta['threshold'], done_at)[1]):.6f}")


def recent_cursor(order: str, key: str, meta: Mapping[str, Any], distance: float) -> str:
    """`RECENT_CURSOR` for one row of an `order` page."""
    place = f"{meta['at']}~{key}"
    return f"e~{distance:.6f}~{place}" if order == "edge" else f"n~{place}"


def parse_recent_cursor(token: str, order: str) -> tuple[float, str, str] | None:
    """(distance, at, key) from a cursor of `order` (distance 0.0 on `newest`); None when it is not one, or belongs
    to the other order (a cursor names a place in ONE ordering)."""
    match = RECENT_CURSOR.fullmatch(token)
    if match is None or (match["distance"] is not None) != (order == "edge"):
        return None
    return float(match["distance"] or 0.0), match["at"], match["key"]


def recent_rows(
    snap: Snapshot,
    limit: int,
    order: str = "newest",
    todo: bool = False,
    *,
    subject: tuple[str, ...] | None = None,
    since: str | None = None,
    offset: int = 0,
    after: str | None = None,
) -> tuple[list[dict], dict]:
    """(`reviewRecentRowSchema` rows, `reviewRecentSummarySchema`) for `limit` live verdicts, after the cursor
    `after`, then from `offset`.

    `newest` takes the newest; `edge` the ones nearest a cut (`edge_of`), ties newest first. `todo`
    leaves out every text the operator already answered BEFORE the page is cut, so grading the head
    of a page brings the next ungraded ones in rather than leaving a page of answers.

    `subject` keeps one rack session's verdicts, under its name now and the ones it had before (a
    verdict is recorded under the name of the moment), and `since` (a `utc_stamp`) only those at or
    after it: tmux reuses a `vc-N` name once its session is gone, and the older session's turn ends
    would otherwise read as this one's. The rack's per-session LABEL button sends both.

    `after` is how the REVIEW page's one-conversation pager walks on through the same ordering (older turns,
    or ones further from a cut) instead of dead-ending after the first page: `summary.next` names where this
    page's last row sits (`RECENT_CURSOR`), null once nothing follows it, and the next page is the rows
    strictly after that place. A place, not a count, because the pool moves between pages: a new verdict
    lands before the place (it is newer, or the text left its old spot), and an answer under `todo`, from
    this page or any other client, takes its row out of the pool wherever it sat. A count shifted with
    both, and an answer made elsewhere silently skipped a row. `offset` still skips that many rows after
    the place (the pager does not send it). `pool` stays the whole filtered count; the tally covers the
    emitted rows only.

    `mark` is what the rack drew, from the cuts the decision recorded; `band` is where the same p
    falls at today's done cut (`ReviewSurface.default_done_threshold` for a row that predates the
    band), so the list can grade the ✅ on turn ends the rack drew as 💬. Right and wrong are judged
    against `band`, and `edge` against the same cuts."""
    if offset < 0:  # a negative slice would count from the oldest end: review.py refuses it before here
        raise ValueError("recent_rows offset must be 0 or more")
    place = None if after is None else parse_recent_cursor(after, order)
    if after is not None and place is None:  # review.py refuses it before here
        raise ValueError("recent_rows after must be a cursor of this order")
    spec, current = snap.spec, snap.fold

    def done_now(meta: Mapping[str, Any]) -> float:
        return meta["done_threshold"] if meta["done_threshold"] is not None else spec.default_done_threshold

    floor = _at_number(since) if since else None
    picked = [
        pair
        for pair in snap.items.recent
        if not (todo and current.answered(pair[0]))
        and (subject is None or pair[1]["subject"] in subject)
        and (floor is None or _at_number(pair[1]["at"]) >= floor)
    ]
    distance = {key: edge_distance(meta, done_now(meta)) for key, meta in picked} if order == "edge" else {}
    if order == "edge":  # sorted() is stable, so equal distances keep the newest-first order
        picked = sorted(picked, key=lambda pair: distance[pair[0]])
    rest = picked
    if place is not None:
        # Strictly after the place, in the order's own terms: `recent` is (at, key) newest first, and EDGE sorts
        # by distance with that order breaking a tie.
        place_distance, place_at, place_key = place

        def follows(pair: tuple[str, dict]) -> bool:
            older = (pair[1]["at"], pair[0]) < (place_at, place_key)
            if order != "edge":
                return older
            return distance[pair[0]] > place_distance or (distance[pair[0]] == place_distance and older)

        rest = [pair for pair in picked if follows(pair)]
    page = rest[offset : offset + limit]
    out: list[dict] = []
    tally = {band: {"n": 0, "reviewed": 0, "open": 0} for band in BANDS}
    needs = dict.fromkeys(spec.needs, 0)
    skipped = 0
    for key, meta in page:
        found = card(snap, key)
        band = band_of(meta["p"], meta["threshold"], done_now(meta))
        cut, delta = edge_of(meta["p"], meta["threshold"], done_now(meta))
        live = current.live_answer(key) if current.answered(key) else None
        tally[band]["n"] += 1
        if live is not None and live["answer"] == "skip":
            skipped += 1
        elif live is not None:
            tally[band]["reviewed"] += 1
            tally[band]["open"] += 1 if live["open"] else 0
            if live.get("needs") in needs:
                needs[live["needs"]] += 1
        text = utf16_tail(meta["text"], CARD_TEXT_MAX)
        out.append(
            {
                "key": key,
                "decision_id": meta["decision_id"],
                "at": meta["at"],
                "subject": meta["subject"],
                "conversation": meta["conversation"],
                "p": meta["p"],
                "threshold": meta["threshold"],
                "done_threshold": meta["done_threshold"],
                "mark": band_of(meta["p"], meta["threshold"], meta["done_threshold"]),
                "band": band,
                "edge": {"cut": cut, "delta": round(delta, 4)},
                "model": meta["model"],
                "checkpoint": meta["checkpoint"],
                "text": text,
                "text_chars": utf16_len(meta["text"]),
                "stratum": found["stratum"] if found else None,
                "reference_open": found["reference"]["open"] if found else None,
                "great_example": current.examples.get(key, {}).get("great_example", False),
                "operator": review_log.answer_ref(live) if live is not None else None,
            }
        )
    summary = {
        "n": len(out),
        "skipped": skipped,
        "done_at": spec.default_done_threshold,
        "order": order,
        "todo": todo,
        "pool": len(picked),
        "offset": offset,
        "next": (
            recent_cursor(order, page[-1][0], page[-1][1], distance.get(page[-1][0], 0.0))
            if page and len(rest) > offset + limit
            else None
        ),
        **({"subject": subject[0]} if subject else {}),
        "bands": {
            band: {
                "n": t["n"],
                "reviewed": t["reviewed"],
                # right: the operator's answer matches the band (reply has no right answer: it said "unsure")
                "right": (t["reviewed"] - t["open"]) if band == "done" else t["open"] if band == "open" else None,
                "open": t["open"],
            }
            for band, t in tally.items()
        },
    }
    if needs:  # the OPEN answers split: how many turns really needed the operator (laya-attention § L3)
        summary["needs"] = needs
    return out, summary


def _agreement(pairs: list[tuple[bool, bool]], *, at_cut: bool = False) -> dict:
    """One `agreementSchema` block over `(the other labeller, the operator)` pairs: κ with its interval, and for a
    labeller that flags at a cut (Laya), its precision and recall there. The rules: owned by `agreement.py`."""
    block = {**agreement.kappa_summary(pairs), "basis": "sample"}
    if at_cut:
        block["at_cut"] = agreement.at_cut(pairs)
    return block


def _rate(pairs: list[tuple[bool, bool]], name: str, pool: int | None = None) -> dict:
    """One `rate_summary` (`{n, value, lo, hi}`, None under `agreement.RATE_MIN`), with its `pool` when it has one."""
    return {**({} if pool is None else {"pool": pool}), **agreement.rate_summary(pairs, name)}


def live_rates(snap: Snapshot) -> tuple[dict, dict]:
    """(Laya's precision on the ⚠️ the rack SHOWED, overall, on free rows and per landing class; its recall per
    audience), from the operator's grades of `live_sample` members. Below `agreement.RATE_MIN` graded a rate is None,
    which the row prints as '—'.

    NOT FROM DISMISSALS. `1 − dismissed / shown` reads near 1.0 whenever the operator seldom presses ✗: it
    measures how seldom they press, never whether the work was open. A grade says that. The 7-day reaction
    counts stay beside it as telemetry.

    ONLY THE UNIFORM SAMPLE. Every other lane of the deck is chosen by p (`uncertain` sits on a cut, `flagged` far over
    it), so a rate over those grades reads whichever lane was graded most. A member is a member by its key's hash
    (`is_live_member`), whatever stratum its answer recorded and whichever door graded it: the deck, RECENT, and the
    rack's ✗ NOT OPEN / ✓ STILL OPEN (the module doc: THE RACK'S DOOR).

    WHICH DECISION. A text can be judged more than once, and the grade is the text's. Precision takes its newest SHOWN
    decision, so a ⚠️ shown twice is one ⚠️ graded once; recall takes the decision the answer recorded (what its card
    showed), else the newest, flagged or not.

    WHERE IT LANDED: the landing record (`landing.py`), joined on the decision id. `free` (new + repeat) is the ⚠️ drawn
    in front of the operator, the number the row leads with; `held` drew ⏳, `retired` nothing, and a decision the
    record never saw is `unknown`, never guessed. Recall's audience is the row's: `free`, `background`, `children`.
    `pool` counts the shown ⚠️ there were to grade, so a rate always says how much of its pool it stands on."""
    spec, items, fold = snap.spec, snap.items, snap.fold
    pool = dict.fromkeys(LANDING_SPLIT, 0)
    said: dict[str, list[tuple[bool, bool]]] = {name: [] for name in LANDING_SPLIT}
    caught: dict[str, list[tuple[bool, bool]]] = {name: [] for name in AUDIENCES}
    for key, ids in items.decisions_of.items():
        if key not in items.items or not is_live_member(spec, key):
            continue  # a text no card can show is nobody's to grade
        answer = fold.labels.get(key)
        shown = next((items.verdicts[decision_id] for decision_id in ids if items.verdicts[decision_id].shown), None)
        if shown is not None:
            pool[shown.landing_class] += 1
            if answer is not None:
                said[shown.landing_class].append((True, answer["open"]))
        if answer is not None:
            judged = items.verdicts[answer["decision_id"] if answer.get("decision_id") in ids else ids[0]]
            caught[judged.audience].append((judged.flagged, answer["open"]))
    free = [pair for name in FREE_CLASSES for pair in said[name]]
    precision = {
        "basis": "live_sample",
        "min_graded": agreement.RATE_MIN,
        **_rate([pair for pairs in said.values() for pair in pairs], "precision", sum(pool.values())),
        "free": _rate(free, "precision", sum(pool[name] for name in FREE_CLASSES)),
        "by_class": {name: _rate(said[name], "precision", pool[name]) for name in LANDING_SPLIT},
    }
    return precision, {name: _rate(caught[name], "recall") for name in AUDIENCES}


def challenger_sides(snap: Snapshot) -> dict | None:
    """`reviewStatsSchema.challenger`: which side the operator took on the @challenger's graded disagreements, or None
    without a @challenger. What a promotion reads (the bar: the operator sided with the challenger on at least
    70% of at least 10).

    ONLY GRADES OF THE `challenger` STRATUM, and only of THIS challenger: an answer row dealt from that lane carries
    the challenger's own block (`review.py` `answer`), and the side is read off the row alone, so a later shadow pass
    or a relabel cannot move it. A grade that reached the same text through another door (the rack's menu, a card
    dealt before the shadow pass) carries no block and is not counted: those doors pick which texts get graded, and
    the rack's only ever shows the ones the champion flagged."""
    name = snap.items.challenger
    if name is None:
        return None
    graded = sided = 0
    for row in snap.fold.labels.values():
        block = row.get("challenger")
        if row.get("stratum") != "challenger" or not isinstance(block, dict) or block.get("name") != name:
            continue
        graded += 1
        sided += 1 if row["open"] is (block["p"] >= block["threshold"]) else 0
    return {"name": name, "graded": graded, "with_challenger": sided, "remaining": len(snap.remaining.get("challenger", ()))}


def door_waiting(snap: Snapshot) -> int:
    """How many cards of `DOOR_STRATA` wait on a text with a live verdict: what the phone's RECENT list can grade."""
    live = snap.items.decisions_of
    return sum(1 for name in DOOR_STRATA for key in snap.remaining.get(name, ()) if key in live)


def stats(snap: Snapshot, base: Path, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None) -> dict:
    """`reviewStatsSchema`. Pairs use what each answer RECORDED, so a relabel cannot move history;
    below its minimum n a metric is None, never 0."""
    spec, current = snap.spec, snap.fold
    live = list(current.live.values())
    counts = {answer: sum(1 for row in live if row["answer"] == answer) for answer in review_log.ANSWERS}
    # `open`: how many of a stratum's answers recorded OPEN, so each stratum's OPEN yield is read off its own grades
    # (the acquisition strata exist to find the short class; their yield is measured before it is quoted).
    by_stratum = {
        name: {"reviewed": 0, "remaining": len(snap.remaining.get(name, ())), "open": 0} for name in (*STRATA, RACK_STRATUM)
    }
    for row in live:
        name = row.get("stratum") if isinstance(row.get("stratum"), str) and len(row["stratum"]) <= 32 else "rest"
        tally = by_stratum.setdefault(name, {"reviewed": 0, "remaining": 0, "open": 0})
        tally["reviewed"] += 1
        tally["open"] += 1 if row.get("open") is True else 0

    labelled = [row for row in live if row["answer"] != "skip"]
    sample = [row for row in labelled if row.get("stratum") == "sample"]
    # (said, truth): the operator is the truth, so Laya's precision and recall at its cut read the right way round.
    vs_sonnet = [
        (row["against"]["open"], row["open"])
        for row in sample
        if row["against"].get("by") not in ("operator", "golden") and not is_laya_model(str(row["against"].get("by")))
    ]
    vs_laya = [
        (row["laya"]["p"] >= row["laya"]["threshold"], row["open"])
        for row in sample
        if isinstance(row.get("laya"), dict) and is_p(row["laya"].get("p")) and is_p(row["laya"].get("threshold"))
    ]

    since = rows.utc_stamp((now or datetime.now(timezone.utc)) - timedelta(days=PRECISION_WINDOW_DAYS))
    window = [actions for at, actions in snap.items.reactions if at >= since]
    shown = sum(1 for actions in window if "shown" in actions)
    dismissed = sum(1 for actions in window if "shown" in actions and "dismissed" in actions)
    precision, by_audience = live_rates(snap)

    def side(keys: frozenset[str]) -> tuple[int, int]:
        # By population, not by the stratum an answer recorded: `uncertain` and `live_sample` take members of both.
        answered = [row for row in labelled if row["key"] in keys]
        return len(answered), sum(1 for row in answered if row["open"])

    # The flagged side is stratified on the deck's own lanes. The deck deals near-cut flagged texts as `uncertain`
    # at a heavier weight than `flagged`, so the answered flagged texts are mostly near the cut, the ones least
    # likely to be really OPEN, and one pooled rate over them read tp low (20 near at 30% OPEN, 80 far at 90%:
    # 15 near and 5 far graded pooled to 45% for a true 78%). `challenger` is a third such lane: the flagged texts
    # the @challenger would not have flagged, dealt at its own weight. Each part is scaled by its own size, and
    # needs RECALL_MIN_PER_SIDE answers, or all of it when it holds fewer (a whole census is exact).
    parts = [(len(keys), *side(keys)) for keys in (snap.items.flagged_near, snap.items.flagged_far, snap.items.flagged_rival)]
    n_f = sum(n for _size, n, _a in parts)
    n_u, a_u = side(snap.items.unflagged_sampled)
    recall = None
    if (
        n_f >= RECALL_MIN_PER_SIDE
        and n_u >= RECALL_MIN_PER_SIDE
        and all(n >= min(RECALL_MIN_PER_SIDE, size) for size, n, _a in parts)
    ):
        tp = sum(size * a / n for size, n, a in parts if n)
        fn = snap.items.unflagged_total * a_u / n_u
        recall = round(tp / (tp + fn), 3) if tp + fn > 0 else None

    last = live[-1] if live else None
    remaining = snap.remaining
    return {
        "surface": spec.name,
        "label": spec.label,
        "question": clip(spec.question, 400),
        "to_review": sum(len(remaining.get(name, ())) for name in TO_REVIEW),
        "backlog": len(remaining.get("rest", ())),
        "reviewed": len(live),
        "agree": counts["agree"],
        "flip": counts["flip"],
        "skip": counts["skip"],
        "by_stratum": by_stratum,
        "kappa_min_pairs": agreement.KAPPA_MIN_PAIRS,
        "operator_vs_sonnet": _agreement(vs_sonnet),
        "operator_vs_laya": _agreement(vs_laya, at_cut=True),
        # `shown` / `dismissed`: the window's reaction counts, kept as telemetry; the precision is `live_rates`'.
        "precision": {"window_days": PRECISION_WINDOW_DAYS, "shown": shown, "dismissed": dismissed, **precision},
        "recall_est": {"n_flagged": n_f, "n_unflagged": n_u, "value": recall, "by_audience": by_audience},
        "sweep_ready": snap.readiness or sweep_readiness(base, spec.name, current, loaded=loaded),
        "deficit": snap.deficit,
        "three_way": snap.grades,
        "retest": {
            "percent": spec.retest_percent,
            "after_days": spec.retest_after_days,
            "due": len(retest_due(snap, now)),
            **intra_rater(snap),
        },
        "challenger": challenger_sides(snap),
        "door": {"waiting": door_waiting(snap), "min": DOOR_MIN},
        # What each band deal would deal (`band_waiting`): the deck's 💬 button draws while `reply` holds any.
        "band_waiting": {band: len(band_waiting(snap, band)) for band in BANDS},
        "last_answer_id": last["answer_id"] if last else None,
        "last_answer_at": last["at"] if last else None,
        # Whether that answer was one of a wave's, so the deck's UNDO after a reload takes the whole press back.
        "last_answer_wave": bool(last and isinstance(last.get("wave_id"), str)),
    }
