import test from 'node:test';
import assert from 'node:assert/strict';
import { z } from 'zod';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import { bridge_read, bridge_write, down_after, failure_kind, read_checked, type BridgeResult } from '../../src/settings/bridge-call.ts';

const schema = z.object({ n: z.number() });
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

test('a status says what kind of failure it is', () => {
    assert.equal(failure_kind(0), 'unreachable');
    assert.equal(failure_kind(404), 'missing');
    for (const status of [400, 403, 409, 426, 500, 503]) assert.equal(failure_kind(status), 'refused', String(status));
});

test('a body is data only after its schema took it', () => {
    assert.deepEqual(read_checked(schema, { n: 3, extra: true }, 'a number'), { ok: true, data: { n: 3 } });
    const bad = read_checked(schema, { n: 'three' }, 'a number');
    assert.deepEqual([bad.ok, !bad.ok && bad.kind, !bad.ok && bad.error], [false, 'invalid', 'The bridge returned a number this page cannot read']);
});

test('only an unanswered call says the bridge is down: a refusal came from a bridge that is up', () => {
    const failure = (kind: 'unreachable' | 'missing' | 'refused' | 'invalid'): BridgeResult<number> => ({ ok: false, kind, status: 0, error: 'x', body: null });
    assert.equal(down_after({ ok: true, data: 1 }), null);
    assert.equal(down_after(failure('refused')), null);
    assert.equal(down_after(failure('missing')), null);
    assert.equal(down_after(failure('invalid')), null);
    assert.deepEqual(down_after(failure('unreachable')), failure('unreachable'));
});

test('a read goes to the page’s bridge and comes back checked', async () => {
    const urls: string[] = [];
    const result = await with_fetch(
        async (url) => {
            urls.push(url);
            return Response.json({ n: 4 });
        },
        () => bridge_read('/thing?x=1', schema, 'a number'),
    );
    assert.deepEqual(urls, [`${ORIGIN}/thing?x=1`]);
    assert.deepEqual(result, { ok: true, data: { n: 4 } });
});

test('a failed read keeps the bridge’s sentence, its hint and its body', async () => {
    const result = await with_fetch(async () => Response.json({ error: 'nope', hint: 'try this', code: 'x' }, { status: 503 }), () => bridge_read('/thing', schema, 'a number'));
    assert.deepEqual(result, { ok: false, kind: 'refused', status: 503, error: 'nope', hint: 'try this', body: { error: 'nope', hint: 'try this', code: 'x' } });
});

test('a write posts JSON; a 200 that says ok:false is a refusal; a non-JSON answer does not throw', async () => {
    const sent: unknown[] = [];
    const ok = await with_fetch(
        async (_url, init) => {
            sent.push([init?.method, new Headers(init?.headers).get('content-type'), init?.body]);
            return Response.json({ n: 1 });
        },
        () => bridge_write('/thing', { a: 1 }, schema, 'a number'),
    );
    assert.deepEqual(sent, [['POST', 'application/json', '{"a":1}']]);
    assert.deepEqual(ok, { ok: true, data: { n: 1 } });

    const refused = await with_fetch(async () => Response.json({ ok: false, message: 'not today' }), () => bridge_write('/thing', {}, schema, 'a number'));
    assert.deepEqual([refused.ok, !refused.ok && refused.kind, !refused.ok && refused.error], [false, 'refused', 'not today']);

    const html = await with_fetch(async () => new Response('<h1>502</h1>', { status: 502 }), () => bridge_write('/thing', {}, schema, 'a number'));
    assert.deepEqual([html.ok, !html.ok && html.kind, !html.ok && html.error], [false, 'refused', 'bridge responded 502']);
});

test('no answer at all is "unreachable", from a read and from a write alike', async () => {
    const dead = async (): Promise<Response> => {
        throw new TypeError('fetch failed');
    };
    const read = await with_fetch(dead, () => bridge_read('/thing', schema, 'a number'));
    const write = await with_fetch(dead, () => bridge_write('/thing', {}, schema, 'a number'));
    for (const result of [read, write]) {
        assert.equal(!result.ok && result.kind, 'unreachable');
        assert.equal(!result.ok && result.status, 0);
        assert.match(!result.ok ? result.error : '', /machine\.example\/thing/);
    }
});
