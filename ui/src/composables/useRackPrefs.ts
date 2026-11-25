/**
 * This browser's rack switches (`config/rack-prefs.ts`) as ONE reactive copy for every component
 * on the page.
 *
 * Shared on purpose. The canvas pane and the office inside it both follow the switches; a copy
 * each would be a storage read and a `storage` listener each, and two copies can disagree for
 * the length of one event. So the first user reads storage and starts the watcher, every later
 * one gets the same ref, and the last one to unmount stops the watcher. A host that mounts and
 * unmounts the rack inside one long-lived page therefore leaves no listener behind.
 *
 * The ref follows a change made in ANOTHER tab (the settings page): that is the only writer the
 * rack has. A page that writes the switches itself sets its own state as it saves.
 */
import { onBeforeUnmount, shallowRef, type ShallowRef } from 'vue';
import { DEFAULT_RACK_PREFS, load_rack_prefs, watch_rack_prefs, type RackPrefs } from '../config/rack-prefs.ts';

const prefs = shallowRef<RackPrefs>({ ...DEFAULT_RACK_PREFS });
let users = 0;
let stop_watch: (() => void) | null = null;

export function useRackPrefs(): Readonly<ShallowRef<RackPrefs>> {
    if (users === 0) {
        prefs.value = load_rack_prefs();
        stop_watch = watch_rack_prefs((next) => {
            prefs.value = next;
        });
    }
    users += 1;
    onBeforeUnmount(() => {
        users -= 1;
        if (users > 0) return;
        stop_watch?.();
        stop_watch = null;
    });
    return prefs;
}
