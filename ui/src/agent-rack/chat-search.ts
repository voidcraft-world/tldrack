/**
 * The rack's search box, past the titles: which live sessions SAID every term.
 *
 * The box filters by name and auto title on the rack page itself, instantly (`rack-search.ts`). A
 * title is a few words chosen at the first prompt, so it is not enough to find "the one where we
 * fixed the login redirect". This asks the bridge (`GET /ledger/rack-search`), which reads each live row's
 * conversation thread in the session ledger plus the live transcript the ledger has not caught up
 * with yet; the semantics and that join are owned by `tldrack/ledger/rack_search.py` — see
 * the note there. Its answer only ever ADDS rows, each with the line that matched.
 *
 * Host-side, like `closed-client.ts`: the rack page sends `'search'` with the box's text on every
 * keystroke, and the payload owner asks, debounced, and carries the answer back as
 * `RackPayload.chat_search`. The list may be mounted in a frame that does no I/O of its own, so
 * the fetch cannot live beside the box.
 *
 * Latest wins. While a newer question is out, the last answer stands (`pending`), so chat rows narrow
 * as you type instead of blinking out on every key; the page shows them only while its box still
 * says what they answer.
 *
 * Free of the DOM: every payload owner loads it, and it runs under bare `node --test`.
 */
import {
    RACK_SEARCH_SESSIONS_MAX,
    rack_search_schema,
    type RackSearchHit,
} from '@tldrack/schemas/agent-history.ts';
import type { ChatSearchView } from '../engine/terminal/rack-view.ts';
import { chat_query } from './rack-search.ts';
import { poller_io, type BaseUrl, type FetchImpl } from './worktree-client.ts';

export type { ChatSearchView, RackSearchHit };

/** Quiet time after a keystroke before the bridge is asked. */
export const CHAT_SEARCH_DEBOUNCE_MS = 250;
const SEARCH_TIMEOUT_MS = 10_000;

type Timer = ReturnType<typeof setTimeout>;

export interface ChatSearchDeps {
    base_url: BaseUrl;
    fetch_impl?: FetchImpl;
    /** Something the rack draws changed. */
    on_change: () => void;
    set_timeout?: (fn: () => void, ms: number) => Timer;
    clear_timeout?: (handle: Timer) => void;
}

export interface ChatSearch {
    /** Every snapshot's LIVE session names. A changed set re-asks while a query stands, so a session
     *  launched or renamed mid-search is searched too. */
    observe_sessions: (names: readonly string[]) => void;
    /** The rack's `'search'` action, for every payload owner's dispatcher: true when handled. */
    handle_action: (action: string, value?: string | null) => boolean;
    view: () => ChatSearchView | undefined;
    /** Drop the pending ask and abort the one in flight: the owner is going away. */
    dispose: () => void;
}

/** `GET /ledger/rack-search`'s URL, or null when there is nothing to ask. */
export function rack_search_url(base_url: BaseUrl, query: string, sessions: readonly string[]): string | null {
    const q = chat_query(query);
    if (!q || !sessions.length) return null;
    const params = new URLSearchParams({ q });
    for (const name of sessions.slice(0, RACK_SEARCH_SESSIONS_MAX)) params.append('s', name);
    return base_url(`/ledger/rack-search?${params}`);
}

type Outcome = { ok: true; matches: Record<string, RackSearchHit>; recent_only: boolean } | { ok: false; error: string };

/** One ask. Never throws: every failure is one line for the readout's tooltip. */
export async function ask_rack_search(url: string, do_fetch: FetchImpl, signal: AbortSignal): Promise<Outcome> {
    let res: Response;
    try {
        res = await do_fetch(url, { cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(SEARCH_TIMEOUT_MS)]) });
    } catch {
        return { ok: false, error: 'Chat search could not reach the bridge' };
    }
    const body: unknown = await res.json().catch(() => null);
    if (res.status === 404) return { ok: false, error: 'Chat search needs a newer bridge: restart it' };
    if (!res.ok) {
        const error = (body as { error?: unknown } | null)?.error;
        return { ok: false, error: typeof error === 'string' && error ? error : `Chat search failed (${res.status})` };
    }
    const parsed = rack_search_schema.safeParse(body);
    if (!parsed.success) return { ok: false, error: 'The bridge returned an unreadable chat search' };
    return { ok: true, matches: parsed.data.matches, recent_only: !parsed.data.ledger };
}

export function create_chat_search(deps: ChatSearchDeps): ChatSearch {
    const { do_fetch } = poller_io(deps);
    const set_timer = deps.set_timeout ?? ((fn, ms) => setTimeout(fn, ms));
    const clear_timer = deps.clear_timeout ?? ((handle) => clearTimeout(handle));
    let query = '';
    let sessions: readonly string[] = [];
    let sessions_key = '';
    let matches: Record<string, RackSearchHit> = {};
    let recent_only = false;
    let error: string | undefined;
    let pending = false;
    let timer: Timer | null = null;
    let inflight: AbortController | null = null;

    function cancel(): void {
        if (timer !== null) clear_timer(timer);
        timer = null;
        inflight?.abort();
        inflight = null;
    }

    async function ask(): Promise<void> {
        timer = null;
        const url = rack_search_url(deps.base_url, query, sessions);
        if (url === null) {
            matches = {};
            pending = false;
            deps.on_change();
            return;
        }
        const controller = new AbortController();
        inflight?.abort();
        inflight = controller;
        const outcome = await ask_rack_search(url, do_fetch, controller.signal);
        if (inflight !== controller) return; // a newer question owns the answer now
        inflight = null;
        pending = false;
        if (outcome.ok) {
            matches = outcome.matches;
            recent_only = outcome.recent_only;
            error = undefined;
        } else {
            matches = {};
            error = outcome.error;
        }
        deps.on_change();
    }

    function schedule(): void {
        cancel();
        pending = true;
        timer = set_timer(() => void ask(), CHAT_SEARCH_DEBOUNCE_MS);
        deps.on_change();
    }

    return {
        observe_sessions: (names) => {
            const key = [...names].sort().join('\n');
            if (key === sessions_key) return;
            sessions = names;
            sessions_key = key;
            if (query) schedule();
        },
        handle_action: (action, value) => {
            if (action !== 'search') return false;
            const next = chat_query(value ?? '');
            if (next === query) return true;
            query = next;
            if (query) {
                schedule();
                return true;
            }
            cancel();
            matches = {};
            pending = false;
            error = undefined;
            deps.on_change();
            return true;
        },
        view: () => {
            if (!query) return undefined;
            return {
                query,
                pending,
                matches,
                ...(recent_only ? { recent_only } : {}),
                ...(error !== undefined ? { error } : {}),
            };
        },
        dispose: cancel,
    };
}
