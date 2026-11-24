import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, it } from 'node:test';

import { enable_plugin_hint } from '../../src/laya-review/bridge-error.ts';
import { get_bridge_json } from '../../src/laya-review/bridge-get.ts';
import { BRIDGE_PREDATES_MESSAGE, post_bridge_json } from '../../src/laya-review/bridge-post.ts';
import {
    LAYA_ROUTE_PREDATES,
    fail_text,
    load_laya_ops,
    load_review_queue,
    load_review_recent,
    post_laya_lab,
    post_laya_restart,
    post_review_example,
    post_review_answer,
    post_review_undo,
} from '../../src/decide/laya-review-client.ts';
import {
    BRIDGE_DOWN_HINT,
    LOCAL_ACCESS_LOCKED_HINT,
    LOCAL_ACCESS_PROMPTING_HINT,
    refresh_local_permission,
    relock_local_access,
    unlock_local_access,
} from '../../src/engine/terminal/local-network-gate.ts';

/**
 * The client never throws and never trusts: every body is checked against `schemas/src/laya-review.ts`, and every
 * failure carries the kind of remedy the deck draws. The bridge is a stubbed `fetch` behind an open local-access gate.
 */

const BASE = 'http://127.0.0.1:7682/plugin/laya';
const KEY = '0123456789abcdef';

const AGREEMENT = { n: 24, kappa: 0.71, agree_rate: 0.88, basis: 'sample' };
const STATS = {
    surface: 'agent_open_work',
    label: 'OPEN WORK',
    question: 'Did the agent leave work open?',
    to_review: 12,
    backlog: 140,
    reviewed: 30,
    agree: 24,
    flip: 4,
    skip: 2,
    by_stratum: {},
    kappa_min_pairs: 20,
    operator_vs_sonnet: AGREEMENT,
    operator_vs_laya: { ...AGREEMENT, n: 3, kappa: null, agree_rate: null },
    precision: { window_days: 7, shown: 9, dismissed: 2, value: 0.78 },
    recall_est: { n_flagged: 9, n_unflagged: 4, value: null },
    sweep_ready: { rows: 28, open: 9, done: 19, ready: false, reason: null },
    last_answer_id: 'a_0123456789abcdef',
    last_answer_at: '2026-10-26T23:30:00Z',
};
const CARD = {
    item_id: `agent_open_work:${KEY}`,
    key: KEY,
    surface: 'agent_open_work',
    stratum: 'sample',
    text: 'Hand-off: the migration is written; the deploy is still owed.',
    text_chars: 61,
    truncated: false,
    held_out: true,
    reference: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
    sonnet: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
    laya: null,
    golden: null,
    decision: null,
    answers: ['agree', 'flip', 'skip'],
};
const QUEUE = {
    v: 1,
    ok: true,
    message: '12 to review',
    surfaces: [STATS],
    surface: 'agent_open_work',
    cards: [CARD],
    sources: { labels: 'ok' },
    generated_at: '2026-10-26T23:31:00Z',
};
const ANSWER = { answer_id: 'a_00000000000000aa', key: KEY, answer: 'agree', open: true };
const REQUEST = { surface: 'agent_open_work', key: KEY, answer: 'agree' as const, reference_open: true };

const refusal = (code: string, message: string, extra: Record<string, unknown> = {}) => ({ v: 1, ok: false, code, message, error: message, ...extra });

// ── the stubbed bridge ──────────────────────────────────────────────────────

type Reply = Response | (() => never);
let reply: Reply = new Response('{}');
let calls: { url: string; init?: RequestInit }[] = [];
const real_fetch = globalThis.fetch;

function json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/**
 * Chrome's local-network verdict, as `navigator.permissions` answers it. Null rejects the query, as every browser
 * without the permission does: the gate then reads `unknown`. Stubbed before the gate's first read, which it caches.
 */
let chrome_permission: 'prompt' | null = null;
const real_navigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');

before(() => {
    Object.defineProperty(globalThis, 'navigator', {
        configurable: true,
        value: {
            permissions: {
                query: async () => {
                    if (chrome_permission === null) throw new TypeError('unknown permission name');
                    return { state: chrome_permission, onchange: null };
                },
            },
        },
    });
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), init });
        if (typeof reply === 'function') reply();
        return (reply as Response).clone();
    }) as typeof fetch;
    unlock_local_access();
});

after(() => {
    globalThis.fetch = real_fetch;
    relock_local_access();
    if (real_navigator) Object.defineProperty(globalThis, 'navigator', real_navigator);
});

beforeEach(() => {
    calls = [];
});

describe('reads', () => {
    it('asks for the queue with a clamped limit and the surface, and answers the checked body', async () => {
        reply = json(200, QUEUE);
        const got = await load_review_queue('agent_open_work', 20);
        assert.ok(got.ok);
        assert.equal(got.data.cards[0].key, KEY);
        assert.equal(calls[0].url, `${BASE}/review/queue?limit=20&surface=agent_open_work`);
        await load_review_queue(null, 99);
        assert.equal(calls[1].url, `${BASE}/review/queue?limit=50`);
        await load_review_queue('agent_open_work', 20, 'sample');
        assert.equal(calls[2].url, `${BASE}/review/queue?limit=20&surface=agent_open_work&stratum=sample`, 'DEAL narrows the queue');
    });

    it('a golden card keeps the request its case was written with, and an older bridge sends none', async () => {
        const golden = { case_id: 'case-a', expected: false, why: 'optional offer' };
        reply = json(200, { ...QUEUE, cards: [{ ...CARD, golden: { ...golden, request: 'Explain the scheduler.' } }, { ...CARD, golden }] });
        const got = await load_review_queue('agent_open_work', 20);
        assert.ok(got.ok);
        assert.deepEqual(got.data.cards.map((c) => c.golden?.request), ['Explain the scheduler.', undefined]);
        reply = json(200, { ...QUEUE, cards: [{ ...CARD, golden: { ...golden, request: 'x'.repeat(601) } }] });
        assert.equal((await load_review_queue('agent_open_work', 20)).ok, false, 'past REQUEST_MAX is outside the contract');
    });

    it('asks RECENT for the page after a cursor, and only a cursor in its shape', async () => {
        reply = json(200, {}); // the query is the point here, not the body
        const after = 'n~2026-11-07T12:00:00Z~0123456789abcdef';
        await load_review_recent('agent_open_work', 100, { after });
        assert.equal(calls[0].url, `${BASE}/review/recent?limit=100&surface=agent_open_work&${new URLSearchParams({ after })}`);
        const edge = 'e~0.010000~2026-11-07T12:00:00Z~golden:docs-1';
        await load_review_recent(null, 100, { order: 'edge', todo: true, after: edge });
        assert.equal(calls[1].url, `${BASE}/review/recent?limit=100&order=edge&todo=1&${new URLSearchParams({ after: edge })}`);
        for (const bad of [null, undefined, '', 'n~', `${after}\n`, 'x'.repeat(50)]) {
            await load_review_recent(null, 100, { after: bad });
        }
        assert.deepEqual(new Set(calls.slice(2).map((c) => c.url)), new Set([`${BASE}/review/recent?limit=100`]),
            'the first page never sends one, so a bridge that predates `after` still loads it');
    });

    it('an answer outside the contract is an error, never data', async () => {
        reply = json(200, { ...QUEUE, cards: [{ ...CARD, text: '' }] });
        const got = await load_review_queue(null, 20);
        assert.equal(got.ok, false);
        assert.deepEqual(got.ok ? null : [got.kind, got.error], ['invalid', 'review/queue: invalid laya response']);
        reply = json(200, { surprise: true });
        const ops = await load_laya_ops();
        assert.equal(ops.ok ? null : ops.error, 'ops: invalid laya response');
    });

    it('a 404 from the plugin means it predates the route: restart', async () => {
        reply = json(404, { error: 'not found' });
        const got = await load_laya_ops();
        assert.deepEqual(got.ok ? null : [got.kind, got.status, got.error], ['restart', 404, LAYA_ROUTE_PREDATES]);
    });

    it('a 404 from the host means the plugin was never mounted: enable it', async () => {
        reply = json(404, { error: 'unknown plugin: laya' });
        const got = await load_review_queue(null, 0);
        assert.deepEqual(got.ok ? null : [got.kind, got.hint], ['plugin', enable_plugin_hint('laya')]);
    });

    it('fail_text is the refusal, then its remedy when there is one', () => {
        assert.equal(fail_text({ error: 'busy' }), 'busy');
        assert.equal(fail_text({ error: 'The laya data folder is not there', hint: 'restart the bridge' }), 'The laya data folder is not there — restart the bridge');
    });

    it('a 503 shows its hint and keeps its code', async () => {
        reply = json(503, refusal('params_unavailable', 'The laya data folder is not there', { hint: 'restart the bridge (it creates <data>/laya), or run laya-lab once' }));
        const got = await load_review_queue(null, 20);
        assert.deepEqual(
            got.ok ? null : [got.kind, got.status, got.code, got.error, got.hint],
            ['refused', 503, 'params_unavailable', 'The laya data folder is not there', 'restart the bridge (it creates <data>/laya), or run laya-lab once'],
        );
    });

    it('a GET refusal keeps the code the deck keys on (bad_query, host_too_old), and none from a bare body', async () => {
        reply = json(400, refusal('bad_query', 'surface must be one of agent_open_work'));
        const bad = await load_review_queue('gone_surface', 20);
        assert.deepEqual(bad.ok ? null : [bad.kind, bad.status, bad.code], ['refused', 400, 'bad_query']);
        reply = json(403, refusal('host_too_old', 'This terminal bridge does not pass request headers to plugins: update tldrack'));
        const old = await load_laya_ops();
        assert.equal(old.ok ? null : old.code, 'host_too_old');
        reply = json(500, { error: 'boom' });
        const bare = await load_laya_ops();
        assert.deepEqual(bare.ok ? null : [bare.code, bare.error], [null, 'boom']);
    });

    it('tells a shut gate from a dead bridge', async () => {
        relock_local_access();
        try {
            const gated = await load_laya_ops();
            assert.deepEqual(gated.ok ? null : [gated.kind, gated.status, gated.error], ['gate', 0, LOCAL_ACCESS_LOCKED_HINT]);
            assert.equal(calls.length, 0, 'a shut gate never reaches the network');
        } finally {
            unlock_local_access();
        }
        reply = () => {
            throw new TypeError('Failed to fetch');
        };
        const dead = await load_laya_ops();
        assert.deepEqual(dead.ok ? null : [dead.kind, dead.status, dead.hint], ['down', 0, BRIDGE_DOWN_HINT]);
    });
});

describe('writes', () => {
    it('posts the checked request and answers the result', async () => {
        reply = json(200, { v: 1, ok: true, message: 'recorded', answer: ANSWER, card: null, stats: STATS });
        const got = await post_review_answer(REQUEST);
        assert.ok(got.ok);
        assert.equal(got.data.answer?.answer_id, ANSWER.answer_id);
        assert.equal(calls[0].url, `${BASE}/review/answer`);
        assert.deepEqual(JSON.parse(String(calls[0].init?.body)), REQUEST);
    });

    it('never sends a request the route would refuse', async () => {
        const got = await post_review_answer({ ...REQUEST, key: 'not-a-key' });
        assert.deepEqual(got.ok ? null : [got.kind, got.error], ['invalid', 'review/answer: invalid request']);
        const undo = await post_review_undo({ surface: 'agent_open_work', answer_id: 'nope' });
        assert.equal(undo.ok, false);
        assert.equal(calls.length, 0);
    });

    it('reads a 409 already_answered body: the live answer comes back', async () => {
        reply = json(409, refusal('already_answered', 'this item is already answered', { answer: { ...ANSWER, answer: 'flip', open: false }, card: null, stats: null }));
        const got = await post_review_answer(REQUEST);
        assert.equal(got.ok, false);
        if (got.ok) return;
        assert.equal(got.kind, 'refused');
        assert.equal(got.code, 'already_answered');
        assert.equal(got.error, 'this item is already answered');
        assert.equal(got.body?.answer?.answer, 'flip');
    });

    it('reads a 409 reference_changed body: the fresh card comes back', async () => {
        reply = json(409, refusal('reference_changed', 'the label changed', { card: { ...CARD, reference: { ...CARD.reference, open: false } } }));
        const got = await post_review_answer(REQUEST);
        assert.equal(got.ok ? null : got.body?.card?.reference.open, false);
    });

    it('a 409 body outside the contract is an error', async () => {
        reply = json(409, { ok: false, error: 'busy' });
        const got = await post_review_undo({ surface: 'agent_open_work', answer_id: 'a_0123456789abcdef' });
        assert.deepEqual(got.ok ? null : [got.kind, got.status, got.error], ['invalid', 409, 'review/undo: invalid laya response']);
    });

    it('a lab refusal needs no session to be read', async () => {
        reply = json(409, refusal('busy', 'laya-score is still running'));
        const got = await post_laya_lab('sweep', 'agent_open_work');
        assert.deepEqual(got.ok ? null : [got.kind, got.code, got.error], ['refused', 'busy', 'laya-score is still running']);
        reply = json(202, { v: 1, ok: true, message: 'started', session: 'laya-sweep' });
        const started = await post_laya_lab('sweep', 'agent_open_work');
        assert.equal(started.ok ? started.data.session : null, 'laya-sweep');
        assert.deepEqual(JSON.parse(String(calls.at(-1)?.init?.body)), { verb: 'sweep', surface: 'agent_open_work' });
    });

    it('any other refusal keeps its code and hint', async () => {
        reply = json(403, refusal('origin_refused', 'Laya refuses this origin', { hint: 'open it from the rack page' }));
        const got = await post_laya_restart();
        assert.deepEqual(got.ok ? null : [got.kind, got.status, got.code, got.error, got.hint], ['refused', 403, 'origin_refused', 'Laya refuses this origin', 'open it from the rack page']);
    });

    it('a 404 on a write is the restart message too', async () => {
        reply = json(404, { error: 'not found' });
        const got = await post_review_answer(REQUEST);
        assert.deepEqual(got.ok ? null : [got.kind, got.error], ['restart', BRIDGE_PREDATES_MESSAGE]);
    });
});

describe('the shared bridge helpers', () => {
    it('post_bridge_json passes the parsed body through, and null for a non-JSON one', async () => {
        const body = refusal('busy', 'still running');
        reply = json(409, body);
        const sent = await post_bridge_json(`${BASE}/lab`, {}, 1_000, 160);
        assert.deepEqual([sent.ok, sent.status, sent.message], [false, 409, 'still running']);
        assert.deepEqual(sent.body, body);
        reply = new Response('<html>', { status: 502 });
        assert.equal((await post_bridge_json(`${BASE}/lab`, {}, 1_000, 160)).body, null);
    });

    it('get_bridge_json reports the HTTP status of a failure, 0 when nothing answered', async () => {
        reply = json(409, refusal('unknown_item', 'no such item'));
        const got = await get_bridge_json(`${BASE}/review/item`, 1_000);
        assert.deepEqual(got.ok ? null : [got.status, got.error], [409, 'no such item']);
        reply = () => {
            throw new TypeError('Failed to fetch');
        };
        const dead = await get_bridge_json(`${BASE}/ops`, 1_000);
        assert.equal(dead.ok ? null : dead.status, 0);
        assert.equal(dead.ok ? undefined : dead.body, null);
    });

    it('get_bridge_json passes a failure’s parsed body through, and null for a non-JSON one', async () => {
        const body = refusal('params_unavailable', 'The laya data folder is not there');
        reply = json(503, body);
        const got = await get_bridge_json(`${BASE}/ops`, 1_000);
        assert.deepEqual(got.ok ? null : [got.error, got.body], ['The laya data folder is not there', body]);
        reply = new Response('<html>', { status: 502 });
        const html = await get_bridge_json(`${BASE}/ops`, 1_000);
        assert.deepEqual(html.ok ? null : [html.error, html.body], ['bridge responded 502', null]);
    });
});

// Last in the file: the gate never forgets a verdict it read, so nothing may run after it.
describe('Chrome’s local-network prompt', () => {
    it('a call that hangs behind the unanswered prompt is the gate, never BRIDGE DOWN', async () => {
        chrome_permission = 'prompt';
        assert.equal(await refresh_local_permission(), 'prompt');
        reply = () => {
            throw new DOMException('signal timed out', 'TimeoutError');
        };
        const got = await load_review_queue(null, 20);
        assert.deepEqual(got.ok ? null : [got.kind, got.status, got.error, got.hint], ['gate', 0, LOCAL_ACCESS_PROMPTING_HINT, undefined]);
        const posted = await post_review_answer(REQUEST);
        assert.deepEqual(posted.ok ? null : [posted.kind, posted.error], ['gate', LOCAL_ACCESS_PROMPTING_HINT]);
    });
});


describe('great example writes', () => {
    const request = { surface: 'agent_open_work', key: KEY, great_example: true };
    it('posts a checked setting and reads back the saved state', async () => {
        reply = json(200, { v: 1, ok: true, message: 'Great example saved', key: KEY, great_example: true });
        const got = await post_review_example(request);
        assert.ok(got.ok);
        assert.equal(got.data.great_example, true);
        assert.equal(calls[0].url, `${BASE}/review/example`);
        assert.deepEqual(JSON.parse(calls[0].init?.body as string), request);
    });
    it('refuses an invalid setting without sending it', async () => {
        const got = await post_review_example({ ...request, great_example: 'yes' as unknown as boolean });
        assert.equal(got.ok, false);
        assert.equal(calls.length, 0);
    });
    it('reports an older bridge and invalid response instead of claiming success', async () => {
        reply = json(404, { error: 'not found' });
        const old = await post_review_example(request);
        assert.ok(!old.ok);
        assert.equal(old.kind, 'restart');
        reply = json(200, { v: 1, ok: true, message: 'saved', key: KEY, great_example: 'true' });
        const bad = await post_review_example(request);
        assert.ok(!bad.ok);
        assert.equal(bad.kind, 'invalid');
    });
});
