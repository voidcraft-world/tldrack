/**
 * The ONE owner of `/plugin/worktree/*`.
 *
 * More than one surface drives worktree sessions: the rack page
 * (`rack-http-transport.ts`) and a host application's own payload owner. They
 * have separate `send` implementations; without this module they would grow two
 * separate copies of the plugin protocol as well, which is the duplication the
 * transport seam exists to prevent.
 *
 * DELIBERATELY FREE of the DOM, of `three` and of any host state: unit tests
 * run under bare `node --test` in strip-only mode. Everything configurable
 * arrives as a dep.
 */

// TYPE-ONLY: a type import is erased outright, so nothing here depends on
// `rack-view` at runtime. The type lives there because `RackPayload` carries it
// and that module imports nothing but pure siblings.
import type {
    BranchLanding,
    BranchLandingHow,
    CreateStep,
    WorktreeIsland,
    WorktreeRow,
} from '../engine/terminal/rack-view.ts';
// A real value import, unlike the type-only one above, and allowed under the
// same rule: the gate guards every browser global, so it loads under
// `node --test` exactly as this file does. Every plugin call below routes
// through it; see `local-network-gate.ts`.
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
// Dependency-free like the gate, so it loads under `node --test` too. The ONE
// profile-id rule, shared with the snapshot parser.
import { parse_profile_id } from '../engine/terminal/terminal-status-logic.ts';

import { parse_task_groups, type TaskGroup } from './task-groups.ts';
export type { BranchLanding, BranchLandingHow, WorktreeRow };

export interface WorktreeState {
    task_groups?: TaskGroup[];
    worktrees: WorktreeRow[];
    root: string;
    base: string;
    /**
     * Every NON-main worktree path on the machine, `~`-collapsed and unfiltered
     * — the managed rows above are a subset. Feeds `rack-view.ts::
     * attach_only_sessions`, which is where the reasoning lives. Absent on a
     * plugin that predates the key, and then the rack behaves exactly as before.
     */
    worktree_paths?: string[];
    /**
     * The same trees with their branches — the rack row's island
     * (`rack-view.ts::island_for_session`). Built from `worktree_paths` with no
     * branch when the plugin predates the key, so an un-restarted bridge still
     * draws islands, labelled by folder. Absent exactly when `worktree_paths` is.
     */
    worktree_trees?: WorktreeIsland[];
}

/** What `+ WORKTREE` submits. `value` on the transport carries this as JSON. */
export interface WorktreeCreateRequest {
    branch: string;
    prompt?: string;
    /** Check out a branch that ALREADY exists (no `-b`, no base). The plugin
     *  refuses 404 for an unknown branch and 409 for one any worktree holds. */
    existing?: boolean;
    /** The launch profile to spawn in the tree (the rack's LAUNCH chip). Absent
     *  → the bridge's default profile, which is also what an older plugin does
     *  with it (ignores the key and spawns `claude`). */
    profile?: string;
}

// ── the harbor document: what `GET /branches` ships ──────────────────────────
//
// TS mirrors of plugins/worktree/worktree/branches.py's rows. Every
// judgement here was made ONCE, server-side: `merged` is exactly `ahead === 0`
// against the LOCAL base (what `git branch -d` checks), `landed` is whether the
// work is on the base at all (squash-merges included — `branches.py::landed`),
// and `delete_verdict` is the refusal table the route will actually honour —
// the page re-derives nothing, which is what keeps the UI from promising a
// delete the plugin refuses.

export interface BranchUpstream {
    /** `origin/x` */
    ref: string;
    remote: string;
    /** The remote branch's short name — `refs/heads/` already stripped, so
     *  `kind: 'base'` can be trusted. */
    branch: string;
    /** `own` = same name on the remote · `base` = the base branch itself (the
     *  numbers duplicate the vs-main read) · `other` = anything else. */
    kind: 'own' | 'base' | 'other';
    ahead: number;
    behind: number;
    /** `%(upstream:track)` said `[gone]` — the remote ref was deleted. */
    gone: boolean;
}

export interface BranchWorktree {
    /** `~`-collapsed. */
    path: string;
    /** Under the plugin's own root — the only kind `/remove` will touch. */
    managed: boolean;
    is_main: boolean;
    /** null = not probed yet / probe failed. Render as UNKNOWN, never clean. */
    dirty: boolean | null;
    prunable: boolean;
    locked: boolean;
    /** Additive — unique command names (≤ 3) of processes whose cwd is inside
     *  this tree, on the dirty tier. null = not probed (and an older plugin);
     *  `[]` = nobody. */
    occupied_by?: string[] | null;
}

/** The patch-id tier for a row: `pending` = not measured yet, which reads as
 *  unknown and refuses, as every unknown does. */
export type BranchContent = 'measured' | 'pending' | 'skipped';

export interface BranchLedgerCommit {
    /** Short sha. */
    sha: string;
    subject: string;
    /** Its patch is on the base or in a merged PR. */
    found: boolean;
    /** `title` = a base commit is titled `<subject> (#pr)` — a HINT, never
     *  proof; `pr_patch` = the patch is in merged PR #pr's head range. */
    hint: { pr: number; why: 'title' | 'pr_patch' } | null;
}

/** "What would a `-D` lose?" — a not-landed, not-contained row's own commits, split. */
export interface BranchLedger {
    found: number;
    missing: number;
    /** ≤ 6: missing first, then newest first. */
    commits: BranchLedgerCommit[];
}

/**
 * The EVIDENCE tier for an unproven row: how much of its work the base
 * already holds, measured by content (`worktree/absorption.py` owns the
 * measurement and the threshold). Never a proof, so it never makes a row
 * `landed`; `absorbed` is the plugin's verdict, read and never re-derived.
 * Absent / null where it does not apply (the base, a landed or contained row,
 * nothing ahead, content proofs still pending) and from an older plugin.
 */
export type BranchAbsorption =
    | { status: 'pending' }
    | { status: 'too_big' }
    | {
        status: 'measured';
        /** Lines that voted: the branch's net additions of `MIN_LINE_CHARS`+, data files excluded. */
        lines: number;
        /** Of those, the ones the base holds. */
        present: number;
        /** ≤ 6 paths the branch added that the base never had; the count says the rest. */
        missing_files: string[];
        missing_file_count: number;
        absorbed: boolean;
    };

/** One pinned tip under `refs/tldrack/archived/` (or the older `refs/voidcraft/archived/`) — what a FORCED delete
 *  left behind so it can be undone (`worktree/archive.py`). */
export interface ArchivedTip {
    branch: string;
    /** Full object id — FORGET sends it back, so the press forgets exactly this tip. */
    oid: string;
    sha: string;
    committed_at: number | null;
    subject: string;
}

export interface BranchPr {
    number: number;
    state: 'open' | 'merged' | 'closed';
    url: string;
    draft: boolean;
    merged_at: string | null;
    /** Additive — the PR's head commit, full id. Read by the plugin's `landed`
     *  only; nothing on this side compares it. */
    head_oid?: string | null;
}

export interface BranchPrJob {
    status: 'opening' | 'open' | 'error';
    url: string | null;
    error: string | null;
}

/** The adopt anchor (`refs/tldrack/adopted/<branch>`; older ones `refs/voidcraft/…`), as the plugin ships it on a
 *  row. `unpreserved` is NOT here — it costs a whole-tree snapshot and rides
 *  the pulled `/changes` answer instead. */
export interface BranchAdopted {
    at: number | null;
    files: string[];
    ref: string | null;
}

export interface BranchRow {
    branch: string;
    sha: string;
    /** `wt-<slug>` — the join key against a snapshot row, and the name `/create`
     *  would use. Derived by the plugin; nothing here parses a name back. */
    session: string;
    is_base: boolean;
    /** Unix seconds. */
    committed_at: number;
    author: string;
    subject: string;
    /** vs the LOCAL base; null when the plugin's git lacks `%(ahead-behind:)`. */
    ahead: number | null;
    behind: number | null;
    /** Tri-state: exactly `ahead === 0`; null when `ahead` is null. */
    merged: boolean | null;
    /** Additive — the work is on the base: `merged`, a merged PR whose head IS
     *  this tip (a squash-merge, which `merged` cannot see), or a patch-id
     *  proof (`landing.how`). Absent from an older plugin, and absent reads as
     *  false — never as landed. */
    landed?: boolean;
    upstream: BranchUpstream | null;
    worktree: BranchWorktree | null;
    pr: BranchPr | null;
    pr_job: BranchPrJob | null;
    create_status?: 'creating' | 'error' | null;
    create_step?: CreateStep | string | null;
    create_error?: string | null;
    delete_verdict: 'ok' | 'needs_force' | 'refused';
    delete_reason: string | null;
    /** The refusal is a MANAGED checkout — REMOVE WORKTREE is the way out. */
    worktree_removable?: true;
    /**
     * The triage fields — additive, and every absent one reads as the refusing
     * answer (`normalize_triage` owns the defaults), so an older plugin draws
     * exactly the harbor it always did.
     *
     * `landing` says how `landed` was proven (null whenever it is false);
     * `contained_in` names the kept local branch holding every commit of this
     * one, which is therefore safe to delete though not landed; `ledger` is
     * the split a `-D` would lose; `worktree_removable_outside` says `POST
     * /remove-landed-worktree` would clear on the cached facts.
     */
    landing?: BranchLanding | null;
    contained_in?: string | null;
    content?: BranchContent;
    ledger?: BranchLedger | null;
    worktree_removable_outside?: true;
    /** Additive — absent from a bridge running the older plugin. */
    absorption?: BranchAbsorption | null;
    /** Additive — absent from a bridge running the older plugin. */
    adopted?: BranchAdopted | null;
    /** Additive — absent from a bridge running the older plugin. */
    brief?: BranchBrief | null;
}

/**
 * A branch that exists on a REMOTE and has no local ref at all.
 *
 * `for-each-ref refs/heads/` is local refs, so without this tier these are
 * invisible to the harbor. On a busy repository they outnumber the local
 * branches, and nearly all of them are merged PRs whose branch was never
 * deleted upstream.
 *
 * Deliberately NOT a `BranchRow`. There is no `session` (nothing to attach to),
 * no `worktree` (nothing is checked out) and no `delete_verdict`: deleting one
 * of these is `git push <remote> --delete`, a network write that other people
 * can see, and the plugin does not implement it. A verdict field here would
 * promise a verb that does not exist.
 */
export interface RemoteBranchRow {
    /** `origin/feat-nav` — what you would type. */
    ref: string;
    remote: string;
    /** The name with no remote prefix; the join key for `pr`. */
    branch: string;
    sha: string;
    /** Unix seconds. */
    committed_at: number;
    author: string;
    subject: string;
    /** vs `<remote>/<base>`, NOT the local base: "has this landed upstream?" */
    ahead: number | null;
    behind: number | null;
    /** Tri-state, exactly `ahead === 0`; null when the sweep could not measure. */
    merged: boolean | null;
    /** Additive — `BranchRow.landed`, against `<remote>/<base>`. */
    landed?: boolean;
    pr: BranchPr | null;
    /** May `git push --delete` take this? `'clear'` or `'refused'`, and no
     *  third answer: unlike `delete_verdict` there is no force to unlock.
     *  `'refused'` against a plugin that predates the verb. */
    scuttle_verdict: 'clear' | 'refused';
    /** Why it refused — null when clear. */
    scuttle_reason: string | null;
}

export interface WorktreeInfo {
    path: string;
    branch: string | null;
    head: string;
    detached: boolean;
    managed: boolean;
    is_main: boolean;
    dirty: boolean | null;
    prunable: boolean;
    prunable_reason: string | null;
    locked: boolean;
}

/**
 * The two steps `POST /fetch` walks, in order: `git fetch --prune`, then the
 * `gh pr list` index. Re-declared from the plugin's `harbor.py::FETCH_STEPS`
 * for the reason `CREATE_STEPS` gives in `rack-view.ts` — two languages either
 * side of HTTP — and pinned by a test on each side.
 */
export const FETCH_STEPS = ['fetch', 'prs'] as const;

export type FetchStep = (typeof FETCH_STEPS)[number];

export interface HarborFetch {
    status: 'idle' | 'running' | 'error';
    step: FetchStep | string | null;
    error: string | null;
    /** Unix seconds — the mtime of `.git/FETCH_HEAD`, so a CLI fetch counts. */
    at: number | null;
}

/**
 * A branch's BRIEF — one sentence and the next steps, from a cheap model on
 * the bridge (`plugins/worktree/worktree/brief.py` owns the recipe
 * and the reasons). `stale` = the branch or the base moved since it was
 * written; the text is still shown, dimmed, until RE-BRIEF.
 */
export interface BranchBrief {
    what: string;
    next: string[];
    /** The model name the CLI resolved (`claude-haiku-4-5-…`), or null. */
    model: string | null;
    /** Unix seconds. Never in a signature — derive the age on a tick. */
    at: number | null;
    stale: boolean;
}

/** The ONE brief job, `/branches.brief` — `HarborFetch` with progress and money. */
export interface HarborBrief {
    status: 'idle' | 'running' | 'done' | 'error';
    /** The branch being briefed while running. */
    step: string | null;
    done: number;
    total: number;
    /** The running sum of the CLI's own `total_cost_usd` estimates. */
    cost_usd: number;
    /** How a capped run ended — not an error, what it made is kept. */
    note: string | null;
    error: string | null;
    /** Unix seconds the last run ended. */
    at: number | null;
    /** Briefs on disk. */
    count: number;
    /** The model alias the plugin will call (`haiku`). */
    model: string;
}

export interface HarborState {
    /** `warming` = cold cache, a background sweep is scheduled — poll fast. */
    status: 'warming' | 'ready';
    generated_at: number | null;
    age_seconds: number | null;
    /** The last refresh's failure; the last good document is still served. */
    error: string | null;
    /** false + `git_error` (`base branch main not found`, `git < 2.41`) is an
     *  ERROR harbor, never an empty one. null while warming. */
    git_ok: boolean | null;
    git_error: string | null;
    base: string;
    pr_remote: string;
    capabilities: { ahead_behind: boolean };
    /** The primary checkout, `~`-collapsed. */
    repo: string;
    stash_count: number;
    fetch: HarborFetch;
    /** The PR list every `pr` landing proof reads. The plugin refreshes it
     *  itself (every 10 min while polled, and after a CLI fetch); `stale` =
     *  over an hour old, failing, cut, or never fetched — false from an older
     *  plugin, which cannot say. */
    pr_index: {
        fetched_at: number | null;
        count: number;
        error: string | null;
        stale: boolean;
        refreshing: boolean;
        /** The list filled the plugin's ceiling, so its oldest PRs may be missing
         *  (`branches.py::PR_LIST_CEILING` owns why) — false from an older plugin. */
        truncated: boolean;
    };
    brief: HarborBrief;
    branches: BranchRow[];
    /** Remote-only branches — `[]` against a plugin that predates the tier. */
    remote_branches: RemoteBranchRow[];
    /** The remote sweep's failure, per remote. Never empties `branches`: the
     *  tier is additive and must not take the document down with it. */
    remote_error: string | null;
    worktrees: WorktreeInfo[];
    /** Pinned tips a forced delete left, newest commit first — `[]` from an older plugin. */
    archived: ArchivedTip[];
}

const STATE_TIMEOUT_MS = 2500;
/**
 * Deliberately still generous, though `/create` now answers 202 in milliseconds.
 *
 * It costs nothing against a current plugin, and it is the entire compatibility
 * story for one that has not been restarted: the OLD route did `git worktree
 * add` + bootstrap + spawn before answering, and tightening this would turn a
 * slow-but-working bridge into a client-side failure. The number is smaller
 * than the plugin's own 435 s budget either way, so a timeout here has always
 * meant "the browser gave up", never "the work stopped" — which is precisely
 * the mismatch the async route exists to retire.
 */
const CREATE_TIMEOUT_MS = 90_000;
const ACTION_TIMEOUT_MS = 10_000;
/**
 * `POST /scuttle-remote`. Above the plugin's own `SCUTTLE_TIMEOUT_SECONDS`
 * (30) on purpose: this is the one SYNCHRONOUS network write, and a client
 * that gave up first would report a failure for a delete that landed — the
 * exact mismatch `CREATE_TIMEOUT_MS`'s note is about.
 */
const SCUTTLE_TIMEOUT_MS = 35_000;
/** `GET /changes` walks the whole working tree (`status -uall`) and may read a
 *  patch on top — a cold worktree earns more than a polled route's 2.5 s. */
const CHANGES_TIMEOUT_MS = 25_000;

/**
 * How fast `/state` is polled while a create is in flight.
 *
 * The idle cadence is the rack's 5 s `worktree_poll_ms`, which is far too slow to
 * narrate a four-step ladder — the label would sit still for most of each step
 * and read as stuck. An adaptive poll: fast while something is live, back to
 * slow the moment nothing is.
 */
export const CREATE_POLL_INTERVAL_MS = 1000;

export type BaseUrl = (path: string) => string;
export type FetchImpl = (...args: Parameters<typeof fetch>) => Promise<Response>;

function is_row(value: unknown): value is WorktreeRow {
    if (!value || typeof value !== 'object') return false;
    const row = value as Record<string, unknown>;
    return typeof row.branch === 'string' && typeof row.session === 'string';
}

/**
 * `GET /state`, or `null` when the plugin is not mounted.
 *
 * `null` is not an error case — it is the overwhelmingly common one. A machine
 * whose bridge has no `worktree` plugin registered must grow no worktree UI at
 * all. The gating is structural: no flag, just an absent surface.
 */
export async function fetch_worktree_state(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<WorktreeState | null> {
    try {
        const response = await fetch_impl(base_url('/plugin/worktree/state'), {
            signal: AbortSignal.timeout(STATE_TIMEOUT_MS),
        });
        if (!response.ok) return null;
        const body = (await response.json()) as Partial<WorktreeState>;
        if (!Array.isArray(body.worktrees)) return null;
        const task_groups = parse_task_groups(body.task_groups);
        const can_clean = can_clean_up(body);
        const state: WorktreeState = {
            worktrees: body.worktrees.filter(is_row).map((row) => with_cleanup(row, can_clean)),
            ...(task_groups !== undefined ? { task_groups } : {}),
            root: typeof body.root === 'string' ? body.root : '',
            base: typeof body.base === 'string' ? body.base : 'main',
        };
        // Copied only when the plugin sent it: absent stays absent, so an
        // un-restarted plugin leaves `RackPayload.attach_only` off entirely.
        if (Array.isArray(body.worktree_paths)) {
            state.worktree_paths = body.worktree_paths.filter(
                (path): path is string => typeof path === 'string',
            );
            const pr_known = (body as { pr_known?: unknown }).pr_known === true;
            state.worktree_trees = Array.isArray(body.worktree_trees)
                ? body.worktree_trees.filter(is_island).map((tree) => with_pr(tree, pr_known))
                : state.worktree_paths.map((path) => ({ path }));
        }
        return state;
    } catch {
        return null;
    }
}

/**
 * A tree's `pr` as the island reads it (`WorktreeIsland.pr`): the plugin's `{number, state, url}`, `none` for its
 * null once the index has been fetched (`pr_known`), and left out while nothing is known, never guessed. Its
 * `landing` rides along when the plugin sent a well-formed one (`island_pr` reads it first), and is dropped
 * otherwise.
 */
function with_pr(tree: WorktreeIsland, pr_known: boolean): WorktreeIsland {
    const raw = (tree as { pr?: unknown }).pr;
    const { pr: _dropped, landing: _landing, ...base } = tree as WorktreeIsland & { pr?: unknown; landing?: unknown };
    const landing = tree_landing((tree as { landing?: unknown }).landing);
    const rest: WorktreeIsland = landing ? { ...base, landing } : base;
    if (raw && typeof raw === 'object') {
        const record = raw as Record<string, unknown>;
        if (record.state === 'open' || record.state === 'merged' || record.state === 'closed') {
            return {
                ...rest,
                pr: {
                    state: record.state,
                    number: typeof record.number === 'number' ? record.number : null,
                    url: typeof record.url === 'string' ? record.url : null,
                },
            };
        }
    }
    return pr_known ? { ...rest, pr: { state: 'none', number: null, url: null } } : rest;
}

/** One tree's `landing` from `/state`: `{how, pr}` with a known `how`, else null. */
function tree_landing(raw: unknown): BranchLanding | null {
    if (!raw || typeof raw !== 'object') return null;
    const landing = raw as Record<string, unknown>;
    return typeof landing.how === 'string' && LANDING_HOWS.has(landing.how)
        ? { how: landing.how as BranchLandingHow, pr: pr_number_or_null(landing.pr) }
        : null;
}

/** `capabilities.cleanup` on the `/state` document: the bridge has `POST /cleanup`. */
function can_clean_up(body: unknown): boolean {
    const caps = (body as { capabilities?: unknown }).capabilities;
    return !!caps && typeof caps === 'object' && (caps as Record<string, unknown>).cleanup === true;
}

/** `cleanup_error` is drawn into a title; the plugin's messages are one line. */
const CLEANUP_ERROR_CAP = 300;

/**
 * A managed row's CLEAN fields (`WorktreeRow.landed` and the three after it),
 * re-validated — and DROPPED when the document does not declare
 * `capabilities.cleanup`. That is the structural gate: a bridge that cannot run
 * `POST /cleanup` never hands the rack a reason to offer it, so the button stays
 * PR. Each field present is parsed to its type or null; absent stays absent
 * (unknown, never false). `landing` rides a landed row only, as on `/branches`
 * (`normalize_triage`).
 */
function with_cleanup(row: WorktreeRow, can_clean: boolean): WorktreeRow {
    const { landed: _landed, landing: _landing, cleanup_status: _status, cleanup_error: _error, ...rest } = row;
    if (!can_clean) return rest;
    const out: WorktreeRow = rest;
    const raw = row as unknown as Record<string, unknown>;
    if ('landed' in raw) out.landed = typeof raw.landed === 'boolean' ? raw.landed : null;
    if ('landing' in raw) {
        const landing = (raw.landing && typeof raw.landing === 'object' ? raw.landing : {}) as Record<string, unknown>;
        out.landing = out.landed === true && typeof landing.how === 'string' && LANDING_HOWS.has(landing.how)
            ? { how: landing.how as BranchLandingHow, pr: pr_number_or_null(landing.pr) }
            : null;
    }
    if ('cleanup_status' in raw) {
        out.cleanup_status = raw.cleanup_status === 'running' || raw.cleanup_status === 'error'
            ? raw.cleanup_status : null;
    }
    if ('cleanup_error' in raw) {
        out.cleanup_error = typeof raw.cleanup_error === 'string' && raw.cleanup_error.trim() !== ''
            ? raw.cleanup_error.trim().slice(0, CLEANUP_ERROR_CAP) : null;
    }
    return out;
}

/** One `/state.worktree_trees` record. `branch` is a string or null (detached);
 *  anything else drops the record rather than guessing. */
function is_island(value: unknown): value is WorktreeIsland {
    if (typeof value !== 'object' || value === null) return false;
    const record = value as Record<string, unknown>;
    return typeof record.path === 'string' && record.path !== ''
        && (record.branch === null || typeof record.branch === 'string');
}

export interface ActionResult {
    ok: boolean;
    /** Present on failure — the plugin's own message, written to be read in a
     *  rack row rather than a log. */
    error?: string;
    session?: string;
    /**
     * The HTTP status, so a caller can tell the two successes apart: 202 means
     * a worker is now building this branch and `/state` will narrate it, while
     * 200 means the session was already there and nothing is in flight. The
     * pending row must not be raised for a 200, or it would sit at step one
     * until it timed out.
     *
     * Absent on a transport failure, where there was no answer to read.
     */
    status?: number;
    /**
     * Why the first task was NOT typed into the agent, when the plugin says so
     * (`prompt_queued: false` + `prompt_reason`). Only a `claude` profile gets
     * its task typed: a Codex first launch can open on a trust list whose
     * marker is the same `›` as its composer, and a typed task plus Enter there
     * would pick an option. Absent whenever the task was queued or none given.
     */
    prompt_note?: string;
}

/**
 * One POST, with whatever the plugin answered kept on the result.
 *
 * `body` is the parsed JSON of the answer on BOTH branches — a refusal carries
 * more than its `error` line (`/delete-branch` answers 409 with `needs_force`
 * and the unlanded count, `/prune-worktrees` answers 200 with the names it
 * removed), and the route wrappers below read those fields off it rather than
 * each re-fetching. Empty when the answer was not JSON, or there was none.
 */
interface PostOutcome extends ActionResult {
    body: Record<string, unknown>;
}

async function read_body(response: Response): Promise<Record<string, unknown>> {
    try {
        const parsed: unknown = await response.json();
        return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
    } catch {
        return {};
    }
}

async function post(
    base_url: BaseUrl,
    fetch_impl: FetchImpl,
    path: string,
    body: unknown,
    timeout_ms: number,
    fallback: string,
): Promise<PostOutcome> {
    try {
        const response = await fetch_impl(base_url(`/plugin/worktree${path}`), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(timeout_ms),
        });
        const parsed = await read_body(response);
        if (!response.ok) {
            // The plugin's error bodies are `{error: string}`; anything else is
            // a bug on our side and must not be shown as if the plugin said it.
            return { ok: false, status: response.status, body: parsed,
                     error: typeof parsed.error === 'string' ? parsed.error : fallback };
        }
        const outcome: PostOutcome = { ok: true, status: response.status, body: parsed,
                 session: typeof parsed.session === 'string' ? parsed.session : undefined };
        if (parsed.prompt_queued === false) {
            outcome.prompt_note = typeof parsed.prompt_reason === 'string' && parsed.prompt_reason.trim() !== ''
                ? parsed.prompt_reason.trim().slice(0, 200)
                : 'first task not typed — paste it into the agent';
        }
        return outcome;
    } catch {
        return { ok: false, error: fallback, body: {} };
    }
}

/** The public shape: every route wrapper hands back `ActionResult`, never the
 *  raw body — what a caller may read is declared on its own result type. */
function action_result(outcome: PostOutcome): ActionResult {
    const { body: _body, ...result } = outcome;
    return result;
}

export async function create_worktree(
    base_url: BaseUrl,
    request: WorktreeCreateRequest,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/create', request,
        CREATE_TIMEOUT_MS, 'could not create the worktree'));
}

/**
 * The line a create leaves on the rack's notice strip, or null.
 *
 * A refusal answers SYNCHRONOUSLY (a branch that already exists is a 409).
 * Dropped, it makes the press read as a dead button. The 🏝 launch names
 * branches itself (`auto-branch.ts`), which makes that refusal an ordinary
 * outcome and not only a typo's.
 */
export function create_notice(result: ActionResult): string | null {
    if (!result.ok) return `worktree not created — ${result.error ?? 'could not create the worktree'}`;
    return result.prompt_note ?? null;
}

export async function open_pr(
    base_url: BaseUrl,
    branch: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    // 202 is the success here: the plugin pushes and calls `gh` on a background
    // thread, and reports the outcome through `/state`.
    return action_result(await post(base_url, fetch_impl, '/pr', { branch },
        ACTION_TIMEOUT_MS, 'could not start the PR'));
}

/** `POST /cleanup`'s body. */
export interface WorktreeCleanupRequest {
    branch: string;
    /** The pressed row's session, sent ONLY when it is not the tree's derived
     *  one — a `+ NEW` pane that ran `EnterWorktree`, joined by cwd
     *  (`rack-view.ts::worktree_for_session`). The worker kills it too. */
    session?: string;
}

/** CLEAN on the rack row `session`, whose tree is `row`. */
export function cleanup_request(row: WorktreeRow, session: string): WorktreeCleanupRequest {
    return session === row.session ? { branch: row.branch } : { branch: row.branch, session };
}

/**
 * `POST /cleanup` — kill the session(s), `git worktree remove` (never forced),
 * delete the branch. 202 is the success: the worker re-checks `landed` and the
 * dirt FRESH, refuses rather than forces, and narrates through `/state`'s
 * `cleanup_status`. A synchronous refusal (no managed tree, a create or a
 * clean-up already running) is an answer, carried on the result.
 */
export async function clean_up_worktree(
    base_url: BaseUrl,
    request: WorktreeCleanupRequest,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/cleanup', request,
        ACTION_TIMEOUT_MS, 'could not start the clean-up'));
}

/** The notice strip's line for a CLEAN press, or null: like `create_notice`, so a
 *  refused press never reads as a dead button. */
export function cleanup_notice(result: ActionResult): string | null {
    return result.ok ? null : `not cleaned up — ${result.error ?? 'no answer from the bridge'}`;
}

export interface RemoveWorktreeResult extends ActionResult {
    /**
     * The plugin refused a tree holding uncommitted work (409). Re-arm with
     * `force` — and SHOW what that destroys first (the CHANGES fold).
     *
     * Without this flag the forced remove is unreachable from the UI: a panel
     * re-arms on it, so a caller that never sees it can only show git's raw
     * `fatal: … use --force to delete it` and no button that would ever pass
     * `--force`. `delete_branch` below reads its own copy of this field; the
     * two routes answer the same shape and must parse it the same way.
     */
    needs_force?: boolean;
}

export async function remove_worktree(
    base_url: BaseUrl,
    branch: string,
    force: boolean,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<RemoveWorktreeResult> {
    const outcome = await post(base_url, fetch_impl, '/remove', { branch, force },
        ACTION_TIMEOUT_MS, 'could not remove the worktree');
    const result: RemoveWorktreeResult = action_result(outcome);
    if (outcome.body.needs_force === true) result.needs_force = true;
    return result;
}

// ── the harbor routes ────────────────────────────────────────────────────────

function is_branch_row(value: unknown): value is BranchRow {
    if (!value || typeof value !== 'object') return false;
    const row = value as Record<string, unknown>;
    return typeof row.branch === 'string'
        && typeof row.session === 'string'
        && typeof row.delete_verdict === 'string';
}

function is_remote_branch_row(value: unknown): value is RemoteBranchRow {
    if (!value || typeof value !== 'object') return false;
    const row = value as Record<string, unknown>;
    return typeof row.ref === 'string' && typeof row.branch === 'string'
        && typeof row.remote === 'string';
}

/** What a plugin too old to know the verb gets told, once the row is drawn. */
const OLD_PLUGIN_SCUTTLE_REASON = 'this bridge cannot delete remote branches — restart it';

/**
 * The verdict, defaulted for a plugin that predates it.
 *
 * Kept out of the guard so the guard stays a shape test. A plugin older than
 * the verb ships neither field, and the default has to be `'refused'`: reading
 * a missing answer as `'clear'` would offer a delete against a bridge with no
 * route to serve it — the same rule `merged === null` follows everywhere else.
 */
function with_scuttle_verdict(row: RemoteBranchRow): RemoteBranchRow {
    if (row.scuttle_verdict === 'clear') return { ...row, scuttle_reason: null };
    const known = row.scuttle_verdict === 'refused';
    return {
        ...row,
        scuttle_verdict: 'refused',
        scuttle_reason: typeof row.scuttle_reason === 'string' ? row.scuttle_reason
            : (known ? null : OLD_PLUGIN_SCUTTLE_REASON),
    };
}

function is_worktree_info(value: unknown): value is WorktreeInfo {
    if (!value || typeof value !== 'object') return false;
    return typeof (value as Record<string, unknown>).path === 'string';
}

function number_or_null(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function string_or_null(value: unknown): string | null {
    return typeof value === 'string' ? value : null;
}

/**
 * `GET /branches`, or `null` when the plugin is not mounted or predates the
 * route — the same "absent grows no UI" rule as `fetch_worktree_state`, and
 * the same 2.5 s budget, which is safe only because the route NEVER does work
 * on the request thread: a cold cache answers `warming` and a stale one answers
 * stale, so slow can never be mistaken for absent.
 *
 * Tolerant of a partial document (an older or newer plugin): a missing header
 * field takes its idle default and a malformed row is dropped rather than the
 * whole harbor.
 */
export async function fetch_branches(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<HarborState | null> {
    try {
        const response = await fetch_impl(base_url('/plugin/worktree/branches'), {
            signal: AbortSignal.timeout(STATE_TIMEOUT_MS),
        });
        if (!response.ok) return null;
        const body: unknown = await response.json();
        if (!body || typeof body !== 'object') return null;
        const raw = body as Record<string, unknown>;
        if (!Array.isArray(raw.branches)) return null;
        const fetch_raw = (raw.fetch && typeof raw.fetch === 'object'
            ? raw.fetch : {}) as Record<string, unknown>;
        const pr_raw = (raw.pr_index && typeof raw.pr_index === 'object'
            ? raw.pr_index : {}) as Record<string, unknown>;
        const brief_raw = (raw.brief && typeof raw.brief === 'object'
            ? raw.brief : {}) as Record<string, unknown>;
        const caps = (raw.capabilities && typeof raw.capabilities === 'object'
            ? raw.capabilities : {}) as Record<string, unknown>;
        return {
            status: raw.status === 'warming' ? 'warming' : 'ready',
            generated_at: number_or_null(raw.generated_at),
            age_seconds: number_or_null(raw.age_seconds),
            error: string_or_null(raw.error),
            git_ok: typeof raw.git_ok === 'boolean' ? raw.git_ok : null,
            git_error: string_or_null(raw.git_error),
            base: typeof raw.base === 'string' ? raw.base : 'main',
            pr_remote: typeof raw.pr_remote === 'string' ? raw.pr_remote : 'origin',
            capabilities: { ahead_behind: caps.ahead_behind === true },
            repo: typeof raw.repo === 'string' ? raw.repo : '',
            stash_count: number_or_null(raw.stash_count) ?? 0,
            fetch: {
                status: fetch_raw.status === 'running' || fetch_raw.status === 'error'
                    ? fetch_raw.status : 'idle',
                step: string_or_null(fetch_raw.step),
                error: string_or_null(fetch_raw.error),
                at: number_or_null(fetch_raw.at),
            },
            pr_index: {
                fetched_at: number_or_null(pr_raw.fetched_at),
                count: number_or_null(pr_raw.count) ?? 0,
                error: string_or_null(pr_raw.error),
                stale: pr_raw.stale === true,
                refreshing: pr_raw.refreshing === true,
                truncated: pr_raw.truncated === true,
            },
            brief: {
                status: brief_raw.status === 'running' || brief_raw.status === 'error' || brief_raw.status === 'done'
                    ? brief_raw.status : 'idle',
                step: string_or_null(brief_raw.step),
                done: number_or_null(brief_raw.done) ?? 0,
                total: number_or_null(brief_raw.total) ?? 0,
                cost_usd: number_or_null(brief_raw.cost_usd) ?? 0,
                note: string_or_null(brief_raw.note),
                error: string_or_null(brief_raw.error),
                at: number_or_null(brief_raw.at),
                count: number_or_null(brief_raw.count) ?? 0,
                model: typeof brief_raw.model === 'string' ? brief_raw.model : 'haiku',
            },
            branches: raw.branches.filter(is_branch_row)
                .map(normalize_brief).map(normalize_adopted).map(normalize_triage),
            // Additive: a bridge running the older plugin sends neither key,
            // and an absent remote tier grows no UI (the `fetch_worktree_state`
            // rule this whole parser is written to).
            remote_branches: Array.isArray(raw.remote_branches)
                ? raw.remote_branches.filter(is_remote_branch_row).map(with_scuttle_verdict) : [],
            remote_error: string_or_null(raw.remote_error),
            worktrees: Array.isArray(raw.worktrees) ? raw.worktrees.filter(is_worktree_info) : [],
            archived: Array.isArray(raw.archived)
                ? raw.archived.map(archived_tip_of).filter((tip): tip is ArchivedTip => tip !== null) : [],
        };
    } catch {
        return null;
    }
}

const FULL_OID = /^[0-9a-f]{40}([0-9a-f]{24})?$/;

function archived_tip_of(value: unknown): ArchivedTip | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.branch !== 'string' || raw.branch === '') return null;
    if (typeof raw.oid !== 'string' || !FULL_OID.test(raw.oid)) return null;
    return {
        branch: raw.branch,
        oid: raw.oid,
        sha: typeof raw.sha === 'string' ? raw.sha : raw.oid.slice(0, 9),
        committed_at: number_or_null(raw.committed_at),
        subject: typeof raw.subject === 'string' ? raw.subject : '',
    };
}

/** The row's `absorption`, shape-checked; anything malformed is null (no evidence). */
function absorption_of(value: unknown): BranchAbsorption | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    if (raw.status === 'pending' || raw.status === 'too_big') return { status: raw.status };
    if (raw.status !== 'measured') return null;
    const lines = count_or_null(raw.lines);
    const present = count_or_null(raw.present);
    const missing_file_count = count_or_null(raw.missing_file_count);
    if (lines === null || present === null || missing_file_count === null || present > lines) return null;
    return {
        status: 'measured',
        lines,
        present,
        missing_files: (Array.isArray(raw.missing_files) ? raw.missing_files : [])
            .filter((path): path is string => typeof path === 'string').slice(0, ABSORPTION_MISSING_CAP),
        missing_file_count,
        // The verdict is the plugin's; a contradiction with its own counts refuses.
        absorbed: raw.absorbed === true && missing_file_count === 0 && lines > 0,
    };
}

/**
 * The row's `brief`, as a `BranchBrief` or null. Rows pass through the
 * parser verbatim (`is_branch_row` checks three keys), so this is the one
 * field that gets its own shape check — it is TEXT a model wrote, rendered
 * into the panel, and `next` must be strings.
 */
function normalize_brief(row: BranchRow): BranchRow {
    const raw = (row as { brief?: unknown }).brief;
    if (!raw || typeof raw !== 'object') return { ...row, brief: null };
    const b = raw as Record<string, unknown>;
    const what = typeof b.what === 'string' ? b.what.trim() : '';
    if (!what) return { ...row, brief: null };
    return {
        ...row,
        brief: {
            what,
            next: Array.isArray(b.next) ? b.next.filter((s): s is string => typeof s === 'string' && s.trim() !== '') : [],
            model: string_or_null(b.model),
            at: number_or_null(b.at),
            stale: b.stale === true,
        },
    };
}

/**
 * The row's `adopted` anchor, or null. Additive, so an older bridge simply
 * has none and every surface reads "never adopted" — which is the truth for a
 * plugin that cannot adopt.
 *
 * `files` is normalised to strings for the same reason `brief.next` is: it is
 * rendered into the panel, and one non-string would break the row it lands in.
 */
function normalize_adopted(row: BranchRow): BranchRow {
    const raw = (row as { adopted?: unknown }).adopted;
    if (!raw || typeof raw !== 'object') return { ...row, adopted: null };
    const entry = raw as Record<string, unknown>;
    return {
        ...row,
        adopted: {
            at: number_or_null(entry.at),
            files: Array.isArray(entry.files)
                ? entry.files.filter((name): name is string => typeof name === 'string')
                : [],
            ref: string_or_null(entry.ref),
        },
    };
}

const LANDING_HOWS: ReadonlySet<string> = new Set(['ancestor', 'pr', 'squash', 'commits']);
const CONTENT_TIERS: ReadonlySet<string> = new Set(['measured', 'pending', 'skipped']);
/** The plugin's own caps, re-applied: the ledger and `occupied_by` are rendered into the panel. */
const LEDGER_COMMIT_CAP = 6;
const OCCUPANT_CAP = 3;
const ABSORPTION_MISSING_CAP = 6;

function count_or_null(value: unknown): number | null {
    return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

function pr_number_or_null(value: unknown): number | null {
    return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

function ledger_commit(value: unknown): BranchLedgerCommit | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.sha !== 'string' || raw.sha === '') return null;
    const hint_raw = (raw.hint && typeof raw.hint === 'object' ? raw.hint : {}) as Record<string, unknown>;
    const hint_pr = pr_number_or_null(hint_raw.pr);
    const why = hint_raw.why === 'title' || hint_raw.why === 'pr_patch' ? hint_raw.why : null;
    return {
        sha: raw.sha,
        subject: typeof raw.subject === 'string' ? raw.subject : '',
        found: raw.found === true,
        hint: hint_pr !== null && why !== null ? { pr: hint_pr, why } : null,
    };
}

function ledger_of(value: unknown): BranchLedger | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    const found = count_or_null(raw.found);
    const missing = count_or_null(raw.missing);
    if (found === null || missing === null) return null;
    return {
        found,
        missing,
        commits: (Array.isArray(raw.commits) ? raw.commits : [])
            .map(ledger_commit)
            .filter((commit): commit is BranchLedgerCommit => commit !== null)
            .slice(0, LEDGER_COMMIT_CAP),
    };
}

/**
 * The triage fields, each defaulted to the reading that REFUSES — the
 * `with_scuttle_verdict` posture. An older plugin sends none of them, and the
 * defaults are what make it draw today's harbor: no landing detail, nothing
 * contained, no ledger, nothing removable outside the managed root, nobody
 * known to be in a tree. `content` absent reads `measured` — "nothing is
 * pending" — because a plugin with no patch-id tier has nothing to wait for.
 *
 * The contract's own exclusions are re-applied rather than trusted: `landing`
 * only on a landed row, `contained_in` only on one that is not, and a ledger
 * only on a row that is neither — a contradictory row reads as the stronger
 * proof alone, never as both.
 */
function normalize_triage(row: BranchRow): BranchRow {
    const raw = row as unknown as Record<string, unknown>;
    const landed = row.landed === true || row.merged === true;
    const landing_raw = (raw.landing && typeof raw.landing === 'object'
        ? raw.landing : null) as Record<string, unknown> | null;
    const landing: BranchLanding | null = landed && landing_raw
        && typeof landing_raw.how === 'string' && LANDING_HOWS.has(landing_raw.how)
        ? { how: landing_raw.how as BranchLandingHow, pr: pr_number_or_null(landing_raw.pr) }
        : null;
    const contained_in = !landed && typeof raw.contained_in === 'string' && raw.contained_in !== ''
        ? raw.contained_in : null;
    const { worktree_removable_outside: removable_raw, ...rest } = row;
    const out: BranchRow = {
        ...rest,
        landing,
        contained_in,
        content: typeof raw.content === 'string' && CONTENT_TIERS.has(raw.content)
            ? raw.content as BranchContent : 'measured',
        ledger: landed || contained_in !== null ? null : ledger_of(raw.ledger),
        absorption: landed || contained_in !== null ? null : absorption_of(raw.absorption),
        worktree: row.worktree ? { ...row.worktree, occupied_by: occupants_of(row.worktree.occupied_by) } : null,
    };
    if (removable_raw === true) out.worktree_removable_outside = true;
    return out;
}

function occupants_of(value: unknown): string[] | null {
    if (!Array.isArray(value)) return null;
    return value
        .filter((name): name is string => typeof name === 'string' && name.trim() !== '')
        .slice(0, OCCUPANT_CAP);
}

/** `POST /brief {branch?}` — 202 is the success; the worker narrates through `/branches.brief`. */
export async function start_brief(
    base_url: BaseUrl,
    branch: string | null = null,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/brief', branch ? { branch } : {},
        ACTION_TIMEOUT_MS, 'could not start the brief'));
}

/** `POST /fetch` — 202 is the success; the worker narrates through `/branches.fetch`. */
export async function start_fetch(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/fetch', {},
        ACTION_TIMEOUT_MS, 'could not start the fetch'));
}

export interface DeleteBranchResult extends ActionResult {
    /** A 409 saying `-d` would refuse: the branch has commits main lacks.
     *  Re-arm with `force` — and print `ahead` on the arm, never a guess. */
    needs_force?: boolean;
    ahead?: number;
    /** A 409 saying the checkout holding it is a MANAGED worktree, so REMOVE
     *  WORKTREE is the way through. */
    worktree_removable?: boolean;
}

/**
 * `POST /delete-branch` — synchronous on the plugin (local git only).
 *
 * A refusal is an ANSWER, not a transport failure: the 409 body says WHY and,
 * for an unmerged branch, HOW MANY commits `-D` would drop. Those ride on the
 * result so the panel can re-arm red with the real number.
 */
export async function delete_branch(
    base_url: BaseUrl,
    branch: string,
    force: boolean,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<DeleteBranchResult> {
    const outcome = await post(base_url, fetch_impl, '/delete-branch', { branch, force },
        ACTION_TIMEOUT_MS, 'could not delete the branch');
    const result: DeleteBranchResult = action_result(outcome);
    if (outcome.body.needs_force === true) result.needs_force = true;
    const ahead = number_or_null(outcome.body.ahead);
    if (ahead !== null) result.ahead = ahead;
    if (outcome.body.worktree_removable === true) result.worktree_removable = true;
    return result;
}

/** `POST /restore-branch` — the archived tip back as a branch of the same name. */
export async function restore_branch(
    base_url: BaseUrl,
    branch: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/restore-branch', { branch },
        ACTION_TIMEOUT_MS, 'could not restore the branch'));
}

/** `POST /forget-archived` — drop one pin. `oid` is the tip the row showed;
 *  the plugin refuses if the pin moved since. */
export async function forget_archived(
    base_url: BaseUrl,
    branch: string,
    oid: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/forget-archived', { branch, sha: oid },
        ACTION_TIMEOUT_MS, 'could not forget the archived tip'));
}

export interface ScuttleResult extends ActionResult {
    /** False when the remote had already lost the ref — asked-for, not failed. */
    deleted?: boolean;
    /** `origin/feat-nav`. */
    ref?: string;
}

/**
 * `POST /scuttle-remote` — `git push <remote> --delete <branch>`.
 *
 * No `force` parameter, and there is not one to add: the plugin gates this on
 * a PREDICATE ("already landed"), so there is nothing a flag could unlock.
 * A 409 here is a refusal to be shown, never a prompt to re-arm — which is
 * what makes this different from `delete_branch`'s `needs_force`.
 */
export async function scuttle_remote(
    base_url: BaseUrl,
    remote: string,
    branch: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ScuttleResult> {
    const outcome = await post(base_url, fetch_impl, '/scuttle-remote', { remote, branch },
        SCUTTLE_TIMEOUT_MS, 'could not delete the remote branch');
    const result: ScuttleResult = action_result(outcome);
    if (typeof outcome.body.deleted === 'boolean') result.deleted = outcome.body.deleted;
    if (typeof outcome.body.ref === 'string') result.ref = outcome.body.ref;
    return result;
}

/**
 * `POST /remove-landed-worktree` — `git worktree remove` on a tree OUTSIDE the
 * managed root, for CLEANUP's outside step. The branch is kept; the walk's
 * delete step takes it next.
 *
 * No `force`, and none to add: the plugin re-checks its refusal table on a
 * fresh sweep (main checkout · not landed · uncommitted files · locked · a
 * process inside · a create mid-ladder), so a 409 is a refusal to show, never
 * a prompt to re-arm — `scuttle_remote`'s posture, for the same reason.
 */
export async function remove_landed_worktree(
    base_url: BaseUrl,
    branch: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<ActionResult> {
    return action_result(await post(base_url, fetch_impl, '/remove-landed-worktree', { branch },
        ACTION_TIMEOUT_MS, 'could not remove the landed worktree'));
}

export interface PruneResult extends ActionResult {
    /** The worktree admin names `git worktree prune -v` removed. */
    pruned?: string[];
}

/** `POST /prune-worktrees` — drops stale worktree bookkeeping; deletes no directory. */
export async function prune_worktrees(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<PruneResult> {
    const outcome = await post(base_url, fetch_impl, '/prune-worktrees', {},
        ACTION_TIMEOUT_MS, 'could not prune worktrees');
    const result: PruneResult = action_result(outcome);
    if (Array.isArray(outcome.body.pruned)) {
        result.pruned = outcome.body.pruned.filter(
            (name): name is string => typeof name === 'string',
        );
    }
    return result;
}

// ── changes: what a forced remove would destroy ─────────────────────────────

/**
 * One uncommitted file, exactly as `GET /changes` ships it.
 *
 * `kind` is the plugin's collapse of git's two status columns into ONE word
 * (`changes.py::kind_for` owns the table) — the client never reads `index` /
 * `worktree` to decide what a row IS, only to show which side of the index it
 * sits on. `added` / `removed` are null for a binary and for an untracked file
 * too big to count; `binary` is the flag that says which, because "0 lines"
 * and "not lines" must never render the same.
 */
export interface WorktreeChangeRow {
    /** Repo-relative, as git printed it. */
    path: string;
    /** A rename's source; null otherwise. */
    orig_path: string | null;
    /** git's X column (the index). */
    index: string;
    /** git's Y column (the working tree). */
    worktree: string;
    kind: 'modified' | 'added' | 'deleted' | 'renamed' | 'copied' | 'typechange'
        | 'untracked' | 'ignored' | 'conflicted';
    staged: boolean;
    added: number | null;
    removed: number | null;
    binary: boolean;
    /** Untracked only — the size on disk, so a row is never blank. */
    bytes: number | null;
}

export interface WorktreeChangeSummary {
    /** Every row, INCLUDING the ones past the list's cap. */
    files: number;
    added: number;
    removed: number;
    binary: number;
    /** `{modified: 2, untracked: 2}` — counted by the collapsed name. */
    kinds: Record<string, number>;
}

/** One file's unified diff, when the caller asked for one. */
export interface WorktreePatch {
    path: string;
    text: string;
    /** Hit the plugin's line or byte ceiling — the view must say so. */
    truncated: boolean;
    error: string | null;
}

export interface WorktreeChanges {
    branch: string;
    /** `~`-collapsed path of the worktree the rows came from. */
    path_label: string | null;
    /** Whether `POST /remove` will even look at this tree — reading is not gated. */
    managed: boolean;
    files: WorktreeChangeRow[];
    summary: WorktreeChangeSummary;
    /** `files` is a prefix of `summary.files` rows. */
    truncated: boolean;
    patch: WorktreePatch | null;
    /** git could not answer; `files` is empty and means nothing. */
    error: string | null;
    /**
     * The adopt anchor for this branch, WITH `unpreserved` computed — the one
     * place that number exists, because answering it costs a whole-tree
     * snapshot and this route is pulled rather than polled.
     *
     * `unpreserved: null` is UNKNOWN (the probe failed) and must render as
     * unsafe. `[]` is the safe-delete signal: a forced remove destroys nothing
     * that is not already anchored.
     */
    adopted: ChangesAdopted | null;
}

export interface ChangesAdopted {
    at: number | null;
    files: string[];
    ref: string | null;
    unpreserved: string[] | null;
    /** Why `unpreserved` is null, when it is. */
    error: string | null;
}

const CHANGE_KINDS: ReadonlySet<string> = new Set([
    'modified', 'added', 'deleted', 'renamed', 'copied', 'typechange',
    'untracked', 'ignored', 'conflicted',
]);

function change_row(value: unknown): WorktreeChangeRow | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.path !== 'string' || raw.path === '') return null;
    return {
        path: raw.path,
        orig_path: string_or_null(raw.orig_path),
        index: typeof raw.index === 'string' ? raw.index : ' ',
        worktree: typeof raw.worktree === 'string' ? raw.worktree : ' ',
        // An unrecognised kind degrades to `modified` rather than dropping the
        // row: a file the view cannot name is still a file the remove destroys.
        kind: (typeof raw.kind === 'string' && CHANGE_KINDS.has(raw.kind)
            ? raw.kind : 'modified') as WorktreeChangeRow['kind'],
        staged: raw.staged === true,
        added: number_or_null(raw.added),
        removed: number_or_null(raw.removed),
        binary: raw.binary === true,
        bytes: number_or_null(raw.bytes),
    };
}

/**
 * The `WorktreeChanges` a reader draws when there is nothing to report.
 *
 * The one owner of the shape's zero value: a caller that never reaches the
 * bridge (a stale branch key) hands it the `error` and returns it whole, and
 * the fetcher spreads it under every early return.
 */
export function empty_worktree_changes(branch: string, error: string | null = null): WorktreeChanges {
    return {
        branch,
        path_label: null,
        managed: false,
        files: [],
        summary: { files: 0, added: 0, removed: 0, binary: 0, kinds: {} },
        truncated: false,
        patch: null,
        error,
        adopted: null,
    };
}

/**
 * One `GET /plugin/worktree/<route>` read, parsed leniently.
 *
 * `error` is non-null when the bridge refused; `raw` is the decoded body
 * either way (a refusal still carries the plugin's own wording, which beats
 * the status-code fallback). Throwing is left to the caller's `catch` — the
 * two readers word "the bridge did not answer" differently.
 */
async function get_plugin_json(
    base_url: BaseUrl,
    fetch_impl: FetchImpl,
    route: string,
    query: URLSearchParams,
    failure_label: string,
): Promise<{ raw: Record<string, unknown>; error: string | null }> {
    const response = await fetch_impl(
        base_url(`/plugin/worktree/${route}?${query.toString()}`),
        { signal: AbortSignal.timeout(CHANGES_TIMEOUT_MS) },
    );
    const body: unknown = await response.json().catch(() => null);
    const raw = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    if (!response.ok) {
        return { raw, error: string_or_null(raw.error) ?? `${failure_label} (${response.status})` };
    }
    return { raw, error: null };
}

/**
 * `GET /changes` — the uncommitted work in a branch's worktree, and (with
 * `path`) that one file's patch.
 *
 * Read-only, and the ONE route here that answers about a worktree this plugin
 * did not create: looking is what makes `git worktree remove`'s refusal
 * legible, while the write gate stays exactly where it was.
 *
 * A failure is a RESULT, never null: the caller is a panel block the operator
 * opened on purpose, and "the bridge said no" has to be drawable. That is the
 * opposite of `fetch_worktree_state`, whose null means "grow no UI at all".
 */
export async function fetch_worktree_changes(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
    branch: string,
    path?: string,
): Promise<WorktreeChanges> {
    const empty = empty_worktree_changes(branch);
    const query = new URLSearchParams({ branch });
    if (path !== undefined && path !== '') query.set('path', path);
    try {
        const { raw, error } = await get_plugin_json(
            base_url, fetch_impl, 'changes', query, 'changes failed');
        if (error !== null) return { ...empty, error };
        const summary_raw = (raw.summary && typeof raw.summary === 'object'
            ? raw.summary : {}) as Record<string, unknown>;
        const kinds_raw = (summary_raw.kinds && typeof summary_raw.kinds === 'object'
            ? summary_raw.kinds : {}) as Record<string, unknown>;
        const kinds: Record<string, number> = {};
        for (const [name, count] of Object.entries(kinds_raw)) {
            if (typeof count === 'number' && Number.isFinite(count)) kinds[name] = count;
        }
        const files = (Array.isArray(raw.files) ? raw.files : [])
            .map(change_row)
            .filter((row): row is WorktreeChangeRow => row !== null);
        const patch_raw = (raw.patch && typeof raw.patch === 'object'
            ? raw.patch : null) as Record<string, unknown> | null;
        return {
            branch: typeof raw.branch === 'string' ? raw.branch : branch,
            path_label: string_or_null(raw.path_label),
            managed: raw.managed === true,
            files,
            summary: {
                files: number_or_null(summary_raw.files) ?? files.length,
                added: number_or_null(summary_raw.added) ?? 0,
                removed: number_or_null(summary_raw.removed) ?? 0,
                binary: number_or_null(summary_raw.binary) ?? 0,
                kinds,
            },
            truncated: raw.truncated === true,
            patch: patch_raw === null ? null : {
                path: typeof patch_raw.path === 'string' ? patch_raw.path : (path ?? ''),
                text: typeof patch_raw.text === 'string' ? patch_raw.text : '',
                truncated: patch_raw.truncated === true,
                error: string_or_null(patch_raw.error),
            },
            error: null,
            adopted: changes_adopted(raw.adopted),
        };
    } catch {
        return { ...empty, error: 'the bridge did not answer' };
    }
}

/**
 * Is the harbor mid-anything? Decides the poll cadence, and nothing else —
 * the `has_active_create` rule, widened to the document.
 *
 * `warming` counts only while the sweep has NOT failed: a warming document
 * that carries an error is a runner that backed off (a bridge whose start
 * dir is not a repo, say), and fast-polling it forever would be the wedge
 * the idle cadence exists to avoid.
 */
export function has_active_harbor_job(state: HarborState | null): boolean {
    if (!state) return false;
    if (state.status === 'warming' && state.error === null) return true;
    if (state.fetch.status === 'running') return true;
    if (state.brief.status === 'running') return true;
    return state.branches.some((row) => row.create_status === 'creating');
}

/**
 * Parse a `worktree` action's `value`.
 *
 * `RackAction`'s argument is one optional string by design — it crosses a
 * postMessage boundary AND a BroadcastChannel, and every hop re-validates
 * `typeof === 'string'`. So a two-field form rides as JSON and is re-validated
 * HERE, at the receiving end, exactly like every other hop.
 */
export function parse_create_request(value: unknown): WorktreeCreateRequest | null {
    if (typeof value !== 'string' || value === '') return null;
    let parsed: unknown;
    try {
        parsed = JSON.parse(value);
    } catch {
        return null;
    }
    if (!parsed || typeof parsed !== 'object') return null;
    const candidate = parsed as Record<string, unknown>;
    const branch = typeof candidate.branch === 'string' ? candidate.branch.trim() : '';
    if (branch === '') return null;
    const prompt = typeof candidate.prompt === 'string' ? candidate.prompt.trim() : '';
    const request: WorktreeCreateRequest = prompt ? { branch, prompt } : { branch };
    // Only a literal `true` rides through: the plugin treats absent as "create
    // a new branch", and a truthy string here would turn a typo into a
    // checkout of some existing branch.
    if (candidate.existing === true) request.existing = true;
    // Validated, not passed through: a malformed id is dropped (→ the bridge
    // default) rather than forwarded for the plugin to refuse the whole create.
    const profile = parse_profile_id(candidate.profile);
    if (profile !== null) request.profile = profile;
    return request;
}

export interface WorktreePoller {
    /** Last known state — `null` until the first successful fetch, and after a
     *  fetch that found no plugin. */
    state: () => WorktreeState | null;
    /** Session names backed by a worktree. THE reason this is a Set: it decides
     *  the `attach:` invariant on every focus change. */
    sessions: () => Set<string>;
    tick: () => Promise<void>;
    start: () => void;
    stop: () => void;
}

/** A rack poller's injectable I/O — the fakes a test hands in. */
export interface PollerIoDeps {
    fetch_impl?: FetchImpl;
    set_interval?: (fn: () => void, ms: number) => ReturnType<typeof setInterval>;
    clear_interval?: (handle: ReturnType<typeof setInterval>) => void;
}

/** That I/O with the real defaults filled in; the fetch default carries the local-network gate. */
export function poller_io(deps: PollerIoDeps): {
    do_fetch: FetchImpl;
    start_timer: (fn: () => void, ms: number) => ReturnType<typeof setInterval>;
    stop_timer: (handle: ReturnType<typeof setInterval>) => void;
} {
    return {
        do_fetch: deps.fetch_impl ?? ((...args) => local_fetch(...args)),
        start_timer: deps.set_interval ?? ((fn, ms) => setInterval(fn, ms)),
        stop_timer: deps.clear_interval ?? ((handle) => clearInterval(handle)),
    };
}

export interface WorktreePollerDeps extends PollerIoDeps {
    base_url: BaseUrl;
    poll_interval_ms: number;
    on_change: () => void;
    /** May the TIMER poll right now? Read on each timed tick; absent is always. Why, and what an
     *  owner that answers false still owes: `create_worktree_poller`. */
    wanted?: () => boolean;
}

/** Is any row mid-create? Decides the poll cadence, and nothing else. */
export function has_active_create(state: WorktreeState | null): boolean {
    return (state?.worktrees ?? []).some((row) => row.create_status === 'creating');
}

/** Is a CLEAN running? Also cadence only: its worker is seconds of git, and at
 *  the idle 5 s a finished clean-up would sit on `CLEAN…` most of a poll. */
export function has_active_cleanup(state: WorktreeState | null): boolean {
    return (state?.worktrees ?? []).some((row) => row.cleanup_status === 'running');
}

/**
 * Session names that a create is currently building — the rows to show in the
 * agent list before tmux has anything to report.
 *
 * ── Why this is needed at all ──────────────────────────────────────────────
 * `tmux new-session` is the THIRD of four steps. Until it runs there is no
 * session, so `/snapshot` knows nothing and the rack list stays exactly as it
 * was — which is the whole complaint: you press `+ WORKTREE` and the rack does
 * not visibly react. The worktree row carries the progress, but a row in the
 * WORKTREE list is not where anyone is looking; the agent list is.
 *
 * So every payload owner injects an optimistic session row for these, through
 * the same `augment_sessions` seam `+ NEW` already uses. The row is decorated as
 * "creating" by joining it back to the worktree row, exactly the way the PR
 * button decorates a row from `pr_status` — which is what keeps `RackState` out
 * of this. A `creating` member there would have to be ranked against
 * `attention` / `done` / `free` / `busy` in `session_rank`, and that ladder
 * answers one question ("how much does this row want you") that a session which
 * does not exist yet cannot answer.
 *
 * Deliberately NOT filtered against the snapshot here: the caller has the
 * session list in hand and the "already present?" test belongs beside it.
 */
export function creating_sessions(state: WorktreeState | null): string[] {
    return (state?.worktrees ?? [])
        .filter((row) => row.create_status === 'creating')
        .map((row) => row.session);
}

/** A 🏝 launch the session never reached by then is given up on, focus-wise:
 *  a `git worktree add` + bootstrap is seconds to a minute, never this. */
export const ISLAND_FOCUS_TIMEOUT_MS = 180_000;

/**
 * Focus a 🏝 launch's session the moment it exists: the hand-off every payload owner shares.
 *
 * MAIN focuses at once: `/spawn` makes the session before it answers. A create
 * answers 202 with the session still two steps away, and pointing the pane at
 * it early is `tmux new -A` racing the plugin's own `new-session` for the name.
 * So the launch ARMS this, and each poll asks
 * `take`: the session is there once the create has passed its `session` step
 * (`prompt`, or finished) AND the snapshot lists it live — not before, because
 * the optimistic creating row is live too (`rack-ingest.ts`).
 *
 * A failed create, the timeout, or the operator picking any other row (`cancel`)
 * drops it: a late focus must never yank the pane off what they chose.
 */
export interface IslandFocus {
    arm(session: string, now: number): void;
    cancel(): void;
    /** Is a launch still waiting to land? Its owner keeps the worktree poll running while so. */
    armed(): boolean;
    /** The session to focus now, once; else null. */
    take(state: WorktreeState | null, live: readonly string[], now: number): string | null;
}

export function create_island_focus(): IslandFocus {
    let pending: { session: string; until: number } | null = null;
    return {
        arm(session, now) {
            pending = { session, until: now + ISLAND_FOCUS_TIMEOUT_MS };
        },
        cancel() {
            pending = null;
        },
        armed() {
            return pending !== null;
        },
        take(state, live, now) {
            if (!pending) return null;
            const { session } = pending;
            const row = (state?.worktrees ?? []).find((r) => r.session === session);
            if (row?.create_status === 'error' || now > pending.until) {
                pending = null;
                return null;
            }
            if (row?.create_status === 'creating' && row.create_step !== 'prompt') return null;
            if (!live.includes(session)) return null;
            pending = null;
            return session;
        },
    };
}

/**
 * Poll `/state` and notify on change.
 *
 * Change is compared on the SERIALIZED state, not on identity: this drives a
 * payload emit, and re-rendering the whole rack every 5 s because a fresh object
 * arrived is cost for nothing. The WHOLE parsed document is what gets
 * serialized. Comparing only `worktrees` would keep `worktree_paths` (which
 * the managed rows say nothing about) from reaching `state()` once the first
 * document had landed: a bridge restart onto the plugin that ships the key, or
 * a new unmanaged tree appearing mid-session, would leave `attach_only` off the
 * payload and the rack back on the bare `?arg=` form. `fetch_worktree_state` is
 * the one owner of which keys exist, and nothing it copies ticks, so following
 * it wholesale also guards the next additive key.
 *
 * The cadence is adaptive. Idle, it runs at the caller's interval (the rack's
 * 5 s `worktree_poll_ms`); while any row is `creating` or cleaning up it drops
 * to `CREATE_POLL_INTERVAL_MS` so the job's readout actually reads as motion. The
 * timer is only re-armed when the cadence CHANGES, so a create that runs for
 * five minutes costs one `clearInterval`/`setInterval` pair, not one per tick.
 *
 * `wanted` rests the TIMER, the way `plugin-poller.ts` rests on its own `wanted`: a page nobody
 * is looking at was asking the plugin for a `git` readout of every tree each 5 s (each second
 * through a create) to redraw rows nobody could see. Only the timed tick asks it. `tick()` called
 * by name always fetches: it is how an owner reads after an action, and how it catches up the
 * moment the page is looked at again. An owner that rests the timer owes those calls for
 * whatever it reads from `state()` without drawing it; `rack-http-transport.ts` owns that list.
 */
export function create_worktree_poller(deps: WorktreePollerDeps): WorktreePoller {
    const { do_fetch, start_timer, stop_timer } = poller_io(deps);

    let state: WorktreeState | null = null;
    let signature = '';
    let timer: ReturnType<typeof setInterval> | null = null;
    let timer_interval_ms = 0;
    let stopped = false;

    function desired_interval_ms(): number {
        return has_active_create(state) || has_active_cleanup(state) ? CREATE_POLL_INTERVAL_MS : deps.poll_interval_ms;
    }

    /** Re-arm only on a real cadence change — see the note above. */
    function sync_timer(): void {
        if (stopped) return;
        const next_ms = desired_interval_ms();
        if (timer !== null && next_ms === timer_interval_ms) return;
        if (timer !== null) stop_timer(timer);
        timer_interval_ms = next_ms;
        timer = start_timer(() => {
            if (deps.wanted?.() ?? true) void tick();
        }, next_ms);
    }

    async function tick(): Promise<void> {
        if (stopped) return;
        const next = await fetch_worktree_state(deps.base_url, do_fetch);
        const next_signature = next ? JSON.stringify(next) : '';
        if (next_signature === signature) return;
        signature = next_signature;
        state = next;
        // Before `on_change`, so a listener that reads the poller sees the
        // cadence it will actually run at.
        sync_timer();
        deps.on_change();
    }

    return {
        state: () => state,
        sessions: () => new Set((state?.worktrees ?? []).map((row) => row.session)),
        tick,
        start: () => {
            if (timer !== null) return;
            void tick();
            sync_timer();
        },
        stop: () => {
            stopped = true;
            if (timer !== null) stop_timer(timer);
            timer = null;
            timer_interval_ms = 0;
        },
    };
}

/**
 * `/changes.adopted` → `ChangesAdopted | null`. Additive: an older bridge
 * sends nothing and every surface reads "never adopted".
 *
 * `unpreserved` stays null unless the plugin sent a real array. An absent or
 * malformed value is UNKNOWN, and unknown renders as unsafe — the same rule
 * `BranchWorktree.dirty` keeps, and for the same reason: a probe that failed
 * must never be mistaken for a clean bill of health on a destroy button.
 */
function changes_adopted(value: unknown): ChangesAdopted | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    return {
        at: number_or_null(raw.at),
        files: Array.isArray(raw.files)
            ? raw.files.filter((name): name is string => typeof name === 'string')
            : [],
        ref: string_or_null(raw.ref),
        unpreserved: Array.isArray(raw.unpreserved)
            ? raw.unpreserved.filter((name): name is string => typeof name === 'string')
            : null,
        error: string_or_null(raw.error),
    };
}

// ── ADOPT and CATCH UP ─────────────────────────────────────────────────────

/** One row of the adopt plan. `verdict` is what adopting DOES to this path. */
export interface AdoptPlanRow {
    path: string;
    /**
     * `new` absent on the base · `merge` a clean three-way · `readd` the base
     * DELETED it and your edit is kept (no conflict markers — git's own
     * resolution) · `conflict` would land with markers, needs force ·
     * `blocked` the base has uncommitted edits here too, and is never forced ·
     * `delete` the worktree removed a file the base has.
     */
    verdict: 'new' | 'merge' | 'readd' | 'conflict' | 'blocked' | 'delete';
    /** git's `diff --raw` status letter, for the row's mark. */
    status: string;
}

export interface AdoptPlan {
    branch: string;
    path_label: string | null;
    managed: boolean;
    /** The branch the changes would land on — the base. */
    target: string;
    files: AdoptPlanRow[];
    total: number;
    truncated: boolean;
    /** `4 FILES · 2 NEW · 2 RE-ADD` — zero terms omitted. */
    summary: string;
    blocked: string[];
    conflicts: string[];
    /** Nothing blocked and nothing conflicting: one press, no force. */
    clean: boolean;
    /** Something to adopt, and nothing blocked. A conflict is still adoptable
     *  — with `force` — so this is NOT the same question as `clean`. */
    adoptable: boolean;
    error: string | null;
}

/**
 * The `AdoptPlan` a reader draws when there is nothing to plan —
 * `empty_worktree_changes`'s twin, for the same reason.
 */
export function empty_adopt_plan(branch: string, error: string | null = null): AdoptPlan {
    return {
        branch, path_label: null, managed: false, target: '', files: [], total: 0,
        truncated: false, summary: '', blocked: [], conflicts: [], clean: false,
        adoptable: false, error,
    };
}

/**
 * `GET /adopt-plan` — what ADOPT would do, per file, before it does anything.
 *
 * A failure is a RESULT, never null: like `fetch_worktree_changes`, the caller
 * is a block the operator opened on purpose and "the bridge said no" has to be
 * drawable.
 */
export async function fetch_adopt_plan(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
    branch: string,
): Promise<AdoptPlan> {
    const empty = empty_adopt_plan(branch);
    const query = new URLSearchParams({ branch });
    try {
        const { raw, error } = await get_plugin_json(
            base_url, fetch_impl, 'adopt-plan', query, 'adopt plan failed');
        if (error !== null) return { ...empty, error };
        const files = (Array.isArray(raw.files) ? raw.files : [])
            .map(adopt_plan_row)
            .filter((row): row is AdoptPlanRow => row !== null);
        return {
            branch: typeof raw.branch === 'string' ? raw.branch : branch,
            path_label: string_or_null(raw.path_label),
            managed: raw.managed === true,
            target: typeof raw.target === 'string' ? raw.target : '',
            files,
            total: number_or_null(raw.total) ?? files.length,
            truncated: raw.truncated === true,
            summary: typeof raw.summary === 'string' ? raw.summary : '',
            blocked: string_list(raw.blocked),
            conflicts: string_list(raw.conflicts),
            clean: raw.clean === true,
            adoptable: raw.adoptable === true,
            error: null,
        };
    } catch {
        return { ...empty, error: 'could not read the adopt plan' };
    }
}

const ADOPT_VERDICTS: ReadonlySet<string> = new Set([
    'new', 'merge', 'readd', 'conflict', 'blocked', 'delete',
]);

function adopt_plan_row(value: unknown): AdoptPlanRow | null {
    if (!value || typeof value !== 'object') return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.path !== 'string' || raw.path === '') return null;
    // An unknown verdict is not guessed at. Dropping the row keeps the count
    // honest (`total` is the plugin's) rather than drawing a mark we invented.
    if (typeof raw.verdict !== 'string' || !ADOPT_VERDICTS.has(raw.verdict)) return null;
    return {
        path: raw.path,
        verdict: raw.verdict as AdoptPlanRow['verdict'],
        status: typeof raw.status === 'string' ? raw.status : '',
    };
}

function string_list(value: unknown): string[] {
    return Array.isArray(value)
        ? value.filter((name): name is string => typeof name === 'string')
        : [];
}

export interface AdoptResult extends ActionResult {
    /** The plugin refused a plan that would write conflict markers (409).
     *  Re-arm with `force` — the same handshake `remove_worktree` speaks. */
    needs_force?: true;
    /** Paths the BASE has uncommitted edits on. Never forceable: the base's
     *  copy has nowhere else to live, while the source's is still in its
     *  worktree, so refusing is the cheap recovery. */
    blocked?: string[];
    conflicts?: string[];
    /** What actually landed. */
    adopted?: string[];
    count?: number;
    ref?: string | null;
}

/** `POST /adopt {branch, force?}` — land a worktree's uncommitted work on the
 *  base. The worktree is left exactly as it is; this is a copy, not a move. */
export async function adopt_worktree(
    base_url: BaseUrl,
    branch: string,
    force: boolean,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<AdoptResult> {
    const outcome = await post(base_url, fetch_impl, '/adopt', { branch, force },
        CHANGES_TIMEOUT_MS, 'could not adopt those changes');
    const result: AdoptResult = action_result(outcome);
    if (outcome.body.needs_force === true) result.needs_force = true;
    const blocked = string_list(outcome.body.blocked);
    if (blocked.length > 0) result.blocked = blocked;
    const conflicts = string_list(outcome.body.conflicts);
    if (conflicts.length > 0) result.conflicts = conflicts;
    const adopted = string_list(outcome.body.adopted);
    if (adopted.length > 0) result.adopted = adopted;
    if (typeof outcome.body.count === 'number') result.count = outcome.body.count;
    if (typeof outcome.body.ref === 'string') result.ref = outcome.body.ref;
    return result;
}

export interface CatchUpResult extends ActionResult {
    /** The branch has commits of its own — a fast-forward cannot do it, and
     *  this verb deliberately does not rebase. NOT a force flag: there is no
     *  forced form of this route. */
    needs_rebase?: true;
    ahead?: number;
    /** Its uncommitted edits collide with what is coming in, so the autostash
     *  would not pop. ADOPT them first and the collision empties. */
    needs_adopt?: true;
    conflicts?: string[];
    /** Commits gained. */
    advanced?: number;
    autostashed?: boolean;
}

/** `POST /catch-up {branch}` — fast-forward a branch onto the base. Refuses
 *  anything it cannot do safely rather than asking for a confirmation. */
export async function catch_up_branch(
    base_url: BaseUrl,
    branch: string,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<CatchUpResult> {
    const outcome = await post(base_url, fetch_impl, '/catch-up', { branch },
        CHANGES_TIMEOUT_MS, 'could not catch that branch up');
    const result: CatchUpResult = action_result(outcome);
    if (outcome.body.needs_rebase === true) result.needs_rebase = true;
    if (outcome.body.needs_adopt === true) result.needs_adopt = true;
    if (typeof outcome.body.ahead === 'number') result.ahead = outcome.body.ahead;
    const conflicts = string_list(outcome.body.conflicts);
    if (conflicts.length > 0) result.conflicts = conflicts;
    if (typeof outcome.body.advanced === 'number') result.advanced = outcome.body.advanced;
    if (typeof outcome.body.autostashed === 'boolean') result.autostashed = outcome.body.autostashed;
    return result;
}

/** Parent controls share the rack's existing local-network gate and action result. */
export async function control_task_group(base_url: BaseUrl, id: string, action: string): Promise<ActionResult> {
    if (!['pause', 'resume', 'retry'].includes(action)) return { ok: false, error: 'Unknown group action' };
    return action_result(await post(base_url, (...args) => local_fetch(...args), '/group-control', { id, action }, ACTION_TIMEOUT_MS, 'Could not update task group'));
}
