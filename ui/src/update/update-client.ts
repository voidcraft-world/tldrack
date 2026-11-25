/**
 * The page's side of the bridge's self-update: `GET /update` (where the checkout stands against
 * its remote, and what a run is doing) and `POST /update` (`check`, `apply`). What a run does,
 * and when a pull is refused: owned by the bridge's update module — this file only reads its view.
 *
 * BOTH THE RACK AND THE SETTINGS PAGE READ THIS, through one poller (`update-poller.ts`).
 *
 * A MISSING KEY IS ITS DEFAULT, A WRONG TYPE IS NO VIEW. The view grew key by key and will grow
 * again, so every key but `supported` may be absent or null and reads as the quiet answer
 * (nothing behind, nothing running, nothing to say). A key of the wrong type is a body this page
 * does not understand, and is never drawn as data. Two fields are open on purpose:
 *
 *   phase       any short word. One this page has no words for still means "a run is going"
 *               (`update-status.ts` `phase_words`), which is the half that matters.
 *   freshness   `page.source` and `code`: a word that is not `current` or `stale` is `unknown`.
 *
 * NO SUPPORT IS AN ANSWER, NOT A FAILURE. A bridge that predates the route says 404, and one
 * that cannot update itself says 503. Both come back as `unsupported`, so the rack draws nothing
 * and the settings card says so once.
 *
 * The request itself: owned by `settings/bridge-call.ts` — see the note there. It serves the
 * rack too, because it calls `config/bridge-origin.ts`, which every mount of the rack sets.
 */
import { z } from 'zod';

import { bridge_read, bridge_write, type BridgeResult } from '../settings/bridge-call.ts';

export const UPDATE_ACTIONS = ['check', 'apply'] as const;
export type UpdateAction = (typeof UPDATE_ACTIONS)[number];

export type Freshness = 'current' | 'stale' | 'unknown';

/** The most incoming commits the card lists; the bridge sends no more either. */
export const MAX_INCOMING = 8;

const sentence = z
    .string()
    .max(2000)
    .nullish()
    .transform((text) => text?.trim() || null);
const name = z
    .string()
    .max(300)
    .nullish()
    .transform((text) => text || null);
const count = z
    .number()
    .int()
    .min(0)
    .max(1_000_000)
    .nullish()
    .transform((value) => value ?? 0);
const freshness = z
    .string()
    .max(40)
    .nullish()
    .transform((word): Freshness => (word === 'current' || word === 'stale' ? word : 'unknown'));
const flag = (fallback: boolean) =>
    z
        .boolean()
        .nullish()
        .transform((value) => value ?? fallback);

const commit_schema = z.object({ sha: z.string().min(1).max(64), subject: z.string().max(500) });
export type IncomingCommit = z.infer<typeof commit_schema>;

const page_schema = z
    .object({ id: name, source: freshness })
    .nullish()
    .transform((page) => page ?? { id: null, source: 'unknown' as Freshness });

export const update_schema = z.object({
    /** False: this bridge does not run from a git checkout it can pull. `reason` says why. */
    supported: z.boolean(),
    reason: sentence,
    branch: name,
    upstream: name,
    head: name,
    target: name,
    behind: count,
    ahead: count,
    dirty: count,
    /** Newest first. A row this page cannot read is dropped: the count (`behind`) is the fact. */
    incoming: z
        .array(z.unknown())
        .max(200)
        .nullish()
        .transform((rows): IncomingCommit[] =>
            (rows ?? [])
                .map((row) => commit_schema.safeParse(row))
                .flatMap((parsed) => (parsed.success ? [parsed.data] : []))
                .slice(0, MAX_INCOMING),
        ),
    /** Why a pull cannot run, in the bridge's words. */
    blocked: sentence,
    /** `id` changes whenever the served build changes. */
    page: page_schema,
    /** Is the running bridge the code on disk. */
    code: freshness,
    /** An UPDATE press would do something right now. */
    available: flag(false),
    phase: z
        .string()
        .max(40)
        .nullish()
        .transform((word) => word || 'idle'),
    /** An update is held, and for what. */
    waiting: sentence,
    /** The last run's failure. */
    error: sentence,
    /** The last check could not reach the remote. */
    check_error: sentence,
    /** Unix seconds; null when the remote was never asked. */
    checked_at: z
        .number()
        .nullish()
        .transform((value) => value ?? null),
    /** The `auto_update` switch. */
    auto: flag(false),
    /** The `update_check` switch: the timed check. */
    checks: flag(true),
    /** Changes when the bridge restarts. */
    boot_id: name,
});
export type UpdateView = z.infer<typeof update_schema>;

/** A `/update` body → the view, or null when it is not one. */
export function parse_update(body: unknown): UpdateView | null {
    const parsed = update_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

/**
 * One answer about updates, in the four shapes a caller branches on.
 *
 *   view         the bridge's view.
 *   unsupported  404 or 503: this bridge has no self-update.
 *   silent       no answer came. Expected for up to a minute and a half while a run restarts
 *                the bridge (`update-status.ts` owns how long that is believed).
 *   refused      the bridge answered and said no (a 409 names why), or sent a body that is not a view.
 */
export type UpdateReply =
    | { kind: 'view'; view: UpdateView }
    | { kind: 'unsupported'; status: number; error: string }
    | { kind: 'silent'; error: string }
    | { kind: 'refused'; status: number; error: string };

export function update_reply(result: BridgeResult<UpdateView>): UpdateReply {
    if (result.ok) return { kind: 'view', view: result.data };
    if (result.kind === 'unreachable') return { kind: 'silent', error: result.error };
    if (result.status === 404 || result.status === 503) return { kind: 'unsupported', status: result.status, error: result.error };
    return { kind: 'refused', status: result.status, error: result.error };
}

const WHAT = 'an update status';

export async function read_update(): Promise<UpdateReply> {
    return update_reply(await bridge_read('/update', update_schema, WHAT));
}

/** Ask for a check or a run. The bridge answers at once (202) with the view; the work runs behind it. */
export async function post_update(action: UpdateAction): Promise<UpdateReply> {
    return update_reply(await bridge_write('/update', { action }, update_schema, WHAT));
}
