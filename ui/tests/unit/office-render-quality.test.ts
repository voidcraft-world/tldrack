import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LIGHT_FRAME_MS, office_frame_due, office_pixel_ratio } from '../../src/agent-office/office-render-quality.ts';

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

test('sharp is the office as it was: the default, and the same numbers when named', () => {
    // Pinned as numbers, not as a formula: `sharp` is the quality every browser that never opened
    // the settings page draws at, so a drift here changes the rack for everyone.
    for (const [width, height, dpr, limit, ratio] of [
        [640, 360, 1, 16384, 2], [640, 360, 1.25, 16384, 2], [640, 360, 2, 16384, 3], [640, 360, 3, 16384, 4],
        [3840, 2160, 2, 16384, Math.sqrt(8_000_000 / (3840 * 2160))], [20000, 120, 2, 4096, 4096 / 20000],
    ]) {
        assert.equal(office_pixel_ratio(width, height, dpr, limit), ratio);
        assert.equal(office_pixel_ratio(width, height, dpr, limit, 'sharp'), ratio);
    }
});

test('light draws the display\'s own pixels: never supersampled, never upscaled, capped at 2', () => {
    assert.equal(office_pixel_ratio(640, 360, 1, 16384, 'light'), 1);
    // One CSS pixel is two device pixels here: a ratio of 1 would be blown up into a blur.
    assert.equal(office_pixel_ratio(640, 360, 2, 16384, 'light'), 2);
    assert.equal(office_pixel_ratio(640, 360, 1.25, 16384, 'light'), 1.25);
    assert.equal(office_pixel_ratio(640, 360, 3, 16384, 'light'), 2);
    // Zoomed out, the device has FEWER pixels than CSS pixels; native is still the ceiling.
    assert.equal(office_pixel_ratio(640, 360, 0.5, 16384, 'light'), 0.5);
    for (const dpr of [1, 1.25, 2, 3]) {
        assert.ok(office_pixel_ratio(640, 360, dpr, 16384, 'light') < office_pixel_ratio(640, 360, dpr, 16384, 'sharp'));
    }
});

test('light keeps the pixel budget and the GPU limits, and survives a ratio that is not a number', () => {
    for (const [width, height, dpr, limit] of [[3840, 2160, 2, 16384], [20000, 120, 2, 4096], [120, 20000, 3, 4096]]) {
        const ratio = office_pixel_ratio(width, height, dpr, limit, 'light');
        assert.ok(ratio > 0);
        assert.ok(width * height * ratio ** 2 <= 8_000_000 + 0.01);
        assert.ok(Math.max(width, height) * ratio <= limit);
    }
    for (const dpr of [0, -1, Number.NaN]) assert.equal(office_pixel_ratio(640, 360, dpr, 16384, 'light'), 1);
});

/** How many of `frames` animation frames `quality` draws on a display refreshing every `step_ms`. */
function drawn(quality: 'sharp' | 'light', step_ms: number, frames: number): number {
    let last = -Infinity, count = 0;
    for (let i = 0; i < frames; i += 1) {
        const now = 1000 + i * step_ms;
        if (!office_frame_due(quality, now, last)) continue;
        last = now;
        count += 1;
    }
    return count;
}

test('sharp draws every animation frame, whatever the display', () => {
    for (const hz of [60, 120, 144]) assert.equal(drawn('sharp', 1000 / hz, hz), hz);
});

test('light draws about 30 frames a second on any display, the first frame included', () => {
    assert.equal(office_frame_due('light', 0, -Infinity), true);
    assert.equal(drawn('light', 1000 / 60, 60), 30); // every other frame
    assert.equal(drawn('light', 1000 / 120, 120), 30); // a frame count would have drawn 60
    assert.ok(drawn('light', 1000 / 144, 144) <= 30);
    assert.ok(drawn('light', 1000 / 144, 144) >= 24);
    // Never faster than the clock, and a slow display is not slowed further.
    assert.equal(office_frame_due('light', 100 + LIGHT_FRAME_MS - 0.1, 100), false);
    assert.equal(drawn('light', 1000 / 30, 30), 30);
});
