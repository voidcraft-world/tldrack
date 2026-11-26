"""Durable retrain boundaries, using synthetic journals and step bodies only."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from laya_lab import clock, retrain, cli, open_work_data as data
from laya_bridge import lab_files
from conftest import SUITE

#: A Sunday, 04:00 on the test's pinned local clock (conftest pins `clock.ZONE` to UTC): inside OPEN_WINDOW.
NOW = datetime(2026, 11, 15, 4, tzinfo=timezone.utc)


def cycle(lab, monkeypatch):
    monkeypatch.setattr(retrain.infer, "laya_version", lambda: "test")
    monkeypatch.setattr(retrain, "report_lines", lambda _j: [])
    monkeypatch.setattr(retrain.infer, "lease_module", lambda: SimpleNamespace(night_window_open=lambda _at: True))
    journal = retrain.new_journal(SUITE, "mps", retrain.Owed(True, "fixture"), NOW)
    return retrain.Cycle(SUITE, lab.base, journal, clock=lambda: NOW)


def test_failed_step_resumes_without_repeating_completed_steps(lab, monkeypatch):
    c = cycle(lab, monkeypatch)
    seen = []

    def body(step):
        def run(_cycle, _block):
            seen.append(step)
            if step == "train" and seen.count(step) == 1:
                raise RuntimeError("synthetic interruption")
            return {"promote": True} if step == "gate" else {}

        return run

    for step in retrain.STEPS:
        monkeypatch.setitem(retrain.BODIES, step, body(step))
    assert retrain.work(c) == 1
    saved = lab_files.read_retrain_journal(lab.base, SUITE)
    assert saved["steps"]["build"]["state"] == "done"
    assert saved["steps"]["train"]["state"] == "failed"
    c.journal = saved
    assert retrain.work(c) == 0
    assert seen.count("export") == 1 and seen.count("train") == 2
    assert c.journal["state"] == "done"


def test_refused_gate_never_registers_or_sweeps(lab, monkeypatch):
    c = cycle(lab, monkeypatch)
    seen = []
    for step in retrain.STEPS:
        monkeypatch.setitem(
            retrain.BODIES, step, lambda _c, _b, step=step: seen.append(step) or ({"promote": False} if step == "gate" else {})
        )
    assert retrain.work(c) == 0
    assert c.journal["state"] == "refused"
    assert "register" not in seen and "sweep" not in seen and "card" in seen


def test_lease_refusal_is_not_a_failed_attempt(lab, monkeypatch):
    c = cycle(lab, monkeypatch)

    def busy(*_args):
        raise SystemExit(75)

    monkeypatch.setitem(retrain.BODIES, "export", busy)
    assert retrain.work(c) == 75
    assert c.journal["state"] == "waiting"
    assert c.journal["steps"]["export"]["attempts"] == 0


def test_gpu_step_waits_outside_window_even_for_direct_run(lab, monkeypatch):
    c = cycle(lab, monkeypatch)
    for step in ("export", "label", "build"):
        c.journal["steps"][step]["state"] = "done"
    monkeypatch.setattr(retrain.infer, "lease_module", lambda: SimpleNamespace(night_window_open=lambda _at: False))
    monkeypatch.setitem(retrain.BODIES, "train", lambda *_: pytest.fail("trained outside the night window"))
    assert retrain.work(c) == 75
    assert c.journal["steps"]["train"]["attempts"] == 0


def test_corrupt_journal_cannot_restart_a_cycle(lab):
    path = lab_files.retrain_journal_path(lab.base, SUITE)
    path.parent.mkdir()
    path.write_text("{broken")
    with pytest.raises(ValueError, match="unreadable"):
        retrain.run(SUITE, lab.base)
    assert path.read_text() == "{broken"


def test_readiness_and_sunday_gate(lab, monkeypatch):
    monkeypatch.setattr(data, "require_sources", lambda *_: object())
    monkeypatch.setattr(retrain.review_data, "sweep_readiness", lambda *_: {"ready": True, "rows": 40, "open": 20, "done": 20})
    monkeypatch.setattr(retrain, "last_cutoff", lambda *_: (None, None))
    monkeypatch.setattr(retrain, "new_grades", lambda *_: {"new": 10, "train": 5, "reserve": 5})
    assert retrain.owed(SUITE, lab.base, NOW, days=7).owed
    assert not retrain.owed(SUITE, lab.base, NOW, days=6, forced=True).owed
    assert not retrain.owed(SUITE, lab.base, NOW.replace(day=16), days=7).owed
    # The window is the machine's own night: the same instant is Sunday 13:00 nine hours east, and Saturday west.
    monkeypatch.setattr(clock, "ZONE", timezone(timedelta(hours=9)))
    assert not retrain.owed(SUITE, lab.base, NOW, days=7).owed
    monkeypatch.setattr(clock, "ZONE", timezone(timedelta(hours=-8)))
    assert not retrain.in_open_window(NOW) and retrain.in_open_window(NOW + timedelta(hours=8))


def test_the_week_of_the_label_cap_starts_on_the_local_monday(monkeypatch):
    sunday_late = datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc)
    assert data.week_start(sunday_late) == datetime(2026, 11, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "ZONE", timezone(timedelta(hours=1)))  # there it is already Monday 00:30
    assert data.week_start(sunday_late) == datetime(2026, 11, 15, 23, tzinfo=timezone.utc)
    calls = [
        {"at": "2026-11-15T22:00:00Z", "teacher": "sonnet", "asked": 5},
        {"at": "2026-11-15T23:10:00Z", "teacher": "sonnet", "asked": 7},
    ]
    assert data.asked_this_week(calls, "sonnet", sunday_late) == 7, "only the call after the local Monday began counts"


def test_resume_key_and_budget_are_conservative(lab):
    assert data.label_todo({"a": "A", "b": "B"}, {"a": ("sonnet", 2)}, ["b"], teacher="sonnet", question_version=2) == []
    assert len(data.label_todo({"a": "A"}, {"a": ("sonnet", 2)}, [], teacher="sonnet", question_version=3)) == 1
    data.record_label_call(lab.base, "sonnet", asked=40, labelled=0, now=NOW)
    assert data.weekly_left(lab.base, NOW) == 0
    with data.label_calls_path(lab.base).open("a") as f:
        f.write("{bad\n")
    with pytest.raises(SystemExit, match="malformed"):
        data.weekly_left(lab.base, NOW)


def test_retrain_cli_accepts_its_journal_and_registry_arguments():
    parser = cli.build_parser()
    assert parser.parse_args(["retrain", SUITE, "--owed"]).owed
    args = parser.parse_args(["label-open-work", "--teacher", "auto", "--weekly"])
    assert args.teacher == "auto" and args.weekly
    assert parser.parse_args(["verdict", "retrain", SUITE]).kind == "retrain"


TRAINER_LINES = [
    "train items 240, calibration items 26, eval items 40 from eval.jsonl, skipped {}, device mps",
    "optimizer budget: 240 train item(s), effective batch up to 64 (micro_batch 8 x grad_accum 8), 8 optimizer updates over 2 epoch(s)",
    "epoch 1/2 step 5 loss 0.6931",
    "something the trainer may print one day",
    "epoch 1/2 mean loss 0.6100",
    "epoch 2/2 step 10 loss 0.4000",
]


def test_a_fit_s_progress_is_read_off_the_trainer_s_own_lines():
    from laya_lab import train

    reader = train.FitProgress()
    moved = [found for line in TRAINER_LINES if (found := reader.read(line)) is not None]
    assert reader.steps == 30, "240 items in micro-batches of 8"
    assert moved == [
        lab_files.train_progress(1, 2, 5, 30, 0.6931),
        lab_files.train_progress(1, 2, 30, 30, 0.61),
        lab_files.train_progress(2, 2, 10, 30, 0.4),
    ]
    assert train.FitProgress().read("epoch 1/2 step 5 loss 0.5") == lab_files.train_progress(1, 2, 5, None, 0.5)
    assert train.FitProgress().read("epoch 1/2 mean loss 0.5") is None, "an epoch's end needs the epoch's size"


def test_following_a_fit_keeps_the_pane_s_output_and_survives_a_failing_callback(capsys):
    from laya_lab import train

    seen = []
    with train.follow_fit(seen.append):
        print("\n".join(TRAINER_LINES[:3]))
        print("epoch 1/2 step 10", end="")
        print(" loss 0.5")
    assert [block["step"] for block in seen] == [5, 10]
    assert "epoch 1/2 step 5 loss 0.6931" in capsys.readouterr().out, "the pane still shows every line"

    def broken(_progress):
        raise OSError("the journal's disk is full")

    with train.follow_fit(broken):
        print("epoch 1/2 step 5 loss 0.5")
        print("epoch 1/2 step 10 loss 0.4")
    assert "progress is no longer recorded (OSError)" in capsys.readouterr().err
    with train.follow_fit(None):
        print("epoch 1/2 step 5 loss 0.5")


def test_the_train_step_journals_its_progress_and_drops_it_when_the_fit_ends(lab, monkeypatch):
    from laya_lab import train

    c = cycle(lab, monkeypatch)
    out = lab.base / "checkpoints" / "tldrack-agent-open-work-20261101-090000"
    plan = train.Fit("agent_open_work", out, out.with_name(out.name + ".data"), 240, 40, 6, 2, "cpu")
    during = []

    def fit(_base, _plan, on_progress):
        on_progress(lab_files.train_progress(1, 2, 5, 30, 0.69))
        saved = lab_files.read_retrain_journal(lab.base, SUITE)
        during.append(
            lab_files.retrain_view(
                {**saved, "steps": {**saved["steps"], "train": {**saved["steps"]["train"], "state": "running"}}}
            )
        )
        return {"eval_items": 40, "before": {}, "after": {}}

    monkeypatch.setattr(train, "prepare", lambda *_a, **_k: plan)
    monkeypatch.setattr(train, "fit", fit)
    monkeypatch.setattr(train, "base_revision", lambda: "rev")
    monkeypatch.setattr(retrain.infer, "checkpoint_digest", lambda *_a: "sha256:0")
    block = c.journal["steps"]["train"]
    retrain.step_train(c, block)
    assert during[0]["progress"]["done"] == round(5 / 60, 4) and during[0]["progress"]["loss"] == 0.69
    assert "progress" not in block
