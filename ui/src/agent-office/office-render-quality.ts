/**
 * What the office costs to draw, by the operator's quality switch (`config/rack-prefs.ts`
 * `office_quality`). Two dials: how many pixels a frame holds, and how often a frame is drawn.
 *
 *   sharp  supersampled, every animation frame. The office as it was before the switch.
 *   light  the display's own pixels, about 30 frames a second: a quarter of the pixels on a 1×
 *          display (four ninths on a 2×) and half the frames at 60 Hz, for softer glyph edges.
 */
import type { OfficeQuality } from '../config/rack-prefs.ts';

/**
 * The drawing buffer's pixels per CSS pixel.
 *
 * `sharp`: supersample the compact office, including glyph interiors that MSAA cannot smooth.
 * `light`: the device's own ratio and never more, so one buffer pixel lands on one device pixel.
 * Not a flat 1: on a 2× display one CSS pixel is two device pixels, and a ratio of 1 would be
 * UPSCALED by the compositor into a blur. Capped at 2, where a 3× phone stops gaining anything a
 * small canvas can show.
 *
 * Both keep a larger pop-out within eight million pixels and the GPU's buffer/viewport limits.
 */
export function office_pixel_ratio(
    width: number,
    height: number,
    device_ratio: number,
    max_dimension: number,
    quality: OfficeQuality = 'sharp',
): number {
    const w = Math.max(1, width), h = Math.max(1, height);
    const desired = quality === 'light'
        ? Math.min(2, device_ratio > 0 ? device_ratio : 1)
        : Math.min(4, Math.max(2, device_ratio * 1.5));
    return Math.min(desired, Math.sqrt(8_000_000 / (w * h)), max_dimension / Math.max(w, h));
}

/**
 * The least time between two `light` frames. A clock, not "every other frame": a frame count
 * would still draw 60 times a second on a 120 Hz display. 30 ms rather than 1000/30 so that two
 * 60 Hz frames (33.3 ms) clear it with room for timer jitter: every second frame at 60 Hz, every
 * fourth at 120 Hz.
 */
export const LIGHT_FRAME_MS = 30;

/** Should the animation frame at `now_ms` draw, given the last one that did? `sharp` always. */
export function office_frame_due(quality: OfficeQuality, now_ms: number, last_draw_ms: number): boolean {
    return quality !== 'light' || now_ms - last_draw_ms >= LIGHT_FRAME_MS;
}
