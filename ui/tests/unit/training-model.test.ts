import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { benchResultsSchema, type BenchResults } from '@tldrack/schemas/bench-results.ts';
import { layaTrainingStateSchema, type LayaOpsState, type LayaRetrainRow, type LayaTrainingState } from '@tldrack/schemas/laya-review.ts';

import {
    auc_of,
    baseline_of,
    cheapest_cut,
    confusion_at,
    cost_of,
    cycle_clock,
    cycle_progress,
    default_pick,
    fit_seconds_left,
    fmt_change,
    fmt_duration,
    moved_rows,
    paired_rows,
    rates_of,
    retrain_view,
    roc_of,
    rows_from_roc,
    set_of,
    snapshot_label,
    snapshot_standing,
    stack_dots,
    voters_from_bench,
    voters_from_evidence,
    type ScoredSet,
} from '../../src/training/training-model.ts';

// Synthetic rows: every number here is invented.
const ROWS: ScoredSet = { set: 'heldout', open: [0.9, 0.8, 0.6, 0.2], done: [0.75, 0.3, 0.1, 0.1] };
const CANDIDATE = 'tldrack-agent-open-work-20261101-090000';
const SERVED = 'tldrack-agent-open-work-20261001-0900';

const STATE: LayaTrainingState = layaTrainingStateSchema.parse({
    v: 1,
    ok: true,
    message: 'ok',
    surface: 'agent_open_work',
    snapshots: [],
    evidence: [
        {
            candidate: CANDIDATE,
            served: SERVED,
            at: '2026-11-01T10:00:00Z',
            verdict: 'REFUSE',
            promote: false,
            why: 'heldout: loss did not improve',
            cuts: { candidate: { at: 0.6, done_at: 0.4 }, served: { at: 0.7, done_at: 0.4 } },
            false_warning_cost: 2,
            checks: [{ name: 'heldout', state: 'fail' }],
            sets: [{ set: 'heldout', expected: [true, true, false, false], candidate: [0.9, 0.65, 0.2, 0.8], served: [0.9, 0.5, 0.75, 0.3] }],
        },
        {
            candidate: 'tldrack-agent-open-work-20261020-1200',
            served: SERVED,
            at: '2026-10-20T13:00:00Z',
            verdict: 'PASS',
            promote: true,
            why: null,
            cuts: { candidate: { at: 0.7, done_at: 0.4 }, served: { at: 0.7, done_at: 0.4 } },
            false_warning_cost: 2,
            checks: [],
            sets: [{ set: 'golden', expected: [true, false], candidate: [0.8, 0.1], served: [0.1, 0.9] }],
        },
    ],
    last_run: null,
});

test('a cut counts the four quadrants, and the rates follow from them', () => {
    const c = confusion_at(ROWS, 0.7);
    assert.deepEqual(c, { tp: 2, fp: 1, fn: 2, tn: 3 });
    assert.deepEqual(rates_of(c), { rows: 8, accuracy: 5 / 8, precision: 2 / 3, recall: 0.5, false_rate: 0.25 });
    assert.equal(cost_of(c, 2), 4, 'one false warning weighs two missed turns');
    assert.deepEqual(confusion_at(ROWS, 0.75), { tp: 2, fp: 1, fn: 2, tn: 3 }, 'a row AT the cut draws the warning');
    assert.equal(rates_of({ tp: 0, fp: 0, fn: 0, tn: 0 }).accuracy, null);
});

test('the curve runs corner to corner, and its area is the chance an open row outranks a done one', () => {
    const roc = roc_of(ROWS);
    assert.deepEqual(roc[0], { threshold: 1, fpr: 0, tpr: 0 });
    assert.deepEqual(roc.at(-1), { threshold: 0, fpr: 1, tpr: 1 });
    assert.ok(roc.every((point, i) => i === 0 || (point.fpr >= roc[i - 1]!.fpr && point.tpr >= roc[i - 1]!.tpr)));
    assert.equal(auc_of(ROWS), 13 / 16);
    assert.equal(auc_of({ set: 'golden', open: [0.5], done: [0.5] }), 0.5, 'a tie counts half');
    assert.equal(auc_of({ set: 'golden', open: [], done: [0.5] }), null);
});

test('the cheapest cut is the highest of the cuts that cost least', () => {
    assert.equal(cheapest_cut(ROWS, 2), 0.8, 'a false warning that weighs two misses is not worth the third open row');
    assert.equal(cheapest_cut(ROWS, 0.5), 0.2, 'a cheap false warning buys every open row');
    assert.equal(cheapest_cut({ set: 'golden', open: [0.9, 0.4], done: [] }, 2), 0.4);
});

test('rows read back off a ROC curve are the rows that drew it', () => {
    const back = rows_from_roc(roc_of(ROWS), { open: 4, done: 4 });
    assert.deepEqual(back && { open: back.open.sort(), done: back.done.sort() }, { open: [...ROWS.open].sort(), done: [...ROWS.done].sort() });
    const thinned = roc_of(ROWS).filter((_point, i) => i !== 2);
    assert.notEqual(rows_from_roc(thinned, { open: 4, done: 4 }), null, 'a dropped interior point merges two scores, and still counts every row');
    assert.equal(rows_from_roc(roc_of(ROWS).slice(0, -2), { open: 4, done: 4 }), null, 'a curve that stops short holds no rows');
});

test('every voter of the bundled results gives back exactly the rows its file counted', () => {
    const file = new URL('../../../bench/results/fixture.json', import.meta.url);
    const results: BenchResults = benchResultsSchema.parse(JSON.parse(readFileSync(file, 'utf8')));
    const voters = voters_from_bench(results);
    assert.ok(voters.length > 0);
    for (const voter of voters) {
        for (const block of voter.sets) {
            const n = results.sets.find((entry) => entry.set === block.set)!.n;
            assert.deepEqual([block.open.length, block.done.length], [n.open, n.done], `${voter.id} ${block.set}`);
            const score = results.sets.find((entry) => entry.set === block.set)!.scores.find((entry) => `bench:${entry.voter}` === voter.id)!;
            const c = confusion_at(block, score.operating.threshold);
            assert.deepEqual([c.fp, c.fn], [score.operating.false_warnings, score.operating.missed_open], `${voter.id} ${block.set} at its cut`);
        }
    }
    assert.ok(voters.every((voter) => voter.source === 'bench' && voter.id.startsWith('bench:')));
});

test('a gate gives a candidate and the served model the same rows, the served one once', () => {
    const voters = voters_from_evidence(STATE);
    assert.deepEqual(voters.map((voter) => [voter.snapshot, voter.served]), [[CANDIDATE, false], [SERVED, true], ['tldrack-agent-open-work-20261020-1200', false]]);
    const [candidate, served] = voters;
    assert.deepEqual(set_of(candidate!, 'heldout'), { set: 'heldout', open: [0.9, 0.65], done: [0.2, 0.8] });
    assert.deepEqual(set_of(served!, 'heldout'), { set: 'heldout', open: [0.9, 0.5], done: [0.75, 0.3] });
    assert.equal(candidate!.cut, 0.6);
    assert.equal(candidate!.verdict, 'REFUSE');
    assert.equal(served!.verdict, null, 'a gate judges its candidate, not the model it is judged against');
    assert.equal(baseline_of(candidate!, voters), served);
    assert.equal(baseline_of(served!, voters), null);
    assert.equal(default_pick(voters), `gate:${CANDIDATE}`);
    assert.equal(default_pick([]), null);
    assert.equal(snapshot_label(CANDIDATE), '11-01 09:00');
    assert.equal(snapshot_label('english'), 'english');
});

test('row by row, a candidate fixes some rows and breaks others', () => {
    const [candidate, served] = voters_from_evidence(STATE);
    const pairs = paired_rows(STATE, candidate!, 'heldout')!;
    assert.deepEqual(moved_rows(pairs, 0.6), { fixed: 2, broke: 1 });
    assert.equal(paired_rows(STATE, served!, 'heldout'), null, 'the served model has no other side');
    assert.equal(paired_rows(STATE, candidate!, 'golden'), null);
});

test('dots stack where rows share a bin, so no row hides another', () => {
    const { dots, tallest } = stack_dots([0.11, 0.12, 0.5, 1, 0], 10);
    assert.equal(tallest, 2);
    assert.deepEqual(dots.map((dot) => [dot.bin, dot.level]), [[0, 0], [1, 0], [1, 1], [5, 0], [9, 0]]);
    assert.deepEqual(stack_dots([], 10), { dots: [], tallest: 0 });
});

const step = (name: string, state: string, extra: object = {}) => ({ name, state, started_at: null, seconds: null, ...extra });
function row(state: string, steps: object[], extra: object = {}): LayaRetrainRow {
    return {
        surface: 'agent_open_work', id: '20261101-010000-abc123', state, started_at: null, updated_at: null, step: null, steps,
        error: null, checkpoint: null, teacher: null, label_calls: null, rows: null, operator_rows: null, reserved: null,
        train_rows: null, gate: null, sweep_best: null, sweep_rows: null, progress: null, ...extra,
    } as LayaRetrainRow;
}
const job = (state: string, extra: object = {}) =>
    ({ verb: 'retrain', surface: 'agent_open_work', state, exit_code: null, position: null, session: 'laya-retrain', cli: '', ...extra }) as LayaOpsState['jobs'][number];
const NAMES = ['export', 'label', 'build', 'train', 'gate', 'register', 'sweep', 'card'];

test('a cycle is as far as its steps, the fit counted by its own progress', () => {
    const steps = NAMES.map((name, i) => step(name, i < 3 ? 'done' : i === 3 ? 'running' : 'pending'));
    assert.equal(cycle_progress(row('running', steps, { progress: { epoch: 1, epochs: 2, step: 30, steps: 60, done: 0.25, loss: 0.5 } })), 3.25 / 8);
    assert.equal(cycle_progress(row('running', steps)), 3 / 8);
    assert.equal(cycle_progress(row('refused', NAMES.map((name) => step(name, name === 'register' || name === 'sweep' ? 'skipped' : 'done')))), 1);
});

test('the one button says what a press does now', () => {
    assert.deepEqual([retrain_view(null, job('idle')).action, retrain_view(null, job('idle')).label], ['start', 'RETRAIN']);
    const running = row('running', NAMES.map((name) => step(name, 'pending')), { step: 'train' });
    const working = retrain_view(running, job('working'));
    assert.deepEqual([working.action, working.busy], ['none', true]);
    assert.match(working.status, /at train/);
    assert.equal(retrain_view(null, job('queued', { position: 2 })).label, 'QUEUED #2');
    const cut_off = retrain_view(running, job('idle'));
    assert.deepEqual([cut_off.action, cut_off.label], ['resume', 'RESUME RETRAIN']);
    assert.match(retrain_view(row('waiting', [], { step: 'label' }), job('idle')).status, /gpu lease/);
    assert.match(retrain_view(row('failed', [], { step: 'gate', error: 'RuntimeError' }), job('failed', { exit_code: 1 })).status, /RuntimeError/);
    assert.equal(retrain_view(row('refused', []), job('idle')).action, 'start');
    assert.equal(retrain_view(row('done', []), null).tone, 'ok');
});

test('the time left in a fit is the time behind it, scaled', () => {
    const started = Date.parse('2026-11-01T01:00:00Z');
    const train = step('train', 'running', { started_at: '2026-11-01T01:00:00Z' }) as LayaRetrainRow['steps'][number];
    assert.equal(fit_seconds_left(train, 0.25, started + 600_000), 1800);
    assert.equal(fit_seconds_left(train, 0, started + 600_000), null);
    assert.equal(fit_seconds_left(train, null, started), null);
    assert.equal(fit_seconds_left(undefined, 0.5, started), null);
});

test('a cycle nobody is working on stops its clocks where its journal stopped', () => {
    const written = '2026-11-26T19:24:00Z';
    const now = Date.parse('2026-11-26T19:30:00Z');
    const stale = row('running', [], { updated_at: written });
    assert.equal(cycle_clock(stale, true, now), now, 'a live cycle runs on the wall clock');
    assert.equal(cycle_clock(stale, false, now), Date.parse(written), 'a cut-off fit never counts up');
    assert.equal(cycle_clock(row('running', []), false, now), now, 'no stamp to stop at');
    assert.equal(cycle_clock(null, false, now), now);
});

test('words', () => {
    assert.deepEqual([fmt_duration(47), fmt_duration(754), fmt_duration(3725)], ['47s', '12m 34s', '1h 02m']);
    assert.deepEqual([fmt_change(0), fmt_change(3), fmt_change(-2)], ['±0', '+3', '−2']);
    const snapshot = { name: SERVED, alias: null, registered: false, rejected: false, at: null, gate: null, verdict: null, epochs: null, train_rows: null, eval: null, scored: false };
    assert.equal(snapshot_standing({ ...snapshot, alias: 'champion' }).word, 'SERVED');
    assert.equal(snapshot_standing({ ...snapshot, verdict: 'REFUSE' }).word, 'REFUSED');
    assert.equal(snapshot_standing(snapshot).word, 'UNJUDGED');
});
