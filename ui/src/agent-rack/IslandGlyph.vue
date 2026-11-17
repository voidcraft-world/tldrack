<script setup lang="ts">
/**
 * The island drawing alone: a mainland, water, and the agent's flag planted on
 * an island off it — the Branch Harbor's own metaphor (branches as islands), at
 * row size. Decorative: whoever places it owns the words (`IslandMark.vue` on a
 * row, the LAUNCH fork's 🏝 button in `AgentRackComponent.vue`), so the two read
 * as one sign — what you pressed is what the row then wears.
 *
 * Paints in `currentColor`; static — no idle motion.
 */
</script>

<template>
    <svg class="island-glyph" viewBox="0 0 34 14" aria-hidden="true">
        <path class="island-glyph__main" d="M0 12 V6.5 Q3 4.5 6 6 Q8.5 7.5 10 12 Z" />
        <path class="island-glyph__water" d="M0 12.5 q2 -1.4 4 0 t4 0 t4 0 t4 0 t4 0 t4 0 t4 0 t4 0 t2 0" />
        <path class="island-glyph__land" d="M17 12 Q23.5 4 30 12 Z" />
        <path class="island-glyph__pole" d="M23.5 8 V1.5" />
        <path class="island-glyph__flag" d="M23.5 1.5 L28 3 L23.5 4.5 Z" />
    </svg>
</template>

<style scoped>
.island-glyph { flex: 0 0 auto; width: 30px; height: 13px; overflow: visible; }
.island-glyph__main { fill: currentColor; opacity: 0.3; }
.island-glyph__water { fill: none; stroke: currentColor; stroke-width: 0.9; stroke-linecap: round; opacity: 0.55; }
.island-glyph__land, .island-glyph__flag { fill: currentColor; }
.island-glyph__pole { stroke: currentColor; stroke-width: 1; }
</style>
