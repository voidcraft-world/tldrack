"""Topic-name labels: did the rack's auto name fit its session? The namer's eval set.

`session_topics.py` names a vc-N session from its first prompt, and nothing measured whether the
name was any good. This log keeps three kinds of row:

- `named`: one per landed topic call, with the request the model saw (`session_topics.py`
  `topic_request`) and the label it answered. That pair is what an eval needs.
- `label`: the operator's verdict on one naming, `fits` or `wrong` with an optional better name.
  It comes from the REVIEW page's NAME card (`via: card`, the rack's per-session LABEL button opens
  it), or from a ✎ rename of an auto-named row (`via: rename`): a rename to the same name keeps
  it, any other name is the better one. The system can tell, so it does not ask.
  The newest label on a naming is the live one; `undo` is a
  newer label, never an edit.
- `renamed`: every ✎ rename, so a session's former names stay findable. Laya's turn-end
  verdicts are recorded under the name the session had then (`open_work.py` `ledger_tag`), so the
  REVIEW page asks for all of them.

WHERE. `topic-labels.jsonl` in the `rack` store (paths.py): the rows hold the operator's prompts, so
they are kept data, never state. `TLDRACK_TOPIC_LABELS_PATH` points tests at a scratch file.
Append-only JSONL, 0600 in a 0700 directory, one process lock.

TIME. `since` (a tmux `session_created`, epoch seconds) bounds every read, because tmux hands a
`vc-N` name to the next session once the old one is gone, and that one's naming is not this one's.

Never raises into a hook: a failed append is reported to `log` and dropped.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tldrack import paths

PATH_ENV = "TLDRACK_TOPIC_LABELS_PATH"
FILE_NAME = "topic-labels.jsonl"

VERDICTS = ("fits", "wrong")
VIAS = ("card", "rename")
#: The former names walked back from a session: more than this is a loop or a rename war.
MAX_ALIASES = 8
#: What a read returns of the request the model saw: enough to judge a name by.
REQUEST_PREVIEW_CHARS = 600
#: A row past this is not one this module wrote; skip it rather than parse it.
MAX_LINE_BYTES = 32 * 1024


def resolve_topic_labels_path() -> Path:
    env = os.environ.get(PATH_ENV)
    return Path(env) if env else paths.store_dir("rack") / FILE_NAME


def _text(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def parse_rows(text: str) -> list[dict[str, Any]]:
    """Every well-formed row, in file order; a torn or foreign line is skipped. Pure."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip() or len(line) > MAX_LINE_BYTES:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("at"), (int, float)):
            continue
        op = row.get("op")
        if (
            op == "named"
            and _text(row.get("session"), 128)
            and _text(row.get("name"), 128)
            or (
                op == "label"
                and _text(row.get("session"), 128)
                and _text(row.get("name"), 128)
                and row.get("verdict") in VERDICTS
                and row.get("via") in VIAS
            )
            or op == "renamed"
            and _text(row.get("old"), 128)
            and _text(row.get("new"), 128)
        ):
            rows.append(row)
    return rows


def aliases_of(rows: list[dict[str, Any]], session: str, since: float | None) -> list[str]:
    """The names `session` had before, newest first, from the renames at or after `since`. Pure."""
    found: list[str] = []
    current = session
    for row in reversed(rows):
        if len(found) >= MAX_ALIASES:
            break
        if row["op"] != "renamed" or (since is not None and row["at"] < since):
            continue
        if row["new"] == current and row["old"] != session and row["old"] not in found:
            found.append(row["old"])
            current = row["old"]
    return found


def naming_of(rows: list[dict[str, Any]], names: list[str], since: float | None) -> dict[str, Any] | None:
    """The newest `named` row under any of `names` at or after `since`. Pure."""
    for row in reversed(rows):
        if row["op"] == "named" and row["session"] in names and (since is None or row["at"] >= since):
            return row
    return None


def label_of(rows: list[dict[str, Any]], naming: dict[str, Any]) -> dict[str, Any] | None:
    """The live verdict on one naming: its newest `label` row. Pure."""
    for row in reversed(rows):
        if (
            row["op"] == "label"
            and row["session"] == naming["session"]
            and row["name"] == naming["name"]
            and row["at"] >= naming["at"]
        ):
            return row
    return None


def view(rows: list[dict[str, Any]], session: str, since: float | None) -> dict[str, Any]:
    """What the NAME card draws for `session`: its former names, its naming, and the live verdict. Pure."""
    aliases = aliases_of(rows, session, since)
    naming = naming_of(rows, [session, *aliases], since)
    label = label_of(rows, naming) if naming is not None else None
    request = naming.get("request") if naming is not None else None
    return {
        "session": session,
        "aliases": aliases,
        "named": None
        if naming is None
        else {
            "session": naming["session"],
            "name": naming["name"],
            "at": naming["at"],
            "model": naming.get("model"),
            "request": request[:REQUEST_PREVIEW_CHARS] if isinstance(request, str) else None,
            "request_chars": len(request) if isinstance(request, str) else 0,
        },
        "label": None
        if label is None
        else {
            "verdict": label["verdict"],
            "better": label.get("better"),
            "via": label["via"],
            "at": label["at"],
        },
    }


class TopicLabels:
    """The log, appended to under one lock and read whole (it grows a few rows per session)."""

    def __init__(self, path: Path, *, log: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()

    def _append(self, row: dict[str, Any]) -> bool:
        row = {"v": 1, "at": round(self._clock(), 3), **row}
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
            return True
        except OSError as exc:
            if self._log is not None:
                self._log(f"  topic labels: {type(exc).__name__}: {exc}"[:200])
            return False

    def rows(self) -> list[dict[str, Any]]:
        try:
            with self._lock:
                text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return parse_rows(text)

    def named(self, session: str, name: str, request: str, *, model: str | None = None, profile: str | None = None) -> bool:
        return self._append(
            {"op": "named", "session": session, "name": name, "request": request, "model": model, "profile": profile}
        )

    def label(self, session: str, name: str, verdict: str, *, via: str, better: str | None = None) -> bool:
        if verdict not in VERDICTS or via not in VIAS:
            raise ValueError("unknown verdict or via")
        row: dict[str, Any] = {"op": "label", "session": session, "name": name, "verdict": verdict, "via": via}
        if better:
            row["better"] = better
        return self._append(row)

    def renamed(self, old: str, new: str) -> bool:
        return self._append({"op": "renamed", "old": old, "new": new})

    def view(self, session: str, since: float | None = None) -> dict[str, Any]:
        return view(self.rows(), session, since)

    def on_rename(self, old: str, new: str, auto_name: str | None) -> None:
        """A ✎ rename: always a `renamed` row; and when `old` wore a landed auto name, a verdict on it."""
        self.renamed(old, new)
        if not auto_name:
            return
        naming = naming_of(self.rows(), [old], None)
        if naming is None or naming["name"] != auto_name:
            return
        if new.casefold() == auto_name.casefold():
            self.label(old, auto_name, "fits", via="rename")
        else:
            self.label(old, auto_name, "wrong", via="rename", better=new)
