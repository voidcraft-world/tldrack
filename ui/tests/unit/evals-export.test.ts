import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';

import {
    EXPORT_PAGE, FIGURES_MANIFEST, REPO, SOURCE_META, evals_source_files, evals_source_hash, figure_files, figures_for,
    sha256_file,
} from '../../scripts/evals-export-source.mjs';
import { pick_results_file } from '../../src/evals/evals-model.ts';

/**
 * `docs/evals/index.html` is a built copy of the EVALS page with the results inside it. Nothing
 * rebuilds it by itself, so this is what stops it from going stale: a changed page, or a new
 * results file, fails here until the export is run again.
 */
const page = existsSync(EXPORT_PAGE) ? readFileSync(EXPORT_PAGE, 'utf8') : null;

/** The figures the page yields: those of the results file it shows (`pick_results_file`). */
function expected_figures(): string[] {
    const dir = new URL('bench/results/', REPO);
    const picked = pick_results_file(readdirSync(dir).filter((name) => name.endsWith('.json')));
    assert.ok(picked !== null, 'bench/results/ holds no results file');
    return figure_files(figures_for(JSON.parse(readFileSync(new URL(picked, dir), 'utf8'))));
}

test('the static export is current with the page and the results it was built from', () => {
    assert.ok(page !== null, 'docs/evals/index.html is missing: run `npm run export:evals` in ui/');
    const stamp = new RegExp(`<meta name="${SOURCE_META}" content="([0-9a-f]{64})">`).exec(page);
    assert.ok(stamp, 'the export carries no source stamp');
    assert.equal(stamp[1], evals_source_hash(), 'docs/evals/index.html is older than its sources: run `npm run export:evals` in ui/ and commit the result');
});

test('the stamp covers the page, the schema and every results file', () => {
    const files = evals_source_files();
    for (const file of ['ui/evals.html', 'ui/src/evals/EvalsPage.vue', 'ui/src/evals/evals.css', 'schemas/src/bench-results.ts', 'bench/results/fixture.json']) {
        assert.ok(files.includes(file), file);
    }
    assert.deepEqual(files, [...files].sort(), 'a fixed order, so the hash is the same on every machine');
});

test('the export is one file: it opens from the file system', () => {
    assert.ok(page !== null);
    assert.doesNotMatch(page, /<script[^>]*\ssrc=/, 'a page opened from a file cannot load a second file as a module');
    assert.doesNotMatch(page, /<link[^>]*rel="stylesheet"/);
    assert.match(page, /<script type="module">/);
    for (const file of expected_figures()) {
        assert.ok(existsSync(new URL(file, FIGURES_MANIFEST)), `docs/evals/figures/${file}`);
    }
});

test('the figures were taken from this export, not an older one', () => {
    assert.ok(page !== null);
    assert.ok(existsSync(FIGURES_MANIFEST), 'docs/evals/figures/manifest.json is missing: run `npm run export:evals` in ui/ (with figures)');
    const manifest = JSON.parse(readFileSync(FIGURES_MANIFEST, 'utf8'));
    const stamp = new RegExp(`<meta name="${SOURCE_META}" content="([0-9a-f]{64})">`).exec(page);
    assert.equal(manifest.source, stamp?.[1], 'the figures were taken at another export than the page: a `--no-figures` run refreshed only the page');
    assert.equal(manifest.source, evals_source_hash(), 'the figures are older than their sources: run `npm run export:evals` in ui/ (with figures)');
    assert.deepEqual(Object.keys(manifest.figures).sort(), expected_figures().sort());
    for (const file of expected_figures()) {
        assert.equal(sha256_file(new URL(file, FIGURES_MANIFEST)), manifest.figures[file], `docs/evals/figures/${file} is not the figure the manifest stamped`);
    }
});

test('a figure of a withheld block is not published', () => {
    const on_disk = readdirSync(new URL('./', FIGURES_MANIFEST)).filter((name) => name.endsWith('.png')).sort();
    assert.deepEqual(on_disk, expected_figures().sort(), 'docs/evals/figures/ holds a PNG the results no longer draw: run `npm run export:evals` in ui/');
});

test('a block left null yields no figure; the others always do', () => {
    assert.deepEqual(figures_for({ rack: null }), ['labels-buy', 'roc']);
    assert.deepEqual(figures_for({ rack: { days: 7 } }), ['read-less', 'labels-buy', 'roc']);
});
