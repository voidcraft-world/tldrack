import test from 'node:test';
import assert from 'node:assert/strict';

import type { LaunchDirectoriesView } from '../../src/agent-rack/launch-directories-client.ts';
import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import type { BridgeFailure } from '../../src/settings/bridge-call.ts';
import { create_directories_feed } from '../../src/settings/directories-feed.ts';

const ORIGIN = 'https://machine.example';
const state = { directories: [{ label: 'Project', path: '/tmp/project' }], default_path: '/tmp/project', bridge_path: '/tmp/project' };
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

function harness(fetch_impl: (url: string, init?: RequestInit) => Promise<Response>) {
    set_bridge_origin(ORIGIN);
    const reports: Array<BridgeFailure | null> = [];
    const calls: Array<{ url: string; method: string }> = [];
    const feed = create_directories_feed(
        (down) => reports.push(down),
        (input, init) => {
            calls.push({ url: String(input), method: init?.method ?? 'GET' });
            return fetch_impl(String(input), init);
        },
        // No page under a test: the client's own "came back to the page" read has nothing to watch.
        { watch_return: () => () => {} },
    );
    const views: LaunchDirectoriesView[] = [];
    const stop = feed.subscribe((view) => views.push(view));
    return { feed, views, reports, calls, stop };
}

test('subscribing reads the directories once, from the page’s bridge', async () => {
    const h = harness(async () => Response.json(state));
    await flush();
    assert.deepEqual(h.calls, [{ url: `${ORIGIN}/launch-directories`, method: 'GET' }]);
    assert.deepEqual(h.views.at(-1)?.state, state);
    assert.deepEqual(h.reports, [null]);
});

test('refresh reads again at once, however soon after the last read', async () => {
    const h = harness(async () => Response.json(state));
    await flush();
    h.feed.refresh();
    await flush();
    h.feed.refresh();
    await flush();
    assert.equal(h.calls.length, 3);
    assert.deepEqual(h.views.at(-1)?.state, state);
});

test('a refresh while a read is out does not send a second one', async () => {
    let answer: (response: Response) => void = () => {};
    const h = harness(() => new Promise((resolve) => (answer = resolve)));
    h.feed.refresh();
    h.feed.refresh();
    assert.equal(h.calls.length, 1);
    answer(Response.json(state));
    await flush();
    assert.deepEqual(h.views.at(-1)?.state, state);
});

test('no answer tells the page the bridge is down, in a sentence that names the route', async () => {
    const h = harness(async () => {
        throw new TypeError('fetch failed');
    });
    await flush();
    const down = h.reports.at(-1);
    assert.equal(down?.kind, 'unreachable');
    assert.match(down?.error ?? '', /not reachable on machine\.example\/launch-directories \(fetch failed\)/);
    assert.equal(down?.hint, 'Start it: tldrack up');
    // The form's own line is the same sentence, not the browser's.
    assert.equal(h.views.at(-1)?.error, down?.error);
});

test('the bridge coming back clears "down" and fills the form on RETRY', async () => {
    let up = false;
    const h = harness(async () => {
        if (!up) throw new TypeError('fetch failed');
        return Response.json(state);
    });
    await flush();
    assert.equal(h.reports.at(-1)?.kind, 'unreachable');
    up = true;
    h.feed.refresh();
    await flush();
    assert.equal(h.reports.at(-1), null);
    assert.deepEqual(h.views.at(-1)?.state, state);
    assert.equal(h.views.at(-1)?.error, null);
});

test('a refusal is an answer: the bridge is up, and the form shows its reason', async () => {
    const h = harness(async (_url, init) => (init?.method === 'POST' ? Response.json({ error: 'Not a directory: /tmp/missing' }, { status: 400 }) : Response.json(state)));
    await flush();
    h.feed.save(JSON.stringify({ directories: [{ label: 'Gone', path: '/tmp/missing' }], default_path: '/tmp/missing' }));
    await flush();
    assert.deepEqual(h.calls.at(-1), { url: `${ORIGIN}/launch-directories`, method: 'POST' });
    assert.equal(h.reports.at(-1), null);
    assert.match(h.views.at(-1)?.error ?? '', /Not a directory/);
    assert.deepEqual(h.views.at(-1)?.state, state);
});

test('a save answers with the saved list', async () => {
    const next = { ...state, directories: [{ label: 'Renamed', path: '/tmp/project' }] };
    const h = harness(async (_url, init) => Response.json(init?.method === 'POST' ? next : state));
    await flush();
    h.feed.save(JSON.stringify(next));
    await flush();
    assert.deepEqual(h.views.at(-1)?.state, next);
    assert.equal(h.views.at(-1)?.saved, true);
});

test('after the last listener leaves, refresh sends nothing', async () => {
    const h = harness(async () => Response.json(state));
    await flush();
    h.stop();
    h.feed.refresh();
    await flush();
    assert.equal(h.calls.length, 1);
});
