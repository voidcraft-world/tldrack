"""`laya-lab stability agent-open-work`: STABLE κ, the meter that says when more grading has stopped changing Laya.

    laya-lab stability agent-open-work            # score what is unscored on this machine's GPU, then print the meter

small-text's KappaAverage stopping criterion, on checkpoints instead of active-learning rounds: ask every checkpoint
the same fixed states that nobody labelled, and read κ between the verdicts of each consecutive pair. While a retrain
on more operator grades still flips verdicts on states it never saw, κ is low and the grades are buying something;
once consecutive checkpoints draw the same ⚠️ on all of them, they are not. `lab_files.stability_summary` owns the
number (what "consecutive", "its own cut" and `stable` mean), so this command, the nightly pack's `stability` step
(`nightly.py`) and the LAYA row's STABLE line (`laya_bridge/ops.py`) read one rule.

**The stop set** (`stability/<surface>-stop-set.jsonl`, operator text): `lab_files.STOP_SET_SIZE` live states from the
ledger, exactly as they were asked (`shadow.live_states`: whole text, today's question). It is fixed: chosen once, in
key order (a key is a hash, so that is a stable random draw), and never re-drawn. A state leaves only when it stops
being unlabelled: the operator graded it, Sonnet labelled it, a checkpoint trained on it, or a golden suite took it
(`taken_keys`). Agreement on a state one checkpoint was trained on measures memory, not stability. The next eligible
state then takes its place, and the scores of the one that left are dropped with it, before anything is scored: a
pass that stops mid-score (a checkpoint that will not load) must not leave the meter counting a state the set no
longer holds. Until the ledger has held that many eligible states the set is smaller, and it grows.

**The scores** (`stability/<surface>.jsonl`, keys and numbers only): one row per (state, checkpoint, digest, device),
so a quiet night scores nothing, a checkpoint re-trained in place is scored again (its old rows go), and a CPU p never
stands in for an MPS one. Every registered version of the surface that is on disk is scored, the challenger included
(`golden.registered_targets`); the base checkpoint is not a point on the label curve and is left out. A deleted
checkpoint's rows stay, because they are the only record of what it said, for the newest VERSIONS_KEPT checkpoints.

Both files are only ever rewritten from a read that succeeded: one its reader refuses (too large, a link) is left as
it is and the pass fails, since rewriting from that empty read would draw a NEW stop set and orphan every score.

Exit codes: 0 scored (or nothing to score) · 1 a stability file its reader refuses (nothing written).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import golden, infer, shadow
from laya_lab.data import write_jsonl
from laya_lab.golden import SUITES, Suite
from laya_lab.metrics import fmt_num
from laya_lab.registry import registry_versions
from laya_bridge import agreement, files, lab_files, paths, review_data, review_log, rows
from laya_bridge.review_surfaces import REVIEW_SURFACES

#: How many checkpoints' scores the file keeps (the newest, by folder name): ~30 KB each at a full stop set.
VERSIONS_KEPT = 32


class Unreadable(Exception):
    """A stability file its reader refuses: the pass writes nothing."""


@dataclass(frozen=True)
class Target:
    """One checkpoint to score: the name the router knows it by (the served name for the champion), its folder, its
    digest, and the stop-set rows it has no score for under that digest and device."""

    router_name: str
    folder: Path
    digest: str | None
    todo: list[dict[str, Any]]


@dataclass(frozen=True)
class Plan:
    """What a pass will do, from reads alone (`plan` writes nothing): the stop set as it will stand, the keys that
    left it and how many joined, the score rows that stay, and the checkpoints with their unscored states."""

    stop: list[dict[str, Any]]
    retired: frozenset[str]
    added: int
    held: list[dict[str, Any]]
    targets: dict[str, Target]

    @property
    def loading(self) -> list[str]:
        """The router names a pass will load: what the gpu claim is sized to."""
        return [target.router_name for target in self.targets.values() if target.todo]


def taken_keys(base: Path, suite: Suite) -> frozenset[str]:
    """Every state key that is no longer unlabelled (the module note owns the four ways). A label, a training row and
    a golden case are keyed by their TEXT, and a long text is asked as its fitted state, so each text's key is joined
    by the key of the state it is asked as. Read tolerantly: a source that does not read takes nothing tonight, which
    can only leave a state in the set one night longer."""
    spec = REVIEW_SURFACES[suite.surface]
    _code, fold = review_log.read_fold(base, suite.surface)
    _labels_code, sonnet = review_data.sonnet_labels(base, spec)
    taken = set(fold.labels) | set(sonnet) | set(review_data.golden_keys(base, suite.name))
    texts = review_data.texts(base, suite.surface)
    taken |= {rows.state_key(suite.build_state({spec.text_field: texts[key]})) for key in taken & texts.keys()}
    public, _pc, private, _vc = review_data.golden_suites(base, suite.name)
    taken |= {rows.state_key(suite.build_state(case["state"])) for case in (*public, *private)}
    return frozenset(taken | rows.trained_keys(base, suite.fine_tune)[0])


def _read(base: Path, surface: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stop, stop_code = lab_files.read_stop_set(base, surface)
    held, held_code = lab_files.read_stability(base, surface)
    for path, code in ((paths.stop_set_path(base, surface), stop_code), (paths.stability_path(base, surface), held_code)):
        if code not in (files.OK, files.MISSING):
            raise Unreadable(f"{path.name} is {code}; the stability files are left as they are")
    return stop, held


def plan(name: str, base: Path, *, device: str = "mps", now: datetime | None = None) -> Plan:
    """The pass a run would make. Raises `Unreadable` instead of planning from a refused read."""
    suite = SUITES[name]
    stop, held = _read(base, suite.surface)
    taken = taken_keys(base, suite)
    kept = [row for row in stop if row["key"] not in taken]
    retired = frozenset(row["key"] for row in stop) - frozenset(row["key"] for row in kept)
    added = 0
    if len(kept) < lab_files.STOP_SET_SIZE:
        have = {row["key"] for row in kept}
        live, _skipped = shadow.live_states(base, suite, None)
        fresh = {state.key: state for state in live if state.key not in taken and state.key not in have}
        stamp = rows.utc_stamp(now)
        for key in sorted(fresh)[: lab_files.STOP_SET_SIZE - len(kept)]:
            kept.append(lab_files.stop_set_row(key=key, at=fresh[key].at, state=fresh[key].state, added_at=stamp))
            added += 1
    active = {row["key"] for row in kept}
    staying = [row for row in held if row["key"] in active]
    seen = {lab_files.stability_key(row) for row in staying}
    targets: dict[str, Target] = {}
    for router_name, folder in golden.registered_targets(base, suite.surface).items():
        if folder is None:
            continue
        digest = infer.checkpoint_digest(router_name, folder)
        version = Path(folder).name
        # A digest that cannot be read keys nothing: such a row is never reused (`infer.checkpoint_digest`).
        todo = [row for row in kept if digest is None or (row["key"], version, digest, device) not in seen]
        targets[version] = Target(router_name, Path(folder), digest, todo)
    return Plan(stop=kept, retired=retired, added=added, held=staying, targets=targets)


def compact(rows_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the scores file keeps: per (checkpoint, device) only its newest digest's rows, and only the newest
    VERSIONS_KEPT checkpoints. Ordered by checkpoint, device, key, so a rewrite of the same rows is byte-identical."""
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows_in:
        group = (row["version"], row["device"])
        if group not in newest or row["scored_at"] >= newest[group]["scored_at"]:
            newest[group] = row
    versions = set(sorted({version for version, _device in newest})[-VERSIONS_KEPT:])
    kept = {
        lab_files.stability_key(row): row
        for row in rows_in
        if row["version"] in versions and row.get("digest") == newest[(row["version"], row["device"])].get("digest")
    }
    return sorted(kept.values(), key=lambda row: (row["version"], row["device"], row["key"]))


def run(
    name: str, base: Path, *, device: str = "mps", router: Any = None, now: datetime | None = None
) -> tuple[int, dict[str, Any]]:
    """Score every registered checkpoint on the stop-set states it has no score for; `(exit code, summary)`. `summary`
    is what the nightly report keeps: the stop set's size and moves, rows scored per checkpoint, and
    `lab_files.stability_summary` under `meter`."""
    suite = SUITES[name]
    now = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"device": device, "states": 0, "added": 0, "retired": 0, "scored": {}, "meter": None}
    try:
        planned = plan(name, base, device=device, now=now)
    except Unreadable as refused:
        summary["note"] = str(refused)
        return 1, summary
    summary.update(states=len(planned.stop), added=planned.added, retired=len(planned.retired))
    if planned.added or planned.retired:
        write_jsonl(paths.stop_set_path(base, suite.surface), planned.stop)
    if planned.retired:  # before scoring: the module note owns why
        write_jsonl(paths.stability_path(base, suite.surface), compact(planned.held))
    if not planned.stop:
        summary["note"] = "the ledger holds no unlabelled live state yet"
    elif not planned.targets:
        summary["note"] = "no fine-tune is registered: nothing to compare"
    if planned.loading and router is None:
        router = infer.open_router(base, device, {t.router_name: t.folder for t in planned.targets.values() if t.todo})
    cuts = lab_files.stability_cuts(registry_versions(base), suite.surface)
    stamp = rows.utc_stamp(now)
    new_rows: list[dict[str, Any]] = []
    for version, target in planned.targets.items():
        if target.todo:
            probabilities = infer.predict_noul(router, target.router_name, [row["state"] for row in target.todo], suite.question)
            new_rows.extend(
                lab_files.stability_row(
                    key=row["key"],
                    version=version,
                    digest=target.digest,
                    device=device,
                    p=round(p, 4),
                    cut=cuts.get(version, suite.threshold),
                    scored_at=stamp,
                )
                for row, p in zip(target.todo, probabilities)
            )
        summary["scored"][version] = len(target.todo)
    written = compact([*planned.held, *new_rows])
    if new_rows:
        write_jsonl(paths.stability_path(base, suite.surface), written)
    summary["meter"] = lab_files.stability_summary(written, cuts)
    return 0, summary


def meter_lines(meter: dict[str, Any]) -> list[str]:
    """`stability_summary` as a readout: the meter, then one line per consecutive pair. Names and numbers only."""
    average = meter["kappa_average"]
    head = (
        f"STABLE κ {fmt_num(average, 3)} over the last {meter['window']} pair{'' if meter['window'] == 1 else 's'}"
        f" · {'stable' if meter['stable'] else 'still moving'} (≥ {lab_files.STABLE_KAPPA} over {lab_files.KAPPA_WINDOW})"
        if average is not None
        else f"STABLE κ — (it needs two checkpoints scored on {agreement.KAPPA_MIN_PAIRS}+ shared states)"
    )
    lines = [f"  {head} · {meter['states']} states · {len(meter['versions'])} checkpoints"]
    for pair in meter["pairs"]:
        lines.append(
            f"    {pair['from']} → {pair['to']}: κ {fmt_num(pair['kappa'], 3)} · agree {fmt_num(pair['agree'], 3)} · "
            f"{pair['flips']} of {pair['n']} flipped"
        )
    return lines


def cmd(name: str, base: Path, *, device: str) -> int:
    code, summary = run(name, base, device=device)
    suite = SUITES[name]
    print(
        f"[laya-lab] stability {name} · stop set {summary['states']} of {lab_files.STOP_SET_SIZE}"
        f" (+{summary['added']}, −{summary['retired']}) · {device}" + (f" · {summary['note']}" if summary.get("note") else "")
    )
    for version, scored in summary["scored"].items():
        print(f"  {version}: +{scored} scored")
    if summary["meter"] is not None:
        for line in meter_lines(summary["meter"]):
            print(line)
    print(f"  → {paths.stability_path(base, suite.surface)}")
    return code
