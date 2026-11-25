import test from 'node:test';
import assert from 'node:assert/strict';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import {
    changes_needed,
    default_changes,
    fetch_features,
    group_features,
    lite_changes,
    parse_features,
    set_features,
    waits_for_restart,
    type Feature,
} from '../../src/settings/features-client.ts';

/** One row as `GET /features` sends it (`bridge/tldrack/features.py` `view`). */
function row(over: Partial<Feature> = {}): Feature {
    return {
        key: 'code_watch',
        group: 'bridge',
        title: 'Code watch',
        does: 'Notices when the source changed.',
        cost: 'Hashes the source every 5 s.',
        without: 'The bridge no longer knows its code is stale.',
        on: true,
        applies: 'now',
        pending: false,
        lite: true,
        ...over,
    };
}
const laya = row({ key: 'plugin:laya', group: 'plugins', title: 'laya', applies: 'restart', lite: false });

test('a feature list is read row by row, with the bridge’s own error beside it', () => {
    const view = parse_features({ features: [row(), laya], error: null });
    assert.deepEqual(view, { features: [row(), laya], error: null, unreadable: 0 });
    assert.equal(parse_features({ features: [], error: 'features.json could not be read' })?.error, 'features.json could not be read');
    // `error` may be absent on a bridge that has nothing to say.
    assert.equal(parse_features({ features: [] })?.error, null);
});

test('one row this page cannot read costs that row, and is counted', () => {
    const view = parse_features({
        features: [row(), { ...row({ key: 'later' }), applies: 'someday' }, { key: 'bare' }, 'nonsense', row({ key: 'ledger_ingest' })],
        error: null,
    });
    assert.deepEqual(view?.features.map((feature) => feature.key), ['code_watch', 'ledger_ingest']);
    assert.equal(view?.unreadable, 3);
});

test('a repeated key keeps its first row: two switches must never share one answer', () => {
    const view = parse_features({ features: [row({ on: true }), row({ on: false })], error: null });
    assert.equal(view?.features.length, 1);
    assert.equal(view?.features[0]?.on, true);
    assert.equal(view?.unreadable, 1);
});

test('a body that is not a feature list is no view at all', () => {
    for (const body of [null, 'features', [], {}, { features: 'all' }, { features: [], error: 7 }]) {
        assert.equal(parse_features(body), null, JSON.stringify(body));
    }
});

test('rows are grouped in page order, and a group the bridge adds later is drawn after the known two', () => {
    const groups = group_features([laya, row({ key: 'x', group: 'experiments' }), row()]);
    assert.deepEqual(groups.map((group) => group.id), ['bridge', 'plugins', 'experiments']);
    assert.deepEqual(groups.map((group) => group.features.map((feature) => feature.key)), [['code_watch'], ['plugin:laya'], ['x']]);
    assert.equal(groups[2]?.title, 'experiments');
    // No rows, no card.
    assert.deepEqual(group_features([laya]).map((group) => group.id), ['plugins']);
});

test('only a restart switch whose answer is not live yet waits for a restart', () => {
    assert.equal(waits_for_restart(row({ applies: 'restart', pending: true })), true);
    assert.equal(waits_for_restart(row({ applies: 'restart', pending: false })), false);
    assert.equal(waits_for_restart(row({ applies: 'now', pending: true })), false);
});

test('LITE turns off exactly the switches the bridge marks lite; DEFAULTS turns every switch on', () => {
    const features = [row(), row({ key: 'ledger_ingest', on: false }), laya];
    assert.deepEqual(lite_changes(features), { code_watch: false, ledger_ingest: false });
    assert.deepEqual(default_changes(features), { code_watch: true, ledger_ingest: true, 'plugin:laya': true });
    assert.equal(changes_needed(features, lite_changes(features)), 1);
    assert.equal(changes_needed(features, default_changes(features)), 1);
    assert.equal(changes_needed([row({ on: false })], lite_changes([row({ on: false })])), 0);
});

// ── the wire ─────────────────────────────────────────────────────────────────
// A non-loopback origin, so the request skips the local-access gate and reaches the stubbed `fetch`.
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

test('GET /features answers the view; POST /features sends {"set": …} and answers the view after it', async () => {
    const calls: Array<{ url: string; method: string; body: unknown }> = [];
    const stub = async (url: string, init?: RequestInit): Promise<Response> => {
        calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : null });
        return Response.json({ features: [row({ on: init?.method !== 'POST' })], error: null });
    };
    const read = await with_fetch(stub, fetch_features);
    assert.ok(read.ok);
    assert.equal(read.data.features[0]?.on, true);
    const written = await with_fetch(stub, () => set_features({ code_watch: false }));
    assert.ok(written.ok);
    assert.equal(written.data.features[0]?.on, false);
    assert.deepEqual(calls, [
        { url: `${ORIGIN}/features`, method: 'GET', body: null },
        { url: `${ORIGIN}/features`, method: 'POST', body: { set: { code_watch: false } } },
    ]);
});

test('a refusal carries the bridge’s sentence, an older bridge is "missing", silence is "unreachable"', async () => {
    const refused = await with_fetch(async () => Response.json({ error: 'unknown feature: nope' }, { status: 400 }), () => set_features({ nope: true }));
    assert.deepEqual([refused.ok, !refused.ok && refused.kind, !refused.ok && refused.error], [false, 'refused', 'unknown feature: nope']);

    const missing = await with_fetch(async () => Response.json({ error: 'not found' }, { status: 404 }), fetch_features);
    assert.equal(!missing.ok && missing.kind, 'missing');

    const silent = await with_fetch(async () => {
        throw new TypeError('fetch failed');
    }, fetch_features);
    assert.equal(!silent.ok && silent.kind, 'unreachable');
    assert.match(!silent.ok ? silent.error : '', /not reachable on machine\.example\/features/);
    assert.equal(!silent.ok && silent.status, 0);
});

test('a 200 that is not a feature list is "invalid", never data', async () => {
    const result = await with_fetch(async () => Response.json({ sections: [] }), fetch_features);
    assert.equal(!result.ok && result.kind, 'invalid');
    assert.match(!result.ok ? result.error : '', /a feature list this page cannot read/);
});
