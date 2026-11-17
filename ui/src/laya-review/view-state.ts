/**
 * The state every REVIEW page view keeps while it talks to the bridge — its down state, the first load, the one
 * write in flight, the status line — and the gate watcher that reloads it. The deck and its RECENT list each call
 * this once; why the page opens the gate itself: owned by `LayaReviewDeck.vue` `onMounted` — see the note there.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';

import { on_local_access_change } from '../engine/terminal/local-network-gate.ts';
import type { DeckDown } from './review-deck-model.ts';

export function use_laya_view(reload: () => void) {
    const down = ref<DeckDown | null>(null);
    const loading = ref(true);
    const in_flight = ref(false);
    const status_line = ref<{ ok: boolean; text: string } | null>(null);

    let off_access: (() => void) | null = null;
    onMounted(() => {
        off_access = on_local_access_change((state) => {
            // Any gate change redraws a gate down state (a shut gate fails without touching the network). Once it
            // opens — CONNECT, or Chrome's prompt answered — a call that hung behind the prompt and timed out as
            // BRIDGE DOWN goes again on its own too.
            if (down.value?.kind === 'gate' || (state === 'open' && down.value?.kind === 'down')) reload();
        });
    });
    onBeforeUnmount(() => off_access?.());

    return { down, loading, in_flight, status_line };
}
