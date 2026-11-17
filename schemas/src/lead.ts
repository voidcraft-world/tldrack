import { z } from 'zod';

/**
 * The LEAD launch's wire contract: runs, steps and decisions (rung 2's inbox).
 * Python owns every rule (`bridge/tldrack/lead/`, `inbox.py`); this file only
 * validates what crosses into the browser.
 */

export const LEAD_STEP_STATES = [
    'pending', 'spawning', 'working', 'ready', 'published', 'reported',
    'closed', 'reopened', 'failed', 'abandoned',
] as const;

export const lead_step_schema = z.object({
    step_id: z.string().min(1).max(40),
    title: z.string().max(200),
    /** Stored tokens: `ship` is a change step, `scout` a report step (bridge `lead/runs.py` `KIND_WORDS`). */
    kind: z.enum(['ship', 'scout']),
    /** The agent family a step runs on: `claude`, `any`, or another the bridge names (`lead/runs.py` LANES owns the list). */
    lane: z.string().regex(/^[a-z][a-z0-9-]{0,31}$/),
    state: z.enum(LEAD_STEP_STATES),
    parked: z.boolean(),
    crew_session: z.string().nullable(),
    crew_live: z.boolean(),
    profile: z.string().nullable(),
    branch: z.string().nullable(),
    pr_url: z.string().nullable(),
    ci: z.enum(['pending', 'passing', 'failing', 'none']).nullable(),
});
export type LeadStep = z.infer<typeof lead_step_schema>;

export const decision_schema = z.object({
    key: z.string().min(1).max(80),
    source: z.string().min(1).max(40),
    session: z.string().nullable(),
    run_id: z.string().nullable(),
    title: z.string().min(1).max(200),
    detail: z.string().max(2000),
    options: z.array(z.string().min(1).max(60)).max(6).nullable(),
    opened_at: z.number(),
    defer_until: z.number().nullable(),
    /** True while `defer_until` is still in the future: the row is hidden from the open list. */
    deferred: z.boolean(),
    blocking: z.boolean(),
    assumption: z.string().max(400).nullable(),
    state: z.enum(['open', 'answered', 'dismissed', 'withdrawn']),
    steps: z.array(z.string()).max(64),
});
export type Decision = z.infer<typeof decision_schema>;

export const lead_run_schema = z.object({
    id: z.string().min(1).max(40),
    title: z.string().max(200),
    state: z.enum(['open', 'ended']),
    lead_session: z.string().nullable(),
    /** False when no live session wears the run's lead identity: the row offers RESUME. */
    lead_live: z.boolean(),
    profile: z.string().nullable(),
    plan_path: z.string().nullable(),
    created_at: z.number(),
    steps: z.array(lead_step_schema).max(64),
    /** The model pair this run STARTED with (see `lead_models_schema`). Absent on an older bridge. */
    lead_model: z.string().max(80).nullable().optional(),
    crew_model: z.string().max(80).nullable().optional(),
    /** Open and deferred decisions of this run, newest first. */
    decisions: z.array(decision_schema).max(64),
});
export type LeadRun = z.infer<typeof lead_run_schema>;

/**
 * The standing model pair a NEW lead launches with: the lead's model, and the weaker one its workers
 * and its own subagents run on. `null` worker model = no split; `null` model = the account's own. The rules
 * and the default: owned by `bridge/tldrack/lead/models.py` — see the note there.
 */
export const lead_models_schema = z.object({
    model: z.string().min(1).max(80).nullable(),
    crew_model: z.string().min(1).max(80).nullable(),
    /** Model families, weakest first: what a worker model may be chosen from. */
    families: z.array(z.string().min(1).max(20)).max(8),
});
export type LeadModels = z.infer<typeof lead_models_schema>;
/** Either half of the pair, as a request names it: absent = leave it, `null` = clear it. */
const model_pair_fields = {
    model: z.string().min(1).max(80).nullable().optional(),
    crew_model: z.string().min(1).max(80).nullable().optional(),
};
/** `POST /lead/models` → 200 with the stored pair. Only the keys sent change. */
export const lead_models_request_schema = z.object(model_pair_fields);

/** `GET /lead/state`. Absent on an older bridge (404): the surfaces grow nothing. */
export const lead_state_schema = z.object({
    runs: z.array(lead_run_schema).max(32),
    /** Absent on a bridge that keeps no pair. */
    models: lead_models_schema.optional(),
});
export type LeadState = z.infer<typeof lead_state_schema>;

/**
 * `POST /lead/launch {profile, resume?, model?, crew_model?}` → 201. `profile` is `"auto"` or a
 * Claude profile id. `model` / `crew_model` override the standing pair for this one launch; a
 * RESUME ignores them and takes its run's.
 */
export const lead_launch_request_schema = z.object({
    profile: z.string().min(1).max(40),
    resume: z.string().min(1).max(40).optional(),
    ...model_pair_fields,
});
export const lead_launch_response_schema = z.object({
    ok: z.literal(true),
    session: z.string(),
    profile: z.string(),
    run: z.string().nullable(),
});

/** The operator's verbs. Every one carries `X-VC-Decision-Secret` (`GET /decision-secret`). */
export const decision_answer_request_schema = z.object({
    key: z.string().min(1).max(80),
    kind: z.enum(['keep', 'option', 'text']),
    answer: z.string().max(2000).optional(),
});
export const decision_defer_request_schema = z.object({
    key: z.string().min(1).max(80),
    /** Epoch seconds, in the future, at most 30 days out. */
    until: z.number(),
});
export const decision_dismiss_request_schema = z.object({
    key: z.string().min(1).max(80),
    reason: z.string().min(1).max(400),
});
export const decision_secret_response_schema = z.object({ secret: z.string().min(32).max(128) });
/** `POST /inbox/answer` → 200. `reopened` names the steps the answer moved to `reopened` (a contradicted assumption). */
export const decision_answer_response_schema = z.object({
    ok: z.literal(true),
    reopened: z.array(z.string().max(40)).max(64),
});

/** Additive `/snapshot` row fields: `lead` on a lead session, `crew` on a worker session. Absent on any other row. */
export const session_lead_schema = z.object({
    run: z.string().min(1).max(40).nullable(),
    open_decisions: z.number().int().min(0),
});
export type SessionLead = z.infer<typeof session_lead_schema>;
export const session_crew_schema = z.object({
    run: z.string().min(1).max(40),
    step: z.string().min(1).max(40),
});
export type SessionCrew = z.infer<typeof session_crew_schema>;

export const DECISION_SECRET_HEADER = 'X-VC-Decision-Secret';
