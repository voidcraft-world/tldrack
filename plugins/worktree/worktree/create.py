"""CREATE a worktree-backed agent session: `git worktree add`, bootstrap, then
the agent in a DETACHED tmux session so the rack materializes a live pane for it
like any other session. WHICH agent is a launch profile — the bridge's
`profiles.spawn_argv` (via `PluginContext.profiles`) owns that argv; the
`build_spawn_command` below is the bare-`claude` fallback for a host without it.

Shape and traps inherited wholesale from `coordinator/resume.py`, which is the
unit-tested precedent for this exact tmux call. The pure halves — the argv, the
refusal table — carry all the judgement, so they are testable without spawning.

── WHY THE PROMPT IS NOT AN ARGV ARGUMENT ──────────────────────────────────
`resume.py` records that `shlex.quote` defeats the SHELL but not the CLI, and
answers it with an `=`-attached flag value. That answer does not transfer here,
because a first-turn prompt is a POSITIONAL, and `claude` resolves its first
operand against its SUBCOMMANDS before it is ever a prompt. Measured, not
assumed:

    $ claude -- doctor
    Claude Code doctor
    Running: native (2.1.251)     ← ran the SUBCOMMAND, through the `--`

So `--` does not fence this channel, and ordinary task prompts collide with it:
"install the eslint plugin", "import the CSV", "rm the dead file", "project
structure notes" all begin with a real subcommand name. Quoting cannot help —
the string is a well-formed operand either way.

`tmux send-keys -l` sidesteps the parser entirely by TYPING the prompt into the
input box Claude has already drawn. No shell, no option parser, no subcommand
resolution, and multi-line-safe input is a formatting question rather than a
security one. The cost is a readiness race, and its failure mode is chosen to be
harmless: if the pane never looks ready we DO NOT SEND, and say so
(`PROMPT_NOT_TYPED`). A task pasted into a bare shell is the outcome worth
paying to avoid.

── SUBMITTED, OR IT SAYS SO ────────────────────────────────────────────────
The input frame is drawn before Claude reads its input. Measured (2.1.332): the
frame was up 0.9 s after the spawn, a prompt typed at once arrived as ONE chunk
that the TUI filed as a paste (`[Pasted text #1]…`), and the Enter sent right
behind it was swallowed. The prompt then sat in the box until a person pressed
Enter, while this module reported it sent. A session that has been up for a
while submits the same line with the same two calls, so the race is the start,
not the length.

So the Enter is its own step (`submit_prompt`), and it has three rules:

- **Wait until the pane SHOWS the line.** A painted draft proves the TUI took
  the burst and is reading keys, which no fixed pause can.
- **Enter only on our line.** Never on a screen that does not show the prompt
  in the input box (`prompt_drafted`): on a trust list, a permission dialog or
  a menu, an Enter picks the highlighted option.
- **Sent means it left the box.** After each Enter the pane is read again:
  a running turn or an empty box is the proof (`prompt_submitted`). While the
  line still sits there the Enter is repeated, bounded (`SUBMIT_TRIES`), and
  the line is NEVER typed twice. Past the bound the answer is the honest one,
  `PROMPT_UNSUBMITTED`.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

from worktree import hook_trust
from worktree.listing import list_worktrees, worktree_by_branch
from worktree.names import display_path

#: `tmux new-session -d` returns as soon as the server has forked the pane, so
#: this is a wedged-tmux backstop, not a budget for the command inside it.
SPAWN_TIMEOUT_SECONDS = 15
#: Budget for read-only probes (`has-session`, `capture-pane`). Deliberately
#: shorter than the spawn's: these run on a bridge REQUEST THREAD, and a wedged
#: tmux server (a stale socket is the classic one) hangs them forever.
PROBE_TIMEOUT_SECONDS = 4
#: `git worktree add` copies no files (it hard-links the object store) but it
#: does write a full checkout, and a cold NFS-ish disk is slow.
GIT_TIMEOUT_SECONDS = 120
#: A repository's bootstrap hook: linking dependencies, maybe one build step.
BOOTSTRAP_TIMEOUT_SECONDS = 300

#: How long to wait for Claude's input box before giving up on the prompt. The
#: session is already created and usable by then — only the typing is skipped.
READY_TIMEOUT_SECONDS = 30
READY_POLL_SECONDS = 0.4

#: The submit confirm (module header § SUBMITTED): how often the pane is read
#: after the prompt is typed, how many reads before giving up, how many Enters
#: at most, and how much of the prompt identifies it in the input box.
SUBMIT_POLL_SECONDS = 0.5
SUBMIT_LOOKS = 30
SUBMIT_TRIES = 3
SUBMIT_MATCH_CHARS = 24
#: What a running turn draws in the pane, lowercased: the spinner line's hint.
WORKING_MARKER = "esc to interrupt"

#: `send_prompt`'s two failures. Not typed: nothing went in, so pasting the
#: prompt by hand is right. Unsubmitted: it IS in the input box, so pasting
#: would double it and the only thing left to do is press Enter.
PROMPT_NOT_TYPED = "not typed"
PROMPT_UNSUBMITTED = "typed, but not submitted: press Enter in the pane"

#: The bridge caps a plugin POST body at 4 KiB; this is the narrower cap on the
#: part that gets typed, so a paste cannot become a wall of keystrokes.
PROMPT_MAX = 2000

#: What a repository may leave out of its worktrees is ITS call
#: (`.tldrack/worktree.json` `sparse_exclude`, read by `repo.py`): large assets no
#: test, typecheck or editor needs. Excluded with NON-CONE patterns on purpose.
#: Cone mode can only INCLUDE directories, so excluding one deep path means
#: enumerating its siblings at every level, and a directory added later would
#: then be silently missing from every new worktree. `/*` plus one negation per
#: path keeps new directories included by default.

#: A new worktree branches from the LOCAL base, not `origin/<base>`: a dirty
#: main checkout's uncommitted files never travel (a worktree starts at the base's
#: commit), and no network is needed to open one. Which base: `repo.resolve_base_branch`.


def normalize_prompt(prompt: object) -> str:
    """The prompt as it will actually be typed: one line, bounded.

    `send-keys -l` sends a newline as the ENTER KEY, which Claude's input box
    reads as "submit". A three-line prompt would therefore submit its first line
    and leave the other two typing into a busy agent. Collapsing to one line is
    predictable; splitting into typed-then-submitted chunks is not.
    """
    if not isinstance(prompt, str):
        return ""
    # No control characters either: typed as keys, an ESC would be a keypress in the input box, not text.
    spaced = " ".join(prompt.split())
    return "".join(ch for ch in spaced if unicodedata.category(ch) != "Cc")[:PROMPT_MAX].strip()


def build_worktree_add_command(repo_root: Path, path: Path, branch: str, base: str, *, existing: bool = False) -> list[str]:
    """`git worktree add -b <branch> <path> <base>` — pure.

    Every value rides as its own argv element and none of it goes through a
    shell, so nothing here needs quoting. `-C <repo_root>` rather than a cwd
    change: this runs on a request thread shared with every other route, and
    `os.chdir` is process-global.

    `existing` checks out a branch that is already there: no `-b`, and no
    `base` — the branch's own tip is the starting point, whatever it is.

    `--no-checkout` because `add_worktree` configures sparse-checkout before
    populating the tree: checking out here would write the excluded files and
    then delete them, paying the cost this exists to avoid.
    """
    if existing:
        return ["git", "-C", str(repo_root), "worktree", "add", "--no-checkout", str(path), branch]
    return ["git", "-C", str(repo_root), "worktree", "add", "--no-checkout", "-b", branch, str(path), base]


def build_sparse_checkout_command(path: Path, excludes: tuple[str, ...]) -> list[str]:
    """`git -C <path> sparse-checkout set --no-cone /* !/<dir>/ ...` — pure.

    Writes `.git/worktrees/<name>/info/sparse-checkout`, which is PER-WORKTREE
    (unlike `info/exclude`, which linked worktrees share with the main checkout
    — see the note in `scripts/bootstrap.sh`). The main checkout is untouched.
    """
    patterns = ["/*"] + [f"!/{e.strip('/')}/" for e in excludes]
    return ["git", "-C", str(path), "sparse-checkout", "set", "--no-cone", *patterns]


def build_checkout_command(path: Path) -> list[str]:
    """`git -C <path> checkout` — pure. Populates a `--no-checkout` worktree.

    Required: `sparse-checkout set` writes the patterns but does not itself
    materialise a tree that was never checked out, so without this the worktree
    is empty and every tracked path reads as a staged deletion.
    """
    return ["git", "-C", str(path), "checkout"]


def build_worktree_remove_command(repo_root: Path, path: Path, *, force: bool = False) -> list[str]:
    """`git worktree remove [--force] <path>` — pure.

    `--force` is a separate, explicit request: without it git refuses to delete
    a worktree holding uncommitted work, and that refusal is the one thing the
    callers (`POST /remove`, `wt remove`, the `WorktreeRemove` hook) lean on.
    """
    command = ["git", "-C", str(repo_root), "worktree", "remove", str(path)]
    if force:
        command.append("--force")
    return command


#: The pane's own `cd`, a port of the host's: owned by tldrack `profiles.py`
#: (`PANE_CD_SCRIPT`) — see the note there.
PANE_CD_SCRIPT = 'cd -- "$1" 2>/dev/null || cd; shift; [[ -n "$1" ]] && exec "$@"; exec "${SHELL:-/bin/zsh}" -l'


def build_spawn_command(claude_bin: str, session: str, cwd: Path, env: dict[str, str] | None = None) -> list[str]:
    """The exact tmux argv (pure — unit-tested against quoting regressions).

    Deliberately carries NO prompt: see the module header. What is interpolated
    into `inner` is the claude binary path and the inline env, both shlex-quoted.

    - `/bin/zsh -l -i`: login+interactive so the user's PATH resolves whatever
      `claude` shells out to, and so an exotic-PATH failure surfaces INSIDE the
      visible session rather than in a 500 nobody reads.
    - `cwd` needs no quoting: it rides as its own argv element (`-c`), never
      through the shell. That asymmetry is why this builder is pinned by a test.
    - The trailing `exec /bin/zsh -l` keeps the pane alive after `claude` exits
      — without it the rack row vanishes the moment the user quits, which is the
      one thing that makes a spawned session feel broken.
    """
    prefix = "".join(f"{name}={shlex.quote(value)} " for name, value in sorted((env or {}).items()))
    inner = f'{prefix}{shlex.quote(str(claude_bin))}; echo; echo "[claude exited $?]"; exec /bin/zsh -l'
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(cwd),
        *("/bin/zsh", "-f", "-c", PANE_CD_SCRIPT, "zsh", str(cwd)),
        "/bin/zsh",
        "-l",
        "-i",
        "-c",
        inner,
    ]


def build_send_keys_commands(session: str, prompt: str) -> list[list[str]]:
    """The two tmux calls that deliver a prompt: type it, and press Enter.

    Two calls, not one: `-l` means LITERAL, so an `Enter` in the same call would
    be typed as the five characters `E n t e r`. The submit has to be a separate,
    non-literal key, and it is never sent straight behind the text: `submit_prompt`
    owns when.

    `--` before the prompt is the guard that matters here, and unlike `claude`'s
    it genuinely works: `send-keys` has no subcommands, so ending its option list
    is sufficient to stop a prompt beginning with `-` from being read as a flag.
    `=<session>:` is an exact-match PANE target (a bare `-t` prefix-matches). The
    trailing colon is required: tmux 3.7 reads a bare `=<session>` as a pane spec
    and answers `can't find pane` — which made every typed prompt silently fail.
    Session-only commands (`has-session`, `kill-session`) take `=<session>` as is.
    """
    return [
        ["tmux", "send-keys", "-t", pane_target(session), "-l", "--", prompt],
        ["tmux", "send-keys", "-t", pane_target(session), "Enter"],
    ]


def pane_target(session: str) -> str:
    """The exact-match target for a pane command (`send-keys`, `capture-pane`)."""
    return f"={session}:"


_RULE_MIN = 10
"""A horizontal rule this long or longer above the caret is Claude's input frame."""


def looks_ready(pane_text: object) -> bool:
    """Has Claude drawn its input box yet? (pure)

    Matched on the FRAME, not on any wording: the welcome banner is versioned
    marketing copy, while a framed prompt line is what "ready for input"
    structurally looks like. Two frames are accepted — the old box (`╭─╮ │ > │`)
    and the current one, a `❯` caret line directly under a `────` rule. The box
    alone stopped matching when the TUI dropped its corners, and every prompt
    then timed out unsent.

    A false negative costs the prompt (safe, reported); a false positive types
    into a shell (not safe), so this errs strict. A shell prompt that uses `❯`
    (pure, starship) has no rule line above it.
    """
    if not isinstance(pane_text, str) or not pane_text.strip():
        return False
    if any(char in pane_text for char in "│╭╰┌└") and ">" in pane_text:
        return True
    lines = pane_text.splitlines()
    for above, line in zip(lines, lines[1:]):
        rule = above.strip()
        if len(rule) >= _RULE_MIN and set(rule) == {"─"} and line.lstrip().startswith(("❯", ">")):
            return True
    return False


#: The input box's top rule once a long draft has scrolled: `─── ↑ 2 more ───…`.
_SCROLLED_RULE = re.compile(r"─+ ↑ \d+ more ─+")
#: A burst the TUI filed as a paste, as the box shows it: `[Pasted text #1 +3 lines]`.
_PASTE_PLACEHOLDER = re.compile(r"\[Pasted text[^\]]*\]")


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= _RULE_MIN and set(stripped) == {"─"}


def input_box(pane_text: object) -> tuple[str, bool] | None:
    """Claude's input box as the pane shows it: `(text, scrolled)`, or None when
    none is drawn (pure). The LAST frame in the pane wins.

    Two frames. The usual one is a caret line directly under a rule, running
    down to the next rule: `text` is the whole draft, `""` when it is empty. A
    draft taller than the box SCROLLS it: the top rule reads `─── ↑ 2 more ───`,
    the caret line is off screen, and `text` is only the draft's END. That is
    not a corner case: a session nobody has attached to is 80x24, the box shows
    six rows there, and a 520-character prompt already scrolls (measured). A
    reader that knows only the caret line calls every long prompt "not ours".
    """
    if not isinstance(pane_text, str):
        return None
    lines = pane_text.splitlines()
    start, scrolled = None, False
    for index, line in enumerate(lines):
        if _SCROLLED_RULE.fullmatch(line.strip()):
            start, scrolled = index + 1, True
        elif index and _is_rule(lines[index - 1]) and line.lstrip().startswith(("❯", ">")):
            start, scrolled = index, False
    if start is None:
        return None
    body, closed = [], False
    for line in lines[start:]:
        if _is_rule(line):
            closed = True
            break
        body.append(line)
    if scrolled and not closed:
        return None  # a rule-shaped line of output, not a frame
    if body and not scrolled:
        body[0] = body[0].lstrip()[1:]  # the caret
    return " ".join(" ".join(body).replace(" ", " ").split()), scrolled


def _squash(text: str) -> str:
    """Without whitespace: the box wraps a draft wherever the pane is wide enough to."""
    return "".join(text.split())


def prompt_drafted(prompt: str, pane_text: object) -> bool:
    """Is OUR prompt sitting in the input box, unsent? (pure) The one screen an
    Enter is safe on (module header § SUBMITTED).

    What the box shows is a slice of the prompt (all of it, or its end once the
    box has scrolled), with a placeholder wherever the TUI filed part of the
    burst as a paste. So every piece of text around the placeholders has to
    come from the prompt, and without a placeholder there has to be enough of
    it (`SUBMIT_MATCH_CHARS`) that a stray keystroke in the box is not "ours".
    """
    box = input_box(pane_text)
    ours = _squash(prompt)
    if box is None or not box[0] or not ours:
        return False
    pieces = [piece for piece in map(_squash, _PASTE_PLACEHOLDER.split(box[0])) if piece]
    if any(piece not in ours for piece in pieces):
        return False
    if _PASTE_PLACEHOLDER.search(box[0]):
        return True
    return sum(map(len, pieces)) >= min(SUBMIT_MATCH_CHARS, len(ours))


def prompt_submitted(pane_text: object) -> bool:
    """Has the prompt left the input box? (pure) A turn running proves it; else
    the box is drawn and empty again. Only meaningful AFTER an Enter: an empty
    box before one is a prompt the TUI has not painted yet."""
    if not isinstance(pane_text, str):
        return False
    return WORKING_MARKER in pane_text.lower() or input_box(pane_text) == ("", False)


def decide_create(
    branch_error: str | None,
    *,
    git_ok: bool,
    tmux_ok: bool,
    claude_found: bool,
    branch_exists: bool,
    path_exists: bool,
    existing: bool = False,
    checked_out_at: str | None = None,
    respawn: bool = False,
    cli: str = "claude",
) -> tuple[int, dict] | None:
    """The refusal table for POST /create (pure). None = clear to build.

    Every refusal is decided BEFORE a directory is made or a process is forked,
    which is what makes the machine-acting half of this route testable at all.
    Ordered cheapest-and-most-likely first so the message a user sees is the one
    they can act on.

    `existing` flips the meaning of `branch_exists`: the branch is the thing
    being asked for, so its absence is the refusal (404) and its presence is
    not. A branch can be checked out in ONE worktree, so `checked_out_at` (the
    path holding it) refuses before git would — with the path, because the
    right move is to attach to the session already there, not to build a twin.
    Without `existing` the table is exactly what it was.

    `respawn` (the caller found that holding tree managed and its session not
    live) lifts the checked-out refusal and the on-disk one: the tree IS the
    target, so nothing is built and only the session starts.

    `claude_found` answers for the launch profile's CLI, whichever it is;
    `cli` names it in the refusal (`codex`, `claude`).
    """
    respawn = respawn and existing and bool(checked_out_at)
    if branch_error is not None:
        return 400, {"error": branch_error}
    if existing:
        if not branch_exists:
            return 404, {"error": "no local branch by that name"}
        if checked_out_at and not respawn:
            return 409, {"error": f"already checked out at {display_path(checked_out_at)} — attach to that instead"}
    elif branch_exists:
        return 409, {"error": "that branch already exists — pick another name, or open its existing session"}
    if path_exists and not respawn:
        # Reachable without `branch_exists` when two branch names slug alike, or
        # when a previous worktree was removed with `git worktree remove --force`
        # while its directory survived.
        return 409, {"error": "a worktree directory with that name is already on disk"}
    if not git_ok:
        return 503, {"error": "git not found"}
    if not tmux_ok:
        return 503, {"error": "tmux not found — install with: brew install tmux"}
    if not claude_found:
        return 503, {"error": f"the `{cli}` CLI was not found on PATH"}
    return None


# jscpd:ignore-start
# A declared cross-package port: owned by plugins/README.md — see the note there.
def is_duplicate_session_error(stderr: object) -> bool:
    """tmux's own refusal to create a name that already exists.

    The last word on idempotency, and the only one that holds across processes:
    `session_exists` + `new-session` is a check-then-act, so a second CREATE that
    overlaps the first can pass a guard the first had not yet invalidated. tmux
    settles that race atomically — but as a FAILURE, and reporting "duplicate
    session" as a 500 would tell the user their agent did not come up when it did.
    """
    return isinstance(stderr, str) and "duplicate session" in stderr.lower()


# jscpd:ignore-end


# ── bounded process helpers ────────────────────────────────────────────────


def run_bounded(
    command: list[str],
    timeout: int,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    input: str | None = None,
) -> tuple[int, str, str]:
    """Bounded `subprocess.run` → `(returncode, stdout, stderr)`.

    A timeout or an OSError answers with a non-zero code and a readable stderr
    rather than raising: every caller is a request handler whose job is to turn
    this into a status code, and an exception there is a 500 with a traceback in
    it. `-1` is not a real exit status, which is the point — it cannot be
    confused with a command that ran and failed.

    `env` replaces the child's environment outright (build it over `os.environ`;
    `branches.fetch_env` does), `cwd` is for the one tool with no `-C` — `gh`
    resolves the repository from the working directory — and `input` is text
    for the child's stdin (`brief.py` feeds `claude -p` its prompt that way, so
    the prompt is never an argv operand). All three are keyword-only so the
    test fence that impersonates this function keeps matching.

    Every subprocess a plugin module runs goes through here BY MODULE ATTRIBUTE
    (`create_mod.run_bounded`), never a `from … import`: the plugin test suite
    fences `worktree.create.run_bounded`, and an imported name would slip past it.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            input=input,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"`{command[0]}` timed out after {timeout}s"
    except OSError as exc:
        return -1, "", f"could not run {command[0]} ({type(exc).__name__}: {exc})"[:200]
    return result.returncode, result.stdout, result.stderr


def tool_found(name: str) -> bool:
    return shutil.which(name) is not None


def session_exists(name: str) -> bool:
    """'=' prefix = exact match (a bare -t prefix-matches other sessions).

    A probe that times out answers "not there" rather than raising: the spawn is
    the next thing to run, it is bounded too, and its stderr is a far better
    error than anything this function could invent.
    """
    code, _out, _err = run_bounded(["tmux", "has-session", "-t", f"={name}"], PROBE_TIMEOUT_SECONDS)
    return code == 0


def session_home(name: str) -> str | None:
    """The directory tmux session `name` was started in, or None (no such
    session, or tmux did not answer). `=` is an exact match."""
    code, out, _err = run_bounded(["tmux", "display-message", "-p", "-t", f"={name}:", "#{session_path}"], PROBE_TIMEOUT_SECONDS)
    home = out.strip()
    return home if code == 0 and home else None


def current_branch(repo_root: Path) -> str | None:
    """The branch the checkout at `repo_root` is on, or None (detached, or not a repository)."""
    code, out, _err = run_bounded(["git", "-C", str(repo_root), "symbolic-ref", "--short", "-q", "HEAD"], PROBE_TIMEOUT_SECONDS)
    branch = out.strip()
    return branch if code == 0 and branch else None


def branch_exists(repo_root: Path, branch: str) -> bool:
    """Local branches only. A remote-only branch of the same name is not a
    conflict for `worktree add -b` until it is fetched, and refusing on one
    would block the common "re-open a branch I already pushed" case."""
    code, _out, _err = run_bounded(
        ["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], PROBE_TIMEOUT_SECONDS
    )
    return code == 0


#: The longest base ref `/create` accepts — a full ref name or a SHA fits in far less.
BASE_REF_MAX = 200


def resolve_base_ref(repo_root: Path, ref: str) -> tuple[str | None, str | None]:
    """`(sha, None)` for a ref `/create` may cut a new branch from, else `(None, why)`.

    The base is caller-chosen (`launch.py --base`), so it is checked as data before
    git sees it: no leading `-` (an argv flag), no whitespace, bounded — then git
    resolves it with `--end-of-options`, to a COMMIT. The SHA, not the name, is what
    the worker uses: it is what was validated, a ref moved between now and the
    worker's `worktree add` cannot change it, and a remote-tracking name such as
    `origin/main` would otherwise make the new branch track it.
    """
    if not ref or len(ref) > BASE_REF_MAX or ref.startswith("-") or any(c.isspace() for c in ref):
        return None, f"not a usable base ref: {ref[:60]!r}"
    code, out, _err = run_bounded(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"],
        PROBE_TIMEOUT_SECONDS,
    )
    sha = out.strip()
    if code != 0 or len(sha) < 40:
        return None, f"unknown base ref '{ref}' — no commit by that name in this repo"
    return sha, None


def add_worktree(
    repo_root: Path, path: Path, branch: str, base: str, *, existing: bool = False, sparse_exclude: tuple[str, ...] = ()
) -> tuple[int, dict] | None:
    """Create the worktree. None on success, else a `(status, body)` refusal.

    Up to three git calls: `add --no-checkout`, then the per-worktree sparse
    patterns when the repository excludes anything, then the checkout that
    populates the tree. Splitting it is the only way to skip excluded paths
    without writing those files first.

    Sparse is BEST-EFFORT and the checkout is not. If `sparse-checkout set`
    fails the checkout still runs, so the worst case is a full, correct
    worktree — never an empty one. A failed checkout IS fatal: the directory
    exists by then, and handing back an empty worktree is worse than refusing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    code, _out, err = run_bounded(
        build_worktree_add_command(repo_root, path, branch, base, existing=existing), GIT_TIMEOUT_SECONDS
    )
    if code != 0:
        return 500, {"error": (err or "git worktree add failed").strip()[:300]}

    if sparse_exclude:
        run_bounded(build_sparse_checkout_command(path, sparse_exclude), PROBE_TIMEOUT_SECONDS)

    code, _out, err = run_bounded(build_checkout_command(path), GIT_TIMEOUT_SECONDS)
    if code != 0:
        return 500, {"error": (err or "git checkout failed").strip()[:300]}
    return None


def kill_sessions(sessions: list[str]) -> None:
    """`tmux kill-session` each of `sessions`, in order, once each. Best effort:
    `=` is tmux's exact match, so an absent session is a harmless non-zero and
    never a prefix-match on another one."""
    for session in dict.fromkeys(sessions):  # ordered and de-duplicated
        run_bounded(["tmux", "kill-session", "-t", f"={session}"], PROBE_TIMEOUT_SECONDS)


def remove_worktree(repo_root: Path, path: Path, sessions: list[str], *, force: bool = False) -> str | None:
    """Kill `sessions`, then `git worktree remove [--force]`. None once the
    tree is gone, else git's reason. The ONE copy `POST /remove`, `/cleanup`
    and `wt remove` share; the branch is each caller's own business.

    The kills are deliberately FIRST: removing the directory out from under a
    live pane leaves a session whose cwd no longer exists. A caller that must
    look between the two (`/cleanup`'s occupant probe) kills with
    `kill_sessions` itself and passes no sessions here.
    """
    kill_sessions(sessions)
    code, _out, err = run_bounded(build_worktree_remove_command(repo_root, path, force=force), GIT_TIMEOUT_SECONDS)
    return None if code == 0 else (err or "git worktree remove failed").strip()[:300]


def checkout_path_for(repo_root: Path, branch: str) -> Path | None:
    """The worktree holding `branch`, or None. One bounded `git worktree list`.

    Asked only for an `existing` create: git itself refuses to check a branch
    out twice, but its stderr arrives after the directory was made, and the
    answer the user needs is WHERE it already is.
    """
    record = worktree_by_branch(list_worktrees(repo_root)).get(branch)
    return Path(record["path"]) if record and record.get("path") else None


def tree_present(path: Path) -> bool:
    """Is the tree git lists still on disk? A `prunable` one is not, and a
    session started there would open in a directory that does not exist."""
    return Path(path).is_dir()


def run_bootstrap(script: Path | None, path: Path, repo_root: Path, env: dict[str, str] | None = None) -> str | None:
    """The repository's `.tldrack/bootstrap.sh` (`repo.py`) → a note, or None.
    No hook is the common case and is not a note.

    It runs only when the operator pinned its exact bytes (`hook_trust.py` owns
    why), and what runs is a private copy of the bytes that were checked.

    Deliberately NOT fatal. A worktree whose dependencies were not linked is
    still a perfectly good place to read code and write a fix. Failing the whole
    CREATE over it would trade a working session for a tidy one.

    `env` is the launch profile's (`profiles.child_env`), so a hook that writes
    under `${CLAUDE_CONFIG_DIR:-~/.claude}` writes into the account the agent
    will run as, not the bridge's own.
    """
    if script is None:
        return None
    data, note = hook_trust.verified_hook(repo_root, script)
    if data is None:
        return note
    fd, copy = tempfile.mkstemp(prefix="tldrack-bootstrap-", suffix=".sh")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(copy, 0o700)
        code, _out, err = run_bounded(["/bin/bash", copy, str(path), str(repo_root)], BOOTSTRAP_TIMEOUT_SECONDS, env=env)
    finally:
        try:
            os.unlink(copy)
        except OSError:
            pass
    if code != 0:
        return (err or "bootstrap failed").strip()[:300]
    return None


def spawn_session(claude_bin: str, session: str, cwd: Path, env: dict[str, str] | None = None) -> tuple[int, dict]:
    """The bare-`claude` spawn, for a host with no launch profiles."""
    return run_spawn(build_spawn_command(claude_bin, session, cwd, env), session)


def run_spawn(command: list[str], session: str) -> tuple[int, dict]:
    """Run a prepared `tmux new-session` argv — `profiles.spawn_argv`'s, or
    `build_spawn_command`'s — and map tmux's answer to `(status, body)`."""
    code, _out, err = run_bounded(command, SPAWN_TIMEOUT_SECONDS)
    if code != 0:
        if is_duplicate_session_error(err):
            return 200, {"session": session, "created": False}
        return 500, {"error": (err or "tmux new-session failed").strip()[:300]}
    return 200, {"session": session, "created": True}


def capture_pane(session: str) -> str | None:
    """The pane's visible rows, or None (the session died, or tmux is wedged)."""
    code, out, _err = run_bounded(["tmux", "capture-pane", "-p", "-t", pane_target(session)], PROBE_TIMEOUT_SECONDS)
    return out if code == 0 else None


def submit_prompt(session: str, prompt: str, press_enter: list[str]) -> bool:
    """Enter the typed prompt and confirm it left the input box. True only on
    that proof. The rules are the module header's (§ SUBMITTED): read the pane
    every `SUBMIT_POLL_SECONDS`, press `press_enter` only while the pane shows
    our line, at most `SUBMIT_TRIES` times, and stop reading after
    `SUBMIT_LOOKS`."""
    pressed = 0
    for _look in range(SUBMIT_LOOKS):
        time.sleep(SUBMIT_POLL_SECONDS)
        pane = capture_pane(session)
        if pane is None:
            return False
        if pressed and prompt_submitted(pane):
            return True
        if not prompt_drafted(prompt, pane):
            continue  # not painted yet, or mid-repaint: look again, press nothing
        if pressed == SUBMIT_TRIES:
            return False
        code, _out, _err = run_bounded(press_enter, PROBE_TIMEOUT_SECONDS)
        if code != 0:
            return False
        pressed += 1
    return False


def send_prompt(session: str, prompt: str, ready_timeout: float = READY_TIMEOUT_SECONDS) -> str | None:
    """Wait for Claude's input box, type the prompt, submit it. None once it has
    provably left the input box, else why not: `PROMPT_NOT_TYPED` (nothing went
    in) or `PROMPT_UNSUBMITTED` (it is in the box, waiting for an Enter).

    Runs on a BACKGROUND thread — never a request thread. `READY_TIMEOUT_SECONDS`
    is longer than any bridge route may block for, and the whole point of the
    route answering before this finishes is that session creation is not held
    hostage to a TUI that is still painting.
    """
    type_prompt, press_enter = build_send_keys_commands(session, prompt)
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        pane = capture_pane(session)
        if pane is None:
            return PROMPT_NOT_TYPED  # nothing to type into
        if looks_ready(pane):
            code, _out, _err = run_bounded(type_prompt, PROBE_TIMEOUT_SECONDS)
            if code != 0:
                return PROMPT_NOT_TYPED
            return None if submit_prompt(session, prompt, press_enter) else PROMPT_UNSUBMITTED
        time.sleep(READY_POLL_SECONDS)
    return PROMPT_NOT_TYPED
