/**
 * The bridge's update status as ONE reactive copy for every component on the page: the rack's ⚙
 * and its tray line, or the settings page's card.
 *
 * Shared for the reason `composables/useRackPrefs.ts` is: two copies would be two polls of one
 * route, and could disagree for the length of one read. The first user starts the poller, every
 * later one gets the same ref, and the last one to unmount stops it (`update-poller.ts` owns
 * when it reads). The poller itself is made on first use, never at import: by then the page has
 * named its bridge (`config/bridge-origin.ts`).
 */
import { onBeforeUnmount, shallowRef, type ShallowRef } from 'vue';

import { watch_page_return } from '../agent-rack/page-return.ts';
import { bridge_origin } from '../config/bridge-origin.ts';
import { post_update, read_update, type UpdateAction } from './update-client.ts';
import { create_update_poller, type UpdatePoller } from './update-poller.ts';
import { initial_update_state, type UpdateState } from './update-status.ts';

export interface UpdateStatus {
    state: Readonly<ShallowRef<UpdateState>>;
    /** Read now: for a caller that just saw the bridge come back. */
    refresh: () => void;
    /** CHECK NOW or UPDATE. */
    press: (action: UpdateAction) => Promise<void>;
}

/** `update-status.ts` owns the rule this answers: did the bridge this page talks to serve the page. */
function served_by_bridge(): boolean {
    return typeof window !== 'undefined' && window.location.origin === bridge_origin();
}

const state = shallowRef<UpdateState>(initial_update_state(false));
let poller: UpdatePoller | null = null;

function page_poller(): UpdatePoller {
    poller ??= create_update_poller({
        read: read_update,
        post: post_update,
        now: () => Date.now(),
        is_hidden: () => document.hidden,
        set_timer: (run, ms) => window.setTimeout(run, ms),
        clear_timer: (handle) => window.clearTimeout(handle as number),
        watch_return: (on_return) => watch_page_return(on_return),
        tracks_page: served_by_bridge(),
    });
    return poller;
}

export function useUpdateStatus(): UpdateStatus {
    const mine = page_poller();
    state.value = mine.state();
    const unsubscribe = mine.subscribe((next) => {
        state.value = next;
    });
    onBeforeUnmount(unsubscribe);
    return { state, refresh: mine.refresh, press: mine.press };
}
