<script setup lang="ts">
/**
 * The rack's ⌗ HOW window: how the agent rack works, one diagram per tab.
 *
 * The diagrams are owned by rack-architecture.md. This component only renders them, through
 * the lazy, strict-mode mermaid path of `markdown/markdown-doc-render.ts` (`render_prose_fragment`).
 * The stage pans by dragging and zooms with − / + / FIT or Ctrl/⌘-wheel; a plain wheel or a
 * trackpad scroll pans, so a diagram never traps the page.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import rack_architecture_md from './rack-architecture.md?raw';
import { split_rack_diagram } from './rack-diagram-sections.ts';
import { render_prose_fragment } from '../markdown/markdown-doc-render.ts';
import { prose_html } from '../utils/trusted-html.ts';

const sections = split_rack_diagram(rack_architecture_md);

const MIN_SCALE = 0.15;
const MAX_SCALE = 6;
const ZOOM_STEP = 1.25;
/** Margin FIT leaves round the diagram, as a share of the stage. */
const FIT_FILL = 0.94;

function section_from_hash(): string {
    const id = window.location.hash.replace(/^#/, '');
    return sections.some((s) => s.id === id) ? id : (sections[0]?.id ?? '');
}

const active_id = ref(section_from_hash());
const active = computed(() => sections.find((s) => s.id === active_id.value) ?? sections[0]);

/** Rendered SVG markup per tab, filled on first visit. */
const rendered = ref<Record<string, string>>({});
const rendering = ref(false);

const stage = ref<HTMLElement | null>(null);
const canvas = ref<HTMLElement | null>(null);
const scale = ref(1);
const offset_x = ref(0);
const offset_y = ref(0);
/** True while the view is FIT's; any zoom or pan hands the view to the reader, and a resize
 *  then leaves it alone. */
const fitted = ref(true);
const transform = computed(() => `translate(${offset_x.value}px, ${offset_y.value}px) scale(${scale.value})`);
const scale_label = computed(() => `${Math.round(scale.value * 100)}%`);

function clamp_scale(value: number): number {
    return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
}

/** The diagram's natural size, from its viewBox — mermaid caps the svg with an inline
 *  `max-width`, which is dropped so the stage's transform alone decides how big it draws. */
function diagram_size(): { width: number; height: number } | null {
    const svg = canvas.value?.querySelector('svg');
    if (!svg) return null;
    svg.style.maxWidth = 'none';
    const box = svg.viewBox.baseVal;
    const width = box && box.width > 0 ? box.width : svg.getBoundingClientRect().width;
    const height = box && box.height > 0 ? box.height : svg.getBoundingClientRect().height;
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    return width > 0 && height > 0 ? { width, height } : null;
}

function fit(): void {
    const size = diagram_size();
    const host = stage.value;
    if (!size || !host) return;
    const next = clamp_scale(Math.min(host.clientWidth / size.width, host.clientHeight / size.height) * FIT_FILL);
    scale.value = next;
    offset_x.value = (host.clientWidth - size.width * next) / 2;
    offset_y.value = (host.clientHeight - size.height * next) / 2;
    fitted.value = true;
}

/** Zoom about a stage point (the cursor, or the centre for the buttons) so it stays put. */
function zoom_at(factor: number, at_x?: number, at_y?: number): void {
    const host = stage.value;
    if (!host) return;
    const x = at_x ?? host.clientWidth / 2;
    const y = at_y ?? host.clientHeight / 2;
    const next = clamp_scale(scale.value * factor);
    const ratio = next / scale.value;
    offset_x.value = x - (x - offset_x.value) * ratio;
    offset_y.value = y - (y - offset_y.value) * ratio;
    scale.value = next;
    fitted.value = false;
}

function on_wheel(event: WheelEvent): void {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) {
        // Pinch on a trackpad arrives as ctrl+wheel with small deltas; a mouse notch is ~100.
        const rect = stage.value!.getBoundingClientRect();
        zoom_at(Math.exp(-event.deltaY * 0.01), event.clientX - rect.left, event.clientY - rect.top);
        return;
    }
    offset_x.value -= event.deltaX;
    offset_y.value -= event.deltaY;
    fitted.value = false;
}

let drag: { pointer: number; x: number; y: number } | null = null;

function on_pointer_down(event: PointerEvent): void {
    if (event.button !== 0) return;
    drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY };
    stage.value?.setPointerCapture(event.pointerId);
}

function on_pointer_move(event: PointerEvent): void {
    if (!drag || drag.pointer !== event.pointerId) return;
    offset_x.value += event.clientX - drag.x;
    offset_y.value += event.clientY - drag.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    fitted.value = false;
}

function on_pointer_up(event: PointerEvent): void {
    if (drag?.pointer === event.pointerId) drag = null;
}

async function show(id: string): Promise<void> {
    const section = sections.find((s) => s.id === id);
    if (!section) return;
    if (!(id in rendered.value)) {
        rendering.value = true;
        try {
            // A fence that fails to render comes back as an inline error card, never a throw.
            const html = await render_prose_fragment(`\`\`\`mermaid\n${section.mermaid}\n\`\`\``);
            // Named for the HTML sink below: owned by utils/trusted-html.ts — see the note there.
            rendered.value = { ...rendered.value, [id]: prose_html(html) };
        } finally {
            rendering.value = false;
        }
    }
    await nextTick();
    fit();
}

function select(id: string): void {
    if (id === active_id.value) return;
    active_id.value = id;
    history.replaceState(null, '', `#${id}`);
}

function on_hash_change(): void {
    active_id.value = section_from_hash();
}

watch(active_id, (id) => void show(id));

let resize_observer: ResizeObserver | null = null;

onMounted(() => {
    void show(active_id.value);
    window.addEventListener('hashchange', on_hash_change);
    resize_observer = new ResizeObserver(() => {
        if (fitted.value) fit();
    });
    if (stage.value) resize_observer.observe(stage.value);
});

onBeforeUnmount(() => {
    window.removeEventListener('hashchange', on_hash_change);
    resize_observer?.disconnect();
});
</script>

<template>
    <div class="rack-diagram fui-panel">
        <span class="fui-corner tl" /><span class="fui-corner tr" />
        <span class="fui-corner bl" /><span class="fui-corner br" />

        <header class="rack-diagram__head">
            <span class="fui-techtext rack-diagram__title">agent rack · how it works</span>
            <nav class="rack-diagram__tabs" aria-label="Diagrams">
                <button v-for="section in sections" :key="section.id" type="button" class="fui-btn"
                    :aria-pressed="active_id === section.id" @click="select(section.id)">
                    {{ section.title.toUpperCase() }}
                </button>
            </nav>
            <div class="rack-diagram__zoom" role="group" aria-label="Zoom">
                <button type="button" class="fui-btn" title="Zoom out" aria-label="Zoom out"
                    @click="zoom_at(1 / ZOOM_STEP)">−</button>
                <button type="button" class="fui-btn" title="Fit the diagram to the window"
                    @click="fit">FIT</button>
                <button type="button" class="fui-btn" title="Zoom in" aria-label="Zoom in"
                    @click="zoom_at(ZOOM_STEP)">+</button>
                <span class="fui-techtext rack-diagram__scale" aria-live="polite">{{ scale_label }}</span>
            </div>
        </header>

        <p v-if="active?.lead" class="rack-diagram__lead">{{ active.lead }}</p>

        <div ref="stage" class="rack-diagram__stage" title="Drag to pan · Ctrl/⌘-scroll to zoom"
            @wheel="on_wheel" @pointerdown="on_pointer_down" @pointermove="on_pointer_move"
            @pointerup="on_pointer_up" @pointercancel="on_pointer_up">
            <span v-if="rendering && !(active_id in rendered)" class="fui-techtext rack-diagram__loading">
                rendering…
            </span>
            <!-- Strict-mode mermaid output spliced after DOMPurify: owned by
                 markdown-doc-render.ts `render_prose_fragment` — see the note there. -->
            <div ref="canvas" class="rack-diagram__canvas" :style="{ transform }"
                v-html="rendered[active_id] ?? ''" />
        </div>
    </div>
</template>

<style scoped>
.rack-diagram {
    box-sizing: border-box;
    height: 100%;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 12px 16px;
}

.rack-diagram__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 16px;
    flex: 0 0 auto;
}

.rack-diagram__title {
    margin-right: auto;
}

.rack-diagram__tabs,
.rack-diagram__zoom {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
}

.rack-diagram__scale {
    min-width: 3.5em;
    text-align: right;
    font-size: 0.68rem;
}

.rack-diagram__lead {
    margin: 0;
    flex: 0 0 auto;
    font-size: 0.82rem;
    line-height: 1.4;
    color: rgba(220, 235, 228, 0.78);
}

.rack-diagram__stage {
    position: relative;
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--fui-accent) 22%, transparent);
    background: rgba(4, 8, 6, 0.6);
    cursor: grab;
    touch-action: none;
}

.rack-diagram__stage:active {
    cursor: grabbing;
}

.rack-diagram__canvas {
    position: absolute;
    top: 0;
    left: 0;
    transform-origin: 0 0;
    will-change: transform;
}

.rack-diagram__loading {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    font-size: 0.68rem;
}

.rack-diagram__head [aria-pressed="true"] {
    border-color: var(--color-accent-green, #93ffd4);
}

/* The inline error card a failed fence renders as. */
.rack-diagram__canvas :deep(.vc-mermaid-error) {
    max-width: 80vw;
    white-space: pre-wrap;
    color: #f8c146;
}

@media (max-width: 600px) {
    .rack-diagram {
        padding: 10px 16px;
    }
    .rack-diagram__title {
        flex-basis: 100%;
    }
}
</style>
