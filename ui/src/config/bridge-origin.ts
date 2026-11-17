/**
 * Which bridge a page talks to. ONE owner, so no client spells an address of its own.
 *
 * The default is the bridge's default address on this machine, which is right for a host
 * application that embeds a component and runs beside a default bridge. A page the bridge
 * itself serves sets it to its own origin before it mounts (`page_bridge_origin`), so a bridge
 * on any port, or behind a proxy, is the one its own pages call.
 *
 * Module state on purpose: the clients are plain functions called from many components, and
 * threading an origin through every one of them would put the same argument on every call.
 * Set it once, before mount, and never after: a page talks to one bridge for its whole life.
 */
import { DEFAULT_STATUS_PORT } from './terminal-status-flags.ts';

let origin = `http://127.0.0.1:${DEFAULT_STATUS_PORT}`;

export function bridge_origin(): string {
    return origin;
}

/** `next` must already be validated: every client fetches from it. */
export function set_bridge_origin(next: string): void {
    origin = next;
}
