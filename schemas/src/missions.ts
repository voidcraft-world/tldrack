import { z } from 'zod';

/** The bridge's `GET /missions` wire contract. Owner of what a mission is: `tldrack/missions.py`. */
export const mission_note_schema = z.object({
    seq: z.number().int(),
    at: z.number(),
    author: z.string(),
    text: z.string(),
});

export const mission_member_schema = z.object({
    session: z.string(),
    role: z.string(),
    /** Where its recent edits landed, worked out by the bridge from its hooks. */
    touching: z.array(z.string()),
    /** Can its hook relay carry an answer? null until its first hook on this bridge run. */
    hears: z.boolean().nullable().default(null),
});

export const mission_schema = z.object({
    id: z.string().min(1),
    title: z.string().min(1),
    brief: z.string(),
    /** `operator`, a session, or '' from a shell. A label, not a credential. */
    created_by: z.string().default(''),
    created_at: z.number(),
    closed_at: z.number().nullable(),
    members: z.array(mission_member_schema),
    /** The newest few; `note_count` counts every note ever written. */
    notes: z.array(mission_note_schema),
    note_count: z.number().int().nonnegative(),
});
export type Mission = z.infer<typeof mission_schema>;
export type MissionMember = z.infer<typeof mission_member_schema>;
export type MissionNote = z.infer<typeof mission_note_schema>;
