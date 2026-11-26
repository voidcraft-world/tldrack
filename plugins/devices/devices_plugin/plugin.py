"""The devices plugin: CPU, memory, swap and GPU of this machine and every compute host, for the rack's DEVICES section.

    GET /state   {"version", "sample_s", "devices": [device, …]}, this machine first, then hosts.json order

What each field means, and the optional `session_memory`, `lease` and `jobs`: the README's § Route.
What the numbers mean: probe.py. What a GET costs, and why it never blocks: sampler.py. The shape
only ever gains keys.

The host list is the bridge core's `hosts.json`, read through its owner (`load_hosts`) on every
GET, so a host added to the file shows up on the next poll.

── OFF UNTIL THE OPERATOR TURNS IT ON ────────────────────────────────────────
Sampling forks ssh streams to other machines, so nothing happens until `plugins.devices.enabled`
is `true` (README § Settings). Off, the plugin stays mounted — a `create_plugin` that returned
nothing would be logged as a load failure — but it builds nothing: no sampler, no thread, no
subprocess, no settings past `enabled`. It advertises no routes and answers every request 404
`{"error": "off"}`, which is what a bridge without the plugin answers, so the rack's DEVICES
section simply does not appear.

Read-only once on, so the host's guard is the whole policy: any client the bridge admits may read
it. A GET cannot amplify cost: it keeps at most one sampler per device alive, and each winds down
on its own.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Mapping

from voidcraft_world_bridge.local_llm.hosts import LOCAL, RemoteHost, load_hosts
from voidcraft_world_bridge.local_llm.tunnel import SSH_OPTIONS

from devices_plugin.lease_view import load_lease
from devices_plugin.mac import read_local
from devices_plugin.probe import Reading
from devices_plugin.sampler import SAMPLE_SECONDS, DeviceSampler, LocalSampler, RemoteSampler, jobs_argv
from devices_plugin.sessions import read_session_memory
from devices_plugin.settings import ENABLE_HINT, PLUGIN_NAME, DevicesSettings, read_settings

STATE_VERSION = 1
ROUTE = "/state"
#: Every route's answer while the plugin is off (module header).
OFF_ANSWER = {"error": "off", "enabled": False, "plugin": PLUGIN_NAME, "hint": ENABLE_HINT}


class DevicesPlugin:
    name = PLUGIN_NAME

    def __init__(
        self,
        context,
        *,
        settings: DevicesSettings | None = None,
        load: Callable[[], list[RemoteHost]] = load_hosts,
        read: Callable[[], Reading] = read_local,
        read_sessions: Callable[[], dict] | None = None,
        read_lease: Callable[[], dict] | None = None,
        make_remote: Callable[[RemoteHost], DeviceSampler] | None = None,
    ) -> None:
        """`settings` defaults to the operator's, read from `context`. The rest are seams for tests;
        left out, `read_sessions` honours `daemons`, `read_lease` is the configured `lease` file's
        `status`, and a remote sampler runs the configured `jobs` command."""
        self._context = context
        self._lock = threading.Lock()
        self._remote: dict[str, DeviceSampler] = {}
        self._local: LocalSampler | None = None
        self.settings = settings if settings is not None else read_settings(context, self._log)
        if not self.settings.enabled:
            self._log(f"  {PLUGIN_NAME}: off ({ENABLE_HINT})")
            return
        self._load = load
        jobs = self.settings.jobs
        self._make_remote = make_remote or (
            lambda host: RemoteSampler(
                host.name, host.ssh, ssh_options=SSH_OPTIONS, jobs_argv=jobs_argv(jobs, host.name) if jobs else None
            )
        )
        if read_lease is None and self.settings.lease is not None:
            read_lease, problem = load_lease(self.settings.lease)
            if problem:
                self._log(f"  {PLUGIN_NAME}: lease {problem}; no lease readout")
        if read_sessions is None:
            read_sessions = functools.partial(read_session_memory, daemons=self.settings.daemons)
        self._local = LocalSampler(LOCAL, read, read_sessions=read_sessions, read_lease=read_lease)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def _log(self, line: str) -> None:
        log = getattr(self._context, "log", None)
        if callable(log):
            log(line)

    def routes(self) -> list[str]:
        return [f"/plugin/{self.name}{ROUTE}"] if self.enabled else []

    def busy_reasons(self) -> list[str]:
        return []

    def close(self) -> None:
        with self._lock:
            samplers = [sampler for sampler in (self._local, *self._remote.values()) if sampler is not None]
            self._remote = {}
        for sampler in samplers:
            sampler.close()

    # -- reading ---------------------------------------------------------------------------------
    def _samplers(self) -> list[DeviceSampler]:
        """This machine, then one sampler per host in the file. A host removed from the file, or
        re-pointed at another alias, has its stream stopped here."""
        if self._local is None:
            return []  # off: never sampled (every route already answered 404)
        hosts = self._load()
        stale: list[DeviceSampler] = []
        with self._lock:
            by_name = {host.name: host for host in hosts}
            for name, sampler in list(self._remote.items()):
                host = by_name.get(name)
                if host is None or getattr(sampler, "alias", host.ssh) != host.ssh:
                    stale.append(self._remote.pop(name))
            for host in hosts:
                if host.name not in self._remote:
                    self._remote[host.name] = self._make_remote(host)
            ordered = [self._local, *(self._remote[host.name] for host in hosts)]
        for sampler in stale:
            sampler.close()
        return ordered

    def state(self) -> dict:
        samplers = self._samplers()
        for sampler in samplers:
            sampler.touch()
        return {"version": STATE_VERSION, "sample_s": SAMPLE_SECONDS, "devices": [sampler.snapshot() for sampler in samplers]}

    # -- routes ----------------------------------------------------------------------------------
    def handle_request(
        self, method: str, subpath: str, query: dict[str, list[str]], body: dict | None, headers: Mapping[str, str] | None
    ) -> tuple:
        if not self.enabled:
            return 404, dict(OFF_ANSWER)
        if method != "GET":
            return 405, {"ok": False, "plugin": PLUGIN_NAME, "error": f"use GET {ROUTE}"}
        return self.handle_get(subpath, query)

    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if not self.enabled:
            return 404, dict(OFF_ANSWER)
        if subpath not in ("", "/", ROUTE):
            return 404, {"ok": False, "plugin": PLUGIN_NAME, "error": f"unknown route {subpath}"}
        return 200, self.state()

    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        return self.handle_request("POST", subpath, query, body, None)
