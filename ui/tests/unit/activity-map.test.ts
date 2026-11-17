import { test } from 'node:test';
import assert from 'node:assert/strict';
import { activity_map_image, map_station, observe_activity, MAP_SAMPLE_LIMIT } from '../../src/agent-rack/activity-map.ts';
import type { RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';

const row: RackSession = { session: 'agent-a', state: 'busy', live: true };

test('classifies the shared rack activity without reading commands or prompts', () => {
    for (const [tool, zone, expected] of [
        ['Read', 'codebase', 'files'], ['Bash', 'tests', 'shell'], ['Bash', 'aws', 'web'],
        ['Skill', 'computer', 'skills'], ['Agent', 'computer', 'agents'],
        ['functions.spawn_agent', 'computer', 'agents'], ['mcp__cua_repl__js', 'computer', 'browser'],
        ['mcp__memory__recall', 'computer', 'memory'], ['functions.apply_patch', 'computer', 'files'],
        ['WebSearch', 'web', 'web'], ['AskUserQuestion', 'computer', 'you'],
    ]) {
        assert.equal(map_station({ ...row, activity: { tool, zone, detail: 'private' } }), expected);
    }
    assert.equal(map_station(row), 'model');
    assert.equal(map_station({ ...row, session: 'svc-vite' }), null);
    assert.equal(map_station({ ...row, state: 'unknown' }), null);
    assert.equal(map_station({ ...row, live: false }), null);
});

test('counts observations, deduplicates polls and never fabricates tool outcomes or durations', () => {
    const active = { ...row, activity: { tool: 'Read', zone: 'codebase', detail: 'private.ts' } };
    const first = observe_activity(null, active, 100);
    const second = observe_activity(first, { ...active }, 200);
    assert.deepEqual(second.board.entries, first.board.entries);
    assert.equal(second.board.counts.files, 1);
    const third = observe_activity(second, { ...active, activity: { ...active.activity, detail: 'other.ts' } }, 300);
    assert.equal(third.board.counts.files, 2);
    assert.equal(third.board.entries[0].endedAt, undefined);
    assert.ok(third.board.entries.every(entry => entry.status !== 'run'));
    assert.ok(!JSON.stringify(third.board).includes('private.ts'));
    const stopped = observe_activity(third, { ...active, state: 'free' }, 400);
    assert.equal(stopped.label, 'Idle');
    assert.equal(stopped.board.entries.length, 2);
});

test('switching sessions clears observations, and context is used rather than remaining', () => {
    const first = observe_activity(null, { ...row, context_left: 40, model_label: 'opus' }, 100);
    assert.equal(first.board.context, 60);
    const next = observe_activity(first, { ...row, session: 'agent-b', state: 'free' }, 200);
    assert.deepEqual(next.board.counts, {});
    assert.equal(next.board.entries.length, 0);
    assert.equal(next.board.model, undefined);
    assert.equal(next.board.context, undefined);
    assert.equal(observe_activity(null, { ...row, context_left: NaN }, 0).board.context, undefined);
});

test('retains bounded history, only names enter SVG, and reduced motion removes SMIL', () => {
    let map = observe_activity(null, row, 0);
    for (let i = 1; i <= 100; i++) {
        map = observe_activity(map, { ...row, activity: { tool: 'Read', zone: 'codebase', detail: `secret-${i}` } }, i * 100);
    }
    assert.equal(map.board.entries.length, MAP_SAMPLE_LIMIT);
    assert.equal(map.board.counts.files, 100);
    const svg = decodeURIComponent(activity_map_image(map, 10000, false).split(',')[1]);
    assert.ok(svg.includes('<svg'));
    assert.ok(!svg.includes('<animate'));
    assert.ok(!svg.includes('secret-'));
    const hostile = observe_activity(null, { ...row, model_label: '<script>alert(1)</script>' }, 100);
    assert.ok(!decodeURIComponent(activity_map_image(hostile, 100, true)).includes('<script>'));
});

test('the board highlights only the current category and keeps idle history without live activity', () => {
    const active = observe_activity(null, { ...row, auto_name: 'Demo task', context_left: 40,
        activity: { tool: 'Bash', zone: 'tests', detail: 'private command' } }, 0);
    const svg = decodeURIComponent(activity_map_image(active, 0, true).split(',')[1]);
    assert.ok(svg.includes('Demo task'));
    assert.ok(svg.includes('CTX 60% USED'));
    assert.ok(svg.includes('data-station="shell" data-active="true"'));
    assert.equal((svg.match(/data-active="true"/g) ?? []).length, 1);
    assert.ok(svg.includes('<animateMotion'));
    assert.ok(!svg.includes('private command'));
    const idle = observe_activity(active, { ...row, state: 'free' }, 1);
    const stopped = decodeURIComponent(activity_map_image(idle, 1, false).split(',')[1]);
    assert.ok(!stopped.includes('data-active="true"'));
    assert.ok(!stopped.includes('<animate'));
    assert.equal(idle.board.counts.shell, 1);
});

test('wide and narrow boards retain all ten categories without importing upstream artwork', () => {
    const map = observe_activity(null, { ...row, auto_name: 'Example', activity: { tool: 'Read', zone: 'codebase', detail: '' } }, 0);
    for (const size of [{ width: 832, height: 510 }, { width: 384, height: 440 }]) {
        const svg = decodeURIComponent(activity_map_image(map, 0, false, size).split(',')[1]);
        assert.equal((svg.match(/data-station=/g) ?? []).length, 10);
        assert.ok(svg.includes(`viewBox="0 0 ${size.width} ${size.height}"`));
        assert.ok(svg.includes('fill="#2d3035"'));
        assert.ok(!svg.includes('#111a15'));
        assert.ok(svg.includes('Counts and trail show observed changes, not tool-call totals.'));
        assert.ok(!svg.includes('<script'));
        assert.ok(!svg.includes('foreignObject'));
    }
});

test('session names and tool names are escaped, and context is absent when unavailable', () => {
    const map = observe_activity(null, { ...row, auto_name: '\"><svg onload="alert(1)">',
        activity: { tool: '<image href="bad"/>', zone: 'computer', detail: 'never render' } }, 0);
    const svg = decodeURIComponent(activity_map_image(map, 0, false).split(',')[1]);
    assert.ok(!svg.includes('<svg onload'));
    assert.ok(!svg.includes('<image href'));
    assert.ok(!svg.includes('never render'));
    assert.ok(svg.includes('CTX —'));
});
