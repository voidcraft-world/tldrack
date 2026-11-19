import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import {
    create_decision_secret_cache,
    decision_choices,
    decision_notice,
    decision_outcome,
    decision_route,
    decision_stance,
    defer_presets,
    deferred_count,
    handle_lead_action,
    launch_lead,
    led_run,
    open_decisions,
    parse_decision_request,
    parse_lead_launch_request,
    parse_lead_state,
    post_decision,
    run_progress,
    with_decision_secret,
    type Decision,
    type LeadRun,
    type SecretResult,
} from '../../src/agent-rack/lead-client.ts';
import { DECISION_SECRET_HEADER } from '@tldrack/schemas/lead.ts';
import { parse_session_row } from '../../src/engine/terminal/terminal-status-logic.ts';
import { build_rack_payload } from '../../src/engine/terminal/rack-view.ts';

const SECRET = 'a'.repeat(32);
const SECRET_2 = 'b'.repeat(32);

function decision(overrides: Partial<Decision> = {}): Decision {
    return {
        key: 'd-1', source: 'lead', session: 'lead-1', run_id: 'R7', title: 'Which store?', detail: '',
        options: null, opened_at: 100, defer_until: null, deferred: false, blocking: false,
        assumption: 'sqlite', state: 'open', steps: ['S2'],
        ...overrides,
    };
}

function step(step_id: string, state: LeadRun['steps'][number]['state']): LeadRun['steps'][number] {
    return {
        step_id, title: step_id, kind: 'change', lane: 'claude', state, parked: false, worker_session: null,
        worker_live: false, profile: null, branch: null, pr_url: null, ci: null,
    };
}

function run(overrides: Partial<LeadRun> = {}): LeadRun {
    return {
        id: 'R7', title: 'Lead launch', state: 'open', lead_session: 'lead-1', lead_live: true, profile: 'claude-a',
        plan_path: null, created_at: 1, steps: [], decisions: [],
        ...overrides,
    };
}

function json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status });
}

/** A fake fetch that answers in order and records what was asked. Each test uses its own origin:
 *  the secret cache is per document, keyed by URL. */
function fake_fetch(answers: Array<(url: string, init?: RequestInit) => Response>) {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const impl = async (input: Parameters<typeof fetch>[0], init?: RequestInit): Promise<Response> => {
        const url = String(input);
        calls.push({ url, init });
        const answer = answers.shift();
        if (!answer) throw new Error(`unexpected fetch ${url}`);
        return answer(url, init);
    };
    return { impl, calls };
}

let origin_count = 0;
function fresh_base(): (path: string) => string {
    origin_count += 1;
    return (path) => `http://127.0.0.${origin_count}:7682${path}`;
}

describe('parse_lead_state', () => {
    test('anything that is not a lead document is "no capability"', () => {
        assert.equal(parse_lead_state(null), null);
        assert.equal(parse_lead_state({}), null);
        assert.equal(parse_lead_state({ runs: 'no' }), null);
    });

    test('a bad run is dropped alone', () => {
        assert.deepEqual(parse_lead_state({ runs: [run(), { id: 'broken' }] }), { runs: [run()] });
    });
});

describe('the reads both surfaces share', () => {
    test('led_run finds the run a session row names, and nothing for a row that leads none', () => {
        const state = { runs: [run(), run({ id: 'R8' })] };
        assert.equal(led_run(state, { run: 'R8', open_decisions: 0 })?.id, 'R8');
        assert.equal(led_run(state, { run: null, open_decisions: 0 }), null);
        assert.equal(led_run(state, undefined), null);
        assert.equal(led_run(undefined, { run: 'R7', open_decisions: 1 }), null);
    });

    test('open decisions leave out the deferred and the closed; deferred are counted', () => {
        const r = run({ decisions: [
            decision({ key: 'a' }),
            decision({ key: 'b', deferred: true, defer_until: 999 }),
            decision({ key: 'c', state: 'answered' }),
        ] });
        assert.deepEqual(open_decisions(r).map((d) => d.key), ['a']);
        assert.equal(deferred_count(r), 1);
    });

    test('stance: BLOCKS with its steps, or ASSUMING with the assumption', () => {
        assert.deepEqual(decision_stance(decision({ blocking: true, steps: ['S2', 'S4'] })), { blocking: true, text: 'BLOCKS S2 S4' });
        assert.deepEqual(decision_stance(decision({ blocking: true, steps: [] })), { blocking: true, text: 'BLOCKS' });
        assert.deepEqual(decision_stance(decision({ assumption: 'sqlite' })), { blocking: false, text: 'ASSUMING sqlite' });
    });

    test('choices: KEEP only where an assumption can hold; options, else ANSWER in words', () => {
        assert.deepEqual(decision_choices(decision()), { keep: true, options: [], text: true });
        assert.deepEqual(decision_choices(decision({ blocking: true, assumption: null, options: ['A', 'B'] })), { keep: false, options: ['A', 'B'], text: false });
        assert.equal(decision_choices(decision({ assumption: null })).keep, false, 'nothing to keep');
    });

    test('progress: closed out of the steps that will ever close', () => {
        const r = run({ steps: [step('S1', 'closed'), step('S2', 'abandoned'), step('S3', 'working'), step('S4', 'closed')] });
        assert.deepEqual(run_progress(r), { closed: 2, total: 3, abandoned: 1 });
    });
});

describe('defer_presets', () => {
    const epoch = (at: Date) => Math.floor(at.getTime() / 1000);

    test('afternoon: an hour, tonight 18:00, tomorrow 09:00', () => {
        const now = new Date(2026, 10, 4, 15, 30);
        const [hour, evening, morning] = defer_presets(now);
        assert.equal(hour.until, epoch(now) + 3600);
        assert.equal(evening.label, 'TONIGHT');
        assert.equal(evening.until, epoch(new Date(2026, 10, 4, 18, 0)));
        assert.equal(morning.label, 'TOMORROW');
        assert.equal(morning.until, epoch(new Date(2026, 10, 5, 9, 0)));
    });

    test('past 18:00, the evening preset moves to tomorrow and says so', () => {
        const [, evening, morning] = defer_presets(new Date(2026, 10, 4, 19, 5));
        assert.equal(evening.label, '18:00 TMRW');
        assert.equal(evening.until, epoch(new Date(2026, 10, 5, 18, 0)));
        assert.equal(morning.until, epoch(new Date(2026, 10, 5, 9, 0)));
    });

    test('before 04:00, "tomorrow morning" is this morning', () => {
        const [, evening, morning] = defer_presets(new Date(2026, 10, 5, 1, 30));
        assert.equal(morning.until, epoch(new Date(2026, 10, 5, 9, 0)));
        assert.equal(evening.label, 'TONIGHT');
    });

    test('every preset is in the future and a whole second', () => {
        const now = new Date(2026, 10, 4, 17, 59, 30);
        for (const preset of defer_presets(now)) {
            assert.ok(preset.until > epoch(now), preset.key);
            assert.ok(Number.isInteger(preset.until), preset.key);
        }
    });
});

describe('parse_decision_request', () => {
    const parse = (value: unknown) => parse_decision_request(JSON.stringify(value));

    test('KEEP carries no words; an option or a text answer must', () => {
        assert.deepEqual(parse({ verb: 'answer', key: 'd', kind: 'keep', answer: 'stray' }), { verb: 'answer', key: 'd', kind: 'keep' });
        assert.deepEqual(parse({ verb: 'answer', key: 'd', kind: 'text', answer: '  sqlite  ' }), { verb: 'answer', key: 'd', kind: 'text', answer: 'sqlite' });
        assert.equal(parse({ verb: 'answer', key: 'd', kind: 'option' }), null);
        assert.equal(parse({ verb: 'answer', key: 'd', kind: 'text', answer: '   ' }), null);
    });

    test('DEFER takes whole epoch seconds; DISMISS needs a reason', () => {
        assert.deepEqual(parse({ verb: 'defer', key: 'd', until: 1_800_000_000 }), { verb: 'defer', key: 'd', until: 1_800_000_000 });
        assert.equal(parse({ verb: 'defer', key: 'd', until: 1.5 }), null);
        assert.deepEqual(parse({ verb: 'dismiss', key: 'd', reason: ' not mine ' }), { verb: 'dismiss', key: 'd', reason: 'not mine' });
        assert.equal(parse({ verb: 'dismiss', key: 'd', reason: ' ' }), null);
    });

    test('anything else is not a request', () => {
        assert.equal(parse_decision_request(undefined), null);
        assert.equal(parse_decision_request('{not json'), null);
        assert.equal(parse({ verb: 'approve', key: 'd' }), null);
        assert.equal(parse({ verb: 'answer', key: '', kind: 'keep' }), null);
    });

    test('the route is the verb, and the body is the rest', () => {
        assert.deepEqual(decision_route({ verb: 'defer', key: 'd', until: 5 }), { path: '/inbox/defer', body: { key: 'd', until: 5 } });
    });
});

describe('the decision secret', () => {
    test('one fetch per document; a failure is not cached; invalidate asks again', async () => {
        let fetched = 0;
        const answers: SecretResult[] = [{ ok: false, error: 'down' }, { ok: true, secret: SECRET }, { ok: true, secret: SECRET_2 }];
        const cache = create_decision_secret_cache(async () => {
            fetched += 1;
            return answers.shift()!;
        });
        assert.deepEqual(await cache.get(), { ok: false, error: 'down' });
        assert.deepEqual(await cache.get(), { ok: true, secret: SECRET });
        assert.deepEqual(await cache.get(), { ok: true, secret: SECRET });
        assert.equal(fetched, 2);
        cache.invalidate();
        assert.deepEqual(await cache.get(), { ok: true, secret: SECRET_2 });
    });

    test('a refusal refetches the secret once and sends once more — never a loop', async () => {
        const secrets = [SECRET, SECRET_2];
        const cache = create_decision_secret_cache(async () => ({ ok: true, secret: secrets.shift() ?? 'none' }));
        const sent: string[] = [];
        const result = await with_decision_secret(cache, async (secret) => {
            sent.push(secret);
            return 403;
        }, (status) => status === 403);
        assert.deepEqual(sent, [SECRET, SECRET_2]);
        assert.deepEqual(result, { ok: true, result: 403 });
    });

    test('no secret, nothing sent', async () => {
        const cache = create_decision_secret_cache(async () => ({ ok: false, error: 'Bridge not reachable' }));
        let sends = 0;
        const result = await with_decision_secret(cache, async () => { sends += 1; return 200; }, () => false);
        assert.deepEqual(result, { ok: false, error: 'Bridge not reachable' });
        assert.equal(sends, 0);
    });
});

describe('post_decision', () => {
    test('fetches the secret, then posts the verb with it', async () => {
        const base = fresh_base();
        const { impl, calls } = fake_fetch([
            () => json(200, { secret: SECRET }),
            () => json(200, { ok: true, decision: {}, reopened: ['S2', 'S4'] }),
        ]);
        const result = await post_decision(base, { verb: 'answer', key: 'd', kind: 'option', answer: 'JSON file' }, impl);
        assert.deepEqual(result, { ok: true, reopened: ['S2', 'S4'] });
        assert.equal(calls[0].url, base('/decision-secret'));
        assert.equal(calls[1].url, base('/inbox/answer'));
        assert.equal((calls[1].init?.headers as Record<string, string> | undefined)?.[DECISION_SECRET_HEADER], SECRET);
        assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { key: 'd', kind: 'option', answer: 'JSON file' });
        assert.equal(decision_notice(result), 'answered — reopened S2, S4');
    });

    test('a restarted bridge: 403, a fresh secret, one retry', async () => {
        const base = fresh_base();
        const { impl, calls } = fake_fetch([
            () => json(200, { secret: SECRET }),
            () => json(403, { error: 'bad secret' }),
            () => json(200, { secret: SECRET_2 }),
            () => json(200, { ok: true, reopened: [] }),
        ]);
        const result = await post_decision(base, { verb: 'dismiss', key: 'd', reason: 'not mine' }, impl);
        assert.equal(result.ok, true);
        assert.equal((calls[3].init?.headers as Record<string, string> | undefined)?.[DECISION_SECRET_HEADER], SECRET_2);
        assert.equal(decision_notice(result), null, 'nothing reopened, nothing to say');
    });

    test('an older bridge says restart; a refusal keeps the bridge’s words', () => {
        assert.deepEqual(decision_outcome(404, null), { ok: false, error: 'Restart the bridge to use LEAD', reopened: [] });
        assert.deepEqual(decision_outcome(409, { ok: false, error: 'already answered' }), { ok: false, error: 'already answered', reopened: [] });
        assert.deepEqual(decision_outcome(200, { ok: false, error: 'stale' }), { ok: false, error: 'stale', reopened: [] });
        assert.equal(decision_notice(decision_outcome(0, null)), 'decision not recorded — Bridge not reachable');
    });
});

describe('LAUNCH › LEAD', () => {
    test('the action value is {profile, resume?} and nothing else', () => {
        assert.deepEqual(parse_lead_launch_request('{"profile":"auto"}'), { profile: 'auto' });
        assert.deepEqual(parse_lead_launch_request('{"profile":"auto","resume":"R7"}'), { profile: 'auto', resume: 'R7' });
        assert.equal(parse_lead_launch_request('{"profile":""}'), null);
        assert.equal(parse_lead_launch_request('auto'), null);
        assert.equal(parse_lead_launch_request(undefined), null);
    });

    test('focus is handed over only after the bridge answers 201, with the session it named', async () => {
        const order: string[] = [];
        const { impl, calls } = fake_fetch([() => {
            order.push('bridge answered');
            return json(201, { ok: true, session: 'lead-3', profile: 'claude-a', run: null });
        }]);
        await launch_lead('{"profile":"auto"}', {
            base_url: fresh_base(),
            fetch_impl: impl,
            on_launched: (session) => order.push(`focus ${session}`),
            on_failure: (message) => order.push(`fail ${message}`),
        });
        assert.deepEqual(order, ['bridge answered', 'focus lead-3']);
        assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { profile: 'auto' });
    });

    test('a refusal lands as one line; a malformed value sends nothing', async () => {
        const failures: string[] = [];
        const hooks = (impl: ReturnType<typeof fake_fetch>['impl']) => ({
            base_url: fresh_base(), fetch_impl: impl, on_launched: () => assert.fail('no focus'), on_failure: (m: string) => failures.push(m),
        });
        await launch_lead('{"profile":"auto"}', hooks(fake_fetch([() => json(409, { ok: false, error: 'not a subscription login' })]).impl));
        await launch_lead('{"profile":"auto"}', hooks(fake_fetch([() => json(404, {})]).impl));
        const idle = fake_fetch([]);
        await launch_lead('nope', hooks(idle.impl));
        assert.deepEqual(failures, ['LEAD not started — not a subscription login', 'LEAD not started — Restart the bridge to use LEAD']);
        assert.equal(idle.calls.length, 0);
    });

    function owner(events: string[], impl?: ReturnType<typeof fake_fetch>['impl']) {
        return {
            base_url: fresh_base(),
            fetch_impl: impl,
            now: () => 42,
            island_focus: {
                arm: (session: string, at: number) => events.push(`arm ${session} @${at}`),
                cancel: () => events.push('cancel'),
            },
            launch_notice: { show: (text: string) => events.push(`notice ${text}`), clear: () => events.push('clear') },
            tick_rack: () => events.push('tick rack'),
            tick_lead: () => events.push('tick lead'),
        };
    }
    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    test('handle_lead_action › lead: cancel and clear at once, arm the hand-off only after the 201', async () => {
        const events: string[] = [];
        const { impl } = fake_fetch([() => json(201, { ok: true, session: 'lead-4', profile: 'claude-a', run: null })]);
        assert.equal(handle_lead_action('lead', '{"profile":"claude-a"}', owner(events, impl)), true);
        assert.deepEqual(events, ['cancel', 'clear'], 'synchronous, before any answer');
        await settle();
        assert.deepEqual(events, ['cancel', 'clear', 'arm lead-4 @42', 'tick rack', 'tick lead']);
    });

    test('handle_lead_action › lead_decision: the notice, then a fresh read', async () => {
        const events: string[] = [];
        const { impl } = fake_fetch([() => json(200, { secret: SECRET }), () => json(200, { ok: true, reopened: ['S3'] })]);
        const request = JSON.stringify({ verb: 'answer', key: 'd', kind: 'keep' });
        assert.equal(handle_lead_action('lead_decision', request, owner(events, impl)), true);
        await settle();
        await settle();
        assert.deepEqual(events, ['notice answered — reopened S3', 'tick lead']);
        assert.equal(handle_lead_action('select', 'x', owner(events)), false);
        assert.equal(events.length, 2, 'any other action touches nothing');
    });
});

describe('the snapshot row fields', () => {
    test('lead and worker are kept when well formed and dropped when not', () => {
        const row = parse_session_row({ session: 'lead-1', state: 'free', lead: { run: 'R7', open_decisions: 2 }, worker: { run: 'R7', step: 'S2' } })!;
        assert.deepEqual(row.lead, { run: 'R7', open_decisions: 2 });
        assert.deepEqual(row.worker, { run: 'R7', step: 'S2' });
        const bad = parse_session_row({ session: 'lead-1', state: 'free', lead: { run: 7 }, worker: 'S2' })!;
        assert.equal('lead' in bad, false);
        assert.equal('worker' in bad, false);
    });

    test('a bridge one release behind still names its workers and its runs the old way', () => {
        // Legacy (step 6.8): `crew` on /snapshot, `crew_*` keys and `ship` / `scout` kinds on /lead/state.
        const row = parse_session_row({ session: 'w-1', state: 'busy', crew: { run: 'R7', step: 'S2' } })!;
        assert.deepEqual(row.worker, { run: 'R7', step: 'S2' });
        assert.equal('crew' in row, false);
        const legacy_step = { ...step('S1', 'working'), kind: 'scout', crew_session: 'w-1', crew_live: true } as Record<string, unknown>;
        delete legacy_step.worker_session;
        delete legacy_step.worker_live;
        const state = parse_lead_state({
            runs: [{ ...run({ steps: [] }), crew_model: 'opus', steps: [legacy_step] }],
            // step 6.8: the models block's legacy key
            models: { model: 'fable', crew_model: 'opus', families: ['opus', 'fable'] },
        })!;
        assert.deepEqual(
            [state.runs[0].steps[0].kind, state.runs[0].steps[0].worker_session, state.runs[0].steps[0].worker_live],
            ['report', 'w-1', true],
        );
        assert.equal(state.runs[0].worker_model, 'opus');
        assert.equal(state.models?.worker_model, 'opus');
    });

    test('the payload carries the row’s lead and the run state; absent stays absent', () => {
        const row = { ...parse_session_row({ session: 'lead-1', state: 'free', lead: { run: 'R7', open_decisions: 1 } })!, display: 'free' as const };
        const with_lead = build_rack_payload({ rows: [row], focused: 'lead-1', reachable: true, max_sessions: 20, lead: { runs: [run()] } });
        assert.deepEqual(with_lead.sessions[0].lead, { run: 'R7', open_decisions: 1 });
        assert.deepEqual(with_lead.lead, { runs: [run()] });
        const without = build_rack_payload({ rows: [row], focused: 'lead-1', reachable: true, max_sessions: 20 });
        assert.equal('lead' in without, false);
    });
});
