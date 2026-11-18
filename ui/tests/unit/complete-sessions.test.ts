import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import { COMPLETE_COHORT_LABEL, is_complete_session } from '../../src/engine/terminal/complete-sessions.ts';
import { build_rack_rows, type RackRow, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { with_island } from '../../src/engine/terminal/rack-view.ts';
import { TERMINAL_STATUS_FLAGS } from '../../src/config/terminal-status-flags.ts';

/** A finished agent: idle, Laya judged its last turn done, in the main checkout. */
function done(overrides: Partial<RackSession> & { session: string }): RackSession {
    return { state: 'free', live: true, repo: 'my-app', work_done: { p: 0.1 }, ...overrides };
}

function working(overrides: Partial<RackSession> & { session: string }): RackSession {
    return { state: 'busy', live: true, repo: 'my-app', ...overrides };
}

const ISLAND = { path: '~/worktrees/my-app/feat-x', label: 'feat/x', title: 'Worktree: feat/x' };

function rows_of(sessions: RackSession[], max_sessions = 32): RackRow[] {
    return build_rack_rows({ sessions, focused: null, max_sessions });
}

function heads(rows: RackRow[]): string[] {
    return rows.filter((row) => row.kind === 'cohort').map((row) => row.text);
}

function names(rows: RackRow[], pick: (row: RackRow) => boolean = () => true): string[] {
    return rows.filter((row) => row.kind === 'session' && pick(row)).map((row) => row.session!);
}

describe('is_complete_session', () => {
    test('a finished agent in the main checkout is complete once its ✅ has been seen', () => {
        assert.equal(is_complete_session(done({ session: 'on-main' })), true);
    });

    test('an unread ✅ is never complete: Laya alone never hides a row', () => {
        assert.equal(is_complete_session(done({ session: 'on-main', state: 'done' })), false);
    });

    test('an unread answer is never complete, even on a row a host already marked seen', () => {
        assert.equal(is_complete_session(done({ session: 'on-main', work_done: { p: 0.1, answer: true } })), false);
    });

    test('finished but standing in a worktree is not complete', () => {
        assert.equal(is_complete_session(done({ session: 'wt-feat-x', island: ISLAND })), false);
    });

    test('no Laya verdict, or one that left work open, is not complete', () => {
        assert.equal(is_complete_session(done({ session: 'a', work_done: undefined })), false);
        assert.equal(is_complete_session(done({ session: 'a', open_work: { p: 0.9 } })), false);
    });

    test('a turn that is not over, was cut off, or whose session is gone is not complete', () => {
        for (const state of ['busy', 'attention', 'background', 'unknown'] as const) {
            assert.equal(is_complete_session(done({ session: 'a', state })), false, state);
        }
        assert.equal(is_complete_session(done({ session: 'a', stop_failure: { kind: 'rate_limit', resets_at: null } as never })), false);
        assert.equal(is_complete_session(done({ session: 'a', live: false })), false);
    });

    test('a service row is never complete', () => {
        assert.equal(is_complete_session(done({ session: 'laya-train' })), false);
    });

    test('turning the ✅ mark off empties the band with it', () => {
        const saved = TERMINAL_STATUS_FLAGS.work_done_mark;
        TERMINAL_STATUS_FLAGS.work_done_mark = false;
        try {
            assert.equal(is_complete_session(done({ session: 'a' })), false);
        } finally {
            TERMINAL_STATUS_FLAGS.work_done_mark = saved;
        }
    });
});

describe('the COMPLETE band', () => {
    test('a complete session leaves its project for a trailing band with a count head', () => {
        const rows = rows_of([working({ session: 'agent-a' }), done({ session: 'on-main' })]);
        // One project left, so it keeps no header; the band always draws its own.
        assert.deepEqual(heads(rows), [`${COMPLETE_COHORT_LABEL} · 1`]);
        assert.deepEqual(names(rows), ['agent-a', 'on-main']);
        const band = rows.slice(rows.findIndex((row) => row.kind === 'cohort'));
        assert.ok(band.every((row) => row.complete === true));
        assert.equal(rows.find((row) => row.session === 'agent-a')!.complete, undefined);
    });

    test('finished in a worktree stays with its project', () => {
        const rows = rows_of([working({ session: 'agent-a' }), done({ session: 'wt-feat-x', island: ISLAND })]);
        assert.deepEqual(heads(rows), []);
        assert.deepEqual(names(rows, (row) => row.complete === true), []);
    });

    test('the band sorts after every cohort, (no path) included', () => {
        const rows = rows_of([
            done({ session: 'b-read' }),
            done({ session: 'a-read' }),
            working({ session: 'docs', repo: 'my-docs' }),
            working({ session: 'vc-1', repo: null, cwd: null }),
            working({ session: 'agent-a' }),
        ]);
        assert.deepEqual(heads(rows), ['my-app — 1 working', 'my-docs — 1 working', '(no path) — 1 working', `${COMPLETE_COHORT_LABEL} · 2`]);
        assert.deepEqual(names(rows, (row) => row.complete === true), ['a-read', 'b-read']);
    });

    test('an unread ✅ stays with its project until opened, then joins the band', () => {
        const unread = rows_of([working({ session: 'agent-a' }), done({ session: 'on-main', state: 'done' })]);
        assert.deepEqual(heads(unread), []);
        assert.deepEqual(names(unread, (row) => row.complete === true), []);
        assert.ok(names(unread).includes('on-main'));
        const seen = rows_of([working({ session: 'agent-a' }), done({ session: 'on-main' })]);
        assert.deepEqual(names(seen, (row) => row.complete === true), ['on-main']);
    });

    test('a family is complete only when every member is', () => {
        const parent = { id: 'p', parent_id: null } as never;
        const child = { id: 'c', parent_id: 'p' } as never;
        const mixed = rows_of([done({ session: 'parent', lineage: parent }), working({ session: 'child', lineage: child })]);
        assert.deepEqual(heads(mixed), []);
        const all = rows_of([done({ session: 'parent', lineage: parent }), done({ session: 'child', lineage: child })]);
        assert.deepEqual(heads(all), [`${COMPLETE_COHORT_LABEL} · 2`]);
        assert.deepEqual(names(all, (row) => row.complete === true), ['parent', 'child']);
    });

    test('complete families are the first behind +N MORE, even before a dead session', () => {
        const rows = rows_of([
            done({ session: 'a-done' }),
            done({ session: 'b-done' }),
            working({ session: 'gone', live: false }),
            working({ session: 'z-busy' }),
        ], 2);
        assert.deepEqual(names(rows), ['z-busy', 'gone']);
        assert.equal(rows.at(-1)!.text, '+2 MORE');
    });

    test('a rack that is all complete is the band alone', () => {
        const rows = rows_of([done({ session: 'on-main' })]);
        assert.deepEqual(heads(rows), [`${COMPLETE_COHORT_LABEL} · 1`]);
    });
});

describe('with_island', () => {
    test('joins the worktree a session stands in, so the band can see it', () => {
        const islands = [{ path: '~/worktrees/my-app/feat-x', branch: 'feat/x' }];
        const inside = with_island(done({ session: 'agent', cwd: '~/worktrees/my-app/feat-x/ui' }), islands);
        assert.equal(inside.island?.path, '~/worktrees/my-app/feat-x');
        assert.equal(is_complete_session(inside), false);
        const main = with_island(done({ session: 'on-main', cwd: '~/code/my-app' }), islands);
        assert.equal(main.island, undefined);
        assert.equal(is_complete_session(main), true);
    });
});
