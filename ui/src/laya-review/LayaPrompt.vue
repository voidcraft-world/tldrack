<template>
    <div class="lp" :class="{ 'lp--other': other }" :title="press_title" @click.stop="toggle">
        <!-- The head is the control (a real button: Enter and Space, and no link or fold nested inside a button); a press
             anywhere else on the block toggles it too, for the mouse. -->
        <button type="button" class="lp-head" :aria-expanded="expanded" :aria-controls="body_id" @click.stop="expanded = !expanded">
            {{ head }}<span v-if="age" class="lp-age"> · {{ age }}</span>
        </button>
        <AgentMessage :id="body_id" :text="text" :mode="expanded ? 'full' : 'head'" :head_lines="lines" :clipped="clipped" />
        <p v-if="expanded && clipped" class="lp-cut">The first {{ count(text.length) }} of {{ count(chars ?? 0) }} characters</p>
    </div>
</template>

<script setup lang="ts">
/**
 * A prompt in RECENT's conversation: the operator's first message, or the prompt a group of turns answered (`thread-model.ts`
 * `turn_groups`), clipped to `lines` until pressed. One block for both, so a prompt reads the same wherever it draws.
 * A press on a link, a paste's fold or selected text is that thing's, not a toggle.
 *
 * `chars`: the whole message's length when the bridge sent only its head (`HistoryClip.chars`), so the block says it
 * shows the first part, and a paste the clip cut reads as at least its size (`AgentMessage.vue` `clipped`).
 */
import { computed, ref, useId, watch } from 'vue';

import AgentMessage from './AgentMessage.vue';

const props = withDefaults(defineProps<{ head: string; text: string; age?: string | null; other?: boolean; lines?: number; chars?: number | null }>(), {
    age: null,
    other: false,
    lines: 3,
    chars: null,
});
const expanded = ref(false);
watch(() => props.text, () => (expanded.value = false));
const body_id = useId();
const clipped = computed(() => props.chars !== null && props.chars > props.text.length);
const COUNT = new Intl.NumberFormat('en-US');
const count = (n: number) => COUNT.format(n);
const press_title = computed(() => {
    if (expanded.value) return 'Show less';
    return clipped.value ? `Show the first ${count(props.text.length)} of ${count(props.chars ?? 0)} characters` : 'Show the whole message';
});

function toggle(event: MouseEvent): void {
    // A link, a paste's fold: theirs. A code block is not, so a press on one at the top of a clipped prompt opens it.
    const target = event.target as HTMLElement | null;
    if (target?.closest('a, summary, details')) return;
    if ((window.getSelection?.()?.toString() ?? '') !== '') return;
    expanded.value = !expanded.value;
}
</script>

<style scoped>
/* Tokens: owned by laya-review.css — see the note there. */
.lp {
    min-width: 0;
    padding: 8px 12px;
    border-radius: 3px;
    background: var(--lr-tint);
    font-size: 0.92rem;
    cursor: pointer;
    scroll-margin-top: calc(var(--lr-sticky-top, 0px) + 8px);
}
/* A prompt the rack or a lead typed: the same block, without the operator's tint. */
.lp--other {
    border: 1px dashed var(--lr-well-line);
    background: none;
}
.lp-head {
    display: block;
    width: 100%;
    margin: 0 0 4px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--lr-ink-3);
    font: inherit;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    line-height: 1.6;
    text-align: left;
    cursor: pointer;
}
.lp-head:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 2px;
}
.lp-age {
    font-family: var(--fui-mono);
    font-weight: 400;
    letter-spacing: 0;
}
.lp-cut {
    margin: 4px 0 0;
    color: var(--lr-ink-3);
    font-size: 0.78rem;
}
</style>
