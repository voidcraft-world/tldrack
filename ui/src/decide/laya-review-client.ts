/**
 * The REVIEW deck's side of the `laya` plugin's policy routes, and that of any host surface that operates Laya (`/ops`, `/review/*`,
 * `/lab`, `/restart`, and `/install` + `/start` through the same guard). The wire is `schemas/src/laya-review.ts`;
 * every 2xx and 409 body is `safeParse`d here, so nothing past this file reads an unchecked shape.
 *
 * Never throws: a call answers its data, or a `LayaFail` whose `kind` names the remedy — the deck draws one
 * down state per kind. No mock data in a dev build: a stub would hide "enable the plugin" / "install Laya" behind
 * sample cards, and both are states the operator has to see.
 */
import {
    layaLabRequestSchema,
    layaVerifierResultSchema,
    layaVerifierPublishSchema,
    layaLabResultSchema,
    layaOpsStateSchema,
    layaRestartResultSchema,
    reviewAnswerRequestSchema,
    reviewItemResultSchema,
    REVIEW_RECENT_MAX,
    REVIEW_RECENT_CURSOR,
    reviewQueueSchema,
    reviewRecentSchema,
    reviewResultSchema,
    reviewRetestRequestSchema,
    reviewRetestResultSchema,
    reviewUndoRequestSchema,
    type LayaLabResult,
    type LayaLabVerb,
    type LayaOpsState,
    type LayaRestartResult,
    type ReviewAnswerRequest,
    type ReviewItemResult,
    type ReviewQueue,
    type ReviewRecent,
    type ReviewRecentOrder,
    type ReviewResult,
    type ReviewRetestRequest,
    type ReviewRetestResult,
    type ReviewUndoRequest,
} from '@tldrack/schemas/laya-review.ts';

import { enable_plugin_hint, unmounted_plugin_hint } from '../laya-review/bridge-error.ts';
import { get_bridge_json } from '../laya-review/bridge-get.ts';
import { BRIDGE_PREDATES_MESSAGE, post_bridge_json } from '../laya-review/bridge-post.ts';
import { LAYA_PLUGIN, laya_base_url } from './laya-local-client.ts';
import type { ReviewSession } from '../laya-review/open.ts';
import {
    BRIDGE_DOWN_HINT,
    LOCAL_ACCESS_PROMPTING_HINT,
    classify_access_notice,
    local_access_state,
    local_network_permission,
} from '../engine/terminal/local-network-gate.ts';

/** Loopback answers in milliseconds; these only bound a hung bridge. */
const GET_TIMEOUT_MS = 5_000;
const POST_TIMEOUT_MS = 5_000;
/** `/lab` spawns a tmux session and `/restart` stops then starts Laya: both answer before the work finishes. */
const SLOW_POST_TIMEOUT_MS = 15_000;
/** The envelope's own `message` bound (`schemas/src/laya-review.ts`). */
const MESSAGE_CHARS = 160;

/** The GET twin of `BRIDGE_PREDATES_MESSAGE`: the plugin answered 404 for one of its own routes. */
export const LAYA_ROUTE_PREDATES = 'The running bridge predates this Laya route. Restart the bridge.';

/**
 * Why a call has no data — each kind is one remedy:
 * - `gate` — the local-access gate (`local-network-gate.ts`): off (CONNECT), blocked in Chrome's settings, or Chrome's prompt unanswered
 * - `down` — no bridge on 127.0.0.1: start it
 * - `plugin` — the bridge never mounted `plugins/laya`: enable it
 * - `restart` — the plugin predates the route: restart the bridge
 * - `refused` — the plugin answered no (409 `already_answered`, 503 `params_unavailable`, a 403 policy refusal, …)
 * - `invalid` — an answer outside the contract, or a request this page refused to send
 */
export type LayaFailKind = 'gate' | 'down' | 'plugin' | 'restart' | 'refused' | 'invalid';

export interface LayaFail<R = never> {
    ok: false;
    kind: LayaFailKind;
    /** HTTP status; 0 when no answer came (or the request was never sent). */
    status: number;
    /** The plugin's refusal `code` (`already_answered`, `busy`, …) when its body carried one. */
    code: string | null;
    error: string;
    hint?: string;
    /** A 409's own checked body: `already_answered` carries `answer`, `reference_changed` a fresh `card`. */
    body: R | null;
}

export type LayaCall<T, R = never> = { ok: true; data: T } | LayaFail<R>;

/** A failure as one line beside a button: the refusal, then its remedy when it named one. */
export function fail_text(f: Pick<LayaFail<unknown>, 'error' | 'hint'>): string {
    return f.hint ? `${f.error} — ${f.hint}` : f.error;
}

/** What this file needs of a schema — structural, because `schemas/` resolves its own copy of zod. */
interface Parser<T> {
    safeParse(value: unknown): { success: true; data: T } | { success: false };
}

// A refusal is the route's envelope without the success-only fields: `.partial` keeps every field it DOES send checked.
const reviewRefusalSchema = reviewResultSchema.partial({ answer: true, card: true, stats: true });
const retestRefusalSchema = reviewRetestResultSchema.partial({ retest: true, stats: true });
const labRefusalSchema = layaLabResultSchema.partial({ session: true });
const envelopeSchema = layaLabResultSchema.pick({ v: true, ok: true, message: true, code: true, error: true, hint: true });

export type ReviewRefusal = ReturnType<typeof reviewRefusalSchema.parse>;
export type RetestRefusal = ReturnType<typeof retestRefusalSchema.parse>;
export type LabRefusal = ReturnType<typeof labRefusalSchema.parse>;

interface FailExtra<R> {
    hint?: string;
    code?: string | null;
    body?: R | null;
}

function fail<R = never>(kind: LayaFailKind, status: number, error: string, extra: FailExtra<R> = {}): LayaFail<R> {
    return { ok: false, kind, status, code: extra.code ?? null, error, ...(extra.hint ? { hint: extra.hint } : {}), body: extra.body ?? null };
}

/**
 * No answer at all: the gate, whenever the gate's own reading says it stood in the way — shut, blocked by Chrome, or
 * open with Chrome's prompt still unanswered, where the request hangs and times out like a dead bridge
 * (`classify_access_notice` owns that case) — else a dead bridge.
 */
function unanswered(error: string): LayaFail {
    switch (classify_access_notice(local_access_state(), local_network_permission())) {
        case 'prompting':
            return fail('gate', 0, LOCAL_ACCESS_PROMPTING_HINT);
        case 'locked':
        case 'denied':
            return fail('gate', 0, error);
        case null:
            return fail('down', 0, error, { hint: BRIDGE_DOWN_HINT });
    }
}

function invalid(route: string, status: number): LayaFail {
    return fail('invalid', status, `${route}: invalid laya response`);
}

async function get_route<T>(route: string, query: string, schema: Parser<T>): Promise<LayaCall<T>> {
    const got = await get_bridge_json(`${laya_base_url()}/${route}${query}`, GET_TIMEOUT_MS);
    if (!got.ok) {
        if (got.status === 0) return unanswered(got.error);
        if (got.status === 404) {
            // The bridge HOST's 404 names a plugin it never mounted (`read_bridge_error` hints so); the plugin's own is a route it lacks.
            return got.hint === enable_plugin_hint(LAYA_PLUGIN)
                ? fail('plugin', 404, got.error, { hint: got.hint })
                : fail('restart', 404, LAYA_ROUTE_PREDATES);
        }
        // The plugin's refusal envelope names its `code` (`params_unavailable`, `bad_query`, `origin_refused`, …).
        const env = envelopeSchema.safeParse(got.body);
        return fail('refused', got.status, got.error, { hint: got.hint, code: env.success ? (env.data.code ?? null) : null });
    }
    const parsed = schema.safeParse(got.body);
    if (!parsed.success) return invalid(route, 200);
    const env = parsed.data as { ok?: boolean; message?: string; code?: string; hint?: string };
    return env.ok === false ? fail('refused', 200, env.message ?? 'refused', { hint: env.hint, code: env.code }) : { ok: true, data: parsed.data };
}

async function post_route<T, R = never>(route: string, body: unknown, timeout_ms: number, schema: Parser<T>, refusal: Parser<R> | null): Promise<LayaCall<T, R>> {
    const sent = await post_bridge_json(`${laya_base_url()}/${route}`, body, timeout_ms, MESSAGE_CHARS);
    if (sent.status === 0) return unanswered(sent.message);
    if (sent.status === 404) {
        const said = (sent.body as { error?: unknown } | null)?.error;
        const hint = typeof said === 'string' ? unmounted_plugin_hint(404, said) : undefined;
        return hint && typeof said === 'string' ? fail('plugin', 404, said, { hint }) : fail('restart', 404, BRIDGE_PREDATES_MESSAGE);
    }
    if (sent.status === 426) return fail('restart', 426, BRIDGE_PREDATES_MESSAGE);
    if (sent.status >= 200 && sent.status < 300) {
        const parsed = schema.safeParse(sent.body);
        if (!parsed.success) return invalid(route, sent.status);
        if (sent.ok) return { ok: true, data: parsed.data };
    }
    if (sent.status === 409 && refusal) {
        const parsed = refusal.safeParse(sent.body);
        if (!parsed.success) return invalid(route, 409);
        const env = parsed.data as { message: string; code?: string; hint?: string };
        return fail<R>('refused', 409, env.message, { code: env.code ?? null, hint: env.hint, body: parsed.data });
    }
    // Every other refusal (400, 403, 413, 415, 503, a 2xx saying ok:false): its envelope when it has one, else the status line.
    const env = envelopeSchema.safeParse(sent.body);
    return fail('refused', sent.status, sent.message, env.success ? { code: env.data.code ?? null, hint: env.data.hint } : {});
}

/** A request the route would refuse is never sent: the routes are strict, and so is this page. */
function unsent(route: string): Promise<LayaFail> {
    return Promise.resolve(fail('invalid', 0, `${route}: invalid request`));
}

// ── reads ────────────────────────────────────────────────────────────────────

export function load_laya_verifier() {
    return get_route('card', '', layaVerifierResultSchema);
}

export function publish_laya_verifier(digest: string) {
    return post_route('card/publish', { digest }, 185_000, layaVerifierPublishSchema, null);
}

export function post_laya_challenger(action: 'promote' | 'reject', surface: string, token: string) {
    return post_route('challenger', { action, surface, token }, 35_000, envelopeSchema, null);
}

export function load_laya_ops(): Promise<LayaCall<LayaOpsState>> {
    return get_route('ops', '', layaOpsStateSchema);
}

/**
 * `surface` null = the surface with the most `to_review`; `limit` 0 = the stats alone (a `REVIEW · N` readout);
 * `stratum` deals only that stratum (the deck's DEAL filter), null the plugin's round robin over all of them.
 */
export function load_review_queue(surface: string | null, limit: number, stratum: string | null = null): Promise<LayaCall<ReviewQueue>> {
    const query = new URLSearchParams({ limit: String(Math.max(0, Math.min(50, Math.trunc(limit)))) });
    if (surface !== null) query.set('surface', surface);
    if (stratum !== null) query.set('stratum', stratum);
    return get_route('review/queue', `?${query}`, reviewQueueSchema);
}

/** The RECENT list: `limit` live verdicts of `surface` — the newest, or with `order` `edge` the nearest a cut — and with
 *  `todo` only the unanswered ones, the page after `after` (the previous page's `summary.next`: `review_data.py`
 *  `recent_rows`). The defaults are never sent, so the plain list still loads from a bridge that predates `order`, `todo`
 *  and `after`, which refuses a name it does not know. */
export function load_review_recent(
    surface: string | null,
    limit: number,
    view: { order?: ReviewRecentOrder; todo?: boolean; session?: ReviewSession | null; after?: string | null } = {},
): Promise<LayaCall<ReviewRecent>> {
    const query = new URLSearchParams({ limit: String(Math.max(1, Math.min(REVIEW_RECENT_MAX, Math.trunc(limit)))) });
    if (surface !== null) query.set('surface', surface);
    if (view.order && view.order !== 'newest') query.set('order', view.order);
    if (view.todo) query.set('todo', '1');
    if (typeof view.after === 'string' && REVIEW_RECENT_CURSOR.test(view.after)) query.set('after', view.after);
    if (view.session) {
        // The name now, then the ones it had (`review.py` MAX_SUBJECTS: 9 in all).
        query.set('subject', [view.session.name, ...(view.session.aliases ?? [])].slice(0, 9).join(','));
        if (view.session.since !== null) query.set('since', String(view.session.since));
    }
    return get_route('review/recent', `?${query}`, reviewRecentSchema);
}

export function load_review_item(surface: string, key: string): Promise<LayaCall<ReviewItemResult>> {
    return get_route('review/item', `?${new URLSearchParams({ surface, key })}`, reviewItemResultSchema);
}

// ── writes ───────────────────────────────────────────────────────────────────

export function post_review_answer(request: ReviewAnswerRequest): Promise<LayaCall<ReviewResult, ReviewRefusal>> {
    const checked = reviewAnswerRequestSchema.safeParse(request);
    if (!checked.success) return unsent('review/answer');
    return post_route('review/answer', checked.data, POST_TIMEOUT_MS, reviewResultSchema, reviewRefusalSchema);
}

export function post_review_undo(request: ReviewUndoRequest): Promise<LayaCall<ReviewResult, ReviewRefusal>> {
    const checked = reviewUndoRequestSchema.safeParse(request);
    if (!checked.success) return unsent('review/undo');
    return post_route('review/undo', checked.data, POST_TIMEOUT_MS, reviewResultSchema, reviewRefusalSchema);
}

/** A blind retest's answer (`review.py` `retest`): 409 `retest_stale`, `already_retested` or `not_due` records nothing. */
export function post_review_retest(request: ReviewRetestRequest): Promise<LayaCall<ReviewRetestResult, RetestRefusal>> {
    const checked = reviewRetestRequestSchema.safeParse(request);
    if (!checked.success) return unsent('review/retest');
    return post_route('review/retest', checked.data, POST_TIMEOUT_MS, reviewRetestResultSchema, retestRefusalSchema);
}

export function post_laya_lab(verb: LayaLabVerb, surface: string): Promise<LayaCall<LayaLabResult, LabRefusal>> {
    const checked = layaLabRequestSchema.safeParse({ verb, surface });
    if (!checked.success) return unsent('lab');
    return post_route('lab', checked.data, SLOW_POST_TIMEOUT_MS, layaLabResultSchema, labRefusalSchema);
}

export function post_laya_restart(): Promise<LayaCall<LayaRestartResult>> {
    return post_route('restart', {}, SLOW_POST_TIMEOUT_MS, layaRestartResultSchema, null);
}

/**
 * INSTALL and START: the plugin's existing routes, now behind the policy guard. Their success body is the plugin's
 * status, which `parse_laya_status` (laya-local-client.ts) owns reading. So here only "it accepted" counts, and the
 * caller re-reads `/ops` straight after.
 */
export function post_laya_simple(route: 'install' | 'start'): Promise<LayaCall<{ message: string }>> {
    const message = route === 'install' ? 'Installing Laya' : 'Starting Laya';
    const accepted: Parser<{ message: string }> = { safeParse: () => ({ success: true, data: { message } }) };
    return post_route(route, {}, POST_TIMEOUT_MS, accepted, null);
}
