/**
 * The three optional parts of the bridge that had a switch before the feature list did, each on
 * its own routes. The FEATURES tab draws them beside `features-client.ts`'s rows; what each part
 * is and what its switch means is owned by its module in `bridge/tldrack/` (named per part below).
 *
 * Each reader takes only the fields the page shows. The replies carry far more (a week of counts,
 * a month of metrics), and none of it is this page's to draw.
 */
import { z } from 'zod';

import { bridge_read, bridge_write, type BridgeResult } from './bridge-call.ts';

// ── next step suggester: `next_step.py` ──────────────────────────────────────

export const NEXT_STEP_MODES = ['off', 'shadow', 'live'] as const;
export type NextStepSwitch = (typeof NEXT_STEP_MODES)[number];

/** What each mode does, for the control's title and the line under it. */
export const NEXT_STEP_MODE_NOTES: Readonly<Record<NextStepSwitch, string>> = {
    off: 'Off: no suggestions are made.',
    shadow: 'Shadow: suggestions are made and logged, and the rack shows none.',
    live: 'Live: a finished turn’s row and its canvas show the suggestion.',
};

const next_step_schema = z.object({ mode: z.enum(NEXT_STEP_MODES), model: z.string().max(120).optional() });
export type NextStepPart = z.infer<typeof next_step_schema>;

export function parse_next_step(body: unknown): NextStepPart | null {
    const parsed = next_step_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

/** `days=1`: the reply summarises that much of the log, and the page reads none of the summary. */
export function fetch_next_step(): Promise<BridgeResult<NextStepPart>> {
    return bridge_read('/next-step?days=1', next_step_schema, 'a next-step status');
}

const next_step_set_schema = z.object({ mode: z.enum(NEXT_STEP_MODES) });

export function set_next_step_mode(mode: NextStepSwitch): Promise<BridgeResult<{ mode: NextStepSwitch }>> {
    return bridge_write('/next-step/settings', { mode }, next_step_set_schema, 'a next-step reply');
}

// ── phone pushes: `notify.py` ────────────────────────────────────────────────

/** How a topic is made. The bridge takes none over HTTP (`POST /notify` owns why), so the page shows the command. */
export const NOTIFY_INIT_COMMAND = 'tldrack notify --init';

const notify_schema = z.object({
    configured: z.boolean(),
    enabled: z.boolean(),
    server: z.string().max(400).optional(),
    running: z.boolean().optional(),
});
export type NotifyPart = z.infer<typeof notify_schema>;

export function parse_notify(body: unknown): NotifyPart | null {
    const parsed = notify_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

export function fetch_notify(): Promise<BridgeResult<NotifyPart>> {
    return bridge_read('/notify', notify_schema, 'a phone push status');
}

/**
 * Turn the pushes on or off. A bridge with no topic answers 409 with the state it is in: that
 * state is returned as the answer (the switch stays off and the row shows the command), because
 * "nowhere to send to" is something the page draws, not an error it reports.
 */
export async function set_notify(enabled: boolean): Promise<BridgeResult<NotifyPart>> {
    const result = await bridge_write('/notify', { enabled }, notify_schema, 'a phone push status');
    if (result.ok || result.status !== 409) return result;
    const state = parse_notify(result.body);
    return state && !state.configured ? { ok: true, data: state } : result;
}

// ── rack metrics: `rack_metrics.py` ──────────────────────────────────────────

const rack_metrics_schema = z.object({ enabled: z.boolean() });
export type RackMetricsPart = z.infer<typeof rack_metrics_schema>;

export function parse_rack_metrics(body: unknown): RackMetricsPart | null {
    const parsed = rack_metrics_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

export function fetch_rack_metrics(): Promise<BridgeResult<RackMetricsPart>> {
    return bridge_read('/rack-metrics', rack_metrics_schema, 'a rack metrics summary');
}

const rack_metrics_set_schema = z.object({ ok: z.literal(true), message: z.string().max(400).optional() });

/** Pause or resume recording. The reply names what happened and not the state, so a 200 IS the new state. */
export async function set_rack_metrics(enabled: boolean): Promise<BridgeResult<RackMetricsPart>> {
    const result = await bridge_write('/rack-metrics/settings', { enabled }, rack_metrics_set_schema, 'a rack metrics reply');
    return result.ok ? { ok: true, data: { enabled } } : result;
}
