import { z } from 'zod';

// bridge/tldrack/ledger/reader.py emits this boundary. The wire carries transcript contents as TEXT, never markup; a
// page that renders it as markdown sanitizes on its own side (ui/src/laya-review/agent-markdown.ts is the one such
// page, since 2026-11-07, so the operator reads an agent's reply the way the terminal showed it).
export const history_session_schema = z.object({
    file_id: z.number().int().positive(),
    session_id: z.string().nullable(),
    source: z.string(),
    profile: z.string().nullable(),
    kind: z.string(),
    title: z.string().nullable(),
    cwd: z.string().nullable(),
});

const message_position = {
    seq: z.number().int().nonnegative(),
    ts: z.number().nullable(),
    role: z.string(),
};

export const history_hit_schema = history_session_schema.extend({
    ...message_position,
    snippet: z.string(),
});

export const history_search_schema = z.object({
    hits: z.array(history_hit_schema),
    next_offset: z.number().int().nonnegative().nullable(),
    indexed_at: z.number().nullable(),
});

export const history_conversation_schema = z.object({
    session: history_session_schema,
    messages: z.array(z.object({ ...message_position, text: z.string() })),
    /** First human request in this file, independent of pagination; absent on older bridges. */
    first_user: z.object({ ...message_position, role: z.enum(['human', 'user']), text: z.string() }).nullable().optional(),
    before: z.number().int().nonnegative().nullable(),
    after: z.number().int().nonnegative().nullable(),
    indexed_at: z.number().nullable(),
});

// ── /ledger/threads: the Laya REVIEW page's batch join ───────────────────────
// Every verdict on a RECENT page (and every deck card, by its state key) resolved to the conversation and the turn it
// judged, plus one header per THREAD. Server: bridge/tldrack/ledger/reader.py `threads`, over the
// `laya_links` table that ledger/laya_links.py writes at ingest (it owns how a verdict is matched and why).

/** Ids per call, all kinds together: one call for a full RECENT page (`REVIEW_RECENT_DEFAULT`). */
export const HISTORY_THREADS_MAX = 100;
/**
 * `linked`: matched to its message. `pending`: not linked YET (the ledger has not ingested the verdict or its turn;
 * POST /ledger/ingest and ask again). `none`: the linker gave up (7 days unmatched). `missing`: an id this ledger has
 * never seen, or a ledger without the links table.
 */
export const HISTORY_LINK_STATUSES = ['linked', 'pending', 'none', 'missing'] as const;

/** A message the join clipped: `chars` is its full length when `text` was cut. */
const history_clip_schema = z.object({ ...message_position, text: z.string(), chars: z.number().int().nonnegative().optional() });

/** One verdict's turn. Every field past `status` is null unless `status` is `linked`. */
export const history_turn_schema = z.object({
    status: z.enum(HISTORY_LINK_STATUSES),
    thread: z.string().nullable(), // the THREAD key: the conversation id a resume chain started under
    conversation: z.string().nullable(), // this file's own session id
    file_id: z.number().int().positive().nullable(),
    closing_seq: z.number().int().nonnegative().nullable(),
    turn_idx: z.number().int().nonnegative().nullable(),
    /** The operator's prompts in this file up to and including this turn (turns whose origin is human or command). */
    human_ordinal: z.number().int().nonnegative().nullable(),
    /** What opened the turn (`turns.origin`): `human`, `command`, `task`, `peer`, `relay`, … */
    woke_by: z.string().max(32).nullable(),
    request: history_clip_schema.nullable(), // the turn's opening message, whoever wrote it
    /** The operator opened the turn with a question (the bridge's whole-prompt reading; the clip may have lost the `?`).
     *  Null unless linked; absent from an older bridge. A done turn with it is an answer: the review list draws 📖. */
    asked: z.boolean().nullable().optional(),
    ask: history_clip_schema.nullable(), // the operator's last prompt at or before this turn
    closing_tail: z.string().nullable(), // the closing message's end, cut at a line boundary
    closing_chars: z.number().int().nonnegative().nullable(),
});

/** One thread's header: the same for every turn in it. */
export const history_thread_schema = z.object({
    thread: z.string(),
    title: z.string().nullable(), // the ledger's title, else the first prompt's first line (always for Codex)
    first_user: history_clip_schema.nullable(),
    started_at: z.number().nullable(),
    last_ts: z.number().nullable(),
    cwd: z.string().nullable(),
    branch: z.string().nullable(),
    profile: z.string().nullable(),
    source: z.string(),
    kind: z.string(),
    human_turns: z.number().int().nonnegative().nullable(),
    resumed: z.number().int().nonnegative(), // later files of the same conversation folded into this thread
});

export const history_threads_schema = z.object({
    decisions: z.record(z.string(), history_turn_schema), // by Laya decision_id (`d=`)
    keys: z.record(z.string(), history_turn_schema), // by review key / state key (`k=`): deck cards
    conversations: z.record(z.string(), z.string().nullable()), // raw conversation id (`c=`) → its thread, null unindexed
    threads: z.record(z.string(), history_thread_schema),
    indexed_at: z.number().nullable(),
});

export type HistoryTurn = z.infer<typeof history_turn_schema>;
export type HistoryThread = z.infer<typeof history_thread_schema>;
export type HistoryThreads = z.infer<typeof history_threads_schema>;
export type HistoryClip = z.infer<typeof history_clip_schema>;

export type HistorySession = z.infer<typeof history_session_schema>;
export type HistoryHit = z.infer<typeof history_hit_schema>;
export type HistorySearch = z.infer<typeof history_search_schema>;
export type HistoryConversation = z.infer<typeof history_conversation_schema>;

// ── /ledger/rack-search: the rack's search box, past the titles ─────────────
// Which of the rack's live sessions SAID every term. Server: bridge/tldrack/ledger/rack_search.py,
// which owns the semantics and where a row's conversation is found; client: ui/src/agent-rack/chat-search.ts.

/** Session names per call (`s=`): the rack's DOM list scrolls past `scroll_max_sessions` (32), never this. */
export const RACK_SEARCH_SESSIONS_MAX = 64;

/** Why a row's chat matched: the newest message holding a term its title did not. */
export const rack_search_hit_schema = z.object({
    snippet: z.string(),
    role: z.string(),
    ts: z.number().nullable(),
});

export const rack_search_schema = z.object({
    /** session → its hit; only the sessions the CHAT decided (a title match is the rack's own). */
    matches: z.record(z.string(), rack_search_hit_schema),
    /** false: the ledger is off or empty, so only each live transcript's recent window was read. */
    ledger: z.boolean(),
    indexed_at: z.number().nullable(),
});

export type RackSearchHit = z.infer<typeof rack_search_hit_schema>;
export type RackSearch = z.infer<typeof rack_search_schema>;
