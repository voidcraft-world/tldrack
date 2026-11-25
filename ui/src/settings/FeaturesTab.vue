<template>
    <section class="fui-panel st-card" aria-labelledby="st-preset-title">
        <div class="st-card__head">
            <h2 id="st-preset-title" class="fui-techtext st-card__title">PRESETS</h2>
            <span class="lr-grow"></span>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': arm.armed.value === 'lite' }"
                :disabled="applying"
                title="Switch off everything that costs a CPU or a GPU: no office and no motion in this browser, and the bridge’s LITE switches off. Press twice."
                @click="arm.press('lite', () => void apply_preset('lite'))"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ arm.armed.value === 'lite' ? 'LITE?' : 'LITE' }}
            </button>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': arm.armed.value === 'defaults' }"
                :disabled="applying"
                title="Back to the rack as it ships: everything in this browser, and every bridge switch at its default. Press twice."
                @click="arm.press('defaults', () => void apply_preset('defaults'))"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ arm.armed.value === 'defaults' ? 'DEFAULTS?' : 'DEFAULTS' }}
            </button>
        </div>
        <p class="st-card__about">
            LITE is the rack with its costly parts off: the rows, the terminal and the agents all still work. DEFAULTS is the rack as it
            ships: everything on, except a part that acts with no click (auto-update).
            Both set this browser and the bridge in one press.
        </p>
        <p v-if="preset_note" class="lr-status" :class="{ 'lr-status--bad': !preset_note.ok }" role="status">{{ preset_note.text }}</p>
    </section>

    <section class="fui-panel st-card" aria-labelledby="st-browser-title">
        <div class="st-card__head">
            <h2 id="st-browser-title" class="fui-techtext st-card__title">THIS BROWSER</h2>
        </div>
        <p class="st-card__about">What this device draws. Another device on the same bridge keeps its own answers.</p>
        <p v-if="!stored" class="fui-callout fui-callout--warn" role="status">
            This browser stored nothing for the page (a private window, or blocked site data), so these choices hold for this visit only.
        </p>
        <ul class="st-rows">
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">3D office</span>
                    <p id="st-office-text" class="st-row__text">
                        Every agent at a desk, in the rack’s canvas pane. Off, the 3D library is never loaded and nothing is drawn per frame.
                    </p>
                </div>
                <SettingSwitch :on="prefs.office" label="3D office" describedby="st-office-text" @set="(on) => set_prefs({ ...prefs, office: on })" />
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">Office quality</span>
                    <p class="st-row__text">
                        Sharp supersamples the scene on every frame. Light draws the display’s own pixels, about 30 times a second.
                    </p>
                </div>
                <SettingChoice
                    name="st-office-quality"
                    label="Office quality"
                    :options="QUALITY_OPTIONS"
                    :value="prefs.office_quality"
                    @set="(quality) => set_prefs({ ...prefs, office_quality: quality })"
                />
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">Motion</span>
                    <p id="st-motion-text" class="st-row__text">
                        The panels’ transitions. The scanner on a control that is still working keeps running either way.
                    </p>
                </div>
                <SettingSwitch :on="prefs.motion" label="Motion" describedby="st-motion-text" @set="(on) => set_prefs({ ...prefs, motion: on })" />
            </li>
        </ul>
    </section>

    <section v-if="!features" class="fui-panel st-card" aria-labelledby="st-bridge-title">
        <div class="st-card__head">
            <h2 id="st-bridge-title" class="fui-techtext st-card__title">THE BRIDGE</h2>
        </div>
        <p v-if="problem" class="fui-callout fui-callout--warn" role="alert">{{ problem }}</p>
        <p v-else-if="loading" class="lr-loading">Reading the bridge’s switches…</p>
        <p v-else class="lr-loading">Nothing read yet.</p>
    </section>

    <template v-else>
        <p v-if="features.error" class="fui-callout fui-callout--warn" role="alert">{{ features.error }}</p>
        <p v-if="features.unreadable" class="fui-callout fui-callout--warn" role="status">
            {{ features.unreadable }} {{ features.unreadable === 1 ? 'switch' : 'switches' }} from this bridge could not be read by this page, and
            {{ features.unreadable === 1 ? 'is' : 'are' }} not shown.
        </p>
        <p v-if="write_error" class="fui-callout fui-callout--warn" role="alert">{{ write_error }}</p>
        <p v-if="groups.length === 0" class="lr-loading">This bridge offers no switches.</p>

        <section v-for="(group, group_index) in groups" :key="group.id" class="fui-panel st-card" :aria-labelledby="`st-group-${group_index}`">
            <div class="st-card__head">
                <h2 :id="`st-group-${group_index}`" class="fui-techtext st-card__title">{{ group.title }}</h2>
            </div>
            <p v-if="group.about" class="st-card__about">{{ group.about }}</p>
            <ul class="st-rows">
                <li v-for="(feature, index) in group.features" :key="feature.key" class="st-row">
                    <div class="st-row__body">
                        <span class="st-row__title">{{ feature.title }}</span>
                        <p :id="`st-feature-${group_index}-${index}`" class="st-row__text">{{ feature.does }}</p>
                        <dl class="st-facts">
                            <div>
                                <dt>Costs</dt>
                                <dd>{{ feature.cost }}</dd>
                            </div>
                            <div>
                                <dt>Without it</dt>
                                <dd>{{ feature.without }}</dd>
                            </div>
                        </dl>
                        <p v-if="waits_for_restart(feature)" class="st-row__note" role="status">
                            applies at the next start: <code>{{ RESTART_COMMAND }}</code>
                        </p>
                    </div>
                    <SettingSwitch
                        :on="feature.on"
                        :label="feature.title"
                        :disabled="saving.has(feature.key)"
                        :describedby="`st-feature-${group_index}-${index}`"
                        @set="(on) => void write({ [feature.key]: on })"
                    />
                </li>
            </ul>
        </section>
    </template>

    <OptionalParts />
</template>

<script setup lang="ts">
/**
 * FEATURES: one place to switch off the parts of the rack that cost a CPU or a GPU.
 *
 *   THIS BROWSER  what this device draws: `config/rack-prefs.ts` owns the three switches and why
 *                 they are per device. Saved on change; the rack in another tab follows.
 *   THE BRIDGE    what the bridge runs: `features-client.ts`. A switch moves when the bridge
 *                 says it did, never before.
 *   OPTIONAL      three parts with routes of their own: `OptionalParts.vue`.
 *
 * LITE and DEFAULTS set both halves in one press, and are armed. A `restart` switch only says
 * what to run: a restart is the operator's call, so nothing here restarts the bridge.
 */
import { computed, onBeforeUnmount, ref } from 'vue';

import { useArmedAction } from '../composables/useArmedAction.ts';
import { FUI_MOTION_FLAGS } from '../config/fui-motion-flags.ts';
import {
    DEFAULT_RACK_PREFS,
    LITE_RACK_PREFS,
    load_rack_prefs,
    save_rack_prefs,
    watch_rack_prefs,
    type OfficeQuality,
    type RackPrefs,
} from '../config/rack-prefs.ts';
import OptionalParts from './OptionalParts.vue';
import SettingChoice from './SettingChoice.vue';
import SettingSwitch from './SettingSwitch.vue';
import {
    RESTART_COMMAND,
    changes_needed,
    default_changes,
    fetch_features,
    group_features,
    lite_changes,
    set_features,
    waits_for_restart,
    type FeaturesView,
} from './features-client.ts';
import { use_reload } from './page-bridge.ts';

const ARM_MS = 4_000;
const QUALITY_OPTIONS: ReadonlyArray<{ value: OfficeQuality; label: string; title: string }> = [
    { value: 'sharp', label: 'SHARP', title: 'Supersampled: the sharpest picture, the most GPU' },
    { value: 'light', label: 'LIGHT', title: 'The display’s own pixels, about 30 frames a second' },
];

// ── this browser ─────────────────────────────────────────────────────────────
const prefs = ref<RackPrefs>(load_rack_prefs());
/** False once a save stored nothing: the page then says the choice is for this visit. */
const stored = ref(true);

function set_prefs(next: RackPrefs): void {
    prefs.value = next;
    stored.value = save_rack_prefs(next);
    // The browser tells every tab but the writer, so the writer applies its own switch: how a
    // page follows MOTION is owned by styles/motion-pref.ts — see the note there.
    FUI_MOTION_FLAGS.enabled = next.motion;
}

// A change made in another tab (a second settings page) lands here too.
const stop_watching = watch_rack_prefs((next) => {
    prefs.value = next;
});
onBeforeUnmount(stop_watching);

// ── the bridge ───────────────────────────────────────────────────────────────
const features = ref<FeaturesView | null>(null);
const loading = ref(false);
/** Why there is no list, when the reason is not "the bridge is down" (the banner says that). */
const problem = ref<string | null>(null);
const write_error = ref<string | null>(null);
/** The keys with a write out: their switches wait for the answer. */
const saving = ref<ReadonlySet<string>>(new Set());

const groups = computed(() => group_features(features.value?.features ?? []));

let generation = 0;

async function load(): Promise<void> {
    const mine = ++generation;
    loading.value = true;
    const result = bridge.note(await fetch_features());
    if (mine !== generation) return; // a newer read or a write is out: its answer is the one to draw
    loading.value = false;
    if (result.ok) {
        features.value = result.data;
        problem.value = null;
    } else if (result.kind === 'missing') {
        problem.value = `The running bridge predates these switches. Restart it: ${RESTART_COMMAND}`;
    } else {
        problem.value = result.kind === 'unreachable' ? null : result.error;
    }
}

const bridge = use_reload(() => void load());

/** Set switches by key. True when the bridge took them; the list is then its answer. */
async function write(changes: Readonly<Record<string, boolean>>): Promise<boolean> {
    const keys = Object.keys(changes);
    saving.value = new Set([...saving.value, ...keys]);
    write_error.value = null;
    generation += 1; // a read already out describes the switches before this write
    const result = bridge.note(await set_features(changes));
    saving.value = new Set([...saving.value].filter((key) => !keys.includes(key)));
    loading.value = false;
    if (result.ok) {
        features.value = result.data;
        return true;
    }
    write_error.value = result.kind === 'unreachable' ? null : result.error;
    return false;
}

// ── presets ──────────────────────────────────────────────────────────────────
const arm = useArmedAction(ARM_MS);
const applying = ref(false);
const preset_note = ref<{ ok: boolean; text: string } | null>(null);

const PRESET_DONE = {
    lite: 'LITE is in place: this browser draws no office and no motion, and the bridge’s LITE switches are off.',
    defaults: 'Defaults are back: this browser draws everything, and every bridge switch is at its default.',
} as const;

async function apply_preset(preset: keyof typeof PRESET_DONE): Promise<void> {
    set_prefs({ ...(preset === 'lite' ? LITE_RACK_PREFS : DEFAULT_RACK_PREFS) });
    const list = features.value?.features;
    if (!list) {
        preset_note.value = { ok: false, text: 'This browser is set. The bridge’s switches were not read, so none of them changed.' };
        return;
    }
    const changes = preset === 'lite' ? lite_changes(list) : default_changes(list);
    applying.value = true;
    const done = changes_needed(list, changes) === 0 || (await write(changes));
    applying.value = false;
    preset_note.value = done
        ? { ok: true, text: PRESET_DONE[preset] }
        : { ok: false, text: 'This browser is set. The bridge did not take its half: its switches are as they were.' };
}
</script>
