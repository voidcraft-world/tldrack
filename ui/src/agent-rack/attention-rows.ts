import type { RackRow } from '../engine/terminal/multi-terminal-layout.ts';
import { shows_service_mark } from '../engine/terminal/service-sessions.ts';

/** Attention bands keep whole families and their notes together. A worker's ask lifts its
 * parent too, so folding cannot bury an ask or detach a worker from its context. */
export function attention_rows(rows: readonly RackRow[], asks: (row: RackRow) => boolean,
    directories?: ReadonlyMap<string, string>): RackRow[] {
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
        const members = index === 1 && directories ? directory_rows(band.groups, directories) : band.groups.flat();
        return [{ kind: 'cohort', cohort: band.label, text: `${band.label} · ${count}`, dim: false,
            focused: false, actions: [], ...(index === 3 ? { complete: true } : {}) }, ...members];
    }), ...other];
}

/** A family stays under its parent's directory; an absent parent uses its first known worker.
 * Only In progress is subdivided, so a newly asking family leaves any collapsed directory. */
function directory_rows(families: readonly RackRow[][], directories: ReadonlyMap<string, string>): RackRow[] {
    const groups = new Map<string, RackRow[]>();
    for (const family of families) {
        const label = family.map((row) => row.session ? directories.get(row.session) : undefined)
            .find((value) => value !== undefined) ?? 'No directory';
        const group = groups.get(label) ?? [];
        group.push(...family);
        groups.set(label, group);
    }
    return [...groups].flatMap(([label, members]): RackRow[] => [
        { kind: 'cohort', cohort: `directory:${label}`, cohort_parent: 'In progress', text: label,
            cohort_count: members.filter((row) => row.kind === 'session').length,
            dim: false, focused: false, actions: [] }, ...members,
    ]);
}

export function is_directory_heading(row: RackRow): boolean {
    return row.kind === 'cohort' && row.cohort_parent === 'In progress';
}

/** Apply band folds to freshly grouped rows, before holding rename order. Search and stars
 * bypass these folds. Keep headers/counts and trailing overflow, including when every row folds. */
export function fold_sidebar_rows(rows: readonly RackRow[], services_open: boolean,
    directory_choices: ReadonlyMap<string, boolean> = new Map()): RackRow[] {
    let hidden = false;
    return rows.filter((row) => {
        if (row.kind === 'cohort') {
            hidden = row.cohort === 'Services' ? !services_open
                : is_directory_heading(row) && directory_choices.get(row.cohort!) === true;
            return true;
        }
        return !hidden || (row.kind !== 'session' && row.kind !== 'note');
    });
}
