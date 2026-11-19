import test from 'node:test';
import assert from 'node:assert/strict';
import {
    adopt_focus,
    build_rack_payload,
    rack_header,
    live_sessions,
    parse_context_left,
    to_rack_session,
} from '../../src/engine/terminal/rack-view.ts';
import {
    UNKNOWN_MODEL_LABEL,
    parse_profiles,
    type PortSnapshotRow,
} from '../../src/engine/terminal/terminal-status-logic.ts';

function row(overrides: Partial<PortSnapshotRow> & { session: string }): PortSnapshotRow {
    return {
        state: 'free',
        display: 'free',
        age_seconds: 10,
        message: null,
        title: null,
        attached: false,
        ...overrides,
    };
}

const RACK: PortSnapshotRow[] = [
    row({ session: 'alpha', state: 'attention', display: 'attention', message: 'needs a decision', repo: '/r/one' }),
    row({ session: 'beta', state: 'busy', display: 'busy', cwd: '/tmp/beta', model: 'claude-opus-4-8' }),
    row({ session: 'gamma', display: 'done' }),
];

const COMMON = {
    rows: RACK,
    focused: 'alpha',
    reachable: true,
    max_sessions: 8,
};

/**
 * THE point of this module: one `to_rack_session` for every transport. Two
 * hand-kept copies that must match field for field are an invariant enforced by
 * memory; this test is that invariant, made executable.
 */
test('desktop and remote build the SAME payload for the same rack', () => {
    // The desktop carries the ttyd port.
    const desktop = build_rack_payload({ ...COMMON, ttyd_port: 7681 });
    // The remote page: the bridge services actions directly.
    const remote = build_rack_payload({ ...COMMON, attached: true });

    // Every field that DESCRIBES THE RACK must match exactly.
    assert.equal(desktop.summary, remote.summary);
    assert.equal(desktop.focused, remote.focused);
    assert.equal(desktop.reachable, remote.reachable);
    assert.equal(desktop.max_sessions, remote.max_sessions);

    // Rows match too. Anything drifting here is the bug this test exists for.
    assert.deepEqual(desktop.sessions, remote.sessions);
});

test('the intentional differences are EXACTLY ttyd_port / attached', () => {
    const desktop = build_rack_payload({ ...COMMON, ttyd_port: 7681 });
    const remote = build_rack_payload({ ...COMMON, attached: true });

    const differing = (Object.keys(desktop) as (keyof typeof desktop)[]).filter(
        (key) => JSON.stringify(desktop[key]) !== JSON.stringify(remote[key]),
    );
    assert.deepEqual(differing.sort(), ['ttyd_port']);
    assert.equal(desktop.attached, undefined);
    assert.equal(remote.attached, true);
    assert.equal(remote.ttyd_port, undefined);
});

test('state comes from the committed display verdict, not the raw bridge state', () => {
    // A session that finished while you were away is `free` upstream and `done`
    // after the tracker. Reading `state` would silently drop the done highlight.
    const mapped = to_rack_session(row({ session: 'gamma', state: 'free', display: 'done' }));
    assert.equal(mapped.state, 'done');
});

test('background_jobs rides through on a background row only', () => {
    const background = to_rack_session(row({ session: 'bg', state: 'background', display: 'background', background_jobs: 3 }));
    assert.equal(background.state, 'background');
    assert.equal(background.background_jobs, 3);
    // The bridge has no count (background agents carry no pid): null, never dropped.
    const uncounted = to_rack_session(row({ session: 'bg', state: 'background', display: 'background' }));
    assert.equal(uncounted.background_jobs, null);
    // Still debouncing from busy: the row is DRAWN busy, so it carries no jobs label yet.
    const settling = to_rack_session(row({ session: 'bg', state: 'background', display: 'busy', background_jobs: 3 }));
    assert.equal('background_jobs' in settling, false);
});

test('a dead row survives mapping as live: false', () => {
    assert.equal(to_rack_session(row({ session: 'ghost', live: false })).live, false);
    // Absent `live` (older bridge) counts as alive — never hide on missing data.
    assert.equal(to_rack_session(row({ session: 'old' })).live, true);
});

test('the summary carries the agents prefix on EVERY path', () => {
    // The drift this pins: a bus path that prefixes and an HTTP path that does
    // not show two different headers in the same window, depending on which
    // transport it happens to be using.
    const payload = build_rack_payload({ ...COMMON, attached: true });
    assert.match(payload.summary, /^agents/);
});

test('an empty rack reads as the bare noun, never a dangling separator', () => {
    const payload = build_rack_payload({ ...COMMON, rows: [] });
    assert.equal(payload.summary, 'agents');
});

test('the count and the list can no longer disagree', () => {
    // One path counting the whole snapshot while another counts its own
    // filtered list is the disagreement. Both count exactly the rows they render.
    const payload = build_rack_payload({ ...COMMON, rows: RACK.slice(0, 2) });
    assert.equal(payload.sessions.length, 2);
    assert.equal(payload.summary, 'agents · 2');
});

// ── the header is a COUNT, never a state rollup ─────────────────────────────
//
// A rollup such as `agents — 1 busy · 1 done · 1 free` is rewritten by every
// state flip, so a single agent oscillating between `done` and `free` reflows
// the whole line several times a minute. The row dots already carry state; the
// header carries only what dots cannot.

test('the header does not move when a session merely changes state', () => {
    const before = build_rack_payload({ ...COMMON, rows: RACK }).summary;
    // The flicker: gamma goes done → free, nothing else moves.
    const after = build_rack_payload({
        ...COMMON,
        rows: [RACK[0], RACK[1], row({ session: 'gamma', display: 'free' })],
    }).summary;
    assert.equal(before, after);
    assert.equal(before, 'agents · 3');
});

test('the header DOES move when the rack gains or loses a session — that is real news', () => {
    assert.equal(build_rack_payload({ ...COMMON, rows: RACK.slice(0, 1) }).summary, 'agents · 1');
    assert.equal(rack_header(0), 'agents');
    assert.equal(rack_header(12), 'agents · 12');
});

test('no state word survives in the header', () => {
    const payload = build_rack_payload({ ...COMMON, rows: RACK });
    for (const word of ['busy', 'free', 'done', 'needs you', 'unknown']) {
        assert.ok(!payload.summary.includes(word), `header still spells out "${word}"`);
    }
});

// ── focus adoption ─────────────────────────────────────────────────────────
//
// One rule for every pane owner (the rack page, the pop-out host, the remote
// transport), written once.

test('a live focus is kept', () => {
    assert.equal(adopt_focus('beta', ['alpha', 'beta']), 'beta');
});

test('a DEAD focus is dropped to the first live row', () => {
    // Not cosmetic: focusing a dead name points ttyd at `?arg=<name>`, and under
    // `-a` that is `tmux new -A -s <name>` — it RESURRECTS what you just killed.
    assert.equal(adopt_focus('ghost', ['alpha', 'beta']), 'alpha');
});

test('no focus adopts the first live row', () => {
    assert.equal(adopt_focus(null, ['alpha']), 'alpha');
});

test('an empty rack clears focus rather than inventing one', () => {
    assert.equal(adopt_focus('alpha', []), null);
    assert.equal(adopt_focus(null, []), null);
});

test('pending_new survives even though the bridge has never heard of it', () => {
    // `+ NEW` points the pane at a name that does not exist yet; the session is
    // created only once the iframe loads and its socket connects. Without this
    // clause the very next poll yanks focus away and it is never created at all.
    assert.equal(adopt_focus('brand-new', ['alpha'], 'brand-new'), 'brand-new');
});

test('pending_new does NOT protect some other session', () => {
    assert.equal(adopt_focus('ghost', ['alpha'], 'brand-new'), 'alpha');
});

test('live_sessions treats an absent live flag as alive', () => {
    // Older bridges omit it — never drop a session on missing data.
    assert.deepEqual(
        live_sessions([row({ session: 'old' }), row({ session: 'ghost', live: false })]),
        ['old'],
    );
});

// ── attach-only: the invariant for EVERY worktree, not only the managed ones ─
//
// `pane_arg` protects the plugin-managed rows by session name. A worktree the
// plugin does not manage is exposed to the same bug: kill the agent there, click
// its row, and ttyd's attach-or-create rebuilds the session in the main checkout.
// `/state` lists every non-main worktree path, and the payload carries the joined set.

import { attach_only_sessions, pane_arg } from '../../src/engine/terminal/rack-view.ts';

const WORKTREE_PATHS = ['~/worktrees/my-app/docs-sync', '~/worktrees/my-app/fix-nav'];

test('attach_only_sessions joins a session to a worktree by cwd containment', () => {
    const rows = [
        { session: 'deep', cwd: '/Users/u/worktrees/my-app/docs-sync/ui/src' },
        { session: 'root', cwd: '/Users/u/worktrees/my-app/fix-nav' },
        { session: 'sibling', cwd: '/Users/u/worktrees/my-app/docs-sync-2' },
        { session: 'main', cwd: '/Users/u/code/my-app' },
        { session: 'nowhere', cwd: null },
        { session: 'old-bridge' },
    ];
    assert.deepEqual([...attach_only_sessions(rows, WORKTREE_PATHS)].sort(), ['deep', 'root']);
});

test('the main checkout is never in the list, so a main-checkout session is never attach-only', () => {
    // The plugin excludes it; this pins the consequence — `+ NEW` keeps the
    // creating form it depends on.
    const rows = [{ session: 'vc-1', cwd: '/Users/u/code/my-app' }];
    assert.equal(attach_only_sessions(rows, WORKTREE_PATHS).size, 0);
    assert.equal(attach_only_sessions(rows, []).size, 0);
});

test('pane_arg with the set addresses a cwd-joined session attach-only', () => {
    assert.equal(pane_arg('deep', [], new Set(['deep'])), 'attach:deep');
    assert.equal(pane_arg('deep', undefined, new Set(['deep'])), 'attach:deep');
});

test('pane_arg without the set is byte-identical to before it existed', () => {
    assert.equal(pane_arg('deep', []), 'deep');
    assert.equal(pane_arg('deep', [], new Set()), 'deep');
    assert.equal(pane_arg('deep', [], new Set(['other'])), 'deep');
});

test('build_rack_payload carries attach_only only when given', () => {
    const without = build_rack_payload({ ...COMMON });
    assert.equal('attach_only' in without, false, 'absent means "no plugin listed any paths"');
    const with_set = build_rack_payload({ ...COMMON, attach_only: ['beta'] });
    assert.deepEqual(with_set.attach_only, ['beta']);
    const empty = build_rack_payload({ ...COMMON, attach_only: [] });
    assert.deepEqual(empty.attach_only, [], 'an empty list is an answer, not an absence');
});

test('build_rack_payload carries access only when the gate is the blocker', () => {
    // Absent is what keeps `reachable: false` meaning "the bridge was asked and
    // did not answer". Writing the key unconditionally would put a local-tools
    // message in front of every genuine outage.
    const without = build_rack_payload({ ...COMMON });
    assert.equal('access' in without, false, 'absent means "the gate is not in the way"');
    const locked = build_rack_payload({ ...COMMON, access: 'locked' });
    assert.equal(locked.access, 'locked');
    const prompting = build_rack_payload({ ...COMMON, access: 'prompting' });
    assert.equal(prompting.access, 'prompting');
});

// ── worktree_for_session: the PR button's join ──────────────────────────────
import { worktree_for_session } from '../../src/engine/terminal/rack-view.ts';
import type { WorktreeRow } from '../../src/engine/terminal/rack-view.ts';

const WT = (branch: string, path: string): WorktreeRow => ({
    branch, path, session: `wt-${branch.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`,
    dirty: false, ahead: 0, behind: 0, pr_status: null, pr_url: null, pr_error: null,
});
const MANAGED = [WT('feat/foo', '/Users/x/worktrees/my-app/feat-foo'),
                 WT('fix/nav', '/Users/x/worktrees/my-app/fix-nav')];

test('worktree_for_session joins by the derived name first', () => {
    const row = worktree_for_session({ session: 'wt-fix-nav', cwd: '/Users/x/code/my-app' }, MANAGED);
    assert.equal(row?.branch, 'fix/nav');   // name wins even when the cwd says main
});

test('worktree_for_session falls back to cwd containment for an ordinary name', () => {
    // `claude -w` inside a `+ NEW` pane, or EnterWorktree mid-session: the tmux
    // name is unrelated, the cwd is the only tell.
    const row = worktree_for_session(
        { session: 'agent-3', cwd: '/Users/x/worktrees/my-app/feat-foo/ui/src' }, MANAGED);
    assert.equal(row?.branch, 'feat/foo');
});

test('worktree_for_session is undefined off every worktree, with no cwd, and with no plugin', () => {
    assert.equal(worktree_for_session({ session: 'main-agent', cwd: '/Users/x/code/my-app' }, MANAGED), undefined);
    assert.equal(worktree_for_session({ session: 'x', cwd: null }, MANAGED), undefined);
    assert.equal(worktree_for_session({ session: 'wt-feat-foo', cwd: null }, undefined), undefined);
    assert.equal(worktree_for_session({ session: 'wt-feat-foo', cwd: null }, []), undefined);
});

test('worktree_for_session never matches a sibling by prefix', () => {
    const rows = [...MANAGED, WT('feat/foo-2', '/Users/x/worktrees/my-app/feat-foo-2')];
    const row = worktree_for_session({ session: 'z', cwd: '/Users/x/worktrees/my-app/feat-foo-2/docs' }, rows);
    assert.equal(row?.branch, 'feat/foo-2');
});

// ── launch profiles ─────────────────────────────────────────────────────────

const PROFILES = parse_profiles([
    { id: 'claude-a', label: 'A', kind: 'claude', default: true },
    { id: 'codex-a', label: 'CODEX A', kind: 'codex', model_label: 'gpt-6-codex' },
])!;

test('to_rack_session carries a valid profile and drops a malformed one', () => {
    assert.equal(to_rack_session(row({ session: 'vc-1', profile: 'codex-a' })).profile, 'codex-a');
    assert.equal('profile' in to_rack_session(row({ session: 'vc-1', profile: 'Not Valid' })), false);
    assert.equal('profile' in to_rack_session(row({ session: 'vc-1' })), false);
});

test("a codex row shows its profile's model, never 'model unknown'", () => {
    const codex = to_rack_session(row({ session: 'vc-2', profile: 'codex-a' }), PROFILES);
    assert.equal(codex.model_label, 'gpt-6-codex');
    // Only the GAP is filled: a model the bridge actually read wins.
    const read = to_rack_session(row({ session: 'vc-3', profile: 'codex-a', model: 'gpt-6.1-sol' }), PROFILES);
    assert.equal(read.model_label, 'gpt-6.1-sol');
    // No profile → the honest unknown.
    assert.equal(to_rack_session(row({ session: 'vc-4' }), PROFILES).model_label, UNKNOWN_MODEL_LABEL);
});

test("to_rack_session carries the profile's own model, so the row need not print it", () => {
    assert.equal(to_rack_session(row({ session: 'vc-2', profile: 'codex-a' }), PROFILES).profile_model, 'gpt-6-codex');
    // A profile that names no model, or no profile at all, carries none.
    assert.equal('profile_model' in to_rack_session(row({ session: 'vc-1', profile: 'claude-a' }), PROFILES), false);
    assert.equal('profile_model' in to_rack_session(row({ session: 'vc-4' }), PROFILES), false);
});

test('profiled sessions join attach_only even with no worktree plugin', () => {
    const payload = build_rack_payload({
        ...COMMON,
        rows: [row({ session: 'vc-1', profile: 'claude-a' }), row({ session: 'plain' })],
        profiles: PROFILES,
    });
    assert.deepEqual(payload.attach_only, ['vc-1']);
    assert.equal(pane_arg('vc-1', payload.worktrees, new Set(payload.attach_only)), 'attach:vc-1');
    assert.equal(pane_arg('plain', payload.worktrees, new Set(payload.attach_only)), 'plain');
});

test('a pending /spawn launch is attach-only BEFORE the snapshot lists it', () => {
    const payload = build_rack_payload({ ...COMMON, pending_profiled: 'vc-9' });
    assert.deepEqual(payload.attach_only, ['vc-9']);
});

test('attach_only merges the worktree set with the profiled one, without duplicates', () => {
    const payload = build_rack_payload({
        ...COMMON,
        rows: [row({ session: 'wt-a', profile: 'codex-a' }), row({ session: 'vc-1', profile: 'codex-a' })],
        attach_only: ['wt-a', 'claude-ws'],
    });
    assert.deepEqual(payload.attach_only, ['wt-a', 'claude-ws', 'vc-1']);
});

test('no profiles anywhere leaves attach_only and profiles ABSENT — a pre-profile bridge is byte-identical', () => {
    const payload = build_rack_payload(COMMON);
    assert.equal('attach_only' in payload, false);
    assert.equal('profiles' in payload, false);
    assert.equal('launch_notice' in payload, false);
});

test('spawning rides the payload only while a /spawn is in flight; pane_loading is never a host field', () => {
    assert.equal(build_rack_payload({ ...COMMON, spawning: 'vc-9' }).spawning, 'vc-9');
    const idle = build_rack_payload({ ...COMMON, spawning: null });
    assert.equal('spawning' in idle, false);
    assert.equal('pane_loading' in idle, false);
});

test('context_left crosses the boundary validated: integer 0–100 or null', () => {
    assert.equal(parse_context_left(61.6), 62);
    assert.equal(parse_context_left(140), 100);
    assert.equal(parse_context_left(-3), 0);
    assert.equal(parse_context_left('62'), null);
    assert.equal(parse_context_left(Number.NaN), null);
    assert.equal(parse_context_left(undefined), null);
    assert.equal(to_rack_session(row({ session: 'a', context_left: 37 })).context_left, 37);
    assert.equal(to_rack_session(row({ session: 'b' })).context_left, null);
});

test('a cut-off turn rides to_rack_session onto the rack session', () => {
    const stop_failure = { error: 'rate_limit', limit: 'five_hour', resets_at: 1700003600, message: null };
    assert.deepEqual(to_rack_session(row({ session: 'vc-1', stop_failure })).stop_failure, stop_failure);
    assert.equal('stop_failure' in to_rack_session(row({ session: 'vc-1' })), false);
});

test('a fresh session rides to_rack_session, and anything but a literal true does not', () => {
    assert.equal(to_rack_session(row({ session: 'vc-1', fresh: true })).fresh, true);
    assert.equal('fresh' in to_rack_session(row({ session: 'vc-1', fresh: false })), false);
    assert.equal('fresh' in to_rack_session(row({ session: 'vc-1' })), false);
});

// ── islands: a session working in a non-main worktree ───────────────────────
// An agent's changes are easy to look for in the wrong checkout, so the row
// says where the agent's edits land. `island_for_session`
// joins by cwd against EVERY non-main tree, not only the managed rows.
import { island_for_session, island_pr, session_island, type WorktreeIsland } from '../../src/engine/terminal/rack-view.ts';
import { build_rack_rows } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { session_graph } from '../../src/engine/terminal/session-graph.ts';

const ISLANDS: WorktreeIsland[] = [
    // Outside the managed root, and its leaf is the repository's own name.
    { path: '~/scratch/spike/my-app', branch: null },
    { path: '~/worktrees/my-app/fix-nav', branch: 'fix/nav' },
    { path: '~/worktrees/my-app/fix-nav/nested', branch: 'nested' },
];

test('island_for_session joins an ordinary name by cwd, longest root winning', () => {
    // EnterWorktree mid-session: the name is `vc-3`, the cwd is the only tell.
    assert.equal(island_for_session({ cwd: '/Users/u/worktrees/my-app/fix-nav/ui' }, ISLANDS)?.branch, 'fix/nav');
    assert.equal(island_for_session({ cwd: '/Users/u/worktrees/my-app/fix-nav/nested/x' }, ISLANDS)?.branch, 'nested');
    assert.equal(island_for_session({ cwd: '/Users/u/scratch/spike/my-app' }, ISLANDS)?.path,
        '~/scratch/spike/my-app', 'an unmanaged tree is an island too');
});

test('island_for_session: no island in the main checkout, a sibling, no cwd, or no plugin', () => {
    assert.equal(island_for_session({ cwd: '/Users/u/code/my-app' }, ISLANDS), undefined);
    assert.equal(island_for_session({ cwd: '/Users/u/worktrees/my-app/fix-nav-2' }, ISLANDS), undefined);
    assert.equal(island_for_session({ cwd: null }, ISLANDS), undefined);
    assert.equal(island_for_session({ cwd: '/Users/u/worktrees/my-app/fix-nav' }, undefined), undefined);
    assert.equal(island_for_session({ cwd: '/Users/u/worktrees/my-app/fix-nav' }, []), undefined);
});

test('session_island prints the branch, and names the path in the title', () => {
    const island = session_island({ path: '~/worktrees/my-app/fix-nav', branch: 'fix/nav' }, 'vc-3', 'my-app');
    assert.equal(island.label, 'fix/nav');
    assert.match(island.title, /branch fix\/nav · ~\/worktrees\/my-app\/fix-nav/);
    assert.match(island.title, /not in the main checkout/);
});

test('session_island colours the island by the branch PR: blue to make one, green once handled, mint while unknown', () => {
    const at = (pr?: WorktreeIsland['pr']) => session_island({ path: '~/worktrees/my-app/fix-nav', branch: 'fix/nav', ...(pr ? { pr } : {}) }, 'vc-3');
    assert.equal(at().pr, 'unknown', 'absent is unknown, never "no PR"');
    assert.doesNotMatch(at().title, /PR/);
    const none = at({ state: 'none', number: null, url: null });
    assert.equal(none.pr, 'needed');
    assert.match(none.title, /· no PR yet/);
    assert.equal(at({ state: 'closed', number: 3, url: null }).pr, 'needed', 'a closed PR is none open');
    const open = at({ state: 'open', number: 101, url: 'https://github.com/o/r/pull/101' });
    assert.equal(open.pr, 'handled');
    assert.match(open.title, /· PR #101 open/);
    assert.equal(at({ state: 'merged', number: 7, url: null }).pr, 'handled');
});

test('island_pr: a landed branch is handled whatever its own PR says; not landed with none is needed', () => {
    const merged_in = { how: 'ancestor', pr: 41 } as const;
    const none = { state: 'none', number: null, url: null } as const;
    assert.equal(island_pr({ pr: none, landing: merged_in }), 'handled', 'landed with no PR of its own');
    assert.equal(island_pr({ pr: { state: 'closed', number: 9, url: null }, landing: merged_in }), 'handled', 'landed with a closed PR');
    assert.equal(island_pr({ pr: none, landing: { how: 'commits', pr: null } }), 'handled', 'landed with no PR at all');
    assert.equal(island_pr({ pr: none }), 'needed', 'not landed, no PR');
    assert.equal(island_pr({ pr: none, landing: null }), 'needed');
    assert.equal(island_pr({}), 'unknown', 'nothing known stays unknown');
    // A branch cut from main and never committed to is an ancestor with no PR: nothing of its own landed.
    assert.equal(island_pr({ pr: none, landing: { how: 'ancestor', pr: null } }), 'needed');
    assert.equal(island_pr({ landing: { how: 'ancestor', pr: null } }), 'unknown');
});

test('session_island names how a landed branch landed, and sends only the colour', () => {
    const at = (landing: WorktreeIsland['landing'], pr: WorktreeIsland['pr'] = { state: 'none', number: null, url: null }) =>
        session_island({ path: '~/worktrees/my-app/lane', branch: 'lane', pr, landing }, 'vc-3');
    const carried = at({ how: 'ancestor', pr: 41 });
    assert.equal(carried.pr, 'handled');
    assert.match(carried.title, /· landed in #41 —/);
    assert.doesNotMatch(carried.title, /no PR yet/);
    assert.equal('pr_state' in carried, false, 'its own PR state would paint it blue in island_status');
    assert.match(at({ how: 'squash', pr: null }).title, /· landed: on main —/);
    assert.match(at({ how: 'pr', pr: 12 }, { state: 'closed', number: 3, url: null }).title, /· landed in #12/);
    const own = at({ how: 'pr', pr: 12 }, { state: 'merged', number: 12, url: null });
    assert.equal(own.pr_state, 'merged', 'its own merged PR still says so');
    const fresh = at({ how: 'ancestor', pr: null });
    assert.equal(fresh.pr, 'needed');
    assert.match(fresh.title, /· no PR yet/);
    assert.equal(fresh.pr_state, 'none');
});

test('session_island drops the label when the row name already says it — the glyph stays', () => {
    assert.equal(session_island({ path: '~/worktrees/my-app/feat-nav', branch: 'feat/nav' }, 'wt-feat-nav').label, null);
    // A segment test, not a substring one: `fixer` does not say `fix`.
    assert.equal(session_island({ path: '~/w/fix', branch: 'fix' }, 'fixer').label, 'fix');
});

test('session_island falls back to the folder that tells the checkout apart', () => {
    const detached = session_island({ path: '~/scratch/spike/my-app', branch: null }, 'vc-1', 'my-app');
    assert.equal(detached.label, 'spike', 'the repo-named leaf says nothing');
    assert.match(detached.title, /detached HEAD/);
    // An older plugin sent paths only: branch ABSENT is "not told", never "detached".
    const untold = session_island({ path: '~/worktrees/my-app/fix-nav' }, 'vc-1', 'my-app');
    assert.equal(untold.label, 'fix-nav');
    assert.doesNotMatch(untold.title, /detached|branch/);
});

test('build_rack_payload stamps an island on worktree rows only, and rows carry it', () => {
    const rows = [
        row({ session: 'vc-3', cwd: '/Users/u/worktrees/my-app/fix-nav/ui', repo: 'my-app' }),
        row({ session: 'main-agent', cwd: '/Users/u/code/my-app', repo: 'my-app' }),
    ];
    const payload = build_rack_payload({ ...COMMON, rows, focused: null, islands: ISLANDS });
    const by_name = new Map(payload.sessions.map((s) => [s.session, s]));
    assert.equal(by_name.get('vc-3')?.island?.label, 'fix/nav');
    assert.equal('island' in by_name.get('main-agent')!, false);
    const rack_rows = build_rack_rows({ sessions: payload.sessions, focused: null, max_sessions: 8 });
    assert.equal(rack_rows.find((r) => r.session === 'vc-3')?.island?.label, 'fix/nav');
    assert.equal(session_graph(payload.sessions).nodes.find((n) => n.data.session === 'vc-3')?.data.island?.label,
        'fix/nav', 'GRAPH keeps the island LIST shows');
    assert.equal(build_rack_payload({ ...COMMON, rows }).sessions.some((s) => s.island), false,
        'no plugin, no islands');
});

// ── the worktree button: PR, or CLEAN once the branch has landed ────────────
import {
    cleaning_sessions,
    held_pane_arg,
    work_landed,
    worktree_button_kind,
    worktree_button_view,
} from '../../src/engine/terminal/rack-view.ts';

const LANDED = (extra: Partial<WorktreeRow> = {}): WorktreeRow => ({
    ...WT('fix/nav', '/Users/x/worktrees/my-app/fix-nav'),
    landed: true, landing: { how: 'squash', pr: 102 }, ...extra,
});

test('the button is CLEAN once landed, and PR while the branch is not — or unknown', () => {
    assert.equal(worktree_button_kind(LANDED()), 'cleanup');
    assert.equal(worktree_button_kind(LANDED({ dirty: null })), 'cleanup', 'unswept dirt: the worker re-checks it');
    assert.equal(worktree_button_kind(LANDED({ landed: false, landing: null })), 'pr');
    assert.equal(worktree_button_kind(LANDED({ landed: null, landing: null })), 'pr', 'unknown is never landed');
    const older_plugin = WT('fix/nav', '/w/fix-nav');
    assert.equal('landed' in older_plugin, false);
    assert.equal(worktree_button_kind(older_plugin), 'pr', 'absent keeps the old PR button');
});

test('landed + dirty stays PR: that is new work after the merge', () => {
    assert.equal(worktree_button_kind(LANDED({ dirty: true })), 'pr');
});

test('a branch cut from main with no work of its own is an ancestor, and stays PR', () => {
    // `/state` for a fresh worktree session: 0 ahead, so an ancestor of main —
    // landed — with no PR. CLEAN here would kill its agent mid-turn.
    const fresh = LANDED({ ahead: 0, dirty: false, landing: { how: 'ancestor', pr: null } });
    assert.equal(worktree_button_kind(fresh), 'pr');
    assert.equal(worktree_button_kind(LANDED({ landing: null })), 'pr', 'landed with no proof named');
    assert.equal(worktree_button_kind(LANDED({ ahead: 0, landing: { how: 'ancestor', pr: 103 } })), 'cleanup',
        'its merged PR shipped this very tip (a merge commit)');
    assert.deepEqual(
        [work_landed({ how: 'ancestor', pr: null }), work_landed({ how: 'ancestor', pr: 103 }),
            work_landed({ how: 'squash', pr: null }), work_landed({ how: 'commits', pr: null }), work_landed(null)],
        [false, true, true, true, false],
    );
});

test('a running clean-up keeps CLEAN whatever landed says; a PR job keeps PR', () => {
    assert.equal(worktree_button_kind(LANDED({ landed: false, cleanup_status: 'running' })), 'cleanup');
    assert.equal(worktree_button_kind(LANDED({ pr_status: 'opening' })), 'pr');
    assert.equal(worktree_button_kind(LANDED({ pr_status: 'open', pr_url: 'u' })), 'cleanup',
        'a PR that merged is exactly the case this exists for');
});

test('a FAILED clean-up stays CLEAN only while the row is still cleanable', () => {
    assert.equal(worktree_button_kind(LANDED({ cleanup_status: 'error', cleanup_error: 'lsof' })), 'cleanup');
    // Refused for new work: the fresh probe found an edit the sweep had not.
    const dirty = LANDED({ dirty: true, cleanup_status: 'error',
        cleanup_error: 'uncommitted work in the worktree — nothing removed' });
    assert.equal(worktree_button_kind(dirty), 'pr', 'new work wants PR, not a button that can only fail');
    assert.equal(worktree_button_view(dirty, null).title,
        'Commit, push and open a PR for fix/nav (uncommitted changes)'
        + ' · clean-up refused: uncommitted work in the worktree — nothing removed');
    const moved = LANDED({ landed: false, landing: null, cleanup_status: 'error', cleanup_error: 'not landed in main' });
    assert.equal(worktree_button_view(moved, null).label, 'PR');
    assert.match(worktree_button_view(moved, null).title, / · clean-up refused: not landed in main$/);
});

test('CLEAN labels share PR\'s glyph-width vocabulary, and only `running` refuses a press', () => {
    assert.deepEqual(
        [worktree_button_view(LANDED(), null), worktree_button_view(LANDED(), 'cleanup'),
            worktree_button_view(LANDED({ cleanup_status: 'running' }), null),
            worktree_button_view(LANDED({ cleanup_status: 'error', cleanup_error: 'dirty tree' }), null)]
            .map((view) => [view.label, view.press, view.tone]),
        [['CLEAN', 'arm', null], ['CLEAN?', 'arm', null], ['CLEAN…', 'none', null], ['CLEAN !', 'arm', 'error']],
    );
    assert.equal(worktree_button_view(LANDED({ cleanup_status: 'running' }), 'cleanup').label, 'CLEAN…',
        'a running job outranks a stale arm');
});

test('the CLEAN title names why the work is in main, and what the press does', () => {
    const title = (landing: WorktreeRow['landing']) => worktree_button_view(LANDED({ landing }), null).title;
    assert.equal(title({ how: 'squash', pr: 102 }),
        'fix/nav: PR #102 merged — remove this worktree, its branch and this session');
    assert.match(title({ how: 'pr', pr: 104 }), /^fix\/nav: PR #104 merged — /);
    assert.match(title({ how: 'ancestor', pr: 103 }), /^fix\/nav: PR #103 merged — /, 'a merge commit names its PR');
    assert.match(title({ how: 'squash', pr: null }), /^fix\/nav: squash-merged into main — /);
    assert.match(title({ how: 'commits', pr: null }), /every commit is in main/);
    assert.match(worktree_button_view(LANDED({ cleanup_status: 'error', cleanup_error: 'dirty tree' }), null).title,
        /^Clean-up failed: dirty tree$/);
});

test('an arm is keyed by KIND: a PR arm never shows on CLEAN, nor a CLEAN arm on PR', () => {
    assert.equal(worktree_button_view(LANDED(), 'pr').label, 'CLEAN');
    assert.equal(worktree_button_view(LANDED({ landed: false }), 'cleanup').label, 'PR');
});

test('the PR button reads as it always did', () => {
    const pr = (extra: Partial<WorktreeRow>, armed: 'pr' | null = null) =>
        worktree_button_view({ ...WT('fix/nav', '/w/fix-nav'), ...extra }, armed);
    assert.deepEqual([pr({}).label, pr({}).press, pr({}).title],
        ['PR', 'arm', 'Commit, push and open a PR for fix/nav (0 commit(s))']);
    assert.equal(pr({ dirty: true }).title, 'Commit, push and open a PR for fix/nav (uncommitted changes)');
    assert.deepEqual([pr({}, 'pr').label, pr({}, 'pr').title], ['PR?', 'Click again to commit, push and open the PR']);
    assert.deepEqual([pr({ pr_status: 'opening' }).label, pr({ pr_status: 'opening' }).press], ['PR…', 'none']);
    const open = pr({ pr_status: 'open', pr_url: 'https://github.com/o/r/pull/9' });
    assert.deepEqual([open.label, open.press, open.url, open.tone],
        ['PR ↗', 'open', 'https://github.com/o/r/pull/9', 'open']);
    const failed = pr({ pr_status: 'error', pr_error: 'gh missing' });
    assert.deepEqual([failed.label, failed.title, failed.tone], ['PR !', 'PR failed: gh missing', 'error']);
});

test('cleaning_sessions: the derived session and every cwd-joined one, only while running', () => {
    const rows = [
        { session: 'vc-3', cwd: '/Users/x/worktrees/my-app/fix-nav/ui' },
        { session: 'main-agent', cwd: '/Users/x/code/my-app' },
        { session: 'wt-feat-foo', cwd: null },
    ];
    const running = [{ ...MANAGED[0] }, { ...MANAGED[1], cleanup_status: 'running' as const }];
    assert.deepEqual([...cleaning_sessions(rows, running)].sort(), ['vc-3', 'wt-fix-nav']);
    const failed = [MANAGED[0], { ...MANAGED[1], cleanup_status: 'error' as const }];
    assert.equal(cleaning_sessions(rows, failed).size, 0, 'a refused clean-up leaves its sessions adoptable');
    assert.equal(cleaning_sessions(rows, undefined).size, 0);
});

test('held_pane_arg never lets a focused pane go from attach-only to the CREATING form', () => {
    // The tree is gone from `/state` — no managed row, no attach_only entry — but
    // the snapshot has not caught up, so the pane is still on `wt-fix-nav`.
    assert.equal(held_pane_arg('wt-fix-nav', 'attach:wt-fix-nav', [], new Set()), 'attach:wt-fix-nav');
    assert.equal(pane_arg('wt-fix-nav', [], new Set()), 'wt-fix-nav', 'what the recomputed arg would have been');
});

test('held_pane_arg is pane_arg for a session the pane is not already on', () => {
    assert.equal(held_pane_arg('vc-1', null), 'vc-1', '+ NEW keeps the creating form');
    assert.equal(held_pane_arg('vc-1', 'attach:wt-fix-nav'), 'vc-1', 'another session\'s hold is not this one\'s');
    assert.equal(held_pane_arg('wt-fix-nav', 'vc-1', MANAGED), 'attach:wt-fix-nav');
    assert.equal(held_pane_arg('vc-1', 'vc-1', [], new Set(['vc-1'])), 'attach:vc-1', 'bare → attach still upgrades');
});
