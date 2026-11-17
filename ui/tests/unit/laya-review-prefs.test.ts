import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    REVIEW_PREFS_DEFAULT,
    REVIEW_PREFS_KEY,
    load_review_prefs,
    parse_review_prefs,
    save_review_prefs,
    type PrefsStorage,
} from '../../src/laya-review/review-prefs.ts';

function memory(): PrefsStorage & { data: Map<string, string> } {
    const data = new Map<string, string>();
    return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
}

const THROWS: PrefsStorage = {
    getItem() {
        throw new Error('SecurityError');
    },
    setItem() {
        throw new Error('QuotaExceededError');
    },
};

describe('review prefs', () => {
    it('defaults to dark with EXPLAIN on', () => {
        assert.deepEqual(REVIEW_PREFS_DEFAULT, { theme: 'dark', explain: true });
        assert.deepEqual(parse_review_prefs(null), REVIEW_PREFS_DEFAULT);
    });

    it('reads each field on its own, so one bad field keeps the other', () => {
        assert.deepEqual(parse_review_prefs('{"theme":"light","explain":false}'), { theme: 'light', explain: false });
        assert.deepEqual(parse_review_prefs('{"theme":"sepia","explain":false}'), { theme: 'dark', explain: false });
        assert.deepEqual(parse_review_prefs('{"theme":"light","explain":"yes"}'), { theme: 'light', explain: true });
        assert.deepEqual(parse_review_prefs('not json'), REVIEW_PREFS_DEFAULT);
        assert.deepEqual(parse_review_prefs('[1,2]'), REVIEW_PREFS_DEFAULT);
    });

    it('round-trips through storage', () => {
        const store = memory();
        save_review_prefs({ theme: 'light', explain: false }, store);
        assert.equal(store.data.get(REVIEW_PREFS_KEY), '{"theme":"light","explain":false}');
        assert.deepEqual(load_review_prefs(store), { theme: 'light', explain: false });
    });

    it('draws the defaults when storage is missing or throws, and never throws itself', () => {
        assert.deepEqual(load_review_prefs(null), REVIEW_PREFS_DEFAULT);
        assert.deepEqual(load_review_prefs(THROWS), REVIEW_PREFS_DEFAULT);
        assert.doesNotThrow(() => save_review_prefs({ theme: 'light', explain: true }, THROWS));
        assert.doesNotThrow(() => save_review_prefs({ theme: 'light', explain: true }, null));
    });
});
