/**
 * One eval results file, `bench/results/<date>.json`: every voter's numbers on every set, measured under ONE
 * protocol. Writer: `laya-lab bench` (lab/). Reader: the EVALS page (ui/src/evals/).
 *
 * **Aggregates only.** A set's rows never leave the machine that scored them: no row key, no row id, no text. Every
 * object is strict, so a writer cannot slip a `rows` list in beside the numbers, and the only free text is what the
 * author of a run writes about it (the protocol's description, a voter's label, the command that re-runs it).
 *
 * **One protocol per file.** Two numbers are comparable only under the same protocol: the question, its version, the
 * set and its split, the interval method. A figure from another protocol is a footnote, never a headline, so a file
 * carries exactly one `protocol.id` and a page compares files only when the ids match.
 */

import { z } from 'zod';

import { DECIDE_QUESTION_NAME, decideSurfaceSchema } from './decide.ts';

export const BENCH_RESULTS_VERSION = 1;

/** A protocol id: lower-case, dotted or dashed, e.g. `open-work.q2.heldout-105`. */
export const BENCH_PROTOCOL_ID = /^[a-z0-9][a-z0-9.-]{0,63}$/;
/** A voter's id: the name its row carries on every set (`laya-ft`, `laya-base`, `tfidf-floor`, `mean-laya-mica`). */
export const BENCH_VOTER_NAME = /^[a-z0-9][a-z0-9.+_-]{0,63}$/;

/**
 * The sets a bench scores. `golden` is public: its cases are in this repository and anyone can re-run them.
 * `heldout` is the operator's own graded turn ends, never trained on: only these aggregates of it ever leave the
 * machine.
 */
export const BENCH_SETS = ['golden', 'heldout'] as const;
export type BenchSet = (typeof BENCH_SETS)[number];
export const BENCH_SET_PUBLIC: Readonly<Record<BenchSet, boolean>> = { golden: true, heldout: false };

/** What a voter is: what the EVALS roster groups by, and how "what your labels buy" finds base and fine-tune. */
export const BENCH_VOTER_KINDS = ['fine_tune', 'base', 'zero_shot', 'floor', 'ensemble'] as const;

/** Where a voter's inference ran when it was timed. */
export const BENCH_RUNTIMES = ['mps', 'cuda', 'cpu', 'mlx', 'ollama', 'llama.cpp', 'none'] as const;

/** The most points a ROC curve carries: one per distinct score is plenty for the sets a bench scores. */
export const BENCH_ROC_MAX = 512;
/** The most reliability bins: ten equal bins, or edges either side of each live cut. */
export const BENCH_BINS_MAX = 20;
/** The most voters one file scores. */
export const BENCH_VOTERS_MAX = 16;

const p01 = z.number().finite().min(0).max(1);
const count = z.number().int().nonnegative();
const ms = z.number().finite().nonnegative();
const voterName = z.string().regex(BENCH_VOTER_NAME);

/**
 * An estimate and its interval (the protocol's `interval` says which level and how drawn). `lo ≤ value ≤ hi`: an
 * interval that does not hold its own estimate is a writer bug, refused here rather than drawn.
 */
export const benchEstimateSchema = z
  .strictObject({ value: z.number().finite(), lo: z.number().finite(), hi: z.number().finite() })
  .refine((e) => e.lo <= e.value && e.value <= e.hi, { message: 'an interval holds its estimate: lo ≤ value ≤ hi' });
export type BenchEstimate = z.infer<typeof benchEstimateSchema>;

/** The same, on a [0, 1] metric (AUC, Brier). */
const unitEstimateSchema = benchEstimateSchema.refine((e) => e.lo >= 0 && e.hi <= 1, { message: 'a [0, 1] metric stays in [0, 1]' });

export const benchProtocolSchema = z.strictObject({
  id: z.string().regex(BENCH_PROTOCOL_ID),
  /** The surface the question is asked on; its question's name and version. */
  surface: decideSurfaceSchema,
  question: z.string().regex(DECIDE_QUESTION_NAME),
  question_version: z.number().int().positive(),
  /** What `p` is the probability of: the positive class every rate below counts. */
  positive: z.literal('open'),
  /** One sentence for the page: what was measured, on what. It names every challenger and retrain
   *  (`lab/laya_lab/bench.py`), so it grows with the voters: a cap of 400 refused the 2026-11-14 run. */
  description: z.string().min(1).max(1000),
  interval: z.strictObject({
    level: p01,
    method: z.literal('bootstrap'),
    resamples: z.number().int().min(100).max(100_000),
    seed: z.number().int().nonnegative(),
  }),
  /** How much worse one false ⚠️ is than one missed open turn: `cost = false_warning · fp + fn`. */
  costs: z.strictObject({ false_warning: z.number().finite().positive() }),
});
export type BenchProtocol = z.infer<typeof benchProtocolSchema>;

/** One voter, once per file: the EVALS roster's row. */
export const benchVoterSchema = z.strictObject({
  name: voterName,
  /** Mono label for the page. */
  label: z.string().min(1).max(40),
  kind: z.enum(BENCH_VOTER_KINDS),
  /** The voter a fine-tune was trained from, or an ensemble's first member: what "what your labels buy" pairs. */
  of: voterName.nullable(),
  /** Operator labels a fine-tune trained on; null for every other kind. */
  labels: count.nullable(),
  /** Parameters in millions; null for a floor or an ensemble. */
  params_m: z.number().finite().positive().nullable(),
  runtime: z.enum(BENCH_RUNTIMES),
  /** SPDX id of the weights' licence (`Apache-2.0`, `MIT`), or null for a voter with no weights of its own. */
  licence: z.string().regex(/^[A-Za-z0-9.+-]{1,40}$/).nullable(),
  /** The model the rack serves on this surface: drawn bold. */
  served: z.boolean(),
});
export type BenchVoter = z.infer<typeof benchVoterSchema>;

/** One ROC point: at `threshold`, the share of open turns flagged (`tpr`) and of done turns flagged (`fpr`). */
export const benchRocPointSchema = z.strictObject({ threshold: p01, fpr: p01, tpr: p01 });
export type BenchRocPoint = z.infer<typeof benchRocPointSchema>;

/** One reliability bin, `[lo, hi)` (the last one closed): its count, its mean p and the open rate it came true at. */
export const benchBinSchema = z
  .strictObject({ lo: p01, hi: p01, n: count, p: p01.nullable(), rate: p01.nullable() })
  .refine((bin) => bin.lo < bin.hi, { message: 'a bin has width' })
  .refine((bin) => (bin.n === 0) === (bin.p === null && bin.rate === null), { message: 'an empty bin has no p or rate' });
export type BenchBin = z.infer<typeof benchBinSchema>;

/** The operating point: the cut the rack draws ⚠️ at, and what it costs on this set. */
export const benchOperatingPointSchema = z.strictObject({
  threshold: p01,
  /** `live`: the cut the voter serves with. `swept`: the cut this run found cheapest on this set (in-sample). */
  source: z.enum(['live', 'swept']),
  false_warnings: count,
  missed_open: count,
  cost: benchEstimateSchema,
});
export type BenchOperatingPoint = z.infer<typeof benchOperatingPointSchema>;

/** One voter's numbers on one set. */
export const benchScoreSchema = z.strictObject({
  voter: voterName,
  /** Null when either class is empty, where AUC is undefined. */
  auc: unitEstimateSchema.nullable(),
  brier: unitEstimateSchema,
  ece: z.strictObject({ value: p01, bins: z.number().int().min(2).max(BENCH_BINS_MAX) }),
  /** Ascending fpr, then tpr: (0, 0) at the top threshold to (1, 1) at the bottom. */
  roc: z
    .array(benchRocPointSchema)
    .min(2)
    .max(BENCH_ROC_MAX)
    .refine((points) => points.every((pt, i) => i === 0 || (pt.fpr >= points[i - 1]!.fpr && pt.tpr >= points[i - 1]!.tpr)), {
      message: 'a ROC curve never turns back',
    })
    .refine((points) => points.every((pt, i) => i === 0 || pt.threshold <= points[i - 1]!.threshold), {
      message: 'thresholds fall along the curve',
    }),
  /** Contiguous bins covering [0, 1]; their counts sum to the set's rows. */
  reliability: z
    .array(benchBinSchema)
    .min(2)
    .max(BENCH_BINS_MAX)
    .refine((bins) => bins[0]!.lo === 0 && bins.at(-1)!.hi === 1 && bins.every((bin, i) => i === 0 || bin.lo === bins[i - 1]!.hi), {
      message: 'bins cover [0, 1] without a gap',
    }),
  operating: benchOperatingPointSchema,
  /** Per-call latency on this set, warm; null for a voter with none of its own (an ensemble). */
  latency: z.strictObject({ n: count, p50_ms: ms, p90_ms: ms }).refine((l) => l.p50_ms <= l.p90_ms, { message: 'p50 ≤ p90' }).nullable(),
});
export type BenchScore = z.infer<typeof benchScoreSchema>;

/** One set: how many rows of each class, and every voter's numbers on it. */
export const benchSetResultSchema = z
  .strictObject({
    set: z.enum(BENCH_SETS),
    n: z.strictObject({ open: count, done: count }),
    scores: z.array(benchScoreSchema).min(1).max(BENCH_VOTERS_MAX),
  })
  .superRefine((result, ctx) => {
    const rows = result.n.open + result.n.done;
    const seen = new Set<string>();
    result.scores.forEach((score, i) => {
      if (seen.has(score.voter)) ctx.addIssue({ code: 'custom', path: ['scores', i, 'voter'], message: 'one score per voter' });
      seen.add(score.voter);
      if (score.reliability.reduce((sum, bin) => sum + bin.n, 0) !== rows) {
        ctx.addIssue({ code: 'custom', path: ['scores', i, 'reliability'], message: 'the bins count every row once' });
      }
      if ((score.auc === null) !== (result.n.open === 0 || result.n.done === 0)) {
        ctx.addIssue({ code: 'custom', path: ['scores', i, 'auc'], message: 'AUC is null exactly when a class is empty' });
      }
      const { false_warnings, missed_open } = score.operating;
      if (false_warnings > result.n.done || missed_open > result.n.open) {
        ctx.addIssue({ code: 'custom', path: ['scores', i, 'operating'], message: 'more errors than rows of that class' });
      }
    });
  });
export type BenchSetResult = z.infer<typeof benchSetResultSchema>;

/**
 * What the rack's live use looked like over the window, from the decision ledger: turn ends judged, the verdict each
 * drew, how many ⚠️ reached the operator, live latency and the next-step taken rate. Counts only.
 */
export const benchRackSchema = z.strictObject({
  from: z.iso.date(),
  to: z.iso.date(),
  days: z.number().int().positive(),
  judged: count,
  verdicts: z.strictObject({ done: count, reply: count, open: count }),
  /** ⚠️ the rack showed: what reached the operator. */
  reached: count,
  latency: z.strictObject({ n: count, p50_ms: ms, p90_ms: ms }).nullable(),
  next_step: z.strictObject({ suggested: count, taken: count }).nullable(),
});
export type BenchRack = z.infer<typeof benchRackSchema>;

/** Legacy (step 6.8): the rack block was `fleet` before 0.1.0; a file written then reads as today's. */
function renamed_rack_block(raw: unknown): unknown {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw) || !('fleet' in raw) || 'rack' in raw) return raw;
  const { fleet, ...rest } = raw as Record<string, unknown>;
  return { ...rest, rack: fleet };
}

const benchResultsFields = z
  .strictObject({
    v: z.literal(BENCH_RESULTS_VERSION),
    /** Present and true only on invented numbers (`fixture.json`): the page says so on every panel. */
    fixture: z.literal(true).optional(),
    protocol: benchProtocolSchema,
    generated_at: z.iso.datetime(),
    /** The commit the bench ran from; null outside a checkout. */
    commit: z.string().regex(/^[0-9a-f]{40}$/).nullable(),
    voters: z.array(benchVoterSchema).min(1).max(BENCH_VOTERS_MAX),
    sets: z.array(benchSetResultSchema).min(1).max(BENCH_SETS.length),
    rack: benchRackSchema.nullable(),
    /** The one command that re-runs the public set, and which set it is. */
    reproduce: z.strictObject({ command: z.string().min(1).max(300), set: z.literal('golden') }),
  })
  .superRefine((file, ctx) => {
    const names = new Set<string>();
    file.voters.forEach((voter, i) => {
      if (names.has(voter.name)) ctx.addIssue({ code: 'custom', path: ['voters', i, 'name'], message: 'voter names are unique' });
      names.add(voter.name);
    });
    file.voters.forEach((voter, i) => {
      if (voter.of !== null && !names.has(voter.of)) ctx.addIssue({ code: 'custom', path: ['voters', i, 'of'], message: 'of names a voter in this file' });
      if ((voter.kind === 'fine_tune') !== (voter.labels !== null)) {
        ctx.addIssue({ code: 'custom', path: ['voters', i, 'labels'], message: 'labels is set on exactly the fine-tunes' });
      }
    });
    if (file.voters.filter((voter) => voter.served).length > 1) ctx.addIssue({ code: 'custom', path: ['voters'], message: 'at most one served voter' });
    const sets = new Set<string>();
    file.sets.forEach((result, i) => {
      if (sets.has(result.set)) ctx.addIssue({ code: 'custom', path: ['sets', i, 'set'], message: 'one result per set' });
      sets.add(result.set);
      result.scores.forEach((score, j) => {
        if (!names.has(score.voter)) ctx.addIssue({ code: 'custom', path: ['sets', i, 'scores', j, 'voter'], message: 'scores name a voter in this file' });
      });
    });
    if (file.rack) {
      const { done, reply, open } = file.rack.verdicts;
      if (done + reply + open !== file.rack.judged) ctx.addIssue({ code: 'custom', path: ['rack', 'verdicts'], message: 'every judged turn drew one verdict' });
      if (file.rack.reached > open) ctx.addIssue({ code: 'custom', path: ['rack', 'reached'], message: 'only an open verdict reaches the operator' });
    }
  });
export const benchResultsSchema = z.preprocess(renamed_rack_block, benchResultsFields);
export type BenchResults = z.infer<typeof benchResultsFields>;

/** The errors behind a ROC point on a set of `n` rows: false ⚠️ and missed open turns. */
export function roc_counts(point: BenchRocPoint, n: { open: number; done: number }): { false_warnings: number; missed_open: number } {
  return { false_warnings: Math.round(point.fpr * n.done), missed_open: Math.round((1 - point.tpr) * n.open) };
}

/** Cost at every ROC point, under the protocol's weight: the EVALS page's cost-against-threshold curve. */
export function cost_curve(
  score: BenchScore,
  n: { open: number; done: number },
  costs: BenchProtocol['costs'],
): { threshold: number; false_warnings: number; missed_open: number; cost: number }[] {
  return score.roc.map((point) => {
    const errors = roc_counts(point, n);
    return { threshold: point.threshold, ...errors, cost: costs.false_warning * errors.false_warnings + errors.missed_open };
  });
}
