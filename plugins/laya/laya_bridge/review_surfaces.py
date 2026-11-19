"""The surfaces the REVIEW deck reviews, and everything the deck needs to know about each.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

One entry per surface. `stem` names its files under `data/` (`paths.data_file`), `golden_suite`
its golden suite (public in lab/golden/, private under `golden/`), `fine_tune` the name
laya-lab registers its checkpoint under (schemas/src/decide.ts `fine_tune_name`). `weights` is the
deck's round robin over the strata (`review_data.py` owns what each stratum holds).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Claude Code's own one-line notices for a turn an API error CUT OFF: the text of its synthetic
#: `isApiErrorMessage` entry (bridge/tldrack/transcript.py `fold_stop_failure` owns the
#: entry). No agent wrote them, so they are not a hand-off: no card, no label, no training row. laya-lab's
#: export drops such a turn by the entry's flag; these catch the texts it exported before it did, and any
#: kept in the ledger. Whole-text, one line, at most 300 characters (transcript.py
#: `STOP_FAILURE_MESSAGE_MAX_CHARS`).
CUT_OFF_NOTICES: tuple[str, ...] = (
    r"API Error: [^\n]{1,288}",
    r"You(?:'|’)ve hit your [^\n]{1,40} limit(?: [^\n]{0,200})?",
    r"Claude AI usage limit reached[^\n]{0,200}",
)


@dataclass(frozen=True)
class ReviewSurface:
    name: str
    label: str
    question: str
    question_type: str
    answers: tuple[str, ...]
    stem: str
    text_field: str
    golden_suite: str
    fine_tune: str
    default_threshold: float
    #: The agent rack's ✅ cut when a decision recorded none: copy of open_work.py DONE_AT.
    default_done_threshold: float
    weights: tuple[tuple[str, int], ...]
    question_version: int = 2  # pinned to open_work.QUESTION_VERSION by lab tests
    sample_percent: int = 50
    unflagged_percent: int = 20
    #: The share of live verdicts in `live_sample`, by `bucket(key, 'review-live')` (`review_data.py` owns why).
    live_percent: int = 10
    #: The second halves an answer may add, each on its own side of open (schemas/src/laya-review.ts `REVIEW_NEEDS`
    #: owns why); () = no second half.
    needs: tuple[str, ...] = ()
    #: The four-way grade's floor: the deck leans toward the class shortest of this many grades until DONE and each
    #: of `needs` hold it (`review_data.three_way` owns the rule).
    needs_min_per_class: int = 20
    #: The blind retest (`review_data.retest_due` owns the rule): the share of graded texts shown again, by
    #: `bucket(key, 'review-retest')`, and how many days old a label must be first. 0 percent = never.
    retest_percent: int = 10
    retest_after_days: int = 7
    #: Whole texts that are never this surface's question (patterns, matched whole after a strip): no card,
    #: no label, no training row. The ONE test is `admits`.
    rejects: tuple[str, ...] = ()

    def admits(self, text: str) -> bool:
        """Whether `text` is a reviewable text for this surface: anything a `rejects` pattern does not match whole."""
        stripped = text.strip()
        return not any(re.fullmatch(pattern, stripped) for pattern in self.rejects)


REVIEW_SURFACES: dict[str, ReviewSurface] = {
    "agent_open_work": ReviewSurface(
        name="agent_open_work",
        label="OPEN WORK",
        # copy of bridge/tldrack/open_work.py OPEN_WORK_QUESTION, OPEN_WORK_AT and DONE_AT — pinned by lab/tests/test_operator_labels.py
        question=("Does this message say that some work is still not done, not started, blocked, or waiting on an answer?"),
        question_type="noul",
        answers=("agree", "flip", "skip"),
        stem="agent-open-work",
        text_field="hand_off",
        golden_suite="agent-open-work",
        fine_tune="tldrack-agent-open-work",
        default_threshold=0.7,
        default_done_threshold=0.4,
        weights=(
            ("sample", 3),
            ("uncertain", 3),
            ("challenger", 2),
            ("live_sample", 2),
            ("disagree", 2),
            ("unflagged", 2),
            ("suspect", 1),
            ("golden", 1),
            ("flagged", 1),
            ("rest", 1),
        ),
        needs=("you", "running", "read"),
        rejects=CUT_OFF_NOTICES,
    ),
}


def surface_for_suite(suite: str) -> ReviewSurface | None:
    return next((surface for surface in REVIEW_SURFACES.values() if surface.golden_suite == suite), None)
