/**
 * Service sessions: the tmux sessions that RUN things for the operator. A supervised service
 * (`svc-*`) and the one-shot JOBS a plugin starts in a watchable pane.
 *
 * WHY THEY ARE A BAND. Every tmux session is a rack row, so without this a dev server and a job
 * pane sit interleaved with the agents, sorted by the same urgency ladder: a plain shell is
 * `unknown`, which files it among the agents nobody has heard from. They answer a different
 * question (is the machinery up?), so they get ONE band, by name prefix, and not a cohort per
 * repository. The prefixes: owned by `config/service-session-flags.ts` — see the note there.
 *
 * A job stays on screen after it ends (`remain-on-exit`) with its log and exit status, so the
 * band also answers whether a finished pane can be closed (`exited_readout`).
 *
 * Dependency-free (no DOM), so `multi-terminal-layout.ts` and `rack-view.ts` load it under bare
 * `node --test`.
 */
// Type-only, so the cycle with the layout module is erased at runtime.
import type { ContextTone } from './multi-terminal-layout.ts';
import { SERVICE_SESSION_FLAGS } from '../../config/service-session-flags.ts';

/** The band's header. Uppercase mono, like every FUI head. */
export const SERVICES_COHORT_LABEL = 'SERVICES';

/**
 * Also the reason these rows offer no rename: each name is how its plugin finds the session
 * again. Renaming a supervised `svc-*` session would make its supervisor read the service as
 * stopped and start a second one. Reads the flags on every call, so a prefix a host adds after
 * load counts.
 */
export function is_service_session(session: string): boolean {
    const { service_prefixes, job_prefixes } = SERVICE_SESSION_FLAGS;
    return service_prefixes.some((prefix) => session.startsWith(prefix))
        || job_prefixes.some((prefix) => session.startsWith(prefix));
}

/**
 * The readout of a session whose pane has ENDED (the bridge's `exited`, owned
 * by `bridge/tldrack/snapshot.py` — see the note there):
 * `✓ DONE`, `✗ EXIT 1`. Nothing runs in it any more, so the title says the one
 * thing the row exists to answer — it is safe to close.
 */
export function exited_readout(session: string, exit_code: number | null): ServiceReadout {
    const ok = exit_code === 0;
    const text = ok ? '✓ DONE' : exit_code === null ? '✗ ENDED' : `✗ EXIT ${exit_code}`;
    const how = ok ? 'finished' : exit_code === null ? 'ended' : `failed (exit ${exit_code})`;
    return {
        text,
        ...(ok ? {} : { tone: 'critical' as const }),
        title: `${how} — nothing runs here now; the pane only keeps its log (tmux attach -t ${session}). Safe to close with ✕.`,
    };
}

/** The rack mark of a service row no agent hooks report on. */
export const SERVICE_SESSION_EMOJI = '🖥️';

/**
 * Does this row draw {@link SERVICE_SESSION_EMOJI} in place of its state mark?
 * Only while nothing reports a state: a dev server is a plain shell (`unknown`, which would
 * read ◌ "no status"). An agent a job starts in its own pane is a hooked session, and keeps its
 * 🙋/🤔/✅, which carry its state.
 */
export function shows_service_mark(session: string | undefined, state: string | undefined): boolean {
    return session !== undefined && is_service_session(session) && (state ?? 'unknown') === 'unknown';
}

// ── dev-services (`GET /plugin/dev-services/state`) ──────────────────────────
//
// The wire shape of a supervisor plugin mounted as `dev-services`, restated here structurally
// because a TS/Python boundary has no shared owner. Additive: keys may be added, never renamed.
// No such plugin ships in this repository; a bridge without one answers 404 and grows no UI.

export type DevServiceStatus = 'running' | 'starting' | 'stopped' | 'failed' | 'external';

const STATUSES: ReadonlySet<string> = new Set<DevServiceStatus>(['running', 'starting', 'stopped', 'failed', 'external']);

export interface DevServiceRow {
    /** `vite` | `ext` today; kept open so a new service needs no client release. */
    key: string;
    /** `VITE`, `EXT` — what the readout prints. */
    label: string;
    /** The tmux session the plugin supervises it in (`svc-vite`). */
    session: string;
    status: DevServiceStatus;
    port: number | null;
    pid: number | null;
    /** A NON-svc process holding the port (status `external`). */
    external_pid: number | null;
    /** Set when `failed` — the dead pane's exit status. */
    exit_code: number | null;
    /** ≤ 20 lines; `[]` when none. */
    log_tail: string[];
    since: string | null;
}

/** The plugin's own list cap; a client-side cap keeps a hostile body off a tooltip. */
const LOG_TAIL_MAX = 20;

function int_or_null(value: unknown): number | null {
    return typeof value === 'number' && Number.isInteger(value) ? value : null;
}

function parse_service(raw: unknown): DevServiceRow | null {
    if (!raw || typeof raw !== 'object') return null;
    const row = raw as Record<string, unknown>;
    if (typeof row.key !== 'string' || typeof row.session !== 'string') return null;
    // An unknown status is NOT dropped as "stopped": a newer plugin's state reads
    // as starting (in flux) rather than as a claim that nothing is running.
    const status = typeof row.status === 'string' && STATUSES.has(row.status) ? (row.status as DevServiceStatus) : 'starting';
    return {
        key: row.key,
        label: typeof row.label === 'string' && row.label ? row.label : row.key.toUpperCase(),
        session: row.session,
        status,
        port: int_or_null(row.port),
        pid: int_or_null(row.pid),
        external_pid: int_or_null(row.external_pid),
        exit_code: int_or_null(row.exit_code),
        log_tail: Array.isArray(row.log_tail)
            ? row.log_tail.filter((line): line is string => typeof line === 'string').slice(-LOG_TAIL_MAX)
            : [],
        since: typeof row.since === 'string' ? row.since : null,
    };
}

/**
 * `/state`'s body → its service rows, or null when it is not a dev-services
 * document at all. Null is "the plugin is not mounted", which grows no UI.
 */
export function parse_dev_services(body: unknown): DevServiceRow[] | null {
    if (!body || typeof body !== 'object') return null;
    const services = (body as Record<string, unknown>).services;
    if (!Array.isArray(services)) return null;
    return services.map(parse_service).filter((row): row is DevServiceRow => row !== null);
}

/** The supervised service a rack row stands for, by its tmux session name. */
export function service_for_session(
    session: string,
    services: readonly DevServiceRow[] | undefined,
): DevServiceRow | undefined {
    return services?.find((row) => row.session === session);
}

// ── the readout ─────────────────────────────────────────────────────────────

/** One glyph per status, so every surface that lists a service reads the same mark. */
export const SERVICE_GLYPH: Readonly<Record<DevServiceStatus, string>> = {
    running: '●',
    starting: '◌',
    stopped: '○',
    failed: '✗',
    external: '⚠',
};

export interface ServiceReadout {
    /** `● VITE :5173` — the row's segment. */
    text: string;
    /** The rack's segment tint; absent for a healthy service, so it draws in
     *  the row's own colour. */
    tone?: ContextTone;
    /** The sentence version, for `title`. */
    title: string;
}

/**
 * What follows the glyph: the port while it serves, the exit status once it
 * died, who holds the port when it is not ours.
 */
export function service_detail(row: DevServiceRow): string {
    switch (row.status) {
        case 'running':
            return row.port !== null ? `:${row.port}` : 'UP';
        case 'starting':
            return 'STARTING';
        case 'stopped':
            return 'STOPPED';
        case 'failed':
            return row.exit_code !== null ? `EXIT ${row.exit_code}` : 'FAILED';
        case 'external':
            return row.port !== null ? `EXTERNAL :${row.port}` : 'EXTERNAL';
    }
}

/** The sentence version of the readout. */
export function service_title(row: DevServiceRow): string {
    const attach = `tmux attach -t ${row.session}`;
    switch (row.status) {
        case 'running':
            return `${row.label} is serving${row.port !== null ? ` on :${row.port}` : ''} — log: ${attach}`;
        case 'starting':
            return `${row.label} is starting — log: ${attach}`;
        case 'stopped':
            return `${row.label} is stopped`;
        case 'failed': {
            const tail = row.log_tail.slice(-3).join('\n');
            return `${row.label} exited${row.exit_code !== null ? ` ${row.exit_code}` : ''} — ${attach}${tail ? `\n${tail}` : ''}`;
        }
        case 'external':
            return `Another process${row.external_pid !== null ? ` (pid ${row.external_pid})` : ''} holds the port — ADOPT moves it into ${row.session}`;
    }
}

const TONE: Readonly<Record<DevServiceStatus, ContextTone | undefined>> = {
    running: undefined,
    starting: 'low',
    stopped: 'low',
    failed: 'critical',
    external: 'low',
};

export function service_readout(row: DevServiceRow): ServiceReadout {
    const tone = TONE[row.status];
    return {
        text: `${SERVICE_GLYPH[row.status]} ${row.label} ${service_detail(row)}`,
        ...(tone ? { tone } : {}),
        title: service_title(row),
    };
}
