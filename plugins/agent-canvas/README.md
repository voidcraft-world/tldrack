# agent-canvas

A tldrack plugin that gives every agent in the rack a small pane beside its terminal. The agent
writes one file, a typed JSON doc or body markup; the rack draws it. A question on the canvas is
answered with a click, and the answer is TYPED into the agent's prompt as one line.

```
GET  /plugin/agent-canvas/health                   → {ok, plugin, canvas_dir}
GET  /plugin/agent-canvas/session-start?session=<name> → what the SessionStart hook prints (text/plain)
GET  /plugin/agent-canvas/page?session=<name>      → the page the rack frames (text/html)
GET  /plugin/agent-canvas/frag?session=<name>      → {key, rev, kind, html | doc+errors+questions, question, answered, queued, truncated, view, artifacts}
GET  /plugin/agent-canvas/check?key=<key>          → {ok, kind, drawn, errors, questions}; &format=text for the issues as text
POST /plugin/agent-canvas/check  <PostToolUse stdin>  → those issues for the canvas just written, or nothing (native callers)
GET  /plugin/agent-canvas/draw?session=<name>      → {key, paste}: the terminal's ◧ DRAW button
POST /plugin/agent-canvas/view {session, view: canvas|skills|toggle} → {session, view}: the pane's face
POST /plugin/agent-canvas/nudge?session=<name> <PostToolUse stdin> → a canvas reminder, or nothing (native callers)
POST /plugin/agent-canvas/reply {session, rev?, answers: {id: value}} → 200 delivered · 202 queued
POST /plugin/agent-canvas/reply {session, rev?, answer, question?}    → the same, one value
POST /plugin/agent-canvas/artifacts/register {session, path} → saves a file on the session's shelf (native callers)
GET  /plugin/agent-canvas/artifact?key=<key>&id=<id> → a sandboxed viewer for one saved file
GET  /plugin/agent-canvas/mermaid.js               → the vendored Mermaid bundle
```

Ideas taken from [Lavish](https://github.com/kunchenguid/lavish-axi) (agent-written HTML, reviewed
and answered in a browser) and [OpenUI](https://github.com/thesysdev/openui) (a registered component
library the model is told about and the runtime validates). No code or dependency from either.

## Enabling

1. Add this directory to `~/.config/tldrack/plugins.json` and restart the bridge.
2. Install the three hooks (§ Hooks). Without them the pane works, but no agent learns where its
   canvas is.
3. Link tldrack's skills into your account (`plugins/claude-skills/scripts/link-skills`), so the
   `agent-canvas` skill, the block reference an agent reads before drawing, loads in every repository.

Plugins run inside the bridge's Python with no install step, so this one is stdlib only.

## Hooks

`hooks/` holds three Claude Code hooks. Each is a short `sh` relay to this plugin over loopback;
every rule lives in the plugin. They need `curl`, and `tmux` for the two that name the session; each
reads the bridge's status port from `TLDRACK_STATUS_PORT` (default 7682) and stays silent when the
bridge does not answer.

| Hook | Event | Does |
|---|---|---|
| `session-start.sh` | `SessionStart` | Prints the session's canvas paths and how to draw (`session_start.py`) |
| `check.sh` | `PostToolUse`, `Write\|Edit` | After a write to a typed canvas, prints what was cut and exits 2, so the agent reads it |
| `nudge.sh` | `PostToolUse`, `Write` | Registers a written `.html` on the shelf, and reminds the agent of the canvas when it creates structure (`nudge.py`) |

In Claude Code's `~/.claude/settings.json` (not the bridge's), with `<dir>` this directory's absolute path:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "<dir>/hooks/session-start.sh"}]}],
    "PostToolUse": [
      {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "<dir>/hooks/check.sh"}]},
      {"matcher": "Write", "hooks": [{"type": "command", "command": "<dir>/hooks/nudge.sh"}]}
    ]
  }
}
```

## Settings

No keys of its own. It reads two things the bridge provides:

- **`allowed_origins`**: the browser origins in the bridge's own `settings.json`
  (`~/.config/tldrack/settings.json`, top-level `allowed_origins`). Pages at these origins may frame
  the canvas, besides any loopback page (§ Security). Unset means loopback only.
- **The canvas directory**: `$TLDRACK_DATA_DIR/canvas`, which the bridge exports, else
  `~/.local/share/tldrack/canvas`. `AGENT_CANVAS_DIR` overrides it. It is created mode `0700`.
  `paths.py` owns why it lives there.

## The rack strip

The Canvas pane puts the 3D office first. A row beneath it shows the focused agent's lot-map on the
left and artifacts on the right when both exist; either uses the row alone otherwise. The map starts
with 75% of the available width. Drag the vertical divider to change that share; the choice persists
on this device. Double-click or Home resets it to 75%; Left/Right adjusts the focused divider.
`ui/src/agent-rack/CanvasDetailsRow.vue` owns that split, and both resize directions share pointer
capture through `useCanvasPointerDrag.ts`.
Decision and next-step cards follow, then the Canvas document. An empty document takes no space.
MAP hides only the map; UNDER / COLUMN / OFF controls placement. Scrollbars stay hidden.

The handle below the office, compact row and document resizes that section's height. Heights persist
on this device across session changes and reloads. Double-click or Home resets a section; Up/Down
adjusts the focused handle. `ui/src/agent-rack/CanvasResizableSection.vue` owns the gesture and storage;
`canvas-sections.ts` owns the limits. The office starts larger; the map row defaults to 280–420px as
the column widens so its SVG can use the extra width. Existing saved heights still win.

The office draws the rack's current sessions and sends robot selections through that rack's transport.
It pauses rendering outside the visible strip or in a hidden tab. Mouse drag orbits; the wheel,
trackpad pinch and two-finger touch pinch zoom inside the 3D view. FIT and the zoom buttons remain
available. In the embedded view a one-finger swipe scrolls the column; a pinch never selects an
agent when its fingers lift. The separate OFFICE window keeps one-finger orbit and its activity-map
overlay. Both use `ui/src/agent-office/AgentOfficeView.vue`; `office-pointer.ts` classifies taps and swipes.

Artifacts reach `ui/src/agent-rack/AgentArtifactShelf.vue` through the existing canvas hello, with no
second poller. The rack validates the iframe source, origin, session and bounded shelf metadata
before acknowledging ownership. `assets/artifacts.js` keeps the in-frame fallback for older hosts
and direct canvas pages, before their document. Invalid metadata leaves that fallback visible.
Previews use a full-window dialog and the existing sandboxed artifact viewer; raw files never gain
the rack's origin. Switching sessions closes a preview. Native scroll chaining returns from the
document to the strip at its edges. A missing Canvas document does not hide the map or office.

## The canvas loop

1. **SessionStart.** `hooks/session-start.sh` asks `GET /session-start` and prints the answer: the
   agent's two canvas paths, `<canvas dir>/<key>.json` and `<key>.html`, and when to draw.
2. **The agent writes one file** with its ordinary Write tool. Whichever form it wrote last is shown.
   - `<key>.json`, a **typed doc**: blocks from the registry in `schemas/src/agent-canvas.ts`, the
     owner. `assets/canvas-schema.json` is its generated twin (§ The schema twin). `doc.py`
     validates a doc against it, cuts each bad block (the deepest one holding an issue) and keeps
     the rest. `assets/kit.js` builds the doc from text nodes only.
   - `<key>.html`, **body markup** against the kit's classes (`assets/kit.css`).
3. **Issues go back to the agent.** `hooks/check.sh` relays every write to a key-shaped `.json` to
   `POST /check`. When it was this session's typed canvas, the agent reads each issue: the path, the
   code, and what the key should have been. The page shows the same list.
4. **The rack frames `/page`** for the focused session. The page polls `/frag` once a second and
   swaps the fragment in place, so scroll survives.
5. **A question** is a `choice` or `form` block (several per doc), or a markup `.choice` holding
   `data-reply` buttons (one). When the agent's turn ends with one unanswered, the watcher raises
   the session to `attention` through the bridge's own `POST /status`, so every rack surface that
   follows `attention` shows it with no code of its own.
6. **Answers** are one `POST /reply`, typed into the pane as ONE line plus Enter:
   `canvas › <q> → <a> · <q2> → <a2>` (a form as compact JSON, a blank question as `(skipped)`).
   `desk.py` owns why it is one line. A lone single-pick `choice` sends on click; anything more
   collects answers until the page's SEND bar. The server checks every answer against its question
   (an option, a field's options, a reordering of a rank) before typing anything.

## Getting agents to draw at all

Telling an agent once, at SessionStart, is not enough: by the turn where a picture would help, the
line is far behind it. Two more doors carry the canvas into that turn:

- **◧ DRAW**, on demand: a button on the terminal page pastes a request naming the session's exact
  canvas path into the draft, without Enter. Owner: `agent_canvas/draw.py`.
- **The nudge**, automatic: `hooks/nudge.sh` relays each Write that CREATED a file. When it is the
  first file in a new directory, a launchd job or systemd unit, a rack plugin's entry or a route
  module, the agent gets a one-line reminder as `additionalContext`, with no extra turn. At most once
  per 20 minutes, and never right after it drew. Owner: `agent_canvas/nudge.py`.

## Two faces: the canvas, and ◇ SKILLS

The pane shows one of two faces per session: the agent's canvas, or the **skills audit**, every skill
an agent on this machine can call (project, global, synced, each plugin's, built-in) as a chevron
tree with name, age and size on every row. A skill's files and its SKILL.md load when its row opens.

- **The desk holds the face** (`desk.py` `set_view`, in memory). The terminal's ◇ SKILLS button lives
  on the terminal page, a cross-origin sibling of this one, so the bridge is the only thing both can
  reach. `/frag` carries `view`, and the page follows it.
- **The page's own chips flip it too**: `◇ SKILLS` on the canvas, `◂ CANVAS` on the audit. The chip
  turns `●` when the canvas changed or the agent asked something meanwhile. A new canvas never takes
  the audit away, and is drawn only once its face shows, because Mermaid cannot lay out a hidden
  diagram.
- **The data is the claude-skills plugin's** (`GET /state`, `GET /skill`, `POST /open` for ⌖ REVEAL),
  read same-origin and built by `assets/skills.js`. Without that plugin the face says so in one line.

## Artifact shelf

Generated deliverables (HTML, Markdown, text, JSON, CSV, PNG, JPEG, WebP, PDF) appear below the
canvas, independent of its revision. The shelf collapses when empty and takes at most 40% of the
pane. Preview stays in the pane, Open uses a new tab, the viewer offers Download, and a new file
never moves terminal focus.

An agent registers a file with `python3 <dir>/scripts/register-artifact.py <path>` (the current tmux
pane, or `--session <name>`); the SessionStart text gives it the absolute path. A Write of an `.html`
file registers itself through the nudge hook.

`artifacts.py` is the storage boundary: the session's starting workspace only, regular files, no
hidden path parts, no symlinks, 8 MiB a file, 32 files a session. Copies and their index live in
`<canvas dir>/artifacts/<key>/`. Registering the same relative path again replaces it; unchanged
bytes are a no-op. A deleted source, a bridge restart or a renamed session keeps the copy, and
nothing deletes copies automatically.

`GET /artifact` serves a trusted viewer, never the raw file. `artifact_page.py` and
`assets/artifact-viewer.js` own the isolation: HTML runs in an opaque-origin `allow-scripts` sandbox
whose CSP blocks network requests, external assets, forms and nested frames; it cannot navigate the
parent, open popups or download. A page that exports a picture hands it to the viewer, which saves
PNG, JPEG and WebP only, after a click. PDFs are download-only. Registration refuses every browser caller.

## Why the key is the tmux session ID

`paths.py` owns it: a key that survives a rename and a tmux server restart. The rack addresses
sessions by name, so every route resolves name → key with one `tmux display-message`.

## When an answer is typed, and when it waits

`delivery.py` owns the gate and its reasons. An answer is QUEUED, never dropped and never typed
blind, while the agent is working, while a permission dialog or a terminal question is up (Enter
would confirm it), while a draft sits in the input box, or when the pane holds no Claude prompt.
The watcher (`desk.py`, every 1.5 s) retries until it is safe; an answer waits at most 10 minutes,
and a second click replaces a queued one.

## Security

- **Agent markup cannot run script.** The page's CSP admits only the kit's nonced script (`page.py`
  owns why: a script on the bridge's origin can call every bridge route).
- **A typed doc carries no markup.** `kit.js` builds it with `textContent`; a link must be http(s),
  checked by the schema and again at build.
- **No remote fetches.** Images are `self` or `data:` only. Mermaid is vendored and served by this
  plugin (`vendor.py` owns why, and checks the bundle's hash before serving it).
- **The page goes inert under any other parent.** It types into an agent, so a site framing it under
  a decoy could borrow the click. The kit checks `location.ancestorOrigins` against loopback and the
  operator's `allowed_origins` (`page.py` `framer_origins` owns why), and posts to the rack only at
  that origin, never `'*'`. The tldrack host refuses such a parent outright as well
  (`frame-ancestors` on every response); the kit's check holds under a host that does not.
- **`/reply` is narrowed to the bridge's own origin** (`plugin.py` `same_origin`), on top of the
  host's guards: it types into an agent, and no other page on this machine may.
- **The hook routes take native callers only.** A symlinked canvas is refused, keys are digits and
  one dash, and reads stop at 256 KiB.

## Known limits

- **Over a tailnet** the page renders but answers are refused: the host's Origin guard does not
  admit that origin. The page says to answer in the terminal; ◧ DRAW says the same beside the button.
- **State is in memory.** A bridge restart forgets queued answers (`busy_reasons` names them first).
- **One markup question per canvas.** A typed doc asks as many as it likes; the server tracks one
  answer set per canvas revision.

## Mermaid

`assets/vendor/` holds Mermaid 12.2.0's browser bundle, gzipped, with its license.
`assets/vendor/VENDORED.md` names its source and hash and says how to update it.

## The schema twin

`assets/canvas-schema.json` is generated from `schemas/src/agent-canvas.ts` by
`schemas/scripts/export-agent-canvas.ts`; never edit it by hand. `tests/test_schema_twin.py` runs the
generator's `--check` when `schemas/` is installed, and says why it skipped when it is not.

## Tests

```bash
uv run pytest
```
