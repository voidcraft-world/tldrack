<script setup lang="ts">
/**
 * The agent rack as a page: the list (`AgentRackComponent`) beside ttyd's own page at native 1:1.
 *
 * TWO FEEDS (`feed` prop), and the page does not care which:
 *
 *   • `bridge` (the default, and what `index.html` mounts): the page talks to a bridge over HTTP
 *     (`rack-http-transport.ts`). With no `?remote=` that bridge is the origin that served the
 *     page (`rack-remote.ts` `same_origin_config`), so a page the bridge serves needs no setup.
 *   • `bus`: the page is a satellite of a host application's main window on this machine. The
 *     fleet arrives over the window bus (`rack-bus-transport.ts`) and every action goes home. In
 *     this mode the window does no work: it never polls and never opens an EventSource, because
 *     the main window owns the one status poller and its notifications. `?remote=` still wins.
 *
 * ONE-LIVE-TERMINAL RULE: `ttyd_src` stays `about:blank` until the first payload lands. On the
 * bus a main window stands its own pane down on our HELLO, so that ordering is what guarantees
 * its ttyd socket is closed before this one opens.
 *
 * SHARPNESS: the rack pane is sized in whole DEVICE pixels, never a percentage. A
 * `flex-basis: 28%` put the terminal frame's raster layer at a fractional device-pixel offset on
 * nearly every window width, and Chrome then resampled the whole xterm canvas, which made the
 * text look soft next to a native terminal at the same size. For the same reason nothing sits
 * above the terminal: it starts at y = 0. Window chrome belongs in the LEFT RAIL, never in a bar
 * over the terminal.
 *
 * The `settings` slot is the host's: whatever it renders sits in the rack's settings tray
 * (slot props `open` and `bridge_origin`). This repository fills it with nothing.
 */
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue';
import AgentRackComponent from './AgentRackComponent.vue';
import { create_bus_transport } from './rack-bus-transport.ts';
import { create_http_transport } from './rack-http-transport.ts';
import { with_launch_directories } from './launch-directories-client.ts';
import {
    REMOTE_PARAM,
    clear_remote_config,
    is_loopback_host,
    parse_host_list,
    resolve_remote_config,
    resolved_ttyd_origin,
    same_origin_config,
} from './rack-remote.ts';
import { ALLOWED_REMOTE_HOSTS } from '../global/environment.ts';
import { useRackWindowGeometry } from './useRackWindowGeometry.ts';
import { useCompanionLifecycle } from '../companion/useCompanionLifecycle.ts';
import { useWindowBus } from '../composables/useWindowBus.ts';
import { COMPANION_BUS, type SatelliteMode } from '../companion/companion-bus.ts';
import { MESSAGE_TERMINAL_KEY, type TerminalSoftKey } from '../events/messages.ts';
import { useTtydFontStep } from '../composables/useTtydFontStep.ts';
import { useDevicePixelRail } from '../composables/useDevicePixelRail.ts';
import { SPLIT_STORAGE_KEY, TERM_MIN_PX, clamp_split_px, parse_split_px } from './rack-window-geometry.ts';
import AgentCanvasPane from './AgentCanvasPane.vue';
import { office_fleet } from '../engine/terminal/agent-office-link.ts';
import {
    CANVAS_LAYOUT_KEY,
    CANVAS_SIZE_KEY,
    DEFAULT_CANVAS_LAYOUT,
    LEAD_DECISIONS_SLOT_ID,
    NEXT_STEP_SLOT_ID,
    canvas_page_url,
    clamp_canvas_column_px,
    clamp_canvas_under_px,
    next_canvas_layout,
    parse_canvas_layout,
    parse_canvas_sizes,
    type CanvasLayout,
    type CanvasSizes,
} from './agent-canvas.ts';
import { bridge_origin, set_bridge_origin } from '../config/bridge-origin.ts';
import { MULTI_TERMINAL_FLAGS } from '../config/multi-terminal-flags.ts';
import { ttyd_url } from '../engine/terminal/multi-terminal-layout.ts';
import { held_pane_arg } from '../engine/terminal/rack-view.ts';
import {
    clamp_render_options,
    has_render_override,
    load_render_options,
    save_render_options,
    type TerminalRenderOptions,
} from '../engine/terminal/terminal-render-options.ts';
import { TTYD_FRAME_ALLOW } from '../engine/terminal/terminal-clipboard.ts';
import { with_payload_overlay, type RackPayload, type RackTransport } from './rack-transport.ts';
import { create_pane_settle } from './pane-settle.ts';

// History reads on demand, independently of fleet polling and notification ownership.
const AgentHistoryPane = defineAsyncComponent(() => import('./AgentHistoryPane.vue'));
const history_open = ref(false);

/** The beat a host's main window listens for; its silence budget is a multiple of this. */
const HEARTBEAT_MS = 3000;
/** How the host's HELLO handler recognises us. Typed so a rename of the mode
 *  can't silently desync the two sides into a satellite nobody heartbeats. */
const SATELLITE_MODE: SatelliteMode = 'agent-rack';
/** Floor for the rack pane, matching `.rack-window__list`'s min-width. Applied
 *  in JS BEFORE the device-pixel snap — a CSS floor applied afterwards would
 *  reintroduce the fractional offset the snap exists to remove. */
const RACK_MIN_PX = 220;
/** Below this the rail and the terminal stop fitting side by side and the layout
 *  switches to one-pane-at-a-time. Declared up here rather than beside the
 *  narrow refs because `initial_render_options()` runs during setup and would
 *  otherwise hit it in the temporal dead zone. */
const NARROW_PX = 720;
/** 18 px is tuned for a monitor at arm's length; on a phone it is small enough
 *  to be the reason you give up and walk to the machine. Applied ONLY when the
 *  operator has never chosen a size: a stored value always wins. */
const PHONE_FONT_SIZE = 24;
/** This window composites at 1:1, with no supersampling. Named and not inlined, so the
 *  contrast with a host that draws its frame at a multiple is explicit: same stored size,
 *  different device value on the wire (`device_font_size`). */
const POPOUT_DPI_SCALE = 1;

const bus = useWindowBus();

const props = withDefaults(defineProps<{
    /** Where the fleet comes from; the file header owns the two meanings. */
    feed?: 'bridge' | 'bus';
}>(), { feed: 'bridge' });

/**
 * The machines `?remote=` may name (`VITE_ALLOWED_REMOTE_HOSTS`). Empty means loopback only:
 * `global/environment.ts` owns why.
 */
const allowed_hosts = parse_host_list(ALLOWED_REMOTE_HOSTS);

/**
 * The bridge this document talks to over HTTP, or null for a bus satellite.
 *
 * A valid `?remote=` (or the one stored from an earlier visit) wins; `rack-remote.ts` validates
 * it, and a hostile or malformed one resolves to null there. Otherwise a `bridge` page talks to
 * the origin that served it. Read ONCE at setup, so every `if (remote)` below is stable for the
 * component's whole lifetime, which is what makes the conditional composable calls legal.
 */
const remote = resolve_remote_config(window.location.search, allowed_hosts)
    ?? (props.feed === 'bridge' ? same_origin_config(window.location) : null);
/** True when `remote` is the origin that served this page: there is no other machine to name or forget. */
const same_origin = remote !== null && remote.bridge_origin === window.location.origin;
// Every other client on this page (Laya's, the name card's) calls the same bridge.
if (remote) set_bridge_origin(remote.bridge_origin);
const http_transport = remote ? create_http_transport(remote) : null;
const transport: RackTransport = with_launch_directories(http_transport ?? create_bus_transport(), (path) => `${bridge_origin()}${path}`);

// This window owns the live pane, so it — not the host — knows which session
// the terminal iframe is still loading: `agent-rack/pane-settle.ts` owns why
// the rack locks that row's name box meanwhile. The child rack reads it off its payload.
const pane_settle = create_pane_settle({
    on_change: () => rack_transport.refresh_overlay(),
    grace_ms: () => MULTI_TERMINAL_FLAGS.pane_settle_grace_ms,
    cap_ms: () => MULTI_TERMINAL_FLAGS.pane_settle_cap_ms,
});
const rack_transport = with_payload_overlay(transport, () => {
    const loading = pane_settle.loading();
    return loading === null ? {} : { pane_loading: loading };
});

// Both of these are about being a satellite of a MAIN WINDOW ON THIS MACHINE,
// and neither applies on the HTTP feed:
//
//   • useCompanionLifecycle closes this document once the main window is PROVEN
//     gone. On the HTTP feed nothing is beating, so it would try to close the
//     tab on a timer: the watchdog firing would be the normal case, not the
//     crash case it was written for.
//   • useRackWindowGeometry samples and persists a rect for `window.open` to
//     restore. A browser tab has no rect anybody can restore.
let lifecycle: { main_lost: Ref<boolean> } | null = null;
if (!remote) {
    lifecycle = useCompanionLifecycle();
    useRackWindowGeometry();
}

/**
 * The main window stopped answering, and this window was kept open anyway
 * because you are looking at it (see `companion/lifecycle-logic.ts`).
 *
 * Surfacing it is not decoration. A main usually goes quiet because its timers
 * are throttled while it is hidden, not because anything died. Refusing to
 * close this window is only half the answer: a window that stays but silently
 * stops updating is the same mystery with extra steps.
 */
const main_lost = computed<boolean>(() => lifecycle?.main_lost.value === true);

const payload = ref<RackPayload | null>(null);
/** The main window has nothing registered to service an action. Structurally
 *  impossible on the HTTP feed, where the bridge answers directly and the
 *  transport always stamps `attached: true`: such a rack is never frozen, it is
 *  either reachable or stale. */
const frozen = computed<boolean>(() => payload.value?.attached === false);
const term_frame = ref<HTMLIFrameElement | null>(null);


/** The HTTP feed's equivalent of DETACHED: the bridge stopped answering, so these
 *  rows are the last good read. Deliberately keeps rendering them and does not
 *  blank them. */
const stale = computed<boolean>(() => !!remote && payload.value?.reachable === false);

// ── the connect screen ──────────────────────────────────────────────────────
//
// Without this, two ordinary situations end at a permanently blank rack with no
// explanation: opening the page with nothing answering, and pressing SWITCH
// MACHINE (which deliberately reloads to a bare URL).
//
// It is deliberately NOT gated on "looks like a phone". The honest condition is
// "nothing is feeding this rack", and the card says what this feed needs and
// offers every pinned machine instead of guessing which cure the reader wants.
// On a working rack the first payload lands within one ~3 s poll, so the delay
// below is comfortably past it and the panel never flashes.

const CONNECT_PROMPT_MS = 6000;
/** Shown on the connect card: the bridge a `bridge` page expected to answer. */
const page_origin = window.location.origin;

const connect_open = ref(false);

/**
 * One button per pinned machine — deliberately NOT a free-text field.
 *
 * With an exact-host pin the only input that can ever succeed is a name already
 * on this list, so a text box would exist purely to be typed into wrongly (on a
 * phone keyboard, with a 40-character host name). Offering the list also
 * makes the boundary legible: what you see is the complete set of machines this
 * build will talk to.
 */
function connect_to(host: string): void {
    window.location.search = `?${REMOTE_PARAM}=${encodeURIComponent(host)}`;
}

/** Which machine this page is driving. Worth surfacing: the whole point of the
 *  remote path is that the fleet is somewhere else, and a rack with no host on
 *  it looks exactly like a local one. */
const remote_host = computed<string>(() => {
    if (!remote) return '';
    try {
        return new URL(remote.ttyd_origin).hostname;
    } catch {
        return remote.ttyd_origin;
    }
});

/** The bridge is this machine, or the origin that served the page. The host tag would print
 *  `127.0.0.1`, which says nothing, and "forget this machine" has nowhere to go, so both drop. */
const remote_local = computed<boolean>(() => !!remote && (same_origin || is_loopback_host(remote_host.value)));

/** The dockbar draws only when it has something to say; an empty bordered strip
 *  under the list is chrome carrying zero bits. */
const dockbar_visible = computed<boolean>(() => !narrow.value || !remote_local.value || stale.value || canvas_off_door.value);
/** With the canvas OFF its own header (and the placement chip in it) is gone, so
 *  the dock bar carries the way back. Never on a narrow viewport. */
const canvas_off_door = computed<boolean>(() => !narrow.value && canvas_layout.value === 'off');

// ── render options ──────────────────────────────────────────────────────────
//
// Two refs, and the split is load-bearing. `render_options` is what the UI shows
// and what gets persisted; `nav_render` is the snapshot the URL is built from.
//
// If `ttyd_src` read the font size directly, every slider tick would change the
// src and RELOAD the pane — dropping the socket and throwing away the live
// resize path entirely. So a font-size change updates only `render_options` and
// is pushed over MESSAGE_TERMINAL_CONFIG (xterm resizes in place, socket
// untouched), while the renderer/family/weight — which ttyd only reads at load —
// update `nav_render` and deliberately do renavigate.

/**
 * A phone gets a bigger starting font — but only if the operator has never picked
 * one. `load_render_options` reads untouched fields through to the defaults, so
 * it cannot tell "never set" from "set to the default"; `has_render_override`
 * asks the store whether font_size is a TOUCHED field, and getting that wrong
 * would silently overwrite a size the operator deliberately chose.
 */
function initial_render_options(): TerminalRenderOptions {
    const stored = load_render_options();
    if (!remote || window.innerWidth > NARROW_PX) return stored;
    return has_render_override('font_size') ? stored : clamp_render_options({ ...stored, font_size: PHONE_FONT_SIZE });
}

const render_options = ref<TerminalRenderOptions>(initial_render_options());
const nav_render = ref<TerminalRenderOptions>({ ...render_options.value });

/** The pane's `?arg=`. NOT the bare session name — the attach-or-create hazard is
 *  owned by `pane_arg`, and this owner recomputes on every payload, so it holds
 *  the attach-only form it is already on (`held_pane_arg` owns why). */
const pane_target = computed<string | null>((previous) => {
    const data = payload.value;
    if (!data || data.focused === null) return null;
    return held_pane_arg(data.focused, previous, data.worktrees, new Set(data.attach_only ?? []));
});

const ttyd_src = computed<string>(() => {
    const data = payload.value;
    if (!data) return 'about:blank'; // see ONE-LIVE-TERMINAL RULE above
    // On the HTTP feed the origin replaces `http://localhost:<port>` wholesale. It has been
    // through `rack-remote.ts` already, and `ttyd_url` itself validates nothing.
    return ttyd_url(
        data.ttyd_port ?? 7681,
        pane_target.value,
        nav_render.value,
        POPOUT_DPI_SCALE,
        remote ? resolved_ttyd_origin(remote, data.ttyd_port) : undefined,
    );
});

// Every new `src` is a fresh ttyd page that grabs focus once it connects.
// Pre-flush, so the lock lands in the same render as the navigation.
watch(ttyd_src, (src) => {
    pane_settle.navigated(src === 'about:blank' ? null : (payload.value?.focused ?? null));
});

function on_term_load(): void {
    font_step.on_frame_load();
    pane_settle.loaded();
}

// A session switch renavigates the frame anyway, so re-seed the URL with the
// CURRENT size first. Without this the new pane would paint at whatever size
// was last baked into the URL and then visibly jump when the config push lands
// (the patched page applies ours up to one 250ms poll after ttyd's own).
// `flush: 'pre'` (the default) is what makes this land before `ttyd_src` is read.
watch(
    () => payload.value?.focused,
    () => {
        nav_render.value = { ...render_options.value };
    },
);

// Live font push, re-push on load, and ⌘± from inside the terminal: owned by
// composables/useTtydFontStep.ts — see the note there.
const font_step = useTtydFontStep({
    frame: term_frame,
    get_options: () => render_options.value,
    apply: apply_render,
    dpi_scale: POPOUT_DPI_SCALE,
});
const push_font_size = font_step.push_font_size;

function apply_render(next: TerminalRenderOptions): void {
    const previous = render_options.value;
    const clean = clamp_render_options(next);
    render_options.value = clean;
    save_render_options(clean);

    if (clean.font_size !== previous.font_size) push_font_size();
    // Which options are live vs load-time-only, and why a renavigate is safe:
    // owned by engine/terminal/terminal-render-options.ts — see the note there.
    if (
        clean.renderer !== previous.renderer ||
        clean.font_family !== previous.font_family ||
        clean.font_weight !== previous.font_weight ||
        clean.theme !== previous.theme
    ) {
        nav_render.value = { ...clean };
    }
}

// ── the split, and its device-pixel snapping ────────────────────────────────
//
// The rack pane is a whole DEVICE pixel wide so the terminal iframe beside it
// starts on the pixel grid: owned by composables/useDevicePixelRail.ts.
//
// DRAGGABLE, but the grip must never take flex space. Five CSS px of divider
// between the rail and the iframe would push the iframe's origin by 5·dpr
// device pixels — an integer at dpr 2, but 7.5 at dpr 1.5, which is exactly the
// fractional offset the snap exists to remove. So the grip is ABSOLUTELY
// positioned over the seam (see `.rack-window__grip`) and takes no part in the
// flex layout.
//
// `split_px` is null until the operator drags: with no opinion the split follows the
// window, and once there is one it is honoured literally at every window size.
// Persistence is localStorage — a per-device convenience for a per-device
// window, and it must survive being unavailable (private window, blocked site
// data), hence every access is wrapped.
const split_px = ref<number | null>(null);
try {
    split_px.value = parse_split_px(localStorage.getItem(SPLIT_STORAGE_KEY));
} catch {
    // Site data blocked — the split just does not persist. Never fatal.
}
const rack_rail = useDevicePixelRail(RACK_MIN_PX, () =>
    clamp_split_px(
        split_px.value ?? window.innerWidth * MULTI_TERMINAL_FLAGS.pop_out_rack_ratio,
        rack_space(),
        RACK_MIN_PX,
    ),
);
const rack_width = rack_rail.width;

/** True only while a drag is in flight — see `.rack-window__body--dragging`. */
const splitting = ref(false);
const split_body = ref<HTMLElement | null>(null);

function persist_split(): void {
    try {
        if (split_px.value === null) localStorage.removeItem(SPLIT_STORAGE_KEY);
        else localStorage.setItem(SPLIT_STORAGE_KEY, String(split_px.value));
    } catch {
        // As above: a split that cannot be remembered still works this session.
    }
}

/** Set the split from a viewport X, measured against the SPLIT CONTAINER rather
 *  than the window — the body sits at x = 0 today, and this keeps it correct if
 *  window chrome ever appears to its left. */
function split_to(client_x: number): void {
    const left = split_body.value?.getBoundingClientRect().left ?? 0;
    split_px.value = clamp_split_px(client_x - left, rack_space(), RACK_MIN_PX);
    rack_rail.measure(); // re-snap: a drag lands on fractional CSS px constantly
}

function on_grip_down(event: PointerEvent): void {
    if (narrow.value) return;
    splitting.value = true;
    // Pointer capture is what keeps the move stream on the grip once the cursor
    // is over the terminal. The iframe is a separate document, which is the one
    // case where capture alone is worth backing up — see the `--dragging` rule.
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    event.preventDefault();
}
function on_grip_move(event: PointerEvent): void {
    if (!splitting.value) return;
    split_to(event.clientX);
}
function on_grip_up(event: PointerEvent): void {
    if (!splitting.value) return;
    splitting.value = false;
    const grip = event.currentTarget as HTMLElement;
    if (grip.hasPointerCapture(event.pointerId)) grip.releasePointerCapture(event.pointerId);
    persist_split(); // once per gesture, not once per pointermove
}
/** Back to following the window. The accessible twin of "drag it back roughly". */
function reset_split(): void {
    split_px.value = null;
    rack_rail.measure();
    persist_split();
}
/** Keyboard equivalent of the drag: the grip is focusable, so the split is not
 *  mouse-only. A pointer gesture is never the sole trigger. */
function nudge_split(delta: number): void {
    split_px.value = clamp_split_px((split_px.value ?? rack_rail.px.value) + delta, rack_space(), RACK_MIN_PX);
    rack_rail.measure();
    persist_split();
}

// ── narrow (phone) layout ───────────────────────────────────────────────────
//
// The wide split is a fixed device-pixel rail beside the terminal. On a
// ~400 px phone that leaves ~180 px of terminal, which is not a terminal — so
// below NARROW_PX the two become full-width and only one is shown at a time.
//
// The device-pixel snapping above simply does not apply here: it exists because
// a fractional offset makes Chrome resample the xterm canvas next to a native
// terminal at the same size, and neither half of that comparison is true on a
// phone. `rack_width` is ignored while narrow (the pane is `width: 100%`).

const narrow = ref(false);
const touch = ref(false);
/** Which pane a narrow viewport is showing. Terminal-first: someone who opened
 *  this on a phone came to type at an agent, not to read a list. */
const mobile_pane = ref<'list' | 'terminal'>('terminal');

let narrow_query: MediaQueryList | null = null;
let touch_query: MediaQueryList | null = null;

function on_narrow_change(event: MediaQueryListEvent | MediaQueryList): void {
    narrow.value = event.matches;
}
function on_touch_change(event: MediaQueryListEvent | MediaQueryList): void {
    touch.value = event.matches;
}

// ── agent canvas ────────────────────────────────────────────────────────────
//
// An HTML pane per agent, framed from the `agent-canvas` bridge plugin — owned
// by `plugins/agent-canvas/README.md`, placement rules by
// `agent-rack/agent-canvas.ts`. Wide viewports only: a narrow one has no room
// for a third surface, and from another machine its answers are refused anyway.
//
// It does no work here either. The framed page polls its own bridge; this
// window only decides where the pane sits and how big it is.

const canvas_layout = ref<CanvasLayout>(DEFAULT_CANVAS_LAYOUT);
const canvas_sizes = ref<CanvasSizes>({ under_px: null, column_px: null });
try {
    canvas_layout.value = parse_canvas_layout(localStorage.getItem(CANVAS_LAYOUT_KEY));
    canvas_sizes.value = parse_canvas_sizes(localStorage.getItem(CANVAS_SIZE_KEY));
} catch {
    // Site data blocked — the canvas keeps its defaults this session.
}
const canvas_bridge_origin = remote?.bridge_origin ?? bridge_origin();
const canvas_shown = computed<boolean>(() => !narrow.value && canvas_layout.value !== 'off');
const canvas_src = computed<string | null>(() =>
    canvas_page_url(canvas_bridge_origin, payload.value?.focused ?? null),
);
const canvas_fleet = computed(() => payload.value ? office_fleet(payload.value) : null);
const canvas_agent = computed(() => payload.value?.sessions.find(row => row.session === payload.value?.focused) ?? null);
const canvas_map_stale = computed(() => payload.value?.reachable !== true || payload.value?.attached === false);
const canvas_dragging = ref(false);
const list_column = ref<HTMLElement | null>(null);
const canvas_under_px = ref(0);
const canvas_column_px = ref(0);

/** The width the rack/terminal split may use: the window, less a canvas column. */
function rack_space(): number {
    const column = canvas_shown.value && canvas_layout.value === 'column' ? canvas_column_px.value : 0;
    return window.innerWidth - column;
}

function measure_canvas(): void {
    const column_h = list_column.value?.clientHeight ?? window.innerHeight;
    canvas_under_px.value = clamp_canvas_under_px(canvas_sizes.value.under_px ?? Number.NaN, column_h);
    canvas_column_px.value = clamp_canvas_column_px(
        canvas_sizes.value.column_px ?? Number.NaN,
        window.innerWidth,
        rack_rail.px.value,
        TERM_MIN_PX,
    );
}

function persist_canvas(): void {
    try {
        localStorage.setItem(CANVAS_LAYOUT_KEY, canvas_layout.value);
        localStorage.setItem(CANVAS_SIZE_KEY, JSON.stringify(canvas_sizes.value));
    } catch {
        // As with the split: unremembered, still working.
    }
}

/** Re-fit both splits — the canvas column and the rack rail share one width. */
function refit_canvas(): void {
    measure_canvas();
    rack_rail.measure();
}

function cycle_canvas(): void {
    canvas_layout.value = next_canvas_layout(canvas_layout.value);
    persist_canvas();
    void nextTick(refit_canvas);
}

function on_canvas_resize(wanted_px: number | null): void {
    const key: keyof CanvasSizes = canvas_layout.value === 'column' ? 'column_px' : 'under_px';
    canvas_sizes.value = { ...canvas_sizes.value, [key]: wanted_px };
    refit_canvas();
    // A null is a reset and a nudge is one keypress; a drag persists on release.
    if (!canvas_dragging.value) persist_canvas();
}

function on_canvas_dragging(active: boolean): void {
    canvas_dragging.value = active;
    if (!active) persist_canvas();
}

onMounted(() => {
    window.addEventListener('resize', refit_canvas);
    void nextTick(refit_canvas);
});
onBeforeUnmount(() => window.removeEventListener('resize', refit_canvas));

/**
 * Soft keys.
 *
 * An Android keyboard has no Esc, Tab, Ctrl or arrows, and Claude Code needs Esc
 * and Ctrl-C constantly — so without this a rack on a phone can display an agent
 * but not answer one.
 *
 * Sent as a TOKEN; the bridge's injected script owns the byte sequence. See
 * `MESSAGE_TERMINAL_KEY` for why that direction is a security boundary rather
 * than a preference, and why the list has no Enter on it.
 *
 * `'*'` as the target origin for the same reason `push_font_size` uses it: the
 * iframe can be cross-origin (ttyd's origin against this page's).
 */
const SOFT_KEYS: ReadonlyArray<{ key: TerminalSoftKey; label: string; title: string }> = [
    { key: 'escape', label: 'ESC', title: 'Escape — interrupt Claude Code' },
    { key: 'tab', label: 'TAB', title: 'Tab — complete' },
    { key: 'ctrl_c', label: '^C', title: 'Ctrl-C — cancel' },
    { key: 'up', label: '↑', title: 'Up — previous command' },
    { key: 'down', label: '↓', title: 'Down — next command' },
];

function send_soft_key(key: TerminalSoftKey): void {
    const win = term_frame.value?.contentWindow;
    if (!win) return;
    win.postMessage({ type: MESSAGE_TERMINAL_KEY, key }, '*');
}

function dock(): void {
    // Tell the host first, so its own pane is already coming back as this
    // window goes away; then close ourselves, which is always permitted for a
    // script-opened window.
    bus.post(COMPANION_BUS.RACK_CLOSING);
    window.close();
}

/**
 * The HTTP feed's replacement for RETURN: there is no main window to return
 * to, and `window.close()` is refused for a tab the operator opened themselves.
 *
 * Forgets the stored origin and reloads to a bare page, which is also the only
 * way to re-point the page at a different machine (and the recovery path if a
 * host name changes). The reload is to `pathname` alone so a stale `?remote=`
 * in the address bar cannot immediately re-adopt what was just cleared.
 */
function disconnect(): void {
    clear_remote_config();
    window.location.replace(window.location.pathname);
}

let heartbeat: ReturnType<typeof setInterval> | null = null;
let connect_timer: ReturnType<typeof setTimeout> | null = null;
let unsubscribers: Array<() => void> = [];

onMounted(() => {
    unsubscribers.push(
        transport.subscribe((next) => {
            payload.value = next;
            document.title = next.summary ? `${next.summary} — tldrack` : 'tldrack';
            // Data arrived, so "nothing is feeding this rack" is no longer true.
            // Without this a main window that took its time coming up would find
            // the connect panel parked over a working rack.
            connect_open.value = false;
            if (connect_timer) {
                clearTimeout(connect_timer);
                connect_timer = null;
            }
        }),
    );

    // Narrow layout + touch. Both are live queries rather than one-shot reads:
    // a phone rotating to landscape crosses NARROW_PX, and a tablet with a
    // keyboard attached crosses `hover`.
    narrow_query = window.matchMedia(`(max-width: ${NARROW_PX}px)`);
    narrow_query.addEventListener('change', on_narrow_change);
    on_narrow_change(narrow_query);
    touch_query = window.matchMedia('(hover: none)');
    touch_query.addEventListener('change', on_touch_change);
    on_touch_change(touch_query);

    // The connect panel. Armed on a timer, not shown at once, because on a
    // healthy rack the first payload lands well inside CONNECT_PROMPT_MS and
    // the panel must never flash there.
    connect_timer = setTimeout(() => {
        if (!payload.value) connect_open.value = true;
    }, CONNECT_PROMPT_MS);
    // ── main-window negotiation ────────────────────────────────────────────
    //
    // Every line below is a conversation with a host's main window sharing
    // this browser's BroadcastChannel. On the HTTP feed there is none: the
    // bridge is the peer, so posting HELLO would announce us to nobody and the
    // heartbeat would beat into an empty channel. Skipped wholesale and not
    // left running: a satellite that claims to exist is what makes a main
    // window stand its own pane down, and this document is not that satellite.
    if (!remote) {
        // The host asked for the rack back. A message and not the host calling
        // win.close(), because after a main reload it no longer holds our handle.
        unsubscribers.push(bus.on(COMPANION_BUS.RACK_CLOSE, () => window.close()));

        // HELLO is not optional: a main window gates its MAIN_ALIVE heartbeat on
        // having seen one, and useCompanionLifecycle acts on that heartbeat's
        // silence. It also tells the host to stand its own pane down.
        bus.post(COMPANION_BUS.HELLO, { mode: SATELLITE_MODE });

        // Announce again the instant a reloaded main comes back, without waiting
        // up to a full heartbeat. That gap matters: the reloaded main rebuilds
        // "a rack window is open" from our beat, and if it built its own pane
        // before it heard us there would be a SECOND live ttyd pane until the
        // next beat landed.
        unsubscribers.push(bus.on(COMPANION_BUS.MAIN_READY, () => announce()));
        // "Are you still there?" — the main stopped hearing our beat and is
        // asking before it concludes we closed. Answering is what keeps a
        // satellite parked BEHIND the main window from being declared dead when
        // the browser throttles its timers: this handler is event dispatch, so
        // it runs at full speed no matter what the interval below is doing.
        // Without it the main stands its own pane back up under a live
        // satellite: two ttyd connections on one session.
        unsubscribers.push(bus.on(COMPANION_BUS.SATELLITE_PING, () => announce()));
        window.addEventListener('focus', announce);

        heartbeat = setInterval(() => beat(), HEARTBEAT_MS);
        beat();
        // An extra beat the moment visibility flips, so a window-switch lands in
        // well under the 3s cadence. The interval above is throttled while this
        // window is hidden, so it is also the LAST beat the main hears before
        // the throttle bites. Which is exactly why the
        // main asks (SATELLITE_PING) rather than concluding from the gap.
        document.addEventListener('visibilitychange', beat);
        window.addEventListener('focus', beat);
        window.addEventListener('blur', beat);

        // The fast path for "closed with the OS chrome". A force-quit fires nothing
        // at all, which is what the host's silence timeout is the backstop for.
        window.addEventListener('pagehide', notify_closing);
        window.addEventListener('beforeunload', notify_closing);
    }

});

function notify_closing(): void {
    bus.post(COMPANION_BUS.RACK_CLOSING);
}

/**
 * One heartbeat, carrying whether this window is actually being LOOKED AT. The
 * main window feeds that into the ingestor's `is_watching`, so a session
 * finishing in the pane you are staring at does not also raise a `done`
 * highlight.
 *
 * `document.hasFocus()` is REQUIRED, not belt-and-braces: a rack parked on a
 * second monitor has `hidden === false` forever, and without the focus test it
 * would suppress every `done` for its focused session for as long as it sits
 * there.
 */
function beat(): void {
    bus.post(COMPANION_BUS.RACK_ALIVE, { visible: !document.hidden && document.hasFocus() });
}

/**
 * "I exist, and I am the agent rack." Idempotent on the host side.
 *
 * Beats through `beat()` rather than posting a bare RACK_ALIVE, because the host
 * reads an absent `visible` as TRUE. That guess is right for the `focus`
 * listener and wrong everywhere else this is called — answering a SATELLITE_PING
 * in particular, which happens precisely when this window has been sitting
 * hidden long enough for its timers to be throttled.
 */
function announce(): void {
    bus.post(COMPANION_BUS.HELLO, { mode: SATELLITE_MODE });
    beat();
}

onBeforeUnmount(() => {
    pane_settle.dispose();
    if (heartbeat) {
        clearInterval(heartbeat);
        heartbeat = null;
    }
    window.removeEventListener('pagehide', notify_closing);
    window.removeEventListener('beforeunload', notify_closing);
    window.removeEventListener('focus', announce);
    narrow_query?.removeEventListener('change', on_narrow_change);
    narrow_query = null;
    touch_query?.removeEventListener('change', on_touch_change);
    touch_query = null;
    if (connect_timer) {
        clearTimeout(connect_timer);
        connect_timer = null;
    }
    unsubscribers.forEach((fn) => fn());
    unsubscribers = [];
    // The bus transport owns no resources; the HTTP one owns two intervals and
    // an EventSource, and unsubscribing a listener would leave all three running.
    http_transport?.dispose();
});
</script>

<template>
    <div class="rack-window" :class="{ 'rack-window--narrow': narrow }">
        <!-- CONNECT: nothing is feeding this rack. Reached when no payload arrives (a bus page
             with no main window, a bridge that stopped) or by pressing SWITCH MACHINE. The card
             says what this feed needs, then offers every pinned machine. -->
        <section v-if="connect_open" class="connect">
            <div class="fui-panel connect__card">
                <span class="fui-corner tl" />
                <span class="fui-corner tr" />
                <span class="fui-corner bl" />
                <span class="fui-corner br" />
                <h1 class="fui-techtext connect__title">▚ TLDRACK</h1>
                <p class="connect__lead">Nothing is feeding this rack.</p>
                <p v-if="props.feed === 'bus'" class="connect__note">
                    On this machine: open the application this window belongs to and pop the rack
                    out again.
                </p>
                <p v-else class="connect__note">
                    The bridge at <code>{{ page_origin }}</code> is not answering. Start it with
                    <code>tldrack up</code>, then reload this page.
                </p>
                <!-- The complete set of machines this build will talk to. A pinned list rather
                     than a text field: with an exact-host pin nothing else could be accepted,
                     so the list is kinder on a phone keyboard and shows the trust boundary. -->
                <template v-if="allowed_hosts.length">
                    <p class="connect__note connect__note--or">or drive a machine over your tailnet</p>
                    <div class="connect__hosts">
                        <button
                            v-for="host in allowed_hosts"
                            :key="host"
                            class="connect__host"
                            type="button"
                            @click="connect_to(host)"
                        >
                            <span class="connect__host-name">{{ host }}</span>
                            <span class="connect__host-go">CONNECT →</span>
                        </button>
                    </div>
                    <p class="connect__hint">
                        That machine needs <code>tailscale serve</code> in front of a running
                        <code>tldrack</code>, and this device must be on the same tailnet.
                    </p>
                </template>
            </div>
        </section>

        <main
            v-else
            ref="split_body"
            class="rack-window__body"
            :class="{
                'rack-window__body--show-term': narrow && mobile_pane === 'terminal',
                'rack-window__body--dragging': splitting || canvas_dragging,
            }"
        >
            <div
                ref="list_column"
                class="rack-window__list"
                :style="{ flexBasis: narrow ? undefined : rack_width }"
            >
                <!-- The rail is a wrapper, not noise. AgentRackComponent's root
                     is `height: 100%` on purpose; as a direct flex child beside
                     the dock bar that 100% would resolve against the WHOLE
                     column and overflow by the bar's height. A flex item with a
                     resolved height gives it something correct to measure
                     against, so the shared component needs no change. -->
                <div class="rack-window__rail">
                    <AgentRackComponent
                        :transport="rack_transport"
                        :metrics_visible="!history_open && (!narrow || mobile_pane === 'terminal')"
                        :render_options="render_options"
                        :next_step_target="canvas_shown ? `#${NEXT_STEP_SLOT_ID}` : null"
                        :lead_decisions_target="canvas_shown ? `#${LEAD_DECISIONS_SLOT_ID}` : null"
                        @update:render_options="apply_render"
                    >
                        <template #settings="{ open }"><slot name="settings" :open="open" :bridge_origin="remote?.bridge_origin" /></template>
                    </AgentRackComponent>
                </div>
                <!-- AGENT CANVAS, under the list — the default placement. -->
                <AgentCanvasPane
                    v-if="canvas_shown && canvas_layout === 'under'"
                    layout="under"
                    :src="canvas_src"
                    :fleet="canvas_fleet"
                    :agent="canvas_agent"
                    :stale="canvas_map_stale"
                    :session="payload?.focused ?? null"
                    :bridge_origin="canvas_bridge_origin"
                    :style="{ flex: `0 0 ${canvas_under_px}px` }"
                    @select="rack_transport.send('select', $event)"
                    @cycle="cycle_canvas"
                    @resize="on_canvas_resize"
                    @dragging="on_canvas_dragging"
                />

                <!-- Window chrome lives in the RAIL, never in a bar above the
                     terminal — see the note on `.rack-window__term`. -->
                <footer v-if="dockbar_visible" class="rack-window__dockbar">
                    <button v-if="!narrow" class="rack-window__dock" type="button"
                        :aria-pressed="history_open" title="Search conversation history, including closed sessions"
                        @click="history_open = !history_open">HISTORY</button>
                    <button
                        v-if="canvas_off_door"
                        class="rack-window__link rack-window__switch"
                        title="Show the agent canvas again (under the list · a column · off)"
                        @click="cycle_canvas"
                    >
                        ▤ CANVAS
                    </button>
                    <!-- STALE and DETACHED are different failures and must not
                         share a label: DETACHED means a main window stopped
                         servicing actions, STALE means the bridge stopped
                         answering. The HTTP feed can only ever be the latter. -->
                    <span
                        v-if="stale"
                        class="fui-tag rack-window__tag"
                        :title="`${remote_host} stopped answering — these rows are the last good read`"
                    >
                        STALE
                    </span>
                    <span
                        v-else-if="remote && !remote_local"
                        class="fui-tag rack-window__tag rack-window__tag--remote"
                        :title="`Driving ${remote_host}: the bridge is the source, no main window needed`"
                    >
                        {{ remote_host || 'REMOTE' }}
                    </span>
                    <!-- A THIRD failure, and the most severe of the three, so it
                         outranks DETACHED: there is no main window answering at
                         all. The window stays and says so: a hidden main's
                         heartbeat timer is usually throttled, not dead. -->
                    <span
                        v-else-if="main_lost"
                        class="fui-tag rack-window__tag rack-window__tag--alert"
                        title="The main window stopped answering. This window stays open; click back into the main window (or reload it) to reconnect."
                    >
                        MAIN LOST
                    </span>
                    <span v-else-if="frozen" class="fui-tag rack-window__tag">DETACHED</span>
                    <!-- Not "DISCONNECT", which reads like ending the terminal session:
                         what it does is forget the machine and pick one again. -->
                    <button
                        v-if="remote && !remote_local"
                        class="rack-window__dock"
                        :title="`Forget ${remote_host} and choose another machine`"
                        @click="disconnect"
                    >
                        ⇄ SWITCH MACHINE
                    </button>
                    <button
                        v-else-if="!remote"
                        class="rack-window__dock"
                        title="Close this window and hand the rack back to the main window"
                        @click="dock"
                    >
                        ⤓ RETURN
                    </button>
                </footer>
            </div>
            <!-- THE SPLIT HANDLE. Absolutely positioned over the seam, and
                 deliberately NOT a flex child: a divider that took 5 px of the
                 row would move the iframe's origin by 5·dpr device pixels —
                 fine at dpr 2, fractional at dpr 1.5 — which is the exact blur
                 the device-pixel rail exists to prevent. It straddles the
                 border `.rack-window__list` already draws, so the seam stays
                 one line wide and only its colour changes on hover. -->
            <div
                v-if="!narrow"
                class="rack-window__grip"
                :style="{ left: rack_width }"
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize the agent list"
                tabindex="0"
                title="Drag to resize · double-click to reset"
                @pointerdown="on_grip_down"
                @pointermove="on_grip_move"
                @pointerup="on_grip_up"
                @pointercancel="on_grip_up"
                @dblclick="reset_split"
                @keydown.left.prevent="nudge_split(-16)"
                @keydown.right.prevent="nudge_split(16)"
                @keydown.home.prevent="reset_split"
            ></div>

            <!-- ttyd's own page, at native DPI, in a frame. Not an xterm.js of this
                 page's own: ttyd already serves a terminal, and a second one here
                 would be a ~345 KB chunk and a second status subscriber. -->
            <iframe
                ref="term_frame"
                class="rack-window__term"
                :inert="history_open"
                title="Agent terminal"
                :allow="TTYD_FRAME_ALLOW"
                :src="ttyd_src"
                @load="on_term_load"
            />
            <!-- AGENT CANVAS as a third column — AFTER the terminal, so the
                 terminal's origin never leaves the device-pixel grid
                 (`agent-canvas.ts` owns why). -->
            <AgentCanvasPane
                v-if="canvas_shown && canvas_layout === 'column'"
                :inert="history_open"
                layout="column"
                :src="canvas_src"
                :fleet="canvas_fleet"
                :agent="canvas_agent"
                :stale="canvas_map_stale"
                :session="payload?.focused ?? null"
                :bridge_origin="canvas_bridge_origin"
                :style="{ flex: `0 0 ${canvas_column_px}px` }"
                @select="rack_transport.send('select', $event)"
                @cycle="cycle_canvas"
                @resize="on_canvas_resize"
                @dragging="on_canvas_dragging"
            />
            <AgentHistoryPane v-if="history_open" class="rack-window__history"
                :style="{ left: narrow ? '0' : rack_width }" :bridge_origin="canvas_bridge_origin"
                @close="history_open = false" />
        </main>

        <!-- NARROW-VIEWPORT BAR — the pane switch, plus the keys an Android
             keyboard does not have.

             BELOW the terminal, never above it: chrome stacked above the pane
             has to be pinned to an integer height forever or it puts the
             iframe's raster layer at a fractional device-pixel Y offset. A footer changes the pane's
             HEIGHT but never its origin, so the constraint does not apply.

             It is also the only always-visible surface while the terminal is
             showing — the rail (and its dock bar) is off screen then — so the
             pane switch has to live here rather than in `.rack-window__dockbar`. -->
        <footer v-if="narrow && !connect_open" class="mobilebar">
            <div class="mobilebar__panes" role="group" aria-label="Pane">
                <button
                    class="mobilebar__pane"
                    :class="{ 'mobilebar__pane--on': !history_open && mobile_pane === 'list' }"
                    type="button"
                    @click="history_open = false; mobile_pane = 'list'"
                >
                    LIST
                </button>
                <button
                    class="mobilebar__pane"
                    :class="{ 'mobilebar__pane--on': !history_open && mobile_pane === 'terminal' }"
                    type="button"
                    @click="history_open = false; mobile_pane = 'terminal'"
                >
                    TERM
                </button>
                <button class="mobilebar__pane" :class="{ 'mobilebar__pane--on': history_open }"
                    type="button" @click="history_open = !history_open">HISTORY</button>
            </div>
            <!-- Only while the terminal is on screen: these type into it, and a
                 key that silently goes nowhere is worse than an absent one. -->
            <div v-if="mobile_pane === 'terminal' && !history_open" class="mobilebar__keys">
                <button
                    v-for="soft in SOFT_KEYS"
                    :key="soft.key"
                    class="mobilebar__key"
                    type="button"
                    :title="soft.title"
                    @click="send_soft_key(soft.key)"
                >
                    {{ soft.label }}
                </button>
            </div>
        </footer>
    </div>
</template>

<style scoped>
/* No @import here: Vite scopes @imports inside scoped styles, which silently
   strips the shared FUI classes. They are loaded globally by `styles/boot.ts`. */
.rack-window {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--rack-base);
    color: #f4f7ff;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.rack-window__history {
    position: absolute;
    inset: 0 0 0 auto;
    z-index: 5;
}

.rack-window__tag {
    flex: 0 0 auto;
    color: #f8c146;
    border-color: rgba(248, 193, 70, 0.5);
}

/* The rack's own footer, holding the window chrome. Height here is FREE: it changes only where
   the rail's own content ends, and can never move the terminal iframe on the
   Y axis. See `.rack-window__term`. */
.rack-window__dockbar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    padding: 8px 10px;
    border-top: 1px solid rgba(147, 255, 212, 0.18);
}

.rack-window__dock {
    flex: 1 1 auto;
    text-align: center;
    cursor: pointer;
    background: transparent;
    color: var(--color-accent-green, #93ffd4);
    border: 1px solid currentColor;
    border-radius: 2px;
    padding: 4px 12px;
    font: inherit;
    font-size: 11px;
    letter-spacing: 0.12em;
}
.rack-window__dock:hover { background: rgba(147, 255, 212, 0.16); }
.rack-window__dock:active { transform: translateY(1px); }
.rack-window__switch { margin-left: auto; }

.rack-window__body {
    display: flex;
    flex: 1 1 auto;
    /* Without this a long session list stretches the row instead of scrolling
       inside it, and the terminal loses height as the fleet grows. */
    min-height: 0;
    /* The positioning context the split grip hangs off. Costs the flex layout
       nothing, which is the point — see `.rack-window__grip`. */
    position: relative;
}

/* The split handle: a 9 px hit target centred on the 1 px seam, laid OVER the
   layout rather than in it. `left` is the snapped rail width, and the negative
   margin centres the target without moving anything. */
.rack-window__grip {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 9px;
    margin-left: -4px;
    z-index: 2;
    cursor: col-resize;
    /* Without this a touch drag pans the window instead of moving the split. */
    touch-action: none;
}

/* Paint the 1 px line INSIDE the hit area, never the hit area itself — a 9 px
   band of colour under the cursor would read as a scrollbar. */
.rack-window__grip::after {
    content: '';
    position: absolute;
    inset: 0 4px;
    background: transparent;
    transition: background 120ms ease;
}
.rack-window__grip:hover::after,
.rack-window__grip:focus-visible::after,
.rack-window__body--dragging .rack-window__grip::after {
    background: rgba(147, 255, 212, 0.55);
}
.rack-window__grip:focus-visible {
    outline: none; /* the ::after line IS the focus ring, at the right width */
}

/* A cross-document iframe is the one place `setPointerCapture` is worth backing
   up: while a drag is in flight the terminal must not swallow the move stream
   the moment the cursor crosses into it. */
.rack-window__body--dragging .rack-window__term {
    pointer-events: none;
}

/* `flex-basis` is set inline from `rack_width` — a whole DEVICE pixel, never a
   percentage. `min-width` is only a backstop: the same RACK_MIN_PX floor is
   applied in JS BEFORE the snap, because a CSS floor winning afterwards would
   put back exactly the fractional offset the snap removes. */
.rack-window__list {
    display: flex;
    flex-direction: column;
    flex-grow: 0;
    flex-shrink: 0;
    min-width: 220px;
    min-height: 0;
    border-right: 1px solid rgba(147, 255, 212, 0.18);
}

/* Owns the height AgentRackComponent's `height: 100%` resolves against. */
.rack-window__rail {
    flex: 1 1 auto;
    min-height: 0;
}

/* `.fui-panel` is styled for a FLOATING panel: a `0 8px 32px` drop shadow, an
   accent glow and a 3px radius. The rail is neither floating nor detached — it
   is a flush, full-bleed cell against the terminal — so the shadow would smear
   onto the pane beside it and the radius would clip against square window
   corners. Shadow, radius and border go; the glass background and the
   `.fui-corner` brackets — the parts that make it read as FUI — stay. */
.rack-window__list :deep(.rack) {
    box-shadow: none;
    border-radius: 0;
    border: 0;
    --fui-glass-fill: var(--rack-surface);
}

.rack-window__link {
    cursor: pointer;
    background: transparent;
    color: #8b98a5;
    border: 1px solid currentColor;
    border-radius: 2px;
    padding: 1px 6px;
    font: inherit;
    font-size: 10px;
    letter-spacing: 0.1em;
}
.rack-window__link:hover:not(:disabled) { background: rgba(255, 255, 255, 0.08); }

/* Sits at y = 0 with nothing above it, which is the whole point: a bar across
   the top has to be pinned to an INTEGER height, because a fractional one puts
   this iframe's raster layer at a fractional device-pixel Y offset and Chrome
   resamples the entire xterm canvas. Chrome in the left rail cannot do that at
   any height. Keep it that way: new window chrome belongs in
   `.rack-window__dockbar`. */
.rack-window__term {
    flex: 1 1 auto;
    min-width: 0;
    border: 0;
    background: var(--rack-base);
}

/* The same amber every other "this needs your attention" control in this window
   uses (the armed ✕, an armed CONFIRM?), so the alert vocabulary stays one
   colour. */
.rack-window__tag--alert {
    color: #f8c146;
    border-color: rgba(248, 193, 70, 0.55);
    background: rgba(248, 193, 70, 0.14);
}

.rack-window__tag--remote {
    color: #93ffd4;
    border-color: rgba(147, 255, 212, 0.5);
    /* A host name is longer than DETACHED and must not push the button
       off the bar. */
    max-width: 55%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* ── narrow (phone) layout ──────────────────────────────────────────────────
   Driven by the `narrow` ref rather than a bare media query, because the pane
   TOGGLE needs the same boolean — one source of truth beats a media query and a
   matchMedia that can disagree at the boundary.

   `rack_width` and its device-pixel snapping are inert here: they exist so the
   terminal's raster layer starts on the pixel grid next to a native terminal at
   the same size, and neither half of that comparison applies on a phone. */
.rack-window--narrow .rack-window__list {
    flex: 1 1 auto;
    min-width: 0;
    border-right: 0;
}
.rack-window--narrow .rack-window__term {
    display: none;
}
.rack-window--narrow .rack-window__body--show-term > .rack-window__list {
    display: none;
}
.rack-window--narrow .rack-window__body--show-term > .rack-window__term {
    display: block;
    flex: 1 1 auto;
}

/* TOUCH HAS NO HOVER. `AgentRackComponent` shows each row's PR/✕ on
   `.row:hover`, so without this only the FOCUSED row would show its actions on
   a phone and every other session would be unkillable. The name's rename box is
   drawn on hover too, so it is drawn always here: a door you cannot see is not
   a door.

   Fixed here, in the wrapper, and not in the component: that file is shared
   VERBATIM with a framed rack and must stay that way. `:deep()` is the seam for
   exactly this (see `.rack-window__list :deep(.rack)`). */
@media (hover: none) {
    .rack-window__list :deep(.row__actions) {
        display: flex;
    }
    .rack-window__list :deep(.row__name:not(.row__name--static)) {
        border-color: rgba(244, 247, 255, 0.22);
    }
    /* Fitts's law, not taste: a 15px glyph is not a tap target. */
    .rack-window__list :deep(.row__btn),
    .rack-window__list :deep(.row__name) {
        min-width: 30px;
        min-height: 30px;
    }
}

/* ── the narrow-viewport bar ────────────────────────────────────────────────
   BELOW the terminal. See the template comment: a footer changes the pane's
   height but never its raster origin, which is what the no-top-bar rule is
   actually about. */
.mobilebar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    padding: 6px 8px;
    /* Keeps the bar clear of the Android gesture pill / iOS home indicator. */
    padding-bottom: calc(6px + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid rgba(147, 255, 212, 0.18);
    background: var(--rack-base);
}

.mobilebar__panes {
    display: flex;
    flex: 0 0 auto;
    border: 1px solid rgba(147, 255, 212, 0.35);
    border-radius: 2px;
    overflow: hidden;
}

.mobilebar__pane {
    cursor: pointer;
    background: transparent;
    color: #8b98a5;
    border: 0;
    padding: 7px 11px;
    font: inherit;
    font-size: 11px;
    letter-spacing: 0.1em;
    min-height: 44px;
}
.mobilebar__pane--on {
    background: rgba(147, 255, 212, 0.18);
    color: #93ffd4;
}

.mobilebar__keys {
    display: flex;
    gap: 6px;
    flex: 1 1 auto;
    justify-content: flex-start;
    min-width: 0;
    overflow-x: auto;
}

.mobilebar__key {
    cursor: pointer;
    background: transparent;
    color: var(--color-accent-green, #93ffd4);
    border: 1px solid currentColor;
    border-radius: 2px;
    /* 34px square: the smallest thing a thumb hits reliably. */
    min-width: 34px;
    min-height: 34px;
    padding: 0 6px;
    font: inherit;
    font-size: 12px;
    letter-spacing: 0.06em;
}
.mobilebar__key:active {
    background: rgba(147, 255, 212, 0.22);
}

/* ── connect screen ─────────────────────────────────────────────────────────
   The one surface here that IS a floating panel, so unlike the rail and the dock
   bar it keeps `.fui-panel`'s shadow and brackets rather than overriding them. */
.connect {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 20px;
}

.connect__card {
    position: relative;
    width: min(440px, 100%);
    padding: 20px 18px;
}

.connect__title {
    margin: 0 0 14px;
    font-size: 13px;
    letter-spacing: 0.18em;
    color: #93ffd4;
}

.connect__lead {
    margin: 0 0 12px;
    font-size: 15px;
}

.connect__note {
    margin: 0 0 10px;
    font-size: 12px;
    line-height: 1.5;
    color: #8b98a5;
}

.connect__note--or {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 16px 0 8px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.connect__note--or::before,
.connect__note--or::after {
    content: '';
    flex: 1 1 auto;
    height: 1px;
    background: rgba(147, 255, 212, 0.18);
}

.connect__hosts {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.connect__host {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    cursor: pointer;
    background: rgba(0, 0, 0, 0.35);
    color: #f4f7ff;
    border: 1px solid rgba(147, 255, 212, 0.35);
    border-radius: 2px;
    /* Comfortably past the 44px tap-target floor — this is the primary action
       on a phone and it is being pressed one-handed. */
    min-height: 46px;
    padding: 8px 12px;
    font: inherit;
    text-align: left;
}
.connect__host:active {
    background: rgba(147, 255, 212, 0.18);
    border-color: #93ffd4;
}

/* A host name can be long; let it wrap and not overflow a 400px screen. */
.connect__host-name {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 13px;
    overflow-wrap: anywhere;
}

.connect__host-go {
    flex: 0 0 auto;
    color: var(--color-accent-green, #93ffd4);
    font-size: 10px;
    letter-spacing: 0.12em;
}


.connect__hint {
    margin: 14px 0 0;
    font-size: 11px;
    line-height: 1.5;
    opacity: 0.55;
}
.connect__hint code {
    color: #93ffd4;
}
</style>
