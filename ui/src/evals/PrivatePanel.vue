<script setup lang="ts">
import { computed } from 'vue';
import { BENCH_SET_PUBLIC, type BenchResults } from '@tldrack/schemas/bench-results.ts';

import { fmt_count, SET_LABEL, sets_in, set_result } from './evals-model.ts';

/** Panel 8. Which rows a reader can open, which they cannot, and what of each is in the file. */
const props = defineProps<{ results: BenchResults }>();

const rows = computed(() =>
    sets_in(props.results).map((set) => {
        const n = set_result(props.results, set)!.n;
        return {
            set,
            label: SET_LABEL[set],
            n: `${fmt_count(n.open + n.done)} (${fmt_count(n.open)} open · ${fmt_count(n.done)} done)`,
            where: BENCH_SET_PUBLIC[set] ? 'In the repository: anyone can read and re-run them' : "On the operator's machine only",
            carries: BENCH_SET_PUBLIC[set] ? 'Every number, and the cases behind them' : 'Counts, curves and bins. No row text, no row key',
        };
    }),
);
</script>

<template>
    <div class="ev-scroll">
        <table class="ev-table">
            <thead>
                <tr>
                    <th scope="col">Set</th>
                    <th scope="col">Rows</th>
                    <th scope="col">Where the rows are</th>
                    <th scope="col">What the results file carries</th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="row in rows" :key="row.set">
                    <th scope="row">
                        <b>{{ row.label }}</b>
                    </th>
                    <td class="ev-num">{{ row.n }}</td>
                    <td class="ev-wrap">{{ row.where }}</td>
                    <td class="ev-wrap">{{ row.carries }}</td>
                </tr>
            </tbody>
        </table>
    </div>
    <p class="ev-note">
        The results file is a strict schema (schemas/src/bench-results.ts): every object refuses a field it does not name, so a row cannot ride in beside the
        numbers.
    </p>
</template>
