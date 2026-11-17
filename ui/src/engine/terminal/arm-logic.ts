/**
 * The two-step arm, as a pure latch.
 *
 * Every destructive button in this app confirms the same way: the first press
 * turns it amber (`CONFIRM?`), the second fires, and a few seconds of silence
 * disarm it, never `window.confirm`. This is the one copy, written so the rack's ✕
 * and PR and every later verb do not hand-roll their own.
 *
 * ── The rule that keeps biting ─────────────────────────────────────────────
 * An armed state must not outlive its button. Collapsing a section hides a
 * button without clearing its arm, so re-opening inside the window shows
 * `CONFIRM?` and the next single click fires. A rack update that retires a
 * row does the same to the rack's ✕.
 * `retain(keys)` is that rule made structural — the panel calls it with the
 * keys it is about to render and the latch drops an arm whose key is gone.
 *
 * ONE arm at a time. Pressing a different key moves the arm rather than
 * stacking a second, because two amber buttons is two ways to fire by mistake.
 *
 * Timers are injected, so this runs under bare `node --test` with a fake clock
 * and the Vue wrapper (`composables/useArmedAction.ts`) supplies the real ones.
 */

export interface ArmLatchDeps {
    timeout_ms: number;
    set_timeout: (fn: () => void, ms: number) => unknown;
    clear_timeout: (handle: unknown) => void;
    /** Fired on every transition, with the key now armed (null = disarmed). */
    on_change?: (armed: string | null) => void;
}

export interface ArmLatch {
    /** The armed key, or null. */
    armed(): string | null;
    /** Press `key`: `'armed'` on the first press, `'fired'` on the second
     *  within the window — and fired means DISARMED, the caller acts. */
    press(key: string): 'armed' | 'fired';
    disarm(): void;
    /** Drop the arm unless its key is among `keys` — call with what is about
     *  to be rendered, on every data update, collapse and selection change. */
    retain(keys: Iterable<string>): void;
}

export function create_arm_latch(deps: ArmLatchDeps): ArmLatch {
    let armed: string | null = null;
    let timer: unknown = null;

    function stop_timer(): void {
        if (timer === null) return;
        deps.clear_timeout(timer);
        timer = null;
    }

    function set(next: string | null): void {
        // Disarming a latch that is not armed is a no-op, and silent: the panel
        // calls `retain` on every render and must not be told about nothing.
        if (next === null && armed === null) return;
        stop_timer();
        armed = next;
        if (next !== null) {
            timer = deps.set_timeout(() => {
                timer = null;
                set(null);
            }, deps.timeout_ms);
        }
        deps.on_change?.(armed);
    }

    return {
        armed: () => armed,
        press(key) {
            if (armed === key) {
                set(null);
                return 'fired';
            }
            set(key);
            return 'armed';
        },
        disarm: () => set(null),
        retain(keys) {
            if (armed === null) return;
            for (const key of keys) if (key === armed) return;
            set(null);
        },
    };
}
