// Cross-window message bus over one BroadcastChannel (same origin, all tabs and
// windows; for one release its legacy twin too, LEGACY_CHANNEL_NAME). It is the transport between a host application's main
// window and its satellite windows, the rack page among them when it runs with
// `feed: 'bus'` (`agent-rack/AgentRackWindow.vue`). The messages a rack page
// sends and reads: owned by `companion/companion-bus.ts`.
//
// Module-scope state is per window (each tab is its own JS realm), which is
// what is wanted: one channel and one identity per window.

export type WindowRole = 'main' | 'companion';

export const CHANNEL_NAME = 'tldrack-instance';
/**
 * Legacy (step 6.8): the channel's name before 0.1.0. For one release a window posts on both and
 * listens on both, because a satellite outlives a deploy of its main window and the reverse. A copy
 * posted here carries `bus: 2`, so a window that also heard it on CHANNEL_NAME drops it
 * (`accept_envelope`); a window one release behind reads only this channel and ignores the field.
 */
export const LEGACY_CHANNEL_NAME = 'voidcraft-instance';
const BUS_VERSION = 2;

// Per-window identity. Date.now()/Math.random() are fine in app code.
const instance_id = `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;

export interface BusEnvelope {
    type: string;
    payload?: any;
    sender_id: string;
    sender_role: WindowRole;
    /** Set by a window that posts on both channels; absent from a window one release behind. */
    bus?: number;
}

/** Whether a window handles `msg`, heard on the legacy channel or on CHANNEL_NAME. Pure. */
export function accept_envelope(msg: BusEnvelope | null | undefined, legacy: boolean, self_id: string): msg is BusEnvelope {
    if (!msg || typeof msg.type !== 'string' || msg.sender_id === self_id) return false;
    return !(legacy && msg.bus === BUS_VERSION);
}

export interface BusMeta {
    sender_id: string;
    sender_role: WindowRole;
}

type BusHandler = (payload: any, meta: BusMeta) => void;

let channel: BroadcastChannel | null = null;
let legacy_channel: BroadcastChannel | null = null;
let role: WindowRole = 'main';
const handlers = new Map<string, Set<BusHandler>>();

function deliver(msg: BusEnvelope | null | undefined, legacy: boolean): void {
    // BroadcastChannel never echoes our own posts, but accept_envelope guards anyway.
    if (!accept_envelope(msg, legacy, instance_id)) {
        return;
    }
    const set = handlers.get(msg.type);
    if (!set) {
        return;
    }
    const meta: BusMeta = { sender_id: msg.sender_id, sender_role: msg.sender_role };
    for (const handler of set) {
        handler(msg.payload, meta);
    }
}

function ensure_channel(): void {
    if (channel || typeof BroadcastChannel === 'undefined') {
        return;
    }
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.onmessage = (event: MessageEvent<BusEnvelope>) => deliver(event.data, false);
    legacy_channel = new BroadcastChannel(LEGACY_CHANNEL_NAME);
    legacy_channel.onmessage = (event: MessageEvent<BusEnvelope>) => deliver(event.data, true);
}

export function useWindowBus() {
    ensure_channel();

    /** Broadcast a typed message to every other window on this origin. */
    const post = (type: string, payload?: any): void => {
        ensure_channel();
        const envelope: BusEnvelope = { type, payload, sender_id: instance_id, sender_role: role, bus: BUS_VERSION };
        channel?.postMessage(envelope);
        legacy_channel?.postMessage(envelope);
    };

    /** Subscribe to a message type. Returns an unsubscribe function. */
    const on = (type: string, handler: BusHandler): (() => void) => {
        let set = handlers.get(type);
        if (!set) {
            set = new Set();
            handlers.set(type, set);
        }
        set.add(handler);
        return () => {
            handlers.get(type)?.delete(handler);
        };
    };

    const off = (type: string, handler: BusHandler): void => {
        handlers.get(type)?.delete(handler);
    };

    /** A satellite window calls this early (before any post), so a main window that
     *  watches for a second copy of itself reads it as a satellite, not as one. */
    const set_role = (next: WindowRole): void => {
        role = next;
    };

    const get_role = (): WindowRole => role;

    return { post, on, off, set_role, get_role, instance_id };
}
