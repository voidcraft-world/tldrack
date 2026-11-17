"""The shadow permission ledger — every permission prompt an agent raised, and what
became of it. Rung 1 of the automation ladder: it DECIDES NOTHING. It
records, so that a later policy or judge can be calibrated against what the human
actually did instead of against a guess.

── WHERE A ROW COMES FROM ──────────────────────────────────────────────────────
A `PermissionRequest` hook (Claude and Codex both fire it the moment the dialog
opens) opens a REQUEST row. The relay returns nothing, so the dialog itself runs
exactly as it would without us.

── HOW IT IS RESOLVED ──────────────────────────────────────────────────────────
No hook fires when a dialog is ANSWERED (Claude Code #103295), and the bridge
relays no PostToolUse for Claude. The transcript knows, though:

- Claude writes the assistant's `tool_use` before the tool runs, and its
  `tool_result` after. A rejected tool's result is `is_error: true` and begins
  "The user doesn't want to proceed with this tool use" (measured on 2.1.30x;
  with "…the user said:" appended when the rejection carried feedback). So a
  request is BOUND to the newest unclaimed `tool_use` with the same tool and key
  (`tool_key`), and resolved by that `tool_use`'s result: `approved` (it ran —
  even if it then failed) or `denied`.
- Codex relays PostToolUse, so a PostToolUse for the same tool is `approved`.
- A turn that ends (Stop, Interrupt, SessionEnd, a new prompt) with a request
  still unresolved and no transcript evidence is `not_run`: denied, Esc'd or
  abandoned — the honest union, since nothing tells those apart.
- A SUBAGENT's request ends `unknown` instead: its calls live in the subagent's
  own transcript, so no evidence was ever reachable, and counting it `not_run`
  would drag the approval rate down for prompts the human likely approved.

`TranscriptTracker` already tails every transcript incrementally; its
`tool_events` fold (transcript.py) is what this reads, so resolution costs no
extra file I/O. The arbiter's per-tick re-tail of turn-over rows (main.py) is
what resolves a rejection that ended the turn with no further hook.

── WHAT IS STORED ──────────────────────────────────────────────────────────────
Append-only JSONL at ~/.local/state/tldrack/permission-ledger.jsonl (env
TLDRACK_PERMISSIONS_PATH): `request` lines and `outcome` lines, joined by
id on read. The summary is REDACTED and clipped (`summarize`) — a Bash command can
carry a token, and an Edit's `new_string` is a whole file, which is never stored.
This never leaves the Mac: no route sends it anywhere but loopback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

from tldrack import paths

PERMISSIONS_PATH_ENV = "TLDRACK_PERMISSIONS_PATH"
FILE_NAME = "permission-ledger.jsonl"

#: Claude Code's rejection text on the tool_result (see the module docstring).
REJECT_MARKER = "doesn't want to proceed with this tool use"
FEEDBACK_MARKER = "the user said:"

OUTCOMES = ("approved", "denied", "not_run", "unknown")

#: The events that end a turn: a request still open then was never run.
TURN_ENDS = frozenset({"Stop", "StopFailure", "Interrupt", "SessionEnd", "UserPromptSubmit"})

#: A request older than this is no longer matched to anything. tmux names are
#: reused (`vc-3` tomorrow is another agent), and a dialog open for two hours
#: was abandoned whatever the next hook says. It stays `pending` in the stats.
OPEN_MAX_SECONDS = 2 * 3600

SUMMARY_MAX = 160
#: A file this size rotates to `<name>.1` on the next append — a ledger is a
#: calibration sample, not an archive, and a week of prompts is a few hundred KB.
ROTATE_BYTES = 8 * 1024 * 1024

_SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH|CREDENTIAL)[A-Z0-9_]*)(\s*[=:]\s*)"
    r"(?!(?:bearer|basic|token)\s)(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(r"(\w+://)[^/\s:@]+:[^@\s]+@")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9/._-])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9/._-])")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", re.DOTALL)
REDACTED = "‹redacted›"


def redact(text: str) -> str:
    """Strip what looks like a credential. Pure. Errs towards redacting: a hash
    lost from a summary costs nothing, a token kept costs everything."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _LONG_TOKEN.sub(REDACTED, text)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_key(tool_name: object, tool_input: object) -> str:
    """What identifies ONE call of a tool, for joining a request to its tool_use.
    The same function runs on the hook's `tool_input` and on the transcript's,
    so only its consistency matters. A HASH, because it is stored: the raw
    command would put an unredacted token on disk. Pure."""
    name = tool_name if isinstance(tool_name, str) else ""
    data = tool_input if isinstance(tool_input, dict) else {}
    basis = None
    for field in ("command", "file_path", "notebook_path", "url", "pattern", "path"):
        value = data.get(field)
        if isinstance(value, str) and value:
            basis = f"{name}:{field}:{_squash(value)}"
            break
    if basis is None:
        try:
            basis = f"{name}:{json.dumps(data, sort_keys=True, default=str)}"
        except (TypeError, ValueError):
            basis = name
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:24]


def summarize(tool_name: object, tool_input: object) -> str:
    """A short, redacted line saying what was asked. Never a file's contents."""
    name = tool_name if isinstance(tool_name, str) else "?"
    data = tool_input if isinstance(tool_input, dict) else {}
    for field in ("command", "file_path", "notebook_path", "url", "pattern", "path", "description"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return _clip(redact(_squash(value)), SUMMARY_MAX)
    return name


def tool_pattern(tool_name: object, tool_input: object) -> str:
    """The bucket a request is counted in — what a policy rule would match on. Pure.

    Bash → the program and its subcommand (`git push`, `npm install`, `rm`);
    a file tool → the tool and the extension; a fetch → the host; else the tool."""
    name = tool_name if isinstance(tool_name, str) else "?"
    data = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        command = data.get("command") if isinstance(data.get("command"), str) else ""
        words = [w for w in command.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            return "Bash"
        program = os.path.basename(words[0])
        sub = next((w for w in words[1:] if not w.startswith("-")), None)
        if program in ("git", "npm", "npx", "uv", "gh", "docker", "aws", "brew", "pnpm", "yarn", "cargo", "kubectl") and sub:
            return f"Bash {program} {os.path.basename(sub)}"
        return f"Bash {program}"
    path = data.get("file_path") or data.get("notebook_path")
    if isinstance(path, str) and path:
        suffix = Path(path).suffix
        return f"{name} {suffix}" if suffix else name
    url = data.get("url")
    if isinstance(url, str) and url:
        host = urlparse(url).hostname
        return f"{name} {host}" if host else name
    return name


def resolve_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(PERMISSIONS_PATH_ENV)
    return Path(override).expanduser() if override else paths.state_file(FILE_NAME)


class PermissionLedger:
    """The store and its fold. Thread-safe; never raises into a hook — a failed
    write is logged and the request still lands in memory."""

    def __init__(
        self, path: Path | None, *, clock: Callable[[], float] = time.time, log: Callable[[str], None] = lambda message: None
    ) -> None:
        self._path = path
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._order: list[str] = []
        self._load()

    # -- writes ---------------------------------------------------------------
    def request(
        self, session: str, payload: Mapping, *, profile: str | None, kind: str, tool_events: Mapping[str, dict] | None
    ) -> str:
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        agent_id = payload.get("agent_id")
        row = {
            "t": "request",
            "id": uuid.uuid4().hex[:12],
            "at": self._clock(),
            "session": session,
            "profile": profile,
            "kind": kind,
            "tool_name": tool_name if isinstance(tool_name, str) else None,
            "summary": summarize(tool_name, tool_input),
            "pattern": tool_pattern(tool_name, tool_input),
            "key": tool_key(tool_name, tool_input),
            "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            "subagent": isinstance(agent_id, str) and bool(agent_id),
            "permission_mode": payload.get("permission_mode") if isinstance(payload.get("permission_mode"), str) else None,
        }
        with self._lock:
            self._append(row)
            self._remember(dict(row))
            self._bind(self._rows[row["id"]], tool_events or {})
            self._resolve_bound(session, tool_events or {})
        return row["id"]

    def observe(
        self, session: str, event: str, payload: Mapping, tool_events: Mapping[str, dict] | None
    ) -> list[tuple[str, str]]:
        """Any other hook (or the arbiter's re-tail, `event="tick"`): resolve what
        it can. Returns `(id, outcome)` for each row it closed."""
        events = tool_events or {}
        closed: list[tuple[str, str]] = []
        with self._lock:
            open_rows = [r for r in self._open(session)]
            if not open_rows:
                return closed
            for row in open_rows:
                if row.get("tool_use_id") is None:
                    self._bind(row, events)
            closed += self._resolve_bound(session, events)
            if event == "PostToolUse" and not isinstance(payload.get("agent_id"), str):
                # Codex: the tool ran. The oldest open request for that tool is the one it answers.
                name = payload.get("tool_name")
                row = next((r for r in self._open(session) if r["tool_name"] == name and r.get("tool_use_id") is None), None)
                if row is not None:
                    closed.append(self._close(row, "approved", "post_tool_use", self._clock()))
            if event in TURN_ENDS:
                for row in self._open(session):
                    if row.get("tool_use_id") is not None:
                        continue  # bound: its tool_result decides, on a later read
                    closed.append(self._close(row, "unknown" if row.get("subagent") else "not_run", event, self._clock()))
        return closed

    # -- reads ----------------------------------------------------------------
    def rows(self, since: float | None = None, limit: int = 500) -> list[dict]:
        """Request rows joined with their outcome, newest first."""
        with self._lock:
            picked = [dict(self._rows[i]) for i in reversed(self._order) if since is None or self._rows[i]["at"] >= since]
        for row in picked:
            row.pop("t", None)
            row.pop("key", None)
        return picked[:limit]

    def stats(self, since: float | None = None) -> dict:
        """Counts by pattern and overall, plus the human approval rate and wait."""
        rows = self.rows(since, limit=10**9)
        by_pattern: dict[str, dict] = {}
        for row in rows:
            bucket = by_pattern.setdefault(
                row["pattern"],
                {"pattern": row["pattern"], "requests": 0, "approved": 0, "denied": 0, "not_run": 0, "unknown": 0, "pending": 0},
            )
            bucket["requests"] += 1
            bucket[row.get("outcome") or "pending"] += 1
        for bucket in by_pattern.values():
            decided = bucket["approved"] + bucket["denied"]
            bucket["approve_rate"] = round(bucket["approved"] / decided, 3) if decided else None
        totals = {
            key: sum(b[key] for b in by_pattern.values())
            for key in ("requests", "approved", "denied", "not_run", "unknown", "pending")
        }
        decided = totals["approved"] + totals["denied"]
        waits = sorted(row["wait_s"] for row in rows if isinstance(row.get("wait_s"), (int, float)))
        return {
            **totals,
            "approve_rate": round(totals["approved"] / decided, 3) if decided else None,
            "median_wait_s": waits[len(waits) // 2] if waits else None,
            "since": since,
            "patterns": sorted(by_pattern.values(), key=lambda b: (-b["requests"], b["pattern"])),
        }

    # -- internals (lock held) ------------------------------------------------
    def _open(self, session: str) -> list[dict]:
        cutoff = self._clock() - OPEN_MAX_SECONDS
        return [
            self._rows[i]
            for i in self._order
            if self._rows[i]["session"] == session and self._rows[i].get("outcome") is None and self._rows[i]["at"] >= cutoff
        ]

    def _claimed(self) -> set[str]:
        return {row["tool_use_id"] for row in self._rows.values() if row.get("tool_use_id")}

    def _bind(self, row: dict, events: Mapping[str, dict]) -> None:
        """Bind to the newest unclaimed tool_use of the same key, written no
        earlier than a minute before the request."""
        claimed = self._claimed()
        for use_id, event in reversed(list(events.items())):
            if use_id in claimed or event.get("key") != row["key"]:
                continue
            at = event.get("at")
            if isinstance(at, (int, float)) and at < row["at"] - 60:
                continue
            row["tool_use_id"] = use_id
            return

    def _resolve_bound(self, session: str, events: Mapping[str, dict]) -> list[tuple[str, str]]:
        closed = []
        for row in self._open(session):
            event = events.get(row.get("tool_use_id") or "")
            if event and event.get("outcome") in ("approved", "denied"):
                closed.append(
                    self._close(
                        row,
                        event["outcome"],
                        "transcript",
                        event.get("result_at") or self._clock(),
                        feedback=bool(event.get("feedback")),
                    )
                )
        return closed

    def _close(self, row: dict, outcome: str, via: str, at: float, *, feedback: bool = False) -> tuple[str, str]:
        line = {
            "t": "outcome",
            "id": row["id"],
            "at": self._clock(),
            "outcome": outcome,
            "via": via,
            "answered_at": at,
            "tool_use_id": row.get("tool_use_id"),
            "feedback": feedback,
        }
        self._append(line)
        self._apply_outcome(line)
        return row["id"], outcome

    def _apply_outcome(self, line: Mapping) -> None:
        row = self._rows.get(line.get("id"))
        if row is None or row.get("outcome") is not None or line.get("outcome") not in OUTCOMES:
            return
        row["outcome"] = line["outcome"]
        row["via"] = line.get("via")
        row["feedback"] = bool(line.get("feedback"))
        if line.get("tool_use_id"):
            row["tool_use_id"] = line["tool_use_id"]
        answered = line.get("answered_at")
        if isinstance(answered, (int, float)) and answered >= row["at"]:
            row["wait_s"] = round(answered - row["at"], 1)

    def _remember(self, row: dict) -> None:
        if row["id"] not in self._rows:
            self._order.append(row["id"])
        self._rows[row["id"]] = row

    def _append(self, line: Mapping) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists() and self._path.stat().st_size > ROTATE_BYTES:
                self._path.replace(self._path.with_name(self._path.name + ".1"))
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, separators=(",", ":")) + "\n")
        except OSError as err:
            self._log(f"tldrack: permission ledger write failed ({err})")

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or not isinstance(line.get("id"), str):
                continue
            if (
                line.get("t") == "request"
                and isinstance(line.get("at"), (int, float))
                and isinstance(line.get("session"), str)
                and isinstance(line.get("key"), str)
            ):
                self._remember(dict(line))
            elif line.get("t") == "outcome":
                self._apply_outcome(line)
