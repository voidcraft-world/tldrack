import test from 'node:test';
import assert from 'node:assert/strict';

import { area_to_baseline, arrow_step, clamp, line_path, linear_scale, nearest_index, nice_ceiling, nice_ticks, nice_top, step_path, unit_tick_anchor } from '../../src/evals/chart-geometry.ts';

test('a linear scale maps its domain onto its range, either way up', () => {
    const x = linear_scale([0, 1], [50, 450]);
    assert.equal(x(0), 50);
    assert.equal(x(0.5), 250);
    assert.equal(x(1), 450);
    const y = linear_scale([0, 200], [300, 20]);
    assert.equal(y(0), 300);
    assert.equal(y(200), 20);
    assert.equal(linear_scale([3, 3], [10, 90])(3), 10, 'a flat domain has one place to draw');
});

test('round numbers: the ceiling, the top of an axis, the ticks', () => {
    assert.deepEqual([0.127, 1, 26, 104, 250].map(nice_ceiling), [0.2, 1, 50, 200, 250]);
    assert.equal(nice_ceiling(0), 1);
    assert.equal(nice_top(104), 125, 'an axis ends on the first step above its data, not a whole power above it');
    assert.equal(nice_top(50, 2), 50);
    assert.equal(nice_top(0.254, 2), 0.4);
    assert.equal(nice_top(0), 1);
    assert.deepEqual(nice_ticks(0, 125, 5), [0, 25, 50, 75, 100, 125]);
    assert.deepEqual(nice_ticks(0, 1, 4), [0, 0.25, 0.5, 0.75, 1]);
    assert.deepEqual(nice_ticks(2, 2), [2]);
});

test('a polyline, and the two staircases', () => {
    const points = [[0, 10], [5, 4], [9, 4.005]] as const;
    assert.equal(line_path(points), 'M0,10L5,4L9,4.01');
    assert.equal(step_path(points, 'after'), 'M0,10L5,10L5,4L9,4L9,4.01', 'a value holds on to the next point');
    assert.equal(step_path(points, 'before'), 'M0,10L0,4L5,4L5,4.01L9,4.01', 'a value holds back to the point before');
    assert.equal(line_path([]), '');
    assert.equal(area_to_baseline('M0,10L5,4', [[0, 10], [5, 4]], 20), 'M0,10L5,4L5,20L0,20Z');
});

test('the nearest stop, and a value held in range', () => {
    assert.equal(nearest_index([0, 0.1, 0.4, 1], 0.3), 2);
    assert.equal(nearest_index([0, 0.1, 0.4, 1], -5), 0);
    assert.equal(nearest_index([], 0.3), -1);
    assert.equal(clamp(1.4, 0, 1), 1);
    assert.equal(clamp(-0.2, 0, 1), 0);
});

test('arrow keys walk a readout and stop at its ends', () => {
    assert.equal(arrow_step('ArrowRight', null, 4), 0, 'the first press lands on the first stop');
    assert.equal(arrow_step('ArrowLeft', null, 4), 4, 'or on the last, from the other side');
    assert.equal(arrow_step('ArrowRight', 4, 4), 4);
    assert.equal(arrow_step('ArrowLeft', 0, 4), 0);
    assert.equal(arrow_step('Escape', 2, 4), null);
    assert.equal(arrow_step('Tab', 2, 4), undefined, 'any other key is left to the page');
});

test('the end labels of a unit axis hang inward', () => {
    assert.deepEqual([0, 0.5, 1].map(unit_tick_anchor), ['start', 'middle', 'end']);
});
