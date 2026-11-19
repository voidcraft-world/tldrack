/**
 * The wire between an agent RACK and the OFFICE window (`office.html`).
 *
 * THE OFFICE DOES NO WORK. It never polls the bridge: whichever rack document is
 * open (the rack page, a framed rack in a host, a rack pointed at another
 * machine) already holds the rack, and relays a projection of it here. A second
 * poller would be a second writer on every notification tag.
 *
 * WHY A BroadcastChannel AND NOT `window.opener`. The opener link dies whenever
 * the rack's document reloads, and a reloaded rack cannot reach a window it did
 * not open. A named channel is found again by both sides, in either order, on
 * every load. It is DEDICATED: never the window bus
 * (`composables/useWindowBus.ts`), whose traffic belongs to a host's main window.
 *
 * THE HANDSHAKE. The office says HELLO on load and every `OFFICE_HEARTBEAT_MS`;
 * a rack relays only while it heard one inside `OFFICE_LISTENER_TTL_MS`, so a
 * rack with no office open serialises nothing. A rack answers a HELLO with its
 * last rack at once, so a freshly opened office never waits a whole poll.
 *
 * Every message is re-validated on arrival: same origin is not the same as
 * well-formed. The rack carries only the fields the office draws.
 *
 * THE OFFICE'S MENU ACTS THROUGH THE RACK. A right-click on a robot offers what its
 * rack row offers: close the session, open the PR, clean up the landed worktree. The
 * office sends the verb and the session, never a branch or a path, and the rack runs
 * it through the transport its own buttons use. The rack re-checks the verb against
 * the rack it holds NOW (`office_action_allowed`): the office's picture is up to a
 * poll old, and a PR button that became CLEAN under an armed menu must not clean.
 */
import { parse_status_emoji, session_status_emoji } from './session-status-emoji.ts';
import { parse_office_resources, type OfficeResources } from './office-resources.ts';
import { office_accounts, office_machines, parse_office_board, type OfficeBoardData } from './office-board.ts';
import type { DeviceRow } from './device-readout.ts';
import { z } from 'zod';
import { session_lineage_schema } from '@tldrack/schemas/session-lineage.ts';
import type { RackSession } from './multi-terminal-layout.ts';
import type { ProfileInfo, TerminalDisplayState } from './terminal-status-logic.ts';
import { worktree_button_view, worktree_for_session, type WorktreeButtonRow, type WorktreeRow } from './rack-view.ts';

export const OFFICE_CHANNEL = 'tldrack:agent-office';
/** The office's HELLO cadence. */
export const OFFICE_HEARTBEAT_MS = 4000;
/** A rack stops relaying this long after the last HELLO (three missed beats). */
export const OFFICE_LISTENER_TTL_MS = 12_000;
/** The office calls its rack stale this long after the last one arrived. */
export const OFFICE_STALE_MS = 15_000;
/** A rack bigger than this is malformed, not large. */
const MAX_SESSIONS = 500;

/** A verb the office's menu sends: each is the `RackAction` of the same name. */
export type OfficeAction = 'kill' | 'pr' | 'cleanup';
const ACTIONS: readonly OfficeAction[] = ['kill', 'pr', 'cleanup'];

/** The rack's sessions, as the office reads it. */
export interface OfficeRack {
    sessions: RackSession[];
    resources?: OfficeResources;
    /** The managed worktree behind a session, by session name: what its PR / CLEAN
     *  button is drawn from. Absent for a session with none, and while one is being created. */
    worktrees?: Record<string, WorktreeButtonRow>;
    /** The accounts and machines on the wall monitor (`office-board.ts`). Absent where the
     *  rack has neither to report. */
    board?: OfficeBoardData;
    focused: string | null;
    /** The bridge answered on the rack's last poll. */
    reachable: boolean;
    /** The rack cannot service a click right now (`RackPayload.attached === false`). */
    frozen: boolean;
}

export type OfficeMessage =
    /** office → racks: I am open (and every heartbeat). */
    | { type: 'office:hello' }
    /** office → racks: focus this session in the live pane. */
    | { type: 'office:select'; session: string }
    /** office → racks: a menu verb on this session. */
    | { type: 'office:action'; action: OfficeAction; session: string }
    /** rack → offices: the rack. */
    | { type: 'office:rack'; rack: OfficeRack };

const STATES: readonly TerminalDisplayState[] = ['busy', 'background', 'attention', 'free', 'unknown', 'done'];

/** The fields the office draws, and nothing else: the projection a rack relays. */
export function office_session(entry: RackSession, now_s = Date.now() / 1000): RackSession {
    return {
        session: entry.session,
        state: entry.state,
        live: entry.live,
        status_emoji: session_status_emoji({ ...entry, dim: !entry.live }, now_s),
        ...(entry.auto_name ? { auto_name: entry.auto_name } : {}),
        ...(entry.model_label ? { model_label: entry.model_label } : {}),
        ...(valid_context(entry.context_left) ? { context_left: entry.context_left } : {}),
        ...(entry.repo ? { repo: entry.repo } : {}),
        ...(entry.cwd ? { cwd: entry.cwd } : {}),
        ...(entry.lineage ? { lineage: entry.lineage } : {}),
        ...(entry.activity ? { activity: entry.activity } : {}),
        ...(entry.island ? { island: entry.island } : {}),
    };
}

function valid_context(value: unknown): value is number {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

function optional_string(value: unknown): string | undefined {
    return typeof value === 'string' && value.length > 0 && value.length <= 400 ? value : undefined;
}

const session_island_schema = z.object({
    path: z.string().min(1).max(2048),
    label: z.string().min(1).max(400).nullable(),
    title: z.string().min(1).max(4096),
    pr: z.enum(['needed', 'handled', 'unknown']).optional(),
    pr_state: z.enum(['none', 'open', 'closed', 'merged']).optional(),
    landed: z.literal(true).optional(),
});

const LANDING_HOWS = ['ancestor', 'pr', 'squash', 'commits'] as const;
/** A PR link is opened by the office itself, so only a web address survives the wire. */
function web_url(value: string | null): string | null {
    if (value === null) return null;
    try {
        return /^https?:$/.test(new URL(value).protocol) ? value : null;
    } catch {
        return null;
    }
}
const office_worktree_schema = z.object({
    branch: z.string().min(1).max(400),
    dirty: z.boolean().nullable(),
    ahead: z.number().int().min(0),
    pr_status: z.enum(['opening', 'open', 'error']).nullable(),
    pr_url: z.string().max(2048).nullable().transform(web_url),
    pr_error: z.string().max(8192).nullable(),
    landed: z.boolean().nullable().optional(),
    landing: z.object({ how: z.enum(LANDING_HOWS), pr: z.number().int().nullable() }).nullable().optional(),
    cleanup_status: z.enum(['running', 'error']).nullable().optional(),
    cleanup_error: z.string().max(8192).nullable().optional(),
});

/** The fields the worktree button reads, and nothing else. */
function office_worktree(row: WorktreeRow): WorktreeButtonRow {
    return {
        branch: row.branch,
        dirty: row.dirty,
        ahead: row.ahead,
        pr_status: row.pr_status,
        pr_url: row.pr_url,
        pr_error: row.pr_error,
        ...(row.landed !== undefined ? { landed: row.landed } : {}),
        ...(row.landing !== undefined ? { landing: row.landing } : {}),
        ...(row.cleanup_status !== undefined ? { cleanup_status: row.cleanup_status } : {}),
        ...(row.cleanup_error !== undefined ? { cleanup_error: row.cleanup_error } : {}),
    };
}

/** The worktree whose button a session's rack row draws, or undefined. The join is the
 *  rack's own (`rack-view.ts::worktree_for_session`); a row still being created has no button. */
function button_row(session: RackSession, worktrees: readonly WorktreeRow[] | undefined): WorktreeRow | undefined {
    const row = worktree_for_session(session, worktrees);
    return row && row.create_status !== 'creating' ? row : undefined;
}

/** A session may be named anything, `__proto__` and `constructor` included: the map is
 *  written and read by OWN key only, so a name never reaches the object's prototype. */
function set_worktree(map: Record<string, WorktreeButtonRow>, session: string, row: WorktreeButtonRow): void {
    Object.defineProperty(map, session, { value: row, enumerable: true, writable: true, configurable: true });
}

/** The worktree behind `session` in a relayed rack, or undefined. */
export function office_worktree_for(rack: OfficeRack | null | undefined, session: string): WorktreeButtonRow | undefined {
    const map = rack?.worktrees;
    return map && Object.hasOwn(map, session) ? map[session] : undefined;
}

function parse_office_worktrees(raw: unknown, sessions: readonly RackSession[]): Record<string, WorktreeButtonRow> | undefined {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return undefined;
    const out: Record<string, WorktreeButtonRow> = {};
    // Read by the sessions that arrived, never by the map's own keys: a key is only ever a name the rack listed.
    for (const { session } of sessions) {
        if (!Object.hasOwn(raw, session)) continue;
        const row = office_worktree_schema.safeParse((raw as Record<string, unknown>)[session]);
        if (row.success) set_worktree(out, session, row.data);
    }
    return Object.keys(out).length ? out : undefined;
}

/** One relayed session, or null when it is not one. */
export function parse_office_session(raw: unknown): RackSession | null {
    if (typeof raw !== 'object' || raw === null) return null;
    const row = raw as Record<string, unknown>;
    const session = optional_string(row.session);
    if (!session) return null;
    const state = STATES.includes(row.state as TerminalDisplayState) ? (row.state as TerminalDisplayState) : 'unknown';
    const lineage = session_lineage_schema.safeParse(row.lineage);
    const island = session_island_schema.safeParse(row.island);
    const activity = row.activity as Record<string, unknown> | null | undefined;
    const zone = typeof activity === 'object' && activity !== null ? optional_string(activity.zone) : undefined;
    const auto_name = optional_string(row.auto_name);
    const status_emoji = parse_status_emoji(row.status_emoji);
    const model_label = optional_string(row.model_label);
    const repo = optional_string(row.repo);
    const cwd = optional_string(row.cwd);
    return {
        session,
        state,
        live: row.live !== false,
        ...(auto_name ? { auto_name } : {}),
        ...(status_emoji ? { status_emoji } : {}),
        ...(model_label ? { model_label } : {}),
        ...(valid_context(row.context_left) ? { context_left: row.context_left } : {}),
        ...(repo ? { repo } : {}),
        ...(cwd ? { cwd } : {}),
        ...(lineage.success ? { lineage: lineage.data } : {}),
        ...(island.success ? { island: island.data } : {}),
        ...(zone && activity
            ? {
                  activity: {
                      zone,
                      tool: optional_string(activity.tool) ?? null,
                      detail: optional_string(activity.detail) ?? null,
                  },
              }
            : {}),
    };
}

/** Any channel message → a typed one, or null. */
export function parse_office_message(raw: unknown): OfficeMessage | null {
    if (typeof raw !== 'object' || raw === null) return null;
    const message = raw as Record<string, unknown>;
    switch (message.type) {
        case 'office:hello':
            return { type: 'office:hello' };
        case 'office:select': {
            const session = optional_string(message.session);
            return session ? { type: 'office:select', session } : null;
        }
        case 'office:action': {
            const session = optional_string(message.session);
            const action = ACTIONS.find((known) => known === message.action);
            return session && action ? { type: 'office:action', action, session } : null;
        }
        // Legacy (step 6.8): a rack page one release behind relays `office:fleet` with the rack under `fleet`.
        case 'office:fleet':
        case 'office:rack': {
            const rack = (message.type === 'office:rack' ? message.rack : message.fleet) as Record<string, unknown> | null | undefined;
            if (typeof rack !== 'object' || rack === null || !Array.isArray(rack.sessions)) return null;
            if (rack.sessions.length > MAX_SESSIONS) return null;
            const sessions = rack.sessions.map(parse_office_session).filter((row): row is RackSession => row !== null);
            const resources = parse_office_resources(rack.resources);
            const worktrees = parse_office_worktrees(rack.worktrees, sessions);
            const board = parse_office_board(rack.board);
            return {
                type: 'office:rack',
                rack: {
                    sessions,
                    focused: optional_string(rack.focused) ?? null,
                    reachable: rack.reachable !== false,
                    frozen: rack.frozen === true,
                    ...(resources ? { resources } : {}),
                    ...(worktrees ? { worktrees } : {}),
                    ...(board ? { board } : {}),
                },
            };
        }
        default:
            return null;
    }
}

/** The part of a `RackPayload` the office reads. */
export interface OfficeSource {
    sessions: readonly RackSession[];
    focused: string | null;
    reachable: boolean;
    attached?: boolean;
    office_resources?: OfficeResources;
    worktrees?: readonly WorktreeRow[];
    profiles?: readonly ProfileInfo[];
    devices?: readonly DeviceRow[];
}

/** What a rack relays for its payload. */
export function office_rack(payload: OfficeSource): OfficeRack {
    const worktrees: Record<string, WorktreeButtonRow> = {};
    for (const session of payload.sessions) {
        const row = session.live ? button_row(session, payload.worktrees) : undefined;
        if (row) set_worktree(worktrees, session.session, office_worktree(row));
    }
    const board = parse_office_board({
        accounts: office_accounts(payload.profiles ?? []),
        machines: office_machines(payload.devices ?? []),
    });
    return {
        sessions: payload.sessions.map(entry => office_session(entry)),
        focused: payload.focused,
        reachable: payload.reachable,
        frozen: payload.attached === false,
        ...(payload.office_resources ? { resources: payload.office_resources } : {}),
        ...(Object.keys(worktrees).length ? { worktrees } : {}),
        ...(board ? { board } : {}),
    };
}

/**
 * May the office's menu run `action` on `session`, against the rack as it stands NOW?
 *
 * Both of the office's doors pass through here before a verb reaches the transport:
 * the relay below (the window) and `AgentRackWindow.vue` (the Canvas). The session must
 * be live in a rack that can act; `pr` and `cleanup` must be what that session's
 * worktree button would send on its second press this instant, so a verb armed against
 * an older picture dies here instead of doing the other thing.
 */
export function office_action_allowed(source: OfficeSource | null | undefined, action: OfficeAction, session: string): boolean {
    if (!source || !source.reachable || source.attached === false) return false;
    const row = source.sessions.find((entry) => entry.session === session);
    if (!row?.live) return false;
    if (action === 'kill') return true;
    const worktree = button_row(row, source.worktrees);
    if (!worktree) return false;
    const view = worktree_button_view(worktree, null);
    return view.kind === action && view.press === 'arm';
}

/** The slice of BroadcastChannel both ends use — injectable so the relay is testable. */
export interface OfficeChannel {
    postMessage(message: OfficeMessage): void;
    close(): void;
    onmessage: ((event: { data: unknown }) => void) | null;
}

/**
 * The RACK's end: relay each payload while an office listens, hand an office's
 * click back as a `select`, and its menu's verb back as an action once
 * `office_action_allowed` passes it. Owned by whoever owns the rack's transport
 * (`AgentRackComponent.vue`); one per rack document.
 */
export function create_office_relay(opts: {
    on_select: (session: string) => void;
    on_action: (action: OfficeAction, session: string) => void;
    channel?: OfficeChannel;
    now?: () => number;
}): { push: (payload: OfficeSource) => void; dispose: () => void } {
    const now = opts.now ?? (() => Date.now());
    const channel: OfficeChannel | null =
        opts.channel ?? (typeof BroadcastChannel === 'undefined' ? null : (new BroadcastChannel(OFFICE_CHANNEL) as unknown as OfficeChannel));
    let last_hello = Number.NEGATIVE_INFINITY;
    // The raw payload, projected only when relayed: a rack with no office open pays nothing.
    let last: OfficeSource | null = null;
    const relay = (): void => {
        if (last && channel) channel.postMessage({ type: 'office:rack', rack: office_rack(last) });
    };
    if (channel) {
        channel.onmessage = (event) => {
            const message = parse_office_message(event.data);
            if (message?.type === 'office:hello') {
                last_hello = now();
                // Every HELLO is answered, not only the first: a second office
                // opened inside the TTL must not wait a whole poll for its rack.
                relay();
            } else if (message?.type === 'office:select') {
                opts.on_select(message.session);
            } else if (message?.type === 'office:action') {
                if (office_action_allowed(last, message.action, message.session)) opts.on_action(message.action, message.session);
            }
        };
    }
    return {
        push: (payload) => {
            last = payload;
            if (now() - last_hello < OFFICE_LISTENER_TTL_MS) relay();
        },
        dispose: () => {
            if (!channel) return;
            channel.onmessage = null;
            channel.close();
        },
    };
}
