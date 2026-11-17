"""Sampler lifecycle: a read starts it, the lack of one stops it, a failure backs off, and a GET never waits."""

import io
import json
import threading
import time

from devices_plugin.probe import END, Reading, Ticks
from devices_plugin.sampler import (
    CONNECTING,
    DOWN,
    JOBS_LINE_MAX,
    JOBS_TEXT_MAX,
    UP,
    LocalSampler,
    RemoteSampler,
    jobs_argv,
    parse_jobs_line,
    ssh_argv,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_session_memory_is_read_on_the_samplers_thread_and_survives_a_failed_read():
    answers = [{"sessions": [{"session": "a", "rss": 1, "procs": 1}], "more": 0, "orphans": {"rss": 0, "procs": 0}}]

    def read_sessions():
        if answers:
            return answers.pop()
        raise OSError("tmux gone")

    sampler = LocalSampler("local", lambda: Reading(cores=1), sleep_seconds=0.001, read_sessions=read_sessions)
    assert sampler.snapshot()["session_memory"] is None
    sampler.touch()
    assert wait_for(lambda: sampler.snapshot()["session_memory"] is not None)
    time.sleep(0.05)
    assert sampler.snapshot()["session_memory"]["sessions"][0]["session"] == "a"
    sampler.close()


def test_a_sampler_with_no_reader_grows_no_key():
    row = LocalSampler("local", lambda: Reading(cores=1)).snapshot()
    assert "session_memory" not in row and "lease" not in row


def test_the_lease_is_read_on_the_samplers_thread_and_a_failed_read_keeps_the_last():
    answers = [{"v": 1, "slots": [], "window": {"start": "03:00", "end": "07:00", "open": False}, "live": [], "unreadable": 0}]

    def read_lease():
        if answers:
            return answers.pop()
        raise PermissionError("lease folder unreadable")

    sampler = LocalSampler("local", lambda: Reading(cores=1), sleep_seconds=0.001, read_lease=read_lease)
    assert sampler.snapshot()["lease"] is None
    sampler.touch()
    assert wait_for(lambda: sampler.snapshot()["lease"] is not None)
    time.sleep(0.05)
    assert sampler.snapshot()["lease"]["window"]["start"] == "03:00"
    sampler.close()


class FakeProc:
    def __init__(self, stdout: str, stderr: str = "", code: int = 0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.code


LINUX = f"os=Linux x86_64\ncores=4\n{END}\n" + "".join(
    f"cpu=cpu {busy} 0 0 {1000 * i - busy} 0 0 0 0\nmem_total_kb=1000\nmem_avail_kb=250\n{END}\n"
    for i, busy in ((1, 100), (2, 600))
)


def test_local_sampler_reports_busy_cores_from_the_second_read():
    reads = iter(
        [
            Reading(os="Darwin arm64", cores=4, ticks=Ticks(0, 0), mem_used=1, mem_total=4),
            Reading(os="Darwin arm64", cores=4, ticks=Ticks(50, 100), mem_used=2, mem_total=4),
        ]
    )
    gate = threading.Event()

    def read():
        reading = next(reads, None)
        if reading is None:
            gate.wait(5)
            raise OSError("done")
        return reading

    sampler = LocalSampler("local", read, sleep_seconds=0.01)
    assert sampler.snapshot()["state"] == CONNECTING
    sampler.touch()
    # The GET that started it already carries the first read.
    assert sampler.snapshot()["state"] == UP and sampler.snapshot()["mem_used"] in (1, 2)
    assert wait_for(lambda: sampler.snapshot()["cpu_busy"] == 2.0)
    snap = sampler.snapshot()
    assert snap["local"] is True and snap["mem_used"] == 2 and snap["state"] == UP
    sampler.close()
    gate.set()


def test_local_sampler_stops_when_nobody_reads_it():
    clock = Clock()
    calls = []
    sampler = LocalSampler(
        "local", lambda: calls.append(1) or Reading(cores=1), sleep_seconds=0.01, clock=clock, idle_stop_seconds=45
    )
    sampler.touch()
    assert wait_for(lambda: len(calls) >= 2)
    clock.now += 46
    assert wait_for(lambda: not sampler.running)
    stopped_at = len(calls)
    time.sleep(0.05)
    assert len(calls) == stopped_at
    sampler.touch()  # a new read restarts it
    assert wait_for(lambda: len(calls) > stopped_at)
    sampler.close()


def test_remote_sampler_reads_the_stream():
    procs = []
    hang_up = threading.Event()

    class HeldOpen(FakeProc):
        """The stream's lines, then held open until the test hangs up."""

        def __init__(self):
            super().__init__("")
            self.stdout = self

        def __iter__(self):
            yield from LINUX.splitlines(keepends=True)
            hang_up.wait(5)

    def popen(argv):
        proc = HeldOpen()
        procs.append((argv, proc))
        return proc

    sampler = RemoteSampler("compute-0", "box2", ssh_options=("-o", "BatchMode=yes"), popen=popen, backoff_seconds=(10.0,))
    sampler.touch()
    assert wait_for(lambda: sampler.snapshot()["cpu_busy"] == 2.0)
    snap = sampler.snapshot()
    assert snap["cores"] == 4 and snap["mem_used"] == 750 * 1024 and snap["mem_total"] == 1000 * 1024
    assert snap["local"] is False and snap["os"] == "Linux x86_64"
    # The stream then ends (EOF): the host reads as down with ssh's exit, and lets go of the proc.
    hang_up.set()
    assert wait_for(lambda: sampler.snapshot()["state"] == DOWN)
    assert sampler.snapshot()["error"] == "ssh exited (0)" and sampler.snapshot()["cpu_busy"] is None
    assert sampler._procs == {}
    assert procs[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "--"]
    sampler.close()


def test_remote_failure_reports_stderr_and_backs_off():
    clock = Clock()
    starts = []

    def popen(argv):
        starts.append(clock.now)
        return FakeProc("", stderr="ssh: connect to host box2 port 22: Operation timed out\n", code=255)

    sampler = RemoteSampler("w", "box2", ssh_options=(), popen=popen, clock=clock, backoff_seconds=(0.05, 60.0))
    sampler.touch()
    assert wait_for(lambda: sampler.snapshot()["error"] == "ssh: connect to host box2 port 22: Operation timed out")
    assert sampler.snapshot()["state"] in (DOWN, CONNECTING)
    assert wait_for(lambda: len(starts) >= 2)  # the first backoff is short
    time.sleep(0.2)
    assert len(starts) == 2  # the second is 60 s
    sampler.close()
    assert wait_for(lambda: not sampler.running)


def test_remote_sampler_kills_the_stream_once_idle():
    clock = Clock()

    class Endless(FakeProc):
        def __init__(self):
            super().__init__("")
            self.stdout = self

        def __iter__(self):
            yield "cores=2\n"
            while not self.terminated:
                yield "cpu=cpu 1 0 0 1 0 0 0 0\n"
                yield f"{END}\n"
                clock.now += 1

        def poll(self):
            return None if not self.terminated else 0

    proc = Endless()
    sampler = RemoteSampler("w", "box2", ssh_options=(), popen=lambda argv: proc, clock=clock, idle_stop_seconds=45)
    sampler.touch()
    assert wait_for(lambda: proc.terminated)
    assert wait_for(lambda: not sampler.running)
    assert clock.now - 1000.0 >= 45


def test_ssh_argv_quotes_the_script_after_the_alias():
    argv = ssh_argv("box2", ("-o", "BatchMode=yes"), 2)
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "--", "box2", "sh"]
    assert argv[6] == "-c" and argv[7].startswith("'") and "sleep 2" in argv[7]


#: An operator's jobs command, as the `jobs` setting holds it (its first word already resolved).
JOBS_COMMAND = ("/bin/jobs-tool", "--json", "--every", "{seconds}", "--host", "{host}")
#: One line of it. Invented values.
JOBS_ROW = {
    "key": "nightly-build",
    "label": "NIGHTLY BUILD",
    "state": "running",
    "procs": 7,
    "cores": 7.9,
    "rss": 1,
    "age_s": 960,
    "groups": [{"name": "compile", "count": 6, "state": "running", "cores": 7.8, "rss": 1}],
    "groups_more": 0,
}
JOBS_LINE = json.dumps({"version": 1, "host": "w", "window_s": 10.0, "jobs": [JOBS_ROW]})


class Streaming(FakeProc):
    """Its lines, then more of `tail` until terminated: a stream that never ends on its own."""

    def __init__(self, lines, tail=()):
        super().__init__("")
        self.stdout = self
        self._lines, self._tail = lines, tail

    def __iter__(self):
        yield from self._lines
        while not self.terminated:
            yield from self._tail
            time.sleep(0.01)

    def poll(self):
        return None if not self.terminated else 0


def test_the_jobs_stream_is_relayed_beside_the_device_stream():
    started = {}

    def popen(argv):
        lines = LINUX.splitlines(keepends=True) if argv[0] == "ssh" else ["jobs-tool: warming up\n", JOBS_LINE + "\n"]
        started[argv[0]] = proc = Streaming(lines)
        return proc

    sampler = RemoteSampler("w", "box2", ssh_options=(), popen=popen, jobs_argv=jobs_argv(JOBS_COMMAND, "w"))
    assert sampler.snapshot()["jobs"] is None
    sampler.touch()
    assert wait_for(lambda: sampler.snapshot()["jobs"] is not None)
    jobs = sampler.snapshot()["jobs"]
    assert jobs["window_s"] == 10.0 and jobs["rows"][0]["key"] == "nightly-build" and jobs["age_s"] >= 0
    assert started["/bin/jobs-tool"] and started["ssh"]
    sampler.close()
    assert wait_for(lambda: started["/bin/jobs-tool"].terminated and started["ssh"].terminated)
    assert wait_for(lambda: not sampler.running)


def test_a_stream_that_never_samples_is_killed_and_retried():
    """ssh stalled in its key exchange has no error and no timeout: CONNECTING forever, until this."""
    attempts = []
    # The retry waits for the test, so the DOWN in between cannot be missed on a loaded machine.
    retry = threading.Event()

    def popen(argv):
        if attempts:
            retry.wait(3)
        attempts.append(Streaming([]) if not attempts else Streaming(LINUX.splitlines(keepends=True)))
        return attempts[-1]

    sampler = RemoteSampler("w", "box2", ssh_options=(), popen=popen, backoff_seconds=(0.01,), first_sample_seconds=0.2)
    sampler.touch()
    # The error stays until the next sample, and the next sample waits for `retry`.
    assert wait_for(lambda: sampler.snapshot()["error"] is not None)
    assert sampler.snapshot()["error"] == "no answer from box2 in 0.2 s: the connection stalled"
    assert sampler.snapshot()["state"] in (DOWN, CONNECTING) and attempts[0].terminated
    retry.set()
    assert wait_for(lambda: sampler.snapshot()["state"] == UP)
    sampler.close()
    assert wait_for(lambda: not sampler.running)


def test_a_failed_jobs_stream_says_why_until_it_samples_again():
    """A stall must not read as `jobs: null` forever, the same as "not sampled yet"."""
    attempts = []
    gate = threading.Event()

    def popen(argv):
        if argv[0] == "ssh":
            return Streaming(LINUX.splitlines(keepends=True))
        attempts.append(argv)
        if len(attempts) == 1:
            return FakeProc("", "jobs-tool: no answer from `ssh box2` in 30 s: the connection stalled\n", code=2)
        gate.wait(3)
        return Streaming([JOBS_LINE + "\n"])

    sampler = RemoteSampler(
        "w", "box2", ssh_options=(), popen=popen, backoff_seconds=(0.01,), jobs_argv=jobs_argv(JOBS_COMMAND, "w")
    )
    sampler.touch()
    assert wait_for(lambda: (sampler.snapshot()["jobs"] or {}).get("error"))
    jobs = sampler.snapshot()["jobs"]
    assert jobs == {
        "window_s": None,
        "rows": [],
        "error": "no answer from `ssh box2` in 30 s: the connection stalled",
        "age_s": None,
    }
    gate.set()
    assert wait_for(lambda: (sampler.snapshot()["jobs"] or {}).get("rows"))
    assert sampler.snapshot()["jobs"]["error"] is None
    sampler.close()
    assert wait_for(lambda: not sampler.running)


def test_idle_stops_the_jobs_stream_with_the_device_stream():
    clock = Clock()
    procs = {}

    def popen(argv):
        if argv[0] == "ssh":
            procs["ssh"] = Streaming(["cores=2\n"], tail=("cpu=cpu 1 0 0 1 0 0 0 0\n", f"{END}\n"))
        else:
            procs["jobs"] = Streaming([JOBS_LINE + "\n"])
        return procs["ssh" if argv[0] == "ssh" else "jobs"]

    sampler = RemoteSampler(
        "w", "box2", ssh_options=(), popen=popen, clock=clock, idle_stop_seconds=45, jobs_argv=jobs_argv(JOBS_COMMAND, "w")
    )
    sampler.touch()
    assert wait_for(lambda: "jobs" in procs and sampler.snapshot()["jobs"] is not None)
    clock.now += 60
    assert wait_for(lambda: procs["ssh"].terminated and procs["jobs"].terminated)
    assert wait_for(lambda: not sampler.running)
    assert sampler._procs == {}


def test_a_host_without_a_jobs_command_grows_no_jobs_key():
    assert "jobs" not in RemoteSampler("w", "box2", ssh_options=()).snapshot()


def test_the_jobs_command_names_the_host_and_the_window_in_place():
    assert jobs_argv(JOBS_COMMAND, "compute-0") == ["/bin/jobs-tool", "--json", "--every", "10", "--host", "compute-0"]
    # Plain substitution: a placeholder inside a word, and a brace that is not one, both survive.
    assert jobs_argv(("/t", "--target={host}:x", "{other}"), "h", 2.5) == ["/t", "--target=h:x", "{other}"]


def test_only_jobs_lines_are_relayed():
    assert parse_jobs_line(JOBS_LINE)["rows"] == [JOBS_ROW]
    for junk in ("", "not json", "[1, 2]", '{"jobs": 3}', json.dumps({"jobs": [{"a": 1}], "pad": "x" * JOBS_LINE_MAX})):
        assert parse_jobs_line(junk) is None, junk[:40]
    assert parse_jobs_line('{"jobs": [1, {"key": "k"}], "window_s": "x"}') == {"window_s": None, "rows": []}


def test_a_jobs_row_keeps_its_documented_fields_and_nothing_else():
    """The line is a tool's, not the plugin's: whatever else it prints never reaches the route."""
    row = {
        **JOBS_ROW,
        "cmd": "deploy --token=sekret",
        "env": {"TOKEN": "sekret"},
        "groups": [{**JOBS_ROW["groups"][0], "argv": ["sekret"]}, {"count": 3}, "junk"],
    }
    relayed = parse_jobs_line(json.dumps({"window_s": 10, "jobs": [row]}))["rows"][0]
    assert "sekret" not in json.dumps(relayed)
    assert relayed == JOBS_ROW


def test_a_jobs_row_with_odd_values_reads_as_the_quiet_default():
    line = json.dumps(
        {
            "window_s": -1,
            "jobs": [
                {
                    "key": "  k  ",
                    "label": "L" * 500,
                    "state": "exploding",
                    "procs": True,
                    "cores": -2,
                    "rss": "big",
                    "age_s": None,
                    "groups": "nope",
                    "groups_more": 1.5,
                }
            ],
        }
    )
    parsed = parse_jobs_line(line)
    assert parsed["window_s"] is None
    assert parsed["rows"] == [
        {
            "key": "k",
            "label": "L" * JOBS_TEXT_MAX,
            "state": "idle",
            "procs": 0,
            "cores": 0,
            "rss": 0,
            "age_s": None,
            "groups": [],
            "groups_more": 1.5,
        }
    ]


def test_session_sample_age_survives_failures_independently_of_machine_sample():
    clock = Clock()
    answers = [{"sessions": [], "more": 0}]

    def read_sessions():
        if answers:
            return answers.pop()
        raise OSError("timeout")

    sampler = LocalSampler("local", lambda: Reading(cores=1), clock=clock, read_sessions=read_sessions)
    sampler._sample_sessions()
    assert sampler.snapshot()["session_memory"]["age_s"] == 0
    clock.now += 35
    sampler._sample_sessions()
    sampler._sample()
    assert sampler.snapshot()["session_memory"]["age_s"] == 35
