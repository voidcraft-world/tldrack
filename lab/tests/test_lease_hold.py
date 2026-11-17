"""The resource lease is optional (laya_lab/lease.py): with none configured the lab just runs, and with one configured
a lab command holds its slot while its models load (infer.py `hold_slot`).

The lease module here is invented for the tests: an in-memory book with a capacity per slot. conftest unsets
`lease.MODULE_ENV` before every test, so none of them takes the machine's own lease."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from laya_lab import infer, lease, train

INVENTED_LEASE = """
LEASE_SCHEDULED_ENV = "INVENTED_LEASE_SCHEDULED"
WAIT_POLL_S = 0.5
OLLAMA_URL_ENV = "INVENTED_OLLAMA_URL"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
GIB = 1024 ** 3
OLLAMA_RESIDENT_FACTOR = 1.0
ROUTER_TARGET_GB = 2.0
CAPACITY = {"gpu": 8.0, "cpu": 8, "subscription_claude": 1}
BOOK = []        # every ticket held
YIELD = []       # the reasons `should_yield` hands a scheduled holder, one a look
NIGHT = [True]
MINUTES = [None]


class LeaseRefused(Exception):
    pass


class Held:
    def __init__(self, ticket, reentered):
        self.ticket, self.reentered = ticket, reentered

    def release(self):
        if self.ticket in BOOK:
            BOOK.remove(self.ticket)
            return True
        return False

    def should_yield(self):
        return YIELD.pop(0) if self.ticket["wait_s"] and YIELD else None


def router_gb(targets):
    return max(1, targets) * ROUTER_TARGET_GB


def take(slot, units, holder, *, kind, models=(), wait_s=0.0):
    mine = next((t for t in BOOK if t["slot"] == slot and t["holder"] == holder), None)
    if mine is not None:
        return Held(mine, True)
    held = [t for t in BOOK if t["slot"] == slot]
    if sum(t["units"] for t in held) + units > CAPACITY[slot]:
        raise LeaseRefused(f"{slot} is held by " + ", ".join(f"{t['holder']} {t['units']}" for t in held))
    ticket = {"slot": slot, "units": units, "holder": holder, "kind": kind, "models": list(models), "wait_s": wait_s}
    BOOK.append(ticket)
    return Held(ticket, False)


def night_window_open(now=None):
    return NIGHT[0]


def run_minutes_left():
    return MINUTES[0]
"""


@pytest.fixture(autouse=True)
def as_command(monkeypatch):
    monkeypatch.setattr("sys.argv", ["laya-lab", "golden", "agent-open-work"])
    monkeypatch.setattr(infer, "HELD", {})
    monkeypatch.delitem(sys.modules, lease.CONFIGURED_NAME, raising=False)


@pytest.fixture
def configured(tmp_path, monkeypatch) -> ModuleType:
    """The invented lease, configured the way an operator configures theirs."""
    path = tmp_path / "invented_lease.py"
    path.write_text(INVENTED_LEASE, encoding="utf-8")
    monkeypatch.setenv(lease.MODULE_ENV, str(path))
    return infer.lease_module()


class Router:
    """Answers p(yes) = 0.5 for every state, and counts how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    def predict(self, _state, _questions, *, model):
        self.asked += 1
        return {"answers": {"q": {"noul": 0.5}}}


# ── no lease: the default ─────────────────────────────────────────────────────


def test_with_no_lease_configured_every_command_just_runs(monkeypatch):
    assert infer.lease_module() is lease, "the seam is its own no-lease answer"
    for take in (
        lambda: infer.hold_device("mps", targets=2, models=["english", "a-checkpoint"]),
        lambda: infer.hold_device("cpu"),
        lambda: infer.hold_device("mps", gb=train.TRAIN_GB),
        lambda: infer.hold_slot("subscription_claude", "claude", 1, ["sonnet"]),
    ):
        held = take()
        assert held.should_yield() is None and held.release() is False, "nothing is held, so nothing is released"
    monkeypatch.setenv(lease.LEASE_SCHEDULED_ENV, "1")  # even a scheduled command has nothing to wait for
    monkeypatch.setattr(infer.time, "sleep", lambda _s: pytest.fail("a command waited with no lease"))
    infer.hold_device("mps")
    assert infer.predict_noul(Router(), "english", ["a", "b"], {"type": "noul"}) == [0.5, 0.5]


def test_with_no_lease_the_night_is_always_open_and_a_run_has_no_deadline():
    assert lease.night_window_open() and lease.night_window_open(0.0)
    assert lease.run_minutes_left() is None
    assert lease.router_gb(0) == lease.ROUTER_TARGET_GB and lease.router_gb(3) == pytest.approx(3 * lease.ROUTER_TARGET_GB)
    assert train.TRAIN_GB > lease.ROUTER_TARGET_GB * 4, "a fine-tune declares more than four loaded checkpoints"


def test_a_configured_lease_that_does_not_load_stops_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv(lease.MODULE_ENV, str(tmp_path / "missing.py"))
    with pytest.raises(SystemExit, match=lease.MODULE_ENV):
        infer.hold_device("mps")
    broken = tmp_path / "broken.py"
    broken.write_text("raise RuntimeError('invented')\n", encoding="utf-8")
    monkeypatch.setenv(lease.MODULE_ENV, str(broken))
    with pytest.raises(SystemExit, match="did not load"):
        infer.hold_device("mps")
    monkeypatch.setenv(lease.MODULE_ENV, "relative/lease.py")
    with pytest.raises(SystemExit, match="absolute"):
        infer.lease_module()


# ── a configured lease ────────────────────────────────────────────────────────


def test_the_configured_module_is_loaded_once_and_is_the_lease_in_force(configured):
    assert configured is not lease and configured.ROUTER_TARGET_GB == 2.0
    assert infer.lease_module() is configured, "one load a process"


def test_a_router_holds_one_checkpoint_of_gpu_per_target_it_loads(configured):
    held = infer.hold_device("mps", targets=2, models=["english", "a-checkpoint"])
    [ticket] = configured.BOOK
    assert (ticket["holder"], ticket["kind"], ticket["units"], ticket["wait_s"]) == ("laya-lab golden", "mps", 4.0, 0.0)
    assert ticket["models"] == ["english", "a-checkpoint"]
    assert infer.hold_device("mps").reentered, "a second load in the same command is the same hold"
    assert {"gpu": held} == infer.HELD
    assert held.release() and configured.BOOK == []


def test_cpu_torch_holds_cores_and_a_fine_tune_claims_its_own_gb(configured):
    infer.hold_device("cpu")
    assert [(t["slot"], t["kind"], t["units"]) for t in configured.BOOK] == [("cpu", "cpu", infer.LAB_CPU_CORES)]
    configured.BOOK.clear()
    infer.HELD.clear()
    infer.hold_device("mps", gb=train.TRAIN_GB, models=["a-checkpoint"])
    assert [(t["slot"], t["units"]) for t in configured.BOOK] == [("gpu", train.TRAIN_GB)]


def test_a_full_gpu_exits_75_saying_who_holds_it(configured, capsys):
    configured.BOOK.append({"slot": "gpu", "units": 8.0, "holder": "another job", "kind": "ollama", "models": [], "wait_s": 0.0})
    with pytest.raises(SystemExit) as stopped:
        infer.hold_device("mps")
    assert stopped.value.code == infer.LEASE_REFUSED_EXIT == 75
    assert "another job 8.0" in capsys.readouterr().err
    assert len(configured.BOOK) == 1 and infer.HELD == {}, "a refused take holds nothing"


def test_the_subscription_slot_holds_one_labeller(configured):
    infer.hold_slot("subscription_claude", "claude", 1, ["sonnet"])
    assert [(t["slot"], t["units"], t["models"]) for t in configured.BOOK] == [("subscription_claude", 1, ["sonnet"])]


def test_a_scheduled_command_queues_and_steps_aside_between_predictions(configured, monkeypatch, capsys):
    monkeypatch.setenv(configured.LEASE_SCHEDULED_ENV, "1")
    infer.hold_device("mps")
    assert configured.BOOK[0]["wait_s"] == infer.SCHEDULED_WAIT_S, "a scheduled command may queue for its slot"
    configured.YIELD.append(("live", "live work wants the machine"))
    router, slept = Router(), []
    monkeypatch.setattr(infer.time, "sleep", lambda seconds: slept.append((router.asked, seconds)))
    assert infer.predict_noul(router, "english", ["a", "b", "c"], {"type": "noul"}) == [0.5, 0.5, 0.5]
    assert slept == [(0, configured.WAIT_POLL_S)], "it paused before its first prediction, then ran on"
    assert "paused: live work wants the machine" in capsys.readouterr().err


def test_a_hand_started_command_never_pauses(configured, monkeypatch):
    held = infer.hold_device("mps")
    assert {"gpu": held} == infer.HELD
    configured.YIELD.append(("live", "live work wants the machine"))
    monkeypatch.setattr(infer.time, "sleep", lambda _s: pytest.fail("a hand-started command waited"))
    assert infer.predict_noul(Router(), "english", ["a"], {"type": "noul"}) == [0.5]
