import type { Mission } from '@tldrack/schemas/missions.ts';
import type { TaskGroup } from '@tldrack/schemas/task-groups.ts';
import type { ClosedSession, RestorableSession } from '@tldrack/schemas/closed-sessions.ts';
import type { RackSearchHit } from '@tldrack/schemas/agent-history.ts';
import type { NextStepMode } from '@tldrack/schemas/next-step.ts';
import type { LeadState } from '@tldrack/schemas/lead.ts';
/**
 * The ONE place a bridge snapshot becomes something a rack surface can draw.
 *
 * One owner, because every surface that draws the rack (the rack page's transport, a host
 * application's own) would otherwise carry its own copy of the same mapping. A comment asking
 * two functions to stay identical is a duplication with a note attached, and such copies drift
 * (see *Divergences settled* below).
 *
 * Deliberately dependency-free in the same sense as `terminal-status-logic.ts`:
 * sibling imports only, no DOM, and **flags arrive as arguments, not as a
 * module import** (the convention `advance_tracker` and `derive_effective_state`
 * already set). That is what lets this run under `node --test` AND be consumed
 * by `rack-http-transport.ts` and by a host's own payload owner alike.
 *
 * ── Divergences settled ─────────────────────────────────────────────────────
 *
 * What two copies of this mapping disagree on, and what one owner settles:
 *
 * 1. **`state`.** The input is `PortSnapshotRow`, whose `display` is the
 *    tracker's committed verdict, so no caller derives a state of its own.
 *
 * 2. **`summary`.** One header form (`rack_header`), whichever transport a
 *    rack page is on.
 *
 * 3. **Which rows are counted.** `rows` is expected **already dead-filtered by
 *    whoever ingested it**, so the count and the list cannot disagree.
 */

import {
    UNKNOWN_MODEL_LABEL,
    format_model_label,
    parse_profile_id,
    type LaunchPick,
    type PortSnapshotRow,
    type ProfileInfo,
    type SessionActivity,
} from './terminal-status-logic.ts';
import type { RackSession, SessionIsland } from './multi-terminal-layout.ts';
import { attach_only_arg } from './session-name.ts';
import { longest_containing_root, path_contains } from './path-containment.ts';
import type { AccessNotice } from './local-network-gate.ts';
import type { RenameFollower } from './rename-tracker.ts';
import { exited_readout, is_service_session, service_for_session, service_readout, type DevServiceRow } from './service-sessions.ts';
import { office_resources, type OfficeResources } from './office-resources.ts';
import { device_rows, type DeviceRow, type DevicesState } from './device-readout.ts';

/**
 * A managed git worktree, as `GET /plugin/worktree/state` reports it.
 *
 * Declared HERE and not beside the fetch in `agent-rack/worktree-client.ts`
 * for one reason: this module imports nothing but pure siblings (see the
 * header), and `RackPayload` carries these. The client imports
 * the type back out — a type-only import, so nothing crosses at runtime and
 * `node --test`'s strip-only mode erases it entirely.
 */
export interface WorktreeRow {
    branch: string;
    path: string;
    /** Derived by the plugin from the branch — the join key against a snapshot
     *  row. The plugin deliberately does not ask tmux whether this is live;
     *  every caller already polls the snapshot that knows. */
    session: string;
    /** `null` until the plugin's background dirty sweep reaches this tree —
     *  unknown, never clean (`plugins/worktree/worktree/dirty.py`). */
    dirty: boolean | null;
    ahead: number;
    behind: number;
    /** `opening` while the PR job runs on the bridge; then `open` or `error`. */
    pr_status: 'opening' | 'open' | 'error' | null;
    pr_url: string | null;
    pr_error: string | null;
    /**
     * Create-job progress, while `POST /plugin/worktree/create` is still
     * building this branch. `error` is terminal and expires on the bridge.
     *
     * OPTIONAL, and that is load-bearing: a bridge running the older plugin —
     * one that answered `/create` synchronously — sends none of these three,
     * and must keep behaving exactly as it does today rather than rendering a
     * half-drawn row. Absent means "no create here", which is why the plugin
     * writes the fields onto in-flight rows only and never nulls them onto all.
     */
    create_status?: 'creating' | 'error' | null;
    create_step?: CreateStep | null;
    create_error?: string | null;
    /**
     * Is this branch's work in the base? The Branch Harbor's `landed`, from the
     * plugin's ONE landing door (`plugins/worktree/worktree/landing.py`):
     * an ancestor of the base, a merged PR whose head IS this tip, a squash, or
     * every commit. `null` is unknown (no PR index yet, `ahead` unknown).
     *
     * This and the three below are ABSENT unless the plugin declares
     * `capabilities.cleanup` — `worktree-client.ts::fetch_worktree_state` drops
     * them otherwise, so a bridge without `POST /cleanup` keeps the PR button.
     */
    landed?: boolean | null;
    /** How `landed` was proven — `/branches`' own shape; set on a landed row only. */
    landing?: BranchLanding | null;
    /** `POST /cleanup`'s job: `running` while its worker runs; then the row is
     *  gone, or `error` (which expires on the bridge, like a create's). */
    cleanup_status?: 'running' | 'error' | null;
    cleanup_error?: string | null;
}

/** Which proof put the work on the base — `plugins/worktree/worktree/landing.py` owns the order. */
export type BranchLandingHow = 'ancestor' | 'pr' | 'squash' | 'commits';

/** Declared here, not in `worktree-client.ts`, for `WorktreeRow`'s reason above; the client re-exports it. */
export interface BranchLanding {
    how: BranchLandingHow;
    /** The PR that proves it (`pr`), else the PR the patch matched; on an
     *  `ancestor`, the merged PR that shipped this very tip or whose merge carried it in (`work_landed`); else null. */
    pr: number | null;
}

/**
 * A NON-main git worktree, as `/state.worktree_trees` lists it — every one on
 * the machine, unfiltered, unlike the managed `WorktreeRow`s.
 *
 * `branch` is three-valued: a name, `null` for a detached HEAD, and ABSENT when
 * the plugin predates `worktree_trees` and only `worktree_paths` arrived
 * (`worktree-client.ts::fetch_worktree_state` builds those). Absent is "not
 * told", never "detached", so the island's title cannot claim one.
 */
export interface WorktreeIsland {
    /** `~`-collapsed, as every plugin GET path is (`path-containment.ts`). */
    path: string;
    branch?: string | null;
    /**
     * The branch's pull request, from the plugin's PR index and the PR button (`/state.worktree_trees[].pr`):
     * `none` when the index knows of none. ABSENT is unknown (an older plugin, or an index never fetched),
     * never "no PR".
     */
    pr?: { readonly state: 'open' | 'merged' | 'closed' | 'none'; readonly number: number | null; readonly url: string | null };
    /**
     * The branch's landing (`/state.worktree_trees[].landing`, the harbor's verdict): how its work reached the
     * base, and through which PR. Null or absent: not landed, or not told.
     */
    landing?: BranchLanding | null;
}

/**
 * What a row's island says of the PR, as its colour (`IslandMark.vue`): `needed` (no PR yet, or a closed one:
 * the reminder), `handled` (open or merged, or the branch's own work landed: a lane merged inside another
 * branch's PR needs no PR of its own), or `unknown` (nothing known: the plain chrome mint). Two hues, never a
 * third: they are the operator's call (`CLAUDE.md` of this directory, the PR row).
 */
export type IslandPr = 'needed' | 'handled' | 'unknown';

/** `island.pr` as the mark reads it (the type's note). Landed wins, whatever the branch's own PR says. */
export function island_pr(island: Pick<WorktreeIsland, 'pr' | 'landing'>): IslandPr {
    if (work_landed(island.landing)) return 'handled';
    const state = island.pr?.state;
    if (state === undefined) return 'unknown';
    return state === 'open' || state === 'merged' ? 'handled' : 'needed';
}

/** The island title's words for a landed branch: the PR that carried it, else that it is on the base. */
function landed_words(landing: BranchLanding): string {
    return landing.pr === null ? 'landed: on main' : `landed in #${landing.pr}`;
}

/**
 * The four steps `POST /create` walks, in order.
 *
 * Re-declared from the plugin's `store.py::CREATE_STEPS` rather than derived —
 * they are two languages either side of an HTTP boundary, so a shared owner is
 * not available. The python side is the original and its test pins the tuple;
 * `create_step_view` below is pinned against this copy.
 */
export const CREATE_STEPS = ['worktree', 'bootstrap', 'session', 'prompt'] as const;

export type CreateStep = (typeof CREATE_STEPS)[number];

/** What the rack draws for a row that is still being created. */
export interface CreateStepView {
    /** Uppercase mono: a label is a head, not prose. */
    label: string;
    /** 1-based, so the meter is never empty on the step that is running. */
    step_index: number;
    step_total: number;
}

/**
 * A creating row's readout, or null when this row is not being created.
 *
 * Null for an absent `create_status` is what makes an older bridge a no-op
 * rather than a broken row. An unrecognised step is treated as step 1 rather
 * than dropped: a future plugin that inserts a step must degrade to "creating,
 * position unknown", never to "not creating".
 */
export function create_step_view(row: WorktreeRow): CreateStepView | null {
    if (row.create_status !== 'creating') return null;
    const found = CREATE_STEPS.indexOf(row.create_step as CreateStep);
    return {
        label: (row.create_step ?? 'creating').toUpperCase(),
        step_index: found >= 0 ? found + 1 : 1,
        step_total: CREATE_STEPS.length,
    };
}

/** What a worktree row's button does — named for the transport action it sends. */
export type WorktreeButtonKind = 'pr' | 'cleanup';

/** What the worktree button reads off a row, and so all a second surface has to carry to
 *  draw the same button in the same words (the office's menu: `agent-office-link.ts`). */
export type WorktreeButtonRow = Pick<WorktreeRow,
    'branch' | 'dirty' | 'ahead' | 'pr_status' | 'pr_url' | 'pr_error' | 'landed' | 'landing' | 'cleanup_status' | 'cleanup_error'>;

/**
 * Did work of the branch's OWN land? The twin of the plugin's
 * `plugins/worktree/worktree/landing.py::work_landed`, which owns why — a
 * branch cut from main and never committed to is an ancestor of it, so `landed`
 * alone would offer CLEAN on every fresh worktree session.
 */
export function work_landed(landing: BranchLanding | null | undefined): boolean {
    if (!landing) return false;
    return landing.how !== 'ancestor' || landing.pr !== null;
}

/**
 * PR or CLEAN?
 *
 * CLEAN once the branch's own work has LANDED and its tree holds nothing new.
 * The rule since 2026-10-31: the PR got merged one way or another, so the
 * button that made it becomes the one that clears up after it. A ✕ would leave
 * the worktree and the branch behind for a later clean-up.
 *
 * `landed` + `dirty` stays PR: that is NEW work after the merge, and it wants a
 * new PR. `dirty: null` (not swept yet) reads CLEAN — the worker re-checks dirt
 * fresh and refuses, never forces. A RUNNING clean-up keeps the button its own,
 * so the readout never flips under it, and so does a PR job in flight. A FAILED
 * one stays CLEAN only while the row would still be CLEAN: its usual refusals
 * (uncommitted work, a tip moved past the merge) are new work, and that wants PR
 * — the error then rides PR's title (`worktree_button_view`) instead of holding
 * a button that can only fail again until the error expires.
 */
export function worktree_button_kind(row: WorktreeButtonRow): WorktreeButtonKind {
    if (row.cleanup_status === 'running') return 'cleanup';
    if (row.pr_status === 'opening') return 'pr';
    return row.landed === true && row.dirty !== true && work_landed(row.landing) ? 'cleanup' : 'pr';
}

/** The worktree button as a rack row draws it. */
export interface WorktreeButtonView {
    kind: WorktreeButtonKind;
    /** One glyph-width vocabulary per kind, so the column does not reflow as a
     *  job moves through it: `PR` `PR?` `PR…` `PR ↗` `PR !`, `CLEAN` `CLEAN?`
     *  `CLEAN…` `CLEAN !`. */
    label: string;
    title: string;
    /** What a press does: `arm` (the second press sends `kind`), `open` the PR on
     *  GitHub (one press, no arm — it writes nothing), or `none` while a job runs. */
    press: 'arm' | 'open' | 'none';
    /** `open` only. */
    url: string | null;
    tone: 'open' | 'error' | null;
}

const CLEANUP_DOES = 'remove this worktree, its branch and this session';

/** Why the work counts as in main, in the words the CLEAN title leads with. An
 *  `ancestor` reaches CLEAN only with its PR (`work_landed`), so it names it too. */
function landing_words(landing: BranchLanding | null | undefined): string {
    const how = landing?.how ?? null;
    const pr = landing?.pr ?? null;
    if (how === 'commits') return 'every commit is in main';
    if (pr !== null) return `PR #${pr} merged`;
    if (how === 'pr') return 'PR merged';
    if (how === 'squash') return 'squash-merged into main';
    return 'in main';
}

/**
 * The worktree button's label, title and press, for `armed` — the kind whose arm
 * is live on this row, or null. An arm is keyed by KIND as well as by row, so a
 * button that changes kind under an arm never inherits it: a `PR?` that became
 * CLEAN must not clean on the next click.
 */
export function worktree_button_view(row: WorktreeButtonRow, armed: WorktreeButtonKind | null): WorktreeButtonView {
    const kind = worktree_button_kind(row);
    const view = (label: string, title: string, press: WorktreeButtonView['press'] = 'arm',
        tone: WorktreeButtonView['tone'] = null, url: string | null = null): WorktreeButtonView =>
        ({ kind, label, title, press, url, tone });
    if (kind === 'cleanup') {
        if (row.cleanup_status === 'running') return view('CLEAN…', `Cleaning up — ${CLEANUP_DOES}…`, 'none');
        if (armed === kind) return view('CLEAN?', `Click again to ${CLEANUP_DOES}`);
        if (row.cleanup_status === 'error') {
            return view('CLEAN !', `Clean-up failed: ${row.cleanup_error ?? 'unknown error'}`, 'arm', 'error');
        }
        return view('CLEAN', `${row.branch}: ${landing_words(row.landing)} — ${CLEANUP_DOES}`);
    }
    // PR's order is unchanged from the button it grew out of: the arm first.
    const press = row.pr_status === 'opening' ? 'none' : row.pr_status === 'open' ? 'open' : 'arm';
    const url = row.pr_status === 'open' ? row.pr_url : null;
    // A clean-up refused because the row stopped being cleanable (`worktree_button_kind`).
    const refused = row.cleanup_status === 'error' ? ` · clean-up refused: ${row.cleanup_error ?? 'unknown error'}` : '';
    if (armed === kind) return view('PR?', 'Click again to commit, push and open the PR', press, null, url);
    if (row.pr_status === 'opening') return view('PR…', 'Pushing and opening the pull request…', press);
    if (row.pr_status === 'open') return view('PR ↗', `Open on GitHub: ${row.pr_url ?? ''}${refused}`, press, 'open', url);
    if (row.pr_status === 'error') return view('PR !', `PR failed: ${row.pr_error ?? 'unknown error'}${refused}`, press, 'error');
    const state = row.dirty ? 'uncommitted changes' : `${row.ahead} commit(s)`;
    return view('PR', `Commit, push and open a PR for ${row.branch} (${state})${refused}`, press);
}

/**
 * What one rack surface renders — the payload every transport carries.
 *
 * Lives here rather than in `agent-rack/rack-transport.ts` for the same reason
 * `RackSession` lives in `multi-terminal-layout.ts`: rack *data* types belong
 * beside the pure code that builds them, and the transport seam stays a seam.
 * `rack-transport.ts` re-exports it, so no importer moved.
 */
export interface RackPayload {
    sessions: RackSession[];
    focused: string | null;
    max_sessions: number;
    summary: string;
    reachable: boolean;
    /** ttyd's port, carried in the payload. A satellite window needs it to build its frame's
     *  address, and only the payload owner knows which bridge it polls. */
    ttyd_port?: number;
    /**
     * False when nothing is registered to service actions, which freezes a
     * satellite window's buttons. Absent (undefined) for a framed rack, whose
     * host is always there to service it.
     *
     * It means only "somebody can service a click". Whether a host has a
     * surface of its own open is the narrower fact `in_world` carries.
     */
    attached?: boolean;
    /**
     * Does the host have its own rack surface open right now? The key is the
     * wire's name for it.
     *
     * Gates what only that surface can serve (a host's coordinator panel).
     * Absent means "not applicable": a framed rack is that surface.
     */
    in_world?: boolean;
    /**
     * Managed git worktrees, as `GET /plugin/worktree/state` reports them.
     *
     * Absent means the bridge has no `worktree` plugin registered — which is the
     * common case on any other machine, and must grow no worktree UI at all.
     * That is structural gating, not a feature flag.
     */
    worktrees?: WorktreeRow[];
    task_groups?: TaskGroup[];
    /** Open missions (`GET /missions`). Absent on a bridge that predates them: no section. */
    missions?: Mission[];
    /**
     * Sessions whose cwd sits inside a NON-main git worktree — every one of
     * them, not only the plugin-managed rows above. Absent when no plugin
     * listed `worktree_paths`, and then the pane behaves exactly as before the
     * key existed. Why it rides the payload: `pane_arg`, below.
     */
    attach_only?: string[];
    /**
     * Why the GATE — never the bridge — is the reason there are no rows.
     *
     * Absent means "the gate is not in the way", so `reachable: false` keeps its
     * original meaning and its original copy. Present, it OUTRANKS `reachable`
     * on screen: a locked gate rejects before any network I/O, so the bridge was
     * never asked and "bridge not running" would be a claim about a machine
     * nothing has spoken to.
     *
     * It rides the payload and is not read in the rack document because the
     * opt-in is MODULE STATE PER DOCUMENT (`local-network-gate.ts` registers
     * no `storage` listener), so a framed rack and a satellite window cannot
     * see, or change, the main window's gate. One owner, pushed.
     */
    access?: AccessNotice;
    /**
     * The bridge's launch profiles — the LAUNCH chips, the row chips' labels,
     * and the capability signal for `+ NEW`'s `POST /spawn`.
     *
     * Absent means the bridge predates `/spawn`: no chips, and `+ NEW` keeps
     * the legacy attach-or-create path. Structural gating again, exactly like
     * `worktrees`; never default it to `[]`, which would read as "a bridge that
     * can spawn, with nothing to spawn".
     */
    /** This payload owner understands a `new` argument containing {profile, cwd}. */
    launch_cwd?: true;
    profile_availability?: import('../../agent-rack/profile-availability-client.ts').ProfileAvailabilityView;
    launch_directories?: import('../../agent-rack/launch-directories-client.ts').LaunchDirectoriesView;
    profiles?: ProfileInfo[];
    /**
     * Where AUTO would launch right now — the AUTO chip's target and title.
     * Absent on a bridge that cannot resolve `profile: "auto"`: no AUTO chip.
     * Display only; the bridge re-picks when `+ NEW` lands.
     */
    launch_pick?: LaunchPick;
    /**
     * The bridge's suggester is on (`shadow` or `live`): the rack REPORTS each control press so the
     * log can score what it would have suggested. Absent on a bridge without one, or with it off —
     * then nothing is reported. Owner: `bridge/tldrack/next_step.py`.
     */
    next_step_mode?: NextStepMode;
    /**
     * One line about a launch that did not go as asked — `/spawn` refused, the
     * CLI is missing, or a worktree's first task was not typed (non-Claude
     * profiles). Expires on its owner's timer; absent when there is nothing to
     * say. A notice, not an error state: the rack keeps working around it.
     */
    launch_notice?: string;
    /**
     * The name a `POST /spawn` is still building. Its row exists only
     * optimistically — tmux has no such session yet, so a rename would 410 —
     * and the rack locks its name box until the pane is up. Absent when no
     * launch is in flight. Set by the HOST, which owns the spawn.
     */
    spawning?: string;
    /**
     * The session the live pane is still loading. NEVER set by a host: only the
     * pane OWNER sees its iframe navigate and `load`, so it overlays this onto
     * the payload it relays (`agent-rack/pane-settle.ts` owns why the rack
     * locks that row's name box meanwhile — see the note there).
     */
    pane_loading?: string;
    /**
     * The DEVICES section: CPU and memory per machine, built by `device-readout.ts::device_rows`.
     * Absent means this bridge has no `devices` plugin, and absent grows no section — the same
     * structural gating as `worktrees`. Present but stale while closed: the poller stops, so
     * the last rows stay for the head's count.
     *
     * EMPTY means the plugin is there and has not been read: the section draws its head alone.
     * The rows come from a poll that runs only while the section is open and on screen
     * (`agent-rack/devices-client.ts`), so a section that waited for an answer before it drew
     * could never be opened again once a page loaded with it closed. Whether the plugin is
     * there is therefore `RackPayloadInput.devices_mounted`, never the poll.
     */
    devices?: DeviceRow[];
    office_resources?: OfficeResources;
    /** Is the DEVICES section open? Sent with `devices`; the host owns it (`'devices'` action). */
    devices_open?: boolean;
    /**
     * The ↺ list — recent closes, one press to reopen, and the sessions a reboot took down,
     * one press (or RESTORE ALL) to restore. Absent while the bridge offers neither, and absent
     * grows no button. The host owns it (`agent-rack/closed-client.ts`; the `'closed'`,
     * `'reopen'`, `'restore'` and `'restore_forget'` actions).
     */
    closed?: ClosedView;
    /**
     * The search box's CHAT half: which live sessions said every term, past what their titles say.
     * Absent while the box is empty. The host owns the ask (`agent-rack/chat-search.ts`; the
     * `'search'` action carries the box's text), and the page shows it only while its box still
     * says `query`.
     */
    chat_search?: ChatSearchView;
    /**
     * Every LEAD run the bridge holds (`GET /lead/state`): the fork's LEAD and the opened lead row's
     * DECISIONS card. Absent on a bridge that predates it, and absent grows neither — the same
     * structural gating as `missions`. Polled by `agent-rack/lead-client.ts::create_lead_poller`.
     */
    lead?: LeadState;
}

/** The payload's `chat_search` key. Built by `agent-rack/chat-search.ts::create_chat_search`. */
export interface ChatSearchView {
    /** The query being asked, normalized (`rack-search.ts::chat_query`). */
    query: string;
    /** A newer question is out; `matches` still answers the last one. */
    pending: boolean;
    /** session → the newest message that matched a term its title did not. */
    matches: Readonly<Record<string, RackSearchHit>>;
    /** Only each live transcript's recent window was read: the session ledger is off or empty. */
    recent_only?: boolean;
    /** Why the chat could not be searched; one line for the readout's tooltip. */
    error?: string;
}

/** The payload's `closed` key. Built by `agent-rack/closed-client.ts::create_closed_list`. */
export interface ClosedView {
    /** How many closes the bridge can offer — the button's number. */
    count: number;
    open: boolean;
    /** The rows, newest first; null until the first read answers. */
    rows: ClosedSession[] | null;
    /** The close a `POST /reopen` is still bringing back. */
    reopening?: string;
    /** Sessions that were running when the machine went down (`GET /restorable`); absent at 0. */
    restorable?: RestorableView;
}

/** `ClosedView.restorable`. A reboot is never a close, so these are their own band. */
export interface RestorableView {
    /** What the bridge can offer — the button's number while it is not 0. */
    count: number;
    /** The rows, longest-running first as the bridge lists them; null until the first read answers. */
    rows: RestorableSession[] | null;
    /** The session a `POST /restore` is bringing back, or `'*'` for RESTORE ALL. */
    restoring?: string;
}

/** Empty rack → the bare noun, so the header never reads as missing data. */
const SUMMARY_PREFIX = 'agents';

/**
 * The rack header: the noun and a COUNT, never a state rollup.
 *
 * A rollup (`1 busy · 1 done · 1 free`) is wrong twice over:
 *
 *  1. **It duplicates the marks.** Every session row already carries its state
 *     mark, so the header would re-encode in words what the column beside it
 *     says.
 *  2. **It is the twitchiest string on the page.** Every state a session passes
 *     through rewrites it, and because the segments are counts, a single agent
 *     flipping between `done` and `free` reflows the WHOLE line several times a
 *     minute. `done` in particular is a transient the operator is meant to
 *     notice on the row, not read as a tally.
 *
 * A count changes only when a session actually appears or disappears, which is
 * real news. It also covers what dots cannot: sessions hidden behind the
 * `+N MORE` overflow row still count here, so the header never under-reports the
 * rack.
 */
export function rack_header(session_count: number): string {
    return session_count > 0 ? `${SUMMARY_PREFIX} · ${session_count}` : SUMMARY_PREFIX;
}

/** A bridge `context_left` → an integer 0–100, or null. The bridge already
 *  validates, but this row crossed a process boundary, so it is checked again. */
export function parse_context_left(raw: unknown): number | null {
    if (typeof raw !== 'number' || !Number.isFinite(raw)) return null;
    return Math.max(0, Math.min(100, Math.round(raw)));
}

/** One snapshot row → one rack row. */
export function to_rack_session(
    row: PortSnapshotRow,
    profiles?: readonly ProfileInfo[],
    services?: readonly DevServiceRow[],
): RackSession {
    const profile = parse_profile_id(row.profile);
    const model_label = format_model_label(row.model, row.usage, { show_cost: false, show_tokens: false });
    const service = service_for_session(row.session, services);
    // A service pane is a plain shell, so "model unknown" there carries zero
    // bits on every row of the band. An agent that runs under a service name
    // keeps the model the bridge read for it.
    const unknown_service = is_service_session(row.session) && model_label === UNKNOWN_MODEL_LABEL;
    const profile_model = profiles?.find((p) => p.id === profile)?.model_label ?? null;
    return {
        session: row.session,
        ...(row.auto_name ? { auto_name: row.auto_name } : {}),
        ...(row.created_at ? { created_at: row.created_at } : {}),
        ...(row.lineage ? { lineage: row.lineage } : {}),
        // The committed post-debounce state (it also carries 'done'), i.e. the
        // same value every other surface here dots.
        state: row.display,
        live: row.live !== false,
        // Cost off: spend has its own readout, and a row has to stay
        // scannable, doubly so on a phone.
        //
        // A profile that names its own model (Codex: the bridge cannot read one
        // off a Claude transcript) fills the gap rather than "model unknown" —
        // only the gap, so a model the bridge DID read always wins.
        model_label: unknown_service
            ? null
            : model_label === UNKNOWN_MODEL_LABEL
              ? (profile_model ?? model_label)
              : model_label,
        ...(profile_model ? { profile_model } : {}),
        context_left: parse_context_left(row.context_left),
        message: row.message,
        // Drives cohort grouping — which agents share a project. `repo` is
        // authoritative (bridge-resolved via git); cwd is the fallback.
        cwd: row.cwd,
        repo: row.repo,
        ...(profile ? { profile } : {}),
        // Background rows only; validated at ingest (`parse_session_row`).
        ...(row.display === 'background' ? { background_jobs: row.background_jobs ?? null } : {}),
        // dev-services knows more about its own pane (`✗ VITE EXIT 1`, the log
        // tail), so its readout wins; any other ended pane reads ✓ DONE / ✗ EXIT n.
        ...(service
            ? { service: service_readout(service) }
            : row.exited
              ? { service: exited_readout(row.session, row.exit_code ?? null) }
              : {}),
        ...(row.exited ? { exited: { code: row.exit_code ?? null } } : {}),
        ...(row.open_work === true ? { open_work: {
            p: row.open_work_p ?? null,
            ...(row.open_work_seen === true ? { seen: true as const } : {}),
            ...(row.children_running ? { children: row.children_running } : {}),
            ...(row.open_work_graded === true ? { graded: true as const } : {}),
            ...(row.open_work_labelled === true ? { labelled: true as const } : {}),
        } } : {}),
        ...(row.stuck ? { stuck: row.stuck } : {}),
        ...(row.work_done === true && row.open_work !== true ? { work_done: {
            p: row.open_work_p ?? null,
            ...(row.answer_to_read === true ? { answer: true as const } : {}),
            ...(row.answer_read === true ? { read: true as const } : {}),
            ...(row.open_work_labelled === true ? { labelled: true as const } : {}),
        } } : {}),
        ...(row.next_step ? { next_step: row.next_step } : {}),
        ...(row.lead ? { lead: row.lead } : {}),
        ...(row.stop_failure ? { stop_failure: row.stop_failure } : {}),
        ...(row.fresh === true ? { fresh: true as const } : {}),
        ...(row.starred !== undefined ? { starred: row.starred } : {}),
        ...(row.activity ? { activity: rack_activity(row.activity) } : {}),
    };
}

/** The longest activity detail a rack row carries — a tooltip line, never a whole command. */
export const RACK_ACTIVITY_DETAIL_MAX = 80;

/** A snapshot row's activity, re-validated (it crossed a process boundary) and its detail clipped. */
function rack_activity(raw: SessionActivity): SessionActivity {
    const detail = typeof raw.detail === 'string' && raw.detail ? raw.detail : null;
    return {
        zone: String(raw.zone),
        tool: typeof raw.tool === 'string' && raw.tool ? raw.tool : null,
        detail: detail && detail.length > RACK_ACTIVITY_DETAIL_MAX ? `${detail.slice(0, RACK_ACTIVITY_DETAIL_MAX - 1)}…` : detail,
    };
}

/**
 * Which session the live pane should show after a rack update.
 *
 * One owner, because a copy per surface that owns a pane is exactly the shape that drifts.
 *
 * Two rules, both load-bearing:
 *
 * 1. **Adopt against the LIVE set only.** Focusing a dead name points ttyd at
 *    `?arg=<name>`, and under `-a` that is `tmux new -A -s <name>` — it would
 *    silently RESURRECT the session the operator just killed.
 * 2. **`pending_new` is the deliberate exception.** A `+ NEW` session does not
 *    exist in the snapshot until its iframe has loaded and its socket has
 *    connected. Without this clause the re-adopt yanks focus to some other
 *    session on the very next poll and the new one is never created at all.
 * 3. **A rename in flight is not a death.** Owned by `rename-tracker.ts` — see
 *    the note there.
 */
export function adopt_focus(
    current: string | null,
    live: readonly string[],
    pending_new?: string | null,
    renames?: RenameFollower,
): string | null {
    if (current && (live.includes(current) || current === pending_new)) return current;
    const renamed = current ? renames?.follow(current, live) : null;
    return renamed ?? live[0] ?? null;
}

/** The live subset of a snapshot, by name. Absent `live` counts as alive — never
 *  drop a session on missing data from an older bridge. */
export function live_sessions(rows: readonly PortSnapshotRow[]): string[] {
    return rows.filter((row) => row.live !== false).map((row) => row.session);
}

export interface RackViewInput {
    /** Already dead-filtered by the ingestor — see divergence 4. */
    rows: PortSnapshotRow[];
    focused: string | null;
    reachable: boolean;
    max_sessions: number;
    ttyd_port?: number;
    /** Omit for a framed rack (its host is always attached). */
    attached?: boolean;
    /** Omit where the question does not apply. */
    in_world?: boolean;
    /** Omit where no `worktree` plugin answered. */
    worktrees?: WorktreeRow[];
    task_groups?: TaskGroup[];
    /** Omit where the bridge did not answer — see `RackPayload.missions`. */
    missions?: Mission[];
    /** Omit where no plugin shipped `worktree_paths` — see `RackPayload.attach_only`. */
    attach_only?: string[];
    /** Every non-main worktree (`WorktreeState.worktree_trees`); omit where no
     *  plugin listed them. Joined onto each session as its island here. */
    islands?: readonly WorktreeIsland[];
    /** Omit where the gate cannot be the blocker — see `RackPayload.access`. */
    access?: AccessNotice;
    /** Omit on a bridge that sent no `profiles` — see `RackPayload.profiles`. */
    profiles?: ProfileInfo[];
    /** Omit on a bridge that sent no `launch_pick` — see `RackPayload.launch_pick`. */
    launch_pick?: LaunchPick;
    /** Omit when the bridge sent no `next_step` — see `RackPayload.next_step_mode`. */
    next_step_mode?: NextStepMode;
    /** `GET /plugin/dev-services/state`'s rows; omit where it did not answer.
     *  Joined onto `svc-*` rows here, so no payload key carries them. */
    services?: readonly DevServiceRow[];
    /** Omit when there is nothing to say — see `RackPayload.launch_notice`. */
    launch_notice?: string;
    /** A `/spawn` launch the snapshot may not list yet (the owner's pending
     *  name, set only while it carries a profile). Joins `attach_only` at once,
     *  so the pane's FIRST navigation to it is already `attach:` — waiting for
     *  the row would change the URL a poll later and reload the pane. */
    pending_profiled?: string | null;
    /** The owner's in-flight `/spawn` name, or null — see `RackPayload.spawning`. */
    spawning?: string | null;
    /** `GET /plugin/devices/state`; omit where it did not answer — see `RackPayload.devices`. */
    devices?: DevicesState;
    devices_open?: boolean;
    /**
     * Does the bridge mount the `devices` plugin (`PortSnapshot.plugins`)? Omit where the owner
     * cannot say. Then a section this device CLOSED keeps its head: the stored flag is proof the
     * section was once on this page, and opening it asks. See `RackPayload.devices`.
     */
    devices_mounted?: boolean;
    /** Omit while there is nothing to reopen — see `RackPayload.closed`. */
    closed?: ClosedView;
    /** Omit while the search box is empty — see `RackPayload.chat_search`. */
    chat_search?: ChatSearchView;
    /** Omit where the bridge did not answer — see `RackPayload.lead`. */
    lead?: LeadState;
}

/**
 * Sessions that must be addressed `attach:` because their cwd is inside a
 * non-main worktree — the `attach_only` half of the payload.
 *
 * `pane_arg` already protects the plugin-MANAGED rows by session name. That
 * leaves every other tree on the machine (a checkout an agent CLI made for
 * itself) exposed to the same bug: kill the agent working there, click its row,
 * and ttyd's attach-or-create quietly rebuilds the session in the main
 * checkout. The plugin lists every non-main worktree path on `/state`, and
 * this joins them to the snapshot by cwd containment.
 *
 * The main checkout is never in `worktree_paths` (the plugin excludes it), so
 * a main-checkout session keeps the creating form `+ NEW` depends on.
 */
export function attach_only_sessions(
    rows: readonly { session: string; cwd?: string | null }[],
    worktree_paths: readonly string[],
): Set<string> {
    const out = new Set<string>();
    if (worktree_paths.length === 0) return out;
    for (const row of rows) {
        const cwd = row.cwd;
        if (!cwd) continue;
        if (worktree_paths.some((root) => path_contains(root, cwd))) out.add(row.session);
    }
    return out;
}

/**
 * The managed worktree a session STANDS IN — the join the PR button and the
 * `pr` action key on.
 *
 * By name first: the plugin derives `wt-<slug>` from the branch and ships it,
 * so a session the rack's `+ WORKTREE` (or `wt create` from a shell) made
 * matches outright. Then by cwd containment against the rows' `path`, longest
 * root winning (`path-containment.ts` `longest_containing_root`): a `+ NEW` pane that
 * ran `claude -w`, or a session that called `EnterWorktree`, carries an
 * ordinary tmux name and its only tell is where it is. The bridge reports where
 * CLAUDE is, not where the pane's shell is (`snapshot.apply_hook_cwds`), so the
 * cwd here is trustworthy for that case.
 *
 * Absent or empty `worktrees` (no plugin answered) is `undefined`, and the
 * name-only behaviour this replaces is a strict subset of the result.
 */
export function worktree_for_session(
    session: { session: string; cwd?: string | null },
    worktrees: readonly WorktreeRow[] | undefined,
): WorktreeRow | undefined {
    if (!worktrees || worktrees.length === 0) return undefined;
    const by_name = worktrees.find((row) => row.session === session.session);
    if (by_name) return by_name;
    const cwd = session.cwd;
    if (!cwd) return undefined;
    const root = longest_containing_root(cwd, worktrees.map((row) => row.path));
    return root === null ? undefined : worktrees.find((row) => row.path === root);
}

/**
 * Sessions a running CLEAN is about to kill — never adopted into the live pane.
 *
 * Its worker kills the tree's derived session and the pressed row's session,
 * which joins by cwd when it is a different one (`worktree_for_session`); any
 * OTHER session still in the tree keeps it (`landing.await_vacated` in the
 * plugin), so this set is the most the press can take. A pane that re-adopted
 * one between the press and the kill would only show "session is gone" a moment
 * later. Read off `/state`, so a CLEAN pressed on another surface moves this
 * pane off once its poll sees it running; a kill that lands before that is
 * harmless only because a worktree session's pane is already attach-only
 * (`held_pane_arg`), never because of this set.
 */
export function cleaning_sessions(
    sessions: readonly { session: string; cwd?: string | null }[],
    worktrees: readonly WorktreeRow[] | undefined,
): Set<string> {
    const out = new Set<string>();
    const running = (worktrees ?? []).filter((row) => row.cleanup_status === 'running');
    if (running.length === 0) return out;
    for (const row of running) out.add(row.session);
    for (const session of sessions) {
        if (worktree_for_session(session, worktrees)?.cleanup_status === 'running') out.add(session.session);
    }
    return out;
}

/**
 * The non-main worktree a session stands in — the rack row's island.
 *
 * Why it exists: an agent working in a worktree edits files the main checkout
 * never sees, and without the island nothing on its row says so. A `wt-<slug>`
 * name is a weak tell (a rename erases it), and a `+ NEW` session that ran
 * `EnterWorktree` or `claude -w` mid-session carries an ordinary name and no
 * tell at all, so its changes get looked for in the wrong tree.
 *
 * Joined by cwd against EVERY non-main tree, not only the plugin-managed rows
 * `worktree_for_session` covers: a checkout an agent CLI made for itself is an
 * island too. The bridge reports where CLAUDE is (`snapshot.apply_hook_cwds`),
 * so the move shows on the next hook. Longest root wins, for nested trees.
 */
export function island_for_session(
    session: { cwd?: string | null },
    islands: readonly WorktreeIsland[] | undefined,
): WorktreeIsland | undefined {
    const cwd = session.cwd;
    if (!cwd || !islands || islands.length === 0) return undefined;
    const root = longest_containing_root(cwd, islands.map((island) => island.path));
    return root === null ? undefined : islands.find((island) => island.path === root);
}

/** Lower-case, every non-alphanumeric run one `-`, dash-fenced — a segment
 *  test, so branch `fix` is not "said" by a session named `fixer`. */
function name_segments(text: string): string {
    return `-${text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')}-`;
}

/**
 * The checkout's own folder, for a tree with no branch to print. The last path
 * segment, unless it is just the repo's name: `~/workspaces/spike/my-app`
 * is `spike`, the segment that tells it apart.
 */
function island_folder(path: string, repo: string | null | undefined): string {
    const segments = path.split('/').filter((segment) => segment !== '' && segment !== '~');
    const last = segments.at(-1) ?? path;
    if (repo && segments.length > 1 && last.toLowerCase() === repo.toLowerCase()) return segments.at(-2)!;
    return last;
}

/**
 * What a row's island draws: a `label` (the branch, else the folder) and the
 * sentence `title`. `label` is null when the row's own name already says it —
 * `wt-feat-nav` on `feat/nav` would say the branch twice; the island glyph
 * still draws, because the
 * glyph, not the text, is what says "not the main checkout".
 */
export function session_island(
    island: WorktreeIsland,
    shown_name: string,
    repo?: string | null,
): SessionIsland {
    const text = island.branch || island_folder(island.path, repo);
    const said = name_segments(shown_name).includes(name_segments(text));
    const where = island.branch
        ? `branch ${island.branch} · ${island.path}`
        : island.branch === null
          ? `detached HEAD · ${island.path}`
          : island.path;
    const pr = island_pr(island);
    const landed = work_landed(island.landing) ? island.landing! : null;
    const number = island.pr?.number == null ? '' : ` #${island.pr.number}`;
    const pr_words = landed ? ` · ${landed_words(landed)}`
        : pr === 'unknown' ? '' : pr === 'handled' ? ` · PR${number} ${island.pr!.state}` : island.pr!.state === 'closed' ? ` · PR${number} closed, none open` : ' · no PR yet';
    return {
        path: island.path,
        label: said ? null : text,
        title: `Worktree: ${where}${pr_words} — this agent's edits land there, not in the main checkout`,
        pr,
        ...(landed ? { landed: true as const } : {}),
        // A landed island whose own PR reads none or closed sends only its colour: `island-status.ts` reads an
        // exact state ahead of the colour, and that state is not its story any more. Open or merged still says it.
        ...(island.pr && !(landed && (island.pr.state === 'none' || island.pr.state === 'closed'))
            ? { pr_state: island.pr.state } : {}),
    };
}

/**
 * A rack session with its island joined. Every surface that builds rack rows
 * applies this ONE join, because the island decides the COMPLETE
 * band (`complete-sessions.ts`): a surface that skipped it would file a
 * worktree agent as finished, and row N on one surface would stop being row N on
 * another.
 */
export function with_island(session: RackSession, islands: readonly WorktreeIsland[] | undefined): RackSession {
    const island = island_for_session(session, islands);
    if (!island) return session;
    return { ...session, island: session_island(island, session.auto_name || session.session, session.repo) };
}

/** Assemble the full payload. Pure: same input, same output, no clock. */
export function build_rack_payload(input: RackViewInput): RackPayload {
    const sessions = input.rows.map((row) =>
        with_island(to_rack_session(row, input.profiles, input.services), input.islands));
    const payload: RackPayload = {
        launch_cwd: true,
        sessions,
        focused: input.focused,
        max_sessions: input.max_sessions,
        summary: rack_header(input.rows.length),
        reachable: input.reachable,
    };
    // Both are genuinely optional: an absent `attached` means "not applicable",
    // which is NOT the same as `false`, and writing the key unconditionally
    // would freeze every framed rack.
    if (input.ttyd_port !== undefined) payload.ttyd_port = input.ttyd_port;
    if (input.attached !== undefined) payload.attached = input.attached;
    if (input.in_world !== undefined) payload.in_world = input.in_world;
    if (input.worktrees !== undefined) payload.worktrees = input.worktrees;
    if (input.task_groups !== undefined) payload.task_groups = input.task_groups;
    if (input.missions !== undefined) payload.missions = input.missions;
    const attach_only = attach_only_with_profiled(input.attach_only, sessions, input.pending_profiled ?? null);
    if (attach_only !== undefined) payload.attach_only = attach_only;
    if (input.access !== undefined) payload.access = input.access;
    if (input.profiles !== undefined) payload.profiles = input.profiles;
    if (input.launch_pick !== undefined) payload.launch_pick = input.launch_pick;
    if (input.next_step_mode !== undefined) payload.next_step_mode = input.next_step_mode;
    if (input.launch_notice !== undefined) payload.launch_notice = input.launch_notice;
    if (input.spawning) payload.spawning = input.spawning;
    if (input.devices !== undefined) {
        const labels = new Map(sessions.map((row) => [row.session, row.auto_name || row.session]));
        payload.devices = device_rows(input.devices, labels);
        payload.devices_open = input.devices_open ?? true;
        payload.office_resources = office_resources(input.devices, payload.devices_open);
    } else if (input.devices_mounted ?? input.devices_open === false) {
        // Unread, not absent: owned by `RackPayload.devices` — see the note there.
        payload.devices = [];
        payload.devices_open = input.devices_open ?? true;
    }
    if (input.closed !== undefined) payload.closed = input.closed;
    if (input.chat_search !== undefined) payload.chat_search = input.chat_search;
    if (input.lead !== undefined) payload.lead = input.lead;
    return payload;
}

/**
 * `attach_only` plus every PROFILED session — owned here, once, rather than in
 * each payload owner, which only compute the
 * worktree half and only when a worktree plugin answered.
 *
 * A profiled session was made by `POST /spawn` (or attributed to a profile by
 * its transcript), so nothing about it depends on ttyd's attach-or-create — and
 * keeping that form would let a killed `vc-3` be quietly resurrected as an
 * EMPTY SHELL the moment its row is clicked or ttyd reconnects (~30 ms). The
 * `attach:` grammar answers "session is gone" instead.
 *
 * Absent stays absent when nothing qualifies, so a pre-profile bridge's payload
 * is byte-identical to before.
 */
function attach_only_with_profiled(
    attach_only: string[] | undefined,
    sessions: readonly RackSession[],
    pending_profiled: string | null,
): string[] | undefined {
    const profiled = sessions.filter((s) => s.profile).map((s) => s.session);
    if (pending_profiled) profiled.push(pending_profiled);
    if (profiled.length === 0) return attach_only;
    return [...new Set([...(attach_only ?? []), ...profiled])];
}

/**
 * The `?arg=` value the live pane must use for `session`. **THE worktree
 * invariant, in one place.**
 *
 * `ttyd_session_script()` runs `tmux new -A -s "$1" -c '<global start_dir>'` —
 * attach-or-CREATE. So a bare `?arg=<name>` pointed at a worktree session that
 * has DIED silently creates a fresh session of that name in the MAIN CHECKOUT:
 * an agent that believes it is isolated while editing `main`. That is the exact
 * failure worktree sessions exist to prevent, and it is reachable by ordinary
 * means — kill the agent, then click its row.
 *
 * The `attach:` grammar refuses to create, printing "session is gone" instead.
 *
 * A plain UNPROFILED session keeps the creating form: on a bridge without
 * `POST /spawn`, `+ NEW` still works purely by pointing the pane at an unused
 * name. A profiled one (made by `/spawn`) is in `attach_only` — see
 * `attach_only_with_profiled`.
 *
 * Takes the session being navigated to rather than reading `payload.focused`:
 * the pane is navigated by argument, and a derived-from-focus boolean would be
 * answering a different question than the caller is asking.
 *
 * ── Why a THIRD argument, and why it is a set of names ──────────────────────
 *
 * `worktrees` covers the rows the plugin MANAGES, joined by session name. On a
 * busy machine those are the minority; the rest are checkouts an agent CLI
 * made for itself, whose sessions carry ordinary names and whose only tell is
 * a cwd inside the tree. `attach_only` is that second population, already
 * resolved to session names by `attach_only_sessions` at the payload build,
 * because a pane owner that is not the payload owner (a satellite window) runs
 * no worktree poller, so the payload is the only thing it has to decide from. Absent, or empty, means "no plugin listed any paths",
 * and the answer is byte-identical to before the argument existed.
 */
export function pane_arg(
    session: string,
    worktrees?: readonly WorktreeRow[],
    attach_only?: ReadonlySet<string>,
): string {
    const managed = worktrees?.some((row) => row.session === session) ?? false;
    return managed || attach_only?.has(session) === true ? attach_only_arg(session) : session;
}

/**
 * `pane_arg` for an owner that RECOMPUTES its URL from every payload
 * (`AgentRackWindow`'s `ttyd_src`): `previous` is the arg the pane is already on, and
 * attach-only never downgrades for the session it names.
 *
 * Why: a tree's managed row and its `attach_only` entry leave `/state` when the
 * tree is removed — CLEAN, the harbor's REMOVE WORKTREE — often a poll before
 * the snapshot reports its session dead. Recomputed without the hold, the
 * focused session's arg turns bare, the pane renavigates, and ttyd's
 * attach-or-create rebuilds the session in the MAIN checkout: `pane_arg`'s
 * whole hazard, from the other side.
 * Holding the form costs nothing — attaching a live session is what the bare
 * form does too — and the one bare form that matters is a NEW name (`+ NEW`'s
 * legacy door), which no pane was ever on.
 */
export function held_pane_arg(
    session: string,
    previous: string | null | undefined,
    worktrees?: readonly WorktreeRow[],
    attach_only?: ReadonlySet<string>,
): string {
    const attach = attach_only_arg(session);
    return previous === attach ? attach : pane_arg(session, worktrees, attach_only);
}
