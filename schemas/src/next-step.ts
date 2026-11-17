import { z } from 'zod';

/**
 * The suggested next step for a rack session whose turn ended — a SUGGESTION, never an act.
 * Python owner: `bridge/tldrack/next_step.py` (what, when, the checks, the log).
 *
 * The vocabulary is the rack's own per-row controls and nothing else, `kill` last as on the row.
 * Twin of `next_step.py::ACTIONS`, pinned by `schemas/tests/twins.test.ts`.
 */
export const NEXT_STEP_ACTIONS = ['select', 'pr', 'cleanup', 'kill'] as const;
export type NextStepAction = (typeof NEXT_STEP_ACTIONS)[number];

const hash = z.string().regex(/^[0-9a-f]{16}$/);

/** The additive `next_step` field on a live `/snapshot` row — present only in `live` mode. */
export const next_step_schema = z.object({
    kind: z.enum(['suggest', 'none']),
    action: z.enum(NEXT_STEP_ACTIONS).nullable(),
    /** The control's name on the chip: `Open`, `PR`, `Clean`, `Close`. */
    label: z.string().max(16).nullable(),
    reason: z.string().min(1).max(90),
    /** Model: a verbatim quote from the closing message. Rules: the runtime fact that decided it. */
    evidence: z.string().max(400).nullable(),
    source: z.enum(['rules', 'model']),
    model_version: z.string().max(80),
    hash,
}).strict().refine((step) => (step.kind === 'suggest') === (step.action !== null), {
    message: 'an action exactly when kind is suggest',
});
export type NextStep = z.infer<typeof next_step_schema>;

/** The snapshot's top-level `next_step`: the suggester's mode, present while it is not `off`. */
export const next_step_mode_schema = z.object({ mode: z.enum(['shadow', 'live']) });
export type NextStepMode = z.infer<typeof next_step_mode_schema>['mode'];

/** `POST /next-step/event` — what the rack did. A press is REPORTED, never performed: the control
 *  already fired through its own handler. `via: 'card'` is [Do it]. */
export const next_step_event_schema = z.discriminatedUnion('event', [
    z.object({ event: z.literal('shown'), hash }).strict(),
    z.object({ event: z.literal('dismissed'), hash }).strict(),
    z.object({ event: z.literal('press'), action: z.enum(NEXT_STEP_ACTIONS), via: z.enum(['manual', 'card']),
        hash: hash.optional() }).strict(),
]);
export type NextStepEvent = z.infer<typeof next_step_event_schema>;

/** `GET /next-step` — the AGENT RACK › NEXT STEP row's readout. Counts only, never transcript text. */
const outcome_counts = z.object({
    shown: z.number(), clicked: z.number(), dismissed: z.number(),
    taken: z.number(), other: z.number(), ignored: z.number(),
});
export const next_step_status_schema = z.object({
    mode: z.enum(['off', 'shadow', 'live']),
    model: z.string(),
    model_version: z.string(),
    pending: z.number(),
    suggested: z.number(),
    none: z.number(),
    rejected: z.record(z.string(), z.number()),
    by_action: z.record(z.string(), z.number()),
    outcomes: outcome_counts,
    resolved: z.number(),
    took: z.number(),
    take_rate: z.number().nullable(),
}).passthrough();
export type NextStepStatus = z.infer<typeof next_step_status_schema>;
