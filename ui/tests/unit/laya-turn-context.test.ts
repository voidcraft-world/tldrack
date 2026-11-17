import { after, before, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { HISTORY_THREADS_MAX, type HistoryHit, type HistoryThreads, type HistoryTurn } from '@tldrack/schemas/agent-history.ts';

import {
    NEEDLE_CHARS, NO_THREADS, ask_seq, ask_size, awaits_ingest, bridge_origin, closing_needle, context_source, freshen_threads, ingest_pacer, is_human_role,
    laya_read_note, load_linked_context, load_threads, merge_threads, pick_hit, context_title, prepend_context, load_turn_context, reader_layout,
    role_head, threads_urls, unresolved_ask, WAKE_SUMMARY_CHARS, WAKE_TAG, wake_note, wake_summary, woke_text, type FreshenDeps, type ThreadsResult, type TurnContext,
} from '../../src/laya-review/turn-context.ts';

import { unlock_local_access, relock_local_access } from '../../src/engine/terminal/local-network-gate.ts';

// The conversation behind a verdict, found in the session ledger by its closing text (turn-context.ts owns why).

const hit = (over: Partial<HistoryHit>): HistoryHit => ({
    file_id: 1, session_id: null, source: 'claude', profile: null, kind: 'main', title: 't', cwd: null,
    seq: 5, ts: Date.parse('2026-11-04T09:00:00Z') / 1000, role: 'agent', snippet: '', ...over,
});

describe('closing_needle', () => {
    it('takes the first line that is the message’s own words, capped', () => {
        assert.equal(closing_needle('ok\n\nIt was not a typo, it was the wrong key. Nothing limits the retries.'), 'It was not a typo, it was the wrong key. Nothing limits the retries.');
        assert.equal(closing_needle(`  ${'x'.repeat(300)}`)?.length, NEEDLE_CHARS);
        assert.equal(closing_needle('Waiting.\nDone.'), null, 'too short to find one message');
    });
});

describe('pick_hit', () => {
    const at = '2026-11-04T09:00:02Z';
    it('takes the nearest agent message inside the window', () => {
        const near = hit({ seq: 9, ts: Date.parse(at) / 1000 - 2 });
        assert.equal(pick_hit([hit({ ts: Date.parse(at) / 1000 - 600 }), near], at), near);
    });
    it('refuses a user echo, a message from another day, and a bad stamp', () => {
        assert.equal(pick_hit([hit({ role: 'user', ts: Date.parse(at) / 1000 })], at), null);
        assert.equal(pick_hit([hit({ ts: Date.parse(at) / 1000 - 86_400 })], at), null);
        assert.equal(pick_hit([hit({ ts: null })], at), null);
        assert.equal(pick_hit([hit({})], 'nope'), null);
    });
    it('without a recorded time, requires one unique match and a complete result page', () => {
        const only = hit({});
        assert.equal(pick_hit([hit({ role: 'human' }), only], null), only);
        assert.equal(pick_hit([only, hit({ file_id: 2 })], null), null);
        assert.equal(pick_hit([only], null, true), null);
        assert.equal(pick_hit([], null), null);
    });
});

describe('ask_seq', () => {
    it('is the last prompt at or before the closing message', () => {
        const m = (seq: number, role: string) => ({ seq, ts: null, role, text: '' });
        assert.equal(ask_seq([m(1, 'user'), m(2, 'agent'), m(3, 'user'), m(4, 'agent'), m(6, 'user')], 4), 3);
        assert.equal(ask_seq([m(2, 'agent')], 2), null);
        assert.equal(ask_seq([m(1, 'human'), m(2, 'agent')], 2), 1);
        assert.equal(is_human_role('human'), true);
        assert.equal(is_human_role('user'), true);
        assert.equal(is_human_role('machine'), false);
    });
});

describe('bridge_origin', () => {
    it('drops the plugin path', () => {
        assert.equal(bridge_origin('http://127.0.0.1:7682/plugin/laya'), 'http://127.0.0.1:7682');
    });
});


const message = (seq: number, role = 'agent') => ({ seq, role, ts: null, text: `Message ${seq}` });
const context = (over: Partial<Extract<TurnContext, { ok: true }>> = {}): Extract<TurnContext, { ok: true }> => ({
    ok: true, title: 'PR #12', cwd: null, file_id: 1, closing_seq: 60,
    messages: [message(59), message(60)], before: 59, ...over,
});

describe('prepend_context', () => {
    it('keeps the judged close and drops overlapping or later messages when paging backwards', () => {
        const current = context();
        const merged = prepend_context(current, context({ messages: [message(1, 'human'), message(59), message(61)], closing_seq: 59, before: null }));
        assert.deepEqual(merged.messages.map(m => m.seq), [1, 59, 60]);
        assert.equal(merged.closing_seq, 60);
        assert.equal(merged.before, null);
        assert.equal(prepend_context(current, context({ file_id: 2 })), current);
        assert.equal(prepend_context(current, context()), current, 'no progress preserves the existing page');
    });
});

describe('loading conversation context', () => {
    const real_fetch = globalThis.fetch;
    const real_navigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    before(() => {
        Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { permissions: { query: async () => { throw new Error('no permission API'); } } } });
        unlock_local_access();
    });
    after(() => {
        globalThis.fetch = real_fetch;
        relock_local_access();
        if (real_navigator) Object.defineProperty(globalThis, 'navigator', real_navigator);
        else Reflect.deleteProperty(globalThis, 'navigator');
    });

    function serve(pages: unknown[]): string[] {
        const urls: string[] = [];
        globalThis.fetch = async (input) => {
            urls.push(String(input));
            assert.ok(pages.length, 'no unbounded history reads');
            return new Response(JSON.stringify(pages.shift()), { headers: { 'content-type': 'application/json' } });
        };
        return urls;
    }
    const search = { hits: [hit({ seq: 60 })], next_offset: null, indexed_at: null };
    const page = (messages: ReturnType<typeof message>[], before: number | null, extra: Record<string, unknown> = {}) =>
        ({ session: hit({}), messages, before, after: 60, indexed_at: null, ...extra });

    it('shows the original request independently of a long session’s recent page', async () => {
        const first_user = { ...message(0, 'human'), text: 'The original user request' };
        const urls = serve([search, page([message(58, 'human'), message(60)], 58, { first_user })]);
        const found = await load_turn_context('The requested pull request is now mergeable.', '2026-11-04T09:00:02Z');
        assert.ok(found.ok);
        assert.equal(found.first_user?.text, 'The original user request');
        assert.equal(found.first_user?.seq, 0, 'not the recent prompt at 58');
        assert.deepEqual(found.messages.map(m => m.seq), [58, 60]);
        assert.equal(found.closing_seq, 60);
        assert.equal(found.before, 58);
        assert.equal(new URL(urls[1]).searchParams.get('before'), '61');
        assert.equal(urls.length, 2, 'does not scan backwards through the session');
    });

    it('on an older bridge reads the start directly and excludes later turns', async () => {
        const first = { ...message(0, 'user'), text: 'Original request\nFull details' };
        const urls = serve([search, page([message(59, 'human'), message(60)], 59, { session: hit({ title: null }) }),
            page([first, message(90)], null, { session: hit({ title: null }) })]);
        const found = await load_turn_context('The requested pull request is now mergeable.', '2026-11-04T09:00:02Z');
        assert.ok(found.ok);
        assert.deepEqual(found.first_user, first);
        assert.equal(found.title, 'Original request');
        assert.deepEqual(found.messages.map(m => m.seq), [59, 60]);
        assert.equal(new URL(urls[2]).searchParams.get('around'), '0');
        assert.equal(urls.length, 3);
    });

    it('does not use a request from after the judged close', async () => {
        serve([search, page([message(60)], 60, { first_user: message(90, 'human') })]);
        const found = await load_turn_context('The requested pull request is now mergeable.', '2026-11-04T09:00:02Z');
        assert.ok(found.ok);
        assert.equal(found.first_user, null);
    });

    it('keeps the closing message readable when the first page is unavailable', async () => {
        serve([search, page([message(59, 'human'), message(60)], 59), { error: 'Index busy' }]);
        const found = await load_turn_context('The requested pull request is now mergeable.', '2026-11-04T09:00:02Z');
        assert.ok(found.ok);
        assert.equal(found.first_user, undefined, 'never substitutes the latest user message');
        assert.deepEqual(found.messages.map(m => m.seq), [59, 60]);
    });

});


describe('context_title', () => {
    const messages = [{ seq: 0, ts: null, role: 'human', text: 'get the branch ready, please\nhttps://github.com/o/r/pull/12' }];
    it('uses the first request for an untitled Codex session, only when the page reaches its start', () => {
        assert.equal(context_title({ title: null, messages, before: null }), 'get the branch ready, please');
        assert.equal(context_title({ title: null, messages, before: 15 }), null, 'a later turn must not rename the session');
        assert.equal(context_title({ title: null, messages: [], before: 500, first_user: messages[0] }), 'get the branch ready, please');
        assert.equal(context_title({ title: 'Saved title', messages, before: null }), 'Saved title');
        assert.equal(context_title({ title: null, messages: [], before: null }), null);
    });
});


// ── the join (/ledger/threads), and what falls back to the needle ──

const D1 = '20261107T120000-aaaaaaaaaaaa';
const D2 = '20261107T120100-bbbbbbbbbbbb';
const C1 = '11111111-1111-4111-8111-111111111111';
const K1 = '00000000000000ab';
const linked = (over: Partial<HistoryTurn> = {}): HistoryTurn => ({
    status: 'linked', thread: C1, conversation: C1, file_id: 7, closing_seq: 40, turn_idx: 3, human_ordinal: 2,
    woke_by: 'human', request: null, ask: null, closing_tail: 'tail', closing_chars: 900, ...over,
});
const answer = (over: Partial<HistoryThreads> = {}): HistoryThreads => ({ ...NO_THREADS, ...over });

describe('threads_urls', () => {
    it('joins each kind into one comma param, drops bad shapes and repeats, and asks nothing for nothing', () => {
        const [url] = threads_urls('http://b', { decisions: [D1, D1, 'nope'], conversations: [C1, 'x'], keys: [K1, 'golden:case-1'] });
        assert.equal(url, `http://b/ledger/threads?d=${D1}&c=${C1}&k=${K1}`);
        assert.deepEqual(threads_urls('http://b', {}), []);
        assert.deepEqual(threads_urls('http://b', { decisions: ['nope'] }), []);
    });
    it('chunks at the cap, all kinds together', () => {
        const decisions = Array.from({ length: HISTORY_THREADS_MAX + 5 }, (_, i) => `20261107T120000-${i.toString(16).padStart(12, '0')}`);
        const urls = threads_urls('http://b', { decisions, conversations: [C1] });
        assert.equal(urls.length, 2);
        const ids = (u: string) => [...new URL(u).searchParams.values()].flatMap((v) => v.split(','));
        assert.equal(ids(urls[0]).length, HISTORY_THREADS_MAX);
        assert.deepEqual(ids(urls[1]).slice(-1), [C1]);
        assert.equal(ids(urls[0]).length + ids(urls[1]).length, decisions.length + 1);
    });
});

describe('merging and what is still unsettled', () => {
    it('later answers win id by id', () => {
        const merged = merge_threads(answer({ decisions: { [D1]: linked({ status: 'pending' }), [D2]: linked() }, indexed_at: 5 }),
            answer({ decisions: { [D1]: linked() }, indexed_at: null }));
        assert.equal(merged.decisions[D1].status, 'linked');
        assert.equal(merged.decisions[D2].status, 'linked');
        assert.equal(merged.indexed_at, 5);
    });
    it('pending, missing and unasked are unsettled; none never asks again', () => {
        const data = answer({
            decisions: { [D1]: linked({ status: 'pending' }), [D2]: linked({ status: 'none' }) },
            conversations: { [C1]: null },
            keys: { [K1]: linked() },
        });
        const left = unresolved_ask(data, { decisions: [D1, D2], conversations: [C1], keys: [K1] });
        assert.deepEqual(left, { decisions: [D1], conversations: [C1], keys: [] });
        assert.equal(ask_size(left), 2);
        assert.equal(ask_size(unresolved_ask(answer({ conversations: { [C1]: C1 } }), { conversations: [C1] })), 0);
    });
    it('a key’s missing is final, a decision’s is asked again', () => {
        // A deck key with no link row is a labelled text no live verdict ever had: asking again never changes it.
        const K2 = '00000000000000cd';
        const data = answer({
            decisions: { [D1]: linked({ status: 'missing' }) },
            keys: { [K1]: linked({ status: 'missing' }), [K2]: linked({ status: 'pending' }) },
        });
        assert.deepEqual(unresolved_ask(data, { decisions: [D1], keys: [K1, K2] }), { decisions: [D1], conversations: [], keys: [K2] });
        assert.equal(awaits_ingest(linked({ status: 'missing' }), 'key'), false);
        assert.equal(awaits_ingest(linked({ status: 'missing' }), 'decision'), true);
        assert.equal(awaits_ingest(linked({ status: 'pending' }), 'key'), true);
        assert.equal(awaits_ingest(linked({ status: 'none' }), 'decision'), false);
        assert.equal(awaits_ingest(linked(), 'decision'), false);
    });
    it('reads a linked turn by position, waits for a join in flight, and searches by text otherwise', () => {
        assert.equal(context_source(linked(), false), 'linked');
        assert.equal(context_source(undefined, true), 'wait');
        assert.equal(context_source(undefined, false), 'needle');
        assert.equal(context_source(linked({ status: 'pending', file_id: null, closing_seq: null }), true), 'needle', 'answered: no need to wait');
        assert.equal(context_source(linked({ status: 'none' }), false), 'needle');
        assert.equal(context_source(null, false), 'needle');
    });
});

describe('freshen_threads', () => {
    function deps(answers: ThreadsResult[], over: Partial<FreshenDeps> = {}) {
        const timers: { ms: number; run: () => void }[] = [];
        const asked: unknown[] = [];
        let ingests = 0;
        const d: FreshenDeps = {
            pacer: ingest_pacer(60_000, () => 0),
            ingest: async () => { ingests += 1; return true; },
            load: async (ask) => { asked.push(ask); return answers.shift() ?? { ok: false, reason: 'unavailable', message: 'none left' }; },
            delays: [3_000, 6_000],
            wait: (ms, run) => { timers.push({ ms, run }); return () => {}; },
            ...over,
        };
        const flush = async () => { await new Promise((r) => setTimeout(r, 0)); };
        return { d, timers, asked, ingests: () => ingests, flush };
    }
    const pending = answer({ decisions: { [D1]: linked({ status: 'pending' }), [D2]: linked() } });
    const ask = { decisions: [D1, D2] };

    it('wakes the ingest once, re-asks only the unsettled ids, and stops once they settle', async () => {
        const t = deps([{ ok: true, data: answer({ decisions: { [D1]: linked() } }) }]);
        const took: HistoryThreads[] = [];
        freshen_threads(ask, pending, (data) => took.push(data), t.d);
        await t.flush();
        assert.equal(t.ingests(), 1);
        assert.deepEqual(t.timers.map((x) => x.ms), [3_000]);
        t.timers[0].run();
        await t.flush();
        assert.deepEqual(t.asked, [{ decisions: [D1], conversations: [], keys: [] }]);
        assert.equal(took.at(-1)?.decisions[D1].status, 'linked');
        assert.equal(took.at(-1)?.decisions[D2].status, 'linked', 'merged over the first answer');
        assert.equal(t.timers.length, 2);
        t.timers[1].run();
        await t.flush();
        assert.equal(t.asked.length, 1, 'settled: the second re-ask asks nothing');
    });

    it('asks again at most twice, and never while nothing is unsettled', async () => {
        const still = { ok: true as const, data: answer({ decisions: { [D1]: linked({ status: 'pending' }) } }) };
        const t = deps([still, still, still]);
        freshen_threads(ask, pending, () => {}, t.d);
        await t.flush();
        for (let i = 0; i < 5 && i < t.timers.length; i++) {
            t.timers[i].run();
            await t.flush();
        }
        assert.equal(t.asked.length, 2);
        const quiet = deps([]);
        freshen_threads(ask, answer({ decisions: { [D1]: linked(), [D2]: linked() } }), () => {}, quiet.d);
        await quiet.flush();
        assert.equal(quiet.ingests(), 0);
    });

    it('wakes the ingest at most once a minute, page-wide', async () => {
        let clock = 0;
        const pacer = ingest_pacer(60_000, () => clock);
        const t = deps([], { pacer });
        freshen_threads(ask, pending, () => {}, t.d);
        clock = 30_000;
        freshen_threads(ask, pending, () => {}, t.d);
        await t.flush();
        assert.equal(t.ingests(), 1);
        clock = 61_000;
        freshen_threads(ask, pending, () => {}, t.d);
        await t.flush();
        assert.equal(t.ingests(), 2);
    });

    it('a caller the pacer turns down still re-asks its own ids, on the wake another caller spent', async () => {
        // The deck and RECENT share one pacer: the deck's deal takes the slot, then RECENT's join lands with its newest
        // turn still pending. RECENT must still ask again for ITS ids; the deck's re-asks only cover the deck's.
        const pacer = ingest_pacer(60_000, () => 0);
        const deck = deps([], { pacer });
        freshen_threads({ keys: [K1] }, answer({ keys: { [K1]: linked({ status: 'pending' }) } }), () => {}, deck.d);
        const recent = deps([{ ok: true, data: answer({ decisions: { [D1]: linked() } }) }], { pacer });
        const took: HistoryThreads[] = [];
        freshen_threads(ask, pending, (data) => took.push(data), recent.d);
        await deck.flush();
        assert.equal(deck.ingests() + recent.ingests(), 1, 'one wake between them');
        assert.deepEqual(recent.timers.map((x) => x.ms), [3_000]);
        recent.timers[0].run();
        await recent.flush();
        assert.deepEqual(recent.asked, [{ decisions: [D1], conversations: [], keys: [] }]);
        assert.equal(took.at(-1)?.decisions[D1].status, 'linked');
    });

    it('a cancel stops the re-asks, and a failed wake schedules none', async () => {
        const t = deps([{ ok: true, data: answer({ decisions: { [D1]: linked() } }) }]);
        const took: HistoryThreads[] = [];
        const cancel = freshen_threads(ask, pending, (data) => took.push(data), t.d);
        await t.flush();
        cancel();
        t.timers[0].run();
        await t.flush();
        assert.equal(t.asked.length, 0);
        assert.equal(took.length, 0);
        const down = deps([], { ingest: async () => false });
        freshen_threads(ask, pending, () => {}, down.d);
        await down.flush();
        assert.equal(down.timers.length, 0);
    });
});

describe('the reader’s layout', () => {
    const m = (seq: number, role = 'agent') => ({ seq, ts: null, role, text: `m${seq}` });
    const page = [m(30, 'human'), m(31), m(32, 'machine'), m(33), m(34), m(35)];

    it('puts the operator’s prompt above the turn’s progress, and the closing last', () => {
        const at = reader_layout(page, 35, { request_seq: 30, ask: { seq: 30, ts: null, role: 'human', text: 'clip' } });
        assert.equal(at.ask?.text, 'm30', 'the loaded message beats the clip');
        assert.deepEqual(at.in_turn.map((x) => x.seq), [31, 32, 33, 34]);
        assert.equal(at.in_turn_count, 4);
        assert.deepEqual(at.earlier, []);
        assert.equal(at.earlier_count, 30, 'seqs 0–29 sit before the turn');
        assert.equal(at.closing?.seq, 35);
    });

    it('a turn a task woke keeps its opener in the turn and the operator’s older prompt as the ask', () => {
        const at = reader_layout(page, 35, { request_seq: 32, ask: { seq: 30, ts: null, role: 'human', text: 'clip' } });
        assert.equal(at.ask?.seq, 30);
        assert.deepEqual(at.in_turn.map((x) => x.seq), [32, 33, 34]);
        assert.deepEqual(at.earlier.map((x) => x.seq), [], 'the ask is not drawn twice');
        assert.deepEqual(at.after_ask.map((x) => x.seq), [31], 'the agent’s answer to the ask sits after it');
        assert.equal(at.earlier_count, 31);
    });

    it('splits what came before the turn at the ask, so a revealed reader stays in time order', () => {
        // 10 the operator's prompt, 11–20 the agent's earlier turn, 21 the notification that opened this one, 31 closing.
        const all = Array.from({ length: 32 }, (_, i) => m(i, i === 10 ? 'human' : i === 21 ? 'machine' : 'agent'));
        const at = reader_layout(all, 31, { request_seq: 21, ask: { seq: 10, ts: null, role: 'human', text: 'clip' } });
        assert.deepEqual(at.earlier.map((x) => x.seq), Array.from({ length: 10 }, (_, i) => i));
        assert.equal(at.ask?.seq, 10);
        assert.deepEqual(at.after_ask.map((x) => x.seq), Array.from({ length: 10 }, (_, i) => 11 + i));
        assert.deepEqual(at.in_turn.map((x) => x.seq), Array.from({ length: 10 }, (_, i) => 21 + i));
        assert.equal(at.earlier_count, 20, 'seqs 0–20, the ask left out');
        const own = reader_layout(page, 35, { request_seq: 30, ask: { seq: 30, ts: null, role: 'human', text: 'clip' } });
        assert.deepEqual(own.after_ask, [], 'the operator’s own prompt opened the turn: nothing sits between');
    });

    it('counts what a 30-message page did not reach, and shows the clip until it loads', () => {
        const at = reader_layout([m(80), m(81)], 81, { request_seq: 10, ask: { seq: 10, ts: 5, role: 'human', text: 'the clip', chars: 900 } });
        assert.equal(at.ask?.text, 'the clip');
        assert.equal(at.in_turn_count, 70);
        assert.equal(at.in_turn.length, 1);
    });

    it('on the needle path starts the turn at the last human message loaded', () => {
        const at = reader_layout(page, 35);
        assert.equal(at.ask?.seq, 30);
        assert.equal(at.in_turn_count, 4);
        const none = reader_layout([m(31), m(35)], 35);
        assert.equal(none.ask, null);
        assert.deepEqual(none.earlier.map((x) => x.seq), [31], 'no ask: nothing is claimed as the turn');
        assert.equal(none.in_turn_count, 0);
    });

    it('names roles for a reader, never raw', () => {
        assert.deepEqual(['human', 'user', 'agent', 'peer', 'machine', 'parent', 'weird'].map((r) => role_head(r)), ['YOU', 'YOU', 'AGENT', 'RACK', 'SYSTEM', 'LEAD', 'SYSTEM']);
        // A turn's opener heads by who opened the turn: a rack-typed prompt keeps the human role (relay.py).
        assert.equal(role_head('human', 'relay'), 'RACK');
        assert.equal(role_head('human', 'peer'), 'RACK');
        assert.equal(role_head('human', 'human'), 'YOU');
        assert.equal(role_head('human', 'command'), 'YOU');
        assert.equal(role_head('machine', 'task'), 'SYSTEM');
        assert.equal(woke_text('human'), null);
        assert.equal(woke_text('command'), null);
        assert.equal(woke_text(null), null);
        assert.equal(woke_text('task'), 'a background report');
        assert.equal(woke_text('peer'), "another agent's message");
        assert.equal(woke_text('relay'), 'the rack');
    });

    it('says plainly that a woken turn was not the operator\'s prompt, and what woke it', () => {
        // An invented opener of a turn a background check woke, in the shape the ledger keeps.
        const hook = '<task-notification>\n<summary>The test run failed</summary>\n</task-notification>\n<system-reminder>\nStop hook blocking error…';
        const task = wake_note('task', hook);
        assert.equal(task?.tag, WAKE_TAG);
        assert.equal(task?.line, 'A background report woke the agent after its last reply: “The test run failed”');
        assert.match(task?.title ?? '', /^You sent no prompt here\..*task notification.*a turn of its own to grade/);
        // No summary to quote: the line still says who woke it, and never prints a raw tag.
        assert.equal(wake_note('task', 'no tags here')?.line, 'A background report woke the agent after its last reply');
        assert.equal(wake_note('task')?.line, 'A background report woke the agent after its last reply');
        assert.equal(wake_note('relay', hook)?.line, 'The rack woke the agent after its last reply');
        assert.equal(wake_note('peer')?.line, "Another agent's message woke the agent after its last reply");
        assert.doesNotMatch(wake_note('peer')?.title ?? '', /task notification/);
        assert.equal(wake_note('human', hook), null);
        assert.equal(wake_note(null), null);
    });

    it('quotes a task notification\'s own summary, one line and capped', () => {
        assert.equal(wake_summary('<task-notification><summary>\n  Background command\n "npm test" completed  </summary></task-notification>'), 'Background command "npm test" completed');
        assert.equal(wake_summary('<summary>outside a notification</summary>'), null);
        assert.equal(wake_summary('<task-notification><summary>   </summary></task-notification>'), null);
        assert.equal(wake_summary(null), null);
        const long = wake_summary(`<task-notification><summary>${'x'.repeat(200)}</summary></task-notification>`) ?? '';
        assert.equal(long.length, WAKE_SUMMARY_CHARS);
        assert.ok(long.endsWith('…'));
    });

    it('says what Laya read only when it differs from the message', () => {
        assert.equal(laya_read_note('a … b', 'a long message b'), 'What Laya read · 5 of 16 characters');
        assert.equal(laya_read_note(' same\n', 'same'), null);
        assert.equal(laya_read_note('x', null), null);
    });
});

describe('loading the join', () => {
    const real_fetch = globalThis.fetch;
    const real_navigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    before(() => {
        Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { permissions: { query: async () => { throw new Error('no permission API'); } } } });
        unlock_local_access();
    });
    after(() => {
        globalThis.fetch = real_fetch;
        relock_local_access();
        if (real_navigator) Object.defineProperty(globalThis, 'navigator', real_navigator);
        else Reflect.deleteProperty(globalThis, 'navigator');
    });
    function serve(replies: { status?: number; body: unknown }[]): string[] {
        const urls: string[] = [];
        globalThis.fetch = async (input) => {
            urls.push(String(input));
            const next = replies.shift();
            assert.ok(next, 'no unexpected reads');
            return new Response(JSON.stringify(next.body), { status: next.status ?? 200, headers: { 'content-type': 'application/json' } });
        };
        return urls;
    }

    it('reads every chunk and merges them', async () => {
        const decisions = Array.from({ length: HISTORY_THREADS_MAX + 1 }, (_, i) => `20261107T120000-${i.toString(16).padStart(12, '0')}`);
        const urls = serve([
            { body: answer({ decisions: { [decisions[0]]: linked() } }) },
            { body: answer({ decisions: { [decisions.at(-1) as string]: linked({ status: 'none', thread: null }) } }) },
        ]);
        const got = await load_threads({ decisions });
        assert.ok(got.ok);
        assert.equal(urls.length, 2);
        assert.equal(Object.keys(got.data.decisions).length, 2);
        assert.deepEqual(await load_threads({}), { ok: true, data: NO_THREADS }, 'nothing to ask: no read');
    });

    it('an older bridge (404) and an unreadable answer fall back, typed', async () => {
        serve([{ status: 404, body: { error: 'not found' } }]);
        const old = await load_threads({ decisions: [D1] });
        assert.equal(!old.ok && old.reason, 'old_bridge');
        serve([{ body: { decisions: 'nope' } }]);
        const bad = await load_threads({ decisions: [D1] });
        assert.equal(!bad.ok && bad.reason, 'unavailable');
    });

    it('a linked turn reads its page by position, never by search', async () => {
        const session = { file_id: 7, session_id: C1, source: 'claude', profile: null, kind: 'main', title: 'T', cwd: null };
        const urls = serve([{ body: { session, messages: [message(39, 'human'), message(40), message(41)], first_user: null, before: 39, after: null, indexed_at: null } }]);
        const found = await load_linked_context({ file_id: 7, closing_seq: 40 });
        assert.ok(found.ok);
        assert.deepEqual(found.messages.map((x) => x.seq), [39, 40], 'nothing after the closing');
        assert.equal(found.closing_seq, 40);
        assert.equal(new URL(urls[0]).pathname, '/ledger/conversation');
        assert.equal(new URL(urls[0]).searchParams.get('before'), '41');
    });
});
