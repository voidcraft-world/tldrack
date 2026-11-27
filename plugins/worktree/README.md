# worktree — tldrack plugin

Git-worktree-backed agent sessions, and a button that turns one into a pull request
and, once that work lands, cleans it away.

`+ WORKTREE` in the agent rack creates a branch, a worktree, and a detached tmux
session running `claude` inside it, in any repository the rack serves. The session shows up in the rack like any
other. When the work is done, `PR` commits it, pushes it, and opens the pull
request. Once that work has landed in main (merged, squashed, or any other way
the harbor can prove — and the branch carried work of its own, so a fresh one
does not qualify), the same button becomes `CLEAN`: the worktree, its session
and its branch go in one press.

## Why this exists

`+ NEW` creates a session by pointing the ttyd pane at `?arg=<name>`. ttyd runs
with `-a`, so that becomes `tmux new -A -s <name>` — attach-or-create — in the
bridge's single global `--start-dir`. Every agent therefore lands in the *same
working tree*: hundreds of dirty files on `main`, and no agent (or code review)
able to tell its own work from the rack's.

A worktree session is isolated by construction. The PR button is what keeps that
isolation from becoming a directory you forget about.

## Repositories

One rack serves several repositories. The operator lists them in `settings.json`
(§ Settings); unset, the plugin serves the repository the bridge starts in.

- **Where worktrees live:** `~/worktrees/<repo>/<branch slug>`, with `<repo>` the main
  checkout's directory name. `WORKTREE_ROOT` moves the parent, `~/worktrees`.
- **Which one a request means:** every route takes `repo`, in the query or the JSON
  body: a key from `GET /repos`, or a path inside the repository or one of its
  worktrees. Absent, the first configured repository answers, so a client that
  never sends `repo` sees the single-repository plugin.
- **State is keyed by repository:** `~/.local/state/tldrack/worktree/<key>/`. The
  repository the bridge starts in keeps the state an older, unkeyed plugin left
  (copied once, never moved).
- **Session names are tmux-global:** `wt-<slug>` in one repository can collide with the
  same branch name in another. `/create` refuses (`409`) rather than select another
  repository's agent; pick another branch name.

| Route | Shape |
|---|---|
| `GET /repos` | `{repos: [{key, label, path, worktree_root, base, bootstrap, bootstrap_sha256, bootstrap_trusted, sparse_exclude, default, gh, settings_errors?}], default, skipped: [{entry, reason}]}`. Paths are `~`-collapsed. `skipped` names every configured entry that was refused: not a git checkout, the same repository twice, or two repositories with one directory name (both would use `~/worktrees/<name>`). |

### What a repository decides for itself: `.tldrack/`

Optional, read from the main checkout by every door (the plugin, `wt`, the hooks):

| File | What |
|---|---|
| `.tldrack/bootstrap.sh <worktree> <main checkout>` | Runs after a worktree is created and after `wt sync`, **only once the operator pinned it**: it is the repository's code, and git never runs a hook a clone brings either. `wt trust` prints it and its sha256; `wt trust <sha256>` (or `POST /trust-hook {repo?, sha256}`) pins exactly those bytes, and a changed hook stops running until pinned again. Best-effort: a failure, or an unpinned hook, is a note on the row, never a refused create. Must be idempotent. Link dependency directories from the main checkout, write an env file. Absent: nothing runs. |
| `.tldrack/worktree.json` | `{"base": "<branch>", "sparse_exclude": ["<dir>", …]}`. `base` is where a new branch starts (else `main`, else the main checkout's branch). `sparse_exclude` leaves large directories out of every worktree (non-cone sparse checkout, so a directory added later is still included). |

## Session families

`parent_session` records who requested a new agent. The host resolves its UUID before the worker
starts, and the worker passes the stamp through `profiles.spawn_argv`. The state document reports
`capabilities.session_lineage`; a caller requiring ancestry must check it before creating.


## Parent task groups

The existing plugin also owns persistent worker batches and automatic PR publication.
`GET /groups`, `POST /group-create`, `/group-join`, `/group-report` and `/group-control` serve the
launcher, the LEAD's core, the worker CLI and parent controls. `/state.task_groups` supplies both rack
transports. The publication contract is owned by `worktree/task_group_runner.py`.
The state file is `worktree-task-groups.sqlite` in the repository's state directory.
No new plugin registration or model-based supervisor is required.

| Route | Shape |
|---|---|
| `POST /group-create {parent_session, label, start_sha, workers?, auto_pr?, membership?, release?}` | `membership: "fixed"` (default; 1–32 `workers`) or `"open"` (0–64, grown by join). `release: "batch"` (default; sealed + all ready) or `"each"`. A lane is `{branch, publication_branch?, start_sha?}`. `200 {group}` · `400` invalid or a branch another group holds. |
| `POST /group-join {id, branch, publication_branch?, start_sha?}` | One lane onto an OPEN group, validated as a create lane is. An exact repeat answers the group unchanged. `200 {group}` · `409` fixed, sealed, 64 already, or the branch joined with a different publication branch or start · `400` invalid, unknown group, a branch another group holds. `start_sha` is the commit the branch was cut from; the published-base guard checks it instead of the group's. |
| `GET /groups` | `{groups, capabilities: {task_group_modes}}`. Worker rows carry `stage`, `head`, `pr_url`, `pr_status`, `pr_state` (`open` · `merged` · `closed`), `pr_head`, `landing` (`{how, pr, branch}` once its branch landed), and for `each` groups `ci` (`pending` · `passing` · `failing` · `none`) + `ci_head`. |

**release `each`** publishes a worker the tick it is ready (no error, no PR, `auto_pr` on), whatever
its siblings are doing; the seal is not needed and one worker's failure stays on that worker. A worker
with an OPEN PR that re-reports ready with a new tested head is a **follow-up**: the runner preflights
it as a first publication, reads the PR (`gh pr view`), proves the PR head is an ancestor of the new
head (`git merge-base --is-ancestor`) and pushes without `--force`; else the worker's error reads
`a follow-up must fast-forward the open PR`. A merged or closed PR is recorded, never pushed to. Each
followed PR's `statusCheckRollup` is read at most once a minute (`ship.summarize_checks`); a failed
read keeps the last one. `batch` groups get none of this and publish exactly as before.
`capabilities.task_group_modes` (on `/state` and `/groups`) says all of it is here.

**Delivered** is either fact: the worker's own PR merged, or its branch (or the publication branch it
prepared) landed by the harbor's verdict, inside another branch's PR or any other way
(`landing.work_landed`). A delivered worker is never a blocker, counts 100%, freezes its report and is
never published or followed again; a group whose every worker is delivered reads `complete`. The runner
stamps `landing` each tick from `/state`'s verdict, and `/delete-branch` stamps it before a landed
branch goes, so a CLEANUP never sends a group back to `blocked`. `task_groups.py` owns why.

## THE INVARIANT

**A worktree session must always be focused with `attach:<name>`, never a bare
`?arg=<name>`.**

`ttyd_session_script()` runs `tmux new -A -s "$1" -c '<global start_dir>'`. If a
worktree session dies and the pane re-points at the bare form, tmux silently
creates a *new* session with that name **in the main checkout** — an agent that
believes it is in a worktree while editing `main`. That is the precise failure
this plugin exists to prevent. `ATTACH_ONLY_PREFIX` / `attach_only_arg()` in the
UI's `engine/terminal/session-name.ts` are the primitives.

## Routes

| Route | Behaviour |
|---|---|
| `GET /state` | Every managed worktree: branch, path, derived session name, dirty, ahead/behind, PR status. One `git worktree list` + one `for-each-ref` behind a 4 s TTL cache; `dirty` comes from the background dirty tier (`worktree/dirty.py` — `null` until a sweep reaches the tree), never a `git status` on the request thread, which makes every recompute seconds long with dozens of trees. Additive `worktree_paths`: every **non-main** worktree on the machine, `~`-collapsed and unfiltered — the rack's attach-only set. Additive `worktree_trees[]`: each of those as `{path, branch, pr, landing}`, `pr` the branch's PR (`null` = none known; `pr_known` says whether the index was ever fetched) and `landing` the same `{how, pr}` verdict its row would carry (`null` = not landed or not judged) — the island reads `landing` first. Additive `capabilities: {create_base, cleanup, permission_modes, gh, repos, …}` — what `/create` accepts beyond its first shape, whether `/cleanup` exists, and whether `gh` is installed (`false`: hide the PR verbs). Additive `repo_key`. Each row also carries `landed` (`true` · `false` · `null` = cannot say yet: no PR index, merge state unmeasured, content proofs pending — `branches.landed_or_unknown`) and `landing: {how, pr}` — the harbor's own verdict through `_prove_landed`, overlaid after the cache like `dirty` — plus `cleanup_status` (`running` · `error` · `null`) and `cleanup_error`. |
| `POST /cleanup {branch, session?}` | The rack's CLEAN: a managed tree whose branch has landed goes, with its session(s) and its branch, in one press. Async, the `/pr` shape. Synchronous refusals, in order: `400` bad branch (`validate_existing_branch`) or session name (`validate_session_name`; empty = absent) → `404` no managed tree (`_managed()` stays the only write gate) → `409` creating → `409` already cleaning (`CleanupStore.claim`, a test-and-set) → `202 {ok, branch}` + worker (`500` when the thread cannot start; the row reads `error`, never a stuck `running`). The worker asks everything FRESH and before it destroys anything (`plugin.py::_clean_up` owns the order): no `/pr` job in flight → **landed** by the harbor's own verdict (`_fresh_row`, as `/delete-branch` asks it) → **work of its own landed** (`landing.work_landed`: a branch cut from main and never committed to is an `ancestor` too, so an ancestor counts only with the merged PR that shipped its tip, or whose merge commit carried it in, `landing: {how: 'ancestor', pr: N}`) → still managed → **clean** (a fresh dirty probe; uncommitted or unreadable refuses, never forced; a `prunable` tree has nothing on disk to probe) → kill the derived `wt-<slug>` and `session` (the caller's cwd-joined pane: a `+ NEW` that ran `EnterWorktree`; killed only if it exists) → **nobody else in the tree** (`landing.await_vacated`: `lsof` cwds, polled for up to `OCCUPANT_SETTLE_SECONDS` while the killed processes leave; a third session or a dev server still there, or an `lsof` that cannot answer, keeps the tree) → `git worktree remove` without `--force` (`create.remove_worktree`, `/remove`'s own) → drop the tree's rows and invalidate the `/state` + harbor caches → delete the branch through `/delete-branch`'s body (`-D` when squash-landed). A refusal or a crash lands on the row as `cleanup_status: error` + `cleanup_error`, cleared by the next press, by `/remove`, `/delete-branch` or a new `/create` of that branch (`BranchJobs.drop_settled`, which never drops a running job), or after `CREATE_ERROR_TTL_SECONDS`; a branch delete refused once the tree is gone is logged, not an error (the harbor can finish it). `CleanupStore` is in memory like create progress (`store.py::BranchJobs` owns why). While one runs, `POST /pr` on that branch answers `409` and `busy_reasons` holds a bridge restart. |
| `POST /create {branch, prompt?, existing?, profile?, base?, parent_session?, permission_mode?}` | `git worktree add` → the repository's bootstrap hook → detached tmux session running `claude`. Idempotent on the session name. `existing: true` checks out a branch that already exists (`404` unknown, `409 already checked out at ~/…` when any worktree holds it). A branch held by a MANAGED tree whose `wt-<slug>` session is not live is **respawned** instead: the agent starts in that tree (profile, `parent_session` and prompt as usual), `worktree` and `bootstrap` complete at once, and the answer carries `respawn: true` and the tree's path; a live session or a tree outside the repository's worktree root keeps the `409` (`capabilities.create_respawn`). `base` cuts the new branch from that ref instead of the repository's base — resolved to a commit up front (`400` for an unknown or flag-shaped ref, or with `existing`), echoed as `base` + `base_sha` (`create.resolve_base_ref`). `permission_mode` (today only `auto`; the LEAD sends it for every worker) starts the agent in its CLI's automatic-approval mode, a respawn too, and is echoed on the `202` that starts it. A `400` names why it cannot: a mode the host does not list in `capabilities.permission_modes`, a host that lists none, or the host's `permission_refusal` for this profile (a shell, a profile that pins its own approvals, a CLI without the flag). Absent is the default launch. What each mode means: owned by the bridge's `profiles.py` `PERMISSION_ARGS`. |
| `POST /pr {branch, title?}` | `202`; commits dirty work, pushes, `gh pr create`. Outcome lands in `/state`. |
| `POST /remove {branch, force?}` | Kills the session and removes the worktree. Refuses `409` on uncommitted work unless `force`. Keeps the branch (`/cleanup` is the verb that takes it, once landed). |
| `GET /branches` | The **Branch Harbor** document: every local branch (`ahead`/`behind`/`merged` vs the local base, `landed` + `landing: {how, pr}` (ancestor · PR · squash · commits — `worktree/landing.py`), `contained_in`, `content` (`measured`/`pending`/`skipped`), a `ledger` of the commits main lacks, upstream + `[gone]`, the worktree holding it, PR from the index, a `delete_verdict`, `worktree_removable_outside`) and every worktree (`managed`, `is_main`, `dirty`, `occupied_by`, `prunable`, `locked`). Every unproven row also carries `absorption` (the evidence tier, `worktree/absorption.py`: `pending` · `too_big` · `measured` with `lines`, `present`, `missing_files`, `missing_file_count`, `absorbed`), and the document lists `archived` (`[{branch, oid, sha, committed_at, subject}]`, the pinned tips, `worktree/archive.py`). `pr_index` carries `stale`, `refreshing` and `truncated` (the `gh pr list` filled `branches.PR_LIST_CEILING`, so the oldest PRs may be missing; stale at any age). Never runs git on the request thread — see below. |
| `POST /fetch {}` | `202` + worker: `git fetch --prune origin` (never prompts), then `gh pr list` into the persisted PR index. Two steps, `FETCH_STEPS = ("fetch", "prs")`. A missing or logged-out `gh` is recorded on `pr_index.error`; the git half still counts. |
| `POST /delete-branch {branch, force?}` | Synchronous `git branch -d` (`-D` with `force`) after a **fresh** inline sweep. Refusal order: `400` bad name → `404` unknown → `409` base → `409` creating → `409` checked out (managed: `worktree_removable: true`) → landed: `200` (through `-D` when squash-landed — `-d` cannot see it) → `409` merged unknown (even with `force`) → `409 needs_force` with `ahead` → `200 {deleted, forced}`. A `force` on a branch not proven landed or contained pins its tip at `refs/tldrack/archived/<branch>` FIRST (pins under the older `refs/voidcraft/archived/` are still read: `worktree/archive.py`) and refuses (`500`, or `409 archive_conflict` on a divergent old pin) when it cannot; the body says `archived`. |
| `POST /restore-branch {branch}` | Recreates `refs/heads/<branch>` at its archived tip (`update-ref` with a zero old value, so never over an existing branch), then drops the pin. `400` name · `404` no pin · `409` base name or a branch of that name exists. |
| `POST /forget-archived {branch, sha}` | Drops one pin, only if it still holds `sha` (the full oid the caller was shown): `409` otherwise. Its commits become collectable. |
| `POST /prune-worktrees {}` | Synchronous `git worktree prune -v` → `200 {pruned: [admin names], count}`. Touches no directory and no branch. |
| `POST /remove-landed-worktree {branch}` | `git worktree remove` (never `--force`) for a tree **outside** the repository's worktree root whose branch is landed or contained, after a fresh sweep and fresh `git status` + `lsof` probes. Refusals (`landing.decide_outside_remove`): `400` name → `404` no tree → `409` main checkout · managed (use `/remove`) · creating · locked · not in main · unreadable status · uncommitted · unknown occupancy · a process inside it. Keeps the branch. `_managed()` and `/remove` are untouched. |
| `POST /brief {branch?}` | `202 {started, total}` + worker: one `claude -p --model haiku` per branch with no brief or a stale one (or the one named), each landing in the persisted brief store as it is made. `409` while the harbor is warming, `503` without `git` or `claude`. Progress on `/branches.brief`; rows gain `brief: {what, next, model, at, stale}`. Never `_managed()`-gated — a read-only annotation. `worktree/brief.py` owns the recipe: **$0.003–0.007 a call**, because the CLI's default context (~56k tokens, $0.11) is replaced, not because of the model. |
| `GET /ship-state` | The main checkout's SHIP: `{ok, ship: {phase (idle·shipping·open·syncing·failed), branch_now, head, dirty, ahead, branch, base, snapshot, title, pr: {number, url, state, checks, mergeable, merge_commit}, error, blocking, files, last}}`. Local probes on a 4 s TTL (`--no-optional-locks`); the PR summary refreshes off-thread every 20 s. See **SHIP** below. |
| `POST /ship {title?}` | `202 {ok, ship}` + worker: snapshot the WHOLE dirty tree as one commit on HEAD, push it to `ship/<yyyymmdd-hhmm>`, `gh pr create`. `409` off main · nothing dirty and nothing ahead · a ship already open or in flight; `503` without `gh`. |
| `POST /ship-sync` | `202 {ok, ship}` + worker: `gh pr merge` (squash, else merge, else rebase — whichever the repo allows), then bring `main` to `origin/main` keeping every edit made since the snapshot. `409` no open PR · checks failing or still running · conflicting · HEAD moved since SHIP (a refusing PR summary is re-read on the click, never trusted from the 20 s cache). A PR already MERGED on GitHub skips `gh pr merge`. A refusal mid-way is `phase: failed` with `blocking` paths; SYNC again resumes. |
| `POST /ship-cancel` | `200 {ok, ship}`: `gh pr close --delete-branch` and forget the ship. `409` while a job runs or once the PR merged. Never touches the tree. |

`/state` deliberately asks tmux **nothing**. The session name is a pure function
of the branch, and every client polling this is already polling `/snapshot`,
which carries live state for every session. Probing `tmux has-session` per
worktree here would be a second, slower, racier answer to a question the caller
can join locally for free.

### SHIP — the main checkout, through a PR

The operator's "iteration" commits go out as a PR instead of straight to `main`,
from a SHIP button, while every agent keeps editing the same checkout. SHIP never touches the working tree, the index or `main`, nothing is
ever stashed, and SYNC refuses rather than overwrite a dirty file: the
argument for all three is owned by `worktree/ship.py` — see the note there.
The record (`base` H, `snapshot` S, `branch`, `pr`) is persisted, so an open
ship survives a bridge restart; a job the restart killed loads as `failed`, and
SHIP or SYNC again picks up from wherever it stopped.

### The harbor's warming contract

`GET /branches` returns `{status: "warming", branches: []}` on a cold cache and
schedules **one** background sweep (single-flight); the client fast-polls until
`status: "ready"`. A stale document is served as-is while one refresh runs. A
sweep git cannot answer is still a `ready` document with `git_ok: false` and
`git_error` (`base branch main not found`), never an empty harbor. A runner that
raises keeps the last document, sets `error`, and backs off a full TTL —
`age_seconds` keeps counting from the last document that LANDED, so a wedged
git reads as stale, never as fresh.

| Knob | Value | Why |
|---|---|---|
| `BRANCHES_TTL_SECONDS` | 4 s | the same pollers as `/state` |
| `DIRTY_TTL_SECONDS` | 20 s | `git status` × every worktree is the only expensive tier |
| `DIRTY_SWEEP_BUDGET_SECONDS` | 15 s | what the budget did not reach is `dirty: null` — unknown, never clean. One background tier serves `/branches` and `/state` (`worktree/dirty.py`) |

`merged` is tri-state and is exactly `ahead == 0` against the **local** base —
what `git branch -d` checks; `null` refuses a delete even with `force`. `landed`
is the wider answer every verdict reads: `merged`, or a merged pull request whose
head IS this tip — a squash-merge, which `merged` cannot see. A merged PR alone
is not that (`branches.py::landed` owns the rules). `_managed()` remains the only write gate: `/branches`
lists every worktree with `managed` stamped, and `/remove`'s reach is unchanged.

Every path in a harbor GET body is `~`-collapsed (`names.display_path`), and so
is every error string in it — `git_error`, `error`, `fetch.error`,
`pr_index.error`, `pr_job.error`, `create_error` — because git's stderr is where
absolute paths hide once the path fields are collapsed (`names.scrub_home`,
applied where the body is assembled). `/state` stays byte-compatible (absolute
paths) except for its new key.

`delete_verdict` is `branches.decide_delete` asked per row with `force: false`,
and its first row is the branch NAME: a name the route would answer `400` to is
`refused` with the validator's reason, never `ok`.

That validator is `names.validate_existing_branch` — git's own rules
(`check-ref-format`) plus a leading-`-` ban for argv safety — and NOT
`validate_branch`, which is the create path's much narrower MINTING alphabet.
Holding the minting alphabet over an existing ref would strand a branch:
`fix-+-retry` is a legal ref git lists and `git branch -d` takes, but `+` is not
in `BRANCH_RE`, so the harbor would show it as an undeletable atoll.
Shell metacharacters are allowed on purpose — `create.run_bounded` passes a LIST
to `subprocess.run`, never a shell string.

`GET /branches` also carries `remote_branches`: every branch on a remote with no
local ref, swept once per remote so each is measured against its own
`<remote>/<base>`. `refs/remotes/<remote>/HEAD` is filtered on `%(symref)` — its
short name is the bare remote name and reads as a branch called `origin`. The
tier is READ-ONLY and carries no `delete_verdict`: `git push --delete` is a
network write this plugin does not implement. A failed remote is named in
`remote_error` and empties nothing.

## `wt` — the command line, and the Claude Code hooks

```bash
scripts/wt create <branch>          # git worktree add + the repository's hook → prints the path (reopens if it exists)
scripts/wt remove <branch> [--force]  # kills the session, removes the worktree, `git branch -d` if merged
scripts/wt path <branch>            # ~/worktrees/<repo>/<slug>
scripts/wt session <branch>         # wt-<slug>
scripts/wt trust [<sha256>]         # show the repository's bootstrap hook and its sha256, or pin it
scripts/wt hook-create              # WorktreeCreate hook: JSON on stdin → the path on stdout
scripts/wt hook-remove              # WorktreeRemove hook: JSON on stdin
```

`worktree/cli.py` is the one command every other door calls: a shell function, the `/worktree`
skill, and Claude Code's `WorktreeCreate` / `WorktreeRemove` hooks, which make `claude -w`,
`EnterWorktree` and `isolation: "worktree"` land in the managed root with the same hook. It works
in whatever repository the caller stands in. To install the hooks in a repository's
`.claude/settings.json`:

```json
{"hooks": {
  "WorktreeCreate": [{"hooks": [{"type": "command", "command": "/path/to/plugins/worktree/scripts/wt hook-create"}]}],
  "WorktreeRemove": [{"hooks": [{"type": "command", "command": "/path/to/plugins/worktree/scripts/wt hook-remove"}]}]
}}
```

`scripts/wt` only finds a Python ≥ 3.10 (the bridge venv, then `python3.x`); the plugin is
stdlib-only, so any will do. **stdout is the one answer** (a path), stderr is everything else: the
shell `cd`s into stdout and Claude Code reads its last line as the directory
(`worktree/cli.py` § The hook contract owns the details).

## Why the prompt is typed, not passed as an argument

`coordinator/resume.py` records that `shlex.quote` defeats the shell but not the
CLI, and answers it with an `=`-attached flag value. That answer does not
transfer to a first-turn prompt, because a prompt is a **positional**, and
`claude` resolves its first operand against its **subcommands** first. Measured:

```
$ claude -- doctor
Claude Code doctor
Running: native (2.1.251)     ← ran the SUBCOMMAND, straight through the `--`
```

Ordinary task prompts collide with that: *"install the eslint plugin"*, *"import
the CSV"*, *"rm the dead file"*, *"project structure notes"* all begin with a real
subcommand name, and no amount of quoting helps — the string is a well-formed
operand either way.

So the prompt is delivered with `tmux send-keys -l`, which **types** it into the
input box Claude has already drawn. No shell, no option parser, no subcommand
resolution. The cost is a readiness race, and its failure mode is chosen to be
harmless: if the pane never looks ready we do not send, and say so. A task pasted
into a bare shell is the outcome worth paying to avoid.

Typed is not sent. The Enter waits until the pane shows the prompt in the input
box, is pressed only on that screen, and counts only once the prompt has left the
box; a prompt that stays there is logged as typed but not submitted. Owned by
`worktree/create.py` § SUBMITTED — see the note there.

## Settings

`plugins.worktree` in the operator's `settings.json` (the seam: `plugins/README.md`):

| Key | Default | What |
|---|---|---|
| `repos` | the repository the bridge starts in | Paths, or `{"path", "label"}` objects, at most 16. The first answers a request that names none. |

```json
{"plugins": {"worktree": {"repos": ["~/code/my-app", {"path": "~/code/tldrack", "label": "rack"}]}}}
```

The PR verbs need GitHub's CLI (`gh`). Without it they refuse with a sentence,
`capabilities.gh` reads `false` so a client hides them, and every other route works.

## Environment

| Env var | Default |
|---|---|
| `WORKTREE_ROOT` | `~/worktrees` — the parent of every repository's worktree directory |
| `WORKTREE_BASE_BRANCH` | unset: the repository's `.tldrack/worktree.json` `base`, else `main`, else the main checkout's branch |
| `WORKTREE_CLAUDE_BIN` | whatever `claude` resolves to on `PATH` |
| `TLDRACK_WORKTREE_PATH` | `~/.local/state/tldrack/worktree-prs.json`; each repository's state sits beside it under `worktree/<key>/` |
| `TLDRACK_PR_INDEX_PATH` | `worktree-pr-index.json` beside the PR store — the persisted `gh pr list` index, filled by `POST /fetch` AND by the plugin itself while the harbor is polled (every 10 min, and after a terminal `git fetch`) |
| `TLDRACK_PATCH_ID_PATH` | `worktree-patch-ids.json` beside the PR index — the content tier's patch-ids, per commit and per diff (immutable, so never invalidated) |
| `TLDRACK_SHIP_PATH` | `worktree-ship.json` beside the PR store — the main checkout's open ship (H, S, branch, PR) and the last SYNC |
| `TLDRACK_BRIEF_PATH` | `worktree-briefs.json` beside the PR store — the persisted briefs, one entry per branch, stamped with the tips they were written against |
| `WORKTREE_BRIEF_MODEL` | `haiku` — an alias on purpose; each entry records the name the CLI resolved |
| `WORKTREE_BRIEF_MAX_CALLS` / `WORKTREE_BRIEF_MAX_COST_USD` | 64 / 0.75 per `POST /brief` run — reaching either ends the run with a note, keeping what was made. Per call, the CLI's own `--max-budget-usd 0.05` |

Worktrees live outside the repository and outside any directory another tool
writes worktrees into (`worktree/repo.py` owns why). `is_managed_path()`
enforces containment per repository, which keeps other tools' worktrees, and
other repositories', out of this plugin's list and out of `POST /remove`'s reach.

## Install

```bash
# register with the bridge
$EDITOR ~/.config/tldrack/plugins.json     # add this directory
# then restart the bridge and confirm:
curl -s localhost:7682/snapshot | jq .plugins.worktree
```

Plugins load **once**: after editing this plugin, restart the bridge. Then:

```bash
curl -s localhost:7682/plugin/worktree/branches | jq '.status, (.branches|length), (.worktrees|length)'
curl -s localhost:7682/plugin/worktree/branches | grep -c '/Users/'    # 0
```

The PR button and the fetch's PR half additionally need `gh`:

```bash
brew install gh && gh auth login
```

## Tests

```bash
uv sync && uv run pytest -q          # every test fences its subprocesses, or runs git in a tmp dir
```

Everything decidable is a pure function — the argv builders, the refusal tables,
the porcelain parser, the readiness predicate — so the suite spawns nothing.
