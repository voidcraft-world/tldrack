"""RESUME a finding: reopen its conversation with `claude --resume <id>` in a
DETACHED TMUX SESSION, so the rack materializes a live pane for it like any
other session (watchable, attachable from any terminal, killable via /kill-session).

The pure halves — the argv, the session name, the refusal table — carry all the
judgement, so they are testable without spawning anything; the tmux calls around
them are thin and bounded.

── Why RESUME and not "jump to the live session" ──────────────────────────
There is zero overlap between tmux session names and Claude Code's own session
labels, and only a handful of the audited sessions are live at all. `claude
--resume <id>` reopens the exact conversation whether or not anything is still
running, which is the only thing that works for every finding.

Resume IN PLACE, deliberately not `--fork-session`: new turns append to the
original transcript, so the finding stops matching on the next audit and the
list self-clears. A fork would leave the parked transcript parked forever.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from coordinator.store import is_finding_key

#: `tmux new-session -d` returns as soon as the server has forked the pane, so
#: this is a wedged-tmux backstop, not a budget for the command inside it.
SPAWN_TIMEOUT_SECONDS = 15
#: Budget for the read-only `has-session` probe. Deliberately shorter than the
#: spawn's: a wedged tmux server (a stale socket is the classic one) makes
#: `has-session` hang forever, and this handler runs on a bridge REQUEST THREAD
#: — one leaked thread per RESUME click, against a route sitting under a long
#: list of findings. Probe + spawn together must also stay well inside a
#: client's own request timeout, or the panel reports a failure for a session
#: that in fact came up.
PROBE_TIMEOUT_SECONDS = 4

SESSION_PREFIX = "resume"
#: Long enough to recognise the conversation in `tmux ls`, short enough that the
#: rack row's name is not the whole row.
SLUG_MAX = 24
#: The same 8 characters `signals.finding_key` slices, so a session name and the
#: finding key it came from are visibly the same conversation.
SESSION_ID_CHARS = 8

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

#: A Claude Code session id is the STEM OF A TRANSCRIPT FILENAME
#: (`~/.claude/projects/<slug>/<stem>.jsonl` — see `sources.scan_transcript`),
#: so it is whatever anyone who can write into that directory named a file.
#: Claude Code names every one with a uuid, and pinning that is what closes
#: ARGUMENT injection:
#: shlex.quote makes `--dangerously-skip-permissions` safe from the *shell* and
#: then hands it to `claude` as a flag anyway. Two independent layers guard this
#: (see `build_resume_command`'s attached `--resume=` form) because the payoff
#: for missing it is an agent running in the user's repo with prompts disabled.
SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def is_session_id(value: object) -> bool:
    """True for a uuid-shaped session id, and only for one."""
    return isinstance(value, str) and bool(SESSION_ID_RE.fullmatch(value))


def slugify(text: object) -> str:
    """Lowercase, non-alphanumerics collapsed to `-`, truncated. Total: anything
    unusable (None, an emoji-only label) slugs to the empty string rather than
    raising, and the caller decides what to do with that."""
    if not isinstance(text, str):
        return ""
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    return slug[:SLUG_MAX].strip("-")


def session_name_for(label: object, session_id: object) -> str:
    """`resume-<label slug>-<session id prefix>` — PURE and DETERMINISTIC.

    Determinism is the idempotency guard, not a nicety: RESUME clicked twice
    must produce the same name so `session_exists` can short-circuit the second
    spawn (see `decide_resume` and the `created` flag). That rules out the
    obvious "-2 / -3 suffix counted off the sessions that exist right now" —
    click #2 would be handed a fresh name *precisely because* click #1 worked,
    and the user would end up with two agents appending to one transcript.

    So the disambiguator is the session id itself: two conversations that slug
    to the same label (the same project resumed twice, or a session with no
    label at all) still get different sessions, and the answer for a given
    conversation never changes as sessions come and go.
    """
    sid = slugify(session_id)[:SESSION_ID_CHARS]
    stem = slugify(label)
    if stem == sid:
        # `signals._build_finding` falls back to `session_id[:8]` when a
        # transcript has no title — so an unlabelled finding would otherwise
        # print the same eight characters twice.
        stem = ""
    parts = [part for part in (SESSION_PREFIX, stem, sid) if part]
    return "-".join(parts) if len(parts) > 1 else f"{SESSION_PREFIX}-session"


def inline_env(environ: dict | None = None) -> dict[str, str]:
    """Env vars that have to ride INLINE in the command string — the fallback
    for a host without launch profiles; with them, the account comes from the
    profile whose config dir OWNS the transcript (plugin.py `_resume_argv`).

    A tmux session inherits the tmux SERVER's environment, not the bridge's, and
    the server is usually much older than the bridge. `CLAUDE_CONFIG_DIR` is the
    one that matters here: the coordinator honours it when it reads the
    transcripts, so a resumed CLI that did not get it would go looking for the
    session in `~/.claude` and find nothing.
    """
    source = os.environ if environ is None else environ
    value = source.get("CLAUDE_CONFIG_DIR")
    return {"CLAUDE_CONFIG_DIR": value} if isinstance(value, str) and value else {}


#: The pane's own `cd`, a port of the host's: owned by tldrack `profiles.py`
#: (`PANE_CD_SCRIPT`) — see the note there.
PANE_CD_SCRIPT = 'cd -- "$1" 2>/dev/null || cd; shift; [[ -n "$1" ]] && exec "$@"; exec "${SHELL:-/bin/zsh}" -l'


def build_resume_command(
    claude_bin: str, session_id: str, session: str, cwd: Path, env: dict[str, str] | None = None
) -> list[str]:
    """The exact tmux argv (pure — unit-tested against quoting regressions).

    - `/bin/zsh -l -i`: login+interactive so the user's PATH resolves whatever
      `claude` shells out to, and so an exotic-PATH failure surfaces INSIDE the
      visible session rather than in a 500 nobody reads.
    - Everything interpolated into `inner` is shlex-quoted. A session id is a
      uuid today, but it reaches us as the STEM OF A FILENAME under
      `~/.claude/projects`, and the only thing standing between that directory
      and a shell is this call.
    - **`--resume=<id>`, attached, never `--resume <id>`.** The fence is owned
      by tldrack `profiles.resume_args` — see the note there (a
      transcript named `--dangerously-skip-permissions.jsonl` is the case).
      `decide_resume`'s uuid guard is the second, independent layer.
    - `cwd` needs no quoting: it rides as its own argv element (`-c`), never
      through the shell. That asymmetry is the whole reason this builder is
      pinned by a test.
    - The trailing `exec /bin/zsh -l` keeps the pane alive after `claude` exits
      — without it the rack row vanishes the moment the user quits, which is
      the one thing that makes a resumed session feel broken.
    """
    prefix = "".join(f"{name}={shlex.quote(value)} " for name, value in sorted((env or {}).items()))
    inner = (
        f"{prefix}{shlex.quote(str(claude_bin))} --resume={shlex.quote(str(session_id))}; "
        'echo; echo "[claude exited $?]"; exec /bin/zsh -l'
    )
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


def resolve_cwd(finding_cwd: object, start_dir: object) -> Path:
    """The directory the resumed pane opens in.

    A finding's cwd is wherever the transcript last recorded itself, and
    worktrees, scratch clones and renamed projects do disappear. `tmux
    new-session -c <gone>` fails outright, so a vanished directory has to
    degrade to the bridge's own start dir instead of turning RESUME into a 500.
    """
    for candidate in (finding_cwd, start_dir):
        if isinstance(candidate, str) and candidate:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
    return Path.home()


def session_exists(name: str) -> bool:
    """'=' prefix = exact match (a bare -t prefix-matches other sessions).

    BOUNDED, because it sits behind a long list of findings and runs on a
    bridge request thread. A tmux server wedged on a stale socket hangs
    `has-session` indefinitely, and a client's own request timeout only frees
    the BROWSER — the thread would leak, one per click, stalling the status
    listener every client reads from.

    A probe that times out answers "not there" rather than raising: the spawn
    is the next thing to run, it is bounded too, and its stderr is a far better
    error than anything this function could invent.
    """
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def tmux_found() -> bool:
    return shutil.which("tmux") is not None


def decide_resume(
    key: object, session_id: object, key_known: bool, claude_found: bool, tmux_ok: bool, session_live: bool = False
) -> tuple[int, dict] | None:
    """The refusal table for POST /resume (pure). None = clear to spawn.

    Every refusal is decided BEFORE anything is spawned, which is what makes the
    machine-acting half of this route testable at all. An already-open TMUX
    SESSION is deliberately NOT in here: unlike a spawn that answers 409 on a
    name clash, re-resuming a conversation into the pane it already has is a
    success — see the `created` flag.

    An already-open CONVERSATION is the opposite, and is the one refusal that
    protects data rather than the caller's expectations — see `session_live`.
    """
    if not is_finding_key(key):
        return 400, {"error": "expected a finding key like '<session prefix>:<kind>'"}
    if not key_known:
        return 400, {"error": f"no current finding for key '{key}' — re-run the audit"}
    if not isinstance(session_id, str) or not session_id:
        return 400, {"error": f"the finding '{key}' carries no session id to resume"}
    if not is_session_id(session_id):
        # The id is a filename stem off disk, so this is an authorization check,
        # not a typo check: a session id shaped like a flag is how a planted
        # `--dangerously-skip-permissions.jsonl` would turn one RESUME click
        # into an agent with prompts disabled. See `SESSION_ID_RE`.
        return 400, {"error": f"the session id on '{key}' is not a Claude session uuid"}
    if session_live:
        # The `blocked` findings this most often fires on sort to the TOP of the
        # panel (severity 0), so this is the likeliest row to be clicked — and
        # the one where resuming is both the most destructive and the least
        # useful. Two processes appending to one JSONL interleave their
        # `parentUuid` chains; and the session is not lost in the first place,
        # it is sitting in a pane waiting for an answer.
        return 409, {
            "error": "that conversation is still open — answer it where it is running "
            "rather than resuming, or a second agent joins the same transcript"
        }
    if not claude_found:
        return 503, {"error": "the `claude` CLI was not found — set COORDINATOR_CLAUDE_BIN"}
    if not tmux_ok:
        return 503, {"error": "tmux not found — install with: brew install tmux"}
    return None


def is_duplicate_session_error(stderr: object) -> bool:
    """tmux's own refusal to create a name that already exists.

    The last word on idempotency, and the only one that holds across processes:
    `session_exists` + `new-session` is a check-then-act, so a second RESUME
    that overlaps the first can pass the guard the first one had not yet
    invalidated. tmux settles that race atomically — but it settles it as a
    FAILURE, and reporting "duplicate session: resume-…" as a 500 would tell the
    user their agent did not come up when it did. Same outcome as the guard:
    the session is there, `created` is False.
    """
    return isinstance(stderr, str) and "duplicate session" in stderr.lower()


def spawn_resume_session(
    claude_bin: str, session_id: str, session: str, cwd: Path, env: dict[str, str] | None = None
) -> tuple[int, dict]:
    """The bare-`claude` resume, for a host with no launch profiles."""
    return run_resume_spawn(build_resume_command(claude_bin, session_id, session, cwd, env), session)


def run_resume_spawn(command: list[str], session: str) -> tuple[int, dict]:
    """Run a prepared `tmux new-session` argv — `build_resume_command`'s, or the
    bridge's `profiles.spawn_argv` with `--resume=<id>` appended — and map
    tmux's answer to `(status, body)`."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=SPAWN_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return 500, {"error": "tmux new-session timed out"}
    except OSError as exc:
        return 500, {"error": f"could not run tmux ({type(exc).__name__}: {exc})"[:200]}
    if result.returncode != 0:
        if is_duplicate_session_error(result.stderr):
            return 200, {"session": session, "created": False}
        return 500, {"error": (result.stderr or "tmux new-session failed").strip()[:300]}
    return 200, {"session": session, "created": True}
