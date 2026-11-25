/**
 * What the whole settings page knows about its bridge: is it answering, and when to ask again.
 *
 * ONE OWNER of "the bridge is down". Every tab reads through its own client, and hands each
 * result to `note`. An unanswered call puts the page's banner up (once, above whichever tab is
 * open); any answer takes it down, a refusal included, because a bridge that says no is up.
 * RETRY bumps `asks`, and every mounted tab reloads on it.
 *
 * NO TIMER LIVES HERE. A tab reads when it mounts, when the browser tab comes back to the front,
 * and on RETRY (`use_reload`). The page has two polls, and each belongs to a card on SETUP: the
 * Laya card's, only while an install or a start is under way (owned by `LayaCard.vue` — see the
 * note there), and the update card's (owned by `update/update-poller.ts` — see the note there).
 */
import { inject, onBeforeUnmount, onMounted, provide, ref, watch, type InjectionKey, type Ref } from 'vue';

import { down_after, type BridgeFailure, type BridgeResult } from './bridge-call.ts';

export interface PageBridge {
    /** The failure that says the bridge is not answering, or null. */
    down: Ref<BridgeFailure | null>;
    /** Counts RETRY presses: a tab watches it. */
    asks: Ref<number>;
    /** Record one result, and hand it back so a call site reads `note(await fetch_…())`. */
    note<T>(result: BridgeResult<T>): BridgeResult<T>;
    retry(): void;
}

const PAGE_BRIDGE: InjectionKey<PageBridge> = Symbol('settings-page-bridge');

export function provide_page_bridge(): PageBridge {
    const down = ref<BridgeFailure | null>(null);
    const asks = ref(0);
    const bridge: PageBridge = {
        down,
        asks,
        note(result) {
            down.value = down_after(result);
            return result;
        },
        retry() {
            asks.value += 1;
        },
    };
    provide(PAGE_BRIDGE, bridge);
    return bridge;
}

export function use_page_bridge(): PageBridge {
    const bridge = inject(PAGE_BRIDGE);
    if (!bridge) throw new Error('use_page_bridge: no settings page above this component');
    return bridge;
}

/**
 * Run `reload` when the component mounts, on RETRY, and when the browser tab becomes visible
 * again (the operator may have run a fix in a terminal meanwhile). Returns the bridge.
 */
export function use_reload(reload: () => void): PageBridge {
    const bridge = use_page_bridge();
    const on_visible = (): void => {
        if (document.visibilityState === 'visible') reload();
    };
    watch(bridge.asks, reload);
    onMounted(() => {
        document.addEventListener('visibilitychange', on_visible);
        reload();
    });
    onBeforeUnmount(() => document.removeEventListener('visibilitychange', on_visible));
    return bridge;
}
