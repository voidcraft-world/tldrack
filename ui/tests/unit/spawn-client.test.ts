import test from 'node:test';
import assert from 'node:assert/strict';
import {
    classify_spawn_status,
    create_launch_notice,
    launch_new,
    spawn_failure_message,
    spawn_session,
    type LaunchNewHooks,
} from '../../src/agent-rack/spawn-client.ts';
import { parse_profiles, type ProfileInfo } from '../../src/engine/terminal/terminal-status-logic.ts';

const BASE = (path: string): string => `http://127.0.0.1:7682${path}`;

const PROFILES: ProfileInfo[] = parse_profiles([
    { id: 'claude-a', label: 'A', kind: 'claude', default: true },
    { id: 'codex-a', label: 'CODEX A', kind: 'codex' },
    { id: 'gone', label: 'GONE', kind: 'codex', cli_found: false },
])!;

/** A fetch that answers each call from `statuses` in turn and records bodies. */
function scripted(statuses: Array<number | 'throw'>, error_body?: unknown) {
    const bodies: Array<{ name: string; profile: string }> = [];
    let i = 0;
    const fetch_impl = (async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        const status = statuses[Math.min(i, statuses.length - 1)];
        i += 1;
        if (status === 'throw') throw new Error('unreachable');
        return {
            ok: status < 400,
            status,
            json: async () => error_body ?? { ok: status < 400 },
        } as unknown as Response;
    }) as unknown as typeof fetch;
    return { fetch_impl, bodies };
}

/** Records every hook call in order — the ORDER is what is under test. */
function recording(overrides: Partial<LaunchNewHooks> = {}) {
    const events: string[] = [];
    const hooks: LaunchNewHooks = {
        base_url: BASE,
        known_sessions: () => ['vc-1'],
        profiles: () => PROFILES,
        set_pending: (name, profile) => events.push(`pending:${name ?? '-'}:${profile ?? '-'}`),
        on_spawned: (name) => events.push(`spawned:${name}`),
        legacy_new: () => events.push('legacy'),
        on_failure: (message) => events.push(`failure:${message}`),
        ...overrides,
    };
    return { hooks, events };
}

test('classify_spawn_status keeps 404 meaning "this bridge predates the route"', () => {
    assert.equal(classify_spawn_status(201), 'ok');
    assert.equal(classify_spawn_status(409), 'taken');
    assert.equal(classify_spawn_status(400), 'invalid');
    assert.equal(classify_spawn_status(503), 'cli_missing');
    assert.equal(classify_spawn_status(403), 'forbidden');
    assert.equal(classify_spawn_status(404), 'unsupported');
    assert.equal(classify_spawn_status(500), 'error');
});

test('spawn_session posts {name, profile} and reads the bridge error line', async () => {
    const { fetch_impl, bodies } = scripted([503], { error: 'codex not found' });
    const outcome = await spawn_session(BASE, { name: 'vc-2', profile: 'codex-a' }, fetch_impl);
    assert.deepEqual(bodies, [{ name: 'vc-2', profile: 'codex-a' }]);
    assert.deepEqual(outcome, { result: 'cli_missing', error: 'codex not found' });
});

test('spawn_session is unreachable, never a throw, on a transport failure', async () => {
    const { fetch_impl } = scripted(['throw']);
    assert.deepEqual(await spawn_session(BASE, { name: 'vc-2', profile: 'codex-a' }, fetch_impl), {
        result: 'unreachable',
    });
});

test('201: the pending row comes FIRST and focus only AFTER the bridge answers', async () => {
    // Focus first would point the pane at a bare `?arg=` and let ttyd's
    // attach-or-create build a shell under the name before /spawn lands.
    const { fetch_impl, bodies } = scripted([201]);
    const { hooks, events } = recording({ fetch_impl });
    await launch_new('codex-a', hooks);
    assert.deepEqual(events, ['pending:vc-2:codex-a', 'spawned:vc-2']);
    assert.deepEqual(bodies, [{ name: 'vc-2', profile: 'codex-a' }]);
});

test('409 retries ONCE with the taken name excluded', async () => {
    const { fetch_impl, bodies } = scripted([409, 201]);
    const { hooks, events } = recording({ fetch_impl });
    await launch_new('codex-a', hooks);
    assert.deepEqual(bodies.map((b) => b.name), ['vc-2', 'vc-3']);
    assert.deepEqual(events, ['pending:vc-2:codex-a', 'pending:vc-3:codex-a', 'spawned:vc-3']);
});

test('two 409s give up with a message rather than looping', async () => {
    const { fetch_impl, bodies } = scripted([409, 409]);
    const { hooks, events } = recording({ fetch_impl });
    await launch_new('codex-a', hooks);
    assert.equal(bodies.length, 2);
    assert.equal(events.at(-2), 'pending:-:-');
    assert.match(events.at(-1)!, /^failure:CODEX A/);
});

test('404 drops the pending row and runs the legacy + NEW unchanged', async () => {
    const { fetch_impl } = scripted([404]);
    const { hooks, events } = recording({ fetch_impl });
    await launch_new('codex-a', hooks);
    assert.deepEqual(events, ['pending:vc-2:codex-a', 'pending:-:-', 'legacy']);
});

test('a bridge with no profiles never calls /spawn at all', async () => {
    const { fetch_impl, bodies } = scripted([201]);
    for (const profiles of [undefined, []]) {
        const { hooks, events } = recording({ fetch_impl, profiles: () => profiles });
        await launch_new('codex-a', hooks);
        assert.deepEqual(events, ['legacy']);
    }
    assert.equal(bodies.length, 0);
});

test('an unknown, null or uninstalled pick launches the DEFAULT profile, never a bare shell', async () => {
    for (const pick of ['nope', null, 'gone']) {
        const { fetch_impl, bodies } = scripted([201]);
        const { hooks } = recording({ fetch_impl });
        await launch_new(pick, hooks);
        assert.equal(bodies[0].profile, 'claude-a', String(pick));
    }
});

test('AUTO goes to the bridge as "auto"; its row wears the current pick meanwhile', async () => {
    const { fetch_impl, bodies } = scripted([201]);
    const { hooks, events } = recording({
        fetch_impl,
        launch_pick: () => ({ id: 'codex-a', label: 'CODEX A', reason: 'CODEX A · 53% left · idle', spent: false }),
    });
    await launch_new('auto', hooks);
    assert.equal(bodies[0].profile, 'auto');
    assert.deepEqual(events, ['pending:vc-2:codex-a', 'spawned:vc-2']);
});

test('AUTO on a bridge that cannot pick falls back like an unknown id', async () => {
    for (const launch_pick of [undefined, () => undefined, () => ({ id: null, label: null, reason: '', spent: false })]) {
        const { fetch_impl, bodies } = scripted([201]);
        const { hooks } = recording({ fetch_impl, launch_pick });
        await launch_new('auto', hooks);
        assert.equal(bodies[0].profile, 'claude-a');
    }
});

test('an AUTO refusal names the bridge’s reason, not a CLI', async () => {
    const { fetch_impl } = scripted([503], { error: 'no launchable agent profile for auto' });
    const { hooks, events } = recording({
        fetch_impl,
        launch_pick: () => ({ id: 'codex-a', label: 'CODEX A', reason: '', spent: false }),
    });
    await launch_new('auto', hooks);
    assert.equal(events.at(-1), 'failure:AUTO: no launchable agent profile for auto');
});

test('a refusal surfaces as one line naming the profile', async () => {
    const { fetch_impl } = scripted([503], { error: 'x' });
    const { hooks, events } = recording({ fetch_impl });
    await launch_new('codex-a', hooks);
    assert.deepEqual(events, ['pending:vc-2:codex-a', 'pending:-:-', 'failure:CODEX A: CLI not found on this machine']);
    assert.equal(spawn_failure_message('A', { result: 'error', error: 'boom' }), 'A: boom');
});

test('the launch notice expires on its own and re-emits each time', () => {
    let emitted = 0;
    let fire: (() => void) | null = null;
    const real = globalThis.setTimeout;
    globalThis.setTimeout = ((fn: () => void) => {
        fire = fn;
        return 1 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout;
    try {
        const notice = create_launch_notice(() => {
            emitted += 1;
        });
        notice.show('A: launch failed');
        assert.equal(notice.text(), 'A: launch failed');
        assert.equal(emitted, 1);
        fire!();
        assert.equal(notice.text(), null);
        assert.equal(emitted, 2);
        notice.clear(); // already empty — no spurious re-emit
        assert.equal(emitted, 2);
    } finally {
        globalThis.setTimeout = real;
    }
});

test('directory launches pin the cwd across the session-name retry', async () => {
    const { fetch_impl, bodies } = scripted([409, 201]);
    const { hooks } = recording({ fetch_impl });
    await launch_new(JSON.stringify({ profile: 'codex-a', cwd: '/tmp/second project' }), hooks);
    assert.deepEqual(bodies, [
        { name: 'vc-2', profile: 'codex-a', cwd: '/tmp/second project' },
        { name: 'vc-3', profile: 'codex-a', cwd: '/tmp/second project' },
    ]);
});

test('directory launch cannot fall back to a shell in a different project', async () => {
    for (const profiles of [PROFILES, undefined]) {
        const { fetch_impl } = scripted([404]);
        const { hooks, events } = recording({ fetch_impl, profiles: () => profiles });
        await launch_new(JSON.stringify({ profile: null, cwd: '/tmp/project' }), hooks);
        assert.ok(!events.includes('legacy'));
        assert.match(events.at(-1)!, /Restart the bridge/);
    }
});

test('malformed directory requests do not launch a default by accident', async () => {
    for (const value of ['{broken', '{"profile":null}', '{"profile":123,"cwd":"/tmp/project"}', '{"profile":null,"cwd":"relative"}']) {
        const { fetch_impl, bodies } = scripted([201]);
        const { hooks, events } = recording({ fetch_impl });
        await launch_new(value, hooks);
        assert.equal(bodies.length, 0);
        assert.deepEqual(events, ['failure:Invalid launch directory request']);
    }
});
