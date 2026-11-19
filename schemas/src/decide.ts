/**
 * The typed-decision contract: a state and typed questions in, probability-scored answers out.
 *
 * One shape, every provider. Laya (`convaiinnovations/laya`, served by `laya-serve` behind the bridge's `laya`
 * plugin) speaks the `POST /v1/systemone` wire, and any provider a host adds speaks it too, so the plugin, the
 * pages and a host's own server all validate against THIS module rather than a hand-written twin.
 *
 * What lives here: the questions and answers, the one reader of a `/v1/systemone` answer, the surface names, the
 * rack's own surfaces, Laya's model ids, and the Laya decision ledger. What does not: which provider answers a
 * host's surfaces, and where each can run. That registry belongs to the host, which imports these types and
 * names its surfaces beside the rack's.
 */

import { z } from 'zod';

// ── the questions ────────────────────────────────────────────────────────────

export const decideQuestionTypeSchema = z.enum(['noul', 'choice', 'score']);
export type DecideQuestionType = z.infer<typeof decideQuestionTypeSchema>;

/**
 * One question. `noul` = the probability a condition holds (0.5 = "cannot tell"); `choice` = one
 * of a fixed set (`criteria` = `{option: description}`); `score` = a 0-based degree (`criteria` =
 * level descriptions, lowest first).
 */
export const decideQuestionSchema = z.object({
  type: decideQuestionTypeSchema,
  instructions: z.string().min(1),
  criteria: z.union([z.record(z.string(), z.string()), z.array(z.string()).readonly()]).optional(),
});
export type DecideQuestion = z.infer<typeof decideQuestionSchema>;

/** Question names are wire keys AND stored-verdict keys: short, stable identifiers. */
export const DECIDE_QUESTION_NAME = /^[a-z][a-z0-9_]{0,63}$/;

export const decideRequestSchema = z.object({
  /** What the questions are about. Every question shares it — that is what makes them one request. */
  state: z.union([z.string().min(1), z.record(z.string(), z.unknown())]),
  questions: z
    .record(z.string().regex(DECIDE_QUESTION_NAME), decideQuestionSchema)
    .refine((questions) => Object.keys(questions).length > 0, 'a request must ask at least one question'),
});
export type DecideRequest = z.infer<typeof decideRequestSchema>;

// ── the answers ──────────────────────────────────────────────────────────────

const probability = z.number().finite().min(0).max(1);

/** One typed answer. Exactly one of `noul` / `choice` / `score` is set, per the question's type. */
export const decideAnswerSchema = z.object({
  noul: probability.optional(),
  choice: z.string().min(1).optional(),
  score: z.number().finite().min(0).optional(),
  /** Choice/Score only — a noul's value IS its confidence. */
  confidence: probability.optional(),
  /** Choice/Score: the full distribution over options (score levels keyed "0", "1", …). */
  probabilities: z.record(z.string(), probability).optional(),
});
export type DecideAnswer = z.infer<typeof decideAnswerSchema>;

export const decideResultSchema = z.object({
  /** Keyed by question name. Every ASKED question is present, or the call threw. */
  answers: z.record(z.string(), decideAnswerSchema),
  usage: z.object({
    input_tokens: z.number().nonnegative(),
    output_tokens: z.number().nonnegative(),
    /** The vendor's own USD figure when it reports one — recorded, never trusted for metering. */
    cost_usd: z.number().nonnegative().nullable(),
  }),
  /** The model the endpoint says answered — may be a dated build of the requested alias. */
  model: z.string(),
});
export type DecideResult = z.infer<typeof decideResultSchema>;

// ── reading a /v1/systemone answer ────────────────────────────────────────────
//
// One reader for every client (the page's Laya client, `ui/src/decide/laya-local-client.ts`, and any host's
// server-side decider), so "what counts as a trustworthy answer" cannot drift between them.

/**
 * Strict only where trust is at stake: every ASKED answer must be readable, and
 * `usage.input_tokens` must be a count, because the pass's cost cap meters on
 * it — a missing count would make a paid call look free. Everything else is
 * `.passthrough()` or `.catch()`, so a field the vendor adds (or drops from the
 * informational ones) never fails a paid answer. `answers.<n>.type` is not read
 * at all: its casing is unverified, and the ASKED question already says which
 * field to read.
 */
const systemOneResponseSchema = z
  .object({
    model: z.string().min(1).optional().catch(undefined),
    // Values checked per ASKED question (`read_answer`), so a malformed answer
    // to a question nobody asked cannot fail the call.
    answers: z.record(z.string(), z.unknown()),
    usage: z
      .object({
        input_tokens: z.number().nonnegative(),
        output_tokens: z.number().nonnegative().optional().catch(undefined),
        cost: z.number().nonnegative().nullable().optional().catch(null),
      })
      .passthrough(),
  })
  .passthrough();

function is_probability(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
}

class ShapeError extends Error {}

/** Choice/Score extras. Informational, so a malformed one is dropped rather than failing the call. */
function read_confidence_fields(raw: Record<string, unknown>): Pick<DecideAnswer, 'confidence' | 'probabilities'> {
  const extras: Pick<DecideAnswer, 'confidence' | 'probabilities'> = {};
  const confidence = raw.confidence;
  if (is_probability(confidence)) extras.confidence = confidence;

  const probabilities = raw.probabilities;
  if (probabilities && typeof probabilities === 'object' && !Array.isArray(probabilities)) {
    const entries = Object.entries(probabilities as Record<string, unknown>);
    if (entries.length > 0 && entries.every(([, p]) => is_probability(p))) {
      extras.probabilities = Object.fromEntries(entries) as Record<string, number>;
    }
  }
  return extras;
}

/** One asked question's answer, read by the ASKED type. Throws `ShapeError`. */
function read_answer(name: string, question: DecideQuestion, value: unknown): DecideAnswer {
  if (value === undefined) throw new ShapeError(`no answer for the asked question "${name}"`);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ShapeError(`"${name}" answer is not an object`);
  const raw = value as Record<string, unknown>;

  switch (question.type) {
    case 'noul': {
      // A Noul's value IS its confidence (no separate field is documented), so
      // nothing else is read — and nothing else is required.
      const noul = raw.noul;
      if (!is_probability(noul)) throw new ShapeError(`"${name}" noul is not a probability in [0, 1]`);
      return { noul };
    }
    case 'choice': {
      const choice = raw.choice;
      if (typeof choice !== 'string' || !choice) throw new ShapeError(`"${name}" carries no choice`);
      const options = question.criteria && !Array.isArray(question.criteria) ? Object.keys(question.criteria) : null;
      if (options && !options.includes(choice)) throw new ShapeError(`"${name}" chose an option it was not offered`);
      return { choice, ...read_confidence_fields(raw) };
    }
    case 'score': {
      const score = raw.score;
      // Levels are 0-based and the score interpolates between them.
      const top = Array.isArray(question.criteria) ? question.criteria.length - 1 : Number.POSITIVE_INFINITY;
      if (typeof score !== 'number' || !Number.isFinite(score) || score < 0 || score > top) {
        throw new ShapeError(`"${name}" score is outside its levels`);
      }
      return { score, ...read_confidence_fields(raw) };
    }
  }
}

export type SystemOneRead = { ok: true; result: DecideResult } | { ok: false; detail: string };

/** A 2xx body's text → the typed result, or why it cannot be trusted. Never throws. */
export function read_systemone_response(text: string, request: DecideRequest, requested_model: string): SystemOneRead {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return { ok: false, detail: 'the response body is not JSON' };
  }

  const parsed = systemOneResponseSchema.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    return { ok: false, detail: `unexpected response shape at "${issue?.path.join('.') ?? ''}"` };
  }

  const answers: Record<string, DecideAnswer> = {};
  try {
    for (const [name, question] of Object.entries(request.questions)) {
      answers[name] = read_answer(name, question, parsed.data.answers[name]);
    }
  } catch (error) {
    if (error instanceof ShapeError) return { ok: false, detail: error.message };
    throw error;
  }

  const usage = parsed.data.usage;
  return {
    ok: true,
    result: {
      answers,
      usage: {
        input_tokens: usage.input_tokens,
        output_tokens: usage.output_tokens ?? 0,
        cost_usd: usage.cost ?? null,
      },
      // Absent means the endpoint did not say; the request named this one.
      model: parsed.data.model ?? requested_model,
    },
  };
}

// ── the surfaces ─────────────────────────────────────────────────────────────

/**
 * A surface is the feature that asks: its name tags every ledger row, review card and fine-tune. A host names
 * its own (a mail triage, a feed gate), so the wire, the ledger and the review deck take any name of this shape,
 * and a host's registry adds its surfaces beside `DECIDE_SURFACES`. Python twin: plugins/laya/laya_bridge/paths.py
 * `SURFACE_NAME`.
 */
export const DECIDE_SURFACE_NAME = /^[a-z][a-z0-9_]{0,63}$/;
export const decideSurfaceSchema = z.string().regex(DECIDE_SURFACE_NAME);
export type DecideSurface = z.infer<typeof decideSurfaceSchema>;

/** The rack's own surfaces: a finished agent turn (`agent_open_work`) and the coordinator's routing. */
export const rackSurfaceSchema = z.enum(['agent_open_work', 'coordinator']);
export type RackSurface = z.infer<typeof rackSurfaceSchema>;

/** What a page shows for a surface. A host's registry extends it with where the surface runs and who answers. */
export interface DecideSurfaceInfo {
  /** Mono label. A surface without an entry reads as its name, upper-cased, `_` as a space. */
  label: string;
}

/** The surfaces this package registers: the rack's. A host spreads it into its own registry and adds its surfaces. */
export const DECIDE_SURFACES: Readonly<Record<RackSurface, DecideSurfaceInfo>> = {
  agent_open_work: { label: 'OPEN WORK' },
  coordinator: { label: 'COORDINATOR' },
};

/**
 * A fine-tune's served name: the prefix, then the surface with `_` as `-` (`tldrack-agent-open-work`). The
 * trainer registers a surface's fine-tune under it and the plugin serves it under it, so the two cannot disagree
 * about which checkpoint answers which surface. Python twin: plugins/laya/laya_bridge/paths.py.
 */
export const LAYA_FINE_TUNE_PREFIX = 'tldrack-';
/**
 * Legacy (step 6.8): the prefix before 0.1.0. Registries, ledger rows, reports and checkpoint folders on disk still
 * carry it, so a reader takes it as the same model for one release (`served_model_name`); nothing writes it.
 */
export const LAYA_LEGACY_FINE_TUNE_PREFIX = 'voidcraft-';

export function fine_tune_name(surface: DecideSurface): string {
  return `${LAYA_FINE_TUNE_PREFIX}${surface.replace(/_/g, '-')}`;
}

/**
 * A model name as it is served today: one under LAYA_LEGACY_FINE_TUNE_PREFIX reads as `tldrack-<surface>`, any other name as
 * it is. A checkpoint folder's name (`…-<stamp>`) is never rewritten. Python twin: paths.py `served_name`.
 */
export function served_model_name(name: string): string {
  if (!name.startsWith(LAYA_LEGACY_FINE_TUNE_PREFIX) || /-[0-9]/.test(name)) return name;
  return LAYA_FINE_TUNE_PREFIX + name.slice(LAYA_LEGACY_FINE_TUNE_PREFIX.length);
}

// ── laya-local model ids ─────────────────────────────────────────────────────

/**
 * Every verdict Laya gives through the bridge carries `laya-local/<checkpoint>@<version>`: the checkpoint the
 * plugin served and its revision (`plugins/laya` reports both as `model_ids`). A reader that cannot know which
 * checkpoint ran (a host's server reading verdicts a page handed it) asks for the FAMILY, `laya-local`, and
 * `model_matches` takes any member of it.
 */
export const LAYA_LOCAL_FAMILY = 'laya-local';
/** A name Laya serves: a built-in or a registered fine-tune. Python mirror: laya_bridge/wire.py `MODEL_NAME`. */
const LAYA_MODEL_NAME_PART = '[a-z][a-z0-9-]{0,63}';
export const LAYA_MODEL_NAME = new RegExp(`^${LAYA_MODEL_NAME_PART}$`);
const LAYA_LOCAL_MODEL_ID = new RegExp(`^${LAYA_LOCAL_FAMILY}/${LAYA_MODEL_NAME_PART}@[A-Za-z0-9._-]{1,64}$`);

export function is_laya_local_model_id(value: unknown): value is string {
  return typeof value === 'string' && LAYA_LOCAL_MODEL_ID.test(value);
}

/** Does a stored verdict's model satisfy the requested one? Exact, or a member of a requested family. */
export function model_matches(stored: string, requested: string): boolean {
  if (stored === requested) return true;
  return requested === LAYA_LOCAL_FAMILY && is_laya_local_model_id(stored);
}

// ── the Laya decision ledger ─────────────────────────────────────────────────
// Row owner. Python mirror: plugins/laya/laya_bridge/ledger.py, which owns why a row keeps what it keeps.

export const LAYA_LEDGER_VERSION = 1;
export const LAYA_DECISION_ID = /^\d{8}T\d{6}-[0-9a-f]{12}$/; // UTC second + 48 random bits; sorts by time
export const LAYA_STATE_DIGEST = /^[0-9a-f]{64}$/;
export const LAYA_LEDGER_STATE_MAX = 8_000;
export const LAYA_LEDGER_SUBJECT = /^[A-Za-z0-9_.:#|-]{1,128}$/;
/**
 * The agent conversation a rack verdict judged: Claude Code's session id or Codex's thread id, a UUID and only a UUID
 * (tldrack `profiles.py` `AGENT_SESSION_ID_RE`, the hook-text owner). An opaque id, never text or a path, so it
 * fits the tag rule above; it is how the REVIEW page bands a verdict into its thread
 * (bridge/tldrack/ledger/laya_links.py owns the join).
 */
export const LAYA_LEDGER_CONVERSATION = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
/** A path relative to Laya's data directory (`<data>/laya`): `/`-separated, never absolute, no `.` or `..` segment, no backslash. */
export const LAYA_DATA_RELATIVE_PATH = /^(?!.*(?:^|\/)\.\.?(?:\/|$))[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/;
/** The only surfaces whose decision rows keep their state text; every other surface, and `untagged`, is hash-only. */
export const LAYA_LEDGER_KEEP_TEXT: readonly RackSurface[] = ['agent_open_work'];
/**
 * Surfaces only a native caller may tag: the rack's own, so no page can write a verdict into the rack's ledger.
 * What counts as native: owned by plugins/laya/laya_bridge/guard.py — see the note there.
 */
export const LAYA_LEDGER_RACK_SURFACES: readonly RackSurface[] = rackSurfaceSchema.options;
/** The surface a row records when its call named none. Never a tag's surface. */
export const LAYA_LEDGER_UNTAGGED = 'untagged';
export const layaLedgerSurfaceSchema = decideSurfaceSchema;
export const layaReactionActionSchema = z.enum(['shown', 'accepted', 'converted', 'dismissed', 'expired']);

/**
 * The tag fields a decision row copies as they came (`ledger.py` `record_decision`): one list for both schemas.
 * Adding one: owned by bridge/tldrack/open_work.py `OPTIONAL_TAG_FIELDS` — see the note there.
 */
const ledgerTagFields = {
  subject: z.string().regex(LAYA_LEDGER_SUBJECT).optional(), // the caller's, never the state's text
  conversation: z.string().regex(LAYA_LEDGER_CONVERSATION).optional(), // LAYA_LEDGER_RACK_SURFACES only (ledger.py `clean_tag`)
  question_version: z.number().int().nonnegative().optional(),
  threshold: probability.optional(),
  done_threshold: probability.optional(), // the agent rack's ✅ cut: tldrack open_work.py DONE_AT
};

/** A `/decide` request's `ledger` field. The plugin reads it; laya-serve never sees it. */
export const layaLedgerTagSchema = z.strictObject({
  surface: decideSurfaceSchema.refine((surface) => surface !== LAYA_LEDGER_UNTAGGED, 'a tag names a surface'),
  ...ledgerTagFields,
});

/**
 * The longest option NAME a Laya question may carry, in UTF-16 units — the ledger records it as `choice`.
 * The bridge's wire check refuses a whole request past it (`plugins/laya/laya_bridge/wire.py`
 * `MAX_OPTION_NAME_CHARS`, the Python twin of this number). A surface that builds options from user data
 * drops longer names before asking.
 */
export const LAYA_CHOICE_NAME_MAX = 80;

/** One asked question's recorded answer. Which value lands where, per type: `ledger.py` `decisions_from`. */
export const layaLedgerAnswerSchema = z
  .strictObject({
    q: z.string().regex(DECIDE_QUESTION_NAME),
    qh: z.string().regex(/^[0-9a-f]{12}$/),
    type: decideQuestionTypeSchema,
    choice: z.string().max(LAYA_CHOICE_NAME_MAX),
    p: probability,
    score: z.number().min(0).optional(),
    alt: z.array(z.strictObject({ choice: z.string().max(LAYA_CHOICE_NAME_MAX), p: probability })).max(63).optional(),
    abstain: z.literal(true).optional(),
  })
  .refine((answer) => !answer.abstain || (answer.choice === '' && answer.p === 0 && answer.score === undefined && answer.alt === undefined), {
    path: ['abstain'],
    message: 'an abstain records no choice, p, score or alternatives',
  })
  .refine((answer) => answer.abstain || answer.choice !== '', { path: ['choice'], message: 'an answer names its choice' })
  .refine((answer) => answer.abstain || (answer.type === 'score') === (answer.score !== undefined), {
    path: ['score'],
    message: 'score is set on exactly the score answers',
  })
  .refine((answer) => answer.type !== 'noul' || answer.alt === undefined, { path: ['alt'], message: 'a noul answer has no alternatives' });

/** One `/decide` call. Reactions to it are later rows that name its `decision_id`. */
export const layaLedgerDecisionRowSchema = z
  .strictObject({
    kind: z.literal('decision'),
    v: z.literal(LAYA_LEDGER_VERSION),
    decision_id: z.string().regex(LAYA_DECISION_ID),
    at: z.iso.datetime(), // rows.py `utc_stamp`: the `Z` form
    surface: layaLedgerSurfaceSchema,
    ...ledgerTagFields,
    model: z.string().regex(LAYA_MODEL_NAME), // the served name the answer came from
    model_id: z.string().refine(is_laya_local_model_id).nullable(),
    checkpoint_path: z.string().max(256).regex(LAYA_DATA_RELATIVE_PATH).nullable(), // null for a built-in; disk only, never HTTP
    state_hash: z.string().regex(LAYA_STATE_DIGEST), // rows.py `state_digest` of the whole state, kept or not
    state: z.string().max(LAYA_LEDGER_STATE_MAX).nullable(), // null outside LAYA_LEDGER_KEEP_TEXT
    state_truncated: z.boolean(),
    decisions: z.array(layaLedgerAnswerSchema).min(1).max(32),
    ms: z.number().int().nonnegative(),
    // load, cold: owned by plugins/laya/laya_bridge/ledger.py — see the note there. Absent from older rows.
    load: z.number().nonnegative().optional(),
    cold: z.boolean().optional(),
  })
  .refine((row) => row.state === null || (LAYA_LEDGER_KEEP_TEXT as readonly string[]).includes(row.surface), {
    path: ['state'],
    message: 'text is kept only for LAYA_LEDGER_KEEP_TEXT surfaces',
  })
  .refine((row) => row.state !== null || !row.state_truncated, { path: ['state_truncated'], message: 'a hash-only row has no text to truncate' });

export const layaLedgerReactionRowSchema = z.strictObject({
  kind: z.literal('reaction'),
  v: z.literal(LAYA_LEDGER_VERSION),
  decision_id: z.string().regex(LAYA_DECISION_ID),
  at: z.iso.datetime(),
  surface: layaLedgerSurfaceSchema,
  action: layaReactionActionSchema,
});

export const layaLedgerRowSchema = z.discriminatedUnion('kind', [layaLedgerDecisionRowSchema, layaLedgerReactionRowSchema]);

/** The body of `POST /react`. */
export const layaReactRequestSchema = z.strictObject({ decision_id: z.string().regex(LAYA_DECISION_ID), action: layaReactionActionSchema });

// ── the landing record ───────────────────────────────────────────────────────
// Row owner. Python mirror and its one writer: plugins/laya/laya_bridge/landing.py, which owns why a verdict's
// landing is a sidecar row and never a tag field — see the note there.

export const LAYA_LANDING_VERSION = 1;
/** The surfaces whose verdicts land on a rack row. */
export const LAYA_LANDING_SURFACES = ['agent_open_work'] as const satisfies readonly RackSurface[];
/** A landed ⚠️'s class (tldrack attention.py `LANDING_CLASSES`); null on the row = it drew no ⚠️. */
export const layaLandingClassSchema = z.enum(['held', 'new', 'repeat', 'retired']);
/** Why a row was held (attention.py `HELD_REASONS`); null on the row = a free row. */
export const layaHeldReasonSchema = z.enum(['background', 'children']);
/** No live count comes near it; a larger one is a writer bug (landing.py `COUNT_MAX`). */
export const LAYA_LANDING_COUNT_MAX = 100_000;
const landingCount = z.number().int().nonnegative().max(LAYA_LANDING_COUNT_MAX);
/** What was still running when it landed: what decided `held` (attention.py § 1, § 1b). */
export const layaLandingRunningSchema = z.strictObject({
  descendants: landingCount,
  agents: landingCount,
  workflows: landingCount,
  shells: landingCount,
});
/** The turn's trace (tldrack transcript.py `fold_turn_trace`): logged, never fed to Laya. */
export const layaLandingTraceSchema = z.strictObject({
  ran_tests: z.boolean(),
  edited_files: landingCount,
  opened_pr: z.boolean(),
  ended_on_question: z.boolean(),
});

export const layaLandingRowSchema = z
  .strictObject({
    kind: z.literal('landing'),
    v: z.literal(LAYA_LANDING_VERSION),
    decision_id: z.string().regex(LAYA_DECISION_ID), // the ledger decision it joins
    at: z.iso.datetime(), // when the plugin wrote it: rows.py `utc_stamp`
    surface: z.enum(LAYA_LANDING_SURFACES),
    class: layaLandingClassSchema.nullable(),
    held: layaHeldReasonSchema.nullable(), // the audience, recorded for every verdict
    running: layaLandingRunningSchema,
    trace: layaLandingTraceSchema.nullable(), // null without a Claude transcript
  })
  .refine((row) => row.class !== 'held' || row.held !== null, { path: ['held'], message: 'a held ⚠️ names why' })
  .refine((row) => !(row.class === 'new' || row.class === 'repeat') || row.held === null, {
    path: ['held'],
    message: 'a new or repeat ⚠️ was shown, so it was not held',
  });

export type LayaLandingClass = z.infer<typeof layaLandingClassSchema>;
export type LayaHeldReason = z.infer<typeof layaHeldReasonSchema>;
export type LayaLandingRow = z.infer<typeof layaLandingRowSchema>;

export type LayaLedgerSurface = z.infer<typeof layaLedgerSurfaceSchema>;
export type LayaLedgerTag = z.infer<typeof layaLedgerTagSchema>;
export type LayaLedgerAnswer = z.infer<typeof layaLedgerAnswerSchema>;
export type LayaLedgerDecisionRow = z.infer<typeof layaLedgerDecisionRowSchema>;
export type LayaLedgerReactionRow = z.infer<typeof layaLedgerReactionRowSchema>;
export type LayaLedgerRow = z.infer<typeof layaLedgerRowSchema>;
export type LayaReactionAction = z.infer<typeof layaReactionActionSchema>;
export type LayaReactRequest = z.infer<typeof layaReactRequestSchema>;
