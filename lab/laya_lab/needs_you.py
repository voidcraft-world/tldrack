"""The four-way challenger (done · read · running · needs you). Local, gated, and never registered as a served model.

The ordinary checkpoint basename keeps training provenance in the existing label-budget union;
only this module's manifest selects the choice head. The training record uses the unchanged
open-work state keys. RUNNING's free background signal is applied after the model, identically
on reserve and live rows, and reported separately. Nothing changes the live warning or abstains.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_lab import infer, open_work_data as source, retrain, shadow, train
from laya_lab.data import DEFAULT_EPOCHS, atomic_write_text, make_private_dirs, open_private, split_rows, write_jsonl
from laya_lab.golden import SUITES
from laya_bridge import agreement, files, landing, review_data, rows
from tldrack import open_work

SURFACE = "agent_open_work"
SUITE = "agent-open-work"
CLASSES = ("done", "read", "running", "you")
QUESTION = {
    "type": "choice",
    "question": "What does this agent hand-off require next?",
    "criteria": {
        "done": (
            "The requested work is complete and the developer can close it unread; routine review, restart, deploy "
            "or optional offers do not count."
        ),
        "read": (
            "The requested work is complete, but the hand-off carries an answer, finding or explanation the developer "
            "asked for and still has to read."
        ),
        "running": "Requested work remains and the agent can continue without an answer or action from the developer.",
        "you": "The agent cannot continue until the developer supplies an answer, decision, access or other action.",
    },
}
#: 2: READ joined the classes (schemas/src/laya-review.ts REVIEW_NEEDS owns why).
VERSION = 2
CAP = 8 * 1024 * 1024
#: The reason of every refusal while the lease's night window is closed (`lease.night_window_open`).
OUTSIDE_WINDOW = "outside the lease's night window"


def root(base: Path) -> Path:
    return base / "needs-you"


def read_doc(path: Path) -> dict:
    try:
        payload = json.loads(files.read_capped(path, CAP) or b"{}")
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("needs-you record is not an object")
    return payload


def manifest(base: Path) -> dict:
    path = root(base) / "model.json"
    if not path.exists() and not path.is_symlink():
        return {}
    doc = read_doc(path)
    if (
        doc.get("version") != VERSION
        or doc.get("question_version") != open_work.QUESTION_VERSION
        or doc.get("question") != QUESTION
        or not rows.checkpoint_name(source.SPEC.fine_tune).fullmatch(str(doc.get("checkpoint", "")))
    ):
        raise ValueError("needs-you manifest does not match the current question")
    return doc


def status(base: Path) -> dict:
    """Read-only readiness; only counts, fixed reasons and measured agreement escape this module."""
    source.require_sources(base)
    snap = review_data.snapshot(base, SURFACE)
    grades = review_data.three_way(snap.fold, snap.spec) or {}
    reliability = review_data.intra_rater(snap)["needs"].get("you", {})
    kappa = reliability.get("kappa")
    eligible = all(grades.get(c, 0) >= 20 for c in CLASSES)
    eligible = eligible and isinstance(kappa, (int, float)) and math.isfinite(kappa) and kappa >= 0.6
    return {
        "ready": bool(eligible),
        "counts": {c: grades.get(c, 0) for c in CLASSES},
        "kappa": kappa,
        "pairs": reliability.get("n", 0),
        "shadow_only": True,
        "reason": None if eligible else "needs 20 grades per class and needs-you retest kappa at least 0.6",
    }


def dataset(base: Path) -> tuple[list[dict], list[dict]]:
    """Operator-only, deduplicated judged states; golden held out and stable reserve never trains."""
    fold = source.require_sources(base)
    found = source.texts(base, SURFACE)
    budget, reserved = source.reserve_held(base, fold)
    _, public_code, _, private_code = review_data.golden_suites(base, SUITE)
    if any(code not in (files.OK, files.MISSING) for code in (public_code, private_code)):
        raise ValueError("golden exclusion sources are unreadable")
    golden = source.golden_held(base)
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for key, label in fold.labels.items():
        grade = review_data.grade_of(label)
        text = found.get(key)
        if grade not in CLASSES or not text or not source.SPEC.admits(text):
            continue
        judged = source.judged_key(text)
        if key in golden or judged in golden:
            continue
        groups.setdefault(judged, []).append((key, grade, text))
    fitted, held = [], []
    for judged, aliases in sorted(groups.items()):
        if len({grade for _, grade, _ in aliases}) != 1:
            continue  # inconsistent aliases are not independent evidence
        key, grade, text = sorted(aliases)[0]
        row = {"key": key, "state": open_work.decide_state(text), "questions": {"q": QUESTION}, "expected": {"q": grade}}
        target = held if judged in reserved or any(k in reserved for k, _, _ in aliases) else fitted
        target.append(row)
    return fitted, held


def fingerprint(fitted: list[dict]) -> str:
    return hashlib.sha256(json.dumps(fitted, sort_keys=True).encode()).hexdigest()


def night_open() -> bool:
    return infer.lease_module().night_window_open(datetime.now(timezone.utc).timestamp())


def fit_head(base: Path, fitted: list[dict], held: list[dict], device: str) -> dict:
    """Use the existing trainer/lease, but never call register_fit or modify any live alias."""
    if not night_open():
        raise ValueError(OUTSIDE_WINDOW)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    checkpoint = f"{source.SPEC.fine_tune}-{stamp}-3"
    out = base / "checkpoints" / checkpoint
    work = out.with_name(out.name + ".data")
    if out.exists() or work.exists():
        raise ValueError("needs-you checkpoint already exists")
    fitted_train, calibration = split_rows(fitted)
    if any(not any(r["expected"]["q"] == c for r in group) for group in (fitted_train, calibration) for c in CLASSES):
        raise ValueError("each class needs training and calibration examples outside the reserve")
    write_jsonl(work / "train.jsonl", fitted_train)
    write_jsonl(work / "eval.jsonl", calibration)
    make_private_dirs(out)
    atomic_write_text(work / "head.json", json.dumps({"kind": "needs-you-choice", "version": VERSION}) + "\n")
    plan = train.Fit(SURFACE, out, work, len(fitted_train), len(calibration), len(held), DEFAULT_EPOCHS, device)
    train.fit(base, plan)
    doc = {
        "version": VERSION,
        "question_version": open_work.QUESTION_VERSION,
        "question": QUESTION,
        "checkpoint": checkpoint,
        "fingerprint": fingerprint(fitted),
        "at": rows.utc_stamp(),
        "train_rows": len(fitted_train),
        "calibration_rows": len(calibration),
        "reserve_rows": len(held),
        "shadow_only": True,
    }
    atomic_write_text(root(base) / "model.json", json.dumps(doc, sort_keys=True) + "\n")
    return doc


def predict(router: Any, checkpoint: str, state: Any, landed: dict | None = None) -> tuple[str, str, float]:
    infer.yield_point()
    if not night_open():
        raise ValueError(OUTSIDE_WINDOW)
    answer = router.predict(state, {"q": QUESTION}, model=checkpoint).get("answers", {}).get("q", {})
    choice = answer.get("choice")
    distribution = answer.get("probabilities", {})
    confidence = distribution.get(choice) if isinstance(distribution, dict) else None
    confidence = answer.get("confidence") if confidence is None else confidence
    if (
        choice not in CLASSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("invalid needs-you choice or confidence")
    running = landed is not None and landed.get("held") in landing.HELD_REASONS
    return ("running" if running else choice), choice, float(confidence)


def measurements(pairs: list[tuple[str, str]]) -> dict:
    confusion = {truth: dict.fromkeys(CLASSES, 0) for truth in CLASSES}
    for said, truth in pairs:
        confusion[truth][said] += 1
    binary = [(said == "you", truth == "you") for said, truth in pairs]
    return {
        "rows": len(pairs),
        "confusion": confusion,
        "needs_you_precision": agreement.rate_summary(binary, "precision"),
        "needs_you_recall": agreement.rate_summary(binary, "recall"),
    }


def run(base: Path, *, device: str = "mps", allow_train: bool = False, router: Any = None) -> dict:
    bar = status(base)
    if not bar["ready"]:
        return {"state": "skipped", **bar}
    if not night_open():
        return {**bar, "state": "skipped", "reason": OUTSIDE_WINDOW}
    make_private_dirs(root(base))
    with open_private(root(base) / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "skipped", "reason": "needs-you already running"}
        fitted, held = dataset(base)
        doc = manifest(base)
        if allow_train and (
            not doc
            or (
                doc["fingerprint"] != fingerprint(fitted)
                and datetime.now(timezone.utc) - datetime.fromisoformat(doc["at"].replace("Z", "+00:00")) >= timedelta(days=7)
            )
        ):
            owed = retrain.owed(SUITE, base, datetime.now(timezone.utc), forced=True)
            if not owed.owed:
                return {**bar, "state": "skipped", "reason": owed.reason}
            if any(not any(r["expected"]["q"] == c for r in group) for group in (fitted, held) for c in CLASSES):
                return {**bar, "state": "skipped", "reason": "each class needs both train and reserve rows"}
            doc = fit_head(base, fitted, held, device)
        if not doc:
            return {**bar, "state": "skipped", "reason": "no shadow checkpoint; request --train"}
        checkpoint = doc["checkpoint"]
        folder = base / "checkpoints" / checkpoint
        if not files.is_real_dir(folder):
            raise ValueError("needs-you checkpoint missing")
        trained = rows.checkpoint_keys(base, checkpoint)
        if trained is None:
            raise ValueError("needs-you training provenance missing")
        held = [r for r in held if r["key"] not in trained and rows.state_key(r["state"]) not in trained]
        router = router if router is not None else infer.open_router(base, device, {checkpoint: folder})
        landed = landing.landings_by_decision(base)
        live, _ = shadow.live_states(
            base, SUITES[SUITE], max(doc["at"], rows.utc_stamp(datetime.now(timezone.utc) - timedelta(days=7)))
        )
        by_key = {item.key: landed.get(item.decision_id) for item in live}
        pairs = [
            (predict(router, checkpoint, r["state"], by_key.get(rows.state_key(r["state"])))[0], r["expected"]["q"]) for r in held
        ]
        path = root(base) / "shadow.jsonl"
        prior = []
        if path.exists() or path.is_symlink():
            prior, bad = files.read_jsonl_capped(path, CAP)
            if bad:
                raise ValueError("needs-you shadow journal has invalid rows")
        digest = infer.checkpoint_digest(checkpoint, folder)
        seen = {(r.get("decision_id"), r.get("checkpoint"), r.get("digest"), r.get("device")) for r in prior}
        added = []
        for item in live:
            key = (item.decision_id, checkpoint, digest, device)
            if key in seen:
                continue
            choice, raw, confidence = predict(router, checkpoint, item.state, landed.get(item.decision_id))
            added.append(
                {
                    "decision_id": item.decision_id,
                    "key": item.key,
                    "at": item.at,
                    "checkpoint": checkpoint,
                    "digest": digest,
                    "device": device,
                    "choice": choice,
                    "model_choice": raw,
                    "confidence": confidence,
                    "scored_at": rows.utc_stamp(),
                }
            )
        if added:
            combined = prior + added
            if len(json.dumps(combined).encode()) > CAP:
                raise ValueError("needs-you shadow journal full; preserving existing evidence")
            write_jsonl(path, combined)
        current = [
            r
            for r in prior + added
            if r.get("checkpoint") == checkpoint and r.get("digest") == digest and r.get("device") == device
        ]
        report = {
            **bar,
            "state": "ran",
            "shadow_only": True,
            "at": rows.utc_stamp(),
            "new": len(added),
            "shadow_rows": len(current),
            "shadow_days": len({r["scored_at"][:10] for r in current}),
            "reserve": measurements(pairs),
            "live_switch": False,
            "next": "live abstention requires fresh warning_new/warning_held noise measurement and a separate decision",
        }
        atomic_write_text(root(base) / "report.json", json.dumps(report, sort_keys=True) + "\n")
        return report


def nightly(name: str, base: Path, device: str, router: Any = None) -> dict:
    if name != SUITE:
        return {"state": "skipped", "reason": "unsupported suite"}
    try:
        return run(base, device=device, allow_train=True, router=router)
    except (ValueError, OSError, files.SourceError) as error:
        return {"state": "failed", "reason": type(error).__name__, "shadow_only": True}


def cmd(args: Any, base: Path) -> int:
    summary = run(base, device=args.device, allow_train=args.train)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["state"] == "ran" else 3
