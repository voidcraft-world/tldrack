import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * The package's public surface. A host application imports `@tldrack/ui/<module>`, and nothing
 * but `exports` decides what that reaches: a module left out is unreachable for a host, and a
 * target that no longer exists fails only in the host's build.
 */
const ROOT = new URL('../../', import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('package.json', ROOT), 'utf8')) as {
    name: string;
    exports: Record<string, string>;
    files: string[];
    scripts: Record<string, string>;
};
const modules = Object.entries(manifest.exports).filter(([key]) => key !== './package.json');

test('every export is a source module that exists, published under its own path', () => {
    assert.equal(manifest.name, '@tldrack/ui');
    assert.ok(modules.length > 0);
    for (const [key, target] of modules) {
        assert.equal(target, `./src/${key.slice(2)}`, key);
        assert.ok(existsSync(new URL(target, ROOT)), `${key} points at ${target}, which does not exist`);
    }
});

test('the specifier a host writes resolves to that module', () => {
    for (const [key, target] of modules) {
        const resolved = import.meta.resolve(`@tldrack/ui/${key.slice(2)}`);
        assert.equal(resolved, new URL(target, ROOT).href, key);
    }
});

test('the three transports and the two mounts are exported: a host embeds the rack through them', () => {
    for (const key of [
        './agent-rack/rack-transport.ts',
        './agent-rack/rack-bus-transport.ts',
        './agent-rack/rack-http-transport.ts',
        './agent-rack/mount.ts',
    ]) {
        assert.ok(key in manifest.exports, key);
    }
    const mount = readFileSync(new URL('src/agent-rack/mount.ts', ROOT), 'utf8');
    for (const name of ['export function mount_rack_window', 'export function mount_rack_frame']) {
        assert.ok(mount.includes(name), name);
    }
});

test('an exported module imports nothing through an alias, so any bundler can load it', () => {
    const seen = new Set<string>();
    const queue = modules.map(([, target]) => fileURLToPath(new URL(target, ROOT)));
    while (queue.length > 0) {
        const file = queue.pop()!;
        if (seen.has(file) || !/\.(ts|vue)$/.test(file)) continue;
        seen.add(file);
        const source = readFileSync(file, 'utf8');
        for (const match of source.matchAll(/(?:\bfrom\s+|\bimport\s*\(\s*|^\s*import\s+)['"]([^'"\n]+)['"]/gm)) {
            const specifier = match[1]!;
            assert.ok(!specifier.startsWith('@/') && !specifier.startsWith('~/'), `${file} imports ${specifier}`);
            assert.ok(!/(^|\/)\.\.\/.*\bschemas\/src\//.test(specifier), `${file} reaches the schemas by path: ${specifier}`);
            if (specifier.startsWith('.')) queue.push(fileURLToPath(new URL(specifier.split('?')[0]!, `file://${file}`)));
        }
    }
    assert.ok(seen.size >= modules.length);
});

test('the release ships the sources and the build, and builds with one script', () => {
    for (const entry of ['src', 'dist']) assert.ok(manifest.files.includes(entry), entry);
    assert.equal(typeof manifest.scripts.build, 'string');
});
