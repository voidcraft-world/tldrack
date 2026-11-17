"""The nightly pack and what it stands on: models.json v2 (aliases, per-checkpoint cuts, retention), the shadow pass,
dataset versions and checkpoint digests. Synthetic trees and a fake router only: no torch, no operator data."""

from __future__ import annotations

import json
import os

import pytest

from laya_lab import cli, golden, heldout, infer, nightly, registry, shadow
from laya_lab.registry import read_registry, register
from laya_bridge import lab_files, paths, review_data
from laya_bridge.paths import read_extra_models
from tldrack import open_work

from conftest import CHECKPOINT, FINE_TUNE, SUITE, SURFACE, FakeRouter, decide, store_env, synthetic

RIVAL = f"{FINE_TUNE}-20261101-0900"
NEWER = f"{FINE_TUNE}-20261102-0900"


def env(lab) -> dict[str, str]:
    return store_env(lab.base.parent)


def v1(lab, checkpoint: str = CHECKPOINT, *, cut_at: float | None = 0.62) -> None:
    lab.checkpoint(checkpoint, train_texts=[])
    payload = {"version": 1, "models": {FINE_TUNE: {"path": f"checkpoints/{checkpoint}", "surface": SURFACE}}}
    if cut_at is not None:
        payload["thresholds"] = {
            SURFACE: lab_files.threshold_entry(cut_at, FINE_TUNE, checkpoint, "2026-11-01T00:00:00Z", "operator", 40, 0.5, None)
        }
    lab.path("models.json").write_text(json.dumps(payload), encoding="utf-8")


def operator_labels(lab, n_open: int = 20, n_done: int = 20, prefix: str = "f") -> list[str]:
    texts = synthetic(f"{prefix}-open", n_open) + synthetic(f"{prefix}-done", n_done)
    keys = lab.unlabelled(texts)
    labels = {key: i < n_open for i, key in enumerate(keys)}
    lab.labels(labels)
    for key, is_open in labels.items():
        lab.operator(key, "agree", against_open=is_open)
    return keys


def p_of(state) -> float:
    text = state if isinstance(state, str) else str(state)
    return 0.8 if ("-open " in text or "Phase C" in text or "live open" in text) else 0.2


# ── the registry ──────────────────────────────────────────────────────────────


def test_a_v1_registry_is_migrated_without_changing_what_is_served(lab):
    v1(lab)
    before = read_extra_models(env(lab)), paths.read_thresholds(env(lab))
    peeked = read_registry(lab.base, migrate=False)
    assert peeked["version"] == 2 and peeked["aliases"][SURFACE]["champion"] == CHECKPOINT
    assert json.loads((lab.base / "models.json").read_text())["version"] == 1, "a read-only command writes nothing"
    migrated, notes = registry.migrate_registry(lab.base)
    assert migrated and any(note.startswith("v1 kept as models.v1-") for note in notes)
    assert (read_extra_models(env(lab)), paths.read_thresholds(env(lab))) == before
    payload = read_registry(lab.base)
    assert payload["version"] == 2 and payload["aliases"][SURFACE]["champion"] == CHECKPOINT
    assert payload["versions"][CHECKPOINT]["cut"]["at"] == 0.62
    assert payload["models"] == {FINE_TUNE: {"path": f"checkpoints/{CHECKPOINT}", "surface": SURFACE}}, (
        "the v1-shaped serving view a pre-v2 bridge reads"
    )
    assert payload["thresholds"][SURFACE]["at"] == 0.62 and payload["thresholds"][SURFACE]["model"] == FINE_TUNE
    assert payload["history"][-1]["why"] == "migrated from v1"
    assert registry.migrate_registry(lab.base) == (False, []), "idempotent"
    backups = [p for p in lab.base.iterdir() if p.name.startswith("models.v1-")]
    assert len(backups) == 1 and json.loads(backups[0].read_text())["version"] == 1


def v1_backups(lab) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(lab.base.iterdir()) if p.name.startswith("models.v1-")]


def test_an_armed_press_on_a_v1_registry_migrates_it_and_succeeds(lab, capsys):
    """main only ever wrote v1: RE-SWEEP (`sweep --labels operator --apply`) and RESET (`sweep --reset`) on the operator's
    file must work the day this merges, not after a night has run."""
    v1(lab, cut_at=None)
    operator_labels(lab)
    before = read_extra_models(env(lab))
    assert heldout.run_sweep(SUITE, lab.base, labels="operator", apply=True, router=FakeRouter(p_of)) == 0
    payload = json.loads((lab.base / "models.json").read_text())
    assert payload["version"] == 2 and payload["versions"][CHECKPOINT]["cut"]["checkpoint"] == CHECKPOINT
    assert read_extra_models(env(lab)) == before, "nothing served moved"
    assert [backup["version"] for backup in v1_backups(lab)] == [1]
    assert "migrated v1 → v2" in capsys.readouterr().err

    v1(lab, cut_at=0.62)
    assert heldout.run_sweep(SUITE, lab.base, reset=True) == 0
    payload = json.loads((lab.base / "models.json").read_text())
    assert payload["version"] == 2 and "cut" not in payload["versions"][CHECKPOINT] and payload["thresholds"] == {}
    backups = v1_backups(lab)
    assert len(backups) == 2 and [b["thresholds"][SURFACE]["at"] for b in backups if b.get("thresholds")] == [0.62], (
        "the v1 file, cut and all, is kept beside it, never over the first backup"
    )


def test_train_checks_the_registry_before_any_gpu_work(lab, monkeypatch):
    from laya_lab import train

    lab.path("models.json").write_text('{"version": 2, "versions": {', encoding="utf-8")
    monkeypatch.setattr(train, "split_rows", lambda found: ([{}] * 50, [{}] * 10))
    monkeypatch.setattr(train, "read_jsonl", lambda path: [])
    monkeypatch.setattr(train.infer, "hold_device", lambda *_a, **_kw: pytest.fail("held the GPU before the check"))
    with pytest.raises(SystemExit, match="not JSON"):
        train.run(SURFACE, lab.base, None, epochs=1, device="mps", force=False)
    assert not (lab.base / "checkpoints").exists(), "no checkpoint folder for a run that could never register"


def test_a_challenger_is_never_served_and_a_newer_one_supersedes_it(lab):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT), {"eval_items": 1})
    served = read_extra_models(env(lab))
    tags = lab_files.gate_tags("PASS", "0123456789ab", "gate-x.json", {"heldout": "pass"})
    assert register(lab.base, SURFACE, lab.checkpoint(RIVAL), {"eval_items": 1}, as_="challenger", tags=tags) == RIVAL
    assert read_extra_models(env(lab)) == served, "a challenger never touches the served entry"
    assert registry.served_model(lab.base, SURFACE) == FINE_TUNE
    assert registry.resolve_model(lab.base, SURFACE, "challenger") == (RIVAL, (lab.base / "checkpoints" / RIVAL).resolve())
    assert paths.registry_versions(lab.base)["versions"][RIVAL]["tags"]["gate"] == "PASS"
    register(lab.base, SURFACE, lab.checkpoint(NEWER), {"eval_items": 1}, as_="challenger")
    versions = paths.registry_versions(lab.base)
    assert versions["aliases"][SURFACE]["challenger"] == NEWER and versions["versions"][RIVAL]["rejected_at"]
    with pytest.raises(SystemExit, match="served champion"):
        register(lab.base, SURFACE, lab.base / "checkpoints" / CHECKPOINT, {}, as_="challenger")


def test_promote_rollback_and_reject_move_aliases_and_only_prune_deletes(lab):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT), {"eval_items": 1})
    register(lab.base, SURFACE, lab.checkpoint(RIVAL), {"eval_items": 1}, as_="challenger")
    assert registry.promote_challenger(lab.base, SURFACE, "test") == (RIVAL, CHECKPOINT)
    assert read_extra_models(env(lab)) == {FINE_TUNE: str((lab.base / "checkpoints" / RIVAL).resolve())}
    assert registry.rollback(lab.base, SURFACE, "regressed") == (CHECKPOINT, RIVAL)
    versions = paths.registry_versions(lab.base)
    assert versions["aliases"][SURFACE] == {"champion": CHECKPOINT, "challenger": None, "previous": None}
    assert versions["versions"][RIVAL]["rejected_at"]
    with pytest.raises(SystemExit, match="no challenger"):
        registry.reject_challenger(lab.base, SURFACE, "x")

    rejects = [f"{FINE_TUNE}-2026090{day}-0900" for day in range(1, 4)]
    for name in rejects:
        lab.checkpoint(name)
    plan = registry.retention(lab.base, SURFACE)
    assert plan["keep"] == [CHECKPOINT, RIVAL, rejects[2]], "the aliases, then the newest rejects (RIVAL is one now)"
    assert [p["name"] for p in plan["prunable"]] == [rejects[1], rejects[0]]
    assert all((lab.base / "checkpoints" / name).is_dir() for name in rejects), "retention only reads"
    pruned = registry.prune_rejects(lab.base, SURFACE)
    assert [item["name"] for item in pruned] == [rejects[1], rejects[0]]
    assert not (lab.base / "checkpoints" / rejects[0]).exists() and not (lab.base / "checkpoints" / f"{rejects[0]}.data").exists()
    assert (lab.base / "checkpoints" / rejects[2]).is_dir() and registry.prune_rejects(lab.base, SURFACE) == []


def test_train_registers_a_challenger_by_default():
    parser = cli.build_parser()
    assert parser.parse_args(["train", "agent_open_work"]).as_ == "challenger"
    assert parser.parse_args(["train", "agent_open_work", "--as", "champion"]).as_ == "champion"
    with pytest.raises(SystemExit):
        parser.parse_args(["registry", "register", "agent_open_work", RIVAL, "--as", "champion"])


def test_registry_show_reads_only(lab, monkeypatch, capsys):
    monkeypatch.setenv(paths.DATA_ENV, str(lab.base.parent))
    v1(lab)
    assert cli.main(["registry", "show"]) == 0
    out = capsys.readouterr().out
    assert f"v1{cli.LEGACY_NOTE}" in out and f"@champion   {CHECKPOINT}  cut 0.62" in out
    assert json.loads((lab.base / "models.json").read_text())["version"] == 1


# ── digests and dataset versions ──────────────────────────────────────────────


def test_a_checkpoint_digest_is_cached_and_follows_the_weights(lab):
    folder = lab.checkpoint(RIVAL)
    first = infer.checkpoint_digest(RIVAL, folder)
    assert first.startswith("sha256:") and (folder / infer.DIGEST_SIDECAR).is_file()
    assert infer.checkpoint_digest(RIVAL, folder) == first
    (folder / "model.safetensors").write_bytes(b"other weights")
    assert infer.checkpoint_digest(RIVAL, folder) != first
    assert infer.checkpoint_digest(RIVAL, lab.base / "missing") is None
    assert infer.checkpoint_digest("english", None) == f"english.laya-{infer.laya_version()}"


def test_the_dataset_version_moves_with_the_suites_and_the_operator_labels(lab, public_golden):
    first = golden.dataset_version(lab.base, SUITE)
    assert lab_files.EVAL_DIGEST.fullmatch(first["id"]) and first["id"].startswith(f"q{open_work.QUESTION_VERSION}.g")
    assert first["id"].endswith(".f-"), "no operator label yet"
    assert golden.dataset_version(lab.base, SUITE)["id"] == first["id"], "the same content keeps its version"
    keys = lab.unlabelled(["a synthetic text the operator answered"])
    row = lab.operator(keys[0], "agree", at="2026-11-09T01:02:03Z")
    labelled = golden.dataset_version(lab.base, SUITE)["id"]
    assert labelled.endswith(".f20261109T010203Zn1")
    lab.undo(row["answer_id"])
    assert golden.dataset_version(lab.base, SUITE)["id"].endswith(".f-")
    public_golden.write_text(
        public_golden.read_text()
        + json.dumps({"id": "case-new", "expected": True, "state": {"hand_off": "Step 4 is still to do."}})
        + "\n"
    )
    grown = golden.dataset_version(lab.base, SUITE)
    assert grown["golden"]["public_cases"] == 3 and len(lab_files.read_dataset_versions(lab.base, SUITE)) == 2


# ── the shadow pass ───────────────────────────────────────────────────────────


def test_shadow_scores_each_live_state_once_beside_the_p_it_was_served_at(lab):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    assert shadow.run(SUITE, lab.base, router=FakeRouter(p_of))[1]["note"] == "no @challenger is registered"
    register(lab.base, SURFACE, lab.checkpoint(RIVAL, train_texts=[]), {"eval_items": 1}, as_="challenger")
    ids = decide(lab, ["a live open closing text", "a finished closing text"], p=0.65)
    decide(lab, ["a text the question no longer asks"], question_version=open_work.QUESTION_VERSION - 1)
    router = FakeRouter(p_of)
    code, summary = shadow.run(SUITE, lab.base, router=router)
    assert code == 0 and summary["challengers"][RIVAL]["new"] == 2 and summary["skipped"] == {"question_changed": 1}
    found, _code = lab_files.read_shadow(lab.base, SURFACE)
    by_id = {row["decision_id"]: row for row in found}
    assert set(by_id) == set(ids)
    live = by_id[ids[0]]
    assert (live["served_p"], live["challenger_p"], live["served_threshold"], live["challenger_threshold"]) == (
        0.65,
        0.8,
        0.62,
        0.7,
    )
    assert live["served_checkpoint"] == CHECKPOINT and live["challenger_digest"].startswith("sha256:")
    assert "closing text" not in paths.shadow_path(lab.base, SURFACE).read_text(), "keys and numbers only"

    again = FakeRouter(p_of)
    assert shadow.run(SUITE, lab.base, router=again)[1]["challengers"][RIVAL]["new"] == 0 and again.calls == []
    assert shadow.run(SUITE, lab.base, router=FakeRouter(p_of), device="cpu")[1]["challengers"][RIVAL]["new"] == 2, (
        "CPU p is not MPS p"
    )
    registry.reject_challenger(lab.base, SURFACE, "test")
    registry.prune_rejects(lab.base, SURFACE)  # nothing past the retention rule: RIVAL stays on disk, and in the file
    assert len(lab_files.read_shadow(lab.base, SURFACE)[0]) == 4

    for name in (f"{FINE_TUNE}-20261103-0900", f"{FINE_TUNE}-20261104-0900"):
        lab.checkpoint(name, train_texts=[])
    pruned = registry.prune_rejects(lab.base, SURFACE)
    assert [(item["name"], item["shadow_rows"]) for item in pruned] == [(RIVAL, 4)], (
        "the armed REJECT deletes a version's shadow rows with it, and only then"
    )
    assert lab_files.read_shadow(lab.base, SURFACE) == ([], "ok")


@pytest.mark.parametrize("models_json", ['{"version": 2, "versions": {', '{"version": 9}', None])
def test_a_registry_read_that_failed_never_costs_the_shadow_file_a_row(lab, models_json):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    register(lab.base, SURFACE, lab.checkpoint(RIVAL, train_texts=[]), {"eval_items": 1}, as_="challenger")
    decide(lab, ["a live open closing text"])
    assert shadow.run(SUITE, lab.base, router=FakeRouter(p_of))[1]["challengers"][RIVAL]["new"] == 1
    held = paths.shadow_path(lab.base, SURFACE).read_bytes()
    if models_json is None:
        (lab.base / "checkpoints" / RIVAL).rename(lab.base / "checkpoints" / "moved-away")  # a folder gone for a moment
    else:
        lab.path("models.json").write_text(models_json, encoding="utf-8")
    code, summary = shadow.run(SUITE, lab.base, router=FakeRouter(p_of))
    assert paths.shadow_path(lab.base, SURFACE).read_bytes() == held
    if models_json is None:
        assert code == 0 and summary["note"] == "no @challenger is registered"
    else:
        assert code == 1 and "left as it is" in summary["note"]
        assert nightly.step_shadow(SUITE, lab.base, "mps", FakeRouter(p_of))["state"] == "failed"


def test_the_shadow_file_stays_under_its_budget_and_one_its_reader_refuses_is_left(lab, monkeypatch):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    register(lab.base, SURFACE, lab.checkpoint(RIVAL, train_texts=[]), {"eval_items": 1}, as_="challenger")
    decide(lab, ["a live open closing text", "a finished closing text", "another live open text"])
    assert shadow.run(SUITE, lab.base, router=FakeRouter(p_of))[1]["challengers"][RIVAL]["rows"] == 3
    path = paths.shadow_path(lab.base, SURFACE)
    size = len(path.read_bytes())
    monkeypatch.setattr(shadow, "SHADOW_WRITE_BYTES", size)
    code, summary = shadow.run(SUITE, lab.base, router=FakeRouter(p_of), device="cpu")
    assert code == 0 and summary["challengers"][RIVAL]["new"] == 3 and summary["challengers"][RIVAL]["rows"] < 6
    assert len(path.read_bytes()) <= size, "never a file its own reader would refuse"
    assert lab_files.read_shadow(lab.base, SURFACE)[0][-1]["device"] == "cpu", "the newest rows are the ones kept"

    monkeypatch.setattr(lab_files, "SHADOW_CAP_BYTES", size // 2)
    held = path.read_bytes()
    code, summary = shadow.run(SUITE, lab.base, router=FakeRouter(p_of))
    assert code == 1 and summary["note"] == "the shadow file is too_large; it is left as it is"
    assert path.read_bytes() == held


def test_prune_never_trusts_a_registry_view_that_read_empty(lab):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT), {"eval_items": 1})
    for day in range(1, 4):
        lab.checkpoint(f"{FINE_TUNE}-2026090{day}-0900")
    models = lab.path("models.json")
    models.write_text(models.read_text() + " " * paths.MODELS_CAP_BYTES, encoding="utf-8")  # valid JSON, over the cap
    assert paths.registry_versions(lab.base)["version"] is None
    with pytest.raises(SystemExit, match="nothing pruned"):
        registry.prune_rejects(lab.base, SURFACE)
    assert (lab.base / "checkpoints" / CHECKPOINT).is_dir() and len(list((lab.base / "checkpoints").iterdir())) >= 4


# ── the pack ──────────────────────────────────────────────────────────────────


def test_the_nightly_pack_runs_every_step_reuses_what_did_not_change_and_reads_a_verdict(lab, public_golden, monkeypatch):
    v1(lab, cut_at=None)
    operator_labels(lab)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "aaaaaaaaaaaa")
    assert nightly.run(SUITE, lab.base, router=FakeRouter(p_of)) == 0
    _path, first = lab_files.newest_nightly(lab.base, SUITE)
    steps = first["steps"]
    assert steps["registry"]["state"] == "ran" and steps["registry"]["migrated"]
    assert set(steps["golden"]["targets"]) == {"english", FINE_TUNE} and steps["golden"]["state"] == "ran"
    assert steps["score"]["state"] == "ran" and steps["compare"]["state"] == "ran"
    assert "floor" in steps["compare"]["notes"], "the champion trained on no rows: the floor cannot train"
    assert steps["shadow"]["state"] == "skipped"
    assert steps["stability"]["state"] == "skipped" and "no unlabelled live state" in steps["stability"]["why"]
    assert steps["readiness"]["state"] == "ran" and list(steps["readiness"]["dry_runs"]) == ["served"]
    reserve = review_data.sweep_readiness(lab.base, SURFACE)["reserve"]
    assert steps["readiness"]["reserve"] == reserve and steps["readiness"]["dry_runs"]["served"]["reserve"] == reserve["rows"]
    assert 0 < reserve["rows"] < steps["readiness"]["rows"], "the label budget's reserve, beside the clean count"
    assert first["verdict"] == "PASS" and first["dataset_version"].startswith(f"q{open_work.QUESTION_VERSION}.g")
    verdict, why = lab_files.run_verdict(lab.base, "nightly", SUITE)
    assert why == "" and verdict["verdict"] == "PASS" and verdict["accelerator"] == "mps"
    assert verdict["model_digest"] == steps["golden"]["targets"][FINE_TUNE]["digest"]
    assert verdict["headline"]["cases"] == 2 and verdict["headline"]["failed"] == 0
    assert paths.registry_view(lab.base)["models"][FINE_TUNE]["checkpoint"] == CHECKPOINT, "nothing served moved"
    assert "thresholds" not in paths.registry_view(lab.base) or not paths.registry_view(lab.base)["thresholds"], (
        "a dry run never applies a cut"
    )

    register(lab.base, SURFACE, lab.checkpoint(RIVAL, train_texts=[]), {"eval_items": 1}, as_="challenger")
    decide(lab, ["a live open closing text"])
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "bbbbbbbbbbbb")
    router = FakeRouter(p_of)
    assert nightly.run(SUITE, lab.base, router=router) == 0
    _path, second = lab_files.newest_nightly(lab.base, SUITE)
    targets = second["steps"]["golden"]["targets"]
    assert targets["english"]["reused"] and targets[FINE_TUNE]["reused"] and not targets[RIVAL]["reused"]
    assert second["steps"]["registry"]["state"] == "skipped"
    assert second["steps"]["shadow"]["challengers"][RIVAL]["new"] == 1
    stable = second["steps"]["stability"]
    assert stable["state"] == "ran" and stable["scored"] == {CHECKPOINT: 1, RIVAL: 1} and stable["meter"]["states"] == 1
    assert [(pair["from"], pair["to"], pair["kappa"]) for pair in stable["meter"]["pairs"]] == [(CHECKPOINT, RIVAL, None)], (
        "one shared state is no κ yet"
    )
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "dddddddddddd")
    assert nightly.run(SUITE, lab.base, router=FakeRouter(p_of)) == 0
    assert lab_files.newest_nightly(lab.base, SUITE)[1]["steps"]["stability"]["state"] == "reused"
    assert list(second["steps"]["readiness"]["dry_runs"]) == [RIVAL], "the champion was swept at these labels already"
    assert second["steps"]["readiness"]["dry_runs"][RIVAL]["reserve"] == reserve["rows"], "the challenger's dry run too"
    assert f"laya:{RIVAL}" in second["steps"]["compare"]["voters"]
    assert lab_files.run_verdict(lab.base, "nightly", SUITE)[0]["verdict"] == "PASS"


def test_a_failed_step_costs_that_step_only_and_fails_the_verdict(lab, public_golden, monkeypatch):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})
    operator_labels(lab, 3, 3)

    def broken(*_a, **_kw):
        raise ValueError(f"synthetic failure under {os.path.expanduser('~')}/x")

    def out_of_memory(*_a, **_kw):
        raise RuntimeError("MPS backend out of memory (MPS allocated: 17.10 GB)")

    monkeypatch.setattr(nightly.score, "run", broken)
    monkeypatch.setattr(nightly.compare, "evaluate", out_of_memory)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "cccccccccccc")
    assert nightly.run(SUITE, lab.base, router=FakeRouter(p_of)) == 0
    _path, report = lab_files.newest_nightly(lab.base, SUITE)
    assert report["steps"]["score"]["state"] == "failed" and "~/x" in report["steps"]["score"]["why"]
    assert report["steps"]["score"]["error"] == "ValueError"
    assert report["steps"]["compare"] == {
        **report["steps"]["compare"],
        "state": "failed",
        "error": "RuntimeError",
        "why": "MPS backend out of memory (MPS allocated: 17.10 GB)",
    }
    assert report["steps"]["golden"]["state"] == "ran" and report["steps"]["shadow"]["state"] == "skipped"
    assert report["steps"]["readiness"]["state"] == "skipped" and report["verdict"] == "FAIL"
    assert lab_files.run_verdict(lab.base, "nightly", SUITE)[0]["verdict"] == "FAIL", (
        "a torch error is a FAIL, not a missing report"
    )

    def interrupted(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(nightly.compare, "evaluate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        nightly.run(SUITE, lab.base, router=FakeRouter(p_of))


def test_a_refused_lease_ends_the_pack_with_exit_75(lab, public_golden, monkeypatch):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT, train_texts=[]), {"eval_items": 1})

    def refused(*_a, **_kw):
        raise SystemExit(infer.LEASE_REFUSED_EXIT)

    monkeypatch.setattr(nightly.infer, "open_router", refused)
    with pytest.raises(SystemExit) as stop:
        nightly.run(SUITE, lab.base)
    assert stop.value.code == infer.LEASE_REFUSED_EXIT and lab_files.newest_nightly(lab.base, SUITE) is None


def test_the_gpu_claim_counts_only_what_will_load(lab, public_golden):
    register(lab.base, SURFACE, lab.checkpoint(CHECKPOINT), {"eval_items": 1})
    register(lab.base, SURFACE, lab.checkpoint(RIVAL), {"eval_items": 1}, as_="challenger")
    registry.reject_challenger(lab.base, SURFACE, "kept on disk, still registered")
    targets = golden.registered_targets(lab.base, SURFACE)
    assert list(targets) == ["english", FINE_TUNE, RIVAL]
    always = {"english", FINE_TUNE}
    assert nightly.lease_targets(SUITE, lab.base, targets, always, "mps") == ["english", FINE_TUNE, RIVAL]
    golden.evaluate(SUITE, lab.base, targets, router=FakeRouter(p_of), reuse=True, by="nightly")
    assert nightly.lease_targets(SUITE, lab.base, targets, always, "mps") == ["english", FINE_TUNE], (
        "a reject whose golden report stands loads nothing tonight"
    )
    assert nightly.lease_targets(SUITE, lab.base, targets, always, "cpu") == ["english", FINE_TUNE, RIVAL]
    assert nightly.stability_loading(SUITE, lab.base, "mps") == [], "no stop set yet: the stability step loads nothing"
    decide(lab, ["a live open closing text"])
    assert nightly.stability_loading(SUITE, lab.base, "mps") == [FINE_TUNE, RIVAL], (
        "a reject still on disk is loaded for the stop-set states it has no score for"
    )
    target = paths.stop_set_path(lab.base, SURFACE)
    target.parent.mkdir(parents=True)
    target.symlink_to(lab.base / "models.json")
    assert nightly.stability_loading(SUITE, lab.base, "mps") == [], "a stop set its reader refuses sizes nothing"
    assert nightly.run(SUITE, lab.base, router=FakeRouter(p_of)) == 0
    step = lab_files.newest_nightly(lab.base, SUITE)[1]["steps"]["stability"]
    assert step["state"] == "failed" and "left as they are" in step["why"] and target.is_symlink()
