<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { LayaOpsState, LayaTrainingState } from '@tldrack/schemas/laya-review.ts';

import { EVALS_PAGE, RACK_PAGE, REVIEW_PAGE } from '../config/pages.ts';
import { fail_text, load_laya_ops, load_laya_training, post_laya_lab, type LayaFail } from '../decide/laya-review-client.ts';
import { parse_results } from '../evals/evals-model.ts';
import ConfusionGrid from './ConfusionGrid.vue';
import ModelRoster from './ModelRoster.vue';
import PairedScatter from './PairedScatter.vue';
import RetrainPanel from './RetrainPanel.vue';
import RocPlot from './RocPlot.vue';
import ScoreStrip from './ScoreStrip.vue';
import {
    baseline_of,
    cheapest_cut,
    default_pick,
    paired_rows,
    SET_LABELS,
    SET_TITLES,
    set_of,
    snapshot_label,
    voters_from_bench,
    voters_from_evidence,
    type SetName,
} from './training-model.ts';

/**
 * Laya training: kick off the gated retrain, watch it, and read any model's results from its rows.
 * The two reads (`/ops` for the retrain, the queue and the label count, `/training` for the
 * snapshots and their rows) are polled; nothing is kept in the page, so a reload shows the same.
 */
const props = defineProps<{ bench: unknown }>();

const SURFACE = 'agent_open_work';
/** A retrain's journal moves every few micro-batches; an idle page only needs to notice a scheduled run. */
const POLL_BUSY_MS = 4000;
const POLL_IDLE_MS = 20_000;

const ops = ref<LayaOpsState | null>(null);
const state = ref<LayaTrainingState | null>(null);
const ops_fail = ref<LayaFail | null>(null);
const state_fail = ref<LayaFail | null>(null);
const now = ref(Date.now());

const row = computed(() => ops.value?.retrain?.find((entry) => entry.surface === SURFACE) ?? null);
const job = computed(() => ops.value?.jobs.find((entry) => entry.verb === 'retrain' && entry.surface === SURFACE) ?? null);
const ready = computed(() => ops.value?.review.find((entry) => entry.surface === SURFACE)?.sweep_ready ?? null);
const busy = computed(() => job.value?.state === 'working' || job.value?.state === 'queued');

const parsed = computed(() => parse_results(props.bench));
const false_warning = computed(() => (parsed.value.ok ? parsed.value.results.protocol.costs.false_warning : 2));
const voters = computed(() => [...(state.value ? voters_from_evidence(state.value) : []), ...(parsed.value.ok ? voters_from_bench(parsed.value.results) : [])]);

const pick_id = ref<string | null>(null);
const set_name = ref<SetName>('heldout');
const cut = ref(0.7);
const pick = computed(() => voters.value.find((voter) => voter.id === pick_id.value) ?? null);
const baseline = computed(() => (pick.value ? baseline_of(pick.value, voters.value) : null));
const sets = computed(() => pick.value?.sets.map((block) => block.set) ?? []);
const rows = computed(() => set_of(pick.value, set_name.value));
const baseline_rows = computed(() => set_of(baseline.value, set_name.value));
const pairs = computed(() => paired_rows(state.value, pick.value, set_name.value));

watch(voters, (list) => {
    if (!list.some((voter) => voter.id === pick_id.value)) pick_id.value = default_pick(list);
});
watch(pick, (voter) => {
    if (!voter) return;
    cut.value = voter.cut;
    if (!voter.sets.some((block) => block.set === set_name.value)) set_name.value = voter.sets[0]!.set;
});

async function refresh(): Promise<void> {
    const [got_ops, got_state] = await Promise.all([load_laya_ops(), load_laya_training(SURFACE)]);
    if (got_ops.ok) ops.value = got_ops.data;
    ops_fail.value = got_ops.ok ? null : got_ops;
    if (got_state.ok) state.value = got_state.data;
    state_fail.value = got_state.ok ? null : got_state;
}

let poll: ReturnType<typeof setTimeout> | null = null;
let tick: ReturnType<typeof setInterval> | null = null;
function schedule(): void {
    if (poll) clearTimeout(poll);
    poll = setTimeout(async () => {
        if (document.visibilityState === 'visible') await refresh();
        schedule();
    }, busy.value || row.value?.state === 'running' ? POLL_BUSY_MS : POLL_IDLE_MS);
}
function on_visible(): void {
    if (document.visibilityState === 'visible') void refresh();
}
onMounted(async () => {
    await refresh();
    schedule();
    tick = setInterval(() => (now.value = Date.now()), 1000);
    document.addEventListener('visibilitychange', on_visible);
});
onBeforeUnmount(() => {
    if (poll) clearTimeout(poll);
    if (tick) clearInterval(tick);
    document.removeEventListener('visibilitychange', on_visible);
});

const pressing = ref(false);
const notice = ref<string | null>(null);
const notice_bad = ref(false);
async function press(): Promise<void> {
    pressing.value = true;
    const sent = await post_laya_lab('retrain', SURFACE);
    pressing.value = false;
    notice_bad.value = !sent.ok;
    notice.value = sent.ok ? sent.data.message : fail_text(sent);
    await refresh();
    schedule();
}

const page = (name: string): string => new URL(name, window.location.href).href;
</script>

<template>
    <div class="nv-board train">
        <header class="nv-head fui-panel train__head">
            <span class="fui-corner tl"></span><span class="fui-corner br"></span>
            <strong class="fui-techtext train__title">LAYA TRAINING</strong>
            <span class="fui-tag">OPEN WORK</span>
            <span class="train__grow"></span>
            <a class="board-btn" :href="page(REVIEW_PAGE)" title="Grade Laya's verdicts: every grade is a label the next retrain or sweep can spend"><span class="fui-corner tl"></span><span class="fui-corner br"></span>REVIEW</a>
            <a class="board-btn" :href="page(EVALS_PAGE)" title="The published eval results"><span class="fui-corner tl"></span><span class="fui-corner br"></span>EVALS</a>
            <a class="board-btn" :href="page(RACK_PAGE)" title="Back to the rack"><span class="fui-corner tl"></span><span class="fui-corner br"></span>RACK</a>
        </header>

        <p v-if="ops_fail" class="fui-callout fui-callout--warn" role="alert">{{ fail_text(ops_fail) }}</p>
        <RetrainPanel v-else :row="row" :job="job" :ready="ready" :last_run="state?.last_run ?? null" :now="now" :pressing="pressing" :notice="notice" :notice_bad="notice_bad" @press="press" />

        <p v-if="state_fail && !ops_fail" class="fui-callout fui-callout--warn" role="status">
            {{ state_fail.kind === 'restart' ? 'The running bridge predates the training page, so the snapshots on this machine are not listed yet. Restart the bridge to see them. The eval results below are bundled with the page.' : fail_text(state_fail) }}
        </p>

        <div class="train__body">
            <aside class="fui-panel train__side">
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                <ModelRoster :snapshots="state?.snapshots ?? []" :voters="voters" :pick="pick_id" @pick="pick_id = $event" />
            </aside>

            <main class="fui-panel train__main">
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                <p v-if="!pick || !rows" class="train__empty">Pick a model on the left to see its rows.</p>
                <template v-else>
                    <div class="train__pick">
                        <h2 class="train__name">{{ pick.label }}</h2>
                        <span v-if="pick.served" class="fui-tag fui-tag--ok">SERVED</span>
                        <span v-if="pick.verdict" class="fui-tag" :class="pick.verdict === 'PASS' ? 'fui-tag--ok' : 'fui-tag--warn'">GATE {{ pick.verdict }}</span>
                        <span class="train__vs">{{ baseline ? `against ${baseline.label}, the served model, on the same rows` : 'the served model: nothing to compare it with here' }}</span>
                        <span class="train__grow"></span>
                        <div class="train__sets" role="group" aria-label="Set">
                            <button v-for="name in sets" :key="name" type="button" class="board-btn" :class="{ 'is-on': set_name === name }" :aria-pressed="set_name === name" :title="SET_TITLES[name]" @click="set_name = name">
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ SET_LABELS[name] }}
                            </button>
                        </div>
                    </div>
                    <p v-if="pick.why" class="train__why">{{ pick.why }}</p>
                    <p class="train__source">
                        {{ pick.source === 'gate' ? `Rows a promotion gate scored${pick.at ? ` on ${pick.at.slice(0, 10)}` : ''}: ${rows.open.length + rows.done.length} that neither model trained on.` : `Rows read back off the eval results of ${pick.at?.slice(0, 10) ?? 'the bundle'}: ${rows.open.length + rows.done.length} rows.` }}
                    </p>

                    <section class="train__block">
                        <h3 class="fui-techtext train__h">EVERY ROW, AT ITS PROBABILITY</h3>
                        <ScoreStrip :rows="rows" :cut="cut" :baseline_cut="null" @cut="cut = $event" />
                        <div class="train__cuts">
                            <button type="button" class="board-btn" title="The cut this model was measured at" @click="cut = pick.cut"><span class="fui-corner tl"></span><span class="fui-corner br"></span>ITS CUT {{ pick.cut.toFixed(2) }}</button>
                            <button type="button" class="board-btn" :title="`The cheapest cut on these rows, a false ⚠️ weighing ${false_warning} missed turns. Read on the rows it is chosen on, so it flatters the model.`" @click="cut = cheapest_cut(rows, false_warning)"><span class="fui-corner tl"></span><span class="fui-corner br"></span>CHEAPEST CUT</button>
                        </div>
                    </section>

                    <div class="train__two">
                        <section class="train__block">
                            <h3 class="fui-techtext train__h">THE QUADRANT AT THAT CUT</h3>
                            <ConfusionGrid :rows="rows" :cut="cut" :label="pick.label" :baseline="baseline_rows" :baseline_cut="baseline?.cut ?? null" :baseline_label="baseline?.label ?? null" :false_warning="false_warning" />
                        </section>
                        <section class="train__block">
                            <h3 class="fui-techtext train__h">ROC · EVERY CUT AT ONCE</h3>
                            <RocPlot :rows="rows" :cut="cut" :label="pick.label" :baseline="baseline_rows" :baseline_cut="baseline?.cut ?? null" :baseline_label="baseline?.label ?? null" />
                        </section>
                        <section v-if="pairs" class="train__block">
                            <h3 class="fui-techtext train__h">ROW BY ROW · WHAT MOVED</h3>
                            <PairedScatter :rows="pairs" :cut="cut" :label="pick.label" :served_label="snapshot_label(pairs.served_name)" />
                        </section>
                    </div>
                </template>
            </main>
        </div>
    </div>
</template>

<style scoped>
.train { --fui-accent: rgba(157, 218, 255, 0.9); }
.train__head { position: relative; }
.train__title { font-size: 0.95rem; color: var(--fui-text); }
.train__grow { flex: 1 1 auto; }
.train a.board-btn { text-decoration: none; }
.train :deep(.board-btn.is-on) { background: color-mix(in srgb, var(--fui-accent) 20%, transparent); border-color: var(--fui-accent); }
.train__body { display: grid; grid-template-columns: minmax(250px, 320px) minmax(0, 1fr); gap: 12px; align-items: start; }
.train__side, .train__main { position: relative; padding: 14px 16px; min-width: 0; }
.train__side { max-height: calc(100vh - 28px); overflow: auto; position: sticky; top: 14px; }
.train__empty { margin: 0; color: var(--fui-text-dim); }
.train__pick { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.train__name { margin: 0; font: 600 1.15rem var(--fui-mono); color: var(--fui-text); }
.train__vs, .train__source, .train__why { font-size: 0.8rem; color: var(--fui-text-dim); }
.train__source, .train__why { margin: 6px 0 0; }
.train__why { color: var(--fui-warn); }
.train__sets, .train__cuts { display: flex; gap: 6px; flex-wrap: wrap; }
.train__cuts { margin-top: 8px; }
.train__block { margin-top: 18px; min-width: 0; }
.train__h { margin: 0 0 8px; font-size: 0.7rem; color: var(--fui-slate); }
.train__two { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 0 28px; }
@media (max-width: 900px) {
    .train__body { grid-template-columns: 1fr; }
    .train__side { position: static; max-height: none; }
}
</style>
