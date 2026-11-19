import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { build_rack_rows, type RackRow, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { search_rack_rows } from '../../src/agent-rack/rack-search.ts';
import {
    FAMILY_FOLD_MAX, choose_fold, family_finished, family_readout, family_readout_text, fold_rows, folded_families,
    prune_fold_choices, read_fold_choices, row_asks, write_fold_choices,
} from '../../src/agent-rack/family-fold.ts';
import { parse_task_groups, worker_stage_word, type TaskGroup } from '../../src/agent-rack/task-groups.ts';

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const PARENT = id(1);

function agent(name: string, n: number, parent?: number, state: RackSession['state'] = 'busy'): RackSession {
    return { session: name, live: true, state, repo: 'project',
        lineage: { id: id(n), ...(parent ? { parent_id: id(parent), parent_name: 'lead' } : {}) } };
}
function rows_of(sessions: RackSession[], focused: string | null = null): RackRow[] {
    return build_rack_rows({ sessions, max_sessions: 40, focused });
}
/** A parent with eleven workers, w1…w11. */
function eleven(state: RackSession['state'] = 'busy', overrides: Record<string, Partial<RackSession>> = {}): RackSession[] {
    return [agent('lead', 1, undefined, 'free'),
        ...Array.from({ length: 11 }, (_, i) => ({ ...agent(`w${i + 1}`, i + 2, 1, state), ...overrides[`w${i + 1}`] }))];
}
function worker(session: string, extra: Partial<TaskGroup['workers'][number]> = {}): TaskGroup['workers'][number] {
    return { branch: `lane/${session}`, session, stage: 'implement', note: '', error: null, pr_status: 'pending',
        pr_url: null, ...extra };
}
function group(n: number, workers: TaskGroup['workers'], extra: Partial<TaskGroup> = {}): TaskGroup {
    return { id: id(100 + n), label: `group ${n}`, parent_id: PARENT, parent_session: 'lead', auto_pr: true, sealed: true,
        percent: 50, total: workers.length, ready: 0, opened: 0, blockers: 0, phase: 'working', workers, ...extra };
}
const sessions_of = (rows: RackRow[]) => rows.filter((row) => row.kind === 'session').map((row) => row.session);
const never = () => false;

describe('a folded family is one row', () => {
    test('a parent with eleven workers takes one row, with one readout and one meter for every group', () => {
        const rows = rows_of(eleven());
        const groups = [
            group(1, ['w1', 'w2', 'w3', 'w4'].map((s) => worker(s)), { percent: 100, blockers: 0 }),
            group(2, ['w5', 'w6', 'w7', 'w8'].map((s) => worker(s)), { percent: 50, blockers: 1 }),
            group(3, ['w9', 'w10'].map((s) => worker(s)), { percent: 0 }),
            group(4, ['w11'].map((s) => worker(s)), { percent: 0, blockers: 1 }),
        ];
        const folded = folded_families(rows, groups, new Map([[PARENT, true]]));
        assert.deepEqual(sessions_of(fold_rows(rows, folded, never)), ['lead']);
        const parent = rows.find((row) => row.session === 'lead')!;
        const readout = family_readout(parent, rows, groups, never);
        // (100·4 + 50·4 + 0·2 + 0·1) / 11, weighted by workers.
        assert.deepEqual(readout, { workers: 11, busy: 11, asks: 0, percent: 55, blockers: 2 });
        assert.equal(family_readout_text(readout), '11 workers · 11 busy');
    });

    test('a parent without task groups draws no meter', () => {
        const rows = rows_of(eleven('free'));
        const readout = family_readout(rows.find((row) => row.session === 'lead')!, rows, [], never);
        assert.equal(readout.percent, null);
        assert.equal(family_readout_text(readout), '11 workers · all free');
    });

    test('an unfolded family keeps every row', () => {
        const rows = rows_of(eleven());
        assert.equal(fold_rows(rows, folded_families(rows, [], new Map()), never).length, rows.length);
    });
});

describe('a fold never hides an ask', () => {
    test('a child on the ⚠️ band keeps its row, and the rest stay folded', () => {
        const rows = rows_of(eleven('busy', { w4: { state: 'attention' } }));
        const asks = (row: RackRow) => row_asks(row, []);
        const shown = fold_rows(rows, new Set([PARENT]), asks);
        assert.deepEqual(sessions_of(shown), ['lead', 'w4']);
        const readout = family_readout(rows.find((row) => row.session === 'lead')!, rows, [], asks);
        assert.equal(family_readout_text(readout), '11 workers · 10 busy · 1 needs you');
    });

    test("Laya's open-work badge and an unread answer are asks", () => {
        const rows = rows_of(eleven('busy', {
            w2: { state: 'free', open_work: { p: 0.9 } },
            w3: { state: 'free', work_done: { p: 0.1, answer: true } },
        }));
        const asks = (row: RackRow) => row_asks(row, []);
        assert.deepEqual(sessions_of(fold_rows(rows, new Set([PARENT]), asks)), ['lead', 'w2', 'w3']);
    });

    test('a blocked task-group worker is an ask; a landed one is not', () => {
        const rows = rows_of(eleven());
        const groups = [group(1, [worker('w5', { stage: 'blocked' }), worker('w6', { error: 'push refused' }),
            worker('w7', { stage: 'blocked', landing: { how: 'squash', pr: 9, branch: 'lane/w7' } })])];
        const asks = (row: RackRow) => row_asks(row, groups);
        assert.deepEqual(sessions_of(fold_rows(rows, new Set([PARENT]), asks)), ['lead', 'w5', 'w6']);
    });

    test('a grandchild that asks shows with the child between it and the parent', () => {
        const rows = rows_of([agent('lead', 1, undefined, 'free'), agent('mid', 2, 1), agent('deep', 3, 2, 'attention'),
            agent('other', 4, 1)]);
        const shown = fold_rows(rows, new Set([PARENT]), (row) => row_asks(row, []));
        assert.deepEqual(sessions_of(shown), ['lead', 'mid', 'deep']);
    });

    test('the focused session stays on screen under a fold', () => {
        const rows = rows_of(eleven(), 'w9');
        assert.deepEqual(sessions_of(fold_rows(rows, new Set([PARENT]), never)), ['lead', 'w9']);
    });
});

describe('a finished family arrives folded', () => {
    const done = () => [group(1, [worker('w1', { pr_state: 'merged' }),
        worker('w2', { landing: { how: 'pr', pr: 4, branch: 'lane/w2' } })], { phase: 'complete', percent: 100 })];

    test('every worker free and every group complete folds it with no choice stored', () => {
        const rows = rows_of(eleven('free'));
        assert.ok(family_finished(rows.find((row) => row.session === 'lead')!, rows, done()));
        assert.deepEqual([...folded_families(rows, done(), new Map())], [PARENT]);
    });

    test('a busy worker, or a group still working, leaves it open', () => {
        const busy = rows_of(eleven('free', { w3: { state: 'busy' } }));
        assert.equal(folded_families(busy, done(), new Map()).size, 0);
        const free = rows_of(eleven('free'));
        assert.equal(folded_families(free, [group(1, [worker('w1')])], new Map()).size, 0);
    });

    test('a press by hand wins over the default, both ways', () => {
        const finished = rows_of(eleven('free'));
        assert.equal(folded_families(finished, done(), new Map([[PARENT, false]])).size, 0);
        const working = rows_of(eleven());
        assert.equal(folded_families(working, [], new Map([[PARENT, true]])).size, 1);
    });
});

describe('the fold is remembered', () => {
    test('a choice survives a reload, and junk reads as none', () => {
        const stored = write_fold_choices(choose_fold(new Map(), PARENT, true));
        assert.deepEqual([...read_fold_choices(stored)], [[PARENT, true]]);
        for (const junk of [null, '', '{', '{"a":1}', '[["",true]]', '[[1,true]]', '[["x","yes"]]']) {
            assert.equal(read_fold_choices(junk).size, 0, String(junk));
        }
    });

    test('it is bounded, and the newest choice is the one kept', () => {
        let choices = new Map<string, boolean>();
        for (let i = 0; i < FAMILY_FOLD_MAX + 5; i++) choices = choose_fold(choices, `n${i}`, true);
        choices = choose_fold(choices, 'n10', false);
        assert.equal(choices.size, FAMILY_FOLD_MAX);
        assert.equal([...choices.keys()].at(-1), 'n10');
        assert.ok(!choices.has('n0'));
        assert.equal(read_fold_choices(write_fold_choices(choices)).size, FAMILY_FOLD_MAX);
    });

    test('a family gone past the grace is pruned; a brief gap and nothing to prune write nothing', () => {
        const rows = rows_of(eleven());
        const gone = id(999);
        const choices = new Map([[PARENT, true], [gone, false]]);
        const absent = new Map<string, number>();
        assert.equal(prune_fold_choices(choices, rows, 0, absent, 1000), null);
        assert.equal(prune_fold_choices(choices, rows, 999, absent, 1000), null);
        assert.deepEqual([...prune_fold_choices(choices, rows, 1000, absent, 1000)!], [[PARENT, true]]);
        assert.equal(absent.size, 0);
        // A family back inside the grace keeps its choice and its clock restarts.
        const back = new Map([[PARENT, true]]);
        prune_fold_choices(back, [], 0, absent, 1000);
        assert.equal(prune_fold_choices(back, rows, 500, absent, 1000), null);
        assert.equal(prune_fold_choices(back, [], 1200, absent, 1000), null);
        assert.equal(prune_fold_choices(back, [], 2199, absent, 1000), null);
        assert.equal(prune_fold_choices(back, [], 2200, absent, 1000)!.size, 0);
    });
});

describe('search reaches a folded child', () => {
    test('FIND returns the match with its parent whatever the fold says', () => {
        const rows = rows_of(eleven());
        assert.deepEqual(sessions_of(search_rack_rows(rows, 'w7')), ['lead', 'w7']);
    });
});

describe('a landed worker reads as landed', () => {
    test('its bar word says how it was delivered', () => {
        assert.equal(worker_stage_word(worker('a', { stage: 'blocked', landing: { how: 'squash', pr: 12, branch: 'b' } })), 'landed in #12');
        assert.equal(worker_stage_word(worker('a', { stage: 'blocked', landing: { how: 'ancestor', pr: null, branch: 'b' } })), 'landed');
        assert.equal(worker_stage_word(worker('a', { pr_state: 'merged' })), 'merged');
        assert.equal(worker_stage_word(worker('a', { stage: 'validate' })), 'validate');
    });

    test('the wire keeps a landing, and a malformed one reads as absent without dropping the group', () => {
        const wire = group(1, [worker('a', { landing: { how: 'pr', pr: 3, branch: 'lane/a' } }), worker('b')]);
        const bad = { ...wire, workers: [{ ...wire.workers[0], landing: { how: 'teleport' } }, wire.workers[1]] };
        assert.deepEqual(parse_task_groups([wire])![0].workers[0].landing, { how: 'pr', pr: 3, branch: 'lane/a' });
        const parsed = parse_task_groups([bad])!;
        assert.equal(parsed.length, 1);
        assert.equal(parsed[0].workers[0].landing, undefined);
    });
});
