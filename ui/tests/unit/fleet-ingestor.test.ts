import test from 'node:test';
import assert from 'node:assert/strict';
import {
    create_fleet_ingestor,
    type FleetIngestorDeps,
    type FleetNoticeSets,
    type IngestorFlags,
} from '../../src/engine/terminal/fleet-ingestor.ts';
import type { PortSnapshot, SessionStatus } from '../../src/engine/terminal/terminal-status-logic.ts';

const FLAGS: IngestorFlags = {
    poll_interval_ms: 3000,
    debounce_polls: 2,
    hide_dead_sessions: true,
    snapshot_reprobe_ticks: 10,
    sse_enabled: false,
    busy_stale_after_s: 600,
    title_hint_enabled: true,
    fake_cwds: {},
    fake_background: {},
};

function status(overrides: Partial<SessionStatus> & { session: string }): SessionStatus {
    return { state: 'free', age_seconds: 1, message: null, title: null, attached: false, ...overrides };
}

interface Route {
    status?: number;
    body?: unknown;
    throws?: boolean;
}

function harness(options: {
    routes?: Record<string, Route | (() => Route)>;
    flags?: Partial<IngestorFlags>;
    deps?: Partial<FleetIngestorDeps>;
} = {}) {
    const calls: string[] = [];
    const snapshots: PortSnapshot[] = [];
    const notices: FleetNoticeSets[] = [];
    let watching = new Set<string>();
    let tracked: string[] = [];
    const flags: IngestorFlags = { ...FLAGS, ...options.flags };
    let routes = options.routes ?? {};

    const ingestor = create_fleet_ingestor({
        base_url: (path) => `http://127.0.0.1:7682${path}`,
        flags: () => flags,
        is_watching: (session) => watching.has(session),
        tracked_sessions: () => tracked,
        allow_legacy_fallback: true,
        on_snapshot: (snapshot) => snapshots.push(snapshot),
        on_notice: (sets) => notices.push(sets),
        fetch_impl: (async (url: string) => {
            const path = String(url).replace('http://127.0.0.1:7682', '');
            calls.push(path);
            const key = Object.keys(routes).find((k) => path.startsWith(k));
            const raw = key ? routes[key] : undefined;
            const route = typeof raw === 'function' ? raw() : raw;
            if (!route || route.throws) throw new Error('unreachable');
            return {
                ok: (route.status ?? 200) < 400,
                status: route.status ?? 200,
                json: async () => route.body ?? {},
            } as unknown as Response;
        }) as unknown as typeof fetch,
        set_interval: (() => 0) as never,
        clear_interval: () => {},
        event_source: () => null, // no SSE in tests unless a case wants it
        ...options.deps,
    });

    return {
        ingestor,
        calls,
        snapshots,
        notices,
        flags,
        last: () => snapshots[snapshots.length - 1],
        set_routes: (next: Record<string, Route | (() => Route)>) => {
            routes = next;
        },
        set_watching: (sessions: string[]) => {
            watching = new Set(sessions);
        },
        set_tracked: (sessions: string[]) => {
            tracked = sessions;
        },
    };
}

const snapshot_route = (sessions: SessionStatus[]): Record<string, Route> => ({
    '/snapshot': { body: { sessions } },
});

test('a snapshot becomes rows, sorted by session name', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'zeta' }), status({ session: 'alpha' })]) });
    await h.ingestor.tick();
    assert.deepEqual(h.last().sessions.map((s) => s.session), ['alpha', 'zeta']);
    assert.equal(h.last().reachable, true);
});

test('THE COLLISION SET KEEPS DEAD ROWS even though the snapshot hides them', async () => {
    // A collision set built from the rows the rack SHOWS is live-only, and
    // `+ NEW` could then hand out a name the bridge still had a store row for.
    const h = harness({
        routes: snapshot_route([status({ session: 'alive' }), status({ session: 'ghost', live: false })]),
    });
    await h.ingestor.tick();
    assert.deepEqual(h.last().sessions.map((s) => s.session), ['alive'], 'the rack hides the dead row');
    assert.deepEqual(h.ingestor.known_sessions().sort(), ['alive', 'ghost'], 'but + NEW still sees it');
});

test('hide_dead_sessions off shows the dead row in both', async () => {
    const h = harness({
        flags: { hide_dead_sessions: false },
        routes: snapshot_route([status({ session: 'alive' }), status({ session: 'ghost', live: false })]),
    });
    await h.ingestor.tick();
    assert.deepEqual(h.last().sessions.map((s) => s.session).sort(), ['alive', 'ghost']);
    assert.deepEqual(h.ingestor.known_sessions().sort(), ['alive', 'ghost']);
});

test('the FIRST tick raises no notice — entry must not replay the rack as alerts', async () => {
    const h = harness({
        routes: snapshot_route([status({ session: 'alpha', state: 'attention', message: 'help' })]),
    });
    await h.ingestor.tick();
    assert.deepEqual(h.notices, []);
    await h.ingestor.tick();
    assert.equal(h.notices.length, 1);
});

test('attention must survive debounce_polls before it enters the notice set', async () => {
    // `attention` commits INSTANTLY on the upgrade branch, so testing
    // `display === 'attention'` alone is a tautology that alerts on poll one —
    // exactly the flicker debounce_polls exists to absorb.
    const h = harness({
        flags: { debounce_polls: 3 },
        routes: snapshot_route([status({ session: 'alpha', state: 'attention', message: 'help' })]),
    });
    await h.ingestor.tick(); // suppressed (first tick)
    await h.ingestor.tick(); // committed_polls = 2 — not yet
    assert.deepEqual(h.notices[0].attention, []);
    await h.ingestor.tick(); // committed_polls = 3 — now
    assert.deepEqual(h.notices[1].attention, [{ session: 'alpha', message: 'help' }]);
});

test('an unreachable bridge drives every tracked session to unknown', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'alpha', state: 'busy' })]) });
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'busy');

    h.set_tracked(['alpha']);
    h.set_routes({ '/snapshot': { throws: true } });
    for (let i = 0; i < 5; i += 1) await h.ingestor.tick();
    assert.equal(h.last().reachable, false);
    assert.equal(h.ingestor.trackers().get('alpha')?.committed, 'unknown');
});

test('a session finishing WHILE WATCHED never latches done', async () => {
    const busy = snapshot_route([status({ session: 'alpha', state: 'busy' })]);
    const free = snapshot_route([status({ session: 'alpha', state: 'free' })]);

    const watched = harness({ routes: busy, flags: { debounce_polls: 1 } });
    watched.set_watching(['alpha']);
    await watched.ingestor.tick();
    watched.set_routes(free);
    await watched.ingestor.tick();
    assert.equal(watched.last().sessions[0].display, 'free');

    const away = harness({ routes: busy, flags: { debounce_polls: 1 } });
    await away.ingestor.tick();
    away.set_routes(free);
    await away.ingestor.tick();
    assert.equal(away.last().sessions[0].display, 'done', 'nobody was looking');
});

// ── an unreachable tick must not ERASE what the trackers know ───────────────
//
// `process([], false, …)` used to fall through to the "drop trackers for
// sessions that vanished" sweep, and with `tracked_sessions()` empty (which is
// how the rack page runs) that wiped every tracker in the map. One dropped poll
// therefore threw away `done_pending` (the one flag with no other source),
// restarted the attention debounce, and restarted the downgrade debounce
// mid-transition.

test('a dropped poll does not erase a done highlight', async () => {
    const h = harness({
        routes: snapshot_route([status({ session: 'alpha', state: 'busy' })]),
        flags: { debounce_polls: 1 },
    });
    await h.ingestor.tick();
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done');

    // The bridge blips — a timeout, a restart, a hidden tab losing a race.
    h.set_routes({});
    await h.ingestor.tick();
    assert.equal(h.last().reachable, false);

    // ...and comes back with the session unchanged. The highlight must still be
    // there: nothing observed it, so nothing cleared it.
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done', 'silence is not evidence of absence');
});

/** Drive a session to a latched `done` highlight, nobody watching. */
async function finished_unseen() {
    const h = harness({
        routes: snapshot_route([status({ session: 'alpha', state: 'busy' })]),
        flags: { debounce_polls: 1 },
    });
    await h.ingestor.tick();
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done');
    return h;
}

test('one flickering answer does not erase a done highlight either', async () => {
    // `live` is "this session is in list_tmux_sessions() right now", so a single
    // `tmux ls` that times out reports the whole rack dead — and with
    // hide_dead_sessions on those rows never reach the tracker loop at all.
    const h = await finished_unseen();

    h.set_routes(snapshot_route([])); // the bridge answers, alpha is missing
    await h.ingestor.tick();

    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done', 'one blink is not a departure');
});

test('a session that really is gone still drops its tracker', async () => {
    const h = await finished_unseen();

    // Sustained absence — not a blink. The grace is 3 consecutive answers.
    h.set_routes(snapshot_route([]));
    for (let i = 0; i < 4; i += 1) await h.ingestor.tick();

    // Reappearing now is a NEW session, not a resumed one.
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'free');
});

test('the absence run resets on every sighting, so a slow flapper never expires', async () => {
    const h = await finished_unseen();
    const present = snapshot_route([status({ session: 'alpha', state: 'free' })]);

    // Miss, hit, miss, hit… forever. A counter that only ever incremented would
    // drop the tracker on the third miss no matter how much time it spent alive.
    for (let i = 0; i < 6; i += 1) {
        h.set_routes(snapshot_route([]));
        await h.ingestor.tick();
        h.set_routes(present);
        await h.ingestor.tick();
    }
    assert.equal(h.last().sessions[0].display, 'done');
});

test('mark_seen clears a done highlight', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'alpha', state: 'busy' })]), flags: { debounce_polls: 1 } });
    await h.ingestor.tick();
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done');

    h.ingestor.mark_seen('alpha');
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'free');
});

test('a 404 falls back to the legacy /status ladder, once per reprobe window', async () => {
    const h = harness({
        flags: { snapshot_reprobe_ticks: 3 },
        routes: {
            '/snapshot': { status: 404 },
            '/status': { body: { session: 'alpha', state: 'busy', age_seconds: 1, message: null } },
        },
    });
    h.set_tracked(['alpha']);
    await h.ingestor.tick();
    assert.ok(h.calls.includes('/status?session=alpha'), 'probed the legacy route');
    assert.equal(h.last().reachable, true);

    // The next ticks skip /snapshot until the reprobe window elapses — a 404 on
    // every poll against an old bridge is pure noise.
    //
    // Note the OFF-BY-ONE, which is real and worth pinning: the probing tick
    // resets `ticks_since_probe` to 0 and the counter only increments on ticks
    // that SKIP, so with a window of 3 the re-probe lands on tick 5, not tick 4.
    const probes = (): number => h.calls.filter((c) => c.startsWith('/snapshot')).length;
    assert.equal(probes(), 1);
    await h.ingestor.tick(); // 2 — ticks_since_probe 0 -> 1
    await h.ingestor.tick(); // 3 — 1 -> 2
    await h.ingestor.tick(); // 4 — 2 -> 3
    assert.equal(probes(), 1, 'still riding the legacy route');
    await h.ingestor.tick(); // 5 — 3 >= 3, re-probe
    assert.equal(probes(), 2, 're-probed once the window elapsed');
});

test('allow_legacy_fallback: false REFUSES the ladder — a security policy, not a shape', async () => {
    // A bridge without /snapshot also predates the Origin guard that makes
    // remote access safe, so the phone must not talk to one at all.
    const h = harness({
        deps: { allow_legacy_fallback: false },
        routes: {
            '/snapshot': { status: 404 },
            '/status': { body: { session: 'alpha', state: 'busy', age_seconds: 1, message: null } },
        },
    });
    h.set_tracked(['alpha']);
    await h.ingestor.tick();
    assert.equal(h.calls.some((c) => c.startsWith('/status')), false, 'never asked');
    assert.equal(h.last().reachable, false);
});

test('a tracked session absent from the snapshot still advances', async () => {
    const h = harness({ routes: snapshot_route([]) });
    h.set_tracked(['ghost']);
    await h.ingestor.tick();
    assert.ok(h.ingestor.trackers().has('ghost'));
    // ...but contributes no ROW, because the bridge reported nothing about it.
    assert.deepEqual(h.last().sessions, []);
});

test('an untracked session that vanishes drops its tracker, after the absence grace', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'alpha' })]) });
    await h.ingestor.tick();
    assert.ok(h.ingestor.trackers().has('alpha'));

    h.set_routes(snapshot_route([]));
    await h.ingestor.tick();
    // One missing answer is a blink, not a departure — see the note in `process`.
    assert.ok(h.ingestor.trackers().has('alpha'), 'dropped on the first absence');

    await h.ingestor.tick();
    await h.ingestor.tick();
    assert.equal(h.ingestor.trackers().has('alpha'), false, 'or a long-lived tab leaks one per session ever seen');
});

test('fake_cwds is typeof-guarded against prototype names', async () => {
    // A session legitimately named `constructor` would otherwise pull a FUNCTION
    // off Object.prototype and blow up path splitting downstream.
    const h = harness({
        routes: snapshot_route([status({ session: 'constructor', cwd: '/real/path' })]),
    });
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].cwd, '/real/path');
});

test('fake_cwds overrides a real cwd when seeded', async () => {
    const h = harness({
        flags: { fake_cwds: { alpha: '/fake/project' } },
        routes: snapshot_route([status({ session: 'alpha', cwd: '/real' })]),
    });
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].cwd, '/fake/project');
});

// ── background: the fourth state ────────────────────────────────────────────

test('busy → background notifies NOTHING; background → free latches done once and still pings nothing', async () => {
    const h = harness({
        routes: snapshot_route([status({ session: 'alpha', state: 'busy' })]),
        flags: { debounce_polls: 1 },
    });
    await h.ingestor.tick(); // first tick: suppressed
    await h.ingestor.tick();
    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'background', background_jobs: 2 })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'background');
    assert.equal(h.last().sessions[0].background_jobs, 2);
    assert.deepEqual(h.notices.at(-1), { attention: [] }, 'the turn ended, the work did not');

    h.set_routes(snapshot_route([status({ session: 'alpha', state: 'free' })]));
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'done');
    assert.deepEqual(h.notices.at(-1), { attention: [] }, 'never ping on done (notify.py property 6)');
});

test('a state word outside the vocabulary is ingested as unknown — poll path', async () => {
    const h = harness({
        routes: { '/snapshot': { body: { sessions: [{ session: 'alpha', state: 'hibernating', age_seconds: 1 }] } } },
    });
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].state, 'unknown');
    assert.equal(h.last().sessions[0].display, 'unknown');
});

test('a state word outside the vocabulary is ingested as unknown — SSE path', () => {
    const handlers = new Map<string, (event: { data: string }) => void>();
    const h = harness({
        flags: { sse_enabled: true },
        deps: {
            event_source: () =>
                ({
                    close: () => {},
                    addEventListener: (name: string, handler: (event: { data: string }) => void) => {
                        handlers.set(name, handler);
                    },
                }) as unknown as EventSource,
        },
    });
    h.ingestor.start_stream();
    handlers.get('snapshot')?.({
        data: JSON.stringify({ sessions: [{ session: 'alpha', state: 'hibernating' }, { session: 'beta', state: 'background', background_jobs: 'many' }] }),
    });
    const rows = new Map(h.last().sessions.map((row) => [row.session, row]));
    assert.equal(rows.get('alpha')?.state, 'unknown');
    assert.equal(rows.get('beta')?.state, 'background');
    assert.equal(rows.get('beta')?.background_jobs, null, 'a malformed count is unknown, not trusted');
});

test('fake_background seeds the state the bridge refuses to be told', async () => {
    const h = harness({
        flags: { fake_background: { alpha: 3 } },
        routes: snapshot_route([status({ session: 'alpha', state: 'free' }), status({ session: 'beta', state: 'free' })]),
    });
    await h.ingestor.tick();
    const rows = new Map(h.last().sessions.map((row) => [row.session, row]));
    assert.equal(rows.get('alpha')?.display, 'background');
    assert.equal(rows.get('alpha')?.background_jobs, 3);
    assert.equal(rows.get('beta')?.display, 'free');
});

test('fake_background is own-key guarded against prototype names', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'constructor', state: 'free' })]) });
    await h.ingestor.tick();
    assert.equal(h.last().sessions[0].display, 'free');
});

test('/activity is refreshed only while SSE is down', async () => {
    const entries = [{ ts: 5, session: 'alpha', from: 'busy', to: 'free' }];
    const h = harness({
        routes: { ...snapshot_route([status({ session: 'alpha' })]), '/activity': { body: { entries } } },
        deps: { on_activity: () => {} },
    });
    await h.ingestor.tick();
    assert.ok(h.calls.some((c) => c.startsWith('/activity')), 'SSE never connected, so the poll carries it');
});

test('no on_activity subscriber means no /activity fetch at all', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'alpha' })]) });
    await h.ingestor.tick();
    assert.equal(h.calls.some((c) => c.startsWith('/activity')), false);
});

test('a malformed snapshot body degrades to an empty rack, not a throw', async () => {
    const h = harness({ routes: { '/snapshot': { body: { sessions: 'not-an-array' } } } });
    await h.ingestor.tick();
    assert.deepEqual(h.last().sessions, []);
    assert.equal(h.last().reachable, true, 'the bridge DID answer');
});

test('start_stream opens the SSE stream WITHOUT starting the poll', () => {
    // The whole reason these are two lifecycles. The stream is a command channel
    // that stays up for the life of the page; the poll belongs to a rack that is
    // on screen. Fused, one look at the rack leaves a 3s /snapshot poll running
    // forever.
    let opened = 0;
    const h = harness({
        flags: { sse_enabled: true },
        deps: {
            event_source: () => {
                opened += 1;
                return { close: () => {}, addEventListener: () => {} } as unknown as EventSource;
            },
            set_interval: (() => {
                throw new Error('start_stream must not arm a timer');
            }) as never,
        },
    });
    h.ingestor.start_stream();
    assert.equal(opened, 1);
    assert.equal(h.ingestor.is_polling(), false);
});

test('stop_polling keeps the stream alive', () => {
    let closed = 0;
    const h = harness({
        flags: { sse_enabled: true },
        deps: {
            event_source: () =>
                ({
                    close: () => {
                        closed += 1;
                    },
                    addEventListener: () => {},
                }) as unknown as EventSource,
            set_interval: (() => 1) as never,
        },
    });
    h.ingestor.start_polling();
    assert.equal(h.ingestor.is_polling(), true);
    h.ingestor.stop_polling();
    assert.equal(h.ingestor.is_polling(), false);
    assert.equal(closed, 0, 'the command channel survives');
    h.ingestor.stop();
    assert.equal(closed, 1, 'stop() tears down both');
});

test('a malformed bridge identity is treated as absent, never trusted', async () => {
    const h = harness({
        routes: { '/snapshot': { body: { sessions: [], bridge: { pid: 'not-a-number' } } } },
    });
    await h.ingestor.tick();
    assert.equal(h.last().bridge, null);
});

test('profiles ride the snapshot through, parsed', async () => {
    const h = harness({
        routes: {
            '/snapshot': {
                body: {
                    sessions: [status({ session: 'vc-1', profile: 'codex-a' } as Partial<SessionStatus> & { session: string })],
                    profiles: [
                        { id: 'codex-a', label: 'CODEX A', kind: 'codex', default: false, cli_found: true },
                        { id: 'BAD ID', label: 'x', kind: 'claude' },
                    ],
                },
            },
        },
    });
    await h.ingestor.tick();
    assert.deepEqual(h.last().profiles?.map((p) => p.id), ['codex-a']);
    assert.equal(h.last().sessions[0].profile, 'codex-a');
});

test('a bridge that sends no profiles leaves the key ABSENT — the /spawn capability signal', async () => {
    const h = harness({ routes: snapshot_route([status({ session: 'vc-1' })]) });
    await h.ingestor.tick();
    assert.equal('profiles' in h.last(), false);
});
