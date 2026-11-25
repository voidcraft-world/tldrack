import test from 'node:test';
import assert from 'node:assert/strict';
import { build_rack_rows, cohort_label_for, hold_row_order, rack_row_key, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { attention_rows, fold_sidebar_rows, is_directory_heading } from '../../src/agent-rack/attention-rows.ts';
import { row_asks, fold_rows } from '../../src/agent-rack/family-fold.ts';
import { search_rack_rows, starred_rack_rows } from '../../src/agent-rack/rack-search.ts';

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
    const folded = fold_sidebar_rows(rows, false);
    assert.equal(folded.find((row) => row.cohort === 'Services')?.text, 'Services · 2');
    assert.ok(!folded.some((row) => row.session === 'svc-web' || row.session === 'svc-closed'));
    assert.ok(folded.some((row) => row.session === 'svc-failed'));
    assert.ok(folded.some((row) => row.session === 'working'));
    assert.ok(folded.some((row) => row.session === 'finished'));
    assert.deepEqual(fold_sidebar_rows(rows, true), rows);
});


function grouped_rows(sessions: RackSession[]) {
    return attention_rows(rows_for(sessions), asks, new Map(sessions.flatMap((session) => {
        const label = cohort_label_for(session);
        return label ? [[session.session, label] as const] : [];
    })));
}

test('In progress groups by canonical directory and keeps a worker with its parent', () => {
    const rows = grouped_rows([
        { session: 'lead', live: true, state: 'busy', repo: 'alpha', cwd: '/workspace/alpha', lineage: { id: id(1) } },
        { session: 'worker', live: true, state: 'busy', repo: 'beta', lineage: { id: id(2), parent_id: id(1) } },
        { session: 'other-checkout', live: true, state: 'busy', repo: 'alpha', cwd: '/worktrees/alpha/fix' },
        { session: 'second', live: true, state: 'busy', repo: 'beta' },
        { session: 'unknown-dir', live: true, state: 'busy' },
        { session: 'svc-web', live: true, state: 'unknown', repo: 'alpha' },
    ]);
    const headings = rows.filter(is_directory_heading);
    assert.deepEqual(headings.map((row) => [row.text, row.cohort_count]).sort(), [['No directory', 1], ['alpha', 3], ['beta', 1]]);
    assert.equal(rows.find((row) => row.cohort === 'In progress')?.text, 'In progress · 5');
    const closed = new Map([['directory:alpha', true]]);
    const folded = fold_sidebar_rows(rows, true, closed);
    assert.ok(folded.includes(headings.find((row) => row.text === 'alpha')!));
    assert.ok(!folded.some((row) => ['lead', 'worker', 'other-checkout'].includes(row.session ?? '')));
    assert.ok(folded.some((row) => row.session === 'second'));
    assert.ok(folded.some((row) => row.session === 'svc-web'));
    assert.deepEqual(fold_sidebar_rows(rows, true, new Map([['directory:alpha', false]])), rows);
});

test('search and stars retain the attention band, directory and family for hidden matches', () => {
    const rows = grouped_rows([
        { session: 'lead', live: true, state: 'busy', repo: 'alpha', lineage: { id: id(1) } },
        { session: 'target', live: true, state: 'busy', repo: 'alpha', starred: true, lineage: { id: id(2), parent_id: id(1) } },
        { session: 'unrelated', live: true, state: 'busy', repo: 'beta' },
    ]);
    for (const matches of [search_rack_rows(rows, 'target'), starred_rack_rows(rows), search_rack_rows(starred_rack_rows(rows), 'target')]) {
        assert.deepEqual(matches.map((row) => row.cohort ?? row.session), ['In progress', 'directory:alpha', 'lead', 'target']);
    }
    assert.deepEqual(search_rack_rows(rows, 'no match'), []);
});

test('folds hide directory notes but retain counts, overflow and newly asking families during rename', () => {
    const sessions: RackSession[] = [
        { session: 'lead', live: true, state: 'busy', repo: 'alpha', lineage: { id: id(1) } },
        { session: 'worker', live: true, state: 'busy', repo: 'alpha', lineage: { id: id(2), parent_id: id(1) } },
        { session: 'closed', live: false, state: 'unknown', repo: 'alpha' },
        { session: 'other', live: true, state: 'busy', repo: 'alpha' },
    ];
    const original = grouped_rows(sessions);
    const choices = new Map([['directory:alpha', true]]);
    const folded = fold_sidebar_rows([...original, { kind: 'overflow', text: '+1 MORE', dim: false, focused: false, actions: [] }], false, choices);
    assert.ok(!folded.some((row) => row.kind === 'session' || row.kind === 'note'));
    assert.equal(folded.find(is_directory_heading)?.cohort_count, 4);
    assert.equal(folded.at(-1)?.text, '+1 MORE');
    sessions[1] = { ...sessions[1], state: 'attention' };
    const visible = fold_sidebar_rows(grouped_rows(sessions), false, choices);
    const held = hold_row_order(original.map(rack_row_key), visible);
    assert.deepEqual(held.filter((row) => row.kind === 'session').map((row) => row.session), ['lead', 'worker']);
    assert.ok(held.some((row) => row.cohort === 'Needs you'));
});
