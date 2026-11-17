"""The operator-label budget: one owner for which label trains, which is promoted, which is held."""

from __future__ import annotations

from conftest import CHECKPOINT, FINE_TUNE, in_half
from laya_bridge import label_budget, review_data, review_log, rows
from laya_bridge.label_budget import RESERVE, TRAIN

SURFACE = "agent_open_work"


def operator(tree, key, verdict="agree", against_open=True):
    row = review_log.answer_row(
        answer_id=review_log.new_answer_id(),
        at="2026-11-09T01:02:03Z",
        surface=SURFACE,
        key=key,
        stratum="uncertain",
        answer=verdict,
        against={"by": "english", "open": against_open},
        laya=None,
        decision_id=None,
        held_out=True,
    )
    tree.append_jsonl("data/agent-open-work-operator.jsonl", row)
    return row


def record_training(tree, checkpoint, texts):
    """A checkpoint's training record on disk, registered or not: the union reads both."""
    tree.path(f"checkpoints/{checkpoint}").mkdir(exist_ok=True)
    tree.write_jsonl(
        f"checkpoints/{checkpoint}.data/train.jsonl",
        [{"state": text, "questions": {}, "expected": {"q": True}} for text in texts],
    )


def test_the_draw_is_stable_and_deals_one_in_two():
    keys = [rows.state_key(f"text {i}") for i in range(4000)]
    drawn = [label_budget.drawn(key) for key in keys]
    assert drawn == [label_budget.drawn(key) for key in keys]
    assert set(drawn) == set(label_budget.HALVES)
    assert 0.47 < drawn.count(RESERVE) / len(keys) < 0.53
    assert label_budget.RESERVE_PERCENT == 50, "D13: 1 in 2 (changing it re-deals every key: see the module note)"


def test_only_a_operator_label_is_budgeted_and_a_spent_key_is_train():
    [held, spent] = [rows.state_key(text) for text in in_half(RESERVE, "r", 2)]
    [trains] = [rows.state_key(text) for text in in_half(TRAIN, "t", 1)]
    [sonnet_only] = [rows.state_key(text) for text in in_half(RESERVE, "s", 1)]
    budget = label_budget.budget_of([held, spent, trains, "golden:case-open"], trained=[spent, sonnet_only])
    assert (budget.half(held), budget.half(spent), budget.half(trains)) == (RESERVE, TRAIN, TRAIN)
    assert budget.half(sonnet_only) is None and budget.half("golden:case-open") is None
    assert budget.reserve == {held}
    assert budget.promotable(trains) and budget.promotable(spent)
    assert not budget.promotable(held) and not budget.promotable(sonnet_only)


def test_withhold_takes_a_reserve_row_by_its_key_or_by_its_state():
    reserve_texts, train_texts = in_half(RESERVE, "r", 2), in_half(TRAIN, "t", 1)
    budget = label_budget.budget_of([rows.state_key(text) for text in reserve_texts + train_texts], trained=[])
    rows_in = [
        {"key": rows.state_key(reserve_texts[0]), "state": "a shortened state, so only the key names the text"},
        {"state": reserve_texts[1]},  # a row from before train rows carried their key
        {"key": rows.state_key(train_texts[0]), "state": train_texts[0]},
        {"key": "0" * 16, "state": "a Sonnet row the operator never graded"},
        {"key": "1" * 16},  # no state at all: nothing to hash, and its key is not the reserve's
    ]
    kept, held = budget.withhold(rows_in)
    assert held == 2 and kept == rows_in[2:]
    # The caller's wider set (laya-lab: the reserve and each text's judged-state key) replaces the reserve.
    wider = budget.reserve | {"0" * 16}
    assert budget.withhold(rows_in, wider) == ([rows_in[2], rows_in[4]], 3)
    assert budget.withhold(rows_in, frozenset()) == (rows_in, 0)


def test_read_joins_the_live_operator_labels_with_every_training_record(tree):
    texts = in_half(RESERVE, "r", 4)
    keys = tree.unlabelled(texts)
    operator(tree, keys[0])
    operator(tree, keys[1])
    undone = operator(tree, keys[2])
    tree.append_jsonl(
        "data/agent-open-work-operator.jsonl",
        review_log.undo_row(answer_id=undone["answer_id"], at="2026-11-09T01:03:00Z", surface=SURFACE),
    )
    operator(tree, keys[3], "skip")
    tree.register(train_texts=[])
    record_training(tree, f"{FINE_TUNE}-20261101-0300", [texts[1]])  # a rejected checkpoint, never registered
    budget = label_budget.read(tree.base, SURFACE)
    assert budget.operator == {keys[0], keys[1]}, "an undone answer and a skip are not labels"
    assert budget.reserve == {keys[0]} and budget.half(keys[1]) == TRAIN
    assert label_budget.read(tree.base, SURFACE, review_log.OperatorFold()).operator == frozenset(), "the caller's fold wins"


def test_the_reserve_is_still_clean_after_a_retrain_took_the_train_half(tree):
    """What the split is for: the next checkpoint starts with the reserve, not from zero."""
    reserve, train = in_half(RESERVE, "r", 6), in_half(TRAIN, "t", 6)
    keys = tree.unlabelled(reserve + train)
    tree.register(train_texts=[])
    for i, key in enumerate(keys):
        operator(tree, key, "agree", against_open=i % 2 == 0)
    before = review_data.sweep_readiness(tree.base, SURFACE)
    assert before["rows"] == 12 and before["reserve"] == {"rows": 6, "open": 3, "done": 3}

    # The retrain: build() leaves the reserve out, so the new record holds the TRAIN half only.
    kept, held = label_budget.read(tree.base, SURFACE).withhold([{"key": key} for key in keys])
    assert held == 6 and [row["key"] for row in kept] == keys[6:]
    record_training(tree, f"{FINE_TUNE}-20261110-0300", train)

    after = review_data.sweep_readiness(tree.base, SURFACE)
    assert after["rows"] == 6 and after["reserve"] == {"rows": 6, "open": 3, "done": 3}
    labels = dict.fromkeys(keys, True)
    clean = review_data.clean_label_rows(tree.base, SURFACE, labels)
    assert [key for key, _open, _text in clean.rows] == sorted(keys[:6]) and clean.reserve == set(keys[:6])
    assert clean.excluded["trained"] == 6
    assert label_budget.read(tree.base, SURFACE).reserve == set(keys[:6]), "a held key is in no record: never spent"
    assert CHECKPOINT in str(review_data.served_model(tree.base, SURFACE).checkpoint), "the old champion still serves"


def test_counts_split_a_pool_by_label():
    assert label_budget.counts(["a", "b", "c"], {"a": True, "b": False, "c": False, "d": True}) == {
        "rows": 3,
        "open": 1,
        "done": 2,
    }
    assert label_budget.counts([], {}) == {"rows": 0, "open": 0, "done": 0}
