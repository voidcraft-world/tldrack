"""What the laya plugin reports after a label write, read by the tldrack bridge that repaints the row
(tldrack open_work.py § The operator's label): the two cannot import each other, and this venv holds both."""

from __future__ import annotations

import pytest

from laya_bridge import review_log
from laya_bridge.review_surfaces import REVIEW_SURFACES
from tldrack import open_work

DECISION_ID = "20261126T120000-0123456789ab"


def test_the_copied_needs_match_the_plugin():
    assert open_work.LABEL_NEEDS_OPEN == review_log.NEEDS_OPEN
    assert set(open_work.LABEL_NEEDS_OPEN + open_work.LABEL_NEEDS_DONE) == set(review_log.NEEDS)
    assert set(REVIEW_SURFACES[open_work.LEDGER_SURFACE].needs) == set(review_log.NEEDS)


@pytest.mark.parametrize("is_open", [True, False, None])
@pytest.mark.parametrize("needs", [None, *review_log.NEEDS])
def test_every_label_the_plugin_may_write_reads_on_the_rack_exactly_when_the_plugin_would_write_it(is_open, needs):
    change = {"surface": open_work.LEDGER_SURFACE, "decision_id": DECISION_ID, "open": is_open, "needs": needs}
    read = open_work.label_change(change)
    if is_open is None:
        assert read == (DECISION_ID, None)  # a skip or an undo: `label_changes` writes needs None beside it
    elif review_log.needs_ok(needs, is_open):
        assert read == (DECISION_ID, {"open": is_open, "needs": needs})
    else:
        assert read is None
