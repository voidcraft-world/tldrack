/**
 * The one reader of `/update` on a page: it asks, folds each answer into the state
 * (`update-status.ts`), and decides when to ask again. Free of Vue and of every browser global at
 * import, so it runs under bare `node --test` with a fake clock; `useUpdateStatus.ts` hands it the
 * page.
 *
 * WHEN IT READS. This page was made light on purpose, so there is one slow timer and no more:
 *
 *   - once when the first subscriber arrives;
 *   - when the operator comes back to the page (`agent-rack/page-return.ts`): always when the
 *     tab is shown again, and for a window `focus` only if the last read is old;
 *   - every `SLOW_POLL_MS` while the tab is visible, and every `FAST_POLL_MS` while a check or a
 *     run is in flight (`update-status.ts` `poll_delay_ms` owns both numbers);
 *   - when a caller says the bridge just came back (`refresh`): a restart is how an update ends.
 *
 * A hidden tab sets no timer at all. The last subscriber to leave stops everything.
 */
import type { PageReturn } from '../agent-rack/page-return.ts';
import type { UpdateAction, UpdateReply } from './update-client.ts';
import { after_press, after_reply, in_flight, initial_update_state, poll_delay_ms, reads_on_return, type UpdateState } from './update-status.ts';

export interface UpdatePollerDeps {
    read: () => Promise<UpdateReply>;
    post: (action: UpdateAction) => Promise<UpdateReply>;
    now: () => number;
    is_hidden: () => boolean;
    set_timer: (run: () => void, ms: number) => unknown;
    clear_timer: (handle: unknown) => void;
    watch_return: (on_return: (how: PageReturn) => void) => () => void;
    /** `update-status.ts` owns the rule: true only on a page the bridge itself served. */
    tracks_page: boolean;
}

export interface UpdatePoller {
    state: () => UpdateState;
    /** The first subscriber starts the reads, the last to leave stops them. Returns the unsubscribe. */
    subscribe: (listener: (state: UpdateState) => void) => () => void;
    /** Read now, unless a read is already out. */
    refresh: () => void;
    /** CHECK NOW or UPDATE. Resolves when the bridge has answered the press; the run is then followed by the reads. */
    press: (action: UpdateAction) => Promise<void>;
}

export function create_update_poller(deps: UpdatePollerDeps): UpdatePoller {
    const listeners = new Set<(state: UpdateState) => void>();
    let state = initial_update_state(deps.tracks_page);
    let timer: unknown = null;
    let stop_return: (() => void) | null = null;
    let reading = false;
    let pressing = false;
    let last_read_ms = -Infinity;
    /** Bumped by a press and by the last unsubscribe: a read sent before either describes a world that is gone. */
    let generation = 0;

    function set(next: UpdateState): void {
        state = next;
        for (const listener of listeners) listener(state);
    }

    function schedule(): void {
        if (timer !== null) deps.clear_timer(timer);
        timer = null;
        if (listeners.size === 0) return;
        const delay = poll_delay_ms(state, deps.is_hidden());
        if (delay === null) return;
        timer = deps.set_timer(() => {
            timer = null;
            // Hidden since the timer was set: rest, and let the return do the read.
            if (!deps.is_hidden()) void read();
        }, delay);
    }

    async function read(): Promise<void> {
        if (reading || pressing || listeners.size === 0) return;
        const mine = generation;
        reading = true;
        last_read_ms = deps.now();
        const reply = await deps.read();
        reading = false;
        if (mine !== generation) {
            // Answered for a world that is gone. Whoever is here now is owed a read of their own,
            // unless a press is out: its answer is one.
            if (!pressing) void read();
            return;
        }
        set(after_reply(state, reply, deps.now(), 'read'));
        schedule();
    }

    async function press(action: UpdateAction): Promise<void> {
        if (pressing || in_flight(state)) return;
        pressing = true;
        generation += 1;
        set(after_press(state, action, deps.now()));
        const reply = await deps.post(action);
        pressing = false;
        set(after_reply(state, reply, deps.now(), 'press'));
        // A press the bridge refused may have been refused for something the view on screen
        // does not show yet (a pull that became blocked): read it rather than wait five minutes.
        if (reply.kind === 'view') schedule();
        else void read();
    }

    function start(): void {
        stop_return = deps.watch_return((how) => {
            if (reads_on_return(how, deps.now() - last_read_ms)) void read();
        });
        void read();
    }

    function stop(): void {
        stop_return?.();
        stop_return = null;
        if (timer !== null) deps.clear_timer(timer);
        timer = null;
        generation += 1;
    }

    return {
        state: () => state,
        subscribe(listener) {
            listeners.add(listener);
            if (listeners.size === 1) start();
            return () => {
                if (!listeners.delete(listener)) return;
                if (listeners.size === 0) stop();
            };
        },
        refresh: () => void read(),
        press,
    };
}
