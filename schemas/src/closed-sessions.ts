import { z } from 'zod';

/** One row of the bridge's `GET /closed` — a recent close the rack's ↺ CLOSED list can reopen.
 *  Owner of what counts as a close and what is kept: `tldrack/closed_sessions.py`. */
export const closed_session_schema = z.object({
    /** The close's own id, the only thing `POST /reopen` takes. Never the conversation id. */
    id: z.string().regex(/^[0-9a-f]{12}$/),
    session: z.string().min(1),
    /** The first-prompt name the rack showed for a `vc-N` session, else null. */
    label: z.string().nullable().default(null),
    cwd: z.string().nullable(),
    profile: z.string().nullable(),
    kind: z.string(),
    /** `killed`: a ✕ (`POST /kill-session`). `gone`: ended outside the bridge. */
    how: z.enum(['killed', 'gone']),
    closed_at: z.number(),
    /** A reopen resumes the conversation (`claude --resume`) rather than starting fresh. */
    resumable: z.boolean(),
    ok: z.boolean(),
    /** Why a reopen would be refused (`unknown profile`, `cli missing`); absent when `ok`. */
    reason: z.string().optional(),
    /** It closed carrying the operator's star, and a reopen hands the star back
     *  (`tldrack/stars.py`). Absent otherwise. */
    starred: z.boolean().optional(),
});
export type ClosedSession = z.infer<typeof closed_session_schema>;

/** `POST /reopen`'s 200. */
export const reopen_result_schema = z.object({
    ok: z.literal(true),
    session: z.string().min(1),
    profile: z.string().nullable(),
    resumed: z.boolean(),
});
export type ReopenResult = z.infer<typeof reopen_result_schema>;
