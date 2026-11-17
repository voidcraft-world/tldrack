import test from 'node:test';
import assert from 'node:assert/strict';
import {
    DEFAULT_RENDER_OPTIONS,
    DEFAULT_TERMINAL_FONT_SIZE,
    MAX_BRIDGE_FONT_PX,
    MAX_TERMINAL_FONT_SIZE,
    MIN_TERMINAL_FONT_SIZE,
    TERMINAL_FONT_FAMILIES,
    DEFAULT_TERMINAL_THEME,
    TERMINAL_THEME,
    TERMINAL_THEMES,
    TERMINAL_THEME_IDS,
    LEGACY_RENDER_OPTIONS_STORAGE_KEY,
    RENDER_OPTIONS_STORAGE_KEY,
    clamp_font_size,
    clamp_render_options,
    device_font_size,
    has_render_override,
    load_render_options,
    merge_render_overrides,
    render_query,
    save_render_options,
    serialize_render_options,
    type RenderOptionsStorage,
    type TerminalRenderOptions,
} from '../../src/engine/terminal/terminal-render-options.ts';

function options(overrides: Partial<TerminalRenderOptions> = {}): TerminalRenderOptions {
    return { ...DEFAULT_RENDER_OPTIONS, ...overrides };
}

// ── clamp_font_size ─────────────────────────────────────────────────────────

test('clamp_font_size holds the slider range', () => {
    assert.equal(clamp_font_size(MIN_TERMINAL_FONT_SIZE - 5), MIN_TERMINAL_FONT_SIZE);
    assert.equal(clamp_font_size(MAX_TERMINAL_FONT_SIZE + 5), MAX_TERMINAL_FONT_SIZE);
    assert.equal(clamp_font_size(20), 20);
});

test('clamp_font_size rounds to whole pixels', () => {
    // A fractional fontSize gives xterm a fractional cell, which is one of the
    // ways glyphs land off the device-pixel grid and blur.
    assert.equal(clamp_font_size(17.4), 17);
    assert.equal(clamp_font_size(17.6), 18);
});

test('clamp_font_size never propagates a NaN', () => {
    // `fontSize=NaN` survives ttyd's parseInt as NaN and blanks the pane, so
    // garbage must become the default rather than flow through.
    for (const bad of [NaN, Infinity, 'huge', null, undefined, {}]) {
        assert.equal(clamp_font_size(bad), DEFAULT_TERMINAL_FONT_SIZE);
    }
});

// ── clamp_render_options ────────────────────────────────────────────────────

test('clamp_render_options returns the defaults for non-objects', () => {
    for (const bad of [null, undefined, 'nope', 42]) {
        assert.deepEqual(clamp_render_options(bad), DEFAULT_RENDER_OPTIONS);
    }
});

test('clamp_render_options falls back per FIELD, not all-or-nothing', () => {
    // One corrupt key must not discard the others.
    const result = clamp_render_options({ font_size: 24, renderer: 'raytraced', font_family: '  ' });
    assert.equal(result.font_size, 24);
    assert.equal(result.renderer, DEFAULT_RENDER_OPTIONS.renderer);
    assert.equal(result.font_family, DEFAULT_RENDER_OPTIONS.font_family);
});

test('clamp_render_options accepts every renderer ttyd understands', () => {
    for (const renderer of ['webgl', 'canvas', 'dom'] as const) {
        assert.equal(clamp_render_options(options({ renderer })).renderer, renderer);
    }
});

test('clamp_render_options snaps font weight into the CSS 100..900 range', () => {
    // Out of range makes the canvas font shorthand fail to parse ENTIRELY, and
    // xterm's glyph atlas silently falls back to 10px sans-serif.
    assert.equal(clamp_render_options(options({ font_weight: 5 })).font_weight, 100);
    assert.equal(clamp_render_options(options({ font_weight: 9000 })).font_weight, 900);
    assert.equal(clamp_render_options(options({ font_weight: 437 })).font_weight, 400);
    assert.equal(clamp_render_options(options({ font_weight: 500 })).font_weight, 500);
});

test('clamp_render_options trims a padded font family but keeps the stack', () => {
    const stack = '  ui-monospace, Menlo, monospace  ';
    assert.equal(clamp_render_options(options({ font_family: stack })).font_family, 'ui-monospace, Menlo, monospace');
});

// ── device_font_size ────────────────────────────────────────────────────────

test('device_font_size multiplies by the surface supersample factor', () => {
    // A surface that renders into a 2x buffer and is scaled back by half needs
    // this multiply, or its glyphs land at half the chosen size.
    assert.equal(device_font_size(18, 1), 18);
    assert.equal(device_font_size(18, 2), 36);
    assert.equal(device_font_size(18, 4), 72);
});

test('device_font_size respects the bridge live-push ceiling', () => {
    // index_patch.py ignores a push outside 8..120 DEVICE px, so a supersampled
    // surface must not be able to multiply its way past it and silently lose
    // every later font change.
    assert.equal(device_font_size(MAX_TERMINAL_FONT_SIZE, 4), MAX_BRIDGE_FONT_PX);
    assert.ok(device_font_size(MAX_TERMINAL_FONT_SIZE, 8) <= MAX_BRIDGE_FONT_PX);
});

test('device_font_size treats a nonsense scale as 1x', () => {
    for (const bad of [0, -2, NaN]) {
        assert.equal(device_font_size(18, bad), 18);
    }
});

// ── render_query ────────────────────────────────────────────────────────────

test('render_query emits every ttyd client option, unprefixed', () => {
    const query = render_query(options({ font_size: 20, font_weight: 500, renderer: 'dom' }), 1);
    assert.ok(!query.startsWith('&') && !query.startsWith('?'), 'the caller owns the join');
    assert.match(query, /(^|&)fontSize=20(&|$)/);
    assert.match(query, /(^|&)fontWeight=500(&|$)/);
    assert.match(query, /(^|&)rendererType=dom(&|$)/);
    assert.match(query, /(^|&)fontFamily=/);
    assert.match(query, /(^|&)theme=/);
});

test('render_query round-trips the theme through the URL encoding', () => {
    // ttyd parses this one with JSON.parse (the `case "object"` branch of
    // parseOptsFromUrlQuery), so a theme that does not survive
    // encode -> decode -> parse reaches xterm as a thrown SyntaxError and the
    // pane comes up with no colours at all.
    assert.deepEqual(theme_in(render_query(options())), TERMINAL_THEME);
});

test('render_query percent-encodes the theme, so no raw # reaches the URL', () => {
    // Every colour is a `#rrggbb`. A raw `#` in a query string starts the
    // fragment, so an unencoded theme would truncate the URL at the first
    // colour and drop every option after it.
    const query = render_query(options());
    assert.ok(!query.includes('#'), 'a raw # would truncate the URL at the fragment');
    assert.ok(query.includes('%23'));
});

// ── themes ──────────────────────────────────────────────────────────────────

const ANSI_SLOTS = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'];
/** The 22 keys of a complete theme: six base keys and the 16 ANSI slots. */
const THEME_KEYS = [
    'background', 'foreground', 'cursor', 'cursorAccent', 'selectionBackground', 'selectionForeground',
    ...ANSI_SLOTS,
    ...ANSI_SLOTS.map((slot) => `bright${slot[0].toUpperCase()}${slot.slice(1)}`),
];

/** The theme a query string carries, decoded the way ttyd decodes it. */
function theme_in(query: string): unknown {
    const pair = query.split('&').find((part) => part.startsWith('theme='));
    assert.ok(pair, 'render_query must emit a theme pair');
    return JSON.parse(decodeURIComponent(pair.slice('theme='.length)));
}

test('every theme carries all 22 keys, each a #rrggbb', () => {
    // ttyd's applyPreferences MERGES object options onto the bridge's
    // `-t theme=`, so an omitted key is NOT reset to xterm's default — it keeps
    // whatever the bridge set. A partial theme silently inherits half of
    // another palette.
    assert.equal(THEME_KEYS.length, 22);
    assert.ok(TERMINAL_THEME_IDS.length >= 2);
    for (const id of TERMINAL_THEME_IDS) {
        const theme: Record<string, string> = TERMINAL_THEMES[id];
        assert.deepEqual(Object.keys(theme).sort(), [...THEME_KEYS].sort(), id);
        for (const key of THEME_KEYS) assert.match(theme[key], /^#[0-9a-f]{6}$/, `${id}.${key}`);
    }
});

test('the default theme is the first one offered, and TERMINAL_THEME is its colours', () => {
    assert.equal(TERMINAL_THEME_IDS[0], DEFAULT_TERMINAL_THEME);
    assert.equal(DEFAULT_RENDER_OPTIONS.theme, DEFAULT_TERMINAL_THEME);
    assert.equal(TERMINAL_THEME, TERMINAL_THEMES[DEFAULT_TERMINAL_THEME]);
});

test('an unknown theme id falls back to the default, and a known one is kept', () => {
    for (const bad of ['neon', '', 'Graphite', 7, null, undefined, { background: '#000000' }]) {
        const result = clamp_render_options({ ...options({ font_size: 24 }), theme: bad });
        assert.equal(result.theme, DEFAULT_TERMINAL_THEME, String(bad));
        assert.equal(result.font_size, 24, 'a bad theme does not discard the other fields');
    }
    for (const id of TERMINAL_THEME_IDS) {
        assert.equal(clamp_render_options(options({ theme: id })).theme, id);
    }
});

test('constructor and __proto__ are not themes', () => {
    // Both are properties of every object, so an `in` test would accept them and
    // `TERMINAL_THEMES[theme]` would hand ttyd a function or a prototype.
    for (const name of ['constructor', '__proto__', 'toString', 'hasOwnProperty']) {
        const hostile = JSON.parse(`{"theme":"${name}","font_size":24}`);
        assert.equal(clamp_render_options(hostile).theme, DEFAULT_TERMINAL_THEME, name);
        const storage = memory_storage({ [RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify(hostile) });
        assert.equal(has_render_override('theme', storage), false, `${name} in storage is not a pick`);
        assert.equal(load_render_options(storage).theme, DEFAULT_TERMINAL_THEME, name);
        assert.deepEqual(theme_in(render_query({ ...options(), theme: name as never })), TERMINAL_THEME, name);
    }
});

test('render_query carries the chosen theme\'s colours', () => {
    for (const id of TERMINAL_THEME_IDS) {
        assert.deepEqual(theme_in(render_query(options({ theme: id }))), TERMINAL_THEMES[id], id);
    }
    assert.notDeepEqual(theme_in(render_query(options({ theme: 'mint' }))), theme_in(render_query(options())));
});

test('a stored -v3 blob without a theme reads through to the default', () => {
    // `theme` arrived after `-v3` existed: a blob written before it holds no
    // such key, and that must read as "never picked", not as a corrupt blob.
    const storage = memory_storage({ [RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({ font_size: 22, renderer: 'dom' }) });
    const loaded = load_render_options(storage);
    assert.equal(loaded.theme, DEFAULT_TERMINAL_THEME);
    assert.equal(loaded.font_size, 22);
    assert.equal(loaded.renderer, 'dom');
    assert.equal(has_render_override('theme', storage), false);
    assert.equal(storage.writes, 0, 'a load must never write');
});

test('picking a theme stores it as one more touched field', () => {
    const storage = memory_storage({ [RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({ font_size: 22 }) });
    save_render_options({ ...load_render_options(storage), theme: 'mint' }, storage);
    assert.deepEqual(JSON.parse(storage.data.get(RENDER_OPTIONS_STORAGE_KEY)!), { font_size: 22, theme: 'mint' });
    assert.equal(load_render_options(storage).theme, 'mint');
    assert.equal(has_render_override('theme', storage), true);
});

test('every offered font family survives clamping unchanged', () => {
    // The dropdown binds to the STACK. If clamp_render_options rewrote one
    // (trimming, or falling back), the <select> would render blank because no
    // <option> value matched the stored value.
    for (const font of TERMINAL_FONT_FAMILIES) {
        assert.equal(clamp_render_options(options({ font_family: font.stack })).font_family, font.stack);
    }
});

test('the default font family is one the dropdown offers', () => {
    assert.ok(TERMINAL_FONT_FAMILIES.some((font) => font.stack === DEFAULT_RENDER_OPTIONS.font_family));
});

test('render_query escapes the font stack', () => {
    // A raw stack carries commas and spaces; unescaped they would split the
    // query and hand xterm a truncated family.
    const query = render_query(options({ font_family: 'ui-monospace, Menlo, monospace' }));
    assert.ok(query.includes('fontFamily=ui-monospace%2C%20Menlo%2C%20monospace'));
    assert.ok(!query.includes('fontFamily=ui-monospace, Menlo'));
});

test('render_query carries the DEVICE size, not the logical one', () => {
    assert.match(render_query(options({ font_size: 18 }), 2), /(^|&)fontSize=36(&|$)/);
});

// ── SF Mono default + touched-field storage ─────────────────────────────────

/** An in-memory Storage that records writes, so a test can assert a LOAD
 *  never writes (a write would fire `storage` events into other documents). */
function memory_storage(seed: Record<string, string> = {}): RenderOptionsStorage & {
    data: Map<string, string>;
    writes: number;
} {
    const data = new Map(Object.entries(seed));
    const store = {
        data,
        writes: 0,
        getItem: (key: string) => data.get(key) ?? null,
        setItem: (key: string, value: string) => {
            store.writes += 1;
            data.set(key, value);
        },
        removeItem: (key: string) => {
            store.writes += 1;
            data.delete(key);
        },
    };
    return store;
}

const SF_MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';
const MONACO = 'Monaco, Menlo, monospace';

test('SF Mono is the default, reached through ui-monospace', () => {
    assert.equal(DEFAULT_RENDER_OPTIONS.font_family, SF_MONO);
    assert.equal(TERMINAL_FONT_FAMILIES[0].label, 'SF Mono');
});

test('a save stores ONLY the touched field', () => {
    const storage = memory_storage();
    save_render_options(options({ font_size: 22 }), storage);
    assert.deepEqual(JSON.parse(storage.data.get(RENDER_OPTIONS_STORAGE_KEY)!), { font_size: 22 });
});

test('an untouched field reads through to TODAY\'S default — the treadmill this ends', () => {
    // The whole point: a user who only ever moved the slider must still pick up
    // a future default family, which a whole-blob store made impossible.
    const storage = memory_storage({ [RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({ font_size: 22 }) });
    const loaded = load_render_options(storage);
    assert.equal(loaded.font_size, 22);
    assert.equal(loaded.font_family, DEFAULT_RENDER_OPTIONS.font_family);
});

test('touched means "differs from the previous EFFECTIVE value", not "differs from the default"', () => {
    // ⌘0 back to the default size after a bump must STICK as a pick — the
    // phone font bump keys off whether font_size is an override.
    const storage = memory_storage();
    save_render_options(options({ font_size: 22 }), storage);
    save_render_options(options({ font_size: DEFAULT_RENDER_OPTIONS.font_size }), storage);
    assert.equal(has_render_override('font_size', storage), true);
    assert.equal(load_render_options(storage).font_size, DEFAULT_RENDER_OPTIONS.font_size);
    // An explicit pick equal to the current effective value changes nothing.
    assert.deepEqual(
        merge_render_overrides({}, DEFAULT_RENDER_OPTIONS, DEFAULT_RENDER_OPTIONS),
        {},
    );
});

test('a touched field is never dropped by a later save of another field', () => {
    const storage = memory_storage();
    save_render_options(options({ renderer: 'dom' }), storage);
    save_render_options(options({ renderer: 'dom', font_weight: 500 }), storage);
    assert.deepEqual(JSON.parse(storage.data.get(RENDER_OPTIONS_STORAGE_KEY)!), { renderer: 'dom', font_weight: 500 });
});

test('a v2 blob is read as overrides MINUS its font_family, and loading never writes', () => {
    const storage = memory_storage({
        [LEGACY_RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({
            font_size: 24, font_family: MONACO, font_weight: 400, renderer: 'canvas',
        }),
    });
    const loaded = load_render_options(storage);
    assert.equal(loaded.font_size, 24);
    assert.equal(loaded.renderer, 'canvas');
    assert.equal(loaded.font_family, SF_MONO, 'the v2 family cannot say it was ever picked');
    assert.equal(has_render_override('font_size', storage), true);
    assert.equal(has_render_override('font_family', storage), false);
    assert.equal(storage.writes, 0, 'a load must never write');
});

test('the first v3 save carries the v2 overrides forward and retires the v2 key', () => {
    const storage = memory_storage({
        [LEGACY_RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({ font_size: 24, font_family: MONACO, renderer: 'canvas' }),
    });
    save_render_options({ ...load_render_options(storage), font_weight: 500 }, storage);
    assert.equal(storage.data.has(LEGACY_RENDER_OPTIONS_STORAGE_KEY), false);
    const stored = JSON.parse(storage.data.get(RENDER_OPTIONS_STORAGE_KEY)!);
    assert.equal(stored.font_size, 24);
    assert.equal(stored.renderer, 'canvas');
    assert.equal(stored.font_weight, 500);
    assert.equal('font_family' in stored, false);
});

test('garbage in storage degrades field by field, and a corrupt blob to the defaults', () => {
    const partial = memory_storage({
        [RENDER_OPTIONS_STORAGE_KEY]: JSON.stringify({ font_size: 'huge', renderer: 'webgpu', font_weight: 700 }),
    });
    const loaded = load_render_options(partial);
    assert.equal(loaded.font_size, DEFAULT_RENDER_OPTIONS.font_size);
    assert.equal(loaded.renderer, DEFAULT_RENDER_OPTIONS.renderer);
    assert.equal(loaded.font_weight, 700);
    assert.equal(has_render_override('font_size', partial), false, 'an invalid value is not a pick');
    assert.deepEqual(load_render_options(memory_storage({ [RENDER_OPTIONS_STORAGE_KEY]: '{not json' })), DEFAULT_RENDER_OPTIONS);
    assert.deepEqual(load_render_options(null), DEFAULT_RENDER_OPTIONS);
});

test('serialize_render_options is canonical: same effective options, same string', () => {
    // The pop-out echo guard compares these — raw stored JSON is sparse and
    // could never equal the full set that was sent.
    const a = serialize_render_options(options({ font_size: 20 }));
    const b = serialize_render_options({
        theme: DEFAULT_TERMINAL_THEME, renderer: 'webgl', font_weight: 400, font_family: SF_MONO, font_size: 20,
    });
    assert.equal(a, b);
    assert.notEqual(a, serialize_render_options(options({ font_size: 22 })));
    assert.notEqual(a, serialize_render_options(options({ font_size: 20, theme: 'mint' })), 'the theme is part of the string');
    assert.deepEqual(
        (JSON.parse(a) as [string, unknown][]).map(([field]) => field),
        ['font_size', 'font_family', 'font_weight', 'renderer', 'theme'],
    );
    const storage = memory_storage();
    save_render_options(options({ font_size: 20 }), storage);
    assert.equal(serialize_render_options(load_render_options(storage)), a);
});
