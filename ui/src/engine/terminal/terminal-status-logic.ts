/**
 * Pure logic for terminal agent-status: bridge URL/port math, tmux-title state
 * hints, staleness handling, and the per-session state tracker (debounce +
 * seen-bit). No browser dependencies, so it runs under `node --test`
 * (tests/unit/terminal-status-logic.test.ts).
 *
 * State vocabulary matches the tldrack status listener
 * (bridge/tldrack/main.py): busy | background | attention |
 * free | unknown. The tracker adds a client-only display state 'done' — the
 * agent finished while the operator wasn't watching; it persists until mark_seen().
 *
 * THE vocabulary: every other surface's state union is an alias of
 * `TerminalDisplayState`, so the next state is a one-place edit plus the
 * exhaustive switches the compiler then points at.
 */
import { session_lineage_schema, type SessionLineage } from '@tldrack/schemas/session-lineage.ts';
import { next_step_schema, type NextStep, type NextStepMode } from '@tldrack/schemas/next-step.ts';
import { session_crew_schema, session_lead_schema, type SessionCrew, type SessionLead } from '@tldrack/schemas/lead.ts';


/** `background`: owned by bridge/tldrack/agent_index.py — see § BACKGROUND WORK there. */
export type TerminalHookState = 'busy' | 'background' | 'attention' | 'free' | 'unknown';
export type TerminalDisplayState = TerminalHookState | 'done';

const HOOK_STATES: readonly TerminalHookState[] = ['busy', 'background', 'attention', 'free', 'unknown'];

/** A state word off the wire → the vocabulary. An unrecognised word (a newer
 *  bridge's state) is `unknown`, explicitly — never cast through, where it would
 *  index every state table to `undefined`. */
export function as_hook_state(raw: unknown): TerminalHookState {
    return HOOK_STATES.find((state) => state === raw) ?? 'unknown';
}

/** The text for a `background` session: `1 background job`, `3 background
 *  jobs`, or `background work` when the bridge has no count (its
 *  `background_jobs` is null — background agents carry no pid to count).
 *  `short` is for tight readouts: `1 bg job`, `3 bg jobs`, `background`. */
export function background_jobs_label(jobs: number | null | undefined, form: 'long' | 'short' = 'long'): string {
    const short = form === 'short';
    if (typeof jobs !== 'number' || !Number.isFinite(jobs) || jobs < 1) return short ? 'background' : 'background work';
    const count = Math.floor(jobs);
    const noun = short ? 'bg job' : 'background job';
    return count === 1 ? `1 ${noun}` : `${count} ${noun}s`;
}

/** What the agent is doing right now, classified by the bridge from the
 *  PreToolUse hook payload (tldrack/activity.py). `zone` is an
 *  architecture-zone key — computer | codebase | aws | web — that says which
 *  part of the machine the agent is working in (the office draws it);
 *  `tool`/`detail` are display-only extras. */
export interface SessionActivity {
    zone: string;
    tool: string | null;
    detail: string | null;
}

/** Cumulative Claude token usage for a session, summed across the transcript's
 *  assistant messages by the bridge (tldrack/transcript.py). */
export interface SessionUsage {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
}

/** One row of the bridge's GET /snapshot (superset of GET /status). */
export interface SessionStatus {
    lineage?: SessionLineage;
    session: string;
    /** First-prompt display label; session remains the stable transport key. */
    auto_name?: string;
    /** When tmux created the session (`#{session_created}`, epoch seconds). A rename keeps it, and it tells
     *  this session from an older one that held the same `vc-N` name. Absent on older bridges. */
    created_at?: number;
    state: TerminalHookState;
    age_seconds: number | null;
    message: string | null;
    title: string | null;
    attached: boolean;
    /** The active pane's working directory (`#{pane_current_path}`) — the ONLY
     *  field that maps a session to a project/worktree, which is what lets the
     *  rack group sessions into cohorts. Absent on pre-cwd bridges and on the
     *  legacy /status fallback; null when tmux reported an empty path. */
    cwd?: string | null;
    /** The git repo the session's cwd belongs to, resolved BY THE BRIDGE via
     *  `rev-parse --git-common-dir` so linked worktrees fold onto their parent
     *  repo. This is the cohort key — prefer it over `cwd`, which cannot answer
     *  "same project" from path text alone. Absent on pre-repo bridges; null
     *  when the session is not inside a git repo. */
    repo?: string | null;
    /** False when the tmux session no longer exists (the bridge keeps killed
     *  sessions' last reported state forever as "store-only" rows). Absent on
     *  pre-2026-07-12 bridges and on the legacy /status fallback path. */
    live?: boolean;
    /** Absent on pre-activity bridges (< 2026-07-12) and the /status fallback;
     *  null when nothing has been classified for the session yet. */
    activity?: SessionActivity | null;
    /** Running Claude model id (e.g. `claude-opus-4-8`), read from the session's
     *  transcript on each hook. Absent on pre-model bridges / the /status
     *  fallback; null when no Claude session has reported in this tmux session. */
    model?: string | null;
    /** Cumulative token usage for the current Claude session. Same availability
     *  caveats as `model`. */
    usage?: SessionUsage | null;
    /** Percent of the context window still free (0–100), off Claude Code's
     *  status line. Absent when unknown (pre-first-reply, post-/compact, a
     *  non-Claude row, an older bridge). Validate before use (`parse_context_left`). */
    context_left?: number | null;
    /** The session's explicitly picked icon id (POST /session-icon). Absent on pre-icon bridges; null when nothing was picked —
     *  either way, render the shared FNV-1a default for the session name. */
    icon?: string | null;
    /** The launch profile this session runs (`claude-a`, `codex-a`, …): the
     *  tmux `@vc_profile` stamp `POST /spawn` sets, overridden by the profile
     *  whose config root holds the session's transcript. Absent on pre-profile
     *  bridges; null for a session nothing can attribute (a hand-made tmux
     *  shell). Validated by `parse_profile_id` before anything renders it. */
    profile?: string | null;
    /** A long goal's progress, folded by the bridge from the agent's own task
     *  list (TodoWrite / TaskCreate / TaskUpdate — `tldrack/activity.py`).
     *  Absent on older bridges; null while the agent keeps no list. Crossed HTTP,
     *  so validate it before drawing it. */
    tasks?: { done: number; total: number; current: string | null } | null;
    /** How many background jobs a `background` row is running; null when the
     *  bridge cannot count them. Absent on other rows and older bridges.
     *  Validated by `parse_session_rows`; render with `background_jobs_label`. */
    background_jobs?: number | null;
    /** Laya's turn-end verdict: did the agent's closing message leave planned
     *  work open? The bridge thresholds it (`tldrack/open_work.py` owns
     *  why); null = no verdict (no Laya, a fresh turn, or dismissed). Absent on
     *  older bridges. Validated by `parse_session_row`. */
    open_work?: boolean | null;
    /** The probability behind `open_work` (0–1), for the mark's title. */
    open_work_p?: number | null;
    /** The same verdict's other end: p under the done cut, so ✅ replaces 💬
     *  (`work_done_shown`; the cuts are owned by `open_work.py`). Absent on older
     *  bridges, false on a verdict from before the done band. */
    work_done?: boolean | null;
    /** The ✅'s turn was opened by a question, and the operator has not opened its pane since: an
     *  answer to read. Owned by `bridge/tldrack/open_work.py` — see § An answer to read there.
     *  Absent on older bridges. */
    answer_to_read?: boolean | null;
    /** The operator starred this session to come back to it (`POST /session-star`). On EVERY row of a
     *  bridge that keeps stars and absent on an older one, which is how the rack knows whether to
     *  draw the control. Owned by `bridge/tldrack/stars.py` — see the note there. */
    starred?: boolean;
    /** How many of this session's live descendants (lineage) are still working, in the
     *  background or at a prompt: its ⚠️ draws as ⏳. Owner: `tldrack/attention.py`
     *  § 1. Absent at 0 and on older bridges. */
    children_running?: number;
    /** The ⚠️'s loop is parked — "seen, not now" (attention.py § 3): draw no ⚠️. */
    open_work_seen?: boolean;
    /** The ⚠️ retells an open loop the operator has not answered (attention.py § 2). */
    open_work_repeat?: boolean;
    /** A ✓ STILL OPEN press left this ⚠️'s turn graded (`POST /open-work/confirm`; the label is the
     *  press's, or one the text already had): the menu offers no second ✓. Absent otherwise and on
     *  older bridges. */
    open_work_graded?: boolean;
    /** A busy row circling (the same command failed again) or silent (no progress).
     *  Owner: `tldrack/stuck.py`. Validated by `parse_stuck`. */
    stuck?: StuckReading | null;
    /** The turn did not finish — an API error CUT IT OFF (the session or weekly
     *  limit, mostly). Live turn-over rows only; absent otherwise and on older
     *  bridges. Owner: `tldrack/transcript.py` `fold_stop_failure`.
     *  Validated by `parse_stop_failure`. */
    stop_failure?: StopFailure | null;
    /** A new conversation nobody has prompted yet — `free`, but not a reply you
     *  read. Live `free` rows only; absent otherwise and on older bridges.
     *  Owner: `tldrack/main.py` § Fresh sessions. */
    fresh?: boolean;
    /** The session is live but its pane has ENDED — a plugin job held on screen
     *  for its log. Live rows only; absent while the pane runs and on older
     *  bridges. Owner: `tldrack/snapshot.py` `TMUX_DEAD_FIELD`. */
    exited?: true;
    /** The ended pane's exit status (a signal reads 128 + n); null when tmux gave none. */
    exit_code?: number | null;
    /** The suggested next step, in the bridge's `live` mode only (`tldrack/next_step.py`).
     *  Validated by `parse_session_row` against `schemas/src/next-step.ts`; dropped when malformed. */
    next_step?: NextStep;
    /** A LEAD session: the run it leads and its open decisions (`tldrack/lead/`). Validated by
     *  `parse_session_row` against `schemas/src/lead.ts`; dropped when malformed, absent on other rows. */
    lead?: SessionLead;
    /** A worker session: the run and step it works (`tldrack/lead/`). Same validation as `lead`. */
    crew?: SessionCrew;
}

/** Why a turn was cut off, off Claude Code's own synthetic error entry. */
export interface StopFailure {
    /** Claude Code's word: `rate_limit`, `overloaded`, `authentication_failed`, … */
    error: string;
    /** The limit window for a rate limit (`five_hour`, `seven_day`); null otherwise. */
    limit: string | null;
    /** Epoch seconds the limit lifts — exact, where the notice text rounds. */
    resets_at: number | null;
    /** Claude Code's one-line notice, verbatim ("You've hit your session limit · …"). */
    message: string | null;
}

/** The bridge's words are lowercase tokens; anything else is not one of them. */
const STOP_FAILURE_TOKEN = /^[a-z0-9_]{1,40}$/;

/** Why a busy row looks stuck (`tldrack/stuck.py`): the same command failed
 *  `count` times this turn, or no hook for `seconds`. */
export interface StuckReading {
    kind: 'repeat_failure' | 'no_progress';
    count: number | null;
    seconds: number | null;
}

/** A `stuck` field off the wire, or null when it is not one. Pure. */
export function parse_stuck(value: unknown): StuckReading | null {
    if (typeof value !== 'object' || value === null) return null;
    const raw = value as Record<string, unknown>;
    if (raw.kind !== 'repeat_failure' && raw.kind !== 'no_progress') return null;
    const whole = (n: unknown) => (typeof n === 'number' && Number.isInteger(n) && n >= 0 ? n : null);
    return { kind: raw.kind, count: whole(raw.count), seconds: whole(raw.seconds) };
}

/** A `stop_failure` field off the wire, or null when it is not one. Pure. */
export function parse_stop_failure(value: unknown): StopFailure | null {
    if (typeof value !== 'object' || value === null) return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.error !== 'string' || !STOP_FAILURE_TOKEN.test(raw.error)) return null;
    const resets = raw.resets_at;
    return {
        error: raw.error,
        limit: typeof raw.limit === 'string' && STOP_FAILURE_TOKEN.test(raw.limit) ? raw.limit : null,
        resets_at: typeof resets === 'number' && Number.isFinite(resets) && resets > 0 ? resets : null,
        message: typeof raw.message === 'string' && raw.message ? raw.message.slice(0, 300) : null,
    };
}

/**
 * One `/snapshot` row (poll, SSE frame, or a legacy `/status` body), checked at
 * the boundary. Null for anything that is not an object with a session name.
 * `state` goes through `as_hook_state`, so nothing downstream ever sees a word
 * outside the vocabulary; `background_jobs` is a whole count or null. The rest
 * of the row passes through — its consumers validate the fields they draw.
 */
export function parse_session_row(value: unknown): SessionStatus | null {
    if (typeof value !== 'object' || value === null) return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.session !== 'string') return null;
    const row: SessionStatus = { ...(raw as unknown as SessionStatus), state: as_hook_state(raw.state) };
    if (raw.lineage !== undefined) {
        const parsed = session_lineage_schema.safeParse(raw.lineage);
        if (parsed.success) row.lineage = parsed.data;
        else delete row.lineage;
    }
    if (raw.auto_name !== undefined) {
        const title = raw.auto_name;
        if (typeof title === 'string' && title.length > 0 && title.length <= 56 && !/[\x00-\x1f\x7f]/.test(title)) {
            row.auto_name = title;
        } else delete row.auto_name;
    }
    if (raw.created_at !== undefined) {
        const at = raw.created_at;
        if (typeof at === 'number' && Number.isInteger(at) && at > 0) row.created_at = at;
        else delete row.created_at;
    }
    if (raw.background_jobs !== undefined) {
        const jobs = raw.background_jobs;
        row.background_jobs =
            typeof jobs === 'number' && Number.isInteger(jobs) && jobs >= 0 ? jobs : null;
    }
    if (raw.open_work !== undefined) row.open_work = typeof raw.open_work === 'boolean' ? raw.open_work : null;
    if (raw.work_done !== undefined) row.work_done = typeof raw.work_done === 'boolean' ? raw.work_done : null;
    if (raw.answer_to_read !== undefined) row.answer_to_read = typeof raw.answer_to_read === 'boolean' ? raw.answer_to_read : null;
    if (raw.starred !== undefined && typeof raw.starred !== 'boolean') delete row.starred;
    if (raw.open_work_p !== undefined) {
        const p = raw.open_work_p;
        row.open_work_p = typeof p === 'number' && Number.isFinite(p) && p >= 0 && p <= 1 ? p : null;
    }
    if (raw.stop_failure !== undefined) row.stop_failure = parse_stop_failure(raw.stop_failure);
    if (raw.fresh !== undefined) row.fresh = raw.fresh === true;
    if (raw.next_step !== undefined) {
        const parsed = next_step_schema.safeParse(raw.next_step);
        if (parsed.success) row.next_step = parsed.data;
        else delete row.next_step;
    }
    if (raw.lead !== undefined) {
        const parsed = session_lead_schema.safeParse(raw.lead);
        if (parsed.success) row.lead = parsed.data;
        else delete row.lead;
    }
    if (raw.crew !== undefined) {
        const parsed = session_crew_schema.safeParse(raw.crew);
        if (parsed.success) row.crew = parsed.data;
        else delete row.crew;
    }
    if (raw.children_running !== undefined) {
        const n = raw.children_running;
        if (typeof n === 'number' && Number.isInteger(n) && n > 0) row.children_running = n;
        else delete row.children_running;
    }
    if (raw.open_work_seen !== undefined) row.open_work_seen = raw.open_work_seen === true;
    if (raw.open_work_repeat !== undefined) row.open_work_repeat = raw.open_work_repeat === true;
    if (raw.open_work_graded !== undefined) row.open_work_graded = raw.open_work_graded === true;
    if (raw.stuck !== undefined) row.stuck = parse_stuck(raw.stuck);
    delete row.exited;
    delete row.exit_code;
    if (raw.exited === true) {
        row.exited = true;
        const code = raw.exit_code;
        row.exit_code = typeof code === 'number' && Number.isInteger(code) && code >= 0 && code <= 255 ? code : null;
    }
    return row;
}

/** The snapshot's `sessions` list: malformed rows dropped (`parse_session_row`),
 *  a non-list read as empty. */
export function parse_session_rows(value: unknown): SessionStatus[] {
    if (!Array.isArray(value)) return [];
    const rows: SessionStatus[] = [];
    for (const entry of value) {
        const row = parse_session_row(entry);
        if (row) rows.push(row);
    }
    return rows;
}

/** Does the row's tmux session currently exist? Absent `live` (older bridge /
 *  legacy path) counts as alive — never hide sessions on missing data. */
export function is_live(status: SessionStatus): boolean {
    return status.live !== false;
}

// ── account usage limits (the /usage screen data) ───────────────────────────

/** One rate-limit window from the bridge's `usage_limits` (5-hour or weekly). */
export interface UsageWindow {
    /** 0–100. */
    used_percentage: number;
    /** Epoch seconds when the window resets; null when the bridge had none. */
    resets_at: number | null;
}

/** Additive top-level `usage_limits` key of GET /snapshot — the account's
 *  Claude usage windows, forwarded from Claude Code's statusline (see
 *  tldrack statusline.py). Absent until the first statusline report. */
export interface UsageLimits {
    five_hour?: UsageWindow;
    seven_day?: UsageWindow;
    /** Seconds since the bridge last received a report — grey out stale bars. */
    age_seconds: number;
}

/** Malformed/absent → null; windows are individually optional but at least one
 *  must be present (an empty report is treated as absent). */
export function parse_usage_limits(value: unknown): UsageLimits | null {
    if (typeof value !== 'object' || value === null) return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.age_seconds !== 'number') return null;
    const result: UsageLimits = { age_seconds: raw.age_seconds };
    for (const key of ['five_hour', 'seven_day'] as const) {
        const window = raw[key];
        if (typeof window !== 'object' || window === null) continue;
        const used = (window as Record<string, unknown>).used_percentage;
        if (typeof used !== 'number') continue;
        const resets = (window as Record<string, unknown>).resets_at;
        result[key] = { used_percentage: used, resets_at: typeof resets === 'number' ? resets : null };
    }
    return result.five_hour || result.seven_day ? result : null;
}

// ── launch profiles (the additive `profiles` key of GET /snapshot) ──────────

/** Which CLI a profile launches. `antigravity` is Google's `agy` (the GEMINI chip). */
export type ProfileKind = 'claude' | 'codex' | 'antigravity' | 'shell';

/**
 * One launch profile, as the bridge reports it.
 *
 * The bridge OWNS profiles (`~/.config/tldrack/profiles.json`, or its
 * detected defaults) because they are machine-local paths and binaries; the
 * rack only picks one and draws it. The key's PRESENCE is the capability
 * signal for `POST /spawn` — an older bridge sends no `profiles` and the rack
 * keeps the legacy `+ NEW`, the same structural gating `worktrees` uses.
 */
export interface ProfileInfo {
    id: string;
    /** ≤10 chars, drawn on the LAUNCH chip and the row chip. */
    label: string;
    kind: ProfileKind;
    /** The profile `+ NEW` falls back to before the user has picked one. */
    default: boolean;
    /** Are the bridge's status hooks merged into this profile's config? Null
     *  where hooks do not apply (`shell`). False → amber `!` on the chip. */
    hooks_ok: boolean | null;
    /** Did the bridge find this profile's CLI? False → the chip is disabled. */
    cli_found: boolean;
    /** Display model for a kind whose model the bridge cannot read. */
    model_label: string | null;
    /** This profile's OWN account usage — two Claude accounts no longer share
     *  one last-writer-wins slot. Null for antigravity/shell, or before a report. */
    usage: UsageLimits | null;
}

const PROFILE_ID_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;
const PROFILE_KINDS: readonly ProfileKind[] = ['claude', 'codex', 'antigravity', 'shell'];
/** Longer labels are clipped rather than dropped — a label is display text. */
export const PROFILE_LABEL_MAX = 10;

/** A profile id off the wire, or null. Mirrors the bridge's `PROFILE_ID_RE`. */
export function parse_profile_id(value: unknown): string | null {
    return typeof value === 'string' && PROFILE_ID_RE.test(value) ? value : null;
}

/**
 * The bridge's `profiles` list, or null when the key is absent / not a list
 * (an older bridge — see `ProfileInfo`).
 *
 * TOTAL: it crosses HTTP, a postMessage and a BroadcastChannel, so nothing
 * about its shape survives the type system. A malformed entry is DROPPED (never
 * coerced into a profile that would launch something unintended), a duplicate
 * id keeps its first occurrence, and at most one entry stays `default`.
 */
export function parse_profiles(value: unknown): ProfileInfo[] | null {
    if (!Array.isArray(value)) return null;
    const out: ProfileInfo[] = [];
    const seen = new Set<string>();
    let has_default = false;
    for (const entry of value) {
        if (typeof entry !== 'object' || entry === null) continue;
        const raw = entry as Record<string, unknown>;
        const id = parse_profile_id(raw.id);
        if (id === null || seen.has(id)) continue;
        const kind = PROFILE_KINDS.find((k) => k === raw.kind);
        if (!kind) continue;
        const label =
            typeof raw.label === 'string' && raw.label.trim() !== ''
                ? raw.label.trim().slice(0, PROFILE_LABEL_MAX)
                : id.slice(0, PROFILE_LABEL_MAX).toUpperCase();
        const is_default: boolean = raw.default === true && !has_default;
        has_default = has_default || is_default;
        seen.add(id);
        out.push({
            id,
            label,
            kind,
            default: is_default,
            hooks_ok: typeof raw.hooks_ok === 'boolean' ? raw.hooks_ok : null,
            // Absent counts as found: never disable a launch on missing data.
            cli_found: raw.cli_found !== false,
            model_label:
                typeof raw.model_label === 'string' && raw.model_label.trim() !== ''
                    ? raw.model_label.trim().slice(0, 40)
                    : null,
            usage: parse_usage_limits(raw.usage),
        });
    }
    return out;
}

/** The `profile` a client sends for "the freest account". Never a profile of
 *  its own: the bridge resolves it at click time (`tldrack/launch_pick.py`). */
export const AUTO_PROFILE_ID = 'auto';

/** The bridge's `launch_pick` — where AUTO would launch right now. Display
 *  only: `POST /spawn` re-picks when the click lands. Its PRESENCE is the
 *  capability for `profile: "auto"`, the same structural gate as `profiles`. */
export interface LaunchPick {
    /** The profile AUTO resolves to, or null when nothing is launchable. */
    id: string | null;
    label: string | null;
    /** One short mono line — the AUTO chip's title. */
    reason: string;
    /** Every candidate is spent; `id` is the one that resets first. */
    spent: boolean;
}

/** `launch_pick` off the wire, or null when absent / malformed (an older bridge). */
export function parse_launch_pick(value: unknown): LaunchPick | null {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    const raw = value as Record<string, unknown>;
    const id = raw.id === null ? null : parse_profile_id(raw.id);
    if (id === null && raw.id !== null) return null;
    return {
        id,
        label: typeof raw.label === 'string' && raw.label.trim() !== '' ? raw.label.trim().slice(0, PROFILE_LABEL_MAX) : null,
        reason: typeof raw.reason === 'string' ? raw.reason.slice(0, 120) : '',
        spent: raw.spent === true,
    };
}

/** The profile `+ NEW` should launch: `wanted` when it is offered and its CLI
 *  exists; otherwise AUTO when the bridge can pick one (also the answer for an
 *  explicit `wanted === 'auto'`), else the bridge's default, else the first
 *  launchable one. A stored manual pick therefore still wins over AUTO. */
export function resolve_launch_profile(
    profiles: readonly ProfileInfo[],
    wanted: string | null,
    launch_pick: LaunchPick | null = null,
): string | null {
    const launchable = profiles.filter((p) => p.cli_found);
    if (wanted && wanted !== AUTO_PROFILE_ID && launchable.some((p) => p.id === wanted)) return wanted;
    if (launch_pick?.id) return AUTO_PROFILE_ID;
    return (launchable.find((p) => p.default) ?? launchable[0])?.id ?? null;
}

// ── rack rollup + activity ticker ──────────────────────────────────────────

/** Sessions-by-display-state rollup, for a surface that summarises the rack. */
export type DisplayCounts = Record<TerminalDisplayState, number>;

export function count_by_display(rows: { display: TerminalDisplayState }[]): DisplayCounts {
    const counts: DisplayCounts = { busy: 0, background: 0, attention: 0, done: 0, free: 0, unknown: 0 };
    // Own-key check, not a bare index: a word outside the vocabulary would add
    // `undefined + 1` and paint NaN on the chip.
    for (const row of rows) counts[Object.hasOwn(counts, row.display) ? row.display : 'unknown'] += 1;
    return counts;
}

/** The rollup's order and words — attention first (most urgent), then work
 *  still running, then what is finished or waiting. */
const SUMMARY_TERMS: { state: TerminalDisplayState; word: string }[] = [
    { state: 'attention', word: 'needs you' },
    { state: 'busy', word: 'busy' },
    { state: 'background', word: 'background' },
    { state: 'done', word: 'awaiting review' },
    { state: 'free', word: 'free' },
];

/** The rollup as segments, for a surface that dots each one.
 *  Zero counts are dropped; unknown shows only when it's all there is. */
export function fleet_summary_segments(counts: DisplayCounts): { state: TerminalDisplayState; text: string }[] {
    const segments = SUMMARY_TERMS.filter(({ state }) => counts[state] > 0).map(({ state, word }) => ({
        state,
        text: `${counts[state]} ${word}`,
    }));
    if (segments.length === 0 && counts.unknown > 0) segments.push({ state: 'unknown', text: `${counts.unknown} unknown` });
    return segments;
}

/** '1 needs you · 2 busy · 3 free'. Empty string for an empty rack. */
export function format_fleet_summary(counts: DisplayCounts): string {
    return fleet_summary_segments(counts)
        .map((segment) => segment.text)
        .join(' · ');
}

// ── coalesced rack notifications ──────────────────────────────────────────
//
// One summary of everything currently outstanding, NOT one alert per session.
// With a real rack, three agents blocking at once produced three racing
// banners, which is how a signal gets trained into noise. Modeled on
// AltanS/collie's NotificationCoordinator: a single tag so renders replace
// rather than stack, the named agent when exactly one wants you, a count when
// several do, and retraction once the last one is resolved.

/** A rendered notification, or null when there is nothing outstanding (which is
 *  the caller's cue to RETRACT whatever is on screen). */
export interface FleetNotice {
    title: string;
    body: string;
    /** In-page snackbar text. Separate from title+body because the snackbar is already
     *  inside the page: concatenating the OS-notification title would read
     *  "tldrack — agent needs input — api — allow Bash?". */
    toast: string;
    /** Stable across renders so the platform replaces instead of stacking. */
    tag: string;
    /** Identity of the current outstanding set — unchanged means don't re-render
     *  (re-firing an identical notification re-alerts for no new information). */
    key: string;
    /** The outstanding sessions themselves. The renderer needs the membership,
     *  not just its hash, to tell a set that GAINED an agent (alert) from one
     *  that merely shrank (update quietly — nothing new is wrong). */
    members: string[];
}

export const ATTENTION_NOTICE_TAG = 'tldrack-attention';

/** Sorted, so a set that merely reordered between polls is the SAME key. */
function notice_key(sessions: string[]): string {
    return [...sessions].sort().join(' ');
}

/**
 * The "needs you" summary for every session currently blocked.
 *
 * Takes the full outstanding set rather than an edge, because retraction and
 * de-duplication both need current state: an agent that blocks and then
 * unblocks on its own simply never appears in a set, so it never reaches you.
 */
export function summarize_fleet_attention(
    entries: { session: string; message: string | null }[],
): FleetNotice | null {
    if (entries.length === 0) return null;
    const members = entries.map((entry) => entry.session);
    const key = notice_key(members);
    if (entries.length === 1) {
        const only = entries[0];
        return {
            title: 'tldrack — agent needs input',
            // The message is the whole reason you're being summoned, so it stays
            // in the body verbatim when there is exactly one agent to name.
            body: only.message ? `${only.session} — ${only.message}` : `${only.session} needs input`,
            toast: only.message
                ? `terminal '${only.session}' needs input — ${only.message}`
                : `terminal '${only.session}' needs input`,
            tag: ATTENTION_NOTICE_TAG,
            key,
            members,
        };
    }
    // Names, not messages: N messages would not fit and would not be read.
    const names = members.join(', ');
    return {
        title: `${entries.length} agents need you`,
        body: names,
        toast: `${entries.length} agents need you — ${names}`,
        tag: ATTENTION_NOTICE_TAG,
        key,
        members,
    };
}

/** One entry of the bridge's transition ring buffer (GET /activity + the
 *  `activity` SSE event) — a state change or a PreToolUse activity change. */
export interface TransitionEntry {
    /** Epoch seconds. */
    ts: number;
    session: string;
    from_state: string;
    to_state: TerminalHookState;
    event: string;
    activity: SessionActivity | null;
}

/** Malformed → null (SSE frames and /activity rows are untrusted JSON). */
export function parse_transition_entry(value: unknown): TransitionEntry | null {
    if (typeof value !== 'object' || value === null) return null;
    const raw = value as Record<string, unknown>;
    if (typeof raw.ts !== 'number' || typeof raw.session !== 'string') return null;
    if (typeof raw.from_state !== 'string' || typeof raw.to_state !== 'string') return null;
    const activity = raw.activity;
    return {
        ts: raw.ts,
        session: raw.session,
        from_state: raw.from_state,
        to_state: as_hook_state(raw.to_state),
        event: typeof raw.event === 'string' ? raw.event : '',
        activity:
            typeof activity === 'object' && activity !== null && typeof (activity as SessionActivity).zone === 'string'
                ? (activity as SessionActivity)
                : null,
    };
}

/** Ticker row text: 'HH:MM  session  busy→free · bash: pytest' (local time). */
export function format_transition_text(entry: TransitionEntry): string {
    const date = new Date(entry.ts * 1000);
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    const change = entry.from_state === entry.to_state ? entry.to_state : `${entry.from_state}→${entry.to_state}`;
    const detail = entry.activity?.detail ? ` · ${entry.activity.detail}` : '';
    return `${hh}:${mm}  ${entry.session}  ${change}${detail}`;
}

/** ttyd URL → status-listener port (ttyd port + 1 by convention). Null when unparseable. */
export function status_port_for_url(url: string): number | null {
    try {
        const parsed = new URL(url);
        const port = parsed.port
            ? Number.parseInt(parsed.port, 10)
            : parsed.protocol === 'https:'
              ? 443
              : 80;
        return Number.isFinite(port) ? port + 1 : null;
    } catch {
        return null;
    }
}

/**
 * Claude Code broadcasts state through its terminal title (readable via tmux
 * `#{pane_title}`): a braille spinner glyph (U+2800–U+28FF) prefix while
 * working, '✳ ' when idle. Mirrors herdr's claude.toml osc_title rules.
 */
export function title_hint_state(title: string | null): 'busy' | 'free' | null {
    if (!title) return null;
    const first = title.codePointAt(0);
    if (first !== undefined && first >= 0x2800 && first <= 0x28ff) return 'busy';
    if (title.startsWith('✳ ')) return 'free';
    return null;
}

/**
 * The state the UI should trust for one poll: downgrade stale 'busy' to
 * unknown, then let the tmux title hint fill in unknowns. Flags are passed in
 * (not imported) to keep this module pure.
 */
export function derive_effective_state(
    status: SessionStatus,
    opts: { busy_stale_after_s: number; title_hint_enabled: boolean },
): TerminalHookState {
    let state = status.state;
    if (state === 'busy' && status.age_seconds !== null && status.age_seconds > opts.busy_stale_after_s) {
        state = 'unknown';
    }
    if (state === 'unknown' && opts.title_hint_enabled) {
        state = title_hint_state(status.title) ?? state;
    }
    return state;
}

/** Attention outranks busy outranks background outranks free outranks unknown
 *  (herdr's rollup ladder, minus done, plus the bridge's background). */
const STATE_PRIORITY: Record<TerminalHookState, number> = {
    attention: 4,
    busy: 3,
    background: 2,
    free: 1,
    unknown: 0,
};

/** States in which the agent still has work in flight — leaving one for
 *  `free` is what "done" means. */
const ACTIVE_STATES: ReadonlySet<TerminalHookState> = new Set(['busy', 'background', 'attention']);

/**
 * Per-session transition tracker. Upgrades (→busy, →attention, →background)
 * commit instantly so short attention windows are never missed; downgrades
 * (busy→free, busy→background etc.) must survive `debounce_polls` consecutive
 * polls — the herdr working→idle flicker guard. The attention notification also
 * fires only once the state has survived `debounce_polls`.
 *
 * `done` fires on the step INTO `free` only. busy→background is a quiet
 * downgrade — the turn ended but the work did not — so a background session
 * notifies once, when its last job ends (background→free), never twice.
 */
export interface SessionTracker {
    committed: TerminalHookState;
    /** Consecutive polls observed at the committed state. */
    committed_polls: number;
    downgrade_candidate: TerminalHookState | null;
    downgrade_polls: number;
    /** busy/background/attention → free happened while the operator wasn't watching. */
    done_pending: boolean;
}

export function new_tracker(): SessionTracker {
    return {
        committed: 'unknown',
        committed_polls: 0,
        downgrade_candidate: null,
        downgrade_polls: 0,
        done_pending: false,
    };
}

export interface TrackerAdvance {
    tracker: SessionTracker;
    /** Notify "agent needs input" (already debounced). */
    fire_attention: boolean;
    /** Notify "agent finished while you weren't looking". */
    fire_done: boolean;
}

export function advance_tracker(
    tracker: SessionTracker,
    state: TerminalHookState,
    opts: { debounce_polls: number; watching: boolean },
): TrackerAdvance {
    const next: SessionTracker = { ...tracker };
    let fire_attention = false;
    let fire_done = false;

    if (state === next.committed) {
        next.committed_polls += 1;
        next.downgrade_candidate = null;
        next.downgrade_polls = 0;
        if (next.committed === 'attention' && next.committed_polls === opts.debounce_polls) {
            fire_attention = true;
        }
    } else if (STATE_PRIORITY[state] > STATE_PRIORITY[next.committed]) {
        next.committed = state;
        next.committed_polls = 1;
        next.downgrade_candidate = null;
        next.downgrade_polls = 0;
        if (ACTIVE_STATES.has(state)) next.done_pending = false;
        if (state === 'attention' && opts.debounce_polls <= 1) fire_attention = true;
    } else {
        if (state === next.downgrade_candidate) {
            next.downgrade_polls += 1;
        } else {
            next.downgrade_candidate = state;
            next.downgrade_polls = 1;
        }
        if (next.downgrade_polls >= opts.debounce_polls) {
            const was_active = ACTIVE_STATES.has(next.committed);
            next.committed = state;
            next.committed_polls = 1;
            next.downgrade_candidate = null;
            next.downgrade_polls = 0;
            if (was_active && state === 'free') {
                if (opts.watching) {
                    next.done_pending = false;
                } else {
                    next.done_pending = true;
                    fire_done = true;
                }
            }
        }
    }
    return { tracker: next, fire_attention, fire_done };
}

/** The bridge's self-identity (`bridge` key of GET /snapshot): pid, uptime,
 *  and who launched it, so a "windowless" bridge (a launcher app, a background
 *  agent) is traceable from the page. Absent on pre-2026-07-12 bridges. The
 *  bridge also serves a `parent_chain`, omitted here: no page renders it, and
 *  extra keys are harmless. */
export interface BridgeIdentity {
    pid: number;
    started_at: number;
    uptime_seconds: number;
    cwd: string;
    tty: string | null;
    /** 'status-only' = the bridge is a sidecar beside a ttyd it doesn't own. */
    mode: 'full' | 'status-only';
    port: number;
    status_port: number;
    launched_by: { kind: string; label: string };
    ttyd: { pid: number | null; owned: boolean };
}

/** One ingested row: the bridge's raw `SessionStatus` plus the tracker's verdict.
 *
 *  Lives HERE, in the pure module, and not beside an ingestor: the HTTP
 *  transport and a host's own payload owner both produce it, and neither may
 *  import the other, so a type on the boundary between an ingestor and a rack
 *  surface has to be reachable without either. */
export interface PortSnapshotRow extends SessionStatus {
    /** Committed display state after debounce/seen logic ('done' included). */
    display: TerminalDisplayState;
}

/** Everything one bridge port reported on a tick — the unit every rack surface
 *  renders from, whichever ingestor produced it. */
export interface PortSnapshot {
    reachable: boolean;
    sessions: PortSnapshotRow[];
    /** Bridge self-identity (pid / uptime / launched-by). Null when the bridge
     *  is unreachable, pre-identity (older bridge), or on the legacy path. */
    bridge: BridgeIdentity | null;
    /** Account usage windows (statusline-fed). Null when the bridge is
     *  unreachable, pre-usage, or nothing has reported yet. */
    usage_limits: UsageLimits | null;
    /** The bridge's launch profiles. ABSENT on a bridge without `POST /spawn`
     *  — that absence is the capability signal, so never default it to `[]`. */
    profiles?: ProfileInfo[];
    /** Where AUTO would launch. ABSENT on a bridge that cannot resolve
     *  `profile: "auto"` — the capability signal, like `profiles`. */
    launch_pick?: LaunchPick;
    /** Recent closes the rack's ↺ CLOSED list can reopen. ABSENT at 0 and on a
     *  bridge without `POST /reopen` — `agent-rack/closed-client.ts`. */
    closed?: number;
    /** Sessions from before a reboot the same list can restore. ABSENT at 0 and on a bridge
     *  without session records — `agent-rack/closed-client.ts`. */
    restorable?: number;
    /** The suggester's mode, ABSENT while it is off or on a bridge without one — the rack's cue to
     *  report its control presses (`RackPayload.next_step_mode`). */
    next_step_mode?: NextStepMode;
}

/** Minute-granularity uptime ('up 2h 14m'), coarse on purpose: a surface that
 *  repaints only when the string changes then repaints once a minute. */
export function format_uptime_short(seconds: number): string {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return 'up <1m';
    const m = Math.floor(s / 60);
    if (m < 60) return `up ${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `up ${h}h ${m % 60}m`;
    return `up ${Math.floor(h / 24)}d ${h % 24}h`;
}

/** launched_by.kind → short word for the indicator sublabel. */
export function launched_by_short(kind: string): string {
    switch (kind) {
        case 'menubar':
            return 'menu bar';
        case 'iterm2':
            return 'iTerm2';
        case 'apple-terminal':
            return 'Terminal';
        case 'tmux':
            return 'tmux';
        case 'agent':
            return 'agent';
        default:
            return 'headless';
    }
}

/** BRIDGE indicator sublabel when online: 'pid 41320 · up 2h 14m · iTerm2'. */
export function format_bridge_sublabel(bridge: BridgeIdentity): string {
    return `pid ${bridge.pid} · ${format_uptime_short(bridge.uptime_seconds)} · ${launched_by_short(bridge.launched_by.kind)}`;
}

/** Outcome of POST /shutdown — each case gets its own user-facing next step. */
export type ShutdownResult = 'ok' | 'unsupported' | 'forbidden' | 'error' | 'unreachable';

/** HTTP status → ShutdownResult: 404 = pre-shutdown bridge (route doesn't
 *  exist), 403 = the bridge's Origin guard rejected this page. */
export function classify_shutdown_status(status: number): ShutdownResult {
    if (status >= 200 && status < 300) return 'ok';
    if (status === 404) return 'unsupported';
    if (status === 403) return 'forbidden';
    return 'error';
}

/**
 * Outcome of POST /rename-session.
 *
 * A superset of ShutdownResult rather than a reuse of it, because rename has two
 * failures a user hits by accident and can act on: the name is already taken,
 * and the session died while they were typing. Collapsing either into `error`
 * would leave the rack saying "rename failed" when it knows exactly why.
 */
export type RenameResult = ShutdownResult | 'taken' | 'gone';

/**
 * HTTP status → RenameResult.
 *
 * 409 = a live session already owns that name; 410 = the session being renamed
 * is gone. The bridge deliberately does NOT answer 404 for a missing session —
 * 404 keeps its one meaning across every route here, "this bridge predates the
 * route", which is `unsupported` and tells the user to restart it.
 */
export function classify_rename_status(status: number): RenameResult {
    if (status === 409) return 'taken';
    if (status === 410) return 'gone';
    return classify_shutdown_status(status);
}

/**
 * Why a rename didn't happen, in words, with the next step where there is one.
 *
 * Pure so it is unit-testable and so both rack surfaces can never drift into
 * describing the same failure differently. Every branch names the SESSION —
 * a fleet-sized rack is exactly where "rename failed" with no subject is
 * useless.
 */
export function rename_failure_message(session: string, name: string, result: RenameResult): string {
    switch (result) {
        case 'taken':
            return `A session named '${name}' already exists — pick another name.`;
        case 'gone':
            return `'${session}' is gone — it ended before the rename landed.`;
        case 'unsupported':
            // 404 means this bridge has no /rename-session at all.
            return `'${session}' was not renamed — this tldrack is too old for the route. Restart it.`;
        case 'forbidden':
            return `The bridge refused this page's origin, so '${session}' was not renamed.`;
        case 'unreachable':
            return `Couldn't reach the tldrack — '${session}' was not renamed.`;
        default:
            return `Couldn't rename '${session}'.`;
    }
}

/** The operator looked at or interacted with the terminal: clear the done highlight. */
export function mark_seen(tracker: SessionTracker): SessionTracker {
    if (!tracker.done_pending) return tracker;
    return { ...tracker, done_pending: false };
}

/** What a surface should render right now. */
export function display_state(tracker: SessionTracker): TerminalDisplayState {
    if (tracker.done_pending && tracker.committed === 'free') return 'done';
    return tracker.committed;
}

// ── model + cost/token labels ───────────────────────────────────────────────
// Pure display helpers for the model label a session carries. Kept in this
// dependency-free module so they run under
// `node --test` alongside the rest of the status logic.

/**
 * Shown when the bridge has no model for this session. NOT a connection state.
 * It read "claude disconnected" until 2026-09-07 and was taken at its word — a
 * session that had merely not replied yet looked like an outage.
 *
 * Three things now produce it, and none is a disconnection: a tmux pane that is
 * a plain SHELL with no Claude in it (the steady-state case — every pane gets a
 * rack row); the sub-second window before the statusline names the model
 * (tldrack/statusline_models.py); hooks or statusline not merged into
 * ~/.claude/settings.json at all. "unknown" is this file's existing word for
 * "alive, but the bridge has nothing on it" — see TerminalHookState — so this
 * reuses the vocabulary rather than minting a second one.
 *
 * Bare "claude" was rejected for the first case: it would claim a Claude is
 * sitting in a pane that only holds a shell.
 *
 * The real disconnections are owned elsewhere and already carry their own
 * marks: an unreachable bridge is the rack's STALE tag / warn empty-state, and
 * a session tmux no longer has is DEAD_SESSION_TEXT (multi-terminal-layout.ts).
 */
export const UNKNOWN_MODEL_LABEL = 'model unknown';

/** Model families, longest-word-first so `find(includes)` can't mis-match. */
const MODEL_FAMILIES = ['opus', 'sonnet', 'haiku', 'fable', 'mythos'] as const;

/**
 * Friendly model name from a Claude model id: `claude-opus-4-8` → "Opus 4.8",
 * `claude-sonnet-5` → "Sonnet 5", `claude-haiku-4-5` → "Haiku 4.5". Strips any
 * `[1m]` / `-fast` / date-snapshot suffix. Returns null for empty/unknown input
 * so callers can fall back to UNKNOWN_MODEL_LABEL; an unrecognized-but-present id is
 * returned verbatim rather than dropped.
 */
export function format_model_name(model: string | null | undefined): string | null {
    if (!model) return null;
    // Family word + the run of `-<1–2 digits>` groups that immediately follow it,
    // each bounded by `-`, `[`, or end. That bound keeps an 8-digit yyyymmdd
    // snapshot (`-20251001`) and a `[1m]` context suffix out of the version:
    // `claude-opus-4-8` → "Opus 4.8", `claude-opus-4-8[1m]` → "Opus 4.8".
    const match = model.toLowerCase().match(/(opus|sonnet|haiku|fable|mythos)((?:-\d{1,2}(?=-|\[|$))*)/);
    if (!match) return model; // unknown shape — better to show the raw id than nothing
    const title = match[1][0].toUpperCase() + match[1].slice(1);
    const version = (match[2].match(/\d+/g) ?? []).join('.');
    return version ? `${title} ${version}` : title;
}

/** Per-1M-token base rates (USD) by family; cache write/read are multiples of input. */
const MODEL_PRICING: Record<(typeof MODEL_FAMILIES)[number], { input: number; output: number }> = {
    opus: { input: 5, output: 25 },
    sonnet: { input: 3, output: 15 },
    haiku: { input: 1, output: 5 },
    fable: { input: 10, output: 50 },
    mythos: { input: 10, output: 50 },
};
// Cache economics: reads ≈ 0.1× input, writes ≈ 1.25×
// (5-minute TTL). Claude Code uses the 1-hour TTL (≈ 2×), but the wire only
// carries a single cumulative cache-creation total, so this stays an estimate.
const CACHE_READ_MULT = 0.1;
const CACHE_WRITE_MULT = 1.25;

/**
 * Approximate USD cost of a session from its cumulative token usage. Best-effort:
 * pricing drifts and the cache-write TTL is assumed — always present the result
 * as an estimate. Returns null when the model is unpriced or usage is missing.
 */
export function estimate_session_cost(
    model: string | null | undefined,
    usage: SessionUsage | null | undefined,
): number | null {
    if (!model || !usage) return null;
    const id = model.toLowerCase();
    const family = MODEL_FAMILIES.find((f) => id.includes(f));
    if (!family) return null;
    const rate = MODEL_PRICING[family];
    const dollars =
        usage.input_tokens * rate.input +
        usage.output_tokens * rate.output +
        usage.cache_creation_input_tokens * rate.input * CACHE_WRITE_MULT +
        usage.cache_read_input_tokens * rate.input * CACHE_READ_MULT;
    return dollars / 1_000_000;
}

/** Compact USD: `$1.23`, `$0.05`, `<$0.01` for sub-cent, `$12` for whole dollars. */
export function format_usd(cost: number): string {
    if (cost > 0 && cost < 0.01) return '<$0.01';
    if (cost >= 100) return `$${Math.round(cost)}`;
    return `$${cost.toFixed(2)}`;
}

/** Compact token count: `950`, `22k`, `1.7M`. */
export function format_token_count(tokens: number): string {
    if (tokens < 1000) return String(tokens);
    if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k`;
    return `${(tokens / 1_000_000).toFixed(1)}M`;
}

/**
 * Assemble the label: `Opus 4.8 · ~$1.23 · 22k out`, or
 * UNKNOWN_MODEL_LABEL when the bridge has not learned the model yet. `show_cost`/`show_tokens` are passed in (not read from
 * config) so this module stays dependency-free and testable.
 */
export function format_model_label(
    model: string | null | undefined,
    usage: SessionUsage | null | undefined,
    opts: { show_cost: boolean; show_tokens: boolean } = { show_cost: true, show_tokens: true },
): string {
    const name = format_model_name(model);
    if (!name) return UNKNOWN_MODEL_LABEL;
    const parts = [name];
    if (opts.show_cost) {
        const cost = estimate_session_cost(model, usage);
        if (cost !== null) parts.push(`~${format_usd(cost)}`);
    }
    if (opts.show_tokens && usage) parts.push(`${format_token_count(usage.output_tokens)} out`);
    return parts.join(' · ');
}
