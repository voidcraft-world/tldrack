"""`laya-lab question-challenger agent_open_work --version 3`: what a question version alone is worth.

    laya-lab question-challenger agent_open_work --version 3 --dry-run   # the rows, and how many carry a request
    laya-lab question-challenger agent_open_work --version 4 --dry-run   # … and how many carry the turn's facts
    laya-lab question-challenger agent_open_work --version 3             # fine-tune it (never registered)
    laya-lab question-challenger agent_open_work --version 2             # the control: the served rows, refitted
    laya-lab question-challenger agent_open_work --version 3 --like <checkpoint>   # another fit's record, not the served one

One fine-tune that differs from the SERVED one in the state it reads and nothing else: the served checkpoint's own
training record (its rows, their labels, its train/held-out split), each state rebuilt by `open_work.decide_state` at
`--version` from the row's text, the request that opened its turn (`open_work_data.read_requests`) and, at version 4,
that turn's runtime facts (`open_work_data.read_facts`), both written by `export-open-work`. The same epochs and seed as `train`. A `--version` equal to the served one is the control: it
separates what the new state buys from what a refit alone does.

The result is measured, never served: it is not registered, so nothing asks it until `laya-lab bench --challenger
<checkpoint>` scores it beside the served model on the bench's rows, each asked at its own version
(`question.json` in its training record says which). Registering one is the verdict's business (lab/README.md
"Question v3").

A row of a record older than its keys (`key` absent) is found by its state: a version-1 state IS its text. A text whose
turn was never found has no request and no facts, and reads as a turn nobody typed one for, as it would be served.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab import train
from laya_lab.data import atomic_write_text, read_jsonl
from laya_lab.registry import resolve_model
from laya_bridge import rows as bridge_rows
from laya_bridge.paths import CHECKPOINTS_DIRNAME
from tldrack import open_work

SURFACE = "agent_open_work"
#: Beside a checkpoint's training record: the question version its states were built at.
QUESTION_FILE = "question.json"
REFUSED = 3


@dataclass(frozen=True)
class Rebuilt:
    """The served record's rows, rebuilt at one version, on their own sides of its split, with counts (never text)."""

    like: str
    version: int
    train_rows: list[dict[str, Any]]
    eval_rows: list[dict[str, Any]]
    stats: dict[str, int]


def row_key(row: dict[str, Any]) -> str:
    """The label key a record row stands for: its own `key`, else its state's (a version-1 state is its text)."""
    key = row.get("key")
    return key if isinstance(key, str) else bridge_rows.state_key(row["state"])


def rebuild(
    row: dict[str, Any],
    texts: dict[str, str],
    requests: dict[str, str | None],
    version: int,
    facts: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool, bool]:
    """`(row at version, whether its state carries a request, whether it carries the turn's facts)`. Pure."""
    key = row_key(row)
    text = texts.get(key, row["state"])
    request = requests.get(key)
    if request is None:
        request = requests.get(bridge_rows.state_key(text))
    found = (facts or {}).get(key) or (facts or {}).get(bridge_rows.state_key(text))
    state = open_work.decide_state(text, request, facts=found, version=version)
    asked = state != open_work.decide_state(text, facts=found, version=version)
    told = state != open_work.decide_state(text, request, version=version)
    row = {"key": key, "state": state, "questions": {"q": open_work.OPEN_WORK_QUESTION}, "expected": row["expected"]}
    return row, asked, told


def rebuild_record(base: Path, version: int, checkpoint: str = "served") -> Rebuilt:
    """The record of `checkpoint` (the served one by default) rebuilt at `version`."""
    from laya_lab.open_work_data import read_facts, read_requests, texts

    name, folder = resolve_model(base, SURFACE, checkpoint)
    if folder is None:
        raise SystemExit(f"[laya-lab] {name} is a built-in model: it has no training record to rebuild")
    record = folder.with_name(folder.name + ".data")
    sides = [read_jsonl(record / "train.jsonl"), read_jsonl(record / "eval.jsonl")]
    if not sides[0] or not sides[1]:
        raise SystemExit(f"[laya-lab] {folder.name} has no training record (train.jsonl and eval.jsonl) to rebuild")
    found, requests, facts = texts(base, SURFACE), read_requests(base), read_facts(base)
    out: list[list[dict[str, Any]]] = [[], []]
    asked, told = [0, 0], [0, 0]
    for side, rows in enumerate(sides):
        for row in rows:
            rebuilt, has_request, has_facts = rebuild(row, found, requests, version, facts)
            out[side].append(rebuilt)
            asked[side] += has_request
            told[side] += has_facts
    stats = {
        "train": len(out[0]),
        "eval": len(out[1]),
        "train_requests": asked[0],
        "eval_requests": asked[1],
        "train_facts": told[0],
        "eval_facts": told[1],
    }
    return Rebuilt(folder.name, version, out[0], out[1], stats)


def write_record(work: Path, version: int, **facts: Any) -> None:
    """Beside a training record: the question version its states were built at, and counts (never text). Two writers:
    `run` (a rebuild of another checkpoint's rows: `like` names it) and `train.prepare` (a retrain on its own rows and
    split: `retrain` is true), which `bench` names apart."""
    atomic_write_text(work / QUESTION_FILE, json.dumps({"question_version": version, **facts}) + "\n")


def read_record(base: Path, checkpoint: str) -> dict[str, Any] | None:
    """A checkpoint's question record, None when it has none or it says no version a state builder knows."""
    try:
        found = json.loads((base / CHECKPOINTS_DIRNAME / f"{checkpoint}.data" / QUESTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = found.get("question_version") if isinstance(found, dict) else None
    return found if type(version) is int and version in open_work.STATE_VERSIONS else None


def read_question(base: Path, checkpoint: str) -> int | None:
    """The question version a checkpoint was built at (`QUESTION_FILE`), None when its record does not say."""
    found = read_record(base, checkpoint)
    return None if found is None else found["question_version"]


def run(base: Path, version: int, *, epochs: int, device: str, dry_run: bool = False, like: str = "served") -> int:
    if version not in open_work.STATE_VERSIONS:
        print(f"[laya-lab] refused: question version {version} has no state builder ({open_work.STATE_VERSIONS})")
        return REFUSED
    built = rebuild_record(base, version, like)
    s = built.stats
    told = lambda side: f", {s[side + '_facts']} with facts" if version >= 4 else ""  # noqa: E731
    print(
        f"[laya-lab] question v{version} like {built.like}: {s['train']} train ({s['train_requests']} with a request"
        f"{told('train')}) / {s['eval']} held-out ({s['eval_requests']} with a request{told('eval')})"
    )
    if dry_run:
        print("  dry run: nothing trained, nothing written")
        return 0
    plan = train.prepare(SURFACE, base, None, epochs=epochs, device=device, split=(built.train_rows, built.eval_rows))
    if plan.reserved:
        trained_held = len(built.train_rows) - plan.train_rows
        print(
            f"[laya-lab] the label budget's reserve held back {trained_held} train and {plan.reserved - trained_held} "
            "held-out row(s)" + ("" if trained_held else ": the train rows are still the served ones")
        )
    write_record(plan.work, version, like=built.like, **s)
    report = train.fit(base, plan)
    after = report.get("after") or {}
    print(
        f"[laya-lab] {plan.out.name}: held-out accuracy {after.get('accuracy')} · loss {after.get('loss')} "
        f"(NOT registered; score it: laya-lab bench --challenger {plan.out.name})"
    )
    return 0
