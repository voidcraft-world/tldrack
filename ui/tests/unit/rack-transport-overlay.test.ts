import test from 'node:test';
import assert from 'node:assert/strict';
import { with_payload_overlay, type RackPayload, type RackTransport } from '../../src/agent-rack/rack-transport.ts';

const BASE: RackPayload = { sessions: [], focused: 'vc-1', max_sessions: 8, summary: 'agents', reachable: true };

function fake_base(): RackTransport & { push: (p: RackPayload) => void; sent: unknown[][] } {
    const subs = new Set<(p: RackPayload) => void>();
    const sent: unknown[][] = [];
    return {
        send: (...args) => void sent.push(args),
        subscribe: (fn) => {
            subs.add(fn);
            return () => subs.delete(fn);
        },
        push: (p) => subs.forEach((fn) => fn(p)),
        sent,
    };
}

test('payloads carry the overlay; an empty overlay adds no keys', () => {
    const base = fake_base();
    let loading: string | null = 'vc-1';
    const transport = with_payload_overlay(base, () => (loading ? { pane_loading: loading } : {}));
    const seen: RackPayload[] = [];
    transport.subscribe((p) => seen.push(p));
    base.push(BASE);
    assert.equal(seen[0].pane_loading, 'vc-1');
    loading = null;
    base.push(BASE);
    assert.equal('pane_loading' in seen[1], false);
});

test('refresh_overlay re-delivers the last payload, and nothing before the first', () => {
    const base = fake_base();
    let loading: string | null = null;
    const transport = with_payload_overlay(base, () => (loading ? { pane_loading: loading } : {}));
    const seen: RackPayload[] = [];
    transport.subscribe((p) => seen.push(p));
    transport.refresh_overlay();
    assert.equal(seen.length, 0);
    base.push(BASE);
    loading = 'vc-2';
    transport.refresh_overlay();
    assert.equal(seen.length, 2);
    assert.equal(seen[1].pane_loading, 'vc-2');
    assert.equal(seen[1].focused, 'vc-1');
});

test('send passes straight through; unsubscribe stops delivery', () => {
    const base = fake_base();
    const transport = with_payload_overlay(base, () => ({}));
    transport.send('rename', 'vc-1', 'web-fix');
    assert.deepEqual(base.sent, [['rename', 'vc-1', 'web-fix']]);
    const seen: RackPayload[] = [];
    const off = transport.subscribe((p) => seen.push(p));
    off();
    base.push(BASE);
    transport.refresh_overlay();
    assert.equal(seen.length, 0);
});
