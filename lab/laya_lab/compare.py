"""`laya-lab compare`: several decision voters side by side on one surface's question.

    laya-lab compare agent-open-work                                     # served Laya vs base Laya
    laya-lab compare agent-open-work --voter laya:served --voter other=http://127.0.0.1:8010
    laya-lab compare agent-open-work --voter laya:served --voter laya:challenger --voter floor   # the nightly pack's set

A voter is `laya:<served|challenger|english|checkpoint folder>` (in process, through `infer.open_router`),
`<name>=<loopback URL>`: any `/v1/systemone` server (`infer.EndpointRouter`), or `floor`: the promotion gate's TF-IDF + logistic floor (`floor.py`), trained on the
SERVED checkpoint's own training rows, so the held-out set is held out from it too (skipped, with the reason in the
report's `notes`, while no fine-tune is served). Every voter answers the SAME question object and state builder the
surface serves (`golden.SUITES`), on two sets:
- golden: the public cases, plus the private ones the served fine-tune never trained on;
- held-out: labelled turn ends the served fine-tune never trained on (`heldout.labelled_rows`, `--labels`).
With two voters or more, a `mean` voter (the average p) answers whether one more voter helps, and a `stack` voter (a
logistic regression over the voters' logits, scored out of fold on the set itself: `stack.py`) whether they make
different mistakes: its paired ΔAUC against the best single voter, and per pair of voters, of the rows one gets wrong
at its own cut, how many the other gets right. Measurement only: nothing registers or serves a stack.

Per voter and set: AUC (ranking at no cut — THE number between voters calibrated differently),
Brier, ECE, the result at its live cut (a Laya voter's served cut, else the code's), the cost-rule
cut on a wide grid (IN-SAMPLE on that set: a hint, never applied), and warm latency per decision;
then pairwise agreement. Keys, ids and numbers only, like every laya-lab readout. The report lands
in `evals/` under a name no golden or held-out reader matches (`lab_files.compare_report_name`); the rack
reads it as its own COMPARE line, and `laya-lab verdict compare` reads its verdict from it.
Exit 0 · 1 a voter that could not answer · 3 a voter spec refused.
"""

from __future__ import annotations

import json
import re
import secrets
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import claude_voter
from laya_lab import floor as floor_voter
from laya_lab import infer
from laya_lab import stack as stack_voter
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES, Suite, counted_public, dataset_version, load_suites
from laya_lab.heldout import labelled_rows, resolve_labels
from laya_lab.metrics import at_cut, auc, brier, ece, fmt_num, pick_cut
from laya_lab.open_work_data import require_sources
from laya_lab.registry import resolve_model, served_model, served_threshold
from laya_bridge import lab_files, rows
from laya_bridge.paths import EVALS_DIRNAME
from laya_bridge.review_data import CleanRows

REFUSED = 3
LAYA = "laya:"
MEAN = "mean"
STACK = stack_voter.NAME
FLOOR = "floor"
DEFAULT_VOTERS = ("laya:served", "laya:english")
#: Wider than the sweep's 0.30–0.90: a voter that runs hot (Mica) only separates near 0.95.
GRID = tuple(round(0.05 * i, 2) for i in range(1, 20))
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")


class Refused(ValueError):
    """A voter spec this command will not run (exit 3)."""


class Timed:
    """A router that records each `predict`'s wall time, so a voter's latency is its own calls only."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.ms: list[float] = []

    @property
    def loaded_revisions(self) -> dict:
        return getattr(self.inner, "loaded_revisions", None) or {}

    def predict(self, state: Any, questions: Mapping[str, Any], model: str | None = None) -> dict:
        start = time.perf_counter()
        try:
            return self.inner.predict(state, questions, model=model)
        finally:
            # A votes file answers with what its vote cost when it was asked (claude_voter.py § The file).
            recorded = getattr(self.inner, "last_ms", None)
            self.ms.append((time.perf_counter() - start) * 1000 if recorded is None else recorded)


@dataclass
class Voter:
    name: str
    kind: str  # "laya" | "endpoint"
    model: str
    router: Timed
    live_cut: float
    cut_source: str
    url: str | None = None
    #: `infer.checkpoint_digest` of a Laya voter (None for an endpoint or the floor): the report's key, with the device.
    digest: str | None = None
    probabilities: dict[str, list[float]] = field(default_factory=dict)
    latency: dict[str, list[float]] = field(default_factory=dict)


def parse_spec(spec: str) -> tuple[str, str]:
    """`("laya", model)`, `("floor", "floor")` or `(name, target)`: a loopback URL, or `votes:<model>[/<arm>]` (a
    Claude model's recorded votes: claude_voter.py). Refuses a malformed spec and a URL off this machine."""
    if spec == FLOOR:
        return FLOOR, FLOOR
    if spec.startswith(LAYA):
        model = spec[len(LAYA) :]
        if not model:
            raise Refused(f"{spec!r}: laya:<served|english|checkpoint folder>")
        return "laya", model
    name, sep, url = spec.partition("=")
    if not sep or not NAME.fullmatch(name) or name in (MEAN, STACK, FLOOR) or name.startswith("laya"):
        raise Refused(f"{spec!r}: <name>=<url>, name in [a-z0-9_.-], not 'mean', 'stack', 'floor' or 'laya…'")
    if url.startswith(claude_voter.VOTES):
        try:
            claude_voter.parse_votes(url)
        except ValueError as error:
            raise Refused(str(error)) from None
        return name, url
    if not infer.is_loopback_url(url):
        raise Refused(f"{url} is not a loopback URL: an endpoint voter never sends a text off this machine")
    return name, url


def endpoint_router(base: Path, suite: Suite, target: str) -> Any:
    """The router of a `(name, target)` spec: the votes file's reader, or the loopback server's client."""
    if target.startswith(claude_voter.VOTES):
        return claude_voter.VotesRouter(base, suite, *claude_voter.parse_votes(target))
    return infer.EndpointRouter(target)


def floor_of(base: Path, suite: Suite) -> tuple[Voter | None, str | None]:
    """The `floor` voter, trained on the served checkpoint's own rows, or `(None, why)` when it cannot train."""
    _served, path = resolve_model(base, suite.surface, "served")
    if path is None:
        return None, "no fine-tune is served: the floor trains on the served checkpoint's own rows"
    texts, labels = floor_voter.checkpoint_rows(base, Path(path).name)
    try:
        fitted = floor_voter.fit(texts, labels, suite.false_warning_cost)
    except ValueError as error:
        return None, str(error)
    return Voter(FLOOR, FLOOR, floor_voter.NAME, Timed(floor_voter.FloorRouter(fitted)), fitted.cut, "fit"), None


def build_voters(
    base: Path, suite: Suite, specs: list[str], device: str, router: Any = None, notes: dict[str, str] | None = None
) -> list[Voter]:
    """The voters `specs` name, in order. A voter that cannot be built for a reason of the data, not the spec (the
    floor with nothing to train on), is left out and its reason goes into `notes`."""
    parsed = [parse_spec(spec) for spec in specs]
    laya_targets: dict[str, Path | None] = {}
    for kind, model in parsed:
        if kind == "laya":
            target, path = resolve_model(base, suite.surface, model)
            laya_targets[target] = path
    shared = None
    if laya_targets:
        shared = router if router is not None else infer.open_router(base, device, laya_targets)
    voters: list[Voter] = []
    seen: set[str] = set()
    for (kind, value), _spec in zip(parsed, specs):
        if kind == FLOOR:
            voter, why = floor_of(base, suite)
            if voter is None:
                if notes is not None:
                    notes[FLOOR] = why or "the floor could not train"
                continue
        elif kind == "laya":
            target, path = resolve_model(base, suite.surface, value)
            name = f"{LAYA}{target}"
            cut, source = served_threshold(base, suite.surface, target, code=suite.threshold)
            voter = Voter(name, "laya", target, Timed(shared), cut, source, digest=infer.checkpoint_digest(target, path))
        else:
            voter = Voter(kind, "endpoint", kind, Timed(endpoint_router(base, suite, value)), suite.threshold, "code", url=value)
        if voter.name in seen:
            continue  # `laya:served` is `laya:english` until a fine-tune is registered
        seen.add(voter.name)
        voters.append(voter)
    return voters


def golden_rows(base: Path, name: str, suite: Suite) -> list[tuple[str, Any, bool]]:
    """`(id, state, expected)`: the public cases that count (`counted_public`), then the private ones the served
    fine-tune never trained on."""
    public, private = load_suites(base, name)
    public, _provisional = counted_public(base, suite.surface, public)
    trained, _unrecorded = rows.trained_keys(base, suite.fine_tune)
    clean_private = [
        case for case in private if rows.state_key(case["state"]["hand_off"]) not in trained and case.get("key") not in trained
    ]
    return [(case["id"], suite.build_state(case["state"]), case["expected"]) for case in (*public, *clean_private)]


def _labelled(
    base: Path, name: str, suite: Suite, labels: str, *, in_sample: bool
) -> tuple[tuple[str, CleanRows] | None, str | None]:
    """`((label source, clean rows), None)`, or `(None, why)` when a label source cannot be read."""
    try:
        fold = require_sources(base, suite.surface)
    except SystemExit as stop:
        return None, str(stop)
    resolved = resolve_labels(labels, base, suite.surface, fold)
    return (resolved, labelled_rows(base, name, resolved, in_sample=in_sample, fold=fold)), None


def heldout_raw(
    base: Path, name: str, suite: Suite, labels: str
) -> tuple[list[tuple[str, dict[str, Any], bool]], dict[str, Any]]:
    """`(key, raw state, expected)` rows the served fine-tune never trained on, and how they were chosen. A raw state
    is the text and the request that opened its turn (`open_work_data.read_requests`), so a voter can build the state
    of any question version from it. No rows (and the reason) when the labels or the training record cannot say what
    is held out."""
    found, skipped = _labelled(base, name, suite, labels, in_sample=False)
    if found is None:
        return [], {"skipped": skipped}
    resolved, clean = found
    meta: dict[str, Any] = {"labels": resolved, "excluded": clean.excluded}
    if clean.reason is not None and served_model(base, suite.surface) != "english":
        return [], {**meta, "skipped": clean.reason}
    from laya_lab.open_work_data import read_requests

    requests = read_requests(base)
    return [(key, {"hand_off": text, "request": requests.get(key)}, is_open) for key, is_open, text in clean.rows], meta


def heldout_rows(base: Path, name: str, suite: Suite, labels: str) -> tuple[list[tuple[str, Any, bool]], dict[str, Any]]:
    """`heldout_raw`, each state built at the suite's own question version."""
    raw, meta = heldout_raw(base, name, suite, labels)
    return [(key, suite.build_state(state), is_open) for key, state, is_open in raw], meta


def was_trained(key: str, state: Any, trained: frozenset[str]) -> bool:
    """Whether a labelled text is in a training record, by its label key OR by the key of the state it
    trained as. A record written before train rows carried their key holds `state_key(decide_state(text))`
    only, and `fit_hand_off` shortens a long text, so its label key is not in that record."""
    return key in trained or rows.state_key(state) in trained


def untrained_rows(
    base: Path, name: str, suite: Suite, labels: str, trained: frozenset[str]
) -> tuple[list[tuple[str, Any, bool]], dict[str, Any]]:
    """`(key, state, expected)` labelled turn ends no key in `trained` covers (the promotion gate passes the
    candidate's and the served model's records: rows NEITHER trained on), and how they were chosen. Golden
    and textless rows are out by the shared rule (`review_data.clean_label_rows`)."""
    found, skipped = _labelled(base, name, suite, labels, in_sample=True)
    if found is None:
        return [], {"skipped": skipped}
    resolved, clean = found
    kept, dropped = [], 0
    for key, is_open, text in clean.rows:
        state = suite.build_state({"hand_off": text})
        if was_trained(key, state, trained):
            dropped += 1
        else:
            kept.append((key, state, is_open))
    return kept, {"labels": resolved, "excluded": {**clean.excluded, "trained": dropped}}


def voter_scores(
    probabilities: list[float], expected: list[bool], live_cut: float, fp_cost: float, latency: list[float]
) -> dict[str, Any]:
    grid = [at_cut(probabilities, expected, cut) for cut in GRID]
    best = pick_cut(grid, fp_cost)
    return {
        "auc": auc(probabilities, expected),
        "brier": brier(probabilities, expected),
        "ece": ece(probabilities, expected),
        "at_live": at_cut(probabilities, expected, live_cut),
        "best_in_sample": next(entry for entry in grid if entry["threshold"] == best["at"]) | {"cost": best["cost"]},
        "latency_ms": ({"p50": round(statistics.median(latency), 1), "max": round(max(latency), 1)} if latency else None),
    }


def pearson(a: list[float], b: list[float]) -> float | None:
    try:
        return round(statistics.correlation(a, b), 3)
    except statistics.StatisticsError:  # fewer than two rows, or a voter that never moved
        return None


def agreement(probabilities: dict[str, list[float]], cuts: dict[str, float]) -> list[dict[str, Any]]:
    """Every pair: the share of rows where both said the same at their OWN live cuts, and r of their p."""
    names = list(probabilities)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            pa, pb = probabilities[a], probabilities[b]
            same = sum((x >= cuts[a]) == (y >= cuts[b]) for x, y in zip(pa, pb))
            out.append({"a": a, "b": b, "same_verdict": round(same / len(pa), 3) if pa else None, "pearson": pearson(pa, pb)})
    return out


def stack_of(
    ids: list[str], expected: list[bool], singles: dict[str, list[float]], cuts: dict[str, float]
) -> tuple[list[float] | None, dict[str, Any]]:
    """`(the stack's out-of-fold p, its block)` over `singles`, or `(None, block with why it was skipped)`. The block
    carries what the stack learned on every row (`weights`, voters then bias), its ΔAUC against the best single voter,
    and the mistakes each voter rescues of the others' (`stack.mistakes`)."""
    block: dict[str, Any] = {"over": list(singles), "mistakes": stack_voter.mistakes(singles, expected, cuts)}
    try:
        stacked = stack_voter.out_of_fold(list(singles.values()), expected, ids)
    except ValueError as error:
        return None, block | {"skipped": str(error)}
    return stacked.probabilities, block | {
        "weights": stacked.weights,
        "vs_best": stack_voter.verdict(stacked.probabilities, singles, expected),
    }


def score_set(set_name: str, data: list[tuple[str, Any, bool]], voters: list[Voter], suite: Suite) -> dict[str, Any]:
    states = [state for _id, state, _e in data]
    expected = [e for _id, _state, e in data]
    stack_block: dict[str, Any] | None = None
    for voter in voters:
        infer.predict_noul(voter.router, voter.model, states[:1], suite.question)  # warm: a cold start is not latency
        voter.router.ms.clear()
        voter.probabilities[set_name] = infer.predict_noul(voter.router, voter.model, states, suite.question)
        voter.latency[set_name] = list(voter.router.ms)
    probabilities = {voter.name: voter.probabilities[set_name] for voter in voters}
    cuts = {voter.name: voter.live_cut for voter in voters}
    latency = {voter.name: voter.latency[set_name] for voter in voters}
    if len(voters) > 1:
        probabilities[MEAN] = [statistics.fmean(ps) for ps in zip(*(voter.probabilities[set_name] for voter in voters))]
        cuts[MEAN] = suite.threshold
        latency[MEAN] = []
        singles = {voter.name: voter.probabilities[set_name] for voter in voters}
        stacked, stack_block = stack_of([row_id for row_id, _s, _e in data], expected, singles, cuts)
        if stacked is not None:
            probabilities[STACK] = stacked
            cuts[STACK] = suite.threshold  # the code's, as mean's: stack.py owns why
            latency[STACK] = []
    out = {
        "rows": len(data),
        "open": sum(expected),
        "done": len(expected) - sum(expected),
        "voters": {
            name: voter_scores(ps, expected, cuts[name], suite.false_warning_cost, latency[name])
            for name, ps in probabilities.items()
        },
        "agreement": agreement({name: ps for name, ps in probabilities.items() if name not in (MEAN, STACK)}, cuts),
        # ids and keys only: a text never enters a report
        "p": {row_id: {name: round(ps[i], 4) for name, ps in probabilities.items()} for i, (row_id, _s, _e) in enumerate(data)},
    }
    if stack_block is not None:
        out["stack"] = stack_block
    return out


def evaluate(
    name: str,
    base: Path,
    *,
    voters: list[str] | None = None,
    labels: str = "auto",
    heldout: bool = True,
    device: str = "mps",
    router: Any = None,
) -> tuple[int, dict[str, Any] | None, Path | None]:
    """`(exit code, report, its path)`: the report is written before this returns; None on a refusal or an error."""
    suite = SUITES[name]
    notes: dict[str, str] = {}
    try:
        built = build_voters(base, suite, list(voters or DEFAULT_VOTERS), device, router, notes)
    except Refused as refusal:
        print(f"[laya-lab] compare refused: {refusal}")
        return REFUSED, None, None
    sets: dict[str, dict[str, Any]] = {}
    held_meta: dict[str, Any] = {}
    try:
        sets["golden"] = score_set("golden", golden_rows(base, name, suite), built, suite)
        if heldout:
            held, held_meta = heldout_rows(base, name, suite, labels)
            if held:
                sets["heldout"] = score_set("heldout", held, built, suite) | held_meta
    except ValueError as error:
        print(f"[laya-lab] compare: {error}")
        return 1, None, None
    report = {
        "kind": "compare",
        "suite": name,
        "surface": suite.surface,
        "question_version": suite.question_version,
        "at": rows.utc_stamp(),
        "run_id": secrets.token_hex(6),
        "grid": list(GRID),
        "voters": [
            {
                "name": v.name,
                "kind": v.kind,
                "model": v.model,
                "url": v.url,
                "digest": v.digest,
                "live_cut": {"at": v.live_cut, "source": v.cut_source},
            }
            for v in built
        ],
        "sets": sets,
        "heldout_note": held_meta.get("skipped"),
        "notes": notes,
        "device": device,
        "dataset_version": dataset_version(base, name, by="compare")["id"],
        **lab_files.eval_run_field(),
    }
    out = base / EVALS_DIRNAME / lab_files.compare_report_name(name, lab_files.report_stamp())
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0, report, out


def run(
    name: str,
    base: Path,
    *,
    voters: list[str] | None = None,
    labels: str = "auto",
    heldout: bool = True,
    device: str = "mps",
    router: Any = None,
) -> int:
    code, report, out = evaluate(name, base, voters=voters, labels=labels, heldout=heldout, device=device, router=router)
    if report is not None:
        for line in report_lines(report):
            print(line)
        print(f"  report → {out}")
    return code


def report_lines(report: dict[str, Any]) -> list[str]:
    lines = [
        f"[laya-lab] compare {report['suite']} · q v{report['question_version']} · "
        + " · ".join(f"{v['name']} (cut {v['live_cut']['at']} {v['live_cut']['source']})" for v in report["voters"])
    ]
    for set_name, block in report["sets"].items():
        head = f"  {set_name} · {block['rows']} rows ({block['open']} open / {block['done']} done)"
        if "labels" in block:
            head += f" · {block['labels']} labels · excluded {block['excluded']}"
        lines.append(head)
        width = max(len(name) for name in block["voters"])
        lines.append(f"    {'voter':<{width}}   AUC  brier    ECE   live: acc ⚠️✗ miss   best (in-sample)   p50 ms")
        for voter_name, s in block["voters"].items():
            live, best = s["at_live"], s["best_in_sample"]
            p50 = s["latency_ms"]["p50"] if s["latency_ms"] else "—"
            lines.append(
                f"    {voter_name:<{width}}  {fmt_num(s['auc'], 3)}  {fmt_num(s['brier'], 3)}  {fmt_num(s['ece'], 3)}"
                f"   {fmt_num(live['accuracy'], 3)} {live['false_warnings']:>3} {live['missed_open']:>4}"
                f"   {best['threshold']:.2f} → {fmt_num(best['accuracy'], 3)} cost {best['cost']:<5}  {p50:>7}"
            )
        for pair in block["agreement"]:
            lines.append(
                f"    {pair['a']} ~ {pair['b']}: same verdict {fmt_num(pair['same_verdict'], 3)} · r {fmt_num(pair['pearson'], 3)}"
            )
        lines.extend(stack_lines(block.get("stack")))
    if report["heldout_note"]:
        lines.append(f"  held-out skipped: {report['heldout_note']}")
    for voter, why in (report.get("notes") or {}).items():
        lines.append(f"  {voter} left out: {why}")
    return lines


def stack_lines(block: dict[str, Any] | None) -> list[str]:
    """The stack's verdict and the mistakes question, under a set's table."""
    if block is None:
        return []
    lines = []
    best = block.get("vs_best")
    if "skipped" in block:
        lines.append(f"    stack skipped: {block['skipped']}")
    elif best is not None:
        lines.append(
            f"    stack vs {best['best_single']}: ΔAUC {best['delta_auc']:+.4f} [{best['lo']:+.4f}, {best['hi']:+.4f}]"
            f" · {'WIN' if best['win'] else 'no win'} · weights {block['weights']}"
        )
    for pair in block["mistakes"]:
        lines.append(
            f"    {pair['a']} wrong {pair['a_wrong']} → {pair['b']} right {pair['b_right']} ({fmt_num(pair['share'], 3)})"
        )
    return lines
