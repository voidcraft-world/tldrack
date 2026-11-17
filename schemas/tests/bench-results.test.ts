import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { FIXTURE_PATH, format_fixture, make_fixture } from '../scripts/bench-fixture.ts';
import { BENCH_SET_PUBLIC, benchResultsSchema, cost_curve, roc_counts, type BenchResults } from '../src/bench-results.ts';

const committed = readFileSync(FIXTURE_PATH, 'utf8');
const fixture: BenchResults = benchResultsSchema.parse(JSON.parse(committed));

/** A deep copy to break: every refusal below starts from the valid fixture. */
const copy = (): BenchResults => structuredClone(fixture);

function refused(file: unknown, path: string): void {
  const result = benchResultsSchema.safeParse(file);
  assert.equal(result.success, false, `expected a refusal at ${path}`);
  const paths = result.error.issues.map((issue) => issue.path.join('.'));
  assert.ok(paths.some((p) => p.startsWith(path)), `refused, but at ${paths.join(' | ')}, not ${path}`);
}

describe('fixture.json', () => {
  it('parses, and says it is invented', () => {
    assert.equal(fixture.fixture, true);
    assert.match(fixture.protocol.description, /Invented/);
    assert.equal(fixture.commit, null);
  });

  it('is exactly what the generator writes: edit scripts/bench-fixture.ts, never the JSON', () => {
    assert.equal(committed, `${format_fixture(make_fixture())}\n`);
  });

  it('scores every voter on every set, the public golden set and the held-out split', () => {
    assert.deepEqual(fixture.sets.map((s) => s.set), ['golden', 'heldout']);
    assert.deepEqual(BENCH_SET_PUBLIC, { golden: true, heldout: false });
    for (const set of fixture.sets) assert.deepEqual(set.scores.map((s) => s.voter), fixture.voters.map((v) => v.name));
    assert.equal(fixture.voters.filter((v) => v.served).length, 1);
  });

  it('agrees with itself: the operating point is the ROC curve at its cut, and the cost is the protocol’s weight', () => {
    for (const set of fixture.sets) {
      for (const score of set.scores) {
        const at = score.roc.filter((pt) => pt.threshold >= score.operating.threshold).at(-1)!;
        assert.deepEqual(roc_counts(at, set.n), { false_warnings: score.operating.false_warnings, missed_open: score.operating.missed_open }, score.voter);
        const { false_warnings, missed_open } = score.operating;
        assert.equal(score.operating.cost.value, fixture.protocol.costs.false_warning * false_warnings + missed_open, score.voter);
        const curve = cost_curve(score, set.n, fixture.protocol.costs);
        assert.equal(curve.length, score.roc.length);
        assert.ok(Math.min(...curve.map((c) => c.cost)) <= score.operating.cost.value, `${score.voter}: a sweep never costs more than the live cut`);
      }
    }
  });

  it('carries no row: nothing in it is a key, an id or a text longer than a label', () => {
    const strings: string[] = [];
    const walk = (value: unknown): void => {
      if (typeof value === 'string') strings.push(value);
      else if (Array.isArray(value)) value.forEach(walk);
      else if (value && typeof value === 'object') Object.values(value).forEach(walk);
    };
    walk(fixture);
    for (const text of strings) {
      assert.ok(!/^[0-9a-f]{16}$/.test(text), `a review key: ${text}`);
      assert.ok(!/^\d{8}T\d{6}-[0-9a-f]{12}$/.test(text), `a decision id: ${text}`);
    }
    assert.ok(strings.every((text) => text.length <= 400));
  });
});

describe('the results contract', () => {
  it('is strict at every level, so no row can ride in beside the numbers', () => {
    refused({ ...copy(), rows: [{ key: '0123456789abcdef', text: 'a turn' }] }, '');
    const set = copy();
    (set.sets[0] as Record<string, unknown>).rows = [];
    refused(set, 'sets.0');
    const score = copy();
    (score.sets[1]!.scores[0] as Record<string, unknown>).keys = ['0123456789abcdef'];
    refused(score, 'sets.1.scores.0');
    const bin = copy();
    (bin.sets[0]!.scores[0]!.reliability[0] as Record<string, unknown>).examples = ['a turn'];
    refused(bin, 'sets.0.scores.0.reliability.0');
  });

  it('refuses an interval that does not hold its estimate, and an AUC outside [0, 1]', () => {
    const file = copy();
    file.sets[0]!.scores[0]!.auc = { value: 0.8, lo: 0.82, hi: 0.9 };
    refused(file, 'sets.0.scores.0.auc');
    const wide = copy();
    wide.sets[0]!.scores[0]!.brier = { value: 0.5, lo: -0.1, hi: 0.6 };
    refused(wide, 'sets.0.scores.0.brier');
  });

  it('takes AUC as null exactly when a class is empty', () => {
    const empty = copy();
    const set = empty.sets[0]!;
    set.n = { open: set.n.open + set.n.done, done: 0 };
    refused(empty, 'sets.0.scores');
    const missing = copy();
    missing.sets[0]!.scores[0]!.auc = null;
    refused(missing, 'sets.0.scores.0.auc');
  });

  it('refuses a ROC curve that turns back and bins that leave a gap or miss a row', () => {
    const back = copy();
    const roc = back.sets[0]!.scores[0]!.roc;
    roc[2] = { ...roc[2]!, tpr: 0 };
    roc[1] = { ...roc[1]!, tpr: 0.5 };
    refused(back, 'sets.0.scores.0.roc');
    const gap = copy();
    gap.sets[0]!.scores[0]!.reliability[1]!.lo += 0.01;
    refused(gap, 'sets.0.scores.0.reliability');
    const lost = copy();
    const bins = lost.sets[0]!.scores[0]!.reliability;
    const full = bins.find((b) => b.n > 1)!;
    full.n -= 1;
    refused(lost, 'sets.0.scores.0.reliability');
  });

  it('keeps the roster honest: unique names, one served voter, `of` inside the file, labels on fine-tunes only', () => {
    const twice = copy();
    twice.voters.push({ ...twice.voters[0]!, served: false });
    refused(twice, 'voters');
    const two_served = copy();
    two_served.voters[1]!.served = true;
    refused(two_served, 'voters');
    const orphan = copy();
    orphan.voters[0]!.of = 'no-such-voter';
    refused(orphan, 'voters.0.of');
    const labelled_base = copy();
    labelled_base.voters[1]!.labels = 10;
    refused(labelled_base, 'voters.1.labels');
    const unknown = copy();
    unknown.sets[0]!.scores[0]!.voter = 'no-such-voter';
    refused(unknown, 'sets.0.scores.0.voter');
  });

  it('refuses more errors at the cut than rows of that class', () => {
    const file = copy();
    const set = file.sets[0]!;
    set.scores[0]!.operating.false_warnings = set.n.done + 1;
    refused(file, 'sets.0.scores.0.operating');
  });

  it('counts the fleet once: every judged turn drew one verdict, and only an open one reaches the operator', () => {
    const file = copy();
    file.fleet!.judged += 1;
    refused(file, 'fleet.verdicts');
    const reached = copy();
    reached.fleet!.reached = reached.fleet!.verdicts.open + 1;
    refused(reached, 'fleet.reached');
    assert.ok(benchResultsSchema.safeParse({ ...copy(), fleet: null }).success, 'a bench without a ledger has no fleet');
  });

  it('carries one protocol: a real file drops the fixture flag, never sets it false', () => {
    const { fixture: _flag, ...real } = copy();
    assert.ok(benchResultsSchema.safeParse(real).success);
    refused({ ...copy(), fixture: false }, 'fixture');
    refused({ ...copy(), protocol: { ...fixture.protocol, id: 'Open Work v2' } }, 'protocol.id');
  });
});
