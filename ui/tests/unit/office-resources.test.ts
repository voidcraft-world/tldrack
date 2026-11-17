import { test } from 'node:test';
import assert from 'node:assert/strict';
import { office_resources, parse_office_resources, resource_readout } from '../../src/engine/terminal/office-resources.ts';
import { parse_devices } from '../../src/engine/terminal/device-readout.ts';
import { office_fleet, parse_office_message } from '../../src/engine/terminal/agent-office-link.ts';

test('floor totals add session trees once and distinguish unknown from idle', () => {
    const rows = [{ session: 'a', rss: 1024 ** 3, cpu_pct: 120 }, { session: 'svc-b', rss: 512 * 1024 ** 2, cpu_pct: 0 }];
    assert.equal(resource_readout(rows, 2), 'CPU 120% · RAM 1.5 GiB');
    assert.equal(resource_readout([], 1), 'CPU — · RAM — · 0/1 sampled');
    assert.equal(resource_readout([{ ...rows[0], cpu_pct: null }], 2, true), 'CPU — · RAM 1.0 GiB · 1/2 sampled · stale');
});
test('the existing device sample supplies resources, closes as stale, and rejects invalid numbers', () => {
    const state = parse_devices({ sample_s: 2, devices: [{ name: 'local', local: true, state: 'up', age_s: 1,
        session_memory: { age_s: 4, sessions: [{ session: 'a', rss: 100, cpu_pct: 12.5 }] } }] })!;
    assert.equal(office_resources(state, true)?.sessions[0].cpu_pct, 12.5);
    assert.equal(office_resources(state, true)?.stale, false);
    assert.equal(office_resources(state, false)?.stale, true);
    state.devices[0].session_memory!.age_s = 40;
    assert.equal(office_resources(state, true)?.stale, true);
    assert.equal(parse_office_resources({ sessions: [{ session: 'a', rss: 1, cpu_pct: Infinity }] })?.sessions[0].cpu_pct, null);
    assert.equal(parse_office_resources({ sessions: [{ session: 'a', rss: -1 }] })?.sessions.length, 0);
});
test('resources round trip through the standalone office channel without extra polling', () => {
    const resources = { stale: false, sessions: [{ session: 'a', rss: 123, cpu_pct: 4.5 }] };
    const fleet = office_fleet({ sessions: [], focused: null, reachable: true, office_resources: resources });
    const parsed = parse_office_message(JSON.parse(JSON.stringify({ type: 'office:fleet', fleet })));
    assert.equal(parsed?.type, 'office:fleet');
    if (parsed?.type === 'office:fleet') assert.deepEqual(parsed.fleet.resources, resources);
});
