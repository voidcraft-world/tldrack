/**
 * The TRAINING page's arithmetic: who can be shown, what a cut does to their rows, and how far a
 * retrain is. Pure, so `tests/unit/training-model.test.ts` runs it under bare `node --test`.
 *
 * ONE SHAPE FOR EVERY VOTER. A voter is anything that answered the open-work question with a
 * probability: a Laya snapshot, a subscription model, the floor. Each is shown from its ROWS, a
 * probability per row grouped by the row's label (`ScoredSet`), because the page's three views are
 * all the same rows read three ways: the strip (every row), the quadrant (the rows counted at a
 * cut) and the ROC curve (the count at every cut).
 *
 * TWO SOURCES, NEVER MIXED IN ONE COMPARISON.
 * - `gate`: a promotion gate's kept rows (`/training` `evidence`): a candidate snapshot and the
 *   served model on the SAME rows. Why that is the only place a snapshot's rows are kept: owned by
 *   plugins/laya/laya_bridge/training.py — see the note there.
 * - `bench`: the eval results bundled with the page (`evals/results-source.ts`). A results file
 *   is aggregates only, but its ROC curve has one point per distinct score, so a voter's rows are
 *   read back off the curve's steps (`rows_from_roc`): each step up is that many open rows at that
 *   score, each step right that many done rows. Exact while the curve was not thinned; a thinned
 *   curve gives no rows rather than wrong ones.
 * A voter is compared with the BASELINE of its own source (the served model there), on the same
 * set, so two curves on one chart always answered the same rows.
 */
import type { BenchResults, BenchRocPoint } from '@tldrack/schemas/bench-results.ts';
import type { LayaOpsState, LayaRetrainRow, LayaTrainingSnapshot, LayaTrainingState } from '@tldrack/schemas/laya-review.ts';

export type VoterGroup = 'laya' | 'subscription' | 'other';
export type VoterSource = 'gate' | 'bench';
export type SetName = 'golden' | 'heldout';

/** One set's rows for one voter: `p` of every row whose label is open, and of every row whose label is done. */
export interface ScoredSet {
    set: SetName;
    open: number[];
    done: number[];
}

export interface Voter {
    /** Unique on the page: `gate:<checkpoint>` or `bench:<voter>`. */
    id: string;
    label: string;
    group: VoterGroup;
    source: VoterSource;
    /** The model the rack serves, as this source knew it. */
    served: boolean;
    /** The ⚠️ cut the voter was measured at: where the page's cut starts. */
    cut: number;
    sets: ScoredSet[];
    /** A snapshot's folder name, when the voter is one. */
    snapshot: string | null;
    /** A gate's verdict on this candidate; null for every other voter. */
    verdict: string | null;
    why: string | null;
    /** When the rows were scored, as the source stamped it. */
    at: string | null;
}

export const SET_LABELS: Record<SetName, string> = { golden: 'GOLDEN', heldout: 'HELD-OUT' };
export const SET_TITLES: Record<SetName, string> = {
    golden: 'Hand-labelled cases no model trains on',
    heldout: 'Labelled turn ends neither model trained on',
};
/** The cut the rack draws ⚠️ at when a voter names none (`open_work.py`). */
export const DEFAULT_CUT = 0.7;

// ── rows ─────────────────────────────────────────────────────────────────────

/**
 * A voter's rows read back off its ROC curve, or null when the curve does not hold them all (the
 * module note owns why this is exact). `roc` runs from the top threshold down, each point the
 * share of open and of done rows at or over its threshold.
 */
export function rows_from_roc(roc: readonly BenchRocPoint[], n: { open: number; done: number }): { open: number[]; done: number[] } | null {
    const open: number[] = [];
    const done: number[] = [];
    let seen_open = 0;
    let seen_done = 0;
    for (const point of roc) {
        const at_open = Math.round(point.tpr * n.open);
        const at_done = Math.round(point.fpr * n.done);
        if (at_open < seen_open || at_done < seen_done) return null;
        for (let i = seen_open; i < at_open; i += 1) open.push(point.threshold);
        for (let i = seen_done; i < at_done; i += 1) done.push(point.threshold);
        seen_open = at_open;
        seen_done = at_done;
    }
    return open.length === n.open && done.length === n.done ? { open, done } : null;
}

function split(expected: readonly boolean[], p: readonly number[]): { open: number[]; done: number[] } {
    const open: number[] = [];
    const done: number[] = [];
    expected.forEach((is_open, i) => (is_open ? open : done).push(p[i]!));
    return { open, done };
}

/** `tldrack-agent-open-work-20261016-1401` → `10-16 14:01`: a snapshot by when it was trained. */
export function snapshot_label(name: string): string {
    const stamp = /-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})?$/.exec(name);
    return stamp ? `${stamp[2]}-${stamp[3]} ${stamp[4]}:${stamp[5]}` : name;
}

/** Every voter a gate kept rows for: each candidate, and the served model from the newest gate that judged against it. */
export function voters_from_evidence(state: LayaTrainingState): Voter[] {
    const out: Voter[] = [];
    const seen = new Set<string>();
    for (const row of state.evidence) {
        const sides = [
            { name: row.candidate, key: 'candidate' as const, served: false },
            { name: row.served, key: 'served' as const, served: true },
        ];
        for (const side of sides) {
            if (seen.has(side.name)) continue;
            seen.add(side.name);
            out.push({
                id: `gate:${side.name}`,
                label: snapshot_label(side.name),
                group: 'laya',
                source: 'gate',
                served: side.served,
                cut: row.cuts[side.key].at,
                sets: row.sets.map((block) => ({ set: block.set, ...split(block.expected, block[side.key]) })),
                snapshot: side.name,
                verdict: side.served ? null : row.verdict,
                why: side.served ? null : row.why,
                at: row.at,
            });
        }
    }
    return out;
}

const BENCH_GROUPS: Record<string, VoterGroup> = { base: 'laya', fine_tune: 'laya', panel: 'laya', zero_shot: 'subscription' };

/** Every voter of a results file whose rows its ROC curve still holds. */
export function voters_from_bench(results: BenchResults): Voter[] {
    return results.voters.flatMap((voter): Voter[] => {
        const sets: ScoredSet[] = [];
        let cut = DEFAULT_CUT;
        for (const block of results.sets) {
            const score = block.scores.find((entry) => entry.voter === voter.name);
            const rows = score ? rows_from_roc(score.roc, block.n) : null;
            if (!score || !rows) continue;
            if (block.set === 'golden' || sets.length === 0) cut = score.operating.threshold;
            if (block.set === 'golden' || block.set === 'heldout') sets.push({ set: block.set, ...rows });
        }
        if (sets.length === 0) return [];
        return [
            {
                id: `bench:${voter.name}`,
                label: voter.label,
                group: BENCH_GROUPS[voter.kind] ?? 'other',
                source: 'bench',
                served: voter.served,
                cut,
                sets,
                snapshot: null,
                verdict: null,
                why: null,
                at: results.generated_at,
            },
        ];
    });
}

export interface PairedRows {
    expected: boolean[];
    /** The candidate's p and the served model's p on row i. */
    candidate: number[];
    served: number[];
    cuts: { candidate: number; served: number };
    served_name: string;
}

/** The rows a gate scored BOTH models on, kept row by row: what a candidate moved, and which way. */
export function paired_rows(state: LayaTrainingState | null, voter: Voter | null, set: SetName): PairedRows | null {
    if (!state || !voter || voter.source !== 'gate' || voter.served) return null;
    const row = state.evidence.find((entry) => entry.candidate === voter.snapshot);
    const block = row?.sets.find((entry) => entry.set === set);
    if (!row || !block) return null;
    return {
        expected: block.expected,
        candidate: block.candidate,
        served: block.served,
        cuts: { candidate: row.cuts.candidate.at, served: row.cuts.served.at },
        served_name: row.served,
    };
}

/** How many rows a candidate moved across the served model's side of a cut, by whether the move was right. */
export function moved_rows(rows: PairedRows, cut: number): { fixed: number; broke: number } {
    let fixed = 0;
    let broke = 0;
    rows.expected.forEach((is_open, i) => {
        const before = rows.served[i]! >= rows.cuts.served === is_open;
        const after = rows.candidate[i]! >= cut === is_open;
        if (after && !before) fixed += 1;
        if (before && !after) broke += 1;
    });
    return { fixed, broke };
}

/** The voter a pick is read against: the served model of the same source; null for the served model itself. */
export function baseline_of(voter: Voter, voters: readonly Voter[]): Voter | null {
    return voters.find((other) => other.source === voter.source && other.served && other.id !== voter.id) ?? null;
}

export function set_of(voter: Voter | null, set: SetName): ScoredSet | null {
    return voter?.sets.find((block) => block.set === set) ?? null;
}

/** The pick the page opens on: the newest gate's candidate, else the served model, else the first voter. */
export function default_pick(voters: readonly Voter[]): string | null {
    const pick = voters.find((voter) => voter.source === 'gate' && !voter.served) ?? voters.find((voter) => voter.served) ?? voters[0];
    return pick?.id ?? null;
}

// ── a cut ────────────────────────────────────────────────────────────────────

/** `p ≥ cut` says open: caught open work, false ⚠️, missed open work, quiet done turns. */
export interface Confusion {
    tp: number;
    fp: number;
    fn: number;
    tn: number;
}

export function confusion_at(rows: ScoredSet, cut: number): Confusion {
    const tp = rows.open.filter((p) => p >= cut).length;
    const fp = rows.done.filter((p) => p >= cut).length;
    return { tp, fp, fn: rows.open.length - tp, tn: rows.done.length - fp };
}

export interface Rates {
    rows: number;
    accuracy: number | null;
    /** Of the ⚠️ drawn, the share that were open work. */
    precision: number | null;
    /** Of the open work, the share that drew a ⚠️. */
    recall: number | null;
    /** Of the done turns, the share that drew a false ⚠️. */
    false_rate: number | null;
}

const share = (part: number, whole: number): number | null => (whole > 0 ? part / whole : null);

export function rates_of(c: Confusion): Rates {
    const rows = c.tp + c.fp + c.fn + c.tn;
    return {
        rows,
        accuracy: share(c.tp + c.tn, rows),
        precision: share(c.tp, c.tp + c.fp),
        recall: share(c.tp, c.tp + c.fn),
        false_rate: share(c.fp, c.fp + c.tn),
    };
}

/** The rack's cost of a cut: one false ⚠️ weighs `false_warning` missed open turns (`benchProtocolSchema.costs`). */
export function cost_of(c: Confusion, false_warning: number): number {
    return false_warning * c.fp + c.fn;
}

export interface RocPoint {
    threshold: number;
    fpr: number;
    tpr: number;
}

/** The curve from the top threshold down, one point per distinct score, both corners included. */
export function roc_of(rows: ScoredSet): RocPoint[] {
    const thresholds = [...new Set([1, 0, ...rows.open, ...rows.done])].sort((a, b) => b - a);
    return thresholds.map((threshold) => {
        const c = confusion_at(rows, threshold);
        return { threshold, fpr: share(c.fp, rows.done.length) ?? 0, tpr: share(c.tp, rows.open.length) ?? 0 };
    });
}

/** ROC AUC by Mann–Whitney, a tie counting half: the chance a random open row outscores a random done one. */
export function auc_of(rows: ScoredSet): number | null {
    if (rows.open.length === 0 || rows.done.length === 0) return null;
    const done = [...rows.done].sort((a, b) => a - b);
    let wins = 0;
    for (const p of rows.open) {
        let below = 0;
        let ties = 0;
        for (const q of done) {
            if (q < p) below += 1;
            else if (q === p) ties += 1;
            else break;
        }
        wins += below + ties / 2;
    }
    return wins / (rows.open.length * rows.done.length);
}

/** The cut of the smallest cost over every distinct score, ties to the higher cut (`heldout.py` `RULE`). */
export function cheapest_cut(rows: ScoredSet, false_warning: number): number {
    const cuts = [...new Set([...rows.open, ...rows.done])].sort((a, b) => b - a);
    let best = DEFAULT_CUT;
    let lowest = Infinity;
    for (const cut of cuts) {
        const cost = cost_of(confusion_at(rows, cut), false_warning);
        if (cost < lowest) {
            lowest = cost;
            best = cut;
        }
    }
    return best;
}

// ── the strip ────────────────────────────────────────────────────────────────

export interface Dot {
    /** The row's probability. */
    p: number;
    /** Its bin along [0, 1], and its place in that bin's stack, from 0. */
    bin: number;
    level: number;
}

/**
 * A dot per row, stacked where rows share a bin: the raw points as a dot histogram, so no row
 * hides behind another. `bins` is how many dots fit side by side.
 */
export function stack_dots(values: readonly number[], bins: number): { dots: Dot[]; tallest: number } {
    const heights = Array.from({ length: bins }, () => 0);
    const dots = [...values]
        .sort((a, b) => a - b)
        .map((p): Dot => {
            const bin = Math.min(bins - 1, Math.max(0, Math.floor(p * bins)));
            const level = heights[bin]!;
            heights[bin] = level + 1;
            return { p, bin, level };
        });
    return { dots, tallest: Math.max(0, ...heights) };
}

// ── the retrain ──────────────────────────────────────────────────────────────

export type RetrainStep = LayaRetrainRow['steps'][number];
type Job = LayaOpsState['jobs'][number];

export interface RetrainView {
    /** What the one button does now. */
    action: 'start' | 'resume' | 'none';
    label: string;
    /** One line under the button: where the cycle stands, in words. */
    status: string;
    tone: 'idle' | 'live' | 'warn' | 'ok';
    /** The cycle is being worked on right now, or waits in the lab queue. */
    busy: boolean;
    /** The share of the whole cycle behind it: steps done, the running fit counted by its own progress. */
    done: number;
}

const OPEN_STATES = ['running', 'waiting', 'failed'];

/** How far the whole cycle is: each step one share, the fit by its own progress. */
export function cycle_progress(row: LayaRetrainRow): number {
    const total = row.steps.length || 1;
    const done = row.steps.reduce((sum, step) => {
        if (step.state === 'done' || step.state === 'skipped') return sum + 1;
        return step.name === 'train' && step.state === 'running' ? sum + (row.progress?.done ?? 0) : sum;
    }, 0);
    return done / total;
}

/** The retrain as the page shows it: `row` is OPS's journal row, `job` the lab queue's row for the verb. */
export function retrain_view(row: LayaRetrainRow | null, job: Job | null): RetrainView {
    const open = row !== null && OPEN_STATES.includes(row.state);
    const done = row ? cycle_progress(row) : 0;
    if (job?.state === 'queued') {
        return { action: 'none', label: `QUEUED #${job.position ?? 1}`, status: 'Waiting behind the lab job that runs now', tone: 'live', busy: true, done };
    }
    if (job?.state === 'working') {
        const at = row && open && row.step ? `at ${row.step}` : 'starting';
        return { action: 'none', label: 'RETRAINING…', status: `Running ${at} · watch ${job.session} in the rack`, tone: 'live', busy: true, done };
    }
    if (row && open) {
        const where = row.step ?? 'its last step';
        const status =
            row.state === 'waiting'
                ? `Waiting at ${where} for the gpu lease · a press tries again`
                : row.state === 'failed'
                  ? `Stopped at ${where}${row.error ? ` (${row.error})` : ''} · a press retries that step`
                  : `Cut off at ${where} · a press picks it up there`;
        return { action: 'resume', label: 'RESUME RETRAIN', status, tone: 'warn', busy: false, done };
    }
    if (job?.state === 'failed') {
        return { action: 'start', label: 'RETRAIN', status: `The last press exited ${job.exit_code ?? '?'} · see ${job.session}`, tone: 'warn', busy: false, done };
    }
    if (row?.state === 'done') {
        return { action: 'start', label: 'RETRAIN', status: 'The last cycle registered a @challenger', tone: 'ok', busy: false, done };
    }
    if (row?.state === 'refused') {
        return { action: 'start', label: 'RETRAIN', status: 'The promotion gate refused the last candidate', tone: 'warn', busy: false, done };
    }
    if (row?.state === 'abandoned') {
        return { action: 'start', label: 'RETRAIN', status: 'The last cycle was abandoned: a step failed too often', tone: 'warn', busy: false, done };
    }
    return { action: 'start', label: 'RETRAIN', status: 'No retrain has run on this machine yet', tone: 'idle', busy: false, done };
}

/**
 * The moment a cycle's clocks are read at. A cycle being worked on runs on the wall clock. One that is not (cut off,
 * waiting, failed, closed) stopped when its journal was last written: read on the wall clock, a dead fit would
 * keep counting up and keep promising a finish.
 */
export function cycle_clock(row: LayaRetrainRow | null, live: boolean, now_ms: number): number {
    const written = row?.updated_at ? Date.parse(row.updated_at) : NaN;
    return live || !Number.isFinite(written) ? now_ms : written;
}

/** Seconds left in the fit, from how long the share behind it took; null until there is a share to read. */
export function fit_seconds_left(step: RetrainStep | undefined, done: number | null | undefined, now_ms: number): number | null {
    const started = step?.started_at ? Date.parse(step.started_at) : NaN;
    if (!Number.isFinite(started) || !done || done <= 0 || done >= 1) return null;
    const elapsed = Math.max(0, now_ms - started) / 1000;
    return (elapsed * (1 - done)) / done;
}

/** How long a running step has been going, in seconds; null when it never said when it started. */
export function running_seconds(step: RetrainStep, now_ms: number): number | null {
    const started = step.started_at ? Date.parse(step.started_at) : NaN;
    return step.state === 'running' && Number.isFinite(started) ? Math.max(0, now_ms - started) / 1000 : null;
}

// ── words ────────────────────────────────────────────────────────────────────

/** `47` → `47s`, `754` → `12m 34s`, `3725` → `1h 02m`. */
export function fmt_duration(seconds: number): string {
    const whole = Math.max(0, Math.round(seconds));
    if (whole < 60) return `${whole}s`;
    if (whole < 3600) return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`;
    return `${Math.floor(whole / 3600)}h ${String(Math.floor((whole % 3600) / 60)).padStart(2, '0')}m`;
}

export function fmt_share(value: number | null): string {
    return value === null ? '—' : `${Math.round(value * 100)}%`;
}

export function fmt_p(value: number | null, digits = 2): string {
    return value === null ? '—' : value.toFixed(digits);
}

/** A signed change with its own sign, `±0` spelled as no change. */
export function fmt_change(delta: number): string {
    if (delta === 0) return '±0';
    return delta > 0 ? `+${delta}` : `−${Math.abs(delta)}`;
}

/** A snapshot's one-word standing: the alias it holds, else what its gate said, else that nothing judged it. */
export function snapshot_standing(snapshot: LayaTrainingSnapshot): { word: string; tone: 'ok' | 'warn' | 'idle' | 'live' } {
    if (snapshot.alias === 'champion') return { word: 'SERVED', tone: 'ok' };
    if (snapshot.alias === 'challenger') return { word: 'CHALLENGER', tone: 'live' };
    if (snapshot.alias === 'previous') return { word: 'PREVIOUS', tone: 'idle' };
    if (snapshot.rejected) return { word: 'REJECTED', tone: 'warn' };
    if (snapshot.verdict === 'REFUSE') return { word: 'REFUSED', tone: 'warn' };
    if (snapshot.verdict === 'PASS') return { word: 'PASSED', tone: 'ok' };
    return { word: 'UNJUDGED', tone: 'idle' };
}
