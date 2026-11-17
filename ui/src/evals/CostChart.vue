<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchProtocol, BenchSetResult, BenchVoter } from '@tldrack/schemas/bench-results.ts';

import ChartTip from './ChartTip.vue';
import { arrow_step, clamp, linear_scale, nice_ticks, nice_top, step_path, unit_tick_anchor, UNIT_TICKS, type Point } from './chart-geometry.ts';
import { cheapest_step, cost_at, cost_steps, fmt_count, fmt_fixed, fmt_interval, focused_voter, scored_voters } from './evals-model.ts';
import { use_width } from './use-width.ts';

/**
 * Panel 5. What every cut would cost the focus voter on one set: the false ⚠️ it would show,
 * the open turns it would miss, and the two weighed into one cost. The cut the rack serves
 * with is marked with its interval, beside the cheapest cut on this set. The two markers are
 * keyed on their readouts above the plot, not labelled on it: near each other, two labels on
 * the cost line cover the line.
 */
const props = defineProps<{
    result: BenchSetResult;
    voters: BenchVoter[];
    slots: Record<string, number | null>;
    focus: string;
    set_label: string;
    costs: BenchProtocol['costs'];
    level: number;
}>();

const MARGIN = { top: 34, right: 20, bottom: 54, left: 50 };
const HEIGHT = 340;
/** The readout moves in hundredths of a cut. */
const STOPS = 100;

const box = ref<HTMLElement | null>(null);
const box_width = use_width(box, 760);
const width = computed(() => clamp(box_width.value, 300, 860));
const plot_right = computed(() => width.value - MARGIN.right);
const plot_bottom = HEIGHT - MARGIN.bottom;

const focused = computed(() => focused_voter(scored_voters(props.result, props.voters, props.slots, props.focus)));
const steps = computed(() => (focused.value ? cost_steps(focused.value.score, props.result.n, props.costs) : []));
const top = computed(() => nice_top(Math.max(1, ...steps.value.map((step) => step.cost), focused.value?.score.operating.cost.hi ?? 0)));
const x = computed(() => linear_scale([0, 1], [MARGIN.left, plot_right.value]));
const y = computed(() => linear_scale([0, top.value], [plot_bottom, MARGIN.top]));
const y_ticks = computed(() => nice_ticks(0, top.value, 5));

const SERIES = [
    { key: 'false_warnings', label: 'false ⚠️', color: 'var(--ev-warn-line)', bold: false },
    { key: 'missed_open', label: 'missed open', color: 'var(--ev-critical)', bold: false },
    { key: 'cost', label: 'cost', color: 'var(--ev-ink)', bold: true },
] as const;
const lines = computed(() =>
    SERIES.map((series) => ({ ...series, path: step_path(steps.value.map((step): Point => [x.value(step.threshold), y.value(step[series.key])]), 'before') })),
);

const marks = computed(() => {
    const entry = focused.value;
    if (!entry || steps.value.length === 0) return null;
    const { operating } = entry.score;
    const live = { cut: operating.threshold, px: x.value(operating.threshold), py: y.value(cost_at(steps.value, operating.threshold).cost) };
    const cheapest = cheapest_step(steps.value);
    const low = { cut: cheapest.threshold, cost: cheapest.cost, px: x.value(cheapest.threshold), py: y.value(cheapest.cost) };
    return {
        operating,
        live,
        whisker: { top: y.value(operating.cost.hi), bottom: y.value(operating.cost.lo) },
        /** No second marker when the served cut is already the cheapest. */
        low: cheapest.cost < cost_at(steps.value, operating.threshold).cost ? low : null,
    };
});
/** A label's side: it hangs toward the middle of the plot, so neither end clips it. */
const side = (px: number): { dx: number; anchor: 'start' | 'end' } => (px > width.value / 2 ? { dx: -10, anchor: 'end' } : { dx: 10, anchor: 'start' });

// The readout: a cut, by pointer or arrow key.
const at = ref<number | null>(null);
const readout = computed(() => {
    if (at.value === null || steps.value.length === 0) return null;
    const cut = at.value / STOPS;
    return { cut, px: x.value(cut), step: cost_at(steps.value, cut) };
});

function on_move(event: PointerEvent): void {
    const left = (event.currentTarget as SVGElement).getBoundingClientRect().left;
    at.value = Math.round(clamp((event.clientX - left - MARGIN.left) / (plot_right.value - MARGIN.left), 0, 1) * STOPS);
}

function on_key(event: KeyboardEvent): void {
    const next = arrow_step(event.key, at.value ?? Math.round((marks.value?.live.cut ?? 0.5) * STOPS), STOPS);
    if (next === undefined) return;
    at.value = next;
    event.preventDefault();
}
</script>

<template>
    <p v-if="!focused || !marks" class="ev-empty">This set scored no voter.</p>
    <div v-else>
        <dl class="ev-tiles">
            <div class="ev-tile">
                <dt><i class="ev-key ev-key--dot"></i>{{ marks.operating.source === 'live' ? 'The served cut' : 'The swept cut' }}</dt>
                <dd>
                    <b>{{ fmt_fixed(marks.operating.threshold, 2) }}</b>
                    {{ fmt_count(marks.operating.false_warnings) }} false ⚠️ · {{ fmt_count(marks.operating.missed_open) }} missed open
                </dd>
            </div>
            <div class="ev-tile">
                <dt>Cost at that cut</dt>
                <dd>
                    <b>{{ fmt_fixed(marks.operating.cost.value, 0) }}</b>
                    {{ level }}% interval {{ fmt_interval(marks.operating.cost, 0) }}
                </dd>
            </div>
            <div v-if="marks.low" class="ev-tile">
                <dt><i class="ev-key ev-key--ring"></i>Cheapest cut on this set</dt>
                <dd>
                    <b>{{ fmt_fixed(marks.low.cost, 0) }}</b>
                    at {{ fmt_fixed(marks.low.cut, 2) }} · found on these same rows
                </dd>
            </div>
        </dl>

        <p class="ev-formula" style="margin-top: 22px">
            <span><i class="ev-swatch" style="background: var(--ev-ink); height: 6px"></i>Cost</span>
            = {{ costs.false_warning }} ×
            <span><i class="ev-swatch" style="background: var(--ev-warn-line)"></i>false ⚠️</span>
            +
            <span><i class="ev-swatch" style="background: var(--ev-critical)"></i>missed open</span>
        </p>

        <div ref="box" class="ev-plotbox">
            <svg
                class="ev-plot"
                :width="width"
                :height="HEIGHT"
                tabindex="0"
                role="img"
                :aria-label="`Cost against the cut for ${focused.voter.label} on the ${set_label} set. Left and right arrows read each cut; the table below has every step.`"
                @pointermove="on_move"
                @pointerleave="at = null"
                @keydown="on_key"
                @blur="at = null"
            >
                <text class="ev-plot__title" x="0" y="14">Rows, and cost</text>
                <g v-for="tick in y_ticks" :key="`y${tick}`">
                    <line class="ev-grid" :x1="MARGIN.left" :x2="plot_right" :y1="y(tick)" :y2="y(tick)" />
                    <text :x="MARGIN.left - 8" :y="y(tick)" dy="0.35em" text-anchor="end">{{ fmt_count(tick) }}</text>
                </g>
                <text v-for="tick in UNIT_TICKS" :key="`x${tick}`" :x="x(tick)" :y="plot_bottom + 20" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
                <line class="ev-axis" :x1="MARGIN.left" :x2="plot_right" :y1="plot_bottom" :y2="plot_bottom" />
                <text class="ev-plot__title" :x="plot_right" :y="plot_bottom + 46" text-anchor="end">Cut: ⚠️ at p(open) ≥</text>

                <line class="ev-axis" :x1="marks.live.px" :x2="marks.live.px" :y1="MARGIN.top" :y2="plot_bottom" />
                <path v-for="line in lines" :key="line.key" class="ev-line" :class="{ 'ev-line--focus': line.bold }" :d="line.path" :stroke="line.color" />

                <line :x1="marks.live.px" :x2="marks.live.px" :y1="marks.whisker.top" :y2="marks.whisker.bottom" stroke="var(--ev-ink)" stroke-width="2" stroke-linecap="round" />
                <circle class="ev-dot" :cx="marks.live.px" :cy="marks.live.py" r="6" fill="var(--ev-ink)" />
                <text class="ev-plot__mark" :x="marks.live.px + side(marks.live.px).dx" :y="MARGIN.top + 14" :text-anchor="side(marks.live.px).anchor">
                    {{ marks.operating.source === 'live' ? 'served' : 'swept' }} {{ fmt_fixed(marks.live.cut, 2) }}
                </text>
                <circle v-if="marks.low" :cx="marks.low.px" :cy="marks.low.py" r="5" fill="var(--ev-surface)" stroke="var(--ev-ink)" stroke-width="2" />

                <g v-if="readout">
                    <line class="ev-axis" :x1="readout.px" :x2="readout.px" :y1="MARGIN.top" :y2="plot_bottom" />
                    <circle v-for="series in SERIES" :key="series.key" class="ev-dot" :cx="readout.px" :cy="y(readout.step[series.key])" r="5" :fill="series.color" />
                </g>
            </svg>
            <ChartTip v-if="readout" :x="readout.px" :y="MARGIN.top + 30" :box_width="box_width" :head="`Cut ${fmt_fixed(readout.cut, 2)}`">
                <template v-for="series in SERIES.toReversed()" :key="series.key">
                    <span class="ev-swatch" :style="{ background: series.color }"></span>
                    <b>{{ fmt_count(readout.step[series.key]) }}</b>
                    <span>{{ series.label }}</span>
                </template>
            </ChartTip>
        </div>

        <details class="ev-details">
            <summary>{{ focused.voter.label }} · every cut as a table</summary>
            <div class="ev-details__body">
                <table class="ev-table">
                    <thead>
                        <tr>
                            <th scope="col" class="ev-r">Cut, up to</th>
                            <th scope="col" class="ev-r">False ⚠️</th>
                            <th scope="col" class="ev-r">Missed open</th>
                            <th scope="col" class="ev-r">Cost</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="(step, i) in steps" :key="i">
                            <td class="ev-r ev-num">{{ fmt_fixed(step.threshold, 3) }}</td>
                            <td class="ev-r ev-num">{{ fmt_count(step.false_warnings) }}</td>
                            <td class="ev-r ev-num">{{ fmt_count(step.missed_open) }}</td>
                            <td class="ev-r ev-num">{{ fmt_count(step.cost) }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </details>
    </div>
</template>
