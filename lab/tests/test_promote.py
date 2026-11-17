"""PROMOTE: operator flips become PRIVATE golden cases; the public suite is never written."""

from __future__ import annotations

import json
import os
import stat

from laya_lab import promote
from laya_lab.golden import GOLDEN_DIR, load_suites
from laya_bridge import label_budget, lab_files, paths, review_data, rows

from conftest import SONNET, SUITE, in_half, synthetic


def private_lines(lab):
    return paths.private_golden_path(lab.base, SUITE).read_text(encoding="utf-8").splitlines()


def test_a_flip_becomes_a_private_case_with_the_shared_shape(lab, public_golden, umask_022, capsys):
    real_public = (GOLDEN_DIR / f"{SUITE}.jsonl").read_bytes()
    public_before = (public_golden.read_bytes(), os.stat(public_golden).st_mtime_ns)
    text = synthetic("flip", 1)[0]
    key = lab.unlabelled([text])[0]
    lab.trained_on([text])
    lab.labels({key: True})
    lab.operator(key, "flip", against_open=True)

    assert promote.promote(lab.base, SUITE, flips=True) == 0  # a dry run writes nothing
    assert not paths.private_golden_path(lab.base, SUITE).exists()
    assert "public 1 open/1 done · private 0 open/1 done" in capsys.readouterr().out

    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    lines = private_lines(lab)
    assert lines[0] == lab_files.PRIVATE_GOLDEN_HEADER
    case = json.loads(lines[1])
    assert case == {**lab_files.private_golden_case(key, text, False, True, SONNET, case["promoted_at"])}
    assert case["id"] == f"f-{key}" and case["key"] == key and case["state"] == {"hand_off": text}
    path = paths.private_golden_path(lab.base, SUITE)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert (public_golden.read_bytes(), os.stat(public_golden).st_mtime_ns) == public_before
    assert (GOLDEN_DIR / f"{SUITE}.jsonl").read_bytes() == real_public
    assert load_suites(lab.base, SUITE)[1] == [case]
    out = capsys.readouterr().out
    assert text not in out, "the terminal gets ids and keys, never the operator's text"

    built = (lab.base / "data" / "agent_open_work.jsonl").read_text()
    assert key in review_data.golden_keys(lab.base, SUITE) and key not in built, "build() then holds it back"


def test_a_second_promote_appends_under_one_header(lab, public_golden):
    keys = lab.unlabelled(synthetic("flip", 2))
    lab.trained_on(synthetic("flip", 2))
    lab.operator(keys[0], "flip", against_open=True)
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    lab.operator(keys[1], "flip", against_open=False)
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    lines = private_lines(lab)
    assert lines.count(lab_files.PRIVATE_GOLDEN_HEADER) == 1 and len(lines) == 3
    public, private = load_suites(lab.base, SUITE)
    assert [c["expected"] for c in private] == [False, True]


def test_explicit_keys_are_refused_unless_operator_labelled_with_text_and_new(lab, public_golden, capsys):
    texts = synthetic("k", 3)
    keys = lab.unlabelled(texts)
    lab.labels({keys[0]: True})  # Sonnet only: not an operator label
    lab.operator(keys[1], "skip")
    lab.operator(keys[2], "agree", against_open=True)
    golden_text = "Phase C is not started."  # the synthetic public suite's case-open
    golden_key = lab.unlabelled([*texts, golden_text])[-1]
    lab.operator(golden_key, "agree", against_open=True)
    for key in (keys[0], keys[1], golden_key, "golden:case-open", "e" * 16):
        assert promote.promote(lab.base, SUITE, keys=[key], apply=True) == promote.REFUSED, key
    out = capsys.readouterr().out
    assert "not a live operator label" in out and "already golden" in out and "already a public golden case" in out
    assert not paths.private_golden_path(lab.base, SUITE).exists()
    assert promote.promote(lab.base, SUITE, keys=[keys[2]], apply=True) == 0  # an agree may be named explicitly
    assert json.loads(private_lines(lab)[1])["expected"] is True


def test_a_text_already_private_is_not_a_candidate_twice(lab, public_golden):
    text = synthetic("dup", 1)[0]
    key = lab.unlabelled([text])[0]
    lab.trained_on([text])
    lab.operator(key, "flip", against_open=True)
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    assert review_data.promotion_candidates(lab.base, SUITE) == []
    assert promote.promote(lab.base, SUITE, keys=[key], apply=True) == promote.REFUSED
    assert rows.state_key(text) == key


def test_a_reserve_flip_is_never_promoted_by_flips_or_by_name(lab, public_golden, capsys):
    """The label budget (`laya_bridge.label_budget`): the reserve feeds RE-SWEEP, so no door promotes it."""
    [text] = in_half(label_budget.RESERVE, "open", 1)
    [key] = lab.unlabelled([text])
    lab.operator(key, "flip", against_open=False)  # records OPEN: a clean label, held in reserve
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == promote.REFUSED
    out = capsys.readouterr().out
    assert "1 flip(s) stay in the label budget's reserve" in out and "nothing to promote" in out
    assert promote.promote(lab.base, SUITE, keys=[key], apply=True) == promote.REFUSED
    assert "in the label budget's reserve" in capsys.readouterr().out
    assert not paths.private_golden_path(lab.base, SUITE).exists()
    assert review_data.sweep_readiness(lab.base, "agent_open_work")["reserve"]["rows"] == 1


def test_a_clean_train_flip_is_promoted_even_as_its_classs_only_clean_label(lab, public_golden):
    """The keep-ten-per-class guard is gone: PROMOTE takes only what the next retrain was going to take."""
    [text] = in_half(label_budget.TRAIN, "open", 1)
    [key] = lab.unlabelled([text])
    lab.operator(key, "flip", against_open=False)  # records OPEN: the only clean OPEN label
    assert review_data.promotion_candidates(lab.base, SUITE) == [key]
    assert promote.promote(lab.base, SUITE, flips=True, apply=True) == 0
    assert key in review_data.golden_keys(lab.base, SUITE)
