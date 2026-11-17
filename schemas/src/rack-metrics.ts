import { z } from 'zod';

/** Local interaction measurements, never transcript or command contents. Python owner: rack_metrics.py. */
export const rack_metric_event_schema = z.object({
    id: z.string().uuid(),
    client: z.string().uuid(),
    at: z.number().int().nonnegative(),
    kind: z.enum(['select', 'exposure', 'action']),
    session: z.string().regex(/^[A-Za-z0-9_-]{1,64}$/).nullable(),
    state: z.enum(['busy', 'attention', 'background', 'done', 'free', 'unknown']),
    mark: z.enum(['open', 'done', 'background', 'reply', 'none']),
    action: z.enum(['select', 'kill', 'rename', 'new', 'worktree', 'pr', 'cleanup', 'dismiss_open_work', 'snooze_open_work', 'confirm_open_work', 'reopen', 'group_control', 'lead', 'exposure']),
    source: z.enum(['window', 'embedded']),
    reselect: z.boolean(),
    duration_ms: z.number().int().min(0).max(20_000),
    fleet_size: z.number().int().min(0).max(10_000),
    context_left: z.number().min(0).max(100).nullable(),
    model: z.string().max(160).nullable(),
    profile: z.string().max(80).nullable(),
}).strict();
export type RackMetricEvent = z.infer<typeof rack_metric_event_schema>;

const totals_schema = z.object({
    clicks: z.number(), sessions: z.number(), revisits: z.number(), reselects: z.number(),
    working_clicks: z.number(), attention_clicks: z.number(), warning_clicks: z.number(),
    done_clicks: z.number(), identified_clicks: z.number(), visible_ms: z.number(),
    clicks_per_session: z.number().nullable(), clicks_per_hour: z.number().nullable(),
    // Attention signals the bridge raised / held back (rack_metrics.py § Attention signals).
    // Defaulted: a bridge from before them sends neither.
    interruptions: z.number().default(0), avoided: z.number().default(0),
});
export const rack_metrics_summary_schema = z.object({
    version: z.literal(1), enabled: z.boolean(), started_at: z.number().nullable(),
    last_event_at: z.number().nullable(), path: z.string(), timezone: z.string(),
    from: z.string(), to: z.string(), total: totals_schema,
    days: z.array(totals_schema.extend({ day: z.string(), observed: z.boolean() })),
    sessions: z.array(totals_schema.extend({ key: z.string(), session: z.string(), model: z.string().nullable(), profile: z.string().nullable() })),
    actions: z.array(z.object({ action: z.string(), count: z.number() })),
    states: z.array(z.object({ state: z.string(), count: z.number() })),
    marks: z.array(z.object({ mark: z.string(), count: z.number() })),
    sources: z.array(z.object({ source: z.string(), count: z.number() })),
    markers: z.array(z.object({ at: z.number(), label: z.string() })),
    signals: z.array(z.object({ signal: z.string(), reason: z.string().nullable(), count: z.number() })).default([]),
});
export type RackMetricsSummary = z.infer<typeof rack_metrics_summary_schema>;
