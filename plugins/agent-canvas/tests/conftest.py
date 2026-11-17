"""Shared fakes: a canvas dir per test, a scripted tmux, a scripted bridge."""

from __future__ import annotations

import json
import os
import time

import pytest

from agent_canvas.paths import CANVAS_DIR_ENV

RULE = "─" * 40
IDLE_PANE = f"output\n{RULE}\n❯ \n{RULE}\n  92% left\n"


class FakeTmux:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}  # session → canvas key
        self.panes: dict[str, str] = {}  # session → capture text
        self.typed: list[tuple[str, str]] = []
        self.type_ok = True

    def key_for_session(self, session):
        return self.keys.get(session)

    def sessions_by_key(self):
        return {key: name for name, key in self.keys.items()}

    def capture(self, session):
        return self.panes.get(session)

    def type_line(self, session, line):
        if self.type_ok:
            self.typed.append((session, line))
        return self.type_ok


class FakeHost:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.raised: list[tuple[str, str]] = []

    def session_rows(self):
        return self.rows

    def raise_attention(self, session, message):
        self.raised.append((session, message))
        self.rows.setdefault(session, {})["state"] = "attention"
        self.rows[session]["message"] = message
        return True


@pytest.fixture()
def canvas_root(tmp_path, monkeypatch):
    root = tmp_path / "canvas"
    root.mkdir()
    monkeypatch.setenv(CANVAS_DIR_ENV, str(root))
    return root


@pytest.fixture()
def tmux():
    return FakeTmux()


@pytest.fixture()
def host():
    return FakeHost()


def write_canvas(root, key: str, html: str, *, mtime: float | None = None, suffix: str = ".html"):
    path = root / f"{key}{suffix}"
    path.write_text(html, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class Clock:
    def __init__(self, start: float | None = None) -> None:
        self.now = start if start is not None else time.time()

    def __call__(self) -> float:
        return self.now


def write_doc(root, key: str, *blocks, mtime: float | None = None, **extra):
    """A typed (JSON) canvas — `doc.py`."""
    return write_canvas(root, key, json.dumps({"blocks": list(blocks), **extra}), mtime=mtime, suffix=".json")
