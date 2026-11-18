import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import {
    closed_ago,
    closed_place,
    closed_rows_view,
    create_closed_list,
    parse_closed,
    parse_restorable,
    reopen_closed,
    restorable_rows_view,
    restore_notice,
    type ClosedSession,
    type RestorableSession,
} from '../../src/agent-rack/closed-client.ts';
import { build_rack_payload } from '../../src/engine/terminal/rack-view.ts';

const NOW_S = 1_800_000_000;

function row(overrides: Partial<ClosedSession> = {}): ClosedSession {
    return {
        id: 'a1b2c3d4e5f6',
        session: 'vc-3',
        label: 'fix the nav sidebar',
        cwd: '/Users/u/code/my-app',
        profile: 'claude-a',
        kind: 'claude',
        how: 'killed',
        closed_at: NOW_S - 720,
        resumable: true,
        ok: true,
        ...overrides,
    };
}

function response(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status });
}

const url = (path: string) => `http://127.0.0.1:7682${path}`;

describe('parse_closed', () => {
    test('anything that is not a closed-list document is "no list"', () => {
        assert.equal(parse_closed(null), null);
        assert.equal(parse_closed({}), null);
        assert.equal(parse_closed({ sessions: 'no' }), null);
    });

    test('a bad row is dropped alone, and a missing label reads null', () => {
        const { label: _label, ...unlabelled } = row();
        assert.deepEqual(parse_closed({ sessions: [unlabelled, { id: 'not-a-close-id', session: 'x' }] }), [
            row({ label: null }),
        ]);
    });
});

describe('the row view', () => {
    test('ages read in the rack shorthand', () => {
        assert.equal(closed_ago(NOW_S - 30, NOW_S * 1000), 'now');
        assert.equal(closed_ago(NOW_S - 720, NOW_S * 1000), '12m');
        assert.equal(closed_ago(NOW_S - 3 * 3600, NOW_S * 1000), '3h');
        assert.equal(closed_ago(NOW_S - 2 * 86_400, NOW_S * 1000), '2d');
    });

    test('the place is the folder a worktree is named by', () => {
        assert.equal(closed_place('/Users/u/worktrees/my-app/fix-nav'), 'fix-nav');
        assert.equal(closed_place(null), '');
    });

    test('a place every row shares is wallpaper and is dropped', () => {
        const same = closed_rows_view([row(), row({ id: 'b1b2c3d4e5f6', session: 'vc-4', label: null })], NOW_S * 1000);
        assert.deepEqual(same.map((r) => [r.name, r.place]), [['fix the nav sidebar', ''], ['vc-4', '']]);
        const mixed = closed_rows_view([row(), row({ id: 'b1b2c3d4e5f6', cwd: '/w/fix-nav' })], NOW_S * 1000);
        assert.deepEqual(mixed.map((r) => r.place), ['my-app', 'fix-nav']);
    });

    test('the title says whether the conversation comes back, and why a row cannot', () => {
        const [fresh, refused] = closed_rows_view(
            [row({ resumable: false }), row({ id: 'b1b2c3d4e5f6', ok: false, reason: 'cli missing' })],
            NOW_S * 1000,
        );
        assert.match(fresh.title, /starts a fresh session/);
        assert.match(refused.title, /Cannot reopen vc-3: cli missing/);
    });
});

describe('reopen_closed', () => {
    test('a 200 is the session the bridge spawned', async () => {
        const outcome = await reopen_closed(url, 'a1b2c3d4e5f6', async (input, init) => {
            assert.equal(input, url('/reopen'));
            assert.equal(init?.method, 'POST');
            assert.deepEqual(JSON.parse(String(init?.body)), { id: 'a1b2c3d4e5f6' });
            return response(200, { ok: true, session: 'vc-1', profile: 'claude-a', resumed: true });
        });
        assert.deepEqual(outcome, { ok: true, session: 'vc-1', profile: 'claude-a', resumed: true });
    });

    test('a refusal carries the bridge’s reason; silence carries its own', async () => {
        const refused = await reopen_closed(url, 'a1b2c3d4e5f6', async () => response(409, { ok: false, error: 'cli missing' }));
        assert.deepEqual(refused, { ok: false, error: 'Reopen refused: cli missing' });
        const silent = await reopen_closed(url, 'a1b2c3d4e5f6', async () => {
            throw new TypeError('fetch failed');
        });
        assert.equal(silent.ok, false);
    });
});

describe('create_closed_list', () => {
    function harness(answers: { closed?: ClosedSession[]; reopen?: Response } = {}) {
        const calls: string[] = [];
        const events: string[] = [];
        const list = create_closed_list({
            base_url: url,
            fetch_impl: async (input) => {
                const path = String(input).replace(url(''), '');
                calls.push(path);
                if (path === '/closed') return response(200, { max_age_days: 7, sessions: answers.closed ?? [row()] });
                return answers.reopen ?? response(200, { ok: true, session: 'vc-3', profile: 'claude-a', resumed: true });
            },
            on_change: () => events.push('change'),
            on_reopened: (session, profile) => events.push(`reopened ${session} ${profile}`),
            on_failure: (message) => events.push(`failure ${message}`),
        });
        return { list, calls, events };
    }

    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    test('no count, no button; a count draws it without reading the list', () => {
        const { list, calls } = harness();
        assert.equal(list.view(), undefined);
        list.observe_count(2);
        assert.deepEqual(list.view(), { count: 2, open: false, rows: null });
        assert.deepEqual(calls, []);
    });

    test('opening reads the list; a count that moves while open reads it again', async () => {
        const { list, calls } = harness();
        list.observe_count(1);
        list.toggle();
        await settle();
        assert.deepEqual(list.view()?.rows, [row()]);
        list.observe_count(2);
        await settle();
        assert.deepEqual(calls, ['/closed', '/closed']);
    });

    test('a reopen focuses the session and takes its row', async () => {
        const { list, events } = harness();
        list.observe_count(1);
        list.toggle();
        await settle();
        await list.reopen('a1b2c3d4e5f6');
        assert.ok(events.includes('reopened vc-3 claude-a'));
        assert.deepEqual(list.view()?.rows, []);
        list.observe_count(0);
        assert.equal(list.view(), undefined, 'the list folds away once nothing is left');
    });

    test('a refused reopen is a notice and a fresh read, never a focus', async () => {
        const { list, events, calls } = harness({ reopen: response(409, { ok: false, error: 'cli missing' }) });
        list.observe_count(1);
        list.toggle();
        await settle();
        await list.reopen('a1b2c3d4e5f6');
        await settle();
        assert.ok(events.includes('failure Reopen refused: cli missing'));
        assert.ok(!events.some((e) => e.startsWith('reopened')));
        assert.deepEqual(calls, ['/closed', '/reopen', '/closed']);
    });

    test('handle_action takes its two actions and lets every other one through', async () => {
        const { list, calls } = harness();
        list.observe_count(1);
        assert.equal(list.handle_action('select', 'vc-2'), false);
        assert.equal(list.handle_action('reopen', null), true, 'a reopen without an id is still ours, and sends nothing');
        assert.equal(list.handle_action('closed'), true);
        assert.equal(list.view()?.open, true);
        await settle();
        assert.deepEqual(calls, ['/closed']);
    });

    test('a second press while one reopen is in flight sends nothing', async () => {
        const { list, calls } = harness();
        list.observe_count(1);
        const first = list.reopen('a1b2c3d4e5f6');
        assert.equal(list.view()?.reopening, 'a1b2c3d4e5f6');
        await list.reopen('a1b2c3d4e5f6');
        await first;
        assert.deepEqual(calls, ['/reopen']);
    });
});

describe('the payload', () => {
    test('`closed` rides the payload only when the host has a view', () => {
        const base = { rows: [], focused: null, reachable: true, max_sessions: 8 };
        assert.equal('closed' in build_rack_payload(base), false);
        const view = { count: 1, open: false, rows: null };
        assert.deepEqual(build_rack_payload({ ...base, closed: view }).closed, view);
    });
});

// ── after a reboot ──────────────────────────────────────────────────────

function restorable(overrides: Partial<RestorableSession> = {}): RestorableSession {
    return {
        session: 'vc-22',
        label: 'bench laya vs opus',
        cwd: '/Users/u/code/my-app',
        profile: 'claude-a',
        kind: 'claude',
        resumable: true,
        age_seconds: 975,
        ok: true,
        ...overrides,
    };
}

describe('the restore band', () => {
    function harness(answers: { restorable?: RestorableSession[]; restore?: Response; forget?: Response } = {}) {
        const calls: string[] = [];
        const bodies: unknown[] = [];
        const events: string[] = [];
        const list = create_closed_list({
            base_url: url,
            fetch_impl: async (input, init) => {
                const path = String(input).replace(url(''), '');
                calls.push(path);
                if (init?.body) bodies.push(JSON.parse(String(init.body)));
                if (path === '/closed') return response(200, { max_age_days: 7, sessions: [row()] });
                if (path === '/restorable') return response(200, { max_age_days: 7, sessions: answers.restorable ?? [restorable()] });
                if (path === '/restore/forget') return answers.forget ?? response(200, { ok: true, forgotten: ['vc-22'] });
                return answers.restore ?? response(200, { ok: true, results: [{ session: 'vc-22', restored: true, resumed: true }] });
            },
            on_change: () => events.push('change'),
            on_reopened: (session, profile) => events.push(`reopened ${session} ${profile}`),
            on_failure: (message) => events.push(`failure ${message}`),
            on_restored: (sessions) => events.push(`restored ${sessions.join(',')}`),
        });
        return { list, calls, bodies, events };
    }

    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    test('a reboot alone draws the button, and opening reads only the band that has rows', async () => {
        const { list, calls } = harness();
        list.observe_count(undefined, 3);
        assert.deepEqual(list.view(), { count: 0, open: false, rows: null, restorable: { count: 3, rows: null } });
        list.toggle();
        await settle();
        assert.deepEqual(calls, ['/restorable']);
        assert.deepEqual(list.view()?.rows, [], 'no closes: an empty band, never a read');
        assert.deepEqual(list.view()?.restorable?.rows, [restorable()]);
    });

    test('one restore names its session, focuses it on its own account and takes its row', async () => {
        const { list, bodies, events } = harness();
        list.observe_count(0, 1);
        list.toggle();
        await settle();
        await list.restore('vc-22');
        assert.deepEqual(bodies, [{ sessions: ['vc-22'] }]);
        assert.ok(events.includes('reopened vc-22 claude-a'));
        assert.ok(!events.some((e) => e.startsWith('restored')));
    });

    test('RESTORE ALL asks for every row and hands back what came up; a refusal is a notice', async () => {
        const { list, bodies, events } = harness({
            restore: response(200, {
                ok: true,
                results: [
                    { session: 'vc-22', restored: true, resumed: true },
                    { session: 'vc-11', restored: false, reason: 'cwd gone: /gone' },
                ],
            }),
        });
        list.observe_count(0, 2);
        assert.equal(list.handle_action('restore', null), true);
        assert.equal(list.view()?.restorable?.restoring, '*');
        await settle();
        await settle();
        assert.deepEqual(bodies, [{}]);
        assert.ok(events.includes('restored vc-22'));
        assert.ok(events.includes('failure Restored 1; vc-11 not: cwd gone: /gone'));
    });

    test('a restore the bridge refuses whole is one notice', async () => {
        const { list, events } = harness({ restore: response(503, { ok: false, error: 'this bridge keeps no session records' }) });
        list.observe_count(0, 1);
        await list.restore(null);
        assert.ok(events.includes('failure Restore refused: this bridge keeps no session records'));
    });

    test('dismiss forgets every row and leaves the band empty', async () => {
        const { list, calls, bodies } = harness({ restorable: [restorable(), restorable({ session: 'vc-11', label: undefined })] });
        list.observe_count(0, 2);
        list.toggle();
        await settle();
        assert.equal(list.handle_action('restore_forget'), true);
        await settle();
        assert.deepEqual(calls, ['/restorable', '/restore/forget']);
        assert.deepEqual(bodies, [{ sessions: ['vc-22', 'vc-11'] }]);
        assert.deepEqual(list.view()?.restorable?.rows, []);
        list.observe_count(0, 0);
        assert.equal(list.view(), undefined, 'the list folds away once nothing is left');
    });

    test('a restore in flight holds a reopen, and the other way round', async () => {
        const { list, calls } = harness();
        list.observe_count(1, 1);
        const first = list.restore('vc-22');
        await list.reopen('a1b2c3d4e5f6');
        await first;
        assert.deepEqual(calls.filter((c) => c !== '/restorable'), ['/restore']);
    });
});

describe('the restore rows', () => {
    test('a refused row sinks below the ones that can come back and says why', () => {
        const views = restorable_rows_view(
            [restorable({ session: 'vc-11', ok: false, reason: 'cwd gone', label: undefined }), restorable()],
            NOW_S * 1000,
        );
        assert.deepEqual(views.map((v) => [v.name, v.ok, v.ago]), [['bench laya vs opus', true, '16m'], ['vc-11', false, '16m']]);
        assert.match(views[1].title, /^Cannot restore vc-11: cwd gone/);
        assert.match(views[0].title, /resumes the conversation · claude-a/);
    });

    test('parse_restorable drops a bad row alone', () => {
        assert.equal(parse_restorable({}), null);
        assert.deepEqual(parse_restorable({ sessions: [restorable(), { session: '' }] }), [restorable()]);
    });

    test('the notice is silent when every row came back', () => {
        assert.equal(restore_notice({ ok: true, restored: ['vc-1'], failed: [] }), null);
        assert.equal(
            restore_notice({ ok: true, restored: [], failed: [{ session: 'a', reason: 'x' }, { session: 'b', reason: 'y' }] }),
            'Restored 0; a not: x (+1 more)',
        );
    });
});
