import test from 'node:test';
import assert from 'node:assert/strict';
import { with_launch_directories } from '../../src/agent-rack/launch-directories-client.ts';
import type { RackPayload, RackTransport } from '../../src/agent-rack/rack-transport.ts';

const state = { directories: [{ label: 'Project', path: '/tmp/project' }], default_path: '/tmp/project', bridge_path: '/tmp/project' };
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
function harness(fetch_impl: typeof fetch) {
    let receive: (payload: RackPayload) => void = () => {};
    const sent: unknown[] = [];
    const base: RackTransport = {
        send: (...args) => { sent.push(args); },
        subscribe: (listener) => { receive = listener; return () => {}; },
    };
    const transport = with_launch_directories(base, (path) => `https://machine.example${path}`, fetch_impl);
    const payloads: RackPayload[] = [];
    transport.subscribe((payload) => payloads.push(payload));
    return { transport, sent, payloads, receive: (payload: Partial<RackPayload>) => receive(payload as RackPayload) };
}

test('directory settings overlay every mount while launches keep their existing owner', async () => {
    const calls: string[] = [];
    const h = harness(async (url) => { calls.push(String(url)); return Response.json(state); });
    h.receive({ reachable: false });
    assert.equal(calls.length, 0);
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    assert.deepEqual(h.payloads.at(-1)?.launch_directories?.state, state);
    const request = JSON.stringify({ profile: 'codex', cwd: state.default_path });
    h.transport.send('new', undefined, request);
    assert.deepEqual(h.sent, [['new', undefined, request]]);
    assert.deepEqual(calls, ['https://machine.example/launch-directories']);
});

test('failed save retains the last confirmed settings and reports the bridge reason', async () => {
    const h = harness(async (_url, init) => init?.method === 'POST'
        ? Response.json({ error: 'Not a directory: /tmp/missing' }, { status: 400 }) : Response.json(state));
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    h.transport.send('directories', undefined, '{}');
    assert.equal(h.payloads.at(-1)?.launch_directories?.saving, true);
    await flush();
    assert.equal(h.payloads.at(-1)?.launch_directories?.saving, false);
    assert.match(h.payloads.at(-1)?.launch_directories?.error ?? '', /Not a directory/);
    assert.deepEqual(h.payloads.at(-1)?.launch_directories?.state, state);
    assert.deepEqual(h.sent, []);
});

test('a read started before Save cannot overwrite its result', async () => {
    let answer_read: (response: Response) => void = () => {};
    const next = { ...state, directories: [{ label: 'Renamed', path: '/tmp/project' }] };
    const h = harness(async (_url, init) => init?.method === 'POST'
        ? Response.json(next) : new Promise((resolve) => { answer_read = resolve; }));
    h.receive({ reachable: true, launch_cwd: true });
    h.transport.send('directories', undefined, JSON.stringify(next));
    await flush();
    answer_read(Response.json(state));
    await flush();
    assert.deepEqual(h.payloads.at(-1)?.launch_directories?.state, next);
    assert.equal(h.payloads.at(-1)?.launch_directories?.saved, true);
});


test('an older parent cannot mistake a directory request for a default-profile launch', async () => {
    const h = harness(async () => Response.json(state));
    h.receive({ reachable: true });
    await flush();
    h.transport.send('new', undefined, JSON.stringify({ profile: 'codex', cwd: state.default_path }));
    assert.deepEqual(h.sent, []);
    assert.match(h.payloads.at(-1)?.launch_directories?.error ?? '', /Reload the parent/);
});
