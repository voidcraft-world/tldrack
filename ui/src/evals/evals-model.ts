/**
 * What the EVALS page derives from one results file (`@tldrack/schemas/bench-results.ts` owns the
 * file's shape and what each number means). Pure: every panel's readout is a function here, so
 * the components only draw.
 */

import {
    BENCH_SETS,
    benchResultsSchema,
    cost_curve,
    type BenchEstimate,
    type BenchRack,
    type BenchProtocol,
    type BenchResults,
    type BenchRocPoint,
    type BenchScore,
    type BenchSet,
    type BenchSetResult,
    type BenchVoter,
} from '@tldrack/schemas/bench-results.ts';

import { nice_top } from './chart-geometry.ts';

// --- Which file ---------------------------------------------------------------------------

/** The invented results the page is built against. A real run always wins over it. */
export const FIXTURE_FILE = 'fixture.json';

/** A path's last part: the name a results file is known by. */
export function base_name(path: string): string {
    return path.slice(path.lastIndexOf('/') + 1);
}

/**
 * The results file the page shows, from the paths in `bench/results/`: the newest real run, and
 * the fixture only when there is no real run. A run is named for its date, so the greatest name
 * is the newest.
 */
export function pick_results_file(paths: readonly string[]): string | null {
    const real = paths.filter((path) => base_name(path) !== FIXTURE_FILE);
    if (real.length > 0) return real.reduce((best, path) => (base_name(path) > base_name(best) ? path : best));
    return paths.find((path) => base_name(path) === FIXTURE_FILE) ?? null;
}

export type ParsedResults = { ok: true; results: BenchResults } | { ok: false; issues: string[] };

/** The most issues a refused file prints: the first few name the fault, the rest repeat it. */
export const ISSUES_SHOWN = 8;

/** The file through its schema. A file that does not parse is refused whole: no panel draws from it. */
export function parse_results(raw: unknown): ParsedResults {
    const parsed = benchResultsSchema.safeParse(raw);
    if (parsed.success) return { ok: true, results: parsed.data };
    const issues = parsed.error.issues.slice(0, ISSUES_SHOWN).map((issue) => `${issue.path.join('.') || '(file)'}: ${issue.message}`);
    return { ok: false, issues };
}

// --- Sets and voters ----------------------------------------------------------------------

export const SET_LABEL: Readonly<Record<BenchSet, string>> = { golden: 'Golden', heldout: 'Held-out' };

/** The sets a file scored, in the schema's order. */
export function sets_in(results: BenchResults): BenchSet[] {
    return BENCH_SETS.filter((set) => results.sets.some((result) => result.set === set));
}

/** The set the charts open on: the operator's own turn ends when the file has them. */
export function headline_set(results: BenchResults): BenchSet {
    const sets = sets_in(results);
    return sets.includes('heldout') ? 'heldout' : sets[0]!;
}

export function set_result(results: BenchResults, set: BenchSet): BenchSetResult | undefined {
    return results.sets.find((result) => result.set === set);
}

export function score_of(result: BenchSetResult | undefined, voter: string): BenchScore | undefined {
    return result?.scores.find((score) => score.voter === voter);
}

/** The voter the charts draw bold until the reader picks another: the served model. */
export function default_focus(voters: readonly BenchVoter[]): string {
    return (voters.find((voter) => voter.served) ?? voters[0]!).name;
}

/** The colours a chart has for voters. A ninth voter is drawn in the neutral ink, never a made-up hue. */
export const SERIES_SLOTS = 8;

/**
 * A voter's colour slot, 1 to `SERIES_SLOTS`, or null for the neutral ink. Slots go to the
 * models, in the file's order. An ensemble takes none: it is the mean of voters that already
 * have a colour, and a real run scores more ensembles than models, so hues spent on them would
 * leave a chart nobody can read. The slot follows the voter, so a set that scores fewer voters
 * repaints none of them.
 */
export function voter_slots(voters: readonly BenchVoter[]): Record<string, number | null> {
    let next = 0;
    return Object.fromEntries(voters.map((voter) => [voter.name, voter.kind !== 'ensemble' && next < SERIES_SLOTS ? ++next : null]));
}

/** The CSS colour of a slot (`evals.css` owns the values, one set per theme). */
export function series_color(slot: number | null | undefined): string {
    return slot ? `var(--ev-series-${slot})` : 'var(--ev-series-other)';
}

export interface ScoredVoter {
    voter: BenchVoter;
    score: BenchScore;
    color: string;
    focus: boolean;
    /** In the neutral ink and not picked: drawn thin, and folded out of a chart's legend and readout. */
    rest: boolean;
}

/**
 * The voters a set scored, in the file's order, each with its colour and how it is drawn. A
 * picked voter with no colour of its own is drawn in the page's ink, so it stands out from the
 * other neutral lines.
 */
export function scored_voters(result: BenchSetResult, voters: readonly BenchVoter[], slots: Record<string, number | null>, focus: string): ScoredVoter[] {
    return voters.flatMap((voter) => {
        const score = score_of(result, voter.name);
        if (!score) return [];
        const picked = voter.name === focus;
        const slot = slots[voter.name] ?? null;
        return [{ voter, score, color: slot === null && picked ? 'var(--ev-ink)' : series_color(slot), focus: picked, rest: slot === null && !picked }];
    });
}

/** The order lines are drawn in: the neutral ones under the coloured ones, the picked one on top. */
export function draw_order(a: ScoredVoter, b: ScoredVoter): number {
    const rank = (entry: ScoredVoter): number => (entry.focus ? 2 : entry.rest ? 0 : 1);
    return rank(a) - rank(b);
}

/**
 * The head of a legend's closed second table: what the voters with no colour of their own are.
 * Ensembles are named as what they are; any other voter there is one the eight colours ran out
 * before, and is counted apart so a reader knows a model is folded away, not only means.
 */
export function rest_head(rest: readonly ScoredVoter[]): string {
    const means = rest.filter((entry) => entry.voter.kind === 'ensemble').length;
    const others = rest.length - means;
    const count = (n: number, one: string): string => `${n} ${n === 1 ? one : `${one}s`}`;
    return [
        ...(others > 0 ? [count(others, 'more voter')] : []),
        ...(means > 0 ? [`${count(means, 'ensemble')} · each the mean of two voters`] : []),
    ].join(' · ');
}

/** The voter a chart draws bold: the picked one, or the first when this set did not score it. */
export function focused_voter(scored: readonly ScoredVoter[]): ScoredVoter | undefined {
    return scored.find((entry) => entry.focus) ?? scored[0];
}

export const KIND_LABEL: Readonly<Record<BenchVoter['kind'], string>> = {
    fine_tune: 'fine-tune',
    base: 'base',
    zero_shot: 'zero-shot',
    floor: 'floor',
    ensemble: 'ensemble',
    panel: 'panel',
};

// --- Panel 1: read less -------------------------------------------------------------------

export type ReadLessPart = 'done' | 'reply' | 'held' | 'reached';

export interface ReadLess {
    judged: number;
    reached: number;
    /** Turn ends the operator never had to read: judged, and not shown as ⚠️. */
    kept: number;
    kept_share: number;
    /** Every judged turn end, once: the three verdicts, with `open` split by whether its ⚠️ was shown. */
    parts: { key: ReadLessPart; n: number; share: number }[];
    per_day: { judged: number; reached: number };
    taken_share: number | null;
}

export function read_less(rack: BenchRack): ReadLess {
    const { judged, reached, verdicts, days } = rack;
    const share = (n: number): number => (judged === 0 ? 0 : n / judged);
    const counts: [ReadLessPart, number][] = [
        ['done', verdicts.done],
        ['reply', verdicts.reply],
        ['held', verdicts.open - reached],
        ['reached', reached],
    ];
    return {
        judged,
        reached,
        kept: judged - reached,
        kept_share: share(judged - reached),
        parts: counts.map(([key, n]) => ({ key, n, share: share(n) })),
        per_day: { judged: judged / days, reached: reached / days },
        taken_share: rack.next_step && rack.next_step.suggested > 0 ? rack.next_step.taken / rack.next_step.suggested : null,
    };
}

// --- Panel 2: what the labels buy ---------------------------------------------------------

export type GainMetric = 'auc' | 'cost' | 'brier';

export const GAIN_METRICS: readonly { key: GainMetric; label: string; better: 'higher' | 'lower'; digits: number }[] = [
    { key: 'auc', label: 'AUC', better: 'higher', digits: 3 },
    { key: 'cost', label: 'Cost', better: 'lower', digits: 0 },
    { key: 'brier', label: 'Brier', better: 'lower', digits: 3 },
];

export function metric_estimate(score: BenchScore, metric: GainMetric): BenchEstimate | null {
    if (metric === 'auc') return score.auc;
    return metric === 'cost' ? score.operating.cost : score.brier;
}

export interface GainRow {
    metric: (typeof GAIN_METRICS)[number];
    base: BenchEstimate | null;
    tune: BenchEstimate | null;
    /** Fine-tune minus base; null when either side has no estimate. */
    delta: number | null;
    /** Whether the change is in the metric's good direction; null on no change or no estimate. */
    better: boolean | null;
    /** Whether the two intervals share no value. Not a paired test: the page prints it as what it is. */
    apart: boolean | null;
}

export interface LabelGain {
    base: BenchVoter;
    tune: BenchVoter;
    labels: number;
    rows: GainRow[];
}

/** Every fine-tune beside the voter it was trained from, on one set. */
export function label_gains(results: BenchResults, set: BenchSet): LabelGain[] {
    const result = set_result(results, set);
    const gains: LabelGain[] = [];
    for (const tune of results.voters) {
        if (tune.kind !== 'fine_tune' || tune.of === null || tune.labels === null) continue;
        const base = results.voters.find((voter) => voter.name === tune.of);
        const tune_score = score_of(result, tune.name);
        const base_score = base && score_of(result, base.name);
        if (!base || !tune_score || !base_score) continue;
        const rows = GAIN_METRICS.map((metric): GainRow => {
            const from = metric_estimate(base_score, metric.key);
            const to = metric_estimate(tune_score, metric.key);
            if (!from || !to) return { metric, base: from, tune: to, delta: null, better: null, apart: null };
            const delta = to.value - from.value;
            const better = delta === 0 ? null : delta > 0 === (metric.better === 'higher');
            return { metric, base: from, tune: to, delta, better, apart: to.lo > from.hi || to.hi < from.lo };
        });
        gains.push({ base, tune, labels: tune.labels, rows });
    }
    return gains;
}

/**
 * The axis a metric's two intervals are drawn on. AUC runs from chance to 1, unless an interval
 * reaches under chance. The two that count down to nothing start at 0 and end on a round number.
 */
export function gain_domain(row: GainRow): [number, number] {
    const estimates = [row.base, row.tune].filter((estimate) => estimate !== null);
    if (row.metric.key === 'auc') return [estimates.some((estimate) => estimate.lo < 0.5) ? 0 : 0.5, 1];
    return [0, nice_top(Math.max(0, ...estimates.map((estimate) => estimate.hi)), 2)];
}

// --- Panels 3 and 5: the curve, and what a cut costs --------------------------------------

/** The best share of open turns a voter flags while flagging at most `fpr` of the done ones. */
export function roc_tpr_at(roc: readonly BenchRocPoint[], fpr: number): number {
    return roc.reduce((best, point) => (point.fpr <= fpr + 1e-9 && point.tpr > best ? point.tpr : best), 0);
}

/**
 * Where a cut sits on the curve. A row is flagged when its score is at or above the cut, so the
 * cut lands on the last point whose threshold is still at or above it.
 */
export function roc_point_at(roc: readonly BenchRocPoint[], cut: number): BenchRocPoint {
    return roc.findLast((point) => point.threshold >= cut) ?? roc[0]!;
}

export interface CostStep {
    threshold: number;
    false_warnings: number;
    missed_open: number;
    cost: number;
}

/**
 * Cost against the cut, across the whole of [0, 1], thresholds rising. A step's numbers hold
 * from just above the step before it up to its own threshold: no row scores between two
 * neighbouring thresholds, so every cut in that gap flags the same rows.
 */
export function cost_steps(score: BenchScore, n: { open: number; done: number }, costs: BenchProtocol['costs']): CostStep[] {
    const steps = cost_curve(score, n, costs).reverse();
    if (steps[0]!.threshold > 0) steps.unshift({ ...steps[0]!, threshold: 0 });
    if (steps.at(-1)!.threshold < 1) steps.push({ ...steps.at(-1)!, threshold: 1 });
    return steps;
}

/** The numbers a cut draws: those of the first step at or above it. */
export function cost_at(steps: readonly CostStep[], cut: number): CostStep {
    return steps.find((step) => step.threshold >= cut) ?? steps.at(-1)!;
}

/** The cheapest cut on this set. Among equals, the highest: the one that shows the fewest ⚠️. */
export function cheapest_step(steps: readonly CostStep[]): CostStep {
    return steps.reduce((best, step) => (step.cost <= best.cost ? step : best));
}

// --- Readouts -----------------------------------------------------------------------------

export function fmt_count(value: number): string {
    return Math.round(value).toLocaleString('en-US');
}

export function fmt_fixed(value: number, digits: number): string {
    return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A share as a whole per cent, or with one decimal under ten, where a whole one hides most of it. */
export function fmt_share(share: number): string {
    const per_cent = share * 100;
    return `${per_cent > 0 && per_cent < 10 ? fmt_fixed(per_cent, 1) : Math.round(per_cent)}%`;
}

export function fmt_interval(estimate: BenchEstimate, digits: number): string {
    return `${fmt_fixed(estimate.lo, digits)} – ${fmt_fixed(estimate.hi, digits)}`;
}

/** A signed change, with a real minus sign. */
export function fmt_delta(delta: number, digits: number): string {
    const sign = delta > 0 ? '+' : delta < 0 ? '−' : '±';
    return `${sign}${fmt_fixed(Math.abs(delta), digits)}`;
}

export function fmt_ms(value: number): string {
    if (value < 0.05) return '<0.1 ms';
    return `${value < 10 ? fmt_fixed(value, 1) : fmt_count(value)} ms`;
}

/**
 * What a fine-tune trained on. The bench writes 0 when the checkpoint has no training record to
 * count labels in, so a 0 is "not recorded", never "none".
 */
export function fmt_labels(labels: number): string {
    return labels > 0 ? `${fmt_count(labels)} operator labels` : 'operator labels (count not recorded)';
}

/** Parameters, given in millions: `395M`, `1.7B`. */
export function fmt_params(millions: number): string {
    if (millions < 1000) return `${Number(millions.toPrecision(3))}M`;
    return `${Number((millions / 1000).toPrecision(2))}B`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** An ISO date or date-time as `Nov 12, 2026`, read in UTC so every reader sees the same day. */
export function fmt_day(iso: string, with_year = true): string {
    const date = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
    const day = `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}`;
    return with_year ? `${day}, ${date.getUTCFullYear()}` : day;
}

export function fmt_day_range(from: string, to: string): string {
    return `${fmt_day(from, from.slice(0, 4) !== to.slice(0, 4))} – ${fmt_day(to)}`;
}
