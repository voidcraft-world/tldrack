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
