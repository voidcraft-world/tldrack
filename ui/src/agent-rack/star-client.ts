/**
 * The operator's star: the handful of sessions to come back to.
 *
 * Every other mark on a row is the rack's own reading of the session. The star is the one the
 * OPERATOR sets, so it never moves by itself. What a star is, and what a rename, a close and a
 * reopen do to it: owned by `bridge/tldrack/stars.py` — see the note there.
 *
 * This file is the page's half: the one call (`POST /session-star`), the value that carries the
 * wish across a transport (`RackAction` `'star'`), and the row tests the rack draws from. A host
 * that embeds the rack serves the action with `parse_star_value` and `set_star`.
 */
import { local_fetch } from '../engine/terminal/local-network-gate.ts';

const STAR_TIMEOUT_MS = 6000;

/** The `'star'` action's `value`: the state the operator asked for, never a toggle. A press that is
 *  delivered twice (a retry, two windows) then lands on the same answer. */
const STAR_VALUES = { on: true, off: false } as const;

export function star_value(starred: boolean): string {
    return starred ? 'on' : 'off';
}

/** The wish a `'star'` action carries, or null for anything else: the value crossed a transport. */
export function parse_star_value(value: unknown): boolean | null {
    return typeof value === 'string' && Object.hasOwn(STAR_VALUES, value)
        ? STAR_VALUES[value as keyof typeof STAR_VALUES]
        : null;
}

/** The rows these tests read: a row's kind and its star. */
interface StarRow {
    kind?: string;
    starred?: boolean;
}

/** Does this bridge keep stars? Its rows say so (`starred` present); an older bridge's do not. */
export function star_known(row: StarRow): row is StarRow & { starred: boolean } {
    return row.starred !== undefined;
}

/** How many sessions carry a star: the filter's count, and whether the filter is drawn at all. */
export function starred_count(rows: readonly StarRow[]): number {
    return rows.filter((row) => row.kind === 'session' && row.starred === true).length;
}

/** The star button's tooltip and accessible name. */
export function star_title(starred: boolean): string {
    return starred ? 'Starred: a session to come back to. Press to clear the star' : 'Star this session to come back to it';
}

/** The filter's tooltip and accessible name. */
export function star_filter_title(count: number, only: boolean): string {
    const noun = count === 1 ? '1 starred session' : `${count} starred sessions`;
    return only ? `Showing only the ${noun}. Press to show every session` : `Show only the ${noun}`;
}

/** True when the bridge answered 200. An older bridge answers 404 and nothing changes; the next
 *  snapshot repaints the row truthfully either way. Never throws. */
export async function set_star(url: (path: string) => string, session: string, starred: boolean): Promise<boolean> {
    try {
        const res = await local_fetch(url('/session-star'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session, starred }),
            signal: AbortSignal.timeout(STAR_TIMEOUT_MS),
        });
        return res.ok;
    } catch {
        return false;
    }
}
