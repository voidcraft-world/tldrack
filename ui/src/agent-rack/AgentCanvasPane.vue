<script setup lang="ts">
/**
 * The agent canvas pane: frames the `agent-canvas` bridge plugin's page for the
 * focused session. Feature owner: `plugins/agent-canvas/README.md`.
 *
 * THIS PANE FETCHES NOTHING. The page it frames polls its own bridge, and tells
 * the rack what it holds over `postMessage` (`kit.js` `hello`). That hello is
 * also the plugin's liveness signal — no hello within `HELLO_TIMEOUT_MS` reads
 * as "plugin not running", which is the structural gate: no plugin, no canvas,
 * and the pane says why rather than framing the bridge's 404 JSON.
 *
 * The grip on the pane's LEADING edge (top when under the list, left when a
 * column) resizes it; the window owns the size, clamps it and persists it.
 */
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AgentActivityMap from './AgentActivityMap.vue';
import AgentArtifactShelf from './AgentArtifactShelf.vue';
import CanvasResizableSection from './CanvasResizableSection.vue';
import CanvasDetailsRow from './CanvasDetailsRow.vue';
import { parse_canvas_artifacts, type CanvasArtifacts } from './canvas-artifacts.ts';
import type { OfficeRack } from '../engine/terminal/agent-office-link.ts';
import type { RackSession } from '../engine/terminal/multi-terminal-layout.ts';
import { is_service_session } from '../engine/terminal/service-sessions.ts';
import {
    LEAD_DECISIONS_SLOT_ID,
    NEXT_STEP_SLOT_ID,
    canvas_layout_label,
    parse_canvas_hello,
    type CanvasHello,
    type CanvasLayout,
} from './agent-canvas.ts';

const AgentOfficeView = defineAsyncComponent(() => import('../agent-office/AgentOfficeView.vue'));

const props = defineProps<{
    src: string | null;
    agent?: RackSession | null;
    rack: OfficeRack | null;
    stale?: boolean;
    session: string | null;
    layout: Exclude<CanvasLayout, 'off'>;
    /** The bridge origin the page is served from — the only origin whose
     *  messages are believed, alongside the source-window check. */
    bridge_origin: string;
}>();

const emit = defineEmits<{
    (event: 'cycle'): void;
    (event: 'select', session: string): void;
    /** A drag or nudge: the size the operator is asking for, in CSS px; null = reset. */
    (event: 'resize', wanted_px: number | null): void;
    (event: 'dragging', active: boolean): void;
}>();

/** How long a framed page has to say hello before the pane calls it offline.
 *  The page polls once a second and says hello on its first answer. */
const HELLO_TIMEOUT_MS = 4000;
/** While offline, try the page again this often — a plugin enabled by a bridge
 *  restart then appears without anyone touching the rack. */
const OFFLINE_RETRY_MS = 15000;

const frame = ref<HTMLIFrameElement | null>(null);
const pane = ref<HTMLElement | null>(null);
const hello = ref<CanvasHello | null>(null);
const artifact_shelf = ref<CanvasArtifacts | null>(null);
const artifacts_visible = computed(() => !!artifact_shelf.value?.artifacts.length || !!artifact_shelf.value?.error);
const offline = ref(false);
/** Bumped to remount the iframe for a retry. */
const attempt = ref(0);
const dragging = ref(false);
const map_open = ref(true);
try { map_open.value = localStorage.getItem('tldrack:activity-map') !== 'off'; } catch { /* default */ }
const map_available = computed(() => props.agent?.live && !is_service_session(props.agent.session));
const map_visible = computed(() => !!(map_open.value && map_available.value && props.agent));
const document_visible = computed(() => !props.src || offline.value || !hello.value || hello.value.has_content || artifact_shelf.value === null);
function toggle_map(): void {
    map_open.value = !map_open.value;
    try { localStorage.setItem('tldrack:activity-map', map_open.value ? 'on' : 'off'); } catch { /* session only */ }
}

let hello_timer: ReturnType<typeof setTimeout> | null = null;
let retry_timer: ReturnType<typeof setTimeout> | null = null;

function clear_timers(): void {
    if (hello_timer) clearTimeout(hello_timer);
    if (retry_timer) clearTimeout(retry_timer);
    hello_timer = retry_timer = null;
}

function arm(): void {
    clear_timers();
    hello.value = null;
    artifact_shelf.value = null;
    offline.value = false;
    if (!props.src) return;
    hello_timer = setTimeout(() => {
        offline.value = true;
        retry_timer = setTimeout(() => {
            attempt.value += 1;
            arm();
        }, OFFLINE_RETRY_MS);
    }, HELLO_TIMEOUT_MS);
}

watch(() => props.src, arm);

function on_message(event: MessageEvent): void {
    if (!frame.value || event.source !== frame.value.contentWindow) return;
    if (event.origin !== props.bridge_origin.replace(/\/+$/, '')) return;
    const parsed = parse_canvas_hello(event.data);
    if (!parsed || parsed.session !== props.session) return;
    hello.value = parsed;
    artifact_shelf.value = parse_canvas_artifacts(event.data.shelf);
    frame.value.contentWindow?.postMessage({
        type: 'tldrack-canvas-artifacts', session: props.session, external: artifact_shelf.value !== null,
    }, event.origin);
    if (hello_timer) {
        clearTimeout(hello_timer);
        hello_timer = null;
    }
    offline.value = false;
}

onMounted(() => {
    window.addEventListener('message', on_message);
    arm();
});
onBeforeUnmount(() => {
    window.removeEventListener('message', on_message);
    clear_timers();
});

const question = computed<string | null>(() => hello.value?.question ?? null);
const horizontal = computed<boolean>(() => props.layout === 'under');

function section_dragging(active: boolean): void {
    dragging.value = active;
    emit('dragging', active);
}

// ── the grip ────────────────────────────────────────────────────────────────
function wanted_from(event: PointerEvent): number | null {
    const box = pane.value?.getBoundingClientRect();
    if (!box) return null;
    return horizontal.value ? box.bottom - event.clientY : box.right - event.clientX;
}
function on_grip_down(event: PointerEvent): void {
    dragging.value = true;
    emit('dragging', true);
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    event.preventDefault();
}
function on_grip_move(event: PointerEvent): void {
    if (!dragging.value) return;
    const wanted = wanted_from(event);
    if (wanted !== null) emit('resize', wanted);
}
function on_grip_up(event: PointerEvent): void {
    if (!dragging.value) return;
    dragging.value = false;
    emit('dragging', false);
    const grip = event.currentTarget as HTMLElement;
    if (grip.hasPointerCapture(event.pointerId)) grip.releasePointerCapture(event.pointerId);
}
/** Keyboard twin of the drag — a pointer gesture is never the sole trigger. */
function nudge(delta: number): void {
    const box = pane.value?.getBoundingClientRect();
    if (!box) return;
    emit('resize', (horizontal.value ? box.height : box.width) + delta);
}
</script>

<template>
    <section
        ref="pane"
        class="canvas"
        :class="[`canvas--${layout}`, { 'canvas--dragging': dragging }]"
        aria-label="Agent canvas"
    >
        <div
            class="canvas__grip"
            role="separator"
            :aria-orientation="horizontal ? 'horizontal' : 'vertical'"
            aria-label="Resize the canvas"
            tabindex="0"
            title="Drag to resize · double-click to reset"
            @pointerdown="on_grip_down"
            @pointermove="on_grip_move"
            @pointerup="on_grip_up"
            @pointercancel="on_grip_up"
            @dblclick="emit('resize', null)"
            @keydown.up.prevent="horizontal && nudge(16)"
            @keydown.down.prevent="horizontal && nudge(-16)"
            @keydown.left.prevent="!horizontal && nudge(16)"
            @keydown.right.prevent="!horizontal && nudge(-16)"
        ></div>
        <header class="canvas__head">
            <span class="fui-techtext canvas__title">▤ CANVAS</span>
            <button v-if="map_available" class="fui-btn canvas__map-toggle" type="button"
                :aria-pressed="map_open" title="Show or hide the compact activity map below the 3D office"
                @click="toggle_map">MAP</button>
            <span
                v-if="question"
                class="fui-tag fui-tag--warn canvas__ask"
                :title="`The agent is asking: ${question}`"
            >
                🙋 {{ question }}
            </span>
            <button
                class="canvas__layout"
                type="button"
                :title="`Canvas placement: ${canvas_layout_label(layout)} — click for the next (under the list · a column · off)`"
                @click="emit('cycle')"
            >
                {{ canvas_layout_label(layout) }}
            </button>
        </header>
        <div class="canvas__content" tabindex="0" aria-label="3D office, artifacts, activity map and canvas content">
            <CanvasResizableSection section="office" label="3D office" default_height="clamp(360px, 100cqw, 720px)"
                @dragging="section_dragging">
                <AgentOfficeView embedded :rack="rack" :stale="stale" @select="emit('select', $event)" />
            </CanvasResizableSection>
            <CanvasResizableSection v-show="artifacts_visible || map_visible" section="details" label="Artifacts and activity map"
                default_height="clamp(280px, 75cqw, 420px)" @dragging="section_dragging">
                <CanvasDetailsRow :map_visible="map_visible" :artifacts_visible="artifacts_visible" @dragging="section_dragging">
                    <template #map><AgentActivityMap v-if="map_visible && agent" compact :agent="agent" :stale="stale" /></template>
                    <template #artifacts>
                        <AgentArtifactShelf v-if="artifacts_visible && artifact_shelf" :shelf="artifact_shelf" :bridge_origin="bridge_origin" />
                    </template>
                </CanvasDetailsRow>
            </CanvasResizableSection>
            <!-- The opened row's cards land here (the rack teleports them in): a lead's open decisions,
                 then the suggested next step. Empty, a slot takes no height. The cards are the rack's,
                 not the plugin page's, and the agent's own canvas doc below is untouched. -->
            <div :id="LEAD_DECISIONS_SLOT_ID" class="canvas__next" />
            <div :id="NEXT_STEP_SLOT_ID" class="canvas__next" />
            <CanvasResizableSection v-show="document_visible" section="document" label="Canvas content"
                default_height="clamp(240px, 70cqw, 480px)" @dragging="section_dragging">
                <p v-if="!src" class="canvas__note">NO AGENT FOCUSED</p>
                <p
                    v-else-if="offline"
                    class="canvas__note"
                    title="Add plugins/agent-canvas to ~/.config/tldrack/plugins.json, then restart the bridge"
                >
                    CANVAS OFFLINE · <span class="canvas__why">agent-canvas plugin not answering</span>
                </p>
                <iframe
                    v-if="src"
                    v-show="!offline"
                    :key="`${src}#${attempt}`"
                    ref="frame"
                    class="canvas__frame"
                    title="Agent canvas"
                    sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-downloads"
                    referrerpolicy="origin"
                    :src="src"
                />
            </CanvasResizableSection>
        </div>
    </section>
</template>

<style scoped>
/* Flush cell: no `.fui-panel`, whose drop shadow would smear onto the terminal. */
.canvas {
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
    background: var(--rack-surface);
    font-size: 12px;
}
.canvas--under {
    border-top: 1px solid rgba(147, 255, 212, 0.18);
}
.canvas--column {
    border-left: 1px solid rgba(147, 255, 212, 0.18);
}

.canvas__head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border-bottom: 1px solid rgba(147, 255, 212, 0.1);
    flex: 0 0 auto;
    min-width: 0;
}
.canvas__title {
    font-size: 0.68rem;
    letter-spacing: 0.12em;
    flex: 0 0 auto;
}
.canvas__ask {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.canvas__layout {
    margin-left: auto;
    flex: 0 0 auto;
    cursor: pointer;
    background: transparent;
    color: var(--color-accent-green, #93ffd4);
    border: 1px solid rgba(147, 255, 212, 0.35);
    border-radius: 2px;
    padding: 1px 8px;
    font: inherit;
    font-size: 0.68rem;
    letter-spacing: 0.12em;
}
.canvas__layout:hover {
    background: rgba(147, 255, 212, 0.16);
}

/* No `:empty` styling: Teleport keeps its own (empty) anchor text nodes in the target, so the slot
   is never `:empty` even with no card. The card carries its own edge and fill. */
.canvas__next {
    flex: 0 0 auto;
    min-width: 0;
}
.canvas__map-toggle { flex: 0 0 auto; min-height: 28px; }
.canvas__map-toggle[aria-pressed="true"] { color: var(--color-accent-green, #93ffd4); }
@media (pointer: coarse) {
    .canvas__map-toggle { min-height: 44px; }
}
/* The cards and views share a strip; the framed reader keeps its document scroll and dialogs. */
.canvas__content { flex: 1 1 auto; min-height: 0; min-width: 0; overflow: auto; scrollbar-width: none; container-type: inline-size; }
.canvas__content::-webkit-scrollbar { display: none; }
.canvas__frame {
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
    background: var(--rack-surface);
}
.canvas--dragging :deep(iframe), .canvas--dragging :deep(canvas) {
    pointer-events: none;
}
.canvas__note {
    margin: auto;
    padding: 12px;
    text-align: center;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    color: rgba(150, 195, 175, 0.85);
}
.canvas__why {
    text-transform: none;
    letter-spacing: 0;
    font-family: inherit;
}

/* The grip: a 9 px hit target over the leading edge, painted as a 1 px line —
   the same treatment as `.rack-window__grip`. */
.canvas__grip {
    position: absolute;
    z-index: 2;
    touch-action: none;
}
.canvas--under .canvas__grip {
    left: 0;
    right: 0;
    top: -4px;
    height: 9px;
    cursor: row-resize;
}
.canvas--column .canvas__grip {
    top: 0;
    bottom: 0;
    left: -4px;
    width: 9px;
    cursor: col-resize;
}
.canvas__grip::after {
    content: '';
    position: absolute;
    background: transparent;
    transition: background 120ms ease;
}
.canvas--under .canvas__grip::after {
    inset: 4px 0;
}
.canvas--column .canvas__grip::after {
    inset: 0 4px;
}
.canvas__grip:hover::after,
.canvas__grip:focus-visible::after,
.canvas--dragging .canvas__grip::after {
    background: rgba(147, 255, 212, 0.55);
}
.canvas__grip:focus-visible {
    outline: none;
}
</style>
