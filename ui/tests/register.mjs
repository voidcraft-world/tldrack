// `npm test` preloads this. It gives bare `node --test` what it needs to load the sources the
// way the build does. Sources import each other by relative path with the extension written, so
// everything else is node's own resolution.
//
// 1. The two Vite import forms a module under test may use: `x?raw` is the file's text and
//    `x?url` is its address.
// 2. A sibling package linked by `file:` (`@tldrack/schemas` → `../schemas`) takes its
//    dependencies from THIS package. Node resolves a link to its real directory, which has no
//    `node_modules` in a checkout where only `ui/` was installed (the CI job). The build makes the
//    same choice (`vite.config.ts` `dedupe`), so a test and the page share one copy of `zod`.
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';

const QUERIES = ['?raw', '?url'];
const PACKAGE = new URL('../', import.meta.url).href;
const SIBLINGS = new URL('../../', import.meta.url).href;

registerHooks({
    resolve(specifier, context, next_resolve) {
        const query = QUERIES.find((q) => specifier.endsWith(q));
        if (query) {
            const file = next_resolve(specifier.slice(0, -query.length), context);
            return { url: `${file.url}${query}`, shortCircuit: true };
        }
        try {
            return next_resolve(specifier, context);
        } catch (error) {
            const parent = context.parentURL ?? '';
            const bare = !specifier.startsWith('.') && !specifier.startsWith('/') && !specifier.includes(':');
            const from_sibling = parent.startsWith(SIBLINGS) && !parent.startsWith(PACKAGE);
            if (error?.code !== 'ERR_MODULE_NOT_FOUND' || !bare || !from_sibling) throw error;
            return next_resolve(specifier, { ...context, parentURL: `${PACKAGE}package.json` });
        }
    },
    load(url, context, next_load) {
        const query = QUERIES.find((q) => url.endsWith(q));
        if (!query) return next_load(url, context);
        const file = new URL(url);
        file.search = '';
        const value = query === '?raw' ? readFileSync(file, 'utf8') : file.href;
        return { format: 'module', shortCircuit: true, source: `export default ${JSON.stringify(value)};` };
    },
});
