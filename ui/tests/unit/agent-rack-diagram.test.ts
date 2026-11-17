import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { section_id, split_rack_diagram } from '../../src/agent-rack-diagram/rack-diagram-sections.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../..');
const DIAGRAM_FILE = 'ui/src/agent-rack-diagram/rack-architecture.md';
const DIAGRAM = readFileSync(path.join(REPO, DIAGRAM_FILE), 'utf8');

test('split: one tab per ## section, lead and fence separated, comments dropped', () => {
    const sections = split_rack_diagram([
        '# Title',
        '<!-- ## not a tab -->',
        '## Status pipeline',
        '',
        'Hooks are fast',
        'but miss things.',
        '',
        '```mermaid',
        'flowchart TD',
        '    a --> b',
        '```',
        '## Prose only',
        'Just words.',
    ].join('\n'));
    assert.deepEqual(sections, [
        { id: 'status-pipeline', title: 'Status pipeline', lead: 'Hooks are fast but miss things.', mermaid: 'flowchart TD\n    a --> b' },
        { id: 'prose-only', title: 'Prose only', lead: 'Just words.', mermaid: '' },
    ]);
});

test('section id: hash-safe slug', () => {
    assert.equal(section_id('Topology'), 'topology');
    assert.equal(section_id('  Launch / spawn! '), 'launch-spawn');
});

test('the shipped diagram: every tab, each with a diagram', () => {
    const sections = split_rack_diagram(DIAGRAM);
    assert.deepEqual(sections.map((s) => s.id), ['topology', 'status-pipeline', 'launch', 'close-and-reopen', 'parent-task-groups',
        'missions', 'lead', 'session-ledger', 'safety']);
    for (const section of sections) {
        assert.equal(section.id, section_id(section.title), `${section.id} is not its title's slug`);
        assert.ok(section.lead.length > 0, `${section.id} has no lead sentence`);
        assert.ok(section.mermaid.trim().length > 0, `${section.id} has no mermaid fence`);
    }
});

// The drift guard: a bridge plugin nobody drew is a rack the diagram no longer explains.
test('every plugin under plugins/ is named in rack-architecture.md', () => {
    const plugins_dir = path.join(REPO, 'plugins');
    const plugins = readdirSync(plugins_dir, { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && existsSync(path.join(plugins_dir, entry.name, 'bridge_plugin.py')))
        .map((entry) => entry.name);
    assert.ok(plugins.length > 0, 'found no plugins — the guard would pass vacuously');
    const missing = plugins.filter((name) => !DIAGRAM.includes(name));
    assert.deepEqual(missing, [], `add these to ${DIAGRAM_FILE}: ${missing.join(', ')}`);
});
