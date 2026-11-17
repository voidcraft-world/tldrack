import test from 'node:test';
import assert from 'node:assert/strict';
import {
    ACTIVITY_ZONE_KEYS,
    session_hue,
    slot_offset,
    target_zone,
    zone_scatter_offset,
    type AgentZoneKey,
} from '../../src/engine/terminal/agent-zone-logic.ts';
import type { SessionActivity } from '../../src/engine/terminal/terminal-status-logic.ts';

const activity = (zone: string): SessionActivity => ({ zone, tool: 'Bash', detail: null });

test('busy with a classified zone routes there', () => {
    for (const zone of ACTIVITY_ZONE_KEYS) {
        assert.equal(target_zone('busy', activity(zone)), zone);
    }
});

test('busy with no activity routes to the thinking pedestal', () => {
    assert.equal(target_zone('busy', null), 'thinking');
    assert.equal(target_zone('busy', undefined), 'thinking');
});

test('busy with an unrecognized zone (newer bridge) degrades to computer', () => {
    assert.equal(target_zone('busy', activity('quantum')), 'computer');
});

test('non-busy states are always at the desk, regardless of stale activity', () => {
    const stale = activity('aws');
    for (const display of ['attention', 'free', 'done', 'unknown'] as const) {
        assert.equal(target_zone(display, stale), 'desk');
    }
});

test('slot offsets are centered around the anchor', () => {
    assert.equal(slot_offset(0, 1), 0);
    assert.deepEqual([slot_offset(0, 2), slot_offset(1, 2)], [-0.5, 0.5]);
    assert.deepEqual([slot_offset(0, 3), slot_offset(1, 3), slot_offset(2, 3)], [-1, 0, 1]);
});

test('session hue is stable, bounded, and spreads sessions apart', () => {
    assert.equal(session_hue('voidcraft'), session_hue('voidcraft'));
    for (const name of ['voidcraft', 'claude', 'research', 'a']) {
        const hue = session_hue(name);
        assert.ok(hue >= 0 && hue < 360, `${name} → ${hue}`);
    }
    assert.notEqual(session_hue('voidcraft'), session_hue('claude'));
});

// Type-level guard: every per-zone table keys off AgentZoneKey (the office's screen words in
// `agent-office-logic.ts`), so desk/thinking + the activity zones stay in sync with the union.
test('activity zones plus desk and thinking cover the AgentZoneKey union', () => {
    const all: AgentZoneKey[] = ['desk', 'thinking', ...ACTIVITY_ZONE_KEYS];
    assert.equal(new Set(all).size, 7);
});

test('zone scatter is deterministic — same spot every poll', () => {
    assert.deepEqual(zone_scatter_offset('voidcraft', 'web', 0.35, 0.8), zone_scatter_offset('voidcraft', 'web', 0.35, 0.8));
});

test('zone scatter stays on the annulus — off-center, inside the rim', () => {
    const zones: AgentZoneKey[] = ['desk', 'thinking', ...ACTIVITY_ZONE_KEYS];
    for (const session of ['voidcraft', 'claude', 'research', 'a']) {
        for (const zone of zones) {
            const { x, z } = zone_scatter_offset(session, zone, 0.35, 0.8);
            const radius = Math.hypot(x, z);
            assert.ok(radius >= 0.35 - 1e-9 && radius <= 0.8 + 1e-9, `${session}@${zone} → r=${radius}`);
        }
    }
});

test('zone scatter spreads a session across zones and sessions apart', () => {
    // Different zones give the same session different spots (organic travel)…
    assert.notDeepEqual(zone_scatter_offset('voidcraft', 'web', 0.35, 0.8), zone_scatter_offset('voidcraft', 'tests', 0.35, 0.8));
    // …and co-located sessions land apart.
    assert.notDeepEqual(zone_scatter_offset('voidcraft', 'web', 0.35, 0.8), zone_scatter_offset('claude', 'web', 0.35, 0.8));
});
