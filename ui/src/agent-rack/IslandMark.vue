<script setup lang="ts">
/**
 * The island beside a session working in a non-main git worktree — its edits
 * are THERE, not in the main checkout. Which sessions get one:
 * `rack-view.ts::island_for_session`; the drawing: `IslandGlyph.vue`.
 *
 * Island colour and merge status: owned by `island-status.ts` — see the note there.
 */
import { island_status } from '../engine/terminal/island-status.ts';
import type { SessionIsland } from '../engine/terminal/multi-terminal-layout.ts';
import IslandGlyph from './IslandGlyph.vue';

defineProps<{ island: SessionIsland }>();
</script>

<template>
    <span class="island" :style="{ color: island_status(island).color }" role="img" :title="island.title" :aria-label="island.title">
        <IslandGlyph />
        <span v-if="island_status(island).merged" aria-hidden="true">✓</span>
        <span v-if="island.label" class="island__label">{{ island.label }}</span>
    </span>
</template>

<style scoped>
.island {
    flex: 0 1 auto;
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--color-accent-green, #93ffd4);
}
.island__label {
    max-width: 16ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.68rem;
    letter-spacing: 0.04em;
}
</style>
