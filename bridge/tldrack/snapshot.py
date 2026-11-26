"""Whole-machine session snapshot for the rack's session rows and status glow.

`GET /snapshot` (served by main.py) unions three sources so the frontend can show
*every* tmux session at once — not just the ones Claude Code has reported on:

  - live tmux state (`tmux list-panes -a`): which sessions exist, whether one is
    attached, the active pane's working directory, and the active pane's title
    (Claude Code writes progress there);
  - attached clients (`tmux list-clients`): the tty of each terminal window showing
    a session, so a caller can FOCUS an existing window instead of opening a new
    one. Panes and clients are distinct objects — a detached session has panes but
    no clients — which is why this is a second query rather than more `-F` fields;
  - the in-memory StatusStore: per-session Claude state (`busy | attention | free`)
    with age, exactly as `GET /status` returns it.

A session may appear in either or both. tmux-only sessions read as state
`"unknown"` (same semantics as `GET /status` — nobody reported on them);
store-only sessions (reported, but the tmux server is gone / never ran) still
list with `title None, attached False`.

tmux `-F` format fields are joined with `\x1f` (ASCII unit separator), not a tab:
tmux does NOT expand `\t` in a format string, and a pane title is arbitrary text
that can contain spaces/tabs — `\x1f` is the one byte a title won't hold, so the
title (always last) survives a `split(sep, 9)` intact.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from tldrack.open_work import VERDICT_ROW_KEYS
from tldrack.profiles import (
    MODEL_OPTION,
    PERMISSION_MODE_OPTION,
    PROFILE_ID_RE,
    PROFILE_OPTION,
    SUBAGENT_MODEL_OPTION,
    clean_launch_model,
    clean_permission_mode,
)
from tldrack.session_lineage import LINEAGE_OPTION, parse_lineage

# Moved here from main.py so this module has no import back into main (main
# imports snapshot). main.py re-imports the name to keep its own usages working.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

TMUX_FIELD_SEP = "\x1f"
#: A DEAD pane's end, `D<status>` or `S<signal>`; empty while it runs. Only a pane
#: held by `remain-on-exit` is ever dead (a plugin's background job), and
#: without this field the rack drew one as an idle agent nobody dared close.
#: `parse_dead` reads it.
TMUX_DEAD_FIELD = "#{?pane_dead,#{?pane_dead_signal,S#{pane_dead_signal},D#{pane_dead_status}},}"
#: The launch models `profiles.spawn_argv` stamped (`profiles.py` § LAUNCH MODELS):
#: the agent's, then the one forced on its subagents, as ONE field joined by a
#: space, which no model token holds. Two unset options read as a lone space.
TMUX_MODELS_FIELD = "#{" + MODEL_OPTION + "} #{" + SUBAGENT_MODEL_OPTION + "}"
TMUX_PANE_FORMAT = TMUX_FIELD_SEP.join(
    (
        TMUX_MODELS_FIELD,
        # The launch permission mode `profiles.spawn_argv` stamped ("" when none),
        # so a restore brings the session back in it. Leading, like the rest.
        "#{" + PERMISSION_MODE_OPTION + "}",
        TMUX_DEAD_FIELD,
        # When tmux made the session (epoch s) — a just-spawned agent counts as
        # engaged before its first turn (launch_pick.py). Leading, like lineage,
        # so the seven-field core every parser test writes never moves.
        "#{session_created}",
        "#{" + LINEAGE_OPTION + "}",
        "#{session_name}",
        "#{session_attached}",
        "#{window_active}",
        "#{pane_active}",
        # A dead pane has no current path, so it falls back to the directory the
        # session was started in (`new-session -c`) rather than to no cwd at all,
        # which filed it under the rack's `(no path)`.
        "#{?pane_current_path,#{pane_current_path},#{session_path}}",
        # The launch profile `profiles.spawn_argv` stamped on the session. An
        # unset option expands to "", so the field count never moves.
        "#{" + PROFILE_OPTION + "}",
        "#{pane_title}",
    )
)

# `tmux list-clients` (deliberately NO `-t`): with no target it lists every client
# attached to the server, which is what we want. Note there is no `-a` flag here —
# that belongs to `list-panes`; passing it errors with "unknown flag -a".
#
# A *client* is an attached terminal, which is NOT the same as a pane process:
# `#{session_attached}` counts clients, so a session running a busy agent with
# nobody looking at it is `attached=0` with an empty client list. That is the
# normal state for a background agent, not an anomaly.
TMUX_CLIENT_FORMAT = TMUX_FIELD_SEP.join(("#{client_session}", "#{client_tty}"))


def parse_created(raw: str) -> int | None:
    """`#{session_created}` → epoch seconds, or None for anything else."""
    return int(raw) if raw.isdigit() else None


#: A shell's convention for a process a signal ended (SIGTERM → 143).
SIGNAL_EXIT_BASE = 128


def parse_dead(raw: str) -> tuple[bool, int | None]:
    """`TMUX_DEAD_FIELD` → (is the pane dead, its exit code). A signal reads as
    `128 + n`, the shell's convention; a dead pane tmux gave no number reads None."""
    if not raw:
        return False, None
    kind, number = raw[0], raw[1:]
    if kind not in "DS":
        return False, None
    if not number.isdigit():
        return True, None
    return True, int(number) + (SIGNAL_EXIT_BASE if kind == "S" else 0)


def parse_launch_models(raw: str) -> tuple[str | None, str | None]:
    """`TMUX_MODELS_FIELD` → (launch model, subagent model), each cleaned or None."""
    model, _, subagent = raw.partition(" ")
    return clean_launch_model(model), clean_launch_model(subagent)


def parse_tmux_panes(output: str) -> dict[str, dict]:
    """Parse `tmux list-panes -a -F TMUX_PANE_FORMAT` stdout into per-session info.

    One pane per session: the active pane (window_active and pane_active both "1")
    is preferred, otherwise the first pane seen for that session is kept. Titles
    can contain the separator's neighbours, so split with maxsplit=11 (title last —
    any new field must be inserted BEFORE it, and this maxsplit bumped to match).
    The format's five LEADING fields are optional to a reader: twelve fields carry
    the launch models, the permission mode, the dead-pane field, `session_created`
    and lineage; eleven the last four; ten the last three; nine the last two;
    eight lineage alone.

    `cwd` is the active pane's working directory, which is how a session maps to a
    project/worktree — nothing else in the payload identifies one. A dead pane
    adds `exited: True` and `exit_code` (int or None); a running one carries
    neither key.
    """
    result: dict[str, dict] = {}
    active_locked: set[str] = set()  # sessions whose active pane we already captured
    for line in output.splitlines():
        fields = line.split(TMUX_FIELD_SEP, 11)
        launch_model, subagent_model = None, None
        if len(fields) == 12:
            launch_model, subagent_model = parse_launch_models(fields.pop(0))
        launch_mode = None
        if len(fields) == 11:
            launch_mode = clean_permission_mode(fields.pop(0))
        dead, exit_code = False, None
        if len(fields) == 10:
            dead, exit_code = parse_dead(fields.pop(0))
        created_at = None
        if len(fields) == 9:
            created_at = parse_created(fields.pop(0))
        lineage = None
        if len(fields) == 8:
            lineage = parse_lineage(fields.pop(0))
        if len(fields) != 7:
            continue
        (session_name, session_attached, window_active, pane_active, pane_cwd, pane_profile, pane_title) = fields
        if not SESSION_NAME_RE.fullmatch(session_name):
            continue
        is_active = window_active == "1" and pane_active == "1"
        if session_name in active_locked:
            continue  # already have this session's active pane — nothing beats it
        if session_name in result and not is_active:
            continue  # keep the first pane seen until the active one turns up
        try:
            attached = int(session_attached) > 0
        except ValueError:
            attached = False  # non-numeric #{session_attached} — treat as detached
        result[session_name] = {
            "title": pane_title or None,
            "attached": attached,
            "cwd": pane_cwd or None,
            # Re-validated: any process on the machine can `tmux set-option`.
            "profile": pane_profile if PROFILE_ID_RE.fullmatch(pane_profile) else None,
        }
        if lineage is not None:
            result[session_name]["lineage"] = lineage
        if launch_mode is not None:
            result[session_name]["launch_permission_mode"] = launch_mode
        if launch_model is not None:
            result[session_name]["launch_model"] = launch_model
        if subagent_model is not None:
            result[session_name]["subagent_model"] = subagent_model
        if created_at is not None:
            result[session_name]["created_at"] = created_at
        if dead:
            result[session_name]["exited"] = True
            result[session_name]["exit_code"] = exit_code
        if is_active:
            active_locked.add(session_name)
    return result


# ── repo identity ──────────────────────────────────────────────────────────
#
# `cwd` alone cannot answer "are these two agents in the same project". A
# frontend heuristic over path segments was tried and provably fails on a real
# worktree layout: with checkouts at ~/code/project,
# ~/workspaces/*/project and ~/worktrees/project/*, the only shared
# path prefix is ~, so every grouping degrades to ancestor directories
# (`code`, `workspaces`, `worktrees`) — three groups for ONE repo, and a single
# outlier session drags the label for every other session with it.
#
# git answers it exactly, and only the bridge can ask: `rev-parse
# --git-common-dir` resolves to the MAIN repo's .git even from inside a linked
# worktree, which is precisely the "fold worktrees onto their parent repo"
# property the grouping needs. (herdr keys its own workspace grouping on the
# same canonicalized git common dir.)

#: git is a local disk read, but a network filesystem could stall it — and this
#: runs on a request thread the SPA is polling.
GIT_TIMEOUT_S = 2.0


def repo_name_from_common_dir(common_dir: str, cwd: str) -> str | None:
    """`--git-common-dir` output + the cwd it was run from → the repo's name.

    git prints a RELATIVE path (usually bare `.git`) for an ordinary checkout and
    an absolute one from a linked worktree, so the value is resolved against
    `cwd` before use. A trailing `.git` is stripped to land on the repo root; a
    bare repo (`/srv/thing.git`) keeps its own name.

    Pure — no filesystem access — so the parsing is unit-testable without git.
    """
    if not common_dir:
        return None
    resolved = common_dir if os.path.isabs(common_dir) else os.path.join(cwd, common_dir)
    resolved = os.path.normpath(resolved)
    root = os.path.dirname(resolved) if os.path.basename(resolved) == ".git" else resolved
    name = os.path.basename(root.rstrip(os.sep))
    if name.endswith(".git"):  # bare repo: /srv/thing.git
        name = name[: -len(".git")]
    return name or None


class RepoResolver:
    """cwd → repo name, cached forever.

    A session's repo does not change while the session lives (its cwd is the
    cache key, so a `cd` into a different repo simply misses and resolves once).
    Without the cache this would fork git per session per poll.

    Negative results are cached too: most non-repo directories stay non-repos,
    and re-forking git every poll for a shell sitting in ~ is exactly the kind of
    per-poll cost the bridge avoids elsewhere.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}

    def resolve(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        if cwd in self._cache:
            return self._cache[cwd]
        name = self._read(cwd)
        self._cache[cwd] = name
        return name

    def resolve_all(self, sessions: dict[str, dict]) -> dict[str, str]:
        """session → repo name, omitting sessions with no cwd or no repo."""
        out: dict[str, str] = {}
        for session, info in sessions.items():
            name = self.resolve(info.get("cwd"))
            if name:
                out[session] = name
        return out

    def _read(self, cwd: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None  # no git binary, or the call blew up — not a repo, move on
        if completed.returncode != 0:
            return None  # not a git repo (the common case for a plain shell)
        return repo_name_from_common_dir(completed.stdout.strip(), cwd)


#: `GET /capture` bounds. The cap is a payload guard, not a tmux limit — a pane
#: is only ever `#{pane_height}` rows tall anyway (see `capture_lines`).
DEFAULT_CAPTURE_LINES = 20
MAX_CAPTURE_LINES = 200


def capture_lines(raw: str | None) -> int:
    """Requested preview line count, clamped to [1, MAX_CAPTURE_LINES].

    Garbage (or absent) reads as the default rather than erroring: a preview is
    decoration, and failing a tile because a query string was malformed would be
    a worse outcome than showing the default number of rows.
    """
    if raw is None:
        return DEFAULT_CAPTURE_LINES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_LINES
    return max(1, min(MAX_CAPTURE_LINES, value))


def tail_visible(output: str, lines: int) -> list[str]:
    """The last `lines` rows of a pane capture, trailing blanks removed.

    `tmux capture-pane -p` pads to the full pane height, so a mostly-idle pane
    comes back as a handful of real rows followed by many empty ones. Dropping
    the trailing blanks is what lets a tile size itself to the actual content;
    interior blank lines are preserved because they're part of the frame.
    """
    rows = output.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    return rows[-lines:] if lines < len(rows) else rows


def parse_tmux_clients(output: str) -> dict[str, list[str]]:
    """Parse `tmux list-clients -F TMUX_CLIENT_FORMAT` stdout into session → ttys.

    The tty is the join key that lets a desktop app focus the *existing* terminal
    window showing a session instead of opening a new one: a `tmux attach` client
    inherits the emulator pane's pty as its controlling terminal, so this value is
    the same device file the emulator reports for that window (iTerm2's `tty of
    session`, Terminal.app's `tty of tab`). Titles change constantly; a tty does not.

    A session can have several clients (the same session attached in two windows),
    so values are lists, order-preserving and de-duplicated.
    """
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        fields = line.split(TMUX_FIELD_SEP, 1)
        if len(fields) != 2:
            continue
        session_name, client_tty = fields
        if not SESSION_NAME_RE.fullmatch(session_name) or not client_tty:
            continue
        ttys = result.setdefault(session_name, [])
        if client_tty not in ttys:
            ttys.append(client_tty)
    return result


#: Where every pane lives NOW. A pane id (`%3`) is fixed for the pane's life and
#: survives `tmux rename-session`; a session name does not — the join
#: `agent_index.py` needs (§ WHAT MAKES AN ENTRY BELIEVABLE, rule 3).
TMUX_PANE_SESSION_FORMAT = TMUX_FIELD_SEP.join(("#{pane_id}", "#{session_name}"))
PANE_ID_RE = re.compile(r"%[0-9]+")

#: A local tmux socket read; this is a wedged-server backstop. The caller is the
#: state arbiter's own thread, so a hang would stall every correction.
PANE_SESSIONS_TIMEOUT_SECONDS = 2.0


def parse_pane_sessions(output: str) -> dict[str, str]:
    """`tmux list-panes -a -F TMUX_PANE_SESSION_FORMAT` stdout → pane id →
    session name. Pure. A line with a malformed id or name is skipped."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(TMUX_FIELD_SEP, 1)
        if len(fields) != 2:
            continue
        pane_id, session_name = fields
        if PANE_ID_RE.fullmatch(pane_id) and SESSION_NAME_RE.fullmatch(session_name):
            result[pane_id] = session_name
    return result


def list_tmux_pane_sessions() -> dict[str, str]:
    """Every live pane's current session, or `{}` when tmux cannot answer."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", TMUX_PANE_SESSION_FORMAT],
            capture_output=True,
            text=True,
            check=False,
            timeout=PANE_SESSIONS_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}  # no tmux server running
    return parse_pane_sessions(result.stdout)


class PaneSessionCache:
    """pane id → its tmux session, for the `http` hook relay (main.hook_http_entry),
    which can name only its pane. One `tmux list-panes` serves every hook within
    TTL_SECONDS; a pane the map does not hold refreshes it, at most once per
    MISS_SECONDS so a stream of hooks from a foreign tmux server is not a fork
    storm. `/rename-session` calls `invalidate()`: a rename keeps the pane id,
    so a stale map would file the next hook under the OLD name — a ghost row."""

    TTL_SECONDS = 1.0
    MISS_SECONDS = 0.5

    def __init__(self, read: Callable[[], dict[str, str]] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._read = read or list_tmux_pane_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}
        self._at = float("-inf")

    def session_of(self, pane: str) -> str | None:
        if not PANE_ID_RE.fullmatch(pane or ""):
            return None
        with self._lock:
            now = self._clock()
            fresh = now - self._at < self.TTL_SECONDS
            if fresh and pane in self._map:
                return self._map[pane]
            if fresh and now - self._at < self.MISS_SECONDS:
                return None
            self._map, self._at = self._read(), now
            return self._map.get(pane)

    def invalidate(self) -> None:
        with self._lock:
            self._at = float("-inf")


def list_tmux_sessions() -> dict[str, dict]:
    """Enumerate live tmux sessions, or `{}` if there's no server / no tmux."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", TMUX_PANE_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {}  # tmux not installed (status-only mode can reach here)
    if result.returncode != 0:
        return {}  # no tmux server running
    return parse_tmux_panes(result.stdout)


def list_tmux_clients() -> dict[str, list[str]]:
    """Enumerate attached tmux clients as session → ttys, or `{}` if none/no server.

    Separate from `list_tmux_sessions` because panes and clients are different
    objects: `list-panes` cannot report a client tty, and a detached session has
    panes but no clients. An empty result is the common case, not an error.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-clients", "-F", TMUX_CLIENT_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {}  # tmux not installed (status-only mode can reach here)
    if result.returncode != 0:
        return {}  # no tmux server running
    return parse_tmux_clients(result.stdout)


def apply_hook_cwds(tmux_sessions: dict[str, dict], store_entries: list[dict]) -> dict[str, dict]:
    """Overlay each live pane's `cwd` with the cwd Claude's hooks reported. Pure.

    tmux's `pane_current_path` is where the pane's SHELL is. When Claude enters
    a worktree (`EnterWorktree`, `/cd`) its own cwd moves and the shell's does
    not — so every consumer that keys on cwd (the rack's attach-only set, the
    harbor's islands, `RepoResolver`) placed the agent in the main checkout it
    had just left. Hook bodies carry a `cwd` that does follow Claude; StatusStore
    keeps the latest one per session, and this puts it where those consumers
    already look. Applied BEFORE `merge_snapshot` and before the repo resolver
    so `cwd` and `repo` agree.

    Only LIVE sessions are touched: a store row with no tmux pane stays a dead
    row with `cwd: None` — its cwd is a memory, not a location. A store row with
    no cwd (legacy `/status`, or after SessionEnd cleared it) leaves the pane's
    own cwd in place. The input dicts are not mutated.
    """
    hook_cwds = {entry["session"]: entry["cwd"] for entry in store_entries if isinstance(entry.get("cwd"), str) and entry["cwd"]}
    if not hook_cwds:
        return tmux_sessions
    return {
        session: ({**pane, "cwd": hook_cwds[session]} if session in hook_cwds else pane)
        for session, pane in tmux_sessions.items()
    }


def merge_snapshot(
    tmux_sessions: dict[str, dict],
    store_entries: list[dict],
    icons: dict[str, str] | None = None,
    clients: dict[str, list[str]] | None = None,
    repos: dict[str, str] | None = None,
    models: dict[str, dict] | None = None,
) -> list[dict]:
    """Union-merge tmux sessions with StatusStore.list() rows, sorted by session name.

    Every row carries all twenty-one keys. Store fields (state/age_seconds/message/
    activity/tasks/model/usage/state_source, and Laya's verdict, `VERDICT_ROW_KEYS`)
    come from StatusStore; title/attached/cwd come from tmux,
    client_ttys from `list_tmux_clients`. A session in only one source gets defaults
    for the other: tmux-only → state `"unknown"`, age None, message None, activity
    None, tasks None, model None, usage None; store-only → title None, attached False, cwd None.

    `client_ttys` lists the ttys of terminals currently attached to the session, so
    a caller can focus an existing window rather than opening another one. It is
    empty for a detached session — including a session actively running an agent
    that nobody is watching, which is the normal background case.

    `model`/`usage` are the running Claude model and cumulative token usage read
    from the session's transcript on POST /hook (None until a Claude session has
    reported in that tmux session).

    `live` distinguishes the two: True iff the tmux session currently exists.
    Store-only rows (session was killed, or reported before tmux listing worked)
    are `live: False` — clients that only care about attachable sessions (a
    native client's list) filter on it, while the store row itself is kept so
    history-minded consumers still see the last reported state.

    `models` maps session → {model, usage} remembered across restarts
    (SessionModelStore.get_all()). It is consulted ONLY where the store row has
    no model, because an in-memory reading from this run beats the file. It is
    what keeps an idle Claude session labelled after a bridge restart: that
    session's Stop hook already fired, so it emits nothing further and the live
    store never learns its model.

    `icons` maps session → explicitly picked icon id (SessionIconStore.get_all()).
    Each row gets `icon`: the pick, or None meaning "no pick — render your
    hash-computed default". Icon-only sessions (a pick outlives its session) get
    NO row — the pick stays dormant in the store until the name reappears.

    `profile` is the launch profile running in the session (profiles.py), or
    None. The store's hint — the profile whose config dir holds the session's
    transcript, recorded on POST /hook — beats the tmux stamp: the stamp
    records what was SPAWNED, and once that agent exits the pane's trailing
    shell can run anything. A dead row has none; it is not running anything.
    """
    by_session: dict[str, dict] = {}
    for entry in store_entries:
        session = entry["session"]
        by_session[session] = {
            "session": session,
            "state": entry["state"],
            "age_seconds": entry["age_seconds"],
            "message": entry["message"],
            # .get(): pre-activity/pre-model callers (tests, older stores) omit these.
            "activity": entry.get("activity"),
            # {done, total, current} | None — task_progress in tasks.py.
            "tasks": entry.get("tasks"),
            "model": entry.get("model"),
            "usage": entry.get("usage"),
            # "hook" | "index" — who wrote `state` (main.py `_session_row`).
            "state_source": entry.get("state_source"),
            # Laya's turn-end verdict, the rack's ⚠️ / ✅: owned by `open_work.VERDICT_ROW_KEYS`.
            **{key: entry.get(key) for key in VERDICT_ROW_KEYS},
            "title": None,
            "attached": False,
            "cwd": None,
            "live": False,
        }
    for session, pane in tmux_sessions.items():
        row = by_session.get(session)
        if row is None:
            by_session[session] = {
                "session": session,
                "state": "unknown",
                "age_seconds": None,
                "message": None,
                "activity": None,
                "tasks": None,
                "model": None,
                "usage": None,
                "state_source": None,
                **dict.fromkeys(VERDICT_ROW_KEYS),
                "title": pane["title"],
                "attached": pane["attached"],
                # .get(): pre-cwd callers (tests, older parses) omit it.
                "cwd": pane.get("cwd"),
                "live": True,
            }
        else:
            row["title"] = pane["title"]
            row["attached"] = pane["attached"]
            row["cwd"] = pane.get("cwd")
            row["live"] = True
    hints = {entry["session"]: entry.get("profile") for entry in store_entries}
    for session, row in by_session.items():
        # Last-known model/usage from disk (models.py), as a FALLBACK only. The
        # in-memory StatusStore always wins when it has a reading — a hook that
        # fired this run is more current than the file.
        #
        # This is what keeps an IDLE Claude session labelled across a bridge
        # restart. Its Stop hook fired before the restart, so it emits nothing
        # afterwards and the live store never learns its model; without this the
        # row read "claude disconnected" until the user typed into it.
        if row["model"] is None:
            remembered = (models or {}).get(session)
            if remembered:
                row["model"] = remembered.get("model")
                # Only alongside a remembered model — usage with no model has
                # nothing to price it against.
                if row["usage"] is None:
                    row["usage"] = remembered.get("usage")
        row["icon"] = (icons or {}).get(session)
        pane = tmux_sessions.get(session) if row["live"] else None
        if pane is not None and pane.get("lineage"):
            row["lineage"] = pane["lineage"]
        # The mode it was LAUNCHED in, never the one it is in now (that rides each hook).
        if pane is not None and pane.get("launch_permission_mode"):
            row["launch_permission_mode"] = pane["launch_permission_mode"]
        # The same for the launch models: what the spawn asked for, not what runs now.
        for field in ("launch_model", "subagent_model"):
            if pane is not None and pane.get(field):
                row[field] = pane[field]
        if pane is not None and pane.get("created_at") is not None:
            row["created_at"] = pane["created_at"]
        # A live session whose pane has ENDED: a plugin job held on screen by
        # `remain-on-exit`, so its log and exit status stay readable. Nothing runs
        # in it; the rack reads it as finished and safe to close.
        if pane is not None and pane.get("exited"):
            row["exited"] = True
            row["exit_code"] = pane.get("exit_code")
        # .get(): pre-profile callers (tests, older parses) omit it.
        row["profile"] = (hints.get(session) or pane.get("profile")) if pane is not None else None
        # The repo this session's cwd belongs to (worktrees folded onto their
        # parent), or None when it is not in a git repo. This is the grouping key
        # a client should prefer over `cwd` — see RepoResolver.
        row["repo"] = (repos or {}).get(session)
        # A dead session cannot have a live client; never carry a stale tty into a
        # row a caller might then try to focus.
        row["client_ttys"] = list((clients or {}).get(session, ())) if row["live"] else []
    return [by_session[name] for name in sorted(by_session)]
