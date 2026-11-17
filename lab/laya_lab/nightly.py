"""`laya-lab nightly agent-open-work`: the nightly Laya pack, one command a scheduler runs each night, under one
gpu-slot take for the whole pack (when the machine has a lease: `laya_lab/lease.py`).

Steps, each recorded in the pack's report with its state (`ran` · `reused` · `skipped` · `failed`), its numbers and the
files it wrote. A step that fails costs that step only; the rest still run.

0. `registry`  a v1 `models.json` is migrated (`registry.migrate_registry`): nothing served changes, and the alias
               machinery the steps below read becomes the file's own shape.
1. `golden`    the base, @champion and every registered version (`golden.registered_targets`). A target whose newest
               report has the same (checkpoint digest, device, dataset version) is reused, not re-scored: a quiet
               night scores nothing twice.
2. `score`     the incremental SCORE (`score.run`): only the keys with no p under the served digest and device.
3. `compare`   the served model, each @challenger, the base and the TF-IDF floor (`compare.evaluate`), golden and held-out.
4. `shadow`    each @challenger on the live states the ledger kept since it was registered (`shadow.run`).
5. `stability` every registered checkpoint on the stop-set states it has no score for, then STABLE κ (`stability.run`).
6. `readiness` the operator's clean grades (`review_data.sweep_readiness`, 40/10). Once ready, and again after every
               READINESS_STEP more since the pack last swept, a RE-SWEEP DRY RUN on the champion and on each challenger
               (on rows it never trained on). The step records the label budget's reserve beside the counts
               (`laya_bridge.label_budget`): the rows a challenger trained under the budget still has to sweep on.
               APPLY stays the operator's armed press: nothing here moves a cut.

Every report the steps write is keyed by (checkpoint digest, device, case) and stamped with the dataset version
(`golden.dataset_version`); the pack's own report (`lab_files.nightly_report_name`) carries both, and its verdict is
what `laya-lab verdict nightly` prints: FAIL when a step failed, INCONCLUSIVE when golden scored nothing, else PASS.
Headline: the champion's golden numbers, its AUC on held-out grades, and the step counts.

Exit codes: 0 the report was written (read its verdict) · 75 the lease refused (a busy machine, never a failure) · 1 an
error before any step ran.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import compare, golden, heldout, infer, registry, score, shadow, stability
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES
from laya_bridge import lab_files, review_data, rows
from laya_bridge.paths import EVALS_DIRNAME

KIND = "nightly"
STEPS = ("registry", "golden", "score", "compare", "shadow", "stability", "readiness", "challenger", "needs_you")
RAN, REUSED, SKIPPED, FAILED = "ran", "reused", "skipped", "failed"
#: The operator's clean grades between two RE-SWEEP dry runs.
READINESS_STEP = 10


def run_step(steps: dict[str, dict[str, Any]], name: str, body: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    """Run one step into `steps[name]`: its returned dict (with a `state`), or `failed` with the reason and the error's
    type. A lease refusal (exit 75) is not a step's failure: it ends the pack, as it would end any lab command.

    Any `Exception` is a failed step, not only the lab's own refusals: torch and laya raise their own (an MPS
    out-of-memory is a `RuntimeError`, a corrupt checkpoint a safetensors error or a `TypeError`), and one escaping
    here would end the pack with no report, so `verdict nightly` would find nothing instead of FAIL and the steps that
    already ran would be lost. A `KeyboardInterrupt` still ends it."""
    started = time.monotonic()
    try:
        result = body()
    except SystemExit as stop:
        if stop.code == infer.LEASE_REFUSED_EXIT:
            raise
        result = {"state": FAILED, "why": infer.why_line(stop), "error": "SystemExit"}
    except Exception as error:  # noqa: BLE001 (the docstring owns why)
        result = {"state": FAILED, "why": infer.why_line(error), "error": type(error).__name__}
    result["seconds"] = round(time.monotonic() - started, 1)
    steps[name] = result
    return result


def headline_of(steps: dict[str, dict[str, Any]], champion: str) -> dict[str, Any]:
    """The champion's golden numbers, its held-out AUC from compare (golden when there were no held-out rows), and how
    many steps ran, were skipped or failed (`EVAL_METRIC_KEYS`)."""
    head: dict[str, Any] = {}
    served = (steps.get("golden") or {}).get("targets", {}).get(champion)
    if served:
        head.update({key: served.get(key) for key in ("cases", "accuracy", "kappa", "brier", "ece")})
        head["model_digest"] = served.get("digest")
    sets = (steps.get("compare") or {}).get("auc") or {}
    head["auc"] = sets.get("heldout", sets.get("golden"))
    head["latency_ms_p50"] = (steps.get("compare") or {}).get("latency_ms_p50")
    states = [step["state"] for step in steps.values()]
    head.update(trials=sum(s in (RAN, REUSED) for s in states), skipped=states.count(SKIPPED), failed=states.count(FAILED))
    return head


def verdict_of(steps: dict[str, dict[str, Any]]) -> str:
    if any(step["state"] == FAILED for step in steps.values()):
        return "FAIL"
    if not (steps.get("golden") or {}).get("targets"):
        return "INCONCLUSIVE"
    return "PASS"


# ── the steps ─────────────────────────────────────────────────────────────────


def step_registry(base: Path) -> dict[str, Any]:
    migrated, notes = registry.migrate_registry(base)
    return {
        "state": RAN if migrated else SKIPPED,
        "migrated": migrated,
        "notes": notes,
        **({} if migrated else {"why": "models.json is already v2 (or absent)"}),
    }


def step_golden(name: str, base: Path, targets: dict[str, Path | None], device: str, router: Any) -> dict[str, Any]:
    written, meta = golden.evaluate(name, base, targets, device=device, router=router, reuse=True, by="nightly")
    out: dict[str, Any] = {}
    for target, report, path, reused in written:
        head = report.get("at_threshold") or {}
        out[target] = {
            "report": path.name,
            "reused": reused,
            "digest": (report.get("checkpoint") or {}).get("digest"),
            "cases": report.get("cases"),
            "accuracy": head.get("accuracy"),
            "kappa": report.get("kappa"),
            "brier": report.get("brier"),
            "ece": report.get("ece"),
            "cut": head.get("threshold"),
        }
    reused = all(block["reused"] for block in out.values())
    return {"state": REUSED if out and reused else RAN, "targets": out, "dataset_version": meta["dataset"]["id"]}


def step_score(base: Path, device: str, router: Any) -> dict[str, Any]:
    code = score.run(base, model="served", device=device, router=router)
    found, _code = lab_files.read_scores(base, score.REVIEW_SURFACES[score.SURFACE].stem)
    return {"state": RAN if code == 0 else FAILED, "rows": len(found), **({} if code == 0 else {"why": f"exit {code}"})}


def step_compare(name: str, base: Path, voters: list[str], device: str, router: Any) -> dict[str, Any]:
    code, report, path = compare.evaluate(name, base, voters=voters, labels="auto", device=device, router=router)
    if report is None or path is None:
        return {"state": FAILED, "why": f"exit {code}"}
    served = next((v["name"] for v in report["voters"] if v["kind"] == "laya"), None)
    auc = (
        {set_name: block["voters"][served]["auc"] for set_name, block in report["sets"].items() if served in block["voters"]}
        if served
        else {}
    )
    latency = ((report["sets"].get("golden") or {}).get("voters", {}).get(served) or {}).get("latency_ms") if served else None
    return {
        "state": RAN,
        "report": path.name,
        "voters": [v["name"] for v in report["voters"]],
        "auc": auc,
        "latency_ms_p50": (latency or {}).get("p50"),
        "notes": report.get("notes") or {},
        "heldout_rows": (report["sets"].get("heldout") or {}).get("rows", 0),
    }


def step_shadow(name: str, base: Path, device: str, router: Any) -> dict[str, Any]:
    code, summary = shadow.run(name, base, device=device, router=router)
    if code != 0:
        return {"state": FAILED, "why": summary.get("note") or f"exit {code}", **summary}
    if not summary["challengers"]:
        return {"state": SKIPPED, "why": summary.get("note") or "no @challenger", **summary}
    return {"state": RAN, **summary}


def step_stability(name: str, base: Path, device: str, router: Any) -> dict[str, Any]:
    code, summary = stability.run(name, base, device=device, router=router)
    if code != 0:
        return {"state": FAILED, "why": summary.get("note") or f"exit {code}", **summary}
    if summary.get("note"):
        return {"state": SKIPPED, "why": summary["note"], **summary}
    return {"state": RAN if any(summary["scored"].values()) else REUSED, **summary}


def stability_loading(name: str, base: Path, device: str) -> list[str]:
    """The router targets the stability step will load tonight. This runs before any step, to size the gpu claim, so
    nothing it raises may end the pack: a plan that cannot be made loads none here, and the step itself then fails
    inside `run_step`, with its reason in the report."""
    try:
        return stability.plan(name, base, device=device).loading
    except Exception:  # noqa: BLE001 (the docstring owns why)
        return []


def step_readiness(
    name: str, base: Path, device: str, router: Any, previous: dict[str, Any] | None, digests: dict[str, str | None]
) -> dict[str, Any]:
    """Readiness every night; the RE-SWEEP dry runs only once it is ready AND (READINESS_STEP more clean grades since
    the last dry run, or a model to sweep that the last one did not)."""
    suite = SUITES[name]
    ready = review_data.sweep_readiness(base, suite.surface)
    last = ((previous or {}).get("steps") or {}).get("readiness") or {}
    swept_rows, swept = last.get("swept_rows"), last.get("swept") or {}
    out: dict[str, Any] = {
        "rows": ready["rows"],
        "open": ready["open"],
        "done": ready["done"],
        "ready": ready["ready"],
        "reason": ready["reason"],
        "reserve": ready["reserve"],
        "swept_rows": swept_rows,
        "swept": swept,
        "dry_runs": {},
    }
    if not ready["ready"]:
        return {"state": SKIPPED, "why": ready["reason"] or "not ready", **out}
    moved = swept_rows is None or ready["rows"] - swept_rows >= READINESS_STEP
    wanted = {model: digest for model, digest in digests.items() if moved or swept.get(model) != digest}
    if not wanted:
        return {
            "state": SKIPPED,
            "why": f"{ready['rows'] - swept_rows} clean grades since the last dry run (it reruns at +{READINESS_STEP})",
            **out,
        }
    for model in wanted:
        code = heldout.run_sweep(name, base, model=model, labels="operator", device=device, router=router)
        target = registry.resolve_model(base, suite.surface, model)[0]
        found = lab_files.newest_report(base, name, target, "heldout") if code == 0 else None
        view = lab_files.heldout_view(*found) if found else None
        out["dry_runs"][model] = {
            "exit": code,
            "report": found[0].name if found else None,
            "best": view["best"] if view else None,
            "rows": view["rows"] if view else 0,
            "reserve": ((found[1].get("reserve") or {}).get("rows", 0)) if found else 0,
        }
    out.update(swept_rows=ready["rows"], swept={**swept, **wanted})
    failed = [model for model, block in out["dry_runs"].items() if block["exit"] not in (0, heldout.REFUSED)]
    return {"state": FAILED if failed else RAN, **({"why": f"sweep exit on {', '.join(failed)}"} if failed else {}), **out}


# ── the pack ──────────────────────────────────────────────────────────────────


def lease_targets(name: str, base: Path, targets: dict[str, Path | None], always: set[str], device: str) -> list[str]:
    """The router targets the pack will actually LOAD: the ones every step asks (`always`: the base, the champion,
    each challenger) and any other version whose golden report cannot be reused. The gpu claim is sized to these, so
    a rejected checkpoint kept on disk does not hold 1.6 GB of the slot all night. Router loading is lazy."""
    dataset = golden.dataset_version(base, name, by="nightly")["id"]
    return [
        target
        for target, path in targets.items()
        if target in always
        or golden.reusable_report(base, name, target, infer.checkpoint_digest(target, path), device, dataset) is None
    ]


def run(name: str, base: Path, *, device: str = "mps", router: Any = None) -> int:
    suite = SUITES[name]
    steps: dict[str, dict[str, Any]] = {}
    previous = lab_files.newest_nightly(base, name)
    started = rows.utc_stamp()
    run_step(steps, "registry", lambda: step_registry(base))

    targets = golden.registered_targets(base, suite.surface)
    rivals = shadow.challengers(base, suite.surface)
    targets.update({version: path for version, path in rivals.items() if version not in targets})
    champion = registry.served_model(base, suite.surface)
    if router is None:
        loading = lease_targets(
            name, base, targets, {"english", champion, *rivals, *stability_loading(name, base, device)}, device
        )
        infer.hold_device(device, targets=len(loading), models=loading)  # sized to what loads; open_router re-enters it
        router = infer.open_router(base, device, targets)

    run_step(steps, "golden", lambda: step_golden(name, base, targets, device, router))
    run_step(steps, "score", lambda: step_score(base, device, router))
    voters = ["laya:served", *(f"laya:{version}" for version in rivals), "laya:english", compare.FLOOR]
    run_step(steps, "compare", lambda: step_compare(name, base, voters, device, router))
    run_step(steps, "shadow", lambda: step_shadow(name, base, device, router))
    run_step(steps, "stability", lambda: step_stability(name, base, device, router))
    digests = {
        "served": infer.checkpoint_digest(champion, targets.get(champion)) if champion != "english" else None,
        **{version: infer.checkpoint_digest(version, path) for version, path in rivals.items()},
    }
    if champion == "english":
        digests.pop("served")
    run_step(steps, "readiness", lambda: step_readiness(name, base, device, router, previous[1] if previous else None, digests))

    from laya_lab import challenger

    run_step(steps, "challenger", lambda: challenger.nightly(name, base, device=device, router=router))

    from laya_lab import needs_you

    def step_needs_you():
        if hasattr(router, "unload"):
            router.unload()
        # A same-process lease reentry keeps its original units. Release the unloaded pack claim
        # before the choice trainer asks for TRAIN_GB, rather than under-account its larger fit.
        for slot in ("gpu", "cpu"):
            held = infer.HELD.pop(slot, None)
            if held is not None:
                held.release()
        return needs_you.nightly(name, base, device=device)

    run_step(steps, "needs_you", step_needs_you)

    dataset = (steps.get("golden") or {}).get("dataset_version") or golden.dataset_version(base, name, by="nightly")["id"]
    report = {
        "kind": KIND,
        "suite": name,
        "surface": suite.surface,
        "question_version": suite.question_version,
        "at": rows.utc_stamp(),
        "started_at": started,
        "run_id": secrets.token_hex(6),
        "device": device,
        "champion": champion,
        "challengers": list(rivals),
        "dataset_version": dataset,
        "verdict": verdict_of(steps),
        "headline": headline_of(steps, champion),
        "steps": steps,
        "laya_version": infer.laya_version(),
        **lab_files.eval_run_field(),
    }
    out = base / EVALS_DIRNAME / lab_files.nightly_report_name(name, lab_files.report_stamp())
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False, default=str) + "\n")
    for line in report_lines(report):
        print(line)
    print(f"  report → {out}")
    return 0


def report_lines(report: dict[str, Any]) -> list[str]:
    """Names, states and numbers: no text from the operator's files."""
    lines = [
        f"[laya-lab] nightly {report['suite']} · {report['verdict']} · champion {report['champion']}"
        + (f" · challengers {', '.join(report['challengers'])}" if report["challengers"] else "")
        + f" · dataset {report['dataset_version']} · {report['device']}"
    ]
    for step_name in STEPS:
        step = report["steps"].get(step_name)
        if step is None:
            continue
        detail = step.get("why") or ""
        if step_name == "golden" and step.get("targets"):
            detail = " · ".join(f"{t} {b['accuracy']}{' (reused)' if b['reused'] else ''}" for t, b in step["targets"].items())
        elif step_name == "shadow" and step.get("challengers"):
            detail = " · ".join(f"{c} +{b['new']} ({b['rows']})" for c, b in step["challengers"].items())
        elif step_name == "stability" and step.get("meter") and not step.get("why"):
            meter = step["meter"]
            detail = (
                f"STABLE κ {meter['kappa_average'] if meter['kappa_average'] is not None else '—'} · "
                f"{meter['states']} states · +{sum(step['scored'].values())} scored"
            )
        elif step_name == "readiness":
            detail = (
                f"{step.get('rows', 0)} clean ({step.get('open', 0)} open / {step.get('done', 0)} done, "
                f"{(step.get('reserve') or {}).get('rows', 0)} reserve)"
            ) + (f" · dry runs {', '.join(step['dry_runs'])}" if step.get("dry_runs") else f" · {detail}" if detail else "")
        lines.append(f"  {step_name:<10} {step['state']:<8} {step['seconds']:>7}s  {detail}")
    return lines
