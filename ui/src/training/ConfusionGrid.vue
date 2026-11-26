<script setup lang="ts">
import { computed } from 'vue';

import { auc_of, confusion_at, cost_of, fmt_change, fmt_p, fmt_share, rates_of, type ScoredSet } from './training-model.ts';

/**
 * The quadrant: the same rows counted at the cut, the pick's count large and its change against the
 * baseline beside it. Then the rates the four counts make, pick and baseline side by side.
 */
const props = defineProps<{
    rows: ScoredSet;
    cut: number;
    label: string;
    baseline: ScoredSet | null;
    baseline_cut: number | null;
    baseline_label: string | null;
    false_warning: number;
}>();

const mine = computed(() => confusion_at(props.rows, props.cut));
const theirs = computed(() => (props.baseline && props.baseline_cut !== null ? confusion_at(props.baseline, props.baseline_cut) : null));

type Cell = { key: 'tp' | 'fn' | 'fp' | 'tn'; name: string; code: string; says: string; good: boolean };
const CELLS: Cell[] = [
    { key: 'tp', name: 'CAUGHT', code: 'true positive', says: 'open work, drew ⚠️', good: true },
    { key: 'fn', name: 'MISSED', code: 'false negative', says: 'open work, no ⚠️', good: false },
    { key: 'fp', name: 'FALSE ⚠️', code: 'false positive', says: 'done, drew ⚠️', good: false },
    { key: 'tn', name: 'QUIET', code: 'true negative', says: 'done, no ⚠️', good: true },
];
const cells = computed(() =>
    CELLS.map((cell) => {
        const delta = theirs.value ? mine.value[cell.key] - theirs.value[cell.key] : null;
        return { ...cell, count: mine.value[cell.key], delta, better: delta === null || delta === 0 ? null : delta > 0 === cell.good };
    }),
);

const rows_of = (c: { tp: number; fp: number; fn: number; tn: number }, rows: ScoredSet) => {
    const rates = rates_of(c);
    return [fmt_share(rates.accuracy), fmt_share(rates.precision), fmt_share(rates.recall), fmt_share(rates.false_rate), String(cost_of(c, props.false_warning)), fmt_p(auc_of(rows), 3)];
};
const HEADS = [
    { name: 'ACCURACY', title: 'Rows it got right' },
    { name: 'PRECISION', title: 'Of the ⚠️ it drew, the share that were open work' },
    { name: 'RECALL', title: 'Of the open work, the share it drew a ⚠️ on' },
    { name: 'FALSE ⚠️ RATE', title: 'Of the done turns, the share that drew a ⚠️' },
    { name: 'COST', title: 'False ⚠️ times their weight, plus missed open turns: lower is better' },
    { name: 'AUC', title: 'How well it ranks open above done, at no cut: 0.5 is a coin, 1 is perfect' },
];
const table = computed(() => ({
    mine: rows_of(mine.value, props.rows),
    theirs: theirs.value && props.baseline ? rows_of(theirs.value, props.baseline) : null,
}));
</script>

<template>
    <div class="quad">
        <div class="quad__grid" role="table" aria-label="Confusion at the cut">
            <span class="quad__corner"></span>
            <span class="quad__col fui-techtext">DREW ⚠️</span>
            <span class="quad__col fui-techtext">NO ⚠️</span>
            <span class="quad__row fui-techtext">OPEN WORK</span>
            <div v-for="cell in cells.slice(0, 2)" :key="cell.key" class="quad__cell" :class="cell.good ? 'quad__cell--good' : 'quad__cell--bad'" :title="`${cell.code}: ${cell.says}`">
                <span class="quad__name fui-techtext">{{ cell.name }}</span>
                <strong class="quad__count">{{ cell.count }}</strong>
                <span v-if="cell.delta !== null" class="quad__delta" :class="{ 'quad__delta--up': cell.better === true, 'quad__delta--down': cell.better === false }">{{ fmt_change(cell.delta) }}</span>
                <span class="quad__code">{{ cell.code }}</span>
            </div>
            <span class="quad__row fui-techtext">DONE</span>
            <div v-for="cell in cells.slice(2)" :key="cell.key" class="quad__cell" :class="cell.good ? 'quad__cell--good' : 'quad__cell--bad'" :title="`${cell.code}: ${cell.says}`">
                <span class="quad__name fui-techtext">{{ cell.name }}</span>
                <strong class="quad__count">{{ cell.count }}</strong>
                <span v-if="cell.delta !== null" class="quad__delta" :class="{ 'quad__delta--up': cell.better === true, 'quad__delta--down': cell.better === false }">{{ fmt_change(cell.delta) }}</span>
                <span class="quad__code">{{ cell.code }}</span>
            </div>
        </div>
        <p v-if="theirs" class="quad__note">± is against {{ baseline_label }} at its own cut {{ baseline_cut?.toFixed(2) }}, on the same rows.</p>
        <table class="quad__rates">
            <thead>
                <tr>
                    <th></th>
                    <th class="quad__who">{{ label }} <small>@ {{ cut.toFixed(2) }}</small></th>
                    <th v-if="table.theirs" class="quad__who quad__base">{{ baseline_label }} <small>@ {{ baseline_cut?.toFixed(2) }}</small></th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="(head, i) in HEADS" :key="head.name" :title="head.title">
                    <th class="fui-techtext quad__metric">{{ head.name }}</th>
                    <td>{{ table.mine[i] }}</td>
                    <td v-if="table.theirs" class="quad__base">{{ table.theirs[i] }}</td>
                </tr>
            </tbody>
        </table>
    </div>
</template>

<style scoped>
.quad { min-width: 0; }
.quad__grid { display: grid; grid-template-columns: auto 1fr 1fr; gap: 6px; align-items: stretch; }
.quad__col, .quad__row { font-size: 0.68rem; color: var(--fui-text-dim); }
.quad__col { text-align: center; padding-bottom: 2px; }
.quad__row { writing-mode: vertical-rl; transform: rotate(180deg); text-align: center; }
.quad__cell { position: relative; display: grid; grid-template-columns: 1fr auto; align-items: baseline; gap: 0 8px; padding: 10px 12px; border: 1px solid; border-radius: 3px; min-width: 0; }
.quad__cell--good { border-color: rgba(147, 255, 212, 0.4); background: rgba(147, 255, 212, 0.06); }
.quad__cell--bad { border-color: rgba(248, 193, 70, 0.4); background: rgba(248, 193, 70, 0.06); }
.quad__name { grid-column: 1 / -1; font-size: 0.68rem; }
.quad__cell--good .quad__name { color: var(--color-accent-green); }
.quad__cell--bad .quad__name { color: var(--fui-warn); }
.quad__count { font: 600 1.9rem/1.1 var(--fui-mono); color: var(--fui-text); font-variant-numeric: tabular-nums; }
.quad__delta { font: 600 0.8rem var(--fui-mono); color: var(--fui-text-dim); }
.quad__delta--up { color: var(--color-accent-green); }
.quad__delta--down { color: var(--fui-warn); }
.quad__code { grid-column: 1 / -1; font-size: 0.68rem; color: var(--fui-text-dim); }
.quad__note { margin: 6px 0 0; font-size: 0.72rem; color: var(--fui-text-dim); }
.quad__rates { width: 100%; margin-top: 12px; border-collapse: collapse; font: 0.78rem var(--fui-mono); font-variant-numeric: tabular-nums; }
.quad__rates th, .quad__rates td { padding: 5px 6px; text-align: right; border-top: 1px solid rgba(157, 218, 255, 0.12); }
.quad__rates thead th { border-top: 0; }
.quad__rates tbody tr { cursor: help; }
.quad__rates { table-layout: fixed; }
.quad__who { color: var(--fui-text); font-weight: 600; overflow-wrap: anywhere; }
.quad__who small { display: block; color: var(--fui-text-dim); font-weight: 400; }
.quad__metric { text-align: left !important; font-size: 0.64rem; font-weight: 600; color: var(--fui-text-dim); }
.quad__rates td { color: var(--fui-text); }
.quad__rates .quad__base { color: var(--fui-text-dim); }
</style>
