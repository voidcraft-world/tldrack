import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import {
    CHAT_SEARCH_DEBOUNCE_MS,
    ask_rack_search,
    create_chat_search,
    rack_search_url,
} from '../../src/agent-rack/chat-search.ts';
import { build_rack_payload } from '../../src/engine/terminal/rack-view.ts';

const url = (path: string) => `http://127.0.0.1:7682${path}`;

function response(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status });
}

const HIT = { snippet: 'the cache broke', role: 'human', ts: 10 };

/** A chat search on manual timers and a scripted bridge. */
function harness(answer: (asked: URL) => Response | Promise<Response> = () => response(200, { matches: {}, ledger: true, indexed_at: 1 })) {
    const timers = new Map<number, () => void>();
    let next_timer = 0;
    const asked: URL[] = [];
    let changes = 0;
    const search = create_chat_search({
        base_url: url,
        on_change: () => { changes += 1; },
        fetch_impl: async (input) => {
            const at = new URL(String(input));
            asked.push(at);
            return answer(at);
        },
        set_timeout: (fn, ms) => {
            assert.equal(ms, CHAT_SEARCH_DEBOUNCE_MS);
            timers.set(++next_timer, fn);
            return next_timer as unknown as ReturnType<typeof setTimeout>;
        },
        clear_timeout: (handle) => { timers.delete(handle as unknown as number); },
    });
    const fire = async () => {
        const pending = [...timers.values()];
        timers.clear();
        for (const fn of pending) fn();
        await new Promise((resolve) => setTimeout(resolve, 0));
    };
    return { search, asked, timers, fire, changes: () => changes };
}

describe('rack_search_url', () => {
    test('asks for the normalized query and every live session, capped', () => {
        const at = new URL(rack_search_url(url, '  the   cache ', ['vc-1', 'vc-2'])!);
        assert.equal(at.pathname, '/ledger/rack-search');
        assert.equal(at.searchParams.get('q'), 'the cache');
        assert.deepEqual(at.searchParams.getAll('s'), ['vc-1', 'vc-2']);
        const many = Array.from({ length: 80 }, (_, i) => `vc-${i}`);
        assert.equal(new URL(rack_search_url(url, 'x', many)!).searchParams.getAll('s').length, 64);
    });

    test('nothing to ask is no URL', () => {
        assert.equal(rack_search_url(url, '   ', ['vc-1']), null);
        assert.equal(rack_search_url(url, 'cache', []), null);
    });
});

describe('ask_rack_search', () => {
    const signal = new AbortController().signal;
    test('an answer parses; a ledger that is off reads as recent-only', async () => {
        const fetch_ok = async () => response(200, { matches: { 'vc-1': HIT }, ledger: false, indexed_at: null });
        assert.deepEqual(await ask_rack_search('u', fetch_ok, signal), { ok: true, matches: { 'vc-1': HIT }, recent_only: true });
    });

    test('every failure is one line, never a throw', async () => {
        const cases: [() => Promise<Response>, RegExp][] = [
            [async () => { throw new TypeError('down'); }, /could not reach/],
            [async () => response(404, {}), /newer bridge/],
            [async () => response(503, { error: 'History search is busy. Please retry.' }), /busy/],
            [async () => response(200, { matches: 'no' }), /unreadable/],
        ];
        for (const [fetch_impl, expected] of cases) {
            const outcome = await ask_rack_search('u', fetch_impl, signal);
            assert.equal(outcome.ok, false);
            assert.match((outcome as { error: string }).error, expected);
        }
    });
});

describe('create_chat_search', () => {
    test('a query is debounced, asked once, and carried back', async () => {
        const h = harness(() => response(200, { matches: { 'vc-1': HIT }, ledger: true, indexed_at: 1 }));
        h.search.observe_sessions(['vc-1', 'vc-2']);
        assert.equal(h.search.view(), undefined);
        assert.ok(h.search.handle_action('search', 'c'));
        assert.ok(h.search.handle_action('search', 'ca'));
        assert.ok(h.search.handle_action('search', ' cache '));
        assert.deepEqual(h.search.view(), { query: 'cache', pending: true, matches: {} });
        assert.equal(h.timers.size, 1);
        await h.fire();
        assert.equal(h.asked.length, 1);
        assert.equal(h.asked[0]!.searchParams.get('q'), 'cache');
        assert.deepEqual(h.search.view(), { query: 'cache', pending: false, matches: { 'vc-1': HIT } });
    });

    test('a newer question keeps the last answer standing, then replaces it', async () => {
        const h = harness((at) => response(200, {
            matches: at.searchParams.get('q') === 'cache' ? { 'vc-1': HIT } : {}, ledger: true, indexed_at: 1,
        }));
        h.search.observe_sessions(['vc-1']);
        h.search.handle_action('search', 'cache');
        await h.fire();
        h.search.handle_action('search', 'cache fix');
        assert.deepEqual(h.search.view(), { query: 'cache fix', pending: true, matches: { 'vc-1': HIT } });
        await h.fire();
        assert.deepEqual(h.search.view(), { query: 'cache fix', pending: false, matches: {} });
    });

    test('a slow answer to an old question never lands over a newer one', async () => {
        let release_first: (r: Response) => void = () => {};
        const h = harness((at) => at.searchParams.get('q') === 'old'
            ? new Promise<Response>((resolve) => { release_first = resolve; })
            : response(200, { matches: {}, ledger: true, indexed_at: 1 }));
        h.search.observe_sessions(['vc-1']);
        h.search.handle_action('search', 'old');
        await h.fire();
        h.search.handle_action('search', 'new');
        await h.fire();
        release_first(response(200, { matches: { 'vc-1': HIT }, ledger: true, indexed_at: 1 }));
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.deepEqual(h.search.view(), { query: 'new', pending: false, matches: {} });
    });

    test('clearing the box drops the question, the timer and the answer', async () => {
        const h = harness();
        h.search.observe_sessions(['vc-1']);
        h.search.handle_action('search', 'cache');
        h.search.handle_action('search', '   ');
        assert.equal(h.timers.size, 0);
        assert.equal(h.search.view(), undefined);
        await h.fire();
        assert.equal(h.asked.length, 0);
    });

    test('a changed rack re-asks while a query stands; the same rack does not', async () => {
        const h = harness();
        h.search.observe_sessions(['vc-1']);
        h.search.handle_action('search', 'cache');
        await h.fire();
        h.search.observe_sessions(['vc-1']);
        assert.equal(h.timers.size, 0);
        h.search.observe_sessions(['vc-2', 'vc-1']);
        await h.fire();
        assert.equal(h.asked.length, 2);
        assert.deepEqual(h.asked[1]!.searchParams.getAll('s'), ['vc-2', 'vc-1']);
    });

    test('a refusal is shown, not swallowed, and other actions pass through', async () => {
        const h = harness(() => response(404, {}));
        h.search.observe_sessions(['vc-1']);
        assert.equal(h.search.handle_action('closed'), false);
        h.search.handle_action('search', 'cache');
        await h.fire();
        assert.match(h.search.view()!.error!, /newer bridge/);
        assert.equal(h.search.view()!.pending, false);
    });

    test('dispose cancels a pending ask', async () => {
        const h = harness();
        h.search.observe_sessions(['vc-1']);
        h.search.handle_action('search', 'cache');
        h.search.dispose();
        await h.fire();
        assert.equal(h.asked.length, 0);
    });

    test('the payload carries the view only while there is one', () => {
        const base = { rows: [], focused: null, reachable: true, max_sessions: 8 };
        assert.equal(build_rack_payload(base).chat_search, undefined);
        const view = { query: 'cache', pending: false, matches: { 'vc-1': HIT } };
        assert.deepEqual(build_rack_payload({ ...base, chat_search: view }).chat_search, view);
    });
});
