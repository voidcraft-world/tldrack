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
 */
import { parse_office_resources, type OfficeResources } from './office-resources.ts';
import { z } from 'zod';
import { session_lineage_schema } from '@tldrack/schemas/session-lineage.ts';
import type { RackSession } from './multi-terminal-layout.ts';
import type { TerminalDisplayState } from './terminal-status-logic.ts';

export const OFFICE_CHANNEL = 'tldrack:agent-office';
/** The office's HELLO cadence. */
export const OFFICE_HEARTBEAT_MS = 4000;
/** A rack stops relaying this long after the last HELLO (three missed beats). */
export const OFFICE_LISTENER_TTL_MS = 12_000;
/** The office calls its rack stale this long after the last one arrived. */
export const OFFICE_STALE_MS = 15_000;
/** A rack bigger than this is malformed, not large. */
const MAX_SESSIONS = 500;

/** The rack's sessions, as the office reads it. */
export interface OfficeFleet {
    sessions: RackSession[];
    resources?: OfficeResources;
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
    /** rack → offices: the rack. */
    | { type: 'office:fleet'; fleet: OfficeFleet };

const STATES: readonly TerminalDisplayState[] = ['busy', 'background', 'attention', 'free', 'unknown', 'done'];

/** The fields the office draws, and nothing else: the projection a rack relays. */
export function office_session(entry: RackSession): RackSession {
    return {
        session: entry.session,
        state: entry.state,
        live: entry.live,
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
});

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
    const model_label = optional_string(row.model_label);
    const repo = optional_string(row.repo);
    const cwd = optional_string(row.cwd);
    return {
        session,
        state,
        live: row.live !== false,
        ...(auto_name ? { auto_name } : {}),
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
        case 'office:fleet': {
            const fleet = message.fleet as Record<string, unknown> | null | undefined;
            if (typeof fleet !== 'object' || fleet === null || !Array.isArray(fleet.sessions)) return null;
            if (fleet.sessions.length > MAX_SESSIONS) return null;
            const sessions = fleet.sessions.map(parse_office_session).filter((row): row is RackSession => row !== null);
            const resources = parse_office_resources(fleet.resources);
            return {
                type: 'office:fleet',
                fleet: {
                    sessions,
                    focused: optional_string(fleet.focused) ?? null,
                    reachable: fleet.reachable !== false,
                    frozen: fleet.frozen === true,
                    ...(resources ? { resources } : {}),
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
}

/** What a rack relays for its payload. */
export function office_fleet(payload: OfficeSource): OfficeFleet {
    return {
        sessions: payload.sessions.map(office_session),
        focused: payload.focused,
        reachable: payload.reachable,
        frozen: payload.attached === false,
        ...(payload.office_resources ? { resources: payload.office_resources } : {}),
    };
}

/** The slice of BroadcastChannel both ends use — injectable so the relay is testable. */
export interface OfficeChannel {
    postMessage(message: OfficeMessage): void;
    close(): void;
    onmessage: ((event: { data: unknown }) => void) | null;
}

/**
 * The RACK's end: relay each payload while an office listens, and hand an
 * office's click back as a `select`. Owned by whoever owns the rack's transport
 * (`AgentRackComponent.vue`); one per rack document.
 */
export function create_office_relay(opts: {
    on_select: (session: string) => void;
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
        if (last && channel) channel.postMessage({ type: 'office:fleet', fleet: office_fleet(last) });
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
