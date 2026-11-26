"""One sampler per device: started by a read, stopped by the absence of one.

── WHY IT COSTS NOTHING WHEN NOBODY LOOKS ────────────────────────────────────
Every GET `touch()`es every sampler. A sampler that has not been touched for
`IDLE_STOP_SECONDS` stops: this machine's thread exits, a remote host's ssh stream
is killed (and its loop dies on its next write). The rack only polls while the
DEVICES section is open and on screen (`ui/src/agent-rack/devices-client.ts`),
so a closed section or a hidden rack sends nothing and every sampler winds down.

── WHY A GET NEVER WAITS ─────────────────────────────────────────────────────
A request thread reads the last sample under a lock and returns. Connecting to a
host, reading its stream and backing off after a failure all happen on the
sampler's own thread, so a sleeping laptop reads as `connecting`, then `down`,
never as a stalled rack.

── ONE STREAM PER HOST, NOT ONE SSH PER POLL ─────────────────────────────────
A remote host runs `probe.remote_script` inside a single long-lived ssh session
and prints a block every `SAMPLE_SECONDS`. Per sample the host forks one `sleep`
(on Linux); per poll the bridge forks nothing.

── WHO HOLDS THIS MACHINE: THE RESOURCE LEASE ────────────────────────────────
When the operator configures one (`lease_view.py`), this machine also carries `lease`: who
holds each slot of their resource lease and how much, read on the sampler's own thread at the
sessions' cadence, never on a GET.

── WHAT A HOST RUNS: A SECOND, SLOWER STREAM ─────────────────────────────────
When the operator configures a `jobs` command (README § Settings), each host gets a second
stream beside its ssh one: that command, run on this machine with the host's name, printing
one JSON line of the host's named jobs every `JOBS_SAMPLE_SECONDS`. The plugin relays each
line through `parse_jobs_line`, which keeps the documented fields and nothing else. Same
lifecycle: it starts with the sampler and stops with it.
"""

from __future__ import annotations

import json
import math
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol

from voidcraft_world_bridge.shape import typed

from devices_plugin.probe import BlockReader, Reading, Ticks, busy_cores, remote_script, swap_rate
from devices_plugin.settings import HOST_PLACEHOLDER, SECONDS_PLACEHOLDER

SAMPLE_SECONDS = 2.0
#: Every 10 s: a session's memory moves slowly, and each read forks `ps` and `tmux`.
SESSIONS_EVERY_SAMPLES = 5
IDLE_STOP_SECONDS = 45.0
#: The window a jobs command is asked to sample over (`{seconds}`). CPU per job is a delta over a
#: window, so a longer one reads steadier.
JOBS_SAMPLE_SECONDS = 10
#: A longer line is not a jobs line, and is dropped.
JOBS_LINE_MAX = 65_536
#: The longest failure the jobs readout relays: the last stderr line of the jobs command.
JOBS_ERROR_MAX = 240
#: Text fields of a relayed job row are clipped here: a label, not a paragraph.
JOBS_TEXT_MAX = 120
JOB_STATES = ("running", "held", "idle")
BACKOFF_SECONDS = (5.0, 15.0, 60.0)
#: A stream with no sample by now is killed and retried: ssh can stall in its key exchange with no error and no
#: timeout, and the stream would read CONNECTING forever. Generous, so a jobs command that keeps a stall deadline
#: of its own (shorter than this plus a jobs window) reports its better-worded failure first.
FIRST_SAMPLE_SECONDS = 45.0
STDERR_TAIL = 4

CONNECTING, UP, DOWN = "connecting", "up", "down"


class Proc(Protocol):
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class DeviceSampler:
    """The state a GET reads, and the lifecycle around it. Subclasses own the reading loop."""

    def __init__(
        self, name: str, *, local: bool, clock: Callable[[], float] = time.monotonic, idle_stop_seconds: float = IDLE_STOP_SECONDS
    ) -> None:
        self.name = name
        self.local = local
        self._clock = clock
        self._idle_stop = idle_stop_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = False
        self._closed = False
        self._touched = clock()
        self._state = CONNECTING
        self._error: str | None = None
        self._reading = Reading()
        self._cpu_busy: float | None = None
        self._ticks: Ticks | None = None
        #: The last cumulative swap count and when it was read: what `swap_rate` is a delta against.
        self._swap_moved: tuple[int, float] | None = None
        self._swap_rate: int | None = None
        self._sampled_at: float | None = None

    # -- read side (request threads) ----------------------------------------------------------
    def touch(self) -> None:
        """Mark the sampler wanted, and start its thread if it is not running."""
        with self._lock:
            self._touched = self._clock()
            if self._closed or self._running:
                return
            self._running = True
            self._state, self._error = CONNECTING, None
            self._forget_deltas()
            self._wake.clear()
        self._prime()
        threading.Thread(target=self._run_guarded, name=f"devices-{self.name}", daemon=True).start()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict:
        with self._lock:
            reading = self._reading
            age = None if self._sampled_at is None else round(self._clock() - self._sampled_at, 1)
            return {
                "name": self.name,
                "local": self.local,
                "state": self._state,
                "os": reading.os,
                "cores": reading.cores,
                "cpu_busy": self._cpu_busy,
                "mem_used": reading.mem_used,
                "mem_total": reading.mem_total,
                "swap_used": reading.swap_used,
                "swap_total": reading.swap_total,
                "swap_rate": self._swap_rate,
                "gpu_pct": reading.gpu.pct if reading.gpu else None,
                "gpu_mem_used": reading.gpu.mem_used if reading.gpu else None,
                "gpu_mem_total": reading.gpu.mem_total if reading.gpu else None,
                "age_s": age,
                "error": self._error,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._wake.set()

    # -- write side (the sampler's own thread) --------------------------------------------------
    def _wanted(self) -> bool:
        with self._lock:
            return not self._closed and self._clock() - self._touched < self._idle_stop

    def _forget_deltas(self) -> None:
        """Drop what a delta is taken against (lock held): the next sample starts one, it never spans a gap."""
        self._ticks = self._cpu_busy = self._swap_moved = self._swap_rate = None

    def _record(self, reading: Reading) -> None:
        with self._lock:
            now = self._clock()
            if reading.ticks is not None:
                self._cpu_busy = busy_cores(self._ticks, reading.ticks, reading.cores)
                self._ticks = reading.ticks
            else:
                self._cpu_busy = reading.cpu_busy
            before, at = self._swap_moved or (None, None)
            self._swap_rate = swap_rate(before, reading.swap_moved, None if at is None else now - at)
            self._swap_moved = None if reading.swap_moved is None else (reading.swap_moved, now)
            self._reading = reading
            self._sampled_at = now
            self._state, self._error = UP, None

    def _fail(self, error: str) -> None:
        with self._lock:
            self._state, self._error = DOWN, error[:200]
            self._forget_deltas()

    def _pause(self, seconds: float) -> bool:
        """Sleep, waking early on close. True while the sampler is still wanted."""
        self._wake.wait(seconds)
        return self._wanted()

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as error:  # noqa: BLE001 — a sampler thread must never take the bridge down
            self._fail(f"{type(error).__name__}: {error}")
        finally:
            with self._lock:
                self._running = False

    def _prime(self) -> None:
        """Anything cheap enough to answer the GET that started the sampler. Nothing, by default."""

    def _run(self) -> None:
        raise NotImplementedError


class LocalSampler(DeviceSampler):
    """This machine, read in-process (`mac.read_local`) every `SAMPLE_SECONDS`. The first read
    happens in the GET that starts it, so this machine never reads CONNECTING.

    Per-session memory forks, so it runs only on this thread, every `SESSIONS_EVERY_SAMPLES`
    samples: `sessions.py` owns the number and the cost. The lease readout rides the same
    cadence. A reader's absence means no key; a failed read keeps the last answer, because one
    `tmux` timeout is not an empty machine."""

    def __init__(
        self,
        name: str,
        read: Callable[[], Reading],
        *,
        sleep_seconds: float = SAMPLE_SECONDS,
        read_sessions: Callable[[], dict] | None = None,
        read_lease: Callable[[], dict] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(name, local=True, **kwargs)
        self._read = read
        self._sleep = sleep_seconds
        self._read_sessions = read_sessions
        self._session_memory: dict | None = None
        self._session_memory_at: float | None = None
        self._read_lease = read_lease
        self._lease: dict | None = None

    def snapshot(self) -> dict:
        row = super().snapshot()
        with self._lock:
            if self._read_sessions is not None:
                row["session_memory"] = (
                    {**self._session_memory, "age_s": max(0, self._clock() - self._session_memory_at)}
                    if self._session_memory is not None and self._session_memory_at is not None
                    else None
                )
            if self._read_lease is not None:
                row["lease"] = self._lease
        return row

    def _prime(self) -> None:
        self._sample()

    def _sample(self) -> None:
        try:
            self._record(self._read())
        except OSError as error:
            self._fail(str(error))

    def _sample_sessions(self) -> None:
        if self._read_sessions is not None:
            try:
                memory = self._read_sessions()
            except (OSError, subprocess.SubprocessError):
                memory = None
            if memory is not None:
                with self._lock:
                    self._session_memory = memory
                    self._session_memory_at = self._clock()
        if self._read_lease is not None:
            try:
                lease = self._read_lease()
            except Exception:  # noqa: BLE001 — one unreadable lease folder keeps the last answer, never the thread
                lease = None
            if isinstance(lease, dict):
                with self._lock:
                    self._lease = lease

    def _run(self) -> None:
        self._sample_sessions()
        samples = 0
        while self._pause(self._sleep):
            self._sample()
            samples += 1
            if samples % SESSIONS_EVERY_SAMPLES == 0:
                self._sample_sessions()


def jobs_argv(command: tuple[str, ...] | list[str], host: str, sample_seconds: float = JOBS_SAMPLE_SECONDS) -> list[str]:
    """The operator's jobs command for one host: `{host}` becomes the host's hosts.json NAME and
    `{seconds}` the sample window, in whichever words carry them. Plain substitution, never
    `str.format`, so a brace elsewhere in a word is left alone. Run without a shell."""
    seconds = f"{sample_seconds:g}"
    return [word.replace(HOST_PLACEHOLDER, host).replace(SECONDS_PLACEHOLDER, seconds) for word in command]


def parse_jobs_line(line: str) -> dict | None:
    """One jobs line → `{"window_s", "rows"}`; None for any other line (a banner, a warning).

    The line is the jobs command's, which the plugin did not write, so it is read as untrusted
    JSON and each row is rebuilt from the documented fields (README § Settings, `jobs`): a
    command line or anything else a tool happens to print beside them never reaches the route."""
    if len(line) > JOBS_LINE_MAX:
        return None
    try:
        body = json.loads(line)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        return None
    rows = [row for row in (_job_row(raw) for raw in body["jobs"]) if row is not None]
    return {"window_s": _amount(body.get("window_s")), "rows": rows}


def _amount(value: object) -> float | int | None:
    """A finite, non-negative number, or None (a bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _label(value: object) -> str | None:
    text = typed(value, str, "").strip()
    return text[:JOBS_TEXT_MAX] if text else None


def _state(value: object) -> str:
    return value if value in JOB_STATES else "idle"


def _job_row(raw: object) -> dict | None:
    """One job, keeping only its documented fields; None without a `key` and a `label`."""
    if not isinstance(raw, dict):
        return None
    key, label = _label(raw.get("key")), _label(raw.get("label"))
    if key is None or label is None:
        return None
    groups = []
    for group in typed(raw.get("groups"), list, []):
        name = _label(group.get("name")) if isinstance(group, dict) else None
        if name is not None:
            groups.append(
                {
                    "name": name,
                    "count": _amount(group.get("count")) or 0,
                    "state": _state(group.get("state")),
                    "cores": _amount(group.get("cores")) or 0,
                    "rss": _amount(group.get("rss")) or 0,
                }
            )
    return {
        "key": key,
        "label": label,
        "state": _state(raw.get("state")),
        "procs": _amount(raw.get("procs")) or 0,
        "cores": _amount(raw.get("cores")) or 0,
        "rss": _amount(raw.get("rss")) or 0,
        "age_s": _amount(raw.get("age_s")),
        "groups": groups,
        "groups_more": _amount(raw.get("groups_more")) or 0,
    }


def ssh_argv(alias: str, options: tuple[str, ...], sample_seconds: float, ssh: str = "ssh") -> list[str]:
    """The stream's argv. The alias is validated by the host list's owner before it gets here
    (`load_hosts`), and `--` keeps it from ever being read as an option."""
    return [ssh, *options, "--", alias, "sh", "-c", shlex.quote(remote_script(sample_seconds))]


class RemoteSampler(DeviceSampler):
    """A compute host, read from one long-lived ssh stream, and its jobs from a second one when
    `jobs_argv` is given (module header). Without it the row grows no `jobs` key."""

    def __init__(
        self,
        name: str,
        alias: str,
        *,
        ssh_options: tuple[str, ...],
        popen: Callable[[list[str]], Proc] | None = None,
        sample_seconds: float = SAMPLE_SECONDS,
        backoff_seconds: tuple[float, ...] = BACKOFF_SECONDS,
        first_sample_seconds: float = FIRST_SAMPLE_SECONDS,
        jobs_argv: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(name, local=False, **kwargs)
        self.alias = alias
        self._argv = ssh_argv(alias, ssh_options, sample_seconds)
        self._jobs_argv = jobs_argv
        self._popen = popen or _popen
        self._backoff = backoff_seconds
        self._first_sample = first_sample_seconds
        #: The live subprocess of each stream: "device", "jobs".
        self._procs: dict[str, Proc] = {}
        self._jobs: dict | None = None
        self._jobs_at: float | None = None
        #: Why the jobs stream last failed, until it samples again: drawn, so a stall is never a blank.
        self._jobs_error: str | None = None

    def snapshot(self) -> dict:
        row = super().snapshot()
        if self._jobs_argv is not None:
            with self._lock:
                if self._jobs is not None and self._jobs_at is not None:
                    row["jobs"] = {**self._jobs, "error": None, "age_s": round(self._clock() - self._jobs_at, 1)}
                elif self._jobs_error:
                    row["jobs"] = {"window_s": None, "rows": [], "error": self._jobs_error, "age_s": None}
                else:
                    row["jobs"] = None
        return row

    def close(self) -> None:
        super().close()
        self._stop_streams()

    def _stop_streams(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            _terminate(proc)

    def _run(self) -> None:
        jobs = None
        if self._jobs_argv is not None:
            with self._lock:
                self._jobs = self._jobs_at = self._jobs_error = None
            jobs = threading.Thread(target=self._run_jobs, name=f"devices-{self.name}-jobs", daemon=True)
            jobs.start()
        reader = BlockReader()

        def feed(line: str) -> bool:
            reading = reader.feed(line)
            if reading is not None:
                self._record(reading)
            return reading is not None

        try:
            self._follow("device", self._argv, feed, self._fail, reconnect=True)
        finally:
            # Stopped (idle or closed): the jobs stream goes with it, so a later start never runs two. The
            # wake cuts its backoff short; the next `touch` clears it before starting again.
            self._stop_streams()
            self._wake.set()
            if jobs is not None:
                jobs.join(timeout=10)

    def _run_jobs(self) -> None:
        def feed(line: str) -> bool:
            parsed = parse_jobs_line(line)
            if parsed is not None:
                with self._lock:
                    self._jobs, self._jobs_at, self._jobs_error = parsed, self._clock(), None
            return parsed is not None

        # A tool's own error lines usually open with its name; the readout already says whose they are.
        prefix = f"{Path(self._jobs_argv[0]).name}: " if self._jobs_argv else ""

        def fail(error: str) -> None:
            with self._lock:
                self._jobs = self._jobs_at = None
                self._jobs_error = (error.removeprefix(prefix) or "the jobs stream stopped")[:JOBS_ERROR_MAX]

        try:
            self._follow("jobs", self._jobs_argv or [], feed, fail, reconnect=False)
        except Exception:  # noqa: BLE001 — the jobs readout is optional; it must never take the sampler down
            fail("")

    def _follow(
        self, stream: str, argv: list[str], feed: Callable[[str], bool], fail: Callable[[str], None], *, reconnect: bool
    ) -> None:
        """Run `argv` again after each failure, backing off, for as long as the sampler is wanted."""
        failures = 0
        while self._wanted():
            stopped_idle, error = self._stream(stream, argv, feed)
            if stopped_idle:
                return
            failures += 1
            fail(error)
            if not self._pause(self._backoff[min(failures, len(self._backoff)) - 1]):
                return
            if reconnect:
                with self._lock:
                    self._state = CONNECTING

    def _stream(self, stream: str, argv: list[str], feed: Callable[[str], bool]) -> tuple[bool, str]:
        """Read one subprocess to its end, `feed`ing each line (True when the line closed a sample).
        (True, "") when it was stopped for being idle."""
        proc = self._popen(argv)
        with self._lock:
            self._procs[stream] = proc
        stderr: deque[str] = deque(maxlen=STDERR_TAIL)
        drain = threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True)
        drain.start()
        stalled = threading.Event()

        def stall() -> None:
            stalled.set()
            proc.terminate()

        watchdog = threading.Timer(self._first_sample, stall)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in proc.stdout or ():
                if feed(line):
                    watchdog.cancel()
                    if not self._wanted():
                        return True, ""
            code = _wait(proc)
            drain.join(timeout=1)
            if stalled.is_set():
                return False, f"no answer from {self.alias} in {self._first_sample:g} s: the connection stalled"
            return False, stderr[-1] if stderr else f"{Path(argv[0]).name if argv else stream} exited ({code})"
        finally:
            watchdog.cancel()
            _terminate(proc)
            drain.join(timeout=1)
            with self._lock:
                if self._procs.get(stream) is proc:
                    del self._procs[stream]


def _popen(argv: list[str]) -> Proc:
    # stdin=DEVNULL: the stream reads nothing; start_new_session keeps ^C in the bridge's
    # terminal from reaching ssh.
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def _drain(stream: IO[str] | None, tail: deque[str]) -> None:
    for line in stream or ():
        if line.strip():
            tail.append(line.strip())


def _wait(proc: Proc) -> int | str:
    try:
        return proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return "still running"


def _terminate(proc: Proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
