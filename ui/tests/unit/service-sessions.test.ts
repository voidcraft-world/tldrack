import test, { describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
    SERVICES_COHORT_LABEL,
    exited_readout,
    is_service_session,
    parse_dev_services,
    service_for_session,
    service_readout,
    shows_service_mark,
    type DevServiceRow,
} from '../../src/engine/terminal/service-sessions.ts';
import {
    build_rack_rows,
    cohort_label_for,
    group_into_cohorts,
    type RackSession,
} from '../../src/engine/terminal/multi-terminal-layout.ts';
import { build_rack_payload, to_rack_session } from '../../src/engine/terminal/rack-view.ts';
import { UNKNOWN_MODEL_LABEL, parse_session_row, type PortSnapshotRow } from '../../src/engine/terminal/terminal-status-logic.ts';
import { create_dev_services_poller } from '../../src/agent-rack/dev-services-client.ts';
import { SERVICE_SESSION_FLAGS } from '../../src/config/service-session-flags.ts';

function service(overrides: Partial<DevServiceRow> = {}): DevServiceRow {
    return {
        key: 'vite',
        label: 'VITE',
        session: 'svc-vite',
        status: 'running',
        port: 5173,
        pid: 4242,
        external_pid: null,
        exit_code: null,
        log_tail: [],
        since: null,
        ...overrides,
    };
}

function session(overrides: Partial<RackSession> & { session: string }): RackSession {
    return { state: 'free', live: true, repo: 'my-app', ...overrides };
}

function snapshot_row(overrides: Partial<PortSnapshotRow> & { session: string }): PortSnapshotRow {
    return { display: 'unknown', live: true, model: null, usage: null, ...overrides } as PortSnapshotRow;
}

describe('is_service_session', () => {
    test('the prefixes this repository names sessions with', () => {
        for (const name of ['svc-vite', 'svc-docs', 'laya-train', 'laya-golden']) {
            assert.equal(is_service_session(name), true, name);
        }
    });

    test('agents, shells and look-alikes are not services', () => {
        for (const name of ['vc-1', 'wt-fix-nav', 'main', 'svc', 'my-svc-vite', 'laya', 'layabout', 'Svc-vite', 'docs-sync', 'nightly-report']) {
            assert.equal(is_service_session(name), false, name);
        }
    });

    test('a prefix a host pushes onto the flags makes its sessions services, and only while it is there', () => {
        const prefixes = SERVICE_SESSION_FLAGS.job_prefixes;
        const was = [...prefixes];
        assert.equal(is_service_session('nightly-report'), false);
        prefixes.push('nightly-');
        try {
            assert.equal(is_service_session('nightly-report'), true);
            assert.equal(is_service_session('nightly'), false, 'the prefix carries its dash');
            assert.equal(shows_service_mark('nightly-report', undefined), true);
            const cohorts = group_into_cohorts([session({ session: 'vc-1' }), session({ session: 'nightly-report' })]);
            assert.deepEqual(cohorts.map((cohort) => cohort.label), ['my-app', SERVICES_COHORT_LABEL]);
        } finally {
            prefixes.splice(0, prefixes.length, ...was);
        }
        assert.deepEqual(SERVICE_SESSION_FLAGS.job_prefixes, was);
        assert.equal(is_service_session('nightly-report'), false);
    });

    // The plugin JOBS, each name READ out of the Python that spawns it, so a
    // renamed session fails here instead of landing among the agents as
    // `(no path) — 1 idle`.
    const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
    const JOB_OWNERS: { file: string; pattern: RegExp; sample: (value: string) => string }[] = [
        { file: 'plugins/laya/laya_bridge/lab_jobs.py', pattern: /^SESSION_PREFIX = "([^"]+)"/m, sample: (v) => `${v}train` },
    ];
    for (const owner of JOB_OWNERS) {
        test(`the job session named in ${owner.file} (${owner.pattern.source}) is machinery`, () => {
            const match = owner.pattern.exec(readFileSync(resolve(REPO, owner.file), 'utf8'));
            assert.ok(match, `the anchor was not found in ${owner.file} — re-point this test at the session name's new owner`);
            const name = owner.sample(match[1]);
            assert.equal(is_service_session(name), true, name);
        });
    }

    test('a finished job pane files under SERVICES, never `(no path)`', () => {
        const cohorts = group_into_cohorts([
            session({ session: 'vc-1' }),
            session({ session: 'laya-train', repo: null, cwd: null, state: 'unknown' }),
        ]);
        assert.deepEqual(cohorts.map((cohort) => cohort.label), ['my-app', SERVICES_COHORT_LABEL]);
    });
});

describe('exited_readout', () => {
    test('exit 0 reads DONE, untinted', () => {
        const readout = exited_readout('laya-golden', 0);
        assert.equal(readout.text, '✓ DONE');
        assert.equal(readout.tone, undefined);
        assert.match(readout.title, /Safe to close/);
    });

    test('a failure names its code, in red, and says it is safe to close', () => {
        const readout = exited_readout('laya-train', 1);
        assert.equal(readout.text, '✗ EXIT 1');
        assert.equal(readout.tone, 'critical');
        assert.match(readout.title, /failed \(exit 1\)/);
        assert.match(readout.title, /tmux attach -t laya-train/);
        assert.match(readout.title, /Safe to close/);
    });

    test('no code reads ENDED', () => {
        assert.equal(exited_readout('laya-train', null).text, '✗ ENDED');
    });
});

describe('an ended pane on the rack', () => {
    test('the snapshot row is validated at the boundary', () => {
        assert.deepEqual(
            pick(parse_session_row({ session: 'laya-train', state: 'unknown', exited: true, exit_code: 1 })),
            { exited: true, exit_code: 1 },
        );
        assert.deepEqual(pick(parse_session_row({ session: 'q', state: 'unknown', exited: true, exit_code: 'x' })), { exited: true, exit_code: null });
        assert.deepEqual(pick(parse_session_row({ session: 'q', state: 'unknown', exited: 'yes', exit_code: 1 })), {});
    });

    test('reads its exit in place of a model, and carries `exited` for the ✕', () => {
        const entry = to_rack_session(snapshot_row({ session: 'laya-train', exited: true, exit_code: 1 }));
        assert.equal(entry.service?.text, '✗ EXIT 1');
        assert.deepEqual(entry.exited, { code: 1 });
        const rows = build_rack_rows({ sessions: [entry, session({ session: 'vc-1' })], focused: null, max_sessions: 8 });
        const row = rows.find((candidate) => candidate.session === 'laya-train');
        assert.deepEqual(row?.exited, { code: 1 });
        assert.match(row?.text ?? '', /✗ EXIT 1/);
    });

    test("dev-services' own readout wins on a svc row", () => {
        const entry = to_rack_session(
            snapshot_row({ session: 'svc-vite', exited: true, exit_code: 1 }),
            undefined,
            [service({ status: 'failed', exit_code: 1 })],
        );
        assert.equal(entry.service?.text, '✗ VITE EXIT 1');
    });

    test('a running pane carries none of it', () => {
        const entry = to_rack_session(snapshot_row({ session: 'laya-train' }));
        assert.equal(entry.exited, undefined);
        assert.equal(entry.service, undefined);
    });
});

function pick(row: ReturnType<typeof parse_session_row>): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    if (row && 'exited' in row) out.exited = row.exited;
    if (row && 'exit_code' in row) out.exit_code = row.exit_code;
    return out;
}

describe('parse_dev_services', () => {
    test('a non-document is null — the plugin is not mounted, grow nothing', () => {
        assert.equal(parse_dev_services(null), null);
        assert.equal(parse_dev_services({ ok: false, error: 'unknown plugin: dev-services' }), null);
        assert.equal(parse_dev_services('nope'), null);
    });

    test('keeps well-formed rows and drops the rest', () => {
        const rows = parse_dev_services({
            ok: true,
            services: [
                { key: 'vite', label: 'VITE', session: 'svc-vite', status: 'running', port: 5173, pid: 1, log_tail: ['a'] },
                { label: 'NO KEY', session: 'svc-x' },
                'garbage',
            ],
        });
        assert.equal(rows?.length, 1);
        assert.equal(rows?.[0].port, 5173);
        assert.deepEqual(rows?.[0].log_tail, ['a']);
    });

    test('an unknown status reads as starting, never as stopped', () => {
        const rows = parse_dev_services({ services: [{ key: 'vite', session: 'svc-vite', status: 'warming' }] });
        assert.equal(rows?.[0].status, 'starting');
        assert.equal(rows?.[0].label, 'VITE');
    });

    test('a fractional or string port is not a port', () => {
        const rows = parse_dev_services({ services: [{ key: 'vite', session: 'svc-vite', status: 'running', port: '5173' }] });
        assert.equal(rows?.[0].port, null);
    });

    test('the log tail is capped and string-only', () => {
        const tail = [...Array.from({ length: 40 }, (_, i) => `line ${i}`), 7];
        const rows = parse_dev_services({ services: [{ key: 'vite', session: 'svc-vite', status: 'failed', log_tail: tail }] });
        assert.equal(rows?.[0].log_tail.length, 20);
        assert.equal(rows?.[0].log_tail.at(-1), 'line 39');
    });
});

describe('service_readout', () => {
    test('running reads glyph, label and port, untinted', () => {
        const readout = service_readout(service());
        assert.equal(readout.text, '● VITE :5173');
        assert.equal(readout.tone, undefined);
        assert.match(readout.title, /tmux attach -t svc-vite/);
    });

    test('failed is red, carries the exit status, and the log tail in its title', () => {
        const readout = service_readout(service({ status: 'failed', port: null, exit_code: 1, log_tail: ['x', 'Error: EADDRINUSE'] }));
        assert.equal(readout.text, '✗ VITE EXIT 1');
        assert.equal(readout.tone, 'critical');
        assert.match(readout.title, /EADDRINUSE/);
    });

    test('external names the port and the pid that holds it', () => {
        const readout = service_readout(service({ status: 'external', external_pid: 999 }));
        assert.equal(readout.text, '⚠ VITE EXTERNAL :5173');
        assert.equal(readout.tone, 'low');
        assert.match(readout.title, /pid 999/);
    });

    test('stopped and starting are amber', () => {
        assert.equal(service_readout(service({ status: 'stopped', port: null })).text, '○ VITE STOPPED');
        assert.equal(service_readout(service({ status: 'starting', port: null })).tone, 'low');
    });

    test('joined to a row by session name', () => {
        const rows = [service(), service({ key: 'docs', label: 'DOCS', session: 'svc-docs', port: null })];
        assert.equal(service_for_session('svc-docs', rows)?.key, 'docs');
        assert.equal(service_for_session('svc-nope', rows), undefined);
        assert.equal(service_for_session('svc-vite', undefined), undefined);
    });
});

describe('the SERVICES band', () => {
    test('service sessions group into ONE band whatever their repo', () => {
        const cohorts = group_into_cohorts([
            session({ session: 'vc-1', state: 'busy' }),
            session({ session: 'svc-vite', state: 'unknown' }),
            session({ session: 'laya-train', state: 'unknown', repo: null, cwd: '/tmp' }),
        ]);
        assert.deepEqual(cohorts.map((c) => c.label), ['my-app', SERVICES_COHORT_LABEL]);
        assert.deepEqual(cohorts[1].sessions.map((s) => s.session), ['svc-vite', 'laya-train']);
    });

    test('the band loses a rank tie to every project', () => {
        const cohorts = group_into_cohorts([
            session({ session: 'svc-vite', state: 'free' }),
            session({ session: 'vc-1', state: 'free', repo: 'zzz' }),
        ]);
        assert.deepEqual(cohorts.map((c) => c.label), ['zzz', SERVICES_COHORT_LABEL]);
    });

    test('a blocked agent in a job pane lifts the band like any cohort', () => {
        const cohorts = group_into_cohorts([
            session({ session: 'vc-1', state: 'busy' }),
            session({ session: 'laya-label', state: 'attention' }),
        ]);
        assert.equal(cohorts[0].label, SERVICES_COHORT_LABEL);
    });

    test('its header is a count, never the agent rollup (a serving Vite is not idle)', () => {
        const rows = build_rack_rows({
            sessions: [
                session({ session: 'vc-1', state: 'busy' }),
                session({ session: 'svc-vite', state: 'unknown' }),
                session({ session: 'laya-golden', state: 'unknown' }),
            ],
            focused: null,
            max_sessions: 8,
        });
        const heads = rows.filter((row) => row.kind === 'cohort').map((row) => row.text);
        assert.deepEqual(heads, ['my-app — 1 working', 'SERVICES · 2']);
    });

    test('a services-only rack gets no header, like any single cohort', () => {
        const rows = build_rack_rows({ sessions: [session({ session: 'svc-vite' })], focused: null, max_sessions: 8 });
        assert.equal(rows.some((row) => row.kind === 'cohort'), false);
    });

    test('a service row offers no rename — its plugin finds it by name — and kill stays last', () => {
        const rows = build_rack_rows({ sessions: [session({ session: 'svc-vite' })], focused: null, max_sessions: 8 });
        assert.deepEqual(rows[0].actions, ['focus', 'kill']);
        const agent = build_rack_rows({ sessions: [session({ session: 'vc-1' })], focused: null, max_sessions: 8 });
        assert.deepEqual(agent[0].actions, ['focus', 'rename', 'kill']);
    });

    test('the service readout replaces the model segment, tone and title intact', () => {
        const rows = build_rack_rows({
            sessions: [session({ session: 'svc-vite', model_label: 'should not show', service: service_readout(service({ status: 'failed', exit_code: 2 })) })],
            focused: null,
            max_sessions: 8,
        });
        const detail = rows[0].segments?.slice(1) ?? [];
        assert.equal(detail.length, 1);
        assert.equal(detail[0].text, '✗ VITE EXIT 2');
        assert.equal(detail[0].tone, 'critical');
        assert.match(detail[0].title ?? '', /svc-vite/);
    });

    test('the rack map still places a service at its checkout', () => {
        assert.equal(cohort_label_for(session({ session: 'svc-vite' })), 'my-app');
    });
});

describe('rack-view join', () => {
    test('a svc row gets its readout; "model unknown" is dropped from every service row', () => {
        const services = [service()];
        const vite = to_rack_session(snapshot_row({ session: 'svc-vite' }), undefined, services);
        assert.equal(vite.service?.text, '● VITE :5173');
        assert.equal(vite.model_label, null);
        const job = to_rack_session(snapshot_row({ session: 'laya-train' }));
        assert.equal(job.model_label, null);
        assert.equal(job.service, undefined);
    });

    test('an agent keeps "model unknown" — there it is a real fact', () => {
        const agent = to_rack_session(snapshot_row({ session: 'vc-1' }), undefined, [service()]);
        assert.equal(agent.model_label, UNKNOWN_MODEL_LABEL);
        assert.equal(agent.service, undefined);
    });

    test('build_rack_payload threads the rows through and adds no payload key', () => {
        const payload = build_rack_payload({
            rows: [snapshot_row({ session: 'svc-vite' })],
            focused: null,
            reachable: true,
            max_sessions: 8,
            services: [service({ status: 'stopped', port: null })],
        });
        assert.equal(payload.sessions[0].service?.text, '○ VITE STOPPED');
        assert.equal('services' in payload, false);
    });
});

describe('create_dev_services_poller', () => {
    function harness(bodies: unknown[], wanted = () => true) {
        const calls: string[] = [];
        let changes = 0;
        const poller = create_dev_services_poller({
            base_url: (path) => `http://bridge${path}`,
            on_change: () => {
                changes += 1;
            },
            wanted,
            fetch_impl: async (url) => {
                calls.push(String(url));
                const body = bodies.shift();
                if (body === 404) return new Response('{}', { status: 404 });
                return new Response(JSON.stringify(body), { status: 200 });
            },
            set_interval: () => 0 as unknown as ReturnType<typeof setInterval>,
            clear_interval: () => {},
        });
        return { poller, calls, changes: () => changes };
    }

    test('asks nothing while the rack has no service row', async () => {
        const h = harness([{ services: [service()] }], () => false);
        await h.poller.tick();
        assert.equal(h.calls.length, 0);
        assert.equal(h.poller.state(), null);
    });

    test('a 404 is "no plugin": null state, no change', async () => {
        const h = harness([404]);
        await h.poller.tick();
        assert.equal(h.calls[0], 'http://bridge/plugin/dev-services/state');
        assert.equal(h.poller.state(), null);
        assert.equal(h.changes(), 0);
    });

    test('notifies on a visible change only — a moving log tail is not one', async () => {
        const h = harness([
            { services: [service({ log_tail: ['a'] })] },
            { services: [service({ log_tail: ['a', 'b'] })] },
            { services: [service({ status: 'failed', exit_code: 1 })] },
        ]);
        await h.poller.tick();
        await h.poller.tick();
        assert.equal(h.changes(), 1);
        await h.poller.tick();
        assert.equal(h.changes(), 2);
        assert.equal(h.poller.state()?.[0].status, 'failed');
    });
});

describe('the service mark', () => {
    test('a hookless service shell draws the server mark', () => {
        assert.equal(shows_service_mark('svc-vite', 'unknown'), true);
        assert.equal(shows_service_mark('laya-train', undefined), true);
    });

    test('a hooked agent in a job pane keeps its state mark', () => {
        assert.equal(shows_service_mark('laya-label', 'attention'), false);
    });

    test('an agent row never draws it', () => {
        assert.equal(shows_service_mark('wt-fix-nav', 'unknown'), false);
        assert.equal(shows_service_mark(undefined, 'unknown'), false);
    });
});
