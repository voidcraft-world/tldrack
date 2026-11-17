import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue';
import { useWindowBus } from '../composables/useWindowBus.ts';
import { COMPANION_BUS } from './companion-bus';
import { decide_lifecycle_action } from './lifecycle-logic';

const UNLOAD_GRACE_MS = 2500; // on a main 'unload', wait this long — cancelled if it returns (reload)
const WATCHDOG_INTERVAL_MS = 3000;

/**
 * Self-close behaviour of a satellite window: the rack page under a host's main
 * window (`feed: 'bus'` in `agent-rack/AgentRackWindow.vue`). The main window
 * heartbeats (MAIN_ALIVE) while alive. The satellite closes when the main signs
 * out (MAIN_SHUTDOWN reason 'logout'), closes (reason 'unload', after a short
 * grace), or is PROVEN silent. It survives a plain reload of the main, which
 * announces itself again and cancels the grace.
 *
 * "Proven": it probes (`MAIN_PING`, event-dispatched and so immune to timer
 * throttling) before believing the silence, and refuses to close a window the
 * operator can still see. Why both halves are needed: owned by
 * `lifecycle-logic.ts` — see the note there.
 *
 * Call once from a window-root component's setup. The returned `main_lost` is
 * true while the main is presumed gone but this window was kept open anyway. A
 * surface that can say so should render it (the agent rack shows MAIN LOST):
 * a satellite that silently stops updating is no better than one that vanishes.
 */
export function useCompanionLifecycle(): { main_lost: Ref<boolean> } {
    const bus = useWindowBus();

    const main_lost = ref(false);
    let last_main_seen = Date.now();
    let unanswered_probes = 0;
    let unload_grace: ReturnType<typeof setTimeout> | null = null;
    let watchdog: ReturnType<typeof setInterval> | null = null;
    let unsub: Array<() => void> = [];

    const close_self = (): void => {
        window.close();
    };

    const cancel_unload_grace = (): void => {
        if (unload_grace) {
            clearTimeout(unload_grace);
            unload_grace = null;
        }
    };

    const mark_main_alive = (): void => {
        last_main_seen = Date.now();
        unanswered_probes = 0;
        main_lost.value = false;
        cancel_unload_grace(); // the main is clearly still here (e.g. it just reloaded)
    };

    /**
     * Is the user looking at this window? Visible OR focused — see
     * `LifecycleInput.in_use` for why neither test alone is enough.
     */
    const window_in_use = (): boolean => !document.hidden || document.hasFocus();

    onMounted(() => {
        unsub.push(bus.on(COMPANION_BUS.MAIN_ALIVE, mark_main_alive));
        unsub.push(bus.on(COMPANION_BUS.MAIN_READY, mark_main_alive));

        unsub.push(
            bus.on(COMPANION_BUS.MAIN_SHUTDOWN, (payload) => {
                if (payload?.reason === 'logout') {
                    close_self(); // sign-out is definitive
                } else if (!unload_grace) {
                    // Main is unloading — might be a plain reload. Wait; mark_main_alive cancels.
                    unload_grace = setTimeout(close_self, UNLOAD_GRACE_MS);
                }
            })
        );

        // Fallback for an ungraceful main exit (crash / force-quit) where neither
        // a shutdown nor further heartbeats arrive. Every non-idle tick asks
        // before it acts, and keeps asking afterwards so a main that comes back
        // — a discarded tab reloads the instant it is clicked — reconnects us.
        watchdog = setInterval(() => {
            const action = decide_lifecycle_action({
                now: Date.now(),
                last_main_seen,
                unanswered_probes,
                in_use: window_in_use(),
            });
            if (action === 'idle') {
                return;
            }
            if (action === 'close') {
                close_self();
                return;
            }
            unanswered_probes += 1;
            bus.post(COMPANION_BUS.MAIN_PING);
            if (action === 'orphaned') {
                main_lost.value = true;
            }
        }, WATCHDOG_INTERVAL_MS);
    });

    onBeforeUnmount(() => {
        cancel_unload_grace();
        if (watchdog) {
            clearInterval(watchdog);
            watchdog = null;
        }
        unsub.forEach((fn) => fn());
        unsub = [];
    });

    return { main_lost };
}
