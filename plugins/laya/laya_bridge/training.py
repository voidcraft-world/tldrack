"""The TRAINING page in one body (`layaTrainingStateSchema`): every Laya snapshot on disk, and the per-row scores a
promotion gate kept, so the page can draw a confusion table, a ROC curve and the raw points of any snapshot.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

WHAT A SNAPSHOT IS. One checkpoint folder of the surface under `checkpoints/`, registered or not: the champion, a
challenger, the champion a promotion replaced, and every candidate a gate refused that retention still keeps
(`lab_files.retention` owns which). Its numbers are the ones laya-lab left: the registry's `eval` and `trained` for a
registered version, the train report beside the weights for the rest.

WHERE THE POINTS COME FROM. A promotion gate scores its candidate and the served model on the SAME rows, the golden
cases and the labelled turn ends neither trained on, and keeps each row's two probabilities with its label
(laya-lab `gate.py`, `trimmed_evidence`). That is the only place a snapshot's per-row scores are kept, so a snapshot
has points when a gate judged it, and the served model has points wherever it was the other side. The newest gate
report of each candidate is the one read. Nothing here scores anything: the page shows what a run already measured.

NUMBERS ONLY. A row crosses as its label and two probabilities. Its id, its key and its text stay on disk.

The gated retrain's own view (its steps, its progress) is OPS's RETRAIN row: owned by `lab_files.retrain_view` — see
the note there. This body adds the one thing that row cannot hold, the last run that found nothing owed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from laya_bridge import files, lab_files, paths, rows
from laya_bridge.envelope import ok_body
from laya_bridge.files import clip, is_number, is_p
from laya_bridge.review_surfaces import REVIEW_SURFACES, ReviewSurface

#: The schema's bounds (`LAYA_TRAINING_*`).
SNAPSHOTS_MAX = 40
EVIDENCE_MAX = 8
POINTS_MAX = 2000
CHECKS_MAX = 8
WHY_MAX = 200
SETS = ("golden", "heldout")
#: A checkpoint folder ends in the local time it was trained at, to the minute or to the second.
NAME_STAMP = re.compile(r"-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})?$")
#: A closed gate's verdict as its report spells it (laya-lab `gate.py`).
GATE_VERDICTS = ("PASS", "REFUSE", "INCONCLUSIVE")


def trained_at(name: str) -> str | None:
    """`<fine-tune>-20261016-1401` → `2026-10-16T14:01:00`: local time, no `Z`, like every report stamp."""
    match = NAME_STAMP.search(name)
    if match is None:
        return None
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s or '00'}"


def _folders(base: Path, spec: ReviewSurface) -> set[str]:
    folder = base / paths.CHECKPOINTS_DIRNAME
    pattern = rows.checkpoint_name(paths.fine_tune_name(spec.name))
    if not files.is_real_dir(folder):
        return set()
    try:
        return {child.name for child in folder.iterdir() if pattern.fullmatch(child.name) and files.is_real_dir(child)}
    except OSError:
        return set()


def _train_report(base: Path, name: str) -> dict | None:
    """The train report beside an unregistered snapshot's weights, as `{items, before, after}`."""
    try:
        data = files.read_capped(
            base / paths.CHECKPOINTS_DIRNAME / name / lab_files.TRAIN_REPORT_FILENAME, lab_files.REPORT_CAP_BYTES
        )
        raw = files.loads(data) if data is not None else None
    except (files.SourceError, ValueError, RecursionError):
        return None
    return lab_files.eval_view(raw)


def snapshots(base: Path, spec: ReviewSurface, judged: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """Every snapshot of the surface, newest first. `judged` is `{candidate: its evidence row}` (`evidence`)."""
    view = paths.registry_versions(base)
    held = view["aliases"].get(spec.name) or {}
    alias_of = {name: alias for alias in paths.ALIASES if (name := held.get(alias))}
    versions = {name: version for name, version in view["versions"].items() if version["surface"] == spec.name}
    # By when each was trained, not by name: a folder trained under the legacy prefix sorts apart from its successors.
    names = sorted(_folders(base, spec) | set(versions), key=lambda name: (trained_at(name) or "", name), reverse=True)[
        :SNAPSHOTS_MAX
    ]
    out = []
    for name in names:
        version = versions.get(name)
        verdict = judged.get(name)
        trained = version["trained"] if version else None
        out.append(
            {
                "name": name,
                "alias": alias_of.get(name),
                "registered": version is not None,
                "rejected": bool(version and version["rejected_at"]),
                "at": trained_at(name),
                "gate": ((version["tags"] or {}).get("gate") if version else None),
                "verdict": verdict["verdict"] if verdict else None,
                "epochs": trained["epochs"] if trained else None,
                "train_rows": trained["train_rows"] if trained else None,
                "eval": version["eval"] if version else _train_report(base, name),
                "scored": name in judged or any(row["served"] == name for row in judged.values()),
            }
        )
    return out


def _side(raw: object) -> dict | None:
    """One side of a gate (`candidate` | `served`): its checkpoint and the two cuts it was judged at."""
    if not isinstance(raw, dict) or not lab_files.is_basename(raw.get("checkpoint")):
        return None
    if not is_p(raw.get("at")) or not is_p(raw.get("done_at")):
        return None
    return {"name": raw["checkpoint"], "at": float(raw["at"]), "done_at": float(raw["done_at"])}


def _set(name: str, raw: object) -> dict | None:
    """One set's rows as three aligned lists, or None when the block is not whole: a row is never half shown."""
    if not isinstance(raw, dict):
        return None
    expected, candidate, served = raw.get("expected"), raw.get("candidate"), raw.get("served")
    if not (isinstance(expected, list) and isinstance(candidate, list) and isinstance(served, list)):
        return None
    if not 0 < len(expected) <= POINTS_MAX or not len(expected) == len(candidate) == len(served):
        return None
    if not all(type(e) is bool for e in expected) or not all(is_p(p) for p in candidate) or not all(is_p(p) for p in served):
        return None
    return {
        "set": name,
        "expected": list(expected),
        "candidate": [round(float(p), 4) for p in candidate],
        "served": [round(float(p), 4) for p in served],
    }


def evidence_view(path: Path, report: Mapping[str, Any]) -> dict | None:
    """One gate report as the page reads it, or None when it kept no rows this can show."""
    kept = report.get("evidence")
    if not isinstance(kept, dict):
        return None
    candidate, served = _side(kept.get("candidate")), _side(kept.get("served"))
    sets = [found for name in SETS if (found := _set(name, kept.get(name))) is not None]
    if candidate is None or served is None or not sets:
        return None
    raw_costs = kept.get("costs")
    costs: Mapping[str, Any] = raw_costs if isinstance(raw_costs, dict) else {}
    raw_checks = report.get("checks")
    checks: Mapping[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
    why = report.get("why")
    return {
        "candidate": candidate["name"],
        "served": served["name"],
        "at": lab_files.report_at(path, report),
        "verdict": report["verdict"] if report.get("verdict") in GATE_VERDICTS else "INCONCLUSIVE",
        "promote": report.get("promote") is True,
        "why": clip(why, WHY_MAX) if isinstance(why, str) else None,
        "cuts": {
            "candidate": {"at": candidate["at"], "done_at": candidate["done_at"]},
            "served": {"at": served["at"], "done_at": served["done_at"]},
        },
        "false_warning_cost": float(costs["false_warning"])
        if is_number(costs.get("false_warning")) and costs["false_warning"] > 0
        else None,
        "checks": [
            {"name": name, "state": check["state"]}
            for name, check in checks.items()
            if isinstance(name, str)
            and lab_files.CHECK_NAME.fullmatch(name)
            and isinstance(check, dict)
            and check.get("state") in lab_files.GATE_CHECK_STATES
        ][:CHECKS_MAX],
        "sets": sets,
    }


def evidence(base: Path, spec: ReviewSurface) -> list[dict]:
    """The newest readable gate evidence of each candidate, newest first, at most EVIDENCE_MAX."""
    out: list[dict] = []
    seen: set[str] = set()
    for path, report in lab_files.gate_reports(base, spec.golden_suite):
        row = evidence_view(path, report)
        if row is None or row["candidate"] in seen:
            continue
        seen.add(row["candidate"])
        out.append(row)
        if len(out) == EVIDENCE_MAX:
            break
    return out


def last_run(base: Path, spec: ReviewSurface) -> dict | None:
    """The newest retrain report: a closed cycle's copy, or a run that found nothing owed and why (laya-lab
    `retrain.py` owns when one is owed). Names and numbers only."""
    found = lab_files.newest_retrain_report(base, spec.golden_suite)
    if found is None:
        return None
    path, report = found
    state = report.get("state")
    if state not in (*lab_files.RETRAIN_CLOSED_STATES, lab_files.RETRAIN_NOOP):
        return None
    raw_owed = report.get("owed")
    owed: Mapping[str, Any] = raw_owed if isinstance(raw_owed, dict) else {}
    reason = owed.get("reason")
    return {
        "at": lab_files.report_at(path, report),
        "state": state,
        "verdict": lab_files.RETRAIN_VERDICTS[state],
        "reason": clip(reason, WHY_MAX) if isinstance(reason, str) else None,
    }


def training_state(base: Path | None, surface: str) -> dict:
    """`GET /training`'s body. A missing store never fails it: the arrays come back empty and `message` says why."""
    spec = REVIEW_SURFACES[surface]
    body: dict[str, Any] = {"surface": surface, "snapshots": [], "evidence": [], "last_run": None}
    if base is None or not files.is_real_dir(base):
        return ok_body("The laya data folder is not there yet", **body)
    rows_ = evidence(base, spec)
    body["evidence"] = rows_
    body["snapshots"] = snapshots(base, spec, {row["candidate"]: row for row in rows_})
    body["last_run"] = last_run(base, spec)
    return ok_body("ok", **body)
