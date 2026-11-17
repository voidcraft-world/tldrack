/**
 * Size + position persistence for a rack page that runs as a window of its own.
 *
 * Pure on purpose: `window.open` has to run synchronously inside the click
 * gesture or the browser treats it as an unrequested popup, so there is no room
 * for an async read — this is localStorage + arithmetic, and the
 * arithmetic is where all the bugs live. Three of them, each encoded below:
 *
 *   1. A NaN in a window-features string is SILENTLY DROPPED by Chrome, which
 *      then picks its own geometry. A corrupt stored rect must therefore fail
 *      loudly to `null` (→ defaults), never leak a NaN downstream.
 *   2. `screen.availLeft` is NEGATIVE when a monitor sits to the LEFT of the
 *      primary. Clamping x against 0 — the obvious thing — drags the window
 *      back onto the primary display on every single launch.
 *   3. `screen.availWidth/Height` describe the PRIMARY DISPLAY ONLY. A strict
 *      clamp to that box would likewise yank back a window the operator
 *      deliberately parked on a second monitor to the right. So this uses a
 *      deliberately GENEROUS envelope: wide enough for any realistic
 *      multi-monitor arrangement, tight enough that a stranded, unreachable
 *      window is still impossible.
 *
 * Restoring exactly onto a named display would need `window.getScreenDetails()`
 * (Window Management API, permission-gated). Deliberately out of scope.
 *
 * No enums and no parameter properties: `node --test` runs this file directly
 * in strip-only mode.
 */
import { parse_json_object } from '../utils/parse-json-object.ts';

export interface RackWindowRect {
    x: number;
    y: number;
    w: number;
    h: number;
}

/** The primary display's usable box, as reported by `screen`. `left`/`top` may
 *  be negative — see note 2 in the module header. */
export interface AvailScreen {
    left: number;
    top: number;
    width: number;
    height: number;
}

/** Below this the rack list and the terminal can't both be usable. */
export const MIN_W = 520;
export const MIN_H = 360;

export const STORAGE_KEY = 'tldrack:agent-rack-window-rect';

/** Where the operator dragged the rack/terminal split, in CSS px. Absent until they
 *  drag it — see `clamp_split_px` for why absent is not the same as a number. */
export const SPLIT_STORAGE_KEY = 'tldrack:agent-rack-split-px';

/** Floor for the TERMINAL half of the split. Below this a terminal stops being
 *  one — the same judgement `NARROW_PX` makes about the window as a whole. */
export const TERM_MIN_PX = 320;

/**
 * The rack pane's width for a drag to `wanted_px`, clamped so neither half
 * collapses.
 *
 * Kept here, pure, because the arithmetic is where the bugs are — the same
 * reason `clamp_rect` lives beside it.
 *
 * `rack_min_px` is passed in rather than declared here: it is paired with
 * `.rack-window__list`'s CSS `min-width` and must be read next to it, so
 * AgentRackWindow.vue stays its owner.
 *
 * A window too narrow to satisfy BOTH floors resolves to the rack floor rather
 * than splitting the difference. That state is not this function's to fix: below
 * `NARROW_PX` the window stops splitting at all and stacks the two panes.
 */
export function clamp_split_px(wanted_px: number, window_width: number, rack_min_px: number): number {
    if (!Number.isFinite(wanted_px) || !Number.isFinite(window_width)) return rack_min_px;
    const ceiling = window_width - TERM_MIN_PX;
    if (ceiling <= rack_min_px) return rack_min_px;
    return Math.min(Math.max(wanted_px, rack_min_px), ceiling);
}

/**
 * A stored split width, or `null` for "never dragged / unreadable".
 *
 * Null is a MEANINGFUL answer, not a failure: with no stored width the split
 * follows the window (`pop_out_rack_ratio` of its width), which is the right
 * default for a window the operator has not expressed an opinion about. A stored
 * width is respected LITERALLY on every window size, because a session list
 * needs the pixels it needs — rescaling someone's deliberate choice is what
 * makes a resizable split feel broken.
 *
 * Corrupt values fail to null rather than downstream, for the reason
 * `parse_rect` gives: a NaN that escapes here becomes a NaN in a layout.
 */
export function parse_split_px(raw: string | null): number | null {
    if (raw === null || raw.trim() === '') return null;
    const px = Number(raw);
    return Number.isFinite(px) && px > 0 ? px : null;
}

/** How far outside the primary display a stored rect may sit and still be
 *  believed. One screen-width to the left, three to the right / down — that
 *  covers a 4-wide monitor wall without ever believing a rect at x=999999. */
const ENVELOPE_BEFORE = 1;
const ENVELOPE_AFTER = 3;

function is_finite_number(value: unknown): value is number {
    return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Parse a stored rect, rejecting anything that isn't four finite numbers.
 *
 * Returning `null` for garbage is the whole point: the caller falls back to the
 * flag defaults, whereas a NaN reaching `features_string` produces a window
 * roughly 100x100 with no error anywhere.
 */
export function parse_rect(raw: string | null): RackWindowRect | null {
    const candidate = parse_json_object(raw);
    if (candidate === null) return null;
    if (
        !is_finite_number(candidate.x) ||
        !is_finite_number(candidate.y) ||
        !is_finite_number(candidate.w) ||
        !is_finite_number(candidate.h)
    ) {
        return null;
    }
    return { x: candidate.x, y: candidate.y, w: candidate.w, h: candidate.h };
}

function clamp(value: number, low: number, high: number): number {
    if (high < low) return low;
    return Math.min(Math.max(value, low), high);
}

/**
 * Bring a stored rect back into the realm of the plausible.
 *
 * Size is clamped hard (a 50x50 window is never what anyone wanted, and one
 * larger than the display is pointless). Position is clamped only to the
 * generous multi-monitor envelope, so a rect that was legitimately on another
 * display survives a round trip — see notes 2 and 3 in the module header.
 */
export function clamp_rect(rect: RackWindowRect, screen: AvailScreen): RackWindowRect {
    const w = clamp(Math.round(rect.w), MIN_W, Math.max(MIN_W, Math.round(screen.width)));
    const h = clamp(Math.round(rect.h), MIN_H, Math.max(MIN_H, Math.round(screen.height)));

    // NOTE the bounds are anchored on screen.left/top, NOT on 0.
    const min_x = screen.left - screen.width * ENVELOPE_BEFORE;
    const max_x = screen.left + screen.width * ENVELOPE_AFTER - w;
    const min_y = screen.top - screen.height * ENVELOPE_BEFORE;
    const max_y = screen.top + screen.height * ENVELOPE_AFTER - h;

    return {
        x: Math.round(clamp(rect.x, min_x, max_x)),
        y: Math.round(clamp(rect.y, min_y, max_y)),
        w,
        h,
    };
}

/**
 * The third argument to `window.open`.
 *
 * Integers only — a fractional value is ignored by some browsers, which then
 * substitute their own size for that dimension alone, giving a window that is
 * the right width and the wrong height.
 */
export function features_string(rect: RackWindowRect): string {
    return [
        'popup',
        `width=${Math.round(rect.w)}`,
        `height=${Math.round(rect.h)}`,
        `left=${Math.round(rect.x)}`,
        `top=${Math.round(rect.y)}`,
    ].join(',');
}

/**
 * Read the primary display's usable box.
 *
 * `availLeft` / `availTop` are non-standard but present in Chrome and Safari;
 * `?? 0` degrades to "the display starts at the origin", which is correct for
 * every single-monitor setup and for a primary-left arrangement.
 */
export function read_avail_screen(): AvailScreen {
    const s = window.screen as Screen & { availLeft?: number; availTop?: number };
    return {
        left: is_finite_number(s?.availLeft) ? s.availLeft : 0,
        top: is_finite_number(s?.availTop) ? s.availTop : 0,
        width: is_finite_number(s?.availWidth) ? s.availWidth : 1280,
        height: is_finite_number(s?.availHeight) ? s.availHeight : 800,
    };
}
