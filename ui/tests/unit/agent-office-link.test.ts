import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    OFFICE_LISTENER_TTL_MS,
    create_office_relay,
    office_rack,
    office_session,
    parse_office_message,
    type OfficeChannel,
    type OfficeMessage,
    type OfficeSource,
} from '../../src/engine/terminal/agent-office-link.ts';
import type { RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';

const LINEAGE = { id: '11111111-1111-4111-8111-111111111111' };

function row(session: string, over: Partial<RackSession> = {}): RackSession {
    return { session, state: 'busy', live: true, ...over };
}

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
        const relay = create_office_relay({ on_select: (s) => selected.push(s), channel, now: () => clock });
        const hear = (data: unknown): void => channel.onmessage?.({ data });
        return {
            relay,
            posted,
            selected,
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

    test('dispose closes the channel', () => {
        const h = harness();
        h.relay.dispose();
        assert.equal(h.closed(), true);
        assert.equal(h.channel.onmessage, null);
    });
});
