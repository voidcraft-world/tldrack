/**
 * Pure layout for the multi-terminal RACK: one live terminal, and a row for
 * every other agent (the shape herdr gets right).
 *
 * Deliberately NOT "N live terminals". Each live pane is a frame with its own
 * renderer, socket and backing store, so a wall of them is a memory wall, not a
 * feature. Instead: ONE live terminal plus cheap text rows, and clicking a row
 * moves the live pane.
 *
 * Free of three and of the DOM, so `node --test` runs it directly. This module
 * decides *what is shown*; a surface maps the rows onto what it draws and
 * attaches the callbacks.
 *
 * Its imports are siblings that are themselves dependency-free, which keeps the
 * run-it-under-bare-node property intact.
 */
import type { SessionLineage } from '@tldrack/schemas/session-lineage.ts';
import type { NextStep } from '@tldrack/schemas/next-step.ts';
import type { SessionLead } from '@tldrack/schemas/lead.ts';
import { session_families, flatten_family, visible_families } from './session-families.ts';
import { render_query, type TerminalRenderOptions } from './terminal-render-options.ts';
import {
    background_jobs_label,
    type SessionActivity,
    type StopFailure,
    type StuckReading,
    type TerminalDisplayState,
} from './terminal-status-logic.ts';

/** Laya's open-work verdict as a row carries it (`RackSession.open_work`). */
export interface OpenWorkMark {
    p: number | null;
    /** Parked by the operator ("seen, not now"): draw no ⚠️ (`tldrack/attention.py` § 3). */
    seen?: true;
    /** Live descendants still working: the ⚠️ draws as ⏳ (`attention.py` § 1). */
    children?: number;
    /** A ✓ STILL OPEN press left this turn graded: the ⚠️ stays, its menu drops the ✓. */
    graded?: true;
}

/** Laya's done verdict as a row carries it (`RackSession.work_done`). */
export interface WorkDoneMark {
    p: number | null;
    /** A question opened the turn and its pane has not been opened since: 📖 in the ✅'s place
     *  (`answer_to_read_shown`). Owned by `bridge/tldrack/open_work.py` — see § An answer to read there. */
    answer?: true;
}
import { SERVICES_COHORT_LABEL, is_service_session, type ServiceReadout } from './service-sessions.ts';
import { COMPLETE_COHORT_LABEL, is_complete_session } from './complete-sessions.ts';

/**
 * The local ttyd server's port. One owner, for the same reason
 * `DEFAULT_STATUS_PORT` exists in `config/terminal-status-flags.ts` (which is
 * this port + 1): nothing else spells the literal. Here and not in a config
 * module because this file already owns `ttyd_url()` and is cheap to import.
 */
export const DEFAULT_TTYD_PORT = 7681;

/**
 * The base size of one live terminal pane, for a host that draws the rack as an
 * object of a fixed size and scales its panel from this. Here for the same
 * reason as the port: one owner of the literal.
 */
export const DEFAULT_TERMINAL_WIDTH = 1000;
export const DEFAULT_TERMINAL_HEIGHT = 600;

/** How a rack row should be rendered. `note` is the dimmed line under a session
 *  row — today only {@link DEAD_SESSION_TEXT}. */
export type RackRowKind = 'cohort' | 'session' | 'note' | 'overflow' | 'empty';

/** Actions a session row can offer. `rename` doubles as "the name box may edit". */
export type RackActionId = 'focus' | 'rename' | 'kill';

/** The display state vocabulary — an alias, so a new state is one edit there. */
export type RackState = TerminalDisplayState;

export interface RackSession {
    lineage?: SessionLineage;
    session: string;
    auto_name?: string;
    /** The office relay's resolved rack status mark; owned by session-status-emoji.ts. */
    status_emoji?: string;
    /** Carried from the snapshot's `created_at` (`SessionStatus.created_at`). */
    created_at?: number;
    state: RackState;
    /** False once the tmux session is gone but the bridge still remembers it. */
    live: boolean;
    /** Short model/cost label, already formatted by the caller. */
    model_label?: string | null;
    /** The model this session's launch profile names (`ProfileInfo.model_label`),
     *  so a row on that profile need not print it ({@link usual_model_label}). */
    profile_model?: string | null;
    /** Percent of the context window still free (0–100), from Claude Code's
     *  status line. Null/absent when unknown — non-Claude rows, before the first
     *  reply, right after /compact. Validated by the caller (`to_rack_session`). */
    context_left?: number | null;
    /** Why the session wants you — shown verbatim when state is `attention`. */
    message?: string | null;
    /** Jobs a `background` session is running (null = uncounted) — the row's
     *  secondary text and the dot's title, via `background_jobs_label`. */
    background_jobs?: number | null;
    /** The session's working directory, straight from the bridge snapshot.
     *  Only a FALLBACK for grouping — see `repo`. */
    cwd?: string | null;
    /** The git repo this session is in, resolved by the bridge (worktrees folded
     *  onto their parent). The cohort key when present. */
    repo?: string | null;
    /** The launch profile this session runs (`RackPayload.profiles` has its
     *  label). Absent/null on a pre-profile bridge or an unattributed session. */
    profile?: string | null;
    /** A `svc-*` row's dev-services readout (`● VITE :5173`), or an ended pane's
     *  `✓ DONE` / `✗ EXIT 1` (`exited_readout`), drawn in place of the model
     *  label. Absent otherwise. */
    service?: ServiceReadout | null;
    /** Laya judged the last turn to leave planned work open — the ⚠️ badge
     *  (`open_work_badge_shown` decides where it draws). `seen`: the operator parked
     *  it ("seen, not now"); `children`: live descendants still working, so it draws
     *  as ⏳. Both owned by `tldrack/attention.py`. Absent otherwise. */
    open_work?: OpenWorkMark;
    /** Laya judged the last turn DONE — ✅/☑️ in place of 💬/○ (`work_done_shown`
     *  decides where). Never beside `open_work`. Absent otherwise. `answer`: a question
     *  opened that turn and its pane has not been opened since (`answer_to_read`). */
    work_done?: WorkDoneMark;
    /** The suggested next step — a SUGGESTION the row's chip and the canvas card draw; nothing runs
     *  until [Do it]. Present only in the bridge's `live` mode. Owner:
     *  `bridge/tldrack/next_step.py` — see the note there. */
    next_step?: NextStep;
    /** A LEAD session: the run it leads (`RackPayload.lead` holds it) and its open decisions — the
     *  opened row's DECISIONS card (`agent-rack/LeadDecisionsCard.vue`). Absent on every other row. */
    lead?: SessionLead;
    /** The last turn was CUT OFF by an API error, mostly the session limit — the
     *  mark that replaces 💬/○ (`stop_failure_shown` decides where). Absent otherwise. */
    stop_failure?: StopFailure;
    /** A busy row looks stuck — 🧱 beside its mark (`stuck_shown`). Absent otherwise. */
    stuck?: StuckReading;
    /** A new conversation nobody has prompted yet — the 🆕 that replaces ○
     *  (`fresh_session_shown` decides where). Absent otherwise. */
    fresh?: true;
    /** The operator's star: a session to come back to. Present (true or false) only when the bridge
     *  keeps stars, so its absence means "draw no control". Owner: `agent-rack/star-client.ts`. */
    starred?: boolean;
    /** The pane has ENDED (the bridge's `exited`): a finished plugin job kept on
     *  screen for its log, safe to close. Its readout rides `service`. */
    exited?: { code: number | null };
    /** The non-main worktree this session stands in, ready to draw — built by
     *  `rack-view.ts::session_island`. Absent in the main checkout, or where no
     *  worktree plugin listed its trees. */
    island?: SessionIsland;
    /** What the agent's last classified tool call was (the bridge's PreToolUse
     *  zone), for the OFFICE's screens (`agent-office-logic.ts`). The rack's own
     *  rows draw nothing from it. Absent when nothing is classified. */
    activity?: SessionActivity;
}

/** A row's island: the session works in a non-main git worktree. */
export interface SessionIsland {
    /** `~`-collapsed worktree root. */
    path: string;
    /** The chip's text — the branch, else the checkout's folder; null when the
     *  row's own name already says it. */
    label: string | null;
    /** The sentence version, for `title` / `aria-label`. */
    title: string;
    /** The branch's PR, as the island's colour: owned by `rack-view.ts::island_pr`. Absent reads as unknown. */
    pr?: 'needed' | 'handled' | 'unknown';
    /** Exact PR state, when known; older senders only provide the colour. */
    pr_state?: 'none' | 'open' | 'closed' | 'merged';
    /** The branch's work is on the base (`rack-view.ts::work_landed`): its readout says LANDED, not a PR state. */
    landed?: true;
}

/** How urgent a context-left reading is. `normal` draws in the row's own colour. */
export type ContextTone = 'normal' | 'low' | 'critical';

/** Context-left thresholds: the owner. Inclusive: 25% left is `low`. */
export const CONTEXT_LOW_PCT = 25;
export const CONTEXT_CRITICAL_PCT = 10;

export function context_tone(left: number | null | undefined): ContextTone | null {
    if (typeof left !== 'number' || !Number.isFinite(left)) return null;
    if (left <= CONTEXT_CRITICAL_PCT) return 'critical';
    if (left <= CONTEXT_LOW_PCT) return 'low';
    return 'normal';
}

/** One ` · `-separated piece of a session row. `text` on {@link RackRow} is
 *  these joined, so a surface that only draws strings (the rack board) still
 *  gets every piece; a DOM surface can tint by `tone`. */
export interface RowSegment {
    text: string;
    tone?: ContextTone;
    /** The session-name piece (always the first). Its `text` carries the `▶`
     *  focus mark for the string surfaces; a DOM surface draws `RackRow.session`
     *  as the rename box instead. */
    name?: true;
    /** The sentence version, for a DOM `title`. Absent → the surface's own. */
    title?: string;
    /** What the piece IS, where a DOM surface gives it its own place: the
     *  context-left reading (a column), the attention message (a second line) or
     *  the unusual model (inline only while the rack is too narrow for its MODEL
     *  column, which prints `RackRow.model` on every row).
     *  Absent → inline detail (service readout, background jobs). */
    role?: 'context' | 'message' | 'model';
}

export interface RackRow {
    node_id?: string;
    ancestors?: string[];
    depth?: number;
    child_count?: number;
    attention_count?: number;
    missing_parent?: boolean;
    auto_name?: string;
    /** Carried from {@link RackSession.created_at}: the LABEL button's `since`. */
    created_at?: number;
    kind: RackRowKind;
    /** Owning session; absent on `overflow` / `empty`. */
    session?: string;
    /** Cohort header rows only: the cohort's label — its identity, where `text`
     *  carries counts that change every poll ({@link rack_row_key}). */
    cohort?: string;
    /** A nested heading's parent cohort, retained by search and star filtering. */
    cohort_parent?: string;
    /** Session count for a heading whose label and count are drawn separately. */
    cohort_count?: number;
    text: string;
    /** Session rows only: `text` before it was joined. */
    segments?: RowSegment[];
    /** Note/secondary rows render dimmed. */
    dim: boolean;
    /** The session currently shown in the live pane. */
    focused: boolean;
    state?: RackState;
    actions: RackActionId[];
    /** Carried from {@link RackSession.profile} — the row's profile chip. */
    profile?: string | null;
    /** Live rows: {@link RackSession.model_label}, even when the row's text
     *  dropped it as the usual model — the mark's tooltip still names it. */
    model?: string;
    /** Carried from {@link RackSession.background_jobs} on a `background` row. */
    background_jobs?: number | null;
    /** Carried from {@link RackSession.open_work}. */
    open_work?: OpenWorkMark;
    /** Carried from {@link RackSession.stuck}, live rows only. */
    stuck?: StuckReading;
    /** Carried from {@link RackSession.work_done}. */
    work_done?: WorkDoneMark;
    /** Carried from {@link RackSession.next_step}, on a live row only. */
    next_step?: NextStep;
    /** Carried from {@link RackSession.stop_failure}. */
    stop_failure?: StopFailure;
    /** Carried from {@link RackSession.fresh}. */
    fresh?: true;
    /** Carried from {@link RackSession.starred}. */
    starred?: boolean;
    /** Carried from {@link RackSession.exited}, on a live row only. */
    exited?: { code: number | null };
    /** Carried from {@link RackSession.island}. */
    island?: SessionIsland;
    /** The row sits in the COMPLETE band: its header, and every session and
     *  note row under it (`complete-sessions.ts` owns the rule). A DOM surface
     *  folds the band on this. */
    complete?: true;
}

export interface RackInput {
    sessions: RackSession[];
    /** Session shown in the live terminal, if any. */
    focused: string | null;
    max_sessions: number;
}

/** Marker for a session the bridge remembers but tmux no longer has. A dead
 *  session must LOOK dead — Orca's issue #5626 is exactly this failure: a lost
 *  session that renders like a fresh prompt reads as "my work vanished". */
export const DEAD_SESSION_TEXT = '— session gone —';

/** Prefix for sessions the NEW button creates. Kept short and inside the
 *  bridge's `[A-Za-z0-9_-]` session-name charset. */
export const NEW_SESSION_PREFIX = 'vc';

/**
 * First unused `vc-<n>` name — the name `+ NEW` asks for.
 *
 * On a bridge with `POST /spawn` the name rides the request and the bridge
 * refuses a taken one (409); on an older bridge ttyd's `-a` makes
 * `?arg=<name>` a `tmux new -A -s <name>`, so pointing the pane at an unused
 * name IS the creation (`agent-rack/spawn-client.ts::launch_new` owns both).
 *
 * Either way collision-avoidance is the job: reusing a live name would
 * silently ATTACH to somebody else's running agent instead of starting a new
 * one, so the caller must pass every name the bridge currently knows about —
 * dead/store-only rows included, since those names are still claimed.
 */
export function next_session_name(existing: Iterable<string>): string {
    const taken = new Set(existing);
    for (let i = 1; i <= 999; i += 1) {
        const candidate = `${NEW_SESSION_PREFIX}-${i}`;
        if (!taken.has(candidate)) return candidate;
    }
    // 999 live sessions is not a real state; fall back to something unique-ish
    // rather than returning a name that would attach to an existing agent.
    return `${NEW_SESSION_PREFIX}-${taken.size + 1000}`;
}

/**
 * ttyd client option that removes ttyd's own `beforeunload` handler.
 *
 * WITHOUT it, switching sessions in the rack pops Chrome's "Leave site? Changes
 * you made may not be saved." ttyd's `onWindowUnload` calls `preventDefault()`
 * and sets `returnValue` (its own message is discarded by the browser), and
 * picking a session IS a navigation of the terminal frame — `?arg=<name>` is
 * the only way to choose one — so the dialog would fire on the rack's most
 * common action. It bites hardest on the rack page, where the operator clicks
 * and types in that frame, so it always holds the sticky user activation the
 * dialog requires.
 *
 * Carried on the URL rather than left to the bridge's `-t disableLeaveAlert=true`
 * so it works against an ALREADY-RUNNING bridge with no restart. Both are set;
 * they converge on the same ttyd code path (`applyPreferences` →
 * `removeEventListener('beforeunload', …)`).
 */
const LEAVE_ALERT_OFF = 'disableLeaveAlert=true';

/**
 * The ttyd URL for a session, or `about:blank` when there is none.
 *
 * Shared by every surface that frames a live pane, so no two can drift into pointing at
 * subtly different sessions.
 *
 * `voidcraft` is special: it is the bridge's own default session, which ttyd
 * already attaches to at the bare root, so passing it as `?arg=` would work but
 * is redundant. Every other name goes through `-a`'s attach-or-create. Note it
 * still needs its own `?` — it is the one branch with no query string of its
 * own to append to.
 *
 * `render` seeds the pane's font/renderer options. ttyd applies URL options
 * LAST, so these beat the bridge's `-t fontSize=…` flags and need no restart
 * (see `terminal-render-options.ts`). Omit it and the URL is byte-for-byte what
 * it always was — the caller then gets the bridge's server-side defaults.
 *
 * `dpi_scale` is the caller's supersample factor: 1 for a page, more for a
 * host that renders the frame at `scale`× logical pixels and shrinks it back.
 *
 * `origin` is the REMOTE path (a rack pointed at another machine: see
 * `agent-rack/rack-remote.ts`). When given it replaces the whole
 * `http://localhost:<port>` base, so `port` is then unused: an origin already
 * carries its own. Omit it and the URL is byte-for-byte what it always was,
 * which is the only reason every existing call site can stay untouched.
 *
 * **Callers must never pass an unvalidated origin here.** This function is a
 * string builder and deliberately does no checking; `normalize_remote_origin`
 * is where that decision lives, and it is the only thing standing between a
 * page anyone can open and framing an arbitrary host as "your terminal".
 */
export function ttyd_url(
    port: number,
    session: string | null,
    render?: TerminalRenderOptions,
    dpi_scale: number = 1,
    origin?: string,
): string {
    if (!session) return 'about:blank';
    const base = origin ?? `http://localhost:${port}`;
    const options = render ? `&${render_query(render, dpi_scale)}` : '';
    if (session === 'voidcraft') return `${base}/?${LEAVE_ALERT_OFF}${options}`;
    return `${base}/?arg=${encodeURIComponent(session)}&${LEAVE_ALERT_OFF}${options}`;
}

// ── cohorts ────────────────────────────────────────────────────────────────
//
// "Which agents are working in the same space" is answered by `cwd` and nothing
// else in the snapshot. Grouping on it is what turns a flat rack list into
// something a supervisor could reason about ("A, B and C all finished in
// my-app"), and it is the reason `cwd` is threaded this far.

/** Sessions sharing a project, plus the label its header row shows. */
export interface RackCohort {
    label: string;
    sessions: RackSession[];
    /** The SERVICES band (`service-sessions.ts` owns why it exists). */
    services?: true;
    /** The COMPLETE band (`complete-sessions.ts` owns why it exists). Built by
     *  `build_rack_rows`, never by `group_into_cohorts`: it is decided per
     *  FAMILY, which a cohort of family roots can no longer see. */
    complete?: true;
}

/** Cohort for sessions the bridge gave no path for. Sorts last — it is a
 *  bucket of unknowns, not a project. */
export const UNGROUPED_COHORT_LABEL = '(no path)';

/** Split on both separators and drop empties, so a trailing slash, a doubled
 *  slash and a Windows path all reduce to the same segment list. */
function path_segments(cwd: string): string[] {
    return cwd.split(/[/\\]+/).filter((part) => part.length > 0);
}

/** Trailing worktree marker: `-wt`, `_worktree2`, `.worktrees`, `-wt-3`… Kept
 *  deliberately narrow (anchored, separator-led) so an ordinary project that
 *  merely contains those letters is not truncated. */
const WORKTREE_SUFFIX_RE = /[-_.](?:wt|worktree|worktrees)[-_.]?\d*$/i;

/** Fold a worktree checkout back onto its project, so `my-app` and
 *  `my-app-wt-3` are ONE cohort — parallel agents in per-worktree checkouts
 *  are exactly the case this grouping exists for. */
export function normalize_cohort_label(segment: string): string {
    return segment.replace(WORKTREE_SUFFIX_RE, '') || segment;
}

/**
 * One session's cohort label, or null when it has nothing to group on.
 *
 * `repo` first, and it is authoritative: the bridge resolves it with
 * `git rev-parse --git-common-dir`, which folds a linked worktree onto its
 * PARENT repo. Git-derived names are used verbatim — they are already canonical.
 *
 * The `cwd` fallback is only for sessions git knows nothing about (a plain shell
 * in ~ or /tmp), and it is deliberately PER-SESSION — its own directory name and
 * nothing else.
 *
 * An earlier version labelled by "the segment past the prefix every session
 * shares". That was wrong in a way worth recording: the shared prefix is a
 * property of the WHOLE rack, so one outlier session dragged it shallow for
 * everyone. With checkouts of one repository at `~/code/my-app`,
 * `~/workspaces/<name>/my-app` and `~/worktrees/my-app/<name>` the only common
 * prefix is `~`, so ONE repo rendered as three cohorts named `code`, `workspaces`
 * and `worktrees`, and a single shell sitting in `~` merged two unrelated repos
 * into a cohort called `code`. Never let one row's path decide another row's label.
 */
export function cohort_label_for(entry: RackSession): string | null {
    if (entry.repo) return entry.repo;
    if (!entry.cwd) return null;
    const segments = path_segments(entry.cwd);
    if (segments.length === 0) return null;
    return normalize_cohort_label(segments[segments.length - 1]);
}

/**
 * Group sessions by project. See `cohort_label_for` for how the key is chosen.
 *
 * Service sessions (`config/service-session-flags.ts` lists their prefixes)
 * form ONE band by name before any repo is consulted — `service-sessions.ts`
 * owns why. The band ranks like any cohort (a member that blocks lifts it), and
 * loses a TIE to every project, because machinery is never the first thing to
 * read. `cohort_label_for` itself stays repo-only: a surface that places a
 * session by its project puts a `svc-vite` at its checkout, where it runs.
 */
export function group_into_cohorts(sessions: RackSession[]): RackCohort[] {
    const by_label = new Map<string, RackSession[]>();
    const without_path: RackSession[] = [];
    const services: RackSession[] = [];
    for (const entry of sessions) {
        if (is_service_session(entry.session)) {
            services.push(entry);
            continue;
        }
        const label = cohort_label_for(entry);
        if (label === null) {
            without_path.push(entry);
            continue;
        }
        const bucket = by_label.get(label);
        if (bucket) bucket.push(entry);
        else by_label.set(label, [entry]);
    }

    const cohorts: RackCohort[] = [];
    for (const [label, members] of by_label) cohorts.push({ label, sessions: members });
    if (services.length > 0) cohorts.push({ label: SERVICES_COHORT_LABEL, sessions: services, services: true });
    // A blocked member makes its whole group look blocked (herdr's rollup rule),
    // so the cohort holding whoever needs you sorts first.
    cohorts.sort(
        (a, b) =>
            cohort_rank(a.sessions) - cohort_rank(b.sessions) ||
            Number(a.services === true) - Number(b.services === true) ||
            a.label.localeCompare(b.label),
    );
    if (without_path.length > 0) cohorts.push({ label: UNGROUPED_COHORT_LABEL, sessions: without_path });
    return cohorts;
}

/** States worth counting in a header, in the order they're read. */
const ROLLUP_TERMS: { state: RackState; word: string }[] = [
    { state: 'attention', word: 'blocked' },
    { state: 'busy', word: 'working' },
    { state: 'background', word: 'background' },
    { state: 'done', word: 'done' },
];

/**
 * The `2 blocked · 1 working · 3 done` summary for one cohort.
 *
 * Zero counts are omitted so the header stays scannable, but the string is
 * never empty — an all-idle cohort still says how many sessions it holds, since
 * a header with no numbers reads as missing data rather than as "nothing doing".
 */
export function cohort_rollup(sessions: RackSession[]): string {
    const parts: string[] = [];
    for (const { state, word } of ROLLUP_TERMS) {
        const count = sessions.filter((entry) => entry.live && entry.state === state).length;
        if (count > 0) parts.push(`${count} ${word}`);
    }
    const gone = sessions.filter((entry) => !entry.live).length;
    if (gone > 0) parts.push(`${gone} gone`);
    if (parts.length === 0) parts.push(`${sessions.length} idle`);
    return parts.join(' · ');
}

/**
 * A cohort header's text. The SERVICES band reads a COUNT, never the agent
 * rollup: a dev server is a plain shell, which the rollup would call `idle`
 * while it is serving — a false word where `rack_header`'s count rule fits.
 * The COMPLETE band counts too: its rollup would only say `N done` again.
 */
export function cohort_header_text(cohort: RackCohort): string {
    return cohort.services || cohort.complete
        ? `${cohort.label} · ${cohort.sessions.length}`
        : `${cohort.label} — ${cohort_rollup(cohort.sessions)}`;
}

/**
 * The rack's rows, in render order.
 *
 * Overflow is EXPLICIT — sessions past `max_sessions` become a `+N MORE` row
 * rather than being silently dropped: a bare `slice()` reads as "that's everything".
 * Sessions wanting
 * attention are hoisted to the top, since finding the one that needs you is the
 * entire reason to look at this panel.
 *
 * Cohort headers appear only when there is more than one cohort: a single-project
 * rack is the common case and a lone header above every row would be pure noise.
 * The COMPLETE band is the exception and always draws its header, last: it is
 * a divider, and without it nothing on screen says those rows are finished.
 */
export function build_rack_rows(input: RackInput): RackRow[] {
    const { sessions, focused, max_sessions } = input;
    if (sessions.length === 0) {
        return [{ kind: 'empty', text: 'No tmux sessions', dim: true, focused: false, actions: [] }];
    }

    const families = session_families(sessions, family_rank);
    const shown = visible_families(families, max_sessions);
    const hidden = sessions.length - shown.reduce((sum, root) => sum + root.count, 0);
    const rows: RackRow[] = [];
    // Project cohorts contain whole families, including cross-repo children.
    const roots = new Map(shown.map((root) => [root.entry.session, root]));
    // A family's rank is its most urgent member's, so it reads COMPLETE_RANK
    // exactly when every member is complete (a missing parent starts past it).
    const finished = shown.filter((root) => root.rank === COMPLETE_RANK);
    const cohorts = group_into_cohorts(shown.filter((root) => root.rank !== COMPLETE_RANK).map((root) => ({
        ...root.entry,
        live: root.rank < DEAD_RANK,
        state: (Object.entries(URGENCY_RANK).find(([, rank]) => rank === root.rank)?.[0] ?? 'unknown') as RackState,
    })));
    const show_headers = cohorts.length > 1;
    if (finished.length > 0) {
        cohorts.push({ label: COMPLETE_COHORT_LABEL, sessions: finished.map((root) => root.entry), complete: true });
    }
    const usual_model = usual_model_label(sessions);

    for (const cohort of cohorts) {
        const band = cohort.complete ? { complete: true as const } : {};
        if (show_headers || cohort.complete) {
            rows.push({
                kind: 'cohort',
                cohort: cohort.label,
                text: cohort_header_text({ ...cohort, sessions: cohort.sessions.flatMap((entry) =>
                    flatten_family(roots.get(entry.session)!).filter(({ node }) => !node.missing).map(({ node }) => node.entry)) }),
                dim: false,
                focused: false,
                actions: [],
                ...band,
            });
        }
        for (const representative of sort_sessions(cohort.sessions)) {
            const root = roots.get(representative.session)!;
            for (const { node, depth, ancestors } of flatten_family(root)) {
                const entry = node.entry;
                const family_fields = { node_id: node.id, depth, ancestors,
                    child_count: node.count - (node.missing ? 0 : 1), attention_count: node.attention,
                    missing_parent: node.missing };
                const is_focused = entry.session === focused;
                const row_text = session_row_text(entry, is_focused, usual_model);
                rows.push({
                    kind: 'session',
                    ...family_fields,
                    session: entry.session,
                    ...(entry.auto_name ? { auto_name: entry.auto_name } : {}),
                    ...(entry.created_at ? { created_at: entry.created_at } : {}),
                    ...row_text,
                    text: `${'  '.repeat(depth)}${depth ? '↳ ' : ''}${row_text.text}${node.missing ? " · parent closed" : ""}`,
                    dim: !entry.live,
                    focused: is_focused,
                    state: entry.state,
                    // A dead session can't be focused into a live pane; only
                    // clearing it makes sense.
                    actions: node.missing ? [] : entry.live ? session_actions(is_focused, is_service_session(entry.session)) : ['kill'],
                    ...(entry.profile ? { profile: entry.profile } : {}),
                    ...(entry.live && entry.model_label ? { model: entry.model_label } : {}),
                    ...(entry.state === 'background' ? { background_jobs: entry.background_jobs ?? null } : {}),
                    ...(entry.open_work ? { open_work: entry.open_work } : {}),
                    ...(entry.live && entry.stuck ? { stuck: entry.stuck } : {}),
                    ...(entry.work_done ? { work_done: entry.work_done } : {}),
                    ...(entry.live && entry.next_step ? { next_step: entry.next_step } : {}),
                    ...(entry.stop_failure ? { stop_failure: entry.stop_failure } : {}),
                    ...(entry.fresh ? { fresh: true as const } : {}),
                    ...(entry.starred !== undefined ? { starred: entry.starred } : {}),
                    ...(entry.live && entry.exited ? { exited: entry.exited } : {}),
                    ...(entry.island ? { island: entry.island } : {}),
                    ...band,
                });

                if (!entry.live && !node.missing) {
                    rows.push({
                        kind: 'note',
                        ...family_fields,
                        session: entry.session,
                        text: DEAD_SESSION_TEXT,
                        dim: true,
                        focused: false,
                        actions: [],
                        ...band,
                    });
                }
            }
        }
    }

    if (hidden > 0) {
        rows.push({ kind: 'overflow', text: `+${hidden} MORE`, dim: true, focused: false, actions: [] });
    }
    return rows;
}

/** A row's identity across rebuilds — what a keyed list keys on. `overflow`
 *  and `empty` occur at most once per build, so their kind is enough. */
export function rack_row_key(row: RackRow): string {
    if (row.kind === 'session' || row.kind === 'note') return `${row.kind}:${row.session ?? ''}`;
    if (row.kind === 'cohort') return `cohort:${row.cohort ?? row.text}`;
    return row.kind;
}

/**
 * `fresh`'s rows in `held`'s ORDER — the DOM rack renders this while a rename
 * is open, so the rack re-sorting cannot move the row being typed into.
 *
 * Why: a keyed `v-for` keeps the row's node but REORDERS by moving it
 * (`insertBefore`), and moving a focused `<input>` blurs it, which throws the
 * half-typed name away. A `+ NEW` row is the common victim: it arrives as
 * `unknown` with no cwd (bottom rung, `(no path)` cohort) and climbs
 * `unknown → free → busy` while its agent boots, a second after the name box
 * unlocks. Content stays live; only the order holds.
 *
 * Removing a node and inserting one never move the others, so: a held row
 * still in `fresh` keeps its place, a gone one drops, and a row the hold never
 * saw goes in right after the fresh row that precedes it.
 */
export function hold_row_order(held: readonly string[], fresh: readonly RackRow[]): RackRow[] {
    const unplaced = new Map(fresh.map((row) => [rack_row_key(row), row]));
    const out: RackRow[] = [];
    for (const key of held) {
        const row = unplaced.get(key);
        if (!row) continue;
        out.push(row);
        unplaced.delete(key);
    }
    let anchor = -1;
    for (const row of fresh) {
        if (!unplaced.has(rack_row_key(row))) {
            anchor = out.indexOf(row);
            continue;
        }
        out.splice(anchor + 1, 0, row);
        anchor += 1;
    }
    return out;
}

/**
 * Urgency ladder, lower first:
 *
 *   needs you → finished-unseen → free → background → working → unknown → gone
 *
 * Shared by row ordering and cohort ordering so a group can never sort by a
 * different rule than the rows inside it.
 *
 * **`free` outranks `busy`, and that is not arbitrary.** The ladder answers one
 * question — *how much does this row want you* — and a working agent wants you
 * least of anything alive: there is nothing to do but let it run. A free agent
 * is a prompt waiting for work. Sorting by that ordering is what lets the dot
 * column be read on its own as the rack's state, which is the whole reason the
 * header no longer spells the same thing out in words (see `rack-view.ts`
 * `rack_header`).
 *
 * `busy` and `free` used to share rank 2 and fall through to a name sort, so the
 * dots interleaved by whatever the sessions happened to be called and the column
 * carried no signal at all.
 *
 * `background` sits between them on the same question: the turn is over, so it
 * wants you a little — closing it would kill the work, and the jobs it reports
 * are worth a glance — but nothing is asked of you until they end.
 *
 * Exported so another surface ranks by this table and not by a copy.
 */
export const URGENCY_RANK: Readonly<Record<RackState, number>> = {
    attention: 0,
    done: 1,
    free: 2,
    background: 3,
    busy: 4,
    unknown: 5, // alive, but the bridge has nothing on it
};
/** A session tmux no longer has — one past the lowest live rung, derived so a
 *  new state can never land beside (or below) it. */
const DEAD_RANK = Math.max(...Object.values(URGENCY_RANK)) + 1;

/**
 * A COMPLETE session, for the row budget only: past even a dead one, so a
 * finished family is the first to go behind `+N MORE` (a dead row is still a
 * "my work vanished" signal; a complete one has nothing left to say, and the
 * operator has already seen it — `complete-sessions.ts` rule 2). Rows INSIDE the
 * band still order by {@link session_rank}.
 */
const COMPLETE_RANK = DEAD_RANK + 1;

function session_rank(entry: RackSession): number {
    if (!entry.live) return DEAD_RANK;
    // Own-key: a word outside the vocabulary (`constructor` included) ranks as
    // unknown rather than NaN, which would make the sort comparator inconsistent.
    return Object.hasOwn(URGENCY_RANK, entry.state) ? URGENCY_RANK[entry.state] : URGENCY_RANK.unknown;
}

/** What a family is ranked by when the row budget is spent and it is ordered
 *  ({@link COMPLETE_RANK}); everything else is {@link session_rank}. */
function family_rank(entry: RackSession): number {
    return is_complete_session(entry) ? COMPLETE_RANK : session_rank(entry);
}

/** A cohort inherits its most urgent member's rank. Seeded past the bottom rung
 *  so an empty cohort can never outrank one holding a dead session. */
function cohort_rank(sessions: RackSession[]): number {
    let best = COMPLETE_RANK + 1;
    for (const entry of sessions) best = Math.min(best, session_rank(entry));
    return best;
}

/** Attention first, then live-before-dead, then name. Total and stable, so the
 *  rack doesn't reshuffle between polls for reasons the operator can't see. */
function sort_sessions(sessions: RackSession[]): RackSession[] {
    return [...sessions].sort((a, b) => {
        const delta = session_rank(a) - session_rank(b);
        return delta !== 0 ? delta : a.session.localeCompare(b.session);
    });
}

function session_actions(is_focused: boolean, is_service: boolean): RackActionId[] {
    // The focused session is already in the live pane, so offering "focus"
    // again would be a dead control. `rename` is offered on LIVE rows only —
    // build_rack_rows gives a dead session kill-only, and there is no tmux
    // session left for `tmux rename-session` to find — and never on a service
    // row, whose name its plugin looks for (`service-sessions.ts`).
    const actions: RackActionId[] = is_focused ? ['rename', 'kill'] : ['focus', 'rename', 'kill'];
    return is_service ? actions.filter((id) => id !== 'rename') : actions;
}

/**
 * The model most live rows run, when at least two share it — else null.
 *
 * A label every row repeats is wallpaper: the same model id printed on every
 * row carries zero bits on all of them and takes the width the context reading
 * needs. So a row prints its model only
 * where it DIFFERS — from this, and from its own profile's model. The full model
 * still rides `RackRow.model` for a tooltip. One row is no rack: with nothing
 * to differ from, it keeps its model.
 */
export function usual_model_label(sessions: readonly RackSession[]): string | null {
    const counts = new Map<string, number>();
    for (const entry of sessions) {
        if (!entry.live || entry.service || !entry.model_label) continue;
        counts.set(entry.model_label, (counts.get(entry.model_label) ?? 0) + 1);
    }
    let best: string | null = null;
    let best_n = 1;
    for (const [label, n] of counts) {
        if (n > best_n) [best, best_n] = [label, n];
        // A tie names no usual model: neither is the background the other differs from.
        else if (n === best_n) best = null;
    }
    return best;
}

function session_row_text(entry: RackSession, is_focused: boolean, usual_model: string | null): { text: string; segments: RowSegment[] } {
    const name = entry.auto_name || entry.session;
    const segments: RowSegment[] = [{ text: is_focused ? `▶ ${name}` : name, name: true }];
    // LIVE rows only. A dead row already carries DEAD_SESSION_TEXT on the
    // note line right under it, so a model label there is a second telling —
    // and the worse of the two, since it describes a session that is gone.
    // A supervised service shows its own state instead: it has no model.
    if (entry.live && entry.service) {
        const { text, tone, title } = entry.service;
        segments.push({ text, ...(tone ? { tone } : {}), title });
    } else if (entry.live && entry.model_label && entry.model_label !== usual_model && entry.model_label !== entry.profile_model) {
        segments.push({ text: entry.model_label, role: 'model' });
    }
    // Beside the model it qualifies — "when do I start a new session" is read
    // off this. Live rows only, for the same reason as the model.
    const tone = entry.live ? context_tone(entry.context_left) : null;
    if (tone) segments.push({ text: `${entry.context_left}% left`, tone, role: 'context' });
    // Only surface the message when it's the reason you're being summoned;
    // otherwise it's stale narration of whatever last happened.
    if (entry.state === 'attention' && entry.message) segments.push({ text: entry.message, role: 'message' });
    // A background row's reason to look twice: work you would kill by closing it.
    if (entry.live && entry.state === 'background') segments.push({ text: background_jobs_label(entry.background_jobs) });
    return { text: segments.map((segment) => segment.text).join(' · '), segments };
}

