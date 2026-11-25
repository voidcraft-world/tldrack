/**
 * What the page makes of the bridge's update view: the state one poller keeps, how each answer
 * moves it, when to ask again, and every word the rack's ⚙ and the settings card show. Pure, so
 * all of it runs under bare `node --test`. The view itself: owned by `update-client.ts` — see the
 * note there. The timers and the requests: `update-poller.ts`.
 *
 * A RUN THIS PAGE STARTED is followed until the page has seen it end, because the bridge answers
 * a press at once (202) and the work happens behind that answer. "Ended" is an idle view AFTER
 * some sign the run happened: a busy phase was seen, or the view moved (`run_mark`), or an UPDATE
 * has nothing left to do or says why it stopped, or `RUN_SETTLE_MS` passed with none of those (a
 * press that had nothing to do). A FAILED READ IS NEVER THE SIGN: a whole run, restart included,
 * can fit between two reads, so the restart is known by its `boot_id`, not by its silence.
 *
 * NO ANSWER DURING A RUN IS THE RESTART, NOT AN ERROR. An update ends by restarting the bridge,
 * and for that stretch every read fails. So while a run is in flight an unanswered read keeps the
 * state and is drawn as "restarting the bridge", for at most `RESTART_WAIT_MS`. Past that the run
 * is given up as LOST, with a sentence, and the slow poll is what notices the bridge come back.
 *
 * THE PAGE ON SCREEN CAN BE OLDER THAN THE ONE SERVED. `page.id` names the served build, so the
 * first id a page load sees is its own, and a later, different id means a newer build is out.
 * Judged only on a page the bridge itself served (`tracks_page`): a host application's bundle, or
 * a rack pointed at another machine, did not come from that build and a reload would change nothing.
 */
import type { UpdateAction, UpdateReply, UpdateView } from './update-client.ts';

/** While a run is in flight. */
export const FAST_POLL_MS = 2_000;
/** While nothing is: the view moves only when the bridge's own timed check finds something. */
export const SLOW_POLL_MS = 5 * 60_000;
/** The least time between two reads set off by a window `focus` (`agent-rack/page-return.ts` owns why that one is noisy). */
export const FOCUS_READ_MS = 30_000;
/** How long silence during a run is believed to be the restart. The bridge takes up to about 90 s. */
export const RESTART_WAIT_MS = 120_000;
/** How long a press may show no sign of a run before it is taken to have had nothing to do. */
export const RUN_SETTLE_MS = 10_000;

const NOTICE_DETAIL_CHARS = 140;

export interface UpdateRun {
    action: UpdateAction;
    started_ms: number;
    /** `run_mark` of the view the run started from: a later idle view that differs is the run's result. */
    mark: string;
    seen_busy: boolean;
    boot_id: string | null;
}

/** What the last run this page started came to. */
export interface UpdateFinish {
    action: UpdateAction;
    restarted: boolean;
}

export interface UpdateState {
    view: UpdateView | null;
    /** The bridge's own no: a 404 or a 503. Then there is no view. */
    unsupported: { status: number; error: string } | null;
    /** May this page compare `page.id` with its own (the header owns the rule). */
    tracks_page: boolean;
    /** The served build this page load first saw: its own. */
    first_page_id: string | null;
    run: UpdateRun | null;
    finished: UpdateFinish | null;
    /** When the reads stopped bringing a view (ms), or null while they do. */
    no_view_since: number | null;
    /** A run was given up: the bridge stayed silent past `RESTART_WAIT_MS`. */
    lost: boolean;
    /** Why the last press did nothing, in the bridge's words. Stands until the next press. */
    press_error: string | null;
    /** Why the last read brought no view. Cleared by the next view. */
    read_error: string | null;
}

export function initial_update_state(tracks_page: boolean): UpdateState {
    return {
        view: null,
        unsupported: null,
        tracks_page,
        first_page_id: null,
        run: null,
        finished: null,
        no_view_since: null,
        lost: false,
        press_error: null,
        read_error: null,
    };
}

/** The fields a run changes when it does anything at all. */
export function run_mark(view: UpdateView | null): string {
    return view ? JSON.stringify([view.boot_id, view.checked_at, view.head, view.page.id, view.error, view.check_error]) : '';
}

/** A check or a run is going: one this page started, or one the bridge's view names. */
export function in_flight(state: UpdateState): boolean {
    if (state.lost) return false;
    return state.run !== null || (state.view !== null && state.view.phase !== 'idle');
}

/** A press was sent: follow it from the view on screen. */
export function after_press(state: UpdateState, action: UpdateAction, now_ms: number): UpdateState {
    return {
        ...state,
        run: { action, started_ms: now_ms, mark: run_mark(state.view), seen_busy: false, boot_id: state.view?.boot_id ?? null },
        finished: null,
        lost: false,
        press_error: null,
    };
}

function after_view(state: UpdateState, view: UpdateView, now_ms: number, from: 'read' | 'press'): UpdateState {
    const next: UpdateState = {
        ...state,
        view,
        unsupported: null,
        first_page_id: state.first_page_id ?? (state.tracks_page ? view.page.id : null),
        no_view_since: null,
        lost: false,
        read_error: null,
    };
    const run = state.run;
    if (!run) return next;
    const busy = view.phase !== 'idle';
    const seen_busy = run.seen_busy || busy;
    // The press's own answer is where the run starts from, never its result.
    if (from === 'press') {
        return { ...next, run: { ...run, seen_busy, mark: busy ? run.mark : run_mark(view), boot_id: run.boot_id ?? view.boot_id } };
    }
    const moved = run_mark(view) !== run.mark;
    const spent = run.action === 'apply' && (!view.available || view.error !== null || view.waiting !== null);
    if (busy || !(seen_busy || moved || spent || now_ms - run.started_ms >= RUN_SETTLE_MS)) return { ...next, run: { ...run, seen_busy } };
    const restarted = run.boot_id !== null && view.boot_id !== null && view.boot_id !== run.boot_id;
    return { ...next, run: null, finished: { action: run.action, restarted } };
}

const LOST_SENTENCE =
    'The bridge has not answered for two minutes, so this page stopped waiting. If it does not come back, start it: tldrack up';

/** One answer, from a timed read or from a press, folded into the state. */
export function after_reply(state: UpdateState, reply: UpdateReply, now_ms: number, from: 'read' | 'press' = 'read'): UpdateState {
    if (reply.kind === 'view') return after_view(state, reply.view, now_ms, from);
    if (reply.kind === 'unsupported') {
        return {
            ...state,
            view: null,
            unsupported: { status: reply.status, error: reply.error },
            run: null,
            no_view_since: null,
            lost: false,
            read_error: null,
        };
    }
    // The bridge said no to a press, or never heard it: nothing was started.
    if (from === 'press') {
        return { ...state, run: null, press_error: reply.error, no_view_since: reply.kind === 'silent' ? (state.no_view_since ?? now_ms) : state.no_view_since };
    }
    const flying = in_flight(state);
    // A refusal came from a bridge that is up; only a run in flight counts it against the wait.
    if (reply.kind === 'refused' && !flying) return { ...state, no_view_since: null, read_error: reply.error };
    const since = state.no_view_since ?? now_ms;
    if (!flying) return { ...state, no_view_since: since };
    if (now_ms - since < RESTART_WAIT_MS) return { ...state, no_view_since: since };
    return { ...state, run: null, lost: true, no_view_since: since, read_error: LOST_SENTENCE };
}

// ── when to ask again ────────────────────────────────────────────────────────

/** The wait before the next timed read, or null for no timer: a hidden page rests, and reads on its return. */
export function poll_delay_ms(state: UpdateState, hidden: boolean): number | null {
    if (hidden) return null;
    return in_flight(state) ? FAST_POLL_MS : SLOW_POLL_MS;
}

/** Does this return to the page read now? Being shown again always does; a `focus` only when the last read is old. */
export function reads_on_return(how: 'visible' | 'focus', since_last_read_ms: number): boolean {
    return how === 'visible' || since_last_read_ms >= FOCUS_READ_MS;
}

// ── the words ────────────────────────────────────────────────────────────────

export function commits(count: number): string {
    return `${count} commit${count === 1 ? '' : 's'}`;
}

/** The phase to draw: the view's, except that silence during a run is the restart. */
export function shown_phase(state: UpdateState): string {
    if (!in_flight(state)) return 'idle';
    if (state.no_view_since !== null) return 'restarting';
    const phase = state.view?.phase ?? 'idle';
    if (phase !== 'idle') return phase;
    return state.run?.action === 'check' ? 'checking' : 'starting';
}

/**
 * Is the work in flight a check and nothing more? Every run opens with a look at the remote, so
 * the phase alone cannot say: an UPDATE this page pressed is an update from its first moment.
 */
export function only_checking(state: UpdateState): boolean {
    return shown_phase(state) === 'checking' && state.run?.action !== 'apply';
}

/** A phase in words. One this page does not know is shown as the bridge spelled it. */
export function phase_words(phase: string, view: UpdateView | null): string {
    switch (phase) {
        case 'checking':
            return 'checking the remote';
        case 'pulling':
            return view && view.behind > 0 ? `pulling ${commits(view.behind)}` : 'pulling';
        case 'building':
            return 'building the page';
        case 'syncing':
            return 'installing bridge dependencies';
        case 'verifying':
            return 'checking that the new code starts';
        case 'restarting':
            return 'restarting the bridge';
        case 'starting':
            return 'starting';
        default:
            return phase;
    }
}

/**
 * What UPDATE will do, in order, from the view alone: only the steps that apply. A pull may change
 * the source of the page, of the bridge, or of neither, and only the bridge finds out which
 * (`bridge/tldrack/update.py`), so after a pull the other two are promised with their condition.
 * A page of unknown source is rebuilt only as part of a pull.
 */
export function plan_steps(view: UpdateView): string[] {
    const pulls = view.behind > 0 && !view.blocked;
    const steps: string[] = [];
    if (pulls) steps.push(`pull ${commits(view.behind)}`);
    if (view.page.source === 'stale') steps.push('rebuild the page');
    else if (pulls) steps.push('rebuild the page, if its source changed');
    if (view.code === 'stale') steps.push('restart the bridge');
    else if (pulls) steps.push('restart the bridge, if its code changed');
    return steps;
}

/** True when the bridge now serves a newer build of the page than the one on screen. */
export function page_is_newer(state: UpdateState): boolean {
    const served = state.view?.page.id ?? null;
    return state.first_page_id !== null && served !== null && served !== state.first_page_id;
}

function clip(text: string, chars: number): string {
    return text.length > chars ? `${text.slice(0, chars - 1)}…` : text;
}

/**
 * The bridge's sentences mark a name with backticks (a branch, a command). A surface that draws
 * markup takes the spans; one that draws plain text (the tray's line, a title) takes them unmarked.
 * An unpaired backtick is text.
 */
export interface SentenceSpan {
    text: string;
    code: boolean;
}

export function code_spans(sentence: string): SentenceSpan[] {
    const spans: SentenceSpan[] = [];
    let rest = sentence;
    for (;;) {
        const open = rest.indexOf('`');
        const close = open < 0 ? -1 : rest.indexOf('`', open + 1);
        if (close < 0) break;
        if (open > 0) spans.push({ text: rest.slice(0, open), code: false });
        if (close > open + 1) spans.push({ text: rest.slice(open + 1, close), code: true });
        rest = rest.slice(close + 1);
    }
    if (rest) spans.push({ text: rest, code: false });
    return spans;
}

export function without_code_marks(sentence: string): string {
    return code_spans(sentence)
        .map((span) => span.text)
        .join('');
}

export type UpdateLevel = 'none' | 'ready' | 'held' | 'busy';

/**
 * The one thing about updates worth the operator's eye, or null for nothing. The ⚙'s dot is its
 * `level`, and the tray's line and the ⚙'s title are its words: one source, so the colour and
 * the sentence cannot disagree.
 *
 *   busy   a check or a run is in flight
 *   ready  a press would do something: UPDATE, or RELOAD for a newer page build
 *   held   something is in the way: the last run failed, a run waits, or a pull is blocked
 */
export interface UpdateNotice {
    level: Exclude<UpdateLevel, 'none'>;
    label: string;
    detail: string | null;
    /** What the line's button does: open the settings page's update card, or reload this page. */
    action: 'settings' | 'reload';
}

export function update_notice(state: UpdateState): UpdateNotice | null {
    const view = state.view;
    if (!view || !view.supported) return null;
    if (in_flight(state)) {
        if (only_checking(state)) return { level: 'busy', label: 'CHECKING FOR UPDATES', detail: null, action: 'settings' };
        return { level: 'busy', label: 'UPDATING', detail: phase_words(shown_phase(state), view), action: 'settings' };
    }
    if (page_is_newer(state)) return { level: 'ready', label: 'NEW PAGE BUILD', detail: null, action: 'reload' };
    const held = (label: string, detail: string): UpdateNotice => ({
        level: 'held',
        label,
        detail: clip(without_code_marks(detail), NOTICE_DETAIL_CHARS),
        action: 'settings',
    });
    if (view.error) return held('UPDATE FAILED', view.error);
    if (view.waiting) return held('UPDATE WAITING', view.waiting);
    if (view.available) {
        const detail = view.behind > 0 && !view.blocked ? commits(view.behind) : plan_steps(view).join(' · ') || null;
        return { level: 'ready', label: 'UPDATE READY', detail, action: 'settings' };
    }
    if (view.behind > 0 && view.blocked) return held('UPDATE HELD', view.blocked);
    return null;
}

export function update_level(state: UpdateState): UpdateLevel {
    return update_notice(state)?.level ?? 'none';
}

/** The tray's line: `UPDATE READY · 3 commits`. */
export function notice_line(notice: UpdateNotice): string {
    return notice.detail ? `${notice.label} · ${notice.detail}` : notice.label;
}

/** The same as a clause for a title or an accessible name: `update ready: 3 commits`. */
export function notice_words(notice: UpdateNotice): string {
    const label = notice.label.toLowerCase();
    return notice.detail ? `${label}: ${notice.detail}` : label;
}

/** `checked 2 min ago`, from the bridge's unix seconds. */
export function checked_words(checked_at: number | null, now_ms: number): string {
    if (checked_at === null) return 'never checked';
    const minutes = Math.floor((now_ms / 1000 - checked_at) / 60);
    if (minutes < 1) return 'checked just now';
    if (minutes < 60) return `checked ${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `checked ${hours} h ago`;
    return `checked ${Math.floor(hours / 24)} days ago`;
}

export interface UpdateHeadline {
    /** The short word beside the card's title. */
    tag: string;
    tone: 'ok' | 'warn' | 'plain';
    line: string;
}

function stale_words(view: UpdateView): string {
    const parts: string[] = [];
    if (view.page.source === 'stale') parts.push('the served page is older than its source');
    if (view.code === 'stale') parts.push('the running bridge is older than the code on disk');
    return parts.join(', and ') || 'there is work to do';
}

/** The card's state in words, or null before the first view. */
export function update_headline(state: UpdateState, now_ms: number): UpdateHeadline | null {
    const view = state.view;
    if (!view || !view.supported) return null;
    if (in_flight(state)) {
        if (only_checking(state)) return { tag: 'CHECKING', tone: 'plain', line: 'Checking the remote…' };
        return { tag: 'UPDATING', tone: 'plain', line: `Updating: ${phase_words(shown_phase(state), view)}…` };
    }
    const where = `${view.branch ?? 'no branch'} @ ${view.head ?? 'unknown'}`;
    const checked = checked_words(view.checked_at, now_ms);
    const behind = `${commits(view.behind)} behind ${view.upstream ?? 'its remote'}`;
    if (view.error) return { tag: 'FAILED', tone: 'warn', line: `The last update failed · ${where} · ${checked}` };
    if (view.behind > 0 && view.blocked) return { tag: 'HELD', tone: 'warn', line: `Held: ${view.blocked} · ${behind} · ${checked}` };
    if (view.behind > 0) return { tag: view.available ? 'UPDATE READY' : 'BEHIND', tone: 'plain', line: `${behind} · ${where} · ${checked}` };
    if (view.available) return { tag: 'UPDATE READY', tone: 'plain', line: `${where} has every commit, but ${stale_words(view)} · ${checked}` };
    if (view.checked_at === null) return { tag: 'NOT CHECKED', tone: 'plain', line: `Not checked yet · ${where}` };
    return { tag: 'UP TO DATE', tone: 'ok', line: `Up to date · ${where} · ${checked}` };
}

/** One line for the run this page started and saw end, or null when the view's own sentences say it. */
export function finished_words(state: UpdateState): string | null {
    const view = state.view;
    if (!state.finished || !view) return null;
    if (state.finished.action === 'check') {
        if (view.check_error) return null;
        return view.behind > 0 ? `Checked: ${commits(view.behind)} to pull.` : 'Checked: nothing new.';
    }
    // A failure, or a restart that is waiting for something, is the view's to say.
    if (view.error || view.waiting) return null;
    const at = view.head ? ` Now at ${view.branch ?? 'no branch'} @ ${view.head}.` : '';
    return `Update finished.${at}${state.finished.restarted ? ' The bridge restarted.' : ''}`;
}
