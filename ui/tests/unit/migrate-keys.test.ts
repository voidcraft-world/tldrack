import test from 'node:test';
import assert from 'node:assert/strict';

import { KEY_MOVES, MIGRATED_KEY, migrate_keys, type KeyStore } from '../../src/storage/migrate-keys.ts';

function store(initial: Record<string, string> = {}): KeyStore & { data: Map<string, string> } {
    const data = new Map(Object.entries(initial));
    return { data, getItem: (key) => data.get(key) ?? null, setItem: (key, value) => void data.set(key, value) };
}

test('an old key is copied to its new name, and the old one stays', () => {
    const s = store({ 'voidcraft:agent-rack-split-px': '312', 'vc:laya-review:prefs': '{"explain":true}' });
    assert.equal(migrate_keys(s), 2);
    assert.equal(s.data.get('tldrack:agent-rack-split-px'), '312');
    assert.equal(s.data.get('tldrack:laya-review-prefs'), '{"explain":true}');
    assert.equal(s.data.get('voidcraft:agent-rack-split-px'), '312');
});

test('a value the new name already holds is never overwritten', () => {
    const s = store({ 'voidcraft:rack-jobs-open': '0', 'tldrack:rack-jobs-open': '1' });
    assert.equal(migrate_keys(s), 0);
    assert.equal(s.data.get('tldrack:rack-jobs-open'), '1');
});

test('it runs once: a setting cleared afterwards is not brought back', () => {
    const s = store({ 'voidcraft:agent-rack-remote': '{"ttyd_origin":"x"}' });
    migrate_keys(s);
    s.data.delete('tldrack:agent-rack-remote');
    assert.equal(migrate_keys(s), 0);
    assert.equal(s.data.has('tldrack:agent-rack-remote'), false);
    assert.notEqual(s.data.get(MIGRATED_KEY), undefined);
});

test('keys of another application on the origin are left alone', () => {
    const s = store({ 'voidcraft:something-else': 'x' });
    assert.equal(migrate_keys(s), 0);
    assert.deepEqual([...s.data.keys()].sort(), [MIGRATED_KEY, 'voidcraft:something-else'].sort());
});

test('every new name is one this app reads under the tldrack prefix', () => {
    for (const [old_key, new_key] of KEY_MOVES) {
        assert.match(new_key, /^tldrack:[a-z0-9-]+$/, new_key);
        assert.notEqual(old_key, new_key);
    }
    assert.equal(new Set(KEY_MOVES.map(([, new_key]) => new_key)).size, KEY_MOVES.length);
});
