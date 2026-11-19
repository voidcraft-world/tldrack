/**
 * The RECENT list's rules — the marks, the grading, the tally, the keys — pure, so they run under bare `node --test`.
 * `LayaRecentList.vue` owns only the fetches and the DOM; the wire is `schemas/src/laya-review.ts`
 * (`reviewRecentSchema`).
 *
 * The deck asks "agree with this reference?"; the list asks the plainer question the rack poses: was this turn DONE,
 * still RUNNING on its own, or did it NEED YOU? A button names a verdict, never AGREE or FLIP, so `answer_request` maps
 * it onto the reference the plugin holds — and a reference that moved meanwhile maps again without changing what the
 * operator said. RUNNING and NEEDS YOU both record OPEN; `needs` is the half that says which (schemas/src/laya-review.ts
 * `REVIEW_NEEDS` owns why).
 */
import type { ReviewAnswer, ReviewAnswerRequest, ReviewBand, ReviewNeeds, ReviewRecentOrder, ReviewRecentRow } from '@tldrack/schemas/laya-review.ts';

import { ANSWER_TO_READ_EMOJI, OPEN_WORK_BADGE, WORK_DONE_EMOJI, display_state_emoji } from '../config/terminal-status-flags.ts';

/** The rack's own marks, from their owner — the list must draw exactly what the operator saw. */
export const BAND_MARK: Readonly<Record<ReviewBand, string>> = {
    done: WORK_DONE_EMOJI.done,
    reply: display_state_emoji('done'),
    open: OPEN_WORK_BADGE,
};

export const BAND_WORDS: Readonly<Record<ReviewBand, string>> = {
    done: 'Laya: the work looks done',
    reply: 'Laya: unsure — the plain reply mark',
    open: 'Laya: planned work still open',
};

/**
 * The mark a row draws: the band's, except that a ✅ on a turn the operator opened with a question draws 📖, as the
 * rack does for an answer to read (`config/terminal-status-flags.ts` owns the mark). `asked` is the bridge's reading
 * of the whole prompt (`bridge/tldrack/ledger/reader.py` `turn_asked`). Here 📖 stays once read: the list records what
 * each turn was, and this one was an answer.
 */
export function row_mark(band: ReviewBand, asked: boolean | null | undefined): string {
    return band === 'done' && asked === true ? ANSWER_TO_READ_EMOJI : BAND_MARK[band];
}

/** The row mark's words, for its accessible name: the band's, or the answer's. */
export function row_mark_words(band: ReviewBand, asked: boolean | null | undefined): string {
    return band === 'done' && asked === true ? 'Laya: the work looks done · it answered your question' : BAND_WORDS[band];
}

/** What the operator says a turn was, by press. `skip` leaves it out of every count, as on the deck. */
export type RecentVerdict = 'done' | 'running' | 'you' | 'skip';
/** What a live answer reads back as: a press, or `open` for an OPEN answer recorded without its half (the deck's). */
export type OperatorVerdict = RecentVerdict | 'open';

/** The four grades, as their buttons draw them: `title` is the tooltip, `caption` the line EXPLAIN prints under it. */
export const RECENT_VERDICTS: readonly { verdict: RecentVerdict; label: string; key: string; title: string; caption: string }[] = [
    { verdict: 'done', label: '✓ DONE', key: 'D', title: 'The turn finished what it set out to do', caption: 'It finished what it set out to do' },
    {
        verdict: 'running', label: '⏳ RUNNING', key: 'R',
        title: 'Work still open, but the agent was only waiting on its own agents or jobs: you were not needed',
        caption: 'Still open, but only waiting on its own agents or jobs',
    },
    {
        verdict: 'you', label: '⚠ NEEDS YOU', key: 'N',
        title: 'Work still open and waiting on you: a question, a decision, a go-ahead, or blocked',
        caption: 'It needs your answer, a decision or a go-ahead',
    },
    { verdict: 'skip', label: 'SKIP', key: 'S', title: 'Ambiguous: no label, and it leaves training too', caption: 'Ambiguous: no label, and it stays out of training' },
];

/** A graded turn's compact chip, `✓ DONE · RIGHT`, in place of its buttons; null while ungraded. */
export function verdict_chip(row: Pick<ReviewRecentRow, 'band' | 'operator'>): { label: string; grade: 'right' | 'wrong' | null } | null {
    const verdict = operator_verdict(row);
    if (verdict === null) return null;
    const label = verdict === 'open' ? 'OPEN' : RECENT_VERDICTS.find((v) => v.verdict === verdict)?.label ?? verdict.toUpperCase();
    return { label, grade: grade(row) };
}

/** The chip as one line, for the status line after a press: `✓ DONE · RIGHT`. */
export function chip_text(chip: NonNullable<ReturnType<typeof verdict_chip>>): string {
    return chip.grade ? `${chip.label} · ${chip.grade.toUpperCase()}` : chip.label;
}

/** Display names never replace the stable rack identity used by filters and answer requests. */
export function review_title(subject?: string | null, rack_title?: string | null, conversation_title?: string | null): string {
    return rack_title?.trim() || conversation_title?.trim() || subject?.trim() || 'Conversation';
}

const NEEDS_OF: Readonly<Partial<Record<RecentVerdict, ReviewNeeds>>> = { running: 'running', you: 'you' };

export function is_open_verdict(verdict: OperatorVerdict | null): boolean {
    return verdict === 'running' || verdict === 'you' || verdict === 'open';
}

/**
 * The `/review/answer` body for a verdict, against the reference the row (or a fresh card) carries; null when no card
 * holds the text, so it cannot be answered at all.
 */
export function answer_request(row: Pick<ReviewRecentRow, 'key'>, surface: string, verdict: RecentVerdict, reference_open: boolean | null): ReviewAnswerRequest | null {
    if (reference_open === null) return null;
    const answer: ReviewAnswer = verdict === 'skip' ? 'skip' : is_open_verdict(verdict) === reference_open ? 'agree' : 'flip';
    const needs = NEEDS_OF[verdict];
    return needs ? { surface, key: row.key, answer, reference_open, needs } : { surface, key: row.key, answer, reference_open };
}

/** The operator's verdict on a row, read back from its live answer. */
export function operator_verdict(row: Pick<ReviewRecentRow, 'operator'>): OperatorVerdict | null {
    const f = row.operator;
    if (!f) return null;
    if (f.answer === 'skip' || f.open === null) return 'skip';
    if (!f.open) return 'done';
    return f.needs === 'running' ? 'running' : f.needs === 'you' ? 'you' : 'open';
}

/** RIGHT or WRONG against the band; a 💬 row has no right answer (it said "unsure"), so it grades as null. The band
 *  only says open or done, so RUNNING and NEEDS YOU both grade as open. */
export function grade(row: Pick<ReviewRecentRow, 'band' | 'operator'>): 'right' | 'wrong' | null {
    const verdict = operator_verdict(row);
    if (verdict === null || verdict === 'skip' || row.band === 'reply') return null;
    return is_open_verdict(verdict) === (row.band === 'open') ? 'right' : 'wrong';
}

export interface BandTally {
    n: number;
    reviewed: number;
    right: number;
    open: number;
}

export interface RecentTally {
    n: number;
    reviewed: number;
    skipped: number;
    bands: Record<ReviewBand, BandTally>;
    /** The OPEN answers split: who the turn was really waiting on (`review_data.py` summary `needs`). */
    needs: Record<ReviewNeeds, number>;
}

/** The strip's counts, over the rows on the page. A port of `review_data.py` `recent_rows`'s summary (the owner), run
 *  here so an answer updates the strip without a refetch that would reorder the list under the cursor. */
export function recent_tally(rows: readonly ReviewRecentRow[]): RecentTally {
    const blank = (): BandTally => ({ n: 0, reviewed: 0, right: 0, open: 0 });
    const bands: Record<ReviewBand, BandTally> = { done: blank(), reply: blank(), open: blank() };
    const needs: Record<ReviewNeeds, number> = { you: 0, running: 0 };
    let reviewed = 0;
    let skipped = 0;
    for (const row of rows) {
        const t = bands[row.band];
        t.n += 1;
        const verdict = operator_verdict(row);
        if (verdict === null) continue;
        if (verdict === 'skip') {
            skipped += 1;
            continue;
        }
        reviewed += 1;
        t.reviewed += 1;
        if (is_open_verdict(verdict)) t.open += 1;
        if (verdict === 'running' || verdict === 'you') needs[verdict] += 1;
        if (grade(row) === 'right') t.right += 1;
    }
    return { n: rows.length, reviewed, skipped, bands, needs };
}

export interface BandReadout {
    band: ReviewBand;
    mark: string;
    text: string;
    title: string;
    /** Amber once a graded band is wrong more than it may be (`WRONG_WARN`); never on 💬. */
    warn: boolean;
}

/** Past this share of wrong answers a ✅ or ⚠️ band's readout turns amber: one in five is worth a retrain. */
export const WRONG_WARN = 0.2;

/** `✅ 18/20` — right over graded; 💬 says how the unsure turns split, which is what moves the done cut. */
export function band_readouts(tally: RecentTally): BandReadout[] {
    return (['done', 'reply', 'open'] as const).map((band) => {
        const t = tally.bands[band];
        const mark = BAND_MARK[band];
        if (band === 'reply') {
            const done = t.reviewed - t.open;
            return {
                band, mark, warn: false,
                text: t.reviewed ? `${done} done · ${t.open} open` : `${t.n}`,
                title: `${t.n} unsure turns; you called ${done} done and ${t.open} open — what moves the ✅ cut`,
            };
        }
        const wrong = t.reviewed - t.right;
        return {
            band, mark, warn: t.reviewed >= 5 && wrong / t.reviewed > WRONG_WARN,
            text: t.reviewed ? `${t.right}/${t.reviewed}` : `${t.n}`,
            title: t.reviewed ? `${t.n} drawn; ${t.right} of ${t.reviewed} you graded were right` : `${t.n} drawn; none graded yet`,
        };
    });
}

/** `NEEDS YOU 3/12`: of the OPEN turns you split, how many really needed you — the number the ⏳ mark and the NEEDS-YOU
 *  question are built on. Null until one is split. */
export function needs_readout(tally: RecentTally): { text: string; title: string } | null {
    const split = tally.needs.you + tally.needs.running;
    if (split === 0) return null;
    return {
        text: `${tally.needs.you}/${split}`,
        title: `Of ${split} open turns you split, ${tally.needs.you} needed you and ${tally.needs.running} were only waiting on their own work`,
    };
}

/** The narrowing a band readout applies; null shows every row. */
export function visible_rows(rows: readonly ReviewRecentRow[], band: ReviewBand | null, unanswered_only: boolean): ReviewRecentRow[] {
    return rows.filter((row) => (band === null || row.band === band) && (!unanswered_only || operator_verdict(row) === null));
}

/** Rows of `key` still unanswered: what one answer takes out of TO GRADE's server pool. */
export function open_rows_of(rows: readonly ReviewRecentRow[], key: string): number {
    return rows.filter((row) => row.key === key && operator_verdict(row) === null).length;
}

/** Every row of the answered text takes the new answer: a text judged twice is one key. */
export function with_answer(rows: readonly ReviewRecentRow[], key: string, operator: ReviewRecentRow['operator']): ReviewRecentRow[] {
    return rows.map((row) => (row.key === key ? { ...row, operator } : row));
}

/** `4m`, `3h`, `2d` — how long ago, as a short mono readout; the ISO stamp goes in the title. */
export function age_text(at: string, now_ms: number): string {
    const then = Date.parse(at);
    if (!Number.isFinite(then)) return '—';
    const s = Math.max(0, Math.round((now_ms - then) / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86_400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86_400)}d`;
}

/** `age_text` for an epoch-seconds stamp (the session ledger's `ts`); null when there is none. */
export function ts_age_text(ts: number | null | undefined, now_ms: number): string | null {
    return ts === null || ts === undefined || !Number.isFinite(ts) ? null : age_text(new Date(ts * 1000).toISOString(), now_ms);
}

/** Within this of a cut a row is an edge case, and says so beside its score in either order. Five hundredths is the
 *  sweep grid's own step, so a label here is one a RE-SWEEP can act on. Which cut is nearest: `review_data.py` `edge_of`. */
export const EDGE_NEAR = 0.05;

const CUT_MARK: Readonly<Record<'open' | 'done', string>> = { open: OPEN_WORK_BADGE, done: WORK_DONE_EMOJI.done };

/** `0.02 FROM ⚠️` when p falls just short of a mark, `⚠️ BY 0.01` when it only just made it; null away from a cut, or
 *  from an older bridge that sends no `edge`. A RECENT row, or a deck card's edge beside its Laya p (`card_edge`). */
export function edge_note(row: Pick<ReviewRecentRow, 'edge' | 'p'>): { text: string; title: string; cut: number } | null {
    const edge = row.edge;
    if (!edge || Math.abs(edge.delta) > EDGE_NEAR) return null;
    const mark = CUT_MARK[edge.cut];
    const gap = Math.abs(edge.delta).toFixed(2);
    // ⚠️ is at or over its cut; ✅ is strictly under its own.
    const made_it = edge.cut === 'open' ? edge.delta >= 0 : edge.delta < 0;
    const cut = row.p - edge.delta;
    const at = cut.toFixed(2);
    return made_it
        ? { cut, text: `${mark} BY ${gap}`, title: `p ${row.p.toFixed(3)}: drew ${mark} by ${gap} past its cut (${at}) — one nudge from the plain 💬` }
        : { cut, text: `${gap} FROM ${mark}`, title: `p ${row.p.toFixed(3)}: ${gap} short of ${mark} (its cut is ${at}) — one nudge from drawing it` };
}

/** What the RECENT list is showing, as one line, when EXPLAIN is on. `pool` is how many verdicts the page was cut from. */
export function explain_order(order: ReviewRecentOrder, todo: boolean, n: number, pool: number | null): string {
    const of = pool === null ? `${n}` : `${n} of ${pool.toLocaleString('en-US')}`;
    const which = todo ? 'ungraded verdicts' : 'verdicts';
    return order === 'edge'
        ? `EDGE: the ${of} ${which} whose score sits nearest a cut, closest first — the turns one nudge from another mark. ` +
              'Grading these moves the cuts on RE-SWEEP and teaches a fine-tune the most.'
        : `NEWEST: the ${of} ${which}, newest first. Press ⚠️ for the latest warnings, or EDGE for the near misses.`;
}

/** The mark the rack drew, when today's band differs — `was 💬` on a pre-band verdict that would be ✅ now. */
export function drawn_note(row: Pick<ReviewRecentRow, 'mark' | 'band'>): string | null {
    return row.mark === row.band ? null : `was ${BAND_MARK[row.mark]}`;
}

/** `next` / `prev` walk the turns of the conversation shown; `next_conv` / `prev_conv` walk the conversations. */
export type RecentAction = RecentVerdict | 'undo' | 'next' | 'prev' | 'next_conv' | 'prev_conv' | 'expand' | 'edge';

const RECENT_KEYS: ReadonlyMap<string, RecentAction> = new Map([
    ['KeyD', 'done'],
    ['KeyR', 'running'],
    ['KeyN', 'you'],
    ['KeyS', 'skip'],
    ['KeyU', 'undo'],
    ['KeyJ', 'next'],
    ['ArrowDown', 'next'],
    ['KeyK', 'prev'],
    ['ArrowUp', 'prev'],
    ['ArrowRight', 'next_conv'],
    ['ArrowLeft', 'prev_conv'],
    ['Enter', 'expand'],
    ['KeyE', 'edge'],
]);

/** The parts of a `KeyboardEvent` the list reads. */
export interface RecentKeyEvent {
    code: string;
    metaKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    shiftKey: boolean;
    repeat: boolean;
    target: EventTarget | null;
}

/** What scrolls inside itself on the page: a code block, a table, an opened paste (`AgentMessage.vue`). */
const OWN_SCROLLERS = 'pre, table, .am-paste__text';

/** The parts of an element `scrolls_itself` reads, so a test can stub one. */
export interface ScrollTarget {
    tagName?: unknown;
    closest?: (selector: string) => unknown;
    scrollWidth?: number;
    clientWidth?: number;
    scrollHeight?: number;
    clientHeight?: number;
}

/** Whether an arrow key on `el` belongs to a scroller of its own: inside a code block, a table or an opened paste, or
 *  an element that overflows on the key's axis. The page itself is not one (its arrows are the list's). */
export function scrolls_itself(el: ScrollTarget | null | undefined, code: string): boolean {
    if (!el || !code.startsWith('Arrow') || el.tagName === 'BODY' || el.tagName === 'HTML') return false;
    if (typeof el.closest === 'function' && el.closest(OWN_SCROLLERS)) return true;
    const sideways = code === 'ArrowLeft' || code === 'ArrowRight';
    const [full, box] = sideways ? [el.scrollWidth, el.clientWidth] : [el.scrollHeight, el.clientHeight];
    return typeof full === 'number' && typeof box === 'number' && full > box + 1;
}

/** D, R, N, S, U, J/K or ↓/↑, ← →, Enter, E — plain keys only, never a held one, never while a field is typed in. A
 *  held ↓/↑ may repeat: it only moves the cursor. A held ← → does not: it would fly past every conversation. An arrow
 *  inside something that scrolls itself scrolls it (`scrolls_itself`); J/K still move from anywhere. */
export function recent_key(e: RecentKeyEvent): RecentAction | null {
    if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return null;
    const el = e.target as ({ isContentEditable?: unknown } & ScrollTarget) | null;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable === true)) return null;
    if (e.code === 'Enter' && el && (el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'SUMMARY')) return null;
    if (scrolls_itself(el, e.code)) return null;
    const action = RECENT_KEYS.get(e.code) ?? null;
    if (e.repeat && action !== 'next' && action !== 'prev') return null;
    return action;
}
