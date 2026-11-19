"""Legacy (step 6.8): a fine-tune named before 0.1.0 (`voidcraft-<surface>`) is still the same model for one release.

Registries, checkpoint folders, report names and scores rows on disk carry the old prefix; every reader takes it as
the `tldrack-` name, and nothing writes it. A checkpoint folder keeps the name it was trained under. Synthetic trees only.
"""

from __future__ import annotations

import json

from laya_bridge import lab_files, paths, review_data, rows

from conftest import FINE_TUNE

SURFACE = "agent_open_work"
#: Legacy (step 6.8): the names before 0.1.0.
LEGACY = "voidcraft-agent-open-work"
LEGACY_CHECKPOINT = f"{LEGACY}-20261001-0900"


def test_a_legacy_model_name_reads_as_its_tldrack_twin_and_a_folder_name_never_changes():
    assert paths.FINE_TUNE_PREFIX == "tldrack-" and paths.fine_tune_name(SURFACE) == FINE_TUNE
    assert paths.served_name(LEGACY) == FINE_TUNE
    assert paths.served_name(FINE_TUNE) == FINE_TUNE and paths.served_name("english") == "english"
    assert paths.served_name(LEGACY_CHECKPOINT) == LEGACY_CHECKPOINT, "a version keeps the name it was trained under"
    assert paths.surface_of_name(LEGACY) == SURFACE == paths.surface_of_name(FINE_TUNE)


def test_a_checkpoint_trained_under_either_prefix_is_the_surfaces():
    pattern = rows.checkpoint_name(FINE_TUNE)
    assert pattern.fullmatch(f"{FINE_TUNE}-20261101-0900") and pattern.fullmatch(LEGACY_CHECKPOINT)
    assert rows.checkpoint_name(LEGACY).fullmatch(f"{FINE_TUNE}-20261101-0900")
    assert not pattern.fullmatch("other-agent-open-work-20261001-0900")
    assert not pattern.fullmatch(f"{FINE_TUNE}-x-20261001-0900"), "a longer name is another surface"


def test_a_v1_registry_written_under_the_legacy_name_is_served_under_the_new_one(tree):
    folder = tree.path(f"checkpoints/{LEGACY_CHECKPOINT}")
    folder.mkdir()
    cut = lab_files.threshold_entry(0.62, LEGACY, LEGACY_CHECKPOINT, "2026-11-01T00:00:00Z", "operator", 40, 0.5, None, 0.3)
    tree.models(
        {
            "version": 1,
            "models": {LEGACY: {"path": f"checkpoints/{LEGACY_CHECKPOINT}", "surface": SURFACE}},
            "thresholds": {SURFACE: cut},
        }
    )
    view = paths.registry_view(tree.base)
    assert list(view["models"]) == [FINE_TUNE] and view["models"][FINE_TUNE]["checkpoint"] == LEGACY_CHECKPOINT
    assert view["thresholds"][SURFACE]["model"] == FINE_TUNE
    assert paths.thresholds_view(view, {FINE_TUNE: str(folder)})[SURFACE]["at"] == 0.62
    migrated, notes = paths.migrate_payload(json.loads((tree.base / "models.json").read_text()))
    assert notes == [] and migrated["aliases"][SURFACE]["champion"] == LEGACY_CHECKPOINT
    assert migrated["versions"][LEGACY_CHECKPOINT]["cut"]["at"] == 0.62, "the cut rides along"
    assert list(migrated["models"]) == [FINE_TUNE]


def test_a_v2_registry_serves_a_legacy_folder_under_the_new_name(tree):
    tree.path(f"checkpoints/{LEGACY_CHECKPOINT}").mkdir()
    tree.models(
        {
            "version": 2,
            "versions": {LEGACY_CHECKPOINT: {"path": f"checkpoints/{LEGACY_CHECKPOINT}", "surface": SURFACE}},
            "aliases": {SURFACE: {"champion": LEGACY_CHECKPOINT, "challenger": None, "previous": None}},
            "history": [],
            "models": {LEGACY: {"path": f"checkpoints/{LEGACY_CHECKPOINT}"}},
        }
    )
    assert list(paths.registry_view(tree.base)["models"]) == [FINE_TUNE]
    # A laya-serve spawned before the rename LOADED the old name: its checkpoint's own cut still applies.
    view = {"models": {}, "thresholds": {}, "cuts": {LEGACY_CHECKPOINT: {"surface": SURFACE, "cut": {"at": 0.6}}}}
    assert paths.thresholds_view(view, {LEGACY: f"/x/{LEGACY_CHECKPOINT}"})[SURFACE]["at"] == 0.6


def test_reports_and_scores_under_the_legacy_name_speak_for_the_same_model(tree):
    report = {"suite": "agent-open-work", "model": LEGACY, "cases": 32}
    tree.path("evals").mkdir(exist_ok=True)
    (tree.base / "evals" / f"agent-open-work-{LEGACY}-20261019-000000.json").write_text(json.dumps(report))
    path, found = lab_files.newest_report(tree.base, "agent-open-work", FINE_TUNE, "golden")
    assert found["cases"] == 32 and LEGACY in path.name
    stamp = "2026-10-27T01:02:03Z"
    tree.write_jsonl(
        "data/agent-open-work-laya-p.jsonl",
        [
            lab_files.scores_row(rows.state_key("a"), 0.8, LEGACY, LEGACY_CHECKPOINT, 1, 0.7, stamp),
            lab_files.scores_row(rows.state_key("b"), 0.2, LEGACY_CHECKPOINT, LEGACY_CHECKPOINT, 1, 0.7, stamp),
        ],
    )
    scored = lab_files.read_scores(tree.base, "agent-open-work")[0]
    assert [row["model"] for row in scored] == [FINE_TUNE, LEGACY_CHECKPOINT]


def test_a_legacy_name_is_still_a_laya_model_on_the_deck():
    assert review_data.is_laya_model(LEGACY) and review_data.is_laya_model(FINE_TUNE)
    assert not review_data.is_laya_model("claude-sonnet-5-5")
