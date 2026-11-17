import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import type { BenchResults } from '@tldrack/schemas/bench-results.ts';

import {
    cheapest_step,
    cost_at,
    cost_steps,
    default_focus,
    draw_order,
    fmt_day,
    fmt_day_range,
    fmt_delta,
    fmt_interval,
    fmt_labels,
    fmt_ms,
    fmt_params,
    fmt_share,
    focused_voter,
    gain_domain,
    headline_set,
    label_gains,
    parse_results,
    pick_results_file,
    read_less,
    roc_point_at,
    roc_tpr_at,
    scored_voters,
    series_color,
    set_result,
    sets_in,
    voter_slots,
} from '../../src/evals/evals-model.ts';

/** The fixture the page is built on: the schemas package owns the file and tests that it parses. */
const RAW: unknown = JSON.parse(readFileSync(new URL('../../../bench/results/fixture.json', import.meta.url), 'utf8'));

function fixture(): BenchResults {
    const parsed = parse_results(structuredClone(RAW));
    assert.ok(parsed.ok);
    return parsed.results;
}

test('a real run wins over the fixture, and the newest real run wins', () => {
    const dir = '../../../bench/results';
    assert.equal(pick_results_file([`${dir}/fixture.json`]), `${dir}/fixture.json`);
    assert.equal(pick_results_file([`${dir}/fixture.json`, `${dir}/2026-11-14.json`, `${dir}/2026-11-20.json`]), `${dir}/2026-11-20.json`);
    assert.equal(pick_results_file([`${dir}/2026-11-14.json`]), `${dir}/2026-11-14.json`);
    assert.equal(pick_results_file([]), null);
});

test('a file is read through its schema, and a bad one is refused with where it broke', () => {
    assert.equal(fixture().fixture, true);
    const broken = structuredClone(RAW) as { voters: { name: string }[]; rows?: unknown };
    broken.voters[1]!.name = broken.voters[0]!.name;
    broken.rows = [{ text: 'a row that must never ship' }];
    const parsed = parse_results(broken);
    assert.equal(parsed.ok, false);
    assert.ok(!parsed.ok && parsed.issues.some((issue) => issue.includes('rows')), 'a row list beside the numbers is refused');
    assert.ok(!parsed.ok && parsed.issues.some((issue) => issue.startsWith('voters.1.name')), 'an issue names its path');
    assert.equal(parse_results(null).ok, false);
});

test('the charts open on the held-out set, on the served voter', () => {
    const results = fixture();
    assert.deepEqual(sets_in(results), ['golden', 'heldout']);
    assert.equal(headline_set(results), 'heldout');
    assert.equal(headline_set({ ...results, sets: results.sets.filter((result) => result.set === 'golden') }), 'golden');
    assert.equal(default_focus(results.voters), 'laya-ft');
    assert.equal(default_focus(results.voters.map((voter) => ({ ...voter, served: false }))), results.voters[0]!.name);
});

test('a colour follows the voter, and a ninth voter gets no made-up hue', () => {
    const results = fixture();
    const slots = voter_slots(results.voters);
    assert.deepEqual(Object.values(slots), [1, 2, 3, 4, null], 'the models take the slots in order; the ensemble takes none');
    const many = Array.from({ length: 10 }, (_, i) => ({ ...results.voters[0]!, name: `v${i}` }));
    assert.equal(voter_slots(many).v7, 8);
    assert.equal(voter_slots(many).v8, null);
    const mean_first = voter_slots([results.voters[4]!, ...results.voters.slice(0, 4)]);
    assert.deepEqual(Object.values(mean_first), [null, 1, 2, 3, 4], 'an ensemble ahead of the models costs them no colour');
    assert.equal(series_color(3), 'var(--ev-series-3)');
    assert.equal(series_color(null), 'var(--ev-series-other)');

    const heldout = set_result(results, 'heldout')!;
    const fewer = { ...heldout, scores: heldout.scores.filter((score) => score.voter !== 'laya-base') };
    const scored = scored_voters(fewer, results.voters, slots, 'laya-base');
    assert.deepEqual(scored.map((entry) => entry.color), ['var(--ev-series-1)', 'var(--ev-series-3)', 'var(--ev-series-4)', 'var(--ev-series-other)'], 'a voter missing from a set repaints nobody');
    assert.deepEqual(scored.map((entry) => entry.rest), [false, false, false, true], 'the ensemble is drawn thin and folded out of the legend');
    assert.equal(focused_voter(scored)!.voter.name, 'laya-ft', 'a pick this set did not score falls back to the first voter');

    const picked = scored_voters(heldout, results.voters, slots, 'mean-ft-zs');
    assert.equal(picked.at(-1)!.color, 'var(--ev-ink)', 'a picked ensemble is drawn in the ink, apart from the other neutral lines');
    assert.equal(picked.at(-1)!.rest, false);
    assert.deepEqual([...picked].sort(draw_order).map((entry) => entry.voter.name).at(-1), 'mean-ft-zs', 'the picked voter is drawn last, on top');
    assert.equal([...scored].sort(draw_order)[0]!.voter.name, 'mean-ft-zs', 'the neutral lines are drawn first, underneath');
});

test('read less: every judged turn end is counted once', () => {
    const results = fixture();
    const view = read_less(results.fleet!);
    assert.equal(view.judged, 2400);
    assert.equal(view.kept, 2140);
    assert.equal(fmt_share(view.kept_share), '89%');
    assert.deepEqual(view.parts.map((part) => [part.key, part.n]), [['done', 1500], ['reply', 420], ['held', 220], ['reached', 260]]);
    assert.equal(view.parts.reduce((sum, part) => sum + part.n, 0), view.judged);
    assert.equal(view.per_day.judged, 80);
    assert.equal(fmt_share(view.taken_share!), '34%');
    assert.equal(read_less({ ...results.fleet!, next_step: null }).taken_share, null);
    const idle = read_less({ ...results.fleet!, judged: 0, reached: 0, verdicts: { done: 0, reply: 0, open: 0 } });
    assert.equal(idle.kept_share, 0, 'an empty window divides by nothing');
});

test('what the labels buy: a fine-tune beside its base, with the direction that is good', () => {
    const results = fixture();
    const [gain, ...rest] = label_gains(results, 'heldout');
    assert.equal(rest.length, 0);
    assert.equal(gain!.base.name, 'laya-base');
    assert.equal(gain!.tune.name, 'laya-ft');
    assert.equal(gain!.labels, 480);
    const [auc, cost, brier] = gain!.rows;
    assert.equal(fmt_delta(auc!.delta!, 3), '+0.093');
    assert.equal(auc!.better, true, 'AUC up is better');
    assert.equal(fmt_delta(cost!.delta!, 0), '−7');
    assert.equal(cost!.better, true, 'cost down is better');
    assert.equal(brier!.better, true);
    assert.equal(auc!.apart, false, 'the two intervals share values, and the page says so');
    assert.deepEqual(gain_domain(auc!), [0.5, 1]);
    assert.deepEqual(gain_domain(cost!), [0, 50]);
    assert.deepEqual(gain_domain({ ...auc!, base: { value: 0.5, lo: 0.4, hi: 0.6 } }), [0, 1], 'an interval under chance widens the axis');

    const apart = label_gains(
        { ...results, sets: results.sets.map((set) => ({ ...set, scores: set.scores.map((score) => (score.voter === 'laya-ft' ? { ...score, auc: { value: 0.99, lo: 0.98, hi: 1 } } : score)) })) },
        'heldout',
    );
    assert.equal(apart[0]!.rows[0]!.apart, true);
    const no_base = { ...results, sets: results.sets.map((set) => ({ ...set, scores: set.scores.filter((score) => score.voter !== 'laya-base') })) };
    assert.deepEqual(label_gains(no_base, 'heldout'), [], 'a pair needs both of its voters scored on the set');
});

test('a cut lands on the curve where the rack would flag', () => {
    const roc = [
        { threshold: 1, fpr: 0, tpr: 0 },
        { threshold: 0.9, fpr: 0, tpr: 0.5 },
        { threshold: 0.6, fpr: 0.25, tpr: 0.75 },
        { threshold: 0.2, fpr: 1, tpr: 1 },
    ];
    assert.deepEqual(roc_point_at(roc, 0.7), roc[1], 'a row is flagged at or above the cut, so 0.7 flags what 0.9 flags');
    assert.deepEqual(roc_point_at(roc, 0.6), roc[2]);
    assert.deepEqual(roc_point_at(roc, 0.05), roc[3]);
    assert.equal(roc_tpr_at(roc, 0.1), 0.5);
    assert.equal(roc_tpr_at(roc, 0.25), 0.75);
    assert.equal(roc_tpr_at(roc, 1), 1);
});

test('cost against the cut covers [0, 1] and agrees with the operating point', () => {
    const results = fixture();
    for (const result of results.sets) {
        for (const score of result.scores) {
            const steps = cost_steps(score, result.n, results.protocol.costs);
            assert.equal(steps[0]!.threshold, 0);
            assert.equal(steps.at(-1)!.threshold, 1);
            assert.ok(steps.every((step, i) => i === 0 || step.threshold >= steps[i - 1]!.threshold), 'thresholds rise');
            assert.equal(steps[0]!.missed_open, 0, 'a cut of 0 flags every row');
            assert.equal(steps.at(-1)!.false_warnings, 0, 'a cut of 1 flags none');
            const live = cost_at(steps, score.operating.threshold);
            assert.equal(live.false_warnings, score.operating.false_warnings, `${result.set} ${score.voter}`);
            assert.equal(live.missed_open, score.operating.missed_open, `${result.set} ${score.voter}`);
            assert.equal(live.cost, score.operating.cost.value);
            assert.ok(cheapest_step(steps).cost <= live.cost);
        }
    }
    const tie = [
        { threshold: 0, false_warnings: 5, missed_open: 0, cost: 10 },
        { threshold: 0.4, false_warnings: 1, missed_open: 2, cost: 4 },
        { threshold: 0.8, false_warnings: 0, missed_open: 4, cost: 4 },
        { threshold: 1, false_warnings: 0, missed_open: 9, cost: 9 },
    ];
    assert.equal(cheapest_step(tie).threshold, 0.8, 'among equal costs, the cut that shows the fewest ⚠️');
});

test('readouts', () => {
    assert.equal(fmt_share(0.092), '9.2%');
    assert.equal(fmt_share(0.625), '63%');
    assert.equal(fmt_share(0), '0%');
    assert.equal(fmt_interval({ value: 0.85, lo: 0.7714, hi: 0.9183 }, 3), '0.771 – 0.918');
    assert.equal(fmt_delta(0, 3), '±0.000');
    assert.equal(fmt_ms(1.2), '1.2 ms');
    assert.equal(fmt_ms(0), '<0.1 ms');
    assert.equal(fmt_ms(2041.3), '2,041 ms');
    assert.equal(fmt_labels(480), '480 operator labels');
    assert.equal(fmt_labels(0), 'operator labels (count not recorded)', 'the bench writes 0 for a count it could not make');
    assert.equal(fmt_ms(410), '410 ms');
    assert.equal(fmt_params(395), '395M');
    assert.equal(fmt_params(1700), '1.7B');
    assert.equal(fmt_day('2026-11-13T00:00:00Z'), 'Nov 13, 2026');
    assert.equal(fmt_day_range('2026-10-14', '2026-11-12'), 'Oct 14 – Nov 12, 2026');
    assert.equal(fmt_day_range('2026-12-20', '2027-01-18'), 'Dec 20, 2026 – Jan 18, 2027');
});
