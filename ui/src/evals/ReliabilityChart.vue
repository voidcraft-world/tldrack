<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchSetResult, BenchVoter } from '@tldrack/schemas/bench-results.ts';

import ChartTip from './ChartTip.vue';
import VoterLegend from './VoterLegend.vue';
import { arrow_step, clamp, line_path, linear_scale, unit_tick_anchor, UNIT_TICKS, type Point } from './chart-geometry.ts';
import { fmt_count, fmt_fixed, fmt_interval, draw_order, focused_voter, scored_voters } from './evals-model.ts';
import { use_width } from './use-width.ts';

/**
 * Panel 4. What each voter predicted against what came true, one point per bin. Only the focus
 * voter is drawn in full: ten bins of a hundred rows are noisy, and seven such lines at equal
 * weight are a knot. The other models are thin lines behind it, and the ensembles are not
 * drawn at all: every voter's numbers are in the legend.
 *
 * A point from three rows is not a point from thirty, so a dot's size is its bin's row count,
 * and the strip under the plot counts the same rows.
 */
const props = defineProps<{
    result: BenchSetResult;
    voters: BenchVoter[];
    slots: Record<string, number | null>;
    focus: string;
    set_label: string;
    level: number;
}>();
defineEmits<{ focus: [name: string] }>();

const MARGIN = { top: 34, right: 18, bottom: 54, left: 50 };
/** The rows-per-bin strip under the plot: its head line, then its columns. */
const STRIP = { head: 26, bars: 40, foot: 6 };
/** A column's width, at most: a bin is a span, but its count is one thin mark. */
const BAR_MAX = 24;

const box = ref<HTMLElement | null>(null);
const box_width = use_width(box, 460);
const size = computed(() => clamp(box_width.value, 280, 460));
const inner = computed(() => size.value - MARGIN.left - MARGIN.right);
const strip_top = computed(() => MARGIN.top + inner.value + MARGIN.bottom);
const height = computed(() => strip_top.value + STRIP.head + STRIP.bars + STRIP.foot);
const x = computed(() => linear_scale([0, 1], [MARGIN.left, MARGIN.left + inner.value]));
const y = computed(() => linear_scale([0, 1], [MARGIN.top + inner.value, MARGIN.top]));

const scored = computed(() => scored_voters(props.result, props.voters, props.slots, props.focus));
/** A focus dot's radius, by the rows behind it: the smallest still a dot, the largest still inside its bin. */
const DOT = { min: 4, max: 9 };
const curves = computed(() =>
    scored.value
        .filter((entry) => !entry.rest)
        .map((entry) => {
            const most = Math.max(1, ...entry.score.reliability.map((bin) => bin.n));
            const dots = entry.score.reliability.flatMap((bin) =>
                bin.p === null || bin.rate === null ? [] : [{ at: [x.value(bin.p), y.value(bin.rate)] as Point, r: DOT.min + (DOT.max - DOT.min) * Math.sqrt(bin.n / most) }],
            );
            return { ...entry, dots, path: line_path(dots.map((dot) => dot.at)) };
        })
        .sort(draw_order),
);
const focused = computed(() => focused_voter(scored.value));
const bins = computed(() => focused.value?.score.reliability ?? []);

const bars = computed(() => {
    const most = Math.max(1, ...bins.value.map((bin) => bin.n));
    const base = strip_top.value + STRIP.head + STRIP.bars;
    return bins.value.map((bin) => {
        const width = Math.max(2, Math.min(BAR_MAX, x.value(bin.hi) - x.value(bin.lo) - 2));
        const tall = (bin.n / most) * (STRIP.bars - 16);
        const left = x.value((bin.lo + bin.hi) / 2) - width / 2;
        const radius = Math.min(4, tall, width / 2);
        const top = base - tall;
        const d = `M${left},${base}V${top + radius}Q${left},${top} ${left + radius},${top}H${left + width - radius}Q${left + width},${top} ${left + width},${top + radius}V${base}Z`;
        return { bin, d, cx: left + width / 2, top, most: bin.n === most };
    });
});

// The readout: one of the focus voter's bins, by pointer or arrow key.
const at = ref<number | null>(null);
const readout = computed(() => {
    const bin = at.value === null ? undefined : bins.value[at.value];
    if (!bin) return null;
    const rows = scored.value
        .filter((entry) => !entry.rest)
        .map((entry) => ({ ...entry, bin: entry.score.reliability.find((other) => other.lo === bin.lo && other.hi === bin.hi) }));
    return { bin, left: x.value(bin.lo), width: x.value(bin.hi) - x.value(bin.lo), rows };
});

function on_move(event: PointerEvent): void {
    const left = (event.currentTarget as SVGElement).getBoundingClientRect().left;
    const p = clamp((event.clientX - left - MARGIN.left) / inner.value, 0, 1);
    const found = bins.value.findIndex((bin, i) => p >= bin.lo && (p < bin.hi || i === bins.value.length - 1));
    at.value = found === -1 ? null : found;
}

function on_key(event: KeyboardEvent): void {
    const next = arrow_step(event.key, at.value, bins.value.length - 1);
    if (next === undefined) return;
    at.value = next;
    event.preventDefault();
}

const columns = computed(() => [
    { head: 'ECE', cells: Object.fromEntries(scored.value.map(({ voter, score }) => [voter.name, fmt_fixed(score.ece.value, 3)])) },
    {
        head: `Brier · ${props.level}% interval`,
        cells: Object.fromEntries(scored.value.map(({ voter, score }) => [voter.name, fmt_fixed(score.brier.value, 3)])),
        under: Object.fromEntries(scored.value.map(({ voter, score }) => [voter.name, fmt_interval(score.brier, 3)])),
    },
]);
const bin_range = (bin: { lo: number; hi: number }): string => `${fmt_fixed(bin.lo, 2)} – ${fmt_fixed(bin.hi, 2)}`;
</script>

<template>
    <div class="ev-figure ev-figure--side">
        <div ref="box" class="ev-plotbox">
            <svg
                class="ev-plot"
                :width="size"
                :height="height"
                tabindex="0"
                role="img"
                :aria-label="`Reliability on the ${set_label} set: predicted probability against the share that came true, one line per voter. Left and right arrows read each bin.`"
                @pointermove="on_move"
                @pointerleave="at = null"
                @keydown="on_key"
                @blur="at = null"
            >
                <rect v-if="readout" :x="readout.left" :y="MARGIN.top" :width="readout.width" :height="inner" fill="var(--ev-wash)" />
                <text class="ev-plot__title" x="0" y="14">Came true (open rate)</text>
                <g v-for="tick in UNIT_TICKS" :key="tick">
                    <line class="ev-grid" :x1="MARGIN.left" :x2="MARGIN.left + inner" :y1="y(tick)" :y2="y(tick)" />
                    <line class="ev-grid" :x1="x(tick)" :x2="x(tick)" :y1="MARGIN.top" :y2="MARGIN.top + inner" />
                    <text :x="MARGIN.left - 8" :y="y(tick)" dy="0.35em" text-anchor="end">{{ tick }}</text>
                    <text :x="x(tick)" :y="MARGIN.top + inner + 20" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
                </g>
                <line class="ev-axis" :x1="x(0)" :y1="y(0)" :x2="x(1)" :y2="y(1)" />
                <text class="ev-plot__title" :x="MARGIN.left + inner" :y="MARGIN.top + inner + 46" text-anchor="end">Predicted p(open)</text>

                <g v-for="curve in curves" :key="curve.voter.name">
                    <path class="ev-line" :class="curve.focus ? 'ev-line--focus' : 'ev-line--rest'" :d="curve.path" :stroke="curve.color" />
                    <circle v-for="(dot, i) in curve.focus ? curve.dots : []" :key="i" class="ev-dot" :cx="dot.at[0]" :cy="dot.at[1]" :r="dot.r" :fill="curve.color" />
                </g>

                <g v-if="focused">
                    <text class="ev-plot__title" x="0" :y="strip_top + 14">Rows per bin · {{ focused.voter.label }}</text>
                    <line class="ev-axis" :x1="MARGIN.left" :x2="MARGIN.left + inner" :y1="strip_top + STRIP.head + STRIP.bars" :y2="strip_top + STRIP.head + STRIP.bars" />
                    <g v-for="(bar, i) in bars" :key="i">
                        <path v-if="bar.bin.n > 0" :d="bar.d" :fill="focused.color" />
                        <text v-if="bar.most || at === i" class="ev-plot__mark" :x="bar.cx" :y="bar.top - 5" text-anchor="middle">{{ fmt_count(bar.bin.n) }}</text>
                    </g>
                </g>
            </svg>
            <ChartTip v-if="readout" :x="readout.left + readout.width / 2" :y="MARGIN.top + inner * 0.42" :box_width="box_width" :head="`Predicted ${bin_range(readout.bin)}`">
                <template v-for="row in readout.rows" :key="row.voter.name">
                    <span class="ev-swatch" :style="{ background: row.color }"></span>
                    <b>{{ row.bin && row.bin.rate !== null ? fmt_fixed(row.bin.rate, 2) : '—' }}</b>
                    <span>{{ row.voter.label }} · {{ row.bin ? fmt_count(row.bin.n) : 0 }} rows</span>
                </template>
            </ChartTip>
        </div>

        <div>
            <VoterLegend :scored="scored" :columns="columns" @focus="$emit('focus', $event)" />
            <details v-if="focused" class="ev-details">
                <summary>{{ focused.voter.label }} · the bins as a table</summary>
                <div class="ev-details__body">
                    <table class="ev-table">
                        <thead>
                            <tr>
                                <th scope="col">Predicted</th>
                                <th scope="col" class="ev-r">Rows</th>
                                <th scope="col" class="ev-r">Mean p</th>
                                <th scope="col" class="ev-r">Came true</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="(bin, i) in bins" :key="i">
                                <td class="ev-num">{{ bin_range(bin) }}</td>
                                <td class="ev-r ev-num">{{ fmt_count(bin.n) }}</td>
                                <td class="ev-r ev-num">{{ bin.p === null ? '—' : fmt_fixed(bin.p, 3) }}</td>
                                <td class="ev-r ev-num">{{ bin.rate === null ? '—' : fmt_fixed(bin.rate, 3) }}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </details>
        </div>
    </div>
</template>
