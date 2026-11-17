/**
 * The results file the page shows, bundled at build time: the page is static and asks no
 * server for anything. Which file: owned by evals-model.ts (`pick_results_file`) — see the
 * note there.
 *
 * Every file in `bench/results/` rides in the bundle, because the pick runs in the page. A
 * file is aggregates only, a few tens of kilobytes.
 */
import { base_name, pick_results_file } from './evals-model.ts';

const FILES = import.meta.glob('../../../bench/results/*.json', { eager: true, import: 'default' }) as Record<string, unknown>;

export function load_results(): { file: string | null; raw: unknown } {
    const path = pick_results_file(Object.keys(FILES));
    return path === null ? { file: null, raw: null } : { file: base_name(path), raw: FILES[path] };
}
