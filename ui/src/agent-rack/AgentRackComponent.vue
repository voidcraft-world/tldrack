<script setup lang="ts">
/**
 * The agent rack — the session list half of the harness terminal.
 *
 * Rendered in more than one place, which is why it takes a transport and does
 * not reach for `window.parent` itself: inside `AgentRackWindow.vue`, where
 * `window.parent === window` and postMessage would talk to itself, and inside
 * a frame of a host application (`mount.ts` `mount_rack_frame`).
 *
 * Everything here is presentation. Whoever holds the other end of the transport
 * owns the rack and pushes it in as one `RackPayload`; the operator's intent goes back
 * out as an action. This component never talks to the bridge or to tmux, which
 * is what keeps it mountable anywhere, and it must stay that way.
 *
 * `new` deliberately carries no session name: only the payload owner knows which
 * names are taken (dead rows included), so letting a surface pick one
 * would silently attach to a running agent. It carries the LAUNCH profile id
 * instead — the one choice that IS this surface's to make.
 *
 * Row content comes from the shared pure module (`build_rack_rows`), so the
 * ordering, overflow and dead-session rules are one set, under unit tests.
 */
import { defineAsyncComponent, computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useArmedAction } from '../composables/useArmedAction.ts';
import {
    build_rack_rows,
    hold_row_order,
    rack_row_key,
    type RackActionId,
    type RackRow,
    type RowSegment,
} from '../engine/terminal/multi-terminal-layout.ts';
import { session_name_error } from '../engine/terminal/session-name.ts';
import { SERVICE_SESSION_EMOJI, shows_service_mark } from '../engine/terminal/service-sessions.ts';
import {
    ANSWER_TO_READ_EMOJI,
    DEAD_SESSION_EMOJI,
    FRESH_SESSION_EMOJI,
    OPEN_WORK_BADGE,
    OPEN_WORK_SEEN_TITLE,
    OPEN_WORK_WAITING_BADGE,
    OPEN_WORK_WAITING_TITLE,
    STOP_FAILURE_EMOJI,
    STUCK_BADGE,
    TERMINAL_STATUS_FLAGS,
    WORK_DONE_EMOJI,
    answer_to_read_shown,
    answer_to_read_title,
    display_state_color_css,
    display_state_emoji,
    fresh_session_shown,
    open_work_badge_title,
    open_work_children_title,
    open_work_mark_kind,
    state_pulse_period_ms,
    stop_failure_phase,
    stop_failure_shown,
    stop_failure_title,
    stuck_shown,
    stuck_title,
    work_done_shown,
    work_done_title,
} from '../config/terminal-status-flags.ts';
// Type-only imports of its own, so the state words cost this page nothing.
import { state_label } from '../engine/terminal/rack-board-logic.ts';
import {
    DEFAULT_RENDER_OPTIONS,
    MAX_TERMINAL_FONT_SIZE,
    MIN_TERMINAL_FONT_SIZE,
    TERMINAL_FONT_FAMILIES,
    TERMINAL_FONT_STEP_PX,
    TERMINAL_RENDERERS,
    TERMINAL_THEME_IDS,
    clamp_render_options,
    type TerminalRenderOptions,
} from '../engine/terminal/terminal-render-options.ts';
import type { RackAction, RackPayload, RackTransport } from './rack-transport.ts';
import type { OpenWorkAnswer } from './open-work-client.ts';
import {
    AUTO_PROFILE_ID,
    background_jobs_label,
    standing_reason,
    type LaunchPick,
    type ProfileInfo,
    type StopFailure,
} from '../engine/terminal/terminal-status-logic.ts';
import { create_step_view, worktree_button_view, worktree_for_session } from '../engine/terminal/rack-view.ts';
import type { CreateStepView, WorktreeButtonKind, WorktreeButtonView, WorktreeRow } from '../engine/terminal/rack-view.ts';
import type { DeviceRow } from '../engine/terminal/device-readout.ts';
// Copy only: `local-network-gate.ts` is dependency-free, so importing it costs
// this component nothing. The gate itself is never READ here: the answer rides
// the payload, because the opt-in is module state per document and this
// component renders in more than one.
import {
    LOCAL_ACCESS_DENIED_HINT,
    LOCAL_ACCESS_LOCKED_HINT,
    LOCAL_ACCESS_PROMPTING_HINT,
} from '../engine/terminal/local-network-gate.ts';

/** `render_options` is optional so this component still renders for a surface
 *  that owns no terminal; both mounts pass it (`mount.ts`). `next_step_target` is where the opened row's
 *  next-step card goes: the rack page's canvas pane (a selector); absent, it sits under the row.
 *  `lead_decisions_target` is the same for an opened lead's DECISIONS card. */
const props = defineProps<{
    transport: RackTransport;
    render_options?: TerminalRenderOptions;
    metrics_visible?: boolean;
    next_step_target?: string | null;
    lead_decisions_target?: string | null;
}>();
const emit = defineEmits<{ 'update:render_options': [TerminalRenderOptions] }>();

const payload = ref<RackPayload | null>(null);
import { mdiCheck, mdiClose, mdiCog, mdiOfficeBuildingOutline, mdiPause, mdiStar, mdiStarOutline, mdiThumbsUpDown, mdiUnfoldLessHorizontal, mdiUnfoldMoreHorizontal } from '../config/icons.ts';
import { open_laya_review } from '../laya-review/open.ts';
import { create_office_relay } from '../engine/terminal/agent-office-link.ts';
import TaskGroupBar from './TaskGroupBar.vue';
import FamilyFoldLine from './FamilyFoldLine.vue';
import MissionBar from './MissionBar.vue';
import { mission_row_title, type MissionRequest } from './missions-client.ts';
import { closed_rows_view, restorable_rows_view, type ClosedRowView, type RestorableRowView } from './closed-client.ts';
import { groups_for_parent } from './task-groups.ts';
import {
    FAMILY_FOLD_KEY, choose_fold, family_readout, fold_rows, folded_families, prune_fold_choices, read_fold_choices,
    row_asks, write_fold_choices, type FamilyFoldReadout,
} from './family-fold.ts';
import IslandGlyph from './IslandGlyph.vue';
import LaunchDirectorySettings from './LaunchDirectorySettings.vue';
import { attention_rows } from './attention-rows.ts';
import IslandMark from './IslandMark.vue';
import { auto_branch } from './auto-branch.ts';
import { chat_query, focus_snippet, highlight_terms, search_rack_rows, starred_rack_rows } from './rack-search.ts';
import { star_filter_title, star_known, star_title, star_value, starred_count } from './star-client.ts';
import type { RackSearchHit } from './chat-search.ts';
import { create_rack_metric_recorder } from './rack-metrics.ts';
import NextStepCard from './NextStepCard.vue';
import LeadDecisionsCard from './LeadDecisionsCard.vue';
import { led_run, open_decisions, type Decision, type DecisionRequest, type LeadRun } from './lead-client.ts';
import { encode_next_step_event } from './next-step-client.ts';
import {
    card_press,
    is_next_step_control,
    next_step_chip,
    press_next_step,
    shown_key,
    type CardPress,
} from './next-step-view.ts';
import { DIAGRAM_PAGE, OFFICE_PAGE } from '../config/pages.ts';
import { UNKNOWN_MODEL_LABEL } from '../engine/terminal/terminal-status-logic.ts';
import '../styles/rack-palette.css';
const AgentSessionGraph = defineAsyncComponent(() => import('./AgentSessionGraph.vue'));
const session_view = ref<'list' | 'graph'>('list');
/** The ⚙ tray (font / renderer / typeface / refresh). Closed on every mount. */
const settings_open = ref(false);
const launch_open = ref(false);
const profiles_open = ref(false);
const directories_open = ref(false);
function toggle_launch(): void {
    launch_open.value = !launch_open.value;
    close_fork();
}
function profile_paused(profile: ProfileInfo): boolean {
    return payload.value?.profile_availability?.state?.paused.includes(profile.id) ?? profile.paused === true;
}
function toggle_profile(profile: ProfileInfo): void {
    send('profile_pause', undefined, JSON.stringify({ profile: profile.id, paused: !profile_paused(profile) }));
}
const available_profiles = computed(() => profiles.value.filter((profile) => !profile_paused(profile)));
const attention_count = computed(() => rows.value.filter(asks_now).length);
const working_count = computed(() => rows.value.filter((row) => row.kind === 'session' && !row.dim
    && (row.state === 'busy' || row.state === 'background')).length);

const session_search = ref('');
/** The bridge's answer for what the box says now: sessions whose CHAT met the terms their titles
 *  did not. The host asks (`chat-search.ts`); while a newer question is out the last answer stands,
 *  so chat rows narrow as you type rather than blink. */
const chat_matches = computed<Readonly<Record<string, RackSearchHit>>>(() => {
    const view = payload.value?.chat_search;
    return view && view.query === chat_query(session_search.value) ? view.matches : {};
});
/** Why a row is in the results when its title is not: the line its conversation matched. */
function chat_hit(row: RackRow): RackSearchHit | null {
    return row.session ? chat_matches.value[row.session] ?? null : null;
}
const CHAT_ROLE_WORD: Readonly<Record<string, string>> = { human: 'You', agent: 'The agent' };
function chat_hit_title(hit: RackSearchHit): string {
    return `${CHAT_ROLE_WORD[hit.role] ?? 'Said'}: ${hit.snippet.replace(/\s+/g, ' ').trim()}`;
}
/** The readout beside the box: how many rows, and whether the chat half is still answering. */
const chat_state = computed<'idle' | 'pending' | 'error'>(() => {
    const view = payload.value?.chat_search;
    if (!view || view.query !== chat_query(session_search.value)) return chat_query(session_search.value) ? 'pending' : 'idle';
    return view.pending ? 'pending' : view.error ? 'error' : 'idle';
});
const chat_status_title = computed<string>(() => {
    const view = payload.value?.chat_search;
    if (chat_state.value === 'pending') return 'Searching what each session said';
    if (view?.error) return view.error;
    if (view?.recent_only) return 'Names, titles and each session\'s recent conversation (the session ledger is off)';
    return 'Names, titles and everything each session said';
});
/** ⌗ HOW: the rack's architecture diagram in its own window. The address: owned by
 *  config/pages.ts — see the note there. The diagrams: owned by
 *  agent-rack-diagram/rack-architecture.md. */
function open_diagram(): void {
    window.open(new URL(DIAGRAM_PAGE, window.location.href).href, 'tldrack-diagram');
}
/** OFFICE: every agent at a desk, in 3D, in its own window, resolved like ⌗ HOW. This rack
 *  feeds it while it is open; a robot's click comes back as `select`, and its right-click
 *  menu's verb as the row action of the same name. The wire: owned by
 *  engine/terminal/agent-office-link.ts — see the note there. */
const office_relay = create_office_relay({
    on_select: (session) => send('select', session),
    on_action: (action, session) => send(action, session),
});
function open_office(): void {
    window.open(new URL(OFFICE_PAGE, window.location.href).href, 'tldrack-office');
}

/**
 * The COMPLETE band's fold (what the band is: `complete-sessions.ts`). Closed by
 * default; the operator's choice to expand it is
 * remembered per device. Local to this page, unlike DEVICES: closing it stops
 * no poll. Two rack documents on one origin share the key, so the `storage`
 * listener in `onMounted` keeps them folded alike.
 */
const COMPLETE_OPEN_KEY = 'tldrack:rack-complete-open';
function read_complete_open(): boolean {
    try {
        return localStorage.getItem(COMPLETE_OPEN_KEY) === '1';
    } catch {
        return false;
    }
}
const complete_open = ref(read_complete_open());
function toggle_complete(): void {
    complete_open.value = !complete_open.value;
    try {
        localStorage.setItem(COMPLETE_OPEN_KEY, complete_open.value ? '1' : '0');
    } catch {
        // Storage refused (a private window): the fold lasts this page.
    }
}
function on_storage(event: StorageEvent): void {
    if (event.key === COMPLETE_OPEN_KEY) complete_open.value = read_complete_open();
    if (event.key === MEMORY_OPEN_KEY) memory_open.value = event.newValue === '1';
    if (event.key === JOBS_OPEN_KEY) jobs_open.value = event.newValue === '1';
    if (event.key === JOBS_EXPANDED_KEY) expanded_jobs.value = read_expanded_jobs(event.newValue);
}


/** True when NOTHING can service a click: a satellite window whose main window
 *  is not servicing the rack. Only the bus feed ever sets it; a framed rack and
 *  the HTTP feed leave `attached` undefined or true. */
const frozen = computed<boolean>(() => payload.value?.attached === false);

/**
 * Every session's row. No `+N MORE` budget here: this list scrolls (`.rack__list`), so a cap
 * only hides agents the operator could have scrolled to. `payload.max_sessions` is for a
 * surface that cannot scroll.
 */
const rows = computed<RackRow[]>(() => {
    const data = payload.value;
    if (!data) return [];
    return build_rack_rows({
        sessions: data.sessions,
        focused: data.focused,
        max_sessions: data.sessions.length,
    });
});

/**
 * The row's state mark: an emoji (`TERMINAL_STATUS_FLAGS.state_emoji`, the
 * default) or the colour dot, each from the ONE mapping every surface shares.
 *
 * One mapping, because a surface that hand-rolls its own hexes drifts: the
 * surface the operator looks at most then disagrees with every other one about
 * the same fact.
 *
 * `config/terminal-status-flags.ts` is a leaf module with ZERO runtime imports
 * of its own, so pulling it in costs this page nothing.
 *
 * Colour is bound inline rather than via a class because it is DATA; the two
 * motions — attention's pulse, background's slower breathe — are behaviours and
 * stay classes, with their PERIOD bound inline from the same flags.
 *
 * CAVEAT: a rack page and a host's main window are separate DOCUMENTS, so each
 * holds its own module instance of the flags. A colour changed in the main
 * window does NOT retint the rack. Push the colours through `RackPayload` if
 * that ever needs to work; until then the defaults are what both sides agree on.
 */
const use_state_emoji = TERMINAL_STATUS_FLAGS.state_emoji;

function dot_class(row: RackRow): string {
    const base = use_state_emoji ? 'glyph' : 'dot';
    if (row.state === 'attention') return `${base} mark--attention`;
    if (row.state === 'background') return `${base} mark--background`;
    if (!use_state_emoji && is_fresh(row)) return `${base} dot--fresh`;
    return base;
}

function dot_style(row: RackRow): Record<string, string> {
    const period = state_pulse_period_ms(row.state ?? 'unknown');
    const color = display_state_color_css(row.state ?? 'unknown');
    // A fresh session's dot is the free colour HOLLOWED: same state, no turn yet.
    const fill: Record<string, string> = is_fresh(row) ? { color } : { background: color };
    const style: Record<string, string> = use_state_emoji ? {} : { ...fill };
    if (period !== null) style.animationDuration = `${period}ms`;
    return style;
}

/** Deeper rows draw at this depth: eight steps already take most of a name cell. */
const FAMILY_INDENT_MAX_DEPTH = 8;

/** The row's family depth as `--rack-depth`, which the mark and name-cell indents
 *  read (the note at `.row__indent` owns how). */
function family_depth_style(row: RackRow): Record<string, string> | undefined {
    const depth = Math.min(row.depth ?? 0, FAMILY_INDENT_MAX_DEPTH);
    return depth > 0 ? { '--rack-depth': String(depth) } : undefined;
}

/** A new session nobody has prompted yet (`fresh_session_shown`). */
function is_fresh(row: RackRow): boolean {
    return fresh_session_shown(row.state, row.fresh, row.dim);
}

/** The row's cut-off turn, where its mark shows one (`stop_failure_shown`). */
function cut_off(row: RackRow): StopFailure | null {
    return row.stop_failure && stop_failure_shown(row.state, row.stop_failure, row.dim) ? row.stop_failure : null;
}

/**
 * Epoch seconds the cut-off marks are read against. Refreshed on every payload,
 * and bumped by ONE timer at the soonest reset on screen: nothing else arrives
 * when a limit lifts, and that moment is the whole point of 🔋.
 */
const now_s = ref(Date.now() / 1000);
const next_reset = computed<number | null>(() => {
    let soonest: number | null = null;
    for (const row of rows.value) {
        const at = cut_off(row)?.resets_at ?? null;
        if (at !== null && at > now_s.value && (soonest === null || at < soonest)) soonest = at;
    }
    return soonest;
});
let reset_timer: ReturnType<typeof setTimeout> | null = null;
function clear_reset_timer(): void {
    if (reset_timer) clearTimeout(reset_timer);
    reset_timer = null;
}
watch(next_reset, (at) => {
    clear_reset_timer();
    if (at === null) return;
    // Against the wall clock, not `now_s`: after a sleep the reset may be past.
    reset_timer = setTimeout(() => {
        reset_timer = null;
        now_s.value = Date.now() / 1000;
    }, Math.max(0, at * 1000 - Date.now()) + 250);
}, { immediate: true });

/** `build_rack_rows` dims a session row exactly when tmux no longer has it, and
 *  keeps its LAST state — which on a dead row would read as a live 🤔. */
function dot_glyph(row: RackRow): string {
    if (!use_state_emoji) return '';
    if (row.dim) return DEAD_SESSION_EMOJI;
    const stopped = cut_off(row);
    if (stopped) return STOP_FAILURE_EMOJI[stop_failure_phase(stopped, now_s.value)];
    if (is_fresh(row)) return FRESH_SESSION_EMOJI;
    if (row.exited || shows_service_mark(row.session, row.state)) return SERVICE_SESSION_EMOJI;
    if (answer_to_read_shown(row.state, row.work_done, row.dim)) return ANSWER_TO_READ_EMOJI;
    if (work_done_shown(row.state, row.work_done ? true : null, row.dim)) return WORK_DONE_EMOJI[row.state];
    return display_state_emoji(row.state ?? 'unknown');
}

/** What Laya's verdict draws on this row — the rule is `open_work_mark_kind`'s. */
function open_work_kind(row: RackRow): ReturnType<typeof open_work_mark_kind> {
    return open_work_mark_kind(row.state, row.open_work ? true : null, row.dim, row.open_work ?? {});
}

/** Laya's ⚠️ on this row's mark: the one that wants the operator. */
function shows_open_work(row: RackRow): boolean {
    return open_work_kind(row) === 'warning';
}

/** ⏳ beside a reply-ready / idle parent whose children still work. A background row's own
 *  mark is already ⏳, so it takes no badge. */
function shows_open_work_waiting(row: RackRow): boolean {
    return row.state !== 'background' && open_work_kind(row) === 'waiting';
}

/** 🧱 beside a working row the bridge flagged stuck. */
function shows_stuck(row: RackRow): boolean {
    return stuck_shown(row.state, row.stuck, row.dim);
}

/** The mark's words, for the tooltip and screen readers. A background row says
 *  how much work closing it would kill. */
function dot_title(row: RackRow): string {
    if (row.dim) return 'session gone';
    const stopped = cut_off(row);
    if (stopped) return stop_failure_title(stopped, now_s.value);
    if (is_fresh(row)) return 'new session · no prompt yet';
    const kind = open_work_kind(row);
    const seen = kind === 'seen' ? ` · ${OPEN_WORK_SEEN_TITLE}` : '';
    if (row.state === 'background') {
        const jobs = background_jobs_label(row.background_jobs);
        return kind === 'waiting' ? `${jobs} · ${OPEN_WORK_WAITING_TITLE}` : jobs + seen;
    }
    if (row.exited) return row.exited.code === 0 ? 'finished job' : 'failed job';
    if (shows_service_mark(row.session, row.state)) return 'service';
    if (answer_to_read_shown(row.state, row.work_done, row.dim)) return answer_to_read_title(row.work_done?.p);
    if (work_done_shown(row.state, row.work_done ? true : null, row.dim)) return work_done_title(row.state, row.work_done?.p);
    const label = state_label(row.state ?? 'unknown');
    return (label === '—' ? 'no status' : label.toLowerCase()) + seen;
}

/**
 * The empty state, when the reason is the GATE rather than the bridge.
 *
 * `null` hands the render back to the original `bridge not running` copy, so
 * that message keeps meaning exactly one thing: the bridge was asked and did
 * not answer. Anything here means it was never asked at all.
 *
 * The heading words match `rack-board-logic.ts::local_access_view` on purpose:
 * a board and the rack may sit side by side, and an operator must not read them
 * as two different problems.
 *
 * **The head states, the button acts, and neither repeats the other.** The gate
 * exports one sentence per state, but those were written for surfaces with no
 * control on them; printing "connect to reach this machine's bridge" beside a
 * CONNECT button says one thing twice.
 * So the sentence goes on `title` — the accessible channel — and only `denied`
 * gets visible body text, because a settings path is an INSTRUCTION rather than
 * a restatement, and it is the one branch with no button to press.
 */
const access_view = computed<{
    head: string;
    /** The sentence version, on `title`. Null where the body already says it. */
    why: string | null;
    detail: string | null;
    /** Null for `denied`: a page cannot re-prompt after a browser block. */
    retry: string | null;
} | null>(() => {
    switch (payload.value?.access) {
        case 'locked':
            return { head: 'LOCAL TOOLS OFF', why: LOCAL_ACCESS_LOCKED_HINT, detail: null, retry: 'CONNECT' };
        case 'prompting':
            return { head: 'AWAITING BROWSER PROMPT', why: LOCAL_ACCESS_PROMPTING_HINT, detail: null, retry: 'RETRY' };
        case 'denied':
            return { head: 'LOCAL ACCESS BLOCKED', why: null, detail: LOCAL_ACCESS_DENIED_HINT, retry: null };
        default:
            return null;
    }
});

const metric_recorder = create_rack_metric_recorder({
    emit: event => props.transport.send('metric', undefined, JSON.stringify(event)),
    focused: () => payload.value?.focused ?? null,
    source: window.parent === window ? 'window' : 'embedded',
    visible: () => {
        if (props.metrics_visible === false || frozen.value || !payload.value?.reachable
            || document.visibilityState !== 'visible') return false;
        const frame = window.frameElement;
        if (frame) {
            const bounds = frame.getBoundingClientRect();
            if (!frame.isConnected || bounds.width <= 0 || bounds.height <= 0 || bounds.bottom <= 0 || bounds.right <= 0
                || bounds.top >= (window.top?.innerHeight ?? innerHeight)
                || bounds.left >= (window.top?.innerWidth ?? innerWidth)) return false;
        }
        // In a host, the terminal and the rack are sibling frames; focus belongs to their common document.
        try { return (window.top?.document ?? document).hasFocus(); } catch { return document.hasFocus(); }
    },
    context: (session) => {
        const data = payload.value;
        const row = data?.sessions.find(item => item.session === (session ?? data.focused) && item.live);
        const state = row?.state ?? 'unknown';
        const done = row && !row.stop_failure && !row.fresh && work_done_shown(state, row.work_done ? true : null, false);
        const open = row && !row.stop_failure && !row.fresh
            && open_work_mark_kind(state, row.open_work ? true : null, false, row.open_work ?? {}) === 'warning';
        return { session: row?.session ?? null, state,
            mark: done ? 'done' : open ? 'open' : state === 'background' ? 'background' : state === 'done' ? 'reply' : 'none',
            rack_size: data?.sessions.filter(item => item.live).length ?? 0,
            context_left: row?.context_left ?? null,
            model: row?.model_label?.slice(0, 160) ?? null, profile: row?.profile?.slice(0, 80) ?? null };
    },
});
watch(() => props.metrics_visible, () => metric_recorder.tick(), { flush: 'sync' });

function send(action: RackAction, session?: string, value?: string): void {
    // Guard as well as disable: `frozen` disables the buttons, but a stale
    // pointer event mid-transition would otherwise post an action nobody is
    // listening for, which reads to the operator as the click having worked.
    if (frozen.value) return;
    metric_recorder.action(action, session);
    report_press(action, session);
    props.transport.send(action, session, value);
}

// ── suggested next step ─────────────────────────────────────────────────
//
// A SUGGESTION, never an act: what it is and why nothing here can run one is owned by
// `bridge/tldrack/next_step.py` — see the note there. This component draws the
// row chip and the opened row's card, and REPORTS presses so the bridge's log can score them.
// [Do it] presses the row's own control through `press_next_step`, from a click and nowhere else.

/** Which door the press now firing came through: [Do it] sets `card` for exactly the synchronous
 *  span of its own handler, so `send` can tell the bridge. */
let press_via: { via: 'card'; hash: string } | null = null;

/** Every fired vocabulary control is reported while the suggester is on (shadow or live): the log
 *  scores a suggestion against what the operator actually pressed. Before the control's own send, so
 *  the report is queued ahead of it. An ARM sends nothing, so only a fired press is reported. */
function report_press(action: RackAction, session?: string): void {
    if (!session || !payload.value?.next_step_mode || !is_next_step_control(action)) return;
    const via = press_via;
    props.transport.send('next_step', session, encode_next_step_event(
        via ? { event: 'press', action, via: 'card', hash: via.hash } : { event: 'press', action, via: 'manual' },
    ));
}

/** The opened row's card sits in the canvas pane when one is mounted, else under the row. */
const card_in_canvas = computed<boolean>(() => !!props.next_step_target);

/** The [Do it] for a row's suggestion, mirroring its control's arm right now. */
function next_step_press(row: RackRow): CardPress | null {
    const session = row.session ?? '';
    return card_press(row.next_step, {
        frozen: frozen.value,
        kill_armed: armed_kill.value === session,
        worktree: worktree_buttons.value.get(session),
        worktree_armed: !!session && worktree_armed(session),
    });
}

/** [Do it]: press the row's own control, exactly as a hand would — its arm and refusals run. */
function do_next_step(row: RackRow): void {
    const step = row.next_step;
    const session = row.session;
    if (!step || !session || frozen.value) return;
    press_via = { via: 'card', hash: step.hash };
    try {
        press_next_step(step, next_step_press(row), {
            select: () => send('select', session),
            kill: () => on_kill_click(row),
            worktree: () => on_worktree_click(session),
        });
    } finally {
        press_via = null;
    }
}

function dismiss_next_step(row: RackRow): void {
    if (!row.next_step || !row.session || frozen.value) return;
    props.transport.send('next_step', row.session, encode_next_step_event({ event: 'dismissed', hash: row.next_step.hash }));
}

/** Is the row's second line already saying something? The chip yields to it: a cut-off reason or
 *  an attention message beside a chip saying "open it" is the same thing said twice. */
function line2_taken(row: RackRow): boolean {
    return (editing.value === row.session && !!rename_error.value) || !!chat_hit(row) || !!create_view(row.session)
        || !!create_failure(row.session) || !!role_segment(row, 'message') || !!cut_off(row);
}

/** The row chip's words, where it draws: not on an opened row whose card sits right under it. */
function next_chip(row: RackRow): string | null {
    if (row.focused && !card_in_canvas.value) return null;
    if (line2_taken(row)) return null;
    return next_step_chip(row.next_step);
}

/** `shown`, once per suggestion per document — watched beside `shown_rows`, below. */
const reported_shown = new Set<string>();
function report_shown(rows: readonly RackRow[]): void {
    if (frozen.value) return;
    for (const row of rows) {
        const step = row.next_step;
        if (!row.session || step?.kind !== 'suggest' || !(next_chip(row) || row.focused)) continue;
        const key = shown_key(row.session, step);
        if (reported_shown.has(key)) continue;
        reported_shown.add(key);
        props.transport.send('next_step', row.session, encode_next_step_event({ event: 'shown', hash: step.hash }));
    }
}

// ── launch ──────────────────────────────────────────────────────────────
//
// The project row pins regular launches to a saved directory. Worktrees and LEAD use the
// bridge's repository configuration, so their forks stay fenced to its startup directory.
// Directory persistence and validation: bridge/tldrack/launch_directories.py.
const directory_state = computed(() => payload.value?.launch_directories?.state);
const chosen_directory = ref<string | null>(null);
const launch_directory = computed(() => directory_state.value?.directories.find(
    (entry) => entry.path === (chosen_directory.value ?? directory_state.value?.default_path),
));
watch(directory_state, (state) => {
    if (state && chosen_directory.value && !state.directories.some((entry) => entry.path === chosen_directory.value)) {
        chosen_directory.value = null;
    }
});
const repository_launch = computed(() => !launch_directory.value || launch_directory.value.path === directory_state.value?.bridge_path);
const repository_hint = computed(() => `Worktrees and LEAD use ${directory_state.value?.bridge_path ?? 'the bridge repository'}. Select that project to use them.`);
function choose_directory(path: string): void {
    chosen_directory.value = path;
    close_fork();
}

/** One LAUNCH chip. `profile` is null on AUTO and on the lone `+ NEW` chip. */
interface LaunchChip {
    key: string;
    kind: 'profile' | 'auto' | 'new';
    /** What a launch sends: the profile id, or null for the legacy bare shell. */
    id: string | null;
    /** The account in words (`A`) — null on a bridge with no profiles. */
    label: string | null;
    profile: ProfileInfo | null;
}

const NEW_CHIP_KEY = '+new';

const profiles = computed<ProfileInfo[]>(() => payload.value?.profiles ?? []);

/** Where AUTO would launch right now, or null on a bridge that cannot pick
 *  (no AUTO chip then — structural gating, like `profiles`). The bridge owns
 *  the scoring and re-picks when the launch lands (`tldrack/launch_pick.py`). */
const auto_pick = computed<LaunchPick | null>(() => payload.value?.launch_pick ?? null);

const launch_chips = computed<LaunchChip[]>(() => {
    if (profiles.value.length) {
        const chips: LaunchChip[] = available_profiles.value.map((profile) => ({ key: profile.id, kind: 'profile', id: profile.id, label: profile.label, profile }));
        if (auto_pick.value) chips.unshift({ key: AUTO_PROFILE_ID, kind: 'auto', id: AUTO_PROFILE_ID, label: 'AUTO', profile: null });
        return chips;
    }
    return [{ key: NEW_CHIP_KEY, kind: 'new', id: null, label: null, profile: null }];
});

/** The chip whose fork is open — one at a time — or null. */
const fork_key = ref<string | null>(null);

/** `+ NEW` follows the bridge's own default, which is launchable by definition;
 *  AUTO is launchable while the bridge has somewhere to put it. */
function chip_launchable(chip: LaunchChip): boolean {
    if (launch_directory.value && !payload.value?.launch_cwd) return false;
    if (chip.kind === 'auto') return available_profiles.value.some((profile) => profile.id === auto_pick.value?.id);
    return chip.profile === null || chip.profile.cli_found;
}

/** LEAD on the fork: a bridge that answers `/lead/state` can launch one (`RackPayload.lead`). */
const lead_available = computed<boolean>(() => payload.value?.lead !== undefined);

/** A lead is always a Claude subscription: AUTO, which the bridge resolves among the Claude
 *  accounts, or a Claude account's own chip. Never a Codex profile or SHELL. */
function chip_offers_lead(chip: LaunchChip): boolean {
    if (!lead_available.value) return false;
    if (chip.kind === 'auto') return available_profiles.value.some((p) => p.kind === 'claude' && p.cli_found);
    const profile = chip.profile ?? profiles.value.find((p) => p.id === chip.id);
    return profile?.kind === 'claude' && profile.cli_found;
}

/** A chip forks while it has somewhere besides MAIN to offer: 🏝, LEAD, or both. */
function chip_forks(chip: LaunchChip): boolean {
    return worktrees_available.value || chip_offers_lead(chip);
}

function close_fork(): void {
    fork_key.value = null;
}

function on_chip(chip: LaunchChip, event: Event): void {
    if (frozen.value || !chip_launchable(chip)) return;
    if (!chip_forks(chip)) {
        launch_main(chip);
        return;
    }
    if (fork_key.value === chip.key) {
        close_fork();
        return;
    }
    fork_key.value = chip.key;
    // Read NOW: `currentTarget` is nulled once dispatch finishes. Focus lands
    // on MAIN so a keyboard press is chip, ⏎ — the same two presses as a click.
    const slot = (event.currentTarget as HTMLElement | null)?.parentElement;
    void nextTick(() => slot?.querySelector<HTMLElement>('.rack__fork-main')?.focus());
}

/** Esc inside an open fork closes it and hands focus back to its chip, rather
 *  than to a button that is about to unmount. */
function on_fork_esc(event: KeyboardEvent): void {
    if (fork_key.value === null) return;
    event.preventDefault();
    event.stopPropagation();
    const slot = event.currentTarget as HTMLElement | null;
    close_fork();
    slot?.querySelector<HTMLElement>('.rack__chip')?.focus();
}

function launch_main(chip: LaunchChip): void {
    close_fork();
    send('new', undefined, launch_directory.value
        ? JSON.stringify({ profile: chip.id, cwd: launch_directory.value.path })
        : chip.id ?? undefined);
}

/**
 * 🏝 launches at once, like MAIN: a new branch named off the clock
 * (`auto-branch.ts`), no first task, and the pane jumps to the session the poll
 * it exists (`worktree-client.ts::create_island_focus`). No task-and-branch form
 * sits between the press and the agent: it would ask what nearly everyone
 * answers blank. The task is typed into the agent itself, and the branch
 * renames like any other.
 */
function launch_island(chip: LaunchChip): void {
    close_fork();
    if (frozen.value || !repository_launch.value) return;
    const branch = auto_branch('', chip.label ?? '', new Date(), known_branches.value);
    // One JSON string, because `RackAction`'s argument is one optional string by
    // design — it crosses a postMessage boundary AND a BroadcastChannel, and
    // every hop re-validates `typeof === 'string'`.
    const request: Record<string, string> = { branch };
    if (chip.id) request.profile = chip.id;
    send('worktree', undefined, JSON.stringify(request));
}

/** LEAD: the bridge starts the lead and names its session; the pane focuses it once it is live
 *  (`lead-client.ts::launch_lead`). AUTO sends `"auto"`, resolved among the Claude accounts. */
function launch_lead(chip: LaunchChip): void {
    close_fork();
    if (frozen.value || !repository_launch.value || !chip_offers_lead(chip)) return;
    send('lead', undefined, JSON.stringify({ profile: chip.id ?? AUTO_PROFILE_ID }));
}

/** The chip whose fork is open wears the create accent. */
function chip_on(chip: LaunchChip): boolean {
    return fork_key.value === chip.key;
}

/** The account AUTO would land on, outlined dashed while AUTO's fork is open —
 *  "this one, by AUTO" — so the press shows its destination. */
function is_auto_target(chip: LaunchChip): boolean {
    return chip.kind === 'profile' && fork_key.value === AUTO_PROFILE_ID && auto_pick.value?.id === chip.id;
}

/** An account's used % for one window, or null where no report exists (a
 *  shell, an account that has never reported, or a window past its reset
 *  — the bridge drops those, see tldrack usage.py). */
function used_pct(profile: ProfileInfo, window: 'five_hour' | 'seven_day'): number | null {
    const used = profile.usage?.[window]?.used_percentage;
    return typeof used === 'number' ? Math.min(100, Math.max(0, used)) : null;
}
const five_hour = (profile: ProfileInfo): number | null => used_pct(profile, 'five_hour');
const weekly = (profile: ProfileInfo): number | null => used_pct(profile, 'seven_day');

/** Past this a launch on that account is likely to hit its cap mid-task. */
const USAGE_WARN_PCT = 80;

/** Either window at its cap: a launch there stalls at once. The chip stays
 *  launchable — the reset may be minutes away and the operator can see when. */
function is_spent(profile: ProfileInfo): boolean {
    return is_out(profile) || (weekly(profile) ?? 0) >= 100 || (five_hour(profile) ?? 0) >= 100;
}

/** The account cannot run a turn at all (`ProfileStanding`). Its last usage windows are then
 *  history, so the chip prints OUT where the percentage was. Still launchable: the operator
 *  who just renewed the plan starts an agent there, and its first finished turn clears this. */
function is_out(profile: ProfileInfo): boolean {
    return profile.standing?.state === 'out';
}

function out_title(profile: ProfileInfo): string {
    const standing = profile.standing!;
    const since = typeof standing.since === 'number'
        ? ` since ${new Date(standing.since * 1000).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}`
        : '';
    const said = standing.message ? ` · it said: ${standing.message}` : '';
    return `${profile.label} is OUT: ${standing_reason(standing)}${since}${said} · AUTO skips it · a finished turn on it clears this`;
}

function resets_label(epoch: number | null | undefined): string {
    if (typeof epoch !== 'number') return '';
    const at = new Date(epoch * 1000);
    return ` · resets ${at.toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}`;
}

function usage_age_label(age: number | undefined): string {
    if (typeof age !== 'number' || age < 120) return '';
    return age < 7200 ? ` · as of ${Math.round(age / 60)}m ago` : ` · as of ${Math.round(age / 3600)}h ago`;
}

/** "a new A session", or the legacy bridge's bare shell. */
function chip_what(chip: LaunchChip): string {
    if (chip.kind === 'auto') return 'a new session on the freest account, picked when it lands';
    return chip.label ? `a new ${chip.label} session` : 'a new tmux session';
}

function chip_title(chip: LaunchChip): string {
    if (launch_directory.value && !payload.value?.launch_cwd) return 'Reload the parent app to launch in a selected directory';
    const profile = chip.profile;
    if (chip.kind === 'auto' && !auto_pick.value?.id) return 'AUTO: no agent profile can launch on this machine';
    if (profile && !profile.cli_found) return `${profile.label}: CLI not found on this machine`;
    const places = [launch_directory.value?.label ?? 'MAIN (the main checkout)'];
    if (worktrees_available.value) places.push('🏝 (its own worktree)');
    if (chip_offers_lead(chip)) places.push('LEAD (it runs workers)');
    const parts = [places.length > 1
        ? `Start ${chip_what(chip)} — then ${places.slice(0, -1).join(', ')} or ${places[places.length - 1]}`
        : `Start ${chip_what(chip)} and focus it`];
    if (chip.kind === 'auto' && auto_pick.value) parts.push(`now ${auto_pick.value.reason}`);
    if (!profile) return parts.join(' · ');
    if (is_out(profile)) return [out_title(profile), ...parts].join(' · ');
    const week = weekly(profile);
    if (week !== null) parts.push(`week ${Math.round(week)}%${resets_label(profile.usage?.seven_day?.resets_at)}`);
    const used = five_hour(profile);
    if (used !== null) parts.push(`5h ${Math.round(used)}%${resets_label(profile.usage?.five_hour?.resets_at)}`);
    if (week !== null || used !== null) parts[parts.length - 1] += usage_age_label(profile.usage?.age_seconds);
    if (profile.hooks_ok === false) {
        parts.push(`status hooks out of date — run: tldrack profiles --install-hooks ${profile.id}`);
    }
    return parts.join(' · ');
}

function main_title(chip: LaunchChip): string {
    return `Start ${chip_what(chip)} in ${launch_directory.value?.path ?? 'the main checkout'} and focus it`;
}

function island_title(chip: LaunchChip): string {
    if (!repository_launch.value) return repository_hint.value;
    return `Start ${chip_what(chip)} in its own worktree, on a new branch — its edits land there, not in main`;
}

function lead_title(chip: LaunchChip): string {
    if (!repository_launch.value) return repository_hint.value;
    const who = chip.kind === 'auto' ? 'the freest Claude account, picked when it lands' : (chip.label ?? 'this account');
    return `Start a LEAD on ${who}: it splits a plan into steps, runs each in a worker session of its own worktree, and brings you only the decisions`;
}

/** id → label, for the row chips. */
const profile_labels = computed<Map<string, string>>(
    () => new Map(profiles.value.map((p) => [p.id, p.label])),
);

/** The PROFILE column: drawn wherever there is a choice of account (the same
 *  test as the LAUNCH label), on every session row, default included. The rule
 *  since 2026-10-28: not only where a row DIFFERS from the default, because a
 *  blank there reads the same as "account unknown", and a column is read top to
 *  bottom, where A is the answer and not wallpaper. */
const show_profile_column = computed<boolean>(() => profiles.value.length >= 2);

/** The trailing cell (MODEL, then PROFILE) exists when either has something to print. */
const show_who_column = computed<boolean>(() => show_profile_column.value || rows.value.some((row) => row_model(row) !== null));

/** A row's model for the MODEL readout beside its account (`Fable 5.2`), or
 *  null: no model on record yet, or the one its profile already names (a profile
 *  label beside the model it is named for says one thing twice). */
function row_model(row: RackRow): string | null {
    if (!row.model || row.model === UNKNOWN_MODEL_LABEL) return null;
    const own = profiles.value.find((p) => p.id === row.profile)?.model_label;
    return row.model === own ? null : row.model;
}

/** The column's width in characters: the longest label the bridge lists, so a
 *  row's label sits at one x whatever the rack happens to be running. */
const profile_column_chars = computed<number>(() => Math.max(1, ...profiles.value.map((p) => p.label.length)));

/** A row's account, or null when the bridge attributes it to none — a session
 *  started outside the rack, or a profile id this bridge no longer lists. */
function row_profile_label(row: RackRow): string | null {
    return (row.profile && profile_labels.value.get(row.profile)) || null;
}

// ── kill arming ─────────────────────────────────────────────────────────
//
// EVERY ✕ asks twice: the first click arms, only a second one inside
// `KILL_ARM_MS` sends. The rule since 2026-11-02: a session closed by a stray
// click is the stronger argument than "a step on every row trains the confirm
// away". `↺ CLOSED N` still undoes a kill. No `window.confirm`: this component
// is also mounted in a frame, where a modal blocks the page.
//
// The armed label is `KILL?`. A cut-off turn (🪫 / 🔋 / 🛑) keeps its own
// tooltip, because the kill also drops its RESTORE offer.
const KILL_ARM_MS = 4000;
const complete_delete = useArmedAction(KILL_ARM_MS);
const complete_delete_armed = complete_delete.armed;
// Use the band's family classification before search, folds, and the row budget.
const complete_sessions = computed(() => {
    return rows.value
        .flatMap((row) => row.kind === 'session' && row.complete && row.session ? [row.session] : [])
        .sort();
});
const complete_delete_key = computed(() => JSON.stringify(complete_sessions.value));

function delete_all_complete(): void {
    if (frozen.value || !payload.value?.reachable || !complete_sessions.value.length) return;
    disarm_kill();
    complete_delete.press(complete_delete_key.value, () => {
        for (const session of complete_sessions.value) send('kill', session);
    });
}

/** Session whose ✕ is armed (`KILL?`), or null. */
const armed_kill = ref<string | null>(null);
let kill_arm_timer: ReturnType<typeof setTimeout> | null = null;

function disarm_kill(): void {
    if (kill_arm_timer) {
        clearTimeout(kill_arm_timer);
        kill_arm_timer = null;
    }
    armed_kill.value = null;
}

/** What this row's armed ✕ warns about. */
function kill_arm_reason(row: RackRow): 'cut_off' | 'kill' {
    return cut_off(row) ? 'cut_off' : 'kill';
}

function on_kill_click(row: RackRow): void {
    if (frozen.value || !row.session) return;
    complete_delete.disarm();
    if (armed_kill.value === row.session) {
        disarm_kill();
        send('kill', row.session);
        return;
    }
    disarm_kill();
    armed_kill.value = row.session;
    kill_arm_timer = setTimeout(() => {
        kill_arm_timer = null;
        armed_kill.value = null;
    }, KILL_ARM_MS);
}

// ── the ⚠️'s three answers ─────────────────────────────────────────────
//
// A press on ⚠️ opens its choices beside it, and only a second press acts, so a stray click on
// an 11px badge never answers Laya: ⏸ NOT NOW parks the loop (seen, nothing posted — its repeats
// draw nothing until you next prompt that session), ✗ NOT OPEN clears the ⚠️ and ✓ STILL OPEN
// keeps it, and each of those two is the operator's GRADE of that verdict. After a ✓ the turn has
// its grade, so the menu draws no second ✓. This menu is the rack's one grading control: no second pair, no hover strip.
// Owners: `tldrack/attention.py` § 3 (the loop) and `open_work.py` § The operator's grade.
// Closes itself after `OPEN_WORK_MENU_MS`, on any rack update that retires the ⚠️, and on
// unmount: an armed state must not outlive its button (`engine/terminal/arm-logic.ts`).
const OPEN_WORK_MENU_MS = 4000;
/** Session whose ⚠️ answers are open, or null. */
const open_work_menu = ref<string | null>(null);
let open_work_menu_timer: ReturnType<typeof setTimeout> | null = null;

function close_open_work_menu(): void {
    if (open_work_menu_timer) {
        clearTimeout(open_work_menu_timer);
        open_work_menu_timer = null;
    }
    open_work_menu.value = null;
}

function toggle_open_work_menu(row: RackRow): void {
    if (frozen.value || !row.session) return;
    const was = open_work_menu.value === row.session;
    close_open_work_menu();
    if (was) return;
    open_work_menu.value = row.session;
    open_work_menu_timer = setTimeout(() => {
        open_work_menu_timer = null;
        open_work_menu.value = null;
    }, OPEN_WORK_MENU_MS);
}

function answer_open_work(row: RackRow, action: OpenWorkAnswer): void {
    close_open_work_menu();
    if (row.session) send(action, row.session);
}

// ── worktrees ───────────────────────────────────────────────────────────
//
// `payload.worktrees` is ABSENT when the bridge has no `worktree` plugin
// registered, which is the common case on any other machine. Absent must grow
// no UI at all: that is structural gating, not a feature flag.
const worktrees_available = computed<boolean>(() => payload.value?.worktrees !== undefined);

// ── devices ─────────────────────────────────────────────────────────────
//
// CPU and memory per machine, under the list. Absent `devices` is a bridge
// without the plugin: no section. Open/closed belongs to the payload's owner,
// because closing is what stops the sampling (`devices-client.ts`).
const device_rows = computed<DeviceRow[] | null>(() => payload.value?.devices ?? null);
// Absent `missions` grows no section. Computed like `device_rows`: the section sits outside the
// template's payload branch, so it renders before a first payload exists.
const missions = computed(() => payload.value?.missions ?? null);
const mission_focused = computed(() => payload.value?.focused ?? null);
// Who can join a mission, under the names their rows wear.
const mission_sessions = computed(() => (payload.value?.sessions ?? [])
    .filter((row) => row.live)
    .map((row) => ({ session: row.session, label: row.auto_name || row.session })));
function mission_title(session: string | undefined): string | null {
    return mission_row_title(payload.value?.missions, session);
}
function send_mission(request: MissionRequest): void {
    send('mission', undefined, JSON.stringify(request));
}

// ── LEAD decisions ──────────────────────────────────────────────────────
//
// The opened lead row's run, while a decision of it waits on the operator: the DECISIONS card,
// teleported like the next-step card. No open decision, no card — never an empty box.
const lead_card = computed<{ run: LeadRun; decisions: Decision[] } | null>(() => {
    const data = payload.value;
    if (!data?.lead || !data.focused) return null;
    const run = led_run(data.lead, data.sessions.find((row) => row.session === data.focused)?.lead);
    const decisions = run ? open_decisions(run) : [];
    return run && decisions.length > 0 ? { run, decisions } : null;
});
function send_decision(request: DecisionRequest): void {
    send('lead_decision', undefined, JSON.stringify(request));
}
const devices_open = computed<boolean>(() => payload.value?.devices_open !== false);
// Presentation only: DEVICES still owns sampling. Remember the optional breakdown on this device.
const MEMORY_OPEN_KEY = 'tldrack:rack-memory-open';
const memory_open = ref(false);
try {
    memory_open.value = localStorage.getItem(MEMORY_OPEN_KEY) === '1';
} catch { /* Storage unavailable: start folded. */ }
function toggle_memory(): void {
    memory_open.value = !memory_open.value;
    try {
        localStorage.setItem(MEMORY_OPEN_KEY, memory_open.value ? '1' : '0');
    } catch { /* The choice still lasts this page. */ }
}
// A compute host's JOBS (what it runs: `device-readout.ts` `DeviceJobs`). Presentation only, like the memory
// breakdown: the list folds, each job opens to its processes, and both are remembered on this device.
const JOBS_OPEN_KEY = 'tldrack:rack-jobs-open';
const JOBS_EXPANDED_KEY = 'tldrack:rack-jobs-expanded';
/** Open jobs remembered; past this, the oldest are forgotten. */
const JOBS_EXPANDED_MAX = 24;
function read_expanded_jobs(raw: string | null): Set<string> {
    try {
        const keys: unknown = JSON.parse(raw ?? '[]');
        return new Set(Array.isArray(keys) ? keys.filter((key): key is string => typeof key === 'string') : []);
    } catch {
        return new Set();
    }
}
const jobs_open = ref(false);
const expanded_jobs = ref<Set<string>>(new Set());
try {
    jobs_open.value = localStorage.getItem(JOBS_OPEN_KEY) === '1';
    expanded_jobs.value = read_expanded_jobs(localStorage.getItem(JOBS_EXPANDED_KEY));
} catch { /* Storage unavailable: start folded. */ }
function toggle_jobs(): void {
    jobs_open.value = !jobs_open.value;
    try {
        localStorage.setItem(JOBS_OPEN_KEY, jobs_open.value ? '1' : '0');
    } catch { /* The choice still lasts this page. */ }
}
function toggle_job(key: string): void {
    const next = new Set(expanded_jobs.value);
    if (!next.delete(key)) next.add(key);
    expanded_jobs.value = new Set([...next].slice(-JOBS_EXPANDED_MAX));
    try {
        localStorage.setItem(JOBS_EXPANDED_KEY, JSON.stringify([...expanded_jobs.value]));
    } catch { /* The choice still lasts this page. */ }
}

// ── closed ──────────────────────────────────────────────────────────────
//
// ↺ CLOSED: the undo for a ✕. Absent `closed` is a bridge with nothing to offer
// (or one that predates `POST /reopen`): no button. The list and its open state
// belong to the payload's owner (`closed-client.ts`), which reads the bridge.
const closed_view = computed(() => payload.value?.closed ?? null);
/** Ages are taken when the payload lands, which is often enough for `12m`. */
const closed_rows = computed<ClosedRowView[]>(() => closed_rows_view(closed_view.value?.rows ?? [], Date.now()));
const restorable_view = computed(() => closed_view.value?.restorable ?? null);
const restorable_rows = computed<RestorableRowView[]>(() => restorable_rows_view(restorable_view.value?.rows ?? [], Date.now()));
const restorable_ok = computed<number>(() => restorable_rows.value.filter((row) => row.ok).length);
const closed_busy = computed<boolean>(() =>
    closed_view.value?.reopening !== undefined || restorable_view.value?.restoring !== undefined);
/** The header button leads with what a reboot took down: that is the one the operator came for. */
const closed_button_label = computed<string>(() => {
    const view = closed_view.value;
    if (!view) return '';
    const r = view.restorable?.count ?? 0;
    return r > 0 ? `↺ RESTORE ${r}` : `↺ CLOSED ${view.count}`;
});
const closed_button_title = computed<string>(() => {
    const view = closed_view.value;
    if (!view) return '';
    if (view.open) return 'Hide the sessions that can come back';
    const r = view.restorable?.count ?? 0;
    const restore = r > 0 ? `${r} were running when the machine went down — restore them where they were, conversation and all` : '';
    const closed = view.count > 0 ? `${view.count} recently closed — reopen one where it was` : '';
    return [restore, closed].filter(Boolean).join(' · ');
});
// RESTORE ALL starts every agent at once: armed, like CLOSE ALL, and on the same timer.
const restore_all = useArmedAction(KILL_ARM_MS);
const restore_all_armed = restore_all.armed;
const restorable_all_armed_now = computed<boolean>(() => restore_all_armed.value === `restore:${restorable_ok.value}`);
function press_restore_all(): void {
    if (frozen.value || closed_busy.value || restorable_ok.value === 0) return;
    restore_all.press(`restore:${restorable_ok.value}`, () => send('restore'));
}

const worktree_by_session = computed<Map<string, WorktreeRow>>(() => {
    const map = new Map<string, WorktreeRow>();
    const worktrees = payload.value?.worktrees ?? [];
    // Every managed row under its own derived name first — a row mid-create has
    // no tmux session yet and must still be findable by the ladder below.
    for (const row of worktrees) map.set(row.session, row);
    // Then a live session standing INSIDE a managed worktree under some other
    // tmux name (`claude -w` in a `+ NEW` pane; `EnterWorktree` mid-session),
    // joined by cwd. `worktree_for_session` owns the rule.
    for (const session of payload.value?.sessions ?? []) {
        if (map.has(session.session)) continue;
        const row = worktree_for_session(session, worktrees);
        if (row) map.set(session.session, row);
    }
    return map;
});

/**
 * The create readout for a row, or null.
 *
 * `+ WORKTREE` is not instant — `git worktree add`, then a bootstrap that runs
 * a `tsc` emit, then the tmux spawn — and with no readout the first run reads
 * as a button that did nothing. The plugin reports which step it is on; this
 * turns that into a label and a determinate meter.
 *
 * ONE cause, ONE indicator: this drives the rack row and nothing else, so a
 * create never puts two progress marks on screen at once.
 */
const create_view_by_session = computed<Map<string, CreateStepView>>(() => {
    const map = new Map<string, CreateStepView>();
    for (const row of payload.value?.worktrees ?? []) {
        const view = create_step_view(row);
        if (view) map.set(row.session, view);
    }
    return map;
});

/** A MAP LOOKUP, not a rebuild. The template reads this several times per row,
 *  and `create_step_view` allocates, so calling it per binding per render is
 *  cost for nothing. */
function create_view(session: string | null | undefined): CreateStepView | undefined {
    return session ? create_view_by_session.value.get(session) : undefined;
}

/** A create that FAILED — the plugin expires these, so it is not permanent. */
const create_error_by_session = computed<Map<string, string>>(() => {
    const map = new Map<string, string>();
    for (const row of payload.value?.worktrees ?? []) {
        if (row.create_status === 'error') map.set(row.session, row.create_error ?? 'create failed');
    }
    return map;
});

function create_failure(session: string | null | undefined): string | undefined {
    return session ? create_error_by_session.value.get(session) : undefined;
}

/** The branches the plugin manages — what `auto_branch` must not hand out twice. */
const known_branches = computed<ReadonlySet<string>>(
    () => new Set((payload.value?.worktrees ?? []).map((row) => row.branch)),
);

// The worktree button: PR, or CLEAN once the branch has landed
// (`rack-view.ts::worktree_button_kind` owns which). Both arm twice, in the same
// hand-rolled shape as the ✕ above: PR PUSHES TO GITHUB and opens a public pull
// request, CLEAN kills the session and deletes a branch — neither is undone by a
// click. No `window.confirm`: this component is also mounted in a frame, where a
// modal blocks the page.
const WORKTREE_ARM_MS = 4000;
/** The armed worktree button, keyed by KIND too — `worktree_button_view` owns why. */
const armed_worktree = ref<{ session: string; kind: WorktreeButtonKind } | null>(null);
let worktree_arm_timer: ReturnType<typeof setTimeout> | null = null;

function disarm_worktree(): void {
    if (worktree_arm_timer) {
        clearTimeout(worktree_arm_timer);
        worktree_arm_timer = null;
    }
    armed_worktree.value = null;
}

/** Each worktree row's button, built once per change rather than per binding —
 *  the same reason `create_view` is a map lookup. */
const worktree_buttons = computed<Map<string, WorktreeButtonView>>(() => {
    const map = new Map<string, WorktreeButtonView>();
    const armed = armed_worktree.value;
    for (const [session, row] of worktree_by_session.value) {
        map.set(session, worktree_button_view(row, armed?.session === session ? armed.kind : null));
    }
    return map;
});

/** Is this row's worktree button armed AS the kind it shows now? */
function worktree_armed(session: string): boolean {
    const armed = armed_worktree.value;
    return armed?.session === session && armed.kind === worktree_buttons.value.get(session)?.kind;
}

/**
 * The worktree button. `PR ↗` opens the pull request on GitHub, one press, no arm:
 * it writes nothing. Otherwise the first press arms and the second sends — `pr`
 * (commit, push, open the PR) or `cleanup` (kill, remove the tree, delete the
 * branch) — to the bridge.
 */
function on_worktree_click(session: string): void {
    if (frozen.value) return;
    const view = worktree_buttons.value.get(session);
    if (!view || view.press === 'none') return;
    if (view.press === 'open') {
        disarm_worktree();
        if (view.url) window.open(view.url, '_blank', 'noopener');
        return;
    }
    if (worktree_armed(session)) {
        disarm_worktree();
        send(view.kind, session);
        return;
    }
    disarm_worktree();
    armed_worktree.value = { session, kind: view.kind };
    worktree_arm_timer = setTimeout(() => {
        worktree_arm_timer = null;
        armed_worktree.value = null;
    }, WORKTREE_ARM_MS);
}

/**
 * Which action buttons a row actually shows.
 *
 * `✎` is never one of them here: the boxed session name IS the rename control
 * on this surface (see § rename), and a pencil beside it would be a second door
 * to the same act inside one row. `rename` in `row.actions` only says the
 * name box may edit (`can_rename`).
 */
function visible_actions(row: RackRow): Exclude<RackActionId, 'rename'>[] {
    // A `RackActionId` subtype, not `string[]`: the template narrows this value
    // through `kill`, and a `string` would let anything through to `send()`.
    return row.actions.filter((action): action is Exclude<RackActionId, 'rename'> => action !== 'rename');
}

/** Tooltip for a row's action button. Out of the template because the ✕ has
 *  three states, which is past what a nested ternary there can carry. */
function action_title(action: Exclude<RackActionId, 'rename'>, row: RackRow): string {
    if (action === 'focus') return 'Show in the live terminal';
    const stopped = cut_off(row);
    if (stopped && kill_arm_reason(row) === 'cut_off') {
        return armed_kill.value === row.session
            ? `Confirm: kills '${row.session}' with its turn unfinished`
            : `Kill this tmux session — its turn was cut off (${stop_failure_title(stopped, now_s.value)}), so this asks twice`;
    }
    if (row.exited) {
        return armed_kill.value === row.session
            ? `Confirm: closes '${row.session}'`
            : 'Close this ended job — nothing runs in it, only its log goes — press twice';
    }
    return armed_kill.value === row.session
        ? `Confirm: kills '${row.session}'`
        : 'Kill this tmux session — press twice';
}

// ── label ───────────────────────────────────────────────────────────────
//
// The operator's feedback on what the rack drew for one session: Laya's ✅ / 💬 / ⚠️ on its turn ends,
// and the auto name Haiku gave it. Both are graded on the Laya REVIEW page, opened here narrowed to this
// session (`laya-review/open.ts` owns the address). `since` is the session's tmux start, so a reused
// `vc-N` brings none of an older session's turns. Read-only until a grade is pressed there, so one press
// is enough — nothing here to arm.

/** Agent sessions only: an ended job pane has no turns and no auto name. */
function can_label(row: RackRow): boolean {
    return row.kind === 'session' && !!row.session && !row.exited;
}

/** Why a press did nothing, per session, until the next press: a blocked popup is the one way it fails. */
const label_blocked = ref<string | null>(null);

function label_title(row: RackRow): string {
    if (label_blocked.value === row.session) return 'The browser blocked the REVIEW window: allow popups for this page, then press again';
    return 'Label this session: were its ✅ 💬 ⚠️ marks right, and does its name fit? Opens Laya REVIEW';
}

function open_label(row: RackRow): void {
    if (!row.session) return;
    const opened = open_laya_review(undefined, 'recent', { session: { name: row.session, since: row.created_at ?? null } });
    label_blocked.value = opened === 'blocked' ? row.session : null;
}

// ── rename ──────────────────────────────────────────────────────────────
//
// Edited INLINE, not in a modal. This component is mounted in more than one
// kind of document (the rack page, a frame in a host), and a framed one cannot
// reach its host's overlays, so a shared inline editor is the only version that
// behaves identically in both.
//
// The door is the session name itself, drawn as a box: a click INSIDE the box
// edits, a click anywhere else on the row selects. Right-click on the row is
// the accelerator. Focus surviving the rename is the host's job
// (`engine/terminal/rename-tracker.ts`).

/** A live row whose name can be edited — dead rows are kill-only. */
function can_rename(row: RackRow): boolean {
    return row.kind === 'session' && !!row.session && row.actions.includes('rename');
}

/** The name box is locked while `/spawn` is still building the session or the
 *  live pane is still loading it — `agent-rack/pane-settle.ts` owns why. */
function name_locked(row: RackRow): boolean {
    const data = payload.value;
    return !!row.session && (row.session === data?.spawning || row.session === data?.pane_loading);
}

function name_title(row: RackRow): string {
    return name_locked(row) ? 'Starting — rename once the terminal is up'
        : row.auto_name ? `Auto-named by topic · ${row.session} · Click to rename` : `Rename '${row.session}'`;
}

/** The inline detail after a session row's name: model (where unusual), a
 *  service readout, background jobs. The context reading and the attention
 *  message have places of their own — the ctx column and the second line. */
function detail_segments(row: RackRow): RowSegment[] {
    return row.segments?.filter((segment) => !segment.name && !segment.role) ?? [];
}

/** The segment a session row puts in `role`'s place, or null. */
function role_segment(row: RackRow, role: NonNullable<RowSegment['role']>): RowSegment | null {
    return row.segments?.find((segment) => segment.role === role) ?? null;
}

/** The mark's tooltip: its state, then the model the row text may have left
 *  out as the usual one (`usual_model_label`). */
function mark_title(row: RackRow): string {
    return row.model ? `${dot_title(row)} · ${row.model}` : dot_title(row);
}

/** Session whose row is currently an input, or null. */
const editing = ref<string | null>(null);
const draft = ref('');
/** Inline validation copy; nulled whenever the draft becomes valid again. */
const rename_error = ref<string | null>(null);
const rename_input = ref<HTMLInputElement | null>(null);
/** Row order captured when the rename opened; null otherwise. Why the list
 *  holds still: owned by `multi-terminal-layout.ts` `hold_row_order`. */
const held_order = ref<string[] | null>(null);
/** A press landed outside the box since it last took focus or a key. */
let pressed_away = false;

// ── stars ───────────────────────────────────────────────────────────────
//
// The operator's own mark on a row, and the one filter that goes with it. What a star is: owned by
// `star-client.ts` — see the note there. The filter is drawn only while something is starred, and
// it is not remembered: a list that opens already narrowed reads as sessions gone missing.
const starred_only = ref(false);
const starred_total = computed<number>(() => starred_count(rows.value));
watch(starred_total, (total) => { if (!total) starred_only.value = false; });

/** The row draws the star: a starred row always (so it can be cleared), else a live agent row. */
function shows_star(row: RackRow): boolean {
    return star_known(row) && (row.starred || can_rename(row));
}

function toggle_star(row: RackRow): void {
    if (row.session) send('star', row.session, star_value(row.starred !== true));
}

// ── family folds ──────────────────────────────────────────────────────────
//
// A family drawn as one row. What a fold is, and its four rules: owned by `family-fold.ts` — see the note there.
const fold_choices = ref<Map<string, boolean>>(new Map());
try {
    fold_choices.value = read_fold_choices(localStorage.getItem(FAMILY_FOLD_KEY));
} catch { /* Storage unavailable: the defaults only. */ }
function save_fold_choices(next: Map<string, boolean>): void {
    fold_choices.value = next;
    try {
        localStorage.setItem(FAMILY_FOLD_KEY, write_fold_choices(next));
    } catch { /* The choice still lasts this page. */ }
}
/** FIND and ★ open every family (rule 4), so nothing reads as folded while either narrows the list. */
const folded = computed<Set<string>>(() => session_search.value.trim() || starred_only.value
    ? new Set() : folded_families(rows.value, payload.value?.task_groups, fold_choices.value));
function is_folded(row: RackRow): boolean {
    return !!row.node_id && folded.value.has(row.node_id);
}
function fold_word(row: RackRow): string {
    return is_folded(row) ? 'Open workers' : 'Fold workers';
}
function toggle_family(row: RackRow): void {
    if (row.node_id) save_fold_choices(choose_fold(fold_choices.value, row.node_id, !is_folded(row)));
}
function asks_now(row: RackRow): boolean {
    return row_asks(row, payload.value?.task_groups);
}
const fold_readouts = computed<Map<string, FamilyFoldReadout>>(() => new Map(rows.value.flatMap((row) =>
    row.kind === 'session' && row.node_id && folded.value.has(row.node_id)
        ? [[row.node_id, family_readout(row, rows.value, payload.value?.task_groups, asks_now)] as const] : [])));
// Forget a family's choice once it has been gone a while, read against every session (not the row budget) and
// only from a bridge that answered: an unreachable one lists nothing, which is not every family closing.
const fold_absent = new Map<string, number>();
watch(rows, (list) => {
    if (!payload.value?.reachable || !fold_choices.value.size) return;
    const kept = prune_fold_choices(fold_choices.value, list, Date.now(), fold_absent);
    if (kept) save_fold_choices(kept);
});

/** What the list renders: the live rows, in the held order while renaming. */
const shown_rows = computed<RackRow[]>(() => {
    const attention = attention_rows(rows.value, asks_now);
    const ordered = held_order.value ? hold_row_order(held_order.value, attention) : attention;
    // ★ narrows first, so FIND then searches inside the starred sessions.
    const base = starred_only.value ? starred_rack_rows(ordered) : ordered;
    if (session_search.value.trim()) return search_rack_rows(base, session_search.value, chat_matches.value);
    if (starred_only.value) return base;
    return fold_rows(ordered, folded.value, asks_now)
        .filter((row) => complete_open.value || !row.complete || row.kind === 'cohort');
});
// A suggested next step that reached the screen is reported `shown`, once per hash (§ suggested next step).
watch(shown_rows, report_shown, { immediate: true });
// The box's text goes to the host on every keystroke; the host debounces (`chat-search.ts`). A host
// that comes back without the question (the main window reloaded under a satellite) is asked again,
// once per loss — never per poll.
watch(session_search, (text) => send('search', undefined, text));
watch(() => !!chat_query(session_search.value) && !!payload.value && !payload.value.chat_search,
    (lost) => { if (lost) send('search', undefined, session_search.value); });
watch(
    [complete_delete_key, frozen, () => payload.value?.reachable],
    () => complete_delete.retain(
        !frozen.value && payload.value?.reachable && complete_sessions.value.length
            ? [complete_delete_key.value] : [],
    ),
    { flush: 'sync' },
);

function start_rename(session: string | undefined): void {
    if (frozen.value || !session) return;
    // The right-click accelerator reaches here without the button's `:disabled`.
    if (session === payload.value?.spawning || session === payload.value?.pane_loading) return;
    // `v-if="… && editing !== row.session"` UNMOUNTS the armed ✕ without
    // touching `armed_kill`, so cancelling the rename inside the 4s window would
    // bring back a still-armed button and the next single click would confirm
    // (`engine/terminal/arm-logic.ts` owns the rule). The worktree button sits
    // in the same unmounted strip.
    disarm_kill();
    disarm_worktree();
    editing.value = session;
    held_order.value = attention_rows(rows.value, asks_now).map(rack_row_key);
    pressed_away = false;
    const title = rows.value.find((row) => row.session === session)?.auto_name;
    draft.value = title?.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || session;
    rename_error.value = null;
    void nextTick(() => rename_input.value?.select());
}

function cancel_rename(): void {
    editing.value = null;
    held_order.value = null;
    rename_error.value = null;
}

/** Capture phase, on the document: a press anywhere but the box is the operator
 *  leaving it — the rename box, or an open LAUNCH fork. Mouse too, because a
 *  host that proxies input into the frame synthesizes mouse events. */
function on_document_press(event: Event): void {
    if (editing.value !== null && event.target !== rename_input.value) pressed_away = true;
    // Its own slot is exempt: the chip's click toggles the fork itself.
    if (fork_key.value !== null && !(event.target as Element | null)?.closest?.('.rack__launch-slot--open')) {
        close_fork();
    }
}

function on_rename_focus(): void {
    pressed_away = false;
}

/**
 * Only the operator ends a rename. A blur can also come from the row's node being
 * RE-INSERTED during a render — which the held order prevents, and this
 * forgives if anything else ever does it — so the verdict waits for the render
 * flush, then reads where focus went: another control, another document (the
 * terminal pane included), a press outside the box, or the row gone → cancel,
 * as ever; nowhere (`body`), with no press → put the caret back.
 */
function on_rename_blur(): void {
    const session = editing.value;
    if (!session) return;
    const input = rename_input.value;
    const selectionStart = input?.selectionStart ?? null;
    const selectionEnd = input?.selectionEnd ?? null;
    const selectionDirection = input?.selectionDirection ?? null;
    void nextTick(() => {
        if (editing.value !== session) return;
        const el = rename_input.value;
        const active = document.activeElement;
        const involuntary = !pressed_away && !!el?.isConnected && document.hasFocus()
            && (active === null || active === document.body);
        if (!involuntary || !el) {
            cancel_rename();
            return;
        }
        el.focus({ preventScroll: true });
        el.setSelectionRange(selectionStart, selectionEnd, selectionDirection ?? undefined);
    });
}

/**
 * ONE keydown listener rather than stacked `.enter` / `.esc` modifiers.
 *
 * Two `@keydown` bindings on one element rely on the compiler merging them into
 * a handler array; a single handler with `.stop` cannot be broken that way. The
 * `.stop` itself is the load-bearing part — the row's own `@click`/keyboard
 * path would otherwise re-point the live terminal while the operator is typing.
 */
function on_rename_key(event: KeyboardEvent): void {
    pressed_away = false;
    if (event.key === 'Enter') {
        event.preventDefault();
        commit_rename();
    } else if (event.key === 'Escape') {
        event.preventDefault();
        cancel_rename();
    }
}

function commit_rename(): void {
    const session = editing.value;
    if (!session) return;
    const name = draft.value.trim();
    if (!name || (name === session && !rows.value.some((row) => row.session === session && row.auto_name))) {
        cancel_rename();
        return;
    }
    // Validated here as well as on the bridge (which re-checks the shape and its own
    // plugins' prefixes): catching it locally means no round trip and the input stays
    // open on the bad name instead of collapsing back to the old one.
    const refused = session_name_error(name, session);
    if (refused) {
        rename_error.value = refused;
        return;
    }
    send('rename', session, name);
    cancel_rename();
}

// ── render options (font size, family, theme, xterm renderer) ────────────────
//
// Emitted, not sent down the transport, because the surfaces apply them in different places: a
// page that owns its terminal frame applies them itself, and a framed rack routes them home to
// the host that owns the terminal. One `update:` event lets each mount wire whichever it is
// (`mount.ts`) and keeps `RackTransport` unchanged.
//
// DELIBERATELY NOT gated on `frozen`. Frozen means nothing services the rack's actions, but a
// page's own terminal frame talks straight to ttyd and keeps working, so the font control must
// too. Only actions that need someone on the other end go through `send()`.

const render_opts = computed<TerminalRenderOptions>(() => props.render_options ?? DEFAULT_RENDER_OPTIONS);

const font_min = MIN_TERMINAL_FONT_SIZE;
const font_max = MAX_TERMINAL_FONT_SIZE;
const renderers = TERMINAL_RENDERERS;

/** The RENDER menu's text. The value stays the raw id ttyd's URL takes. */
function renderer_label(name: string): string {
    return name === 'webgl' ? 'webgl (GPU)' : name;
}
const families = TERMINAL_FONT_FAMILIES;

/** The `<select>` binds to the STACK, not the label, so a stored value the menu
 *  doesn't offer (an older default, or one hand-set in localStorage) leaves the
 *  control blank rather than silently claiming to be the default. */
const family_stack = computed<string>(() => render_opts.value.font_family);

/** Values are `unknown` on purpose: DOM inputs hand back strings, and
 *  `clamp_render_options` already coerces field by field — correctly, unlike a
 *  bare `Number()`, which turns `''` into 0 and would clamp to the MINIMUM font
 *  size rather than falling back to the default. Keys stay checked. */
function patch_render(patch: Partial<Record<keyof TerminalRenderOptions, unknown>>): void {
    emit('update:render_options', clamp_render_options({ ...render_opts.value, ...patch }));
}

function step_font(delta: number): void {
    patch_render({ font_size: render_opts.value.font_size + delta * TERMINAL_FONT_STEP_PX });
}

function set_font(value: string): void {
    patch_render({ font_size: value });
}

function set_renderer(value: string): void {
    patch_render({ renderer: value });
}

function set_family(value: string): void {
    patch_render({ font_family: value });
}

const themes = TERMINAL_THEME_IDS;

function set_theme(value: string): void {
    patch_render({ theme: value });
}

let unsubscribe: (() => void) | null = null;
let metric_timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
    metric_timer = setInterval(metric_recorder.tick, 15_000);
    document.addEventListener('visibilitychange', metric_recorder.tick);
    window.addEventListener('focus', metric_recorder.tick);
    window.addEventListener('blur', metric_recorder.tick);
    window.addEventListener('pagehide', metric_recorder.stop);
    document.addEventListener('pointerdown', on_document_press, true);
    document.addEventListener('mousedown', on_document_press, true);
    window.addEventListener('storage', on_storage);
    unsubscribe = props.transport.subscribe((next) => {
        const focus_changed = payload.value?.focused !== next.focused;
        if (focus_changed) metric_recorder.stop();
        payload.value = next;
        if (focus_changed) metric_recorder.tick();
        now_s.value = Date.now() / 1000;
        office_relay.push(next);
        // A rack update can retire the armed row out from under the arm (the
        // session died, or someone else killed it). An armed state that outlives
        // its button turns the NEXT single click into a confirmed one.
        if (armed_kill.value && !rows.value.some((row) => row.session === armed_kill.value)) {
            disarm_kill();
        }
        // The ⚠️ menu, by the same rule: its ⚠️ left (a new turn, parked elsewhere, the row died).
        if (open_work_menu.value
            && !rows.value.some((row) => row.session === open_work_menu.value && shows_open_work(row))) {
            close_open_work_menu();
        }
        // The worktree button's arm, by the same rule: its row left, or the button
        // became the other kind (a PR whose branch just landed is CLEAN now).
        if (armed_worktree.value && !worktree_armed(armed_worktree.value.session)) disarm_worktree();
        // Same for an open rename whose session left the rack: its box is gone,
        // and a blur on removal is not something every browser fires.
        if (editing.value && !rows.value.some((row) => row.kind === 'session' && row.session === editing.value)) {
            cancel_rename();
        }
    });
});
onUnmounted(() => {
    close_open_work_menu();
    metric_recorder.stop();
    if (metric_timer !== null) clearInterval(metric_timer);
    document.removeEventListener('visibilitychange', metric_recorder.tick);
    window.removeEventListener('focus', metric_recorder.tick);
    window.removeEventListener('blur', metric_recorder.tick);
    window.removeEventListener('pagehide', metric_recorder.stop);
    document.removeEventListener('pointerdown', on_document_press, true);
    document.removeEventListener('mousedown', on_document_press, true);
    window.removeEventListener('storage', on_storage);
    unsubscribe?.();
    unsubscribe = null;
    office_relay.dispose();
    disarm_kill();
    disarm_worktree();
    clear_reset_timer();
});
</script>

<template>
    <div class="rack fui-panel">
        <div class="rack__body">
        <header class="rack__head">
            <span class="fui-techtext rack__title">Rack <span class="rack__session-count">{{ payload?.sessions.filter(session => session.live).length ?? 0 }} sessions</span></span>
            <button type="button" class="fui-btn rack__icon"
                :aria-pressed="settings_open" :aria-expanded="settings_open" aria-controls="rack-settings"
                aria-label="Rack settings"
                :title="settings_open
                    ? 'Hide rack settings'
                    : 'Rack settings: directories, view, terminal appearance and help'"
                @click="settings_open = !settings_open"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiCog" /></svg></button>
            <button v-if="closed_view" type="button"
                :class="['fui-btn', { 'rack__restore-btn': (closed_view.restorable?.count ?? 0) > 0 }]"
                :aria-pressed="closed_view.open" :aria-expanded="closed_view.open" :disabled="frozen"
                :title="closed_button_title"
                @click="send('closed')">{{ closed_button_label }}</button>
        </header>

        <section v-if="payload?.reachable" class="rack__attention" aria-label="Rack attention summary">
            <strong><span>{{ attention_count }}</span> {{ attention_count === 1 ? 'session needs you' : 'sessions need you' }}</strong>
            <p>{{ working_count }} working · {{ complete_sessions.length }} completed</p>
        </section>

        <!-- Terminal render controls, behind the head's ⚙ (closed by default since
             2026-11-02: set once, rarely touched). ⌘/Ctrl ± stays
             the accelerator, hinted in the ⚙ and button titles. Never disabled
             by `frozen` — see the note on `patch_render`. -->
        <section v-show="settings_open" id="rack-settings" class="rack__settings">
            <button type="button" class="fui-btn" @click="launch_open = true; directories_open = true; settings_open = false">Edit launch directories</button>
            <div class="rack__render">
                <label class="fui-techtext rack__render-label" for="rack-view">VIEW</label>
                <select id="rack-view" v-model="session_view" class="rack__render-select">
                    <option value="list">LIST</option><option value="graph">GRAPH</option>
                </select>
                <button type="button" class="fui-btn rack__icon" aria-label="Agent office"
                    title="Agent office — every agent at its desk, in 3D (opens a window; click a robot to open it here)"
                    @click="open_office"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiOfficeBuildingOutline" /></svg></button>
                <button type="button" class="fui-btn" title="How the agent rack works — opens a diagram"
                    @click="open_diagram">⌗ HOW</button>
            </div>
            <div v-if="render_options" class="rack__render">
                <span class="fui-techtext rack__render-label">FONT</span>
                <button
                    class="rack__render-btn"
                    type="button"
                    title="Smaller (⌘/Ctrl −)"
                    @click="step_font(-1)"
                >
                    A−
                </button>
                <input
                    class="rack__render-slider"
                    type="range"
                    :min="font_min"
                    :max="font_max"
                    step="1"
                    :value="render_opts.font_size"
                    title="Terminal font size"
                    aria-label="Terminal font size"
                    @input="set_font(($event.target as HTMLInputElement).value)"
                />
                <button
                    class="rack__render-btn"
                    type="button"
                    title="Larger (⌘/Ctrl +)"
                    @click="step_font(1)"
                >
                    A+
                </button>
                <span class="rack__render-readout">{{ render_opts.font_size }}px</span>

                <span class="fui-techtext rack__render-label rack__render-label--gap">RENDER</span>
                <select
                    class="rack__render-select"
                    :value="render_opts.renderer"
                    title="xterm renderer. Switching RELOADS the pane — it reattaches to the same tmux session, so nothing is lost. dom uses the browser's own text rendering; webgl is GPU-drawn and the fastest under heavy output. No WebGPU option: ttyd's bundled xterm has no such renderer."
                    aria-label="Terminal renderer"
                    @change="set_renderer(($event.target as HTMLSelectElement).value)"
                >
                    <option v-for="name in renderers" :key="name" :value="name">{{ renderer_label(name) }}</option>
                </select>

                <span class="fui-techtext rack__render-label rack__render-label--gap">FAMILY</span>
                <select
                    class="rack__render-select"
                    :value="family_stack"
                    title="Terminal typeface. SF Mono is the default. Switching RELOADS the pane — it reattaches to the same tmux session, so scrollback and any running agent survive."
                    aria-label="Terminal font family"
                    @change="set_family(($event.target as HTMLSelectElement).value)"
                >
                    <option v-for="font in families" :key="font.stack" :value="font.stack">
                        {{ font.label }}
                    </option>
                </select>

                <span class="fui-techtext rack__render-label rack__render-label--gap">THEME</span>
                <select
                    class="rack__render-select"
                    :value="render_opts.theme"
                    title="Terminal colours. Switching RELOADS the pane: it reattaches to the same tmux session, so scrollback and any running agent survive."
                    aria-label="Terminal colour theme"
                    @change="set_theme(($event.target as HTMLSelectElement).value)"
                >
                    <option v-for="name in themes" :key="name" :value="name">{{ name.toUpperCase() }}</option>
                </select>
            </div>
            <!-- Keep host controls mounted while Settings is closed. -->
            <slot name="settings" :open="settings_open" />
        </section>

        <!-- ↺ CLOSED: one press per row reopens it (`POST /reopen`) and focuses it.
             Rows the bridge cannot reopen stay, disabled, their reason in the title. -->
        <section v-if="closed_view?.open" class="rack__closed" aria-label="Sessions that can come back">
            <!-- AFTER REBOOT: what was running when the machine went down. A reboot is never a close,
                 so these are their own band, first, with RESTORE ALL (armed: it starts every agent). -->
            <template v-if="restorable_view && (restorable_view.rows === null || restorable_rows.length > 0 || restorable_view.restoring)">
                <div class="rack__closed-head">
                    <span class="fui-techtext">AFTER REBOOT</span>
                    <button type="button"
                        :class="['fui-btn', 'rack__restore-all', { 'row__btn--armed': restorable_all_armed_now }]"
                        :disabled="frozen || closed_busy || restorable_ok === 0"
                        :title="`Restore all ${restorable_ok} — each resumes its conversation where it can, on its own account`"
                        :aria-label="`Restore all ${restorable_ok} sessions${restorable_all_armed_now ? ' — confirm' : ''}`"
                        @click="press_restore_all">{{ restorable_view.restoring === '*' ? 'RESTORING…' : restorable_all_armed_now ? `RESTORE ${restorable_ok}?` : 'RESTORE ALL' }}</button>
                    <button type="button" class="fui-btn rack__icon"
                        :disabled="frozen || closed_busy || restorable_rows.length === 0"
                        title="Stop offering these — the records are dropped, nothing is started"
                        aria-label="Dismiss every restore"
                        @click="send('restore_forget')">✕</button>
                </div>
                <p v-if="restorable_view.rows === null" class="rack__closed-note fui-techtext">reading…</p>
                <ul v-else class="rack__closed-list">
                    <li v-for="row in restorable_rows" :key="row.session">
                        <button
                            type="button"
                            :class="['closed', { 'closed--busy': restorable_view.restoring === row.session }]"
                            :disabled="frozen || !row.ok || closed_busy"
                            :title="row.title"
                            :aria-label="row.title"
                            @click="send('restore', undefined, row.session)"
                        >
                            <span class="closed__glyph" aria-hidden="true">{{ restorable_view.restoring === row.session ? '…' : row.resumable ? '↺' : '+' }}</span>
                            <span class="closed__name">{{ row.name }}</span>
                            <span v-if="row.place" class="closed__place">{{ row.place }}</span>
                            <span class="closed__ago">{{ row.ago }}</span>
                        </button>
                    </li>
                </ul>
                <div v-if="closed_view.count > 0" class="rack__closed-head"><span class="fui-techtext">CLOSED</span></div>
            </template>
            <template v-if="closed_view.count > 0 || !restorable_view">
            <p v-if="closed_view.rows === null" class="rack__closed-note fui-techtext">reading…</p>
            <p v-else-if="closed_rows.length === 0" class="rack__closed-note fui-techtext">nothing to reopen</p>
            <ul v-else class="rack__closed-list">
                <li v-for="row in closed_rows" :key="row.id">
                    <button
                        type="button"
                        :class="['closed', { 'closed--busy': closed_view.reopening === row.id }]"
                        :disabled="frozen || !row.ok || closed_busy"
                        :title="row.title"
                        :aria-label="row.title"
                        @click="send('reopen', undefined, row.id)"
                    >
                        <span class="closed__glyph" aria-hidden="true">{{ closed_view.reopening === row.id ? '…' : row.resumable ? '↺' : '+' }}</span>
                        <span class="closed__name"><svg v-if="row.starred" class="closed__star" viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiStar" /></svg>{{ row.name }}</span>
                        <span v-if="row.place" class="closed__place">{{ row.place }}</span>
                        <span class="closed__ago">{{ row.ago }}</span>
                    </button>
                </li>
            </ul>
            </template>
        </section>

        <!-- LAUNCH: the rack's one way to start an agent. A chip forks into
             MAIN / 🏝 under it — the why, and the fallbacks, are owned by the
             `launch` block in the script. Mono, no hue: hue means STATE on this
             page, and a profile is identity, not state. Each Claude chip reads
             its account's WEEKLY % (number) and 5-hour window (bar); a capped
             account is struck through. AUTO, first where the bridge can pick,
             reads where it would land (`→ A`); while its fork is open that
             chip is outlined dashed. -->
        <section class="rack__create" aria-label="New session">
        <button type="button" class="rack__create-toggle" :aria-expanded="launch_open" aria-controls="rack-launch-options"
            :disabled="frozen" @click="toggle_launch"><span>+ New session</span><span aria-hidden="true">{{ launch_open ? '▾' : '▸' }}</span></button>
        <div v-show="launch_open" id="rack-launch-options" class="rack__launch-options">
        <div class="rack__section-head"><span>Launch directory</span><button type="button" class="fui-btn" :aria-expanded="directories_open" @click="directories_open = !directories_open">Edit directories</button></div>
        <LaunchDirectorySettings v-if="directories_open" :view="payload?.launch_directories" :disabled="frozen || !payload?.reachable"
            @save="(value) => send('directories', undefined, value)" />
        <div v-if="directory_state" class="rack__projects" role="group" aria-label="Directory for new agents">
            <button v-for="directory in directory_state.directories" :key="directory.path" type="button"
                class="fui-btn rack__project" :aria-pressed="launch_directory?.path === directory.path"
                :title="`${directory.path}${directory.path === directory_state.default_path ? ' · default' : ''}`"
                :disabled="frozen" @click="choose_directory(directory.path)"><span>{{ directory.label }}</span><small>{{ directory.path }}</small></button>
        </div>
        <p v-if="directory_state && !payload?.launch_cwd" class="rack__project-note">Reload the parent app to launch in a selected directory.</p>
        <div class="rack__section-head"><span>Launch profile</span><button type="button" class="fui-btn" :aria-expanded="profiles_open" @click="profiles_open = !profiles_open">Manage profiles</button></div>
        <section v-if="profiles_open" class="rack__profiles" aria-label="Launch profiles">
            <p>Pause to hide a profile from Launch and exclude it from Auto. Existing sessions stay available.</p>
            <div v-for="profile in profiles" :key="profile.id" class="rack__profile-setting">
                <span>{{ profile.label }} <small>{{ profile_paused(profile) ? 'Paused' : profile.kind }}</small></span>
                <button type="button" class="fui-btn" :disabled="frozen || !payload?.reachable || !payload?.profile_availability?.state || payload.profile_availability.saving"
                    :aria-label="`${profile_paused(profile) ? 'Resume' : 'Pause'} ${profile.label}`" @click="toggle_profile(profile)">{{ profile_paused(profile) ? 'Resume' : 'Pause' }}</button>
            </div>
            <p v-if="payload?.profile_availability?.error || payload?.profile_availability?.state?.error" role="alert">{{ payload.profile_availability.error || payload.profile_availability.state?.error }}</p>
            <p v-else-if="!payload?.profile_availability?.state">Reading profile availability…</p>
            <p v-else-if="payload.profile_availability.saved" role="status">Saved</p>
        </section>
        <p v-if="profiles.length && !available_profiles.length" class="rack__project-note">All profiles are paused. Resume one in Manage profiles.</p>
        <div class="rack__launch" role="group" aria-label="Launch an agent">

            <span
                v-for="chip in launch_chips"
                :key="chip.key"
                :class="['rack__launch-slot', { 'rack__launch-slot--open': fork_key === chip.key }]"
                @keydown.esc="on_fork_esc"
            >
                <button
                    type="button"
                    :class="['fui-tag', 'rack__chip', {
                        'rack__chip--new': chip.kind === 'new',
                        'rack__chip--on': chip_on(chip),
                        'rack__chip--spent': chip.profile !== null && is_spent(chip.profile),
                        'rack__chip--auto-target': is_auto_target(chip),
                    }]"
                    :disabled="frozen || !chip_launchable(chip)"
                    :aria-expanded="chip_forks(chip) ? fork_key === chip.key : undefined"
                    :title="chip_title(chip)"
                    @click="on_chip(chip, $event)"
                >
                    <template v-if="chip.profile">
                        <span class="rack__chip-head">
                            <span class="rack__chip-label">{{ chip.profile.label }}<span v-if="chip.profile.hooks_ok === false" class="rack__chip-warn" aria-hidden="true">!</span></span>
                            <span
                                v-if="is_out(chip.profile)"
                                class="rack__chip-week rack__chip-week--out"
                                :aria-label="`${chip.profile.label} is out: ${standing_reason(chip.profile.standing!)}`"
                            >OUT</span>
                            <span
                                v-else-if="weekly(chip.profile) !== null"
                                :class="['rack__chip-week', { 'rack__chip-week--warn': weekly(chip.profile)! >= USAGE_WARN_PCT }]"
                                :aria-label="`${chip.profile.label} weekly usage ${Math.round(weekly(chip.profile)!)}%`"
                            >{{ Math.round(weekly(chip.profile)!) }}%</span>
                        </span>
                        <span
                            v-if="!is_out(chip.profile) && five_hour(chip.profile) !== null"
                            :class="['fui-meter', 'rack__chip-meter', { 'fui-meter--warn': five_hour(chip.profile)! >= USAGE_WARN_PCT }]"
                            role="meter"
                            :aria-label="`${chip.profile.label} 5-hour usage`"
                            :aria-valuenow="Math.round(five_hour(chip.profile)!)"
                            aria-valuemin="0"
                            aria-valuemax="100"
                        >
                            <i class="fui-meter__fill" :style="{ width: `${five_hour(chip.profile)}%` }" />
                        </span>
                    </template>
                    <template v-else-if="chip.kind === 'auto'">
                        <span class="rack__chip-head"><span class="rack__chip-label">AUTO</span></span>
                        <span
                            :class="['rack__chip-target', { 'rack__chip-week--warn': auto_pick?.spent }]"
                            :aria-label="auto_pick?.id ? `AUTO would launch ${auto_pick.label}` : 'AUTO has nothing to launch'"
                        >→ {{ auto_pick?.label ?? '—' }}</span>
                    </template>
                    <template v-else>+ NEW</template>
                </button>
                <!-- The fork: WHERE the agent works. 🏝 is the row's own island
                     drawing (`IslandGlyph.vue`) — what you press is what the
                     row then wears. -->
                <span v-if="fork_key === chip.key" class="rack__fork">
                    <button
                        type="button"
                        class="rack__fork-btn rack__fork-main"
                        :disabled="frozen"
                        :title="main_title(chip)"
                        @click="launch_main(chip)"
                    >{{ launch_directory ? 'HERE' : 'MAIN' }}</button>
                    <button
                        v-if="worktrees_available"
                        type="button"
                        class="rack__fork-btn rack__fork-island"
                        :disabled="frozen || !repository_launch"
                        :title="island_title(chip)"
                        :aria-label="island_title(chip)"
                        @click="launch_island(chip)"
                    ><IslandGlyph /></button>
                    <button
                        v-if="chip_offers_lead(chip)"
                        type="button"
                        class="rack__fork-btn"
                        :disabled="frozen || !repository_launch"
                        :title="lead_title(chip)"
                        @click="launch_lead(chip)"
                    >LEAD</button>
                </span>
            </span>
        </div>

        <p v-if="!repository_launch && fork_key !== null" class="rack__project-note">{{ repository_hint }}</p>
        </div>
        </section>

        <!-- A launch that did not go as asked. Expires on its own; the rack
             keeps working around it. -->
        <div v-if="session_view === 'list'" class="rack__search">
            <input v-model="session_search" type="search" aria-label="Find a session by name or by what was said"
                placeholder="Find a session — name or chat…" autocomplete="off" spellcheck="false"
                @keydown.esc.stop="session_search = ''" />
            <!-- ★ N: only the starred sessions. Drawn only while something is starred. -->
            <button v-if="starred_total" type="button"
                :class="['fui-btn', 'rack__icon', 'rack__star-filter', { 'rack__star-filter--on': starred_only }]"
                :aria-pressed="starred_only"
                :title="star_filter_title(starred_total, starred_only)"
                :aria-label="star_filter_title(starred_total, starred_only)"
                @click="starred_only = !starred_only"
            ><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiStar" /></svg>{{ starred_total }}</button>
            <span v-if="session_search.trim()" class="fui-techtext" role="status" :title="chat_status_title">
                {{ shown_rows.filter(row => row.kind === 'session').length }} shown<template
                    v-if="chat_state === 'pending'"> · chat…</template><template
                    v-else-if="chat_state === 'error'"> · chat ✗</template>
            </span>
        </div>

        <p v-if="payload?.launch_notice" class="fui-callout fui-callout--warn rack__notice">
            {{ payload.launch_notice }}
        </p>


        <!-- Frozen: the main window is not servicing the rack, so the poller that
             fills this list has stopped. Saying so (and disabling the buttons)
             beats silently swallowing a KILL click. -->
        <p v-if="frozen" class="fui-callout fui-callout--warn rack__frozen">
            the main window is not servicing the rack: list frozen
        </p>

        <p v-if="!payload" class="rack__empty fui-techtext">connecting to the bridge…</p>

        <!-- The gate outranks `reachable`: a shut gate rejects before any
             network I/O, so "bridge not running" would be a claim about a
             machine nothing has spoken to. See `access_view`. -->
        <div
            v-else-if="access_view"
            class="fui-callout fui-callout--warn rack__access"
            :title="access_view.why ?? undefined"
        >
            <span class="fui-techtext rack__access-head">{{ access_view.head }}</span>
            <span v-if="access_view.detail" class="rack__access-detail">{{ access_view.detail }}</span>
            <button
                v-if="access_view.retry"
                type="button"
                class="rack__new rack__access-btn"
                :disabled="frozen"
                @click="send('connect')"
            >
                {{ access_view.retry }}
            </button>
        </div>

        <p v-else-if="!payload.reachable" class="rack__empty fui-techtext rack__empty--warn">
            bridge not running
        </p>

        <AgentSessionGraph v-else-if="session_view === 'graph'" :sessions="payload.sessions"
            :focused="payload.focused" :disabled="frozen" :groups="payload.task_groups"
            @control="(id, action) => send('group_control', id, action)" @select="send('select', $event)" />

        <!-- THE LEDGER GRID. The list is the grid and every session row a
             subgrid, so the trailing CTX and PROFILE columns are shared by every
             row: a value there sits at one x however much the row carries before
             it. The actions live INSIDE the name cell for the same reason: beside
             the profile chip and hidden by opacity they would still take width,
             and the chip would move with each row's button count. -->
        <ul
            v-else
            :class="['rack__list', { 'rack__list--emoji': use_state_emoji, 'rack__list--profiles': show_who_column }]"
            :style="{ '--rack-profile-chars': profile_column_chars }"
        >
            <!-- Keyed by identity, held in order while renaming: owned by
                 `multi-terminal-layout.ts` `hold_row_order`. -->
            <li
                v-for="row in shown_rows"
                :key="rack_row_key(row)"
                :class="['row', `row--${row.kind}`, { 'row--focused': row.focused, 'row--dim': row.dim, 'row--frozen': frozen, 'row--child': (row.depth ?? 0) > 0, 'row--family': (row.child_count ?? 0) > 0 }]"
                :style="family_depth_style(row)"
                @click="row.kind === 'session' && !row.dim && row.session && editing !== row.session ? send('select', row.session) : null"
                @contextmenu="row.kind === 'session' && !row.dim && row.session
                    ? ($event.preventDefault(), start_rename(row.session))
                    : null"
            >
                <template v-if="row.kind === 'session'">
                    <span class="mark">
                        <span
                            :class="dot_class(row)"
                            :style="dot_style(row)"
                            :title="mark_title(row)"
                            :aria-label="mark_title(row)"
                            role="img"
                        >{{ dot_glyph(row) }}</span>
                        <!-- Laya's verdict: the turn ended, planned work did not.
                             `.stop`, or the click also selects the row. -->
                        <button
                            v-if="row.session && shows_open_work(row)"
                            type="button"
                            class="mark__open-work"
                            :title="open_work_badge_title(row.open_work?.p, row.open_work?.graded)"
                            :aria-label="open_work_badge_title(row.open_work?.p, row.open_work?.graded)"
                            :aria-expanded="open_work_menu === row.session"
                            :disabled="frozen"
                            @click.stop="toggle_open_work_menu(row)"
                        >{{ OPEN_WORK_BADGE }}</button>
                        <!-- Its children still work: ⏳ in place of ⚠️ (attention.py § 1). -->
                        <span
                            v-else-if="row.session && shows_open_work_waiting(row)"
                            class="mark__open-work"
                            role="img"
                            :title="open_work_children_title(row.open_work?.children ?? 0)"
                            :aria-label="open_work_children_title(row.open_work?.children ?? 0)"
                        >{{ OPEN_WORK_WAITING_BADGE }}</span>
                        <span
                            v-if="row.stuck && shows_stuck(row)"
                            class="mark__stuck"
                            role="img"
                            :title="stuck_title(row.stuck)"
                            :aria-label="stuck_title(row.stuck)"
                        >{{ STUCK_BADGE }}</span>
                        <span v-if="row.session && open_work_menu === row.session" class="mark__answers" @click.stop>
                            <button type="button" class="fui-btn rack__icon mark__answer"
                                title="Seen, not now — its repeats stay quiet until you next prompt this session"
                                aria-label="Seen, not now"
                                @click.stop="answer_open_work(row, 'snooze_open_work')"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiPause" /></svg></button>
                            <button type="button" class="fui-btn rack__icon mark__answer"
                                :title="row.open_work?.graded
                                    ? 'Not open — clears the ⚠️; the grade this turn already has stands (change it on RECENT)'
                                    : 'Not open — Laya was wrong; clears the ⚠️ and grades it DONE'"
                                aria-label="Not open, Laya was wrong"
                                @click.stop="answer_open_work(row, 'dismiss_open_work')"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiClose" /></svg></button>
                            <button v-if="!row.open_work?.graded" type="button" class="fui-btn rack__icon mark__answer"
                                title="Still open — Laya was right; keeps the ⚠️ and grades it OPEN"
                                aria-label="Still open, Laya was right"
                                @click.stop="answer_open_work(row, 'confirm_open_work')"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiCheck" /></svg></button>
                        </span>
                    </span>

                    <span class="row__main">
                        <!-- Family depth indents the row's LEADING part, never the row: a
                             padded row would shift its trailing columns too. The mark steps
                             right `--rack-depth-mark-step` a level, by `translate` — the mark
                             track is `auto` and shared through the subgrid, so a margin on one
                             child's mark would widen it for every row and move the lead's mark
                             with it. This spacer clears that step plus `--rack-depth-name-step`,
                             so a name keeps its family indent past its own mark. -->
                        <span v-if="row.depth" class="row__indent" aria-hidden="true" />
                        <!-- The fold: one glyph button; its count lives in the folded line (`FamilyFoldLine.vue`).
                             ← folds and → opens while it has focus, as in a tree. -->
                        <button v-if="row.node_id && row.child_count" type="button"
                            :class="['fui-btn', 'rack__icon', 'row__family-toggle', { 'row__family-toggle--folded': is_folded(row) }]"
                            :aria-expanded="!is_folded(row)"
                            :title="`${fold_word(row)} (${is_folded(row) ? '→' : '←'})`"
                            :aria-label="`${fold_word(row)}: ${row.auto_name || row.session}`"
                            aria-keyshortcuts="ArrowLeft ArrowRight"
                            @click.stop="toggle_family(row)"
                            @keydown.left.stop.prevent="!is_folded(row) && toggle_family(row)"
                            @keydown.right.stop.prevent="is_folded(row) && toggle_family(row)"
                        ><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="is_folded(row) ? mdiUnfoldMoreHorizontal : mdiUnfoldLessHorizontal" /></svg><kbd
                            class="fui-key" aria-hidden="true">{{ is_folded(row) ? '→' : '←' }}</kbd></button>
                        <span v-if="row.child_count && row.attention_count && !is_folded(row)" class="fui-tag"
                            :title="'Sessions in this family waiting for you'">{{ row.attention_count }} need you</span>
                        <span v-if="row.missing_parent" class="fui-tag">parent closed</span>

                        <!-- Inline rename. `.stop` on click and keydown is load-bearing:
                             without it, typing or clicking in the field bubbles to the
                             row's @click and re-points the live terminal mid-edit.
                             A FUNCTION ref, not `ref="…"` — a string ref inside v-for
                             collects into an array, so `.select()` would be undefined. -->
                        <input
                            v-if="editing === row.session"
                            :ref="(el) => { rename_input = el as HTMLInputElement | null; }"
                            v-model="draft"
                            class="row__rename"
                            :class="{ 'row__rename--bad': rename_error }"
                            spellcheck="false"
                            autocomplete="off"
                            :aria-label="`Rename session ${row.session}`"
                            @click.stop
                            @keydown.stop="on_rename_key"
                            @focus="on_rename_focus"
                            @blur="on_rename_blur"
                            @input="rename_error = null"
                        />
                        <!-- The name is the rename door: `.stop` keeps the click
                             from also selecting the row. -->
                        <template v-else>
                            <span v-if="row.focused" class="row__mark" aria-hidden="true">▶</span>
                            <button
                                v-if="can_rename(row)"
                                type="button"
                                class="row__name"
                                :class="{ 'row__name--locked': name_locked(row) }"
                                :disabled="frozen || name_locked(row)"
                                :title="name_title(row)"
                                :aria-label="name_locked(row) ? name_title(row) : `Rename session ${row.session}`"
                                @click.stop="start_rename(row.session)"
                            >{{ row.auto_name || row.session }}</button>
                            <span v-else class="row__name row__name--static">{{ row.auto_name || row.session }}</span>
                            <!-- The operator's star, beside the name it marks. Solid and always
                                 drawn once set; an outline on hover or the focused row before.
                                 `.stop`, or the click also selects the row. -->
                            <button
                                v-if="shows_star(row)"
                                type="button"
                                :class="['row__star', 'rack__icon', { 'row__star--on': row.starred }]"
                                :disabled="frozen"
                                :aria-pressed="row.starred === true"
                                :title="star_title(row.starred === true)"
                                :aria-label="star_title(row.starred === true)"
                                @click.stop="toggle_star(row)"
                            ><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="row.starred ? mdiStar : mdiStarOutline" /></svg></button>
                            <!-- Beside the NAME, not out by the actions: the point is
                                 to read "this agent is not in your checkout" with the
                                 name it belongs to. Owner: `IslandMark.vue`. -->
                            <IslandMark v-if="row.island" :island="row.island" />
                            <span v-if="mission_title(row.session)" class="row__mission"
                                :title="mission_title(row.session) ?? undefined"
                                :aria-label="mission_title(row.session) ?? undefined">◆</span>
                            <span v-if="detail_segments(row).length" class="row__text">
                                <!-- The unusual model, inline ONLY while the rack is
                                     too narrow for the MODEL readout (`.row__model`). -->
                                <span v-if="role_segment(row, 'model')" class="row__model-inline">{{ role_segment(row, 'model')!.text }}<template v-if="detail_segments(row).length">{{ ' · ' }}</template></span>
                                <template v-for="(segment, s) in detail_segments(row)" :key="s">
                                    <span v-if="s > 0">{{ ' · ' }}</span>
                                    <span
                                        v-if="segment.tone"
                                        :class="['row__ctx', `row__ctx--${segment.tone}`]"
                                        :title="segment.title"
                                    >{{ segment.text }}</span>
                                    <span v-else-if="segment.title" :title="segment.title">{{ segment.text }}</span>
                                    <template v-else>{{ segment.text }}</template>
                                </template>
                            </span>
                        </template>

                        <small class="row__status">{{ row.dim ? 'Closed' : asks_now(row) ? 'Needs you' : row.complete ? 'Completed' : row.state === 'busy' || row.state === 'background' ? 'Working' : state_label(row.state ?? 'unknown') }}</small>
                        <span v-if="row.actions.length && editing !== row.session" class="row__actions">
                            <!-- PR / CLEAN. Deliberately NOT a `RackActionId`: that union
                                 is the per-row action set, where `kill` must stay last
                                 (pinned by a test). Only this surface needs it.
                                 Label, title and press: `rack-view.ts::worktree_button_view`. -->
                            <button
                                v-if="row.session && worktree_buttons.get(row.session)
                                    && !create_view(row.session)"
                                :class="['row__btn', 'row__btn--pr', {
                                    'row__btn--armed': worktree_armed(row.session),
                                    'row__btn--pr-open': worktree_buttons.get(row.session)!.tone === 'open',
                                    'row__btn--danger': worktree_buttons.get(row.session)!.tone === 'error',
                                }]"
                                :disabled="frozen || worktree_buttons.get(row.session)!.press === 'none'"
                                :title="worktree_buttons.get(row.session)!.title"
                                @click.stop="on_worktree_click(row.session)"
                            >{{ worktree_buttons.get(row.session)!.label }}</button>
                            <template v-for="action in visible_actions(row)" :key="action">
                                <!-- LABEL, beside the ✕: grade this session's marks and its name on the REVIEW page.
                                     Not a `RackActionId` for the PR button's reason above. -->
                                <button
                                    v-if="action === 'kill' && can_label(row)"
                                    type="button"
                                    class="row__btn row__btn--label rack__icon"
                                    :title="label_title(row)"
                                    :aria-label="label_title(row)"
                                    @click.stop="open_label(row)"
                                ><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="mdiThumbsUpDown" /></svg></button>
                                <button
                                    :class="['row__btn', {
                                        'row__btn--danger': action === 'kill',
                                        'row__btn--armed': action === 'kill' && armed_kill === row.session,
                                    }]"
                                    :disabled="frozen"
                                    :title="action_title(action, row)"
                                    @click.stop="action === 'kill' ? on_kill_click(row) : send('select', row.session)"
                                >
                                    <template v-if="action === 'kill'">
                                        {{ armed_kill === row.session ? 'KILL?' : '✕' }}
                                    </template>
                                    <template v-else>▶</template>
                                </button>
                            </template>
                        </span>
                    </span>

                    <!-- CTX: context left, the number only — the column says what
                         it is, the title says it in words. Amber ≤25, red ≤10. -->
                    <span
                        v-if="role_segment(row, 'context')"
                        :class="['row__ctx-col', `row__ctx--${role_segment(row, 'context')!.tone}`]"
                        :title="`${role_segment(row, 'context')!.text.replace(' left', '')} of the context window left`"
                    >{{ role_segment(row, 'context')!.text.replace(' left', '') }}</span>
                    <span v-else class="row__ctx-col" />

                    <!-- PROFILE: every row, so the column reads top to bottom;
                         `—` is "no account on record", never a blank. -->
                    <span v-if="show_who_column" class="row__who">
                        <!-- MODEL: every live row, when the rack is wide enough
                             (the container query on `.row__model`). -->
                        <span v-if="row_model(row)" class="row__model" :title="`Running ${row_model(row)}`">{{ row_model(row) }}</span>
                        <span
                            v-if="show_profile_column"
                            :class="['row__profile', { 'row__profile--none': !row_profile_label(row) }]"
                            :title="row_profile_label(row) ? `Launched as ${row_profile_label(row)}` : 'Launched outside the rack — account unknown'"
                        >{{ row_profile_label(row) ?? '—' }}</span>
                    </span>

                    <!-- THE SECOND LINE: one slot under the name for whatever the
                         row has to say at length — the rename error, the attention
                         message, the cut-off reason, or create progress. A long
                         string squeezed into the row strip would ellipsize the
                         name exactly when you most want to read it. -->
                    <span v-if="editing === row.session && rename_error" class="row__line2 row__rename-error">{{ rename_error }}</span>
                    <!-- A search found this row by what was SAID in it: the line that matched, its
                         terms marked. Ahead of the rest while the box has text, because it is the
                         one thing that says why this row is here. Text nodes and marks only. -->
                    <span v-else-if="chat_hit(row)" class="row__line2 row__said" :title="chat_hit_title(chat_hit(row)!)">
                        <template v-for="(part, i) in highlight_terms(focus_snippet(chat_hit(row)!.snippet, session_search), session_search)"
                            :key="i"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template>
                    </span>
                    <template v-else-if="create_view(row.session)">
                        <!-- Create progress. A determinate `.fui-meter`, because the
                             step count is known — a ratio against a limit is what that
                             primitive is for, and it replaces the progress SENTENCE
                             rather than sitting beside one.
                             The marching fill is a LOOP, and that is the one
                             exception to "nothing idles": a frozen spinner
                             communicates nothing (WCAG 2.2.2). It stops dead when the
                             step lands. Do NOT delete it as ambient motion. Owner:
                             styles/fui-panel.css — see the note there. -->
                        <span class="row__line2 row__creating fui-techtext">
                            creating · {{ create_view(row.session)!.label }}
                        </span>
                        <span
                            class="row__line2 fui-meter fui-meter--march row__creating-meter"
                            role="progressbar"
                            :aria-label="`Creating worktree: step ${create_view(row.session)!.step_index} of ${create_view(row.session)!.step_total}`"
                            :aria-valuenow="create_view(row.session)!.step_index"
                            :aria-valuemin="1"
                            :aria-valuemax="create_view(row.session)!.step_total"
                        >
                            <i
                                class="fui-meter__fill"
                                :style="{ width: `${(create_view(row.session)!.step_index / create_view(row.session)!.step_total) * 100}%` }"
                            />
                        </span>
                    </template>
                    <!-- A create that failed. Terminal, but not permanent: the
                         plugin expires these, and pressing 🏝 on the same branch
                         clears it at once. -->
                    <span v-else-if="create_failure(row.session)" class="row__line2 row__create-error">
                        {{ create_failure(row.session) }}
                    </span>
                    <span v-else-if="role_segment(row, 'message')" class="row__line2 row__ask" :title="role_segment(row, 'message')!.text">
                        {{ role_segment(row, 'message')!.text }}
                    </span>
                    <span v-else-if="cut_off(row)" class="row__line2 row__cut">{{ dot_title(row) }}</span>
                    <!-- The suggested next step's chip: a SUGGESTION, never an act. A click opens
                         the row (it bubbles to the row's select), which shows the card. -->
                    <span
                        v-else-if="next_chip(row)"
                        class="row__line2 row__next"
                        :title="`Suggested next step: ${row.next_step!.label} — ${row.next_step!.reason}. Open the row to act on it`"
                    >{{ next_chip(row) }}</span>
                    <!-- An opened LEAD's open decisions: the canvas pane's slot when one is
                         mounted (`lead_decisions_target`), else right under the row. Ahead of
                         the next-step card: a decision is the operator's to make. -->
                    <Teleport
                        v-if="row.focused && lead_card"
                        defer
                        :to="lead_decisions_target || 'body'"
                        :disabled="!lead_decisions_target"
                    >
                        <LeadDecisionsCard
                            :class="{ row__card: !lead_decisions_target }"
                            :run="lead_card.run"
                            :decisions="lead_card.decisions"
                            :frozen="frozen"
                            @decide="send_decision"
                        />
                    </Teleport>
                    <!-- The OPENED row's card: in the canvas pane when one is
                         mounted (`next_step_target`), else right under the row. A `none`
                         shows its reason here only. -->
                    <Teleport
                        v-if="row.focused && row.next_step"
                        defer
                        :to="next_step_target || 'body'"
                        :disabled="!next_step_target"
                    >
                        <NextStepCard
                            :class="{ row__card: !next_step_target }"
                            :step="row.next_step"
                            :press="next_step_press(row)"
                            @do="do_next_step(row)"
                            @dismiss="dismiss_next_step(row)"
                        />
                    </Teleport>
                </template>
                <!-- The COMPLETE band's head folds it. Folded, it still counts what
                     it holds, so a finished session is never silently gone. Its
                     CLOSE ALL sits here, on the band it empties, never in the rack's
                     head: a control appears with its subject. -->
                <div
                    v-else-if="row.kind === 'cohort' && row.complete"
                    class="row__complete-head"
                >
                    <button
                        type="button"
                        class="row__fold"
                        :aria-expanded="complete_open"
                        :title="complete_open
                            ? 'Fold the finished sessions: Laya judged them done and none has a worktree'
                            : 'Show the finished sessions: Laya judged them done and none has a worktree'"
                        @click.stop="toggle_complete"
                    >
                        <span>{{ row.text }}</span>
                        <span aria-hidden="true">{{ complete_open ? '▾' : '▸' }}</span>
                    </button>
                    <button
                        type="button"
                        :class="['fui-btn', 'row__btn', 'row__btn--danger', { 'row__btn--armed': complete_delete_armed }]"
                        :disabled="frozen || !payload?.reachable || !complete_sessions.length"
                        :title="`Close all ${complete_sessions.length} finished sessions, including rows hidden by search or +N MORE — press twice. Reopen from CLOSED.`"
                        :aria-label="`Close all ${complete_sessions.length} finished sessions${complete_delete_armed ? ' — confirm' : ''}`"
                        @click.stop="delete_all_complete"
                    >{{ complete_delete_armed ? `CLOSE ${complete_sessions.length}?` : 'CLOSE ALL' }}</button>
                </div>
                <span v-else class="row__text">{{ row.text }}</span>
                <FamilyFoldLine v-if="row.kind === 'session' && row.node_id && fold_readouts.has(row.node_id)"
                    class="row__line2" :readout="fold_readouts.get(row.node_id)!" />
                <template v-else>
                    <TaskGroupBar v-for="group in groups_for_parent(payload.task_groups, row.node_id)" :key="group.id"
                        class="row__line2" :group="group" :disabled="frozen"
                        @control="(id, action) => send('group_control', id, action)" />
                </template>
            </li>
        </ul>

        <MissionBar v-if="missions" :missions="missions" :sessions="mission_sessions"
            :focused="mission_focused" :disabled="frozen" @act="send_mission" />

        </div>
        <section v-if="device_rows" class="rack__devices">
            <button
                type="button"
                class="rack__devices-head"
                :aria-expanded="devices_open"
                :disabled="frozen"
                :title="devices_open ? 'Hide CPU and memory per machine (stops sampling them)' : 'Show CPU and memory per machine'"
                @click="send('devices')"
            >
                <span>DEVICES<template v-if="device_rows.length"> · {{ device_rows.length }}</template></span>
                <span aria-hidden="true">{{ devices_open ? '▾' : '▸' }}</span>
            </button>
            <!-- Open with nothing read yet: owned by `rack-view.ts` `RackPayload.devices` — see the note there. -->
            <p v-if="devices_open && !device_rows.length" class="rack__devices-wait">waiting for the first sample…</p>
            <ul v-if="devices_open && device_rows.length" class="rack__devices-list">
                <li
                    v-for="device in device_rows"
                    :key="device.key"
                    :class="['device', `device--${device.tone}`, { 'device--has-memory': device.memory }]"
                    :title="device.title"
                    :aria-label="device.title"
                >
                    <span class="device__name" :style="{ gridRow: `span ${Math.max(device.meters.length, 1)}` }">
                        <i class="device__dot" aria-hidden="true" />
                        <span class="device__label">{{ device.label }}</span>
                        <span v-if="device.status" class="device__status">{{ device.status }}</span>
                    </span>
                    <template v-for="meter in device.meters" :key="meter.label">
                        <span class="device__kind">{{ meter.label }}</span>
                        <span class="device__value">{{ meter.text }}</span>
                        <span :class="['device__pct', { 'device__pct--warn': meter.warn }]">{{ meter.pct === null ? '—' : `${meter.pct}%` }}</span>
                        <span :class="['fui-meter', 'device__meter', { 'fui-meter--warn': meter.warn }]" aria-hidden="true">
                            <i class="fui-meter__fill" :style="{ width: `${meter.pct ?? 0}%` }" />
                        </span>
                    </template>
                    <button
                        v-if="device.memory"
                        type="button"
                        class="device__memory-toggle"
                        :style="{ gridRow: `1 / span ${Math.max(device.meters.length, 1)}` }"
                        :aria-expanded="memory_open"
                        :aria-label="memory_open ? 'Hide memory breakdown by agent' : 'Show memory breakdown by agent'"
                        :title="memory_open ? 'Hide memory breakdown by agent' : 'Show memory breakdown by agent'"
                        @click="toggle_memory"
                    >
                        <span aria-hidden="true">{{ memory_open ? '▾' : '▸' }}</span>
                    </button>
                    <ul v-if="device.memory && memory_open" class="device__memory">
                        <li v-for="row in device.memory.rows" :key="row.key" :title="row.title" :aria-label="row.title">
                            <span class="device__memory-name">{{ row.label }}</span>
                            <span>{{ row.text }}</span>
                        </li>
                        <li v-if="device.memory.more" class="device__memory-more">+{{ device.memory.more }} MORE</li>
                        <li v-if="device.memory.orphans" class="device__memory-orphans"
                            :title="device.memory.orphans.title" :aria-label="device.memory.orphans.title">
                            <span>ORPHANS</span>
                            <span>{{ device.memory.orphans.text }}</span>
                        </li>
                    </ul>
                    <template v-if="device.jobs">
                        <button
                            type="button"
                            class="device__jobs-head"
                            :aria-expanded="jobs_open"
                            :title="device.jobs.title"
                            :aria-label="`${jobs_open ? 'Hide' : 'Show'} what ${device.label} runs. ${device.jobs.title}`"
                            @click="toggle_jobs"
                        >
                            <span aria-hidden="true">{{ jobs_open ? '▾' : '▸' }}</span>
                            <span class="device__jobs-kind">PROCESSES</span>
                            <span :class="['device__jobs-summary', { 'device__jobs-summary--failed': device.jobs.failed }]">{{ device.jobs.summary }}</span>
                        </button>
                        <p v-if="jobs_open && !device.jobs.failed" class="device__jobs-note">Live processes grouped by task; queued work is not shown.</p>
                        <ul v-if="jobs_open && device.jobs.rows.length" class="device__jobs">
                            <li v-for="job in device.jobs.rows" :key="job.key" :class="['job', `job--${job.tone}`]">
                                <button
                                    type="button"
                                    class="job__head"
                                    :aria-expanded="expanded_jobs.has(job.key)"
                                    :title="job.title"
                                    :aria-label="job.title"
                                    @click="toggle_job(job.key)"
                                >
                                    <span class="job__status">{{ job.status }}</span>
                                    <span class="job__label">{{ job.label }}</span>
                                    <span class="job__text">{{ job.text }}</span>
                                    <span class="job__chev" aria-hidden="true">{{ expanded_jobs.has(job.key) ? '▾' : '▸' }}</span>
                                </button>
                                <ul v-if="expanded_jobs.has(job.key)" class="job__groups">
                                    <li v-for="group in job.groups" :key="group.key" :class="{ 'job__group--held': group.held }"
                                        :title="group.title" :aria-label="group.title">
                                        <span class="job__group-name">{{ group.name }}</span>
                                        <span>{{ group.text }}</span>
                                    </li>
                                </ul>
                            </li>
                            <li v-if="device.jobs.more" class="device__memory-more">+{{ device.jobs.more }} MORE</li>
                        </ul>
                    </template>
                    <p v-if="device.lease" class="device__lease" :title="device.lease.title" :aria-label="device.lease.title">
                        <span class="device__jobs-kind">LEASE</span>
                        <span :class="['device__jobs-summary', { 'device__jobs-summary--failed': device.lease.warn }]">{{ device.lease.summary }}</span>
                    </p>
                </li>
            </ul>
        </section>
    </div>
</template>

<style scoped>
/* No @import here: Vite scopes @imports inside scoped styles, which silently
   strips the shared FUI classes. They are loaded globally by `styles/boot.ts`. */
/* `100%`, NOT `100vh`. On the rack page this component is a flex child of a
   column that holds more than it, where `100vh` would overflow. The page gives
   html/body/#app `height: 100%`, so where the rack IS the whole document (a
   framed rack) this resolves to the viewport's height all the same. */
.rack {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    padding: 14px 12px;
    gap: 10px;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --fui-glass-fill: var(--rack-surface);
    color: var(--rack-text);
}

.rack__search { display: flex; align-items: center; gap: 8px; }
.rack__search input {
    width: 100%; min-width: 0; min-height: 30px; padding: 5px 8px;
    font: inherit; font-size: 13px; color: var(--rack-text);
    background: var(--rack-raised); border: 1px solid rgba(147, 255, 212, 0.2);
    border-radius: 2px;
}
.rack__search input::placeholder { color: var(--rack-muted); }
.rack__search input:focus-visible { outline: 1px solid var(--color-accent-green, #93ffd4); }
.rack__search span { flex-shrink: 0; color: var(--rack-muted); font-size: 11px; }

.rack__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex: 0 0 auto;
}

.rack__title {
    margin-right: auto;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    text-transform: none;
    letter-spacing: -0.02em;
}

/* The rack's button primitive, in the create accent: the colour of bringing
   something into being (`+ NEW`, the fork, CREATE). */
.rack__new {
    flex: 0 0 auto;
    cursor: pointer;
    background: transparent;
    color: #42ffa8; /* rack-local; deliberately not a shared token */
    border: 1px solid currentColor;
    border-radius: 2px;
    padding: 4px 10px;
    font: inherit;
    font-size: 13px;
    letter-spacing: 0.1em;
}
.rack__new:hover:not(:disabled) { background: rgba(66, 255, 168, 0.14); }
.rack__new:active:not(:disabled) { transform: translateY(1px); }

/* LAUNCH chips. `.fui-tag` shape in neutral ink — the open one takes the
   create accent's border, which is chrome (what the press will do), not a hue
   per profile. The meter sits under the label inside the chip so the column
   never reflows as usage moves. */
.rack__settings { flex: 0 0 auto; display: grid; gap: 10px; padding: 8px 10px; border-bottom: 1px solid rgba(180, 190, 200, 0.3); }
.rack__projects { display: grid; gap: 6px; padding: 2px 0; flex-shrink: 0; }
.rack__project { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; min-width: 0; max-width: 100%; min-height: 38px; text-align: left; }
.rack__project small { max-width: 100%; overflow-wrap: anywhere; font: 11px ui-monospace, monospace; color: var(--rack-muted); }
.rack__project[aria-pressed="true"] { color: var(--rack-text); border-color: currentColor; background: var(--rack-raised); }
.rack__project-note { margin: 4px 10px; font-size: 0.8rem; }
@media (pointer: coarse) { .rack__project { min-height: 44px; } }
.rack__launch {
    position: relative;
    z-index: 4;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    flex: 0 0 auto;
}
.rack__launch-slot { display: inline-flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.rack__launch-slot--open { flex-basis: 100%; padding: 8px 0; }
.rack__chip {
    flex-direction: column;
    align-items: stretch;
    gap: 2px;
    cursor: pointer;
    background: transparent;
    color: rgba(244, 247, 255, 0.72);
    border-color: rgba(244, 247, 255, 0.28);
    font-size: 0.72rem;
}
.rack__chip:hover:not(:disabled) { background: rgba(255, 255, 255, 0.06); }
.rack__chip--on {
    color: #f4f7ff;
    border-color: #42ffa8;
    background: rgba(66, 255, 168, 0.1);
}
.rack__chip:focus-visible { outline: 1px solid #42ffa8; outline-offset: 2px; }
.rack__chip-warn { margin-left: 4px; color: #f8c146; }
.rack__chip-meter { height: 3px; min-width: 28px; }
/* The WEEKLY readout is the number, the 5-hour window the bar under it: two
   different windows, so neither restates the other. Amber at the warn line is
   the only colour (the hue contract above); a spent account dims as a whole. */
.rack__chip-head { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.rack__chip-week { font-size: 0.68rem; font-variant-numeric: tabular-nums; color: var(--rack-text); }
.rack__chip-week--warn { color: #f8c146; opacity: 1; }
/* The offline red (`.device--bad`): not a level of usage, an account that runs nothing. */
.rack__chip-week--out { color: #ff7b72; letter-spacing: 0.06em; }
.rack__chip--spent .rack__chip-label { text-decoration: line-through; opacity: 0.55; }
/* AUTO: its second line is WHERE it lands, not a usage reading. Amber only
   when every account is spent and it is pointing at the soonest reset. */
.rack__chip-target { font-size: 0.68rem; opacity: 0.8; white-space: nowrap; }
/* The chip AUTO lands on: a dashed outline in the same create accent, so it
   reads as "this one, by AUTO" beside AUTO's own solid accent. */
.rack__chip--auto-target { border-style: dashed; border-color: rgba(66, 255, 168, 0.6); }
/* The lone `+ NEW` chip (fewer than two profiles) reads as the create button it is. */
.rack__chip--new { color: #42ffa8; border-color: currentColor; letter-spacing: 0.1em; }

/* The selected profile expands in flow so its choices never cover search or sessions. */
.rack__fork {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 4px;
    background: var(--rack-raised);
    border: 1px solid rgba(66, 255, 168, 0.45);
    border-radius: 2px;
}
.rack__fork-btn {
    display: inline-flex;
    align-items: center;
    cursor: pointer;
    background: transparent;
    color: #42ffa8;
    border: 1px solid currentColor;
    border-radius: 2px;
    padding: 3px 8px;
    font: inherit;
    font-size: 0.72rem;
    letter-spacing: 0.1em;
}
.rack__fork-btn:hover:not(:disabled) { background: rgba(66, 255, 168, 0.14); }
.rack__fork-btn:focus-visible { outline: 1px solid #42ffa8; outline-offset: 2px; }
/* The island wears the row mark's mint, so the press and the mark it leaves match. */
.rack__fork-island { color: var(--color-accent-green, #93ffd4); }

.rack__notice { flex: 0 0 auto; margin: 0; font-size: 12px; }

/* The PROFILE column. Unboxed mono ink, no hue (a profile is identity, not
   state), sized to the longest label the bridge lists — `ch` plus the
   letter-spacing per character — so the column holds still as rows come and go. */
.row__profile {
    min-width: calc(var(--rack-profile-chars, 3) * (1ch + 0.08em));
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
    color: var(--rack-muted);
    font-weight: 600;
}
.row__profile--none { opacity: 0.25; }

/* The trailing cell: MODEL then PROFILE, right-aligned, so the profile labels
   (fixed min-width) and the model's right edge each sit at one x. */
.row__who { display: flex; justify-content: flex-end; align-items: baseline; gap: 8px; }
/* MODEL beside the account — only when the list is wide enough to spare it.
   Narrower, the name keeps the width and only an UNUSUAL model prints, inline
   (`.row__model-inline`; `usual_model_label` owns which). Sentence case, as the
   model is named everywhere else. */
.row__model { display: none; font-size: 0.68rem; white-space: nowrap; color: var(--rack-muted); }
@container rack-list (min-width: 440px) {
    .row__model { display: inline; }
    .row__model-inline { display: none; }
}


/* Render controls (FONT / RENDER / FAMILY, behind the head's ⚙). */
.rack__render {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    flex-wrap: wrap;
    padding: 4px 8px;
    border: 1px solid rgba(140, 205, 170, 0.35);
    border-radius: 3px;
    background: var(--rack-raised);
    font-size: 12px;
    color: rgba(225, 240, 232, 0.96);
}

.rack__render-label {
    color: rgba(150, 195, 175, 0.7);
    font-size: 10px;
    letter-spacing: 0.12em;
}
/* Separates the two control groups when they share a row, and does nothing
   extra once flex-wrap has already broken them onto separate lines. */
.rack__render-label--gap { margin-left: 4px; }

.rack__render-btn {
    background: none;
    border: 1px solid rgba(140, 205, 170, 0.55);
    border-radius: 3px;
    color: rgba(225, 240, 232, 0.96);
    font: inherit;
    line-height: 16px;
    padding: 0 6px;
    cursor: pointer;
}
.rack__render-btn:hover { background: rgba(140, 205, 170, 0.18); }
.rack__render-btn:active { transform: translateY(1px); }

.rack__render-slider {
    /* min-width 0 so the slider is what shrinks in a narrow rack, rather than
       the readout wrapping to its own line. */
    flex: 1 1 90px;
    min-width: 0;
    max-width: 130px;
    margin: 0;
    accent-color: rgba(140, 205, 170, 0.9);
    cursor: pointer;
}

.rack__render-readout {
    min-width: 4ch;
    text-align: right;
    color: rgba(150, 195, 175, 0.85);
}

.rack__render-select {
    background: var(--rack-raised);
    border: 1px solid rgba(140, 205, 170, 0.55);
    border-radius: 3px;
    color: rgba(225, 240, 232, 0.96);
    font: inherit;
    font-size: 11px;
    letter-spacing: 0.08em;
    padding: 1px 4px;
    cursor: pointer;
}
/* The dropdown list itself is painted by the OS, which ignores the control's
   colors — without this it opens as white-on-white inside the dark panel. */
.rack__render-select option { background: var(--rack-raised); color: var(--rack-text); }

.rack__empty { opacity: 0.6; font-size: 14px; }
.rack__empty--warn { color: #ffd54f; opacity: 0.95; }

/* Stacked rather than inline: `denied` carries Chrome's full settings path, and
   that string has to WRAP and stay readable — it is the only instruction on the
   one branch with no button to press. */
.rack__access { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.rack__access-head { font-size: 12px; letter-spacing: 0.08em; }
.rack__access-detail { font-size: 12px; opacity: 0.85; line-height: 1.45; }
/* Borrows `.rack__new`'s shape (that class IS the rack's button primitive) and
   recolours to the warn amber
   it sits inside. Green there means "bring a session into being"; this asks for
   a permission, which is the callout's business, not a creation. */
.rack__access-btn { align-self: flex-start; margin-top: 2px; color: #ffd54f; }
.rack__access-btn:hover:not(:disabled) { background: rgba(255, 213, 79, 0.14); }

/* Frozen state (a satellite window only). The list is still worth reading (it's the last
   good snapshot), so it stays legible; only the ACTIONS go dead, because there
   is nothing on the other end to service them. */
.rack__frozen { flex: 0 0 auto; margin: 0; }
.row--frozen.row--session { cursor: not-allowed; }
.row--frozen.row--session:hover { background: none; }
.rack button:disabled { cursor: not-allowed; opacity: 0.4; }
.rack button:disabled:hover { background: transparent; }

.rack__list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    flex: 1 1 auto;
    /* Without it a flex item never shrinks below its content, so a long rack
       overflowed the clipped `.rack` instead of scrolling inside the list. */
    min-height: 140px;
    /* The ledger: mark · name · ctx · (profile). Session rows are subgrids, so
       the trailing `auto` columns size to the widest cell in the WHOLE list and
       every row's value sits at one x. */
    display: grid;
    /* `auto`, not the mark width: a subgrid adds its row's padding and border to
       the edge items, so a FIXED first track would squeeze the mark into the gap. */
    grid-template-columns: auto minmax(0, 1fr) auto;
    /* What "wide enough for the MODEL readout" is measured against. */
    container: rack-list / inline-size;
    align-content: start;
    column-gap: 9px;
    --rack-mark-w: 9px;
    /* One family level. Family depth: owned by `.row__indent` (template) — see the note there. */
    --rack-depth-mark-step: 9px;
    --rack-depth-name-step: 14px;
}
.rack__list--profiles { grid-template-columns: auto minmax(0, 1fr) auto auto; }
.rack__list--emoji { --rack-mark-w: 18px; }

/* ↺ CLOSED: under the head, above LAUNCH — the undo sits next to where you look
   first. Capped so a long list scrolls instead of pushing the rack off. */
.rack__closed { flex: 0 0 auto; max-height: 30%; overflow-y: auto; padding: 0 6px; }
.rack__closed-note { margin: 0; font-size: 12px; opacity: 0.6; }
.rack__closed-head { display: flex; align-items: center; gap: 8px; padding: 6px 6px 2px; font-weight: 700; font-size: 12px; }
.rack__closed-head .fui-techtext { flex: 1 1 auto; }
/* A reboot's offer is the one the operator came for, so it is the bright one: the ↺ glyph's own
   green (`.closed__glyph`), until it is taken or dismissed. Armed turns it amber (`.row__btn--armed`, later). */
.rack__restore-btn,
.rack__restore-all {
    color: var(--color-accent-green, #93ffd4);
    border-color: rgba(147, 255, 212, 0.55);
    background: rgba(147, 255, 212, 0.1);
}
.rack__closed-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.closed {
    display: grid;
    grid-template-columns: 1.2em minmax(0, 1fr) auto auto;
    align-items: center;
    column-gap: 10px;
    width: 100%;
    min-height: 28px;
    padding: 2px 6px;
    background: transparent;
    border: 1px solid transparent;
    color: inherit;
    font: inherit;
    font-size: 13px;
    text-align: left;
    cursor: pointer;
}
.closed:hover:not(:disabled),
.closed:focus-visible { border-color: rgba(147, 255, 212, 0.4); background: rgba(147, 255, 212, 0.06); }
.closed:disabled { cursor: default; opacity: 0.45; }
.closed--busy:disabled { opacity: 1; }
.closed__glyph { color: var(--color-accent-green, #93ffd4); text-align: center; }
.closed__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.closed__place { opacity: 0.55; font-size: 11.5px; max-width: 14ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.closed__ago { opacity: 0.55; font-size: 11.5px; font-variant-numeric: tabular-nums; }

/* DEVICES: pinned under the list (the list is the flex-grow item), so it sits
   in the space a short rack leaves empty and is on screen however long the list is.
   Capped, with its own scroll: an open JOBS or memory breakdown is taller than the rack on a
   small window, and uncapped it took the list's whole height (the list shrinks to nothing
   before a `flex: 0 0 auto` neighbour gives up a pixel). Never `flex-shrink`: against a long
   list the section would give up its own rows first. The head stays put while it scrolls. */
.rack__devices { flex: 0 0 auto; max-height: 45%; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.rack__devices-wait { margin: 0; padding: 0 6px; font-size: 11.5px; opacity: 0.55; }
/* The cohort header's look (`.row--cohort`), as a button: it opens and closes. MISSIONS wears it
   too, so the two sections under the list read as one band. */
.rack__devices-head,
.rack :deep(.missions__head) {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 0 6px 3px;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--rack-raised);
    color: var(--rack-muted);
    font: inherit;
    font-size: 11.5px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
}
/* After the shared rule: an opaque head, so the rows scroll under it (`.rack__devices`). */
.rack__devices-head { position: sticky; top: 0; z-index: 1; flex: 0 0 auto; background: var(--rack-surface); }
.rack__devices-list { list-style: none; margin: 0; padding: 0 6px; display: flex; flex-direction: column; gap: 6px; }
/* name | CPU/RAM | value | % | meter — one row per meter, the name spanning them. */
.device {
    display: grid;
    grid-template-columns: minmax(0, max-content) auto auto auto minmax(28px, 1fr);
    column-gap: 8px;
    row-gap: 2px;
    align-items: center;
    font-size: 12.5px;
    font-variant-numeric: tabular-nums;
}
.device--has-memory { grid-template-columns: minmax(0, max-content) auto auto auto minmax(28px, 1fr) 28px; }
.device__memory-toggle {
    grid-column: 6;
    align-self: stretch;
    min-height: 28px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--color-accent-green, #93ffd4);
    font: inherit;
    cursor: pointer;
    opacity: 0.65;
}
.device__memory-toggle:hover,
.device__memory-toggle:focus-visible { opacity: 1; }
@media (pointer: coarse) {
    .device--has-memory { grid-template-columns: minmax(0, max-content) auto auto auto minmax(28px, 1fr) 44px; }
    .device__memory-toggle { min-height: 44px; }
}
.device__name { display: flex; align-items: center; gap: 6px; min-width: 0; align-self: start; padding-top: 1px; }
.device__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: 0.04em; }
.device__dot { flex: 0 0 auto; width: 7px; height: 7px; border-radius: 50%; background: var(--color-accent-green, #93ffd4); }
.device--wait .device__dot { background: rgba(244, 247, 255, 0.35); }
.device--bad .device__dot { background: #ff7b72; }
.device--wait .device__value,
.device--wait .device__pct,
.device--wait .device__meter { opacity: 0.5; }
.device__status { font-size: 11px; letter-spacing: 0.06em; opacity: 0.6; }
.device--bad .device__status { color: #ff7b72; opacity: 0.9; }
.device__kind { font-size: 11px; letter-spacing: 0.06em; opacity: 0.55; }
.device__value { text-align: right; white-space: nowrap; }
.device__pct { text-align: right; min-width: 3.2em; opacity: 0.8; }
.device__pct--warn { color: #f8c146; opacity: 1; }
.device__meter { height: 3px; }
/* Who holds that memory: a ranking under the meters, on the row's whole width. */
.device__memory { grid-column: 1 / -1; list-style: none; margin: 2px 0 0; padding: 0 0 0 13px; display: flex; flex-direction: column; gap: 1px; font-size: 11.5px; opacity: 0.8; }
.device__memory li { display: flex; justify-content: space-between; gap: 8px; }
.device__memory-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.device__memory-more { opacity: 0.6; letter-spacing: 0.06em; }
.device__memory-orphans { color: #f8c146; letter-spacing: 0.04em; }
/* What a compute host runs: one closed line naming its busy jobs, opening to a row per job, each opening to its
   processes. Running wears the rack's green, held the amber every wait wears, idle stays quiet. */
.device__jobs-head {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 28px;
    padding: 0 0 0 13px;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 11.5px;
    text-align: left;
    cursor: pointer;
    opacity: 0.8;
}
.device__jobs-head:hover,
.device__jobs-head:focus-visible { opacity: 1; }
.device__jobs-kind { letter-spacing: 0.06em; opacity: 0.55; }
.device__jobs-summary { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.device__jobs-summary--failed { color: #f8c146; letter-spacing: 0.04em; }
.device__jobs-note { grid-column: 1 / -1; margin: 0; padding-left: 13px; font: 0.78rem system-ui, sans-serif; }
/* This machine's resource lease: who holds the GPU's GB budget, the subscription login and the CPU. A readout, not a
   control, so it is the JOBS line's look without its button; amber when a slot is full or has work waiting. */
.device__lease { grid-column: 1 / -1; display: flex; align-items: center; gap: 6px; margin: 0; min-height: 22px; padding: 0 0 0 13px; font-size: 11.5px; opacity: 0.8; }
.device__jobs { grid-column: 1 / -1; list-style: none; margin: 0; padding: 0 0 0 13px; display: flex; flex-direction: column; font-size: 11.5px; }
.job__head {
    display: grid;
    grid-template-columns: 7ch minmax(0, 1fr) auto 1em;
    align-items: center;
    gap: 6px;
    width: 100%;
    min-height: 28px;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
}
.job__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: 0.04em; }
.job__text { white-space: nowrap; opacity: 0.8; }
.job__chev { opacity: 0.45; }
.job__head:hover .job__chev,
.job__head:focus-visible .job__chev { opacity: 1; }
.job--run .job__status { color: var(--color-accent-green, #93ffd4); }
.job--held { color: #f8c146; }
.job--idle .job__head,
.job--other .job__head { opacity: 0.6; }
.job__groups { list-style: none; margin: 0 0 4px; padding: 0 0 0 calc(7ch + 6px); display: flex; flex-direction: column; gap: 1px; opacity: 0.8; }
.job__groups li { display: flex; justify-content: space-between; gap: 8px; }
.job__group-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job__group--held { color: #f8c146; }
@media (pointer: coarse) {
    .device__jobs-head,
    .job__head { min-height: 44px; }
}
.row__mission { flex: 0 0 auto; color: var(--color-accent-green, #93ffd4); font-size: 10px; }

.row {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 6px;
    border-left: 2px solid transparent;
    font-size: 14px;
    line-height: 1.4;
    /* Resolved HERE, where `--rack-depth` is set inline; on the list it would read 0. */
    --rack-depth-indent: calc(var(--rack-depth, 0) * (var(--rack-depth-mark-step) + var(--rack-depth-name-step)));
}
.row--session {
    display: grid;
    grid-template-columns: subgrid;
    align-items: center;
    cursor: pointer;
}
/* A hairline between neighbours instead of a box round every name. */
.row--session + .row--session { box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035); }
.row--session:hover { background: rgba(255, 255, 255, 0.06); }
.row--focused { border-left-color: var(--color-accent-green, #93ffd4); background: rgba(147, 255, 212, 0.08); }
.row--dim { opacity: 0.55; }
/* Note lines are context, not targets — indented and never clickable. */
.row--note { padding-left: calc(var(--rack-mark-w) + 17px + var(--rack-depth-indent)); font-size: 12.5px; opacity: 0.5; cursor: default; }
.row--overflow, .row--empty { opacity: 0.5; font-style: italic; cursor: default; }
/* Attention bands are quiet labels; state marks and the summary carry urgency. */
.row--cohort {
    cursor: default;
    margin-top: 9px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--rack-raised);
    color: var(--rack-muted);
    font-size: 12px;
    letter-spacing: 0.02em;
}
.row--cohort:first-child { margin-top: 0; }
/* The COMPLETE band's head: a fold button that keeps the head's look, then CLOSE ALL. */
.row__complete-head { display: flex; align-items: center; gap: 8px; width: 100%; }
.row__complete-head .row__fold { flex: 1; min-width: 0; min-height: 28px; }
.row__complete-head .row__btn { flex: 0 0 auto; min-height: 28px; }
@media (pointer: coarse) {
    .row__complete-head .row__fold, .row__complete-head .row__btn { min-height: 44px; }
}
.row__fold {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 0;
    background: transparent;
    border: 0;
    color: inherit;
    font: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    cursor: pointer;
}

.row__text { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* The name cell: indent, family chips, ▶, the name, its island, the detail,
   and — on hover / the focused row — the actions at its end. */
.row__main { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 7px; min-width: 0; }
.row__status { flex-basis: 100%; order: 2; font-size: 12px; color: var(--rack-muted); padding-left: var(--rack-depth-indent); }
.row__indent { flex: 0 0 auto; width: var(--rack-depth-indent); }
/* After the name: muted and smaller, so the name is what the eye lands on. */
.row--session .row__text { flex-basis: 100%; order: 3; font-size: 12px; color: var(--rack-muted); }
.row--session .row__text:empty { display: none; }
.row__ctx-col { text-align: right; font-size: 12px; font-variant-numeric: tabular-nums; min-width: 4ch; white-space: nowrap; }

/* The session name, boxed: the rename door. Same frame and padding as the
   `.row__rename` input it turns into, so a click swaps one for the other
   without the row moving. Neutral ink at rest, the input's mint on hover —
   the box is chrome until you reach for it. */
.row__name {
    flex: 0 1 auto;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    background: transparent;
    color: inherit;
    /* Boxed only while the row is hovered or focused: the same door, a quarter
       of the ink. A box on every row at rest is most of the panel's weight. */
    border: 1px solid transparent;
    border-radius: 2px;
    padding: 0 6px;
    margin-left: -7px;
    font: inherit;
    line-height: inherit;
    text-align: left;
    cursor: text;
}
.row--session:hover .row__name:not(.row__name--static) { border-color: rgba(244, 247, 255, 0.22); }
.row__name:hover:not(:disabled),
.row__name:focus-visible {
    border-color: var(--color-accent-green, #93ffd4);
    background: rgba(147, 255, 212, 0.06);
    outline: none;
}
/* A dead row's name: nothing to rename, so no box. */
.row__name--static { border-color: transparent; cursor: inherit; }
/* Locked while the session is still starting (`name_locked`): the box stays
   drawn so nothing moves when it unlocks, but reads as not-yet. Scoped under
   `.rack` in front to outrank `.rack button:disabled`'s not-allowed cursor. */
.rack .row__name--locked:disabled { border-style: dashed; opacity: 0.55; cursor: progress; }
.row__mark { flex: 0 0 auto; color: var(--color-accent-green, #93ffd4); }
/* The operator's star. `--rack-star` is used for nothing else on this page: every other hue here
   is the rack's reading of a session, and this one mark is the operator's own. */
.rack { --rack-star: #ff9ad5; }
.row__star {
    display: none;
    flex: 0 0 auto;
    width: 24px;
    height: 24px;
    padding: 0;
    cursor: pointer;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    color: var(--rack-muted);
}
.row:hover .row__star, .row--focused .row__star, .row__star--on { display: inline-flex; }
.row__star--on { color: var(--rack-star); }
.row__star:hover:not(:disabled), .row__star:focus-visible { color: var(--rack-star); border-color: currentColor; outline: none; }
@media (pointer: coarse) {
    .row__star { width: 44px; height: 32px; }
}
.rack__star-filter {
    flex: 0 0 auto;
    gap: 4px;
    min-width: 28px;
    min-height: 30px;
    padding: 0 8px;
    color: var(--rack-star);
    font-variant-numeric: tabular-nums;
}
.rack__star-filter--on { border-color: var(--rack-star); background: color-mix(in srgb, var(--rack-star) 16%, transparent); }
@media (pointer: coarse) {
    .rack__star-filter { min-width: 44px; min-height: 44px; }
}
.closed__star { width: 12px; height: 12px; margin-right: 5px; vertical-align: -1px; fill: var(--rack-star); }
/* Context left: muted while there's room, the armed-✕ amber at ≤25%, the danger
   red at ≤10% (thresholds: multi-terminal-layout.ts). */
.row__ctx--normal { opacity: 0.6; }
.row__ctx--low { color: #f8c146; }
.row__ctx--critical { color: #ff7b72; font-weight: 600; }

/* Inline rename field. Sharp cells + mint rule, the same FUI vocabulary the
   rest of the rack uses; `font: inherit` keeps it on the mono grid so the row
   doesn't jump height when it swaps in. The row wraps so a validation message
   can take its own line instead of squeezing the input. */

.row__rename {
    flex: 1 1 auto;
    min-width: 0;
    background: rgba(147, 255, 212, 0.06);
    color: #f4f7ff;
    border: 1px solid var(--color-accent-green, #93ffd4);
    border-radius: 2px;
    padding: 0 6px;
    font: inherit;
    font-size: 14px;
    outline: none;
}
.row__rename:focus { background: rgba(147, 255, 212, 0.12); }
/* Amber, not red: the name is not accepted YET, but nothing has failed and
   nothing was destroyed — matching the DETACHED tag's warn hue. */
.row__rename--bad { border-color: #f8c146; }

/* The second line: under the NAME, out to the row's end. */
.row__line2 {
    grid-column: 2 / -1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
    padding-bottom: 2px;
}
/* What the row SAID that the search matched. The marks are chrome mint, never a state hue. */
.row__said { opacity: 0.9; }
.row__said::before { content: '“ '; opacity: 0.5; }
.row__said mark {
    background: rgba(147, 255, 212, 0.18);
    color: var(--color-accent-green, #93ffd4);
    border-radius: 2px;
}
/* Why the agent wants you — the reason this row is at the top. */
.row__ask { opacity: 0.9; }
.row__ask::before { content: '↳ '; opacity: 0.5; }
/* The suggested next step's chip: chrome mint, never a state hue (the rack's hue contract). */
.row__next {
    color: var(--color-accent-green, #93ffd4);
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 11.5px;
    letter-spacing: 0.02em;
}
/* The opened row's card when no canvas pane holds it: the row's full width, under its name. */
.row__card {
    grid-column: 2 / -1;
    margin: 2px 0 4px;
}
/* A cut-off turn's reason (🪫 / 🔋 / 🛑), in the armed amber: look at this. */
.row__cut { color: #f8c146; }

.row__rename-error {
    color: #f8c146;
    font-size: 11.5px;
    letter-spacing: 0.04em;
}

/* Colour is bound inline from display_state_color_css (see dot_style), so this
   surface cannot drift from any other that draws a state. Only the pulse lives
   here. */
.dot { flex: 0 0 auto; width: 9px; height: 9px; border-radius: 50%; background: #6b7683; }
.dot--fresh { background: transparent; box-shadow: inset 0 0 0 2px currentColor; }
/* The emoji mark (display_state_emoji) — the rack's at-a-glance "what needs
   me" signal. ~1.35x the 14px row text: much larger, a column of emoji is the
   heaviest ink in the window and out-shouts the names it marks. Colour emoji
   read at this size; the column
   (`--rack-mark-w`) is fixed so a name starts in the same place whatever the
   glyph. The emoji stack first, or the mono row font's own fallback picks a
   monochrome glyph for some of them. */
/* The state mark plus, on a finished turn with work left, Laya's ⚠️ pinned to
   its lower-right corner — it rides the mark rather than taking a column, so
   names stay aligned whether or not a row carries it. */
.mark { position: relative; display: inline-flex; align-items: center; justify-content: center; min-width: var(--rack-mark-w); translate: calc(var(--rack-depth, 0) * var(--rack-depth-mark-step)) 0; }
.mark__open-work {
    position: absolute;
    right: -4px;
    bottom: -5px;
    padding: 0;
    border: 0;
    background: none;
    cursor: pointer;
    font-size: 11px;
    line-height: 1;
    font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif;
}
.mark__open-work:disabled { cursor: default; }
span.mark__open-work { cursor: default; }
/* stuck.py's 🧱 rides the upper-right corner, opposite the ⚠️, so a row can carry both. */
.mark__stuck {
    position: absolute;
    right: -4px;
    top: -5px;
    font-size: 11px;
    line-height: 1;
    font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif;
}
/* The ⚠️'s answers: opened by a press, beside the mark, over the name — never a column. */
.mark__answers {
    position: absolute;
    left: calc(100% + 2px);
    top: 50%;
    transform: translateY(-50%);
    z-index: 3;
    display: inline-flex;
    gap: 4px;
}
.mark__answer { min-width: 28px; min-height: 28px; padding: 0; }
@media (hover: none) { .mark__answer { min-width: 44px; min-height: 44px; } }
.glyph {
    flex: 0 0 auto;
    width: var(--rack-mark-w);
    font-size: 16px;
    line-height: 1;
    text-align: center;
    font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif;
}
/* Two states move, dot or emoji. Attention pulses — it's the one asking for
   you; background breathes, shallower and at half the rate — work still
   running, so don't close it. Periods ride `dot_style` (state_pulse_period_ms);
   these are fallbacks. */
.mark--attention { animation: rack-pulse 1.6s ease-in-out infinite; }
@keyframes rack-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.mark--background { animation: rack-breathe 3.2s ease-in-out infinite; }
@keyframes rack-breathe { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }

/* ── Worktree create progress ──────────────────────────────────────────────
   Both readouts ride `.row__line2`, like a rename validation message. */

.row__creating {
    color: rgba(150, 195, 175, 0.85);
    font-size: 11px;
    letter-spacing: 0.06em;
}

.row__creating-meter {
    margin: 1px 0 3px;
    padding-bottom: 0;
    height: 3px;
    --fui-meter-color: #42ffa8;
}

/* The fill's WIDTH is the determinate part (step N of 4, bound inline). This
   is the continuous part: a quantized march inside whatever width the current
   step has, so a step that takes 90 s still reads as running rather than hung.

   `steps(8, jump-none)` and not a smooth slide: motion on these pages is
   quantized throughout; a system switches, it does not drift.

   This LOOPS, and it is the one kind that may: a frozen spinner communicates
   nothing (WCAG 2.2.2). It is also NOT gated on reduced motion, for the same
   reason. It stops dead when the row stops being a creating row, because the
   element is gone. Do not delete this as ambient motion. */
/* The marching stripes themselves: `.fui-meter--march`, owned by
   styles/fui-panel.css — see the note there. The exemption above is why
   this one loops and is not gated on reduced motion. */

/* Amber, matching the rename error: this is "look at this", not "danger". The
   plugin expires it, and a retry of the same branch clears it at once. */
.row__create-error {
    flex: 1 0 100%;
    color: #f8c146;
    font-size: 11.5px;
    letter-spacing: 0.04em;
}

/* NOT `opacity: 0`: hidden actions that keep their width make the profile
   label drift row to row. `display: none` takes no width, and when they
   show they squeeze the muted detail text inside the name cell — never a column. */
.row__actions { display: none; gap: 4px; flex: 0 0 auto; margin-left: auto; }
.row:hover .row__actions, .row--focused .row__actions { display: flex; }

/* PR / CLEAN sit in the same row strip as ▶ / ✕ but act on the BRANCH (push it,
   or clear it away once landed), so they get their own hue rather than the blue
   the session actions share. */
.row__btn--pr { color: #8cd9b0; letter-spacing: 0.06em; }
.row__btn--pr-open { color: #42ffa8; }
.row__btn--pr:disabled { opacity: 0.7; cursor: default; }

.row__btn {
    cursor: pointer;
    background: transparent;
    color: #79c0ff;
    border: 1px solid rgba(121, 192, 255, 0.4);
    border-radius: 2px;
    padding: 1px 7px;
    font: inherit;
    font-size: 12px;
}
.row__btn:hover { background: rgba(121, 192, 255, 0.16); }
.row__btn--danger { color: #ff7b72; border-color: rgba(255, 123, 114, 0.4); }

/* Armed ✕: the same amber as every armed `CONFIRM?`, so an armed
   control looks identical wherever it appears in this window. Declared AFTER
   `--danger` so it wins the color. */
.row__btn--armed {
    color: #f8c146;
    border-color: rgba(248, 193, 70, 0.55);
    background: rgba(248, 193, 70, 0.14);
}
/* Needs the same specificity as `.row__btn:hover` above, or the armed button
   reverts to the blue hover the moment you move to click it. */
.row__btn--armed:hover { background: rgba(248, 193, 70, 0.24); }
.row__btn--danger:hover { background: rgba(255, 123, 114, 0.16); }
.rack :deep(.fui-btn:not(.row__btn)) { border: 1px solid color-mix(in srgb, var(--rack-muted) 32%, transparent); border-radius: 5px; background: transparent; color: var(--rack-text); padding: 5px 8px; font: inherit; font-size: 12px; cursor: pointer; }
.rack :deep(.fui-btn:not(.row__btn):hover:not(:disabled)) { background: var(--rack-raised); }
.rack :deep(.fui-btn:disabled) { opacity: 0.4; cursor: not-allowed; }
.rack .rack__project { padding: 9px 10px; }
.rack .rack__project[aria-pressed="true"] { background: var(--rack-raised); border-color: var(--rack-muted); }
.rack__search input { border-radius: 5px; min-height: 36px; }
/* Settings and launch choices may grow; the device readout owns a separate visible region. */
.rack__body { display: flex; flex-direction: column; gap: 12px; flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; }
.rack__head, .rack__search, .rack__attention, .rack__create { flex-shrink: 0; }
.rack__session-count { font-size: 12px; font-weight: 400; color: var(--rack-muted); margin-left: 8px; }
.rack__attention { padding: 8px 2px 12px; }
.rack__attention strong { font-size: 22px; font-weight: 500; letter-spacing: -0.03em; }
.rack__attention strong span { color: var(--fui-warn, #f8c146); }
.rack__attention p { margin: 6px 0 0; color: var(--rack-muted); font-size: 12px; }
.rack__create-toggle { width: 100%; display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--rack-muted) 40%, transparent); border-radius: 6px; background: var(--rack-raised); color: var(--rack-text); font: inherit; cursor: pointer; }
.rack__launch-options { padding: 12px 2px 4px; }
.rack__section-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: 10px 0; font-size: 12px; color: var(--rack-muted); }
.rack__profiles { padding: 8px; background: var(--rack-raised); border-radius: 6px; }
.rack__profiles p { font-size: 12px; margin: 5px 0 10px; }
.rack__profile-setting { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 6px 0; }
.rack__profile-setting small { margin-left: 6px; color: var(--rack-muted); }
.rack__devices { border-top: 1px solid var(--rack-raised); padding-top: 8px; max-height: 35%; min-height: 36px; }
.row__name { font-weight: 550; }
.row__who, .row__ctx-col { font-size: 11px; }
.row__who { color: var(--rack-muted); }
.rack :is(button, input):focus-visible { outline: 2px solid var(--color-accent-green, #93ffd4); outline-offset: 2px; }
@media (pointer: coarse) { .rack__create-toggle, .rack__profile-setting button { min-height: 44px; } }
</style>

<style scoped>
.row--child { background: color-mix(in srgb, var(--color-accent-green, #93ffd4) 4%, transparent); border-left: 2px solid color-mix(in srgb, var(--color-accent-green, #93ffd4) 18%, transparent); }
.row--family { border-top: 1px solid color-mix(in srgb, var(--color-accent-green, #93ffd4) 12%, transparent); }
.row__family-toggle { flex-shrink: 0; cursor: pointer; min-width: 28px; min-height: 28px; padding: 0 4px; gap: 2px; }
.row__family-toggle .fui-key { margin-left: 0; }
.row__family-toggle--folded { color: var(--color-accent-green, #93ffd4); }
@media (pointer: coarse) { .row__family-toggle { min-width: 44px; min-height: 44px; } }
.rack__head > [aria-pressed="true"] { border-color: var(--color-accent-green, #93ffd4); }
.rack__icon { display: inline-flex; align-items: center; justify-content: center; }
.rack__icon svg { width: 14px; height: 14px; fill: currentColor; }
/* LABEL: a glyph sized to sit level with the ▶ / ✕ text beside it. */
.rack__icon.row__btn--label svg { width: 12px; height: 12px; }
</style>
