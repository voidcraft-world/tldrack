"""`laya-lab card`: a count-only preview, and a reviewed card written into a checkout's `docs/`, nowhere else.

Every measurement, journal and text here is invented for the test. `--publish` is given a temp folder as the
checkout root; it starts no other program.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from laya_lab import bench, card
from laya_lab.golden import SUITES
from laya_bridge import agreement, card_files

from conftest import CHECKPOINT

SENTINEL = "PRIVATE-SENTINEL"
DOCS = ["docs/VERIFIER.md", "docs/REPRODUCE.md", "docs/MODEL_CARD.md"]


def measures(n: int = 20) -> dict:
    rate = {"value": 0.8, "lo": 0.5, "hi": 0.95, "n": 10}
    return {
        "kappa": {**agreement.kappa_summary([(True, True), (False, False)] * (n // 2)), "private_text": SENTINEL},
        "live": {"warning_new": {"precision": rate, "recall": rate}},
        "felt": {"false_warning": {"count": 2, "n": 20, "value": 10.0, "lo": 2.6, "hi": 28.4}},
        "cut": 0.7,
        "latency": None,
        "shadow": None,
    }


def preview(lab, monkeypatch, n: int = 20) -> dict:
    monkeypatch.setattr(card, "metrics", lambda *_: measures(n))
    return card.save(lab.base)[0]


def scored(voter: str, auc: float, false: int, missed: int) -> dict:
    return {
        "voter": voter,
        "auc": {"value": auc, "lo": round(auc - 0.063, 4), "hi": round(auc + 0.059, 4)},
        "brier": {"value": 0.2031, "lo": 0.18, "hi": 0.23},
        "ece": {"value": 0.1561, "bins": 10},
        "roc": [],
        "reliability": [],
        "latency": None,
        "operating": {
            "threshold": 0.7,
            "source": "live",
            "false_warnings": false,
            "missed_open": missed,
            "cost": {"value": 2.0 * false + missed, "lo": 1.0, "hi": 99.0},
        },
    }


def results(golden_protocol: str, *, heldout: bool = True) -> dict:
    """A results file in `bench.py`'s shape, invented: the served fine-tune, the base, the floor and a challenger."""
    voters = [
        {"name": "laya-base", "kind": "base", "served": False, "runtime": "mps"},
        {"name": "laya-fine-tune", "kind": "fine_tune", "served": True, "runtime": "mps"},
        {"name": "tfidf-floor", "kind": "floor", "served": False, "runtime": "cpu"},
        {"name": "laya-fine-tune-q3", "kind": "fine_tune", "served": False, "runtime": "mps"},
    ]
    sets = [
        {
            "set": "golden",
            "n": {"open": 1, "done": 1},
            "scores": [
                scored("laya-base", 0.824, 4, 78),
                scored("laya-fine-tune", 0.911, 3, 9),
                scored("tfidf-floor", 0.741, 9, 4),
                scored("laya-fine-tune-q3", 0.555, 0, 0),
            ],
        }
    ]
    if heldout:
        sets.append(
            {
                "set": "heldout",
                "n": {"open": 28, "done": 33},
                "scores": [
                    scored("laya-base", 0.685, 6, 20),
                    scored("laya-fine-tune", 0.758, 7, 14),
                    scored("tfidf-floor", 0.8, 2, 18),
                    scored("laya-fine-tune-q3", 0.556, 1, 1),
                ],
            }
        )
    return {
        "v": bench.RESULTS_VERSION,
        "voters": voters,
        "sets": sets,
        "generated_at": "2026-11-14T12:00:00Z",
        "protocol": {
            "id": golden_protocol + (".h61-d987767c" if heldout else ""),
            "question_version": 2,
            "interval": {"level": 0.95, "method": "bootstrap", "resamples": 2000, "seed": 20261113},
            "costs": {"false_warning": 2.0},
        },
        "reproduce": {"command": card.COMMAND, "set": "golden"},
    }


def golden_protocol() -> str:
    suite = SUITES[card.SUITE]
    return bench.protocol_id(suite, {"golden": bench.golden_rows(card.SUITE, suite)})


@pytest.fixture(autouse=True)
def results_folder(tmp_path, monkeypatch):
    """`bench/results/` of a stand-in checkout, empty until a test writes a file: no test reads the repository's."""
    folder = tmp_path / "bench-results"
    folder.mkdir()
    monkeypatch.setattr(card, "results_dir", lambda: folder)
    return folder


@pytest.fixture
def bench_results(public_golden, results_folder):
    """A current results file: its golden protocol is the synthetic public suite's own."""
    path = results_folder / "2026-11-14.json"
    path.write_text(json.dumps(results(golden_protocol())), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path):
    """A stand-in checkout: where `--publish` writes."""
    folder = tmp_path / "checkout"
    folder.mkdir()
    return folder


@pytest.fixture(autouse=True)
def no_other_program(monkeypatch):
    """The card never opens a pull request, clones or shells out: any spawn fails the test."""

    def spawned(*args, **kwargs):
        pytest.fail(f"the card started a program: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", spawned)
    monkeypatch.setattr(subprocess, "Popen", spawned)


def tree(folder) -> list[str]:
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())


def test_an_empty_store_previews_a_held_card(lab, public_golden):
    out, waiting = card.save(lab.base)
    assert not out["ready"] and out["pairs"] == 0
    assert waiting == ["fewer than 20 measurable κ pairs (0)", "no bench results: run `laya-lab bench`"]
    assert set(out["files"]) == {"VERIFIER.md", "golden/agent-open-work.jsonl", "REPRODUCE.md", "MODEL_CARD.md"}
    assert out["files"]["golden/agent-open-work.jsonl"] == public_golden.read_text(encoding="utf-8")
    assert json.loads((lab.base / card.FILENAME).read_text(encoding="utf-8")) == out


def test_the_plugin_s_reader_accepts_the_preview(lab, public_golden):
    """`card_files.read` is the route's gate: it must take a preview that carries MODEL_CARD.md."""
    assert card_files.read(lab.base) is None
    out = card.save(lab.base)[0]
    assert card_files.read(lab.base) == out


def test_only_counts_fixed_prose_and_the_public_suite_are_previewed(lab, monkeypatch, public_golden, bench_results):
    out = preview(lab, monkeypatch)
    text = json.dumps(out)
    assert SENTINEL not in text and str(lab.base) not in text
    assert out["ready"] and out["pairs"] == 20 and out["repository"] == "voidcraft-world/tldrack"
    assert card.COMMAND == "TLDRACK_DATA_DIR=$(mktemp -d) uv run --project lab laya-lab bench agent-open-work"
    assert card.COMMAND in out["files"]["VERIFIER.md"] and card.COMMAND in out["files"]["REPRODUCE.md"]
    assert "PARAMS" not in text
    model = out["files"]["MODEL_CARD.md"]
    for said in (
        "convaiinnovations/laya",
        "ModernBERT-large",
        "Apache-2.0",
        "No trained weights ship",
        "## Intended use",
        "## Limits",
    ):
        assert said in model, said


def test_a_card_that_is_not_the_reviewed_one_writes_nothing(lab, monkeypatch, public_golden, bench_results, root):
    with pytest.raises(card.Held, match="no readable preview"):
        card.publish(lab.base, "anything", root=root)
    out = preview(lab, monkeypatch, 18)
    with pytest.raises(card.Held, match="fewer than 20"):
        card.publish(lab.base, out["digest"], root=root)
    out = preview(lab, monkeypatch)
    with pytest.raises(card.Held, match="not the previewed card"):
        card.publish(lab.base, "wrong", root=root)
    monkeypatch.setattr(card, "metrics", lambda *_: measures(22))  # a new grade since the preview
    with pytest.raises(card.Held, match="changed"):
        card.publish(lab.base, out["digest"], root=root)
    assert tree(root) == []


def test_publish_writes_exactly_the_previewed_card_into_docs(lab, monkeypatch, public_golden, bench_results, root):
    out = preview(lab, monkeypatch)
    (root / "docs").mkdir()
    (root / "docs" / "VERIFIER.md").write_text("an older card", encoding="utf-8")
    (root / "docs" / "INSPIRATIONS.md").write_text("not the card's", encoding="utf-8")
    assert card.publish(lab.base, out["digest"], root=root) == DOCS
    assert tree(root) == ["docs/INSPIRATIONS.md", "docs/MODEL_CARD.md", "docs/REPRODUCE.md", "docs/VERIFIER.md"]
    for name in ("VERIFIER.md", "REPRODUCE.md", "MODEL_CARD.md"):
        assert (root / "docs" / name).read_text(encoding="utf-8") == out["files"][name]
    assert (root / "docs" / "INSPIRATIONS.md").read_text(encoding="utf-8") == "not the card's"
    assert card.publish(lab.base, out["digest"], root=root) == DOCS  # a second press writes the same files


def test_docs_that_leads_out_of_the_checkout_is_refused(lab, monkeypatch, public_golden, bench_results, root, tmp_path):
    out = preview(lab, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "docs").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(card.Held, match="out of the checkout"):
        card.publish(lab.base, out["digest"], root=root)
    assert tree(elsewhere) == []


def test_the_command_prints_one_json_line(lab, monkeypatch, public_golden, bench_results, root, capsys):
    monkeypatch.setattr(card, "metrics", lambda *_: measures())
    assert card.cmd(SimpleNamespace(publish=False, digest=""), lab.base, root=root) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == {"pairs", "ready", "digest", "waiting"} and shown["ready"] and shown["waiting"] == []
    assert tree(root) == []

    assert card.cmd(SimpleNamespace(publish=True, digest="stale"), lab.base, root=root) == card.REFUSED
    refused = capsys.readouterr()
    assert refused.out == "" and "card held" in refused.err and tree(root) == []

    assert card.cmd(SimpleNamespace(publish=True, digest=shown["digest"]), lab.base, root=root) == 0
    assert json.loads(capsys.readouterr().out) == {"written": DOCS}


def test_a_tampered_preview_is_not_shown(lab, monkeypatch, public_golden):
    out = preview(lab, monkeypatch)
    out["files"]["private.json"] = SENTINEL
    (lab.base / card.FILENAME).write_text(json.dumps(out), encoding="utf-8")
    assert card_files.read(lab.base) is None


def journal() -> dict:
    return {
        "state": "done",
        "checkpoint": CHECKPOINT,
        "surface": "agent_open_work",
        "question_version": 2,
        "device": "mps",
        "id": "synthetic-cycle",
        "started_at": "2026-11-09T10:00:00Z",
        "model_digest": "sha256:abcdef",
        "dataset_version": "fixture",
        "steps": {
            "build": {"unique": 4321, "open": 2100, "done": 2221, "operator": 77},
            "train": {"epochs": 3, "before": {"accuracy": 0.61, "note": SENTINEL}, "after": {"accuracy": 0.83}},
            "register": {"state": "done"},
            "gate": {"gate": "PASS", "checks": {"heldout": "pass", "odd": SENTINEL}},
        },
    }


def test_the_model_card_carries_the_cycle_s_numbers_and_never_a_file_from_the_store(lab, monkeypatch):
    assert "The last retrain cycle" not in card.model_card(lab.base)  # no cycle: the fixed prose alone
    folder = lab.checkpoint(CHECKPOINT)
    (folder / "MODEL_CARD.md").write_text(f"{SENTINEL} prose someone edited by hand", encoding="utf-8")
    held = journal()
    monkeypatch.setattr(card.lab_files, "read_retrain_journal", lambda *_: held)
    text = card.model_card(lab.base)
    assert text.startswith("# Model card") and "## The last retrain cycle" in text
    assert "### Training data" in text and "### Metrics" in text
    assert "4321 labelled turn ends" in text and "77 from the operator" in text and "0.610 → 0.830" in text
    assert SENTINEL not in text and CHECKPOINT not in text and "synthetic-cycle" not in text

    held["steps"]["label"] = {"teacher": "a sentence someone typed into the journal"}
    text = card.model_card(lab.base)
    assert "The last retrain cycle" not in text and "a sentence someone typed" not in text and "## Limits" in text


def test_a_cycle_that_never_wrote_its_card_is_left_out(lab, monkeypatch):
    lab.checkpoint(CHECKPOINT)
    monkeypatch.setattr(card.lab_files, "read_retrain_journal", lambda *_: journal())
    assert "The last retrain cycle" not in card.model_card(lab.base)


def test_provenance_states_the_batch_sign_off_and_its_bound():
    """`card.provenance`: counts and the bound, never a case; a batch not yet signed off is said to be so."""
    from laya_bridge import lab_files

    cases = [{"id": f"h-{i}", "expected": i % 2 == 0, "tags": ["open"]} for i in range(4)]
    cases += [{"id": f"b-{i}", "expected": i % 2 == 0, "tags": ["open", "batch-3", lab_files.PROVISIONAL_TAG]} for i in range(60)]
    sample = lab_files.batch_sample("batch-3", cases[4:])
    assert "not yet signed off (0/40" in card.provenance(cases, {})
    labels = {f"golden:{case_id}": {"answer": "agree"} for case_id in sample}
    text = card.provenance(cases, labels)
    assert text.startswith("Every case is invented") and "4 hand-written" in text
    assert "60 agent-written (batch-3), accepted on a stratified random sample of 40" in text
    assert f"at most {lab_files.batch_bound(60, 40)} of them" in text and "b-0" not in text
    # `laya-lab settle` drops the tag from the signed-off cases: the file alone then reads accepted, with no labels
    settled = cases[:4] + [
        {**case, "tags": [tag for tag in case["tags"] if tag != lab_files.PROVISIONAL_TAG]} for case in cases[4:]
    ]
    assert card.provenance(settled, {}) == text
    assert "a sampled case disputed" in card.provenance(settled, {f"golden:{sample[0]}": {"answer": "flip"}})


# ── one protocol: the bench's numbers, and the earlier one as a footnote ──────


def test_the_headline_and_the_reproduce_numbers_come_from_the_newest_results_file(
    lab, monkeypatch, bench_results, results_folder
):
    (results_folder / "fixture.json").write_text("{}", encoding="utf-8")  # invented numbers: never read
    (results_folder / "2026-11-13.json").write_text(json.dumps(results("agent-open-work.q2.gold")), encoding="utf-8")
    out = preview(lab, monkeypatch)
    assert out["ready"]
    verifier, reproduce = out["files"]["VERIFIER.md"], out["files"]["REPRODUCE.md"]
    assert "`bench/results/2026-11-14.json`" in verifier
    assert (
        "| Held-out: operator-graded turn ends no fine-tune trained on | 61 (28 open / 33 done) "
        "| 0.758 [0.695, 0.817] | 0.685 [0.622, 0.744] | 0.800 [0.737, 0.859] |"
    ) in verifier
    assert "| Public golden suite | 2 (1 open / 1 done) | 0.911 [0.848, 0.970] | 0.824 [0.761, 0.883] |" in verifier
    assert "0.555" not in verifier and "q3" not in verifier  # a challenger is measured, not served: off the card
    assert verifier.index("## The headline") < verifier.index("## Live sample")
    assert "per 100 graded sampled turn ends: 10.0 (95% CI 2.6, 28.4; false=2, n=20)" in verifier  # metrics.felt's
    assert card.COMMAND in reproduce and "`laya-base`" in reproduce
    assert "| 0.824 [0.761, 0.883] | 0.203 | 0.156 | 0.70 | 4 | 78 | 86.0 |" in reproduce
    assert f"golden protocol `{golden_protocol()}`" in reproduce and "2,000 resamples, seed 20261113" in reproduce
    assert "2 cases, 1 open and 1 done" in reproduce and "`mps`" in reproduce


def test_the_earlier_protocol_is_one_footnote_and_never_a_headline(lab, monkeypatch, bench_results):
    out = preview(lab, monkeypatch)
    texts = out["files"]
    assert sum(text.count("0.879") for name, text in texts.items() if name != card.GOLDEN) == 1
    verifier = texts["VERIFIER.md"]
    assert verifier.count("[^earlier]") == 2  # one mark, one definition
    note = verifier[verifier.index("[^earlier]:") :]
    assert "AUC 0.879" in note and "question v1" in note and "84-row" in note and "does not compare" in note
    assert verifier.index("[^earlier]:") > verifier.index("## Reproduce")


def test_results_of_another_golden_set_hold_the_card(lab, monkeypatch, public_golden, results_folder, root):
    """A file scored before the suite changed: its numbers are of rows nobody can rerun, so the card waits."""
    (results_folder / "2026-11-14.json").write_text(json.dumps(results("agent-open-work.q2.g000000000000")), encoding="utf-8")
    monkeypatch.setattr(card, "metrics", lambda *_: measures())
    out, waiting = card.save(lab.base)
    assert not out["ready"] and len(waiting) == 1 and "another golden set" in waiting[0]
    assert "Not measured: bench/results/2026-11-14.json scored another golden set" in out["files"]["VERIFIER.md"]
    assert "No published run to compare with yet" in out["files"]["REPRODUCE.md"]
    assert "0.824" not in json.dumps(out["files"])
    with pytest.raises(card.Held, match="not ready: .*another golden set"):
        card.publish(lab.base, out["digest"], root=root)
    assert tree(root) == []


def test_a_results_file_that_cannot_be_read_holds_the_card(lab, monkeypatch, public_golden, results_folder):
    broken = results(golden_protocol())
    broken["sets"][0]["scores"][0]["auc"]["value"] = "0.9"
    (results_folder / "2026-11-14.json").write_text(json.dumps(broken), encoding="utf-8")
    monkeypatch.setattr(card, "metrics", lambda *_: measures())
    out, waiting = card.save(lab.base)
    assert not out["ready"] and waiting == ["bench/results/2026-11-14.json cannot be read"]


def test_a_stranger_s_run_has_no_held_out_row(lab, monkeypatch, public_golden, results_folder):
    """The reproduce run on an empty store: a golden set alone, the base model alone. Still current."""
    alone = results(golden_protocol(), heldout=False)
    alone["voters"] = [{**alone["voters"][0], "served": True}]  # nothing registered: the base model is the one served
    alone["sets"][0]["scores"] = alone["sets"][0]["scores"][:1]
    (results_folder / "2026-11-14.json").write_text(json.dumps(alone), encoding="utf-8")
    out = preview(lab, monkeypatch)
    verifier = out["files"]["VERIFIER.md"]
    assert out["ready"] and "Held-out" not in verifier
    assert "| Public golden suite | 2 (1 open / 1 done) | — | 0.824 [0.761, 0.883] | — |" in verifier


def test_a_configured_card_repository_is_written_into_the_preview(lab, monkeypatch, public_golden, bench_results):
    import os
    from laya_bridge import paths as bridge_paths

    config = Path(os.environ[bridge_paths.CONFIG_ENV])
    (config / "settings.json").write_text(json.dumps({"plugins": {"laya": {"card_repo": "someone/their-fork"}}}))
    out = preview(lab, monkeypatch)
    assert out["repository"] == "someone/their-fork"
    assert card_files.read(lab.base) is None, "the default reader refuses a card for another repository"
    assert card_files.read(lab.base, "someone/their-fork") == out
