<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchSetResult, BenchVoter } from '@tldrack/schemas/bench-results.ts';

import ChartTip from './ChartTip.vue';
import VoterLegend from './VoterLegend.vue';
import { arrow_step, clamp, line_path, linear_scale, nearest_index, unit_tick_anchor, UNIT_TICKS, type Point } from './chart-geometry.ts';
import { fmt_fixed, fmt_interval, fmt_share, draw_order, focused_voter, roc_point_at, roc_tpr_at, scored_voters } from './evals-model.ts';
import { use_width } from './use-width.ts';

/** Panel 3. One ROC curve per voter on one set, the focus voter bold, its cut marked. */
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

const box = ref<HTMLElement | null>(null);
const box_width = use_width(box, 460);
const size = computed(() => clamp(box_width.value, 280, 460));
const inner = computed(() => size.value - MARGIN.left - MARGIN.right);
const height = computed(() => MARGIN.top + inner.value + MARGIN.bottom);
const x = computed(() => linear_scale([0, 1], [MARGIN.left, MARGIN.left + inner.value]));
const y = computed(() => linear_scale([0, 1], [MARGIN.top + inner.value, MARGIN.top]));

const scored = computed(() => scored_voters(props.result, props.voters, props.slots, props.focus));
/** Drawing order: the focus voter last, so it lies over the rest. */
const curves = computed(() =>
    scored.value
        .map((entry) => ({ ...entry, path: line_path(entry.score.roc.map((point): Point => [x.value(point.fpr), y.value(point.tpr)])) }))
        .sort(draw_order),
);
const focused = computed(() => focused_voter(scored.value));

const operating = computed(() => {
    const entry = focused.value;
    if (!entry) return null;
    const cut = entry.score.operating.threshold;
    const point = roc_point_at(entry.score.roc, cut);
    const px = x.value(point.fpr);
    return { px, py: y.value(point.tpr), cut, point, source: entry.score.operating.source, color: entry.color, flip: px > size.value - 110 };
});

// The readout: a position along the focus voter's own false-⚠️ rates, by pointer or arrow key.
const stops = computed(() => [...new Set(focused.value?.score.roc.map((point) => point.fpr) ?? [])].sort((a, b) => a - b));
const at = ref<number | null>(null);
const readout = computed(() => {
    const fpr = at.value === null ? undefined : stops.value[at.value];
    if (fpr === undefined) return null;
    const rows = scored.value.filter((entry) => !entry.rest).map((entry) => ({ ...entry, tpr: roc_tpr_at(entry.score.roc, fpr) }));
    return { fpr, px: x.value(fpr), rows };
});

function on_move(event: PointerEvent): void {
    const left = (event.currentTarget as SVGElement).getBoundingClientRect().left;
    at.value = nearest_index(stops.value, clamp((event.clientX - left - MARGIN.left) / inner.value, 0, 1));
}

function on_key(event: KeyboardEvent): void {
    const next = arrow_step(event.key, at.value, stops.value.length - 1);
    if (next === undefined) return;
    at.value = next;
    event.preventDefault();
}

const columns = computed(() => [
    {
        head: `AUC · ${props.level}% interval`,
        cells: Object.fromEntries(scored.value.map(({ voter, score }) => [voter.name, score.auc ? fmt_fixed(score.auc.value, 3) : '—'])),
        under: Object.fromEntries(scored.value.flatMap(({ voter, score }) => (score.auc ? [[voter.name, fmt_interval(score.auc, 3)]] : []))),
    },
]);
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
                :aria-label="`ROC curves on the ${set_label} set, one per voter. Left and right arrows read the values; the legend table has each AUC.`"
                @pointermove="on_move"
                @pointerleave="at = null"
                @keydown="on_key"
                @blur="at = null"
            >
                <text class="ev-plot__title" x="0" y="14">Open turns flagged</text>
                <g v-for="tick in UNIT_TICKS" :key="tick">
                    <line class="ev-grid" :x1="MARGIN.left" :x2="MARGIN.left + inner" :y1="y(tick)" :y2="y(tick)" />
                    <line class="ev-grid" :x1="x(tick)" :x2="x(tick)" :y1="MARGIN.top" :y2="MARGIN.top + inner" />
                    <text :x="MARGIN.left - 8" :y="y(tick)" dy="0.35em" text-anchor="end">{{ tick }}</text>
                    <text :x="x(tick)" :y="MARGIN.top + inner + 20" :text-anchor="unit_tick_anchor(tick)">{{ tick }}</text>
                </g>
                <line class="ev-axis" :x1="x(0)" :y1="y(0)" :x2="x(1)" :y2="y(1)" />
                <text class="ev-plot__dim" :x="x(0.62) + 8" :y="y(0.62) + 18">chance</text>
                <text class="ev-plot__title" :x="MARGIN.left + inner" :y="MARGIN.top + inner + 46" text-anchor="end">Done turns flagged (false ⚠️)</text>

                <path v-for="curve in curves" :key="curve.voter.name" class="ev-line" :class="{ 'ev-line--focus': curve.focus, 'ev-line--rest': curve.rest }" :d="curve.path" :stroke="curve.color" />

                <g v-if="operating">
                    <circle class="ev-dot" :cx="operating.px" :cy="operating.py" r="6" :fill="operating.color" />
                    <text class="ev-plot__mark" :x="operating.px + (operating.flip ? -14 : 14)" :y="operating.py + 24" :text-anchor="operating.flip ? 'end' : 'start'">
                        cut {{ fmt_fixed(operating.cut, 2) }}
                    </text>
                </g>

                <g v-if="readout">
                    <line class="ev-axis" :x1="readout.px" :x2="readout.px" :y1="MARGIN.top" :y2="MARGIN.top + inner" />
                    <circle v-for="row in readout.rows" :key="row.voter.name" class="ev-dot" :cx="readout.px" :cy="y(row.tpr)" r="5" :fill="row.color" />
                </g>
            </svg>
            <ChartTip v-if="readout" :x="readout.px" :y="MARGIN.top + inner * 0.42" :box_width="box_width" :head="`False ⚠️ rate ${fmt_fixed(readout.fpr, 2)}`">
                <template v-for="row in readout.rows" :key="row.voter.name">
                    <span class="ev-swatch" :style="{ background: row.color }"></span>
                    <b>{{ fmt_fixed(row.tpr, 2) }}</b>
                    <span>{{ row.voter.label }}</span>
                </template>
            </ChartTip>
            <dl v-if="focused && operating" class="ev-tiles ev-tiles--after">
                <div class="ev-tile">
                    <dt>Open turns flagged</dt>
                    <dd>
                        <b>{{ fmt_share(operating.point.tpr) }}</b>
                        at the {{ operating.source === 'live' ? 'served' : 'swept' }} cut, {{ fmt_fixed(operating.cut, 2) }}
                    </dd>
                </div>
                <div class="ev-tile">
                    <dt>Done turns flagged</dt>
                    <dd>
                        <b>{{ fmt_share(operating.point.fpr) }}</b>
                        false ⚠️ at that cut
                    </dd>
                </div>
            </dl>
        </div>

        <div>
            <VoterLegend :scored="scored" :columns="columns" @focus="$emit('focus', $event)" />
            <details v-if="focused" class="ev-details">
                <summary>{{ focused.voter.label }} · the curve as a table</summary>
                <div class="ev-details__body">
                    <table class="ev-table">
                        <thead>
                            <tr>
                                <th scope="col" class="ev-r">Cut</th>
                                <th scope="col" class="ev-r">Done flagged</th>
                                <th scope="col" class="ev-r">Open flagged</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="(point, i) in focused.score.roc" :key="i">
                                <td class="ev-r ev-num">{{ fmt_fixed(point.threshold, 3) }}</td>
                                <td class="ev-r ev-num">{{ fmt_fixed(point.fpr, 3) }}</td>
                                <td class="ev-r ev-num">{{ fmt_fixed(point.tpr, 3) }}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </details>
        </div>
    </div>
</template>
