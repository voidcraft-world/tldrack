<script setup lang="ts">
/** Shared office renderer. The window supplies the rack it hears on the channel; the Canvas supplies its own. */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { PerspectiveCamera, Raycaster, Scene, Vector2, WebGLRenderer } from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { AgentOfficeScene } from '../engine/terminal/agent-office-scene.ts';
import { build_office, type OfficeAgent, type OfficeLayout } from '../engine/terminal/agent-office-logic.ts';
import { office_worktree_for, type OfficeAction, type OfficeRack } from '../engine/terminal/agent-office-link.ts';
import { useArmedAction } from '../composables/useArmedAction.ts';
import { office_menu, office_menu_key, OFFICE_MENU_ARM_MS, type OfficeMenuItem, type OfficeMenuItemId } from './office-menu.ts';
import AgentActivityMap from '../agent-rack/AgentActivityMap.vue';
import IslandMark from '../agent-rack/IslandMark.vue';
import { island_status } from '../engine/terminal/island-status.ts';
import { is_service_session } from '../engine/terminal/service-sessions.ts';
import { state_label } from '../engine/terminal/rack-board-logic.ts';
import { display_state_color_css } from '../config/terminal-status-flags.ts';
import { office_pointer_gesture } from './office-pointer.ts';
import { office_frame_due, office_pixel_ratio } from './office-render-quality.ts';
import { useRackPrefs } from '../composables/useRackPrefs.ts';

/** The camera's default pitch above the floor, looking down over the office. */
const OVERLOOK_PITCH_DEG = 52;

const props = defineProps<{ rack: OfficeRack | null; stale?: boolean; embedded?: boolean }>();
const emit = defineEmits<{
    (event: 'select', session: string): void;
    /** A menu verb. Whoever holds the rack's transport re-checks it (`agent-office-link.ts` `office_action_allowed`). */
    (event: 'action', action: OfficeAction, session: string): void;
}>();
const rack = computed(() => props.rack);
const stale = computed(() => props.stale === true);
const layout = computed<OfficeLayout | null>(() =>
    rack.value ? build_office(rack.value.sessions, rack.value.focused) : null,
);
const unavailable = ref(false);
const hovered = shallowRef<OfficeAgent | null>(null);
const tip = ref({ x: 0, y: 0 });
const sent = ref<string | null>(null);
const dismissed_map = ref<string | null>(null);
const inspected_session = computed(() => hovered.value?.session ?? rack.value?.focused ?? null);
const inspected_agent = computed(() => {
    const session = inspected_session.value;
    if (!session || session === dismissed_map.value || is_service_session(session)) return null;
    return rack.value?.sessions.find(row => row.session === session && row.live) ?? null;
});
watch(inspected_session, () => { dismissed_map.value = null; });

/** The operator's quality switch, live: owned by office-render-quality.ts — see the note there. */
const prefs = useRackPrefs();
const quality = computed(() => prefs.value.office_quality);

const host = ref<HTMLDivElement | null>(null);
let renderer: WebGLRenderer | null = null;
let camera: PerspectiveCamera | null = null;
let controls: OrbitControls | null = null;
let office: AgentOfficeScene | null = null;
let frame = 0;
let resize: ResizeObserver | null = null;
let density: MediaQueryList | null = null;
let stop_density_watch: (() => void) | null = null;
let visibility: IntersectionObserver | null = null;
let visible = false;
let scene: Scene | null = null;
/** Re-measure the drawing buffer; set once the renderer exists. */
let resize_buffer: (() => void) | null = null;
let last_draw_ms = -Infinity;
/** Once the operator orbits, a growing rack no longer re-frames the camera under them. */
let user_moved = false;
let framed_size = '';
const raycaster = new Raycaster();
const pointer = new Vector2();
let labels_dirty = true;
let last_labels_ms = 0;
function update_labels(now: number): void {
    if (!labels_dirty || now - last_labels_ms < 100 || !camera || !office || !host.value) return;
    last_labels_ms = now;
    labels_dirty = false;
    office.update_labels(camera, host.value.clientWidth, host.value.clientHeight);
}
/** Can the rack take a click right now? */
const usable = computed(() => rack.value?.reachable === true && !rack.value.frozen && !stale.value);
function select_session(session: string | null): void {
    if (!session || !usable.value) return;
    emit('select', session);
    sent.value = session;
    dismissed_map.value = null;
}
const gesture = office_pointer_gesture(props.embedded === true);

// ── the right-click menu ────────────────────────────────────────────────
//
// What it offers and in whose words: owned by office-menu.ts — see the note there.
/** The robot whose menu is open, and where on the page the menu is pinned. */
const menu = ref<{ session: string; x: number; y: number } | null>(null);
const menu_el = ref<HTMLElement | null>(null);
const menu_arm = useArmedAction(OFFICE_MENU_ARM_MS);
const menu_agent = computed(() => (menu.value ? agent_for(menu.value.session) : null));
const menu_items = computed<OfficeMenuItem[]>(() => {
    const agent = menu_agent.value;
    if (!agent) return [];
    const armed = (['select', 'kill', 'pr', 'cleanup'] as const)
        .find((id: OfficeMenuItemId) => menu_arm.armed.value === office_menu_key(agent.session, id)) ?? null;
    return office_menu(agent, office_worktree_for(rack.value, agent.session), armed, usable.value);
});
// An arm must not outlive its entry: a PR? that became CLEAN, a rack gone stale, a robot gone.
watch(menu_items, (items) => {
    const session = menu.value?.session;
    if (session && !menu_agent.value) return close_menu();
    menu_arm.retain(session ? items.filter(item => item.press === 'arm').map(item => office_menu_key(session, item.id)) : []);
}, { flush: 'sync' });

function menu_buttons(): HTMLButtonElement[] {
    return [...(menu_el.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
}
function open_menu(session: string | null, event: PointerEvent): void {
    close_menu();
    if (!session || !agent_for(session)) return;
    menu.value = { session, x: event.clientX, y: event.clientY };
    hovered.value = null;
    office?.set_hovered(session);
    void nextTick(() => {
        const el = menu_el.value;
        if (!el || !menu.value) return;
        // Pinned at the pointer, pulled back inside the page where it would hang off it.
        const box = el.getBoundingClientRect();
        menu.value = { session, x: Math.max(4, Math.min(menu.value.x, window.innerWidth - box.width - 4)),
            y: Math.max(4, Math.min(menu.value.y, window.innerHeight - box.height - 4)) };
        menu_buttons()[0]?.focus({ preventScroll: true });
    });
}
function close_menu(): void {
    if (!menu.value) return;
    menu.value = null;
    menu_arm.disarm();
    office?.set_hovered(null);
}
function press_menu_item(item: OfficeMenuItem): void {
    const session = menu.value?.session;
    if (!session || item.press === 'none') return;
    if (item.press === 'open') {
        if (item.url) window.open(item.url, '_blank', 'noopener');
        return close_menu();
    }
    if (item.id === 'select') {
        select_session(session);
        return close_menu();
    }
    const action = item.id;
    menu_arm.press(office_menu_key(session, action), () => {
        emit('action', action, session);
        close_menu();
    });
}
function on_menu_key(event: KeyboardEvent): void {
    if (event.key === 'Escape' || event.key === 'Tab') {
        event.preventDefault();
        close_menu();
        renderer?.domElement.focus({ preventScroll: true });
        return;
    }
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
    const buttons = menu_buttons();
    if (!buttons.length || (!step && event.key !== 'Home' && event.key !== 'End')) return;
    event.preventDefault();
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (at + step + buttons.length) % buttons.length;
    buttons[next].focus({ preventScroll: true });
}
/** A press anywhere off the menu leaves it. Capture phase: the canvas's own handlers still run. */
function on_document_press(event: Event): void {
    if (menu.value && !menu_el.value?.contains(event.target as Node | null)) close_menu();
}

function agent_for(session: string | null): OfficeAgent | null {
    if (!session || !layout.value) return null;
    for (const room of layout.value.rooms) {
        for (const desk of room.desks) if (desk.agent.session === session) return desk.agent;
    }
    return null;
}

/** Frame the whole office from the overlook pitch. */
function fit(): void {
    if (!camera || !controls || !layout.value) return;
    const radius = Math.max(layout.value.width, layout.value.depth, 6) / 2 + 2;
    const half_fov = (camera.fov * Math.PI) / 360;
    const fit_h = radius / Math.tan(half_fov);
    const fit_w = radius / (Math.tan(half_fov) * camera.aspect);
    const distance = Math.max(fit_h, fit_w) * 1.05;
    const pitch = (OVERLOOK_PITCH_DEG * Math.PI) / 180;
    controls.target.set(0, 0, 0);
    camera.position.set(0, distance * Math.sin(pitch), distance * Math.cos(pitch));
    camera.lookAt(0, 0, 0);
    controls.update();
}

function on_fit(): void {
    user_moved = false;
    fit();
}

function ray_at(event: PointerEvent): void {
    if (!renderer || !camera) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
}

function on_pointer_move(event: PointerEvent): void {
    let scroll = gesture.move(event);
    // touch-action:none leaves pinches with OrbitControls. Forward a single-finger swipe
    // through scrollable ancestors, including normal chaining at the strip's edges.
    for (let parent = host.value?.parentElement; parent && scroll !== 0; parent = parent.parentElement) {
        if (!/^(auto|scroll)$/.test(getComputedStyle(parent).overflowY)) continue;
        const before = parent.scrollTop;
        parent.scrollTop += scroll;
        scroll -= parent.scrollTop - before;
    }
    // An open menu holds the ring on its robot: the pointer is on its way to the menu.
    if (!office || gesture.pressed() || event.pointerType === 'touch' || menu.value) return;
    ray_at(event);
    const session = office.pick(raycaster);
    office.set_hovered(session);
    hovered.value = agent_for(session);
    tip.value = { x: event.clientX, y: event.clientY };
    if (renderer) renderer.domElement.style.cursor = session ? 'pointer' : '';
}

function on_pointer_down(event: PointerEvent): void {
    gesture.down(event);
    on_pointer_leave();
}

function on_pointer_up(event: PointerEvent): void {
    const tap = gesture.up(event);
    if (!tap || !office) return;
    ray_at(event);
    const session = office.pick(raycaster);
    if (tap === 'menu') open_menu(session, event);
    else select_session(session);
}

/** The menu is ours (`office-pointer.ts` decides it on release), never the browser's. */
function on_context_menu(event: Event): void {
    event.preventDefault();
}

function on_pointer_cancel(event?: PointerEvent): void {
    gesture.cancel(event?.pointerId);
    on_pointer_leave();
}

function on_pointer_leave(): void {
    if (!menu.value) office?.set_hovered(null);
    hovered.value = null;
}

function update_office(next: OfficeLayout | null): void {
    if (!office || !next) return;
    office.update(next);
    labels_dirty = true;
    update_resources();
    // Re-frame while the operator has not taken the camera, and only when the
    // office's footprint changed — never on a state flip.
    const size = `${next.width.toFixed(1)}x${next.depth.toFixed(1)}`;
    if (!user_moved && size !== framed_size) {
        framed_size = size;
        fit();
    }
    // A hovered agent's tooltip follows its live state.
    if (hovered.value) hovered.value = agent_for(hovered.value.session);
}
watch(layout, update_office);
function update_resources(): void {
    if (!office || !layout.value) return;
    const resources = rack.value?.resources;
    office.update_resources(layout.value, resources ? { ...resources, stale: resources.stale || stale.value || !rack.value?.reachable || !!rack.value?.frozen } : undefined);
}
watch(() => [rack.value?.resources, stale.value], update_resources);

function loop(now: number = performance.now()): void {
    frame = requestAnimationFrame(loop);
    // The camera advances on EVERY animation frame, whatever is drawn: OrbitControls' damping
    // decays per `update`, so stepping it only on drawn frames would make a `light` orbit coast
    // twice as long as a `sharp` one. The update is arithmetic; the frame is what costs.
    controls?.update();
    if (!office_frame_due(quality.value, now, last_draw_ms)) return;
    last_draw_ms = now;
    office?.tick(now);
    update_labels(now);
    if (renderer && camera && scene) renderer.render(scene, camera);
}
// A new quality is a new buffer size, and it lands on the next frame with no remount.
watch(quality, () => resize_buffer?.());

function update_running(): void {
    if (visible && !document.hidden) {
        if (!frame) loop();
    } else {
        cancelAnimationFrame(frame);
        frame = 0;
        if (document.hidden) on_pointer_cancel();
        else on_pointer_leave();
        close_menu();
    }
}

onMounted(() => {
    const el = host.value;
    if (!el) return;
    try {
        renderer = new WebGLRenderer({ antialias: true });
    } catch {
        unavailable.value = true;
        return;
    }
    const gl = renderer.getContext();
    const max_dimension = Math.min(gl.getParameter(gl.MAX_RENDERBUFFER_SIZE), ...gl.getParameter(gl.MAX_VIEWPORT_DIMS));
    el.appendChild(renderer.domElement);
    camera = new PerspectiveCamera(42, 1, 0.1, 2000);
    scene = new Scene();
    office = new AgentOfficeScene();
    scene.add(office.group);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enableZoom = true;
    // OrbitControls keeps touch-action:none for reliable pinch zoom. The pointer classifier
    // handles embedded one-finger scrolling; the standalone view keeps one-finger orbit.
    if (props.embedded) controls.touches.ONE = null;
    controls.maxPolarAngle = (82 * Math.PI) / 180; // never under the floor
    controls.minDistance = 3;
    controls.maxDistance = 400;
    controls.addEventListener('change', () => { labels_dirty = true; });
    controls.addEventListener('start', () => {
        user_moved = true;
    });

    const size = (): void => {
        if (!renderer || !camera) return;
        const width = Math.max(1, el.clientWidth);
        const height = Math.max(1, el.clientHeight);
        renderer.setDrawingBufferSize(width, height,
            office_pixel_ratio(width, height, window.devicePixelRatio, max_dimension, quality.value));
        renderer.domElement.style.width = '100%';
        renderer.domElement.style.height = '100%';
        labels_dirty = true;
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        if (!user_moved) fit();
    };
    update_office(layout.value);
    size();
    resize_buffer = size;
    resize = new ResizeObserver(size);
    resize.observe(el);
    // Moving between displays or changing browser zoom can change DPR without resizing the host.
    const density_changed = (): void => {
        size();
        density?.removeEventListener('change', density_changed);
        density = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
        density.addEventListener('change', density_changed);
    };
    density_changed();
    stop_density_watch = () => density?.removeEventListener('change', density_changed);

    renderer.domElement.addEventListener('pointermove', on_pointer_move);
    renderer.domElement.addEventListener('pointerdown', on_pointer_down);
    renderer.domElement.addEventListener('pointerup', on_pointer_up);
    renderer.domElement.addEventListener('pointerleave', on_pointer_leave);
    renderer.domElement.addEventListener('pointercancel', on_pointer_cancel);
    renderer.domElement.addEventListener('contextmenu', on_context_menu);
    renderer.domElement.addEventListener('wheel', close_menu, { passive: true });
    // Focusable, so Escape can hand the keyboard back to where the menu came from.
    renderer.domElement.tabIndex = -1;
    document.addEventListener('pointerdown', on_document_press, true);
    window.addEventListener('blur', close_menu);
    window.addEventListener('resize', close_menu);

    visibility = new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting;
        update_running();
    });
    visibility.observe(el);
    document.addEventListener('visibilitychange', update_running);
});

onBeforeUnmount(() => {
    cancelAnimationFrame(frame);
    resize_buffer = null;
    resize?.disconnect();
    stop_density_watch?.();
    visibility?.disconnect();
    document.removeEventListener('visibilitychange', update_running);
    document.removeEventListener('pointerdown', on_document_press, true);
    window.removeEventListener('blur', close_menu);
    window.removeEventListener('resize', close_menu);
    controls?.dispose();
    office?.dispose();
    if (renderer) {
        const canvas = renderer.domElement;
        canvas.removeEventListener('pointermove', on_pointer_move);
        canvas.removeEventListener('pointerdown', on_pointer_down);
        canvas.removeEventListener('pointerup', on_pointer_up);
        canvas.removeEventListener('pointerleave', on_pointer_leave);
        canvas.removeEventListener('pointercancel', on_pointer_cancel);
        canvas.removeEventListener('contextmenu', on_context_menu);
        canvas.removeEventListener('wheel', close_menu);
        renderer.dispose();
        canvas.remove();
    }
});

function state_color(agent: OfficeAgent): string {
    return display_state_color_css(agent.state);
}
</script>

<template>
    <div class="office" :class="{ 'office--embedded': embedded }" aria-label="3D agent office">
        <div ref="host" class="office__stage" />

        <button v-if="!unavailable && layout?.rooms.length" class="fui-btn office__fit" type="button"
            title="Frame the whole office · wheel or pinch to zoom" @click="on_fit">FIT</button>
        <span v-if="stale || (rack && !rack.reachable)" class="fui-tag fui-tag--warn office__stale">{{ stale ? 'STALE' : 'BRIDGE' }}</span>

        <section v-if="unavailable" class="fui-panel office__notice" role="status">3D unavailable · WebGL could not start</section>
        <section v-else-if="!rack || layout?.rooms.length === 0" class="fui-panel office__notice">
            <p class="fui-techtext office__notice-head">{{ rack ? 'NO AGENTS RUNNING' : 'WAITING FOR THE RACK' }}</p>
            <p class="office__notice-body">
                {{ rack
                    ? 'Start one from the rack’s LAUNCH row — it takes a desk here.'
                    : 'Keep a rack page open. It feeds this window.' }}
            </p>
        </section>

        <aside v-if="!embedded && inspected_agent" class="fui-panel office__map" aria-label="Selected agent activity">
            <button class="fui-btn office__map-close" type="button" aria-label="Close activity map"
                @click="dismissed_map = inspected_session">×</button>
            <AgentActivityMap :agent="inspected_agent" :stale="stale || !rack?.reachable || rack?.frozen" />
        </aside>

        <!-- The right-click menu. Entries and words: owned by office-menu.ts — see the note there. -->
        <div
            v-if="menu && menu_agent"
            ref="menu_el"
            class="fui-panel office__menu"
            role="menu"
            :aria-label="`Actions for ${menu_agent.label}`"
            :style="{ left: `${menu.x}px`, top: `${menu.y}px` }"
            @keydown="on_menu_key"
            @contextmenu.prevent
        >
            <span class="office__menu-name">{{ menu_agent.label }}</span>
            <span class="office__menu-state" :style="{ color: state_color(menu_agent) }">
                ● {{ state_label(menu_agent.state) }}<template v-if="menu_agent.island"> · <span
                    :style="{ color: island_status(menu_agent.island).color }">{{ island_status(menu_agent.island).label }}</span></template>
            </span>
            <button
                v-for="item in menu_items"
                :key="item.id"
                type="button"
                role="menuitem"
                :class="['office__menu-item', {
                    'office__menu-item--danger': item.tone === 'danger' || item.tone === 'error',
                    'office__menu-item--open': item.tone === 'open',
                    'office__menu-item--armed': item.armed,
                }]"
                :disabled="item.press === 'none'"
                @click="press_menu_item(item)"
            >
                <span class="office__menu-label">{{ item.label }}</span>
                <span class="office__menu-detail">{{ item.detail }}</span>
            </button>
            <span v-if="!usable" class="office__menu-hint">the rack cannot take a click right now</span>
        </div>

        <div
            v-if="hovered && !menu"
            class="fui-panel office__tip"
            :style="{ left: `${tip.x + 14}px`, top: `${tip.y + 14}px` }"
            role="tooltip"
        >
            <span class="office__tip-name">{{ hovered.label }}</span>
            <span class="office__tip-state" :style="{ color: state_color(hovered) }">
                ● {{ state_label(hovered.state) }}<template v-if="hovered.screen"> · {{ hovered.screen }}</template>
            </span>
            <span v-if="hovered.tool && hovered.pose !== 'idle'" class="office__tip-detail">
                {{ hovered.tool }}<template v-if="hovered.detail"> · {{ hovered.detail }}</template>
            </span>
            <template v-if="hovered.island">
                <IslandMark :island="hovered.island" />
                <span :style="{ color: island_status(hovered.island).color }">{{ island_status(hovered.island).label }}</span>
            </template>
            <span class="office__tip-hint">
                {{ !usable
                    ? 'the rack cannot take a click right now'
                    : `${sent === hovered.session ? 'in the rack’s pane' : 'click: open in the rack'} · right-click: actions` }}
            </span>
        </div>
    </div>
</template>

<style scoped>
.office {
    position: relative;
    height: 100%;
    background: #0b0f0e;
    color: var(--color-text-primary, #e4edf2);
    overflow: hidden;
}
.office__stage { position: absolute; inset: 0; }
/* The canvas takes focus only to hand the keyboard back from the menu: no ring for that. */
.office__stage :deep(canvas:focus) { outline: none; }
.office__fit { position: absolute; z-index: 3; top: 8px; right: 8px; min-height: 32px; }
.office__stale { position: absolute; left: 8px; bottom: 8px; }
@media (pointer: coarse) { .office__fit { min-height: 44px; min-width: 44px; } }
.office__notice {
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    max-width: min(420px, calc(100% - 40px));
    padding: 14px 18px;
    text-align: center;
}
.office__notice-head {
    margin: 0 0 6px;
    font-size: 0.78rem;
    letter-spacing: 0.12em;
}
.office__notice-body {
    margin: 0;
    font-size: 0.82rem;
    opacity: 0.8;
}
.office__map { position: absolute; right: 10px; bottom: 10px; width: min(390px, calc(100% - 20px)); max-height: calc(100% - 70px); display: flex; flex-direction: column; }
.office__map > .activity-map { max-height: min(480px, calc(100dvh - 110px)); }
.office__map-close { align-self: flex-end; min-width: 28px; min-height: 28px; }
@media (pointer: coarse) {
    .office__map-close { min-width: 44px; min-height: 44px; }
}
.office__tip {
    position: fixed;
    z-index: 5;
    display: grid;
    gap: 2px;
    padding: 6px 10px;
    max-width: 360px;
    pointer-events: none;
    font-size: 0.8rem;
}
.office__tip-name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.office__tip-detail {
    opacity: 0.75;
    overflow-wrap: anywhere;
}
.office__tip-hint {
    font-size: 0.72rem;
    opacity: 0.55;
}
.office__menu {
    position: fixed;
    z-index: 6;
    display: grid;
    gap: 2px;
    padding: 8px;
    width: min(320px, calc(100vw - 8px));
    font-size: 0.8rem;
}
.office__menu-name {
    padding: 0 6px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.office__menu-state { padding: 0 6px 6px; font-size: 0.72rem; }
.office__menu-item {
    display: grid;
    gap: 1px;
    min-height: 36px;
    padding: 5px 6px;
    border: 1px solid transparent;
    border-radius: 3px;
    background: none;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
}
.office__menu-item:hover:not(:disabled),
.office__menu-item:focus-visible { background: rgba(121, 192, 255, 0.16); outline: none; }
.office__menu-item:focus-visible { border-color: rgba(121, 192, 255, 0.55); }
.office__menu-item:disabled { opacity: 0.5; cursor: default; }
.office__menu-label { font-weight: 600; letter-spacing: 0.06em; color: #8cd9b0; }
.office__menu-detail { font-size: 0.72rem; opacity: 0.7; overflow-wrap: anywhere; }
.office__menu-item--open .office__menu-label { color: #42ffa8; }
.office__menu-item--danger .office__menu-label { color: #ff7b72; }
.office__menu-item--danger:hover:not(:disabled),
.office__menu-item--danger:focus-visible { background: rgba(255, 123, 114, 0.16); }
/* Armed is the rack row's amber (`AgentRackComponent.vue` `.row__btn--armed`), and it outranks every tone. */
.office__menu-item.office__menu-item--armed,
.office__menu-item.office__menu-item--armed:hover:not(:disabled),
.office__menu-item.office__menu-item--armed:focus-visible {
    border-color: rgba(248, 193, 70, 0.55);
    background: rgba(248, 193, 70, 0.14);
}
.office__menu-item--armed .office__menu-label { color: #f8c146; }
.office__menu-hint { padding: 4px 6px 0; font-size: 0.72rem; opacity: 0.55; }
@media (pointer: coarse) { .office__menu-item { min-height: 44px; } }
</style>
