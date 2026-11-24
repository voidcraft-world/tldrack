import test from 'node:test';
import assert from 'node:assert/strict';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import { LAYA_STATES } from '../../src/decide/laya-local-client.ts';
import {
    fetch_laya_status,
    laya_card_of,
    laya_card_view,
    laya_lifecycle,
    laya_should_poll,
    parse_laya_install_status,
    type LayaInstallStatus,
} from '../../src/settings/laya-install-client.ts';

/** A status as `plugins/laya/laya_bridge/runtime.py` `status` builds it, trimmed to what a state carries. */
function status(over: Record<string, unknown> = {}): LayaInstallStatus {
    const parsed = parse_laya_install_status({
        state: 'stopped',
        installed: true,
        idle_stop_seconds: 900,
        models: ['english'],
        thresholds: {},
        serving: { ready: false, base: 'convaiinnovations/laya', base_only: true, fine_tunes: [], first_fine_tune: {} },
        ...over,
    });
    assert.ok(parsed);
    return parsed;
}

test('a status is read for the fields the card shows, and the rest is left alone', () => {
    const ready = status({ state: 'ready', device: 'mps', model_ids: { english: 'laya-local/english@abc' } });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.device, 'mps');
    assert.equal(ready.installed, true);
    assert.deepEqual(ready.serving?.fine_tunes, []);
    assert.equal('thresholds' in ready, false);
});

test('an older plugin’s status (no installed, no serving) is still a status', () => {
    const old = parse_laya_install_status({ state: 'not_installed', models: [] });
    assert.deepEqual(old, { state: 'not_installed', models: [] });
    // A `serving` or `models` this page cannot read costs the caption, never the card.
    assert.equal(parse_laya_install_status({ state: 'ready', serving: 'yes', models: 'english' })?.state, 'ready');
});

test('a body with no known state is not a status', () => {
    for (const body of [null, [], {}, { state: 'melting' }, { state: 7 }, { error: 'unknown plugin: laya' }]) {
        assert.equal(parse_laya_install_status(body), null, JSON.stringify(body));
    }
});

test('the card polls only while a state ends by itself', () => {
    assert.deepEqual(LAYA_STATES.filter(laya_should_poll), ['installing', 'starting']);
});

test('each state offers the buttons that can move it, and no others', () => {
    const offered = Object.fromEntries(
        LAYA_STATES.map((state) => {
            const view = laya_card_view(status({ state, installed: state !== 'not_installed' }));
            return [state, [view.install ? 'INSTALL' : '', view.start ? 'START' : ''].filter(Boolean).join('+')];
        }),
    );
    assert.deepEqual(offered, {
        not_installed: 'INSTALL',
        installing: '',
        stopped: 'START',
        starting: '',
        ready: '',
        failed: 'INSTALL+START',
    });
});

test('the install’s cost is said before it is pressed, and a failure shows the plugin’s own log tail', () => {
    assert.match(laya_card_view(status({ state: 'not_installed', installed: false })).line, /about 800 MB/);
    assert.match(laya_card_view(status({ state: 'installing' })).line, /about 800 MB/);
    const failed = laya_card_view(status({ state: 'failed', error: 'uv sync exited 1: no space left on device' }));
    assert.equal(failed.error, 'uv sync exited 1: no space left on device');
    assert.equal(failed.tone, 'warn');
    assert.equal(laya_card_view(status({ state: 'stopped' })).error, null);
});

test('ready names where it computes and what it serves', () => {
    assert.match(laya_card_view(status({ state: 'ready', device: 'mps' })).line, /public base model on mps/);
    const tuned = status({ state: 'ready', device: 'cpu', serving: { base_only: false, fine_tunes: ['tldrack-open-work'] } });
    assert.equal(laya_card_view(tuned).line, 'Serving on cpu: tldrack-open-work.');
    assert.equal(laya_card_view(tuned).tone, 'ok');
});

// ── the wire ─────────────────────────────────────────────────────────────────
const ORIGIN = 'https://machine.example';

async function with_fetch<T>(stub: (url: string, init?: RequestInit) => Promise<Response>, run: () => Promise<T>): Promise<T> {
    const real = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => stub(String(input), init)) as typeof fetch;
    set_bridge_origin(ORIGIN);
    try {
        return await run();
    } finally {
        globalThis.fetch = real;
    }
}

test('a plugin the bridge never mounted reads as "off", not as a failure', async () => {
    const result = await with_fetch(async () => Response.json({ error: 'unknown plugin: laya' }, { status: 404 }), fetch_laya_status);
    assert.deepEqual(laya_card_of(result), { kind: 'off' });
});

test('an answer is a status card; silence is a failed card that keeps its kind', async () => {
    const answered = await with_fetch(async () => Response.json({ state: 'ready', models: ['english'], device: 'mps' }), fetch_laya_status);
    const card = laya_card_of(answered);
    assert.equal(card.kind === 'status' && card.status.state, 'ready');

    const silent = await with_fetch(async () => {
        throw new TypeError('fetch failed');
    }, fetch_laya_status);
    const down = laya_card_of(silent);
    assert.equal(down.kind === 'failed' && down.failure.kind, 'unreachable');
});

test('INSTALL posts to the plugin and its 202 status is the card’s next state', async () => {
    const calls: Array<{ url: string; method: string; body: string }> = [];
    const result = await with_fetch(
        async (url, init) => {
            calls.push({ url, method: init?.method ?? 'GET', body: String(init?.body) });
            return Response.json({ state: 'installing', installed: false, models: [] }, { status: 202 });
        },
        () => laya_lifecycle('install'),
    );
    assert.deepEqual(calls, [{ url: `${ORIGIN}/plugin/laya/install`, method: 'POST', body: '{}' }]);
    assert.equal(result.ok && result.data.state, 'installing');
});

test('a refused START carries the plugin’s sentence', async () => {
    const result = await with_fetch(async () => Response.json({ error: 'forbidden', message: 'this origin may not start Laya' }, { status: 403 }), () => laya_lifecycle('start'));
    assert.deepEqual([result.ok, !result.ok && result.kind, !result.ok && result.error], [false, 'refused', 'this origin may not start Laya']);
});
