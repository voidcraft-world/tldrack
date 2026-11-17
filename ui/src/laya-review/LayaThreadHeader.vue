<template>
    <header class="lth" :class="[`lr-hue-${head.hue}`, { 'lth--compact': compact, 'lth--unlinked': head.unlinked }]">
        <span class="lth-badge" aria-hidden="true">{{ head.monogram }}</span>
        <div class="lth-line">
            <strong class="lth-title" :title="head.title">{{ head.title }}</strong>
            <slot name="tags" />
            <!-- The tags and the mono line: one flow with the title here; a caller with one line to spare (RECENT's pager
                 bar at phone width) lays them out as a row of their own. -->
            <span class="lth-sub">
                <span v-for="t in head.tags" :key="t.id" class="fui-tag lth-tag" :class="{ 'lth-tag--new': t.id === 'new' }" :title="t.title">{{ t.text }}</span>
                <span v-if="head.subject || head.meta" class="lth-meta" :title="head.meta_title || undefined">
                    <button
                        v-if="head.subject && narrowable"
                        type="button"
                        class="lth-rack"
                        :title="`Show only ${head.subject}’s turns`"
                        @click.stop="narrow"
                    >{{ head.subject }}</button>
                    <span v-else-if="head.subject" class="lth-rack lth-rack--still">{{ head.subject }}</span>
                    <span v-if="head.meta" class="lth-meta__rest">{{ head.subject ? ' · ' : '' }}{{ head.meta }}</span>
                </span>
            </span>
        </div>
        <div
            v-if="head.first_user && !compact"
            class="lth-first"
            role="button"
            tabindex="0"
            :aria-expanded="expanded"
            :title="expanded ? 'Show less' : 'Show your whole first message'"
            @click.stop="toggle"
            @keydown.enter.self.prevent="expanded = !expanded"
            @keydown.space.self.prevent="expanded = !expanded"
        >
            <span class="lth-who">You, first</span>
            <AgentMessage class="lth-first__text" :text="head.first_user" :mode="expanded ? 'full' : 'head'" :head_lines="2" :clipped="head.first_clipped" />
        </div>
    </header>
</template>

<script setup lang="ts">
/**
 * One conversation's head: its mark (a monogram badge in the thread's identity hue), its title, the rack name it ran
 * under, a mono readout, and the operator's first message, clamped to two lines until pressed. RECENT draws one in its
 * pager bar (`compact`, no first message: the conversation below prints it), the deck one per card. The view model,
 * and every word on it, is `thread-model.ts` (`band_head` / `turn_head`); this draws it.
 *
 * Colour is identity and nothing else: the hue reaches only the badge here, and the band's left rule in the caller.
 * Pressing the rack name narrows RECENT to that session (`LayaReviewDeck.vue` `set_session` keeps it in the address bar).
 */
import { ref, watch } from 'vue';

import AgentMessage from './AgentMessage.vue';
import type { ThreadHead } from './thread-model.ts';

const props = withDefaults(defineProps<{ head: ThreadHead; compact?: boolean; narrowable?: boolean }>(), { compact: false, narrowable: false });
const emit = defineEmits<{ session: [name: string] }>();
const expanded = ref(false);
watch(() => props.head.first_user, () => (expanded.value = false));

function narrow(): void {
    if (props.head.subject) emit('session', props.head.subject);
}

/** Keys and presses on a link or a paste's fold inside the first message are theirs (`.self` above, this guard here). */
function toggle(event: MouseEvent): void {
    if ((event.target as HTMLElement | null)?.closest('a, summary, details')) return;
    expanded.value = !expanded.value;
}
</script>

<style scoped>
/* Tokens (`--lr-*`, the identity hue `--lr-thread`): owned by laya-review.css — see the note there. */
.lth {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 4px 12px;
    padding: 12px 14px 10px;
    min-width: 0;
}
.lth-badge {
    grid-row: span 2;
    align-self: start;
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: 1.5px solid var(--lr-thread, var(--lr-rule));
    background: color-mix(in srgb, var(--lr-thread, var(--lr-rule)) 12%, transparent);
    color: var(--lr-thread, var(--lr-ink-2));
    font: 700 0.78rem/1 var(--fui-mono);
}
.lth--unlinked .lth-badge {
    border-style: dashed;
}
.lth-line {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 4px 12px;
    min-width: 0;
}
.lth-title {
    color: var(--lr-ink);
    font-size: 1.02rem;
    line-height: 1.35;
    overflow-wrap: anywhere;
}
/* A tag wraps inside the line rather than push the bar past the screen (the kit's `.fui-tag` is nowrap). */
.lth-tag {
    min-width: 0;
    white-space: normal;
    overflow-wrap: anywhere;
}
.lth-tag--new {
    border-color: var(--fui-accent);
    background: var(--lr-on-fill);
    color: var(--lr-ink);
}
.lth-sub {
    display: contents;
}
.lth-meta {
    color: var(--lr-ink-3);
    font-family: var(--fui-mono);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
}
.lth-rack {
    display: inline-flex;
    align-items: center;
    min-height: 28px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--lr-ink-2);
    font: inherit;
    text-decoration: underline dotted;
    text-underline-offset: 3px;
    cursor: pointer;
}
.lth-rack:hover {
    color: var(--lr-ink);
}
.lth-rack:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 2px;
}
.lth-rack--still {
    text-decoration: none;
    cursor: default;
}
.lth-first {
    grid-column: 2;
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    color: var(--lr-ink-2);
    font-size: 0.9rem;
    cursor: pointer;
}
.lth-first:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 2px;
}
.lth-who {
    flex: none;
    color: var(--lr-ink-3);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
}
/* Two lines until pressed: the clip and its fade are AgentMessage's head mode (owned by AgentMessage.vue — see the note there). */
.lth-first__text {
    flex: 1;
    min-width: 0;
}

.lth--compact {
    padding-block: 8px;
}
.lth--compact .lth-badge {
    grid-row: span 1;
    align-self: center;
    width: 26px;
    height: 26px;
    font-size: 0.68rem;
}
.lth--compact .lth-title {
    font-size: 0.92rem;
}

@container (max-width: 559px) {
    .lth {
        padding: 10px;
    }
    /* A touch target of 44px without growing the line: the box reaches 8px past it, above and below. */
    button.lth-rack {
        min-height: 44px;
        margin-block: -8px;
    }
    .lth-first {
        grid-column: 1 / -1;
    }
}
</style>
