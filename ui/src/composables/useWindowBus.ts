// Cross-window message bus over a single BroadcastChannel (same origin, all
// tabs and windows). It is the transport between a host application's main
// window and its satellite windows, the rack page among them when it runs with
// `feed: 'bus'` (`agent-rack/AgentRackWindow.vue`). The messages a rack page
// sends and reads: owned by `companion/companion-bus.ts`.
//
// Module-scope state is per window (each tab is its own JS realm), which is
// what is wanted: one channel and one identity per window.

export type WindowRole = 'main' | 'companion';

const CHANNEL_NAME = 'voidcraft-instance';

// Per-window identity. Date.now()/Math.random() are fine in app code.
const instance_id = `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;

interface BusEnvelope {
    type: string;
    payload?: any;
    sender_id: string;
    sender_role: WindowRole;
}

export interface BusMeta {
    sender_id: string;
    sender_role: WindowRole;
}

type BusHandler = (payload: any, meta: BusMeta) => void;

let channel: BroadcastChannel | null = null;
let role: WindowRole = 'main';
const handlers = new Map<string, Set<BusHandler>>();

function ensure_channel(): void {
    if (channel || typeof BroadcastChannel === 'undefined') {
        return;
    }
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.onmessage = (event: MessageEvent<BusEnvelope>) => {
        const msg = event.data;
        if (!msg || msg.sender_id === instance_id) {
            return; // BroadcastChannel never echoes our own posts, but guard anyway.
        }
        const set = handlers.get(msg.type);
        if (!set) {
            return;
        }
        const meta: BusMeta = { sender_id: msg.sender_id, sender_role: msg.sender_role };
        for (const handler of set) {
            handler(msg.payload, meta);
        }
    };
}

export function useWindowBus() {
    ensure_channel();

    /** Broadcast a typed message to every other window on this origin. */
    const post = (type: string, payload?: any): void => {
        ensure_channel();
        channel?.postMessage({ type, payload, sender_id: instance_id, sender_role: role } as BusEnvelope);
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
