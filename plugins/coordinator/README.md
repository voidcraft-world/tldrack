# coordinator/

A tldrack plugin that answers one question: **which of my Claude Code sessions did I leave
unfinished?** It reads the transcripts Claude Code keeps on this machine and lists the loose
ends: a session blocked at a permission prompt right now, a plan that was rejected and never
replaced, a `Next:` step nobody took, a closing offer nobody accepted. A RESUME button reopens
the conversation in a tmux session the rack picks up.

On the side it serves **who is running right now** (`GET /live`): every Claude process on this
machine, including ones started outside tmux, which the bridge's own `/snapshot` cannot see.

**Off by default.** Every route reads your own transcripts, so the plugin does nothing until you
turn it on (§ Settings). Everything stays local: no transcript leaves the machine. The one
exception is an explicit deep audit, which hands a compact list of one-line headlines to a local
model (Laya, when you have trained it for this) or to a headless `claude -p` call.

## Settings

The `plugins.coordinator` object of the operator's `settings.json` (where that file lives, and how
plugins read it: `plugins/README.md`). Read once, when the bridge loads the plugin; restart the
bridge after a change.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | `true` turns the plugin on. Only a JSON `true` counts: any other value is off, and a non-boolean is reported in the bridge log |

```json
{"plugins": {"coordinator": {"enabled": true}}}
```

**Off** means nothing runs: no store read, no thread, no transcript or index scan, at load or on
request. The plugin stays loaded (so the bridge does not log a load failure), advertises no routes
on `/snapshot`, logs `coordinator: off (set plugins.coordinator.enabled in settings.json)` once at
load, and answers every request with

```json
404 {"error": "off", "enabled": false, "plugin": "coordinator", "hint": "set plugins.coordinator.enabled in settings.json"}
```

That is the same status a bridge without the plugin answers, so a client needs no second case: the
rack shows no COORDINATOR panel and no live-agent feed.

### Environment

Three environment variables, for the bridge process:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where `sessions/` and `projects/` live. Each Claude launch profile's own directory is read too |
| `TLDRACK_COORDINATOR_PATH` | `~/.local/state/tldrack/coordinator-findings.json` | the store: the last scan and the dismissals |
| `COORDINATOR_CLAUDE_BIN` | `claude` on `PATH`, then `~/.local/bin/claude` | the CLI the deep pass and `/resume` run |

## Routes

Mounted at `http://127.0.0.1:<status-port>/plugin/coordinator/…`.

| Method | Sub-path | Body / query | Returns |
|---|---|---|---|
| GET | `/state` | — | `CoordinatorState`. **Cheap**: reads the store, scans nothing |
| GET | `/live` | — | `{agents, count}`: every Claude process running now, pid-checked, `cwd` `~`-collapsed. Read fresh on every GET |
| POST | `/audit` | `{"deep": bool, "scan_limit": int, "max_age_days": int, "session_id": str}`, all optional; query parameters win | `CoordinatorState` with `status: "scanning"`, at once |
| POST | `/dismiss` | `{"key": "<finding key>"}` | `CoordinatorState` |
| POST | `/restore` | `{}`: clears every dismissal | `CoordinatorState` |
| POST | `/resume` | `{"key": "<finding key>"}` | `CoordinatorState` **plus** a top-level `resumed` |

`POST /audit` never blocks: the scan runs on a background thread and the route answers straight
away. Poll `/state` until `status` is `"idle"` again. A flag in the query string needs a value
(`?deep=1`), because the host drops a bare `?deep`.

```jsonc
// CoordinatorState (values invented)
{
  "status": "idle" | "scanning",
  "scanned_at": "2030-01-02T03:04:05Z" | null,
  "scanned": 42,          // transcripts examined in the last run
  "ranked": false,        // true once the deep pass has classified the findings
  "ranked_by": null | "laya" | "claude",
  "dismissed": 3,         // how many of this scan's findings are hidden
  "error": null | "human-readable string",
  "findings": [ /* sorted: severity ascending, then last_active descending */ ]
}
```

Each finding carries `key`, `session_id`, `label`, `cwd`, `project`, `branch`, `last_active`,
`age_hours`, `kind`, `severity`, `headline`, `detail`, `live_session` and `work_class`, and
`laya_p` when Laya ranked it. Responses only ever gain keys.

### `POST /resume`: reopen the conversation

```jsonc
{ /* ...CoordinatorState... */
  "resumed": { "key": "1f2e3d4c:open-offer",
               "session": "resume-fix-login-flow-1f2e3d4c",
               "created": true }   // false: the session was already open
}
```

The input is the finding **key**, never a session id. The plugin resolves the key against the last
scan itself, so a session id never crosses the wire and only a conversation the audit surfaced can
be resumed. It then runs `claude --resume=<session_id>` in a detached tmux session, under the
Claude account whose directory holds the transcript. Refusals: `400` an unknown or malformed key,
or a session id that is missing or not uuid-shaped; `409` the conversation is still open in a
running Claude process; `503` no tmux or no `claude` binary; `500` the spawn failed.

Three properties shape this route:

- **A session id is a file name, not a validated uuid.** It is whatever someone named a
  `~/.claude/projects/<slug>/*.jsonl` file, so it is guarded twice: `SESSION_ID_RE` in
  `decide_resume`, and the attached `--resume=<id>` form in the argv. `shlex.quote` alone is not
  enough: `-` is in its safe set, so a transcript named `--dangerously-skip-permissions.jsonl`
  would pass through unquoted and reach `claude` as a flag.
- **Resume in place, not `--fork-session`.** New turns append to the original transcript, so the
  finding stops matching on the next audit and the list clears itself. A fork would leave the
  parked transcript parked forever.
- **Idempotent.** The session name is a pure function of the finding
  (`resume-<label slug>-<session prefix>`), so a second RESUME finds the session already open and
  answers `created: false` instead of starting a second agent on one transcript. The probe and the
  spawn run under one lock, and tmux's own `duplicate session` refusal, the cross-process
  backstop, is reported as `created: false`, never as a `500`.

It is the one route here that acts on the machine. It inherits the host's Origin guard rather than
carrying a weaker check of its own.

### `GET /live`: who is running right now

```jsonc
// values invented
{ "agents": [ { "pid": 4321, "session_id": "12345678-1234-4321-8765-123456789abc",
                "cwd": "~/code/my-app" | null,
                "status": "busy" | "shell" | "idle" | "waiting", "name": "…" | null,
                "kind": "interactive" | null,
                "updated_at": 1900000000 | null } ],   // unix seconds
  "count": 1 }
```

The live-session index, `~/.claude/sessions/*.json`, served as rows a client can join on: the rack
uses it to place agents on branches even when they run outside tmux. Rows are pid-checked with
`kill(pid, 0)`, because a crashed session leaves its entry behind forever. An unknown `status`
reads as `idle`, and `updated_at` accepts the index's epoch milliseconds or an ISO string and
always answers in seconds. Only these seven keys leave the file, and every `cwd` is `~`-collapsed:
the host's Origin guard covers POST only, so what a GET returns is the boundary.

### Severity ladder

| Severity | Kind | Meaning |
|---|---|---|
| 0 | `blocked` | a live session is sitting at `status: "waiting"` **right now** |
| 1 | `rejected-plan`, `dismissed-question` | a structured prompt was refused near the end of the session |
| 2 | `stated-next-step` classified `open-work` | Claude named a next step and it looks like real unfinished work |
| 3 | `open-offer` | the closing message offered more work ("want me to…", "say the word") |
| 4 | `unactioned-recommendation`, and `stated-next-step` that is `routine` or unclassified | `/code-review`, deploy, "verify visually": the reflexive hand-offs |

## The deep pass (`deep: true`)

The heuristics find the loose ends; the deep pass sorts them. It answers two questions a phrase list
cannot:

1. **`open-work` or `routine`?** Most `Next:` clauses are reflexes the developer performs without
   being reminded ("verify visually", "restart tldrack", "run /code-review", "deploy"). Separating
   out the ones that name real work is the whole value of the pass.
2. **Is this a repeat?** One reflex can close several sessions in a row; it should be one row.

It asks **Laya** first, through the bridge's own `laya` plugin, but only once Laya has a fine-tune
registered for this surface: the base checkpoint is not trained on these judgements. Otherwise it
makes **one** batched `claude -p` call over the whole candidate list (never one per finding, never a
transcript), built by the host's headless-call seam: no tools, no MCP, a scratch working directory
so no project `CLAUDE.md` is loaded, a small model, a 90 s timeout and a spending cap. `ranked_by`
says which one sorted the list.

The headlines are text from your own transcripts, so they are untrusted input. Every field of the
answer is checked against a closed vocabulary and the candidate keys, and a collapse (the one thing
that hides a row) must also pass a word-overlap check that involves no model output. **The
heuristic layer stands alone**: when neither model answers, the findings are served unranked, with
the reason in `error`.

## Why dismissals are not polish

An audit of a busy machine surfaces dozens of items, most of which you have already seen and
decided about. Without a durable dismissal set the audit re-reports the same list forever and the
panel gets ignored. Dismissals are persisted beside the findings and applied on the way *out* of the
store, so dismissing takes effect without a re-scan.

## Limits

Every transcript touched inside the 21-day window is audited (`max_age_days`). File mtime is a safe
pre-filter, because a touch can only make a file look fresher, so an older file is skipped unopened.
The walk is capped at 400 files as a backstop against unbounded growth (`scan_limit`), and each read
is a bounded 512 KB tail, so a file costs the same to scan however large it has grown. Findings
older than the window are dropped.

## What the detection relies on

Properties of Claude Code's transcripts and session index that the code is built around. Each has a
test.

- **A tool call always gets a result.** An `AskUserQuestion` or `ExitPlanMode` call is never left
  dangling, so a refusal is detected by the result's *content*.
- **`away_summary` is the richest source.** Most long sessions have one, and most of those name a
  next step.
- **Phrases are chosen, not guessed.** `should I`, `shall I` and `would you prefer` read like
  obvious soft asks but do not show up in closing hand-offs, so they are left out.
- **mtime lies, in one direction.** Anything that bulk-touches the directory moves it by hours or
  days, always fresher. It is a pre-filter only; "untouched since the cutoff" is sound and "mtime
  rank > N" is not. `last_active` always comes from a record timestamp.
- **Many records carry no timestamp** and are often the last line, so the reader scans backward
  for the last real one.
- **`tail -n` is unbounded**: one record can hold a whole file. The reader takes a fixed byte tail.
- **`/exit` is not resolution.** A session that ends with one can still close on an offer nobody
  accepted.
- **A record is not a turn.** Every tool call writes an assistant record plus a user record
  carrying the `tool_result`, so counting records makes the agent's own loop look like the user
  steering. The recency gate on refused plans and questions counts real prompts and assistant
  messages only.

## Layout

| File | Role |
|---|---|
| `coordinator/sources.py` | the two sources, the one function that walks them, the pure `/live` rows |
| `coordinator/tail.py` | bounded byte tail, partial-line discard, backward timestamp scan |
| `coordinator/signals.py` | **pure**: parsed records in, findings out. The tested core |
| `coordinator/rank.py` | the deep pass's `claude -p` call, and folding any ranking into the findings |
| `coordinator/laya_rank.py` | the deep pass asked of Laya instead |
| `coordinator/resume.py` | **pure** argv, session name and refusal table, plus the tmux spawn |
| `coordinator/claude_cli.py` | finding the `claude` binary |
| `coordinator/store.py` | durable findings and dismissals |
| `coordinator/plugin.py` | the settings and the six routes |

`signals.py` never touches the filesystem, the clock or a subprocess. That is why the detection is
provable against a handful of hand-built records, and it is the seam to keep.

## Enable it

1. Add this directory's absolute path to the bridge's `plugins.json` (or run the bridge with
   `TLDRACK_PLUGINS="$PWD/plugins/coordinator"`).
2. Set `plugins.coordinator.enabled` to `true` (§ Settings).
3. Restart the bridge.

## Tests

```bash
uv run pytest          # from plugins/coordinator/
```

`tests/test_signals.py` is the important one: every signal has a hit case *and* its near-miss (an
approved plan, an answered question, "nothing pending", a soft ask in a non-final message, a
refusal 100 turns before the end). Every fixture value is invented.
