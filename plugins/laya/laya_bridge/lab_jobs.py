"""AGENT RACK › LAYA's lab buttons: each one runs a fixed `laya-lab` command in its own tmux session, and a press
while another lab job runs waits its turn in a FIFO.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

WHAT A VERB RUNS is `LAB_ARGS`, in this file: a press never builds an argv from its body, and no press can append a
flag. The session is `laya-<verb>`, stamped `@laya_verb` and `@laya_surface`, which is how `parse_lab_panes` finds it.

TWO SPAWNERS, ONE AT A TIME.
- By default this module starts the session itself (`job_argv`): the tmux recipe that cannot lose a fast failure.
- A machine that keeps an eval runner (a directory holding `evals/cli.py` and `registry/<id>.json`) names it in
  this plugin's `evals_dir` setting (README.md § Settings). Then a press runs `evals start laya-<verb> --session … --stamp …` there instead, so every lab
  job is also a run in that runner's ledger, and the verb's argv is read from ITS registry file (`runner_args`
  checks it). The runner is optional: nothing in this repository needs one.

ONE LAB JOB AT A TIME, THE REST IN ORDER. laya-lab's verbs share the GPU and the operator's files (a PROMOTE rewrites
the private suite a GOLDEN is reading), so a press while one runs is appended to `<laya>/lab-queue.json` and reads
`queued #n`. `advance` starts the head once nothing runs: on every press, and from the plugin's drain thread while
anything waits (plugin.py `LabDrain`), so a night's SCORE + GOLDEN + COMPARE run back to back with nobody watching.
A press of a verb already running is `409 busy`; of one already queued, its place again. A queued job is re-checked
when its turn comes (a RE-SWEEP whose labels moved, a PROMOTE with nothing left) and dropped with a log line if it no
longer holds.

A pane is a lab job by either mark: its `laya-<verb>` session stamped `@laya_verb` (a press here), or an `@eval_id`
naming a lab eval (the same eval started by a runner from anywhere else), so the queue never starts beside one. The
nightly pack and the gated retrain (`laya-nightly`, `laya-retrain`: `OCCUPYING_EVALS`) are no press verbs, but each
scores, sweeps and rewrites the same files (the retrain: the training rows and the registry), so while one runs it
holds the queue too: a press waits behind it.

TRAIN AND LABEL ARE NOT LAB VERBS. `laya-lab train --as champion` serves the checkpoint it registers, and laya-serve
rebuilds `LAYA_EXTRA_MODELS` from models.json at every spawn, so a press could put an unshadowed checkpoint live
within about 15 idle minutes (its default, `--as challenger`, is shadow-only, but a press would still spend an hour of
GPU outside the pack). Training happens only through the gated retrain; `label-open-work` spends a model
subscription. `is_forbidden` refuses either verb, and `--force`, in any lab argv, built in or read from a runner's
registry, so no press can run them even if a registry file is edited to.

Every subprocess here has a timeout: these run on request threads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from laya_bridge import files, review_data, rows
from laya_bridge.envelope import Refusal, ok_body, params_unavailable
from laya_bridge.paths import CONFIG_ENV, DATA_ENV, LAB_DIR, REPO_ROOT
from laya_bridge.review_surfaces import REVIEW_SURFACES

SESSION_PREFIX = "laya-"
VERB_OPTION = "@laya_verb"
SURFACE_OPTION = "@laya_surface"
#: What an eval runner stamps on every session it starts (the module note owns the second spawner).
EVAL_ID_OPTION = "@eval_id"
#: The lab verbs, in OPS order (`layaLabVerbSchema`).
LAB_VERBS = ("challenger-sweep", "sweep", "sweep-reset", "score", "promote", "golden", "compare")
#: A job row's states (`LAYA_LAB_JOB_STATES`).
JOB_STATES = ("idle", "working", "queued", "failed")
#: `LAB_ARGS[surface][verb]`: what a lab verb runs after the laya-lab binary. Every review surface needs a full row.
LAB_ARGS: dict[str, dict[str, tuple[str, ...]]] = {
    "agent_open_work": {
        "challenger-sweep": ("sweep", "agent-open-work", "--labels", "operator", "--apply", "--model", "challenger", "--reserve"),
        "sweep": ("sweep", "agent-open-work", "--labels", "operator", "--apply"),
        "sweep-reset": ("sweep", "agent-open-work", "--reset"),
        "score": ("review-open-work", "--model", "served"),
        "promote": ("promote", "agent-open-work", "--flips", "--apply"),
        "golden": ("golden", "agent-open-work"),
        "compare": ("compare", "agent-open-work"),
    }
}
#: `LAB_EVALS[surface][verb]`: the id a lab verb has in an eval runner's registry (`<runner>/registry/<id>.json`).
LAB_EVALS: dict[str, dict[str, str]] = {"agent_open_work": {verb: f"laya-{verb}" for verb in LAB_VERBS}}
#: eval id → (surface, verb): how a session started outside this module is recognised.
EVAL_VERBS: dict[str, tuple[str, str]] = {eid: (surface, verb) for surface, ids in LAB_EVALS.items() for verb, eid in ids.items()}
#: Registered evals that are no press verb but hold the lab queue while they run (the module note owns why): eval id →
#: the key their pane is tracked under (never a `LAB_VERBS` row).
OCCUPYING_EVALS: dict[str, str] = {"laya-nightly": "nightly", "laya-retrain": "retrain"}
#: laya-lab's own venv, relative to the checkout this plugin was loaded from.
LAB_BIN = "lab/.venv/bin/laya-lab"
LAB_BIN_NAME = "laya-lab"
#: Under the optional eval runner's directory (the module note owns the runner).
REGISTRY_DIRNAME = "registry"
#: The data class a runner's registry must give a lab eval: operator text never leaves this machine.
DATA_CLASS = "private_operator"
REGISTRY_CAP_BYTES = 64 * 1024
#: The module note owns why: never as a lab eval's subcommand, never anywhere in its argv.
FORBIDDEN_SUBCOMMANDS = ("train", "label-open-work")
FORBIDDEN_FLAGS = ("--force",)

TMUX_TIMEOUT_S = 2.0
#: A start reads the registry (a runner's), checks the session and spawns it.
START_TIMEOUT_S = 30.0
EVALS_MODULE = "evals.cli"
#: A runner's `start --json` answer `code` → (HTTP status, this route's refusal code).
START_CODES = {
    "working": (409, "busy"),
    "no_tmux": (503, "tmux_missing"),
    "refused": (503, "eval_refused"),
    "spawn_failed": (500, "spawn_failed"),
}
LAB_HINT = "cd lab && uv sync"
PANE_FORMAT = "\x1f".join(
    (
        "#{session_name}",
        "#{pane_dead}",
        "#{pane_dead_status}",
        "#{" + VERB_OPTION + "}",
        "#{" + SURFACE_OPTION + "}",
        "#{" + EVAL_ID_OPTION + "}",
    )
)

QUEUE_FILENAME = "lab-queue.json"
QUEUE_VERSION = 1
QUEUE_CAP_BYTES = 64 * 1024
#: Held around every read-modify-write of the queue and every start, so two presses never both take the head. A press
#: waits for it at most one start's length.
_QUEUE_LOCK = threading.Lock()
LOCK_WAIT_S = START_TIMEOUT_S + 5


def session_name(verb: str) -> str:
    return f"{SESSION_PREFIX}{verb}"


def eval_id(verb: str, surface: str) -> str:
    return LAB_EVALS[surface][verb]


def runner_dir(raw: object, log: Callable[[str], None] = lambda _message: None) -> Path | None:
    """The eval runner the operator named (`evals_dir` in this plugin's settings), or None: the default, and what
    a value that is not an existing absolute directory reads as, with one line saying so."""
    if raw is None:
        return None
    path = Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else None
    if path is None or not path.is_absolute() or not path.is_dir():
        log("[laya] ignored evals_dir: not an existing absolute directory; lab jobs start without a runner")
        return None
    return path


def cli_text(verb: str, surface: str, runner: Path | None = None) -> str:
    """The same act, pasteable into a terminal. It skips the queue, so the tooltip is a terminal's way in. With a
    runner it is the recorded run, never the bare laya-lab command, which would leave no row there."""
    if runner is not None:
        return f"evals start {eval_id(verb, surface)}"
    return " ".join(("cd lab && uv run laya-lab", *LAB_ARGS[surface][verb]))


# ── the registry guard ────────────────────────────────────────────────────────


def is_forbidden(args: tuple[str, ...] | list[str]) -> bool:
    """A laya-lab argv (after the binary) that could put a checkpoint live or spend the subscription."""
    return not args or args[0] in FORBIDDEN_SUBCOMMANDS or any(arg in FORBIDDEN_FLAGS for arg in args)


def runner_args(verb: str, surface: str, runner: Path) -> tuple[str, ...] | None:
    """What `verb`'s eval runs after the laya-lab binary, read from the runner's registry file; None when the file is
    missing, unreadable, not a laya-lab command, takes extra arguments, is not private to this machine, or runs a
    forbidden verb (the module note owns why)."""
    try:
        data = files.read_capped(runner / REGISTRY_DIRNAME / f"{eval_id(verb, surface)}.json", REGISTRY_CAP_BYTES)
        entry = files.loads(data) if data is not None else None
    except (files.SourceError, ValueError, RecursionError):
        return None
    engine = entry.get("engine") if isinstance(entry, dict) else None
    argv = engine.get("argv") if isinstance(engine, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or Path(argv[0]).name != LAB_BIN_NAME:
        return None
    if entry.get("args") is not None or entry.get("data_class") != DATA_CLASS:
        return None  # no press may append a flag, and a lab job never leaves this machine
    args = tuple(argv[1:])
    return None if is_forbidden(args) else args


def lab_args(verb: str, surface: str, runner: Path | None = None) -> tuple[str, ...] | None:
    """What `verb` runs after the laya-lab binary: `LAB_ARGS`, or the runner's own registry file when one is named.
    None when there is nothing runnable."""
    if surface not in LAB_ARGS or verb not in LAB_ARGS[surface]:
        return None
    if runner is not None:
        return runner_args(verb, surface, runner)
    args = LAB_ARGS[surface][verb]
    return None if is_forbidden(args) else args


# ── panes ─────────────────────────────────────────────────────────────────────


def parse_lab_panes(output: str) -> dict[str, dict]:
    """`tmux list-panes -a -F PANE_FORMAT` → `{verb: {state, exit_code, session, surface}}` for lab jobs (the module
    note owns both marks). A live pane outranks a dead one of the same verb."""
    found: dict[str, dict] = {}
    for line in output.splitlines():
        fields = line.split("\x1f")
        if len(fields) != 6:
            continue
        session, dead, dead_status, verb, surface, eid = fields
        if verb in LAB_VERBS and session == session_name(verb):
            surface = surface if surface in REVIEW_SURFACES else None
        elif eid in EVAL_VERBS:
            surface, verb = EVAL_VERBS[eid]
        elif eid in OCCUPYING_EVALS:
            surface, verb = None, OCCUPYING_EVALS[eid]
        else:
            continue  # someone else's session, or one we did not stamp
        failed = dead == "1"
        if verb in found and (failed or found[verb]["state"] == "working"):
            continue
        found[verb] = {
            "state": "failed" if failed else "working",
            "exit_code": int(dead_status) if failed and dead_status.lstrip("-").isdigit() else None,
            "session": session,
            "surface": surface,
        }
    return found


def running(panes: Mapping[str, dict]) -> str | None:
    """The session of the lab job that holds the queue, or None."""
    return next((pane["session"] for pane in panes.values() if pane["state"] == "working"), None)


# ── the queue file ────────────────────────────────────────────────────────────


def queue_path(base: Path) -> Path:
    return base / QUEUE_FILENAME


def _entry_ok(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("verb") in LAB_VERBS
        and entry.get("surface") in LAB_EVALS
        and isinstance(entry.get("at"), str)
        and 0 < len(entry["at"]) <= 40
    )


def read_queue(base: Path | None) -> list[dict]:
    """`[{verb, surface, at}]` in press order: valid, unique entries only; [] when there is none to read."""
    if not files.is_real_dir(base):
        return []
    assert base is not None
    try:
        data = files.read_capped(queue_path(base), QUEUE_CAP_BYTES)
        payload = files.loads(data) if data is not None else None
    except (files.SourceError, ValueError, RecursionError):
        return []
    raw = payload.get("queue") if isinstance(payload, dict) and payload.get("version") == QUEUE_VERSION else None
    out, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        if _entry_ok(entry) and (entry["verb"], entry["surface"]) not in seen:
            seen.add((entry["verb"], entry["surface"]))
            out.append({"verb": entry["verb"], "surface": entry["surface"], "at": entry["at"]})
    return out


def write_queue(base: Path, queue: list[dict]) -> None:
    """Replace the queue whole: a 0600 temp file in the same folder, then one rename (a link at the path is replaced,
    never followed). OSError propagates."""
    target = queue_path(base)
    temp = base / f".{QUEUE_FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    body = json.dumps({"version": QUEUE_VERSION, "queue": queue}, sort_keys=True).encode("utf-8") + b"\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    try:
        os.replace(temp, target)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def position(queue: list[dict], verb: str, surface: str) -> int | None:
    """1-based place in the queue, or None."""
    return next((i + 1 for i, e in enumerate(queue) if e["verb"] == verb and e["surface"] == surface), None)


# ── rows ──────────────────────────────────────────────────────────────────────


def job_rows(panes: Mapping[str, dict] | None, queue: list[dict] | None = None, runner: Path | None = None) -> list[dict]:
    """Always one row per verb × review surface: working, else queued (#n), else failed, else idle."""
    out = []
    for surface in REVIEW_SURFACES:
        for verb in LAB_VERBS:
            live = (panes or {}).get(verb)
            if live is not None and live.get("surface") not in (surface, None):
                live = None
            place = position(queue or [], verb, surface) if surface in LAB_EVALS else None
            state = "working" if live and live["state"] == "working" else "queued" if place else live["state"] if live else "idle"
            out.append(
                {
                    "verb": verb,
                    "surface": surface,
                    "state": state,
                    "exit_code": live["exit_code"] if live and state == "failed" else None,
                    "position": place if state == "queued" else None,
                    "session": live["session"] if live and state != "queued" else session_name(verb),
                    "cli": cli_text(verb, surface, runner) if surface in LAB_EVALS else "",
                }
            )
    return out


# ── impure (thin; monkeypatched in tests) ─────────────────────────────────────


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def lab_ok(repo: Path = REPO_ROOT) -> bool:
    """laya-lab's venv is installed (`LAB_HINT`)."""
    return (repo / LAB_BIN).is_file()


def list_panes() -> str | None:
    """One `list-panes -a` for every lab job. No server is "" (nothing runs); a tmux that would not answer is None,
    which never reads as idle: the queue waits rather than start beside a job it could not see."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", PANE_FORMAT], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else ""


def start_argv(verb: str, surface: str) -> list[str]:
    """A runner's `evals start` for one lab verb in its own session, with the stamps this module reads (pure)."""
    return [
        sys.executable,
        "-m",
        EVALS_MODULE,
        "start",
        eval_id(verb, surface),
        "--session",
        session_name(verb),
        "--stamp",
        f"{VERB_OPTION}={verb}",
        "--stamp",
        f"{SURFACE_OPTION}={surface}",
        "--json",
    ]


def store_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The resolver's variables a lab job must see to land in THIS bridge's store (paths.py owns the order). tmux
    gives a new session its server's environment, not this process's, so they go in by name."""
    source = os.environ if env is None else env
    return {name: source[name] for name in (CONFIG_ENV, DATA_ENV) if source.get(name)}


def job_argv(verb: str, surface: str, repo: Path = REPO_ROOT, env: Mapping[str, str] | None = None) -> list[str]:
    """One tmux invocation that starts a lab verb in its own session and cannot race (pure).

    The session starts on an inert `sleep`, gets `remain-on-exit failed` and its two stamps, and only then does
    `respawn-pane -k` swap in the command. Set any later, the option could miss a command that exits at once, and a
    failed job would vanish with no exit status for the job row to show. tmux runs a multi-argument command directly,
    with no shell, so no argument ever meets one."""
    session = session_name(verb)
    target = f"={session}:"
    argv = ["tmux", "new-session", "-d", "-s", session, "-c", str(repo / LAB_DIR.name)]
    for name, value in store_env(env).items():
        argv += ["-e", f"{name}={value}"]
    argv += ["sleep", "60", ";", "set-option", "-w", "-t", target, "remain-on-exit", "failed"]
    for name, value in ((VERB_OPTION, verb), (SURFACE_OPTION, surface)):
        argv += [";", "set-option", "-t", target, name, value]
    return [*argv, ";", "respawn-pane", "-k", "-t", target, str(repo / LAB_BIN), *LAB_ARGS[surface][verb]]


def run_start(argv: list[str], cwd: Path, env: Mapping[str, str] | None = None) -> tuple[int | None, str, str]:
    """(exit code or None on a timeout, stdout, stderr) of one start command."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            timeout=START_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", ""
    except OSError as error:
        return -1, "", str(error)
    return done.returncode, done.stdout, done.stderr


def _last_line(text: str) -> str:
    """The last line, `$HOME` folded to `~`: a refusal never carries a path off this machine."""
    lines = text.strip().splitlines()
    return files.clip(lines[-1].replace(str(Path.home()), "~"), 160) if lines else ""


def start_direct(verb: str, surface: str, repo: Path = REPO_ROOT) -> str:
    """Start one lab verb now in its own tmux session; the session. `Refusal`: spawn_failed. A dead pane of the same
    verb (a failed run kept for its exit status) is replaced."""
    session = session_name(verb)
    run_start(["tmux", "kill-session", "-t", f"={session}"], repo)  # no such session is the usual answer
    code, _stdout, stderr = run_start(job_argv(verb, surface, repo), repo)
    if code is None:
        raise Refusal(500, "spawn_failed", f"tmux ran past {START_TIMEOUT_S:.0f} s")
    if code != 0:
        raise Refusal(500, "spawn_failed", _last_line(stderr) or f"tmux exited {code}")
    return session


def start_eval(verb: str, surface: str, runner: Path) -> str:
    """Start one lab eval now through the runner; its session. `Refusal`: busy, tmux_missing, eval_refused,
    spawn_failed, evals_unavailable (no JSON came back). The runner is started from this interpreter with PYTHONPATH
    at its directory, and sees the store this bridge resolved."""
    env = {**os.environ, "PYTHONPATH": str(runner)}
    code, stdout, stderr = run_start(start_argv(verb, surface), runner.parent, env)
    if code is None:
        raise Refusal(500, "spawn_failed", f"evals start ran past {START_TIMEOUT_S:.0f} s")
    try:
        reply = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise Refusal(503, "evals_unavailable", _last_line(stderr) or f"evals start exited {code}")
    if reply.get("code") == "started" and reply.get("ok") is True:
        return session_name(verb)
    status, refusal = START_CODES.get(str(reply.get("code")), (500, "spawn_failed"))
    raise Refusal(status, refusal, _last_line(str(reply.get("message") or "")) or f"evals start: {reply.get('code')}")


def start_job(verb: str, surface: str, repo: Path = REPO_ROOT, runner: Path | None = None) -> str:
    """Start one lab job by whichever spawner this machine has (the module note owns the two)."""
    return start_eval(verb, surface, runner) if runner is not None else start_direct(verb, surface, repo)


# ── the gates, the press and the queue ────────────────────────────────────────


def clean_lab_request(body: object) -> tuple[str, str]:
    """`(verb, surface)` from a `layaLabRequestSchema` body (strict)."""
    if not isinstance(body, dict) or set(body) != {"verb", "surface"}:
        raise Refusal(400, "bad_body", "the body must hold exactly verb and surface")
    verb, surface = body["verb"], body["surface"]
    if not isinstance(verb, str) or verb not in LAB_VERBS:
        raise Refusal(400, "bad_body", f"verb must be one of {', '.join(LAB_VERBS)}")
    if not isinstance(surface, str) or surface not in REVIEW_SURFACES:
        raise Refusal(400, "bad_body", f"surface must be one of {', '.join(REVIEW_SURFACES)}")
    return verb, surface


def gate(
    verb: str,
    surface: str,
    base: Path,
    *,
    loaded: Mapping[str, str] | None = None,
    repo: Path = REPO_ROOT,
    runner: Path | None = None,
) -> None:
    """Everything a lab job needs before it may start, checked at the press AND again when its turn comes.
    `Refusal`: tmux_missing, lab_missing, eval_missing, too_few_labels, nothing_to_promote."""
    if not tmux_available():
        raise Refusal(503, "tmux_missing", "tmux is not installed on this machine", "install tmux, then press again")
    if not lab_ok(repo):
        raise Refusal(503, "lab_missing", "laya-lab is not installed", LAB_HINT)
    if lab_args(verb, surface, runner) is None:
        raise Refusal(503, "eval_missing", f"no runnable lab eval for {verb}: see the runner's registry/laya-{verb}.json")
    spec = REVIEW_SURFACES[surface]
    if verb == "sweep":
        ready = review_data.sweep_readiness(base, spec.name, loaded=loaded)
        if not ready["ready"]:
            raise Refusal(409, "too_few_labels", ready["reason"] or "not enough clean operator labels to sweep")
    if verb == "promote" and not review_data.promotion_candidates(base, spec.golden_suite):
        raise Refusal(409, "nothing_to_promote", "No operator flip is waiting to become a golden case")


@dataclass
class Advance:
    """What one pass did: `started` (verb, surface, session), `dropped` (verb, surface, refusal), and how many wait."""

    started: tuple[str, str, str] | None = None
    dropped: list[tuple[str, str, Refusal]] = field(default_factory=list)
    waiting: int = 0
    held: bool = True  # False: another thread held the queue, so this pass did nothing


def _advance_locked(base: Path, loaded: Mapping[str, str] | None, repo: Path, runner: Path | None) -> Advance:
    done = Advance()
    queue = read_queue(base)
    output = list_panes()
    if output is None or running(parse_lab_panes(output)) is not None:
        done.waiting = len(queue)
        return done
    while queue:
        head = queue.pop(0)
        write_queue(base, queue)  # taken BEFORE the start: a crash mid-start never runs it twice
        try:
            gate(head["verb"], head["surface"], base, loaded=loaded, repo=repo, runner=runner)
            session = start_job(head["verb"], head["surface"], repo, runner)
        except Refusal as refusal:
            if refusal.code == "busy":  # a run of that eval we cannot see: it keeps its place
                queue.insert(0, head)
                write_queue(base, queue)
                break
            done.dropped.append((head["verb"], head["surface"], refusal))
            continue
        done.started = (head["verb"], head["surface"], session)
        break
    done.waiting = len(queue)
    return done


def advance(
    base: Path | None,
    *,
    loaded: Mapping[str, str] | None = None,
    repo: Path = REPO_ROOT,
    blocking: bool = False,
    runner: Path | None = None,
) -> Advance:
    """Start the queue's head if no lab job runs (at most one start a pass). Non-blocking by default: the drain thread
    and a read skip a pass another thread is already making."""
    if not files.is_real_dir(base):
        return Advance(waiting=0)
    assert base is not None
    if not (_QUEUE_LOCK.acquire(timeout=LOCK_WAIT_S) if blocking else _QUEUE_LOCK.acquire(blocking=False)):
        return Advance(held=False, waiting=len(read_queue(base)))
    try:
        return _advance_locked(base, loaded, repo, runner)
    finally:
        _QUEUE_LOCK.release()


def press(
    verb: str,
    surface: str,
    base: Path | None,
    *,
    loaded: Mapping[str, str] | None = None,
    repo: Path = REPO_ROOT,
    runner: Path | None = None,
) -> tuple[dict, Advance | None]:
    """A lab button: start it now, or queue it behind the job that runs. `(ok_body with session and queued, the pass
    it made)`. `envelope.Refusal`: params_unavailable, busy (THIS verb is running), every `gate` refusal, and the head's
    own start refusal when the press was the head."""
    if not files.is_real_dir(base):
        raise params_unavailable()
    assert base is not None
    gate(verb, surface, base, loaded=loaded, repo=repo, runner=runner)
    if not _QUEUE_LOCK.acquire(timeout=LOCK_WAIT_S):
        raise Refusal(409, "busy", "another lab press is being started: press again in a moment")
    try:
        output = list_panes()
        panes = parse_lab_panes(output or "")
        live = panes.get(verb)
        if live is not None and live["state"] == "working":
            raise Refusal(409, "busy", f"{live['session']} is still running")
        queue = read_queue(base)
        place = position(queue, verb, surface)
        if place is not None:
            return ok_body(f"{verb} is already queued #{place}", session=None, queued=place), None
        queue.append({"verb": verb, "surface": surface, "at": rows.utc_stamp()})
        try:
            write_queue(base, queue)
            done = _advance_locked(base, loaded, repo, runner)
        except OSError:
            raise Refusal(503, "queue_unwritable", f"{queue_path(base).name} could not be written") from None
    finally:
        _QUEUE_LOCK.release()
    if done.started is not None and done.started[:2] == (verb, surface):
        session = done.started[2]
        return ok_body(f"{verb} started: watch {session} in the rack", session=session, queued=None), done
    mine = next((refusal for v, s, refusal in done.dropped if (v, s) == (verb, surface)), None)
    if mine is not None:
        raise mine
    place = position(read_queue(base), verb, surface) or 1
    ahead = done.started[2] if done.started is not None else running(parse_lab_panes(list_panes() or "")) or "the queue"
    return ok_body(f"{verb} queued #{place}: one lab job at a time, behind {ahead}", session=None, queued=place), done
