<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { CANVAS_SECTION_LIMITS, clamp_section_height, parse_section_height, type CanvasSection } from './canvas-sections.ts';
import { use_canvas_pointer_drag } from './useCanvasPointerDrag.ts';

const props = defineProps<{ section: CanvasSection; label: string; default_height: string }>();
const emit = defineEmits<{ (event: 'dragging', active: boolean): void }>();
const root = ref<HTMLElement | null>(null);
const height = ref<number | null>(null);
const rendered_height = ref(CANVAS_SECTION_LIMITS[props.section].min as number);
const storage_key = `tldrack:canvas-section-height:${props.section}`;
try { height.value = parse_section_height(props.section, localStorage.getItem(storage_key)); } catch { /* session only */ }
let observer: ResizeObserver | null = null;

function persist(): void {
    try {
        if (height.value === null) localStorage.removeItem(storage_key);
        else localStorage.setItem(storage_key, String(height.value));
    } catch { /* session only */ }
}
function resize(value: number): void { height.value = clamp_section_height(props.section, value); }
const { begin, move, end } = use_canvas_pointer_drag('y',
    () => root.value ? { value: root.value.getBoundingClientRect().height, scale: 1 } : null,
    resize, persist, (active) => emit('dragging', active));
function nudge(delta: number): void {
    resize((root.value?.getBoundingClientRect().height ?? rendered_height.value) + delta);
    persist();
}
function reset(): void { height.value = null; persist(); }
onMounted(() => {
    observer = new ResizeObserver(() => {
        if (root.value) rendered_height.value = Math.round(root.value.getBoundingClientRect().height);
    });
    if (root.value) observer.observe(root.value);
});
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
    <section ref="root" class="canvas-section" :aria-label="label"
        :style="{ height: height === null ? default_height : `${height}px` }">
        <div class="canvas-section__body"><slot /></div>
        <button class="canvas-section__grip" type="button" role="separator" aria-orientation="horizontal"
            :aria-label="`Resize ${label}`" :aria-valuenow="rendered_height"
            :aria-valuemin="CANVAS_SECTION_LIMITS[section].min" :aria-valuemax="CANVAS_SECTION_LIMITS[section].max"
            title="Drag to resize · arrow keys to adjust · double-click or Home to reset"
            @pointerdown="begin" @pointermove="move" @pointerup="end" @pointercancel="end"
            @lostpointercapture="end" @dblclick="reset"
            @keydown.up.prevent="nudge(-24)" @keydown.down.prevent="nudge(24)" @keydown.home.prevent="reset" />
    </section>
</template>

<style scoped>
.canvas-section { display: flex; flex-direction: column; min-width: 0; box-sizing: border-box; }
.canvas-section__body { flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.canvas-section__grip { flex: 0 0 14px; display: block; width: 100%; padding: 0; border: 0;
    border-top: 1px solid var(--rack-raised); background: var(--rack-base); cursor: row-resize; touch-action: none; }
.canvas-section__grip::after { content: ''; display: block; width: 36px; height: 3px; margin: auto;
    border-radius: 2px; background: var(--rack-muted); opacity: 0.6; }
.canvas-section__grip:hover::after, .canvas-section__grip:focus-visible::after { opacity: 1; background: var(--color-accent-green); }
.canvas-section__grip:focus-visible { outline: 1px solid var(--color-accent-green); outline-offset: -1px; }
@media (pointer: coarse) { .canvas-section__grip { flex-basis: 28px; } }
</style>
