import test from 'node:test';
import assert from 'node:assert/strict';
import {
    DEAD_SESSION_TEXT,
    UNGROUPED_COHORT_LABEL,
    build_rack_rows,
    cohort_rollup,
    group_into_cohorts,
    next_session_name,
    ttyd_url,
    cohort_label_for,
    normalize_cohort_label,
    context_tone,
    CONTEXT_LOW_PCT,
    CONTEXT_CRITICAL_PCT,
    URGENCY_RANK,
    hold_row_order,
    rack_row_key,
    usual_model_label,
    type RackInput,
    type RackRow,
    type RackSession,
} from '../../src/engine/terminal/multi-terminal-layout.ts';
import { DEFAULT_RENDER_OPTIONS } from '../../src/engine/terminal/terminal-render-options.ts';

function session(overrides: Partial<RackSession> & { session: string }): RackSession {
    return { state: 'free', live: true, ...overrides };
}

function input(overrides: Partial<RackInput> & { sessions: RackSession[] }): RackInput {
    return {
        focused: null,
        max_sessions: 8,
        ...overrides,
    };
}

test('empty rack renders one explanatory row, not a blank panel', () => {
    const rows = build_rack_rows(input({ sessions: [] }));
    assert.equal(rows.length, 1);
    assert.equal(rows[0].kind, 'empty');
});

test('a session row carries its state and every action', () => {
    const rows = build_rack_rows(input({ sessions: [session({ session: 'main', state: 'busy' })] }));
    assert.equal(rows[0].kind, 'session');
    assert.equal(rows[0].session, 'main');
    assert.equal(rows[0].state, 'busy');
    assert.deepEqual(rows[0].actions, ['focus', 'rename', 'kill']);
});

test('the focused session is marked and drops its now-meaningless focus action', () => {
    const rows = build_rack_rows(
        input({ sessions: [session({ session: 'main' })], focused: 'main' }),
    );
    assert.equal(rows[0].focused, true);
    assert.ok(rows[0].text.startsWith('▶'));
    assert.deepEqual(rows[0].actions, ['rename', 'kill']);
});

test('kill is always LAST, so a new action can never land under a stray click', () => {
    // The rack renders actions in this order and the destructive one sits at the
    // end of the row on purpose. (A renderer whose if/else chain has `kill` as the
    // trailing `else` draws any new action as a ✕.)
    for (const focused of [null, 'main']) {
        const rows = build_rack_rows(input({ sessions: [session({ session: 'main' })], focused }));
        assert.equal(rows[0].actions.at(-1), 'kill');
    }
});

test('attention sessions are hoisted above everything else', () => {
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'aaa-busy', state: 'busy' }),
                session({ session: 'zzz-needs-you', state: 'attention' }),
            ],
        }),
    );
    // Alphabetically 'aaa-busy' wins; by urgency it must not.
    assert.equal(rows[0].session, 'zzz-needs-you');
});

test('ordering is otherwise alphabetical so the rack does not reshuffle', () => {
    const rows = build_rack_rows(
        input({ sessions: [session({ session: 'zeta' }), session({ session: 'alpha' })] }),
    );
    assert.deepEqual(
        rows.filter((r) => r.kind === 'session').map((r) => r.session),
        ['alpha', 'zeta'],
    );
});

test('attention message is shown, but only when it is the reason you are summoned', () => {
    const needy = build_rack_rows(
        input({ sessions: [session({ session: 'a', state: 'attention', message: 'Allow Bash?' })] }),
    );
    assert.ok(needy[0].text.includes('Allow Bash?'));

    const busy = build_rack_rows(
        input({ sessions: [session({ session: 'a', state: 'busy', message: 'Allow Bash?' })] }),
    );
    assert.ok(!busy[0].text.includes('Allow Bash?'));
});

test('a dead session looks dead and offers only kill', () => {
    const rows = build_rack_rows(input({ sessions: [session({ session: 'ghost', live: false })] }));
    assert.equal(rows[0].dim, true);
    // No rename in particular: there is no tmux session left for
    // `tmux rename-session` to find, so the control would always fail.
    assert.deepEqual(rows[0].actions, ['kill']);
    assert.equal(rows[1].kind, 'note');
    assert.equal(rows[1].text, DEAD_SESSION_TEXT);
});

test('a dead row drops its model label rather than telling you twice', () => {
    const dead = session({ session: 'ghost', live: false, model_label: 'claude' });
    const rows = build_rack_rows(input({ sessions: [dead] }));
    // `— session gone —` on the line below already says it, and says it better.
    assert.equal(rows[0].text, 'ghost');
    assert.equal(rows[1].text, DEAD_SESSION_TEXT);
});

test('a live row keeps its model label', () => {
    const alive = session({ session: 'vc-2', model_label: 'Opus 5' });
    const rows = build_rack_rows(input({ sessions: [alive] }));
    assert.equal(rows[0].text, 'vc-2 · Opus 5');
});

test('dead sessions sort last', () => {
    const rows = build_rack_rows(
        input({
            sessions: [session({ session: 'aaa', live: false }), session({ session: 'zzz', live: true })],
        }),
    );
    assert.equal(rows[0].session, 'zzz');
});

// ── the full urgency ladder ─────────────────────────────────────────────────
//
// The dot column IS the rack readout now — the header stopped spelling the
// same thing out in words (see rack-view.ts `fleet_header`) — so the order the
// dots appear in has to mean something on its own.

test('rows sort by how much they want you: attention → done → free → background → busy → unknown → dead', () => {
    // Names chosen so an alphabetical sort would produce the EXACT reverse:
    // this fails loudly if the ladder ever collapses back to a name sort.
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'a-dead', live: false }),
                session({ session: 'b-unknown', state: 'unknown' }),
                session({ session: 'c-busy', state: 'busy' }),
                session({ session: 'd-background', state: 'background' }),
                session({ session: 'e-free', state: 'free' }),
                session({ session: 'f-done', state: 'done' }),
                session({ session: 'g-attention', state: 'attention' }),
            ],
        }),
    );
    assert.deepEqual(
        rows.filter((r) => r.kind === 'session').map((r) => r.session),
        ['g-attention', 'f-done', 'e-free', 'd-background', 'c-busy', 'b-unknown', 'a-dead'],
    );
});

test('URGENCY_RANK is total and strictly ordered', () => {
    const order = ['attention', 'done', 'free', 'background', 'busy', 'unknown'] as const;
    assert.deepEqual(Object.keys(URGENCY_RANK).sort(), [...order].sort());
    for (let i = 1; i < order.length; i += 1) {
        assert.ok(URGENCY_RANK[order[i - 1]] < URGENCY_RANK[order[i]], `${order[i - 1]} before ${order[i]}`);
    }
});

test('a dead-only cohort sorts after every live one, even an all-unknown one', () => {
    // DEAD_RANK is derived from URGENCY_RANK, so background joining moved it to 6.
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/x/alpha', live: false }),
        session({ session: 'b', cwd: '/x/bravo', state: 'unknown' }),
    ]);
    assert.deepEqual(cohorts.map((c) => c.label), ['bravo', 'alpha']);
});

test('a cohort holding only a background session still outranks one holding only busy', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/x/alpha', state: 'busy' }),
        session({ session: 'b', cwd: '/x/bravo', state: 'background' }),
    ]);
    assert.deepEqual(cohorts.map((c) => c.label), ['bravo', 'alpha']);
});

test('a background row says what is running; any other state says nothing of it', () => {
    const counted = build_rack_rows(
        input({ sessions: [session({ session: 'a', state: 'background', background_jobs: 2, model_label: 'Opus 5' })] }),
    );
    assert.equal(counted[0].text, 'a · Opus 5 · 2 background jobs');
    assert.equal(counted[0].background_jobs, 2, 'carried for the dot title');

    const uncounted = build_rack_rows(input({ sessions: [session({ session: 'a', state: 'background' })] }));
    assert.equal(uncounted[0].text, 'a · background work');

    const busy = build_rack_rows(input({ sessions: [session({ session: 'a', state: 'busy', background_jobs: 2 })] }));
    assert.equal(busy[0].text, 'a');
    assert.equal(busy[0].background_jobs, undefined);
});

test('free outranks busy — a working agent wants you least of anything alive', () => {
    // These shared rank 2 and fell through to a name sort, so the dots
    // interleaved by whatever the sessions happened to be called.
    const rows = build_rack_rows(
        input({
            sessions: [session({ session: 'aaa', state: 'busy' }), session({ session: 'zzz', state: 'free' })],
        }),
    );
    assert.equal(rows[0].session, 'zzz');
});

test('same rank still falls back to name, so the rack cannot reshuffle between polls', () => {
    const rows = build_rack_rows(
        input({
            sessions: [session({ session: 'zzz', state: 'busy' }), session({ session: 'aaa', state: 'busy' })],
        }),
    );
    assert.deepEqual(
        rows.filter((r) => r.kind === 'session').map((r) => r.session),
        ['aaa', 'zzz'],
    );
});

test('a live session is ONE row — no pane text under it', () => {
    // No pane preview: a `/capture` tail under the row would be a full-screen
    // TUI's frame by construction, never a readable line.
    const rows = build_rack_rows(input({ sessions: [session({ session: 'a', model_label: 'Opus 5.5' })] }));
    assert.deepEqual(rows.map((r) => r.kind), ['session']);
});

test('overflow is explicit — sessions past the cap are counted, not silently dropped', () => {
    const sessions = ['a', 'b', 'c', 'd', 'e'].map((name) => session({ session: name }));
    const rows = build_rack_rows(input({ sessions, max_sessions: 2 }));
    assert.equal(rows.filter((r) => r.kind === 'session').length, 2);
    const overflow = rows.find((r) => r.kind === 'overflow');
    assert.ok(overflow);
    assert.equal(overflow.text, '+3 MORE');
});

test('no overflow row when everything fits', () => {
    const rows = build_rack_rows(
        input({ sessions: [session({ session: 'a' })], max_sessions: 8 }),
    );
    assert.equal(rows.find((r) => r.kind === 'overflow'), undefined);
});

test('a max_sessions of 0 still shows one session rather than nothing', () => {
    const rows = build_rack_rows(
        input({ sessions: [session({ session: 'a' }), session({ session: 'b' })], max_sessions: 0 }),
    );
    assert.equal(rows.filter((r) => r.kind === 'session').length, 1);
});

test('next_session_name starts at vc-1 on an empty machine', () => {
    assert.equal(next_session_name([]), 'vc-1');
});

test('next_session_name fills the first gap', () => {
    assert.equal(next_session_name(['vc-1', 'vc-3']), 'vc-2');
});

test('next_session_name skips every taken name', () => {
    assert.equal(next_session_name(['vc-1', 'vc-2', 'vc-3']), 'vc-4');
});

test('next_session_name ignores unrelated session names', () => {
    assert.equal(next_session_name(['main', 'BILLING', 'api-refactor']), 'vc-1');
});

test('next_session_name must not reuse a DEAD session name', () => {
    // The name is still claimed by the bridge's store; reusing it would attach
    // to (or resurrect) that row instead of starting a clean shell.
    assert.equal(next_session_name(['vc-1']), 'vc-2');
});

// ── cohorts ────────────────────────────────────────────────────────────────

test('repo is authoritative — every worktree of one repo is ONE cohort', () => {
    // The layout a shared-prefix rule gets wrong: three unrelated roots, one
    // repo. The bridge resolves them all to `my-app` via
    // `git rev-parse --git-common-dir`.
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/Users/me/code/my-app', repo: 'my-app' }),
        session({ session: 'b', cwd: '/Users/me/scratch/any/my-app', repo: 'my-app' }),
        session({ session: 'c', cwd: '/Users/me/worktrees/my-app/testing', repo: 'my-app' }),
    ]);
    assert.equal(cohorts.length, 1);
    assert.equal(cohorts[0].label, 'my-app');
});

test('different repos stay apart even when they share a parent directory', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/Users/me/code/my-app', repo: 'my-app' }),
        session({ session: 'b', cwd: '/Users/me/code/other-repo', repo: 'other-repo' }),
    ]);
    assert.deepEqual(cohorts.map((c) => c.label).sort(), ['my-app', 'other-repo']);
});

test('ONE outlier session can never change another session\'s label', () => {
    // The case a global-prefix rule fails: adding a plain shell in $HOME would
    // collapse both repos into a cohort named `code`.
    const repos = [
        session({ session: 'a', cwd: '/Users/me/code/my-app', repo: 'my-app' }),
        session({ session: 'b', cwd: '/Users/me/code/other-repo', repo: 'other-repo' }),
    ];
    const before = group_into_cohorts(repos).map((c) => c.label).sort();
    const after = group_into_cohorts([...repos, session({ session: 'shell', cwd: '/Users/me' })])
        .map((c) => c.label)
        .filter((l) => l !== 'me')
        .sort();
    assert.deepEqual(after, before);
});

test('a session outside any repo is labelled by its OWN directory, nothing shared', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/tmp/scratch' }),
        session({ session: 'b', cwd: '/Users/me/code/my-app', repo: 'my-app' }),
    ]);
    assert.deepEqual(cohorts.map((c) => c.label).sort(), ['my-app', 'scratch']);
});

test('a sub-directory of a repo groups with the repo, not on its own', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/repo/ui', repo: 'repo' }),
        session({ session: 'b', cwd: '/repo/server/api', repo: 'repo' }),
    ]);
    assert.equal(cohorts.length, 1);
    assert.equal(cohorts[0].label, 'repo');
});

test('cohort_label_for prefers repo over cwd', () => {
    assert.equal(cohort_label_for(session({ session: 'a', cwd: '/x/y', repo: 'my-app' })), 'my-app');
    assert.equal(cohort_label_for(session({ session: 'a', cwd: '/x/y' })), 'y');
    assert.equal(cohort_label_for(session({ session: 'a' })), null);
});

test('a worktree-suffixed DIRECTORY still folds when there is no repo to trust', () => {
    assert.equal(normalize_cohort_label('my-app-wt-3'), 'my-app');
    assert.equal(normalize_cohort_label('my-app.worktrees'), 'my-app');
    assert.equal(normalize_cohort_label('my-app_worktree2'), 'my-app');
    // Narrow on purpose: a project that merely contains the letters keeps them.
    assert.equal(normalize_cohort_label('switch-widget'), 'switch-widget');
    assert.equal(normalize_cohort_label('wt'), 'wt');
});

test('trailing and doubled slashes do not create a separate cohort', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/x/my-app/' }),
        session({ session: 'b', cwd: '/x//my-app' }),
    ]);
    assert.equal(cohorts.length, 1);
});

test('sessions with no cwd fall into a trailing bucket, never a fake project', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'nopath' }),
        session({ session: 'a', cwd: '/x/my-app' }),
    ]);
    assert.equal(cohorts[cohorts.length - 1].label, UNGROUPED_COHORT_LABEL);
    assert.deepEqual(cohorts[cohorts.length - 1].sessions.map((s) => s.session), ['nopath']);
});

test('the cohort holding whoever needs you sorts first', () => {
    const cohorts = group_into_cohorts([
        session({ session: 'a', cwd: '/x/alpha', state: 'busy' }),
        session({ session: 'b', cwd: '/x/zulu', state: 'attention' }),
    ]);
    // 'zulu' loses on name but wins on urgency — a blocked member hoists its group.
    assert.equal(cohorts[0].label, 'zulu');
});

test('the rollup omits zero counts', () => {
    const text = cohort_rollup([
        session({ session: 'a', state: 'attention' }),
        session({ session: 'b', state: 'attention' }),
        session({ session: 'c', state: 'done' }),
    ]);
    assert.equal(text, '2 blocked · 1 done');
});

test('the rollup counts background work between working and done', () => {
    const text = cohort_rollup([
        session({ session: 'a', state: 'busy' }),
        session({ session: 'b', state: 'background' }),
        session({ session: 'c', state: 'background' }),
        session({ session: 'd', state: 'done' }),
    ]);
    assert.equal(text, '1 working · 2 background · 1 done');
});

test('an all-idle cohort still reports a count rather than an empty header', () => {
    assert.equal(cohort_rollup([session({ session: 'a' }), session({ session: 'b' })]), '2 idle');
});

test('a dead session is counted as gone in the rollup', () => {
    assert.equal(cohort_rollup([session({ session: 'a', live: false })]), '1 gone');
});

test('a single-project rack gets NO cohort header — it would be pure noise', () => {
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'a', cwd: '/x/my-app' }),
                session({ session: 'b', cwd: '/x/my-app' }),
            ],
        }),
    );
    assert.equal(rows.filter((r) => r.kind === 'cohort').length, 0);
});

test('two projects get one header each, carrying label and rollup', () => {
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'a', cwd: '/x/alpha', state: 'busy' }),
                session({ session: 'b', cwd: '/x/zulu', state: 'attention' }),
            ],
        }),
    );
    const headers = rows.filter((r) => r.kind === 'cohort');
    assert.equal(headers.length, 2);
    assert.equal(headers[0].text, 'zulu — 1 blocked');
    assert.equal(headers[1].text, 'alpha — 1 working');
});

test('a cohort header is never a target — no session, no actions', () => {
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'a', cwd: '/x/alpha' }),
                session({ session: 'b', cwd: '/x/zulu' }),
            ],
        }),
    );
    for (const header of rows.filter((r) => r.kind === 'cohort')) {
        assert.equal(header.session, undefined);
        assert.deepEqual(header.actions, []);
    }
});

test('overflow is still decided globally — grouping cannot push a blocked agent off the rack', () => {
    // 'needy' sorts last by name and its project sorts last too; the attention
    // ladder must still get it onto the visible set.
    const sessions = [
        session({ session: 'a', cwd: '/x/alpha' }),
        session({ session: 'b', cwd: '/x/alpha' }),
        session({ session: 'needy', cwd: '/x/zulu', state: 'attention' }),
    ];
    const rows = build_rack_rows(input({ sessions, max_sessions: 1 }));
    const shown = rows.filter((r) => r.kind === 'session').map((r) => r.session);
    assert.deepEqual(shown, ['needy']);
    assert.equal(rows.find((r) => r.kind === 'overflow')?.text, '+2 MORE');
});

// ── ttyd_url ────────────────────────────────────────────────────────────────
// Shared by every surface that shows a live pane, so a drift here would point
// two of them at different sessions.

test('ttyd_url returns about:blank when no session is focused', () => {
    assert.equal(ttyd_url(7681, null), 'about:blank');
});

test('ttyd_url attaches to the bridge default session with no ?arg=', () => {
    // `voidcraft` is what ttyd already attaches to with no argument, so it
    // carries no ?arg= — but it still needs its own `?` for the leave-alert
    // option, being the one branch with no query string to append to.
    assert.equal(ttyd_url(7681, 'voidcraft'), 'http://localhost:7681/?disableLeaveAlert=true');
});

test('ttyd_url passes any other session through ?arg=', () => {
    // ttyd runs with -a, so ?arg=<name> is `tmux new -A -s <name>`.
    assert.equal(ttyd_url(7681, 'agent-2'), 'http://localhost:7681/?arg=agent-2&disableLeaveAlert=true');
});

test('ttyd_url honours a non-default port', () => {
    assert.equal(ttyd_url(9999, 'agent-2'), 'http://localhost:9999/?arg=agent-2&disableLeaveAlert=true');
});

test('ttyd_url encodes a session name that would otherwise break the query', () => {
    assert.equal(ttyd_url(7681, 'a b'), 'http://localhost:7681/?arg=a%20b&disableLeaveAlert=true');
    assert.equal(ttyd_url(7681, 'a&b=c'), 'http://localhost:7681/?arg=a%26b%3Dc&disableLeaveAlert=true');
    assert.equal(ttyd_url(7681, 'feat/x'), 'http://localhost:7681/?arg=feat%2Fx&disableLeaveAlert=true');
});

test('ttyd_url always disables ttyd\'s leave-site alert', () => {
    // Every surface, every session shape. ttyd's onWindowUnload sets
    // returnValue, and picking a session IS a navigation of that frame, so
    // without this Chrome interrupts the rack's most common action.
    for (const session of ['voidcraft', 'agent-2', 'a b']) {
        assert.ok(
            ttyd_url(7681, session).includes('disableLeaveAlert=true'),
            `missing leave-alert opt-out for ${session}`,
        );
    }
});

// ── ttyd_url + render options ───────────────────────────────────────────────
// ttyd merges URL options LAST, so these override the bridge's `-t fontSize=…`
// with no restart. See terminal-render-options.ts.

test('ttyd_url without render options is byte-identical to the bare form', () => {
    // The parameter is optional precisely so every existing caller — and the
    // bridge's own server-side defaults — are untouched.
    assert.equal(ttyd_url(7681, 'agent-2', undefined), ttyd_url(7681, 'agent-2'));
    assert.equal(ttyd_url(7681, 'voidcraft', undefined), ttyd_url(7681, 'voidcraft'));
});

test('ttyd_url appends render options to both session branches', () => {
    const render = { ...DEFAULT_RENDER_OPTIONS, font_size: 22, renderer: 'dom' as const };
    // The `voidcraft` branch is the one with no ?arg=, so a `&`/`?` slip shows
    // up here first.
    for (const session of ['voidcraft', 'agent-2']) {
        const url = ttyd_url(7681, session, render);
        assert.ok(url.includes('?'), `${session}: no query string at all`);
        assert.ok(!url.includes('?&'), `${session}: empty first query pair`);
        assert.ok(!url.includes('&&'), `${session}: empty query pair`);
        assert.match(url, /[?&]fontSize=22(&|$)/);
        assert.match(url, /[?&]rendererType=dom(&|$)/);
        assert.ok(url.includes('disableLeaveAlert=true'), `${session}: lost the leave-alert opt-out`);
    }
});

test('ttyd_url scales the font size by the surface DPI factor', () => {
    // A surface that supersamples 2x sends twice the stored size; one that does
    // not sends it as stored. Same logical size, different device value on the wire.
    const render = { ...DEFAULT_RENDER_OPTIONS, font_size: 18 };
    assert.match(ttyd_url(7681, 'agent-2', render, 1), /[?&]fontSize=18(&|$)/);
    assert.match(ttyd_url(7681, 'agent-2', render, 2), /[?&]fontSize=36(&|$)/);
});

test('ttyd_url still returns about:blank with render options and no session', () => {
    assert.equal(ttyd_url(7681, null, DEFAULT_RENDER_OPTIONS), 'about:blank');
});

// ── context left ───────────────────────────────────────────────────────────

test('context left sits beside the model, before an attention message', () => {
    const rows = build_rack_rows(
        input({
            sessions: [session({
                session: 'vc-1', state: 'attention', model_label: 'Opus 5.5',
                context_left: 62, message: 'needs a decision',
            })],
        }),
    );
    assert.equal(rows[0].text, 'vc-1 \u00b7 Opus 5.5 \u00b7 62% left \u00b7 needs a decision');
    assert.deepEqual(rows[0].segments?.map((s) => s.tone), [undefined, undefined, 'normal', undefined]);
    assert.equal(rows[0].segments?.[1].role, 'model', 'the DOM rack moves it to the MODEL column when wide');
    assert.equal(rows[0].model, 'Opus 5.5');
});

test('context and the attention message carry their role, so the DOM rack can place them', () => {
    const rows = build_rack_rows(input({
        sessions: [session({ session: 'vc-1', state: 'attention', context_left: 62, message: 'needs a decision' })],
    }));
    assert.deepEqual(rows[0].segments?.map((s) => s.role), [undefined, 'context', 'message']);
});

// ── the usual model ────────────────────────────────────────────────────────
// One model id on every row carries no information: a row prints its model
// only where it differs from the rack's, or its profile's.

test('usual_model_label: the model most live rows share, null for a tie or a lone row', () => {
    const opus = (name: string, extra: Partial<RackSession> = {}) => session({ session: name, model_label: 'Opus 5.5', ...extra });
    assert.equal(usual_model_label([opus('a'), opus('b'), session({ session: 'c', model_label: 'Sonnet 5.5' })]), 'Opus 5.5');
    assert.equal(usual_model_label([opus('a')]), null, 'one row is no rack');
    assert.equal(usual_model_label([opus('a'), session({ session: 'b', model_label: 'Sonnet 5.5' })]), null, 'a tie names none');
    // A tie for first that a later, larger count overtakes.
    const sonnet = (name: string) => session({ session: name, model_label: 'Sonnet 5.5' });
    assert.equal(usual_model_label([opus('a'), opus('b'), sonnet('c'), sonnet('d'), sonnet('e')]), 'Sonnet 5.5');
    // Dead rows and service rows do not vote.
    assert.equal(usual_model_label([opus('a', { live: false }), opus('b', { live: false }), sonnet('c')]), null);
});

test('a row drops the usual model and keeps an unusual one; the full model rides RackRow.model', () => {
    const rows = build_rack_rows(input({
        sessions: [
            session({ session: 'a', model_label: 'Opus 5.5' }),
            session({ session: 'b', model_label: 'Opus 5.5' }),
            session({ session: 'c', model_label: 'Sonnet 5.5' }),
        ],
    })).filter((row) => row.kind === 'session');
    const by = new Map(rows.map((row) => [row.session, row]));
    assert.equal(by.get('a')!.text, 'a');
    assert.equal(by.get('a')!.model, 'Opus 5.5');
    assert.equal(by.get('c')!.text, 'c · Sonnet 5.5');
});

test("a row on a profile that names its model does not print it again", () => {
    const rows = build_rack_rows(input({
        sessions: [session({ session: 'WEB', model_label: 'gpt-6-codex', profile: 'codex-a', profile_model: 'gpt-6-codex' })],
    }));
    assert.equal(rows[0].text, 'WEB');
    assert.equal(rows[0].model, 'gpt-6-codex');
});

test('a dead row carries no model, not even for the tooltip', () => {
    const rows = build_rack_rows(input({ sessions: [session({ session: 'ghost', live: false, model_label: 'Opus 5.5' })] }));
    assert.equal(rows[0].model, undefined);
});

test('context tone: inclusive thresholds, null when unknown', () => {
    assert.equal(CONTEXT_LOW_PCT, 25);
    assert.equal(CONTEXT_CRITICAL_PCT, 10);
    assert.equal(context_tone(26), 'normal');
    assert.equal(context_tone(25), 'low');
    assert.equal(context_tone(11), 'low');
    assert.equal(context_tone(10), 'critical');
    assert.equal(context_tone(0), 'critical');
    assert.equal(context_tone(null), null);
    assert.equal(context_tone(undefined), null);
    assert.equal(context_tone(Number.NaN), null);
});

test('no context segment when unknown, or on a dead row', () => {
    const rows = build_rack_rows(
        input({
            sessions: [
                session({ session: 'a', model_label: 'Opus 5.5' }),
                session({ session: 'b', live: false, model_label: 'Opus 5.5', context_left: 40 }),
            ],
        }),
    );
    const text = rows.filter((row) => row.kind === 'session').map((row) => row.text);
    assert.ok(!text.some((line) => line.includes('% left')), text.join(' | '));
});



test('first-prompt names change the label without changing transport identity or actions', () => {
    const row = build_rack_rows(input({
        sessions: [session({ session: 'vc-1', auto_name: 'Fix rack resizing' })],
        focused: 'vc-1',
    })).find((row) => row.kind === 'session')!;
    assert.equal(row.session, 'vc-1');
    assert.equal(row.auto_name, 'Fix rack resizing');
    assert.equal(row.segments?.[0].text, '▶ Fix rack resizing');
    assert.equal(row.focused, true);
    assert.ok(row.actions.includes('rename'));
});

// ── hold_row_order ──────────────────────────────────────────────────────────
// The DOM rack renders the held order while a rename is open, so the row being
// typed into never moves (a moved focused input blurs, which ate the draft).

/** `keys` in the order they appear in `rows`, keeping only those listed. */
function order_of(rows: RackRow[], keys: string[]): string[] {
    return rows.map(rack_row_key).filter((key) => keys.includes(key));
}

test('a cohort header carries its label as its identity', () => {
    const rows = build_rack_rows(input({
        sessions: [session({ session: 'a', cwd: '/x/alpha' }), session({ session: 'b', cwd: '/x/zulu' })],
    }));
    const headers = rows.filter((r) => r.kind === 'cohort');
    assert.deepEqual(headers.map((r) => r.cohort), ['alpha', 'zulu']);
    assert.deepEqual(headers.map(rack_row_key), ['cohort:alpha', 'cohort:zulu']);
});

test('rack_row_key is unique across headers, dead-session notes and +N MORE', () => {
    const sessions = [
        session({ session: 'a', cwd: '/x/alpha' }),
        session({ session: 'b', cwd: '/x/zulu', live: false }),
        session({ session: 'c', cwd: '/x/zulu', state: 'busy' }),
    ];
    // A dead row is the bottom rung, so overflow cuts it first: two builds.
    for (const [max_sessions, expected] of [[3, 'note:b'], [2, 'overflow']] as const) {
        const keys = build_rack_rows(input({ sessions, max_sessions })).map(rack_row_key);
        assert.ok(keys.includes(expected), keys.join(' '));
        assert.equal(new Set(keys).size, keys.length);
    }
});

test('a + NEW row being renamed stays put while its agent boots and joins the project', () => {
    const before = build_rack_rows(input({
        sessions: [
            session({ session: 'a', cwd: '/x/my-app' }),
            session({ session: 'b', cwd: '/x/my-app', state: 'busy' }),
            // The optimistic row: no status, no cwd — bottom rung, `(no path)`.
            session({ session: 'vc-3', state: 'unknown' }),
        ],
    }));
    const held = before.map(rack_row_key);
    const after = build_rack_rows(input({
        sessions: [
            session({ session: 'a', cwd: '/x/my-app' }),
            session({ session: 'b', cwd: '/x/my-app', state: 'busy' }),
            session({ session: 'vc-3', cwd: '/x/my-app', state: 'free' }),
        ],
    }));
    // Live, it climbs above `b` and both headers go.
    assert.deepEqual(after.map(rack_row_key), ['session:a', 'session:vc-3', 'session:b']);
    const shown = hold_row_order(held, after);
    assert.deepEqual(shown.map(rack_row_key), ['session:a', 'session:b', 'session:vc-3']);
    // Content is the fresh row's.
    assert.equal(shown[2].state, 'free');
    assert.equal(shown[2], after[1]);
});

test('held rows keep their relative order; new rows land after their fresh predecessor', () => {
    const held = build_rack_rows(input({
        sessions: [session({ session: 'a' }), session({ session: 'b', state: 'busy' })],
    })).map(rack_row_key);
    const fresh = build_rack_rows(input({
        sessions: [
            session({ session: 'b', state: 'attention' }),
            session({ session: 'a' }),
            session({ session: 'c' }),
        ],
    }));
    assert.deepEqual(fresh.map(rack_row_key), ['session:b', 'session:a', 'session:c']);
    const shown = hold_row_order(held, fresh);
    assert.deepEqual(order_of(shown, held), held);
    assert.deepEqual(shown.map(rack_row_key), ['session:a', 'session:c', 'session:b']);
});

test('a gone session drops out of the hold, and a dead one brings its note along', () => {
    const held = build_rack_rows(input({
        sessions: [session({ session: 'a' }), session({ session: 'b' }), session({ session: 'c' })],
    })).map(rack_row_key);
    const fresh = build_rack_rows(input({
        sessions: [session({ session: 'b', live: false }), session({ session: 'c' })],
    }));
    const shown = hold_row_order(held, fresh);
    assert.deepEqual(shown.map(rack_row_key), ['session:b', 'note:b', 'session:c']);
    assert.equal(shown[0].dim, true);
});

test('with no change, the hold is the fresh list itself', () => {
    const rows = build_rack_rows(input({
        sessions: [session({ session: 'a', cwd: '/x/alpha' }), session({ session: 'b', cwd: '/x/zulu' })],
    }));
    assert.deepEqual(hold_row_order(rows.map(rack_row_key), rows), rows);
});
