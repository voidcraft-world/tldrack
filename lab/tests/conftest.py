"""laya-lab's siblings (plugins/laya, plugins/coordinator) on sys.path for the tests too.

The autouse fixture is not optional: no test may resolve the operator's real store, so the two roles the path seam
reads (`laya_bridge.paths`: the data role and the config role, where `paths.json` lives) point at EMPTY temp folders
before any test body runs. No test takes the machine's lease either (`laya_lab/lease.py`: none is configured here),
and the local zone is pinned (`laya_lab/clock.py`), so a night or a week falls where the test says. A test that needs
a tree asks for `lab` (a synthetic `data/laya/` under its own tmp_path) and passes `lab.base` explicitly.
Every text, name and number here is invented.
"""

from __future__ import annotations

import json
import os
from datetime import timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

import pytest

from laya_lab import siblings  # noqa: F401
from laya_lab import clock, lease
from laya_bridge import env_names, label_budget, lab_files, paths, review_log, rows
from laya_bridge.ledger import Ledger
from laya_bridge.wire import clean_request
from tldrack import open_work

SURFACE = "agent_open_work"
SUITE = "agent-open-work"
FINE_TUNE = "tldrack-agent-open-work"
#: An invented checkpoint folder: the served-name prefix is the wire convention, the stamp is made up.
CHECKPOINT = f"{FINE_TUNE}-20261010-1000"
SONNET = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv(paths.DATA_ENV, str(tmp_path_factory.mktemp("data")))
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path_factory.mktemp("config")))
    # Nor the rack's attention store, which the gated retrain counts days in (tldrack/rack_metrics.py).
    monkeypatch.setenv("TLDRACK_METRICS_PATH", str(tmp_path_factory.mktemp("metrics") / "rack-metrics.sqlite"))
    monkeypatch.delenv(lease.MODULE_ENV, raising=False)
    # A scheduler's names, both spellings (laya_bridge/env_names.py): a test never inherits the run it was started by.
    for name in (lease.LEASE_SCHEDULED_ENV, lab_files.EVAL_RUN_ENV):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(env_names.legacy_name(name) or name, raising=False)
    monkeypatch.setattr(clock, "ZONE", timezone.utc)


def store_env(data: Path) -> dict[str, str]:
    """An explicit environment whose laya store is `data/laya`. It carries the test's empty config role too: a mapping
    without one reads `paths.json` from the real home."""
    return {paths.DATA_ENV: str(data), paths.CONFIG_ENV: os.environ[paths.CONFIG_ENV]}


def synthetic(prefix: str, n: int) -> list[str]:
    return [f"{prefix} {i}: a synthetic closing message, long enough to label" for i in range(n)]


def in_half(half: str, prefix: str, n: int) -> list[str]:
    """`n` synthetic texts whose key the label budget draws into `half` (`label_budget.drawn`)."""
    found, i = [], 0
    while len(found) < n:
        text = f"{prefix} {i}: a synthetic closing message, long enough to label"
        if label_budget.drawn(rows.state_key(text)) == half:
            found.append(text)
        i += 1
    return found


class Lab:
    """A synthetic `<data>/laya/` — never operator data."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def path(self, relative: str) -> Path:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_jsonl(self, relative: str, rows_out: list[dict], header: str | None = None) -> Path:
        path = self.path(relative)
        lines = ([header] if header else []) + [json.dumps(row, sort_keys=True) for row in rows_out]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def unlabelled(self, texts: list[str]) -> list[str]:
        self.write_jsonl(
            "data/agent-open-work-unlabelled.jsonl", [{"key": rows.state_key(text), "hand_off": text} for text in texts]
        )
        return [rows.state_key(text) for text in texts]

    def labels(self, labelled: dict[str, bool], by: str = SONNET) -> None:
        self.write_jsonl(
            "data/agent-open-work-labels.jsonl",
            [{"key": key, "open": is_open, "why": "synthetic", "by": by} for key, is_open in labelled.items()],
        )

    def operator(
        self,
        key: str,
        answer: str = "agree",
        against_open: bool = True,
        *,
        by: str = SONNET,
        stratum: str = "sample",
        at: str = "2026-10-27T01:02:03Z",
    ) -> dict:
        """One operator answer row, appended as the plugin appends it (`review_log.answer_row`)."""
        row = review_log.answer_row(
            answer_id=review_log.new_answer_id(),
            at=at,
            surface=SURFACE,
            key=key,
            stratum=stratum,
            answer=answer,
            against={"by": by, "open": against_open},
            laya=None,
            decision_id=None,
            held_out=True,
        )
        with self.path("data/agent-open-work-operator.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def undo(self, answer_id: str) -> None:
        row = review_log.undo_row(answer_id=answer_id, at="2026-10-27T01:03:00Z", surface=SURFACE)
        with self.path("data/agent-open-work-operator.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def checkpoint(self, name: str = CHECKPOINT, *, train_texts: list[str] | None = (), weights: bool = True) -> Path:
        """A checkpoint folder on disk; `train_texts=None` leaves it without a training record."""
        folder = self.base / "checkpoints" / name
        folder.mkdir(parents=True, exist_ok=True)
        if weights:
            (folder / "model.safetensors").write_bytes(b"synthetic")
        if train_texts is not None:
            self.write_jsonl(
                f"checkpoints/{name}.data/train.jsonl",
                [{"state": text, "questions": {}, "expected": {"q": True}} for text in train_texts],
            )
        return folder

    def trained_on(self, texts: list[str]) -> Path:
        """A served fine-tune that trained on `texts`: an operator label on one is spent, so it is in the label budget's
        TRAIN half whatever its hash drew, and PROMOTE offers its flip (`label_budget.Budget.half`)."""
        from laya_lab.registry import register

        folder = self.checkpoint(train_texts=texts)
        register(self.base, SURFACE, folder, {"eval_items": 1})
        return folder


@pytest.fixture
def umask_022():
    """The usual umask, under which a plain `open` would make 0644 files: the writers must not."""
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    base = tmp_path / "data" / "laya"
    base.mkdir(parents=True)
    return Lab(base)


@pytest.fixture
def public_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic public suite in place of the repo's, so a test can assert it is never written."""
    folder = tmp_path / "public-golden"
    folder.mkdir()
    cases = [
        {
            "id": "case-open",
            "expected": True,
            "why": "phase C remains",
            "tags": ["open"],
            "state": {"hand_off": "Phase C is not started."},
        },
        {
            "id": "case-done",
            "expected": False,
            "why": "all done",
            "tags": ["done"],
            "state": {"hand_off": "All three phases shipped."},
        },
    ]
    (folder / f"{SUITE}.jsonl").write_text("# public\n" + "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    monkeypatch.setattr(paths, "PUBLIC_GOLDEN_DIR", folder)
    return folder / f"{SUITE}.jsonl"


def add_provisional_case(suite_path: Path, case_id: str = "case-new") -> None:
    """Append one agent-written open case, tagged `lab_files.PROVISIONAL_TAG`, to a synthetic public suite."""
    case = {
        "id": case_id,
        "expected": True,
        "why": "agent-written",
        "tags": ["open", lab_files.PROVISIONAL_TAG],
        "state": {"hand_off": "The docs pair is still missing; the button itself works."},
    }
    suite_path.write_text(suite_path.read_text(encoding="utf-8") + json.dumps(case) + "\n", encoding="utf-8")


def decide(
    lab: Lab,
    texts: list[str],
    *,
    p: float = 0.8,
    threshold: float = 0.62,
    ms: float = 500,
    book: Ledger | None = None,
    spawn: int | None = None,
    **row: Any,
) -> list[str]:
    """Rack verdicts as the plugin records them (now, unless `book` has its own clock): the served state, its p and
    the cuts the rack drew at."""
    book = book or Ledger(lambda: lab.base)
    ids = []
    for text in texts:
        cleaned = clean_request(
            {"state": open_work.decide_state(text), "questions": {"q": open_work.OPEN_WORK_QUESTION}},
            frozenset({"english", FINE_TUNE}),
        )
        tag = {
            "surface": SURFACE,
            "threshold": threshold,
            "done_threshold": 0.3,
            "question_version": open_work.QUESTION_VERSION,
            **row,
        }
        ids.append(
            book.record_decision(
                tag, cleaned, {"answers": {"q": {"noul": p}}}, FINE_TUNE, None, lab.base / "checkpoints" / CHECKPOINT, ms, spawn
            )
        )
    return ids


class FakeRouter:
    """Stands in for `laya.Router`: one noul per state from `p_of(state)`, and the calls it got."""

    def __init__(self, p_of: Callable[[Any], float]) -> None:
        self.p_of = p_of
        self.calls: list[tuple[Any, str]] = []
        self.loaded_revisions: dict[str, str | None] = {}

    def predict(self, state: Any, questions: dict, model: str | None = None) -> dict:
        self.calls.append((state, model))
        self.loaded_revisions.setdefault(model or "english", "55cf4c4ebb4e" if model == "english" else None)
        return {"answers": {name: {"noul": self.p_of(state)} for name in questions}}
