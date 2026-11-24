/**
 * The Laya REVIEW deck and OPS wire: what `plugins/laya` serves on `/review/*` (the deck's queue and its RECENT list), `/ops`, `/lab` and
 * `/restart`, the agent rack's in-process grade (`reviewRackRequestSchema`), and the operator's answer log on disk.
 *
 * Responses use Zod's default strip, so a newer bridge never breaks an older page. Requests are strict.
 * Server: `plugins/laya` — `laya_bridge/review.py`, `ops.py` and `review_log.py` are derived copies
 * of this file. Client: `ui/src/decide/laya-review-client.ts`.
 */

import { z } from 'zod';
import { LAYA_DATA_RELATIVE_PATH, LAYA_DECISION_ID, LAYA_LEDGER_CONVERSATION, decideSurfaceSchema } from './decide.ts';

export const LAYA_REVIEW_VERSION = 1;
export const REVIEW_KEY = /^(?:[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63})$/;
export const REVIEW_ANSWER_ID = /^a_[0-9a-f]{16}$/;
/** Every stratum, in the order a key is placed (`review_data.py` owns what each holds and why). */
export const REVIEW_STRATA = [
  'sample',
  'live_sample',
  'challenger',
  'suspect',
  'disagree',
  'uncertain',
  'golden',
  'flagged',
  'unflagged',
  'recent',
  'rest',
] as const;
/** What an answer from the agent rack's ⚠️ menu records as its stratum: a door, never a lane a key is placed in
 *  (`review_data.py` RACK_STRATUM, the twin and the owner of why). */
export const REVIEW_RACK_STRATUM = 'rack';
/** One press that graded a whole wave: every answer row it wrote carries it, so one UNDO takes them all back
 *  (`review_log.py` WAVE_ID, the twin; what a wave is: `review_data.py`). */
export const REVIEW_WAVE_ID = /^w_[0-9a-f]{16}$/;
/** The most texts one wave card grades (`review_data.py` WAVE_MAX). */
export const REVIEW_WAVE_MAX = 12;
const textKey = z.string().regex(/^[0-9a-f]{16}$/);
/** The three marks a turn end can draw on the agent rack: ✅ done · 💬 reply (unsure) · ⚠️ open. Owner of the cuts:
 *  bridge/tldrack/open_work.py § Three bands. */
export const REVIEW_BANDS = ['done', 'reply', 'open'] as const;
export const reviewBandSchema = z.enum(REVIEW_BANDS);
/** The RECENT list's default and largest page (`review_data.py` RECENT_DEFAULT / RECENT_MAX). */
export const REVIEW_RECENT_DEFAULT = 100;
export const REVIEW_RECENT_MAX = 200;
/** The deepest page `?offset=` reaches (`review.py` RECENT_OFFSET_MAX; why it pages: `review_data.py` `recent_rows`). */
export const REVIEW_RECENT_OFFSET_MAX = 100_000;
/** A RECENT page's cursor, `summary.next`, sent back as `?after=` for the page after it: where that page's last row sits
 *  in its order (`review_data.py` RECENT_CURSOR, the twin; why a place and not a count: `recent_rows`). */
export const REVIEW_RECENT_CURSOR =
  /^(?:n|e~[01]\.[0-9]{6})~[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z~(?:[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63})$/;
/** Its two orders (`review_data.py` RECENT_ORDERS): `newest` by time, `edge` nearest a cut first, over the whole ledger. */
export const REVIEW_RECENT_ORDERS = ['newest', 'edge'] as const;
/** A review card's surface: any surface name (owned by decide.ts — see the note there). */
export const layaReviewSurfaceSchema = decideSurfaceSchema;
export const reviewAnswerSchema = z.enum(['agree', 'flip', 'skip']);
/** An answer's second half on the agent rack's surface: what the turn left the operator. Beside OPEN, did it need the
 *  operator (`you`), or was the agent only waiting on its own work (`running`)? Beside DONE, `read`: the agent owes
 *  nothing, but its answer still owes the operator a read (📖), where a bare DONE is a turn closed without reading it.
 *  Each half keeps its side's `open`, so every count, sweep and training row built on `open` is unchanged; `needs` is
 *  the label the four-way grade trains on. `read` exists since `REVIEW_READ_SINCE`. */
export const REVIEW_NEEDS = ['you', 'running', 'read'] as const;
export const reviewNeedsSchema = z.enum(REVIEW_NEEDS);
/** The halves that ride beside `open: true`; every other one rides beside `open: false`. */
export const REVIEW_NEEDS_OPEN: readonly ReviewNeeds[] = ['you', 'running'];
/** Does `needs` sit on its own side of `open`? Absent always does. Python twin: review_log.py `needs_ok`. */
export function needs_side_ok(needs: ReviewNeeds | undefined, open: boolean | null | undefined): boolean {
  return needs === undefined || open === REVIEW_NEEDS_OPEN.includes(needs);
}
/** When `read` became a grade (`review_data.py` READ_SINCE, the twin). A DONE recorded before it could not say
 *  whether the answer needed a read, so the four-way grade does not count it as DONE; the open/done label keeps it. */
export const REVIEW_READ_SINCE = '2026-11-19T00:00:00Z';
/** The four-way grade's classes, DONE, its READ half and the two OPEN halves, in the order a readout prints them
 *  (`review_data.py` GRADES, the twin; `grade_of` owns which row is which). */
export const REVIEW_GRADES = ['done', 'read', 'running', 'you'] as const;
const NEEDS_SIDE = 'needs rides on its own side: read beside DONE, you and running beside OPEN';
/** The most blind retests one queue page carries (`review_data.py` `retest_cards`: one per 10 cards of a 50-card page). */
export const REVIEW_RETEST_PAGE_MAX = 5;
/** The lab verbs the plugin runs as jobs; TRAIN and LABEL never are: owned by
 *  plugins/laya/laya_bridge/lab_jobs.py — see the note there. */
export const layaLabVerbSchema = z.enum(['challenger-sweep', 'sweep', 'sweep-reset', 'score', 'promote', 'golden', 'compare']);
/** A lab job's state: one runs, the rest wait their turn in order (`queued`, with `position`). */
export const LAYA_LAB_JOB_STATES = ['idle', 'working', 'queued', 'failed'] as const;
/** The gated retrain's steps, in order, the states a step can be in, and a cycle's states: owned by
 *  plugins/laya/laya_bridge/lab_files.py (`RETRAIN_STEPS`, `RETRAIN_STEP_STATES`, `RETRAIN_STATES`) — see the note there. */
export const LAYA_RETRAIN_STEPS = ['export', 'label', 'build', 'train', 'gate', 'register', 'sweep', 'card'] as const;
export const LAYA_RETRAIN_STEP_STATES = ['pending', 'running', 'done', 'skipped', 'failed'] as const;
export const LAYA_RETRAIN_STATES = ['running', 'waiting', 'failed', 'done', 'refused', 'abandoned'] as const;
/** The verdict a registered version carries: `lab_files.GATE_TAGS`. */
const gateTagSchema = z.enum(['PASS', 'FORCED', 'MIGRATED', 'HELDOUT']);
const iso = z.string().max(40);
const n = z.number().int().nonnegative();
const p01 = z.number().min(0).max(1);
const kappa = z.number().min(-1).max(1).nullable();
const basename = z.string().max(120).regex(/^[^/\\]*$/); // never a path over HTTP
const layaSourceSchema = z.enum(['scores', 'ledger', 'eval']);
/** The @challenger's own answer on a text (`review_data.py` `shadow_rivals`): its version name, and its p and ⚠️ cut
 *  on the decision the card shows. On a `challenger` card it is the other answer than the served model's. */
const challengerBlock = { name: basename.min(1), p: p01, threshold: p01 };
// A refusal repeats `message` as `error`, so the shared readers (bridge-error.ts, bridge-post.ts) show it unchanged.
const envelope = {
  v: z.literal(LAYA_REVIEW_VERSION),
  ok: z.boolean(),
  message: z.string().max(160),
  code: z.string().max(40).optional(),
  error: z.string().max(160).optional(),
  hint: z.string().max(200).optional(),
};

// ── the deck ─────────────────────────────────────────────────────────────────

export const reviewVerdictSchema = z.object({ by: z.string().min(1).max(120), open: z.boolean(), why: z.string().max(400).nullable() });
export const reviewLayaSchema = z.object({
  p: p01,
  threshold: p01,
  model: z.string().max(80),
  checkpoint: basename.nullable(),
  question_version: n,
  source: layaSourceSchema,
});
export const reviewCardSchema = z.object({
  great_example: z.boolean().optional(), // absent from older bridges
  item_id: z.string().max(200), // `${surface}:${key}`
  key: z.string().regex(REVIEW_KEY),
  surface: layaReviewSurfaceSchema,
  stratum: z.string().max(32), // one of REVIEW_STRATA; a string so a new stratum never breaks an old page
  text: z.string().min(1).max(4_000), // the tail, as fold_closing_text keeps it
  text_chars: n,
  truncated: z.boolean(),
  held_out: z.boolean().nullable(), // key not in any trained set of the served fine-tune; null = unknown
  reference: reviewVerdictSchema, // what AGREE agrees with: Sonnet, else golden, else Laya
  sonnet: reviewVerdictSchema.nullable(),
  laya: reviewLayaSchema.nullable(),
  // `request`: the request a written case was written with (`review_data.py` REQUEST_MAX): shown on its card, never
  // scored. Absent when the case has none, and from an older bridge.
  golden: z
    .object({ case_id: z.string().max(64), expected: z.boolean(), why: z.string().max(400), request: z.string().max(600).optional() })
    .nullable(),
  decision: z
    .object({ decision_id: z.string().regex(LAYA_DECISION_ID), at: iso, shown: z.boolean(), reaction: z.string().max(16).nullable() })
    .nullable(),
  answers: z.array(reviewAnswerSchema).min(1).max(3),
  // The cut the card's p sits nearest and p minus it (`review_data.py` `edge_of`); null without a p, absent from an
  // older bridge. An `uncertain` card is one within 0.1 of a cut.
  edge: z.object({ cut: z.enum(['open', 'done']), delta: z.number().min(-1).max(1) }).nullable().optional(),
  // A wave's lead: how many texts one press grades (`n`, this one included) and every other one's tail, oldest first.
  // Answer it with `wave` = those keys. Null on a card that grades one text; absent from an older bridge.
  wave: z
    .object({
      n: n.min(2).max(REVIEW_WAVE_MAX),
      members: z
        .array(
          z.object({
            key: textKey,
            at: iso,
            p: p01,
            stratum: z.string().max(32),
            text: z.string().max(600), // the tail
            text_chars: n,
          })
        )
        .max(REVIEW_WAVE_MAX - 1),
    })
    .nullable()
    .optional(),
  // A `challenger` card: what the @challenger answered where the served model answered the other way. AGREE on a
  // live card sides with the served model, FLIP with the challenger. Null on every other card; absent from an older bridge.
  challenger: z.object(challengerBlock).nullable().optional(),
});
/** κ's 95% interval and how many pairs ±0.15 needs (how it is drawn: owned by plugins/laya/laya_bridge/agreement.py —
 *  see the note there). Null below `kappa_min_pairs` or while κ is undefined; absent from a bridge older than it. */
const kappaIntervalSchema = z.object({
  lo: z.number().min(-1).max(1),
  hi: z.number().min(-1).max(1),
  /** κ's distance to the FARTHER of lo and hi, so `κ ± half_width` never claims a floor above lo (agreement.py owns why). */
  half_width: p01,
  level: p01,
  target_half_width: p01,
  pairs_for_target: n,
});
/** A precision or recall at the cut: `n` is its denominator; value and interval are null below the minimum. */
const rateAtCutSchema = z.object({ n, value: p01.nullable(), lo: p01.nullable(), hi: p01.nullable() });
/** Where a SHOWN ⚠️ landed (`review_data.py` LANDING_SPLIT), and the row any verdict landed on (AUDIENCES): what the
 *  live sample's precision and recall split on (why: owned by plugins/laya/laya_bridge/review_data.py
 *  `live_rates` — see the note there). */
export const REVIEW_LANDING_SPLIT = ['held', 'new', 'repeat', 'retired', 'unknown'] as const;
export const REVIEW_FREE_CLASSES = ['new', 'repeat'] as const;
export const REVIEW_AUDIENCES = ['free', 'background', 'children', 'unknown'] as const;
/** A precision over the shown ⚠️ of the live sample: `pool` is how many there were to grade, `n` how many were. */
const pooledRateSchema = rateAtCutSchema.extend({ pool: n });
const agreementSchema = z.object({
  n,
  kappa,
  agree_rate: p01.nullable(),
  basis: z.string().max(40),
  kappa_ci: kappaIntervalSchema.nullable().optional(),
  // operator_vs_laya only: Laya's flag at its cut against the operator
  at_cut: z.object({ precision: rateAtCutSchema, recall: rateAtCutSchema }).optional(),
});
export const reviewStatsSchema = z.object({
  surface: layaReviewSurfaceSchema,
  label: z.string().max(40),
  question: z.string().max(400),
  to_review: n,
  backlog: n,
  reviewed: n,
  agree: n,
  flip: n,
  skip: n,
  // `open`: the stratum's answers that recorded OPEN (its OPEN yield); absent from an older bridge.
  by_stratum: z.record(z.string(), z.object({ reviewed: n, remaining: n, open: n.optional() })),
  kappa_min_pairs: n,
  operator_vs_sonnet: agreementSchema,
  operator_vs_laya: agreementSchema,
  // `value` is Laya's precision on the ⚠️ the rack SHOWED, from the operator's grades of the live sample (`live_rates`).
  // `shown` / `dismissed` are the window's reaction counts, telemetry only. The rest is absent from an older bridge,
  // whose `value` was `1 − dismissed / shown`.
  precision: z.object({
    window_days: n,
    shown: n,
    dismissed: n,
    value: p01.nullable(),
    basis: z.string().max(40).optional(),
    min_graded: n.optional(),
    pool: n.optional(),
    n: n.optional(),
    lo: p01.nullable().optional(),
    hi: p01.nullable().optional(),
    free: pooledRateSchema.optional(),
    by_class: z.record(z.string(), pooledRateSchema).optional(),
  }),
  // `by_audience`: recall straight off the live sample, per row the verdict landed on; absent from an older bridge.
  recall_est: z.object({ n_flagged: n, n_unflagged: n, value: p01.nullable(), by_audience: z.record(z.string(), rateAtCutSchema).optional() }),
  // `reserve`: the clean rows the label budget holds back from training and from golden, so the ones still clean after
  // the next retrain (plugins/laya `label_budget.py` owns the rule). Absent from an older bridge.
  sweep_ready: z.object({
    rows: n,
    open: n,
    done: n,
    ready: z.boolean(),
    reason: z.string().max(160).nullable(),
    reserve: z.object({ rows: n, open: n, done: n }).optional(),
  }),
  // The class a sweep is shortest of clean labels, which the `uncertain` cards lean toward; null once both have enough.
  // Absent from an older bridge.
  deficit: z.enum(['open', 'done']).nullable().optional(),
  // The four-way grade (`review_data.py` `three_way` owns the rule; the field's name predates READ): live labels per
  // class, the floor each class needs, the class shortest of it (`deficit`: what the challenger test waits on) and the
  // half the live cards lean toward (`lean`). `read` is absent from an older bridge. Null on a surface without `needs`;
  // absent from an older bridge.
  three_way: z
    .object({
      done: n,
      read: n.optional(),
      running: n,
      you: n,
      min: n,
      deficit: z.enum(REVIEW_GRADES).nullable(),
      lean: reviewNeedsSchema.nullable(),
    })
    .nullable()
    .optional(),
  // The blind retest (`review_data.py` `retest_due`, `intra_rater`): how many labels are due one, and the operator's
  // agreement with their own earlier label: `open` on OPEN against DONE, `needs` per OPEN half. Absent from an older bridge.
  retest: z
    .object({ percent: n, after_days: n, due: n, pairs: n, open: agreementSchema, needs: z.record(z.string(), agreementSchema) })
    .optional(),
  // Which side the operator took on the @challenger's graded disagreements (`review_data.py` `challenger_sides` owns
  // what counts): what PROMOTE CHALLENGER reads. Null without a @challenger; absent from an older bridge.
  challenger: z.object({ name: basename, graded: n, with_challenger: n, remaining: n }).nullable().optional(),
  // The daily phone door: cards waiting that the phone's RECENT list can grade, and how many make a push worth one
  // (`review_data.py` DOOR_STRATA / DOOR_MIN; sent by plugins/phone-link). Absent from an older bridge.
  door: z.object({ waiting: n, min: n }).optional(),
  last_answer_id: z.string().regex(REVIEW_ANSWER_ID).nullable(),
  last_answer_at: iso.nullable(),
  last_answer_wave: z.boolean().optional(), // that answer was one of a wave's; absent from an older bridge
});
/** A blind retest card (`review_data.py` `retest_card` owns what it may hold): the text of something the operator already
 *  graded, and the answer id it re-tests. It has no reference, no score and no stratum, by design. Answer it through
 *  `/review/retest`. */
export const reviewRetestCardSchema = z.object({
  item_id: z.string().max(200), // `${surface}:retest:${key}`
  key: z.string().regex(REVIEW_KEY),
  surface: layaReviewSurfaceSchema,
  of: z.string().regex(REVIEW_ANSWER_ID),
  text: z.string().min(1).max(4_000), // the tail, as a card keeps it
  text_chars: n,
  truncated: z.boolean(),
  at: iso.nullable(), // when the turn ended, for the conversation round it; null without a live verdict
  needs: z.array(reviewNeedsSchema).max(REVIEW_NEEDS.length), // the halves this surface takes; [] = OPEN or DONE only
});
export const reviewQueueSchema = z.object({
  ...envelope,
  surfaces: z.array(reviewStatsSchema).max(20),
  surface: layaReviewSurfaceSchema.nullable(),
  cards: z.array(reviewCardSchema).max(50),
  // The blind retests this page deals, before its cards; empty on a narrowed deal, absent from an older bridge.
  retests: z.array(reviewRetestCardSchema).max(REVIEW_RETEST_PAGE_MAX).optional(),
  sources: z.record(z.string(), z.string().max(16)), // ok | missing | too_large | refused
  generated_at: iso,
});
export const reviewItemResultSchema = z.object({ ...envelope, card: reviewCardSchema.nullable() });
export const reviewExampleRequestSchema = z.strictObject({
  surface: layaReviewSurfaceSchema,
  key: z.string().regex(REVIEW_KEY),
  great_example: z.boolean(),
});
export const reviewExampleResultSchema = z.object({
  ...envelope,
  key: z.string().regex(REVIEW_KEY),
  great_example: z.boolean(),
});
export type ReviewExampleRequest = z.infer<typeof reviewExampleRequestSchema>;
export const reviewAnswerRequestSchema = z.strictObject({
  surface: layaReviewSurfaceSchema,
  key: z.string().regex(REVIEW_KEY),
  answer: reviewAnswerSchema,
  reference_open: z.boolean(),
  needs: reviewNeedsSchema.optional(), // on its own side of the open this answer records (`needs_side_ok`)
  // A wave card's other keys, exactly as the card listed them: each records the verdict this answer means on `key`.
  wave: z.array(textKey).max(REVIEW_WAVE_MAX - 1).optional(),
});
/** A blind retest's answer: `open` true or false (with `needs` on its side, where the surface takes it), or null for
 *  "cannot say". `of` is the card's, so a label replaced since the card was dealt is refused (`review.py` `retest`). */
export const reviewRetestRequestSchema = z
  .strictObject({
    surface: layaReviewSurfaceSchema,
    key: z.string().regex(REVIEW_KEY),
    of: z.string().regex(REVIEW_ANSWER_ID),
    open: z.boolean().nullable(),
    needs: reviewNeedsSchema.optional(), // on its own side of open (`needs_side_ok`)
  })
  .refine((body) => needs_side_ok(body.needs, body.open), { path: ['needs'], message: NEEDS_SIDE });
/** The agent rack's grade of one live verdict, from the ⚠️'s own menu: ✗ NOT OPEN is `open: false`, ✓ STILL OPEN
 *  `open: true`. Never an HTTP body: tldrack sends it to the plugin in process (`review.py` `rack` owns what
 *  it records, `plugin.py` why it has no HTTP door). The answer comes back as `reviewResultSchema`. */
export const reviewRackRequestSchema = z.strictObject({ decision_id: z.string().regex(LAYA_DECISION_ID), open: z.boolean() });
/** `wave: true` takes back every answer the same press wrote; without it, only the one named. */
export const reviewUndoRequestSchema = z.strictObject({
  surface: layaReviewSurfaceSchema,
  answer_id: z.string().regex(REVIEW_ANSWER_ID),
  wave: z.boolean().optional(),
});
const answerRefSchema = z.object({
  answer_id: z.string().regex(REVIEW_ANSWER_ID),
  key: z.string().regex(REVIEW_KEY),
  answer: reviewAnswerSchema,
  open: z.boolean().nullable(),
  needs: reviewNeedsSchema.nullable().optional(), // absent from a bridge older than the three-way grade
  wave_id: z.string().regex(REVIEW_WAVE_ID).nullable().optional(), // the press, when it graded a wave
});
/** answer: success → the new answer (a wave's lead), card null; already_answered → the live answer;
 *          reference_changed / wave_changed → card = fresh item, nothing recorded.
 *  undo:   success → answer = the undone one, card = the card back: its wave's lead, whole, when it rides one
 *          (`review_data.py` holding_card). A refused write records nothing, a wave press included. */
export const reviewResultSchema = z.object({
  ...envelope,
  answer: answerRefSchema.nullable(),
  card: reviewCardSchema.nullable(),
  stats: reviewStatsSchema.nullable(),
});

const retestRefSchema = z.object({
  answer_id: z.string().regex(REVIEW_ANSWER_ID), // the retest row's own id: what UNDO names
  key: z.string().regex(REVIEW_KEY),
  of: z.string().regex(REVIEW_ANSWER_ID),
  open: z.boolean().nullable(),
  needs: reviewNeedsSchema.nullable(),
});
/** retest: success → the new retest row's ref; a refusal (`retest_stale`, `already_retested`, `not_due`) records nothing. */
export const reviewRetestResultSchema = z.object({ ...envelope, retest: retestRefSchema.nullable(), stats: reviewStatsSchema.nullable() });

// ── the RECENT list ──────────────────────────────────────────────────────────

/** One live verdict, newest first. `mark` is what the rack drew (the decision's own cuts); `band` is where p falls at
 *  today's done cut, which is what RIGHT and WRONG are graded against. Answer it through `/review/answer` with `key` and
 *  `reference_open` (null: no current card holds the text, so it cannot be answered). */
export const reviewRecentRowSchema = z.object({
  great_example: z.boolean().optional(),
  key: z.string().regex(REVIEW_KEY),
  decision_id: z.string().regex(LAYA_DECISION_ID),
  at: iso,
  subject: z.string().max(128).nullable(), // the rack session
  // The conversation the verdict judged (`LAYA_LEDGER_CONVERSATION`), recorded since 2026-11-07: null on an older
  // verdict, absent from an older bridge. The page bands rows by it before the session ledger has indexed the turn.
  conversation: z.string().regex(LAYA_LEDGER_CONVERSATION).nullable().optional(),
  p: p01,
  threshold: p01,
  done_threshold: p01.nullable(), // null: the decision predates the ✅ band
  mark: reviewBandSchema,
  band: reviewBandSchema,
  // The cut p sits nearest, and p minus it: −0.02 on `open` is 0.02 short of a ⚠️ (`review_data.py` `edge_of` owns
  // the rule). Absent from an older bridge.
  edge: z.object({ cut: z.enum(['open', 'done']), delta: z.number().min(-1).max(1) }).optional(),
  model: z.string().max(80),
  checkpoint: basename.nullable(),
  text: z.string().max(4_000), // the tail, as a card keeps it
  text_chars: n,
  stratum: z.string().max(32).nullable(),
  reference_open: z.boolean().nullable(),
  operator: answerRefSchema.nullable(), // the live answer on this text, if any
});
const bandTallySchema = z.object({ n, reviewed: n, right: n.nullable(), open: n });
export const reviewRecentSummarySchema = z.object({
  n,
  skipped: n,
  // The answers' halves: the OPEN split, and the DONE answers that owed a read (`read`, absent from an older bridge).
  // Absent from an older bridge.
  needs: z.object({ you: n, running: n, read: n.optional() }).optional(),
  done_at: p01, // the done cut a row with no recorded one is graded at
  bands: z.object({ done: bandTallySchema, reply: bandTallySchema, open: bandTallySchema }),
  // How the page was cut, and from how many verdicts (every live one, or every unanswered one with `todo`). Absent
  // from an older bridge, which only ever served the newest.
  order: z.enum(REVIEW_RECENT_ORDERS).optional(),
  todo: z.boolean().optional(),
  pool: n.optional(),
  // How many of `pool` came before this page (`?offset=`); absent from an older bridge, which only served the first.
  offset: z.number().int().nonnegative().optional(),
  // Where the next page starts (`?after=`), null once nothing follows; absent from a bridge that predates it.
  next: z.string().regex(REVIEW_RECENT_CURSOR).nullable().optional(),
  // The one rack session the page was narrowed to (`?subject=`), echoed so the page knows the bridge honoured it.
  subject: z.string().max(128).optional(),
});
export const reviewRecentSchema = z.object({
  ...envelope,
  surface: layaReviewSurfaceSchema.nullable(),
  rows: z.array(reviewRecentRowSchema).max(REVIEW_RECENT_MAX),
  summary: reviewRecentSummarySchema.nullable(),
  stats: reviewStatsSchema.nullable(),
  sources: z.record(z.string(), z.string().max(16)),
  generated_at: iso,
});

// ── the operator's answer log ─────────────────────────────────────────────────

/** What every row of the operator log carries, whatever its op. `answer_id` is the row's own id on an answer and a
 *  retest, and the id being taken back on an undo. */
const operatorRowHead = {
  v: z.literal(1),
  answer_id: z.string().regex(REVIEW_ANSWER_ID),
  at: z.iso.datetime(),
  by: z.literal('operator'),
  surface: layaReviewSurfaceSchema,
};

/** One line of `<data>/laya/data/<stem>-operator.jsonl`. Python mirror: laya_bridge/review_log.py. */
export const operatorAnswerRowSchema = z.discriminatedUnion('op', [
  z
    .strictObject({
      ...operatorRowHead,
      op: z.literal('answer'),
      key: z.string().regex(REVIEW_KEY),
      stratum: z.string().max(32),
      answer: reviewAnswerSchema,
      open: z.boolean().optional(), // present iff agree/flip: agree → against.open, flip → !against.open
      needs: reviewNeedsSchema.optional(), // on its own side of open (`needs_side_ok`)
      wave_id: z.string().regex(REVIEW_WAVE_ID).optional(), // one press graded a wave: every row it wrote carries this
      challenger: z.strictObject(challengerBlock).optional(), // only on an answer to a `challenger` card: what it showed
      against: z.strictObject({ by: z.string().min(1).max(120), open: z.boolean(), case_id: z.string().max(64).optional() }),
      laya: z
        .strictObject({
          p: p01,
          threshold: p01,
          source: layaSourceSchema,
          checkpoint_path: z.string().max(256).regex(LAYA_DATA_RELATIVE_PATH).nullable(),
        })
        .nullable(),
      decision_id: z.string().regex(LAYA_DECISION_ID).nullable(),
      held_out: z.boolean().nullable(),
    })
    .refine(
      (row) => (row.answer === 'skip' ? row.open === undefined : row.open === (row.answer === 'agree' ? row.against.open : !row.against.open)),
      { path: ['open'], message: 'open is against.open on agree, its negation on flip, and absent on skip' }
    )
    .refine((row) => needs_side_ok(row.needs, row.open), { path: ['needs'], message: NEEDS_SIDE }),
  // A blind second look at a graded text: never a label, never counted (`review_log.py` owns why: RETEST).
  z
    .strictObject({
      ...operatorRowHead, // `answer_id` is its own, so UNDO takes it back like an answer
      op: z.literal('retest'),
      key: z.string().regex(REVIEW_KEY),
      of: z.string().regex(REVIEW_ANSWER_ID), // the answer it re-tests
      open: z.boolean().optional(), // absent: the operator could not say
      needs: reviewNeedsSchema.optional(), // on its own side of open (`needs_side_ok`)
    })
    .refine((row) => needs_side_ok(row.needs, row.open), { path: ['needs'], message: NEEDS_SIDE }),
  // Great examples: owned by plugins/laya/laya_bridge/review_log.py — see the note there.
  z.strictObject({
    ...operatorRowHead,
    op: z.literal('example'),
    key: z.string().regex(REVIEW_KEY),
    great_example: z.boolean(),
    decision_id: z.string().regex(LAYA_DECISION_ID).nullable(),
  }),
  z.strictObject({ ...operatorRowHead, op: z.literal('undo') }),
]);

// ── OPS and the lab ──────────────────────────────────────────────────────────

export const layaLabRequestSchema = z.strictObject({ verb: layaLabVerbSchema, surface: layaReviewSurfaceSchema });
// `queued`: the press's 1-based place when it waits behind a running lab job (null when it started); absent from an
// older bridge, which refused such a press with 409 busy.
export const layaLabResultSchema = z.object({ ...envelope, session: z.string().max(64).nullable(), queued: n.min(1).nullable().optional() });
export const layaRestartResultSchema = z.object({ ...envelope, status: z.unknown() });
const metrics = z.object({ accuracy: z.number(), brier: z.number(), ece: z.number(), loss: z.number() });
const balance = z.object({ cases: n, open: n, done: n });
const thresholdSourceSchema = z.enum(['code', 'swept']);
/** One alias's version in models.json v2 (`ops._version_row`): its folder, its gate tag and its OWN swept cut. */
const registryVersionSchema = z
  .object({
    name: basename,
    registered_at: iso.nullable(),
    gate: gateTagSchema.nullable(),
    cut: z.object({ at: p01, done_at: p01.nullable(), swept_at: iso.nullable() }).nullable(),
  })
  .nullable();
export const layaOpsStateSchema = z.object({
  ...envelope,
  status: z.unknown(), // read by parse_laya_status (laya-local-client.ts): one owner
  // exactly one row per REVIEW_SURFACES entry (english, checkpoint null, when none is registered)
  served: z
    .array(
      z.object({
        name: z.string().max(80),
        surface: layaReviewSurfaceSchema,
        checkpoint: basename.nullable(),
        model_id: z.string().max(140).nullable(),
        threshold: z.object({ at: p01, source: thresholdSourceSchema, swept_at: iso.nullable(), done_at: p01.optional() }),
        eval: z.object({ items: n, before: metrics, after: metrics }).nullable(),
        golden: z.object({ at: iso, cases: n, accuracy: z.number(), kappa, brier: z.number() }).nullable(),
        scores: z.object({ at: iso, rows: n, current: z.boolean() }).nullable(), // current: scored by this checkpoint
      })
    )
    .max(20),
  review: z.array(reviewStatsSchema).max(20),
  flags_7d: z.record(z.string(), z.object({ decisions: n, shown: n, dismissed: n, accepted: n })),
  sweeps: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        at: iso.nullable(),
        rows: n,
        labels: z.string().max(16).nullable(),
        best: p01.nullable(),
        live: p01,
        source: thresholdSourceSchema,
        kappa,
        done_live: p01.optional(), // the agent rack's live ✅ cut; absent from older bridges
        // operator-ECE beside teacher-ECE once a sweep ran on ≥40 operator labels (`lab_files.calibration_view`); absent
        // from older bridges, null before then. Why: owned by lab/laya_lab/metrics.py — see "Calibration".
        calibration: z
          .object({
            rows: n,
            ece: p01.nullable(),
            teacher_rows: n,
            teacher_ece: p01.nullable(),
            temperature: z.number().positive().nullable(),
            ece_refit: p01.nullable(),
          })
          .nullable()
          .optional(),
      })
    )
    .max(20),
  golden: z
    // `reserved`: waiting flips PROMOTE never offers, the label budget's reserve; absent from an older bridge.
    .array(z.object({ surface: layaReviewSurfaceSchema, suite: z.string().max(64), public: balance, private: balance, candidates: n, reserved: n.optional() }))
    .max(20),
  // The newest `laya-lab compare` per surface, names and numbers only (`lab_files.compare_view`); absent from an older
  // bridge, empty before the first compare.
  compare: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        suite: z.string().max(64),
        at: iso,
        heldout_skipped: z.boolean(),
        sets: z
          .array(
            z.object({
              set: z.enum(['golden', 'heldout']),
              rows: n,
              open: n,
              done: n,
              voters: z
                .array(
                  z.object({
                    name: z.string().max(85),
                    served: z.boolean(), // the surface's served Laya model
                    auc: p01.nullable(),
                    brier: z.number().nullable(),
                    ece: z.number().nullable(),
                    accuracy: p01.nullable(), // at the voter's own live cut
                    p50_ms: z.number().nonnegative().nullable(),
                  })
                )
                .max(8),
            })
          )
          .max(2),
      })
    )
    .max(20)
    .optional(),
  // The model registry per surface (`ops.registry_row`, models.json v2): each alias with its own swept cut and gate tag,
  // the challenger's shadow so far, retention, and the last nightly pack. Absent from an older bridge. The wire for the
  // LAYA row's CHALLENGER line and PROMOTE CHALLENGER / REJECT.
  registry: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        version: z.number().int().nullable(), // 1: a v1 file the nightly pack migrates; null: none or unreadable
        legacy: z.boolean(),
        champion: registryVersionSchema,
        challenger: registryVersionSchema,
        previous: registryVersionSchema, // the champion a promotion replaced: the one-night rollback target
        needs_you: z.object({ state: z.enum(['ran','reused','skipped','failed']), ready: z.boolean(),
          shadow_days: n, shadow_rows: n, counts: z.object({ done: n, running: n, you: n }),
          kappa, pairs: n, precision: p01.nullable(), recall: p01.nullable(),
        }).nullable().optional(),
        promotion: z.object({
          name: basename, token: z.string().regex(/^[a-f0-9]{64}$/), ready: z.boolean(),
          requirements: z.object({ gate: z.boolean(), shadow: z.boolean(), operator: z.boolean(), reserve: z.boolean() }),
          checks: z.array(z.object({ name: z.string(), state: z.string(), p: p01.nullable() })).max(6),
          shadow_days: n, shadow_agreement: p01.nullable(), graded: n, with_challenger: n,
          cut: p01.nullable(), done_cut: p01.nullable(), reserve_rows: n,
          prune_paths: z.array(z.string()).max(200), prune_bytes: n,
        }).nullable().optional(),
        rejected: n,
        prunable: z.object({ checkpoints: n, bytes: n }),
        shadow: z
          .object({
            rows: n,
            days: n,
            agree: p01.nullable(), // same mark as the served model, each at its own cuts
            disagree: n,
            since: iso.nullable(),
            last: iso.nullable(),
          })
          .nullable(),
        nightly: z
          .object({
            at: iso.nullable(),
            verdict: z.enum(['PASS', 'FAIL', 'INCONCLUSIVE']),
            steps: z.record(z.string().max(16), z.enum(['ran', 'reused', 'skipped', 'failed'])),
          })
          .nullable(),
      })
    )
    .max(20)
    .optional(),
  // The newest `laya-lab latency` read per surface (`lab_files.latency_view`): how long the ledger's decisions took,
  // cold against warm, and the token overflow of the states Laya is asked. Absent from an older bridge, empty before
  // the first read.
  latency: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        at: iso,
        days: n.nullable(), // the window read; null: everything the ledger keeps
        decisions: n,
        p50_ms: z.number().nonnegative().nullable(),
        p90_ms: z.number().nonnegative().nullable(),
        le_100: p01.nullable(), // the share answered within 100 ms
        cold: z.object({ n, p50_ms: z.number().nonnegative().nullable() }), // a model's first answer since laya-serve started
        warm: z.object({ n, p50_ms: z.number().nonnegative().nullable() }),
        truncation: z.object({ states: n, overflowed: n }).nullable(), // null until a read measured the window
      })
    )
    .max(20)
    .optional(),
  // STABLE κ per surface (`lab_files.stability_summary` owns the rule): κ between consecutive checkpoints' verdicts on
  // the stop set, the window's pairs only. Absent from an older bridge, empty before the first stability pass.
  stability: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        device: z.enum(['mps', 'cuda', 'cpu']),
        states: n,
        versions: n,
        pairs: z.array(z.object({ from: basename, to: basename, n, kappa, agree: p01.nullable(), flips: n })).max(3),
        kappa_average: kappa,
        window: n,
        stable: z.boolean(),
        last: iso,
      })
    )
    .max(20)
    .optional(),
  // The gated retrain's step journal per surface (`lab_files.retrain_view`): the cycle in flight or the last one
  // closed. Absent from an older bridge, empty before the first retrain. What a step is and when a retrain is owed:
  // owned by lab/laya_lab/retrain.py — see the note there.
  retrain: z
    .array(
      z.object({
        surface: layaReviewSurfaceSchema,
        id: z.string().max(40),
        state: z.enum(LAYA_RETRAIN_STATES),
        started_at: iso.nullable(),
        updated_at: iso.nullable(),
        step: z.enum(LAYA_RETRAIN_STEPS).nullable(), // the first step not done or skipped; null once the cycle closed
        steps: z.array(z.object({ name: z.enum(LAYA_RETRAIN_STEPS), state: z.enum(LAYA_RETRAIN_STEP_STATES) })).max(8),
        error: z.string().max(64).nullable(), // a failed step's error type; its message stays on the Mac
        checkpoint: basename.nullable(),
        teacher: z.string().max(64).nullable(), // `sonnet`, or the local teacher's tag once it cleared its bar
        label_calls: n.nullable(),
        rows: n.nullable(),
        operator_rows: n.nullable(),
        reserved: n.nullable(), // operator labels the label budget held out of this training set
        train_rows: n.nullable(),
        gate: gateTagSchema.nullable(),
        sweep_best: p01.nullable(), // the RE-SWEEP dry run's cut for the challenger, on rows it never trained on
        sweep_rows: n.nullable(),
      })
    )
    .max(20)
    .optional(),
  // always one per verb × review surface
  jobs: z
    .array(
      z.object({
        verb: layaLabVerbSchema,
        surface: layaReviewSurfaceSchema,
        state: z.enum(LAYA_LAB_JOB_STATES),
        exit_code: z.number().int().nullable(),
        position: n.min(1).nullable().optional(), // 1-based place in the lab queue while `queued`; absent from an older bridge
        session: z.string().max(64),
        cli: z.string().max(300),
      })
    )
    .max(120),
  lab: z.object({ ok: z.boolean(), hint: z.string().max(160).nullable() }),
});

export type LayaReviewSurface = z.infer<typeof layaReviewSurfaceSchema>;
export type ReviewStratum = (typeof REVIEW_STRATA)[number];
export type ReviewBand = z.infer<typeof reviewBandSchema>;
export type ReviewRecentRow = z.infer<typeof reviewRecentRowSchema>;
export type ReviewRecentSummary = z.infer<typeof reviewRecentSummarySchema>;
export type ReviewRecent = z.infer<typeof reviewRecentSchema>;
export type ReviewRecentOrder = (typeof REVIEW_RECENT_ORDERS)[number];
export type ReviewAnswer = z.infer<typeof reviewAnswerSchema>;
export type ReviewNeeds = z.infer<typeof reviewNeedsSchema>;
export type LayaLabVerb = z.infer<typeof layaLabVerbSchema>;
export type LayaLabJobState = (typeof LAYA_LAB_JOB_STATES)[number];
export type ReviewVerdict = z.infer<typeof reviewVerdictSchema>;
export type ReviewLaya = z.infer<typeof reviewLayaSchema>;
export type ReviewCard = z.infer<typeof reviewCardSchema>;
export type ReviewStats = z.infer<typeof reviewStatsSchema>;
export type ReviewAgreement = z.infer<typeof agreementSchema>;
export type ReviewKappaInterval = z.infer<typeof kappaIntervalSchema>;
export type ReviewRateAtCut = z.infer<typeof rateAtCutSchema>;
export type ReviewPooledRate = z.infer<typeof pooledRateSchema>;
export type ReviewQueue = z.infer<typeof reviewQueueSchema>;
export type ReviewItemResult = z.infer<typeof reviewItemResultSchema>;
export type ReviewAnswerRequest = z.infer<typeof reviewAnswerRequestSchema>;
export type ReviewUndoRequest = z.infer<typeof reviewUndoRequestSchema>;
export type ReviewRackRequest = z.infer<typeof reviewRackRequestSchema>;
export type ReviewResult = z.infer<typeof reviewResultSchema>;
export type ReviewRetestCard = z.infer<typeof reviewRetestCardSchema>;
export type ReviewRetestRequest = z.infer<typeof reviewRetestRequestSchema>;
export type ReviewRetestResult = z.infer<typeof reviewRetestResultSchema>;
export type OperatorAnswerRow = z.infer<typeof operatorAnswerRowSchema>;
export type LayaLabRequest = z.infer<typeof layaLabRequestSchema>;
export type LayaLabResult = z.infer<typeof layaLabResultSchema>;
export type LayaRestartResult = z.infer<typeof layaRestartResultSchema>;
export type LayaOpsState = z.infer<typeof layaOpsStateSchema>;

/** `owner/name` as GitHub spells them; the same shape as plugins/laya/laya_bridge/card_files.py `REPO_SHAPE`. */
export const GITHUB_REPO = /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/;
/** Where a card's pull request goes unless the operator sets `plugins.laya.card_repo`. */
export const DEFAULT_CARD_REPO = 'voidcraft-world/tldrack';

/** Exact outgoing verifier files; the digest binds the preview to the armed publication. */
export const layaVerifierCardSchema = z.object({
  v: z.literal(1), repository: z.string().regex(GITHUB_REPO), at: iso,
  pairs: n, ready: z.boolean(), digest: z.string().regex(/^[a-f0-9]{64}$/),
  files: z.object({ 'VERIFIER.md': z.string().max(100000), 'golden/agent-open-work.jsonl': z.string().max(100000),
    'REPRODUCE.md': z.string().max(100000), 'MODEL_CARD.md': z.string().max(100000).optional() }),
});
export const layaVerifierResultSchema = z.object({ v: z.literal(1), ok: z.boolean(), message: z.string(), card: layaVerifierCardSchema.nullable() });
/** `url` is empty until a pull request exists, and then it is one of the card's OWN repository. */
export const layaVerifierPublishSchema = z.object({
  v: z.literal(1), ok: z.boolean(), message: z.string(),
  url: z.union([z.literal(''), z.string().regex(/^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+\/pull\/\d+$/)]),
  repository: z.string().regex(GITHUB_REPO).optional(),
}).refine((reply) => !reply.url || !reply.repository || reply.url.startsWith(`https://github.com/${reply.repository}/pull/`),
  { message: 'a pull request of another repository', path: ['url'] });
export type LayaVerifierCard = z.infer<typeof layaVerifierCardSchema>;
