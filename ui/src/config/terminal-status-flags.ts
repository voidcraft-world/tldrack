/**
 * Tunable parameters for the agent-status marks and alerts.
 * Every rack surface reads the local tldrack status listener (ttyd
 * port + 1, default :7682) and marks each session by what its agent is doing:
 * busy (working), background (turn over, background jobs still running —
 * breathes slowly), attention (needs input — pulses), done (finished while
 * you weren't looking; persists until you look), free (ready for work),
 * unknown (no mark).
 *
 * A plain object: a page or a host changes a field, and nothing is stored.
 *
 * Flags are read at poll time, so a change applies on the next poll (~poll_interval_ms).
 * Note: background tabs throttle setInterval (up to ~1/min), so state changes
 * while the tab is hidden can surface late; that is acceptable for alerts.
 */
// Type-only, so this config module stays runtime-import-free.
import type { StopFailure, StuckReading, TerminalDisplayState } from '../engine/terminal/terminal-status-logic.ts';

/** The bridge's default port (ttyd's port + 1). One owner: nothing else spells the literal. */
export const DEFAULT_STATUS_PORT = 7682;

export const TERMINAL_STATUS_FLAGS = {
    /** How often each bridge status port is polled (ms). */
    poll_interval_ms: 3000,
    /** Consecutive polls a state must survive before it commits on a DOWNGRADE
     *  (busy→free etc.) or fires a notification: herdr's flicker guard.
     *  Upgrades (→busy, →attention) always apply instantly. */
    debounce_polls: 2,
    /** A hook-reported 'busy' older than this is treated as unknown (laptop lid
     *  closed, claude killed — staleness is the client's job per the bridge docs). */
    busy_stale_after_s: 600,
    /** Infer busy/free from the tmux pane title when the hook state is unknown:
     *  Claude Code prefixes its terminal title with a braille spinner while
     *  working and '✳ ' when idle. */
    title_hint_enabled: true,
    /** Glow color while the agent is working. */
    busy_color: 0x58a6ff,
    /** Glow color while the turn is over but background jobs still run (closing
     *  the session kills them). Indigo, not a second blue: it has to stay
     *  distinct from `busy_color` on the static surfaces that cannot breathe. */
    background_color: 0x5c7cfa,
    /** Glow color when the agent needs input (permission prompt / question) — red, pulses. */
    attention_color: 0xf85149,
    /** Glow color after the agent finished while unseen; persists until you look. */
    done_color: 0x2dd4bf,
    /** Glow color while the session is free — no agent working, ready for you. */
    free_color: 0x2ea043,
    /** No data / placeholder: grey. */
    unknown_color: 0x6e7681,
    /** The DOM rack marks each row's state with `display_state_emoji` instead of
     *  the colour dot: 🙋 reads as "it asked me something" with nothing to learn,
     *  a hue has to be memorised. `false` restores the dot. Read once per
     *  document, so set it before the rack mounts. */
    state_emoji: true,
    /** ⚠️ on a reply-ready / idle row whose closing message Laya judged to leave planned
     *  work open (`open_work_badge_shown`). `false` hides it; the bridge keeps
     *  judging either way. */
    open_work_badge: true,
    /** ✅ / ☑️ in place of a reply-ready / idle row's 💬 / ○ when Laya judged the
     *  closing message DONE (`work_done_shown`). `false` keeps the plain marks. */
    work_done_mark: true,
    /** Period of the attention pulse animation (ms). */
    attention_pulse_period_ms: 1600,
    /** Period of the background breathe (ms) — twice the alarm's, because it
     *  says "still running, don't close me", not "come here". */
    background_pulse_period_ms: 3200,
    /**
     * Quantize the attention pulse into this many steps per half-cycle.
     * **0 restores the old continuous `ease-in-out` glide.**
     *
     * Stepping it is not a compromise: motion on these pages is quantized and
     * state-driven (`styles/fui-panel.css` § The motion vocabulary). At the default
     * the pulse repaints ~5×/second instead of ~60 and reads as an instrument, not
     * a breath.
     */
    attention_pulse_steps: 4,
    /** Listen to the bridge's SSE stream (GET /events) so state flips land
     *  instantly instead of on the next ~3s poll. Pure accelerator: the poll
     *  keeps running regardless — downgrade debounce counts poll observations,
     *  and polling is the liveness/fallback path for older bridges. Flipping
     *  this off mid-session stops SSE frames from being processed (the stream
     *  object stays open until the port loses its last subscriber). */
    sse_enabled: true,
    /** When /snapshot 404s (older bridge), re-probe it every N ticks while using
     *  the per-session /status fallback. */
    snapshot_reprobe_ticks: 20,
    /** Hide sessions whose tmux session no longer exists (`live: false` snapshot
     *  rows — the bridge keeps killed sessions' last state forever). Every
     *  surface drops them within one poll. A seeder that fabricates sessions
     *  turns this off while it runs: its sessions have no tmux backing. */
    hide_dead_sessions: true,
    /** DEV ONLY: session name → working directory, overriding the snapshot's
     *  `cwd` for those sessions. Exists because cohort grouping runs on `cwd`,
     *  which comes from tmux, and a fabricated session has no tmux backing.
     *  Without this the grouped rack can only be worked on with real shells
     *  open in several repos. Empty in every real run; a seeder sets and
     *  restores it like `hide_dead_sessions`. */
    fake_cwds: {} as Record<string, string>,
    /** DEV ONLY: session name → background job count (null = uncounted). A
     *  listed session is ingested as `background` whatever the bridge says.
     *  Exists because the bridge DERIVES that state and refuses it on
     *  `POST /status`, so a seeder has no other way to show it.
     *  Empty in every real run; a seeder sets and restores it like `fake_cwds`. */
    fake_background: {} as Record<string, number | null>,
};

/** The one status→color mapping every surface shares. Reads TERMINAL_STATUS_FLAGS
 *  at call time, so a changed color applies on the next poll. Exhaustive: a new
 *  state is a compile error here, never a silent grey. */
export function display_state_color(state: TerminalDisplayState): number {
    const flags = TERMINAL_STATUS_FLAGS;
    switch (state) {
        case 'busy':
            return flags.busy_color;
        case 'background':
            return flags.background_color;
        case 'attention':
            return flags.attention_color;
        case 'done':
            return flags.done_color;
        case 'free':
            return flags.free_color;
        case 'unknown':
            return flags.unknown_color;
        default: {
            const unhandled: never = state;
            void unhandled;
            // A word from a newer bridge that slipped past `as_hook_state`: grey, never a crash.
            return flags.unknown_color;
        }
    }
}

/**
 * The one status→emoji mapping, beside `display_state_color` for the same
 * reason: a surface that draws a state glyph reads it here, so two never
 * disagree. Exhaustive like the colours.
 *
 * A data-carrying identifier, not decoration, so the house rule against emoji
 * in chrome does not reach it.
 */
export function display_state_emoji(state: TerminalDisplayState): string {
    switch (state) {
        case 'attention':
            return '🙋'; // asked you something — a question, a permission, a plan
        case 'done':
            return '💬'; // unread reply; a turn ending does not establish task completion
        case 'free':
            return '○'; // idle: the reply was seen (a never-prompted session is 🆕, FRESH_SESSION_EMOJI)
        case 'background':
            return '⏳'; // turn over, jobs still running — closing it kills them
        case 'busy':
            return '🤔'; // working
        case 'unknown':
            return '◌'; // the bridge has nothing on it (a plain shell, no hooks)
        default: {
            const unhandled: never = state;
            void unhandled;
            return '◌';
        }
    }
}

/** A session tmux no longer has — whatever state it last reported. */
export const DEAD_SESSION_EMOJI = '💀';

/** A new session nobody has prompted yet. It reads `free` like one whose reply
 *  you read, and both drew ○ — so a just-opened agent hid among finished ones. */
export const FRESH_SESSION_EMOJI = '🆕';

/** Does 🆕 replace this row's ○? Idle, live rows the bridge marked `fresh`
 *  (`tldrack/main.py` § Fresh sessions owns when). */
export function fresh_session_shown(
    state: TerminalDisplayState | undefined,
    fresh: boolean | undefined,
    dead: boolean,
): boolean {
    return fresh === true && !dead && state === 'free';
}

/** The mark for a session row: 🆕 on a fresh one, else its state's emoji. */
export function session_state_emoji(state: TerminalDisplayState, fresh: boolean | undefined, dead: boolean): string {
    return fresh_session_shown(state, fresh, dead) ? FRESH_SESSION_EMOJI : display_state_emoji(state);
}

/** Rides the state mark when a finished turn still left planned work open. */
export const OPEN_WORK_BADGE = '⚠️';

/** Rides it instead while the work is the agent's own, still running — its jobs, or its children. */
export const OPEN_WORK_WAITING_BADGE = '⏳';

/** The ⚠️ as a row carries it: `seen` = parked ("seen, not now"), `children` = live descendants
 *  still working. Owner of both: `bridge/tldrack/attention.py`. */
export interface OpenWorkMarkInput {
    seen?: boolean;
    children?: number;
}

/**
 * What Laya's open-work verdict draws on a row — the ONE rule every surface asks:
 * - `warning` ⚠️ on a reply-ready / idle row: work is open and nothing else is moving it.
 * - `waiting` ⏳ while the work is the agent's own and still running: a `background` row, or a
 *   parent whose children still work (`children`). The rule since 2026-11-04: a ⚠️ on a
 *   background row becomes ⏳, because most such ⚠️ come from an orchestrator waiting on its
 *   own waves. The ⚠️ comes back once the last child stops.
 * - `seen` — the operator parked the loop: nothing draws, only the title says so.
 * - null — no verdict, the flag is off, or a working, blocked or dead row (the bridge clears the
 *   verdict on the next turn).
 */
export function open_work_mark_kind(
    state: TerminalDisplayState | undefined,
    open_work: boolean | null | undefined,
    dead: boolean,
    mark: OpenWorkMarkInput = {},
): 'warning' | 'waiting' | 'seen' | null {
    if (!TERMINAL_STATUS_FLAGS.open_work_badge || open_work !== true || dead) return null;
    if (state !== 'done' && state !== 'free' && state !== 'background') return null;
    if (mark.seen) return 'seen';
    if (state === 'background' || (mark.children ?? 0) > 0) return 'waiting';
    return 'warning';
}

/** Does the ⚠️ ride this row's mark? `open_work_mark_kind` is the rule. */
export function open_work_badge_shown(
    state: TerminalDisplayState | undefined,
    open_work: boolean | null | undefined,
    dead: boolean,
    mark: OpenWorkMarkInput = {},
): boolean {
    return open_work_mark_kind(state, open_work, dead, mark) === 'warning';
}

/** Is the ⚠️ drawn as ⏳ instead? `open_work_mark_kind` is the rule. */
export function open_work_waiting_shown(
    state: TerminalDisplayState | undefined,
    open_work: boolean | null | undefined,
    dead: boolean,
    mark: OpenWorkMarkInput = {},
): boolean {
    return open_work_mark_kind(state, open_work, dead, mark) === 'waiting';
}

/** The ⏳'s words, appended to its job count when `open_work_waiting_shown`. */
export const OPEN_WORK_WAITING_TITLE = 'work still open · running on its own, not waiting on you';

/** The ⏳'s words on a parent whose children still work. */
export function open_work_children_title(children: number): string {
    return `work still open · waiting on ${children} child session${children === 1 ? '' : 's'}, not on you`;
}

/** A parked ⚠️'s words, on the row's own mark title. */
export const OPEN_WORK_SEEN_TITLE = 'open work seen, not now';

/** A busy row the bridge flagged stuck (`bridge/tldrack/stuck.py`). */
export const STUCK_BADGE = '🧱';

/** Does 🧱 ride this row's mark? Live working rows only — the flag clears itself with its cause. */
export function stuck_shown(state: TerminalDisplayState | undefined, stuck: StuckReading | null | undefined, dead: boolean): boolean {
    return !!stuck && !dead && (state === 'busy' || state === 'unknown');
}

/** The 🧱's words: the title and the accessible name. */
export function stuck_title(stuck: StuckReading): string {
    if (stuck.kind === 'repeat_failure') return `stuck? the same command failed ${stuck.count ?? 'several'}× this turn`;
    const minutes = stuck.seconds === null ? null : Math.round(stuck.seconds / 60);
    return `stuck? no progress for ${minutes ?? '20+'} min`;
}

/**
 * Laya's other answer: the closing message leaves NO planned work open (p under
 * the done cut). ✅ replaces a reply-ready row's 💬, ☑️ an idle row's ○. Between
 * the cuts the plain mark stays — "not sure it is open" is not "done". The cuts:
 * owned by `bridge/tldrack/open_work.py` — see § Three bands there.
 */
export const WORK_DONE_EMOJI = { done: '✅', free: '☑️' } as const;

/** Does ✅/☑️ replace this row's mark? Reply-ready and idle rows only, never dead. */
export function work_done_shown(
    state: TerminalDisplayState | undefined,
    work_done: boolean | null | undefined,
    dead: boolean,
): state is 'done' | 'free' {
    return TERMINAL_STATUS_FLAGS.work_done_mark && work_done === true && !dead
        && (state === 'done' || state === 'free');
}

/** The ✅ mark's words: the title and the accessible name. */
export function work_done_title(state: 'done' | 'free', p: number | null | undefined): string {
    const pct = typeof p === 'number' ? ` (${Math.round(p * 100)}% open)` : '';
    return `${state === 'done' ? 'reply ready' : 'idle'} · work looks done${pct}`;
}

/** The badge's words: the title and the accessible name. The answers it lists are the ones its menu will
 *  draw: once the turn has the operator's grade (`graded`, set by a ✓), that says so and the ✓ is gone. */
export function open_work_badge_title(p: number | null | undefined, graded = false): string {
    const pct = typeof p === 'number' ? ` (${Math.round(p * 100)}%)` : '';
    return graded
        ? `planned work may remain${pct} · graded — ⏸ not now · ✗ not open`
        : `planned work may remain${pct} — ⏸ not now · ✗ not open · ✓ still open`;
}

/**
 * A turn the session limit (or another API error) CUT OFF ends exactly like a
 * finished one — the index goes idle and the row reads reply-ready, then idle.
 * These three marks replace that ordinary state mark, never a working,
 * blocked or dead row's: 🪫 the limit still holds, 🔋 its window has reset
 * (resume it now), 🛑 any other API error. Why the bridge knows: owned by
 * `bridge/tldrack/transcript.py` `fold_stop_failure`.
 */
export const STOP_FAILURE_EMOJI = { limited: '🪫', recharged: '🔋', error: '🛑' } as const;

export type StopFailurePhase = keyof typeof STOP_FAILURE_EMOJI;

/** Does the cut-off mark replace this row's state mark? Turn-over rows only. */
export function stop_failure_shown(
    state: TerminalDisplayState | undefined,
    stop_failure: StopFailure | null | undefined,
    dead: boolean,
): boolean {
    return !!stop_failure && !dead && (state === 'done' || state === 'free' || state === 'background');
}

/** Which of the three marks, at `now_s` (epoch seconds). A limit with no known
 *  reset holds until the next turn says otherwise. */
export function stop_failure_phase(stop_failure: StopFailure, now_s: number): StopFailurePhase {
    if (stop_failure.error !== 'rate_limit') return 'error';
    return stop_failure.resets_at !== null && stop_failure.resets_at <= now_s ? 'recharged' : 'limited';
}

const LIMIT_WORDS: Record<string, string> = { five_hour: 'session limit', seven_day: 'weekly limit' };

/** `4:40 PM`, or `Thu 4:40 PM` when it is not today — a weekly reset is days out. */
function reset_clock(resets_at: number, now_s: number): string {
    const at = new Date(resets_at * 1000);
    const today = new Date(now_s * 1000).toDateString() === at.toDateString();
    const clock = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return today ? clock : `${at.toLocaleDateString([], { weekday: 'short' })} ${clock}`;
}

/** The mark's words — its title and accessible name. */
export function stop_failure_title(stop_failure: StopFailure, now_s: number): string {
    const phase = stop_failure_phase(stop_failure, now_s);
    if (phase === 'error') return `turn cut off: ${stop_failure.error.replace(/_/g, ' ')} — resume it`;
    const limit = LIMIT_WORDS[stop_failure.limit ?? ''] ?? 'usage limit';
    if (stop_failure.resets_at === null) return stop_failure.message ?? `${limit} hit — resume it once it resets`;
    const clock = reset_clock(stop_failure.resets_at, now_s);
    return phase === 'limited' ? `${limit} · resets ${clock}` : `${limit} reset at ${clock} — resume it`;
}

/** display_state_color as a CSS hex string ('#f85149') for DOM surfaces. */
export function display_state_color_css(state: TerminalDisplayState): string {
    return '#' + display_state_color(state).toString(16).padStart(6, '0');
}

/**
 * The period a state pulses at on every DOM surface that animates it, or null
 * for a still one. Two states move: attention ALARMS (the one asking for you),
 * background BREATHES at half the rate (work still running — don't close it).
 * A surface that cannot animate takes only the colour.
 */
export function state_pulse_period_ms(state: TerminalDisplayState): number | null {
    if (state === 'attention') return TERMINAL_STATUS_FLAGS.attention_pulse_period_ms;
    if (state === 'background') return TERMINAL_STATUS_FLAGS.background_pulse_period_ms;
    return null;
}
