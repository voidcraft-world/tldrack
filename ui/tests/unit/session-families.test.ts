import test from 'node:test';
import assert from 'node:assert/strict';
import { build_rack_rows, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { session_families, flatten_family } from '../../src/engine/terminal/session-families.ts';
import { session_graph } from '../../src/engine/terminal/session-graph.ts';
import { parse_session_row } from '../../src/engine/terminal/terminal-status-logic.ts';

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, '0')}`;
function agent(name: string, n: number, parent?: number, state: RackSession['state'] = 'busy'): RackSession {
    return { session: name, live: true, state, repo: 'project',
        lineage: { id: id(n), ...(parent ? { parent_id: id(parent), parent_name: 'original-parent' } : {}) } };
}
function rows(sessions: RackSession[], max_sessions = 20) {
    return build_rack_rows({ sessions, max_sessions, focused: null });
}

test('a blocked grandchild raises the intact family; siblings sort by urgency', () => {
    const result = rows([agent('other', 1, undefined, 'free'), agent('parent', 2),
        agent('busy-child', 3, 2), agent('child', 4, 2), agent('grandchild', 5, 4, 'attention')]);
    assert.deepEqual(result.filter((r) => r.kind === 'session').map((r) => [r.session, r.depth]),
        [['parent', 0], ['child', 1], ['grandchild', 2], ['busy-child', 1], ['other', 0]]);
    assert.equal(result[0].attention_count, 1);
    assert.equal(result[0].child_count, 3);
});

test('overflow keeps complete families, including children in other repositories', () => {
    const result = rows([agent('parent', 1), { ...agent('child', 2, 1, 'attention'), repo: 'elsewhere' }, agent('other', 3)], 1);
    assert.deepEqual(result.filter((r) => r.kind === 'session').map((r) => r.session), ['parent', 'child']);
    assert.equal(result.at(-1)?.text, '+1 MORE');
});

test('closed parents retain a shared context node; a reused name cannot adopt children', () => {
    const result = rows([agent('original-parent', 4), agent('one', 2, 1), agent('two', 3, 1)]);
    const missing = result.find((r) => r.missing_parent)!;
    assert.equal(missing.child_count, 2);
    assert.deepEqual(missing.actions, []);
    assert.equal(result.find((r) => r.session === 'original-parent')?.child_count, 0);
});

test('rename preserves ancestry and graph identity', () => {
    const before = session_graph([agent('parent', 1), agent('child', 2, 1)]);
    const after = session_graph([agent('renamed', 1), agent('child', 2, 1)]);
    assert.deepEqual(before.edges, after.edges);
    assert.deepEqual(before.nodes.map((n) => n.id), after.nodes.map((n) => n.id));
});

test('graph positions do not change on urgency updates', () => {
    const sessions = [agent('parent', 1), agent('one', 2, 1), agent('two', 3, 1)];
    const before = session_graph(sessions);
    const after = session_graph(sessions.map((s) => ({ ...s, state: 'attention' })));
    assert.deepEqual(before.nodes.map((n) => n.position), after.nodes.map((n) => n.position));
});

test('cycles and duplicate identities cannot lose or recursively loop sessions', () => {
    for (const entries of [[agent('a', 1, 2), agent('b', 2, 1)],
        [agent('a', 1), agent('b', 1), agent('c', 3, 1)]]) {
        const forest = session_families(entries, () => 0);
        const names = forest.flatMap(flatten_family).filter((x) => !x.node.missing).map((x) => x.node.entry.session);
        assert.deepEqual(names.sort(), entries.map((s) => s.session).sort());
    }
});

test('snapshot boundary rejects malformed ancestry', () => {
    assert.equal(parse_session_row({ session: 'a', lineage: { id: '__proto__' } })?.lineage, undefined);
    assert.deepEqual(parse_session_row({ session: 'a', lineage: agent('a', 1).lineage })?.lineage, { id: id(1) });
});
