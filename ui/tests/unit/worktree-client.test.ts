import test from 'node:test';
import assert from 'node:assert/strict';
import {
    ISLAND_FOCUS_TIMEOUT_MS,
    create_island_focus,
    create_notice,
    create_worktree,
    create_worktree_poller,
    fetch_worktree_state,
    has_active_create,
    parse_create_request,
} from '../../src/agent-rack/worktree-client.ts';
import { CREATE_STEPS, create_step_view, pane_arg } from '../../src/engine/terminal/rack-view.ts';
import type { WorktreeRow } from '../../src/engine/terminal/rack-view.ts';

function row(session: string, branch = 'b'): WorktreeRow {
    return {
        branch, path: `/wt/${branch}`, session,
        dirty: false, ahead: 0, behind: 0,
        pr_status: null, pr_url: null, pr_error: null,
    };
}

// ── pane_arg: THE worktree invariant ───────────────────────────────────────

test('pane_arg leaves a plain session in the CREATING form', () => {
    // `+ NEW` works purely by pointing the pane at an unused name — prefixing
    // every session with `attach:` would disable it entirely.
    assert.equal(pane_arg('vc-1', [row('wt-a')]), 'vc-1');
});

test('pane_arg addresses a worktree session as attach-only', () => {
    // Without this, a DEAD worktree session re-pointed at by the pane is
    // silently re-created by ttyd's `-a` in the bridge's global start dir —
    // i.e. an agent that believes it is isolated while editing `main`.
    assert.equal(pane_arg('wt-a', [row('wt-a')]), 'attach:wt-a');
});

test('pane_arg with no worktree list never prefixes', () => {
    // Absent means "no plugin answered", which must leave every existing
    // behaviour byte-identical.
    assert.equal(pane_arg('wt-a', undefined), 'wt-a');
    assert.equal(pane_arg('wt-a', []), 'wt-a');
});

// ── parse_create_request: the one-string transport hop ─────────────────────

test('parse_create_request reads a branch and an optional prompt', () => {
    assert.deepEqual(parse_create_request('{"branch":"fix/nav"}'), { branch: 'fix/nav' });
    assert.deepEqual(
        parse_create_request('{"branch":"fix","prompt":"do it"}'),
        { branch: 'fix', prompt: 'do it' },
    );
});

test('parse_create_request drops a blank prompt rather than sending an empty one', () => {
    assert.deepEqual(parse_create_request('{"branch":"fix","prompt":"   "}'), { branch: 'fix' });
});

test('parse_create_request refuses anything without a branch', () => {
    for (const value of ['', 'not json', '{}', '{"branch":"  "}', '[]', 'null', 42, null]) {
        assert.equal(parse_create_request(value), null, JSON.stringify(value));
    }
});

// ── fetch_worktree_state: absent plugin is the common case ─────────────────

test('fetch_worktree_state returns null when the plugin is not mounted', async () => {
    const not_found = async (): Promise<Response> =>
        new Response('{"error":"unknown"}', { status: 404 });
    assert.equal(await fetch_worktree_state((p) => p, not_found as never), null);
});

test('fetch_worktree_state returns null rather than throwing on a network failure', async () => {
    const boom = async (): Promise<Response> => { throw new Error('offline'); };
    assert.equal(await fetch_worktree_state((p) => p, boom as never), null);
});

test('fetch_worktree_state drops rows that are not shaped like worktrees', async () => {
    const body = JSON.stringify({
        worktrees: [row('wt-a'), { nope: true }, null, row('wt-b')],
        root: '/wt', base: 'main',
    });
    const ok = async (): Promise<Response> => new Response(body, { status: 200 });
    const state = await fetch_worktree_state((p) => p, ok as never);
    assert.equal(state?.worktrees.length, 2);
    assert.equal(state?.base, 'main');
});

// ── the poller ─────────────────────────────────────────────────────────────

test('the poller notifies only when the worktree list actually changes', async () => {
    let changes = 0;
    const body = JSON.stringify({ worktrees: [row('wt-a')], root: '/wt', base: 'main' });
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 1000,
        on_change: () => { changes += 1; },
        fetch_impl: (async () => new Response(body, { status: 200 })) as never,
        set_interval: (() => 0) as never,
        clear_interval: () => {},
    });

    await poller.tick();
    assert.equal(changes, 1, 'first data is a change');
    await poller.tick();
    // Re-rendering the whole rack every 5s because a fresh object arrived is
    // pure cost: nothing on screen changed.
    assert.equal(changes, 1, 'identical state must not re-emit');
    assert.deepEqual([...poller.sessions()], ['wt-a']);
});

test('the poller follows worktree_paths even when the managed rows are unchanged', async () => {
    // The managed rows are a SUBSET of the paths, and the whole point of the
    // key is the trees they say nothing about. A bridge restart onto the plugin
    // that ships it, or a new unmanaged tree appearing mid-session, changes only
    // this key — a signature over `worktrees` alone would sit on the first
    // document forever and the rack would never learn its attach-only set.
    let changes = 0;
    const same_rows = [row('wt-a')];
    const bodies = [
        { worktrees: same_rows, root: '/wt', base: 'main' },
        { worktrees: same_rows, root: '/wt', base: 'main', worktree_paths: ['~/worktrees/my-app/x'] },
        { worktrees: same_rows, root: '/wt', base: 'main',
            worktree_paths: ['~/worktrees/my-app/x', '~/worktrees/my-app/y'] },
    ];
    let call = 0;
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 1000,
        on_change: () => { changes += 1; },
        fetch_impl: (async () => new Response(JSON.stringify(bodies[Math.min(call++, bodies.length - 1)]),
            { status: 200 })) as never,
        set_interval: (() => 0) as never,
        clear_interval: () => {},
    });

    await poller.tick();
    assert.equal(changes, 1);
    assert.equal('worktree_paths' in (poller.state() ?? {}), false, 'old plugin: absent stays absent');
    await poller.tick();
    assert.equal(changes, 2, 'the key arriving is a change');
    assert.deepEqual(poller.state()?.worktree_paths, ['~/worktrees/my-app/x']);
    await poller.tick();
    assert.equal(changes, 3, 'a new tree is a change');
    assert.deepEqual(poller.state()?.worktree_paths,
        ['~/worktrees/my-app/x', '~/worktrees/my-app/y']);
    await poller.tick();
    assert.equal(changes, 3, 'the same document must still not re-emit');
    assert.deepEqual([...poller.sessions()], ['wt-a'], 'rows untouched throughout');
});

test('the poller reports an empty session set before any data', () => {
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 1000,
        on_change: () => {},
        fetch_impl: (async () => new Response('{}', { status: 200 })) as never,
        set_interval: (() => 0) as never,
        clear_interval: () => {},
    });
    assert.equal(poller.state(), null);
    assert.equal(poller.sessions().size, 0);
});

test('the timer rests while its owner does not want it, and a tick asked for by name still fetches', async () => {
    let fetches = 0;
    let wanted = true;
    let timer: () => void = () => {};
    const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 5000,
        on_change: () => {},
        wanted: () => wanted,
        fetch_impl: (async () => {
            fetches += 1;
            return new Response(JSON.stringify({ worktrees: [row('wt-a')], root: '/wt', base: 'main' }), { status: 200 });
        }) as never,
        set_interval: ((fn: () => void) => { timer = fn; return 0; }) as never,
        clear_interval: () => {},
    });
    poller.start();
    await flush();
    assert.equal(fetches, 1, 'the first read is not the timer: a page opened in a background tab still gets its rows');
    timer();
    await flush();
    assert.equal(fetches, 2);
    // Nobody is looking: a `git` readout of every tree would only redraw rows nobody can see.
    wanted = false;
    timer();
    timer();
    await flush();
    assert.equal(fetches, 2);
    // After an action, or the moment the page is looked at again.
    await poller.tick();
    assert.equal(fetches, 3);
    wanted = true;
    timer();
    await flush();
    assert.equal(fetches, 4);
});

// ── create progress ────────────────────────────────────────────────────────
//
// Everything the user sees during a create comes from these two: the step view
// that draws the row, and the adaptive cadence that makes the label move.

function creating(step: string | null, branch = 'b'): WorktreeRow {
    return { ...row(`wt-${branch}`, branch), create_status: 'creating', create_step: step as never };
}

test('create_step_view is null for an ordinary row', () => {
    assert.equal(create_step_view(row('wt-b')), null);
});

test('create_step_view is null for a row from an OLDER plugin', () => {
    // The compatibility case: no create fields at all must read as "not
    // creating", never as a half-drawn creating row.
    const old_row = row('wt-b');
    assert.equal('create_status' in old_row, false);
    assert.equal(create_step_view(old_row), null);
});

test('create_step_view is null once the create failed', () => {
    assert.equal(create_step_view({ ...row('wt-b'), create_status: 'error',
        create_error: 'boom' }), null);
});

test('create_step_view walks the four steps 1-based', () => {
    const seen = CREATE_STEPS.map((step) => create_step_view(creating(step)));
    assert.deepEqual(seen.map((v) => v?.step_index), [1, 2, 3, 4]);
    assert.deepEqual(seen.map((v) => v?.label),
        ['WORKTREE', 'BOOTSTRAP', 'SESSION', 'PROMPT']);
    assert.ok(seen.every((v) => v?.step_total === 4));
});

test('create_step_view degrades an UNKNOWN step to step 1, not to null', () => {
    // A future plugin that inserts a step must still show as creating.
    const view = create_step_view(creating('verifying'));
    assert.equal(view?.step_index, 1);
    assert.equal(view?.label, 'VERIFYING');
});

test('has_active_create only fires on a row that is actually creating', () => {
    assert.equal(has_active_create(null), false);
    assert.equal(has_active_create({ worktrees: [], root: '', base: 'main' }), false);
    assert.equal(has_active_create({ worktrees: [row('wt-b')], root: '', base: 'main' }), false);
    assert.equal(has_active_create({ worktrees: [creating('bootstrap')], root: '', base: 'main' }),
        true);
});

// ── create_island_focus: a 🏝 launch lands on its session ───────────────────

function state_of(...worktrees: WorktreeRow[]) {
    return { worktrees, root: '', base: 'main' };
}

test('island focus waits through the steps before the session exists, even with an optimistic live row', () => {
    const focus = create_island_focus();
    focus.arm('wt-b', 0);
    // The optimistic creating row is live too — it must not count.
    assert.equal(focus.take(state_of(creating('worktree')), ['wt-b'], 1), null);
    assert.equal(focus.take(state_of(creating('bootstrap')), ['wt-b'], 2), null);
    assert.equal(focus.take(state_of(creating('session')), ['wt-b'], 3), null);
    // Finished, but the snapshot has not caught up yet.
    assert.equal(focus.take(state_of(row('wt-b')), [], 4), null);
    assert.equal(focus.take(state_of(row('wt-b')), ['wt-b'], 5), 'wt-b');
    // Once only.
    assert.equal(focus.take(state_of(row('wt-b')), ['wt-b'], 6), null);
});

test('island focus lands while a first task is being typed', () => {
    const focus = create_island_focus();
    focus.arm('wt-b', 0);
    assert.equal(focus.take(state_of(creating('prompt')), ['wt-b'], 1), 'wt-b');
});

test('island focus gives up on a failed create, a cancel or the timeout', () => {
    const failed = create_island_focus();
    failed.arm('wt-b', 0);
    const error: WorktreeRow = { ...row('wt-b'), create_status: 'error', create_error: 'boom' };
    assert.equal(failed.take(state_of(error), ['wt-b'], 1), null);
    assert.equal(failed.take(state_of(row('wt-b')), ['wt-b'], 2), null);

    const cancelled = create_island_focus();
    cancelled.arm('wt-b', 0);
    cancelled.cancel();
    assert.equal(cancelled.take(state_of(row('wt-b')), ['wt-b'], 1), null);

    const late = create_island_focus();
    late.arm('wt-b', 0);
    assert.equal(late.take(state_of(row('wt-b')), ['wt-b'], ISLAND_FOCUS_TIMEOUT_MS + 1), null);
});

test('island focus says while a launch is still waiting, which is what keeps its poll awake', () => {
    // A hidden page rests the worktree timer; a launch that has not landed is the reason not to.
    const focus = create_island_focus();
    assert.equal(focus.armed(), false);
    focus.arm('wt-b', 0);
    assert.equal(focus.armed(), true);
    assert.equal(focus.take(state_of(creating('bootstrap')), ['wt-b'], 1), null);
    assert.equal(focus.armed(), true, 'still building');
    assert.equal(focus.take(state_of(row('wt-b')), ['wt-b'], 2), 'wt-b');
    assert.equal(focus.armed(), false, 'landed');
    focus.arm('wt-c', 0);
    focus.cancel();
    assert.equal(focus.armed(), false, 'the operator picked another row');
    focus.arm('wt-d', 0);
    focus.take(state_of(row('wt-d')), [], ISLAND_FOCUS_TIMEOUT_MS + 1);
    assert.equal(focus.armed(), false, 'given up on');
});

test('the poller speeds up while a create is live and slows back down after', async () => {
    const intervals: number[] = [];
    let worktrees: WorktreeRow[] = [];
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 5000,
        on_change: () => {},
        fetch_impl: async () =>
            new Response(JSON.stringify({ worktrees, root: '/r', base: 'main' })),
        set_interval: (_fn, ms) => { intervals.push(ms); return intervals.length as never; },
        clear_interval: () => {},
    });

    poller.start();
    await poller.tick();
    assert.deepEqual(intervals, [5000], 'idle starts at the caller cadence');

    worktrees = [creating('bootstrap')];
    await poller.tick();
    assert.deepEqual(intervals, [5000, 1000], 'a live create speeds it up');

    worktrees = [row('wt-b')];
    await poller.tick();
    assert.deepEqual(intervals, [5000, 1000, 5000], 'and it slows back down');
});

test('the poller re-arms only when the CADENCE changes, not on every tick', async () => {
    // A five-minute bootstrap must not churn a timer pair per second.
    const intervals: number[] = [];
    let n = 0;
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 5000,
        on_change: () => {},
        // A different list every tick, so `on_change` fires each time and only
        // the cadence guard can keep the timer still.
        fetch_impl: async () => new Response(JSON.stringify({
            worktrees: [creating('bootstrap', `b${n++}`), creating('session')],
            root: '/r', base: 'main',
        })),
        set_interval: (_fn, ms) => { intervals.push(ms); return intervals.length as never; },
        clear_interval: () => {},
    });

    poller.start();
    for (let i = 0; i < 5; i++) await poller.tick();
    assert.deepEqual(intervals, [5000, 1000],
        'the idle arm start() lays down before any answer, then ONE fast arm — '
        + 'five ticks, two timers');
});

// ── the harbor routes ──────────────────────────────────────────────────────

import {
    FETCH_STEPS,
    delete_branch,
    fetch_branches,
    has_active_harbor_job,
    prune_worktrees,
    remove_landed_worktree,
    remove_worktree,
    start_brief,
    start_fetch,
} from '../../src/agent-rack/worktree-client.ts';
import type { BranchRow, HarborState } from '../../src/agent-rack/worktree-client.ts';

function branch_row(name: string, extra: Partial<BranchRow> = {}): BranchRow {
    return {
        branch: name, sha: 'abc1234', session: `wt-${name}`, is_base: false,
        committed_at: 1_788_000_000, author: 'u', subject: 's',
        ahead: 0, behind: 3, merged: true, upstream: null, worktree: null,
        pr: null, pr_job: null, delete_verdict: 'ok', delete_reason: null, ...extra,
    };
}

const HARBOR_DOC = {
    status: 'ready', generated_at: 1_788_000_100, age_seconds: 1.5, error: null,
    git_ok: true, git_error: null, base: 'main', pr_remote: 'origin',
    capabilities: { ahead_behind: true }, repo: '~/code/my-app', stash_count: 1,
    fetch: { status: 'idle', step: null, error: null, at: 1_788_000_000 },
    pr_index: { fetched_at: null, count: 0, error: null },
    branches: [branch_row('main', { is_base: true }), branch_row('x'), { nope: true }, null],
    worktrees: [{ path: '~/code/my-app', branch: 'main', head: 'abc', detached: false,
        managed: false, is_main: true, dirty: false, prunable: false, prunable_reason: null, locked: false }, 42],
};

const respond = (body: unknown, status = 200) =>
    (async (): Promise<Response> => new Response(JSON.stringify(body), { status })) as never;

test('FETCH_STEPS is pinned to the plugin’s tuple', () => {
    // The twin of harbor.py's pin — two languages either side of HTTP.
    assert.deepEqual([...FETCH_STEPS], ['fetch', 'prs']);
});

test('fetch_branches parses a document and drops malformed rows', async () => {
    const urls: string[] = [];
    const impl = (async (url: string): Promise<Response> => {
        urls.push(url);
        return new Response(JSON.stringify(HARBOR_DOC), { status: 200 });
    }) as never;
    const state = await fetch_branches((p) => p, impl);
    assert.deepEqual(urls, ['/plugin/worktree/branches']);
    assert.equal(state?.status, 'ready');
    assert.equal(state?.branches.length, 2);
    assert.equal(state?.worktrees.length, 1);
    assert.equal(state?.fetch.at, 1_788_000_000);
    assert.equal(state?.pr_index.fetched_at, null);
    assert.equal(state?.capabilities.ahead_behind, true);
});

test('fetch_branches is null on 404, on a timeout, and on a body with no branches', async () => {
    assert.equal(await fetch_branches((p) => p, respond({ error: 'unknown route' }, 404)), null);
    const timeout = (async (): Promise<Response> => {
        throw new DOMException('signal timed out', 'TimeoutError');
    }) as never;
    assert.equal(await fetch_branches((p) => p, timeout), null);
    assert.equal(await fetch_branches((p) => p, respond({ worktrees: [] })), null);
    assert.equal(await fetch_branches((p) => p, respond('nope')), null);
});

test('fetch_branches fills a partial document with idle defaults', async () => {
    // A cold cache answers little more than `warming`; nothing may be undefined.
    const state = await fetch_branches((p) => p, respond({ status: 'warming', branches: [] }));
    assert.equal(state?.status, 'warming');
    assert.equal(state?.git_ok, null);
    assert.equal(state?.base, 'main');
    assert.equal(state?.pr_remote, 'origin');
    assert.deepEqual(state?.fetch, { status: 'idle', step: null, error: null, at: null });
    assert.deepEqual(state?.pr_index, { fetched_at: null, count: 0, error: null, stale: false, refreshing: false, truncated: false });
    assert.deepEqual(state?.worktrees, []);
    assert.equal(state?.stash_count, 0);
});

test('delete_branch surfaces needs_force and the unlanded count from a 409', async () => {
    const result = await delete_branch((p) => p, 'x', false,
        respond({ error: '3 commits are not on main', needs_force: true, ahead: 3 }, 409));
    assert.deepEqual(result, {
        ok: false, status: 409, error: '3 commits are not on main', needs_force: true, ahead: 3,
    });
});

test('delete_branch passes a plain refusal and a success through unchanged', async () => {
    const held = await delete_branch((p) => p, 'x', false,
        respond({ error: 'checked out at ~/worktrees/my-app/x — remove the worktree first',
            worktree_removable: true }, 409));
    assert.equal(held.ok, false);
    assert.equal(held.needs_force, undefined);
    assert.equal(held.worktree_removable, true);
    const done = await delete_branch((p) => p, 'x', true,
        respond({ branch: 'x', deleted: true, forced: true }));
    assert.deepEqual(done, { ok: true, status: 200, session: undefined });
});

test('remove_worktree surfaces needs_force from a 409, like its sibling', async () => {
    // REGRESSION, and not a cosmetic one: the panel re-arms the button red on
    // this flag and `harbor_action` suppresses its toast on it. Without it the
    // operator gets git's raw `fatal: … use --force to delete it` in a
    // notification and NO button that would ever pass `--force` — the forced
    // remove is unreachable from the UI entirely.
    const result = await remove_worktree((p) => p, 'fix-nav', false,
        respond({ error: "fatal: '~/worktrees/my-app/fix-nav' contains modified or "
            + 'untracked files, use --force to delete it', needs_force: true }, 409));
    assert.equal(result.ok, false);
    assert.equal(result.status, 409);
    assert.equal(result.needs_force, true);
});

test('remove_worktree leaves needs_force off a refusal that is not about files', async () => {
    // `kill its session first` is a 409 the plugin answers WITHOUT the flag —
    // forcing would not help, so the button must not arm red for it.
    const held = await remove_worktree((p) => p, 'x', false,
        respond({ error: 'no managed worktree for that branch' }, 404));
    assert.equal(held.ok, false);
    assert.equal(held.needs_force, undefined);
    // A forced remove that lands carries no flag either.
    const done = await remove_worktree((p) => p, 'x', true, respond({ branch: 'x', removed: true }));
    assert.equal(done.ok, true);
    assert.equal(done.needs_force, undefined);
});

test('delete_branch sends the branch and the force flag as JSON', async () => {
    let sent: unknown = null;
    const impl = (async (_url: string, init: RequestInit): Promise<Response> => {
        sent = JSON.parse(String(init.body));
        return new Response('{}', { status: 200 });
    }) as never;
    await delete_branch((p) => p, 'fix/nav', true, impl);
    assert.deepEqual(sent, { branch: 'fix/nav', force: true });
});

test('start_fetch treats 202 as success; prune_worktrees reads the pruned names', async () => {
    const started = await start_fetch((p) => p, respond({ status: 'running', step: 'fetch', started: true }, 202));
    assert.equal(started.ok, true);
    assert.equal(started.status, 202);
    const no_git = await start_fetch((p) => p, respond({ error: 'git not found' }, 503));
    assert.deepEqual(no_git, { ok: false, status: 503, error: 'git not found' });
    const pruned = await prune_worktrees((p) => p, respond({ pruned: ['old', 7, 'stale'], count: 2 }));
    assert.deepEqual(pruned.pruned, ['old', 'stale']);
});

test('a transport failure on a harbor route is a fallback message, never a throw', async () => {
    const boom = (async (): Promise<Response> => { throw new Error('offline'); }) as never;
    assert.deepEqual(await delete_branch((p) => p, 'x', false, boom),
        { ok: false, error: 'could not delete the branch' });
    assert.deepEqual(await start_fetch((p) => p, boom), { ok: false, error: 'could not start the fetch' });
});

test('has_active_harbor_job: warming without an error, a running fetch, or a create', () => {
    const base: HarborState = {
        status: 'ready', generated_at: 1, age_seconds: 0, error: null, git_ok: true, git_error: null,
        base: 'main', pr_remote: 'origin', capabilities: { ahead_behind: true }, repo: '~/r',
        stash_count: 0, fetch: { status: 'idle', step: null, error: null, at: null },
        pr_index: { fetched_at: null, count: 0, error: null, stale: false, refreshing: false, truncated: false },
        branches: [], worktrees: [], archived: [],
        remote_branches: [], remote_error: null,
        brief: { status: 'idle', step: null, done: 0, total: 0, cost_usd: 0, note: null, error: null, at: null, count: 0, model: 'haiku' },
    };
    assert.equal(has_active_harbor_job(null), false);
    assert.equal(has_active_harbor_job(base), false);
    assert.equal(has_active_harbor_job({ ...base, status: 'warming' }), true);
    // A warming document that carries an error is a runner that backed off —
    // fast-polling it forever is the wedge the idle cadence exists to avoid.
    assert.equal(has_active_harbor_job({ ...base, status: 'warming', error: 'not a repo' }), false);
    assert.equal(has_active_harbor_job({ ...base, fetch: { ...base.fetch, status: 'running', step: 'prs' } }), true);
    assert.equal(has_active_harbor_job({ ...base, fetch: { ...base.fetch, status: 'error' } }), false);
    assert.equal(has_active_harbor_job({ ...base, branches: [branch_row('x', { create_status: 'creating', create_step: 'bootstrap' })] }), true);
    assert.equal(has_active_harbor_job({ ...base, branches: [branch_row('x', { create_status: 'error' })] }), false);
});

// ── `existing` rides the one-string transport hop ──────────────────────────

test('parse_create_request passes existing:true through and nothing else', () => {
    assert.deepEqual(parse_create_request('{"branch":"fix","existing":true}'), { branch: 'fix', existing: true });
    assert.deepEqual(parse_create_request('{"branch":"fix","prompt":"go","existing":true}'),
        { branch: 'fix', prompt: 'go', existing: true });
    assert.deepEqual(parse_create_request('{"branch":"fix","existing":false}'), { branch: 'fix' });
    assert.deepEqual(parse_create_request('{"branch":"fix","existing":"true"}'), { branch: 'fix' },
        'a truthy string must not become a checkout of some existing branch');
});

test('fetch_worktree_state copies worktree_paths only when the plugin sent them', async () => {
    const old_plugin = await fetch_worktree_state((p) => p,
        respond({ worktrees: [row('wt-a')], root: '/wt', base: 'main' }));
    assert.equal('worktree_paths' in (old_plugin ?? {}), false, 'absent stays absent');
    const current = await fetch_worktree_state((p) => p,
        respond({ worktrees: [row('wt-a')], root: '/wt', base: 'main',
            worktree_paths: ['~/worktrees/my-app/x', 7, '~/worktrees/my-app/a'] }));
    assert.deepEqual(current?.worktree_paths, ['~/worktrees/my-app/x', '~/worktrees/my-app/a']);
});

// ── briefs ──────────────────────────────────────────────────────────────────

test('fetch_branches parses the brief job field by field and the row briefs by shape', async () => {
    const doc = {
        ...HARBOR_DOC,
        brief: { status: 'running', step: 'x', done: 1, total: 3, cost_usd: 0.004, note: null, error: null, at: null, count: 7, model: 'haiku' },
        branches: [
            branch_row('main', { is_base: true }),
            { ...branch_row('x'), brief: { what: ' Adds the widget. ', next: ['rebase', 3, ''], model: 'claude-haiku-4-5', at: 5, stale: true } },
            { ...branch_row('y'), brief: { what: '', next: [] } },
            { ...branch_row('z'), brief: 'nope' },
        ],
    };
    const state = (await fetch_branches((p) => p, respond(doc)))!;
    assert.deepEqual(state.brief, { status: 'running', step: 'x', done: 1, total: 3, cost_usd: 0.004,
        note: null, error: null, at: null, count: 7, model: 'haiku' });
    const by = new Map(state.branches.map((row) => [row.branch, row.brief]));
    assert.deepEqual(by.get('x'), { what: 'Adds the widget.', next: ['rebase'], model: 'claude-haiku-4-5', at: 5, stale: true });
    assert.equal(by.get('y'), null, 'an empty sentence is no brief');
    assert.equal(by.get('z'), null);
    assert.equal(by.get('main'), null);
    // An older plugin sends no brief block: idle, never undefined.
    const old = (await fetch_branches((p) => p, respond(HARBOR_DOC)))!;
    assert.equal(old.brief.status, 'idle');
    assert.equal(has_active_harbor_job(state), true, 'a running brief is an active job');
});

test('start_brief posts the branch when given and nothing when briefing all', async () => {
    const seen: { path: string; body: unknown }[] = [];
    const impl = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        seen.push({ path: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify({ status: 'running', started: true, done: 0, total: 1 }), { status: 202 });
    }) as never;
    assert.equal((await start_brief((p) => p, 'feat/x', impl)).ok, true);
    assert.equal((await start_brief((p) => p, null, impl)).ok, true);
    assert.deepEqual(seen.map((s) => s.body), [{ branch: 'feat/x' }, {}]);
    assert.ok(seen.every((s) => s.path.endsWith('/plugin/worktree/brief')));
    const warming = await start_brief((p) => p, null, respond({ error: 'the harbor is still warming' }, 409));
    assert.deepEqual(warming, { ok: false, status: 409, error: 'the harbor is still warming' });
});

test('parse_create_request carries a valid profile and drops a malformed one', () => {
    assert.deepEqual(parse_create_request(JSON.stringify({ branch: 'b', profile: 'claude-a' })), {
        branch: 'b',
        profile: 'claude-a',
    });
    // Dropped → the bridge default, rather than forwarded for the plugin to
    // refuse the whole create.
    assert.deepEqual(parse_create_request(JSON.stringify({ branch: 'b', profile: '../etc' })), { branch: 'b' });
    assert.deepEqual(parse_create_request(JSON.stringify({ branch: 'b', profile: 7 })), { branch: 'b' });
});

test('create_worktree surfaces prompt_queued:false as a prompt note', async () => {
    const answer = (body: unknown) =>
        (async () => ({ ok: true, status: 202, json: async () => body }) as unknown as Response) as typeof fetch;
    const base = (path: string): string => `http://127.0.0.1:7682${path}`;
    const skipped = await create_worktree(
        base,
        { branch: 'b', prompt: 'do it', profile: 'codex-a' },
        answer({ session: 'wt-b', prompt_queued: false, prompt_reason: 'codex: type the first task yourself' }),
    );
    assert.equal(skipped.prompt_note, 'codex: type the first task yourself');
    const queued = await create_worktree(base, { branch: 'b' }, answer({ session: 'wt-b' }));
    assert.equal(queued.prompt_note, undefined);
});

test('create_notice: a refusal is shown, then a skipped task, else nothing', async () => {
    const base = (path: string): string => `http://127.0.0.1:7682${path}`;
    const refuse = (async () => ({ ok: false, status: 409, json: async () => ({ error: "branch 'fix-nav' already exists" }) }) as unknown as Response) as typeof fetch;
    const refused = await create_worktree(base, { branch: 'fix-nav' }, refuse);
    assert.equal(create_notice(refused), "worktree not created — branch 'fix-nav' already exists");
    assert.equal(create_notice({ ok: false }), 'worktree not created — could not create the worktree');
    assert.equal(create_notice({ ok: true, status: 202, prompt_note: 'paste it' }), 'paste it');
    assert.equal(create_notice({ ok: true, status: 202, session: 'wt-b' }), null);
});

// ── the triage fields ──────────────────────────────────────────────────────

const TREE = {
    path: '/private/tmp/scratch-7', managed: false, is_main: false,
    dirty: false, prunable: false, locked: false,
};

test('fetch_branches parses the triage fields off a row', async () => {
    const doc = {
        ...HARBOR_DOC,
        pr_index: { fetched_at: 1_787_000_000, count: 33, error: null, stale: true, refreshing: true, truncated: true },
        branches: [
            branch_row('main', { is_base: true }),
            {
                ...branch_row('squashed', { ahead: 5, merged: false, landed: true }),
                landing: { how: 'squash', pr: 104 }, content: 'skipped',
                worktree: { ...TREE, occupied_by: ['zsh', 7, '', 'node', 'vim', 'git'] },
                worktree_removable_outside: true,
            },
            {
                ...branch_row('agent-x', { ahead: 1, merged: false }),
                contained_in: 'feat/billing', content: 'measured',
            },
            {
                ...branch_row('feat/widget', { ahead: 3, merged: false }),
                content: 'measured',
                ledger: {
                    found: 2, missing: 1,
                    commits: [
                        { sha: 'cccc333', subject: 'Add the widget', found: false, hint: { pr: 105, why: 'title' } },
                        { sha: 'aaaa111', subject: 'wip', found: true, hint: { pr: 106, why: 'pr_patch' } },
                        { sha: 'bbbb222', subject: 'no hint', found: true, hint: { pr: 'x', why: 'title' } },
                        { nope: true },
                    ],
                },
            },
            { ...branch_row('pending', { ahead: 2, merged: false }), content: 'pending' },
        ],
    };
    const state = (await fetch_branches((p) => p, respond(doc)))!;
    assert.deepEqual(state.pr_index, { fetched_at: 1_787_000_000, count: 33, error: null, stale: true, refreshing: true, truncated: true });
    const by = new Map(state.branches.map((row) => [row.branch, row]));

    const squashed = by.get('squashed')!;
    assert.deepEqual(squashed.landing, { how: 'squash', pr: 104 });
    assert.equal(squashed.content, 'skipped');
    assert.equal(squashed.worktree_removable_outside, true);
    assert.deepEqual(squashed.worktree?.occupied_by, ['zsh', 'node', 'vim'], 'strings only, capped at three');

    assert.equal(by.get('agent-x')!.contained_in, 'feat/billing');
    assert.equal(by.get('agent-x')!.landing, null, 'not landed, so no landing');

    const ledger = by.get('feat/widget')!.ledger!;
    assert.equal(ledger.found, 2);
    assert.equal(ledger.missing, 1);
    assert.deepEqual(ledger.commits.map((c) => [c.sha, c.found, c.hint]), [
        ['cccc333', false, { pr: 105, why: 'title' }],
        ['aaaa111', true, { pr: 106, why: 'pr_patch' }],
        ['bbbb222', true, null],
    ], 'a malformed hint is no hint; a malformed commit is dropped');

    assert.equal(by.get('pending')!.content, 'pending');
});

test('an older plugin reads as the refusing answer on every triage field', async () => {
    // HARBOR_DOC is a plugin that predates the triage: none of the keys exist.
    const state = (await fetch_branches((p) => p, respond({
        ...HARBOR_DOC,
        branches: [{ ...branch_row('x'), worktree: TREE }, branch_row('y', { ahead: 2, merged: false })],
    })))!;
    for (const row of state.branches) {
        assert.equal(row.landing, null, row.branch);
        assert.equal(row.contained_in, null, row.branch);
        assert.equal(row.ledger, null, row.branch);
        // Measured-with-no-ledger: nothing is "pending" on a plugin with no patch-id tier.
        assert.equal(row.content, 'measured', row.branch);
        assert.equal('worktree_removable_outside' in row, false, `${row.branch}: absent stays absent`);
    }
    assert.equal(state.branches[0].worktree?.occupied_by, null, 'not probed');
    assert.equal(state.pr_index.stale, false);
    assert.equal(state.pr_index.refreshing, false);
    assert.equal(state.pr_index.truncated, false, 'an older plugin cannot say it was cut');
});

test('the triage parser re-applies the contract instead of trusting it', async () => {
    const state = (await fetch_branches((p) => p, respond({
        ...HARBOR_DOC,
        branches: [
            // landed AND contained AND a ledger: the landing proof alone stands.
            {
                ...branch_row('both', { ahead: 1, merged: false, landed: true }),
                landing: { how: 'pr', pr: 12 }, contained_in: 'other',
                ledger: { found: 0, missing: 1, commits: [] },
            },
            // A landing on a row that is not landed is no landing; an unknown `how` is none either.
            { ...branch_row('liar', { ahead: 1, merged: false }), landing: { how: 'ancestor', pr: null } },
            { ...branch_row('odd', { landed: true }), landing: { how: 'osmosis', pr: 3 }, content: 'maybe' },
            // `worktree_removable_outside` rides through only as a literal true.
            { ...branch_row('truthy', { worktree: TREE }), worktree_removable_outside: 'yes' },
            { ...branch_row('bad-ledger', { ahead: 1, merged: false }), ledger: { found: -1, missing: 'x' } },
        ],
    })))!;
    const by = new Map(state.branches.map((row) => [row.branch, row]));
    assert.deepEqual(by.get('both')!.landing, { how: 'pr', pr: 12 });
    assert.equal(by.get('both')!.contained_in, null);
    assert.equal(by.get('both')!.ledger, null);
    assert.equal(by.get('liar')!.landing, null);
    assert.equal(by.get('odd')!.landing, null);
    assert.equal(by.get('odd')!.content, 'measured', 'an unknown tier is the default, never passed through');
    assert.equal('worktree_removable_outside' in by.get('truthy')!, false);
    assert.equal(by.get('bad-ledger')!.ledger, null);
});

test('remove_landed_worktree posts the branch alone, and a refusal is an answer', async () => {
    const seen: { path: string; body: unknown }[] = [];
    const impl = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        seen.push({ path: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify({ branch: 'x', removed: true }), { status: 200 });
    }) as never;
    const done = await remove_landed_worktree((p) => p, 'x', impl);
    assert.deepEqual(done, { ok: true, status: 200, session: undefined });
    assert.deepEqual(seen, [{ path: '/plugin/worktree/remove-landed-worktree', body: { branch: 'x' } }],
        'no force parameter exists to send');

    const held = await remove_landed_worktree((p) => p, 'x',
        respond({ error: 'zsh is open inside /private/tmp/scratch-7' }, 409));
    assert.deepEqual(held, { ok: false, status: 409, error: 'zsh is open inside /private/tmp/scratch-7' });
    const boom = (async (): Promise<Response> => { throw new Error('offline'); }) as never;
    assert.deepEqual(await remove_landed_worktree((p) => p, 'x', boom),
        { ok: false, error: 'could not remove the landed worktree' });
});


test('fetch_branches reads the evidence tier and the archive, and refuses a contradiction', async () => {
    const tip = 'c'.repeat(40);
    const state = (await fetch_branches((p) => p, respond({
        ...HARBOR_DOC,
        archived: [{ branch: 'feat/x', oid: tip, sha: 'ccccccccc', committed_at: 5, subject: 's' },
            { branch: 'bad', oid: 'nothex' }],
        branches: [
            { ...branch_row('lane', { ahead: 3, merged: false }), content: 'measured',
                absorption: { status: 'measured', lines: 100, present: 97, missing_files: [], missing_file_count: 0, absorbed: true } },
            { ...branch_row('liar', { ahead: 1, merged: false }), content: 'measured',
                absorption: { status: 'measured', lines: 10, present: 9, missing_files: ['a.ts'], missing_file_count: 1, absorbed: true } },
            { ...branch_row('waiting', { ahead: 1, merged: false }), content: 'measured', absorption: { status: 'pending' } },
            { ...branch_row('landed', { ahead: 0, merged: true }),
                absorption: { status: 'measured', lines: 1, present: 1, missing_files: [], missing_file_count: 0, absorbed: true } },
        ],
    })))!;
    assert.deepEqual(state.archived, [{ branch: 'feat/x', oid: tip, sha: 'ccccccccc', committed_at: 5, subject: 's' }]);
    const by = new Map(state.branches.map((row) => [row.branch, row]));
    assert.equal(by.get('lane')!.absorption?.status, 'measured');
    assert.equal((by.get('liar')!.absorption as { absorbed: boolean }).absorbed, false,
        'a missing file and absorbed:true cannot both be true — refuse');
    assert.deepEqual(by.get('waiting')!.absorption, { status: 'pending' });
    assert.equal(by.get('landed')!.absorption, null, 'a proven row carries no evidence');
});

test('an older plugin has no archive and no evidence', async () => {
    const state = (await fetch_branches((p) => p, respond({ ...HARBOR_DOC, branches: [branch_row('x')] })))!;
    assert.deepEqual(state.archived, []);
    assert.equal(state.branches[0].absorption ?? null, null);
});

test('fetch_worktree_state parses worktree_trees, and builds branchless ones from an older plugin', async () => {
    const current = await fetch_worktree_state((p) => p,
        respond({ worktrees: [], root: '/wt', base: 'main',
            worktree_paths: ['~/worktrees/my-app/a', '~/worktrees/my-app/x'],
            worktree_trees: [{ path: '~/worktrees/my-app/a', branch: 'a' },
                { path: '~/worktrees/my-app/x', branch: null },
                { path: '~/bad', branch: 7 }, { branch: 'no-path' }] }));
    assert.deepEqual(current?.worktree_trees, [{ path: '~/worktrees/my-app/a', branch: 'a' },
        { path: '~/worktrees/my-app/x', branch: null }]);
    const older = await fetch_worktree_state((p) => p,
        respond({ worktrees: [], root: '/wt', base: 'main', worktree_paths: ['~/worktrees/my-app/a'] }));
    assert.deepEqual(older?.worktree_trees, [{ path: '~/worktrees/my-app/a' }], 'branch absent = not told');
    const oldest = await fetch_worktree_state((p) => p, respond({ worktrees: [], root: '/wt', base: 'main' }));
    assert.equal('worktree_trees' in (oldest ?? {}), false, 'absent stays absent');
});

test('fetch_worktree_state reads each tree\'s PR, and a null one as none only once the index is known', async () => {
    const trees = [{ path: '~/w/a', branch: 'a', pr: { number: 101, state: 'open', url: 'https://github.com/o/r/pull/101' } },
        { path: '~/w/b', branch: 'b', pr: null },
        { path: '~/w/c', branch: 'c', pr: { state: 'draftish' } }];
    const known = await fetch_worktree_state((p) => p,
        respond({ worktrees: [], root: '/wt', base: 'main', worktree_paths: ['~/w/a', '~/w/b', '~/w/c'], worktree_trees: trees, pr_known: true }));
    assert.deepEqual(known?.worktree_trees, [
        { path: '~/w/a', branch: 'a', pr: { state: 'open', number: 101, url: 'https://github.com/o/r/pull/101' } },
        { path: '~/w/b', branch: 'b', pr: { state: 'none', number: null, url: null } },
        { path: '~/w/c', branch: 'c', pr: { state: 'none', number: null, url: null } },
    ]);
    const unknown = await fetch_worktree_state((p) => p,
        respond({ worktrees: [], root: '/wt', base: 'main', worktree_paths: ['~/w/b'], worktree_trees: [trees[1]], pr_known: false }));
    assert.deepEqual(unknown?.worktree_trees, [{ path: '~/w/b', branch: 'b' }], 'never fetched: unknown, never "no PR"');
});

test('fetch_worktree_state keeps a tree\'s landing when well-formed, and drops anything else', async () => {
    const trees = [{ path: '~/w/a', branch: 'a', pr: null, landing: { how: 'ancestor', pr: 41 } },
        { path: '~/w/b', branch: 'b', pr: null, landing: null },
        { path: '~/w/c', branch: 'c', pr: null, landing: { how: 'teleport', pr: 1 } },
        { path: '~/w/d', branch: 'd', pr: null, landing: { how: 'squash', pr: 'x' } }];
    const state = await fetch_worktree_state((p) => p,
        respond({ worktrees: [], root: '/wt', base: 'main', worktree_paths: [], worktree_trees: trees, pr_known: true }));
    const none = { state: 'none', number: null, url: null };
    assert.deepEqual(state?.worktree_trees, [
        { path: '~/w/a', branch: 'a', pr: none, landing: { how: 'ancestor', pr: 41 } },
        { path: '~/w/b', branch: 'b', pr: none },
        { path: '~/w/c', branch: 'c', pr: none },
        { path: '~/w/d', branch: 'd', pr: none, landing: { how: 'squash', pr: null } },
    ]);
});

// ── CLEAN: `/state`'s landing fields and `POST /cleanup` ───────────────────

import {
    clean_up_worktree,
    cleanup_notice,
    cleanup_request,
    has_active_cleanup,
} from '../../src/agent-rack/worktree-client.ts';

const CLEAN_FIELDS = { landed: true, landing: { how: 'pr', pr: 102 }, cleanup_status: null, cleanup_error: null };

test('fetch_worktree_state reads the CLEAN fields off a plugin that declares capabilities.cleanup', async () => {
    const state = await fetch_worktree_state((p) => p, respond({
        worktrees: [{ ...row('wt-a', 'a'), ...CLEAN_FIELDS },
            { ...row('wt-b', 'b'), landed: null, cleanup_status: 'running' },
            { ...row('wt-c', 'c'), landed: false, cleanup_status: 'error', cleanup_error: '  not landed in main  ' }],
        root: '/wt', base: 'main', capabilities: { cleanup: true },
    }));
    assert.deepEqual(state?.worktrees.map(({ landed, landing, cleanup_status, cleanup_error }) =>
        ({ landed, landing, cleanup_status, cleanup_error })), [
        { landed: true, landing: { how: 'pr', pr: 102 }, cleanup_status: null, cleanup_error: null },
        { landed: null, landing: undefined, cleanup_status: 'running', cleanup_error: undefined },
        { landed: false, landing: undefined, cleanup_status: 'error', cleanup_error: 'not landed in main' },
    ]);
    assert.equal('landing' in state!.worktrees[1], false, 'absent stays absent — unknown, never false');
});

test('without capabilities.cleanup the CLEAN fields are dropped, so the button stays PR', async () => {
    for (const capabilities of [undefined, {}, { cleanup: 'yes' }, { cleanup: false }]) {
        const state = await fetch_worktree_state((p) => p, respond({
            worktrees: [{ ...row('wt-a', 'a'), ...CLEAN_FIELDS, cleanup_status: 'running' }],
            root: '/wt', base: 'main', ...(capabilities ? { capabilities } : {}),
        }));
        assert.deepEqual(state?.worktrees, [row('wt-a', 'a')], JSON.stringify(capabilities));
    }
});

test('the CLEAN fields are re-validated, never trusted', async () => {
    const state = await fetch_worktree_state((p) => p, respond({
        worktrees: [
            { ...row('wt-a', 'a'), landed: 'yes', landing: { how: 'pr', pr: 1 }, cleanup_status: 'done', cleanup_error: 7 },
            { ...row('wt-b', 'b'), landed: true, landing: { how: 'rebased', pr: 2 } },
            { ...row('wt-c', 'c'), landed: true, landing: { how: 'squash', pr: -4 } },
        ],
        root: '/wt', base: 'main', capabilities: { cleanup: true },
    }));
    const [a, b, c] = state!.worktrees;
    assert.deepEqual([a.landed, a.landing, a.cleanup_status, a.cleanup_error], [null, null, null, null],
        'a landing on a row not proven landed is dropped, as on /branches');
    assert.equal(b.landing, null, 'an unknown proof is no proof');
    assert.deepEqual(c.landing, { how: 'squash', pr: null });
});

test('the poller speeds up while a clean-up runs', async () => {
    const intervals: number[] = [];
    let worktrees: unknown[] = [row('wt-a', 'a')];
    const poller = create_worktree_poller({
        base_url: (p) => p,
        poll_interval_ms: 5000,
        on_change: () => {},
        fetch_impl: async () => new Response(JSON.stringify({
            worktrees, root: '/r', base: 'main', capabilities: { cleanup: true },
        })),
        set_interval: (_fn, ms) => { intervals.push(ms); return intervals.length as never; },
        clear_interval: () => {},
    });
    poller.start();
    await poller.tick();
    worktrees = [{ ...row('wt-a', 'a'), cleanup_status: 'running' }];
    await poller.tick();
    assert.equal(has_active_cleanup(poller.state()), true);
    worktrees = [];
    await poller.tick();
    assert.equal(has_active_cleanup(poller.state()), false);
    assert.deepEqual(intervals, [5000, 1000, 5000]);
});

test('cleanup_request sends the pressed session only when it is not the tree\'s own', () => {
    const tree = row('wt-fix-nav', 'fix/nav');
    assert.deepEqual(cleanup_request(tree, 'wt-fix-nav'), { branch: 'fix/nav' });
    assert.deepEqual(cleanup_request(tree, 'vc-3'), { branch: 'fix/nav', session: 'vc-3' }, 'the cwd join');
});

test('clean_up_worktree posts to /cleanup; 202 is success and a refusal reaches the notice strip', async () => {
    let sent: { url: string; body: unknown } | null = null;
    const accepted = await clean_up_worktree((p) => p, { branch: 'fix/nav', session: 'vc-3' },
        (async (url: string, init: RequestInit): Promise<Response> => {
            sent = { url, body: JSON.parse(String(init.body)) };
            return new Response(JSON.stringify({ ok: true, branch: 'fix/nav' }), { status: 202 });
        }) as never);
    assert.deepEqual(sent, { url: '/plugin/worktree/cleanup', body: { branch: 'fix/nav', session: 'vc-3' } });
    assert.deepEqual([accepted.ok, accepted.status, cleanup_notice(accepted)], [true, 202, null]);
    const busy = await clean_up_worktree((p) => p, { branch: 'fix/nav' }, respond({ error: 'already cleaning up' }, 409));
    assert.equal(cleanup_notice(busy), 'not cleaned up — already cleaning up');
    const boom = (async (): Promise<Response> => { throw new Error('offline'); }) as never;
    assert.equal(cleanup_notice(await clean_up_worktree((p) => p, { branch: 'x' }, boom)),
        'not cleaned up — could not start the clean-up');
});
