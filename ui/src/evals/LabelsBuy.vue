<script setup lang="ts">
import { computed } from 'vue';
import type { BenchEstimate } from '@tldrack/schemas/bench-results.ts';

import { linear_scale } from './chart-geometry.ts';
import { fmt_delta, fmt_labels, fmt_fixed, fmt_interval, gain_domain, series_color, type GainRow, type LabelGain } from './evals-model.ts';

/**
 * Panel 2. A fine-tune beside the model it was trained from, on the same rows: what the
 * operator's labels bought. The three metrics are one table, so the pair is read across; each
 * row draws both intervals on one axis of its own.
 */
const props = defineProps<{ gains: LabelGain[]; slots: Record<string, number | null>; level: number }>();

const PLOT = { width: 280, height: 66, left: 6, right: 6, base_y: 14, tune_y: 32, axis_y: 44 };

interface Whisker {
    y: number;
    color: string;
    lo: number;
    hi: number;
    at: number;
    spoken: string;
}

function plot_of(gain: LabelGain, row: GainRow): { ends: [string, string]; whiskers: Whisker[] } {
    const domain = gain_domain(row);
    const x = linear_scale(domain, [PLOT.left, PLOT.width - PLOT.right]);
    const whisker = (estimate: BenchEstimate | null, y: number, name: string, label: string): Whisker[] =>
        estimate === null
            ? []
            : [
                  {
                      y,
                      color: series_color(props.slots[name]),
                      lo: x(estimate.lo),
                      hi: x(estimate.hi),
                      at: x(estimate.value),
                      spoken: `${label}: ${fmt_fixed(estimate.value, row.metric.digits)} (${fmt_interval(estimate, row.metric.digits)})`,
                  },
              ];
    return {
        ends: [String(domain[0]), String(domain[1])],
        whiskers: [...whisker(row.base, PLOT.base_y, gain.base.name, gain.base.label), ...whisker(row.tune, PLOT.tune_y, gain.tune.name, gain.tune.label)],
    };
}

const tables = computed(() => props.gains.map((gain) => ({ gain, rows: gain.rows.map((row) => ({ row, plot: plot_of(gain, row) })) })));
</script>

<template>
    <p v-if="gains.length === 0" class="ev-empty">This set scored no fine-tune beside the model it was trained from.</p>
    <div v-for="{ gain, rows } in tables" :key="gain.tune.name" class="ev-gain">
        <p class="ev-gain__pair">
            <span class="ev-voter"><i class="ev-swatch" :style="{ background: series_color(slots[gain.base.name]) }"></i>{{ gain.base.label }}</span>
            <span class="ev-gain__arrow">+ {{ fmt_labels(gain.labels) }} →</span>
            <span class="ev-voter"><i class="ev-swatch" :style="{ background: series_color(slots[gain.tune.name]) }"></i>{{ gain.tune.label }}</span>
        </p>
        <div class="ev-scroll">
            <table class="ev-table ev-matrix">
                <thead>
                    <tr>
                        <th scope="col">Metric</th>
                        <th scope="col">Base</th>
                        <th scope="col" class="ev-plotcell">{{ level }}% intervals</th>
                        <th scope="col">Fine-tune</th>
                        <th scope="col" class="ev-matrix__wide">Change</th>
                    </tr>
                </thead>
                <tbody>
                    <tr v-for="{ row, plot } in rows" :key="row.metric.key">
                        <th scope="row">
                            <span class="ev-metric">{{ row.metric.label }}</span>
                            <span class="ev-sub">{{ row.metric.better }} is better</span>
                        </th>
                        <td>
                            <template v-if="row.base">
                                <span class="ev-value">{{ fmt_fixed(row.base.value, row.metric.digits) }}</span>
                                <span class="ev-sub ev-num">{{ fmt_interval(row.base, row.metric.digits) }}</span>
                            </template>
                            <template v-else>—</template>
                        </td>
                        <td class="ev-plotcell">
                            <svg class="ev-plot" :width="PLOT.width" :height="PLOT.height" aria-hidden="true">
                                <line class="ev-axis" :x1="PLOT.left" :x2="PLOT.width - PLOT.right" :y1="PLOT.axis_y" :y2="PLOT.axis_y" />
                                <text :x="PLOT.left" :y="PLOT.axis_y + 18">{{ plot.ends[0] }}</text>
                                <text :x="PLOT.width - PLOT.right" :y="PLOT.axis_y + 18" text-anchor="end">{{ plot.ends[1] }}</text>
                                <g v-for="whisker in plot.whiskers" :key="whisker.y">
                                    <title>{{ whisker.spoken }}</title>
                                    <line :x1="whisker.lo" :x2="whisker.hi" :y1="whisker.y" :y2="whisker.y" :stroke="whisker.color" stroke-width="2" stroke-linecap="round" />
                                    <circle class="ev-dot" :cx="whisker.at" :cy="whisker.y" r="6" :fill="whisker.color" />
                                </g>
                            </svg>
                        </td>
                        <td>
                            <template v-if="row.tune">
                                <span class="ev-value">{{ fmt_fixed(row.tune.value, row.metric.digits) }}</span>
                                <span class="ev-sub ev-num">{{ fmt_interval(row.tune, row.metric.digits) }}</span>
                                <span v-if="row.delta !== null" class="ev-sub ev-num ev-matrix__narrow">
                                    {{ fmt_delta(row.delta, row.metric.digits) }}<template v-if="row.better !== null"> · {{ row.better ? 'better' : 'worse' }}</template>
                                </span>
                            </template>
                            <template v-else>—</template>
                        </td>
                        <td class="ev-matrix__wide">
                            <template v-if="row.delta !== null">
                                <span class="ev-value" :class="row.better === null ? '' : row.better ? 'ev-delta--better' : 'ev-delta--worse'">
                                    {{ fmt_delta(row.delta, row.metric.digits) }}
                                    <template v-if="row.better !== null">{{ row.better ? '· better' : '· worse' }}</template>
                                </span>
                                <span class="ev-sub">{{ row.apart ? 'intervals apart' : 'intervals overlap' }}</span>
                            </template>
                            <template v-else>—</template>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</template>
