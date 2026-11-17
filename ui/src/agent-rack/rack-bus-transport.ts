/**
 * A satellite window's half of the rack transport: the same two operations, but
 * over the cross-window BroadcastChannel instead of `window.parent`.
 *
 * A SEPARATE FILE from `rack-transport.ts` on purpose. A framed rack
 * (`mount_rack_frame` in `mount.ts`) can never take this code path, so importing
 * the bus there would be pure cost. Only the rack page
 * (`AgentRackWindow.vue`) imports this.
 *
 * There is no `window.parent` to talk to here: in a window of its own
 * `window.parent === window`, so the frame transport would post to itself and
 * nothing would ever listen.
 */
import { useWindowBus } from '../composables/useWindowBus.ts';
import { COMPANION_BUS } from '../companion/companion-bus.ts';
import type { RackPayload, RackTransport } from './rack-transport.ts';

export function create_bus_transport(): RackTransport {
    const bus = useWindowBus();
    return {
        send: (action, session, value) => bus.post(COMPANION_BUS.RACK_ACTION, { action, session, value }),
        subscribe: (on_payload) =>
            bus.on(COMPANION_BUS.RACK_DATA, (payload) => on_payload(payload as RackPayload)),
    };
}
