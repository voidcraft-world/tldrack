/**
 * RECENT's conversations: which rows are one conversation, the order the pager walks them in, and how a conversation
 * names, marks and groups itself. Pure, so it runs under bare `node --test`; `LayaRecentList.vue` draws ONE
 * conversation at a time (◀ ▶ / ← → walk them), `LayaThreadHeader.vue` a head, and the deck borrows a head for its card
 * (`card_source`).
 *
 * One at a time because a page of interleaved bands left the operator unable to tell whether it showed one
 * conversation or several. A page now answers that by construction, and the pager's `3 / 21` says how many there are.
 *
 * A conversation is one THREAD, keyed, in order of trust, by:
 * 1. the join's `turn.thread`: the conversation id a resume chain started under. A resumed Claude conversation gets a
 *    NEW session id, so keying by the raw id would split it and read "new conversation in vc-8" (the bridge's
 *    `ledger/laya_links.py` owns how a chain is found);
 * 2. the row's raw `conversation`, mapped through the join's `conversations` when it can: a verdict the ledger has not
 *    indexed yet still lands at once, beside its thread's older turns;
 * 3. neither (an old verdict the linker gave up on, or a bridge without the join): the rack name, split wherever
 *    `UNLINKED_GAP_S` passes between turns. Such a conversation says NOT LINKED, because a rack name is reused by
 *    every agent.
 *
 * Inside a conversation the turns read in the order they happened, oldest first, grouped under the prompt each one
 * answered (`turn_groups`): a prompt that woke five turn ends prints once, not five times. The pager walks
 * conversations newest first by their newest turn; in EDGE order, by the server's rank of their nearest-a-cut turn.
 */
import type { HistoryClip, HistoryThread, HistoryThreads, HistoryTurn } from '@tldrack/schemas/agent-history.ts';
import type { ReviewRecentOrder, ReviewRecentRow } from '@tldrack/schemas/laya-review.ts';

import { operator_verdict, ts_age_text } from './recent-model.ts';
import { awaits_ingest } from './turn-context.ts';

/** Turns of one rack name further apart than this are not assumed to be one conversation. */
export const UNLINKED_GAP_S = 30 * 60;

export type BandKind = 'thread' | 'unlinked';

/** What a head is drawn from: a band of rows, or (the deck) one card's turn. */
export interface HeadSource {
    /** What the badge and the hue hash: the thread key, or the unlinked run's own key. */
    identity: string;
    kind: BandKind;
    /** The thread's header from the join, once the ledger knows it. */
    thread: HistoryThread | null;
    /** The rack name on the band's newest turn. */
    subject: string | null;
    /** The rack name, when an older conversation on this page held it too. */
    new_in_rack: string | null;
    /** The join answered, but the ledger has not caught up with this band yet. */
    indexing: boolean;
    /** Epoch ms of the oldest and newest turn shown. */
    span: readonly [number, number] | null;
}

export interface ThreadBand extends HeadSource {
    /** The band's identity: one conversation, one key. */
    key: string;
    rows: ReviewRecentRow[];
}

function at_ms(row: Pick<ReviewRecentRow, 'at'>): number {
    const t = Date.parse(row.at);
    return Number.isFinite(t) ? t : 0;
}

/** The thread a row belongs to, or null when nothing ties it to one (see the header). */
export function thread_key(row: Pick<ReviewRecentRow, 'decision_id' | 'conversation'>, join: HistoryThreads | null): string | null {
    const linked = join?.decisions[row.decision_id]?.thread;
    if (linked) return linked;
    const raw = row.conversation;
    if (!raw) return null;
    return join?.conversations[raw] ?? raw;
}

/** Every row's identity: its thread, or its unlinked run (rack name + gap, walked newest first). */
export function row_identities(rows: readonly ReviewRecentRow[], join: HistoryThreads | null): Map<string, { identity: string; kind: BandKind }> {
    const out = new Map<string, { identity: string; kind: BandKind }>();
    const loose = new Map<string, ReviewRecentRow[]>();
    for (const row of rows) {
        const key = thread_key(row, join);
        if (key !== null) out.set(row.decision_id, { identity: `t:${key}`, kind: 'thread' });
        else loose.set(row.subject ?? '', [...(loose.get(row.subject ?? '') ?? []), row]);
    }
    for (const [subject, list] of loose) {
        const sorted = [...list].sort((a, b) => at_ms(b) - at_ms(a));
        let run: ReviewRecentRow[] = [];
        const close = () => {
            // Keyed by the run's OLDEST turn, so a fresh verdict joining it keeps its mark.
            const identity = `u:${subject}@${at_ms(run[run.length - 1])}`;
            for (const r of run) out.set(r.decision_id, { identity, kind: 'unlinked' });
            run = [];
        };
        for (const row of sorted) {
            if (run.length && at_ms(run[run.length - 1]) - at_ms(row) > UNLINKED_GAP_S * 1000) close();
            run.push(row);
        }
        if (run.length) close();
    }
    return out;
}

/** Per identity, the rack name it was the NEWER conversation in: the earliest-started conversation of a rack name is
 *  the one it was not new in. Unlinked runs neither mark nor get marked, since nothing says they differ. */
function new_in_rack(rows: readonly ReviewRecentRow[], ids: Map<string, { identity: string; kind: BandKind }>, join: HistoryThreads | null): Map<string, string> {
    const start = new Map<string, number>();
    const by_rack = new Map<string, Set<string>>();
    for (const row of rows) {
        const id = ids.get(row.decision_id);
        if (!id || id.kind !== 'thread') continue;
        start.set(id.identity, Math.min(start.get(id.identity) ?? Infinity, at_ms(row)));
        if (row.subject) by_rack.set(row.subject, (by_rack.get(row.subject) ?? new Set()).add(id.identity));
    }
    for (const identity of start.keys()) {
        const began = join?.threads[identity.slice(2)]?.started_at;
        if (began !== null && began !== undefined) start.set(identity, began * 1000);
    }
    const marks = new Map<string, string>();
    for (const [rack, set] of by_rack) {
        if (set.size < 2) continue;
        const [, ...newer] = [...set].sort((a, b) => (start.get(a) ?? 0) - (start.get(b) ?? 0));
        for (const identity of newer) if (!marks.has(identity)) marks.set(identity, rack);
    }
    return marks;
}

/** The page's conversations, in the pager's order (see the header). `rows` arrive in the server's order, which is the
 *  EDGE rank. `join` null: no join (yet, or an older bridge). */
export function thread_bands(rows: readonly ReviewRecentRow[], join: HistoryThreads | null, order: ReviewRecentOrder): ThreadBand[] {
    const ids = row_identities(rows, join);
    const marks = new_in_rack(rows, ids, join);
    const groups = new Map<string, ReviewRecentRow[]>();
    const rank = new Map<string, number>();
    rows.forEach((row, i) => {
        const { identity } = ids.get(row.decision_id) as { identity: string };
        groups.set(identity, [...(groups.get(identity) ?? []), row]);
        if (!rank.has(identity)) rank.set(identity, i);
    });
    const bands = [...groups].map(([identity, list]): ThreadBand => {
        const kind = (ids.get(list[0].decision_id) as { kind: BandKind }).kind;
        const thread = kind === 'thread' ? join?.threads[identity.slice(2)] ?? null : null;
        const times = list.map(at_ms);
        return {
            key: identity, identity, kind, thread,
            // Oldest first: a conversation reads in the order it happened. sort() is stable, so a tie keeps the server's order.
            rows: [...list].sort((a, b) => at_ms(a) - at_ms(b)),
            subject: list.reduce((newest, r) => (at_ms(r) >= at_ms(newest) ? r : newest)).subject ?? null,
            new_in_rack: marks.get(identity) ?? null,
            indexing: join !== null && (kind === 'thread' ? thread === null : list.some((r) => {
                const turn = join?.decisions[r.decision_id];
                return turn !== undefined && awaits_ingest(turn, 'decision');
            })),
            span: [Math.min(...times), Math.max(...times)],
        };
    });
    return order === 'edge'
        ? bands.sort((a, b) => (rank.get(a.identity) ?? 0) - (rank.get(b.identity) ?? 0))
        : bands.sort((a, b) => (b.span?.[1] ?? 0) - (a.span?.[1] ?? 0));
}

// ── the pager: where a conversation opens, and where grading moves on to ──────

/** Turns still to grade in a conversation. */
export function todo_count(band: Pick<ThreadBand, 'rows'>): number {
    return band.rows.filter((r) => operator_verdict(r) === null).length;
}

/** The turn a conversation opens on: its first ungraded turn, else its newest. */
export function opening_row(band: Pick<ThreadBand, 'rows'>): ReviewRecentRow | null {
    return band.rows.find((r) => operator_verdict(r) === null) ?? band.rows.at(-1) ?? null;
}

/** After a grade: the next ungraded turn after `from`, else an earlier one still ungraded; null when every turn of the
 *  conversation is graded (the pager then moves on: `after_grade`). By place in time, so the answer is the same
 *  whether the graded turn is still in the band or TO GRADE already took it out. */
export function next_todo(band: Pick<ThreadBand, 'rows'>, from: Pick<ReviewRecentRow, 'decision_id' | 'at'>): ReviewRecentRow | null {
    const rows = band.rows;
    const own = rows.findIndex((r) => r.decision_id === from.decision_id);
    const later = rows.findIndex((r) => at_ms(r) > at_ms(from));
    // Its own index when present; else where it would sit (rows are oldest first, a tie sorts before it).
    const at = own >= 0 ? own : later < 0 ? rows.length : later;
    const open = (r: ReviewRecentRow) => operator_verdict(r) === null && r.decision_id !== from.decision_id;
    return rows.slice(own >= 0 ? at + 1 : at).find(open) ?? rows.slice(0, at).find(open) ?? null;
}

/** The next conversation after `from` with a turn to grade; forward only, so a conversation skipped with ← → is never
 *  jumped back to. Null when none is left on the page. */
export function next_todo_band(bands: readonly Pick<ThreadBand, 'rows'>[], from: number): number | null {
    for (let i = from + 1; i < bands.length; i++) if (todo_count(bands[i]) > 0) return i;
    return null;
}

/** `at` itself while it still has a turn to grade, else the next one that has (`next_todo_band`). What NEXT TO GRADE
 *  and a grade check after an older page lands: that page's turns of the conversation shown join IT, not a new one. */
export function next_todo_from(bands: readonly Pick<ThreadBand, 'rows'>[], at: number): number | null {
    return at >= 0 && at < bands.length && todo_count(bands[at]) > 0 ? at : next_todo_band(bands, at);
}

/** The nearest conversation before `at` with a turn to grade: what is left once nothing ahead is. */
export function todo_behind(bands: readonly Pick<ThreadBand, 'rows'>[], at: number): number | null {
    for (let i = Math.min(at, bands.length) - 1; i >= 0; i--) if (todo_count(bands[i]) > 0) return i;
    return null;
}

/** The conversation `ids` (its turns, as they were) and `key` stood for, where it is now; -1 when it left the page. By
 *  any of its turns first: a key can move (an unlinked run is keyed by its oldest turn, and older turns joining it
 *  change that), and a filter can hide the one turn the cursor was on. */
export function band_holding(bands: readonly Pick<ThreadBand, 'rows' | 'key'>[], ids: readonly string[], key: string | null): number {
    const wanted = new Set(ids);
    const by_turn = wanted.size ? bands.findIndex((b) => b.rows.some((r) => wanted.has(r.decision_id))) : -1;
    return by_turn >= 0 || key === null ? by_turn : bands.findIndex((b) => b.key === key);
}

/** Where the pager stands: the cursor turn's conversation; when that turn left the page (a filter, TO GRADE), the
 *  conversation that held it, if it is still here; else the same place in the list. */
export function pager_at(
    bands: readonly Pick<ThreadBand, 'rows' | 'key'>[],
    place: { cursor: string | null; ids: readonly string[]; key: string | null; last: number },
): number {
    const own = place.cursor === null ? -1 : band_holding(bands, [place.cursor], null);
    if (own >= 0) return own;
    const held = band_holding(bands, place.ids, place.key);
    return held >= 0 ? held : Math.min(place.last, Math.max(0, bands.length - 1));
}

/** The conversation a fresh page opens on: the first with a turn to grade, else the first. */
export function opening_band(bands: readonly Pick<ThreadBand, 'rows'>[]): number {
    const i = bands.findIndex((b) => todo_count(b) > 0);
    return i < 0 ? 0 : i;
}

/** A grade, as the pager needs it to move on: the conversation's turns before the answer (any one still on the page
 *  finds it again) and its key, the graded turn, and where the conversation stood. */
export interface GradedAt {
    ids: readonly string[];
    key: string | null;
    row: Pick<ReviewRecentRow, 'decision_id' | 'at'>;
    at: number;
}

/** Where a grade moves the cursor: `same`, the conversation's next open turn; `next`, a later conversation (`back`:
 *  ← returns to the one just finished, which is still the one before it); `behind`, nothing ahead but an earlier
 *  conversation still has turns; `none`, nothing on the page. */
export type AfterGrade =
    | { kind: 'same'; band: number; row: ReviewRecentRow }
    | { kind: 'next'; band: number; back: boolean }
    | { kind: 'behind'; band: number; held: boolean }
    | { kind: 'none'; held: boolean };

/** Forward from a grade, on the page as it is now: null when nothing lies ahead on it, so the caller loads an older
 *  page and asks again (that page's turns of the graded conversation join it, so it is checked first), then settles
 *  for `left_after_grade`. */
export function after_grade(bands: readonly Pick<ThreadBand, 'rows' | 'key'>[], graded: GradedAt): Extract<AfterGrade, { kind: 'same' | 'next' }> | null {
    const here = band_holding(bands, graded.ids, graded.key);
    const row = here >= 0 ? next_todo(bands[here], graded.row) : null;
    if (row) return { kind: 'same', band: here, row };
    // Gone from the page (TO GRADE): the conversation that slid into its place is the first one after it.
    const to = next_todo_band(bands, here >= 0 ? here : graded.at - 1);
    return to === null ? null : { kind: 'next', band: to, back: here >= 0 && to === here + 1 };
}

/** Once nothing lies ahead: the nearest earlier conversation with a turn to grade, or none. `held`: the graded
 *  conversation is still on the page, so the pager stays on it rather than sliding to a neighbour. */
export function left_after_grade(bands: readonly Pick<ThreadBand, 'rows' | 'key'>[], graded: GradedAt): Extract<AfterGrade, { kind: 'behind' | 'none' }> {
    const here = band_holding(bands, graded.ids, graded.key);
    const behind = todo_behind(bands, here >= 0 ? here : graded.at);
    return behind === null ? { kind: 'none', held: here >= 0 } : { kind: 'behind', band: behind, held: here >= 0 };
}

/** The status line after a grade (`graded` is `Graded ✓ DONE · RIGHT`), in the words of where it went. */
export function after_grade_text(graded: string, step: AfterGrade): string {
    if (step.kind === 'same') return graded;
    if (step.kind === 'next') return `${graded}. That conversation is done: here is the next one${step.back ? ' (← goes back)' : ''}`;
    if (step.kind === 'behind' && !step.held) return `${graded}. That conversation is done, and nothing lies ahead: here is an earlier one with turns to grade`;
    return `${graded}. ${end_text(step.kind === 'behind')}`;
}

/** What is left once a conversation is graded and nothing lies ahead: one owner for the status line and the footer. */
export function end_text(behind: boolean): string {
    return behind ? 'Nothing further ahead: an earlier conversation still has turns to grade' : 'Nothing else to grade on this page';
}

// ── a conversation's turns, under the prompt each answered ───────────────────

export interface TurnGroup {
    key: string;
    /** What opened the group: the operator's prompt (`ask`), else, for a turn the rack or a lead opened with no prompt of
     *  the operator's before it, that turn's own opening message. Null when the join has neither (an unlinked turn). */
    anchor: HistoryClip | null;
    /** `you`: the anchor is the operator's. `other`: the rack, a peer or a lead typed it. */
    by: 'you' | 'other';
    /** What woke the anchor's turn when it was not the operator (`turns.origin`), for an `other` anchor. */
    woke_by: string | null;
    /** Which of the operator's prompts in its session file the anchor is, and the file's count of them. */
    ordinal: number | null;
    of: number | null;
    /** Operator prompts in the same file between the previous group's and this one's: none of their turns is on the page. */
    skipped: number;
    rows: ReviewRecentRow[];
}

/** A conversation's turns (oldest first, as `thread_bands` orders them), grouped by the prompt each answered: the turns
 *  a task notification or the rack woke after one prompt sit under that prompt once. */
export function turn_groups(band: Pick<ThreadBand, 'rows' | 'thread'>, join: HistoryThreads | null): TurnGroup[] {
    const out: TurnGroup[] = [];
    let last: { file: number; ordinal: number } | null = null;
    for (const row of band.rows) {
        const found = join?.decisions[row.decision_id];
        const turn = found?.status === 'linked' ? found : null;
        const ask = turn?.ask ?? null;
        const anchor = ask ?? turn?.request ?? null;
        const file = turn?.file_id ?? null;
        const key = anchor && file !== null ? `${file}:${anchor.seq}` : `row:${row.decision_id}`;
        const prev = out.at(-1);
        if (prev && prev.key === key) {
            prev.rows.push(row);
            continue;
        }
        const ordinal = ask && turn?.human_ordinal ? turn.human_ordinal : null;
        const skipped = last && ordinal !== null && file === last.file ? Math.max(0, ordinal - last.ordinal - 1) : 0;
        out.push({
            key, anchor, by: ask ? 'you' : 'other', woke_by: ask ? null : turn?.woke_by ?? null,
            ordinal, of: ordinal !== null ? band.thread?.human_turns ?? null : null, skipped, rows: [row],
        });
        if (ordinal !== null && file !== null) last = { file, ordinal };
    }
    return out;
}

/** Whether the conversation's first message already draws as a group's prompt, so its head need not print it again
 *  (that would be a second telling inside one view). Both clips start at the message's start. */
export function first_in_groups(groups: readonly TurnGroup[], first: HistoryClip | null | undefined): boolean {
    if (!first) return false;
    const lead = (text: string) => text.trim().slice(0, 80);
    return groups.some((g) => g.by === 'you' && g.anchor?.seq === first.seq && lead(g.anchor.text) === lead(first.text));
}

/** `YOU · prompt 2 of 5`: a group's head, before its age. */
export function group_head(group: Pick<TurnGroup, 'by' | 'ordinal' | 'of' | 'woke_by'>): string {
    if (group.by === 'other') return group.woke_by === 'parent' ? 'LEAD' : 'RACK';
    if (group.ordinal === null) return 'YOU';
    return group.of && group.of >= group.ordinal ? `YOU · prompt ${group.ordinal} of ${group.of}` : `YOU · prompt ${group.ordinal}`;
}

/** What the join is asked about for a page of rows: every verdict, and every raw conversation (for a fresh verdict the
 *  links have not reached, its thread). */
export function threads_ask_of(rows: readonly Pick<ReviewRecentRow, 'decision_id' | 'conversation'>[]): { decisions: string[]; conversations: string[] } {
    const conversations = new Set<string>();
    for (const row of rows) if (row.conversation) conversations.add(row.conversation);
    return { decisions: rows.map((row) => row.decision_id), conversations: [...conversations] };
}

// ── marks ────────────────────────────────────────────────────────────────────

/** 32-bit FNV-1a: stable across loads and machines, so a thread keeps its mark. */
export function fnv1a(text: string): number {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
        h ^= text.charCodeAt(i);
        h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
}

/** No I or O: beside a digit they read as 1 and 0. */
const MONO_LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
/** Identity hues on the page (`laya-review.css` `--lr-t1`…`--lr-t4`); never the good / bad / amber of a grade. */
export const THREAD_HUES = 4;

/** `A3`: a letter and a digit, so a thread reads without its colour too. An unlinked run is `··`: nothing to name. */
export function monogram(source: Pick<HeadSource, 'identity' | 'kind'>): string {
    if (source.kind === 'unlinked') return '··';
    const h = fnv1a(source.identity);
    return `${MONO_LETTERS[h % MONO_LETTERS.length]}${1 + ((h >>> 8) % 9)}`;
}

/** 0…`THREAD_HUES - 1`, from the same hash. */
export function thread_hue(identity: string): number {
    return (fnv1a(identity) >>> 16) % THREAD_HUES;
}

// ── heads ────────────────────────────────────────────────────────────────────

/** What the reader's needle fallback found for a band the join did not name (`LayaConversation.vue` `found`). */
export interface FoundHead {
    title: string | null;
    first_user: string | null;
}

export type HeadTagId = 'new' | 'resumed' | 'indexing' | 'unlinked';

export interface HeadTag {
    id: HeadTagId;
    text: string;
    title: string;
}

/** The head's tags, one owner for their words: the tag prints `word`, its tooltip and EXPLAIN print `means`. */
const TAG_WORDS: Readonly<Record<HeadTagId, { word: string; means: string }>> = {
    new: { word: 'NEW', means: 'its rack name held a different conversation earlier on this page' },
    resumed: { word: 'RESUMED', means: 'resumed in a new session file; it still reads as one thread' },
    indexing: { word: 'INDEXING', means: 'the session ledger has not caught up with this turn yet; it was asked to, and the head fills in within a minute' },
    unlinked: { word: 'NOT LINKED', means: 'not matched to a conversation, so grouped by rack name, at most 30 minutes between turns' },
};

function tag(id: HeadTagId, suffix = ''): HeadTag {
    const { word, means } = TAG_WORDS[id];
    return { id, text: suffix ? `${word} ${suffix}` : word, title: `${means[0].toUpperCase()}${means.slice(1)}` };
}

export interface ThreadHead {
    monogram: string;
    hue: number;
    unlinked: boolean;
    title: string;
    subject: string | null;
    /** The mono line after the rack name: `4 turns · 2 to grade · started 2h`, or a turn's `turn 4 of 4 · 12m`. */
    meta: string;
    meta_title: string;
    first_user: string | null;
    /** True when the bridge sent only the first message's head (`HistoryClip.chars`): a paste it cut reads as at least
     *  its size (`paste-blocks.ts`). */
    first_clipped: boolean;
    tags: HeadTag[];
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function clock(ms: number): { day: string; time: string; half: 'am' | 'pm' } {
    const d = new Date(ms);
    const h = d.getHours();
    return {
        day: `${MONTHS[d.getMonth()]} ${d.getDate()} ${d.getFullYear()}`,
        time: `${h % 12 || 12}:${String(d.getMinutes()).padStart(2, '0')}`,
        half: h < 12 ? 'am' : 'pm',
    };
}

/** `3:10 – 3:40 pm` in local time, with the day in front when it is not today. */
export function clock_span(from_ms: number, to_ms: number, now_ms: number): string {
    const a = clock(from_ms);
    const b = clock(to_ms);
    const short = (day: string) => day.replace(/ \d{4}$/, '');
    if (a.day !== b.day) return `${short(a.day)}, ${a.time} ${a.half} – ${short(b.day)}, ${b.time} ${b.half}`;
    const day = a.day === clock(now_ms).day ? '' : `${short(a.day)}, `;
    if (a.time === b.time && a.half === b.half) return `${day}${a.time} ${a.half}`;
    return a.half === b.half ? `${day}${a.time} – ${b.time} ${b.half}` : `${day}${a.time} ${a.half} – ${b.time} ${b.half}`;
}

function first_line(text: string | null | undefined): string | null {
    return text?.trim().split(/\r?\n/)[0]?.slice(0, 120) || null;
}

/** The ledger's title, else what the needle found, else the first prompt's first line, else the rack name (with its
 *  time span, for an unlinked run), else `Conversation`. */
export function band_title(source: HeadSource, found: FoundHead | null | undefined, now_ms: number): string {
    const named = source.thread?.title?.trim() || found?.title?.trim() || first_line(source.thread?.first_user?.text) || first_line(found?.first_user);
    if (named) return named;
    if (source.kind === 'unlinked' && source.span) return `${source.subject ?? 'A session'}, ${clock_span(source.span[0], source.span[1], now_ms)}`;
    return source.subject?.trim() || 'Conversation';
}

function head_tags(source: HeadSource, join_ok: boolean): HeadTag[] {
    const tags: HeadTag[] = [];
    // The rack name prints beside the tag already, so it is said once; the tip names it.
    if (source.new_in_rack) tags.push({ ...tag('new'), title: `A new conversation: ${source.new_in_rack} held a different one earlier on this page` });
    if (source.thread && source.thread.resumed > 0) tags.push(tag('resumed', `${source.thread.resumed}×`));
    if (source.indexing) tags.push(tag('indexing'));
    else if (source.kind === 'unlinked' && join_ok) tags.push(tag('unlinked'));
    return tags;
}

function where_title(thread: HistoryThread | null): string {
    if (!thread) return '';
    const place = [thread.cwd, thread.branch && `(${thread.branch})`].filter(Boolean).join(' ');
    return [place, thread.profile && `profile ${thread.profile}`, thread.source].filter(Boolean).join(' · ');
}

/** A band's full head: `4 turns · 2 to grade · started 2h`. */
export function band_head(band: ThreadBand, ctx: { now: number; join_ok: boolean; found?: FoundHead | null }): ThreadHead {
    const n = band.rows.length;
    const todo = band.rows.filter((r) => operator_verdict(r) === null).length;
    const started = ts_age_text(band.thread?.started_at, ctx.now);
    const meta = [`${n} turn${n === 1 ? '' : 's'}`, todo ? `${todo} to grade` : null, started ? `started ${started}` : null].filter(Boolean).join(' · ');
    const prompts = band.thread?.human_turns;
    const meta_title = [
        `${n} judged turn${n === 1 ? '' : 's'} on this page`,
        prompts !== null && prompts !== undefined ? `${prompts} prompt${prompts === 1 ? '' : 's'} in the conversation` : null,
        where_title(band.thread),
    ].filter(Boolean).join(' · ');
    return head_of(band, ctx, meta, meta_title);
}

/** One turn's compact head (a deck card): `turn 4 of 4 · 12m`. */
export function turn_head(source: HeadSource, turn: HistoryTurn | null | undefined, at: string | null, ctx: { now: number; join_ok: boolean; found?: FoundHead | null }): ThreadHead {
    const ordinal = turn?.human_ordinal;
    const of = source.thread?.human_turns;
    const which = ordinal ? (of && of >= ordinal ? `turn ${ordinal} of ${of}` : `turn ${ordinal}`) : null;
    const age = at ? ts_age_text(Date.parse(at) / 1000, ctx.now) : null;
    return head_of(source, ctx, [which, age].filter(Boolean).join(' · '), where_title(source.thread));
}

function head_of(source: HeadSource, ctx: { now: number; join_ok: boolean; found?: FoundHead | null }, meta: string, meta_title: string): ThreadHead {
    return {
        monogram: monogram(source),
        hue: thread_hue(source.identity),
        unlinked: source.kind === 'unlinked',
        title: band_title(source, ctx.found, ctx.now),
        subject: source.subject,
        meta,
        meta_title,
        first_user: source.thread?.first_user?.text ?? ctx.found?.first_user ?? null,
        first_clipped: (source.thread?.first_user?.chars ?? 0) > (source.thread?.first_user?.text.length ?? 0),
        tags: head_tags(source, ctx.join_ok),
    };
}

/** A deck card's head source: the card has no rack name or row, only its turn from the join (`k=` its state key, or
 *  its decision's link when that is the better answer: `LayaReviewDeck.vue` `card_turn_for`). It reads INDEXING only
 *  while that turn is `pending`: a key's `missing` is final (`awaits_ingest`). */
export function card_source(key: string, turn: HistoryTurn | null | undefined, join: HistoryThreads | null): HeadSource {
    const thread_id = turn?.thread ?? null;
    return {
        identity: thread_id ? `t:${thread_id}` : `card:${key}`,
        kind: thread_id ? 'thread' : 'unlinked',
        thread: thread_id ? join?.threads[thread_id] ?? null : null,
        subject: null,
        new_in_rack: null,
        indexing: !!turn && awaits_ingest(turn, 'key'),
        span: null,
    };
}

// ── a turn row ───────────────────────────────────────────────────────────────

/** A row's preview: the agent's own closing message when linked (its end, cut at a line), else what Laya read. */
export function preview_text(row: Pick<ReviewRecentRow, 'text'>, turn: HistoryTurn | null | undefined): string {
    return turn?.status === 'linked' && turn.closing_tail ? turn.closing_tail : row.text;
}

// ── EXPLAIN ──────────────────────────────────────────────────────────────────

export type JoinState = 'loading' | 'ok' | 'old_bridge' | 'unavailable';

/** What the pager means and what its tags mean, as EXPLAIN prints them: only the states on screen now, so pass the
 *  conversation shown (a key for a state this page does not show is noise). */
export function explain_bands(bands: readonly HeadSource[], join: JoinState, order: ReviewRecentOrder): string[] {
    if (!bands.length) return [];
    const lines: string[] = [];
    if (join === 'old_bridge') lines.push('This bridge predates thread links, so turns group by rack name, at most 30 minutes apart: restart the bridge.');
    else if (join === 'unavailable') lines.push('Thread links did not load, so turns group by rack name, at most 30 minutes apart.');
    lines.push('One conversation at a time: ◀ ▶ (or ← →) move to the previous or next one without grading, and the counter lists them all. ' +
        'Its turns read oldest first, under the prompt each one answered; grading its last open turn moves on to the next conversation with turns to grade.');
    if (order === 'edge') lines.push('EDGE orders the conversations by their turn nearest a cut.');
    const shown = new Set<HeadTagId>();
    for (const band of bands) for (const t of head_tags(band, join === 'ok')) shown.add(t.id);
    for (const id of ['new', 'resumed', 'indexing', 'unlinked'] as const) {
        if (shown.has(id)) lines.push(`${TAG_WORDS[id].word}: ${TAG_WORDS[id].means}.`);
    }
    return lines;
}
