---
name: agent-canvas
description: Create or update user-facing file previews in TLDRack (HTML proposals, reports, images, PDFs), or fix a missing preview; artifact delivery does not require an assigned canvas path. Also draw structured diagrams, comparisons and questions when SessionStart supplies an Agent canvas path, the operator requests a canvas, or a canvas hook prompts you. Skip routine chat and application source edits without a user-facing deliverable.
---

# Agent canvas

## Generated files: artifact shelf

When running inside TLDRack, creating or updating a user-facing file includes putting the result
on this session's artifact shelf before the final reply. A filesystem link in terminal text does
not put a file on the shelf. This applies to every agent and writing tool, without an assigned
canvas path. Do not register application source files or intermediate output.

1. Save the deliverable inside this session's starting workspace. Hidden paths and symlinks are
   refused. HTML must be self-contained: inline scripts and embedded assets work, but external
   resources, network calls, forms and nested frames are blocked. Use ordinary DOM or shadow roots
   for comparison panels. The viewer's policy is owned by
   `plugins/agent-canvas/agent_canvas/artifact_page.py` — see the note there.
2. Run `python3 plugins/agent-canvas/scripts/register-artifact.py <path>` from the tldrack checkout,
   or use the absolute register-script path supplied by SessionStart when working in another
   repository. The script resolves the current tmux pane; outside it, pass the known
   `--session <name>`. Never guess another session. Registration is local delivery, part of the
   requested artifact; it needs no separate publishing step.
3. Confirm registration succeeded. Repeat after revising the file: the shelf stores a copy, so
   editing the source alone leaves a stale preview. Re-registering an unchanged file is a no-op.
   Claude's Write hook may register HTML automatically; shell, apply_patch and other tools do not
   have that guarantee. Explicit registration makes the handoff independent of the writing tool.
4. For interactive HTML, verify the saved copy in TLDRack's artifact viewer, including its main
   controls. A browser check of the source file alone does not exercise the viewer's restrictions.
   Resolve the shelf entry through `GET /plugin/agent-canvas/frag?session=<name>` and use its actual
   key and artifact id for `GET /plugin/agent-canvas/artifact?key=<key>&id=<id>` on the configured
   bridge status port. Do not weaken the viewer policy to make an artifact work.
5. In the final reply, name the file under **Artifacts → Preview**, and provide its verified viewer
   URL when available. A local file link can be a fallback. If registration or verification fails,
   report what failed and give the source-file link; do not claim it is visible in TLDRack.

The bridge must have the agent-canvas plugin running. Registration failures and viewer behavior:
owned by `plugins/agent-canvas/README.md` § Artifact shelf — see the note there.

**A deliverable is read by a person, so its text comes first.** A label that names a section is
bold and never the faintest ink in it. Body text is at least 16px on a page and 14px in a figure.
Several cards that answer the same questions are a table: the questions are row labels, printed once.
Read every figure at 100%, in each theme it has, before you hand it over.

Your canvas is the path the SessionStart context gave you, in the plugin's canvas directory
(`$AGENT_CANVAS_DIR`, else `$TLDRACK_DATA_DIR/canvas`, else `~/.local/share/tldrack/canvas`;
`GET /plugin/agent-canvas/health` reports it as `canvas_dir`), in one of two forms:

- **`<canvas_dir>/<key>.json` — a typed doc. Use this.** Blocks from a fixed vocabulary, checked
  the moment you write: anything wrong is cut from the pane and reported back to you by a hook, with
  the path and the fix. It can ask several questions at once, and real forms.
- `<canvas_dir>/<key>.html` — body markup against the CSS kit (end of this file). Only for
  something the blocks cannot express. One yes/no question at most.

Whichever you wrote LAST is shown. The user sees it a second after you write it.

## When to draw

Draw instead of describing whenever the turn changed or explained **structure**. The user reads a
picture in seconds and a paragraph in a minute.

| You just… | Draw |
|---|---|
| created directories or a module layout | `mermaid` `flowchart TD`: folders → files, new ones marked |
| added a process, service, launchd job, hook or plugin | `mermaid` `flowchart LR`: who starts what, and what talks to what |
| traced a request or a data path | `mermaid` `sequenceDiagram` |
| changed behaviour or a value | `diff` (BEFORE / AFTER) |
| weighed options | `table` (one row each), then a `choice` if it is the user's call |
| finished a build or a run | `stat` / `meter` row first, the detail in `details` |

Two prompts carry it to you mid-session: the terminal's **◧ DRAW** button pastes
"Draw what you just did on your agent canvas, <path>…", and a hook reminds you when a Write
creates a new directory, plist, plugin or router. Either way, draw the structure, not a recap of
your reply. The SessionStart line carries a compressed copy of this table; this file is its owner.

## Rules

1. **Overwrite the whole file each time**, with the Write tool. It is one pane, not a log.
2. **Small.** A side pane about 400–700 px wide. Lead with a `stat` or `meter`, not a paragraph; put
   second tellings in `details`. Several things with the same fields are ONE `table`, a row each,
   never a `stat` group or a list per thing that repeats the field names.
3. **Terminal text still carries the answer.** The canvas is the picture; your reply says what it shows in one line.
4. **To ask, end your turn after writing.** The answers are typed into your prompt as ONE line:
   `canvas › <q> → <answer> · <q2> → <answer2>`. A form's answer is compact JSON; a question the user
   left blank reads `(skipped)`. Do not also ask with AskUserQuestion.
5. **If the hook reports issues, fix and rewrite.** The rest of the canvas was drawn; the named blocks were not.

## The doc

```json
{"title": "optional", "blocks": [ {"type": "…", …}, … ]}
```

Every block and its keys: **[reference.md](reference.md)** (generated from the registry — the source of
truth). Unknown keys are errors, so `colums` fails loudly instead of rendering an empty table.

## Asking

- `choice` — buttons, plus a free-text box (`"other": false` removes it). `"multi": true` for several picks.
  An option is a string, or `{"label": "LATER", "reply": "defer", "hint": "…"}` when what is typed back
  should differ from the button.
- `form` — fields `text` · `select` · `toggle` · `rank` (the user reorders a list; the answer is the order).
- **One single-pick `choice` on the canvas sends on click.** Anything more collects every answer and
  sends them together from a SEND bar, so you get one message, not several.
- Give a question an `id` when you will refer to it; otherwise it is `q1`, `q2`… in document order.
- An answer you did not expect (free text) is an ordinary user message.

## Asking before you act

A button that deploys, pushes or deletes has to tell the operator what it does before they press it.
The kit prints what each press will do above the buttons (`ONE PRESS SENDS`). It also arms risky
options so they take two presses, and it locks a sent question with `SENT · THE AGENT HAS IT`. Your
job is the words. The schema refuses a risky option that leaves them out.

- **`does` on every option that runs something.** Say it in plain words with the command:
  `Runs the test suite, fixes what fails, then git push origin main`. It prints under
  the button and is never only a tooltip.
- **`risk`**: `outward` for anything that leaves this Mac (a push, a deploy, a post, a merge on
  GitHub), `destructive` for anything that deletes. Either one takes two presses.
- **A destructive option names every target and its undo.** `targets` lists each branch, file or
  row by name, and `undo` says how to get each back, or `none: permanent`. Say in `does` what is
  NOT backed up anywhere else (a branch never pushed).
- **The label is a verb and an object, never jargon or a bare count.** Write `DELETE 3 BRANCHES`,
  not `DELETE ABSORBED 3`.
- **Order safe → risky.** Put LEAVE or the read-only option first and destructive last.
- **After you act, redraw.** The answered question stays on screen, locked, until you write the
  canvas again. Replace it with the outcome (what ran and what changed), so nobody wonders whether
  a press did anything.

```json
{"type": "choice", "id": "ship", "q": "Ship main?", "options": [
  {"label": "LEAVE", "does": "Nothing runs. Main stays 1 commit ahead, unpushed."},
  {"label": "PUSH ONLY", "risk": "outward", "does": "git push origin main. No deploy."},
  {"label": "DELETE 1 BRANCH", "risk": "destructive",
   "does": "git branch -D; never pushed, so this Mac holds the only copy",
   "targets": ["old-experiment · 3 commits · 90% in main"],
   "undo": "git branch old-experiment refs/tldrack/archived/old-experiment"}
]}
```

## Example: a plan with a decision

```json
{"title": "Rack canvas · plan", "blocks": [
  {"type": "cols", "blocks": [
    {"type": "stat", "label": "tests", "value": 41, "unit": "pass", "tone": "ok"},
    {"type": "meter", "label": "context", "value": 62, "tone": "warn"}
  ]},
  {"type": "tabs", "tabs": [
    {"label": "Plugin", "blocks": [{"type": "list", "items": ["serve page + frag", "reply → send-keys"]}]},
    {"label": "Rack", "blocks": [{"type": "list", "items": ["CANVAS chip", "under-rack / column"]}]}
  ]},
  {"type": "mermaid", "source": "flowchart LR; agent-->|Write|file-->|poll|page-->|click|reply-->|send-keys|agent"},
  {"type": "details", "summary": "why this order", "blocks": [
    {"type": "text", "text": "The rack can only draw what the plugin serves."}
  ]},
  {"type": "choice", "id": "first", "q": "Build the plugin first?",
   "options": ["yes", {"label": "RACK FIRST", "reply": "no, rack first"}]},
  {"type": "form", "id": "scope", "q": "Scope", "fields": [
    {"type": "select", "name": "layout", "label": "Default layout", "options": ["under", "column"]},
    {"type": "rank", "name": "order", "label": "Order", "options": ["plugin", "rack", "docs"]}
  ]}
]}
```

Two questions, so the user answers both and presses SEND once; you receive e.g.
`canvas › Build the plugin first? → yes · Scope → {"layout":"under","order":["plugin","rack","docs"]}`.

## The markup form (fallback)

Body markup only — no `<html>`, `<head>`, `<style>` or script (the page's CSP drops script and
`onclick=`; remote images are blocked).

| Markup | Renders as |
|---|---|
| `<h1>` `<h2>` `<h3>` | mono uppercase heads |
| `<div class="card">…</div>` | bracket-framed block |
| `<div class="cols">…</div>` | side-by-side children that wrap when narrow |
| `<dl class="kv"><dt>tests</dt><dd>41 pass</dd></dl>` | key → value readout |
| `<table>` (`td class="num"` right-aligns) | styled table |
| `<div class="diff"><del>old</del><ins>new</ins></div>` | BEFORE / AFTER columns |
| `<div class="meter" style="--v:62%">context</div>` (`warn` / `bad`) | ratio bar |
| `<span class="tag">`, `.tag.ok` / `.warn` / `.bad` | short label |
| `<pre class="mermaid">flowchart LR; a-->b</pre>` | rendered Mermaid diagram |
| `<pre>`, `<code>`, `<ul>`, `<ol>`, `<p class="muted">` | the usual |

One question, as a `.choice` with `data-reply` buttons (`data-q` is how it reads in your transcript;
the kit adds the free-text box):

```html
<div class="choice" data-q="Merge the refactor now?">
  <button data-reply="yes">YES</button><button data-reply="no">NO</button><button data-reply="defer">DEFER</button>
</div>
```
