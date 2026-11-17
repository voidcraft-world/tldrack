<script setup lang="ts">
import { computed } from 'vue';
import type { BenchSetResult, BenchVoter } from '@tldrack/schemas/bench-results.ts';

import { fmt_labels, fmt_ms, fmt_params, KIND_LABEL, score_of, series_color } from './evals-model.ts';

/** Panel 6. Every voter in the file, once: what it is, how big, where it ran, and its licence. */
const props = defineProps<{ result: BenchSetResult; voters: BenchVoter[]; slots: Record<string, number | null> }>();

const rows = computed(() =>
    props.voters.map((voter) => {
        const of = props.voters.find((other) => other.name === voter.of);
        const latency = score_of(props.result, voter.name)?.latency;
        let from = '—';
        // An ensemble's label already names its members; `of` is only the first of them.
        if (of && voter.kind === 'fine_tune') from = `${of.label} + ${fmt_labels(voter.labels ?? 0)}`;
        else if (of && voter.kind !== 'ensemble') from = of.label;
        return {
            voter,
            from,
            size: voter.params_m === null ? '—' : fmt_params(voter.params_m),
            runtime: voter.runtime === 'none' ? '—' : voter.runtime,
            latency: latency ? `${fmt_ms(latency.p50_ms)} · ${fmt_ms(latency.p90_ms)}` : '—',
        };
    }),
);
</script>

<template>
    <div class="ev-scroll">
        <table class="ev-table">
            <thead>
                <tr>
                    <th scope="col">Voter</th>
                    <th scope="col">Kind</th>
                    <th scope="col">Built from</th>
                    <th scope="col" class="ev-r">Parameters</th>
                    <th scope="col">Runtime</th>
                    <th scope="col" class="ev-r">Latency p50 · p90</th>
                    <th scope="col">Licence</th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="row in rows" :key="row.voter.name">
                    <th scope="row">
                        <span class="ev-voter">
                            <i class="ev-swatch" :style="{ background: series_color(slots[row.voter.name]) }"></i>{{ row.voter.label }}
                            <span v-if="row.voter.served" class="ev-tag" title="The model the rack serves">SERVED</span>
                        </span>
                    </th>
                    <td>{{ KIND_LABEL[row.voter.kind] }}</td>
                    <td class="ev-wrap">{{ row.from }}</td>
                    <td class="ev-r ev-num">{{ row.size }}</td>
                    <td>{{ row.runtime }}</td>
                    <td class="ev-r ev-num">{{ row.latency }}</td>
                    <td>{{ row.voter.licence ?? '—' }}</td>
                </tr>
            </tbody>
        </table>
    </div>
</template>
