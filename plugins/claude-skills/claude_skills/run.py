"""POST /run: an allow-listed skill as an interactive Claude session in a detached tmux session.

It exists for buttons that run a skill where the operator can watch and answer it: the session
lands in `/snapshot` and the agent rack like any other.

**The allow-list is the operator's, and it is empty by default.** It is the `run` object of this
plugin's settings (`plugins["claude-skills"].run` in the bridge's settings.json; the keys are
documented in this plugin's README, § Settings). `runnable_from_settings` reads it once, when the
plugin is built, and drops any entry it cannot prove safe with one log line. With no settings,
every key is refused.

Three rules, each load-bearing:

- **Nothing is ever typed.** No `send-keys` anywhere in this plugin. A skill started here can stop
  at a question whose answer acts (places an order, merges, publishes), and a keystroke into that
  pane could answer it. The skill's prompt rides the spawn argv instead, so there is nothing to type.
- **The prompt is a fixed literal from the operator's settings, never caller text.** Task text is
  typed rather than passed as an argument (owned by plugins/worktree/worktree/create.py — see the
  note there) because free text can collide with a `claude` subcommand. A literal that starts with
  `/<skill>` is neither a subcommand nor a flag, so it is safe as the positional; the parser holds
  every entry to that shape.
- **A deterministic session name is the idempotency** (the coordinator's RESUME rule): a press on a
  live session answers `created: false` and touches nothing. `replace` kills it first. A caller
  asks for that only behind a two-step arm, and only when the bridge reports the agent idle; a kill
  answers nothing, since an unanswered question is not a yes. Two keys may therefore never share a
  session: the second would report the first one's run as its own.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: `tmux new-session -d` returns once the pane is forked: a wedged-tmux backstop, not a budget.
SPAWN_TIMEOUT_SECONDS = 15
#: `has-session` / `kill-session` run on a bridge request thread; a stale socket hangs them.
PROBE_TIMEOUT_SECONDS = 4

#: Claude Code's skill-name rule (lowercase letters, digits, hyphens; at most 64). A run key
#: is held to it as well: it is the token a button sends.
SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: The rack's session-name alphabet: safe as a tmux target and in a ttyd URL.
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: One prompt on the argv. A slash command and a sentence of mode, never a document.
MAX_PROMPT_CHARS = 2000

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class RunSpec:
    skill: str
    session: str
    prompt: str


def parse_run_entry(key: object, entry: object) -> RunSpec | str:
    """One `run` entry → its RunSpec, or the reason it is dropped (pure).

    Every field is optional: `skill` defaults to the key, `session` to the key, and `prompt` to
    `/<skill>`. A key may name a different skill, so one skill can have several entry points,
    each a fixed mode in its own session.
    """
    if not isinstance(key, str) or not SKILL_NAME_RE.fullmatch(key):
        return "a key is a lowercase skill-style name (a-z, 0-9, -; at most 64)"
    if not isinstance(entry, Mapping):
        return 'an entry is an object: {"skill"?, "session"?, "prompt"?}'
    skill = entry.get("skill", key)
    if not isinstance(skill, str) or not SKILL_NAME_RE.fullmatch(skill):
        return "`skill` is a skill name (a-z, 0-9, -; at most 64)"
    session = entry.get("session", key)
    if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
        return "`session` is a tmux name (A-Z, a-z, 0-9, _, -; at most 64)"
    prompt = entry.get("prompt", f"/{skill}")
    if not isinstance(prompt, str) or not (prompt == f"/{skill}" or prompt.startswith(f"/{skill} ")):
        return f"`prompt` starts with the skill's own slash command, `/{skill}`"
    if len(prompt) > MAX_PROMPT_CHARS or not prompt.isprintable():
        return f"`prompt` is one printable line of at most {MAX_PROMPT_CHARS} characters"
    return RunSpec(skill=skill, session=session, prompt=prompt)


def runnable_from_settings(raw: object, log: Callable[[str], None]) -> dict[str, RunSpec]:
    """The settings' `run` object → the allow-list. Absent is empty, and says nothing."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        log(f"  claude-skills: ignored settings.run — expected an object, got {type(raw).__name__}")
        return {}
    runnable: dict[str, RunSpec] = {}
    sessions: dict[str, str] = {}
    for key, entry in raw.items():
        parsed = parse_run_entry(key, entry)
        if isinstance(parsed, RunSpec) and parsed.session in sessions:
            parsed = f"session '{parsed.session}' already belongs to '{sessions[parsed.session]}'"
        if isinstance(parsed, str):
            log(f"  claude-skills: dropped run entry {str(key)[:64]!r} — {parsed}")
            continue
        runnable[key] = parsed
        sessions[parsed.session] = key
    return runnable


def describe_runnable(runnable: Mapping[str, RunSpec]) -> list[dict]:
    """What `GET /state` says may run, so a page shows only the buttons that would work."""
    return [{"key": key, "skill": spec.skill, "session": spec.session} for key, spec in sorted(runnable.items())]


def skill_file(skills_dir: Path, skill: str) -> Path:
    return skills_dir / skill / "SKILL.md"


def account_skills_dir(profiles: object, profile_id: str | None, default_claude_dir: Path) -> Path:
    """The skills dir of the account the run will start in: the profile's config dir, else the default."""
    describe = getattr(profiles, "describe", None)
    if profile_id is not None and callable(describe):
        try:
            described = describe(profile_id)
        except Exception:  # noqa: BLE001 — a registry hiccup falls back to the default account
            described = None
        config_dir = described.get("config_dir") if isinstance(described, Mapping) else None
        if isinstance(config_dir, str) and config_dir:
            return Path(config_dir).expanduser() / "skills"
    return default_claude_dir / "skills"


def decide_run(
    skill: object,
    runnable: Mapping[str, RunSpec],
    *,
    tmux_ok: bool,
    has_profiles: bool,
    profile_id: str | None,
    cli_found: bool,
    skill_file_exists: bool,
) -> tuple[int, dict] | None:
    """The refusal table for POST /run (pure). None = clear to spawn."""
    if not isinstance(skill, str) or skill not in runnable:
        if not runnable:
            return 400, {
                "code": "unknown_skill",
                "error": "no skill is runnable here",
                "hint": 'the operator lists runnable skills in settings.json, under plugins["claude-skills"].run',
            }
        return 400, {
            "code": "unknown_skill",
            "error": f"not a runnable skill; runnable: {', '.join(sorted(runnable))}",
            "hint": None,
        }
    if not tmux_ok:
        return 503, {"code": "tmux_missing", "error": "tmux not found", "hint": "install tmux"}
    if not has_profiles:
        return 503, {"code": "no_profiles", "error": "this bridge has no launch profiles", "hint": "update and restart tldrack"}
    if profile_id is None:
        return 503, {"code": "no_claude_profile", "error": "no claude launch profile is configured", "hint": "tldrack profiles"}
    if not cli_found:
        return 503, {"code": "cli_missing", "error": "the claude profile's CLI was not found on this machine", "hint": None}
    if not skill_file_exists:
        return 503, {
            "code": "skill_missing",
            "error": f"no {runnable[skill].skill}/SKILL.md in the bridge's project skills or the account's skills",
            "hint": "link it into the account (link-skills) or start the bridge from the repo that has it",
        }
    return None


# jscpd:ignore-start
# Plugins never import each other; a declared port of plugins/coordinator/coordinator/resume.py
# (session_exists, is_duplicate_session_error, run_resume_spawn), whose notes own the why.
def session_exists(name: str, run: Runner = subprocess.run) -> bool:
    try:
        result = run(
            ["tmux", "has-session", "-t", f"={name}"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def is_duplicate_session_error(stderr: object) -> bool:
    return isinstance(stderr, str) and "duplicate session" in stderr.lower()


def run_spawn(command: list[str], session: str, run: Runner = subprocess.run) -> tuple[int, dict]:
    try:
        result = run(command, capture_output=True, text=True, timeout=SPAWN_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return 500, {"error": "tmux new-session timed out"}
    except OSError as exc:
        return 500, {"error": f"could not run tmux ({type(exc).__name__}: {exc})"[:200]}
    if result.returncode != 0:
        if is_duplicate_session_error(result.stderr):
            return 200, {"session": session, "created": False}
        return 500, {"error": (result.stderr or "tmux new-session failed").strip()[:300]}
    return 200, {"session": session, "created": True}


# jscpd:ignore-end


def kill_session(name: str, run: Runner = subprocess.run) -> str | None:
    """`tmux kill-session` on the exact name; the error to report, or None once it is gone."""
    try:
        result = run(
            ["tmux", "kill-session", "-t", f"={name}"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"could not replace '{name}': tmux kill-session failed ({type(exc).__name__})"
    if result.returncode != 0 and session_exists(name, run):
        return (result.stderr or f"tmux kill-session '{name}' failed").strip()[:300]
    return None


class SkillRunner:
    """The effectful half of POST /run. `run` and `which` are injected so tests record every call.

    A skill is found where the spawned session will look for it: the project's
    `.claude/skills/` (the session's cwd) or the skills dir of the account it starts in.
    """

    def __init__(
        self,
        project: Path,
        profiles: object,
        log: Callable[[str], None],
        runnable: Mapping[str, RunSpec],
        *,
        claude_dir: Path,
        run: Runner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._project = project
        self._profiles = profiles
        self._log = log
        self._runnable = dict(runnable)
        self._claude_dir = claude_dir
        self._run = run
        self._which = which
        # The probe, the kill and the spawn are ONE critical section: apart they are a
        # check-then-act, and two overlapping presses would both see "not there".
        self._lock = threading.Lock()

    @property
    def runnable(self) -> dict[str, RunSpec]:
        return dict(self._runnable)

    def _skill_found(self, skill: str, profile_id: str | None) -> bool:
        homes = (self._project / ".claude" / "skills", account_skills_dir(self._profiles, profile_id, self._claude_dir))
        return any(skill_file(home, skill).is_file() for home in homes)

    def start(self, body: object) -> tuple[int, dict]:
        request = body if isinstance(body, dict) else {}
        skill = request.get("skill")
        replace = request.get("replace") is True
        profiles = self._profiles
        profile_id = profiles.default_of_kind("claude") if profiles is not None else None
        spec = self._runnable.get(skill) if isinstance(skill, str) else None
        refusal = decide_run(
            skill,
            self._runnable,
            tmux_ok=self._which("tmux") is not None,
            has_profiles=profiles is not None,
            profile_id=profile_id,
            cli_found=profile_id is not None and profiles.cli_found(profile_id),
            skill_file_exists=spec is not None and self._skill_found(spec.skill, profile_id),
        )
        if refusal is not None:
            return refusal

        replaced = False
        with self._lock:
            if session_exists(spec.session, self._run):
                if not replace:
                    return 200, {"skill": skill, "session": spec.session, "created": False, "replaced": False}
                failure = kill_session(spec.session, self._run)
                if failure is not None:
                    return 500, {"code": "replace_failed", "error": failure}
                replaced = True
            try:
                argv = profiles.spawn_argv(profile_id, spec.session, str(self._project), extra_args=[spec.prompt])
            except KeyError:
                return 503, {
                    "code": "no_claude_profile",
                    "error": "the claude launch profile vanished",
                    "hint": "restart tldrack",
                }
            code, payload = run_spawn(argv, spec.session, self._run)
        if code != 200:
            return code, {"code": "spawn_failed", **payload}
        created = payload.get("created") is True
        if created:
            self._log(f"  claude-skills: started /{spec.skill} in tmux session '{spec.session}'")
        return 200, {"skill": skill, "session": spec.session, "created": created, "replaced": replaced and created}
