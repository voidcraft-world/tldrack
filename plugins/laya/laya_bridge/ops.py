"""AGENT RACK › LAYA's whole state in one body (`layaOpsStateSchema`), read from laya-lab's real outputs.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

There is no status file: every line comes from the file laya-lab actually wrote, read through
`lab_files.py`, so OPS and `laya-lab status` (which prints `ops_state(base, None, [])`) cannot
disagree. The runtime's facts arrive as arguments, never by import, so laya-lab's SDK-free venv
can call this too. Missing params never fail it: the arrays come back empty and `message` says why.
`registry` (one row per surface, `registry_row`) is the champion/challenger view of models.json v2 with the
challenger's shadow and the last nightly pack: the wire for P4-2's PROMOTE CHALLENGER.
`latency` and `stability` (one row per surface that has one) are the row's LATENCY and STABLE lines: the newest
`laya-lab latency` report, and STABLE κ computed here from the stability file (`lab_files.stability_summary`).
`retrain` (one row per surface whose retrain has a journal) is the row's RETRAIN line (`lab_files.retrain_view`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from laya_bridge import files, label_budget, lab_files, lab_jobs, ledger, paths, review_data
from laya_bridge.envelope import ok_body
from laya_bridge.review_surfaces import REVIEW_SURFACES, ReviewSurface

FLAGS_WINDOW_DAYS = 7
FLAG_FIELDS = ("decisions", "shown", "dismissed", "accepted")
ALIAS_ROWS = paths.ALIASES
#: laya-lab `nightly.py` step states, as OPS carries them.
NIGHTLY_STEP_STATES = ("ran", "reused", "skipped", "failed")


def shadow_summary(rows_in: list[dict], challenger: str) -> dict:
    """A challenger's shadow so far (`lab_files.read_shadow` rows): how many live verdicts, on how many UTC days, and
    the share where it would have drawn the SAME mark as the served model, each at its own cuts (`band_of`)."""
    mine = [row for row in rows_in if row["challenger"] == challenger]
    if not mine:
        return {"rows": 0, "days": 0, "agree": None, "disagree": 0, "since": None, "last": None}
    same = sum(
        review_data.band_of(row["served_p"], row["served_threshold"], row["served_done_threshold"])
        == review_data.band_of(row["challenger_p"], row["challenger_threshold"], row["challenger_done_threshold"])
        for row in mine
    )
    stamps = sorted(row["at"] for row in mine)
    return {
        "rows": len(mine),
        "days": len({at[:10] for at in stamps}),
        "agree": round(same / len(mine), 3),
        "disagree": len(mine) - same,
        "since": stamps[0],
        "last": stamps[-1],
    }


def _version_row(version: Mapping[str, Any] | None) -> dict | None:
    if version is None:
        return None
    cut = version["cut"]
    return {
        "name": version["name"],
        "registered_at": version["registered_at"],
        "gate": (version["tags"] or {}).get("gate"),
        "cut": {"at": cut["at"], "done_at": cut["done_at"], "swept_at": cut["swept_at"]} if cut else None,
    }


def registry_row(base: Path, spec: ReviewSurface) -> dict:
    """`layaOpsStateSchema.registry[]`: one surface's aliases (each with its own cut and gate tag), its rejects and what
    retention would prune, the challenger's shadow, and the last nightly pack. Names and numbers only."""
    view = paths.registry_versions(base)
    aliases = view["aliases"].get(spec.name) or {}
    row: dict[str, Any] = {"surface": spec.name, "version": view["version"], "legacy": view["legacy"]}
    for alias in ALIAS_ROWS:
        row[alias] = _version_row(view["versions"].get(aliases.get(alias) or ""))
    kept = lab_files.retention(base, spec.name, view)
    row["rejected"] = sum(
        1 for version in view["versions"].values() if version["surface"] == spec.name and version["rejected_at"]
    )
    row["prunable"] = {"checkpoints": len(kept["prunable"]), "bytes": sum(item["bytes"] for item in kept["prunable"])}
    shadow_rows, _code = lab_files.read_shadow(base, spec.name)
    row["shadow"] = shadow_summary(shadow_rows, row["challenger"]["name"]) if row["challenger"] else None
    found = lab_files.newest_nightly(base, spec.golden_suite)
    nightly = found[1] if found else None
    row["nightly"] = (
        None
        if nightly is None
        else {
            "at": nightly["at"] if lab_files.is_iso(nightly.get("at")) else None,
            "verdict": nightly["verdict"] if nightly.get("verdict") in lab_files.NIGHTLY_VERDICTS else "INCONCLUSIVE",
            "steps": {
                name: step["state"]
                for name, step in (nightly.get("steps") or {}).items()
                if isinstance(step, dict) and step.get("state") in NIGHTLY_STEP_STATES
            },
        }
    )
    head = ((nightly or {}).get("steps") or {}).get("needs_you")
    row["needs_you"] = (
        None
        if not isinstance(head, dict)
        else {
            "state": head.get("state") if head.get("state") in NIGHTLY_STEP_STATES else "failed",
            "shadow_days": head.get("shadow_days", 0),
            "shadow_rows": head.get("shadow_rows", 0),
            "ready": bool(head.get("ready")),
            "counts": head.get("counts") or {"done": 0, "running": 0, "you": 0},
            "kappa": head.get("kappa"),
            "pairs": head.get("pairs", 0),
            "precision": ((head.get("reserve") or {}).get("needs_you_precision") or {}).get("value"),
            "recall": ((head.get("reserve") or {}).get("needs_you_recall") or {}).get("value"),
        }
    )
    from laya_bridge.challenger import view

    row["promotion"] = view(base, spec.name)
    return row


def latency_row(base: Path, spec: ReviewSurface) -> dict | None:
    """`layaOpsStateSchema.latency[]`: the newest latency read of the surface's suite, or None before the first."""
    found = lab_files.newest_latency(base, spec.golden_suite)
    view = lab_files.latency_view(*found) if found else None
    return None if view is None else {"surface": spec.name, **view}


def stability_row(base: Path, spec: ReviewSurface) -> dict | None:
    """`layaOpsStateSchema.stability[]`: STABLE κ for the surface, each checkpoint at its live cut, or None while the
    stability file holds no row. Only the window's pairs cross HTTP."""
    rows_in, _code = lab_files.read_stability(base, spec.name)
    if not rows_in:
        return None
    summary = lab_files.stability_summary(rows_in, lab_files.stability_cuts(paths.registry_versions(base), spec.name))
    return {
        "surface": spec.name,
        "device": summary["device"],
        "states": summary["states"],
        "versions": len(summary["versions"]),
        "pairs": summary["pairs"][-lab_files.KAPPA_WINDOW :],
        "kappa_average": summary["kappa_average"],
        "window": summary["window"],
        "stable": summary["stable"],
        "last": summary["last"],
    }


def retrain_row(base: Path, spec: ReviewSurface) -> dict | None:
    """`layaOpsStateSchema.retrain[]`: the surface's retrain journal, the cycle in flight or the last one closed, or
    None before the first retrain."""
    journal = lab_files.read_retrain_journal(base, spec.golden_suite)
    return None if journal is None else {"surface": spec.name, **lab_files.retrain_view(journal)}


def _model_id(status: Mapping[str, Any] | None, served: review_data.Served) -> str | None:
    """What laya-serve reported for the name, else the id its checkpoint folder implies."""
    reported = (status or {}).get("model_ids") if isinstance(status, Mapping) else None
    found = reported.get(served.name) if isinstance(reported, Mapping) else None
    if isinstance(found, str) and paths.LAYA_LOCAL_MODEL_ID.fullmatch(found):
        return found
    return paths.laya_local_model_id(served.name, path=served.checkpoint)


def _threshold(spec: ReviewSurface, served: review_data.Served, cuts: Mapping[str, dict]) -> dict:
    cut = cuts.get(spec.name)
    if cut is not None and cut["model"] == served.name:
        done = cut.get("done_at")
        return {
            "at": cut["at"],
            "source": "swept",
            "swept_at": cut["swept_at"],
            "done_at": done if done is not None else min(spec.default_done_threshold, cut["at"]),
        }
    return {"at": spec.default_threshold, "source": "code", "swept_at": None, "done_at": spec.default_done_threshold}


def _scores(base: Path, spec: ReviewSurface, served: review_data.Served) -> dict | None:
    found, _code = lab_files.read_scores(base, spec.stem)
    if not found:
        return None
    newest = max(found, key=lambda row: row["at"])
    return {
        "at": newest["at"],
        "rows": len(found),
        "current": newest["model"] == served.name and newest["checkpoint"] == served.basename,
    }


def _served_row(
    base: Path | None, spec: ReviewSurface, status: Mapping[str, Any] | None, loaded: Mapping[str, str] | None
) -> tuple[dict, review_data.Served]:
    ready = base if files.is_real_dir(base) else None
    served = review_data.served_model(ready, spec.name, loaded) if ready else review_data.Served("english", None)
    view = paths.registry_view(ready)
    report = lab_files.newest_report(ready, spec.golden_suite, served.name, "golden") if ready else None
    row = {
        "name": served.name,
        "surface": spec.name,
        "checkpoint": served.basename,
        "model_id": _model_id(status, served),
        "threshold": _threshold(spec, served, paths.thresholds_view(view, loaded)),
        "eval": (view["models"].get(served.name) or {}).get("eval"),
        "golden": lab_files.golden_view(*report) if report else None,
        "scores": _scores(ready, spec, served) if ready else None,
    }
    return row, served


def _balance(cases: list[dict]) -> dict:
    n_open = sum(1 for case in cases if case["expected"])
    return {"cases": len(cases), "open": n_open, "done": len(cases) - n_open}


def ops_state(
    base: Path | None,
    runtime_status: dict | None,
    jobs: list[dict],
    *,
    loaded: Mapping[str, str] | None = None,
    lab_ok: bool | None = None,
    now: datetime | None = None,
    runner: Path | None = None,
) -> dict:
    """`layaOpsStateSchema`. `jobs=[]` reads as no lab job running (the queue file still says what waits);
    `lab_ok=None` checks laya-lab's venv."""
    ok = lab_jobs.lab_ok() if lab_ok is None else lab_ok
    body: dict[str, Any] = {
        "status": runtime_status,
        "served": [],
        "review": [],
        "flags_7d": {},
        "sweeps": [],
        "golden": [],
        "compare": [],
        "registry": [],
        "latency": [],
        "stability": [],
        "retrain": [],
        "jobs": list(jobs) if jobs else lab_jobs.job_rows(None, lab_jobs.read_queue(base), runner),
        "lab": {"ok": ok, "hint": None if ok else lab_jobs.LAB_HINT},
    }
    served: dict[str, tuple[dict, review_data.Served]] = {
        name: _served_row(base, spec, runtime_status, loaded) for name, spec in REVIEW_SURFACES.items()
    }
    body["served"] = [row for row, _served in served.values()]
    if base is None or not files.is_real_dir(base):
        return ok_body("The laya data folder is not there yet", **body)

    summary = ledger.summary(base, FLAGS_WINDOW_DAYS, now)
    flags = {surface: {field: counts[field] for field in FLAG_FIELDS} for surface, counts in summary.items()}
    for name in REVIEW_SURFACES:
        flags.setdefault(name, dict.fromkeys(FLAG_FIELDS, 0))
    body["flags_7d"] = flags

    for name, spec in REVIEW_SURFACES.items():
        row, model = served[name]
        snap = review_data.snapshot(base, name, loaded=loaded)
        body["review"].append(review_data.stats(snap, base, loaded=loaded, now=now))

        report = lab_files.newest_report(base, spec.golden_suite, model.name, "heldout")
        view = lab_files.heldout_view(*report) if report else None
        body["sweeps"].append(
            {
                "surface": name,
                "at": view["at"] if view else None,
                "rows": view["rows"] if view else 0,
                "labels": view["labels"] if view else None,
                "best": view["best"] if view else None,
                "live": row["threshold"]["at"],
                "source": row["threshold"]["source"],
                "kappa": view["kappa"] if view else None,
                "done_live": row["threshold"]["done_at"],
                "calibration": view["calibration"] if view else None,
            }
        )

        public, _pc, private, _vc = review_data.golden_suites(base, spec.golden_suite)
        flips = review_data.waiting_flips(base, spec.golden_suite, snap.fold)
        body["golden"].append(
            {
                "surface": name,
                "suite": spec.golden_suite,
                "public": _balance(public),
                "private": _balance(private),
                "candidates": len(flips[label_budget.TRAIN]),
                "reserved": len(flips[label_budget.RESERVE]),
            }
        )

        compared = lab_files.newest_compare(base, spec.golden_suite)
        compare = lab_files.compare_view(*compared, model.name) if compared else None
        if compare is not None:
            body["compare"].append({"surface": name, "suite": spec.golden_suite, **compare})
        body["registry"].append(registry_row(base, spec))
        for name_of, row_of in (("latency", latency_row), ("stability", stability_row), ("retrain", retrain_row)):
            found = row_of(base, spec)
            if found is not None:
                body[name_of].append(found)
    return ok_body("ok", **body)
