# devices/

A tldrack plugin that feeds the rack's **DEVICES** section: CPU and memory for this machine and for
every compute host you list, as `2.2/10 · 22%` cores busy and `21.4/32 GB · 67%` memory used. Where a
device reports them, the GPU's utilisation sits on the CPU row and swap on the memory row, so a device
stays two rows tall. Optionally, which tmux session holds the memory here, who holds your resource lease, and what each
host is running.

**Off by default.** On, it opens one long-lived ssh stream to each compute host while the section is
on screen, so nothing happens until you turn it on (§ Settings).

The hosts are the public core's `hosts.json`: each entry names an ssh alias from your own
`~/.ssh/config`. Where the file lives and what it holds is owned by the core
(`voidcraft_world_bridge/local_llm/hosts.py`). It is read on every request, so a host added to it
shows up on the next poll.

## Settings

The `plugins.devices` object of the operator's `settings.json` (where that file lives, and how plugins
read it: `plugins/README.md`). Read once, when the bridge loads the plugin; restart the bridge after a
change. While `enabled` is not `true`, no other key is read.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | `true` turns the plugin on. Only a JSON `true` counts; a non-boolean is reported in the bridge log |
| `lease` | string | none | Absolute path (or `~/…`) to a Python file whose `status()` returns your resource lease's readout. Adds `lease` to this machine's row |
| `jobs` | list of strings | none | A command that prints what a host runs, one JSON line per sample. Adds `jobs` to each host's row |
| `daemons` | list of strings | none | Command-line substrings of long-lived services that are never counted as orphans |

```json
{"plugins": {"devices": {
  "enabled": true,
  "jobs": ["~/.local/bin/host-jobs", "--json", "--every", "{seconds}", "--host", "{host}"],
  "daemons": ["my-mcp-service"]
}}}
```

A key that fails validation is logged once at load and left at its default. Every default does less.

### `enabled`

**Off** means nothing runs: no sampler, no thread, no subprocess, no file read, at load or on request.
The plugin stays loaded (so the bridge does not log a load failure), advertises no routes on
`/snapshot`, logs `devices: off (set plugins.devices.enabled in settings.json)` once at load, and answers
every request with

```json
404 {"error": "off", "enabled": false, "plugin": "devices", "hint": "set plugins.devices.enabled in settings.json"}
```

That is the same status a bridge without the plugin answers, so the rack's DEVICES section does not
appear.

### `lease`

A resource lease is your own scheme for sharing one machine between jobs: each job takes a ticket for
a slot (a GPU's memory budget, a login, the CPU) before it runs. This plugin has no lease of its own.
Name a Python file that defines `status()`, returning the readout in the shape of
`schemas/src/resource-lease.ts` `LeaseStatusSchema`; the plugin loads the file once and calls
`status()` every 10 s on its own sampling thread, never on a request. The path must be absolute, so it
names the same file whatever directory the bridge was started from.

Loading the file runs it inside the bridge: the same trust you give every plugin directory you list in
`plugins.json`. A file that fails to load, or has no callable `status`, is logged and costs the readout,
never the plugin. A `status()` that raises keeps the last good answer.

### `jobs`

A command this machine runs once per compute host, for as long as that host's row is sampled. It is
run directly, never through a shell, so a word is never re-split. Two placeholders are replaced in
whichever words carry them:

| Placeholder | Becomes |
|---|---|
| `{host}` | the host's name in `hosts.json` (required: a command that does not name the host cannot be per host) |
| `{seconds}` | the sample window, 10 |

The first word is resolved when the plugin loads: as given when it is a path, otherwise on the bridge's
`PATH`. A bridge started from a desktop launcher often has a shorter `PATH` than your shell, so an
absolute path is the reliable form.

The command prints one JSON object per line, one line per sample:

```jsonc
// values invented
{"window_s": 10, "jobs": [
  {"key": "nightly-build", "label": "NIGHTLY BUILD", "state": "running" | "held" | "idle",
   "procs": 7, "cores": 7.9, "rss": 123456789, "age_s": 960,
   "groups": [{"name": "compile", "count": 6, "state": "running", "cores": 7.8, "rss": 98765432}],
   "groups_more": 0}]}
```

`cores` is cores busy over the window, `rss` is bytes, `age_s` is seconds since the job started,
`groups` folds a job's processes by a safe name, and `groups_more` counts the groups left out. A line
that is not such an object (a banner, a warning) is skipped. Each row is rebuilt from exactly these
fields, text clipped to 120 characters and an unknown `state` read as `idle`, so nothing else the
command prints reaches the route. Still, **never put a command line or a secret in `key`, `label` or a
group `name`**: they are shown to any client the bridge admits. When the command exits, its last
stderr line becomes the row's `jobs.error` until it samples again.

### `daemons`

This machine's row ranks memory by tmux session and counts **orphans**: agent CLIs and MCP servers that
launchd (or init) adopted after their session went away, which nothing on screen owns. Long-lived
services are adopted on purpose and are not strays: the tmux server and Codex's `app-server` are
excluded by name already. List any service of your own here (an MCP server you run as a daemon, for
instance) and a process whose command line contains one of these strings is never counted.

## Route

`GET http://127.0.0.1:<status-port>/plugin/devices/state`. Responses only ever gain keys.

```jsonc
// values invented
{"version": 1, "sample_s": 2, "devices": [
  {"name": "local", "local": true, "state": "up", "os": "Darwin arm64", "cores": 10,
   "cpu_busy": 2.2, "mem_used": 22977224704, "mem_total": 34359738368,
   "swap_used": 2147483648, "swap_total": 4294967296, "swap_rate": 0,
   "gpu_pct": 38.0, "gpu_mem_used": 9663676416, "gpu_mem_total": null, "age_s": 0.8, "error": null,
   "session_memory": {"sessions": [{"session": "fix-login", "rss": 1073741824, "procs": 9}], "more": 0,
                      "orphans": {"rss": 0, "procs": 0}}}]}
```

This machine comes first, then each host in `hosts.json` order.

| Field | Meaning |
|---|---|
| `state` | `connecting` (no sample yet, or reconnecting), `up`, `down` (`error` says why) |
| `cpu_busy` | cores busy over the last sample interval; `null` until there are two samples |
| `mem_used` / `mem_total` | bytes. "Used" is Activity Monitor's Memory Used on macOS, `MemTotal − MemAvailable` on Linux |
| `swap_used` / `swap_total` | bytes of swap in use and of swap there is. On macOS the total is what the kernel has allocated so far and grows on demand; `0` on Linux is no swap configured |
| `swap_rate` | bytes a second moving in and out of swap over the last sample interval; `null` until there are two samples. The level says how much was pushed out; the rate says the machine is short of memory now |
| `gpu_pct` | the busiest GPU's utilisation, 0–100; `null` on a device that reports no GPU (this machine on Linux, a Linux host without `nvidia-smi`) |
| `gpu_mem_used` / `gpu_mem_total` | bytes, summed over every GPU. Apple silicon shares one memory with the CPU, so its total is `null` |
| `age_s` | seconds since that sample |
| `session_memory` | this machine only, `null` until the first read: `{sessions: [{session, rss, procs, cpu_pct}], age_s, more, orphans: {rss, procs}}`, bytes, largest first. CPU is the OS process average (100% = one core); absent means unavailable. `age_s` ages the session sample independently of machine samples. Summed RSS is a ranking: it will not add up to the memory meter |
| `lease` | this machine only, and only with the `lease` setting; `null` until the first read |
| `jobs` | each host, only with the `jobs` setting; `null` until the first sample: `{window_s, rows, error, age_s}` |

The page makes every percentage and string (`ui/src/engine/terminal/device-readout.ts`). Any method but
GET is 405.

## What it costs

Nothing while nobody is looking. Every GET touches each device's sampler, and a sampler with no GET for
45 s stops. The rack polls only while DEVICES is open and on screen.

| Device | How | Per 2 s sample |
|---|---|---|
| this machine, macOS | ctypes `host_statistics` + `host_statistics64`, the `vm.swapusage` sysctl, and the GPU's I/O Registry entry, in-process | no subprocess, microseconds |
| this machine, Linux | `/proc/stat`, `/proc/meminfo` and `/proc/vmstat`, in-process; no GPU | no subprocess |
| a Linux host | ONE long-lived `ssh <alias> sh -c '<loop>'` reading `/proc` with shell builtins | forks `sleep`, and `nvidia-smi` where the host has one |
| a macOS host | the same stream, `ps` + `vm_stat` + `sysctl` + `ioreg` | forks a handful of small tools |

`session_memory` (and `lease`) run on this machine's sampling thread every 10 s, and only while the
section is open: one `ps` and one `tmux` each time. The `jobs` command, when set, is a second stream per
host, started and stopped with the first.

A GET never waits on ssh. A host that is asleep reads as `connecting`, then `down` with ssh's error, and
the stream retries after 5 s, then 15 s, then 60 s. A stream with no first sample after 45 s is killed
and retried, because ssh can stall in its key exchange with no error at all. Killing the stream ends the
remote loop on its next write. The why for each of these is in the module headers: `probe.py` (the
numbers), `sampler.py` (the lifecycle), `mac.py` (this machine) and `sessions.py` (the memory ranking).

## Enable it

1. Add this directory's absolute path to the bridge's `plugins.json`.
2. Set `plugins.devices.enabled` to `true` (§ Settings), and list your hosts in `hosts.json`.
3. Restart the bridge.

Dependencies: the standard library plus the public core, `voidcraft-world-bridge>=0.1.4,<0.2`, for
`hosts.json`'s reader, the ssh options and its JSON reader.

## Tests

```bash
uv run pytest          # from plugins/devices/
```

Every fixture value is invented.
