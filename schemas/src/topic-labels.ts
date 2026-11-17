import { z } from 'zod';

/**
 * The bridge's `/topic-labels`: the NAME card on the REVIEW page, where the operator says whether a
 * rack session's auto name (Haiku's topic slug) fit. Owner of the rows, and why renames count as
 * verdicts: `bridge/tldrack/topic_labels.py`.
 */

export const TOPIC_VERDICTS = ['fits', 'wrong'] as const;
export const topicVerdictSchema = z.enum(TOPIC_VERDICTS);
export type TopicVerdict = z.infer<typeof topicVerdictSchema>;

/** `valid_title` in `session_names.py`: a short line, no control characters. */
export const TOPIC_BETTER_MAX = 56;

export const topicLabelsViewSchema = z.object({
    session: z.string().min(1).max(128),
    /** The names this session had before, newest first: its Laya verdicts were recorded under them. */
    aliases: z.array(z.string().min(1).max(128)).max(8),
    /** The topic call that named it (under `session`, or a former name), or null: never auto-named. */
    named: z
        .object({
            session: z.string().min(1).max(128),
            name: z.string().min(1).max(128),
            at: z.number(),
            model: z.string().max(80).nullable(),
            /** The head of the request the model saw (the operator's first prompt). */
            request: z.string().max(600).nullable(),
            request_chars: z.number().int().nonnegative(),
        })
        .nullable(),
    /** The live verdict on that naming, or null: not labelled yet. */
    label: z
        .object({
            verdict: topicVerdictSchema,
            better: z.string().max(TOPIC_BETTER_MAX).nullable().default(null),
            /** `card`: answered here. `rename`: inferred from a ✎ rename of the row. */
            via: z.enum(['card', 'rename']),
            at: z.number(),
        })
        .nullable(),
    /** What the rack shows for the session right now (a placeholder included), or null. */
    shown: z.string().max(128).nullable(),
});
export type TopicLabelsView = z.infer<typeof topicLabelsViewSchema>;

export const topicLabelRequestSchema = z.strictObject({
    session: z.string().regex(/^[A-Za-z0-9_-]+$/).max(128),
    verdict: topicVerdictSchema,
    /** Only with `wrong`: the name it should have been; on a vc-N row it becomes the label at once. */
    better: z.string().trim().min(1).max(TOPIC_BETTER_MAX).optional(),
    since: z.number().int().positive().optional(),
});
export type TopicLabelRequest = z.infer<typeof topicLabelRequestSchema>;

export const topicLabelResultSchema = topicLabelsViewSchema.extend({ relabeled: z.boolean() });
export type TopicLabelResult = z.infer<typeof topicLabelResultSchema>;
