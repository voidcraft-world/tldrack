"""Typing one line into an agent's pane, only when that is safe — the bridge's own typist.

Who uses it: the LEAD's wake (lead/watcher.py), `lead tell` from a lead to its worker, and the brief a
Codex worker gets (the worktree plugin types first prompts for Claude only). Nothing else should
grow a fourth copy of this.

── WHEN IT IS SAFE ───────────────────────────────────────────────────────────
The canvas reply's rule (plugins/agent-canvas/agent_canvas/delivery.py), widened to Codex:
the row is `free` (never busy, background, at a question, or unknown), the agent's input is on
screen, and nothing is drafted in it. Typing into a draft would send the operator's half-written
line; typing into a shell would run the text. Claude's box is the ruled `❯` frame; Codex's empty
composer shows its placeholder (`COMPOSER`), which disappears the moment anyone types, so the
placeholder being there IS the no-draft check. A trust prompt on screen always holds.

ONE exception to `free`: a BRIEF (`FIRST_BRIEF_KIND`, the first line into a worker the bridge itself
just spawned) may also go on `unknown`. A fresh Codex reports no hook before its first turn, and
its first turn IS the brief, so waiting for `free` was a deadlock (a lead run's second step, 2026-11-05: an
idle `› Ask Codex to do anything` that never got its prompt). On `unknown` the pane has to prove
what the hook would have: no turn running (`WORKING_MARKER`, which Claude's spinner and Codex's
status line both draw) on top of every check above. A wake or a tell still needs `free`: those go
to an agent that has already worked, whose `unknown` means the bridge does not know, not "new".

── QUEUED, AND IT SURVIVES A RESTART ─────────────────────────────────────────
A line that cannot go now waits in rack.sqlite's `typed_queue`, unlike the canvas's in-memory one,
because a lead's message to its worker must not vanish with a bridge restart. A queued line is pinned
to the lineage id of the session it was meant for: a new agent wearing the name gets nothing, and
the line is dropped (`name reused`). A line queued before its agent exists (a brief: the worktree
plugin mints the worker's lineage in its create job) waits UNPINNED until its owner proves who the
worker is and calls `pin` (lead/service.py `_pin_lines`); an unpinned line is never typed. Lines
older than `MAX_AGE_SECONDS` are dropped too. Lines go in order per session: a held line holds the
ones behind it, so a tell never overtakes the brief it builds on. Why a line is held is kept in
memory (`pending()`'s `hold`), so `lead status` can name a stuck one.

── SUBMITTED, OR IT SAYS SO ──────────────────────────────────────────────────
A line counts as delivered only once the pane shows it left the input (`submit`; the confirm is
ported from create.py `send_prompt`). A line left sitting in the input is never typed again (that
would double it): later flushes press Enter while it is still there, and until then it waits as
`UNSUBMITTED`. Nothing ever presses Enter on a screen that does not show our line (`still_drafted`):
there an Enter would answer an approval prompt.

── MARKED, SO NOBODY MISTAKES IT FOR THE OPERATOR ─────────────────────────────
Every line this module types starts with a rack marker (`RACK_TAGS`). The ledger's prompt origin
(ledger/claude_fold.py `PEER_TAGS`) reads it as an agent's message, so attention.py keeps the
open-work loop open, and ledger/relay.py files it as machine-typed, not as a human decision.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Mapping

from tldrack.rack_db import RackDb

RACK_TAGS = ("<rack-wake", "<lead-message")
LINE_MAX = 2000
MAX_AGE_SECONDS = 24 * 3600
COMPOSER = "Ask Codex to do anything"
TRUST_MARKERS = ("Do you trust", "trust the files", "Trust this")
#: What a running turn draws, lowercased: Claude's spinner `(… esc to interrupt)` and Codex's status
#: line `• Working (12s • esc to interrupt)`. Checked only where no hook vouches for the agent.
WORKING_MARKER = "esc to interrupt"
#: The queued kind that may go on `unknown` (§ WHEN IT IS SAFE).
FIRST_BRIEF_KIND = "brief"
UNKNOWN_STATES = (None, "", "unknown")
#: Row states a hook only reports once a turn has run.
TURN_STATES = ("busy", "background", "attention")
#: Submit confirm (`submit`): Enters per line, the pause around each, and how much of the line's
#: start identifies it in the input.
SUBMIT_TRIES = 3
SUBMIT_SETTLE_SECONDS = 0.5
SUBMIT_HEAD_CHARS = 24
UNSUBMITTED = "typed, but not submitted: press Enter in the pane"
KEYS_REFUSED = "tmux refused the keys"
TMUX_TIMEOUT = 5
_RULE_MIN = 10


def one_line(text: str) -> str:
    """`send-keys -l` sends a newline as Enter, so a typed message is ALWAYS one line, bounded."""
    flat = " ".join(text.split())
    return flat if len(flat) <= LINE_MAX else flat[: LINE_MAX - 1] + "…"


# jscpd:ignore-start
# A declared cross-package port: owned by plugins/README.md — see the note there.
def claude_ready(pane_text: str) -> bool:
    """Has Claude drawn its input box? Copied from plugins/worktree/worktree/create.py
    `looks_ready` — see the note there (plugins cannot import the core, nor the core a plugin)."""
    if not pane_text.strip():
        return False
    if any(char in pane_text for char in "│╭╰┌└") and ">" in pane_text:
        return True
    lines = pane_text.splitlines()
    for above, line in zip(lines, lines[1:]):
        rule = above.strip()
        if len(rule) >= _RULE_MIN and set(rule) == {"─"} and line.lstrip().startswith(("❯", ">")):
            return True
    return False


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= _RULE_MIN and set(stripped) == {"─"}


def claude_draft(pane_text: str) -> str:
    """The text in Claude's input box (the last caret line under a rule, down to the next rule).
    Copied from plugins/agent-canvas/agent_canvas/delivery.py `input_box_draft` — the same port."""
    lines = pane_text.splitlines()
    start = None
    for index in range(1, len(lines)):
        if _is_rule(lines[index - 1]) and lines[index].lstrip().startswith(("❯", ">")):
            start = index
    if start is None:
        return ""
    body = [lines[start].lstrip()[1:]]
    for line in lines[start + 1 :]:
        if _is_rule(line):
            break
        body.append(line)
    return " ".join(" ".join(body).replace(" ", " ").split())


# jscpd:ignore-end


def decide_typing(cli: str | None, state: object, pane_text: object, *, first_brief: bool = False) -> str | None:
    """None to type now, else why it waits. Pure, so every hold is tested. `first_brief` admits
    `unknown` for a worker's first line (§ WHEN IT IS SAFE)."""
    unknown = state in UNKNOWN_STATES
    if state != "free" and not (first_brief and unknown):
        return f"the agent is {state or 'unknown'}"
    if not isinstance(pane_text, str) or not pane_text.strip():
        return "the pane could not be read"
    if any(marker in pane_text for marker in TRUST_MARKERS):
        return "a trust prompt is on screen"
    if unknown and WORKING_MARKER in pane_text.lower():
        return "a turn is running in the pane"
    if cli == "claude":
        if not claude_ready(pane_text):
            return "no Claude input box in the pane"
        if claude_draft(pane_text):
            return "a draft is in the input box"
        return None
    if cli == "codex":
        return None if COMPOSER in pane_text else "no empty Codex composer in the pane"
    return "not an agent pane"


def capture_pane(session: str) -> str | None:
    try:
        done = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"={session}:"],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _tmux(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=TMUX_TIMEOUT, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _codex_input_head(pane_text: str) -> str:
    """The first line of Codex's composer: the text after the LAST `›` line (history sits above it)."""
    heads = [line.lstrip()[1:].strip() for line in pane_text.splitlines() if line.lstrip().startswith("›")]
    return heads[-1] if heads else ""


def submitted(cli: str | None, pane_text: object) -> bool:
    """Has the typed line left the input? A turn running proves it; else the input is back to empty:
    Codex's placeholder (`COMPOSER`) on screen, or Claude's box drawn with no draft in it."""
    if not isinstance(pane_text, str) or not pane_text.strip():
        return False
    if WORKING_MARKER in pane_text.lower():
        return True
    if cli == "codex":
        return COMPOSER in pane_text
    return claude_ready(pane_text) and not claude_draft(pane_text)


def still_drafted(cli: str | None, text: str, pane_text: object) -> bool:
    """Is OUR line still sitting in the input, unsent? Only then is one more Enter safe: on any other
    screen (an approval prompt, a menu) an Enter would answer it."""
    if not isinstance(pane_text, str) or any(marker in pane_text for marker in TRUST_MARKERS):
        return False
    head = one_line(text)[:SUBMIT_HEAD_CHARS]
    if cli == "codex":
        line = _codex_input_head(pane_text)
        return line.startswith("[Pasted Content") or (bool(head) and line.startswith(head))
    draft = claude_draft(pane_text)
    return draft.startswith("[Pasted text") or (bool(draft) and draft.startswith(head))


# Settle, Enter, re-capture, Enter again while the line is still in the box, bounded, then an honest
# False: owned by plugins/worktree/worktree/create.py `send_prompt` — see the note there.
def submit(
    session: str,
    cli: str | None,
    text: str,
    *,
    capture: Callable[[str], str | None] = capture_pane,
    enter: Callable[[str], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Press Enter until the pane shows the line left the input. True only on that proof."""
    press = enter or (lambda name: _tmux(["tmux", "send-keys", "-t", f"={name}:", "Enter"]))
    for _ in range(SUBMIT_TRIES):
        sleep(SUBMIT_SETTLE_SECONDS)
        if not press(session):
            return False
        sleep(SUBMIT_SETTLE_SECONDS)
        pane = capture(session)
        if submitted(cli, pane):
            return True
        if not still_drafted(cli, text, pane):
            return False
    return False


def type_line(
    session: str,
    cli: str,
    text: str,
    *,
    run: Callable[[list[str]], bool] = _tmux,
    capture: Callable[[str], str | None] = capture_pane,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Claude: `send-keys -l` (the canvas and worktree path). Codex: a bracketed paste (the
    Codex launch path), which its composer takes as one paste, not keystrokes. Then `submit`.
    None once the line has provably left the input; `KEYS_REFUSED` when nothing went in (safe to type
    again); `UNSUBMITTED` once the keys went in, whatever happened after (never type it again)."""
    target = f"={session}:"
    if cli == "codex":
        steps = [
            ["tmux", "set-buffer", "-b", "vc-typist", "--", text],
            ["tmux", "paste-buffer", "-p", "-d", "-b", "vc-typist", "-t", target],
        ]
    else:
        steps = [["tmux", "send-keys", "-t", target, "-l", "--", text]]
    if not all(run(argv) for argv in steps):
        return KEYS_REFUSED
    entered = submit(
        session,
        cli,
        text,
        capture=capture,
        sleep=sleep,
        enter=lambda name: run(["tmux", "send-keys", "-t", f"={name}:", "Enter"]),
    )
    return None if entered else UNSUBMITTED


class PaneTypist:
    def __init__(
        self,
        db: RackDb,
        *,
        capture: Callable[[str], str | None] = capture_pane,
        send: Callable[[str, str, str], str | None] = type_line,
        clock: Callable[[], float] = time.time,
        resubmit: Callable[[str, str | None, str], bool] | None = None,
    ) -> None:
        self.db = db
        self._capture = capture
        self._send = send
        self._clock = clock
        self._resubmit = resubmit or (lambda session, cli, text: submit(session, cli, text, capture=self._capture))
        self._holds: dict[int, str] = {}  # queued id → why the last flush held it (in memory)
        self._unsubmitted: set[int] = set()  # typed into the input, never seen to leave it: never retyped
        self._holds_lock = threading.Lock()

    def try_now(self, session: str, cli: str | None, state: object, text: str, *, first_brief: bool = False) -> str | None:
        """Type it now if safe. None on success, else why it waited (nothing is queued); `UNSUBMITTED`
        means the keys went in, so the caller must never type the line again."""
        hold = decide_typing(cli, state, self._capture(session), first_brief=first_brief)
        if hold is not None:
            return hold
        return self._send(session, cli or "claude", one_line(text))

    def _retry_submit(self, session: str, cli: str | None, state: object, text: str) -> str | None:
        """A line already typed and left in the input: never type it again. Done once a turn has run
        since (nobody can send past our line without sending it) or the input is clear (a late Enter,
        or the operator's); one more bounded submit while it still sits there."""
        if state in TURN_STATES:
            return None
        pane = self._capture(session)
        if submitted(cli, pane):
            return None
        if still_drafted(cli, text, pane) and self._resubmit(session, cli, text):
            return None
        return UNSUBMITTED

    def queue(self, session: str, lineage: str | None, kind: str, text: str) -> int:
        """`lineage` None queues the line unpinned: it waits for `pin` (§ QUEUED)."""
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO typed_queue (session, lineage, kind, text, queued_at) VALUES (?,?,?,?,?)",
                (session, lineage, kind, one_line(text), self._clock()),
            )
            return int(cur.lastrowid)

    def pin(self, session: str, lineage: str) -> int:
        """Pin every waiting unpinned line for `session` to the agent its owner has proven is the one."""
        with self.db.write() as conn:
            cur = conn.execute(
                "UPDATE typed_queue SET lineage = ? WHERE session = ? AND lineage IS NULL "
                "AND delivered_at IS NULL AND dropped IS NULL",
                (lineage, session),
            )
            return int(cur.rowcount)

    def drop_pending(self, session: str, reason: str, *, kind: str | None = None, unpinned_only: bool = False) -> int:
        """Drop the waiting lines for `session` (of one `kind`, or only the unpinned ones)."""
        sql = "UPDATE typed_queue SET dropped = ? WHERE session = ? AND delivered_at IS NULL AND dropped IS NULL"
        args: list = [reason, session]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if unpinned_only:
            sql += " AND lineage IS NULL"
        with self.db.write() as conn:
            return int(conn.execute(sql, args).rowcount)

    def pending(self, session: str | None = None) -> list[dict]:
        """Waiting lines, oldest first; each carries `hold`, why the last flush held it (None: not tried
        since this bridge started)."""
        sql = "SELECT * FROM typed_queue WHERE delivered_at IS NULL AND dropped IS NULL"
        args: tuple = ()
        if session is not None:
            sql += " AND session = ?"
            args = (session,)
        with self.db.read() as conn:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
        with self._holds_lock:
            for row in rows:
                row["hold"] = self._holds.get(row["id"])
        return rows

    def _settle(self, item_id: int, column: str, value: object) -> None:
        with self.db.write() as conn:
            conn.execute(f"UPDATE typed_queue SET {column} = ? WHERE id = ?", (value, item_id))
        with self._holds_lock:
            self._holds.pop(item_id, None)
            self._unsubmitted.discard(item_id)

    def _held(self, item_id: int, reason: str) -> None:
        with self._holds_lock:
            self._holds[item_id] = reason

    def flush(self, rows: Mapping[str, Mapping], cli_of: Callable[[Mapping], str | None]) -> list[dict]:
        """Deliver what is safe now: at most ONE line per session per call, oldest first, and never past
        a held one. `rows` maps a session name to its live snapshot row. Returns the lines typed."""
        now = self._clock()
        typed: list[dict] = []
        done_sessions: set[str] = set()  # typed to, or held, this pass
        for item in self.pending():
            session = item["session"]
            if session in done_sessions:
                continue
            row = rows.get(session)
            drop = None
            if now - item["queued_at"] > MAX_AGE_SECONDS:
                drop = "expired"
            elif row is not None and item["lineage"] and ((row.get("lineage") or {}).get("id") != item["lineage"]):
                drop = "name reused"
            if drop is not None:
                self._settle(item["id"], "dropped", drop)
                continue
            done_sessions.add(session)
            if not item["lineage"]:
                self._held(item["id"], "the worker is not proven yet")
                continue
            if row is None or not row.get("live"):
                self._held(item["id"], "the session is not live")
                continue
            with self._holds_lock:
                typed_before = item["id"] in self._unsubmitted
            if typed_before:
                hold = self._retry_submit(session, cli_of(row), row.get("state"), item["text"])
            else:
                hold = self.try_now(
                    session, cli_of(row), row.get("state"), item["text"], first_brief=item["kind"] == FIRST_BRIEF_KIND
                )
            if hold is not None:
                if hold == UNSUBMITTED:
                    with self._holds_lock:
                        self._unsubmitted.add(item["id"])
                self._held(item["id"], hold)
                continue
            self._settle(item["id"], "delivered_at", now)
            typed.append(item)
        return typed
