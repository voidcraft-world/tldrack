<script setup lang="ts">
import { computed, ref } from 'vue';

import { clamp, line_path, linear_scale, UNIT_TICKS, unit_tick_anchor, type Point } from '../evals/chart-geometry.ts';
import { use_width } from '../evals/use-width.ts';
import { auc_of, confusion_at, fmt_p, roc_of, type ScoredSet } from './training-model.ts';

/** The ROC curve of the pick and of its baseline, each curve's points drawn, each at its own cut marked. */
const props = defineProps<{
    rows: ScoredSet;
    cut: number;
    label: string;
    baseline: ScoredSet | null;
    baseline_cut: number | null;
    baseline_label: string | null;
}>();

const M = { top: 12, right: 14, bottom: 40, left: 60 };
const box = ref<HTMLElement | null>(null);
const box_width = use_width(box, 360);
const size = computed(() => clamp(box_width.value, 240, 420));
const inner = computed(() => size.value - M.left - M.right);
const height = computed(() => M.top + inner.value + M.bottom);
const x = computed(() => linear_scale([0, 1], [M.left, M.left + inner.value]));
const y = computed(() => linear_scale([0, 1], [M.top + inner.value, M.top]));

function curve(rows: ScoredSet, cut: number) {
    const roc = roc_of(rows);
    const c = confusion_at(rows, cut);
    const at: Point = [x.value(rows.done.length ? c.fp / rows.done.length : 0), y.value(rows.open.length ? c.tp / rows.open.length : 0)];
    const points = roc.map((point): Point => [x.value(point.fpr), y.value(point.tpr)]);
    return { points, path: line_path(points), at, auc: auc_of(rows) };
}
const mine = computed(() => curve(props.rows, props.cut));
const theirs = computed(() => (props.baseline && props.baseline_cut !== null ? curve(props.baseline, props.baseline_cut) : null));
</script>

<template>
    <div ref="box" class="roc">
        <svg :width="size" :height="height" role="img"
            :aria-label="`ROC curve of ${label}, area ${fmt_p(mine.auc, 3)}${theirs ? `, against ${baseline_label}, area ${fmt_p(theirs.auc, 3)}` : ''}`">
            <g v-for="tick in UNIT_TICKS" :key="tick">
                <line :x1="x(tick)" :x2="x(tick)" :y1="M.top" :y2="M.top + inner" class="roc__grid" />
                <line :x1="M.left" :x2="M.left + inner" :y1="y(tick)" :y2="y(tick)" class="roc__grid" />
                <text :x="x(tick)" :y="M.top + inner + 15" class="roc__tick" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
                <text :x="M.left - 7" :y="y(tick) + 4" class="roc__tick" text-anchor="end">{{ tick }}</text>
            </g>
            <line :x1="x(0)" :y1="y(0)" :x2="x(1)" :y2="y(1)" class="roc__chance" />
            <template v-if="theirs">
                <path :d="theirs.path" class="roc__line roc__line--base" />
                <circle :cx="theirs.at[0]" :cy="theirs.at[1]" r="4.5" class="roc__at roc__at--base" />
            </template>
            <path :d="mine.path" class="roc__line" />
            <circle v-for="(point, i) in mine.points" :key="i" :cx="point[0]" :cy="point[1]" r="1.6" class="roc__point" />
            <circle :cx="mine.at[0]" :cy="mine.at[1]" r="5.5" class="roc__at" />
            <text :x="M.left + inner / 2" :y="height - 6" class="roc__axis" text-anchor="middle">done turns flagged (false ⚠️ rate)</text>
            <text :transform="`translate(13 ${M.top + inner / 2}) rotate(-90)`" class="roc__axis" text-anchor="middle">open work caught</text>
        </svg>
        <ul class="roc__legend">
            <li><i class="roc__key"></i>{{ label }} <b>AUC {{ fmt_p(mine.auc, 3) }}</b></li>
            <li v-if="theirs"><i class="roc__key roc__key--base"></i>{{ baseline_label }} <b>AUC {{ fmt_p(theirs.auc, 3) }}</b></li>
            <li class="roc__note">Each small point is one distinct score. The ring is the cut.</li>
        </ul>
    </div>
</template>

<style scoped>
.roc { min-width: 0; }
.roc__grid { stroke: rgba(157, 218, 255, 0.09); }
.roc__chance { stroke: rgba(157, 218, 255, 0.3); stroke-dasharray: 2 5; }
.roc__tick, .roc__axis { fill: var(--fui-text-dim); font: 0.68rem var(--fui-mono); }
.roc__line { fill: none; stroke: var(--color-accent-green); stroke-width: 2; stroke-linejoin: round; }
.roc__line--base { stroke: var(--fui-slate); stroke-width: 1.5; stroke-dasharray: 5 4; opacity: 0.85; }
.roc__point { fill: var(--color-accent-green); opacity: 0.75; }
.roc__at { fill: #080c12; stroke: var(--color-accent-green); stroke-width: 2.5; }
.roc__at--base { stroke: var(--fui-slate); stroke-width: 2; }
.roc__legend { list-style: none; margin: 6px 0 0; padding: 0; font: 0.72rem var(--fui-mono); color: var(--fui-text-dim); display: grid; gap: 3px; }
.roc__legend b { color: var(--fui-text); font-weight: 600; margin-left: 6px; }
.roc__key { display: inline-block; width: 18px; height: 0; border-top: 2px solid var(--color-accent-green); margin-right: 8px; vertical-align: middle; }
.roc__key--base { border-top: 2px dashed var(--fui-slate); }
.roc__note { font-family: inherit; opacity: 0.8; }
</style>
