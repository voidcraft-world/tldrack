/**
 * The page's side of the `laya` bridge plugin (`plugins/laya/`): Laya decisions on the operator's
 * own machine.
 *
 * Laya is a ~420M encoder that answers typed questions with probabilities, never text. A decision
 * is sub-second, so a caller asks it INLINE with a budget of a few seconds and falls back to its
 * own rules on anything but a clean answer. `decide` therefore never throws: it answers a result
 * or the reason there is none, and "no Laya here" is the normal case.
 *
 * Every loopback call goes through `local_fetch`, so nothing here reaches the machine before the
 * user has connected local tools (`engine/terminal/local-network-gate.ts` owns why). The contract
 * and the answer reader are shared with the server: `schemas/src/decide.ts`.
 *
 * A caller may TAG a decision for the plugin's ledger and later report what the user did with it
 * (`laya_react`). The plugin is the ledger's only writer: the ledger is what a later fine-tune
 * learns from.
 */
import {
    LAYA_DECISION_ID,
    LAYA_LEDGER_RACK_SURFACES,
    decideRequestSchema,
    is_laya_local_model_id,
    layaLedgerTagSchema,
    layaReactRequestSchema,
    read_systemone_response,
    type DecideRequest,
    type DecideResult,
    type LayaLedgerTag,
    type LayaReactionAction,
} from '@tldrack/schemas/decide.ts';

import { bridge_origin } from '../config/bridge-origin.ts';
import { local_fetch } from '../engine/terminal/local-network-gate.ts';

export const LAYA_PLUGIN = 'laya';

/** `plugins/laya/laya_bridge/runtime.py` `STATES`. */
export const LAYA_STATES = ['not_installed', 'installing', 'stopped', 'starting', 'ready', 'failed'] as const;
export type LayaState = (typeof LAYA_STATES)[number];

export type FetchImpl = (input: string, init?: RequestInit) => Promise<Response>;

/** The Laya plugin's routes on the page's bridge, or on this machine's bridge at `port`. */
export function laya_base_url(port?: number): string {
    const base = port === undefined ? bridge_origin() : `http://127.0.0.1:${port}`;
    return `${base}/plugin/${LAYA_PLUGIN}`;
}

export interface LayaStatus {
    state: LayaState;
    /** Built-ins in use plus the operator's registered fine-tunes (the Laya plugin's `models.json`). */
    models: string[];
    /** Where a ready Laya computes (`mps`, `cpu`). */
    device: string | null;
    error: string | null;
    idle_stop_seconds: number | null;
    /** `{served name: "laya-local/<name>@<version>"}` while ready — the ids relayed verdicts carry. */
    model_ids: Record<string, string>;
}

function parse_model_ids(raw: unknown): Record<string, string> {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return {};
    return Object.fromEntries(
        Object.entries(raw as Record<string, unknown>).filter((entry): entry is [string, string] => is_laya_local_model_id(entry[1])),
    );
}

/** A `/status` body → the fields the page reads. Tolerant: an older plugin reads as "unknown", not a crash. */
export function parse_laya_status(payload: unknown): LayaStatus | null {
    if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return null;
    const body = payload as Record<string, unknown>;
    const state = LAYA_STATES.find((known) => known === body.state);
    if (!state) return null;
    return {
        state,
        models: Array.isArray(body.models) ? body.models.filter((m): m is string => typeof m === 'string' && m.length > 0) : [],
        device: typeof body.device === 'string' ? body.device : null,
        error: typeof body.error === 'string' && body.error ? body.error : null,
        idle_stop_seconds: typeof body.idle_stop_seconds === 'number' ? body.idle_stop_seconds : null,
        model_ids: parse_model_ids(body.model_ids),
    };
}

/** One loopback call within `timeout_ms`: its status and body text, or null on any failure (no bridge, gate shut, timeout). */
export async function call_local_text(fetch_impl: FetchImpl, url: string, init: RequestInit, timeout_ms: number): Promise<{ status: number; text: string } | null> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    try {
        const response = await fetch_impl(url, { ...init, signal: controller.signal });
        return { status: response.status, text: await response.text() };
    } catch {
        return null;
    } finally {
        clearTimeout(timer);
    }
}

async function call_json(fetch_impl: FetchImpl, url: string, init: RequestInit, timeout_ms: number): Promise<{ status: number; body: unknown } | null> {
    const reply = await call_local_text(fetch_impl, url, init, timeout_ms);
    if (reply === null) return null;
    let body: unknown = null;
    try {
        body = JSON.parse(reply.text);
    } catch {
        body = null;
    }
    return { status: reply.status, body };
}

/** Null on ANY failure — no bridge, plugin not enabled, gate shut, timeout. */
export async function fetch_laya_status(fetch_impl: FetchImpl = local_fetch, timeout_ms = 2_500): Promise<LayaStatus | null> {
    const reply = await call_json(fetch_impl, `${laya_base_url()}/status`, { method: 'GET' }, timeout_ms);
    return reply && reply.status === 200 ? parse_laya_status(reply.body) : null;
}

export type LayaAction = 'start' | 'stop' | 'install';

/** START, STOP and INSTALL. Answers the plugin's status after the action, or null. */
export async function laya_action(action: LayaAction, fetch_impl: FetchImpl = local_fetch): Promise<LayaStatus | null> {
    const reply = await call_json(
        fetch_impl,
        `${laya_base_url()}/${action}`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
        5_000,
    );
    return reply && (reply.status === 200 || reply.status === 202) ? parse_laya_status(reply.body) : null;
}

export type LayaDecideOutcome =
    /** `decision_id` names the plugin's ledger row for this answer; null when the ledger is off or the plugin predates it. */
    | { ok: true; result: DecideResult; decision_id: string | null }
    /**
     * - `unreachable` — no bridge, no plugin, gate shut, or the budget ran out
     * - `warming` / `not_installed` — the plugin answered but Laya cannot yet; it was asked to start
     * - `refused` — this request (or its ledger tag) was rejected (a bug in the caller's question)
     * - `invalid` — Laya answered in a shape the shared reader does not trust
     */
    | { ok: false; reason: 'unreachable' | 'warming' | 'not_installed' | 'refused' | 'invalid'; detail?: string };

export interface LayaDecideOptions {
    timeout_ms?: number;
    model?: string;
    fetch_impl?: FetchImpl;
    /** Which surface asked, for the plugin's ledger. Untagged decisions are recorded as `untagged`. */
    ledger?: LayaLedgerTag;
}

/** A 200 body's `decision_id`, kept only in the ledger's own shape. */
function read_decision_id(text: string): string | null {
    let body: unknown;
    try {
        body = JSON.parse(text);
    } catch {
        return null;
    }
    const id = typeof body === 'object' && body !== null ? (body as Record<string, unknown>).decision_id : undefined;
    return typeof id === 'string' && LAYA_DECISION_ID.test(id) ? id : null;
}

/** One decision. Never throws; anything but a trustworthy answer is a reason to use the rules. */
export async function laya_decide(request: DecideRequest, options: LayaDecideOptions = {}): Promise<LayaDecideOutcome> {
    const checked = decideRequestSchema.safeParse(request);
    if (!checked.success) return { ok: false, reason: 'refused', detail: checked.error.issues[0]?.message };
    const tag = options.ledger === undefined ? null : layaLedgerTagSchema.safeParse(options.ledger);
    if (tag && !tag.success) {
        const issue = tag.error.issues[0];
        // `ledger.<field>`, the plugin's own detail for the same refusal (`ledger.py` `clean_tag`). An
        // unknown key's issue has an empty path and names the key in `keys`; the plugin names the first.
        const path = issue?.code === 'unrecognized_keys' ? issue.keys.slice(0, 1) : (issue?.path ?? []);
        const field = ['ledger', ...path.map(String)].join('.');
        return { ok: false, reason: 'refused', detail: `${field}: ${issue?.message ?? 'invalid'}` };
    }
    // A page always sends Origin, so it is never the native caller a rack surface needs: the plugin
    // would 400 it. Refused here too, so a misrouted tag never costs the decision a round trip.
    if (tag && (LAYA_LEDGER_RACK_SURFACES as readonly string[]).includes(tag.data.surface)) {
        return { ok: false, reason: 'refused', detail: 'ledger.surface: only the agent rack may tag this surface' };
    }
    const body = JSON.stringify({
        ...checked.data,
        ...(options.model ? { model: options.model } : {}),
        ...(tag ? { ledger: tag.data } : {}),
    });

    const fetch_impl = options.fetch_impl ?? local_fetch;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout_ms ?? 1_500);
    try {
        const response = await fetch_impl(`${laya_base_url()}/decide`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            signal: controller.signal,
        });
        const text = await response.text();
        if (response.status === 200) {
            const read = read_systemone_response(text, checked.data, options.model ?? 'laya-local');
            return read.ok
                ? { ok: true, result: read.result, decision_id: read_decision_id(text) }
                : { ok: false, reason: 'invalid', detail: read.detail };
        }
        if (response.status === 503) {
            const reason = text.includes('"not_installed"') ? 'not_installed' : 'warming';
            return { ok: false, reason };
        }
        if (response.status === 404) return { ok: false, reason: 'unreachable', detail: 'the laya plugin is not enabled' };
        return { ok: false, reason: 'refused', detail: `HTTP ${response.status}` };
    } catch {
        return { ok: false, reason: 'unreachable' };
    } finally {
        clearTimeout(timer);
    }
}

export type LayaReact = (decision_id: string, action: LayaReactionAction) => Promise<boolean>;

/**
 * Report what the user did with a decision. True only when the plugin recorded it (a repeat it already
 * holds counts); false on anything else (an unknown decision is a 409, an older plugin a 404), never a throw.
 */
export async function laya_react(decision_id: string, action: LayaReactionAction, fetch_impl: FetchImpl = local_fetch): Promise<boolean> {
    const checked = layaReactRequestSchema.safeParse({ decision_id, action });
    if (!checked.success) return false;
    const reply = await call_json(
        fetch_impl,
        `${laya_base_url()}/react`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(checked.data) },
        1_500,
    );
    return reply?.status === 200;
}
