<template>
    <button type="button" class="board-btn lr-undo" :disabled="disabled" :title="title" @click="emit('undo')">
        <span class="fui-corner tl"></span><span class="fui-corner br"></span>
        UNDO<kbd class="fui-key">U</kbd>
    </button>
</template>

<script setup lang="ts">
/** The deck's UNDO, drawn in two places (the header at phone width, the stats strip wider): `LayaReviewDeck.vue` owns which shows. */
defineProps<{ disabled: boolean; title: string }>();
const emit = defineEmits<{ undo: [] }>();
</script>
