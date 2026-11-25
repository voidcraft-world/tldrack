import test from 'node:test';
import assert from 'node:assert/strict';
import { build_rack_rows, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { attention_rows, fold_services } from '../../src/agent-rack/attention-rows.ts';
import { row_asks, fold_rows } from '../../src/agent-rack/family-fold.ts';
import { search_rack_rows } from '../../src/agent-rack/rack-search.ts';

const asks = (row: Parameters<typeof row_asks>[0]) => row_asks(row, []);
function rows_for(sessions: RackSession[]) {
    return build_rack_rows({ sessions, focused: null, max_sessions: 40 });
}
const id = (number: number) => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;

test('attention leads across projects without detaching an asking worker from its parent', () => {
    const rows = rows_for([
        { session: 'working', live: true, state: 'busy', repo: 'first' },
        { session: 'lead', live: true, state: 'busy', repo: 'second', lineage: { id: id(1) } },
        { session: 'worker', live: true, state: 'attention', repo: 'third', lineage: { id: id(2), parent_id: id(1) } },
    ]);
    const grouped = attention_rows(rows, asks);
    assert.equal(grouped[0].text, 'Needs you · 2');
    assert.deepEqual(grouped.filter((row) => row.kind === 'session').map((row) => row.session), ['lead', 'worker', 'working']);
    assert.equal(grouped.find((row) => row.session === 'worker')?.depth, 1);
    const folded = fold_rows(grouped, new Set([id(1)]), asks);
    assert.ok(folded.some((row) => row.session === 'worker'));
});

test('completed sessions stay searchable and services retain their own band', () => {
    const rows = attention_rows(rows_for([
        { session: 'done', live: true, state: 'free', work_done: { p: 0.1 } },
        { session: 'svc-web', live: true, state: 'unknown' },
        { session: 'working', live: true, state: 'busy' },
    ]), asks);
    assert.deepEqual(rows.filter((row) => row.kind === 'cohort').map((row) => row.cohort), ['In progress', 'Services', 'Completed']);
    assert.ok(rows.find((row) => row.session === 'done')?.complete);
    assert.ok(search_rack_rows(rows, 'done', {}).some((row) => row.session === 'done'));
});

test('dead attention is not an ask and every row survives regrouping', () => {
    const original = rows_for([{ session: 'closed', live: false, state: 'attention' }, { session: 'reading', live: true, state: 'done' }]);
    const grouped = attention_rows(original, asks);
    assert.equal(grouped.find((row) => row.kind === 'session')?.session, 'reading');
    assert.deepEqual(grouped.filter((row) => row.kind !== 'cohort').map((row) => row.text).sort(), original.filter((row) => row.kind !== 'cohort').map((row) => row.text).sort());
    assert.deepEqual(attention_rows(rows_for([]), asks), rows_for([]));
});

test('folding services keeps their count, reveals them on demand, and never hides an ask', () => {
    const rows = attention_rows(rows_for([
        { session: 'svc-web', live: true, state: 'unknown' },
        { session: 'svc-failed', live: true, state: 'attention', message: 'Check server' },
        { session: 'svc-closed', live: false, state: 'unknown' },
        { session: 'working', live: true, state: 'busy' },
        { session: 'finished', live: true, state: 'free', work_done: { p: 0.1 } },
    ]), asks);
    const folded = fold_services(rows, false);
    assert.equal(folded.find((row) => row.cohort === 'Services')?.text, 'Services · 2');
    assert.ok(!folded.some((row) => row.session === 'svc-web' || row.session === 'svc-closed'));
    assert.ok(folded.some((row) => row.session === 'svc-failed'));
    assert.ok(folded.some((row) => row.session === 'working'));
    assert.ok(folded.some((row) => row.session === 'finished'));
    assert.deepEqual(fold_services(rows, true), rows);
});
