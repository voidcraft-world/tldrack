"""Live work marks the operator's resource lease and never takes a ticket (live_marks.py)."""

import pytest

from tldrack import live_marks
from tldrack.open_work import plugin_caller


class Plugin:
    def handle_get(self, path, query):
        return 200, {"models": []}

    def handle_post(self, path, query, body):
        return 200, {"answers": {}}


LEASE = """
import pathlib, os
def mark_live(name):
    if name == "boom":
        raise RuntimeError("the lease is broken")
    pathlib.Path(os.environ["LEASE_OUT"], name).write_text("marked")
"""


@pytest.fixture(autouse=True)
def no_lease_after():
    yield
    live_marks.configure(None)


def test_no_lease_is_configured_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_OUT", str(tmp_path))
    live_marks.configure(None)
    live_marks.mark(live_marks.NEXT_STEP)
    assert list(tmp_path.iterdir()) == []


def test_a_mark_reaches_the_configured_lease_module(tmp_path, monkeypatch):
    lease = tmp_path / "lease.py"
    lease.write_text(LEASE)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("LEASE_OUT", str(out))
    live_marks.configure(lease)
    live_marks.mark(live_marks.NEXT_STEP)
    assert [p.name for p in out.iterdir()] == ["next-step"]


def test_a_mark_never_raises(tmp_path):
    lease = tmp_path / "lease.py"
    lease.write_text(LEASE)
    live_marks.configure(lease)
    live_marks.mark("boom")  # the lease raises
    live_marks.configure(tmp_path / "missing.py")
    live_marks.mark(live_marks.LAYA_DECIDE)  # no such file
    broken = tmp_path / "broken.py"
    broken.write_text("def mark_live(:")
    live_marks.configure(broken)
    live_marks.mark(live_marks.LAYA_DECIDE)  # not Python


def test_the_caller_marks_around_each_decide_and_nothing_else():
    marks = []
    call = plugin_caller(Plugin(), on_decide=lambda: marks.append("mark"))
    assert call("/status", None)[0] == 200
    assert marks == []
    assert call("/decide", {"state": "x"})[0] == 200
    assert marks == ["mark", "mark"], "before and after the decide"
    assert plugin_caller(Plugin())("/decide", {})[0] == 200, "no mark without the hook"


def test_the_next_step_chat_marks_around_each_ollama_call(monkeypatch):
    from voidcraft_world_bridge.local_llm import ollama

    from tldrack import next_step

    seen = []
    monkeypatch.setattr(ollama, "chat", lambda *_a, **_k: seen.append("call") or {"text": "{}"})
    monkeypatch.setattr(ollama, "base_url", lambda: "http://127.0.0.1:11434")
    assert next_step.ollama_chat(on_call=lambda: seen.append("mark"))([], {}) == "{}"
    assert seen == ["mark", "call", "mark"]
    assert next_step.ollama_chat()([], {}) == "{}", "no mark without the hook"
