"""The shared label rules and the deck's strata, cards and stats — over synthetic trees only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from laya_bridge import files, label_budget, lab_files, landing, paths, review_data, review_log, rows
from laya_bridge.ledger import Ledger
from laya_bridge.review_surfaces import REVIEW_SURFACES
from laya_bridge.wire import clean_request

from conftest import in_half

SURFACE = "agent_open_work"
SUITE = "agent-open-work"
NOUL = {"type": "noul", "instructions": "Is work open?"}
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def pick(prefix: str, salt: str, inside: bool, n: int, percent: int = 50) -> list[str]:
    """`n` texts whose `bucket(state_key(text), salt)` is (or is not) below `percent`."""
    found, i = [], 0
    while len(found) < n:
        text = f"{prefix} {i}: some closing message long enough to read"
        if (rows.bucket(rows.state_key(text), salt) < percent) == inside:
            found.append(text)
        i += 1
    return found


@pytest.fixture(autouse=True)
def public_golden(tmp_path, monkeypatch):
    """A synthetic public suite: the repo's own is never what these tests measure."""
    folder = tmp_path / "public-golden"
    folder.mkdir()
    cases = [
        {
            "id": "case-open",
            "expected": True,
            "why": "phase C remains",
            "tags": ["open"],
            "state": {"hand_off": "Phase C is not started."},
        },
        {
            "id": "case-done",
            "expected": False,
            "why": "all done",
            "tags": ["done"],
            "state": {"hand_off": "All three phases shipped."},
        },
    ]
    (folder / f"{SUITE}.jsonl").write_text("# public\n" + "".join(json.dumps(c) + "\n" for c in cases))
    monkeypatch.setattr(paths, "PUBLIC_GOLDEN_DIR", folder)
    return cases


def record(tree, text, p, threshold=0.7, clock=None, model="english", done_threshold=None, subject=None, conversation=None):
    book = Ledger(lambda: tree.base, clock=clock or (lambda: NOW))
    payload = {"answers": {"q": {"noul": p}}}
    tag = {"surface": SURFACE, "threshold": threshold, "question_version": 1}
    if done_threshold is not None:
        tag["done_threshold"] = done_threshold
    if subject is not None:
        tag["subject"] = subject
    if conversation is not None:  # written as given: record_decision trusts a tag clean_tag already passed
        tag["conversation"] = conversation
    return book.record_decision(
        tag, clean_request({"state": text, "questions": {"q": NOUL}}, frozenset({"english"})), payload, model, None, None, 5
    )


def operator(
    tree, key, verdict="agree", against_open=True, stratum="sample", laya=None, by="claude-sonnet-5-5", decision_id=None
):
    row = review_log.answer_row(
        answer_id=review_log.new_answer_id(),
        at="2026-10-27T01:02:03Z",
        surface=SURFACE,
        key=key,
        stratum=stratum,
        answer=verdict,
        against={"by": by, "open": against_open},
        laya=laya,
        decision_id=decision_id,
        held_out=True,
    )
    tree.path("data/x")
    tree.append_jsonl("data/agent-open-work-operator.jsonl", row)
    return row


def snap(tree, loaded=None):
    return review_data.snapshot(tree.base, SURFACE, loaded=loaded)


def strata(snapshot):
    return {key: item.stratum for key, item in snapshot.items.items.items()}


# -- strata ----------------------------------------------------------------------


def test_sonnet_keys_split_into_sample_and_rest_stably(tree):
    sample = pick("s", "review-sample", True, 4)
    rest = pick("r", "review-sample", False, 4)
    keys = tree.unlabelled(sample + rest)
    tree.labels({key: i % 2 == 0 for i, key in enumerate(keys)})
    first = strata(snap(tree))
    assert first == {rows.state_key(t): "sample" for t in sample} | {rows.state_key(t): "rest" for t in rest}
    assert strata(snap(tree)) == first
    view = snap(tree)
    assert sorted(view.remaining["sample"]) == sorted(rows.state_key(t) for t in sample)
    assert sum(len(v) for v in view.remaining.values()) == 8, "each key in exactly one stratum"


def test_disagree_needs_a_current_score(tree):
    texts = pick("d", "review-sample", False, 3)
    keys = tree.unlabelled(texts)
    tree.labels(dict.fromkeys(keys, True))
    checkpoint = tree.register()
    current = checkpoint.name
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [
            lab_files.scores_row(keys[0], 0.2, "tldrack-agent-open-work", current, 1, 0.7, "2026-10-27T00:00:00Z"),  # disagrees
            lab_files.scores_row(keys[1], 0.9, "tldrack-agent-open-work", current, 1, 0.7, "2026-10-27T00:00:00Z"),  # agrees
            lab_files.scores_row(keys[2], 0.1, "tldrack-agent-open-work", "an-older-checkpoint", 1, 0.7, "2026-10-27T00:00:00Z"),
        ],
    )
    found = strata(snap(tree))
    assert found == {keys[0]: "disagree", keys[1]: "rest", keys[2]: "rest"}
    card = review_data.card(snap(tree), keys[0])
    assert card["laya"] == {
        "p": 0.2,
        "threshold": 0.7,
        "model": "tldrack-agent-open-work",
        "checkpoint": current,
        "question_version": 1,
        "source": "scores",
    }


def test_flagged_and_unflagged_split_on_the_rows_own_threshold(tree):
    [flag_text] = pick("flag me: phase C is next", "review-live", False, 1, percent=10)
    [below_text] = pick("below its cut", "review-live", False, 1, percent=10)
    flagged = record(tree, flag_text, 0.85, threshold=0.6)
    record(tree, below_text, 0.65, threshold=0.9)
    found = strata(snap(tree))
    flagged_key = rows.state_key(flag_text)
    assert found[flagged_key] == "flagged"
    unflagged_key = rows.state_key(below_text)
    expected = "unflagged" if rows.bucket(unflagged_key, "review-unflagged") < 20 else "recent"
    assert found.get(unflagged_key) == expected
    card = review_data.card(snap(tree), flagged_key)
    assert card["decision"]["decision_id"] == flagged and card["decision"]["shown"] is False
    assert card["reference"] == {"by": "english", "open": True, "why": None} and card["laya"]["source"] == "ledger"
    assert card["edge"] == {"cut": "open", "delta": 0.25} and card["wave"] is None


def test_unflagged_membership_is_per_text_and_survives_a_rejudge(tree):
    texts = pick("u", "review-unflagged", True, 2, percent=20) + pick("v", "review-unflagged", False, 2, percent=20)
    for text in texts:
        record(tree, text, 0.1)
    before = strata(snap(tree))
    clock_later = lambda: datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)  # noqa: E731
    for text in texts:
        record(tree, text, 0.2, clock=clock_later)  # the same text judged again: a new decision_id
    after = strata(snap(tree))
    assert (
        before
        == after
        == ({rows.state_key(t): "unflagged" for t in texts[:2]} | {rows.state_key(t): "recent" for t in texts[2:]})
    )
    assert snap(tree).remaining["recent"] and review_data.queue(snap(tree), 50, "recent") == [], "recent is never dealt"
    assert snap(tree).items.unflagged_total == 4


def test_a_ledger_decision_on_a_sonnet_key_keeps_the_sonnet_stratum(tree):
    [text] = pick("k", "review-sample", True, 1)
    [key] = tree.unlabelled([text])
    tree.labels({key: False})
    decision_id = record(tree, text, 0.9)
    view = snap(tree)
    assert strata(view) == {key: "sample"}
    card = review_data.card(view, key)
    assert card["decision"]["decision_id"] == decision_id and card["reference"]["by"] == "claude-sonnet-5-5"
    assert card["laya"]["source"] == "ledger" and card["sonnet"]["open"] is False


def test_abstained_decisions_are_never_reviewed(tree):
    Ledger(lambda: tree.base, clock=lambda: NOW).record_decision(
        {"surface": SURFACE},
        clean_request({"state": "abstained text", "questions": {"q": NOUL}}, frozenset({"english"})),
        {"answers": {}},
        "english",
        None,
        None,
        1,
    )
    assert strata(snap(tree)) == {}


def test_answered_and_skipped_keys_leave_the_queue(tree):
    keys = tree.unlabelled(pick("a", "review-sample", True, 3))
    tree.labels(dict.fromkeys(keys, True))
    operator(tree, keys[0], "agree")
    operator(tree, keys[1], "skip")
    view = snap(tree)
    assert view.remaining["sample"] == [k for k in view.remaining["sample"] if k == keys[2]] == [keys[2]]
    assert review_data.card(view, keys[0]) is not None, "an answered item still has a card"


def test_golden_misses_come_from_the_newest_report_of_the_served_model(tree, public_golden):
    def report(name, model, misses):
        tree.path(f"evals/{name}").write_text(
            json.dumps(
                {
                    "cases": 2,
                    "at_threshold": {"threshold": 0.7, "accuracy": 0.5},
                    "brier": 0.2,
                    "suite": SUITE,
                    "model": model,
                    "question_version": 1,
                    "misses": [{"id": m, "expected": True, "p": 0.3, "tags": []} for m in misses],
                    "probabilities": dict.fromkeys(misses, 0.3),
                }
            )
        )

    report("agent-open-work-english-20260102-010101.json", "english", ["case-done"])
    report("agent-open-work-english-20260102-010203.json", "english", ["case-open"])
    report("agent-open-work-multilingual-20261020-000000.json", "multilingual", ["case-done"])
    view = snap(tree)
    assert strata(view) == {"golden:case-open": "golden"}
    card = review_data.card(view, "golden:case-open")
    assert card["golden"] == {"case_id": "case-open", "expected": True, "why": "phase C remains"}
    assert card["reference"] == {"by": "golden", "open": True, "why": "phase C remains"}
    assert card["laya"] == {
        "p": 0.3,
        "threshold": 0.7,
        "model": "english",
        "checkpoint": None,
        "question_version": 1,
        "source": "eval",
    }
    assert review_data.sources(view)["eval"] == "ok"


def test_a_golden_card_carries_the_request_its_case_was_written_with(tree, tmp_path, monkeypatch):
    folder = tmp_path / "asked-golden"
    folder.mkdir()
    cases = [
        {
            "id": "asked",
            "expected": True,
            "why": "w",
            "tags": ["open", lab_files.PROVISIONAL_TAG],
            "state": {"request": "  Ship phases A to C.  ", "hand_off": "A and B are in. C is not started."},
        },
        {
            "id": "long-ask",
            "expected": False,
            "why": "w",
            "tags": ["done", lab_files.PROVISIONAL_TAG],
            "state": {"request": "x" * (review_data.REQUEST_MAX + 50), "hand_off": "All of it shipped."},
        },
        {
            "id": "no-ask",
            "expected": False,
            "why": "w",
            "tags": ["done", lab_files.PROVISIONAL_TAG],
            "state": {"request": "   ", "hand_off": "Everything is merged."},
        },
    ]
    (folder / f"{SUITE}.jsonl").write_text("# public\n" + "".join(json.dumps(c) + "\n" for c in cases))
    monkeypatch.setattr(paths, "PUBLIC_GOLDEN_DIR", folder)
    view = snap(tree)
    assert review_data.card(view, "golden:asked")["golden"]["request"] == "Ship phases A to C."
    clipped = review_data.card(view, "golden:long-ask")["golden"]["request"]
    assert len(clipped) <= review_data.REQUEST_MAX and clipped.endswith("…")
    assert "request" not in review_data.card(view, "golden:no-ask")["golden"]  # the schema's field is optional, never ""


def test_a_golden_case_whose_text_the_operator_answered_is_excluded(tree, public_golden):
    tree.path("evals/agent-open-work-english-20260102-010203.json").write_text(
        json.dumps(
            {
                "cases": 2,
                "at_threshold": {"threshold": 0.7, "accuracy": 0.5},
                "brier": 0.2,
                "suite": SUITE,
                "model": "english",
                "misses": [{"id": "case-open"}],
                "probabilities": {},
            }
        )
    )
    assert snap(tree).remaining["golden"] == ["golden:case-open"]
    operator(tree, rows.state_key("Phase C is not started."), "flip", stratum="flagged", by="english")
    assert snap(tree).remaining["golden"] == []


def test_a_provisional_public_case_is_dealt_for_sign_off_until_the_operator_answers_it(tree, public_golden):
    """`lab_files.PROVISIONAL_TAG`: an agent-written case waits in the golden stratum, missed or not, report or none."""
    case = {
        "id": "case-new",
        "expected": True,
        "why": "agent-written",
        "tags": ["open", lab_files.PROVISIONAL_TAG],
        "state": {"hand_off": "The docs pair is still missing; the button itself works."},
    }
    path = paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl"
    path.write_text(path.read_text() + json.dumps(case) + "\n")
    view = snap(tree)
    assert view.remaining["golden"] == ["golden:case-new"]
    assert review_data.card(view, "golden:case-new")["reference"] == {"by": "golden", "open": True, "why": "agent-written"}
    operator(tree, "golden:case-new", "agree", by="golden", stratum="golden")
    view = snap(tree)
    assert view.remaining["golden"] == []
    cases, _code = lab_files.read_golden_suite(path)
    counted, held = lab_files.counted_cases(cases, lab_files.agreed_case_ids(view.fold.labels))
    assert [c["id"] for c in counted] == ["case-open", "case-done", "case-new"] and held == []
    assert lab_files.counted_cases(cases, frozenset())[1] == ["case-new"]


def batch_cases(n: int, batch: str = "batch-9") -> list[dict]:
    return [
        {
            "id": f"b-{i}",
            "expected": i % 2 == 0,
            "why": "agent-written",
            "tags": ["open" if i % 2 == 0 else "done", "waiting" if i % 2 == 0 else "clean", batch, lab_files.PROVISIONAL_TAG],
            "state": {"hand_off": f"Synthetic batch hand-off number {i}."},
        }
        for i in range(n)
    ]


def test_a_batch_deals_its_sample_then_counts_whole_on_every_agree(tree, public_golden, monkeypatch):
    """`lab_files.BATCH_TAG`: the deck deals the sample only; all agreed, the unsampled cases count with no card each."""
    monkeypatch.setattr(lab_files, "BATCH_SAMPLE", 2)
    path = paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl"
    path.write_text(path.read_text() + "".join(json.dumps(c) + "\n" for c in batch_cases(6)))
    cases, _code = lab_files.read_golden_suite(path)
    sample = lab_files.batch_sample("batch-9", [c for c in cases if lab_files.case_batch(c)])
    assert len(sample) == 2 and {c["expected"] for c in cases if c["id"] in sample} == {True, False}, "one per stratum"
    view = snap(tree)
    assert sorted(view.remaining["golden"]) == [f"golden:{case_id}" for case_id in sample]
    for case_id in sample:
        operator(tree, f"golden:{case_id}", "agree", by="golden", stratum="golden")
    view = snap(tree)
    assert view.remaining["golden"] == []
    standing = lab_files.batch_standing(cases, view.fold.labels, view.fold.skipped)["batch-9"]
    assert standing["state"] == "accepted" and standing["agreed"] == 2 and standing["bound"] == lab_files.batch_bound(6, 2)
    counted, held = lab_files.counted_cases(cases, lab_files.signed_off_ids(cases, view.fold.labels, view.fold.skipped))
    assert held == [] and len(counted) == 8


def test_a_flipped_sample_case_rejects_the_batch_and_deals_all_of_it(tree, public_golden, monkeypatch):
    """A defect in the sample is never redrawn around: the whole batch is dealt, and each counts on its own agree."""
    monkeypatch.setattr(lab_files, "BATCH_SAMPLE", 2)
    path = paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl"
    path.write_text(path.read_text() + "".join(json.dumps(c) + "\n" for c in batch_cases(6)))
    cases, _code = lab_files.read_golden_suite(path)
    first, second = lab_files.batch_sample("batch-9", [c for c in cases if lab_files.case_batch(c)])
    operator(tree, f"golden:{first}", "agree", by="golden", stratum="golden")
    operator(tree, f"golden:{second}", "flip", by="golden", stratum="golden")
    view = snap(tree)
    assert lab_files.batch_standing(cases, view.fold.labels)["batch-9"]["state"] == "rejected"
    assert sorted(view.remaining["golden"]) == sorted(f"golden:b-{i}" for i in range(6) if f"b-{i}" not in (first, second))
    signed = lab_files.signed_off_ids(cases, view.fold.labels, view.fold.skipped)
    assert signed == {first}, "a rejected batch counts only what was agreed one by one"


def test_a_skipped_sample_case_is_a_defect():
    cases = batch_cases(6)
    sample = lab_files.batch_sample("batch-9", cases, size=2)
    labels = {f"golden:{sample[0]}": {"answer": "agree"}}
    standing = lab_files.batch_standing(cases, labels, {f"golden:{sample[1]}"})["batch-9"]
    assert standing["state"] == "rejected" and standing["defects"] == [sample[1]]


def test_missing_too_large_and_refused_sources_are_empty_with_codes(tree, tmp_path, monkeypatch):
    view = snap(tree)
    assert all(not keys for keys in view.remaining.values())
    codes = review_data.sources(view)
    assert codes["unlabelled"] == codes["labels"] == codes["scores"] == codes["operator"] == "missing"
    assert codes["golden"] == "ok" and codes["golden_private"] == "missing"
    tree.unlabelled(["a text"])
    monkeypatch.setattr(review_data, "UNLABELLED_CAP_BYTES", 3)
    outside = tmp_path / "labels.jsonl"
    outside.write_text("{}\n")
    tree.path("data/agent-open-work-labels.jsonl").symlink_to(outside)
    codes = review_data.sources(snap(tree))
    assert codes["unlabelled"] == "too_large" and codes["labels"] == "refused"


def test_card_text_is_tail_capped(tree):
    [text] = pick("long" + "x" * 5_000, "review-sample", True, 1)
    [key] = tree.unlabelled([text])
    tree.labels({key: True})
    card = review_data.card(snap(tree), key)
    assert card["truncated"] is True and card["text_chars"] == len(text) and len(card["text"]) == 4_000
    assert text.endswith(card["text"])


def test_card_text_is_capped_in_utf16_units_as_zod_counts_them(tree):
    [text] = pick("emoji" + "😀" * 3_000, "review-sample", True, 1)  # ~3,050 code points, ~6,050 UTF-16 units
    [key] = tree.unlabelled([text])
    tree.labels({key: True})
    card = review_data.card(snap(tree), key)
    assert card["truncated"] is True and card["text_chars"] == files.utf16_len(text)
    assert files.utf16_len(card["text"]) <= 4_000 and text.endswith(card["text"])


def test_a_question_version_javascript_cannot_read_becomes_zero(tree):
    record(tree, "an unlabelled text", 0.9)
    path = tree.base / "ledger" / "agent_open_work.jsonl"
    [row] = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text(json.dumps({**row, "question_version": 10**400}) + "\n")  # a hand-edited row
    [card] = [c for c in review_data.queue(snap(tree), 10) if c["stratum"] == "flagged"]
    assert card["laya"]["question_version"] == 0


def test_held_out_is_true_false_or_unknown(tree):
    trained_text, fresh_text = pick("h", "review-sample", True, 2)
    keys = tree.unlabelled([trained_text, fresh_text])
    tree.labels(dict.fromkeys(keys, True))
    assert review_data.card(snap(tree), keys[0])["held_out"] is True, "a built-in trained on nothing here"
    tree.register(train_texts=[trained_text])
    view = snap(tree)
    assert review_data.card(view, keys[0])["held_out"] is False and review_data.card(view, keys[1])["held_out"] is True
    tree.path("checkpoints/tldrack-agent-open-work-20261001-0000").mkdir()  # an OLD checkpoint with no record
    assert review_data.card(snap(tree), keys[1])["held_out"] is True, "only the served checkpoint's record decides"
    tree.register(checkpoint="tldrack-agent-open-work-20261020-0000", train_texts=None)  # served, no record
    assert review_data.card(snap(tree), keys[1])["held_out"] is None


def test_loaded_wins_over_the_registry(tree):
    tree.register(train_texts=["x"])
    assert review_data.served_model(tree.base, SURFACE).name == "tldrack-agent-open-work"
    assert review_data.served_model(tree.base, SURFACE, loaded={}).name == "english"
    loaded = {"tldrack-agent-open-work": str(tree.base / "checkpoints" / "elsewhere-1")}
    assert review_data.served_model(tree.base, SURFACE, loaded=loaded).basename == "elsewhere-1"


# -- stats -----------------------------------------------------------------------


def test_kappa_is_none_below_twenty_pairs_and_counts_what_answers_recorded(tree):
    keys = tree.unlabelled(pick("p", "review-sample", True, 25))
    tree.labels({key: i % 2 == 0 for i, key in enumerate(keys)})
    for i, key in enumerate(keys[:19]):
        operator(
            tree,
            key,
            "agree" if i % 3 else "flip",
            against_open=i % 2 == 0,
            laya={"p": 0.9 if i % 2 == 0 else 0.1, "threshold": 0.7, "source": "scores", "checkpoint_path": None},
        )
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert stats["operator_vs_sonnet"]["n"] == 19 and stats["operator_vs_sonnet"]["kappa"] is None
    assert stats["operator_vs_sonnet"]["agree_rate"] is not None and stats["kappa_min_pairs"] == 20
    operator(
        tree,
        keys[19],
        "agree",
        against_open=False,
        laya={"p": 0.1, "threshold": 0.7, "source": "scores", "checkpoint_path": None},
    )
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert stats["operator_vs_sonnet"]["n"] == 20 and isinstance(stats["operator_vs_sonnet"]["kappa"], float)
    assert stats["operator_vs_laya"]["n"] == 20
    assert stats["reviewed"] == 20 and stats["agree"] + stats["flip"] == 20
    recorded_open = sum(1 for row in snap(tree).fold.labels.values() if row["open"])
    assert stats["by_stratum"]["sample"] == {"reviewed": 20, "remaining": 5, "open": recorded_open}
    assert stats["to_review"] == 5 and stats["backlog"] == 0
    assert stats["last_answer_id"].startswith("a_") and stats["last_answer_at"] == "2026-10-27T01:02:03Z"


def test_kappa_carries_its_interval_and_laya_its_precision_and_recall_at_the_cut(tree):
    """P1-13: past the minimum, κ ± rides every agreement, and Laya's flag at its cut reads against the operator."""
    keys = tree.unlabelled(pick("c", "review-sample", True, 24))
    tree.labels(dict.fromkeys(keys, True))
    # 12 the operator calls OPEN: Laya flags 10 of them; 12 DONE: Laya flags 2 of them.
    for i, key in enumerate(keys):
        is_open = i < 12
        flagged = i < 10 or i in (12, 13)
        operator(
            tree,
            key,
            "agree" if is_open else "flip",
            against_open=True,
            laya={"p": 0.9 if flagged else 0.1, "threshold": 0.7, "source": "scores", "checkpoint_path": None},
        )
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    sonnet, laya = stats["operator_vs_sonnet"], stats["operator_vs_laya"]
    assert sonnet["n"] == 24 and sonnet["kappa"] == 0.0 and sonnet["kappa_ci"] is not None, "Sonnet said OPEN every time"
    assert "at_cut" not in sonnet, "κ-vs-Sonnet stays one number"
    assert laya["kappa"] == 0.667 and laya["kappa_ci"]["lo"] < 0.667 < laya["kappa_ci"]["hi"]
    assert laya["kappa_ci"]["half_width"] > 0.15 and laya["kappa_ci"]["pairs_for_target"] > 24
    precision, recall = laya["at_cut"]["precision"], laya["at_cut"]["recall"]
    assert (precision["n"], precision["value"]) == (12, 0.833), "of the 12 Laya flagged, the operator called 10 open"
    assert (recall["n"], recall["value"]) == (12, 0.833), "of the 12 the operator called open, Laya flagged 10"
    assert precision["lo"] < 0.833 < precision["hi"]


def test_below_twenty_pairs_there_is_no_interval_and_no_rate_without_its_minimum(tree):
    keys = tree.unlabelled(pick("d", "review-sample", True, 6))
    for key in keys:
        operator(
            tree, key, "agree", against_open=True, laya={"p": 0.9, "threshold": 0.7, "source": "scores", "checkpoint_path": None}
        )
    laya = review_data.stats(snap(tree), tree.base, now=NOW)["operator_vs_laya"]
    assert laya["kappa"] is None and laya["kappa_ci"] is None
    assert laya["at_cut"]["precision"]["value"] == 1.0 and laya["at_cut"]["precision"]["n"] == 6
    empty = review_data.stats(snap(tree), tree.base, now=NOW)["operator_vs_sonnet"]
    assert empty["kappa_ci"] is None


def test_answers_against_golden_or_laya_are_not_sonnet_pairs(tree):
    keys = tree.unlabelled(pick("g", "review-sample", True, 3))
    operator(tree, keys[0], against_open=True, by="golden")
    operator(tree, keys[1], against_open=True, by="tldrack-agent-open-work")
    operator(tree, keys[2], against_open=True, by="english")
    assert review_data.stats(snap(tree), tree.base, now=NOW)["operator_vs_sonnet"]["n"] == 0


def estimate(recall_est):
    """The stratified whole-population recall, without its per-audience split (`live_rates` owns that)."""
    return {name: recall_est[name] for name in ("n_flagged", "n_unflagged", "value")}


def test_precision_and_recall_need_their_minimums(tree):
    texts = pick("f", "review-unflagged", True, 6, percent=20)
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    ids = []
    for text in texts:
        ids.append(record(tree, text, 0.95))
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert {name: stats["precision"][name] for name in ("window_days", "shown", "dismissed", "value")} == {
        "window_days": 7,
        "shown": 0,
        "dismissed": 0,
        "value": None,
    }
    assert estimate(stats["recall_est"]) == {"n_flagged": 0, "n_unflagged": 0, "value": None}
    for decision_id in ids[:5]:
        book.react(decision_id, "shown")
    book.react(ids[0], "dismissed")
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert (stats["precision"]["shown"], stats["precision"]["dismissed"]) == (5, 1), "the reactions stay as telemetry"
    assert stats["precision"]["value"] is None, "a dismissal is not a grade: 1 − 1/5 is no precision"

    low = pick("l", "review-unflagged", True, 5, percent=20)
    for text in low:
        record(tree, text, 0.05)
    for text in texts[:5]:
        operator(tree, rows.state_key(text), "agree", against_open=True, stratum="flagged", by="english")
    for i, text in enumerate(low):
        operator(tree, rows.state_key(text), "flip" if i == 0 else "agree", against_open=False, stratum="unflagged", by="english")
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    # F = 6 flagged, all open (a_f/n_f = 1); U = 5 unflagged, 1 of 5 open → recall = 6 / (6 + 1)
    assert estimate(stats["recall_est"]) == {"n_flagged": 5, "n_unflagged": 5, "value": round(6 / 7, 3)}


def test_a_second_stats_call_parses_only_the_appended_line(tree):
    keys = tree.unlabelled(pick("t", "review-sample", True, 3))
    tree.labels(dict.fromkeys(keys, True))
    operator(tree, keys[0])
    review_data.stats(snap(tree), tree.base, now=NOW)
    path = review_log.operator_path(tree.base, SURFACE)
    before = files.shared_tail(path, review_log.READ_CAP_BYTES).parsed_lines
    operator(tree, keys[1])
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert stats["reviewed"] == 2
    assert files.shared_tail(path, review_log.READ_CAP_BYTES).parsed_lines == before + 1


# -- the shared label rules -------------------------------------------------------


def labelled_tree(tree, n_open, n_done, *, train=(), register=True):
    open_texts = [f"open {i}: phase {i} is not started yet" for i in range(n_open)]
    done_texts = [f"done {i}: shipped and verified" for i in range(n_done)]
    keys = tree.unlabelled(open_texts + done_texts)
    if register:
        tree.register(train_texts=list(train))
    for key in keys[:n_open]:
        operator(tree, key, "agree", against_open=True)
    for key in keys[n_open:]:
        operator(tree, key, "flip", against_open=True)
    return keys


def test_sweep_readiness_counts_clean_labels(tree):
    labelled_tree(tree, 10, 9)
    ready = review_data.sweep_readiness(tree.base, SURFACE)
    assert ready["rows"] == 19 and ready["open"] == 10 and ready["done"] == 9 and ready["ready"] is False
    assert "needs 40" in ready["reason"]


def test_sweep_readiness_is_ready_at_forty_with_ten_each(tree):
    keys = labelled_tree(tree, 25, 15)
    held = [label_budget.drawn(key) == label_budget.RESERVE for key in keys]  # nothing is trained, so the draw decides
    reserve = {"rows": sum(held), "open": sum(held[:25]), "done": sum(held[25:])}
    assert 0 < reserve["rows"] < 40
    assert review_data.sweep_readiness(tree.base, SURFACE) == {
        "rows": 40,
        "open": 25,
        "done": 15,
        "ready": True,
        "reason": None,
        "reserve": reserve,
    }


def test_sweep_refuses_a_built_in(tree):
    labelled_tree(tree, 25, 15, register=False)
    ready = review_data.sweep_readiness(tree.base, SURFACE)
    assert ready["ready"] is False and ready["reason"] == "no fine-tune registered for agent_open_work"


def test_sweep_refuses_a_served_checkpoint_without_a_training_record(tree):
    labelled_tree(tree, 25, 15)
    tree.register(checkpoint="tldrack-agent-open-work-20261020-0000", train_texts=None)
    ready = review_data.sweep_readiness(tree.base, SURFACE)
    assert ready["ready"] is False
    assert ready["reason"] == "checkpoint tldrack-agent-open-work-20261020-0000 has no training record — retrain it"


def test_an_old_unrecorded_checkpoint_never_blocks_a_sweep(tree):
    labelled_tree(tree, 25, 15)  # served: a checkpoint WITH its training record
    tree.path("checkpoints/tldrack-agent-open-work-20261001-0000").mkdir()  # retraining never removes this one
    ready = review_data.sweep_readiness(tree.base, SURFACE)
    assert ready["reason"] is None and ready["ready"] is True


def test_a_served_folder_laya_lab_did_not_name_has_no_known_record(tree):
    labelled_tree(tree, 25, 15)
    loaded = {"tldrack-agent-open-work": str(tree.path("checkpoints/hand-copied"))}
    ready = review_data.sweep_readiness(tree.base, SURFACE, loaded=loaded)
    assert ready["reason"] == "checkpoint hand-copied has no training record — retrain it"


def test_clean_rows_drop_trained_golden_and_textless_keys(tree, public_golden):
    keys = labelled_tree(tree, 2, 0, train=["open 0: phase 0 is not started yet"])
    golden_key = rows.state_key("Phase C is not started.")
    labels = {keys[0]: True, keys[1]: True, golden_key: True, "golden:case-open": True, "f" * 16: False}
    clean = review_data.clean_label_rows(tree.base, SURFACE, labels)
    assert [key for key, _open, _text in clean.rows] == [keys[1]]
    assert clean.excluded == {"trained": 1, "golden": 2, "no_text": 1} and clean.reason is None
    # keys[1] carries an operator label (`labelled_tree`), so it is the reserve's exactly when its hash draws it there
    assert clean.reserve == ({keys[1]} if label_budget.drawn(keys[1]) == label_budget.RESERVE else set())


def test_texts_merge_exports_with_kept_ledger_states(tree):
    [exported] = tree.unlabelled(["exported text"])
    record(tree, "only in the ledger", 0.9)
    found = review_data.texts(tree.base, SURFACE)
    assert found == {exported: "exported text", rows.state_key("only in the ledger"): "only in the ledger"}


def test_a_cut_off_notice_is_never_a_text_a_card_or_a_label_target(tree):
    notice = "API Error: Server error mid-response. The response above may be incomplete."
    limit = "You've hit your weekly limit · resets 12am (America/Denver)"
    [real, notice_key, _limit_key] = tree.unlabelled(["Phase C is not started.", notice, limit])
    record(tree, "API Error: 529 Overloaded. This is a server-side issue.", 0.9)  # a kept ledger state
    tree.labels({real: True, notice_key: True})
    assert review_data.texts(tree.base, SURFACE) == {real: "Phase C is not started."}
    assert set(review_data.unlabelled_texts(tree.base, REVIEW_SURFACES[SURFACE])[1]) == {real}
    snap = review_data.snapshot(tree.base, SURFACE)
    assert [card["key"] for card in review_data.queue(snap, 10)] == [real]
    # A message that merely MENTIONS an error is still a hand-off.
    assert REVIEW_SURFACES[SURFACE].admits(f"Fixed the retry.\n{notice}")


def test_promotion_candidates_are_live_flips_with_text_not_golden(tree, public_golden):
    keys = tree.unlabelled(["flip me", "agree me", "Phase C is not started.", "undone flip"])
    tree.register(train_texts=["flip me", "agree me", "undone flip"])  # trained on, so each is in the TRAIN half
    operator(tree, keys[0], "flip")
    operator(tree, keys[1], "agree")
    operator(tree, keys[2], "flip")  # a golden text
    undone = operator(tree, keys[3], "flip")
    tree.append_jsonl(
        "data/agent-open-work-operator.jsonl",
        review_log.undo_row(answer_id=undone["answer_id"], at="2026-10-27T01:02:04Z", surface=SURFACE),
    )
    operator(tree, "0" * 16, "flip")  # no text
    assert review_data.promotion_candidates(tree.base, SUITE) == [keys[0]]
    assert review_data.promotion_candidates(tree.base, "no-such-suite") == []


def test_golden_keys_cover_public_and_private_cases(tree):
    key = rows.state_key("an operator case")
    case = lab_files.private_golden_case(key, "an operator case", True, False, "claude-sonnet-5-5", "2026-10-27T00:00:00Z")
    tree.write_jsonl("golden/agent-open-work.private.jsonl", [case], header=lab_files.PRIVATE_GOLDEN_HEADER)
    assert review_data.golden_keys(tree.base, SUITE) == {
        rows.state_key("Phase C is not started."),
        rows.state_key("All three phases shipped."),
        key,
    }


def test_queue_is_a_weighted_round_robin(tree):
    sample = pick("s", "review-sample", True, 7)
    rest = pick("r", "review-sample", False, 3)
    keys = tree.unlabelled(sample + rest)
    tree.labels(dict.fromkeys(keys, True))
    cards = review_data.queue(snap(tree), 8)
    assert [c["stratum"] for c in cards] == ["sample"] * 3 + ["rest"] + ["sample"] * 3 + ["rest"]
    assert [c["stratum"] for c in review_data.queue(snap(tree), 5, "rest")] == ["rest"] * 3
    assert review_data.queue(snap(tree), 0) == []


# -- the RECENT list --------------------------------------------------------------


def at(minute):
    return lambda: datetime(2026, 10, 27, 12, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "p, open_at, done_at, band",
    [
        (0.9, 0.7, 0.4, "open"),
        (0.7, 0.7, 0.4, "open"),
        (0.69, 0.7, 0.4, "reply"),
        (0.4, 0.7, 0.4, "reply"),
        (0.39, 0.7, 0.4, "done"),
        (0.1, 0.7, None, "reply"),  # before the band: never done
        (0.3, 0.35, 0.5, "done"),
        (0.36, 0.35, 0.5, "open"),  # the done cut never passes the open cut
    ],
)
def test_band_of(p, open_at, done_at, band):
    assert review_data.band_of(p, open_at, done_at) == band


def test_recent_rows_are_newest_first_with_the_mark_drawn_and_the_band_now(tree):
    record(tree, "old: all three phases shipped", 0.2, clock=at(1))  # predates the band: the rack drew 💬
    record(tree, "done: shipped, tests green", 0.2, clock=at(2), done_threshold=0.4, subject="wt-a")
    record(tree, "unsure: mostly there", 0.5, clock=at(3), done_threshold=0.4)
    record(tree, "open: phase C is next", 0.9, clock=at(4), done_threshold=0.4)
    found, summary = review_data.recent_rows(snap(tree), 100)
    assert [row["text"].split(":")[0] for row in found] == ["open", "unsure", "done", "old"]
    assert [(row["mark"], row["band"]) for row in found] == [
        ("open", "open"),
        ("reply", "reply"),
        ("done", "done"),
        ("reply", "done"),
    ]
    assert found[2]["subject"] == "wt-a" and found[3]["done_threshold"] is None
    assert all(row["reference_open"] is not None for row in found), "every live verdict is answerable"
    assert summary["n"] == 4 and summary["done_at"] == 0.4
    assert summary["bands"]["done"] == {"n": 2, "reviewed": 0, "right": 0, "open": 0}


def test_a_text_judged_twice_is_one_row_at_its_newest_verdict(tree):
    record(tree, "same text", 0.2, clock=at(1), done_threshold=0.4)
    record(tree, "same text", 0.8, clock=at(5), done_threshold=0.4)
    found, _summary = review_data.recent_rows(snap(tree), 100)
    assert len(found) == 1 and found[0]["band"] == "open" and found[0]["at"] == "2026-10-27T12:05:00Z"


def test_recent_rows_carry_the_operators_answer_and_grade_it_against_the_band(tree):
    record(tree, "done text", 0.1, clock=at(1), done_threshold=0.4)
    record(tree, "wrong check", 0.15, clock=at(2), done_threshold=0.4)
    record(tree, "open text", 0.95, clock=at(3), done_threshold=0.4)
    record(tree, "unsure text", 0.55, clock=at(4), done_threshold=0.4)
    record(tree, "skipped text", 0.95, clock=at(5), done_threshold=0.4)
    k = rows.state_key
    operator(tree, k("done text"), "agree", against_open=False, stratum="recent", by="english")
    operator(tree, k("wrong check"), "flip", against_open=False, stratum="recent", by="english")
    operator(tree, k("open text"), "agree", against_open=True, stratum="flagged", by="english")
    operator(tree, k("unsure text"), "flip", against_open=False, stratum="recent", by="english")
    operator(tree, k("skipped text"), "skip", against_open=True, stratum="flagged", by="english")
    found, summary = review_data.recent_rows(snap(tree), 100)
    by_text = {row["text"]: row for row in found}
    assert [row["operator"]["key"] for row in found] == [row["key"] for row in found], "answerRefSchema requires key"
    assert by_text["wrong check"]["operator"]["open"] is True and by_text["wrong check"]["operator"]["answer"] == "flip"
    assert by_text["skipped text"]["operator"]["open"] is None
    assert summary["bands"]["done"] == {"n": 2, "reviewed": 2, "right": 1, "open": 1}
    assert summary["bands"]["open"] == {"n": 2, "reviewed": 1, "right": 1, "open": 1}
    assert summary["bands"]["reply"] == {"n": 1, "reviewed": 1, "right": None, "open": 1}
    assert summary["skipped"] == 1


def test_recent_rows_narrow_to_one_session_since_it_started(tree):
    record(tree, "older vc-3, before this one", 0.2, clock=at(1), done_threshold=0.4, subject="vc-3")
    record(tree, "this vc-3", 0.9, clock=at(3), done_threshold=0.4, subject="vc-3")
    record(tree, "another session", 0.9, clock=at(4), done_threshold=0.4, subject="wt-a")
    record(tree, "this one, renamed since", 0.2, clock=at(5), done_threshold=0.4, subject="build-docs")
    found, summary = review_data.recent_rows(snap(tree), 100, subject=("build-docs", "vc-3"), since="2026-10-27T12:02:00Z")
    assert [row["text"] for row in found] == ["this one, renamed since", "this vc-3"]
    assert summary["subject"] == "build-docs" and summary["pool"] == 2
    every, unfiltered = review_data.recent_rows(snap(tree), 100)
    assert len(every) == 4 and "subject" not in unfiltered


CONVERSATION = "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab"


def test_recent_rows_carry_the_conversation_the_verdict_judged_or_null(tree):
    """Two conversations reusing one rack name, and a verdict from before the field: the page bands by it."""
    record(tree, "before the field", 0.2, clock=at(1), done_threshold=0.4, subject="vc-8")
    record(tree, "first agent in vc-8", 0.2, clock=at(2), done_threshold=0.4, subject="vc-8", conversation=CONVERSATION)
    record(tree, "next agent in vc-8", 0.9, clock=at(3), done_threshold=0.4, subject="vc-8", conversation=CONVERSATION.upper())
    found, _summary = review_data.recent_rows(snap(tree), 100)
    assert [(row["text"], row["conversation"]) for row in found] == [
        ("next agent in vc-8", CONVERSATION.upper()),
        ("first agent in vc-8", CONVERSATION),
        ("before the field", None),
    ]
    assert all(row["subject"] == "vc-8" for row in found)


@pytest.mark.parametrize("conversation", ["not-a-uuid", CONVERSATION + "\n", 7, ["x"], {"id": CONVERSATION}, ""])
def test_a_conversation_that_is_not_a_uuid_on_disk_serves_null(tree, conversation):
    """A hand-edited or foreign row never puts text where the page expects an opaque id."""
    record(tree, "odd row", 0.2, clock=at(1), done_threshold=0.4, conversation=conversation)
    [row], _summary = review_data.recent_rows(snap(tree), 100)
    assert row["conversation"] is None


def test_recent_rows_respect_the_limit_and_the_cap(tree):
    for i in range(5):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    found, summary = review_data.recent_rows(snap(tree), 2)
    assert [row["text"] for row in found] == ["text 4", "text 3"] and summary["n"] == 2
    assert review_data.RECENT_DEFAULT <= review_data.RECENT_MAX


def test_an_offset_pages_on_through_the_newest_order(tree):
    for i in range(5):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    operator(tree, rows.state_key("text 4"), "agree", against_open=False, stratum="recent", by="english")
    first, first_summary = review_data.recent_rows(snap(tree), 2)
    found, summary = review_data.recent_rows(snap(tree), 2, offset=2)
    assert [row["text"] for row in first] == ["text 4", "text 3"] and first_summary["offset"] == 0
    assert [row["text"] for row in found] == ["text 2", "text 1"]
    assert summary["offset"] == 2 and summary["pool"] == 5 and summary["n"] == 2
    assert first_summary["bands"]["done"]["reviewed"] == 1 and summary["bands"]["done"] == {
        "n": 2,
        "reviewed": 0,
        "right": 0,
        "open": 0,
    }, "the tally counts the emitted rows only"
    assert [row["text"] for row in review_data.recent_rows(snap(tree), 2, offset=4)[0]] == ["text 0"]


def test_an_offset_past_the_end_is_an_empty_page_of_the_same_pool(tree):
    for i in range(3):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    found, summary = review_data.recent_rows(snap(tree), 100, offset=3)
    assert found == [] and summary["n"] == 0 and summary["pool"] == 3 and summary["offset"] == 3
    assert all(band["n"] == 0 for band in summary["bands"].values())
    assert review_data.recent_rows(snap(tree), 100, "edge", offset=100_000)[0] == []


def pages(tree, limit, order="newest", todo=False):
    """Every page of an order, walked by `summary.next` as the REVIEW page does."""
    out, after = [], None
    while True:
        found, summary = review_data.recent_rows(snap(tree), limit, order, todo, after=after)
        out.append([row["text"] for row in found])
        after = summary["next"]
        if after is None:
            return out


def test_a_cursor_pages_on_through_the_newest_order_and_ends(tree):
    for i in range(5):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    assert pages(tree, 2) == [["text 4", "text 3"], ["text 2", "text 1"], ["text 0"]]
    _found, summary = review_data.recent_rows(snap(tree), 5)
    assert summary["next"] is None, "nothing follows a page that holds the whole pool"
    assert (
        review_data.recent_rows(snap(tree), 2)[1]["next"]
        == f"n~{at(3)().strftime('%Y-%m-%dT%H:%M:%SZ')}~{rows.state_key('text 3')}"
    )


def test_a_cursor_holds_its_place_when_a_served_row_is_answered_elsewhere(tree):
    """Under `todo` an answer takes its row out of the pool. A count of rows served would then reach one row too far
    and skip `text 2`; the place does not move."""
    for i in range(5):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    first, summary = review_data.recent_rows(snap(tree), 2, todo=True)
    assert [row["text"] for row in first] == ["text 4", "text 3"]
    operator(tree, rows.state_key("text 3"), "agree", against_open=False, stratum="recent", by="english")
    found, _summary = review_data.recent_rows(snap(tree), 2, todo=True, after=summary["next"])
    assert [row["text"] for row in found] == ["text 2", "text 1"]
    assert [row["text"] for row in review_data.recent_rows(snap(tree), 2, todo=True, offset=2)[0]] == ["text 1", "text 0"], (
        "what the count did"
    )


def test_a_cursor_holds_its_place_when_a_new_verdict_lands(tree):
    for i in range(4):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    _first, summary = review_data.recent_rows(snap(tree), 2)
    record(tree, "fresh", 0.3, clock=at(9), done_threshold=0.4)
    record(tree, "text 0", 0.3, clock=at(8), done_threshold=0.4)  # judged again: its row moves up to its newest decision
    found, summary = review_data.recent_rows(snap(tree), 2, after=summary["next"])
    assert [row["text"] for row in found] == ["text 1"] and summary["next"] is None


def test_a_cursor_pages_through_the_edge_order_once_each_ties_newest_first(tree):
    for i, p in enumerate((0.02, 0.68, 0.99, 0.41, 0.705, 0.39, 0.72)):
        record(tree, f"p {p}", p, clock=at(i), done_threshold=0.4)
    every = [row["text"] for row in review_data.recent_rows(snap(tree), 100, "edge")[0]]
    walked = pages(tree, 2, "edge")
    assert [text for page in walked for text in page] == every and len(walked) == 4
    assert every[1:3] == ["p 0.39", "p 0.41"], "0.01 from ✅ both: the newer first"


def test_a_cursor_names_one_order(tree):
    for i in range(3):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    newest = review_data.recent_rows(snap(tree), 1)[1]["next"]
    edge = review_data.recent_rows(snap(tree), 1, "edge")[1]["next"]
    assert newest.startswith("n~") and edge.startswith("e~0.100000~")
    assert review_data.parse_recent_cursor(newest, "edge") is None and review_data.parse_recent_cursor(edge, "newest") is None
    for bad in ("", "n~", "n~2026-10-27T12:00:00Z~XYZ", f"{newest}\n", "e~0.1~2026-10-27T12:00:00Z~0123456789abcdef"):
        assert review_data.parse_recent_cursor(bad, "newest") is None and review_data.parse_recent_cursor(bad, "edge") is None
    with pytest.raises(ValueError):
        review_data.recent_rows(snap(tree), 1, "edge", after=newest)


def test_a_negative_offset_is_a_bug_never_a_slice_from_the_oldest_end(tree):
    record(tree, "text", 0.3, clock=at(1), done_threshold=0.4)
    with pytest.raises(ValueError):
        review_data.recent_rows(snap(tree), 100, offset=-1)


@pytest.mark.parametrize(
    "p, open_at, done_at, cut, delta",
    [
        (0.68, 0.7, 0.4, "open", -0.02),  # 0.02 short of a ⚠️
        (0.7, 0.7, 0.4, "open", 0.0),  # a ⚠️ by nothing
        (0.41, 0.7, 0.4, "done", 0.01),  # 0.01 short of a ✅
        (0.55, 0.7, 0.4, "open", -0.15),  # the midpoint ties to the costlier mark
        (0.05, 0.7, 0.4, "done", -0.35),
        (0.36, 0.35, 0.5, "open", 0.01),  # the done cut never passes the open cut
    ],
)
def test_edge_of(p, open_at, done_at, cut, delta):
    found_cut, found_delta = review_data.edge_of(p, open_at, done_at)
    assert found_cut == cut and found_delta == pytest.approx(delta)


def test_edge_order_ranks_every_verdict_by_its_distance_to_a_cut(tree):
    record(tree, "far done", 0.02, clock=at(1), done_threshold=0.4)
    record(tree, "almost a warning", 0.68, clock=at(2), done_threshold=0.4)
    record(tree, "far open", 0.99, clock=at(3), done_threshold=0.4)
    record(tree, "almost a check", 0.41, clock=at(4), done_threshold=0.4)
    record(tree, "barely a warning", 0.705, clock=at(5), done_threshold=0.4)
    record(tree, "old almost a check", 0.39, clock=at(0))  # predates the band: graded at today's done cut
    found, summary = review_data.recent_rows(snap(tree), 100, "edge")
    assert [row["text"] for row in found] == [
        "barely a warning",
        "almost a check",
        "old almost a check",
        "almost a warning",
        "far open",
        "far done",
    ]
    assert [row["edge"] for row in found[:4]] == [
        {"cut": "open", "delta": 0.005},
        {"cut": "done", "delta": 0.01},
        {"cut": "done", "delta": -0.01},
        {"cut": "open", "delta": -0.02},
    ]
    assert summary["order"] == "edge" and summary["pool"] == 6
    newest, newest_summary = review_data.recent_rows(snap(tree), 100)
    assert newest[0]["text"] == "barely a warning" and newest[1]["text"] == "almost a check"
    assert newest_summary["order"] == "newest" and newest[0]["edge"] == found[0]["edge"]


def test_edge_order_reaches_past_the_newest_page(tree):
    """The nearest-a-cut turn is found wherever it sits in the ledger, not only among the newest RECENT_MAX."""
    record(tree, "the one edge case", 0.7, clock=at(0), done_threshold=0.4)
    for i in range(review_data.RECENT_MAX + 1):
        record(
            tree,
            f"far {i}",
            0.99,
            clock=lambda i=i: datetime(2026, 10, 27, 13, tzinfo=timezone.utc) + timedelta(minutes=i),
            done_threshold=0.4,
        )
    assert all(row["text"] != "the one edge case" for row in review_data.recent_rows(snap(tree), review_data.RECENT_MAX)[0])
    assert review_data.recent_rows(snap(tree), 1, "edge")[0][0]["text"] == "the one edge case"


def test_an_offset_pages_on_through_the_edge_order(tree):
    for i, p in enumerate((0.02, 0.68, 0.99, 0.41, 0.705, 0.55)):
        record(tree, f"p {p}", p, clock=at(i), done_threshold=0.4)
    every, _summary = review_data.recent_rows(snap(tree), 100, "edge")
    found, summary = review_data.recent_rows(snap(tree), 2, "edge", offset=2)
    assert [row["text"] for row in found] == [row["text"] for row in every[2:4]]
    assert summary["order"] == "edge" and summary["offset"] == 2 and summary["pool"] == 6


def test_todo_leaves_answered_texts_out_before_the_page_is_cut(tree):
    record(tree, "graded edge", 0.7, clock=at(1), done_threshold=0.4)
    record(tree, "skipped edge", 0.69, clock=at(2), done_threshold=0.4)
    record(tree, "next edge", 0.66, clock=at(3), done_threshold=0.4)
    record(tree, "far", 0.99, clock=at(4), done_threshold=0.4)
    operator(tree, rows.state_key("graded edge"), "agree", against_open=True, stratum="flagged", by="english")
    operator(tree, rows.state_key("skipped edge"), "skip", against_open=False, stratum="recent", by="english")
    found, summary = review_data.recent_rows(snap(tree), 1, "edge", todo=True)
    assert [row["text"] for row in found] == ["next edge"] and summary["pool"] == 2 and summary["todo"] is True
    assert review_data.recent_rows(snap(tree), 1, "edge")[0][0]["text"] == "graded edge", "without todo, answers stay"


def test_todo_drops_answered_texts_before_the_offset_is_counted(tree):
    for i in range(5):
        record(tree, f"text {i}", 0.3, clock=at(i), done_threshold=0.4)
    for answered in ("text 4", "text 2"):
        operator(tree, rows.state_key(answered), "agree", against_open=False, stratum="recent", by="english")
    found, summary = review_data.recent_rows(snap(tree), 1, todo=True, offset=1)
    assert [row["text"] for row in found] == ["text 1"], "the pool is text 3, 1, 0: the offset skips text 3 alone"
    assert summary["pool"] == 3 and summary["todo"] is True and summary["offset"] == 1


def test_an_abstain_is_not_a_recent_verdict(tree):
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    book.record_decision(
        {"surface": SURFACE},
        clean_request({"state": "no verdict", "questions": {"q": NOUL}}, frozenset({"english"})),
        {"answers": {}},
        "english",
        None,
        None,
        1,
    )
    assert review_data.recent_rows(snap(tree), 100)[0] == []


# -- the acquisition strata ---------------------------------------


def pick_where(
    prefix: str, n: int, *, live: bool = False, unflagged: bool | None = False, sample: bool | None = None
) -> list[str]:
    """`n` texts by hash: in `live_sample` or not, in the unflagged sample or not (None: either), in Sonnet's sample or not."""
    found, i = [], 0
    while len(found) < n:
        text = f"{prefix} {i}: some closing message long enough to read"
        key = rows.state_key(text)
        if (
            (rows.bucket(key, "review-live") < 10) == live
            and (unflagged is None or (rows.bucket(key, "review-unflagged") < 20) == unflagged)
            and (sample is None or (rows.bucket(key, "review-sample") < 50) == sample)
        ):
            found.append(text)
        i += 1
    return found


def keys_of(texts):
    return [rows.state_key(text) for text in texts]


def test_live_sample_draws_live_verdicts_by_hash_ahead_of_every_ledger_stratum(tree):
    live = pick_where("live", 3, live=True, unflagged=None)
    other = pick_where("other", 2)
    for text in live + other:
        record(tree, text, 0.95)
    [low] = pick_where("live low", 1, live=True, unflagged=None)
    record(tree, low, 0.05)
    view = snap(tree)
    found = strata(view)
    assert {found[k] for k in keys_of(live + [low])} == {"live_sample"} and {found[k] for k in keys_of(other)} == {"flagged"}
    assert view.remaining["live_sample"] == sorted(keys_of(live + [low]), key=lambda k: rows.bucket(k, "review-live-order"))
    # Totals still count every verdict, so recall's populations are whole; a live member under the cut is a uniform draw.
    assert view.items.flagged_total == 5 and view.items.unflagged_total == 1
    assert rows.state_key(low) in view.items.unflagged_sampled
    assert set(keys_of(live)) <= view.items.flagged_far and not view.items.flagged_near, "p 0.95 is far from a cut"


def test_a_sonnet_sample_key_stays_in_sample_whatever_its_live_hash(tree):
    [text] = pick_where("both", 1, live=True, unflagged=None, sample=True)
    [key] = tree.unlabelled([text])
    tree.labels({key: True})
    record(tree, text, 0.9)
    assert strata(snap(tree)) == {key: "sample"}
    assert review_data.is_live_member(REVIEW_SURFACES[SURFACE], key), "P1-15 reads membership, not the stratum"


def test_uncertain_takes_near_cut_texts_from_flagged_recent_and_rest_never_from_a_sample(tree):
    near_flag, far_flag = pick_where("near flag", 1), pick_where("far flag", 1)
    near_recent, near_done, middle = pick_where("near recent", 1), pick_where("near done", 1), pick_where("middle", 1)
    [sampled] = pick_where("sampled", 1, unflagged=True)
    for text, p in [
        (near_flag[0], 0.72),
        (far_flag[0], 0.95),
        (near_recent[0], 0.62),
        (near_done[0], 0.45),
        (middle[0], 0.55),
        (sampled, 0.62),
    ]:
        record(tree, text, p)
    rest_text, sample_text = pick("rest", "review-sample", False, 1)[0], pick("smp", "review-sample", True, 1)[0]
    rest_key, sample_key = tree.unlabelled([rest_text, sample_text])
    tree.labels({rest_key: True, sample_key: True})
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [lab_files.scores_row(key, 0.74, "english", None, 1, 0.7, "2026-10-27T00:00:00Z") for key in (rest_key, sample_key)],
    )
    found = strata(snap(tree))
    assert {found[k] for k in keys_of(near_flag + near_recent + near_done)} == {"uncertain"}
    assert found[rest_key] == "uncertain" and found[sample_key] == "sample"
    assert found[rows.state_key(far_flag[0])] == "flagged" and found[rows.state_key(middle[0])] == "recent", "0.15 from both cuts"
    assert found[rows.state_key(sampled)] == "unflagged", "the unflagged sample keeps its members: recall reads it"
    card = review_data.card(snap(tree), rows.state_key(near_done[0]))
    assert card["stratum"] == "uncertain" and card["edge"] == {"cut": "done", "delta": 0.05}


def test_uncertain_deals_held_out_first_then_the_short_class_cut_then_nearest(tree):
    a, b, c, d, trained = pick_where("u", 5)
    for text, p in [(a, 0.69), (b, 0.41), (c, 0.65), (d, 0.45), (trained, 0.70)]:
        record(tree, text, p)
    tree.register(train_texts=[trained])
    order = lambda: snap(tree).remaining["uncertain"]  # noqa: E731
    # No clean labels: both classes short by 10, and a tie leans OPEN, so the ⚠️ cut leads.
    assert snap(tree).deficit == "open"
    assert order() == keys_of([a, c, b, d, trained]), "the trained text last: a label on it is never clean"
    opens = tree.unlabelled([f"open label {i}: phase {i} is next" for i in range(10)])
    for key in opens:
        operator(tree, key, "agree", against_open=True)
    assert snap(tree).deficit == "done" and review_data.stats(snap(tree), tree.base, now=NOW)["deficit"] == "done"
    assert order() == keys_of([b, d, a, c, trained])


@pytest.mark.parametrize(
    "n_open, n_done, short", [(2, 11, "open"), (12, 3, "done"), (10, 10, None), (0, 0, "open"), (9, 8, "done")]
)
def test_class_deficit(n_open, n_done, short):
    assert review_data.class_deficit({"open": n_open, "done": n_done}) == short


def test_suspect_ranked_is_the_confident_learning_off_diagonal():
    teacher = {
        "a": (True, 0.9),
        "b": (True, 0.8),
        "c": (True, 0.2),
        "d": (False, 0.9),
        "e": (False, 0.7),
        "f": (False, 0.3),
        "g": (True, 0.55),
    }
    # thresholds: open (0.9+0.8+0.2+0.55)/4 = 0.6125, done (0.9+0.7+0.3)/3 = 0.633
    assert review_data.suspect_ranked(teacher) == ["c", "f"]
    assert review_data.suspect_ranked({}) == []
    assert review_data.suspect_ranked({"x": (True, 0.1)}) == [], "no other class to clear a threshold"


def test_suspect_reads_the_teacher_pair_from_any_checkpoint_and_ignores_a_stale_label(tree):
    texts = pick("sus", "review-sample", False, 5)
    keys = tree.unlabelled(texts)
    tree.labels({keys[0]: True, keys[1]: True, keys[2]: False, keys[3]: False, keys[4]: True})
    stamp = "2026-10-27T00:00:00Z"
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [
            lab_files.scores_row(keys[0], 0.95, "english", None, 1, 0.7, stamp, teacher=(True, 0.9)),
            lab_files.scores_row(keys[1], 0.95, "english", "an-older-checkpoint", 1, 0.7, stamp, teacher=(True, 0.1)),
            lab_files.scores_row(keys[2], 0.05, "english", None, 1, 0.7, stamp, teacher=(False, 0.9)),
            lab_files.scores_row(keys[3], 0.05, "english", None, 1, 0.7, stamp, teacher=(False, 0.8)),
            lab_files.scores_row(keys[4], 0.95, "english", None, 1, 0.7, stamp, teacher=(False, 0.05)),  # Sonnet relabelled since
        ],
    )
    found = strata(snap(tree))
    assert found[keys[1]] == "suspect" and found[keys[4]] == "rest"
    assert [found[keys[i]] for i in (0, 2, 3)] == ["rest"] * 3


def test_a_malformed_teacher_pair_leaves_the_rows_own_score(tree):
    key = tree.unlabelled(["one text"])[0]
    row = {**lab_files.scores_row(key, 0.3, "english", None, 1, 0.7, "2026-10-27T00:00:00Z"), "teacher_open": True}
    tree.write_jsonl("data/agent-open-work-laya-p.jsonl", [row])
    [parsed], code = lab_files.read_scores(tree.base, "agent-open-work")
    assert code == "ok" and parsed["p"] == 0.3 and "teacher_open" not in parsed


# -- waves -----------------------------------------------------------------------------


def wave_tree(tree, subject="vc-1"):
    """vc-1 ends turns at 12:00, :02, :04 (⚠️), :05 (✅), :06, :08 (⚠️) and :50 (⚠️, past the gap); vc-2 once at :01."""
    texts = pick_where("wave", 8)
    plan = [(0, 0.9), (2, 0.9), (4, 0.9), (5, 0.1), (6, 0.9), (8, 0.9), (50, 0.9)]
    for text, (minute, p) in zip(texts, plan):
        record(tree, text, p, clock=at(minute), subject=subject, done_threshold=0.4)
    record(tree, texts[7], 0.9, clock=at(1), subject="vc-2")
    return keys_of(texts)


def test_a_wave_is_adjacent_same_mark_turn_ends_of_one_session_inside_the_gap(tree):
    keys = wave_tree(tree)
    view = snap(tree)
    assert view.items.runs == [tuple(keys[0:3]), tuple(keys[4:6])]
    assert view.waves == {keys[2]: tuple(keys[0:3]), keys[5]: tuple(keys[4:6])}, "the newest of one stratum leads"
    assert keys[0] not in view.remaining["flagged"] and keys[2] in view.remaining["flagged"]
    block = review_data.card(view, keys[2])["wave"]
    assert block["n"] == 3 and [m["key"] for m in block["members"]] == keys[0:2]
    assert block["members"][0]["at"] == "2026-10-27T12:00:00Z" and block["members"][0]["p"] == 0.9
    assert review_data.card(view, keys[0])["wave"] is None, "a rider is still a card of its own on RECENT"
    assert review_data.wave_members(view, keys[2]) == tuple(keys[0:2])


def test_an_answered_member_leaves_its_wave_and_a_lone_one_is_no_wave(tree):
    keys = wave_tree(tree)
    operator(tree, keys[1], "agree", stratum="flagged", by="english")
    assert snap(tree).waves[keys[2]] == (keys[0], keys[2])
    operator(tree, keys[0], "agree", stratum="flagged", by="english")
    assert keys[2] not in snap(tree).waves


def test_a_conversation_beats_the_rack_name_and_a_reused_name_splits_on_the_gap(tree):
    texts = pick_where("conv", 3)
    record(tree, texts[0], 0.9, clock=at(0), subject="vc-1", conversation="019a5f3e-7b2c-7d41-9e8f-0123456789ab")
    record(tree, texts[1], 0.9, clock=at(1), subject="vc-1", conversation="0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab")
    record(tree, texts[2], 0.9, clock=at(2), subject="vc-1", conversation="0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab")
    assert snap(tree).items.runs == [tuple(keys_of(texts[1:]))]


def test_the_uniform_samples_never_ride_and_the_best_stratum_leads(tree):
    [live] = pick_where("w live", 1, live=True, unflagged=None)
    near, far = pick_where("w near", 1)[0], pick_where("w far", 1)[0]
    for minute, (text, p) in enumerate([(far, 0.95), (live, 0.95), (near, 0.72)]):
        record(tree, text, p, clock=at(minute), subject="vc-3")
    view = snap(tree)
    assert view.items.runs == [tuple(keys_of([far, live, near]))]
    assert view.waves == {rows.state_key(near): keys_of_tuple([far, near])}, "uncertain leads; live_sample is graded alone"


def keys_of_tuple(texts):
    return tuple(keys_of(texts))


def test_the_unflagged_sample_never_joins_a_wave_recall_reads_it(tree):
    """`unflagged` is recall's unflagged side: as a lead or a rider, it would take a verdict from another text's look."""
    [sampled] = pick_where("wu sampled", 1, unflagged=True)
    first, last = pick_where("wu recent", 2)
    for minute, text in enumerate([first, sampled, last]):
        record(tree, text, 0.55, clock=at(minute), subject="vc-5")  # 💬, 0.15 from both cuts
    view = snap(tree)
    assert view.items.runs == [keys_of_tuple([first, sampled, last])]
    assert strata(view)[rows.state_key(sampled)] == "unflagged"
    assert view.waves == {rows.state_key(last): keys_of_tuple([first, last])}
    assert rows.state_key(sampled) in view.remaining["unflagged"], "dealt as a card of its own"


def test_a_run_past_wave_max_deals_as_more_cards(tree, monkeypatch):
    monkeypatch.setattr(review_data, "WAVE_MAX", 2)
    texts = pick_where("long", 5)
    for minute, text in enumerate(texts):
        record(tree, text, 0.9, clock=at(minute), subject="vc-4")
    keys = keys_of(texts)
    assert snap(tree).waves == {keys[1]: tuple(keys[0:2]), keys[3]: tuple(keys[2:4])}


# -- recall by population, and the PROMOTE guard -------------------------------------


def test_recall_counts_answers_by_population_whatever_stratum_they_recorded(tree):
    near = pick_where("rn", 5)
    for text in near:
        record(tree, text, 0.72)  # flagged, but `uncertain`
    low = pick_where("rl", 5, unflagged=True)
    for text in low:
        record(tree, text, 0.05)
    for key in keys_of(near):
        operator(tree, key, "agree", against_open=True, stratum="uncertain", by="english")
    for i, key in enumerate(keys_of(low)):
        operator(tree, key, "flip" if i == 0 else "agree", against_open=False, stratum="unflagged", by="english")
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    assert estimate(stats["recall_est"]) == {"n_flagged": 5, "n_unflagged": 5, "value": round(5 / 6, 3)}
    assert stats["by_stratum"]["uncertain"] == {"reviewed": 5, "remaining": 0, "open": 5}, "the stratum's OPEN yield"
    assert stats["by_stratum"]["unflagged"]["open"] == 1


def flagged_tree(tree, n_near, n_far, n_low=5):
    """`n_near` flagged texts near the cut (p 0.72, dealt `uncertain`), `n_far` far over it (0.95, `flagged`), and
    `n_low` under it in the unflagged sample (0.05)."""
    near, far = pick_where("strat near", n_near), pick_where("strat far", n_far)
    low = pick_where("strat low", n_low, unflagged=True)
    for texts, p in ((near, 0.72), (far, 0.95), (low, 0.05)):
        for text in texts:
            record(tree, text, p)
    return keys_of(near), keys_of(far), keys_of(low)


def grade(tree, keys, n_open, stratum):
    """The operator's answers on `keys`: the first `n_open` OPEN, the rest DONE."""
    for i, key in enumerate(keys):
        operator(tree, key, "agree" if i < n_open else "flip", against_open=True, stratum=stratum, by="english")


def test_recall_stratifies_the_flagged_side_on_the_decks_near_far_split(tree):
    """The deck deals near-cut flagged texts at a heavier weight, so most flagged answers sit where OPEN is rarest."""
    near, far, low = flagged_tree(tree, 10, 10)
    view = snap(tree)
    assert view.items.flagged_near == frozenset(near) and view.items.flagged_far == frozenset(far)
    grade(tree, near[:8], 1, "uncertain")  # near: 1 of 8 OPEN
    grade(tree, far[:4], 4, "flagged")  # far: 4 of 4
    grade(tree, low, 1, "unflagged")  # unflagged: 1 of 5
    recall = lambda: estimate(review_data.stats(snap(tree), tree.base, now=NOW)["recall_est"])  # noqa: E731
    assert recall() == {"n_flagged": 12, "n_unflagged": 5, "value": None}, "12 flagged answers, but only 4 far"
    grade(tree, far[4:5], 1, "flagged")
    # tp = 10·1/8 + 10·5/5 = 11.25, fn = 5·1/5 = 1. Pooled, 20·6/13 = 9.23 read recall 0.902.
    assert recall() == {"n_flagged": 13, "n_unflagged": 5, "value": round(11.25 / 12.25, 3)}
    assert recall()["value"] != round((20 * 6 / 13) / (20 * 6 / 13 + 1), 3)


def test_a_flagged_part_smaller_than_the_minimum_counts_once_it_is_all_graded(tree):
    near, far, low = flagged_tree(tree, 3, 6)
    grade(tree, far[:5], 5, "flagged")
    grade(tree, low, 1, "unflagged")
    grade(tree, near[:2], 0, "uncertain")
    recall = lambda: review_data.stats(snap(tree), tree.base, now=NOW)["recall_est"]["value"]  # noqa: E731
    assert recall() is None, "2 of the 3 near texts graded"
    grade(tree, near[2:], 0, "uncertain")
    assert recall() == round(6 / 7, 3), "a whole census is exact: tp = 0 + 6·5/5, fn = 1"


# -- precision per landing class, recall per audience -------------------


def land(tree, decision_id, landing_class, held=None, asked=None):
    """The rack's landing row for one decision (P1-20), through the plugin's own writer. `asked`: the turn's trace,
    with whether its hand-off ended on a question (None: the bridge read no transcript)."""
    trace = None if asked is None else {"ran_tests": False, "edited_files": 0, "opened_pr": False, "ended_on_question": asked}
    landing.LandingLog(lambda: tree.base, clock=lambda: NOW).record(
        landing.clean_landing(
            {
                "decision_id": decision_id,
                "surface": SURFACE,
                "class": landing_class,
                "held": held,
                "running": {"descendants": 0, "agents": 0, "workflows": 0, "shells": 0},
                "trace": trace,
            }
        )
    )


def live_grade(tree, key, is_open, decision_id, stratum="live_sample"):
    operator(tree, key, "agree" if is_open else "flip", against_open=True, stratum=stratum, by="english", decision_id=decision_id)


def rates(tree):
    stats = review_data.stats(snap(tree), tree.base, now=NOW)
    return stats["precision"], stats["recall_est"]["by_audience"]


def test_precision_is_the_operators_grades_of_shown_warnings_in_the_live_sample_per_landing_class(tree):
    """Of the ⚠️ the rack SHOWED, the share the operator called open: on the uniform live sample only, split by where
    each landed, and never read off dismissals."""
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    texts = pick_where("pl", 14, live=True, unflagged=None)
    ids = [record(tree, text, 0.95) for text in texts]
    for decision_id in ids:
        book.react(decision_id, "shown")
    classes = ["new"] * 4 + ["repeat"] * 3 + ["held"] * 5 + ["retired", None]  # the last one never landed
    for decision_id, landing_class in zip(ids, classes):
        if landing_class is not None:
            land(tree, decision_id, landing_class, "children" if landing_class == "held" else None)
    keys = keys_of(texts)
    for key, decision_id, is_open in zip(keys[:4], ids[:4], (True, True, True, False)):
        live_grade(tree, key, is_open, decision_id)  # new: 3 of 4 open
    for key, decision_id in zip(keys[4:6], ids[4:6]):
        live_grade(tree, key, True, decision_id)  # repeat: 2 of 2 (one ungraded)
    for i, (key, decision_id) in enumerate(zip(keys[7:12], ids[7:12])):
        live_grade(tree, key, i == 0, decision_id)  # held: 1 of 5
    precision, _ = rates(tree)
    assert precision["basis"] == "live_sample" and precision["min_graded"] == 5
    assert (precision["pool"], precision["n"], precision["value"]) == (14, 11, round(6 / 11, 3))
    free = precision["free"]
    assert (free["pool"], free["n"], free["value"]) == (7, 6, round(5 / 6, 3)), "free rows: new + repeat"
    assert free["lo"] < free["value"] < free["hi"]
    assert {name: (b["pool"], b["n"], b["value"]) for name, b in precision["by_class"].items()} == {
        "new": (4, 4, None),
        "repeat": (3, 2, None),
        "held": (5, 5, 0.2),
        "retired": (1, 0, None),
        "unknown": (1, 0, None),
    }, "under 5 graded a class reads none, never a number"

    # None of these moves it: a dismissal, a shown ⚠️ outside the sample, a sample ⚠️ the rack never showed.
    book.react(ids[0], "dismissed")
    for text in pick_where("po", 3):
        outside = record(tree, text, 0.95)
        book.react(outside, "shown")
        land(tree, outside, "new")
        live_grade(tree, rows.state_key(text), False, outside, stratum="flagged")
    [unshown] = pick_where("pu", 1, live=True, unflagged=None)
    live_grade(tree, rows.state_key(unshown), False, record(tree, unshown, 0.95))
    again, _ = rates(tree)
    assert {name: again[name] for name in ("pool", "n", "value", "free", "by_class")} == {
        name: precision[name] for name in ("pool", "n", "value", "free", "by_class")
    }


def test_a_live_member_counts_whichever_stratum_its_grade_recorded(tree):
    """Membership is the key's hash: a member graded from RECENT, or as `sample` (a Sonnet label put it there first),
    is still a uniform draw."""
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    texts = pick_where("ps", 5, live=True, unflagged=None)
    for i, text in enumerate(texts):
        decision_id = record(tree, text, 0.95)
        book.react(decision_id, "shown")
        land(tree, decision_id, "new")
        live_grade(tree, rows.state_key(text), i < 4, decision_id, stratum=("recent", "sample", "live_sample")[i % 3])
    precision, _ = rates(tree)
    assert (precision["free"]["n"], precision["free"]["value"]) == (5, 0.8)


def test_recall_per_audience_reads_the_decision_each_grade_recorded(tree):
    """Of the turns the operator called open, the share Laya flagged, per row the verdict landed on; a text judged twice
    is read at the decision its card showed, and is one ⚠️ in precision's pool."""
    texts = pick_where("ra", 12, live=True, unflagged=None)
    ps = [0.95] * 4 + [0.05] * 2 + [0.95] * 2 + [0.05] * 3 + [0.95]
    ids = [record(tree, text, p) for text, p in zip(texts, ps)]
    for i, decision_id in enumerate(ids[:11]):
        flagged = ps[i] >= 0.7
        held = "children" if i >= 6 else None
        land(tree, decision_id, ("held" if held else "new") if flagged else None, held)
    for text, decision_id in zip(texts, ids):
        live_grade(tree, rows.state_key(text), True, decision_id)
    _, by_audience = rates(tree)
    assert {name: (b["n"], b["value"]) for name, b in by_audience.items()} == {
        "free": (6, round(4 / 6, 3)),
        "background": (0, None),
        "children": (5, 0.4),
        "unknown": (1, None),
    }

    # The first free text judged again, later and under the cut: its grade recorded the first decision, a ⚠️.
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    book.react(ids[0], "shown")
    rejudged = record(tree, texts[0], 0.05, clock=lambda: NOW + timedelta(hours=1))
    land(tree, rejudged, None)
    _, by_audience = rates(tree)
    assert by_audience["free"]["value"] == round(4 / 6, 3), "read at the ⚠️ the card showed, not the newer verdict"
    # A text shown as ⚠️ twice is one ⚠️ graded once.
    book.react(ids[1], "shown")
    book.react(record(tree, texts[1], 0.95, clock=lambda: NOW + timedelta(hours=2)), "shown")
    precision, _ = rates(tree)
    assert precision["pool"] == precision["n"] == 2


def test_a_landing_row_that_arrives_later_moves_its_verdict_out_of_unknown(tree):
    """The deck's memo watches the landing record, so the row the rack posts a second after the verdict is read."""
    [text] = pick_where("late", 1, live=True, unflagged=None)
    decision_id = record(tree, text, 0.95)
    Ledger(lambda: tree.base, clock=lambda: NOW).react(decision_id, "shown")
    first, _ = rates(tree)
    assert (first["by_class"]["unknown"]["pool"], first["by_class"]["new"]["pool"]) == (1, 0)
    assert snap(tree).items.sources["landing"] == files.MISSING
    land(tree, decision_id, "new")
    later, _ = rates(tree)
    assert (later["by_class"]["unknown"]["pool"], later["by_class"]["new"]["pool"]) == (0, 1)
    assert snap(tree).items.sources["landing"] == files.OK


def test_promote_offers_the_train_half_and_never_a_reserve_flip(tree):
    """The label budget replaced the keep-ten-per-class guard: a clean TRAIN flip is offered even when it is one of
    the only clean labels of its class, and no reserve flip ever is."""
    train, reserve = in_half(label_budget.TRAIN, "flip", 2), in_half(label_budget.RESERVE, "flip", 3)
    keys = tree.unlabelled(train + reserve)  # one export: each call rewrites it
    train_keys, reserve_keys, spent = keys[:2], keys[2:4], keys[4]
    tree.register(train_texts=[reserve[2]])  # drawn RESERVE, but a checkpoint trained on it: spent, so TRAIN
    for key in keys:
        operator(tree, key, "flip", against_open=False)  # records OPEN
    ready = review_data.sweep_readiness(tree.base, SURFACE)
    assert (ready["rows"], ready["open"]) == (4, 4) and ready["reserve"] == {"rows": 2, "open": 2, "done": 0}
    assert review_data.promotion_candidates(tree.base, SUITE) == sorted([*train_keys, spent])
    assert review_data.reserved_flips(tree.base, SUITE) == sorted(reserve_keys)
    assert review_data.reserved_flips(tree.base, "no-such-suite") == []


# -- the four-way grade and the blind retest -----------------------------

#: A stamp after READ became a grade, so a bare DONE counts as DONE (review_data.py READ_SINCE).
AFTER_READ = "2026-11-20T01:02:03Z"


def three_way_labels(tree, done=0, running=0, you=0, unsplit=0, read=0, at=AFTER_READ):
    """Operator labels on keys no card holds: the four-way counts read the fold, not the deck."""
    made = []
    for needs, n in ((False, done), ("read", read), ("running", running), ("you", you), (None, unsplit)):
        for _ in range(n):
            key = review_log.new_answer_id()[2:]
            row = review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at=at,
                surface=SURFACE,
                key=key,
                stratum="recent",
                answer="flip" if needs in (False, "read") else "agree",
                against={"by": "english", "open": True},
                laya=None,
                decision_id=None,
                held_out=True,
                needs=needs or None,
            )
            tree.path("data/x")
            tree.append_jsonl("data/agent-open-work-operator.jsonl", row)
            made.append(row)
    return made


def three_way_now(tree):
    return review_data.stats(snap(tree), tree.base, now=NOW)["three_way"]


def test_three_way_counts_the_live_labels_per_class_and_names_the_shortest(tree):
    tree.path("data/x")
    empty = {"done": 0, "read": 0, "running": 0, "you": 0, "min": 20, "deficit": "you", "lean": "you"}
    assert three_way_now(tree) == empty, "a tie goes to you"
    three_way_labels(tree, done=30, read=25, running=5, you=5, unsplit=9)
    short = {"done": 30, "read": 25, "running": 5, "you": 5, "min": 20, "deficit": "you", "lean": "you"}
    assert three_way_now(tree) == short, "an unsplit OPEN is no class"
    three_way_labels(tree, you=1)
    assert three_way_now(tree)["deficit"] == "running" and three_way_now(tree)["lean"] == "running"
    three_way_labels(tree, running=15, you=14)
    full = {"done": 30, "read": 25, "running": 20, "you": 20, "min": 20, "deficit": None, "lean": None}
    assert three_way_now(tree) == full


def test_a_done_from_before_read_is_no_class_and_read_is_a_done_of_its_own(tree):
    """READ_SINCE: a bare DONE recorded before READ existed could not say which DONE it was. A READ is a DONE to the
    open/done label (its `open` is false) and its own class to the four-way grade."""
    old = three_way_labels(tree, done=4, at="2026-11-18T23:59:59Z")
    three_way_labels(tree, done=1, read=21, running=20, you=20)
    assert three_way_now(tree) == {"done": 1, "read": 21, "running": 20, "you": 20, "min": 20, "deficit": "done", "lean": None}
    assert all(snap(tree).fold.labels[row["key"]]["open"] is False for row in old), "the open/done label keeps them"
    assert review_data.grade_of({"open": True, "needs": "read", "at": AFTER_READ}) is None, "READ rides only beside DONE"
    assert review_data.grade_of({"open": False, "needs": "you", "at": AFTER_READ}) is None, "NEEDS YOU only beside OPEN"


def test_the_gate_names_the_shortest_class_and_the_lean_the_shortest_open_half(tree):
    three_way_labels(tree, done=1, read=2, running=3, you=2)
    mixed = {"done": 1, "read": 2, "running": 3, "you": 2, "min": 20, "deficit": "done", "lean": "you"}
    assert three_way_now(tree) == mixed, "DONE is the gate's shortest; the deck leans toward a half"
    three_way_labels(tree, running=17, you=18)
    after = {"done": 1, "read": 2, "running": 20, "you": 20, "min": 20, "deficit": "done", "lean": None}
    assert three_way_now(tree) == after


def test_the_needs_hint_reads_where_a_verdict_landed_and_nothing_else():
    asked = {"ran_tests": False, "edited_files": 0, "opened_pr": False, "ended_on_question": True}
    assert review_data.needs_hint(None) is None
    held = {"class": "held", "held": "children", "trace": asked}
    assert review_data.needs_hint(held) == "running", "held wins: it was waiting on its own"
    assert review_data.needs_hint({"class": None, "held": "background", "trace": None}) == "running"
    assert review_data.needs_hint({"class": "new", "held": None, "trace": asked}) == "you"
    assert review_data.needs_hint({"class": "new", "held": None, "trace": {**asked, "ended_on_question": False}}) is None
    assert review_data.needs_hint({"class": "new", "held": None, "trace": None}) is None


def lean_tree(tree):
    """Four flagged live verdicts far from the cut, oldest first: one held for its children, two free that ended on
    a question, one free that did not; and two backlog texts behind them."""
    texts = pick_where("lean", 4, live=False, unflagged=None)
    ids = [record(tree, text, 0.95, clock=lambda i=i: NOW + timedelta(minutes=i)) for i, text in enumerate(texts)]
    land(tree, ids[0], "held", "children", asked=False)
    land(tree, ids[1], "new", asked=True)
    land(tree, ids[2], "new", asked=True)
    land(tree, ids[3], "new", asked=False)
    rest = pick_where("lean rest", 2, live=False, unflagged=None, sample=False)
    tree.unlabelled(rest)
    tree.labels(dict.fromkeys(keys_of(rest), True))
    return keys_of(texts)


def test_the_live_lanes_lean_toward_the_short_three_way_class_until_each_holds_its_floor(tree):
    running, you_old, you_new, plain = lean_tree(tree)
    assert snap(tree).needs_short == "you"
    assert snap(tree).remaining["flagged"] == [you_new, you_old, plain, running], "the hinted cards, then newest first"
    # `flagged` deals its hinted cards past its weight of one, and the card behind them at that weight again.
    backlog = snap(tree).remaining["rest"]
    assert [c["key"] for c in review_data.queue(snap(tree), 5)] == [you_new, you_old, backlog[0], plain, backlog[1]]
    # DONE the single shortest class (done 1, running 3, you 2): the halves are still under the floor, so it leans on.
    three_way_labels(tree, done=1, read=2, running=3, you=2)
    assert snap(tree).grades["deficit"] == "done" and snap(tree).needs_short == "you"
    assert snap(tree).remaining["flagged"] == [you_new, you_old, plain, running], "the hinted NEEDS YOU cards lead"
    assert [c["key"] for c in review_data.queue(snap(tree), 5)] == [you_new, you_old, backlog[0], plain, backlog[1]]
    three_way_labels(tree, you=18)
    assert snap(tree).grades["deficit"] == "done" and snap(tree).needs_short == "running"
    assert snap(tree).remaining["flagged"] == [running, plain, you_new, you_old]
    three_way_labels(tree, running=17)
    assert snap(tree).grades["deficit"] == "done", "the gate still waits on DONE"
    assert snap(tree).needs_short is None, "both halves hold the floor: a short DONE is the open/done deficit's lean"
    assert snap(tree).remaining["flagged"] == [plain, you_new, you_old, running], "newest first again"
    assert [c["stratum"] for c in review_data.queue(snap(tree), 4)] == ["flagged", "rest", "flagged", "rest"]


def test_no_card_shows_the_hint_and_a_sample_is_never_reordered_by_it(tree):
    [member] = pick_where("lean member", 1, live=True, unflagged=None)
    land(tree, record(tree, member, 0.95), "new", asked=True)
    running, you, _you_new, _plain = lean_tree(tree)
    shot = snap(tree)
    assert shot.items.items[you].needs_hint == "you" and shot.items.items[running].needs_hint == "running"
    assert all("hint" not in json.dumps(card) for card in review_data.queue(shot, 10))
    sampled = shot.items.items[rows.state_key(member)]
    assert sampled.stratum == "live_sample" and sampled.needs_hint == "you"
    assert review_data._lane_order(sampled, None, "you") == sampled.order, "its order stays the hash's"


def old_label(tree, key, is_open=True, needs=None, days=10, stratum="rest", wave_id=None):
    """An operator label `days` before NOW, on a text the deck holds."""
    row = review_log.answer_row(
        answer_id=review_log.new_answer_id(),
        at=rows.utc_stamp(NOW - timedelta(days=days)),
        surface=SURFACE,
        key=key,
        stratum=stratum,
        answer="agree" if is_open else "flip",
        against={"by": "claude-sonnet-5-5", "open": True},
        laya=None,
        decision_id=None,
        held_out=True,
        needs=needs,
        wave_id=wave_id,
    )
    tree.path("data/x")
    tree.append_jsonl("data/agent-open-work-operator.jsonl", row)
    return row


def second_look(tree, label, said, needs=None, at=None):
    row = review_log.retest_row(
        answer_id=review_log.new_answer_id(),
        at=at or rows.utc_stamp(NOW),
        surface=SURFACE,
        key=label["key"],
        of=label["answer_id"],
        said=said,
        needs=needs,
    )
    tree.append_jsonl("data/agent-open-work-operator.jsonl", row)
    return row


def retest_tree(tree, drawn=4, passed=3):
    """Sonnet-labelled texts: `drawn` the retest hash draws, `passed` it does not."""
    inside, outside = pick("retest in", "review-retest", True, drawn, 10), pick("retest out", "review-retest", False, passed, 10)
    keys = tree.unlabelled(inside + outside)
    tree.labels(dict.fromkeys(keys, True))
    return keys[:drawn], keys[drawn:]


def test_a_retest_is_due_for_a_drawn_text_once_its_label_is_a_week_old(tree):
    drawn, passed = retest_tree(tree)
    for key in passed:
        old_label(tree, key)
    assert review_data.retest_due(snap(tree), NOW) == [], "1 in 10 by the key's hash: these were not drawn"
    young = old_label(tree, drawn[0], days=6)
    oldest = old_label(tree, drawn[1], days=30)
    old_label(tree, drawn[2], days=7)
    old_label(tree, drawn[3], days=9, wave_id=review_log.new_wave_id())  # one press graded a whole wave
    assert review_data.retest_due(snap(tree), NOW) == [drawn[1], drawn[2]], "oldest label first; 6 days is too fresh"
    assert review_data.retest_due(snap(tree), NOW + timedelta(days=1)) == [drawn[1], drawn[2], young["key"]]
    second_look(tree, oldest, said=None)  # "cannot say" still spends the retest
    assert review_data.retest_due(snap(tree), NOW) == [drawn[2]]
    assert review_data.stats(snap(tree), tree.base, now=NOW)["retest"]["due"] == 1
    operator(tree, "f" * 16)  # a label on a text no source holds can never be shown again
    assert review_data.retest_due(snap(tree), NOW + timedelta(days=400)) == [drawn[2], young["key"]]


def test_a_retest_card_is_blind(tree):
    drawn, _passed = retest_tree(tree, drawn=3, passed=0)
    labels = [old_label(tree, key, needs="you", days=30 - i) for i, key in enumerate(drawn)]
    cards = review_data.retest_cards(snap(tree), 20, NOW)
    assert [card["key"] for card in cards] == drawn[:2], "one per ten cards asked for, oldest label first"
    assert len(review_data.retest_cards(snap(tree), 9, NOW)) == 1, "at least one while any is due"
    assert review_data.retest_cards(snap(tree), 0, NOW) == [], "a stats-only read deals nothing"
    [card] = cards[:1]
    assert set(card) == {"item_id", "key", "surface", "of", "text", "text_chars", "truncated", "at", "needs"}
    assert card["of"] == labels[0]["answer_id"] and card["needs"] == ["you", "running", "read"] and card["at"] is None
    told = json.dumps(card)
    assert "sonnet" not in told and "why " not in told and "stratum" not in told, "the key set is the proof; this is the belt"


def kappa_of_retests(tree):
    return review_data.stats(snap(tree), tree.base, now=NOW)["retest"]


def test_intra_rater_kappa_pairs_each_retest_with_the_label_it_re_tests(tree):
    made = three_way_labels(tree, done=12, running=6, you=6, unsplit=2)
    done, running, you, unsplit = made[:12], made[12:18], made[18:24], made[24:]
    before = review_data.stats(snap(tree), tree.base, now=NOW)
    for row in done[:10]:
        second_look(tree, row, False, at=AFTER_READ)
    for row in done[10:]:
        second_look(tree, row, True, "running", at=AFTER_READ)  # two DONE the operator now calls RUNNING
    for row in running:
        second_look(tree, row, True, "running", at=AFTER_READ)
    for row in you[:4]:
        second_look(tree, row, True, "you", at=AFTER_READ)
    for row in you[4:]:
        second_look(tree, row, True, "running", at=AFTER_READ)  # two NEEDS YOU the operator now calls RUNNING
    for row in unsplit:
        second_look(tree, row, True, "you", at=AFTER_READ)  # OPEN both times: a pair for `open`, none for a half
    found = kappa_of_retests(tree)
    assert found["pairs"] == 26 and found["open"]["n"] == 26 and found["open"]["basis"] == "retest"
    assert found["open"]["agree_rate"] == round(24 / 26, 3) and found["open"]["kappa_ci"] is not None
    assert found["needs"]["you"]["n"] == found["needs"]["running"]["n"] == 24, "only pairs where both looks drew a class"
    # you against the rest: 4 both, 2 first-only, 18 neither.
    assert found["needs"]["you"]["kappa"] == review_data.agreement.cohens_kappa(
        [(True, True)] * 4 + [(False, True)] * 2 + [(False, False)] * 18
    )
    assert found["needs"]["running"]["agree_rate"] == round(20 / 24, 3)
    assert found["needs"]["read"]["n"] == 24 and found["needs"]["read"]["agree_rate"] == 1.0, "nobody said READ"
    # Nothing but `retest` moved: the labels, the counts and every other κ are the first look's.
    after = review_data.stats(snap(tree), tree.base, now=NOW)
    assert {k: v for k, v in after.items() if k != "retest"} == {k: v for k, v in before.items() if k != "retest"}
    assert snap(tree).fold.labels[done[10]["key"]]["open"] is False


def test_a_retest_follows_its_label_out_and_cannot_say_is_no_pair(tree):
    made = three_way_labels(tree, done=10, you=11)
    for row in made[:20]:
        second_look(tree, row, row["open"], row.get("needs"))
    assert kappa_of_retests(tree)["pairs"] == 20
    second_look(tree, made[20], None)
    assert kappa_of_retests(tree)["pairs"] == 20, "cannot say"
    tree.append_jsonl(
        "data/agent-open-work-operator.jsonl",
        review_log.undo_row(answer_id=made[0]["answer_id"], at="2026-10-28T00:00:00Z", surface=SURFACE),
    )
    assert kappa_of_retests(tree)["pairs"] == 19, "its label was undone"
    operator(tree, made[0]["key"], "agree", stratum="recent")  # labelled again: a new answer, not the one re-tested
    assert kappa_of_retests(tree)["pairs"] == 19
    assert kappa_of_retests(tree)["open"]["kappa"] is None, "below twenty pairs there is no κ"
