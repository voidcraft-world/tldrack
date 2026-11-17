import test from 'node:test';
import assert from 'node:assert/strict';
import { chat_query, focus_snippet, highlight_terms, query_terms, search_rack_rows } from '../../src/agent-rack/rack-search.ts';
import type { RackRow } from '../../src/engine/terminal/multi-terminal-layout.ts';

const heading = (text: string): RackRow => ({ kind: 'cohort', text, dim: false, focused: false, actions: [] });
const session = (name: string, extra: Partial<RackRow> = {}): RackRow => ({
    kind: 'session', session: name, text: name, dim: false, focused: false, actions: [], ...extra,
});

test('name search preserves order and ignores case, matching generated names too', () => {
    const a = session('vc-1', { auto_name: 'Billing invoice details' });
    const b = session('billing-other');
    const rows = [heading('repo'), a, b];
    assert.deepEqual(search_rack_rows(rows, ' BILLING details '), [rows[0], a]);
    assert.deepEqual(search_rack_rows(rows, 'vc-1'), [rows[0], a]);
    assert.deepEqual(search_rack_rows(rows, '   '), rows);
    assert.deepEqual(search_rack_rows(rows, 'missing'), []);
});

test('matching child retains ancestors and its heading without unrelated rows', () => {
    const parent = session('parent', { node_id: 'p' });
    const child = session('target', { node_id: 'c', ancestors: ['p'], complete: true });
    const rows = [heading('first'), session('irrelevant'), heading('second'), parent, child];
    assert.deepEqual(search_rack_rows(rows, 'target'), [rows[2], parent, child]);
});

test('a chat match adds its row, with its family and heading, beside the title matches', () => {
    const parent = session('parent', { node_id: 'p' });
    const child = session('vc-7', { node_id: 'c', ancestors: ['p'], auto_name: 'Invoice totals' });
    const titled = session('ledger-fix');
    const rows = [heading('repo'), parent, child, titled, session('other')];
    assert.deepEqual(search_rack_rows(rows, 'ledger', { 'vc-7': {} }), [rows[0], parent, child, titled]);
    // An answer naming a session not on screen adds nothing.
    assert.deepEqual(search_rack_rows(rows, 'ledger', { ghost: {} }), [rows[0], titled]);
});

test('chat_query is the one normal form, and terms fold case', () => {
    assert.equal(chat_query('  the   Cache\tfix '), 'the Cache fix');
    assert.deepEqual(query_terms('  the   Cache\tfix '), ['the', 'cache', 'fix']);
});

test('highlight_terms marks every term, merges overlaps and caps the marks', () => {
    assert.deepEqual(highlight_terms('Fix the Ledger total', 'ledger TOTAL'), [
        { text: 'Fix the ', match: false },
        { text: 'Ledger', match: true },
        { text: ' ', match: false },
        { text: 'total', match: true },
    ]);
    assert.deepEqual(highlight_terms('abcd', 'abc bcd'), [{ text: 'abcd', match: true }]);
    assert.ok(highlight_terms('a'.repeat(500), 'a').filter((part) => part.match).length <= 40);
    assert.deepEqual(highlight_terms('<b>x</b>', 'x'), [
        { text: '<b>', match: false }, { text: 'x', match: true }, { text: '</b>', match: false },
    ]);
});

test('focus_snippet brings the first match near the start, at a word boundary', () => {
    const text = 'one two three four five six seven eight nine ten\n eleven twelve the cache broke again';
    const focused = focus_snippet(text, 'cache');
    assert.ok(focused.startsWith('…') && focused.endsWith('the cache broke again'));
    assert.ok(focused.indexOf('cache') <= 26);
    assert.ok(!/\s{2}|\n/.test(focused) && !focused.slice(1).startsWith(' '));
    assert.equal(focus_snippet('the cache', 'cache'), 'the cache');
    assert.equal(focus_snippet('no hit here', 'cache'), 'no hit here');
});
