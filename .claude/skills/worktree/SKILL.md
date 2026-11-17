---
name: worktree
description: Open a git worktree for a branch and put an agent in it — this session, a new parallel agent in the rack, or worktree-isolated subagents. Use when the user says "work in a worktree", "parallelize this", "spin up an agent on a branch", "/worktree <name>", or starts a feature while other agents are on main. Every door lands in ~/worktrees/<repo>/<slug> with the tmux session named wt-<slug>, so the agent rack, the Branch Harbor and the PR button pick it up. Skip for plain branch switching in the current checkout.
---

# Worktree — one door, whichever way you came in

The point is a HABIT: parallel work happens in worktrees, not as a pile of agents on `main`.
Every door runs the same code, tldrack's worktree plugin (`plugins/worktree/`: naming in
`worktree/names.py`, git and the repository's hook in `worktree/create.py`), so pick the door by
what the user wants, not by mechanism. The plugin's `README.md` owns the invariants; read it before
changing anything below.

Below, `wt` is `plugins/worktree/scripts/wt` in the tldrack checkout. It works in whatever
repository you stand in, and puts that repository's worktrees under `~/worktrees/<repo>/<slug>`
(`WORKTREE_ROOT` moves the parent).

`$ARGUMENTS`: `<branch> [prompt…]`. The branch is a git branch name (`feat/foo` is fine: the
directory and session use its slug `feat-foo`). No name? Ask for one; a random name reads as
noise in the harbor.

## What the repository adds, if anything

- `.tldrack/bootstrap.sh` runs after a worktree is created (link dependency directories, write an env
  file), **only once pinned**: `wt trust` shows it and its sha256, `wt trust <sha256>` pins exactly
  those bytes. Unpinned or absent, nothing runs, and the row says so.
- `.tldrack/worktree.json` sets the base branch and directories to leave out (`sparse_exclude`).

## 1. Isolate THIS session

The user wants the conversation they are in to move onto a branch.

- `EnterWorktree({ name: "<branch>" })`. It lands in the managed root only when the repository's
  `.claude/settings.json` runs `wt hook-create` as its `WorktreeCreate` hook (and `wt hook-remove`
  for `WorktreeRemove`); the plugin's README § `wt` has the JSON. Without those hooks, Claude Code
  makes its own worktree and the rack does not group it: use `wt create <branch>` and `cd` there
  instead, or offer to add the hooks.
- Report three lines: the branch, the path (`wt path <branch>`), and the tmux name `wt-<slug>`
  (`wt session <branch>`). If this session is inside a tmux pane the rack already lists, say the
  rack shows it on its island after the next hook event (the bridge reads `cwd` from the hooks).
- Leaving: `ExitWorktree({ action: "keep" })` keeps the work for the rack's `PR` button. `"remove"`
  runs the `WorktreeRemove` hook when it is installed, which deletes the worktree, and the branch
  only if merged. Claude Code cannot verify a hook-created worktree's state itself, so `remove`
  needs `discard_changes: true` even on a clean tree: check `git status` first, and never remove on
  the user's behalf.

## 2. Spawn a PARALLEL agent

The user wants another agent working on the branch while this one continues.

- Bridge up? `curl -s -m 2 localhost:7682/plugin/worktree/state` answers JSON.
  - Yes: `curl -s -X POST localhost:7682/plugin/worktree/create -H 'Content-Type: application/json'
    -d '{"branch":"<branch>","prompt":"<prompt>"}'`. It returns `202`; poll `/state` until the row
    leaves `creating` (steps `worktree → bootstrap → session → prompt`). The rack draws the same
    ladder. When spawning from another rack session, include `"parent_session":"<caller>"`
    in the JSON body. Resolve the caller with `tmux display-message -p -t "$TMUX_PANE" '#{session_name}'`
    in the launching agent's shell; omit it outside tmux. Never use a name prefix or cwd to infer ancestry.
    The prompt is TYPED into the new agent, one line, ≤ 2000 chars.
  - No: `wt create <branch>` prints the path. Start the agent in a session the rack recognises:
    `tmux new -d -s "$(wt session <branch>)" -c "$(wt path <branch>)" claude`, then tell the user
    the session name.
- Report the session name `wt-<slug>` and that the rack row is attach-only (`attach:`, the
  invariant in the plugin's README); `PR` on that row commits, pushes and opens the pull request.

## 3. Fan out subagents

The work splits into independent pieces this session should run itself.

- `Agent({ …, isolation: "worktree" })` per piece. With the `WorktreeCreate` hook installed, each
  one lands under `~/worktrees/<repo>/agent-<id>`; a subagent that changed nothing is removed when it
  finishes, one with changes stays and shows in the harbor as an island until someone lands or
  removes it. Without the hook they still isolate, in Claude Code's own location.

## 4. Hand the branch to Codex

The user wants Codex (not Claude) working the branch.

- From the rack: pick the **CODEX** LAUNCH chip (the profile with `"kind": "codex"` in
  `~/.config/tldrack/profiles.json`), then `+ WORKTREE`. `POST /create` takes that `profile` and
  spawns its CLI. The first-task prompt is NOT typed into Codex: its first launch in a new tree
  shows a trust list, and Enter would pick "trust, no approvals". The user types the task once Codex
  is up.
- From a shell: `wt create <branch>`, then
  `tmux new -d -s "$(wt session <branch>)" -c "$(wt path <branch>)" codex`: the same tree and the
  same `wt-<slug>` session, Codex in the pane.
- Codex reads `AGENTS.md`. Where a repository keeps it as a symlink to `CLAUDE.md`, a worktree
  branches from the base's HEAD, so a guide change reaches it only once COMMITTED.

## Rules that travel with every door

- **Never `git stash`, `checkout`, `reset` or `clean` in the main checkout.** Other agents' work may
  be live there, uncommitted.
- **If the repository's bootstrap LINKS dependency directories from the main checkout, `npm install`
  inside a worktree writes THROUGH the link** into main's `node_modules`. Fine for a source-only
  branch; wrong for one that changes dependencies: install into a real directory there instead.
- **Test only what you touched; never a whole suite.** Every worktree is one more agent on the same
  CPU.
- **A worktree is not done until `PR` (or `wt remove`) has run.** The harbor's islands are the list
  of ones nobody returned to.
- PR references (`#1234`) are not accepted by the hook: the branch validator refuses `#`. Use `git
  worktree add` by hand for those.
