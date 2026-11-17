"""`laya-lab promote`: the operator's vetted disagreements become PRIVATE golden cases.

    laya-lab promote agent-open-work --flips            # dry run: what would be added, and the balance
    laya-lab promote agent-open-work --flips --apply    # what AGENT RACK › LAYA › PROMOTE runs
    laya-lab promote agent-open-work --key 0123456789abcdef --apply

`--flips` takes `laya_bridge.review_data.promotion_candidates` — live operator flips with text that
are not golden yet, of the label budget's TRAIN half; the same rule counts the rack's `candidates` and gates the
bridge's `/lab`, so the three never disagree. `--key` names one operator-labelled text (an agree too), and a key
of the reserve is refused. Which half a label is in, and why the reserve is never promoted: owned by
`laya_bridge.label_budget` — see the note there.

Cases go to `<data>/laya/golden/<suite>.private.jsonl` (`paths.private_golden_path`),
0600 under a 0700 `golden/`, first line `lab_files.PRIVATE_GOLDEN_HEADER`, rows built by
`lab_files.private_golden_case`; this module never writes the checked-in `lab/golden/`.
Why it is named apart from the public suite: owned by lab/README.md — see "Golden suites".
`--apply` then records the grown suite as a new dataset version (`golden.record_dataset_version`, so every report
after it is stamped with it) and re-runs `build()`, so a promoted text leaves the training set (golden cases never train).

Exit codes are the bridge's contract (`laya_bridge/lab_jobs.py` runs this): 0 promoted (or a dry
run with something to promote) · 3 nothing to promote, or a key refused.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import open_work_data
from laya_lab.data import jsonl_line, open_private
from laya_lab.golden import SUITES, load_suites, record_dataset_version
from laya_bridge import files, label_budget, lab_files, paths, review_data, review_log, rows

REFUSED = 3


def balance(cases: Iterable[dict]) -> str:
    cases = list(cases)
    n_open = sum(1 for case in cases if case["expected"])
    return f"{n_open} open/{len(cases) - n_open} done"


def refusals(base: Path, name: str, keys: list[str], fold: review_log.OperatorFold) -> dict[str, str]:
    """`{key: why}` for every named key that cannot become a private case."""
    suite = SUITES[name]
    found = review_data.texts(base, suite.surface)
    golden = review_data.golden_keys(base, name)
    budget = label_budget.read(base, suite.surface, fold)
    out: dict[str, str] = {}
    for key in keys:
        if key.startswith("golden:"):
            out[key] = "already a public golden case"
        elif key not in fold.labels:
            out[key] = "not a live operator label (unanswered, skipped or undone)"
        elif key not in found:
            out[key] = "no text on this machine"
        elif key in golden or rows.state_key(found[key]) in golden:
            out[key] = "already golden (same text)"
        elif not budget.promotable(key):
            out[key] = "in the label budget's reserve (it feeds RE-SWEEP; never trained on, never promoted)"
    return out


def promote(base: Path, name: str, *, keys: Iterable[str] = (), flips: bool = False, apply: bool = False) -> int:
    suite = SUITES[name]
    fold = open_work_data.require_sources(base, suite.surface)
    wanted = list(dict.fromkeys(keys))
    refused = refusals(base, name, wanted, fold)
    if refused:
        for key, why in refused.items():
            print(f"[laya-lab] promote refused {key}: {why}")
        return REFUSED
    if flips:
        wanted = list(dict.fromkeys([*wanted, *review_data.promotion_candidates(base, name, fold)]))
        reserved = len(review_data.reserved_flips(base, name, fold))
        if reserved:
            print(f"[laya-lab] promote {name}: {reserved} flip(s) stay in the label budget's reserve")
    public, private = load_suites(base, name)
    if not wanted:
        print(f"[laya-lab] promote {name}: nothing to promote · public {balance(public)} · private {balance(private)}")
        return REFUSED

    found = review_data.texts(base, suite.surface)
    at = rows.utc_stamp()
    cases = []
    for key in wanted:
        row = fold.labels[key]
        cases.append(
            lab_files.private_golden_case(key, found[key], row["open"], row["against"]["open"], str(row["against"]["by"]), at)
        )
    for case in cases:
        verdict = "open" if case["expected"] else "done"
        print(
            f"[laya-lab]   {case['id']}  operator {verdict}  ({case['labels']['model_by']} said "
            f"{'open' if case['labels']['model'] else 'done'})"
        )
    after = [*private, *cases]
    print(f"[laya-lab] promote {name}: {len(cases)} case(s) · public {balance(public)} · private {balance(after)}")
    if not apply:
        print("[laya-lab] dry run: --apply writes them to the private suite (AGENT RACK › LAYA › PROMOTE)")
        return 0

    path = paths.private_golden_path(base, name)
    stamp = files.stamp_of(path)
    fresh = stamp is None or stamp[2] == 0
    with open_private(path, "a") as handle:
        if fresh:
            handle.write(lab_files.PRIVATE_GOLDEN_HEADER + "\n")
        for case in cases:
            handle.write(jsonl_line(case))
    print(f"[laya-lab] promoted → {path} · dataset version {record_dataset_version(base, name, 'promote')['at']}")
    # Golden cases never train: rebuild so the promoted texts leave the training set now.
    return open_work_data.build(base) if suite.surface == open_work_data.SURFACE else 0
