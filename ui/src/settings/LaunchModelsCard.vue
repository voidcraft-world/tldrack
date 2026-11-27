<template>
    <section class="fui-panel st-card" aria-labelledby="st-models-title">
        <div class="st-card__head">
            <h2 id="st-models-title" class="fui-techtext st-card__title">LAUNCH DEFAULTS</h2>
            <span class="lr-grow"></span>
            <button type="button" class="board-btn" :disabled="loading || saving !== null" @click="load">REFRESH</button>
        </div>
        <p class="st-card__about">
            Choose a model for each Claude or Codex profile, and a reasoning level for Codex.
            Saved changes apply to new launches immediately. Running sessions keep their settings.
        </p>
        <p class="st-card__about">Leave a field at “Profile default” to inherit that profile’s settings. Model and reasoning support depend on your CLI and account.</p>
        <p v-if="problem" class="fui-callout fui-callout--warn" role="alert">{{ problem }}</p>
        <p v-if="note" class="lr-status" role="status">{{ note }}</p>
        <p v-if="loading && !settings" class="lr-loading">Reading launch defaults…</p>
        <p v-if="settings?.error" class="fui-callout fui-callout--warn" role="alert">{{ settings.error }}</p>
        <p v-if="settings && rows.length === 0" class="lr-loading">No Claude or Codex profiles are configured.</p>
        <form v-for="row in rows" :key="row.profile.id" class="st-model-form" @submit.prevent="save(row)">
            <fieldset :disabled="saving !== null || !!settings?.error">
                <legend>{{ row.profile.label }} <span class="st-model-kind">{{ row.profile.kind }}</span></legend>
                <p v-if="row.profile.profile_model_label" class="st-row__text">Profile default: {{ row.profile.profile_model_label }}</p>
                <div class="st-model-fields">
                    <label>
                        Model
                        <input v-model="row.model" type="text" maxlength="128" placeholder="Profile default" autocomplete="off" spellcheck="false" />
                    </label>
                    <label v-if="row.profile.kind === 'codex'">
                        Reasoning
                        <select v-model="row.effort">
                            <option value="">Profile default</option>
                            <option v-for="effort in settings?.reasoning_efforts" :key="effort" :value="effort">{{ effort }}</option>
                        </select>
                    </label>
                    <button type="submit" class="board-btn" :disabled="!dirty(row)">{{ saving === row.profile.id ? 'SAVING…' : 'SAVE' }}</button>
                    <button type="button" class="board-btn" :disabled="!row.model && !row.effort" @click="reset(row)">USE PROFILE DEFAULTS</button>
                </div>
                <p v-if="dirty(row)" class="st-row__text">Unsaved changes</p>
            </fieldset>
        </form>
    </section>
</template>

<script setup lang="ts">
import { ref } from 'vue';

import { use_reload } from './page-bridge.ts';
import { fetch_launch_settings, save_launch_settings, type LaunchSettings, type ProfileLaunch } from './profile-launch-client.ts';

interface Draft {
    profile: ProfileLaunch;
    model: string;
    effort: string;
}
const settings = ref<LaunchSettings | null>(null);
const rows = ref<Draft[]>([]);
const loading = ref(false);
const saving = ref<string | null>(null);
const problem = ref<string | null>(null);
const note = ref<string | null>(null);
let generation = 0;

function dirty(row: Draft): boolean {
    return row.model.trim() !== (row.profile.model ?? '') || row.effort !== (row.profile.reasoning_effort ?? '');
}

function accept(data: LaunchSettings, saved?: string): void {
    settings.value = data;
    rows.value = data.profiles.map((profile) => {
        const old = rows.value.find((row) => row.profile.id === profile.id);
        if (old && profile.id !== saved && dirty(old)) return { ...old, profile };
        return { profile, model: profile.model ?? '', effort: profile.reasoning_effort ?? '' };
    });
}

async function load(): Promise<void> {
    if (saving.value !== null) return;
    const mine = ++generation;
    loading.value = true;
    const result = bridge.note(await fetch_launch_settings());
    if (mine !== generation) return;
    loading.value = false;
    if (result.ok) {
        accept(result.data);
        problem.value = null;
    } else {
        problem.value = result.kind === 'missing' ? 'This bridge does not support launch defaults yet. Update the bridge to use these controls.' : result.error;
    }
}
const bridge = use_reload(() => void load());

function reset(row: Draft): void {
    row.model = '';
    row.effort = '';
    note.value = null;
}

async function save(row: Draft): Promise<void> {
    if (saving.value !== null) return;
    ++generation;
    loading.value = false;
    saving.value = row.profile.id;
    note.value = null;
    problem.value = null;
    const result = bridge.note(await save_launch_settings(row.profile.id, row.model.trim() || null, row.effort || null));
    saving.value = null;
    if (result.ok) {
        accept(result.data, row.profile.id);
        note.value = `${row.profile.label}: saved. New launches use these defaults now.`;
    } else {
        problem.value = result.error;
    }
}
</script>

<style scoped>
.st-model-form { margin-top: 16px; }
.st-model-form fieldset { border: 1px solid var(--lr-rule); padding: 12px; min-width: 0; }
.st-model-form legend { padding: 0 6px; font-weight: 600; }
.st-model-kind { font-weight: 400; opacity: .7; margin-left: 6px; }
.st-model-fields { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin-top: 10px; }
.st-model-fields label { display: grid; gap: 6px; flex: 1 1 180px; min-width: 0; }
.st-model-fields input, .st-model-fields select { font: inherit; color: inherit; background: var(--lr-well); border: 1px solid var(--lr-rule); padding: 8px; width: 100%; box-sizing: border-box; }
.st-model-fields input:focus-visible, .st-model-fields select:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
.st-model-fields button { min-height: 36px; }
</style>
