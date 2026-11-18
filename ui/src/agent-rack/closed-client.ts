/**
 * The rack's ↺ list: the sessions closed recently, and the ones a reboot took down, one press to
 * bring one back.
 *
 * A ✕ is two clicks, and a closed agent's conversation is the work itself, so the undo has to be
 * as close as the mistake. The bridge keeps the list (`tldrack/closed_sessions.py` owns
 * what counts as a close and how long it is kept); `POST /reopen` respawns one where it was, on
 * its account, with `claude --resume`.
 *
 * A reboot (or a crash) is never a close (`tldrack/closed_sessions.py`), so what it took down
 * comes from `GET /restorable` and draws as its own band above the closes, with RESTORE ALL.
 * `POST /restore` respawns each under its old name, resuming the conversation where the bridge
 * can (`tldrack/restore.py` owns the refusals). Until 2026-11-18 the only door to it was the
 * macOS menu-bar dock, and when the dock went the offer had no button anywhere: a crash took
 * four agents down and the rack showed nothing to bring them back with.
 *
 * Fetched, never polled. `/snapshot` carries the additive `closed` and `restorable` counts, which
 * move on every close, reopen and restore, so the list is re-read only when a count moves while
 * it is open, and when it is opened. A bridge that predates the routes sends no count and grows
 * no button.
 *
 * Free of the DOM, like `devices-client.ts`: every rack payload owner (`rack-http-transport.ts`, and a host application's own)
 * loads it, and it runs under bare `node --test`.
 */
import {
    closed_session_schema,
    reopen_result_schema,
    restorable_session_schema,
    restore_result_schema,
    type ClosedSession,
    type RestorableSession,
} from '@tldrack/schemas/closed-sessions.ts';
import type { ClosedView, RestorableView } from '../engine/terminal/rack-view.ts';
import { fetch_plugin_state } from './plugin-poller.ts';
import { poller_io, type BaseUrl, type FetchImpl } from './worktree-client.ts';

export type { ClosedSession, ClosedView, RestorableSession, RestorableView };

const REOPEN_TIMEOUT_MS = 15_000;
/** The bridge spawns serially under one lock, a tmux call per row: twenty rows take a while. */
const RESTORE_TIMEOUT_MS = 60_000;

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
        return { ok: false, error: refusal('Reopen', res, body) };
    } catch {
        return { ok: false, error: 'Reopen failed: the bridge did not answer' };
    }
}

/** `GET /restorable`'s body, or null when it is not a restorable-list document. A bad row is dropped alone. */
export function parse_restorable(body: unknown): RestorableSession[] | null {
    const rows = (body as { sessions?: unknown } | null)?.sessions;
    if (!Array.isArray(rows)) return null;
    return rows.flatMap((row) => {
        const parsed = restorable_session_schema.safeParse(row);
        return parsed.success ? [parsed.data] : [];
    });
}

export function fetch_restorable(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<RestorableSession[] | null> {
    return fetch_plugin_state(base_url, '/restorable', parse_restorable, fetch_impl);
}

export type RestoreOutcome =
    | { ok: true; restored: string[]; failed: { session: string; reason: string }[] }
    | { ok: false; error: string };

/** The bridge's own errors are its sentences; one line, whatever it sent. */
function refusal(verb: string, res: Response, body: unknown): string {
    const error = (body as { error?: unknown } | null)?.error;
    return typeof error === 'string' && error ? `${verb} refused: ${error}` : `${verb} failed (${res.status})`;
}

/** `POST /restore`: `sessions` names the rows, null asks for every one that can come back.
 *  Never throws. The request is 200 even when a row was refused, so each row's outcome rides back. */
export async function restore_sessions(
    base_url: BaseUrl,
    sessions: readonly string[] | null,
    fetch_impl: FetchImpl,
): Promise<RestoreOutcome> {
    try {
        const res = await fetch_impl(base_url('/restore'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(sessions === null ? {} : { sessions }),
            signal: AbortSignal.timeout(RESTORE_TIMEOUT_MS),
        });
        const body: unknown = await res.json().catch(() => null);
        const parsed = restore_result_schema.safeParse(body);
        if (!res.ok || !parsed.success) return { ok: false, error: refusal('Restore', res, body) };
        const restored = parsed.data.results.filter((row) => row.restored).map((row) => row.session);
        const failed = parsed.data.results
            .filter((row) => !row.restored)
            .map((row) => ({ session: row.session, reason: row.reason ?? 'refused' }));
        return { ok: true, restored, failed };
    } catch {
        return { ok: false, error: 'Restore failed: the bridge did not answer' };
    }
}

/** `POST /restore/forget`: stop offering these. Never throws; false when it did not land. */
export async function forget_restorable(base_url: BaseUrl, sessions: readonly string[], fetch_impl: FetchImpl): Promise<boolean> {
    try {
        const res = await fetch_impl(base_url('/restore/forget'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessions }),
            signal: AbortSignal.timeout(REOPEN_TIMEOUT_MS),
        });
        return res.ok;
    } catch {
        return false;
    }
}

/** The launch notice after a restore: what came back, and the first refusal in full. */
export function restore_notice(outcome: Extract<RestoreOutcome, { ok: true }>): string | null {
    if (outcome.failed.length === 0) return null;
    const first = outcome.failed[0];
    const more = outcome.failed.length > 1 ? ` (+${outcome.failed.length - 1} more)` : '';
    return `Restored ${outcome.restored.length}; ${first.session} not: ${first.reason}${more}`;
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

/** The restore row's hover sentence: everything the row abbreviates. */
export function restorable_title(row: RestorableSession): string {
    const what = row.resumable ? 'resumes the conversation' : 'starts a fresh session there';
    const head = row.ok ? `Restore ${row.session} — ${what}` : `Cannot restore ${row.session}: ${row.reason ?? 'refused'}`;
    const mode = row.permission_mode ? ` · comes back in ${row.permission_mode} mode` : '';
    return `${head}${row.profile ? ` · ${row.profile}` : ''}${mode}${row.cwd ? ` · ${row.cwd}` : ''}`;
}

/** One drawn restore row: the closed row's shape, so both bands share one grid. */
export interface RestorableRowView {
    session: string;
    name: string;
    place: string;
    /** How long since the bridge last saw it live — about when the machine went down. */
    ago: string;
    title: string;
    ok: boolean;
    resumable: boolean;
}

/** The restore band as drawn. Pure. Refused rows sink below the ones that can come back. */
export function restorable_rows_view(rows: readonly RestorableSession[], now_ms: number): RestorableRowView[] {
    const places = rows.map((row) => closed_place(row.cwd));
    const show_place = new Set(places).size > 1;
    return rows
        .map((row, i) => ({
            session: row.session,
            name: row.label || row.session,
            place: show_place ? places[i] : '',
            ago: closed_ago(now_ms / 1000 - row.age_seconds, now_ms),
            title: restorable_title(row),
            ok: row.ok,
            resumable: row.resumable,
        }))
        .sort((a, b) => Number(b.ok) - Number(a.ok));
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
    /** A RESTORE ALL landed: these sessions exist again. Absent → `on_change` alone; the next
     *  poll lists them. (One restored row goes through `on_reopened`, which focuses it.) */
    on_restored?: (sessions: string[]) => void;
}

export interface ClosedList {
    /** Every snapshot's `closed` and `restorable` counts (absent → 0). Call BEFORE building the payload. */
    observe_count: (count: number | undefined, restorable?: number | undefined) => void;
    /** The header button. */
    toggle: () => void;
    reopen: (id: string) => Promise<void>;
    /** One session by name, or null for every row that can come back. */
    restore: (session: string | null) => Promise<void>;
    /** Stop offering every remaining restore. */
    forget_restorable: () => Promise<void>;
    /** The rack's `'closed'` / `'reopen'` / `'restore'` / `'restore_forget'` actions, for every
     *  payload owner's dispatcher: true when `action` was one of them (handled), false to let the
     *  host carry on. */
    handle_action: (action: string, value?: string | null) => boolean;
    view: () => ClosedView | undefined;
}

export function create_closed_list(deps: ClosedListDeps): ClosedList {
    const { do_fetch } = poller_io(deps);
    let count = 0;
    let restorable_count = 0;
    let open = false;
    let rows: ClosedSession[] | null = null;
    let restorable_rows: RestorableSession[] | null = null;
    let reopening: string | null = null;
    /** A session name, or `'*'` for RESTORE ALL. */
    let restoring: string | null = null;
    /** Latest-wins: a slow read must not land over a newer one. One counter per band. */
    let read_seq = 0;
    let restorable_seq = 0;

    const busy = (): boolean => reopening !== null || restoring !== null;

    async function refresh_closed(): Promise<void> {
        const seq = ++read_seq;
        const next = count > 0 ? await fetch_closed(deps.base_url, do_fetch) : [];
        if (seq !== read_seq || next === null) return;
        rows = next;
        deps.on_change();
    }

    async function refresh_restorable(): Promise<void> {
        const seq = ++restorable_seq;
        const next = restorable_count > 0 ? await fetch_restorable(deps.base_url, do_fetch) : [];
        if (seq !== restorable_seq || next === null) return;
        restorable_rows = next;
        deps.on_change();
    }

    function refresh(): void {
        void refresh_closed();
        void refresh_restorable();
    }

    function toggle(): void {
        open = !open;
        if (open) refresh();
        deps.on_change();
    }

    async function reopen(id: string): Promise<void> {
        if (busy()) return;
        reopening = id;
        deps.on_change();
        const outcome = await reopen_closed(deps.base_url, id, do_fetch);
        reopening = null;
        if (outcome.ok) {
            rows = rows?.filter((row) => row.id !== id) ?? null;
            deps.on_reopened(outcome.session, outcome.profile);
        } else {
            deps.on_failure(outcome.error);
            void refresh_closed();
        }
        deps.on_change();
    }

    async function restore(session: string | null): Promise<void> {
        if (busy()) return;
        restoring = session ?? '*';
        deps.on_change();
        const outcome = await restore_sessions(deps.base_url, session === null ? null : [session], do_fetch);
        restoring = null;
        if (!outcome.ok) {
            deps.on_failure(outcome.error);
        } else {
            const back = new Set(outcome.restored);
            // Read before the row leaves the list: the pane's first URL wants the profiled form.
            const profile = restorable_rows?.find((row) => row.session === session)?.profile ?? null;
            restorable_rows = restorable_rows?.filter((row) => !back.has(row.session)) ?? null;
            const notice = restore_notice(outcome);
            if (notice !== null) deps.on_failure(notice);
            if (session !== null && back.has(session)) {
                deps.on_reopened(session, profile);
            } else if (back.size > 0) {
                deps.on_restored?.(outcome.restored);
            }
        }
        void refresh_restorable();
        deps.on_change();
    }

    async function forget_all(): Promise<void> {
        if (busy() || !restorable_rows?.length) return;
        const names = restorable_rows.map((row) => row.session);
        if (!(await forget_restorable(deps.base_url, names, do_fetch))) {
            deps.on_failure('Dismiss failed: the bridge did not answer');
            return;
        }
        restorable_rows = [];
        deps.on_change();
    }

    return {
        observe_count: (next, next_restorable) => {
            const n = next ?? 0;
            const r = next_restorable ?? 0;
            const closed_moved = n !== count;
            const restorable_moved = r !== restorable_count;
            if (!closed_moved && !restorable_moved) return;
            count = n;
            restorable_count = r;
            if (count === 0 && restorable_count === 0 && !busy()) {
                // Nothing left to offer: the list folds itself away with its button.
                open = false;
                rows = null;
                restorable_rows = null;
                return;
            }
            if (!open) return;
            if (closed_moved) void refresh_closed();
            if (restorable_moved) void refresh_restorable();
        },
        toggle,
        reopen,
        restore,
        forget_restorable: forget_all,
        handle_action: (action, value) => {
            if (action === 'closed') toggle();
            else if (action === 'reopen') {
                if (value) void reopen(value);
            } else if (action === 'restore') void restore(value || null);
            else if (action === 'restore_forget') void forget_all();
            else return false;
            return true;
        },
        view: () => {
            if (count === 0 && restorable_count === 0 && !open && !busy()) return undefined;
            const restorable: RestorableView | undefined =
                restorable_count > 0 || restoring !== null || (restorable_rows?.length ?? 0) > 0
                    ? { count: restorable_count, rows: restorable_rows, ...(restoring !== null ? { restoring } : {}) }
                    : undefined;
            return {
                count,
                open,
                rows,
                ...(reopening !== null ? { reopening } : {}),
                ...(restorable ? { restorable } : {}),
            };
        },
    };
}
