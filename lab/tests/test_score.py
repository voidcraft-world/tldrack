"""`review-open-work`: one scores row per reviewable text, rewritten whole, 0600."""

from __future__ import annotations

import json
import os
import stat
import statistics
from datetime import datetime, timezone

from laya_lab import infer, score
from laya_lab.registry import register
from laya_bridge import lab_files, paths, review_data, rows
from laya_bridge.ledger import Ledger
from laya_bridge.wire import clean_request
from tldrack import open_work

from conftest import CHECKPOINT, FINE_TUNE, SURFACE, FakeRouter, synthetic


def scores_path(lab):
    return paths.data_file(lab.base, "agent-open-work", "laya-p")


def test_scores_every_sonnet_and_ledger_text_once(lab, umask_022):
    labelled, unlabelled_only = synthetic("labelled", 2), synthetic("export-only", 1)
    keys = lab.unlabelled([*labelled, *unlabelled_only])
    lab.labels({keys[0]: True, keys[1]: False})
    ledger_text = "a synthetic closing text the rack judged live"
    book = Ledger(lambda: lab.base, clock=lambda: datetime(2026, 10, 27, tzinfo=timezone.utc))
    cleaned = clean_request({"state": ledger_text, "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
    assert book.record_decision({"surface": SURFACE}, cleaned, {"answers": {"q": {"noul": 0.3}}}, "english", None, None, 1)
    folder = lab.checkpoint()
    register(lab.base, SURFACE, folder, {"eval_items": 1})

    router = FakeRouter(lambda state: 0.91234 if "labelled 0" in state else 0.1)
    assert score.run(lab.base, router=router) == 0
    assert len(router.calls) == 3 and {model for _s, model in router.calls} == {FINE_TUNE}
    found, code = lab_files.read_scores(lab.base, "agent-open-work")
    assert code == "ok" and len(found) == 3
    by_key = {row["key"]: row for row in found}
    first = by_key[keys[0]]
    assert first == lab_files.scores_row(
        keys[0],
        0.9123,
        FINE_TUNE,
        CHECKPOINT,
        open_work.QUESTION_VERSION,
        open_work.OPEN_WORK_AT,
        first["at"],
        device="mps",
        digest=infer.checkpoint_digest(FINE_TUNE, folder),
    )
    assert first["at"].endswith("Z")
    assert keys[2] not in by_key, "an exported text nobody labelled or judged is not reviewable"
    path = scores_path(lab)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_rerun_replaces_the_file_whole(lab):
    keys = lab.unlabelled(synthetic("t", 2))
    lab.labels(dict.fromkeys(keys, True))
    assert score.run(lab.base, router=FakeRouter(lambda state: 0.2)) == 0
    first = os.stat(scores_path(lab)).st_ino
    lab.labels({keys[0]: True})
    assert score.run(lab.base, model="english", router=FakeRouter(lambda state: 0.8), full=True) == 0
    rows_out = [json.loads(line) for line in scores_path(lab).read_text().splitlines()]
    assert [(row["key"], row["p"], row["checkpoint"]) for row in rows_out] == [(keys[0], 0.8, None)]
    assert os.stat(scores_path(lab)).st_ino != first, "replaced by rename, never truncated in place"
    assert not [p for p in scores_path(lab).parent.iterdir() if p.name.endswith(".tmp")]


def test_a_rerun_scores_only_the_unscored_keys_under_the_same_digest_and_device(lab):
    keys = lab.unlabelled(synthetic("t", 3))
    lab.labels({keys[0]: True, keys[1]: False})
    assert score.run(lab.base, router=FakeRouter(lambda state: 0.2)) == 0
    lab.labels({keys[0]: True, keys[1]: False, keys[2]: True})
    router = FakeRouter(lambda state: 0.8)
    assert score.run(lab.base, router=router) == 0
    assert len(router.calls) == 1, "only the new key is predicted"
    p_by_key = {row["key"]: row["p"] for row in lab_files.read_scores(lab.base, "agent-open-work")[0]}
    assert p_by_key == {keys[0]: 0.2, keys[1]: 0.2, keys[2]: 0.8}
    router = FakeRouter(lambda state: 0.6)
    assert score.run(lab.base, router=router, device="cpu") == 0
    assert len(router.calls) == 3, "CPU p is not MPS p: another device re-scores every key"


def test_nothing_to_score_writes_nothing(lab, capsys):
    assert score.run(lab.base, router=FakeRouter(lambda state: 0.5)) == 0
    assert not scores_path(lab).exists()
    assert "nothing to score" in capsys.readouterr().out


# -- the teacher surrogate (the deck's `suspect` stratum) --------------------------------


def separable(n_each=12):
    """Open texts say what is left, done ones what shipped; every key labelled as its text reads."""
    texts, labels = {}, {}
    for i in range(n_each):
        for is_open, words in (
            (True, "phase next is remaining and still blocked on review"),
            (False, "shipped merged and verified everything landed"),
        ):
            text = f"turn {i} {'o' if is_open else 'd'}: {words} item{i}"
            key = rows.state_key(text)
            texts[key], labels[key] = text, is_open
    return texts, labels


def test_the_surrogate_trusts_labels_its_text_supports_and_doubts_a_flipped_one():
    texts, labels = separable()
    wrong = sorted(key for key in labels if labels[key])[0]
    labels[wrong] = False  # Sonnet's mistake: an open text labelled done
    conf = score.teacher_confidence(texts, labels)
    assert set(conf) == set(labels) and all(0 <= c <= 1 for c in conf.values())
    assert conf[wrong] < 0.5 and statistics.median(c for k, c in conf.items() if k != wrong) > 0.5
    suspects = review_data.suspect_ranked({key: (labels[key], c) for key, c in conf.items()})
    assert suspects[0] == wrong
    assert score.teacher_confidence(texts, labels) == conf, "fixed folds and seeds: a rerun writes the same"


def test_a_keys_own_label_never_trains_the_model_that_scores_it():
    texts, labels = separable()
    key = sorted(labels)[3]
    first = score.teacher_confidence(texts, labels)[key]
    flipped = score.teacher_confidence(texts, {**labels, key: not labels[key]})[key]
    assert abs(first + flipped - 1.0) < 2e-4, "out of fold: the same p, read for the other label"


def test_too_few_of_a_class_writes_no_teacher_pair():
    texts, labels = separable(n_each=4)
    assert score.teacher_confidence(texts, labels) == {}
    assert score.teacher_confidence({}, {}) == {}


def test_run_writes_the_teacher_pair_on_sonnet_rows_only(lab):
    texts, labels = separable()
    keys = lab.unlabelled(list(texts.values()))
    lab.labels({key: labels[key] for key in keys})
    ledger_text = "a synthetic closing text the rack judged live"
    book = Ledger(lambda: lab.base, clock=lambda: datetime(2026, 10, 27, tzinfo=timezone.utc))
    cleaned = clean_request({"state": ledger_text, "questions": {"q": open_work.OPEN_WORK_QUESTION}}, frozenset({"english"}))
    assert book.record_decision({"surface": SURFACE}, cleaned, {"answers": {"q": {"noul": 0.3}}}, "english", None, None, 1)
    assert score.run(lab.base, model="english", router=FakeRouter(lambda state: 0.5)) == 0
    found, _code = lab_files.read_scores(lab.base, "agent-open-work")
    by_key = {row["key"]: row for row in found}
    assert all(by_key[key]["teacher_open"] is labels[key] and 0 <= by_key[key]["teacher_conf"] <= 1 for key in keys)
    assert "teacher_open" not in by_key[rows.state_key(ledger_text)], "a live text has no teacher label to doubt"
