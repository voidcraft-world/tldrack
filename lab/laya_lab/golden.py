"""Golden suites: small, hand-labelled cases that score a surface's question as served.

A golden suite is not training data. It is the fixed yardstick every wording change, threshold
move and fine-tune is read against — small enough to eyeball, and run against the SAME question
object the server asks, imported from its owner rather than copied.

    laya-lab golden agent-open-work                     # the SERVED model, on MPS
    laya-lab golden agent-open-work --model english     # the base checkpoint
    laya-lab golden agent-open-work --all-checkpoints   # the base + every fine-tune on disk, one table
    laya-lab golden agent-open-work --registered        # the base + champion + every registered version (nightly)

Each name has two suites, scored together and reported apart: the checked-in public one (the
headline) and the private one `promote.py` writes. Why they are named apart: owned by
lab/README.md — see "Golden suites". A private case the served fine-tune trained on is listed
as `contaminated` and left out of the private numbers. A public case tagged provisional is scored
and listed under `provisional`, out of the headline until the operator agrees it (`counted_public`).

The cut is the one served (`registry.served_threshold`: swept for this checkpoint, else the
code's). Prints accuracy and κ ± its interval at that cut, the sweep, Brier, ECE (with reliability bins around
both live cuts, so `--all-checkpoints` tracks calibration per checkpoint) and every miss by case id; writes the
report to `<data>/laya/evals/` (name and the fields the rack reads: owned by
`laya_bridge/lab_files.py`). Only `run` loads torch, so everything else is unit tested without weights.

Every report is keyed by (checkpoint digest, device, case id) (`infer.checkpoint_digest`: CPU p is not MPS p, the
lm-eval cache lesson) and stamped with the dataset version it scored (`dataset_version`, Langfuse's rule: the golden
suites and the operator label set are each named by when they took that content; PROMOTE appends a version). With
`reuse` (the nightly pack) a target whose newest report has the same digest, device, dataset and question version is
not scored again: that report stands.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from laya_lab import siblings  # noqa: F401  (puts tldrack and laya_bridge on sys.path)
from laya_lab import infer
from laya_lab.data import atomic_write_text, jsonl_line, open_private
from laya_lab.infer import laya_version  # noqa: F401  (re-exported: heldout and gate read it from here)
from laya_lab.metrics import SWEEP, at_cut, brier, ece, fmt_num, fmt_report_kappa, reliability, trust_at_cut
from laya_lab.open_work_data import operator_fold
from laya_lab.registry import (
    available_checkpoints,
    fine_tune_name,
    registry_versions,
    resolve_model,
    served_done_threshold,
    served_model,
    served_threshold,
)
from laya_bridge import files, lab_files, paths, review_log, rows
from laya_bridge.paths import EVALS_DIRNAME  # noqa: F401  (re-exported)
from tldrack import open_work

#: The public suites (`paths.PUBLIC_GOLDEN_DIR` owns the location; read through it at call time).
GOLDEN_DIR = paths.PUBLIC_GOLDEN_DIR


@dataclass(frozen=True)
class Suite:
    name: str
    surface: str
    question: dict[str, Any]
    question_version: int
    threshold: float
    #: How much worse a false ⚠️ is than a missed one; the cut rule's owner is named in metrics.py.
    false_warning_cost: float
    #: The agent rack's ✅ cut, and how much worse a false ✅ is than a missed one (open_work.py § Three bands).
    done_threshold: float
    false_done_cost: float
    build_state: Callable[[dict[str, Any]], Any]
    #: The state at another question version, for a challenger measured against the served one (`question.py`).
    build_state_at: Callable[[dict[str, Any], int], Any] | None = None

    @property
    def fine_tune(self) -> str:
        return fine_tune_name(self.surface)


SUITES: dict[str, Suite] = {
    "agent-open-work": Suite(
        name="agent-open-work",
        surface="agent_open_work",
        question=open_work.OPEN_WORK_QUESTION,
        question_version=open_work.QUESTION_VERSION,
        threshold=open_work.OPEN_WORK_AT,
        false_warning_cost=open_work.FALSE_WARNING_COST,
        done_threshold=open_work.DONE_AT,
        false_done_cost=open_work.FALSE_DONE_COST,
        build_state=lambda raw: open_work.decide_state(raw["hand_off"], raw.get("request")),
        build_state_at=lambda raw, version: open_work.decide_state(raw["hand_off"], raw.get("request"), version=version),
    ),
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """A PUBLIC suite's rows, strictly: a malformed row in a checked-in suite stops the run.
    `#` lines are comments. Each row: id, state, expected (bool), tags, why."""
    cases, seen = [], set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        where = f"{path.name}:{lineno}"
        if not isinstance(row.get("id"), str) or row["id"] in seen:
            raise ValueError(f"{where}: missing or duplicate id")
        if not isinstance(row.get("expected"), bool):
            raise ValueError(f"{where}: expected must be true or false")
        if not isinstance(row.get("state"), dict) or not row["state"].get("hand_off"):
            raise ValueError(f"{where}: state.hand_off is required")
        seen.add(row["id"])
        cases.append(row)
    return cases


def load_suites(base: Path, name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(public, private)`. The private suite may be absent; it is read by the reader the plugin
    uses (`lab_files.read_golden_suite`), so the deck and this score the same cases. Ids and texts
    are unique across BOTH suites."""
    public = load_cases(paths.PUBLIC_GOLDEN_DIR / f"{name}.jsonl")
    private, code = lab_files.read_golden_suite(paths.private_golden_path(base, name))
    if code not in (files.OK, files.MISSING):
        raise SystemExit(f"[laya-lab] {paths.private_golden_path(base, name)} is {code}; refusing to read it")
    ids: set[str] = set()
    texts: dict[str, str] = {}
    for case in (*public, *private):
        if case["id"] in ids:
            raise ValueError(f"{name}: case id {case['id']} appears twice across the public and private suites")
        key = rows.state_key(case["state"]["hand_off"])
        if key in texts:
            raise ValueError(f"{name}: case {case['id']} repeats the text of {texts[key]}")
        ids.add(case["id"])
        texts[key] = case["id"]
    return public, private


def counted_public(base: Path, surface: str, public: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """`(public cases that count, provisional ids held back)`: the rule is `lab_files.PROVISIONAL_TAG`'s. Read
    tolerantly: an unreadable operator log agrees nothing, which can only hold cases back, never count one."""
    _code, fold = review_log.read_fold(base, surface)
    return lab_files.counted_cases(public, lab_files.signed_off_ids(public, fold.labels, fold.skipped))


def suites_digest(base: Path, name: str) -> tuple[str, int, int]:
    """`(digest, public cases, private cases)`: 12 hex of the public and the private suite files' bytes (missing = empty)."""
    digest = hashlib.sha256()
    for path in (paths.PUBLIC_GOLDEN_DIR / f"{name}.jsonl", paths.private_golden_path(base, name)):
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            pass
        digest.update(b"\0")
    public, private = load_suites(base, name)
    return digest.hexdigest()[:12], len(public), len(private)


def record_dataset_version(base: Path, name: str, by: str) -> dict[str, Any]:
    """The golden suites' current version: the first `golden/versions.jsonl` row with today's content, appended now
    (`by` who saw it first) when there is none. PROMOTE calls it after a write, so the suite it grew is a new version."""
    digest, n_public, n_private = suites_digest(base, name)
    for row in reversed(lab_files.read_dataset_versions(base, name)):
        if row["digest"] == digest:
            return row
    row = lab_files.dataset_row(
        suite=name, at=rows.utc_stamp(), digest=digest, public_cases=n_public, private_cases=n_private, by=by
    )
    with open_private(paths.dataset_versions_path(base), "a") as handle:
        handle.write(jsonl_line(row))
    return row


def operator_version(base: Path, surface: str) -> dict[str, Any]:
    """The operator label set's version: its newest live answer's time and how many live labels it holds. An undo
    lowers the count, so no two label sets of one log share a version. Read tolerantly (an unreadable log is empty)."""
    _code, fold = review_log.read_fold(base, surface)
    stamps = [row["at"] for row in fold.live.values() if isinstance(row.get("at"), str)]
    return {"at": max(stamps) if stamps else None, "rows": len(fold.labels)}


def dataset_version(base: Path, name: str, *, by: str = "golden") -> dict[str, Any]:
    """`{"id": lab_files.dataset_version_id(...), "golden": row, "operator": {at, rows}}` for suite `name`, recording
    the golden suites' version when it is new."""
    suite = SUITES[name]
    golden = record_dataset_version(base, name, by)
    operator = operator_version(base, suite.surface)
    operator_at = f"{operator['at']}n{operator['rows']}" if operator["at"] else None
    return {
        "id": lab_files.dataset_version_id(suite.question_version, golden["at"], operator_at),
        "golden": {k: golden[k] for k in ("at", "digest", "public_cases", "private_cases")},
        "operator": operator,
    }


def registered_targets(base: Path, surface: str) -> dict[str, Path | None]:
    """The base, the served champion (by its served name) and every other registered version of `surface` (by folder):
    what the nightly pack scores. Deduped by folder, the served name first."""
    targets: dict[str, Path | None] = {"english": None}
    served = served_model(base, surface)
    if served != "english":
        targets[served] = resolve_model(base, surface, served)[1]
    seen = {path for path in targets.values() if path is not None}
    for version_name, version in sorted(registry_versions(base)["versions"].items()):
        path = Path(version["path"])
        if version["surface"] == surface and path not in seen and version_name in available_checkpoints(base, surface):
            targets[version_name] = path
            seen.add(path)
    return targets


def reusable_report(
    base: Path, name: str, target: str, digest: str | None, device: str, dataset: str
) -> tuple[Path, dict[str, Any]] | None:
    """The newest golden report of `target` keyed exactly as a new one would be: same digest, device, dataset version
    and question version. None when anything differs, or the digest is unknown."""
    found = lab_files.newest_report(base, name, target, "golden")
    if digest is None or found is None:
        return None
    path, report = found
    checkpoint = report.get("checkpoint") if isinstance(report.get("checkpoint"), dict) else {}
    same = (
        checkpoint.get("digest") == digest
        and report.get("device") == device
        and report.get("dataset_version") == dataset
        and report.get("question_version") == SUITES[name].question_version
    )
    return (path, report) if same else None


def eval_rows(suite: Suite, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cases as `laya-evals` rows, carrying the served question and state builder."""
    return [
        {
            "state": suite.build_state(case["state"]),
            "questions": {"q": suite.question},
            "expected": {"q": case["expected"]},
            "tags": list(case.get("tags") or []),
        }
        for case in cases
    ]


def score(
    cases: list[dict[str, Any]], probabilities: list[float], threshold: float, done_threshold: float | None = None
) -> dict[str, Any]:
    """Accuracy and κ at `threshold` (with κ's interval and precision/recall there: `trust`), the sweep, Brier,
    ECE with reliability bins around both cuts, a confusion count and the misses. Pure."""
    expected = [case["expected"] for case in cases]
    head = at_cut(probabilities, expected, threshold)
    misses = [
        {"id": case["id"], "expected": case["expected"], "p": round(p, 3), "tags": case.get("tags", [])}
        for case, p in zip(cases, probabilities)
        if (p >= threshold) != case["expected"]
    ]
    return {
        "cases": len(cases),
        "at_threshold": head,
        "sweep": [at_cut(probabilities, expected, cut) for cut in SWEEP],
        "brier": brier(probabilities, expected),
        "kappa": head["kappa"],
        "trust": trust_at_cut(probabilities, expected, threshold),
        "ece": ece(probabilities, expected),
        "reliability": reliability(probabilities, expected, (threshold, done_threshold)),
        "misses": misses,
    }


def private_block(
    cases: list[dict[str, Any]], probabilities: list[float], threshold: float, trained: frozenset[str]
) -> dict[str, Any]:
    """The private suite's numbers without the cases `trained` covers (listed as `contaminated`).
    Ids and keys only: a private case's text never enters a report."""
    contaminated = [
        case["id"] for case in cases if rows.state_key(case["state"]["hand_off"]) in trained or case.get("key") in trained
    ]
    clean = [(case, p) for case, p in zip(cases, probabilities) if case["id"] not in contaminated]
    expected = [case["expected"] for case, _p in clean]
    ps = [p for _case, p in clean]
    head = at_cut(ps, expected, threshold)
    return {
        "cases": len(clean),
        "accuracy": head["accuracy"],
        "kappa": head["kappa"],
        "false_warnings": head["false_warnings"],
        "missed_open": head["missed_open"],
        "brier": brier(ps, expected),
        "contaminated": contaminated,
        "misses": [
            {"id": case["id"], "expected": case["expected"], "p": round(p, 3)}
            for case, p in clean
            if (p >= threshold) != case["expected"]
        ],
    }


def provisional_block(
    public: list[dict[str, Any]], by_id: dict[str, float], held_back: list[str], threshold: float
) -> dict[str, Any]:
    """The provisional cases' own numbers, apart from the headline: ids, and which the model misses at the cut."""
    held = set(held_back)
    return {
        "cases": len(held_back),
        "ids": list(held_back),
        "misses": [
            {"id": case["id"], "expected": case["expected"], "p": round(by_id[case["id"]], 3)}
            for case in public
            if case["id"] in held and (by_id[case["id"]] >= threshold) != case["expected"]
        ],
    }


def operator_disputes(base: Path, surface: str) -> list[str]:
    """Public case ids the operator FLIPPED in the deck. Read-only: an agent edits the public file by hand."""
    fold = operator_fold(base, surface)
    return sorted(
        key.removeprefix("golden:") for key, row in fold.labels.items() if key.startswith("golden:") and row["answer"] == "flip"
    )


def batch_lines(base: Path, surface: str, public: list[dict[str, Any]], name: str) -> list[str]:
    """One line per batch of agent-written cases: where its sample sign-off stands (`lab_files.BATCH_TAG` owns the rule)."""
    _code, fold = review_log.read_fold(base, surface)
    lines = []
    for batch, standing in lab_files.batch_standing(public, fold.labels, fold.skipped).items():
        head = f"  {batch}: {standing['size']} cases · sample {standing['agreed']}/{len(standing['sample'])} agreed"
        held = sum(1 for case in public if lab_files.case_batch(case) == batch and lab_files.is_provisional(case))
        if standing["state"] == "accepted":
            tail = f" · ACCEPTED: at {lab_files.BATCH_CONFIDENCE:.0%} at most {standing['bound']} carry a wrong label" + (
                f" · {held} still tagged provisional: laya-lab settle {name} --apply" if held else ""
            )
        elif standing["state"] == "rejected":
            tail = (
                f" · REJECTED ({', '.join(standing['defects'])} flipped or skipped): REVIEW now deals the whole "
                "batch, and each case counts on its own AGREE"
            )
        else:
            tail = " · pending: REVIEW deals the sample"
        lines.append(head + tail)
    return lines


def settle(name: str, base: Path, *, apply: bool = False) -> int:
    """Drop the provisional tag from every public case that counts (`counted_public`), so a run on an EMPTY store
    (the verifier card's reproduce command, `bench`) counts what the operator signed off. Dry run without `apply`.
    Only the changed lines are rewritten; comments and every other line keep their bytes. 0 · 3 nothing to settle."""
    suite = SUITES[name]
    path = paths.PUBLIC_GOLDEN_DIR / f"{name}.jsonl"
    public = load_cases(path)
    counted, held = counted_public(base, suite.surface, public)
    settling = {case["id"] for case in counted if lab_files.is_provisional(case)}
    print(f"[laya-lab] {name}: {len(settling)} signed-off cases to settle · {len(held)} stay provisional")
    for line in batch_lines(base, suite.surface, public, name):
        print(line)
    if not settling:
        return 3
    if not apply:
        print(f"  dry run: laya-lab settle {name} --apply rewrites {path.name}")
        return 0
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() and not line.lstrip().startswith("#") else None
        if row is not None and row["id"] in settling:
            row["tags"] = [tag for tag in row["tags"] if tag != lab_files.PROVISIONAL_TAG]
            line = json.dumps(row, ensure_ascii=False)
        out.append(line)
    atomic_write_text(path, "\n".join(out) + "\n")
    print(f"  settled {len(settling)} → {path}")
    return 0


def evaluate(
    name: str,
    base: Path,
    targets: dict[str, Path | None],
    *,
    device: str = "mps",
    router: Any = None,
    reuse: bool = False,
    by: str = "golden",
) -> tuple[list[tuple[str, dict[str, Any], Path, bool]], dict[str, Any]]:
    """Score `targets` on the suite; `([(target, report, path, reused)], meta)`. Each report is written as it is made.
    `reuse` keeps a target's matching report (`reusable_report`) instead of scoring it again."""
    suite = SUITES[name]
    public, private = load_suites(base, name)
    meta = {"disputes": operator_disputes(base, suite.surface)}  # before any work: an unreadable operator log refuses
    counted, held_back = counted_public(base, suite.surface, public)
    dataset = dataset_version(base, name, by=by)
    meta.update(counted=len(counted), private=len(private), held_back=held_back, dataset=dataset)
    written: list[tuple[str, dict[str, Any], Path, bool]] = []
    todo: dict[str, Path | None] = {}
    for target, path in targets.items():
        found = (
            reusable_report(base, name, target, infer.checkpoint_digest(target, path), device, dataset["id"]) if reuse else None
        )
        if found is not None:
            written.append((target, found[1], found[0], True))
        else:
            todo[target] = path
    if not todo:
        return written, meta
    router = router if router is not None else infer.open_router(base, device, todo)
    trained, _unrecorded = rows.trained_keys(base, suite.fine_tune)
    states = [suite.build_state(case["state"]) for case in (*public, *private)]
    run_id = secrets.token_hex(6)
    for target, path in todo.items():
        probabilities = infer.predict_noul(router, target, states, suite.question)
        public_p, private_p = probabilities[: len(public)], probabilities[len(public) :]
        cut, source = served_threshold(base, suite.surface, target, code=suite.threshold)
        done_cut, _done_source = served_done_threshold(base, suite.surface, target, code=suite.done_threshold)
        by_id = {case["id"]: p for case, p in zip(public, public_p)}
        report = score(counted, [by_id[case["id"]] for case in counted], cut, done_cut)
        report["provisional"] = provisional_block(public, by_id, held_back, cut)
        report.update(
            lab_files.golden_report_fields(
                rows.utc_stamp(), run_id, report["kappa"], infer.checkpoint_stamp(base, router, target, path)
            )
        )
        report.update(
            suite=name,
            model=target,
            question_version=suite.question_version,
            threshold_source=source,
            laya_version=laya_version(),
            device=device,
            revisions=dict(getattr(router, "loaded_revisions", None) or {}),
            dataset_version=dataset["id"],
            dataset={"golden": dataset["golden"], "operator": dataset["operator"]},
            probabilities={case["id"]: round(p, 4) for case, p in zip((*public, *private), probabilities)},
            private=private_block(private, private_p, cut, trained if path is not None else frozenset()),
            **lab_files.eval_run_field(),
        )
        out = base / EVALS_DIRNAME / lab_files.golden_report_name(name, target, lab_files.report_stamp())
        atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
        written.append((target, report, out, False))
    meta["run_id"] = run_id
    return written, meta


def run(
    name: str,
    base: Path,
    *,
    model: str = "served",
    all_checkpoints: bool = False,
    registered: bool = False,
    device: str = "mps",
    router: Any = None,
) -> int:
    suite = SUITES[name]
    if all_checkpoints:
        targets: dict[str, Path | None] = {"english": None, **available_checkpoints(base, suite.surface)}
    elif registered:
        targets = registered_targets(base, suite.surface)
    else:
        resolved, path = resolve_model(base, suite.surface, model)
        targets = {resolved: path}
    written, meta = evaluate(name, base, targets, device=device, router=router)
    many = all_checkpoints or registered
    print(
        f"[laya-lab] {name} · q v{suite.question_version} · {meta['counted']} public + {meta['private']} private cases"
        + (f" · {len(meta['held_back'])} provisional (not counted until agreed in REVIEW)" if meta["held_back"] else "")
        + f" · dataset {meta['dataset']['id']}"
        + (f" · run {meta.get('run_id')}" if many else "")
    )
    if many:
        for line in checkpoint_table([(target, report, out) for target, report, out, _reused in written]):
            print(line)
    else:
        for line in report_lines(*written[0][:2]):
            print(line)
    for line in batch_lines(base, suite.surface, load_suites(base, name)[0], name):
        print(line)
    if meta["disputes"]:
        print(
            f"  operator disputes: {', '.join(meta['disputes'])}  (public cases flipped in REVIEW; edit golden/{name}.jsonl by hand)"
        )
    for _target, _report, out, _reused in written:
        print(f"  report → {out}")
    return 0


def report_lines(target: str, report: dict[str, Any]) -> list[str]:
    head, private = report["at_threshold"], report["private"]
    checkpoint = report["checkpoint"]["name"]
    lines = [
        f"  {target}{f' ({checkpoint})' if checkpoint and checkpoint != target else ''}",
        f"  at {head['threshold']} ({report['threshold_source']}): accuracy {head['accuracy']}  "
        f"κ {fmt_report_kappa(report)}  "
        f"false ⚠️ {head['false_warnings']}  missed open {head['missed_open']}  brier {report['brier']}  "
        f"ECE {fmt_num(report.get('ece'), 3)}",
    ]
    for s in report["sweep"]:
        lines.append(
            f"  sweep {s['threshold']}: accuracy {s['accuracy']}  κ {fmt_num(s['kappa'])}  "
            f"false ⚠️ {s['false_warnings']}  missed open {s['missed_open']}"
        )
    for miss in report["misses"]:
        lines.append(f"  miss  {miss['id']:<28} expected {'open' if miss['expected'] else 'done'}  p={miss['p']}")
    provisional = report.get("provisional") or {}
    if provisional.get("cases"):
        misses = ", ".join(miss["id"] for miss in provisional["misses"]) or "none"
        lines.append(f"  provisional: {provisional['cases']} cases, not counted until agreed in REVIEW · misses: {misses}")
    if private["cases"] or private["contaminated"]:
        lines.append(
            f"  private: {private['cases']} cases  accuracy {private['accuracy']}  κ {fmt_num(private['kappa'])}  "
            f"false ⚠️ {private['false_warnings']}  missed open {private['missed_open']}"
            + (f"  ({len(private['contaminated'])} contaminated: trained on)" if private["contaminated"] else "")
        )
    return lines


def checkpoint_table(written: list[tuple[str, dict[str, Any], Path]]) -> list[str]:
    """One row per checkpoint: model · reg · acc@cut · κ ± · brier · ECE · false⚠️ · missed · private κ."""
    width = max([len("model"), *(len(target) for target, _r, _o in written)])
    lines = [f"  {'model':<{width}}  reg   acc@cut            κ ±   brier     ECE  false⚠️  missed  private κ"]
    for target, report, _out in written:
        head = report["at_threshold"]
        reg = "base" if report["checkpoint"]["name"] is None else ("✓" if report["checkpoint"]["registered"] else "")
        lines.append(
            f"  {target:<{width}}  {reg:<4}  {head['accuracy']}@{head['threshold']:<5}  {fmt_report_kappa(report):>11}  "
            f"{fmt_num(report['brier'], 4):>6}  {fmt_num(report.get('ece'), 3):>6}  {head['false_warnings']:>7}  "
            f"{head['missed_open']:>6}  {fmt_num(report['private']['kappa']):>9}"
        )
    return lines
