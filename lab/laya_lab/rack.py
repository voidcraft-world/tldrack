"""`laya-lab rack`: what the rack's live use looked like, as the counts a bench results file carries under `rack`.

    laya-lab rack                      # the last DEFAULT_DAYS days, printed
    laya-lab rack --days 14
    laya-lab rack --write              # fold them into today's bench/results/<date>.json (the bench run's own file)

`laya-lab bench --rack` measures the same thing itself and writes it beside the voters; `--write` is for a results
file a run already wrote, so a rack number never costs a second pass over the models. Both are opt-in: a results file
is published, and these counts are the operator's own use of the rack. Shape: owned by
`schemas/src/bench-results.ts` (`benchRackSchema`) — see the note there.

Three stores, one window (`since` → now). Each is read, never written:

- **The decision ledger** (`laya_bridge.ledger`, the surface's file): `judged` is every answered decision in the window,
  each banded by the cuts IT was served with (the row's `threshold` and `done_threshold`, the code's
  `OPEN_WORK_AT`/`DONE_AT` for a row that predates them): `open` at p ≥ the cut, `done` under the done cut, `reply`
  between. `latency` is the rows' `ms`, the plugin's wall time around the answer.
- **The rack's metrics** (`tldrack.rack_metrics`, SQLite, opened `mode=ro`): `reached` is the `warning_new` signals,
  the ⚠️ the rack actually raised. It is at most `open`: the rack holds a repeat, a snoozed session and a background
  turn back, so the gap between the two is what the rack's own holding saved.
- **The next-step log** (`tldrack.next_step`): `suggested` is the suggestions with an action, `taken` the ones the
  operator took, by [Do it] or by hand (`next_step.summarize`'s `took`). None when the log holds none in the window.

A store that is missing leaves its part None where the schema allows it (`latency`, `next_step`) and makes the whole
rack None where it does not (no decisions, no metrics): the reason is printed, never written. Counts only: no
session name, state or text leaves this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab.golden import SUITES, Suite
from laya_lab.latency import spread
from laya_bridge import ledger, paths, rows
from laya_bridge.files import is_number
from tldrack import next_step, open_work, rack_metrics

DEFAULT_DAYS = 7
#: The signal the rack records when a ⚠️ is raised for the first time on a turn (`rack_metrics.INTERRUPTIONS`).
REACHED_SIGNAL = "warning_new"
VERDICTS = ("done", "reply", "open")


def band(p: float, threshold: Any, done_threshold: Any) -> str:
    """One verdict, at the cuts the decision was served with (the module note owns the fallback)."""
    open_at = threshold if is_number(threshold) else open_work.OPEN_WORK_AT
    done_at = done_threshold if is_number(done_threshold) else open_work.DONE_AT
    if p >= open_at:
        return "open"
    return "done" if p < done_at else "reply"


def decision_p(row: Mapping[str, Any]) -> float | None:
    """p of the decision's first answer, the one the rack asks (`bench.QUESTION_NAME`)."""
    answers = row.get("decisions")
    first = answers[0] if isinstance(answers, list) and answers and isinstance(answers[0], Mapping) else None
    p = first.get("p") if first is not None else None
    return float(p) if is_number(p) and 0 <= p <= 1 else None


def judge(decisions: Iterable[Mapping[str, Any]]) -> tuple[dict[str, int], list[int]]:
    """`({done, reply, open}, ms of each)` over decision rows; a row without a readable p is not a judgement."""
    verdicts = dict.fromkeys(VERDICTS, 0)
    timings: list[int] = []
    for row in decisions:
        p = decision_p(row)
        if p is None:
            continue
        verdicts[band(p, row.get("threshold"), row.get("done_threshold"))] += 1
        ms = row.get("ms")
        if is_number(ms) and ms >= 0:
            timings.append(int(ms))
    return verdicts, timings


def read_reached(path: Path, since_ms: int, until_ms: int) -> int | None:
    """The ⚠️ the rack raised in the window, or None when the store cannot be read. Read-only: the bridge writes it live."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        found = db.execute(
            "SELECT COALESCE(SUM(count), 0) FROM signals WHERE signal = ? AND at >= ? AND at <= ?",
            (REACHED_SIGNAL, since_ms, until_ms),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return int(found[0]) if found else None


def read_next_step(path: Path, since_s: float, until_s: float) -> dict[str, int] | None:
    rows_in = [row for row in next_step.NextStepLog(path).read(since=since_s) if (row.get("ts") or 0) <= until_s]
    summary = next_step.summarize(rows_in)
    if not summary["suggested"]:
        return None
    return {"suggested": summary["suggested"], "taken": summary["took"]}


def measure(
    base: Path,
    suite: Suite,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    metrics_path: Path | None = None,
    next_step_path: Path | None = None,
) -> tuple[dict | None, str | None]:
    """`(rack, None)`, or `(None, why)` when the window cannot be told. The module note owns which store says what."""
    if days < 1:
        raise ValueError("days: at least 1")
    until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = until - timedelta(days=days)
    stamp_since, stamp_until = rows.utc_stamp(since), rows.utc_stamp(until)
    decided = (
        row
        for row in ledger.iter_rows(base, suite.surface, since=stamp_since)
        if row["kind"] == "decision" and row["at"] <= stamp_until
    )
    verdicts, timings = judge(decided)
    judged = sum(verdicts.values())
    if not judged:
        return None, f"the decision ledger holds no {suite.surface} decision in the last {days} days"
    reached = read_reached(
        metrics_path or rack_metrics.resolve_metrics_path(), int(since.timestamp() * 1000), int(until.timestamp() * 1000)
    )
    if reached is None:
        return None, "the rack's metrics store cannot be read"
    if reached > verdicts["open"]:
        return None, f"the rack raised {reached} ⚠️ against {verdicts['open']} open verdicts: the stores disagree"
    timed = spread(timings)
    local = until.astimezone()
    return {
        "from": (local - timedelta(days=days)).date().isoformat(),
        "to": local.date().isoformat(),
        "days": days,
        "judged": judged,
        "verdicts": verdicts,
        "reached": reached,
        "latency": {"n": timed["n"], "p50_ms": timed["p50"], "p90_ms": timed["p90"]} if timed["n"] else None,
        "next_step": read_next_step(next_step_path or next_step.resolve_log_path(), since.timestamp(), until.timestamp()),
    }, None


def lines(rack: Mapping[str, Any]) -> list[str]:
    judged, verdicts = rack["judged"], rack["verdicts"]
    share = " · ".join(f"{name} {verdicts[name]} ({verdicts[name] / judged:.0%})" for name in VERDICTS)
    out = [
        f"  rack    {rack['from']} → {rack['to']} ({rack['days']} d) · {judged} judged · {share}",
        f"           ⚠️ reached {rack['reached']} ({rack['reached'] / rack['days']:.1f} / day)",
    ]
    if rack["latency"]:
        out.append(
            f"           live ms p50 {rack['latency']['p50_ms']} · p90 {rack['latency']['p90_ms']} (n {rack['latency']['n']})"
        )
    taken = rack["next_step"]
    out.append(
        "           next step: none suggested"
        if taken is None
        else f"           next step taken {taken['taken']} / {taken['suggested']} ({taken['taken'] / taken['suggested']:.0%})"
    )
    return out


def fold_into(path: Path, rack: Mapping[str, Any]) -> None:
    """Set `rack` in a results file a bench run wrote, whole or not at all (its every other key is kept)."""
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{path.name}: no results file to fold into (run `laya-lab bench` first)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Legacy (step 6.8): the block's name in a file written before 0.1.0.
    if isinstance(payload, dict) and "fleet" in payload and "rack" not in payload:
        payload["rack"] = payload.pop("fleet")
    if not isinstance(payload, dict) or "rack" not in payload or payload.get("fixture"):
        raise ValueError(f"{path.name} is not a bench results file a run wrote")
    payload["rack"] = dict(rack)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, allow_nan=False, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def cmd(
    suite_name: str,
    base: Path,
    *,
    days: int = DEFAULT_DAYS,
    write: bool = False,
    day: str | None = None,
    out_dir: Path | None = None,
) -> int:
    """Exit 0 measured (and folded) · 1 not measured, or nothing to fold into."""
    rack, why = measure(base, SUITES[suite_name], days=days)
    if rack is None:
        print(f"[laya-lab] rack: {why}")
        return 1
    for line in lines(rack):
        print(line)
    if write:
        from laya_lab.bench import RESULTS_DIR

        target = (out_dir or paths.REPO_ROOT.joinpath(*RESULTS_DIR)) / f"{day or date.today().isoformat()}.json"
        try:
            fold_into(target, rack)
        except (OSError, ValueError) as error:
            print(f"[laya-lab] rack: {error}")
            return 1
        print(f"  rack → {'/'.join((*RESULTS_DIR, target.name))}")
    return 0
