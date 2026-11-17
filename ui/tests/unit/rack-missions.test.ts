import test, { describe } from 'node:test';
import assert from 'node:assert/strict';

import {
    create_missions_poller,
    fetch_missions,
    mission_for_session,
    mission_row_title,
    missions_signature,
    parse_mission_request,
    parse_missions,
    post_mission,
    type Mission,
} from '../../src/agent-rack/missions-client.ts';
import { device_rows, MEMORY_ROWS, parse_devices } from '../../src/engine/terminal/device-readout.ts';
import { build_rack_payload } from '../../src/engine/terminal/rack-view.ts';

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

function mission(overrides: Partial<Mission> = {}): Mission {
    return {
        id: 'm-1',
        title: 'Rack upgrade',
        brief: '',
        created_by: 'operator',
        created_at: 1,
        closed_at: null,
        members: [{ session: 'a', role: '', touching: [], hears: true }],
        notes: [],
        note_count: 0,
        ...overrides,
    };
}

function response(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status });
}

const url = (path: string) => `http://127.0.0.1:7682${path}`;

describe('parse_missions', () => {
    test('anything that is not a missions document is "no section"', () => {
        assert.equal(parse_missions(null), null);
        assert.equal(parse_missions({}), null);
        assert.equal(parse_missions({ missions: 'no' }), null);
    });

    test('a bad row is dropped alone', () => {
        assert.deepEqual(parse_missions({ missions: [mission(), { id: 'broken' }] }), [mission()]);
    });
});

describe('an older bridge', () => {
    test('a row without the newer keys still parses', () => {
        const { created_by: _created_by, ...row } = mission({ members: [] });
        const member = { session: 'a', role: '', touching: [] };
        const parsed = parse_missions({ missions: [{ ...row, members: [member] }] })!;
        assert.equal(parsed[0].created_by, '');
        assert.equal(parsed[0].members[0].hears, null);
    });
});

describe('fetch_missions', () => {
    test('a bridge that predates the route grows nothing', async () => {
        assert.equal(await fetch_missions(url, async () => response(404, {})), null);
    });

    test('an empty list is a section with nothing in it, not an absent one', async () => {
        assert.deepEqual(await fetch_missions(url, async () => response(200, { missions: [] })), []);
    });
});

describe('mission_for_session', () => {
    test('finds the mission a session is on, and nothing otherwise', () => {
        assert.equal(mission_for_session([mission()], 'a')?.id, 'm-1');
        assert.equal(mission_for_session([mission()], 'b'), undefined);
        assert.equal(mission_for_session(undefined, 'a'), undefined);
    });
});

describe('mission_row_title', () => {
    test('names the mission, and says so when the agent cannot hear it', () => {
        assert.equal(mission_row_title([mission()], 'a'), 'On the mission: Rack upgrade');
        const deaf = mission({ members: [{ session: 'a', role: '', touching: [], hears: false }] });
        assert.match(mission_row_title([deaf], 'a')!, /Cannot hear this mission/);
    });

    test('a session on none, and a hook not yet seen, say nothing extra', () => {
        assert.equal(mission_row_title([mission()], 'z'), null);
        assert.equal(mission_row_title([mission()], undefined), null);
        const unseen = mission({ members: [{ session: 'a', role: '', touching: [], hears: null }] });
        assert.equal(mission_row_title([unseen], 'a'), 'On the mission: Rack upgrade');
    });
});

describe('parse_mission_request', () => {
    test('takes the five ops and nothing else', () => {
        assert.deepEqual(parse_mission_request('{"op":"close","mission":"m"}'), { op: 'close', mission: 'm' });
        assert.equal(parse_mission_request('{"op":"drop"}'), null);
        assert.equal(parse_mission_request('not json'), null);
        assert.equal(parse_mission_request(undefined), null);
    });
});

describe('post_mission', () => {
    test('says what the bridge refused', async () => {
        const result = await post_mission(url, { op: 'close', mission: 'm' },
            async () => response(410, { error: "no mission 'm'" }));
        assert.deepEqual(result, { ok: false, error: "no mission 'm'" });
    });

    test('a 404 means the bridge needs a restart', async () => {
        const result = await post_mission(url, { op: 'close', mission: 'm' }, async () => response(404, {}));
        assert.match(result.error ?? '', /Restart the bridge/);
    });

    test('an unreachable bridge is a failure with a sentence, never a throw', async () => {
        const result = await post_mission(url, { op: 'close', mission: 'm' }, async () => {
            throw new Error('refused');
        });
        assert.deepEqual(result, { ok: false, error: 'Bridge not reachable' });
    });
});

describe('the poller', () => {
    test('notifies on a visible change only, and asks nothing while unwanted', async () => {
        let body: unknown = { missions: [mission()] };
        let calls = 0;
        let changes = 0;
        let wanted = true;
        const poller = create_missions_poller({
            base_url: url,
            fetch_impl: async () => {
                calls += 1;
                return response(200, body);
            },
            on_change: () => {
                changes += 1;
            },
            wanted: () => wanted,
        });
        await poller.tick();
        await poller.tick();
        assert.equal(changes, 1);
        body = { missions: [mission({ note_count: 1 })] };
        await poller.tick();
        assert.equal(changes, 2);
        assert.equal(missions_signature(poller.state()), JSON.stringify([mission({ note_count: 1 })]));
        wanted = false;
        const before = calls;
        await poller.tick();
        assert.equal(calls, before);
    });
});

describe('the payload', () => {
    const base = { rows: [], focused: null, reachable: true, max_sessions: 8 };

    test('absent missions stay absent, so an older bridge grows no section', () => {
        assert.equal('missions' in build_rack_payload(base), false);
        assert.deepEqual(build_rack_payload({ ...base, missions: [] }).missions, []);
    });
});

describe('per-session memory', () => {
    const sample = {
        name: 'local', local: true, state: 'up', os: 'Darwin arm64', cores: 10, cpu_busy: 1, mem_used: GIB, mem_total: 32 * GIB,
        age_s: 1, error: null,
    };

    function memory(sessions: { session: string; rss: number; procs: number }[], more = 0, orphans = { rss: 0, procs: 0 }) {
        return parse_devices({ sample_s: 2, devices: [{ ...sample, session_memory: { sessions, more, orphans } }] })!;
    }

    test('a device that does not report it grows no block', () => {
        assert.equal(device_rows(parse_devices({ sample_s: 2, devices: [sample] })!)[0].memory, undefined);
    });

    test('rows read in MB under a gigabyte, GB from there, under the rack names', () => {
        const rows = device_rows(memory([{ session: 'vc-1', rss: 1.25 * GIB, procs: 3 }, { session: 'b', rss: 640 * MIB, procs: 1 }]),
            new Map([['vc-1', 'fix nav']]))[0].memory!;
        assert.deepEqual(rows.rows.map((row) => [row.label, row.text]), [['fix nav', '1.3 GB'], ['b', '640 MB']]);
        assert.match(rows.rows[0].title, /3 processes/);
    });

    test('the cut is counted, including what the plugin already cut', () => {
        const sessions = Array.from({ length: MEMORY_ROWS + 2 }, (_, i) => ({ session: `s${i}`, rss: GIB, procs: 1 }));
        const rows = device_rows(memory(sessions, 4))[0].memory!;
        assert.equal(rows.rows.length, MEMORY_ROWS);
        assert.equal(rows.more, 6);
    });

    test('orphans appear only while there are some', () => {
        assert.equal(device_rows(memory([]))[0].memory!.orphans, null);
        const orphans = device_rows(memory([], 0, { rss: 420 * MIB, procs: 3 }))[0].memory!.orphans!;
        assert.equal(orphans.text, '420 MB');
        assert.match(orphans.title, /3 agent or MCP processes/);
    });

    test('a device that went down drops its stale ranking', () => {
        const state = memory([{ session: 'a', rss: GIB, procs: 1 }]);
        state.devices[0].state = 'down';
        assert.equal(device_rows(state)[0].memory, undefined);
    });
});
