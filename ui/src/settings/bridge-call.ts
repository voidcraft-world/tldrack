/**
 * One call from the settings page to its bridge, read through a schema. Every client in this
 * folder goes through here, so none of them casts a reply: a body is `data` only after its
 * schema accepted it.
 *
 * The request itself (fetch → status → error body → unreachable, and the local-access gate) is
 * not restated: owned by `laya-review/bridge-get.ts` and `laya-review/bridge-post.ts` — see the
 * notes there. This file adds the two things a settings client needs on top: the schema, and a
 * `kind` a component can branch on without reading a status code.
 *
 *   unreachable  no answer came (the bridge is down, or the gate is shut). The page's banner.
 *   missing      404: the running bridge has no such route (an older bridge), or the plugin
 *                behind it is not mounted.
 *   refused      the bridge answered and said no; `error` is its own sentence.
 *   invalid      a 200 whose body the schema did not accept. Never shown as data.
 *
 * Never throws.
 */
import type { z } from 'zod';

import { bridge_origin } from '../config/bridge-origin.ts';
import { get_bridge_json } from '../laya-review/bridge-get.ts';
import { post_bridge_json } from '../laya-review/bridge-post.ts';

export type BridgeFailureKind = 'unreachable' | 'missing' | 'refused' | 'invalid';

export interface BridgeFailure {
    ok: false;
    kind: BridgeFailureKind;
    /** The HTTP status; 0 when no answer came. */
    status: number;
    /** One sentence for the page. */
    error: string;
    hint?: string;
    /** The failure's parsed JSON body, unvalidated, for a client whose refusal carries more than `error`. */
    body: unknown;
}

export type BridgeResult<T> = { ok: true; data: T } | BridgeFailure;

const READ_TIMEOUT_MS = 6_000;
const WRITE_TIMEOUT_MS = 10_000;
const MESSAGE_CHARS = 280;

export function failure_kind(status: number): BridgeFailureKind {
    if (status === 0) return 'unreachable';
    return status === 404 ? 'missing' : 'refused';
}

/**
 * What one result says about the bridge as a whole: the failure to put in the page's banner, or
 * null. Only an unanswered call is "down"; a refusal came from a bridge that is up.
 */
export function down_after<T>(result: BridgeResult<T>): BridgeFailure | null {
    return !result.ok && result.kind === 'unreachable' ? result : null;
}

/** `body` through `schema`. `what` names the reply in the sentence a failed read shows ("a feature list"). */
export function read_checked<T>(schema: z.ZodType<T>, body: unknown, what: string): BridgeResult<T> {
    const parsed = schema.safeParse(body);
    if (parsed.success) return { ok: true, data: parsed.data };
    return { ok: false, kind: 'invalid', status: 200, error: `The bridge returned ${what} this page cannot read`, body };
}

/** GET `path` on the page's bridge. */
export async function bridge_read<T>(
    path: string,
    schema: z.ZodType<T>,
    what: string,
    timeout_ms: number = READ_TIMEOUT_MS,
): Promise<BridgeResult<T>> {
    const reply = await get_bridge_json(`${bridge_origin()}${path}`, timeout_ms);
    if (reply.ok) return read_checked(schema, reply.body, what);
    return {
        ok: false,
        kind: failure_kind(reply.status),
        status: reply.status,
        error: reply.error,
        ...(reply.hint ? { hint: reply.hint } : {}),
        body: reply.body,
    };
}

/** POST `body` to `path` on the page's bridge. */
export async function bridge_write<T>(
    path: string,
    body: unknown,
    schema: z.ZodType<T>,
    what: string,
    timeout_ms: number = WRITE_TIMEOUT_MS,
): Promise<BridgeResult<T>> {
    const reply = await post_bridge_json(`${bridge_origin()}${path}`, body, timeout_ms, MESSAGE_CHARS);
    if (reply.ok) return read_checked(schema, reply.body, what);
    return { ok: false, kind: failure_kind(reply.status), status: reply.status, error: reply.message, body: reply.body };
}
