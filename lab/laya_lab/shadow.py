"""`laya-lab shadow`: each @challenger's p on the live states the ledger recorded, beside the p they were served at.

    laya-lab shadow agent-open-work                 # every @challenger, on the decisions it has not scored yet
    laya-lab shadow agent-open-work --since 2001-02-03T00:00:00Z

SageMaker's shadow test, as a nightly batch: a live verdict is already slow enough, so a challenger never answers a
request. Instead the nightly pack (`nightly.py`) re-asks each @challenger, on this machine's GPU, every
`agent_open_work` decision the ledger kept since the challenger was registered (at most LOOKBACK_DAYS back), with the
SAME state and question that was served. The day's live turn ends are the live distribution, which a held-out split of
a few dozen rows is not.

Each answer is one `lab_files.shadow_row` in `<data>/laya/shadow/<surface>.jsonl`: `challenger_p` beside
`served_p`, with the cuts each side would draw at (the decision's own recorded cuts; the challenger's swept cut, else
the code's). Keyed by (decision_id, challenger, digest, device), so a re-run scores only what is new and a re-trained
folder or a CPU run never reuses an MPS p. The REVIEW deck's `challenger` stratum reads it, and the rack's LAYA shadow
line counts days and agreement from it (`laya_bridge/ops.py`), each by challenger name.

The file is months of evidence and a re-run re-scores only the last LOOKBACK_DAYS, so it is only ever rewritten from
a read that succeeded:
- A version's rows leave only when the armed REJECT deletes that version (`registry.prune_rejects` →
  `drop_versions`). The plugin's registry read is forgiving: an unparseable, oversized or unknown models.json, or a
  checkpoint folder missing for a moment, reads as no version at all, and garbage-collecting from that read would
  empty the file. A row of a version that is gone is harmless: every reader selects by challenger.
- A shadow file the reader refuses (too large, a link) is left as it is and the pass fails: rewriting it from that
  empty read would keep only tonight's rows.
- The file keeps the newest rows that fit in SHADOW_WRITE_BYTES, a quarter under the reader's cap
  (`lab_files.SHADOW_CAP_BYTES`), so it never grows into a file its own reader refuses. Rewritten whole, atomically.

A decision is left out, and counted, when it cannot be re-asked exactly: its text was cut by the ledger's 8,000
character cap, it carries no text, the question changed since (its hash or `question_version`), the served answer
abstained, or it recorded no cut. Keys and numbers only: no text leaves the ledger.

Exit codes: 0 scored (or nothing to score) · 1 an error (models.json or the shadow file unreadable: nothing written).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import infer
from laya_lab.data import atomic_write_text, jsonl_line
from laya_lab.golden import SUITES, Suite
from laya_lab.registry import CHALLENGER, alias_target, registry_versions, served_done_threshold, served_threshold
from laya_bridge import files, lab_files, ledger, paths, rows
from laya_bridge.files import is_p

#: A challenger older than this is shadow-scored on its last LOOKBACK_DAYS only (the ledger's read window is ~32 MB).
LOOKBACK_DAYS = 14
#: What the file may hold (the module note owns why): about 18,000 rows at ~650 bytes each.
SHADOW_WRITE_BYTES = lab_files.SHADOW_CAP_BYTES * 3 // 4
SKIPS = ("truncated", "no_text", "question_changed", "abstained", "no_cut")


@dataclass(frozen=True)
class LiveState:
    """One ledger decision as it was served: the exact state, its p, and the cuts the rack drew it at."""

    decision_id: str
    key: str
    at: str
    state: str
    question_version: int
    served_model: str
    served_checkpoint: str | None
    served_p: float
    served_threshold: float
    served_done_threshold: float | None


def _answer(decision: dict[str, Any], qh: str) -> dict[str, Any] | None:
    for answer in decision.get("decisions") or []:
        if isinstance(answer, dict) and answer.get("q") == "q":
            return answer if answer.get("qh") == qh else None
    return None


def live_states(base: Path, suite: Suite, since: str | None) -> tuple[list[LiveState], Counter]:
    """The ledger's decisions on `suite.surface` since `since`, re-askable exactly, oldest first; and what was left out
    (by SKIPS reason). The newest row of a decision wins."""
    qh = ledger.question_hash(suite.question)
    skipped: Counter = Counter()
    out: list[LiveState] = []
    for entry in ledger.fold(ledger.iter_rows(base, suite.surface, since=since)).values():
        decision = entry["decision"]
        if decision is None or decision.get("surface") != suite.surface:
            continue
        state = decision.get("state")
        if decision.get("state_truncated"):
            skipped["truncated"] += 1
            continue
        if not isinstance(state, str) or not state.strip():
            skipped["no_text"] += 1
            continue
        version = decision.get("question_version")
        answer = _answer(decision, qh)
        if answer is None or (version is not None and version != suite.question_version):
            skipped["question_changed"] += 1
            continue
        if answer.get("abstain") or not is_p(answer.get("p")):
            skipped["abstained"] += 1
            continue
        threshold, done = decision.get("threshold"), decision.get("done_threshold")
        if not is_p(threshold):
            skipped["no_cut"] += 1
            continue
        relative = decision.get("checkpoint_path")
        out.append(
            LiveState(
                decision_id=decision["decision_id"],
                key=rows.state_key(state),
                at=decision["at"],
                state=state,
                question_version=suite.question_version,
                served_model=decision["model"],
                served_checkpoint=Path(relative).name if isinstance(relative, str) and relative else None,
                served_p=float(answer["p"]),
                served_threshold=float(threshold),
                served_done_threshold=float(done) if is_p(done) else None,
            )
        )
    out.sort(key=lambda live: (live.at, live.decision_id))
    return out, skipped


def challengers(base: Path, surface: str) -> dict[str, Path]:
    """`{version: folder}` of every @challenger of `surface` (one alias today; a dict so a second is no rewrite)."""
    target = alias_target(base, surface, CHALLENGER)
    return {target[0]: target[1]} if target is not None else {}


def window_start(base: Path, challenger: str, now: datetime) -> str:
    """Where a challenger's shadow starts: its registration, at most LOOKBACK_DAYS back."""
    floor = rows.utc_stamp(now - timedelta(days=LOOKBACK_DAYS))
    registered = registry_versions(base)["versions"].get(challenger, {}).get("registered_at")
    return max(registered, floor) if isinstance(registered, str) else floor


def _order(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["at"], row["decision_id"], row["challenger"]


def _write_shadow(base: Path, surface: str, rows_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite `shadow/<surface>.jsonl` whole: oldest first, the newest rows that fit in SHADOW_WRITE_BYTES. Returns
    the rows written."""
    ordered = sorted(rows_out, key=_order)
    lines = [jsonl_line(row) for row in ordered]
    total, start = 0, len(lines)
    while start > 0 and total + len(lines[start - 1].encode("utf-8")) <= SHADOW_WRITE_BYTES:
        start -= 1
        total += len(lines[start].encode("utf-8"))
    atomic_write_text(paths.shadow_path(base, surface), "".join(lines[start:]))
    return ordered[start:]


def drop_versions(base: Path, surface: str, names: set[str]) -> dict[str, int]:
    """Remove the rows of versions that were DELETED (`registry.prune_rejects` is the one caller): `{name: rows
    dropped}`. A file that is missing or that the reader refuses is left as it is."""
    held, code = lab_files.read_shadow(base, surface)
    if code != files.OK:
        return {}
    dropped = Counter(row["challenger"] for row in held if row["challenger"] in names)
    if dropped:
        _write_shadow(base, surface, [row for row in held if row["challenger"] not in names])
    return dict(dropped)


def _unreadable(base: Path, surface: str) -> str | None:
    """Why this pass must not write tonight, or None: a models.json the plugin cannot read (so no @challenger can be
    told apart from an unreadable registry), or a shadow file its reader refuses."""
    if (base / paths.MODELS_FILENAME).exists() and registry_versions(base)["version"] is None:
        return f"{paths.MODELS_FILENAME} does not read the way the plugin reads it; the shadow file is left as it is"
    code = lab_files.read_shadow(base, surface)[1]
    if code not in (files.OK, files.MISSING):
        return f"the shadow file is {code}; it is left as it is"
    return None


def run(
    name: str, base: Path, *, device: str = "mps", router: Any = None, since: str | None = None, now: datetime | None = None
) -> tuple[int, dict[str, Any]]:
    """Score every @challenger on its unscored live states; `(exit code, summary)` (`summary` is what the nightly
    report keeps: per challenger, rows new and held, and the skip counts). Every row already in the file stays (the
    module note owns why)."""
    suite = SUITES[name]
    now = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"challengers": {}, "skipped": {}, "device": device}
    refused = _unreadable(base, suite.surface)
    if refused is not None:
        summary["note"] = refused
        return 1, summary
    targets = challengers(base, suite.surface)
    if not targets:
        summary["note"] = "no @challenger is registered"
        return 0, summary
    held, _code = lab_files.read_shadow(base, suite.surface)
    seen = {lab_files.shadow_key(row) for row in held}
    plan: dict[str, tuple[Path, str | None, list[LiveState]]] = {}
    skipped: Counter = Counter()
    for challenger, path in targets.items():
        digest = infer.checkpoint_digest(challenger, path)
        states, left_out = live_states(base, suite, since or window_start(base, challenger, now))
        skipped.update(left_out)
        todo = [live for live in states if (live.decision_id, challenger, digest or "", device) not in seen]
        plan[challenger] = (path, digest, todo)
    summary["skipped"] = {reason: skipped[reason] for reason in SKIPS if skipped[reason]}
    if any(todo for _path, _digest, todo in plan.values()):
        router = router if router is not None else infer.open_router(base, device, {c: p for c, (p, _d, _t) in plan.items()})
    scored_at = rows.utc_stamp(now)
    new_rows: list[dict[str, Any]] = []
    for challenger, (_path, digest, todo) in plan.items():
        cut, source = served_threshold(base, suite.surface, challenger, code=suite.threshold)
        done_cut, _done_source = served_done_threshold(base, suite.surface, challenger, code=suite.done_threshold)
        probabilities = infer.predict_noul(router, challenger, [live.state for live in todo], suite.question) if todo else []
        for live, p in zip(todo, probabilities):
            new_rows.append(
                lab_files.shadow_row(
                    decision_id=live.decision_id,
                    key=live.key,
                    at=live.at,
                    question_version=live.question_version,
                    served_model=live.served_model,
                    served_checkpoint=live.served_checkpoint,
                    served_p=live.served_p,
                    served_threshold=live.served_threshold,
                    served_done_threshold=live.served_done_threshold,
                    challenger=challenger,
                    challenger_digest=digest,
                    device=device,
                    challenger_p=round(p, 4),
                    challenger_threshold=cut,
                    challenger_threshold_source=source,
                    challenger_done_threshold=done_cut,
                    scored_at=scored_at,
                )
            )
        summary["challengers"][challenger] = {
            "new": len(todo),
            "rows": 0,
            "digest": digest,
            "cut": {"at": cut, "source": source, "done_at": done_cut},
        }
    written = _write_shadow(base, suite.surface, [*held, *new_rows]) if new_rows else held
    for challenger, count in Counter(row["challenger"] for row in written).items():
        if challenger in summary["challengers"]:
            summary["challengers"][challenger]["rows"] = count
    return 0, summary


def cmd(name: str, base: Path, *, device: str, since: str | None) -> int:
    code, summary = run(name, base, device=device, since=since)
    if summary.get("note"):
        print(f"[laya-lab] shadow {name}: {summary['note']}")
    for challenger, block in summary["challengers"].items():
        print(
            f"[laya-lab] shadow {name} · @challenger {challenger} · cut {block['cut']['at']} {block['cut']['source']}: "
            f"+{block['new']} live states · {block['rows']} in the shadow file"
        )
    if summary["skipped"]:
        print("  left out: " + " · ".join(f"{reason} {n}" for reason, n in summary["skipped"].items()))
    print(f"  → {paths.shadow_path(base, SUITES[name].surface)}")
    return code
