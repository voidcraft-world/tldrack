<template>
    <button
        type="button"
        class="fui-disclose"
        :aria-expanded="open"
        @click.stop="open = !open"
        @mousedown.stop
        @mouseup.stop
    >{{ open ? '▾' : '▸' }} <slot /></button>
</template>

<script setup lang="ts">
/**
 * The closed-by-default disclosure the review page reveals its secondary text with:
 * the `▸` / `▾` button of "▸ Conversation" and "▸ Wave".
 *
 * A sentence that restates a mark goes behind a disclosure, closed by default, so
 * a card leads with its readouts.
 *
 * It owns the button and nothing else. The caller decides what `open` reveals
 * (`v-if` on the block below it) and sizes the label through
 * `--fui-disclose-fs`, so a surface with its own type ladder can put the button
 * on the right rung without a second class. The `@mousedown.stop @mouseup.stop`
 * pair keeps a press from reaching a pointer handler on whatever the button sits
 * in: a card that toggles on a press, or a host that embeds the page.
 */
const open = defineModel<boolean>({ default: false });
</script>

<style scoped>
.fui-disclose {
    padding: 1px 8px;
    border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
    border-radius: 2px;
    background: transparent;
    color: inherit;
    font-family: var(--fui-mono);
    font-size: var(--fui-disclose-fs, 0.75rem);
    letter-spacing: 0.06em;
    text-transform: none;
    opacity: 0.6;
    cursor: pointer;
}

.fui-disclose:hover {
    opacity: 0.9;
}
</style>
