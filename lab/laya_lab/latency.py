"""`laya-lab latency agent-open-work`: why a rack verdict takes the time it takes, read from the decision ledger.

    laya-lab latency agent-open-work              # the decisions of the last DEFAULT_DAYS days
    laya-lab latency agent-open-work --days 0     # everything the ledger keeps

A read: no model answers anything here, so it takes no slot of the resource lease. A scheduler can run it every
night; its report is `evals/latency-<suite>-<stamp>.json` (`lab_files.latency_report_name`) and the rack reads it as
the LAYA row's LATENCY line.

**Latency.** Every decision row carries `ms`, the plugin's wall time around laya-serve's answer. A live median can sit
far above a benchmark's, and one number cannot say why. So `ms` is cut four ways, each bucket with its n, p50 and p90:

- `by_length`: the judged state's characters (`fit_hand_off` caps a v2 state at `STATE_MAX_CHARS`; longer ones are
  older rows, and a text the ledger cut at its own 8,000 characters is longer still).
- `by_load`: the machine's load per core when the decision was answered (the row's `load`; `ledger.load_per_core`).
- `by_start`: cold against warm. Cold is a model's first answer since laya-serve started (the row's `cold`). A row
  from before the ledger recorded it is read by the gap rule instead: cold when the previous decision on ANY surface
  (one laya-serve answers them all) was at least `paths.IDLE_STOP_SECONDS` earlier, since laya-serve had stopped
  itself by then. `cold_source` counts how many rows each rule read, and how many neither could.
- `by_gap`: the seconds since that previous decision. A GPU that sat idle for a minute is slower than one that
  answered a second ago long before laya-serve stops, and the rack asks in bursts.

**Truncation.** Laya reads one window (512 tokens for every checkpoint so far) and keeps the HEAD of a state that
does not fit, so an overflow costs the hand-off's last lines. `truncation` tokenizes every reviewable text as it is
asked (`decide_state` of it, the suite's own builder) with the SERVED checkpoint's tokenizer and the question's own
head, through laya's `build_sequence`: the code that builds the model's input, not a character guess. It reports how
many states overflow and by how many tokens. This is the residual after `fit_hand_off`: QUESTION_VERSION 2 fits a
long hand-off to the window first, and the ledger's `state_truncated` is its 8,000-character text cap, which
says nothing about Laya's window. `priced` then reads the overflow in accuracy: the served model's scores
(`data/<stem>-laya-p.jsonl`) against the held-out labels a sweep uses, for the states that overflow and the ones that
fit. No overflow prices nothing.

Exit codes: 0 the report was written (read its verdict: `lab_files.run_verdict`) · 1 an error before it was.
"""

from __future__ import annotations

import bisect
import calendar
import json
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab import heldout, infer
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES, Suite
from laya_lab.registry import resolve_model, served_threshold
from laya_bridge import agreement, lab_files, ledger, review_data, rows
from laya_bridge.files import is_count, is_number
from laya_bridge.paths import EVALS_DIRNAME, IDLE_STOP_SECONDS
from laya_bridge.review_surfaces import REVIEW_SURFACES
from tldrack import open_work

KIND = "latency"
DEFAULT_DAYS = 14
#: "Fast": the benchmark's side of the gap (a warm decision on a short state is tens of milliseconds).
FAST_MS = 100
#: `(upper edge, label)` per bucket, then the label past the last edge and the label for a row with no value.
LENGTH_BUCKETS = (
    ((200, "0-200"), (400, "201-400"), (800, "401-800"), (open_work.STATE_MAX_CHARS, "801-1300")),
    "1301+",
    "no text",
)
LOAD_BUCKETS = (((0.5, "<0.5"), (1.0, "0.5-1"), (2.0, "1-2")), "2+", "unrecorded")
GAP_BUCKETS = (((10, "<10 s"), (60, "10-60 s"), (IDLE_STOP_SECONDS, "1-15 min")), "15 min+", "first")
COLD, WARM = "cold", "warm"
CONFIG_FILE = "rl_agent_config.json"
TOKENIZER_DIRNAME = "tokenizer"

#: `meter(state)` → (the state's tokens, how many of them fall outside the window).
Meter = Callable[[Any], tuple[int, int]]


@dataclass(frozen=True)
class Timed:
    """One decision as the latency read sees it. `chars` is None for a hash-only row; `cold` None when neither rule
    could say (`cold_recorded` tells which rule did)."""

    at: str
    ms: int
    chars: int | None
    load: float | None
    gap_s: int | None
    cold: bool | None
    cold_recorded: bool


# ── reading the ledger ────────────────────────────────────────────────────────


def _epoch(at: str) -> int | None:
    try:
        return calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _decisions(base: Path, surface: str, since: str | None) -> Iterable[dict]:
    return (row for row in ledger.iter_rows(base, surface, since=since) if row["kind"] == "decision")


def read_decisions(base: Path, suite: Suite, since: str | None) -> list[Timed]:
    """Every decision on `suite.surface` at or after `since`, oldest first, each with the gap to the decision before
    it on ANY surface. The gaps are read from everything the ledger keeps, not from the window: the window's first
    row is cold or warm by what came before it."""
    answered = sorted(
        stamp
        for surface in ledger.surfaces(base)
        for row in _decisions(base, surface, None)
        if (stamp := _epoch(row["at"])) is not None
    )
    out: list[Timed] = []
    for row in _decisions(base, suite.surface, since):
        stamp = _epoch(row["at"])
        if stamp is None or not is_count(row.get("ms")):
            continue
        # This row is one of the entries at `stamp`: the entry before the last of them is a decision in the same
        # second (gap 0) or the newest earlier one.
        before = bisect.bisect_right(answered, stamp) - 2
        gap = stamp - answered[before] if before >= 0 else None
        recorded = row.get("cold") if isinstance(row.get("cold"), bool) else None
        state = row.get("state")
        chars = None if not isinstance(state, str) else ledger.STATE_MAX + 1 if row.get("state_truncated") else len(state)
        out.append(
            Timed(
                at=row["at"],
                ms=row["ms"],
                chars=chars,
                load=float(row["load"]) if is_number(row.get("load")) and row["load"] >= 0 else None,
                gap_s=gap,
                cold=recorded if recorded is not None else (None if gap is None else gap >= IDLE_STOP_SECONDS),
                cold_recorded=recorded is not None,
            )
        )
    return out


# ── the cuts ──────────────────────────────────────────────────────────────────


def bucket_of(value: float | None, spec: tuple[tuple[tuple[float, str], ...], str, str], *, inclusive: bool) -> str:
    """The label of `value` under a `*_BUCKETS` spec; `inclusive` puts a value AT an edge in that edge's bucket."""
    edges, over, missing = spec
    if value is None:
        return missing
    for edge, label in edges:
        if value < edge or (inclusive and value == edge):
            return label
    return over


def spread(values: Sequence[int]) -> dict[str, Any]:
    """`{n, p50, p90}` of some `ms` values (None for both when there are none)."""
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p90": None}
    return {
        "n": len(ordered),
        "p50": round(agreement.percentile(ordered, 0.5), 1),
        "p90": round(agreement.percentile(ordered, 0.9), 1),
    }


def cut_by(
    timed: Sequence[Timed], label_of: Callable[[Timed], str | None], order: Sequence[str], *, keep_empty: bool = False
) -> list[dict[str, Any]]:
    """One `{bucket, n, p50, p90}` per label in `order` that holds a row (every label with `keep_empty`)."""
    held: dict[str, list[int]] = {label: [] for label in order}
    for row in timed:
        label = label_of(row)
        if label is not None:
            held[label].append(row.ms)
    return [{"bucket": label, **spread(values)} for label, values in held.items() if values or keep_empty]


def _labels(spec: tuple[tuple[tuple[float, str], ...], str, str]) -> list[str]:
    edges, over, missing = spec
    return [*(label for _edge, label in edges), over, missing]


def measure(timed: Sequence[Timed]) -> dict[str, Any]:
    """The latency half of the report: the overall spread and the four cuts (the module note owns what each is)."""
    ordered = sorted(row.ms for row in timed)
    overall = {
        **spread(ordered),
        "p95": round(agreement.percentile(ordered, 0.95), 1) if ordered else None,
        "max": ordered[-1] if ordered else None,
        "le_100": round(sum(1 for ms in ordered if ms <= FAST_MS) / len(ordered), 4) if ordered else None,
    }
    return {
        "latency_ms": overall,
        "by_length": cut_by(timed, lambda row: bucket_of(row.chars, LENGTH_BUCKETS, inclusive=True), _labels(LENGTH_BUCKETS)),
        "by_load": cut_by(timed, lambda row: bucket_of(row.load, LOAD_BUCKETS, inclusive=False), _labels(LOAD_BUCKETS)),
        "by_start": cut_by(
            timed, lambda row: None if row.cold is None else COLD if row.cold else WARM, (COLD, WARM), keep_empty=True
        ),
        "cold_source": {
            "recorded": sum(1 for row in timed if row.cold_recorded),
            "gap_rule": sum(1 for row in timed if not row.cold_recorded and row.cold is not None),
            "unknown": sum(1 for row in timed if row.cold is None),
        },
        "by_gap": cut_by(timed, lambda row: bucket_of(row.gap_s, GAP_BUCKETS, inclusive=False), _labels(GAP_BUCKETS)),
    }


# ── truncation ────────────────────────────────────────────────────────────────


def builtin_folder(name: str) -> Path:
    """The newest Hub snapshot ON DISK of a built-in checkpoint that holds its tokenizer. Never a download: this
    command is a read, and laya-serve has loaded the checkpoint from that cache already."""
    from huggingface_hub import constants
    from laya.router import resolve_model_spec

    spec = resolve_model_spec(name)
    if spec is None:
        raise LookupError(f"{name} is not a built-in checkpoint")
    repo, subfolder = spec
    snapshots = Path(constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "snapshots"
    found = []
    for snapshot in sorted(snapshots.iterdir()) if snapshots.is_dir() else ():
        folder = snapshot / subfolder if subfolder else snapshot
        if (folder / TOKENIZER_DIRNAME).is_dir() and (folder / CONFIG_FILE).is_file():
            found.append(folder)
    if not found:
        raise LookupError(f"no snapshot of {repo} on disk holds a tokenizer")
    return max(found, key=lambda folder: folder.stat().st_mtime)


def open_meter(base: Path, suite: Suite) -> tuple[Meter, dict[str, Any]]:
    """`(meter, {model, checkpoint, digest, window, room})` for the SERVED model of `suite`: the checkpoint's own
    tokenizer and window (`rl_agent_config.json`), and the suite's question as laya lays its head out. `room` is the
    state tokens that question leaves inside `window`. Loads a tokenizer only, never weights."""
    target, path = resolve_model(base, suite.surface, "served")
    folder = Path(path) if path is not None else builtin_folder(target)
    if not (folder / TOKENIZER_DIRNAME).is_dir():
        raise LookupError(f"{folder.name} holds no {TOKENIZER_DIRNAME}/ folder")
    config = json.loads((folder / CONFIG_FILE).read_text(encoding="utf-8"))
    from laya.agent import Agent, _load_tokenizer
    from laya.common import build_sequence, state_room

    tokenizer = _load_tokenizer(str(folder / TOKENIZER_DIRNAME), config)
    window, head = int(config.get("max_len", 512)), int(config.get("head_max_len", 192))
    question = Agent._to_internal(dict(suite.question))

    def meter(state: Any) -> tuple[int, int]:
        *_sequence, stats = build_sequence(
            tokenizer, state, question, window, head, truncate_left=isinstance(state, list), return_truncation_stats=True
        )
        return stats["state_tokens"], stats["state_tokens_dropped"]

    return meter, {
        "model": target,
        "checkpoint": folder.name if path is not None else None,
        "digest": infer.checkpoint_digest(target, path),
        "window": window,
        "room": state_room(tokenizer, question, window, head),
    }


def price(base: Path, name: str, overflowed: set[str]) -> dict[str, Any]:
    """The overflow in accuracy: `{cut, fits: {labelled, accuracy}, overflow: {labelled, accuracy}, delta}` over the
    held-out labelled rows a sweep would use (`heldout.labelled_rows`, merged labels) that the served checkpoint has
    scored. `delta` is overflow minus fits, None until each side has `agreement.RATE_MIN` rows. `note` when the
    served fine-tune has no training record, so its trained rows could not be held out."""
    suite = SUITES[name]
    spec = REVIEW_SURFACES[suite.surface]
    target, path = resolve_model(base, suite.surface, "served")
    checkpoint = Path(path).name if path is not None else None
    scores = {
        row["key"]: row["p"]
        for row in lab_files.read_scores(base, spec.stem)[0]
        if row["model"] == target and row["checkpoint"] == checkpoint
    }
    cut = served_threshold(base, suite.surface, target, code=suite.threshold)[0]
    sides: dict[str, list[tuple[bool, bool]]] = {"fits": [], "overflow": []}
    clean = heldout.labelled_rows(base, name, "merged")
    for key, is_open, _text in clean.rows:
        if key in scores:
            sides["overflow" if key in overflowed else "fits"].append((scores[key] >= cut, is_open))
    out: dict[str, Any] = {
        "cut": cut,
        **{side: {"labelled": len(pairs), "accuracy": agreement.agree_rate(pairs)} for side, pairs in sides.items()},
    }
    ready = all(len(pairs) >= agreement.RATE_MIN for pairs in sides.values())
    out["delta"] = round(out["overflow"]["accuracy"] - out["fits"]["accuracy"], 3) if ready else None
    if clean.reason and path is not None:  # a fine-tune with no training record: rows it trained on may be in here
        out["note"] = clean.reason
    return out


def truncation(
    base: Path, name: str, *, opener: Callable[[Path, Suite], tuple[Meter, dict[str, Any]]] = open_meter
) -> dict[str, Any]:
    """The truncation half of the report (the module note owns what it measures). `state`: `ran`, `skipped` (no
    reviewable text yet) or `failed` (the tokenizer could not be opened; `why` says so). Keys and numbers only."""
    suite = SUITES[name]
    spec = REVIEW_SURFACES[suite.surface]
    texts = review_data.texts(base, suite.surface)
    if not texts:
        return {"state": "skipped", "why": "no reviewable text yet"}
    try:
        meter, tokenizer = opener(base, suite)
        measured = {key: meter(suite.build_state({spec.text_field: text})) for key, text in texts.items()}
    except Exception as error:  # noqa: BLE001 (transformers and laya raise their own; the latency half still stands)
        return {"state": "failed", "why": infer.why_line(error), "error": type(error).__name__}
    tokens = sorted(total for total, _dropped in measured.values())
    dropped = sorted(lost for _total, lost in measured.values() if lost > 0)
    overflowed = {key for key, (_total, lost) in measured.items() if lost > 0}
    out: dict[str, Any] = {
        "state": "ran",
        "tokenizer": tokenizer,
        "states": len(measured),
        "overflowed": len(overflowed),
        "share": round(len(overflowed) / len(measured), 4),
        "tokens": {
            "p50": round(agreement.percentile(tokens, 0.5), 1),
            "p90": round(agreement.percentile(tokens, 0.9), 1),
            "max": tokens[-1],
        },
        "dropped": {"p50": round(agreement.percentile(dropped, 0.5), 1), "max": dropped[-1]} if dropped else None,
        "overflowed_keys": sorted(overflowed)[:50],
    }
    try:
        out["priced"] = price(base, name, overflowed)
    except SystemExit as stop:  # a label source the lab refuses to read
        out["priced"] = {"why": infer.why_line(stop)}
    return out


# ── the command ───────────────────────────────────────────────────────────────


def run(
    name: str,
    base: Path,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    opener: Callable[[Path, Suite], tuple[Meter, dict[str, Any]]] = open_meter,
) -> int:
    suite = SUITES[name]
    now = now or datetime.now(timezone.utc)
    since = rows.utc_stamp(now - timedelta(days=days)) if days > 0 else None
    timed = read_decisions(base, suite, since)
    report = {
        "kind": KIND,
        "suite": name,
        "surface": suite.surface,
        "question_version": suite.question_version,
        "at": rows.utc_stamp(now),
        "run_id": secrets.token_hex(6),
        "window": {
            "days": days if days > 0 else None,
            "since": since,
            "first": timed[0].at if timed else None,
            "last": timed[-1].at if timed else None,
        },
        **measure(timed),
        "truncation": truncation(base, name, opener=opener),
        "laya_version": infer.laya_version(),
        **lab_files.eval_run_field(),
    }
    out = base / EVALS_DIRNAME / lab_files.latency_report_name(name, lab_files.report_stamp(now))
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
    for line in report_lines(report):
        print(line)
    print(f"  report → {out}")
    return 0


def _cut_line(title: str, buckets: list[dict[str, Any]]) -> str:
    return f"  {title:<8} " + (
        " · ".join(f"{b['bucket']} {b['p50']} / {b['p90']} (n {b['n']})" for b in buckets if b["n"]) or "no rows"
    )


def report_lines(report: dict[str, Any]) -> list[str]:
    """Names and numbers only: no text from the operator's files."""
    overall, window = report["latency_ms"], report["window"]
    span = f"{window['days']} d" if window["days"] else "everything the ledger keeps"
    lines = [f"[laya-lab] latency {report['suite']} · {overall['n']} decisions · {span}"]
    if overall["n"]:
        lines.append(
            f"  ms       p50 {overall['p50']} · p90 {overall['p90']} · p95 {overall['p95']} · max {overall['max']} · "
            f"≤{FAST_MS} ms {overall['le_100']:.1%}"
        )
        lines.append("           buckets read p50 / p90 ms")
        lines.append(_cut_line("length", report["by_length"]))
        lines.append(_cut_line("load", report["by_load"]))
        source = report["cold_source"]
        lines.append(
            _cut_line("start", report["by_start"])
            + f" · {source['recorded']} recorded, {source['gap_rule']} by the gap rule, {source['unknown']} unknown"
        )
        lines.append(_cut_line("gap", report["by_gap"]))
    cut = report["truncation"]
    if cut["state"] != "ran":
        lines.append(f"  window   {cut['state']}: {cut['why']}")
        return lines
    tokenizer = cut["tokenizer"]
    lines.append(
        f"  window   {cut['overflowed']} of {cut['states']} states overflow ({cut['share']:.1%}) · room "
        f"{tokenizer['room']} of {tokenizer['window']} tokens · state tokens p50 {cut['tokens']['p50']} / p90 "
        f"{cut['tokens']['p90']} / max {cut['tokens']['max']}"
        + (f" · dropped p50 {cut['dropped']['p50']} / max {cut['dropped']['max']}" if cut["dropped"] else "")
    )
    priced = cut["priced"]
    if "why" in priced:
        lines.append(f"  priced   not priced: {priced['why']}")
    elif cut["overflowed"]:
        fits, over = priced["fits"], priced["overflow"]
        lines.append(
            f"  priced   accuracy at {priced['cut']}: fits {fits['accuracy']} (n {fits['labelled']}) · overflow "
            f"{over['accuracy']} (n {over['labelled']})"
            + (f" · Δ {priced['delta']:+.3f}" if priced["delta"] is not None else " · too few labelled to compare")
        )
    return lines
