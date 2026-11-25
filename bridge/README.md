# tldrack bridge

The rack's server. It runs your coding agents in tmux, serves each session to the browser through
[ttyd](https://github.com/tsl0922/ttyd), and tells the rack page what every agent is doing: working,
waiting on you, or done. Everything listens on loopback only.

It is a host built on the public bridge core, [`voidcraft-world-bridge`](https://pypi.org/project/voidcraft-world-bridge/)
(the guards, the plugin host, `/snapshot`), and adds the terminal, agent and status routes.

## Start

```bash
brew install tmux ttyd          # or your package manager's
cd my-project
tldrack up                      # from a checkout: uv run --project /path/to/tldrack/bridge tldrack up
```

`tldrack up` starts tmux, ttyd and the bridge, builds the page (`ui/dist`) when it is missing, and
opens `http://127.0.0.1:7682/`. Run it again any time: a bridge that is up is left alone. New
sessions start in the directory you ran it from.

```bash
tldrack doctor                  # what this machine has, what is missing, and the fix for each
tldrack hooks --install         # let your agent CLIs report their state to the rack
tldrack status                  # is a bridge running, its pid, who started it
tldrack kill                    # stop the bridge; tmux sessions survive
```

A restart is `tldrack kill && tldrack up`. Every session reattaches.

## Updating

tldrack runs from its checkout, so an update is three steps: the checkout moves, the page is rebuilt
from it, and the bridge restarts into the new Python. Doing only the first by hand shows the old rack.

The bridge does all three. Every five minutes it runs one `git fetch` of the checkout's `origin`. When
the remote's main branch has moved, the rack's ⚙ shows a dot, and **Settings → SETUP → UPDATE**:

1. fast-forwards the checkout (`git merge --ff-only`);
2. rebuilds `ui/dist` beside the old one and swaps it in only if the build finished;
3. runs `uv sync --locked` if the lockfile changed;
4. imports the new code in a child process, and only if that works, restarts the bridge in place: the
   same process becomes the new code, so whatever started it (your terminal, tmux, a supervisor app)
   never sees it exit. tmux sessions and their agents are untouched.

It pulls only when the checkout is on the remote's main branch, with no uncommitted change to a tracked
file and no commit the remote lacks. Otherwise the card says what is in the way and changes nothing.

**Auto-update** (Settings → SETUP, or FEATURES) does the same on the timer, with no click. It is off
until you switch it on, never acts on a checkout with an uncommitted change, and does not retry a
failed run until the checkout or the remote moves. **Update check** switches the timer off; CHECK NOW
and UPDATE still work when pressed.

`GET /update` is what the card reads; `POST /update {"action": "check" | "apply"}` is its two buttons.

The page does the rest. **Settings** (the ⚙ in the rack, or `/settings.html`) shows the same checks
as `tldrack doctor` with a button beside each one a request can fix, installs the hooks and Laya,
and holds every switch below.

## Running lighter

Everything runs until you switch it off. **Settings → Features** lists each part with what it
costs and what the rack is without it; **LITE** switches off the ones that only cost.

| Switch | Off means | Applies |
|---|---|---|
| Code watch | the bridge stops hashing its own source every 5 s | at once |
| Agent CLI cross-check | no `claude agents --json` run every 15 s | at once |
| Session ledger ingest | no timed ingest; INGEST NOW still works | at once |
| Update check | no `git fetch` every 5 minutes, and no update dot | at once |
| A plugin, by name | it is not mounted: its routes and its part of the page are gone | at the next start |
| 3D office, office quality, motion | this browser stops drawing them | at once, per browser |

The bridge's switches are one file, `features.json` in the config directory, and hold for every
device. It lists only what differs from the default, so deleting it puts every switch back: every
part on, and Auto-update (the one switch that starts off) off. What a browser draws is that
browser's own choice and is kept there.

## What runs where

| Port | What | Reached by |
|---|---|---|
| 7681 | the terminals: one per tmux session, chosen by `?arg=<name>`. The bridge listens here, checks `Host`, and relays to ttyd on a Unix socket | the rack page's live pane |
| 7682 | the bridge's HTTP API, and the rack page at `/` | the page, the agent hooks, the CLI |

The bridge itself runs in a tmux session on its own socket, so it is never a row in the rack:
`tmux -L tldrack-bridge-7682 attach` shows its output, and `bridge-7682.log` in the state
directory keeps it.

```
http://localhost:7681/?arg=research          attach to "research", or create it
http://localhost:7681/?arg=attach:research   attach only: never creates a session
```

Anything that saves or replays a terminal URL must use the `attach:` form, or it brings back a
session you closed. `CLAUDE.md` owns why.

## Agent state

Each agent CLI reports its own state through hooks. `tldrack hooks --install` merges the relay
into every launch profile that lacks it and backs each file up first; `tldrack hooks` prints the
JSON to merge by hand. A hook is a silent no-op outside tmux and always exits 0.

```bash
curl http://127.0.0.1:7682/snapshot
# {"sessions": [{"session": "research", "live": true, "state": "busy", "title": "⠹ refactoring", …}],
#  "bridge": {"name": "voidcraft-world-bridge", "version": "…", "pid": 41320, "mode": "full", …}}
```

`/snapshot` is every session in one answer: the status store joined with a live read of tmux. It is
additive-only: keys are added, never changed. `GET /events` streams the same body over SSE.

## Where files go

| Role | Default | Holds |
|---|---|---|
| config | `~/.config/tldrack` | what you write: `profiles.json`, `plugins.json`, `settings.json`, `notify.json` |
| state | `~/.local/state/tldrack` | what the bridge writes and could lose: session stores, logs |
| data | `~/.local/share/tldrack` | what is kept: the session ledger, decisions, the rack database |

`TLDRACK_CONFIG_DIR`, `TLDRACK_STATE_DIR` and `TLDRACK_DATA_DIR` move a role. So does
`~/.config/tldrack/paths.json`, which can also put one store on another disk:

```json
{"data": "/Volumes/big/tldrack", "stores": {"ledger": "/Volumes/archive/agent-ledger"}}
```

A mapped directory must exist. Nothing is moved or migrated. `tldrack doctor` prints where each
role and store is and who decided.

## Optional parts

All off until you turn them on. `tldrack doctor` lists each one.

| Part | Turn on |
|---|---|
| A hosted page may call the bridge | `settings.json`: `"allowed_origins": ["https://rack.example"]` |
| A small model names each session from its first prompt | `settings.json`: `"session_namer": true` |
| A local model suggests the next step for a finished session | Settings → Features, or `tldrack next-step --mode shadow` (or `live`) |
| Phone pushes when an agent has waited a while | `tldrack notify --init` once, then the switch in Settings → Features |
| An app of yours owns and restarts the bridge | `settings.json`: `"supervisor_url": "myapp://bridge/start"` |
| Standing missions, decision import, a resource lease | `settings.json`: see `tldrack/settings.py` |

By default only pages served from this machine reach the bridge: no hosted site is allowed. A page
reached through a tunnel or a tailnet name has its own origin, so it needs both that host allowed
(`VOIDCRAFT_BRIDGE_ALLOWED_HOSTS`, the core's variable) and its origin under `allowed_origins`.

## Plugins

A plugin mounts routes under `/plugin/<name>/…` on the API port, behind the same guards.

```bash
TLDRACK_PLUGINS="/path/to/tldrack/plugins/worktree" tldrack up
# or in ~/.config/tldrack/plugins.json:
#   { "version": 1, "plugins": ["/path/to/tldrack/plugins/worktree"] }
```

Mounted plugins are listed at startup and on `/snapshot` under `plugins`. The rack treats each as a
capability: with no `worktree` plugin there are no worktree controls, and with no `laya` plugin no
turn-end verdicts. Nothing else changes.

To leave one out without editing that list, switch it off in Settings → Features. The switch only
subtracts: it can never mount a directory the list above does not name.

## Running the server in the foreground

`tldrack up` is the door for daily use. `tldrack` with no subcommand runs the same server in the
foreground (Ctrl-C stops it), which is what `up` starts inside tmux:

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--port` | `TLDRACK_PORT` | `7681` | Port ttyd listens on |
| `--status-port` | `TLDRACK_STATUS_PORT` | `--port + 1` | Port of the API and the page |
| `--start-dir` | `TLDRACK_START_DIR` | the working directory | Where new sessions start |
| `--session` | `TLDRACK_SESSION` | `tldrack` | The tmux session a bare terminal URL opens |
| `--pinned` | — | off | Serve only the default session; ignore `?arg=` |
| `--read-only` | — | off | View-only terminals |
| `--no-index-patch` | — | off | Serve ttyd's stock page (no live font size, no Shift+Enter newline) |
| `--print-hooks` | — | — | Print the hooks JSON and exit |

A second bridge beside a running one needs its own ports and its own directories:

```bash
TLDRACK_CONFIG_DIR=/tmp/rack/config TLDRACK_STATE_DIR=/tmp/rack/state TLDRACK_DATA_DIR=/tmp/rack/data \
  tldrack up --port 7781 --status-port 7791
```

## Tests

```bash
cd bridge && uv run pytest
```

Every test gets its own empty config, state and data directories (`tests/conftest.py`).
`CLAUDE.md` has the design and security notes.
