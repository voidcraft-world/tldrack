"""`laya-lab sweep`: where the ⚠️ cut should sit, measured on labelled texts the served fine-tune
never trained on, and (with `--apply`) moved there.

    laya-lab sweep agent-open-work                           # dry run: auto labels, held-out rows
    laya-lab sweep agent-open-work --labels operator --apply  # what AGENT RACK › LAYA › RE-SWEEP runs
    laya-lab sweep agent-open-work --reset                   # back to the code's cut (AGENT RACK › LAYA › RESET)

Which rows: owned by `laya_bridge.review_data.clean_label_rows` — never a golden case, never a key
without text, never a row the served fine-tune trained on — so the rack's `sweep_ready`, the bridge's
`/lab` gate and this command's exit 3 read one rule. The report's `reserve` counts the rows of the label budget's
reserve (`laya_bridge.label_budget`): the ones still clean after the next retrain. Which labels (`--labels`):
- `operator`: the REVIEW deck's answers only (`laya_bridge.review_log.fold`).
- `model`: Sonnet's labels only.
- `merged`: Sonnet's, with every operator answer winning (`open_work_data.effective_labels`).
- `auto` (default): `operator` once `review_data.sweep_readiness` is ready, else `merged`.

The cut rule: owned by bridge/tldrack/open_work.py `FALSE_WARNING_COST` — see the
note there. The same run sweeps the agent rack's ✅ cut under it (`FALSE_DONE_COST`, the same owner)
and `--apply` records both, so the operator's RECENT grades move the ✅ as well as the ⚠️. `--apply` writes the cut onto
the registered version it was swept for (`models.json` `versions.<version>.cut`, `registry.set_threshold`): the served
champion by default, or `--model challenger` (a cut that rides the challenger into a promotion), and only from OPERATOR
labels on a ready set: Sonnet's labels trained the model, so a cut tuned on them would grade the model with its own
teacher. A non-served model is swept on rows IT never trained on (`labelled_rows`' `scope`). `--no-heldout` sweeps
every labelled row, trained ones too: IN-SAMPLE, so it never applies, and its report is
`kind: "in_sample"`, which the rack's SWEEP row (`lab_files.newest_report(..., "heldout")`) never reads.

Every report carries κ's interval and precision/recall at the live cut (`trust`), ECE and reliability bins
around both live cuts. On operator labels with at least `rows.SWEEP_MIN_ROWS` rows it also carries `calibration`:
operator-ECE beside teacher-ECE on the same rows, and one temperature refit on the operator's labels (owned by
`laya_lab/metrics.py` — see "Calibration"). The rack's SWEEP tip reads it (`lab_files.heldout_view`).

Exit codes are the bridge's contract (`laya_bridge/lab_jobs.py` runs this): 0 swept, applied or
unchanged · 3 refused · 1 an error.
"""

from __future__ import annotations

import json
from dataclasses import replace
import secrets
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import infer
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES, dataset_version, laya_version
from laya_lab.metrics import (
    SWEEP_DONE,
    SWEEP_FINE,
    at_cut,
    at_done_cut,
    brier,
    calibration,
    ece,
    fmt_num,
    fmt_report_kappa,
    pick_cut,
    pick_done_cut,
    reliability,
    trust_at_cut,
)
from laya_lab.open_work_data import effective_labels, model_labels, require_sources
from laya_lab.registry import (
    clear_threshold,
    read_registry,
    registry_versions,
    resolve_model,
    served_done_threshold,
    served_model,
    served_threshold,
    set_threshold,
)
from laya_bridge import label_budget, lab_files, review_data, rows
from laya_bridge.paths import EVALS_DIRNAME
from laya_bridge.review_data import CleanRows
from laya_bridge.review_log import OperatorFold

REFUSED = 3
LABEL_SOURCES = ("auto", *lab_files.HELDOUT_LABELS)
RULE = "min FALSE_WARNING_COST·fp + fn, ties to the higher cut (tldrack/open_work.py)"
DONE_RULE = "min FALSE_DONE_COST·false ✅ + missed ✅, under the ⚠️ cut, ties to the lower cut (tldrack/open_work.py)"
#: The `kind` of a `--no-heldout` report: not "heldout", so no reader takes it for one.
IN_SAMPLE = "in_sample"


def labels_of(base: Path, surface: str, labels: str, fold: OperatorFold | None = None) -> dict[str, bool]:
    """`{key: open}` from one label source (`operator` | `model` | `merged`), before cleaning. `fold`
    is the run's one read of the operator log; without it, the sources are read (and refused) here."""
    fold = fold if fold is not None else require_sources(base, surface)
    if labels == "operator":
        # Exactly what `review_data.sweep_readiness` counts, so its verdict and this sweep agree.
        return {key: row["open"] for key, row in fold.labels.items()}
    merged = effective_labels(model_labels(base), fold if labels == "merged" else None)
    return {key: label["open"] for key, label in merged.items()}


def labelled_rows(
    base: Path,
    suite: str,
    labels: str,
    *,
    in_sample: bool = False,
    fold: OperatorFold | None = None,
    scope: dict[str, str] | None = None,
) -> CleanRows:
    """The clean `(key, open, text)` rows of a label source. `in_sample` keeps the rows the served
    fine-tune trained on: `loaded={}` reads as "nothing loaded", a built-in, which trained on nothing. `scope`
    (`{fine_tune: checkpoint dir}`) judges the rows against THAT checkpoint instead of the served one: a challenger's
    sweep must refuse when its own training record is missing (`sweep_scope`)."""
    surface = SUITES[suite].surface
    fold = fold if fold is not None else require_sources(base, surface)
    return review_data.clean_label_rows(
        base, surface, labels_of(base, surface, labels, fold), loaded={} if in_sample else scope, fold=fold
    )


def sweep_scope(base: Path, suite: str, target: str, path: Path | None) -> dict[str, str] | None:
    """`{fine_tune: path}` when `target` is a fine-tune other than the served one, else None (the served rule)."""
    spec = SUITES[suite]
    if path is None or target == served_model(base, spec.surface):
        return None
    return {spec.fine_tune: str(path)}


def resolve_labels(requested: str, base: Path, surface: str, fold: OperatorFold | None = None) -> str:
    if requested != "auto":
        return requested
    return "operator" if review_data.sweep_readiness(base, surface, fold)["ready"] else "merged"


def _refuse(message: str) -> int:
    print(f"[laya-lab] sweep refused: {message}")
    return REFUSED


def run_sweep(
    name: str,
    base: Path,
    *,
    model: str = "served",
    labels: str = "auto",
    heldout: bool = True,
    apply: bool = False,
    reset: bool = False,
    device: str = "mps",
    router: Any = None,
    reserve: bool = False,
) -> int:
    suite = SUITES[name]
    surface = suite.surface
    if reset:
        cleared = clear_threshold(base, surface)
        print(f"[laya-lab] {surface}: {'swept cut cleared' if cleared else 'no swept cut'}; the code's {suite.threshold} applies")
        return 0
    if labels not in LABEL_SOURCES:
        return _refuse(f"--labels must be one of {', '.join(LABEL_SOURCES)}")
    fold = require_sources(base, surface)
    target, path = resolve_model(base, surface, model)
    resolved = resolve_labels(labels, base, surface, fold)
    scope = sweep_scope(base, name, target, path)

    if apply:
        if not heldout:
            return _refuse("--no-heldout is IN-SAMPLE: its cut is never applied")
        if resolved != "operator":
            return _refuse(f"--apply sweeps operator labels only (got {resolved})")
        read_registry(base)  # the write this ends in, checked (a v1 file migrated) before any GPU work, not after it
        version = registry_versions(base)["versions"].get(path.name) if path is not None else None
        if version is None or version["surface"] != surface:
            return _refuse(f"a cut is kept on a registered checkpoint; {target} is not one")
        ready = review_data.sweep_readiness(base, surface, fold, loaded=scope)
        if not ready["ready"]:
            return _refuse(ready["reason"] or "not enough clean operator labels")

    clean = labelled_rows(base, name, resolved, in_sample=not heldout, fold=fold, scope=scope)
    if clean.reason is not None and heldout and path is not None:
        return _refuse(clean.reason)  # an unknown training record: no row can be called held out
    if reserve:
        clean = replace(clean, rows=[r for r in clean.rows if r[0] in clean.reserve])
        yes = sum(r[1] for r in clean.rows)
        if len(clean.rows) < rows.SWEEP_MIN_ROWS or min(yes, len(clean.rows) - yes) < rows.SWEEP_MIN_PER_CLASS:
            return _refuse("the reserve needs 40 clean rows, 10 of each class")
    if not clean.rows:
        return _refuse(f"no clean {resolved}-labelled rows (excluded: {clean.excluded})")

    router = router if router is not None else infer.open_router(base, device, {target: path})
    expected = [is_open for _key, is_open, _text in clean.rows]
    probabilities = infer.predict_noul(
        router, target, [suite.build_state({"hand_off": text}) for _k, _o, text in clean.rows], suite.question
    )
    live, source = served_threshold(base, surface, target, code=suite.threshold)
    head = at_cut(probabilities, expected, live)
    sweep = [at_cut(probabilities, expected, cut) for cut in SWEEP_FINE]
    picked = pick_cut(sweep, suite.false_warning_cost)
    best_kappa = next(entry["kappa"] for entry in sweep if entry["threshold"] == picked["at"])
    live_done, done_source = served_done_threshold(base, surface, target, code=suite.done_threshold)
    done_sweep = [at_done_cut(probabilities, expected, cut) for cut in SWEEP_DONE]
    done_pick = pick_done_cut(done_sweep, suite.false_done_cost, picked["at"])
    operator_calibration = None
    if resolved == "operator" and len(clean.rows) >= rows.SWEEP_MIN_ROWS:
        teacher = {row["key"]: row["open"] for row in model_labels(base)}
        operator_calibration = calibration(
            probabilities, expected, [teacher.get(key) for key, _o, _t in clean.rows], (live, live_done)
        )

    at = rows.utc_stamp()
    checkpoint = infer.checkpoint_stamp(base, router, target, path)
    report_name = lab_files.heldout_report_name(name, target, resolved, lab_files.report_stamp())
    applied: Any = False
    if apply:
        applied = {
            "at": picked["at"],
            "done_at": done_pick["at"],
            "previous": {"at": live, "source": source, "done_at": live_done, "done_source": done_source},
        }
    # κ beside `best` is κ AT best: what the rack pairs them as, and what `threshold_entry` records on apply.
    report = lab_files.heldout_report_head(at, name, target, resolved, len(clean.rows), picked["at"], best_kappa)
    if not heldout:
        report["kind"] = IN_SAMPLE
    report["recommended"].update(
        cost=picked["cost"], fp_cost=suite.false_warning_cost, max_kappa_at=picked["max_kappa_at"], rule=RULE
    )
    report.update(
        surface=surface,
        checkpoint=checkpoint,
        question_version=suite.question_version,
        labels_requested=labels,
        excluded=clean.excluded,
        reserve_only=reserve,
        class_counts={"open": sum(expected), "done": len(expected) - sum(expected)},
        reserve=label_budget.counts(clean.reserve, {key: is_open for key, is_open, _text in clean.rows}),
        served_threshold={"at": live, "source": source},
        at_threshold=head,
        sweep=sweep,
        brier=brier(probabilities, expected),
        trust=trust_at_cut(probabilities, expected, live),
        ece=ece(probabilities, expected),
        reliability=reliability(probabilities, expected, (live, live_done)),
        calibration=operator_calibration,
        applied=applied,
        done={
            "at": done_pick["at"],
            "cost": done_pick["cost"],
            "fp_cost": suite.false_done_cost,
            "rule": DONE_RULE,
            "live": {"at": live_done, "source": done_source},
            "at_live": at_done_cut(probabilities, expected, live_done),
            "sweep": done_sweep,
        },
        misses=[
            {"key": key, "expected": is_open, "p": round(p, 3)}
            for (key, is_open, _text), p in zip(clean.rows, probabilities)
            if (p >= live) != is_open
        ],
        run_id=secrets.token_hex(6),
        laya_version=laya_version(),
        device=device,
        dataset_version=dataset_version(base, name, by="sweep")["id"],
    )
    out = base / EVALS_DIRNAME / report_name
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
    if apply:
        set_threshold(
            base,
            surface,
            lab_files.threshold_entry(
                picked["at"], target, checkpoint["name"], at, resolved, len(clean.rows), best_kappa, report_name, done_pick["at"]
            ),
        )

    for line in sweep_lines(report):
        print(line)
    if clean.reason is not None and heldout:
        print(f"  note: {clean.reason}")
    print(f"  report → {out}")
    return 0


def sweep_lines(report: dict[str, Any]) -> list[str]:
    """What the sweep prints: counts, the live cut, the grid, the recommendation and what it did. Keys
    and numbers only: a closing text never reaches the terminal from here."""
    head, rec, counts = report["at_threshold"], report["recommended"], report["class_counts"]
    live = report["served_threshold"]
    in_sample = report["kind"] == IN_SAMPLE
    label = report["labels"] + (" (auto)" if report["labels_requested"] == "auto" else "")
    lines = [
        f"[laya-lab] sweep {report['suite']} · {report['model']} · {label} labels · {report['rows']} rows "
        f"({counts['open']} open / {counts['done']} done) · reserve {(report.get('reserve') or {}).get('rows', 0)} · "
        f"excluded {report['excluded']}" + (" · IN-SAMPLE" if in_sample else " · held out"),
        f"  live {live['at']} ({live['source']}): accuracy {head['accuracy']}  "
        f"κ {fmt_report_kappa(report)}  "
        f"false ⚠️ {head['false_warnings']}  missed open {head['missed_open']}  brier {report['brier']}  "
        f"ECE {fmt_num(report.get('ece'), 3)}",
    ]
    trust = report.get("trust") or {}
    rates = [
        f"{name} {fmt_num(trust[name]['value'])} [{fmt_num(trust[name]['lo'])}, {fmt_num(trust[name]['hi'])}] "
        f"(n {trust[name]['n']})"
        for name in ("precision", "recall")
        if (trust.get(name) or {}).get("value") is not None
    ]
    if rates:
        lines.append(f"  at the live cut: {' · '.join(rates)}")
    fit = report.get("calibration")
    if fit:
        lines.append(
            f"  calibration on {fit['rows']} operator labels: ECE {fmt_num(fit['ece'], 3)} · teacher ECE "
            f"{fmt_num(fit['teacher']['ece'], 3)} (same rows, {fit['teacher']['rows']} with a Sonnet label) · "
            f"one temperature {fmt_num(fit['temperature'])} → ECE {fmt_num(fit['ece_refit'], 3)} out of fold"
        )
    for entry in report["sweep"]:
        cost = rec["fp_cost"] * entry["false_warnings"] + entry["missed_open"]
        mark = " ←" if entry["threshold"] == rec["at"] else ""
        lines.append(
            f"  {entry['threshold']:.2f}: false ⚠️ {entry['false_warnings']:>3}  missed {entry['missed_open']:>3}  "
            f"cost {cost:>6.1f}  κ {fmt_num(entry['kappa'])}{mark}"
        )
    lines.append(f"  recommended {rec['at']} (cost {rec['cost']}, κ {fmt_num(report['kappa'])}; best κ at {rec['max_kappa_at']})")
    done = report.get("done")
    if done:
        now = done["at_live"]
        lines.append(
            f"  ✅ live {done['live']['at']} ({done['live']['source']}): false ✅ {now['false_done']}  "
            f"missed ✅ {now['missed_done']}  drawn {now['shown']}"
        )
        picked = next((e for e in done["sweep"] if e["threshold"] == done["at"]), None)
        lines.append(
            f"  ✅ recommended {done['at']}"
            + (
                f" (false ✅ {picked['false_done']}, missed ✅ {picked['missed_done']}, cost {done['cost']})"
                if picked
                else " (no cut under the ⚠️ one)"
            )
        )
    applied = report["applied"]
    if applied:
        previous = applied["previous"]
        verb = "unchanged at" if previous["source"] == "swept" and previous["at"] == applied["at"] else "applied"
        done_note = f", ✅ {applied['done_at']} (was {previous.get('done_at')})" if applied.get("done_at") is not None else ""
        lines.append(
            f"  {verb} {applied['at']}{done_note} for {report['model']} (was {previous['at']}, {previous['source']}); "
            "the rack reads it on its next verdict"
        )
    elif in_sample:
        lines.append("  IN-SAMPLE: trained rows included, so this cut is never applied")
    else:
        lines.append("  dry run: --labels operator --apply moves the cut (AGENT RACK › LAYA › RE-SWEEP)")
    return lines
