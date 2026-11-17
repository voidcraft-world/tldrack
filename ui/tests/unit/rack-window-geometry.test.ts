import test from 'node:test';
import assert from 'node:assert/strict';
import {
    MIN_H,
    MIN_W,
    clamp_rect,
    features_string,
    parse_rect,
    type AvailScreen,
    type RackWindowRect,
    TERM_MIN_PX,
    clamp_split_px,
    parse_split_px,
} from '../../src/agent-rack/rack-window-geometry.ts';

/** A plain 1920x1080 primary display starting at the origin. */
const PRIMARY: AvailScreen = { left: 0, top: 0, width: 1920, height: 1080 };

/** A 1920x1080 primary with a second display to its LEFT, which is what makes
 *  `availLeft` negative. */
const PRIMARY_WITH_LEFT_MONITOR: AvailScreen = { left: -1920, top: 0, width: 1920, height: 1080 };

function rect(overrides: Partial<RackWindowRect>): RackWindowRect {
    return { x: 100, y: 100, w: 1200, h: 760, ...overrides };
}

// ── parse_rect ──────────────────────────────────────────────────────────────

test('parse_rect returns null for absent or unparseable input', () => {
    assert.equal(parse_rect(null), null);
    assert.equal(parse_rect(''), null);
    assert.equal(parse_rect('not json'), null);
    assert.equal(parse_rect('null'), null);
    assert.equal(parse_rect('42'), null);
});

test('parse_rect rejects a rect missing any field', () => {
    assert.equal(parse_rect('{}'), null);
    assert.equal(parse_rect(JSON.stringify({ x: 1, y: 2, w: 3 })), null);
    assert.equal(parse_rect(JSON.stringify({ x: 1, y: 2, w: 3, h: null })), null);
    assert.equal(parse_rect(JSON.stringify({ x: 1, y: 2, w: 3, h: '760' })), null);
});

test('parse_rect rejects non-finite numbers', () => {
    // JSON has no NaN/Infinity literal, so this is how a corrupt value actually
    // arrives. It matters because a NaN in a features string is silently
    // dropped by Chrome, which then invents its own ~100x100 window.
    assert.equal(parse_rect('{"x":0,"y":0,"w":null,"h":760}'), null);
    const from_nan = JSON.stringify({ x: 0, y: 0, w: Number.NaN, h: 760 }); // -> {"x":0,"y":0,"w":null,...}
    assert.equal(parse_rect(from_nan), null);
    const from_infinity = JSON.stringify({ x: Number.POSITIVE_INFINITY, y: 0, w: 1200, h: 760 });
    assert.equal(parse_rect(from_infinity), null);
});

test('parse_rect accepts a well-formed rect, including negative coordinates', () => {
    assert.deepEqual(parse_rect('{"x":-1500,"y":40,"w":1200,"h":760}'), {
        x: -1500,
        y: 40,
        w: 1200,
        h: 760,
    });
});

// ── clamp_rect: size ────────────────────────────────────────────────────────

test('clamp_rect enforces the minimum usable size', () => {
    const result = clamp_rect(rect({ w: 50, h: 50 }), PRIMARY);
    assert.equal(result.w, MIN_W);
    assert.equal(result.h, MIN_H);
});

test('clamp_rect caps size at the display', () => {
    const result = clamp_rect(rect({ w: 99999, h: 99999 }), PRIMARY);
    assert.equal(result.w, PRIMARY.width);
    assert.equal(result.h, PRIMARY.height);
});

test('clamp_rect leaves a reasonable rect untouched', () => {
    const input = rect({ x: 240, y: 120, w: 1200, h: 760 });
    assert.deepEqual(clamp_rect(input, PRIMARY), input);
});

// ── clamp_rect: position ────────────────────────────────────────────────────

test('clamp_rect pulls a stranded rect back on-screen', () => {
    // The failure this exists to prevent: a window saved on a monitor that is
    // no longer attached, reopening somewhere the user cannot reach it.
    const result = clamp_rect(rect({ x: 500_000, y: 500_000 }), PRIMARY);
    assert.ok(result.x < PRIMARY.left + PRIMARY.width * 3, 'x pulled inside the envelope');
    assert.ok(result.y < PRIMARY.top + PRIMARY.height * 3, 'y pulled inside the envelope');
    // And the whole window still fits within the envelope's right/bottom edge.
    assert.ok(result.x + result.w <= PRIMARY.left + PRIMARY.width * 3);
    assert.ok(result.y + result.h <= PRIMARY.top + PRIMARY.height * 3);
});

test('clamp_rect PRESERVES a legal negative x when availLeft is negative', () => {
    // A monitor to the left of the primary reports a negative availLeft, and a
    // window parked there legitimately has a negative x. The naive
    // `Math.max(0, x)` would drag it onto the primary display on EVERY launch —
    // which is the entire reason this function takes an AvailScreen rather than
    // a width and a height.
    const input = rect({ x: -1500, y: 100 });
    const result = clamp_rect(input, PRIMARY_WITH_LEFT_MONITOR);
    assert.equal(result.x, -1500);
    assert.equal(result.y, 100);
});

test('clamp_rect preserves a rect on a monitor to the right', () => {
    // availWidth describes the PRIMARY display only, so a strict clamp to it
    // would also yank back a window the user parked on a second monitor.
    const input = rect({ x: 2400, y: 200 });
    const result = clamp_rect(input, PRIMARY);
    assert.equal(result.x, 2400);
    assert.equal(result.y, 200);
});

// ── features_string ─────────────────────────────────────────────────────────

test('features_string emits integers only and always requests a popup', () => {
    const features = features_string({ x: 10.7, y: 20.2, w: 1200.5, h: 760.4 });
    assert.ok(features.startsWith('popup,'), 'popup is always requested');
    assert.equal(features, 'popup,width=1201,height=760,left=11,top=20');
    assert.ok(!/\./.test(features), 'no fractional values survive');
    assert.ok(!/NaN/.test(features));
});

test('features_string round-trips a clamped rect without introducing fractions', () => {
    const features = features_string(clamp_rect(rect({ x: 33.3, y: 44.4 }), PRIMARY));
    assert.ok(!/\./.test(features));
});


// ── the draggable rack/terminal split ───────────────────────────────────────

test('clamp_split_px keeps both halves usable', () => {
    assert.equal(clamp_split_px(500, 1600, 220), 500);
    assert.equal(clamp_split_px(50, 1600, 220), 220, 'floors at the rack minimum');
    assert.equal(clamp_split_px(1590, 1600, 220), 1600 - TERM_MIN_PX, 'leaves a real terminal');
});

test('clamp_split_px resolves an impossible window to the rack floor', () => {
    // Both floors cannot fit. Not this function's problem to split: below
    // NARROW_PX the window stacks the panes instead of dividing them.
    assert.equal(clamp_split_px(300, 400, 220), 220);
    assert.equal(clamp_split_px(300, 540, 220), 220);
});

test('clamp_split_px never leaks a NaN into a layout', () => {
    assert.equal(clamp_split_px(Number.NaN, 1600, 220), 220);
    assert.equal(clamp_split_px(500, Number.NaN, 220), 220);
    // Every non-finite input resolves the same way. Infinity could arguably
    // clamp to the ceiling, but it only ever arrives through a bug, and one
    // defined answer for "not a number" beats two.
    assert.equal(clamp_split_px(Number.POSITIVE_INFINITY, 1600, 220), 220);
});

test('parse_split_px treats "never dragged" as null, not zero', () => {
    // Null is meaningful: it means "follow the window", the right default for a
    // split the user has not touched.
    assert.equal(parse_split_px(null), null);
    assert.equal(parse_split_px(''), null);
    assert.equal(parse_split_px('  '), null);
    assert.equal(parse_split_px('nope'), null);
    assert.equal(parse_split_px('0'), null);
    assert.equal(parse_split_px('-40'), null);
    assert.equal(parse_split_px('420'), 420);
    assert.equal(parse_split_px('420.5'), 420.5);
});
