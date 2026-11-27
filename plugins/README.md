# plugins/

Capabilities the rack mounts on the bridge's status port, kept out of the bridge itself. Each directory
is a self-contained package with its own `pyproject.toml` and tests (`uv run pytest` inside it). The
bridge loads the directories listed in `~/.config/tldrack/plugins.json`
(`{"version": 1, "plugins": ["/abs/dir", …]}`) and advertises each mounted plugin on `GET /snapshot`
under `plugins`.

| Plugin | Does | Default |
|---|---|---|
| [`worktree/`](worktree/) | Worktree-backed agent sessions, the PR button, the branch harbor, the `wt` command | on |
| [`agent-canvas/`](agent-canvas/) | A small pane per agent: typed docs, diagrams, one-click answers typed back | on |
| [`claude-skills/`](claude-skills/) | Every Claude Code skill on this machine; `/run` starts an allow-listed one | on, `/run` empty |
| [`coordinator/`](coordinator/) | Audits local Claude Code sessions for unfinished work | off |
| [`devices/`](devices/) | CPU and memory of this machine and each compute host | off |
| [`laya/`](laya/) | Laya, the local model that decides when a finished turn needs the operator | on |
| [`phone-link/`](phone-link/) | A token-gated listener the Android review app reaches over `adb reverse` | on |

A plugin on that list can be switched off by name on the rack's Settings page, and is then not mounted
at the next start. The switch only subtracts from the list: owned by `bridge/tldrack/features.py` — see
the note there.

## The contract

A plugin is a directory holding `bridge_plugin.py`:

```python
PLUGIN_NAME = "my-plugin"  # lowercase kebab token, ≤ 32 characters


def create_plugin(context):  # the host's PluginContext
    return MyPlugin(context)
```

The returned object is duck-typed; the core's `voidcraft_world_bridge/plugins.py` owns the host side.

| Member | Contract |
|---|---|
| `name` | equals `PLUGIN_NAME` |
| `routes()` | route strings, advertised on `/snapshot` |
| `handle_get(subpath, query)` | `subpath` like `"/state"`, `query` from `parse_qs` |
| `handle_post(subpath, query, body)` | `body` is a parsed JSON object or `None` |
| `busy_reasons()` | optional: short strings for in-process jobs a restart would kill; lock-and-read only, no I/O |
| `handle_request(method, subpath, query, body, headers)` | optional: called INSTEAD of the pair, header names lowercased, for a plugin that must read a header |
| `pane_buttons()` | optional: `[{"label", "title", "route"}]`, buttons drawn in every terminal pane after + IMAGE. A press POSTs `{"session"}` to `route` (one of the plugin's own) and the answer's `paste` lands in the draft, never sent. Owned by `bridge/tldrack/pane_buttons.py` — see the note there |

A handler returns `(status, dict)` for JSON or `(status, bytes, content_type)` for a page or an asset.
Routes mount at `http://127.0.0.1:<status_port>/plugin/<name>/…`.

The host enforces, and a plugin cannot weaken: the Host and Origin guards on every request (a plugin may
narrow them through `handle_request`), failure isolation (an import error skips the plugin, a handler
exception is a 500), and first-configured-wins on a duplicate name.

A plugin follows: **stdlib plus the public core** (`voidcraft-world-bridge>=0.1.4,<0.2`, declared in its
`pyproject.toml` when it imports it); **never block long** (every subprocess carries a timeout); **additive
JSON** (a response only gains keys).

## What the context carries

The core guarantees `status_port` and `log`. The rest reads empty on a host that lacks it, so a plugin
reads each with `getattr(context, "<name>", None)` and keeps a default; the tests' fake contexts carry
none of them.

| Attribute | What | Absent means |
|---|---|---|
| `port`, `default_session`, `start_dir` | the terminal server's port, its default session, the bridge's start directory | no terminals; the working directory |
| `profiles` | the launch-profile registry. A plugin that starts an agent spawns through `spawn_argv`; one that asks a model headless builds the call with `headless_call` | bare `claude` |
| `plugin_settings(name)` | the `plugins.<name>` object of the operator's `settings.json`, a mapping; `{}` when unset | every setting at its default |
| `allowed_origins` | the browser origins the operator configured for the bridge, loopback not included | loopback only |

**Settings are the operator's, and every default is the safe one.** A plugin reads its own object once, at
`create_plugin`, validates each key it knows, ignores the rest, and says nothing when the object is
missing. A key is documented in the plugin's own README, in its § Settings, and nowhere else.

```json
{
  "plugins": {
    "worktree": {"repos": ["~/code/my-app", "~/code/tldrack"]},
    "claude-skills": {"run": {}},
    "coordinator": {"enabled": true},
    "devices": {"enabled": true}
  }
}
```

Origin lists stay per plugin. A plugin that checks a caller or a framing page builds its set from
`allowed_origins` plus loopback, through the core's `CallerPolicy`, and never names a hosted site of its
own.
