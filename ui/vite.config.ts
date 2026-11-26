import { fileURLToPath } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

/**
 * One build, seven pages (`src/config/pages.ts` owns the names). The bridge serves `dist/` at
 * `/`, and each page opens the others by relative address, so `base` is relative: the build
 * works at any path, behind any proxy.
 *
 * No alias: sources import each other by relative path, so a host application that imports
 * `@tldrack/ui/…` needs nothing from this file.
 */
const page = (name: string): string => fileURLToPath(new URL(name, import.meta.url));

export default defineConfig({
    base: './',
    plugins: [vue()],
    // `@tldrack/schemas` is a link to `../schemas` in a checkout, which has no node_modules of
    // its own here: its `zod` is this package's, and there is exactly one copy in the bundle.
    resolve: { dedupe: ['zod'] },
    // The evals page bundles the results files, which live beside this package (`../bench`).
    server: { fs: { allow: ['..'] } },
    build: {
        target: 'es2023',
        rollupOptions: {
            input: {
                index: page('index.html'),
                review: page('review.html'),
                diagram: page('diagram.html'),
                office: page('office.html'),
                evals: page('evals.html'),
                settings: page('settings.html'),
                training: page('training.html'),
            },
        },
    },
});
