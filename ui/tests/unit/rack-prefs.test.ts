import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    DEFAULT_RACK_PREFS,
    LITE_RACK_PREFS,
    RACK_PREFS_KEY,
    load_rack_prefs,
    parse_rack_prefs,
    same_rack_prefs,
    save_rack_prefs,
    watch_rack_prefs,
    type RackPrefs,
    type RackPrefsStorage,
    type StorageEventTarget,
} from '../../src/config/rack-prefs.ts';

function memory(): RackPrefsStorage & { data: Map<string, string> } {
    const data = new Map<string, string>();
    return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
}

const THROWS: RackPrefsStorage = {
    getItem() {
        throw new Error('SecurityError');
    },
    setItem() {
        throw new Error('QuotaExceededError');
    },
};

describe('rack prefs', () => {
    it('defaults to the rack as it was: office on and sharp, motion on', () => {
        assert.deepEqual(DEFAULT_RACK_PREFS, { office: true, office_quality: 'sharp', motion: true });
        assert.deepEqual(parse_rack_prefs(null), DEFAULT_RACK_PREFS);
    });

    it('reads each field on its own, so one bad field keeps the others', () => {
        assert.deepEqual(parse_rack_prefs('{"office":false,"office_quality":"light","motion":false}'), LITE_RACK_PREFS);
        assert.deepEqual(parse_rack_prefs('{"office":"no","office_quality":"light"}'), {
            office: true,
            office_quality: 'light',
            motion: true,
        });
        assert.deepEqual(parse_rack_prefs('{"office_quality":"ultra","motion":false}'), {
            office: true,
            office_quality: 'sharp',
            motion: false,
        });
        assert.deepEqual(parse_rack_prefs('not json'), DEFAULT_RACK_PREFS);
        assert.deepEqual(parse_rack_prefs('[false]'), DEFAULT_RACK_PREFS);
    });

    it('round-trips through storage and stores only its own fields', () => {
        const storage = memory();
        const extra = { ...LITE_RACK_PREFS, stray: 1 } as RackPrefs;
        assert.equal(save_rack_prefs(extra, storage), true);
        assert.equal(storage.data.get(RACK_PREFS_KEY), JSON.stringify(LITE_RACK_PREFS));
        assert.deepEqual(load_rack_prefs(storage), LITE_RACK_PREFS);
    });

    it('survives storage that is missing or throws', () => {
        assert.deepEqual(load_rack_prefs(null), DEFAULT_RACK_PREFS);
        assert.deepEqual(load_rack_prefs(THROWS), DEFAULT_RACK_PREFS);
        assert.equal(save_rack_prefs(LITE_RACK_PREFS, null), false);
        assert.equal(save_rack_prefs(LITE_RACK_PREFS, THROWS), false);
    });

    it('compares by value', () => {
        assert.equal(same_rack_prefs(DEFAULT_RACK_PREFS, { ...DEFAULT_RACK_PREFS }), true);
        assert.equal(same_rack_prefs(DEFAULT_RACK_PREFS, LITE_RACK_PREFS), false);
    });

    it("follows another tab's write, a whole-storage clear, and nothing else", () => {
        type Listener = (event: { key: string | null; newValue: string | null }) => void;
        const listeners = new Set<Listener>();
        const target: StorageEventTarget = {
            addEventListener: (_type, listener) => void listeners.add(listener),
            removeEventListener: (_type, listener) => void listeners.delete(listener),
        };
        const seen: RackPrefs[] = [];
        const stop = watch_rack_prefs((prefs) => seen.push(prefs), target);
        const fire = (key: string | null, value: string | null): void => listeners.forEach((listener) => listener({ key, newValue: value }));

        fire('tldrack:some-other-key', '{}');
        assert.equal(seen.length, 0);
        fire(RACK_PREFS_KEY, JSON.stringify(LITE_RACK_PREFS));
        assert.deepEqual(seen.at(-1), LITE_RACK_PREFS);
        fire(null, null);
        assert.deepEqual(seen.at(-1), DEFAULT_RACK_PREFS);

        stop();
        assert.equal(listeners.size, 0);
        assert.deepEqual(watch_rack_prefs(() => {}, null)(), undefined);
    });
});
