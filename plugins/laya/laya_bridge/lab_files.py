"""THE owner of every file laya-lab writes and this plugin reads: names, patterns, row builders, readers.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

laya-lab writes through the builders here and the plugin reads through the readers, so the two
cannot drift (`lab/tests/test_bridge_reads_lab.py` writes each file with laya-lab's real
writers and reads it back with `ops.ops_state` / `review_data`).

- `evals/<suite>-<model>-<stamp>.json`: a golden report. Today's shape is read as it is: no `kind`
  means golden, `at` comes from the filename stamp (LOCAL time, no `Z`), `accuracy` from
  `at_threshold.accuracy`, and κ is null when absent. New reports add `golden_report_fields`.
- `evals/heldout-<suite>-<model>-<labels>-<stamp>.json`: a held-out sweep (`heldout_report_head`);
  its misses carry keys, never text.
- `evals/compare-<suite>-<stamp>.json`: voters side by side (`laya-lab compare`), read as a COMPARE
  readout of its own (`compare_view`), never as any one model's golden or held-out report.
- `eval_run` on a golden and a compare report: the `evals run` that wrote it (`eval_run_field`), so the
  registry evals `laya-golden` and `laya-compare` find THEIR report for a verdict (`run_verdict`).
- `data/<stem>-laya-p.jsonl`: the served model's scores, one `scores_row` per key.
- `golden/<suite>.private.jsonl`: the operator's promoted cases (`private_golden_case`), first line
  `PRIVATE_GOLDEN_HEADER`; never committed.
- `models.json` (v2: `paths.py` owns the shape) `versions.<version>.trained` (`trained_entry`), `.cut` (`threshold_entry`,
  the swept cut of that checkpoint) and `.tags` (`gate_tags`, the verdict that registered it).
- `evals/nightly-<suite>-<stamp>.json`: the nightly pack's report (laya-lab `nightly.py`), one step per eval it ran.
- `shadow/<surface>.jsonl`: each @challenger's p beside the served p on the ledger's live states (`shadow_row`).
- `golden/versions.jsonl`: the dataset versions every report is stamped with (`dataset_row`, `dataset_version_id`).
- `evals/latency-<suite>-<stamp>.json`: the ledger's `ms` by state length, Mac load and cold start, and the token
  overflow of the states Laya is asked (laya-lab `latency.py`), read as OPS's LATENCY line (`latency_view`).
- `stability/<surface>-stop-set.jsonl` (`stop_set_row`: operator text) and `stability/<surface>.jsonl` (`stability_row`):
  the stability meter's fixed states and each checkpoint's p on them; `stability_summary` is THE STABLE κ.
- `retrain/<suite>.json`: the gated retrain's step journal (laya-lab `retrain.py`), the cycle in flight or last closed,
  read as OPS's RETRAIN line (`retrain_view`); `evals/retrain-<suite>-<stamp>.json` is a closed cycle's copy, which
  `run_verdict` reads.
- `checkpoints/<checkpoint>/laya-lab-report.json` (`TRAIN_REPORT_FILENAME`): laya's train report beside the weights
  (laya-lab `train.fit`), which the TRAINING page reads for a snapshot the registry does not hold.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from math import comb
from pathlib import Path
from typing import Any, TypeGuard

from laya_bridge import agreement, env_names, files, rows
from laya_bridge.files import is_count, is_number, is_p, utf16_len
from laya_bridge.ledger import DECISION_ID, STATE_MAX
from laya_bridge.paths import (
    ALIASES,
    CHECKPOINTS_DIRNAME,
    EVALS_DIRNAME,
    RETRAIN_DIRNAME,
    data_file,
    dataset_versions_path,
    fine_tune_name,
    served_name,
    shadow_path,
    stability_path,
    stop_set_path,
)
from laya_bridge.wire import MODEL_NAME

REPORT_CAP_BYTES = 1024 * 1024
SCORES_CAP_BYTES = 8 * 1024 * 1024
GOLDEN_CAP_BYTES = 1024 * 1024
HELDOUT_LABELS = ("operator", "model", "merged")
PRIVATE_GOLDEN_HEADER = "# private golden suite: operator text — never commit"

KEY = re.compile(r"[0-9a-f]{16}")
CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")
BASENAME_MAX = 120
BASENAME = re.compile(r"[^/\\]+")
ISO_MAX_CHARS = 40
#: Set by an eval runner (lab_jobs.py owns the runner) on every command it starts: a 12-hex run id. Read through
#: `env_names.read`, so a runner that still sets the older name is heard too.
EVAL_RUN_ENV = "TLDRACK_EVAL_RUN"
EVAL_RUN = re.compile(r"[0-9a-f]{12}")
#: A compare voter's name: `laya:<model>`, an endpoint's `<name>` (laya-lab `compare.NAME`), or `mean`.
VOTER_NAME = re.compile(r"(?:laya:)?[a-z0-9][a-z0-9_.-]{0,79}")
COMPARE_SETS = ("golden", "heldout")
COMPARE_VOTERS_MAX = 8
#: `EVAL_DIGEST_RE` in schemas/src/eval-runs.ts (a verdict's model digest and dataset version).
EVAL_DIGEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,79}")
ACCELERATORS = ("mps", "cuda", "cpu")

_REPORTS = files.StampedCache()
_SCORES = files.StampedCache()
_SUITES = files.StampedCache()
_SHADOW = files.StampedCache()
_DATASETS = files.StampedCache()
_STABILITY = files.StampedCache()
_STOP_SETS = files.StampedCache()


# ── small checks ──────────────────────────────────────────────────────────────


def is_iso(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < utf16_len(value) <= ISO_MAX_CHARS


def is_basename(value: object) -> TypeGuard[str]:
    """A checkpoint or report name that may cross HTTP: no separator, ≤ 120 UTF-16 units (`basename` in the schema)."""
    return (
        isinstance(value, str)
        and BASENAME.fullmatch(value) is not None
        and value not in (".", "..")
        and utf16_len(value) <= BASENAME_MAX
    )


def kappa_or_none(value: object) -> float | None:
    return float(value) if is_number(value) and -1 <= value <= 1 else None


# ── report names ──────────────────────────────────────────────────────────────


def report_stamp(now: datetime | None = None) -> str:
    """UTC `YYYYMMDD-HHMMSS` for every NEW report (today's reports carry a local stamp)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S")


def golden_report_name(suite: str, model: str, stamp: str) -> str:
    return f"{suite}-{model}-{stamp}.json"


def heldout_report_name(suite: str, model: str, labels: str, stamp: str) -> str:
    return f"heldout-{suite}-{model}-{labels}-{stamp}.json"


def compare_report_name(suite: str, stamp: str) -> str:
    """`laya-lab compare`'s report. Neither golden nor held-out pattern matches it, so a voter beside Laya never speaks
    for a model's own readout; OPS reads it only as COMPARE (`newest_compare`)."""
    return f"compare-{suite}-{stamp}.json"


def compare_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^compare-{re.escape(suite)}-(?P<stamp>\d{{8}}-\d{{6}})\.json$")


def gate_report_name(suite: str, stamp: str, run_id: str) -> str:
    """`laya-lab gate`'s verdict (laya-lab `gate.py`). No golden, held-out or compare pattern matches it. The run id
    keeps a promotion verdict from being overwritten by a prospective run in the same second."""
    return f"gate-{suite}-{stamp}-{run_id}.json"


def gate_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^gate-{re.escape(suite)}-(?P<stamp>\d{{8}}-\d{{6}})-(?P<run>[0-9a-f]{{12}})\.json$")


def nightly_report_name(suite: str, stamp: str) -> str:
    """`laya-lab nightly`'s report: one step per eval of the pack. No golden, held-out, compare or gate pattern matches."""
    return f"nightly-{suite}-{stamp}.json"


def nightly_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^nightly-{re.escape(suite)}-(?P<stamp>\d{{8}}-\d{{6}})\.json$")


def latency_report_name(suite: str, stamp: str) -> str:
    """`laya-lab latency`'s report (laya-lab `latency.py`). No golden, held-out, compare, gate or nightly pattern matches."""
    return f"latency-{suite}-{stamp}.json"


def latency_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^latency-{re.escape(suite)}-(?P<stamp>\d{{8}}-\d{{6}})\.json$")


def retrain_report_name(suite: str, stamp: str) -> str:
    """A closed retrain cycle's copy of its journal (laya-lab `retrain.py`), or a run that found nothing owed. No
    golden, held-out, compare, gate, nightly or latency pattern matches it."""
    return f"retrain-{suite}-{stamp}.json"


def retrain_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^retrain-{re.escape(suite)}-(?P<stamp>\d{{8}}-\d{{6}})\.json$")


def teacher_report_name(suite: str, stamp: str) -> str:
    """`laya-lab teacher-kappa`'s report (laya_lab/teacher_local.py). No OPS reader matches it: a teacher's κ is
    a verdict about the teacher, never a served model's number."""
    return f"teacher-kappa-{suite}-{stamp}.json"


def teacher_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^teacher-kappa-{re.escape(suite)}-\d{{8}}-\d{{6}}\.json$")


def golden_report_pattern(suite: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(suite)}-(?P<model>[a-z0-9-]+)-(?P<stamp>\d{{8}}-\d{{6}})\.json$")


def heldout_report_pattern(suite: str) -> re.Pattern[str]:
    labels = "|".join(HELDOUT_LABELS)
    return re.compile(
        rf"^heldout-{re.escape(suite)}-(?P<model>[a-z0-9-]+)-(?P<labels>{labels})-(?P<stamp>\d{{8}}-\d{{6}})\.json$"
    )


def stamp_at(stamp: str) -> str | None:
    """`20260102-010101` → `2026-01-02T01:01:01`: no `Z`, because today's stamps are local time."""
    match = STAMP.fullmatch(stamp)
    if match is None:
        return None
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _parse_object(data: bytes) -> dict | None:
    try:
        value = files.loads(data)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _listing(base: Path, pattern: re.Pattern[str]) -> list[tuple[re.Match[str], Path]]:
    """Every `evals/` file `pattern` matches, newest stamp first; [] when the folder is missing or unlistable."""
    folder = base / EVALS_DIRNAME
    if not files.is_real_dir(folder):
        return []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = [(match, folder / name) for name in names if (match := pattern.fullmatch(name)) is not None]
    return sorted(found, key=lambda item: item[0]["stamp"], reverse=True)


def _report(path: Path) -> dict | None:
    code, report = _REPORTS.get(path, REPORT_CAP_BYTES, _parse_object)
    return report if code == files.OK else None


def newest_report(base: Path, suite: str, model: str, kind: str) -> tuple[Path, dict] | None:
    """The newest `kind` report (golden | heldout) of `suite` for `model`, or None.

    Matched by filename, then by the report's own `model` (and `kind`; a report with none is golden),
    so a renamed file never speaks for another model. A report under the legacy name is the same model's
    (`paths.served_name`). Each read is capped at 1 MiB.
    """
    pattern = golden_report_pattern(suite) if kind == "golden" else heldout_report_pattern(suite)
    model = served_name(model)
    for match, path in _listing(base, pattern):
        if served_name(match["model"]) != model:
            continue
        report = _report(path)
        if report is None or served_name(str(report.get("model"))) != model:
            continue
        if report.get("kind", "golden") != kind or report.get("suite", suite) != suite:
            continue
        return path, report
    return None


def gate_reports(base: Path, suite: str) -> list[tuple[Path, dict]]:
    """Every readable gate verdict of `suite`, newest first (each read capped like every report)."""
    found = []
    for _match, path in _listing(base, gate_report_pattern(suite)):
        report = _report(path)
        if report is not None and report.get("kind") == "gate" and report.get("suite", suite) == suite:
            found.append((path, report))
    return found


def _stamp_of(path: Path) -> str:
    match = re.search(r"(\d{8}-\d{6})\.json$", path.name)
    return match.group(1) if match else ""


# ── golden reports ────────────────────────────────────────────────────────────


def golden_report_fields(at: str, run_id: str, kappa: float | None, checkpoint: dict | None) -> dict:
    """The fields a NEW golden report adds over today's shape (laya-lab `golden.py` merges them)."""
    return {"kind": "golden", "at": at, "run_id": run_id, "kappa": kappa, "checkpoint": checkpoint}


def golden_view(path: Path, report: dict) -> dict | None:
    """`{at, cases, accuracy, kappa, brier}` for OPS, from either report shape; None when unreadable."""
    head = report.get("at_threshold") if isinstance(report.get("at_threshold"), dict) else {}
    at = report.get("at") if is_iso(report.get("at")) else stamp_at(_stamp_of(path))
    accuracy = head.get("accuracy")
    if at is None or not is_count(report.get("cases")) or not is_number(accuracy) or not is_number(report.get("brier")):
        return None
    kappa = kappa_or_none(report["kappa"]) if "kappa" in report else kappa_or_none(head.get("kappa"))
    return {"at": at, "cases": report["cases"], "accuracy": float(accuracy), "kappa": kappa, "brier": float(report["brier"])}


def golden_facts(report: dict) -> dict:
    """What the REVIEW deck reads from a golden report: its cut, question version, checkpoint, misses, p."""
    head = report.get("at_threshold") if isinstance(report.get("at_threshold"), dict) else {}
    checkpoint = report.get("checkpoint") if isinstance(report.get("checkpoint"), dict) else {}
    misses = [miss["id"] for miss in report.get("misses") or [] if isinstance(miss, dict) and isinstance(miss.get("id"), str)]
    raw_p = report.get("probabilities") if isinstance(report.get("probabilities"), dict) else {}
    return {
        "threshold": float(head["threshold"]) if is_p(head.get("threshold")) else None,
        "question_version": report["question_version"] if is_count(report.get("question_version")) else 0,
        "checkpoint": checkpoint.get("name") if is_basename(checkpoint.get("name")) else None,
        "misses": misses,
        "probabilities": {case: float(p) for case, p in raw_p.items() if isinstance(case, str) and is_p(p)},
    }


# ── held-out reports ──────────────────────────────────────────────────────────


def heldout_report_head(at: str, suite: str, model: str, labels: str, rows: int, best: float | None, kappa: float | None) -> dict:
    """The fields `heldout_view` reads; laya-lab adds its sweep and its misses (keys only, never text)."""
    return {
        "kind": "heldout",
        "at": at,
        "suite": suite,
        "model": model,
        "labels": labels,
        "rows": rows,
        "recommended": {"at": best},
        "kappa": kappa,
    }


def calibration_view(raw: object) -> dict | None:
    """A sweep's operator `calibration` block as OPS reads it, numbers only (laya-lab `metrics.calibration` writes it;
    the reliability bins stay in the report): `{rows, ece, teacher_rows, teacher_ece, temperature, ece_refit}`, or None
    when the report has none (fewer than `rows.SWEEP_MIN_ROWS` operator labels) or it is malformed."""
    if not isinstance(raw, dict) or not is_count(raw.get("rows")):
        return None
    teacher = raw.get("teacher") if isinstance(raw.get("teacher"), dict) else {}
    temperature = raw.get("temperature")
    return {
        "rows": raw["rows"],
        "ece": _p(raw.get("ece")),
        "teacher_rows": teacher["rows"] if is_count(teacher.get("rows")) else 0,
        "teacher_ece": _p(teacher.get("ece")),
        "temperature": float(temperature) if is_number(temperature) and temperature > 0 else None,
        "ece_refit": _p(raw.get("ece_refit")),
    }


def heldout_view(path: Path, report: dict) -> dict | None:
    """`{at, rows, labels, best, kappa, calibration}` for OPS' SWEEP line; None when unreadable."""
    recommended = report.get("recommended") if isinstance(report.get("recommended"), dict) else {}
    at = report.get("at") if is_iso(report.get("at")) else stamp_at(_stamp_of(path))
    if at is None or not is_count(report.get("rows")):
        return None
    labels = report.get("labels")
    return {
        "at": at,
        "rows": report["rows"],
        "labels": labels if labels in HELDOUT_LABELS else None,
        "best": float(recommended["at"]) if is_p(recommended.get("at")) else None,
        "kappa": kappa_or_none(report.get("kappa")),
        "calibration": calibration_view(report.get("calibration")),
    }


# ── compare reports ───────────────────────────────────────────────────────────


def _newest_of(base: Path, kind: str, suite: str) -> tuple[Path, dict] | None:
    """The newest report of `suite` whose name matches `kind`'s pattern and that names that kind and suite itself
    (each read capped like every report), or None."""
    for _match, path in _listing(base, REPORT_PATTERNS[kind](suite)):
        report = _report(path)
        if report is not None and report.get("kind") == kind and report.get("suite") == suite:
            return path, report
    return None


def newest_retrain_report(base: Path, suite: str) -> tuple[Path, dict] | None:
    """The newest retrain report of `suite` (a closed cycle's copy, or a run that found nothing owed), or None."""
    return _newest_of(base, "retrain", suite)


def report_at(path: Path, report: Mapping[str, Any]) -> str | None:
    """When a report was written: its own `at`, else its filename's stamp (local time, no `Z`)."""
    return report["at"] if is_iso(report.get("at")) else stamp_at(_stamp_of(path))


def newest_compare(base: Path, suite: str) -> tuple[Path, dict] | None:
    """The newest `laya-lab compare` report of `suite`, or None."""
    return _newest_of(base, "compare", suite)


def _number(value: object) -> float | None:
    return float(value) if is_number(value) else None


def _p(value: object) -> float | None:
    return float(value) if is_p(value) else None


def _voter_view(name: str, scores: Any, served: str | None) -> dict | None:
    if not VOTER_NAME.fullmatch(name) or not isinstance(scores, dict):
        return None
    live = scores.get("at_live") if isinstance(scores.get("at_live"), dict) else {}
    latency = scores.get("latency_ms") if isinstance(scores.get("latency_ms"), dict) else {}
    return {
        "name": name,
        "served": served is not None and name == f"laya:{served}",
        "auc": _p(scores.get("auc")),
        "brier": _number(scores.get("brier")),
        "ece": _number(scores.get("ece")),
        "accuracy": _p(live.get("accuracy")),
        "p50_ms": p50 if (p50 := _number(latency.get("p50"))) is not None and p50 >= 0 else None,
    }


def compare_view(path: Path, report: dict, served: str | None) -> dict | None:
    """`{at, sets: [{set, rows, open, done, voters}], heldout_skipped}` for OPS' COMPARE line; None when unreadable.

    Names and numbers only: the per-row p map, a voter's URL and the skip reason (a sentence that may carry a path)
    never cross HTTP. `served` marks the voter that is the surface's served Laya model.
    """
    at = report.get("at") if is_iso(report.get("at")) else stamp_at(_stamp_of(path))
    sets_raw = report.get("sets")
    if at is None or not isinstance(sets_raw, dict):
        return None
    sets = []
    for name in COMPARE_SETS:
        block = sets_raw.get(name)
        if not isinstance(block, dict) or not all(is_count(block.get(k)) for k in ("rows", "open", "done")):
            continue
        voters_raw = block.get("voters") if isinstance(block.get("voters"), dict) else {}
        voters = [
            view
            for voter, scores in voters_raw.items()
            if isinstance(voter, str) and (view := _voter_view(voter, scores, served)) is not None
        ]
        sets.append(
            {
                "set": name,
                "rows": block["rows"],
                "open": block["open"],
                "done": block["done"],
                "voters": voters[:COMPARE_VOTERS_MAX],
            }
        )
    if not sets:
        return None
    return {"at": at, "sets": sets, "heldout_skipped": report.get("heldout_note") is not None}


# ── an eval run's report, and its verdict ─────────────────────────────────────


def eval_run_field(env: Mapping[str, str] = os.environ) -> dict:
    """`{"eval_run": <id>}` inside an `evals run`, else `{"eval_run": None}`: laya-lab merges it into a golden and a
    compare report, so `run_verdict` reads the report of THAT run and never an older one."""
    raw = env_names.read(env, EVAL_RUN_ENV) or ""
    return {"eval_run": raw if EVAL_RUN.fullmatch(raw) else None}


REPORT_PATTERNS = {
    "compare": compare_report_pattern,
    "golden": golden_report_pattern,
    "nightly": nightly_report_pattern,
    "latency": latency_report_pattern,
    "retrain": retrain_report_pattern,
}


def newest_nightly(base: Path, suite: str) -> tuple[Path, dict] | None:
    """The newest nightly pack report of `suite`, or None."""
    return _newest_of(base, "nightly", suite)


def newest_latency(base: Path, suite: str) -> tuple[Path, dict] | None:
    """The newest latency report of `suite`, or None."""
    return _newest_of(base, "latency", suite)


def run_report(base: Path, kind: str, suite: str, run: str) -> tuple[Path, dict] | None:
    """The newest `kind` report (golden | compare | nightly | latency | retrain) of `suite` that eval run `run` wrote, or None."""
    pattern = REPORT_PATTERNS[kind](suite)
    for _match, path in _listing(base, pattern):
        report = _report(path)
        if report is None or report.get("eval_run") != run or report.get("suite", suite) != suite:
            continue
        if report.get("kind", "golden") == kind:
            return path, report
    return None


def _digest(value: object) -> str | None:
    return value if isinstance(value, str) and EVAL_DIGEST.fullmatch(value) else None


def _headline(**numbers: float | None) -> dict:
    return {key: round(float(value), 4) for key, value in numbers.items() if is_number(value)}


def _golden_verdict(report: dict) -> dict:
    head = report.get("at_threshold") if isinstance(report.get("at_threshold"), dict) else {}
    checkpoint = report.get("checkpoint") if isinstance(report.get("checkpoint"), dict) else {}
    cases = report.get("cases") if is_count(report.get("cases")) else 0
    headline = _headline(
        cases=cases,
        accuracy=head.get("accuracy"),
        kappa=report.get("kappa", head.get("kappa")),
        brier=report.get("brier"),
        ece=report.get("ece"),
    )
    device = report.get("device")
    return {
        "verdict": "PASS" if cases > 0 and "accuracy" in headline else "INCONCLUSIVE",
        "headline": headline,
        "model_digest": _digest(checkpoint.get("digest")) or _digest(checkpoint.get("name")) or _digest(report.get("model")),
        "dataset_version": _digest(report.get("dataset_version"))
        or (f"q{report['question_version']}" if is_count(report.get("question_version")) else None),
        "accelerator": device if device in ACCELERATORS else None,
    }


def _compare_verdict(report: dict) -> dict:
    """The first Laya voter (`laya:served` by default) on held-out grades when there were any, else on golden."""
    sets = report.get("sets") if isinstance(report.get("sets"), dict) else {}
    voters = [v for v in report.get("voters") or [] if isinstance(v, dict) and isinstance(v.get("name"), str)]
    lead = next((v for v in voters if v.get("kind") == "laya"), voters[0] if voters else None)
    block = next(
        (
            sets[name]
            for name in ("heldout", "golden")
            if isinstance(sets.get(name), dict) and is_count(sets[name].get("rows")) and sets[name]["rows"] > 0
        ),
        None,
    )
    # A report is read off disk: a `voters` block that is not an object is a broken report, never a crash.
    by_voter = block.get("voters") if block is not None else None
    scores = by_voter.get(lead["name"]) if lead is not None and isinstance(by_voter, dict) else None
    if not isinstance(scores, dict):
        return {"verdict": "INCONCLUSIVE", "headline": {}}
    live = scores.get("at_live") if isinstance(scores.get("at_live"), dict) else {}
    latency = scores.get("latency_ms") if isinstance(scores.get("latency_ms"), dict) else {}
    device = report.get("device")
    return {
        "verdict": "PASS",
        "headline": _headline(
            cases=block["rows"],
            auc=scores.get("auc"),
            brier=scores.get("brier"),
            ece=scores.get("ece"),
            accuracy=live.get("accuracy"),
            kappa=live.get("kappa"),
            latency_ms_p50=latency.get("p50"),
        ),
        "model_digest": _digest(lead.get("digest")) or _digest(lead.get("model")),
        "dataset_version": _digest(report.get("dataset_version"))
        or (f"q{report['question_version']}" if is_count(report.get("question_version")) else None),
        "accelerator": device if device in ACCELERATORS else None,
    }


NIGHTLY_VERDICTS = ("PASS", "FAIL", "INCONCLUSIVE")


def _nightly_verdict(report: dict) -> dict:
    """The pack's own verdict (laya-lab `nightly.py` owns the rule) and the served model's golden and compare numbers."""
    verdict = report.get("verdict") if report.get("verdict") in NIGHTLY_VERDICTS else "INCONCLUSIVE"
    head = report.get("headline") if isinstance(report.get("headline"), dict) else {}
    device = report.get("device")
    dataset = report.get("dataset_version")
    return {
        "verdict": verdict,
        "headline": _headline(
            **{
                key: head.get(key)
                for key in ("cases", "accuracy", "kappa", "brier", "ece", "auc", "latency_ms_p50", "trials", "skipped", "failed")
            }
        ),
        "model_digest": _digest(head.get("model_digest")),
        "dataset_version": _digest(dataset),
        "accelerator": device if device in ACCELERATORS else None,
    }


# ── the latency report ────────────────────────────────────────────────────────

#: laya-lab `latency.py`'s truncation block states: measured · nothing to measure · the tokenizer could not be opened.
TRUNCATION_STATES = ("ran", "skipped", "failed")


def _ms(value: object) -> float | None:
    return float(value) if is_number(value) and value >= 0 else None


def _start_view(buckets: object, name: str) -> dict:
    block = next((b for b in buckets if isinstance(b, dict) and b.get("bucket") == name), {}) if isinstance(buckets, list) else {}
    return {"n": block["n"] if is_count(block.get("n")) else 0, "p50_ms": _ms(block.get("p50"))}


def latency_view(path: Path, report: dict) -> dict | None:
    """`{at, days, decisions, p50_ms, p90_ms, le_100, cold, warm, truncation}` for OPS' LATENCY line; None when
    unreadable. `cold` and `warm` are `{n, p50_ms}`; `truncation` is `{states, overflowed}` once it was measured.
    Numbers only: the buckets by length and by load stay in the report."""
    at = report.get("at") if is_iso(report.get("at")) else stamp_at(_stamp_of(path))
    overall = report.get("latency_ms") if isinstance(report.get("latency_ms"), dict) else {}
    if at is None or not is_count(overall.get("n")):
        return None
    window = report.get("window") if isinstance(report.get("window"), dict) else {}
    cut = report.get("truncation") if isinstance(report.get("truncation"), dict) else {}
    measured = cut.get("state") == "ran" and is_count(cut.get("states")) and is_count(cut.get("overflowed"))
    return {
        "at": at,
        "days": window["days"] if is_count(window.get("days")) else None,
        "decisions": overall["n"],
        "p50_ms": _ms(overall.get("p50")),
        "p90_ms": _ms(overall.get("p90")),
        "le_100": _p(overall.get("le_100")),
        "cold": _start_view(report.get("by_start"), "cold"),
        "warm": _start_view(report.get("by_start"), "warm"),
        "truncation": {"states": cut["states"], "overflowed": cut["overflowed"]} if measured else None,
    }


def _latency_verdict(report: dict) -> dict:
    """A measurement with no bar: PASS once it read a decision, INCONCLUSIVE on an empty window, FAIL when the
    tokenizer it was to measure the window with could not be opened (half the eval did not run)."""
    overall = report.get("latency_ms") if isinstance(report.get("latency_ms"), dict) else {}
    cut = report.get("truncation") if isinstance(report.get("truncation"), dict) else {}
    decisions = overall["n"] if is_count(overall.get("n")) else 0
    verdict = "FAIL" if cut.get("state") == "failed" else "PASS" if decisions > 0 else "INCONCLUSIVE"
    tokenizer = cut.get("tokenizer") if isinstance(cut.get("tokenizer"), dict) else {}
    return {
        "verdict": verdict,
        "headline": _headline(cases=decisions, latency_ms_p50=overall.get("p50"), latency_ms_p95=overall.get("p95")),
        "model_digest": _digest(tokenizer.get("digest")) or _digest(tokenizer.get("model")),
        "dataset_version": f"q{report['question_version']}" if is_count(report.get("question_version")) else None,
        "accelerator": None,
    }


def run_verdict(base: Path | None, kind: str, suite: str, env: Mapping[str, str] = os.environ) -> tuple[dict | None, str]:
    """(`VerdictProbeSchema` object, '') for the report the current eval run wrote, or (None, why). `laya-lab verdict`
    prints it: the verdict probe an eval runner calls after `laya-golden`, `laya-compare`, `laya-nightly`,
    `laya-latency` and `laya-retrain`. Golden and compare are measurements with no bar: PASS when the report
    read and scored something, INCONCLUSIVE when it scored nothing; the nightly pack, the latency read and the retrain
    carry their own."""
    run = eval_run_field(env)["eval_run"]
    if run is None:
        return None, f"{env_names.spelled(EVAL_RUN_ENV)} is not set: a verdict is read inside an eval runner's run only"
    if base is None or not files.is_real_dir(base):
        return None, "the laya data folder is not there"
    found = run_report(base, kind, suite, run)
    if found is None:
        return None, f"no {kind} report of {suite} carries eval run {run}"
    _path, report = found
    verdicts = {
        "compare": _compare_verdict,
        "golden": _golden_verdict,
        "nightly": _nightly_verdict,
        "latency": _latency_verdict,
        "retrain": _retrain_verdict,
    }
    return verdicts[kind](report), ""


# ── the retrain's step journal ────────────────────────────────────────────────

#: laya-lab `retrain.py`'s steps, in order, and the states a step can be in.
RETRAIN_STEPS = ("export", "label", "build", "train", "gate", "register", "sweep", "card")
RETRAIN_STEP_STATES = ("pending", "running", "done", "skipped", "failed")
#: A cycle: `running` (in flight, or cut off mid-step by a kill: the next run resumes it at that step) · `waiting`
#: (it stopped at a held lease) · `failed` (a step failed: the next run retries it) · `done` (closed, its checkpoint
#: registered as @challenger) · `refused` (closed, the promotion gate refused the candidate) · `abandoned` (closed, one
#: step failed too often: the next owed retrain starts over) · `noop` (a run that found nothing owed: only ever a
#: report, never the journal).
RETRAIN_OPEN_STATES = ("running", "waiting", "failed")
RETRAIN_CLOSED_STATES = ("done", "refused", "abandoned")
RETRAIN_STATES = (*RETRAIN_OPEN_STATES, *RETRAIN_CLOSED_STATES)
RETRAIN_NOOP = "noop"
RETRAIN_VERDICTS = {"done": "PASS", "refused": "REFUSE", "abandoned": "FAIL", RETRAIN_NOOP: "NO-OP"}
RETRAIN_VERSION = 1
#: The card the retrain's last step writes beside a checkpoint's weights (laya-lab `retrain.model_card`).
MODEL_CARD_FILENAME = "MODEL_CARD.md"
#: laya's train report beside a checkpoint's weights (laya-lab `train.fit`).
TRAIN_REPORT_FILENAME = "laya-lab-report.json"
RETRAIN_CAP_BYTES = 256 * 1024
#: One line of names and numbers a journal block keeps (`why`, the cycle's `owed.reason`), as OPS carries it.
RETRAIN_WHY_MAX = 200
RETRAIN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
#: A failed step's error as OPS carries it: the exception's type name. Its message stays in the journal
#: (`laya-lab status` prints it): an error's text is not held to "names and numbers only".
ERROR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: The local teacher's tag as OPS carries it (`gpt-oss:20b`), or `sonnet`.
TEACHER_NAME = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")


def retrain_journal_path(base: Path, suite: str) -> Path:
    return base / RETRAIN_DIRNAME / f"{suite}.json"


def read_retrain_journal(base: Path | None, suite: str) -> dict | None:
    """The suite's retrain journal as laya-lab wrote it, or None: absent, a link, too large, not this version, or
    naming another suite. Never raises: laya-lab's resume and OPS both read through here."""
    if base is None or not files.is_real_dir(base / RETRAIN_DIRNAME):
        return None
    try:
        data = files.read_capped(retrain_journal_path(base, suite), RETRAIN_CAP_BYTES)
    except files.SourceError:
        return None
    journal = _parse_object(data) if data is not None else None
    if (
        journal is None
        or journal.get("kind") != "retrain"
        or journal.get("v") != RETRAIN_VERSION
        or journal.get("suite") != suite
        or journal.get("state") not in RETRAIN_STATES
        or not isinstance(journal.get("id"), str)
        or not RETRAIN_ID.fullmatch(journal["id"])
        or not isinstance(journal.get("steps"), dict)
    ):
        return None
    return journal


def train_progress(epoch: int, epochs: int, step: int, steps: int | None, loss: float | None) -> dict:
    """The train step's `progress` in the journal, written while a fit runs (laya-lab `retrain.step_train`): the epoch
    it is in (1-based), the micro-batches done in that epoch, how many an epoch holds (None until the trainer said how
    many items it trains on), and the last loss it printed."""
    return {"epoch": epoch, "epochs": epochs, "step": step, "steps": steps, "loss": loss}


def train_progress_view(raw: object) -> dict | None:
    """A journal's `progress` as OPS carries it, or None: absent, or not whole. `done` is the share of the fit behind
    it, by micro-batches, and None while an epoch's size is unknown."""
    if not isinstance(raw, dict) or not all(is_count(raw.get(key)) for key in ("epoch", "epochs", "step")):
        return None
    epoch, epochs, step, steps = raw["epoch"], raw["epochs"], raw["step"], raw.get("steps")
    if not 1 <= epoch <= epochs:
        return None
    steps = steps if is_count(steps) and steps > 0 else None
    done = None if steps is None else min(1.0, ((epoch - 1) * steps + min(step, steps)) / (epochs * steps))
    loss = raw.get("loss")
    return {
        "epoch": epoch,
        "epochs": epochs,
        "step": step,
        "steps": steps,
        "done": None if done is None else round(done, 4),
        "loss": float(loss) if is_number(loss) else None,
    }


def _why(value: object) -> str | None:
    return files.clip(value, RETRAIN_WHY_MAX) if isinstance(value, str) and value else None


def retrain_view(journal: Mapping[str, Any]) -> dict:
    """`layaOpsStateSchema.retrain[]` minus its surface: the cycle's state, each step's state in order, the step it
    stands at (the first not done or skipped; None once closed), a failed step's error type, and the cycle's few
    numbers. Names, states and numbers only.

    Each step also carries when it started and how long it took, the cycle why it opened and when it closed, the train
    step its progress while a fit runs and its before/after once it ended, and the gate its one-line reason: what the
    TRAINING page draws its progress bar and its timings from."""
    steps_in = journal["steps"]

    def block(name: str) -> Mapping[str, Any]:
        return steps_in[name] if isinstance(steps_in.get(name), dict) else {}

    states = {
        name: block(name).get("state") if block(name).get("state") in RETRAIN_STEP_STATES else "pending" for name in RETRAIN_STEPS
    }
    at = next((name for name in RETRAIN_STEPS if states[name] not in ("done", "skipped")), None)
    error = block(at).get("error") if at is not None and states[at] == "failed" else None
    label, build, train, gate, sweep = (block(name) for name in ("label", "build", "train", "gate", "sweep"))
    checkpoint = journal.get("checkpoint")
    teacher = label.get("teacher")
    return {
        "id": journal["id"],
        "state": journal["state"],
        "started_at": journal["started_at"] if is_iso(journal.get("started_at")) else None,
        "updated_at": journal["updated_at"] if is_iso(journal.get("updated_at")) else None,
        "step": at,
        "steps": [
            {
                "name": name,
                "state": states[name],
                "started_at": block(name)["started_at"] if is_iso(block(name).get("started_at")) else None,
                "seconds": float(block(name)["seconds"])
                if is_number(block(name).get("seconds")) and block(name)["seconds"] >= 0
                else None,
            }
            for name in RETRAIN_STEPS
        ],
        "closed_at": journal["closed_at"] if is_iso(journal.get("closed_at")) else None,
        "reason": _why((journal.get("owed") or {}).get("reason") if isinstance(journal.get("owed"), dict) else None),
        "progress": train_progress_view(train.get("progress")) if states["train"] == "running" else None,
        "eval": eval_view({"eval_items": train.get("eval_items"), "before": train.get("before"), "after": train.get("after")}),
        "gate_why": _why(gate.get("why")),
        "error": error if isinstance(error, str) and ERROR_NAME.fullmatch(error) else None,
        "checkpoint": checkpoint if is_basename(checkpoint) and MODEL_NAME.fullmatch(checkpoint) else None,
        "teacher": teacher if isinstance(teacher, str) and TEACHER_NAME.fullmatch(teacher) else None,
        "label_calls": label["calls"] if is_count(label.get("calls")) else None,
        "rows": build["rows"] if is_count(build.get("rows")) else None,
        "operator_rows": build["operator"] if is_count(build.get("operator")) else None,
        "reserved": build["reserved"] if is_count(build.get("reserved")) else None,
        "train_rows": train["train_rows"] if is_count(train.get("train_rows")) else None,
        "gate": gate["gate"] if gate.get("gate") in GATE_TAGS else None,
        "sweep_best": _p(sweep.get("best")),
        "sweep_rows": sweep["rows"] if is_count(sweep.get("rows")) else None,
    }


def _retrain_verdict(report: dict) -> dict:
    """A closed cycle's verdict: PASS (a @challenger was registered), REFUSE (the gate refused the candidate), FAIL
    (abandoned), NO-OP (nothing was owed). An open journal never reaches here: laya-lab writes a report only when a
    cycle closes."""
    steps = report.get("steps") if isinstance(report.get("steps"), dict) else {}
    train = steps.get("train") if isinstance(steps.get("train"), dict) else {}
    after = train.get("after") if isinstance(train.get("after"), dict) else {}
    device = report.get("device")
    states = [step.get("state") for step in steps.values() if isinstance(step, dict)]
    return {
        "verdict": RETRAIN_VERDICTS.get(report.get("state"), "INCONCLUSIVE"),
        "headline": _headline(
            cases=train.get("held_out_rows"),
            accuracy=after.get("accuracy"),
            brier=after.get("brier"),
            ece=after.get("ece"),
            trials=sum(1 for state in states if state == "done") if states else None,
            skipped=states.count("skipped") if states else None,
        ),
        "model_digest": _digest(report.get("model_digest")) or _digest(report.get("checkpoint")),
        "dataset_version": _digest(report.get("dataset_version")),
        "accelerator": device if device in ACCELERATORS else None,
    }


# ── the scores file ───────────────────────────────────────────────────────────

#: The surrogate's two fields on a Sonnet key's scores row: the teacher label it was scored against, and the out-of-fold
#: probability it gives that label (laya-lab `score.teacher_confidence` writes them; `review_data.suspect_ranked` reads).
TEACHER_FIELDS = ("teacher_open", "teacher_conf")


def scores_row(
    key: str,
    p: float,
    model: str,
    checkpoint: str | None,
    question_version: int,
    threshold: float,
    at: str,
    *,
    device: str | None = None,
    digest: str | None = None,
    teacher: tuple[bool, float] | None = None,
) -> dict:
    """One line of `data/<stem>-laya-p.jsonl`. `checkpoint` is a basename, null for a built-in. `device` and `digest`
    (`checkpoint_digest`) make the row's key (digest, device, key): an incremental SCORE reuses a p only under it.
    `teacher` (label, self-confidence) rides only on a key the surrogate scored: both fields or neither."""
    row = {
        "key": key,
        "p": p,
        "model": model,
        "checkpoint": checkpoint,
        "question_version": question_version,
        "threshold": threshold,
        "at": at,
        "device": device,
        "digest": digest,
    }
    if teacher is not None:
        row["teacher_open"], row["teacher_conf"] = bool(teacher[0]), float(teacher[1])
    return row


def _score_ok(row: dict) -> bool:
    return (
        isinstance(row.get("key"), str)
        and KEY.fullmatch(row["key"]) is not None
        and is_p(row.get("p"))
        and isinstance(row.get("model"), str)
        and MODEL_NAME.fullmatch(row["model"]) is not None
        and (row.get("checkpoint") is None or is_basename(row.get("checkpoint")))
        and is_count(row.get("question_version"))
        and is_p(row.get("threshold"))
        and is_iso(row.get("at"))
    )


def _teacher_ok(row: dict) -> bool:
    return isinstance(row.get("teacher_open"), bool) and is_p(row.get("teacher_conf"))


def _parse_scores(data: bytes) -> list[dict]:
    """Valid rows; a malformed or half teacher pair is dropped from its row, never the row's own p."""
    rows, _bad = files.parse_jsonl(data)
    kept = []
    for row in rows:
        if not _score_ok(row):
            continue
        if any(name in row for name in TEACHER_FIELDS) and not _teacher_ok(row):
            row = {name: value for name, value in row.items() if name not in TEACHER_FIELDS}
        kept.append({**row, "model": served_name(row["model"])})
    return kept


def read_scores(base: Path, stem: str) -> tuple[list[dict], str]:
    """(valid rows in file order, source code: ok | missing | too_large | refused)."""
    code, rows = _SCORES.get(data_file(base, stem, "laya-p"), SCORES_CAP_BYTES, _parse_scores)
    return (rows if code == files.OK else []), code


# ── golden suites ─────────────────────────────────────────────────────────────


def _case_ok(case: dict) -> bool:
    # Any id is kept, so `review_data.golden_keys` covers every case; the deck checks CASE_ID itself.
    state = case.get("state")
    return (
        isinstance(case.get("id"), str)
        and 0 < len(case["id"]) <= 64
        and isinstance(case.get("expected"), bool)
        and isinstance(state, dict)
        and isinstance(state.get("hand_off"), str)
        and bool(state["hand_off"].strip())
    )


def _parse_suite(data: bytes) -> list[dict]:
    rows, _bad = files.parse_jsonl(data)
    return [row for row in rows if _case_ok(row)]


def read_golden_suite(path: Path) -> tuple[list[dict], str]:
    """(cases, source code). Public or private; `#` lines are comments; a malformed case is dropped."""
    code, cases = _SUITES.get(path, GOLDEN_CAP_BYTES, _parse_suite)
    return (cases if code == files.OK else []), code


#: A public golden case an agent wrote that the operator has not agreed yet. The yardstick is operator-agreed text,
#: so such a case is scored, but it counts toward no golden headline and no promotion-gate check until the operator
#: signs it off (`signed_off_ids`): one by one, its live answer to `golden:<id>` being agree, or with its BATCH.
#: A flip keeps it out either way (`operator disputes:`; the public file is fixed by hand).
PROVISIONAL_TAG = "provisional"

#: THE BATCH SIGN-OFF. Agent-written cases written together carry one batch tag (`batch-2`) and are signed off on a
#: SAMPLE instead of one by one: zero-defect acceptance sampling. `BATCH_SAMPLE` of them, drawn by `batch_sample` (a
#: published rule, so nobody picks the easy ones), are dealt into the REVIEW deck's golden stratum. Every sampled case
#: agreed accepts the batch, and its unsampled cases count without a card each. One sampled case flipped or skipped
#: (a case a careful reader cannot call is as much a defect as a wrong one) REJECTS it: the deck then deals the whole
#: batch, and each case counts on its own agree. Redrawing after a rejection would be choosing the sample, so the
#: rule never does. `batch_bound` is what an accepted batch promises, and the card prints it. Membership is the tag,
#: provisional or not, so the sample a reader recomputes from the settled file is the one the operator graded.
BATCH_TAG = re.compile(r"batch-[1-9][0-9]{0,2}")
BATCH_SAMPLE = 40
#: The strata a sample is spread over, in proportion (largest remainder): open against done, and within each the
#: writer's category, so the hardest category (done with a caveat) is never left to chance. Any other case is `other`.
BATCH_STRATA = ("failing-tests", "waiting", "caveat", "clean")
#: The confidence `batch_bound` is stated at.
BATCH_CONFIDENCE = 0.95


def is_provisional(case: Mapping[str, Any]) -> bool:
    tags = case.get("tags")
    return isinstance(tags, list) and PROVISIONAL_TAG in tags


def case_batch(case: Mapping[str, Any]) -> str | None:
    tags = case.get("tags")
    return (
        next((tag for tag in tags if isinstance(tag, str) and BATCH_TAG.fullmatch(tag)), None) if isinstance(tags, list) else None
    )


def _stratum(case: Mapping[str, Any]) -> tuple[bool, str]:
    tags = case.get("tags") if isinstance(case.get("tags"), list) else []
    return bool(case["expected"]), next((tag for tag in tags if tag in BATCH_STRATA), "other")


def batch_sample(batch: str, members: list[Mapping[str, Any]], size: int | None = None) -> tuple[str, ...]:
    """The sampled ids of one batch, sorted. Pure and deterministic: each stratum gets its proportional share (largest
    remainder, ties to the larger stratum, then its name), filled by the lowest `sha256(batch:id)`."""
    groups: dict[tuple[bool, str], list[str]] = {}
    for case in members:
        groups.setdefault(_stratum(case), []).append(case["id"])
    total = sum(len(ids) for ids in groups.values())
    take = min(BATCH_SAMPLE if size is None else size, total)
    if take == 0:
        return ()
    quota = {key: take * len(ids) // total for key, ids in groups.items()}
    rest = sorted(groups, key=lambda key: (-(take * len(groups[key]) % total), -len(groups[key]), key))
    for key in rest[: take - sum(quota.values())]:
        quota[key] += 1

    def order(case_id: str) -> str:
        return hashlib.sha256(f"{batch}:{case_id}".encode()).hexdigest()

    return tuple(sorted(case_id for key, ids in groups.items() for case_id in sorted(ids, key=order)[: quota[key]]))


def batch_bound(size: int, sampled: int, confidence: float | None = None) -> int:
    """The most wrong labels an accepted batch can still hold at `confidence`: the largest D for which a sample of
    `sampled` from `size` with D wrong in it still draws none with probability above 1 - confidence (hypergeometric)."""
    confidence = BATCH_CONFIDENCE if confidence is None else confidence
    if sampled >= size:
        return 0
    worst = 0
    for wrong in range(1, size - sampled + 1):
        if comb(size - wrong, sampled) / comb(size, sampled) <= 1 - confidence:
            break
        worst = wrong
    return worst


def batch_standing(
    cases: list[dict], labels: Mapping[str, Mapping[str, Any]], skipped: frozenset[str] | set[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """Per batch: `{size, sample, agreed, defects, state, bound}`. `state` is `accepted`, `rejected` or `pending`;
    `defects` are the sampled ids flipped or skipped; `bound` is `batch_bound` once accepted, else None. A sampled case
    that no longer carries PROVISIONAL_TAG counts as agreed: `laya-lab settle` drops the tag only from signed-off
    cases, so the file itself carries the sign-off and an EMPTY store (the card's reproduce run) reads it accepted."""
    members: dict[str, list[dict]] = {}
    for case in cases:
        batch = case_batch(case)
        if batch is not None:
            members.setdefault(batch, []).append(case)
    out: dict[str, dict[str, Any]] = {}
    for batch, group in sorted(members.items()):
        sample = batch_sample(batch, group)
        answers = {case_id: (labels.get(f"golden:{case_id}") or {}).get("answer") for case_id in sample}
        settled = {case["id"] for case in group if not is_provisional(case)}
        agreed = [
            case_id for case_id in sample if answers[case_id] == "agree" or (case_id in settled and answers[case_id] is None)
        ]
        defects = [case_id for case_id in sample if answers[case_id] == "flip" or f"golden:{case_id}" in skipped]
        state = "rejected" if defects else "accepted" if len(agreed) == len(sample) else "pending"
        out[batch] = {
            "size": len(group),
            "sample": list(sample),
            "agreed": len(agreed),
            "defects": defects,
            "state": state,
            "bound": batch_bound(len(group), len(sample)) if state == "accepted" else None,
        }
    return out


def agreed_case_ids(labels: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Public case ids whose live operator answer (a `OperatorFold.labels`) is agree."""
    return frozenset(
        key.removeprefix("golden:") for key, row in labels.items() if key.startswith("golden:") and row.get("answer") == "agree"
    )


def signed_off_ids(
    cases: list[dict], labels: Mapping[str, Mapping[str, Any]], skipped: frozenset[str] | set[str] = frozenset()
) -> frozenset[str]:
    """The provisional ids that count (PROVISIONAL_TAG's rule): each one agreed, and every case of an accepted batch
    that carries no live flip of its own."""
    agreed = agreed_case_ids(labels)
    accepted = {batch for batch, standing in batch_standing(cases, labels, skipped).items() if standing["state"] == "accepted"}
    flipped = {key.removeprefix("golden:") for key, row in labels.items() if row.get("answer") == "flip"}
    return agreed | frozenset(case["id"] for case in cases if case_batch(case) in accepted and case["id"] not in flipped)


def dealt_golden_ids(
    cases: list[dict], labels: Mapping[str, Mapping[str, Any]], skipped: frozenset[str] | set[str] = frozenset()
) -> frozenset[str]:
    """The provisional ids the REVIEW deck deals for sign-off: a case outside any batch, a sampled case, and every
    case of a rejected batch (BATCH_TAG's rule). A pending or accepted batch's unsampled cases are never dealt."""
    standing = batch_standing(cases, labels, skipped)
    dealt = set()
    for case in cases:
        if not is_provisional(case):
            continue
        batch = case_batch(case)
        if batch is None or case["id"] in standing[batch]["sample"] or standing[batch]["state"] == "rejected":
            dealt.add(case["id"])
    return frozenset(dealt)


def counted_cases(cases: list[dict], agreed: frozenset[str]) -> tuple[list[dict], list[str]]:
    """`(the cases that count, the provisional ids held back)` under PROVISIONAL_TAG's rule; `agreed` is
    `signed_off_ids`."""
    counted = [case for case in cases if not is_provisional(case) or case["id"] in agreed]
    held = [case["id"] for case in cases if is_provisional(case) and case["id"] not in agreed]
    return counted, held


def private_golden_case(key: str, text: str, expected: bool, model: bool, model_by: str, promoted_at: str) -> dict:
    """An operator flip promoted into the private suite: the public row shape plus `key`, `promoted_at`, `labels`."""
    return {
        "id": f"f-{key}",
        "key": key,
        "expected": expected,
        "tags": ["operator", "open" if expected else "done"],
        "why": f"operator label; {model_by} said {'open' if model else 'done'}",
        "state": {"hand_off": text},
        "promoted_at": promoted_at,
        "labels": {"operator": expected, "model": model, "model_by": model_by},
    }


# ── models.json entries ───────────────────────────────────────────────────────


def threshold_entry(
    at: float,
    model: str,
    checkpoint: str,
    swept_at: str,
    labels: str,
    rows: int,
    kappa: float | None,
    report: str | None,
    done_at: float | None = None,
) -> dict:
    """`models.json` `thresholds.<surface>`: a swept cut, scoped to the checkpoint it was swept on. `done_at` is the
    agent rack's ✅ cut from the same sweep (tldrack/open_work.py § Three bands), absent when none was picked."""
    entry = {
        "at": at,
        "model": model,
        "checkpoint": checkpoint,
        "swept_at": swept_at,
        "labels": labels,
        "rows": rows,
        "kappa": kappa,
        "report": report,
    }
    if done_at is not None:
        entry["done_at"] = done_at
    return entry


def threshold_view(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not is_p(raw.get("at")) or not is_basename(raw.get("checkpoint")):
        return None
    if not isinstance(raw.get("model"), str) or MODEL_NAME.fullmatch(raw["model"]) is None:
        return None
    labels = raw.get("labels")
    return {
        "at": float(raw["at"]),
        "model": served_name(raw["model"]),
        "checkpoint": raw["checkpoint"],
        "swept_at": raw["swept_at"] if is_iso(raw.get("swept_at")) else None,
        "labels": labels if labels in HELDOUT_LABELS else None,
        "rows": raw["rows"] if is_count(raw.get("rows")) else 0,
        "kappa": kappa_or_none(raw.get("kappa")),
        "report": raw["report"] if is_basename(raw.get("report")) else None,
        # Only a ✅ cut strictly under the ⚠️ one: the bands must not overlap.
        "done_at": float(raw["done_at"]) if is_p(raw.get("done_at")) and 0 < raw["done_at"] < raw["at"] else None,
    }


#: How a version came to be registered: the promotion gate passed it, `--force` overrode a refusal, it came over from a
#: v1 registry, or its surface has no golden suite and laya's own held-out rule passed it (laya-lab `train.py`).
GATE_TAGS = ("PASS", "FORCED", "MIGRATED", "HELDOUT")
GATE_CHECK_STATES = ("pass", "fail", "skipped", "unmeasured", "pending")
CHECK_NAME = re.compile(r"[a-z][a-z_]{0,31}")


def gate_tags(gate: str, run_id: str | None = None, report: str | None = None, checks: Mapping[str, str] | None = None) -> dict:
    """`models.json` `versions.<version>.tags`: the verdict that registered the version (MLflow's per-version tags)."""
    return {"gate": gate, "gate_run": run_id, "gate_report": report, "checks": dict(checks or {})}


def tags_view(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("gate") not in GATE_TAGS:
        return None
    checks = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    return {
        "gate": raw["gate"],
        "gate_run": raw["gate_run"] if isinstance(raw.get("gate_run"), str) and EVAL_RUN.fullmatch(raw["gate_run"]) else None,
        "gate_report": raw["gate_report"] if is_basename(raw.get("gate_report")) else None,
        "checks": {
            name: state
            for name, state in checks.items()
            if isinstance(name, str) and CHECK_NAME.fullmatch(name) and state in GATE_CHECK_STATES
        },
    }


def trained_entry(epochs: int, train_rows: int, held_out_rows: int, base_revision: str | None, at: str) -> dict:
    """`models.json` `versions.<version>.trained`: how the registered checkpoint was trained."""
    return {"epochs": epochs, "train_rows": train_rows, "held_out_rows": held_out_rows, "base_revision": base_revision, "at": at}


def trained_view(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not all(is_count(raw.get(k)) for k in ("epochs", "train_rows", "held_out_rows")):
        return None
    revision = raw.get("base_revision")
    return {
        "epochs": raw["epochs"],
        "train_rows": raw["train_rows"],
        "held_out_rows": raw["held_out_rows"],
        "base_revision": revision if isinstance(revision, str) and len(revision) <= 64 else None,
        "at": raw["at"] if is_iso(raw.get("at")) else None,
    }


METRICS = ("accuracy", "brier", "ece", "loss")


def eval_view(raw: Any) -> dict | None:
    """`models.json` `versions.<version>.eval` (laya-lab `train.py`: before/after on held-out) → `{items, before, after}`."""
    if not isinstance(raw, dict) or not is_count(raw.get("eval_items")):
        return None
    sides = {}
    for side in ("before", "after"):
        metrics = raw.get(side)
        if not isinstance(metrics, dict) or not all(is_number(metrics.get(m)) for m in METRICS):
            return None
        sides[side] = {m: float(metrics[m]) for m in METRICS}
    return {"items": raw["eval_items"], **sides}


# ── the shadow file ───────────────────────────────────────────────────────────

SHADOW_VERSION = 1
SHADOW_CAP_BYTES = 16 * 1024 * 1024


def shadow_row(
    *,
    decision_id: str,
    key: str,
    at: str,
    question_version: int,
    served_model: str,
    served_checkpoint: str | None,
    served_p: float,
    served_threshold: float,
    served_done_threshold: float | None,
    challenger: str,
    challenger_digest: str | None,
    device: str,
    challenger_p: float,
    challenger_threshold: float,
    challenger_threshold_source: str,
    challenger_done_threshold: float,
    scored_at: str,
) -> dict:
    """One line of `shadow/<surface>.jsonl` (laya-lab `shadow.py`): a challenger's p on one live decision, beside the p
    and the cuts that decision was SERVED at. Keyed by (decision_id, challenger digest, device): CPU p is not MPS p.
    Keys, names and numbers only; the text stays in the ledger."""
    return {
        "v": SHADOW_VERSION,
        "decision_id": decision_id,
        "key": key,
        "at": at,
        "question_version": question_version,
        "served_model": served_model,
        "served_checkpoint": served_checkpoint,
        "served_p": served_p,
        "served_threshold": served_threshold,
        "served_done_threshold": served_done_threshold,
        "challenger": challenger,
        "challenger_digest": challenger_digest,
        "device": device,
        "challenger_p": challenger_p,
        "challenger_threshold": challenger_threshold,
        "challenger_threshold_source": challenger_threshold_source,
        "challenger_done_threshold": challenger_done_threshold,
        "scored_at": scored_at,
    }


def shadow_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """What makes a shadow row unique: the decision, and the challenger as (name, digest, device) that scored it."""
    return row["decision_id"], row["challenger"], row.get("challenger_digest") or "", row["device"]


def _shadow_ok(row: dict) -> bool:
    return (
        row.get("v") == SHADOW_VERSION
        and isinstance(row.get("decision_id"), str)
        and DECISION_ID.fullmatch(row["decision_id"]) is not None
        and isinstance(row.get("key"), str)
        and KEY.fullmatch(row["key"]) is not None
        and is_iso(row.get("at"))
        and is_count(row.get("question_version"))
        and isinstance(row.get("served_model"), str)
        and MODEL_NAME.fullmatch(row["served_model"]) is not None
        and (row.get("served_checkpoint") is None or is_basename(row.get("served_checkpoint")))
        and is_p(row.get("served_p"))
        and is_p(row.get("served_threshold"))
        and (row.get("served_done_threshold") is None or is_p(row.get("served_done_threshold")))
        and isinstance(row.get("challenger"), str)
        and MODEL_NAME.fullmatch(row["challenger"]) is not None
        and (
            row.get("challenger_digest") is None
            or (isinstance(row.get("challenger_digest"), str) and EVAL_DIGEST.fullmatch(row["challenger_digest"]) is not None)
        )
        and row.get("device") in ACCELERATORS
        and is_p(row.get("challenger_p"))
        and is_p(row.get("challenger_threshold"))
        and row.get("challenger_threshold_source") in ("swept", "code")
        and is_p(row.get("challenger_done_threshold"))
        and is_iso(row.get("scored_at"))
    )


def _parse_shadow(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    return [row for row in parsed if _shadow_ok(row)]


def read_shadow(base: Path, surface: str) -> tuple[list[dict], str]:
    """(valid rows in file order, source code: ok | missing | too_large | refused). P2-11's `challenger` stratum and
    OPS's shadow line read it."""
    code, found = _SHADOW.get(shadow_path(base, surface), SHADOW_CAP_BYTES, _parse_shadow)
    return (found if code == files.OK else []), code


# ── the stability meter ───────────────────────────────────────────────────────

STABILITY_VERSION = 1
STABILITY_CAP_BYTES = 4 * 1024 * 1024
STOP_SET_CAP_BYTES = 4 * 1024 * 1024
#: The stop set's size: live states no one labelled, asked of every checkpoint.
STOP_SET_SIZE = 200
#: KappaAverage (small-text's stopping criterion, with its defaults): the mean κ between consecutive checkpoints'
#: verdicts over the last KAPPA_WINDOW pairs; at or past STABLE_KAPPA more grading has stopped changing the verdicts.
KAPPA_WINDOW = 3
STABLE_KAPPA = 0.99


def stop_set_row(*, key: str, at: str, state: str, added_at: str) -> dict:
    """One line of `stability/<surface>-stop-set.jsonl`: a live state as the ledger kept it (`at` its decision's
    time), under its `rows.state_key`. Operator text: the file stays under `laya/`, 0600, and never crosses HTTP."""
    return {"v": STABILITY_VERSION, "key": key, "at": at, "state": state, "added_at": added_at}


def _stop_ok(row: dict) -> bool:
    state = row.get("state")
    return (
        row.get("v") == STABILITY_VERSION
        and isinstance(state, str)
        and bool(state.strip())
        and utf16_len(state) <= STATE_MAX
        and row.get("key") == rows.state_key(state)
        and is_iso(row.get("at"))
        and is_iso(row.get("added_at"))
    )


def _parse_stop_set(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    kept: dict[str, dict] = {}
    for row in parsed:
        if _stop_ok(row):
            kept.setdefault(row["key"], row)
    return list(kept.values())


def read_stop_set(base: Path, surface: str) -> tuple[list[dict], str]:
    """(the stop set's rows in file order, one per key; source code: ok | missing | too_large | refused)."""
    code, found = _STOP_SETS.get(stop_set_path(base, surface), STOP_SET_CAP_BYTES, _parse_stop_set)
    return (found if code == files.OK else []), code


def stability_row(*, key: str, version: str, digest: str | None, device: str, p: float, cut: float, scored_at: str) -> dict:
    """One line of `stability/<surface>.jsonl`: checkpoint `version`'s p on one stop-set state, and the ⚠️ cut that
    checkpoint drew at when it was scored (what a deleted checkpoint is still read at). `version` is the checkpoint's
    folder name, never the name it is served under, so a promotion moves no row. Keys, names and numbers only."""
    return {
        "v": STABILITY_VERSION,
        "key": key,
        "version": version,
        "digest": digest,
        "device": device,
        "p": p,
        "cut": cut,
        "scored_at": scored_at,
    }


def stability_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """What makes a stability row unique: the state, and the checkpoint as (folder, digest, device) that scored it."""
    return row["key"], row["version"], row.get("digest") or "", row["device"]


def _stability_ok(row: dict) -> bool:
    return (
        row.get("v") == STABILITY_VERSION
        and isinstance(row.get("key"), str)
        and KEY.fullmatch(row["key"]) is not None
        and is_basename(row.get("version"))
        and MODEL_NAME.fullmatch(row["version"]) is not None
        and (
            row.get("digest") is None or (isinstance(row.get("digest"), str) and EVAL_DIGEST.fullmatch(row["digest"]) is not None)
        )
        and row.get("device") in ACCELERATORS
        and is_p(row.get("p"))
        and is_p(row.get("cut"))
        and is_iso(row.get("scored_at"))
    )


def _parse_stability(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    return [row for row in parsed if _stability_ok(row)]


def read_stability(base: Path, surface: str) -> tuple[list[dict], str]:
    """(valid rows in file order, source code: ok | missing | too_large | refused)."""
    code, found = _STABILITY.get(stability_path(base, surface), STABILITY_CAP_BYTES, _parse_stability)
    return (found if code == files.OK else []), code


def stability_cuts(versions: Mapping[str, Any], surface: str) -> dict[str, float]:
    """`{checkpoint: its own swept ⚠️ cut}` for the registered versions of `surface` that have one (`versions` is
    `paths.registry_versions(base)`). A version without one reads at the cut its rows recorded."""
    return {
        name: version["cut"]["at"]
        for name, version in versions["versions"].items()
        if version["surface"] == surface and version["cut"] is not None and 0 < version["cut"]["at"] < 1
    }


def stability_summary(rows_in: list[dict], cuts: Mapping[str, float] | None = None) -> dict:
    """THE STABLE κ, from `read_stability` rows: for each pair of consecutive checkpoints (folder names end in their
    training stamp, so they sort oldest first), κ between their ⚠️ verdicts on the stop-set states both scored, each
    at its own cut (`cuts`: the live swept one; else the cut its newest row recorded), and KappaAverage over the last
    KAPPA_WINDOW pairs.

    `{device, states, versions, pairs: [{from, to, n, kappa, agree, flips}], kappa_average, window, stable, last}`.
    One device only, the newest row's (CPU p is not MPS p); a checkpoint re-trained in place counts by its newest
    digest. A pair's κ is None under `agreement.KAPPA_MIN_PAIRS` shared states. Two checkpoints that draw the SAME
    constant verdict on every state agree totally, where κ is undefined (chance agreement is total too): that pair
    reads 1.0, since nothing moved. `stable` needs a full window at or past STABLE_KAPPA.
    """
    empty = {
        "device": None,
        "states": 0,
        "versions": [],
        "pairs": [],
        "kappa_average": None,
        "window": 0,
        "stable": False,
        "last": None,
    }
    if not rows_in:
        return empty
    last = max(rows_in, key=lambda row: row["scored_at"])
    device = last["device"]
    newest: dict[str, dict] = {}
    for row in rows_in:
        held = newest.get(row["version"])
        if row["device"] == device and (held is None or row["scored_at"] > held["scored_at"]):
            newest[row["version"]] = row
    scores: dict[str, dict[str, float]] = {version: {} for version in newest}
    for row in rows_in:
        head = newest.get(row["version"])
        if head is not None and row["device"] == device and row.get("digest") == head.get("digest"):
            scores[row["version"]][row["key"]] = row["p"]
    order = sorted(scores)
    live = cuts or {}
    cut_of = {version: live.get(version, newest[version]["cut"]) for version in order}
    pairs = []
    for older, newer in zip(order, order[1:]):
        shared = sorted(scores[older].keys() & scores[newer].keys())
        said = [(scores[older][key] >= cut_of[older], scores[newer][key] >= cut_of[newer]) for key in shared]
        flips = sum(1 for a, b in said if a != b)
        kappa = None
        if len(said) >= agreement.KAPPA_MIN_PAIRS:
            kappa = agreement.cohens_kappa(said)
            if kappa is None and flips == 0:
                kappa = 1.0
        pairs.append(
            {"from": older, "to": newer, "n": len(said), "kappa": kappa, "agree": agreement.agree_rate(said), "flips": flips}
        )
    window = [pair["kappa"] for pair in pairs[-KAPPA_WINDOW:] if pair["kappa"] is not None]
    average = round(sum(window) / len(window), 3) if window else None
    return {
        "device": device,
        "states": len(scores[order[-1]]),
        "versions": order,
        "pairs": pairs,
        "kappa_average": average,
        "window": len(window),
        "stable": len(window) == KAPPA_WINDOW and average is not None and average >= STABLE_KAPPA,
        "last": last["scored_at"],
    }


# ── dataset versions ──────────────────────────────────────────────────────────

DATASET_CAP_BYTES = 4 * 1024 * 1024
#: Which command first saw a golden-suite content (every report-writing command stamps the version it scored).
DATASET_BY = ("nightly", "promote", "golden", "compare", "sweep", "gate")
DIGEST_HEX = re.compile(r"[0-9a-f]{12}")


def compact_at(at: str) -> str:
    """`2026-11-09T03:12:00Z` → `20261109T031200Z`: an ISO stamp inside a version id (`EVAL_DIGEST`'s alphabet)."""
    return re.sub(r"[-:]", "", at)


def dataset_version_id(question_version: int, golden_at: str | None, operator_at: str | None) -> str:
    """THE dataset version a report is stamped with, Langfuse's rule: a dataset is named by WHEN it took this content.
    `q<question version>.g<golden suites' version>.f<operator label set's version>`, each a UTC stamp (`-` when that
    part is empty), at most 80 characters of `EVAL_DIGEST`."""
    golden = compact_at(golden_at) if golden_at else "-"
    operator = compact_at(operator_at) if operator_at else "-"
    return f"q{question_version}.g{golden}.f{operator}"


def dataset_row(*, suite: str, at: str, digest: str, public_cases: int, private_cases: int, by: str) -> dict:
    """One line of `golden/versions.jsonl`: a content of the golden suites (public + private, `digest`) first seen at
    `at`. Digests and counts only: no case text."""
    return {
        "v": 1,
        "suite": suite,
        "at": at,
        "digest": digest,
        "public_cases": public_cases,
        "private_cases": private_cases,
        "by": by,
    }


def _dataset_ok(row: dict) -> bool:
    return (
        row.get("v") == 1
        and isinstance(row.get("suite"), str)
        and CASE_ID.fullmatch(row["suite"]) is not None
        and is_iso(row.get("at"))
        and isinstance(row.get("digest"), str)
        and DIGEST_HEX.fullmatch(row["digest"]) is not None
        and is_count(row.get("public_cases"))
        and is_count(row.get("private_cases"))
        and row.get("by") in DATASET_BY
    )


def _parse_datasets(data: bytes) -> list[dict]:
    parsed, _bad = files.parse_jsonl(data)
    return [row for row in parsed if _dataset_ok(row)]


def read_dataset_versions(base: Path, suite: str) -> list[dict]:
    """Every recorded golden-suite version of `suite`, oldest first."""
    code, found = _DATASETS.get(dataset_versions_path(base), DATASET_CAP_BYTES, _parse_datasets)
    return [row for row in found if row["suite"] == suite] if code == files.OK else []


# ── checkpoint retention ──────────────────────────────────────────────────────

#: Rejected checkpoints kept on disk besides champion, challenger and previous.
REJECTS_KEPT = 2


def folder_bytes(folder: Path) -> int:
    """A checkpoint folder's size with its `.data` record: regular files only, links never followed."""
    total = 0
    for path in (folder, folder.with_name(folder.name + ".data")):
        if not files.is_real_dir(path):
            continue
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    return total


def is_choice_head(base: Path, name: str) -> bool:
    """Choice heads keep ordinary training provenance but are never binary golden/gate candidates."""
    return is_basename(name) and (base / CHECKPOINTS_DIRNAME / (name + ".data") / "head.json").exists()


def choice_keep(base: Path) -> tuple[str | None, bool]:
    path = base / "needs-you" / "model.json"
    if not path.exists() and not path.is_symlink():
        return None, True
    try:
        raw = files.loads(files.read_capped(path, 64 * 1024) or b"{}")
        name = raw.get("checkpoint")
        if raw.get("version") == 1 and raw.get("shadow_only") is True and is_basename(name):
            return name, True
    except (files.SourceError, ValueError):
        pass
    return None, False


def retention(base: Path, surface: str, versions: Mapping[str, Any]) -> dict[str, Any]:
    """THE retention rule: `{"keep": [names], "prunable": [{name, path, bytes}]}` over every checkpoint folder of
    `surface` on disk, registered or not (a checkpoint the gate refused is a reject too). Kept: champion, challenger,
    previous and the REJECTS_KEPT newest of the rest; folder names end in their training stamp, so the newest sorts
    last. `versions` is `paths.registry_versions(base)`. Reads only: laya-lab's `registry.prune_rejects` deletes."""
    block = versions["aliases"].get(surface) or {}
    protected = [name for name in (block.get(alias) for alias in ALIASES) if name]
    folder = base / CHECKPOINTS_DIRNAME
    pattern = rows.checkpoint_name(fine_tune_name(surface))
    names: set[str] = set()
    if files.is_real_dir(folder):
        try:
            names = {child.name for child in folder.iterdir() if pattern.fullmatch(child.name) and files.is_real_dir(child)}
        except OSError:
            names = set()
    names |= {name for name, version in versions["versions"].items() if version["surface"] == surface}
    choice, readable = choice_keep(base)
    if choice:
        protected.append(choice)
    elif not readable:
        protected.extend(name for name in names if is_choice_head(base, name))
    rest = sorted((name for name in names if name not in protected), reverse=True)
    return {
        "keep": [*protected, *rest[:REJECTS_KEPT]],
        "prunable": [
            {"name": name, "path": str(folder / name), "bytes": folder_bytes(folder / name)} for name in rest[REJECTS_KEPT:]
        ],
    }
