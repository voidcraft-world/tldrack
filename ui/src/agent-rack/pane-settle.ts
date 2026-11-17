/**
 * Which session the live pane is still LOADING — the rack locks that row's
 * name box until it is not.
 *
 * Why the lock exists: every navigation of the pane (`+ NEW`, a row select, a
 * spawn finishing) loads a fresh ttyd page, and xterm calls `term.focus()` when
 * its socket connects — a beat after the iframe's `load`. A rename the operator
 * opened in that window loses focus to the terminal, the input blurs, and
 * `cancel_rename` throws the half-typed name away. Locking the box until the
 * pane has settled is the only fix the rack can make: the steal happens inside
 * a cross-origin frame, and a blur caused by it is indistinguishable from the
 * operator clicking into the terminal on purpose.
 *
 * Each pane owner drives one of these (`AgentRackWindow.vue`, or a host that
 * frames the terminal itself), because only the owner sees its frame navigate
 * and `load`. Each overlays `loading()` onto the
 * payload it relays as `RackPayload.pane_loading`; no host ever sets it.
 *
 * `grace_ms` covers `load` → connect-time focus; `cap_ms` guarantees a `load`
 * that never arrives (a blocked frame, a raw ttyd) cannot strand a row locked.
 * Both are read per call, so a change to `MULTI_TERMINAL_FLAGS` applies live.
 */

export interface SettleTimers {
    set: (fn: () => void, ms: number) => unknown;
    clear: (handle: unknown) => void;
}

export interface PaneSettleOptions {
    on_change: () => void;
    grace_ms: () => number;
    cap_ms: () => number;
    /** Injected by tests; defaults to `setTimeout`/`clearTimeout`. */
    timers?: SettleTimers;
}

export interface PaneSettle {
    /** The pane's `src` was just pointed at `session` (null: blanked). */
    navigated: (session: string | null) => void;
    /** The pane's iframe fired `load`. */
    loaded: () => void;
    /** The session still loading, or null once settled. */
    loading: () => string | null;
    dispose: () => void;
}

const DEFAULT_TIMERS: SettleTimers = {
    set: (fn, ms) => setTimeout(fn, ms),
    clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function create_pane_settle(opts: PaneSettleOptions): PaneSettle {
    const timers = opts.timers ?? DEFAULT_TIMERS;
    let current: string | null = null;
    let cap: unknown = null;
    let grace: unknown = null;

    function stop_timers(): void {
        if (cap !== null) timers.clear(cap);
        if (grace !== null) timers.clear(grace);
        cap = null;
        grace = null;
    }

    function settle(): void {
        stop_timers();
        if (current === null) return;
        current = null;
        opts.on_change();
    }

    return {
        navigated(session) {
            stop_timers();
            const changed = current !== session;
            current = session;
            if (session !== null) cap = timers.set(settle, opts.cap_ms());
            if (changed) opts.on_change();
        },
        loaded() {
            // A `load` for `about:blank`, or one after the cap already fired.
            if (current === null || grace !== null) return;
            grace = timers.set(settle, opts.grace_ms());
        },
        loading: () => current,
        dispose() {
            stop_timers();
            current = null;
        },
    };
}
