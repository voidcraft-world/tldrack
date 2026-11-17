# tldrack bridge — agent guide

The rack's server: a host built on the public bridge core (`voidcraft-world-bridge`, on PyPI), with the CLI `tldrack`. It runs agent sessions in tmux, serves each one to the browser through **ttyd**, serves the rack page itself, and tells that page what every agent is doing. Everything listens on loopback only. It is a uv package with its own venv (`pyproject.toml`, `uv.lock`); the code is `tldrack/`.

Rack diagram: owned by `ui/src/agent-rack-diagram/rack-architecture.md` — update it when you add a plugin, route or hook.

A module's own docstring owns its why. This file is the map, the route contract, and the rules an agent would otherwise get wrong. Tests are `tests/test_<module>.py` unless a section names another file.

## Commands

```bash
cd bridge && uv run pytest                 # all tests; one file: uv run pytest tests/test_snapshot.py
tldrack up                                 # start the rack (or find it up); the only way an agent gets a bridge
tldrack doctor                             # what this machine has, what is missing, the fix for each; changes nothing
tldrack hooks [--install [--profile ID]]   # print the hooks JSON, or merge it into every launch profile that lacks it
tldrack status                             # the running bridge: pid, who started it, live sessions, the `code:` line
tldrack kill [--with-tmux]                 # stop the bridge; tmux sessions survive unless --with-tmux
tldrack profiles [--install-hooks ID]      # the launch profiles, and each one's CLI / hooks / auth
tldrack notify [--init] [--test]           # phone push: state, make a topic, send a test
tldrack permissions | next-step [--mode off|shadow|live] | decisions import|status
tldrack mission … | lead … | ledger ingest|status|export|search|audience
tldrack laya install|status|--help        # every word after `laya` goes to the checkout's plugins/laya/cli.py
```

- **Tests never touch the real home.** An autouse fixture in `tests/conftest.py` gives every test its own empty config, state and data directories.
- **Never stop the operator's live bridge** (ports 7681 and 7682). To try a change, run a dev bridge beside it: `--port 7781 --status-port 7791`, with `TLDRACK_CONFIG_DIR`, `TLDRACK_STATE_DIR` and `TLDRACK_DATA_DIR` set to empty directories of its own.
- A restart is `tldrack kill && tldrack up`, and it is the operator's call. Every session reattaches.
- **CLI shape**: `main` is a `click.group(invoke_without_command=True)`. Bare `tldrack` with flags runs the server in the foreground; every subcommand has its own `--port` / `--status-port`. `main` resolves paths and settings before any subcommand except `doctor`, which must still run to report a broken `paths.json` or `settings.json`.

### `tldrack up` (`tldrack/ensure.py`)

1. `/snapshot` answers: the bridge is up and is left alone.
2. Something holds the status port but does not answer: wait (a bridge mid-start looks like this); past `--timeout` it names the pid and the fix.
3. Nothing listens: build `ui/dist` when it is missing and the checkout has a build script (`--no-build` skips; a failed build never stops the start), then start the bridge. With settings `supervisor_url` set, that app is asked first and gets a short grace period. Otherwise the bridge starts in tmux on its OWN socket, `tmux -L tldrack-bridge-<status port>`, session `bridge`, so it is never a row in the rack. Its output is copied to `<state>/bridge-<status port>.log`.
4. Wait until every `--wait-for <route>` answers 200 (default: none). A plugin the snapshot says is not mounted fails at once.

It prints which case it found (down, starting, wedged, slow) and exits 0 or 1. `--no-start` only checks. The browser opens only when this run started the bridge and a person is at the terminal (`--open` / `--no-open` override). The started bridge gets a cleaned environment (`clean_env`): `TLDRACK_*` and `VOIDCRAFT_BRIDGE_*` ride through; an agent's `TMUX`, credentials and virtualenv do not.

## Configuration and paths

**Paths** (`tldrack/paths.py` — one resolver, three roles):

| Role | Default | Env | Holds |
|---|---|---|---|
| config | `~/.config/tldrack` | `TLDRACK_CONFIG_DIR` | what the operator writes: `profiles.json`, `plugins.json`, `settings.json`, `notify.json`, `paths.json` |
| state | `~/.local/state/tldrack` | `TLDRACK_STATE_DIR` | what the bridge writes and could lose: session stores, logs |
| data | `~/.local/share/tldrack` | `TLDRACK_DATA_DIR` | what is kept: the stores `ledger`, `decisions`, `rack`, `laya` |

Order: the env variable, then `<config>/paths.json`, then the default. `paths.json` maps a role, or one named store under `"stores"`, to an absolute directory that already exists; an unknown key or a missing directory is a startup error. The data directory always exists, so `/lead/*`, `/inbox*`, the ledger, decisions and name labels are on by default. `paths.export_env()` runs at CLI entry and writes the three roles into the environment, so a plugin or child process asks the environment. Nothing is ever moved or migrated.

**Settings** (`tldrack/settings.py`): `<config>/settings.json`. Every key is optional; an unknown key or a wrong type is a startup error.

| Key | Absent | Set |
|---|---|---|
| `allowed_origins` | only loopback pages reach the bridge | those browser origins do too |
| `supervisor_url` | `tldrack up` starts the bridge in tmux | it asks that URL's app first |
| `supervisor_process` | no launcher reads as a supervisor | a bridge whose parent is one of those process names reports `launched_by.kind` `menubar` (a frozen wire name; `identity.py`) |
| `ui_dist` | the page is served from the checkout's `ui/dist` | from this directory |
| `session_namer` | a session keeps the name it was given | a small-model call names it from its first prompt |
| `lease_module` | live marks are not written (`live_marks.py`) | that file's lease functions are used |
| `decision_docs` | `tldrack decisions import` reads nothing | those globs, under `--root` (default `.`) |
| `standing_missions` | no standing mission is minted | each is minted once (`missions.py`) |
| `plugins` | every plugin keeps its safe defaults | `plugins.<name>` is that plugin's own object |

`plugins` is checked for shape only (an object of objects, keyed by a valid plugin name); each plugin validates its own keys and documents them in its README § Settings. A plugin reads its object through the context's `plugin_settings(name)` and the operator's origins through `allowed_origins` (`tldrack/plugin_context.py`; the contract is owned by `plugins/README.md` § What the context carries). An object naming a plugin that did not mount is a startup line and a `doctor` warning, never an error.

Also off by default, each with its own owner: phone pushes (`notify.json`, off until it names a topic) and the next-step suggester (default mode `off`; `tldrack next-step --mode shadow` turns it on). `tldrack doctor` lists every optional part, on or off.

**Environment**: `TLDRACK_PORT` (7681, ttyd), `TLDRACK_STATUS_PORT` (port + 1, so 7682), `TLDRACK_SESSION` (default `voidcraft`: the page still hard-codes that name, so it is frozen for now), `TLDRACK_START_DIR`, `TLDRACK_PLUGINS` (colon-separated plugin dirs), `TLDRACK_UI_DIST`, `TLDRACK_NTFY_TOPIC` (a secret; under 24 characters is refused), `TLDRACK_LEAD_SKILL`, `TLDRACK_FIX_CAP_USD`. The `Host` allowlist is the core's variable, `VOIDCRAFT_BRIDGE_ALLOWED_HOSTS` (see Security posture).

**Server flags**: `--port`, `--status-port`, `--session`, `--start-dir`, `--cmd` (full command override), `--pinned`, `--font-size`, `--read-only`, `--no-status`, `--no-index-patch`, `--print-hooks`, `--print-statusline`.

**Start directory**: new sessions start in the directory the bridge was started from (`DEFAULT_START_DIR` is the working directory, or `TLDRACK_START_DIR`), never the package's location.

**Test-only file overrides**: most stores also take one `TLDRACK_<NAME>_PATH` or `_DIR` variable (`TLDRACK_ICONS_PATH`, `TLDRACK_RACK_DB`, `TLDRACK_LEDGER_DIR`, …), read next to that store's path resolver. Prefer the three role variables.

## How ttyd is launched

`tldrack/main.py` runs:

```
ttyd -p <port> -i 127.0.0.1 -O -t fontSize=<n> -t theme=<dark JSON> \
    -t titleFixed=tldrack -t disableLeaveAlert=true \
    -I ~/.cache/tldrack/index-<ver>-<hash>.html -a -W \
    sh -c '<ttyd_session_script>' ttyd-session
```

- **`-t disableLeaveAlert=true` must stay.** ttyd's page registers a `beforeunload` that Chrome shows as a "Leave site?" dialog, and the rack navigates its terminal frame on every session switch. The page also sends the option on the URL (`ui/src/engine/terminal/multi-terminal-layout.ts`).
- **Multi-session by default**: ttyd's `-a` appends the URL's `?arg=` values to the command argv, so `$1` in the `sh -c` wrapper picks the tmux session and `--session` is only the fallback. `--pinned` drops `-a` and serves the one session; `--cmd` overrides the whole command. The default session name is validated to `[A-Za-z0-9_-]+` because it lands inside the shell snippet.
- **A bare ttyd URL is a constructor, not a view.** `tmux new -A` creates what it cannot find, so anything that saves or replays `?arg=<name>` brings back a session the operator killed, faster than a kill can land.

| URL | tmux | Who uses it |
|---|---|---|
| `?arg=<name>` | `new -A -s <name>`: attach or **create** | only the rack's live pane |
| `?arg=attach:<name>` | `attach -t =<name>`: attach **only** | everything persisted or replayed, and a worktree session's pane |

  Built by `ttyd_session_script()` (`tests/test_ttyd_script.py` executes it against a fake `tmux`). A failed attach prints why and blocks on `read`; exiting would let ttyd's client reconnect in a loop and spawn processes without end. `:` cannot occur in a session name, so the prefix is unambiguous.
- The bridge sheds an inherited `TMUX` / `TMUX_PANE` at startup (`PANE_IDENTITY_ENV`), or every attach reads as nesting.
- **Process model**: ttyd is a `subprocess.Popen` child so the status listener thread shares the process. The bridge mirrors ttyd's exit code, except that a `POST /shutdown` stop exits 0: a deliberate stop must never read as a crash. `--no-status` execs ttyd directly.
- **Double-launch guard**: if the ttyd port has a listener and the status port too, exit 0 (idempotent). If only the ttyd port is taken, stay up in **status-only mode** beside the existing ttyd. That ttyd keeps serving its own page; it cannot be patched after the fact.
- Missing `ttyd` or `tmux` prints the install hint and exits 1. `tmux new -A` means a session survives browser reloads and can be attached from a terminal at the same time.

## The rack page is served by the bridge

`tldrack/ui_static.py`, `StatusHandler.do_GET` and `_serve_ui` in `main.py`. `GET /` and any path that is not an API route are answered from the built `ui/dist`, found per request: `TLDRACK_UI_DIST`, settings `ui_dist`, the checkout's `ui/dist`, then a packaged `_ui/`. API routes are asked first, so a build can never shadow one. Only regular files that resolve inside the build directory are served; `..`, an absolute segment or a symlink that leaves it names nothing. With no build, `/` answers 503 with a page that says to run `tldrack up`. Hashed files under `assets/` are immutable; HTML is `no-cache`. Because the page comes from the same loopback port as the API, no origin setup is needed.

## Index patching

ttyd's stock page knows nothing about the rack, so the bridge serves a **patched copy** through `ttyd -I` (`tldrack/index_patch.py`). ttyd has no index on disk, so once per (ttyd version, script revision, status port) the bridge starts a scratch ttyd on a random loopback port, fetches `/`, injects a `<script>` before the final `</body>` and caches the result under `~/.cache/tldrack/`. Any failure falls back to the stock page with a warning: the terminal always works. `--no-index-patch` opts out.

The injected script provides:

1. **Shift+Enter is a newline**: it sends backslash + CR, which agent CLIs and plain shells both read as a line continuation.
2. **No scrollbar**, and `color-scheme: dark`. Nothing is lost: tmux runs on the alternate screen, where xterm keeps no scrollback.
3. **Copy on select**: a tmux copy reaches the page only as an OSC 52 escape, which ttyd's xterm.js drops. Owned by `ui/src/engine/terminal/terminal-clipboard.ts` — see the note there; `tests/test_index_patch.py` runs the port against that file's vectors.
4. **Image paste and + IMAGE**: `POST /images` saves PNG/JPEG/GIF/WebP bytes (signature checked, size capped) to a private temporary file and returns a quoted path, which `term.paste` puts in the draft without Enter. Contract: `tldrack/images.py`; transport test: `tests/test_image_paste.py`. A remote viewer uploads to the viewed host's status port mapping (8443 by default, `imageBridgePort` on the URL to change it).
5. **◧ DRAW and ◇ SKILLS**: need the `agent-canvas` plugin. DRAW pastes a request from `GET /plugin/agent-canvas/draw` into the draft; SKILLS flips the pane's canvas view. Owners: `plugins/agent-canvas/agent_canvas/draw.py` and `desk.py`.
6. **⟳ SYNC, ⇡ DEPS, ✓ CHECKS**: each asks the `claude-skills` plugin to start one allow-listed skill in its OWN session (`POST /plugin/claude-skills/run`); nothing is typed into this pane. SYNC is armed: the first press only asks. A button is drawn only when its skill's key is in `runnable` on `GET /plugin/claude-skills/state` (`draw_runnable`): the allow-list is the operator's and empty by default, so by default there are none. What may run: `plugins/claude-skills/claude_skills/run.py`.
7. **Work-persona selector** (top left): see `/persona` below.
8. **⤒ TOP** (top middle): jumps to the first prompt of the conversation, then reads ⤓ END to come back. It types the agent's own scroll keys (Ctrl+Home, Ctrl+End), because an alternate-screen agent leaves no tmux history. Owned by `tldrack/index_patch.py` (`SCROLL_JUMPS`) — see the note there.
9. **Font size and soft keys**: see Page contract.

Without the plugin a button shows the reason beside it and types nothing. The `postMessage` strings must match `ui/src/events/messages.ts`: owned by `tldrack/index_patch.py` — see the note there.

## Identity, status and kill

- `tldrack/identity.py` captures at startup who launched the bridge: pid, cwd, tty (through `ps`, since a supervised bridge has its stdio redirected), and a nearest-first walk of the parent chain that classifies the launcher (an agent, a supervisor, tmux, a terminal app, launchd, headless). macOS `ps -o comm=` prints a full path that may contain spaces: split once from the left and classify on basenames.
- The result is the `bridge` key of `GET /snapshot`. Native clients decode strictly: keep the key additive, and never add required keys to session rows.
- `POST /shutdown` → 200 `{"ok", "pid", "mode"}`, then `ShutdownControl.trigger()`. Full mode terminates the owned ttyd child. Status-only mode SIGTERMs the external ttyd **only if `ps` still says it is `ttyd`** (`terminate_if_ttyd`): never signal a port squatter the bridge cannot name.
- `POST /kill-session?session=<name>` kills one tmux session with `tmux kill-session -t =<name>`. The `=` forces an exact match; a bare `-t` would prefix-match another session. An already-gone session is `killed: false` at 200; only a real tmux failure is 500. It forgets the name in every per-session store.
- `tldrack kill --with-tmux` kills only the sessions the pre-kill snapshot listed as live, never `tmux kill-server`.
- **`bridge.freshness`: is the running code still the code on disk?** Owned by `tldrack/freshness.py` — see the note there. `null` means the watcher never started: unknown, never stale. Staleness is a content hash, so a touch or a byte-for-byte revert is not stale. A preflight compiles and imports the changed tree in a CHILD process, because re-importing in-process would swap modules under live threads. `busy` lists each plugin's optional `busy_reasons()`. `tldrack status` prints it as the `code:` line.
- **A supervisor owns the bridge it started.** While its auto-start is on it restarts a killed bridge within seconds; `status` and `kill` both warn about this.

## The status listener

A stdlib `ThreadingHTTPServer` on a daemon thread, loopback only, that subclasses the core's handler. Its routes are the `GET_ROUTES` and `POST_ROUTES` tables of `StatusHandler` in `main.py`; a new route goes in those tables and inherits the Host and Origin guards. Access logs are silenced; only state transitions are echoed.

**Frozen on the wire until the page moves**: every route path, the `/snapshot` shape, the default session name `voidcraft`, the three `voidcraft-terminal-*` message names, and ports 7681 and 7682. `/snapshot` is additive only: keys are added, never changed or made required.

### Hooks and session state

- `POST /hook?event=<HookEvent>[&session=<name>]`: the body is the agent CLI's **raw hook stdin JSON** (capped; Write/Edit `tool_input` carries whole files). State comes from `HOOK_STATES[event]`; `PreToolUse` payloads classify into an activity `{zone, tool, detail}` (`tldrack/activity.py`). A malformed or oversized body degrades to a **state-only** update; never 400 the state away.
- **Claude's relay is an `http` hook** (since 2026-10-27): no `session`, the tmux pane named in `X-VC-Pane`. The bridge resolves pane → session (`snapshot.PaneSessionCache`) and answers an empty or unknown pane `200 ignored`, never the default session. Why: `main.py` `hook_http_entry`.
- **Codex relays the curl `hook_command`** from `$CODEX_HOME/hooks.json`, through its own `CODEX_HOOK_STATES` table; `HOOK_STATES` stays Claude-only so `--print-hooks` output never changes. The command is deliberately dumb (`curl --data-binary @-`), is guarded by `[ -n "$TMUX" ]` (outside tmux it still drains stdin), has a 2 s timeout, and **must always exit 0**: a non-zero hook exit surfaces errors in the CLI, and exit 2 blocks the hooked event. Codex runs no hook the user has not trusted in its TUI, and asks again when the file changes.
- A relay prints nothing, so on `PermissionRequest` it is "no decision" and the CLI's own dialog runs unchanged.
- **A hook carrying `agent_id` fired inside a subagent** and never speaks for the main thread: it cannot clear `attention`, it turns a finished turn into `background`, and it leaves `cwd`, `tasks` and the arbiter's clock alone (`main.py` § Subagent hooks).
- **The hook's `cwd` beats tmux's.** It is where the agent is (a worktree, a `/cd`); `pane_current_path` is where the pane's shell is. `snapshot.apply_hook_cwds` overlays it before the merge.
- **The state arbiter corrects what hooks miss** (`agent_index.py`, `state_arbiter.py`). Hooks leak: Esc mid-turn leaves `busy`, an answered permission prompt leaves `attention`. Claude's own `<config dir>/sessions/<pid>.json` index gets these right, and is joined by **pane id**, never by the session name in it, which survives a rename. A correction is written only when the index disagrees, changed after the stored state, and has held for 2 s; it is a compare-and-set, so a hook landing mid-pass wins. An index entry is believed only if its pid is alive, its start time still matches, and no other entry claims the session. Rows carry `state_source: "hook" | "index"`. **Started only by `start_status_server`**, never by `build_status_handler`, so a test-built handler is never reconciled against this machine's real sessions. The same rule holds for the LEAD watcher and the ledger scheduler.
- **`background`** is the fourth state: the turn is over and work is still running. Owned by `agent_index.py` § BACKGROUND WORK. The pane title was rejected as a signal by measurement.
- `claude agents --json` is the supported surface that checks the index and stands in when the index stops parsing; `/snapshot` carries `state_sources`. Owned by `tldrack/agents_cli.py` — see the note there.
- **Codex automatic review is working, not a question** (`tldrack/codex_rollout.py`). For main-thread `Bash` / `apply_patch` only, `/hook` keeps the state busy when the rollout's latest complete `turn_context` matches the hook turn and says `approvals_reviewer=auto_review`. Missing, stale or unfamiliar metadata keeps `attention`. Never infer the reviewer from `config.toml` or a model name. [Codex auto-review contract](https://learn.chatgpt.com/docs/sandboxing/auto-review).
- `POST /status` `{"session"?, "state", "message"?}` is the legacy state-only route; it never touches activity. `GET /status?session=` returns `{"session", "state", "age_seconds", "message", "activity"}`; an unreported session is `"unknown"`. **Staleness is the client's job**: the server reports raw age. State is in memory only.
- **`fresh`**: a new conversation nobody has prompted yet. Set by a `SessionStart` whose `source` is not `resume` or `compact`, cleared for good by any busy, attention or background write, enforced inside `StatusStore`. Owner: `main.py` § Fresh sessions.
- **`stuck`** (`tldrack/stuck.py`): a busy row whose same `Bash` command failed `REPEAT_FAILURES` times this turn, or that has sent no hook for `NO_PROGRESS_SECONDS`. Evaluated at snapshot time, cleared by its own cause, never dismissed.
- **`stop_failure`**: an API error cut the turn off (a usage limit, mostly). Owned by `transcript.py` `fold_stop_failure`. The newest main-thread assistant entry decides, so the next real reply clears it. A cut-off turn can arrive with no hook at all, so the arbiter's tick re-tails every turn-over row.
- **`tasks`** `{done, total, current} | null`: step progress folded from the task hooks and the agent's task-tool payloads (`tldrack/tasks.py`); survives `Stop`, cleared by a new conversation.

### `/snapshot`, `/events`, `/activity`

- `GET /snapshot` is every session in one answer: the union of `StatusStore.list()` and a live `tmux list-panes -a`, merged by `merge_snapshot` (pure functions in `tldrack/snapshot.py`; only `list_tmux_sessions` shells out). A tmux-only session reads `"unknown"`; a store-only session lists with `title: null, attached: false`.
- **The tmux `-F` fields are joined with `\x1f`, not a tab**, and `#{pane_title}` is LAST because a title is arbitrary text. A new field goes BEFORE it.
- Additive row keys include `profile`, `created_at`, `launch_permission_mode`, `launch_model`, `icon`, `tasks`, `fresh`, `stuck`, `stop_failure`, `persona`, `auto_name`, `lineage`, `context_left`, `open_work`, `lead` / `crew`, and `exited` + `exit_code` for a pane held open after its job ended (`snapshot.py` `TMUX_DEAD_FIELD`).
- Additive top-level keys: `bridge`, `plugins`, `profiles` (its presence is the client's capability signal for `/spawn`), `launch_pick`, `usage_limits`, `clients`, `state_sources`, `restorable`, `closed`, `next_step`. The counts are omitted at 0.
- `GET /events` is SSE: a `snapshot` event at once and on every state-affecting change, an `activity` event per ticker entry, and a ping when idle. Broker: `tldrack/events.py`, one queue per client so a wedged client never blocks a hook POST, capped at `MAX_SSE_CLIENTS` (503 above). An overflowing queue drops its OLDEST chunk. Polling stays fully supported.
- `GET /activity?limit=N` returns recent transitions newest first from a ring buffer (`tldrack/history.py`). Entries append only on a real state or activity change.
- `GET /capture?session=&lines=` returns the VISIBLE rows of a pane, never scrollback: full-screen agents draw on the alternate screen, where history returns less, not more. `GET /wait?session=&for=<state,…>[&timeout_ms=]` blocks until a session reaches a state (`tldrack/wait.py`).

### Spawn, rename, restore, reopen

- `POST /spawn` `{"name", "profile", "prompt"?, "model"?, "subagent_model"?, "parent_session"?}` starts a new tmux session running a launch profile. `profile: "auto"` picks the freest account. **201** created · **400** bad body, name, profile or prompt · **409** name taken · **503** no CLI for that profile, or nothing launchable · **404** keeps its one meaning: this bridge predates the route. **It forgets the name in every store BEFORE spawning**: the name is free, so whatever the stores hold describes a previous occupant. The icon is kept.
- `POST /rename-session` `{"session", "name"}` is a real `tmux rename-session -t =<old> <new>`. **It moves every per-session store entry onto the new key**; skipping that leaves a store-only row that every client draws as a dead session. **409** a live session owns that name · **410** the session is gone · a same-name rename is a 200 no-op. Hooks need no migration (they resolve the session at post time), and the arbiter joins by pane id. A caller must re-point its ttyd frame: `?arg=<old>` would now create an empty session under the freed name.
- **Session records and restore** (`session_records.py`, `restore.py`; routes in `tests/test_session_routes.py`). `<state>/session-records.json` keeps, per session, the cwd, profile, kind, conversation id and launch mode. A record is forgotten only by `/kill-session`, by `/spawn` reusing the name, by a session that vanished while others stayed live, or by age. A dead tmux server is never evidence: that is the reboot a restore exists for. `SessionEnd` clears the conversation id only for a user-ended reason. `GET /restorable` and `POST /restore {"sessions"?}` never expose the conversation id. Refusals are owned by `restore.py`; the important one: a session whose **cwd is gone is never relocated** to the start directory. A button only: nothing restores at boot.
- **The closed list** (`closed_sessions.py`): the record a close drops lands in `<state>/closed-sessions.json`, capped and aged out. A reboot is never a close. `GET /closed`, `POST /reopen {"id"}` (400 bad id · 404 not listed · 409 refused), serial under the restore lock.
- `POST /session-icon` `{"session", "icon": <id> | null}`: the bridge validates **shape only**; catalog membership is a client concern, so a new icon never needs a bridge upgrade. Picks persist and are never pruned.
- `POST /session-star` `{"session", "starred": <bool>}`: the operator stars a session to come back to, or clears the star. Every row then carries `starred` (a boolean; absent on a bridge with no star store, which is how a client knows to draw no control). A star follows the conversation, not the name: a rename moves it, a close takes it into the closed entry (`GET /closed` rows say `starred`; a starred close is never dropped for room), a reopen hands it back, and a spawn under a reused name starts without it. Owned by `tldrack/stars.py`.
- **Session families** (`session_lineage.py`): `/spawn` and worktree `/create` accept `parent_session`, stamped as `@vc_lineage`. Callers resolve it from their own pane, never the bridge's environment. Never join ancestry by mutable session names.

### Models, usage and context

- **Three model tiers**, covering disjoint failures: the transcript (`POST /hook`) beats the statusline (`POST /usage`, `statusline_models.py`) beats the on-disk store (`tldrack/models.py`). The disk store exists because an idle session emits no hook after a bridge restart. It persists **model and usage only**: a `busy` restored from disk is a lie. Model ids are validated by shape, not membership.
- `POST /usage`: the body is the **raw statusline stdin JSON** (`tldrack/statusline.py`; stanza from `--print-statusline`). `parse_usage_limits` (`tldrack/usage.py`) keeps the usage windows, keyed **per account** (the launch profile whose root holds the payload's `transcript_path`) and served as `profiles[i].usage`. A malformed shape is a 400; absent windows are a valid no-op.
- The same payload's `model.id` is read FIRST, before either early exit of the usage parse: usage is absent for exactly the sessions that need the model. It is stored under the transcript path, not the session name, and joined at snapshot time, so nothing writes to `StatusStore` (a write there would restamp `at` every second and mute the notifier's grace window). `context_left` rides the same join. A `tmux rename-session` must call `TranscriptTracker.rename`, or the join is lost.
- `transcript_path` is allowlisted (`transcript.py` `is_allowed_transcript`): it must end `.jsonl` and, after `realpath`, sit under a configured Claude config root, fixed at startup and never taken from the request. Reads are capped. A refused path keeps the prior reading; a hook the user's shell is blocked on must not fail.

### Open work, next step, persona, names

- **`open_work`**: a turn-end verdict from the mounted `laya` plugin, behind the rack's optional ⚠️ and ✅. Owned by `tldrack/open_work.py` — the question, the thresholds and the why are there. On `Stop` a judge re-reads the closing message and lands the verdict compare-and-set; any busy hook, a session start or end, and `/kill-session` void it. `POST /open-work/dismiss | confirm | snooze` `{"session"}` record the operator's grade or park the loop. A ✅ on a turn a question opened also carries `answer_to_read` until the operator opens that pane (`POST /open-work/read`) or the session turns busy (open_work.py § An answer to read). Which ⚠️ is news, a repeat, or held while the row's children still work: `tldrack/attention.py`. A verdict survives a bridge restart (`verdicts.py`). No `laya` plugin: nothing is queued, silently.
- **`next_step`**: a suggested next step for a turn-over row, never an act (`tldrack/next_step.py`). Modes `off` (default), `shadow`, `live`; only `live` adds the per-row `next_step`. `GET /next-step`, `POST /next-step/event` (the page reports what was shown or pressed; nothing is performed), `POST /next-step/settings {mode}`.
- **Work persona** (`tldrack/personas.py`): `GET /persona?session=` returns the pick and the catalog; `POST /persona {session, persona: id|null}` stores it. Nothing is typed: `/hook`'s `UserPromptSubmit` answer carries it as `additionalContext` on every prompt. Claude's http relay only; the curl relay discards the answer.
- **Topic names** (`session_names.py`, `session_topics.py`): a GENERATED session name (`vc-N`, a LEAD's `lead-<profile>-MMDD-HHMM`) gets a placeholder label at its first main-thread prompt. With settings `session_namer` on, one small-model `claude -p` call replaces it with the shortest topic slug unique among the live rows, such as `docs-voice`. The call goes through `claude_cli.run_agent`'s three gates: a subscription login, a plan with room, never overage. A line the rack typed (`pane_typist.RACK_TAGS`) names nothing. **The tmux name never changes**: the rack database keys runs and queues on it; `/snapshot` carries `auto_name`. An explicit rename suppresses the label.
- **Name labels** (`tldrack/topic_labels.py`): each landed topic is logged with its request, and the operator's ✓/✗ arrives through `GET` / `POST /topic-labels` or a rename of an auto-named row.

### `/focus` and `/presence`

- `POST /focus?session=<name>[&target=world|rack]` asks an already-open page to jump to a session. It is a **pass-through to connected clients**: the bridge broadcasts a `command` SSE frame and answers with `delivered`, the SSE client count. `delivered: 0` tells the caller to fall back to a terminal. `target` must default to `world`: older callers omit it.
- `POST /presence` `{"client_id", "kind"?, "world"?, "running"?, "rack_open"?, "rack_attached"?, "rack_focused"?}`: a rack page reporting that it is on screen, which the bridge cannot observe for itself. Served as the top-level `clients` key. Rules (`tldrack/presence.py`): **in memory only**; **expired on read** after a short TTL, because there is no disconnect event; **an unchanged heartbeat is a no-op**, or N tabs would fan a snapshot out N times a beat; capped, with the least recently seen evicted. A malformed field is a 400, never coerced.

### `/notify`: phone push when an agent is blocked and nobody is looking

`tldrack/notify.py` (transport in `tldrack/sinks/`). A timer thread, not a hook hang-off: a stuck agent emits no further events.

- **The policy is the feature, and it is enforced in code** (the module docstring owns all seven properties): edge-triggered with a per-session cooldown, suppressed while a rack page is on screen, a grace window before the first ping, coalescing, a hard daily budget, once per wait and never on done, and optional reviews as a digest at `notify.json`'s `digest_hours`.
- **Messages are pointers, never payloads**: session names and counts. On hosted ntfy the topic name IS the password. Never include an `attention` row's `message`, which is the agent's own text; a test asserts this. The sink emits only view/click URLs, and there is no subscribe half.
- **Off unless a topic is configured**: `<config>/notify.json` or `TLDRACK_NTFY_TOPIC` (env wins). A topic under 24 characters is refused, which is why `tldrack notify --init` exists. `enabled` is tri-state: absent means on.
- `GET /notify` never returns the topic. `POST /notify {"enabled"}`: **`enabled` is the only writable field**; the topic is set by hand, never over a route a page could reach. 409 when nothing is configured.
- `POST /notify/test` bypasses grace, presence and cooldown but still spends a budget unit. `POST /notify/send` `{"title", "body", "tags"?, "open"?}` is a local tool's one-off event; `open` must be a key of `EVENT_DOORS`. A failed send answers `sent: false` at 200.
- The ledger is in memory: a cooldown restored from disk would mute a new alert. Sinks are duck-typed and **must never raise or block**. ntfy.sh has no uptime guarantee: a best-effort ping, never a delivery guarantee.

### Permission ledger

`tldrack/permission_ledger.py`: every `PermissionRequest` becomes a row in `<state>/permission-ledger.jsonl`, resolved from the transcript's `tool_result`. It decides nothing. `GET /permission/ledger`, `GET /permission/stats`, `tldrack permissions`. Summaries are redacted on the way in and the match key is a hash.

## Launch profiles (`tldrack/profiles.py`)

A profile is one agent CLI on one account: `{id, label, kind: claude|codex|antigravity|shell, config_dir, pin_env, args, model_label}`, from `<config>/profiles.json` (tolerant per entry), or DETECTED when that file is absent. The bridge owns the list because the bridge is what spawns; clients name a profile by id (labels such as `ONE` and `TWO` are the operator's). `tldrack hooks --install` (or `profiles --install-hooks <id>`, or `POST /profiles/install-hooks {"profile"}`) merges the relay: timestamped `.bak`, atomic, idempotent, replacing only its own entries. For Claude it also adds `CLAUDE_SETTINGS_ENV` to the settings `env` where the key is unset. Each snapshot profile carries `hooks_missing`.

Nine things that must not be simplified:

1. **`spawn_argv` is the spawn shape for every door**: `/spawn`, `/restore`, worktree `/create`, coordinator resume (plugins reach it as `PluginContext.profiles`). `tmux new-session … ";" set-option -t =S: @vc_profile <id>` is ONE invocation, so no poll sees an unstamped session.
2. **`=S:` with the colon.** `set-option -t` parses a target pane, which takes the exact-match `=` only as `=session:`.
3. **Claude's default config dir is never pinned; the spawn UNSETS `CLAUDE_CONFIG_DIR`.** The default account keeps `.claude.json` in `$HOME`, so pinning `~/.claude` points Claude at state that is not there.
4. **`SHED_ENV` is unset for every Claude spawn.** Each of those variables outranks the config dir's own login, and a tmux session inherits the tmux SERVER's environment, which is usually older than the bridge's.
5. **Codex is spawned as bare `codex`, never a binary path and never under `env`.** It may be a shell function from your shell profile, visible only to the login shell that runs it. **And with `--no-daemon` whenever the CLI offers it**: Codex runs hooks in its app-server, and on the shared daemon none reach this bridge. Owner: `build_inner`.
6. **Detection never names a profile from the bridge's own environment.** A bridge started from an agent's pane carries that agent's `CLAUDE_CONFIG_DIR`. The environment only picks the default. CLIs are probed past PATH (`CLI_PROBE_DIRS`), because a supervisor's PATH may hold none of them.
7. **A transcript beats the stamp, matched by longest root, separator-aware** (`profile_for_path`). The stamp records what was spawned; the shell can run anything after. `.claude` must never claim a file under `.claude-a`.
8. **Antigravity never relays `PreToolUse`**: it reads that hook's empty stdout as a deny. Owned by `tldrack/antigravity_hooks.py` — see the note there.
9. **`resume_args` is the resume fence** (`--resume=<uuid>`, `=`-attached, uuid only) for `/restore` and coordinator resume.

- **Launch models and permission modes**: `/spawn` and worktree `/create` take an optional `model` (`/spawn` also `subagent_model`) and `permission_mode`, validated, stamped on the session and replayed by a restore. Owned by `profiles.py` § LAUNCH MODELS and `PERMISSION_ARGS`.
- **AUTO** (`tldrack/launch_pick.py`): `"profile": "auto"` resolves to the freest account at click time, from usage headroom, the profile's `capacity` and the agents already running there. The id `auto` is reserved.
- **Headless calls** (`claude -p` as a function call, no tmux): `tldrack/headless.py` owns the recipe; `ProfileRegistry.headless_call` and `auth_info` hand it to plugins and to `claude_cli.py`, the agent runtime this bridge registers into the core's built-in `local-llm`.

## Missions, LEAD and the decision inbox

**Missions** (`tldrack/missions.py`; CLI `tldrack mission …` in `mission_cli.py`): agent sessions assigned to one task, sharing notes. `/hook`'s `UserPromptSubmit` answer carries the whole briefing and its `PreToolUse` answer only what changed. The `PreToolUse` path runs on every tool call of every agent, so it stays a dict lookup. Only the `http` relay can answer; on the curl relay a member reads `hears: false`. A standing mission (an area's notebook, never closed) comes only from settings `standing_missions`; none is seeded by default. `GET /missions[?standing=1]`, `POST /missions`.

**LEAD**: a session that runs a planned feature through worker sessions and keeps one decision inbox for the operator. Package map: `tldrack/lead/__init__.py`.

- **One store, one file**: `rack.sqlite` in the `rack` store holds the inbox (`inbox.py`) and the runs (`lead/runs.py`); `rack_db.py` owns why.
- **Answers need the per-launch secret** (`decision_secret.py`): `GET /decision-secret` serves a browser fetch only; `POST /inbox/answer|defer|dismiss` need `X-VC-Decision-Secret` and an allowed Origin. No CLI verb answers.
- **`POST /lead/<verb>`** is one handler (`LEAD_VERBS` in `lead/service.py`); the caller is the CLI's pane (`X-VC-Pane`). `tldrack lead …` (`lead/cli.py`) is the agents' only door. `/spawn` and `/kill-session` share their bodies with it (`spawn_session_now`, `kill_session_now`), so a LEAD close forgets every store a ✕ does.
- **`/hook`**: a lead's `Edit` / `Write` is denied (subagents too), a worker's `AskUserQuestion` becomes a question for its lead, and `UserPromptSubmit` restates either role. Http relay only.
- **Every worker starts in permission mode `auto`** (`profiles.py` `PERMISSION_ARGS` owns what that means per CLI).
- **The watcher types** only through `pane_typist.py`, which owns when typing is safe, the submit confirm, and the `<rack-wake` / `<lead-message` markers the ledger reads as machine-typed.
- **A lead runs one model and everything under it a weaker one**; the pair, its default and `POST /lead/models`: `tldrack/lead/models.py`.
- `lead/brief.py` points a LEAD at this repo's `.claude/skills/lead/SKILL.md` by absolute path; `TLDRACK_LEAD_SKILL` overrides it for an install with no checkout.
- `lead cap <n>` changes a run's live-worker cap; lowering it kills nobody, it only holds new spawns.

Tests: `tests/test_lead_*.py`, `tests/test_inbox*.py`, `tests/test_hook_lead.py`, `tests/test_pane_typist.py` (fakes in `tests/lead_fakes.py`).

## Agent ops and the session ledger

- **Attention metrics** (`rack_metrics.py`): a local SQLite file, `GET /rack-metrics`, `POST /rack-metrics/event|settings`. The bridge writes its own attention signals there through `SignalQueue`, so no hook waits on SQLite.
- **Costs** (`session_costs.py`): `POST /usage` also books each conversation's cost per day; `GET /costs` rolls it up per session, mission and fixer against `TLDRACK_FIX_CAP_USD`.
- **Error fixers** (`error_fix.py`): `POST /errors/fix` takes one reported error row, writes a brief that fences the row as data, and starts a fixer through the `worktree` plugin; 429 past the budget.
- **Decisions** (`decisions.py`): `PostToolUse` is relayed for `AskUserQuestion` ONLY (`CLAUDE_MATCHED_HOOKS`), so each answer is recorded in the `decisions` store. `GET /decisions`, `POST /decisions/import`, `tldrack decisions import|status`. The import reads only the globs in settings `decision_docs`.
- **Session ledger** (`tldrack/ledger/`; why: its `__init__.py`): every agent conversation on this machine, kept past the CLI's own transcript cleanup. The bridge runs `ingest` on a timer in a `nice`d child (`ledger/scheduler.py`). It writes the `ledger` store only; it reads transcripts and rollouts and never changes their writers' retention.
  - `GET /ledger/status`, `POST /ledger/ingest`; `GET /ledger/search`, `/ledger/conversation`, `/ledger/threads` serve indexed text with no-store responses behind the expensive-route guard (`ledger/reader.py`; `tests/test_history_reader.py`).
  - `GET /ledger/rack-search?q=…&s=<session>…` is the rack search box's chat half: which named live sessions said every term, across the ledger and the live transcript tail (`ledger/rack_search.py`).
  - Each ingest links every `laya` verdict to the turn it judged (`ledger/laya_links.py`).
- `record_transition` and the notifier's sends also append to an activity log (`activity_log.py`, pointers only), so dwell time outlives the ring buffer.

## Plugins

The plugin host, the Host and Origin guards, CORS and `/snapshot`'s `bridge` + `plugins` keys are owned by the public core, **`voidcraft-world-bridge`** (https://github.com/voidcraft-world/voidcraft-world-bridge; its own `CLAUDE.md` is the guide). This bridge depends on `voidcraft-world-bridge>=0.1.4,<0.2` and subclasses its `BridgeHandler`. 0.1.4 is the floor because it lets a host pass its own origin set. On 0.x a minor bump may break this host: widen the range on purpose.

- **Plugins are capabilities.** `main.py` asks `capability("laya")` or `capability("worktree")` once per use; a bridge without the plugin has that feature off, and nothing assumes one is mounted. This repo's plugins are under `plugins/<name>/`.
- **Discovery** (`tldrack/plugin_sources.py`): `TLDRACK_PLUGINS` wins over `<config>/plugins.json` (`{"version": 1, "plugins": [dirs]}`); a `plugins.json` that exists wins outright, even an empty list. Neither present: the checkout's default-on plugins (`DEFAULT_PLUGINS`: worktree, agent-canvas, claude-skills, laya, phone-link); an installed wheel has no checkout, so only the core's built-ins (today `local-llm`) load. `tldrack doctor`'s plugins line names the source. A new plugin under `plugins/` gets a default there or joins the off list in `tests/test_plugin_sources.py`. These are this bridge's own names (`PLUGINS_ENV`, `PLUGINS_FILE`), so the core's own plugin variable never reaches it. A configured dir with a built-in's name is skipped.
- **Entry contract**: `<dir>/bridge_plugin.py` defines `PLUGIN_NAME` (lowercase kebab token) and `create_plugin(context)`. The plugin is duck-typed: `name`, `routes()`, `handle_get(subpath, query)`, `handle_post(subpath, query, body)`. Handlers return `(status, dict)` for JSON or `(status, bytes, content_type)` for a raw page. Routes mount under `/plugin/<name>/…`.
- **Host guarantees a plugin cannot weaken**: the Origin guard runs on every plugin POST before dispatch; a plugin that fails to import is logged and skipped; a handler exception becomes a 500.
- **Plugins run inside the bridge venv**: stdlib plus the core. Heavy work shells out to the plugin's own tooling, and every subprocess call carries a timeout, because handlers run on request threads.
- **Two opt-in affordances**, both clamped by the host: `handle_request(method, subpath, query, body, headers)` also receives the request headers (a plugin without it never sees one, because headers carry credentials); `max_body_bytes` raises that plugin's POST cap, never past the core's ceiling.
- `close_plugins` runs at normal exit and at once when status-server startup fails; the freshness watcher stops first.
- **Changing the core**: change and test it in its own repository, on a spare port. To run this bridge against an unreleased change, add a `[tool.uv.sources]` path entry to `pyproject.toml` temporarily and never commit it; `uv pip install -e` does not stick, because `uv run` re-syncs to the lock on every start. Release through the core's `scripts/release.py`, never a hand-pushed tag; with `VOIDCRAFT_BRIDGE_CONSUMERS` naming this directory, the release moves this lock. Restart the bridge to run it.

## Security posture

- **Origins: loopback only by default.** `StatusHandler.ALLOWED_ORIGINS = settings.allowed_origins`, and `main.origin_allowed(origin, allowed=frozenset())` passes an absent Origin (curl, CLI, native clients), any loopback origin (http or https, any port), and the listed origins. No hosted site is allowed unless the operator lists it. Responses echo the caller's exact origin with `Vary: Origin`, **never `*`**; a rejected preflight is a 403.
- **Every route is guarded**, in `do_GET` and `do_POST`, ahead of the route; a route that acts on the machine or writes to disk re-checks with `_origin_allowed`. Do not exempt a route because it is read-only or its payload looks like telemetry: what matters is what the handler does with it. `/hook` opens a `transcript_path`, and a cross-origin form POST is a navigation with no preflight. (Read routes guarded since 2026-07-31, write routes since 2026-08-01.)
- **DNS rebinding is the attack a loopback bind does not stop; the `Host` header is the only thing that does.** A page on an attacker's hostname re-resolved to `127.0.0.1` makes a genuine same-origin connection, so every Origin check passes by construction. `host_allowed()` runs first and 403s a request addressed to a name the bridge does not answer to. Default: loopback names only; an absent `Host` passes (a browser never omits it).
- **Never widen the host list to a `*.ts.net` suffix.** Tailscale funnel publishes such names to the public internet. The page pins named machines for the same reason (`ui/src/agent-rack/rack-remote.ts`, `VITE_ALLOWED_REMOTE_HOSTS`); the two must stay consistent.
- **An absent `Origin` is not proof of a native client.** A no-cors browser GET (`<img>`, `<script src>`, an iframe) sends none either. The discriminator is `Sec-Fetch-Site`, which browsers send and CLIs do not. `_expensive_route_guard` applies it where the risk is the work performed: `/wait` (a held thread), `/events` (an SSE slot), `/capture` (a tmux shell-out) and the ledger reads. It is left off the merely readable routes on purpose: a plugin page in an iframe and a hand-typed URL both arrive with no Origin.
- **The bridge's own page names no `Origin` on a read.** A same-origin GET carries only `Sec-Fetch-Site: same-origin`, so the handler states the origin before any guard or plugin policy reads the request: owned by `tldrack/own_page.py` — see the note there.
- **The terminal port is loopback and `-W` makes the terminal writable.** Any local process can reach a shell at the terminal port while the bridge runs. That is the accepted trade-off; `--read-only` gives a view-only terminal. The URL arg is passed as argv to the `sh` wrapper, never interpreted as script text, and only selects a session name.
- **`-O` (`--check-origin`) must stay.** Without it any page the operator has open can open the ttyd WebSocket and write to the tty: drive-by RCE. CORS does not prevent this, because the same-origin policy never covered the WebSocket handshake. `-O` accepts only `Origin == Host`, so the page ttyd itself serves passes, and every other origin, and an absent Origin, is refused.
- **`GET /ws?arg=<name>` fronts ttyd's WebSocket** (`ws_proxy.py`) for a page on an allowed origin that is not ttyd's own. Upgrade only: a plain GET is 400, an unreachable ttyd is 502. After the Host and Origin guards it dials the ttyd port, rewrites `Host` and `Origin` to the upstream's own so `-O` passes, and relays bytes blind. Capped at `MAX_PROXY_SOCKETS`, and advertised as `bridge.ws_proxy`, because a browser gets no status code from a refused WebSocket. It is the only door to a shell that checks WHICH origin is asking. A capability that fails only at the socket layer needs a test (`tests/test_ws_proxy.py`): a green status port says nothing about a dead terminal.
- **ttyd does not listen on the network.** It cannot be told which names it answers to, and under rebinding `Origin` and `Host` are both the attacker's hostname, so `-O` alone accepted that handshake. ttyd now serves a Unix socket in a directory only this account can enter, and the terminal port is `ttyd_front.TtydFront` in the bridge's own process: it reads the first request head of each connection, refuses a `Host` that `host_allowed()` does not answer to (and an absent one), then relays bytes to the socket. A WebSocket is relayed blind after that check; any other request is one request per connection (`Connection: close` both ways) whose response head gets the frame policy, so the Host check reads every plain request. The terminal address does not change. `--no-status` execs ttyd alone, so there it keeps the loopback port with no `Host` check. **Do not drop `-O`**: it still stops the cross-origin handshake, and it still sees the browser's real headers.
- **Remote access goes through `tailscale serve`**: the proxy runs on this machine and dials `127.0.0.1`, so the bind and `-O` stay as they are.
  ```bash
  tailscale serve --bg --https=443  http://127.0.0.1:7681   # ttyd
  tailscale serve --bg --https=8443 http://127.0.0.1:7682   # status listener
  export VOIDCRAFT_BRIDGE_ALLOWED_HOSTS=mymac.tailXXXX.ts.net
  ```
  The proxy forwards the original `Host`, so the machine must be named (comma-separated; a full URL is reduced to its hostname). A 403 `host not allowed` on the remote path means the variable is unset. The remote page's origin (the tailnet `https://…` origin) is not loopback, so it must be listed in settings `allowed_origins`. **Never `tailscale funnel`** and **never a `0.0.0.0` bind.** If `-O` refuses on a new tailnet, fix the proxy's Host forwarding; never drop `-O`.
- **No other site frames a bridge page** (`tldrack/framing.py` owns why). The status listener sends `Content-Security-Policy: frame-ancestors` on every response (`StatusHandler.end_headers`), plugin pages included; the terminal front stamps the same policy on every plain ttyd response, and a response it cannot stamp is a 502, never an unstamped page. The policy names loopback (any port) and `allowed_origins`: a page may frame the bridge when it may call it.
- **The injected script takes posts only from the frame that holds it, on loopback or an allowed origin** (`trusted_parents`, baked into the patched index, so a new origin rebuilds the cache). That covers what `frame-ancestors` cannot: a hostile frame beside the terminal on a page that frames it legitimately. The config message carries only a clamped font size, and a soft key is a token from a closed table that has no Enter and nothing printable (`index_patch.py` owns why). Keystrokes never leave the page.

## Page contract

- The rack page appends the **full render query** to the ttyd URL: `fontSize`, `fontFamily`, `fontWeight`, `rendererType` and a JSON `theme`, built by `render_query()` in `ui/src/engine/terminal/terminal-render-options.ts`. The `-t` flags here are the defaults for a **plain browser visit** only: ttyd merges query options last, so the URL always wins and none of this needs a bridge restart.
- ttyd coerces a query option by the default's type: numbers go through `parseInt`, so a fractional `lineHeight` cannot use this path, and an object option (`theme`) is **merged**, so a partial theme keeps the bridge's value for every key it omits.
- The parent posts `{type:'voidcraft-terminal-config', font_size}` (device px) into the frame on every load and font change; the patched page resizes xterm in place with the socket intact. ⌘/Ctrl + = / − / 0 inside the terminal post `{type:'voidcraft-terminal-font-step', step}` to the parent, which clamps, persists and pushes the new size back (`ui/src/composables/useTtydFontStep.ts`). The remote rack's key bar posts `{type:'voidcraft-terminal-key', key: <token>}`. A raw ttyd ignores all three.
- `DARK_THEME` in `main.py` is **not** the source of truth for terminal colours: `TERMINAL_THEME` in `terminal-render-options.ts` is, and it reaches ttyd on the URL. Keep the four keys here tracking its background, foreground, cursor and selection, and do not copy its ANSI colours.
