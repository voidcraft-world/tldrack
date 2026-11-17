import { test } from 'node:test';
import assert from 'node:assert/strict';
import { office_pixel_ratio } from '../../src/agent-office/office-render-quality.ts';

test('compact offices supersample on standard, Retina and dense phone displays', () => {
    for (const dpr of [1, 1.25, 2, 3]) {
        const ratio = office_pixel_ratio(640, 360, dpr, 16384);
        assert.ok(ratio > dpr);
        assert.ok(ratio <= 4);
    }
});

test('large and unusually wide pop-outs respect both the pixel budget and GPU limits', () => {
    for (const [width, height, dpr, limit] of [[3840, 2160, 2, 16384], [20000, 120, 2, 4096], [120, 20000, 3, 4096]]) {
        const ratio = office_pixel_ratio(width, height, dpr, limit);
        assert.ok(ratio > 0);
        assert.ok(width * height * ratio ** 2 <= 8_000_000 + 0.01);
        assert.ok(Math.max(width, height) * ratio <= limit);
    }
});

test('a collapsed host stays finite and recovering its size restores supersampling', () => {
    assert.ok(Number.isFinite(office_pixel_ratio(0, 0, 2, 4096)));
    const large = office_pixel_ratio(3840, 2160, 2, 16384);
    const compact = office_pixel_ratio(640, 360, 2, 16384);
    assert.ok(compact > large);
});
