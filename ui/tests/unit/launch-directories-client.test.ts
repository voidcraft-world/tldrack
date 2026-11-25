import test from 'node:test';
import assert from 'node:assert/strict';
import { with_launch_directories } from '../../src/agent-rack/launch-directories-client.ts';
import type { PageReturn } from '../../src/agent-rack/page-return.ts';
import type { RackPayload, RackTransport } from '../../src/agent-rack/rack-transport.ts';

const state = { directories: [{ label: 'Project', path: '/tmp/project' }], default_path: '/tmp/project', bridge_path: '/tmp/project' };
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
function harness(fetch_impl: typeof fetch) {
    let receive: (payload: RackPayload) => void = () => {};
    let come_back: (how: PageReturn) => void = () => {};
    const sent: unknown[] = [];
    const clock = { now: 0, watching: false };
    const base: RackTransport = {
        send: (...args) => { sent.push(args); },
        subscribe: (listener) => { receive = listener; return () => {}; },
    };
    const transport = with_launch_directories(base, (path) => `https://machine.example${path}`, fetch_impl, {
        watch_return: (on_return) => {
            come_back = on_return;
            clock.watching = true;
            return () => { clock.watching = false; };
        },
        now: () => clock.now,
    });
    const payloads: RackPayload[] = [];
    const unsubscribe = transport.subscribe((payload) => payloads.push(payload));
    return {
        transport, sent, payloads, clock, unsubscribe,
        receive: (payload: Partial<RackPayload>) => receive(payload as RackPayload),
        come_back: (how: PageReturn) => come_back(how),
    };
}

/** A bridge that answers the list and counts how often it was asked. */
function counting(answer: () => Response = () => Response.json(state)) {
    const reads = { count: 0 };
    const fetch_impl = (async (_url: unknown, init?: RequestInit) => {
        if (init?.method !== 'POST') reads.count += 1;
        return init?.method === 'POST' ? Response.json(state) : answer();
    }) as typeof fetch;
    return { reads, fetch_impl };
}

test('the list is read once when the bridge arrives, and no payload after that asks again', async () => {
    const { reads, fetch_impl } = counting();
    const h = harness(fetch_impl);
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    assert.equal(reads.count, 1);
    // A rack page gets a payload every few seconds for as long as it is open. Each used to be a GET.
    for (let i = 0; i < 20; i += 1) {
        h.clock.now += 3000;
        h.receive({ reachable: true, launch_cwd: true });
    }
    await flush();
    assert.equal(reads.count, 1);
});

test('a bridge that went away and came back is read again: it may be a restarted one', async () => {
    const { reads, fetch_impl } = counting();
    const h = harness(fetch_impl);
    h.receive({ reachable: true });
    await flush();
    h.receive({ reachable: false });
    h.receive({ reachable: true });
    await flush();
    assert.equal(reads.count, 2);
});

test('coming back to the page reads: a tab shown again always, a window focus at most each five seconds', async () => {
    const { reads, fetch_impl } = counting();
    const h = harness(fetch_impl);
    h.come_back('visible');
    await flush();
    assert.equal(reads.count, 0, 'no bridge yet: its arrival will read');
    h.receive({ reachable: true });
    await flush();
    // Saved on the settings page in another tab a second ago; the operator switches back.
    h.clock.now += 1000;
    h.come_back('visible');
    await flush();
    assert.equal(reads.count, 2);
    // A click out of the terminal's frame is a window focus too, many times a minute.
    h.clock.now += 1000;
    h.come_back('focus');
    h.come_back('focus');
    await flush();
    assert.equal(reads.count, 2);
    h.clock.now += 5000;
    h.come_back('focus');
    await flush();
    assert.equal(reads.count, 3);
});

test('a read that failed is owed: payloads retry it at the old pace until one lands, then stop', async () => {
    let fail = true;
    const { reads, fetch_impl } = counting(() => (fail ? Response.json({ error: 'busy' }, { status: 500 }) : Response.json(state)));
    const h = harness(fetch_impl);
    h.receive({ reachable: true });
    await flush();
    assert.match(h.payloads.at(-1)?.launch_directories?.error ?? '', /busy/);
    h.clock.now += 3000;
    h.receive({ reachable: true });
    await flush();
    assert.equal(reads.count, 1, 'not on every payload');
    h.clock.now += 3000;
    fail = false;
    h.receive({ reachable: true });
    await flush();
    assert.equal(reads.count, 2);
    assert.deepEqual(h.payloads.at(-1)?.launch_directories?.state, state);
    assert.equal(h.payloads.at(-1)?.launch_directories?.error, null);
    h.clock.now += 60_000;
    h.receive({ reachable: true });
    await flush();
    assert.equal(reads.count, 2);
});

test('a save is its own answer, and the last listener leaving stops the return watch', async () => {
    const { reads, fetch_impl } = counting();
    const h = harness(fetch_impl);
    h.receive({ reachable: true, launch_cwd: true });
    await flush();
    h.transport.send('directories', undefined, '{}');
    await flush();
    assert.equal(h.payloads.at(-1)?.launch_directories?.saved, true);
    assert.equal(reads.count, 1, 'nothing is read after a save');
    assert.equal(h.clock.watching, true);
    h.unsubscribe();
    assert.equal(h.clock.watching, false);
});

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
