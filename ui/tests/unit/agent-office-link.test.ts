import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { parse_profiles } from '../../src/engine/terminal/terminal-status-logic.ts';
import {
    OFFICE_LISTENER_TTL_MS,
    create_office_relay,
    office_action_allowed,
    office_rack,
    office_session,
    office_worktree_for,
    parse_office_message,
    type OfficeChannel,
    type OfficeMessage,
    type OfficeSource,
} from '../../src/engine/terminal/agent-office-link.ts';
import { build_office } from '../../src/engine/terminal/agent-office-logic.ts';
import type { RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import type { WorktreeRow } from '../../src/engine/terminal/rack-view.ts';

const LINEAGE = { id: '11111111-1111-4111-8111-111111111111' };

function row(session: string, over: Partial<RackSession> = {}): RackSession {
    return { session, state: 'busy', live: true, ...over };
}

/** A managed worktree whose button reads PR: commits ahead, nothing landed. */
function worktree(session: string, over: Partial<WorktreeRow> = {}): WorktreeRow {
    return { branch: `feat/${session}`, path: `/worktrees/demo/${session}`, session, dirty: false, ahead: 2, behind: 0,
        pr_status: null, pr_url: null, pr_error: null, ...over };
}
const LANDED: Partial<WorktreeRow> = { landed: true, landing: { how: 'pr', pr: 7 }, pr_status: 'open', pr_url: 'https://github.com/demo/demo/pull/7' };

describe('the projection a rack relays', () => {
    test('carries only what the office draws', () => {
        const projected = office_session(
            row('a', {
                auto_name: 'Fix it',
                model_label: 'opus',
                repo: 'my-app',
                cwd: '/code/my-app',
                lineage: LINEAGE,
                activity: { zone: 'codebase', tool: 'Read', detail: 'x.ts' },
                message: 'secret-ish prompt text',
                context_left: 40,
                fresh: true,
            }),
        );
        assert.deepEqual(projected, {
            session: 'a',
            state: 'busy',
            live: true,
            status_emoji: '🤔',
            auto_name: 'Fix it',
            model_label: 'opus',
            context_left: 40,
            repo: 'my-app',
            cwd: '/code/my-app',
            lineage: LINEAGE,
            activity: { zone: 'codebase', tool: 'Read', detail: 'x.ts' },
        });
    });

    test('a detached rack relays itself frozen', () => {
        const rack = office_rack({ sessions: [row('a')], focused: 'a', reachable: true, attached: false });
        assert.equal(rack.frozen, true);
        assert.equal(office_rack({ sessions: [], focused: null, reachable: false }).frozen, false);
    });

    test('the wall monitor\'s accounts and machines ride the relay, and a rack with neither sends no board', () => {
        const source: OfficeSource = {
            sessions: [row('a')], focused: null, reachable: true,
            profiles: parse_profiles([
                { id: 'one', label: 'ONE', kind: 'claude', usage: { seven_day: { used_percentage: 41 }, age_seconds: 1 } },
                { id: 'two', label: 'TWO', kind: 'claude', standing: { state: 'out', error: 'authentication_failed' } },
                { id: 'shell', label: 'SHELL', kind: 'shell' },
            ])!,
        };
        const message = parse_office_message(JSON.parse(JSON.stringify({ type: 'office:rack', rack: office_rack(source) })));
        assert.ok(message?.type === 'office:rack');
        assert.deepEqual(message.rack.board, {
            accounts: [
                { label: 'ONE', five_hour: null, week: 41, out: null },
                { label: 'TWO', five_hour: null, week: null, out: 'signed out' },
            ],
            machines: [],
        });
        assert.equal('board' in office_rack({ sessions: [row('a')], focused: null, reachable: true }), false);
    });
});

describe('the worktree behind a session', () => {
    function relayed(source: OfficeSource) {
        const message = parse_office_message(JSON.parse(JSON.stringify({ type: 'office:rack', rack: office_rack(source) })));
        assert.ok(message?.type === 'office:rack');
        return message.rack;
    }

    test('rides the relay as the fields its button reads, joined by name and by cwd', () => {
        const rack = relayed({
            sessions: [row('wt-a'), row('vc-2', { cwd: '/worktrees/demo/b/ui' }), row('plain', { cwd: '/code/demo' })],
            worktrees: [worktree('wt-a'), worktree('b', LANDED)],
            focused: null,
            reachable: true,
        });
        assert.deepEqual(rack.worktrees, {
            'wt-a': { branch: 'feat/wt-a', dirty: false, ahead: 2, pr_status: null, pr_url: null, pr_error: null },
            'vc-2': { branch: 'feat/b', dirty: false, ahead: 2, pr_status: 'open', pr_url: 'https://github.com/demo/demo/pull/7',
                pr_error: null, landed: true, landing: { how: 'pr', pr: 7 } },
        });
    });

    test('is absent while the tree is still being created, for a dead session, and with no plugin', () => {
        assert.equal(relayed({ sessions: [row('a')], focused: null, reachable: true }).worktrees, undefined);
        assert.equal(relayed({ sessions: [row('a')], worktrees: [worktree('a', { create_status: 'creating' })],
            focused: null, reachable: true }).worktrees, undefined);
        assert.equal(relayed({ sessions: [row('a', { live: false })], worktrees: [worktree('a')],
            focused: null, reachable: true }).worktrees, undefined);
    });

    test('a malformed row is dropped, a row for no listed session is never read, and only a web link survives', () => {
        const parsed = parse_office_message({ type: 'office:rack', rack: {
            sessions: [row('a'), row('b'), row('c')],
            worktrees: {
                a: { ...worktree('a'), ahead: -1 },
                b: { ...worktree('b'), pr_status: 'open', pr_url: 'javascript:alert(1)' },
                c: { ...worktree('c'), pr_status: 'open', pr_url: 'https://github.com/demo/demo/pull/9' },
                ghost: worktree('ghost'),
            },
        } });
        assert.ok(parsed?.type === 'office:rack');
        assert.deepEqual(Object.keys(parsed.rack.worktrees ?? {}).sort(), ['b', 'c']);
        assert.equal(office_worktree_for(parsed.rack, 'b')?.pr_url, null);
        assert.equal(office_worktree_for(parsed.rack, 'c')?.pr_url, 'https://github.com/demo/demo/pull/9');
        assert.equal(office_worktree_for(parsed.rack, 'ghost'), undefined);
        for (const junk of [[], 'x', 4, null]) {
            const message = parse_office_message({ type: 'office:rack', rack: { sessions: [row('a')], worktrees: junk } });
            assert.ok(message?.type === 'office:rack');
            assert.equal(message.rack.worktrees, undefined);
        }
    });

    test('a session named like an object\'s own machinery is one more key, and never a way to its prototype', () => {
        const rack = relayed({ sessions: [row('__proto__'), row('constructor'), row('branch')],
            worktrees: [worktree('__proto__')], focused: null, reachable: true });
        assert.equal(office_worktree_for(rack, '__proto__')?.branch, 'feat/__proto__');
        assert.equal(office_worktree_for(rack, 'constructor'), undefined);
        assert.equal(office_worktree_for(rack, 'branch'), undefined);
        assert.equal(Object.getPrototypeOf(rack.worktrees), Object.prototype);
    });
});

describe('office_action_allowed', () => {
    const source = (over: Partial<OfficeSource> = {}): OfficeSource => ({
        sessions: [row('a'), row('b'), row('gone', { live: false })],
        worktrees: [worktree('a'), worktree('b', { ...LANDED, pr_status: null, pr_url: null })],
        focused: null,
        reachable: true,
        ...over,
    });

    test('a live session can be closed; a dead or unknown one, or a rack that cannot act, cannot', () => {
        assert.equal(office_action_allowed(source(), 'kill', 'a'), true);
        assert.equal(office_action_allowed(source(), 'kill', 'gone'), false);
        assert.equal(office_action_allowed(source(), 'kill', 'nobody'), false);
        assert.equal(office_action_allowed(source({ reachable: false }), 'kill', 'a'), false);
        assert.equal(office_action_allowed(source({ attached: false }), 'kill', 'a'), false);
        assert.equal(office_action_allowed(null, 'kill', 'a'), false);
    });

    test('pr and cleanup pass only as the verb the row\'s button would send now', () => {
        assert.equal(office_action_allowed(source(), 'pr', 'a'), true);
        assert.equal(office_action_allowed(source(), 'cleanup', 'a'), false);
        assert.equal(office_action_allowed(source(), 'cleanup', 'b'), true);
        assert.equal(office_action_allowed(source(), 'pr', 'b'), false);
        // No worktree behind the session: neither.
        assert.equal(office_action_allowed(source({ worktrees: [] }), 'pr', 'a'), false);
        // A job in flight, or a PR already open (its button only opens a link), takes no second verb.
        assert.equal(office_action_allowed(source({ worktrees: [worktree('a', { pr_status: 'opening' })] }), 'pr', 'a'), false);
        assert.equal(office_action_allowed(source({ worktrees: [worktree('a', { pr_status: 'open', pr_url: 'https://x.test/1' })] }), 'pr', 'a'), false);
        assert.equal(office_action_allowed(source({ worktrees: [worktree('b', { ...LANDED, cleanup_status: 'running' })] }), 'cleanup', 'b'), false);
        assert.equal(office_action_allowed(source({ worktrees: [worktree('a', { create_status: 'creating' })] }), 'pr', 'a'), false);
    });
});

describe('parse_office_message', () => {
    test('carries only valid context percentages into the map', () => {
        for (const value of [0, 40, 100, -1, 101, NaN, '40']) {
            const message = parse_office_message({ type: 'office:rack', rack: {
                sessions: [{ ...row('a'), context_left: value }],
            } });
            assert.ok(message?.type === 'office:rack');
            assert.equal(message.rack.sessions[0].context_left,
                typeof value === 'number' && value >= 0 && value <= 100 ? value : undefined);
        }
    });
    test('rejects junk', () => {
        for (const raw of [null, 7, 'office:hello', {}, { type: 'nope' }, { type: 'office:select' }, { type: 'office:rack' }]) {
            assert.equal(parse_office_message(raw), null, JSON.stringify(raw));
        }
        const huge = { type: 'office:rack', rack: { sessions: Array.from({ length: 501 }, (_, i) => row(`s${i}`)) } };
        assert.equal(parse_office_message(huge), null);
    });

    test('a round trip survives intact', () => {
        const message: OfficeMessage = {
            type: 'office:rack',
            rack: office_rack({
                sessions: [row('a', { lineage: LINEAGE, activity: { zone: 'web', tool: null, detail: null } })],
                focused: 'a',
                reachable: true,
            }),
        };
        assert.deepEqual(parse_office_message(JSON.parse(JSON.stringify(message))), message);
        assert.deepEqual(parse_office_message({ type: 'office:select', session: 'a' }), { type: 'office:select', session: 'a' });
        // Legacy (step 6.8): a rack page one release behind relays the same rack as `office:fleet`.
        assert.deepEqual(parse_office_message({ type: 'office:fleet', fleet: message.rack }), message);
        assert.equal(parse_office_message({ type: 'office:fleet', rack: message.rack }), null);
    });

    test('re-validates every row: bad state is unknown, bad lineage and nameless rows are dropped', () => {
        const parsed = parse_office_message({
            type: 'office:rack',
            rack: {
                sessions: [
                    { session: 'a', state: 'exploding', live: true, lineage: { id: 'not-a-uuid' }, activity: { tool: 'Bash' } },
                    { state: 'busy' },
                    'nope',
                ],
                focused: 42,
            },
        });
        assert.ok(parsed && parsed.type === 'office:rack');
        assert.deepEqual(parsed.rack, {
            sessions: [{ session: 'a', state: 'unknown', live: true }],
            focused: null,
            reachable: true,
            frozen: false,
        });
    });
});

describe('the rack-side relay', () => {
    function harness() {
        const posted: OfficeMessage[] = [];
        let closed = false;
        const channel: OfficeChannel = {
            postMessage: (message) => posted.push(message),
            close: () => {
                closed = true;
            },
            onmessage: null,
        };
        let clock = 1_000_000;
        const selected: string[] = [];
        const acted: string[] = [];
        const relay = create_office_relay({
            on_select: (s) => selected.push(s),
            on_action: (action, s) => acted.push(`${action} ${s}`),
            channel,
            now: () => clock,
        });
        const hear = (data: unknown): void => channel.onmessage?.({ data });
        return {
            relay,
            posted,
            selected,
            acted,
            hear,
            advance: (ms: number) => {
                clock += ms;
            },
            closed: () => closed,
            channel,
        };
    }
    const payload: OfficeSource = { sessions: [row('a')], focused: null, reachable: true };

    test('relays nothing until an office says hello, then answers at once', () => {
        const h = harness();
        h.relay.push(payload);
        assert.equal(h.posted.length, 0);
        h.hear({ type: 'office:hello' });
        assert.equal(h.posted.length, 1);
        assert.equal(h.posted[0].type, 'office:rack');
    });

    test('relays each payload while the office beats, and stops once it goes quiet', () => {
        const h = harness();
        h.hear({ type: 'office:hello' }); // nothing to answer with yet
        assert.equal(h.posted.length, 0);
        h.relay.push(payload);
        assert.equal(h.posted.length, 1);
        h.advance(OFFICE_LISTENER_TTL_MS + 1);
        h.relay.push(payload);
        assert.equal(h.posted.length, 1);
    });

    test('a click in the office comes back as a select; junk is ignored', () => {
        const h = harness();
        h.hear({ type: 'office:select', session: 'vc-1' });
        h.hear({ type: 'office:select', session: 42 });
        h.hear({ type: 'office:rack', rack: { sessions: [] } }); // another rack's relay
        assert.deepEqual(h.selected, ['vc-1']);
    });

    test('a menu verb comes back only when the rack it holds now allows it', () => {
        const h = harness();
        // Before any payload there is no rack to check against.
        h.hear({ type: 'office:action', action: 'kill', session: 'a' });
        assert.deepEqual(h.acted, []);
        h.relay.push({ ...payload, worktrees: [worktree('a')] });
        h.hear({ type: 'office:action', action: 'kill', session: 'a' });
        h.hear({ type: 'office:action', action: 'pr', session: 'a' });
        h.hear({ type: 'office:action', action: 'cleanup', session: 'a' }); // the button reads PR
        h.hear({ type: 'office:action', action: 'kill', session: 'nobody' });
        h.hear({ type: 'office:action', action: 'rename', session: 'a' }); // not a menu verb
        h.hear({ type: 'office:action', action: 'kill', session: 42 });
        assert.deepEqual(h.acted, ['kill a', 'pr a']);
        // The branch landed since the menu was armed: a late `pr` does nothing, and never cleans.
        h.relay.push({ ...payload, worktrees: [worktree('a', { ...LANDED, pr_status: null, pr_url: null })] });
        h.hear({ type: 'office:action', action: 'pr', session: 'a' });
        assert.deepEqual(h.acted, ['kill a', 'pr a']);
        h.relay.push({ ...payload, attached: false });
        h.hear({ type: 'office:action', action: 'kill', session: 'a' });
        assert.deepEqual(h.acted, ['kill a', 'pr a']);
    });

    test('dispose closes the channel', () => {
        const h = harness();
        h.relay.dispose();
        assert.equal(h.closed(), true);
        assert.equal(h.channel.onmessage, null);
    });
});


test('the office carries the exact rack status mark without its verdict metadata', () => {
    const cases: [Partial<RackSession>, string][] = [
        [{ state: 'busy' }, '🤔'], [{ state: 'attention' }, '🙋'], [{ state: 'background' }, '⏳'],
        [{ state: 'done' }, '💬'], [{ state: 'free' }, '○'], [{ state: 'free', fresh: true }, '🆕'],
        [{ state: 'done', work_done: { p: 0.1 } }, '✅'], [{ state: 'free', work_done: { p: 0.1 } }, '☑️'],
        [{ state: 'done', work_done: { p: 0.1, answer: true } }, '📖'],
        [{ state: 'done', stop_failure: { error: 'rate_limit', limit: null, resets_at: 200, message: null } }, '🪫'],
        [{ state: 'done', stop_failure: { error: 'rate_limit', limit: null, resets_at: 50, message: null } }, '🔋'],
        [{ state: 'free', stop_failure: { error: 'overloaded', limit: null, resets_at: null, message: null } }, '🛑'],
    ];
    for (const [over, emoji] of cases) {
        const projected = office_session(row('a', over), 100);
        assert.equal(projected.status_emoji, emoji);
        assert.equal(projected.work_done, undefined);
        assert.equal(projected.stop_failure, undefined);
        const message = parse_office_message(JSON.parse(JSON.stringify({ type: 'office:rack', rack: { sessions: [projected] } })));
        assert.ok(message?.type === 'office:rack');
        assert.equal(build_office(message.rack.sessions).rooms[0].desks[0].agent.emoji, emoji);
    }
    for (const status_emoji of [undefined, null, 4, [], '<script>', '🍃', '🤔'.repeat(400)]) {
        const message = parse_office_message({ type: 'office:rack', rack: { sessions: [{ ...row('a'), status_emoji }] } });
        assert.ok(message?.type === 'office:rack');
        assert.equal(message.rack.sessions[0].status_emoji, undefined);
        assert.equal(build_office(message.rack.sessions).rooms[0].desks[0].agent.emoji, '🤔');
    }
});
