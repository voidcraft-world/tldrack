<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchSet } from '@tldrack/schemas/bench-results.ts';

import CostChart from './CostChart.vue';
import EvalsPanel from './EvalsPanel.vue';
import LabelsBuy from './LabelsBuy.vue';
import PrivatePanel from './PrivatePanel.vue';
import ReadLess from './ReadLess.vue';
import ReliabilityChart from './ReliabilityChart.vue';
import ReproducePanel from './ReproducePanel.vue';
import RocChart from './RocChart.vue';
import VoterRoster from './VoterRoster.vue';
import { default_focus, fmt_count, fmt_day, headline_set, label_gains, parse_results, SET_LABEL, set_result, sets_in, voter_slots } from './evals-model.ts';

/**
 * The EVALS page: one results file, eight panels (seven when it withholds its fleet block), in the order a reader asks. `file` is the
 * file's name and `raw` its JSON, untrusted until `parse_results` has read it.
 */
const props = defineProps<{ file: string | null; raw: unknown }>();

const parsed = computed(() => (props.file === null ? null : parse_results(props.raw)));
const results = computed(() => (parsed.value?.ok ? parsed.value.results : null));

// The two things a reader picks: the set the charts are on, and the voter they draw bold.
const picked_set = ref<BenchSet | null>(null);
const picked_focus = ref<string | null>(null);

const view = computed(() => {
    const file = results.value;
    if (!file) return null;
    const sets = sets_in(file);
    const set = picked_set.value !== null && sets.includes(picked_set.value) ? picked_set.value : headline_set(file);
    const result = set_result(file, set)!;
    return {
        file,
        sets: sets.map((name) => ({ name, label: SET_LABEL[name], rows: set_result(file, name)!.n })),
        set,
        set_label: SET_LABEL[set],
        result,
        rows: result.n.open + result.n.done,
        slots: voter_slots(file.voters),
        focus: picked_focus.value ?? default_focus(file.voters),
        gains: label_gains(file, set),
        fixture: file.fixture === true,
        level: Math.round(file.protocol.interval.level * 100),
        // A file that withholds its fleet block (the operator's own use, opt-in) draws no Read less panel; the rest close up.
        shift: file.fleet ? 0 : 1,
    };
});
const focus_label = computed(() => view.value?.file.voters.find((voter) => voter.name === view.value?.focus)?.label ?? '');
</script>

<template>
    <main class="ev-page">
        <p class="ev-eyebrow">tldrack · evals</p>
        <h1 class="ev-title">Does a finished turn need you?</h1>
        <p class="ev-lede">
            Laya is the small local model that reads the end of every agent turn and decides whether to show the operator a ⚠️. This page is how well it
            decides, measured under one protocol.
        </p>

        <section v-if="file === null" class="ev-refused">
            <strong>No results file.</strong>
            <p class="ev-empty">The build found nothing in bench/results/.</p>
        </section>

        <section v-else-if="parsed && !parsed.ok" class="ev-refused">
            <strong>{{ file }} is not a results file this page can read.</strong>
            <ul>
                <li v-for="issue in parsed.issues" :key="issue">{{ issue }}</li>
            </ul>
        </section>

        <template v-else-if="view">
            <p class="ev-lede">{{ view.file.protocol.description }}</p>
            <ul class="ev-provenance">
                <li><b>protocol</b> {{ view.file.protocol.id }}</li>
                <li><b>surface</b> {{ view.file.protocol.surface }}</li>
                <li><b>question</b> {{ view.file.protocol.question }} · v{{ view.file.protocol.question_version }}</li>
                <li><b>interval</b> {{ view.level }}% bootstrap, {{ fmt_count(view.file.protocol.interval.resamples) }} resamples</li>
                <li><b>run</b> {{ fmt_day(view.file.generated_at) }}</li>
                <li v-if="view.file.commit"><b>commit</b> {{ view.file.commit.slice(0, 7) }}</li>
                <li><b>file</b> bench/results/{{ file }}</li>
            </ul>
            <p v-if="view.fixture" class="ev-banner" role="note">
                <span aria-hidden="true">⚠️</span>
                <span><strong>These numbers are invented.</strong> No model produced them: they are a seeded stand-in this page was built against, and a real bench run replaces them.</span>
            </p>

            <div class="ev-panels">
                <EvalsPanel v-if="view.file.fleet" :num="1" figure="read-less" title="Read less" :fixture="view.fixture" span>
                    <ReadLess :fleet="view.file.fleet" />
                </EvalsPanel>

                <div v-if="view.sets.length > 1" class="ev-filter ev-span">
                    <span id="ev-set-label" class="ev-filter__label">Set</span>
                    <div class="ev-seg" role="group" aria-labelledby="ev-set-label">
                        <button v-for="set in view.sets" :key="set.name" type="button" :aria-pressed="set.name === view.set" @click="picked_set = set.name">
                            {{ set.label }} · {{ fmt_count(set.rows.open + set.rows.done) }} rows
                        </button>
                    </div>
                    <span class="ev-filter__note">Panels {{ 2 - view.shift }} to {{ 6 - view.shift }} are on this set.</span>
                </div>

                <EvalsPanel
                    :num="2 - view.shift"
                    figure="labels-buy"
                    title="What your labels buy"
                    :lede="`${view.set_label} · ${fmt_count(view.rows)} rows (${fmt_count(view.result.n.open)} open · ${fmt_count(view.result.n.done)} done). The same rows, before and after the fine-tune.`"
                    :fixture="view.fixture"
                    span
                >
                    <LabelsBuy :gains="view.gains" :slots="view.slots" :level="view.level" />
                </EvalsPanel>

                <EvalsPanel :num="3 - view.shift" figure="roc" title="ROC" :lede="`${view.set_label} · ${fmt_count(view.rows)} rows. Up and to the left is better: more open turns flagged for fewer false ⚠️.`" :fixture="view.fixture">
                    <RocChart :result="view.result" :voters="view.file.voters" :slots="view.slots" :focus="view.focus" :set_label="view.set_label" :level="view.level" @focus="picked_focus = $event" />
                </EvalsPanel>

                <EvalsPanel :num="4 - view.shift" figure="reliability" title="Reliability" :lede="`${view.set_label} · ${fmt_count(view.rows)} rows. On the line, a p of 0.8 comes true 8 times in 10. ECE is the mean distance from it.`" :fixture="view.fixture">
                    <ReliabilityChart :result="view.result" :voters="view.file.voters" :slots="view.slots" :focus="view.focus" :set_label="view.set_label" :level="view.level" @focus="picked_focus = $event" />
                </EvalsPanel>

                <EvalsPanel
                    :num="5 - view.shift"
                    figure="cost"
                    title="Cost against threshold"
                    :lede="`${focus_label} on ${view.set_label}. Raise the cut and fewer ⚠️ are false, but more open work is missed. A voter picked in a legend above is the one drawn here.`"
                    :fixture="view.fixture"
                    span
                >
                    <CostChart :result="view.result" :voters="view.file.voters" :slots="view.slots" :focus="view.focus" :set_label="view.set_label" :costs="view.file.protocol.costs" :level="view.level" />
                </EvalsPanel>

                <EvalsPanel :num="6 - view.shift" figure="roster" title="The roster" :lede="`Every voter this file scored. Latency is per call, warm, on ${view.set_label}.`" :fixture="view.fixture" span>
                    <VoterRoster :result="view.result" :voters="view.file.voters" :slots="view.slots" />
                </EvalsPanel>

                <EvalsPanel :num="7 - view.shift" figure="reproduce" title="Reproduce it" lede="The golden set is public: its cases are in the repository, and one command scores them again." :fixture="view.fixture" span>
                    <ReproducePanel :results="view.file" :slots="view.slots" :level="view.level" />
                </EvalsPanel>

                <EvalsPanel :num="8 - view.shift" figure="private" title="What is private" lede="The held-out rows are the operator's own turn ends, graded by hand and never trained on. Only aggregates of them leave the machine." :fixture="view.fixture" span>
                    <PrivatePanel :results="view.file" />
                </EvalsPanel>
            </div>
        </template>
    </main>
</template>
