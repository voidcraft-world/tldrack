<script setup lang="ts">
import { computed } from 'vue';

/**
 * A chart's readout at the pointer or the keyboard position. It sits beside `x` and flips to
 * the other side past the middle of its box, so it never leaves the figure.
 */
const props = defineProps<{ x: number; y: number; box_width: number; head: string }>();

const style = computed(() =>
    props.x > props.box_width / 2
        ? { right: `${props.box_width - props.x + 14}px`, top: `${props.y}px` }
        : { left: `${props.x + 14}px`, top: `${props.y}px` },
);
</script>

<template>
    <div class="ev-tip" :style="style" role="status">
        <p class="ev-tip__head">{{ head }}</p>
        <div class="ev-tip__rows"><slot /></div>
    </div>
</template>
