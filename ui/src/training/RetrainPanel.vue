<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import type { LayaOpsState, LayaRetrainRow, LayaTrainingState } from '@tldrack/schemas/laya-review.ts';

import { cycle_clock, fit_seconds_left, fmt_duration, fmt_share, retrain_view, running_seconds, snapshot_label } from './training-model.ts';

/**
 * The gated retrain: one press, then its eight steps as the journal records them. The journal is on
 * disk, so the bar is the same after a reload, a closed tab or a bridge restart. What a step does and
 * what the press may never do: owned by lab/laya_lab/retrain.py and plugins/laya/laya_bridge/lab_jobs.py
 * — see the notes there.
 */
const props = defineProps<{
    row: LayaRetrainRow | null;
    job: LayaOpsState['jobs'][number] | null;
    ready: { rows: number; open: number; done: number; ready: boolean; reason: string | null; reserve?: { rows: number } } | null;
    last_run: LayaTrainingState['last_run'];
    now: number;
    pressing: boolean;
    notice: string | null;
    notice_bad: boolean;
}>();
const emit = defineEmits<{ press: [] }>();

const view = computed(() => retrain_view(props.row, props.job));
/** Every duration below is read at this moment: owned by training-model.ts `cycle_clock` — see the note there. */
const clock = computed(() => cycle_clock(props.row, view.value.busy, props.now));
const stopped_at = computed(() => (!view.value.busy && props.row?.updated_at ? new Date(props.row.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null));
const blocked = computed(() => (props.ready && !props.ready.ready && view.value.action === 'start' ? props.ready.reason ?? 'Not enough clean labels yet' : null));

// An armed press: the first arms it for a few seconds, the second starts an hour of GPU.
const ARM_MS = 5000;
const armed = ref(false);
let disarm: ReturnType<typeof setTimeout> | null = null;
function press(): void {
    if (armed.value) {
        armed.value = false;
        if (disarm) clearTimeout(disarm);
        emit('press');
        return;
    }
    armed.value = true;
    disarm = setTimeout(() => (armed.value = false), ARM_MS);
}
onBeforeUnmount(() => disarm && clearTimeout(disarm));

const STEP_SAYS: Record<string, string> = {
    export: 'new turn ends into the export',
    label: 'the teacher labels them',
    build: 'the training set: your answers win, the reserve stays out',
    train: 'the fit, on the GPU',
    gate: 'the promotion gate, against the served model',
    register: 'registered as @challenger: never served',
    sweep: 'a cut for it, on rows it never trained on (dry run)',
    card: 'its model card',
};
const steps = computed(() =>
    (props.row?.steps ?? []).map((step) => {
        const running = running_seconds(step, clock.value);
        const took = step.seconds != null ? fmt_duration(step.seconds) : running !== null ? fmt_duration(running) : null;
        return { ...step, took, says: STEP_SAYS[step.name] ?? '' };
    }),
);
const fit = computed(() => {
    const progress = props.row?.progress;
    if (!progress) return null;
    const step = props.row?.steps.find((entry) => entry.name === 'train');
    // A fit nobody is running has no finish to promise.
    const left = view.value.busy ? fit_seconds_left(step, progress.done, clock.value) : null;
    return { ...progress, left: left === null ? null : fmt_duration(left) };
});
const total = computed(() => {
    const seconds = (props.row?.steps ?? []).reduce((sum, step) => sum + (step.seconds ?? running_seconds(step, clock.value) ?? 0), 0);
    return seconds > 0 ? fmt_duration(seconds) : null;
});
</script>

<template>
    <section class="fui-panel retrain" aria-label="Retrain">
        <span class="fui-corner tl"></span><span class="fui-corner br"></span>
        <div class="retrain__top">
            <button type="button" class="board-btn retrain__go" :class="{ 'is-armed': armed, 'is-live': view.busy }"
                :disabled="view.action === 'none' || pressing || !!blocked"
                :title="blocked ?? 'Runs the gated retrain now: export, label, build, train, gate. It ends in a @challenger or in nothing, and never changes what is served.'"
                @click="press">
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                {{ pressing ? 'STARTING…' : armed ? 'PRESS AGAIN TO START' : view.label }}
            </button>
            <div class="retrain__words">
                <p class="retrain__status" :class="`retrain__status--${view.tone}`" role="status">{{ view.status }}</p>
                <p v-if="armed" class="retrain__sub">About an hour of GPU, and the week's teacher labels at the label step.</p>
                <p v-else-if="blocked" class="retrain__sub retrain__sub--warn">{{ blocked }}</p>
                <p v-else-if="ready" class="retrain__sub">
                    {{ ready.rows }} clean labels of yours ({{ ready.open }} open / {{ ready.done }} done)<template v-if="ready.reserve"> · {{ ready.reserve.rows }} held in reserve for the sweep</template>
                </p>
                <p v-if="notice" class="retrain__sub" :class="{ 'retrain__sub--warn': notice_bad }" role="status">{{ notice }}</p>
            </div>
            <div v-if="row" class="retrain__meta">
                <span class="fui-techtext">CYCLE {{ row.id }}</span>
                <span v-if="row.checkpoint">→ {{ snapshot_label(row.checkpoint) }}</span>
                <span v-if="total">{{ total }} {{ view.busy ? 'so far' : 'of work' }}</span>
            </div>
        </div>

        <template v-if="row">
            <div class="fui-meter retrain__bar" :class="{ 'fui-meter--march': view.busy, 'fui-meter--warn': view.tone === 'warn' }"
                role="progressbar" aria-label="Retrain cycle" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="Math.round(view.done * 100)">
                <i class="fui-meter__fill" :style="{ width: `${view.done * 100}%` }"></i>
            </div>
            <ol class="retrain__steps">
                <li v-for="step in steps" :key="step.name" class="retrain__step" :class="`retrain__step--${step.state}`" :title="step.says">
                    <span class="retrain__mark" aria-hidden="true">{{ step.state === 'done' ? '✓' : step.state === 'failed' ? '✕' : step.state === 'skipped' ? '–' : step.state === 'running' ? '▸' : '·' }}</span>
                    <span class="fui-techtext retrain__name">{{ step.name }}</span>
                    <span class="retrain__took">{{ step.took ?? (step.state === 'skipped' ? 'skipped' : '') }}</span>
                </li>
            </ol>
            <div v-if="fit" class="retrain__fit">
                <div class="fui-meter retrain__fitbar" role="progressbar" aria-label="The fit" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="Math.round((fit.done ?? 0) * 100)">
                    <i class="fui-meter__fill" :style="{ width: `${(fit.done ?? 0) * 100}%` }"></i>
                </div>
                <p class="retrain__fitline">
                    <b v-if="!view.busy" class="retrain__stopped">NOT RUNNING<template v-if="stopped_at"> · last wrote {{ stopped_at }}</template></b>
                    <template v-if="!view.busy"> · stopped at </template><template v-else>TRAIN · </template>epoch {{ fit.epoch }}/{{ fit.epochs }} · batch {{ fit.step }}<template v-if="fit.steps">/{{ fit.steps }}</template>
                    <template v-if="fit.done !== null"> · <b>{{ fmt_share(fit.done) }}</b></template>
                    <template v-if="fit.loss !== null"> · loss {{ fit.loss.toFixed(4) }}</template>
                    <template v-if="fit.left"> · about {{ fit.left }} left</template>
                    <template v-if="!view.busy"> · a resume starts the fit over</template>
                </p>
            </div>
            <dl class="retrain__facts">
                <div v-if="row.reason"><dt>WHY</dt><dd>{{ row.reason }}</dd></div>
                <div v-if="row.rows !== null"><dt>TRAINING SET</dt><dd>{{ row.rows }} rows · {{ row.operator_rows ?? 0 }} yours · {{ row.reserved ?? 0 }} held in reserve<template v-if="row.teacher"> · teacher {{ row.teacher }}</template></dd></div>
                <div v-if="row.eval"><dt>HELD-OUT, BASE → FINE-TUNE</dt><dd>accuracy {{ fmt_share(row.eval.before.accuracy) }} → <b>{{ fmt_share(row.eval.after.accuracy) }}</b> · loss {{ row.eval.before.loss.toFixed(3) }} → <b>{{ row.eval.after.loss.toFixed(3) }}</b> · {{ row.eval.items }} rows</dd></div>
                <div v-if="row.gate || row.gate_why"><dt>GATE</dt><dd><b v-if="row.gate">{{ row.gate }}</b> {{ row.gate_why }}</dd></div>
                <div v-if="row.sweep_best !== null"><dt>ITS OWN CUT</dt><dd>{{ row.sweep_best.toFixed(2) }} on {{ row.sweep_rows }} rows it never trained on · a dry run, nothing applied</dd></div>
            </dl>
        </template>
        <p v-else-if="last_run" class="retrain__sub retrain__last">Last run: <b>{{ last_run.verdict }}</b><template v-if="last_run.reason"> · {{ last_run.reason }}</template></p>
        <p v-if="row && last_run && last_run.state === 'noop'" class="retrain__sub retrain__last">A later run found nothing owed<template v-if="last_run.reason">: {{ last_run.reason }}</template></p>
    </section>
</template>

<style scoped>
.retrain { position: relative; padding: 14px 16px; display: grid; gap: 10px; }
.retrain__top { display: flex; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
.retrain__go { padding: 11px 20px; font-size: 0.78rem; --fui-accent: rgba(147, 255, 212, 0.9); }
.retrain__go.is-armed { --fui-accent: rgba(248, 193, 70, 0.95); color: var(--fui-warn); }
.retrain__go.is-live { opacity: 0.85; }
.retrain__words { flex: 1 1 320px; min-width: 0; }
.retrain__status { margin: 0; font-size: 0.9rem; color: var(--fui-text); }
.retrain__status--warn { color: var(--fui-warn); }
.retrain__status--ok, .retrain__status--live { color: var(--color-accent-green); }
.retrain__sub { margin: 3px 0 0; font-size: 0.78rem; color: var(--fui-text-dim); }
.retrain__sub b { color: var(--fui-text); }
.retrain__sub--warn { color: var(--fui-warn); }
.retrain__meta { display: grid; gap: 2px; text-align: right; font: 0.72rem var(--fui-mono); color: var(--fui-text-dim); }
.retrain__bar { height: 8px; color: var(--fui-slate); --fui-meter-color: var(--color-accent-green); }
.retrain__steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr)); gap: 6px; }
.retrain__step { display: grid; grid-template-columns: auto 1fr; gap: 0 6px; align-items: baseline; padding: 6px 8px; border: 1px solid rgba(157, 218, 255, 0.14); border-radius: 3px; color: var(--fui-text-dim); }
.retrain__mark { font: 600 0.8rem var(--fui-mono); }
.retrain__name { font-size: 0.68rem; }
.retrain__took { grid-column: 2; font: 0.72rem var(--fui-mono); min-height: 1em; }
.retrain__step--done { color: var(--fui-text); border-color: rgba(147, 255, 212, 0.35); }
.retrain__step--done .retrain__mark { color: var(--color-accent-green); }
.retrain__step--running { color: var(--color-accent-green); border-color: var(--color-accent-green); background: rgba(147, 255, 212, 0.07); }
.retrain__step--failed { color: var(--fui-warn); border-color: rgba(248, 193, 70, 0.6); }
.retrain__fit { display: grid; gap: 4px; }
.retrain__fitbar { height: 14px; color: var(--fui-slate); --fui-meter-color: var(--color-accent-green); }
.retrain__fitline { margin: 0; font: 0.78rem var(--fui-mono); color: var(--fui-text-dim); font-variant-numeric: tabular-nums; }
.retrain__fitline b { color: var(--fui-text); }
.retrain__fitline .retrain__stopped { color: var(--fui-warn); }
.retrain__facts { margin: 0; display: grid; gap: 4px; font-size: 0.78rem; }
.retrain__facts > div { display: grid; grid-template-columns: 190px 1fr; gap: 10px; }
.retrain__facts dt { font: 600 0.66rem var(--fui-mono); letter-spacing: 0.1em; color: var(--fui-text-dim); padding-top: 2px; }
.retrain__facts dd { margin: 0; color: var(--fui-text-dim); min-width: 0; }
.retrain__facts b { color: var(--fui-text); }
.retrain__last { margin-top: 0; }
@media (max-width: 640px) { .retrain__facts > div { grid-template-columns: 1fr; gap: 0; } .retrain__meta { text-align: left; } }
</style>
