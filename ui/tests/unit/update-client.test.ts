import test from 'node:test';
import assert from 'node:assert/strict';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import { MAX_INCOMING, parse_update, post_update, read_update, update_reply } from '../../src/update/update-client.ts';

/** A view as `GET /update` sends it, three commits behind and ready to pull. */
function wire(over: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        supported: true,
        reason: null,
        branch: 'main',
        upstream: 'origin/main',
        head: '1c169f8',
        target: 'abc1234',
        behind: 3,
        ahead: 0,
        dirty: 0,
        incoming: [
            { sha: 'abc1234', subject: 'Merge pull request #40' },
            { sha: 'abc1233', subject: 'Add the thing' },
        ],
        blocked: null,
        page: { id: '9f2c1a', source: 'current' },
        code: 'current',
        available: true,
        phase: 'idle',
        waiting: null,
        error: null,
        check_error: null,
        checked_at: 1790000000.5,
        auto: false,
        checks: true,
        boot_id: '4242-1790000000',
        ...over,
    };
}

test('a full view reads as sent', () => {
    assert.deepEqual(parse_update(wire()), wire());
});

test('every key but `supported` may be absent or null, and reads as the quiet answer', () => {
    const quiet = {
        supported: true,
        reason: null,
        branch: null,
        upstream: null,
        head: null,
        target: null,
        behind: 0,
        ahead: 0,
        dirty: 0,
        incoming: [],
        blocked: null,
        page: { id: null, source: 'unknown' },
        code: 'unknown',
        available: false,
        phase: 'idle',
        waiting: null,
        error: null,
        check_error: null,
        checked_at: null,
        auto: false,
        checks: true,
        boot_id: null,
    };
    assert.deepEqual(parse_update({ supported: true }), quiet);
    const nulls = Object.fromEntries(Object.keys(quiet).map((key) => [key, null]));
    assert.deepEqual(parse_update({ ...nulls, supported: true }), quiet);
    // A bridge that cannot update says why, and sends nothing else worth reading.
    assert.deepEqual(parse_update({ supported: false, reason: 'not a git checkout' }), { ...quiet, supported: false, reason: 'not a git checkout' });
});

test('a body that is not a view is no view at all: a wrong type is never drawn as data', () => {
    const bodies: unknown[] = [
        null,
        'update',
        [],
        {},
        { supported: 'yes' },
        wire({ behind: '3' }),
        wire({ behind: -1 }),
        wire({ behind: 1.5 }),
        wire({ incoming: 'none' }),
        wire({ page: 'current' }),
        wire({ available: 1 }),
        wire({ phase: 7 }),
        wire({ error: { message: 'x' } }),
        wire({ checked_at: 'yesterday' }),
        wire({ auto: 'on' }),
    ];
    for (const body of bodies) assert.equal(parse_update(body), null, JSON.stringify(body));
});

test('the two open fields stay open: an unknown phase is kept, an unknown freshness is `unknown`', () => {
    const view = parse_update(wire({ phase: 'migrating', code: 'newer', page: { id: 'x', source: 'building' } }));
    assert.equal(view?.phase, 'migrating');
    assert.equal(view?.code, 'unknown');
    assert.deepEqual(view?.page, { id: 'x', source: 'unknown' });
    assert.equal(parse_update(wire({ phase: '' }))?.phase, 'idle');
});

test('incoming commits: a row this page cannot read is dropped, and the list never outgrows the card', () => {
    const many = Array.from({ length: 12 }, (_, index) => ({ sha: `sha${index}`, subject: `commit ${index}` }));
    assert.equal(parse_update(wire({ incoming: many }))?.incoming.length, MAX_INCOMING);
    assert.deepEqual(parse_update(wire({ incoming: [{ sha: 'a1' }, 'x', { sha: 'b2', subject: 'kept' }, { sha: '', subject: 'no sha' }] }))?.incoming, [
        { sha: 'b2', subject: 'kept' },
    ]);
});

test('an empty sentence is no sentence', () => {
    const view = parse_update(wire({ blocked: '', error: '   ', waiting: ' busy: creating worktree feat-x ' }));
    assert.equal(view?.blocked, null);
    assert.equal(view?.error, null);
    assert.equal(view?.waiting, 'busy: creating worktree feat-x');
});

test('an answer is one of four shapes: 404 and 503 are "no support here", never a failure', () => {
    const view = parse_update(wire())!;
    assert.deepEqual(update_reply({ ok: true, data: view }), { kind: 'view', view });
    const failed = (kind: 'unreachable' | 'missing' | 'refused' | 'invalid', status: number) => update_reply({ ok: false, kind, status, error: 'why', body: null });
    assert.deepEqual(failed('unreachable', 0), { kind: 'silent', error: 'why' });
    assert.deepEqual(failed('missing', 404), { kind: 'unsupported', status: 404, error: 'why' });
    assert.deepEqual(failed('refused', 503), { kind: 'unsupported', status: 503, error: 'why' });
    for (const status of [400, 403, 409, 500]) assert.deepEqual(failed('refused', status), { kind: 'refused', status, error: 'why' });
    assert.deepEqual(failed('invalid', 200), { kind: 'refused', status: 200, error: 'why' });
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

test('GET /update answers the view; POST /update sends {"action": …} and its 202 is the view too', async () => {
    const calls: Array<{ url: string; method: string; body: unknown }> = [];
    const stub = async (url: string, init?: RequestInit): Promise<Response> => {
        calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : null });
        return Response.json(wire({ phase: init?.method === 'POST' ? 'pulling' : 'idle' }), { status: init?.method === 'POST' ? 202 : 200 });
    };
    const read = await with_fetch(stub, read_update);
    assert.equal(read.kind === 'view' && read.view.phase, 'idle');
    const applied = await with_fetch(stub, () => post_update('apply'));
    assert.equal(applied.kind === 'view' && applied.view.phase, 'pulling');
    await with_fetch(stub, () => post_update('check'));
    assert.deepEqual(calls, [
        { url: `${ORIGIN}/update`, method: 'GET', body: null },
        { url: `${ORIGIN}/update`, method: 'POST', body: { action: 'apply' } },
        { url: `${ORIGIN}/update`, method: 'POST', body: { action: 'check' } },
    ]);
});

test('a 409 carries the bridge’s sentence; an older bridge and one that cannot update are "unsupported"; silence is "silent"', async () => {
    const held = await with_fetch(async () => Response.json({ error: 'busy: creating worktree feat-x' }, { status: 409 }), () => post_update('apply'));
    assert.deepEqual(held, { kind: 'refused', status: 409, error: 'busy: creating worktree feat-x' });

    for (const status of [404, 503]) {
        const read = await with_fetch(async () => Response.json({ error: 'no update here' }, { status }), read_update);
        assert.equal(read.kind, 'unsupported', String(status));
        const pressed = await with_fetch(async () => Response.json({ error: 'no update here' }, { status }), () => post_update('check'));
        assert.equal(pressed.kind, 'unsupported', String(status));
    }

    const silent = await with_fetch(async () => {
        throw new TypeError('fetch failed');
    }, read_update);
    assert.equal(silent.kind, 'silent');
});

test('a 200 that is not a view is refused, never data', async () => {
    const reply = await with_fetch(async () => Response.json({ features: [] }), read_update);
    assert.equal(reply.kind, 'refused');
    assert.match(reply.kind === 'refused' ? reply.error : '', /an update status this page cannot read/);
});
