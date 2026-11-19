import type { SessionIsland } from './multi-terminal-layout.ts';

/** Island hue: blue asks for a PR, green means open, merged or landed, mint is unknown.
 * Exact status stays in words too: green alone never proves a merge. A landed island
 * (its work is on the base, `rack-view.ts::work_landed`) says LANDED ahead of any PR state. */
export function island_status(island: SessionIsland): { color: string; label: string; merged: boolean } {
    const state = island.pr_state;
    const tone = island.landed || state === 'open' || state === 'merged' ? 'handled'
        : state === 'none' || state === 'closed' ? 'needed' : island.pr;
    const color = tone === 'needed' ? '#58a6ff' : tone === 'handled' ? '#2ea043' : '#93ffd4';
    const label = island.landed ? 'LANDED' : state === 'merged' ? 'PR MERGED' : state === 'open' ? 'PR OPEN'
        : state === 'closed' ? 'PR CLOSED' : state === 'none' ? 'NO PR'
        : tone === 'handled' ? 'PR OPEN / MERGED' : tone === 'needed' ? 'NEEDS PR' : 'PR UNKNOWN';
    return { color, label, merged: island.landed === true || state === 'merged' };
}
