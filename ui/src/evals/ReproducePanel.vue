<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchResults } from '@tldrack/schemas/bench-results.ts';

import { fmt_count, fmt_fixed, fmt_interval, GAIN_METRICS, metric_estimate, score_of, series_color, set_result } from './evals-model.ts';

/**
 * Panel 7. The public half: every voter's numbers on the golden set, whose cases are in the
 * repository, and the one command that produces them again.
 */
const props = defineProps<{ results: BenchResults; slots: Record<string, number | null>; level: number }>();

const golden = computed(() => set_result(props.results, props.results.reproduce.set));
const rows = computed(() =>
    props.results.voters.flatMap((voter) => {
        const score = score_of(golden.value, voter.name);
        if (!score) return [];
        const cells = GAIN_METRICS.map((metric) => {
            const estimate = metric_estimate(score, metric.key);
            return estimate ? { value: fmt_fixed(estimate.value, metric.digits), interval: fmt_interval(estimate, metric.digits) } : null;
        });
        return [{ voter, cells }];
    }),
);

const copied = ref(false);

/** The command to the clipboard. A page opened from a file has no clipboard API, so the old selection route is the fallback. */
async function copy_command(): Promise<void> {
    const text = props.results.reproduce.command;
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.append(area);
        area.select();
        document.execCommand('copy');
        area.remove();
    }
    copied.value = true;
    window.setTimeout(() => (copied.value = false), 1600);
}
</script>

<template>
    <p v-if="!golden" class="ev-empty">This file did not score the golden set.</p>
    <div v-else>
        <div class="ev-scroll">
            <table class="ev-table">
                <thead>
                    <tr>
                        <th scope="col">Voter</th>
                        <template v-for="metric in GAIN_METRICS" :key="metric.key">
                            <th scope="col" class="ev-r">{{ metric.label }}</th>
                            <th scope="col" class="ev-r">{{ level }}% interval</th>
                        </template>
                    </tr>
                </thead>
                <tbody>
                    <tr v-for="row in rows" :key="row.voter.name">
                        <th scope="row">
                            <span class="ev-voter"><i class="ev-swatch" :style="{ background: series_color(slots[row.voter.name]) }"></i>{{ row.voter.label }}</span>
                        </th>
                        <template v-for="(cell, i) in row.cells" :key="i">
                            <td class="ev-r ev-num">
                                <b>{{ cell?.value ?? '—' }}</b>
                            </td>
                            <td class="ev-r ev-num ev-sub">{{ cell?.interval ?? '—' }}</td>
                        </template>
                    </tr>
                </tbody>
            </table>
        </div>
        <p class="ev-note">Golden · {{ fmt_count(golden.n.open + golden.n.done) }} rows ({{ fmt_count(golden.n.open) }} open · {{ fmt_count(golden.n.done) }} done)</p>
        <div class="ev-command">
            <code>{{ results.reproduce.command }}</code>
            <button type="button" class="ev-copy" @click="copy_command">{{ copied ? 'COPIED' : 'COPY' }}</button>
        </div>
    </div>
</template>
