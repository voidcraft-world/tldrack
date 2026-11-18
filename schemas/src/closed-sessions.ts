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

/** One row of the bridge's `GET /restorable` — a session that was running when the machine went
 *  down (a reboot, a crash), which the ↺ list can bring back. A reboot is never a close, so these
 *  are their own rows. Owner of what is recorded and when it is forgotten:
 *  `tldrack/session_records.py`; of what a record restores into: `tldrack/restore.py`. */
export const restorable_session_schema = z.object({
    session: z.string().min(1),
    /** The first-prompt name the rack showed for a `vc-N` session; absent when it had none. */
    label: z.string().optional(),
    cwd: z.string().nullable(),
    profile: z.string().nullable(),
    kind: z.string(),
    /** A restore resumes the conversation (`claude --resume`, `codex resume`) rather than starting fresh. */
    resumable: z.boolean(),
    age_seconds: z.number(),
    ok: z.boolean(),
    /** Why a restore would be refused (`cwd gone`, `conversation open in vc-3`); absent when `ok`. */
    reason: z.string().optional(),
    /** The launch mode it comes back in (a LEAD worker's `auto`), said so a restore is never a surprise. */
    permission_mode: z.string().optional(),
});
export type RestorableSession = z.infer<typeof restorable_session_schema>;

/** One row of `POST /restore`'s 200: per session, restored or the reason it was not. */
export const restore_outcome_schema = z.object({
    session: z.string().min(1),
    restored: z.boolean(),
    resumed: z.boolean().optional(),
    reason: z.string().optional(),
});
export type RestoreOutcome = z.infer<typeof restore_outcome_schema>;

export const restore_result_schema = z.object({
    ok: z.literal(true),
    results: z.array(restore_outcome_schema),
});
