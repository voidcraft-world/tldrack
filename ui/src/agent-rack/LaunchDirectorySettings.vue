<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { LaunchDirectory } from '@tldrack/schemas/launch-directories.ts';
import type { LaunchDirectoriesView } from './launch-directories-client.ts';

const props = defineProps<{ view?: LaunchDirectoriesView; disabled: boolean }>();
const emit = defineEmits<{ save: [value: string] }>();
const rows = ref<LaunchDirectory[]>([]);
const default_index = ref(0);
const dirty = ref(false);
const error = ref('');
const waiting = ref(false);
watch(() => props.view, (view) => {
    if (waiting.value && view && !view.saving) {
        waiting.value = false;
        if (view.saved) dirty.value = false;
    }
    if (!dirty.value && view?.state) {
        rows.value = view.state.directories.map((entry) => ({ ...entry }));
        default_index.value = rows.value.findIndex((entry) => entry.path === view.state!.default_path);
    }
}, { immediate: true });
const blocked = computed(() => props.disabled || props.view?.saving);

function add(): void {
    rows.value.push({ label: '', path: '' });
    dirty.value = true;
}
function remove(index: number): void {
    rows.value.splice(index, 1);
    if (default_index.value === index) default_index.value = 0;
    else if (default_index.value > index) default_index.value -= 1;
    dirty.value = true;
}
function save(): void {
    error.value = '';
    if (rows.value.some((entry) => !entry.label.trim() || !entry.path.trim())) {
        error.value = 'Give each directory a name and path.';
        return;
    }
    waiting.value = true;
    emit('save', JSON.stringify({ directories: rows.value, default_path: rows.value[default_index.value]?.path }));
}
</script>

<template>
    <section class="directories" aria-label="Launch directories">
        <strong class="fui-techtext">DIRECTORIES</strong>
        <p>Each directory is a button on the launch row. The default is where a launch that names no directory starts.</p>
        <form v-if="view?.state" @submit.prevent="save">
            <fieldset :disabled="blocked">
                <div v-for="(row, index) in rows" :key="index" class="directories__row">
                    <input v-model="default_index" type="radio" name="launch-default" :value="index"
                        :aria-label="`Use ${row.label || 'directory'} by default`" title="Default directory" @change="dirty = true" />
                    <label class="directories__name">Name<input v-model="row.label" aria-label="Directory name" placeholder="Project name" maxlength="40"
                        required @input="dirty = true" /></label>
                    <button type="button" class="fui-btn" :disabled="rows.length === 1"
                        :aria-label="`Remove ${row.label || 'directory'}`" title="Remove directory" @click="remove(index)">×</button>
                    <label class="directories__path">Path<input v-model="row.path" aria-label="Directory path"
                        placeholder="~/code/project" maxlength="4096" autocomplete="off" spellcheck="false"
                        required @input="dirty = true" /></label>
                </div>
                <div class="directories__actions">
                    <button type="button" class="fui-btn" :disabled="rows.length >= 16" @click="add">+ DIRECTORY</button>
                    <button type="submit" class="fui-btn" :disabled="!dirty">{{ view.saving ? 'SAVING…' : 'SAVE' }}</button>
                    <span v-if="view.saved && !dirty" role="status">Saved</span>
                </div>
            </fieldset>
        </form>
        <p v-else-if="!view?.error">Reading directories…</p>
        <p v-if="error || view?.error" class="fui-callout fui-callout--warn" role="alert">{{ error || view?.error }}</p>
    </section>
</template>

<style scoped>
.directories { display: grid; gap: 6px; min-width: 0; }
.directories p { margin: 0; font-size: 0.8rem; }
.directories fieldset { border: 0; padding: 0; margin: 0; min-width: 0; display: grid; gap: 8px; }
.directories__row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 5px; align-items: center; }
.directories input:not([type='radio']) { min-width: 0; padding: 5px; min-height: 32px; border: 1px solid rgba(180, 190, 200, 0.4); background: transparent; color: inherit; font-size: 0.8rem; }
.directories__path { grid-column: 2 / -1; }
.directories label { display: grid; gap: 4px; min-width: 0; font-size: 12px; color: var(--rack-muted); }
.directories__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.directories :is(button, input):focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
@media (pointer: coarse) { .directories :is(button, input:not([type='radio'])) { min-height: 44px; } }
</style>
