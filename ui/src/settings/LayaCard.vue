<template>
    <section class="fui-panel st-card" aria-labelledby="st-laya-title">
        <div class="st-card__head">
            <h2 id="st-laya-title" class="fui-techtext st-card__title">LAYA</h2>
            <span v-if="view" class="fui-tag" :class="{ 'fui-tag--ok': view.tone === 'ok', 'fui-tag--warn': view.tone === 'warn' }">{{ view.tag }}</span>
            <span class="lr-grow"></span>
            <template v-if="view">
                <button
                    v-if="view.start"
                    type="button"
                    class="board-btn"
                    :disabled="busy !== null"
                    title="Load the model now, so the next verdict does not wait for it"
                    @click="run('start')"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ busy === 'start' ? 'STARTING…' : 'START' }}
                </button>
                <button
                    v-if="view.install"
                    type="button"
                    class="board-btn"
                    :class="{ 'is-on': arm.armed.value === INSTALL }"
                    :disabled="busy !== null"
                    :title="`Downloads ${LAYA_DOWNLOAD}: the runtime, then the base model. Press twice to start it.`"
                    @click="arm.press(INSTALL, () => void run('install'))"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ install_label }}
                </button>
            </template>
        </div>
        <p class="st-card__about">
            The local review model, a plugin of the bridge. It reads a finished turn and marks the row ✅ done or ⚠️ needs you. Nothing it reads leaves this machine.
        </p>

        <p v-if="!card" class="lr-loading">Asking the plugin…</p>
        <p v-else-if="card.kind === 'off'" class="fui-callout">
            The laya plugin is off on this bridge. Switch it on under <a href="#features">FEATURES</a>; it mounts at the bridge’s next start.
        </p>
        <p v-else-if="card.kind === 'failed' && card.failure.kind !== 'unreachable'" class="fui-callout fui-callout--warn" role="alert">
            {{ card.failure.error }}
        </p>
        <template v-else-if="view">
            <p class="st-card__line" role="status">{{ view.line }}</p>
            <pre v-if="view.error" class="st-fix st-fix--log">{{ view.error }}</pre>
        </template>
        <p v-if="note" class="lr-status" :class="{ 'lr-status--bad': !note.ok }" role="status">{{ note.text }}</p>
    </section>
</template>

<script setup lang="ts">
/**
 * SETUP's Laya card: the state of the local review model, INSTALL and START. The routes and
 * what each state means: owned by `laya-install-client.ts` — see the note there.
 *
 * THE PAGE'S ONE POLL. While the state is `installing` or `starting` the card asks again every
 * two seconds, because those two states end by themselves and the operator is waiting on them.
 * In every other state, and whenever the browser tab is hidden, no timer is set: the next read is
 * the page's own (`page-bridge.ts` `use_reload`), which also picks the poll back up on return.
 *
 * INSTALL is armed: the first press says what it costs, the second starts the download.
 */
import { computed, onBeforeUnmount, ref } from 'vue';

import { useArmedAction } from '../composables/useArmedAction.ts';
import {
    LAYA_DOWNLOAD,
    LAYA_POLL_MS,
    fetch_laya_status,
    laya_card_of,
    laya_card_view,
    laya_lifecycle,
    laya_should_poll,
    type LayaCard,
    type LayaLifecycle,
} from './laya-install-client.ts';
import { use_reload } from './page-bridge.ts';

const INSTALL = 'install';
const ARM_MS = 4_000;

const card = ref<LayaCard | null>(null);
const busy = ref<LayaLifecycle | null>(null);
const note = ref<{ ok: boolean; text: string } | null>(null);
const arm = useArmedAction(ARM_MS);

const view = computed(() => (card.value?.kind === 'status' ? laya_card_view(card.value.status) : null));
const install_label = computed(() => {
    if (busy.value === 'install') return 'INSTALLING…';
    return arm.armed.value === INSTALL ? `INSTALL? ${LAYA_DOWNLOAD.toUpperCase()}` : 'INSTALL';
});

let timer: number | null = null;
let generation = 0;

function stop_poll(): void {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
}

/** One more read in two seconds, when the state on screen is one that ends by itself. */
function schedule(): void {
    stop_poll();
    const current = card.value;
    if (current?.kind !== 'status' || !laya_should_poll(current.status.state)) return;
    if (document.visibilityState !== 'visible') return;
    timer = window.setTimeout(() => void load(), LAYA_POLL_MS);
}

async function load(): Promise<void> {
    const mine = ++generation;
    const result = bridge.note(await fetch_laya_status());
    if (mine !== generation) return;
    card.value = laya_card_of(result);
    schedule();
}

const bridge = use_reload(() => void load());

async function run(action: LayaLifecycle): Promise<void> {
    busy.value = action;
    note.value = null;
    generation += 1; // a status read already out describes the state before this press
    // Not handed to `bridge.note`: a slow START is not a down bridge. The status read below is.
    const result = await laya_lifecycle(action);
    busy.value = null;
    if (result.ok) {
        card.value = { kind: 'status', status: result.data };
        schedule();
    } else {
        note.value = { ok: false, text: result.error };
        await load();
    }
}

onBeforeUnmount(stop_poll);
</script>
