/**
 * How a ttyd pane should RENDER: font size, font stack, weight, colour theme, and which xterm
 * renderer backs it. One shared, persisted setting for every surface that embeds a terminal.
 *
 * WHY THIS EXISTS. The bridge's patched ttyd page SWALLOWS Cmd/Ctrl +/-/0 (preventDefault and
 * stopImmediatePropagation) and posts `MESSAGE_TERMINAL_FONT_STEP` to `window.parent`. A pane
 * with no listener eats the keystroke, which is worse than an unpatched page.
 *
 * ── HOW THE OPTIONS REACH ttyd ─────────────────────────────────────────────
 * ttyd 1.7.x merges its client options in this order (verified by reading the
 * page it serves):
 *
 *   applyPreferences({ ...defaults, ...serverPrefs, ...parseOptsFromUrlQuery() })
 *
 * so THE URL QUERY WINS over the bridge's `-t fontSize=…` flags. That is what
 * makes every knob here work against an already-running bridge — no restart, no
 * `index_patch.py` change. `parseOptsFromUrlQuery` coerces each value by the
 * *default's* `typeof`, so `fontSize` arrives as a number while `fontFamily` /
 * `fontWeight` / `rendererType` arrive as strings, and `theme` — object-typed —
 * goes through `JSON.parse`.
 *
 * Two properties of that parser worth knowing before adding a knob:
 *
 *   • Numbers go through `Number.parseInt(n, 10)`, so a fractional value is
 *     silently truncated. `lineHeight=1.2` arrives as `1`. Anything fractional
 *     cannot use this path at all.
 *   • Object-typed options are MERGED, not replaced (`applyPreferences` does
 *     `Object.assign` when the default is an Object) — see TERMINAL_THEME.
 *
 * The two paths are NOT interchangeable:
 *
 *   • `font_size` is pushed LIVE over MESSAGE_TERMINAL_CONFIG. The patched
 *     page's `apply_font_size` sets `term.options.fontSize` and refits, so the
 *     WebSocket stays up and nothing scrolls. The URL value is only the
 *     pre-connect seed.
 *   • `font_family` / `font_weight` / `renderer` / `theme` are NOT in the
 *     config-message contract, so they ride the URL and take effect on load.
 *     Changing one renavigates the frame — acceptable for set-and-forget
 *     tuning, because `?arg=<session>` is attach-or-create and reattaches to
 *     the very same tmux session.
 *
 * ── WHY THE DEFAULTS ARE WHAT THEY ARE ─────────────────────────────────────
 * The typeface is the system mono (SF Mono on macOS): side by side in a pane it renders sharper
 * than Monaco, with Menlo a close second. Weight stays 400: macOS dropped subpixel antialiasing
 * in Mojave, so a native terminal rasterizes with grayscale antialiasing at 400 too, and a
 * heavier weight "to put back stroke weight" has no basis. Size is 18, larger than a native
 * terminal's usual 12pt, because a rack is read from further back and on large displays. The
 * slider and ⌘± are the override. The colours: `TERMINAL_THEMES` owns them.
 *
 * Three.js-free and import-free, like its neighbour `multi-terminal-layout.ts`, so `node --test`
 * runs it directly in strip-only mode. That also means NO enums, NO parameter properties, and
 * no reading `localStorage` at module scope.
 */

/** xterm renderer backends ttyd's `setRendererType` understands. Its switch has
 *  exactly three cases (`canvas`, `webgl`, `dom`); any other value matches none
 *  and is a silent no-op — on first load that leaves xterm's built-in DOM
 *  renderer, which is why the third value is spelled `dom` rather than left
 *  implicit.
 *
 *  There is no `webgpu`: ttyd 1.7.7 bundles xterm ~5.4, and no xterm WebGPU
 *  renderer exists (`@xterm/addon-webgpu` 404s on npm; xterm 6.0 ships none).
 *  The WebGL addon is already GPU-drawn — Chrome runs WebGL on Metal via ANGLE. */
export type TerminalRenderer = 'webgl' | 'canvas' | 'dom';

/** Selectable renderers, in the order the UI should offer them. */
export const TERMINAL_RENDERERS: TerminalRenderer[] = ['webgl', 'canvas', 'dom'];

export interface TerminalRenderOptions {
    /** LOGICAL px — what the user sees on screen. A surface that supersamples
     *  its iframe multiplies this by its DPI scale at push time
     *  (`device_font_size`); it is never stored pre-multiplied, or a 1× page
     *  would inherit a supersampled surface's 2× value. */
    font_size: number;
    font_family: string;
    font_weight: number;
    renderer: TerminalRenderer;
    /** A name from `TERMINAL_THEMES`, never the colours themselves. */
    theme: TerminalThemeId;
}

/** The font-size ladder every ttyd pane steps along under ⌘±. */
export const DEFAULT_TERMINAL_FONT_SIZE = 18;
export const MIN_TERMINAL_FONT_SIZE = 8;
export const MAX_TERMINAL_FONT_SIZE = 40;
export const TERMINAL_FONT_STEP_PX = 2;

/** The patched ttyd page ignores a live font push outside 8..120 DEVICE px
 *  (index_patch.py). A supersampled surface can multiply its way past that, so
 *  `device_font_size` clamps at the boundary rather than trusting the caller. */
export const MIN_BRIDGE_FONT_PX = 8;
export const MAX_BRIDGE_FONT_PX = 120;

/**
 * The font stacks offered by the rack's FAMILY dropdown, in menu order. The
 * first entry is the default (`DEFAULT_RENDER_OPTIONS` reads it — nothing else
 * may index this list for "the default").
 *
 * Only fonts actually present on macOS are listed (`system_profiler
 * SPFontsDataType`: Monaco, Menlo, PT Mono, Andale Mono, Courier New, and
 * `.SF NS Mono`) — an absent family just falls through its stack, but offering
 * one that can never win is a menu entry that silently does nothing.
 *
 * NOTE the SF Mono entry leads with `ui-monospace`, and that is not
 * interchangeable with the name. The system mono is registered as
 * `.SF NS Mono`; browsers do not match dot-prefixed system families by name, so
 * a stack written `"SF Mono", …` quietly falls through to its next entry.
 */
export const TERMINAL_FONT_FAMILIES: Array<{ label: string; stack: string }> = [
    { label: 'SF Mono', stack: 'ui-monospace, SFMono-Regular, Menlo, monospace' },
    { label: 'Menlo', stack: 'Menlo, Monaco, monospace' },
    { label: 'Monaco', stack: 'Monaco, Menlo, monospace' },
    { label: 'PT Mono', stack: '"PT Mono", Menlo, monospace' },
];

/** One complete xterm colour theme: the four base keys, the selection pair and all 16 ANSI slots. */
export interface TerminalTheme {
    background: string;
    foreground: string;
    cursor: string;
    /** The glyph UNDER a block cursor. The background, so the cursor reads as a solid tile. */
    cursorAccent: string;
    selectionBackground: string;
    selectionForeground: string;
    black: string;
    red: string;
    green: string;
    yellow: string;
    blue: string;
    magenta: string;
    cyan: string;
    white: string;
    brightBlack: string;
    brightRed: string;
    brightGreen: string;
    brightYellow: string;
    brightBlue: string;
    brightMagenta: string;
    brightCyan: string;
    brightWhite: string;
}

/**
 * The xterm colour themes the settings tray offers, by name.
 *
 * - `graphite`, the default: neutral grey on the rack's own graphite, so the terminal reads as
 *   part of the page and takes no side on colour.
 * - `mint`: the rack's first theme, in the colours of a dark iTerm2 profile with mint text. Kept
 *   as a pick for anyone who used the rack when it was the only one.
 *
 * WHY AN OPTION CARRIES A NAME AND NOT THE COLOURS. Options cross a `postMessage` boundary and a
 * `BroadcastChannel`, so every field of `TerminalRenderOptions` needs a validator in
 * `clamp_render_options` before it is trusted. A name is one validator; 22 colour keys would be
 * a 22-key validator that buys nothing, because nothing in the UI tunes a single colour.
 *
 * EVERY THEME MUST STAY COMPLETE, never trimmed to "just the keys that differ". ttyd's
 * `applyPreferences` MERGES object-typed options (`Object.assign`), so a key omitted here is
 * not reset to xterm's default: it keeps whatever the bridge's own `-t theme=` put there, and a
 * partial theme silently inherits half of another palette. The 16 ANSI entries are the real
 * payoff: without them colours 0-15 fall through to xterm.js's washed-out defaults, and an
 * agent CLI's output is almost entirely ANSI-coloured.
 *
 * Adding a theme: add it here. `TerminalThemeId`, the validator and the menu follow.
 */
export const TERMINAL_THEMES = {
    graphite: {
        background: '#1f2125',
        foreground: '#e6e9ed',
        cursor: '#e6e9ed',
        cursorAccent: '#1f2125',
        selectionBackground: '#3d4a5c',
        selectionForeground: '#ffffff',
        black: '#23262b',
        red: '#e5645f',
        green: '#7fc97a',
        yellow: '#e3c15a',
        blue: '#6aa5ee',
        magenta: '#c58ae0',
        cyan: '#56c2c9',
        white: '#c9ced6',
        brightBlack: '#6c7480',
        brightRed: '#f08b86',
        brightGreen: '#a0dd9b',
        brightYellow: '#f0d584',
        brightBlue: '#92bdf5',
        brightMagenta: '#d9aaee',
        brightCyan: '#84d9de',
        brightWhite: '#ffffff',
    },
    mint: {
        background: '#2c2c2c',
        foreground: '#acfcd9',
        cursor: '#ffffff',
        cursorAccent: '#2c2c2c',
        selectionBackground: '#b3d7ff',
        selectionForeground: '#000000',
        black: '#14191e',
        red: '#b43c2a',
        green: '#00c200',
        yellow: '#c7c400',
        blue: '#2744c7',
        magenta: '#c040be',
        cyan: '#00c5c7',
        white: '#c7c7c7',
        brightBlack: '#686868',
        brightRed: '#dd7975',
        brightGreen: '#58e790',
        brightYellow: '#ece100',
        brightBlue: '#a7abf2',
        brightMagenta: '#e17ee1',
        brightCyan: '#60fdff',
        brightWhite: '#ffffff',
    },
} satisfies Record<string, TerminalTheme>;

export type TerminalThemeId = keyof typeof TERMINAL_THEMES;

/** Selectable themes, in the order the UI offers them. The first is the default. */
export const TERMINAL_THEME_IDS = Object.keys(TERMINAL_THEMES) as TerminalThemeId[];

export const DEFAULT_TERMINAL_THEME: TerminalThemeId = 'graphite';

/** The default theme's colours, for a surface that paints its own frame to match the pane. */
export const TERMINAL_THEME: TerminalTheme = TERMINAL_THEMES[DEFAULT_TERMINAL_THEME];

export const DEFAULT_RENDER_OPTIONS: TerminalRenderOptions = {
    font_size: DEFAULT_TERMINAL_FONT_SIZE,
    font_family: TERMINAL_FONT_FAMILIES[0].stack,
    font_weight: 400,
    renderer: 'webgl',
    theme: DEFAULT_TERMINAL_THEME,
};

/**
 * Stored under `-v3` as TOUCHED FIELDS ONLY — the fields the user actually
 * changed — never as a whole options blob.
 *
 * WHY: a whole blob outlives every future default. `clamp_render_options` falls
 * back per field only when a field is MISSING or invalid, so anyone who had ever
 * touched the FONT slider stored a perfectly valid `font_family` too, and the
 * next default change looked like a no-op for them. That forced a key bump on
 * every default change (`-v2` was the second); storing only what was touched
 * ends the treadmill, because an untouched field is simply absent and reads
 * through to whatever the default is today.
 *
 * "Touched" means DIFFERENT FROM THE PREVIOUS EFFECTIVE VALUE at save time —
 * deliberately NOT "different from the default". Diffing against the default
 * would forget an explicit pick that happens to equal today's default (⌘0 back
 * to 18 after a phone bump to 22 must stick), and the phone font bump in
 * AgentRackWindow keys off "is font_size an override", which needs that pick
 * remembered. A field, once touched, is never dropped.
 *
 * It is also what lets an option arrive later: `theme` was added after `-v3` existed, and a
 * stored blob without it simply reads through to the default.
 */
export const RENDER_OPTIONS_STORAGE_KEY = 'tldrack:terminal-render-options-v3';

/** The pre-touched-field key: a WHOLE blob. Read (never written) as the
 *  fallback when `-v3` is absent, minus its `font_family` — that field is the
 *  one whose default moved (Monaco → SF Mono) and the v2 blob cannot say
 *  whether it was ever picked. Removed on the first `-v3` save. */
export const LEGACY_RENDER_OPTIONS_STORAGE_KEY = 'tldrack:terminal-render-options-v2';

/** The fields a user can override, in a FIXED order so serialization is
 *  canonical (see `serialize_render_options`). */
const RENDER_FIELDS = ['font_size', 'font_family', 'font_weight', 'renderer', 'theme'] as const;
export type RenderField = (typeof RENDER_FIELDS)[number];

/** The touched fields, each already validated. */
export type RenderOverrides = Partial<TerminalRenderOptions>;

/** The slice of `Storage` this module uses — injectable so tests need no DOM. */
export interface RenderOptionsStorage {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
    removeItem(key: string): void;
}

function clamp(value: number, low: number, high: number): number {
    if (high < low) return low;
    return Math.min(Math.max(value, low), high);
}

/**
 * `Number(x)` is the wrong coercion for absent values and must not be used
 * directly here: `Number(null)`, `Number('')` and `Number(false)` are all `0`,
 * a perfectly finite number that then CLAMPS to the minimum. A missing font
 * size would silently become 8px rather than falling back to the default —
 * caught by the "never propagates a NaN" test, which is why it checks `null`.
 *
 * Only a real number, or a string that is entirely a number, counts.
 */
function to_number(value: unknown): number | null {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '') {
        const n = Number(value);
        return Number.isFinite(n) ? n : null;
    }
    return null;
}

/** Clamp a LOGICAL font size to the slider's range. Unusable input falls back
 *  to the default rather than propagating a NaN — a NaN would reach the URL as
 *  `fontSize=NaN`, which ttyd's `parseInt` turns back into NaN and assigns to
 *  xterm, blanking the pane. */
export function clamp_font_size(size: unknown): number {
    const n = to_number(size);
    if (n === null) return DEFAULT_TERMINAL_FONT_SIZE;
    return Math.round(clamp(n, MIN_TERMINAL_FONT_SIZE, MAX_TERMINAL_FONT_SIZE));
}

function is_renderer(value: unknown): value is TerminalRenderer {
    return value === 'webgl' || value === 'canvas' || value === 'dom';
}

/** `Object.hasOwn`, so `constructor` or `__proto__` off a hostile payload is not a theme. */
function is_theme_id(value: unknown): value is TerminalThemeId {
    return typeof value === 'string' && Object.hasOwn(TERMINAL_THEMES, value);
}

/**
 * Coerce anything — a parsed localStorage blob, a postMessage payload, a
 * partial patch — into a complete, safe options object.
 *
 * Every field falls back INDEPENDENTLY, so one corrupt key cannot discard the
 * other three. This is the same "garbage never leaks downstream" rule
 * `rack-window-geometry.ts::parse_rect` follows, and for the same reason: these
 * values cross a postMessage boundary and a BroadcastChannel, so nothing about
 * their shape is guaranteed.
 */
export function clamp_render_options(raw: unknown): TerminalRenderOptions {
    if (!raw || typeof raw !== 'object') return { ...DEFAULT_RENDER_OPTIONS };
    const candidate = raw as Record<string, unknown>;

    const weight = to_number(candidate.font_weight);
    // 100..900 is the CSS numeric weight range; xterm passes the value straight
    // into a canvas font shorthand, where an out-of-range number makes the
    // WHOLE declaration fail to parse and the glyph atlas silently falls back
    // to 10px sans-serif.
    const font_weight =
        weight === null
            ? DEFAULT_RENDER_OPTIONS.font_weight
            : Math.round(clamp(weight, 100, 900) / 100) * 100;

    const family = candidate.font_family;
    const font_family =
        typeof family === 'string' && family.trim() !== ''
            ? family.trim()
            : DEFAULT_RENDER_OPTIONS.font_family;

    return {
        font_size: clamp_font_size(candidate.font_size),
        font_family,
        font_weight,
        renderer: is_renderer(candidate.renderer) ? candidate.renderer : DEFAULT_RENDER_OPTIONS.renderer,
        theme: is_theme_id(candidate.theme) ? candidate.theme : DEFAULT_RENDER_OPTIONS.theme,
    };
}

/**
 * The font size to hand ttyd, in DEVICE px, for a surface supersampling its
 * iframe by `dpi_scale`.
 *
 * A surface that draws its frame at `dpi_scale`× logical pixels and scales it back down needs
 * this: an uncompensated `fontSize=18` renders 18px glyphs into a 2× buffer that is then halved,
 * about 9 logical px on screen.
 */
export function device_font_size(font_size: number, dpi_scale: number): number {
    const raw = to_number(dpi_scale);
    const scale = raw !== null && raw > 0 ? raw : 1;
    return Math.round(clamp(clamp_font_size(font_size) * scale, MIN_BRIDGE_FONT_PX, MAX_BRIDGE_FONT_PX));
}

/**
 * The ttyd client options as URL query pairs — WITHOUT a leading `?` or `&`, so
 * the caller owns the join. `ttyd_url`'s `voidcraft` branch carries no `?arg=`
 * and therefore needs its own `?`, a trap already documented there.
 */
export function render_query(options: TerminalRenderOptions, dpi_scale: number = 1): string {
    const opts = clamp_render_options(options);
    return [
        `fontSize=${device_font_size(opts.font_size, dpi_scale)}`,
        `fontFamily=${encodeURIComponent(opts.font_family)}`,
        `fontWeight=${opts.font_weight}`,
        `rendererType=${opts.renderer}`,
        // `theme` is object-typed in ttyd's defaults, so `parseOptsFromUrlQuery`
        // takes the `case "object"` branch and JSON.parses it. Encoding is not
        // optional: the JSON carries `{`, `"` and `#`, and a raw `#` would
        // truncate the URL at the fragment.
        `theme=${encodeURIComponent(JSON.stringify(TERMINAL_THEMES[opts.theme]))}`,
    ].join('&');
}

/**
 * The validated touched fields in an untrusted value. A field survives only if
 * it is present AND valid — an invalid one is dropped rather than clamped,
 * because a clamped garbage value would then read as a deliberate pick.
 */
export function parse_render_overrides(raw: unknown): RenderOverrides {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    const candidate = raw as Record<string, unknown>;
    // Clamp a full object built from the candidate, then keep only the fields
    // the candidate actually carried AND that came through unchanged-in-kind.
    const clamped = clamp_render_options(candidate);
    const out: RenderOverrides = {};
    for (const field of RENDER_FIELDS) {
        if (!(field in candidate)) continue;
        const value = candidate[field];
        if (value === undefined || value === null) continue;
        if (field === 'renderer' && !is_renderer(value)) continue;
        if (field === 'theme' && !is_theme_id(value)) continue;
        if (field === 'font_family' && (typeof value !== 'string' || value.trim() === '')) continue;
        if ((field === 'font_size' || field === 'font_weight') && to_number(value) === null) continue;
        assign_field(out, field, clamped);
    }
    return out;
}

function assign_field(target: RenderOverrides, field: RenderField, source: TerminalRenderOptions): void {
    // One switch so the per-field types stay exact without an index-signature cast.
    switch (field) {
        case 'font_size':
            target.font_size = source.font_size;
            break;
        case 'font_family':
            target.font_family = source.font_family;
            break;
        case 'font_weight':
            target.font_weight = source.font_weight;
            break;
        case 'renderer':
            target.renderer = source.renderer;
            break;
        case 'theme':
            target.theme = source.theme;
            break;
    }
}

/** The v2 fallback: a whole blob, minus the field whose default moved. */
export function migrate_v2_overrides(raw: unknown): RenderOverrides {
    const overrides = parse_render_overrides(raw);
    delete overrides.font_family;
    return overrides;
}

/** The effective options for a set of touched fields. */
export function resolve_render_options(overrides: RenderOverrides): TerminalRenderOptions {
    return clamp_render_options({ ...DEFAULT_RENDER_OPTIONS, ...overrides });
}

/**
 * The touched fields after a save: everything already touched, plus every field
 * whose value differs from the PREVIOUS EFFECTIVE value (see the note on
 * `RENDER_OPTIONS_STORAGE_KEY` for why not "differs from the default").
 * Pure — the storage wrappers below are the only I/O.
 */
export function merge_render_overrides(
    stored: RenderOverrides,
    previous: TerminalRenderOptions,
    next: TerminalRenderOptions,
): RenderOverrides {
    const clean = clamp_render_options(next);
    const out: RenderOverrides = { ...stored };
    for (const field of RENDER_FIELDS) {
        if (clean[field] !== previous[field]) assign_field(out, field, clean);
    }
    return out;
}

/**
 * A canonical string for a set of EFFECTIVE options — fixed field order, every
 * field present, clamped. The one way to compare two option sets across a
 * postMessage or `storage` hop: raw stored JSON is now sparse, so comparing it
 * to a full sent value never matches (`agent-rack/mount.ts`'s echo guard).
 */
export function serialize_render_options(options: TerminalRenderOptions): string {
    const clean = clamp_render_options(options);
    return JSON.stringify(RENDER_FIELDS.map((field) => [field, clean[field]]));
}

function default_storage(): RenderOptionsStorage | null {
    try {
        return typeof localStorage === 'undefined' ? null : localStorage;
    } catch {
        return null; // some private modes throw on the property access itself
    }
}

function read_json(storage: RenderOptionsStorage, key: string): unknown {
    const raw = storage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
}

/**
 * The touched fields as stored: `-v3`, else the read-only `-v2` fallback.
 *
 * READ-ONLY on purpose — loading runs in ~six places across three documents,
 * and a write here would fire a `storage` event into every other one. The v2
 * key is retired by the first `save_render_options`, never by a load.
 */
export function load_render_overrides(storage: RenderOptionsStorage | null = default_storage()): RenderOverrides {
    if (!storage) return {};
    try {
        const current = storage.getItem(RENDER_OPTIONS_STORAGE_KEY);
        if (current !== null) return parse_render_overrides(JSON.parse(current));
        return migrate_v2_overrides(read_json(storage, LEGACY_RENDER_OPTIONS_STORAGE_KEY));
    } catch {
        // Unparseable, or private mode with localStorage throwing on access.
        return {};
    }
}

/** Whether `field` is a user pick rather than a read-through default. */
export function has_render_override(
    field: RenderField,
    storage: RenderOptionsStorage | null = default_storage(),
): boolean {
    return field in load_render_overrides(storage);
}

/**
 * Read the shared setting.
 *
 * `localStorage` on purpose: the pages have no login and no account to hang a preference on,
 * and every page on one origin reads the same key, so a second window opens at the size the
 * first one picked. It is a per-device convenience: clearing site data forgets it.
 */
export function load_render_options(storage: RenderOptionsStorage | null = default_storage()): TerminalRenderOptions {
    return resolve_render_options(load_render_overrides(storage));
}

/** Persist `options` as touched fields. Every writer goes through here, so none of them can
 *  store a whole blob again. */
export function save_render_options(
    options: TerminalRenderOptions,
    storage: RenderOptionsStorage | null = default_storage(),
): void {
    if (!storage) return;
    try {
        const stored = load_render_overrides(storage);
        const next = merge_render_overrides(stored, resolve_render_options(stored), options);
        storage.setItem(RENDER_OPTIONS_STORAGE_KEY, JSON.stringify(next));
        storage.removeItem(LEGACY_RENDER_OPTIONS_STORAGE_KEY);
    } catch {
        // Private mode / quota. Losing the setting is not worth throwing over.
    }
}
