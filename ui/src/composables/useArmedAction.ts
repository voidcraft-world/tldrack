/**
 * The two-step arm, for a Vue surface.
 *
 * A thin wrapper over `engine/terminal/arm-logic.ts::create_arm_latch` — the
 * latch owns every rule (one arm at a time, auto-disarm, `retain` for "the row
 * retired") and the reasons; this file only supplies the browser's timers, a
 * `Ref` the template can read, and the unmount disarm. No `window.confirm`
 * anywhere near this: the second press IS the confirm.
 *
 * `timeout_ms` may be a getter, so a live flag is read on every arm and not
 * once at setup.
 */
import { onBeforeUnmount, ref, type Ref } from 'vue';
import { create_arm_latch } from '../engine/terminal/arm-logic.ts';

export interface ArmedAction {
    /** The armed key, or null — bind labels and classes to this. */
    armed: Ref<string | null>;
    /** First press arms `key`; the second within the window runs `fire`. */
    press(key: string, fire: () => void): 'armed' | 'fired';
    disarm(): void;
    /** Drop the arm unless its key is among `keys` — call with what is about
     *  to be rendered, on every data update, collapse and selection change. */
    retain(keys: Iterable<string>): void;
}

export function useArmedAction(timeout_ms: number | (() => number)): ArmedAction {
    const armed = ref<string | null>(null);
    const latch = create_arm_latch({
        get timeout_ms() {
            return typeof timeout_ms === 'function' ? timeout_ms() : timeout_ms;
        },
        set_timeout: (fn, ms) => window.setTimeout(fn, ms),
        clear_timeout: (handle) => window.clearTimeout(handle as number),
        on_change: (key) => {
            armed.value = key;
        },
    });

    // An armed state must not outlive its button — and unmount hides every
    // button at once. The latch header owns the war stories.
    onBeforeUnmount(() => latch.disarm());

    return {
        armed,
        press(key, fire) {
            const result = latch.press(key);
            if (result === 'fired') fire();
            return result;
        },
        disarm: () => latch.disarm(),
        retain: (keys) => latch.retain(keys),
    };
}
