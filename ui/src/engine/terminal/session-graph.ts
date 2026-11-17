import { groups_for_parent, type TaskGroup } from '../../agent-rack/task-groups.ts';
import type { RackSession, SessionIsland } from './multi-terminal-layout.ts';
import { session_families, flatten_family } from './session-families.ts';

/** Layout ignores urgency: status updates must not move a graph under the cursor. */
export function session_graph(sessions: readonly RackSession[], groups?: readonly TaskGroup[]) {
    const roots = session_families(sessions, () => 0);
    const nodes: { id: string; type: string; position: { x: number; y: number };
        data: { session: string; label: string; state: RackSession['state']; live: boolean; missing: boolean;
            fresh?: true; island?: SessionIsland } }[] = [];
    const edges: { id: string; source: string; target: string; type: string }[] = [];
    let row = 0;
    for (const root of roots) {
        for (const { node, depth, ancestors } of flatten_family(root)) {
            nodes.push({ id: node.id, type: 'session', position: { x: depth * 280, y: row++ * 76 },
                data: { session: node.entry.session, label: node.entry.auto_name || node.entry.session,
                    state: node.entry.state, live: node.entry.live, missing: node.missing,
                    ...(node.entry.fresh ? { fresh: true as const } : {}),
                    ...(node.entry.island ? { island: node.entry.island } : {}) } });
            row += groups_for_parent(groups, node.id).length * 2.5;
            const parent = ancestors.at(-1);
            if (parent) edges.push({ id: `${parent}/${node.id}`, source: parent, target: node.id, type: 'smoothstep' });
        }
        row += 0.4;
    }
    return { nodes, edges };
}
