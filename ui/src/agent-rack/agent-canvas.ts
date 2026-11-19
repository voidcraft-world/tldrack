/**
 * The agent CANVAS on the rack page: where it sits, how big, and what it
 * frames. The feature is owned by `plugins/agent-canvas/README.md`;
 * this file is the rack's half, pure, so `node --test` runs it directly.
 *
 * Three layouts, one persisted setting:
 *   - `under`  — the list column splits into two rows, canvas below. The
 *                default: it fills the rail's dead space and costs the
 *                terminal no width.
 *   - `column` — a third column to the RIGHT of the terminal. Right, never
 *                between rail and terminal: the terminal iframe's origin must
 *                stay on the device-pixel grid (`useDevicePixelRail.ts`), and
 *                a column after it changes its width, never its origin.
 *   - `off`.
 */
import { message_is } from '../events/messages.ts';

export const CANVAS_LAYOUTS = ['under', 'column', 'off'] as const;
export type CanvasLayout = (typeof CANVAS_LAYOUTS)[number];

export const CANVAS_LAYOUT_KEY = 'tldrack:agent-canvas-layout';
export const CANVAS_SIZE_KEY = 'tldrack:agent-canvas-size';

export const DEFAULT_CANVAS_LAYOUT: CanvasLayout = 'under';

/** The canvas's floors. Below these a canvas stops being readable. */
export const CANVAS_MIN_UNDER_PX = 140;
export const CANVAS_MIN_COLUMN_PX = 280;
/** What the session list keeps when the canvas sits under it. */
export const RAIL_KEEP_PX = 180;
/** Defaults before the operator drags: a share of the space it splits. */
export const CANVAS_UNDER_RATIO = 0.45;
export const CANVAS_COLUMN_RATIO = 0.3;

/** The canvas pane's slot for the opened row's next-step card: the rack component teleports the
 *  card here while a canvas is mounted, and under the row otherwise (`NextStepCard.vue`). */
export const NEXT_STEP_SLOT_ID = 'rack-next-step-slot';
/** The same, for an opened lead's DECISIONS card (`LeadDecisionsCard.vue`), above the next step. */
export const LEAD_DECISIONS_SLOT_ID = 'rack-lead-decisions-slot';

/** The `postMessage` type the canvas page posts to the rack (`kit.js` `hello`). */
export const CANVAS_MESSAGE_TYPE = 'tldrack-agent-canvas';

/** Session names as the bridge validates them (`snapshot.py` SESSION_NAME_RE). */
const SESSION_NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;

export function parse_canvas_layout(raw: unknown): CanvasLayout {
    return (CANVAS_LAYOUTS as readonly unknown[]).includes(raw) ? (raw as CanvasLayout) : DEFAULT_CANVAS_LAYOUT;
}

export function next_canvas_layout(layout: CanvasLayout): CanvasLayout {
    return CANVAS_LAYOUTS[(CANVAS_LAYOUTS.indexOf(layout) + 1) % CANVAS_LAYOUTS.length];
}

/** The chip's readout — the layout it is IN, never the one a click goes to. */
export function canvas_layout_label(layout: CanvasLayout): string {
    return { under: 'UNDER', column: 'COLUMN', off: 'OFF' }[layout];
}

/** The canvas page for one session, or null when there is nothing to frame. */
export function canvas_page_url(bridge_origin: string, session: string | null): string | null {
    if (session === null || !SESSION_NAME_RE.test(session)) return null;
    return `${bridge_origin.replace(/\/+$/, '')}/plugin/agent-canvas/page?session=${encodeURIComponent(session)}`;
}

/** Persisted drag sizes, each null until that split is dragged. */
export interface CanvasSizes {
    under_px: number | null;
    column_px: number | null;
}

export function parse_canvas_sizes(raw: string | null): CanvasSizes {
    const empty: CanvasSizes = { under_px: null, column_px: null };
    if (!raw) return empty;
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return empty;
    }
    if (typeof parsed !== 'object' || parsed === null) return empty;
    const read = (value: unknown): number | null =>
        typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
    const record = parsed as Record<string, unknown>;
    return { under_px: read(record.under_px), column_px: read(record.column_px) };
}

/** The canvas's height under the list, for a drag to `wanted_px`. A column too
 *  short for both floors gives the list its floor and the canvas the rest. */
export function clamp_canvas_under_px(wanted_px: number, column_height: number): number {
    if (!Number.isFinite(column_height) || column_height <= 0) return CANVAS_MIN_UNDER_PX;
    const ceiling = column_height - RAIL_KEEP_PX;
    if (ceiling <= CANVAS_MIN_UNDER_PX) return Math.max(0, ceiling);
    const wanted = Number.isFinite(wanted_px) ? wanted_px : column_height * CANVAS_UNDER_RATIO;
    return Math.min(Math.max(wanted, CANVAS_MIN_UNDER_PX), ceiling);
}

/** The canvas column's width, for a drag to `wanted_px`: the terminal keeps
 *  `term_min_px` (owned by `rack-window-geometry.ts` `TERM_MIN_PX`). */
export function clamp_canvas_column_px(
    wanted_px: number,
    window_width: number,
    rack_px: number,
    term_min_px: number,
): number {
    if (!Number.isFinite(window_width)) return CANVAS_MIN_COLUMN_PX;
    const ceiling = window_width - rack_px - term_min_px;
    if (ceiling <= CANVAS_MIN_COLUMN_PX) return CANVAS_MIN_COLUMN_PX;
    const wanted = Number.isFinite(wanted_px) ? wanted_px : window_width * CANVAS_COLUMN_RATIO;
    return Math.min(Math.max(wanted, CANVAS_MIN_COLUMN_PX), ceiling);
}

/** What one canvas page told the rack. */
export interface CanvasHello {
    session: string;
    has_canvas: boolean;
    has_content: boolean;
    question: string | null;
}

/** A `message` event's data as a canvas hello, or null. The SOURCE check (is it
 *  our iframe?) is the caller's — this only validates the shape. */
export function parse_canvas_hello(data: unknown): CanvasHello | null {
    if (typeof data !== 'object' || data === null) return null;
    const record = data as Record<string, unknown>;
    if (!message_is(record, CANVAS_MESSAGE_TYPE) || typeof record.session !== 'string') return null;
    return {
        session: record.session,
        has_canvas: record.has_canvas === true,
        // Older plugin pages may still own artifacts or skills with no canvas doc.
        has_content: record.has_content !== false,
        question: typeof record.question === 'string' && record.question ? record.question.slice(0, 120) : null,
    };
}
