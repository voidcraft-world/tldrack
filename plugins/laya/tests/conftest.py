"""Shared fixtures for the laya plugin's tests.

The autouse fixture comes first and is not optional: no test may resolve the operator's real
store, so the resolver's two variables (`paths.DATA_ENV`, `paths.CONFIG_ENV`) point at EMPTY temp
folders before any test body runs: no `paths.json` is read, and a plugin built with defaults makes
its `laya/` there and nowhere else. A test that needs a tree asks for `tree` (a `data/laya/` under
its own tmp_path) and passes `params_dir=lambda: tree.base` explicitly; one that needs the ledger
OFF passes `params_dir=lambda: None`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from laya_bridge import label_budget, paths, rows  # noqa: E402

FINE_TUNE = "voidcraft-agent-open-work"
#: An invented checkpoint folder name, in the shape laya-lab dates them.
CHECKPOINT = f"{FINE_TUNE}-20260102-0304"
SONNET = "claude-sonnet-5-5"


def in_half(half: str, prefix: str, n: int) -> list[str]:
    """`n` synthetic texts whose key the label budget draws into `half` (`label_budget.drawn`)."""
    found, i = [], 0
    while len(found) < n:
        text = f"{prefix} {i}: a synthetic closing message, long enough to label"
        if label_budget.drawn(rows.state_key(text)) == half:
            found.append(text)
        i += 1
    return found


@pytest.fixture(autouse=True)
def isolated_params(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv(paths.DATA_ENV, str(tmp_path_factory.mktemp("data-empty")))
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path_factory.mktemp("config-empty")))


@pytest.fixture(autouse=True)
def no_drain_thread(monkeypatch: pytest.MonkeyPatch) -> list:
    """No test starts the lab queue's drain thread: a thread outliving its monkeypatches would run a real
    lab job on this machine. A kick is recorded here instead; a test runs the loop itself."""
    from laya_bridge import plugin

    kicked: list = []
    monkeypatch.setattr(plugin, "start_daemon", kicked.append)
    return kicked


class Tree:
    """A synthetic laya store — never operator data."""

    def __init__(self, base: Path) -> None:
        self.base = base

    @property
    def params(self) -> Path:
        return self.base.parent

    def path(self, relative: str) -> Path:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_jsonl(self, relative: str, rows_out: list[dict], header: str | None = None) -> Path:
        path = self.path(relative)
        lines = ([header] if header else []) + [json.dumps(row, sort_keys=True) for row in rows_out]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def append_jsonl(self, relative: str, row: dict) -> None:
        with self.path(relative).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def unlabelled(self, texts: list[str]) -> list[str]:
        self.write_jsonl(
            "data/agent-open-work-unlabelled.jsonl", [{"key": rows.state_key(text), "hand_off": text} for text in texts]
        )
        return [rows.state_key(text) for text in texts]

    def labels(self, labelled: dict[str, bool], by: str = SONNET) -> None:
        self.write_jsonl(
            "data/agent-open-work-labels.jsonl",
            [{"key": key, "open": is_open, "why": f"why {key[:4]}", "by": by} for key, is_open in labelled.items()],
        )

    def models(self, payload: dict[str, Any]) -> None:
        self.path("models.json").write_text(json.dumps(payload), encoding="utf-8")

    def register(
        self,
        *,
        checkpoint: str = CHECKPOINT,
        train_texts: list[str] | None = (),
        thresholds: dict | None = None,
        extra_models: dict | None = None,
    ) -> Path:
        """A registered open-work fine-tune. `train_texts=None` leaves it without a training record."""
        folder = self.path(f"checkpoints/{checkpoint}")
        folder.mkdir(exist_ok=True)
        if train_texts is not None:
            self.write_jsonl(
                f"checkpoints/{checkpoint}.data/train.jsonl",
                [{"state": text, "questions": {}, "expected": {"q": True}} for text in train_texts],
            )
        metrics = {"accuracy": 0.8, "brier": 0.15, "ece": 0.05, "loss": 0.4}
        models = {
            FINE_TUNE: {
                "path": f"checkpoints/{checkpoint}",
                "surface": "agent_open_work",
                "eval": {"before": metrics, "after": metrics, "eval_items": 12},
            }
        }
        models.update(extra_models or {})
        self.models({"version": 1, "models": models, **({"thresholds": thresholds} if thresholds else {})})
        return folder.resolve()


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    base = tmp_path / "data" / "laya"
    base.mkdir(parents=True)
    return Tree(base)


class FakeRuntime:
    """Stands in for LayaRuntime on route tests: no subprocess, answers a noul at `p`."""

    def __init__(
        self,
        *,
        state: str = "ready",
        p: float = 0.8,
        loaded: dict[str, str] | None = None,
        payload_model: str = "laya-rl-agent",
        installed: bool = True,
    ) -> None:
        self.state, self.p, self._loaded, self.payload_model, self.installed = state, p, loaded, payload_model, installed
        self.decided: list[dict] = []
        self.calls: list[str] = []
        self.spawns = 1

    def spawn(self) -> int:
        return self.spawns

    def status(self) -> dict:
        return {"state": self.state, "installed": self.installed, "models": sorted(self._loaded or {})}

    def loaded(self) -> dict[str, str] | None:
        return dict(self._loaded) if self._loaded is not None and self.state == "ready" else None

    def served_identity(self, name: str) -> tuple[str | None, str | None]:
        path = (self._loaded or {}).get(name)
        if path:
            return f"laya-local/{name}@{Path(path).name}", path
        return (f"laya-local/{name}@55cf4c4", None) if name == "english" else (None, None)

    def ensure_started(self) -> str:
        self.calls.append("ensure_started")
        return self.state

    def stop(self, reason: str = "requested") -> None:
        self.calls.append(f"stop:{reason}")

    def install(self) -> str:
        self.calls.append("install")
        return "installing"

    def close(self) -> None:
        pass

    def decide(self, body: dict) -> tuple[int, dict]:
        self.decided.append(body)
        answers = {name: {"noul": self.p} for name in body["questions"]}
        return 200, {"model": self.payload_model, "answers": answers, "usage": {"input_tokens": 3, "output_tokens": 0}}


@pytest.fixture
def fake_runtime() -> FakeRuntime:
    return FakeRuntime()


OPEN_WORK_QUESTION = {"type": "noul", "instructions": "Does this message say that some work is still not done?"}


def decide_body(state: str = "phase C is next", ledger: dict | None = None, **extra: Any) -> dict:
    body: dict[str, Any] = {"state": state, "questions": {"q": OPEN_WORK_QUESTION}, **extra}
    if ledger is not None:
        body["ledger"] = ledger
    return body


@pytest.fixture
def decide() -> Any:
    return decide_body
