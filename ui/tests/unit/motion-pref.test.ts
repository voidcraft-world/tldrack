import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { RACK_PREFS_KEY, type RackPrefs } from '../../src/config/rack-prefs.ts';

/**
 * The MOTION switch has to reach the page as the ONE class the stylesheet reads, on boot and on a
 * change made in another tab. Both modules act at import (that is what makes them impossible to
 * forget), so the page is stood up first and they are imported inside `before`, the way
 * `fui-motion-flags.test.ts` does it.
 */

type StorageListener = (event: { key: string | null; newValue: string | null }) => void;

const classes = new Set<string>();
const listeners = new Set<StorageListener>();
const stored = new Map<string, string>();
const prefs = (motion: boolean): string => JSON.stringify({ office: true, office_quality: 'sharp', motion });

let flags: typeof import('../../src/config/fui-motion-flags.ts');
let motion: typeof import('../../src/styles/motion-pref.ts');

before(async () => {
    // The operator turned motion off on the settings page before this page was opened.
    stored.set(RACK_PREFS_KEY, prefs(false));
    (globalThis as Record<string, unknown>).localStorage = {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => void stored.set(key, value),
    };
    (globalThis as Record<string, unknown>).window = {
        matchMedia: () => ({ matches: false, addEventListener: () => {} }),
        addEventListener: (_: string, fn: StorageListener) => listeners.add(fn),
        removeEventListener: (_: string, fn: StorageListener) => listeners.delete(fn),
    };
    (globalThis as Record<string, unknown>).document = {
        documentElement: {
            style: { setProperty: () => {} },
            classList: { toggle: (name: string, on: boolean) => (on ? classes.add(name) : classes.delete(name)) },
        },
    };
    flags = await import('../../src/config/fui-motion-flags.ts');
    motion = await import('../../src/styles/motion-pref.ts');
});

function another_tab_writes(key: string | null, value: string | null): void {
    for (const listener of listeners) listener({ key, newValue: value });
}

describe('the motion switch on a page', () => {
    it('is applied by the import alone, before anything could mount', () => {
        assert.equal(flags.FUI_MOTION_FLAGS.enabled, false);
        assert.equal(flags.should_animate(), false);
        assert.equal(classes.has('fui-motion-off'), true);
    });

    it('follows a change made in another tab, both ways, with no reload', () => {
        another_tab_writes(RACK_PREFS_KEY, prefs(true));
        assert.equal(classes.has('fui-motion-off'), false);
        another_tab_writes(RACK_PREFS_KEY, prefs(false));
        assert.equal(classes.has('fui-motion-off'), true);
    });

    it('ignores other keys, and reads cleared storage as the default: motion on', () => {
        another_tab_writes('tldrack:something-else', prefs(true));
        assert.equal(classes.has('fui-motion-off'), true);
        another_tab_writes(null, null);
        assert.equal(classes.has('fui-motion-off'), false);
    });

    it('writes the master switch and nothing else, so the WORK scanner keeps its timing', () => {
        const work_ms = flags.FUI_MOTION_FLAGS.work_ms;
        another_tab_writes(RACK_PREFS_KEY, prefs(false));
        assert.equal(flags.FUI_MOTION_FLAGS.work_ms, work_ms);
        assert.equal(flags.FUI_MOTION_FLAGS.force_reduced_motion, false);
        assert.equal(flags.FUI_MOTION_FLAGS.respect_reduced_motion, true);
    });
});

describe('follow_motion_pref', () => {
    it('sets the gate from what is stored, then from each change, until stopped', () => {
        const gate = { enabled: true };
        let on_change: (next: RackPrefs) => void = () => {};
        let stopped = false;
        const stop = motion.follow_motion_pref(
            gate,
            () => ({ office: true, office_quality: 'sharp', motion: false }),
            (fn) => { on_change = fn; return () => { stopped = true; }; },
        );
        assert.equal(gate.enabled, false);
        on_change({ office: false, office_quality: 'light', motion: true });
        assert.equal(gate.enabled, true);
        stop();
        assert.equal(stopped, true);
    });
});
