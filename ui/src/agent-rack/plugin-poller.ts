/**
 * One bridge plugin's `GET`, polled while somebody wants it, re-rendering the rack only on a
 * VISIBLE change. The dev-services readout and the DEVICES section both ride it.
 *
 * Every `on_change` re-renders the whole rack, and a host with a framed rack re-posts the payload
 * into the frame, so a poller that notified per answer would repaint every few seconds for text
 * nobody can see move. `signature` is the visible surface: two answers with one signature are one
 * answer.
 *
 * Free of the DOM, like `worktree-client.ts`: every rack payload owner loads it, and it runs
 * under bare `node --test`.
 */
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { poller_io, type BaseUrl, type FetchImpl, type PollerIoDeps } from './worktree-client.ts';

/** The same budget as the status poll; loopback answers in milliseconds. */
const STATE_TIMEOUT_MS = 2500;

/**
 * `path`'s JSON through `parse`, or null when the plugin is not mounted (a 404) or the bridge
 * did not answer. Null grows no UI: an older bridge simply never had the surface.
 */
export async function fetch_plugin_state<T>(
    base_url: BaseUrl,
    path: string,
    parse: (body: unknown) => T | null,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<T | null> {
    try {
        const response = await fetch_impl(base_url(path), { signal: AbortSignal.timeout(STATE_TIMEOUT_MS) });
        if (!response.ok) return null;
        return parse(await response.json());
    } catch {
        return null;
    }
}

export interface PluginPollerDeps<T> extends PollerIoDeps {
    fetch: (do_fetch: FetchImpl) => Promise<T | null>;
    /** What the rack can SEE of an answer; equal signatures do not notify. */
    signature: (state: T | null) => string;
    on_change: () => void;
    /** Read on every tick: while false, the poller sends nothing at all. */
    wanted: () => boolean;
    poll_interval_ms: number;
}

export interface PluginPoller<T> {
    /** The last answer, or null (no plugin / not asked yet). */
    state: () => T | null;
    tick: () => Promise<void>;
    start: () => void;
    stop: () => void;
}

export function create_plugin_poller<T>(deps: PluginPollerDeps<T>): PluginPoller<T> {
    const { do_fetch, start_timer, stop_timer } = poller_io(deps);

    let state: T | null = null;
    let signature = '';
    let timer: ReturnType<typeof setInterval> | null = null;
    let in_flight = false;

    async function tick(): Promise<void> {
        if (in_flight || !deps.wanted()) return;
        in_flight = true;
        try {
            const next = await deps.fetch(do_fetch);
            const next_signature = deps.signature(next);
            if (next_signature === signature) return;
            signature = next_signature;
            state = next;
            deps.on_change();
        } finally {
            in_flight = false;
        }
    }

    return {
        state: () => state,
        tick,
        start: () => {
            if (timer !== null) return;
            void tick();
            timer = start_timer(() => void tick(), deps.poll_interval_ms);
        },
        stop: () => {
            if (timer !== null) stop_timer(timer);
            timer = null;
        },
    };
}
