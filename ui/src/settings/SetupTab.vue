<template>
    <UpdateCard />
    <LaunchModelsCard />

    <section class="fui-panel st-card" aria-labelledby="st-doctor-title">
        <div class="st-card__head">
            <h2 id="st-doctor-title" class="fui-techtext st-card__title">THIS MACHINE</h2>
            <span v-if="report" class="fui-tag" :class="verdict_class">{{ report.verdict }}</span>
            <span v-if="counts" class="fui-count">{{ counts }}</span>
            <span class="lr-grow"></span>
            <button type="button" class="board-btn" :disabled="loading" title="Run the checks again" @click="load">
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ loading ? 'CHECKING…' : 'REFRESH' }}
            </button>
        </div>
        <p class="st-card__about">
            What <code>tldrack doctor</code> prints, read from the running bridge. A part that is missing says how to get it.
        </p>

        <p v-if="action_note" class="lr-status" :class="{ 'lr-status--bad': !action_note.ok }" role="status">{{ action_note.text }}</p>
        <p v-if="problem" class="fui-callout fui-callout--warn" role="alert">{{ problem }}</p>
        <p v-else-if="!report && loading" class="lr-loading">Running the checks…</p>
        <p v-else-if="!report" class="lr-loading">No report yet.</p>

        <template v-if="report">
            <section v-for="section in report.sections" :key="section.title" class="st-section">
                <h3 class="st-section__title">{{ section.title }}</h3>
                <ul class="st-checks">
                    <li v-for="check in section.checks" :key="check.name" class="st-check">
                        <span class="st-mark" :class="`st-mark--${check.level}`" aria-hidden="true">{{ LEVEL_MARKS[check.level] }}</span>
                        <div class="st-check__body">
                            <span class="st-check__name">{{ check.name }}<span class="st-sr">: {{ LEVEL_WORDS[check.level] }}</span></span>
                            <span v-if="check.detail" class="st-check__detail">{{ check.detail }}</span>
                            <code v-if="fix_of(check)" class="st-fix">{{ fix_of(check) }}</code>
                        </div>
                        <button
                            v-if="check.action"
                            type="button"
                            class="board-btn"
                            :disabled="acting !== null"
                            :title="`Install the status hooks for ${check.action.profile}: the same merge as tldrack profiles --install-hooks ${check.action.profile}`"
                            @click="run_action(check.action)"
                        >
                            <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ acting === action_key(check.action) ? 'INSTALLING…' : 'INSTALL' }}
                        </button>
                    </li>
                </ul>
            </section>
        </template>
    </section>

    <LayaCard />
</template>

<script setup lang="ts">
/**
 * SETUP: whether the rack itself is up to date (`UpdateCard.vue`), the machine's health, and the
 * two installs a first run needs (the agent hooks, Laya).
 *
 * The report is read when the tab opens, on REFRESH, when the browser tab comes back to the
 * front, and after an install: never on a timer (`page-bridge.ts` owns the rule). A report
 * already on screen stays while a newer read is out, or failed: a stale answer with the reason
 * beside it beats an empty card.
 */
import { computed, ref } from 'vue';

import LayaCard from './LayaCard.vue';
import LaunchModelsCard from './LaunchModelsCard.vue';
import UpdateCard from './UpdateCard.vue';
import {
    LEVEL_MARKS,
    LEVEL_WORDS,
    action_key,
    doctor_counts,
    fetch_doctor,
    fix_of,
    install_hooks,
    verdict_level,
    type DoctorAction,
    type DoctorReport,
} from './doctor-client.ts';
import { RESTART_COMMAND } from './features-client.ts';
import { use_reload } from './page-bridge.ts';

const report = ref<DoctorReport | null>(null);
const loading = ref(false);
/** Why the last read gave no report, when the reason is not "the bridge is down" (the banner says that). */
const problem = ref<string | null>(null);
/** The action in flight, by key: one at a time, since each ends in a fresh report. */
const acting = ref<string | null>(null);
/** What the last action answered. On the card, not the check: a fixed check loses its button, and the answer must outlive it. */
const action_note = ref<{ ok: boolean; text: string } | null>(null);

let generation = 0;

async function load(): Promise<void> {
    const mine = ++generation;
    loading.value = true;
    const result = bridge.note(await fetch_doctor());
    if (mine !== generation) return; // a newer read is out: its answer is the one to draw
    loading.value = false;
    if (result.ok) {
        report.value = result.data;
        problem.value = null;
    } else if (result.kind === 'missing') {
        problem.value = `The running bridge predates this page's checks. Restart it: ${RESTART_COMMAND}`;
    } else {
        problem.value = result.kind === 'unreachable' ? null : result.error;
    }
}

const bridge = use_reload(() => void load());

const verdict_class = computed(() => {
    if (!report.value) return '';
    const level = verdict_level(report.value);
    return level === 'ok' ? 'fui-tag--ok' : 'fui-tag--warn';
});

const counts = computed(() => {
    if (!report.value) return '';
    const tally = doctor_counts(report.value);
    return [`${tally.ok} OK`, tally.warn ? `${tally.warn} WARN` : '', tally.fail ? `${tally.fail} FAILED` : '', tally.off ? `${tally.off} OFF` : '']
        .filter(Boolean)
        .join(' · ');
});

async function run_action(action: DoctorAction): Promise<void> {
    const key = action_key(action);
    acting.value = key;
    const result = bridge.note(await install_hooks(action.profile));
    acting.value = null;
    action_note.value = result.ok ? { ok: true, text: result.data.message } : { ok: false, text: result.error };
    if (result.ok) await load();
}
</script>
