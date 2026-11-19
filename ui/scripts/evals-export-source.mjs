/**
 * What the static EVALS export is made from, as one hash. `export-evals.mjs` stamps it into
 * `docs/evals/index.html`; `tests/unit/evals-export.test.ts` recomputes it, so an export older
 * than its sources fails a test instead of shipping stale numbers.
 *
 * The hash covers the page's own sources and every results file. It does not cover the
 * libraries the page is built with: an upgrade of those changes the bytes, never the numbers.
 */
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO = new URL('../../', import.meta.url);
export const EXPORT_PAGE = new URL('docs/evals/index.html', REPO);
/** The stamp's name in the export's `<head>`. */
export const SOURCE_META = 'tldrack-evals-source';
/** The README's figures, each in both themes: `docs/evals/figures/<figure>.<theme>.png`. */
export const FIGURES = ['read-less', 'labels-buy', 'roc'];
export const THEMES = ['light', 'dark'];
/**
 * A figure drawn from an optional block of the results file, by the block's key. The page draws
 * no panel for a block the file leaves null, so no figure is taken: a withheld `rack` (the
 * operator's own use, opt-in at `laya-lab bench --rack`) never reaches a PNG either.
 */
export const FIGURE_BLOCK = { 'read-less': 'rack' };

/** The figures one results file yields. */
export function figures_for(results) {
    return FIGURES.filter((figure) => !(figure in FIGURE_BLOCK) || results?.[FIGURE_BLOCK[figure]] != null);
}
/**
 * The figures' own stamp: the source hash they were taken at, and each PNG's sha256. A figures
 * run writes it; a `--no-figures` run leaves it, so a page refreshed without its figures fails
 * the test until the figures are taken again.
 */
export const FIGURES_MANIFEST = new URL('docs/evals/figures/manifest.json', REPO);

export function figure_files(figures = FIGURES) {
    return figures.flatMap((figure) => THEMES.map((theme) => `${figure}.${theme}.png`));
}

export function sha256_file(url) {
    return createHash('sha256').update(readFileSync(url)).digest('hex');
}

const ROOT = fileURLToPath(REPO);

function files_under(dir) {
    return readdirSync(join(ROOT, dir), { withFileTypes: true, recursive: true })
        .filter((entry) => entry.isFile())
        .map((entry) => relative(ROOT, join(entry.parentPath, entry.name)).split(sep).join('/'));
}

export function evals_source_files() {
    return [
        'ui/evals.html',
        'ui/scripts/export-evals.mjs',
        'ui/scripts/evals-export-source.mjs',
        'schemas/src/bench-results.ts',
        ...files_under('ui/src/evals/'),
        ...files_under('bench/results/').filter((file) => file.endsWith('.json')),
    ].sort();
}

export function evals_source_hash() {
    const hash = createHash('sha256');
    for (const file of evals_source_files()) {
        hash.update(`${file}\0`);
        hash.update(readFileSync(new URL(file, REPO)));
        hash.update('\0');
    }
    return hash.digest('hex');
}
