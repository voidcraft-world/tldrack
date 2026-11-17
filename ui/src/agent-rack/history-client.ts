import { z } from 'zod';
import {
    history_conversation_schema,
    history_search_schema,
    type HistorySession,
} from '@tldrack/schemas/agent-history.ts';

export type { HistoryConversation, HistoryHit, HistorySearch } from '@tldrack/schemas/agent-history.ts';

const REQUEST_TIMEOUT_MS = 10_000;

async function request<T>(origin: string, path: string, schema: z.ZodType<T>, signal: AbortSignal,
    init: RequestInit = {}): Promise<T> {
    let response: Response;
    try {
        response = await fetch(`${origin.replace(/\/+$/, '')}${path}`, {
            ...init, cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
        });
    } catch (exc) {
        if (signal.aborted) throw exc;
        throw new Error('History could not reach the bridge. Check the connection and try again.');
    }
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
        const error = z.object({ error: z.string() }).safeParse(body);
        throw new Error(error.success ? error.data.error : `History unavailable (${response.status}); update the bridge.`);
    }
    const parsed = schema.safeParse(body);
    if (!parsed.success) throw new Error('The bridge returned an unreadable history response.');
    return parsed.data;
}

export function search_history(origin: string, query: string, offset: number, file_id: number | null, signal: AbortSignal) {
    const params = new URLSearchParams({ q: query, offset: String(offset) });
    if (file_id !== null) params.set('file_id', String(file_id));
    return request(origin, `/ledger/search?${params}`, history_search_schema, signal);
}

export function read_conversation(origin: string, file_id: number,
    anchor: { mode: 'around' | 'before' | 'after'; seq: number } | null, signal: AbortSignal) {
    const params = new URLSearchParams({ file_id: String(file_id) });
    if (anchor) params.set(anchor.mode, String(anchor.seq));
    return request(origin, `/ledger/conversation?${params}`, history_conversation_schema, signal);
}

export function refresh_history(origin: string, signal: AbortSignal) {
    return request(origin, '/ledger/ingest', z.object({ queued: z.boolean() }), signal, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
}

export function history_status(origin: string, signal: AbortSignal) {
    return request(origin, '/ledger/status', z.object({
        scheduler: z.object({
            child_running: z.boolean(),
            last: z.object({ started_at: z.number(), exit: z.number().nullable() }).nullable(),
        }).optional(),
    }), signal);
}

export function conversation_label(session: HistorySession): string {
    return session.title || session.session_id || `${session.source} conversation ${session.file_id}`;
}

/** Literal matches become Vue text nodes and marks, never transcript-supplied HTML. */
export function highlight_parts(text: string, query: string): { text: string; match: boolean }[] {
    const needle = query.trim();
    if (!needle) return [{ text, match: false }];
    const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    const parts: { text: string; match: boolean }[] = [];
    let start = 0;
    for (const hit of text.matchAll(pattern)) {
        // Very short queries must not turn a long transcript into thousands of DOM marks.
        if (parts.length >= 400) break;
        const at = hit.index;
        if (at > start) parts.push({ text: text.slice(start, at), match: false });
        parts.push({ text: hit[0], match: true });
        start = at + hit[0].length;
    }
    if (start < text.length) parts.push({ text: text.slice(start), match: false });
    return parts;
}
