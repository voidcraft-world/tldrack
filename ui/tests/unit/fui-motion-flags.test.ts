import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

/**
 * `should_animate()` is the single gate for the whole FUI motion vocabulary: the
 * stylesheet reads it indirectly through the `.fui-motion-off` class, and script
 * that animates calls it directly. If its truth table drifts, one layer goes silent
 * while the other keeps moving. So the table gets pinned here.
 *
 * The module reads `window.matchMedia` at import time (that is what installs the
 * live `change` listener, the thing an ad-hoc `matchMedia` call gets wrong by
 * snapshotting once and never updating). To exercise
 * the OS branch at all we therefore have to stand up a stub BEFORE the first
 * import, which is why this file uses a dynamic import inside `before`.
 */

type Listener = (e: { matches: boolean }) => void;

const media = {
    matches: false,
    listeners: new Set<Listener>(),
    /** Simulate the user flipping the OS setting mid-session. */
    emit(matches: boolean) {
        this.matches = matches;
        for (const fn of this.listeners) fn({ matches });
    },
};

// Minimal `document` so apply_fui_motion_flags() can run its real code path
// rather than early-returning — otherwise the class toggle is never exercised.
const classes = new Set<string>();
const style_props = new Map<string, string>();

let flags: typeof import('../../src/config/fui-motion-flags.ts');

before(async () => {
    (globalThis as Record<string, unknown>).window = {
        matchMedia: () => ({
            matches: media.matches,
            addEventListener: (_: string, fn: Listener) => media.listeners.add(fn),
        }),
    };
    (globalThis as Record<string, unknown>).document = {
        documentElement: {
            style: { setProperty: (k: string, v: string) => style_props.set(k, v) },
            classList: {
                toggle: (name: string, on: boolean) => (on ? classes.add(name) : classes.delete(name)),
            },
        },
    };
    flags = await import('../../src/config/fui-motion-flags.ts');
});

const reset = () => {
    media.emit(false);
    flags.FUI_MOTION_FLAGS.enabled = true;
    flags.FUI_MOTION_FLAGS.force_reduced_motion = false;
    flags.FUI_MOTION_FLAGS.respect_reduced_motion = true;
};

describe('should_animate', () => {
    it('animates only when all three opt-outs are clear', () => {
        reset();
        assert.equal(flags.should_animate(), true);
    });

    it('is false when the user preference is off', () => {
        reset();
        flags.FUI_MOTION_FLAGS.enabled = false;
        assert.equal(flags.should_animate(), false);
    });

    it('is false when the dev override forces reduced motion', () => {
        reset();
        flags.FUI_MOTION_FLAGS.force_reduced_motion = true;
        assert.equal(flags.should_animate(), false);
    });

    it('is false when the OS asks for reduced motion', () => {
        reset();
        media.emit(true);
        assert.equal(flags.should_animate(), false);
        assert.equal(flags.os_prefers_reduced_motion(), true);
    });

    it('tracks the OS setting LIVE, both directions', () => {
        // The whole reason this module exists rather than an inline matchMedia
        // call at each site: a query snapshotted once at module load does nothing
        // at all when the OS setting is toggled mid-session.
        reset();
        media.emit(true);
        assert.equal(flags.should_animate(), false);
        media.emit(false);
        assert.equal(flags.should_animate(), true);
    });

    it('ignores the OS query when respect_reduced_motion is off', () => {
        reset();
        media.emit(true);
        flags.FUI_MOTION_FLAGS.respect_reduced_motion = false;
        assert.equal(flags.should_animate(), true);
        // The raw query still reports the truth — callers that need to tell the
        // two apart can.
        assert.equal(flags.os_prefers_reduced_motion(), true);
    });

    it('keeps the preference authoritative over the OS query', () => {
        // enabled=false must win even when the OS is happy to animate.
        reset();
        flags.FUI_MOTION_FLAGS.enabled = false;
        assert.equal(flags.os_prefers_reduced_motion(), false);
        assert.equal(flags.should_animate(), false);
    });
});

describe('apply_fui_motion_flags', () => {
    it('adds .fui-motion-off exactly when should_animate() is false', () => {
        reset();
        assert.equal(classes.has('fui-motion-off'), false);
        flags.FUI_MOTION_FLAGS.enabled = false;
        assert.equal(classes.has('fui-motion-off'), true);
        flags.FUI_MOTION_FLAGS.enabled = true;
        assert.equal(classes.has('fui-motion-off'), false);
    });

    it('republishes on an OS change with no explicit call', () => {
        reset();
        media.emit(true);
        assert.equal(classes.has('fui-motion-off'), true);
        media.emit(false);
        assert.equal(classes.has('fui-motion-off'), false);
    });

    it('publishes every duration WITH a ms unit, including at zero', () => {
        // `--fui-corner-draw-stagger-ms` is multiplied inside a calc() in the
        // stylesheet, and `calc(0 * 3)` is a <number>, not a <time> — which
        // invalidates the whole animation-delay declaration. A bare `0` here
        // therefore un-staggers the brackets instead of un-delaying them, and it
        // fails silently.
        reset();
        flags.FUI_MOTION_FLAGS.corner_stagger_ms = 0;
        flags.FUI_MOTION_FLAGS.corner_draw_ms = 0;
        for (const [key, value] of style_props) {
            if (!key.endsWith('-ms')) continue;
            assert.match(value, /^\d+ms$/, `${key} must carry a ms unit, got "${value}"`);
        }
        flags.FUI_MOTION_FLAGS.corner_stagger_ms = 30;
        flags.FUI_MOTION_FLAGS.corner_draw_ms = 150;
    });

    it('writes through the Proxy so a console edit lands with no reload', () => {
        reset();
        flags.FUI_MOTION_FLAGS.corner_draw_ms = 600;
        assert.equal(style_props.get('--fui-corner-draw-ms'), '600ms');
        flags.FUI_MOTION_FLAGS.corner_draw_ms = 150;
        assert.equal(style_props.get('--fui-corner-draw-ms'), '150ms');
    });
});
