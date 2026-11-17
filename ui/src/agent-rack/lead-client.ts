/**
 * The rack's side of a LEAD: the launch, the run state it polls, and the operator's decision verbs.
 * What a lead, a run and a decision are, and every rule about them: owned by
 * `bridge/tldrack/lead/__init__.py` — see the note there. The bridge half is
 * `bridge/tldrack/lead/` and `inbox.py`; the wire is `schemas/src/lead.ts`.
 *
 * A poll, not a snapshot key, like missions: `GET /lead/state` on an older bridge answers 404, which
 * grows nothing: no LEAD on the fork and no card. A snapshot row only says WHICH run its
 * session leads (`lead`), so the card can find it.
 *
 * Free of the DOM, like `missions-client.ts`: every rack payload owner loads it, a host surface
 * that lists a run's decisions borrows its verbs, and it runs under bare `node --test`.
 */
import {
    DECISION_SECRET_HEADER,
    decision_answer_request_schema,
    decision_answer_response_schema,
    decision_defer_request_schema,
    decision_dismiss_request_schema,
    decision_secret_response_schema,
    lead_launch_request_schema,
    lead_launch_response_schema,
    lead_models_schema,
    lead_run_schema,
    type Decision,
    type LeadModels,
    type LeadRun,
    type LeadState,
    type SessionLead,
} from '@tldrack/schemas/lead.ts';
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { create_plugin_poller, fetch_plugin_state, type PluginPoller } from './plugin-poller.ts';
import type { LaunchNotice } from './spawn-client.ts';
import type { BaseUrl, FetchImpl, IslandFocus, PollerIoDeps } from './worktree-client.ts';

export type { Decision, LeadModels, LeadRun, LeadState, SessionLead };

export const LEAD_POLL_MS = 4000;
/** The bridge answers 201 once tmux holds the lead's session; the agent boots after. */
const LAUNCH_TIMEOUT_MS = 15_000;
const VERB_TIMEOUT_MS = 5000;
const SECRET_TIMEOUT_MS = 2500;
/** 404 has one meaning on this bridge: it predates the route. */
const RESTART_BRIDGE = 'Restart the bridge to use LEAD';

// ── state ───────────────────────────────────────────────────────────────────

/** `/lead/state`'s body, or null when it is not one. A bad run is dropped alone. */
export function parse_lead_state(body: unknown): LeadState | null {
    const runs = (body as { runs?: unknown } | null)?.runs;
    if (!Array.isArray(runs)) return null;
    const models = lead_models_schema.safeParse((body as { models?: unknown }).models);
    return {
        runs: runs.slice(0, 32).flatMap((run) => {
            const parsed = lead_run_schema.safeParse(run);
            return parsed.success ? [parsed.data] : [];
        }),
        ...(models.success ? { models: models.data } : {}),
    };
}

export function lead_signature(state: LeadState | null): string {
    return state ? JSON.stringify(state) : '';
}

export function fetch_lead_state(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<LeadState | null> {
    return fetch_plugin_state(base_url, '/lead/state', parse_lead_state, fetch_impl);
}

export interface LeadPollerDeps extends PollerIoDeps {
    base_url: BaseUrl;
    on_change: () => void;
    wanted: () => boolean;
    poll_interval_ms?: number;
}

export type LeadPoller = PluginPoller<LeadState>;

export function create_lead_poller(deps: LeadPollerDeps): LeadPoller {
    return create_plugin_poller({
        ...deps,
        fetch: (do_fetch) => fetch_lead_state(deps.base_url, do_fetch),
        signature: lead_signature,
        poll_interval_ms: deps.poll_interval_ms ?? LEAD_POLL_MS,
    });
}

// ── reads, shared by the rack's card and any host surface ───────────────────

/** The run a session leads (its snapshot row's `lead`), or null. */
export function led_run(state: LeadState | undefined | null, lead: SessionLead | undefined | null): LeadRun | null {
    if (!state || !lead?.run) return null;
    return state.runs.find((run) => run.id === lead.run) ?? null;
}

/** The decisions waiting on the operator now: open, and not deferred. */
export function open_decisions(run: LeadRun): Decision[] {
    return run.decisions.filter((decision) => decision.state === 'open' && !decision.deferred);
}

export function deferred_count(run: LeadRun): number {
    return run.decisions.filter((decision) => decision.state === 'open' && decision.deferred).length;
}

/** `BLOCKS S2 S4` (amber on every surface) or `ASSUMING <what the lead is doing meanwhile>`. */
export interface DecisionStance {
    blocking: boolean;
    text: string;
}

export function decision_stance(decision: Decision): DecisionStance {
    if (decision.blocking) {
        return { blocking: true, text: ['BLOCKS', ...decision.steps].join(' ') };
    }
    return { blocking: false, text: decision.assumption ? `ASSUMING ${decision.assumption}` : 'ASSUMING' };
}

/** Which of the operator's answers a decision offers. One rule, so every surface that offers them agrees. */
export interface DecisionChoices {
    /** KEEP: the lead's assumption holds. Only a non-blocking row has one to keep. */
    keep: boolean;
    /** One button per option the lead offered. */
    options: readonly string[];
    /** ANSWER in words: offered when the lead gave no options. */
    text: boolean;
}

export function decision_choices(decision: Decision): DecisionChoices {
    const options = decision.options ?? [];
    return { keep: !decision.blocking && decision.assumption !== null, options, text: options.length === 0 };
}

/** Steps closed out of the steps that will ever close; an abandoned step never does, so it is not owed. */
export interface RunProgress {
    closed: number;
    total: number;
    abandoned: number;
}

export function run_progress(run: LeadRun): RunProgress {
    const abandoned = run.steps.filter((step) => step.state === 'abandoned').length;
    return {
        closed: run.steps.filter((step) => step.state === 'closed').length,
        total: run.steps.length - abandoned,
        abandoned,
    };
}

// ── DEFER ───────────────────────────────────────────────────────────────────

export interface DeferPreset {
    key: '1h' | 'evening' | 'morning';
    label: string;
    title: string;
    /** Epoch seconds, as `POST /inbox/defer` takes it. */
    until: number;
}

/** Before this hour "tomorrow morning" is still today's: the operator has not slept yet. */
const NIGHT_ENDS_HOUR = 4;

function hhmm(at: Date): string {
    return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
}

/** `now`'s calendar day plus `days`, at `hour`:00 local. `setDate` and `setHours` keep a DST day right. */
function local_at(now: Date, days: number, hour: number): Date {
    const at = new Date(now.getTime());
    at.setDate(at.getDate() + days);
    at.setHours(hour, 0, 0, 0);
    return at;
}

/**
 * DEFER's three presets, in local time: an hour from now; 18:00 tonight, or tomorrow's 18:00 once
 * tonight's has passed; and 09:00 tomorrow, which before 04:00 means this morning's 09:00.
 */
export function defer_presets(now: Date): DeferPreset[] {
    const epoch = (at: Date): number => Math.floor(at.getTime() / 1000);
    const hour = new Date(now.getTime() + 3600_000);
    const tonight = local_at(now, 0, 18);
    const evening_today = tonight.getTime() > now.getTime();
    const evening = evening_today ? tonight : local_at(now, 1, 18);
    const morning_today = now.getHours() < NIGHT_ENDS_HOUR;
    const morning = local_at(now, morning_today ? 0 : 1, 9);
    return [
        { key: '1h', label: '1H', title: `Hide until ${hhmm(hour)}`, until: epoch(hour) },
        {
            key: 'evening',
            label: evening_today ? 'TONIGHT' : '18:00 TMRW',
            title: `Hide until 18:00 ${evening_today ? 'today' : 'tomorrow'}`,
            until: epoch(evening),
        },
        {
            key: 'morning',
            label: 'TOMORROW',
            title: `Hide until 09:00 ${morning_today ? 'this morning' : 'tomorrow'}`,
            until: epoch(morning),
        },
    ];
}

// ── the operator's verbs ─────────────────────────────────────────────────────

/** One verb. It crosses the rack transport as a JSON string (`RackTransport.send` says why). */
export type DecisionRequest =
    | { verb: 'answer'; key: string; kind: 'keep' | 'option' | 'text'; answer?: string }
    | { verb: 'defer'; key: string; until: number }
    | { verb: 'dismiss'; key: string; reason: string };

/** Every hop re-validates what crossed a postMessage: null for anything that is not a request. */
export function parse_decision_request(value: unknown): DecisionRequest | null {
    if (typeof value !== 'string') return null;
    let raw: unknown;
    try {
        raw = JSON.parse(value);
    } catch {
        return null;
    }
    const verb = (raw as { verb?: unknown } | null)?.verb;
    if (verb === 'answer') {
        const parsed = decision_answer_request_schema.safeParse(raw);
        if (!parsed.success) return null;
        const { key, kind, answer } = parsed.data;
        // KEEP carries nothing; an option or a text answer carries the words.
        if (kind === 'keep') return { verb, key, kind };
        const words = answer?.trim();
        return words ? { verb, key, kind, answer: words } : null;
    }
    if (verb === 'defer') {
        const parsed = decision_defer_request_schema.safeParse(raw);
        return parsed.success && Number.isInteger(parsed.data.until) ? { verb, ...parsed.data } : null;
    }
    if (verb === 'dismiss') {
        const parsed = decision_dismiss_request_schema.safeParse(raw);
        const reason = parsed.success ? parsed.data.reason.trim() : '';
        return parsed.success && reason ? { verb, key: parsed.data.key, reason } : null;
    }
    return null;
}

/** The route and body one verb posts. */
export function decision_route(request: DecisionRequest): { path: string; body: Record<string, unknown> } {
    const { verb, ...body } = request;
    return { path: `/inbox/${verb}`, body };
}

// ── the per-launch secret ───────────────────────────────────────────────────
//
// Every verb carries `X-VC-Decision-Secret`. The bridge mints it at start and hands it only to a
// browser fetch (an allowed `Origin` plus `Sec-Fetch-Site`), so it is fetched once per document and
// held in memory, never stored. A restarted bridge holds a new one: a 403 refetches it once and
// retries once. Why the secret is friction and not a wall: owned by `bridge/tldrack/decision_secret.py`.

export type SecretResult = { ok: true; secret: string } | { ok: false; error: string };

export interface DecisionSecretCache {
    get: () => Promise<SecretResult>;
    invalidate: () => void;
}

/** A failure is not cached: the next press asks again, so a bridge restarted meanwhile just works. */
export function create_decision_secret_cache(fetch_secret: () => Promise<SecretResult>): DecisionSecretCache {
    let pending: Promise<SecretResult> | null = null;
    return {
        get: () => {
            if (!pending) {
                const attempt = fetch_secret().then((result) => {
                    if (!result.ok && pending === attempt) pending = null;
                    return result;
                });
                pending = attempt;
            }
            return pending;
        },
        invalidate: () => {
            pending = null;
        },
    };
}

export async function fetch_decision_secret(
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<SecretResult> {
    try {
        const response = await fetch_impl(base_url('/decision-secret'), { signal: AbortSignal.timeout(SECRET_TIMEOUT_MS) });
        if (response.status === 404) return { ok: false, error: RESTART_BRIDGE };
        if (response.status === 403) return { ok: false, error: 'the bridge would not hand this page the decision secret' };
        if (!response.ok) return { ok: false, error: `decision secret: bridge responded ${response.status}` };
        const parsed = decision_secret_response_schema.safeParse(await response.json());
        return parsed.success ? { ok: true, secret: parsed.data.secret } : { ok: false, error: 'decision secret: malformed answer' };
    } catch {
        return { ok: false, error: 'Bridge not reachable' };
    }
}

/** One cache per bridge per document — module state IS "per document". */
const secret_caches = new Map<string, DecisionSecretCache>();

export function decision_secret_cache(
    base_url: BaseUrl,
    fetch_impl?: FetchImpl,
): DecisionSecretCache {
    const key = base_url('/decision-secret');
    let cache = secret_caches.get(key);
    if (!cache) {
        cache = create_decision_secret_cache(() => fetch_decision_secret(base_url, fetch_impl));
        secret_caches.set(key, cache);
    }
    return cache;
}

/** `send` with the secret; on a refusal (`refused`), refetch the secret once and send once more. */
export async function with_decision_secret<T>(
    cache: DecisionSecretCache,
    send: (secret: string) => Promise<T>,
    refused: (result: T) => boolean,
): Promise<{ ok: true; result: T } | { ok: false; error: string }> {
    for (let attempt = 0; ; attempt += 1) {
        const secret = await cache.get();
        if (!secret.ok) return secret;
        const result = await send(secret.secret);
        if (attempt > 0 || !refused(result)) return { ok: true, result };
        cache.invalidate();
    }
}

export function decision_secret_headers(secret: string): Record<string, string> {
    return { [DECISION_SECRET_HEADER]: secret };
}

/** The steps an answer reopened, off its 200 body; none when the body says nothing. */
export function reopened_steps(body: unknown): string[] {
    const parsed = decision_answer_response_schema.safeParse(body);
    return parsed.success ? parsed.data.reopened : [];
}

export interface DecisionResult {
    ok: boolean;
    error?: string;
    reopened: string[];
}

/** A verb's answer, read the one way both surfaces need it. */
export function decision_outcome(status: number, body: unknown): DecisionResult {
    if (status === 0) return { ok: false, error: 'Bridge not reachable', reopened: [] };
    if (status === 404) return { ok: false, error: RESTART_BRIDGE, reopened: [] };
    const reply = body as { ok?: unknown; error?: unknown } | null;
    if (status >= 200 && status < 300 && reply?.ok !== false) return { ok: true, reopened: reopened_steps(body) };
    const error = typeof reply?.error === 'string' && reply.error ? reply.error.slice(0, 160) : `bridge responded ${status}`;
    return { ok: false, error, reopened: [] };
}

/** The rack's POST of one verb. Any other surface posts the same route with the same secret. */
export async function post_decision(
    base_url: BaseUrl,
    request: DecisionRequest,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<DecisionResult> {
    const { path, body } = decision_route(request);
    const sent = await with_decision_secret(
        decision_secret_cache(base_url, fetch_impl),
        async (secret) => {
            try {
                const response = await fetch_impl(base_url(path), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...decision_secret_headers(secret) },
                    body: JSON.stringify(body),
                    signal: AbortSignal.timeout(VERB_TIMEOUT_MS),
                });
                return { status: response.status, body: (await response.json().catch(() => null)) as unknown };
            } catch {
                return { status: 0, body: null as unknown };
            }
        },
        (result) => result.status === 403,
    );
    if (!sent.ok) return { ok: false, error: sent.error, reopened: [] };
    return decision_outcome(sent.result.status, sent.result.body);
}

/** The line a verb leaves on the rack's notice strip: a refusal, or the steps an answer reopened. */
export function decision_notice(result: DecisionResult): string | null {
    if (!result.ok) return `decision not recorded — ${result.error ?? 'the bridge refused it'}`;
    return result.reopened.length > 0 ? `answered — reopened ${result.reopened.join(', ')}` : null;
}

// ── LAUNCH › LEAD ───────────────────────────────────────────────────────────

export interface LeadLaunchRequest {
    /** `"auto"` (the bridge picks among the Claude accounts) or a Claude profile id. */
    profile: string;
    /** A run id: RESUME that run under a new lead. */
    resume?: string;
}

/** The `'lead'` action's JSON value, re-validated on every hop; null for anything else. */
export function parse_lead_launch_request(value: unknown): LeadLaunchRequest | null {
    if (typeof value !== 'string') return null;
    try {
        const parsed = lead_launch_request_schema.safeParse(JSON.parse(value));
        return parsed.success ? parsed.data : null;
    } catch {
        return null;
    }
}

export type LeadLaunchResult =
    | { ok: true; session: string; profile: string; run: string | null }
    | { ok: false; error: string };

export async function post_lead_launch(
    base_url: BaseUrl,
    request: LeadLaunchRequest,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<LeadLaunchResult> {
    try {
        const response = await fetch_impl(base_url('/lead/launch'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(request),
            signal: AbortSignal.timeout(LAUNCH_TIMEOUT_MS),
        });
        if (response.status === 404) return { ok: false, error: RESTART_BRIDGE };
        const body: unknown = await response.json().catch(() => null);
        if (response.ok) {
            const parsed = lead_launch_response_schema.safeParse(body);
            if (parsed.success) return parsed.data;
        }
        const error = (body as { error?: unknown } | null)?.error;
        return { ok: false, error: typeof error === 'string' && error ? error.slice(0, 200) : `bridge responded ${response.status}` };
    } catch {
        return { ok: false, error: 'the bridge did not answer' };
    }
}

export interface LeadLaunchHooks {
    base_url: BaseUrl;
    /**
     * 201: the session exists. Hand it to the owner's 🏝 focus (`create_island_focus.arm`), which
     * focuses it the poll the snapshot lists it live — never before the bridge answered, or the pane
     * would build a bare shell under the name (`spawn-client.ts::launch_new` owns why).
     */
    on_launched: (session: string) => void;
    on_failure: (message: string) => void;
    fetch_impl?: FetchImpl;
}

/** The `'lead'` action, for both rack payload owners. A malformed value does nothing. */
export async function launch_lead(value: unknown, hooks: LeadLaunchHooks): Promise<void> {
    const request = parse_lead_launch_request(value);
    if (!request) return;
    const result = await post_lead_launch(hooks.base_url, request, hooks.fetch_impl);
    if (result.ok) hooks.on_launched(result.session);
    else hooks.on_failure(`LEAD not started — ${result.error}`);
}

// ── the rack payload owners ─────────────────────────────────────────────────

/** What a rack payload owner (`rack-http-transport.ts`, or a host application's own) hands
 *  `handle_lead_action`: its own state, the only thing owners differ in. */
export interface LeadActionDeps {
    base_url: BaseUrl;
    /** The owner's 🏝 hand-off. The bridge names a lead's session, so nothing is pending before
     *  it answers; the hand-off focuses the session the poll the snapshot lists it live. */
    island_focus: Pick<IslandFocus, 'arm' | 'cancel'>;
    /** The rack's notice strip: a refusal, or the steps an answer reopened. */
    launch_notice: Pick<LaunchNotice, 'show' | 'clear'>;
    /** Tick the owner's rack ingestor, so the new row (and with it the hand-off) lands now. */
    tick_fleet: () => void;
    /** Re-read `/lead/state`, so a press shows at once rather than a poll later. */
    tick_lead: () => void;
    fetch_impl?: FetchImpl;
    now?: () => number;
}

/**
 * `'lead'` and `'lead_decision'`, the one sequence both owners run. Returns false for any other
 * action, so an owner writes `if (handle_lead_action(action, value, deps)) return;`.
 */
export function handle_lead_action(action: string, value: unknown, deps: LeadActionDeps): boolean {
    if (action === 'lead') {
        // Any launch outranks a 🏝 hand-off still waiting, exactly as `+ NEW` does.
        deps.island_focus.cancel();
        deps.launch_notice.clear();
        void launch_lead(value, {
            base_url: deps.base_url,
            on_launched: (session) => {
                deps.island_focus.arm(session, (deps.now ?? Date.now)());
                deps.tick_fleet();
                deps.tick_lead();
            },
            on_failure: (message) => deps.launch_notice.show(message),
            fetch_impl: deps.fetch_impl,
        });
        return true;
    }
    if (action === 'lead_decision') {
        const request = parse_decision_request(value);
        if (request) {
            void post_decision(deps.base_url, request, deps.fetch_impl).then((result) => {
                const notice = decision_notice(result);
                if (notice) deps.launch_notice.show(notice);
                deps.tick_lead();
            });
        }
        return true;
    }
    return false;
}
