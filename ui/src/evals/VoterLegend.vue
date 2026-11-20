<script setup lang="ts">
import { computed } from 'vue';

import { rest_head, type ScoredVoter } from './evals-model.ts';

/**
 * A chart's legend, as a table: one row per voter with its colour key and the chart's own
 * numbers, so every value is readable without a hover. A row is a button: it picks the voter
 * every chart draws bold. The voters with no colour of their own (the ensembles, and any model
 * past the eighth) sit in a closed second table, so a run with fifteen voters still has a legend
 * the size of its chart.
 */
const props = defineProps<{
    scored: ScoredVoter[];
    /** A column: its head, each voter's value, and optionally a second line under the value (its interval). */
    columns: { head: string; cells: Record<string, string>; under?: Record<string, string> }[];
}>();
defineEmits<{ focus: [name: string] }>();

const groups = computed(() => {
    const rest = props.scored.filter((entry) => entry.rest);
    return { main: props.scored.filter((entry) => !entry.rest), rest, rest_head: rest_head(rest) };
});
</script>

<template>
    <div class="ev-scroll">
        <table class="ev-table ev-legend">
            <thead>
                <tr>
                    <th scope="col">Voter</th>
                    <th v-for="column in columns" :key="column.head" scope="col" class="ev-r">{{ column.head }}</th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="{ voter, color, focus } in groups.main" :key="voter.name" :data-focus="focus">
                    <th scope="row">
                        <button type="button" :aria-pressed="focus" :title="`Draw ${voter.label} bold`" @click="$emit('focus', voter.name)">
                            <span class="ev-legend__name"><i class="ev-swatch" :style="{ background: color }"></i>{{ voter.label }}</span>
                            <span v-if="voter.served" class="ev-tag" title="The model the rack serves">SERVED</span>
                        </button>
                    </th>
                    <td v-for="column in columns" :key="column.head" class="ev-r ev-num">
                        {{ column.cells[voter.name] ?? '—' }}
                        <span v-if="column.under?.[voter.name]" class="ev-legend__under">{{ column.under[voter.name] }}</span>
                    </td>
                </tr>
            </tbody>
        </table>
    </div>
    <details v-if="groups.rest.length > 0" class="ev-details">
        <summary>{{ groups.rest_head }}</summary>
        <div class="ev-details__body">
            <table class="ev-table ev-legend">
                <tbody>
                    <tr v-for="{ voter, color } in groups.rest" :key="voter.name">
                        <th scope="row">
                            <button type="button" aria-pressed="false" :title="`Draw ${voter.label} bold`" @click="$emit('focus', voter.name)">
                                <span class="ev-legend__name"><i class="ev-swatch" :style="{ background: color }"></i>{{ voter.label }}</span>
                            </button>
                        </th>
                        <td v-for="column in columns" :key="column.head" class="ev-r ev-num">
                            {{ column.cells[voter.name] ?? '—' }}
                            <span v-if="column.under?.[voter.name]" class="ev-legend__under">{{ column.under[voter.name] }}</span>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </details>
</template>
