<script setup lang="ts">
import { computed, ref } from 'vue';

import { clamp, linear_scale, UNIT_TICKS, unit_tick_anchor } from '../evals/chart-geometry.ts';
import { use_width } from '../evals/use-width.ts';
import { confusion_at, stack_dots, type Dot, type ScoredSet } from './training-model.ts';

/**
 * Every row as one dot at its probability, open work above the line and done turns below. The cut
 * is a line the operator drags: it splits each band in two, and the four pieces are the quadrant.
 */
const props = defineProps<{ rows: ScoredSet; cut: number; baseline_cut: number | null }>();
const emit = defineEmits<{ cut: [value: number] }>();

const R = 3;
const STEP = 2 * R + 1;
const PAD = { left: 12, right: 12, top: 26, axis: 26, gap: 30 };

const box = ref<HTMLElement | null>(null);
const width = use_width(box, 640);
const inner = computed(() => Math.max(120, width.value - PAD.left - PAD.right));
const bins = computed(() => Math.max(10, Math.floor(inner.value / STEP)));
const x = computed(() => linear_scale([0, 1], [PAD.left, PAD.left + inner.value]));

const open = computed(() => stack_dots(props.rows.open, bins.value));
const done = computed(() => stack_dots(props.rows.done, bins.value));
/** A band is never taller than this: a stack past it is squeezed (its dots overlap into a bar) and says its count. */
const BAND_MAX = 96;
const band = (tallest: number): number => clamp(tallest * STEP + 6, 34, BAND_MAX);
const rise = (tallest: number): number => Math.min(STEP, (BAND_MAX - 2 * R - 6) / Math.max(1, tallest - 1));
const open_base = computed(() => PAD.top + band(open.value.tallest));
const done_top = computed(() => open_base.value + PAD.gap);
const done_base = computed(() => done_top.value + band(done.value.tallest));
const height = computed(() => done_base.value + PAD.axis);
const bin_x = (bin: number): number => PAD.left + ((bin + 0.5) / bins.value) * inner.value;
/** A band's tallest stack when it is too tall to count by eye, with how many rows it holds. */
function squeezed(stack: { dots: Dot[]; tallest: number }, base: number): { x: number; y: number; n: number }[] {
    if (rise(stack.tallest) >= STEP) return [];
    const tops = new Map<number, number>();
    for (const dot of stack.dots) tops.set(dot.bin, Math.max(tops.get(dot.bin) ?? 0, dot.level + 1));
    const dy = rise(stack.tallest);
    // The tallest one only: two labels over neighbouring stacks would print over each other.
    const [bin, n] = [...tops].reduce((top, entry) => (entry[1] > top[1] ? entry : top));
    return [{ x: bin_x(bin), y: base - R - 2 - (n - 1) * dy - R - 4, n }];
}
const tall = computed(() => [...squeezed(open.value, open_base.value), ...squeezed(done.value, done_base.value)]);

const counts = computed(() => confusion_at(props.rows, props.cut));
const cut_x = computed(() => x.value(props.cut));

const dragging = ref(false);
function set_from(event: PointerEvent): void {
    const left = (event.currentTarget as SVGElement).getBoundingClientRect().left;
    emit('cut', Math.round(clamp((event.clientX - left - PAD.left) / inner.value, 0, 1) * 100) / 100);
}
function on_down(event: PointerEvent): void {
    dragging.value = true;
    (event.currentTarget as SVGElement).setPointerCapture(event.pointerId);
    set_from(event);
}
function on_move(event: PointerEvent): void {
    if (dragging.value) set_from(event);
}
</script>

<template>
    <div ref="box" class="strip">
        <svg :width="width" :height="height" class="strip__svg" role="img"
            :aria-label="`Every row at its probability. At cut ${cut.toFixed(2)}: ${counts.tp} caught, ${counts.fn} missed, ${counts.fp} false warnings, ${counts.tn} quiet.`"
            @pointerdown="on_down" @pointermove="on_move" @pointerup="dragging = false" @pointercancel="dragging = false">
            <rect :x="cut_x" :y="PAD.top - 8" :width="Math.max(0, PAD.left + inner - cut_x)" :height="height - PAD.top - PAD.axis + 8" class="strip__flagged" />
            <text :x="PAD.left" :y="14" class="strip__band">OPEN WORK · {{ rows.open.length }}</text>
            <text :x="PAD.left" :y="done_top - 8" class="strip__band">DONE · {{ rows.done.length }}</text>
            <line :x1="PAD.left" :x2="PAD.left + inner" :y1="open_base" :y2="open_base" class="strip__base" />
            <line :x1="PAD.left" :x2="PAD.left + inner" :y1="done_base" :y2="done_base" class="strip__base" />
            <circle v-for="(dot, i) in open.dots" :key="`o${i}`" :cx="bin_x(dot.bin)" :cy="open_base - R - 2 - dot.level * rise(open.tallest)" :r="R"
                :class="dot.p >= cut ? 'strip__dot--right' : 'strip__dot--wrong'" />
            <circle v-for="(dot, i) in done.dots" :key="`d${i}`" :cx="bin_x(dot.bin)" :cy="done_base - R - 2 - dot.level * rise(done.tallest)" :r="R"
                :class="dot.p >= cut ? 'strip__dot--wrong' : 'strip__dot--quiet'" />
            <text v-for="(stack, i) in tall" :key="`t${i}`" :x="Math.min(stack.x, PAD.left + inner - 2)" :y="stack.y" class="strip__tall" text-anchor="end">{{ stack.n }} rows</text>
            <line v-if="baseline_cut !== null" :x1="x(baseline_cut)" :x2="x(baseline_cut)" :y1="PAD.top - 8" :y2="done_base" class="strip__ghost" />
            <line :x1="cut_x" :x2="cut_x" :y1="PAD.top - 8" :y2="done_base" class="strip__cut" />
            <text :x="cut_x" :y="PAD.top - 12" class="strip__cut-label" :text-anchor="cut > 0.9 ? 'end' : cut < 0.1 ? 'start' : 'middle'">⚠️ at {{ cut.toFixed(2) }}</text>
            <g v-for="tick in UNIT_TICKS" :key="tick">
                <text :x="x(tick)" :y="height - 8" class="strip__tick" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
            </g>
        </svg>
        <label class="strip__slider">
            <span class="fui-techtext">CUT</span>
            <input type="range" min="0" max="1" step="0.01" :value="cut" aria-label="The cut: a probability at or over it draws a warning"
                @input="emit('cut', Number(($event.target as HTMLInputElement).value))" />
            <span class="strip__hint">drag the line, or use this · a row at or right of it draws ⚠️</span>
        </label>
    </div>
</template>

<style scoped>
.strip { min-width: 0; }
.strip__svg { display: block; cursor: ew-resize; touch-action: none; user-select: none; }
.strip__flagged { fill: rgba(248, 193, 70, 0.06); }
.strip__band { fill: var(--fui-text-dim); font: 600 0.68rem var(--fui-mono); letter-spacing: 0.1em; }
.strip__base { stroke: rgba(157, 218, 255, 0.22); stroke-width: 1; }
.strip__dot--right { fill: var(--color-accent-green); }
.strip__dot--wrong { fill: var(--fui-warn); }
.strip__dot--quiet { fill: rgba(157, 218, 255, 0.5); }
.strip__cut { stroke: var(--fui-text); stroke-width: 1.5; }
.strip__ghost { stroke: var(--fui-text-dim); stroke-width: 1; stroke-dasharray: 3 4; }
.strip__cut-label { fill: var(--fui-text); font: 600 0.7rem var(--fui-mono); }
.strip__tall { fill: var(--fui-text); font: 600 0.68rem var(--fui-mono); }
.strip__tick { fill: var(--fui-text-dim); font: 0.68rem var(--fui-mono); }
.strip__slider { display: flex; align-items: center; gap: 10px; margin-top: 4px; font-size: 0.72rem; color: var(--fui-text-dim); }
.strip__slider input { flex: 0 1 260px; accent-color: var(--fui-slate); }
.strip__hint { min-width: 0; }
</style>
