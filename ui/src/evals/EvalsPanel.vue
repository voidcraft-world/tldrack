<script setup lang="ts">
/** One numbered panel of the EVALS page: its head, its one-line lede, its body. */
defineProps<{ num: number; figure: string; title: string; lede?: string; fixture: boolean; span?: boolean }>();
</script>

<template>
    <section class="ev-panel" :class="{ 'ev-span': span }" :data-figure="figure" :aria-labelledby="`ev-${figure}`">
        <header class="ev-panel__head">
            <span class="ev-panel__num" aria-hidden="true">{{ num }}</span>
            <h2 :id="`ev-${figure}`" class="ev-panel__title">{{ title }}</h2>
            <span v-if="fixture" class="ev-fixture" title="No model produced these numbers: they come from a seeded generator.">FIXTURE · invented numbers</span>
        </header>
        <p v-if="lede" class="ev-panel__lede">{{ lede }}</p>
        <div class="ev-panel__body"><slot /></div>
    </section>
</template>
