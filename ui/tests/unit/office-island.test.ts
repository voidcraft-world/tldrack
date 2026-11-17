import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { Mesh, MeshBasicMaterial } from 'three';
import { with_island, type WorktreeIsland } from '../../src/engine/terminal/rack-view.ts';
import { office_fleet, parse_office_message } from '../../src/engine/terminal/agent-office-link.ts';
import { build_office } from '../../src/engine/terminal/agent-office-logic.ts';
import { island_status } from '../../src/engine/terminal/island-status.ts';
import { OfficeIsland } from '../../src/engine/terminal/office-island.ts';
import type { RackSession, SessionIsland } from '../../src/engine/terminal/multi-terminal-layout.ts';

const TREE = { path: '/worktrees/demo/nav', branch: 'fix/nav' };
const AGENT: RackSession = { session: 'worker', state: 'busy', live: true, repo: 'demo', cwd: `${TREE.path}/ui` };

function island(pr_state?: SessionIsland['pr_state']): SessionIsland {
    return { path: TREE.path, label: TREE.branch, title: 'Worktree: fix/nav', ...(pr_state ? { pr_state } : {}) };
}

function relay(sessions: RackSession[]) {
    const message = parse_office_message(JSON.parse(JSON.stringify({ type: 'office:fleet',
        fleet: office_fleet({ sessions, focused: null, reachable: true }),
    })));
    assert.ok(message?.type === 'office:fleet');
    return message.fleet;
}

describe('worktree islands in the office', () => {
    test('exact PR states survive the cwd join, relay and layout, without moving desks', () => {
        let geometry_key: string | undefined;
        for (const state of ['none', 'open', 'closed', 'merged', undefined] as const) {
            const tree: WorktreeIsland = { ...TREE, ...(state ? { pr: { state, number: state === 'none' ? null : 12, url: null } } : {}) };
            const fleet = relay([
                with_island(AGENT, [tree]),
                with_island({ ...AGENT, session: 'main', cwd: '/code/demo' }, [tree]),
                with_island({ ...AGENT, session: 'svc-vite' }, [tree]),
            ]);
            const layout = build_office(fleet.sessions);
            const agents = layout.rooms.flatMap(room => room.desks.map(desk => desk.agent));
            const worktree = agents.find(agent => agent.session === 'worker')!.island!;
            assert.ok(worktree);
            assert.equal(worktree.pr_state, state);
            assert.equal(island_status(worktree).merged, state === 'merged');
            assert.equal(agents.find(agent => agent.session === 'main')!.island, undefined);
            assert.equal(agents.find(agent => agent.session === 'svc-vite')!.island, undefined);
            if (geometry_key) assert.equal(layout.geometry_key, geometry_key);
            geometry_key = layout.geometry_key;
        }
    });

    test('old senders never turn a handled PR into proof of a merge', () => {
        for (const pr of ['needed', 'handled', 'unknown', undefined] as const) {
            const projected = relay([{ ...AGENT, island: { ...island(), ...(pr ? { pr } : {}) } }]);
            const status = island_status(projected.sessions[0].island!);
            assert.equal(status.merged, false);
            assert.equal(status.label, pr === 'handled' ? 'PR OPEN / MERGED' : pr === 'needed' ? 'NEEDS PR' : 'PR UNKNOWN');
        }
    });

    test('a malformed island is dropped without dropping the agent', () => {
        for (const bad of [null, [], 'island', { ...island(), path: '' }, { ...island(), label: 4 },
            { ...island(), title: 'x'.repeat(4097) }, { ...island(), pr: 'merged' }, { ...island(), pr_state: 'maybe' }]) {
            const message = parse_office_message({ type: 'office:fleet', fleet: { sessions: [{ ...AGENT, island: bad }] } });
            assert.ok(message?.type === 'office:fleet');
            assert.equal(message.fleet.sessions.length, 1);
            assert.equal(message.fleet.sessions[0].island, undefined);
        }
    });

    test('blue means needs a PR, green means handled, and merged has its own readout', () => {
        assert.equal(island_status(island('none')).color, '#58a6ff');
        assert.equal(island_status(island('closed')).color, '#58a6ff');
        assert.equal(island_status(island('open')).color, '#2ea043');
        assert.equal(island_status(island('merged')).color, '#2ea043');
        assert.equal(island_status(island('merged')).label, 'PR MERGED');
        assert.equal(island_status(island()).color, '#93ffd4');
    });

    test('a live PR change reuses the island geometry and disposal releases every owned resource', () => {
        const drawing = new OfficeIsland();
        const meshes: Mesh[] = [];
        drawing.group.traverse(object => { if (object instanceof Mesh) meshes.push(object); });
        const geometries = new Set(meshes.map(mesh => mesh.geometry));
        const materials = new Set(meshes.map(mesh => mesh.material as MeshBasicMaterial));
        const disposed = new Set<unknown>();
        for (const resource of [...geometries, ...materials]) resource.addEventListener('dispose', () => disposed.add(resource));
        drawing.update(island('none'));
        const cap = meshes[0];
        assert.equal((cap.material as MeshBasicMaterial).color.getHexString(), '58a6ff');
        drawing.update(island('merged'));
        assert.equal((cap.material as MeshBasicMaterial).color.getHexString(), '2ea043');
        assert.equal(drawing.group.userData.pr_state, 'merged');
        drawing.update(island());
        assert.equal((cap.material as MeshBasicMaterial).color.getHexString(), '93ffd4');
        assert.equal(disposed.size, 0);
        for (const mesh of meshes) assert.ok(geometries.has(mesh.geometry));
        drawing.dispose();
        assert.equal(disposed.size, geometries.size + materials.size);
        assert.equal(drawing.group.children.length, 0);
    });
});
