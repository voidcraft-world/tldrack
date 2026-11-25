import type { RackRow } from '../engine/terminal/multi-terminal-layout.ts';
import { shows_service_mark } from '../engine/terminal/service-sessions.ts';

/** Attention bands keep whole families and their notes together. A worker's ask lifts its
 * parent too, so folding cannot bury an ask or detach a worker from its context. */
export function attention_rows(rows: readonly RackRow[], asks: (row: RackRow) => boolean): RackRow[] {
    const families: RackRow[][] = [];
    const other: RackRow[] = [];
    for (const row of rows) {
        if (row.kind === 'cohort') continue;
        if (row.kind === 'session' && ((row.depth ?? 0) === 0 || !families.length)) families.push([]);
        if ((row.kind === 'session' || row.kind === 'note') && families.length) families.at(-1)!.push(row);
        else other.push(row);
    }
    const bands = [
        { label: 'Needs you', groups: [] as RackRow[][] },
        { label: 'In progress', groups: [] as RackRow[][] },
        { label: 'Services', groups: [] as RackRow[][] },
        { label: 'Completed', groups: [] as RackRow[][] },
    ];
    for (const family of families) {
        const members = family.filter((row) => row.kind === 'session');
        const index = members.some(asks) ? 0 : members.every((row) => row.complete) ? 3
            : members.every((row) => shows_service_mark(row.session, row.state)) ? 2 : 1;
        bands[index].groups.push(family);
    }
    return [...bands.flatMap((band, index): RackRow[] => {
        if (!band.groups.length) return [];
        const count = band.groups.flat().filter((row) => row.kind === 'session').length;
        return [{ kind: 'cohort', cohort: band.label, text: `${band.label} · ${count}`, dim: false,
            focused: false, actions: [], ...(index === 3 ? { complete: true } : {}) }, ...band.groups.flat()];
    }), ...other];
}
