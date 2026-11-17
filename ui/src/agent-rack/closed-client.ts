/**
 * The rack's ↺ CLOSED list: the sessions closed recently, one press to bring one back.
 *
 * A ✕ is two clicks, and a closed agent's conversation is the work itself, so the undo has to be
 * as close as the mistake. The bridge keeps the list (`tldrack/closed_sessions.py` owns
 * what counts as a close and how long it is kept); `POST /reopen` respawns one where it was, on
 * its account, with `claude --resume`.
 *
 * Fetched, never polled. `/snapshot` carries the additive `closed` count, which moves on every
 * close and every reopen, so the list is re-read only when that count moves while it is open,
 * and when it is opened. A bridge that predates the routes sends no count and grows no button.
 *
 * Free of the DOM, like `devices-client.ts`: every rack payload owner (`rack-http-transport.ts`, and a host application's own)
 * loads it, and it runs under bare `node --test`.
 */
import {
    closed_session_schema,
    reopen_result_schema,
    type ClosedSession,
} from '@tldrack/schemas/closed-sessions.ts';
import type { ClosedView } from '../engine/terminal/rack-view.ts';
import { fetch_plugin_state } from './plugin-poller.ts';
import { poller_io, type BaseUrl, type FetchImpl } from './worktree-client.ts';

export type { ClosedSession, ClosedView };

const REOPEN_TIMEOUT_MS = 15_000;

/** `GET /closed`'s body, or null when it is not a closed-list document. A bad row is dropped alone. */
export function parse_closed(body: unknown): ClosedSession[] | null {
    const rows = (body as { sessions?: unknown } | null)?.sessions;
    if (!Array.isArray(rows)) return null;
    return rows.flatMap((row) => {
        const parsed = closed_session_schema.safeParse(row);
        return parsed.success ? [parsed.data] : [];
    });
}

export function fetch_closed(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<ClosedSession[] | null> {
    return fetch_plugin_state(base_url, '/closed', parse_closed, fetch_impl);
}

export type ReopenOutcome =
    | { ok: true; session: string; profile: string | null; resumed: boolean }
    | { ok: false; error: string };

/** `POST /reopen`. Never throws: every failure is an outcome with one line to show. */
export async function reopen_closed(base_url: BaseUrl, id: string, fetch_impl: FetchImpl): Promise<ReopenOutcome> {
    try {
        const res = await fetch_impl(base_url('/reopen'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id }),
            signal: AbortSignal.timeout(REOPEN_TIMEOUT_MS),
        });
        const body: unknown = await res.json().catch(() => null);
        const parsed = reopen_result_schema.safeParse(body);
        if (res.ok && parsed.success) return parsed.data;
        const error = (body as { error?: unknown } | null)?.error;
        return { ok: false, error: typeof error === 'string' && error ? `Reopen refused: ${error}` : `Reopen failed (${res.status})` };
    } catch {
        return { ok: false, error: 'Reopen failed: the bridge did not answer' };
    }
}

/** How long ago, in the rack's mono shorthand: `now`, `12m`, `3h`, `2d`. */
export function closed_ago(closed_at: number, now_ms: number): string {
    const seconds = Math.max(0, now_ms / 1000 - closed_at);
    if (seconds < 60) return 'now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86_400)}d`;
}

/** The last path segment — a worktree's folder is its branch, which is what tells two rows apart. */
export function closed_place(cwd: string | null): string {
    if (!cwd) return '';
    const parts = cwd.split('/').filter(Boolean);
    return parts[parts.length - 1] ?? '';
}

/** The row's hover sentence: everything the row abbreviates. */
export function closed_title(row: ClosedSession): string {
    const what = row.resumable ? 'resumes the conversation' : 'starts a fresh session there';
    const how = row.how === 'killed' ? 'closed with ✕' : 'ended outside the rack';
    const head = row.ok ? `Reopen ${row.session} — ${what}` : `Cannot reopen ${row.session}: ${row.reason ?? 'refused'}`;
    return `${head}. ${how}${row.starred ? ' · starred' : ''}${row.cwd ? ` · ${row.cwd}` : ''}`;
}

/** One drawn row of the list. */
export interface ClosedRowView {
    id: string;
    /** The first-prompt label the rack showed, else the session name. */
    name: string;
    /** Where it ran, or '' when every row ran in the same place — then it is wallpaper. */
    place: string;
    ago: string;
    title: string;
    ok: boolean;
    resumable: boolean;
    /** It closed with the operator's star (`star-client.ts`); a reopen hands the star back. */
    starred: boolean;
}

/** The list as drawn. Pure. */
export function closed_rows_view(rows: readonly ClosedSession[], now_ms: number): ClosedRowView[] {
    const places = rows.map((row) => closed_place(row.cwd));
    const show_place = new Set(places).size > 1;
    return rows.map((row, i) => ({
        id: row.id,
        name: row.label || row.session,
        place: show_place ? places[i] : '',
        ago: closed_ago(row.closed_at, now_ms),
        title: closed_title(row),
        ok: row.ok,
        resumable: row.resumable,
        starred: row.starred === true,
    }));
}

export interface ClosedListDeps {
    base_url: BaseUrl;
    fetch_impl?: FetchImpl;
    /** Something the rack draws changed. */
    on_change: () => void;
    /** A reopen landed: the session exists under `session` — focus it. */
    on_reopened: (session: string, profile: string | null) => void;
    /** A reopen did not happen — one line for the launch notice. */
    on_failure: (message: string) => void;
}

export interface ClosedList {
    /** Every snapshot's `closed` count (absent → 0). Call BEFORE building the payload. */
    observe_count: (count: number | undefined) => void;
    /** The header button. */
    toggle: () => void;
    reopen: (id: string) => Promise<void>;
    /** The rack's `'closed'` / `'reopen'` actions, for every payload owner's dispatcher: true when
     *  `action` was one of them (handled), false to let the host carry on. */
    handle_action: (action: string, value?: string | null) => boolean;
    view: () => ClosedView | undefined;
}

export function create_closed_list(deps: ClosedListDeps): ClosedList {
    const { do_fetch } = poller_io(deps);
    let count = 0;
    let open = false;
    let rows: ClosedSession[] | null = null;
    let reopening: string | null = null;
    /** Latest-wins: a slow read must not land over a newer one. */
    let read_seq = 0;

    async function refresh(): Promise<void> {
        const seq = ++read_seq;
        const next = await fetch_closed(deps.base_url, do_fetch);
        if (seq !== read_seq || next === null) return;
        rows = next;
        deps.on_change();
    }

    function toggle(): void {
        open = !open;
        if (open) void refresh();
        deps.on_change();
    }

    async function reopen(id: string): Promise<void> {
        if (reopening !== null) return;
        reopening = id;
        deps.on_change();
        const outcome = await reopen_closed(deps.base_url, id, do_fetch);
        reopening = null;
        if (outcome.ok) {
            rows = rows?.filter((row) => row.id !== id) ?? null;
            deps.on_reopened(outcome.session, outcome.profile);
        } else {
            deps.on_failure(outcome.error);
            void refresh();
        }
        deps.on_change();
    }

    return {
        observe_count: (next) => {
            const n = next ?? 0;
            if (n === count) return;
            count = n;
            if (count === 0 && reopening === null) {
                // Nothing left to offer: the list folds itself away with its button.
                open = false;
                rows = null;
                return;
            }
            if (open) void refresh();
        },
        toggle,
        reopen,
        handle_action: (action, value) => {
            if (action === 'closed') toggle();
            else if (action === 'reopen') {
                if (value) void reopen(value);
            } else return false;
            return true;
        },
        view: () => {
            if (count === 0 && !open && reopening === null) return undefined;
            return { count, open, rows, ...(reopening !== null ? { reopening } : {}) };
        },
    };
}
