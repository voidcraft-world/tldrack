import type { RackRow } from '../engine/terminal/multi-terminal-layout.ts';
import { answer_to_read_shown, open_work_badge_shown } from '../config/terminal-status-flags.ts';
import { groups_for_parent, worker_delivered, type TaskGroup } from './task-groups.ts';

/**
 * A family's fold: a parent and every session under it drawn as ONE row.
 *
 * A LEAD's or a parent's workers take most of the rack (one parent had eleven children and four
 * group bars), so a folded family is the parent row plus one readout of its workers: how many, how
 * many are busy, how many need you, and one meter for all its task groups together.
 *
 * Four rules, each pinned by `tests/unit/family-fold.test.ts`:
 * 1. **It never hides an ask.** A worker that needs the operator (`row_asks`) keeps its own row
 *    under a folded parent, with the rows between it and the parent, so it never shows without the
 *    row that says whose it is. So does the focused session: you are looking at it.
 * 2. **A finished family arrives folded.** Every worker free and every task group of the parent
 *    complete (`family_finished`). It is a default, so it is never stored.
 * 3. **A press by hand wins, and is the thing remembered**, per parent `node_id`, in localStorage
 *    (`FAMILY_FOLD_KEY`), newest last, at most `FAMILY_FOLD_MAX`, and pruned once the family is gone.
 * 4. **Search and ★ ignore folds.** FIND opens the family of a match (`rack-search.ts`); the saved
 *    fold is untouched, so clearing the box folds it again.
 */

export const FAMILY_FOLD_KEY = 'tldrack:rack-families-folded';
/** Hand-set folds remembered; past this, the oldest are forgotten. */
export const FAMILY_FOLD_MAX = 48;

/** Parent `node_id` → folded (`true`) or opened by hand (`false`). Insertion order is recency. */
export type FoldChoices = ReadonlyMap<string, boolean>;

/** The stored choices, as `[[node_id, folded], …]`. Anything else reads as no choice at all. */
export function read_fold_choices(raw: string | null): Map<string, boolean> {
    try {
        const pairs: unknown = JSON.parse(raw ?? '[]');
        if (!Array.isArray(pairs)) return new Map();
        const valid = pairs.filter((pair): pair is [string, boolean] => Array.isArray(pair) && pair.length === 2
            && typeof pair[0] === 'string' && pair[0].length > 0 && pair[0].length <= 200 && typeof pair[1] === 'boolean');
        return new Map(valid.slice(-FAMILY_FOLD_MAX));
    } catch {
        return new Map();
    }
}

export function write_fold_choices(choices: FoldChoices): string {
    return JSON.stringify([...choices]);
}

/** A press by hand: the newest choice, bounded. */
export function choose_fold(choices: FoldChoices, node_id: string, folded: boolean): Map<string, boolean> {
    const next = new Map(choices);
    next.delete(node_id);
    next.set(node_id, folded);
    return new Map([...next].slice(-FAMILY_FOLD_MAX));
}

/** How long a family must be gone before its choice is forgotten: a snapshot that briefly lacks a
 *  parent (a bridge restart, lineage arriving a poll late) must not cost the fold a reload keeps. */
export const FAMILY_FOLD_GRACE_MS = 60_000;

/**
 * The choices whose family is still on the rack, or gone for less than `FAMILY_FOLD_GRACE_MS`;
 * `null` when none was dropped (nothing to write). `absent` is the caller's own record of when each
 * choice's family was last seen missing, kept across calls.
 */
export function prune_fold_choices(choices: FoldChoices, rows: readonly RackRow[], now_ms: number,
    absent: Map<string, number>, grace_ms = FAMILY_FOLD_GRACE_MS): Map<string, boolean> | null {
    const present = new Set(rows.flatMap((row) => row.kind === 'session' && row.node_id ? [row.node_id] : []));
    for (const id of absent.keys()) if (!choices.has(id) || present.has(id)) absent.delete(id);
    for (const id of choices.keys()) if (!present.has(id) && !absent.has(id)) absent.set(id, now_ms);
    const kept = [...choices].filter(([id]) => !absent.has(id) || now_ms - absent.get(id)! < grace_ms);
    for (const [id] of choices) if (!kept.some(([k]) => k === id)) absent.delete(id);
    return kept.length === choices.size ? null : new Map(kept);
}

/** The session rows under `node_id`, at any depth. */
export function family_members(rows: readonly RackRow[], node_id: string): RackRow[] {
    return rows.filter((row) => row.kind === 'session' && row.ancestors?.includes(node_id));
}

/**
 * Does this row want the operator? The ⚠️ band (its state, or Laya's open-work badge), an answer
 * to read, or a task-group worker that is blocked, failed or in error and not yet delivered.
 */
export function row_asks(row: RackRow, groups: readonly TaskGroup[] | undefined): boolean {
    if (row.kind !== 'session' || row.dim) return false;
    if (row.state === 'attention') return true;
    if (open_work_badge_shown(row.state, row.open_work ? true : null, row.dim, row.open_work ?? {})) return true;
    if (answer_to_read_shown(row.state, row.work_done, row.dim)) return true;
    return (groups ?? []).some((group) => group.workers.some((worker) => worker.session === row.session
        && !worker_delivered(worker) && (worker.stage === 'blocked' || worker.stage === 'failed' || !!worker.error)));
}

/** Every worker free and every task group of the parent complete (step 6.5's landed lanes count). */
export function family_finished(parent: RackRow, rows: readonly RackRow[], groups: readonly TaskGroup[] | undefined): boolean {
    if (!parent.node_id) return false;
    const members = family_members(rows, parent.node_id);
    return members.length > 0
        && members.every((row) => !row.dim && row.state === 'free')
        && groups_for_parent(groups, parent.node_id).every((group) => group.phase === 'complete');
}

/** The parents folded right now: a hand-set choice, else the finished-family default. */
export function folded_families(rows: readonly RackRow[], groups: readonly TaskGroup[] | undefined, choices: FoldChoices): Set<string> {
    const folded = new Set<string>();
    for (const row of rows) {
        if (row.kind !== 'session' || !row.node_id || !row.child_count) continue;
        if (choices.get(row.node_id) ?? family_finished(row, rows, groups)) folded.add(row.node_id);
    }
    return folded;
}

/**
 * The rows a fold leaves: everything not under a folded parent, plus each row that asks (or is
 * focused) with the rows between it and that parent. Order is kept.
 */
export function fold_rows(rows: readonly RackRow[], folded: ReadonlySet<string>, asks: (row: RackRow) => boolean): RackRow[] {
    if (!folded.size) return [...rows];
    const hidden = (row: RackRow): boolean => !!row.ancestors?.some((id) => folded.has(id));
    const surfacing = rows.filter((row) => row.kind === 'session' && hidden(row) && (row.focused || asks(row)));
    const context = new Set(surfacing.flatMap((row) => row.ancestors ?? []));
    const keep = new Set(surfacing);
    return rows.filter((row) => !hidden(row) || keep.has(row)
        || (row.kind === 'session' && !!row.node_id && context.has(row.node_id)));
}

/** What a folded parent's one line says. `percent` is null when the parent has no task group. */
export interface FamilyFoldReadout {
    workers: number;
    busy: number;
    asks: number;
    percent: number | null;
    blockers: number;
}

export function family_readout(parent: RackRow, rows: readonly RackRow[], groups: readonly TaskGroup[] | undefined,
    asks: (row: RackRow) => boolean): FamilyFoldReadout {
    const members = parent.node_id ? family_members(rows, parent.node_id) : [];
    const own = groups_for_parent(groups, parent.node_id);
    const total = own.reduce((sum, group) => sum + group.total, 0);
    return {
        workers: members.length,
        busy: members.filter((row) => !row.dim && row.state === 'busy').length,
        asks: members.filter(asks).length,
        // Weighted by workers, so a 1-lane group does not count as much as a 10-lane one.
        percent: own.length ? (total ? Math.round(own.reduce((sum, group) => sum + group.percent * group.total, 0) / total) : 0) : null,
        blockers: own.reduce((sum, group) => sum + group.blockers, 0),
    };
}

/** The readout's words. A zero count says nothing, so it is left out; the worker count always stays. */
export function family_readout_text(readout: FamilyFoldReadout): string {
    const parts = [`${readout.workers} ${readout.workers === 1 ? 'worker' : 'workers'}`];
    if (readout.busy) parts.push(`${readout.busy} busy`);
    if (readout.asks) parts.push(`${readout.asks} ${readout.asks === 1 ? 'needs' : 'need'} you`);
    if (!readout.busy && !readout.asks) parts.push('all free');
    return parts.join(' · ');
}
