# claude-skills

A tldrack plugin for the Claude Code skills on this machine. It does three things:

1. **Inventory.** One request answers "what skills do I have, what does this project add, and what am
   I not using?" Skills pile up in several places (every account, every project, every installed
   plugin, the CLI itself) with no single view; `GET /state` is that view. The agent canvas's
   ◇ SKILLS audit (`plugins/agent-canvas`, `assets/skills.js`) draws it.
2. **Run.** `POST /run` starts a skill the operator has allow-listed as its own interactive Claude
   session, which lands in the agent rack like any other. Nothing is runnable until the operator
   says so (§ Settings).
3. **Link.** `scripts/link-skills` links tldrack's own skills into `~/.claude/skills/`, so they load
   in every repository, not only inside the tldrack checkout (§ Linking tldrack's skills).

```
GET  /plugin/claude-skills/state   the full inventory, plus what /run accepts
GET  /plugin/claude-skills/skill   ?path=<skill dir>[&file=<rel>] → {files, text, truncated}: one skill's files, one file's text
POST /plugin/claude-skills/open    {"path": "…"} → reveal that folder in the file manager (macOS)
POST /plugin/claude-skills/run     {"skill": <a run key>, "replace"?: true} → start it in the agent rack
```

## Tiers

| tier | source | revealable |
|---|---|---|
| `global` | every account's `skills/*/SKILL.md` | yes |
| `synced` | every account's `skills/synced/<bucket>/*/SKILL.md`: claude.ai account skills, `anthropic-skills:<name>` | yes |
| `project` | `<start_dir>/.claude/skills/*/SKILL.md` | yes |
| `plugin` | each account's `installed_plugins.json` entry's `installPath/skills/*/` | yes |
| `available` | marketplace catalog **minus** installed: "what am I missing" | vendored only |
| `builtin` | scraped out of the CLI binary (see below) | no, compiled in |

**Every account.** `~/.claude` is only the default account. Each launch profile's config dir
(`profiles.claude_roots()`: `~/.claude-a`, `~/.claude-b`, …) has its own `skills/` and `plugins/`, and
an agent in that account sees those. The scan walks all of them and keeps one row per real directory
(accounts often symlink to each other's skills). Each row's `profiles` lists the accounts that hold it.
Rows also carry `mtime` (the newest file in the skill) and `bytes` (the SKILL.md alone), for an audit.

`available` is filtered to first-party **vendored** entries (`"source": "./plugins/…"`), because those
are the ones whose files are already on disk and describable without a network fetch. The response
always reports `counts.available_catalog_total` alongside, so the filtering is visible rather than
silent; pass `?all=1` to include remote entries.

**Supersession.** A `global` or `project` skill may declare the plugin skills it replaces with a
top-level `supersedes: plugin:skill, plugin:skill` frontmatter key. Every record carries its own
`supersedes` list, and each `plugin` record carries `superseded_by` (the winner's name, or `null`).
Why the declaration sits on the winner: `scan.py` § supersession.

**`runnable`** lists what `POST /run` accepts, as `{key, skill, session}` rows (never the prompts),
so a page draws a skill's button only when pressing it would work.

## The built-ins are a scrape, and that is load-bearing

Built-in slash commands are **not files**. The CLI ships as one large native executable with no
`skills/` directory and no `claude skills` subcommand; the commands live inside its bundled, minified
JS as `ou({name:"…",menuDescription:"…",description:"…"})` calls. `builtins.py` reads the binary in
overlapping chunks and matches that shape.

- **It is best-effort.** Minifier output is not an interface. A future CLI can restructure the call
  and the yield drops. Every row therefore carries `source: "scraped" | "curated"`, and a curated
  floor (`CURATED_BUILTINS`) is unioned in, so a fully broken scrape degrades to a known-good list
  instead of a blank panel.
- **The scrape earns its keep in the other direction**: it surfaces the commands a new CLI build
  adds, which no hand-kept floor can know about.
- **Anchor backward, not forward.** The matcher finds `menuDescription:` and searches *backward* for
  the nearest `name:`. Forward matching needs a guessed window width and silently drops registrations
  that carry extra properties in between (`requires:{workspace:!0}` is the case that broke it). Don't
  "simplify" it back.
- **Cost is paid once per CLI build**, cached under `~/.cache/tldrack/` keyed on the binary's name,
  mtime and size. The scrape runs on a background thread, never the request thread, because the host
  contract forbids blocking and a cold read of a binary that size is slow. The first `/state` after an
  upgrade returns `builtin_status: "scanning"` with the floor as a placeholder; the next is `"ready"`.

## Running a skill (`POST /run`)

`POST /run` starts an allow-listed skill as an **interactive** Claude session in a detached tmux
session, so it lands in `/snapshot` and the agent rack like any other, and the operator can watch it
and answer its questions. The reasoning is owned by `claude_skills/run.py`'s header; in short:

- **The operator's `run` table is the authorization**, and it is empty by default. Only a key named
  there runs, with that entry's skill, session name and prompt. No request field reaches the argv or
  the keyboard.
- **Nothing is ever typed.** The prompt rides the spawn argv (`profiles.spawn_argv`, `extra_args`),
  because a skill can stop at a question whose answer acts, and a keystroke into that pane could
  answer it. No route here sends keys.
- **Idempotent.** A live session answers `created: false` and is left alone. `replace: true` kills it
  first; a caller sends that only behind a two-step arm, for an idle session.
- **The skill must exist where the session will look**: the project's `.claude/skills/<skill>/`, or
  the skills dir of the account the session starts in (the default Claude profile's config dir, else
  `~/.claude`). A skill linked by `link-skills` qualifies.
- Every body carries `plugin` and every refusal a `code`: `unknown_skill` (400), `tmux_missing`,
  `no_profiles`, `no_claude_profile`, `cli_missing`, `skill_missing` (503), and `spawn_failed` /
  `replace_failed` (500).

## Settings

The plugin reads its object from the bridge's `settings.json` once, when the bridge starts:
`plugins["claude-skills"]`. Unknown keys are ignored. A missing object means every default.
Restart the bridge after a change.

### `run`: what `POST /run` may start

Default: `{}`, so nothing is runnable and every key is refused with `unknown_skill`.

Each key is the token a button sends; each value says what it starts:

```json
{
  "plugins": {
    "claude-skills": {
      "run": {
        "release-notes": {},
        "release-notes-ship": {
          "skill": "release-notes",
          "prompt": "/release-notes ship the drafted entry",
          "session": "notes-ship"
        }
      }
    }
  }
}
```

| field | default | rule |
|---|---|---|
| *(the key)* | | a skill-style name: `a-z`, `0-9`, `-`, at most 64 |
| `skill` | the key | the skill it starts, same rule; it must exist (§ Running a skill) |
| `session` | the key | the tmux session it runs in: `A-Z`, `a-z`, `0-9`, `_`, `-`, at most 64; unique across entries |
| `prompt` | `/<skill>` | one printable line of at most 2,000 characters that starts with `/<skill>` |

A key that names another skill gives one skill a second entry point: a fixed mode after the slash
command, in its own session, so it never takes over the first one's. Two keys may not share a
session (the second would report the first one's run as its own). An entry that breaks a rule is
dropped with one line in the bridge log; the rest still load.

The prompt is the operator's literal and never caller text, and it must start with the skill's own
slash command: such an argument is neither a `claude` subcommand nor a flag.

## Linking tldrack's skills

Claude Code reads a project's `.claude/skills/` only inside that project. tldrack's own skills are for
every repository you work in, so `link-skills` gives each one a symlink in your account:
`~/.claude/skills/<name> -> <tldrack checkout>/.claude/skills/<name>`. A symlink, not a copy, so a
`git pull` of the checkout updates them. `tldrack hooks --install` runs it for you.

```bash
plugins/claude-skills/scripts/link-skills              # link (safe to run again)
plugins/claude-skills/scripts/link-skills --dry-run    # print the plan, change nothing
plugins/claude-skills/scripts/link-skills --remove     # delete only the links into this checkout
```

| flag | |
|---|---|
| `--source DIR` | the skills to link (default: this checkout's `.claude/skills`) |
| `--dest DIR` | where to link them, repeatable (default: `~/.claude/skills`, plus `$CLAUDE_CONFIG_DIR/skills` when that is set). Give one per account to link several |
| `--force` | also replace a link that points somewhere else; never a real directory or file |
| `--remove` | delete the links that point into the source, and nothing else |
| `--dry-run` | print the plan and change nothing |
| `--json` | one JSON object (`source`, `dry_run`, `failed`, `steps`) instead of lines |

- **A skill is a directory holding `SKILL.md`.** Nothing else in the source is linked.
- **Nothing of yours is replaced.** A real directory or file with a skill's name is skipped and
  reported (`skipped: <reason>`), and so is a link to somewhere else unless you pass `--force`.
- **Idempotent.** A link already pointing at the skill is `current`. A link into the checkout whose
  skill is gone (renamed upstream) is pruned, so a pull never leaves one dangling.
- **Exit codes:** 0 when nothing failed (skips are not failures), 1 when a step failed, 2 when there
  is no skills directory to link from.

It needs no venv: the script finds any Python >= 3.10 and runs `claude_skills/link.py`, which is
standard library only.

## Security

`POST /open` and `POST /run` are the routes that act on the machine.

`POST /run` passes a caller check narrower than the host's (`claude_skills/guard.py`, through the
core's `CallerPolicy`): a loopback `Host`, no proxy header, and an Origin from exactly three places:
the browser origins the operator configured for the bridge (the host's `allowed_origins`; none by
default, and no hosted site is built in), the Vite dev server on loopback, and this bridge's own
terminal page. Every other page, loopback included, is refused. The check reads headers, so the
plugin defines `handle_request`; on an older host that never passes headers, `/run` refuses (426).

`GET /skill` hands file text to a browser page, so it shares `/open`'s containment
(`resolve_reveal_target`: symlinks resolved first), then resolves the file again under the skill's
real directory. `../` and links out of the skill are refused, binaries are refused, and text is
capped at 256 KiB (`files.py`).

`POST /open` has two independent gates:

1. The **host's Origin guard**, applied to every plugin POST before dispatch; plugins cannot weaken it.
2. `resolve_reveal_target()`: the target must resolve **inside** one of the scan roots.
   `Path.resolve()` runs *before* the containment check, so a symlink planted inside a root that
   points at `/etc` is judged on its real destination, and `..` traversal collapses for free. The
   path is passed as an argv element, never a shell string.

## Enabling

```bash
# One-off, from the checkout root:
TLDRACK_PLUGINS="$PWD/plugins/claude-skills" uv run tldrack

# Persistent: ~/.config/tldrack/plugins.json
{ "version": 1, "plugins": ["/Users/you/code/tldrack/plugins/claude-skills"] }
```

Plugins are imported at bridge **startup only**: restart the bridge after adding it or changing its
settings.

## Tests

```bash
uv run pytest
```
