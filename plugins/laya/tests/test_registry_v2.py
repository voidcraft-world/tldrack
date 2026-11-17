"""models.json v2 on the READ side: only @champion is served, a cut rides its own checkpoint, v1 still reads as v1 meant.

Also the formats laya-lab's nightly pack writes for this plugin to read (the shadow file, retention) and the OPS
registry row they become. Synthetic trees only.
"""

from __future__ import annotations

import json

from laya_bridge import lab_files, lab_jobs, ops, paths
from laya_bridge.review_surfaces import REVIEW_SURFACES

from conftest import CHECKPOINT, FINE_TUNE

SURFACE = "agent_open_work"
RIVAL = f"{FINE_TUNE}-20261101-0900"
OLDER = f"{FINE_TUNE}-20261001-0900"


def cut(checkpoint: str, at: float = 0.62, model: str = FINE_TUNE) -> dict:
    return lab_files.threshold_entry(at, model, checkpoint, "2026-11-01T00:00:00Z", "operator", 40, 0.5, None, 0.3)


def folder(tree, name: str) -> str:
    path = tree.path(f"checkpoints/{name}")
    path.mkdir(exist_ok=True)
    (path / "model.safetensors").write_bytes(b"w")
    return str(path.resolve())


def v2(tree, *, champion=CHECKPOINT, challenger=RIVAL, previous=None, cuts=None) -> None:
    models = {}
    for name in {champion, challenger, previous} - {None}:
        folder(tree, name)
        models[name] = {
            "path": f"checkpoints/{name}",
            "surface": SURFACE,
            "registered_at": "2026-11-01T00:00:00Z",
            "tags": lab_files.gate_tags("PASS", "0123456789ab", "gate-x.json", {"heldout": "pass"}),
        }
        if name in (cuts or {}):
            models[name]["cut"] = cuts[name]
    tree.models(
        paths.with_serving_view(
            {
                "version": 2,
                "versions": models,
                "aliases": {SURFACE: {"champion": champion, "challenger": challenger, "previous": previous}},
                "history": [],
            }
        )
    )


def test_only_the_champion_is_served_and_a_challenger_never_reaches_laya_serve(tree):
    v2(tree, cuts={CHECKPOINT: cut(CHECKPOINT), RIVAL: cut(RIVAL, 0.55, RIVAL)})
    view = paths.registry_view(tree.base)
    assert list(view["models"]) == [FINE_TUNE], "one served entry per surface, under its served name"
    assert view["models"][FINE_TUNE]["checkpoint"] == CHECKPOINT and view["models"][FINE_TUNE]["version"] == CHECKPOINT
    assert paths.read_extra_models({paths.DATA_ENV: str(tree.params), paths.CONFIG_ENV: str(tree.params / "config")}) == {
        FINE_TUNE: view["models"][FINE_TUNE]["path"]
    }
    assert view["thresholds"][SURFACE]["at"] == 0.62 and view["thresholds"][SURFACE]["model"] == FINE_TUNE
    assert set(view["cuts"]) == {CHECKPOINT, RIVAL}
    versions = paths.registry_versions(tree.base)
    assert versions["version"] == 2 and not versions["legacy"]
    assert versions["aliases"][SURFACE] == {"champion": CHECKPOINT, "challenger": RIVAL, "previous": None}
    assert versions["versions"][RIVAL]["cut"]["at"] == 0.55 and versions["versions"][RIVAL]["tags"]["gate"] == "PASS"


def test_a_promotion_before_restart_keeps_the_running_checkpoints_own_cut(tree):
    """laya-serve LOADED the old champion; the alias has moved: the weights answering keep THEIR cut, not 0.7."""
    v2(
        tree,
        champion=RIVAL,
        challenger=None,
        previous=CHECKPOINT,
        cuts={CHECKPOINT: cut(CHECKPOINT), RIVAL: cut(RIVAL, 0.55, RIVAL)},
    )
    view = paths.registry_view(tree.base)
    old = str((tree.base / "checkpoints" / CHECKPOINT).resolve())
    assert paths.thresholds_view(view, {FINE_TUNE: old})[SURFACE]["at"] == 0.62
    new = view["models"][FINE_TUNE]["path"]
    assert paths.thresholds_view(view, {FINE_TUNE: new})[SURFACE]["at"] == 0.55
    assert paths.thresholds_view(view)[SURFACE]["at"] == 0.55


def test_aliases_that_name_nothing_or_collide_are_dropped(tree):
    v2(tree, champion=CHECKPOINT, challenger=None)
    payload = json.loads((tree.base / "models.json").read_text())
    payload["aliases"][SURFACE].update(challenger=CHECKPOINT, previous="voidcraft-agent-open-work-19990101-0000")
    payload["aliases"]["paste"] = {"champion": CHECKPOINT}  # another surface's version
    tree.models(payload)
    aliases = paths.registry_versions(tree.base)["aliases"]
    assert aliases[SURFACE] == {"champion": CHECKPOINT, "challenger": None, "previous": None}
    assert aliases["paste"]["champion"] is None
    assert list(paths.registry_view(tree.base)["models"]) == [FINE_TUNE]


def test_a_v1_file_still_serves_every_entry_and_reads_as_champions(tree):
    checkpoint = tree.register(thresholds={SURFACE: cut(CHECKPOINT)})
    view = paths.registry_view(tree.base)
    assert view["models"][FINE_TUNE]["path"] == str(checkpoint) and view["thresholds"][SURFACE]["at"] == 0.62
    versions = paths.registry_versions(tree.base)
    assert versions["legacy"] and versions["version"] == 1
    assert versions["aliases"][SURFACE]["champion"] == CHECKPOINT
    assert versions["versions"][CHECKPOINT]["cut"]["at"] == 0.62


def test_migrate_payload_carries_each_served_entry_and_its_own_cut_and_names_what_it_drops():
    v1 = {
        "version": 1,
        "models": {
            FINE_TUNE: {"path": f"checkpoints/{CHECKPOINT}", "surface": SURFACE, "trained": {"at": "2026-01-02T03:04:00Z"}},
            "voidcraft-paste": {"path": "checkpoints/voidcraft-paste-20261009-1200"},
            "my-custom": {"path": "checkpoints/x"},
        },
        "thresholds": {SURFACE: cut(CHECKPOINT), "paste": cut("another-folder", model="voidcraft-paste"), "news_gate": cut("r")},
    }
    out, notes = paths.migrate_payload(v1)
    assert out["version"] == 2 and out["models"][FINE_TUNE]["path"] == f"checkpoints/{CHECKPOINT}"
    assert out["thresholds"][SURFACE]["at"] == 0.62
    assert out["aliases"] == {
        SURFACE: {"champion": CHECKPOINT, "challenger": None, "previous": None},
        "paste": {"champion": "voidcraft-paste-20261009-1200", "challenger": None, "previous": None},
    }
    assert out["versions"][CHECKPOINT]["cut"]["at"] == 0.62 and out["versions"][CHECKPOINT]["tags"] == {"gate": "MIGRATED"}
    assert out["versions"][CHECKPOINT]["registered_at"] == "2026-01-02T03:04:00Z"
    assert "cut" not in out["versions"]["voidcraft-paste-20261009-1200"]
    assert any("my-custom" in note for note in notes) and any("paste cut" in note for note in notes)
    assert any("news_gate cut" in note for note in notes)
    assert paths.migrate_payload(out) == (out, []), "a v2 payload passes through"


def test_a_v2_file_a_pre_v2_writer_rewrote_keeps_its_challenger_and_follows_what_it_served(tree):
    """main's laya-lab (no aliases) re-registered a new champion and cleared the cut, writing `version: 1` over a v2
    file: the challenger and each version's own cut survive, and the champion is what that writer served."""
    v2(tree, champion=CHECKPOINT, challenger=RIVAL, cuts={CHECKPOINT: cut(CHECKPOINT)})
    payload = json.loads((tree.base / "models.json").read_text())
    folder(tree, OLDER)
    payload.update(version=1, models={FINE_TUNE: {"path": f"checkpoints/{OLDER}", "surface": SURFACE}}, thresholds={})
    out, notes = paths.migrate_payload(payload)
    assert out["aliases"][SURFACE] == {"champion": OLDER, "challenger": RIVAL, "previous": CHECKPOINT}
    assert out["versions"][CHECKPOINT]["cut"]["at"] == 0.62, "the old champion keeps its own cut for a rollback"
    assert "cut" not in out["versions"][OLDER] and any("pre-v2 writer" in note for note in notes)
    payload.update(models={}, thresholds={})  # …or unregistered it
    assert paths.migrate_payload(payload)[0]["aliases"][SURFACE]["champion"] is None


def test_a_pre_v2_reader_still_serves_the_champion_at_its_own_cut(tree):
    """What main's bridge reads from a v2 file: its `models` / `thresholds`, the derived serving view."""
    v2(tree, cuts={CHECKPOINT: cut(CHECKPOINT), RIVAL: cut(RIVAL, 0.55, RIVAL)})
    payload = json.loads((tree.base / "models.json").read_text())
    assert payload["models"] == {FINE_TUNE: {"path": f"checkpoints/{CHECKPOINT}", "surface": SURFACE}}
    assert payload["thresholds"][SURFACE]["at"] == 0.62 and payload["thresholds"][SURFACE]["model"] == FINE_TUNE
    assert paths._legacy_view(tree.base.resolve(), payload)["models"].keys() == paths.registry_view(tree.base)["models"].keys()


def test_an_unknown_registry_version_reads_as_empty(tree):
    tree.models({"version": 3, "models": {}})
    assert paths.registry_view(tree.base)["models"] == {} and paths.registry_versions(tree.base)["version"] is None


def test_the_shadow_file_keeps_valid_rows_only(tree):
    good = lab_files.shadow_row(
        decision_id="20261109T031500-0123456789ab",
        key="0123456789abcdef",
        at="2026-11-09T03:15:00Z",
        question_version=2,
        served_model=FINE_TUNE,
        served_checkpoint=CHECKPOINT,
        served_p=0.8,
        served_threshold=0.7,
        served_done_threshold=0.4,
        challenger=RIVAL,
        challenger_digest="sha256:0123456789abcdef",
        device="mps",
        challenger_p=0.3,
        challenger_threshold=0.7,
        challenger_threshold_source="code",
        challenger_done_threshold=0.4,
        scored_at="2026-11-09T04:00:00Z",
    )
    bad = {**good, "challenger_p": 1.5}
    tree.write_jsonl(f"shadow/{SURFACE}.jsonl", [good, bad, {**good, "device": "tpu"}])
    rows_in, code = lab_files.read_shadow(tree.base, SURFACE)
    assert code == "ok" and rows_in == [good]
    assert lab_files.shadow_key(good) == (good["decision_id"], RIVAL, "sha256:0123456789abcdef", "mps")


def test_retention_keeps_the_aliases_and_the_two_newest_rejects(tree):
    v2(tree, champion=CHECKPOINT, challenger=RIVAL, previous=OLDER)
    rejects = [f"{FINE_TUNE}-2026090{day}-0900" for day in range(1, 5)]
    for name in rejects:
        folder(tree, name)
    kept = lab_files.retention(tree.base, SURFACE, paths.registry_versions(tree.base))
    assert kept["keep"] == [CHECKPOINT, RIVAL, OLDER, rejects[3], rejects[2]]
    assert [item["name"] for item in kept["prunable"]] == [rejects[1], rejects[0]]
    assert all(item["bytes"] == 1 for item in kept["prunable"])


def test_ops_registry_row_shows_aliases_cuts_and_the_challengers_shadow(tree):
    v2(tree, cuts={CHECKPOINT: cut(CHECKPOINT)})

    def shadow(n, served_p, challenger_p, day):
        return lab_files.shadow_row(
            decision_id=f"202611{day:02d}T031500-0123456789{n:02x}",
            key=f"{n:016x}",
            at=f"2026-11-{day:02d}T03:15:00Z",
            question_version=2,
            served_model=FINE_TUNE,
            served_checkpoint=CHECKPOINT,
            served_p=served_p,
            served_threshold=0.62,
            served_done_threshold=0.3,
            challenger=RIVAL,
            challenger_digest=None,
            device="mps",
            challenger_p=challenger_p,
            challenger_threshold=0.7,
            challenger_threshold_source="code",
            challenger_done_threshold=0.4,
            scored_at="2026-11-10T04:00:00Z",
        )

    # same mark: 0.9/0.9 (open/open), 0.1/0.2 (done/done); a disagreement: 0.65 is ⚠️ at 0.62 but 💬 at 0.7
    tree.write_jsonl(f"shadow/{SURFACE}.jsonl", [shadow(1, 0.9, 0.9, 8), shadow(2, 0.1, 0.2, 9), shadow(3, 0.65, 0.65, 9)])
    row = ops.registry_row(tree.base, REVIEW_SURFACES[SURFACE])
    assert row["version"] == 2 and row["champion"]["name"] == CHECKPOINT and row["champion"]["cut"]["at"] == 0.62
    assert row["challenger"]["name"] == RIVAL and row["challenger"]["gate"] == "PASS" and row["challenger"]["cut"] is None
    assert row["shadow"] == {
        "rows": 3,
        "days": 2,
        "agree": 0.667,
        "disagree": 1,
        "since": "2026-11-08T03:15:00Z",
        "last": "2026-11-09T03:15:00Z",
    }
    assert row["nightly"] is None and row["prunable"] == {"checkpoints": 0, "bytes": 0}


def test_the_nightly_pack_holds_the_lab_queue_without_a_verb_row():
    panes = lab_jobs.parse_lab_panes("\x1f".join(("eval-laya-nightly", "0", "", "", "", "laya-nightly")))
    assert lab_jobs.running(panes) == "eval-laya-nightly"
    assert {row["verb"] for row in lab_jobs.job_rows(panes)} == set(lab_jobs.LAB_VERBS)
    assert all(row["state"] == "idle" for row in lab_jobs.job_rows(panes))
