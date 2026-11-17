import type { RackSession } from './multi-terminal-layout.ts';

export interface SessionNode {
    id: string;
    entry: RackSession;
    missing: boolean;
    children: SessionNode[];
    count: number;
    attention: number;
    rank: number;
}

/** One forest for list and graph. Missing parents get a context node; cycles
 * are cut deterministically and duplicate identities never steal children.
 * Family urgency is recursive, so a grandchild can lift its entire family.
 */
export function session_families(sessions: readonly RackSession[], rank: (entry: RackSession) => number): SessionNode[] {
    const nodes = new Map<string, SessionNode>();
    const by_identity = new Map<string, SessionNode>();
    const duplicate_ids = new Set<string>();
    for (const entry of [...sessions].sort((a, b) => a.session.localeCompare(b.session))) {
        const id = entry.lineage?.id ?? `session:${entry.session}`;
        const node: SessionNode = { id, entry, missing: false, children: [], count: 1,
            attention: Number(entry.live && entry.state === 'attention'), rank: rank(entry) };
        if (nodes.has(id)) {
            duplicate_ids.add(id);
            node.id = `session:${entry.session}`;
        }
        nodes.set(node.id, node);
        if (entry.lineage) by_identity.set(id, node);
    }
    for (const id of duplicate_ids) by_identity.delete(id);
    const parents = new Map<string, string>();
    for (const node of [...nodes.values()]) {
        const parent_id = node.entry.lineage?.parent_id;
        if (!parent_id || parent_id === node.id || duplicate_ids.has(parent_id)) continue;
        let parent = by_identity.get(parent_id);
        if (!parent) {
            parent = nodes.get(`missing:${parent_id}`);
            if (!parent) {
                parent = { id: `missing:${parent_id}`, missing: true, children: [], count: 0, attention: 0,
                    rank: Number.MAX_SAFE_INTEGER,
                    entry: { session: `missing:${parent_id}`, auto_name: node.entry.lineage?.parent_name ?? 'Parent session',
                        state: 'unknown', live: false, repo: node.entry.repo, cwd: node.entry.cwd } };
                nodes.set(parent.id, parent);
            }
        }
        let ancestor: string | undefined = parent.id;
        while (ancestor && ancestor !== node.id) ancestor = parents.get(ancestor);
        if (ancestor === node.id) continue;
        parents.set(node.id, parent.id);
        parent.children.push(node);
    }
    const compare = (a: SessionNode, b: SessionNode): number =>
        a.rank - b.rank || a.entry.session.localeCompare(b.entry.session);
    const fold = (node: SessionNode): void => {
        for (const child of node.children) {
            fold(child);
            node.count += child.count;
            node.attention += child.attention;
            node.rank = Math.min(node.rank, child.rank);
        }
        node.children.sort(compare);
    };
    const roots = [...nodes.values()].filter((node) => !parents.has(node.id));
    roots.forEach(fold);
    return roots.sort(compare);
}

export interface FamilyPosition {
    node: SessionNode;
    depth: number;
    ancestors: string[];
}

export function flatten_family(root: SessionNode): FamilyPosition[] {
    const result: FamilyPosition[] = [];
    const visit = (node: SessionNode, ancestors: string[]): void => {
        result.push({ node, depth: ancestors.length, ancestors });
        for (const child of node.children) visit(child, [...ancestors, node.id]);
    };
    visit(root, []);
    return result;
}

/** Soft row budget: never cut a family in half, and always show the first. */
export function visible_families(roots: SessionNode[], limit: number): SessionNode[] {
    const shown: SessionNode[] = [];
    let count = 0;
    for (const root of roots) {
        if (count >= Math.max(1, limit)) break;
        shown.push(root);
        count += root.count;
    }
    return shown;
}
