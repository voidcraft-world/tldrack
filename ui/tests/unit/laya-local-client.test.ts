import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { laya_decide, laya_react, parse_laya_status } from '../../src/decide/laya-local-client.ts';

/**
 * The page's Laya client never throws and never trusts: every outcome that is not a clean,
 * shared-reader-approved answer is a REASON, so a caller can always fall back to its rules.
 */

const REQUEST = {
    state: 'buy milk\ncall the dentist',
    questions: { kind: { type: 'choice' as const, instructions: 'What is it?', criteria: { note: 'prose', tasks: 'to-dos' } } },
};

function reply(status: number, body: unknown): typeof fetch {
    return (async () => new Response(typeof body === 'string' ? body : JSON.stringify(body), { status })) as typeof fetch;
}

describe('laya_decide', () => {
    it('reads a clean answer through the shared reader', async () => {
        const outcome = await laya_decide(REQUEST, {
            fetch_impl: reply(200, { model: 'laya-rl-agent', answers: { kind: { choice: 'tasks', probabilities: { note: 0.1, tasks: 0.9 } } }, usage: { input_tokens: 20 } }),
        });
        assert.ok(outcome.ok);
        assert.equal(outcome.result.answers.kind!.choice, 'tasks');
    });

    it('refuses an option it was not offered', async () => {
        const outcome = await laya_decide(REQUEST, {
            fetch_impl: reply(200, { answers: { kind: { choice: 'contact' } }, usage: { input_tokens: 20 } }),
        });
        assert.deepEqual(outcome.ok ? null : outcome.reason, 'invalid');
    });

    it('tells warming from not installed', async () => {
        const warming = await laya_decide(REQUEST, { fetch_impl: reply(503, { error: 'warming', state: 'starting' }) });
        const missing = await laya_decide(REQUEST, { fetch_impl: reply(503, { error: 'not_installed', state: 'not_installed' }) });
        assert.equal(warming.ok ? null : warming.reason, 'warming');
        assert.equal(missing.ok ? null : missing.reason, 'not_installed');
    });

    it('is unreachable, not a throw, when the bridge is gone or the plugin absent', async () => {
        const gone = await laya_decide(REQUEST, { fetch_impl: (async () => { throw new TypeError('Failed to fetch'); }) as typeof fetch });
        const absent = await laya_decide(REQUEST, { fetch_impl: reply(404, { error: 'not found' }) });
        assert.equal(gone.ok ? null : gone.reason, 'unreachable');
        assert.equal(absent.ok ? null : absent.reason, 'unreachable');
    });

    it('never sends a malformed request', async () => {
        let called = false;
        const outcome = await laya_decide({ state: '', questions: {} }, {
            fetch_impl: (async () => { called = true; return new Response('{}'); }) as typeof fetch,
        });
        assert.equal(outcome.ok ? null : outcome.reason, 'refused');
        assert.equal(called, false);
    });
});

/** A fetch that records each call and answers `status` with `body`. */
function recorder(status: number, body: unknown): { calls: { url: string; init: RequestInit }[]; fetch_impl: typeof fetch } {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetch_impl = (async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify(body), { status });
    }) as unknown as typeof fetch;
    return { calls, fetch_impl };
}

const ANSWER = { model: 'english', answers: { kind: { choice: 'tasks', probabilities: { note: 0.1, tasks: 0.9 } } }, usage: { input_tokens: 20 } };
const DECISION_ID = '20261027T013000-0123456789ab';

describe('laya_decide ledger tag', () => {
    it('sends the tag beside the model, and the request unchanged', async () => {
        const { calls, fetch_impl } = recorder(200, ANSWER);
        await laya_decide(REQUEST, { model: 'english', ledger: { surface: 'paste', threshold: 0.8 }, fetch_impl });
        assert.equal(calls.length, 1);
        assert.deepEqual(JSON.parse(String(calls[0]!.init.body)), { ...REQUEST, model: 'english', ledger: { surface: 'paste', threshold: 0.8 } });
    });

    it('sends no ledger field when untagged', async () => {
        const { calls, fetch_impl } = recorder(200, ANSWER);
        await laya_decide(REQUEST, { fetch_impl });
        assert.equal('ledger' in JSON.parse(String(calls[0]!.init.body)), false);
    });

    it('refuses a tag the ledger would refuse, without calling the bridge, naming the field as the plugin does', async () => {
        const bad: [unknown, RegExp][] = [
            [{ surface: 'paste', keep_text: false }, /^ledger\.keep_text: /], // a field that no longer exists
            [{ surface: 'No-Where' }, /^ledger\.surface: /], // any host may name a surface, but only in the wire's shape
            [{ surface: 'untagged' }, /^ledger\.surface: /], // the name a row takes when its call named none
            [{ surface: 'notes', subject: 'has a space' }, /^ledger\.subject: /],
            [{ surface: 'notes', question_version: -1 }, /^ledger\.question_version: /],
            [{ surface: 'paste', threshold: 1.5 }, /^ledger\.threshold: /],
            [{ surface: 'coordinator' }, /^ledger\.surface: /], // a rack surface: a page is never a native caller
            [{ surface: 'agent_open_work' }, /^ledger\.surface: /],
        ];
        for (const [ledger, detail] of bad) {
            const { calls, fetch_impl } = recorder(200, ANSWER);
            const outcome = await laya_decide(REQUEST, { ledger: ledger as never, fetch_impl });
            assert.equal(outcome.ok ? null : outcome.reason, 'refused', JSON.stringify(ledger));
            assert.match(outcome.ok ? '' : outcome.detail ?? '', detail);
            assert.equal(calls.length, 0);
        }
    });

    it('reads the decision id, and keeps it only in the ledger’s shape', async () => {
        const kept = await laya_decide(REQUEST, { fetch_impl: recorder(200, { ...ANSWER, decision_id: DECISION_ID, model_id: null, checkpoint: null }).fetch_impl });
        assert.equal(kept.ok ? kept.decision_id : 'refused', DECISION_ID);
        for (const decision_id of [undefined, null, 42, 'not-an-id', `${DECISION_ID}/../x`]) {
            const outcome = await laya_decide(REQUEST, { fetch_impl: recorder(200, { ...ANSWER, decision_id }).fetch_impl });
            assert.ok(outcome.ok);
            assert.equal(outcome.decision_id, null, String(decision_id));
        }
    });
});

describe('laya_react', () => {
    it('posts the reaction as JSON and answers true on 200', async () => {
        const { calls, fetch_impl } = recorder(200, { ok: true, decision_id: DECISION_ID, action: 'dismissed', duplicate: false });
        assert.equal(await laya_react(DECISION_ID, 'dismissed', fetch_impl), true);
        assert.equal(calls.length, 1);
        assert.match(calls[0]!.url, /\/plugin\/laya\/react$/);
        assert.equal(calls[0]!.init.method, 'POST');
        assert.equal((calls[0]!.init.headers as Record<string, string>)['Content-Type'], 'application/json');
        assert.deepEqual(JSON.parse(String(calls[0]!.init.body)), { decision_id: DECISION_ID, action: 'dismissed' });
    });

    it('answers false, never a throw, on an unknown decision, a missing route, or a dead bridge', async () => {
        assert.equal(await laya_react(DECISION_ID, 'shown', recorder(409, { error: 'unknown_decision' }).fetch_impl), false);
        assert.equal(await laya_react(DECISION_ID, 'shown', recorder(404, { error: 'not found' }).fetch_impl), false);
        assert.equal(await laya_react(DECISION_ID, 'shown', recorder(503, { error: 'no_ledger' }).fetch_impl), false);
        const gone = (async () => { throw new TypeError('Failed to fetch'); }) as typeof fetch;
        assert.equal(await laya_react(DECISION_ID, 'shown', gone), false);
    });

    it('gives up after its budget', async (t) => {
        t.mock.timers.enable({ apis: ['setTimeout'] });
        const hung = ((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        })) as unknown as typeof fetch;
        const pending = laya_react(DECISION_ID, 'expired', hung);
        t.mock.timers.tick(1_500);
        assert.equal(await pending, false);
    });

    it('never sends a reaction the plugin would refuse', async () => {
        const { calls, fetch_impl } = recorder(200, {});
        assert.equal(await laya_react('not-an-id', 'shown', fetch_impl), false);
        assert.equal(await laya_react(DECISION_ID, 'liked' as never, fetch_impl), false);
        assert.equal(calls.length, 0);
    });
});

describe('parse_laya_status', () => {
    it('reads the plugin status and rejects unknown states', () => {
        assert.deepEqual(parse_laya_status({
            state: 'ready', models: ['voidcraft-paste', 3], device: 'mps', idle_stop_seconds: 900,
            model_ids: { english: 'laya-local/english@a1b2c3d', evil: 'othervendor/model-1.13' },
        }), {
            state: 'ready', models: ['voidcraft-paste'], device: 'mps', error: null, idle_stop_seconds: 900,
            model_ids: { english: 'laya-local/english@a1b2c3d' },
        });
        assert.equal(parse_laya_status({ state: 'exploded' }), null);
        assert.equal(parse_laya_status(null), null);
    });
});
