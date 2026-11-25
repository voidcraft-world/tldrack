import test from 'node:test';
import assert from 'node:assert/strict';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import {
    NEXT_STEP_MODES,
    NEXT_STEP_MODE_NOTES,
    fetch_next_step,
    fetch_notify,
    fetch_rack_metrics,
    parse_next_step,
    parse_notify,
    parse_rack_metrics,
    set_next_step_mode,
    set_notify,
    set_rack_metrics,
} from '../../src/settings/optional-parts-client.ts';

test('GET /next-step is read for its mode and model; the counts beside them are not this page’s', () => {
    // `bridge/tldrack/next_step.py` `status`.
    const body = { mode: 'shadow', modes: ['off', 'shadow', 'live'], model: 'qwen3.5:4b', model_version: 'v3', pending: 0, suggested: 12, log: '/somewhere/next-step.jsonl' };
    assert.deepEqual(parse_next_step(body), { mode: 'shadow', model: 'qwen3.5:4b' });
    assert.deepEqual(parse_next_step({ mode: 'off' }), { mode: 'off' });
    for (const bad of [null, {}, { mode: 'loud' }, { mode: true }]) assert.equal(parse_next_step(bad), null, JSON.stringify(bad));
});

test('every next-step mode has its sentence', () => {
    assert.deepEqual(Object.keys(NEXT_STEP_MODE_NOTES), [...NEXT_STEP_MODES]);
});

test('GET /notify is read for whether a topic is set and whether pushes are on', () => {
    // `bridge/tldrack/notify.py` `NotifyDaemon.state`: no topic in it, ever.
    const body = { configured: true, enabled: false, server: 'https://ntfy.sh', running: true, digest_hours: [9, 18], sent_today: 2 };
    assert.deepEqual(parse_notify(body), { configured: true, enabled: false, server: 'https://ntfy.sh', running: true });
    for (const bad of [null, {}, { configured: 'yes', enabled: true }, { enabled: true }]) assert.equal(parse_notify(bad), null, JSON.stringify(bad));
});

test('GET /rack-metrics is read for the recording switch alone', () => {
    assert.deepEqual(parse_rack_metrics({ version: 1, enabled: true, total: {}, days: [] }), { enabled: true });
    for (const bad of [null, {}, { enabled: 1 }]) assert.equal(parse_rack_metrics(bad), null, JSON.stringify(bad));
});

// ── the wire ─────────────────────────────────────────────────────────────────
const ORIGIN = 'https://machine.example';
interface Call {
    url: string;
    method: string;
    body: unknown;
}

async function with_fetch<T>(stub: (call: Call) => Response, run: () => Promise<T>): Promise<{ result: T; calls: Call[] }> {
    const calls: Call[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const call = { url: String(input), method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : null };
        calls.push(call);
        return stub(call);
    }) as typeof fetch;
    set_bridge_origin(ORIGIN);
    try {
        return { result: await run(), calls };
    } finally {
        globalThis.fetch = real;
    }
}

test('each part is read from its own route, and the next-step read asks for one day of log', async () => {
    const step = await with_fetch(() => Response.json({ mode: 'off', model: 'qwen3.5:4b' }), fetch_next_step);
    assert.deepEqual(step.calls.map((call) => call.url), [`${ORIGIN}/next-step?days=1`]);
    assert.ok(step.result.ok);

    const pushes = await with_fetch(() => Response.json({ configured: false, enabled: false }), fetch_notify);
    assert.deepEqual(pushes.calls.map((call) => call.url), [`${ORIGIN}/notify`]);
    assert.equal(pushes.result.ok && pushes.result.data.configured, false);

    const metrics = await with_fetch(() => Response.json({ enabled: true }), fetch_rack_metrics);
    assert.deepEqual(metrics.calls.map((call) => call.url), [`${ORIGIN}/rack-metrics`]);
    assert.equal(metrics.result.ok && metrics.result.data.enabled, true);
});

test('the mode is posted as {"mode": …} and the answer is the mode the bridge is now in', async () => {
    const { result, calls } = await with_fetch(() => Response.json({ ok: true, mode: 'live', changed: true }), () => set_next_step_mode('live'));
    assert.deepEqual(calls, [{ url: `${ORIGIN}/next-step/settings`, method: 'POST', body: { mode: 'live' } }]);
    assert.deepEqual(result, { ok: true, data: { mode: 'live' } });
});

test('a bridge with no suggester refuses in its own words', async () => {
    const { result } = await with_fetch(() => Response.json({ error: 'this bridge has no next-step suggester' }, { status: 503 }), fetch_next_step);
    assert.deepEqual([result.ok, !result.ok && result.kind, !result.ok && result.error], [false, 'refused', 'this bridge has no next-step suggester']);
});

test('pushes are posted as {"enabled": …} and the answer is the state after it', async () => {
    const { result, calls } = await with_fetch(() => Response.json({ configured: true, enabled: true, server: 'https://ntfy.sh', running: true }), () => set_notify(true));
    assert.deepEqual(calls, [{ url: `${ORIGIN}/notify`, method: 'POST', body: { enabled: true } }]);
    assert.equal(result.ok && result.data.enabled, true);
});

test('a 409 for "no topic" is the state to draw: the switch stays off and the row shows the command', async () => {
    const body = { error: 'no ntfy topic configured', configured: false, enabled: true, server: 'https://ntfy.sh', running: false };
    const { result } = await with_fetch(() => Response.json(body, { status: 409 }), () => set_notify(true));
    assert.equal(result.ok && result.data.configured, false);
    // Any other refusal stays a refusal, a 409 that still claims a topic included.
    const odd = await with_fetch(() => Response.json({ ...body, configured: true }, { status: 409 }), () => set_notify(true));
    assert.equal(!odd.result.ok && odd.result.kind, 'refused');
    const bad = await with_fetch(() => Response.json({ error: 'body must be {"enabled": true|false}' }, { status: 400 }), () => set_notify(true));
    assert.equal(!bad.result.ok && bad.result.error, 'body must be {"enabled": true|false}');
});

test('pausing the metrics posts {"enabled": false}, and a 200 is the new state', async () => {
    const { result, calls } = await with_fetch(() => Response.json({ ok: true, message: 'Recording paused' }), () => set_rack_metrics(false));
    assert.deepEqual(calls, [{ url: `${ORIGIN}/rack-metrics/settings`, method: 'POST', body: { enabled: false } }]);
    assert.deepEqual(result, { ok: true, data: { enabled: false } });
    const refused = await with_fetch(() => Response.json({ error: 'Rack metrics unavailable; restart the bridge' }, { status: 503 }), () => set_rack_metrics(false));
    assert.equal(!refused.result.ok && refused.result.error, 'Rack metrics unavailable; restart the bridge');
});
