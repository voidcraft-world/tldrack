import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

/**
 * The package's own shape: which leaves it ships, what they may import, and that every one is reachable through
 * `exports` by the specifier a consumer writes (`@tldrack/schemas/decide.ts`, or without the extension).
 */

const SRC = new URL('../src/', import.meta.url);
const LEAVES = [
  'agent-canvas',
  'agent-history',
  'bench-results',
  'closed-sessions',
  'decide',
  'launch-directories',
  'laya-review',
  'lead',
  'missions',
  'next-step',
  'rack-metrics',
  'resource-lease',
  'session-lineage',
  'task-groups',
  'topic-labels',
];

const sources = Object.fromEntries(
  readdirSync(SRC)
    .filter((name) => name.endsWith('.ts'))
    .map((name) => [name.slice(0, -3), readFileSync(new URL(name, SRC), 'utf8')]),
);

describe('@tldrack/schemas', () => {
  it('ships exactly its leaves', () => {
    assert.deepEqual(Object.keys(sources).sort(), LEAVES);
  });

  it('imports zod and its own siblings, nothing else, so any page or server can load it', () => {
    for (const [leaf, source] of Object.entries(sources)) {
      const specifiers = [...source.matchAll(/^\s*(?:import|export)\b[^'"]*?from\s+['"]([^'"]+)['"]/gm), ...source.matchAll(/^\s*import\s+['"]([^'"]+)['"]/gm)].map((m) => m[1]!);
      for (const specifier of specifiers) {
        const sibling = /^\.\/([a-z-]+)\.ts$/.exec(specifier);
        assert.ok(specifier === 'zod' || (sibling && LEAVES.includes(sibling[1]!)), `${leaf}.ts imports ${specifier}`);
      }
    }
  });

  it('resolves every leaf through exports, with and without the extension', async () => {
    for (const leaf of LEAVES) {
      for (const specifier of [`@tldrack/schemas/${leaf}.ts`, `@tldrack/schemas/${leaf}`]) {
        const resolved = import.meta.resolve(specifier);
        assert.equal(resolved, new URL(`${leaf}.ts`, SRC).href, specifier);
      }
      const module: Record<string, unknown> = await import(`@tldrack/schemas/${leaf}.ts`);
      assert.ok(Object.keys(module).length > 0, `${leaf} exports nothing`);
    }
  });

  it('names its functions in snake_case', () => {
    for (const [leaf, source] of Object.entries(sources)) {
      for (const match of source.matchAll(/^export function ([A-Za-z0-9_]+)/gm)) {
        assert.match(match[1]!, /^[a-z][a-z0-9_]*$/, `${leaf}.ts: ${match[1]}`);
      }
    }
  });
});
