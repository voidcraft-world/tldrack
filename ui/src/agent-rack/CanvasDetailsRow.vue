<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { DEFAULT_MAP_SHARE, MAX_MAP_SHARE, MIN_MAP_SHARE, clamp_map_share, map_drag_scale, parse_map_share } from './canvas-sections.ts';
import { use_canvas_pointer_drag } from './useCanvasPointerDrag.ts';

const props = defineProps<{ map_visible: boolean; artifacts_visible: boolean }>();
const emit = defineEmits<{ (event: 'dragging', active: boolean): void }>();
const root = ref<HTMLElement | null>(null);
const divider = ref<HTMLElement | null>(null);
const paired = computed(() => props.map_visible && props.artifacts_visible);
const share = ref(DEFAULT_MAP_SHARE);
const storage_key = 'tldrack:canvas-map-share';
try { share.value = parse_map_share(localStorage.getItem(storage_key)); } catch { /* session only */ }
const columns = computed(() => paired.value
    ? `minmax(0, ${share.value}fr) var(--divider-width) minmax(0, ${100 - share.value}fr)`
    : 'minmax(0, 1fr)');
function persist(): void {
    try { localStorage.setItem(storage_key, String(share.value)); } catch { /* session only */ }
}
function resize(value: number): void { share.value = clamp_map_share(value); }
const { begin, move, end } = use_canvas_pointer_drag('x', () => {
    if (!paired.value || !root.value || !divider.value) return null;
    const scale = map_drag_scale(root.value.getBoundingClientRect().width, divider.value.getBoundingClientRect().width);
    return scale === null ? null : { value: share.value, scale };
}, resize, persist, (active) => emit('dragging', active));
watch(paired, (value) => { if (!value) end(); });
function nudge(delta: number): void { resize(share.value + delta); persist(); }
function reset(): void { share.value = DEFAULT_MAP_SHARE; persist(); }
</script>

<template>
    <div ref="root" class="canvas-details" :style="{ gridTemplateColumns: columns }">
        <div v-show="map_visible" class="canvas-details__map"><slot name="map" /></div>
        <button v-if="paired" ref="divider" type="button" class="canvas-details__divider" role="separator"
            aria-orientation="vertical" aria-label="Resize activity map and artifacts"
            :aria-valuenow="Math.round(share)" :aria-valuemin="MIN_MAP_SHARE" :aria-valuemax="MAX_MAP_SHARE"
            :aria-valuetext="`Activity map ${Math.round(share)} percent`"
            title="Drag to resize map and artifacts · double-click or Home for 75% map"
            @pointerdown="begin" @pointermove="move" @pointerup="end" @pointercancel="end"
            @lostpointercapture="end" @dblclick="reset"
            @keydown.left.stop.prevent="nudge(-5)" @keydown.right.stop.prevent="nudge(5)" @keydown.home.stop.prevent="reset" />
        <div v-show="artifacts_visible" class="canvas-details__artifacts"><slot name="artifacts" /></div>
    </div>
</template>

<style scoped>
.canvas-details { --divider-width: 12px; display: grid; height: 100%; min-width: 0; min-height: 0; }
.canvas-details__map, .canvas-details__artifacts { min-width: 0; min-height: 0; overflow: hidden; }
.canvas-details__divider { position: relative; width: 100%; height: 100%; padding: 0; border: 0;
    border-inline: 1px solid var(--rack-raised); background: var(--rack-base); cursor: col-resize; touch-action: none; }
.canvas-details__divider::after { content: ''; position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
    width: 3px; height: 36px; border-radius: 2px; background: var(--rack-muted); opacity: 0.6; }
.canvas-details__divider:hover::after, .canvas-details__divider:focus-visible::after { opacity: 1; background: var(--color-accent-green); }
.canvas-details__divider:focus-visible { outline: 1px solid var(--color-accent-green); outline-offset: -1px; }
@media (pointer: coarse) { .canvas-details { --divider-width: 28px; } }
</style>
