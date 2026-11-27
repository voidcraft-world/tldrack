# tldrack — Agent Guide

An agent rack: coding-agent sessions in tmux, one browser page that shows them all, and a
small local model, Laya, that decides when a finished turn needs the operator. `AGENTS.md` is a symlink
to this file, so every coding agent reads the same guide.

**Status: pre-release.** Everything below is here and tested; v0.1.0 is the first tag.

A session here starts with no memory. What follows is everything durable it needs.

## Layout

| Directory | Holds |
|---|---|
| `bridge/` | The rack's server. Python package and CLI `tldrack`, a host built on the public bridge core |
| `plugins/` | `worktree`, `laya`, `agent-canvas`, `coordinator`, `claude-skills`, `devices`, `phone-link` |
| `ui/` | One Vite + Vue app: the rack, Laya review, the diagram, evals. npm `@tldrack/ui` |
| `schemas/` | The zod contracts the rack reads. npm `@tldrack/schemas` |
| `lab/` | Laya's training, golden set, gate, bench and verifier card |
| `bench/results/` | Aggregate eval results (JSON). No row text, no row keys |
| `android/` | The Laya review app, `world.voidcraft.tldrack` |
| `docs/` | README figures, `VERIFIER.md`, `REPRODUCE.md`, `MODEL_CARD.md`, `INSPIRATIONS.md`, `evals/` |
| `scripts/` | `projects.py` (what a project is and how it is tested), `pack.py`, `check_archive.py` and `release.py` (a release) |
| `tests/` | The root's tests: `test_purity.py`, `test_projects.py`, `test_release.py`, `test_affected_tests.py` |

## House rules

1. **Nothing private ships.** `tests/test_purity.py` scans every file a commit carries, itself
   included. Tier 1, hazards (a home path, an address, a cloud account id, a token, a device serial, a
   checkpoint, a pointer into a tree this one does not hold), is zero always, and there is no allow
   comment: rewrite the line. Tier 2, private words, is a per-file count that only goes down
   (`--tighten` banks a drop). Its words are the operator's alone, read from outside the tree and never
   written here in any form (that file's docstring owns where).
   A release runs `--publish`: zero of both tiers, with the operator's list configured. The public word
   for the person who runs the rack is **operator**. Public code never points back into the private host
   repository it came from. A new leak shape is a new rule in that file, with a planted sample.
2. **Test what you touched.** The edit hook (`.claude/hooks/affected_tests.py`) runs the purity scan of
   the changed file and the tests of its project after every edit, and wakes you on a failure your change
   caused. The tests run in a copy outside the tree holding only what git tracks or you wrote, so a file
   that arrived by `curl` or `tar` is never executed; `git add` it to have it tested. Do not run a wider
   suite to "make sure": CI runs every project, and `ci-ok` is the one required check.
3. **Function names are `snake_case`**, in Python and TypeScript alike. Classes and Vue components keep
   `PascalCase`.
4. **One explanation, one owner.** The why lives next to the mechanism. Every other site gets one line:
   `// <thing>: owned by <file> — see the note there.` A pointer names a file, never a line number.
   Repeated comments mean repeated code: extract it.
5. **Decide the default; ask only what is the operator's**: a string only they know, a real preference
   with no dominant answer, or something irreversible or outward-facing. Say what you chose and how to
   change it.
6. **Redundant checks over speed.** Every route stays behind the core's guards. Read untrusted JSON
   through the core's `typed()` reader and validate at the boundary. Loopback only by default: no
   hosted origin is allowed unless the operator configures it.
7. **Commits use the GitHub noreply identity**, set in this repository only. The history is public.
8. **The rack's words.** A session a LEAD starts is a **worker**; the sessions on the page are the
   **rack**; a LEAD step is a **change** (it ends in a PR or a merge) or a **report**. Another project's
   vocabulary for these is not ours, in code or prose: `docs/INSPIRATIONS.md` names its words, and
   `tests/test_vocabulary.py` fails on one, and on the brand prefix step 6.8 retired. The few old names a reader still
   takes for one release (step 6.8: wire messages, stored fields, step kinds, fine-tune names) each sit
   beside a comment naming that step.

9. **Deliver files through the rack.** Before handing off a generated deliverable in TLDRack,
   read `.claude/skills/agent-canvas/SKILL.md` § Generated files: artifact shelf; it owns registration,
   preview verification and the final link for every agent, even without a SessionStart canvas path.

## Durable rack facts

- **The bridge is a host on the public core** `voidcraft-world-bridge` (MIT, stdlib only, on PyPI). It
  subclasses the core's `BridgeHandler` and adds about 60 routes; the guards (Host, then Origin) run
  ahead of every one. A change the rack needs from the core is a core release, made in that repository
  through its own `scripts/release.py` and nowhere else.
- **Two ports.** ttyd serves the terminals on 7681; the bridge's HTTP API listens on the status port,
  7682 (default port + 1). The operator's live bridge holds both: never stop it and never start a
  second one there. A dev bridge runs on `--port 7781 --status-port 7791` with its own empty data
  directories.
- **An agent gets a bridge through the CLI's `up` subcommand.** It answers at once when one is up,
  starts a down one, and never stops a running one. Never hand-run a bridge, never kill one.
- **A ttyd URL is a constructor.** `?arg=<name>` attaches or *creates* the tmux session; only the rack's
  live pane uses it, because `+ NEW` needs it. `?arg=attach:<name>` attaches only, and is what anything
  that saves or replays a URL must use, or it resurrects sessions the operator killed. A failed attach
  blocks on `read` instead of exiting, because an exit sends ttyd's client into a reconnect storm.
- **`-t disableLeaveAlert=true` stays on ttyd.** The rack navigates its terminal frame on every session
  switch, and without it the browser asks "Leave site?" each time.
- **`/snapshot` is additive-only**: add keys, never change one. Until step 5.3 of the move it is frozen
  outright, with every route path: a frozen copy of the rack UI still reads them.
- Installed agent hooks post to `/hook`; live rack state streams on `/events`; a plugin mounts at
  `/plugin/<name>/…`.
- **On-disk data** defaults to `~/.config/tldrack`, `~/.local/state/tldrack` and
  `~/.local/share/tldrack`. A machine with older directories maps them in one `paths.json`; nothing is
  migrated.
- **Laya** is a fine-tune of `convaiinnovations/laya` (ModernBERT-large, Apache-2.0) that scores whether
  a finished agent turn needs the operator. The operator's labels, ledger text, checkpoints and compare
  reports never enter this repository and never leave their machine. Whether a trained checkpoint ever
  ships is the operator's decision; until then the purity test fails on checkpoint files. Public numbers
  come from one protocol; a figure from another protocol is a footnote, never a headline.
- **The Laya review app needs a debug build**: the bridge pushes its token with `adb run-as`. Grading is
  tap-only: DONE, READ, RUNNING, NEEDS YOU, SKIP.
- **A release is a git tag** with two tarballs on a GitHub release, `@tldrack/schemas` and `@tldrack/ui`
  (`scripts/release.py tag X.Y.Z`). There is no PyPI release of tldrack yet: it installs from a clone,
  `uv tool install --editable ./bridge` (README § Quickstart owns the steps; the bridge serves `ui/` and
  `plugins/` from the same checkout, so a VCS install of `bridge/` alone is not enough). The tree holds ONE version,
  X.Y.Z, in `bridge/pyproject.toml`, `schemas/package.json` and `ui/package.json`; a tag is that number
  plus an optional `-rc.N`. A package the release builds commits its `package-lock.json`; `ui` has a
  `build` script whose output (`dist/`) its `files` ships; a sibling dependency may stay `file:../schemas`,
  because the tarball's copy is pinned to the same release. Every tarball carries the root's `LICENSE`
  and `NOTICE` and is checked by what it holds before upload. The why, and every refusal: `scripts/pack.py`.

## Commands

```bash
python3 tests/test_purity.py                  # nothing private ships (exit 1 on a hit)
python3 tests/test_purity.py --report         # every hit, by rule and file
python3 tests/test_purity.py --publish        # what a release runs: zero of both tiers
python3 scripts/check_archive.py <tarball>    # a built tarball, read by what it holds
python3 scripts/projects.py                   # the projects CI tests
uvx ruff@0.17.5 check .                       # the lint standard (ruff.toml owns the rules and the ladder)
uvx ruff@0.17.5 format .                      # the layout: run it after editing Python, CI refuses unformatted code
npx -y oxlint@1.94.0 --deny-warnings .        # the same for TypeScript, Vue and JavaScript (.oxlintrc.json)
python3 scripts/typecheck.py                  # mypy: no file may gain a type error (--tighten banks a drop)
npm run typecheck                             # in ui/ or schemas/: the TypeScript type check, zero errors
uvx --from pytest==9.1.1 pytest -q tests      # the root's own tests
python3 scripts/pack.py                       # build both release tarballs here, into dist/release/
python3 scripts/release.py tag X.Y.Z --dry-run   # every release check and the plan; changes nothing
```

## During the move to 0.1

Delete this section at v0.1.0.

- **Every lane lands by pull request to `main`.** A lane rebases on `main` and runs its tests first.
- Each lane works in its own worktree, `~/worktrees/tldrack/<slug>`, and its brief arrives in its first
  prompt.
- **One lane, one set of files.** Two lanes never edit the same file; a missing piece in another lane is
  a comment on that lane's pull request.

| Lane | Owns |
|---|---|
| repo | the root: `README.md`, `CLAUDE.md`, `NOTICE`, `docs/` except `docs/evals/`, `.github/`, `.claude/`, `scripts/`, `tests/` |
| bridge | `bridge/**` |
| plugins | `plugins/{worktree,agent-canvas,claude-skills,coordinator,devices}/**` |
| ui | `ui/**`, `docs/evals/` |
| schemas | `schemas/**` |
| lab | `lab/**`, `plugins/laya/**`, `bench/**` |
| android | `android/**`, `plugins/phone-link/**` |

The purity test's rules are the repo lane's: another lane asks for a new shape on its pull request.
