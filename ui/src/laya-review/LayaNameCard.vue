<template>
    <section v-if="view?.named || error" class="fui-panel nc" aria-label="Session name">
        <span class="fui-corner tl"></span><span class="fui-corner br"></span>
        <template v-if="view?.named">
            <span class="fui-tag" :title="`The name ${view.named.model ?? 'the namer'} gave this session from its first prompt`">NAME</span>
            <strong class="nc-name">{{ view.named.name }}</strong>
            <span v-if="now_shown" class="nc-now" title="What the rack shows for this session now">→ {{ now_shown }}</span>
            <span v-if="verdict" class="nc-verdict" :class="`nc-verdict--${verdict.tone}`" :title="verdict.title">{{ verdict.text }}</span>
            <span class="lr-grow"></span>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': view.label?.verdict === 'fits' }"
                :aria-pressed="view.label?.verdict === 'fits'"
                :disabled="busy"
                title="The name says what this session is about"
                @click="send('fits')"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>✓ FITS
            </button>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': editing || view.label?.verdict === 'wrong' }"
                :aria-pressed="view.label?.verdict === 'wrong'"
                :aria-expanded="editing"
                :disabled="busy"
                title="The name misses: say what it should have been, and the rack takes it"
                @click="open_better"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>✗ WRONG
            </button>
            <form v-if="editing" class="nc-better" @submit.prevent="send('wrong')">
                <input
                    ref="better_el"
                    v-model="better"
                    class="nc-input"
                    :maxlength="TOPIC_BETTER_MAX"
                    spellcheck="false"
                    autocomplete="off"
                    placeholder="what it should be called (optional)"
                    aria-label="The name this session should have had"
                    @keydown.esc.prevent="editing = false"
                />
                <button type="submit" class="board-btn" :disabled="busy">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>SAVE
                </button>
            </form>
            <!-- Interpolated, never v-html: the operator's own prompt, from disk. -->
            <p v-if="view.named.request" class="nc-request" :title="request_title">{{ view.named.request }}</p>
            <span v-if="failure" class="nc-error" role="status">{{ failure }}</span>
        </template>
        <span v-else class="nc-error" role="status">{{ error }}</span>
    </section>
</template>

<script setup lang="ts">
/**
 * The REVIEW page's NAME card: did the rack's auto name (Haiku's topic slug) fit this session? ✓ FITS, or ✗ WRONG with
 * the name it should have been, which the rack takes at once. A ✎ rename in the rack already counts as a verdict, so a
 * renamed session arrives here answered. The rows: `tldrack/topic_labels.py`; the wire: `topic-labels-client.ts`.
 * Drawn only for a session that was auto-named: a name the operator chose has nothing to grade.
 */
import { computed, nextTick, ref } from 'vue';
import { TOPIC_BETTER_MAX, type TopicLabelsView, type TopicVerdict } from '@tldrack/schemas/topic-labels.ts';

import { post_topic_label } from './topic-labels-client.ts';

const props = defineProps<{ view: TopicLabelsView | null; error: string | null; since: number | null }>();
const emit = defineEmits<{ labelled: [view: TopicLabelsView] }>();

const busy = ref(false);
const editing = ref(false);
const better = ref('');
const failure = ref<string | null>(null);
const better_el = ref<HTMLInputElement | null>(null);

/** What the rack shows now, when it is no longer the auto name: a better name taken, or a rename. */
const now_shown = computed(() => {
    const view = props.view;
    if (!view?.named) return null;
    const now = view.shown || view.session;
    return now === view.named.name || now === view.named.session ? null : now;
});

const verdict = computed(() => {
    const label = props.view?.label;
    if (!label) return null;
    const how = label.via === 'rename' ? 'your ✎ rename' : 'you';
    return label.verdict === 'fits'
        ? { tone: 'good', text: '✓ fits', title: `Marked as fitting by ${how}` }
        : { tone: 'bad', text: label.better ? `✗ → ${label.better}` : '✗ wrong', title: `Marked wrong by ${how}` };
});

const request_title = computed(() => {
    const named = props.view?.named;
    if (!named) return '';
    const cut = named.request_chars > (named.request?.length ?? 0) ? ` (the first ${named.request?.length} of ${named.request_chars} characters)` : '';
    return `The first prompt it was named from${cut}`;
});

async function open_better(): Promise<void> {
    editing.value = !editing.value;
    failure.value = null;
    if (!editing.value) return;
    better.value = '';
    await nextTick();
    // The press asked to type, so the field takes focus. A field never focuses itself only because a panel opened.
    better_el.value?.focus();
}

async function send(verdict_now: TopicVerdict): Promise<void> {
    const view = props.view;
    if (!view?.named || busy.value) return;
    busy.value = true;
    failure.value = null;
    const name = better.value.trim();
    const got = await post_topic_label({
        session: view.session,
        verdict: verdict_now,
        ...(verdict_now === 'wrong' && name ? { better: name } : {}),
        ...(props.since !== null ? { since: props.since } : {}),
    });
    busy.value = false;
    if (!got.ok) {
        failure.value = got.error;
        return;
    }
    editing.value = false;
    emit('labelled', got.data);
}
</script>

<style scoped>
/* A button here is sized and tinted through its `--lr-btn-*` tokens: owned by laya-review.css — see the note there. */
.nc {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    padding: 12px 14px;
}
.nc-name {
    font-family: var(--fui-mono);
    font-size: 1.15rem;
    color: var(--lr-ink);
}
.nc-now {
    font-family: var(--fui-mono);
    color: var(--lr-ink-2);
}
.nc-verdict {
    font-family: var(--fui-mono);
    font-size: 0.9rem;
}
.nc-verdict--good {
    color: var(--lr-good);
}
.nc-verdict--bad {
    color: var(--lr-bad);
}
.nc-better {
    display: flex;
    gap: 8px;
    flex-basis: 100%;
}
.nc-input {
    flex: 1;
    min-width: 0;
    font-family: var(--fui-mono);
    font-size: 0.95rem;
    padding: 6px 10px;
    color: var(--lr-ink);
    background: transparent;
    border: 1px solid var(--lr-ink-2);
}
.nc-request {
    flex-basis: 100%;
    margin: 0;
    color: var(--lr-ink-2);
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
}
.nc-error {
    flex-basis: 100%;
    color: var(--lr-bad);
    font-size: 0.85rem;
}
</style>
