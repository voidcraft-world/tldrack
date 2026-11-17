/**
 * The conversation a live verdict was judged in, so the operator grades a turn knowing what was ASKED, and what the
 * session was called then. A rack name like `vc-8` is reused by the next agent; the conversation's own title is not.
 *
 * TWO WAYS IN, in this order:
 * 1. **The join** (`load_threads`, `GET /ledger/threads`): every verdict on the page resolved in one call, by decision
 *    id (`d=`), raw conversation id (`c=`) or deck card key (`k=`), to its thread and its turn. The bridge links each
 *    verdict at ingest (`bridge/tldrack/ledger/laya_links.py` owns how, and why it is exact). A linked
 *    turn's messages then load by position (`load_linked_context`), never by search.
 * 2. **The needle** (`load_turn_context`): the fallback for what the join did not resolve — an older bridge (404), an id
 *    still `pending`, a text that exists only in an export. It searches the ledger for the closing text itself and takes
 *    the nearest agent hit (`pick_hit`), which can pick another session's message: that is why it is only the fallback.
 *
 * The index catches up every 15 minutes, so a verdict from the last minutes can be `pending`: `freshen_threads` wakes
 * the ingest (`POST /ledger/ingest`, what `tldrack ledger ingest` runs) at most once a minute and asks
 * again a few seconds later, twice at most, never in a loop.
 *
 * The pure half (URL building, merging, the reader's layout, the needle's picks) runs under bare `node --test`; the
 * loaders are thin.
 */
import {
    HISTORY_THREADS_MAX,
    history_conversation_schema,
    history_search_schema,
    history_threads_schema,
    type HistoryClip,
    type HistoryConversation,
    type HistoryHit,
    type HistoryThreads,
    type HistoryTurn,
} from '@tldrack/schemas/agent-history.ts';
import { LAYA_DECISION_ID, LAYA_LEDGER_CONVERSATION } from '@tldrack/schemas/decide.ts';

import { get_bridge_json } from './bridge-get.ts';
import { post_bridge_json } from './bridge-post.ts';
import { laya_base_url } from '../decide/laya-local-client.ts';

/** A needle shorter than this matches too much ("Waiting."); longer than the cap gains nothing and risks a fold seam. */
export const NEEDLE_MIN = 24;
export const NEEDLE_CHARS = 100;
/** A verdict is asked ~1–2 s after its message is written; a quarter hour covers a slow judge and a clock skew. */
export const MATCH_WINDOW_S = 15 * 60;
const TIMEOUT_MS = 8_000;

type Message = HistoryConversation['messages'][number];

// ── the join: /ledger/threads ────────────────────────────────────────────────

/** What one page asks the join about. Each list is shape-checked and de-duplicated before it goes on the wire. */
export interface ThreadsAsk {
    decisions?: readonly string[];
    conversations?: readonly string[];
    keys?: readonly string[];
}

/** A deck card's state key; a `golden:` case has no conversation, so it is never asked. */
const STATE_KEY = /^[0-9a-f]{16}$/;
const PARAMS = [
    ['d', 'decisions', LAYA_DECISION_ID],
    ['c', 'conversations', LAYA_LEDGER_CONVERSATION],
    ['k', 'keys', STATE_KEY],
] as const;

/**
 * The GET URLs for an ask: at most `max` ids per URL, all kinds together, each kind ONE comma-joined param. The ids'
 * shapes are URL-safe, so the commas go unescaped, which keeps a full page inside phone-link's 4 KB. Empty → none.
 */
export function threads_urls(origin: string, ask: ThreadsAsk, max: number = HISTORY_THREADS_MAX): string[] {
    const ids: [string, string][] = [];
    for (const [param, field, shape] of PARAMS) {
        const seen = new Set<string>();
        for (const id of ask[field] ?? []) {
            if (seen.has(id) || !shape.test(id)) continue;
            seen.add(id);
            ids.push([param, id]);
        }
    }
    const urls: string[] = [];
    for (let at = 0; at < ids.length; at += max) {
        const chunk = ids.slice(at, at + max);
        const query = PARAMS.map(([param]) => [param, chunk.filter(([p]) => p === param).map(([, id]) => id)] as const)
            .filter(([, list]) => list.length > 0)
            .map(([param, list]) => `${param}=${list.join(',')}`)
            .join('&');
        urls.push(`${origin}/ledger/threads?${query}`);
    }
    return urls;
}

export const NO_THREADS: HistoryThreads = { decisions: {}, keys: {}, conversations: {}, threads: {}, indexed_at: null };

/** Later answers win, id by id; `indexed_at` is the newest any part saw. */
export function merge_threads(...parts: readonly HistoryThreads[]): HistoryThreads {
    const stamps = parts.map((p) => p.indexed_at).filter((t): t is number => t !== null);
    return {
        decisions: Object.assign({}, ...parts.map((p) => p.decisions)),
        keys: Object.assign({}, ...parts.map((p) => p.keys)),
        conversations: Object.assign({}, ...parts.map((p) => p.conversations)),
        threads: Object.assign({}, ...parts.map((p) => p.threads)),
        indexed_at: stamps.length ? Math.max(...stamps) : null,
    };
}

/**
 * Whether an ingest could still change this answer, so the page asks again (and INDEXING is honest). `pending` always
 * can. `none` never does: the linker gave up after a week. `missing` depends on what was asked:
 * - a DECISION id is asked only for a live verdict, which one ingest reads; its `missing` is rare and worth asking
 *   again (a ledger not yet migrated to the links table answers every id `missing` until its next ingest);
 * - a deck KEY is mostly a labelled text no live verdict ever had (`reader.py` `_ThreadJoin.key`), so its `missing` is
 *   the final answer, and treating it as unsettled woke an ingest on every deal.
 */
export function awaits_ingest(turn: HistoryTurn, asked_by: 'decision' | 'key'): boolean {
    return turn.status === 'pending' || (turn.status === 'missing' && asked_by === 'decision');
}

/** The part of an ask the ledger has not caught up with (`awaits_ingest`), an unanswered id, and a conversation it
 *  maps to null. */
export function unresolved_ask(data: HistoryThreads, ask: ThreadsAsk): Required<ThreadsAsk> {
    const open = (turn: HistoryTurn | undefined, asked_by: 'decision' | 'key') => turn === undefined || awaits_ingest(turn, asked_by);
    return {
        decisions: (ask.decisions ?? []).filter((id) => LAYA_DECISION_ID.test(id) && open(data.decisions[id], 'decision')),
        conversations: (ask.conversations ?? []).filter((id) => LAYA_LEDGER_CONVERSATION.test(id) && !data.conversations[id]),
        keys: (ask.keys ?? []).filter((id) => STATE_KEY.test(id) && open(data.keys[id], 'key')),
    };
}

export function ask_size(ask: ThreadsAsk): number {
    return (ask.decisions?.length ?? 0) + (ask.conversations?.length ?? 0) + (ask.keys?.length ?? 0);
}

/** `old_bridge`: the route is not there (404), so the page groups by rack name and time and says why. */
export type ThreadsResult =
    | { ok: true; data: HistoryThreads }
    | { ok: false; reason: 'old_bridge' | 'unavailable'; message: string };

/** One page's join, chunked to `HISTORY_THREADS_MAX` ids a call. Any failed chunk fails the whole: half a join would
 *  band half the page by thread and half by rack name, which reads as two different conversations. */
export async function load_threads(ask: ThreadsAsk): Promise<ThreadsResult> {
    const urls = threads_urls(bridge_origin(), ask);
    const parts: HistoryThreads[] = [];
    for (const url of urls) {
        const got = await get_bridge_json(url, TIMEOUT_MS);
        if (!got.ok) return { ok: false, reason: got.status === 404 ? 'old_bridge' : 'unavailable', message: got.error };
        const parsed = history_threads_schema.safeParse(got.body);
        if (!parsed.success) return { ok: false, reason: 'unavailable', message: 'The bridge returned an unreadable thread join' };
        parts.push(parsed.data);
    }
    return { ok: true, data: merge_threads(NO_THREADS, ...parts) };
}

/** An ingest run takes ~1.7 s: the first re-ask lands after it, the second catches a run that queued behind another. */
export const REASK_MS: readonly number[] = [3_000, 6_000];
/** One ingest wake per this, page-wide, however many loads ask for one. */
export const INGEST_EVERY_MS = 60_000;

export interface IngestPacer {
    /** True (and the slot is spent) when an ingest may be woken now. */
    take(): boolean;
}

export function ingest_pacer(every_ms: number = INGEST_EVERY_MS, now: () => number = Date.now): IngestPacer {
    let last = -Infinity;
    return {
        take() {
            const t = now();
            if (t - last < every_ms) return false;
            last = t;
            return true;
        },
    };
}

const PAGE_PACER = ingest_pacer();

export interface FreshenDeps {
    pacer: IngestPacer;
    ingest: () => Promise<boolean>;
    load: (ask: ThreadsAsk) => Promise<ThreadsResult>;
    delays: readonly number[];
    wait: (ms: number, run: () => void) => () => void;
}

const DEFAULT_FRESHEN: FreshenDeps = {
    pacer: PAGE_PACER,
    ingest: () => refresh_index(),
    load: load_threads,
    delays: REASK_MS,
    wait: (ms, run) => {
        const id = setTimeout(run, ms);
        return () => clearTimeout(id);
    },
};

/**
 * When `first` left any id unsettled: wake the ingest (unless one was woken this minute), then ask again for just
 * those ids after each of `delays`, merging each answer over `first` and handing it to `take`. Stops as soon as nothing
 * is unsettled. A wake the pacer refuses still asks again on the same clock: the ingest another caller woke this minute
 * (the deck beside RECENT, or this view's own earlier load) is what those re-asks catch, and that caller's re-asks
 * cover only its own ids — and stop when it reloads. Returns the cancel (a reload, an unmount).
 */
export function freshen_threads(ask: ThreadsAsk, first: HistoryThreads, take: (data: HistoryThreads) => void, deps: FreshenDeps = DEFAULT_FRESHEN): () => void {
    if (ask_size(unresolved_ask(first, ask)) === 0) return () => {};
    let cancelled = false;
    let stop_timer: () => void = () => {};
    let data = first;
    const step = (i: number): void => {
        if (cancelled || i >= deps.delays.length) return;
        stop_timer = deps.wait(deps.delays[i], async () => {
            const missing = unresolved_ask(data, ask);
            if (cancelled || ask_size(missing) === 0) return;
            const got = await deps.load(missing);
            if (cancelled) return;
            if (got.ok) {
                data = merge_threads(data, got.data);
                take(data);
            }
            step(i + 1);
        });
    };
    void (deps.pacer.take() ? deps.ingest() : Promise.resolve(true)).then((sent) => {
        if (sent) step(0);
    });
    return () => {
        cancelled = true;
        stop_timer();
    };
}

/** Where a reader's messages come from: the join's link, the needle search, or neither yet (the join is in flight,
 *  and a search started now would only race the link that lands a moment later). */
export function context_source(turn: HistoryTurn | null | undefined, joining: boolean): 'linked' | 'needle' | 'wait' {
    if (turn?.status === 'linked' && turn.file_id !== null && turn.closing_seq !== null) return 'linked';
    if (joining && turn === undefined) return 'wait';
    return 'needle';
}

// ── the needle: /ledger/search ───────────────────────────────────────────────

/** The search needle: the first line long enough to be the message's own words, cut to `NEEDLE_CHARS`. One line,
 *  because the ledger and the transcript may join a message's text blocks differently. Null when no line will do. */
export function closing_needle(text: string): string | null {
    for (const raw of text.split('\n')) {
        const line = raw.trim();
        if (line.length >= NEEDLE_MIN) return line.slice(0, NEEDLE_CHARS);
    }
    return null;
}

/** The agent message the verdict judged: an agent hit within `MATCH_WINDOW_S` of the decision, the nearest one. */
export function pick_hit(hits: readonly HistoryHit[], at_iso: string | null, more = false): HistoryHit | null {
    if (at_iso === null) {
        const agents = hits.filter((hit) => hit.role === 'agent');
        return !more && agents.length === 1 ? agents[0] : null;
    }
    const at = Date.parse(at_iso) / 1000;
    if (!Number.isFinite(at)) return null;
    let best: HistoryHit | null = null;
    for (const hit of hits) {
        if (hit.role !== 'agent' || hit.ts === null || Math.abs(hit.ts - at) > MATCH_WINDOW_S) continue;
        if (best === null || Math.abs(hit.ts - at) < Math.abs((best.ts as number) - at)) best = hit;
    }
    return best;
}

/** The operator's prompt that started the judged turn: the last user message at or before the closing message. */
export function ask_seq(messages: readonly Message[], closing_seq: number): number | null {
    let found: number | null = null;
    for (const m of messages) if (is_human_role(m.role) && m.seq <= closing_seq) found = m.seq;
    return found;
}

export function is_human_role(role: string): boolean {
    return role === 'human' || role === 'user';
}

/** A bridge's origin, from its laya plugin's base URL (`http://host:port/plugin/laya` → `http://host:port`). */
export function bridge_origin(laya_url: string = laya_base_url()): string {
    return laya_url.replace(/\/plugin\/[^/]+\/?$/, '');
}

export type TurnContext =
    | { ok: true; title: string | null; cwd: string | null; file_id: number; closing_seq: number; messages: HistoryConversation['messages']; before: number | null; first_user?: HistoryConversation['messages'][number] | null }
    | { ok: false; reason: 'no_text' | 'not_indexed' | 'unavailable'; message: string };

type FoundContext = Extract<TurnContext, { ok: true }>;

/** Codex's ledger title can be absent. Only the start of the session can supply a prompt-based title. */
export function context_title(context: Pick<FoundContext, 'title' | 'messages' | 'before' | 'first_user'>): string | null {
    if (context.title?.trim()) return context.title;
    const prompt = context.first_user?.text ?? (context.before === null
        ? context.messages.find((m) => is_human_role(m.role))?.text : null);
    return prompt?.trim().split(/\r?\n/)[0]?.slice(0, 120) || null;
}

/** Keep the judged turn's anchor when paging back; later messages must never leak into its review. */
export function prepend_context(current: FoundContext, earlier: FoundContext): FoundContext {
    if (earlier.file_id !== current.file_id) return current;
    const first = current.messages[0]?.seq ?? current.closing_seq + 1;
    const messages = earlier.messages.filter((m) => m.seq < first);
    if (!messages.length) return current;
    const merged = { ...current, messages: [...messages, ...current.messages], before: earlier.before,
        first_user: current.first_user === undefined ? earlier.first_user : current.first_user };
    return { ...merged, title: context_title(merged) };
}

const NOT_INDEXED = 'Not in the session index yet: it catches up every 15 minutes, or press REFRESH';

/** The conversation behind one verdict, found by its closing text: its title, and the messages up to and including the
 *  closing one. The fallback for what the join did not resolve (see the header). */
export async function load_turn_context(text: string, at_iso: string | null): Promise<TurnContext> {
    const needle = closing_needle(text);
    if (needle === null) return { ok: false, reason: 'no_text', message: 'The closing text is too short to look up' };
    const origin = bridge_origin();
    const found = await get_bridge_json(`${origin}/ledger/search?${new URLSearchParams({ q: needle })}`, TIMEOUT_MS);
    if (!found.ok) return { ok: false, reason: 'unavailable', message: found.error };
    const search = history_search_schema.safeParse(found.body);
    if (!search.success) return { ok: false, reason: 'unavailable', message: 'The bridge returned an unreadable search' };
    const hit = pick_hit(search.data.hits, at_iso, search.data.next_offset !== null);
    if (hit === null) return { ok: false, reason: 'not_indexed', message: at_iso === null
        ? 'No unique conversation match: this card has no live decision time. It may be a written test case, missing from the index, or repeated in several turns.'
        : NOT_INDEXED };
    const page = await load_earlier(hit.file_id, hit.seq + 1);
    if (!page.ok) return page;
    let context = { ...page, closing_seq: hit.seq };
    // Older bridges omit first_user. Read the start directly, never mistake the end page's latest prompt for it.
    if (context.first_user === undefined) {
        const start = await load_context_page(hit.file_id, 'around', 0, hit.seq);
        if (start.ok) context = { ...context, first_user: start.first_user, title: context.title ?? start.title };
    }
    return context;
}

/** A linked turn's page: the closing message and the ones before it, by position. The thread's title and first message
 *  come from the join, so the start of the session is never read here. */
export async function load_linked_context(turn: Pick<HistoryTurn, 'file_id' | 'closing_seq'>): Promise<TurnContext> {
    if (turn.file_id === null || turn.closing_seq === null) return { ok: false, reason: 'not_indexed', message: NOT_INDEXED };
    const page = await load_earlier(turn.file_id, turn.closing_seq + 1);
    return page.ok ? { ...page, closing_seq: turn.closing_seq } : page;
}

/** One page before before_seq; manual history paging never changes the selected review's closing anchor. */
export async function load_earlier(file_id: number, before_seq: number): Promise<TurnContext> {
    return load_context_page(file_id, 'before', before_seq, before_seq - 1);
}

async function load_context_page(file_id: number, mode: 'before' | 'around', anchor: number, closing_seq: number): Promise<TurnContext> {
    const url = `${bridge_origin()}/ledger/conversation?${new URLSearchParams({ file_id: String(file_id), [mode]: String(anchor) })}`;
    const got = await get_bridge_json(url, TIMEOUT_MS);
    if (!got.ok) return { ok: false, reason: got.status === 404 ? 'not_indexed' : 'unavailable', message: got.error };
    const parsed = history_conversation_schema.safeParse(got.body);
    if (!parsed.success) return { ok: false, reason: 'unavailable', message: 'The bridge returned an unreadable conversation' };
    const { session, before } = parsed.data;
    const messages = parsed.data.messages.filter((m) => m.seq <= closing_seq);
    const first_user = parsed.data.first_user === undefined
        ? before === null ? messages.find((m) => is_human_role(m.role)) ?? null : undefined
        : parsed.data.first_user && parsed.data.first_user.seq <= closing_seq ? parsed.data.first_user : null;
    return { ok: true, title: context_title({ title: session.title, messages, before, first_user }), cwd: session.cwd,
        file_id, closing_seq, messages, before, first_user };
}

/** Ask the bridge to re-index now; the search can find a fresh verdict a few seconds later. */
export async function refresh_index(): Promise<boolean> {
    const sent = await post_bridge_json(`${bridge_origin()}/ledger/ingest`, {}, TIMEOUT_MS, 160);
    return sent.ok;
}

// ── the reader: one turn, in order ───────────────────────────────────────────

/** A message the reader draws: a loaded one, or the join's clip of it until its page loads. */
export type ReaderMessage = Pick<Message, 'seq' | 'ts' | 'role' | 'text'>;

export interface ReaderLayout {
    /** Loaded messages before the turn AND before the ask (it draws as YOU ASKED). */
    earlier: ReaderMessage[];
    /** Loaded messages between the ask and the turn's opener: the agent's earlier answer to it, when something else
     *  woke this turn. Once the earlier messages are shown they draw after YOU ASKED, so the reader stays in order. */
    after_ask: ReaderMessage[];
    /** How many messages sit before the turn, loaded or not, the ask left out (seqs are contiguous from 0: `fold.py`). */
    earlier_count: number;
    ask: ReaderMessage | null;
    /** Loaded messages between the turn's opener and its closing; the opener is one of them when it is not the ask. */
    in_turn: ReaderMessage[];
    in_turn_count: number;
    /** The full closing message, once its page is loaded. */
    closing: ReaderMessage | null;
}

/**
 * The reader's order for one turn: ⋯ earlier → YOU ASKED → ⋯ this turn → CLOSING. `request_seq` is the turn's opener
 * and `ask` the operator's prompt, both from the join; without them (the needle path) the ask is the last human message
 * loaded, and the turn starts there. Without either, every loaded message is "earlier".
 */
export function reader_layout(messages: readonly ReaderMessage[], closing_seq: number, opener: { request_seq?: number | null; ask?: HistoryClip | null } = {}): ReaderLayout {
    const fallback_ask = opener.ask === undefined && opener.request_seq == null ? ask_seq(messages as Message[], closing_seq) : null;
    const ask_at = opener.ask?.seq ?? fallback_ask;
    const loaded_ask = ask_at === null ? undefined : messages.find((m) => m.seq === ask_at);
    const ask: ReaderMessage | null = loaded_ask ?? (opener.ask ? { seq: opener.ask.seq, ts: opener.ask.ts, role: opener.ask.role, text: opener.ask.text } : null);
    const start = Math.min(opener.request_seq ?? ask_at ?? closing_seq, closing_seq);
    const is_ask = (seq: number) => ask !== null && seq === ask.seq;
    const before_closing = messages.filter((m) => m.seq < closing_seq && !is_ask(m.seq));
    const before_turn = before_closing.filter((m) => m.seq < start);
    const split = ask !== null && ask.seq < start ? ask.seq : Infinity;
    return {
        earlier: before_turn.filter((m) => m.seq < split),
        after_ask: before_turn.filter((m) => m.seq > split),
        earlier_count: Math.max(0, start - (ask !== null && ask.seq < start ? 1 : 0)),
        ask,
        in_turn: before_closing.filter((m) => m.seq >= start),
        in_turn_count: Math.max(0, closing_seq - start - (ask !== null && ask.seq >= start && ask.seq < closing_seq ? 1 : 0)),
        closing: messages.find((m) => m.seq === closing_seq) ?? null,
    };
}

/** A message's one-line head. The ledger's roles (`messages.role`) never print raw: a rack-typed line is RACK, a hook or
 *  notification SYSTEM, the lead session LEAD. `woke_by` is the turn's origin, passed for the message that OPENED it:
 *  a prompt the rack or another agent typed keeps the human role (`relay.py` marks only its turn), so the origin
 *  decides whose it was. */
export function role_head(role: string, woke_by?: string | null): 'YOU' | 'AGENT' | 'RACK' | 'SYSTEM' | 'LEAD' {
    if (is_human_role(role)) return woke_text(woke_by) === null ? 'YOU' : 'RACK';
    if (role === 'agent' || role === 'assistant') return 'AGENT';
    if (role === 'peer') return 'RACK';
    if (role === 'parent') return 'LEAD';
    return 'SYSTEM';
}

/** What woke a turn the operator did not open (`turns.origin`), as words; null for a prompt of theirs. */
export function woke_text(woke_by: string | null | undefined): string | null {
    if (!woke_by || woke_by === 'human' || woke_by === 'command') return null;
    if (woke_by === 'task') return 'a background report';
    if (woke_by === 'peer') return "another agent's message";
    return 'the rack';
}

const WAKE_SUMMARY = /<task-notification>[\s\S]*?<summary>\s*([\s\S]*?)\s*<\/summary>/;
export const WAKE_SUMMARY_CHARS = 90;

/** A task notification's own one-line `<summary>` ("Workspace checks failed", "Background command … completed"), read
 *  from the turn's opener; null when it carries none. */
export function wake_summary(request_text: string | null | undefined): string | null {
    const found = request_text ? WAKE_SUMMARY.exec(request_text)?.[1]?.replace(/\s+/g, ' ').trim() : '';
    if (!found) return null;
    return found.length > WAKE_SUMMARY_CHARS ? `${found.slice(0, WAKE_SUMMARY_CHARS - 1)}…` : found;
}

export const WAKE_TAG = '⟲ NOT YOUR PROMPT';

/** The mono `tag`, one plain `line` (who woke the agent, and a task notification's own summary), and the `title` saying
 *  why it is a turn to grade at all. */
export interface WakeNote {
    tag: string;
    line: string;
    title: string;
}

/** A turn the operator did not open, explained where it is graded; null for a prompt of theirs. Why it spells this much
 *  out: a bare note that a task notification woke the turn does not say the agent spoke again on its own, so with two
 *  turns to grade after one prompt the second reads as a duplicate. */
export function wake_note(woke_by: string | null | undefined, request_text?: string | null): WakeNote | null {
    const who = woke_text(woke_by);
    if (who === null) return null;
    const summary = woke_by === 'task' ? wake_summary(request_text) : null;
    const what = woke_by === 'task' ? ' (a task notification: a background job finishing, a hook\'s checks, a monitor)' : '';
    return {
        tag: WAKE_TAG,
        line: `${who[0].toUpperCase()}${who.slice(1)} woke the agent after its last reply${summary ? `: “${summary}”` : ''}`,
        title: `You sent no prompt here. After the agent's last reply, ${who}${what} woke it and it replied again. Laya scores `
            + 'every reply, so this one is a turn of its own to grade: ✓ DONE when it rightly changed nothing.',
    };
}

/** `▸ What Laya read · 1,300 of 2,412 characters`: only when Laya's input (the fitted state) differs from the message it
 *  was fitted from, which is when a misread is worth checking. Null otherwise. */
export function laya_read_note(read: string, message: string | null): string | null {
    if (message === null || read.trim() === message.trim()) return null;
    const n = (x: number) => x.toLocaleString('en-US');
    return `What Laya read · ${n(read.length)} of ${n(message.length)} characters`;
}
