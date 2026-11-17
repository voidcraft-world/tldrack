import test from 'node:test';
import assert from 'node:assert/strict';
import { clamp_map_share, clamp_section_height, map_drag_scale, parse_map_share, parse_section_height } from '../../src/agent-rack/canvas-sections.ts';

test('section preferences reject corrupt storage while accepting saved pixel heights', () => {
    for (const raw of [null, '', ' ', 'wide', 'Infinity', '-1', '0', '{}']) {
        assert.equal(parse_section_height('office', raw), null, String(raw));
    }
    assert.equal(parse_section_height('office', '525.8'), 526);
    assert.equal(parse_section_height('details', '180'), 180);
    assert.equal(parse_section_height('document', '340'), 340);
});

test('oversized and undersized drags keep every section reachable and usable', () => {
    assert.equal(clamp_section_height('office', 1), 180);
    assert.equal(clamp_section_height('office', 9000), 1200);
    assert.equal(clamp_section_height('details', 1), 120);
    assert.equal(clamp_section_height('details', 9000), 600);
    assert.equal(clamp_section_height('document', Number.NaN), 180);
    assert.equal(parse_section_height('details', '10000'), 600);
});

test('the map gets three quarters by default and remembers valid width choices', () => {
    for (const raw of [null, '', ' ', 'wide', 'Infinity', '-1', '0', '{}']) {
        assert.equal(parse_map_share(raw), 75, String(raw));
    }
    assert.equal(parse_map_share('62.5'), 62.5);
    assert.equal(parse_map_share('1000'), 85);
    assert.equal(parse_map_share('1'), 15);
    assert.equal(clamp_map_share(Number.NaN), 75);
});

test('a pointer drag excludes the divider and preserves both panes at either edge', () => {
    const scale = map_drag_scale(412, 12);
    assert.equal(scale, 0.25);
    assert.equal(clamp_map_share(75 - 80 * scale!), 55);
    assert.equal(clamp_map_share(75 + 500 * scale!), 85);
    assert.equal(clamp_map_share(75 - 500 * scale!), 15);
    assert.equal(map_drag_scale(428, 28), scale);
    for (const [width, divider] of [[0, 12], [12, 12], [8, 12], [NaN, 12], [400, Infinity], [400, -1]]) {
        assert.equal(map_drag_scale(width, divider), null);
    }
});
