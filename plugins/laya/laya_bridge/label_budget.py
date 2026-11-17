"""THE operator-label budget: which of the operator's labels feeds what.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

Every Laya lane draws on the same few operator labels a day, and each lane spends a label differently:
- a retrain trains on it, and a text any checkpoint trained on is never a clean row again
  (`review_data.clean_label_rows`);
- a golden PROMOTE takes it out of training and out of every sweep;
- RE-SWEEP, the held-out compare, the promotion gate and the gated retrain need labels nothing trained on.
With no split, a retrain takes most of the operator's labels out of the sweep pool, and registering a checkpoint
clears its swept cut. So every new checkpoint would serve at the code's cut until the pool was graded back from zero.

THE RULE. An operator-labelled text key falls in one HALF, by a stable hash of the key (`drawn`):
- RESERVE (`RESERVE_PERCENT`, 1 in 2) is never a training row (`open_work_data.build`, checked again at the trainer's
  door, `train.withhold_reserve`) and never a golden case (`review_data.promotion_candidates`, `promote.refusals`).
  It is a clean row through every retrain and every PROMOTE, so the pool a sweep reads only grows.
- TRAIN: the operator's answer overrides Sonnet's in the next training set, and it is the only source of a golden
  PROMOTE.
Any door grades into the same halves: the deck, RECENT, the rack's ⚠️ menu.
A long hand-off has a second key, and the reserve holds its text out under both: owned by laya-lab
`open_work_data.judged_key` — see the note there.

SPENT. A key drawn RESERVE that some checkpoint already trained on (a Sonnet label on it trained before the operator
graded it, or it was graded before this rule) can never be a clean row again. It is TRAIN: holding it back would cost a
operator correction and buy no sweep row. This is the one way a key changes half, and it only goes that way. A key held
in the reserve is in no training record, so it never becomes spent.

WHAT A SWEEP READS. Every clean row of either half: a label nothing trained on is held out, whichever half it is in.
The TRAIN half's rows are clean only until the next retrain takes them; the reserve's stay. `review_data.sweep_readiness`
counts both and says how many are the reserve, which is what a new checkpoint starts with. This is also why this rule
replaced PROMOTE's keep-ten-per-class guard: a PROMOTE takes only rows the next retrain was going to take.

Changing `RESERVE_PERCENT` or `SALT` re-deals every key: reserve keys dealt to TRAIN are trained on at the next retrain
and cannot come back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laya_bridge import review_log, rows
from laya_bridge.review_log import OperatorFold
from laya_bridge.review_surfaces import REVIEW_SURFACES

RESERVE, TRAIN = "reserve", "train"
HALVES = (RESERVE, TRAIN)
#: D13: 1 in 2. The share of operator-labelled keys held back from training and from golden.
RESERVE_PERCENT = 50
SALT = "label-budget"


def drawn(key: str) -> str:
    """The half a key's hash deals it, before any training record is read (`Budget.half` is the rule)."""
    return RESERVE if rows.bucket(key, SALT) < RESERVE_PERCENT else TRAIN


@dataclass(frozen=True)
class Budget:
    """One surface's budget at one moment: the keys the operator has a live label on (never a `golden:` key), and
    the keys any checkpoint of its fine-tune trained on (`rows.trained_keys`)."""

    operator: frozenset[str]
    trained: frozenset[str]

    def half(self, key: str) -> str | None:
        """RESERVE or TRAIN for an operator-labelled key; None for any other (only the operator's labels are budgeted)."""
        if key not in self.operator:
            return None
        return RESERVE if drawn(key) == RESERVE and key not in self.trained else TRAIN

    def is_reserve(self, key: str) -> bool:
        return self.half(key) == RESERVE

    def promotable(self, key: str) -> bool:
        return self.half(key) == TRAIN

    @property
    def reserve(self) -> frozenset[str]:
        return frozenset(key for key in self.operator if self.is_reserve(key))

    def withhold(
        self, train_rows: Iterable[Mapping[str, Any]], held: frozenset[str] | None = None
    ) -> tuple[list[Mapping[str, Any]], int]:
        """(the rows that may train, how many were held back): a row is held by its label `key`, or by the key of its
        state (a row written before train rows carried their key). `held` is the reserve unless the caller widened
        it: laya-lab adds each reserve text's judged-state key (`open_work_data.judged_key` owns why)."""
        held_keys = self.reserve if held is None else held
        kept: list[Mapping[str, Any]] = []
        count = 0
        for row in train_rows:
            if row.get("key") in held_keys or _state_key(row.get("state")) in held_keys:
                count += 1
            else:
                kept.append(row)
        return kept, count


def _state_key(state: object) -> str | None:
    if state is None:
        return None
    try:
        return rows.state_key(state)
    except ValueError:  # NaN in a state: no key, so no operator label names it
        return None


def budget_of(operator_keys: Iterable[str], trained: Iterable[str]) -> Budget:
    """Pure: the budget over these operator-labelled keys and this training record."""
    return Budget(frozenset(key for key in operator_keys if not key.startswith("golden:")), frozenset(trained))


def read(base: Path, surface: str, fold: OperatorFold | None = None) -> Budget:
    """The surface's budget now. `fold` is the caller's one read of the operator log; laya-lab always passes the one
    `open_work_data.require_sources` refused or returned, so a log it cannot read is never read here as "no labels"
    (which would train on the whole reserve)."""
    current = fold if fold is not None else review_log.read_fold(base, surface)[1]
    trained, _without = rows.trained_keys(base, REVIEW_SURFACES[surface].fine_tune)
    return budget_of(current.labels, trained)


def counts(keys: Iterable[str], labels: Mapping[str, bool]) -> dict[str, int]:
    """`{rows, open, done}` over `keys`, by each key's label: the shape `sweep_readiness` reports a pool in."""
    found = [bool(labels[key]) for key in keys]
    return {"rows": len(found), "open": sum(found), "done": len(found) - sum(found)}
