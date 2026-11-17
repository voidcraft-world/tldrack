"""The promotion gate: may a candidate checkpoint replace the model a surface serves?

    laya-lab gate agent-open-work                                 # the newest unserved checkpoint vs the served model
    laya-lab gate agent-open-work --candidate <checkpoint folder> --served english
    laya-lab gate agent-open-work --prospective                   # the served fine-tune vs the champion it replaced
    laya-lab gate agent-open-work --candidate <a> --served <b> --fixture tests/fixtures/promotion-gate/gate-<a>-vs-<b>.json

`train` runs the same gate in process and registers only on PASS. Every check reads the two voters on
the SAME rows, each at the cuts it would serve at: the candidate at the code's cuts (a registration
clears the swept cut), the served model at its live ones (`registry.served_threshold`).

THE ROWS. Public golden cases (`build()` never trains on one) that count (`golden.counted_public`: a
provisional case only once the operator agreed it) and labelled turn ends that NEITHER checkpoint trained
on, by each one's own record (`compare.untrained_rows`). "The candidate's 15% split" is not held out
from the served model: the two trained on different data sets, so each one's split differs.

THE CHECKS, each recorded in the verdict with its numbers and its p:
- (a) `heldout`: held-out log-loss improves and top-1 accuracy falls at most ACCURACY_SLACK, the rule
  `train` has always applied, now against the served model rather than the base.
- (b) `warning_cost`, `done_cost`: the cost at each live cut, by the repo's own cost rules
  (`metrics.warning_costs` / `done_costs`, the weights owned by tldrack/open_work.py), is no
  worse than the served model's by McNemar's paired test (`metrics.paired_test`) on public golden ∪
  held-out. The stated tolerance: a candidate is refused only when it is worse with p < PAIRED_ALPHA, so
  one exactly as good is refused by noise at most 1 time in 20 per cut. A hard "fp ≤ served's" would refuse on
  a difference of a few cases that the paired test reads as noise (the tests' `golden-candidate` fixture is one).
- (c) `private_golden`: no new misses on the private suite's cases neither checkpoint trained on;
  skipped while there are none.
- (d) `floor`: held-out ⚠️-cut cost strictly below the TF-IDF + logistic floor's (`floor.py`), trained on
  the candidate's own training rows.
A check without rows to measure it is `unmeasured`, and an unmeasured gate refuses, as `train` always
has: an unmeasured checkpoint never reaches a caller.

AFTER REGISTRATION (`--prospective`). Once PROSPECTIVE_MIN_LABELS operator labels have arrived in the
clean window, on rows neither the candidate nor the champion it replaced trained on, the same paired test
runs at BOTH cuts, held to a family-wise PAIRED_ALPHA by Holm's step-down (`holm`): the smaller p against
α/2, the larger against α only if the first is rejected. Either cut shown worse fails it, so a ✅-cut
regression can no longer hide behind a ⚠️ cut that held. Each side is scored at the cuts it SERVED at:
- the champion at the cuts its promotion verdict recorded while it was live (`verdict_cuts`), never a
  registry re-read: registering the candidate dropped any cut swept for it, so a lookup would fall back
  to the code's cuts and measure a cut it never used;
- the candidate at its swept cuts only when the sweep records when it ran, and then the window opens at
  the LATER of its registration and that sweep (`prospective_cuts`): a sweep reads operator labels, so a
  window that included them would score the cut on the rows that chose it. A sweep with no time scores
  the candidate at the code's cuts. The window used is recorded in the report.
It reports; it never unregisters (rollback is the operator's press, or the one-night auto-rollback).

FIXTURES (`--fixture PATH`). A promotion verdict's evidence, trimmed to numbers (`trimmed_evidence`): held-out
and private rows become ordinals, so a checked-in fixture carries no key of a private text.

Reports land in `evals/` as `gate-<suite>-<stamp>-<run id>.json` (`lab_files.gate_report_name`). Keys, ids and
numbers only. Only `run` loads torch; everything above it is pure.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import floor as floor_voter
from laya_lab import infer
from laya_lab.compare import Timed, Voter, score_set, untrained_rows, was_trained
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES, Suite, counted_public, dataset_version, laya_version, load_suites
from laya_lab.metrics import at_cut, at_done_cut, done_costs, log_loss, paired_test, pick_cut, pick_done_cut, warning_costs
from laya_lab.open_work_data import require_sources
from laya_lab.registry import (
    available_checkpoints,
    fine_tune_name,
    resolve_model,
    served_done_threshold,
    served_threshold,
    swept_cut,
)
from laya_bridge import lab_files, review_data, rows
from laya_bridge.paths import EVALS_DIRNAME, registry_view

REFUSED = 3
#: Accuracy a candidate may give up for a better-calibrated loss before it is refused.
ACCURACY_SLACK = 0.01
#: The stated tolerance of every paired test here: the chance noise refuses a candidate exactly as good.
PAIRED_ALPHA = 0.05
#: Operator labels since registration before the prospective test runs.
PROSPECTIVE_MIN_LABELS = 20
#: Top-1 for a yes/no question: laya's own report scores `p ≥ 0.5` as yes.
TOP1 = 0.5
PASS, FAIL, SKIPPED, UNMEASURED, PENDING = "pass", "fail", "skipped", "unmeasured", "pending"
CHECKS = ("heldout", "warning_cost", "done_cost", "private_golden", "floor")
KIND = "gate"


# ── the pure rule ─────────────────────────────────────────────────────────────


def loss_rule(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    """`(ok, why)`: `after` beat `before` on log-loss and lost at most ACCURACY_SLACK of accuracy."""
    d_loss = after["loss"] - before["loss"]
    d_acc = after["accuracy"] - before["accuracy"]
    if d_loss >= 0:
        return False, f"held-out loss did not improve ({before['loss']:.4f} → {after['loss']:.4f})"
    if d_acc < -ACCURACY_SLACK:
        return False, f"accuracy fell {-d_acc:.3f} ({before['accuracy']:.3f} → {after['accuracy']:.3f})"
    return True, f"loss {before['loss']:.4f} → {after['loss']:.4f}, accuracy {before['accuracy']:.3f} → {after['accuracy']:.3f}"


def _result(state: str, why: str, **numbers: Any) -> dict[str, Any]:
    return {"state": state, "why": why, **numbers}


def _paired_rows(evidence: dict[str, Any], *sets: str) -> tuple[list[bool], list[float], list[float], dict[str, int]]:
    expected: list[bool] = []
    candidate: list[float] = []
    served: list[float] = []
    counts: dict[str, int] = {}
    for name in sets:
        block = evidence.get(name)
        if not block:
            continue
        expected += block["expected"]
        candidate += block["candidate"]
        served += block["served"]
        counts[name] = len(block["expected"])
    return expected, candidate, served, counts


def check_heldout(evidence: dict[str, Any]) -> dict[str, Any]:
    held = evidence.get("heldout")
    if not held or not held["expected"]:
        return _result(
            UNMEASURED,
            f"no held-out rows neither checkpoint trained on ({(evidence.get('notes') or {}).get('heldout') or 'none'})",
        )
    expected = held["expected"]
    before = {"loss": log_loss(held["served"], expected), "accuracy": at_cut(held["served"], expected, TOP1)["accuracy"]}
    after = {"loss": log_loss(held["candidate"], expected), "accuracy": at_cut(held["candidate"], expected, TOP1)["accuracy"]}
    ok, why = loss_rule(before, after)
    return _result(PASS if ok else FAIL, why, rows=len(expected), served=before, candidate=after, slack=ACCURACY_SLACK)


def check_cost(evidence: dict[str, Any], which: str) -> dict[str, Any]:
    """(b): the paired cost test at one live cut (`which` = "warning" | "done") on public golden ∪ held-out."""
    expected, candidate, served, counts = _paired_rows(evidence, "golden", "heldout")
    if not expected:
        return _result(UNMEASURED, "no paired rows to compare the two on")
    costs, cand, champ = evidence["costs"], evidence["candidate"], evidence["served"]
    if which == "warning":
        fp_cost = costs["false_warning"]
        mine = warning_costs(candidate, expected, cand["at"], fp_cost)
        theirs = warning_costs(served, expected, champ["at"], fp_cost)
        totals = {
            "candidate": pick_cut([at_cut(candidate, expected, cand["at"])], fp_cost)["cost"],
            "served": pick_cut([at_cut(served, expected, champ["at"])], fp_cost)["cost"],
        }
        label, cuts = "⚠️", {"candidate": cand["at"], "served": champ["at"]}
    else:
        fp_cost = costs["false_done"]
        mine = done_costs(candidate, expected, cand["done_at"], fp_cost)
        theirs = done_costs(served, expected, champ["done_at"], fp_cost)
        totals = {
            "candidate": pick_done_cut([at_done_cut(candidate, expected, cand["done_at"])], fp_cost, None)["cost"],
            "served": pick_done_cut([at_done_cut(served, expected, champ["done_at"])], fp_cost, None)["cost"],
        }
        label, cuts = "✅", {"candidate": cand["done_at"], "served": champ["done_at"]}
    test = paired_test(mine, theirs)
    worse = test["delta"] > 0 and test["p"] < PAIRED_ALPHA
    where = " + ".join(f"{n} {name}" for name, n in counts.items())
    if worse:
        reading = f"worse, p = {test['p']:.3f} < α {PAIRED_ALPHA}"
    elif test["delta"] > 0:
        reading = f"higher but within noise, p = {test['p']:.3f} ≥ α {PAIRED_ALPHA}"
    else:
        reading = "not worse"
    why = f"{label}-cut cost {test['candidate']:g} vs served {test['served']:g} on {where} rows ({reading})"
    return _result(
        FAIL if worse else PASS,
        why,
        p=test["p"],
        test=test,
        totals=totals,
        cuts=cuts,
        fp_cost=fp_cost,
        rows=counts,
        alpha=PAIRED_ALPHA,
    )


def check_private(evidence: dict[str, Any]) -> dict[str, Any]:
    block = evidence.get("private") or {}
    contaminated = list(block.get("contaminated") or [])
    if not block.get("expected"):
        why = f"every private case was trained on ({len(contaminated)})" if contaminated else "the private golden suite is empty"
        return _result(SKIPPED, why, cases=0, contaminated=contaminated)
    cand, champ = evidence["candidate"]["at"], evidence["served"]["at"]
    new = [
        case_id
        for case_id, e, c, s in zip(block["ids"], block["expected"], block["candidate"], block["served"])
        if (c >= cand) != e and (s >= champ) == e
    ]
    fixed = [
        case_id
        for case_id, e, c, s in zip(block["ids"], block["expected"], block["candidate"], block["served"])
        if (c >= cand) == e and (s >= champ) != e
    ]
    why = (
        f"{len(new)} new miss{'es' if len(new) != 1 else ''} on the private suite: {', '.join(new)}"
        if new
        else f"no new misses on {len(block['ids'])} private cases ({len(fixed)} fixed)"
    )
    return _result(FAIL if new else PASS, why, cases=len(block["ids"]), new_misses=new, fixed=fixed, contaminated=contaminated)


def check_floor(evidence: dict[str, Any]) -> dict[str, Any]:
    held, floor = evidence.get("heldout"), evidence.get("floor")
    notes = evidence.get("notes") or {}
    if not held or not held["expected"]:
        return _result(UNMEASURED, "no held-out rows to score the floor on")
    if not floor:
        return _result(UNMEASURED, f"the floor voter could not train ({notes.get('floor') or 'not run'})")
    expected, fp_cost = held["expected"], evidence["costs"]["false_warning"]
    mine = warning_costs(held["candidate"], expected, evidence["candidate"]["at"], fp_cost)
    theirs = warning_costs(floor["p"], expected, floor["cut"], fp_cost)
    test = paired_test(mine, theirs)
    beats = test["candidate"] < test["served"]
    why = (
        f"held-out ⚠️-cut cost {test['candidate']:g} vs the TF-IDF floor's {test['served']:g} "
        f"(floor cut {floor['cut']}, trained on {floor['rows']} rows){'' if beats else ': does not beat it'}"
    )
    return _result(
        PASS if beats else FAIL,
        why,
        candidate=test["candidate"],
        floor=test["served"],
        cut=floor["cut"],
        test=test,
        rows=len(expected),
    )


def promotion_verdict(evidence: dict[str, Any]) -> dict[str, Any]:
    """`{verdict: PASS | REFUSE, promote, why, checks}` from paired evidence (`gather` builds it; the tests build
    it from fixture reports). Pure: the rule is testable without torch."""
    checks = {
        "heldout": check_heldout(evidence),
        "warning_cost": check_cost(evidence, "warning"),
        "done_cost": check_cost(evidence, "done"),
        "private_golden": check_private(evidence),
        "floor": check_floor(evidence),
    }
    failed = [name for name in CHECKS if checks[name]["state"] == FAIL]
    unmeasured = [name for name in CHECKS if checks[name]["state"] == UNMEASURED]
    promote = not failed and not unmeasured
    if failed:
        why = "; ".join(f"{name}: {checks[name]['why']}" for name in failed)
    elif unmeasured:
        why = "unmeasured — refusing to register an unmeasured checkpoint: " + "; ".join(
            f"{name}: {checks[name]['why']}" for name in unmeasured
        )
    else:
        why = "; ".join(f"{name}: {checks[name]['why']}" for name in CHECKS if checks[name]["state"] == PASS)
    return {
        "verdict": "PASS" if promote else "REFUSE",
        "promote": promote,
        "why": why,
        "checks": checks,
        "failed": failed,
        "unmeasured": unmeasured,
        "alpha": PAIRED_ALPHA,
    }


def holm(ps: Sequence[float], alpha: float) -> list[tuple[bool, float]]:
    """Holm's step-down: `(rejected, the level that p was held to)` per p, in input order. The k-th smallest p
    is held to α/(m − k), and the first that holds ends the rejections, so the family-wise error stays ≤ α."""
    held: list[tuple[bool, float]] = [(False, alpha)] * len(ps)
    rejecting = True
    for rank, index in enumerate(sorted(range(len(ps)), key=lambda i: ps[i])):
        level = round(alpha / (len(ps) - rank), 6)
        rejecting = rejecting and ps[index] < level
        held[index] = (rejecting, level)
    return held


CUT_MARK = {"warning": "⚠️", "done": "✅"}


def prospective_check(block: dict[str, Any] | None, costs: dict[str, float]) -> dict[str, Any]:
    """The paired test at both live cuts on operator rows in the clean window, under Holm. `block` = `{expected,
    candidate, champion, candidate_cuts, champion_cuts}`; None or under PROSPECTIVE_MIN_LABELS rows is `pending`."""
    labels = len(block["expected"]) if block else 0
    if labels < PROSPECTIVE_MIN_LABELS:
        return _result(
            PENDING,
            f"{labels} operator labels in the window: the test needs {PROSPECTIVE_MIN_LABELS}",
            labels=labels,
            needs=PROSPECTIVE_MIN_LABELS,
            steps=[],
        )
    expected, cand, champ = block["expected"], block["candidate_cuts"], block["champion_cuts"]
    cost_rules = (
        ("warning", lambda p, cuts: warning_costs(p, expected, cuts["at"], costs["false_warning"])),
        ("done", lambda p, cuts: done_costs(p, expected, cuts["done_at"], costs["false_done"])),
    )
    tests = [
        (name, paired_test(cost_of(block["candidate"], cand), cost_of(block["champion"], champ))) for name, cost_of in cost_rules
    ]
    steps = [
        {"cut": name, "p": test["p"], "level": level, "worse": rejected and test["delta"] > 0, "test": test}
        for (name, test), (rejected, level) in zip(tests, holm([test["p"] for _n, test in tests], PAIRED_ALPHA))
    ]
    worse = [step for step in steps if step["worse"]]
    if worse:
        why = (
            f"worse than the champion at the {' and '.join(CUT_MARK[s['cut']] + ' cut' for s in worse)} on {labels} "
            "operator labels (" + ", ".join(f"p = {s['p']:.3g} < {s['level']:g}" for s in worse) + ", Holm)"
        )
    else:
        why = (
            f"no worse than the champion at either cut on {labels} operator labels ("
            + ", ".join(f"{CUT_MARK[s['cut']]} p = {s['p']:.3f}" for s in steps)
            + f"; Holm, family-wise α {PAIRED_ALPHA})"
        )
    return _result(
        FAIL if worse else PASS,
        why,
        labels=labels,
        needs=PROSPECTIVE_MIN_LABELS,
        steps=steps,
        alpha=PAIRED_ALPHA,
        correction="holm",
    )


# ── evidence from voters ──────────────────────────────────────────────────────


def costs_of(suite: Suite) -> dict[str, float]:
    return {"false_warning": suite.false_warning_cost, "false_done": suite.false_done_cost}


def paired_block(
    ids: Sequence[str], expected: Sequence[bool], candidate: Sequence[float], served: Sequence[float], **extra: Any
) -> dict[str, Any]:
    return {
        "ids": list(ids),
        "expected": [bool(e) for e in expected],
        "candidate": [float(p) for p in candidate],
        "served": [float(p) for p in served],
        **extra,
    }


def evidence_from_reports(
    suite: Suite,
    cases: Sequence[dict[str, Any]],
    candidate_report: dict[str, Any],
    served_report: dict[str, Any],
    agreed: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Paired golden evidence from two golden reports (`laya-lab golden`'s `probabilities` by case id): the
    fixture path. Only public cases that count (`agreed`: provisional ids the operator agreed) and both reports
    scored; no held-out rows, so (a) and (d) stay unmeasured."""
    counted, provisional = lab_files.counted_cases(list(cases), agreed)
    both = [
        case
        for case in counted
        if case["id"] in candidate_report["probabilities"] and case["id"] in served_report["probabilities"]
    ]

    def side(report: dict[str, Any]) -> dict[str, Any]:
        # a golden report records its ⚠️ cut; the ✅ cut it predates is the code's
        return {
            "name": report["model"],
            "checkpoint": (report.get("checkpoint") or {}).get("name"),
            "at": report["at_threshold"]["threshold"],
            "done_at": suite.done_threshold,
            "report": report.get("run_id"),
        }

    return {
        "suite": suite.name,
        "surface": suite.surface,
        "costs": costs_of(suite),
        "candidate": side(candidate_report),
        "served": side(served_report),
        "golden": paired_block(
            [c["id"] for c in both],
            [c["expected"] for c in both],
            [candidate_report["probabilities"][c["id"]] for c in both],
            [served_report["probabilities"][c["id"]] for c in both],
            provisional=provisional,
        ),
        "private": None,
        "heldout": None,
        "floor": None,
        "notes": {"heldout": "golden reports carry no held-out rows", "floor": "golden reports carry no held-out rows"},
    }


def newest_unserved(base: Path, surface: str, served_path: Path | None) -> str | None:
    """The newest checkpoint folder on disk that is not the one served: what `gate` judges by default.
    Folder names end in their training stamp, so the newest sorts last."""
    served = Path(served_path).resolve() if served_path is not None else None
    folder = rows.checkpoint_name(fine_tune_name(surface))
    names = sorted(
        name
        for name, path in available_checkpoints(base, surface).items()
        if folder.fullmatch(name) and Path(path).resolve() != served
    )
    return names[-1] if names else None


def suite_for_surface(surface: str) -> Suite | None:
    """The golden suite judging `surface`'s question, or None (that surface keeps the report-only rule)."""
    return next((suite for suite in SUITES.values() if suite.surface == surface), None)


def record_keys(base: Path, path: Path | None) -> frozenset[str] | None:
    """The keys a voter trained on: nothing for a built-in (`path` None), its checkpoint's record otherwise
    (None when that record is missing or unreadable)."""
    return frozenset() if path is None else rows.checkpoint_keys(base, Path(path).name)


class GateRefused(ValueError):
    """The gate cannot judge this pair at all (exit 3): a missing record, or a candidate that is the served model."""


def _instant(at: Any) -> datetime | None:
    """An ISO stamp (`rows.utc_stamp`'s `Z` form or an offset) as an aware datetime; None when it is not one."""
    if not isinstance(at, str):
        return None
    try:
        found = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


def _is_cut(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1


def verdict_cuts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The cuts the champion served at, as the promotion verdict that replaced it recorded them (`served.at`,
    `at_source`, `done_at`, `done_source`); None when that verdict does not carry a usable pair."""
    side = report.get("served") if isinstance(report.get("served"), dict) else {}
    at, done_at = side.get("at"), side.get("done_at")
    if not _is_cut(at) or not _is_cut(done_at) or done_at > at:
        return None
    return {
        "at": float(at),
        "at_source": str(side.get("at_source") or "code"),
        "done_at": float(done_at),
        "done_source": str(side.get("done_source") or "code"),
        "from": f"promotion verdict {report.get('run_id')}",
    }


def prospective_cuts(
    base: Path, suite: Suite, model: str, checkpoint: str
) -> tuple[tuple[float, str], tuple[float, str], dict[str, Any]]:
    """`(⚠️ cut, ✅ cut, window)` for the served candidate in `--prospective`. The rule (why the window opens at the
    later of registration and sweep, and why a sweep with no time scores at the code's cuts): the module docstring."""
    registered = registered_at(base, suite.surface, checkpoint)
    window: dict[str, Any] = {"since": registered, "registered_at": registered, "swept_at": None, "opens_at": "registration"}
    code = ((suite.threshold, "code"), (suite.done_threshold, "code"))
    sweep = swept_cut(base, suite.surface, model)
    if sweep is None:
        return code[0], code[1], window
    swept = _instant(sweep.get("swept_at"))
    if swept is None:
        return code[0], code[1], {**window, "note": "its swept cut records no sweep time, so it is scored at the code's cuts"}
    done = (sweep["done_at"], "swept") if sweep.get("done_at") is not None else code[1]
    since_reg = _instant(registered)
    later = sweep["swept_at"] if since_reg is None or swept > since_reg else registered
    return (
        (sweep["at"], "swept"),
        done,
        {
            **window,
            "since": later,
            "swept_at": sweep["swept_at"],
            "opens_at": "sweep" if later == sweep["swept_at"] else "registration",
        },
    )


def gather(
    name: str,
    base: Path,
    *,
    candidate: str | None,
    served: str,
    labels: str,
    device: str,
    router: Any = None,
    prospective: bool = False,
    champion_cuts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The paired evidence for `candidate` vs `served`: both voters on public golden, the clean private suite and
    held-out rows neither trained on, plus the floor on the held-out rows. Loads torch (through `router`).
    `champion_cuts` (`verdict_cuts`) scores the served side at the cuts it served at instead of the registry's."""
    suite = SUITES[name]
    served_target, served_path = resolve_model(base, suite.surface, served)
    if candidate is None:
        candidate = newest_unserved(base, suite.surface, served_path)
        if candidate is None:
            raise GateRefused(f"no checkpoint on disk besides the served {served_target}: train one first")
    cand_target, cand_path = resolve_model(base, suite.surface, candidate)
    if cand_path is None:
        raise GateRefused(f"{cand_target} is a built-in: the gate judges a fine-tuned checkpoint")
    if served_path is not None and Path(cand_path).resolve() == Path(served_path).resolve():
        raise GateRefused(f"{cand_target} is the served checkpoint: name a different --candidate or --served")
    cand_keys, served_keys = record_keys(base, cand_path), record_keys(base, served_path)
    for target, keys in ((cand_target, cand_keys), (served_target, served_keys)):
        if keys is None:
            raise GateRefused(f"{target} has no training record — no row can be called held out from it; retrain it")
    either = frozenset(cand_keys) | frozenset(served_keys)

    window: dict[str, Any] | None = None
    if prospective:
        cand_cut, cand_done, window = prospective_cuts(base, suite, cand_target, Path(cand_path).name)
    else:  # a registration clears the swept cut, so the candidate serves at the code's cuts
        cand_cut, cand_done = (suite.threshold, "code"), (suite.done_threshold, "code")
    if champion_cuts is not None:
        served_cut = (champion_cuts["at"], champion_cuts["at_source"])
        served_done = (champion_cuts["done_at"], champion_cuts["done_source"])
    else:
        served_cut = served_threshold(base, suite.surface, served_target, code=suite.threshold)
        served_done = served_done_threshold(base, suite.surface, served_target, code=suite.done_threshold)

    router = (
        router if router is not None else infer.open_router(base, device, {cand_target: cand_path, served_target: served_path})
    )
    voters = [
        Voter(f"laya:{cand_target}", "laya", cand_target, Timed(router), cand_cut[0], cand_cut[1]),
        Voter(f"laya:{served_target}", "laya", served_target, Timed(router), served_cut[0], served_cut[1]),
    ]

    public, private = load_suites(base, name)
    counted, provisional = counted_public(base, suite.surface, public)
    golden_data = [(case["id"], suite.build_state(case["state"]), case["expected"]) for case in counted]
    contaminated, private_data = [], []
    for case in private:
        state = suite.build_state(case["state"])
        key = case.get("key") if isinstance(case.get("key"), str) else rows.state_key(case["state"]["hand_off"])
        if was_trained(key, state, either) or rows.state_key(case["state"]["hand_off"]) in either:
            contaminated.append(case["id"])
        else:
            private_data.append((case["id"], state, case["expected"]))
    held_data, held_meta = untrained_rows(base, name, suite, labels, either)

    sets: dict[str, dict[str, Any]] = {"golden": score_set("golden", golden_data, voters, suite)}
    if private_data:
        sets["private"] = score_set("private", private_data, voters, suite)
    if held_data:
        sets["heldout"] = score_set("heldout", held_data, voters, suite)

    def block(set_name: str, data: list[tuple[str, Any, bool]], **extra: Any) -> dict[str, Any] | None:
        if not data:
            return None
        return paired_block(
            [row_id for row_id, _s, _e in data],
            [e for _i, _s, e in data],
            voters[0].probabilities[set_name],
            voters[1].probabilities[set_name],
            **extra,
        )

    notes: dict[str, str | None] = {
        "heldout": held_meta.get("skipped") or (None if held_data else "no clean labelled rows"),
        "floor": None,
    }
    floor_block = None
    if held_data and not prospective:
        texts, labels_open = floor_voter.checkpoint_rows(base, Path(cand_path).name)
        try:
            fitted = floor_voter.fit(texts, labels_open, suite.false_warning_cost)
        except ValueError as error:
            notes["floor"] = str(error)
        else:
            p = infer.predict_noul(
                floor_voter.FloorRouter(fitted), floor_voter.NAME, [s for _k, s, _e in held_data], suite.question
            )
            floor_block = {"p": p, "cut": fitted.cut, "rows": fitted.rows, "open_rows": fitted.open_rows}

    evidence = {
        "suite": name,
        "surface": suite.surface,
        "question_version": suite.question_version,
        "costs": costs_of(suite),
        "candidate": {
            "name": cand_target,
            "checkpoint": Path(cand_path).name,
            "at": cand_cut[0],
            "at_source": cand_cut[1],
            "done_at": cand_done[0],
            "done_source": cand_done[1],
            "stamp": infer.checkpoint_stamp(base, router, cand_target, cand_path),
        },
        "served": {
            "name": served_target,
            "checkpoint": Path(served_path).name if served_path else None,
            "at": served_cut[0],
            "at_source": served_cut[1],
            "done_at": served_done[0],
            "done_source": served_done[1],
            "cuts_from": champion_cuts["from"] if champion_cuts is not None else "registry",
            "stamp": infer.checkpoint_stamp(base, router, served_target, served_path),
        },
        "golden": block("golden", golden_data, provisional=provisional),
        "private": block("private", private_data, contaminated=contaminated) or {"contaminated": contaminated},
        "heldout": block("heldout", held_data, **{k: v for k, v in held_meta.items() if k != "skipped"}),
        "floor": floor_block,
        "notes": notes,
        "sets": sets,
    }
    if prospective:
        evidence["prospective"] = prospective_rows(base, suite, voters, either, window or {})
    return evidence


def registered_at(base: Path, surface: str, checkpoint: str) -> str | None:
    """When `checkpoint` was registered (`trained.at` in models.json), while it is the one registered."""
    entry = registry_view(base)["models"].get(fine_tune_name(surface))
    if entry is None or entry["checkpoint"] != checkpoint or not entry["trained"]:
        return None
    return entry["trained"]["at"]


def prospective_rows(
    base: Path, suite: Suite, voters: list[Voter], trained: frozenset[str], window: dict[str, Any]
) -> dict[str, Any]:
    """Operator labels that arrived after `window["since"]` (`prospective_cuts`), on texts neither the served
    fine-tune nor the champion trained on, scored by both (`voters`: candidate, champion) at their live cuts."""
    since, opens = window.get("since"), _instant(window.get("since"))
    if opens is None:
        return {
            "since": None,
            "window": window,
            "expected": [],
            "note": "the served fine-tune is not the registered checkpoint, or its registration carries no time",
        }
    fold = require_sources(base, suite.surface)  # an unreadable operator log stops the run, never reads as empty
    found_texts = review_data.texts(base, suite.surface)
    golden = review_data.golden_keys(base, suite.name)
    data = []
    for key, row in sorted(fold.labels.items()):
        at = _instant(row.get("at"))
        if at is None or at <= opens or key.startswith("golden:") or key in golden or key not in found_texts:
            continue
        state = suite.build_state({"hand_off": found_texts[key]})
        if not was_trained(key, state, trained):
            data.append((key, state, bool(row["open"])))
    if not data:
        return {"since": since, "window": window, "expected": []}
    candidate = infer.predict_noul(voters[0].router, voters[0].model, [s for _k, s, _e in data], suite.question)
    champion = infer.predict_noul(voters[1].router, voters[1].model, [s for _k, s, _e in data], suite.question)
    return {
        "since": since,
        "window": window,
        "keys": [k for k, _s, _e in data],
        "expected": [e for _k, _s, e in data],
        "candidate": candidate,
        "champion": champion,
    }


# ── the command ───────────────────────────────────────────────────────────────


def newest_verdict_for(base: Path, suite: str, checkpoint: str) -> dict[str, Any] | None:
    """The newest promotion (not prospective) gate report whose candidate was `checkpoint`: who it replaced."""
    for _path, report in lab_files.gate_reports(base, suite):
        if report.get("mode") == "promotion" and (report.get("candidate") or {}).get("checkpoint") == checkpoint:
            return report
    return None


def judge(
    name: str,
    base: Path,
    *,
    candidate: str | None = None,
    served: str = "served",
    labels: str = "merged",
    prospective: bool = False,
    device: str = "mps",
    router: Any = None,
    fixture: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Gather, judge and write the report (and, for a promotion, its `trimmed_evidence` to `fixture`). Returns
    `(report, path)`; GateRefused when no judgement is possible."""
    suite = SUITES[name]
    champion_cuts = None
    if prospective:
        if fixture is not None:
            raise GateRefused("--fixture trims a promotion verdict; a prospective one has no held-out evidence")
        target, path = resolve_model(base, suite.surface, "served")
        if path is None:
            raise GateRefused("no fine-tune is served: the prospective test follows a registration")
        earlier = newest_verdict_for(base, name, path.name)
        if served == "served":
            if earlier is None:
                raise GateRefused(f"no promotion verdict names {path.name}, so its champion is unknown: pass --served <champion>")
            served = earlier["served"]["checkpoint"] or earlier["served"]["name"]
        if earlier is not None and served in (earlier["served"].get("checkpoint"), earlier["served"].get("name")):
            champion_cuts = verdict_cuts(earlier)
        candidate = target
    evidence = gather(
        name,
        base,
        candidate=candidate,
        served=served,
        labels=labels,
        device=device,
        router=router,
        prospective=prospective,
        champion_cuts=champion_cuts,
    )
    if prospective:
        block = evidence.pop("prospective")
        if block and block.get("expected"):
            block = {
                **block,
                "candidate_cuts": {"at": evidence["candidate"]["at"], "done_at": evidence["candidate"]["done_at"]},
                "champion_cuts": {"at": evidence["served"]["at"], "done_at": evidence["served"]["done_at"]},
            }
        result = prospective_check(block if block and block.get("expected") else None, evidence["costs"])
        if block and block.get("note"):
            result["why"] = f"{block['note']}: nothing to test"
        verdict = {
            "verdict": {PASS: "PASS", FAIL: "FAIL", PENDING: "INCONCLUSIVE"}[result["state"]],
            "why": result["why"],
            "prospective": result,
            "since": (block or {}).get("since"),
            "window": (block or {}).get("window"),
            "keys": (block or {}).get("keys", []),
        }
    else:
        verdict = promotion_verdict(evidence)
        verdict["prospective"] = prospective_check(None, evidence["costs"])
    # the per-row p already rides in `evidence`; the sets keep each voter's AUC, Brier, ECE and live result
    sets = {set_name: {k: v for k, v in block.items() if k != "p"} for set_name, block in evidence.pop("sets").items()}
    run_id = secrets.token_hex(6)
    report = {
        "kind": KIND,
        "mode": "prospective" if prospective else "promotion",
        "suite": name,
        "at": rows.utc_stamp(),
        "run_id": run_id,
        "laya_version": laya_version(),
        "device": device,
        "dataset_version": dataset_version(base, name, by="gate")["id"],
        **verdict,
        "candidate": evidence["candidate"],
        "served": evidence["served"],
        "evidence": evidence,
        "sets": sets,
        **lab_files.eval_run_field(),
    }
    out = base / EVALS_DIRNAME / lab_files.gate_report_name(name, lab_files.report_stamp(), run_id)
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
    if fixture is not None:
        atomic_write_text(
            Path(fixture), json.dumps(trimmed_evidence(report), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        )
    return report, out


def _ordinal_block(block: dict[str, Any] | None, prefix: str) -> dict[str, Any] | None:
    if not block or not block.get("expected"):
        return None
    keep = {k: v for k, v in block.items() if k in ("expected", "candidate", "served", "labels", "excluded")}
    return {"ids": [f"{prefix}{i}" for i in range(len(block["expected"]))], **keep}


def trimmed_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """A promotion report's evidence as a checked-in fixture: what `promotion_verdict` reads, plus the verdict it
    gave. Numbers only: public case ids stay (they are the repo's own), held-out and private rows become ordinals
    and the floor keeps its p and cut, so no key of a private text leaves this machine."""
    if report.get("mode") != "promotion":
        raise ValueError("trimmed_evidence: only a promotion verdict carries held-out evidence")
    evidence = report["evidence"]
    side_keys = ("name", "checkpoint", "at", "at_source", "done_at", "done_source")
    private = evidence.get("private") or {}
    trimmed_private = _ordinal_block(private, "p") or {}
    trimmed_private["contaminated"] = [f"c{i}" for i in range(len(private.get("contaminated") or []))]
    floor = evidence.get("floor")
    return {
        "kind": "gate-fixture",
        "suite": report["suite"],
        "from_run": report["run_id"],
        "at": report["at"],
        "costs": evidence["costs"],
        "candidate": {k: evidence["candidate"].get(k) for k in side_keys},
        "served": {k: evidence["served"].get(k) for k in side_keys},
        "golden": evidence.get("golden"),
        "heldout": _ordinal_block(evidence.get("heldout"), "h"),
        "private": trimmed_private,
        "floor": {k: floor[k] for k in ("p", "cut", "rows", "open_rows")} if floor else None,
        "notes": evidence.get("notes") or {},
        "recorded": {
            "verdict": report["verdict"],
            "failed": report.get("failed", []),
            "unmeasured": report.get("unmeasured", []),
            "states": {name: check["state"] for name, check in (report.get("checks") or {}).items()},
        },
    }


def run(
    name: str,
    base: Path,
    *,
    candidate: str | None = None,
    served: str = "served",
    labels: str = "merged",
    prospective: bool = False,
    device: str = "mps",
    router: Any = None,
    fixture: Path | None = None,
) -> int:
    """`laya-lab gate`: 0 a verdict was written (PASS or not: read it) · 3 no judgement possible · 1 an error."""
    try:
        report, out = judge(
            name,
            base,
            candidate=candidate,
            served=served,
            labels=labels,
            prospective=prospective,
            device=device,
            router=router,
            fixture=fixture,
        )
    except GateRefused as refusal:
        print(f"[laya-lab] gate refused: {refusal}")
        return REFUSED
    except ValueError as error:  # a voter that could not answer, a malformed suite
        print(f"[laya-lab] gate: {error}")
        return 1
    for line in report_lines(report):
        print(line)
    print(f"  report → {out}")
    if fixture is not None:
        print(f"  fixture → {fixture}")
    return 0


def report_lines(report: dict[str, Any]) -> list[str]:
    """Keys and numbers only: a closing text never reaches the terminal from here."""
    cand, champ = report["candidate"], report["served"]
    lines = [
        f"[laya-lab] gate {report['suite']} · {report['mode']} · {cand['checkpoint'] or cand['name']} "
        f"(⚠️ {cand['at']} {cand['at_source']}, ✅ {cand['done_at']}) vs {champ['checkpoint'] or champ['name']} "
        f"(⚠️ {champ['at']} {champ['at_source']}, ✅ {champ['done_at']}) → {report['verdict']}"
    ]
    for check_name, check in (report.get("checks") or {}).items():
        p = f"  p {check['p']:.3f}" if isinstance(check.get("p"), float) else ""
        lines.append(f"  {check_name:<15} {check['state']:<10}{p}  {check['why']}")
    prospective = report.get("prospective") or {}
    if prospective:
        steps = " · ".join(f"{step['cut']} p {step['p']:.3f} vs {step['level']:g}" for step in prospective.get("steps") or [])
        lines.append(f"  {'prospective':<15} {prospective['state']:<10}  {prospective['why']}{f' ({steps})' if steps else ''}")
    window = report.get("window") or {}
    if window:
        lines.append(
            f"  {'window':<15} since {window.get('since')} (opens at {window.get('opens_at')})"
            + (f" · {window['note']}" if window.get("note") else "")
        )
    if (champ.get("cuts_from") or "registry") != "registry":
        lines.append(f"  {'champion cuts':<15} from {champ['cuts_from']}")
    lines.append(f"  → {report['why']}")
    return lines
