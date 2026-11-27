"""Where an agent WORKS, when that is not where it stands: the linked worktree its tools act in.

THE GAP THIS CLOSES (2026-11-26). The rack draws a session on a worktree's island by the session's cwd
(the hook's, `snapshot.apply_hook_cwds`). An agent can do all its work in a worktree without ever
standing there: `git worktree add ~/worktrees/tldrack/x -b feat/x` from the main checkout, then every
edit by absolute path. Its cwd never moves, and the rack showed it on main the whole time. Every
main-thread `PreToolUse` already carries the evidence, the Bash `command` and each edit's `file_path`,
so the bridge reads it here and the rack draws the island the agent is really on.

THE EVIDENCE (`evidence`, pure). In tool order, newest wins:
  · `added`: a Bash `git … worktree add … <path>`. Seen BEFORE the command runs (a `PreToolUse`), so
    the tree does not exist yet; it resolves once it does (`resolve`). A command that failed names a
    tree that never comes, and resolves to nothing.
  · `edited`: `Edit` / `Write` / `MultiEdit` / `NotebookEdit`'s file. An edit inside a MAIN checkout
    is evidence too, of the opposite: the agent went back to main, so the held tree is let go. An edit
    outside every checkout (a memory file, `/tmp`) says nothing and moves nothing.
  · `cd`: a Bash `cd <dir>` (or `pushd`) INTO a linked tree. Only into one: agents `cd` to the main
    checkout to read all the time, and a grep there is no move.
  · `removed`: `git … worktree remove <path>` lets go of the tree it names.
  Paths are resolved against the hook's cwd, a `cd` earlier in the same command, and git's `-C`. The
  shell is not run: an `echo git worktree add x`, or a path built by `$(…)`, is read as text (the first
  is no git command; the second is a literal path that never resolves).

WHO MAY MOVE IT. The main thread only. A worktree-isolated subagent reports ITS checkout (main.py
§ Subagent hooks), and a session running three of them is not on three islands.

WHAT WINS ON THE ROW (`row_field`). The hook cwd, when it stands in a linked tree itself (`by: "cwd"`):
the agent is there. Otherwise the held evidence, resolved now. A main checkout, a submodule, a tree
since removed: no `work_tree` at all.

WHAT IT IS NOT: STANDING IN THE TREE. It moves the island (where the agent's edits land, and so its
COMPLETE band), never the PR / CLEAN buttons. Those join by where the session stands
(`rack-view.ts` `worktree_for_session`, `next_step.worktree_for`), because CLEAN kills the pressed row's
session, and an agent working in a tree from main is not the tree's to kill.

KEPT ACROSS A RESTART (`SessionWorkTreeStore`): an idle session sends no hook after a restart, and an
island that vanished on every restart would be the 2026-11-04 bug of verdicts.py again. Forgotten
with the conversation: a kill, a spawn reusing the name, a new conversation (`SessionStart` that is not
a resume or a compact), a user-ended `SessionEnd`. A rename moves it.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from tldrack import paths
from tldrack.checkouts import LinkedTree, checkout_root, linked_tree, linked_tree_holding
from tldrack.session_state_file import SessionStateFile, parse_session_map, serialize_session_map

WORK_TREE_FILE_VERSION = 1
WORK_TREES_FILE = "session-work-trees.json"

ADDED, EDITED, CD, REMOVED = "added", "edited", "cd", "removed"
#: What a row's `by` may say: the cwd itself, or the evidence kind that put it there.
BY_CWD = "cwd"
KINDS = (ADDED, EDITED, CD)

#: The tools whose input names the file they write, and the keys that name it.
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
FILE_KEYS = ("file_path", "notebook_path")

MAX_PATH_CHARS = 4096
#: A command this long is a heredoc or a generated script, not a navigation: read its head only.
MAX_COMMAND_CHARS = 16_384

#: `git worktree add`'s options that take a value (git 2.40–2.47); every other dash word is a flag.
ADD_VALUE_OPTIONS = frozenset({"-b", "-B", "--reason"})
#: git's own global options that take a value (`git -C <dir>` is read; the rest are skipped).
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
#: A shell's command separators, as `shlex` with `punctuation_chars` splits them out.
SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"})
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
#: Words a command may run behind and still be the command (`sudo git …`, `command cd …`).
WRAPPERS = frozenset({"sudo", "command", "builtin", "exec", "time", "nice", "env"})


def _resolve(path: str, base: str | None) -> str | None:
    """An agent's path text as an absolute, normalised path; None when it cannot be one."""
    if not path or len(path) > MAX_PATH_CHARS or "\x00" in path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        if not base:
            return None
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)


def _segments(command: str) -> Iterator[list[str]]:
    """The simple commands of a shell line, as word lists, in order. Quotes are honoured; a line
    `shlex` cannot read (an open quote) is split naively, never dropped."""
    for line in command[:MAX_COMMAND_CHARS].splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            words = list(lexer)
        except ValueError:
            words = re.split(r"\s+", line.strip())
        segment: list[str] = []
        for word in words:
            if word in SEPARATORS:
                if segment:
                    yield segment
                segment = []
            else:
                segment.append(word)
        if segment:
            yield segment


def _command_words(segment: list[str]) -> list[str]:
    """The segment from its command word on: leading `NAME=value` assignments and wrappers dropped."""
    index = 0
    while index < len(segment) and (ENV_ASSIGNMENT.fullmatch(segment[index]) or segment[index] in WRAPPERS):
        index += 1
    return segment[index:]


def _git_worktree(words: list[str], base: str | None) -> list[tuple[str, str]]:
    """`git [-C <dir>]… worktree add|remove|move …` as evidence; [] for any other git command."""
    index, git_base = 1, base
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option in GIT_VALUE_OPTIONS and index + 1 < len(words):
            if option == "-C":
                git_base = _resolve(words[index + 1], git_base)
            index += 2
            continue
        index += 1
    if words[index : index + 1] != ["worktree"] or index + 1 >= len(words):
        return []
    verb, rest = words[index + 1], words[index + 2 :]
    positionals: list[str] = []
    skip, options_done = False, False
    for word in rest:
        if skip:
            skip = False
        elif not options_done and word == "--":
            options_done = True
        elif not options_done and word.startswith("-"):
            name = word.split("=", 1)[0]
            skip = verb == "add" and name in ADD_VALUE_OPTIONS and "=" not in word and not _attached(word)
        else:
            positionals.append(word)
    resolved = [_resolve(word, git_base) for word in positionals]
    if verb == "add" and resolved and resolved[0]:
        return [(ADDED, resolved[0])]
    if verb == "remove" and resolved and resolved[0]:
        return [(REMOVED, resolved[0])]
    if verb == "move" and len(resolved) >= 2 and resolved[0] and resolved[1]:
        return [(REMOVED, resolved[0]), (ADDED, resolved[1])]
    return []


def _attached(word: str) -> bool:
    """`-bfeat/x`: a short option with its value glued on, so the next word is not its value."""
    return len(word) > 2 and word[0] == "-" and word[1] != "-"


def evidence(tool_name: object, tool_input: object, cwd: object) -> list[tuple[str, str]]:
    """What one main-thread tool call says about where its agent works: `(kind, absolute path)` in order
    (module note § THE EVIDENCE). Pure; never raises on an agent's text."""
    if not isinstance(tool_input, Mapping):
        return []
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
    if tool_name in EDIT_TOOLS:
        for key in FILE_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str):
                resolved = _resolve(value, base)
                return [(EDITED, resolved)] if resolved else []
        return []
    if tool_name != "Bash":
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    found: list[tuple[str, str]] = []
    for segment in _segments(command):
        words = _command_words(segment)
        if not words:
            continue
        if words[0] in ("cd", "pushd") and len(words) >= 2 and words[1] != "-":
            target = _resolve(words[1], base)
            if target:
                base = target
                found.append((CD, target))
        elif os.path.basename(words[0]) == "git":
            found.extend(_git_worktree(words, base))
    return found


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def fold(held: dict | None, found: list[tuple[str, str]], now: float) -> dict | None:
    """The held evidence after one tool call's (module note): newest wins, a main-checkout edit lets go,
    a `cd` only moves INTO a linked tree, a `removed` lets go of the tree it names. The path held is the
    TREE's root (an `added` path is one already), so a hundred edits in one tree are one entry. Pure but
    for the filesystem reads that tell a main checkout from a linked tree."""
    for kind, path in found:
        if kind == REMOVED:
            if held is not None and _within(held["path"], path):
                held = None
        elif kind == ADDED:
            held = {"path": path, "kind": ADDED, "at": now}
        elif kind == EDITED:
            root = checkout_root(os.path.dirname(path))
            if root is None:
                continue  # outside every checkout: no word on where the agent works
            tree = linked_tree(root)
            # A main checkout (or a submodule) edited: the agent went back, so the tree is let go.
            held = {"path": tree.path, "kind": EDITED, "at": now} if tree is not None else None
        elif kind == CD:
            tree = linked_tree_holding(path)
            if tree is not None:
                held = {"path": tree.path, "kind": CD, "at": now}
    return held


def _same(after: dict | None, held: dict | None) -> bool:
    if after is None or held is None:
        return after is held
    return (after["path"], after["kind"]) == (held["path"], held["kind"])


def _clean_entry(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    path, kind, at = entry.get("path"), entry.get("kind"), entry.get("at")
    if (
        not isinstance(path, str)
        or not os.path.isabs(path)
        or len(path) > MAX_PATH_CHARS
        or kind not in KINDS
        or isinstance(at, bool)
        or not isinstance(at, (int, float))
    ):
        return None
    return {"path": os.path.normpath(path), "kind": kind, "at": float(at)}


def resolve_work_trees_path() -> Path:
    env_path = os.environ.get("TLDRACK_WORK_TREES_PATH")
    return Path(env_path) if env_path else paths.state_file(WORK_TREES_FILE)


class SessionWorkTreeStore(SessionStateFile[dict]):
    """session → the newest evidence of where it works (module note). The file, the lock, the tolerant
    load, `rename` and `forget`: owned by `session_state_file.py` — see the note there."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            parse=lambda text: parse_session_map(text, _clean_entry),
            serialize=lambda entries: serialize_session_map(entries, WORK_TREE_FILE_VERSION),
            noun="work tree",
            plural="session work trees",
            file_stem="session-work-trees",
        )

    def observe(self, session: str, found: list[tuple[str, str]], now: float | None = None) -> bool:
        """Fold one tool call's evidence into the session's entry; True when it changed (and was kept)."""
        if not found:
            return False
        stamp = time.time() if now is None else now
        with self._lock:
            held = self._entries.get(session)
            after = fold(dict(held) if held is not None else None, found, stamp)
            if _same(after, held):
                return False  # the same tree by the same evidence: no write per tool call
            if after is None:
                self._entries.pop(session, None)
            else:
                self._entries[session] = after
            self._persist_locked()
            return True

    def get(self, session: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(session)
            return dict(entry) if entry is not None else None


#: How long a FOUND tree is believed: long enough that a snapshot burst reads git's files once, short
#: enough that a tree an agent removed leaves within a poll.
RESOLVE_TTL_SECONDS = 3.0


class TreeResolver:
    """`linked_tree_holding` behind a short cache, for the snapshot path (module note § WHAT WINS).

    Only a found tree is cached. A miss is re-read every time, because the miss that matters is the
    tree a `git worktree add` is creating right now: cached, its island would lag the command by the
    TTL. A miss costs a few `stat`s up the path, and only rows with unresolved evidence pay it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cache: dict[str, tuple[float, LinkedTree]] = {}

    def holding(self, path: str) -> LinkedTree | None:
        now = self._clock()
        cached = self._cache.get(path)
        if cached is not None and now - cached[0] < RESOLVE_TTL_SECONDS:
            return cached[1]
        if len(self._cache) > 1024:
            self._cache.clear()
        tree = linked_tree_holding(path)
        if tree is None:
            self._cache.pop(path, None)
        else:
            self._cache[path] = (now, tree)
        return tree


def row_field(cwd: object, held: dict | None, resolver: TreeResolver) -> dict | None:
    """The `/snapshot` row's additive `work_tree`: `{path, branch, main, by}`, or None (module note § WHAT
    WINS ON THE ROW). `path` and `main` are absolute; `branch` is None for a detached head."""
    if isinstance(cwd, str) and os.path.isabs(cwd):
        tree = resolver.holding(os.path.normpath(cwd))
        if tree is not None:
            return {"path": tree.path, "branch": tree.branch, "main": tree.main, "by": BY_CWD}
    if held is None:
        return None
    tree = resolver.holding(held["path"])
    if tree is None:
        return None
    return {"path": tree.path, "branch": tree.branch, "main": tree.main, "by": held["kind"]}
