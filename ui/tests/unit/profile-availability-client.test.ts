import test from 'node:test';
import assert from 'node:assert/strict';
import { with_profile_availability } from '../../src/agent-rack/profile-availability-client.ts';
import type { RackPayload, RackTransport } from '../../src/agent-rack/rack-transport.ts';

const state = { paused: ['claude-a'], error: null };
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
function harness(fetch_impl: typeof fetch) {
    let receive: (payload: RackPayload) => void = () => {};
    const sent: unknown[] = [];
    const base: RackTransport = {
        send: (...args) => { sent.push(args); },
        subscribe: (listener) => { receive = listener; return () => {}; },
    };
    const transport = with_profile_availability(base, (path) => `https://machine.example${path}`, fetch_impl);
    const payloads: RackPayload[] = [];
    transport.subscribe((payload) => payloads.push(payload));
    return { transport, sent, payloads, receive: (payload: Partial<RackPayload>) => receive(payload as RackPayload) };
}

test('profile settings overlay every mount while launches keep their existing owner', async () => {
    const calls: string[] = [];
    const h = harness(async (url) => { calls.push(String(url)); return Response.json(state); });
    h.receive({ reachable: false });
    assert.equal(calls.length, 0);
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    assert.deepEqual(h.payloads.at(-1)?.profile_availability?.state, state);
    const request = JSON.stringify({ profile: 'codex' });
    h.transport.send('new', undefined, request);
    assert.deepEqual(h.sent, [['new', undefined, request]]);
    assert.deepEqual(calls, ['https://machine.example/profile-availability']);
});

test('failed save retains the last confirmed settings and reports the bridge reason', async () => {
    const h = harness(async (_url, init) => init?.method === 'POST'
        ? Response.json({ error: 'Could not save profile availability' }, { status: 400 }) : Response.json(state));
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    h.transport.send('profile_pause', undefined, '{}');
    assert.equal(h.payloads.at(-1)?.profile_availability?.saving, true);
    await flush();
    assert.equal(h.payloads.at(-1)?.profile_availability?.saving, false);
    assert.match(h.payloads.at(-1)?.profile_availability?.error ?? '', /Could not save/);
    assert.deepEqual(h.payloads.at(-1)?.profile_availability?.state, state);
    assert.deepEqual(h.sent, []);
});

test('a read started before Save cannot overwrite its result', async () => {
    let answer_read: (response: Response) => void = () => {};
    const next = { ...state, paused: [] };
    const h = harness(async (_url, init) => init?.method === 'POST'
        ? Response.json(next) : new Promise((resolve) => { answer_read = resolve; }));
    h.receive({ reachable: true, launch_cwd: true });
    h.transport.send('profile_pause', undefined, JSON.stringify(next));
    await flush();
    answer_read(Response.json(state));
    await flush();
    assert.deepEqual(h.payloads.at(-1)?.profile_availability?.state, next);
    assert.equal(h.payloads.at(-1)?.profile_availability?.saved, true);
});


test('an older bridge reports the missing capability and invalid responses are rejected', async () => {
    for (const response of [new Response('', { status: 404 }), Response.json({ paused: 'all' })]) {
        const h = harness(async () => response);
        h.receive({ reachable: true });
        await flush();
        assert.equal(h.payloads.at(-1)?.profile_availability?.state, null);
        assert.ok(h.payloads.at(-1)?.profile_availability?.error);
    }
});
