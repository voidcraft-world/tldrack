<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { artifact_viewer_url, type CanvasArtifact, type CanvasArtifacts } from './canvas-artifacts.ts';

const props = defineProps<{ shelf: CanvasArtifacts; bridge_origin: string }>();
const dialog = ref<HTMLDialogElement | null>(null);
const selected = ref<CanvasArtifact | null>(null);
const preview_url = computed(() => selected.value && props.shelf.key
    ? artifact_viewer_url(props.bridge_origin, props.shelf.key, selected.value) : null);
async function preview(file: CanvasArtifact): Promise<void> {
    selected.value = file;
    await nextTick();
    dialog.value?.showModal();
}
function close(): void { dialog.value?.close(); selected.value = null; }
watch(() => props.shelf.key, close);
watch(() => props.shelf.artifacts, (files) => {
    if (!selected.value) return;
    const updated = files.find((file) => file.id === selected.value?.id);
    if (!updated || updated.error) close();
    else selected.value = updated;
});
</script>

<template>
    <section class="artifact-shelf" aria-label="Generated artifacts">
        <header class="artifact-shelf__head"><strong title="Generated artifacts">FILES</strong><span>{{ shelf.artifacts.length }}</span></header>
        <p v-if="shelf.error" class="artifact-shelf__error">{{ shelf.error }}</p>
        <div class="artifact-shelf__files">
            <div v-for="file in shelf.artifacts" :key="file.id" class="artifact-shelf__file">
                <button type="button" class="artifact-shelf__preview" :disabled="!!file.error" :title="file.error || `Preview ${file.path}`"
                    :aria-label="`Preview ${file.name}`" @click="preview(file)">{{ file.name }}</button>
                <a v-if="!file.error && shelf.key" :href="artifact_viewer_url(bridge_origin, shelf.key, file)"
                    target="_blank" rel="noopener noreferrer" :aria-label="`Open ${file.name} in a new tab`" title="Open in new tab">↗</a>
                <span v-if="file.error" class="artifact-shelf__error">{{ file.error }}</span>
            </div>
        </div>
        <dialog ref="dialog" class="artifact-shelf__dialog fui-panel" aria-label="Artifact preview" @close="selected = null">
            <header><strong>{{ selected?.name }}</strong><button type="button" class="fui-btn" @click="close">Close ✕</button></header>
            <iframe v-if="preview_url" :src="preview_url" title="Generated artifact"
                sandbox="allow-scripts allow-downloads" referrerpolicy="no-referrer" />
        </dialog>
    </section>
</template>

<style scoped>
.artifact-shelf { display: flex; flex-direction: column; height: 100%; min-width: 0; overflow: hidden; color: var(--rack-text); }
.artifact-shelf__head { display: flex; align-items: center; justify-content: space-between; padding: 5px 8px;
    font: 600 0.68rem ui-monospace, monospace; border-bottom: 1px solid var(--rack-raised); }
.artifact-shelf__files { min-height: 0; overflow: auto; scrollbar-width: none; }
.artifact-shelf__file { display: flex; flex-wrap: wrap; align-items: center; border-bottom: 1px solid var(--rack-raised); }
.artifact-shelf__preview { flex: 1; min-width: 0; min-height: 30px; padding: 5px 8px; text-align: left; color: inherit;
    background: none; border: 0; cursor: pointer; font: inherit; font-size: 0.78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-shelf__preview:hover { background: var(--rack-raised); }
.artifact-shelf__file a { display: grid; place-items: center; width: 30px; height: 30px; color: var(--color-accent-green); text-decoration: none; }
.artifact-shelf__error { width: 100%; padding: 4px 8px; margin: 0; color: var(--fui-warn); font-size: 0.78rem; overflow-wrap: anywhere; }
.artifact-shelf__dialog { box-sizing: border-box; width: min(1100px, calc(100vw - 32px)); height: calc(100dvh - 32px);
    max-width: none; max-height: none; padding: 12px; color: var(--rack-text); background: var(--rack-surface); }
.artifact-shelf__dialog[open] { display: flex; flex-direction: column; gap: 10px; }
.artifact-shelf__dialog::backdrop { background: #25272bcc; }
.artifact-shelf__dialog header { display: flex; align-items: center; gap: 12px; }
.artifact-shelf__dialog header strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-shelf__dialog button { min-height: 32px; padding: 0 12px; color: inherit; }
.artifact-shelf__dialog iframe { flex: 1; min-height: 0; width: 100%; border: 0; }
@media (pointer: coarse) {
    .artifact-shelf__preview, .artifact-shelf__file a, .artifact-shelf__dialog button { min-height: 44px; }
    .artifact-shelf__file a { width: 44px; }
}
</style>
