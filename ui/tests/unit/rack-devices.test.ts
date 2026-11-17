import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import {
    DEVICE_WARN_PCT,
    device_rows,
    devices_signature,
    parse_devices,
    span_text,
    type DeviceSample,
    type DevicesState,
    type HostJob,
} from '../../src/engine/terminal/device-readout.ts';
import { build_rack_payload } from '../../src/engine/terminal/rack-view.ts';
import { create_devices_poller, read_devices_open, write_devices_open } from '../../src/agent-rack/devices-client.ts';
import { host_label, LOCAL_HOST } from '../../src/engine/terminal/local-host.ts';

const GIB = 1024 ** 3;

function device(overrides: Partial<DeviceSample> = {}): DeviceSample {
    return {
        name: 'local',
        local: true,
        state: 'up',
        os: 'Darwin arm64',
        cores: 10,
        cpu_busy: 2.2,
        mem_used: 21.4 * GIB,
        mem_total: 32 * GIB,
        age_s: 0.8,
        error: null,
        session_memory: null,
        jobs: null,
        lease: null,
        ...overrides,
    };
}

function state(...devices: DeviceSample[]): DevicesState {
    return { sample_s: 2, devices };
}

describe('parse_devices', () => {
    test('anything that is not a devices document is "no plugin"', () => {
        assert.equal(parse_devices(null), null);
        assert.equal(parse_devices({ ok: false, error: 'unknown plugin: devices' }), null);
        assert.equal(parse_devices('nope'), null);
    });

    test('keeps good rows, drops nameless ones, and reads junk numbers as unknown', () => {
        const parsed = parse_devices({
            version: 1,
            sample_s: 2,
            devices: [
                { name: 'local', local: true, state: 'up', cores: 10, cpu_busy: 2.2, mem_used: 5, mem_total: 10, age_s: 1 },
                { name: '', state: 'up' },
                { name: 'box-2', state: 'weird', cores: 'eight', cpu_busy: -1, mem_total: Number.NaN },
            ],
        });
        assert.ok(parsed);
        assert.equal(parsed.devices.length, 2);
        const [local, remote] = parsed.devices;
        assert.equal(local.local, true);
        assert.equal(local.cpu_busy, 2.2);
        assert.equal(remote.state, 'down');
        assert.equal(remote.cores, null);
        assert.equal(remote.cpu_busy, null);
        assert.equal(remote.mem_total, null);
    });
});

describe('device_rows', () => {
    test('raw numbers and their share', () => {
        const [row] = device_rows(state(device()));
        assert.equal(row.label, 'LOCAL');
        assert.equal(row.tone, 'ok');
        assert.equal(row.status, null);
        assert.deepEqual(
            row.meters.map((m) => [m.label, m.text, m.pct]),
            [
                ['CPU', '2.2/10', 22],
                ['RAM', '21.4/32 GB', 67],
            ],
        );
        assert.match(row.title, /LOCAL · Darwin arm64 · 2\.2\/10 cores busy \(22%\) · 21\.4\/32 GB memory used \(67%\)/);
    });

    test('a total keeps its decimal only when it has one', () => {
        const [row] = device_rows(state(device({ mem_used: 2.6 * GIB, mem_total: 15.5 * GIB })));
        assert.equal(row.meters[1].text, '2.6/15.5 GB');
    });

    test('a host label is the shared rule', () => {
        const [, remote] = device_rows(state(device(), device({ name: 'box-2', local: false })));
        assert.equal(remote.label, host_label('box-2'));
        assert.equal(host_label(LOCAL_HOST), 'LOCAL');
    });

    test(`a meter warns at ${DEVICE_WARN_PCT}%`, () => {
        const [row] = device_rows(state(device({ cpu_busy: 8.5, mem_used: 20 * GIB })));
        assert.equal(row.meters[0].pct, 85);
        assert.equal(row.meters[0].warn, true);
        assert.equal(row.meters[1].warn, false);
    });

    test('the first sample has no CPU yet: a dash, never a 0', () => {
        const [row] = device_rows(state(device({ cpu_busy: null })));
        assert.equal(row.meters[0].text, '—/10');
        assert.equal(row.meters[0].pct, null);
    });

    test('a host that is down says OFFLINE and shows no numbers', () => {
        const [, remote] = device_rows(
            state(device(), device({ name: 'box-2', state: 'down', error: 'ssh: connect timed out' })),
        );
        assert.equal(remote.tone, 'bad');
        assert.equal(remote.status, 'OFFLINE');
        assert.deepEqual(remote.meters, []);
        assert.match(remote.title, /offline · ssh: connect timed out/);
    });

    test('connecting with nothing yet reads CONNECTING; with old numbers it dims them', () => {
        const fresh = device_rows(state(device({ state: 'connecting', cores: null, cpu_busy: null, mem_total: null, mem_used: null })));
        assert.equal(fresh[0].status, 'CONNECTING');
        assert.equal(fresh[0].tone, 'wait');
        const reconnecting = device_rows(state(device({ state: 'connecting' })));
        assert.equal(reconnecting[0].status, null);
        assert.equal(reconnecting[0].tone, 'wait');
        assert.equal(reconnecting[0].meters.length, 2);
    });

    test('a sample three intervals old is STALE', () => {
        assert.equal(device_rows(state(device({ age_s: 6 })))[0].status, null);
        assert.equal(device_rows(state(device({ age_s: 6.1 })))[0].status, 'STALE');
    });
});

describe('host jobs', () => {
    const train: HostJob = {
        key: 'train', label: 'TRAIN', state: 'running', procs: 7, cores: 6.04, rss: 2.1 * GIB, age_s: 2700,
        groups: [
            { name: 'train-worker', count: 6, state: 'running', cores: 6, rss: 2 * GIB },
            { name: 'train-lead', count: 1, state: 'idle', cores: 0.04, rss: 139 * 1024 ** 2 },
        ],
        groups_more: 0,
    };
    const bench: HostJob = {
        key: 'bench', label: 'BENCH', state: 'held', procs: 4, cores: 0, rss: 1.4 * GIB, age_s: 260_000,
        groups: [{ name: 'bench-lane', count: 3, state: 'held', cores: 0, rss: 1.2 * GIB }], groups_more: 2,
    };
    const sync: HostJob = { ...train, key: 'sync', label: 'DOCS SYNC', state: 'idle', cores: 0, groups: [], groups_more: 0 };
    const other: HostJob = { ...sync, key: 'other', label: 'OTHER', cores: 0.1, age_s: null };
    const host = (overrides: Partial<DeviceSample> = {}) =>
        device({ name: 'box-2', local: false, jobs: { window_s: 10, rows: [train, bench, sync, other] }, ...overrides });

    test('parsed from the plugin, junk rows and junk numbers dropped', () => {
        const parsed = parse_devices({ sample_s: 2, devices: [{ name: 'h', state: 'up', jobs: { window_s: 10, rows: [
            { key: 'train', label: 'TRAIN', state: 'running', cores: 6, rss: 5, procs: 7, age_s: 9,
              groups: [{ name: 'train-worker', count: 6, state: 'bogus', cores: 'x' }, { nope: 1 }], groups_more: 0 },
            { label: 'no key' },
        ] } }, { name: 'local', state: 'up' }] });
        const [h, local] = parsed?.devices ?? [];
        assert.equal(h.jobs?.rows.length, 1);
        assert.deepEqual(h.jobs?.rows[0].groups, [{ name: 'train-worker', count: 6, state: 'idle', cores: 0, rss: 0 }]);
        assert.equal(local.jobs, null);
    });

    test('the closed line names the busy jobs; rows spell out activity and CPU units', () => {
        const jobs = device_rows(state(host()))[0].jobs;
        assert.ok(jobs);
        assert.equal(jobs.summary, 'RUNNING TRAIN · PAUSED BENCH');
        assert.deepEqual(jobs.rows.map((row) => [row.key, row.tone, row.text]), [
            ['box-2/train', 'run', '6.0 cores · 2.1 GB'],
            ['box-2/bench', 'held', '0.0 cores · 1.4 GB'],
            ['box-2/sync', 'idle', '0.0 cores · 2.1 GB'],
            ['box-2/other', 'other', '0.1 cores · 2.1 GB'],
        ]);
        assert.deepEqual(jobs.rows.map((row) => row.status), ['RUNNING', 'PAUSED', 'IDLE', '']);
        assert.match(jobs.rows[1].title, /^BENCH: paused: its worker processes are suspended .* 4 processes, up 3d$/);
        assert.deepEqual(jobs.rows[1].groups.map((g) => [g.name, g.held]), [['3× bench-lane', true], ['+2 more', false]]);
        assert.equal(jobs.rows[0].groups[0].name, '6× train-worker');
    });

    test('idle processes and an empty sample are distinct; a down host and a device without jobs grow none', () => {
        assert.equal(device_rows(state(host({ jobs: { window_s: 10, rows: [sync, other] } })))[0].jobs?.summary, 'no active tasks');
        assert.equal(device_rows(state(host({ jobs: { window_s: 10, rows: [] } })))[0].jobs?.summary, 'no processes reported');
        assert.equal(device_rows(state(host({ state: 'down' })))[0].jobs, undefined);
        assert.equal(device_rows(state(device()))[0].jobs, undefined);
    });

    test('a failed read says so until a sample replaces it; a sample beside a stale error wins', () => {
        const error = 'no answer from `ssh box2` in 30 s: the connection stalled';
        const parsed = parse_devices({ sample_s: 2, devices: [{ name: 'h', state: 'up', jobs: { window_s: null, rows: [], error, age_s: null } }] });
        assert.equal(parsed?.devices[0].jobs?.error, error);
        const jobs = device_rows(state(host({ jobs: { window_s: null, rows: [], error } })))[0].jobs;
        assert.deepEqual(jobs && [jobs.summary, jobs.failed, jobs.rows.length], ['unreadable', true, 0]);
        assert.match(jobs?.title ?? '', /^Could not read what this host runs: no answer .* stalled\. Retrying\.$/);
        const fresh = device_rows(state(host({ jobs: { window_s: 10, rows: [sync, other], error } })))[0].jobs;
        assert.deepEqual(fresh && [fresh.summary, fresh.failed], ['no active tasks', undefined]);
    });

    test('a job ageing by a sample is not a visible change', () => {
        const later = host({ jobs: { window_s: 10, rows: [{ ...train, age_s: 2710 }, bench, sync, other] } });
        assert.equal(devices_signature(state(host())), devices_signature(state(later)));
    });

    test('span_text cuts where the tray does', () => {
        assert.deepEqual([45, 89, 2700, 5340, 7200, 126_000, 260_000].map(span_text), ['45s', '89s', '45m', '1h', '2h', '35h', '3d']);
    });
});

describe("a machine's resource lease", () => {
    const status = (slots: object[] = [], extra: object = {}) => ({
        v: 1, window: { start: '03:00', end: '07:00', open: false }, live: [], unreadable: 0, ...extra,
        slots: [
            { slot: 'gpu', unit: 'gb', capacity: 22, used: 0, waiting: 0, holders: [] },
            { slot: 'subscription_claude', unit: 'holder', capacity: 1, used: 0, waiting: 0, holders: [] },
            { slot: 'cpu', unit: 'core', capacity: 6, used: 0, waiting: 0, holders: [] },
        ].map((slot) => ({ ...slot, ...slots.find((s) => (s as { slot: string }).slot === slot.slot) })),
    });
    const holder = (name: string, units: number, extra: object = {}) => ({
        holder: name, kind: 'mps', units, models: [], scheduled: true, holds: true, legacy: false, age_s: 120, ...extra,
    });
    const lease_of = (raw: unknown) => device_rows(parse_devices({ sample_s: 2, devices: [{ name: 'local', local: true, state: 'up', cores: 10, lease: raw }] })!)[0].lease;

    test('names each held slot with its holders, and says free when nothing holds one', () => {
        assert.equal(lease_of(status())?.summary, 'free');
        const busy = lease_of(status([
            { slot: 'gpu', used: 17.2, holders: [holder('laya-lab golden', 3.2), holder('nightly-report', 14, { kind: 'ollama' })] },
            { slot: 'subscription_claude', used: 1, holders: [holder('laya-lab label-open-work', 1, { kind: 'claude' })] },
        ]));
        assert.equal(busy?.summary, 'GPU 17.2/22 · laya-lab golden 3.2 · nightly-report 14 — CLAUDE · laya-lab label-open-work');
        assert.equal(busy?.warn, false);
        assert.match(busy?.title ?? '', /GPU 17\.2 of 22 GB held by laya-lab golden \(3\.2 GB, mps, scheduled, 2m\)/);
        assert.match(busy?.title ?? '', /CPU 0 of 6 cores free · night window 03:00–07:00 closed$/);
    });

    test('warns when a slot is full or has work waiting, and names an older checkout\'s lock', () => {
        const full = lease_of(status([{ slot: 'gpu', used: 22, waiting: 1, holders: [
            holder('bench-series', 22, { kind: null, scheduled: null, legacy: true }), holder('laya-lab golden', 1.6, { holds: false }),
        ] }]));
        assert.equal(full?.summary, 'GPU 22/22 · bench-series 22 · 1 waiting');
        assert.equal(full?.warn, true);
        assert.match(full?.title ?? '', /an older checkout's Ollama lock/);
    });

    test('says live work only while it is fresh, and a shape it does not know draws nothing', () => {
        const live = lease_of(status([], { live: [{ name: 'laya-decide', age_s: 3 }, { name: 'next-step', age_s: 400 }] }));
        assert.match(live?.title ?? '', /laya-decide live now/);
        assert.doesNotMatch(live?.title ?? '', /next-step/);
        assert.equal(lease_of({ v: 2 }), undefined);
        assert.equal(lease_of(null), undefined);
        assert.equal(device_rows(state(device()))[0].lease, undefined, 'a device that does not report it grows no line');
    });
});

describe('the payload', () => {
    test('absent devices grows no section', () => {
        const payload = build_rack_payload({ rows: [], focused: null, reachable: true, max_sessions: 8 });
        assert.equal('devices' in payload, false);
        assert.equal('devices_open' in payload, false);
    });

    test('present devices ride as rows, open unless the owner says closed', () => {
        const base = { rows: [], focused: null, reachable: true, max_sessions: 8, devices: state(device()) };
        const open = build_rack_payload(base);
        assert.equal(open.devices?.[0].label, 'LOCAL');
        assert.equal(open.devices_open, true);
        assert.equal(build_rack_payload({ ...base, devices_open: false }).devices_open, false);
    });

    test('office resources keep every sampled session beyond the five-row RAM summary', () => {
        const sessions = Array.from({ length: 8 }, (_, i) => ({ session: `agent-${i}`, rss: (8 - i) * GIB, procs: 1, cpu_pct: i }));
        const devices = state(device({ session_memory: { sessions, age_s: 1, more: 0, orphans: { rss: 0, procs: 0 } } }));
        const input = { rows: [], focused: null, reachable: true, max_sessions: 8, devices };
        assert.equal(build_rack_payload(input).office_resources?.sessions.length, 8);
        assert.equal(build_rack_payload({ ...input, devices_open: false }).office_resources?.stale, true);
        const before = devices_signature(devices);
        sessions[7].cpu_pct = 100;
        assert.notEqual(devices_signature(devices), before);
    });
});

describe('create_devices_poller', () => {
    function harness(bodies: unknown[], wanted = () => true) {
        const calls: string[] = [];
        let changes = 0;
        const poller = create_devices_poller({
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

    test('a closed or hidden section asks nothing', async () => {
        const h = harness([state(device())], () => false);
        await h.poller.tick();
        assert.equal(h.calls.length, 0);
    });

    test('a 404 is "no plugin": null state, no change', async () => {
        const h = harness([404]);
        await h.poller.tick();
        assert.equal(h.calls[0], 'http://bridge/plugin/devices/state');
        assert.equal(h.poller.state(), null);
        assert.equal(h.changes(), 0);
    });

    test('notifies on a visible change only: a sample that rounds the same is not one', async () => {
        const h = harness([
            state(device({ cpu_busy: 2.16, age_s: 0.5 })),
            state(device({ cpu_busy: 2.19, age_s: 1.4 })),
            state(device({ cpu_busy: 3.0 })),
        ]);
        await h.poller.tick();
        await h.poller.tick();
        assert.equal(h.changes(), 1);
        await h.poller.tick();
        assert.equal(h.changes(), 2);
        assert.equal(devices_signature(h.poller.state()), devices_signature(state(device({ cpu_busy: 3.0 }))));
    });
});

describe('open / closed', () => {
    test('open by default, remembered when closed, and a throwing storage never breaks it', () => {
        const store = new Map<string, string>();
        const storage = {
            getItem: (key: string) => store.get(key) ?? null,
            setItem: (key: string, value: string) => void store.set(key, value),
        };
        assert.equal(read_devices_open(storage), true);
        write_devices_open(false, storage);
        assert.equal(read_devices_open(storage), false);
        const broken = {
            getItem: () => {
                throw new Error('blocked');
            },
            setItem: () => {
                throw new Error('blocked');
            },
        };
        assert.equal(read_devices_open(broken), true);
        assert.doesNotThrow(() => write_devices_open(true, broken));
        assert.equal(read_devices_open(null), true);
    });
});
