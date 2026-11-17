"""laya-lab without torch: the split, the coordinator export, the registry and the promotion rule."""

from __future__ import annotations

import json
import os
import stat

import pytest

from laya_lab import cli
from laya_lab.data import coordinator_rows_from_scan, export_coordinator, open_private, split_rows, state_bucket, write_jsonl
from laya_lab.registry import available_checkpoints, base_dir, read_registry, register, resolve_model, unregister
from laya_lab.data import DEFAULT_EPOCHS

from laya_bridge import files, lab_files, paths
from laya_bridge.paths import read_extra_models, read_registry_view
from coordinator.laya_rank import OPEN_WORK_QUESTION

from conftest import CHECKPOINT, SURFACE, store_env

#: A second surface with no golden suite, and an invented checkpoint of it.
OTHER_SURFACE = "coordinator"
OTHER_CHECKPOINT = "voidcraft-coordinator-20261005-1200"


def test_split_is_by_state_and_stable():
    rows = [{"state": {"title": f"post {i}"}} for i in range(400)]
    train, held_out = split_rows(rows)
    assert len(train) + len(held_out) == 400
    assert 30 <= len(held_out) <= 90
    again_train, again_held = split_rows(list(reversed(rows)))
    assert sorted(json.dumps(r) for r in again_held) == sorted(json.dumps(r) for r in held_out)
    assert state_bucket({"a": 1, "b": 2}) == state_bucket({"b": 2, "a": 1})


SCAN = {
    "ranked_by": "claude",
    "findings": [
        {
            "key": "s1:stated-next-step",
            "kind": "stated-next-step",
            "headline": "write the habits migration",
            "label": "Next:",
            "work_class": "open-work",
        },
        {
            "key": "s2:stated-next-step",
            "kind": "stated-next-step",
            "headline": "restart the bridge",
            "label": "Next:",
            "work_class": "routine",
        },
        {"key": "s3:blocked", "kind": "blocked", "headline": "waiting at a prompt", "label": "", "work_class": None},
    ],
}


def test_only_claude_ranked_findings_become_rows_with_the_inference_question():
    rows = coordinator_rows_from_scan(SCAN)
    assert set(rows) == {"s1:stated-next-step", "s2:stated-next-step"}
    assert rows["s1:stated-next-step"] == {
        "state": {"headline": "write the habits migration", "label": "Next:"},
        "questions": {"q": OPEN_WORK_QUESTION},
        "expected": {"q": True},
    }
    assert rows["s2:stated-next-step"]["expected"] == {"q": False}
    assert coordinator_rows_from_scan({**SCAN, "ranked_by": "laya"}) == {}


def test_export_accumulates_across_audits(tmp_path):
    store = tmp_path / "store.json"
    dataset = tmp_path / "data" / "coordinator.jsonl"
    store.write_text(json.dumps({"version": 1, "scan": SCAN, "dismissed": []}))
    assert export_coordinator(dataset, store) == (2, 2)
    later = {
        "ranked_by": "claude",
        "findings": [
            {
                "key": "s9:stated-next-step",
                "kind": "stated-next-step",
                "headline": "decide the export format",
                "label": "Next:",
                "work_class": "open-work",
            }
        ],
    }
    store.write_text(json.dumps({"version": 1, "scan": later, "dismissed": []}))
    assert export_coordinator(dataset, store) == (1, 3)


def test_registry_round_trips_through_the_plugins_reader(tmp_path):
    base = tmp_path / "laya"
    checkpoint = base / "checkpoints" / OTHER_CHECKPOINT
    checkpoint.mkdir(parents=True)
    assert register(base, OTHER_SURFACE, checkpoint, {"eval_items": 120}) == "voidcraft-coordinator"
    assert read_extra_models(store_env(tmp_path)) == {"voidcraft-coordinator": str(checkpoint.resolve())}
    payload = read_registry(base)
    assert payload["versions"][checkpoint.name]["eval"] == {"eval_items": 120}
    assert payload["aliases"][OTHER_SURFACE] == {"champion": checkpoint.name, "challenger": None, "previous": None}
    assert unregister(base, OTHER_SURFACE) is True
    assert read_extra_models(store_env(tmp_path)) == {}


def test_registry_refuses_a_checkpoint_outside_the_store(tmp_path):
    (tmp_path / "laya").mkdir()
    with pytest.raises(SystemExit):
        register(tmp_path / "laya", OTHER_SURFACE, tmp_path, {})


def test_register_records_how_the_checkpoint_was_trained(tmp_path):
    base = tmp_path / "laya"
    checkpoint = base / "checkpoints" / CHECKPOINT
    checkpoint.mkdir(parents=True)
    trained = lab_files.trained_entry(2, 400, 70, "55cf4c4", "2026-10-10T10:00:00Z")
    register(base, SURFACE, checkpoint, {"eval_items": 70}, trained=trained)
    assert read_registry(base)["versions"][CHECKPOINT]["trained"] == trained
    view = read_registry_view(store_env(tmp_path))
    assert view["models"]["voidcraft-agent-open-work"]["trained"] == {**trained}
    register(base, SURFACE, checkpoint, {"eval_items": 70})
    assert "trained" not in read_registry(base)["versions"][CHECKPOINT]


def test_train_defaults_to_two_epochs():
    assert DEFAULT_EPOCHS == 2
    assert cli.build_parser().parse_args(["train", "agent_open_work"]).epochs == 2


def test_the_bridge_argv_parses():
    """Every lab button's argv (`laya_bridge.lab_jobs.LAB_ARGS`, what a press runs after the laya-lab binary) parses
    here, and none of them is a verb the plugin refuses to start."""
    from laya_bridge import lab_jobs

    parser = cli.build_parser()
    assert set(lab_jobs.LAB_ARGS[SURFACE]) == set(lab_jobs.LAB_VERBS)
    for verb in lab_jobs.LAB_VERBS:
        args = lab_jobs.lab_args(verb, SURFACE)
        assert args == lab_jobs.LAB_ARGS[SURFACE][verb] and not lab_jobs.is_forbidden(args), verb
        assert parser.parse_args(list(args)).command in cli.COMMANDS, verb
    assert parser.parse_args(list(lab_jobs.lab_args("sweep", SURFACE))).labels == "operator"
    assert lab_jobs.LAB_BIN == "lab/.venv/bin/laya-lab"


def test_every_documented_command_line_parses():
    """The module note of `cli.py` is the lab's command list: each line of it parses, so the list cannot rot."""
    parser = cli.build_parser()
    documented = [line.split("#")[0].split() for line in cli.__doc__.splitlines() if line.startswith("    laya-lab ")]
    assert len(documented) >= 20
    for argv in documented:
        argv = [word if word != "<checkpoint" else "some-checkpoint" for word in argv[1:] if word != "folder>"]
        assert parser.parse_args(argv).command in cli.COMMANDS, argv
    assert {argv[1] for argv in documented} >= set(cli.COMMANDS) - {"card", "bench", "needs-you", "challenger", "retrain"}


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_every_writer_makes_0600_files_and_0700_dirs(tmp_path, umask_022, monkeypatch):
    data = tmp_path / "data"
    base = base_dir(store_env(data))
    assert base == (data / "laya").resolve() and mode(base) == 0o700, "a first run creates the store, private"
    write_jsonl(base / "data" / "deep" / "rows.jsonl", [{"a": 1}])
    assert mode(base / "data") == mode(base / "data" / "deep") == 0o700
    assert mode(base / "data" / "deep" / "rows.jsonl") == 0o600
    wide = base / "data" / "wide.jsonl"
    wide.write_text("")
    os.chmod(wide, 0o644)
    with open_private(wide, "a") as handle:
        handle.write("x\n")
    assert mode(wide) == 0o600
    with open_private(base / "evals" / "report.json", "w") as handle:
        handle.write("{}\n")
    assert mode(base / "evals") == 0o700 and mode(base / "evals" / "report.json") == 0o600
    checkpoint = base / "checkpoints" / OTHER_CHECKPOINT
    checkpoint.mkdir(parents=True)
    register(base, OTHER_SURFACE, checkpoint, {})
    assert mode(base / "models.json") == 0o600


def test_open_private_refuses_a_link(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "data" / "link.jsonl"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(SystemExit):
        open_private(link, "a")


def test_every_command_tightens_the_store_first(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    (data / "laya" / "data").mkdir(parents=True)
    loose = data / "laya" / "data" / "agent-open-work-labels.jsonl"
    loose.write_text("")
    os.chmod(loose, 0o644)
    monkeypatch.setenv(paths.DATA_ENV, str(data))
    order: list[str] = []
    real = files.tighten_params
    monkeypatch.setattr(files, "tighten_params", lambda base: order.append("tighten") or real(base))
    for name in cli.COMMANDS:
        monkeypatch.setitem(cli.COMMANDS, name, lambda args, base, name=name: order.append(name) or 0)
    argv = {
        "status": [],
        "export-coordinator": [],
        "train": [OTHER_SURFACE],
        "models": [],
        "golden": ["agent-open-work"],
        "settle": ["agent-open-work"],
        "export-open-work": [],
        "label-open-work": [],
        "review-open-work": [],
        "sweep": ["agent-open-work"],
        "promote": ["agent-open-work"],
        "compare": ["agent-open-work"],
        "gate": ["agent-open-work"],
        "unregister": [OTHER_SURFACE],
        "verdict": ["golden", "agent-open-work"],
        "nightly": ["agent-open-work"],
        "shadow": ["agent-open-work"],
        "card": [],
        "bench": [],
        "needs-you": [],
        "challenger": ["promote", "--surface", "agent_open_work", "--token", "abc"],
        "retrain": ["agent-open-work"],
        "registry": ["migrate"],
        "latency": ["agent-open-work"],
        "stability": ["agent-open-work"],
        "teacher-kappa": [],
        "fleet": [],
        "question-challenger": ["agent_open_work", "--version", "3"],
    }
    assert set(argv) == set(cli.COMMANDS)
    for name, extra in argv.items():
        order.clear()
        assert cli.main([name, *extra]) == 0
        assert order == ["tighten", name], name
    assert mode(loose) == 0o600
    assert "tightened data/agent-open-work-labels.jsonl → 0600" in capsys.readouterr().out


def test_status_prints_the_ops_state_without_torch(tmp_path, monkeypatch, capsys):
    import sys

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv(paths.DATA_ENV, str(data))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[laya-lab] status · ") and "served   english (built-in) · cut 0.7 code" in out
    assert not (data / "laya").exists(), "status is a read: it never creates the tree"
    assert "torch" not in sys.modules


def test_available_checkpoints_dedupes_and_needs_weights(tmp_path):
    base = tmp_path / "laya"
    registered = base / "checkpoints" / CHECKPOINT
    for folder in (
        registered,
        base / "checkpoints" / "voidcraft-agent-open-work-20261001-0900",
        base / "checkpoints" / "voidcraft-agent-open-work-20261002-0900",
        base / "checkpoints" / "voidcraft-coordinator-20261001-0900",
    ):
        folder.mkdir(parents=True)
    for folder in (
        registered,
        base / "checkpoints" / "voidcraft-agent-open-work-20261001-0900",
        base / "checkpoints" / "voidcraft-coordinator-20261001-0900",
    ):
        (folder / "model.safetensors").write_bytes(b"w")
    (base / "checkpoints" / f"{CHECKPOINT}.data").mkdir()
    assert list(available_checkpoints(base, SURFACE)) == ["voidcraft-agent-open-work-20261001-0900", CHECKPOINT]
    register(base, SURFACE, registered, {})
    assert list(available_checkpoints(base, SURFACE)) == ["voidcraft-agent-open-work", "voidcraft-agent-open-work-20261001-0900"]
    # the registered folder's own name still names it: `gate --served <folder>` after it was registered
    assert resolve_model(base, SURFACE, CHECKPOINT) == ("voidcraft-agent-open-work", registered.resolve())
    with pytest.raises(SystemExit, match="no model"):
        resolve_model(base, SURFACE, "voidcraft-agent-open-work-20261002-0900")  # no weights
