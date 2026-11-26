<script setup lang="ts">
import { computed, ref } from 'vue';

import { clamp, linear_scale, UNIT_TICKS, unit_tick_anchor } from '../evals/chart-geometry.ts';
import { use_width } from '../evals/use-width.ts';
import { moved_rows, type PairedRows } from './training-model.ts';

/**
 * The same rows under both models: across, what the served model said; up, what the candidate
 * says. A dot on the diagonal did not move. An open row belongs high, a done row low, so an open
 * row above the diagonal and a done row below it both moved the right way.
 */
const props = defineProps<{ rows: PairedRows; cut: number; label: string; served_label: string }>();

const M = { top: 12, right: 14, bottom: 40, left: 60 };
const box = ref<HTMLElement | null>(null);
const box_width = use_width(box, 360);
const size = computed(() => clamp(box_width.value, 240, 420));
const inner = computed(() => size.value - M.left - M.right);
const height = computed(() => M.top + inner.value + M.bottom);
const x = computed(() => linear_scale([0, 1], [M.left, M.left + inner.value]));
const y = computed(() => linear_scale([0, 1], [M.top + inner.value, M.top]));
const moved = computed(() => moved_rows(props.rows, props.cut));
</script>

<template>
    <div ref="box" class="pair">
        <svg :width="size" :height="height" role="img"
            :aria-label="`Each row under both models. ${moved.fixed} rows the candidate gets right that the served model got wrong, ${moved.broke} the other way.`">
            <g v-for="tick in UNIT_TICKS" :key="tick">
                <line :x1="x(tick)" :x2="x(tick)" :y1="M.top" :y2="M.top + inner" class="pair__grid" />
                <line :x1="M.left" :x2="M.left + inner" :y1="y(tick)" :y2="y(tick)" class="pair__grid" />
                <text :x="x(tick)" :y="M.top + inner + 15" class="pair__tick" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
                <text :x="M.left - 7" :y="y(tick) + 4" class="pair__tick" text-anchor="end">{{ tick }}</text>
            </g>
            <line :x1="x(0)" :y1="y(0)" :x2="x(1)" :y2="y(1)" class="pair__same" />
            <line :x1="x(rows.cuts.served)" :x2="x(rows.cuts.served)" :y1="M.top" :y2="M.top + inner" class="pair__cut pair__cut--base" />
            <line :x1="M.left" :x2="M.left + inner" :y1="y(cut)" :y2="y(cut)" class="pair__cut" />
            <circle v-for="(is_open, i) in rows.expected" :key="i" :cx="x(rows.served[i]!)" :cy="y(rows.candidate[i]!)" r="2.8"
                :class="is_open ? 'pair__dot--open' : 'pair__dot--done'" />
            <text :x="M.left + inner / 2" :y="height - 6" class="pair__tick" text-anchor="middle">{{ served_label }} · p(open)</text>
            <text :transform="`translate(13 ${M.top + inner / 2}) rotate(-90)`" class="pair__tick" text-anchor="middle">{{ label }} · p(open)</text>
        </svg>
        <ul class="pair__legend">
            <li><i class="pair__key pair__key--open"></i>open work: better when higher</li>
            <li><i class="pair__key pair__key--done"></i>done: better when lower</li>
            <li>At these cuts it fixes <b>{{ moved.fixed }}</b> row{{ moved.fixed === 1 ? '' : 's' }} and breaks <b>{{ moved.broke }}</b>.</li>
        </ul>
    </div>
</template>

<style scoped>
.pair { min-width: 0; }
.pair__grid { stroke: rgba(157, 218, 255, 0.09); }
.pair__same { stroke: rgba(157, 218, 255, 0.35); stroke-dasharray: 2 5; }
.pair__cut { stroke: var(--fui-text); stroke-width: 1; opacity: 0.55; }
.pair__cut--base { stroke-dasharray: 3 4; }
.pair__tick { fill: var(--fui-text-dim); font: 0.68rem var(--fui-mono); }
.pair__dot--open { fill: var(--color-accent-green); opacity: 0.85; }
.pair__dot--done { fill: none; stroke: var(--fui-slate); stroke-width: 1.4; opacity: 0.9; }
.pair__legend { list-style: none; margin: 6px 0 0; padding: 0; font-size: 0.72rem; color: var(--fui-text-dim); display: grid; gap: 3px; }
.pair__legend b { color: var(--fui-text); font-family: var(--fui-mono); }
.pair__key { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; }
.pair__key--open { background: var(--color-accent-green); }
.pair__key--done { border: 1.4px solid var(--fui-slate); box-sizing: border-box; }
</style>
