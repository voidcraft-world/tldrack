<script setup lang="ts">
import { computed } from 'vue';
import type { LayaTrainingSnapshot } from '@tldrack/schemas/laya-review.ts';

import { fmt_share, snapshot_label, snapshot_standing, type Voter } from './training-model.ts';

/**
 * Who can be looked at: every Laya snapshot on this machine, then the voters of the bundled eval
 * results (the base model, the subscription models, the floor). A row with rows to draw is a
 * button; a snapshot no gate kept rows for is listed with its own numbers and says why it is not.
 */
const props = defineProps<{ snapshots: LayaTrainingSnapshot[]; voters: Voter[]; pick: string | null }>();
const emit = defineEmits<{ pick: [id: string] }>();

const by_snapshot = computed(() => new Map(props.voters.filter((voter) => voter.source === 'gate').map((voter) => [voter.snapshot, voter])));
const snapshot_rows = computed(() =>
    props.snapshots.map((snapshot) => ({
        snapshot,
        voter: by_snapshot.value.get(snapshot.name) ?? null,
        standing: snapshot_standing(snapshot),
        when: snapshot_label(snapshot.name),
    })),
);
const bench = (group: Voter['group']): Voter[] => props.voters.filter((voter) => voter.source === 'bench' && voter.group === group);
const GROUPS: { group: Voter['group']; head: string; note: string }[] = [
    { group: 'subscription', head: 'SUBSCRIPTION MODELS', note: 'asked the same question, zero-shot' },
    { group: 'laya', head: 'LAYA · IN THE EVAL RESULTS', note: 'the base, the fine-tune and the panels, as last benched' },
    { group: 'other', head: 'FLOORS AND ENSEMBLES', note: '' },
];
</script>

<template>
    <nav class="roster" aria-label="Models">
        <h3 class="fui-techtext roster__head">LAYA SNAPSHOTS · THIS MACHINE <span class="fui-count">{{ snapshots.length }}</span></h3>
        <p v-if="snapshots.length === 0" class="roster__empty">No fine-tune on disk yet. A retrain makes the first.</p>
        <ul class="roster__list">
            <li v-for="row in snapshot_rows" :key="row.snapshot.name">
                <button type="button" class="roster__row" :class="{ 'is-on': row.voter && pick === row.voter.id }" :disabled="!row.voter"
                    :aria-pressed="!!row.voter && pick === row.voter.id"
                    :title="row.voter ? row.snapshot.name : `${row.snapshot.name} · no promotion gate kept this snapshot's rows, so there are no points to draw`"
                    @click="row.voter && emit('pick', row.voter.id)">
                    <span class="roster__name">{{ row.when }}</span>
                    <span class="fui-tag" :class="{ 'fui-tag--ok': row.standing.tone === 'ok', 'fui-tag--warn': row.standing.tone === 'warn', 'roster__tag--dim': row.standing.tone === 'idle' }">{{ row.standing.word }}</span>
                    <span class="roster__sub">
                        <template v-if="row.snapshot.eval">held-out {{ fmt_share(row.snapshot.eval.before.accuracy) }} → <b>{{ fmt_share(row.snapshot.eval.after.accuracy) }}</b> · {{ row.snapshot.eval.items }} rows</template>
                        <template v-else>no train report</template>
                        <template v-if="!row.voter"> · no points kept</template>
                    </span>
                </button>
            </li>
        </ul>
        <template v-for="section in GROUPS" :key="section.group">
            <template v-if="bench(section.group).length">
                <h3 class="fui-techtext roster__head">{{ section.head }}</h3>
                <p v-if="section.note" class="roster__note">{{ section.note }}</p>
                <ul class="roster__list">
                    <li v-for="voter in bench(section.group)" :key="voter.id">
                        <button type="button" class="roster__row" :class="{ 'is-on': pick === voter.id }" :aria-pressed="pick === voter.id" @click="emit('pick', voter.id)">
                            <span class="roster__name">{{ voter.label }}</span>
                            <span v-if="voter.served" class="fui-tag fui-tag--ok">SERVED</span>
                        </button>
                    </li>
                </ul>
            </template>
        </template>
    </nav>
</template>

<style scoped>
.roster { min-width: 0; }
.roster__head { margin: 14px 0 6px; font-size: 0.68rem; color: var(--fui-text-dim); display: flex; gap: 8px; align-items: baseline; }
.roster__head:first-child { margin-top: 0; }
.roster__note, .roster__empty { margin: 0 0 6px; font-size: 0.72rem; color: var(--fui-text-dim); }
.roster__list { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.roster__row {
    width: 100%; display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; align-items: center; text-align: left;
    padding: 7px 10px; color: var(--fui-text); background: transparent; cursor: pointer;
    border: 1px solid rgba(157, 218, 255, 0.16); border-left: 3px solid transparent; border-radius: 3px; font: inherit;
}
.roster__row:hover:not(:disabled) { border-color: rgba(157, 218, 255, 0.5); }
.roster__row:focus-visible { outline: 2px solid var(--fui-slate); outline-offset: 1px; }
.roster__row.is-on { border-color: rgba(147, 255, 212, 0.6); border-left-color: var(--color-accent-green); background: rgba(147, 255, 212, 0.07); }
.roster__row:disabled { cursor: default; opacity: 0.62; }
.roster__name { font: 600 0.8rem var(--fui-mono); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.roster__sub { grid-column: 1 / -1; font-size: 0.72rem; color: var(--fui-text-dim); }
.roster__sub b { color: var(--fui-text); }
.roster__tag--dim { color: var(--fui-text-dim); border-color: rgba(174, 169, 191, 0.4); }
</style>
