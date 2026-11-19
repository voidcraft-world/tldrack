/**
 * `bench/results/fixture.json`: a results file of INVENTED numbers, for building the EVALS page before a real bench
 * has run. No model produced them. Each voter's scores are drawn from a seeded generator, and every number in the file
 * (AUC, Brier, ECE, the ROC curve, the bins, the operating point and their intervals) is computed from those draws, so
 * the fixture is consistent the way a real file is, and a panel that disagrees with itself is the page's bug.
 *
 *     node scripts/bench-fixture.ts            # rewrite the file
 *
 * `tests/bench-results.test.ts` checks the committed file is exactly what this writes: edit this, never the JSON.
 */

import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  BENCH_RESULTS_VERSION,
  benchResultsSchema,
  type BenchBin,
  type BenchEstimate,
  type BenchResults,
  type BenchRocPoint,
  type BenchScore,
  type BenchSet,
  type BenchVoter,
} from '../src/bench-results.ts';

export const FIXTURE_PATH = fileURLToPath(new URL('../../bench/results/fixture.json', import.meta.url));

const SEED = 20261113;
const RESAMPLES = 1000;
const LEVEL = 0.95;
const FALSE_WARNING_COST = 2;
const OPEN_CUT = 0.7;
const DONE_CUT = 0.4;
const ECE_BINS = 10;

/** mulberry32: a small seeded PRNG, so the fixture is the same on every machine. */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function normal(rand: () => number): number {
  const u = Math.max(rand(), 1e-12);
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rand());
}

const sigmoid = (x: number) => 1 / (1 + Math.exp(-x));
const round = (x: number, places = 4) => Math.round(x * 10 ** places) / 10 ** places;

/** One row: the voter's p and whether the turn was open. */
type Row = { p: number; open: boolean };

function auc(rows: Row[]): number {
  const open = rows.filter((r) => r.open).map((r) => r.p);
  const done = rows.filter((r) => !r.open).map((r) => r.p);
  let wins = 0;
  for (const o of open) for (const d of done) wins += o > d ? 1 : o === d ? 0.5 : 0;
  return wins / (open.length * done.length);
}

const brier = (rows: Row[]) => rows.reduce((sum, r) => sum + (r.p - (r.open ? 1 : 0)) ** 2, 0) / rows.length;

function errors_at(rows: Row[], cut: number): { false_warnings: number; missed_open: number } {
  return {
    false_warnings: rows.filter((r) => !r.open && r.p >= cut).length,
    missed_open: rows.filter((r) => r.open && r.p < cut).length,
  };
}

const cost = (rows: Row[]) => {
  const e = errors_at(rows, OPEN_CUT);
  return FALSE_WARNING_COST * e.false_warnings + e.missed_open;
};

function ece(rows: Row[]): number {
  let total = 0;
  for (let b = 0; b < ECE_BINS; b++) {
    const bin = rows.filter((r) => (b / ECE_BINS <= r.p && r.p < (b + 1) / ECE_BINS) || (b === ECE_BINS - 1 && r.p === 1));
    if (bin.length === 0) continue;
    const mean_p = bin.reduce((s, r) => s + r.p, 0) / bin.length;
    const rate = bin.filter((r) => r.open).length / bin.length;
    total += (bin.length / rows.length) * Math.abs(mean_p - rate);
  }
  return total;
}

/** A stratified percentile bootstrap: each resample keeps the set's class counts. */
function bootstrap(rows: Row[], metric: (rows: Row[]) => number, rand: () => number): BenchEstimate {
  const open = rows.filter((r) => r.open);
  const done = rows.filter((r) => !r.open);
  const draw = (from: Row[]) => from.map(() => from[Math.floor(rand() * from.length)]!);
  const values = Array.from({ length: RESAMPLES }, () => metric([...draw(open), ...draw(done)])).sort((a, b) => a - b);
  const tail = (1 - LEVEL) / 2;
  const at = (q: number) => values[Math.min(values.length - 1, Math.max(0, Math.round(q * (values.length - 1))))]!;
  const value = metric(rows);
  return { value: round(value), lo: round(Math.min(value, at(tail))), hi: round(Math.max(value, at(1 - tail))) };
}

function roc(rows: Row[]): BenchRocPoint[] {
  const open = rows.filter((r) => r.open).length;
  const done = rows.length - open;
  const cuts = [...new Set(rows.map((r) => round(r.p)))].sort((a, b) => b - a);
  const points: BenchRocPoint[] = [{ threshold: 1, fpr: 0, tpr: 0 }];
  for (const cut of cuts) {
    const flagged = rows.filter((r) => round(r.p) >= cut);
    points.push({
      threshold: cut,
      fpr: round(flagged.filter((r) => !r.open).length / done),
      tpr: round(flagged.filter((r) => r.open).length / open),
    });
  }
  if (points.at(-1)!.fpr !== 1 || points.at(-1)!.tpr !== 1) points.push({ threshold: 0, fpr: 1, tpr: 1 });
  return points;
}

/** Bins with an edge 0.1 either side of each live cut, so the bins that decide ✅ and ⚠️ read on their own. */
function reliability(rows: Row[]): BenchBin[] {
  const edges = [...new Set([0, 1, ...[DONE_CUT, OPEN_CUT].flatMap((c) => [round(c - 0.1), round(c + 0.1)])])].sort((a, b) => a - b);
  return edges.slice(0, -1).map((lo, i) => {
    const hi = edges[i + 1]!;
    const bin = rows.filter((r) => (lo <= r.p && r.p < hi) || (hi === 1 && r.p === 1));
    return {
      lo,
      hi,
      n: bin.length,
      p: bin.length ? round(bin.reduce((s, r) => s + r.p, 0) / bin.length) : null,
      rate: bin.length ? round(bin.filter((r) => r.open).length / bin.length) : null,
    };
  });
}

function score(voter: string, rows: Row[], rand: () => number, latency: BenchScore['latency']): BenchScore {
  return {
    voter,
    auc: bootstrap(rows, auc, rand),
    brier: bootstrap(rows, brier, rand),
    ece: { value: round(ece(rows)), bins: ECE_BINS },
    roc: roc(rows),
    reliability: reliability(rows),
    operating: { threshold: OPEN_CUT, source: 'live', ...errors_at(rows, OPEN_CUT), cost: bootstrap(rows, cost, rand) },
    latency,
  };
}

/** The invented roster: one of each kind a real bench scores. */
const VOTERS: BenchVoter[] = [
  { name: 'laya-ft', label: 'LAYA · FINE-TUNE', kind: 'fine_tune', of: 'laya-base', labels: 480, params_m: 395, runtime: 'mps', licence: 'Apache-2.0', served: true },
  { name: 'laya-base', label: 'LAYA · BASE', kind: 'base', of: null, labels: null, params_m: 395, runtime: 'mps', licence: 'Apache-2.0', served: false },
  { name: 'zero-shot', label: 'ZERO-SHOT LLM', kind: 'zero_shot', of: null, labels: null, params_m: 1700, runtime: 'mlx', licence: 'MIT', served: false },
  { name: 'tfidf-floor', label: 'TF-IDF FLOOR', kind: 'floor', of: null, labels: null, params_m: null, runtime: 'cpu', licence: null, served: false },
  { name: 'mean-ft-zs', label: 'MEAN', kind: 'ensemble', of: 'laya-ft', labels: null, params_m: null, runtime: 'none', licence: null, served: false },
];

/**
 * Each drawn voter reads the row's latent difficulty through its own noise (less noise, higher AUC), then states a p
 * with its own confidence (`scale`: above 1 is over-confident) and lean (`bias`). Latency is its warm p50 and p90.
 */
const DRAWN: Record<string, { noise: number; scale: number; bias: number; p50: number; p90: number }> = {
  'laya-ft': { noise: 0.3, scale: 1.6, bias: 0.2, p50: 31, p90: 58 },
  'laya-base': { noise: 0.9, scale: 1.0, bias: -0.3, p50: 30, p90: 55 },
  'zero-shot': { noise: 1.1, scale: 2.2, bias: 0.5, p50: 410, p90: 760 },
  'tfidf-floor': { noise: 1.6, scale: 0.8, bias: 0, p50: 1.2, p90: 2.4 },
};
/** How far apart open and done rows sit on the latent scale, in standard deviations. */
const LATENT_SEPARATION = 1.3;

const SETS: Record<BenchSet, { open: number; done: number }> = { golden: { open: 25, done: 25 }, heldout: { open: 48, done: 52 } };

export function make_fixture(): BenchResults {
  const rand = prng(SEED);
  const sets = (Object.keys(SETS) as BenchSet[]).map((set) => {
    const n = SETS[set];
    const truth = [...Array<boolean>(n.open).fill(true), ...Array<boolean>(n.done).fill(false)];
    // One latent difficulty per row, shared by every voter, so their errors correlate the way real voters' do.
    const latent = truth.map((open) => ((open ? 1 : -1) * LATENT_SEPARATION) / 2 + normal(rand));
    const drawn: Record<string, Row[]> = {};
    for (const [name, v] of Object.entries(DRAWN)) {
      // Kept inside (0, 1), so the curve's first point (threshold 1: flag nothing) is (0, 0).
      const p = (i: number) => Math.min(0.999, Math.max(0.001, round(sigmoid(v.scale * (latent[i]! + v.noise * normal(rand)) + v.bias), 3)));
      drawn[name] = truth.map((open, i) => ({ open, p: p(i) }));
    }
    drawn['mean-ft-zs'] = truth.map((open, i) => ({ open, p: round((drawn['laya-ft']![i]!.p + drawn['zero-shot']![i]!.p) / 2, 3) }));
    const rows = n.open + n.done;
    const scores = VOTERS.map((voter) => {
      const v = DRAWN[voter.name];
      return score(voter.name, drawn[voter.name]!, rand, v ? { n: rows, p50_ms: v.p50, p90_ms: v.p90 } : null);
    });
    return { set, n, scores };
  });
  return benchResultsSchema.parse({
    v: BENCH_RESULTS_VERSION,
    fixture: true,
    protocol: {
      id: 'fixture',
      surface: 'agent_open_work',
      question: 'open_work',
      question_version: 2,
      positive: 'open',
      description: 'Invented numbers from a seeded generator (schemas/scripts/bench-fixture.ts), to build the EVALS page against. No model produced them.',
      interval: { level: LEVEL, method: 'bootstrap', resamples: RESAMPLES, seed: SEED },
      costs: { false_warning: FALSE_WARNING_COST },
    },
    generated_at: '2026-11-13T00:00:00Z',
    commit: null,
    voters: VOTERS,
    sets,
    rack: {
      from: '2026-10-14',
      to: '2026-11-12',
      days: 30,
      judged: 2400,
      verdicts: { done: 1500, reply: 420, open: 480 },
      reached: 260,
      latency: { n: 2400, p50_ms: 38, p90_ms: 95 },
      next_step: { suggested: 610, taken: 205 },
    },
    reproduce: { command: 'uv run --project lab laya-lab bench --set golden', set: 'golden' },
  });
}

/** Two-space JSON, with every object of plain values on one line: a ROC point is one line, not five. */
export function format_fixture(value: unknown, indent = ''): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  const inner = `${indent}  `;
  const flat = (v: unknown) => v === null || typeof v !== 'object';
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    if (value.every(flat)) return `[${value.map((v) => JSON.stringify(v)).join(', ')}]`;
    return `[\n${value.map((v) => inner + format_fixture(v, inner)).join(',\n')}\n${indent}]`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return '{}';
  if (entries.every(([, v]) => flat(v))) return `{ ${entries.map(([k, v]) => `${JSON.stringify(k)}: ${JSON.stringify(v)}`).join(', ')} }`;
  return `{\n${entries.map(([k, v]) => `${inner}${JSON.stringify(k)}: ${format_fixture(v, inner)}`).join(',\n')}\n${indent}}`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(FIXTURE_PATH, `${format_fixture(make_fixture())}\n`);
  console.log(`wrote ${FIXTURE_PATH}`);
}
