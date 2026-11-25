<template>
    <section class="fui-panel st-card" aria-labelledby="st-update-title">
        <div class="st-card__head">
            <h2 id="st-update-title" class="fui-techtext st-card__title">UPDATE</h2>
            <span v-if="headline" class="fui-tag" :class="{ 'fui-tag--ok': headline.tone === 'ok', 'fui-tag--warn': headline.tone === 'warn' }">{{ headline.tag }}</span>
            <span class="lr-grow"></span>
            <template v-if="view?.supported">
                <button
                    v-if="newer && !flying"
                    type="button"
                    class="board-btn is-on"
                    title="The bridge now serves a newer build of this page. Reload to use it."
                    @click="reload_page"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>RELOAD
                </button>
                <button type="button" class="board-btn" :disabled="flying" title="Ask the checkout’s remote now, without waiting for the timed check" @click="press('check')">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ checking ? 'CHECKING…' : 'CHECK NOW' }}
                </button>
                <button type="button" class="board-btn" :disabled="flying || !view.available" :title="update_title" @click="press('apply')">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ flying && !checking ? 'UPDATING…' : 'UPDATE' }}
                </button>
            </template>
        </div>
        <p class="st-card__about">
            Where this checkout stands against its remote. UPDATE pulls the new commits, rebuilds the page and restarts the bridge. Sessions keep
            running: they live in tmux, not in the bridge.
        </p>

        <p v-if="state.unsupported" class="fui-callout" role="status">
            Updates are not available on this bridge.
            <template v-if="state.unsupported.status === 404">
                It predates self-update. Restart it once by hand: <code>{{ RESTART_COMMAND }}</code>
            </template>
            <template v-else>{{ state.unsupported.error }}</template>
        </p>
        <p v-else-if="!view && state.read_error" class="fui-callout fui-callout--warn" role="alert">{{ state.read_error }}</p>
        <p v-else-if="!view" class="lr-loading">Asking the bridge…</p>
        <p v-else-if="!view.supported" class="fui-callout" role="status">
            Updates are not available on this bridge: <BridgeSentence :text="view.reason ?? 'it does not run from a git checkout it can pull'" />
        </p>

        <template v-else>
            <p v-if="headline" class="st-card__line" role="status"><BridgeSentence :text="headline.line" /></p>
            <p v-if="newer && !flying" class="fui-callout" role="status">The bridge now serves a newer build of this page than the one on screen. RELOAD to use it.</p>
            <p v-if="view.error" class="fui-callout fui-callout--warn" role="alert">The last update failed: <BridgeSentence :text="view.error" /></p>
            <p v-if="view.check_error" class="fui-callout fui-callout--warn" role="status">The last check did not reach the remote: <BridgeSentence :text="view.check_error" /></p>
            <p v-if="view.waiting" class="fui-callout" role="status">An update is waiting: <BridgeSentence :text="view.waiting" /></p>
            <p v-if="held_note" class="fui-callout" role="status"><BridgeSentence :text="held_note" /></p>
            <p v-if="state.read_error" class="lr-status lr-status--bad" role="alert">{{ state.read_error }}</p>
            <p v-if="state.press_error" class="lr-status lr-status--bad" role="alert"><BridgeSentence :text="state.press_error" /></p>
            <p v-if="finished" class="lr-status" role="status">{{ finished }}</p>

            <section v-if="view.incoming.length" class="st-section" aria-labelledby="st-update-incoming">
                <h3 id="st-update-incoming" class="st-section__title">Incoming</h3>
                <ul class="st-commits">
                    <li v-for="commit in view.incoming" :key="commit.sha" class="st-commit">
                        <code>{{ commit.sha }}</code>
                        <span>{{ commit.subject }}</span>
                    </li>
                </ul>
                <p v-if="view.behind > view.incoming.length" class="st-row__text">and {{ view.behind - view.incoming.length }} more</p>
            </section>

            <section v-if="view.available && !flying && steps.length" class="st-section" aria-labelledby="st-update-plan">
                <h3 id="st-update-plan" class="st-section__title">UPDATE will</h3>
                <ol class="st-steps">
                    <li v-for="step in steps" :key="step">{{ step }}</li>
                </ol>
            </section>

            <ul class="st-rows st-rows--ruled">
                <li class="st-row">
                    <div class="st-row__body">
                        <span class="st-row__title">Auto-update</span>
                        <p id="st-update-auto-text" class="st-row__text">
                            When main moves: pull, rebuild the page and restart the bridge with no click. Sessions keep running.
                        </p>
                        <p v-if="!view.checks" class="st-row__note" role="status">
                            The timed check is switched off, and auto-update needs it. Switch on Update check under <a href="#features">FEATURES</a>.
                        </p>
                        <p v-if="auto_error" class="st-row__note" role="alert">{{ auto_error }}</p>
                    </div>
                    <SettingSwitch :on="auto_on" label="Auto-update" :disabled="auto_saving" describedby="st-update-auto-text" @set="(on) => void set_auto(on)" />
                </li>
            </ul>
        </template>
    </section>
</template>

<script setup lang="ts">
/**
 * SETUP's update card: where the checkout stands against its remote, CHECK NOW, UPDATE, and the
 * auto-update switch. The view and its routes: owned by `update/update-client.ts`; every word
 * here that is derived from it: `update/update-status.ts`; when the card reads: `update/update-poller.ts`
 * — see the notes there. The rack's ⚙ draws from the same three.
 *
 * NEVER RELOADS BY ITSELF. A run this card started may end with a newer build of this very page
 * being served. The card then offers RELOAD and waits: the operator may be mid-edit on another tab
 * of the page.
 *
 * THE CARD TELLS THE PAGE WHEN THE BRIDGE CHANGED UNDER IT. Its poll is the only thing on the page
 * that keeps asking, so it is what notices the bridge stop answering, come back, or come back as a
 * new process. Each of those asks the page to read again (`page-bridge.ts` `retry`), which is what
 * puts the banner up, takes it down, and redraws THIS MACHINE after an update. Silence during a
 * run is not one of them: that is the restart, and the card says so itself.
 *
 * The auto-update switch is a feature switch (`features-client.ts`): the same write FEATURES makes.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import BridgeSentence from './BridgeSentence.vue';
import SettingSwitch from './SettingSwitch.vue';
import { RESTART_COMMAND, set_features } from './features-client.ts';
import { use_page_bridge } from './page-bridge.ts';
import type { UpdateAction } from '../update/update-client.ts';
import { finished_words, in_flight, only_checking, page_is_newer, plan_steps, update_headline, without_code_marks } from '../update/update-status.ts';
import { useUpdateStatus } from '../update/useUpdateStatus.ts';

/** The switch's key on the bridge. Opaque, like every feature key: sent back as given. */
const AUTO_UPDATE_KEY = 'auto_update';
/** How often "checked 2 min ago" is recounted. A clock, not a request. */
const CLOCK_MS = 30_000;

const bridge = use_page_bridge();
const update = useUpdateStatus();
const state = update.state;

const now_ms = ref(Date.now());
let clock: number | null = null;
onMounted(() => {
    clock = window.setInterval(() => {
        if (!document.hidden) now_ms.value = Date.now();
    }, CLOCK_MS);
});
onBeforeUnmount(() => {
    if (clock !== null) window.clearInterval(clock);
});
// An answer is also a moment to recount from.
watch(state, () => {
    now_ms.value = Date.now();
});

const view = computed(() => state.value.view);
const flying = computed(() => in_flight(state.value));
const checking = computed(() => only_checking(state.value));
const headline = computed(() => update_headline(state.value, now_ms.value));
const newer = computed(() => page_is_newer(state.value));
const steps = computed(() => (view.value ? plan_steps(view.value) : []));
const finished = computed(() => finished_words(state.value));
/** Why a pull cannot run, wherever the headline is not already saying it. */
const held_note = computed(() => {
    const current = view.value;
    if (!current?.blocked || headline.value?.tag === 'HELD') return null;
    return `${current.behind > 0 ? 'A pull is held' : 'A pull would be held'}: ${current.blocked}`;
});

const update_title = computed(() => {
    const current = view.value;
    if (!current) return '';
    if (current.available) return `UPDATE will: ${plan_steps(current).join(', then ') || 'run'}`;
    if (current.behind > 0 && current.blocked) return `Held: ${without_code_marks(current.blocked)}`;
    return 'Nothing to update';
});

function press(action: UpdateAction): void {
    void update.press(action);
}

function reload_page(): void {
    window.location.reload();
}

// ── the page is told when the bridge changed under it (the header owns why) ──
const down = computed(() => state.value.no_view_since !== null && !flying.value);
const boot_id = computed(() => view.value?.boot_id ?? null);
watch(down, () => bridge.retry());
watch(boot_id, (now, before) => {
    if (now !== null && before !== null) bridge.retry();
});

// ── the auto-update switch ───────────────────────────────────────────────────
const auto_saving = ref(false);
const auto_error = ref<string | null>(null);
/** The bridge's answer to this card's last write, shown until the next view says the same. */
const auto_written = ref<boolean | null>(null);
const auto_on = computed(() => auto_written.value ?? view.value?.auto ?? false);
watch(view, () => {
    auto_written.value = null;
});

async function set_auto(on: boolean): Promise<void> {
    auto_saving.value = true;
    auto_error.value = null;
    const result = bridge.note(await set_features({ [AUTO_UPDATE_KEY]: on }));
    auto_saving.value = false;
    if (!result.ok) {
        auto_error.value = result.kind === 'unreachable' ? null : result.error;
        return;
    }
    auto_written.value = result.data.features.find((feature) => feature.key === AUTO_UPDATE_KEY)?.on ?? null;
    update.refresh();
}
</script>
